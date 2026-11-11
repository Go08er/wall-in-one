//! The service's engine-type check against the cases it shares with the app
//! (`tests/test_engine_refusal_agreement.py` runs the same file through
//! `workshop.engine_refusal`). `fixtures/engine-types/cases.json` is the
//! oracle: each case builds one Workshop item under a fresh home and says
//! whether `refusal` lets the engine start it, web wallpapers off and on.
//! Set `ENGINE_TYPES_RECORD=<path>` to write the file again with the answers
//! this build gives, to be read before it is kept.

use serde_json::Value;
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use wall_in_one_service::workshop::{ENGINE_WORKSHOP_FOLDERS, refusal};

const SCENE: &str = r#"{"type":"scene"}"#;

fn cases() -> Vec<Value> {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/engine-types/cases.json");
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

/// What `project.json` holds, as bytes, or None for a file kind that isn't.
fn project_bytes(spec: &Value) -> Vec<u8> {
    if let Some(text_value) = spec.get("text") {
        return text_value.as_str().unwrap().as_bytes().to_vec();
    }
    if let Some(hex) = spec.get("hex") {
        let hex = hex.as_str().unwrap();
        return (0..hex.len())
            .step_by(2)
            .map(|at| u8::from_str_radix(&hex[at..at + 2], 16).unwrap())
            .collect();
    }
    if let Some(fill) = spec.get("fill") {
        let head = fill["head"].as_str().unwrap();
        let tail = fill["tail"].as_str().unwrap();
        let total = fill["total"].as_u64().unwrap() as usize;
        let byte = fill["byte"].as_str().unwrap().as_bytes()[0];
        let mut bytes = head.as_bytes().to_vec();
        bytes.resize(total - tail.len(), byte);
        bytes.extend_from_slice(tail.as_bytes());
        return bytes;
    }
    let nest = &spec["nest"];
    let depth = nest["depth"].as_u64().unwrap() as usize;
    let mut out = nest["before"].as_str().unwrap().to_string();
    out.push_str(&nest["open"].as_str().unwrap().repeat(depth));
    out.push_str(nest["inner"].as_str().unwrap());
    out.push_str(&nest["close"].as_str().unwrap().repeat(depth));
    out.push_str(nest["after"].as_str().unwrap());
    out.into_bytes()
}

fn write_project(directory: &Path, spec: &Value) {
    let path = directory.join("project.json");
    match spec.get("special").and_then(Value::as_str) {
        Some("fifo") => {
            let c_path = std::ffi::CString::new(path.as_os_str().as_bytes()).unwrap();
            assert_eq!(unsafe { libc::mkfifo(c_path.as_ptr(), 0o600) }, 0);
        }
        Some("symlink") => {
            fs::write(directory.join("real.json"), SCENE).unwrap();
            symlink("real.json", &path).unwrap();
        }
        Some("dangling") => symlink("nowhere.json", &path).unwrap(),
        Some("directory") => fs::create_dir(&path).unwrap(),
        Some("absent") => {}
        Some(other) => panic!("unknown special {other}"),
        None => fs::write(&path, project_bytes(spec)).unwrap(),
    }
}

fn home(index: usize) -> PathBuf {
    let path = std::env::temp_dir().join(format!(
        "wall-in-one-agreement-{}-{index}",
        std::process::id()
    ));
    let _ = fs::remove_dir_all(&path);
    fs::create_dir_all(&path).unwrap();
    path
}

/// The id a case uses: its own, or one derived from its place in the file.
fn id_of(index: usize, case: &Value) -> String {
    case.get("id")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| (100_000 + index).to_string())
}

/// Builds the case under `home`; the id it is looked up by is `id_of`.
fn build(home: &Path, index: usize, case: &Value) {
    let id = id_of(index, case);
    let folder = |at: usize| home.join(ENGINE_WORKSHOP_FOLDERS[at]);
    if let Some(item) = case.get("item").and_then(Value::as_str) {
        fs::create_dir_all(folder(0)).unwrap();
        let place = folder(0).join(&id);
        match item {
            "file" => fs::write(&place, "x").unwrap(),
            "link" => {
                let elsewhere = home.join("elsewhere").join(&id);
                fs::create_dir_all(&elsewhere).unwrap();
                fs::write(elsewhere.join("project.json"), SCENE).unwrap();
                symlink(&elsewhere, &place).unwrap();
            }
            "dangling" => symlink(home.join("nowhere"), &place).unwrap(),
            other => panic!("unknown item kind {other}"),
        }
    }
    for copy in case
        .get("copies")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let directory = folder(copy["folder"].as_u64().unwrap() as usize).join(&id);
        fs::create_dir_all(&directory).unwrap();
        write_project(&directory, &copy["project"]);
    }
    // A file where a folder's ancestor should be: for the lookups that come
    // after a copy built above, never an ancestor of one.
    for name in case
        .get("ancestor_files")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let place = home.join(name.as_str().unwrap());
        fs::write(&place, "not a folder").unwrap();
    }
}

fn answer(allowed: bool) -> &'static str {
    if allowed { "allowed" } else { "refused" }
}

#[test]
fn the_service_gives_the_answers_the_app_is_held_to() {
    let mut cases = cases();
    let record = std::env::var_os("ENGINE_TYPES_RECORD");
    let mut wrong = Vec::new();
    for (index, case) in cases.iter_mut().enumerate() {
        let home = home(index);
        build(&home, index, case);
        let id = id_of(index, case);
        for (key, web) in [("web_off", false), ("web_on", true)] {
            let got = answer(refusal(Some(&home), &id, web).is_none());
            if record.is_some() {
                case[key] = Value::String(got.to_string());
            } else if case[key].as_str() != Some(got) {
                wrong.push(format!(
                    "{} ({key}): expected {}, got {got}",
                    case["name"], case[key]
                ));
            }
        }
        fs::remove_dir_all(&home).unwrap();
    }
    if let Some(path) = record {
        let mut text = serde_json::to_string_pretty(&cases).unwrap();
        text.push('\n');
        fs::write(path, text).unwrap();
    }
    assert!(wrong.is_empty(), "{}", wrong.join("\n"));
}
