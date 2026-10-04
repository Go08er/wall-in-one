"""The Settings page's template row: reading never writes, the button's path refuses safely.

`template.inspect` classifies the registration the way `template.install`
would act on it; `real_settings.read_template` turns that into the row's state
and button; `real_settings.install_template` is what the button runs on the
authoring lane. Noctalia's settings live in the test's own XDG directories
(conftest); Noctalia itself is never asked anything (its reload and render
are recorded instead).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from wall_in_one import paths
from wall_in_one.theme import noctalia, template
from wall_in_one.ui.next import real_settings

SAMPLE = """\
[theme]
source = "wallpaper"

[wallpaper]
directory = "/home/someone/wallpapers"
"""


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """What Noctalia was asked to do (reload its settings, render its templates)."""
    calls: list[str] = []
    monkeypatch.setattr(noctalia, "reload_config", lambda: calls.append("reload"))
    monkeypatch.setattr(noctalia, "apply_templates", lambda: calls.append("render"))
    yield calls


def _settings(body: str) -> Path:
    path = paths.noctalia_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _noctalia_files() -> dict[str, bytes | None]:
    """Every entry in Noctalia's settings directory, with its bytes.

    Only Noctalia's own files: `install` may already have published the
    content-addressed template copy in Wall-in-One's state directory before
    a refusal, as its documented order has it.
    """
    directory = paths.noctalia_settings_path().parent
    return {
        entry.name: entry.read_bytes() if entry.is_file() else None
        for entry in sorted(directory.iterdir())
    }


def _snapshot() -> dict[str, bytes | None]:
    """Every entry beside Noctalia's settings and in the app's state, with its bytes."""
    found: dict[str, bytes | None] = {}
    for directory in (paths.noctalia_settings_path().parent, paths.app_state_dir()):
        if not directory.exists():
            continue
        for entry in sorted(directory.iterdir()):
            found[str(entry)] = entry.read_bytes() if entry.is_file() else None
    return found


def _managed(input_path: str, *, enabled: bool = True) -> str:
    """A registration as Wall-in-One writes it, pointing at ``input_path``."""
    return SAMPLE + "\n".join(
        (
            "",
            "# >>> wall-in-one palette template (managed) >>>",
            "[theme.templates.user.wall-in-one]",
            f"enabled = {'true' if enabled else 'false'}",
            f'input_path = "{input_path}"',
            f'output_path = "{paths.palette_path()}"',
            'post_hook = "wall-in-one ctl reload-palette"',
            "# <<< wall-in-one palette template (managed) <<<",
            "",
        )
    )


# -- each state, read without writing --------------------------------------------------


def test_without_noctalias_settings_there_is_nothing_to_install() -> None:
    before = _snapshot()
    check = template.inspect()
    assert check.state == "unavailable" and not check.repairable
    assert "weren't found" in check.detail
    row = real_settings.read_template()
    assert row.state == "unavailable" and row.action == ""
    assert _snapshot() == before


def test_a_missing_registration_offers_install_and_reading_writes_nothing() -> None:
    _settings(SAMPLE)
    before = _snapshot()
    row = real_settings.read_template()
    assert (row.state, row.action, row.blocked) == ("missing", "Install", "")
    assert "Not installed" in row.detail
    assert _snapshot() == before, "inspect never writes, not even the template copy"
    assert not template.installed_template_path().exists()


def test_an_installed_registration_offers_no_button_and_says_whether_it_rendered(
    asked: list[str],
) -> None:
    _settings(SAMPLE)
    template.install()
    row = real_settings.read_template()
    assert (row.state, row.action) == ("installed", "")
    assert "hasn't rendered" in row.detail
    paths.palette_path().write_text("{}", encoding="utf-8")
    row = real_settings.read_template()
    assert row.state == "installed" and "hasn't rendered" not in row.detail


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (lambda: _managed(str(paths.app_state_dir() / "palette.json.tmpl")), "old template"),
        (lambda: _managed("/nix/store/old-wall-in-one/palette.json.tmpl"), "another template"),
        (lambda: _managed(str(template.installed_template_path()), enabled=False), "turned off"),
        (lambda: _managed(str(template.installed_template_path())), "missing"),
    ],
)
def test_a_stale_registration_offers_repair_with_its_reason(body: object, reason: str) -> None:
    assert callable(body)
    _settings(body())
    before = _snapshot()
    row = real_settings.read_template()
    assert (row.state, row.action) == ("stale", "Repair"), row
    assert reason in row.detail
    assert _snapshot() == before


def test_an_entry_wall_in_one_did_not_write_is_left_to_the_person() -> None:
    _settings(
        SAMPLE + '\n[theme.templates.user.wall-in-one]\nenabled = true\ninput_path = "/elsewhere"\n'
    )
    row = real_settings.read_template()
    assert (row.state, row.action) == ("unavailable", "")
    assert "wasn't written by Wall-in-One" in row.detail


def test_an_interrupted_change_is_offered_as_a_repair_not_finished_by_reading() -> None:
    settings = _settings(SAMPLE)
    locator = settings.with_name(f".{settings.name}.wall-in-one-transaction-record")
    locator.write_text("interrupted", encoding="utf-8")
    before = _snapshot()
    row = real_settings.read_template()
    assert (row.state, row.action) == ("stale", "Repair")
    assert "interrupted" in row.detail
    assert _snapshot() == before


def test_an_upgrade_still_running_turns_the_button_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(SAMPLE)
    monkeypatch.setattr(real_settings, "upgrade_blocked", lambda: "the upgrade hasn't finished")
    row = real_settings.read_template()
    assert (row.action, row.blocked) == ("Install", "the upgrade hasn't finished")
    with pytest.raises(template.TemplateInstallError, match="hasn't finished"):
        real_settings.install_template()
    assert "wall-in-one" not in paths.noctalia_settings_path().read_text(encoding="utf-8")


# -- the button's path ---------------------------------------------------------------------


def test_the_button_registers_backs_up_and_asks_noctalia_to_render(asked: list[str]) -> None:
    settings = _settings(SAMPLE)
    result = real_settings.install_template()
    assert result.changed and result.detail == "registered"
    assert result.backup_path is not None and result.backup_path.read_text() == SAMPLE
    assert "[theme.templates.user.wall-in-one]" in settings.read_text(encoding="utf-8")
    assert asked == ["reload", "render"]
    assert real_settings.read_template().state == "installed"


def test_a_size_refusal_leaves_noctalias_files_byte_identical(asked: list[str]) -> None:
    """H-3: refused before any backup or transaction file; the settings stay readable."""
    line = "# " + "x" * 1022 + "\n"
    room = template.MAX_NOCTALIA_SETTINGS_BYTES - len(SAMPLE) - 64
    _settings(SAMPLE + line * (room // len(line)) + "#" * (room % len(line)) + "\n")
    noctalia_before = _noctalia_files()
    with pytest.raises(template.TemplateInstallError) as refused:
        real_settings.install_template()
    assert "Noctalia's settings weren't changed" in str(refused.value)
    assert _noctalia_files() == noctalia_before, "no backup, no transaction file, same bytes"
    assert asked == [], "Noctalia isn't asked to reload what didn't change"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the read-only directory")
def test_a_backup_that_cannot_be_written_is_a_refusal_with_nothing_changed(
    asked: list[str],
) -> None:
    settings = _settings(SAMPLE)
    noctalia_dir = settings.parent
    before = _noctalia_files()
    noctalia_dir.chmod(0o555)
    try:
        with pytest.raises(template.TemplateInstallError) as refused:
            real_settings.install_template()
    finally:
        noctalia_dir.chmod(0o755)
    assert "cannot create a backup" in str(refused.value)
    assert "Noctalia's settings weren't changed" in str(refused.value)
    assert _noctalia_files() == before
    assert asked == []
