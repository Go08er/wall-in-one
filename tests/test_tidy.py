"""Tidy up's proofs and refusals, one at a time, in an isolated profile.

The golden-profile suite (``tests/golden/test_tidy.py``) runs every action
end to end. Here each rule that keeps something in place is pinned on its
own, along with the edits to a Noctalia-written settings file.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import stat
import time
import tomllib
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from tests import released
from tests.released import released_source  # noqa: F401 -- a fixture
from wall_in_one import (
    deployed_upgrade,
    deployed_upgrade_transaction,
    file_io,
    legacy_migration,
    paths,
    thumbnails,
    tidy,
)
from wall_in_one.library import manage, pairing, removals, stills
from wall_in_one.providers import download
from wall_in_one.theme import noctalia, template

FIXTURES: Final = Path(__file__).parent / "fixtures" / "companion"
OLD: Final = time.time() - 2 * tidy.CLAIM_GRACE_SECONDS
SETTINGS_TOKEN: Final = "1" * 32
RUNTIME_TOKEN: Final = "2" * 32

#: Shaped like a file Noctalia itself wrote: nested tables indented, arrays
#: across lines, the old plugin's settings and the stale template entry.
NOCTALIA_SETTINGS: Final = """\
config_version = 3

[bar]
    start = [
        "control-center",
        "wall-in-one"
    ]

[plugin_settings."goober/wall-in-one"]
capture_directory = "/home/someone/wallpapers"
cycle_interval_minutes = 5
hub_placement = "attached"
refresh_interval_seconds = 30

[plugin_settings."noctalia/notes"]
panel_placement = "attached"

[theme]
mode = "dark"

    [theme.templates]
    builtin_ids = [ "gtk3", "gtk4" ]

        [theme.templates.user.wall-in-one]
        enabled = true
        input_path = "{stale}"
        output_path = "{output}"
        post_hook = "/run/current-system/sw/bin/wall-in-one ctl reload-palette"

[wallpaper]
directory = "/home/someone/wallpapers"
"""


@pytest.fixture
def finished(monkeypatch: pytest.MonkeyPatch) -> deployed_upgrade_transaction.FinishedClaims:
    """A completed deployed upgrade whose first slots are the tokens above."""
    claims = deployed_upgrade_transaction.FinishedClaims(
        adoption_id="a" * 64,
        settings_parent=paths.app_config_dir(),
        settings_tokens=frozenset({SETTINGS_TOKEN}),
        runtime_parent=paths.app_state_dir(),
        runtime_tokens=frozenset({RUNTIME_TOKEN}),
    )
    monkeypatch.setattr(deployed_upgrade_transaction, "finished_claims", lambda: claims)
    paths.app_config_dir().mkdir(parents=True)
    paths.app_state_dir().mkdir(parents=True)
    return claims


def _noctalia_settings() -> Path:
    target = paths.noctalia_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        NOCTALIA_SETTINGS.format(
            stale=paths.app_state_dir() / "palette.json.tmpl",
            output=paths.palette_path(),
        )
    )
    return target


def _claim_folder(parent: Path, name: str = "entry-abcd1234", *, age: float = OLD) -> Path:
    folder = parent / file_io.RETAINED_ENTRY_DIRECTORY / name
    folder.mkdir(parents=True, mode=0o700)
    os.utime(folder, (age, age))
    return folder


def _record(parent: Path, data: bytes = b"", name: str = "0" * 32) -> Path:
    retained = parent / file_io.RETAINED_ENTRY_DIRECTORY
    retained.mkdir(parents=True, exist_ok=True, mode=0o700)
    record = retained / f"entry-{name}"
    record.write_bytes(data)
    return record


def _reasons(plan: tidy.ActionPlan) -> dict[Path, str]:
    return {kept.path: kept.reason for kept in plan.kept}


# -- the companion's settings ---------------------------------------------------------


def test_the_kept_keys_are_every_key_both_companion_releases_read() -> None:
    derived = frozenset().union(
        *(
            tidy.companion_setting_keys(tomllib.loads(fixture.read_text()))
            for fixture in sorted(FIXTURES.glob("plugin-*.toml"))
        )
    )
    assert len(list(FIXTURES.glob("plugin-*.toml"))) == 2
    assert derived == tidy.COMPANION_KEYS
    for retired in ("hub_placement", "hub_open_near_click", "capture_directory"):
        assert retired not in tidy.COMPANION_KEYS


# -- line-exact edits of a Noctalia-written file -----------------------------------------


def test_edits_change_only_their_own_lines_and_round_trip() -> None:
    text = NOCTALIA_SETTINGS.format(stale="/old/palette.json.tmpl", output="/out")
    document = tomllib.loads(text)

    fixed = tidy._set_template_input(text, document, "/old/palette.json.tmpl", "/new/x.tmpl")
    assert fixed == text.replace(
        'input_path = "/old/palette.json.tmpl"', 'input_path = "/new/x.tmpl"'
    )

    keys = ["capture_directory", "cycle_interval_minutes", "hub_placement"]
    removed_text, removed = tidy._remove_plugin_keys(text, document, keys)
    assert [line.strip() for line in removed] == [
        'capture_directory = "/home/someone/wallpapers"',
        "cycle_interval_minutes = 5",
        'hub_placement = "attached"',
    ]
    assert removed_text == "".join(
        line for line in text.splitlines(keepends=True) if line not in removed
    )
    restored = tidy._restore_plugin_keys(removed_text, tomllib.loads(removed_text), removed)
    assert tomllib.loads(restored) == document


@pytest.mark.parametrize(
    "layout",
    [
        '[plugin_settings]\n"goober/wall-in-one" = { capture_directory = "/x" }\n',
        '[plugin_settings."goober/wall-in-one"]\ncapture_directory = [\n  "/x",\n]\n',
        '[plugin_settings."goober/wall-in-one"]\nrefresh_interval_seconds = 5\n'
        '[plugin_settings."goober/wall-in-one".nested]\nx = 1\n',
    ],
    ids=["inline-table", "multi-line-value", "sub-table"],
)
def test_a_layout_it_cannot_edit_exactly_is_refused_not_guessed(layout: str) -> None:
    target = paths.noctalia_settings_path()
    target.parent.mkdir(parents=True)
    target.write_text(layout)
    before = target.read_bytes()

    found = tidy.plan(roots=()).action(tidy.PLUGIN_SETTINGS)

    assert found.changes and "can't be edited safely" in found.blocked
    assert not found.ready
    with pytest.raises(tidy.TidyError, match="can't be edited safely"):
        tidy.apply(tidy.PLUGIN_SETTINGS, found)
    assert target.read_bytes() == before


# -- what the leftovers action keeps, and why ------------------------------------------


def test_only_provably_inert_leftovers_move(
    tmp_path: Path, finished: deployed_upgrade_transaction.FinishedClaims
) -> None:
    config_dir = paths.app_config_dir()
    trash = manage.trash_directory()
    root = tmp_path / "library"
    motion = root / "Wall-in-One" / "MotionBGS"
    motion.mkdir(parents=True)
    prefix = file_io.DELETION_CLAIM_PREFIX

    upgrade_copy = config_dir / f"{prefix}{SETTINGS_TOKEN}"
    upgrade_copy.mkdir(mode=0o700)
    (upgrade_copy / "entry").write_text("roots = []\n")
    foreign = config_dir / f"{prefix}{'3' * 32}"
    foreign.mkdir(mode=0o700)
    crowded = paths.app_state_dir() / f"{prefix}{RUNTIME_TOKEN}"
    crowded.mkdir(mode=0o700)
    (crowded / "entry").write_text("schema_version = 2\n")
    (crowded / "stray").write_text("?")
    finished_removal = motion / f"{prefix}{'4' * 32}"
    finished_removal.mkdir(mode=0o700)
    unfinished_removal = motion / f"{prefix}{'5' * 32}"
    unfinished_removal.mkdir(mode=0o700)
    (unfinished_removal / "entry").write_bytes(b"video bytes")

    old_folder = _claim_folder(motion)
    recent_folder = _claim_folder(motion, "entry-recent00", age=time.time())
    full_folder = _claim_folder(motion, "entry-full0000")
    (full_folder / "entry").write_bytes(b"x")
    tombstone = _record(motion)
    linked = _record(motion, name="1" * 32)
    os.link(linked, tmp_path / "another-name")
    trash_copy = _record(trash, b"[Trash Info]\nPath=/x.png\nDeletionDate=2026-09-23T16:38:31\n")
    trash_folder = _claim_folder(trash)
    stray_fifo = config_dir / file_io.RETAINED_ENTRY_DIRECTORY / "entry-fifo0000"
    stray_fifo.parent.mkdir(mode=0o700)
    os.mkfifo(stray_fifo)

    found = tidy.plan(roots=(root,)).action(tidy.LEFTOVERS)

    assert {change.path for change in found.changes} == {
        upgrade_copy,
        finished_removal,
        old_folder,
        tombstone,
        trash_folder,
    }
    copy_change = next(change for change in found.changes if change.path == upgrade_copy)
    assert "settings.toml from before" in copy_change.detail and copy_change.size == 11
    reasons = _reasons(found)
    assert "isn't one of the finished upgrade's folders" in reasons[foreign]
    assert "besides the upgrade's copy" in reasons[crowded]
    assert "didn't finish" in reasons[unfinished_removal]
    assert "last hour" in reasons[recent_folder]
    assert "isn't empty" in reasons[full_folder]
    assert "another name still links" in reasons[linked]
    assert "a copy of a Trash record that holds 58 bytes" in reasons[trash_copy]
    assert "doesn't recognize" in reasons[stray_fifo]


def test_commit_named_download_files_are_listed_and_archived_never_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A download's file left at its commit name in a managed download folder (an
    interrupted publication, or one public for a moment that couldn't be retired)
    used to be invisible: nothing reclaims it. Tidy up now lists it, offering to
    archive one that is exactly Wall-in-One's (bytes kept, Undo puts it back),
    and keeps, with the reason, one in progress or not provably its own.

    Its ctime is set by its rename and can't be backdated, so "an hour later" is
    `tidy.plan(now=...)` (and Tidy's clock, for the apply)."""
    root = tmp_path / "library"
    motion = root / "Wall-in-One" / "MotionBGS"
    motion.mkdir(parents=True)

    def commit_file(name: str, data: bytes, *, mode: int = 0o600) -> Path:
        path = motion / f"{download.COMMIT_PREFIX}{name}"
        path.write_bytes(data)
        path.chmod(mode)
        return path

    left = commit_file("0123456789abcdef", b"once public video bytes")
    foreign_shape = commit_file("not-ours", b"someone else's")
    shared = commit_file("00112233445566ff", b"two names")
    os.link(shared, tmp_path / "another-name")
    public_mode = commit_file("aaaaaaaaaaaaaaaa", b"made public", mode=0o644)

    # Mid-publish: its bytes are old (mtime), its commit rename is now (ctime).
    os.utime(left, (OLD, OLD))
    now = tidy.plan(roots=(root,)).action(tidy.LEFTOVERS)
    # Failed on 220d011: with only its mtime old, it was offered for archiving.
    assert left not in {change.path for change in now.changes}
    assert "may belong to a download in progress" in _reasons(now)[left]

    later = time.time() + 2 * tidy.CLAIM_GRACE_SECONDS
    found = tidy.plan(roots=(root,), now=later).action(tidy.LEFTOVERS)

    # Failed on 1965700: none of these was listed at all.
    assert {change.path for change in found.changes} == {left}
    change = next(change for change in found.changes if change.path == left)
    assert "a download left while publishing it" in change.detail and change.size == 23
    reasons = _reasons(found)
    assert "doesn't recognize" in reasons[foreign_shape]
    assert "another name still links" in reasons[shared]
    assert "private ones Wall-in-One gives" in reasons[public_mode]

    # Tidy's own clock only (its module's `time`), an hour on.
    monkeypatch.setattr(
        tidy, "time", types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
    )
    result = tidy.apply(tidy.LEFTOVERS, found, roots=(root,))

    assert result.changed and result.archive is not None
    assert not left.exists()
    archived = [path for path in (result.archive / "items").iterdir()]
    assert [path.read_bytes() for path in archived] == [b"once public video bytes"]
    for kept in (foreign_shape, shared, public_mode):
        assert kept.exists(), kept
    # Tidy up's name for these is the download's own.
    assert getattr(tidy, "_COMMIT_PREFIX", None) == download.COMMIT_PREFIX


def kept_pairing_record(video: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A pairing record Wall-in-One really kept beside ``video``: its publication
    is rolled back (a directory fsync EIO), which retires the record whole into
    the video's folder. Returns the kept copy."""
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"not decoded here")
    chosen = video.with_name("chosen.png")
    chosen.write_bytes(b"png")
    sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    real_sync = paths.fsync_directory
    failed: list[Path] = []

    def sync(directory: Path) -> None:
        if not failed and sidecar.exists():
            failed.append(directory)
            raise OSError(errno.EIO, "injected directory sync failure")
        real_sync(directory)

    with monkeypatch.context() as patching:
        patching.setattr(paths, "fsync_directory", sync)
        with pytest.raises(stills.StillError):
            stills.write_sidecar(video, chosen)
    (copy,) = (
        entry
        for entry in (video.parent / file_io.RETAINED_ENTRY_DIRECTORY).iterdir()
        if entry.is_file() and entry.stat().st_size > 0
    )
    return copy


def test_a_copy_kept_beside_an_ordinary_video_is_listed_as_kept_never_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AB-7. A pairing record that was rolled back is retired whole beside its
    video: in the wallpaper root itself, or a folder under it. Those are the
    person's folders, which the survey never looked into. The copies are now
    listed as kept, each root always and each folder that holds a library item;
    nothing there is ever offered, and no walk of the library is made."""
    root = tmp_path / "library"
    nested = root / "trips" / "2025"
    at_root = kept_pairing_record(root / "clip.mp4", monkeypatch)
    in_nested = kept_pairing_record(nested / "clip.mp4", monkeypatch)
    # What keeps nothing is passed over: an emptied record, an empty claim folder.
    empty_record = _record(nested, name="e" * 32)
    empty_folder = _claim_folder(nested, "entry-empty000")
    # Not beside anything the app knows, or not under a root: not looked for.
    elsewhere = kept_pairing_record(root / "elsewhere" / "clip.mp4", monkeypatch)
    outside = kept_pairing_record(tmp_path / "outside" / "clip.mp4", monkeypatch)

    found = tidy.plan(roots=(root,)).action(tidy.LEFTOVERS)
    # Failed on 267c301: nothing beside a video was listed at all.
    assert at_root in _reasons(found), "the root itself is always looked at"
    assert in_nested not in _reasons(found), "a folder nobody named is not walked into"

    folders = (root, nested, tmp_path / "outside")
    named = tidy.plan(roots=(root,), item_folders=folders).action(tidy.LEFTOVERS)
    reasons = _reasons(named)
    for copy in (at_root, in_nested):
        assert reasons[copy].startswith("a copy Wall-in-One kept instead of deleting it")
        assert f"holds {tidy.format_size(copy.stat().st_size)}" in reasons[copy]
    assert not {empty_record, empty_folder, elsewhere, outside} & set(reasons)
    offered = {change.path for change in named.changes}
    assert not any(root in path.parents for path in offered), "nothing of theirs is offered"
    assert named.token == found.token, "what is offered, and so apply, is unchanged"
    for copy in (at_root, in_nested, elsewhere, outside):
        assert copy.stat().st_size > 0, "a preview reads only"


@contextlib.contextmanager
def _unsearchable(folder: Path) -> Iterator[None]:
    """``folder`` readable but not searchable (a native EACCES on what it holds)."""
    mode = stat.S_IMODE(folder.stat().st_mode)
    folder.chmod(0o600)
    try:
        yield
    finally:
        folder.chmod(mode)


def test_a_kept_copy_in_a_folder_that_cannot_be_looked_into_is_said_not_passed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cold review's probe of AB-7: the folder beside the videos loses its
    search permission. The copy is there; Tidy up can't look. It says so, about
    the retained folder it couldn't look into, instead of listing nothing."""
    root = tmp_path / "library"
    nested = root / "trips"
    copy = kept_pairing_record(nested / "clip.mp4", monkeypatch)
    retained = nested / file_io.RETAINED_ENTRY_DIRECTORY
    listed = tidy.plan(roots=(root,), item_folders=(nested,)).action(tidy.LEFTOVERS)
    assert copy in _reasons(listed), "the control: listed while it can be looked at"

    with _unsearchable(nested):
        found = tidy.plan(roots=(root,), item_folders=(nested,)).action(tidy.LEFTOVERS)

    assert copy.stat().st_size > 0, "the copy is still there"
    # Failed on 29999ba: nothing at all was said about the folder.
    assert "it can't be looked into (Permission denied)" in _reasons(found)[retained]
    assert not found.changes


def test_what_the_survey_cannot_look_at_is_listed_and_never_offered(tmp_path: Path) -> None:
    """The same in the survey of Wall-in-One's own folders. An empty claim folder
    that would be archived is, once it can't be inspected, listed with the reason
    and not offered; a download folder whose retained folder, or whose own
    entries, can't be looked into is listed too."""
    root = tmp_path / "library"
    managed = root / "Wall-in-One"
    motion = managed / "MotionBGS"
    motion.mkdir(parents=True)
    in_state = _claim_folder(paths.app_state_dir())
    in_motion = _claim_folder(motion)
    offered = {change.path for change in tidy.plan(roots=(root,)).action(tidy.LEFTOVERS).changes}
    assert {in_state, in_motion} <= offered, "the control: both are archived when inspectable"

    with _unsearchable(in_state.parent), _unsearchable(motion):
        found = tidy.plan(roots=(root,)).action(tidy.LEFTOVERS)
    reasons = _reasons(found)
    # Failed on 29999ba: each was skipped without a word.
    assert reasons[in_state] == "it can't be inspected (Permission denied)"
    assert reasons[motion / file_io.RETAINED_ENTRY_DIRECTORY] == (
        "it can't be looked into (Permission denied)"
    )
    assert not {in_state, in_motion} & {change.path for change in found.changes}

    with _unsearchable(managed):
        found = tidy.plan(roots=(root,)).action(tidy.LEFTOVERS)
    assert _reasons(found)[managed] == "what it holds can't be looked into (Permission denied)"
    assert not any(managed in change.path.parents for change in found.changes)


def _archived_download_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, float]:
    """A download's commit-named file (it holds data), archived by Tidy up.

    Returns the wallpaper root, where the file was, the archive, and "now".
    """
    root = tmp_path / "library"
    motion = root / "Wall-in-One" / "MotionBGS"
    motion.mkdir(parents=True)
    left = motion / f"{download.COMMIT_PREFIX}0123456789abcdef"
    left.write_bytes(b"once public video bytes")
    left.chmod(0o600)
    later = time.time() + 2 * tidy.CLAIM_GRACE_SECONDS
    monkeypatch.setattr(
        tidy, "time", types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
    )
    found = tidy.plan(roots=(root,), now=later).action(tidy.LEFTOVERS)
    result = tidy.apply(tidy.LEFTOVERS, found, roots=(root,))
    assert result.archive is not None and not left.exists()
    return root, left, result.archive, later


def test_an_undo_that_cannot_look_into_the_archive_writes_nothing_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-review's probe. The archive's items folder loses its search
    permission, and the person chooses Undo. The file is in the archive; Undo
    can't look at it. Nothing is called missing: the record stays, the reason
    is given, Undo stays offered, and it works once the folder can be looked into."""
    root, left, archive, later = _archived_download_file(tmp_path, monkeypatch)
    (archived,) = (archive / "items").iterdir()

    with _unsearchable(archive / "items"):
        undone = tidy.undo(tidy.LEFTOVERS)

    manifest = json.loads((archive / "manifest.json").read_text())
    (record,) = manifest["items"]
    assert archived.read_bytes() == b"once public video bytes", "it is still in the archive"
    # Failed on 148eb31: "1 item is in neither place any more", marked undone.
    assert "neither place" not in undone.message, undone.message
    assert "1 item couldn't be checked" in undone.message and not undone.changed
    assert record["outcome"] == "moved" and manifest["state"] == "applied"
    assert record["undo_error"] == "the archive couldn't be checked (Permission denied)"
    assert tidy.plan(roots=(root,), now=later).action(tidy.LEFTOVERS).undo is not None

    again = tidy.undo(tidy.LEFTOVERS)
    assert again.changed and left.read_bytes() == b"once public video bytes"


def test_a_move_whose_arrival_cannot_be_checked_is_not_recorded_as_never_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Apply's twin: the move into the archive lands, and right then the archive's
    items folder loses its search permission, so the move can be neither verified
    nor undone, and where the item is can't be looked at. It is not journalled as
    "stayed where it was": the record stays planned, the message says it couldn't
    be checked, and Undo finds it in the archive and puts it back."""
    root = tmp_path / "library"
    motion = root / "Wall-in-One" / "MotionBGS"
    motion.mkdir(parents=True)
    left = motion / f"{download.COMMIT_PREFIX}0123456789abcdef"
    left.write_bytes(b"once public video bytes")
    left.chmod(0o600)
    later = time.time() + 2 * tidy.CLAIM_GRACE_SECONDS
    monkeypatch.setattr(
        tidy, "time", types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
    )
    found = tidy.plan(roots=(root,), now=later).action(tidy.LEFTOVERS)
    real_rename = file_io._rename_noreplace
    sealed: list[Path] = []

    def rename(source: Path, destination: Path) -> None:
        real_rename(source, destination)
        if not sealed and destination.parent.name == "items":
            sealed.append(destination.parent)
            destination.parent.chmod(0o600)  # there, and no longer searchable

    monkeypatch.setattr(file_io, "_rename_noreplace", rename)
    try:
        result = tidy.apply(tidy.LEFTOVERS, found, roots=(root,))
    finally:
        for folder in sealed:
            folder.chmod(0o700)
    monkeypatch.setattr(file_io, "_rename_noreplace", real_rename)

    assert result.archive is not None and sealed
    (record,) = json.loads((result.archive / "manifest.json").read_text())["items"]
    (archived,) = (result.archive / "items").iterdir()
    assert archived.read_bytes() == b"once public video bytes" and not left.exists()
    # Failed on 148eb31: outcome "skipped: ...", "1 item ... stayed where it was".
    assert record["outcome"] == "planned" and "couldn't be checked" in record["note"]
    assert "stayed where it was" not in result.message, result.message
    assert "Where 1 item ended up couldn't be checked" in result.message

    undone = tidy.undo(tidy.LEFTOVERS)
    assert undone.changed and left.read_bytes() == b"once public video bytes"


def test_archives_that_cannot_be_looked_into_are_said_not_taken_for_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archive folder itself can't be searched: its archives can't be read, so
    no Undo can be offered. The plan says why, and Undo says it couldn't look,
    not "There's nothing to undo"."""
    root, _left, archive, later = _archived_download_file(tmp_path, monkeypatch)

    with _unsearchable(archive.parent):
        found = tidy.plan(roots=(root,), now=later).action(tidy.LEFTOVERS)
        undone = tidy.undo(tidy.LEFTOVERS)

    assert found.undo is None
    # Failed on 148eb31: no note, and "There's nothing to undo."
    assert any("can't be looked into (Permission denied)" in note for note in found.notes)
    assert "can't be looked into" in undone.message and "nothing was undone" in undone.message
    assert tidy.plan(roots=(root,), now=later).action(tidy.LEFTOVERS).undo is not None


def test_a_file_that_cannot_be_looked_at_is_not_called_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wording, in the safe direction: Noctalia's settings and the old palette
    template, in folders that can't be searched, are said to be unlookable, not
    "weren't found" or "There's no old template file"."""
    settings = _noctalia_settings()
    with _unsearchable(settings.parent):
        found, why = tidy._read_noctalia()
    # Failed on 148eb31: (None, ""), which the plans show as "weren't found".
    assert found is None and why == "Noctalia's settings can't be looked at (Permission denied)."

    folder = tmp_path / "state-elsewhere"
    folder.mkdir()
    stale = folder / "palette.json.tmpl"
    stale.write_text("{}")
    monkeypatch.setattr(tidy, "_stale_template", lambda: stale)
    with _unsearchable(folder):
        plan, _status = tidy._plan_old_palette_template(None, [])
    # Failed on 148eb31: "There's no old template file."
    assert plan.summary == (
        "The old template file can't be looked at (Permission denied), so it's left alone."
    )
    assert not plan.changes


def test_a_managed_folder_that_cannot_be_listed_is_said_once(tmp_path: Path) -> None:
    """A row for a thing is said once, not once by each part of the survey."""
    root = tmp_path / "library"
    managed = root / "Wall-in-One"
    (managed / "MotionBGS").mkdir(parents=True)
    managed.chmod(0o300)  # searchable, not listable
    try:
        found = tidy.plan(roots=(root,)).action(tidy.LEFTOVERS)
    finally:
        managed.chmod(0o700)

    rows = [kept for kept in found.kept if kept.path == managed]
    # Failed on 148eb31: the same row twice.
    assert [kept.reason for kept in rows] == ["it can't be listed (Permission denied)"]


#: The released 0.2.2 rolls a pairing record back: its own code, in a child.
_RELEASED_ROLLBACK = r"""
import errno

from wall_in_one import paths
from wall_in_one.library import pairing, stills

video = Path(sys.argv[1])
sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
real_sync = paths.fsync_directory
failed = []


def sync(directory):
    if not failed and sidecar.exists():
        failed.append(directory)
        raise OSError(errno.EIO, "injected directory sync failure")
    real_sync(directory)


paths.fsync_directory = sync
try:
    stills.write_sidecar(video, video.with_name("chosen.png"))
except stills.StillError:
    print("ROLLED-BACK", flush=True)
"""


def test_what_the_released_version_left_beside_a_video_is_passed_over(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Review rule 4 for AB-7: what can a retained folder 0.2.2 left beside a video
    hold? Its rollback (the released code, run for real) discards the record, so
    what stays there is emptied: it keeps nothing, and is neither listed as kept
    nor offered. Same folder name and record names as today's."""
    root = tmp_path / "library"
    video = root / "trips" / "clip.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"not decoded here")
    video.with_name("chosen.png").write_bytes(b"png")

    done = released.run(released_source, _RELEASED_ROLLBACK, str(video))

    assert done.returncode == 0 and "ROLLED-BACK" in done.stdout, done.stdout + done.stderr
    retained = video.parent / file_io.RETAINED_ENTRY_DIRECTORY
    left = sorted(retained.iterdir())
    assert left, "the released rollback left its residue beside the video"
    for entry in left:  # an emptied record, and the empty claim folder it was emptied in
        assert entry.stat().st_size == 0 if entry.is_file() else not any(entry.iterdir()), entry
    found = tidy.plan(
        roots=(root,), item_folders=(video.parent,), now=time.time() + 2 * 24 * 3600
    ).action(tidy.LEFTOVERS)
    assert not set(left) & set(_reasons(found))
    assert not set(left) & {change.path for change in found.changes}


def test_upgrade_folders_stay_until_the_upgrade_is_recorded_finished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deployed_upgrade_transaction, "finished_claims", lambda: None)
    folder = paths.app_config_dir() / f"{file_io.DELETION_CLAIM_PREFIX}{SETTINGS_TOKEN}"
    folder.mkdir(parents=True, mode=0o700)

    found = tidy.plan(roots=()).action(tidy.LEFTOVERS)

    assert not found.changes
    assert "isn't recorded as finished" in _reasons(found)[folder]


def test_a_removal_folder_stays_while_the_journal_could_still_need_it(
    tmp_path: Path, finished: deployed_upgrade_transaction.FinishedClaims
) -> None:
    root = tmp_path / "library"
    downloads = root / "Wall-in-One" / "Wallhaven"
    downloads.mkdir(parents=True)
    token = "6" * 32
    folder = downloads / f"{file_io.DELETION_CLAIM_PREFIX}{token}"
    folder.mkdir(mode=0o700)
    journal = removals.state_path()

    journal.write_text('{"version": 1, "removals": [')
    assert "can't be read" in _reasons(tidy.plan(roots=(root,)).action(tidy.LEFTOVERS))[folder]

    journal.unlink()
    journal.with_name(journal.name + ".broken").write_text("{}")
    assert "older copy" in _reasons(tidy.plan(roots=(root,)).action(tidy.LEFTOVERS))[folder]


# -- refusals while something else owns the files --------------------------------------


def test_noctalia_edits_wait_for_an_unfinished_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    _noctalia_settings()
    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "probe",
        lambda: deployed_upgrade.Probe("in-progress", "journal awaits resume"),
    )

    found = tidy.plan(roots=())

    for action in (tidy.PALETTE_TEMPLATE, tidy.PLUGIN_SETTINGS):
        assert found.action(action).changes
        assert "upgrade hasn't finished" in found.action(action).blocked
    assert not found.offer


def test_old_plugin_settings_wait_for_the_import_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    _noctalia_settings()
    real = legacy_migration.probe
    monkeypatch.setattr(
        legacy_migration,
        "probe",
        lambda **_keywords: legacy_migration.Probe("ready", "importable", real().source),
    )

    found = tidy.plan(roots=()).action(tidy.PLUGIN_SETTINGS)

    assert "import decision" in found.blocked
    assert tidy.plan(roots=()).action(tidy.PALETTE_TEMPLATE).ready


# -- interrupted applies can be undone ------------------------------------------------


def test_an_archive_interrupted_between_moves_is_undone_from_its_manifest(
    tmp_path: Path,
    finished: deployed_upgrade_transaction.FinishedClaims,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = paths.app_state_dir()
    first = _claim_folder(parent, "entry-aaaaaaaa")
    second = _claim_folder(parent, "entry-bbbbbbbb")
    record = _record(parent)
    preview = tidy.plan(roots=()).action(tidy.LEFTOVERS)
    assert len(preview.changes) == 3
    real = tidy._move
    moves = 0

    def power_cut_after_one(source: Path, destination: Path, item: object) -> None:
        nonlocal moves
        if moves == 1:
            raise KeyboardInterrupt
        moves += 1
        real(source, destination, item)  # type: ignore[arg-type]

    monkeypatch.setattr(tidy, "_move", power_cut_after_one)
    with pytest.raises(KeyboardInterrupt):
        tidy.apply(tidy.LEFTOVERS, preview)
    monkeypatch.setattr(tidy, "_move", real)
    assert sum(path.exists() for path in (first, second, record)) == 2

    interrupted = tidy.plan(roots=()).action(tidy.LEFTOVERS)
    assert interrupted.undo is not None and "stopped part way" in interrupted.undo.detail
    tidy.undo(tidy.LEFTOVERS)

    assert first.is_dir() and second.is_dir() and record.is_file()


def test_a_settings_edit_interrupted_after_its_exchange_is_still_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _noctalia_settings()
    original = settings.read_bytes()
    monkeypatch.setattr(noctalia, "reload_config", lambda: None)

    def power_cut() -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(tidy, "_reload_noctalia", power_cut)
    with pytest.raises(KeyboardInterrupt):
        tidy.apply(tidy.PLUGIN_SETTINGS, tidy.plan(roots=()).action(tidy.PLUGIN_SETTINGS))
    (archive,) = tidy.archive_root().iterdir()
    assert json.loads((archive / "manifest.json").read_bytes())["state"] == "applying"
    assert settings.read_bytes() != original

    found = tidy.plan(roots=()).action(tidy.PLUGIN_SETTINGS)
    assert not found.changes and found.undo is not None
    monkeypatch.setattr(tidy, "_reload_noctalia", lambda: True)
    tidy.undo(tidy.PLUGIN_SETTINGS)

    assert settings.read_bytes() == original


def test_format_size_reads_like_the_file_manager() -> None:
    assert tidy.format_size(0) == "0 bytes"
    assert tidy.format_size(1) == "1 byte"
    assert tidy.format_size(953) == "953 bytes"
    assert tidy.format_size(258_528_584) == "258.5 MB"


# -- the thumbnail cache stays inside the directory it inspected ----------------------


def test_clearing_the_cache_never_follows_a_swapped_directory_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's race (F-1): after the scan, the cache directory is renamed
    and a link to another folder takes its name. A same-named file there must
    survive; only the inspected directory's entries go."""
    cache = thumbnails.cache_directory()
    cache.mkdir(parents=True)
    name = "a" * 32 + ".png"
    (cache / name).write_bytes(b"\x89PNG cached")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    foreign = elsewhere / name
    foreign.write_bytes(b"someone else's file")
    preview = tidy.plan(roots=()).action(tidy.THUMBNAIL_CACHE)
    assert preview.ready
    moved = cache.with_name("thumbnails.moved")
    real_scan = thumbnails._scan

    def scan_then_swap_the_directory(*arguments: int) -> tuple[list[object], list[object]]:
        found = real_scan(*arguments)
        cache.rename(moved)
        cache.symlink_to(elsewhere)
        return found  # type: ignore[return-value]

    monkeypatch.setattr(thumbnails, "_scan", scan_then_swap_the_directory)

    result = tidy.apply(tidy.THUMBNAIL_CACHE, preview)

    assert foreign.read_bytes() == b"someone else's file"
    assert not (moved / name).exists(), "the inspected directory's entry was the one cleared"
    assert result.changed


def test_a_linked_cache_directory_is_shown_as_left_alone(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ("b" * 32 + ".png")).write_bytes(b"\x89PNG")
    thumbnails.cache_directory().parent.mkdir(parents=True)
    thumbnails.cache_directory().symlink_to(elsewhere)

    found = tidy.plan(roots=()).action(tidy.THUMBNAIL_CACHE)

    assert not found.ready and "link to another folder" in found.blocked
    with pytest.raises(tidy.TidyError, match="Nothing was deleted"):
        tidy.apply(tidy.THUMBNAIL_CACHE, found)
    assert (elsewhere / ("b" * 32 + ".png")).exists()


# -- a settings edit that committed before a later step failed ------------------------


def test_a_committed_edit_whose_cleanup_failed_stays_undoable_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The review's F-3: keeping the replaced file fails after the atomic exchange.
    The edit is live, so it must not be journaled as abandoned or reported as
    unchanged, and Undo must still put the original bytes back."""
    settings = _noctalia_settings()
    original = settings.read_bytes()
    monkeypatch.setattr(noctalia, "reload_config", lambda: None)
    real_preserve = template._preserve_regular_at_backup

    def fails_after_the_exchange(*_arguments: object, **_keywords: object) -> Path:
        raise OSError(5, "simulated I/O error after the exchange")

    monkeypatch.setattr(template, "_preserve_regular_at_backup", fails_after_the_exchange)

    result = tidy.apply(tidy.PLUGIN_SETTINGS, tidy.plan(roots=()).action(tidy.PLUGIN_SETTINGS))

    assert settings.read_bytes() != original, "the exchange committed"
    assert result.changed
    assert "weren't changed" not in result.message
    assert "The change was made, but a step after it failed" in result.message
    (archive,) = tidy.archive_root().iterdir()
    manifest = json.loads((archive / "manifest.json").read_bytes())
    assert manifest["state"] == "applied" and "simulated I/O error" in manifest["tail_error"]
    found = tidy.plan(roots=()).action(tidy.PLUGIN_SETTINGS)
    assert not found.changes and found.undo is not None

    monkeypatch.setattr(template, "_preserve_regular_at_backup", real_preserve)
    undone = tidy.undo(tidy.PLUGIN_SETTINGS)

    assert undone.changed
    assert settings.read_bytes() == original


def test_a_failure_before_the_exchange_is_still_reported_as_nothing_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _noctalia_settings()
    original = settings.read_bytes()
    monkeypatch.setattr(noctalia, "reload_config", lambda: None)

    def refuses_before_the_exchange(*_arguments: object, **_keywords: object) -> Path:
        raise template.TemplateInstallError("simulated: no room for the backup")

    monkeypatch.setattr(template, "_backup", refuses_before_the_exchange)

    with pytest.raises(tidy.TidyError, match="weren't changed"):
        tidy.apply(tidy.PLUGIN_SETTINGS, tidy.plan(roots=()).action(tidy.PLUGIN_SETTINGS))

    assert settings.read_bytes() == original
    (archive,) = tidy.archive_root().iterdir()
    assert json.loads((archive / "manifest.json").read_bytes())["state"] == "abandoned"
    assert tidy.plan(roots=()).action(tidy.PLUGIN_SETTINGS).undo is None


# -- partial archive and Undo failures stay recoverable --------------------------------


def _manifest(action: str) -> dict[str, object]:
    (archive,) = (
        directory for directory in tidy.archive_root().iterdir() if directory.name.endswith(action)
    )
    document = json.loads((archive / "manifest.json").read_bytes())
    assert isinstance(document, dict)
    return document


def test_a_folder_that_fills_up_and_cannot_go_back_is_journaled_as_archived(
    finished: deployed_upgrade_transaction.FinishedClaims, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's F-4 case A: the moved claim folder gains a file, and its old
    name is taken again, so it can't be put back. It must be recorded where it
    really is, reported, and restorable by Undo."""
    folder = _claim_folder(paths.app_state_dir(), "entry-aaaaaaaa")
    real_move = tidy._move

    def move_then_write_and_reclaim(source: Path, destination: Path, item: object) -> None:
        real_move(source, destination, item)  # type: ignore[arg-type]
        (destination / "live-evidence").write_bytes(b"written by the claim's owner")
        source.mkdir(mode=0o700)

    monkeypatch.setattr(tidy, "_move", move_then_write_and_reclaim)
    result = tidy.apply(tidy.LEFTOVERS, tidy.plan(roots=()).action(tidy.LEFTOVERS))
    monkeypatch.setattr(tidy, "_move", real_move)

    assert result.changed and "changed while it was being archived" in result.message
    items = _manifest("leftovers")["items"]
    assert isinstance(items, list) and len(items) == 1
    item = items[0]
    assert item["outcome"] == "moved" and "gained contents" in item["note"]
    found = tidy.plan(roots=()).action(tidy.LEFTOVERS)
    assert found.undo is not None
    folder.rmdir()  # whoever took the name lets it go

    assert tidy.undo(tidy.LEFTOVERS).changed
    assert (folder / "live-evidence").read_bytes() == b"written by the claim's owner"
    assert _manifest("leftovers")["state"] == "undone"


def test_an_undo_that_cannot_put_everything_back_can_be_tried_again(
    finished: deployed_upgrade_transaction.FinishedClaims,
) -> None:
    """The review's F-4 case B: a new folder holds an archived item's name."""
    folder = _claim_folder(paths.app_state_dir(), "entry-bbbbbbbb")
    record = _record(paths.app_state_dir())
    tidy.apply(tidy.LEFTOVERS, tidy.plan(roots=()).action(tidy.LEFTOVERS))
    folder.mkdir(mode=0o700)

    first = tidy.undo(tidy.LEFTOVERS)

    assert first.changed and "can be tried again" in first.message
    assert record.is_file() and not any(folder.iterdir())
    retry = tidy.plan(roots=()).action(tidy.LEFTOVERS).undo
    assert retry is not None and retry.partial and "Put back 1 item" in retry.detail
    assert ". An earlier Undo couldn't put these back" in retry.detail
    assert _manifest("leftovers")["state"] == "applied"
    folder.rmdir()

    second = tidy.undo(tidy.LEFTOVERS)

    assert second.changed and folder.is_dir()
    assert _manifest("leftovers")["state"] == "undone"
    assert tidy.plan(roots=()).action(tidy.LEFTOVERS).undo is None


def test_keeping_a_partly_undone_archive_stops_offering_undo(
    finished: deployed_upgrade_transaction.FinishedClaims,
) -> None:
    folder = _claim_folder(paths.app_state_dir(), "entry-cccccccc")
    tidy.apply(tidy.LEFTOVERS, tidy.plan(roots=()).action(tidy.LEFTOVERS))
    folder.mkdir(mode=0o700)
    tidy.undo(tidy.LEFTOVERS)

    kept = tidy.keep_archived(tidy.LEFTOVERS)

    assert kept.changed and kept.archive is not None
    assert tidy.plan(roots=()).action(tidy.LEFTOVERS).undo is None
    assert _manifest("leftovers")["state"] == "kept"
    assert any((kept.archive / "items").iterdir()), "the item stays in the archive"


def test_clearing_the_cache_takes_what_is_there_now_not_what_was_previewed() -> None:
    """Review Q2: the cache is the documented exception to preview equality."""
    cache = thumbnails.cache_directory()
    cache.mkdir(parents=True)
    (cache / ("c" * 32 + ".png")).write_bytes(b"\x89PNG one")
    preview = tidy.plan(roots=()).action(tidy.THUMBNAIL_CACHE)
    assert any("doesn't wait for an unchanged list" in note for note in preview.notes)
    (cache / ("d" * 32 + ".png")).write_bytes(b"\x89PNG made while browsing")
    assert tidy.plan(roots=()).action(tidy.THUMBNAIL_CACHE).token != preview.token

    result = tidy.apply(tidy.THUMBNAIL_CACHE, preview)

    assert result.changed and "Cleared 2 thumbnails" in result.message
    assert list(cache.iterdir()) == []


# -- a manifest is never written past what its reader takes ---------------------------


def _archives() -> list[Path]:
    root = tidy.archive_root()
    return sorted(root.iterdir()) if root.is_dir() else []


def test_an_archive_whose_manifest_would_be_unreadable_is_refused_before_moving(
    finished: deployed_upgrade_transaction.FinishedClaims, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest is read back with a cap (MAX_MANIFEST_BYTES). Written past
    it, the archive is invisible to Undo. With a reduced budget, Apply is
    refused before anything is created or moved."""
    parent = paths.app_state_dir()
    leftovers = [
        _claim_folder(parent, "entry-aaaaaaaa"),
        _claim_folder(parent, "entry-bbbbbbbb"),
        _record(parent),
    ]
    preview = tidy.plan(roots=()).action(tidy.LEFTOVERS)
    assert len(preview.changes) == 3
    monkeypatch.setattr(tidy, "MAX_MANIFEST_BYTES", 512)

    with pytest.raises(tidy.TidyError, match="Nothing was changed") as refused:
        tidy.apply(tidy.LEFTOVERS, preview)

    assert "more than the 512 bytes this version can read back" in str(refused.value)
    assert all(path.exists() for path in leftovers), "nothing was moved"
    assert _archives() == [], "no archive was started"
    again = tidy.plan(roots=()).action(tidy.LEFTOVERS)
    assert len(again.changes) == 3 and again.undo is None

    # With the real budget, the same plan archives normally.
    monkeypatch.setattr(tidy, "MAX_MANIFEST_BYTES", 4 * 1024 * 1024)
    done = tidy.apply(tidy.LEFTOVERS, again)
    assert done.changed and not any(path.exists() for path in leftovers)


def test_an_edit_whose_manifest_would_be_unreadable_is_refused_before_editing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _noctalia_settings()
    original = settings.read_bytes()
    monkeypatch.setattr(noctalia, "reload_config", lambda: None)
    preview = tidy.plan(roots=()).action(tidy.PLUGIN_SETTINGS)
    assert preview.changes
    monkeypatch.setattr(tidy, "MAX_MANIFEST_BYTES", 512)

    with pytest.raises(tidy.TidyError, match="Nothing was changed"):
        tidy.apply(tidy.PLUGIN_SETTINGS, preview)

    assert settings.read_bytes() == original
    assert _archives() == []


def test_an_undo_that_could_leave_its_manifest_unreadable_is_refused(
    finished: deployed_upgrade_transaction.FinishedClaims, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Undo records each item's outcome, so its manifest grows too. Short of
    room for that, it is refused before anything is put back."""
    folder = _claim_folder(paths.app_state_dir(), "entry-cccccccc")
    record = _record(paths.app_state_dir())
    tidy.apply(tidy.LEFTOVERS, tidy.plan(roots=()).action(tidy.LEFTOVERS))
    (archive,) = _archives()
    before = (archive / "manifest.json").read_bytes()
    # Room to read the manifest as it is, not for what Undo would add.
    monkeypatch.setattr(tidy, "MAX_MANIFEST_BYTES", len(before) + 64)

    with pytest.raises(tidy.TidyError, match="Nothing was changed"):
        tidy.undo(tidy.LEFTOVERS)

    assert not folder.exists() and not record.exists(), "nothing was put back"
    assert (archive / "manifest.json").read_bytes() == before
    assert tidy.plan(roots=()).action(tidy.LEFTOVERS).undo is not None


def test_journal_text_is_bounded_and_keeps_both_ends() -> None:
    """An item's later notes add a bounded amount, which the room check counts on."""
    long = "first-file " + "näme/" * 3000 + " the reason it failed"
    text = tidy._journal_text(OSError(5, long))
    assert len(json.dumps(text)) <= tidy.MAX_JOURNAL_TEXT_BYTES
    assert text.startswith("[Errno 5] first-file") and text.endswith("the reason it failed")
    assert tidy._journal_text("short") == "short"
