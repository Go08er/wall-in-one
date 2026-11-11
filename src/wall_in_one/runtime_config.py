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
import logging
import os
import re
import stat
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from wall_in_one import config, dials, file_io, paths, runtime_compatibility
from wall_in_one.library import pairings, scan, schedules, state_file, workshop
from wall_in_one.library.model import Kind, MediaItem, Unseen, UnseenKind
from wall_in_one.library.playlists import Entry, Playlist
from wall_in_one.programs import find_program
from wall_in_one.session import SAVED_PLAYLISTS, SAVED_USES, Session
from wall_in_one.wallpaper import scenes

SCHEMA_VERSION: Final = 4
BATTERY_SCHEMA_VERSION: Final = 5
#: A document with a Wallpaper Engine preset entry (``scene_base`` or
#: ``properties``), written only then: every other document keeps 4 or 5. It
#: carries everything 5 does. A live service that doesn't list it gets the
#: document without the presets (`LeftOut`, `PRESET_OWNER`), never a refusal
#: of the whole save (docs/wallpaper-engine.md).
PRESET_SCHEMA_VERSION: Final = 6
#: A document with a renderer dial (docs/renderer-settings.md): a new
#: ``[renderer]`` key at other than its default, or an entry's own ``video``
#: or ``scene`` table. Written only then, and carrying everything 6 does. A
#: live service that doesn't list it gets the document without them
#: (`LeftOut`, `DIALS_OWNER`), never a refusal of the whole save.
DIALS_SCHEMA_VERSION: Final = 7
#: A document with a playlist that plays nothing (``entries = []`` and its
#: ``nothing_to_play`` reason), or with ``allow_web_wallpapers`` in
#: ``[settings]`` (docs/runtime-config.md). Written only then, carrying
#: everything 7 does. A live service that doesn't list it gets the document
#: without ``allow_web_wallpapers`` (it then plays web pages as 0.4.0 did,
#: while the setting is on); a playlist that plays nothing has no older shape,
#: so that document is refused (`NothingToPlayNeedsNewerServiceError`).
NOTHING_TO_PLAY_SCHEMA_VERSION: Final = 8
#: The longest ``nothing_to_play`` reason the service accepts, and what a
#: display that plays nothing is called in its status (``entry_name``).
MAX_NOTHING_TO_PLAY_BYTES: Final = 128
NOTHING_TO_PLAY_LABEL: Final = "Nothing to play: "
#: Why All media plays nothing, as the document and the app say it.
WEB_OFF_REASON: Final = "web wallpapers are off"
FAVOURITES_REASON: Final = "none of the favourites can play"
EMPTY_LIBRARY_REASON: Final = "nothing in the library can play"

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


def is_fallback_reference(reference: str) -> bool:
    """Whether ``reference`` names the generated All media playlist, as the compiler matches it.

    Exactly its id or its name (no case folding): the built-in is generated
    here, never authored, so a reference to it is not dangling.
    """
    return reference in (FALLBACK_PLAYLIST_ID, FALLBACK_PLAYLIST_NAME)


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
#: How many times one publication compiles again because ``allow_web_wallpapers``
#: in ``settings.toml`` is no longer the one it compiled for (`_web_setting_held`).
WEB_SETTING_ATTEMPTS: Final = 5


class RuntimeConfigError(Exception):
    """The resolved document could not be produced or installed."""


class _WebSettingMovedError(Exception):
    """``settings.toml`` says ``fresh`` for ``allow_web_wallpapers``, not what was compiled for.

    Raised by `_web_setting_held` before anything is written, and caught by
    the publication that compiled (`publish_changes`, `write`), which compiles
    again for ``fresh``. Never a `RuntimeConfigError`, so no caller's handler
    for a failed publication can take it for one.
    """

    def __init__(self, fresh: bool) -> None:
        super().__init__(f"allow_web_wallpapers is {str(fresh).lower()} in settings.toml now")
        self.fresh = fresh


class NothingToPlayNeedsNewerServiceError(RuntimeConfigError):
    """All media plays nothing, and the live service isn't confirmed to read that (schema 8)."""


class FallbackUnseenError(RuntimeConfigError):
    """All media plays nothing only as far as the scan could look (``place`` is where)."""

    def __init__(self, message: str, place: Unseen, *, implicit: bool, owner: str) -> None:
        super().__init__(message)
        self.place = place
        #: No default is chosen (or the chosen one was left out); All media is not named.
        self.implicit = implicit
        #: Who names All media when it is named, in words ("schedule rule 'x'").
        self.owner = owner


#: `LeftOut.why`: no playlist answers to the reference, by id or by name.
DOES_NOT_EXIST: Final = "doesn't exist"
#: `LeftOut.why`: the playlist exists but holds nothing the compiler keeps
#: (it is empty, or every wallpaper in it is missing or has no still yet).
NOTHING_TO_PLAY: Final = "has nothing to play"

#: `LeftOut.why` for a Wallpaper Engine preset: the live wallpaper service
#: has not confirmed it reads `PRESET_SCHEMA_VERSION`.
NEEDS_NEWER_SERVICE: Final = "needs a newer wallpaper service"
#: What a preset waits for. Two things keep a service from being confirmed
#: (`runtime_compatibility.require_schema`): one that predates presets and
#: was not restarted after the update, and installed units that can't be
#: verified as current.
PRESETS_UNCONFIRMED: Final = (
    "the wallpaper service is confirmed to read presets: restart it after an update, "
    "and make sure its installed units are current."
)

#: `LeftOut.owner`: whose reference it is.
DEFAULT_OWNER: Final = "default"
SCHEDULE_OWNER: Final = "schedule"
DISPLAY_OWNER: Final = "display"
#: Not a reference but a wallpaper: a preset left out of every playlist, its
#: Workshop id in ``owner_id`` and its title in ``reference``.
PRESET_OWNER: Final = "preset"
#: Not a reference but the renderer dials, the global ones and each
#: wallpaper's own (and, under a service that predates presets, a scene's
#: own Wallpaper Engine values): left out together, said once.
DIALS_OWNER: Final = "dials"
#: Not a reference left out but what plays: All media plays nothing because
#: web wallpapers are off, so no wallpaper is played on a display that plays
#: it (``why``); Noctalia keeps the last still it was given.
NOTHING_OWNER: Final = "nothing"
#: Not a reference but ``allow_web_wallpapers``, while the setting is on:
#: left out for a service not confirmed to read schema 8, whose own check
#: then refuses every web page the document names (`_fitted`). Said once.
WEB_OWNER: Final = "web"


@dataclass(frozen=True, slots=True, order=True)
class LeftOut:
    """One saved reference to a playlist the runtime document was built without.

    The compiler never refuses over a playlist that can't play. A reference
    to one (the default, a schedule rule's, a display's own playlist) is left
    out, everything else is published, and this says what was left out so
    that whoever compiled can say it: the application as a standing notice,
    an unattended command as a ``warning:`` line.

    What leaving it out means depends on whose it is. The default is built as
    "All media". A schedule rule is not emitted, so whatever would apply
    without it applies. A display's own playlist is not emitted, so that
    display follows the rules and the default.
    """

    #: `DEFAULT_OWNER`, `SCHEDULE_OWNER` or `DISPLAY_OWNER`.
    owner: str
    #: The rule's id or the display's connector; empty for the default.
    owner_id: str
    #: The reference as it is saved: a playlist's id or its name.
    reference: str
    #: The playlist's name when it exists (``why`` is then `NOTHING_TO_PLAY`).
    playlist: str
    #: `DOES_NOT_EXIST` or `NOTHING_TO_PLAY`.
    why: str

    def sentence(self, rules: int = 1) -> str:
        """What was left out and what happens instead, for a person.

        ``rules``: how many schedule rules (or presets) this one sentence speaks for.
        """
        if self.owner == NOTHING_OWNER:
            return f"{NOTHING_TO_PLAY_LABEL}{self.why}."
        if self.owner == WEB_OWNER:
            return (
                "Web wallpapers aren\u2019t played until the wallpaper service is confirmed "
                "to read them: restart it after an update, and make sure its installed units "
                "are current."
            )
        if self.owner == DIALS_OWNER:
            return (
                "Renderer settings and scene sound, in Settings and in each wallpaper\u2019s "
                "own, aren\u2019t applied until the wallpaper service is confirmed to read them: "
                "restart it after an update, and make sure its installed units are current."
            )
        if self.owner == PRESET_OWNER:
            if rules == 1:
                return (
                    f"The Wallpaper Engine preset \u201c{self.reference}\u201d isn\u2019t played "
                    f"until {PRESETS_UNCONFIRMED}"
                )
            return (
                f"{rules} Wallpaper Engine presets aren\u2019t played until {PRESETS_UNCONFIRMED}"
            )
        missing = self.why == DOES_NOT_EXIST
        presets = self.why == NEEDS_NEWER_SERVICE
        if self.owner == DISPLAY_OWNER:
            own = (
                f"{self.owner_id}\u2019s own playlist doesn\u2019t exist ({self.reference})"
                if missing
                else (
                    f"{self.owner_id}\u2019s own playlist, \u201c{self.playlist}\u201d, "
                    "holds only Wallpaper Engine presets the running wallpaper service "
                    "doesn\u2019t read yet"
                )
                if presets
                else (
                    f"{self.owner_id}\u2019s own playlist, \u201c{self.playlist}\u201d, "
                    "has nothing to play"
                )
            )
            return f"{own}, so that display follows the schedule and the default."
        named = (
            f"a playlist that doesn\u2019t exist ({self.reference})"
            if missing
            else (
                f"\u201c{self.playlist}\u201d, which holds only Wallpaper Engine presets the "
                "running wallpaper service doesn\u2019t read yet"
            )
            if presets
            else f"\u201c{self.playlist}\u201d, which has nothing to play"
        )
        if self.owner == DEFAULT_OWNER:
            return (
                f"The default names {named}, so \u201c{FALLBACK_PLAYLIST_NAME}\u201d plays instead."
            )
        if rules == 1:
            return f"A schedule rule names {named}, so that rule is left out."
        return f"{rules} schedule rules name {named}, so those rules are left out."

    def warning(self) -> str:
        """The same for an unattended command's ``warning:`` line: ids, and where it is saved."""
        if self.owner == NOTHING_OWNER:
            return (
                f"nothing to play: {self.why}; the wallpaper service plays no wallpaper "
                f'on a display that plays "{FALLBACK_PLAYLIST_NAME}", and the last still '
                "Noctalia was given stays on it"
            )
        if self.owner == WEB_OWNER:
            return (
                "allow_web_wallpapers is left out: the running wallpaper service has not "
                f"confirmed schema_version {NOTHING_TO_PLAY_SCHEMA_VERSION}, and it plays no "
                "web wallpaper without it; restart it after an update, and check that its "
                "installed units are current, to play them"
            )
        if self.owner == DIALS_OWNER:
            return (
                "renderer settings are left out: the running wallpaper service has not "
                f"confirmed schema_version {DIALS_SCHEMA_VERSION}; restart it after an update, "
                "and check that its installed units are current, to apply them"
            )
        if self.owner == PRESET_OWNER:
            return (
                f"Wallpaper Engine preset {self.reference!r} ({self.owner_id}) is left out: "
                f"the running wallpaper service has not confirmed schema_version "
                f"{PRESET_SCHEMA_VERSION}; restart it after an update, and check that its "
                "installed units are current, to play presets"
            )
        named = (
            f"a playlist that doesn't exist ({self.reference!r})"
            if self.why == DOES_NOT_EXIST
            else (
                f"playlist {self.playlist!r}, which holds only Wallpaper Engine presets the "
                "running wallpaper service does not read yet"
            )
            if self.why == NEEDS_NEWER_SERVICE
            else f"playlist {self.playlist!r}, which has nothing to play"
        )
        if self.owner == DEFAULT_OWNER:
            return (
                f"active_playlist in {paths.settings_path()} names {named}; "
                f'building with "{FALLBACK_PLAYLIST_NAME}" as the default'
            )
        if self.owner == SCHEDULE_OWNER:
            return f"schedule rule {self.owner_id!r} names {named}; building without that rule"
        return (
            f"the display assignment for {self.owner_id!r} names {named}; building "
            "without it, so that display follows the schedule and the default"
        )


#: The order a notice speaks in: the default, the rules, then the displays.
_NOTICE_ORDER: Final = (
    NOTHING_OWNER,
    DEFAULT_OWNER,
    SCHEDULE_OWNER,
    DISPLAY_OWNER,
    WEB_OWNER,
    PRESET_OWNER,
    DIALS_OWNER,
)


def left_out_notice(left_out: Iterable[LeftOut]) -> str:
    """Everything one runtime document was built without, as one notice for a person; or empty.

    A sentence per reference (`LeftOut.sentence`): the default, then the
    rules, then the displays. Rules that name the same playlist for the same
    reason are counted in one sentence, not repeated.
    """
    said: dict[tuple[int, str, str, str, str], tuple[LeftOut, int]] = {}
    for one in set(left_out):
        # Rules naming one playlist share a sentence, and so do all presets.
        whose = "" if one.owner in (SCHEDULE_OWNER, PRESET_OWNER) else one.owner_id
        reference = "" if one.owner == PRESET_OWNER else one.reference
        key = (_NOTICE_ORDER.index(one.owner), whose, reference, one.playlist, one.why)
        said[key] = (one, said[key][1] + 1 if key in said else 1)
    return " ".join(one.sentence(count) for _key, (one, count) in sorted(said.items()))


@dataclass(frozen=True, slots=True)
class Compiled:
    """One compile: ``runtime.toml``, its optional overrides, and what was left out."""

    document: str
    #: ``runtime-overrides.toml``; None when no override is in use and the file must not exist.
    overrides: str | None
    left_out: tuple[LeftOut, ...] = ()
    #: Why All media plays nothing in ``document`` (schema 8), or empty.
    nothing: str = ""


@dataclass(frozen=True, slots=True)
class Published:
    """What one `publish_changes` did to disk, and what its document was built without.

    The files it wrote and removed, and ``left_out``: every reference to a
    playlist that can't play, which the document it compiled leaves out
    (`LeftOut`). ``left_out`` is that document's, whether or not any byte
    changed: a publication that found the files already current still says
    what they leave out. Truthy when a file changed.
    """

    written: tuple[Path, ...] = ()
    removed: tuple[Path, ...] = ()
    left_out: tuple[LeftOut, ...] = ()
    #: The ``runtime.toml`` text this replaced, when it replaced one: what the
    #: service may still hold if it refuses the new one (`retained_web_wallpapers`).
    replaced: str | None = None

    def __bool__(self) -> bool:
        return bool(self.written or self.removed)


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
        #: Everything the save put on disk, when it knows (`publish_changes`):
        #: a file can be published and synced while another's sync failed.
        self.changes: Published | None = None


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


LOGGER = logging.getLogger(__name__)


def _lock_trouble(message: str, path: Path) -> str:
    """``message``, for a person; the lock's ``path`` goes to the log only."""
    LOGGER.warning("%s: %s", message, path)
    return message


def _compiler_lock_path(target: Path) -> Path:
    return target.absolute().with_name(f".{target.name}.compiler.lock")


def _lock_timeout(path: Path, seconds: float) -> RuntimeConfigError:
    return RuntimeConfigError(
        _lock_trouble(
            f"timed out after {seconds:g}s waiting for the runtime compiler lock; "
            "the last-known-good runtime configuration was left untouched",
            path,
        )
    )


def _lock_given_up(path: Path) -> RuntimeConfigError:
    return RuntimeConfigError(
        _lock_trouble(
            "stopped waiting for the runtime compiler lock; "
            "the last-known-good runtime configuration was left untouched",
            path,
        )
    )


def _acquire_gate(
    deadline: float, give_up: Callable[[], bool] | None, lock_path: Path, wait: float
) -> None:
    """Take this process's half of the lock (`_LOCAL_COMPILER_GATE`) by ``deadline``, or raise.

    With ``give_up``, waited for in `COMPILER_LOCK_POLL_SECONDS` slices, and
    not after ``give_up()`` says so: one wait for the whole timeout runs no
    Python code, and so can't be ended early.
    """
    if give_up is None:
        if not _LOCAL_COMPILER_GATE.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise _lock_timeout(lock_path, wait)
        return
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        if _LOCAL_COMPILER_GATE.acquire(timeout=min(COMPILER_LOCK_POLL_SECONDS, remaining)):
            return
        if give_up():
            raise _lock_given_up(lock_path)
        if remaining <= 0:
            raise _lock_timeout(lock_path, wait)


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
            _lock_trouble(
                f"cannot safely open the runtime compiler lock: {error.strerror or error}", path
            )
        ) from error

    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeConfigError(
                _lock_trouble("the runtime compiler lock is not a regular file", path)
            )
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise RuntimeConfigError(
                _lock_trouble("the runtime compiler lock changed while being opened", path)
            )
        if opened.st_uid != os.getuid() or opened.st_nlink != 1:
            raise RuntimeConfigError(
                _lock_trouble(
                    "the runtime compiler lock is not a private file owned by this user", path
                )
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
        raise RuntimeConfigError(
            _lock_trouble(
                f"cannot verify the runtime compiler lock: {error.strerror or error}", path
            )
        ) from error
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise RuntimeConfigError(
            _lock_trouble("the runtime compiler lock changed while waiting for it", path)
        )


@contextmanager
def compiler_lock(
    target: Path | None = None,
    *,
    timeout: float | None = None,
    give_up: Callable[[], bool] | None = None,
) -> Iterator[None]:
    """Serialise every complete authoring-snapshot compilation.

    The headless compiler holds this before it reads settings or authoring
    stores. ``write`` and ``update`` enter it too, making direct and GUI callers
    safe while remaining re-entrant on the same thread and target.

    ``give_up``: asked at every poll while the lock is busy; once it says
    so, the wait ends there, as at the timeout (`RuntimeConfigError`). The
    lock is still tried once. For a wait a second Ctrl-C ends
    (`Application._web_off_on_disk_at_exit`).
    """

    document = target if target is not None else paths.runtime_config_path()
    lock_path = _compiler_lock_path(document)
    wait = COMPILER_LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    if wait < 0:
        raise RuntimeConfigError("runtime compiler lock timeout cannot be negative")
    deadline = time.monotonic() + wait

    _acquire_gate(deadline, give_up, lock_path, wait)
    try:
        held = _COMPILER_LOCK_STATE.held
        if held is not None:
            if held.path != lock_path:
                raise RuntimeConfigError(
                    _lock_trouble(
                        "cannot take one runtime compiler lock while holding another",
                        lock_path,
                    )
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
                    if give_up is not None and give_up():
                        raise _lock_given_up(lock_path) from None
                    if remaining <= 0:
                        raise _lock_timeout(lock_path, wait) from None
                    time.sleep(min(COMPILER_LOCK_POLL_SECONDS, remaining))
                except OSError as error:
                    raise RuntimeConfigError(
                        _lock_trouble(
                            f"cannot acquire the runtime compiler lock: {error.strerror or error}",
                            lock_path,
                        )
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
    # A name that starts with '-' is data here, as in the service's
    # `bounded_connector`: only where a name becomes an argument is it refused.


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
        reason = raw.get("nothing_to_play")
        if isinstance(reason, str) and not entries:
            # Status says it in place of an entry, twice: as `nothing_to_play`
            # and in the display's `entry_name` (Rust's validate_status_budget).
            largest_entry = max(
                largest_entry,
                2 * _encoded_length(reason, label="nothing_to_play reason")
                + len(NOTHING_TO_PLAY_LABEL.encode()),
            )
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


def _named_scenes(path: Path | None, text: str | None = None) -> set[str]:
    """The Workshop ids a ``runtime.toml`` names (`scene_id`, `scene_base`); none if unreadable.

    ``text``: that document's text, already read (`Published.replaced`); then
    ``path`` is not read.

    One that isn't TOML 1.0 names none here, though the service, which reads
    TOML 1.1, may read ids in it (`not_toml_1_0`). Every caller that must not
    keep a web page asks `allows_web_wallpapers` of the same document too,
    which counts such a one as allowing them: anything may play from it.
    """
    target = path if path is not None else paths.runtime_config_path()
    try:
        if text is None:
            text = file_io.read_regular_text(target, MAX_RUNTIME_CONFIG_BYTES)
        document = tomllib.loads(text) if text is not None else {}
    except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError:
        return set()
    return _scene_ids(document)


def _scene_ids(document: object) -> set[str]:
    """Every ``scene_id`` and ``scene_base`` string anywhere in a parsed document."""
    named: set[str] = set()
    pending: list[object] = [document]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("scene_id", "scene_base") and isinstance(value, str):
                    named.add(value)
                else:
                    pending.append(value)
        elif isinstance(node, list):
            pending.extend(node)
    return named


def retained_web_wallpapers(
    path: Path | None = None, *, text: str | None = None
) -> tuple[str, ...]:
    """Titles of the web wallpapers the published ``runtime.toml`` still plays.

    A document compiled while ``allow_web_wallpapers`` was on keeps naming
    them after it is turned off, until a compile replaces it; the service
    can't tell a web page from a scene. So a caller that is about to keep or
    leave that document in force asks here (service start with a compile that
    failed, the app after a publication that failed). The Workshop scan
    doesn't depend on the library folders, so it usually works when the
    compile didn't. An unreadable or malformed document says nothing here:
    the service's own validation judges that, and one that isn't TOML 1.0,
    which the service may read all the same, `allows_web_wallpapers` counts
    as allowing web wallpapers (`not_toml_1_0`). What the scan can't classify is
    `unchecked_scene_wallpapers`'s to say. ``text``: judge that document's
    text instead of reading ``path`` (the one a publication replaced, which a
    service that refused its successor still holds).
    """
    named = _named_scenes(path, text)
    if not named:
        return ()
    items = scan.workshop_items(workshop.steam_roots())
    web = {item.scene for item in items if item.web_page or item.workshop_type == "web"}
    return tuple(
        sorted(
            (
                item.name
                for item in items
                if item.scene in named and (item.scene in web or item.scene_base in web)
            ),
            key=str.casefold,
        )
    )


def unchecked_scene_wallpapers(
    path: Path | None = None, *, text: str | None = None
) -> tuple[str, ...]:
    """Workshop ids the published ``runtime.toml`` names that the scan can't classify.

    The Workshop scan lists an item only when it could read its folder and
    its `project.json` (an unreadable or unmounted Steam folder, a missing
    item, or a project file over the scan's size cap all leave it out), so
    for these it isn't known whether the page is a web page. A caller that
    must not keep a web page counts them as possibly one (fail closed). Also
    an id whose preset base the scan doesn't list. ``text`` as for
    `retained_web_wallpapers`.
    """
    named = _named_scenes(path, text)
    if not named:
        return ()
    listed = {item.scene: item for item in scan.workshop_items(workshop.steam_roots())}
    unchecked = {name for name in named if name not in listed}
    for name in named & listed.keys():
        base = listed[name].scene_base
        if base and base not in listed:
            unchecked.add(base)
    return tuple(sorted(unchecked))


def allows_web_wallpapers(path: Path | None = None, *, text: str | None = None) -> bool:
    """Whether the published ``runtime.toml`` says ``allow_web_wallpapers = true`` (schema 8).

    Such a document lets a web page play: the service reads an item's type
    just before it starts the engine, and with this key set it lets a ``web``
    page through (it still refuses every type but ``scene``, ``video`` and
    ``web``, and an item it can't classify). So a Workshop item whose type
    became ``web`` after the compile would play as one. A caller that must
    not keep a web page while the setting is off counts such a document as
    holding one. ``text`` as for `retained_web_wallpapers`.

    One the service can't read either says nothing: missing, unreadable, not
    a regular file (a symbolic link included), over `MAX_RUNTIME_CONFIG_BYTES`
    or not UTF-8, each of which the service refuses the same way
    (``Config::read_bytes``: ``O_NOFOLLOW``, the same cap, strict UTF-8). One
    that isn't TOML 1.0 (`not_toml_1_0`) counts as allowing them (review
    r13f, N1): the service's parser reads TOML 1.1, so a hand edit such as
    ``"allow_web_wallpap\\x65rs" = true``, which ``tomllib`` refuses, is
    loaded as allowing them, and so is the key in an inline table that spans
    lines. A document that is plainly malformed is counted so too, though the
    service refuses it: fail closed, since this can't tell the two apart.
    """
    target = path if path is not None else paths.runtime_config_path()
    try:
        if text is None:
            text = file_io.read_regular_text(target, MAX_RUNTIME_CONFIG_BYTES)
    except OSError, UnicodeDecodeError:
        return False
    if text is None:
        return False
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError, RecursionError:
        return True
    settings = document.get("settings")
    return isinstance(settings, dict) and settings.get("allow_web_wallpapers") is True


def not_toml_1_0(path: Path | None = None, *, text: str | None = None) -> bool:
    """Whether the published ``runtime.toml`` is there and readable, but isn't TOML 1.0.

    Python's ``tomllib`` reads TOML 1.0; the service's ``toml`` crate reads
    1.1, which adds ``\\xHH`` and ``\\e`` escapes, newlines and trailing
    commas in inline tables, and times without seconds. So what such a
    document allows or names can't be judged here the way the service reads
    it: `allows_web_wallpapers` counts it as allowing web wallpapers, and
    `_named_scenes` as naming nothing. A document the service can't read
    either (as `allows_web_wallpapers` lists) is not one. ``text`` as for
    `retained_web_wallpapers`.
    """
    target = path if path is not None else paths.runtime_config_path()
    try:
        if text is None:
            text = file_io.read_regular_text(target, MAX_RUNTIME_CONFIG_BYTES)
    except OSError, UnicodeDecodeError:
        return False
    if text is None:
        return False
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError, RecursionError:
        return True
    return False


def _escaped_key(key: str) -> re.Pattern[str]:
    """A basic-string key that spells ``key``, any of its characters written as an escape.

    TOML 1.0's ``\\u`` (four hex digits) and ``\\U`` (eight), and 1.1's
    ``\\x`` (two), in either case of hex digit. Only for an ASCII ``key``.
    """
    units = "".join(
        f"(?:{re.escape(character)}|\\\\(?:u00|U000000|x)(?i:{ord(character):02X}))"
        for character in key
    )
    return re.compile(f'"{units}"')


#: The keys a document's web state is judged by, as `may_spell_key` looks for them.
_ESCAPED_KEYS: Final = {
    key: _escaped_key(key) for key in ("allow_web_wallpapers", "scene_id", "scene_base")
}


def may_spell_key(text: str, key: str) -> bool:
    """Whether ``text`` may hold the TOML key ``key`` anywhere, as either TOML reads keys.

    Unparsed, in one search of the text: a key can only be ``key`` itself
    (bare, or quoted with no escape in it: then the name is in the text as
    it is), or a basic-string key with some of its characters written as
    escapes (`_escaped_key`). A dotted key or a table header splits nothing
    inside one name, and a multi-line string is never a key. So false means
    no reader, of TOML 1.0 or 1.1, finds ``key`` in it; true may be only the
    name inside a value or a comment. ``key``: one of ``allow_web_wallpapers``,
    ``scene_id`` and ``scene_base`` (review r13f, N2: the step at the end of a
    quit, which a full parse made slow).
    """
    return key in text or _ESCAPED_KEYS[key].search(text) is not None


#: How the compiler writes the Workshop id an entry plays, or its preset's base:
#: on a line of its own (`_resolved_entry`), a Workshop id being digits only.
_COMPILED_SCENE: Final = re.compile(r'^(scene_id|scene_base) = "([0-9]+)"$', re.MULTILINE)


def compiled_scenes(text: str) -> frozenset[str] | None:
    """The Workshop ids ``text`` names, read off the compiler's own lines; None if it may name more.

    Unparsed, in a few searches of the text: when every occurrence of
    ``scene_id`` and ``scene_base`` in it is on such a line (`_COMPILED_SCENE`)
    and no quoted key spells either (`may_spell_key`), those lines are all it
    names, as any reader of TOML 1.0 or 1.1 reads it. A document the compiler
    wrote, 0.4.0's included, is one unless a value in it has either name in
    it (a still's path, a title). Otherwise (a hand edit, or such a value)
    None: `named_scenes` parses it.
    """
    found = _COMPILED_SCENE.findall(text)
    for key in ("scene_id", "scene_base"):
        if text.count(key) != sum(1 for name, _identifier in found if name == key):
            return None
        if _ESCAPED_KEYS[key].search(text) is not None:
            return None
    return frozenset(identifier for _name, identifier in found)


def named_scenes(text: str) -> frozenset[str] | None:
    """The Workshop ids ``text`` names (`_named_scenes`), parsed; None if it isn't TOML 1.0.

    The service may read ids in one that isn't (`not_toml_1_0`), so None
    means "can't be told", never "none".
    """
    try:
        return frozenset(_scene_ids(tomllib.loads(text)))
    except tomllib.TOMLDecodeError, RecursionError:
        return None


def web_wallpapers_in(texts: Iterable[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """What any of ``texts`` (``runtime.toml`` documents) plays that web being off forbids.

    The titles of its web wallpapers (`retained_web_wallpapers`), and the
    Workshop ids whose type the scan can't classify (`unchecked_scene_wallpapers`),
    from one Workshop scan, for every text a service may still hold.
    """
    named: set[str] = set()
    for text in texts:
        named |= _named_scenes(None, text)
    if not named:
        return (), ()
    items = scan.workshop_items(workshop.steam_roots())
    web = {item.scene for item in items if item.web_page or item.workshop_type == "web"}
    titles = sorted(
        (
            item.name
            for item in items
            if item.scene in named and (item.scene in web or item.scene_base in web)
        ),
        key=str.casefold,
    )
    listed = {item.scene: item for item in items}
    unchecked = {name for name in named if name not in listed}
    for name in named & listed.keys():
        base = listed[name].scene_base
        if base and base not in listed:
            unchecked.add(base)
    return tuple(titles), tuple(sorted(unchecked))


def _program(name: str) -> Path:
    """The path ``runtime.toml`` names for the program ``name`` (`programs.find_program`)."""
    program = find_program(name).path
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
    *,
    allow_web: bool | None = None,
    settings: config.Settings | None = None,
    presets: bool = True,
    renderer_dials: bool = True,
    withheld: set[str] | None = None,
) -> tuple[str, ...] | None:
    """One playlist entry's lines, or ``None`` when it isn't one the runtime can play.

    ``allow_web``: the ``allow_web_wallpapers`` the document is compiled for;
    ``None`` is the session's own; ``settings`` likewise. Without
    ``renderer_dials`` its own dials are left out, and without ``presets`` a
    scene's own Wallpaper Engine values: it still plays, and its identity
    goes in ``withheld`` so that is said (`DIALS_OWNER`).
    """
    allowed = session.settings.allow_web_wallpapers if allow_web is None else allow_web
    compiled_for = session.settings if settings is None else settings
    if workshop.web_off(item, allowed):
        # A web page while they are off: listed, never handed to the engine.
        return None
    _bounded_text(entry_id, label="playlist entry id", maximum_bytes=MAX_IDENTIFIER_BYTES)
    bundle = session.pairings.resolve_accepted(item, session.library)
    if item.unplayable is not None:
        # Listed, never played: linux-wallpaperengine can't run it, or a
        # preset's base isn't there (`library.workshop.playback`).
        return None
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
        own_video = bundle.renderer.applied("video")
        if own_video and renderer_dials:
            lines.append(f"video = {_dial_table(own_video)}")
        elif own_video and withheld is not None:
            withheld.add(str(item.path))
    elif item.kind is Kind.SCENE:
        # ASCII digits only: `str.isdigit` also takes "١٢٣", which no Steam
        # folder is named and which the service refuses.
        if not workshop.is_workshop_id(item.scene):
            return None
        _bounded_text(item.scene, label="scene id", maximum_bytes=MAX_IDENTIFIER_BYTES)
        lines.append(f"scene_id = {_quote(item.scene)}")
        # A preset: what the engine renders, and with which values. Written
        # only for one, so a document without presets is byte for byte what a
        # 0.4.0 service reads (docs/wallpaper-engine.md).
        if item.scene_base:
            if not workshop.is_workshop_id(item.scene_base):
                return None
            lines.append(f"scene_base = {_quote(item.scene_base)}")
        # A preset's values, then the person's own over them (docs/renderer-settings.md).
        values = item.scene_properties
        own_values = _own_properties(item, bundle.renderer)
        if own_values and presets:
            values = (
                *((name, value) for name, value in values if name not in own_values),
                *own_values.items(),
            )
        elif own_values and withheld is not None:
            withheld.add(item.scene)
        if values:
            properties = _scene_properties(values)
            if properties is None:
                return None
            lines.append(f"properties = {properties}")
        own_scene = _own_scene_dials(bundle.renderer, compiled_for)
        if own_scene and renderer_dials:
            lines.append(f"scene = {_dial_table(own_scene)}")
        elif own_scene and withheld is not None:
            withheld.add(item.scene)
    lines.append(f"palette = {_palette(bundle.palette, session.settings.preview_scheme)}")
    return tuple(lines)


def _dial_table(table: Sequence[tuple[str, dials.Value]]) -> str:
    """A wallpaper's own dials as the inline table the service reads: typed values only."""
    return "{ " + ", ".join(f"{key} = {dials.toml_value(value)}" for key, value in table) + " }"


def _own_properties(item: MediaItem, own: pairings.RendererOverrides) -> dict[str, str]:
    """The person's own Wallpaper Engine values for ``item`` that the engine would read.

    Only for a property ``item`` declares and a person may set
    (`MediaItem.scene_settings`, empty for a web page and for one that can't
    play), each checked against that declaration (`workshop.own_value`).
    """
    if item.web_page or not own.properties:
        return {}
    declared = {setting.name: setting for setting in item.scene_settings}
    kept: dict[str, str] = {}
    for name, value in own.properties:
        setting = declared.get(name)
        text = workshop.own_value(setting, value) if setting is not None else None
        if text is not None:
            kept[name] = text
    return kept


def _own_scene_dials(
    own: pairings.RendererOverrides, settings: config.Settings
) -> tuple[tuple[str, dials.Value], ...]:
    """A scene's own dials, with the volume it would have when it sounds and the default doesn't.

    ``[renderer]`` writes ``scene_volume = 0`` while scenes are silent, as
    every document before the dials did; a scene that sounds by itself then
    carries the default's volume, the one Settings shows it following.
    """
    table = own.applied("scene")
    keys = {key for key, _value in table}
    if ("muted", False) in table and "volume" not in keys and settings.scene_muted:
        order = [known.key for known in dials.PER_WALLPAPER["scene"]]
        table = tuple(
            sorted((*table, ("volume", settings.scene_volume)), key=lambda p: order.index(p[0]))
        )
    return table


def _renderer_dial_lines(settings: config.Settings) -> list[str]:
    """The ``[renderer]`` lines of the dials added with schema 7: each only off its default.

    Not one that does nothing as things are set: the ``--auto-mode`` trigger
    while videos play when hidden, pausing only for the active window while
    scenes never pause.
    """
    lines: list[str] = []
    for dial in config.DIAL_SETTINGS:
        if dial.released:
            continue
        if dial.setting == "video_hidden_trigger" and settings.video_when_hidden == "play":
            continue
        if dial.setting == "scene_pause_only_active" and not settings.scene_pause_when_covered:
            continue
        value = getattr(settings, dial.setting)
        if value != dial.default:
            lines.append(f"{dial.setting} = {dials.toml_value(value)}")
    return lines


def _scene_properties(properties: Sequence[tuple[str, str]]) -> str | None:
    """A preset's values as the inline table the service reads, or ``None`` if out of bounds.

    The scan already kept only what the service accepts (`library.workshop`);
    this is the last look before the values become renderer arguments.
    """
    if len(properties) > workshop.MAX_PROPERTIES or len({name for name, _ in properties}) != len(
        properties
    ):
        return None
    pairs: list[str] = []
    for name, value in sorted(properties):
        if not workshop.PROPERTY_NAME.fullmatch(name) or not workshop.valid_property_value(value):
            return None
        pairs.append(f"{name} = {_quote(value)}")
    return "{ " + ", ".join(pairs) + " }"


# -- what can play: the compiler's own answer, for every page that asks ---------------
#
# These answer from accepted memory, without reading a file, so a page can ask
# on the GTK thread: a card's pill, a greyed-out picker row, a refusal as a
# gesture is made, and the guards on edits that change only what the app
# itself holds (playlist entries, a rule's or a display's playlist, the
# default, Quick choice, a still choice). That is the user experience. A
# change that can take away files or what the scan finds (Delete, Move to
# Trash, a library folder, Wallpaper Engine scanning) can't be judged from
# memory: its guarantee is the dry run of the real scan, cleanup and compiler
# on the authoring worker (`wall_in_one.dry_run`).

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
    the GTK thread may ask, and the pages and the compiler cannot disagree
    about the library as accepted. What a change to files or sources would
    do to it is the dry run's to say (`wall_in_one.dry_run`), not this.
    """
    if item is None:
        return Playability(False, False, "it is not in the library now")
    why = workshop.why_not_played(item, session.settings.allow_web_wallpapers)
    if why:
        # A Wallpaper Engine item nothing can play as it is (or a web page
        # while they are off): never compiled, and no still would change that.
        return Playability(False, False, why)
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
        # A web page (or a preset of one) with previews off never gets one of
        # its own making: the way out is to pick one, not to wait.
        reason = (
            "playback can\u2019t use it"
            if still is not None
            else "it has no still, and gets none of its own while previews are off: pick one"
            if workshop.no_automatic_still(item, session.library.previews)
            else "it has no still for playback to show yet"
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


def default_playlist_id(reference: str, session: Session) -> str:
    """The id of the playlist the saved default ``reference`` names; empty when it names none.

    A saved default is a reference: a playlist's id or, from an older
    profile, a hand edit or a setup that manages ``settings.toml``, its name.
    The start gives a by-name default its id, unless the file can't be saved;
    then the name stays for the whole session and still names its playlist
    here and in the compiler (`find_playlist`). Every page that asks "is this
    playlist the default?" asks this, never ``reference == playlist.id``, so
    such a default is shown as the default it is and not as "All media".

    The generated All media playlist is the default by its id or exact name,
    as the compiler matches it (`is_fallback_reference`), even where the
    settings can't be saved and the name stays.

    Empty for nothing saved, and for a reference no playlist answers to (the
    compiler leaves it out, `LeftOut`, until the start clears it or a
    playlist answers to it): no playlist is the default then.
    """
    if is_fallback_reference(reference):
        return FALLBACK_PLAYLIST_ID
    found = find_playlist(reference, session) if reference else None
    return found.id if found is not None else ""


def use_refusal(reference: str, session: Session) -> str:
    """Why ``reference`` can't be the default, a rule's or a display's playlist; empty if it can.

    A reference to a playlist the compiler can't build is left out of the
    runtime document (`LeftOut`): the default would play "All media", the rule
    or the display's own playlist would not apply. Nobody means that by
    choosing one, so the choice is refused here instead. A playlist whose
    every wallpaper playback skips compiles but never shows anything. "All
    media" can always be used.
    """
    if is_fallback_reference(reference):
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
    also when nothing that compiles would remain: whatever uses a playlist
    the compiler can't build is left out of the runtime document (`LeftOut`),
    which is not what removing a wallpaper is meant to do. A playlist that
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
    """A change refused because it would leave a playlist with nothing to play.

    A courtesy at the gesture, not what publication relies on: the compiler
    builds without a playlist that can't play and says so (`LeftOut`).

    Raised at the head of the authoring lane (or, in the headless service,
    just before the write). ``kind`` lets a control-socket caller branch on
    it (`control.server.failed`); the message is the whole sentence.
    """

    kind = "nothing-to-play"


class LibraryNotCurrentError(ValueError):
    """A playlist change refused because the library it would be judged by isn't current.

    After a removal commits, until a scan after it is accepted, and after the
    library folders or Workshop scanning change, until their scan lands, the
    application knows its in-memory library may still name files that are
    gone. A wallpaper taken from it, or a check judged by it, could pass and
    then leave a playlist with nothing to play once the scan lands, which
    the compiler builds without (`LeftOut`). Raised at the head of the
    authoring lane, by the application (`Application.require_current_library`);
    nothing is written. ``kind`` lets a control-socket caller branch on it.
    """

    kind = "library-not-current"


def _quoted_name(playlist: Playlist) -> str:
    return f"\u201c{playlist.name}\u201d"


#: `change_problem`: a playlist in use would have nothing left to play.
LEAVES_NOTHING_TO_PLAY: Final = "leaves-nothing-to-play"
#: `change_problem`: a playlist would hold entries, none of which the compiler keeps.
LEAVES_ONLY_UNUSABLE: Final = "leaves-only-unusable"


def change_problem(before: Sequence[Playability], after: Sequence[Playability], used: bool) -> str:
    """What a playlist going from ``before`` to ``after`` would break, or empty.

    Two rules, judged by what the compiler keeps (`_compile`). A playlist in
    use must keep something to play (`leaves_nothing_to_play`):
    `LEAVES_NOTHING_TO_PLAY`. And any playlist that holds entries must keep
    one the compiler keeps, used or not, because a playlist with entries but
    none it can resolve is not in the runtime document at all:
    `LEAVES_ONLY_UNUSABLE`. A playlist that already broke that rule may still
    change, so it can be cleaned up. The words are the caller's
    (`change_refusal` has some).
    """
    if used:
        return LEAVES_NOTHING_TO_PLAY if leaves_nothing_to_play(before, after) else ""
    breaks = after and not any(entry.compiles for entry in after)
    was_fine = not before or any(entry.compiles for entry in before)
    return LEAVES_ONLY_UNUSABLE if breaks and was_fine else ""


def allowed_as_already_broken(
    before: Sequence[Playability], after: Sequence[Playability], used: bool
) -> bool:
    """Whether `change_problem` lets a change pass only because the playlist was broken before.

    What the change leaves is broken all the same: a playlist in use with
    nothing that plays, or one holding only entries the compiler drops. It is
    let pass so a broken playlist can be cleaned up. That allowance stands on
    ``before``, and so on the library ``before`` was judged by. A library held
    in memory can be older than what is on disk (another program added a
    folder, or a file, and the playlist that plays it), and then the playlist
    was fine and this change is what breaks it. So whoever allows a write on
    this answer confirms it first against a scan from disk
    (`asked_of_what_is_saved`, `wall_in_one.dry_run`).
    """
    if change_problem(before, after, used):
        return False
    if used:
        return not any(entry.plays for entry in after)
    return bool(after) and not any(entry.compiles for entry in after)


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


def uses_that_decide(
    playlist: Playlist,
    after: Sequence[Playability],
    settings: config.Settings,
    session: Session,
) -> tuple[str, ...]:
    """`playlist_uses` for a change that takes something away from ``playlist``.

    While something still plays afterwards the change is fine, used or not,
    and what uses the playlist is not asked for. Once nothing would play, the
    answer turns on whether anything uses it, and a view of what is saved
    that could not read the default, the rules or the displays' own can't
    say: that refuses, naming the file (`require_read`). A session that is
    not such a view has read everything it holds.
    """
    if not any(entry.plays for entry in after):
        require_read(session, SAVED_USES)
    return playlist_uses(playlist.id, settings, session)


def entries_refusal(
    playlist: Playlist,
    after: Sequence[Entry],
    settings: config.Settings,
    session: Session,
    *,
    taking_away: bool = True,
) -> str:
    """`change_refusal` for giving ``playlist`` the entries ``after``, against what is saved.

    ``taking_away`` is false for an addition: it takes nothing from what the
    playlist plays, so its answer never waits on what uses the playlist
    (`uses_that_decide`).
    """
    known = library_index(session)
    can_before = playlist_playability(playlist.entries, session, known)
    can_after = playlist_playability(after, session, known)
    uses = (
        uses_that_decide(playlist, can_after, settings, session)
        if taking_away
        else playlist_uses(playlist.id, settings, session)
    )
    if allowed_as_already_broken(can_before, can_after, bool(uses)):
        session.note_already_broken()
    return change_refusal(_quoted_name(playlist), can_before, can_after, uses)


def emptied_by_removing(
    sources: Iterable[Path],
    settings: config.Settings,
    session: Session,
    *,
    uses_of: Callable[[Playlist], Sequence[str]] | None = None,
    label_of: Callable[[Playlist], str] = _quoted_name,
) -> list[str]:
    """Each refusal (`change_refusal`) taking ``sources`` out of the library would earn.

    Early feedback at the gesture, from accepted memory: every entry naming
    one goes with it. It is not the guarantee: a removal is checked again on
    the authoring worker, before anything is moved or deleted, by a dry run of
    the real scan, cleanup and compiler (`wall_in_one.dry_run`), which also
    sees the stills other wallpapers play through. ``uses_of`` and
    ``label_of`` put a playlist's uses and name in a page's own words (a page
    may count a default it asked for and that has not saved yet);
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
        uses = uses_that_decide(playlist, after, settings, session)
        if allowed_as_already_broken(before, after, bool(uses)):
            session.note_already_broken()
        refusal = change_refusal(_quoted_name(playlist), before, after, uses)
        if refusal:
            found.append(refusal)
    return found


# -- the application's boundary: one check for every write that could break it -------
#
# Every write that puts a playlist into use, or can take away what a playlist
# plays, calls one of these from its ``prepare``: on GTK, when its task
# reaches the head of the authoring lane, after every earlier write has been
# adopted. Whichever window, page or control verb asked, it is the same
# check. Each refusal is a `PlaybackGuardError`, raised before anything is
# written.
#
# What this process holds is not all that is saved, though. Nothing watches
# ``settings.toml`` or the stores, and another program (``ctl`` elsewhere, the
# service, an older version, a hand edit) can have saved a default, a rule or
# a display assignment long before the gesture. So the check at the head of
# the lane is the early answer, and the one that counts is asked again on the
# worker, just before the write, of what is saved at that moment
# (`judged_on_what_is_saved`).


def unreadable(unread: Sequence[tuple[str, str]]) -> config.SettingsUnreadableError:
    """The refusal for a question that consults files which could not be read, naming them.

    In the stores' own words for a file they can't read ("... can't be read
    (...), so ..."): whichever refuses first, the person reads the same thing.
    """
    files = "; ".join(f"{name} can't be read ({fault})" for name, fault in unread)
    return config.SettingsUnreadableError(f"{files}, so this change could not be checked")


def require_read(view: Session, needs: Sequence[str]) -> None:
    """Refuse, naming the file, when ``view`` could not read one of the files ``needs``.

    ``view`` is a view of what is saved (`Session.saved_view`) and ``needs``
    the files a question consults, as `wall_in_one.session.SAVED_USES` names
    them. A file that couldn't be read is not "nothing saved there", so the
    question can't be answered and the write it guards isn't made. Only the
    files the question consults count: a damaged schedule file has no say in
    whether a wallpaper may join a playlist. Any other Session has read all
    it holds, and nothing is refused.
    """
    unread = view.unread(*needs)
    if unread:
        raise unreadable(unread)


def asked_of_what_is_saved[Answer](
    ask: Callable[[Session], Answer],
    session: Session,
    *,
    needs: Sequence[str],
) -> Callable[[], Answer]:
    """``ask``, to be put on the worker to what is saved at that moment.

    Called on GTK when a write reaches the head of the lane. The function it
    returns is called on the worker just before the write: it reads
    ``settings.toml`` and every store from disk (`Session.saved_view`), puts
    ``ask`` to that view and lets it go. ``needs`` names the files ``ask``
    consults; one of them that could not be read refuses the write with kind
    ``unreadable`` (`require_read`), and no other file does.

    The view's library is the one this process accepted, while the saved
    folders are the ones it was scanned from; otherwise it is scanned from
    disk there and then. This is read just before the write, not held across
    it: each file has its own lock and no write takes two, so a program that
    saves between this read and the write is not seen. The store then applies
    its change, by id, to what it reads under its own lock.
    """
    saved = session.saved_view()

    def answer() -> Answer:
        view = saved()
        try:
            require_read(view, needs)
            answered = ask(view)
            # Let pass only because a playlist was already broken, as the
            # library this process holds has it (`allowed_as_already_broken`)?
            # Then asked again of a scan from disk, which may know the
            # playlist as fine until this change.
            confirmed = view.confirmed_on_disk() if view.noted_already_broken else None
            if confirmed is None:
                return answered
            try:
                return ask(confirmed)
            finally:
                confirmed.shutdown()
        finally:
            view.shutdown()

    return answer


def judged_on_what_is_saved[Work](
    refuse: Callable[[Session], None],
    work: Callable[[], Work],
    session: Session,
    *,
    needs: Sequence[str],
) -> Callable[[], Work]:
    """``work``, once ``refuse`` lets it pass here and again on the worker.

    ``refuse`` raises if the write may not be made as the session it is given
    stands. It is asked at once of ``session``: the early answer, from what
    this process holds, when the write reaches the head of the lane. It is
    asked again on the worker, just before ``work`` writes, of what is saved
    at that moment (`asked_of_what_is_saved`, which says what ``needs`` is).
    That second answer is the one that counts. A playlist that another
    program made the default since a window opened is in use, whatever the
    window remembers.
    """
    refuse(session)
    judge = asked_of_what_is_saved(refuse, session, needs=needs)

    def judged() -> Work:
        judge()
        return work()

    return judged


def unresolved_on_what_is_saved(item: MediaItem, session: Session) -> Callable[[], list[str]]:
    """`pairing_refusals_if_unresolved`, to be asked on the worker of what is saved then.

    The function it returns is called once it is known the change would leave
    ``item`` with no still, just before the write. It consults the playlists,
    to know which hold ``item``, and what uses one only when that playlist
    would be left with nothing to play (`uses_that_decide`).
    """
    return asked_of_what_is_saved(
        lambda view: pairing_refusals_if_unresolved(item, view),
        session,
        needs=(SAVED_PLAYLISTS,),
    )


def default_refusal_on_what_is_saved(
    changes: Mapping[str, object], session: Session
) -> Callable[[], str]:
    """`settings_refusal`, to be asked on the worker of what is saved then.

    A new default is judged playable from the playlists as they are on disk
    when it is written, not only from the ones this process holds.
    """
    wanted = dict(changes)
    return asked_of_what_is_saved(
        lambda view: settings_refusal(wanted, view), session, needs=(SAVED_PLAYLISTS,)
    )


def checked_entries_removal[Work](
    reference: str,
    entry_ids: Iterable[str],
    work: Callable[[], Work],
    session: Session,
    *,
    said_by_caller: bool = False,
) -> Callable[[], Work]:
    """``work`` (it takes ``entry_ids`` out of playlist ``reference``), once checked.

    It consults the playlists, and what uses this one only when it would be
    left with nothing to play (`uses_that_decide`). With ``said_by_caller``
    the refusal is the reason alone, for a caller that says "was not removed;
    nothing changed" itself.
    """
    chosen = frozenset(entry_ids)

    def refuse(view: Session) -> None:
        playlist = view.playlists.find(reference)
        refusal = entries_refusal(
            playlist,
            [entry for entry in playlist.entries if entry.id not in chosen],
            view.settings,
            view,
        )
        if refusal:
            raise PlaybackGuardError(
                refusal
                if said_by_caller
                else f"Nothing was removed from {_quoted_name(playlist)}: {refusal}"
            )

    return judged_on_what_is_saved(refuse, work, session, needs=(SAVED_PLAYLISTS,))


def checked_entries_addition[Work](
    reference: str,
    sources: Iterable[Path],
    work: Callable[[], Work],
    session: Session,
    *,
    said_by_caller: bool = False,
) -> Callable[[], Work]:
    """``work`` (it adds ``sources`` to playlist ``reference``), once checked.

    An addition takes nothing away, so nothing here turns on what uses the
    playlist, and no file is needed to say so. The playlists themselves are
    the store ``work`` writes to: when their file can't be read, the question
    is left to that store, which refuses the write or, for a damaged file,
    sets it aside as it always has. With ``said_by_caller`` the refusal is
    the reason alone, for a caller that says "was not added; nothing changed"
    itself.
    """
    adding = [Entry(f"adding-{index}", str(source)) for index, source in enumerate(sources)]

    def refuse(view: Session) -> None:
        if view.unread(SAVED_PLAYLISTS):
            return
        playlist = view.playlists.find(reference)
        refusal = entries_refusal(
            playlist, [*playlist.entries, *adding], view.settings, view, taking_away=False
        )
        if refusal:
            raise PlaybackGuardError(
                refusal
                if said_by_caller
                else f"Nothing was added to {_quoted_name(playlist)}: {refusal}"
            )

    return judged_on_what_is_saved(refuse, work, session, needs=())


def new_playlist_refusal(name: str, sources: Iterable[Path | str], session: Session) -> str:
    """`change_refusal` for a new playlist ``name`` made holding ``sources``; empty if fine.

    The one rule that applies to a new playlist: one with entries must hold
    one the compiler keeps (`LEAVES_ONLY_UNUSABLE`), used or not, or it would
    be made and never reach the runtime document. An empty one is fine, also
    under a name a saved reference already names: that reference is left out
    of the document until the playlist has something to play (`LeftOut`). It
    stops at the first entry that compiles, so a copy of a large library is
    answered at once.
    """
    known = library_index(session)
    after: list[Playability] = []
    for source in sources:
        can = entry_playability(known.get(Path(source)), session)
        if can.compiles:
            return ""
        after.append(can)
    return change_refusal(f"\u201c{name}\u201d", (), after, ())


def checked_new_playlist[Work](
    name: str,
    sources: Iterable[Path | str],
    work: Callable[[], Work],
    session: Session,
) -> Callable[[], Work]:
    """``work`` (it makes playlist ``name`` holding ``sources``, a copy included), once checked."""
    held = tuple(sources)

    def refuse(view: Session) -> None:
        refusal = new_playlist_refusal(name, held, view)
        if refusal:
            raise PlaybackGuardError(refusal)

    # Judged by the library alone: no saved file has a say in it.
    return judged_on_what_is_saved(refuse, work, session, needs=())


def checked_playlist_use[Work](
    reference: str,
    what: str,
    work: Callable[[], Work],
    session: Session,
) -> Callable[[], Work]:
    """``work`` (it makes playlist ``reference`` a rule's or a display's), once checked."""

    def refuse(view: Session) -> None:
        refusal = use_refusal(reference, view)
        if refusal:
            raise PlaybackGuardError(f"{what}: {refusal}")

    # Whether the playlist has something to play. The rules and the displays'
    # own are where ``work`` writes, and have no say in that.
    return judged_on_what_is_saved(refuse, work, session, needs=(SAVED_PLAYLISTS,))


def refuse_unresolved_quick_choice(item: MediaItem, session: Session) -> None:
    """Refuse Quick choice of a wallpaper the compiler can't keep, saying why.

    Quick choice replaces "your pick" with that one wallpaper; one the
    compiler drops would leave it holding nothing it can build. (One that
    playback skips is refused by the callers before this, as it always was.)
    """
    can = entry_playability(item, session)
    if not can.compiles:
        raise PlaybackGuardError(f"Playback unavailable for {item.name}: {can.reason}")


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
    """Why a settings change may not be saved (a default with nothing to play); or empty.

    A change to the library folders or Wallpaper Engine scanning is checked
    on the authoring worker instead, by a dry run of the real scan and
    compiler (`wall_in_one.dry_run.sources`), before ``settings.toml`` is
    written.
    """
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
    for dial in config.DIAL_SETTINGS:
        if dial.coerce(getattr(settings, dial.setting)) is None:
            raise RuntimeConfigError(f"{dial.setting} must be {config.describe_dial(dial)}")
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


def render(
    settings: config.Settings,
    session: Session,
    *,
    presets: bool = True,
    renderer_dials: bool = True,
    nothing_to_play: bool = True,
) -> str:
    """Return the TOML with every currently executable decision resolved.

    ``presets``: whether Wallpaper Engine presets are compiled (see
    `presets_supported`); without them the document never needs schema 6.
    ``renderer_dials``: whether the renderer dials are (`dials_supported`);
    without them it never needs schema 7. ``nothing_to_play``: whether
    schema 8's additions may be written (`nothing_to_play_supported`):
    ``allow_web_wallpapers``, and a playlist that plays nothing, without
    which an All media that plays nothing is refused.
    """
    return _compile(
        settings,
        session,
        presets=presets,
        renderer_dials=renderer_dials,
        nothing_to_play=nothing_to_play,
    ).document


def compiled(
    settings: config.Settings,
    session: Session,
    *,
    presets: bool = True,
    renderer_dials: bool = True,
    nothing_to_play: bool = True,
) -> Compiled:
    """Both documents and what they were built without (`LeftOut`); nothing is written."""
    return _compile(
        settings,
        session,
        presets=presets,
        renderer_dials=renderer_dials,
        nothing_to_play=nothing_to_play,
    )


def nothing_to_play_supported(status: object) -> bool:
    """Whether the runtime that answered ``status`` reads schema 8 (a playlist that plays nothing).

    The twin of `presets_supported` and `dials_supported` for a playlist
    that plays nothing and ``allow_web_wallpapers``.
    """
    return _lists_schema(status, NOTHING_TO_PLAY_SCHEMA_VERSION)


def dials_supported(status: object) -> bool:
    """Whether the runtime that answered ``status`` reads the dials (`DIALS_SCHEMA_VERSION`).

    `presets_supported`'s twin: `publish_changes` leaves the dials out
    exactly when this is false.
    """
    return _lists_schema(status, DIALS_SCHEMA_VERSION)


def compiled_as_published(
    settings: config.Settings, session: Session, status: object, generation: str
) -> Compiled:
    """The compile whose document the service holds (``generation``), of those publishing makes.

    Publishing leaves schema 8's ``allow_web_wallpapers`` out unless the
    service lists 8 *and* its loaded units are verified, then the dials
    unless it lists 7 and they are, then presets unless it lists 6 and they
    are (`_fitted`), while ``status`` only shows the lists. A caller comparing
    generations (the health sync) therefore accepts any of them: everything,
    without 8's additions, without them and the dials, without all three,
    and the one ``status`` calls for. When none matches, that last one is
    returned and the caller treats the stores as having moved on. A variant
    that can't be built (an All media that plays nothing, below schema 8) is
    skipped; when none can be, its refusal is raised.
    """
    asked = (
        presets_supported(status),
        dials_supported(status),
        nothing_to_play_supported(status),
    )
    first: Compiled | None = None
    refused: RuntimeConfigError | None = None
    tried: set[tuple[bool, bool, bool]] = set()
    for presets, renderer_dials, eight in (
        asked,
        (True, True, True),
        (True, True, False),
        (True, False, False),
        (False, False, False),
    ):
        if (presets, renderer_dials, eight) in tried:
            continue
        tried.add((presets, renderer_dials, eight))
        try:
            built = _compile(
                settings,
                session,
                presets=presets,
                renderer_dials=renderer_dials,
                nothing_to_play=eight,
            )
        except NothingToPlayNeedsNewerServiceError as error:
            refused = refused or error
            continue
        if document_generation(built.document) == generation:
            return built
        first = built if first is None else first
    if first is None:
        assert refused is not None
        raise refused
    return first


def presets_supported(status: object) -> bool:
    """Whether the runtime that answered ``status`` reads presets (`PRESET_SCHEMA_VERSION`).

    For a caller that already holds the live status and must compile what the
    publisher would have published for that service (the health sync compares
    generations): `publish_changes` leaves presets out exactly when this is false.
    """
    return _lists_schema(status, PRESET_SCHEMA_VERSION)


def _lists_schema(status: object, schema: int) -> bool:
    if not isinstance(status, Mapping) or status.get("status_version") != 2:
        return False
    supported = status.get("supported_config_schemas")
    return isinstance(supported, list) and schema in [
        version for version in supported if type(version) is int
    ]


def render_overrides(settings: config.Settings, session: Session) -> str | None:
    """Return ``runtime-overrides.toml`` for what reaches the wire, or ``None``.

    Only a compiled playlist (one with playable entries) can carry its own
    rotation, and only an independent display assignment can beat global
    rules; mirrored mode leaves assignments, and their opt-ins, dormant.
    An assignment left out of ``runtime.toml`` (`LeftOut`) is left out here
    too. ``None`` means no override is in use and the file must not exist.
    """
    return _compile(settings, session).overrides


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


def _unseen_folder(entries: Iterable[Entry], unseen: tuple[Unseen, ...]) -> Unseen | None:
    """The first place the scan could not look into that covers one of ``entries``.

    Covers: the place is the entry's path or one of its parent folders (path
    ancestry, not text). Only the kinds that mean "files may be there now"
    count (a failed listing or stat, a scan limit); a library folder that is
    absent, not a folder or a link was looked at and holds nothing usable, so
    it does not. None when there is no such place.
    """
    refusing = [place for place in unseen if place.kind.could_not_look]
    if not refusing:
        return None
    for entry in entries:
        path = Path(entry.path)
        ancestry = {path, *path.parents}
        for place in refusing:
            if place.path in ancestry:
                return place
    return None


def _could_not_look(place: Unseen) -> str:
    """The scan's note for ``place``, said as what it could not do."""
    return f"it could not look into {place.note}"


def _unseen_advice(place: Unseen) -> str:
    if place.kind is UnseenKind.LIMIT:
        return (
            "a folder past a scan limit isn't read; "
            "make the library smaller, or remove the reference."
        )
    return "make the folder readable, or remove the reference."


def _fallback_advice(place: Unseen, *, implicit: bool) -> str:
    """The way out of a refused All media: make it readable, or stop depending on it."""
    way = (
        "make the library smaller" if place.kind is UnseenKind.LIMIT else "make the folder readable"
    )
    if implicit:
        return f"{way}, choose a default playlist, or remove the folder from the library."
    return f"{way}, or remove the reference."


def _owner_phrase(owner: str, owner_id: str) -> str:
    if owner == SCHEDULE_OWNER:
        return f"schedule rule {owner_id!r}"
    if owner == DISPLAY_OWNER:
        return f"display {owner_id!r}"
    return "the default"


def _is_preset(item: MediaItem) -> bool:
    """Whether ``item`` would be compiled with a preset's ``scene_base``/``properties``."""
    return item.kind is Kind.SCENE and bool(item.scene_base or item.scene_properties)


def _document_head(
    settings: config.Settings,
    *,
    schema_version: int,
    default: str,
    web_allowed: bool,
    dial_lines: Sequence[str],
    scene_muted: bool,
) -> list[str]:
    """The lines of ``runtime.toml`` before its playlists: the header and its two tables.

    ``web_allowed``: write ``allow_web_wallpapers = true`` (schema 8).
    """
    return [
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
        *(["allow_web_wallpapers = true"] if web_allowed else []),
        "",
        "[renderer]",
        f"noctalia_program = {_quote(_program('noctalia'))}",
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
        # A scene's own sound, apart from a video's: linux-wallpaperengine
        # takes audio at launch only, so coupling the two made every video
        # slider step restart a scene. Silent writes a volume of 0, as every
        # document did before scenes had a sound setting, so an untouched
        # profile keeps its bytes (`_own_scene_dials` for one that sounds).
        # A document without the dials keeps the constants of before: a scene's
        # own silence or volume is a dial that waits, and the global sound
        # would otherwise play what that scene was set to silence.
        f"scene_muted = {str(scene_muted).lower()}",
        f"scene_volume = {0 if scene_muted else settings.scene_volume}",
        f"scene_pause_when_covered = {str(settings.scene_pause_when_covered).lower()}",
        f"scene_scaling = {_quote(settings.scene_scaling)}",
        f"scene_clamp = {_quote(settings.scene_clamp)}",
        *dial_lines,
    ]


def _compile(
    settings: config.Settings,
    session: Session,
    *,
    presets: bool = True,
    renderer_dials: bool = True,
    nothing_to_play: bool = True,
) -> Compiled:
    """Both generated documents, ``runtime.toml`` and its optional overrides, and what
    they were built without.

    **Tolerant of a playlist that can't play.** A playlist with nothing the
    runtime could play (it is empty, or every wallpaper in it is missing or
    has no still yet) is not emitted. A reference to one, or to a playlist
    that doesn't exist, is left out and returned (`LeftOut`): the default is
    built as "All media", a schedule rule or a display's own playlist is not
    emitted. Everything else is published. No save path has to prove it can't
    create such a state, against what another program saved meanwhile, for
    the next publication to get through; the guards that refuse to create one
    (`checked_playlist_use` and the rest) are good manners, not what this
    relies on. The service checks that every reference names an emitted
    playlist, so nothing left out is ever named in either document.

    **Except where the scan could not look.** A playlist that plays nothing
    only because the library scan could not look into a place its entries
    (or their stills) may be in (a failed listing or stat, a scan limit; `_unseen_folder`)
    refuses the build, naming the folder (or, for a video whose still could not
    be looked at, the video itself): files may be there now. So does All media
    when it resolves nothing (named, implicit, or the fallback for a left-out
    default) while the scan could not look into a library folder, or, with
    favourites only, into a starred file's folder. A library
    folder that is absent, not a folder or a link was looked at, and is left
    out as above.

    **Only over stores read in full.** A store that couldn't be read, is
    damaged or was saved by a newer build refuses the whole build, naming the
    file (`Session.authoring_faults`): "couldn't look" is never "nothing
    there", and a playlist that only seems to be missing must not quietly
    turn its rules off. Everything else this checks still refuses too: the
    wire's size and count bounds, duplicate ids, text limits, renderer
    settings.

    **All media may play nothing.** When it resolves nothing (and the scan
    could look), it is written as a playlist that plays nothing, with its
    reason (web wallpapers are off, or nothing in the library or the
    favourites can play), at schema 8, and said (`NOTHING_OWNER`) when the
    document plays it. Without ``nothing_to_play`` (a service not confirmed
    for schema 8) that is refused (`NothingToPlayNeedsNewerServiceError`),
    never written in a shape the service can't load.
    """
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
    fallback_narrowed = False
    if settings.cycle_favourites_only:
        starred = tuple(item for item in fallback_items if item.path in session.favourites.paths)
        if starred:
            fallback_items = starred
            fallback_narrowed = True

    # Presets the live service hasn't confirmed it reads: left out of every
    # playlist and said once each, so everything else keeps publishing.
    presets_left_out: dict[str, LeftOut] = {}
    allow_web = settings.allow_web_wallpapers
    # Wallpapers whose own dials (or values) were left out for an older service.
    withheld: set[str] = set()

    def entry_lines(item: MediaItem, entry_id: str) -> tuple[str, ...] | None:
        if not presets and _is_preset(item) and not workshop.web_off(item, allow_web):
            # (A web preset while web is off is left out for that, said by the
            # library as the owner's setting, not as a service that is too old.)
            if item.unplayable is None:
                presets_left_out.setdefault(
                    item.scene,
                    LeftOut(PRESET_OWNER, item.scene, item.name, "", NEEDS_NEWER_SERVICE),
                )
            return None
        return _resolved_entry(
            item,
            session,
            entry_id,
            allow_web=allow_web,
            settings=settings,
            presets=presets,
            renderer_dials=renderer_dials,
            withheld=withheld,
        )

    fallback_entries: list[tuple[str, ...]] = []
    for item in fallback_items:
        compiled_lines = entry_lines(item, entry_id_for_source(item.path))
        if compiled_lines is not None:
            fallback_entries.append(compiled_lines)
    playlists.append((FALLBACK_PLAYLIST_ID, FALLBACK_PLAYLIST_NAME, tuple(fallback_entries)))
    # Why All media plays nothing, when it does: the owner's words first.
    fallback_nothing = (
        ""
        if fallback_entries
        else WEB_OFF_REASON
        if any(workshop.web_off(item, allow_web) for item in fallback_items)
        else FAVOURITES_REASON
        if fallback_narrowed
        else EMPTY_LIBRARY_REASON
    )

    # All media is a dynamic inventory, not an authored list of paths. If it
    # resolves nothing, an unseen place may still hold something it would
    # play. A favourites-only subset needs only its starred files' evidence,
    # all of them: a starred file under an unreadable folder is not in the
    # library, so it is not among `fallback_items`. An unrelated folder
    # cannot make the starred files resolve.
    fallback_unseen = None
    if not fallback_entries:
        fallback_unseen = (
            _unseen_folder(
                (Entry(id="", source=str(path)) for path in sorted(session.favourites.paths)),
                session.library.unseen,
            )
            if fallback_narrowed
            else next(
                (place for place in session.library.unseen if place.kind.could_not_look),
                None,
            )
        )

    existing_ids = {FALLBACK_PLAYLIST_ID}
    # Playlists that play nothing only because the scan could not look under a
    # folder their entries may be in: "couldn't look" is not "nothing there".
    unseen: dict[str, Unseen] = {}
    # Playlists that play nothing only because their presets were left out for
    # an older service: what names them is said as that, not "nothing to play".
    presets_only: set[str] = set()
    rotations: list[tuple[str, int | None, bool | None]] = []
    for playlist in session.playlists.all():
        resolved: list[tuple[str, ...]] = []
        held_presets = False
        for authored_entry in playlist.entries:
            found_item = known.get(authored_entry.path)
            if found_item is None:
                continue
            compiled = entry_lines(found_item, authored_entry.id)
            if compiled is not None:
                resolved.append(compiled)
            elif found_item.scene in presets_left_out and _is_preset(found_item):
                held_presets = True
        if not resolved and held_presets:
            presets_only.add(playlist.id)
            continue
        if not resolved:
            # Nothing the runtime could play: empty, or every authored entry
            # is missing or unresolved. Not emitted; whatever refers to it is
            # left out below, and said (`LeftOut`, `NOTHING_TO_PLAY`), unless
            # the scan could not look where its entries may be.
            hidden = _unseen_folder(playlist.entries, session.library.unseen)
            if hidden is not None:
                unseen[playlist.id] = hidden
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

    left_out: list[LeftOut] = []
    # True only once the default has resolved to nothing named: a named All media
    # was already judged (and refused) in `runtime_playlist`.
    implicit_default = False
    # Whether anything the document plays names All media (the default, a rule
    # or a display that is emitted): then its playing nothing is said.
    fallback_named = False

    def require_known_fallback(owner: str = DEFAULT_OWNER, owner_id: str = "") -> None:
        """Refuse an All media that plays nothing only because the scan could not look.

        ``owner`` is who names it; the default owner with no ``owner_id`` and
        no named All media is the implicit case (no default chosen, or the
        chosen one left out). Nothing is written, whether or not a runtime
        document exists already.
        """
        if fallback_unseen is None:
            return
        place = fallback_unseen
        if owner == DEFAULT_OWNER and implicit_default:
            raise FallbackUnseenError(
                f"cannot compile runtime configuration because All media has nothing to play "
                f"while {place.path} can't be looked into ({place.note}); it plays when no "
                f"default playlist is chosen or the chosen one is left out. "
                f"Nothing was written; {_fallback_advice(place, implicit=True)}",
                place,
                implicit=True,
                owner=_owner_phrase(owner, owner_id),
            )
        raise FallbackUnseenError(
            f"cannot compile runtime configuration because {_owner_phrase(owner, owner_id)} "
            f"names All media, which has nothing to play while {place.path} can't be "
            f"looked into ({place.note}). Nothing was written; "
            f"{_fallback_advice(place, implicit=False)}",
            place,
            implicit=False,
            owner=_owner_phrase(owner, owner_id),
        )

    def runtime_playlist(
        reference: str, *, owner: str, owner_id: str = "", quiet: bool = False
    ) -> str | None:
        """The emitted playlist ``reference`` names; None, and noted, when it can't be used.

        ``quiet``: for a reference that does nothing while it stays as it is (a
        rule that is turned off). It is not emitted when it can't be used, and
        is neither said nor a reason to refuse.
        """
        nonlocal fallback_named
        if is_fallback_reference(reference):
            if not quiet:
                require_known_fallback(owner, owner_id)
                fallback_named = True
            return FALLBACK_PLAYLIST_ID
        authored = find_playlist(reference, session)
        if authored is None:
            if quiet:
                return None
            left_out.append(LeftOut(owner, owner_id, reference, "", DOES_NOT_EXIST))
            return None
        if authored.id not in existing_ids:
            if quiet:
                return None
            if authored.id in presets_only:
                left_out.append(
                    LeftOut(owner, owner_id, reference, authored.name, NEEDS_NEWER_SERVICE)
                )
                return None
            if authored.id in unseen:
                raise RuntimeConfigError(
                    f"cannot compile runtime configuration because playlist "
                    f"{authored.name!r}, which {_owner_phrase(owner, owner_id)} names, plays "
                    "nothing only as far as the library scan could look; "
                    f"{_could_not_look(unseen[authored.id])}. Nothing was written; "
                    f"{_unseen_advice(unseen[authored.id])}"
                )
            left_out.append(LeftOut(owner, owner_id, reference, authored.name, NOTHING_TO_PLAY))
            return None
        return authored.id

    default = (
        runtime_playlist(settings.active_playlist, owner=DEFAULT_OWNER)
        if settings.active_playlist
        else None
    ) or FALLBACK_PLAYLIST_ID
    if default == FALLBACK_PLAYLIST_ID:
        implicit_default = True
        require_known_fallback()
        fallback_named = True
    if fallback_nothing and not nothing_to_play:
        raise NothingToPlayNeedsNewerServiceError(
            f"cannot compile runtime configuration because \u201c{FALLBACK_PLAYLIST_NAME}\u201d "
            f"has nothing to play ({fallback_nothing}), and the running wallpaper service "
            "is not confirmed to read a playlist that plays nothing: restart it after an "
            "update, and make sure its installed units are current. Nothing was written."
        )
    left_out.extend(presets_left_out.values())
    carries_presets = any(
        line.startswith(("scene_base = ", "properties = "))
        for _identifier, _name, entries in playlists
        for entry in entries
        for line in entry
    )
    dial_lines = _renderer_dial_lines(settings)
    if dial_lines and not renderer_dials:
        withheld.add("")
        dial_lines = []
    # In a document stepped down without the dials (a service not confirmed for
    # schema 7, or presets on one not confirmed for 6), global scene sound waits
    # with them: a scene's own silence can't go with it. Say so when that holds
    # sound back. Any other document keeps the global sound.
    scene_muted = settings.scene_muted or not renderer_dials
    if not renderer_dials and not settings.scene_muted:
        withheld.add("")
    if withheld:
        left_out.append(LeftOut(DIALS_OWNER, "", "", "", NEEDS_NEWER_SERVICE))
    carries_dials = bool(dial_lines) or any(
        line.startswith(("video = ", "scene = "))
        for _identifier, _name, entries in playlists
        for entry in entries
        for line in entry
    )
    # Written only when on, so a document while it is off reads exactly as
    # one did before schema 8, which a service that predates it still loads.
    web_allowed = allow_web and nothing_to_play
    schema_version = (
        NOTHING_TO_PLAY_SCHEMA_VERSION
        if web_allowed or fallback_nothing
        else DIALS_SCHEMA_VERSION
        if carries_dials
        else PRESET_SCHEMA_VERSION
        if carries_presets
        else BATTERY_SCHEMA_VERSION
        if settings.stop_animations_on_battery
        else SCHEMA_VERSION
    )
    # Said when web wallpapers being off is why (the owner's words): an empty
    # library or favourites list is the library's own state, said there.
    if fallback_nothing == WEB_OFF_REASON and fallback_named:
        left_out.append(
            LeftOut(
                NOTHING_OWNER, "", FALLBACK_PLAYLIST_ID, FALLBACK_PLAYLIST_NAME, fallback_nothing
            )
        )
    lines = _document_head(
        settings,
        schema_version=schema_version,
        default=default,
        web_allowed=web_allowed,
        dial_lines=dial_lines,
        scene_muted=scene_muted,
    )

    for playlist_id, name, entries in playlists:
        lines.extend(("", "[[playlists]]", f"id = {_quote(playlist_id)}", f"name = {_quote(name)}"))
        if not entries:
            # Only All media is ever emitted without entries (`fallback_nothing`).
            lines.extend((f"nothing_to_play = {_quote(fallback_nothing)}", "entries = []"))
        for compiled_entry in entries:
            lines.extend(("", *compiled_entry))

    # Mirrored mode deliberately preserves connector rules in authoring but
    # leaves them dormant on the wire. Independent mode emits the complete
    # authored order: Rust evaluates the shared and connector-exact rules
    # together, so the established last-match-wins rule remains literal.
    # A rule whose playlist can't be used is left out whole (`LeftOut`):
    # whatever would apply without it applies.
    emitted_schedules: list[tuple[schedules.Rule, str]] = []
    for rule in session.schedules.rules:
        if settings.display_mode != config.DISPLAY_MODE_INDEPENDENT and rule.connector:
            continue
        rule_playlist = runtime_playlist(
            rule.playlist, owner=SCHEDULE_OWNER, owner_id=rule.id, quiet=not rule.enabled
        )
        if rule_playlist is not None:
            emitted_schedules.append((rule, rule_playlist))
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
    # An assignment whose playlist can't be used is left out (`LeftOut`), from
    # the overrides too: that display follows the rules and the default.
    emitted_assignments: list[tuple[str, str]] = []
    for connector, authored_playlist in assignments:
        own_playlist = runtime_playlist(authored_playlist, owner=DISPLAY_OWNER, owner_id=connector)
        if own_playlist is not None:
            emitted_assignments.append((connector, own_playlist))
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
    return Compiled(
        document,
        _overrides_document(rotations, beating),
        tuple(left_out),
        fallback_nothing,
    )


def nothing_to_play_document(settings: config.Settings, reason: str) -> str:
    """A ``runtime.toml`` that plays nothing on any display, for ``reason`` (schema 8).

    Built from ``settings`` alone, so it can be published when the library
    can't be compiled: All media with no entries is the only playlist, and
    the default. A service that loads it stops every renderer of its own and
    plays nothing until a document that names something replaces it; play,
    toggle and its own reload of the file can't bring anything back
    (`Application._web_off_guard`). Web wallpapers are never allowed in it.
    """
    _validate_settings_wire(settings)
    _bounded_text(reason, label="nothing_to_play reason", maximum_bytes=MAX_NOTHING_TO_PLAY_BYTES)
    dial_lines = _renderer_dial_lines(settings)
    lines = _document_head(
        settings,
        schema_version=NOTHING_TO_PLAY_SCHEMA_VERSION,
        default=FALLBACK_PLAYLIST_ID,
        web_allowed=False,
        dial_lines=dial_lines,
        scene_muted=settings.scene_muted,
    )
    lines.extend(
        (
            "",
            "[[playlists]]",
            f"id = {_quote(FALLBACK_PLAYLIST_ID)}",
            f"name = {_quote(FALLBACK_PLAYLIST_NAME)}",
            f"nothing_to_play = {_quote(reason)}",
            "entries = []",
        )
    )
    semantic_document = "\n".join(lines) + "\n"
    _validate_status_budget(semantic_document)
    lines.insert(2, f"config_generation = {_quote(_semantic_generation(semantic_document))}")
    return "\n".join(lines) + "\n"


def install_nothing_to_play(document: str, path: Path | None = None) -> None:
    """Replace ``runtime.toml`` with ``document`` (`nothing_to_play_document`), durably.

    The caller holds `compiler_lock` and has read what it replaces under it.
    The overrides file is left as it is: the service skips what in it names a
    playlist or display this document lacks. Such a document never allows web
    wallpapers, so it needs no look at ``settings.toml`` (`_web_setting_held`);
    one that does is refused, and nothing is written.
    """
    if allows_web_wallpapers(text=document):
        raise RuntimeConfigError(
            "a document that plays nothing never allows web wallpapers; it was not installed"
        )
    _install(document, path if path is not None else paths.runtime_config_path())


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


@contextmanager
def _web_setting_held(settings: config.Settings, target: Path) -> Iterator[None]:
    """Install ``target`` only while ``settings.toml`` agrees with ``settings`` where it matters.

    Every publication compiles from settings read before its library scan,
    or before its compile (the window's own snapshot, under ``--service``),
    and nothing that saves the settings takes the compiler lock. So the
    person can turn web wallpapers off while a publication that read them on
    is still compiling, and that one would then install a document that
    allows them (review r13c, D2). Only the setting's own lock, the one every
    writer of ``settings.toml`` takes (`config.save`, `config.mutate`), keeps
    it still: for a document compiled for web wallpapers on, it is taken
    here, inside the compiler lock (the order every writer keeps: compiler,
    then a store's own lock), ``allow_web_wallpapers`` is read again under
    it, and it is held across the install, so nothing the app saves can
    turn them off between that read and the rename.

    A document compiled for them off takes no lock (review r13d, N1): it
    neither allows them nor names a web page that can play (`_resolved_entry`
    leaves each out, `_document_head` the key), so installing it keeps the
    web-off rule whatever the file says meanwhile. The setting is still read
    once, without the lock, so that a snapshot taken before the person
    turned them on is compiled again for that rather than installed after
    the publication that turn-on asked for. A turn-on saved after that read
    is published after this install: its publication starts once the save
    is done (the window's own; under ``--service``, with the settings it
    saved) and waits for the compiler lock held here, so it follows this
    install and carries the key.

    When the file no longer says what ``settings`` does,
    `_WebSettingMovedError` is raised before anything is written, with what
    it says now: the caller compiles again for that and comes back here. A
    file that can't be read strictly, or isn't there, isn't known to allow
    web wallpapers, so it counts as off (their default). Every writer
    replaces it by a rename (`state_file.write_atomic_text`), so a read
    without the lock sees one whole file. A hand edit takes no lock and is
    outside this; the next publication, or the next start's compile, reads
    it.

    Only for the live ``runtime.toml``: a document for any other path is
    never loaded by the service. A lock this user can't open for writing (a
    read-only folder) can't be taken by any writer of the file either, so
    what the file says then holds without it. A lock that can't be had in
    time is a failed publication, nothing written, and the guard behind it
    (`Application._web_off_guard`) runs as for any other.
    """
    if target.absolute() != paths.runtime_config_path().absolute():
        yield
        return
    source = paths.settings_path()
    if not settings.allow_web_wallpapers:
        if _web_wallpapers_saved_on(source):
            raise _WebSettingMovedError(True)
        yield
        return
    with ExitStack() as held:
        if source.parent.is_dir():
            try:
                held.enter_context(state_file.mutation_lock(source, description="settings"))
            except OSError as error:
                if error.errno not in state_file.UNWRITABLE_LOCK_ERRNOS:
                    raise RuntimeConfigError(
                        f"cannot keep {source.name} as it is while the runtime configuration is "
                        f"installed ({error}); the last-known-good runtime configuration was "
                        "left untouched"
                    ) from error
        if not _web_wallpapers_saved_on(source):
            raise _WebSettingMovedError(False)
        yield


def _web_wallpapers_saved_on(source: Path) -> bool:
    """Whether ``source`` says ``allow_web_wallpapers = true`` now; unreadable says off."""
    try:
        return config.load_strict(source).allow_web_wallpapers
    except config.ConfigError:
        return False


def _web_setting_kept_moving() -> RuntimeConfigError:
    return RuntimeConfigError(
        "allow_web_wallpapers in settings.toml kept changing while the runtime configuration "
        "was compiled; the last-known-good runtime configuration was left untouched"
    )


def write(settings: config.Settings, session: Session, path: Path | None = None) -> Path:
    """Compile and install both documents, overrides first (see :func:`_publish`).

    Each file is replaced atomically on its own; the pair is not. A refused
    ``runtime.toml`` leaves both files as they were, and so does a failed
    ``runtime.toml`` install, which puts the previous overrides back. What is
    installed never allows web wallpapers while ``settings.toml`` says they
    are off, and is compiled again for them when it says on before the
    install (`_web_setting_held`).
    """
    target = path if path is not None else paths.runtime_config_path()
    with compiler_lock(target):
        for _attempt in range(WEB_SETTING_ATTEMPTS):
            built = _fitted(settings, session, target)
            document, overrides = built.document, built.overrides
            previous_overrides = _read_overrides(target)
            _require_publishable(document, target, built.nothing)
            try:
                with _web_setting_held(settings, target):
                    _publish(
                        target,
                        document,
                        overrides,
                        publish_overrides=not overrides_from_a_newer_build(previous_overrides),
                        previous_overrides=previous_overrides,
                    )
            except _WebSettingMovedError as moved:
                settings = replace(settings, allow_web_wallpapers=moved.fresh)
                continue
            return target
    raise _web_setting_kept_moving()


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
    return bool(publish_changes(settings, session, path))


def publish_changes(
    settings: config.Settings, session: Session, path: Path | None = None
) -> Published:
    """`update`, saying which files it wrote or removed (``--write-config`` reports them).

    A `RuntimeConfigNotDurableError` carries the same answer in ``changes``:
    every file listed was published, only a folder sync failed.

    What is installed, or kept as it is, never allows web wallpapers while
    ``settings.toml`` says they are off (`_web_setting_held`): when the
    person changed the setting after ``settings`` was read, the document is
    compiled again for what it says now (review r13c, D2). So an unchanged
    document that allows web wallpapers is not kept once they were turned
    off either. Only a document compiled for them on waits for the
    setting's lock (review r13d, N1).
    """
    target = path if path is not None else paths.runtime_config_path()
    with compiler_lock(target):
        for _attempt in range(WEB_SETTING_ATTEMPTS):
            try:
                return _publish_changes_locked(settings, session, target)
            except _WebSettingMovedError as moved:
                settings = replace(settings, allow_web_wallpapers=moved.fresh)
    raise _web_setting_kept_moving()


def _publish_changes_locked(settings: config.Settings, session: Session, target: Path) -> Published:
    """`publish_changes`'s one attempt, with the compiler lock held."""
    built = _compile(settings, session)
    try:
        current = file_io.read_regular_text(target, MAX_RUNTIME_CONFIG_BYTES)
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeConfigError(f"cannot read {target}: {error}") from error
    # Asked only when the document would change: an unchanged one was
    # already judged publishable when it was written.
    if current != built.document:
        built = _fitted(settings, session, target, built)
    document, overrides = built.document, built.overrides
    current_overrides = _read_overrides(target)
    document_changed = current != document
    overrides_changed = current_overrides != overrides and not overrides_from_a_newer_build(
        current_overrides
    )
    if document_changed:
        _require_publishable(document, target, built.nothing)
    # Asked before any socket call is over, never while one waits: only the
    # comparison and the renames run with the setting held.
    with _web_setting_held(settings, target):
        if not (document_changed or overrides_changed):
            return Published(left_out=built.left_out)
        sidecar = overrides_path(target)
        # Publication order: the overrides first, then runtime.toml.
        written = (
            *((sidecar,) if overrides_changed and overrides is not None else ()),
            *((target,) if document_changed else ()),
        )
        removed = (sidecar,) if overrides_changed and overrides is None else ()
        changes = Published(
            written=written,
            removed=removed,
            left_out=built.left_out,
            replaced=current if document_changed else None,
        )
        try:
            _publish(
                target,
                document if document_changed else None,
                overrides,
                publish_overrides=overrides_changed,
                previous_overrides=current_overrides,
            )
        except RuntimeConfigNotDurableError as uncertain:
            # Raised only once every planned file was published.
            uncertain.changes = changes
            raise
        return changes


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


def _fitted(
    settings: config.Settings,
    session: Session,
    target: Path,
    built: Compiled | None = None,
) -> Compiled:
    """The document for the live service: all of it, or as much as that service reads.

    Schema 8 (``allow_web_wallpapers``), 7 (the dials) and 6 (presets) are
    written only for what needs them, and a service that predates one refuses
    it whole (its ``deny_unknown_fields``). So one
    `runtime_compatibility.require_schema` can't confirm gets the document
    without 8's additions; then, if it can't confirm 7 either, without the
    dials; then without presets too (`LeftOut` says the last two). What
    `presets_supported`, `dials_supported` and `nothing_to_play_supported`
    compile from a status, exactly. An All media that plays nothing has no
    shape below 8: that document is returned as it is, and
    `_require_publishable` refuses it for such a service in the words it
    uses for any schema it can't confirm. Nothing is written.

    Leaving ``allow_web_wallpapers`` out while the setting is on is said
    (`WEB_OWNER`) when the document names a web page, unless the running
    service predates schema 8: one that reads 8 but isn't confirmed (its
    units can't be verified, or it didn't answer) refuses every web page in
    a document that doesn't allow them, while an older one plays them as
    0.4.0 did, with nothing to say.
    """
    built = built if built is not None else _compile(settings, session)
    refusal = None if built.nothing else _schema_refusal(built.document, target)
    if refusal is None:
        return built
    refused_web = settings.allow_web_wallpapers and not isinstance(
        refusal, runtime_compatibility.SchemaNotListedError
    )
    built = _compile(settings, session, nothing_to_play=False)
    if not _schema_publishable(built.document, target):
        built = _compile(settings, session, renderer_dials=False, nothing_to_play=False)
        if not _schema_publishable(built.document, target):
            built = _compile(
                settings, session, presets=False, renderer_dials=False, nothing_to_play=False
            )
    return _web_waiting(built, session) if refused_web else built


def _web_waiting(built: Compiled, session: Session) -> Compiled:
    """``built``, saying ``allow_web_wallpapers`` was left out of it when it names a web page."""
    named = _named_scenes(None, built.document)
    if not any(
        item.web_page and item.unplayable is None and item.scene in named
        for item in session.library.items
    ):
        return built
    said = LeftOut(WEB_OWNER, "", "", "", NEEDS_NEWER_SERVICE)
    return replace(built, left_out=(*built.left_out, said))


def _schema_publishable(document: str, target: Path) -> bool:
    """Whether ``document`` may go to the live service as it is, or must leave something out.

    Asked only of a document that needs presets or the dials, and only for
    the live path: 4 and 5 are `_require_publishable`'s to refuse, as before.
    """
    return _schema_refusal(document, target) is None


def _schema_refusal(
    document: str, target: Path
) -> runtime_compatibility.RuntimeCompatibilityError | None:
    """`_schema_publishable`, with why not: None when ``document`` may go as it is."""
    if target.absolute() != paths.runtime_config_path().absolute():
        return None
    schema = tomllib.loads(document)["schema_version"]
    if schema < PRESET_SCHEMA_VERSION:
        return None
    try:
        runtime_compatibility.require_schema(schema)
    except runtime_compatibility.RuntimeCompatibilityError as error:
        return error
    return None


def _require_publishable(document: str, target: Path, nothing: str = "") -> None:
    """Refuse, before any byte is written, a schema the live service cannot load.

    Only ``runtime.toml`` needs this: the overrides file is never read by a
    service that predates it, so it is safe to publish under any service.
    ``nothing``: why All media plays nothing in ``document`` (`Compiled.nothing`),
    said with the refusal, since nothing below schema 8 can say it.
    """
    if target.absolute() == paths.runtime_config_path().absolute():
        schema = tomllib.loads(document)["schema_version"]
        try:
            runtime_compatibility.require_schema(schema)
        except runtime_compatibility.RuntimeCompatibilityError as error:
            if nothing:
                raise NothingToPlayNeedsNewerServiceError(
                    f"\u201c{FALLBACK_PLAYLIST_NAME}\u201d has nothing to play ({nothing}), "
                    "and only a wallpaper service that reads schema_version "
                    f"{NOTHING_TO_PLAY_SCHEMA_VERSION} can be told so. {error}"
                ) from error
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
    state_file.record_write(sidecar)
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
    try:
        paths.ensure_directory(target.parent)
        descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    except OSError as error:
        # The temporary couldn't even be made (EACCES on a read-only
        # directory, say): a write failure like any other, old file in place.
        raise RuntimeConfigError(f"cannot write {target}: {error}") from error
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
    state_file.record_write(target)
    try:
        state_file.fsync_parent(target)
    except OSError as error:
        raise RuntimeConfigNotDurableError((target,), error) from error
