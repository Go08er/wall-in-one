//! What a Wallpaper Engine item is, read the way linux-wallpaperengine finds it.
//!
//! The app classifies every Workshop item when it compiles `runtime.toml`,
//! and leaves a web page out while "Allow web wallpapers" is off. But the
//! engine reads `project.json` again when it starts, so an item whose type
//! became `web` after that compile (a Workshop update) would play as a web
//! page whatever the setting says. While the document doesn't allow web
//! wallpapers (`Settings::allow_web_wallpapers`), the service therefore reads
//! the item's type itself, just before it starts the engine, and refuses a
//! web page or an item it can't classify (docs/wallpaper-engine.md).

use std::fs::{self, OpenOptions};
use std::io::{ErrorKind, Read};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

/// Where the engine looks for `--bg <id>`, under the home folder, in its own
/// order (`Steam/FileSystem/FileSystem.cpp`): Steam's two usual homes, the
/// Flatpak's and the Snap's. Each is followed by `<id>`.
pub const ENGINE_WORKSHOP_FOLDERS: &[&str] = &[
    ".local/share/Steam/steamapps/workshop/content/431960",
    ".steam/steam/steamapps/workshop/content/431960",
    ".var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/workshop/content/431960",
    "snap/steam/common/.local/share/Steam/steamapps/workshop/content/431960",
];

/// The app's scan cap for one `project.json` (`workshop.MAX_PROJECT_BYTES`).
pub const MAX_PROJECT_BYTES: u64 = 256 * 1024;

/// The home folder the engine resolves `--bg <id>` against: the service's
/// own `HOME`, which its renderer children inherit. None when unset or not
/// absolute; then no item's type can be read.
pub fn engine_home() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .filter(|home| home.is_absolute())
}

/// Why the engine must not be started for Workshop item `id` while web
/// wallpapers aren't allowed, or None when it may.
///
/// Every place the engine could find the item is read, not only the first,
/// so the answer doesn't depend on matching the engine's order exactly: the
/// item may start only when each copy there is a `scene` or a `video`. A web
/// page, a type the engine doesn't play, a missing or unreadable
/// `project.json` (or one that is a link, not a regular file, or larger than
/// the app's scan reads), and an item found in none of them are refused.
pub fn refusal_without_web(home: Option<&Path>, id: &str) -> Option<String> {
    let why = match home {
        None => Some("can't be checked: the service has no home folder".to_string()),
        Some(_) if id.is_empty() || !id.bytes().all(|byte| byte.is_ascii_digit()) => {
            Some("has no Workshop id the service can check".to_string())
        }
        Some(home) => classify(home, id),
    }?;
    Some(format!(
        "web wallpapers are off: Workshop item {id} {why}, so linux-wallpaperengine was not \
         started for it; its still stays"
    ))
}

fn classify(home: &Path, id: &str) -> Option<String> {
    let mut found = false;
    for folder in ENGINE_WORKSHOP_FOLDERS {
        let item = home.join(folder).join(id);
        match fs::metadata(&item) {
            // The engine follows a link to the item's folder, so this does.
            Ok(metadata) if metadata.is_dir() => {}
            Ok(_) => continue,
            Err(error) if error.kind() == ErrorKind::NotFound => continue,
            Err(error) => {
                return Some(format!("can't be checked: {}: {error}", item.display()));
            }
        }
        found = true;
        match project_type(&item.join("project.json")) {
            Ok(kind) if kind == "web" => return Some("is a web page".to_string()),
            Ok(kind) if kind == "scene" || kind == "video" => {}
            Ok(kind) => {
                return Some(format!(
                    "has a type the service doesn't play ({kind:?}) in {}",
                    item.display()
                ));
            }
            Err(error) => return Some(format!("can't be checked: {error}")),
        }
    }
    if found {
        None
    } else {
        Some("can't be checked: its Workshop folder wasn't found".to_string())
    }
}

/// The item's `type`, lower-cased as the engine lower-cases it (ASCII only,
/// nothing trimmed). Read without following a final link, bounded, from a
/// regular file, as the app's scan reads it.
fn project_type(path: &Path) -> Result<String, String> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|error| format!("{}: {error}", path.display()))?;
    let metadata = file
        .metadata()
        .map_err(|error| format!("{}: {error}", path.display()))?;
    if !metadata.is_file() {
        return Err(format!("{} is not a regular file", path.display()));
    }
    let mut bytes = Vec::new();
    file.take(MAX_PROJECT_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| format!("{}: {error}", path.display()))?;
    if bytes.len() as u64 > MAX_PROJECT_BYTES {
        return Err(format!(
            "{} is larger than {} KiB",
            path.display(),
            MAX_PROJECT_BYTES / 1024
        ));
    }
    let project: serde_json::Value = serde_json::from_slice(&bytes)
        .map_err(|error| format!("{} is not JSON: {error}", path.display()))?;
    match project.get("type") {
        Some(serde_json::Value::String(kind)) => Ok(kind.to_ascii_lowercase()),
        _ => Err(format!("{} names no type", path.display())),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{PermissionsExt, symlink};

    fn home(label: &str) -> PathBuf {
        let path = std::env::temp_dir().join(format!(
            "wall-in-one-workshop-{label}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn item(home: &Path, folder: usize, id: &str, project: &str) -> PathBuf {
        let directory = home.join(ENGINE_WORKSHOP_FOLDERS[folder]).join(id);
        fs::create_dir_all(&directory).unwrap();
        fs::write(directory.join("project.json"), project).unwrap();
        directory
    }

    #[test]
    fn a_scene_or_video_may_start_and_a_web_page_may_not() {
        let home = home("types");
        item(&home, 0, "1", r#"{"type":"scene","file":"scene.json"}"#);
        item(&home, 0, "2", r#"{"type":"Video","file":"a.mp4"}"#);
        item(&home, 0, "3", r#"{"type":"web","file":"index.html"}"#);
        item(&home, 0, "4", r#"{"type":"WEB","file":"index.html"}"#);
        assert_eq!(refusal_without_web(Some(&home), "1"), None);
        assert_eq!(refusal_without_web(Some(&home), "2"), None);
        for id in ["3", "4"] {
            let refused = refusal_without_web(Some(&home), id).unwrap();
            assert!(refused.contains("is a web page"), "{refused}");
            assert!(refused.starts_with("web wallpapers are off"), "{refused}");
        }
        fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn what_the_service_cant_classify_is_refused() {
        let home = home("unclassified");
        item(&home, 0, "10", r#"{"type":"application"}"#);
        item(&home, 0, "11", r#"{"title":"no type"}"#);
        item(&home, 0, "12", "{not json");
        item(&home, 0, "13", r#"{"type":" scene"}"#);
        let large = format!(
            r#"{{"type":"scene","pad":"{}"}}"#,
            "x".repeat(MAX_PROJECT_BYTES as usize)
        );
        item(&home, 0, "14", &large);
        let linked = item(&home, 0, "15", r#"{"type":"scene"}"#);
        fs::remove_file(linked.join("project.json")).unwrap();
        let elsewhere = home.join("elsewhere.json");
        fs::write(&elsewhere, r#"{"type":"scene"}"#).unwrap();
        symlink(&elsewhere, linked.join("project.json")).unwrap();
        let missing = item(&home, 0, "16", "{}");
        fs::remove_file(missing.join("project.json")).unwrap();
        for id in ["10", "11", "12", "13", "14", "15", "16", "17", "x1", ""] {
            assert!(
                refusal_without_web(Some(&home), id).is_some(),
                "{id} must be refused"
            );
        }
        assert!(refusal_without_web(None, "1").is_some());
        fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn every_place_the_engine_looks_must_agree() {
        let home = home("places");
        item(&home, 0, "20", r#"{"type":"scene"}"#);
        item(&home, 2, "20", r#"{"type":"web"}"#);
        assert!(refusal_without_web(Some(&home), "20").is_some());
        // Found only in the Snap's folder: still read.
        item(&home, 3, "21", r#"{"type":"web"}"#);
        assert!(refusal_without_web(Some(&home), "21").is_some());
        item(&home, 1, "22", r#"{"type":"scene"}"#);
        assert_eq!(refusal_without_web(Some(&home), "22"), None);
        fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn a_folder_that_cant_be_looked_into_is_refused() {
        let home = home("unreadable");
        let steam = home.join(".local/share/Steam/steamapps/workshop/content");
        item(&home, 0, "30", r#"{"type":"scene"}"#);
        fs::set_permissions(&steam, fs::Permissions::from_mode(0o000)).unwrap();
        let refused = refusal_without_web(Some(&home), "30");
        fs::set_permissions(&steam, fs::Permissions::from_mode(0o755)).unwrap();
        // Root can look into anything; the refusal only holds for a user.
        if unsafe { libc::geteuid() } != 0 {
            assert!(refused.is_some());
        }
        fs::remove_dir_all(home).unwrap();
    }
}
