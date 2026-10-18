//! Which displays the compositor has, asked over standard Wayland protocols.
//!
//! The service is a long-lived Wayland client of the compositor it renders
//! on. `wl_output` names each enabled output and gives its current mode,
//! scale, physical size, make and model; `zxdg_output_manager_v1` gives its
//! logical position and size; `zwlr_output_manager_v1`, where the compositor
//! offers it, adds the heads that are switched off, which have no `wl_output`.
//! None of this belongs to one compositor, and a compositor that offers only
//! `wl_output` still names every display a wallpaper can be put on.
//!
//! One connection is kept for the life of the service. Each question is one
//! bounded round trip on it, which also takes in every output added or
//! removed since the last; a connection the compositor closed is replaced by
//! a new one on the next question.

use crate::renderer::{IPC_ABANDONED, MAX_OUTPUT_NAME_BYTES, usable_output_name};
use serde::Serialize;
use std::ffi::OsString;
use std::io::ErrorKind;
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};
use wayland_client::backend::WaylandError;
use wayland_client::protocol::{wl_callback, wl_output, wl_registry};
use wayland_client::{Connection, Dispatch, EventQueue, Proxy, QueueHandle, WEnum};
use wayland_protocols::xdg::xdg_output::zv1::client::{zxdg_output_manager_v1, zxdg_output_v1};
use wayland_protocols_wlr::output_management::v1::client::{
    zwlr_output_head_v1, zwlr_output_manager_v1, zwlr_output_mode_v1,
};

/// The most enabled outputs one snapshot may hold: each is a renderer route.
pub const MAX_OUTPUTS: usize = 64;
/// Refresh rates above this are not a display's; they are left out.
pub const MAX_REFRESH_MILLIHZ: u64 = 1_000_000;
/// One question to the compositor, connecting included. The same ceiling the
/// renderer's helper programs have.
pub const DISCOVERY_TIMEOUT: Duration = Duration::from_secs(3);
/// Make and model are descriptions; a status row carries this much of each.
const MAX_LABEL_BYTES: usize = 256;
/// How long one wait for the compositor lasts before the deadline and a
/// shutdown are looked at again.
const WAIT_SLICE: Duration = Duration::from_millis(20);
/// The first round trip learns the globals and binds them; the next brings
/// what was bound. A manager announced after an output needs one more.
const MAX_ROUND_TRIPS: usize = 4;
/// Output globals and heads tracked at once, far above `MAX_OUTPUTS` so that
/// switched-off heads alone never make the enabled ones unreadable.
const MAX_TRACKED: usize = 4 * MAX_OUTPUTS;

const WL_OUTPUT_VERSION: u32 = 4;
const XDG_OUTPUT_MANAGER_VERSION: u32 = 3;
const WLR_OUTPUT_MANAGER_VERSION: u32 = 4;
/// The version from which `release` exists on `wl_output` and on wlr heads
/// and modes. Sending it to an older object is a protocol error.
const RELEASE_VERSION: u32 = 3;

/// One display, as the compositor reports it.
#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct Output {
    /// The connector name (`eDP-1`, `DP-2`) every renderer is given.
    pub name: String,
    /// False only for a head that wlr-output-management reports switched
    /// off. Such a head is listed, never rendered on.
    pub enabled: bool,
    pub make: String,
    pub model: String,
    /// Logical position in the compositor's global space.
    pub x: i32,
    pub y: i32,
    /// Logical size, after scaling; zero when the compositor has no
    /// xdg-output.
    pub width: u32,
    pub height: u32,
    pub scale: f64,
    /// The current mode, in pixels.
    pub mode_width: u32,
    pub mode_height: u32,
    pub refresh_millihz: Option<u64>,
    pub physical_width_mm: u32,
    pub physical_height_mm: u32,
}

/// Where a driver learns which displays there are.
pub trait OutputSource: Send {
    /// Every display the compositor reports now, sorted by connector name.
    fn outputs(&mut self) -> Result<Vec<Output>, String>;
}

/// The socket `WAYLAND_DISPLAY` names, the way a Wayland client finds it.
///
/// `WAYLAND_SOCKET` is deliberately not used: it hands over one connection,
/// once, and this one has to be made again after a compositor restart.
pub fn environment_socket(
    display: Option<OsString>,
    runtime_directory: Option<OsString>,
) -> Result<PathBuf, String> {
    let display = display
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .ok_or("cannot connect to the Wayland compositor: WAYLAND_DISPLAY is not set")?;
    if display.is_absolute() {
        return Ok(display);
    }
    let runtime_directory = runtime_directory
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .ok_or(
            "cannot connect to the Wayland compositor: WAYLAND_DISPLAY is relative and \
             XDG_RUNTIME_DIR is not an absolute path",
        )?;
    Ok(runtime_directory.join(display))
}

/// The compositor, asked over one Wayland connection kept open between
/// questions.
pub struct WaylandOutputs {
    socket: Option<PathBuf>,
    session: Option<Session>,
}

impl WaylandOutputs {
    /// The compositor `WAYLAND_DISPLAY` names, as the renderers find it.
    pub fn from_environment() -> Self {
        Self {
            socket: None,
            session: None,
        }
    }

    /// The compositor listening on `socket`.
    pub fn at(socket: PathBuf) -> Self {
        Self {
            socket: Some(socket),
            session: None,
        }
    }

    /// Whether a connection is open, for tests that a question reuses it.
    pub fn connected(&self) -> bool {
        self.session.is_some()
    }

    fn socket(&self) -> Result<PathBuf, String> {
        match &self.socket {
            Some(socket) => Ok(socket.clone()),
            None => environment_socket(
                std::env::var_os("WAYLAND_DISPLAY"),
                std::env::var_os("XDG_RUNTIME_DIR"),
            ),
        }
    }

    fn snapshot(&mut self) -> Result<Vec<Output>, String> {
        let Some(session) = &self.session else {
            return Err("no Wayland connection is open".into());
        };
        let snapshot = session.state.snapshot();
        if session.state.too_many {
            // Counted past the tracking ceiling: start over with a fresh
            // inventory next time rather than trusting a partial one.
            self.session = None;
        }
        snapshot
    }
}

impl OutputSource for WaylandOutputs {
    fn outputs(&mut self) -> Result<Vec<Output>, String> {
        let deadline = Instant::now() + DISCOVERY_TIMEOUT;
        if let Some(session) = self.session.as_mut() {
            if session.refresh(deadline).is_ok() {
                return self.snapshot();
            }
            // The compositor went away or restarted. One new connection,
            // within the same deadline.
            self.session = None;
        }
        let socket = self.socket()?;
        self.session = Some(Session::open(&socket, deadline)?);
        self.snapshot()
    }
}

struct Session {
    connection: Connection,
    queue: EventQueue<State>,
    _registry: wl_registry::WlRegistry,
    state: State,
}

impl Session {
    fn open(socket: &Path, deadline: Instant) -> Result<Self, String> {
        let stream = UnixStream::connect(socket).map_err(|error| {
            format!(
                "cannot connect to the Wayland compositor at {}: {error}",
                socket.display()
            )
        })?;
        let connection = Connection::from_socket(stream).map_err(|error| {
            format!(
                "cannot use the Wayland connection at {}: {error}",
                socket.display()
            )
        })?;
        let queue: EventQueue<State> = connection.new_event_queue();
        let registry = connection.display().get_registry(&queue.handle(), ());
        let mut session = Self {
            connection,
            queue,
            _registry: registry,
            state: State::default(),
        };
        session.refresh(deadline)?;
        Ok(session)
    }

    /// Bring the snapshot up to date: as many round trips as it takes for
    /// what was bound to report itself, within `deadline`.
    fn refresh(&mut self, deadline: Instant) -> Result<(), String> {
        for _ in 0..MAX_ROUND_TRIPS {
            self.state.bound = false;
            self.round_trip(deadline)?;
            if !self.state.bound {
                break;
            }
        }
        Ok(())
    }

    fn round_trip(&mut self, deadline: Instant) -> Result<(), String> {
        self.state.synced = false;
        self.connection.display().sync(&self.queue.handle(), ());
        loop {
            self.queue
                .dispatch_pending(&mut self.state)
                .map_err(|error| format!("the Wayland connection failed: {error}"))?;
            if self.state.synced {
                return Ok(());
            }
            match self.queue.flush() {
                Ok(()) => {}
                Err(WaylandError::Io(error)) if error.kind() == ErrorKind::WouldBlock => {}
                Err(error) => return Err(format!("the Wayland connection failed: {error}")),
            }
            let Some(guard) = self.queue.prepare_read() else {
                continue;
            };
            wait_readable(guard.connection_fd().as_raw_fd(), deadline)?;
            match guard.read() {
                Ok(_) => {}
                Err(WaylandError::Io(error)) if error.kind() == ErrorKind::WouldBlock => {}
                Err(error) => return Err(format!("the Wayland connection failed: {error}")),
            }
        }
    }
}

/// Wait until the compositor has written something, in short slices, so
/// that neither the deadline nor a shutdown is ever overslept.
fn wait_readable(fd: RawFd, deadline: Instant) -> Result<(), String> {
    loop {
        if IPC_ABANDONED.load(Ordering::Relaxed) {
            return Err("output discovery was abandoned for shutdown".into());
        }
        let now = Instant::now();
        if now >= deadline {
            return Err(format!(
                "the Wayland compositor did not answer within {} ms",
                DISCOVERY_TIMEOUT.as_millis()
            ));
        }
        let slice = (deadline - now).min(WAIT_SLICE);
        let timeout = i32::try_from(slice.as_millis()).unwrap_or(i32::MAX).max(1);
        let mut descriptor = libc::pollfd {
            fd,
            events: libc::POLLIN,
            revents: 0,
        };
        // SAFETY: one valid pollfd for an open descriptor this call borrows.
        let ready = unsafe { libc::poll(&mut descriptor, 1, timeout) };
        if ready > 0 {
            // Readable, or hung up: either way the read reports it.
            return Ok(());
        }
        if ready < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() != ErrorKind::Interrupted {
                return Err(format!("cannot wait for the Wayland compositor: {error}"));
            }
        }
    }
}

/// What one `wl_output` said, applied atomically on its `done`.
#[derive(Clone, Debug, Default)]
struct OutputState {
    name: String,
    make: String,
    model: String,
    x: i32,
    y: i32,
    transform: u32,
    physical_mm: (i32, i32),
    mode: Option<(i32, i32, i32)>,
    scale_factor: i32,
}

/// What xdg-output said about one `wl_output`.
#[derive(Debug, Default)]
struct Logical {
    name: String,
    position: Option<(i32, i32)>,
    size: Option<(i32, i32)>,
}

struct OutputGlobal {
    global: u32,
    output: wl_output::WlOutput,
    xdg: Option<zxdg_output_v1::ZxdgOutputV1>,
    pending: OutputState,
    current: Option<OutputState>,
    logical: Logical,
}

struct Head {
    head: zwlr_output_head_v1::ZwlrOutputHeadV1,
    name: String,
    make: String,
    model: String,
    enabled: bool,
    position: (i32, i32),
    scale: f64,
    physical_mm: (i32, i32),
    current_mode: Option<zwlr_output_mode_v1::ZwlrOutputModeV1>,
}

#[derive(Debug, Default)]
struct ModeState {
    size: (i32, i32),
    refresh: i32,
}

#[derive(Default)]
struct State {
    outputs: Vec<OutputGlobal>,
    xdg_manager: Option<zxdg_output_manager_v1::ZxdgOutputManagerV1>,
    wlr_manager: Option<zwlr_output_manager_v1::ZwlrOutputManagerV1>,
    heads: Vec<Head>,
    /// The wlr manager has sent `done` at least once, so its heads are a
    /// complete set.
    heads_complete: bool,
    synced: bool,
    /// Something was bound during this round trip; what it reports arrives
    /// after the sync that ended it.
    bound: bool,
    too_many: bool,
}

impl State {
    fn add_output(
        &mut self,
        registry: &wl_registry::WlRegistry,
        global: u32,
        version: u32,
        queue: &QueueHandle<Self>,
    ) {
        if self.outputs.len() >= MAX_TRACKED {
            self.too_many = true;
            return;
        }
        let output: wl_output::WlOutput =
            registry.bind(global, version.min(WL_OUTPUT_VERSION), queue, global);
        let xdg = self
            .xdg_manager
            .as_ref()
            .map(|manager| manager.get_xdg_output(&output, queue, global));
        self.outputs.push(OutputGlobal {
            global,
            output,
            xdg,
            pending: OutputState::default(),
            current: None,
            logical: Logical::default(),
        });
        self.bound = true;
    }

    fn remove_output(&mut self, global: u32) {
        let Some(index) = self
            .outputs
            .iter()
            .position(|output| output.global == global)
        else {
            return;
        };
        let removed = self.outputs.swap_remove(index);
        if let Some(xdg) = removed.xdg {
            xdg.destroy();
        }
        if removed.output.version() >= RELEASE_VERSION {
            removed.output.release();
        }
    }

    fn snapshot(&self) -> Result<Vec<Output>, String> {
        if self.too_many {
            return Err(format!(
                "the compositor reported more than the supported {MAX_OUTPUTS} outputs"
            ));
        }
        let heads: &[Head] = if self.heads_complete && self.wlr_manager.is_some() {
            &self.heads
        } else {
            &[]
        };
        let mut found: Vec<Output> = Vec::new();
        for entry in &self.outputs {
            let Some(current) = &entry.current else {
                continue;
            };
            let name = if current.name.trim().is_empty() {
                entry.logical.name.trim()
            } else {
                current.name.trim()
            };
            if !usable_output_name(name) || found.iter().any(|known| known.name == name) {
                continue;
            }
            let head = heads.iter().find(|head| head.name == name);
            found.push(enabled_output(name, current, &entry.logical, head));
        }
        if found.len() > MAX_OUTPUTS {
            return Err(format!(
                "the compositor reported more than the supported {MAX_OUTPUTS} outputs"
            ));
        }
        for head in heads {
            if found.len() >= MAX_OUTPUTS {
                break;
            }
            let name = head.name.trim();
            if head.enabled
                || !usable_output_name(name)
                || found.iter().any(|known| known.name == name)
            {
                continue;
            }
            found.push(disabled_output(name, head));
        }
        found.sort_by(|left, right| left.name.cmp(&right.name));
        Ok(found)
    }
}

fn enabled_output(
    name: &str,
    current: &OutputState,
    logical: &Logical,
    head: Option<&Head>,
) -> Output {
    let (mode_width, mode_height, refresh) = current.mode.unwrap_or((0, 0, 0));
    let (width, height) = logical.size.unwrap_or((0, 0));
    let (x, y) = logical
        .position
        .or_else(|| head.map(|head| head.position))
        .unwrap_or((current.x, current.y));
    let described = |from_head: Option<&str>, from_output: &str| {
        from_head
            .filter(|text| !text.is_empty())
            .unwrap_or(from_output)
            .to_string()
    };
    let (physical_width, physical_height) = match head {
        Some(head) if head.physical_mm.0 > 0 && head.physical_mm.1 > 0 => head.physical_mm,
        _ => current.physical_mm,
    };
    Output {
        name: name.to_string(),
        enabled: true,
        make: described(head.map(|head| head.make.as_str()), &current.make),
        model: described(head.map(|head| head.model.as_str()), &current.model),
        x,
        y,
        width: dimension(width),
        height: dimension(height),
        scale: scale(current, logical, head),
        mode_width: dimension(mode_width),
        mode_height: dimension(mode_height),
        refresh_millihz: refresh_millihz(refresh),
        physical_width_mm: dimension(physical_width),
        physical_height_mm: dimension(physical_height),
    }
}

fn disabled_output(name: &str, head: &Head) -> Output {
    let (mode_width, mode_height, refresh) = head
        .current_mode
        .as_ref()
        .and_then(|mode| mode.data::<Mutex<ModeState>>())
        .and_then(|mode| {
            mode.lock()
                .ok()
                .map(|mode| (mode.size.0, mode.size.1, mode.refresh))
        })
        .unwrap_or((0, 0, 0));
    Output {
        name: name.to_string(),
        enabled: false,
        make: head.make.clone(),
        model: head.model.clone(),
        x: head.position.0,
        y: head.position.1,
        width: 0,
        height: 0,
        scale: if head.scale.is_finite() && head.scale > 0.0 {
            head.scale
        } else {
            1.0
        },
        mode_width: dimension(mode_width),
        mode_height: dimension(mode_height),
        refresh_millihz: refresh_millihz(refresh),
        physical_width_mm: dimension(head.physical_mm.0),
        physical_height_mm: dimension(head.physical_mm.1),
    }
}

/// The scale a compositor applies: wlr-output-management's when it says,
/// else the ratio of the mode to the logical size (to the 1/120 steps of
/// fractional scaling), else `wl_output`'s whole-number buffer scale.
fn scale(current: &OutputState, logical: &Logical, head: Option<&Head>) -> f64 {
    if let Some(head) = head
        && head.scale.is_finite()
        && head.scale > 0.0
    {
        return head.scale;
    }
    let (mode_width, mode_height, _) = current.mode.unwrap_or((0, 0, 0));
    // An odd transform turns the output a quarter: its logical width is the
    // mode's height.
    let across = if (current.transform & 1) == 1 {
        mode_height
    } else {
        mode_width
    };
    if let Some((width, _)) = logical.size
        && width > 0
        && across > 0
    {
        return (f64::from(across) / f64::from(width) * 120.0).round() / 120.0;
    }
    f64::from(current.scale_factor.max(1))
}

fn dimension(value: i32) -> u32 {
    u32::try_from(value).unwrap_or(0)
}

fn refresh_millihz(value: i32) -> Option<u64> {
    u64::try_from(value)
        .ok()
        .filter(|refresh| *refresh > 0 && *refresh <= MAX_REFRESH_MILLIHZ)
}

/// A description the compositor sent, without control characters and cut to
/// `MAX_LABEL_BYTES` on a character boundary.
fn label(text: String) -> String {
    let mut cleaned: String = text
        .chars()
        .filter(|character| !character.is_control())
        .collect();
    if cleaned.len() > MAX_LABEL_BYTES {
        let mut end = MAX_LABEL_BYTES;
        while !cleaned.is_char_boundary(end) {
            end -= 1;
        }
        cleaned.truncate(end);
    }
    cleaned.trim().to_string()
}

/// A connector name as sent, cut short when it is already too long to use:
/// `usable_output_name` then refuses it, and nothing unbounded is kept.
fn connector(text: String) -> String {
    if text.len() > MAX_OUTPUT_NAME_BYTES + 1 {
        let mut end = MAX_OUTPUT_NAME_BYTES + 1;
        while !text.is_char_boundary(end) {
            end -= 1;
        }
        return text[..end].to_string();
    }
    text
}

impl Dispatch<wl_registry::WlRegistry, ()> for State {
    fn event(
        state: &mut Self,
        registry: &wl_registry::WlRegistry,
        event: wl_registry::Event,
        _: &(),
        _: &Connection,
        queue: &QueueHandle<Self>,
    ) {
        match event {
            wl_registry::Event::Global {
                name,
                interface,
                version,
            } => match interface.as_str() {
                "wl_output" => state.add_output(registry, name, version, queue),
                "zxdg_output_manager_v1" if state.xdg_manager.is_none() => {
                    let manager: zxdg_output_manager_v1::ZxdgOutputManagerV1 =
                        registry.bind(name, version.min(XDG_OUTPUT_MANAGER_VERSION), queue, ());
                    for output in &mut state.outputs {
                        output.xdg =
                            Some(manager.get_xdg_output(&output.output, queue, output.global));
                    }
                    state.xdg_manager = Some(manager);
                    state.bound = true;
                }
                "zwlr_output_manager_v1" if state.wlr_manager.is_none() => {
                    state.wlr_manager = Some(registry.bind(
                        name,
                        version.min(WLR_OUTPUT_MANAGER_VERSION),
                        queue,
                        (),
                    ));
                    state.bound = true;
                }
                _ => {}
            },
            wl_registry::Event::GlobalRemove { name } => state.remove_output(name),
            _ => {}
        }
    }
}

impl Dispatch<wl_callback::WlCallback, ()> for State {
    fn event(
        state: &mut Self,
        _: &wl_callback::WlCallback,
        event: wl_callback::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        if let wl_callback::Event::Done { .. } = event {
            state.synced = true;
        }
    }
}

impl Dispatch<wl_output::WlOutput, u32> for State {
    fn event(
        state: &mut Self,
        output: &wl_output::WlOutput,
        event: wl_output::Event,
        global: &u32,
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        let Some(entry) = state
            .outputs
            .iter_mut()
            .find(|entry| entry.global == *global)
        else {
            return;
        };
        match event {
            wl_output::Event::Geometry {
                x,
                y,
                physical_width,
                physical_height,
                make,
                model,
                transform,
                ..
            } => {
                entry.pending.x = x;
                entry.pending.y = y;
                entry.pending.physical_mm = (physical_width, physical_height);
                entry.pending.make = label(make);
                entry.pending.model = label(model);
                entry.pending.transform = u32::from(transform);
            }
            wl_output::Event::Mode {
                flags,
                width,
                height,
                refresh,
            } => {
                if let WEnum::Value(flags) = flags
                    && flags.contains(wl_output::Mode::Current)
                {
                    entry.pending.mode = Some((width, height, refresh));
                }
            }
            wl_output::Event::Scale { factor } => entry.pending.scale_factor = factor,
            wl_output::Event::Name { name } => entry.pending.name = connector(name),
            wl_output::Event::Done => entry.current = Some(entry.pending.clone()),
            _ => {}
        }
        // Version 1 has no `done`: every event stands on its own.
        if output.version() < 2 {
            entry.current = Some(entry.pending.clone());
        }
    }
}

impl Dispatch<zxdg_output_manager_v1::ZxdgOutputManagerV1, ()> for State {
    fn event(
        _: &mut Self,
        _: &zxdg_output_manager_v1::ZxdgOutputManagerV1,
        _: zxdg_output_manager_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
    }
}

impl Dispatch<zxdg_output_v1::ZxdgOutputV1, u32> for State {
    fn event(
        state: &mut Self,
        _: &zxdg_output_v1::ZxdgOutputV1,
        event: zxdg_output_v1::Event,
        global: &u32,
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        let Some(entry) = state
            .outputs
            .iter_mut()
            .find(|entry| entry.global == *global)
        else {
            return;
        };
        match event {
            zxdg_output_v1::Event::LogicalPosition { x, y } => {
                entry.logical.position = Some((x, y));
            }
            zxdg_output_v1::Event::LogicalSize { width, height } => {
                entry.logical.size = Some((width, height));
            }
            zxdg_output_v1::Event::Name { name } => entry.logical.name = connector(name),
            _ => {}
        }
    }
}

impl Dispatch<zwlr_output_manager_v1::ZwlrOutputManagerV1, ()> for State {
    fn event(
        state: &mut Self,
        _: &zwlr_output_manager_v1::ZwlrOutputManagerV1,
        event: zwlr_output_manager_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        match event {
            zwlr_output_manager_v1::Event::Head { head } => {
                if state.heads.len() >= MAX_TRACKED {
                    // Listing switched-off heads is a courtesy; past the
                    // ceiling they are let go rather than kept.
                    if head.version() >= RELEASE_VERSION {
                        head.release();
                    }
                    return;
                }
                state.heads.push(Head {
                    head,
                    name: String::new(),
                    make: String::new(),
                    model: String::new(),
                    enabled: false,
                    position: (0, 0),
                    scale: 0.0,
                    physical_mm: (0, 0),
                    current_mode: None,
                });
            }
            zwlr_output_manager_v1::Event::Done { .. } => state.heads_complete = true,
            zwlr_output_manager_v1::Event::Finished => {
                state.wlr_manager = None;
                state.heads.clear();
                state.heads_complete = false;
            }
            _ => {}
        }
    }

    wayland_client::event_created_child!(State, zwlr_output_manager_v1::ZwlrOutputManagerV1, [
        zwlr_output_manager_v1::EVT_HEAD_OPCODE => (zwlr_output_head_v1::ZwlrOutputHeadV1, ()),
    ]);
}

impl Dispatch<zwlr_output_head_v1::ZwlrOutputHeadV1, ()> for State {
    fn event(
        state: &mut Self,
        proxy: &zwlr_output_head_v1::ZwlrOutputHeadV1,
        event: zwlr_output_head_v1::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        if let zwlr_output_head_v1::Event::Finished = event {
            state.heads.retain(|head| head.head != *proxy);
            if proxy.version() >= RELEASE_VERSION {
                proxy.release();
            }
            return;
        }
        let Some(head) = state.heads.iter_mut().find(|head| head.head == *proxy) else {
            return;
        };
        match event {
            zwlr_output_head_v1::Event::Name { name } => head.name = connector(name),
            zwlr_output_head_v1::Event::PhysicalSize { width, height } => {
                head.physical_mm = (width, height);
            }
            zwlr_output_head_v1::Event::Enabled { enabled } => head.enabled = enabled != 0,
            zwlr_output_head_v1::Event::CurrentMode { mode } => head.current_mode = Some(mode),
            zwlr_output_head_v1::Event::Position { x, y } => head.position = (x, y),
            zwlr_output_head_v1::Event::Scale { scale } => head.scale = scale,
            zwlr_output_head_v1::Event::Make { make } => head.make = label(make),
            zwlr_output_head_v1::Event::Model { model } => head.model = label(model),
            _ => {}
        }
    }

    wayland_client::event_created_child!(State, zwlr_output_head_v1::ZwlrOutputHeadV1, [
        zwlr_output_head_v1::EVT_MODE_OPCODE => (
            zwlr_output_mode_v1::ZwlrOutputModeV1,
            Mutex::new(ModeState::default())
        ),
    ]);
}

impl Dispatch<zwlr_output_mode_v1::ZwlrOutputModeV1, Mutex<ModeState>> for State {
    fn event(
        _: &mut Self,
        proxy: &zwlr_output_mode_v1::ZwlrOutputModeV1,
        event: zwlr_output_mode_v1::Event,
        data: &Mutex<ModeState>,
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        match event {
            zwlr_output_mode_v1::Event::Size { width, height } => {
                if let Ok(mut mode) = data.lock() {
                    mode.size = (width, height);
                }
            }
            zwlr_output_mode_v1::Event::Refresh { refresh } => {
                if let Ok(mut mode) = data.lock() {
                    mode.refresh = refresh;
                }
            }
            zwlr_output_mode_v1::Event::Finished => {
                if proxy.version() >= RELEASE_VERSION {
                    proxy.release();
                }
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_absolute_wayland_display_is_the_socket_itself() {
        assert_eq!(
            environment_socket(Some("/tmp/wio/wayland-0".into()), None).unwrap(),
            PathBuf::from("/tmp/wio/wayland-0")
        );
    }

    #[test]
    fn a_relative_wayland_display_is_found_in_the_runtime_directory() {
        assert_eq!(
            environment_socket(Some("wayland-1".into()), Some("/run/user/1000".into())).unwrap(),
            PathBuf::from("/run/user/1000/wayland-1")
        );
    }

    #[test]
    fn a_missing_or_relative_location_is_a_readiness_error_not_a_guess() {
        for (display, runtime) in [
            (None, Some("/run/user/1000")),
            (Some(""), Some("/run/user/1000")),
            (Some("wayland-1"), None),
            (Some("wayland-1"), Some("relative/run")),
        ] {
            let error =
                environment_socket(display.map(OsString::from), runtime.map(OsString::from))
                    .unwrap_err();
            assert!(
                error.starts_with("cannot connect to the Wayland compositor"),
                "{error}"
            );
        }
    }

    #[test]
    fn compositor_descriptions_are_bounded_and_printable() {
        assert_eq!(label(" BOE\t0x0A9B\n".into()), "BOE0x0A9B");
        let long = "é".repeat(MAX_LABEL_BYTES);
        let cut = label(long);
        assert!(cut.len() <= MAX_LABEL_BYTES);
        assert!(cut.chars().all(|character| character == 'é'));
        let name = connector("x".repeat(10 * MAX_OUTPUT_NAME_BYTES));
        assert_eq!(name.len(), MAX_OUTPUT_NAME_BYTES + 1);
        assert!(!usable_output_name(&name));
    }

    #[test]
    fn the_scale_prefers_the_compositor_then_the_logical_ratio() {
        let current = OutputState {
            mode: Some((2560, 1600, 165_000)),
            scale_factor: 2,
            ..OutputState::default()
        };
        let logical = Logical {
            size: Some((1706, 1066)),
            ..Logical::default()
        };
        assert_eq!(scale(&current, &logical, None), 1.5);
        assert_eq!(scale(&current, &Logical::default(), None), 2.0);
        let rotated = OutputState {
            transform: 1,
            mode: Some((2560, 1440, 60_000)),
            ..OutputState::default()
        };
        let portrait = Logical {
            size: Some((1440, 2560)),
            ..Logical::default()
        };
        assert_eq!(scale(&rotated, &portrait, None), 1.0);
    }

    #[test]
    fn implausible_numbers_are_unknown_rather_than_trusted() {
        assert_eq!(dimension(-5), 0);
        assert_eq!(refresh_millihz(0), None);
        assert_eq!(refresh_millihz(-60_000), None);
        assert_eq!(refresh_millihz(2_000_000), None);
        assert_eq!(refresh_millihz(165_004), Some(165_004));
    }
}
