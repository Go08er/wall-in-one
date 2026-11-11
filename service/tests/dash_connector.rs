//! A 0.4.1-compiled `runtime.toml` that names a display or theme source
//! starting with '-' loads, and the service starts (review-f3 M1).
//!
//! 0.4.1 wrote such a name without a check. Refusing the document made
//! `--check-config` exit 78, and `RestartPreventExitStatus=78` then kept the
//! unit down for every display. The name is data; only where it would become
//! a program's argument is it refused (`renderer.rs`, per display).

use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

fn directory(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let path =
        std::env::temp_dir().join(format!("wio-dash-{label}-{}-{nonce}", std::process::id()));
    fs::create_dir_all(&path).unwrap();
    path
}

fn fixture(name: &str) -> String {
    fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures")
            .join(name),
    )
    .unwrap()
}

struct Reaped(Child);

impl Drop for Reaped {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

const FIXTURES: [&str; 2] = ["0.4.1-dash-theme.toml", "0.4.1-dash-assign.toml"];

#[test]
fn check_config_accepts_a_0_41_document_with_a_dash_connector() {
    for name in FIXTURES {
        let root = directory("check");
        let config = root.join("runtime.toml");
        fs::write(&config, fixture(name)).unwrap();
        let output = Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
            .arg("--check-config")
            .arg("--config")
            .arg(&config)
            .arg("--socket")
            .arg(root.join("none.sock"))
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{name}: exit {:?}: {}",
            output.status.code(),
            String::from_utf8_lossy(&output.stderr)
        );
        fs::remove_dir_all(root).unwrap();
    }
}

#[test]
fn the_service_starts_on_a_0_41_document_with_a_dash_connector() {
    for name in FIXTURES {
        let root = directory("start");
        let config = root.join("runtime.toml");
        let socket = root.join("r.sock");
        fs::write(&config, fixture(name)).unwrap();
        let child = Reaped(
            Command::new(env!("CARGO_BIN_EXE_wall-in-one-service"))
                .env_remove("WAYLAND_DISPLAY")
                .env_remove("WAYLAND_SOCKET")
                .arg("--config")
                .arg(&config)
                .arg("--socket")
                .arg(&socket)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        );
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut stream = loop {
            if let Ok(stream) = UnixStream::connect(&socket) {
                break stream;
            }
            assert!(
                Instant::now() < deadline,
                "{name}: the service never listened"
            );
            std::thread::sleep(Duration::from_millis(20));
        };
        writeln!(stream, r#"{{"verb":"quit"}}"#).unwrap();
        let mut line = String::new();
        BufReader::new(stream).read_line(&mut line).unwrap();
        assert!(line.contains(r#""ok":true"#), "{name}: {line}");
        drop(child);
        fs::remove_dir_all(root).unwrap();
    }
}
