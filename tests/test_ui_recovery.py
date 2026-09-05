from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.gui
gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from wall_in_one import paths  # noqa: E402
from wall_in_one.ui.recovery import RecoveryWindow  # noqa: E402


def test_settings_launcher_requests_edit_access_without_changing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = paths.settings_path()
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b"roots = [broken")
    application = RecoveryWindow("Cannot parse settings")
    observed: list[tuple[str, bool]] = []

    def launch(launcher: Gtk.FileLauncher, *_arguments: object) -> None:
        target = launcher.get_file()
        assert target is not None
        path = target.get_path()
        assert path is not None
        observed.append((path, launcher.get_writable()))

    # Exercise the production launcher construction; no external program or
    # user's desktop is opened by this focused property check.
    monkeypatch.setattr(Gtk.FileLauncher, "launch", launch)
    try:
        application.open_path(settings)
        state = paths.runtime_config_path().parent
        application.open_path(state)
        assert observed == [(str(settings), True), (str(state), False)]
        assert settings.read_bytes() == b"roots = [broken"
    finally:
        application._colours.close()


def test_recovery_window_shows_literal_error_and_never_resets_files(tmp_path: Path) -> None:
    Gtk.init()
    Adw.init()
    settings = paths.settings_path()
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b"roots = [broken")
    application = RecoveryWindow("Cannot parse <settings.toml>: missing ]")
    try:
        assert application.register(None)
        application.activate()
        assert application.window is not None
        assert application._message is not None
        assert application._message.get_text() == "Cannot parse <settings.toml>: missing ]"
        assert application._message.get_selectable()
        assert bool(application.retry) is False
        assert settings.read_bytes() == b"roots = [broken"
        assert not paths.runtime_config_path().exists()
        application.retry_startup()
        assert bool(application.retry) is True
        assert settings.read_bytes() == b"roots = [broken"
    finally:
        application._colours.close()
        if application.window is not None:
            application.window.destroy()
