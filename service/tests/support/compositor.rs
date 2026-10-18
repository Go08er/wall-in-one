//! A Wayland compositor that is nothing but a display inventory, for tests.
//!
//! It speaks the wire protocol for exactly what a client asking "which
//! outputs are there?" uses: `wl_display`, `wl_registry`, `wl_callback`,
//! `wl_output`, `zxdg_output_manager_v1` and, when the layout has heads,
//! `zwlr_output_manager_v1` with its heads and modes. It listens on a socket
//! of the test's own, never a desktop's, and changes its layout when told to,
//! the way a compositor announces a monitor being plugged or unplugged.

use std::collections::HashMap;
use std::io::{ErrorKind, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

const DISPLAY: u32 = 1;
const XDG_GLOBAL: u32 = 1;
const WLR_GLOBAL: u32 = 2;
const FIRST_OUTPUT_GLOBAL: u32 = 100;
const FIRST_SERVER_ID: u32 = 0xff00_0000;

#[derive(Clone, Debug)]
pub struct FakeOutput {
    pub name: String,
    pub make: String,
    pub model: String,
    /// Width, height and refresh in mHz of the current mode.
    pub mode: (i32, i32, i32),
    pub scale: i32,
    pub physical_mm: (i32, i32),
    /// Logical x, y, width and height, as xdg-output reports them.
    pub logical: (i32, i32, i32, i32),
}

impl FakeOutput {
    pub fn new(name: &str, mode: (i32, i32, i32), logical: (i32, i32, i32, i32)) -> Self {
        Self {
            name: name.into(),
            make: "Make".into(),
            model: "Model".into(),
            mode,
            scale: 1,
            physical_mm: (600, 340),
            logical,
        }
    }
}

#[derive(Clone, Debug)]
pub struct FakeHead {
    pub name: String,
    pub make: String,
    pub model: String,
    pub enabled: bool,
    pub mode: (i32, i32, i32),
    pub position: (i32, i32),
    pub scale: f64,
    pub physical_mm: (i32, i32),
    /// Modes announced beyond the current one, as a compositor flooding the
    /// client would.
    pub extra_modes: usize,
}

impl FakeHead {
    pub fn new(name: &str, enabled: bool) -> Self {
        Self {
            name: name.into(),
            make: "Head make".into(),
            model: "Head model".into(),
            enabled,
            mode: (1920, 1080, 60_000),
            position: (0, 0),
            scale: 1.0,
            physical_mm: (530, 300),
            extra_modes: 0,
        }
    }
}

#[derive(Clone, Debug)]
pub struct Layout {
    pub outputs: Vec<FakeOutput>,
    /// `None`: no wlr-output-management global at all. Set to `None` once a
    /// client has the manager, the manager is finished.
    pub heads: Option<Vec<FakeHead>>,
    /// The zwlr_output_manager_v1 version advertised.
    pub wlr_version: u32,
    pub wl_output_version: u32,
    /// `None`: no xdg-output global at all.
    pub xdg_output_version: Option<u32>,
}

impl Layout {
    /// Outputs over `wl_output` 4 and xdg-output 3, without wlr-output-management.
    pub fn of(outputs: Vec<FakeOutput>) -> Self {
        Self {
            outputs,
            heads: None,
            wlr_version: 4,
            wl_output_version: 4,
            xdg_output_version: Some(3),
        }
    }
}

struct Shared {
    layout: Layout,
    generation: u64,
    connections: usize,
    silent: bool,
    released_heads: usize,
}

pub struct FakeCompositor {
    path: PathBuf,
    shared: Arc<Mutex<Shared>>,
    stop: Arc<AtomicBool>,
    listener: Option<JoinHandle<()>>,
    clients: Arc<Mutex<Vec<JoinHandle<()>>>>,
}

impl FakeCompositor {
    /// Listen on `path` (which must not exist yet) with `layout`.
    pub fn start(path: &Path, layout: Layout) -> Self {
        Self::launch(path, layout, false)
    }

    /// Listen on `path` and accept connections, but never answer anything.
    pub fn start_silent(path: &Path) -> Self {
        Self::launch(path, Layout::of(Vec::new()), true)
    }

    fn launch(path: &Path, layout: Layout, silent: bool) -> Self {
        let listener = UnixListener::bind(path).unwrap();
        listener.set_nonblocking(true).unwrap();
        let shared = Arc::new(Mutex::new(Shared {
            layout,
            generation: 0,
            connections: 0,
            silent,
            released_heads: 0,
        }));
        let stop = Arc::new(AtomicBool::new(false));
        let clients = Arc::new(Mutex::new(Vec::new()));
        let listener = {
            let shared = Arc::clone(&shared);
            let stop = Arc::clone(&stop);
            let clients = Arc::clone(&clients);
            thread::spawn(move || {
                while !stop.load(Ordering::SeqCst) {
                    match listener.accept() {
                        Ok((stream, _)) => {
                            shared.lock().unwrap().connections += 1;
                            let shared = Arc::clone(&shared);
                            let stop = Arc::clone(&stop);
                            clients
                                .lock()
                                .unwrap()
                                .push(thread::spawn(move || serve(stream, &shared, &stop)));
                        }
                        Err(error) if error.kind() == ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(5));
                        }
                        Err(_) => break,
                    }
                }
            })
        };
        Self {
            path: path.to_path_buf(),
            shared,
            stop,
            listener: Some(listener),
            clients,
        }
    }

    /// Replace the layout. Every connected client is told what changed.
    pub fn set_layout(&self, layout: Layout) {
        let mut shared = self.shared.lock().unwrap();
        shared.layout = layout;
        shared.generation += 1;
    }

    /// How many connections were accepted so far.
    pub fn connections(&self) -> usize {
        self.shared.lock().unwrap().connections
    }

    /// How many wlr heads clients have released.
    pub fn released_heads(&self) -> usize {
        self.shared.lock().unwrap().released_heads
    }
}

impl Drop for FakeCompositor {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(listener) = self.listener.take() {
            listener.join().unwrap();
        }
        for client in self.clients.lock().unwrap().drain(..) {
            client.join().unwrap();
        }
        let _ = std::fs::remove_file(&self.path);
    }
}

/// A compositor that has stopped: its socket listens but never accepts.
///
/// The listen queue holds one connection (backlog 0), as a libwayland
/// compositor's holds 129, so the second connection finds it full, which is
/// where a blocking connect would wait for as long as the compositor stays
/// stopped.
pub struct WedgedCompositor {
    path: PathBuf,
    descriptor: std::os::fd::OwnedFd,
}

impl WedgedCompositor {
    pub fn start(path: &Path) -> Self {
        use std::os::fd::FromRawFd;
        use std::os::unix::ffi::OsStrExt;
        // SAFETY: a fresh socket, bound to a path of the test's own and
        // owned at once; the address is zeroed and filled within bounds.
        unsafe {
            let fd = libc::socket(libc::AF_UNIX, libc::SOCK_STREAM | libc::SOCK_CLOEXEC, 0);
            assert!(fd >= 0, "socket");
            let descriptor = std::os::fd::OwnedFd::from_raw_fd(fd);
            let mut address: libc::sockaddr_un = std::mem::zeroed();
            address.sun_family = libc::AF_UNIX as libc::sa_family_t;
            let bytes = path.as_os_str().as_bytes();
            assert!(bytes.len() < address.sun_path.len(), "socket path too long");
            for (slot, byte) in address.sun_path.iter_mut().zip(bytes) {
                *slot = *byte as libc::c_char;
            }
            let length = std::mem::size_of::<libc::sockaddr_un>() as libc::socklen_t;
            assert_eq!(
                libc::bind(fd, (&raw const address).cast(), length),
                0,
                "bind"
            );
            assert_eq!(libc::listen(fd, 0), 0, "listen");
            Self {
                path: path.to_path_buf(),
                descriptor,
            }
        }
    }
}

impl Drop for WedgedCompositor {
    fn drop(&mut self) {
        // The descriptor closes with the struct; the path goes with it.
        let _ = std::fs::remove_file(&self.path);
    }
}

#[derive(Clone, Debug)]
enum Object {
    Registry,
    Output { name: String },
    XdgManager { version: u32 },
    XdgOutput,
    WlrManager,
    Head,
    Mode,
}

#[derive(Default)]
struct Client {
    objects: HashMap<u32, Object>,
    registries: Vec<u32>,
    /// Output globals this client was told about: number and connector.
    advertised: Vec<(u32, String)>,
    next_global: u32,
    next_server_id: u32,
    wlr_manager: Option<u32>,
    /// Heads announced to this client: connector, head id, enabled.
    heads: Vec<(String, u32, bool)>,
    serial: u32,
    generation: u64,
    released_heads: usize,
}

fn serve(mut stream: UnixStream, shared: &Mutex<Shared>, stop: &AtomicBool) {
    stream
        .set_read_timeout(Some(Duration::from_millis(10)))
        .unwrap();
    let mut client = Client {
        next_global: FIRST_OUTPUT_GLOBAL,
        next_server_id: FIRST_SERVER_ID,
        ..Client::default()
    };
    let mut incoming = Vec::new();
    let mut chunk = [0_u8; 4096];
    while !stop.load(Ordering::SeqCst) {
        match stream.read(&mut chunk) {
            Ok(0) => return,
            Ok(read) => incoming.extend_from_slice(&chunk[..read]),
            Err(error) if matches!(error.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {}
            Err(_) => return,
        }
        // Looked at after the read: a layout set before a request was sent
        // is announced before that request is answered, as a compositor
        // that had already seen the monitor would.
        let mut out = Vec::new();
        let (silent, layout, generation) = {
            let shared = shared.lock().unwrap();
            (shared.silent, shared.layout.clone(), shared.generation)
        };
        if !silent && generation != client.generation {
            client.generation = generation;
            client.announce_changes(&layout, &mut out);
        }
        while incoming.len() >= 8 {
            let size = (u32::from_le_bytes(incoming[4..8].try_into().unwrap()) >> 16) as usize;
            if size < 8 || incoming.len() < size {
                break;
            }
            let message: Vec<u8> = incoming.drain(..size).collect();
            if !silent {
                client.handle(&message, &layout, &mut out);
            }
        }
        if client.released_heads > 0 {
            shared.lock().unwrap().released_heads += client.released_heads;
            client.released_heads = 0;
        }
        if !out.is_empty() && stream.write_all(&out).is_err() {
            return;
        }
    }
}

impl Client {
    fn server_id(&mut self) -> u32 {
        let id = self.next_server_id;
        self.next_server_id += 1;
        id
    }

    fn handle(&mut self, message: &[u8], layout: &Layout, out: &mut Vec<u8>) {
        let object = word(message, 0);
        let opcode = (word(message, 4) & 0xffff) as u16;
        let body = &message[8..];
        if object == DISPLAY {
            match opcode {
                0 => {
                    let callback = word(body, 0);
                    self.serial += 1;
                    event(out, callback, 0, &[Arg::Uint(self.serial)]);
                    event(out, DISPLAY, 1, &[Arg::Uint(callback)]);
                }
                1 => {
                    let registry = word(body, 0);
                    self.objects.insert(registry, Object::Registry);
                    self.registries.push(registry);
                    self.advertise_all(registry, layout, out);
                }
                _ => {}
            }
            return;
        }
        let Some(target) = self.objects.get(&object).cloned() else {
            return;
        };
        match (target, opcode) {
            (Object::Registry, 0) => {
                let global = word(body, 0);
                let (_, after_interface) = string(body, 4);
                let version = word(body, after_interface);
                let id = word(body, after_interface + 4);
                self.bind(global, version, id, layout, out);
            }
            (Object::XdgManager { version }, 1) => {
                let id = word(body, 0);
                let output = word(body, 4);
                let Some(Object::Output { name }) = self.objects.get(&output).cloned() else {
                    return;
                };
                self.objects.insert(id, Object::XdgOutput);
                let Some(found) = layout
                    .outputs
                    .iter()
                    .find(|candidate| candidate.name == name)
                else {
                    return;
                };
                let (x, y, width, height) = found.logical;
                event(out, id, 0, &[Arg::Int(x), Arg::Int(y)]);
                event(out, id, 1, &[Arg::Int(width), Arg::Int(height)]);
                if version >= 2 {
                    event(out, id, 3, &[Arg::Str(&found.name)]);
                    event(out, id, 4, &[Arg::Str("A test output")]);
                }
                if version < 3 {
                    event(out, id, 2, &[]);
                } else {
                    // From version 3 the batch ends with wl_output.done.
                    event(out, output, 2, &[]);
                }
            }
            (Object::Output { .. } | Object::XdgOutput | Object::XdgManager { .. }, 0) => {
                // release / destroy
                self.objects.remove(&object);
                event(out, DISPLAY, 1, &[Arg::Uint(object)]);
            }
            (Object::Head, 0) => {
                self.objects.remove(&object);
                self.released_heads += 1;
            }
            (Object::Mode, 0) => {
                self.objects.remove(&object);
            }
            _ => {}
        }
    }

    fn advertise(
        &mut self,
        registry: u32,
        global: u32,
        interface: &str,
        version: u32,
        out: &mut Vec<u8>,
    ) {
        event(
            out,
            registry,
            0,
            &[Arg::Uint(global), Arg::Str(interface), Arg::Uint(version)],
        );
    }

    fn advertise_all(&mut self, registry: u32, layout: &Layout, out: &mut Vec<u8>) {
        // Outputs first, as a compositor that started with its monitors
        // does, so that xdg-output arrives after the outputs it describes.
        if self.advertised.is_empty() {
            for output in &layout.outputs {
                let global = self.next_global;
                self.next_global += 1;
                self.advertised.push((global, output.name.clone()));
            }
        }
        for (global, _) in self.advertised.clone() {
            self.advertise(registry, global, "wl_output", layout.wl_output_version, out);
        }
        if let Some(version) = layout.xdg_output_version {
            self.advertise(registry, XDG_GLOBAL, "zxdg_output_manager_v1", version, out);
        }
        if layout.heads.is_some() {
            self.advertise(
                registry,
                WLR_GLOBAL,
                "zwlr_output_manager_v1",
                layout.wlr_version,
                out,
            );
        }
    }

    fn bind(&mut self, global: u32, version: u32, id: u32, layout: &Layout, out: &mut Vec<u8>) {
        match global {
            XDG_GLOBAL => {
                self.objects.insert(id, Object::XdgManager { version });
            }
            WLR_GLOBAL => {
                self.objects.insert(id, Object::WlrManager);
                self.wlr_manager = Some(id);
                for head in layout.heads.clone().unwrap_or_default() {
                    self.announce_head(id, &head, out);
                }
                self.serial += 1;
                event(out, id, 1, &[Arg::Uint(self.serial)]);
            }
            _ => {
                let Some((_, name)) = self
                    .advertised
                    .iter()
                    .find(|(advertised, _)| *advertised == global)
                    .cloned()
                else {
                    return;
                };
                self.objects
                    .insert(id, Object::Output { name: name.clone() });
                let Some(output) = layout.outputs.iter().find(|output| output.name == name) else {
                    return;
                };
                event(
                    out,
                    id,
                    0,
                    &[
                        Arg::Int(output.logical.0),
                        Arg::Int(output.logical.1),
                        Arg::Int(output.physical_mm.0),
                        Arg::Int(output.physical_mm.1),
                        Arg::Int(0),
                        Arg::Str(&output.make),
                        Arg::Str(&output.model),
                        Arg::Int(0),
                    ],
                );
                let (width, height, refresh) = output.mode;
                // A mode that is not current comes first: only the current
                // one counts.
                event(
                    out,
                    id,
                    1,
                    &[Arg::Uint(0), Arg::Int(640), Arg::Int(480), Arg::Int(30_000)],
                );
                event(
                    out,
                    id,
                    1,
                    &[
                        Arg::Uint(3),
                        Arg::Int(width),
                        Arg::Int(height),
                        Arg::Int(refresh),
                    ],
                );
                if version >= 2 {
                    event(out, id, 3, &[Arg::Int(output.scale)]);
                }
                if version >= 4 {
                    event(out, id, 4, &[Arg::Str(&output.name)]);
                    event(out, id, 5, &[Arg::Str("A test output")]);
                }
                if version >= 2 {
                    event(out, id, 2, &[]);
                }
            }
        }
    }

    fn announce_head(&mut self, manager: u32, head: &FakeHead, out: &mut Vec<u8>) {
        let id = self.server_id();
        self.objects.insert(id, Object::Head);
        event(out, manager, 0, &[Arg::Uint(id)]);
        event(out, id, 0, &[Arg::Str(&head.name)]);
        event(out, id, 1, &[Arg::Str("A test head")]);
        event(
            out,
            id,
            2,
            &[Arg::Int(head.physical_mm.0), Arg::Int(head.physical_mm.1)],
        );
        let mode = self.server_id();
        self.objects.insert(mode, Object::Mode);
        event(out, id, 3, &[Arg::Uint(mode)]);
        event(
            out,
            mode,
            0,
            &[Arg::Int(head.mode.0), Arg::Int(head.mode.1)],
        );
        event(out, mode, 1, &[Arg::Int(head.mode.2)]);
        event(out, mode, 2, &[]);
        for index in 0..head.extra_modes {
            let extra = self.server_id();
            self.objects.insert(extra, Object::Mode);
            event(out, id, 3, &[Arg::Uint(extra)]);
            event(
                out,
                extra,
                0,
                &[
                    Arg::Int(640),
                    Arg::Int(i32::try_from(index).unwrap_or(i32::MAX)),
                ],
            );
        }
        event(out, id, 4, &[Arg::Int(i32::from(head.enabled))]);
        if head.enabled {
            event(out, id, 5, &[Arg::Uint(mode)]);
            event(
                out,
                id,
                6,
                &[Arg::Int(head.position.0), Arg::Int(head.position.1)],
            );
            event(out, id, 7, &[Arg::Int(0)]);
            event(out, id, 8, &[Arg::Int((head.scale * 256.0).round() as i32)]);
        }
        event(out, id, 10, &[Arg::Str(&head.make)]);
        event(out, id, 11, &[Arg::Str(&head.model)]);
        self.heads.push((head.name.clone(), id, head.enabled));
    }

    /// Tell this client what changed since it last looked: outputs plugged
    /// in or out, heads added, removed or switched.
    fn announce_changes(&mut self, layout: &Layout, out: &mut Vec<u8>) {
        if self.registries.is_empty() {
            return;
        }
        let gone: Vec<(u32, String)> = self
            .advertised
            .iter()
            .filter(|(_, name)| !layout.outputs.iter().any(|output| output.name == *name))
            .cloned()
            .collect();
        for (global, _) in &gone {
            for registry in self.registries.clone() {
                event(out, registry, 1, &[Arg::Uint(*global)]);
            }
        }
        self.advertised
            .retain(|(global, _)| !gone.iter().any(|(removed, _)| removed == global));
        for output in &layout.outputs {
            if self.advertised.iter().any(|(_, name)| *name == output.name) {
                continue;
            }
            let global = self.next_global;
            self.next_global += 1;
            self.advertised.push((global, output.name.clone()));
            for registry in self.registries.clone() {
                self.advertise(registry, global, "wl_output", layout.wl_output_version, out);
            }
        }
        let Some(manager) = self.wlr_manager else {
            return;
        };
        let Some(wanted) = layout.heads.clone() else {
            // wlr-output-management withdrawn: the manager is finished, and
            // its heads with it, with no finished event of their own.
            event(out, manager, 2, &[]);
            self.wlr_manager = None;
            self.heads.clear();
            return;
        };
        let mut changed = false;
        for (name, id, enabled) in self.heads.clone() {
            match wanted.iter().find(|head| head.name == name) {
                None => {
                    event(out, id, 9, &[]);
                    self.heads.retain(|(_, known, _)| *known != id);
                    changed = true;
                }
                Some(head) if head.enabled != enabled => {
                    // Simplest faithful change: the old head goes, a new one
                    // with the new state comes.
                    event(out, id, 9, &[]);
                    self.heads.retain(|(_, known, _)| *known != id);
                    self.announce_head(manager, head, out);
                    changed = true;
                }
                Some(_) => {}
            }
        }
        for head in &wanted {
            if !self.heads.iter().any(|(name, _, _)| *name == head.name) {
                self.announce_head(manager, head, out);
                changed = true;
            }
        }
        if changed {
            self.serial += 1;
            event(out, manager, 1, &[Arg::Uint(self.serial)]);
        }
    }
}

enum Arg<'a> {
    Uint(u32),
    Int(i32),
    Str(&'a str),
}

fn event(out: &mut Vec<u8>, object: u32, opcode: u16, arguments: &[Arg<'_>]) {
    let mut body = Vec::new();
    for argument in arguments {
        match argument {
            Arg::Uint(value) => body.extend_from_slice(&value.to_le_bytes()),
            Arg::Int(value) => body.extend_from_slice(&value.to_le_bytes()),
            Arg::Str(text) => {
                let length = text.len() + 1;
                body.extend_from_slice(&(length as u32).to_le_bytes());
                body.extend_from_slice(text.as_bytes());
                body.push(0);
                body.resize(body.len().next_multiple_of(4), 0);
            }
        }
    }
    let size = (8 + body.len()) as u32;
    out.extend_from_slice(&object.to_le_bytes());
    out.extend_from_slice(&((size << 16) | u32::from(opcode)).to_le_bytes());
    out.extend_from_slice(&body);
}

fn word(bytes: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(bytes[at..at + 4].try_into().unwrap())
}

/// A string argument at `at`, and the offset just past it.
fn string(bytes: &[u8], at: usize) -> (String, usize) {
    let length = word(bytes, at) as usize;
    let start = at + 4;
    let text =
        String::from_utf8_lossy(&bytes[start..start + length.saturating_sub(1)]).into_owned();
    (text, start + length.next_multiple_of(4))
}
