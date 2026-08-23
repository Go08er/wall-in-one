"""What a wallpaper actually *is*: a still, an optional moving source, a palette.

Until now a wallpaper was a file, and `MediaItem.paired_still` was a field only
videos ever set -- so a still had no pairing, a video's pairing could not be
chosen, and no wallpaper could remember which palette it wanted. That shape
cannot express what the app is for.

Here every library item resolves to a bundle instead, and it does so without
anyone creating one. A still pairs with itself. A video pairs with the still
that represents it while dynamics are off. A Workshop scene will pair with a
screenshot when there are Workshop scenes. The common case therefore costs
nothing, and only the items somebody wants to change are ever written down --
which is also why the file stays small enough to hand-edit.

Two things follow from that and are worth reading before changing any of it.

*Only customizations are stored.* A record exists because a person made a
choice, so a default that improves later reaches every item that never had one
chosen for it, and reaches none of the items where it would overrule somebody.
`customized` is not decoration: it is the difference between "this is what we
worked out" and "this is what you asked for".

*Identity is not the path.* A record is keyed by medium and source, so that a
Workshop scene can be identified by its id rather than by wherever Steam
happens to have unpacked it. For file-backed media the source is still the
path -- but the key is a `medium:source` string either way, so adding a third
medium does not change the file format or the lookups.

`library.pairing`, singular, is the older and narrower half: the conventions by
which a *default* still is found next to a video. It is still how the default
in here gets synthesized. This module owns the record, the choice, and the file.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Final, TypeVar

from wall_in_one import paths
from wall_in_one.library import pairing, state_file, stills
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.theme import noctalia

#: The file, under `paths.app_state_dir()`, beside the favourites.
STATE_FILENAME: Final = "pairings.json"

#: Bumped only if the shape below changes. A newer marker is recovered for the
#: interactive UI but reported as a fault, so an older build cannot compile or
#: rewrite a document whose extra meaning it does not understand.
FORMAT_VERSION: Final = 2
LEGACY_FORMAT_VERSIONS: Final = frozenset((1,))

#: A ceiling, so a file that grew a zero cannot be read forever.
MAX_PAIRINGS: Final = 20_000

#: The same idea for the file itself, before it is parsed.
MAX_STATE_BYTES: Final = 8 * 1024 * 1024

#: Where a file we could not parse is moved before it would be overwritten.
BROKEN_SUFFIX: Final = ".broken"

#: What a pairing asks for when nobody has said otherwise: whatever Noctalia
#: generates from the wallpaper itself, which is what has always happened.
ADAPTIVE: Final = "adaptive"

#: Leave the palette exactly as it is. The one policy that is not a palette:
#: it exists because "this wallpaper should not disturb my colours" is a real
#: thing to want, and is not expressible as a choice among palettes.
KEEP: Final = "keep"

HEALTHY: Final = "healthy"
BORKED: Final = "borked"
MAX_HEALTH_REASON: Final = 512
MAX_HEALTH_SOURCE: Final = 64

# The GUI and the headless runtime-health bridge are separate app processes,
# but pairings.json still has exactly one logical writer. Atomic replacement
# prevents torn JSON; this companion advisory lock prevents two valid, stale
# snapshots from replacing each other. It is deliberately never unlinked:
# unlinking a lock file while another process is waiting on its inode permits a
# third process to lock a new inode and enter the critical section beside it.
MUTATION_LOCK_TIMEOUT_SECONDS: Final = 5.0
MUTATION_LOCK_POLL_SECONDS: Final = 0.025

_LOCAL_MUTATION_GATE = threading.Lock()
_MutationResult = TypeVar("_MutationResult")


class PairingError(Exception):
    """A pairing could not be written, with a machine-readable reason.

    Kinds in use: ``local-io``, ``invalid-state``.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    def __str__(self) -> str:
        return f"{self.kind}: {super().__str__()}"


class Medium(Enum):
    """What kind of thing the pairing is *of*.

    Not `library.model.Kind`, which says how to show a wallpaper. They agree
    for files and part company for scenes, whose source is a Workshop id
    rather than a path -- which is the whole reason a record is keyed
    `medium:source` and not by a filename.
    """

    STILL = "still"
    VIDEO = "video"
    SCENE = "scene"

    @classmethod
    def of(cls, kind: Kind) -> Medium:
        if kind is Kind.VIDEO:
            return cls.VIDEO
        if kind is Kind.SCENE:
            return cls.SCENE
        return cls.STILL


@dataclass(frozen=True, slots=True)
class Identity:
    """What a record is keyed by: a medium and a source within it."""

    medium: Medium
    source: str

    @classmethod
    def of(cls, item: MediaItem) -> Identity:
        """A scene is keyed by its Workshop id, not by where Steam put it.

        Which is the point of `medium:source`: moving an installed directory
        does not change the pairing.  A *confirmed uninstall* is a different
        lifecycle event and :class:`Session` explicitly forgets this record,
        so a later reinstall of the same id starts clean.
        """
        source = item.scene if item.kind is Kind.SCENE and item.scene else str(item.path)
        return cls(medium=Medium.of(item.kind), source=source)

    @property
    def key(self) -> str:
        return f"{self.medium.value}:{self.source}"

    @classmethod
    def parse(cls, key: str) -> Identity | None:
        """The inverse, or ``None`` for anything unrecognised.

        Split once, because a source is a path and paths contain colons.
        """
        medium, separator, source = key.partition(":")
        if not separator or not source:
            return None
        try:
            return cls(medium=Medium(medium), source=source)
        except ValueError:
            # A medium this build does not know -- a scene, read by a version
            # that predates step 15. Dropped rather than guessed at, and the
            # record it names is left alone in the file.
            return None


class Mode(Enum):
    """The dark/light half of a palette policy.

    `KEEP` rather than a `None`, because "do not touch the mode" is a choice
    somebody makes and not the absence of one -- and because a record that
    says nothing about mode has to mean the same thing.
    """

    KEEP = "keep"
    DARK = "dark"
    LIGHT = "light"
    AUTO = "auto"


@dataclass(frozen=True, slots=True)
class Health:
    """App-owned compatibility judgement for one media identity.

    Rust reports session failures. The app is the sole writer that may turn
    one into durable authoring state, so this marker lives beside the pairing
    rather than in a daemon-owned blacklist.
    """

    state: str = HEALTHY
    reason: str = ""
    source: str = ""

    @property
    def is_borked(self) -> bool:
        return self.state == BORKED

    @classmethod
    def borked(cls, reason: str, source: str) -> Health:
        clean = _health_text(reason, MAX_HEALTH_REASON)
        origin = _health_text(source, MAX_HEALTH_SOURCE)
        return cls(
            BORKED,
            clean or "Runtime could not apply this wallpaper",
            origin or "runtime",
        )


def _health_text(value: str, maximum_bytes: int) -> str:
    """Return one single-line UTF-8 value which fits the runtime wire bound."""
    clean = " ".join(value.split())
    encoded = clean.encode("utf-8")[:maximum_bytes]
    while True:
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError as error:
            encoded = encoded[: error.start]


@dataclass(frozen=True, slots=True)
class PalettePolicy:
    """Which colours a wallpaper asks Noctalia for, and in which mode.

    `kind` is `ADAPTIVE`, `KEEP`, or one of Noctalia's own palette sources --
    `builtin`, `community`, `custom`. For adaptive, `name` optionally pins one
    generator; for a named source it names the palette. The wire form is
    `kind` or `kind:name`, which keeps a record readable and lets a source
    Noctalia adds later survive a round trip through an older build.
    """

    kind: str = ADAPTIVE
    name: str = ""
    mode: Mode = Mode.KEEP

    @property
    def is_adaptive(self) -> bool:
        return self.kind == ADAPTIVE

    @property
    def keeps_palette(self) -> bool:
        return self.kind == KEEP

    def adaptive_scheme(self, fallback: str) -> str:
        """Return this pairing's generator, or the application default.

        Older records encoded adaptive as the bare word ``adaptive``. That
        continues to follow the application-wide default, while a newly
        chosen ``adaptive:<scheme>`` is an explicit per-item decision.
        """
        if self.is_adaptive and self.name in noctalia.ALL_SCHEMES:
            return self.name
        return fallback

    def encode(self) -> str:
        return f"{self.kind}:{self.name}" if self.name else self.kind

    @classmethod
    def decode(cls, raw: object, mode: object = None) -> PalettePolicy:
        """Read a stored policy, defaulting anything unusable to adaptive."""
        kind, name = ADAPTIVE, ""
        if isinstance(raw, str) and raw.strip():
            head, _, tail = raw.partition(":")
            if head.strip():
                kind, name = head.strip(), tail.strip()
        chosen = Mode.KEEP
        if isinstance(mode, str):
            with contextlib.suppress(ValueError):
                chosen = Mode(mode)
        return cls(kind=kind, name=name, mode=chosen)

    def selection(self, generator: str) -> noctalia.ColourSchemeSelection | None:
        """What to hand `color-scheme-set`, or ``None`` to leave it alone.

        An adaptive policy is Noctalia's own `wallpaper` source named by the
        generator the user picked in settings, so "adaptive" and "generated
        from this wallpaper with m3-tonal-spot" are the same request.
        """
        if self.keeps_palette:
            return None
        if self.is_adaptive:
            return noctalia.ColourSchemeSelection(
                source="wallpaper", name=self.adaptive_scheme(generator)
            )
        if self.kind not in ("builtin", "community", "custom") or not self.name:
            return None
        return noctalia.ColourSchemeSelection(
            source=self.kind,  # type: ignore[arg-type]
            name=self.name,
        )


@dataclass(frozen=True, slots=True)
class Pairing:
    """One library item as the app actually uses it."""

    identity: Identity
    #: What Noctalia is given as the wallpaper. `None` only for a video whose
    #: still could not be found or made.
    still: Path | None = None
    #: The moving source rendered above the still, or `None` for a plain still.
    motion: Path | None = None
    palette: PalettePolicy = PalettePolicy()
    #: True when a person chose some part of this rather than inheriting it.
    customized: bool = False
    #: True when a chosen still is not on disk right now, so `still` fell back
    #: to the default. The choice is kept -- an unmounted drive is not a
    #: retraction -- and this is how a caller can say so out loud.
    override_missing: bool = False
    health: Health = Health()

    @property
    def is_moving(self) -> bool:
        return self.motion is not None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"identity": self.identity.key}
        if self.customized:
            payload["palette"] = self.palette.encode()
            if self.palette.mode is not Mode.KEEP:
                payload["mode"] = self.palette.mode.value
            if self.still is not None:
                payload["still"] = str(self.still)
        if self.health.is_borked:
            payload["health"] = {
                "state": BORKED,
                "reason": self.health.reason,
                "source": self.health.source,
            }
        return payload


def _record(raw: object) -> Pairing | None:
    """One stored record, or ``None`` if it is not usable.

    Each is read on its own: one bad entry in a hand-edited file should cost
    that entry, not everybody's choices.
    """
    if not isinstance(raw, dict):
        return None
    identity = (
        Identity.parse(raw.get("identity", "")) if isinstance(raw.get("identity"), str) else None
    )
    if identity is None:
        return None
    still = raw.get("still")
    chosen = Path(still) if isinstance(still, str) and still.strip() else None
    if chosen is not None and not chosen.is_absolute():
        # Nothing to be relative to: the process reading this may run anywhere.
        chosen = None
    raw_health = raw.get("health")
    health = Health()
    if isinstance(raw_health, dict) and raw_health.get("state") == BORKED:
        reason = raw_health.get("reason")
        source = raw_health.get("source")
        if isinstance(reason, str) and isinstance(source, str):
            health = Health.borked(reason, source)
    customized = any(key in raw for key in ("still", "palette", "mode"))
    return Pairing(
        identity=identity,
        still=chosen,
        palette=PalettePolicy.decode(raw.get("palette"), raw.get("mode")),
        customized=customized,
        health=health,
    )


def state_path() -> Path:
    return paths.app_state_dir() / STATE_FILENAME


def _mutation_lock_path(target: Path) -> Path:
    return target.absolute().with_name(f".{target.name}.mutation.lock")


def _open_mutation_lock(path: Path) -> int:
    """Open the private lock without following or racing a replacement."""
    try:
        paths.ensure_directory(path.parent)
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as error:
        raise PairingError(
            "local-io", f"cannot safely open pairings mutation lock {path}: {error}"
        ) from error

    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if not stat.S_ISREG(opened.st_mode):
            raise PairingError("local-io", f"pairings mutation lock {path} is not a regular file")
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise PairingError(
                "local-io", f"pairings mutation lock {path} changed while being opened"
            )
        if opened.st_uid != os.getuid() or opened.st_nlink != 1:
            raise PairingError(
                "local-io",
                f"pairings mutation lock {path} is not a private file owned by this user",
            )
        os.fchmod(descriptor, 0o600)
        return descriptor
    except PairingError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise PairingError(
            "local-io", f"cannot verify pairings mutation lock {path}: {error}"
        ) from error


def _verify_open_mutation_lock(path: Path, descriptor: int) -> None:
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
    except OSError as error:
        raise PairingError(
            "local-io", f"cannot verify pairings mutation lock {path}: {error}"
        ) from error
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise PairingError(
            "local-io", f"pairings mutation lock {path} changed while waiting for it"
        )


@contextlib.contextmanager
def _mutation_lock(
    target: Path, *, timeout: float = MUTATION_LOCK_TIMEOUT_SECONDS
) -> Iterator[None]:
    """Serialise one read/rebase/write transaction across threads and processes."""
    if timeout < 0:
        raise PairingError("local-io", "pairings mutation lock timeout cannot be negative")
    lock_path = _mutation_lock_path(target)
    deadline = time.monotonic() + timeout
    remaining = max(0.0, deadline - time.monotonic())
    if not _LOCAL_MUTATION_GATE.acquire(timeout=remaining):
        raise PairingError(
            "local-io",
            f"timed out after {timeout:g}s waiting for pairings mutation lock {lock_path}",
        )
    descriptor: int | None = None
    locked = False
    try:
        descriptor = _open_mutation_lock(lock_path)
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PairingError(
                        "local-io",
                        f"timed out after {timeout:g}s waiting for pairings mutation lock "
                        f"{lock_path}",
                    ) from None
                time.sleep(min(MUTATION_LOCK_POLL_SECONDS, remaining))
            except OSError as error:
                raise PairingError(
                    "local-io", f"cannot acquire pairings mutation lock {lock_path}: {error}"
                ) from error
        _verify_open_mutation_lock(lock_path, descriptor)
        yield
    finally:
        if descriptor is not None:
            try:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        _LOCAL_MUTATION_GATE.release()


def _read(path: Path) -> tuple[dict[str, Pairing], str | None]:
    """Every stored record by key, plus why the file was passed over.

    Never raises. A wallpaper manager that will not start over its own
    customization file is worse than one that starts with the defaults.
    """
    payload, fault = state_file.read_object(
        path, maximum_bytes=MAX_STATE_BYTES, description="pairings"
    )
    if payload is None:
        return {}, fault
    version = payload.get("version")
    version_fault = (
        None
        if version is None
        or (
            type(version) is int
            and (version == FORMAT_VERSION or version in LEGACY_FORMAT_VERSIONS)
        )
        else state_file.version_fault(path, payload, FORMAT_VERSION)
    )
    faults = [found for found in (version_fault,) if found]
    stored = payload.get("pairings")
    if not isinstance(stored, list):
        return {}, f"{path.name} has no pairings in it"

    if len(stored) > MAX_PAIRINGS:
        faults.append(f"{path.name} has more than {MAX_PAIRINGS} pairing records")

    found: dict[str, Pairing] = {}
    malformed = 0
    duplicate = 0
    for raw in stored[:MAX_PAIRINGS]:
        record = _record(raw)
        if record is None:
            malformed += 1
            continue
        assert isinstance(raw, dict)
        still = raw.get("still")
        mode = raw.get("mode")
        palette = raw.get("palette")
        health = raw.get("health")
        if "still" in raw and (
            not isinstance(still, str) or not still.strip() or not Path(still).is_absolute()
        ):
            malformed += 1
        if "mode" in raw and (
            not isinstance(mode, str) or mode not in {item.value for item in Mode}
        ):
            malformed += 1
        if "palette" in raw and (not isinstance(palette, str) or not palette.strip()):
            malformed += 1
        if "health" in raw and (
            not isinstance(health, dict)
            or health.get("state") != BORKED
            or not isinstance(health.get("reason"), str)
            or not isinstance(health.get("source"), str)
        ):
            malformed += 1
        if record.identity.key in found:
            duplicate += 1
        found[record.identity.key] = record
    if malformed:
        faults.append(f"{path.name} has {malformed} malformed pairing record fields")
    if duplicate:
        faults.append(f"{path.name} has {duplicate} duplicate pairing identities")
    return found, state_file.joined_faults(faults)


def load(path: Path | None = None) -> dict[str, Pairing]:
    """The stored customizations, or none at all. Never raises."""
    records, _fault = _read(path if path is not None else state_path())
    return records


def save(records: Mapping[str, Pairing], path: Path | None = None) -> Path:
    """Write the customizations atomically, and return where they went.

    An exclusively-created temporary in the same directory is atomically
    replaced into place. A half-written file would read
    back as somebody's choices having partly evaporated, which looks like the
    app forgetting rather than like a file that needs attention.
    """
    target = path if path is not None else state_path()
    try:
        paths.ensure_directory(target.parent)
    except OSError as error:
        raise PairingError(
            "local-io", f"could not create {target.parent}: {error.strerror or error}"
        ) from error

    payload = {
        "version": FORMAT_VERSION,
        "pairings": [records[key].to_json() for key in sorted(records)],
    }
    try:
        state_file.write_atomic_text(
            target, json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        )
    except OSError as error:
        raise PairingError(
            "local-io", f"could not write {target}: {error.strerror or error}"
        ) from error
    return target


def synthesize(item: MediaItem, roots: Sequence[Path] = ()) -> Pairing:
    """The bundle an item has when nobody has chosen anything for it.

    A still is its own representative. A video's default comes from
    `library.pairing`'s three conventions -- our sidecar, the managed
    `Automatic Stills` directory, or a sibling named by the user's own habit.
    """
    identity = Identity.of(item)
    if not item.is_moving:
        return Pairing(identity=identity, still=item.path, motion=None)
    if item.kind is Kind.SCENE:
        return Pairing(
            identity=identity,
            still=pairing.scene_still(item.scene, roots=roots),
            motion=item.path,
        )
    return Pairing(
        identity=identity,
        still=pairing.find_still(item.path, roots=roots),
        motion=item.path,
    )


def resolve(
    item: MediaItem,
    roots: Sequence[Path] = (),
    records: Mapping[str, Pairing] | None = None,
) -> Pairing:
    """The bundle to actually use for ``item``.

    A chosen still that is not on disk right now falls back to the default
    rather than leaving the item unshowable, and says so through
    `override_missing`. The choice itself is untouched: a drive that is not
    mounted this morning is not somebody changing their mind.
    """
    default = synthesize(item, roots)
    saved = (records or {}).get(default.identity.key)
    if saved is None:
        return default

    chosen = saved.still
    missing = chosen is not None and not chosen.is_file()
    return replace(
        default,
        still=default.still if chosen is None or missing else chosen,
        palette=saved.palette,
        customized=saved.customized,
        override_missing=missing,
        health=saved.health,
    )


def apply(
    items: Iterable[MediaItem],
    roots: Sequence[Path] = (),
    records: Mapping[str, Pairing] | None = None,
) -> tuple[MediaItem, ...]:
    """Attach representative stills and hide only app-generated children.

    A manually supplied still is a first-class library item even when one or
    more moving wallpapers select it.  The only images absorbed by a moving
    item are captures at that item's exact deterministic path in an app-owned
    ``Automatic Stills`` directory.  This is the same ownership boundary used
    when deletion derives which capture goes away with the moving item.

    This replaces `library.pairing.apply`, which could only ever compute the
    default. The difference is the ``records`` argument.
    """
    materialised = list(items)
    resolved = {item.path: resolve(item, roots, records) for item in materialised}

    generated = {
        destination
        for item in materialised
        if item.is_moving
        for root in roots
        if (destination := stills.automatic_destination(item, root)) is not None
    }
    kept: list[MediaItem] = []
    for item in materialised:
        bundle = resolved[item.path]
        if bundle.is_moving:
            kept.append(item.with_still(bundle.still))
        elif item.path not in generated:
            kept.append(item)
    return tuple(kept)


class Store:
    """The customizations as the running app holds them: a map and a file.

    Every change is written through immediately, for the reason the favourites
    are: a session that ends any way other than the close button would
    otherwise lose the lot.
    """

    def __init__(
        self, records: Mapping[str, Pairing] | None = None, path: Path | None = None
    ) -> None:
        self._records: dict[str, Pairing] = dict(records or {})
        self._path = path
        self._fault: str | None = None

    @classmethod
    def open(cls, path: Path | None = None) -> Store:
        """Read the file. Never raises; a broken one degrades to no records."""
        target = path if path is not None else state_path()
        records, fault = _read(target)
        store = cls(records, target)
        store._fault = fault
        return store

    @property
    def records(self) -> Mapping[str, Pairing]:
        return self._records

    @property
    def fault(self) -> str | None:
        """Why the file was passed over, for a toast to say. `None` when fine."""
        return self._fault

    def __len__(self) -> int:
        return len(self._records)

    def get(self, identity: Identity) -> Pairing | None:
        return self._records.get(identity.key)

    def is_customized(self, identity: Identity) -> bool:
        record = self._records.get(identity.key)
        return record is not None and record.customized

    def health(self, identity: Identity) -> Health:
        record = self._records.get(identity.key)
        return record.health if record is not None else Health()

    def resolve(self, item: MediaItem, roots: Sequence[Path] = ()) -> Pairing:
        return resolve(item, roots, self._records)

    def apply(
        self, items: Iterable[MediaItem], roots: Sequence[Path] = ()
    ) -> tuple[MediaItem, ...]:
        return apply(items, roots, self._records)

    def choose_still(self, item: MediaItem, still: Path | None) -> Pairing:
        """Record a chosen representative for ``item``, or clear the choice.

        ``None`` clears the still but keeps any other customization, which is
        what "go back to working it out yourself" means for one field.
        """
        identity = Identity.of(item)

        def choose(records: dict[str, Pairing]) -> tuple[Pairing, bool]:
            existing = records.get(identity.key)
            palette = existing.palette if existing is not None else PalettePolicy()
            health = existing.health if existing is not None else Health()
            record = Pairing(
                identity=identity,
                still=still,
                palette=palette,
                customized=True,
                health=health,
            )
            records[identity.key] = record
            return record, record != existing

        return self._mutate(choose)

    def choose_palette(self, item: MediaItem, palette: PalettePolicy) -> Pairing:
        """Record which colours ``item`` asks for."""
        identity = Identity.of(item)

        def choose(records: dict[str, Pairing]) -> tuple[Pairing, bool]:
            existing = records.get(identity.key)
            still = existing.still if existing is not None else None
            health = existing.health if existing is not None else Health()
            record = Pairing(
                identity=identity,
                still=still,
                palette=palette,
                customized=True,
                health=health,
            )
            records[identity.key] = record
            return record, record != existing

        return self._mutate(choose)

    def mark_borked(self, item: MediaItem, reason: str, source: str) -> bool:
        """Persist one runtime finding without changing pairing choices.

        All playlist occurrences resolve through the media identity, so one
        write marks the wallpaper rather than only the entry which happened to
        expose it.
        """
        return bool(self.mark_borked_many(((item, reason, source),)))

    def mark_borked_many(
        self,
        findings: Iterable[tuple[MediaItem, str, str]],
    ) -> int:
        """Persist one atomic runtime-health snapshot and return changed identities.

        The app can receive several renderer findings in one status response.
        Saving each separately needlessly creates intermediate documents and
        gives another process more chances to race the transaction. Duplicate
        identities are collapsed with the last reported finding winning.

        Unlike an interactive customization, health synchronization refuses
        an unreadable store. An unattended diagnostic must never turn a
        faulted authoring file into a sparse replacement.
        """
        requested: dict[str, tuple[Identity, Health]] = {}
        for item, reason, source in findings:
            identity = Identity.of(item)
            requested[identity.key] = (identity, Health.borked(reason, source))
        if not requested:
            return 0

        def mark(records: dict[str, Pairing]) -> tuple[int, bool]:
            changed = 0
            for key, (identity, health) in requested.items():
                existing = records.get(key)
                if existing is not None and existing.health == health:
                    continue
                records[key] = (
                    replace(existing, health=health)
                    if existing is not None
                    else Pairing(identity=identity, health=health)
                )
                changed += 1
            return changed, changed > 0

        return self._mutate(mark, refuse_fault=True)

    def clear_borked(self, item: MediaItem) -> bool:
        """Clear the durable judgement while retaining authored choices."""
        identity = Identity.of(item)

        def clear(records: dict[str, Pairing]) -> tuple[bool, bool]:
            existing = records.get(identity.key)
            if existing is None or not existing.health.is_borked:
                return False, False
            if existing.customized:
                records[identity.key] = replace(existing, health=Health())
            else:
                del records[identity.key]
            return True, True

        return self._mutate(clear)

    def reset(self, item: MediaItem) -> bool:
        """Forget everything chosen for ``item``. True if there was anything."""
        identity = Identity.of(item)

        def reset_choices(records: dict[str, Pairing]) -> tuple[bool, bool]:
            existing = records.get(identity.key)
            if existing is None:
                return False, False
            if existing.health.is_borked:
                records[identity.key] = Pairing(identity=identity, health=existing.health)
            else:
                del records[identity.key]
            return True, True

        return self._mutate(reset_choices)

    def forget_identity(self, identity: Identity) -> bool:
        """Drop a record by identity, for a wallpaper the app has just deleted.

        Records outlive a missing file on purpose -- it may come back -- but
        not one we destroyed ourselves.
        """

        def forget(records: dict[str, Pairing]) -> tuple[bool, bool]:
            if identity.key not in records:
                return False, False
            del records[identity.key]
            return True, True

        return self._mutate(forget)

    def forget_item(
        self,
        item: MediaItem,
        *,
        removed_stills: Iterable[Path] = (),
    ) -> bool:
        """Forget one explicitly deleted item and every deleted still link.

        This is intentionally stronger than noticing a missing file.  An
        unmounted drive must retain its choices; a delete/trash gesture is an
        authoritative lifecycle boundary.  The item's complete record goes,
        while another moving wallpaper which chose the deleted image merely
        returns its still field to automatic/default.  Its palette and health
        remain exactly as authored.

        ``removed_stills`` names proven app-generated captures deleted beside
        a moving item.  Scrubbing those in the same rebased transaction keeps
        another pairing from retaining a pointer to an automatic still which
        no longer exists.
        """
        identity = Identity.of(item)
        deleted_stills = {item.path, *(Path(path) for path in removed_stills)}

        def forget(records: dict[str, Pairing]) -> tuple[bool, bool]:
            changed = records.pop(identity.key, None) is not None
            # A still is a first-class media item, so deleting it drops its own
            # record too even when the caller's scanned kind was ambiguous.
            for path in deleted_stills:
                still_key = Identity(Medium.STILL, str(path)).key
                if still_key != identity.key and records.pop(still_key, None) is not None:
                    changed = True
            for key, existing in tuple(records.items()):
                if existing.still not in deleted_stills:
                    continue
                records[key] = replace(existing, still=None)
                changed = True
            return changed, changed

        return self._mutate(forget)

    def forget_path(self, path: Path) -> bool:
        """Drop any record naming ``path`` as its source, whatever the medium.

        A file is only ever one medium, so at most one of these matches; the
        caller has a path and no reason to know which. Callers that have the
        `MediaItem` should use `forget_identity`.
        """
        keys = tuple(Identity(medium, str(path)).key for medium in Medium)

        def forget(records: dict[str, Pairing]) -> tuple[bool, bool]:
            present = [key for key in keys if key in records]
            changed = bool(present)
            for key in present:
                del records[key]
            for key, existing in tuple(records.items()):
                if existing.still != path:
                    continue
                records[key] = replace(existing, still=None)
                changed = True
            return changed, changed

        return self._mutate(forget)

    def _mutate(
        self,
        operation: Callable[[dict[str, Pairing]], tuple[_MutationResult, bool]],
        *,
        refuse_fault: bool = False,
    ) -> _MutationResult:
        """Reload, rebase and optionally replace one authoring transaction."""
        target = self._path if self._path is not None else state_path()
        with _mutation_lock(target):
            records, fault = _read(target)
            if fault is not None and refuse_fault:
                raise PairingError(
                    "invalid-state",
                    f"cannot persist runtime health because {fault}; "
                    "the unreadable pairings file was left untouched",
                )
            updated = dict(records)
            result, changed = operation(updated)
            if changed:
                previous_fault = self._fault
                self._fault = fault
                try:
                    self._write(updated)
                except PairingError:
                    self._fault = previous_fault if fault is None else fault
                    raise
                fault = None
            self._records = updated
            self._fault = fault
            return result

    def _write(self, records: Mapping[str, Pairing]) -> None:
        target = self._path if self._path is not None else state_path()
        if self._fault is not None:
            # Do not overwrite bytes we could not understand: they are
            # somebody's choices, in some form, and a copy costs nothing.
            try:
                state_file.preserve_faulted(target)
            except OSError as error:
                raise PairingError(
                    "local-io",
                    f"could not preserve unreadable {target}: {error.strerror or error}",
                ) from error
            self._fault = None
        save(records, target)
