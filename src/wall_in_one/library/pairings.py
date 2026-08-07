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
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Final

from wall_in_one import paths
from wall_in_one.library import pairing
from wall_in_one.library.model import Kind, MediaItem

#: The file, under `paths.app_state_dir()`, beside the favourites.
STATE_FILENAME: Final = "pairings.json"

#: Bumped only if the shape below changes. Read leniently: an unrecognised
#: version is still a list of records, and refusing it would throw away
#: somebody's choices over a number.
FORMAT_VERSION: Final = 1

#: A ceiling, so a file that grew a zero cannot be read forever.
MAX_PAIRINGS: Final = 20_000

#: The same idea for the file itself, before it is parsed.
MAX_STATE_BYTES: Final = 8 * 1024 * 1024

#: Where a file we could not parse is moved before it would be overwritten.
BROKEN_SUFFIX: Final = ".broken"

#: The palette a pairing asks for when nobody has said otherwise: whatever
#: Noctalia generates from the wallpaper itself, which is what happens today.
#: Step 10 gives this field structure -- mode, and a choice among builtin,
#: generated, community, custom and keep-current. It is a string now so that
#: adding that structure does not have to change the file format twice.
ADAPTIVE: Final = "adaptive"


class PairingError(Exception):
    """A pairing could not be written, with a machine-readable reason.

    Kinds in use: ``local-io``.
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    def __str__(self) -> str:
        return f"{self.kind}: {super().__str__()}"


class Medium(Enum):
    """What kind of thing the pairing is *of*.

    Not `library.model.Kind`, which says how to play a file. A Workshop scene
    is neither a still nor a video on disk, so the two will part company at
    step 15 and are kept separate now to save a migration then.
    """

    STILL = "still"
    VIDEO = "video"

    @classmethod
    def of(cls, kind: Kind) -> Medium:
        return cls.VIDEO if kind is Kind.VIDEO else cls.STILL


@dataclass(frozen=True, slots=True)
class Identity:
    """What a record is keyed by: a medium and a source within it."""

    medium: Medium
    source: str

    @classmethod
    def of(cls, item: MediaItem) -> Identity:
        return cls(medium=Medium.of(item.kind), source=str(item.path))

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


@dataclass(frozen=True, slots=True)
class Pairing:
    """One library item as the app actually uses it."""

    identity: Identity
    #: What Noctalia is given as the wallpaper. `None` only for a video whose
    #: still could not be found or made.
    still: Path | None = None
    #: The moving source rendered above the still, or `None` for a plain still.
    motion: Path | None = None
    palette: str = ADAPTIVE
    #: True when a person chose some part of this rather than inheriting it.
    customized: bool = False
    #: True when a chosen still is not on disk right now, so `still` fell back
    #: to the default. The choice is kept -- an unmounted drive is not a
    #: retraction -- and this is how a caller can say so out loud.
    override_missing: bool = False

    @property
    def is_moving(self) -> bool:
        return self.motion is not None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"identity": self.identity.key, "palette": self.palette}
        if self.still is not None:
            payload["still"] = str(self.still)
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
    palette = raw.get("palette")
    return Pairing(
        identity=identity,
        still=chosen,
        palette=palette if isinstance(palette, str) and palette else ADAPTIVE,
        customized=True,
    )


def state_path() -> Path:
    return paths.app_state_dir() / STATE_FILENAME


def _read(path: Path) -> tuple[dict[str, Pairing], str | None]:
    """Every stored record by key, plus why the file was passed over.

    Never raises. A wallpaper manager that will not start over its own
    customization file is worse than one that starts with the defaults.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return {}, None
        if path.stat().st_size > MAX_STATE_BYTES:
            return {}, f"{path.name} is too large to be a list of pairings"
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        return {}, f"could not read {path.name}: {error.strerror or error}"

    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return {}, f"{path.name} is not readable, so no customizations were loaded"
    if not isinstance(payload, dict):
        return {}, f"{path.name} is not a pairings file"
    stored = payload.get("pairings")
    if not isinstance(stored, list):
        return {}, f"{path.name} has no pairings in it"

    found: dict[str, Pairing] = {}
    for raw in stored[:MAX_PAIRINGS]:
        record = _record(raw)
        if record is not None:
            found[record.identity.key] = record
    return found, None


def load(path: Path | None = None) -> dict[str, Pairing]:
    """The stored customizations, or none at all. Never raises."""
    records, _fault = _read(path if path is not None else state_path())
    return records


def save(records: Mapping[str, Pairing], path: Path | None = None) -> Path:
    """Write the customizations atomically, and return where they went.

    Temporary file in the same directory, then `os.replace`, as `config.save`
    and `providers.credentials.save_key` do it. A half-written file would read
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
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
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
    if item.kind is not Kind.VIDEO:
        return Pairing(identity=identity, still=item.path, motion=None)
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
        customized=True,
        override_missing=missing,
    )


def apply(
    items: Iterable[MediaItem],
    roots: Sequence[Path] = (),
    records: Mapping[str, Pairing] | None = None,
) -> tuple[MediaItem, ...]:
    """Attach each item's representative still, and drop the stills spent as one.

    A still whose whole job is standing in for a video is not a separate
    wallpaper: leaving it in would put the same picture in the rotation twice,
    once by itself and once as the paused form of the video. A still that
    nobody points at stays, which is why a user's own `*-still.png` files
    survive when no video claims them.

    This replaces `library.pairing.apply`, which could only ever compute the
    default. The difference is the ``records`` argument.
    """
    materialised = list(items)
    resolved = {item.path: resolve(item, roots, records) for item in materialised}

    spent = {
        bundle.still
        for bundle in resolved.values()
        if bundle.is_moving and bundle.still is not None
    }
    kept: list[MediaItem] = []
    for item in materialised:
        bundle = resolved[item.path]
        if bundle.is_moving:
            kept.append(item.with_still(bundle.still))
        elif item.path not in spent:
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
        return identity.key in self._records

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
        existing = self._records.get(identity.key)
        palette = existing.palette if existing is not None else ADAPTIVE
        return self._commit(
            Pairing(identity=identity, still=still, palette=palette, customized=True)
        )

    def choose_palette(self, item: MediaItem, palette: str) -> Pairing:
        """Record a palette policy for ``item``. Step 10 gives this meaning."""
        identity = Identity.of(item)
        existing = self._records.get(identity.key)
        still = existing.still if existing is not None else None
        return self._commit(
            Pairing(identity=identity, still=still, palette=palette, customized=True)
        )

    def reset(self, item: MediaItem) -> bool:
        """Forget everything chosen for ``item``. True if there was anything."""
        identity = Identity.of(item)
        if identity.key not in self._records:
            return False
        del self._records[identity.key]
        self._write()
        return True

    def forget_identity(self, identity: Identity) -> bool:
        """Drop a record by identity, for a wallpaper the app has just deleted.

        Records outlive a missing file on purpose -- it may come back -- but
        not one we destroyed ourselves.
        """
        if identity.key not in self._records:
            return False
        del self._records[identity.key]
        self._write()
        return True

    def forget_path(self, path: Path) -> bool:
        """Drop any record naming ``path`` as its source, whatever the medium.

        A file is only ever one medium, so at most one of these matches; the
        caller has a path and no reason to know which. Callers that have the
        `MediaItem` should use `forget_identity`.
        """
        keys = [Identity(medium, str(path)).key for medium in Medium]
        present = [key for key in keys if key in self._records]
        if not present:
            return False
        for key in present:
            del self._records[key]
        self._write()
        return True

    def _commit(self, record: Pairing) -> Pairing:
        self._records[record.identity.key] = record
        self._write()
        return record

    def _write(self) -> None:
        target = self._path if self._path is not None else state_path()
        if self._fault is not None:
            # Do not overwrite bytes we could not understand: they are
            # somebody's choices, in some form, and a copy costs nothing.
            broken = target.with_name(target.name + BROKEN_SUFFIX)
            with contextlib.suppress(OSError):
                os.replace(target, broken)
            self._fault = None
        save(self._records, target)
