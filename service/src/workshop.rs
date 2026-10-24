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

use std::fmt;
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

/// Why the engine must not be started for one Workshop item while web
/// wallpapers aren't allowed. Each reason says what it is about: only a web
/// page is about web wallpapers.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Refusal {
    /// The item is a web page. Every other refusal is an item the service
    /// can't find, read or play.
    pub web_page: bool,
    /// What the service says, naming the item.
    pub reason: String,
}

impl fmt::Display for Refusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.reason)
    }
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
pub fn refusal_without_web(home: Option<&Path>, id: &str) -> Option<Refusal> {
    let why = match home {
        None => Why::Other(format!(
            "Workshop item {id} can't be checked: the service has no home folder"
        )),
        Some(_) if id.is_empty() || !id.bytes().all(|byte| byte.is_ascii_digit()) => Why::Other(
            format!("Workshop item {id:?} has no Workshop id the service can check"),
        ),
        Some(home) => classify(home, id)?,
    };
    Some(match why {
        Why::WebPage => Refusal {
            web_page: true,
            reason: format!(
                "this configuration doesn't allow web wallpapers: Workshop item {id} is a web \
                 page, so linux-wallpaperengine was not started for it"
            ),
        },
        Why::Other(why) => Refusal {
            web_page: false,
            reason: format!("{why}, so linux-wallpaperengine was not started for it"),
        },
    })
}

/// What `classify` found wrong with an item.
enum Why {
    WebPage,
    Other(String),
}

fn classify(home: &Path, id: &str) -> Option<Why> {
    let mut found = false;
    for folder in ENGINE_WORKSHOP_FOLDERS {
        let item = home.join(folder).join(id);
        match fs::metadata(&item) {
            // The engine follows a link to the item's folder, so this does.
            Ok(metadata) if metadata.is_dir() => {}
            Ok(_) => continue,
            Err(error) if error.kind() == ErrorKind::NotFound => continue,
            Err(error) => {
                return Some(Why::Other(format!(
                    "Workshop item {id} can't be checked: {}: {error}",
                    item.display()
                )));
            }
        }
        found = true;
        match project_type(&item.join("project.json")) {
            Ok(kind) if kind == "web" => return Some(Why::WebPage),
            Ok(kind) if kind == "scene" || kind == "video" => {}
            Ok(kind) => {
                return Some(Why::Other(format!(
                    "Workshop item {id} has a type the service doesn't play ({kind:?}) in {}",
                    item.display()
                )));
            }
            Err(ProjectError::Unreadable(error)) => {
                return Some(Why::Other(format!(
                    "Workshop item {id}'s project file can't be read: {error}"
                )));
            }
            Err(ProjectError::Unparsable(error)) => {
                return Some(Why::Other(format!(
                    "Workshop item {id}'s project file can't be parsed: {error}"
                )));
            }
        }
    }
    if found {
        None
    } else {
        // A Steam library on another drive (`libraryfolders.vdf`) is one the
        // app lists and the engine never looks in.
        Some(Why::Other(format!(
            "Workshop item {id} wasn't found where linux-wallpaperengine looks: Steam's, the \
             Flatpak's and the Snap's Workshop folders under {}, never another Steam library",
            home.display()
        )))
    }
}

/// Why an item's `project.json` gives no type.
enum ProjectError {
    /// It can't be opened or read, or isn't a regular file of a size the
    /// app's scan reads.
    Unreadable(String),
    /// It is read, but isn't JSON or names no type.
    Unparsable(String),
}

/// The item's `type`, lower-cased as the engine lower-cases it (ASCII only,
/// nothing trimmed). Read without following a final link, bounded, from a
/// regular file, as the app's scan reads it.
fn project_type(path: &Path) -> Result<String, ProjectError> {
    let unreadable =
        |error: std::io::Error| ProjectError::Unreadable(format!("{}: {error}", path.display()));
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(unreadable)?;
    let metadata = file.metadata().map_err(unreadable)?;
    if !metadata.is_file() {
        return Err(ProjectError::Unreadable(format!(
            "{} is not a regular file",
            path.display()
        )));
    }
    let mut bytes = Vec::new();
    file.take(MAX_PROJECT_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(unreadable)?;
    if bytes.len() as u64 > MAX_PROJECT_BYTES {
        return Err(ProjectError::Unreadable(format!(
            "{} is larger than {} KiB",
            path.display(),
            MAX_PROJECT_BYTES / 1024
        )));
    }
    let project: serde_json::Value = serde_json::from_slice(&bytes).map_err(|error| {
        ProjectError::Unparsable(format!("{} is not JSON: {error}", path.display()))
    })?;
    match project.get("type") {
        Some(serde_json::Value::String(kind)) => Ok(kind.to_ascii_lowercase()),
        _ => Err(ProjectError::Unparsable(format!(
            "{} names no type",
            path.display()
        ))),
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
            assert!(refused.web_page);
            assert_eq!(
                refused.reason,
                format!(
                    "this configuration doesn't allow web wallpapers: Workshop item {id} is a \
                     web page, so linux-wallpaperengine was not started for it"
                )
            );
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
    fn only_a_web_page_is_blamed_on_web_wallpapers() {
        // r13 L6: every refusal was worded "web wallpapers are off: …", also for an item
        // that isn't a web page at all, such as a scene on a second Steam library.
        let home = home("worded");
        item(&home, 0, "40", r#"{"type":"web","file":"index.html"}"#);
        item(&home, 0, "41", r#"{"type":"application"}"#);
        item(&home, 0, "42", "{not json");
        item(&home, 0, "43", r#"{"title":"no type"}"#);
        let unreadable = item(&home, 0, "44", r#"{"type":"scene"}"#);
        fs::remove_file(unreadable.join("project.json")).unwrap();
        let said = |home: Option<&Path>, id: &str| {
            refusal_without_web(home, id)
                .map(|refusal| refusal.to_string())
                .unwrap_or_default()
        };
        let base = home.join(ENGINE_WORKSHOP_FOLDERS[0]);
        let cases = [
            (
                "40",
                "this configuration doesn't allow web wallpapers: Workshop item 40 is a web \
                 page, so linux-wallpaperengine was not started for it"
                    .to_string(),
            ),
            (
                "41",
                format!(
                    "Workshop item 41 has a type the service doesn't play (\"application\") in \
                     {}, so linux-wallpaperengine was not started for it",
                    base.join("41").display()
                ),
            ),
            (
                "42",
                format!(
                    "Workshop item 42's project file can't be parsed: {} is not JSON",
                    base.join("42/project.json").display()
                ),
            ),
            (
                "43",
                format!(
                    "Workshop item 43's project file can't be parsed: {} names no type, so \
                     linux-wallpaperengine was not started for it",
                    base.join("43/project.json").display()
                ),
            ),
            (
                "44",
                format!(
                    "Workshop item 44's project file can't be read: {}: ",
                    base.join("44/project.json").display()
                ),
            ),
            (
                "45",
                format!(
                    "Workshop item 45 wasn't found where linux-wallpaperengine looks: Steam's, \
                     the Flatpak's and the Snap's Workshop folders under {}, never another \
                     Steam library, so linux-wallpaperengine was not started for it",
                    home.display()
                ),
            ),
            (
                "x1",
                "Workshop item \"x1\" has no Workshop id the service can check, so \
                 linux-wallpaperengine was not started for it"
                    .to_string(),
            ),
        ];
        for (id, expected) in cases {
            let refused = said(Some(&home), id);
            assert!(refused.starts_with(&expected), "{id}: {refused}");
            assert_eq!(
                refused.contains("web wallpapers"),
                id == "40",
                "{id}: {refused}"
            );
        }
        assert_eq!(
            said(None, "1"),
            "Workshop item 1 can't be checked: the service has no home folder, so \
             linux-wallpaperengine was not started for it"
        );
        fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn control_nothing_is_said_for_a_scene_or_a_video() {
        let home = home("worded-control");
        item(&home, 0, "50", r#"{"type":"scene","file":"scene.json"}"#);
        item(&home, 0, "51", r#"{"type":"video","file":"a.mp4"}"#);
        for id in ["50", "51"] {
            let said = refusal_without_web(Some(&home), id).map(|refusal| refusal.to_string());
            assert_eq!(said, None, "{id}");
        }
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
