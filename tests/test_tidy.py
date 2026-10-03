"""Tidy up's proofs and refusals, one at a time, in an isolated profile.

The golden-profile suite (``tests/golden/test_tidy.py``) runs every action
end to end. Here each rule that keeps something in place is pinned on its
own, along with the edits to a Noctalia-written settings file.
"""

from __future__ import annotations

import json
import os
import time
import tomllib
from pathlib import Path
from typing import Final

import pytest

from wall_in_one import (
    deployed_upgrade,
    deployed_upgrade_transaction,
    file_io,
    legacy_migration,
    paths,
    thumbnails,
    tidy,
)
from wall_in_one.library import manage, removals
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
