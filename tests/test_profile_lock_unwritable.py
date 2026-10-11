"""The profile-wide transaction where its lock file can't be opened for writing, or made.

The state folder read-only (its files too, as ``chmod -R a-w`` or a read-only
mount leaves it), or the lock file missing from a folder that can't take a
new one: `state_file.mutation_lock` refuses at its open, and waiting cures
neither. Every lane that guards its work with the transaction then failed
before it began, the start's repair among them (review AC-13: the gate stayed
"checking" for good).

`legacy_migration.profile_transaction` now takes the same lock through a
read-only descriptor when the file is there: ``flock`` excludes on the file,
however each holder opened it, so there is still exactly one lock object,
the lock file, for this version in either kind of folder, for the released
0.2.2 and for every entry (the windows, the service, ``--write-config``).
Where the file is not there and can't be made, there is nothing to lock and
nothing stands in for it: refused, for that reason (and the start rests,
`tests/test_ui_start_profile_lock.py`). Real permission bits only.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests import released
from tests.released import released_source  # noqa: F401 -- a fixture
from wall_in_one import legacy_migration, paths
from wall_in_one.library import favourites, state_file


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A profile's state folder with the transaction's lock file made, as a first run leaves it."""
    if os.geteuid() == 0:
        pytest.skip("permission bits don't bind root")
    for variable, name in (
        ("HOME", "home"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
    ):
        (tmp_path / name).mkdir(exist_ok=True)
        monkeypatch.setenv(variable, str(tmp_path / name))
    with legacy_migration.unattended_transaction():
        pass
    folder = paths.app_state_dir()
    assert _lock().is_file()
    try:
        yield folder
    finally:
        folder.chmod(0o700)
        for path in folder.iterdir():
            if not path.is_symlink():
                path.chmod(0o600)


def _lock() -> Path:
    marker = legacy_migration.marker_path()
    return marker.with_name(f".{marker.name}.mutation.lock")


def _held_elsewhere() -> bool:
    """Whether the lock file is locked, as another process would find it."""
    descriptor = os.open(_lock(), os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def test_a_read_only_lock_file_is_still_the_lock(state: Path) -> None:
    """``chmod -R a-w``: the transaction runs, holding the very lock others take."""
    _lock().chmod(0o444)
    state.chmod(0o555)
    assert not _held_elsewhere()
    with legacy_migration.unattended_transaction():
        assert _held_elsewhere(), "held, through a read-only descriptor"
        with legacy_migration.unattended_transaction():  # nested, as the lanes nest it
            assert _held_elsewhere()
    assert not _held_elsewhere(), "and released"
    assert stat.S_IMODE(_lock().stat().st_mode) == 0o444, "its mode was left alone"


def test_a_read_only_lock_file_someone_holds_is_waited_for_then_refused(state: Path) -> None:
    """Held is not unwritable: the wait and its timeout are as they always were."""
    _lock().chmod(0o444)
    state.chmod(0o555)
    descriptor = os.open(_lock(), os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with (
            pytest.raises(legacy_migration.MigrationError, match="timed out"),
            legacy_migration.unattended_transaction(timeout=0.2),
        ):
            raise AssertionError("entered beside the holder")
    finally:
        os.close(descriptor)
    with legacy_migration.unattended_transaction(timeout=0.2):
        pass


def test_no_lock_file_in_a_folder_that_cannot_take_one_is_refused_for_that_reason(
    state: Path,
) -> None:
    """A profile copied without its dot files, then read-only: there is no lock
    to take, and no other object is locked in its place (a folder's lock and
    the file's would not exclude each other). Refused, naming the file and
    why; nothing is made, and nothing runs unlocked."""
    _lock().unlink()
    before = sorted(path.name for path in state.iterdir())
    state.chmod(0o555)
    with (
        pytest.raises(legacy_migration.MigrationError, match="Permission denied") as refused,
        legacy_migration.unattended_transaction(timeout=0.2),
    ):
        raise AssertionError("entered with no lock")
    assert _lock().name in str(refused.value)
    assert sorted(path.name for path in state.iterdir()) == before


def test_no_lock_file_in_a_writable_folder_is_made_as_before(state: Path) -> None:
    _lock().unlink()
    with legacy_migration.unattended_transaction():
        assert _held_elsewhere()
    assert stat.S_IMODE(_lock().stat().st_mode) == 0o600


def test_a_lock_file_that_cannot_be_opened_at_all_is_refused_with_its_reason(state: Path) -> None:
    """Not readable either: there is no way to take it, and someone else might
    hold it. Refused, naming the file's trouble; never run unlocked."""
    _lock().chmod(0o000)
    state.chmod(0o555)
    with (
        pytest.raises(legacy_migration.MigrationError, match="Permission denied"),
        legacy_migration.unattended_transaction(timeout=0.2),
    ):
        raise AssertionError("entered with no lock")


def test_a_lock_that_is_not_a_private_regular_file_is_refused(state: Path, tmp_path: Path) -> None:
    """The read-only way checks what `mutation_lock` checks: a link is not the lock."""
    elsewhere = tmp_path / "elsewhere.lock"
    elsewhere.write_bytes(b"")
    _lock().unlink()
    _lock().symlink_to(elsewhere)
    state.chmod(0o555)
    with (
        pytest.raises(legacy_migration.MigrationError),
        legacy_migration.unattended_transaction(timeout=0.2),
    ):
        raise AssertionError("locked through a link")


def test_the_read_only_way_locks_the_file_or_nothing(state: Path) -> None:
    """Asked for directly with no lock file: `FileNotFoundError`, never another object."""
    _lock().unlink()
    with (
        pytest.raises(FileNotFoundError),
        state_file.read_only_mutation_lock(
            legacy_migration.marker_path(), description="legacy migration", timeout=0.2
        ),
    ):
        raise AssertionError("ran with nothing locked")


def test_the_two_ways_of_taking_the_lock_exclude_each_other(state: Path) -> None:
    """One lock object: a holder through the writing descriptor (a writable
    folder) keeps out one through the read-only descriptor, and the other way."""
    marker = legacy_migration.marker_path()

    def writing() -> contextlib.AbstractContextManager[None]:
        return state_file.mutation_lock(
            marker, description="legacy migration", timeout=0.2, process_gate=False
        )

    def reading() -> contextlib.AbstractContextManager[None]:
        return state_file.read_only_mutation_lock(
            marker, description="legacy migration", timeout=0.2
        )

    for holder, other in ((writing, reading), (reading, writing), (reading, reading)):
        with holder(), pytest.raises(TimeoutError, match="timed out"), other():
            raise AssertionError("two holders of the profile")
        with other():
            pass


#: The released 0.2.2 taking the profile lock, as its lanes and its CLI do.
RELEASED_TRANSACTION = """
from wall_in_one import legacy_migration

try:
    with legacy_migration.unattended_transaction(timeout=0.5):
        print("ENTERED")
except legacy_migration.MigrationError as error:
    print("REFUSED", error)
"""


def test_the_released_version_and_this_one_exclude_each_other_on_the_same_file(
    state: Path,
    released_source: Path,  # noqa: F811
) -> None:
    """0.2.2 opens the lock file for writing and flocks it. Held here through
    the read-only descriptor, 0.2.2 waits and is refused; released, it enters.
    In a folder it can't write, 0.2.2 never enters at all (it has no other way)."""
    marker = legacy_migration.marker_path()
    with state_file.read_only_mutation_lock(marker, description="legacy migration"):
        held = released.run(released_source, RELEASED_TRANSACTION)
    assert held.stdout.startswith("REFUSED") and "timed out" in held.stdout, held.stdout
    free = released.run(released_source, RELEASED_TRANSACTION)
    assert free.stdout.strip() == "ENTERED", free.stdout + free.stderr

    _lock().chmod(0o444)
    state.chmod(0o555)
    with legacy_migration.unattended_transaction():  # this version: the read-only way
        there = released.run(released_source, RELEASED_TRANSACTION)
    assert there.stdout.startswith("REFUSED") and "Permission denied" in there.stdout, there.stdout


def test_a_stores_own_lock_is_refused_in_such_a_folder_as_before(state: Path) -> None:
    """Only the transaction takes the read-only way. A store's write is still
    refused by its own lock, with its reason, and nothing is written."""
    store = favourites.Store.open()
    store.add(state / "a wallpaper.png")
    saved = favourites.state_path().read_bytes()
    for path in state.iterdir():
        path.chmod(0o444)
    state.chmod(0o555)
    with pytest.raises(favourites.FavouritesError, match="Permission denied"):
        store.add(state / "another.png")
    assert favourites.state_path().read_bytes() == saved


def test_an_import_or_a_decision_is_still_refused_there(state: Path) -> None:
    """They write the folder, so they take the lock the writing way, and say why not."""
    _lock().chmod(0o444)
    state.chmod(0o555)
    with pytest.raises(legacy_migration.MigrationError, match="Permission denied"):
        legacy_migration.decline()
    with pytest.raises(legacy_migration.MigrationError, match="Permission denied"):
        legacy_migration.migrate()


def _a_profile_that_starts(tmp_path: Path) -> None:
    from wall_in_one import cli, config

    root = tmp_path / "library"
    root.mkdir()
    config.save(config.Settings(roots=(root,), scan_workshop=False))
    assert cli._run_graphical_startup_upgrade(require_legacy_safe=False) is None
    assert cli._run_unattended_writer(cli._write_runtime_config) == 0


def test_every_entry_that_takes_the_lock_goes_on_to_its_own_refusal(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``chmod -R a-w``. The step before a window's start, ``--write-config``
    and the health sync (the service's own start and its timers run the last
    two): each was stopped at the lock with "cannot lock the legacy migration
    transaction", the first before any window existed. Each now goes on: the
    start to its window, a writer to what it writes, refused there, saying
    which file's trouble it is."""
    from wall_in_one import cli

    _a_profile_that_starts(tmp_path)
    for path in state.iterdir():
        path.chmod(0o444)
    state.chmod(0o555)
    capsys.readouterr()

    assert cli._run_graphical_startup_upgrade(require_legacy_safe=False) is None
    assert cli._run_graphical_startup_upgrade(require_legacy_safe=True) is None
    started = capsys.readouterr()
    assert started.err == "", started.err

    wrote = cli._run_unattended_writer(cli._write_runtime_config)
    said = capsys.readouterr()
    assert "cannot lock the legacy migration transaction" not in said.out + said.err
    # Its own lock, beside runtime.toml, can't be opened: refused, saying so.
    assert wrote != 0 and "Permission denied" in said.err, said

    cli._run_unattended_writer(cli._sync_runtime_health)
    said = capsys.readouterr()
    assert "cannot lock the legacy migration transaction" not in said.out + said.err


def test_every_entry_is_refused_with_the_reason_where_no_lock_file_can_be(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No lock file, in a folder that can't take one: every entry stops at the
    lock, as before, saying why and naming the file; a window's start gets no
    further than its recovery step (which can be tried again)."""
    from wall_in_one import cli

    _a_profile_that_starts(tmp_path)
    _lock().unlink()
    state.chmod(0o555)
    capsys.readouterr()
    entries: tuple[Callable[[], int | None], ...] = (
        lambda: cli._run_graphical_startup_upgrade(require_legacy_safe=False),
        lambda: cli._run_unattended_writer(cli._write_runtime_config),
        lambda: cli._run_unattended_writer(cli._sync_runtime_health),
    )
    for entry in entries:
        assert entry() == 1
        said = capsys.readouterr().err
        assert "cannot lock the legacy migration transaction" in said, said
        assert "Permission denied" in said and _lock().name in said, said
