"""Every lane that publishes "Allow web wallpapers: off": no failure leaves a web page playing.

Review r11c's `test_probe_web_off_lanes.py`, made tests. Each turns the setting
off through the API both windows use (`update_settings_async`), over a
``runtime.toml`` that plays a web page, with the service's socket replaced by
a recorder (no real service, systemctl or bus is touched):

* the compile fails, on the GUI lane (the control: 785faf7 sent ``stop``) and
  on ``--service``'s synchronous one (L2: it sent nothing);
* the compile succeeds and the service refuses the reload (M1: nothing sent).

A ``stop`` alone doesn't last (M2: ``play`` starts the page again), so where
the service reads schema 8 it is also given a document that plays nothing and
told to load it, and where it doesn't (one older than this app, still
running after an update) it is told to quit, so that whatever starts it next
runs the updated one through ExecStartPre's guard.
"""

from __future__ import annotations

import json
import tomllib
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_codex030_round1b import profiles  # noqa: E402, F401
from tests.test_ui_web_off_failed_publish import DOCUMENT, WEB, _steam_with_web  # noqa: E402
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402

READS_EIGHT = [4, 5, 6, 7, 8]
OLDER = [4, 5]


def _turn_off(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    *,
    service: bool,
    fail_compile: bool,
    refuse_reload: bool,
    schemas: list[int] | None = None,
) -> tuple[Any, list[str], list[str]]:
    """Turn the setting off; what was sent to the service, and what the app said.

    ``refuse_reload``: the service refuses every document but one that plays
    nothing. ``schemas``: what its status lists (None: a reply that isn't one).
    """
    profile = profiles(service=service)
    application = profile.application
    _steam_with_web()
    config.save(config.Settings(roots=application.settings.roots, allow_web_wallpapers=True))
    application._settings = config.load_strict()
    application._settings_requested = application._settings
    paths.runtime_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_config_path().write_text(DOCUMENT, encoding="utf-8")
    sent: list[str] = []
    reports: list[str] = []

    def send(verb: str, *_a: object, **_k: object) -> Response:
        sent.append(verb)
        if verb == "status" and schemas is not None:
            return Response.success(
                json.dumps({"status_version": 2, "supported_config_schemas": schemas})
            )
        if verb == "reload" and refuse_reload and not _plays_nothing():
            return Response.failure("refused: a bound this service enforces")
        return Response.success()

    monkeypatch.setattr(client, "send_runtime", send)
    original_report = application.window_report

    def report(message: str) -> None:
        reports.append(message)
        original_report(message)

    monkeypatch.setattr(application, "window_report", report)
    if fail_compile:

        def failing(*_a: object, **_k: object) -> object:
            raise runtime_config.RuntimeConfigError("a library folder can't be read")

        # Both lanes' compile step: the GUI's request worker and the sync path's writer.
        monkeypatch.setattr(application, "_compile_runtime_request", failing)
        monkeypatch.setattr(application, "_installed_leaving_out", failing)
    done: list[object] = []
    assert application.update_settings_async(
        allow_web_wallpapers=False, on_success=done.append, on_error=done.append
    )
    spin_until(
        lambda: (
            bool(done)
            and not application._runtime_compile_pending
            and not application._runtime_publication_held
        ),
        30,
        what="the save and its publication",
    )
    assert config.load_strict().allow_web_wallpapers is False
    return profile, sent, reports


def _recorded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A service that answers every verb; what it was sent."""
    sent: list[str] = []

    def send(verb: str, *_a: object, **_k: object) -> Response:
        sent.append(verb)
        return Response.success()

    monkeypatch.setattr(client, "send_runtime", send)
    return sent


def _names_page() -> bool:
    return f'scene_id = "{WEB}"' in paths.runtime_config_path().read_text(encoding="utf-8")


def _plays_nothing() -> bool:
    document = tomllib.loads(paths.runtime_config_path().read_text(encoding="utf-8"))
    playlists = document.get("playlists", [])
    return bool(playlists) and all(
        not playlist.get("entries") and playlist.get("nothing_to_play") for playlist in playlists
    )


# -- the review's three lanes ----------------------------------------------------------------


def test_gui_compile_fails(profiles: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """The control: the GUI lane's failed compile stopped the page before, and still does."""
    profile, sent, _reports = _turn_off(
        profiles, monkeypatch, service=False, fail_compile=True, refuse_reload=False
    )
    assert "stop" in sent
    assert "Electric Eye" in profile.application.publication_problem


def test_service_compile_fails(profiles: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """L2. Before: ``sent: []`` and runtime.toml still named the page."""
    _profile, sent, reports = _turn_off(
        profiles,
        monkeypatch,
        service=True,
        fail_compile=True,
        refuse_reload=False,
        schemas=READS_EIGHT,
    )
    assert sent[:2] == ["stop", "status"] and "reload" in sent
    assert not _names_page() and _plays_nothing()
    said = " ".join(reports)
    assert "can't be read" in said
    assert "now has nothing to play: web wallpapers are off" in said


def test_gui_reload_refused(profiles: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """M1. Before: the refused reload sent no ``stop``, and the service kept the page."""
    profile, sent, _reports = _turn_off(
        profiles,
        monkeypatch,
        service=False,
        fail_compile=False,
        refuse_reload=True,
        schemas=READS_EIGHT,
    )
    assert "stop" in sent and sent.index("reload") < sent.index("stop")
    # The service now holds the document that plays nothing: it took the second reload.
    assert sent[-1] == "reload" and _plays_nothing()
    problem = profile.application.publication_problem
    assert problem.startswith("Runtime rejected the new configuration: refused")
    assert "still plays Electric Eye" in problem
    assert "nothing to play: web wallpapers are off" in problem


# -- what makes it last, and an older service ------------------------------------------------


def test_after_the_guard_the_page_cant_come_back_from_the_service(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M2: what the service holds once the guard ran names no page, whatever it is sent."""
    _turn_off(
        profiles,
        monkeypatch,
        service=False,
        fail_compile=True,
        refuse_reload=False,
        schemas=READS_EIGHT,
    )
    held = paths.runtime_config_path().read_text(encoding="utf-8")
    assert WEB not in held and _plays_nothing()
    assert runtime_config.retained_web_wallpapers(text=held) == ()
    assert runtime_config.unchecked_scene_wallpapers(text=held) == ()


@pytest.mark.parametrize("service", [False, True], ids=("gui", "--service"))
def test_an_older_service_that_cant_play_nothing_is_told_to_quit(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    service: bool,
) -> None:
    """A service that predates schema 8 can't be given nothing: it is ended, and the app says why.

    Only the runtime's own ``quit`` (as ``wall-in-one ctl quit``): it exits cleanly, so
    systemd leaves the unit stopped, and the next start runs ExecStartPre's guard. No unit is
    touched here.
    """
    profile, sent, reports = _turn_off(
        profiles,
        monkeypatch,
        service=service,
        fail_compile=True,
        refuse_reload=False,
        schemas=OLDER,
    )
    assert sent == ["stop", "status", "quit"]
    said = " ".join(reports) if service else profile.application.publication_problem
    assert "still plays Electric Eye" in said
    assert "It was stopped: it is older than this app" in said
    assert "systemctl --user start wall-in-one" in said
    # Nothing it can't load was written.
    assert _names_page()


def test_a_held_document_without_web_is_left_alone(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused reload over a document that plays no page: nothing is stopped or replaced."""
    profile = profiles()
    application = profile.application
    paths.runtime_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_config_path().write_text(
        'schema_version = 4\n[[playlists]]\nid = "p"\n', encoding="utf-8"
    )
    config.save(config.Settings(roots=application.settings.roots, allow_web_wallpapers=False))
    sent = _recorded(monkeypatch)
    assert application._web_off_guard() == ""
    assert sent == []


def test_the_guard_does_nothing_while_web_wallpapers_are_on(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = profiles()
    application = profile.application
    _steam_with_web()
    paths.runtime_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_config_path().write_text(DOCUMENT, encoding="utf-8")
    config.save(config.Settings(roots=application.settings.roots, allow_web_wallpapers=True))
    sent = _recorded(monkeypatch)
    assert application._web_off_guard() == ""
    assert sent == []


def test_a_service_that_newly_reads_schema_8_gets_the_failed_publication_again(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After an older service was told to quit, the updated one is published to once."""
    application = profiles().application
    publications: list[str] = []
    monkeypatch.setattr(application, "_publish_runtime_async", lambda: publications.append("p"))
    older = {"status_version": 2, "supported_config_schemas": OLDER}
    newer = {"status_version": 2, "supported_config_schemas": READS_EIGHT}
    application._publish_again_when_presets_become_readable(older)
    application._set_publication_problem("the running wallpaper service is older")
    application._publish_again_when_presets_become_readable(older)
    assert publications == []
    application._publish_again_when_presets_become_readable(newer)
    assert publications == ["p"]
    application._publish_again_when_presets_become_readable(newer)
    assert publications == ["p"], "once, not on every poll"
    # Nothing failed and web is off: a service newly reading 8 asks for nothing.
    application._set_publication_problem("")
    application._publish_again_when_presets_become_readable(older)
    application._publish_again_when_presets_become_readable(newer)
    assert publications == ["p"]
