"""AE-1b: an import this build began resumes the report it journaled.

The scan for playlists with nothing to play is the one part of an import's
report that reads the library, which its legacy digest does not cover. A
still appearing or a video disappearing between a crash and the retry must
not make the journal "not describe this build's output".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.test_full8_migration_resume import _stillless_legacy_video
from tests.test_legacy_migration import (
    _bundle,
    isolated_xdg,  # noqa: F401 -- a fixture
)
from wall_in_one import legacy_migration


class _InterruptedError(Exception):
    pass


def _interrupt_after(
    monkeypatch: pytest.MonkeyPatch, legacy: Path, installed_targets: int
) -> dict[str, Any]:
    """Begin an import with this build and stop it after journaling."""
    install = legacy_migration._install_targets

    def stop(targets: Any) -> None:
        install(targets[:installed_targets])
        raise _InterruptedError

    with monkeypatch.context() as patch:
        patch.setattr(legacy_migration, "_install_targets", stop)
        with pytest.raises(_InterruptedError):
            legacy_migration.migrate(source_dir=legacy)
    assert not legacy_migration.marker_path().exists()
    return dict(json.loads(legacy_migration.journal_path().read_bytes()))


@pytest.mark.parametrize("installed_targets", [0, 1])
def test_a_still_appearing_after_the_interruption_does_not_block_the_retry(
    isolated_xdg: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    installed_targets: int,
) -> None:
    legacy, _original = _stillless_legacy_video(isolated_xdg)
    journal = _interrupt_after(monkeypatch, legacy, installed_targets)
    recorded_report = journal["targets"]["report"]

    # The video now has a still beside it. Neither the legacy digest nor the
    # converted stores cover that: only the playability scan sees it.
    (isolated_xdg / "videos" / "rain-still.png").write_bytes(b"png")

    looked = legacy_migration.probe(source_dir=legacy)
    assert looked.status == "in-progress", looked.detail
    result = legacy_migration.migrate(source_dir=legacy)

    assert result.changed
    assert "(see the report)" in result.detail, "the report this build staged names it"
    report = legacy_migration.report_path().read_bytes()
    assert hashlib.sha256(report).hexdigest() == recorded_report
    assert journal["nothing_to_play"] == ["Moving"]
    assert "“Moving” has nothing to play yet" in report.decode()
    assert legacy_migration.marker_path().is_file()
    assert not legacy_migration.journal_path().exists()
    assert "already recorded" in legacy_migration.decline(source_dir=legacy).detail


@pytest.mark.parametrize("installed_targets", [0, 1])
def test_a_deleted_video_after_the_interruption_does_not_block_the_retry(
    isolated_xdg: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    installed_targets: int,
) -> None:
    legacy, _original = _stillless_legacy_video(isolated_xdg)
    video = isolated_xdg / "videos" / "rain.mp4"
    (isolated_xdg / "videos" / "rain-still.png").write_bytes(b"png")
    journal = _interrupt_after(monkeypatch, legacy, installed_targets)
    recorded_report = journal["targets"]["report"]

    video.unlink()

    looked = legacy_migration.probe(source_dir=legacy)
    assert looked.status == "in-progress", looked.detail
    result = legacy_migration.migrate(source_dir=legacy)

    assert result.changed
    assert "nothing to play" not in result.detail, "it is the import's record that counts"
    assert "currently missing" not in legacy_migration.report_path().read_text("utf-8")
    report = legacy_migration.report_path().read_bytes()
    assert hashlib.sha256(report).hexdigest() == recorded_report
    assert journal["nothing_to_play"] == []
    assert not legacy_migration.journal_path().exists()
    assert "already recorded" in legacy_migration.decline(source_dir=legacy).detail


def test_a_fresh_import_still_reports_current_playability(
    isolated_xdg: Path,  # noqa: F811
) -> None:
    legacy, _original = _stillless_legacy_video(isolated_xdg)

    result = legacy_migration.migrate(source_dir=legacy)

    assert "“Moving” has nothing to play yet" in result.detail
    assert "(see the report)" in result.detail


def _tampered(
    base: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[dict[str, Any]], None],
) -> Path:
    legacy, _original = _stillless_legacy_video(base)
    journal = _interrupt_after(monkeypatch, legacy, 0)
    assert journal["nothing_to_play"] == ["Moving"]
    assert any("has nothing to play yet" in note for note in journal["report_notes"])
    change(journal)
    legacy_migration.journal_path().write_text(json.dumps(journal), encoding="utf-8")
    return legacy


def _drop_names(journal: dict[str, Any]) -> None:
    journal["nothing_to_play"] = []


def _drop_the_note(journal: dict[str, Any]) -> None:
    journal["report_notes"] = [
        note for note in journal["report_notes"] if "has nothing to play yet" not in note
    ]


def _edit_a_note(journal: dict[str, Any]) -> None:
    journal["report_notes"] = [note + " " for note in journal["report_notes"]]


def _forget_the_notes(journal: dict[str, Any]) -> None:
    del journal["report_notes"]


def _forget_the_names(journal: dict[str, Any]) -> None:
    del journal["nothing_to_play"]


@pytest.mark.parametrize(
    "change",
    [
        lambda journal: journal.update(nothing_to_play=["Moving", "Quiet"]),
        _drop_names,
        lambda journal: journal.update(nothing_to_play=["Moving", "Moving"]),
        lambda journal: journal.update(nothing_to_play=["Nowhere"]),
        lambda journal: journal.update(nothing_to_play="Moving"),
        lambda journal: journal.update(nothing_to_play=[1]),
        lambda journal: journal.update(report_notes="all"),
        _drop_the_note,
        _edit_a_note,
        _forget_the_notes,
        _forget_the_names,
        lambda journal: journal.update(extra=[]),
    ],
    ids=[
        "extra-name",
        "names-emptied",
        "name-repeated",
        "unknown-name",
        "names-not-a-list",
        "name-not-text",
        "notes-not-a-list",
        "note-dropped",
        "note-edited",
        "notes-removed",
        "names-removed",
        "unknown-field",
    ],
)
def test_a_tampered_playability_record_is_refused(
    isolated_xdg: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[dict[str, Any]], None],
) -> None:
    legacy = _tampered(isolated_xdg, monkeypatch, change)

    assert legacy_migration.probe(source_dir=legacy).status == "conflict"
    with pytest.raises(legacy_migration.MigrationError):
        legacy_migration.migrate(source_dir=legacy)
    assert not legacy_migration.marker_path().exists()
    assert legacy_migration.journal_path().exists()


def _many_missing_workshop_items(base: Path, count: int) -> Path:
    """A legacy playlist of ``count`` Workshop items, none installed."""
    legacy, _original = _stillless_legacy_video(base)
    config_path = legacy / "config.json"
    document = json.loads(config_path.read_bytes())
    entries = []
    for number in range(count):
        item = f"{1_000_000_000 + number}"
        name = f"workshop-{item}"
        bundle = _bundle(
            name, media={"kind": "workshop", "source": item}, still={"mode": "automatic"}
        )
        document["pairings"][name] = {**bundle, "id": name}
        entries.append({**bundle, "pairing_id": name})
    document["playlists"]["moving"]["entries"] = entries
    config_path.write_text(json.dumps(document), encoding="utf-8")
    return legacy


def _resume_a_journal_larger_than_a_megabyte(
    base: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    legacy = _many_missing_workshop_items(base, count)
    journal = _interrupt_after(monkeypatch, legacy, 1)
    size = legacy_migration.journal_path().stat().st_size
    assert size > legacy_migration.MAX_JOURNAL_BYTES, size
    assert journal["report_notes"]

    assert legacy_migration.probe(source_dir=legacy).status == "in-progress"
    assert legacy_migration.migrate(source_dir=legacy).changed
    assert not legacy_migration.journal_path().exists()
    assert legacy_migration.marker_path().is_file()


def test_a_journal_past_the_old_cap_still_resumes(
    isolated_xdg: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _resume_a_journal_larger_than_a_megabyte(isolated_xdg, monkeypatch, 5000)


def test_a_journal_that_could_not_be_read_back_is_refused_before_anything_is_written(
    isolated_xdg: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy, _original = _stillless_legacy_video(isolated_xdg)
    monkeypatch.setattr(legacy_migration, "MAX_JOURNAL_FILE_BYTES", 200)

    with pytest.raises(legacy_migration.MigrationError, match="nothing was written"):
        legacy_migration.migrate(source_dir=legacy)

    assert not legacy_migration.journal_path().exists()
    assert not legacy_migration.report_path().exists()
    assert not list(legacy_migration.journal_path().parent.glob(".*legacy-migration-v1.stage"))
