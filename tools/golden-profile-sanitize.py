#!/usr/bin/env python3
"""Make a sanitized golden copy of this machine's Wall-in-One profile.

    tools/golden-profile-sanitize.py OUTPUT_DIR

Writes a profile directory in the layout of ``tests/golden/profile`` that the
golden-profile harness can materialize (``WIO_GOLDEN_PROFILE=OUTPUT_DIR``):

* ``config/wall-in-one`` and ``state/wall-in-one`` are copied whole, dotfiles,
  locks and ``.wall-in-one-removal-*`` evidence included;
* of Noctalia's ``settings.toml`` only what the app reads is kept: the theme
  mode, the ``wall-in-one`` template registration and the wallpaper keys;
* the thumbnail cache is not copied (only counted);
* every wallpaper root and Steam's Workshop tree become ``library`` entries:
  sizes, modes and mtimes for media (materialized as sparse files), the text
  of small JSON/VDF sidecars, markers and ``project.json`` files.

Sanitizing: the home directory becomes ``/home/user`` everywhere, including
the values derived from absolute paths (all-media entry ids and generated
still names); ``wallhaven-api-key`` and every file or key that looks like a
secret (key, token, secret, password, credential) are dropped. Structure,
versions and record counts are kept, and summarized in ``profile.json``.

Read-only towards the real profile: it only ever lists, stats and reads. It
imports no Wall-in-One code, so no Store, lock or migration runs against the
real files. The output is personal data with paths removed; keep it out of
version control (``.project-notes/`` is ignored).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.golden import harness

#: Keys (and file names) that look like they hold a secret.
SECRET_WORDS = r"api[-_]?key|secret|passw|credential|access[-_]?token|auth[-_]?token"
SECRET = re.compile(SECRET_WORDS, re.IGNORECASE)
SECRET_LINE = re.compile(
    r"\s*[\"']?[\w.-]*(?:" + SECRET_WORDS + r")[\w.-]*[\"']?\s*=", re.IGNORECASE
)
SECRET_FILE = re.compile(r"(?i)(api[-_]?key|secret|passw|credential|token)")
#: Secrets carried inside values, such as ``?apikey=...`` in a download URL.
SECRET_PARAMETER = re.compile(r"(?i)\b(api[-_]?key|access[-_]?token|token|key)=([^&#\s\"']+)")
TEXT_SUFFIXES = (".json", ".vdf", ".toml", ".txt")
MAX_TEXT_BYTES = 256 * 1024
MAX_LIBRARY_ENTRIES = 200_000
WORKSHOP = Path("steamapps") / "workshop" / "content" / "431960"


def xdg(name: str, default: str) -> Path:
    value = os.environ.get(name)
    return Path(value) if value and Path(value).is_absolute() else Path.home() / default


def read(path: Path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def scrub_json(value: Any) -> tuple[Any, int]:
    """Drop secret-looking keys; return the value and how many were dropped."""
    dropped = 0
    if isinstance(value, dict):
        kept: dict[str, Any] = {}
        for key, child in value.items():
            if SECRET.search(str(key)):
                dropped += 1
                continue
            kept[key], count = scrub_json(child)
            dropped += count
        return kept, dropped
    if isinstance(value, list):
        items = []
        for child in value:
            item, count = scrub_json(child)
            items.append(item)
            dropped += count
        return items, dropped
    return value, 0


def scrub_text(name: str, text: str) -> tuple[str, int]:
    """Keep the bytes unless a secret-looking key or parameter is present."""
    text, parameters = SECRET_PARAMETER.subn(r"\1=REDACTED", text)
    if parameters:
        scrubbed, dropped = scrub_text(name, text)
        return scrubbed, dropped + parameters
    if name.endswith(".json") or text.lstrip().startswith(("{", "[")):
        try:
            document = json.loads(text)
        except ValueError:
            document = None
        if document is not None:
            scrubbed, dropped = scrub_json(document)
            if dropped:
                return json.dumps(scrubbed, indent=2, ensure_ascii=False) + "\n", dropped
            return text, 0
    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if not SECRET_LINE.match(line)]
    return "".join(kept), len(lines) - len(kept)


def walk(base: Path) -> Iterator[Path]:
    """Everything below ``base`` without following symlinks, bounded."""
    seen = 0
    stack = [base]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as listing:
                children = sorted(listing, key=lambda entry: entry.name)
        except OSError as error:
            print(f"skipped unreadable {directory}: {error}", file=sys.stderr)
            continue
        for child in children:
            seen += 1
            if seen > MAX_LIBRARY_ENTRIES:
                raise SystemExit(f"more than {MAX_LIBRARY_ENTRIES} entries below {base}")
            path = Path(child.path)
            yield path
            if child.is_dir(follow_symlinks=False):
                stack.append(path)


def noctalia_subset(path: Path) -> str | None:
    """The keys of Noctalia's settings that Wall-in-One reads, as TOML."""
    try:
        document = tomllib.loads(read(path).decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        print(f"skipped Noctalia settings: {error}", file=sys.stderr)
        return None

    def value(raw: object) -> str:
        if isinstance(raw, bool):
            return "true" if raw else "false"
        if isinstance(raw, int | float):
            return repr(raw)
        if isinstance(raw, str):
            return json.dumps(raw, ensure_ascii=False)
        if isinstance(raw, list):
            return "[" + ", ".join(value(item) for item in raw) + "]"
        raise TypeError(type(raw).__name__)

    def table(name: str, raw: object, keys: tuple[str, ...] | None = None) -> list[str]:
        if not isinstance(raw, dict):
            return []
        lines = [f"[{name}]"]
        for key, item in raw.items():
            wanted = keys is None or key in keys
            if wanted and not isinstance(item, dict) and not SECRET.search(key):
                lines.append(f"{json.dumps(key) if '-' in key else key} = {value(item)}")
        return [*lines, ""]

    theme = document.get("theme", {})
    templates = theme.get("templates", {}) if isinstance(theme, dict) else {}
    user = templates.get("user", {}) if isinstance(templates, dict) else {}
    wallpaper = document.get("wallpaper", {})
    lines = [
        "# Subset of Noctalia's settings.toml read by Wall-in-One (sanitized).",
        "",
        *table("theme", theme, ("mode", "pure_black_dark", "source", "wallpaper_scheme")),
        *table("accessibility", document.get("accessibility"), ("high_contrast",)),
        *table("theme.templates.user.wall-in-one", user.get("wall-in-one")),
        *table("wallpaper", wallpaper, ("directory", "enabled")),
    ]
    if isinstance(wallpaper, dict):
        lines += table("wallpaper.last", wallpaper.get("last"), ("path",))
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("output", type=Path, help="new or empty directory to write")
    options = parser.parse_args()

    home = Path.home().resolve()
    config_home = xdg("XDG_CONFIG_HOME", ".config")
    state_home = xdg("XDG_STATE_HOME", ".local/state")
    cache_home = xdg("XDG_CACHE_HOME", ".cache")
    sources = {
        "config/wall-in-one": config_home / "wall-in-one",
        "state/wall-in-one": state_home / "wall-in-one",
    }
    output = options.output.absolute()
    protected = [*sources.values(), cache_home / "wall-in-one", state_home / "noctalia"]
    if any(output.is_relative_to(path) for path in protected):
        raise SystemExit(f"refusing to write inside the real profile: {output}")
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"{output} is not empty")
    for name, expected in (("config", ".config"), ("state", ".local/state"), ("cache", ".cache")):
        actual = {"config": config_home, "state": state_home, "cache": cache_home}[name]
        if actual != home / expected:
            raise SystemExit(f"{name} home {actual} is not {home / expected}; not supported")

    files: dict[str, bytes] = {}
    tree: dict[str, dict[str, object]] = {}
    paths: set[str] = set()
    dropped: list[str] = []

    def keep_text(label: str, data: bytes) -> bytes:
        text = harness._decoded_text(data)
        if text is None:
            return data
        scrubbed, count = scrub_text(label, text)
        if count:
            dropped.append(f"{count} secret-looking value(s) in {label}")
        return scrubbed.encode("utf-8")

    for relative_base, base in sources.items():
        if not base.is_dir():
            print(f"absent: {base}", file=sys.stderr)
            continue
        tree[relative_base] = {"type": "dir", "mode": f"{stat.S_IMODE(base.stat().st_mode):04o}"}
        for path in walk(base):
            relative = f"{relative_base}/{path.relative_to(base).as_posix()}"
            status = path.lstat()
            mode = f"{stat.S_IMODE(status.st_mode):04o}"
            paths.add(str(path))
            if SECRET_FILE.search(path.name):
                dropped.append(f"file {relative}")
                continue
            if stat.S_ISDIR(status.st_mode):
                tree[relative] = {"type": "dir", "mode": mode}
            elif stat.S_ISREG(status.st_mode):
                files[relative] = keep_text(relative, read(path))
                tree[relative] = {"mode": mode, "mtime_ns": status.st_mtime_ns}
            else:
                dropped.append(f"non-regular {relative}")

    noctalia = state_home / "noctalia" / "settings.toml"
    subset = noctalia_subset(noctalia) if noctalia.is_file() else None
    if subset is not None:
        files["state/noctalia/settings.toml"] = subset.encode("utf-8")
        tree["state/noctalia/settings.toml"] = {
            "mode": f"{stat.S_IMODE(noctalia.stat().st_mode):04o}",
            "mtime_ns": noctalia.stat().st_mtime_ns,
        }
    thumbnails = cache_home / "wall-in-one" / "thumbnails"
    thumbnail_count = sum(1 for _ in os.scandir(thumbnails)) if thumbnails.is_dir() else 0
    tree["cache/wall-in-one"] = {"type": "dir", "mode": "0755"}

    library: list[dict[str, object]] = []

    def record(path: Path) -> None:
        status = path.lstat()
        mode = f"{stat.S_IMODE(status.st_mode):04o}"
        entry: dict[str, object] = {"path": str(path), "mode": mode}
        paths.add(str(path))
        if stat.S_ISLNK(status.st_mode):
            entry.update(type="symlink", target=os.readlink(path))
        elif stat.S_ISDIR(status.st_mode):
            entry.update(type="dir")
        elif stat.S_ISREG(status.st_mode):
            entry["mtime_ns"] = status.st_mtime_ns
            if SECRET_FILE.search(path.name):
                dropped.append(f"library file {path.name}")
                return
            if path.name.endswith(TEXT_SUFFIXES) and status.st_size <= MAX_TEXT_BYTES:
                data = keep_text(path.name, read(path))
                text = harness._decoded_text(data)
                if text is not None:
                    entry.update(type="text", text=text)
                    library.append(entry)
                    return
            entry.update(type="sparse", size=status.st_size)
        else:
            return
        library.append(entry)

    roots: list[Path] = []
    settings_path = sources["config/wall-in-one"] / "settings.toml"
    if settings_path.is_file():
        settings = tomllib.loads(read(settings_path).decode("utf-8"))
        roots = [Path(root).expanduser() for root in settings.get("roots", [])]
    steam = [
        home / ".local/share/Steam",
        home / ".steam/steam",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ]
    for root in roots:
        if not root.is_relative_to(home):
            print(f"skipped root outside the home: {root}", file=sys.stderr)
            continue
        if root.is_dir():
            for parent in reversed(root.relative_to(home).parents):
                if parent != Path("."):
                    record(home / parent)
            record(root)
            for path in walk(root):
                record(path)
    for candidate in steam:
        if candidate.is_symlink():
            record(candidate)
            continue
        if not candidate.is_dir():
            continue
        index = candidate / "steamapps" / "libraryfolders.vdf"
        if index.is_file():
            record(index)
        folders = [candidate]
        if index.is_file():
            for match in re.finditer(r'"path"\s*"([^"]+)"', read(index).decode("utf-8", "replace")):
                folder = Path(match.group(1))
                if folder != candidate and folder.is_dir():
                    if folder.is_relative_to(home):
                        folders.append(folder)
                    else:
                        print(f"skipped Steam library outside the home: {folder}", file=sys.stderr)
        for folder in folders:
            content = folder / WORKSHOP
            if content.is_dir():
                for parent in content.relative_to(home).parents:
                    if parent != Path("."):
                        record(home / parent)
                record(content)
                for path in walk(content):
                    record(path)

    for data in files.values():
        text = harness._decoded_text(data)
        if text is not None:
            paths.update(harness._embedded_paths(text, str(home)))
    relocation = harness.Relocation(str(home), harness.FIXTURE_HOME, paths)

    summary: dict[str, object] = {"thumbnails_not_copied": thumbnail_count}
    output.mkdir(parents=True, exist_ok=True)
    for relative, data in sorted(files.items()):
        target = output / relocation.name(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = harness._decoded_text(data)
        target.write_bytes(relocation.text(text).encode("utf-8") if text is not None else data)
        if relative.endswith(".json") and text is not None:
            try:
                document = json.loads(text)
            except ValueError:
                continue
            if isinstance(document, dict):
                summary[relative] = {
                    "version": document.get("version"),
                    **{
                        key: len(value)
                        for key, value in document.items()
                        if isinstance(value, list | dict)
                    },
                }
    moved_library = []
    for entry in library:
        moved = dict(entry)
        moved["path"] = relocation.full_path(str(entry["path"]))
        if "text" in entry:
            moved["text"] = relocation.text(str(entry["text"]))
        if "target" in entry:
            moved["target"] = relocation.text(str(entry["target"]))
        moved_library.append(moved)
    summary["library_entries"] = len(moved_library)
    manifest = {
        "format": harness.FORMAT,
        "home": harness.FIXTURE_HOME,
        "description": "Sanitized local copy of a real Wall-in-One profile. Never commit.",
        "tree": {relocation.name(key): value for key, value in sorted(tree.items())},
        "library": sorted(moved_library, key=lambda entry: str(entry["path"])),
        "summary": summary,
        "dropped": dropped,
    }
    (output / harness.MANIFEST).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    leftovers = [
        relative
        for relative in files
        if str(home) in (output / relocation.name(relative)).read_bytes().decode("utf-8", "replace")
    ]
    if leftovers or str(home) in (output / harness.MANIFEST).read_text(encoding="utf-8"):
        raise SystemExit(f"the real home path survived sanitizing in {leftovers or 'profile.json'}")
    print(json.dumps({"output": str(output), "summary": summary, "dropped": dropped}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
