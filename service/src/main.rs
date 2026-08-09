use chrono::Local;
use std::env;
use std::fs;
use std::io::BufReader;
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
            Some("--version") => {
                println!("wall-in-one-service {}", env!("CARGO_PKG_VERSION"));
                std::process::exit(0);
            }
            Some("--help") => {
                println!("usage: wall-in-one-service [--config PATH] [--socket PATH]");
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

fn serve(stream: UnixStream, runtime: &mut Runtime<SystemDriver>) {
    let mut reader = BufReader::new(&stream);
    let response = match read_request(&mut reader) {
        Ok(request) => runtime.handle(request, Local::now().naive_local()),
        Err(error) => Response::failure(error),
    };
    let mut writer = stream;
    let _ = write_response(&mut writer, &response);
}

fn fingerprint(path: &Path) -> Option<(u64, SystemTime)> {
    let metadata = fs::metadata(path).ok()?;
    Some((metadata.len(), metadata.modified().ok()?))
}

fn run() -> Result<(), String> {
    let options = parse()?;
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
    unsafe {
        libc::signal(libc::SIGTERM, terminate as *const () as usize);
        libc::signal(libc::SIGINT, terminate as *const () as usize);
    }

    let mut known = fingerprint(&options.config);
    let mut next_config_check = Instant::now() + Duration::from_secs(1);
    while !runtime.should_quit() && !TERMINATE.load(Ordering::Relaxed) {
        loop {
            match listener.accept() {
                Ok((stream, _)) => serve(stream, &mut runtime),
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
                if response.ok {
                    known = current;
                } else {
                    eprintln!("wall-in-one-service: reload: {}", response.message);
                }
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
