"""Materialize a stored profile into a sandbox home and watch what touches it.

A *profile directory* is the committed fixture (``tests/golden/profile``) or a
sanitized local copy of a real profile made by ``tools/golden-profile-sanitize.py``.
Both use one layout, written for a home of ``/home/user``:

``config/``, ``state/``, ``cache/``
    Copied verbatim to ``XDG_CONFIG_HOME``, ``XDG_STATE_HOME`` and
    ``XDG_CACHE_HOME`` (``.config``, ``.local/state`` and ``.cache`` below the
    sandbox home).
``profile.json``
    Modes, mtimes and empty directories for those trees (Git keeps neither),
    plus ``library``: the media, sidecars and markers outside the XDG dirs
    (wallpaper roots, Steam's Workshop tree), generated rather than stored.

The sandbox home is not ``/home/user``, so materializing *relocates*: every
``/home/user`` prefix in text becomes the sandbox home, and so does every value
this build derives from an absolute path (the 16-hex all-media entry id, the
``video-<24 hex>`` automatic-still stem). The deployed-upgrade evidence hashes
its paths, so it is re-sealed with the module's own renderers afterwards.
``runtime.toml`` is deliberately *not* re-sealed: it also carries this
machine's program paths, and the first start is allowed to recompile it under a
semantic check (see :func:`runtime_document_semantics`).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import stat
import struct
import tomllib
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Final, Literal

FORMAT: Final = 1
FIXTURE: Final = Path(__file__).with_name("profile")
FIXTURE_HOME: Final = "/home/user"
MANIFEST: Final = "profile.json"
XDG_LAYOUT: Final[Mapping[str, str]] = {
    "config": ".config",
    "state": ".local/state",
    "cache": ".cache",
}
DEFAULT_MTIME_NS: Final = 1_788_220_800_000_000_000  # 2026-09-01T00:00:00Z
#: Files larger than this are compared by size, inode and mtime, not by hash.
#: Only placeholder media (sparse videos of a real profile) are that large.
HASH_LIMIT: Final = 16 * 1024 * 1024
#: Small files keep their bytes in a snapshot so allowances can inspect them.
KEEP_LIMIT: Final = 4 * 1024 * 1024

_SAFE_HOME = re.compile(r"[A-Za-z0-9_./+-]+")
_HEX = "0123456789abcdef"


class GoldenProfileError(Exception):
    """The profile directory itself is malformed."""


# -- the sandbox ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Profile:
    """One materialized profile. ``home`` is its sandbox ``$HOME``."""

    source: Path
    root: Path
    home: Path
    original_home: str
    #: Outside the home on purpose: sockets and lifecycle locks are ephemeral
    #: by design and not part of the profile. Kept short, because a Unix
    #: socket path is limited to 108 bytes and pytest's tmp paths are long.
    runtime_dir: Path

    @property
    def config_home(self) -> Path:
        return self.home / XDG_LAYOUT["config"]

    @property
    def state_home(self) -> Path:
        return self.home / XDG_LAYOUT["state"]

    @property
    def cache_home(self) -> Path:
        return self.home / XDG_LAYOUT["cache"]

    @property
    def data_home(self) -> Path:
        return self.home / ".local" / "share"

    @property
    def app_state(self) -> Path:
        return self.state_home / "wall-in-one"

    @property
    def app_config(self) -> Path:
        return self.config_home / "wall-in-one"

    def environment(self) -> dict[str, str]:
        return {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config_home),
            "XDG_STATE_HOME": str(self.state_home),
            "XDG_CACHE_HOME": str(self.cache_home),
            "XDG_DATA_HOME": str(self.data_home),
            "XDG_RUNTIME_DIR": str(self.runtime_dir),
        }

    def to_original(self, value: str) -> str:
        """Map a sandbox path back to the profile's own spelling."""
        return _HOME_PATTERN(str(self.home)).sub(self.original_home, value)


def _HOME_PATTERN(home: str) -> re.Pattern[str]:  # noqa: N802 - reads as a constant
    # A path component boundary must follow, so `/home/user` never matches
    # inside `/home/username`.
    return re.compile(re.escape(home) + r"(?![^/\"'\s\\\]])")


# -- the profile manifest -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entry:
    """One filesystem object to create, by absolute path in the original home."""

    path: str
    kind: Literal["file", "dir", "symlink"]
    mode: int
    mtime_ns: int
    data: bytes = b""
    sparse_size: int = 0
    target: str = ""


def _mode(raw: object, default: int) -> int:
    if raw is None:
        return default
    if not isinstance(raw, str) or not raw or any(c not in "01234567" for c in raw):
        raise GoldenProfileError(f"mode must be an octal string, not {raw!r}")
    return int(raw, 8)


def _mtime(raw: object) -> int:
    if raw is None:
        return DEFAULT_MTIME_NS
    if type(raw) is not int or raw < 0:
        raise GoldenProfileError(f"mtime_ns must be a non-negative integer, not {raw!r}")
    return raw


def tiny_png(rgb: Sequence[int], *, size: int = 2) -> bytes:
    """A valid ``size`` x ``size`` RGB PNG of one colour."""
    red, green, blue = (int(channel) & 0xFF for channel in rgb)
    row = b"\x00" + bytes((red, green, blue)) * size
    raw = row * size

    def chunk(kind: bytes, body: bytes) -> bytes:
        checksum = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def read_manifest(source: Path) -> dict[str, Any]:
    raw = json.loads((source / MANIFEST).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        raise GoldenProfileError(f"{source / MANIFEST} is not a format-{FORMAT} profile")
    home = raw.get("home")
    if not isinstance(home, str) or not home.startswith("/") or home.endswith("/"):
        raise GoldenProfileError("profile home must be an absolute path")
    return raw


def entries(source: Path) -> tuple[str, tuple[Entry, ...]]:
    """Everything the profile directory says to create, in creation order."""
    manifest = read_manifest(source)
    home: str = manifest["home"]
    tree = manifest.get("tree", {})
    if not isinstance(tree, dict):
        raise GoldenProfileError("profile tree must be an object")
    found: dict[str, Entry] = {}

    for top, relative_home in XDG_LAYOUT.items():
        base = source / top
        if not base.is_dir():
            continue
        for directory, directories, files in os.walk(base):
            directories.sort()
            for name in [*sorted(directories), *sorted(files)]:
                on_disk = Path(directory) / name
                relative = on_disk.relative_to(source).as_posix()
                options = tree.get(relative, {})
                original = f"{home}/{relative_home}/{on_disk.relative_to(base).as_posix()}"
                if on_disk.is_symlink():
                    raise GoldenProfileError(f"{relative}: describe symlinks in profile.json")
                if on_disk.is_dir():
                    found[original] = Entry(
                        original, "dir", _mode(options.get("mode"), 0o700), DEFAULT_MTIME_NS
                    )
                else:
                    found[original] = Entry(
                        original,
                        "file",
                        _mode(options.get("mode"), 0o600),
                        _mtime(options.get("mtime_ns")),
                        data=on_disk.read_bytes(),
                    )
    for relative, options in sorted(tree.items()):
        if not isinstance(options, dict):
            raise GoldenProfileError(f"profile tree entry {relative!r} must be an object")
        if options.get("type") != "dir":
            if not (source / relative).exists():
                raise GoldenProfileError(f"profile tree names a missing file: {relative}")
            continue
        top, _, rest = relative.partition("/")
        if top not in XDG_LAYOUT or not rest:
            raise GoldenProfileError(f"profile tree path outside config/state/cache: {relative}")
        original = f"{home}/{XDG_LAYOUT[top]}/{rest}"
        found[original] = Entry(original, "dir", _mode(options.get("mode"), 0o700), 0)

    library = manifest.get("library", [])
    if not isinstance(library, list):
        raise GoldenProfileError("profile library must be a list")
    for raw in library:
        if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
            raise GoldenProfileError(f"malformed library entry {raw!r}")
        path: str = raw["path"]
        if not path.startswith(home + "/"):
            raise GoldenProfileError(f"library entry outside the profile home: {path}")
        kind = raw.get("type")
        mode_default = 0o755 if kind == "dir" else 0o644
        mode = _mode(raw.get("mode"), mode_default)
        mtime = _mtime(raw.get("mtime_ns"))
        if kind == "dir":
            found[path] = Entry(path, "dir", mode, mtime)
        elif kind == "symlink":
            target = raw.get("target")
            if not isinstance(target, str):
                raise GoldenProfileError(f"symlink without a target: {path}")
            found[path] = Entry(path, "symlink", 0o777, mtime, target=target)
        elif kind == "image":
            found[path] = Entry(path, "file", mode, mtime, data=tiny_png(raw.get("rgb", (0,) * 3)))
        elif kind == "text":
            text = raw.get("text")
            if not isinstance(text, str):
                raise GoldenProfileError(f"text entry without text: {path}")
            found[path] = Entry(path, "file", mode, mtime, data=text.encode("utf-8"))
        elif kind == "base64":
            found[path] = Entry(path, "file", mode, mtime, data=base64.b64decode(raw["data"]))
        elif kind == "sparse":
            size = raw.get("size")
            if type(size) is not int or size < 0:
                raise GoldenProfileError(f"sparse entry without a size: {path}")
            found[path] = Entry(path, "file", mode, mtime, sparse_size=size)
        else:
            raise GoldenProfileError(f"unknown library entry type {kind!r} for {path}")
    return home, tuple(found[key] for key in sorted(found))


# -- relocation -----------------------------------------------------------------


def _path_tokens(old: str, new: str) -> Iterator[tuple[str, str]]:
    """Values this build derives from an absolute path, old and new."""
    before = hashlib.sha256(os.fsencode(old)).hexdigest()
    after = hashlib.sha256(os.fsencode(new)).hexdigest()
    # `pairing.automatic_still_stem`: the generated still's basename.
    yield f"video-{before[:24]}", f"video-{after[:24]}"
    # `runtime_config.entry_id_for_source`: the generated All-media entry id.
    yield before[:16], after[:16]


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _strings(key)
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _decoded_text(data: bytes) -> str | None:
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _embedded_paths(text: str, home: str) -> Iterator[str]:
    """Absolute paths a JSON/TOML document names inside the profile home."""
    document: object = None
    try:
        document = json.loads(text)
    except ValueError:
        try:
            document = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            document = None
    if document is not None:
        for value in _strings(document):
            if value == home or value.startswith(home + "/"):
                yield value
        return
    yield from re.findall(re.escape(home) + r"/[^\"'\n]*", text)


class Relocation:
    """Rewrite one home prefix, and the path-derived tokens, to another."""

    def __init__(self, old_home: str, new_home: str, paths: Iterable[str]) -> None:
        self.old_home = old_home
        self.new_home = new_home
        self._home = _HOME_PATTERN(old_home)
        tokens: dict[str, str] = {}
        for old in paths:
            new = self.path(old)
            if new == old:
                continue
            for before, after in _path_tokens(old, new):
                tokens.setdefault(before, after)
        self.tokens = tokens
        ordered = sorted(tokens, key=len, reverse=True)
        self._token = (
            re.compile(
                "(?<![0-9a-f])(" + "|".join(re.escape(token) for token in ordered) + ")(?![0-9a-f])"
            )
            if ordered
            else None
        )

    def path(self, value: str) -> str:
        return self._home.sub(self.new_home, value)

    def text(self, value: str) -> str:
        moved = self._home.sub(self.new_home, value)
        if self._token is None:
            return moved
        return self._token.sub(lambda match: self.tokens[match.group(1)], moved)

    def name(self, value: str) -> str:
        if self._token is None:
            return value
        return self._token.sub(lambda match: self.tokens[match.group(1)], value)

    def full_path(self, value: str) -> str:
        """A created object's new absolute path, tokens in basenames included."""
        moved = self.path(value)
        relative = moved[len(self.new_home) :]
        return self.new_home + "/".join(self.name(part) for part in relative.split("/"))


def materialize(source: Path, root: Path, runtime_dir: Path) -> Profile:
    """Create the sandbox ``root/home`` from ``source`` and return it.

    ``runtime_dir`` becomes ``XDG_RUNTIME_DIR``; it must exist and be private.
    """
    home = (root / "home").absolute()
    if not _SAFE_HOME.fullmatch(str(home)):
        raise GoldenProfileError(f"sandbox home needs no escaping in JSON or TOML: {home}")
    original_home, wanted = entries(source)
    if home.exists():
        raise GoldenProfileError(f"sandbox home already exists: {home}")

    paths: set[str] = set()
    for entry in wanted:
        paths.add(entry.path)
        parent = entry.path
        while parent != original_home and parent.startswith(original_home + "/"):
            parent = parent.rsplit("/", 1)[0]
            paths.add(parent)
        text = _decoded_text(entry.data) if entry.kind == "file" else None
        if text is not None:
            paths.update(_embedded_paths(text, original_home))
    relocation = Relocation(original_home, str(home), paths)

    home.mkdir(parents=True)
    for xdg in XDG_LAYOUT.values():
        (home / xdg).mkdir(parents=True, exist_ok=True)
    runtime_dir.chmod(0o700)

    directory_modes: list[tuple[Path, int]] = []
    for entry in wanted:
        target = Path(relocation.full_path(entry.path))
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == "dir":
            target.mkdir(exist_ok=True)
            directory_modes.append((target, entry.mode))
        elif entry.kind == "symlink":
            target.symlink_to(relocation.text(entry.target))
        else:
            text = _decoded_text(entry.data)
            data = relocation.text(text).encode("utf-8") if text is not None else entry.data
            with open(target, "wb") as handle:
                handle.write(data)
                if entry.sparse_size:
                    handle.truncate(entry.sparse_size)
            target.chmod(entry.mode)
            os.utime(target, ns=(entry.mtime_ns, entry.mtime_ns))
    reseal_deployed_markers(home / XDG_LAYOUT["state"] / "wall-in-one")
    # Deepest first, so a read-only directory cannot block its own children.
    for directory, mode in sorted(directory_modes, key=lambda item: -len(item[0].parts)):
        directory.chmod(mode)
    return Profile(
        source=source,
        root=root,
        home=home,
        original_home=original_home,
        runtime_dir=runtime_dir,
    )


def _rewrite_preserving(path: Path, data: bytes) -> None:
    status = path.stat()
    path.write_bytes(data)
    path.chmod(stat.S_IMODE(status.st_mode))
    os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns))


def reseal_deployed_markers(app_state: Path) -> None:
    """Re-derive the capture-adoption identity after its paths moved.

    ``adoption_id`` and ``bindings_sha256`` hash the manifest's paths, and the
    completion marker pins the manifest's bytes.  Both are rebuilt with
    :mod:`wall_in_one.library.adopted`'s own renderers, so whatever the
    profile recorded is re-attested exactly as this build would write it.
    """
    from wall_in_one.library import adopted

    manifest_path = app_state / "deployed-capture-adoption-v1.json"
    completion_path = app_state / "deployed-upgrade-v1.json"
    if not manifest_path.is_file():
        return
    document = json.loads(manifest_path.read_bytes())

    def identity(value: object) -> tuple[int, int]:
        assert isinstance(value, list) and len(value) == 2
        return int(value[0]), int(value[1])

    def fingerprint(value: object) -> tuple[int, int, int, int, int]:
        assert isinstance(value, list) and len(value) == 5
        return (int(value[0]), int(value[1]), int(value[2]), int(value[3]), int(value[4]))

    authorities = tuple(
        adopted.Authority(
            source_path=Path(binding["source_path"]),
            capture_path=Path(binding["capture_path"]),
            source_fingerprint=fingerprint(binding["source_fingerprint"]),
            capture_fingerprint=fingerprint(binding["capture_fingerprint"]),
            capture_size=int(binding["capture_size"]),
            capture_sha256=str(binding["capture_sha256"]),
        )
        for binding in document["bindings"]
    )
    adoption = adopted.Adoption(
        adoption_id="",
        root=Path(document["root"]),
        root_identity=identity(document["root_identity"]),
        automatic_stills=Path(document["automatic_stills"]),
        automatic_stills_identity=identity(document["automatic_stills_identity"]),
        marker_path=Path(document["marker_path"]),
        marker_fingerprint=fingerprint(document["marker_fingerprint"]),
        marker_sha256=str(document["marker_sha256"]),
        authorities=authorities,
    )
    adoption = replace(adoption, adoption_id=adopted.adoption_id_for(adoption))
    manifest = adopted.render_manifest(adoption)
    _rewrite_preserving(manifest_path, manifest)
    if completion_path.is_file():
        completion = json.loads(completion_path.read_bytes())
        completion["adoption_id"] = adoption.adoption_id
        completion["root"] = str(adoption.root)
        completion["authority_sha256"] = hashlib.sha256(manifest).hexdigest()
        _rewrite_preserving(completion_path, adopted.canonical_bytes(completion))


# -- snapshots ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Node:
    kind: Literal["file", "dir", "symlink", "other"]
    mode: int
    inode: int
    mtime_ns: int
    size: int
    digest: str = ""
    content: bytes | None = field(default=None, repr=False)
    target: str = ""


def snapshot(root: Path) -> dict[str, Node]:
    """Every object below ``root`` (dotfiles included), keyed by relative path."""
    found: dict[str, Node] = {}

    def visit(directory: Path) -> None:
        with os.scandir(directory) as listing:
            children = sorted(listing, key=lambda child: child.name)
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            status = child.stat(follow_symlinks=False)
            mode = stat.S_IMODE(status.st_mode)
            if stat.S_ISLNK(status.st_mode):
                found[relative] = Node(
                    "symlink", mode, status.st_ino, 0, 0, target=os.readlink(path)
                )
            elif stat.S_ISDIR(status.st_mode):
                found[relative] = Node("dir", mode, status.st_ino, 0, 0)
                visit(path)
            elif stat.S_ISREG(status.st_mode):
                digest = ""
                content: bytes | None = None
                if status.st_size <= HASH_LIMIT:
                    data = path.read_bytes()
                    digest = hashlib.sha256(data).hexdigest()
                    if status.st_size <= KEEP_LIMIT:
                        content = data
                found[relative] = Node(
                    "file",
                    mode,
                    status.st_ino,
                    status.st_mtime_ns,
                    status.st_size,
                    digest,
                    content,
                )
            else:
                found[relative] = Node("other", mode, status.st_ino, 0, 0)

    visit(root)
    return found


ChangeKind = Literal["created", "deleted", "modified", "rewritten", "mode", "type"]


@dataclass(frozen=True, slots=True)
class Change:
    """One write observed between two snapshots.

    ``rewritten`` is an atomic replacement (new inode or mtime) whose bytes
    happen to be identical. It is still a write, and still needs a reason.
    """

    path: str
    kind: ChangeKind
    before: Node | None
    after: Node | None

    def describe(self) -> str:
        detail = ""
        if self.kind == "modified" and self.before and self.after:
            detail = f" ({self.before.size} -> {self.after.size} bytes)"
            if self.before.content is not None and self.after.content is not None:
                detail += "\n" + _short_diff(self.before.content, self.after.content)
        elif self.kind == "mode" and self.before and self.after:
            detail = f" ({self.before.mode:o} -> {self.after.mode:o})"
        return f"{self.kind}: {self.path}{detail}"


def _short_diff(before: bytes, after: bytes) -> str:
    import difflib

    old = _decoded_text(before)
    new = _decoded_text(after)
    if old is None or new is None:
        return "    <binary>"
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=1))[2:]
    shown = lines[:40]
    if len(lines) > len(shown):
        shown.append(f"... {len(lines) - len(shown)} more diff lines")
    return "\n".join("    " + line for line in shown)


def diff(before: Mapping[str, Node], after: Mapping[str, Node]) -> list[Change]:
    changes: list[Change] = []
    for path in sorted(set(before) | set(after)):
        old = before.get(path)
        new = after.get(path)
        if old is None:
            changes.append(Change(path, "created", None, new))
        elif new is None:
            changes.append(Change(path, "deleted", old, None))
        elif old.kind != new.kind:
            changes.append(Change(path, "type", old, new))
        elif old.kind == "file":
            if old.digest != new.digest or old.size != new.size:
                changes.append(Change(path, "modified", old, new))
            elif old.inode != new.inode or old.mtime_ns != new.mtime_ns:
                changes.append(Change(path, "rewritten", old, new))
            elif old.mode != new.mode:
                changes.append(Change(path, "mode", old, new))
        elif old.kind == "symlink":
            if old.target != new.target:
                changes.append(Change(path, "modified", old, new))
        elif old.mode != new.mode:
            changes.append(Change(path, "mode", old, new))
    return changes


@dataclass(frozen=True, slots=True)
class Allowance:
    """One expected write, with the reason it is needed.

    ``verify`` sees the change and must raise if its *content* is wrong: an
    allowance is permission for one specific edit, never for any bytes.
    """

    pattern: str
    kinds: frozenset[ChangeKind]
    reason: str
    verify: Callable[[Change], None] | None = None

    def matches(self, change: Change) -> bool:
        return change.kind in self.kinds and fnmatchcase(change.path, self.pattern)


def check_changes(changes: Sequence[Change], allowances: Sequence[Allowance]) -> None:
    """Fail with every unexpected write listed, after verifying expected ones."""
    unexpected: list[str] = []
    for change in changes:
        allowance = next((rule for rule in allowances if rule.matches(change)), None)
        if allowance is None:
            unexpected.append(change.describe())
            continue
        if allowance.verify is not None:
            try:
                allowance.verify(change)
            except AssertionError as error:
                unexpected.append(
                    f"{change.describe()}\n    allowed ({allowance.reason}) but {error}"
                )
    if unexpected:
        raise AssertionError(
            f"{len(unexpected)} unexpected write(s) to the profile:\n" + "\n".join(unexpected)
        )


# -- runtime.toml semantics ---------------------------------------------------------

PROGRAM_KEYS: Final = (
    "noctalia_program",
    "niri_program",
    "mpvpaper_program",
    "linux_wallpaperengine_program",
)


def runtime_document_semantics(text: str, profile: Profile) -> dict[str, Any]:
    """``runtime.toml`` minus what only says *which machine* compiled it.

    Dropped: ``config_generation`` (a hash of the rest), and the directories
    of the four renderer programs (``shutil.which`` on the compiling machine;
    the basename stays).  Sandbox paths are spelled in the profile's original
    home, so a document compiled here compares equal to the stored one.
    Everything else -- every playlist, entry id, still, motion, palette,
    taboo, schedule and setting -- must be identical.
    """
    document = tomllib.loads(profile.to_original(text))
    document.pop("config_generation", None)
    renderer = document.get("renderer", {})
    for key in PROGRAM_KEYS:
        if isinstance(renderer.get(key), str):
            renderer[key] = renderer[key].rsplit("/", 1)[-1]
    return document


# -- the fake runtime ---------------------------------------------------------------


@dataclass
class FakeRuntime:
    """Answers ``status``/``reload`` the way a healthy Rust service would.

    Its status always reports the generation of the ``runtime.toml`` on disk,
    i.e. a service that has loaded whatever was last published.
    """

    taboo: list[dict[str, object]] = field(default_factory=list)
    verbs: list[str] = field(default_factory=list)

    def status(self) -> dict[str, object]:
        from wall_in_one import paths, runtime_config, runtime_health

        target = paths.runtime_config_path()
        document = target.read_bytes() if target.is_file() else b""
        generation = runtime_config.read_config_generation(target) if document else "0" * 64
        return {
            "status_version": 2,
            "supported_config_schemas": [4, 5],
            "config_generation": generation,
            "config_path": str(target.absolute()),
            "loaded_config_sha256": hashlib.sha256(document).hexdigest(),
            "runtime_instance": "a" * runtime_health.RUNTIME_INSTANCE_HEX_CHARS,
            "config_epoch": 1,
            "playlist_id": runtime_config.FALLBACK_PLAYLIST_ID,
            "playlist": runtime_config.FALLBACK_PLAYLIST_NAME,
            "source": "schedule",
            "taboo_entries": [dict(report) for report in self.taboo],
            "taboo_entries_omitted": 0,
        }

    def send_runtime(
        self,
        verb: str,
        argument: str | None = None,
        *,
        timeout: float | None = None,
        cancellation: object = None,
    ) -> Any:
        from wall_in_one.control.protocol import Response

        del timeout, cancellation
        self.verbs.append(verb if argument is None else f"{verb} {argument}")
        if verb == "status":
            return Response.success(json.dumps(self.status()))
        if verb == "reload":
            return Response.success("reloaded")
        raise AssertionError(f"the golden profile run sent an unexpected runtime verb: {verb!r}")


@contextlib.contextmanager
def serve(runtime: FakeRuntime, path: Path) -> Iterator[None]:
    """Answer on a real Unix socket, for a child process (an older build).

    In-process callers patch ``client.send_runtime`` instead; this exists for
    code that cannot be patched because it runs in another interpreter.
    """
    import socketserver
    import threading

    from wall_in_one.control.protocol import Request

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            request = Request.decode(self.rfile.readline(1024 * 1024))
            self.wfile.write(runtime.send_runtime(request.verb, request.argument).encode())

    server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        path.unlink(missing_ok=True)


# -- the idle sequence --------------------------------------------------------------


def assert_no_dangling_references() -> tuple[str, ...]:
    """The read half of ``ui.app.App._repair_dangling_playlist_references``.

    That repair runs on every graphical start, but lives in the GTK module.
    It writes only when a schedule, display or the default names a playlist
    that no longer exists, so on a consistent profile it is these reads. A
    store it cannot read (unreadable or newer) pauses it instead; those are
    returned, as the app reports them.
    """
    from wall_in_one import config
    from wall_in_one.library import displays, playlists, schedules

    playlist_store = playlists.Store.open()
    if playlist_store.fault is not None:
        return (f"playlists: {playlist_store.fault}",)
    valid = {playlist.id for playlist in playlist_store.all()}
    paused: list[str] = []
    schedule_store = schedules.Store.open()
    if schedule_store.fault is not None:
        paused.append(f"schedule rules: {schedule_store.fault}")
    else:
        assert {rule.playlist for rule in schedule_store.rules} <= valid
    display_store = displays.Store.open()
    if display_store.fault is not None:
        paused.append(f"display assignments: {display_store.fault}")
    else:
        assert {playlist for _connector, playlist in display_store.all()} <= valid
    active = config.load_strict().active_playlist
    assert not active or active in valid, active
    return tuple(paused)


def compile_like_the_gui(library: object) -> str:
    """``ui.app.App._compile_runtime_request`` without GTK: same locks, same calls.

    Returns ``changed``, ``unchanged`` or the refusal, which the app reports
    in its window rather than raising.
    """
    from wall_in_one import config, legacy_migration, runtime_config
    from wall_in_one.library.model import Library
    from wall_in_one.session import Session

    assert isinstance(library, Library)
    with legacy_migration.unattended_transaction(), runtime_config.compiler_lock():
        settings = config.load_strict()
        snapshot = Session(settings)
        try:
            snapshot.adopt_library(library, reconcile_workshop=False)
            changed = runtime_config.update(settings, snapshot)
        except runtime_config.RuntimeConfigError as error:
            return f"refused: {error}"
        finally:
            snapshot.shutdown()
    return "changed" if changed else "unchanged"


@dataclass(frozen=True, slots=True)
class IdleRun:
    """What one idle start reported; the writes are judged separately."""

    service_prepare: int
    gui_compile: str
    health_sync: int
    health_sync_on_stop: int
    library_size: int
    skipped: int
    #: As ``App._report_newer_version_files`` would show them.
    newer_version_files: tuple[str, ...]
    #: As ``App`` keeps them from ``config.load_document`` (read-only settings).
    unknown_settings: tuple[str, ...]
    #: Why the dangling-reference repair paused, if it did.
    repair_paused: tuple[str, ...]


def run_idle() -> IdleRun:
    """Start the service and the app, idle through one health sync, close.

    Every step is the production entry point the packaged install runs:
    ``cli._run_graphical_startup_upgrade`` (before GTK reads anything), the
    service unit's ``--service-startup-prepare``, the application's own
    settings, Session, Library and Store construction and first refresh, its
    runtime publication, the palette resolution, the 30 s timer's
    ``--sync-runtime-health``, and the unit's ``--sync-runtime-health-on-stop``.
    Refusals are recorded, not raised: a read-only profile must still idle.
    """
    from wall_in_one import cli, config, legacy_migration
    from wall_in_one.session import Session
    from wall_in_one.theme import source

    blocked = cli._run_graphical_startup_upgrade(require_legacy_safe=False, retry=None)
    assert blocked is None, f"the pre-GTK startup gate refused to open: {blocked}"
    service_prepare = cli.main(["--service-startup-prepare"])

    loaded = config.load_document()
    session = Session(loaded.settings)
    try:
        found = legacy_migration.probe()
        assert not found.needs_decision, found.detail
        paused = assert_no_dangling_references()
        plan = session.prepare_library_refresh()
        session.adopt_library_refresh(plan.run())
        newer = session.newer_version_files()
        compiled = compile_like_the_gui(session.library)
        source.resolve()
        health_sync = cli.main(["--sync-runtime-health"])
    finally:
        session.shutdown()
    health_sync_on_stop = cli.main(["--sync-runtime-health-on-stop"])
    return IdleRun(
        service_prepare=service_prepare,
        gui_compile=compiled,
        health_sync=health_sync,
        health_sync_on_stop=health_sync_on_stop,
        library_size=len(session.library.items),
        skipped=len(session.library.skipped),
        newer_version_files=tuple(newer),
        unknown_settings=tuple(loaded.unknown_keys),
        repair_paused=paused,
    )
