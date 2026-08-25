"""Durable intent journal for explicit wallpaper removals.

The media operation and the three authoring stores cannot be one filesystem
transaction.  This journal is the small durable bridge: a local Delete/Trash
gesture is recorded *before* the media can move, then retained until
the live transaction has consumed every deterministic artifact generation it
pinned before commit and favourites, Pairings, and playlists have been
cleaned. A restart can finish authored metadata and an exact token-owned media
claim, but never rediscovers physical companions after commit: a same-name file
could belong to a later lifecycle. Only paths named by this explicit journal
cross the missing-media boundary.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Literal, TypeVar

from wall_in_one import file_io, paths
from wall_in_one.library import state_file
from wall_in_one.library.model import Kind, MediaItem

STATE_FILENAME: Final = "pending-removals.json"
FORMAT_VERSION: Final = 1
MAX_PENDING_REMOVALS: Final = 4096
MAX_STATE_BYTES: Final = 4 * 1024 * 1024
MAX_ROOTS_PER_REMOVAL: Final = 32
MAX_PATH_BYTES: Final = 4096
MAX_PROVIDER_BYTES: Final = 256
MAX_TOKEN_BYTES: Final = 128
OPERATION_LOCK_SUFFIX: Final = ".operation.lock"

_Result = TypeVar("_Result")
OriginalGenerationState = Literal[
    "exact",
    "missing",
    "different",
    "ambiguous",
    "unavailable",
]


class RemovalJournalError(Exception):
    """A removal intent could not be durably recorded or updated."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    def __str__(self) -> str:
        return f"{self.kind}: {super().__str__()}"


def state_path() -> Path:
    return paths.app_state_dir() / STATE_FILENAME


def _bounded_bytes(value: str, maximum: int, *, filesystem: bool = False) -> bool:
    """Whether one string is representable, NUL-free, and within its wire bound."""
    if not value or "\x00" in value:
        return False
    try:
        encoded = os.fsencode(value) if filesystem else value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= maximum


def _bounded_absolute(raw: object) -> Path | None:
    if not isinstance(raw, str) or not _bounded_bytes(raw, MAX_PATH_BYTES, filesystem=True):
        return None
    path = Path(raw)
    return path if path.is_absolute() else None


@dataclass(frozen=True, slots=True)
class Intent:
    """One explicit lifecycle boundary, prepared or physically committed."""

    path: Path
    kind: Kind
    roots: tuple[Path, ...]
    scene: str = ""
    provider: str = "local"
    token: str = ""
    external: bool = False
    committed: bool = False
    device: int | None = None
    inode: int | None = None
    source_size: int | None = None
    source_mtime_ns: int | None = None
    source_ctime_ns: int | None = None
    source_root: Path | None = None
    root_device: int | None = None
    root_inode: int | None = None
    parent_device: int | None = None
    parent_inode: int | None = None

    @property
    def identity(self) -> str:
        source = self.scene if self.kind is Kind.SCENE and self.scene else str(self.path)
        return f"{self.kind.value}:{source}"

    @property
    def item(self) -> MediaItem:
        return MediaItem(
            path=self.path,
            kind=self.kind,
            size=0,
            mtime=0,
            provider=self.provider,
            scene=self.scene,
        )

    def original_is_present(self) -> bool:
        """Whether the exact pre-delete file generation is still at its path."""
        return self.original_generation_state() == "exact"

    def original_generation_state(
        self,
        *,
        lookup_path: Path | None = None,
    ) -> OriginalGenerationState:
        """Classify the public source without guessing across a rollback crash.

        A claim moved back to its original pathname keeps its device, inode,
        size, and mtime, but both renames advance ctime.  If the process dies
        after that safe restore and before cancelling the journal, the changed
        ctime is therefore ambiguous rather than evidence that deletion
        committed.  The same conservative answer covers an already-open writer
        changing the restored inode.  Inode reuse after process death means the
        journal cannot prove that either case is still the original, so replay
        must retain the intent instead of cancelling it or cleaning metadata.
        """
        identity = self.source_identity
        if identity is None:
            return "unavailable"
        target = self.path if lookup_path is None else lookup_path
        try:
            status = target.lstat()
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "unavailable"
        if not stat.S_ISREG(status.st_mode):
            return "different"
        expected = self.source_fingerprint
        if expected is None:
            # Backward-compatible v1 records use this only to cancel a
            # prepared, uncommitted intent. A false positive therefore
            # revokes authority; it can never authorize a physical mutation.
            return "exact" if (status.st_dev, status.st_ino) == identity else "different"
        try:
            current = file_io.file_fingerprint(status)
        except ValueError:
            return "different"
        if current == expected:
            return "exact"
        if current[:2] == expected[:2]:
            return "ambiguous"
        return "different"

    @property
    def source_identity(self) -> tuple[int, int] | None:
        """The exact directory entry authorized by the prepare transaction."""
        if self.device is None or self.inode is None:
            return None
        return self.device, self.inode

    @property
    def source_fingerprint(self) -> file_io.FileFingerprint | None:
        """Persisted evidence distinguishing an inode from a reused successor."""
        if (
            self.device is None
            or self.inode is None
            or self.source_size is None
            or self.source_mtime_ns is None
            or self.source_ctime_ns is None
        ):
            return None
        return (
            self.device,
            self.inode,
            self.source_size,
            self.source_mtime_ns,
            self.source_ctime_ns,
        )

    def source_context_is_present(self) -> bool:
        """Whether replay still sees the filesystem context captured at prepare.

        A missing source means physical removal committed only while both the
        selected library root and the source's parent directory are the same
        objects. Otherwise an unmounted drive is indistinguishable from a
        deletion and cleanup must wait.
        """
        try:
            with self.pin_source_context():
                return True
        except OSError, ValueError:
            return False

    def pin_source_context(self) -> file_io.PinnedDirectoryContext:
        """Retain the journaled root and parent for one replay decision."""
        if (
            self.source_root is None
            or self.root_device is None
            or self.root_inode is None
            or self.parent_device is None
            or self.parent_inode is None
        ):
            raise OSError("the pending removal has no journaled source directory context")
        return file_io.pin_directory_beneath(
            self.source_root,
            self.path.parent,
            expected_root_identity=(self.root_device, self.root_inode),
            expected_directory_identity=(self.parent_device, self.parent_inode),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "identity": self.identity,
            "path": str(self.path),
            "kind": self.kind.value,
            "roots": [str(root) for root in self.roots],
            "scene": self.scene,
            "provider": self.provider,
            "token": self.token,
            "external": self.external,
            "committed": self.committed,
            "device": self.device,
            "inode": self.inode,
            "source_size": self.source_size,
            "source_mtime_ns": self.source_mtime_ns,
            "source_ctime_ns": self.source_ctime_ns,
            "source_root": str(self.source_root) if self.source_root is not None else None,
            "root_device": self.root_device,
            "root_inode": self.root_inode,
            "parent_device": self.parent_device,
            "parent_inode": self.parent_inode,
        }


def _parse_intent(raw: object) -> Intent | None:
    if not isinstance(raw, dict):
        return None
    path = _bounded_absolute(raw.get("path"))
    roots_raw = raw.get("roots")
    if path is None or not isinstance(roots_raw, list):
        return None
    if len(roots_raw) > MAX_ROOTS_PER_REMOVAL:
        return None
    roots: list[Path] = []
    for candidate in roots_raw:
        root = _bounded_absolute(candidate)
        if root is None:
            return None
        roots.append(root)
    try:
        kind = Kind(raw.get("kind"))
    except TypeError, ValueError:
        return None
    scene = raw.get("scene", "")
    provider = raw.get("provider", "local")
    token = raw.get("token")
    external = raw.get("external", False)
    committed = raw.get("committed", False)
    device = raw.get("device")
    inode = raw.get("inode")
    source_size = raw.get("source_size")
    source_mtime_ns = raw.get("source_mtime_ns")
    source_ctime_ns = raw.get("source_ctime_ns")
    source_root_raw = raw.get("source_root")
    source_root = _bounded_absolute(source_root_raw) if source_root_raw is not None else None
    root_device = raw.get("root_device")
    root_inode = raw.get("root_inode")
    parent_device = raw.get("parent_device")
    parent_inode = raw.get("parent_inode")
    if (
        not isinstance(scene, str)
        or (bool(scene) and not _bounded_bytes(scene, MAX_PATH_BYTES, filesystem=True))
        or not isinstance(provider, str)
        or not _bounded_bytes(provider, MAX_PROVIDER_BYTES)
        or not isinstance(token, str)
        or not _bounded_bytes(token, MAX_TOKEN_BYTES)
        or type(external) is not bool
        or type(committed) is not bool
        or (device is not None and (type(device) is not int or device < 0))
        or (inode is not None and (type(inode) is not int or inode < 0))
        or (device is None) != (inode is None)
        or any(
            value is not None and type(value) is not int
            for value in (source_size, source_mtime_ns, source_ctime_ns)
        )
        or (source_size is not None and source_size < 0)
        or len(
            {
                source_size is None,
                source_mtime_ns is None,
                source_ctime_ns is None,
            }
        )
        != 1
        or any(
            value is not None and (type(value) is not int or value < 0)
            for value in (root_device, root_inode, parent_device, parent_inode)
        )
        or (root_device is None) != (root_inode is None)
        or (parent_device is None) != (parent_inode is None)
    ):
        return None
    if kind is Kind.SCENE:
        if not scene:
            return None
    elif scene:
        return None
    if external:
        if (
            not committed
            or not roots
            or device is not None
            or source_size is not None
            or source_root is not None
            or root_device is not None
            or parent_device is not None
        ):
            return None
    elif (
        device is None
        or not roots
        or source_root is None
        or source_root not in roots
        or not path.is_relative_to(source_root)
        or root_device is None
        or parent_device is None
    ):
        return None
    intent = Intent(
        path=path,
        kind=kind,
        roots=tuple(roots),
        scene=scene,
        provider=provider,
        token=token,
        external=external,
        committed=committed,
        device=device,
        inode=inode,
        source_size=source_size,
        source_mtime_ns=source_mtime_ns,
        source_ctime_ns=source_ctime_ns,
        source_root=source_root,
        root_device=root_device,
        root_inode=root_inode,
        parent_device=parent_device,
        parent_inode=parent_inode,
    )
    return intent if raw.get("identity") == intent.identity else None


def _read(path: Path) -> tuple[dict[str, Intent], str | None]:
    document, fault = state_file.read_object(
        path,
        maximum_bytes=MAX_STATE_BYTES,
        description="pending-removals",
    )
    if fault is not None or document is None:
        return {}, fault
    version = document.get("version")
    if type(version) is not int or version != FORMAT_VERSION:
        return {}, f"{path.name} has unsupported version {version!r}; expected {FORMAT_VERSION}"
    raw_entries = document.get("removals")
    if not isinstance(raw_entries, list):
        return {}, f"{path.name} has no removals in it"
    if len(raw_entries) > MAX_PENDING_REMOVALS:
        return {}, f"{path.name} has more than {MAX_PENDING_REMOVALS} removal intents"
    records: dict[str, Intent] = {}
    malformed = 0
    duplicate = 0
    for raw in raw_entries:
        intent = _parse_intent(raw)
        if intent is None:
            malformed += 1
            continue
        if intent.identity in records:
            duplicate += 1
            continue
        records[intent.identity] = intent
    faults: list[str] = []
    if malformed:
        faults.append(f"{path.name} has {malformed} malformed removal intents")
    if duplicate:
        faults.append(f"{path.name} has {duplicate} duplicate removal identities")
    return records, state_file.joined_faults(faults)


def _save(records: Mapping[str, Intent], path: Path) -> None:
    if len(records) > MAX_PENDING_REMOVALS:
        raise RemovalJournalError(
            "full", f"pending removal journal is limited to {MAX_PENDING_REMOVALS} items"
        )
    for identity, intent in records.items():
        if (
            not isinstance(intent, Intent)
            or identity != intent.identity
            or _parse_intent(intent.to_json()) != intent
        ):
            raise RemovalJournalError(
                "invalid-state", "pending removal journal contains an invalid intent"
            )
    payload = {
        "version": FORMAT_VERSION,
        "removals": [records[key].to_json() for key in sorted(records)],
    }
    rendered = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    try:
        encoded = rendered.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RemovalJournalError(
            "invalid-state", "pending removal journal contains text which is not valid UTF-8"
        ) from error
    if len(encoded) > MAX_STATE_BYTES:
        raise RemovalJournalError(
            "full", f"pending removal journal is limited to {MAX_STATE_BYTES} bytes"
        )
    try:
        paths.ensure_directory(path.parent)
        state_file.write_atomic_text(path, rendered)
    except OSError as error:
        raise RemovalJournalError(
            "local-io", f"could not write {path}: {error.strerror or error}"
        ) from error


class Store:
    """Rebased durable mutations over the pending-removal journal."""

    def __init__(
        self,
        records: Mapping[str, Intent] | None = None,
        path: Path | None = None,
        *,
        fault: str | None = None,
    ) -> None:
        self._records = dict(records or {})
        self._path = path if path is not None else state_path()
        self._fault = fault
        self._operation_descriptor: int | None = None
        self._operation_token: str | None = None
        self._source_pin: file_io.PinnedPath | None = None

    @classmethod
    def open(cls, path: Path | None = None) -> Store:
        target = path if path is not None else state_path()
        records, fault = _read(target)
        return cls(records, target, fault=fault)

    def worker_copy(self, *, rebase: bool = False) -> Store:
        """Return an unleased Store for a background reconciliation owner.

        Operation leases are process-local capabilities and are deliberately
        never copied.  ``rebase`` reloads the durable journal and must only be
        requested after this detached value has reached an I/O worker.
        """
        if rebase:
            return type(self).open(self._path)
        return type(self)(self._records, self._path, fault=self._fault)

    @property
    def operation_owned(self) -> bool:
        """Whether this exact Store currently owns a live removal lease."""
        return self._operation_descriptor is not None

    def adopt_worker_repair(self, expected: str) -> bool:
        """Clear only the same fault a detached worker proved repaired."""
        if self._fault != expected:
            return False
        self._fault = None
        return True

    @property
    def records(self) -> tuple[Intent, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    @property
    def fault(self) -> str | None:
        return self._fault

    def _mutate(self, change: Callable[[dict[str, Intent]], _Result]) -> _Result:
        try:
            with state_file.mutation_lock(
                self._path,
                description="pending-removals",
            ):
                records, fault = _read(self._path)
                if fault is not None:
                    self._fault = fault
                    raise RemovalJournalError(
                        "invalid-state",
                        f"cannot safely update pending removals: {fault}",
                    )
                result = change(records)
                _save(records, self._path)
                self._records = records
                self._fault = None
                return result
        except OSError as error:
            raise RemovalJournalError(
                "local-io",
                f"cannot safely lock pending removals at {self._path}: {error}",
            ) from error

    @property
    def _operation_lock_path(self) -> Path:
        return self._path.absolute().with_name(self._path.name + OPERATION_LOCK_SUFFIX)

    def _take_operation_lease(self, token: str) -> None:
        """Exclude another local remover/replayer until this operation ends."""
        if self._operation_descriptor is not None:
            raise RemovalJournalError("busy", "another wallpaper removal is still in progress")
        lock_path = self._operation_lock_path
        descriptor: int | None = None
        try:
            paths.ensure_directory(lock_path.parent)
            descriptor = os.open(
                lock_path,
                os.O_RDWR
                | os.O_CREAT
                | os.O_CLOEXEC
                | os.O_NONBLOCK
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            opened = os.fstat(descriptor)
            current = lock_path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                or opened.st_uid != os.getuid()
                or opened.st_nlink != 1
            ):
                raise OSError(f"removal operation lock {lock_path} is not a private regular file")
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RemovalJournalError(
                    "busy", "another wallpaper removal is still in progress"
                ) from error
            secured = os.fstat(descriptor)
            current = lock_path.lstat()
            if (
                not stat.S_ISREG(secured.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (secured.st_dev, secured.st_ino) != (opened.st_dev, opened.st_ino)
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
                or secured.st_uid != os.getuid()
                or current.st_uid != os.getuid()
                or secured.st_nlink != 1
                or current.st_nlink != 1
                or stat.S_IMODE(secured.st_mode) != 0o600
                or stat.S_IMODE(current.st_mode) != 0o600
            ):
                raise OSError(f"removal operation lock {lock_path} changed while it was secured")
        except RemovalJournalError:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise
        except OSError as error:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise RemovalJournalError(
                "local-io", f"could not lock wallpaper removal operations: {error}"
            ) from error
        self._operation_descriptor = descriptor
        self._operation_token = token

    def _release_operation_lease(self, token: str | None = None) -> None:
        if self._operation_descriptor is None:
            return
        if token is not None and token != self._operation_token:
            return
        descriptor = self._operation_descriptor
        source_pin = self._source_pin
        self._operation_descriptor = None
        self._operation_token = None
        self._source_pin = None
        active_error = sys.exception()

        def note(cleanup: str, error: OSError) -> None:
            if active_error is not None:
                active_error.add_note(f"also could not {cleanup}: {error}")

        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError as error:
                note("unlock the removal operation lease", error)
        finally:
            try:
                try:
                    os.close(descriptor)
                except OSError as error:
                    note("close the removal operation lease", error)
            finally:
                if source_pin is not None:
                    try:
                        source_pin.close()
                    except OSError as error:
                        note("close the prepared removal source pin", error)

    def operation_is_active(self) -> bool:
        """Whether a live process is between durable prepare and finish."""
        if self._operation_descriptor is not None:
            return True
        probe = Store(path=self._path)
        token = secrets.token_hex(16)
        try:
            probe._take_operation_lease(token)
        except RemovalJournalError as error:
            if error.kind == "busy":
                return True
            raise
        finally:
            probe._release_operation_lease(token)
        return False

    def close(self) -> None:
        """Release an in-flight process lease without changing its journal."""
        self._release_operation_lease()

    def __del__(self) -> None:
        source_pin = getattr(self, "_source_pin", None)
        if source_pin is not None:
            with contextlib.suppress(OSError):
                source_pin.close()
        descriptor = getattr(self, "_operation_descriptor", None)
        if descriptor is not None:
            with contextlib.suppress(OSError):
                os.close(descriptor)

    def prepare(self, item: MediaItem, roots: Sequence[Path]) -> Intent:
        """Persist authority before a local Delete/Trash can touch media."""
        bounded = tuple(roots)
        if len(bounded) > MAX_ROOTS_PER_REMOVAL:
            raise RemovalJournalError(
                "full", f"one removal may name at most {MAX_ROOTS_PER_REMOVAL} library roots"
            )
        if (
            not item.path.is_absolute()
            or not _bounded_bytes(str(item.path), MAX_PATH_BYTES, filesystem=True)
            or not bounded
            or any(
                not root.is_absolute()
                or not _bounded_bytes(str(root), MAX_PATH_BYTES, filesystem=True)
                for root in bounded
            )
            or not any(item.path.is_relative_to(root) for root in bounded)
            or (
                bool(item.scene) and not _bounded_bytes(item.scene, MAX_PATH_BYTES, filesystem=True)
            )
            or not _bounded_bytes(item.provider, MAX_PROVIDER_BYTES)
            or (item.kind is Kind.SCENE) != bool(item.scene)
        ):
            raise RemovalJournalError(
                "outside-root", f"{item.path} is not inside the configured library"
            )
        source_root = max(
            (root for root in bounded if item.path.is_relative_to(root)),
            key=lambda root: len(root.parts),
        )
        token = secrets.token_hex(16)
        self._take_operation_lease(token)
        try:
            scoped_pin = file_io.pin_regular_path_beneath(source_root, item.path)
            source_pin = scoped_pin.source
            self._source_pin = source_pin
            status = source_pin.status()
            confirmed = item.path.lstat()
            if not stat.S_ISREG(confirmed.st_mode) or file_io.file_fingerprint(
                status
            ) != file_io.file_fingerprint(confirmed):
                raise OSError(f"{item.path} changed while its removal was being prepared")
        except (OSError, ValueError) as error:
            self._release_operation_lease(token)
            raise RemovalJournalError(
                "local-io",
                f"could not identify {item.path}: {getattr(error, 'strerror', None) or error}",
            ) from error
        proposed = Intent(
            path=item.path,
            kind=item.kind,
            roots=bounded,
            scene=item.scene,
            provider=item.provider,
            token=token,
            device=status.st_dev,
            inode=status.st_ino,
            source_size=status.st_size,
            source_mtime_ns=status.st_mtime_ns,
            source_ctime_ns=status.st_ctime_ns,
            source_root=source_root,
            root_device=scoped_pin.root_identity[0],
            root_inode=scoped_pin.root_identity[1],
            parent_device=scoped_pin.parent_identity[0],
            parent_inode=scoped_pin.parent_identity[1],
        )

        def insert(records: dict[str, Intent]) -> Intent:
            existing = records.get(proposed.identity)
            if existing is not None:
                raise RemovalJournalError(
                    "busy",
                    f"a pending removal already owns {proposed.identity}",
                )
            if len(records) >= MAX_PENDING_REMOVALS:
                raise RemovalJournalError(
                    "full",
                    f"pending removal journal is limited to {MAX_PENDING_REMOVALS} items",
                )
            records[proposed.identity] = proposed
            return proposed

        try:
            return self._mutate(insert)
        except Exception:
            self._release_operation_lease(token)
            raise

    def source_pin(self, intent: Intent) -> file_io.PinnedPath:
        """Borrow the live prepare capability for this exact operation."""
        pin = self._source_pin
        if (
            self._operation_token != intent.token
            or pin is None
            or pin.path != intent.path
            or intent.source_identity is None
            or intent.source_fingerprint is None
        ):
            raise RemovalJournalError(
                "invalid-state",
                "the prepared removal no longer owns its live source reference",
            )
        try:
            if (
                pin.identity != intent.source_identity
                or pin.fingerprint != intent.source_fingerprint
            ):
                raise RemovalJournalError(
                    "changed",
                    f"{intent.path} changed after its removal was prepared",
                )
        except OSError as error:
            raise RemovalJournalError(
                "local-io", f"could not verify the prepared source reference: {error}"
            ) from error
        return pin

    def record_external(self, item: MediaItem, roots: Sequence[Path]) -> Intent:
        """Best-effort durable record for an already-confirmed external uninstall."""
        bounded = tuple(roots)
        if len(bounded) > MAX_ROOTS_PER_REMOVAL:
            raise RemovalJournalError(
                "full", f"one removal may name at most {MAX_ROOTS_PER_REMOVAL} library roots"
            )
        if (
            not item.path.is_absolute()
            or not _bounded_bytes(str(item.path), MAX_PATH_BYTES, filesystem=True)
            or any(
                not root.is_absolute()
                or not _bounded_bytes(str(root), MAX_PATH_BYTES, filesystem=True)
                for root in bounded
            )
            or (
                bool(item.scene) and not _bounded_bytes(item.scene, MAX_PATH_BYTES, filesystem=True)
            )
            or not _bounded_bytes(item.provider, MAX_PROVIDER_BYTES)
            or not bounded
            or (item.kind is Kind.SCENE) != bool(item.scene)
        ):
            raise RemovalJournalError("invalid-state", "external removal paths are not bounded")
        proposed = Intent(
            path=item.path,
            kind=item.kind,
            roots=bounded,
            scene=item.scene,
            provider=item.provider,
            token=secrets.token_hex(16),
            external=True,
            committed=True,
        )

        def insert(records: dict[str, Intent]) -> Intent:
            existing = records.get(proposed.identity)
            if existing is not None:
                if replace(existing, token=proposed.token) != proposed:
                    raise RemovalJournalError(
                        "invalid-state",
                        f"a different pending removal already owns {proposed.identity}",
                    )
                return existing
            if len(records) >= MAX_PENDING_REMOVALS:
                raise RemovalJournalError(
                    "full",
                    f"pending removal journal is limited to {MAX_PENDING_REMOVALS} items",
                )
            records[proposed.identity] = proposed
            return proposed

        return self._mutate(insert)

    def mark_committed(self, intent: Intent) -> Intent:
        def mark(records: dict[str, Intent]) -> Intent:
            current = records.get(intent.identity)
            if current is None:
                raise RemovalJournalError(
                    "invalid-state", f"pending removal {intent.identity} no longer exists"
                )
            if replace(current, committed=intent.committed) != intent:
                raise RemovalJournalError(
                    "invalid-state",
                    f"a different pending removal now owns {intent.identity}",
                )
            records[intent.identity] = replace(current, committed=True)
            return records[intent.identity]

        return self._mutate(mark)

    def discard(self, intent: Intent) -> bool:
        def remove(records: dict[str, Intent]) -> bool:
            current = records.get(intent.identity)
            if current is None:
                return False
            if replace(current, committed=intent.committed) != intent:
                raise RemovalJournalError(
                    "invalid-state",
                    f"a different pending removal now owns {intent.identity}",
                )
            del records[intent.identity]
            return True

        try:
            return self._mutate(remove)
        finally:
            self._release_operation_lease(intent.token)

    def reload(self) -> tuple[Intent, ...]:
        records, fault = _read(self._path)
        self._records = records
        self._fault = fault
        return self.records

    def owns(self, intent: Intent) -> bool:
        """Whether the durable journal still contains this exact token owner."""
        self.reload()
        if self._fault is not None:
            return False
        current = self._records.get(intent.identity)
        return current is not None and replace(current, committed=intent.committed) == intent

    def finish_operation(self, intent: Intent) -> None:
        """Release this process's lease after commit/cancel reaches a boundary."""
        self._release_operation_lease(intent.token)
