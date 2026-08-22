use chrono::Local;
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::BufReader;
use std::os::fd::AsRawFd;
use std::os::unix::fs::{FileTypeExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime};
use wall_in_one_service::config::Config;
use wall_in_one_service::protocol::{read_request, write_response, Request, Response};
use wall_in_one_service::renderer::SystemDriver;
use wall_in_one_service::runtime::Runtime;

static TERMINATE: AtomicBool = AtomicBool::new(false);
const STARTUP_RETRY_WINDOW: Duration = Duration::from_secs(8);
const STARTUP_RETRY_INTERVAL: Duration = Duration::from_millis(250);

struct SocketOwner {
    listener: UnixListener,
    path: PathBuf,
    identity: (u64, u64),
    _lock: File,
}

impl Drop for SocketOwner {
    fn drop(&mut self) {
        remove_owned_socket(&self.path, self.identity);
    }
}

fn remove_owned_socket(path: &Path, identity: (u64, u64)) {
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return;
    };
    if metadata.file_type().is_socket() && (metadata.dev(), metadata.ino()) == identity {
        let _ = fs::remove_file(path);
    }
}

struct StartupRetry {
    deadline: Instant,
    next_attempt: Instant,
    first_error: String,
    authoritative_generation: u64,
}

extern "C" fn terminate(_: libc::c_int) {
    TERMINATE.store(true, Ordering::Relaxed);
}

struct Options {
    config: PathBuf,
    socket: PathBuf,
    wait_for_config: bool,
}

fn xdg(variable: &str, fallback: PathBuf) -> PathBuf {
    env::var_os(variable)
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .unwrap_or(fallback)
}

fn defaults() -> Options {
    let home = env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/"));
    let state = xdg("XDG_STATE_HOME", home.join(".local/state"));
    let runtime = xdg("XDG_RUNTIME_DIR", state.join("wall-in-one"));
    Options {
        config: state.join("wall-in-one/runtime.toml"),
        socket: runtime.join("wall-in-one-runtime.sock"),
        wait_for_config: false,
    }
}

fn parse() -> Result<Options, String> {
    let mut options = defaults();
    let mut arguments = env::args_os().skip(1);
    while let Some(argument) = arguments.next() {
        match argument.to_str() {
            Some("--config") => {
                options.config = arguments
                    .next()
                    .map(PathBuf::from)
                    .ok_or("--config needs a path")?
            }
            Some("--socket") => {
                options.socket = arguments
                    .next()
                    .map(PathBuf::from)
                    .ok_or("--socket needs a path")?
            }
            Some("--wait-for-config") => options.wait_for_config = true,
            Some("--version") => {
                println!("wall-in-one-service {}", env!("CARGO_PKG_VERSION"));
                std::process::exit(0);
            }
            Some("--help") => {
                println!(
                    "usage: wall-in-one-service [--config PATH] [--socket PATH] [--wait-for-config]"
                );
                std::process::exit(0);
            }
            _ => return Err(format!("unknown argument {}", argument.to_string_lossy())),
        }
    }
    if !options.config.is_absolute() || !options.socket.is_absolute() {
        return Err("config and socket paths must be absolute".into());
    }
    Ok(options)
}

fn serve(stream: UnixStream, runtime: &mut Runtime<SystemDriver>) -> bool {
    let _ = stream.set_read_timeout(Some(Duration::from_millis(250)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(250)));
    let mut reader = BufReader::new(&stream);
    let (response, was_reload) = match read_request(&mut reader) {
        Ok(request) => {
            let was_reload = request.verb == "reload";
            (
                runtime.handle(request, Local::now().naive_local()),
                was_reload,
            )
        }
        Err(error) => (Response::failure(error), false),
    };
    let mut writer = stream;
    if let Err(error) = write_response(&mut writer, &response) {
        eprintln!("wall-in-one-service: response write failed: {error}");
    }
    was_reload
}

fn fingerprint(path: &Path) -> Option<(u64, u64, SystemTime)> {
    let metadata = fs::metadata(path).ok()?;
    Some((metadata.ino(), metadata.len(), metadata.modified().ok()?))
}

fn install_signal_handlers() {
    unsafe {
        libc::signal(libc::SIGTERM, terminate as *const () as usize);
        libc::signal(libc::SIGINT, terminate as *const () as usize);
    }
}

fn wait_for_config(path: &Path) -> Result<bool, String> {
    while !TERMINATE.load(Ordering::Relaxed) {
        match fs::metadata(path) {
            Ok(_) => return Ok(true),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                thread::sleep(Duration::from_millis(250));
            }
            Err(error) => {
                return Err(format!(
                    "cannot inspect config path {}: {error}",
                    path.display()
                ));
            }
        }
    }
    Ok(false)
}

fn lock_path(socket: &Path) -> PathBuf {
    let mut name = socket.as_os_str().to_os_string();
    name.push(".lock");
    PathBuf::from(name)
}

fn claim_socket(path: &Path) -> Result<SocketOwner, String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("socket {} has no parent directory", path.display()))?;
    fs::create_dir_all(parent)
        .map_err(|error| format!("cannot create socket directory: {error}"))?;
    let lock_path = lock_path(path);
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(&lock_path)
        .map_err(|error| {
            format!(
                "cannot open singleton lock {}: {error}",
                lock_path.display()
            )
        })?;
    if !lock
        .metadata()
        .map_err(|error| format!("cannot inspect singleton lock: {error}"))?
        .is_file()
    {
        return Err(format!(
            "refusing non-regular singleton lock {}",
            lock_path.display()
        ));
    }
    fs::set_permissions(&lock_path, fs::Permissions::from_mode(0o600))
        .map_err(|error| format!("cannot secure singleton lock: {error}"))?;
    let locked = unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if locked != 0 {
        let error = std::io::Error::last_os_error();
        if error.kind() == std::io::ErrorKind::WouldBlock {
            return Err(format!("another service owns {}", path.display()));
        }
        return Err(format!(
            "cannot lock singleton guard {}: {error}",
            lock_path.display()
        ));
    }

    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            if UnixStream::connect(path).is_ok() {
                return Err(format!(
                    "another service is listening on {}",
                    path.display()
                ));
            }
            if !metadata.file_type().is_socket() {
                return Err(format!(
                    "refusing to replace non-socket path {}",
                    path.display()
                ));
            }
            fs::remove_file(path)
                .map_err(|error| format!("cannot remove stale socket: {error}"))?;
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(format!("cannot inspect socket {}: {error}", path.display())),
    }
    let listener = UnixListener::bind(path)
        .map_err(|error| format!("cannot bind {}: {error}", path.display()))?;
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("cannot inspect newly bound socket: {error}"))?;
    let identity = (metadata.dev(), metadata.ino());
    if let Err(error) = fs::set_permissions(path, fs::Permissions::from_mode(0o600)) {
        remove_owned_socket(path, identity);
        return Err(format!("cannot secure socket: {error}"));
    }
    if let Err(error) = listener.set_nonblocking(true) {
        remove_owned_socket(path, identity);
        return Err(error.to_string());
    }
    Ok(SocketOwner {
        listener,
        path: path.to_path_buf(),
        identity,
        _lock: lock,
    })
}

fn startup_readiness_error(error: &str) -> bool {
    [
        "cannot run noctalia",
        "noctalia exited",
        "noctalia timed out",
        "cannot run niri outputs",
        "niri outputs exited",
        "niri outputs timed out",
        "niri outputs returned invalid JSON",
        "niri reported no usable outputs",
    ]
    .iter()
    .any(|marker| error.contains(marker))
}

fn initial_apply(runtime: &mut Runtime<SystemDriver>, now: Instant) -> Option<StartupRetry> {
    match runtime.apply_current() {
        Ok(_) => None,
        Err(error) if startup_readiness_error(&error) => {
            eprintln!("wall-in-one-service: initial apply waiting for desktop readiness: {error}");
            Some(StartupRetry {
                deadline: now + STARTUP_RETRY_WINDOW,
                next_attempt: now + STARTUP_RETRY_INTERVAL,
                first_error: error,
                authoritative_generation: runtime.authoritative_generation(),
            })
        }
        Err(error) => {
            eprintln!("wall-in-one-service: initial apply: {error}");
            None
        }
    }
}

fn retry_initial_apply(
    retry: &mut Option<StartupRetry>,
    runtime: &mut Runtime<SystemDriver>,
    now: Instant,
) -> Result<(), String> {
    let Some(pending) = retry.as_mut() else {
        return Ok(());
    };
    if runtime.authoritative_generation() != pending.authoritative_generation {
        *retry = None;
        return Ok(());
    }
    if now < pending.next_attempt {
        return Ok(());
    }
    if now >= pending.deadline {
        return Err(format!(
            "initial apply readiness deadline expired after {} seconds; first error: {}",
            STARTUP_RETRY_WINDOW.as_secs(),
            pending.first_error
        ));
    }
    match runtime.apply_current() {
        Ok(_) => {
            eprintln!("wall-in-one-service: desktop became ready; initial wallpaper applied");
            *retry = None;
        }
        Err(error) if startup_readiness_error(&error) && now < pending.deadline => {
            pending.next_attempt = now + STARTUP_RETRY_INTERVAL;
        }
        Err(error) => {
            if now >= pending.deadline {
                return Err(format!(
                    "initial apply readiness deadline expired after {} seconds; first error: {}; last error: {error}",
                    STARTUP_RETRY_WINDOW.as_secs(),
                    pending.first_error
                ));
            } else {
                eprintln!("wall-in-one-service: initial apply stopped retrying: {error}");
            }
            *retry = None;
        }
    }
    Ok(())
}

fn run() -> Result<(), String> {
    let options = parse()?;
    install_signal_handlers();
    if options.wait_for_config && !wait_for_config(&options.config)? {
        return Ok(());
    }
    let config = Config::load(&options.config).map_err(|error| error.to_string())?;
    let driver = SystemDriver::new(config.renderer.clone());
    let mut runtime = Runtime::new(
        options.config.clone(),
        config,
        driver,
        Local::now().naive_local(),
    )?;
    // Own and secure the public endpoint before applying anything. A losing
    // process must have exactly zero wallpaper or renderer side effects.
    let socket = claim_socket(&options.socket)?;
    let listener = &socket.listener;
    let mut startup_retry = initial_apply(&mut runtime, Instant::now());
    let mut known = fingerprint(&options.config);
    let mut next_config_check = Instant::now() + Duration::from_secs(1);
    while !runtime.should_quit() && !TERMINATE.load(Ordering::Relaxed) {
        loop {
            match listener.accept() {
                Ok((stream, _)) => {
                    // Capture before loading. If an atomic rename races this
                    // request, retaining the older fingerprint can cause one
                    // harmless extra reload; capturing afterward could mark
                    // unseen newer bytes as loaded and miss them entirely.
                    let observed = fingerprint(&options.config);
                    if serve(stream, &mut runtime) {
                        known = observed;
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(error) => eprintln!("wall-in-one-service: accept: {error}"),
            }
        }
        if runtime.should_quit() || TERMINATE.load(Ordering::Relaxed) {
            break;
        }
        let now = Instant::now();
        if now >= next_config_check {
            let current = fingerprint(&options.config);
            if current.is_some() && current != known {
                let response = runtime.handle(
                    Request {
                        verb: "reload".into(),
                        argument: None,
                    },
                    Local::now().naive_local(),
                );
                if !response.ok {
                    eprintln!("wall-in-one-service: reload: {}", response.message);
                }
                // A broken generation is reported once. The next atomic
                // rename changes the inode and gets another attempt; polling
                // the same bad bytes forever would only spam the journal.
                known = current;
            }
            next_config_check = now + Duration::from_secs(1);
        }
        runtime.tick(Local::now().naive_local(), now);
        retry_initial_apply(&mut startup_retry, &mut runtime, now)?;
        thread::sleep(Duration::from_millis(25));
    }
    runtime.shutdown();
    drop(socket);
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("wall-in-one-service: {error}");
        std::process::exit(1);
    }
}
