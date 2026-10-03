//! A SIGTERM ends a stalled mpv IPC exchange at once.
//!
//! This binary installs a process-wide SIGTERM handler and signals itself,
//! so it holds exactly one test. The handler does what the service's does:
//! one atomic store into `renderer::IPC_ABANDONED`.

use std::os::fd::{FromRawFd, OwnedFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use wall_in_one_service::renderer::{IPC_ABANDONED, mpv_ipc};

static TERM_SEEN: AtomicBool = AtomicBool::new(false);

extern "C" fn on_term(_: libc::c_int) {
    TERM_SEEN.store(true, Ordering::Relaxed);
    IPC_ABANDONED.store(true, Ordering::Relaxed);
}

/// A listener that never accepts, with its one-slot backlog already taken.
fn stalled_endpoint(path: &Path) -> (OwnedFd, UnixStream) {
    let bytes = path.as_os_str().as_bytes();
    let mut address: libc::sockaddr_un = unsafe { std::mem::zeroed() };
    assert!(bytes.len() < address.sun_path.len());
    address.sun_family = libc::AF_UNIX as libc::sa_family_t;
    for (slot, byte) in address.sun_path.iter_mut().zip(bytes) {
        *slot = *byte as libc::c_char;
    }
    let fd = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_STREAM | libc::SOCK_CLOEXEC, 0) };
    assert!(fd >= 0);
    let listener = unsafe { OwnedFd::from_raw_fd(fd) };
    let length = std::mem::size_of::<libc::sockaddr_un>() as libc::socklen_t;
    assert_eq!(
        unsafe { libc::bind(fd, (&raw const address).cast(), length) },
        0
    );
    assert_eq!(unsafe { libc::listen(fd, 0) }, 0);
    let filler = UnixStream::connect(path).unwrap();
    (listener, filler)
}

#[test]
fn sigterm_abandons_a_stalled_mpv_exchange_at_once() {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let root = std::env::temp_dir().join(format!(
        "wall-in-one-ipc-shutdown-{}-{nonce}",
        std::process::id()
    ));
    std::fs::create_dir_all(&root).unwrap();
    let socket = root.join("mpv.sock");
    let (listener, filler) = stalled_endpoint(&socket);
    unsafe {
        libc::signal(libc::SIGTERM, on_term as *const () as usize);
    }

    // A long deadline, so only the signal can end the exchange early.
    let signaller = thread::spawn(|| {
        thread::sleep(Duration::from_millis(300));
        unsafe { libc::kill(libc::getpid(), libc::SIGTERM) };
    });
    let pause = serde_json::json!(["set_property", "pause", true]);
    let started = Instant::now();
    let result = mpv_ipc(
        &socket,
        &pause,
        started + Duration::from_secs(30),
        &IPC_ABANDONED,
    );
    let elapsed = started.elapsed();
    signaller.join().unwrap();

    assert!(TERM_SEEN.load(Ordering::Relaxed));
    let error = result.unwrap_err();
    assert!(error.contains("shutting down"), "{error}");
    assert!(
        elapsed >= Duration::from_millis(300) && elapsed < Duration::from_millis(1000),
        "the exchange outlived the signal: {elapsed:?}"
    );
    // Every later exchange, for another output say, fails at once.
    let again = Instant::now();
    assert!(
        mpv_ipc(
            &socket,
            &pause,
            again + Duration::from_secs(30),
            &IPC_ABANDONED
        )
        .is_err()
    );
    assert!(again.elapsed() < Duration::from_millis(100));

    drop(filler);
    drop(listener);
    std::fs::remove_dir_all(root).unwrap();
}
