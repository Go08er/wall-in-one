"""Bounded detection of the deployed Rust predecessor outside its socket name.

The exact deployed service predates the singleton lock used by current Rust
builds.  It also applies its first wallpaper before binding the public Unix
socket.  Absence of that pathname therefore cannot prove that the predecessor
is quiescent during a schema-crossing migration.

This module is deliberately read-only.  It scans same-user processes through
retained ``/proc/<pid>`` directory descriptors, recognizes the shipped
``wall-in-one-service`` argument and XDG-default rules, and refuses ambiguity
for a candidate process.  ``/proc/net/unix`` supplies an independent check for
a listener whose pathname was unlinked after bind.

A negative process scan cannot prevent a new lock-unaware predecessor from
being launched afterward.  Callers must combine this check with the packaged
systemd stop/start-pre boundary (or an explicitly stopped manual service), and
repeat it immediately before their irreversible publication.
"""

from __future__ import annotations

import errno
import os
import posixpath
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from wall_in_one import paths

SERVICE_NAME: Final = b"wall-in-one-service"
MAX_PROC_ENTRIES: Final = 32_768
MAX_CANDIDATES: Final = 64
MAX_STATUS_BYTES: Final = 64 * 1024
MAX_STAT_BYTES: Final = 64 * 1024
MAX_CMDLINE_BYTES: Final = 64 * 1024
MAX_ENVIRON_BYTES: Final = 1024 * 1024
MAX_PROC_PATH_BYTES: Final = 64 * 1024
MAX_FDINFO_BYTES: Final = 64 * 1024
MAX_MOUNTINFO_BYTES: Final = 8 * 1024 * 1024
MAX_MOUNTINFO_LINES: Final = 131_072
MAX_UNIX_TABLE_BYTES: Final = 8 * 1024 * 1024
MAX_UNIX_TABLE_LINES: Final = 131_072
MAX_LOCK_TABLE_BYTES: Final = 8 * 1024 * 1024
MAX_LOCK_TABLE_LINES: Final = 131_072
READ_CHUNK_BYTES: Final = 64 * 1024

_OPEN_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
_OPEN_FILE_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_VANISHED_ERRNOS: Final = frozenset({errno.ENOENT, errno.ESRCH})


class PredecessorProcessError(Exception):
    """A predecessor is live, or its absence cannot be proved safely."""


@dataclass(frozen=True, slots=True)
class _ProcessTarget:
    config: bytes
    socket: bytes


def _read_fd_bounded(descriptor: int, maximum: int, *, label: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = os.read(descriptor, min(READ_CHUNK_BYTES, maximum + 1 - total))
        except OSError as error:
            raise PredecessorProcessError(f"cannot read {label}: {error}") from error
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            raise PredecessorProcessError(f"{label} exceeds its {maximum}-byte ceiling")


def _read_at(directory: int, name: str, maximum: int, *, label: str) -> bytes:
    try:
        descriptor = os.open(name, _OPEN_FILE_FLAGS, dir_fd=directory)
    except OSError as error:
        raise PredecessorProcessError(f"cannot open {label}: {error}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise PredecessorProcessError(f"{label} is not a regular proc record")
        return _read_fd_bounded(descriptor, maximum, label=label)
    finally:
        os.close(descriptor)


def _process_start_time(raw: bytes, pid: int) -> bytes:
    """Return Linux proc-stat field 22 without trusting spaces in ``comm``."""
    closing = raw.rfind(b")")
    prefix = f"{pid} (".encode()
    if not raw.startswith(prefix) or closing < len(prefix):
        raise PredecessorProcessError(f"process {pid} has an ambiguous /proc stat identity")
    fields = raw[closing + 1 :].split()
    # The first field after ``comm`` is field 3 (state); starttime is field 22.
    if len(fields) < 20 or not fields[19].isdigit():
        raise PredecessorProcessError(f"process {pid} has a malformed /proc stat record")
    return fields[19]


def _uid_from_status(raw: bytes, pid: int) -> tuple[int, int, int, int]:
    found: tuple[int, int, int, int] | None = None
    for line in raw.splitlines():
        if not line.startswith(b"Uid:"):
            continue
        fields = line[4:].split()
        if len(fields) != 4 or any(not field.isdigit() for field in fields):
            raise PredecessorProcessError(f"process {pid} has a malformed Uid status record")
        if found is not None:
            raise PredecessorProcessError(f"process {pid} has duplicate Uid status records")
        found = tuple(int(field) for field in fields)  # type: ignore[assignment]
    if found is None:
        raise PredecessorProcessError(f"process {pid} has no Uid status record")
    return found


def _split_nul_record(raw: bytes, *, label: str, allow_empty: bool = False) -> tuple[bytes, ...]:
    if not raw:
        if allow_empty:
            return ()
        raise PredecessorProcessError(f"{label} is empty")
    if not raw.endswith(b"\0"):
        raise PredecessorProcessError(f"{label} changed or is not NUL terminated")
    fields = tuple(raw[:-1].split(b"\0"))
    if any(not field for field in fields):
        raise PredecessorProcessError(f"{label} contains an empty field")
    return fields


def _command_name(raw: bytes) -> bytes | None:
    """Return argv0 without imposing candidate-only rules on other processes."""
    if not raw:
        return None
    name, separator, _remainder = raw.partition(b"\0")
    if not separator:
        # Keep an exact candidate visible to the later strict parser, which
        # will reject the unstable/non-terminated record. An unrelated argv0
        # can still be ignored without interpreting its padding.
        return raw
    return name or None


def _service_basename(value: bytes) -> bool:
    if value.endswith(b" (deleted)"):
        value = value[: -len(b" (deleted)")]
    return posixpath.basename(value) == SERVICE_NAME


def _environment(raw: bytes, pid: int) -> dict[bytes, bytes]:
    fields = _split_nul_record(raw, label=f"process {pid} environment", allow_empty=True)
    wanted = {b"HOME", b"XDG_STATE_HOME", b"XDG_RUNTIME_DIR"}
    result: dict[bytes, bytes] = {}
    for field in fields:
        name, separator, value = field.partition(b"=")
        if not separator or not name:
            raise PredecessorProcessError(f"process {pid} has a malformed environment record")
        if name not in wanted:
            continue
        if name in result:
            raise PredecessorProcessError(
                f"process {pid} has duplicate {os.fsdecode(name)} environment entries"
            )
        result[name] = value
    return result


def _xdg(environment: dict[bytes, bytes], name: bytes, fallback: bytes) -> bytes:
    value = environment.get(name)
    return value if value and posixpath.isabs(value) else fallback


def _default_targets(environment: dict[bytes, bytes]) -> _ProcessTarget:
    home = environment.get(b"HOME", b"/")
    state = _xdg(environment, b"XDG_STATE_HOME", posixpath.join(home, b".local/state"))
    runtime = _xdg(environment, b"XDG_RUNTIME_DIR", posixpath.join(state, b"wall-in-one"))
    return _ProcessTarget(
        posixpath.join(state, b"wall-in-one/runtime.toml"),
        posixpath.join(runtime, b"wall-in-one-runtime.sock"),
    )


def _arguments_target(
    arguments: tuple[bytes, ...],
    read_environment: Callable[[], bytes],
    pid: int,
) -> _ProcessTarget:
    config: bytes | None = None
    socket: bytes | None = None
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        if argument in (b"--config", b"--socket"):
            index += 1
            if index >= len(arguments):
                raise PredecessorProcessError(
                    f"candidate predecessor process {pid} has {os.fsdecode(argument)} "
                    "without a path"
                )
            value = arguments[index]
            if argument == b"--config":
                config = value
            else:
                socket = value
        elif argument == b"--wait-for-config":
            pass
        elif argument in (b"--help", b"--version"):
            # These parse-time modes exit before loading or applying a runtime.
            return _ProcessTarget(b"", b"")
        else:
            raise PredecessorProcessError(
                f"candidate predecessor process {pid} has an unknown argument "
                f"{os.fsdecode(argument)!r}"
            )
        index += 1

    if config is None or socket is None:
        defaults = _default_targets(_environment(read_environment(), pid))
        if config is None:
            config = defaults.config
        if socket is None:
            socket = defaults.socket
    return _ProcessTarget(config, socket)


def _normal_path(value: bytes) -> bytes | None:
    if not value or not posixpath.isabs(value):
        return None
    return posixpath.normpath(value)


def _process_cwd(process: int, pid: int) -> bytes:
    try:
        raw = os.fsencode(os.readlink("cwd", dir_fd=process))
    except OSError as error:
        raise PredecessorProcessError(
            f"cannot identify candidate predecessor process {pid} working directory: {error}"
        ) from error
    if len(raw) > MAX_PROC_PATH_BYTES:
        raise PredecessorProcessError(
            f"candidate predecessor process {pid} working directory exceeds its "
            f"{MAX_PROC_PATH_BYTES}-byte ceiling"
        )
    if raw.endswith(b" (deleted)"):
        raise PredecessorProcessError(
            f"candidate predecessor process {pid} has a deleted working directory"
        )
    normalized = _normal_path(raw)
    if normalized is None:
        raise PredecessorProcessError(
            f"candidate predecessor process {pid} working directory is not absolute"
        )
    return normalized


def _resolve_process_targets(
    target: _ProcessTarget,
    *,
    process: int,
    pid: int,
) -> _ProcessTarget:
    """Resolve Rust's relative PathBuf arguments against the retained cwd."""
    if not target.config and not target.socket:
        return target
    config = _normal_path(target.config)
    runtime_socket = _normal_path(target.socket)
    if config is not None and runtime_socket is not None:
        return _ProcessTarget(config, runtime_socket)
    cwd = _process_cwd(process, pid)
    if config is None:
        config = posixpath.normpath(posixpath.join(cwd, target.config))
    if runtime_socket is None:
        runtime_socket = posixpath.normpath(posixpath.join(cwd, target.socket))
    return _ProcessTarget(config, runtime_socket)


def _read_executable(process: int, pid: int) -> bytes | None:
    try:
        return os.fsencode(os.readlink("exe", dir_fd=process))
    except OSError as error:
        if error.errno in _VANISHED_ERRNOS:
            return None
        raise PredecessorProcessError(
            f"cannot identify same-user process {pid} executable: {error}"
        ) from error


def _same_public_process(proc: int, pid_name: str, process: int, opened: os.stat_result) -> bool:
    try:
        current = os.stat(pid_name, dir_fd=proc, follow_symlinks=False)
    except OSError as error:
        if error.errno in _VANISHED_ERRNOS:
            return False
        raise PredecessorProcessError(
            f"cannot recheck process {pid_name} after inspection: {error}"
        ) from error
    return (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)


def _candidate_targets(
    proc: int,
    pid_name: str,
    *,
    expected_uid: int,
) -> tuple[int, _ProcessTarget] | None:
    pid = int(pid_name)
    try:
        process = os.open(pid_name, _OPEN_DIRECTORY_FLAGS, dir_fd=proc)
    except OSError as error:
        if error.errno in _VANISHED_ERRNOS:
            return None
        raise PredecessorProcessError(f"cannot retain process {pid}: {error}") from error
    try:
        opened = os.fstat(process)
        if not stat.S_ISDIR(opened.st_mode):
            raise PredecessorProcessError(f"process entry {pid} is not a directory")
        if opened.st_uid != expected_uid:
            return None

        before = _process_start_time(
            _read_at(process, "stat", MAX_STAT_BYTES, label=f"process {pid} stat"),
            pid,
        )
        try:
            cmdline_raw = _read_at(
                process,
                "cmdline",
                MAX_CMDLINE_BYTES,
                label=f"process {pid} command line",
            )
        except PredecessorProcessError:
            executable = _read_executable(process, pid)
            if executable is not None and _service_basename(executable):
                raise
            return None
        command_name = _command_name(cmdline_raw)
        if command_name is None:
            # A zombie has no command line and cannot write Noctalia.
            return None
        # The exact systemd and direct predecessor invocations both retain the
        # binary path as argv[0]. Avoid probing every unrelated same-user
        # executable: procfs policies may legitimately hide their ``exe``
        # links, and that must not block this migration.
        if not _service_basename(command_name):
            return None
        arguments = _split_nul_record(
            cmdline_raw,
            label=f"process {pid} command line",
        )
        executable = _read_executable(process, pid)
        if executable is None:
            return None
        if not _service_basename(executable):
            raise PredecessorProcessError(
                f"process {pid} claims the predecessor command name but runs a different executable"
            )

        uids = _uid_from_status(
            _read_at(process, "status", MAX_STATUS_BYTES, label=f"process {pid} status"),
            pid,
        )
        if uids[1] != expected_uid:
            return None
        if any(uid != expected_uid for uid in uids):
            raise PredecessorProcessError(
                f"candidate predecessor process {pid} has mixed real/effective user ids"
            )
        target = _resolve_process_targets(
            _arguments_target(
                arguments,
                lambda: _read_at(
                    process,
                    "environ",
                    MAX_ENVIRON_BYTES,
                    label=f"process {pid} environment",
                ),
                pid,
            ),
            process=process,
            pid=pid,
        )
        after = _process_start_time(
            _read_at(process, "stat", MAX_STAT_BYTES, label=f"process {pid} stat recheck"),
            pid,
        )
        if before != after or not _same_public_process(proc, pid_name, process, opened):
            raise PredecessorProcessError(f"process {pid} changed identity during inspection")
        return pid, target
    except PredecessorProcessError:
        if not _same_public_process(proc, pid_name, process, opened):
            return None
        raise
    finally:
        os.close(process)


def _unix_table_matching_socket(
    proc: int,
    expected_sockets: frozenset[bytes],
    inspector_pid: int,
) -> bytes | None:
    try:
        inspector = os.open(str(inspector_pid), _OPEN_DIRECTORY_FLAGS, dir_fd=proc)
    except OSError as error:
        if error.errno in _VANISHED_ERRNOS:
            raise PredecessorProcessError(
                f"cannot retain inspector process {inspector_pid} in procfs"
            ) from error
        raise PredecessorProcessError(
            f"cannot retain inspector process {inspector_pid}: {error}"
        ) from error
    try:
        try:
            network = os.open("net", _OPEN_DIRECTORY_FLAGS, dir_fd=inspector)
        except OSError as error:
            raise PredecessorProcessError(
                f"cannot retain process {inspector_pid} network namespace: {error}"
            ) from error
        try:
            try:
                raw = _read_at(
                    network,
                    "unix",
                    MAX_UNIX_TABLE_BYTES,
                    label="/proc/net/unix",
                )
            except PredecessorProcessError as error:
                raise PredecessorProcessError(
                    f"cannot prove whether the predecessor owns an unlinked socket: {error}"
                ) from error
        finally:
            os.close(network)
    finally:
        os.close(inspector)

    lines = raw.splitlines()
    if len(lines) > MAX_UNIX_TABLE_LINES:
        raise PredecessorProcessError(
            f"/proc/net/unix exceeds its {MAX_UNIX_TABLE_LINES}-line ceiling"
        )
    if not lines or not lines[0].startswith(b"Num "):
        raise PredecessorProcessError("/proc/net/unix has no recognized header")
    for line in lines[1:]:
        fields = line.split(None, 7)
        if len(fields) < 7:
            raise PredecessorProcessError("/proc/net/unix contains a malformed socket record")
        if len(fields) == 8:
            candidate = _normal_path(fields[7])
            if candidate in expected_sockets:
                return candidate
    return None


def _unix_table_has_socket(proc: int, expected_socket: bytes, inspector_pid: int) -> bool:
    return (
        _unix_table_matching_socket(proc, frozenset((expected_socket,)), inspector_pid) is not None
    )


def _lock_identity(field: bytes) -> tuple[int, int, int] | None:
    major, separator, remainder = field.partition(b":")
    minor, second_separator, inode = remainder.partition(b":")
    if not separator or not second_separator or not major or not minor or not inode:
        return None
    try:
        return int(major, 16), int(minor, 16), int(inode, 10)
    except ValueError:
        return None


def _lock_table_matching_path(
    raw: bytes,
    expected: dict[tuple[int, int, int], Path],
    *,
    ignored_pid: int,
) -> Path | None:
    lines = raw.splitlines()
    if len(lines) > MAX_LOCK_TABLE_LINES:
        raise PredecessorProcessError(
            f"/proc/locks exceeds its {MAX_LOCK_TABLE_LINES}-line ceiling"
        )
    for line in lines:
        fields = line.split()
        # A line prefixed with ``->`` describes a waiter, not an owner. The
        # current singleton implementations use a nonblocking FLOCK and never
        # create one, but ignoring unrelated waiters avoids a false live owner.
        if len(fields) > 1 and fields[1] == b"->":
            continue
        if len(fields) < 6 or not fields[0].endswith(b":"):
            raise PredecessorProcessError("/proc/locks contains a malformed lock record")
        try:
            owner_pid = int(fields[4])
        except ValueError as error:
            raise PredecessorProcessError("/proc/locks contains a malformed owner pid") from error
        if owner_pid == ignored_pid:
            continue
        identity = _lock_identity(fields[5])
        if identity is None:
            raise PredecessorProcessError("/proc/locks contains a malformed lock identity")
        matched = expected.get(identity)
        if matched is not None:
            return matched
    return None


def _read_proc_locks(proc: int) -> bytes:
    try:
        return _read_at(proc, "locks", MAX_LOCK_TABLE_BYTES, label="/proc/locks")
    except PredecessorProcessError as error:
        raise PredecessorProcessError(
            f"cannot prove whether a current singleton guard is held: {error}"
        ) from error


def _fd_mount_id(raw: bytes, descriptor: int) -> int:
    found: int | None = None
    for line in raw.splitlines():
        if not line.startswith(b"mnt_id:"):
            continue
        value = line[len(b"mnt_id:") :].strip()
        if not value.isdigit() or found is not None:
            raise PredecessorProcessError(
                f"/proc fdinfo for singleton descriptor {descriptor} has an ambiguous mnt_id"
            )
        found = int(value)
    if found is None:
        raise PredecessorProcessError(
            f"/proc fdinfo for singleton descriptor {descriptor} has no mnt_id"
        )
    return found


def _mountinfo_devices(
    raw: bytes,
    wanted: frozenset[int],
) -> dict[int, tuple[int, int]]:
    lines = raw.splitlines()
    if len(lines) > MAX_MOUNTINFO_LINES:
        raise PredecessorProcessError(
            f"/proc mountinfo exceeds its {MAX_MOUNTINFO_LINES}-line ceiling"
        )
    result: dict[int, tuple[int, int]] = {}
    for line in lines:
        fields = line.split()
        if len(fields) < 7 or not fields[0].isdigit():
            raise PredecessorProcessError("/proc mountinfo contains a malformed mount record")
        mount_id = int(fields[0])
        if mount_id not in wanted:
            continue
        major, separator, minor = fields[2].partition(b":")
        if not separator or not major.isdigit() or not minor.isdigit():
            raise PredecessorProcessError(
                f"/proc mountinfo record {mount_id} has a malformed device identity"
            )
        if mount_id in result:
            raise PredecessorProcessError(f"/proc mountinfo contains duplicate mount id {mount_id}")
        result[mount_id] = (int(major), int(minor))
    missing = wanted.difference(result)
    if missing:
        rendered = ", ".join(str(value) for value in sorted(missing))
        raise PredecessorProcessError(
            f"/proc mountinfo does not bind singleton mount id(s) {rendered}"
        )
    return result


def _mounted_lock_identities(
    proc: int,
    inspector_pid: int,
    retained: list[tuple[Path, int, os.stat_result]],
) -> dict[tuple[int, int, int], Path]:
    """Map retained FDs to the superblock identity used by /proc/locks."""
    try:
        inspector = os.open(str(inspector_pid), _OPEN_DIRECTORY_FLAGS, dir_fd=proc)
    except OSError as error:
        raise PredecessorProcessError(
            f"cannot retain inspector process {inspector_pid} for lock observation: {error}"
        ) from error
    try:
        try:
            fdinfo = os.open("fdinfo", _OPEN_DIRECTORY_FLAGS, dir_fd=inspector)
        except OSError as error:
            raise PredecessorProcessError(
                f"cannot retain process {inspector_pid} fdinfo: {error}"
            ) from error
        try:
            mount_ids = {
                descriptor: _fd_mount_id(
                    _read_at(
                        fdinfo,
                        str(descriptor),
                        MAX_FDINFO_BYTES,
                        label=f"singleton descriptor {descriptor} fdinfo",
                    ),
                    descriptor,
                )
                for _path, descriptor, _status in retained
            }
        finally:
            os.close(fdinfo)
        mountinfo = _read_at(
            inspector,
            "mountinfo",
            MAX_MOUNTINFO_BYTES,
            label=f"process {inspector_pid} mountinfo",
        )
    finally:
        os.close(inspector)

    devices = _mountinfo_devices(mountinfo, frozenset(mount_ids.values()))
    result: dict[tuple[int, int, int], Path] = {}
    for path, descriptor, status in retained:
        major, minor = devices[mount_ids[descriptor]]
        identity = major, minor, status.st_ino
        existing = result.get(identity)
        if existing is not None and existing != path:
            raise PredecessorProcessError(
                f"singleton guards {existing} and {path} have one ambiguous lock identity"
            )
        result[identity] = path
    return result


def _pin_lock_path(path: Path, expected_uid: int) -> tuple[int, os.stat_result] | None:
    try:
        descriptor = os.open(path, _OPEN_FILE_FLAGS | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PredecessorProcessError(
            f"cannot open singleton guard {path} for observation: {error}"
        ) from error
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
        if not (
            stat.S_ISREG(opened.st_mode)
            and stat.S_ISREG(named.st_mode)
            and opened.st_uid == expected_uid
            and named.st_uid == expected_uid
            and opened.st_nlink == 1
            and named.st_nlink == 1
            and stat.S_IMODE(opened.st_mode) == 0o600
            and stat.S_IMODE(named.st_mode) == 0o600
            and (opened.st_dev, opened.st_ino) == (named.st_dev, named.st_ino)
        ):
            raise PredecessorProcessError(
                f"cannot trust singleton guard {path} during status observation"
            )
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _socket_lock_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


def refuse_live_writer_status(
    *,
    proc_root: Path = Path("/proc"),
    socket_paths: tuple[Path, ...] | None = None,
    lock_paths: tuple[Path, ...] | None = None,
    expected_uid: int | None = None,
    inspector_pid: int | None = None,
) -> None:
    """Observe current singleton locks and bound socket names without acting.

    Unlike a connect probe or a trial ``flock``, this cannot enqueue work in a
    server or make a concurrently starting nonblocking writer lose its guard.
    It pins existing lock-file inodes read-only, compares them with two bounded
    snapshots of ``/proc/locks``, and checks both sides of that observation in
    the inspector's numeric procfs network namespace. Missing lock names are
    rechecked so a concurrent creation is reported as uncertainty.

    This is a truthful status snapshot, not lifecycle exclusion. A mutating
    transaction must still acquire the singleton guards and repeat the exact
    predecessor process/socket proof immediately before publication.
    """
    selected_sockets = (
        (paths.socket_path(), paths.runtime_socket_path()) if socket_paths is None else socket_paths
    )
    selected_locks = (
        tuple(_socket_lock_path(path) for path in selected_sockets)
        if lock_paths is None
        else lock_paths
    )
    normalized_sockets: set[bytes] = set()
    for path in selected_sockets:
        normalized = _normal_path(os.fsencode(path))
        if normalized is None:
            raise PredecessorProcessError(f"status socket path is not absolute: {path}")
        normalized_sockets.add(normalized)

    uid = os.geteuid() if expected_uid is None else expected_uid
    current_pid = os.getpid() if inspector_pid is None else inspector_pid
    retained: list[tuple[Path, int, os.stat_result]] = []
    missing: list[Path] = []
    try:
        for path in selected_locks:
            pinned = _pin_lock_path(path, uid)
            if pinned is None:
                missing.append(path)
                continue
            descriptor, status = pinned
            retained.append((path, descriptor, status))

        try:
            proc = os.open(proc_root, _OPEN_DIRECTORY_FLAGS)
        except OSError as error:
            raise PredecessorProcessError(f"cannot retain procfs {proc_root}: {error}") from error
        try:
            expected_locks: dict[tuple[int, int, int], Path] | None = None
            for _pass in range(2):
                matched_socket = _unix_table_matching_socket(
                    proc,
                    frozenset(normalized_sockets),
                    current_pid,
                )
                if matched_socket is not None:
                    raise PredecessorProcessError(
                        f"a live Unix socket still owns Wall-in-One endpoint "
                        f"{os.fsdecode(matched_socket)}"
                    )
                if retained:
                    observed_locks = _mounted_lock_identities(proc, current_pid, retained)
                    if expected_locks is None:
                        expected_locks = observed_locks
                    elif observed_locks != expected_locks:
                        raise PredecessorProcessError(
                            "singleton mount identities changed during status observation"
                        )
                    matched_lock = _lock_table_matching_path(
                        _read_proc_locks(proc),
                        observed_locks,
                        ignored_pid=current_pid,
                    )
                    if matched_lock is not None:
                        raise PredecessorProcessError(
                            f"a Wall-in-One process still owns singleton guard {matched_lock}"
                        )
                for path, descriptor, opened in retained:
                    current = path.lstat()
                    status = os.fstat(descriptor)
                    if not (
                        stat.S_ISREG(status.st_mode)
                        and stat.S_ISREG(current.st_mode)
                        and status.st_uid == uid
                        and current.st_uid == uid
                        and status.st_nlink == 1
                        and current.st_nlink == 1
                        and stat.S_IMODE(status.st_mode) == 0o600
                        and stat.S_IMODE(current.st_mode) == 0o600
                        and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)
                        and (status.st_dev, status.st_ino) == (opened.st_dev, opened.st_ino)
                    ):
                        raise PredecessorProcessError(
                            f"singleton guard {path} changed during status observation"
                        )
                for path in missing:
                    try:
                        path.lstat()
                    except FileNotFoundError:
                        continue
                    except OSError as error:
                        raise PredecessorProcessError(
                            f"cannot recheck absent singleton guard {path}: {error}"
                        ) from error
                    raise PredecessorProcessError(
                        f"singleton guard {path} appeared during status observation"
                    )
        finally:
            os.close(proc)
    except OSError as error:
        raise PredecessorProcessError(
            f"cannot complete read-only writer status observation: {error}"
        ) from error
    finally:
        for _path, descriptor, _status in retained:
            os.close(descriptor)


def refuse_live_predecessor_runtime(
    *,
    proc_root: Path = Path("/proc"),
    expected_config: Path | None = None,
    expected_socket: Path | None = None,
    expected_uid: int | None = None,
    inspector_pid: int | None = None,
) -> None:
    """Raise unless no same-profile predecessor runtime is currently visible.

    The optional arguments are dependency-injection seams for isolated tests.
    Production callers use the current XDG config/socket paths and effective
    user id.  This function does not write procfs, signal a process, connect a
    socket, or alter any application state.
    """
    config_target = os.fsencode(expected_config or paths.runtime_config_path())
    socket_target = os.fsencode(expected_socket or paths.runtime_socket_path())
    normalized_config = _normal_path(config_target)
    normalized_socket = _normal_path(socket_target)
    if normalized_config is None or normalized_socket is None:
        raise PredecessorProcessError("expected predecessor paths are not absolute")
    uid = os.geteuid() if expected_uid is None else expected_uid
    current_pid = os.getpid() if inspector_pid is None else inspector_pid

    try:
        proc = os.open(proc_root, _OPEN_DIRECTORY_FLAGS)
    except OSError as error:
        raise PredecessorProcessError(f"cannot retain procfs {proc_root}: {error}") from error
    try:
        # Check the kernel socket table on both sides of the process walk.  The
        # second pass catches an old daemon which bound while its pid was being
        # inspected.  A later launch remains the caller's lifecycle concern.
        if _unix_table_has_socket(proc, normalized_socket, current_pid):
            raise PredecessorProcessError(
                "a live Unix socket still owns predecessor endpoint "
                f"{expected_socket or paths.runtime_socket_path()}"
            )

        candidates = 0
        entries = 0
        try:
            iterator = os.scandir(proc)
        except OSError as error:
            raise PredecessorProcessError(
                f"cannot enumerate procfs {proc_root}: {error}"
            ) from error
        with iterator:
            for entry in iterator:
                entries += 1
                if entries > MAX_PROC_ENTRIES:
                    raise PredecessorProcessError(
                        f"procfs exceeds its {MAX_PROC_ENTRIES}-entry inspection ceiling"
                    )
                if not entry.name.isascii() or not entry.name.isdigit():
                    continue
                if int(entry.name) == current_pid:
                    continue
                candidate = _candidate_targets(
                    proc,
                    entry.name,
                    expected_uid=uid,
                )
                if candidate is None:
                    continue
                candidates += 1
                if candidates > MAX_CANDIDATES:
                    raise PredecessorProcessError(
                        f"more than {MAX_CANDIDATES} candidate predecessor processes are live"
                    )
                pid, target = candidate
                config_match = _normal_path(target.config) == normalized_config
                socket_match = _normal_path(target.socket) == normalized_socket
                if config_match or socket_match:
                    matched = "runtime config" if config_match else "runtime socket"
                    raise PredecessorProcessError(
                        f"predecessor wall-in-one-service process {pid} still targets the "
                        f"current {matched}; stop it and retry"
                    )

        if _unix_table_has_socket(proc, normalized_socket, current_pid):
            raise PredecessorProcessError(
                "a live Unix socket still owns predecessor endpoint "
                f"{expected_socket or paths.runtime_socket_path()}"
            )
    finally:
        os.close(proc)
