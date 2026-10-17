"""The copies earlier versions kept before a format change are removed at start.

0.2.0 to 0.2.2 copied ``playlists.json``, ``schedules.json``, ``displays.json``
and ``ui.toml`` to ``<file>.v<old>-backup`` before their first save in a newer
format. Updates are one-way now, so `retired_backups.remove` (the stores) and
`ui_prefs.remove_retired_backup` take those copies away.

What is pinned here: exactly five names go, each only from the one folder it
was written to; nothing beside them, like them, or with their name anywhere
else is touched; and a copy stays whenever it is not provably the copy, or the
file it is a copy of does not read in full.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest

from tests import released
from tests.released import released_source  # noqa: F401 -- a fixture
from wall_in_one import cli, file_io, paths, tidy, ui_prefs
from wall_in_one.library import displays, playlists, retired_backups, schedules

#: Every name this build removes, by the folder it is removed from.
STATE_NAMES: Final = (
    "playlists.json.v1-backup",
    "displays.json.v1-backup",
    "schedules.json.v1-backup",
    "schedules.json.v2-backup",
)
CONFIG_NAMES: Final = ("ui.toml.v1-backup",)

V1_PLAYLISTS: Final = b'{"version": 1, "playlists": []}\n'
V1_DISPLAYS: Final = b'{"version": 1, "displays": {}}\n'
V1_SCHEDULES: Final = b'{"rules": []}\n'  # no marker: schedules' version 1
V2_SCHEDULES: Final = b'{"version": 2, "rules": []}\n'
V1_UI: Final = b'version = 1\nwindow_style = "frosted"\n'
#: What each copy holds when an earlier version made it.
COPIES: Final = {
    "playlists.json.v1-backup": V1_PLAYLISTS,
    "displays.json.v1-backup": V1_DISPLAYS,
    "schedules.json.v1-backup": V1_SCHEDULES,
    "schedules.json.v2-backup": V2_SCHEDULES,
    "ui.toml.v1-backup": V1_UI,
}


def _state() -> Path:
    return paths.app_state_dir()


def _config() -> Path:
    return paths.app_config_dir()


def _folder(name: str) -> Path:
    return _config() if name in CONFIG_NAMES else _state()


def _bumped_profile() -> None:
    """Every file at its newest version, saved by this build's own writers."""
    playlist = playlists.Store.open().create("Evenings")
    playlists.Store.open().set_rotation(playlist.id, cycle_interval=120, shuffle=True)
    rule = schedules.Store.open().add(playlist.id, start="07:00", end="08:00")
    schedules.Store.open().set_name(rule.id, "Frog day")
    displays.Store.open().assign("DP-1", playlist.id)
    displays.Store.open().set_beats_global_rules("DP-1", True)
    ui_prefs.update({"window_style": "frosted"})
    ui_prefs.update({"interface": "classic"})
    assert json.loads((_state() / "playlists.json").read_bytes())["version"] == 2
    assert json.loads((_state() / "schedules.json").read_bytes())["version"] == 3
    assert json.loads((_state() / "displays.json").read_bytes())["version"] == 2
    assert ui_prefs.load().raw["version"] == 2


def _keep_copies() -> None:
    for name, contents in COPIES.items():
        target = _folder(name) / name
        target.write_bytes(contents)
        target.chmod(0o644)


def _remove_everything() -> tuple[Path, ...]:
    """What a start that opens a window does."""
    removed = retired_backups.remove()
    return (*removed, *((_config() / CONFIG_NAMES[0],) if ui_prefs.remove_retired_backup() else ()))


def _tree(root: Path) -> dict[str, bytes | str]:
    """Every entry below ``root``: a file's bytes, a link's target, else its type."""
    found: dict[str, bytes | str] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            found[relative] = f"-> {os.readlink(path)}"
        elif stat.S_ISREG(mode):
            found[relative] = path.read_bytes()
        else:
            found[relative] = f"type {stat.S_IFMT(mode):o}"
    return found


def test_the_names_are_exactly_the_five_any_version_wrote() -> None:
    """One per bump a released store could make, each beside its own file."""
    stores = {candidate.name: candidate for candidate in retired_backups.STORE_CANDIDATES}
    assert tuple(stores) == STATE_NAMES
    for name, candidate in stores.items():
        live = candidate.live()
        assert live.parent == _state()
        assert name == f"{live.name}.v{candidate.version}-backup"
        assert candidate.path() == _state() / name
    assert CONFIG_NAMES[0] == ui_prefs.RETIRED_BACKUP_NAME
    # What each store reads and now always writes: playlists 1 to 2, displays
    # 1 to 2, schedules 1 to 3, ui.toml 1 to 2.
    assert (playlists.FORMATS.oldest, playlists.FORMATS.current) == (1, 2)
    assert (displays.FORMATS.oldest, displays.FORMATS.current) == (1, 2)
    assert (schedules.FORMATS.oldest, schedules.FORMATS.current) == (1, 3)
    assert (ui_prefs.FORMATS.oldest, ui_prefs.FORMATS.current) == (1, 2)


def test_every_copy_goes_and_nothing_else_does(tmp_path: Path) -> None:
    """The five copies, beside decoys: near-miss names in the same folders, and
    the exact names everywhere else a careless sweep could reach."""
    _bumped_profile()
    _keep_copies()
    state, config = _state(), _config()
    wallpapers = tmp_path / "home" / "Pictures" / "Wallpapers"
    noctalia = paths.noctalia_state_dir()
    decoys: list[Path] = []
    # Near misses, in the app's own folders.
    for name in (
        "playlists.json.v1-backup~",
        "playlists.json.v1-backup.txt",
        ".playlists.json.v1-backup",
        "playlists.json.v2-backup",
        "playlists.json.v0-backup",
        "displays.json.v2-backup",
        "schedules.json.v3-backup",
        "pairings.json.v1-backup",
        "pairings.json.v2-backup",
        "favourites.json.v1-backup",
        "pending-removals.json.v1-backup",
        "runtime.toml.v1-backup",
        "playlists.json.broken",
        "playlists.json.backup",
        "PLAYLISTS.JSON.V1-BACKUP",
        # The other folder's name: ui.toml's copy is only ever in config.
        "ui.toml.v1-backup",
    ):
        decoys.append(state / name)
    for name in (
        "ui.toml.v2-backup",
        "ui.toml.v1-backup~",
        "settings.toml.v1-backup",
        "ui.toml.bak",
        # The stores' copies are only ever in the state folder.
        *STATE_NAMES,
    ):
        decoys.append(config / name)
    # The exact names, anywhere else.
    for folder in (
        wallpapers,
        wallpapers / "Wall-in-One" / "Wallhaven",
        noctalia,
        paths.app_cache_dir(),
        state / "nested",
        config / "tidy-archive" / "2026-10-11",
        tmp_path / "home",
    ):
        decoys.extend(folder / name for name in (*STATE_NAMES, *CONFIG_NAMES))
    for decoy in decoys:
        decoy.parent.mkdir(parents=True, exist_ok=True)
        decoy.write_bytes(COPIES.get(decoy.name, V1_PLAYLISTS))
    before = _tree(tmp_path)

    removed = _remove_everything()

    expected = [state / name for name in STATE_NAMES] + [config / name for name in CONFIG_NAMES]
    assert sorted(removed) == sorted(expected)
    after = _tree(tmp_path)
    gone = sorted(set(before) - set(after))
    assert gone == sorted(str(path.relative_to(tmp_path)) for path in expected)
    for decoy in decoys:
        assert decoy.is_file(), decoy
    # Nothing else changed a byte; what is new is only the discard's own record.
    for relative, contents in before.items():
        if relative not in gone:
            assert after[relative] == contents, relative
    new = sorted(set(after) - set(before))
    retained = file_io.RETAINED_ENTRY_DIRECTORY
    for relative in new:
        path = tmp_path / relative
        assert path.parent.name == retained or path.name == retained, relative
        assert path.parent in (state, config) or path.parent.parent in (state, config), relative
        assert path.is_dir() or path.stat().st_size == 0, relative
    # And it is done: a second start finds nothing and changes nothing.
    settled = _tree(tmp_path)
    assert _remove_everything() == ()
    assert _tree(tmp_path) == settled


def test_a_profile_without_copies_is_only_looked_at(tmp_path: Path) -> None:
    """Nothing is locked, made or written: not on a fresh install, where the
    folders don't exist yet, and not on a profile that has none."""
    assert _remove_everything() == ()
    assert not _state().exists() and not _config().exists()

    _bumped_profile()
    for lock in (*_state().glob(".*.lock"), *_config().glob(".*.lock")):
        lock.unlink()
    before = _tree(tmp_path)
    assert _remove_everything() == ()
    assert _tree(tmp_path) == before, "no lock file, no retained folder"


def _as_directory(path: Path, contents: bytes) -> None:
    path.mkdir()
    (path / "inside").write_bytes(contents)


def _as_link(path: Path, contents: bytes) -> None:
    target = path.with_name(f"{path.name}.target")
    target.write_bytes(contents)
    path.symlink_to(target)


def _with_another_name(path: Path, contents: bytes) -> None:
    path.write_bytes(contents)
    os.link(path, path.with_name(f"{path.name}.also"))


def _as_fifo(path: Path, contents: bytes) -> None:
    del contents
    os.mkfifo(path)


NOT_THE_COPY: Final[dict[str, Callable[[Path, bytes], object]]] = {
    "a directory": _as_directory,
    "a link to a copy": _as_link,
    "a file with a second name": _with_another_name,
    "a fifo": _as_fifo,
    "a person's notes": lambda path, _contents: path.write_text("call the plumber\n"),
    "an empty file": lambda path, _contents: path.write_bytes(b""),
    "a list": lambda path, _contents: path.write_bytes(b"[1, 2, 3]\n"),
    "not unicode": lambda path, _contents: path.write_bytes(b"\xff\xfe\x00"),
}


@pytest.mark.parametrize("name", [*STATE_NAMES, *CONFIG_NAMES])
@pytest.mark.parametrize("what", list(NOT_THE_COPY))
def test_something_else_under_the_name_stays(name: str, what: str, tmp_path: Path) -> None:
    """The name alone proves nothing: only a regular, singly linked document of
    the version the name says is the copy."""
    _bumped_profile()
    target = _folder(name) / name
    NOT_THE_COPY[what](target, COPIES[name])
    before = _tree(tmp_path)

    removed = _remove_everything()

    if (name, what) == ("ui.toml.v1-backup", "an empty file"):
        # The one overlap: a ui.toml without a version is a version 1 file, so
        # an empty one is a copy of an empty ui.toml. Nothing is in it to lose.
        assert removed == (target,)
        return
    assert removed == ()
    assert _tree(tmp_path) == before, "it is where it was, byte for byte, and nothing was made"


def _newest(name: str) -> bytes:
    """The live file's own bytes: a document of its store, in a newer version than the name."""
    live = {
        "playlists.json.v1-backup": _state() / "playlists.json",
        "displays.json.v1-backup": _state() / "displays.json",
        "schedules.json.v1-backup": _state() / "schedules.json",
        "schedules.json.v2-backup": _state() / "schedules.json",
        "ui.toml.v1-backup": _config() / "ui.toml",
    }[name]
    return live.read_bytes()


@pytest.mark.parametrize("name", [*STATE_NAMES, *CONFIG_NAMES])
def test_a_document_of_another_version_under_the_name_stays(name: str, tmp_path: Path) -> None:
    """Somebody's own copy of the current file, say: not what the name says it is."""
    _bumped_profile()
    target = _folder(name) / name
    target.write_bytes(_newest(name))
    before = _tree(tmp_path)

    assert _remove_everything() == ()

    assert _tree(tmp_path) == before


def test_a_copy_larger_than_its_file_may_be_stays(tmp_path: Path) -> None:
    _bumped_profile()
    target = _state() / "displays.json.v1-backup"
    padded = json.dumps({"version": 1, "displays": {}, "x": "y" * displays.MAX_STATE_BYTES})
    target.write_text(padded)
    before = _tree(tmp_path)

    assert _remove_everything() == ()

    assert _tree(tmp_path) == before


def _missing(live: Path) -> None:
    live.unlink()


def _damaged(live: Path) -> None:
    live.write_bytes(b'{"version": 2, "playl')


def _from_a_newer_version(live: Path) -> None:
    if live.suffix == ".toml":
        live.write_text('version = 9\nwindow_style = "frosted"\n')
        return
    document = json.loads(live.read_bytes())
    document["version"] = 9
    live.write_text(json.dumps(document))


def _unreadable(live: Path) -> None:
    live.chmod(0o000)


LIVE_NOT_READ_IN_FULL: Final[dict[str, Callable[[Path], None]]] = {
    "missing": _missing,
    "damaged": _damaged,
    "from a newer version": _from_a_newer_version,
    "unreadable": _unreadable,
}


@pytest.mark.parametrize("name", [*STATE_NAMES, *CONFIG_NAMES])
@pytest.mark.parametrize("how", list(LIVE_NOT_READ_IN_FULL))
def test_a_copy_stays_while_its_file_does_not_read_in_full(
    name: str, how: str, tmp_path: Path
) -> None:
    """Then the copy may be the only good one: nothing is removed on a failed
    or unavailable proof. The other files' copies still go."""
    if how == "unreadable" and os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    _bumped_profile()
    _keep_copies()
    target = _folder(name) / name
    live = target.with_name(name.rsplit(".v", 1)[0])
    LIVE_NOT_READ_IN_FULL[how](live)
    kept = target.read_bytes()
    try:
        removed = _remove_everything()
    finally:
        if how == "unreadable":
            live.chmod(0o644)

    assert target.read_bytes() == kept
    assert target not in removed
    # schedules.json has two copies, and both wait for it.
    waiting = {_folder(n) / n for n in COPIES if n.rsplit(".v", 1)[0] == live.name}
    assert set(removed) == {_folder(n) / n for n in COPIES} - waiting


def test_a_schedule_with_a_damaged_record_keeps_its_copies() -> None:
    """Readable, but not in full: a rule in it could not be read, so what the
    copy holds may not all be in it."""
    _bumped_profile()
    _keep_copies()
    live = _state() / "schedules.json"
    document = json.loads(live.read_bytes())
    document["rules"].append({"id": 7, "playlist": None})
    live.write_text(json.dumps(document))
    assert not schedules.reads_cleanly()

    removed = _remove_everything()

    assert (_state() / "schedules.json.v2-backup").read_bytes() == V2_SCHEDULES
    assert (_state() / "schedules.json.v1-backup").read_bytes() == V1_SCHEDULES
    assert len(removed) == 3


def test_a_folder_that_cannot_be_written_keeps_its_copies_and_stops_nothing() -> None:
    if os.geteuid() == 0:
        pytest.skip("root writes into a read-only folder")
    _bumped_profile()
    _keep_copies()
    state = _state()
    for entry in state.iterdir():
        entry.chmod(stat.S_IMODE(entry.stat().st_mode) & ~0o222)
    state.chmod(0o555)
    try:
        removed = _remove_everything()
        for name in STATE_NAMES:
            assert (state / name).read_bytes() == COPIES[name]
    finally:
        state.chmod(0o755)
    assert removed == (_config() / CONFIG_NAMES[0],), "the config folder's copy still went"


def test_a_busy_file_keeps_its_copy_for_a_later_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Another process holds the file's lock (its flock, through a descriptor
    of its own): that one copy waits, and the other files' copies go."""
    _bumped_profile()
    _keep_copies()
    monkeypatch.setattr(retired_backups, "LOCK_TIMEOUT_SECONDS", 0.05)
    live = _state() / "playlists.json"
    descriptor = os.open(live.with_name(f".{live.name}.mutation.lock"), os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        removed = retired_backups.remove()
    finally:
        os.close(descriptor)

    assert (_state() / "playlists.json.v1-backup").read_bytes() == V1_PLAYLISTS
    assert len(removed) == 3
    assert retired_backups.remove() == (_state() / "playlists.json.v1-backup",)


def test_a_copy_replaced_while_it_is_checked_is_left() -> None:
    """The check and the removal are of one pinned file: what is put under the
    name after the check is not what was checked, and stays."""
    _bumped_profile()
    _keep_copies()
    target = _state() / "displays.json.v1-backup"
    swapped = b'{"version": 1, "displays": {"DP-9": "mine"}}\n'
    real = displays.reads_cleanly

    def swap_then_read(path: Path | None = None) -> bool:
        replacement = target.with_name("replacement")
        replacement.write_bytes(swapped)
        os.replace(replacement, target)
        return real(path)

    candidates = tuple(
        replace(candidate, reads_cleanly=swap_then_read)
        if candidate.name == target.name
        else candidate
        for candidate in retired_backups.STORE_CANDIDATES
    )

    removed = retired_backups.remove(candidates)

    assert target.read_bytes() == swapped
    assert target not in removed and len(removed) == 3


#: The released 0.2.2 itself (`tests.released`), using each new field for the
#: first time on files in the older versions: its own bumps, and its own copies.
RELEASED_BUMPS: Final = """
from wall_in_one import ui_prefs
from wall_in_one.library import displays, playlists, schedules

made = playlists.Store.open().create("Evenings")
if sys.argv[1] == "from-version-1":
    # A schedules.json as 0.1.x wrote it: 0.2.2's first rule name takes it to 3.
    schedules.state_path().write_text(
        '{"version": 1, "rules": [{"id": "old", "playlist": "%s"}]}' % made.id
    )
    rule_id = "old"
else:
    rule_id = schedules.Store.open().add(made.id, start="07:00", end="08:00").id
displays.Store.open().assign("DP-1", made.id)
ui_prefs.update({"window_style": "frosted"})

playlists.Store.open().set_rotation(made.id, cycle_interval=120, shuffle=True)
schedules.Store.open().set_name(rule_id, "Frog day")
displays.Store.open().set_beats_global_rules("DP-1", True)
ui_prefs.update({"interface": "next"})
print("DONE", made.id, rule_id)
"""


@pytest.mark.parametrize(
    ("schedule", "copies"),
    [
        (
            "from-version-2",
            (
                "playlists.json.v1-backup",
                "displays.json.v1-backup",
                "schedules.json.v2-backup",
                "ui.toml.v1-backup",
            ),
        ),
        (
            "from-version-1",
            (
                "playlists.json.v1-backup",
                "displays.json.v1-backup",
                "schedules.json.v1-backup",
                "ui.toml.v1-backup",
            ),
        ),
    ],
)
def test_the_copies_the_released_version_really_made_are_the_ones_removed(
    released_source: Path,  # noqa: F811
    schedule: str,
    copies: tuple[str, ...],
    tmp_path: Path,
) -> None:
    """Review rule 4, run rather than read: 0.2.2 is the producer. What it left
    beside the files it bumped is exactly what this build's start takes away,
    and what the person set there is all still in the files."""
    done = released.run(released_source, RELEASED_BUMPS, schedule)
    assert done.stdout.startswith("DONE"), done.stdout + done.stderr
    _done, playlist_id, rule_id = done.stdout.split()
    made = sorted(path.name for folder in (_state(), _config()) for path in folder.glob("*-backup"))
    assert made == sorted(copies), "what 0.2.2 keeps before a bump"
    lives = {name: (_folder(name) / name.rsplit(".v", 1)[0]).read_bytes() for name in copies}
    before = _tree(tmp_path)

    removed = _remove_everything()

    assert sorted(removed) == sorted(_folder(name) / name for name in copies)
    after = _tree(tmp_path)
    assert sorted(set(before) - set(after)) == sorted(
        str((_folder(name) / name).relative_to(tmp_path)) for name in copies
    )
    for name, contents in lives.items():
        assert (_folder(name) / name.rsplit(".v", 1)[0]).read_bytes() == contents, name
    assert playlists.Store.open().find(playlist_id).cycle_interval == 120
    (rule,) = [rule for rule in schedules.Store.open().rules if rule.id == rule_id]
    assert rule.name == "Frog day"
    assert displays.Store.open().beats_global_rules("DP-1")
    assert ui_prefs.load().prefs.interface == "next"


def test_the_service_start_removes_the_stores_copies_and_never_reads_ui_toml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No window opens there, so ui.toml and its copy are not looked at."""
    _bumped_profile()
    _keep_copies()

    def refuse(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("a start that opens no window read ui.toml")

    from wall_in_one.library import capture_upgrade

    monkeypatch.setattr(ui_prefs, "_read", refuse)
    monkeypatch.setattr(ui_prefs, "remove_retired_backup", refuse)
    monkeypatch.setattr(capture_upgrade, "prepare", lambda: None)
    monkeypatch.setattr(cli, "_write_runtime_config", lambda: 0)
    monkeypatch.setattr(cli, "_run_unattended_writer", lambda write: write())

    assert cli._prepare_service_start() == 0

    assert [name for name in STATE_NAMES if (_state() / name).exists()] == []
    assert (_config() / CONFIG_NAMES[0]).read_bytes() == V1_UI


def test_a_start_that_opens_a_window_removes_all_five(monkeypatch: pytest.MonkeyPatch) -> None:
    from wall_in_one import deployed_upgrade_transaction
    from wall_in_one.library import capture_upgrade

    _bumped_profile()
    _keep_copies()
    monkeypatch.setattr(capture_upgrade, "prepare", lambda: None)
    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "ensure",
        lambda: deployed_upgrade_transaction.Outcome(False, "current", ""),
    )

    assert cli._run_graphical_startup_upgrade(require_legacy_safe=False) is None

    assert [name for name in COPIES if (_folder(name) / name).exists()] == []


# -- a copy goes only when nothing in it is missing from the live file -------------------


def _document(version: int | None, key: str, records: object) -> bytes:
    body: dict[str, object] = {key: records}
    if version is not None:
        body = {"version": version, **body}
    return (json.dumps(body, indent=2) + "\n").encode()


def _three_playlists() -> bytes:
    return _document(
        1,
        "playlists",
        [{"id": name, "name": name.title(), "entries": []} for name in ("a", "b", "c")],
    )


def _put_copy(name: str, contents: bytes) -> Path:
    target = _folder(name) / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(contents)
    target.chmod(0o644)
    return target


def _only_these_left(*names: str) -> None:
    assert sorted(path.name for path in _state().glob("*-backup")) == sorted(names)


def test_a_recovered_file_that_lost_what_the_copy_holds_keeps_the_copy() -> None:
    """The reviewer's case: 0.2.x kept the copy of three playlists; later the
    file is damaged; the person adds one playlist, so the damaged file is set
    aside as ``.broken`` and only the new playlist is saved. The file reads in
    full, and is not the copy's translation."""
    copy = _put_copy("playlists.json.v1-backup", _three_playlists())
    live = _state() / "playlists.json"
    live.write_text("{ not json", encoding="utf-8")
    playlists.Store.open().create("New")
    assert live.with_name("playlists.json.broken").is_file()
    assert playlists.reads_cleanly()

    assert retired_backups.remove() == ()

    assert copy.read_bytes() == _three_playlists()


def test_a_schedule_copy_stays_beside_a_file_that_lacks_its_rules() -> None:
    copy = _put_copy(
        "schedules.json.v1-backup", _document(None, "rules", [{"id": "old", "playlist": "x"}])
    )
    (_state() / "schedules.json").write_bytes(
        _document(2, "rules", [{"id": "other", "playlist": "x"}])
    )

    assert retired_backups.remove() == ()

    assert copy.is_file()


def test_a_copy_whose_file_was_deleted_and_recreated_by_a_save_stays() -> None:
    copy = _put_copy("playlists.json.v1-backup", _three_playlists())
    assert not (_state() / "playlists.json").exists()
    playlists.Store.open().create("Fresh")
    assert playlists.reads_cleanly()

    assert retired_backups.remove() == ()

    assert copy.read_bytes() == _three_playlists()


def test_a_displays_copy_stays_while_one_of_its_connectors_is_gone() -> None:
    copy = _put_copy(
        "displays.json.v1-backup", _document(1, "displays", {"DP-1": "a", "DP-2": "b"})
    )
    displays.Store.open().assign("DP-1", "a")

    assert retired_backups.remove() == ()

    assert copy.is_file()


def test_a_ui_copy_stays_while_ui_toml_holds_no_interface_choice() -> None:
    copy = _put_copy("ui.toml.v1-backup", V1_UI)
    ui_prefs.update({"window_style": "frosted"})
    assert ui_prefs.reads_cleanly() and ui_prefs.load().prefs.interface is None

    assert not ui_prefs.remove_retired_backup()
    assert copy.is_file()

    ui_prefs.update({"interface": "classic"})
    assert ui_prefs.remove_retired_backup()
    assert not copy.exists()


def test_every_record_still_there_removes_the_copy_even_after_changes_to_them() -> None:
    """A playlist renamed, a rule changed, a display retargeted: same identity,
    so the copy holds only an older state of something the person changed."""
    _put_copy("playlists.json.v1-backup", _three_playlists())
    _put_copy(
        "schedules.json.v2-backup",
        _document(2, "rules", [{"id": "r", "playlist": "a"}]),
    )
    _put_copy("displays.json.v1-backup", _document(1, "displays", {"DP-1": "a"}))
    (_state() / "playlists.json").write_bytes(
        _document(
            2,
            "playlists",
            [
                {"id": "a", "name": "Renamed after the copy", "entries": []},
                {"id": "b", "name": "B", "entries": []},
                {"id": "c", "name": "C", "entries": []},
                {"id": "d", "name": "Added later", "entries": []},
            ],
        )
    )
    (_state() / "schedules.json").write_bytes(
        _document(3, "rules", [{"id": "r", "playlist": "d", "start": "07:00", "end": "08:00"}])
    )
    (_state() / "displays.json").write_bytes(_document(2, "displays", {"DP-1": "d"}))

    removed = retired_backups.remove()

    assert {path.name for path in removed} == {
        "playlists.json.v1-backup",
        "schedules.json.v2-backup",
        "displays.json.v1-backup",
    }
    _only_these_left()


@pytest.mark.parametrize(
    ("name", "contents"),
    [
        ("playlists.json.v1-backup", b'{"version": 1, "playlists": [{"id": 7}]}\n'),
        ("playlists.json.v1-backup", b'{"version": 1, "playlists": "none"}\n'),
        ("schedules.json.v2-backup", b'{"version": 2, "rules": [{"id": 7, "playlist": null}]}\n'),
        ("displays.json.v1-backup", b'{"version": 1, "displays": {"DP-1": 3}}\n'),
    ],
)
def test_a_copy_that_does_not_read_in_full_stays(name: str, contents: bytes) -> None:
    _bumped_profile()
    copy = _put_copy(name, contents)

    assert retired_backups.remove() == ()

    assert copy.read_bytes() == contents


def test_a_copy_with_a_repeated_key_stays_as_the_store_would_not_read_it() -> None:
    """Bare ``json.loads`` keeps the last of two ``playlists`` keys (an empty
    list) and would call the copy empty; the store's reader says "not readable
    JSON". So a, b and c, which are in no other file, stay."""
    repeated = (
        b'{"version": 1, "playlists": ['
        b'{"id": "a", "name": "A", "entries": []},'
        b'{"id": "b", "name": "B", "entries": []},'
        b'{"id": "c", "name": "C", "entries": []}], "playlists": []}\n'
    )
    assert playlists.record_ids(repeated) is None
    assert playlists.record_ids(_three_playlists()) == frozenset({"a", "b", "c"})
    copy = _put_copy("playlists.json.v1-backup", repeated)
    playlists.Store.open().create("Fresh")
    assert playlists.reads_cleanly()

    assert retired_backups.remove() == ()

    assert copy.read_bytes() == repeated
    assert retired_backups.why_it_stays(_candidate(copy.name)) is not None


@pytest.mark.parametrize(
    ("name", "contents"),
    [
        ("displays.json.v1-backup", b'{"version": 1, "displays": {"DP-1": "a"}, "displays": {}}\n'),
        ("schedules.json.v2-backup", b'{"version": 2, "rules": [], "rules": []}\n'),
        ("schedules.json.v1-backup", b'{"rules": [], "rules": []}\n'),
        ("playlists.json.v1-backup", b'{"version": 1, "playlists": [], "x": NaN}\n'),
    ],
)
def test_a_copy_the_stores_reader_refuses_stays_even_when_it_looks_empty(
    name: str, contents: bytes
) -> None:
    _bumped_profile()
    copy = _put_copy(name, contents)

    assert retired_backups.remove() == ()

    assert copy.read_bytes() == contents


# -- Tidy up lists a copy that stays, by the same function that decides -----------------


def _candidate(name: str) -> retired_backups.Candidate:
    if name == ui_prefs.RETIRED_BACKUP_NAME:
        return ui_prefs.retired_backup_candidate()
    (found,) = [c for c in retired_backups.STORE_CANDIDATES if c.name == name]
    return found


def _listed_as_kept() -> dict[str, str]:
    """Every kept row of every action in the Tidy preview, by file name."""
    found: dict[str, str] = {}
    for action in tidy.plan(roots=()).actions:
        for kept in action.kept:
            found[kept.path.name] = kept.reason
    return found


def _a_recovered_playlists_file_that_lacks_a_playlist() -> Path:
    copy = _put_copy("playlists.json.v1-backup", _three_playlists())
    live = _state() / "playlists.json"
    live.write_text("{ not json", encoding="utf-8")
    playlists.Store.open().create("New")
    assert playlists.reads_cleanly()
    return copy


def test_tidy_lists_a_store_copy_that_stays_with_why_and_how_to_remove_it() -> None:
    """The reviewer's repro: `tidy.plan(roots=())`, a kept copy beside a
    recovered live file that lacks one of its playlists."""
    copy = _a_recovered_playlists_file_that_lacks_a_playlist()
    before = _tree(_state().parent)

    kept = _listed_as_kept()

    reason = kept[copy.name]
    assert reason.startswith(
        "playlists.json.v1-backup stays: playlists.json lacks 3 of the records"
    )
    assert reason.endswith("You can delete it yourself if you don't want it.")
    assert _tree(_state().parent) == before, "the survey wrote nothing"
    assert (retired_backups.why_it_stays(_candidate(copy.name)) or "?") in reason
    # And it is the one Tidy up never acts on: it is not among any change.
    assert all(
        copy.name != change.path.name for a in tidy.plan(roots=()).actions for change in a.changes
    )
    assert copy.read_bytes() == _three_playlists()


def test_tidy_lists_the_ui_copy_in_the_config_folder() -> None:
    copy = _put_copy("ui.toml.v1-backup", V1_UI)
    ui_prefs.update({"window_style": "frosted"})
    assert ui_prefs.load().prefs.interface is None

    reason = _listed_as_kept()[copy.name]

    assert reason.startswith(
        "ui.toml.v1-backup stays: ui.toml holds no interface choice yet, so it isn't shown"
    )
    assert reason.endswith("You can delete it yourself if you don't want it.")


def test_tidy_lists_a_copy_whose_file_cannot_be_read() -> None:
    copy = _put_copy("displays.json.v1-backup", _document(1, "displays", {"DP-1": "a"}))
    (_state() / "displays.json").write_text("{ not json", encoding="utf-8")

    assert (
        "displays.json.v1-backup stays: displays.json is missing or could not be read in full"
        in _listed_as_kept()[copy.name]
    )


def test_tidy_does_not_list_a_copy_that_may_go_the_next_start_removes_it() -> None:
    """Nothing is lost by it, so there is nothing to tell the person: it is
    not Tidy up's to offer, and it is not a "kept" row either."""
    _bumped_profile()
    _keep_copies()
    assert all(retired_backups.why_it_stays(_candidate(name)) is None for name in COPIES)

    assert not [name for name in _listed_as_kept() if name in COPIES]

    assert len(_remove_everything()) == 5


def test_tidy_lists_only_the_exact_names_in_their_own_folder() -> None:
    copy = _a_recovered_playlists_file_that_lacks_a_playlist()
    _config().mkdir(parents=True, exist_ok=True)
    decoys = (
        _config() / "playlists.json.v1-backup",
        _state() / "ui.toml.v1-backup",
        _state() / "playlists.json.v1-backup~",
        _state() / "playlists.json.v3-backup",
    )
    for decoy in decoys:
        decoy.write_bytes(_three_playlists())

    kept = [kept.path for action in tidy.plan(roots=()).actions for kept in action.kept]

    assert copy in kept
    assert not set(decoys) & set(kept)


@pytest.mark.parametrize("name", [*STATE_NAMES, *CONFIG_NAMES])
@pytest.mark.parametrize("how", ["live missing", "live damaged", "not the copy", "link"])
def test_the_survey_and_the_removal_agree(name: str, how: str) -> None:
    """Listed exactly when `remove` keeps it: both ask `why_it_stays`."""
    _bumped_profile()
    target = _folder(name) / name
    live = target.with_name(name.rsplit(".v", 1)[0])
    if how == "link":
        _as_link(target, COPIES[name])
    else:
        target.write_bytes(_newest(name) if how == "not the copy" else COPIES[name])
        if how == "live missing":
            live.unlink()
        elif how == "live damaged":
            live.write_bytes(b"{ no")
    expected = retired_backups.why_it_stays(_candidate(name))
    assert expected is not None
    assert expected in _listed_as_kept()[name]

    _remove_everything()

    assert target.is_symlink() or target.is_file(), "the removal kept it too"


# -- the generation that was checked is the one that is removed ------------------------


def test_a_copy_rewritten_in_place_after_it_was_checked_is_not_removed() -> None:
    """The reviewer's probe: between the check and the discard the copy's bytes
    change under the same inode (two playlists the live file lacks). The
    discard is pinned to the generation the check read, so it refuses."""
    _bumped_profile()
    copy = _put_copy("playlists.json.v1-backup", _three_playlists())
    (_state() / "playlists.json").write_bytes(
        _document(
            2, "playlists", [{"id": n, "name": n, "entries": []} for n in ("a", "b", "c", "d")]
        )
    )
    inode = copy.stat().st_ino
    edited = _document(
        1, "playlists", [{"id": n, "name": n, "entries": []} for n in ("a", "b", "c", "x", "y")]
    )
    real = playlists.held_record_ids

    def rewrite_then_hold(path: Path | None = None) -> frozenset[str] | None:
        with copy.open("r+b") as handle:
            handle.write(edited)
            handle.truncate()
        return real(path)

    candidates = tuple(
        replace(candidate, held=rewrite_then_hold) if candidate.name == copy.name else candidate
        for candidate in retired_backups.STORE_CANDIDATES
    )

    assert retired_backups.remove(candidates) == ()

    assert copy.stat().st_ino == inode and copy.read_bytes() == edited


# -- every reason, as the person reads it -----------------------------------------------

_DELETE: Final = "You can delete it yourself"
_LEFT_ALONE: Final = "so it isn't one of Wall-in-One's old copies and is left alone."
_NOT_SHOWN: Final = "so it can't be shown to be one of Wall-in-One's old copies, and is left alone."


def _live_for(name: str) -> Path:
    return _folder(name) / name.rsplit(".v", 1)[0]


def _setup_directory(name: str) -> None:
    (_folder(name) / name).mkdir()


def _setup_link(name: str) -> None:
    _as_link(_folder(name) / name, COPIES[name])


def _setup_second_name(name: str) -> None:
    _with_another_name(_folder(name) / name, COPIES[name])


def _setup_too_large(name: str) -> None:
    (_folder(name) / name).write_bytes(
        b"x" * (retired_backups.STORE_CANDIDATES[0].maximum_bytes + 1)
    )


def _setup_other_version(name: str) -> None:
    (_folder(name) / name).write_bytes(_newest(name))


def _setup_newer_live(name: str) -> None:
    (_folder(name) / name).write_bytes(COPIES[name])
    _from_a_newer_version(_live_for(name))


def _setup_live_missing(name: str) -> None:
    (_folder(name) / name).write_bytes(COPIES[name])
    _live_for(name).unlink()


def _setup_unreadable_copy(name: str) -> None:
    contents = {
        "playlists.json.v1-backup": b'{"version": 1, "playlists": [{"id": 7}]}\n',
        "displays.json.v1-backup": b'{"version": 1, "displays": {"DP-1": 3}}\n',
        "schedules.json.v1-backup": b'{"rules": [{"id": 7}]}\n',
        "schedules.json.v2-backup": b'{"version": 2, "rules": [{"id": 7}]}\n',
    }
    (_folder(name) / name).write_bytes(contents[name])


#: (setup, what the reason says, whether it is a real copy that stays and so is
#: told it can be deleted by hand)
REASONS: Final[dict[str, tuple[Callable[[str], None], str, bool]]] = {
    "a folder": (_setup_directory, f"is a folder, {_LEFT_ALONE}", False),
    "a link": (_setup_link, f"is a link, {_LEFT_ALONE}", False),
    "another name": (
        _setup_second_name,
        f"belongs to another user or has another name, {_NOT_SHOWN}",
        False,
    ),
    "too large": (_setup_too_large, f"may be, {_NOT_SHOWN}", False),
    "another version": (_setup_other_version, "is not a readable version", False),
    "newer live file": (_setup_newer_live, "was saved by a newer version.", True),
    "live file missing": (_setup_live_missing, "is missing or could not be read in full.", True),
    "copy not readable in full": (
        _setup_unreadable_copy,
        "stays: it could not be read in full.",
        True,
    ),
}


@pytest.mark.parametrize("name", STATE_NAMES)
@pytest.mark.parametrize("how", list(REASONS))
def test_each_reason_names_the_copy_and_advises_only_for_a_real_copy(name: str, how: str) -> None:
    _bumped_profile()
    setup, says, advises = REASONS[how]
    setup(name)

    reason = retired_backups.why_it_stays(_candidate(name))

    assert reason is not None and reason.startswith(name), reason
    assert says in reason, reason
    assert (_DELETE in reason) is advises, reason
    assert _listed_as_kept()[name] == reason


@pytest.mark.parametrize("name", [*STATE_NAMES, *CONFIG_NAMES])
def test_the_reasons_for_a_file_that_lacks_what_the_copy_holds(name: str) -> None:
    _bumped_profile()
    if name == "ui.toml.v1-backup":
        (_config() / name).write_bytes(V1_UI)
        ui_prefs.save(ui_prefs.UiPrefs())
        says = "ui.toml holds no interface choice yet, so it isn't shown to be in use."
    else:
        (_state() / name).write_bytes(
            {
                "playlists.json.v1-backup": _three_playlists(),
                "displays.json.v1-backup": _document(1, "displays", {"DP-9": "z"}),
                "schedules.json.v1-backup": _document(
                    None, "rules", [{"id": "o", "playlist": "x"}]
                ),
                "schedules.json.v2-backup": _document(2, "rules", [{"id": "o", "playlist": "x"}]),
            }[name]
        )
        says = " of the records the copy holds."

    reason = retired_backups.why_it_stays(_candidate(name))

    assert reason is not None and reason.startswith(f"{name} stays: ") and says in reason, reason
    assert _DELETE in reason


def test_a_copy_that_cannot_be_looked_at_is_named_and_left_alone() -> None:
    if os.geteuid() == 0:
        pytest.skip("root searches any folder")
    _bumped_profile()
    copy = _put_copy("playlists.json.v1-backup", _three_playlists())
    _state().chmod(0o000)
    try:
        reason = retired_backups.why_it_stays(_candidate(copy.name))
    finally:
        _state().chmod(0o755)

    assert reason is not None and reason.startswith(f"{copy.name} could not be looked at (")
    assert reason.endswith("so it is left alone.") and _DELETE not in reason


def test_the_live_file_vanishing_after_the_read_is_a_reason_too() -> None:
    _bumped_profile()
    copy = _put_copy("playlists.json.v1-backup", _three_playlists())
    candidate = replace(_candidate(copy.name), held=lambda _path: None)

    reason = retired_backups.why_it_stays(candidate)

    assert reason is not None and "is missing or could not be read in full." in reason
