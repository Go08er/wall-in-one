"""Settings → Tidy up on the golden profile: every action, previewed, applied, undone.

For each action the preview must list exactly what changes (no other write
appears beside the documented backups and the archive), every backup and
archived item must be byte-identical to what it replaced, Undo must restore
every original byte, and applying again must change nothing. Noctalia is
faked: its reload is recorded, and a "render" is a rewrite of palette.json.
"""

from __future__ import annotations

import json
import os
import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

from tests.golden import harness
from tests.golden.harness import Change, Node
from tests.golden.sandbox import Golden
from wall_in_one import deployed_upgrade_transaction, paths, tidy, ui_prefs
from wall_in_one.theme import noctalia, template

NOCTALIA: Final = ".local/state/noctalia"
ARCHIVE: Final = ".config/wall-in-one/tidy-archive"
RETAINED: Final = ".wall-in-one-retained"


@pytest.fixture
def reloads(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Noctalia's config-reload, recorded instead of sent."""
    sent: list[str] = []
    monkeypatch.setattr(noctalia, "reload_config", lambda: sent.append("config-reload"))
    yield sent


def _age_claim_folders(home: Path) -> None:
    """Materializing gives every folder a fresh time; real leftovers are old."""
    old = time.time() - 2 * tidy.CLAIM_GRACE_SECONDS
    for folder in home.rglob(f"{RETAINED}/entry-*"):
        if folder.is_dir():
            os.utime(folder, (old, old))


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _relative(golden: Golden, path: Path) -> str:
    return path.relative_to(golden.profile.home).as_posix()


def _assert_restored(
    before: dict[str, Node],
    after: dict[str, Node],
    *,
    allowed_new: tuple[str, ...],
    changed_meanwhile: tuple[str, ...] = (),
) -> None:
    """Every original path is back with identical bytes and mode; nothing else appeared
    outside ``allowed_new``. ``changed_meanwhile`` were written by someone else."""
    for path, node in before.items():
        if path in changed_meanwhile:
            continue
        found = after.get(path)
        assert found is not None, f"undo lost {path}"
        assert (found.kind, found.mode, found.digest) == (node.kind, node.mode, node.digest), path
    extra = [
        path
        for path in sorted(set(after) - set(before))
        if not any(_under(path, prefix) or path.startswith(prefix) for prefix in allowed_new)
    ]
    assert extra == [], f"left behind after undo: {extra}"


def _settings_backups(golden: Golden) -> tuple[str, ...]:
    """What the template transaction leaves beside Noctalia's settings: the dated
    backup and the replaced file (``.original``), and its record's inert residue."""
    return (f"{NOCTALIA}/settings.toml.bak-wall-in-one-", f"{NOCTALIA}/{RETAINED}")


def _only(changes: list[Change], *, allowed: tuple[str, ...]) -> list[Change]:
    """The changes outside ``allowed`` prefixes."""
    return [
        change
        for change in changes
        if not any(
            _under(change.path, prefix) or change.path.startswith(prefix) for prefix in allowed
        )
    ]


# -- the preview ---------------------------------------------------------------------


def test_the_preview_reads_only(golden: Golden) -> None:
    _age_claim_folders(golden.profile.home)
    before = harness.snapshot(golden.profile.home)

    found = tidy.plan()

    harness.check_changes(harness.diff(before, harness.snapshot(golden.profile.home)), ())
    assert not paths.ui_prefs_path().exists()
    assert {action.action for action in found.offer} == {
        tidy.LEFTOVERS,
        tidy.PALETTE_TEMPLATE,
        tidy.PLUGIN_SETTINGS,
    }, "the thumbnail cache never triggers the offer, and the old template waits"


# -- action 1: leftovers -------------------------------------------------------------


def test_archiving_leftovers_matches_the_preview_and_undo_puts_back_every_byte(
    golden: Golden,
) -> None:
    home = golden.profile.home
    _age_claim_folders(home)
    finished = deployed_upgrade_transaction.finished_claims()
    assert finished is not None
    preview = tidy.plan().action(tidy.LEFTOVERS)
    planned = {_relative(golden, change.path) for change in preview.changes}
    upgrade_copies = {
        _relative(golden, path)
        for parent in (finished.settings_parent, finished.runtime_parent)
        for path in parent.glob(".wall-in-one-removal-*")
    }
    assert len(upgrade_copies) == 2
    assert upgrade_copies <= planned, "the finished upgrade's copies are provably inert"
    assert any("MotionBGS/.wall-in-one-removal-" in path for path in planned)
    assert any(f"Automatic Stills/{RETAINED}/entry-" in path for path in planned)
    assert preview.ready and not preview.kept
    before = harness.snapshot(home)

    result = tidy.apply(tidy.LEFTOVERS, preview)

    assert result.changed and result.archive is not None
    archive = _relative(golden, result.archive)
    after = harness.snapshot(home)
    changes = harness.diff(before, after)
    moved = {change.path for change in changes if change.kind == "deleted"}
    assert {path for path in moved if not any(_under(path, item) for item in planned)} == set()
    assert {path for path in planned if path not in moved} == set(), "a planned item stayed"
    created = [change.path for change in changes if change.kind == "created"]
    assert all(_under(path, ARCHIVE) for path in created), created
    assert [change for change in changes if change.kind not in ("deleted", "created")] == []

    manifest = json.loads((result.archive / "manifest.json").read_bytes())
    assert manifest["state"] == "applied"
    assert {_relative(golden, Path(item["original"])) for item in manifest["items"]} == planned
    for item in manifest["items"]:
        assert item["outcome"] == "moved"
        original = _relative(golden, Path(item["original"]))
        archived = f"{archive}/{item['archived']}"
        for path, node in before.items():
            if _under(path, original):
                copy = after[archived + path.removeprefix(original)]
                assert (copy.kind, copy.inode, copy.digest) == (node.kind, node.inode, node.digest)

    again = tidy.plan().action(tidy.LEFTOVERS)
    assert not again.changes and again.undo is not None
    unchanged = harness.snapshot(home)
    assert not tidy.apply(tidy.LEFTOVERS, again).changed
    harness.check_changes(harness.diff(unchanged, harness.snapshot(home)), ())
    assert deployed_upgrade_transaction.probe().status == "complete"

    undone = tidy.undo(tidy.LEFTOVERS)

    assert undone.changed
    restored = harness.snapshot(home)
    _assert_restored(before, restored, allowed_new=(ARCHIVE,))
    for path, node in before.items():
        assert restored[path].inode == node.inode, f"{path} came back as a copy"
    assert json.loads((result.archive / "manifest.json").read_bytes())["state"] == "undone"
    assert tidy.plan().action(tidy.LEFTOVERS).undo is None
    assert deployed_upgrade_transaction.probe().status == "complete"


def test_a_leftover_that_changed_after_the_preview_refuses_the_whole_apply(
    golden: Golden,
) -> None:
    home = golden.profile.home
    _age_claim_folders(home)
    preview = tidy.plan().action(tidy.LEFTOVERS)
    folder = next(
        change.path
        for change in preview.changes
        if change.path.is_dir() and not any(change.path.iterdir())
    )
    (folder / "entry").write_bytes(b"a claim in progress")
    before = harness.snapshot(home)

    with pytest.raises(tidy.TidyChangedError):
        tidy.apply(tidy.LEFTOVERS, preview)

    harness.check_changes(harness.diff(before, harness.snapshot(home)), ())
    fresh = tidy.plan().action(tidy.LEFTOVERS)
    assert folder not in {change.path for change in fresh.changes}
    assert any(kept.path == folder for kept in fresh.kept)


# -- action 2: the palette template name, then the old file ---------------------------


def _render(golden: Golden) -> None:
    """Noctalia renders the palette: palette.json gets new contents."""
    palette = golden.profile.app_state / "palette.json"
    document = json.loads(palette.read_bytes())
    document["mode"] = "light" if document.get("mode") == "dark" else "dark"
    replacement = palette.with_name("palette.json.noctalia")
    replacement.write_text(json.dumps(document))
    os.replace(replacement, palette)


def test_fixing_the_template_name_then_archiving_the_old_file_and_undoing_both(
    golden: Golden, reloads: list[str]
) -> None:
    home = golden.profile.home
    settings = golden.profile.state_home / "noctalia" / "settings.toml"
    stale = golden.profile.app_state / "palette.json.tmpl"
    target = template.installed_template_path()
    original = settings.read_bytes()
    preview = tidy.plan().action(tidy.PALETTE_TEMPLATE)
    assert preview.ready
    assert [change.path for change in preview.changes] == [settings, target]
    before = harness.snapshot(home)

    result = tidy.apply(tidy.PALETTE_TEMPLATE, preview)

    assert result.changed and result.archive is not None
    assert reloads == ["config-reload"]
    after = harness.snapshot(home)
    changes = harness.diff(before, after)
    outside = _only(changes, allowed=(ARCHIVE, *_settings_backups(golden)))
    assert sorted((change.path, change.kind) for change in outside) == [
        (f"{NOCTALIA}/settings.toml", "modified"),
        (_relative(golden, target), "created"),
    ]
    edited = settings.read_text()
    assert tomllib.loads(edited)["theme"]["templates"]["user"]["wall-in-one"]["input_path"] == str(
        target
    )
    old_lines = original.decode().splitlines()
    new_lines = edited.splitlines()
    assert [i for i, (a, b) in enumerate(zip(old_lines, new_lines, strict=True)) if a != b] == [
        next(i for i, line in enumerate(old_lines) if line.startswith("input_path"))
    ], "exactly one line changed"
    assert target.read_bytes() == stale.read_bytes()
    manifest = json.loads((result.archive / "manifest.json").read_bytes())
    for copy in (
        Path(manifest["backup"]),
        Path(manifest["displaced"]),
        result.archive / manifest["before_copy"],
    ):
        assert copy.read_bytes() == original, copy
    assert stale.exists(), "the old file stays until Noctalia rendered from the new one"

    again = tidy.plan()
    assert not again.action(tidy.PALETTE_TEMPLATE).changes
    unchanged = harness.snapshot(home)
    assert not tidy.apply(tidy.PALETTE_TEMPLATE, again.action(tidy.PALETTE_TEMPLATE)).changed
    harness.check_changes(harness.diff(unchanged, harness.snapshot(home)), ())
    waiting = again.action(tidy.OLD_PALETTE_TEMPLATE)
    assert waiting.changes and "Waiting for Noctalia" in waiting.blocked
    with pytest.raises(tidy.TidyError, match="Waiting"):
        tidy.apply(tidy.OLD_PALETTE_TEMPLATE, waiting)
    assert stale.exists()

    _render(golden)
    ready = tidy.plan().action(tidy.OLD_PALETTE_TEMPLATE)
    assert ready.ready and [change.path for change in ready.changes] == [stale]
    rendered = harness.snapshot(home)
    archived = tidy.apply(tidy.OLD_PALETTE_TEMPLATE, ready)
    assert archived.archive is not None
    moved = harness.diff(rendered, harness.snapshot(home))
    assert [(c.path, c.kind) for c in _only(moved, allowed=(ARCHIVE,))] == [
        (_relative(golden, stale), "deleted")
    ]
    kept = archived.archive / "items" / stale.name
    assert kept.read_bytes() == before[_relative(golden, stale)].content
    assert not tidy.plan().action(tidy.OLD_PALETTE_TEMPLATE).changes

    with pytest.raises(tidy.TidyError, match="old template file back first"):
        tidy.undo(tidy.PALETTE_TEMPLATE)
    assert tidy.undo(tidy.OLD_PALETTE_TEMPLATE).changed
    assert stale.read_bytes() == before[_relative(golden, stale)].content
    undone = tidy.undo(tidy.PALETTE_TEMPLATE)

    assert undone.changed and "back as they were" in undone.message
    assert settings.read_bytes() == original
    assert not target.exists(), "the template this action created went into the archive"
    _assert_restored(
        before,
        harness.snapshot(home),
        allowed_new=(ARCHIVE, *_settings_backups(golden)),
        changed_meanwhile=(_relative(golden, golden.profile.app_state / "palette.json"),),
    )
    assert reloads == ["config-reload", "config-reload"]
    assert tidy.plan().action(tidy.PALETTE_TEMPLATE).ready, "back to needing the fix"


# -- action 3: the retired plugin's settings -----------------------------------------


def _add_companion_settings(settings: Path) -> None:
    """The current companion shares the id; its own keys must survive."""
    text = settings.read_text()
    header = '[plugin_settings."goober/wall-in-one"]\n'
    settings.write_text(
        text.replace(
            header, header + 'refresh_interval_seconds = 30\ncontrols_placement = "floating"\n'
        )
    )


def test_removing_the_old_plugin_settings_keeps_the_companions_and_undoes(
    golden: Golden, reloads: list[str]
) -> None:
    home = golden.profile.home
    settings = golden.profile.state_home / "noctalia" / "settings.toml"
    _add_companion_settings(settings)
    original = settings.read_bytes()
    preview = tidy.plan().action(tidy.PLUGIN_SETTINGS)
    assert preview.ready
    assert sorted(
        change.detail.rsplit(" remove ", 1)[1].split(" =")[0] for change in preview.changes
    ) == [
        "capture_directory",
        "cycle_enabled",
        "cycle_interval",
        "mpvpaper_backend",
    ]
    assert sorted(kept.reason.split(":")[0] for kept in preview.kept) == [
        "controls_placement",
        "refresh_interval_seconds",
    ]
    before = harness.snapshot(home)

    result = tidy.apply(tidy.PLUGIN_SETTINGS, preview)

    assert result.changed and result.archive is not None and reloads == ["config-reload"]
    changes = _only(
        harness.diff(before, harness.snapshot(home)),
        allowed=(ARCHIVE, *_settings_backups(golden)),
    )
    assert [(change.path, change.kind) for change in changes] == [
        (f"{NOCTALIA}/settings.toml", "modified")
    ]
    edited = tomllib.loads(settings.read_text())
    assert edited["plugin_settings"]["goober/wall-in-one"] == {
        "refresh_interval_seconds": 30,
        "controls_placement": "floating",
    }
    expected = tomllib.loads(original.decode())
    for key in ("capture_directory", "cycle_enabled", "cycle_interval", "mpvpaper_backend"):
        del expected["plugin_settings"]["goober/wall-in-one"][key]
    assert edited == expected, "nothing else in Noctalia's settings changed"
    manifest = json.loads((result.archive / "manifest.json").read_bytes())
    assert Path(manifest["backup"]).read_bytes() == original
    assert (result.archive / manifest["before_copy"]).read_bytes() == original

    unchanged = harness.snapshot(home)
    assert not tidy.plan().action(tidy.PLUGIN_SETTINGS).changes
    assert not tidy.apply(tidy.PLUGIN_SETTINGS).changed
    harness.check_changes(harness.diff(unchanged, harness.snapshot(home)), ())

    assert tidy.undo(tidy.PLUGIN_SETTINGS).changed
    assert settings.read_bytes() == original
    _assert_restored(
        before, harness.snapshot(home), allowed_new=(ARCHIVE, *_settings_backups(golden))
    )


def test_undo_after_noctalia_saved_again_keeps_noctalias_new_settings(
    golden: Golden, reloads: list[str]
) -> None:
    settings = golden.profile.state_home / "noctalia" / "settings.toml"
    original = tomllib.loads(settings.read_text())
    tidy.apply(tidy.PLUGIN_SETTINGS, tidy.plan().action(tidy.PLUGIN_SETTINGS))
    replacement = settings.with_name("settings.toml.noctalia")
    replacement.write_text(settings.read_text() + '\n[bar]\nposition = "top"\n')
    os.replace(replacement, settings)

    undone = tidy.undo(tidy.PLUGIN_SETTINGS)

    assert "keep their new values" in undone.message
    restored = tomllib.loads(settings.read_text())
    assert restored.pop("bar") == {"position": "top"}
    assert restored == original


# -- Noctalia rewrites its settings between the preview and the apply ---------------------


def _noctalia_saves(settings: Path) -> bytes:
    replacement = settings.with_name("settings.toml.noctalia")
    replacement.write_text(settings.read_text().replace('mode = "dark"', 'mode = "light"'))
    os.replace(replacement, settings)
    return settings.read_bytes()


@pytest.mark.parametrize("action", [tidy.PALETTE_TEMPLATE, tidy.PLUGIN_SETTINGS])
def test_a_rewrite_after_the_preview_is_refused_without_writing(
    golden: Golden, reloads: list[str], action: tidy.Action
) -> None:
    settings = golden.profile.state_home / "noctalia" / "settings.toml"
    preview = tidy.plan().action(action)
    assert preview.ready
    saved = _noctalia_saves(settings)
    before = harness.snapshot(golden.profile.home)

    with pytest.raises(tidy.TidyChangedError, match="changed since the preview"):
        tidy.apply(action, preview)

    harness.check_changes(harness.diff(before, harness.snapshot(golden.profile.home)), ())
    assert settings.read_bytes() == saved and reloads == []
    fresh = tidy.plan().action(action)
    assert fresh.ready and fresh.token != preview.token
    assert tidy.apply(action, fresh).changed, "the new preview applies"


def test_a_rewrite_during_the_apply_is_refused_and_recorded_as_abandoned(
    golden: Golden, reloads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = golden.profile.state_home / "noctalia" / "settings.toml"
    preview = tidy.plan().action(tidy.PLUGIN_SETTINGS)
    backups_before = sorted(settings.parent.iterdir())
    real = template.edit_settings
    saved: list[bytes] = []

    def noctalia_saves_first(expected: str, transform: object) -> template.SettingsEdit:
        saved.append(_noctalia_saves(settings))
        return real(expected, transform)  # type: ignore[arg-type]

    monkeypatch.setattr(template, "edit_settings", noctalia_saves_first)
    with pytest.raises(tidy.TidyChangedError):
        tidy.apply(tidy.PLUGIN_SETTINGS, preview)

    assert settings.read_bytes() == saved[0] and reloads == []
    assert sorted(settings.parent.iterdir()) == backups_before, "no backup, record or residue"
    (archive,) = (paths.app_config_dir() / tidy.ARCHIVE_DIRECTORY).iterdir()
    assert json.loads((archive / "manifest.json").read_bytes())["state"] == "abandoned"
    assert tidy.plan().action(tidy.PLUGIN_SETTINGS).undo is None


# -- action 4: the thumbnail cache ---------------------------------------------------


def test_clearing_the_thumbnail_cache_deletes_only_thumbnails(golden: Golden) -> None:
    home = golden.profile.home
    preview = tidy.plan().action(tidy.THUMBNAIL_CACHE)
    assert preview.ready and preview.total_size > 0
    assert any("nothing is archived" in note for note in preview.notes)
    before = harness.snapshot(home)

    result = tidy.apply(tidy.THUMBNAIL_CACHE, preview)

    assert result.changed
    changes = harness.diff(before, harness.snapshot(home))
    assert changes and all(
        change.kind == "deleted" and change.path.startswith(".cache/wall-in-one/thumbnails/")
        for change in changes
    )
    assert not tidy.plan().action(tidy.THUMBNAIL_CACHE).changes
    assert not tidy.apply(tidy.THUMBNAIL_CACHE).changed
    with pytest.raises(tidy.TidyError, match="no Undo"):
        tidy.undo(tidy.THUMBNAIL_CACHE)
    assert not ui_prefs.load().raw


def test_a_reload_noctalia_never_confirmed_keeps_the_old_template_until_a_retry(
    golden: Golden, reloads: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's F-2: with the reload failed, a later palette may come from the
    running shell's old configuration, so it proves nothing and the old file stays."""
    stale = golden.profile.app_state / "palette.json.tmpl"
    monkeypatch.setattr(tidy, "_reload_noctalia", lambda: False)

    result = tidy.apply(tidy.PALETTE_TEMPLATE, tidy.plan().action(tidy.PALETTE_TEMPLATE))

    assert "didn't confirm" in result.message
    _render(golden)
    found = tidy.plan()
    old = found.action(tidy.OLD_PALETTE_TEMPLATE)
    assert old.changes and not old.ready and "didn't confirm" in old.blocked
    with pytest.raises(tidy.TidyError, match="didn't confirm"):
        tidy.apply(tidy.OLD_PALETTE_TEMPLATE, old)
    assert stale.exists()
    switch = found.action(tidy.PALETTE_TEMPLATE)
    assert switch.retry == "Retry Reload" and "hasn't confirmed" in switch.summary
    assert not tidy.retry(tidy.PALETTE_TEMPLATE).changed, "still no confirmation"

    monkeypatch.setattr(tidy, "_reload_noctalia", lambda: True)
    assert tidy.retry(tidy.PALETTE_TEMPLATE).changed
    after_retry = tidy.plan()
    assert not after_retry.action(tidy.PALETTE_TEMPLATE).retry
    assert "Waiting for Noctalia to render" in after_retry.action(tidy.OLD_PALETTE_TEMPLATE).blocked
    _render(golden)
    assert tidy.plan().action(tidy.OLD_PALETTE_TEMPLATE).ready


def test_putting_back_the_old_template_waits_for_its_name_to_be_free(
    golden: Golden, reloads: list[str]
) -> None:
    """F-4 also covers the old-template archive, which shares the Undo path."""
    stale = golden.profile.app_state / "palette.json.tmpl"
    original = stale.read_bytes()
    tidy.apply(tidy.PALETTE_TEMPLATE, tidy.plan().action(tidy.PALETTE_TEMPLATE))
    _render(golden)
    tidy.apply(tidy.OLD_PALETTE_TEMPLATE, tidy.plan().action(tidy.OLD_PALETTE_TEMPLATE))
    stale.write_bytes(b"someone else's file")

    first = tidy.undo(tidy.OLD_PALETTE_TEMPLATE)

    assert not first.changed and "can be tried again" in first.message
    assert stale.read_bytes() == b"someone else's file"
    pending = tidy.plan().action(tidy.OLD_PALETTE_TEMPLATE).undo
    assert pending is not None and pending.partial
    stale.unlink()

    assert tidy.undo(tidy.OLD_PALETTE_TEMPLATE).changed
    assert stale.read_bytes() == original
    assert tidy.plan().action(tidy.OLD_PALETTE_TEMPLATE).undo is None
