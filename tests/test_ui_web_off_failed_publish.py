"""Turning "Allow web wallpapers" off, then a publication that fails, leaves no page playing.

The setting is saved before its publication, and the service keeps the last
document it loaded when none lands. When that document still plays a web
page, the application stops playback and says so, instead of leaving the page
on screen under a setting that says it is never played. (Refusing to save the
setting until its publication succeeds was the alternative: the publication
is asynchronous, behind the library scan and the compile lane, so the save
cannot wait for it without holding the settings lane through a socket call.)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_codex030_round1b import Profile, profiles  # noqa: E402, F401
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402

WEB = "2777242023"


def _steam_with_web() -> None:
    item = Path(os.environ["HOME"]) / ".local/share/Steam/steamapps/workshop/content/431960" / WEB
    item.mkdir(parents=True)
    (item / "project.json").write_text(
        json.dumps({"type": "web", "title": "Electric Eye", "file": "index.html"})
    )
    (item / "index.html").write_text("<p>x</p>")


def _off_with_a_failing_publication(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    *,
    document: str,
) -> tuple[Profile, list[str]]:
    profile: Profile = profiles()
    application = profile.application
    _steam_with_web()
    config.save(config.Settings(roots=application.settings.roots, allow_web_wallpapers=True))
    application._settings = config.load_strict()
    paths.runtime_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_config_path().write_text(document, encoding="utf-8")
    sent: list[str] = []

    def send(verb: str, *_a: object, **_k: object) -> Response:
        sent.append(verb)
        return Response.success()

    monkeypatch.setattr(client, "send_runtime", send)

    # The compile itself fails (a library folder that can't be read).
    def failing(_request: object) -> bool:
        raise runtime_config.RuntimeConfigError("a library folder can't be read")

    monkeypatch.setattr(application, "_compile_runtime_request", failing)
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
    return profile, sent


DOCUMENT = (
    f'schema_version = 4\n[[playlists]]\nid = "p"\n[[playlists.entries]]\nscene_id = "{WEB}"\n'
)


def test_a_failed_publication_stops_a_web_page_that_is_off(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before: the page kept playing and the failure said only that the compile failed."""
    profile, sent = _off_with_a_failing_publication(profiles, monkeypatch, document=DOCUMENT)
    assert config.load_strict().allow_web_wallpapers is False
    assert "stop" in sent
    problem = profile.application.publication_problem
    assert "can't be read" in problem
    assert "Electric Eye" in problem and "stopped" in problem


def test_a_failed_publication_over_a_document_without_web_stops_nothing(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, sent = _off_with_a_failing_publication(
        profiles, monkeypatch, document='schema_version = 4\n[[playlists]]\nid = "p"\n'
    )
    assert "stop" not in sent
    assert "stopped" not in profile.application.publication_problem
