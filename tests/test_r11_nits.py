"""Small things from the second review of the Workshop work.

A web preset while web wallpapers are off is said as that, not as waiting for
a newer service; a padded or cased ``application`` is labelled as the program
it is (and still never run).
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_web_wallpapers import _session
from wall_in_one import runtime_config
from wall_in_one.library import scan, workshop


def test_a_web_preset_while_web_is_off_is_not_said_to_need_a_newer_service(
    tmp_path: Path,
) -> None:
    """Before: "isn't played until the wallpaper service ..." although it never plays anyway."""
    settings, session = _session(tmp_path, allow=False)

    left_out = runtime_config.compiled(settings, session, presets=False).left_out

    assert all(one.owner != runtime_config.PRESET_OWNER for one in left_out), left_out
    assert "400" not in runtime_config.render(settings, session, presets=False)


def test_a_web_preset_with_web_on_still_waits_for_a_service_that_reads_presets(
    tmp_path: Path,
) -> None:
    settings, session = _session(tmp_path, allow=True)

    left_out = runtime_config.compiled(settings, session, presets=False).left_out

    assert [(one.owner, one.owner_id) for one in left_out] == [(runtime_config.PRESET_OWNER, "400")]


def test_a_padded_application_is_labelled_a_program_and_never_run(tmp_path: Path) -> None:
    """Before: "Scene · unknown", "with no type it can read"."""
    content = tmp_path / "steamapps" / "workshop" / "content" / workshop.APP_ID
    for folder, project in (
        ("101", {"type": " application ", "title": "Trojan", "file": "run.exe"}),
        ("102", {"type": "Application\t", "title": "Trojan 2", "file": "run.exe"}),
        ("103", {"title": "Its preset", "dependency": "101", "preset": {"a": 1}}),
        ("104", {"type": " APPLICATION", "title": "Preset-like", "preset": {}, "dependency": "9"}),
        ("105", {"type": "application ", "title": "Trailing", "file": "run.exe"}),
    ):
        (content / folder).mkdir(parents=True)
        (content / folder / "project.json").write_text(json.dumps(project))

    items = {item.scene: item for item in scan.workshop_items((tmp_path,))}

    for folder in ("101", "102", "103", "104"):
        item = items[folder]
        assert item.runs_program and item.unplayable is not None, folder
        assert "program" in item.unplayable_reason, folder
    assert items["101"].workshop_type == "application"
    assert items["102"].workshop_type == "application"
    assert items["105"].runs_program and items["105"].workshop_type == "application"
    assert items["105"].workshop_detail == "application"  # no trailing space in the label
