from __future__ import annotations

import fcntl
import os
import select
import socket
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest

from tests.test_deployed_upgrade import _Profile, _profile
from wall_in_one import (
    config,
    deployed_upgrade,
    deployed_upgrade_transaction,
    file_io,
    paths,
    predecessor_process,
)
from wall_in_one.library import adopted, pairing, playlists, scan, stills
from wall_in_one.wallpaper import scenes


def _schema(path: Path) -> int:
    return int(tomllib.loads(path.read_text(encoding="utf-8"))["schema_version"])


def _watched_documents() -> tuple[Path, ...]:
    return (
        paths.settings_path(),
        paths.runtime_config_path(),
        deployed_upgrade.journal_path(),
        deployed_upgrade.authority_path(),
        deployed_upgrade.staged_runtime_path(),
        deployed_upgrade.completion_path(),
    )


def _snapshot(paths_to_watch: tuple[Path, ...] | None = None) -> dict[Path, bytes | None]:
    return {
        path: path.read_bytes() if path.is_file() and not path.is_symlink() else None
        for path in (paths_to_watch or _watched_documents())
    }


def _forbid_decoder(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("the deployed upgrade must not invoke a media decoder")


@contextmanager
def _external_flock(path: Path) -> Iterator[None]:
    """Hold one real cross-process FLOCK with bounded pipe handshakes."""
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    child = subprocess.Popen(
        (
            sys.executable,
            "-c",
            "import fcntl, os, sys; "
            "descriptor = os.open(sys.argv[1], os.O_RDWR); "
            "fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB); "
            "os.write(int(sys.argv[2]), b'x'); "
            "os.read(int(sys.argv[3]), 1)",
            str(path),
            str(ready_write),
            str(release_read),
        ),
        close_fds=True,
        pass_fds=(ready_write, release_read),
    )
    os.close(ready_write)
    os.close(release_read)
    try:
        readable, _writable, _exceptional = select.select((ready_read,), (), (), 5.0)
        if not readable or os.read(ready_read, 1) != b"x":
            raise AssertionError("external flock child did not acquire its guard")
        yield
    finally:
        os.close(ready_read)
        with suppress(BrokenPipeError, OSError):
            os.write(release_write, b"x")
        os.close(release_write)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        if child.returncode != 0:
            raise AssertionError(f"external flock child exited {child.returncode}")


def _replace_noctalia_settings(root: Path, *, directory: Path | None = None) -> bytes:
    """Model the field report's ordinary Noctalia cycle publication."""
    target = paths.noctalia_settings_path()
    replacement = target.with_name(".settings.toml.next")
    chosen = root / "Wall-in-One" / "Automatic Stills" / "cycle-choice.png"
    raw = (
        "[theme]\n"
        'source = "wallpaper"\n'
        'wallpaper_scheme = "new-theme"\n\n'
        "[wallpaper]\n"
        f'directory = "{directory or root}"\n'
        "enabled = true\n"
        'fill_mode = "crop"\n\n'
        "[wallpaper.default]\n"
        f'path = "{chosen}"\n\n'
        "[wallpaper.last]\n"
        f'path = "{chosen}"\n\n'
        "[wallpaper.monitors.eDP-1]\n"
        f'path = "{chosen}"\n'
    ).encode()
    replacement.write_bytes(raw)
    os.replace(replacement, target)
    return raw


def _assert_prepared(profile: _Profile, old_runtime: bytes) -> None:
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert _schema(deployed_upgrade.staged_runtime_path()) == 4
    assert config.load_strict().roots == (profile.root,)
    adoption = adopted.load(strict=True)
    assert adoption is not None
    assert adoption.root == profile.root
    assert (
        tuple((authority.source_path, authority.capture_path) for authority in adoption.authorities)
        == profile.entries
    )
    assert deployed_upgrade.probe().status == "prepared"


def _assert_complete(profile: _Profile, *, recovery_artifacts: bool = False) -> None:
    assert _schema(paths.runtime_config_path()) == 4
    assert config.load_strict().roots == (profile.root,)
    assert deployed_upgrade.probe().status == "complete"
    adoption = adopted.load(strict=True)
    assert adoption is not None
    assert adoption.root == profile.root
    library = scan.scan((profile.root,), include_workshop=False)
    assert {item.path for item in library.items} == {source for source, _capture in profile.entries}
    assert {(item.path, item.paired_still) for item in library.items} == set(profile.entries)
    if not recovery_artifacts:
        assert not deployed_upgrade.journal_path().exists()
        assert not deployed_upgrade.staged_runtime_path().exists()


def test_prepare_keeps_public_schema2_and_builds_private_schema4_without_decoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch, videos=2, named_playlist=True)
    old_runtime = paths.runtime_config_path().read_bytes()
    monkeypatch.setattr(stills, "generate", _forbid_decoder)
    monkeypatch.setattr(stills, "capture_scene", _forbid_decoder)
    monkeypatch.setattr(scenes, "screenshot", _forbid_decoder)

    outcome = deployed_upgrade_transaction.ensure(cutover=False)

    assert outcome.changed
    assert outcome.status == "prepared"
    _assert_prepared(profile, old_runtime)


def test_cutover_installs_schema4_and_hides_adopted_basename_captures_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch, videos=2, named_playlist=True)
    monkeypatch.setattr(stills, "generate", _forbid_decoder)
    monkeypatch.setattr(stills, "capture_scene", _forbid_decoder)
    monkeypatch.setattr(scenes, "screenshot", _forbid_decoder)

    first = deployed_upgrade_transaction.ensure()
    completed = _snapshot()
    second = deployed_upgrade_transaction.ensure()

    assert first.changed and first.status == "complete"
    assert not second.changed and second.status == "complete"
    assert _snapshot() == completed
    _assert_complete(profile)


def test_absent_and_current_profiles_do_not_publish_migration_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", _forbid_decoder)
    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_install_exact_replacement",
        _forbid_decoder,
    )
    absent_before = _snapshot()
    absent = deployed_upgrade_transaction.ensure()
    assert absent == deployed_upgrade_transaction.Outcome(
        False,
        "absent",
        "no Wall-in-One settings have been created",
    )
    assert _snapshot() == absent_before
    assert not (tmp_path / "config").exists()
    assert not (tmp_path / "state").exists()

    config.save(config.Settings())
    current_before = _snapshot()
    current = deployed_upgrade_transaction.ensure()
    assert not current.changed and current.status == "current"
    assert _snapshot() == current_before


@pytest.mark.parametrize("target", ("settings", "runtime"))
@pytest.mark.parametrize(
    "slot_ordinal",
    range(deployed_upgrade_transaction.MAX_CLAIM_TOKEN_ATTEMPTS),
)
def test_ready_status_rejects_every_bounded_unjournaled_claim_slot_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    slot_ordinal: int,
) -> None:
    _profile(tmp_path, monkeypatch)
    journal = deployed_upgrade_transaction._journal_from_plan(deployed_upgrade.build_plan())
    if target == "settings":
        public_path = journal.settings_path
        tokens = deployed_upgrade_transaction._claim_tokens(journal.settings_token)
    else:
        public_path = journal.runtime_path
        tokens = deployed_upgrade_transaction._claim_tokens(journal.runtime_token)
    claim = deployed_upgrade_transaction._claim_path(public_path, tokens[slot_ordinal])
    claim.mkdir(parents=True)
    before = _snapshot()

    found = deployed_upgrade_transaction.probe()

    assert found.status == "conflict"
    assert f"reserved {target} claim path already exists before migration" in found.detail
    assert str(claim) in found.detail
    assert _snapshot() == before
    assert claim.is_dir()
    assert tuple(claim.iterdir()) == ()
    assert not deployed_upgrade.journal_path().exists()

    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure()

    assert raised.value.status == "conflict"
    assert found.detail == str(raised.value)
    assert str(claim) in str(raised.value)
    assert _snapshot() == before
    assert claim.is_dir()
    assert tuple(claim.iterdir()) == ()
    assert not deployed_upgrade.journal_path().exists()


@pytest.mark.parametrize("target", ("settings", "runtime"))
@pytest.mark.parametrize(
    "shape",
    ("regular", "broken-symlink", "directory-symlink", "populated", "wrong-mode"),
)
def test_ready_status_preserves_every_unjournaled_claim_path_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    shape: str,
) -> None:
    _profile(tmp_path, monkeypatch)
    journal = deployed_upgrade_transaction._journal_from_plan(deployed_upgrade.build_plan())
    if target == "settings":
        public_path = journal.settings_path
        token = journal.settings_token
    else:
        public_path = journal.runtime_path
        token = journal.runtime_token
    claim = deployed_upgrade_transaction._claim_path(public_path, token)
    claim.parent.mkdir(parents=True, exist_ok=True)
    symlink_target = tmp_path / f"{target}-foreign-claim-target"
    if shape == "regular":
        claim.write_bytes(b"foreign claim bytes\n")
    elif shape == "broken-symlink":
        claim.symlink_to(symlink_target)
    elif shape == "directory-symlink":
        symlink_target.mkdir()
        (symlink_target / "sentinel").write_bytes(b"foreign target\n")
        claim.symlink_to(symlink_target, target_is_directory=True)
    else:
        claim.mkdir()
        if shape == "populated":
            (claim / "foreign-entry").write_bytes(b"foreign entry\n")
        else:
            claim.chmod(0o755)

    def evidence(path: Path) -> tuple[int, bytes | str | tuple[tuple[str, bytes], ...]]:
        status = path.lstat()
        mode = stat.S_IMODE(status.st_mode) | stat.S_IFMT(status.st_mode)
        if stat.S_ISLNK(status.st_mode):
            payload: bytes | str | tuple[tuple[str, bytes], ...] = os.readlink(path)
        elif stat.S_ISREG(status.st_mode):
            payload = path.read_bytes()
        else:
            payload = tuple(
                (child.name, child.read_bytes())
                for child in sorted(path.iterdir())
                if child.is_file() and not child.is_symlink()
            )
        return mode, payload

    claim_before = evidence(claim)
    target_before = evidence(symlink_target) if symlink_target.exists() else None
    documents_before = _snapshot()

    found = deployed_upgrade_transaction.probe()
    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure()

    assert found.status == "conflict"
    assert raised.value.status == "conflict"
    assert found.detail == str(raised.value)
    assert str(claim) in found.detail
    assert evidence(claim) == claim_before
    assert (evidence(symlink_target) if symlink_target.exists() else None) == target_before
    assert _snapshot() == documents_before
    assert not deployed_upgrade.journal_path().exists()


@pytest.mark.parametrize("target", ("settings", "runtime"))
def test_journaled_public_predecessor_conflicts_when_every_claim_slot_is_occupied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    _profile(tmp_path, monkeypatch)
    original = deployed_upgrade_transaction._publish_capture_sidecars

    def interrupt_before_sidecars(_journal: deployed_upgrade_transaction._Journal) -> bool:
        raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_publish_capture_sidecars",
        interrupt_before_sidecars,
    )
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_capture_sidecars", original)

    journal_record = deployed_upgrade_transaction._read_journal()
    assert journal_record is not None
    journal, _raw = journal_record
    if target == "settings":
        public_path = journal.settings_path
        tokens = deployed_upgrade_transaction._claim_tokens(journal.settings_token)
    else:
        public_path = journal.runtime_path
        tokens = deployed_upgrade_transaction._claim_tokens(journal.runtime_token)
    claims = tuple(deployed_upgrade_transaction._claim_path(public_path, token) for token in tokens)
    for claim in claims:
        claim.mkdir(mode=0o700, parents=True)
    before = _snapshot()

    found = deployed_upgrade_transaction.probe()
    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure()

    assert found.status == "conflict"
    assert raised.value.status == "conflict"
    assert found.detail == str(raised.value)
    assert f"all {deployed_upgrade_transaction.MAX_CLAIM_TOKEN_ATTEMPTS}" in found.detail
    assert target in found.detail
    assert _snapshot() == before
    assert all(claim.is_dir() and tuple(claim.iterdir()) == () for claim in claims)


def test_additional_current_media_conflicts_before_any_migration_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    extra = profile.root / "later-user-image.jpg"
    extra.write_bytes(b"user image")
    before = _snapshot()
    root_before = {
        path: path.read_bytes()
        for path in profile.root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }

    found = deployed_upgrade_transaction.probe()
    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure(cutover=False)

    assert found.status == "conflict"
    assert "additional, missing, or reordered logical media" in found.detail
    assert raised.value.status == "conflict"
    assert _snapshot() == before
    assert {
        path: path.read_bytes()
        for path in profile.root.rglob("*")
        if path.is_file() and not path.is_symlink()
    } == root_before
    assert config.load_strict().roots == ()
    assert not deployed_upgrade.journal_path().exists()
    assert not deployed_upgrade.authority_path().exists()
    assert not deployed_upgrade.staged_runtime_path().exists()


@pytest.mark.parametrize("relationship", ("same-bytes", "same-inode"))
def test_existing_hashed_capture_is_never_overwritten_or_adopted_by_guessing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relationship: str,
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    source, predecessor_capture = profile.entries[0]
    hashed_capture = stills.destination(source, profile.root)
    assert hashed_capture != predecessor_capture
    if relationship == "same-bytes":
        hashed_capture.write_bytes(predecessor_capture.read_bytes())
    else:
        hashed_capture.hardlink_to(predecessor_capture)
    before = {
        path: (path.read_bytes(), file_io.file_fingerprint(path.lstat()))
        for path in (predecessor_capture, hashed_capture, paths.runtime_config_path())
    }

    found = deployed_upgrade_transaction.probe()
    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure()

    assert found.status in ("conflict", "corrupt")
    assert raised.value.status == found.status
    assert {
        path: (path.read_bytes(), file_io.file_fingerprint(path.lstat())) for path in before
    } == before
    assert not deployed_upgrade.journal_path().exists()
    assert not deployed_upgrade.authority_path().exists()
    assert not deployed_upgrade.staged_runtime_path().exists()


@pytest.mark.parametrize(
    "filename",
    (
        deployed_upgrade.JOURNAL_FILENAME,
        deployed_upgrade.AUTHORITY_FILENAME,
        deployed_upgrade.STAGED_RUNTIME_FILENAME,
        deployed_upgrade.COMPLETION_FILENAME,
    ),
)
def test_random_reserved_artifact_is_corrupt_not_a_resume_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    paths.app_state_dir().mkdir(parents=True)
    (paths.app_state_dir() / filename).write_text("{}\n", encoding="utf-8")
    found = deployed_upgrade_transaction.probe()
    assert found.status == "corrupt"


def test_transaction_publication_refuses_symlinked_parent(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    logical_parent = tmp_path / "state" / "wall-in-one"
    logical_parent.parent.mkdir()
    logical_parent.symlink_to(outside, target_is_directory=True)
    target = logical_parent / "journal.json"

    with pytest.raises(
        deployed_upgrade_transaction.TransactionError,
        match=r"parent path.*is not a directory",
    ):
        deployed_upgrade_transaction._write_no_replace(
            target,
            b"{}\n",
            64,
            label="test journal",
        )

    assert not (outside / target.name).exists()


def test_different_existing_capture_sidecar_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    _source, capture = profile.entries[0]
    sidecar = capture.with_name(capture.name + pairing.SIDECAR_SUFFIX)
    sidecar.write_bytes(b"user-owned-sidecar\n")

    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure(cutover=False)

    assert raised.value.status == "conflict"
    assert sidecar.read_bytes() == b"user-owned-sidecar\n"
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert not deployed_upgrade.journal_path().exists()


def test_malformed_orphan_authority_is_corrupt_and_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    deployed_upgrade.authority_path().write_bytes(b"foreign\n")

    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure()

    assert raised.value.status == "corrupt"
    assert deployed_upgrade.authority_path().read_bytes() == b"foreign\n"
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert not deployed_upgrade.journal_path().exists()


def test_live_authoring_socket_requests_retry_before_journal_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile(tmp_path, monkeypatch)
    target = paths.socket_path()
    target.parent.mkdir(parents=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(target))
    server.listen(1)
    try:
        with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
            deployed_upgrade_transaction.ensure(cutover=False)
    finally:
        server.close()
        target.unlink(missing_ok=True)
    assert raised.value.status == "retry"
    assert not deployed_upgrade.journal_path().exists()


def test_live_predecessor_runtime_socket_requests_retry_before_journal_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile(tmp_path, monkeypatch)
    with tempfile.TemporaryDirectory(prefix="wio-runtime-", dir="/tmp") as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        target = paths.runtime_socket_path()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(target))
        server.listen(16)
        try:
            with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
                deployed_upgrade_transaction.ensure(cutover=False)
        finally:
            server.close()
            target.unlink(missing_ok=True)
    assert raised.value.status == "retry"
    assert "wallpaper runtime process is still running" in str(raised.value)
    assert not deployed_upgrade.journal_path().exists()


@pytest.mark.parametrize(
    ("socket_path", "owner"),
    (
        (paths.socket_path, "authoring"),
        (paths.runtime_socket_path, "wallpaper runtime"),
    ),
)
def test_held_current_writer_lock_refuses_migration_even_without_a_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    socket_path: Callable[[], Path],
    owner: str,
) -> None:
    _profile(tmp_path, monkeypatch)
    target = socket_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f"{target.name}.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    os.fchmod(descriptor, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
            deployed_upgrade_transaction.ensure(cutover=False)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    assert raised.value.status == "retry"
    assert owner in str(raised.value)
    assert not deployed_upgrade.journal_path().exists()


def test_migration_retains_both_current_writer_locks_through_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile(tmp_path, monkeypatch)
    original = deployed_upgrade_transaction._publish_capture_sidecars
    observed = False

    def assert_excluded(journal: deployed_upgrade_transaction._Journal) -> bool:
        nonlocal observed
        for target in (paths.socket_path(), paths.runtime_socket_path()):
            lock_path = target.with_name(f"{target.name}.lock")
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
        observed = True
        return original(journal)

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_publish_capture_sidecars",
        assert_excluded,
    )

    assert deployed_upgrade_transaction.ensure(cutover=False).status == "prepared"
    assert observed


def test_ready_status_reports_a_lock_unaware_predecessor_as_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile(tmp_path, monkeypatch)

    def live_predecessor(**_arguments: object) -> None:
        raise predecessor_process.PredecessorProcessError(
            "fixture predecessor is alive before bind"
        )

    monkeypatch.setattr(
        predecessor_process,
        "refuse_live_predecessor_runtime",
        live_predecessor,
    )

    found = deployed_upgrade_transaction.probe()
    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure()

    assert found.status == "in-progress"
    assert "eligible but blocked" in found.detail
    assert raised.value.status == "retry"
    assert "alive before bind" in str(raised.value)
    assert not deployed_upgrade.journal_path().exists()


@pytest.mark.parametrize(
    ("prepare_first", "expected_status"),
    ((False, "ready"), (True, "prepared")),
)
def test_status_uses_only_observational_writer_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepare_first: bool,
    expected_status: str,
) -> None:
    _profile(tmp_path, monkeypatch)
    if prepare_first:
        assert deployed_upgrade_transaction.ensure(cutover=False).status == "prepared"

    observed: list[str] = []

    def observe_status(**_arguments: object) -> None:
        observed.append("locks-and-sockets")

    def observe_process(**_arguments: object) -> None:
        observed.append("predecessor-process")

    def forbid_active_probe(*_arguments: object, **_keywords: object) -> None:
        raise AssertionError("read-only status attempted a connect or trial flock")

    monkeypatch.setattr(
        predecessor_process,
        "refuse_live_writer_status",
        observe_status,
    )
    monkeypatch.setattr(
        predecessor_process,
        "refuse_live_predecessor_runtime",
        observe_process,
    )
    monkeypatch.setattr(socket, "socket", forbid_active_probe)
    monkeypatch.setattr(fcntl, "flock", forbid_active_probe)

    found = deployed_upgrade_transaction.probe()

    assert found.status == expected_status
    assert observed == ["locks-and-sockets", "predecessor-process"]


def test_prepared_upgrade_resumes_after_same_root_noctalia_atomic_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch, videos=2, named_playlist=True)
    old_runtime = paths.runtime_config_path().read_bytes()
    prepared = deployed_upgrade_transaction.ensure(cutover=False)
    assert prepared.status == "prepared"

    replacement = _replace_noctalia_settings(profile.root)

    # Status must perform the same semantic authority check as resume.  An
    # inode replacement caused by a normal wallpaper/theme update is safe only
    # because its effective root remains exact.
    found = deployed_upgrade_transaction.probe()
    assert found.status == "prepared"
    assert paths.runtime_config_path().read_bytes() == old_runtime

    resumed = deployed_upgrade_transaction.ensure()

    assert resumed.status == "complete"
    assert paths.noctalia_settings_path().read_bytes() == replacement
    _assert_complete(profile)


def test_prepared_status_is_not_green_while_predecessor_runtime_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    assert deployed_upgrade_transaction.ensure(cutover=False).status == "prepared"

    with tempfile.TemporaryDirectory(prefix="wio-runtime-", dir="/tmp") as runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
        target = paths.runtime_socket_path()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(target))
        server.listen(16)
        try:
            found = deployed_upgrade_transaction.probe()
            with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
                deployed_upgrade_transaction.ensure()
        finally:
            server.close()
            target.unlink(missing_ok=True)

    assert found.status == "in-progress"
    assert "prepared but blocked" in found.detail
    assert raised.value.status == "retry"
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert not deployed_upgrade.completion_path().exists()


def test_prepared_status_and_resume_observe_a_real_external_runtime_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    assert deployed_upgrade_transaction.ensure(cutover=False).status == "prepared"
    runtime_socket = paths.runtime_socket_path()
    lock = runtime_socket.with_name(f"{runtime_socket.name}.lock")

    with _external_flock(lock):
        found = deployed_upgrade_transaction.probe()
        with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
            deployed_upgrade_transaction.ensure()

    assert found.status == "in-progress"
    assert "prepared but blocked" in found.detail
    assert "still owns singleton guard" in found.detail
    assert raised.value.status == "retry"
    assert "wallpaper runtime process still owns" in str(raised.value)
    assert str(lock) in str(raised.value)
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert deployed_upgrade.staged_runtime_path().exists()
    assert not deployed_upgrade.completion_path().exists()


def test_prepared_status_and_resume_fail_closed_when_noctalia_root_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    assert deployed_upgrade_transaction.ensure(cutover=False).status == "prepared"
    before = _snapshot()
    different = tmp_path / "different-wallpapers"
    different.mkdir()

    replacement = _replace_noctalia_settings(profile.root, directory=different)

    found = deployed_upgrade_transaction.probe()
    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure()

    expected_diagnostic = (
        "Noctalia's wallpaper directory changed during the deployed-profile upgrade "
        f"from {profile.root} to {different}; restore it to {profile.root} and retry"
    )
    assert found.status == "conflict"
    assert expected_diagnostic in found.detail
    assert raised.value.status == "conflict"
    assert expected_diagnostic in str(raised.value)
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert _snapshot() == before
    assert paths.noctalia_settings_path().read_bytes() == replacement
    assert deployed_upgrade.journal_path().exists()
    assert deployed_upgrade.staged_runtime_path().exists()

    restored = _replace_noctalia_settings(profile.root)
    recovered = deployed_upgrade_transaction.ensure()

    assert recovered.status == "complete"
    assert paths.noctalia_settings_path().read_bytes() == restored
    assert _schema(paths.runtime_config_path()) == 4
    assert deployed_upgrade_transaction.probe().status == "complete"


def test_prepared_status_revalidates_the_public_settings_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    assert deployed_upgrade_transaction.ensure(cutover=False).status == "prepared"
    replacement = config.Settings(roots=(tmp_path / "other",)).to_toml().encode()
    paths.settings_path().write_bytes(replacement)

    found = deployed_upgrade_transaction.probe()
    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure()

    assert found.status == "conflict"
    assert "old settings is retained in its durable claim" in found.detail
    assert raised.value.status == "conflict"
    assert paths.settings_path().read_bytes() == replacement
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert profile.root != tmp_path / "other"


def test_prepared_status_is_not_green_for_malformed_noctalia_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    assert deployed_upgrade_transaction.ensure(cutover=False).status == "prepared"
    replacement = paths.noctalia_settings_path().with_name(".settings.toml.next")
    replacement.write_text("[wallpaper\n", encoding="utf-8")
    os.replace(replacement, paths.noctalia_settings_path())

    found = deployed_upgrade_transaction.probe()
    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure()

    assert found.status == "corrupt"
    assert "no longer proves its wallpaper directory" in found.detail
    assert raised.value.status == "corrupt"
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert deployed_upgrade.journal_path().exists()


def test_resume_after_journal_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    original = deployed_upgrade_transaction._publish_capture_sidecars

    def interrupt(_journal: object) -> bool:
        raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_capture_sidecars", interrupt)
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    assert deployed_upgrade.journal_path().exists()
    assert paths.runtime_config_path().read_bytes() == old_runtime

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_capture_sidecars", original)
    resumed = deployed_upgrade_transaction.ensure(cutover=False)
    assert resumed.status == "prepared"
    _assert_prepared(profile, old_runtime)


@pytest.mark.parametrize(
    ("artifact", "message"),
    (("source", "video source changed"), ("capture", "changed generation")),
)
def test_journal_only_status_revalidates_capture_publication_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
    message: str,
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    original = deployed_upgrade_transaction._publish_capture_sidecars

    def interrupt(_journal: object) -> bool:
        raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_capture_sidecars", interrupt)
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_capture_sidecars", original)

    source, capture = profile.entries[0]
    changed = source if artifact == "source" else capture
    changed.write_bytes(changed.read_bytes() + b"drift")
    before = _snapshot()

    found = deployed_upgrade_transaction.probe()

    assert found.status == "corrupt"
    assert message in found.detail
    assert _snapshot() == before


def test_journal_only_status_rejects_a_foreign_capture_sidecar_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    original = deployed_upgrade_transaction._publish_capture_sidecars

    def interrupt(_journal: object) -> bool:
        raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_capture_sidecars", interrupt)
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_capture_sidecars", original)

    _source, capture = profile.entries[0]
    sidecar = capture.with_name(capture.name + pairing.SIDECAR_SUFFIX)
    sidecar.write_bytes(b"foreign sidecar\n")
    before = _snapshot()

    found = deployed_upgrade_transaction.probe()

    assert found.status == "conflict"
    assert "does not match the journal" in found.detail
    assert sidecar.read_bytes() == b"foreign sidecar\n"
    assert _snapshot() == before


def test_resume_rejects_authoring_generation_changed_after_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _profile(tmp_path, monkeypatch, named_playlist=True)
    old_runtime = paths.runtime_config_path().read_bytes()
    original = deployed_upgrade_transaction._publish_capture_sidecars

    def interrupt(_journal: object) -> bool:
        raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_capture_sidecars", interrupt)
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    playlist_state = playlists.state_path()
    playlist_state.write_text(
        playlist_state.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_publish_capture_sidecars",
        original,
    )

    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure(cutover=False)

    assert raised.value.status == "corrupt"
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert deployed_upgrade.journal_path().exists()


def test_resume_after_capture_sidecar_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch, videos=2)
    old_runtime = paths.runtime_config_path().read_bytes()
    original = deployed_upgrade_transaction._write_no_replace

    def interrupt_after_first_sidecar(
        path: Path,
        contents: bytes,
        maximum: int,
        *,
        label: str,
        create_parent: bool = True,
    ) -> bool:
        changed = original(
            path,
            contents,
            maximum,
            label=label,
            create_parent=create_parent,
        )
        if path.name.endswith(pairing.SIDECAR_SUFFIX):
            raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")
        return changed

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_write_no_replace",
        interrupt_after_first_sidecar,
    )
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    assert config.load_strict().roots == ()
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert (
        sum(
            capture.with_name(capture.name + pairing.SIDECAR_SUFFIX).is_file()
            for _source, capture in profile.entries
        )
        == 1
    )

    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", original)
    resumed = deployed_upgrade_transaction.ensure(cutover=False)
    assert resumed.status == "prepared"
    _assert_prepared(profile, old_runtime)


def test_empty_durable_claim_slot_advances_without_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    original_move = file_io.atomic_move_no_replace
    interrupted = False

    def interrupt_after_claim_directory(
        source: Path,
        destination: Path,
        *,
        expected_identity: file_io.PathIdentity,
        expected_file_type: int = stat.S_IFREG,
        expected_fingerprint: file_io.FileFingerprint | None = None,
        pinned_source: file_io.PinnedPath | None = None,
        externally_pinned: bool = False,
    ) -> None:
        nonlocal interrupted
        if not interrupted and source.name == "settings.toml" and destination.name == "entry":
            interrupted = True
            raise OSError("interrupted before durable claim move")
        original_move(
            source,
            destination,
            expected_identity=expected_identity,
            expected_file_type=expected_file_type,
            expected_fingerprint=expected_fingerprint,
            pinned_source=pinned_source,
            externally_pinned=externally_pinned,
        )

    monkeypatch.setattr(
        file_io,
        "atomic_move_no_replace",
        interrupt_after_claim_directory,
    )
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    journal_record = deployed_upgrade_transaction._read_journal()
    assert journal_record is not None
    journal, _raw = journal_record
    first_slot = deployed_upgrade_transaction._claim_path(
        paths.settings_path(),
        journal.settings_token,
    )
    assert first_slot.is_dir()
    assert tuple(first_slot.iterdir()) == ()
    assert config.load_strict().roots == ()

    monkeypatch.setattr(
        file_io,
        "atomic_move_no_replace",
        original_move,
    )
    resumed = deployed_upgrade_transaction.ensure(cutover=False)

    assert resumed.status == "prepared"
    assert first_slot.is_dir()
    assert tuple(first_slot.iterdir()) == ()
    assert config.load_strict().roots == (profile.root,)
    second_token = deployed_upgrade_transaction._claim_tokens(journal.settings_token)[1]
    retained_predecessor = (
        deployed_upgrade_transaction._claim_path(paths.settings_path(), second_token) / "entry"
    )
    assert "roots = []" in retained_predecessor.read_text(encoding="utf-8")


def test_empty_runtime_claim_slot_remains_resumable_and_advances_without_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    original_move = file_io.atomic_move_no_replace
    interrupted = False

    def interrupt_after_runtime_claim_directory(
        source: Path,
        destination: Path,
        *,
        expected_identity: file_io.PathIdentity,
        expected_file_type: int = stat.S_IFREG,
        expected_fingerprint: file_io.FileFingerprint | None = None,
        pinned_source: file_io.PinnedPath | None = None,
        externally_pinned: bool = False,
    ) -> None:
        nonlocal interrupted
        if not interrupted and source.name == "runtime.toml" and destination.name == "entry":
            interrupted = True
            raise OSError("interrupted before durable runtime claim move")
        original_move(
            source,
            destination,
            expected_identity=expected_identity,
            expected_file_type=expected_file_type,
            expected_fingerprint=expected_fingerprint,
            pinned_source=pinned_source,
            externally_pinned=externally_pinned,
        )

    monkeypatch.setattr(file_io, "atomic_move_no_replace", interrupt_after_runtime_claim_directory)
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure()
    journal_record = deployed_upgrade_transaction._read_journal()
    assert journal_record is not None
    journal, _raw = journal_record
    first_slot = deployed_upgrade_transaction._claim_path(
        paths.runtime_config_path(),
        journal.runtime_token,
    )
    assert first_slot.is_dir()
    assert tuple(first_slot.iterdir()) == ()
    assert deployed_upgrade_transaction.probe().status == "prepared"

    monkeypatch.setattr(file_io, "atomic_move_no_replace", original_move)
    resumed = deployed_upgrade_transaction.ensure()

    assert resumed.status == "complete"
    assert first_slot.is_dir()
    assert tuple(first_slot.iterdir()) == ()
    assert config.load_strict().roots == (profile.root,)
    second_token = deployed_upgrade_transaction._claim_tokens(journal.runtime_token)[1]
    retained_predecessor = (
        deployed_upgrade_transaction._claim_path(paths.runtime_config_path(), second_token)
        / "entry"
    )
    assert _schema(retained_predecessor) == 2


def test_resume_after_old_settings_are_claimed_before_new_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    original = deployed_upgrade_transaction._write_no_replace_access
    interrupted = False

    def interrupt_settings_publication(
        logical: Path,
        access: Path,
        contents: bytes,
        maximum: int,
        *,
        label: str,
        context: file_io.PinnedDirectoryContext,
    ) -> bool:
        nonlocal interrupted
        if not interrupted and logical == paths.settings_path():
            interrupted = True
            raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")
        return original(
            logical,
            access,
            contents,
            maximum,
            label=label,
            context=context,
        )

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_write_no_replace_access",
        interrupt_settings_publication,
    )
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    assert not paths.settings_path().exists()
    assert deployed_upgrade.journal_path().exists()

    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace_access", original)
    resumed = deployed_upgrade_transaction.ensure(cutover=False)

    assert resumed.status == "prepared"
    assert config.load_strict().roots == (profile.root,)


def test_resume_after_settings_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    original = deployed_upgrade_transaction._publish_authority_manifest

    def interrupt(_journal: object) -> bool:
        raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_authority_manifest", interrupt)
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    assert config.load_strict().roots == (profile.root,)
    assert paths.runtime_config_path().read_bytes() == old_runtime

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_authority_manifest", original)
    resumed = deployed_upgrade_transaction.ensure(cutover=False)
    assert resumed.status == "prepared"
    _assert_prepared(profile, old_runtime)


def test_resume_after_authority_manifest_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    original = deployed_upgrade_transaction._publish_authority_manifest

    def interrupt(journal: deployed_upgrade_transaction._Journal) -> bool:
        original(journal)
        raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_authority_manifest", interrupt)
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    assert config.load_strict().roots == (profile.root,)
    assert deployed_upgrade.authority_path().is_file()
    assert not deployed_upgrade.staged_runtime_path().exists()
    assert paths.runtime_config_path().read_bytes() == old_runtime

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_authority_manifest", original)
    resumed = deployed_upgrade_transaction.ensure(cutover=False)
    assert resumed.status == "prepared"
    _assert_prepared(profile, old_runtime)


def test_stage_absent_status_revalidates_the_public_settings_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    original = deployed_upgrade_transaction._publish_authority_manifest

    def interrupt(journal: deployed_upgrade_transaction._Journal) -> bool:
        original(journal)
        raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_authority_manifest", interrupt)
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_authority_manifest", original)
    replacement = config.Settings(roots=(tmp_path / "other",)).to_toml().encode()
    paths.settings_path().write_bytes(replacement)

    found = deployed_upgrade_transaction.probe()
    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure()

    assert found.status == "conflict"
    assert "old settings is retained in its durable claim" in found.detail
    assert raised.value.status == "conflict"
    assert paths.settings_path().read_bytes() == replacement
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert not deployed_upgrade.staged_runtime_path().exists()


def test_resume_after_staged_runtime_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    original = deployed_upgrade_transaction._write_no_replace

    def interrupt_stage(
        path: Path,
        contents: bytes,
        maximum: int,
        *,
        label: str,
        create_parent: bool = True,
    ) -> bool:
        changed = original(
            path,
            contents,
            maximum,
            label=label,
            create_parent=create_parent,
        )
        if path == deployed_upgrade.staged_runtime_path():
            raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")
        return changed

    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", interrupt_stage)
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure()
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert _schema(deployed_upgrade.staged_runtime_path()) == 4
    assert deployed_upgrade.probe().status == "prepared"

    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", original)
    resumed = deployed_upgrade_transaction.ensure()
    assert resumed.status == "complete"
    _assert_complete(profile)


def test_prepared_status_rejects_a_missing_runtime_without_an_exact_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile(tmp_path, monkeypatch)
    assert deployed_upgrade_transaction.ensure(cutover=False).status == "prepared"
    paths.runtime_config_path().unlink()

    found = deployed_upgrade_transaction.probe()
    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure()

    assert found.status == "conflict"
    assert "missing without its journaled old claim" in found.detail
    assert raised.value.status == "conflict"
    assert not paths.runtime_config_path().exists()
    assert not deployed_upgrade.completion_path().exists()


def test_predecessor_appearing_at_the_cutover_recheck_preserves_schema2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    appeared = False

    def appear_before_cas(**_arguments: object) -> None:
        nonlocal appeared
        if deployed_upgrade.staged_runtime_path().exists() and not appeared:
            appeared = True
            raise predecessor_process.PredecessorProcessError(
                "fixture predecessor appeared before runtime CAS"
            )

    monkeypatch.setattr(
        predecessor_process,
        "refuse_live_predecessor_runtime",
        appear_before_cas,
    )

    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure()

    assert raised.value.status == "retry"
    assert "appeared before runtime CAS" in str(raised.value)
    assert appeared
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert deployed_upgrade.staged_runtime_path().exists()
    assert not deployed_upgrade.completion_path().exists()


@pytest.mark.parametrize("artifact", ("source", "capture", "sidecar"))
def test_cutover_reproves_capture_generations_after_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    source, capture = profile.entries[0]
    sidecar = capture.with_name(capture.name + pairing.SIDECAR_SUFFIX)
    original = deployed_upgrade_transaction._validate_cutover_snapshot
    drifted = False

    def drift_before_cas(
        journal: deployed_upgrade_transaction._Journal,
        journal_raw: bytes,
        stage: bytes,
    ) -> None:
        nonlocal drifted
        if not drifted:
            drifted = True
            if artifact == "source":
                source.write_bytes(source.read_bytes() + b"source drift")
            elif artifact == "capture":
                capture.write_bytes(capture.read_bytes() + b"capture drift")
            else:
                sidecar.write_bytes(b"foreign replacement\n")
        original(journal, journal_raw, stage)

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_validate_cutover_snapshot",
        drift_before_cas,
    )

    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure()

    assert drifted
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert not deployed_upgrade.completion_path().exists()


def test_resume_after_runtime_cutover_before_completion_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    original = deployed_upgrade_transaction._write_no_replace

    def interrupt_completion(
        path: Path,
        contents: bytes,
        maximum: int,
        *,
        label: str,
        create_parent: bool = True,
    ) -> bool:
        if path == deployed_upgrade.completion_path():
            raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")
        return original(
            path,
            contents,
            maximum,
            label=label,
            create_parent=create_parent,
        )

    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", interrupt_completion)
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure()
    assert _schema(paths.runtime_config_path()) == 4
    assert deployed_upgrade.journal_path().exists()
    assert deployed_upgrade.staged_runtime_path().exists()

    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", original)
    resumed = deployed_upgrade_transaction.ensure()
    assert resumed.status == "complete"
    _assert_complete(profile)


def test_completion_publication_is_a_durable_idempotency_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    original = deployed_upgrade_transaction._write_no_replace

    def interrupt_after_completion(
        path: Path,
        contents: bytes,
        maximum: int,
        *,
        label: str,
        create_parent: bool = True,
    ) -> bool:
        changed = original(
            path,
            contents,
            maximum,
            label=label,
            create_parent=create_parent,
        )
        if path == deployed_upgrade.completion_path():
            raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")
        return changed

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_write_no_replace",
        interrupt_after_completion,
    )
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure()
    assert deployed_upgrade.journal_path().is_file()
    assert deployed_upgrade.staged_runtime_path().is_file()
    _assert_complete(profile, recovery_artifacts=True)

    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", original)
    resumed = deployed_upgrade_transaction.ensure()
    assert not resumed.changed
    assert resumed.status == "complete"
    _assert_complete(profile, recovery_artifacts=True)


def test_surviving_completion_journal_is_historical_after_changed_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _profile(tmp_path, monkeypatch)
    original = deployed_upgrade_transaction._write_no_replace

    def interrupt_after_completion(
        path: Path,
        contents: bytes,
        maximum: int,
        *,
        label: str,
        create_parent: bool = True,
    ) -> bool:
        changed = original(
            path,
            contents,
            maximum,
            label=label,
            create_parent=create_parent,
        )
        if path == deployed_upgrade.completion_path():
            raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")
        return changed

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_write_no_replace",
        interrupt_after_completion,
    )
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure()
    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", original)
    paths.settings_path().write_text(
        paths.settings_path().read_text(encoding="utf-8") + "# concurrent edit\n",
        encoding="utf-8",
    )
    edited_settings = paths.settings_path().read_bytes()

    resumed = deployed_upgrade_transaction.ensure()

    assert not resumed.changed
    assert resumed.status == "complete"
    assert paths.settings_path().read_bytes() == edited_settings
    assert not deployed_upgrade.journal_path().exists()
    assert not deployed_upgrade.staged_runtime_path().exists()
    assert deployed_upgrade.completion_path().exists()


def test_interrupted_completion_cleanup_does_not_bind_later_user_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _profile(tmp_path, monkeypatch, named_playlist=True)
    original_discard = deployed_upgrade_transaction._discard_exact_artifact

    def leave_recovery_artifact(
        _path: Path,
        _contents: bytes,
        _maximum: int,
        *,
        label: str,
    ) -> None:
        del label

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_discard_exact_artifact",
        leave_recovery_artifact,
    )
    completed = deployed_upgrade_transaction.ensure()
    assert completed.status == "complete"
    assert deployed_upgrade.journal_path().is_file()
    assert deployed_upgrade.staged_runtime_path().is_file()

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_discard_exact_artifact",
        original_discard,
    )
    paths.settings_path().write_text(
        paths.settings_path().read_text(encoding="utf-8") + "# later settings edit\n",
        encoding="utf-8",
    )
    paths.runtime_config_path().write_text(
        paths.runtime_config_path().read_text(encoding="utf-8") + "# later runtime edit\n",
        encoding="utf-8",
    )
    playlist_store = playlists.Store.open()
    playlist_store.rename("saved", "Saved later")
    edited_settings = paths.settings_path().read_bytes()
    edited_runtime = paths.runtime_config_path().read_bytes()
    edited_playlists = playlists.state_path().read_bytes()

    resumed = deployed_upgrade_transaction.ensure()

    assert not resumed.changed
    assert resumed.status == "complete"
    assert paths.settings_path().read_bytes() == edited_settings
    assert paths.runtime_config_path().read_bytes() == edited_runtime
    assert playlists.state_path().read_bytes() == edited_playlists
    assert not deployed_upgrade.journal_path().exists()
    assert not deployed_upgrade.staged_runtime_path().exists()
    assert deployed_upgrade.completion_path().is_file()


@pytest.mark.parametrize("artifact", ("stage", "journal"))
def test_completed_cleanup_never_discards_residue_replaced_after_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
) -> None:
    _profile(tmp_path, monkeypatch)
    original_discard = deployed_upgrade_transaction._discard_exact_artifact

    def leave_recovery_artifact(
        _path: Path,
        _contents: bytes,
        _maximum: int,
        *,
        label: str,
    ) -> None:
        del label

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_discard_exact_artifact",
        leave_recovery_artifact,
    )
    assert deployed_upgrade_transaction.ensure().status == "complete"
    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_discard_exact_artifact",
        original_discard,
    )

    target = (
        deployed_upgrade.staged_runtime_path()
        if artifact == "stage"
        else deployed_upgrade.journal_path()
    )
    replacement = b"unrelated private residue replacement\n"
    original_probe = deployed_upgrade_transaction.probe
    calls = 0

    def replace_after_locked_probe() -> deployed_upgrade.Probe:
        nonlocal calls
        calls += 1
        found = original_probe()
        if calls == 2:
            candidate = target.with_name(f".{target.name}.replacement")
            candidate.write_bytes(replacement)
            candidate.chmod(0o600)
            os.replace(candidate, target)
        return found

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "probe",
        replace_after_locked_probe,
    )
    resumed = deployed_upgrade_transaction.ensure()

    assert resumed.status == "complete"
    assert target.read_bytes() == replacement
    assert deployed_upgrade.completion_path().is_file()
    monkeypatch.setattr(deployed_upgrade_transaction, "probe", original_probe)
    assert deployed_upgrade_transaction.probe().status == "complete"
    assert deployed_upgrade_transaction.ensure().status == "complete"
    assert target.read_bytes() == replacement


def test_cutover_strictly_reproves_state_after_completion_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _profile(tmp_path, monkeypatch)
    original = deployed_upgrade_transaction._write_no_replace

    def edit_after_completion(
        path: Path,
        contents: bytes,
        maximum: int,
        *,
        label: str,
        create_parent: bool = True,
    ) -> bool:
        changed = original(
            path,
            contents,
            maximum,
            label=label,
            create_parent=create_parent,
        )
        if path == deployed_upgrade.completion_path():
            paths.settings_path().write_text(
                paths.settings_path().read_text(encoding="utf-8") + "# non-cooperating edit\n",
                encoding="utf-8",
            )
        return changed

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_write_no_replace",
        edit_after_completion,
    )
    with pytest.raises(
        deployed_upgrade_transaction.TransactionError,
        match="migrated settings changed before cutover",
    ):
        deployed_upgrade_transaction.ensure()
    assert deployed_upgrade.completion_path().is_file()
    assert deployed_upgrade.journal_path().is_file()

    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", original)
    resumed = deployed_upgrade_transaction.ensure()
    assert not resumed.changed
    assert resumed.status == "complete"


def test_cleaned_completion_is_historical_after_later_settings_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _profile(tmp_path, monkeypatch)
    completed = deployed_upgrade_transaction.ensure()
    assert completed.status == "complete"
    assert not deployed_upgrade.journal_path().exists()
    paths.settings_path().write_text(
        paths.settings_path().read_text(encoding="utf-8") + "# later user edit\n",
        encoding="utf-8",
    )

    resumed = deployed_upgrade_transaction.ensure()

    assert not resumed.changed
    assert resumed.status == "complete"
