use crate::config::{
    Entry, EntryKind, Palette, PaletteSource, RendererSettings, ThemeMode, VideoInterpolation,
    VideoWhenHidden,
};
use serde_json::json;
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const MAX_OUTPUTS: usize = 32;
const MAX_OUTPUT_REPLY_BYTES: usize = 1024 * 1024;
const MAX_REFRESH_MILLIHZ: u64 = 1_000_000;
const MAX_DIAGNOSTIC_BYTES: usize = 4096;
const HELPER_TIMEOUT: Duration = Duration::from_secs(3);
const HELPER_STOP_GRACE: Duration = Duration::from_millis(250);
const PIPE_DRAIN_GRACE: Duration = Duration::from_millis(100);

#[derive(Default)]
struct CaptureState {
    bytes: Vec<u8>,
    truncated: bool,
    done: bool,
}

#[derive(Clone)]
struct BoundedCapture {
    shared: Arc<(Mutex<CaptureState>, Condvar)>,
}

struct Captured {
    bytes: Vec<u8>,
    truncated: bool,
}

impl BoundedCapture {
    fn start<R: Read + Send + 'static>(mut reader: R, limit: usize) -> Self {
        let shared = Arc::new((Mutex::new(CaptureState::default()), Condvar::new()));
        let worker_state = Arc::clone(&shared);
        thread::spawn(move || {
            let mut chunk = [0_u8; 8192];
            loop {
                match reader.read(&mut chunk) {
                    Ok(0) => break,
                    Ok(count) => {
                        let (lock, _) = &*worker_state;
                        let mut state =
                            lock.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
                        state.bytes.extend_from_slice(&chunk[..count]);
                        if state.bytes.len() > limit {
                            let discard = state.bytes.len() - limit;
                            state.bytes.drain(..discard);
                            state.truncated = true;
                        }
                    }
                    Err(_) => break,
                }
            }
            let (lock, ready) = &*worker_state;
            let mut state = lock.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            state.done = true;
            ready.notify_all();
        });
        Self { shared }
    }

    fn finish(&self) -> Captured {
        let (lock, ready) = &*self.shared;
        let state = lock.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        let (state, _) = ready
            .wait_timeout_while(state, PIPE_DRAIN_GRACE, |state| !state.done)
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        Captured {
            bytes: state.bytes.clone(),
            truncated: state.truncated || !state.done,
        }
    }
}

struct BoundedOutput {
    status: ExitStatus,
    stdout: Captured,
    stderr: Captured,
}

fn diagnostic(captured: &Captured) -> String {
    let text = String::from_utf8_lossy(&captured.bytes);
    let text = text.trim();
    if text.is_empty() && !captured.truncated {
        return String::new();
    }
    let mut rendered = text
        .chars()
        .map(|character| {
            if character == '\n' || character == '\r' || character == '\t' {
                ' '
            } else if character.is_control() {
                '\u{fffd}'
            } else {
                character
            }
        })
        .collect::<String>();
    if captured.truncated {
        if !rendered.is_empty() {
            rendered.push(' ');
        }
        rendered.push_str("[truncated]");
    }
    rendered
}

fn with_diagnostic(message: String, captured: &Captured) -> String {
    let detail = diagnostic(captured);
    if detail.is_empty() {
        message
    } else {
        format!("{message}; stderr: {detail}")
    }
}

fn run_bounded(
    command: &mut Command,
    label: &str,
    timeout: Duration,
    stdout_limit: usize,
) -> Result<BoundedOutput, String> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    let mut child = command
        .spawn()
        .map_err(|error| format!("cannot run {label}: {error}"))?;
    let stdout = BoundedCapture::start(
        child
            .stdout
            .take()
            .ok_or_else(|| format!("cannot capture {label} stdout"))?,
        stdout_limit,
    );
    let stderr = BoundedCapture::start(
        child
            .stderr
            .take()
            .ok_or_else(|| format!("cannot capture {label} stderr"))?,
        MAX_DIAGNOSTIC_BYTES,
    );
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                return Ok(BoundedOutput {
                    status,
                    stdout: stdout.finish(),
                    stderr: stderr.finish(),
                });
            }
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(10)),
            Ok(None) => {
                stop_group_with_grace(&mut child, HELPER_STOP_GRACE);
                return Err(with_diagnostic(
                    format!("{label} timed out after {} ms", timeout.as_millis()),
                    &stderr.finish(),
                ));
            }
            Err(error) => {
                stop_group_with_grace(&mut child, HELPER_STOP_GRACE);
                return Err(with_diagnostic(
                    format!("cannot inspect {label}: {error}"),
                    &stderr.finish(),
                ));
            }
        }
    }
}

pub trait VideoRenderer: Send {
    fn start(
        &mut self,
        entry: &Entry,
        output: &str,
        settings: &RendererSettings,
        refresh_millihz: Option<u64>,
    ) -> Result<(), String>;
    fn stop(&mut self);
    fn set_paused(&mut self, paused: bool) -> bool;
    fn set_volume(&mut self, muted: bool, volume: u8) -> bool;
}

pub trait WallpaperDriver: Send {
    fn begin_apply(&mut self) {}
    fn apply(
        &mut self,
        entry: &Entry,
        output: &str,
        settings: &crate::config::Settings,
    ) -> Result<(), String>;
    fn set_paused(&mut self, paused: bool);
    fn end_apply(&mut self) {}
    fn reconfigure(&mut self, settings: RendererSettings);
    fn poll_failures(&mut self) -> Vec<String> {
        Vec::new()
    }
    fn motion_active(&self, _output: &str) -> bool {
        false
    }
    fn stop(&mut self);
}

pub struct Mpvpaper {
    child: Option<Child>,
    socket: Option<PathBuf>,
    diagnostics: Option<BoundedCapture>,
}

impl Mpvpaper {
    pub fn new() -> Self {
        Self {
            child: None,
            socket: None,
            diagnostics: None,
        }
    }

    fn ipc(&self, command: serde_json::Value) -> bool {
        let Some(path) = &self.socket else {
            return false;
        };
        let Ok(mut stream) = UnixStream::connect(path) else {
            return false;
        };
        let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
        let Ok(mut encoded) = serde_json::to_vec(&json!({"command": command})) else {
            return false;
        };
        encoded.push(b'\n');
        if stream.write_all(&encoded).is_err() {
            return false;
        }
        let mut reply = [0_u8; 4096];
        matches!(stream.read(&mut reply), Ok(count) if count > 0)
    }

    fn take_exit(&mut self) -> Result<Option<(ExitStatus, String)>, String> {
        let Some(child) = self.child.as_mut() else {
            return Ok(None);
        };
        match child.try_wait() {
            Ok(Some(status)) => {
                self.child.take();
                if let Some(socket) = self.socket.take() {
                    let _ = fs::remove_file(socket);
                }
                let diagnostics = self
                    .diagnostics
                    .take()
                    .map(|capture| diagnostic(&capture.finish()))
                    .unwrap_or_default();
                Ok(Some((status, diagnostics)))
            }
            Ok(None) => Ok(None),
            Err(error) => Err(format!("cannot inspect mpvpaper: {error}")),
        }
    }
}

impl Default for Mpvpaper {
    fn default() -> Self {
        Self::new()
    }
}

impl VideoRenderer for Mpvpaper {
    fn start(
        &mut self,
        entry: &Entry,
        output: &str,
        settings: &RendererSettings,
        refresh_millihz: Option<u64>,
    ) -> Result<(), String> {
        let video = entry
            .motion
            .as_ref()
            .ok_or("video entry has no motion path")?;
        self.stop();
        let safe_output: String = output
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() { c } else { '_' })
            .collect();
        let socket = std::env::var_os("XDG_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(std::env::temp_dir)
            .join(format!("wall-in-one-mpv-{safe_output}.sock"));
        let _ = fs::remove_file(&socket);
        let mut options = vec![
            "loop-file=inf".to_string(),
            "panscan=1.0".to_string(),
            "terminal=no".to_string(),
            format!("mute={}", if settings.video_muted { "yes" } else { "no" }),
            format!("volume={}", settings.video_volume),
            format!(
                "hwdec={}",
                if settings.video_hardware_decode {
                    "auto"
                } else {
                    "no"
                }
            ),
            format!("input-ipc-server={}", socket.display()),
        ];
        options.extend(interpolation_options(
            settings.video_interpolation,
            refresh_millihz,
        ));
        let mut command = Command::new(&settings.mpvpaper_program);
        command.arg("--layer").arg(&settings.layer);
        match settings.video_when_hidden {
            VideoWhenHidden::Pause => {
                command.arg("--auto-pause");
            }
            VideoWhenHidden::Stop => {
                command.arg("--auto-stop");
            }
            VideoWhenHidden::Play => {}
        }
        command
            .arg("-o")
            .arg(options.join(" "))
            .arg(if output.is_empty() { "ALL" } else { output })
            .arg(video);
        command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .process_group(0);
        let mut child = command
            .spawn()
            .map_err(|error| format!("cannot start mpvpaper: {error}"))?;
        self.diagnostics = child
            .stderr
            .take()
            .map(|stderr| BoundedCapture::start(stderr, MAX_DIAGNOSTIC_BYTES));
        self.child = Some(child);
        self.socket = Some(socket);
        options.clear();
        Ok(())
    }

    fn stop(&mut self) {
        if let Some(mut child) = self.child.take() {
            stop_group(&mut child);
        }
        if let Some(diagnostics) = self.diagnostics.take() {
            let _ = diagnostics.finish();
        }
        if let Some(socket) = self.socket.take() {
            let _ = fs::remove_file(socket);
        }
    }

    fn set_paused(&mut self, paused: bool) -> bool {
        self.ipc(json!(["set_property", "pause", paused]))
    }
    fn set_volume(&mut self, muted: bool, volume: u8) -> bool {
        let volume_ok = self.ipc(json!(["set_property", "volume", volume]));
        let mute_ok = self.ipc(json!(["set_property", "mute", muted]));
        volume_ok && mute_ok
    }
}

pub struct SystemDriver {
    settings: RendererSettings,
    videos: HashMap<String, ActiveVideo>,
    scenes: HashMap<String, ActiveScene>,
    failed_scenes: HashSet<String>,
    applying_batch: bool,
    output_snapshot: Option<Result<Vec<LiveOutput>, String>>,
}

struct ActiveVideo {
    renderer: Mpvpaper,
    entry: Entry,
    output: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct LiveOutput {
    name: String,
    refresh_millihz: Option<u64>,
}

fn interpolation_options(mode: VideoInterpolation, refresh_millihz: Option<u64>) -> Vec<String> {
    let (Some(tscale), Some(refresh)) = (mode.tscale(), refresh_millihz) else {
        return Vec::new();
    };
    vec![
        "video-sync=display-resample".into(),
        "interpolation=yes".into(),
        format!("display-fps-override={:.3}", refresh as f64 / 1000.0),
        format!("tscale={tscale}"),
    ]
}

fn query_outputs(settings: &RendererSettings) -> Result<Vec<LiveOutput>, String> {
    let mut command = Command::new(&settings.niri_program);
    command.args(["msg", "--json", "outputs"]);
    let reply = run_bounded(
        &mut command,
        "niri outputs",
        HELPER_TIMEOUT,
        MAX_OUTPUT_REPLY_BYTES,
    )?;
    if !reply.status.success() {
        return Err(with_diagnostic(
            format!("niri outputs exited with {}", reply.status),
            &reply.stderr,
        ));
    }
    if reply.stdout.truncated {
        return Err("niri outputs reply exceeded 1 MiB".into());
    }
    let document: serde_json::Value =
        serde_json::from_slice(&reply.stdout.bytes).map_err(|error| {
            with_diagnostic(
                format!("niri outputs returned invalid JSON: {error}"),
                &reply.stderr,
            )
        })?;
    let object = document
        .as_object()
        .ok_or("niri outputs did not return an object")?;
    let mut outputs = Vec::new();
    for (key, value) in object.iter().take(MAX_OUTPUTS) {
        let entry = value.as_object();
        let candidate = entry
            .and_then(|entry| entry.get("name"))
            .and_then(serde_json::Value::as_str)
            .unwrap_or(key)
            .trim();
        if candidate.is_empty()
            || candidate.chars().any(char::is_control)
            || outputs
                .iter()
                .any(|known: &LiveOutput| known.name == candidate)
        {
            continue;
        }
        let refresh_millihz = entry
            .and_then(|entry| {
                let current = entry.get("current_mode")?.as_u64()? as usize;
                entry
                    .get("modes")?
                    .as_array()?
                    .get(current)?
                    .get("refresh_rate")?
                    .as_u64()
            })
            .filter(|refresh| *refresh > 0 && *refresh <= MAX_REFRESH_MILLIHZ);
        outputs.push(LiveOutput {
            name: candidate.to_string(),
            refresh_millihz,
        });
    }
    outputs.sort_by(|left, right| left.name.cmp(&right.name));
    if outputs.is_empty() {
        Err(with_diagnostic(
            "niri reported no usable outputs; paired still remains applied".into(),
            &reply.stderr,
        ))
    } else {
        Ok(outputs)
    }
}

fn unambiguous_refresh_millihz(outputs: &[LiveOutput], output: &str) -> Option<u64> {
    let candidates: Vec<&LiveOutput> = outputs
        .iter()
        .filter(|candidate| output.is_empty() || candidate.name == output)
        .collect();
    let first = candidates
        .first()
        .and_then(|candidate| candidate.refresh_millihz)?;
    if candidates
        .iter()
        .all(|candidate| candidate.refresh_millihz == Some(first))
    {
        Some(first)
    } else {
        None
    }
}

struct ActiveScene {
    child: Child,
    entry: Entry,
    output: String,
    diagnostics: Option<BoundedCapture>,
}

impl SystemDriver {
    pub fn new(settings: RendererSettings) -> Self {
        Self {
            settings,
            videos: HashMap::new(),
            scenes: HashMap::new(),
            failed_scenes: HashSet::new(),
            applying_batch: false,
            output_snapshot: None,
        }
    }

    fn key(output: &str) -> String {
        if output.is_empty() {
            "ALL".into()
        } else {
            output.into()
        }
    }

    fn stop_output(&mut self, output: &str) {
        let key = Self::key(output);
        if let Some(mut video) = self.videos.remove(&key) {
            video.renderer.stop();
        }
        if let Some(mut scene) = self.scenes.remove(&key) {
            stop_group(&mut scene.child);
        }
    }

    fn noctalia(&self, arguments: &[&str]) -> Result<(), String> {
        let mut command = Command::new(&self.settings.noctalia_program);
        command.args(arguments);
        let result = run_bounded(&mut command, "noctalia", HELPER_TIMEOUT, 0)?;
        if result.status.success() {
            Ok(())
        } else {
            Err(with_diagnostic(
                format!("noctalia exited with {}", result.status),
                &result.stderr,
            ))
        }
    }

    fn still(&self, entry: &Entry, output: &str) -> Result<(), String> {
        let path = entry.still.to_str().ok_or("still path is not UTF-8")?;
        if output.is_empty() {
            self.noctalia(&["msg", "wallpaper-set", path])
        } else {
            self.noctalia(&["msg", "wallpaper-set", output, path])
        }
    }

    fn palette(&self, palette: &Palette) -> Result<(), String> {
        let mode = match palette.mode() {
            ThemeMode::Keep => None,
            ThemeMode::Dark => Some("dark"),
            ThemeMode::Light => Some("light"),
            ThemeMode::Auto => Some("auto"),
        };
        if let Some(mode) = mode {
            self.noctalia(&["msg", "theme-mode-set", mode])?;
        }
        match palette {
            Palette::Keep { .. } => Ok(()),
            Palette::Adaptive { scheme, .. } => {
                self.noctalia(&["msg", "color-scheme-set", "wallpaper", scheme])
            }
            Palette::Named { source, name, .. } => {
                let source = match source {
                    PaletteSource::Builtin => "builtin",
                    PaletteSource::Community => "community",
                    PaletteSource::Custom => "custom",
                };
                self.noctalia(&["msg", "color-scheme-set", source, name])
            }
        }
    }

    fn live_outputs(&mut self) -> Result<Vec<LiveOutput>, String> {
        if !self.applying_batch {
            return query_outputs(&self.settings);
        }
        if self.output_snapshot.is_none() {
            self.output_snapshot = Some(query_outputs(&self.settings));
        }
        self.output_snapshot
            .as_ref()
            .expect("output snapshot was just installed")
            .clone()
    }

    fn interpolation_refresh(&mut self, output: &str) -> Option<u64> {
        if self.settings.video_interpolation == VideoInterpolation::Off {
            return None;
        }
        match self.live_outputs() {
            Ok(outputs) => {
                let refresh = unambiguous_refresh_millihz(&outputs, output);
                if refresh.is_none() {
                    eprintln!(
                        "video interpolation disabled: active output refresh is missing or mixed"
                    );
                }
                refresh
            }
            Err(error) => {
                eprintln!("video interpolation disabled: {error}");
                None
            }
        }
    }

    fn current_outputs(&mut self) -> Result<Vec<String>, String> {
        self.live_outputs()
            .map(|outputs| outputs.into_iter().map(|output| output.name).collect())
    }

    fn add_scene_target(&self, command: &mut Command, output: &str, scene: &str) {
        command.arg("--screen-root").arg(output);
        if !self.settings.scene_scaling.is_empty() {
            command.arg("--scaling").arg(&self.settings.scene_scaling);
        }
        if !self.settings.scene_clamp.is_empty() {
            command.arg("--clamp").arg(&self.settings.scene_clamp);
        }
        command.arg("--bg").arg(scene);
    }

    fn start_scene(&mut self, entry: &Entry, output: &str) -> Result<(), String> {
        if !self.settings.own_scene_renderer {
            return Err(
                "scene renderer ownership is disabled; paired still remains applied".into(),
            );
        }
        let scene = entry
            .scene_id
            .as_ref()
            .ok_or("scene entry has no scene id")?;
        if self.failed_scenes.contains(scene) {
            return Err(format!(
                "scene {scene} (entry {:?}) previously crashed linux-wallpaperengine this session; paired still remains applied",
                entry.id
            ));
        }
        let mut command = Command::new(&self.settings.linux_wallpaperengine_program);
        command
            .arg("--layer")
            .arg(&self.settings.layer)
            .arg("--fps")
            .arg(self.settings.scene_fps.to_string());
        if self.settings.scene_muted {
            command.arg("--silent");
        } else {
            command
                .arg("--volume")
                .arg(self.settings.scene_volume.to_string());
        }
        if !self.settings.scene_pause_when_covered {
            command.arg("--no-fullscreen-pause");
        }
        if output.is_empty() {
            for connector in self.current_outputs()? {
                self.add_scene_target(&mut command, &connector, scene);
            }
        } else {
            self.add_scene_target(&mut command, output, scene);
        }
        command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .process_group(0);
        let mut child = command
            .spawn()
            .map_err(|error| format!("cannot start linux-wallpaperengine: {error}"))?;
        let diagnostics = child
            .stderr
            .take()
            .map(|stderr| BoundedCapture::start(stderr, MAX_DIAGNOSTIC_BYTES));
        self.scenes.insert(
            Self::key(output),
            ActiveScene {
                child,
                entry: entry.clone(),
                output: output.to_string(),
                diagnostics,
            },
        );
        Ok(())
    }

    fn failure_message(
        renderer: &str,
        entry: &Entry,
        output: &str,
        status: &str,
        diagnostics: &str,
        fallback: Result<(), String>,
    ) -> String {
        let target = if output.is_empty() { "ALL" } else { output };
        let identity = if entry.kind == EntryKind::Scene {
            format!(
                "scene {} (entry {:?})",
                entry.scene_id.as_deref().unwrap_or("unknown"),
                entry.id
            )
        } else {
            format!("video entry {:?}", entry.id)
        };
        let mut message = format!(
            "{identity} stopped because {renderer} exited on {target} ({status}); paired still is active"
        );
        if !diagnostics.is_empty() {
            message.push_str(&format!("; stderr: {diagnostics}"));
        }
        if let Err(error) = fallback {
            message.push_str(&format!("; could not reapply the still: {error}"));
        }
        message
    }
}

impl WallpaperDriver for SystemDriver {
    fn begin_apply(&mut self) {
        self.applying_batch = true;
        self.output_snapshot = None;
    }

    fn apply(
        &mut self,
        entry: &Entry,
        output: &str,
        runtime: &crate::config::Settings,
    ) -> Result<(), String> {
        // Break before make. A refused scene must expose its already-applied still.
        self.stop_output(output);
        self.still(entry, output)?;
        self.palette(&entry.palette)?;
        if !runtime.dynamics_enabled {
            return Ok(());
        }
        match entry.kind {
            EntryKind::Still => Ok(()),
            EntryKind::Video => {
                let mut video = Mpvpaper::new();
                let refresh = self.interpolation_refresh(output);
                video.start(entry, output, &self.settings, refresh)?;
                self.videos.insert(
                    Self::key(output),
                    ActiveVideo {
                        renderer: video,
                        entry: entry.clone(),
                        output: output.to_string(),
                    },
                );
                Ok(())
            }
            EntryKind::Scene => self.start_scene(entry, output),
        }
    }

    fn set_paused(&mut self, paused: bool) {
        for video in self.videos.values_mut() {
            let _ = video.renderer.set_paused(paused);
        }
        for scene in self.scenes.values() {
            unsafe {
                libc::kill(
                    -(scene.child.id() as i32),
                    if paused { libc::SIGSTOP } else { libc::SIGCONT },
                );
            }
        }
    }

    fn end_apply(&mut self) {
        self.applying_batch = false;
        self.output_snapshot = None;
    }

    fn reconfigure(&mut self, settings: RendererSettings) {
        self.stop();
        self.settings = settings;
    }

    fn poll_failures(&mut self) -> Vec<String> {
        let mut failures = Vec::new();
        let mut exited_videos = Vec::new();
        for (key, active) in &mut self.videos {
            match active.renderer.take_exit() {
                Ok(Some((status, diagnostics))) => {
                    exited_videos.push((key.clone(), status.to_string(), diagnostics));
                }
                Ok(None) => {}
                Err(error) => exited_videos.push((key.clone(), error, String::new())),
            }
        }
        for (key, status, diagnostics) in exited_videos {
            if let Some(active) = self.videos.remove(&key) {
                let fallback = self.still(&active.entry, &active.output);
                failures.push(Self::failure_message(
                    "mpvpaper",
                    &active.entry,
                    &active.output,
                    &status,
                    &diagnostics,
                    fallback,
                ));
            }
        }

        let mut exited_scenes = Vec::new();
        for (key, active) in &mut self.scenes {
            match active.child.try_wait() {
                Ok(Some(status)) => exited_scenes.push((key.clone(), status.to_string())),
                Ok(None) => {}
                Err(error) => exited_scenes.push((
                    key.clone(),
                    format!("cannot inspect linux-wallpaperengine: {error}"),
                )),
            }
        }
        for (key, status) in exited_scenes {
            if let Some(mut active) = self.scenes.remove(&key) {
                if let Some(scene) = &active.entry.scene_id {
                    self.failed_scenes.insert(scene.clone());
                }
                let diagnostics = active
                    .diagnostics
                    .take()
                    .map(|capture| diagnostic(&capture.finish()))
                    .unwrap_or_default();
                let fallback = self.still(&active.entry, &active.output);
                failures.push(Self::failure_message(
                    "linux-wallpaperengine",
                    &active.entry,
                    &active.output,
                    &status,
                    &diagnostics,
                    fallback,
                ));
            }
        }
        failures
    }

    fn motion_active(&self, output: &str) -> bool {
        let key = Self::key(output);
        self.videos.contains_key(&key) || self.scenes.contains_key(&key)
    }

    fn stop(&mut self) {
        for (_, mut video) in self.videos.drain() {
            video.renderer.stop();
        }
        for (_, mut scene) in self.scenes.drain() {
            stop_group(&mut scene.child);
            if let Some(diagnostics) = scene.diagnostics.take() {
                let _ = diagnostics.finish();
            }
        }
    }
}

impl Drop for SystemDriver {
    fn drop(&mut self) {
        self.stop();
    }
}

fn stop_group(child: &mut Child) {
    stop_group_with_grace(child, Duration::from_secs(3));
}

fn stop_group_with_grace(child: &mut Child, grace: Duration) {
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGTERM);
    }
    let deadline = Instant::now() + grace;
    while Instant::now() < deadline {
        if matches!(child.try_wait(), Ok(Some(_))) {
            return;
        }
        thread::sleep(Duration::from_millis(20));
    }
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
    let _ = child.wait();
}

#[allow(dead_code)]
fn _absolute(path: &Path) -> bool {
    path.is_absolute()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn interpolation_options_are_complete_or_absent() {
        assert!(interpolation_options(VideoInterpolation::Off, Some(165_004)).is_empty());
        assert!(interpolation_options(VideoInterpolation::Oversample, None).is_empty());
        assert_eq!(
            interpolation_options(VideoInterpolation::Oversample, Some(165_004)),
            [
                "video-sync=display-resample",
                "interpolation=yes",
                "display-fps-override=165.004",
                "tscale=oversample",
            ]
        );
        assert_eq!(
            interpolation_options(VideoInterpolation::Linear, Some(60_000))[3],
            "tscale=linear"
        );
    }

    #[test]
    fn refresh_must_be_known_and_uniform_across_the_target() {
        let uniform = vec![
            LiveOutput {
                name: "DP-1".into(),
                refresh_millihz: Some(60_000),
            },
            LiveOutput {
                name: "DP-2".into(),
                refresh_millihz: Some(60_000),
            },
        ];
        assert_eq!(unambiguous_refresh_millihz(&uniform, ""), Some(60_000));
        assert_eq!(unambiguous_refresh_millihz(&uniform, "DP-2"), Some(60_000));

        let mixed = vec![
            uniform[0].clone(),
            LiveOutput {
                name: "DP-2".into(),
                refresh_millihz: Some(165_004),
            },
        ];
        let partial = vec![
            uniform[0].clone(),
            LiveOutput {
                name: "DP-2".into(),
                refresh_millihz: None,
            },
        ];
        assert_eq!(unambiguous_refresh_millihz(&mixed, ""), None);
        assert_eq!(unambiguous_refresh_millihz(&partial, ""), None);
        assert_eq!(unambiguous_refresh_millihz(&uniform, "HDMI-A-1"), None);
    }

    #[test]
    fn bounded_helper_times_out_kills_its_group_and_keeps_diagnostics() {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let marker = std::env::temp_dir().join(format!(
            "wall-in-one-helper-descendant-{}-{nonce}",
            std::process::id()
        ));
        let mut command = Command::new("/bin/sh");
        command.arg("-c").arg(format!(
            "printf '%s\\n' 'helper never became ready' >&2; (sleep 0.4; touch {}) & wait",
            marker.display()
        ));
        let started = Instant::now();
        let error = match run_bounded(&mut command, "test helper", Duration::from_millis(100), 0) {
            Ok(_) => panic!("hanging helper unexpectedly succeeded"),
            Err(error) => error,
        };
        assert!(started.elapsed() < Duration::from_secs(2));
        assert!(error.contains("timed out after 100 ms"), "{error}");
        assert!(error.contains("helper never became ready"), "{error}");
        thread::sleep(Duration::from_millis(600));
        assert!(
            !marker.exists(),
            "a descendant survived the timed-out helper's process-group termination"
        );
    }

    #[test]
    fn helper_diagnostics_are_bounded_and_marked_when_truncated() {
        let mut command = Command::new("/bin/sh");
        command.args(["-c", "head -c 20000 /dev/zero | tr '\\0' x >&2; exit 9"]);
        let output = run_bounded(&mut command, "test helper", Duration::from_secs(1), 0).unwrap();
        assert!(!output.status.success());
        let rendered = diagnostic(&output.stderr);
        assert!(rendered.len() <= MAX_DIAGNOSTIC_BYTES + 32);
        assert!(rendered.ends_with("[truncated]"));
    }
}
