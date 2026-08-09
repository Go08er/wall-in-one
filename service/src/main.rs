use chrono::Local;
use std::env;
use std::fs;
use std::io::BufReader;
use std::os::unix::fs::MetadataExt;
use std::os::unix::fs::PermissionsExt;
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
    let _ = write_response(&mut writer, &response);
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
    if let Err(error) = runtime.apply_current() {
        eprintln!("wall-in-one-service: initial apply: {error}");
    }

    if let Some(parent) = options.socket.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("cannot create socket directory: {error}"))?;
    }
    if options.socket.exists() {
        if UnixStream::connect(&options.socket).is_ok() {
            return Err(format!(
                "another service is listening on {}",
                options.socket.display()
            ));
        }
        fs::remove_file(&options.socket)
            .map_err(|error| format!("cannot remove stale socket: {error}"))?;
    }
    let listener = UnixListener::bind(&options.socket)
        .map_err(|error| format!("cannot bind {}: {error}", options.socket.display()))?;
    fs::set_permissions(&options.socket, fs::Permissions::from_mode(0o600))
        .map_err(|error| format!("cannot secure socket: {error}"))?;
    listener
        .set_nonblocking(true)
        .map_err(|error| error.to_string())?;
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
        thread::sleep(Duration::from_millis(25));
    }
    runtime.shutdown();
    drop(listener);
    let _ = fs::remove_file(&options.socket);
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        eprintln!("wall-in-one-service: {error}");
        std::process::exit(1);
    }
}
