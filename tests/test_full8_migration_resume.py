"""AE-1: resume the released import's bytes, not today's extra report notes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests import released
from tests.released import released_source  # noqa: F401 -- a fixture
from tests.test_legacy_migration import (
    _legacy_fixture,
    isolated_xdg,  # noqa: F401 -- a fixture
)
from wall_in_one import legacy_migration, paths
from wall_in_one.library import playlists


def _stillless_legacy_video(base: Path) -> tuple[Path, dict[Path, bytes]]:
    """Local media, all present, but the video has no automatic or chosen still."""
    legacy, _watched, video, _manual, automatic = _legacy_fixture(base)
    automatic.with_name(automatic.name + ".wall-in-one.json").unlink()
    automatic.unlink()
    (legacy / "runtime.json").write_text(
        json.dumps({"schema_version": 6, "pair_registry": {}, "pairs": {}}) + "\n",
        encoding="utf-8",
    )
    assert video.is_file()
    return legacy, {
        path: path.read_bytes()
        for path in (
            legacy / "config.json",
            legacy / "runtime.json",
            paths.noctalia_settings_path(),
        )
    }


@pytest.mark.parametrize("installed_targets", [0, 3])
def test_a_released_import_with_a_stillless_video_resumes_its_report(
    isolated_xdg: Path,  # noqa: F811
    released_source: Path,  # noqa: F811
    installed_targets: int,
) -> None:
    """Use the frozen producer's actual journal, before or partway through install.

    The released converter did not write the new nothing-to-play report note.
    Its original report digest must still match when this build resumes it.
    """
    legacy, original = _stillless_legacy_video(isolated_xdg)
    made = released.run(
        released_source,
        """
from wall_in_one import legacy_migration

install = legacy_migration._install_targets

def stop_after_journaling(targets):
    assert legacy_migration.journal_path().is_file()
    install(targets[:int(sys.argv[1])])
    raise SystemExit(73)

legacy_migration._install_targets = stop_after_journaling
legacy_migration.migrate()
""",
        str(installed_targets),
    )
    assert made.returncode == 73, made.stdout + made.stderr
    assert not legacy_migration.marker_path().exists()
    journal = json.loads(legacy_migration.journal_path().read_bytes())
    expected_report_digest = journal["targets"]["report"]

    looked = legacy_migration.probe(source_dir=legacy)
    assert looked.status == "in-progress", looked.detail
    result = legacy_migration.migrate(source_dir=legacy)

    assert result.changed
    assert "has nothing to play yet" in result.detail
    assert "(see the report)" not in result.detail
    report = legacy_migration.report_path().read_bytes()
    assert hashlib.sha256(report).hexdigest() == expected_report_digest
    assert b"## Rollback" in report, "the released journal bound the released report"
    assert b"has nothing to play yet" not in report
    assert legacy_migration.marker_path().is_file()
    assert not legacy_migration.journal_path().exists()
    store = playlists.Store.open()
    assert store.fault is None
    assert {playlist.name for playlist in store.all()} == {"Moving", "Quiet"}
    assert {path: path.read_bytes() for path in original} == original
    assert not legacy_migration.migrate(source_dir=legacy).changed


def test_a_new_import_still_reports_the_stillless_video(
    isolated_xdg: Path,  # noqa: F811
) -> None:
    """The compatibility rendering must not remove today's import diagnostic."""
    legacy, original = _stillless_legacy_video(isolated_xdg)

    result = legacy_migration.migrate(source_dir=legacy)

    assert result.changed
    assert "has nothing to play yet" in result.detail
    report = legacy_migration.report_path().read_text(encoding="utf-8")
    assert "\u201cMoving\u201d has nothing to play yet" in report
    assert "playback goes on without this playlist" in report
    assert "Rollback" not in report
    assert {path: path.read_bytes() for path in original} == original
