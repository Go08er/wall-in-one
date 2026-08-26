"""The schema-2 daemon is detectable even without its public socket entry."""

from __future__ import annotations

import contextlib
import fcntl
import os
import select
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from wall_in_one import predecessor_process


def _proc_root(tmp_path: Path, *, unix_lines: tuple[bytes, ...] = ()) -> Path:
    proc = tmp_path / "proc"
    network = proc / str(os.getpid()) / "net"
    network.mkdir(parents=True)
    header = b"Num RefCount Protocol Flags Type St Inode Path\n"
    (network / "unix").write_bytes(header + b"\n".join(unix_lines) + (b"\n" if unix_lines else b""))
    (proc / "locks").write_bytes(b"")
    return proc


def _stat(pid: int, start_time: int = 123456) -> bytes:
    # Fields 3 through 21 precede starttime (field 22). ``comm`` deliberately
    # contains spaces and parentheses to exercise the rightmost-')' parser.
    fields = [b"S", *([b"0"] * 18), str(start_time).encode()]
    return f"{pid} (wall in one (service)) ".encode() + b" ".join(fields) + b"\n"


def _process(
    proc: Path,
    pid: int,
    arguments: tuple[bytes, ...],
    *,
    environment: tuple[bytes, ...] = (),
    executable: bytes = b"/nix/store/old-wall-in-one/bin/wall-in-one-service",
    uid: int | None = None,
    cwd: bytes | None = None,
) -> Path:
    entry = proc / str(pid)
    entry.mkdir()
    selected_uid = os.geteuid() if uid is None else uid
    (entry / "stat").write_bytes(_stat(pid))
    (entry / "status").write_bytes(
        f"Name:\twall-in-one\nUid:\t{selected_uid}\t{selected_uid}\t"
        f"{selected_uid}\t{selected_uid}\n".encode()
    )
    (entry / "cmdline").write_bytes(b"\0".join(arguments) + (b"\0" if arguments else b""))
    (entry / "environ").write_bytes(b"\0".join(environment) + (b"\0" if environment else b""))
    (entry / "exe").symlink_to(os.fsdecode(executable))
    if cwd is not None:
        (entry / "cwd").symlink_to(os.fsdecode(cwd))
    return entry


def _targets(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    return (
        state / "wall-in-one" / "runtime.toml",
        runtime / "wall-in-one-runtime.sock",
    )


def test_default_predecessor_is_found_before_its_socket_is_bound(tmp_path: Path) -> None:
    proc = _proc_root(tmp_path)
    config, socket = _targets(tmp_path)
    _process(
        proc,
        4101,
        (b"/nix/store/old/bin/wall-in-one-service", b"--wait-for-config"),
        environment=(
            b"HOME=/home/example",
            b"XDG_STATE_HOME=" + os.fsencode(config.parents[1]),
            b"XDG_RUNTIME_DIR=" + os.fsencode(socket.parent),
        ),
    )

    with pytest.raises(
        predecessor_process.PredecessorProcessError,
        match=r"process 4101.*current runtime config",
    ):
        predecessor_process.refuse_live_predecessor_runtime(
            proc_root=proc,
            expected_config=config,
            expected_socket=socket,
        )


@pytest.mark.parametrize("matching_option", ["config", "socket"])
def test_either_explicit_current_target_is_enough_to_refuse(
    tmp_path: Path,
    matching_option: str,
) -> None:
    proc = _proc_root(tmp_path)
    config, socket = _targets(tmp_path)
    explicit_config = config if matching_option == "config" else tmp_path / "other.toml"
    explicit_socket = socket if matching_option == "socket" else tmp_path / "other.sock"
    _process(
        proc,
        4102,
        (
            b"wall-in-one-service",
            b"--config",
            os.fsencode(explicit_config),
            b"--socket",
            os.fsencode(explicit_socket),
        ),
        environment=(b"HOME=/unrelated",),
    )

    with pytest.raises(predecessor_process.PredecessorProcessError, match="still targets"):
        predecessor_process.refuse_live_predecessor_runtime(
            proc_root=proc,
            expected_config=config,
            expected_socket=socket,
        )


def test_relative_explicit_targets_are_resolved_against_process_cwd(tmp_path: Path) -> None:
    proc = _proc_root(tmp_path)
    config, runtime_socket = _targets(tmp_path)
    cwd = tmp_path / "working-directory"
    cwd.mkdir()
    _process(
        proc,
        4111,
        (
            b"wall-in-one-service",
            b"--config",
            os.fsencode(os.path.relpath(config, cwd)),
            b"--socket",
            os.fsencode(os.path.relpath(runtime_socket, cwd)),
        ),
        cwd=os.fsencode(cwd),
    )

    with pytest.raises(
        predecessor_process.PredecessorProcessError,
        match=r"process 4111.*current runtime config",
    ):
        predecessor_process.refuse_live_predecessor_runtime(
            proc_root=proc,
            expected_config=config,
            expected_socket=runtime_socket,
        )


def test_nonmatching_relative_targets_are_ignored(tmp_path: Path) -> None:
    proc = _proc_root(tmp_path)
    config, runtime_socket = _targets(tmp_path)
    cwd = tmp_path / "working-directory"
    cwd.mkdir()
    _process(
        proc,
        4112,
        (
            b"wall-in-one-service",
            b"--config",
            b"different/runtime.toml",
            b"--socket",
            b"different/runtime.sock",
        ),
        cwd=os.fsencode(cwd),
    )

    predecessor_process.refuse_live_predecessor_runtime(
        proc_root=proc,
        expected_config=config,
        expected_socket=runtime_socket,
    )


def test_relative_candidate_with_unprovable_cwd_fails_closed(tmp_path: Path) -> None:
    proc = _proc_root(tmp_path)
    config, runtime_socket = _targets(tmp_path)
    _process(
        proc,
        4113,
        (
            b"wall-in-one-service",
            b"--config",
            b"relative/runtime.toml",
            b"--socket",
            os.fsencode(tmp_path / "other.sock"),
        ),
    )

    with pytest.raises(
        predecessor_process.PredecessorProcessError,
        match=r"cannot identify candidate predecessor process 4113 working directory",
    ):
        predecessor_process.refuse_live_predecessor_runtime(
            proc_root=proc,
            expected_config=config,
            expected_socket=runtime_socket,
        )


def test_a_different_profile_and_unrelated_process_are_ignored(tmp_path: Path) -> None:
    proc = _proc_root(tmp_path)
    config, socket = _targets(tmp_path)
    _process(
        proc,
        4103,
        (
            b"wall-in-one-service",
            b"--config",
            b"/other/runtime.toml",
            b"--socket",
            b"/other/runtime.sock",
        ),
    )
    _process(
        proc,
        4104,
        (b"/nix/store/example/bin/something-else",),
        executable=b"/nix/store/example/bin/something-else",
    )

    predecessor_process.refuse_live_predecessor_runtime(
        proc_root=proc,
        expected_config=config,
        expected_socket=socket,
    )


def test_padded_unrelated_command_line_is_ignored_before_strict_parsing(tmp_path: Path) -> None:
    proc = _proc_root(tmp_path)
    config, runtime_socket = _targets(tmp_path)
    entry = _process(
        proc,
        4110,
        (b"(wall-in-one)",),
        executable=b"/nix/store/example/bin/wall-in-one",
    )
    # Linux exposes argv memory, which programs may pad or rewrite for a
    # process title. Empty fields are candidate-invalid but ordinary for an
    # unrelated process and must not make migration globally uncertain.
    (entry / "cmdline").write_bytes(b"(wall-in-one)\0\0\0\0")

    predecessor_process.refuse_live_predecessor_runtime(
        proc_root=proc,
        expected_config=config,
        expected_socket=runtime_socket,
    )


def test_rust_xdg_fallback_is_derived_from_the_process_environment(tmp_path: Path) -> None:
    proc = _proc_root(tmp_path)
    home = tmp_path / "home"
    config = home / ".local/state/wall-in-one/runtime.toml"
    socket = home / ".local/state/wall-in-one/wall-in-one-runtime.sock"
    _process(
        proc,
        4105,
        (b"wall-in-one-service",),
        environment=(b"HOME=" + os.fsencode(home), b"XDG_STATE_HOME=relative-is-ignored"),
    )

    with pytest.raises(predecessor_process.PredecessorProcessError, match="process 4105"):
        predecessor_process.refuse_live_predecessor_runtime(
            proc_root=proc,
            expected_config=config,
            expected_socket=socket,
        )


def test_an_unlinked_but_live_kernel_socket_is_still_refused(tmp_path: Path) -> None:
    config, socket = _targets(tmp_path)
    line = b"0000000000000000: 00000002 00000000 00010000 0001 01 12345 " + os.fsencode(socket)
    proc = _proc_root(tmp_path, unix_lines=(line,))

    with pytest.raises(
        predecessor_process.PredecessorProcessError,
        match="live Unix socket still owns predecessor endpoint",
    ):
        predecessor_process.refuse_live_predecessor_runtime(
            proc_root=proc,
            expected_config=config,
            expected_socket=socket,
        )


def test_a_candidate_with_unreadable_semantics_is_uncertain_not_absent(tmp_path: Path) -> None:
    proc = _proc_root(tmp_path)
    config, socket = _targets(tmp_path)
    entry = _process(
        proc,
        4106,
        (b"wall-in-one-service",),
        environment=(b"HOME=/home/example",),
    )
    (entry / "environ").write_bytes(b"HOME=/home/example")

    with pytest.raises(
        predecessor_process.PredecessorProcessError,
        match="environment changed or is not NUL terminated",
    ):
        predecessor_process.refuse_live_predecessor_runtime(
            proc_root=proc,
            expected_config=config,
            expected_socket=socket,
        )


def test_a_mixed_uid_candidate_fails_closed(tmp_path: Path) -> None:
    proc = _proc_root(tmp_path)
    config, socket = _targets(tmp_path)
    entry = _process(
        proc,
        4107,
        (
            b"wall-in-one-service",
            b"--config",
            os.fsencode(config),
            b"--socket",
            os.fsencode(socket),
        ),
    )
    uid = os.geteuid()
    (entry / "status").write_text(
        f"Name:\twall-in-one\nUid:\t{uid}\t{uid}\t{uid + 1}\t{uid}\n",
        encoding="utf-8",
    )

    with pytest.raises(predecessor_process.PredecessorProcessError, match=r"mixed.*user ids"):
        predecessor_process.refuse_live_predecessor_runtime(
            proc_root=proc,
            expected_config=config,
            expected_socket=socket,
        )


def test_scan_is_read_only(tmp_path: Path) -> None:
    proc = _proc_root(tmp_path)
    config, socket = _targets(tmp_path)
    _process(
        proc,
        4108,
        (
            b"wall-in-one-service",
            b"--config",
            b"/different/runtime.toml",
            b"--socket",
            b"/different/runtime.sock",
        ),
    )
    before = {
        path.relative_to(proc): (path.lstat().st_mode, path.read_bytes())
        for path in proc.rglob("*")
        if path.is_file() and not path.is_symlink()
    }

    predecessor_process.refuse_live_predecessor_runtime(
        proc_root=proc,
        expected_config=config,
        expected_socket=socket,
    )

    assert {
        path.relative_to(proc): (path.lstat().st_mode, path.read_bytes())
        for path in proc.rglob("*")
        if path.is_file() and not path.is_symlink()
    } == before


def test_status_observer_detects_an_external_singleton_owner(tmp_path: Path) -> None:
    lock = tmp_path / "runtime.sock.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    child = subprocess.Popen(
        (
            sys.executable,
            "-c",
            "import fcntl, os, sys; "
            "descriptor = os.open(sys.argv[1], os.O_RDWR); "
            "fcntl.flock(descriptor, fcntl.LOCK_EX); "
            "os.write(int(sys.argv[2]), b'x'); "
            "os.read(int(sys.argv[3]), 1)",
            str(lock),
            str(ready_write),
            str(release_read),
        ),
        close_fds=True,
        pass_fds=(ready_write, release_read),
    )
    os.close(ready_write)
    os.close(release_read)
    try:
        readable, _writable, _exceptional = select.select((ready_read,), (), (), 5)
        assert readable, "child did not acquire the singleton flock within five seconds"
        assert os.read(ready_read, 1) == b"x"
        with pytest.raises(
            predecessor_process.PredecessorProcessError,
            match=r"still owns singleton guard.*runtime\.sock\.lock",
        ):
            predecessor_process.refuse_live_writer_status(
                socket_paths=(),
                lock_paths=(lock,),
            )
    finally:
        os.close(ready_read)
        with contextlib.suppress(BrokenPipeError):
            os.write(release_write, b"x")
        os.close(release_write)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
    assert child.returncode == 0


@pytest.mark.parametrize(
    ("raw", "message"),
    (
        (
            b"573 304 0:not-decimal / / rw - btrfs /dev/root rw\n",
            "malformed device identity",
        ),
        (
            b"573 304 0:35 / / rw - btrfs /dev/root rw\n"
            b"573 304 0:35 / /bind rw - btrfs /dev/root rw\n",
            "duplicate mount id 573",
        ),
    ),
)
def test_status_observer_rejects_ambiguous_mount_mapping(raw: bytes, message: str) -> None:
    with pytest.raises(predecessor_process.PredecessorProcessError, match=message):
        predecessor_process._mountinfo_devices(raw, frozenset((573,)))


def test_status_observer_parses_the_mount_device_not_stat_device() -> None:
    assert predecessor_process._mountinfo_devices(
        b"573 304 0:35 / / rw - btrfs /dev/root rw\n",
        frozenset((573,)),
    ) == {573: (0, 35)}


def test_status_observer_ignores_its_own_lock_without_trial_flock(tmp_path: Path) -> None:
    lock = tmp_path / "authoring.sock.lock"
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        # A second trial LOCK_EX on this independently opened inode would fail,
        # even in the same process. Returning proves the helper only observed
        # procfs and ignored this process's retained lock.
        predecessor_process.refuse_live_writer_status(
            socket_paths=(),
            lock_paths=(lock,),
        )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_status_socket_observation_does_not_enqueue_a_connection(tmp_path: Path) -> None:
    endpoint = tmp_path / "authoring.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            listener.bind(str(endpoint))
        except PermissionError:
            pytest.skip("the test sandbox forbids binding Unix sockets")
        listener.listen(1)
        listener.setblocking(False)
        unix_record = b"0000000000000000: 00000002 00000000 00010000 0001 01 12345 " + os.fsencode(
            endpoint
        )
        proc = _proc_root(tmp_path, unix_lines=(unix_record,))

        with pytest.raises(
            predecessor_process.PredecessorProcessError,
            match="live Unix socket still owns Wall-in-One endpoint",
        ):
            predecessor_process.refuse_live_writer_status(
                proc_root=proc,
                socket_paths=(endpoint,),
                lock_paths=(),
            )
        with pytest.raises(BlockingIOError):
            listener.accept()
    finally:
        listener.close()
        endpoint.unlink(missing_ok=True)
