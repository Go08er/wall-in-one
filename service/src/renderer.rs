use crate::config::{
    Entry, EntryKind, Palette, PaletteSource, RendererSettings, ThemeMode, VideoInterpolation,
    VideoWhenHidden,
};
use serde_json::json;
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const MAX_OUTPUTS: usize = 64;
pub const MAX_OUTPUT_NAME_BYTES: usize = 256;
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
    fn set_paused(&mut self, paused: bool) -> Result<(), String>;
    fn set_volume(&mut self, muted: bool, volume: u8) -> bool;
}

pub trait WallpaperDriver: Send {
    fn begin_apply(&mut self) {}
    /// Return the compositor's currently connected output names.
    ///
    /// A batch-aware driver must reuse this same snapshot for any renderer
    /// decisions made before `end_apply`, so one wallpaper hand-over performs
    /// at most one compositor query.
    fn connected_outputs(&mut self) -> Result<Vec<String>, String> {
        Err("live output discovery is unavailable".into())
    }
    fn apply(
        &mut self,
        entry: &Entry,
        output: &str,
        settings: &crate::config::Settings,
    ) -> Result<(), String>;
    /// Stop only the renderer owned by one connector before a staged hand-over.
    fn stop_output_renderer(&mut self, _output: &str) {}
    /// Apply the paired still without launching motion or changing colours.
    fn apply_still_only(
        &mut self,
        entry: &Entry,
        output: &str,
        settings: &crate::config::Settings,
    ) -> Result<(), String> {
        let mut still_only = settings.clone();
        still_only.dynamics_enabled = false;
        self.apply(entry, output, &still_only)
    }
    /// Apply the already-resolved global palette decision for one entry.
    fn apply_palette_only(&mut self, _entry: &Entry) -> Result<(), String> {
        Ok(())
    }
    /// Launch only the motion renderer after every output still is in place.
    fn start_motion_only(
        &mut self,
        _entry: &Entry,
        _output: &str,
        _settings: &crate::config::Settings,
    ) -> Result<(), String> {
        Ok(())
    }
    /// Stop renderers which no longer have an entry in the effective target set.
    ///
    /// Applying one output cannot discover that another output disappeared, so
    /// the runtime supplies the complete set once per batch.  The default keeps
    /// lightweight test drivers source-compatible while real process owners
    /// override it.
    fn retain_outputs(&mut self, _outputs: &[String]) {}
    fn set_paused(&mut self, paused: bool) -> Result<(), String>;
    fn set_output_paused(&mut self, _output: &str, paused: bool) -> Result<(), String> {
        self.set_paused(paused)
    }
    /// Retune video audio without restarting the renderer child.
    ///
    /// The default keeps lightweight/test drivers source-compatible. Process
    /// owners override it and update both active children and launch defaults.
    fn set_video_audio(&mut self, _muted: bool, _volume: u8) -> Result<(), String> {
        Ok(())
    }
    fn end_apply(&mut self) {}
    fn reconfigure(&mut self, settings: RendererSettings);
    fn poll_failures(&mut self) -> Vec<RendererFailure> {
        Vec::new()
    }
    fn motion_active(&self, _output: &str) -> bool {
        false
    }
    fn stop(&mut self);
}

/// An owned renderer-exit report. Runtime policy needs the entry identity,
/// not a diagnostic string it would have to parse, because a scene crash is a
/// session-scoped incompatibility while a video exit remains retryable.
#[derive(Clone, Debug)]
pub struct RendererFailure {
    pub entry_id: String,
    pub kind: EntryKind,
    pub scene_id: Option<String>,
    pub output: String,
    pub message: String,
    pub permanent_for_session: bool,
}

pub struct Mpvpaper {
    child: Option<Child>,
    socket: Option<PathBuf>,
    diagnostics: Option<BoundedCapture>,
    pause_transport: PauseTransport,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
enum PauseTransport {
    #[default]
    None,
    Ipc,
    Signal,
}

impl Mpvpaper {
    pub fn new() -> Self {
        Self {
            child: None,
            socket: None,
            diagnostics: None,
            pause_transport: PauseTransport::None,
        }
    }

    fn ipc(&self, command: serde_json::Value) -> Result<(), String> {
        let Some(path) = &self.socket else {
            return Err("mpvpaper IPC socket is not configured".into());
        };
        let mut stream = UnixStream::connect(path).map_err(|error| {
            format!("cannot connect to mpvpaper IPC {}: {error}", path.display())
        })?;
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .map_err(|error| format!("cannot configure mpvpaper IPC timeout: {error}"))?;
        let mut encoded = serde_json::to_vec(&json!({"command": command}))
            .map_err(|error| format!("cannot encode mpvpaper IPC request: {error}"))?;
        encoded.push(b'\n');
        stream
            .write_all(&encoded)
            .map_err(|error| format!("cannot write mpvpaper IPC request: {error}"))?;
        let mut reply = String::new();
        BufReader::new(stream)
            .take(4097)
            .read_line(&mut reply)
            .map_err(|error| format!("cannot read mpvpaper IPC response: {error}"))?;
        if reply.len() > 4096 {
            return Err("mpvpaper IPC response exceeded 4 KiB".into());
        }
        let document: serde_json::Value = serde_json::from_str(reply.trim())
            .map_err(|error| format!("mpvpaper IPC returned invalid JSON: {error}"))?;
        match document.get("error").and_then(serde_json::Value::as_str) {
            Some("success") => Ok(()),
            Some(error) => Err(format!("mpvpaper IPC refused the command: {error}")),
            None => Err("mpvpaper IPC response has no error field".into()),
        }
    }

    fn signal(&mut self, signal: i32) -> Result<(), String> {
        let child = self.child.as_ref().ok_or("mpvpaper has no running child")?;
        let result = unsafe { libc::kill(-(child.id() as i32), signal) };
        if result == 0 {
            Ok(())
        } else {
            Err(format!(
                "cannot signal mpvpaper process group: {}",
                std::io::Error::last_os_error()
            ))
        }
    }

    fn take_exit(&mut self) -> Result<Option<(ExitStatus, String)>, String> {
        let Some(child) = self.child.as_mut() else {
            return Ok(None);
        };
        match child.try_wait() {
            Ok(Some(status)) => {
                self.child.take();
                self.pause_transport = PauseTransport::None;
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
            Err(error) => Err(self.inspection_failure(error)),
        }
    }

    fn inspection_failure(&mut self, error: std::io::Error) -> String {
        let diagnostic = format!("cannot inspect mpvpaper: {error}");
        // Child::drop does not terminate the process. An inspection failure
        // therefore cannot be handled by merely removing the ActiveVideo:
        // explicitly stop/reap the group before status falls back to still.
        self.stop();
        diagnostic
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
        let safe_output = output_socket_token(output);
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
        self.pause_transport = PauseTransport::None;
        options.clear();
        Ok(())
    }

    fn stop(&mut self) {
        if let Some(mut child) = self.child.take() {
            if self.pause_transport == PauseTransport::Signal {
                unsafe {
                    libc::kill(-(child.id() as i32), libc::SIGCONT);
                }
            }
            stop_group(&mut child);
        }
        self.pause_transport = PauseTransport::None;
        if let Some(diagnostics) = self.diagnostics.take() {
            let _ = diagnostics.finish();
        }
        if let Some(socket) = self.socket.take() {
            let _ = fs::remove_file(socket);
        }
    }

    fn set_paused(&mut self, paused: bool) -> Result<(), String> {
        if paused {
            if self.pause_transport != PauseTransport::None {
                return Ok(());
            }
            match self.ipc(json!(["set_property", "pause", true])) {
                Ok(()) => {
                    self.pause_transport = PauseTransport::Ipc;
                    Ok(())
                }
                Err(ipc_error) => {
                    // mpvpaper creates its IPC socket asynchronously.  A process
                    // signal is an honest pause/resume fallback during that window,
                    // and retains the documented resident-process semantics.
                    self.signal(libc::SIGSTOP).map_err(|signal_error| {
                        format!("{ipc_error}; signal fallback failed: {signal_error}")
                    })?;
                    self.pause_transport = PauseTransport::Signal;
                    Ok(())
                }
            }
        } else {
            match self.pause_transport {
                PauseTransport::None => Ok(()),
                PauseTransport::Signal => {
                    self.signal(libc::SIGCONT)?;
                    self.pause_transport = PauseTransport::None;
                    Ok(())
                }
                PauseTransport::Ipc => {
                    // SIGCONT cannot undo mpv's own `pause` property. If IPC
                    // disappears after an IPC pause, fail instead of claiming
                    // playback resumed while frames remain frozen.
                    self.ipc(json!(["set_property", "pause", false]))?;
                    self.pause_transport = PauseTransport::None;
                    Ok(())
                }
            }
        }
    }
    fn set_volume(&mut self, muted: bool, volume: u8) -> bool {
        let volume_ok = self.ipc(json!(["set_property", "volume", volume])).is_ok();
        let mute_ok = self.ipc(json!(["set_property", "mute", muted])).is_ok();
        volume_ok && mute_ok
    }
}

pub struct SystemDriver {
    settings: RendererSettings,
    videos: HashMap<String, ActiveVideo>,
    scenes: HashMap<String, ActiveScene>,
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
    if object.len() > MAX_OUTPUTS {
        return Err(format!(
            "niri reported more than the supported {MAX_OUTPUTS} outputs"
        ));
    }
    let mut outputs = Vec::new();
    for (key, value) in object {
        let entry = value.as_object();
        let candidate = entry
            .and_then(|entry| entry.get("name"))
            .and_then(serde_json::Value::as_str)
            .unwrap_or(key)
            .trim();
        if !usable_output_name(candidate)
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

fn usable_output_name(candidate: &str) -> bool {
    !candidate.is_empty()
        && candidate.len() <= MAX_OUTPUT_NAME_BYTES
        && !candidate.chars().any(char::is_whitespace)
        && !candidate.chars().any(char::is_control)
}

fn output_socket_token(output: &str) -> String {
    // Connector punctuation is not unique after sanitising (`DP-1` and
    // `DP_1` both become `DP_1`). Keep a readable bounded prefix, then add a
    // deterministic hash so two live outputs can never share mpv's IPC path.
    let label: String = if output.is_empty() {
        "ALL".into()
    } else {
        output
            .chars()
            .map(|character| {
                if character.is_ascii_alphanumeric() {
                    character
                } else {
                    '_'
                }
            })
            .take(32)
            .collect()
    };
    let hash = output
        .as_bytes()
        .iter()
        .fold(0xcbf29ce484222325_u64, |hash, byte| {
            (hash ^ u64::from(*byte)).wrapping_mul(0x100000001b3)
        });
    format!("{label}-{hash:016x}")
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

    fn start_motion(
        &mut self,
        entry: &Entry,
        output: &str,
        runtime: &crate::config::Settings,
    ) -> Result<(), String> {
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

    fn connected_outputs(&mut self) -> Result<Vec<String>, String> {
        self.current_outputs()
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
        self.start_motion(entry, output, runtime)
    }

    fn stop_output_renderer(&mut self, output: &str) {
        self.stop_output(output);
    }

    fn apply_still_only(
        &mut self,
        entry: &Entry,
        output: &str,
        _settings: &crate::config::Settings,
    ) -> Result<(), String> {
        self.still(entry, output)
    }

    fn apply_palette_only(&mut self, entry: &Entry) -> Result<(), String> {
        self.palette(&entry.palette)
    }

    fn start_motion_only(
        &mut self,
        entry: &Entry,
        output: &str,
        settings: &crate::config::Settings,
    ) -> Result<(), String> {
        self.start_motion(entry, output, settings)
    }

    fn retain_outputs(&mut self, outputs: &[String]) {
        let wanted: HashSet<String> = outputs.iter().map(|output| Self::key(output)).collect();
        let stale: HashSet<String> = self
            .videos
            .keys()
            .chain(self.scenes.keys())
            .filter(|key| !wanted.contains(*key))
            .cloned()
            .collect();
        for key in stale {
            let output = self
                .videos
                .get(&key)
                .map(|active| active.output.clone())
                .or_else(|| self.scenes.get(&key).map(|active| active.output.clone()))
                .unwrap_or(key);
            self.stop_output(&output);
        }
    }

    fn set_paused(&mut self, paused: bool) -> Result<(), String> {
        let mut errors = Vec::new();
        for (key, video) in &mut self.videos {
            if let Err(error) = video.renderer.set_paused(paused) {
                errors.push(format!("{key}: {error}"));
            }
        }
        for (key, scene) in &self.scenes {
            let result = unsafe {
                libc::kill(
                    -(scene.child.id() as i32),
                    if paused { libc::SIGSTOP } else { libc::SIGCONT },
                )
            };
            if result != 0 {
                errors.push(format!(
                    "{key}: cannot signal linux-wallpaperengine process group: {}",
                    std::io::Error::last_os_error()
                ));
            }
        }
        if errors.is_empty() {
            Ok(())
        } else {
            Err(errors.join("; "))
        }
    }

    fn set_output_paused(&mut self, output: &str, paused: bool) -> Result<(), String> {
        let key = Self::key(output);
        let mut errors = Vec::new();
        if let Some(video) = self.videos.get_mut(&key) {
            if let Err(error) = video.renderer.set_paused(paused) {
                errors.push(error);
            }
        }
        if let Some(scene) = self.scenes.get(&key) {
            let result = unsafe {
                libc::kill(
                    -(scene.child.id() as i32),
                    if paused { libc::SIGSTOP } else { libc::SIGCONT },
                )
            };
            if result != 0 {
                errors.push(format!(
                    "cannot signal linux-wallpaperengine process group: {}",
                    std::io::Error::last_os_error()
                ));
            }
        }
        if errors.is_empty() {
            Ok(())
        } else {
            Err(format!("{key}: {}", errors.join("; ")))
        }
    }

    fn set_video_audio(&mut self, muted: bool, volume: u8) -> Result<(), String> {
        // Adopt the launch defaults even if one current child refuses IPC: a
        // later wallpaper must not resurrect the old setting.
        self.settings.video_muted = muted;
        self.settings.video_volume = volume;
        let failed: Vec<_> = self
            .videos
            .iter_mut()
            .filter_map(|(output, video)| {
                (!video.renderer.set_volume(muted, volume)).then(|| output.clone())
            })
            .collect();
        if failed.is_empty() {
            Ok(())
        } else {
            Err(format!(
                "could not update video audio on {}",
                failed.join(", ")
            ))
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

    fn poll_failures(&mut self) -> Vec<RendererFailure> {
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
                let message = Self::failure_message(
                    "mpvpaper",
                    &active.entry,
                    &active.output,
                    &status,
                    &diagnostics,
                    fallback,
                );
                failures.push(RendererFailure {
                    entry_id: active.entry.id.clone(),
                    kind: active.entry.kind,
                    scene_id: active.entry.scene_id.clone(),
                    output: active.output.clone(),
                    message,
                    permanent_for_session: false,
                });
            }
        }

        let mut exited_scenes = Vec::new();
        for (key, active) in &mut self.scenes {
            match active.child.try_wait() {
                Ok(Some(status)) => exited_scenes.push((key.clone(), status.to_string(), false)),
                Ok(None) => {}
                Err(error) => exited_scenes.push((
                    key.clone(),
                    format!("cannot inspect linux-wallpaperengine: {error}"),
                    true,
                )),
            }
        }
        for (key, status, must_stop) in exited_scenes {
            if let Some(mut active) = self.scenes.remove(&key) {
                if must_stop {
                    // Dropping Child would orphan a renderer that may still
                    // own the output. Match every other hand-over path and
                    // explicitly terminate/reap its process group.
                    stop_group(&mut active.child);
                }
                let diagnostics = active
                    .diagnostics
                    .take()
                    .map(|capture| diagnostic(&capture.finish()))
                    .unwrap_or_default();
                let fallback = self.still(&active.entry, &active.output);
                let message = Self::failure_message(
                    "linux-wallpaperengine",
                    &active.entry,
                    &active.output,
                    &status,
                    &diagnostics,
                    fallback,
                );
                failures.push(RendererFailure {
                    entry_id: active.entry.id.clone(),
                    kind: active.entry.kind,
                    scene_id: active.entry.scene_id.clone(),
                    output: active.output.clone(),
                    message,
                    permanent_for_session: true,
                });
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
        // A paused renderer cannot process SIGTERM until it is continued.
        libc::kill(-(child.id() as i32), libc::SIGCONT);
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
    use std::os::unix::net::UnixListener;

    #[test]
    fn mpvpaper_inspection_failure_reaps_the_live_child_group() {
        let child = Command::new("sleep")
            .arg("30")
            .process_group(0)
            .spawn()
            .unwrap();
        let pid = child.id() as i32;
        let mut renderer = Mpvpaper::new();
        renderer.child = Some(child);
        let diagnostic = renderer.inspection_failure(std::io::Error::other("forced EIO"));
        assert!(diagnostic.contains("forced EIO"));
        assert!(renderer.child.is_none());
        assert_eq!(unsafe { libc::kill(pid, 0) }, -1, "child was not reaped");
        assert_eq!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(libc::ESRCH)
        );
    }

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
    fn compositor_output_names_are_bounded_before_becoming_process_arguments() {
        assert!(usable_output_name("eDP-1"));
        assert!(usable_output_name(&"x".repeat(MAX_OUTPUT_NAME_BYTES)));
        assert!(!usable_output_name(&"x".repeat(MAX_OUTPUT_NAME_BYTES + 1)));
        assert!(!usable_output_name("Display Port-1"));
        assert!(!usable_output_name("DP-1\n--bg"));
    }

    #[test]
    fn connector_punctuation_cannot_collide_in_mpv_socket_names() {
        assert_ne!(output_socket_token("DP-1"), output_socket_token("DP_1"));
        assert_ne!(output_socket_token(""), output_socket_token("ALL"));
        assert!(output_socket_token(&"x".repeat(MAX_OUTPUT_NAME_BYTES)).len() <= 49);
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

    #[test]
    fn ipc_paused_video_does_not_claim_sigcont_resumed_mpv() {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "wall-in-one-mpv-ipc-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir_all(&root).unwrap();
        let socket = root.join("mpv.sock");
        let listener = UnixListener::bind(&socket).unwrap();
        let responder = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = String::new();
            BufReader::new(stream.try_clone().unwrap())
                .read_line(&mut request)
                .unwrap();
            assert!(request.contains("set_property"));
            stream.write_all(b"{\"error\":\"success\"}\n").unwrap();
        });

        let mut command = Command::new("/bin/sh");
        command
            .args(["-c", "sleep 30"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0);
        let mut renderer = Mpvpaper::new();
        renderer.child = Some(command.spawn().unwrap());
        renderer.socket = Some(socket.clone());

        renderer.set_paused(true).unwrap();
        responder.join().unwrap();
        fs::remove_file(&socket).unwrap();
        let error = renderer.set_paused(false).unwrap_err();
        assert!(error.contains("cannot connect to mpvpaper IPC"), "{error}");
        assert_eq!(renderer.pause_transport, PauseTransport::Ipc);

        renderer.stop();
        fs::remove_dir_all(root).unwrap();
    }
}
