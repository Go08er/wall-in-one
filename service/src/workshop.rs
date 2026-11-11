//! What a Wallpaper Engine item is, read the way linux-wallpaperengine finds it.
//!
//! The app classifies every Workshop item when it compiles `runtime.toml`,
//! and leaves a web page out while "Allow web wallpapers" is off. But the
//! engine reads `project.json` again when it starts, so an item whose type
//! became `web` after that compile (a Workshop update) would play as a web
//! page whatever the setting says, and one that became an `application` would
//! rest on the engine's own parser alone. The service therefore reads the
//! item's type itself, just before it starts the engine, whatever the setting
//! (`Settings::allow_web_wallpapers`): a `scene` or a `video` may start, a
//! `web` page only while the document allows web wallpapers, and nothing else
//! ever, nor an item it can't classify (docs/wallpaper-engine.md). The runtime
//! skips a refused entry to the next one that can play.

use crate::runtime::truncate_middle;
use serde::de::{Deserialize, Deserializer, IgnoredAny, MapAccess, SeqAccess, Visitor};
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

/// The longest project `type` a refusal reason quotes, cut in the middle past
/// it. `project.json` may be `MAX_PROJECT_BYTES` long, and a reason is logged
/// and kept for the runtime's generation before status shortens it.
const MAX_QUOTED_TYPE_BYTES: usize = 64;

/// The home folder the engine resolves `--bg <id>` against: the service's
/// own `HOME`, which its renderer children inherit. None when unset or not
/// absolute; then no item's type can be read.
pub fn engine_home() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .filter(|home| home.is_absolute())
}

/// Why the engine must not be started for one Workshop item. Each reason says
/// what it is about: only a web page is about web wallpapers, and only while
/// they aren't allowed.
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

/// Why the engine must not be started for Workshop item `id`, or None when it
/// may; `allow_web` is whether the document allows web wallpapers.
///
/// Every place the engine could find the item is read, not only the first,
/// so the answer doesn't depend on matching the engine's order exactly: the
/// item may start only when each copy there is a `scene` or a `video`, or a
/// `web` page while `allow_web`. A web page without `allow_web`, a type the
/// engine doesn't play (an `application`, whatever `allow_web` says), a
/// missing or unreadable `project.json` (or one that is a link, not a regular
/// file, or larger than the app's scan reads), and an item found in none of
/// them are refused.
pub fn refusal(home: Option<&Path>, id: &str, allow_web: bool) -> Option<Refusal> {
    let why = match home {
        None => Why::Other(format!(
            "Workshop item {id} can't be checked: the service has no home folder"
        )),
        Some(_) if id.is_empty() || !id.bytes().all(|byte| byte.is_ascii_digit()) => Why::Other(
            format!("Workshop item {id:?} has no Workshop id the service can check"),
        ),
        Some(home) => classify(home, id, allow_web)?,
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

/// `refusal` while the document doesn't allow web wallpapers.
pub fn refusal_without_web(home: Option<&Path>, id: &str) -> Option<Refusal> {
    refusal(home, id, false)
}

/// What `classify` found wrong with an item.
enum Why {
    WebPage,
    Other(String),
}

fn classify(home: &Path, id: &str, allow_web: bool) -> Option<Why> {
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
            Ok(kind) if kind == "web" && !allow_web => return Some(Why::WebPage),
            Ok(kind) if kind == "web" => {}
            Ok(kind) if kind == "scene" || kind == "video" => {}
            Ok(kind) => {
                let kind = truncate_middle(&kind, MAX_QUOTED_TYPE_BYTES);
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
    let not_json = |error: &dyn fmt::Display| {
        ProjectError::Unparsable(format!("{} is not JSON: {error}", path.display()))
    };
    let kind = match serde_json::from_slice::<serde_json::Value>(&bytes) {
        Ok(project) => match project.get("type") {
            Some(serde_json::Value::String(kind)) => Some(kind.clone()),
            _ => None,
        },
        // serde_json stops at 128 levels of nesting, where the app's scan
        // and the engine's parser go on. Only the top level's `type` matters,
        // so the rest is skipped, which serde_json does at any depth. That
        // skip reads the text and its structure, not each number's range or
        // each escape's code point, so past 128 levels those go unchecked.
        Err(error) if error.to_string().starts_with("recursion limit exceeded") => {
            let text = std::str::from_utf8(&bytes).map_err(|error| not_json(&error))?;
            serde_json::from_str::<TopLevelType>(text)
                .map_err(|error| not_json(&error))?
                .0
        }
        Err(error) => return Err(not_json(&error)),
    };
    match kind {
        Some(kind) => Ok(kind.to_ascii_lowercase()),
        None => Err(ProjectError::Unparsable(format!(
            "{} names no type",
            path.display()
        ))),
    }
}

/// A project's top-level `type` when it is a string, read past any depth of
/// nesting beside it. The last `type` wins, as in a `serde_json::Value`.
struct TopLevelType(Option<String>);

impl<'de> Deserialize<'de> for TopLevelType {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Fields;
        impl<'de> Visitor<'de> for Fields {
            type Value = TopLevelType;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a JSON object")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<TopLevelType, A::Error> {
                let mut kind = None;
                while let Some(key) = map.next_key::<String>()? {
                    if key == "type" {
                        kind = map.next_value::<TypeText>()?.0;
                    } else {
                        map.next_value::<IgnoredAny>()?;
                    }
                }
                Ok(TopLevelType(kind))
            }
        }
        deserializer.deserialize_map(Fields)
    }
}

/// A `type` value: its text when it is a string; anything else is none, and
/// what it holds is skipped like any other value.
struct TypeText(Option<String>);

impl<'de> Deserialize<'de> for TypeText {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Text;
        impl<'de> Visitor<'de> for Text {
            type Value = TypeText;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("any JSON value")
            }
            fn visit_str<E>(self, text: &str) -> Result<TypeText, E> {
                Ok(TypeText(Some(text.to_string())))
            }
            fn visit_bool<E>(self, _: bool) -> Result<TypeText, E> {
                Ok(TypeText(None))
            }
            fn visit_i64<E>(self, _: i64) -> Result<TypeText, E> {
                Ok(TypeText(None))
            }
            fn visit_u64<E>(self, _: u64) -> Result<TypeText, E> {
                Ok(TypeText(None))
            }
            fn visit_f64<E>(self, _: f64) -> Result<TypeText, E> {
                Ok(TypeText(None))
            }
            fn visit_unit<E>(self) -> Result<TypeText, E> {
                Ok(TypeText(None))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<TypeText, A::Error> {
                while seq.next_element::<IgnoredAny>()?.is_some() {}
                Ok(TypeText(None))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<TypeText, A::Error> {
                while map.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {}
                Ok(TypeText(None))
            }
        }
        deserializer.deserialize_any(Text)
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
    fn with_web_allowed_a_web_page_may_start_and_an_application_still_may_not() {
        let home = home("allowed");
        item(&home, 0, "1", r#"{"type":"scene","file":"scene.json"}"#);
        item(&home, 0, "2", r#"{"type":"Video","file":"a.mp4"}"#);
        item(&home, 0, "3", r#"{"type":"WEB","file":"index.html"}"#);
        item(&home, 0, "4", r#"{"type":"application","file":"run.exe"}"#);
        item(&home, 0, "5", "{not json");
        for id in ["1", "2", "3"] {
            assert_eq!(refusal(Some(&home), id, true), None, "{id}");
        }
        assert!(refusal(Some(&home), "3", false).unwrap().web_page);
        for id in ["4", "5", "6"] {
            let allowed = refusal(Some(&home), id, true).unwrap();
            assert!(!allowed.web_page, "{id}");
            assert!(!allowed.reason.contains("web wallpapers"), "{id}");
            assert_eq!(Some(allowed), refusal(Some(&home), id, false), "{id}");
        }
        assert!(refusal(None, "1", true).is_some());
        fs::remove_dir_all(home).unwrap();
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

    /// A scene's `project.json` with `depth` levels of arrays beside its type.
    fn nested(kind: &str, depth: usize, tail: &str) -> String {
        format!(
            r#"{{"type":"{kind}","general":{}1{}{tail}}}"#,
            "[".repeat(depth),
            "]".repeat(depth)
        )
    }

    #[test]
    fn deep_nesting_beside_the_type_is_read_past() {
        // r13 N1: serde_json refuses more than 128 levels, which the app's scan and the
        // engine read; such a scene was refused as unparsable.
        let home = home("deep");
        item(&home, 0, "60", &nested("scene", 200, ""));
        item(&home, 0, "61", &nested("web", 200, ""));
        item(&home, 0, "62", &nested("scene", 200, r#","type":"web""#));
        item(&home, 0, "63", &format!("{},", nested("scene", 200, "")));
        item(
            &home,
            0,
            "64",
            &format!(
                r#"{{"type":{}"scene"{}}}"#,
                "[".repeat(200),
                "]".repeat(200)
            ),
        );
        assert_eq!(refusal_without_web(Some(&home), "60"), None);
        assert!(refusal_without_web(Some(&home), "61").unwrap().web_page);
        // The last `type` wins, as it does below the limit.
        assert!(refusal_without_web(Some(&home), "62").unwrap().web_page);
        for (id, said) in [("63", "is not JSON"), ("64", "names no type")] {
            let refused = refusal_without_web(Some(&home), id).unwrap();
            assert!(!refused.web_page);
            assert!(refused.reason.contains(said), "{id}: {refused}");
        }
        fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn control_nesting_below_the_limit_was_always_read() {
        let home = home("shallow");
        item(&home, 0, "65", &nested("scene", 100, ""));
        item(&home, 0, "66", &nested("web", 100, ""));
        assert_eq!(refusal_without_web(Some(&home), "65"), None);
        assert!(refusal_without_web(Some(&home), "66").is_some());
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
    fn every_place_the_engine_looks_must_agree_with_web_on_too() {
        let home = home("places-web-on");
        // A scene in one folder and an application in another: refused.
        item(&home, 0, "20", r#"{"type":"scene"}"#);
        item(&home, 2, "20", r#"{"type":"application"}"#);
        assert!(refusal(Some(&home), "20", true).is_some());
        // And the other way round, found only in the Snap's folder.
        item(&home, 1, "21", r#"{"type":"application"}"#);
        item(&home, 3, "21", r#"{"type":"scene"}"#);
        assert!(refusal(Some(&home), "21", true).is_some());
        item(&home, 3, "22", r#"{"type":"application"}"#);
        assert!(refusal(Some(&home), "22", true).is_some());
        // A web page beside a scene passes only with web on.
        item(&home, 0, "23", r#"{"type":"scene"}"#);
        item(&home, 2, "23", r#"{"type":"web"}"#);
        assert_eq!(refusal(Some(&home), "23", true), None);
        assert!(refusal(Some(&home), "23", false).is_some());
        // A web page beside an application is still refused.
        item(&home, 0, "24", r#"{"type":"web"}"#);
        item(&home, 1, "24", r#"{"type":"application"}"#);
        assert!(refusal(Some(&home), "24", true).is_some());
        // Scene and video agree.
        item(&home, 1, "25", r#"{"type":"scene"}"#);
        item(&home, 3, "25", r#"{"type":"video"}"#);
        assert_eq!(refusal(Some(&home), "25", true), None);
        // A copy that can't be read is refused beside a good one.
        item(&home, 0, "26", r#"{"type":"scene"}"#);
        item(&home, 2, "26", "not json");
        assert!(refusal(Some(&home), "26", true).is_some());
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

    #[test]
    fn an_unknown_type_is_quoted_only_up_to_a_cap() {
        let home = home("long-type");
        // Multi-byte characters, so a cut must keep to character boundaries.
        let long = "\u{e9}".repeat(100_000);
        item(&home, 0, "50", &format!(r#"{{"type":"{long}"}}"#));
        item(&home, 0, "51", r#"{"type":"application"}"#);
        let reason = refusal_without_web(Some(&home), "50").unwrap().reason;
        assert!(reason.len() < 1024, "{} bytes", reason.len());
        assert!(reason.contains("[truncated]"));
        let reason = refusal_without_web(Some(&home), "51").unwrap().reason;
        assert!(reason.contains("(\"application\")"));
        fs::remove_dir_all(home).unwrap();
    }
}
