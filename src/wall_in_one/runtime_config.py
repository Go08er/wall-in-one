"""Compile the rich application model into the Rust service's strict TOML.

This is intentionally a compiler rather than another store. It reads the
library and authoring stores the Python app already owns, resolves every entry,
and atomically replaces one throw-away runtime document. The service never
imports this module and never reads those source stores.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from wall_in_one import config, file_io, paths, runtime_compatibility
from wall_in_one.library import pairings, state_file
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.library.playlists import Entry, Playlist
from wall_in_one.session import Session
from wall_in_one.wallpaper import scenes

SCHEMA_VERSION: Final = 4
BATTERY_SCHEMA_VERSION: Final = 5

#: Per-playlist interval/shuffle and a display's own playlist beating global
#: schedule rules live beside ``runtime.toml`` in this file, never in it, so
#: ``runtime.toml`` keeps exactly the shape every released service loads. It
#: exists only while one of them is in use; a service that predates it never
#: opens it.
OVERRIDES_FILENAME: Final = "runtime-overrides.toml"
OVERRIDES_SCHEMA_VERSION: Final = 1
MAX_RUNTIME_OVERRIDES_BYTES: Final = 1024 * 1024
FALLBACK_PLAYLIST_ID: Final = "all-media"
FALLBACK_PLAYLIST_NAME: Final = "All media"

# Versioned Rust wire bounds. The compiler must enforce these before atomic
# installation: publishing syntactically valid TOML that the service rejects
# would stop rotation while destroying the previous working document.
MAX_RUNTIME_CONFIG_BYTES: Final = 8 * 1024 * 1024
MAX_PLAYLISTS: Final = 578
MAX_ENTRIES_PER_PLAYLIST: Final = 10_000
MAX_SCHEDULES: Final = 512
MAX_DISPLAYS: Final = 64
MAX_PLAYLIST_NAME_CHARS: Final = 120
MAX_IDENTIFIER_BYTES: Final = 256
MAX_REFERENCE_BYTES: Final = MAX_PLAYLIST_NAME_CHARS * 4
MAX_CONNECTOR_BYTES: Final = 256
MAX_OPTION_BYTES: Final = 256
MAX_PATH_BYTES: Final = 4096
CONFIG_GENERATION_HEX_CHARS: Final = 64
MAX_RUNTIME_RESPONSE_BYTES: Final = 1024 * 1024
STATUS_STRUCTURAL_RESERVE_BYTES: Final = 256 * 1024
STATUS_DIAGNOSTIC_RESERVE_BYTES: Final = 64 * 1024
STATUS_JSON_ESCAPE_FACTOR: Final = 4
MAX_LIVE_OUTPUTS: Final = 64
COMPILER_LOCK_TIMEOUT_SECONDS: Final = 5.0
COMPILER_LOCK_POLL_SECONDS: Final = 0.025


class RuntimeConfigError(Exception):
    """The resolved document could not be produced or installed."""


class RuntimeConfigNotDurableError(RuntimeConfigError):
    """A file was replaced (or removed), but syncing its folder then failed.

    The runtime equivalent of :class:`state_file.PublishedNotDurableError`.
    Every reader, the service's watcher included, already sees the new
    files; only their survival across a power loss is unconfirmed. It is not
    a refusal: what is on disk is the new pair, and nothing may be put back
    on its account.
    """

    def __init__(self, published: tuple[Path, ...], error: OSError) -> None:
        names = " and ".join(path.name for path in published)
        verb = "were" if len(published) > 1 else "was"
        folder = "their folder" if len(published) > 1 else "its folder"
        super().__init__(
            f"{names} {verb} saved, but syncing {folder} failed, so the change may not "
            f"survive a power loss ({error.strerror or error})"
        )
        self.published = published
        self.error = error


@dataclass(slots=True)
class _HeldCompilerLock:
    path: Path
    descriptor: int
    depth: int = 1


class _CompilerLockState(threading.local):
    held: _HeldCompilerLock | None

    def __init__(self) -> None:
        self.held = None


_LOCAL_COMPILER_GATE = threading.RLock()
_COMPILER_LOCK_STATE = _CompilerLockState()


def _compiler_lock_path(target: Path) -> Path:
    return target.absolute().with_name(f".{target.name}.compiler.lock")


def _lock_timeout(path: Path, seconds: float) -> RuntimeConfigError:
    return RuntimeConfigError(
        f"timed out after {seconds:g}s waiting for runtime compiler lock {path}; "
        "the last-known-good runtime configuration was left untouched"
    )


def _open_compiler_lock(path: Path) -> int:
    try:
        paths.ensure_directory(path.parent)
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as error:
        raise RuntimeConfigError(
            f"cannot safely open runtime compiler lock {path}: {error}"
        ) from error

    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeConfigError(f"runtime compiler lock {path} is not a regular file")
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeConfigError(f"runtime compiler lock {path} changed while being opened")
        if opened.st_uid != os.getuid() or opened.st_nlink != 1:
            raise RuntimeConfigError(
                f"runtime compiler lock {path} is not a private file owned by this user"
            )
        os.fchmod(descriptor, 0o600)
        return descriptor
    except OSError, RuntimeConfigError:
        os.close(descriptor)
        raise


def _verify_open_lock(path: Path, descriptor: int) -> None:
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
    except OSError as error:
        raise RuntimeConfigError(f"cannot verify runtime compiler lock {path}: {error}") from error
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise RuntimeConfigError(f"runtime compiler lock {path} changed while waiting for it")


@contextmanager
def compiler_lock(
    target: Path | None = None,
    *,
    timeout: float | None = None,
) -> Iterator[None]:
    """Serialise every complete authoring-snapshot compilation.

    The headless compiler holds this before it reads settings or authoring
    stores. ``write`` and ``update`` enter it too, making direct and GUI callers
    safe while remaining re-entrant on the same thread and target.
    """

    document = target if target is not None else paths.runtime_config_path()
    lock_path = _compiler_lock_path(document)
    wait = COMPILER_LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    if wait < 0:
        raise RuntimeConfigError("runtime compiler lock timeout cannot be negative")
    deadline = time.monotonic() + wait

    remaining = max(0.0, deadline - time.monotonic())
    if not _LOCAL_COMPILER_GATE.acquire(timeout=remaining):
        raise _lock_timeout(lock_path, wait)
    try:
        held = _COMPILER_LOCK_STATE.held
        if held is not None:
            if held.path != lock_path:
                raise RuntimeConfigError(
                    f"cannot acquire runtime compiler lock {lock_path} while already holding "
                    f"{held.path}"
                )
            held.depth += 1
            try:
                yield
            finally:
                held.depth -= 1
            return

        descriptor = _open_compiler_lock(lock_path)
        locked = False
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _lock_timeout(lock_path, wait) from None
                    time.sleep(min(COMPILER_LOCK_POLL_SECONDS, remaining))
                except OSError as error:
                    raise RuntimeConfigError(
                        f"cannot acquire runtime compiler lock {lock_path}: {error}"
                    ) from error
            _verify_open_lock(lock_path, descriptor)
            _COMPILER_LOCK_STATE.held = _HeldCompilerLock(lock_path, descriptor)
            try:
                yield
            finally:
                _COMPILER_LOCK_STATE.held = None
        finally:
            try:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
    finally:
        _LOCAL_COMPILER_GATE.release()


def _encoded_length(value: str, *, label: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise RuntimeConfigError(f"{label} must be valid UTF-8 text") from error


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or 0x7F <= ord(character) <= 0x9F for character in value)


def _bounded_text(
    value: str,
    *,
    label: str,
    maximum_bytes: int,
    optional: bool = False,
) -> None:
    if optional and not value:
        return
    if not value.strip():
        raise RuntimeConfigError(f"{label} cannot be empty")
    if _encoded_length(value, label=label) > maximum_bytes:
        raise RuntimeConfigError(f"{label} must be at most {maximum_bytes} UTF-8 bytes")
    if _has_control(value):
        raise RuntimeConfigError(f"{label} cannot contain control characters")


def _bounded_connector(value: str, *, label: str, optional: bool = False) -> None:
    _bounded_text(
        value,
        label=label,
        maximum_bytes=MAX_CONNECTOR_BYTES,
        optional=optional,
    )
    if value and any(character.isspace() for character in value):
        raise RuntimeConfigError(f"{label} cannot contain whitespace")


def _bounded_name(value: str, *, label: str) -> None:
    if not value.strip():
        raise RuntimeConfigError(f"{label} cannot be empty")
    if len(value) > MAX_PLAYLIST_NAME_CHARS:
        raise RuntimeConfigError(f"{label} must be at most {MAX_PLAYLIST_NAME_CHARS} characters")
    if _has_control(value):
        raise RuntimeConfigError(f"{label} cannot contain control characters")
    _encoded_length(value, label=label)


def _absolute_path(value: Path, *, label: str) -> None:
    if not value.is_absolute():
        raise RuntimeConfigError(f"{label} must be an absolute path: {value}")
    _bounded_text(str(value), label=label, maximum_bytes=MAX_PATH_BYTES)


def _quote(value: str | Path) -> str:
    # TOML basic strings and JSON strings share the escaping needed here.
    return json.dumps(str(value), ensure_ascii=False)


def _semantic_generation(document_without_generation: str) -> str:
    """Hash the canonical compiled body before its identity field is inserted."""
    return hashlib.sha256(document_without_generation.encode("utf-8")).hexdigest()


def _validate_status_budget(document_without_generation: str) -> None:
    """Mirror Rust's conservative bound for the atomic JSON status snapshot.

    A runtime document may fit the 8 MiB configuration limit while its expanded
    playlist/schedule/display inventory cannot fit one protocol response.  The
    compiler must reject that document *before* replacing the last-known-good
    file; otherwise reload rolls back only in memory and the next service start
    refuses the bytes left on disk.
    """
    try:
        decoded = tomllib.loads(document_without_generation)
    except (tomllib.TOMLDecodeError, RecursionError) as error:  # pragma: no cover
        raise RuntimeConfigError(
            f"cannot parse generated runtime configuration: {error}"
        ) from error

    raw_playlists = decoded.get("playlists", [])
    raw_schedules = decoded.get("schedules", [])
    raw_displays = decoded.get("displays", [])
    if (
        not isinstance(raw_playlists, list)
        or not isinstance(raw_schedules, list)
        or not isinstance(raw_displays, list)
    ):
        raise RuntimeConfigError("generated runtime inventory has an invalid shape")

    configured_text = 0
    largest_playlist_identity = 0
    largest_entry = 0
    playlist_identity: dict[str, tuple[str, str]] = {}
    for raw in raw_playlists:
        if not isinstance(raw, dict):
            raise RuntimeConfigError("generated playlist inventory has an invalid shape")
        identifier = raw.get("id")
        name = raw.get("name")
        entries = raw.get("entries", [])
        if (
            not isinstance(identifier, str)
            or not isinstance(name, str)
            or not isinstance(entries, list)
        ):
            raise RuntimeConfigError("generated playlist inventory has an invalid shape")
        identity_bytes = _encoded_length(identifier, label="playlist id") + _encoded_length(
            name, label="playlist name"
        )
        configured_text += identity_bytes
        largest_playlist_identity = max(largest_playlist_identity, identity_bytes)
        playlist_identity[identifier] = (identifier, name)
        playlist_identity[name] = (identifier, name)
        for entry in entries:
            if not isinstance(entry, dict):
                raise RuntimeConfigError("generated entry inventory has an invalid shape")
            entry_id = entry.get("id")
            still = entry.get("still")
            if not isinstance(entry_id, str) or not isinstance(still, str):
                raise RuntimeConfigError("generated entry inventory has an invalid shape")
            largest_entry = max(
                largest_entry,
                _encoded_length(entry_id, label="entry id")
                + _encoded_length(still, label="entry still"),
            )

    for raw in raw_schedules:
        if not isinstance(raw, dict):
            raise RuntimeConfigError("generated schedule inventory has an invalid shape")
        identifier = raw.get("id")
        reference = raw.get("playlist")
        connector = raw.get("connector", "")
        start = raw.get("start", "")
        end = raw.get("end", "")
        playlist = playlist_identity.get(reference) if isinstance(reference, str) else None
        if (
            not isinstance(identifier, str)
            or not isinstance(connector, str)
            or not isinstance(start, str)
            or not isinstance(end, str)
            or playlist is None
        ):
            raise RuntimeConfigError("generated schedule inventory has an invalid shape")
        configured_text += sum(
            _encoded_length(value, label="schedule status text")
            for value in (identifier, connector, playlist[0], playlist[1], start, end)
        )

    largest_connector = MAX_CONNECTOR_BYTES
    for raw in raw_displays:
        if not isinstance(raw, dict) or not isinstance(raw.get("connector"), str):
            raise RuntimeConfigError("generated display inventory has an invalid shape")
        largest_connector = max(
            largest_connector,
            _encoded_length(raw["connector"], label="display connector"),
        )

    per_display = largest_connector + largest_playlist_identity * 2 + largest_entry
    configured_text += per_display * (MAX_LIVE_OUTPUTS + MAX_DISPLAYS)
    configured_text += largest_playlist_identity + largest_entry
    encoded_bound = (
        configured_text * STATUS_JSON_ESCAPE_FACTOR
        + STATUS_STRUCTURAL_RESERVE_BYTES
        + STATUS_DIAGNOSTIC_RESERVE_BYTES
    )
    if encoded_bound > MAX_RUNTIME_RESPONSE_BYTES:
        raise RuntimeConfigError(
            "runtime status could exceed the "
            f"{MAX_RUNTIME_RESPONSE_BYTES}-byte protocol response limit"
        )


def document_generation(document: str) -> str:
    """Return a strictly validated generation token from compiled TOML bytes."""
    try:
        decoded = tomllib.loads(document)
    except (tomllib.TOMLDecodeError, RecursionError) as error:
        raise RuntimeConfigError(f"cannot parse runtime configuration: {error}") from error
    generation = decoded.get("config_generation")
    if (
        not isinstance(generation, str)
        or len(generation) != CONFIG_GENERATION_HEX_CHARS
        or any(character not in "0123456789abcdef" for character in generation)
    ):
        raise RuntimeConfigError("runtime configuration has no valid config_generation")
    return generation


def read_config_generation(path: Path | None = None) -> str:
    """Read and validate one app-managed runtime document's generation token.

    Callers which use this as a concurrency guard must hold :func:`compiler_lock`
    across this read and the authoring transaction it protects.
    """
    target = path if path is not None else paths.runtime_config_path()
    try:
        text = file_io.read_regular_text(target, MAX_RUNTIME_CONFIG_BYTES)
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeConfigError(f"cannot read {target}: {error}") from error
    if text is None:
        raise RuntimeConfigError(f"runtime configuration does not exist: {target}")
    try:
        return document_generation(text)
    except RuntimeConfigError as error:
        raise RuntimeConfigError(f"runtime configuration {target}: {error}") from error


def _program(name: str) -> Path:
    found = shutil.which(name)
    if found is not None:
        program = Path(found).resolve()
        _absolute_path(program, label=f"{name} program")
        return program
    # Fully resolved still means absolute when an optional renderer is absent.
    # The service reports spawn failure only when an entry actually needs it.
    program = Path("/usr/bin") / name
    _absolute_path(program, label=f"{name} program")
    return program


def entry_id_for_source(source: Path) -> str:
    """Stable wire id used for one entry in the generated All media list.

    The runtime reports this id back in its atomic status snapshot. Keeping
    the derivation public lets read-only clients map that answer to an authored
    media source without maintaining a second, subtly different hash rule.
    """
    return hashlib.sha256(os.fsencode(source)).hexdigest()[:16]


def _palette(policy: pairings.PalettePolicy, generator: str) -> str:
    mode = policy.mode.value
    if policy.keeps_palette:
        return f'{{ kind = "keep", mode = {_quote(mode)} }}'
    if policy.is_adaptive:
        scheme = policy.adaptive_scheme(generator)
        _bounded_text(scheme, label="adaptive scheme", maximum_bytes=MAX_OPTION_BYTES)
        return f'{{ kind = "adaptive", scheme = {_quote(scheme)}, mode = {_quote(mode)} }}'
    if policy.kind not in ("builtin", "community", "custom") or not policy.name:
        return f'{{ kind = "keep", mode = {_quote(mode)} }}'
    _bounded_text(policy.name, label="palette name", maximum_bytes=MAX_OPTION_BYTES)
    return (
        f'{{ kind = "named", source = {_quote(policy.kind)}, '
        f"name = {_quote(policy.name)}, mode = {_quote(mode)} }}"
    )


def _resolved_entry(
    item: MediaItem,
    session: Session,
    entry_id: str,
) -> tuple[str, ...] | None:
    _bounded_text(entry_id, label="playlist entry id", maximum_bytes=MAX_IDENTIFIER_BYTES)
    bundle = session.pairings.resolve_accepted(item, session.library)
    if bundle.still is None or not bundle.still.is_absolute():
        return None
    _absolute_path(bundle.still, label="playlist entry still")
    lines = [
        "[[playlists.entries]]",
        f"id = {_quote(entry_id)}",
        f"kind = {_quote(item.kind.value)}",
        f"still = {_quote(bundle.still)}",
    ]
    if bundle.health.is_borked:
        _bounded_text(
            bundle.health.reason,
            label="taboo reason",
            maximum_bytes=pairings.MAX_HEALTH_REASON,
        )
        _bounded_text(
            bundle.health.source,
            label="taboo source",
            maximum_bytes=pairings.MAX_HEALTH_SOURCE,
        )
        lines.append(
            "taboo = { reason = "
            f"{_quote(bundle.health.reason)}, source = {_quote(bundle.health.source)} }}"
        )
    if item.kind is Kind.VIDEO:
        if bundle.motion is None or not bundle.motion.is_absolute():
            return None
        _absolute_path(bundle.motion, label="playlist entry motion")
        lines.append(f"motion = {_quote(bundle.motion)}")
    elif item.kind is Kind.SCENE:
        if not item.scene.isdigit():
            return None
        _bounded_text(item.scene, label="scene id", maximum_bytes=MAX_IDENTIFIER_BYTES)
        lines.append(f"scene_id = {_quote(item.scene)}")
    lines.append(f"palette = {_palette(bundle.palette, session.settings.preview_scheme)}")
    return tuple(lines)


# -- what can play: the compiler's own answer, for every page that asks ---------------

#: The id a lone entry is compiled under to ask `_resolved_entry`; never written.
_PROBE_ENTRY_ID: Final = "playability-probe"


@dataclass(frozen=True, slots=True)
class Playability:
    """What one playlist entry gives the runtime, by the compiler's own answer.

    ``compiles``: `_resolved_entry` keeps it, so it becomes an entry in
    ``runtime.toml`` (a library item with a representative still, its video
    for a moving one, a numeric scene id). ``plays``: it compiles and
    playback does not skip it (it is not marked as one the runtime refused).
    ``reason`` says why it doesn't play, for a person.
    """

    compiles: bool
    plays: bool
    reason: str = ""


def entry_playability(item: MediaItem | None, session: Session) -> Playability:
    """Whether ``item`` (``None``: not in the library) compiles and plays.

    The compiler's `_resolved_entry` itself decides, over the session's
    accepted memory (`pairings.Store.resolve_accepted`): no file is read, so
    the GTK thread may ask, and the pages and the compiler cannot disagree.
    """
    if item is None:
        return Playability(False, False, "it is not in the library now")
    try:
        kept = _resolved_entry(item, session, _PROBE_ENTRY_ID) is not None
    except RuntimeConfigError:
        kept = False
    health = session.pairings.health(pairings.Identity.of(item))
    if health.is_borked:
        # The runtime's own refusal says most about why it doesn't play.
        return Playability(kept, False, health.reason or "playback skips it")
    if not kept:
        still = session.pairings.resolve_accepted(item, session.library).still
        reason = (
            "it has no still for playback to show yet"
            if still is None
            else "playback can\u2019t use it"
        )
        return Playability(False, False, reason)
    return Playability(True, True)


def library_index(session: Session) -> dict[Path, MediaItem]:
    """The session's library by path, for asking about many entries at once."""
    return {item.path: item for item in session.library.items}


def playlist_playability(
    entries: Iterable[Entry],
    session: Session,
    known: Mapping[Path, MediaItem] | None = None,
) -> tuple[Playability, ...]:
    """`entry_playability` for each of ``entries``, in order."""
    index = library_index(session) if known is None else known
    return tuple(entry_playability(index.get(entry.path), session) for entry in entries)


def find_playlist(reference: str, session: Session) -> Playlist | None:
    """The playlist ``reference`` names, as `runtime_playlist` resolves it: id, then name."""
    found = session.playlists.get(reference)
    return found if found is not None else session.playlists.by_name(reference)


def use_refusal(reference: str, session: Session) -> str:
    """Why ``reference`` can't be the default, a rule's or a display's playlist; empty if it can.

    The compiler refuses a document naming a playlist it can't build
    (`runtime_playlist`), and the service then keeps the previous one; a
    playlist whose every wallpaper playback skips compiles but never shows
    anything. "All media" can always be used.
    """
    if reference in (FALLBACK_PLAYLIST_ID, FALLBACK_PLAYLIST_NAME):
        return ""
    playlist = find_playlist(reference, session)
    if playlist is None:
        return "That playlist no longer exists"
    entries = playlist_playability(playlist.entries, session)
    if not any(entry.compiles for entry in entries):
        return f"\u201c{playlist.name}\u201d has nothing to play yet; add wallpapers to it first"
    if not any(entry.plays for entry in entries):
        return (
            f"Playback skips everything in \u201c{playlist.name}\u201d, "
            "so it can\u2019t be used here"
        )
    return ""


def leaves_nothing_to_play(before: Sequence[Playability], after: Sequence[Playability]) -> bool:
    """Whether a removal takes away the last thing a used playlist can play.

    Refused when nothing that plays would remain where something did, and
    also when nothing that compiles would remain: a used playlist the
    compiler can't build stops every later publication. A playlist that
    already had nothing playing may still lose skipped entries while one
    that compiles remains, so it can be cleaned up.
    """
    if any(entry.plays for entry in after):
        return False
    return any(entry.plays for entry in before) or not any(entry.compiles for entry in after)


def playlist_uses(pid: str, settings: config.Settings, session: Session) -> tuple[str, ...]:
    """Every saved use of playlist ``pid`` the compiler builds, in words.

    The default, every schedule rule (turned off or aimed at one display,
    which are kept and can come back into force) and every display's own
    playlist (dormant while mirrored, live again in independent mode).
    """

    def names(reference: str) -> bool:
        found = find_playlist(reference, session)
        return found is not None and found.id == pid

    found: list[str] = []
    if settings.active_playlist and names(settings.active_playlist):
        found.append("the default")
    rules = sum(names(rule.playlist) for rule in session.schedules.rules)
    if rules:
        found.append(f"{rules} schedule rule{'' if rules == 1 else 's'}")
    found.extend(
        f"{connector}\u2019s own playlist"
        for connector, assigned in session.displays.all()
        if names(assigned)
    )
    return tuple(found)


def listed(words: Sequence[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + f" and {words[-1]}"


class PlaybackGuardError(ValueError):
    """A change refused because publication, or a playlist in use, would break.

    Raised at the head of the authoring lane (or, in the headless service,
    just before the write). ``kind`` lets a control-socket caller branch on
    it (`control.server.failed`); the message is the whole sentence.
    """

    kind = "nothing-to-play"


def _quoted_name(playlist: Playlist) -> str:
    return f"\u201c{playlist.name}\u201d"


#: `change_problem`: a playlist in use would have nothing left to play.
LEAVES_NOTHING_TO_PLAY: Final = "leaves-nothing-to-play"
#: `change_problem`: a playlist would hold entries, none of which the compiler keeps.
LEAVES_ONLY_UNUSABLE: Final = "leaves-only-unusable"


def change_problem(before: Sequence[Playability], after: Sequence[Playability], used: bool) -> str:
    """What a playlist going from ``before`` to ``after`` would break, or empty.

    The two rules are both the compiler's (`_compile`). A playlist in use
    must keep something to play (`leaves_nothing_to_play`):
    `LEAVES_NOTHING_TO_PLAY`. And any playlist that holds entries must keep
    one the compiler keeps, used or not, because a playlist with entries but
    none it can resolve stops every publication: `LEAVES_ONLY_UNUSABLE`. A
    playlist that already broke that rule may still change, so it can be
    cleaned up. The words are the caller's (`change_refusal` has some).
    """
    if used:
        return LEAVES_NOTHING_TO_PLAY if leaves_nothing_to_play(before, after) else ""
    breaks = after and not any(entry.compiles for entry in after)
    was_fine = not before or any(entry.compiles for entry in before)
    return LEAVES_ONLY_UNUSABLE if breaks and was_fine else ""


def change_refusal(
    label: str,
    before: Sequence[Playability],
    after: Sequence[Playability],
    uses: Sequence[str],
) -> str:
    """`change_problem` for playlist ``label``, used by ``uses`` (in words), as a clause."""
    problem = change_problem(before, after, bool(uses))
    if problem == LEAVES_NOTHING_TO_PLAY:
        return f"{label} (used by {listed(uses)}) would have nothing left to play"
    if problem == LEAVES_ONLY_UNUSABLE:
        return f"{label} would hold only wallpapers playback can\u2019t use"
    return ""


def entries_refusal(
    playlist: Playlist,
    after: Sequence[Entry],
    settings: config.Settings,
    session: Session,
) -> str:
    """`change_refusal` for giving ``playlist`` the entries ``after``, against what is saved."""
    known = library_index(session)
    return change_refusal(
        _quoted_name(playlist),
        playlist_playability(playlist.entries, session, known),
        playlist_playability(after, session, known),
        playlist_uses(playlist.id, settings, session),
    )


def emptied_by_removing(
    sources: Iterable[Path],
    settings: config.Settings,
    session: Session,
    *,
    uses_of: Callable[[Playlist], Sequence[str]] | None = None,
    label_of: Callable[[Playlist], str] = _quoted_name,
) -> list[str]:
    """Each refusal (`change_refusal`) that taking ``sources`` out of the library earns.

    For wallpapers leaving the library: every entry naming one goes with it.
    Checked against what is saved now, so at the head of the authoring lane
    it is the guarantee; earlier it is only advice. ``uses_of`` and
    ``label_of`` put a playlist's uses and name in a page's own words (a
    page may count a default it asked for and that has not saved yet);
    `playlist_uses` and its quoted name otherwise.
    """
    leaving = set(sources)
    known = library_index(session)
    found: list[str] = []
    for playlist in session.playlists.all():
        kept = [entry for entry in playlist.entries if entry.path not in leaving]
        if len(kept) == len(playlist.entries):
            continue
        refusal = change_refusal(
            label_of(playlist),
            playlist_playability(playlist.entries, session, known),
            playlist_playability(kept, session, known),
            (
                uses_of(playlist)
                if uses_of is not None
                else playlist_uses(playlist.id, settings, session)
            ),
        )
        if refusal:
            found.append(refusal)
    return found


def pairing_refusal(
    item: MediaItem,
    compiles_after: bool,
    settings: config.Settings,
    session: Session,
) -> list[str]:
    """Each refusal (`change_refusal`) earned if ``item`` compiled as ``compiles_after`` says.

    For a change to its pairing (its still chosen, cleared or reset): the
    caller says whether the compiler would still keep it afterwards, and
    every playlist holding it is checked with that answer. Nothing is
    refused when the answer doesn't change.
    """
    current = entry_playability(item, session)
    if current.compiles == compiles_after:
        return []
    health = session.pairings.health(pairings.Identity.of(item))
    changed = Playability(
        compiles_after,
        compiles_after and not health.is_borked,
        "" if compiles_after else "it would have no still for playback to show",
    )
    known = library_index(session)
    found: list[str] = []
    for playlist in session.playlists.all():
        if not any(entry.path == item.path for entry in playlist.entries):
            continue
        before = playlist_playability(playlist.entries, session, known)
        after = tuple(
            changed if entry.path == item.path else can
            for entry, can in zip(playlist.entries, before, strict=True)
        )
        refusal = change_refusal(
            _quoted_name(playlist),
            before,
            after,
            playlist_uses(playlist.id, settings, session),
        )
        if refusal:
            found.append(refusal)
    return found


# -- the application's boundary: one check for every write that could break it -------
#
# Every write that puts a playlist into use, or can take away what a playlist
# plays, calls one of these from its ``prepare``: on GTK, when its task
# reaches the head of the authoring lane, after every earlier write has been
# adopted, so against what is saved. Whichever window, page or control verb
# asked, it is the same check. Each refusal is a `PlaybackGuardError`,
# raised before anything is written.


def checked_entries_removal[Work](
    reference: str,
    entry_ids: Iterable[str],
    work: Callable[[], Work],
    session: Session,
) -> Callable[[], Work]:
    """``work`` (it takes ``entry_ids`` out of playlist ``reference``), once checked."""
    playlist = session.playlists.find(reference)
    chosen = frozenset(entry_ids)
    refusal = entries_refusal(
        playlist,
        [entry for entry in playlist.entries if entry.id not in chosen],
        session.settings,
        session,
    )
    if refusal:
        raise PlaybackGuardError(f"Nothing was removed from {_quoted_name(playlist)}: {refusal}")
    return work


def checked_entries_addition[Work](
    reference: str,
    sources: Iterable[Path],
    work: Callable[[], Work],
    session: Session,
) -> Callable[[], Work]:
    """``work`` (it adds ``sources`` to playlist ``reference``), once checked."""
    playlist = session.playlists.find(reference)
    adding = [Entry(f"adding-{index}", str(source)) for index, source in enumerate(sources)]
    refusal = entries_refusal(playlist, [*playlist.entries, *adding], session.settings, session)
    if refusal:
        raise PlaybackGuardError(f"Nothing was added to {_quoted_name(playlist)}: {refusal}")
    return work


def checked_playlist_use[Work](
    reference: str,
    what: str,
    work: Callable[[], Work],
    session: Session,
) -> Callable[[], Work]:
    """``work`` (it makes playlist ``reference`` a rule's or a display's), once checked."""
    refusal = use_refusal(reference, session)
    if refusal:
        raise PlaybackGuardError(f"{what}: {refusal}")
    return work


def refuse_unresolved_quick_choice(item: MediaItem, session: Session) -> None:
    """Refuse Quick choice of a wallpaper the compiler can't keep, saying why.

    Quick choice replaces "your pick" with that one wallpaper; one the
    compiler drops would leave it holding nothing it can build. (One that
    playback skips is refused by the callers before this, as it always was.)
    """
    can = entry_playability(item, session)
    if not can.compiles:
        raise PlaybackGuardError(f"Playback unavailable for {item.name}: {can.reason}")


def refuse_library_removal(items: Sequence[MediaItem], session: Session) -> None:
    """Refuse taking ``items`` out of the library when a playlist would break.

    Every entry naming one goes with it (`emptied_by_removing`). Called before
    the removal plan, so before anything is moved or deleted.
    """
    emptied = emptied_by_removing([item.path for item in items], session.settings, session)
    if emptied:
        names = listed([item.name for item in items])
        raise PlaybackGuardError(
            f"{names} was not removed: {listed(emptied)}. Add another wallpaper there first."
        )


def pairing_refusals_if_unresolved(item: MediaItem, session: Session) -> list[str]:
    """What would break if a pairing change left ``item`` with no still.

    Only a moving wallpaper can lose its still; going back to the worked-out
    one is decided where that is worked out (the authoring worker, or the
    headless service's scan), and this says what to refuse then.
    """
    if not item.is_moving:
        return []
    return pairing_refusal(item, False, session.settings, session)


def settings_refusal(changes: Mapping[str, object], session: Session) -> str:
    """Why a settings change may not be saved (a default with nothing to play); or empty."""
    wanted = changes.get("active_playlist")
    if isinstance(wanted, str) and wanted:
        refusal = use_refusal(wanted, session)
        if refusal:
            return f"the chosen default playlist can\u2019t be used: {refusal}"
    return ""


def _validate_playlist_identity(
    compiled: list[tuple[str, str, tuple[tuple[str, ...], ...]]],
) -> None:
    """Reject references the Rust runtime could resolve to two playlists.

    Authoring ids and names are separate conveniences, but the runtime accepts
    either in schedule/default references.  Duplicate names, duplicate ids,
    or one playlist's id equalling another playlist's name would make the
    selected object depend on iteration order.  Catch that before the atomic
    install so the last-known-good runtime document remains usable.
    """
    identities: dict[str, tuple[int, str, str]] = {}
    for index, (identifier, name, _entries) in enumerate(compiled):
        _bounded_text(identifier, label="playlist id", maximum_bytes=MAX_IDENTIFIER_BYTES)
        _bounded_name(name, label=f"playlist {identifier!r} name")
        for label, value in (("id", identifier), ("name", name)):
            folded = value.casefold()
            previous = identities.get(folded)
            if previous is not None and previous[0] != index:
                _previous_index, previous_label, previous_value = previous
                raise RuntimeConfigError(
                    f"playlist {label} {value!r} conflicts with another playlist's "
                    f"{previous_label} {previous_value!r}; rename it in the app"
                )
            identities[folded] = (index, label, value)


def _validate_settings_wire(settings: config.Settings) -> None:
    """Validate every Settings value that can reach schema-4 TOML."""
    if not 5 <= settings.cycle_interval <= 24 * 60 * 60:
        raise RuntimeConfigError("cycle interval must be between 5 and 86400 seconds")
    if not 0 <= settings.video_volume <= 100:
        raise RuntimeConfigError("video volume must be between 0 and 100")
    if not 1 <= settings.scene_fps <= 240:
        raise RuntimeConfigError("scene fps must be between 1 and 240")
    if settings.video_when_hidden not in ("pause", "stop", "play"):
        raise RuntimeConfigError("video hidden policy is not supported by the runtime")
    if settings.video_interpolation not in ("off", "oversample", "linear"):
        raise RuntimeConfigError("video interpolation mode is not supported by the runtime")
    if settings.scene_scaling not in scenes.SCALING_CHOICES:
        raise RuntimeConfigError("scene scaling mode is not supported by the runtime")
    if settings.scene_clamp not in scenes.CLAMP_CHOICES:
        raise RuntimeConfigError("scene clamp mode is not supported by the runtime")
    if settings.display_mode not in config.DISPLAY_MODES:
        raise RuntimeConfigError("display mode is not supported by the runtime")
    _bounded_text(
        settings.active_playlist,
        label="active playlist setting",
        maximum_bytes=MAX_REFERENCE_BYTES,
        optional=True,
    )
    _bounded_connector(
        settings.output,
        label="output connector setting",
        optional=True,
    )
    _bounded_connector(
        settings.theme_source_connector,
        label="theme source connector setting",
        optional=True,
    )
    if (
        settings.display_mode == config.DISPLAY_MODE_INDEPENDENT
        and not settings.theme_source_connector
    ):
        raise RuntimeConfigError(
            "independent display mode needs a designated theme source connector"
        )
    for index, root in enumerate(settings.roots):
        _absolute_path(root, label=f"library root {index + 1}")


def overrides_path(target: Path) -> Path:
    """Where ``runtime-overrides.toml`` lives for one ``runtime.toml`` path."""
    return target.with_name(OVERRIDES_FILENAME)


def runtime_applies_overrides(status: object) -> bool:
    """Whether a runtime status says that service reads ``runtime-overrides.toml``.

    An older service never mentions it: a setting saved there applies once the
    updated service runs, which a page can say instead of claiming it is live.
    """
    if not isinstance(status, dict):
        return False
    supported = status.get("supported_override_schemas")
    return isinstance(supported, list) and OVERRIDES_SCHEMA_VERSION in supported


def render(settings: config.Settings, session: Session) -> str:
    """Return schema-4 TOML with every currently executable decision resolved."""
    return _compile(settings, session)[0]


def render_overrides(settings: config.Settings, session: Session) -> str | None:
    """Return ``runtime-overrides.toml`` for what reaches the wire, or ``None``.

    Only a compiled playlist (one with playable entries) can carry its own
    rotation, and only an independent display assignment can beat global
    rules; mirrored mode leaves assignments, and their opt-ins, dormant.
    ``None`` means no override is in use and the file must not exist.
    """
    return _compile(settings, session)[1]


def _overrides_document(
    rotations: list[tuple[str, int | None, bool | None]],
    beating: list[str],
) -> str | None:
    if not rotations and not beating:
        return None
    lines = [
        "# Generated by Wall-in-One beside runtime.toml. Edit playlists and displays in the "
        "app, not this file.",
        f"schema_version = {OVERRIDES_SCHEMA_VERSION}",
    ]
    for identifier, interval, shuffle in rotations:
        lines.extend(("", "[[playlists]]", f"id = {_quote(identifier)}"))
        if interval is not None:
            if not 5 <= interval <= 24 * 60 * 60:
                raise RuntimeConfigError(
                    f"playlist {identifier!r} interval must be between 5 and 86400 seconds"
                )
            lines.append(f"cycle_interval_seconds = {interval}")
        if shuffle is not None:
            lines.append(f"shuffle = {str(shuffle).lower()}")
    for connector in beating:
        lines.extend(
            ("", "[[displays]]", f"connector = {_quote(connector)}", "beats_global_rules = true")
        )
    document = "\n".join(lines) + "\n"
    if _encoded_length(document, label="runtime overrides") > MAX_RUNTIME_OVERRIDES_BYTES:
        raise RuntimeConfigError(
            f"runtime overrides are larger than {MAX_RUNTIME_OVERRIDES_BYTES} bytes"
        )
    return document


def _compile(settings: config.Settings, session: Session) -> tuple[str, str | None]:
    """Both generated documents: ``runtime.toml`` and its optional overrides."""
    _validate_settings_wire(settings)
    faults = session.authoring_faults()
    if faults:
        details = "; ".join(f"{name}: {fault}" for name, fault in faults)
        raise RuntimeConfigError(
            "cannot compile runtime configuration because authoring state is "
            f"unreadable or was saved by a newer version of Wall-in-One ({details}). "
            "Repair or restore the named file, or use the version that saved it; the "
            "existing runtime configuration was left untouched."
        )
    known = {item.path: item for item in session.library.items}
    playlists: list[tuple[str, str, tuple[tuple[str, ...], ...]]] = []

    fallback_items = session.library.items
    if settings.cycle_favourites_only:
        starred = tuple(item for item in fallback_items if item.path in session.favourites.paths)
        if starred:
            fallback_items = starred

    fallback_entries: list[tuple[str, ...]] = []
    for item in fallback_items:
        compiled_lines = _resolved_entry(item, session, entry_id_for_source(item.path))
        if compiled_lines is not None:
            fallback_entries.append(compiled_lines)
    playlists.append((FALLBACK_PLAYLIST_ID, FALLBACK_PLAYLIST_NAME, tuple(fallback_entries)))

    existing_ids = {FALLBACK_PLAYLIST_ID}
    rotations: list[tuple[str, int | None, bool | None]] = []
    for playlist in session.playlists.all():
        resolved: list[tuple[str, ...]] = []
        for authored_entry in playlist.entries:
            found_item = known.get(authored_entry.path)
            if found_item is None:
                continue
            compiled = _resolved_entry(found_item, session, authored_entry.id)
            if compiled is not None:
                resolved.append(compiled)
        if not resolved:
            if playlist.entries:
                missing = len(playlist.entries)
                raise RuntimeConfigError(
                    f"playlist {playlist.name!r} has no playable entries; all {missing} "
                    "authored entries are missing or unresolved. Restore the media or "
                    "remove those entries; the existing runtime configuration was left "
                    "untouched."
                )
            continue
        playlists.append((playlist.id, playlist.name, tuple(resolved)))
        existing_ids.add(playlist.id)
        if playlist.has_rotation_override:
            rotations.append((playlist.id, playlist.cycle_interval, playlist.shuffle))

    if len(playlists) > MAX_PLAYLISTS:
        raise RuntimeConfigError(f"no more than {MAX_PLAYLISTS} runtime playlists are supported")
    for identifier, _name, entries in playlists:
        if len(entries) > MAX_ENTRIES_PER_PLAYLIST:
            raise RuntimeConfigError(
                f"playlist {identifier!r} has more than {MAX_ENTRIES_PER_PLAYLIST} entries"
            )
    _validate_playlist_identity(playlists)

    def runtime_playlist(reference: str, *, owner: str) -> str:
        if reference in (FALLBACK_PLAYLIST_ID, FALLBACK_PLAYLIST_NAME):
            return FALLBACK_PLAYLIST_ID
        authored = session.playlists.get(reference)
        if authored is None:
            authored = session.playlists.by_name(reference)
        if authored is None:
            raise RuntimeConfigError(
                f"{owner} refers to unknown playlist {reference!r}; repair it in the app. "
                "The existing runtime configuration was left untouched."
            )
        if authored.id not in existing_ids:
            raise RuntimeConfigError(
                f"{owner} refers to playlist {authored.name!r}, which has no playable "
                "entries. Add media or choose another playlist; the existing runtime "
                "configuration was left untouched."
            )
        return authored.id

    default = (
        runtime_playlist(settings.active_playlist, owner="default playlist")
        if settings.active_playlist
        else FALLBACK_PLAYLIST_ID
    )
    schema_version = (
        BATTERY_SCHEMA_VERSION if settings.stop_animations_on_battery else SCHEMA_VERSION
    )
    lines = [
        "# Generated by Wall-in-One. Edit the library in the app, not this file.",
        f"schema_version = {schema_version}",
        f"default_playlist = {_quote(default)}",
        "",
        "[settings]",
        f"cycle_interval_seconds = {settings.cycle_interval}",
        f"cycle_enabled = {str(settings.cycle_enabled).lower()}",
        f"shuffle = {str(settings.shuffle).lower()}",
        f"dynamics_enabled = {str(settings.dynamics_enabled).lower()}",
        f"display_mode = {_quote(settings.display_mode)}",
        f"theme_source_connector = {_quote(settings.theme_source_connector)}",
        *(["stop_animations_on_battery = true"] if settings.stop_animations_on_battery else []),
        "",
        "[renderer]",
        f"noctalia_program = {_quote(_program('noctalia'))}",
        f"niri_program = {_quote(_program('niri'))}",
        f"mpvpaper_program = {_quote(_program('mpvpaper'))}",
        f"linux_wallpaperengine_program = {_quote(_program('linux-wallpaperengine'))}",
        f"own_scene_renderer = {str(settings.own_scene_renderer).lower()}",
        'layer = "background"',
        f"video_when_hidden = {_quote(settings.video_when_hidden)}",
        f"video_hardware_decode = {str(settings.video_hardware_decode).lower()}",
        f"video_interpolation = {_quote(settings.video_interpolation)}",
        f"video_muted = {str(settings.video_muted).lower()}",
        f"video_volume = {settings.video_volume}",
        f"scene_fps = {settings.scene_fps}",
        # The exposed controls are explicitly video controls and mpv can
        # retune them live. linux-wallpaperengine only accepts audio at launch;
        # coupling it here made every slider step restart a scene while the UI
        # claimed it was changing a video. Scenes stay safely silent until they
        # receive their own deliberate controls.
        "scene_muted = true",
        "scene_volume = 0",
        "scene_pause_when_covered = true",
        f"scene_scaling = {_quote(settings.scene_scaling)}",
        f"scene_clamp = {_quote(settings.scene_clamp)}",
    ]

    for playlist_id, name, entries in playlists:
        lines.extend(("", "[[playlists]]", f"id = {_quote(playlist_id)}", f"name = {_quote(name)}"))
        for compiled_entry in entries:
            lines.extend(("", *compiled_entry))

    # Mirrored mode deliberately preserves connector rules in authoring but
    # leaves them dormant on the wire. Independent mode emits the complete
    # authored order: Rust evaluates the shared and connector-exact rules
    # together, so the established last-match-wins rule remains literal.
    emitted_schedules = [
        (rule, runtime_playlist(rule.playlist, owner=f"schedule {rule.id!r}"))
        for rule in session.schedules.rules
        if settings.display_mode == config.DISPLAY_MODE_INDEPENDENT or not rule.connector
    ]
    if len(emitted_schedules) > MAX_SCHEDULES:
        raise RuntimeConfigError(f"no more than {MAX_SCHEDULES} schedule rules are supported")
    schedule_ids: set[str] = set()
    for rule, playlist_id in emitted_schedules:
        _bounded_text(rule.id, label="schedule id", maximum_bytes=MAX_IDENTIFIER_BYTES)
        if rule.id in schedule_ids:
            raise RuntimeConfigError(f"duplicate schedule id {rule.id!r}")
        schedule_ids.add(rule.id)
        _bounded_text(
            playlist_id,
            label=f"schedule {rule.id!r} playlist reference",
            maximum_bytes=MAX_REFERENCE_BYTES,
        )
        lines.extend(
            ("", "[[schedules]]", f"id = {_quote(rule.id)}", f"playlist = {_quote(playlist_id)}")
        )
        if rule.connector:
            _bounded_connector(
                rule.connector,
                label=f"schedule {rule.id!r} connector",
            )
            lines.append(f"connector = {_quote(rule.connector)}")
        if rule.months:
            lines.append(
                "months = [" + ", ".join(str(value) for value in sorted(rule.months)) + "]"
            )
        if rule.weekdays:
            lines.append(
                "weekdays = [" + ", ".join(str(value) for value in sorted(rule.weekdays)) + "]"
            )
        if rule.start is not None and rule.end is not None:
            lines.append(f'start = "{rule.start // 60:02d}:{rule.start % 60:02d}"')
            lines.append(f'end = "{rule.end // 60:02d}:{rule.end % 60:02d}"')
        lines.append(f"enabled = {str(rule.enabled).lower()}")

    # Mirrored is deliberately one route. Saved dock assignments remain
    # app-owned and dormant there; independent mode compiles them as the
    # baseline beneath matching schedule rules and runtime manual overrides.
    assignments = (
        session.displays.all() if settings.display_mode == config.DISPLAY_MODE_INDEPENDENT else ()
    )
    emitted_assignments = [
        (
            connector,
            runtime_playlist(
                assigned_playlist,
                owner=f"display assignment for {connector!r}",
            ),
        )
        for connector, assigned_playlist in assignments
    ]
    if len(emitted_assignments) > MAX_DISPLAYS:
        raise RuntimeConfigError(f"no more than {MAX_DISPLAYS} display assignments are supported")
    seen_connectors: set[str] = set()
    for connector, assigned_playlist in emitted_assignments:
        _bounded_connector(
            connector,
            label="display connector",
        )
        if connector in seen_connectors:
            raise RuntimeConfigError(f"duplicate display connector {connector!r}")
        seen_connectors.add(connector)
        _bounded_text(
            assigned_playlist,
            label=f"display {connector!r} playlist reference",
            maximum_bytes=MAX_REFERENCE_BYTES,
        )
        lines.extend(
            (
                "",
                "[[displays]]",
                f"connector = {_quote(connector)}",
                f"playlist = {_quote(assigned_playlist)}",
            )
        )
    semantic_document = "\n".join(lines) + "\n"
    _validate_status_budget(semantic_document)
    generation = _semantic_generation(semantic_document)
    # Keep this top-level field before the first TOML table. It deliberately
    # does not hash itself: the token identifies the compiled semantic body,
    # not an unstable recursive document.
    lines.insert(2, f"config_generation = {_quote(generation)}")
    document = "\n".join(lines) + "\n"
    if _encoded_length(document, label="runtime configuration") > MAX_RUNTIME_CONFIG_BYTES:
        raise RuntimeConfigError(
            f"runtime configuration is larger than {MAX_RUNTIME_CONFIG_BYTES} bytes"
        )
    beating = sorted(
        connector
        for connector, _playlist in emitted_assignments
        if session.displays.beats_global_rules(connector)
    )
    return document, _overrides_document(rotations, beating)


def _read_overrides(target: Path) -> str | None:
    sidecar = overrides_path(target)
    try:
        return file_io.read_regular_text(sidecar, MAX_RUNTIME_OVERRIDES_BYTES)
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeConfigError(f"cannot read {sidecar}: {error}") from error


def overrides_from_a_newer_build(text: str | None) -> bool:
    """Whether an overrides file declares a schema newer than this build writes.

    A newer release owns such a file: this build's service ignores it, and
    this build's compiler never rewrites or removes it, so rolling forward
    again finds it as that release left it. A file this build cannot parse
    is its own damaged output and is simply replaced.
    """
    if text is None:
        return False
    try:
        declared = tomllib.loads(text).get("schema_version")
    except tomllib.TOMLDecodeError, RecursionError:
        return False
    return type(declared) is int and declared > OVERRIDES_SCHEMA_VERSION


def write(settings: config.Settings, session: Session, path: Path | None = None) -> Path:
    """Compile and install both documents, overrides first (see :func:`_publish`).

    Each file is replaced atomically on its own; the pair is not. A refused
    ``runtime.toml`` leaves both files as they were, and so does a failed
    ``runtime.toml`` install, which puts the previous overrides back.
    """
    target = path if path is not None else paths.runtime_config_path()
    with compiler_lock(target):
        document, overrides = _compile(settings, session)
        previous_overrides = _read_overrides(target)
        _require_publishable(document, target)
        _publish(
            target,
            document,
            overrides,
            publish_overrides=not overrides_from_a_newer_build(previous_overrides),
            previous_overrides=previous_overrides,
        )
    return target


def update(settings: config.Settings, session: Session, path: Path | None = None) -> bool:
    """Atomically install only changed documents; return whether bytes changed.

    Reloading the runtime applies its current entry.  A GUI refresh that found
    the same library must therefore not rewrite the generated files and
    restart a video or scene for no configuration change. ``runtime.toml``
    and ``runtime-overrides.toml`` are compared separately, and a change to
    either, including the overrides file appearing or going, counts. An
    overrides file from a newer build is left exactly as it is. A refused or
    failed ``runtime.toml`` leaves the previous pair (see :func:`_publish`).
    """
    target = path if path is not None else paths.runtime_config_path()
    with compiler_lock(target):
        document, overrides = _compile(settings, session)
        try:
            current = file_io.read_regular_text(target, MAX_RUNTIME_CONFIG_BYTES)
        except (OSError, UnicodeDecodeError) as error:
            raise RuntimeConfigError(f"cannot read {target}: {error}") from error
        current_overrides = _read_overrides(target)
        document_changed = current != document
        overrides_changed = current_overrides != overrides and not overrides_from_a_newer_build(
            current_overrides
        )
        if not (document_changed or overrides_changed):
            return False
        if document_changed:
            _require_publishable(document, target)
        _publish(
            target,
            document if document_changed else None,
            overrides,
            publish_overrides=overrides_changed,
            previous_overrides=current_overrides,
        )
        return True


def _publish(
    target: Path,
    document: str | None,
    overrides: str | None,
    *,
    publish_overrides: bool,
    previous_overrides: str | None,
) -> None:
    """Publish the overrides, then ``runtime.toml``, keeping the previous pair on failure.

    ``document`` is None when ``runtime.toml`` does not change. The two files
    are separate atomic renames, so the guarantee is per file plus this: if
    ``runtime.toml`` cannot be installed after the overrides were published,
    the previous overrides are put back -- their exact bytes through the same
    exclusive temporary, fsync, rename and directory fsync, or an unlink and
    directory fsync when there were none -- before the error is raised. Only
    a crash between the two renames, or a restore that fails too (said in the
    error), leaves the new overrides beside the previous ``runtime.toml``: a
    pair the service loads, since it skips overrides naming what that
    document lacks, until the next successful save replaces it.

    A file that was replaced but whose folder could not be synced is
    published, not failed (:class:`RuntimeConfigNotDurableError`): the save
    carries on to the new pair and keeps it, and only then reports that it
    may not survive a power loss. Putting the old overrides back beside an
    already published ``runtime.toml`` would make a pair nobody asked for.
    """
    not_durable: list[RuntimeConfigNotDurableError] = []
    if publish_overrides:
        try:
            _publish_overrides(overrides, target)
        except RuntimeConfigNotDurableError as uncertain:
            not_durable.append(uncertain)
    if document is not None:
        try:
            _install(document, target)
        except RuntimeConfigNotDurableError as uncertain:
            not_durable.append(uncertain)
        except Exception as error:
            if not publish_overrides:
                raise
            try:
                _publish_overrides(previous_overrides, target)
            except RuntimeConfigNotDurableError as restore:
                raise RuntimeConfigError(
                    f"{error}; the previous {OVERRIDES_FILENAME} was put back, but {restore}"
                ) from error
            except (RuntimeConfigError, OSError) as restore:
                raise RuntimeConfigError(
                    f"{error}; the previous {OVERRIDES_FILENAME} could not be put back either "
                    f"({restore}), so it may not match runtime.toml until the next successful save"
                ) from error
            raise
    if not_durable:
        raise RuntimeConfigNotDurableError(
            tuple(path for uncertain in not_durable for path in uncertain.published),
            not_durable[-1].error,
        ) from not_durable[-1]


def _require_publishable(document: str, target: Path) -> None:
    """Refuse, before any byte is written, a schema the live service cannot load.

    Only ``runtime.toml`` needs this: the overrides file is never read by a
    service that predates it, so it is safe to publish under any service.
    """
    if target.absolute() == paths.runtime_config_path().absolute():
        schema = tomllib.loads(document)["schema_version"]
        try:
            runtime_compatibility.require_schema(schema)
        except runtime_compatibility.RuntimeCompatibilityError as error:
            raise RuntimeConfigError(str(error)) from error


def _publish_overrides(overrides: str | None, target: Path) -> None:
    """Write ``runtime-overrides.toml``, or durably remove it when unused.

    Written before ``runtime.toml``: the service skips overrides naming a
    playlist or display its document lacks, so every intermediate pair loads.
    """
    sidecar = overrides_path(target)
    if overrides is not None:
        _write_atomic(overrides, sidecar)
        return
    try:
        found = sidecar.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise RuntimeConfigError(f"cannot inspect {sidecar}: {error}") from error
    if not stat.S_ISREG(found.st_mode):
        raise RuntimeConfigError(f"{sidecar} is not a regular file; it was left untouched")
    try:
        sidecar.unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        raise RuntimeConfigError(f"cannot remove {sidecar}: {error}") from error
    # Gone for every reader: a failure from here on is durability uncertainty.
    try:
        state_file.fsync_parent(sidecar)
    except OSError as error:
        raise RuntimeConfigNotDurableError((sidecar,), error) from error


def _install(document: str, target: Path) -> None:
    """Durably replace ``target`` with already-rendered, already-checked configuration."""
    _write_atomic(document, target)


def _write_atomic(document: str, target: Path) -> None:
    """Same-directory temporary, flush, rename, then flush the directory.

    A failure up to the rename is :class:`RuntimeConfigError`, with the old
    file in place; one after it is :class:`RuntimeConfigNotDurableError`.
    """
    paths.ensure_directory(target.parent)
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise RuntimeConfigError(f"cannot write {target}: {error}") from error
    # Published: every reader sees the new bytes. A failure from here on is
    # uncertainty about durability, never a refusal.
    try:
        state_file.fsync_parent(target)
    except OSError as error:
        raise RuntimeConfigNotDurableError((target,), error) from error
