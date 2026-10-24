"""What the web-off guard holds the service to: the texts it may still hold.

Review r13, N2. `Application._web_off_guard` judges the file on disk and the texts publications
replaced since the service last confirmed a reload. A reload's confirmation used to forget every
one of them, also one replaced while that reload was on the wire, which may be the very one the
service loaded. The service is faked; no real service, systemctl or bus is touched.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_ui_codex030_round1b import profiles  # noqa: E402, F401
from tests.test_ui_web_off_failed_publish import DOCUMENT, _steam_with_web  # noqa: E402
from wall_in_one import config, paths  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.ui import app as app_module  # noqa: E402

OLDER = [4, 5]
GENERATION = "c" * 64
#: A newer document that plays no page, with the generation a status can name.
QUIET = f'schema_version = 4\nconfig_generation = "{GENERATION}"\n[[playlists]]\nid = "p"\n'


def _held(application: Any, text: str) -> None:
    """``text`` on disk, with web wallpapers off."""
    paths.runtime_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_config_path().write_text(text, encoding="utf-8")
    config.save(config.Settings(roots=application.settings.roots, allow_web_wallpapers=False))


def _replaced(application: Any, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """A publication replaced ``text``, and no reload of it was confirmed (yet)."""
    monkeypatch.setattr(app_module, "_runtime_installed", lambda *_a: (True, (), text))
    application._installed_leaving_out(application.settings, application.session)


def _service(monkeypatch: pytest.MonkeyPatch, *, schemas: list[int]) -> list[str]:
    """A faked service whose status lists ``schemas``; what it was sent."""
    sent: list[str] = []

    def send(verb: str, *_a: object, **_k: object) -> Response:
        sent.append(verb)
        if verb == "status":
            return Response.success(
                json.dumps({"status_version": 2, "supported_config_schemas": schemas})
            )
        return Response.success()

    monkeypatch.setattr(client, "send_runtime", send)
    return sent


def _acted(sent: list[str]) -> list[str]:
    """What was sent besides ``status``, which is only read."""
    return [verb for verb in sent if verb != "status"]


def test_a_text_replaced_while_a_reload_was_on_the_wire_is_still_judged(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N2. The service may have read the page's document just before it was replaced.

    Before: that reload's confirmation forgot the page's document too, and the next refused
    reload left it playing.
    """
    application = profiles().application
    _steam_with_web()
    _held(application, QUIET)

    def reload(verb: str, *_a: object, **_k: object) -> Response:
        # A publication lands while the reload is on the wire: it replaced the page's document.
        _replaced(application, monkeypatch, DOCUMENT)
        return Response.success()

    monkeypatch.setattr(application, "_send_gui_runtime", reload)
    with application._runtime_config_lock:
        application._runtime_document_generation += 1
    assert application._reload_runtime_if_needed().ok
    sent = _service(monkeypatch, schemas=OLDER)
    said = application._web_off_guard()
    assert _acted(sent) == ["stop", "quit"], said
    assert "still plays Electric Eye" in said


def test_control_a_text_replaced_before_the_reload_was_sent_is_forgotten(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = profiles().application
    _steam_with_web()
    _held(application, QUIET)
    _replaced(application, monkeypatch, DOCUMENT)
    monkeypatch.setattr(application, "_send_gui_runtime", lambda *_a, **_k: Response.success())
    with application._runtime_config_lock:
        application._runtime_document_generation += 1
    assert application._reload_runtime_if_needed().ok
    sent = _service(monkeypatch, schemas=OLDER)
    assert application._web_off_guard() == ""
    assert _acted(sent) == []
