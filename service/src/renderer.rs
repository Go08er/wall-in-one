use crate::config::{
    Entry, EntryKind, Palette, PaletteSource, RendererSettings, ThemeMode, VideoWhenHidden,
};
use serde_json::json;
use std::collections::HashMap;
use std::fs;
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

pub trait VideoRenderer: Send {
    fn start(
        &mut self,
        entry: &Entry,
        output: &str,
        settings: &RendererSettings,
    ) -> Result<(), String>;
    fn stop(&mut self);
    fn set_paused(&mut self, paused: bool) -> bool;
    fn set_volume(&mut self, muted: bool, volume: u8) -> bool;
}

pub trait WallpaperDriver: Send {
    fn apply(
        &mut self,
        entry: &Entry,
        output: &str,
        settings: &crate::config::Settings,
    ) -> Result<(), String>;
    fn set_paused(&mut self, paused: bool);
    fn reconfigure(&mut self, settings: RendererSettings);
    fn stop(&mut self);
}

pub struct Mpvpaper {
    child: Option<Child>,
    socket: Option<PathBuf>,
}

impl Mpvpaper {
    pub fn new() -> Self {
        Self {
            child: None,
            socket: None,
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
            .stderr(Stdio::null())
            .process_group(0);
        self.child = Some(
            command
                .spawn()
                .map_err(|error| format!("cannot start mpvpaper: {error}"))?,
        );
        self.socket = Some(socket);
        options.clear();
        Ok(())
    }

    fn stop(&mut self) {
        if let Some(mut child) = self.child.take() {
            stop_group(&mut child);
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
    videos: HashMap<String, Mpvpaper>,
    scenes: HashMap<String, Child>,
}

impl SystemDriver {
    pub fn new(settings: RendererSettings) -> Self {
        Self {
            settings,
            videos: HashMap::new(),
            scenes: HashMap::new(),
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
            video.stop();
        }
        if let Some(mut child) = self.scenes.remove(&key) {
            stop_group(&mut child);
        }
    }

    fn noctalia(&self, arguments: &[&str]) -> Result<(), String> {
        let result = Command::new(&self.settings.noctalia_program)
            .args(arguments)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map_err(|error| format!("cannot run noctalia: {error}"))?;
        if result.success() {
            Ok(())
        } else {
            Err(format!("noctalia exited with {result}"))
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
        if !output.is_empty() {
            command.arg("--screen-root").arg(output);
            if !self.settings.scene_scaling.is_empty() {
                command.arg("--scaling").arg(&self.settings.scene_scaling);
            }
            if !self.settings.scene_clamp.is_empty() {
                command.arg("--clamp").arg(&self.settings.scene_clamp);
            }
            command.arg("--bg").arg(scene);
        } else {
            command.arg(scene);
        }
        command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0);
        let child = command
            .spawn()
            .map_err(|error| format!("cannot start linux-wallpaperengine: {error}"))?;
        self.scenes.insert(Self::key(output), child);
        Ok(())
    }
}

impl WallpaperDriver for SystemDriver {
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
                video.start(entry, output, &self.settings)?;
                self.videos.insert(Self::key(output), video);
                Ok(())
            }
            EntryKind::Scene => self.start_scene(entry, output),
        }
    }

    fn set_paused(&mut self, paused: bool) {
        for video in self.videos.values_mut() {
            let _ = video.set_paused(paused);
        }
        for child in self.scenes.values() {
            unsafe {
                libc::kill(
                    -(child.id() as i32),
                    if paused { libc::SIGSTOP } else { libc::SIGCONT },
                );
            }
        }
    }

    fn reconfigure(&mut self, settings: RendererSettings) {
        self.stop();
        self.settings = settings;
    }

    fn stop(&mut self) {
        for (_, mut video) in self.videos.drain() {
            video.stop();
        }
        for (_, mut child) in self.scenes.drain() {
            stop_group(&mut child);
        }
    }
}

impl Drop for SystemDriver {
    fn drop(&mut self) {
        self.stop();
    }
}

fn stop_group(child: &mut Child) {
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGTERM);
    }
    let deadline = Instant::now() + Duration::from_secs(3);
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
