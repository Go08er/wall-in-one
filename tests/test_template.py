from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from wall_in_one import paths
from wall_in_one.theme import template


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every XDG root at a scratch directory.

    The installer edits Noctalia's real settings file, so nothing in this
    module may run against the user's actual configuration.
    """
    for variable, name in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_RUNTIME_DIR", "run"),
    ):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(variable, str(directory))
    # Never touch the real shell from a test.
    monkeypatch.setattr("wall_in_one.theme.noctalia.reload_config", lambda: None)
    return tmp_path


def _write_noctalia_settings(body: str) -> Path:
    path = paths.noctalia_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


SAMPLE_SETTINGS = """\
[theme]
source = "wallpaper"

    [theme.templates]
    builtin_ids = [ "gtk3", "qt" ]
    enable_builtin_templates = true

[wallpaper]
directory = "/home/someone/wallpapers"
"""


def test_install_appends_a_valid_registration(fake_home: Path) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)

    result = template.install()

    assert result.changed
    assert result.backup_path is not None and result.backup_path.is_file()

    parsed = tomllib.loads(settings_path.read_text(encoding="utf-8"))
    entry = parsed["theme"]["templates"]["user"]["wall-in-one"]
    assert entry["enabled"] is True
    assert entry["input_path"] == str(template.installed_template_path())
    assert entry["output_path"] == str(paths.palette_path())
    assert entry["post_hook"].endswith("ctl reload-palette")

    # Pre-existing settings must survive untouched.
    assert parsed["theme"]["templates"]["builtin_ids"] == ["gtk3", "qt"]
    assert parsed["wallpaper"]["directory"] == "/home/someone/wallpapers"


def test_install_copies_the_template_to_a_stable_path(fake_home: Path) -> None:
    _write_noctalia_settings(SAMPLE_SETTINGS)
    template.install()

    installed = template.installed_template_path()
    assert installed.is_file()
    assert installed.read_bytes() == template.bundled_template().read_bytes()
    # Must not point into the package directory, which moves on every Nix
    # rebuild and would leave a dangling reference.
    assert "site-packages" not in str(installed)


def test_install_is_idempotent(fake_home: Path) -> None:
    _write_noctalia_settings(SAMPLE_SETTINGS)
    first = template.install()
    second = template.install()

    assert first.changed
    assert not second.changed
    assert second.detail == "already registered"

    body = paths.noctalia_settings_path().read_text(encoding="utf-8")
    assert body.count(template._BEGIN_MARKER) == 1


def test_install_refuses_to_clobber_a_hand_written_entry(fake_home: Path) -> None:
    _write_noctalia_settings(
        SAMPLE_SETTINGS
        + '\n[theme.templates.user.wall-in-one]\nenabled = true\ninput_path = "/somewhere/else"\n'
    )
    with pytest.raises(template.TemplateInstallError, match="not written by us"):
        template.install()


def test_uninstall_removes_only_our_block(fake_home: Path) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    template.install()

    result = template.uninstall()

    assert result.changed
    body = settings_path.read_text(encoding="utf-8")
    assert template._BEGIN_MARKER not in body
    parsed = tomllib.loads(body)
    assert "user" not in parsed["theme"]["templates"]
    assert parsed["theme"]["templates"]["builtin_ids"] == ["gtk3", "qt"]
    assert parsed["wallpaper"]["directory"] == "/home/someone/wallpapers"


def test_uninstall_is_a_no_op_when_absent(fake_home: Path) -> None:
    _write_noctalia_settings(SAMPLE_SETTINGS)
    result = template.uninstall()
    assert not result.changed
    assert result.detail == "not registered"


def test_install_reports_a_missing_settings_file(fake_home: Path) -> None:
    with pytest.raises(template.TemplateInstallError, match="not found"):
        template.install()


def test_install_reports_malformed_settings(fake_home: Path) -> None:
    _write_noctalia_settings("this = is = not = toml")
    with pytest.raises(template.TemplateInstallError, match="not valid TOML"):
        template.install()


def test_status_tracks_installation(fake_home: Path) -> None:
    _write_noctalia_settings(SAMPLE_SETTINGS)
    assert template.status() == "not installed"
    template.install()
    assert template.status().startswith("installed, enabled")
