"""Tidy up: owner-approved cleanup of what older versions left behind.

Nothing here runs by itself. Each action is started by the user, after a
preview (:func:`plan`) that lists exactly what will change; :func:`apply`
refuses if anything changed since that preview, keeps a backup or an archive
first, and :func:`undo` puts it back. Archive, never delete: the only
deletion is the thumbnail cache, which is a cache.

The actions
-----------

``leftovers``
    Moves inert deletion leftovers into one dated folder under
    ``<config>/tidy-archive``. Only what can be *proven* inert moves:

    * empty claim folders (``.wall-in-one-retained/entry-xxxxxxxx``) at least
      an hour old. :func:`file_io.claim_for_deletion` makes a new one for
      every transient claim and never reuses one, so an old empty one is
      residue of a finished deletion;
    * empty deletion records (``.wall-in-one-retained/entry-<32 hex>``, 0
      bytes, one link): the terminal tombstones a finished discard leaves.
      Nothing reads the retained namespace;
    * the deployed upgrade's ``.wall-in-one-removal-<token>`` folders beside
      ``settings.toml`` and ``runtime.toml``, only while
      :func:`deployed_upgrade_transaction.finished_claims` proves the upgrade
      complete and the token is one of its slots. After completion nothing
      recovers, trusts or reuses those slots; they hold the pre-upgrade copies,
      which the archive keeps;
    * an empty ``.wall-in-one-removal-<token>`` beside managed downloads when
      the pending-removal journal reads cleanly, has no ``.broken`` copy and
      no intent with that token. Removal tokens are random, and recovery only
      ever follows journal intents.

    Everything else is listed as kept, with the reason: anything that holds
    data (including the Trash record copies), recent claim folders, folders
    from an unfinished or unrecorded operation, other file types, and entries
    on another filesystem than the archive. The ``.wall-in-one-retained``
    containers themselves never move: a claim in progress holds one open.

``palette-template``
    Points Noctalia's ``[theme.templates.user.wall-in-one]`` ``input_path``
    from the old, non-content-addressed ``palette.json.tmpl`` at the current
    content-addressed template, through :func:`template.edit_settings` (its
    backup, recovery record and atomic exchange), then asks Noctalia to reload.
    The tech review's R7 drop-in (``~/.config/noctalia/wall-in-one.toml``) is
    no substitute: Noctalia loads the config directory first and the state
    directory's ``settings.toml`` overrides it, so the stale entry would still
    win. Fixing it means editing ``settings.toml`` either way.

``old-palette-template``
    Archives the old ``palette.json.tmpl`` once Noctalia has rendered the
    palette after the switch. Noctalia skips writing an unchanged palette, and
    the two templates have the same bytes, so the evidence is the next change
    of colors after the reload; until then the step waits.

``plugin-settings``
    Removes the keys under ``[plugin_settings."goober/wall-in-one"]`` that the
    current companion plugin (0.1.3 and 0.2.0, which share that id) does not
    read: the retired Luau plugin's settings. Same machinery and backup as the
    template fix. Waits while the retired plugin's data still awaits its
    import decision, because the importer reads those keys.

``thumbnail-cache``
    Clears the thumbnail cache through :func:`thumbnails.clear`. It is only a
    cache and is rebuilt on demand, so nothing is archived and there is no
    undo.

Nothing touches Noctalia's settings while a deployed upgrade is unfinished: its
journal pins that file. Every apply and undo holds the profile-wide transaction
lock, which excludes the upgrade and the legacy importer.

The new UI (``--ui=next``) has no Settings page yet; when it gets one, its
AppState can expose these three functions as they are.
"""

from __future__ import annotations

import contextlib
import copy
import datetime
import hashlib
import json
import os
import re
import stat
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal

from wall_in_one import (
    config,
    deployed_upgrade_transaction,
    file_io,
    legacy_migration,
    paths,
    thumbnails,
)
from wall_in_one.library import manage, removals, state_file
from wall_in_one.library import pairing as pairing_conventions
from wall_in_one.theme import noctalia, template

Action = Literal[
    "leftovers",
    "palette-template",
    "old-palette-template",
    "plugin-settings",
    "thumbnail-cache",
]
LEFTOVERS: Final = "leftovers"
PALETTE_TEMPLATE: Final = "palette-template"
OLD_PALETTE_TEMPLATE: Final = "old-palette-template"
PLUGIN_SETTINGS: Final = "plugin-settings"
THUMBNAIL_CACHE: Final = "thumbnail-cache"
ACTIONS: Final[tuple[Action, ...]] = (
    "leftovers",
    "palette-template",
    "old-palette-template",
    "plugin-settings",
    "thumbnail-cache",
)
#: The actions the one-time card after an update offers. The thumbnail cache
#: is not leftover from an update: every profile has one.
OFFERED: Final[frozenset[str]] = frozenset(
    {LEFTOVERS, PALETTE_TEMPLATE, OLD_PALETTE_TEMPLATE, PLUGIN_SETTINGS}
)

ARCHIVE_DIRECTORY: Final = "tidy-archive"
MANIFEST: Final = "manifest.json"
MANIFEST_KIND: Final = "wall-in-one-tidy-up"
MANIFEST_VERSION: Final = 1
MAX_MANIFEST_BYTES: Final = 4 * 1024 * 1024
#: A claim folder this recently changed may belong to a deletion in progress.
CLAIM_GRACE_SECONDS: Final = 3600.0
#: The largest pre-upgrade copy the archive hashes (runtime.toml's own cap is 8 MiB).
MAX_CLAIM_ENTRY_BYTES: Final = 16 * 1024 * 1024
LOCK_TIMEOUT_SECONDS: Final = 30.0

PLUGIN_ID: Final = "goober/wall-in-one"
#: Noctalia stores a plugin panel's shell settings as ``<panel id>_<suffix>``
#: in the plugin's settings table (``plugin_panel_shell.cpp``).
PANEL_SHELL_SUFFIXES: Final = ("placement", "position", "layer", "open_near_click")
#: Every key the current companion reads, from its plugin.toml in 0.1.3 and
#: 0.2.0 (``companion_setting_keys`` derives it; a test checks both).
COMPANION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "refresh_interval_seconds",
        "binary_path",
        "controls_placement",
        "controls_position",
        "controls_layer",
        "controls_open_near_click",
        "display_mode",
        "glyph",
        "stopped_glyph",
        "color",
        "stopped_color",
    }
)

_CLAIM_FOLDER: Final = re.compile(r"\Aentry-[a-z0-9_]{8}\Z")
_DELETION_RECORD: Final = re.compile(r"\Aentry-[0-9a-f]{32}\Z")
_TOKEN: Final = re.compile(r"\A[0-9a-f]{32}\Z")
_TEMPLATE_TABLE: Final = ("theme", "templates", "user", template.TEMPLATE_ID)
_PLUGIN_TABLE: Final = ("plugin_settings", PLUGIN_ID)


class TidyError(Exception):
    """An action could not run; nothing was changed unless the message says so."""


class TidyChangedError(TidyError):
    """What the preview showed is no longer what is there; nothing was changed."""


@dataclass(frozen=True, slots=True)
class Change:
    """One thing an action changes, as the preview shows it."""

    path: Path
    detail: str
    size: int = 0


@dataclass(frozen=True, slots=True)
class Kept:
    """One thing an action deliberately leaves alone, and why."""

    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class Undo:
    """What :func:`undo` would put back for an action."""

    archive: Path
    applied: str
    detail: str


@dataclass(frozen=True, slots=True)
class ActionPlan:
    action: Action
    title: str
    summary: str
    changes: tuple[Change, ...] = ()
    kept: tuple[Kept, ...] = ()
    #: How the action backs up, archives or verifies; shown with the preview.
    notes: tuple[str, ...] = ()
    #: Why it cannot be applied now although there is something to change.
    blocked: str = ""
    undo: Undo | None = None
    #: A digest of exactly what this preview showed; :func:`apply` compares it.
    token: str = ""
    #: Set when a step after the change is still pending and can be retried
    #: with :func:`retry`; the text is the button's label.
    retry: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.changes) and not self.blocked

    @property
    def total_size(self) -> int:
        return sum(change.size for change in self.changes)


@dataclass(frozen=True, slots=True)
class Plan:
    actions: tuple[ActionPlan, ...]

    def action(self, name: str) -> ActionPlan:
        for found in self.actions:
            if found.action == name:
                return found
        raise KeyError(name)

    @property
    def offer(self) -> tuple[ActionPlan, ...]:
        """What the one-time card after the update lists: ready leftover actions."""
        return tuple(found for found in self.actions if found.action in OFFERED and found.ready)


@dataclass(frozen=True, slots=True)
class Result:
    action: Action
    changed: bool
    message: str
    archive: Path | None = None


# -- shared helpers --------------------------------------------------------------------


def format_size(size: int) -> str:
    """Decimal units, as GNOME's file manager shows them."""
    if size < 1000:
        return "1 byte" if size == 1 else f"{size} bytes"
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1000
        if value < 1000 or unit == "TB":
            return f"{value:.1f} {unit}"
    raise AssertionError("unreachable")  # pragma: no cover


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _digest(parts: Sequence[object]) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _real_directory(path: Path) -> os.stat_result | None:
    found = _lstat(path)
    return found if found is not None and stat.S_ISDIR(found.st_mode) else None


def _is_empty_directory(path: Path) -> bool:
    try:
        with os.scandir(path) as listing:
            return next(listing, None) is None
    except OSError:
        return False


def _upgrade_gate() -> str:
    """Why Noctalia's settings or the upgrade's evidence must not change now, or ""."""
    try:
        status = deployed_upgrade_transaction.probe().status
    except Exception as error:
        return f"Wall-in-One couldn't check its own upgrade ({error}), so this waits."
    if status in ("absent", "current", "complete"):
        return ""
    return (
        f"Wall-in-One's own upgrade hasn't finished (its status is {status}). "
        "Nothing here changes until it has."
    )


def archive_root() -> Path:
    return paths.app_config_dir() / ARCHIVE_DIRECTORY


@contextlib.contextmanager
def _exclusive() -> Iterator[None]:
    """The profile-wide writer lock: no upgrade or legacy import runs meanwhile."""
    try:
        with legacy_migration.profile_transaction(timeout=LOCK_TIMEOUT_SECONDS):
            yield
    except legacy_migration.MigrationError as error:
        raise TidyError(f"Another Wall-in-One task is busy; try again shortly ({error})") from error


# -- archives and their manifests ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Manifest:
    directory: Path
    document: dict[str, Any]

    @property
    def action(self) -> str:
        return str(self.document.get("action", ""))

    @property
    def state(self) -> str:
        return str(self.document.get("state", ""))

    @property
    def created(self) -> str:
        return str(self.document.get("created", ""))


_README = """\
Wall-in-One's Tidy up moved these files here on {created}.

Nothing in this folder was deleted. To put everything back where it was,
open Wall-in-One, go to Settings, find Tidy up, and choose Undo.
manifest.json lists where each item came from. Please don't move or edit
the items by hand while an Undo is still possible.
"""


def _new_archive(action: Action) -> Path:
    root = archive_root()
    paths.ensure_directory(root.parent)
    with contextlib.suppress(FileExistsError):
        root.mkdir(mode=0o700)
    found = _real_directory(root)
    if found is None:
        raise TidyError(f"{root} is not a folder, so nothing can be archived there")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    for attempt in range(1000):
        name = f"{stamp}-{action}" if attempt == 0 else f"{stamp}-{action}-{attempt + 1}"
        directory = root / name
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue
        (directory / "items").mkdir(mode=0o700)
        state_file.write_atomic_text(
            directory / "README.txt", _README.format(created=_now_iso()), mode=0o600
        )
        paths.fsync_directory(directory)
        paths.fsync_directory(root)
        paths.fsync_directory(root.parent)
        return directory
    raise TidyError(f"too many tidy-up archives were started at {stamp}")


def _write_manifest(directory: Path, document: Mapping[str, Any]) -> None:
    data = json.dumps(document, indent=2, sort_keys=True) + "\n"
    state_file.write_atomic_text(directory / MANIFEST, data, mode=0o600)


def _manifests() -> list[_Manifest]:
    """Every readable archive manifest, newest first."""
    root = archive_root()
    if _real_directory(root) is None:
        return []
    found: list[_Manifest] = []
    try:
        children = sorted(root.iterdir(), reverse=True)
    except OSError:
        return []
    for directory in children:
        if _real_directory(directory) is None:
            continue
        try:
            raw = file_io.read_regular_bytes(directory / MANIFEST, MAX_MANIFEST_BYTES)
            document = json.loads(raw) if raw is not None else None
        except OSError, ValueError:
            continue
        if (
            isinstance(document, dict)
            and document.get("kind") == MANIFEST_KIND
            and document.get("version") == MANIFEST_VERSION
        ):
            found.append(_Manifest(directory, document))
    found.sort(key=lambda manifest: (manifest.created, manifest.directory.name), reverse=True)
    return found


def _latest(action: Action, manifests: Sequence[_Manifest]) -> _Manifest | None:
    """The newest archive of ``action`` that Undo could still act on."""
    for manifest in manifests:
        if manifest.action == action and manifest.state in ("applying", "applied"):
            return manifest
    return None


# -- action 1: leftovers ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Leftover:
    path: Path
    is_directory: bool
    identity: file_io.PathIdentity
    fingerprint: file_io.FileFingerprint | None
    size: int
    detail: str
    #: The bytes a removal folder's ``entry`` held, when it held one.
    entry_sha256: str = ""


@dataclass(slots=True)
class _Survey:
    leftovers: list[_Leftover] = field(default_factory=list)
    kept: list[Kept] = field(default_factory=list)


def _leftover_parents(roots: Sequence[Path]) -> list[Path]:
    """Every folder the app deletes in, so every folder a leftover can be in."""
    candidates = [
        paths.app_config_dir(),
        paths.app_state_dir(),
        paths.noctalia_state_dir(),
        manage.trash_directory(),
    ]
    for root in roots:
        managed = root / pairing_conventions.MANAGED_PARENT
        if _real_directory(managed) is None:
            continue
        candidates.append(managed)
        try:
            children = sorted(managed.iterdir())
        except OSError:
            continue
        candidates.extend(child for child in children if _real_directory(child) is not None)
    unique: list[Path] = []
    seen: set[tuple[int, int]] = set()
    for candidate in candidates:
        found = _real_directory(candidate)
        if found is None or (found.st_dev, found.st_ino) in seen:
            continue
        seen.add((found.st_dev, found.st_ino))
        unique.append(candidate)
    return unique


def _removal_journal() -> tuple[frozenset[str] | None, str]:
    """Tokens of unfinished wallpaper removals, or ``None`` with the reason."""
    target = removals.state_path()
    broken = sorted(target.parent.glob(f"{target.name}.broken*"))
    if broken:
        return None, (
            f"an older copy of the removal journal ({broken[0].name}) may name it, so it "
            "isn't provably finished"
        )
    store = removals.Store.open()
    if store.fault is not None:
        return None, "the removal journal can't be read, so it isn't provably finished"
    return frozenset(intent.token for intent in store.records), ""


def _survey(roots: Sequence[Path], now: float) -> _Survey:
    survey = _Survey()
    finished = deployed_upgrade_transaction.finished_claims()
    archive_device = _real_directory(paths.app_config_dir())
    config_dir = _real_directory(paths.app_config_dir())
    state_dir = _real_directory(paths.app_state_dir())
    managed: set[tuple[int, int]] = set()
    for root in roots:
        top = root / pairing_conventions.MANAGED_PARENT
        found = _real_directory(top)
        if found is None:
            continue
        managed.add((found.st_dev, found.st_ino))
        with contextlib.suppress(OSError):
            for child in top.iterdir():
                if (inner := _real_directory(child)) is not None:
                    managed.add((inner.st_dev, inner.st_ino))
    journal: tuple[frozenset[str] | None, str] | None = None
    uid = os.getuid()

    def same(status: os.stat_result | None, other: os.stat_result | None) -> bool:
        return (
            status is not None
            and other is not None
            and (status.st_dev, status.st_ino) == (other.st_dev, other.st_ino)
        )

    def movable(path: Path, status: os.stat_result) -> str:
        if status.st_uid != uid:
            return "it belongs to another user"
        if archive_device is not None and status.st_dev != archive_device.st_dev:
            return "it's on a different drive from the archive folder, so it can't just be moved"
        return ""

    for parent in _leftover_parents(roots):
        parent_status = _real_directory(parent)
        in_managed = (
            parent_status is not None and (parent_status.st_dev, parent_status.st_ino) in managed
        )
        retained = parent / file_io.RETAINED_ENTRY_DIRECTORY
        if _real_directory(retained) is not None:
            try:
                entries = sorted(retained.iterdir())
            except OSError as error:
                survey.kept.append(Kept(retained, f"it can't be listed ({error.strerror})"))
                entries = []
            for entry in entries:
                status = _lstat(entry)
                if status is None:
                    continue
                if stat.S_ISDIR(status.st_mode) and _CLAIM_FOLDER.match(entry.name):
                    if not _is_empty_directory(entry):
                        survey.kept.append(Kept(entry, "this claim folder isn't empty"))
                    elif now - status.st_mtime < CLAIM_GRACE_SECONDS:
                        survey.kept.append(
                            Kept(
                                entry,
                                "it changed in the last hour and may belong to a removal "
                                "in progress",
                            )
                        )
                    elif reason := movable(entry, status):
                        survey.kept.append(Kept(entry, reason))
                    else:
                        survey.leftovers.append(
                            _Leftover(
                                entry,
                                True,
                                (status.st_dev, status.st_ino),
                                None,
                                0,
                                "empty claim folder from a finished removal",
                            )
                        )
                elif stat.S_ISREG(status.st_mode) and _DELETION_RECORD.match(entry.name):
                    if status.st_size > 0:
                        what = (
                            "a copy of a Trash record"
                            if same(parent_status, _real_directory(manage.trash_directory()))
                            else "a file"
                        )
                        survey.kept.append(
                            Kept(
                                entry,
                                f"{what} that holds {format_size(status.st_size)}; "
                                "only empty records are archived",
                            )
                        )
                    elif status.st_nlink != 1:
                        survey.kept.append(Kept(entry, "another name still links to this file"))
                    elif reason := movable(entry, status):
                        survey.kept.append(Kept(entry, reason))
                    else:
                        survey.leftovers.append(
                            _Leftover(
                                entry,
                                False,
                                (status.st_dev, status.st_ino),
                                file_io.file_fingerprint(status),
                                0,
                                "empty record from a finished removal",
                            )
                        )
                else:
                    survey.kept.append(Kept(entry, "Tidy up doesn't recognize this entry"))
        try:
            children = sorted(parent.iterdir())
        except OSError:
            children = []
        for child in children:
            if not child.name.startswith(file_io.DELETION_CLAIM_PREFIX):
                continue
            status = _lstat(child)
            if status is None or not stat.S_ISDIR(status.st_mode):
                continue
            token = child.name.removeprefix(file_io.DELETION_CLAIM_PREFIX)
            if not _TOKEN.match(token):
                survey.kept.append(Kept(child, "Tidy up doesn't recognize this folder's name"))
                continue
            if same(parent_status, config_dir) or same(parent_status, state_dir):
                role = "settings.toml" if same(parent_status, config_dir) else "runtime.toml"
                if finished is None:
                    survey.kept.append(
                        Kept(
                            child,
                            "Wall-in-One's upgrade isn't recorded as finished, so this may "
                            "still be needed to resume it",
                        )
                    )
                    continue
                tokens = (
                    finished.settings_tokens if role == "settings.toml" else finished.runtime_tokens
                )
                if token not in tokens:
                    survey.kept.append(
                        Kept(child, "it isn't one of the finished upgrade's folders")
                    )
                    continue
                try:
                    names = sorted(entry.name for entry in child.iterdir())
                except OSError as error:
                    survey.kept.append(Kept(child, f"it can't be listed ({error.strerror})"))
                    continue
                if not set(names) <= {"entry"}:
                    survey.kept.append(Kept(child, "it holds something besides the upgrade's copy"))
                    continue
                if reason := movable(child, status):
                    survey.kept.append(Kept(child, reason))
                    continue
                size = 0
                entry_sha = ""
                detail = "empty folder left by Wall-in-One's finished upgrade"
                if names:
                    entry_status = _lstat(child / "entry")
                    if entry_status is None or not stat.S_ISREG(entry_status.st_mode):
                        survey.kept.append(Kept(child, "its entry isn't a regular file"))
                        continue
                    try:
                        data = file_io.read_regular_bytes(child / "entry", MAX_CLAIM_ENTRY_BYTES)
                    except OSError as error:
                        survey.kept.append(Kept(child, f"its copy can't be read ({error})"))
                        continue
                    if data is None:
                        survey.kept.append(Kept(child, "its copy disappeared while it was read"))
                        continue
                    size = len(data)
                    entry_sha = _sha256(data)
                    stamp = datetime.datetime.fromtimestamp(entry_status.st_mtime).strftime(
                        "%Y-%m-%d"
                    )
                    detail = (
                        f"the copy of {role} from before Wall-in-One's upgrade "
                        f"({format_size(size)}, last changed {stamp}), kept by the finished "
                        "upgrade"
                    )
                survey.leftovers.append(
                    _Leftover(
                        child,
                        True,
                        (status.st_dev, status.st_ino),
                        None,
                        size,
                        detail,
                        entry_sha,
                    )
                )
            elif in_managed:
                if not _is_empty_directory(child):
                    survey.kept.append(
                        Kept(child, "it holds a file from a removal that didn't finish")
                    )
                    continue
                if journal is None:
                    journal = _removal_journal()
                tokens_in_use, why = journal
                if tokens_in_use is None:
                    survey.kept.append(Kept(child, why))
                elif token in tokens_in_use:
                    survey.kept.append(
                        Kept(child, "a wallpaper removal that hasn't finished uses it")
                    )
                elif reason := movable(child, status):
                    survey.kept.append(Kept(child, reason))
                else:
                    survey.leftovers.append(
                        _Leftover(
                            child,
                            True,
                            (status.st_dev, status.st_ino),
                            None,
                            0,
                            "empty folder from a finished wallpaper removal",
                        )
                    )
            else:
                survey.kept.append(
                    Kept(child, "Wall-in-One can't prove the operation that made it has finished")
                )
    return survey


def _leftovers_token(leftovers: Sequence[_Leftover]) -> str:
    return _digest(
        sorted(
            (str(item.path), item.is_directory, list(item.identity), item.size, item.entry_sha256)
            for item in leftovers
        )
    )


def _leftovers_summary(leftovers: Sequence[_Leftover]) -> str:
    if not leftovers:
        return "Nothing to archive."
    folders = sum(1 for item in leftovers if item.is_directory)
    files = len(leftovers) - folders
    parts = []
    if folders:
        parts.append(_plural(folders, "folder"))
    if files:
        parts.append(_plural(files, "file"))
    size = sum(item.size for item in leftovers)
    return f"Archive {' and '.join(parts)} ({format_size(size)}) left by older versions."


def _plan_leftovers(
    roots: Sequence[Path], now: float, manifests: Sequence[_Manifest]
) -> tuple[ActionPlan, list[_Leftover]]:
    survey = _survey(roots, now)
    latest = _latest(LEFTOVERS, manifests)
    undo = None
    if latest is not None:
        moved = sum(
            1
            for item in latest.document.get("items", [])
            if item.get("outcome") in ("moved", "planned")
        )
        partial = " (it stopped part way)" if latest.state == "applying" else ""
        undo = Undo(
            latest.directory,
            latest.created,
            f"Put back {_plural(moved, 'item')} from {latest.directory}{partial}.",
        )
    plan = ActionPlan(
        action=LEFTOVERS,
        title="Archive old leftovers",
        summary=_leftovers_summary(survey.leftovers),
        changes=tuple(Change(item.path, item.detail, item.size) for item in survey.leftovers),
        kept=tuple(survey.kept),
        notes=(
            f"Everything listed moves into a new dated folder in {archive_root()}, "
            "together with a list of where each item came from. Nothing is deleted, "
            "and Undo moves everything back.",
        ),
        undo=undo,
        token=_leftovers_token(survey.leftovers),
    )
    return plan, survey.leftovers


def _archived_name(index: int, path: Path) -> str:
    return f"{index:04d}-{path.name.lstrip('.') or 'entry'}"


def _fsync_all(directories: set[Path]) -> None:
    for directory in sorted(directories):
        with contextlib.suppress(OSError):
            paths.fsync_directory(directory)


def _move(source: Path, destination: Path, item: Mapping[str, Any]) -> None:
    identity = (int(item["identity"][0]), int(item["identity"][1]))
    if item["type"] == "dir":
        file_io.move_directory_no_replace(source, destination, expected_identity=identity)
        return
    raw = item.get("fingerprint")
    fingerprint: file_io.FileFingerprint | None = None
    if isinstance(raw, list) and len(raw) == 5:
        fingerprint = (int(raw[0]), int(raw[1]), int(raw[2]), int(raw[3]), int(raw[4]))
    file_io.atomic_move_no_replace(
        source,
        destination,
        expected_identity=identity,
        expected_fingerprint=fingerprint,
    )


def _apply_leftovers(expected: ActionPlan | None, roots: Sequence[Path]) -> Result:
    plan, leftovers = _plan_leftovers(roots, time.time(), _manifests())
    if expected is not None and expected.token != plan.token:
        raise TidyChangedError(
            "The leftovers changed since the preview; nothing was moved. Check the new list."
        )
    if not leftovers:
        return Result(LEFTOVERS, False, "There was nothing to archive.")
    archive = _new_archive(LEFTOVERS)
    items: list[dict[str, Any]] = []
    for index, item in enumerate(leftovers, start=1):
        items.append(
            {
                "original": str(item.path),
                "archived": f"items/{_archived_name(index, item.path)}",
                "type": "dir" if item.is_directory else "file",
                "identity": list(item.identity),
                "fingerprint": list(item.fingerprint) if item.fingerprint else None,
                "size": item.size,
                "entry_sha256": item.entry_sha256,
                "detail": item.detail,
                "outcome": "planned",
            }
        )
    document: dict[str, Any] = {
        "kind": MANIFEST_KIND,
        "version": MANIFEST_VERSION,
        "action": LEFTOVERS,
        "state": "applying",
        "created": _now_iso(),
        "items": items,
    }
    _write_manifest(archive, document)
    touched: set[Path] = {archive / "items"}
    moved = 0
    for record in items:
        source = Path(record["original"])
        destination = archive / record["archived"]
        try:
            _move(source, destination, record)
        except (OSError, ValueError) as error:
            record["outcome"] = f"skipped: {error}"
            continue
        touched.add(source.parent)
        if (
            record["type"] == "dir"
            and not record["entry_sha256"]
            and not _is_empty_directory(destination)
        ):
            # Something raced into it: it was not a leftover after all.
            with contextlib.suppress(OSError, ValueError):
                file_io.move_directory_no_replace(
                    destination,
                    source,
                    expected_identity=(int(record["identity"][0]), int(record["identity"][1])),
                )
            record["outcome"] = "skipped: it was no longer empty"
            continue
        record["outcome"] = "moved"
        moved += 1
    _fsync_all(touched)
    document["state"] = "applied"
    document["finished"] = _now_iso()
    _write_manifest(archive, document)
    skipped = len(items) - moved
    message = f"Archived {_plural(moved, 'item')} in {archive}."
    if skipped:
        message += f" {_plural(skipped, 'item')} changed meanwhile and stayed where it was."
    return Result(LEFTOVERS, moved > 0, message, archive)


def _undo_leftovers(manifest: _Manifest) -> Result:
    document = copy.deepcopy(manifest.document)
    restored = 0
    missing = 0
    touched: set[Path] = set()
    for record in document.get("items", []):
        if record.get("outcome") not in ("moved", "planned"):
            continue
        original = Path(record["original"])
        archived = manifest.directory / record["archived"]
        identity = (int(record["identity"][0]), int(record["identity"][1]))
        at_archive = _lstat(archived)
        if at_archive is None or (at_archive.st_dev, at_archive.st_ino) != identity:
            at_original = _lstat(original)
            if at_original is not None and (at_original.st_dev, at_original.st_ino) == identity:
                record["outcome"] = "restored"
                continue
            record["outcome"] = "missing"
            missing += 1
            continue
        if original.parent.name == file_io.RETAINED_ENTRY_DIRECTORY and (
            _lstat(original.parent) is None
        ):
            with contextlib.suppress(FileExistsError):
                original.parent.mkdir(mode=0o700)
            touched.add(original.parent.parent)
        try:
            if record["type"] == "dir":
                file_io.move_directory_no_replace(archived, original, expected_identity=identity)
            else:
                file_io.atomic_move_no_replace(archived, original, expected_identity=identity)
        except FileExistsError:
            record["outcome"] = "kept: something new has its name"
            missing += 1
            continue
        except (OSError, ValueError) as error:
            record["outcome"] = f"kept: {error}"
            missing += 1
            continue
        record["outcome"] = "restored"
        touched.update((original.parent, archived.parent))
        restored += 1
    _fsync_all(touched)
    document["state"] = "undone"
    document["undone"] = _now_iso()
    _write_manifest(manifest.directory, document)
    message = f"Put back {_plural(restored, 'item')}."
    if missing:
        message += (
            f" {_plural(missing, 'item')} couldn't be put back and "
            f"{'is' if missing == 1 else 'are'} listed in {manifest.directory / MANIFEST}."
        )
    return Result(LEFTOVERS, restored > 0, message, manifest.directory)


# -- Noctalia's settings: reading and line-exact editing -------------------------------


@dataclass(frozen=True, slots=True)
class _NoctaliaSettings:
    path: Path
    data: bytes
    text: str
    document: dict[str, Any]

    @property
    def sha256(self) -> str:
        return _sha256(self.data)


def _read_noctalia() -> tuple[_NoctaliaSettings | None, str]:
    """Noctalia's settings, or ``None`` with why (empty when simply absent)."""
    target = paths.noctalia_settings_path()
    if _lstat(target) is None:
        return None, ""
    try:
        data = template.read_settings_document()
        text = data.decode("utf-8")
        document = tomllib.loads(text)
    except template.TemplateInstallError as error:
        return None, f"Noctalia's settings can't be read safely ({error})."
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, RecursionError) as error:
        return None, f"Noctalia's settings aren't valid TOML ({error})."
    return _NoctaliaSettings(target, data, text, document), ""


def _table(document: Mapping[str, Any], keys: Sequence[str]) -> Any:
    node: Any = document
    for key in keys:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _toml_string(value: str) -> str:
    escaped = []
    for character in value:
        if character in ('"', "\\"):
            escaped.append("\\" + character)
        elif ord(character) < 0x20 or ord(character) == 0x7F:
            escaped.append(f"\\u{ord(character):04X}")
        else:
            escaped.append(character)
    return '"' + "".join(escaped) + '"'


def _header_path(line: str) -> tuple[str, ...] | None:
    """The table a ``[header]`` line opens, or ``None`` for any other line."""
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    try:
        parsed = tomllib.loads(stripped)
    except tomllib.TOMLDecodeError, RecursionError:
        return None
    found: list[str] = []
    node: Any = parsed
    while isinstance(node, dict) and len(node) == 1:
        key, node = next(iter(node.items()))
        found.append(key)
    return tuple(found) if node == {} else None


def _is_header(line: str) -> bool:
    stripped = line.strip()
    if stripped.startswith("[["):
        return True
    return _header_path(line) is not None


def _single_key(line: str) -> tuple[str, Any] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith("["):
        return None
    try:
        parsed = tomllib.loads(stripped)
    except tomllib.TOMLDecodeError, RecursionError:
        return None
    if len(parsed) != 1:
        return None
    return next(iter(parsed.items()))


def _table_body(lines: Sequence[str], table: tuple[str, ...]) -> tuple[int, int]:
    """The header index and the end (exclusive) of ``table``'s own lines."""
    headers = [index for index, line in enumerate(lines) if _header_path(line) == table]
    if len(headers) != 1:
        raise TidyError("its layout isn't one Wall-in-One can edit safely")
    start = headers[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if _is_header(lines[index]):
            end = index
            break
    return start, end


def _verified(text: str, expected: Mapping[str, Any]) -> str:
    try:
        if tomllib.loads(text) != expected:
            raise TidyError("the edit would change more than intended")
    except (tomllib.TOMLDecodeError, RecursionError) as error:
        raise TidyError(f"the edit wouldn't be valid TOML ({error})") from error
    return text


def _ending(line: str) -> str:
    return line[len(line.rstrip("\r\n")) :]


def _set_template_input(text: str, document: Mapping[str, Any], old: str, new: str) -> str:
    """``text`` with only the template's ``input_path`` changed from old to new."""
    lines = text.splitlines(keepends=True)
    start, end = _table_body(lines, _TEMPLATE_TABLE)
    matches = [
        index for index in range(start + 1, end) if _single_key(lines[index]) == ("input_path", old)
    ]
    if len(matches) != 1:
        raise TidyError("its template entry isn't laid out in a way Wall-in-One can edit safely")
    index = matches[0]
    line = lines[index]
    indent = line[: len(line) - len(line.lstrip())]
    lines[index] = f"{indent}input_path = {_toml_string(new)}{_ending(line) or chr(10)}"
    expected = copy.deepcopy(dict(document))
    entry = _table(expected, _TEMPLATE_TABLE)
    assert isinstance(entry, dict)
    entry["input_path"] = new
    return _verified("".join(lines), expected)


def _remove_plugin_keys(
    text: str, document: Mapping[str, Any], keys: Sequence[str]
) -> tuple[str, list[str]]:
    """``text`` without the lines of ``keys`` in the plugin table, and those lines."""
    lines = text.splitlines(keepends=True)
    start, end = _table_body(lines, _PLUGIN_TABLE)
    wanted = set(keys)
    removed: dict[str, list[int]] = {}
    for index in range(start + 1, end):
        found = _single_key(lines[index])
        if found is not None and found[0] in wanted:
            removed.setdefault(found[0], []).append(index)
    if set(removed) != wanted or any(len(indexes) != 1 for indexes in removed.values()):
        raise TidyError("some of these settings span several lines or are set elsewhere")
    dropped = sorted(index for indexes in removed.values() for index in indexes)
    kept_lines = [line for index, line in enumerate(lines) if index not in set(dropped)]
    expected = copy.deepcopy(dict(document))
    table = _table(expected, _PLUGIN_TABLE)
    assert isinstance(table, dict)
    for key in keys:
        del table[key]
    return _verified("".join(kept_lines), expected), [lines[index] for index in dropped]


def _restore_plugin_keys(text: str, document: Mapping[str, Any], removed: Sequence[str]) -> str:
    """Put removed key lines back at the top of the plugin table."""
    lines = text.splitlines(keepends=True)
    start, _end = _table_body(lines, _PLUGIN_TABLE)
    expected = copy.deepcopy(dict(document))
    table = _table(expected, _PLUGIN_TABLE)
    if not isinstance(table, dict):
        raise TidyError("the plugin's settings table is gone")
    restored: list[str] = []
    for line in removed:
        found = _single_key(line)
        if found is None or found[0] in table:
            raise TidyError("some of these settings exist again")
        table[found[0]] = found[1]
        restored.append(line if line.endswith("\n") else line + "\n")
    if not lines[start].endswith("\n"):
        lines[start] += "\n"
    return _verified("".join([*lines[: start + 1], *restored, *lines[start + 1 :]]), expected)


def companion_setting_keys(manifest: Mapping[str, Any]) -> frozenset[str]:
    """The settings keys a companion plugin.toml makes Noctalia store and read.

    Plugin-level ``[[setting]]`` keys; entry-level ``[[widget.setting]]``,
    ``[[panel.setting]]`` and ``[[desktop_widget.setting]]`` keys; and each
    panel's shell settings, which Noctalia keys ``<panel id>_<suffix>`` in the
    plugin's own table.
    """
    keys: set[str] = set()

    def settings(node: object) -> set[str]:
        if not isinstance(node, list):
            return set()
        return {str(entry["key"]) for entry in node if isinstance(entry, dict) and "key" in entry}

    keys |= settings(manifest.get("setting"))
    for kind in ("widget", "panel", "desktop_widget"):
        entries = manifest.get(kind)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            keys |= settings(entry.get("setting"))
            if kind == "panel" and isinstance(entry.get("id"), str):
                keys |= {f"{entry['id']}_{suffix}" for suffix in PANEL_SHELL_SUFFIXES}
    return frozenset(keys)


def _reload_noctalia() -> bool:
    """Ask the running shell to reread its settings; ``False`` if it couldn't."""
    try:
        noctalia.reload_config()
    except noctalia.NoctaliaError:
        return False
    return True


_SETTINGS_BACKUP_NOTE: Final = (
    "Before the change, Wall-in-One copies Noctalia's settings.toml to "
    "settings.toml.bak-wall-in-one-<date>-<time> beside it and keeps the replaced file "
    "next to that copy as …original. A third copy goes into a new dated folder in "
    "{archive}. The edit's own record leaves small inert entries in "
    "{retained} afterwards."
)


def _settings_backup_note() -> str:
    return _SETTINGS_BACKUP_NOTE.format(
        archive=archive_root(),
        retained=paths.noctalia_state_dir() / file_io.RETAINED_ENTRY_DIRECTORY,
    )


def _effective_state(manifest: _Manifest, current_sha: str | None) -> str:
    """A Noctalia edit's state, resolving one interrupted between its steps."""
    if manifest.state != "applying":
        return manifest.state
    if current_sha == manifest.document.get("after_sha256"):
        return "applied"
    if current_sha == manifest.document.get("before_sha256"):
        return "abandoned"
    return "applying"


def _settings_undo(
    action: Action, manifests: Sequence[_Manifest], current: str | None
) -> Undo | None:
    for manifest in manifests:
        if manifest.action != action:
            continue
        state = _effective_state(manifest, current)
        if state in ("applied", "applying"):
            return Undo(
                manifest.directory,
                manifest.created,
                "Put Noctalia's settings back as they were"
                + (
                    " (byte for byte)."
                    if current == manifest.document.get("after_sha256")
                    else "; settings changed since then keep their new values."
                ),
            )
        if state == "undone":
            return None
    return None


def _current_edit(
    action: Action, manifests: Sequence[_Manifest], current: str | None
) -> _Manifest | None:
    """The newest settings edit of ``action`` still in effect (not undone)."""
    for manifest in manifests:
        if manifest.action != action:
            continue
        state = _effective_state(manifest, current)
        if state in ("applied", "applying"):
            return manifest
        if state == "undone":
            return None
    return None


def _reload_pending(action: Action, manifests: Sequence[_Manifest], current: str | None) -> bool:
    """Whether the edit in effect is on disk but Noctalia never confirmed reloading it."""
    edit = _current_edit(action, manifests, current)
    return edit is not None and edit.document.get("reloaded") is not True


_RELOAD_PENDING_NOTE: Final = (
    "Noctalia didn't confirm that it reloaded its settings after this change, so the "
    "running shell may still use its old ones. Retry the reload once Noctalia is running."
)


# -- action 2: the palette template name -----------------------------------------------


def _stale_template() -> Path:
    return paths.app_state_dir() / template.TEMPLATE_FILENAME


def _plan_palette_template(
    settings: _NoctaliaSettings | None, unreadable: str, manifests: Sequence[_Manifest]
) -> ActionPlan:
    title = "Fix the palette template name"
    undo = _settings_undo(PALETTE_TEMPLATE, manifests, settings.sha256 if settings else None)
    if settings is None:
        return ActionPlan(
            PALETTE_TEMPLATE,
            title,
            unreadable or "Noctalia's settings weren't found, so there's nothing to fix.",
            blocked=unreadable,
            undo=undo,
        )
    entry = _table(settings.document, _TEMPLATE_TABLE)
    if not isinstance(entry, Mapping) or "input_path" not in entry:
        return ActionPlan(
            PALETTE_TEMPLATE,
            title,
            "Noctalia has no Wall-in-One palette template registered.",
            undo=undo,
        )
    try:
        target = template.installed_template_path()
    except template.TemplateInstallError as error:
        return ActionPlan(PALETTE_TEMPLATE, title, str(error), blocked=str(error), undo=undo)
    current = entry.get("input_path")
    stale = _stale_template()
    if current == str(target):
        if _reload_pending(PALETTE_TEMPLATE, manifests, settings.sha256):
            return ActionPlan(
                PALETTE_TEMPLATE,
                title,
                "Noctalia's settings name the current template, but Noctalia hasn't "
                "confirmed that it reloaded them.",
                notes=(_RELOAD_PENDING_NOTE,),
                undo=undo,
                retry="Retry Reload",
            )
        return ActionPlan(
            PALETTE_TEMPLATE, title, "Noctalia already uses the current template.", undo=undo
        )
    if current != str(stale):
        return ActionPlan(
            PALETTE_TEMPLATE,
            title,
            "Noctalia's template entry points at a file Wall-in-One didn't install, so it's "
            "left alone.",
            kept=(Kept(settings.path, f"input_path is {current!r}"),),
            undo=undo,
        )
    blocked = _upgrade_gate()
    if not blocked:
        try:
            _set_template_input(settings.text, settings.document, str(stale), str(target))
        except TidyError as error:
            blocked = f"Noctalia's settings can't be edited safely: {error}."
    changes = [
        Change(
            settings.path,
            f"[theme.templates.user.{template.TEMPLATE_ID}] input_path: "
            f"{_toml_string(str(stale))} → {_toml_string(str(target))}",
        )
    ]
    target_exists = _lstat(target) is not None
    if not target_exists:
        size = _lstat(template.bundled_template())
        changes.append(
            Change(
                target,
                "new: the bundled template under its content-addressed name"
                + (f" ({format_size(size.st_size)})" if size is not None else ""),
                size.st_size if size is not None else 0,
            )
        )
    return ActionPlan(
        PALETTE_TEMPLATE,
        title,
        "Noctalia still renders your colors from the old template file name.",
        changes=tuple(changes),
        notes=(
            _settings_backup_note(),
            "Then Wall-in-One asks Noctalia to reload its settings.",
            f"The old {stale.name} stays until Noctalia has rendered your colors from the "
            "new template; a separate step archives it after that.",
        ),
        blocked=blocked,
        undo=undo,
        token=_digest([settings.sha256, str(target), target_exists]),
    )


def _palette_fingerprint() -> list[int] | None:
    found = _lstat(paths.palette_path())
    if found is None:
        return None
    return [found.st_dev, found.st_ino, found.st_size, found.st_mtime_ns, found.st_ctime_ns]


def _apply_settings_edit(
    action: Action,
    settings: _NoctaliaSettings,
    transform: Callable[[str], str],
    extra: Mapping[str, Any],
    before_edit: Callable[[], dict[str, Any]] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Archive a copy, journal, edit through the template transaction, reload."""
    after_text = transform(settings.text)
    archive = _new_archive(action)
    state_file.write_atomic_bytes(
        archive / "items" / "settings.toml.before",
        settings.data,
        replace_existing=False,
        mode=0o600,
    )
    document: dict[str, Any] = {
        "kind": MANIFEST_KIND,
        "version": MANIFEST_VERSION,
        "action": action,
        "state": "applying",
        "created": _now_iso(),
        "settings_path": str(settings.path),
        "before_copy": "items/settings.toml.before",
        "before_sha256": settings.sha256,
        "after_sha256": _sha256(after_text.encode("utf-8")),
        **extra,
    }
    _write_manifest(archive, document)
    if before_edit is not None:
        try:
            document.update(before_edit())
        except (OSError, template.TemplateInstallError) as error:
            document["state"] = "abandoned"
            _write_manifest(archive, document)
            raise TidyError(f"Nothing in Noctalia's settings was changed: {error}") from error
        _write_manifest(archive, document)
    try:
        edit = template.edit_settings(settings.sha256, transform)
    except template.SettingsChangedError as error:
        document["state"] = "abandoned"
        _write_manifest(archive, document)
        raise TidyChangedError(
            "Noctalia changed its settings since the preview; nothing was changed. "
            "Check the new preview."
        ) from error
    except template.TemplateInstallError as error:
        # The exchange is the commit point, and a later step (keeping the
        # replaced file, retiring the transaction record) can fail after it.
        # What is on disk decides, never the error alone.
        outcome = _settings_outcome(document["after_sha256"], settings.sha256)
        if outcome == "untouched":
            document["state"] = "abandoned"
            _write_manifest(archive, document)
            raise TidyError(f"Noctalia's settings weren't changed: {error}") from error
        document["tail_error"] = str(error)
        if outcome == "unknown":
            # Stays "applying": Undo is offered for whatever did take effect.
            _write_manifest(archive, document)
            raise TidyError(
                "Noctalia's settings may have been changed, but Wall-in-One can't confirm "
                f"it ({error}). The change is recorded in {archive}, and Undo reverses it "
                "if it took effect."
            ) from error
    else:
        document["backup"] = str(edit.backup_path)
        document["displaced"] = str(edit.displaced_path)
        document["after_sha256"] = edit.after_sha256
    document["reloaded"] = _reload_noctalia()
    document["reloaded_at_ns"] = time.time_ns()
    document["palette_after_reload"] = _palette_fingerprint()
    document["state"] = "applied"
    document["finished"] = _now_iso()
    _write_manifest(archive, document)
    return archive, document


def _settings_outcome(after_sha256: object, before_sha256: object) -> str:
    """After a failed exchange: ``committed``, ``untouched`` or ``unknown``, from the bytes."""
    current, _unreadable = _read_noctalia()
    if current is not None and current.sha256 == after_sha256:
        return "committed"
    if current is not None and current.sha256 == before_sha256:
        return "untouched"
    return "unknown"


def _tail_note(document: Mapping[str, Any]) -> str:
    """What to add to a result whose change committed but whose cleanup failed."""
    error = document.get("tail_error")
    if not error:
        return ""
    return (
        f" The change was made, but a step after it failed ({error}); its recovery "
        "files are kept, and Undo is available."
    )


def _apply_palette_template(expected: ActionPlan | None) -> Result:
    template.recover_interrupted_edit()
    manifests = _manifests()
    settings, unreadable = _read_noctalia()
    plan = _plan_palette_template(settings, unreadable, manifests)
    if expected is not None and expected.token != plan.token:
        raise TidyChangedError(
            "Noctalia's settings changed since the preview; nothing was changed. "
            "Check the new preview."
        )
    if not plan.changes or settings is None:
        return Result(PALETTE_TEMPLATE, False, "There was nothing to fix.")
    if plan.blocked:
        raise TidyError(plan.blocked)
    stale = str(_stale_template())
    target = str(template.installed_template_path())

    def publish() -> dict[str, Any]:
        installed, created = template.ensure_installed_template()
        data = file_io.read_regular_bytes(installed, template.MAX_TEMPLATE_BYTES) or b""
        return {
            "created_template": str(installed) if created else None,
            "template_sha256": _sha256(data),
        }

    archive, document = _apply_settings_edit(
        PALETTE_TEMPLATE,
        settings,
        lambda text: _set_template_input(text, tomllib.loads(text), stale, target),
        {"old_input_path": stale, "new_input_path": target},
        publish,
    )
    if document.get("reloaded") is not True:
        return Result(
            PALETTE_TEMPLATE,
            True,
            "Noctalia's settings now name the current palette template, but Noctalia "
            "didn't confirm that it reloaded them, so the old template file stays in use "
            "until it does. Use Retry Reload once Noctalia is running. The old settings "
            f"were backed up beside them and in {archive}." + _tail_note(document),
            archive,
        )
    return Result(
        PALETTE_TEMPLATE,
        True,
        "Noctalia now uses the current palette template. Its old settings were backed up "
        f"beside them and in {archive}." + _tail_note(document),
        archive,
    )


def _restore_settings(manifest: _Manifest, reverse: Callable[[_NoctaliaSettings], str]) -> str:
    """Undo one settings edit: byte for byte if untouched since, else ``reverse``."""
    settings, unreadable = _read_noctalia()
    if settings is None:
        raise TidyError(unreadable or "Noctalia's settings are gone, so there's nothing to undo.")
    document = manifest.document
    if settings.sha256 == document.get("after_sha256"):
        before = file_io.read_regular_bytes(
            manifest.directory / str(document["before_copy"]), template.MAX_NOCTALIA_SETTINGS_BYTES
        )
        if before is None or _sha256(before) != document.get("before_sha256"):
            raise TidyError(
                f"The archived copy in {manifest.directory} doesn't match; nothing changed."
            )
        restored = before.decode("utf-8")
        exact = True
    elif settings.sha256 == document.get("before_sha256"):
        return "Noctalia's settings were already back as they were."
    else:
        restored = reverse(settings)
        exact = False
    tail = ""
    try:
        template.edit_settings(settings.sha256, lambda _text: restored)
    except template.SettingsChangedError as error:
        raise TidyChangedError("Noctalia changed its settings just now; try Undo again.") from error
    except template.TemplateInstallError as error:
        outcome = _settings_outcome(_sha256(restored.encode("utf-8")), settings.sha256)
        if outcome == "untouched":
            raise TidyError(f"Noctalia's settings weren't changed: {error}") from error
        if outcome == "unknown":
            raise TidyError(
                "Noctalia's settings may have been changed, but Wall-in-One can't confirm "
                f"it ({error}). Nothing is marked undone; check them and try Undo again."
            ) from error
        tail = f" A step after the change failed ({error}); its recovery files are kept."
    reloaded = _reload_noctalia()
    message = (
        "Noctalia's settings are back as they were."
        if exact
        else "Wall-in-One's change was reversed; settings changed since then keep their new values."
    )
    if not reloaded:
        message += " Noctalia didn't confirm that it reloaded them."
    return message + tail


def _undo_palette_template(manifest: _Manifest) -> Result:
    later = _latest(OLD_PALETTE_TEMPLATE, _manifests())
    if later is not None:
        raise TidyError(
            "Put the old template file back first: choose Undo on "
            "“Archive the old palette template”."
        )
    document = copy.deepcopy(manifest.document)
    old = str(document["old_input_path"])
    new = str(document["new_input_path"])

    def reverse(settings: _NoctaliaSettings) -> str:
        entry = _table(settings.document, _TEMPLATE_TABLE)
        if not isinstance(entry, Mapping) or entry.get("input_path") != new:
            raise TidyError("Noctalia's template entry changed since; there's nothing to undo")
        return _set_template_input(settings.text, settings.document, new, old)

    message = _restore_settings(manifest, reverse)
    created = document.get("created_template")
    if isinstance(created, str):
        installed = Path(created)
        settings, _unreadable = _read_noctalia()
        still_used = settings is not None and str(installed) in settings.text
        found = _lstat(installed)
        if found is not None and not still_used:
            destination = manifest.directory / "items" / installed.name
            with contextlib.suppress(OSError, ValueError):
                file_io.atomic_move_no_replace(
                    installed, destination, expected_identity=(found.st_dev, found.st_ino)
                )
                _fsync_all({installed.parent, destination.parent})
                message += f" The new template file went into {manifest.directory}."
    document["state"] = "undone"
    document["undone"] = _now_iso()
    _write_manifest(manifest.directory, document)
    return Result(PALETTE_TEMPLATE, True, message, manifest.directory)


# -- action 2b: the old palette template file ------------------------------------------


def _not_yet_rendered(settings: _NoctaliaSettings, manifests: Sequence[_Manifest]) -> str:
    """Why there is no proof yet that Noctalia renders from the new template, or "".

    With Tidy up's own switch in effect the proof is a palette written after
    Noctalia *confirmed* reloading its settings. A switch whose reload failed
    is not proof of anything, whatever the palette's time: the running shell
    may still render from the old file. Only a switch made outside Tidy up
    (for example by ``--install-theme-template``, which reloads Noctalia
    itself) falls back to a palette written after the settings were.
    """
    waiting = (
        "Waiting for Noctalia to render your colors from the new template. It does that "
        "the next time your colors change; then this step becomes available."
    )
    palette = _lstat(paths.palette_path())
    switch = _current_edit(PALETTE_TEMPLATE, manifests, settings.sha256)
    if switch is not None:
        document = switch.document
        reloaded_at = document.get("reloaded_at_ns")
        if document.get("reloaded") is not True or not isinstance(reloaded_at, int):
            return (
                "Noctalia didn't confirm that it reloaded its settings after the template "
                "fix, so it may still read this file. Choose Retry Reload on \u201cFix the "
                "palette template name\u201d."
            )
        if palette is None:
            return waiting
        current = [
            palette.st_dev,
            palette.st_ino,
            palette.st_size,
            palette.st_mtime_ns,
            palette.st_ctime_ns,
        ]
        rendered = current != document.get("palette_after_reload") and (
            palette.st_mtime_ns > reloaded_at
        )
        return "" if rendered else waiting
    found = _lstat(settings.path)
    if palette is None or found is None or palette.st_mtime_ns <= found.st_mtime_ns:
        return waiting
    return ""


def _plan_old_palette_template(
    settings: _NoctaliaSettings | None, manifests: Sequence[_Manifest]
) -> tuple[ActionPlan, os.stat_result | None]:
    title = "Archive the old palette template"
    stale = _stale_template()
    latest = _latest(OLD_PALETTE_TEMPLATE, manifests)
    undo = Undo(latest.directory, latest.created, f"Put {stale.name} back.") if latest else None
    found = _lstat(stale)
    if found is None:
        return ActionPlan(
            OLD_PALETTE_TEMPLATE, title, "There's no old template file.", undo=undo
        ), None
    if not stat.S_ISREG(found.st_mode):
        return (
            ActionPlan(
                OLD_PALETTE_TEMPLATE,
                title,
                f"{stale.name} isn't a regular file, so it's left alone.",
                kept=(Kept(stale, "not a regular file"),),
                undo=undo,
            ),
            None,
        )
    change = Change(
        stale,
        f"the old palette template ({format_size(found.st_size)}), once Noctalia no longer uses it",
        found.st_size,
    )
    token = _digest([list(file_io.file_fingerprint(found)), settings.sha256 if settings else ""])
    if settings is None:
        return (
            ActionPlan(
                OLD_PALETTE_TEMPLATE,
                title,
                "Noctalia's settings can't be checked, so the old template file stays.",
                kept=(Kept(stale, "Noctalia may still use it"),),
                undo=undo,
            ),
            None,
        )
    try:
        target = template.installed_template_path()
    except template.TemplateInstallError as error:
        return ActionPlan(OLD_PALETTE_TEMPLATE, title, str(error), undo=undo), None
    entry = _table(settings.document, _TEMPLATE_TABLE)
    if str(stale) in settings.text or not isinstance(entry, Mapping):
        return (
            ActionPlan(
                OLD_PALETTE_TEMPLATE,
                title,
                "Noctalia still uses the old template file; fix the template name first.",
                kept=(Kept(stale, "Noctalia's settings still name it"),),
                undo=undo,
            ),
            None,
        )
    if entry.get("input_path") != str(target):
        return (
            ActionPlan(
                OLD_PALETTE_TEMPLATE,
                title,
                "Noctalia's template entry doesn't use Wall-in-One's current template, so the "
                "old file stays.",
                kept=(Kept(stale, "Wall-in-One can't tell what Noctalia renders from"),),
                undo=undo,
            ),
            None,
        )
    blocked = ""
    if _lstat(target) is None:
        blocked = "The current template file is missing, so the old one stays for now."
    else:
        blocked = _not_yet_rendered(settings, manifests)
    return (
        ActionPlan(
            OLD_PALETTE_TEMPLATE,
            title,
            "Noctalia renders from the new template now, so the old file is no longer used."
            if not blocked
            else "The old template file can go once Noctalia has rendered from the new one.",
            changes=(change,),
            notes=(
                f"The file moves into a new dated folder in {archive_root()}; Undo moves it back.",
            ),
            blocked=blocked,
            undo=undo,
            token=token,
        ),
        found,
    )


def _apply_old_palette_template(expected: ActionPlan | None) -> Result:
    manifests = _manifests()
    settings, _unreadable = _read_noctalia()
    plan, found = _plan_old_palette_template(settings, manifests)
    if expected is not None and expected.token != plan.token:
        raise TidyChangedError("The old template file or Noctalia's settings changed; check again.")
    if not plan.changes or found is None:
        return Result(OLD_PALETTE_TEMPLATE, False, "There was nothing to archive.")
    if plan.blocked:
        raise TidyError(plan.blocked)
    stale = _stale_template()
    archive = _new_archive(OLD_PALETTE_TEMPLATE)
    fingerprint = file_io.file_fingerprint(found)
    document: dict[str, Any] = {
        "kind": MANIFEST_KIND,
        "version": MANIFEST_VERSION,
        "action": OLD_PALETTE_TEMPLATE,
        "state": "applying",
        "created": _now_iso(),
        "items": [
            {
                "original": str(stale),
                "archived": f"items/{stale.name}",
                "type": "file",
                "identity": list(fingerprint[:2]),
                "fingerprint": list(fingerprint),
                "size": found.st_size,
                "entry_sha256": "",
                "detail": "the old palette template",
                "outcome": "planned",
            }
        ],
    }
    _write_manifest(archive, document)
    record = document["items"][0]
    try:
        file_io.atomic_move_no_replace(
            stale,
            archive / record["archived"],
            expected_identity=fingerprint[:2],
            expected_fingerprint=fingerprint,
        )
    except (OSError, ValueError) as error:
        document["state"] = "abandoned"
        record["outcome"] = f"skipped: {error}"
        _write_manifest(archive, document)
        raise TidyError(f"The old template file wasn't moved: {error}") from error
    record["outcome"] = "moved"
    _fsync_all({stale.parent, archive / "items"})
    document["state"] = "applied"
    document["finished"] = _now_iso()
    _write_manifest(archive, document)
    return Result(OLD_PALETTE_TEMPLATE, True, f"Moved {stale.name} into {archive}.", archive)


# -- action 3: the retired plugin's settings -------------------------------------------


def _legacy_gate() -> str:
    try:
        status = legacy_migration.probe().status
    except Exception as error:
        return f"Wall-in-One couldn't check the retired plugin's data ({error}), so this waits."
    if status in ("absent", "imported", "declined"):
        return ""
    return (
        "The retired plugin's data still waits for its import decision, and that import "
        "reads these settings. Decide on the import first."
    )


def _plan_plugin_settings(
    settings: _NoctaliaSettings | None, unreadable: str, manifests: Sequence[_Manifest]
) -> tuple[ActionPlan, list[str]]:
    title = "Remove the old plugin's settings"
    undo = _settings_undo(PLUGIN_SETTINGS, manifests, settings.sha256 if settings else None)
    if settings is None:
        return (
            ActionPlan(
                PLUGIN_SETTINGS,
                title,
                unreadable or "Noctalia's settings weren't found, so there's nothing to remove.",
                blocked=unreadable,
                undo=undo,
            ),
            [],
        )
    table = _table(settings.document, _PLUGIN_TABLE)
    if not isinstance(table, Mapping) or not table:
        return ActionPlan(
            PLUGIN_SETTINGS, title, "There are no old plugin settings.", undo=undo
        ), []
    orphaned = sorted(key for key in table if key not in COMPANION_KEYS)
    kept = tuple(
        Kept(settings.path, f"{key}: the current companion plugin reads it")
        for key in sorted(table)
        if key in COMPANION_KEYS
    )
    if not orphaned:
        return (
            ActionPlan(
                PLUGIN_SETTINGS,
                title,
                "Every setting there belongs to the current companion plugin.",
                kept=kept,
                undo=undo,
            ),
            [],
        )
    blocked = _upgrade_gate() or _legacy_gate()
    lines: list[str] = []
    try:
        _text, lines = _remove_plugin_keys(settings.text, settings.document, orphaned)
    except TidyError as error:
        blocked = blocked or f"Noctalia's settings can't be edited safely: {error}."
    shown = {(_single_key(line) or ("", None))[0]: line.strip() for line in lines}
    changes = tuple(
        Change(
            settings.path,
            f'[plugin_settings."{PLUGIN_ID}"] remove ' + shown.get(key, f"{key} = {table[key]!r}"),
        )
        for key in orphaned
    )
    return (
        ActionPlan(
            PLUGIN_SETTINGS,
            title,
            f"Remove {_plural(len(orphaned), 'setting')} the retired Wall-in-One plugin left "
            "in Noctalia's settings.",
            changes=changes,
            kept=kept,
            notes=(
                _settings_backup_note(),
                "The current companion plugin uses the same id, so only settings it doesn't "
                "read are removed. Then Wall-in-One asks Noctalia to reload its settings.",
            ),
            blocked=blocked,
            undo=undo,
            token=_digest([settings.sha256, orphaned]),
        ),
        orphaned,
    )


def _apply_plugin_settings(expected: ActionPlan | None) -> Result:
    template.recover_interrupted_edit()
    manifests = _manifests()
    settings, unreadable = _read_noctalia()
    plan, orphaned = _plan_plugin_settings(settings, unreadable, manifests)
    if expected is not None and expected.token != plan.token:
        raise TidyChangedError(
            "Noctalia's settings changed since the preview; nothing was changed. "
            "Check the new preview."
        )
    if not plan.changes or settings is None:
        return Result(PLUGIN_SETTINGS, False, "There was nothing to remove.")
    if plan.blocked:
        raise TidyError(plan.blocked)
    _text, removed = _remove_plugin_keys(settings.text, settings.document, orphaned)
    archive, document = _apply_settings_edit(
        PLUGIN_SETTINGS,
        settings,
        lambda text: _remove_plugin_keys(text, tomllib.loads(text), orphaned)[0],
        {"removed_keys": orphaned, "removed_lines": removed},
    )
    unconfirmed = (
        " Noctalia didn't confirm that it reloaded them, so the running shell may still "
        "hold the old values; use Retry Reload once Noctalia is running."
        if document.get("reloaded") is not True
        else ""
    )
    return Result(
        PLUGIN_SETTINGS,
        True,
        f"Removed {_plural(len(orphaned), 'old plugin setting')}.{unconfirmed} Noctalia's "
        f"settings were backed up beside them and in {archive}." + _tail_note(document),
        archive,
    )


def _undo_plugin_settings(manifest: _Manifest) -> Result:
    document = copy.deepcopy(manifest.document)
    removed = [str(line) for line in document.get("removed_lines", [])]

    def reverse(settings: _NoctaliaSettings) -> str:
        return _restore_plugin_keys(settings.text, settings.document, removed)

    message = _restore_settings(manifest, reverse)
    document["state"] = "undone"
    document["undone"] = _now_iso()
    _write_manifest(manifest.directory, document)
    return Result(PLUGIN_SETTINGS, True, message, manifest.directory)


# -- action 4: the thumbnail cache -----------------------------------------------------


def _plan_thumbnails() -> ActionPlan:
    title = "Clear the thumbnail cache"
    try:
        usage = thumbnails.usage()
    except thumbnails.CacheDirectoryRefusedError as error:
        reason = f"{error}."
        return ActionPlan(
            THUMBNAIL_CACHE,
            title,
            "The thumbnail cache is left alone.",
            kept=(Kept(thumbnails.cache_directory(), reason),),
            blocked=reason,
        )
    cap = format_size(thumbnails.MAX_CACHE_BYTES)
    if usage.entries == 0 and usage.total_bytes == 0:
        return ActionPlan(THUMBNAIL_CACHE, title, "The thumbnail cache is empty.")
    return ActionPlan(
        THUMBNAIL_CACHE,
        title,
        f"{_plural(usage.entries, 'thumbnail')} use {format_size(usage.total_bytes)} "
        f"of the {cap} the cache may use.",
        changes=(
            Change(
                thumbnails.cache_directory(),
                f"delete {_plural(usage.entries, 'cached thumbnail')}",
                usage.total_bytes,
            ),
        ),
        notes=(
            "This is only a cache: thumbnails are made again as you browse. That's why "
            "nothing is archived and there's no Undo.",
        ),
        token=_digest([usage.entries, usage.total_bytes]),
    )


# -- the public surface ----------------------------------------------------------------


def _roots(roots: Sequence[Path] | None) -> tuple[Path, ...]:
    return tuple(roots) if roots is not None else config.load().roots


def plan(*, roots: Sequence[Path] | None = None, now: float | None = None) -> Plan:
    """The exact preview of every action. Reads only; never creates or locks a file.

    ``roots`` are the library roots (default: settings.toml's), whose managed
    folders are searched for leftovers.
    """
    manifests = _manifests()
    settings, unreadable = _read_noctalia()
    leftovers, _items = _plan_leftovers(
        _roots(roots), time.time() if now is None else now, manifests
    )
    old_template, _found = _plan_old_palette_template(settings, manifests)
    plugin, _keys = _plan_plugin_settings(settings, unreadable, manifests)
    current = settings.sha256 if settings is not None else None
    if not plugin.changes and _reload_pending(PLUGIN_SETTINGS, manifests, current):
        plugin = replace(plugin, notes=(*plugin.notes, _RELOAD_PENDING_NOTE), retry="Retry Reload")
    return Plan(
        (
            leftovers,
            _plan_palette_template(settings, unreadable, manifests),
            old_template,
            plugin,
            _plan_thumbnails(),
        )
    )


def apply(
    action: Action, expected: ActionPlan | None = None, *, roots: Sequence[Path] | None = None
) -> Result:
    """Run one action. With ``expected``, refuse unless it still matches the preview."""
    if expected is not None and expected.action != action:
        raise ValueError("the preview belongs to a different action")
    if action == THUMBNAIL_CACHE:
        try:
            removed = thumbnails.clear()
        except thumbnails.CacheDirectoryRefusedError as error:
            raise TidyError(f"Nothing was deleted: {error}.") from error
        return Result(
            THUMBNAIL_CACHE,
            removed > 0,
            f"Cleared {_plural(removed, 'thumbnail')}. They're made again as you browse.",
        )
    with _exclusive():
        if action == LEFTOVERS:
            return _apply_leftovers(expected, _roots(roots))
        if action == PALETTE_TEMPLATE:
            return _apply_palette_template(expected)
        if action == OLD_PALETTE_TEMPLATE:
            return _apply_old_palette_template(expected)
        if action == PLUGIN_SETTINGS:
            return _apply_plugin_settings(expected)
    raise ValueError(f"unknown tidy-up action {action!r}")


def retry(action: Action) -> Result:
    """Ask Noctalia again to reload its settings after an edit it never confirmed.

    Only a confirmed reload is recorded; until then the template switch counts
    as unfinished and the old template file stays.
    """
    if action not in (PALETTE_TEMPLATE, PLUGIN_SETTINGS):
        raise TidyError("There's nothing to retry for this action.")
    with _exclusive():
        settings, _unreadable = _read_noctalia()
        current = settings.sha256 if settings is not None else None
        edit = _current_edit(action, _manifests(), current)
        if edit is None or edit.document.get("reloaded") is True:
            return Result(action, False, "There's no reload left to retry.")
        if not _reload_noctalia():
            return Result(
                action,
                False,
                "Noctalia still didn't confirm a reload. Make sure it's running, then try again.",
                edit.directory,
            )
        document = copy.deepcopy(edit.document)
        document["reloaded"] = True
        document["reloaded_at_ns"] = time.time_ns()
        document["palette_after_reload"] = _palette_fingerprint()
        _write_manifest(edit.directory, document)
        return Result(action, True, "Noctalia reloaded its settings.", edit.directory)


def undo(action: Action) -> Result:
    """Put back what the latest :func:`apply` of ``action`` changed."""
    if action == THUMBNAIL_CACHE:
        raise TidyError("The thumbnail cache has no Undo: thumbnails are made again as you browse.")
    with _exclusive():
        manifests = _manifests()
        if action in (PALETTE_TEMPLATE, PLUGIN_SETTINGS):
            # Finish whatever an earlier edit left half done before reading.
            template.recover_interrupted_edit()
            settings, _unreadable = _read_noctalia()
            current = settings.sha256 if settings is not None else None
            for manifest in manifests:
                if manifest.action != action:
                    continue
                state = _effective_state(manifest, current)
                if state in ("applied", "applying"):
                    if action == PALETTE_TEMPLATE:
                        return _undo_palette_template(manifest)
                    return _undo_plugin_settings(manifest)
                if state == "undone":
                    break
            return Result(action, False, "There's nothing to undo.")
        latest = _latest(action, manifests)
        if latest is None:
            return Result(action, False, "There's nothing to undo.")
        if action in (LEFTOVERS, OLD_PALETTE_TEMPLATE):
            result = _undo_leftovers(latest)
            return Result(action, result.changed, result.message, result.archive)
    raise ValueError(f"unknown tidy-up action {action!r}")
