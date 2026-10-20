use crate::config::{
    Entry, EntryKind, Palette, PaletteSource, RendererSettings, ThemeMode, VideoInterpolation,
    VideoWhenHidden,
};
use crate::outputs::{Output, OutputSource, WaylandOutputs};
use serde_json::json;
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{Read, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const MAX_OUTPUT_NAME_BYTES: usize = 256;
const MAX_DIAGNOSTIC_BYTES: usize = 4096;
const HELPER_TIMEOUT: Duration = Duration::from_secs(3);
const HELPER_STOP_GRACE: Duration = Duration::from_millis(250);
const PIPE_DRAIN_GRACE: Duration = Duration::from_millis(100);
const MAX_SOCKET_PATH_BYTES: usize = 100;
/// The longest one mpv IPC exchange (connect, request and reply together)
/// may take. The runtime has one thread; a renderer that stops answering
/// must not take supervision, other clients and shutdown down with it.
pub const IPC_DEADLINE: Duration = Duration::from_secs(2);
/// How long an exchange waits between looks at its deadline and at
/// `IPC_ABANDONED`.
const IPC_WAIT_SLICE: Duration = Duration::from_millis(20);

/// Set by the service's SIGTERM/SIGINT handler (one atomic store, so it is
/// async-signal-safe). An mpv IPC exchange in progress gives up at its next
/// look instead of waiting out its deadline, and later ones fail at once, so
/// a stalled renderer cannot hold up shutdown.
pub static IPC_ABANDONED: AtomicBool = AtomicBool::new(false);
static DRIVER_NONCE: AtomicU64 = AtomicU64::new(1);

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

/// Run a helper to completion within `timeout`. Its standard output is
/// drained and discarded; its standard error is kept, bounded, for the
/// diagnostic.
fn run_bounded(
    command: &mut Command,
    label: &str,
    timeout: Duration,
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
        0,
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
                // A successful helper leader must not leave background work
                // or inherited capture descriptors behind.
                unsafe {
                    libc::kill(-(child.id() as i32), libc::SIGKILL);
                }
                stdout.finish();
                return Ok(BoundedOutput {
                    status,
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
    /// Global automatic policy, separate from saved settings and user transport.
    fn set_animation_inhibited(&mut self, _inhibited: bool) {}
    fn begin_apply(&mut self) {}
    /// Return the compositor's currently connected output names.
    ///
    /// A batch-aware driver must reuse this same snapshot for any renderer
    /// decisions made before `end_apply`, so one wallpaper hand-over performs
    /// at most one compositor query.
    fn connected_outputs(&mut self) -> Result<Vec<String>, String> {
        Err("live output discovery is unavailable".into())
    }
    /// The compositor's last successful display snapshot, switched-off heads
    /// included, for `status`. Empty for drivers that do not ask one.
    fn published_outputs(&self) -> &[Output] {
        &[]
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
    /// The renderer programs that could not be started at all, in the order
    /// of `RENDERER_PROGRAMS`, at most one record each. A record goes when a
    /// spawn of that program succeeds, when the driver is reconfigured, and
    /// in `recheck_missing_renderers`. Empty for drivers that start nothing.
    fn missing_renderers(&self) -> Vec<MissingRenderer> {
        Vec::new()
    }
    /// The missing program behind `output`'s last motion start, when that
    /// start found its renderer missing and showed the still instead. Gone
    /// once the output is stopped or applied again.
    fn motion_missing(&self, _output: &str) -> Option<MissingRenderer> {
        None
    }
    /// For a reload that does not change the renderer settings: forget each
    /// missing program whose path is now an executable file, and return the
    /// outputs showing a still because of it, which the reload applies again.
    fn recheck_missing_renderers(&mut self) -> Vec<String> {
        Vec::new()
    }
    fn stop(&mut self);
}

/// The two renderer programs, in the order `missing_renderers` reports them.
pub const RENDERER_PROGRAMS: [&str; 2] = ["mpvpaper", "linux-wallpaperengine"];

/// How long after its spawn a renderer that exits with status 127 is taken
/// as one that could not run at all rather than one that failed. 127 is what
/// the dynamic loader exits with when a library is missing ("error while
/// loading shared libraries") and what a shell exits with for a command it
/// cannot find, both before the program's own code runs. The loader fails
/// within milliseconds of the exec, and the service looks at its children
/// every few tens of milliseconds, so five seconds leaves room for a loaded
/// machine and a slow disk while staying far below anything a renderer that
/// drew a frame would do; one that runs that long and then exits 127 is
/// failing in its own code, and stays a failure.
pub const LOADER_FAILURE_WINDOW: Duration = Duration::from_secs(5);

#[cfg(test)]
thread_local! {
    /// How many times `PathIdentity::of` looked at a path, on this thread.
    static IDENTITY_LOOKUPS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

/// What the dynamic loader writes when a library is missing. A 127 with it
/// is a renderer that could not run, however late its exit is noticed.
const LOADER_LINE: &str = "error while loading shared libraries";

/// Why a renderer program could not run (`missing_renderers`'s `reason`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MissingReason {
    /// Nothing is at the path.
    NotFound,
    /// Something is there that is not an executable file.
    NotExecutable,
    /// An executable file is there, and cannot run here: its interpreter or a
    /// library is missing, or its file system forbids running it.
    CannotRun,
    /// The path cannot be reached: a folder on the way does not let the
    /// service in, so what is there (if anything) cannot be told.
    NoAccess,
}

impl MissingReason {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::NotFound => "not-found",
            Self::NotExecutable => "not-executable",
            Self::CannotRun => "cannot-run",
            Self::NoAccess => "no-access",
        }
    }
}

/// What is at a renderer's path, enough to tell a later install from the
/// same file. A reload tries a program again only once this changed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PathIdentity {
    Absent,
    Present {
        dev: u64,
        ino: u64,
        size: u64,
        mtime: i64,
        mtime_nsec: i64,
        mode: u32,
    },
    Unreadable(std::io::ErrorKind),
}

impl PathIdentity {
    /// The identity of what `path` names now, symbolic links followed (a
    /// profile switch re-points one, and that is a change).
    pub fn of(path: &Path) -> Self {
        #[cfg(test)]
        IDENTITY_LOOKUPS.with(|count| count.set(count.get() + 1));
        match fs::metadata(path) {
            Ok(metadata) => Self::Present {
                dev: metadata.dev(),
                ino: metadata.ino(),
                size: metadata.size(),
                mtime: metadata.mtime(),
                mtime_nsec: metadata.mtime_nsec(),
                mode: metadata.mode(),
            },
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
                ) =>
            {
                Self::Absent
            }
            Err(error) => Self::Unreadable(error.kind()),
        }
    }

    fn executable_file(&self) -> bool {
        matches!(self, Self::Present { mode, .. } if (mode & libc::S_IFMT) == libc::S_IFREG && (mode & 0o111) != 0)
    }
}

/// A renderer program that could not run at all, so nothing about the
/// wallpaper: the spawn of the path the document names failed with
/// `NotFound` or `PermissionDenied`, or the child exited with status 127
/// within `LOADER_FAILURE_WINDOW` (a missing library). The driver shows the
/// entry's still instead of motion and nothing is held against the entry. A
/// renderer that ran and then exited is a `RendererFailure`, as before. See
/// docs/missing-components.md.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MissingRenderer {
    /// One of `RENDERER_PROGRAMS`.
    pub program: &'static str,
    /// The path the document names for it.
    pub path: String,
    /// What went wrong, e.g. `No such file or directory (os error 2)`.
    pub error: String,
    pub reason: MissingReason,
    /// What was at `path` then.
    pub identity: PathIdentity,
}

impl MissingRenderer {
    /// A record for `program` at `path`, its reason told from the spawn's
    /// error and what is at the path.
    pub fn from_spawn(program: &'static str, path: &Path, error: &std::io::Error) -> Self {
        let identity = PathIdentity::of(path);
        let reason = match (&identity, error.kind()) {
            (PathIdentity::Absent, _) => MissingReason::NotFound,
            // The path itself could not be looked at: a folder on the way.
            (PathIdentity::Unreadable(_), _) => MissingReason::NoAccess,
            (identity, std::io::ErrorKind::PermissionDenied) if !identity.executable_file() => {
                MissingReason::NotExecutable
            }
            _ => MissingReason::CannotRun,
        };
        Self {
            program,
            path: path.display().to_string(),
            error: error.to_string(),
            reason,
            identity,
        }
    }

    /// The renderer is missing, or cannot run here, in a few words.
    pub fn short(&self) -> String {
        match self.reason {
            MissingReason::NotFound => format!("{} is missing", self.program),
            MissingReason::NotExecutable | MissingReason::CannotRun | MissingReason::NoAccess => {
                format!("{} cannot run here", self.program)
            }
        }
    }

    /// What a display showing its still because of it says (`last_error`).
    pub fn diagnostic(&self) -> String {
        self.diagnostic_after(Ok(()))
    }

    /// [`Self::diagnostic`] once the still was put back (`fallback`), after
    /// a child that could not run exited. When that failed too, it says so
    /// and does not claim the still is showing.
    pub fn diagnostic_after(&self, fallback: Result<(), String>) -> String {
        let cause = match self.reason {
            MissingReason::NotFound => format!(
                "{}: cannot start {}: {}",
                self.short(),
                self.path,
                self.error
            ),
            MissingReason::NotExecutable | MissingReason::CannotRun | MissingReason::NoAccess => {
                format!("{}: {}: {}", self.short(), self.path, self.error)
            }
        };
        match fallback {
            Ok(()) => format!("{cause}; paired still is active"),
            Err(error) => format!("{cause}; could not reapply the still: {error}"),
        }
    }
}

/// Why a renderer did not start: its program is missing, or anything else.
enum StartError {
    Missing(std::io::Error),
    Failed(String),
}

impl StartError {
    fn into_message(self, program: &str) -> String {
        match self {
            Self::Missing(error) => format!("cannot start {program}: {error}"),
            Self::Failed(message) => message,
        }
    }
}

/// Whether a spawn error means the program itself could not be started.
fn program_missing(error: &std::io::Error) -> bool {
    matches!(
        error.kind(),
        std::io::ErrorKind::NotFound | std::io::ErrorKind::PermissionDenied
    )
}

/// What a renderer that exited with status 127 said about why, for its
/// record: the loader's line when there is one.
fn loader_failure(diagnostics: &str) -> String {
    diagnostics
        .lines()
        .find(|line| line.contains(LOADER_LINE))
        .map(|line| line.trim().to_string())
        .unwrap_or_else(|| "it exited with status 127 as it started".to_string())
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
    socket_namespace: u64,
    /// When the running child was spawned.
    started: Option<Instant>,
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
        Self::new_in_namespace(socket_hash(b"default"))
    }

    fn new_in_namespace(socket_namespace: u64) -> Self {
        Self {
            child: None,
            socket: None,
            diagnostics: None,
            pause_transport: PauseTransport::None,
            socket_namespace,
            started: None,
        }
    }

    fn ipc(&self, command: serde_json::Value) -> Result<(), String> {
        let Some(path) = &self.socket else {
            return Err("mpvpaper IPC socket is not configured".into());
        };
        mpv_ipc(
            path,
            &command,
            Instant::now() + IPC_DEADLINE,
            &IPC_ABANDONED,
        )
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
                unsafe {
                    libc::kill(-(child.id() as i32), libc::SIGKILL);
                }
                self.child.take();
                self.pause_transport = PauseTransport::None;
                if let Some(socket) = self.socket.take() {
                    remove_mpv_socket(&socket);
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

impl Mpvpaper {
    /// `VideoRenderer::start`, saying whether the program itself is missing.
    fn launch(
        &mut self,
        entry: &Entry,
        output: &str,
        settings: &RendererSettings,
        refresh_millihz: Option<u64>,
    ) -> Result<(), StartError> {
        let video = entry
            .motion
            .as_ref()
            .ok_or_else(|| StartError::Failed("video entry has no motion path".into()))?;
        self.stop();
        let socket = std::env::var_os("XDG_RUNTIME_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(std::env::temp_dir)
            .join(mpv_socket_name(self.socket_namespace, output));
        let socket =
            (socket.as_os_str().as_bytes().len() <= MAX_SOCKET_PATH_BYTES).then_some(socket);
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
        ];
        if let Some(socket) = &socket {
            options.push(format!("input-ipc-server={}", socket.display()));
        }
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
        let mut child = command.spawn().map_err(|error| {
            if program_missing(&error) {
                StartError::Missing(error)
            } else {
                StartError::Failed(format!("cannot start mpvpaper: {error}"))
            }
        })?;
        self.diagnostics = child
            .stderr
            .take()
            .map(|stderr| BoundedCapture::start(stderr, MAX_DIAGNOSTIC_BYTES));
        self.child = Some(child);
        self.socket = socket;
        self.pause_transport = PauseTransport::None;
        self.started = Some(Instant::now());
        options.clear();
        Ok(())
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
        self.launch(entry, output, settings, refresh_millihz)
            .map_err(|error| error.into_message("mpvpaper"))
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
            remove_mpv_socket(&socket);
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

/// One request to mpv's JSON IPC socket at `path`, and its one-line reply,
/// within `deadline` as a whole: the connect, the write and the read share
/// it. A listener that never accepts (its backlog full), never reads or
/// never answers ends in an ordinary IPC error at the deadline, or as soon as
/// `abandoned` is set.
pub fn mpv_ipc(
    path: &Path,
    command: &serde_json::Value,
    deadline: Instant,
    abandoned: &AtomicBool,
) -> Result<(), String> {
    let mut stream = connect_until(path, deadline, abandoned)
        .map_err(|error| format!("cannot connect to mpvpaper IPC {}: {error}", path.display()))?;
    let mut encoded = serde_json::to_vec(&json!({"command": command}))
        .map_err(|error| format!("cannot encode mpvpaper IPC request: {error}"))?;
    encoded.push(b'\n');
    write_until(&mut stream, &encoded, deadline, abandoned)
        .map_err(|error| format!("cannot write mpvpaper IPC request: {error}"))?;
    let reply = read_line_until(&mut stream, 4097, deadline, abandoned)
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

/// How long the next wait may be: a slice of what is left before `deadline`,
/// or the reason to stop waiting now.
fn wait_slice(deadline: Instant, abandoned: &AtomicBool) -> std::io::Result<Duration> {
    if abandoned.load(Ordering::Relaxed) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::Interrupted,
            "the service is shutting down",
        ));
    }
    let left = deadline.saturating_duration_since(Instant::now());
    if left.is_zero() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::TimedOut,
            format!("no answer within {} s", IPC_DEADLINE.as_secs()),
        ));
    }
    Ok(left.min(IPC_WAIT_SLICE))
}

/// Connect without blocking. A Unix listener whose backlog is full refuses a
/// nonblocking connect with `EAGAIN` instead of queueing it, so retry until
/// it has room, the deadline passes (`TimedOut`) or the exchange is
/// abandoned. The service's startup ownership probe uses it too.
pub fn connect_until(
    path: &Path,
    deadline: Instant,
    abandoned: &AtomicBool,
) -> std::io::Result<UnixStream> {
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    let bytes = path.as_os_str().as_bytes();
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    if bytes.is_empty() || bytes.len() >= address.sun_path.len() || bytes.contains(&0) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "socket path is not a usable Unix socket address",
        ));
    }
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    for (slot, byte) in address.sun_path.iter_mut().zip(bytes) {
        *slot = *byte as libc::c_char;
    }
    let length = std::mem::size_of::<libc::sockaddr_un>() as libc::socklen_t;
    let fd = unsafe {
        libc::socket(
            libc::AF_UNIX,
            libc::SOCK_STREAM | libc::SOCK_NONBLOCK | libc::SOCK_CLOEXEC,
            0,
        )
    };
    if fd < 0 {
        return Err(std::io::Error::last_os_error());
    }
    // Owned at once, so every early return closes it.
    let socket = unsafe { OwnedFd::from_raw_fd(fd) };
    loop {
        let wait = wait_slice(deadline, abandoned)?;
        let result =
            unsafe { libc::connect(socket.as_raw_fd(), (&raw const address).cast(), length) };
        if result == 0 {
            break;
        }
        let error = std::io::Error::last_os_error();
        match error.raw_os_error() {
            Some(libc::EISCONN) => break,
            Some(libc::EAGAIN | libc::EINTR) => thread::sleep(wait),
            Some(libc::EINPROGRESS | libc::EALREADY) => {
                let mut poll = libc::pollfd {
                    fd: socket.as_raw_fd(),
                    events: libc::POLLOUT,
                    revents: 0,
                };
                let timeout = i32::try_from(wait.as_millis()).unwrap_or(i32::MAX).max(1);
                if unsafe { libc::poll(&mut poll, 1, timeout) } > 0 {
                    let mut failure: libc::c_int = 0;
                    let mut size = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
                    let read = unsafe {
                        libc::getsockopt(
                            socket.as_raw_fd(),
                            libc::SOL_SOCKET,
                            libc::SO_ERROR,
                            (&raw mut failure).cast(),
                            &mut size,
                        )
                    };
                    if read != 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                    if failure != 0 {
                        return Err(std::io::Error::from_raw_os_error(failure));
                    }
                    break;
                }
            }
            _ => return Err(error),
        }
    }
    let stream = UnixStream::from(socket);
    stream.set_nonblocking(false)?;
    Ok(stream)
}

fn write_until(
    stream: &mut UnixStream,
    mut bytes: &[u8],
    deadline: Instant,
    abandoned: &AtomicBool,
) -> std::io::Result<()> {
    while !bytes.is_empty() {
        stream.set_write_timeout(Some(wait_slice(deadline, abandoned)?))?;
        match stream.write(bytes) {
            Ok(0) => return Err(std::io::ErrorKind::WriteZero.into()),
            Ok(written) => bytes = &bytes[written..],
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock
                        | std::io::ErrorKind::TimedOut
                        | std::io::ErrorKind::Interrupted
                ) => {}
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

/// The first line of the reply (or what arrives before the peer closes),
/// reading at most `limit` bytes.
fn read_line_until(
    stream: &mut UnixStream,
    limit: usize,
    deadline: Instant,
    abandoned: &AtomicBool,
) -> std::io::Result<String> {
    let mut reply = Vec::new();
    let mut chunk = [0u8; 512];
    while reply.len() < limit && !reply.contains(&b'\n') {
        stream.set_read_timeout(Some(wait_slice(deadline, abandoned)?))?;
        let wanted = (limit - reply.len()).min(chunk.len());
        match stream.read(&mut chunk[..wanted]) {
            Ok(0) => break,
            Ok(read) => reply.extend_from_slice(&chunk[..read]),
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::WouldBlock
                        | std::io::ErrorKind::TimedOut
                        | std::io::ErrorKind::Interrupted
                ) => {}
            Err(error) => return Err(error),
        }
    }
    if let Some(end) = reply.iter().position(|byte| *byte == b'\n') {
        reply.truncate(end + 1);
    }
    String::from_utf8(reply)
        .map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidData, "reply is not UTF-8"))
}

pub struct SystemDriver {
    settings: RendererSettings,
    videos: HashMap<String, ActiveVideo>,
    scenes: HashMap<String, ActiveScene>,
    applying_batch: bool,
    /// One hand-over's answer, reused for every renderer decision in it.
    output_snapshot: Option<Result<Vec<Output>, String>>,
    outputs: Box<dyn OutputSource>,
    /// The last successful answer, switched-off heads included.
    published: Vec<Output>,
    socket_namespace: u64,
    animations_inhibited: bool,
    /// Each renderer program that could not be started at all, by its index
    /// in `RENDERER_PROGRAMS`.
    missing: [Option<MissingRenderer>; 2],
    /// The outputs (by `key`) that show their still because their renderer
    /// is missing: the output as the runtime names it, and what was missing.
    missing_motion: HashMap<String, (String, MissingRenderer)>,
}

struct ActiveVideo {
    renderer: Mpvpaper,
    entry: Entry,
    output: String,
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

pub(crate) fn usable_output_name(candidate: &str) -> bool {
    !candidate.is_empty()
        && candidate.len() <= MAX_OUTPUT_NAME_BYTES
        && !candidate.chars().any(char::is_whitespace)
        && !candidate.chars().any(char::is_control)
}

fn socket_hash(bytes: &[u8]) -> u64 {
    bytes.iter().fold(0xcbf29ce484222325_u64, |hash, byte| {
        (hash ^ u64::from(*byte)).wrapping_mul(0x100000001b3)
    })
}

fn service_socket_namespace(path: &Path) -> u64 {
    let mut bytes = unsafe { libc::geteuid() }.to_ne_bytes().to_vec();
    bytes.extend_from_slice(path.as_os_str().as_bytes());
    socket_hash(&bytes)
}

fn renderer_socket_namespace(path: &Path) -> u64 {
    // The service socket separates supported custom daemon instances. A
    // process/session nonce additionally means a crashed old renderer can
    // never make the next daemon unlink a predictable stale endpoint before
    // mpvpaper starts. PID and an in-process sequence cover clock failures;
    // wall-clock nanoseconds keep PID reuse from recreating the same name.
    let mut bytes = service_socket_namespace(path).to_ne_bytes().to_vec();
    bytes.extend_from_slice(&std::process::id().to_ne_bytes());
    bytes.extend_from_slice(&DRIVER_NONCE.fetch_add(1, Ordering::Relaxed).to_ne_bytes());
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    bytes.extend_from_slice(&timestamp.to_ne_bytes());
    socket_hash(&bytes)
}

fn remove_mpv_socket_with_hook(path: &Path, before_recheck: impl FnOnce()) -> Result<(), String> {
    let opened = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(format!("cannot inspect mpv IPC socket: {error}")),
    };
    if !opened.file_type().is_socket() {
        return Err(format!(
            "refusing to remove non-socket mpv IPC path {}",
            path.display()
        ));
    }
    before_recheck();
    let current = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(format!("cannot recheck mpv IPC socket: {error}")),
    };
    if !current.file_type().is_socket()
        || (opened.dev(), opened.ino()) != (current.dev(), current.ino())
    {
        return Err(format!(
            "mpv IPC path {} changed while it was being cleaned up",
            path.display()
        ));
    }
    fs::remove_file(path).map_err(|error| format!("cannot remove mpv IPC socket: {error}"))
}

fn remove_mpv_socket(path: &Path) {
    if let Err(error) = remove_mpv_socket_with_hook(path, || {}) {
        eprintln!("wall-in-one-service: {error}");
    }
}

fn mpv_socket_name(namespace: u64, output: &str) -> String {
    format!(
        "wio-{:x}-{namespace:016x}-{:016x}.sock",
        unsafe { libc::geteuid() },
        socket_hash(output.as_bytes())
    )
}

fn unambiguous_refresh_millihz(outputs: &[Output], output: &str) -> Option<u64> {
    let candidates: Vec<&Output> = outputs
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
    started: Instant,
}

impl SystemDriver {
    pub fn new(settings: RendererSettings) -> Self {
        Self::new_for_service_socket(settings, Path::new("/wall-in-one-runtime.sock"))
    }

    pub fn new_for_service_socket(settings: RendererSettings, socket: &Path) -> Self {
        Self::with_output_source(
            settings,
            socket,
            Box::new(WaylandOutputs::from_environment()),
        )
    }

    /// A driver that learns its displays from `outputs` rather than from the
    /// compositor `WAYLAND_DISPLAY` names.
    pub fn with_output_source(
        settings: RendererSettings,
        socket: &Path,
        outputs: Box<dyn OutputSource>,
    ) -> Self {
        Self {
            settings,
            videos: HashMap::new(),
            scenes: HashMap::new(),
            applying_batch: false,
            output_snapshot: None,
            outputs,
            published: Vec::new(),
            socket_namespace: renderer_socket_namespace(socket),
            animations_inhibited: false,
            missing: [None, None],
            missing_motion: HashMap::new(),
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
        self.missing_motion.remove(&key);
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
        let result = run_bounded(&mut command, "noctalia", HELPER_TIMEOUT)?;
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

    /// The enabled outputs, asked of the compositor. A switched-off head is
    /// published but never a target.
    fn query_outputs(&mut self) -> Result<Vec<Output>, String> {
        let reported = self.outputs.outputs()?;
        let enabled: Vec<Output> = reported
            .iter()
            .filter(|output| output.enabled)
            .cloned()
            .collect();
        if enabled.is_empty() {
            // A failure like any other: `published` keeps the last snapshot
            // that succeeded, as status documents.
            return Err(
                "the compositor reported no enabled outputs; paired still remains applied".into(),
            );
        }
        self.published = reported;
        Ok(enabled)
    }

    fn live_outputs(&mut self) -> Result<Vec<Output>, String> {
        if !self.applying_batch {
            return self.query_outputs();
        }
        if self.output_snapshot.is_none() {
            self.output_snapshot = Some(self.query_outputs());
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
        if let Some(scaling) = self.settings.scene_scaling.option() {
            command.arg("--scaling").arg(scaling);
        }
        if let Some(clamp) = self.settings.scene_clamp.option() {
            command.arg("--clamp").arg(clamp);
        }
        command.arg("--bg").arg(scene);
    }

    fn start_scene(&mut self, entry: &Entry, output: &str) -> Result<(), StartError> {
        if !self.settings.own_scene_renderer {
            return Err(StartError::Failed(
                "scene renderer ownership is disabled; paired still remains applied".into(),
            ));
        }
        let scene = entry
            .scene_id
            .as_ref()
            .ok_or_else(|| StartError::Failed("scene entry has no scene id".into()))?;
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
            for connector in self.current_outputs().map_err(StartError::Failed)? {
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
        let mut child = command.spawn().map_err(|error| {
            if program_missing(&error) {
                StartError::Missing(error)
            } else {
                StartError::Failed(format!("cannot start linux-wallpaperengine: {error}"))
            }
        })?;
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
                started: Instant::now(),
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
        if !runtime.dynamics_enabled || self.animations_inhibited {
            return Ok(());
        }
        let (index, started) = match entry.kind {
            EntryKind::Still => return Ok(()),
            EntryKind::Video => {
                let mut video = Mpvpaper::new_in_namespace(self.socket_namespace);
                let refresh = self.interpolation_refresh(output);
                let started = video.launch(entry, output, &self.settings, refresh);
                if started.is_ok() {
                    self.videos.insert(
                        Self::key(output),
                        ActiveVideo {
                            renderer: video,
                            entry: entry.clone(),
                            output: output.to_string(),
                        },
                    );
                }
                (0, started)
            }
            EntryKind::Scene => (1, self.start_scene(entry, output)),
        };
        match started {
            Ok(()) => {
                // The program is there: whatever an earlier spawn said. One
                // recorded as unable to run (a library its loader could not
                // find) starts just as well and exits 127 a moment later, so
                // its record stays until a child of it has outlived
                // LOADER_FAILURE_WINDOW (`poll_failures`): a status in
                // between must not say it can run now.
                if self.missing[index]
                    .as_ref()
                    .is_none_or(|record| record.reason != MissingReason::CannotRun)
                {
                    self.missing[index] = None;
                }
                Ok(())
            }
            Err(StartError::Missing(error)) => {
                // Not this wallpaper's fault: its still is already applied
                // and stays. The runtime reports the program, not the entry.
                let record = MissingRenderer::from_spawn(
                    RENDERER_PROGRAMS[index],
                    self.program_path(index),
                    &error,
                );
                self.record_missing(index, output, record);
                Ok(())
            }
            Err(StartError::Failed(message)) => Err(message),
        }
    }

    /// `output` shows its still because `record`'s program could not run.
    fn record_missing(&mut self, index: usize, output: &str, record: MissingRenderer) {
        self.missing_motion
            .insert(Self::key(output), (output.to_string(), record.clone()));
        self.missing[index] = Some(record);
    }

    /// A renderer that exited with status 127 right after its spawn could
    /// not run: a missing library, or a wrapper that could not find what it
    /// runs. Its display keeps the still the exit already restored, and the
    /// program is recorded as missing instead of the entry being blamed.
    fn loader_failed(
        &mut self,
        index: usize,
        output: &str,
        status: ExitStatus,
        started: Option<Instant>,
        diagnostics: &str,
    ) -> Option<MissingRenderer> {
        // The loader's own line says so whenever the exit is noticed (the
        // service's loop can be held past the window by a slow Noctalia); only
        // a bare 127 needs the window to tell it from a renderer's own exit.
        let loader = diagnostics.contains(LOADER_LINE);
        let early = started.is_some_and(|started| started.elapsed() <= LOADER_FAILURE_WINDOW);
        if status.code() != Some(127) || !(loader || early) {
            return None;
        }
        let path = self.program_path(index).to_path_buf();
        let record = MissingRenderer {
            program: RENDERER_PROGRAMS[index],
            path: path.display().to_string(),
            error: loader_failure(diagnostics),
            reason: MissingReason::CannotRun,
            identity: PathIdentity::of(&path),
        };
        self.record_missing(index, output, record.clone());
        Some(record)
    }

    /// The path the document names for `RENDERER_PROGRAMS[index]`.
    fn program_path(&self, index: usize) -> &Path {
        if index == 0 {
            &self.settings.mpvpaper_program
        } else {
            &self.settings.linux_wallpaperengine_program
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
    fn set_animation_inhibited(&mut self, inhibited: bool) {
        self.animations_inhibited = inhibited;
    }
    fn begin_apply(&mut self) {
        self.applying_batch = true;
        self.output_snapshot = None;
    }

    fn connected_outputs(&mut self) -> Result<Vec<String>, String> {
        self.current_outputs()
    }

    fn published_outputs(&self) -> &[Output] {
        &self.published
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
        // A display that is no longer a target shows nothing of ours: it
        // drops the mark of a missing renderer with its still, so a reload
        // that finds the program never applies again for it.
        self.missing_motion.retain(|key, _| wanted.contains(key));
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
        if let Some(video) = self.videos.get_mut(&key)
            && let Err(error) = video.renderer.set_paused(paused)
        {
            errors.push(error);
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
        // Other program paths, or the same ones: the next spawn says again.
        self.missing = [None, None];
        self.settings = settings;
    }

    fn poll_failures(&mut self) -> Vec<RendererFailure> {
        let mut failures = Vec::new();
        let mut exited_videos = Vec::new();
        for (key, active) in &mut self.videos {
            let started = active.renderer.started;
            match active.renderer.take_exit() {
                Ok(Some((status, diagnostics))) => {
                    exited_videos.push((
                        key.clone(),
                        status.to_string(),
                        diagnostics,
                        Some(status),
                        started,
                    ));
                }
                Ok(None) => {}
                Err(error) => exited_videos.push((key.clone(), error, String::new(), None, None)),
            }
        }
        for (key, status, diagnostics, exit, started) in exited_videos {
            if let Some(active) = self.videos.remove(&key) {
                let fallback = self.still(&active.entry, &active.output);
                let missing = exit.and_then(|exit| {
                    self.loader_failed(0, &active.output, exit, started, &diagnostics)
                });
                let message = match missing {
                    Some(record) => record.diagnostic_after(fallback),
                    None => Self::failure_message(
                        "mpvpaper",
                        &active.entry,
                        &active.output,
                        &status,
                        &diagnostics,
                        fallback,
                    ),
                };
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
                Ok(Some(status)) => {
                    exited_scenes.push((key.clone(), status.to_string(), false, Some(status)))
                }
                Ok(None) => {}
                Err(error) => exited_scenes.push((
                    key.clone(),
                    format!("cannot inspect linux-wallpaperengine: {error}"),
                    true,
                    None,
                )),
            }
        }
        for (key, status, must_stop, exit) in exited_scenes {
            if let Some(mut active) = self.scenes.remove(&key) {
                if must_stop {
                    // Dropping Child would orphan a renderer that may still
                    // own the output. Match every other hand-over path and
                    // explicitly terminate/reap its process group.
                    stop_group(&mut active.child);
                } else {
                    // The renderer leader already exited, but descendants
                    // can still own inherited pipes or wallpaper resources.
                    unsafe {
                        libc::kill(-(active.child.id() as i32), libc::SIGKILL);
                    }
                }
                let diagnostics = active
                    .diagnostics
                    .take()
                    .map(|capture| diagnostic(&capture.finish()))
                    .unwrap_or_default();
                let fallback = self.still(&active.entry, &active.output);
                let started = Some(active.started);
                let missing = exit.and_then(|exit| {
                    self.loader_failed(1, &active.output, exit, started, &diagnostics)
                });
                // A scene renderer that could not run is not a scene that
                // crashed it: no quarantine (docs/missing-components.md).
                let permanent_for_session = missing.is_none();
                let message = match missing {
                    Some(record) => record.diagnostic_after(fallback),
                    None => Self::failure_message(
                        "linux-wallpaperengine",
                        &active.entry,
                        &active.output,
                        &status,
                        &diagnostics,
                        fallback,
                    ),
                };
                failures.push(RendererFailure {
                    entry_id: active.entry.id.clone(),
                    kind: active.entry.kind,
                    scene_id: active.entry.scene_id.clone(),
                    output: active.output.clone(),
                    message,
                    permanent_for_session,
                });
            }
        }
        // A program recorded as unable to run whose child has now outlived
        // the loader's window runs after all: the record goes.
        let outlived = |started: Option<Instant>| {
            started.is_some_and(|started| started.elapsed() > LOADER_FAILURE_WINDOW)
        };
        let ran = [
            self.videos
                .values()
                .any(|active| outlived(active.renderer.started)),
            self.scenes
                .values()
                .any(|active| outlived(Some(active.started))),
        ];
        for (index, ran) in ran.into_iter().enumerate() {
            if ran
                && self.missing[index]
                    .as_ref()
                    .is_some_and(|record| record.reason == MissingReason::CannotRun)
            {
                self.missing[index] = None;
            }
        }
        failures
    }

    fn motion_active(&self, output: &str) -> bool {
        let key = Self::key(output);
        self.videos.contains_key(&key) || self.scenes.contains_key(&key)
    }

    fn missing_renderers(&self) -> Vec<MissingRenderer> {
        self.missing.iter().flatten().cloned().collect()
    }

    fn motion_missing(&self, output: &str) -> Option<MissingRenderer> {
        self.missing_motion
            .get(&Self::key(output))
            .map(|(_, record)| record.clone())
    }

    fn recheck_missing_renderers(&mut self) -> Vec<String> {
        let mut outputs = Vec::new();
        for (index, program) in RENDERER_PROGRAMS.iter().enumerate() {
            // Only what changed at the path is worth another try: a program
            // installed, replaced or re-pointed. The same file that could not
            // run would only fail the same way, after setting every still and
            // palette again.
            let marked = self
                .missing_motion
                .values()
                .any(|(_, record)| record.program == *program);
            if self.missing[index].is_none() && !marked {
                continue; // nothing recorded: nothing to look at
            }
            let now = PathIdentity::of(self.program_path(index));
            if self.missing[index]
                .as_ref()
                .is_some_and(|record| record.identity != now)
            {
                self.missing[index] = None;
            }
            // Every display still showing a still for it, whether or not the
            // program has since started on another one (which clears the
            // record above, not the other displays' marks).
            outputs.extend(
                self.missing_motion
                    .values()
                    .filter(|(_, record)| record.program == *program && record.identity != now)
                    .map(|(output, _)| output.clone()),
            );
        }
        outputs.sort();
        outputs
    }

    fn stop(&mut self) {
        self.missing_motion.clear();
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
            // The group leader may exit before a TERM-resistant descendant.
            // Finish the owned group before releasing its output/capture pipes.
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
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
    use std::io::{BufRead, BufReader};
    use std::os::unix::net::UnixListener;

    struct NoOutputs;

    impl OutputSource for NoOutputs {
        fn outputs(&mut self) -> Result<Vec<Output>, String> {
            Err("no compositor in this test".into())
        }
    }

    #[test]
    fn a_reload_with_nothing_recorded_looks_at_no_renderer_path() {
        let settings: RendererSettings = toml::from_str(
            r#"noctalia_program = "/bin/true"
mpvpaper_program = "/nonexistent/mpvpaper"
linux_wallpaperengine_program = "/nonexistent/linux-wallpaperengine"
own_scene_renderer = true
layer = "background"
video_when_hidden = "pause"
video_hardware_decode = true
video_interpolation = "off"
video_muted = true
video_volume = 0
scene_fps = 30
scene_muted = true
scene_volume = 0
scene_pause_when_covered = true
scene_scaling = ""
scene_clamp = ""
"#,
        )
        .unwrap();
        let mut driver = SystemDriver::with_output_source(
            settings,
            Path::new("/wall-in-one-runtime.sock"),
            Box::new(NoOutputs),
        );
        let lookups = || IDENTITY_LOOKUPS.with(std::cell::Cell::get);
        let before = lookups();
        assert!(driver.recheck_missing_renderers().is_empty());
        assert_eq!(lookups(), before, "nothing recorded, nothing looked at");
        driver.missing[0] = Some(MissingRenderer {
            program: "mpvpaper",
            path: "/nonexistent/mpvpaper".into(),
            error: "No such file or directory (os error 2)".into(),
            reason: MissingReason::NotFound,
            identity: PathIdentity::Absent,
        });
        assert!(driver.recheck_missing_renderers().is_empty());
        assert_eq!(lookups(), before + 1, "only the recorded program's path");
    }

    #[test]
    fn stopping_a_leader_also_releases_term_resistant_descendant_pipes() {
        let mut child = Command::new("/bin/sh")
            .args([
                "-c",
                "(trap '' TERM; printf 'ready\\n'; while :; do sleep 1; done) & wait",
            ])
            .stdout(Stdio::piped())
            .process_group(0)
            .spawn()
            .unwrap();
        let mut pipe = BufReader::new(child.stdout.take().unwrap());
        let mut ready = String::new();
        pipe.read_line(&mut ready).unwrap();
        assert_eq!(ready, "ready\n");
        let capture = BoundedCapture::start(pipe, 32);
        stop_group_with_grace(&mut child, Duration::from_millis(200));
        let captured = capture.finish();
        assert!(
            !captured.truncated,
            "descendant retained the capture pipe after stop"
        );
        assert!(child.try_wait().unwrap().is_some());
    }

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
            Output {
                name: "DP-1".into(),
                refresh_millihz: Some(60_000),
                ..Output::default()
            },
            Output {
                name: "DP-2".into(),
                refresh_millihz: Some(60_000),
                ..Output::default()
            },
        ];
        assert_eq!(unambiguous_refresh_millihz(&uniform, ""), Some(60_000));
        assert_eq!(unambiguous_refresh_millihz(&uniform, "DP-2"), Some(60_000));

        let mixed = vec![
            uniform[0].clone(),
            Output {
                name: "DP-2".into(),
                refresh_millihz: Some(165_004),
                ..Output::default()
            },
        ];
        let partial = vec![
            uniform[0].clone(),
            Output {
                name: "DP-2".into(),
                refresh_millihz: None,
                ..Output::default()
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
    fn renderer_instance_and_connector_namespaces_do_not_share_mpv_sockets() {
        let first = service_socket_namespace(Path::new("/run/user/1000/first.sock"));
        let second = service_socket_namespace(Path::new("/run/user/1000/second.sock"));
        assert_ne!(first, second);
        assert_ne!(
            mpv_socket_name(first, "DP-1"),
            mpv_socket_name(first, "DP_1")
        );
        assert_ne!(mpv_socket_name(first, ""), mpv_socket_name(first, "ALL"));
        assert_ne!(
            mpv_socket_name(first, "DP-1"),
            mpv_socket_name(second, "DP-1")
        );
        assert!(mpv_socket_name(first, &"x".repeat(MAX_OUTPUT_NAME_BYTES)).len() <= 58);
    }

    #[test]
    fn mpv_socket_cleanup_never_deletes_a_replacement_or_sentinel() {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "wall-in-one-mpv-cleanup-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir(&root).unwrap();
        let socket = root.join("renderer.sock");
        fs::write(&socket, b"sentinel").unwrap();
        assert!(
            remove_mpv_socket_with_hook(&socket, || {})
                .unwrap_err()
                .contains("non-socket")
        );
        assert_eq!(fs::read(&socket).unwrap(), b"sentinel");

        fs::remove_file(&socket).unwrap();
        drop(UnixListener::bind(&socket).unwrap());
        let error = remove_mpv_socket_with_hook(&socket, || {
            fs::remove_file(&socket).unwrap();
            fs::write(&socket, b"replacement").unwrap();
        })
        .unwrap_err();
        assert!(error.contains("changed while it was being cleaned up"));
        assert_eq!(fs::read(&socket).unwrap(), b"replacement");
        fs::remove_dir_all(root).unwrap();
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
        let error = match run_bounded(&mut command, "test helper", Duration::from_millis(100)) {
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
        let output = run_bounded(&mut command, "test helper", Duration::from_secs(1)).unwrap();
        assert!(!output.status.success());
        let rendered = diagnostic(&output.stderr);
        assert!(rendered.len() <= MAX_DIAGNOSTIC_BYTES + 32);
        assert!(rendered.ends_with("[truncated]"));
    }

    /// A listener that never accepts, with its whole connection backlog
    /// already taken: `listen(0)` admits one queued client, which `filler`
    /// is. The next connect cannot complete until something accepts.
    pub(crate) fn stalled_endpoint(path: &Path) -> (std::os::fd::OwnedFd, UnixStream) {
        use std::os::fd::FromRawFd;
        let bytes = path.as_os_str().as_bytes();
        let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
        assert!(bytes.len() < address.sun_path.len());
        address.sun_family = libc::AF_UNIX as libc::sa_family_t;
        for (slot, byte) in address.sun_path.iter_mut().zip(bytes) {
            *slot = *byte as libc::c_char;
        }
        let fd = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_STREAM | libc::SOCK_CLOEXEC, 0) };
        assert!(fd >= 0);
        let listener = unsafe { std::os::fd::OwnedFd::from_raw_fd(fd) };
        let length = std::mem::size_of::<libc::sockaddr_un>() as libc::socklen_t;
        assert_eq!(
            unsafe { libc::bind(fd, (&raw const address).cast(), length) },
            0,
            "{}",
            std::io::Error::last_os_error()
        );
        assert_eq!(unsafe { libc::listen(fd, 0) }, 0);
        let filler = UnixStream::connect(path).unwrap();
        (listener, filler)
    }

    fn scratch_socket(label: &str) -> (PathBuf, PathBuf) {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "wall-in-one-{label}-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir_all(&root).unwrap();
        let socket = root.join("mpv.sock");
        (root, socket)
    }

    #[test]
    fn a_stalled_mpv_endpoint_cannot_hold_the_runtime_past_its_deadline() {
        let (root, socket) = scratch_socket("mpv-stalled");
        let (listener, filler) = stalled_endpoint(&socket);
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

        // Pause through IPC on a worker, so an unbounded connect fails this
        // test instead of hanging it.
        let (sender, receiver) = std::sync::mpsc::channel();
        let worker = thread::spawn(move || {
            let started = Instant::now();
            let result = renderer.ipc(json!(["set_property", "pause", true]));
            sender.send((result, started.elapsed())).unwrap();
            renderer
        });
        let outcome = receiver.recv_timeout(Duration::from_secs(6));
        drop(filler);
        drop(listener);
        let (result, elapsed) = outcome.expect("mpv IPC to a stalled endpoint never returned");
        let error = result.unwrap_err();
        assert!(error.contains("cannot connect to mpvpaper IPC"), "{error}");
        assert!(
            elapsed >= IPC_DEADLINE && elapsed < IPC_DEADLINE + Duration::from_secs(1),
            "{elapsed:?}"
        );
        let mut renderer = worker.join().unwrap();
        renderer.stop();
        fs::remove_dir_all(root).unwrap();
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
