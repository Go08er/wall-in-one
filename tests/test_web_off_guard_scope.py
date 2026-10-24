"""The web-off guard ends only what lets a page play, and judges what the service holds.

Review r13 (L2, L4, L5, N2, N3), its probes made tests. `Application._web_off_guard` runs after
a publication that failed or a reload the service refused while "Allow web wallpapers" is off.
It used to stop every display, and say "web wallpapers are off", whenever a document the app
remembered named an id the scan couldn't list, or more than four publications went
unconfirmed, though no web wallpaper was anywhere; and it ignored a document that allows
web wallpapers, which turns the service's own check off.

What lets a page play now:

* a held document that allows web wallpapers (``allow_web_wallpapers = true``), whatever it
  names, under any service;
* under a service not confirmed to read schema 8 (no check of its own), a held document that
  names a web page, or an id the scan can't classify, or one the app no longer knows.

The service's status says which document it holds (``config_generation``), and, from the
release that adds it, whether that one allows web wallpapers (``allow_web_wallpapers``), which
is believed over the app's memory. Both shapes are faked here; no real service, systemctl or
bus is touched.
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

from tests.test_ui_codex030_round1b import profiles  # noqa: E402, F401
from tests.test_ui_web_off_failed_publish import DOCUMENT, _steam_with_web  # noqa: E402
from wall_in_one import config, paths  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.ui import app as app_module  # noqa: E402

READS_EIGHT = [4, 5, 6, 7, 8]
OLDER = [4, 5]
SCENE = "1111111111"
#: The web page's document as a service that reads 8 held it while web wallpapers were on.
ALLOWING = DOCUMENT.replace(
    "schema_version = 4\n", "schema_version = 8\n[settings]\nallow_web_wallpapers = true\n", 1
)
GENERATION = "c" * 64
#: A newer document that plays no page, with the generation a status can name.
QUIET = f'schema_version = 4\nconfig_generation = "{GENERATION}"\n[[playlists]]\nid = "p"\n'


def _named(scene: str, *, allowing: bool = False) -> str:
    """A document that plays ``scene``; at schema 8 and allowing web wallpapers if asked."""
    head = (
        "schema_version = 8\n[settings]\nallow_web_wallpapers = true\n"
        if allowing
        else "schema_version = 4\n"
    )
    return f'{head}[[playlists]]\nid = "p"\n[[playlists.entries]]\nscene_id = "{scene}"\n'


def _scene_installed() -> None:
    item = Path(os.environ["HOME"]) / ".local/share/Steam/steamapps/workshop/content/431960" / SCENE
    item.mkdir(parents=True)
    (item / "project.json").write_text(
        json.dumps({"type": "scene", "title": "Toothless", "file": "scene.json"})
    )
    (item / "scene.json").write_text("{}")


def _held(application: Any, text: str) -> None:
    """``text`` on disk, with web wallpapers off."""
    paths.runtime_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_config_path().write_text(text, encoding="utf-8")
    config.save(config.Settings(roots=application.settings.roots, allow_web_wallpapers=False))


def _replaced(application: Any, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """A publication replaced ``text``, and no reload of it was confirmed (yet)."""
    monkeypatch.setattr(app_module, "_runtime_installed", lambda *_a: (True, (), text))
    application._installed_leaving_out(application.settings, application.session)


def _service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    schemas: list[int] | None,
    allows: bool | None = None,
    generation: str = "",
) -> list[str]:
    """A faked service; what it was sent.

    ``schemas``: what its status lists (None: a status it refuses to give). ``allows``: its
    ``allow_web_wallpapers`` (None: a service from before that field). ``generation``: the
    ``config_generation`` it reports.
    """
    sent: list[str] = []

    def send(verb: str, *_a: object, **_k: object) -> Response:
        sent.append(verb)
        if verb == "status":
            if schemas is None:
                return Response.failure("timed out")
            reply: dict[str, object] = {
                "status_version": 2,
                "supported_config_schemas": schemas,
                "playback_state": "stopped",
            }
            if allows is not None:
                reply["allow_web_wallpapers"] = allows
            if generation:
                reply["config_generation"] = generation
            return Response.success(json.dumps(reply))
        return Response.success()

    monkeypatch.setattr(client, "send_runtime", send)
    return sent


def _acted(sent: list[str]) -> list[str]:
    """What was sent besides ``status``, which is only read."""
    return [verb for verb in sent if verb != "status"]


def _plays_nothing() -> bool:
    text = paths.runtime_config_path().read_text(encoding="utf-8")
    return "entries = []" in text and "allow_web_wallpapers" not in text


# -- the review's probe: no web wallpaper anywhere (L2) ---------------------------------------


@pytest.mark.parametrize("allows", [None, False], ids=("no field", "field false"))
def test_a_scene_that_was_unsubscribed_doesnt_blank_every_display(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    allows: bool | None,
) -> None:
    """Before: stop, reload, play, the document that plays nothing installed, and "web
    wallpapers are off" said to someone who never had one."""
    application = profiles().application
    held = _named(SCENE)  # its Workshop folder is gone: the scan can't list it
    _held(application, held)
    sent = _service(monkeypatch, schemas=READS_EIGHT, allows=allows)
    said = application._web_off_guard()
    assert _acted(sent) == [], said
    assert paths.runtime_config_path().read_text(encoding="utf-8") == held
    assert said == ""


@pytest.mark.parametrize("allows", [None, False], ids=("no field", "field false"))
def test_unconfirmed_publications_alone_dont_blank_every_display(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    allows: bool | None,
) -> None:
    """Before: the same, for five installs before a confirmed reload."""
    application = profiles().application
    _scene_installed()
    _held(application, _named(SCENE))
    application._held_candidates_unknown = True
    sent = _service(monkeypatch, schemas=READS_EIGHT, allows=allows)
    said = application._web_off_guard()
    assert _acted(sent) == [], said
    assert said == ""


def test_control_an_installed_scene_is_left_alone(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = profiles().application
    _scene_installed()
    _held(application, _named(SCENE))
    sent = _service(monkeypatch, schemas=READS_EIGHT)
    assert application._web_off_guard() == ""
    assert _acted(sent) == []


@pytest.mark.parametrize("cause", ["unchecked", "unknown"])
def test_control_a_service_without_its_own_check_still_fails_closed(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    cause: str,
) -> None:
    """One that predates schema 8 plays whatever it holds: what can't be ruled out ends it."""
    application = profiles().application
    if cause == "unknown":
        _scene_installed()
        application._held_candidates_unknown = True
    _held(application, _named(SCENE))
    sent = _service(monkeypatch, schemas=OLDER)
    said = application._web_off_guard()
    assert _acted(sent) == ["stop", "quit"], said
    assert "It was stopped: it is older than this app" in said


# -- a document that allows web wallpapers (L4) -----------------------------------------------


def test_a_held_document_that_allows_web_wallpapers_is_ended(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before: nothing. It names an installed scene, but allows web wallpapers, which turns
    the service's own check off: a Workshop update that made it a web page would play."""
    application = profiles().application
    _scene_installed()
    _held(application, _named(SCENE, allowing=True))
    sent = _service(monkeypatch, schemas=READS_EIGHT)  # a service from before the field
    said = application._web_off_guard()
    assert _acted(sent) == ["stop", "reload"], said
    assert _plays_nothing()
    assert "still holds a configuration that allows web wallpapers" in said
    # What the display shows then (L3): no wallpaper plays, Noctalia's last still stays.
    assert "the last still Noctalia was given stays on screen" in said


def test_the_service_saying_it_allows_web_wallpapers_is_believed(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its document may be one this app never saw replaced; its word is taken over the texts.

    Before: nothing, for texts that name only an installed scene.
    """
    application = profiles().application
    _scene_installed()
    _held(application, _named(SCENE))
    sent = _service(monkeypatch, schemas=READS_EIGHT, allows=True)
    said = application._web_off_guard()
    assert _acted(sent) == ["stop", "reload"], said
    assert _plays_nothing()
    assert "still holds a configuration that allows web wallpapers" in said


def test_the_service_saying_its_document_doesnt_allow_web_is_believed(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app remembers replacing a page's document; the service says what it holds now.

    Before: stopped, and given nothing to play over a newer document.
    """
    application = profiles().application
    _steam_with_web()
    _held(application, ALLOWING)
    _replaced(application, monkeypatch, ALLOWING)
    _held(application, QUIET)
    sent = _service(monkeypatch, schemas=READS_EIGHT, allows=False)
    said = application._web_off_guard()
    assert _acted(sent) == [], said
    assert said == ""
    assert paths.runtime_config_path().read_text(encoding="utf-8") == QUIET


@pytest.mark.parametrize("allows", [None, False], ids=("no field", "field false"))
def test_a_service_that_reads_8_refuses_a_page_its_document_doesnt_allow(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    allows: bool | None,
) -> None:
    """It reads the item's type before it starts the engine (P2): nothing is stopped, and the
    app says the setting hasn't reached it yet. Before: stopped, and given nothing to play."""
    application = profiles().application
    _steam_with_web()
    held = DOCUMENT.replace(
        "schema_version = 4\n", f'schema_version = 4\nconfig_generation = "{"e" * 64}"\n', 1
    )
    _held(application, held)
    sent = _service(monkeypatch, schemas=READS_EIGHT, allows=allows, generation="e" * 64)
    said = application._web_off_guard()
    assert _acted(sent) == [], said
    assert paths.runtime_config_path().read_text(encoding="utf-8") == held
    assert "hasn\u2019t reached the wallpaper service" in said
    assert "Electric Eye" in said and "nothing was stopped" in said


# -- what the service holds, by its status (Q5) and the app's memory (N2) ---------------------


@pytest.mark.parametrize(
    ("schemas", "remembered"),
    [(OLDER, DOCUMENT), (READS_EIGHT, ALLOWING)],
    ids=("older service", "reads 8"),
)
def test_a_newer_document_the_service_holds_is_judged_not_the_apps_memory(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    schemas: list[int],
    remembered: str,
) -> None:
    """Another writer's reload (the health sync, the service's own poll) got the newer one in.

    Before: the remembered page's document ended the service over a document that holds no
    page.
    """
    application = profiles().application
    _steam_with_web()
    _held(application, remembered)
    _replaced(application, monkeypatch, remembered)
    _held(application, QUIET)
    sent = _service(monkeypatch, schemas=schemas, generation=GENERATION)
    said = application._web_off_guard()
    assert _acted(sent) == [], said
    assert paths.runtime_config_path().read_text(encoding="utf-8") == QUIET


def test_control_the_remembered_document_is_judged_when_the_service_holds_it(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = profiles().application
    _steam_with_web()
    remembered = DOCUMENT.replace(
        "schema_version = 4\n", f'schema_version = 4\nconfig_generation = "{"d" * 64}"\n', 1
    )
    _held(application, remembered)
    _replaced(application, monkeypatch, remembered)
    _held(application, QUIET)
    sent = _service(monkeypatch, schemas=OLDER, generation="d" * 64)
    said = application._web_off_guard()
    assert _acted(sent) == ["stop", "quit"], said
    assert "still plays Electric Eye" in said


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


# -- what is said when the service is told to quit (N3) ---------------------------------------


def test_a_service_whose_status_cant_be_read_isnt_called_older(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before: "it is older than this app", for a current service whose status timed out."""
    application = profiles().application
    _steam_with_web()
    _held(application, DOCUMENT)
    sent = _service(monkeypatch, schemas=None)
    said = application._web_off_guard()
    assert _acted(sent) == ["stop", "quit"], said
    assert "its status couldn\u2019t be read" in said
    assert "older than this app" not in said
    # Nor does its next start play nothing: it plays no web wallpaper (L3).
    assert "plays nothing while" not in said
