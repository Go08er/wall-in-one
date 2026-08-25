"""Register our palette template with Noctalia.

Noctalia supports user-defined templates as `[theme.templates.user.<id>]` in
its `settings.toml` (`noctalia/src/config/config_types.h:1411-1423`). Because
that is a real schema field, Noctalia round-trips it through its own settings
writes rather than dropping it.

Registering one gets us push-based palette sync: Noctalia re-renders the
template on every palette change and then runs its `post_hook`, which tells the
running app to reload. The app also monitors the rendered file because a hook
without XDG_RUNTIME_DIR cannot find its socket, and because nothing checks a
failed hook's exit status.

The plugin cannot do this itself -- the Luau host API has `writeFile` and
`getConfig` but no config setter -- so it shells out to
`wall-in-one --install-theme-template`, which is this module.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from wall_in_one import file_io, paths
from wall_in_one.theme import noctalia

TEMPLATE_ID: Final = "wall-in-one"
TEMPLATE_FILENAME: Final = "palette.json.tmpl"

#: Marker written above the block we append, so a later uninstall can find the
#: exact region it owns instead of guessing.
_BEGIN_MARKER: Final = "# >>> wall-in-one palette template (managed) >>>"
_END_MARKER: Final = "# <<< wall-in-one palette template (managed) <<<"
MAX_NOCTALIA_SETTINGS_BYTES: Final = 8 * 1024 * 1024
MAX_TEMPLATE_BYTES: Final = 1024 * 1024
MAX_TRANSACTION_RECORD_BYTES: Final = 16 * 1024
MAX_TRANSACTION_DOCUMENT_BYTES: Final = MAX_NOCTALIA_SETTINGS_BYTES + 64 * 1024
_RENAME_EXCHANGE: Final = 2
_AT_FDCWD: Final = -100
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2: Any = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    _RENAMEAT2.restype = ctypes.c_int


def _close_owned_resources(
    resources: list[tuple[Callable[[], None], str]],
) -> None:
    """Close every owned resource once, preserving the first active error."""

    active_error = sys.exception()
    first_close_error: OSError | None = None
    for close, purpose in resources:
        try:
            close()
        except OSError as error:
            if active_error is not None:
                active_error.add_note(f"also could not close {purpose}: {error}")
            elif first_close_error is None:
                first_close_error = error
            else:
                first_close_error.add_note(f"also could not close {purpose}: {error}")
    if first_close_error is not None:
        raise first_close_error


def _descriptor_closer(descriptor: int) -> Callable[[], None]:
    def close() -> None:
        os.close(descriptor)

    return close


class TemplateInstallError(Exception):
    """Registering or removing the template failed."""


@dataclass(frozen=True, slots=True)
class InstallResult:
    changed: bool
    settings_path: Path
    template_path: Path
    output_path: Path
    backup_path: Path | None
    detail: str


@dataclass(slots=True)
class _SettingsSnapshot:
    """One safely-read settings inode, held open until publication finishes."""

    document: bytes
    text: str
    device: int
    inode: int
    _descriptor: int = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def descriptor(self) -> int:
        if self._closed:
            raise OSError("the Noctalia settings snapshot is already closed")
        return self._descriptor

    def close(self) -> None:
        if self._closed:
            return
        descriptor = self._descriptor
        self._closed = True
        self._descriptor = -1
        _close_owned_resources([(_descriptor_closer(descriptor), "the Noctalia settings snapshot")])

    def __enter__(self) -> _SettingsSnapshot:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


@dataclass(slots=True)
class _PublicationTransaction:
    """A discoverable, durable exchange which retains every displaced entry."""

    swap: Path
    record: Path
    backup: Path
    candidate_document: bytes = field(repr=False)
    candidate_identity: file_io.PathIdentity
    candidate_fingerprint: file_io.FileFingerprint
    record_identity: file_io.PathIdentity
    record_fingerprint: file_io.FileFingerprint
    lock: Path
    lock_identity: file_io.PathIdentity
    lock_fingerprint: file_io.FileFingerprint
    _candidate_descriptor: int = field(repr=False)
    _record_descriptor: int = field(repr=False)
    _lock_descriptor: int = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        descriptors = (
            (self._candidate_descriptor, "the staged settings candidate"),
            (self._record_descriptor, "the template transaction record"),
            (self._lock_descriptor, "the template transaction lock"),
        )
        self._candidate_descriptor = -1
        self._record_descriptor = -1
        self._lock_descriptor = -1
        resources: list[tuple[Callable[[], None], str]] = [
            (_descriptor_closer(descriptor), purpose) for descriptor, purpose in descriptors
        ]
        _close_owned_resources(resources)


@dataclass(slots=True)
class _RecoveryEntry:
    """One no-follow transaction entry pinned while recovery decides."""

    path: Path
    _access_path: Path = field(repr=False)
    descriptor: int = field(repr=False)
    file_type: int
    identity: file_io.PathIdentity
    document: bytes | None = field(repr=False)
    _fingerprint: file_io.FileFingerprint | None = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def fingerprint(self) -> file_io.FileFingerprint:
        if self.file_type != stat.S_IFREG or self._fingerprint is None:
            raise ValueError("only a regular recovery entry has a fingerprint")
        return self._fingerprint

    def _named_status(
        self,
        access_path: Path,
        logical_path: Path,
    ) -> tuple[os.stat_result, os.stat_result]:
        opened = os.fstat(self.descriptor)
        named = access_path.lstat()
        if (
            stat.S_IFMT(opened.st_mode) != self.file_type
            or stat.S_IFMT(named.st_mode) != self.file_type
            or (opened.st_dev, opened.st_ino) != self.identity
            or (named.st_dev, named.st_ino) != self.identity
        ):
            raise TemplateInstallError(f"transaction entry {logical_path} changed during recovery")
        return opened, named

    def verify_named(
        self,
        access_path: Path | None = None,
        *,
        logical_path: Path | None = None,
    ) -> None:
        access = self._access_path if access_path is None else access_path
        logical = self.path if logical_path is None else logical_path
        opened, named = self._named_status(access, logical)
        if self.file_type == stat.S_IFREG and (
            file_io.file_fingerprint(opened) != self.fingerprint
            or file_io.file_fingerprint(named) != self.fingerprint
        ):
            raise TemplateInstallError(
                f"regular transaction entry {logical} changed generation during recovery"
            )

    def verify_identity_at(self, access_path: Path, *, logical_path: Path | None = None) -> None:
        """Verify identity after our rename without authorizing deletion."""

        self._named_status(access_path, self.path if logical_path is None else logical_path)

    def verify_relocated_document(
        self,
        access_path: Path,
        *,
        logical_path: Path | None = None,
    ) -> None:
        """Rebind rename-updated metadata only after exact bytes are reread."""

        logical = self.path if logical_path is None else logical_path
        if self.file_type != stat.S_IFREG or self.document is None:
            raise TemplateInstallError(f"cannot prove relocated transaction entry {logical}")
        opened, _named = self._named_status(access_path, logical)
        chunks: list[bytes] = []
        offset = 0
        remaining = len(self.document) + 1
        while remaining:
            chunk = os.pread(self.descriptor, min(remaining, 64 * 1024), offset)
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
            remaining -= len(chunk)
        after, named = self._named_status(access_path, logical)
        if b"".join(chunks) != self.document:
            raise TemplateInstallError(f"relocated transaction entry {logical} changed bytes")
        after_fingerprint = file_io.file_fingerprint(after)
        if (
            opened.st_dev,
            opened.st_ino,
        ) != self.identity or after_fingerprint != file_io.file_fingerprint(named):
            raise TemplateInstallError(f"relocated transaction entry {logical} changed generation")
        self._fingerprint = after_fingerprint

    def close(self) -> None:
        if not self._closed:
            descriptor = self.descriptor
            self._closed = True
            self.descriptor = -1
            _close_owned_resources(
                [(_descriptor_closer(descriptor), f"the recovery entry {self.path}")]
            )

    def __enter__(self) -> _RecoveryEntry:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class _RecoveryRecord:
    backup: str
    candidate_device: int
    candidate_inode: int
    candidate_sha256: str
    candidate_size: int
    expected_device: int
    expected_inode: int
    expected_sha256: str
    expected_size: int
    candidate: str | None
    lock: str | None
    lock_fingerprint: file_io.FileFingerprint | None


def bundled_template() -> Path:
    """The template shipped alongside this package.

    Looked up relative to the installed package so it works from a Nix store
    path, an editable install, or a source checkout alike.
    """
    candidates = (
        Path(__file__).resolve().parent.parent / "data" / TEMPLATE_FILENAME,
        Path(__file__).resolve().parents[3] / "templates" / TEMPLATE_FILENAME,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(path) for path in candidates)
    raise TemplateInstallError(f"cannot find {TEMPLATE_FILENAME}; looked in: {searched}")


def installed_template_path(document: bytes | None = None) -> Path:
    """Where the template lives once installed.

    The content-addressed name is stable across equivalent Nix rebuilds while
    allowing a changed bundled template to publish without replacing any
    existing pathname. Noctalia therefore never points into a
    garbage-collectable store path, and a same-name foreign entry is preserved
    as a conflict rather than overwritten.
    """
    if document is None:
        source = bundled_template()
        try:
            document = file_io.read_regular_bytes(source, MAX_TEMPLATE_BYTES)
        except OSError as error:
            raise TemplateInstallError(f"cannot safely read palette template: {error}") from error
        if document is None:
            raise TemplateInstallError(f"palette template disappeared: {source}")
    digest = hashlib.sha256(document).hexdigest()
    suffix = "".join(Path(TEMPLATE_FILENAME).suffixes)
    stem = TEMPLATE_FILENAME[: -len(suffix)] if suffix else TEMPLATE_FILENAME
    return paths.app_state_dir() / f"{stem}-{digest}{suffix}"


def _read_settings_snapshot(path: Path) -> _SettingsSnapshot:
    """Read and retain one bounded regular inode without following a link."""

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise TemplateInstallError(
            f"Noctalia settings not found at {path}; is Noctalia installed and has it run once?"
        ) from error
    except OSError as error:
        raise TemplateInstallError(f"cannot safely read {path}: {error}") from error

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise TemplateInstallError(f"cannot safely read {path}: it is not a regular file")
        if opened.st_size > MAX_NOCTALIA_SETTINGS_BYTES:
            raise TemplateInstallError(
                f"cannot safely read {path}: it exceeds its "
                f"{MAX_NOCTALIA_SETTINGS_BYTES}-byte limit"
            )

        chunks: list[bytes] = []
        remaining = MAX_NOCTALIA_SETTINGS_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        document = b"".join(chunks)
        if len(document) > MAX_NOCTALIA_SETTINGS_BYTES:
            raise TemplateInstallError(
                f"cannot safely read {path}: it exceeds its "
                f"{MAX_NOCTALIA_SETTINGS_BYTES}-byte limit"
            )

        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(after.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
            or file_io.file_fingerprint(opened) != file_io.file_fingerprint(after)
            or file_io.file_fingerprint(after) != file_io.file_fingerprint(current)
        ):
            raise TemplateInstallError(f"cannot safely read {path}: it changed while being read")
        text = document.decode("utf-8")
    except UnicodeDecodeError as error:
        _close_owned_resources(
            [(_descriptor_closer(descriptor), f"the Noctalia settings snapshot {path}")]
        )
        raise TemplateInstallError(f"{path} is not UTF-8 text") from error
    except TemplateInstallError:
        _close_owned_resources(
            [(_descriptor_closer(descriptor), f"the Noctalia settings snapshot {path}")]
        )
        raise
    except OSError as error:
        _close_owned_resources(
            [(_descriptor_closer(descriptor), f"the Noctalia settings snapshot {path}")]
        )
        raise TemplateInstallError(f"cannot safely read {path}: {error}") from error
    return _SettingsSnapshot(
        document=document,
        text=text,
        device=opened.st_dev,
        inode=opened.st_ino,
        _descriptor=descriptor,
    )


def _read_settings_text(path: Path) -> str:
    with _read_settings_snapshot(path) as snapshot:
        return snapshot.text


def _read_settings(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(_read_settings_text(path))
    except (tomllib.TOMLDecodeError, RecursionError) as error:
        raise TemplateInstallError(f"{path} is not valid TOML: {error}") from error


def _existing_entry(settings: dict[str, Any]) -> dict[str, Any] | None:
    node: Any = settings
    for key in ("theme", "templates", "user", TEMPLATE_ID):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
        if node is None:
            return None
    return node if isinstance(node, dict) else None


def _toml_escape(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_block(template_path: Path, output_path: Path, post_hook: str) -> str:
    return "\n".join(
        (
            _BEGIN_MARKER,
            f"[theme.templates.user.{TEMPLATE_ID}]",
            "enabled = true",
            f"input_path = {_toml_escape(str(template_path))}",
            f"output_path = {_toml_escape(str(output_path))}",
            f"post_hook = {_toml_escape(post_hook)}",
            _END_MARKER,
        )
    )


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_durable_directory(path: Path) -> None:
    """Create one directory chain and durably publish every new component."""

    try:
        status = path.lstat()
    except FileNotFoundError as error:
        parent = path.parent
        if parent == path:
            raise TemplateInstallError(f"cannot create template directory {path}") from error
        _ensure_durable_directory(parent)
        with contextlib.suppress(FileExistsError):
            path.mkdir()
        try:
            status = path.lstat()
        except OSError as error:
            raise TemplateInstallError(
                f"cannot verify created template directory {path}: {error}"
            ) from error
    except OSError as error:
        raise TemplateInstallError(f"cannot inspect template directory {path}: {error}") from error
    if not stat.S_ISDIR(status.st_mode):
        raise TemplateInstallError(f"template directory path is not a directory: {path}")
    try:
        _fsync_parent(path)
    except OSError as error:
        raise TemplateInstallError(
            f"cannot durably publish template directory {path}: {error}"
        ) from error


def _rename_exchange(source: Path, destination: Path) -> None:
    """Atomically exchange two names, or fail without changing either one."""

    if _RENAMEAT2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable on this system")
    result = _RENAMEAT2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_EXCHANGE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(destination))


def _backup(path: Path, snapshot: _SettingsSnapshot) -> Path:
    """Publish a durable byte copy without removing the canonical settings."""

    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.backup-", dir=path.parent)
    temporary = Path(name)
    candidate_identity: file_io.PathIdentity | None = None
    candidate_fingerprint: file_io.FileFingerprint | None = None
    try:
        created = os.fstat(descriptor)
        candidate_identity = created.st_dev, created.st_ino
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            original_mode = stat.S_IMODE(os.fstat(snapshot.descriptor).st_mode)
            os.fchmod(descriptor, original_mode)
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(snapshot.document)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            raise TemplateInstallError(f"cannot create a backup of {path}: {error}") from error
        candidate_fingerprint = file_io.file_fingerprint(os.fstat(descriptor))

        for index in range(10_000):
            suffix = "" if index == 0 else f".{index}"
            destination = path.with_name(f"{path.name}.bak-{TEMPLATE_ID}-{stamp}{suffix}")
            try:
                file_io.atomic_move_no_replace(
                    temporary,
                    destination,
                    expected_identity=candidate_identity,
                    expected_file_type=stat.S_IFREG,
                    expected_fingerprint=candidate_fingerprint,
                )
            except FileExistsError:
                continue
            except (file_io.PathChangedError, OSError) as error:
                raise TemplateInstallError(f"cannot atomically back up {path}: {error}") from error
            try:
                _fsync_parent(destination)
            except OSError as error:
                raise TemplateInstallError(f"cannot durably back up {path}: {error}") from error
            return destination
        raise TemplateInstallError(f"cannot back up {path}: too many same-second backups")
    except TemplateInstallError:
        raise
    except OSError as error:
        raise TemplateInstallError(f"cannot create a backup of {path}: {error}") from error
    finally:
        try:
            with contextlib.suppress(OSError, ValueError):
                candidate_fingerprint = file_io.file_fingerprint(os.fstat(descriptor))
            with contextlib.suppress(OSError, ValueError):
                _discard_candidate_if_owned(
                    temporary,
                    candidate_identity,
                    expected_fingerprint=candidate_fingerprint,
                )
        finally:
            _close_owned_resources(
                [(_descriptor_closer(descriptor), f"the temporary settings backup {temporary}")]
            )


def _assert_claim_unchanged(
    backup: Path,
    expected: _SettingsSnapshot,
) -> file_io.FileFingerprint:
    """Verify a named entry and the still-open snapshot retain exact bytes."""

    try:
        claimed = backup.lstat()
        opened = os.fstat(expected.descriptor)
        if (
            not stat.S_ISREG(claimed.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or (claimed.st_dev, claimed.st_ino) != (expected.device, expected.inode)
            or (opened.st_dev, opened.st_ino) != (expected.device, expected.inode)
        ):
            raise TemplateInstallError("the settings inode changed")
        if opened.st_size > MAX_NOCTALIA_SETTINGS_BYTES:
            raise TemplateInstallError(
                f"the settings exceed their {MAX_NOCTALIA_SETTINGS_BYTES}-byte limit"
            )

        chunks: list[bytes] = []
        offset = 0
        remaining = MAX_NOCTALIA_SETTINGS_BYTES + 1
        while remaining:
            chunk = os.pread(expected.descriptor, min(remaining, 64 * 1024), offset)
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
            remaining -= len(chunk)
        current = b"".join(chunks)
        after = os.fstat(expected.descriptor)
        final_named = backup.lstat()
    except OSError as error:
        raise TemplateInstallError(f"cannot verify the settings inode: {error}") from error

    if (
        len(current) > MAX_NOCTALIA_SETTINGS_BYTES
        or (after.st_dev, after.st_ino) != (expected.device, expected.inode)
        or not stat.S_ISREG(after.st_mode)
        or not stat.S_ISREG(final_named.st_mode)
        or (final_named.st_dev, final_named.st_ino) != (expected.device, expected.inode)
        or file_io.file_fingerprint(after) != file_io.file_fingerprint(final_named)
        or current != expected.document
    ):
        raise TemplateInstallError("the settings bytes changed")
    try:
        # Flush the exact original inode before the candidate may become public.
        os.fsync(expected.descriptor)
    except OSError as error:
        raise TemplateInstallError(f"cannot sync the settings inode: {error}") from error
    return file_io.file_fingerprint(after)


def _assert_candidate_unchanged(
    path: Path,
    descriptor: int,
    candidate_identity: file_io.PathIdentity,
    candidate_document: bytes,
) -> file_io.FileFingerprint:
    """Verify that ``path`` still names the exact staged candidate bytes."""

    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (opened.st_dev, opened.st_ino) != candidate_identity
            or (named.st_dev, named.st_ino) != candidate_identity
            or file_io.file_fingerprint(opened) != file_io.file_fingerprint(named)
        ):
            raise TemplateInstallError("the staged settings candidate changed")
        chunks: list[bytes] = []
        offset = 0
        remaining = len(candidate_document) + 1
        while remaining:
            chunk = os.pread(descriptor, min(remaining, 64 * 1024), offset)
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        final_named = path.lstat()
    except OSError as error:
        raise TemplateInstallError(f"cannot verify the settings candidate: {error}") from error
    if (
        b"".join(chunks) != candidate_document
        or (after.st_dev, after.st_ino) != candidate_identity
        or not stat.S_ISREG(after.st_mode)
        or not stat.S_ISREG(final_named.st_mode)
        or (final_named.st_dev, final_named.st_ino) != candidate_identity
        or file_io.file_fingerprint(after) != file_io.file_fingerprint(final_named)
    ):
        raise TemplateInstallError("the staged settings candidate bytes changed")
    return file_io.file_fingerprint(after)


def _assert_regular_entry_unchanged(
    path: Path,
    descriptor: int,
    expected_fingerprint: file_io.FileFingerprint,
    *,
    role: str,
) -> None:
    """Require one named transaction entry to match its retained descriptor."""

    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
    except OSError as error:
        raise TemplateInstallError(f"cannot verify the template {role}: {error}") from error
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or file_io.file_fingerprint(opened) != expected_fingerprint
        or file_io.file_fingerprint(named) != expected_fingerprint
    ):
        raise TemplateInstallError(f"the template {role} changed generation")


def _transaction_directory(path: Path) -> Path:
    """The legacy, directory-backed recovery name used before record v2."""

    return path.with_name(f".{path.name}.{TEMPLATE_ID}-transaction")


def _transaction_locator(path: Path) -> Path:
    """The fixed, regular-file recovery locator for current transactions."""

    return path.with_name(f".{path.name}.{TEMPLATE_ID}-transaction-record")


def _transaction_leaf_prefix(path: Path, role: str) -> str:
    return f".{path.name}.{TEMPLATE_ID}-transaction-{role}-"


def _open_recovery_entry(
    access_path: Path,
    maximum_bytes: int,
    *,
    logical_path: Path | None = None,
) -> _RecoveryEntry | None:
    """Pin one typed entry and read regular bytes only within a strict bound."""

    displayed_path = access_path if logical_path is None else logical_path
    try:
        observed = access_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise TemplateInstallError(
            f"cannot inspect transaction entry {displayed_path}: {error}"
        ) from error
    file_type = stat.S_IFMT(observed.st_mode)
    flags = os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_RDONLY | os.O_NONBLOCK if file_type == stat.S_IFREG else os.O_PATH
    try:
        descriptor = os.open(access_path, flags)
    except OSError as error:
        raise TemplateInstallError(
            f"cannot pin transaction entry {displayed_path}: {error}"
        ) from error
    try:
        opened = os.fstat(descriptor)
        named = access_path.lstat()
        identity = opened.st_dev, opened.st_ino
        if (
            file_type == 0
            or stat.S_IFMT(opened.st_mode) != file_type
            or stat.S_IFMT(named.st_mode) != file_type
            or (named.st_dev, named.st_ino) != identity
            or (observed.st_dev, observed.st_ino) != identity
        ):
            raise TemplateInstallError(
                f"transaction entry {displayed_path} changed while being pinned"
            )

        document: bytes | None = None
        if file_type == stat.S_IFREG and opened.st_size <= maximum_bytes:
            chunks: list[bytes] = []
            offset = 0
            remaining = maximum_bytes + 1
            while remaining:
                chunk = os.pread(descriptor, min(remaining, 64 * 1024), offset)
                if not chunk:
                    break
                chunks.append(chunk)
                offset += len(chunk)
                remaining -= len(chunk)
            candidate = b"".join(chunks)
            if len(candidate) <= maximum_bytes:
                document = candidate

        after = os.fstat(descriptor)
        final_named = access_path.lstat()
        if (
            stat.S_IFMT(after.st_mode) != file_type
            or stat.S_IFMT(final_named.st_mode) != file_type
            or (after.st_dev, after.st_ino) != identity
            or (final_named.st_dev, final_named.st_ino) != identity
        ):
            raise TemplateInstallError(
                f"transaction entry {displayed_path} changed while being read"
            )
        if file_type == stat.S_IFREG and (
            file_io.file_fingerprint(opened) != file_io.file_fingerprint(after)
            or file_io.file_fingerprint(after) != file_io.file_fingerprint(final_named)
        ):
            raise TemplateInstallError(
                f"regular transaction entry {displayed_path} changed generation while being read"
            )
    except Exception:
        _close_owned_resources(
            [(_descriptor_closer(descriptor), f"the recovery entry {displayed_path}")]
        )
        raise
    fingerprint = file_io.file_fingerprint(after) if file_type == stat.S_IFREG else None
    return _RecoveryEntry(
        displayed_path,
        access_path,
        descriptor,
        file_type,
        identity,
        document,
        fingerprint,
    )


def _parse_recovery_record(
    settings_path: Path,
    path: Path,
    entry: _RecoveryEntry,
) -> _RecoveryRecord:
    """Parse only the canonical, bounded record format emitted by this build."""

    if entry.file_type != stat.S_IFREG or entry.document is None:
        raise TemplateInstallError(
            f"template transaction record {path} is not a bounded regular file"
        )
    try:
        parsed = json.loads(entry.document.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise TemplateInstallError(
            f"template transaction record {path} is invalid: {error}"
        ) from error
    common_keys = {
        "backup",
        "candidate_device",
        "candidate_inode",
        "candidate_sha256",
        "candidate_size",
        "expected_device",
        "expected_inode",
        "expected_sha256",
        "expected_size",
        "version",
    }
    if not isinstance(parsed, dict):
        raise TemplateInstallError(f"template transaction record {path} has an unknown schema")
    version = parsed.get("version")
    current_keys = {
        "candidate",
        "lock",
        "lock_ctime_ns",
        "lock_device",
        "lock_inode",
        "lock_mtime_ns",
        "lock_size",
    }
    expected_keys = common_keys if version == 1 else common_keys | current_keys
    if version not in {1, 3} or set(parsed) != expected_keys:
        raise TemplateInstallError(f"template transaction record {path} has an unknown schema")
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if canonical != entry.document:
        raise TemplateInstallError(f"template transaction record {path} is not canonical")

    integer_keys: tuple[str, ...] = (
        "candidate_device",
        "candidate_inode",
        "candidate_size",
        "expected_device",
        "expected_inode",
        "expected_size",
        "version",
    )
    if version == 3:
        integer_keys += (
            "lock_ctime_ns",
            "lock_device",
            "lock_inode",
            "lock_mtime_ns",
            "lock_size",
        )
    if any(type(parsed[key]) is not int or parsed[key] < 0 for key in integer_keys):
        raise TemplateInstallError(f"template transaction record {path} has invalid integers")
    if parsed["version"] not in {1, 3}:
        raise TemplateInstallError(f"template transaction record {path} has an unsupported version")
    if (
        parsed["candidate_size"] > MAX_TRANSACTION_DOCUMENT_BYTES
        or parsed["expected_size"] > MAX_NOCTALIA_SETTINGS_BYTES
    ):
        raise TemplateInstallError(f"template transaction record {path} exceeds its size limits")
    if (parsed["candidate_device"], parsed["candidate_inode"]) == (
        parsed["expected_device"],
        parsed["expected_inode"],
    ):
        raise TemplateInstallError(
            f"template transaction record {path} reuses one inode identity for both roles"
        )
    hash_keys = ("candidate_sha256", "expected_sha256")
    if any(
        not isinstance(parsed[key], str)
        or len(parsed[key]) != 64
        or any(character not in "0123456789abcdef" for character in parsed[key])
        for key in hash_keys
    ):
        raise TemplateInstallError(f"template transaction record {path} has invalid digests")
    backup = parsed["backup"]
    backup_prefix = f"{settings_path.name}.bak-{TEMPLATE_ID}-"
    if (
        not isinstance(backup, str)
        or Path(backup).name != backup
        or not backup.startswith(backup_prefix)
    ):
        raise TemplateInstallError(f"template transaction record {path} has an unsafe backup name")
    candidate: str | None = None
    lock: str | None = None
    lock_fingerprint: file_io.FileFingerprint | None = None
    if parsed["version"] == 3:
        candidate = parsed["candidate"]
        lock = parsed["lock"]
        if (
            not isinstance(candidate, str)
            or Path(candidate).name != candidate
            or not candidate.startswith(_transaction_leaf_prefix(settings_path, "candidate"))
        ):
            raise TemplateInstallError(
                f"template transaction record {path} has an unsafe candidate name"
            )
        if (
            not isinstance(lock, str)
            or Path(lock).name != lock
            or not lock.startswith(_transaction_leaf_prefix(settings_path, "lock"))
        ):
            raise TemplateInstallError(
                f"template transaction record {path} has an unsafe lock name"
            )
        if candidate == lock or candidate == path.name or lock == path.name:
            raise TemplateInstallError(f"template transaction record {path} reuses an entry name")
        lock_fingerprint = (
            parsed["lock_device"],
            parsed["lock_inode"],
            parsed["lock_size"],
            parsed["lock_mtime_ns"],
            parsed["lock_ctime_ns"],
        )
        if lock_fingerprint[2] != 0:
            raise TemplateInstallError(f"template transaction record {path} has a nonempty lock")
    # An older installer can legitimately have published an equal-document
    # transaction while updating only the separately stored palette template.
    # The two inode identities still distinguish the expected and candidate
    # roles across either side of RENAME_EXCHANGE, so recovery must accept and
    # retire that crash state instead of turning it into a permanent wedge.
    return _RecoveryRecord(
        backup=backup,
        candidate_device=parsed["candidate_device"],
        candidate_inode=parsed["candidate_inode"],
        candidate_sha256=parsed["candidate_sha256"],
        candidate_size=parsed["candidate_size"],
        expected_device=parsed["expected_device"],
        expected_inode=parsed["expected_inode"],
        expected_sha256=parsed["expected_sha256"],
        expected_size=parsed["expected_size"],
        candidate=candidate,
        lock=lock,
        lock_fingerprint=lock_fingerprint,
    )


def _recovery_role(entry: _RecoveryEntry | None, record: _RecoveryRecord) -> str:
    if entry is None:
        return "absent"
    if entry.file_type != stat.S_IFREG or entry.document is None:
        return "other"
    digest = hashlib.sha256(entry.document).hexdigest()
    device, inode = entry.identity
    if (
        device == record.expected_device
        and inode == record.expected_inode
        and len(entry.document) == record.expected_size
        and digest == record.expected_sha256
    ):
        return "expected"
    if (
        device == record.candidate_device
        and inode == record.candidate_inode
        and len(entry.document) == record.candidate_size
        and digest == record.candidate_sha256
    ):
        return "candidate"
    return "other"


def _discard_recovery_regular(
    access_path: Path,
    entry: _RecoveryEntry,
    *,
    logical_path: Path | None = None,
    retained_parent: Path,
    logical_retained_parent: Path | None = None,
) -> None:
    displayed_path = entry.path if logical_path is None else logical_path
    if entry.file_type != stat.S_IFREG:
        raise TemplateInstallError(
            f"refusing to discard non-regular transaction entry {displayed_path}"
        )
    entry.verify_named(access_path, logical_path=displayed_path)
    try:
        removed = file_io.discard_regular_if_same(
            access_path,
            expected_identity=entry.identity,
            expected_fingerprint=entry.fingerprint,
            retained_parent=retained_parent,
            logical_retained_parent=(
                retained_parent if logical_retained_parent is None else logical_retained_parent
            ),
        )
    except OSError as error:
        raise TemplateInstallError(
            f"cannot safely discard transaction entry {displayed_path}: {error}"
        ) from error
    if not removed:
        raise TemplateInstallError(
            f"transaction entry {displayed_path} changed before safe cleanup"
        )


def _retain_regular_intact(
    access_path: Path,
    *,
    expected_identity: file_io.PathIdentity,
    expected_fingerprint: file_io.FileFingerprint,
    retained_parent: Path,
    logical_retained_parent: Path,
    logical_path: Path | None = None,
) -> Path:
    """Retire one exact regular entry without truncating its durable bytes."""

    with file_io.pin_regular_path(
        access_path,
        expected_identity=expected_identity,
        expected_fingerprint=expected_fingerprint,
    ) as pin:
        return file_io._retain_exact_entry(
            access_path,
            expected_identity=expected_identity,
            expected_file_type=stat.S_IFREG,
            expected_fingerprint=expected_fingerprint,
            pinned_source=pin,
            retained_parent=retained_parent,
            logical_parent=logical_retained_parent,
            logical_path=logical_path,
        )


def _retain_recovery_record_intact(
    access_path: Path,
    entry: _RecoveryEntry,
    *,
    logical_path: Path | None = None,
    retained_parent: Path,
    logical_retained_parent: Path,
) -> None:
    displayed_path = entry.path if logical_path is None else logical_path
    if entry.file_type != stat.S_IFREG:
        raise TemplateInstallError(
            f"refusing to retire non-regular transaction record {displayed_path}"
        )
    entry.verify_named(access_path, logical_path=displayed_path)
    try:
        _retain_regular_intact(
            access_path,
            expected_identity=entry.identity,
            expected_fingerprint=entry.fingerprint,
            retained_parent=retained_parent,
            logical_retained_parent=logical_retained_parent,
            logical_path=displayed_path,
        )
    except OSError as error:
        raise TemplateInstallError(
            f"cannot safely retire transaction record {displayed_path}: {error}"
        ) from error


def _retain_recovery_candidate_intact(
    access_path: Path,
    entry: _RecoveryEntry,
    *,
    logical_path: Path | None = None,
    retained_parent: Path,
    logical_retained_parent: Path,
) -> None:
    """Retire an authoritative candidate without first changing its bytes."""

    displayed_path = entry.path if logical_path is None else logical_path
    if entry.file_type != stat.S_IFREG:
        raise TemplateInstallError(
            f"refusing to retire non-regular transaction candidate {displayed_path}"
        )
    entry.verify_named(access_path, logical_path=displayed_path)
    try:
        _retain_regular_intact(
            access_path,
            expected_identity=entry.identity,
            expected_fingerprint=entry.fingerprint,
            retained_parent=retained_parent,
            logical_retained_parent=logical_retained_parent,
            logical_path=displayed_path,
        )
    except OSError as error:
        raise TemplateInstallError(
            f"cannot safely retire transaction candidate {displayed_path}: {error}"
        ) from error


def _recovery_failure(path: Path, transaction: Path, detail: str) -> TemplateInstallError:
    return TemplateInstallError(
        f"cannot safely reconcile interrupted template transaction: {detail}; "
        f"canonical entry preserved at {path}; transaction entries preserved at {transaction}"
    )


def _directory_anchor(pin: file_io.PinnedPath) -> Path:
    """Address a pinned directory without resolving its replaceable public name."""

    return Path("/proc/self/fd") / str(pin.descriptor)


def _fsync_pinned_directory(pin: file_io.PinnedPath, logical_path: Path) -> None:
    """Durably sync the exact directory generation retained by ``pin``."""

    descriptor = -1
    try:
        descriptor = os.open(
            _directory_anchor(pin),
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        status = os.fstat(descriptor)
        if not stat.S_ISDIR(status.st_mode) or (status.st_dev, status.st_ino) != pin.identity:
            raise TemplateInstallError(
                f"the pinned template transaction directory changed: {logical_path}"
            )
        os.fsync(descriptor)
    except TemplateInstallError:
        raise
    except OSError as error:
        raise TemplateInstallError(
            f"cannot sync template transaction directory {logical_path}: {error}"
        ) from error
    finally:
        if descriptor >= 0:
            _close_owned_resources(
                [(_descriptor_closer(descriptor), f"the transaction directory {logical_path}")]
            )


def _validate_transaction_directory(directory: Path) -> file_io.PinnedPath:
    try:
        return file_io.pin_directory_path(
            directory,
            require_private=True,
        )
    except (OSError, ValueError) as error:
        raise TemplateInstallError(
            f"cannot safely open template transaction {directory}: {error}"
        ) from error


def _write_all(descriptor: int, document: bytes) -> None:
    offset = 0
    while offset < len(document):
        offset += os.write(descriptor, document[offset:])


def _reconcile_legacy_interrupted_transaction(path: Path) -> bool:
    """Resolve a legacy directory transaction without owning its container.

    The exchange invariant means ``path`` is always populated unless an
    external actor deletes it. Recovery either removes an app-owned staged
    generation, accepts a newer public writer, or atomically swaps a displaced
    pre-commit writer back. Ambiguous entries are all preserved. The directory
    itself is access-only: even successful recovery leaves its inode and public
    name untouched because its old creator could not atomically retain it.
    """

    directory = _transaction_directory(path)
    try:
        directory.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise _recovery_failure(path, directory, f"cannot inspect transaction: {error}") from error

    directory_pin: file_io.PinnedPath | None = None
    entries: list[_RecoveryEntry] = []
    preserved_directory = directory
    try:
        try:
            directory_pin = _validate_transaction_directory(directory)
            directory_access = _directory_anchor(directory_pin)
            allowed_names = {
                "lock",
                "record.json",
                "swap",
                file_io.RETAINED_ENTRY_DIRECTORY,
            }
            names = set(os.listdir(directory_access))
            if names.issubset({file_io.RETAINED_ENTRY_DIRECTORY}):
                return False
            if "lock" in names and names.issubset({"lock", file_io.RETAINED_ENTRY_DIRECTORY}):
                lock_path = directory / "lock"
                lock_access = directory_access / "lock"
                lock = _open_recovery_entry(lock_access, 0, logical_path=lock_path)
                if lock is None or lock.file_type != stat.S_IFREG or lock.document != b"":
                    raise TemplateInstallError(
                        "terminal transaction lock is not an empty regular file"
                    )
                entries.append(lock)
                lock_status = os.fstat(lock.descriptor)
                if (
                    lock_status.st_uid != os.getuid()
                    or stat.S_IMODE(lock_status.st_mode) != 0o600
                    or lock_status.st_nlink != 1
                ):
                    raise TemplateInstallError(
                        "terminal transaction lock has unsafe ownership or metadata"
                    )
                try:
                    fcntl.flock(lock.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise TemplateInstallError(
                        "another template recovery still owns the terminal lock; retry later"
                    ) from error
                return False
            if not {"lock", "record.json"}.issubset(names) or not names.issubset(allowed_names):
                raise TemplateInstallError(
                    f"transaction directory has unexpected contents: {sorted(names)!r}"
                )

            lock_path = directory / "lock"
            lock_access = directory_access / "lock"
            lock = _open_recovery_entry(lock_access, 0, logical_path=lock_path)
            if lock is None or lock.file_type != stat.S_IFREG or lock.document != b"":
                raise TemplateInstallError("transaction lock is not an empty regular file")
            entries.append(lock)
            lock_status = os.fstat(lock.descriptor)
            if (
                lock_status.st_uid != os.getuid()
                or stat.S_IMODE(lock_status.st_mode) != 0o600
                or lock_status.st_nlink != 1
            ):
                raise TemplateInstallError("transaction lock has unsafe ownership or metadata")
            try:
                fcntl.flock(lock.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise TemplateInstallError(
                    "another template installer still owns the transaction lock; retry later"
                ) from error

            # The lock was created before every other transaction entry. Once
            # held, another Wall-in-One process cannot advance this directory.
            names = set(os.listdir(directory_access))
            if not {"lock", "record.json"}.issubset(names) or not names.issubset(allowed_names):
                raise TemplateInstallError(
                    f"transaction directory changed while locking: {sorted(names)!r}"
                )

            record_path = directory / "record.json"
            record_access = directory_access / "record.json"
            record_entry = _open_recovery_entry(
                record_access,
                MAX_TRANSACTION_RECORD_BYTES,
                logical_path=record_path,
            )
            if record_entry is None:
                raise TemplateInstallError("transaction record disappeared")
            entries.append(record_entry)
            record = _parse_recovery_record(path, record_path, record_entry)
            if (
                record.candidate is not None
                or record.lock is not None
                or record.lock_fingerprint is not None
            ):
                raise TemplateInstallError("legacy transaction directory contains a current record")

            backup_path = path.parent / record.backup
            backup_entry = _open_recovery_entry(backup_path, MAX_NOCTALIA_SETTINGS_BYTES)
            if backup_entry is None:
                raise TemplateInstallError(f"recorded recovery backup is absent at {backup_path}")
            entries.append(backup_entry)
            if (
                backup_entry.file_type != stat.S_IFREG
                or backup_entry.document is None
                or len(backup_entry.document) != record.expected_size
                or hashlib.sha256(backup_entry.document).hexdigest() != record.expected_sha256
            ):
                raise TemplateInstallError(
                    "recorded recovery backup is not the expected regular document "
                    f"at {backup_path}"
                )

            public_entry = _open_recovery_entry(path, MAX_TRANSACTION_DOCUMENT_BYTES)
            if public_entry is not None:
                entries.append(public_entry)
            swap_path = directory / "swap"
            swap_access = directory_access / "swap"
            swap_entry = _open_recovery_entry(
                swap_access,
                MAX_TRANSACTION_DOCUMENT_BYTES,
                logical_path=swap_path,
            )
            if swap_entry is not None:
                entries.append(swap_entry)
            public_role = _recovery_role(public_entry, record)
            swap_role = _recovery_role(swap_entry, record)

            lock.verify_named()
            record_entry.verify_named()
            backup_entry.verify_named()

            if public_role == "absent":
                raise TemplateInstallError(
                    "canonical settings are absent, which this exchange transaction never causes"
                )

            if public_role == "candidate" and swap_role == "other":
                assert public_entry is not None
                assert swap_entry is not None
                public_entry.verify_named()
                swap_entry.verify_named()
                _rename_exchange(swap_access, path)
                # The displaced writer is public again and our exact candidate
                # is the only generation recovery is authorized to discard.
                swap_entry.verify_identity_at(path, logical_path=path)
                public_entry.verify_relocated_document(
                    swap_access,
                    logical_path=swap_path,
                )
                _retain_recovery_candidate_intact(
                    swap_access,
                    public_entry,
                    logical_path=swap_path,
                    retained_parent=path.parent,
                    logical_retained_parent=path.parent,
                )
                _fsync_pinned_directory(directory_pin, directory)
                _fsync_parent(path)
            elif swap_role == "candidate" and public_role in {"expected", "other"}:
                assert swap_entry is not None
                _retain_recovery_candidate_intact(
                    swap_access,
                    swap_entry,
                    logical_path=swap_path,
                    retained_parent=path.parent,
                    logical_retained_parent=path.parent,
                )
                _fsync_pinned_directory(directory_pin, directory)
            elif swap_role == "expected" and public_role == "candidate":
                assert swap_entry is not None
                _preserve_regular_at_backup(
                    swap_access,
                    backup_path,
                    expected_identity=swap_entry.identity,
                    expected_fingerprint=swap_entry.fingerprint,
                )
                _fsync_pinned_directory(directory_pin, directory)
            elif swap_role == "expected" and public_role == "other":
                assert public_entry is not None
                assert swap_entry is not None
                public_entry.verify_named()
                swap_entry.verify_named(logical_path=swap_path)
                _rename_exchange(swap_access, path)
                swap_entry.verify_relocated_document(path, logical_path=path)
                public_entry.verify_identity_at(swap_access, logical_path=swap_path)
                _fsync_pinned_directory(directory_pin, directory)
                _fsync_parent(path)
                raise TemplateInstallError(
                    "the original settings were restored; the conflicting entry "
                    f"remains preserved at {swap_path}"
                )
            elif swap_role == "absent" and public_role in {"candidate", "expected", "other"}:
                pass
            else:
                raise TemplateInstallError(
                    f"ambiguous public/swap generations ({public_role}/{swap_role})"
                )

            record_entry.verify_named(logical_path=record_path)
            lock.verify_named(logical_path=lock_path)
            _retain_recovery_record_intact(
                record_access,
                record_entry,
                logical_path=record_path,
                retained_parent=path.parent,
                logical_retained_parent=path.parent,
            )
            _fsync_pinned_directory(directory_pin, directory)
            _discard_recovery_regular(
                lock_access,
                lock,
                logical_path=lock_path,
                retained_parent=path.parent,
                logical_retained_parent=path.parent,
            )
            _fsync_pinned_directory(directory_pin, directory)
        except TemplateInstallError as error:
            raise _recovery_failure(path, preserved_directory, str(error)) from error
        except OSError as error:
            raise _recovery_failure(
                path,
                preserved_directory,
                f"filesystem operation failed: {error}",
            ) from error
    finally:
        resources: list[tuple[Callable[[], None], str]] = [
            (entry.close, f"the recovery entry {entry.path}") for entry in reversed(entries)
        ]
        if directory_pin is not None:
            resources.append((directory_pin.close, f"the transaction directory {directory}"))
        _close_owned_resources(resources)
    return True


def _reconcile_locator_transaction(path: Path) -> bool:
    """Resolve one current regular-file transaction from its durable locator."""

    locator = _transaction_locator(path)
    try:
        locator.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise _recovery_failure(path, locator, f"cannot inspect transaction: {error}") from error

    entries: list[_RecoveryEntry] = []
    try:
        try:
            record_entry = _open_recovery_entry(locator, MAX_TRANSACTION_RECORD_BYTES)
            if record_entry is None:
                raise TemplateInstallError("transaction locator disappeared")
            entries.append(record_entry)
            record = _parse_recovery_record(path, locator, record_entry)
            if record.candidate is None or record.lock is None:
                raise TemplateInstallError("the fixed transaction locator uses a legacy record")
            record_status = os.fstat(record_entry.descriptor)
            if (
                record_status.st_uid != os.getuid()
                or stat.S_IMODE(record_status.st_mode) != 0o600
                or record_status.st_nlink != 1
            ):
                raise TemplateInstallError("transaction locator has unsafe ownership or metadata")

            lock_path = path.parent / record.lock
            lock = _open_recovery_entry(lock_path, 0)
            if lock is None or lock.file_type != stat.S_IFREG or lock.document != b"":
                raise TemplateInstallError("transaction lock is not an empty regular file")
            entries.append(lock)
            lock_status = os.fstat(lock.descriptor)
            if (
                lock_status.st_uid != os.getuid()
                or stat.S_IMODE(lock_status.st_mode) != 0o600
                or lock_status.st_nlink != 1
                or record.lock_fingerprint is None
                or lock.fingerprint != record.lock_fingerprint
            ):
                raise TemplateInstallError(
                    "transaction lock does not match the recorded private generation"
                )
            try:
                fcntl.flock(lock.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise TemplateInstallError(
                    "another template installer still owns the transaction lock; retry later"
                ) from error
            record_entry.verify_named()

            backup_path = path.parent / record.backup
            backup_entry = _open_recovery_entry(backup_path, MAX_NOCTALIA_SETTINGS_BYTES)
            if backup_entry is None:
                raise TemplateInstallError(f"recorded recovery backup is absent at {backup_path}")
            entries.append(backup_entry)
            if (
                backup_entry.file_type != stat.S_IFREG
                or backup_entry.document is None
                or len(backup_entry.document) != record.expected_size
                or hashlib.sha256(backup_entry.document).hexdigest() != record.expected_sha256
            ):
                raise TemplateInstallError(
                    "recorded recovery backup is not the expected regular document "
                    f"at {backup_path}"
                )

            public_entry = _open_recovery_entry(path, MAX_TRANSACTION_DOCUMENT_BYTES)
            if public_entry is not None:
                entries.append(public_entry)
            swap_path = path.parent / record.candidate
            swap_entry = _open_recovery_entry(swap_path, MAX_TRANSACTION_DOCUMENT_BYTES)
            if swap_entry is not None:
                entries.append(swap_entry)
            public_role = _recovery_role(public_entry, record)
            swap_role = _recovery_role(swap_entry, record)

            lock.verify_named()
            record_entry.verify_named()
            backup_entry.verify_named()
            if public_role == "absent":
                raise TemplateInstallError(
                    "canonical settings are absent, which this exchange transaction never causes"
                )

            if public_role == "candidate" and swap_role == "other":
                assert public_entry is not None
                assert swap_entry is not None
                public_entry.verify_named()
                swap_entry.verify_named()
                _rename_exchange(swap_path, path)
                swap_entry.verify_identity_at(path, logical_path=path)
                public_entry.verify_relocated_document(swap_path)
                _retain_recovery_candidate_intact(
                    swap_path,
                    public_entry,
                    retained_parent=path.parent,
                    logical_retained_parent=path.parent,
                )
                _fsync_parent(path)
            elif swap_role == "candidate" and public_role in {"expected", "other"}:
                assert swap_entry is not None
                _retain_recovery_candidate_intact(
                    swap_path,
                    swap_entry,
                    retained_parent=path.parent,
                    logical_retained_parent=path.parent,
                )
                _fsync_parent(swap_path)
            elif swap_role == "expected" and public_role == "candidate":
                assert swap_entry is not None
                _preserve_regular_at_backup(
                    swap_path,
                    backup_path,
                    expected_identity=swap_entry.identity,
                    expected_fingerprint=swap_entry.fingerprint,
                )
            elif swap_role == "expected" and public_role == "other":
                assert public_entry is not None
                assert swap_entry is not None
                public_entry.verify_named()
                swap_entry.verify_named()
                _rename_exchange(swap_path, path)
                swap_entry.verify_relocated_document(path)
                public_entry.verify_identity_at(swap_path)
                _fsync_parent(path)
                raise TemplateInstallError(
                    "the original settings were restored; the conflicting entry "
                    f"remains preserved at {swap_path}"
                )
            elif swap_role == "absent" and public_role in {"candidate", "expected", "other"}:
                pass
            else:
                raise TemplateInstallError(
                    f"ambiguous public/swap generations ({public_role}/{swap_role})"
                )

            record_entry.verify_named()
            _retain_recovery_record_intact(
                locator,
                record_entry,
                retained_parent=path.parent,
                logical_retained_parent=path.parent,
            )
            _fsync_parent(locator)
            lock.verify_named()
            _discard_recovery_regular(
                lock_path,
                lock,
                retained_parent=path.parent,
                logical_retained_parent=path.parent,
            )
            _fsync_parent(lock_path)
        except TemplateInstallError as error:
            raise _recovery_failure(path, locator, str(error)) from error
        except OSError as error:
            raise _recovery_failure(
                path,
                locator,
                f"filesystem operation failed: {error}",
            ) from error
    finally:
        _close_owned_resources(
            [(entry.close, f"the recovery entry {entry.path}") for entry in reversed(entries)]
        )
    return True


def _reconcile_interrupted_transaction(path: Path) -> bool:
    """Reconcile current locator transactions and legacy directory records."""

    current = _reconcile_locator_transaction(path)
    legacy = _reconcile_legacy_interrupted_transaction(path)
    return current or legacy


def _begin_publication_transaction(
    path: Path,
    candidate_document: bytes,
    expected: _SettingsSnapshot,
    backup: Path,
) -> _PublicationTransaction:
    """Durably stage descriptor-owned regular files and publish their locator."""

    final_record = _transaction_locator(path)
    candidate_descriptor = -1
    record_descriptor = -1
    lock_descriptor = -1
    swap: Path | None = None
    record: Path | None = None
    lock: Path | None = None
    candidate_identity: file_io.PathIdentity | None = None
    candidate_fingerprint: file_io.FileFingerprint | None = None
    record_identity: file_io.PathIdentity | None = None
    record_fingerprint: file_io.FileFingerprint | None = None
    lock_identity: file_io.PathIdentity | None = None
    lock_fingerprint: file_io.FileFingerprint | None = None
    try:
        lock_descriptor, lock_name = tempfile.mkstemp(
            prefix=_transaction_leaf_prefix(path, "lock"),
            dir=path.parent,
        )
        lock = Path(lock_name)
        lock_status = os.fstat(lock_descriptor)
        lock_identity = lock_status.st_dev, lock_status.st_ino
        lock_fingerprint = file_io.file_fingerprint(lock_status)
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.fsync(lock_descriptor)
        lock_fingerprint = file_io.file_fingerprint(os.fstat(lock_descriptor))
        assert lock_fingerprint is not None

        candidate_descriptor, candidate_name = tempfile.mkstemp(
            prefix=_transaction_leaf_prefix(path, "candidate"),
            dir=path.parent,
        )
        swap = Path(candidate_name)
        candidate_status = os.fstat(candidate_descriptor)
        candidate_identity = candidate_status.st_dev, candidate_status.st_ino
        _write_all(candidate_descriptor, candidate_document)
        os.fsync(candidate_descriptor)
        candidate_status = os.fstat(candidate_descriptor)
        candidate_fingerprint = file_io.file_fingerprint(candidate_status)

        recovery_record = json.dumps(
            {
                "backup": backup.name,
                "candidate": swap.name,
                "candidate_device": candidate_identity[0],
                "candidate_inode": candidate_identity[1],
                "candidate_sha256": hashlib.sha256(candidate_document).hexdigest(),
                "candidate_size": len(candidate_document),
                "expected_device": expected.device,
                "expected_inode": expected.inode,
                "expected_sha256": hashlib.sha256(expected.document).hexdigest(),
                "expected_size": len(expected.document),
                "lock": lock.name,
                "lock_ctime_ns": lock_fingerprint[4],
                "lock_device": lock_fingerprint[0],
                "lock_inode": lock_fingerprint[1],
                "lock_mtime_ns": lock_fingerprint[3],
                "lock_size": lock_fingerprint[2],
                "version": 3,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        record_descriptor, record_name = tempfile.mkstemp(
            prefix=_transaction_leaf_prefix(path, "locator-stage"),
            dir=path.parent,
        )
        record = Path(record_name)
        record_status = os.fstat(record_descriptor)
        record_identity = record_status.st_dev, record_status.st_ino
        _write_all(record_descriptor, recovery_record)
        os.fsync(record_descriptor)
        record_status = os.fstat(record_descriptor)
        record_fingerprint = file_io.file_fingerprint(record_status)
        _fsync_parent(record)
        file_io.atomic_move_no_replace(
            record,
            final_record,
            expected_identity=record_identity,
            expected_file_type=stat.S_IFREG,
            expected_fingerprint=record_fingerprint,
            externally_pinned=True,
        )
        record = final_record
        record_status = os.fstat(record_descriptor)
        named_record = record.lstat()
        if (
            not stat.S_ISREG(record_status.st_mode)
            or not stat.S_ISREG(named_record.st_mode)
            or (record_status.st_dev, record_status.st_ino) != record_identity
            or (named_record.st_dev, named_record.st_ino) != record_identity
            or file_io.file_fingerprint(record_status) != file_io.file_fingerprint(named_record)
        ):
            raise file_io.PathChangedError(
                f"the template transaction locator {record} changed while being published"
            )
        record_fingerprint = file_io.file_fingerprint(record_status)
        _fsync_parent(record)
    except OSError as error:
        cleanup_remainders: list[Path] = []
        cleanup_sync_error: OSError | None = None
        if isinstance(error, file_io.PathChangedError) and error.preserved_path is not None:
            cleanup_remainders.append(error.preserved_path)
        locator_publication_is_uncertain = (
            isinstance(error, file_io.PathChangedError)
            and error.preserved_path == final_record
            and record != final_record
        )
        try:
            if candidate_descriptor >= 0:
                with contextlib.suppress(OSError, ValueError):
                    candidate_status = os.fstat(candidate_descriptor)
                    if candidate_identity is None:
                        candidate_identity = candidate_status.st_dev, candidate_status.st_ino
                    candidate_fingerprint = file_io.file_fingerprint(candidate_status)
            if record_descriptor >= 0:
                with contextlib.suppress(OSError, ValueError):
                    record_status = os.fstat(record_descriptor)
                    if record_identity is None:
                        record_identity = record_status.st_dev, record_status.st_ino
                    record_fingerprint = file_io.file_fingerprint(record_status)
            if lock_descriptor >= 0:
                with contextlib.suppress(OSError, ValueError):
                    lock_status = os.fstat(lock_descriptor)
                    if lock_identity is None:
                        lock_identity = lock_status.st_dev, lock_status.st_ino
                    lock_fingerprint = file_io.file_fingerprint(lock_status)

            locator_was_published = record == final_record

            def cleanup_owned_regular(
                regular_path: Path | None,
                identity: file_io.PathIdentity | None,
                fingerprint: file_io.FileFingerprint | None,
                *,
                retain_intact: bool = False,
            ) -> bool:
                if regular_path is None:
                    return True
                if identity is None:
                    if os.path.lexists(regular_path):
                        cleanup_remainders.append(regular_path)
                        return False
                    return True
                try:
                    if retain_intact:
                        if fingerprint is None:
                            raise ValueError("authoritative entry fingerprint is unavailable")
                        _retain_regular_intact(
                            regular_path,
                            expected_identity=identity,
                            expected_fingerprint=fingerprint,
                            retained_parent=path.parent,
                            logical_retained_parent=path.parent,
                            logical_path=regular_path,
                        )
                        removed = True
                    else:
                        removed = file_io.discard_regular_if_same(
                            regular_path,
                            expected_identity=identity,
                            expected_fingerprint=fingerprint,
                            retained_parent=path.parent,
                            logical_retained_parent=path.parent,
                        )
                except OSError, ValueError:
                    removed = False
                if not removed and os.path.lexists(regular_path):
                    cleanup_remainders.append(regular_path)
                return removed

            if locator_publication_is_uncertain:
                # The no-replace move reached its destination, but its
                # post-move verification could not prove whether the exact
                # record or a racing replacement remains there. The fixed
                # locator may therefore be authoritative. Preserve every
                # dependency so either the valid record can replay or the
                # conflicting evidence can be inspected without a wedge.
                for preserved in (record, final_record, swap, lock):
                    if preserved is not None and os.path.lexists(preserved):
                        cleanup_remainders.append(preserved)
            elif locator_was_published:
                candidate_retired = cleanup_owned_regular(
                    swap,
                    candidate_identity,
                    candidate_fingerprint,
                    retain_intact=True,
                )
                may_retire_locator = candidate_retired
                if candidate_retired:
                    try:
                        _fsync_parent(path)
                    except OSError as sync_error:
                        cleanup_sync_error = sync_error
                        may_retire_locator = False

                record_retired = False
                if may_retire_locator:
                    record_retired = cleanup_owned_regular(
                        record,
                        record_identity,
                        record_fingerprint,
                        retain_intact=True,
                    )
                    if record_retired:
                        try:
                            _fsync_parent(path)
                        except OSError as sync_error:
                            cleanup_sync_error = sync_error
                            record_retired = False

                if record_retired:
                    cleanup_owned_regular(lock, lock_identity, lock_fingerprint)
                    try:
                        _fsync_parent(path)
                    except OSError as sync_error:
                        if cleanup_sync_error is None:
                            cleanup_sync_error = sync_error
                else:
                    for preserved in (record, swap, lock):
                        if preserved is not None and os.path.lexists(preserved):
                            cleanup_remainders.append(preserved)
            else:
                cleanup_owned_regular(record, record_identity, record_fingerprint)
                cleanup_owned_regular(swap, candidate_identity, candidate_fingerprint)
                cleanup_owned_regular(lock, lock_identity, lock_fingerprint)
                try:
                    _fsync_parent(path)
                except OSError as sync_error:
                    if cleanup_sync_error is None:
                        cleanup_sync_error = sync_error
        finally:
            resources: list[tuple[Callable[[], None], str]] = []
            for descriptor, purpose in (
                (record_descriptor, "the template transaction record"),
                (candidate_descriptor, "the staged settings candidate"),
                (lock_descriptor, "the template transaction lock"),
            ):
                if descriptor >= 0:
                    resources.append((_descriptor_closer(descriptor), purpose))
            _close_owned_resources(resources)
        remainder = ""
        if cleanup_remainders:
            unique_remainders = tuple(dict.fromkeys(cleanup_remainders))
            remainder = "; cleanup entries remain at " + ", ".join(
                str(candidate) for candidate in unique_remainders
            )
        if cleanup_sync_error is not None:
            remainder += f"; cleanup directory could not be synced: {cleanup_sync_error}"
        raise TemplateInstallError(
            f"cannot stage template transaction for {path}: {error}{remainder}"
        ) from error

    assert candidate_identity is not None
    assert candidate_fingerprint is not None
    assert record_identity is not None
    assert record_fingerprint is not None
    assert lock_identity is not None
    assert lock_fingerprint is not None
    assert swap is not None
    assert record is not None
    assert lock is not None
    return _PublicationTransaction(
        swap=swap,
        record=record,
        backup=backup,
        candidate_document=candidate_document,
        candidate_identity=candidate_identity,
        candidate_fingerprint=candidate_fingerprint,
        record_identity=record_identity,
        record_fingerprint=record_fingerprint,
        lock=lock,
        lock_identity=lock_identity,
        lock_fingerprint=lock_fingerprint,
        _candidate_descriptor=candidate_descriptor,
        _record_descriptor=record_descriptor,
        _lock_descriptor=lock_descriptor,
    )


def _discard_candidate_if_owned(
    temporary: Path,
    candidate_identity: file_io.PathIdentity | None,
    *,
    expected_fingerprint: file_io.FileFingerprint | None = None,
    retained_parent: Path | None = None,
    logical_retained_parent: Path | None = None,
) -> bool:
    """Remove only the exact staged candidate, never a replacement at its name."""

    if candidate_identity is None:
        return True
    try:
        return file_io.discard_regular_if_same(
            temporary,
            expected_identity=candidate_identity,
            expected_fingerprint=expected_fingerprint,
            retained_parent=retained_parent,
            logical_retained_parent=(
                retained_parent if logical_retained_parent is None else logical_retained_parent
            ),
        )
    except OSError:
        return False


def _entry_snapshot_fingerprint(
    path: Path,
    expected: _SettingsSnapshot,
) -> file_io.FileFingerprint | None:
    try:
        return _assert_claim_unchanged(path, expected)
    except TemplateInstallError:
        return None


def _entry_candidate_fingerprint(
    path: Path,
    transaction: _PublicationTransaction,
) -> file_io.FileFingerprint | None:
    try:
        return _assert_candidate_unchanged(
            path,
            transaction._candidate_descriptor,
            transaction.candidate_identity,
            transaction.candidate_document,
        )
    except TemplateInstallError:
        return None


def _preserve_regular_at_backup(
    source: Path,
    backup_copy: Path,
    *,
    expected_identity: file_io.PathIdentity,
    expected_fingerprint: file_io.FileFingerprint,
) -> Path:
    """Move a displaced settings inode to a durable, no-replace backup name."""

    with file_io.pin_regular_path(
        source,
        expected_identity=expected_identity,
        expected_fingerprint=expected_fingerprint,
    ) as pin:
        for index in range(10_000):
            suffix = "" if index == 0 else f".{index}"
            destination = backup_copy.with_name(f"{backup_copy.name}.original{suffix}")
            try:
                file_io.atomic_move_no_replace(
                    source,
                    destination,
                    expected_identity=expected_identity,
                    expected_file_type=stat.S_IFREG,
                    expected_fingerprint=expected_fingerprint,
                    pinned_source=pin,
                )
            except FileExistsError:
                continue
            try:
                _fsync_parent(destination)
            except OSError as error:
                raise TemplateInstallError(
                    f"displaced settings were preserved at {destination}, "
                    f"but its directory could not be synced: {error}"
                ) from error
            return destination
    raise TemplateInstallError(
        f"cannot preserve displaced settings beside {backup_copy}: too many backup names"
    )


def _cleanup_publication_transaction(
    transaction: _PublicationTransaction,
    *,
    discard_candidate: bool,
) -> Path | None:
    """Clean owned entries; return the discoverable path to any remainder."""

    candidate_fingerprint: file_io.FileFingerprint | None = None
    if discard_candidate:
        candidate_fingerprint = _entry_candidate_fingerprint(transaction.swap, transaction)
        if candidate_fingerprint is None:
            return transaction.swap
        candidate_retired = False
        with contextlib.suppress(OSError, ValueError):
            _retain_regular_intact(
                transaction.swap,
                expected_identity=transaction.candidate_identity,
                expected_fingerprint=candidate_fingerprint,
                retained_parent=transaction.record.parent,
                logical_retained_parent=transaction.record.parent,
                logical_path=transaction.swap,
            )
            candidate_retired = True
        if not candidate_retired:
            return transaction.swap
        _fsync_parent(transaction.swap)
    elif os.path.lexists(transaction.swap):
        return transaction.swap

    record_removed = False
    lock_removed = False
    with contextlib.suppress(OSError):
        _retain_regular_intact(
            transaction.record,
            expected_identity=transaction.record_identity,
            expected_fingerprint=transaction.record_fingerprint,
            retained_parent=transaction.record.parent,
            logical_retained_parent=transaction.record.parent,
            logical_path=transaction.record,
        )
        record_removed = True
    if not record_removed:
        return transaction.record
    _fsync_parent(transaction.record)
    with contextlib.suppress(OSError):
        lock_removed = file_io.discard_regular_if_same(
            transaction.lock,
            expected_identity=transaction.lock_identity,
            expected_fingerprint=transaction.lock_fingerprint,
            retained_parent=transaction.lock.parent,
            logical_retained_parent=transaction.lock.parent,
        )
    if not lock_removed:
        return transaction.lock
    _fsync_parent(transaction.lock)
    return None


def _resolve_failed_exchange(
    path: Path,
    expected: _SettingsSnapshot,
    transaction: _PublicationTransaction,
) -> tuple[str, bool]:
    """Preserve the latest public writer and recover a pre-exchange winner."""

    old_fingerprint = _entry_snapshot_fingerprint(transaction.swap, expected)
    candidate_fingerprint = _entry_candidate_fingerprint(path, transaction)
    old_is_relocated = old_fingerprint is not None
    candidate_is_public = candidate_fingerprint is not None

    if candidate_is_public and not old_is_relocated:
        # Another writer won immediately before our exchange. Swap it back in
        # one atomic operation, so even recovery never creates an absent public
        # settings pathname. A crash on either side leaves one valid document
        # public and the other discoverable at the fixed transaction path.
        try:
            _rename_exchange(transaction.swap, path)
        except OSError as error:
            return (
                f"the concurrent entry remains preserved at {transaction.swap}: {error}",
                False,
            )
        relocated_candidate = _entry_candidate_fingerprint(transaction.swap, transaction)
        if relocated_candidate is None:
            return (
                "a newer concurrent exchange occurred; both entries remain preserved "
                f"at {path} and {transaction.swap}",
                False,
            )
        try:
            _retain_regular_intact(
                transaction.swap,
                expected_identity=transaction.candidate_identity,
                expected_fingerprint=relocated_candidate,
                retained_parent=transaction.record.parent,
                logical_retained_parent=transaction.record.parent,
                logical_path=transaction.swap,
            )
        except OSError, ValueError:
            return (
                f"the candidate remains preserved at {transaction.swap}",
                False,
            )
        _fsync_parent(path)
        return (f"the concurrent entry was restored to {path}", True)

    if old_is_relocated and not candidate_is_public:
        # This state is ambiguous: either the random staging name was replaced
        # just before the exchange, or a public writer superseded our candidate
        # just after it. Keeping the current public entry would let the former
        # case transplant an unrelated staging replacement into canonical
        # settings. Restore the exact original atomically in both cases and
        # leave the conflicting entry discoverable at the recorded swap name.
        try:
            _rename_exchange(transaction.swap, path)
        except OSError as error:
            return (
                f"the ambiguous entries remain preserved at {path} and {transaction.swap}: {error}",
                False,
            )
        restored_fingerprint = _entry_snapshot_fingerprint(path, expected)
        if restored_fingerprint is None:
            return (
                "a newer concurrent exchange occurred; both entries remain preserved "
                f"at {path} and {transaction.swap}",
                False,
            )
        try:
            _fsync_parent(path)
        except OSError as error:
            return (
                f"the original settings were restored, but their directory could not "
                f"be synced; the conflicting entry remains at {transaction.swap}: {error}",
                False,
            )
        return (
            f"the original settings were restored; the conflicting entry remains "
            f"preserved at {transaction.swap}",
            True,
        )

    return (
        f"the ambiguous exchange entries remain preserved at {path} and {transaction.swap}",
        False,
    )


def _write_atomically(path: Path, text: str, expected: _SettingsSnapshot) -> Path:
    """Publish through one always-present atomic exchange.

    The backup is a synced byte copy, so creating it never removes the public
    settings pathname. A random candidate and lock are synced as descriptor-
    owned regular siblings, then a regular locator/record is atomically
    published at its fixed recovery name. ``RENAME_EXCHANGE`` is the only
    commit point: before it, the original is public; after it, the candidate is
    public and the displaced entry remains named at ``transaction.swap``.
    """

    candidate_document = text.encode("utf-8")
    backup = _backup(path, expected)
    transaction = _begin_publication_transaction(
        path,
        candidate_document,
        expected,
        backup,
    )
    exchanged = False
    try:
        try:
            _assert_claim_unchanged(path, expected)
            _assert_candidate_unchanged(
                transaction.swap,
                transaction._candidate_descriptor,
                transaction.candidate_identity,
                transaction.candidate_document,
            )
            _assert_regular_entry_unchanged(
                transaction.record,
                transaction._record_descriptor,
                transaction.record_fingerprint,
                role="transaction locator",
            )
            _assert_regular_entry_unchanged(
                transaction.lock,
                transaction._lock_descriptor,
                transaction.lock_fingerprint,
                role="transaction lock",
            )
        except TemplateInstallError as error:
            raise TemplateInstallError(
                f"{path} changed while the template edit was being prepared; "
                f"settings were not replaced ({error})"
            ) from error
        try:
            _rename_exchange(transaction.swap, path)
            exchanged = True
        except OSError as error:
            raise TemplateInstallError(f"cannot atomically exchange {path}: {error}") from error

        old_fingerprint = _entry_snapshot_fingerprint(transaction.swap, expected)
        candidate_fingerprint = _entry_candidate_fingerprint(path, transaction)
        old_is_relocated = old_fingerprint is not None
        candidate_is_public = candidate_fingerprint is not None
        if not (old_is_relocated and candidate_is_public):
            recovery, resolved = _resolve_failed_exchange(
                path,
                expected,
                transaction,
            )
            if resolved:
                remainder = _cleanup_publication_transaction(
                    transaction,
                    discard_candidate=False,
                )
                if remainder is not None:
                    recovery = f"{recovery}; recovery cleanup remains at {remainder}"
            raise TemplateInstallError(
                f"{path} changed during atomic template publication; "
                f"settings were not replaced; {recovery}"
            )

        assert old_fingerprint is not None
        try:
            displaced_backup = _preserve_regular_at_backup(
                transaction.swap,
                backup,
                expected_identity=(expected.device, expected.inode),
                expected_fingerprint=old_fingerprint,
            )
        except OSError as error:
            raise TemplateInstallError(
                f"{path} was updated, but its displaced settings remain at "
                f"{transaction.swap}: {error}"
            ) from error
        remainder = _cleanup_publication_transaction(transaction, discard_candidate=False)
        if remainder is not None:
            raise TemplateInstallError(
                f"{path} was updated, but its recovery cleanup remains at {remainder}"
            )
        return displaced_backup
    except OSError as error:
        raise TemplateInstallError(f"cannot write {path}: {error}") from error
    finally:
        try:
            if not exchanged:
                _cleanup_publication_transaction(transaction, discard_candidate=True)
        finally:
            transaction.close()


def _verify_and_sync_installed_template(path: Path, document: bytes) -> None:
    """Pin, verify and durably sync an existing content-addressed template."""

    parent_descriptor = -1
    descriptor = -1
    try:
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        parent_status = os.fstat(parent_descriptor)
        named_parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent_status.st_mode)
            or not stat.S_ISDIR(named_parent.st_mode)
            or (parent_status.st_dev, parent_status.st_ino)
            != (named_parent.st_dev, named_parent.st_ino)
        ):
            raise TemplateInstallError(f"template destination parent changed: {path.parent}")
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        named = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise TemplateInstallError(
                f"content-addressed palette template is not one exact regular file: {path}"
            )
        chunks: list[bytes] = []
        offset = 0
        remaining = len(document) + 1
        while remaining:
            chunk = os.pread(descriptor, min(remaining, 64 * 1024), offset)
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
            remaining -= len(chunk)
        after_read = os.fstat(descriptor)
        named_after_read = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            b"".join(chunks) != document
            or file_io.file_fingerprint(opened) != file_io.file_fingerprint(after_read)
            or file_io.file_fingerprint(after_read) != file_io.file_fingerprint(named_after_read)
        ):
            raise TemplateInstallError(
                f"content-addressed palette template has conflicting bytes: {path}"
            )
        os.fsync(descriptor)
        os.fsync(parent_descriptor)
        final = os.fstat(descriptor)
        final_named = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        current_parent = path.parent.lstat()
        if (
            file_io.file_fingerprint(final) != file_io.file_fingerprint(final_named)
            or file_io.file_fingerprint(final) != file_io.file_fingerprint(after_read)
            or (current_parent.st_dev, current_parent.st_ino)
            != (parent_status.st_dev, parent_status.st_ino)
        ):
            raise TemplateInstallError(
                f"content-addressed palette template changed while syncing: {path}"
            )
    except TemplateInstallError:
        raise
    except OSError as error:
        raise TemplateInstallError(f"cannot verify palette template {path}: {error}") from error
    finally:
        resources: list[tuple[Callable[[], None], str]] = []
        if descriptor >= 0:
            resources.append((_descriptor_closer(descriptor), f"the installed template {path}"))
        if parent_descriptor >= 0:
            resources.append(
                (_descriptor_closer(parent_descriptor), f"the template parent {path.parent}")
            )
        _close_owned_resources(resources)


def _write_bytes_atomically(path: Path, document: bytes) -> None:
    """Publish one immutable content-addressed template from an unnamed inode."""

    descriptor = -1
    parent_descriptor = -1
    try:
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        parent_status = os.fstat(parent_descriptor)
        named_parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent_status.st_mode)
            or not stat.S_ISDIR(named_parent.st_mode)
            or (parent_status.st_dev, parent_status.st_ino)
            != (named_parent.st_dev, named_parent.st_ino)
        ):
            raise TemplateInstallError(f"template destination parent changed: {path.parent}")

        descriptor = os.open(
            ".",
            os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_descriptor,
        )
        created = os.fstat(descriptor)
        candidate_identity = created.st_dev, created.st_ino
        _write_all(descriptor, document)
        os.fsync(descriptor)
        os.link(
            f"/proc/self/fd/{descriptor}",
            path.name,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=True,
        )
        after = os.fstat(descriptor)
        named = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        current_parent = path.parent.lstat()
        if (
            not stat.S_ISREG(after.st_mode)
            or not stat.S_ISREG(named.st_mode)
            or (after.st_dev, after.st_ino) != candidate_identity
            or (named.st_dev, named.st_ino) != candidate_identity
            or file_io.file_fingerprint(after) != file_io.file_fingerprint(named)
            or (current_parent.st_dev, current_parent.st_ino)
            != (parent_status.st_dev, parent_status.st_ino)
        ):
            raise TemplateInstallError(
                f"the content-addressed palette template changed while publishing: {path}"
            )
        os.fsync(parent_descriptor)
    except FileExistsError as error:
        raise TemplateInstallError(
            f"refusing to replace existing content-addressed palette template {path}"
        ) from error
    except TemplateInstallError:
        raise
    except OSError as error:
        raise TemplateInstallError(f"cannot write {path}: {error}") from error
    finally:
        resources: list[tuple[Callable[[], None], str]] = []
        if descriptor >= 0:
            resources.append((_descriptor_closer(descriptor), "the unnamed palette template"))
        if parent_descriptor >= 0:
            resources.append(
                (_descriptor_closer(parent_descriptor), f"the template parent {path.parent}")
            )
        _close_owned_resources(resources)


def _post_hook_command() -> str:
    """The command Noctalia runs after each render.

    Resolved to an absolute path when possible, because Noctalia's hook runs
    with its own environment and may not share our PATH.
    """
    found = shutil.which("wall-in-one")
    executable = found if found else "wall-in-one"
    return shlex.join((executable, "ctl", "reload-palette"))


def install(*, reload_config: bool = True) -> InstallResult:
    """Register the template, copying it to a stable location first."""
    settings_path = paths.noctalia_settings_path()
    recovered = _reconcile_interrupted_transaction(settings_path)
    with _read_settings_snapshot(settings_path) as snapshot:
        try:
            settings = tomllib.loads(snapshot.text)
        except (tomllib.TOMLDecodeError, RecursionError) as error:
            raise TemplateInstallError(f"{settings_path} is not valid TOML: {error}") from error

        source = bundled_template()
        output_path = paths.palette_path()

        try:
            source_document = file_io.read_regular_bytes(source, MAX_TEMPLATE_BYTES)
        except OSError as error:
            raise TemplateInstallError(f"cannot safely read palette template: {error}") from error
        if source_document is None:
            raise TemplateInstallError(f"palette template disappeared: {source}")
        destination = installed_template_path(source_document)
        _ensure_durable_directory(destination.parent)
        try:
            destination_document = file_io.read_regular_bytes(destination, MAX_TEMPLATE_BYTES)
        except OSError as error:
            raise TemplateInstallError(f"cannot safely read palette template: {error}") from error
        if destination_document is not None and destination_document != source_document:
            raise TemplateInstallError(
                f"refusing to replace existing content-addressed palette template {destination}"
            )
        template_changed = destination_document is None
        if template_changed:
            try:
                _write_bytes_atomically(destination, source_document)
            except TemplateInstallError:
                # A concurrent installer may have linked and synced the same
                # immutable document after our absence check. Converge only
                # after independently pinning and durably verifying it.
                _verify_and_sync_installed_template(destination, source_document)
                template_changed = False
        _verify_and_sync_installed_template(destination, source_document)

        block = _render_block(destination, output_path, _post_hook_command())
        existing = _existing_entry(settings)

        if existing is not None:
            matches = (
                existing.get("enabled") is True
                and existing.get("input_path") == str(destination)
                and existing.get("output_path") in (str(output_path), [str(output_path)])
            )
            if matches and not template_changed:
                if recovered and reload_config:
                    with contextlib.suppress(noctalia.NoctaliaError):
                        noctalia.reload_config()
                return InstallResult(
                    changed=False,
                    settings_path=settings_path,
                    template_path=destination,
                    output_path=output_path,
                    backup_path=None,
                    detail="already registered",
                )
            # Rewriting an entry we do not provably own risks clobbering a hand-
            # edited one, so leave it and say what to fix.
            if _BEGIN_MARKER not in snapshot.text:
                raise TemplateInstallError(
                    f"[theme.templates.user.{TEMPLATE_ID}] already exists in {settings_path} "
                    "but was not written by us; remove it by hand and re-run"
                )

        original = snapshot.text
        if _BEGIN_MARKER in original:
            updated = _replace_managed_block(original, block)
        else:
            separator = (
                "" if original.endswith("\n\n") else ("\n" if original.endswith("\n") else "\n\n")
            )
            updated = f"{original}{separator}{block}\n"

        settings_changed = updated != original
        backup = _write_atomically(settings_path, updated, snapshot) if settings_changed else None

        if reload_config:
            # Not fatal if this fails: the settings file is already correct and
            # Noctalia will pick it up on its next start. Only immediacy is lost.
            with contextlib.suppress(noctalia.NoctaliaError):
                noctalia.reload_config()

        return InstallResult(
            changed=True,
            settings_path=settings_path,
            template_path=destination,
            output_path=output_path,
            backup_path=backup,
            detail=(
                "registered"
                if existing is None
                else "updated"
                if settings_changed
                else "template updated; registration unchanged"
            ),
        )


def _replace_managed_block(text: str, block: str) -> str:
    start = text.index(_BEGIN_MARKER)
    end_marker = text.find(_END_MARKER, start)
    if end_marker == -1:
        raise TemplateInstallError(
            "found the start of our managed block but not its end; "
            "the settings file has been edited in a way we will not guess at"
        )
    end = end_marker + len(_END_MARKER)
    return text[:start] + block + text[end:]


def uninstall(*, reload_config: bool = True) -> InstallResult:
    """Remove the block we added, leaving anything else untouched."""
    settings_path = paths.noctalia_settings_path()
    recovered = _reconcile_interrupted_transaction(settings_path)
    with _read_settings_snapshot(settings_path) as snapshot:
        original = snapshot.text

        if _BEGIN_MARKER not in original:
            if recovered and reload_config:
                with contextlib.suppress(noctalia.NoctaliaError):
                    noctalia.reload_config()
            return InstallResult(
                changed=False,
                settings_path=settings_path,
                template_path=installed_template_path(),
                output_path=paths.palette_path(),
                backup_path=None,
                detail="not registered",
            )

        updated = _replace_managed_block(original, "").replace("\n\n\n", "\n\n")
        backup = _write_atomically(settings_path, updated, snapshot)

        if reload_config:
            with contextlib.suppress(noctalia.NoctaliaError):
                noctalia.reload_config()

        return InstallResult(
            changed=True,
            settings_path=settings_path,
            template_path=installed_template_path(),
            output_path=paths.palette_path(),
            backup_path=backup,
            detail="removed",
        )


def status() -> str:
    """One-line summary for the CLI."""
    settings_path = paths.noctalia_settings_path()
    if not settings_path.is_file():
        return f"not installed (no Noctalia settings at {settings_path})"
    entry = _existing_entry(_read_settings(settings_path))
    if entry is None:
        return "not installed"
    output = paths.palette_path()
    rendered = "rendered" if output.is_file() else "not yet rendered"
    enabled = "enabled" if entry.get("enabled") is True else "disabled"
    return f"installed, {enabled}; palette {rendered} at {output}"
