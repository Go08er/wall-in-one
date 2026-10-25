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
import threading
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_ui_codex030_round1b import profiles  # noqa: E402, F401
from tests.test_ui_web_off_failed_publish import DOCUMENT, _steam_with_web  # noqa: E402
from tests.test_web_off_nothing_to_play import _library  # noqa: E402
from tests.test_web_off_nothing_to_play import _service as _units  # noqa: E402
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.session import Session  # noqa: E402
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


# -- the file on disk the service hasn't loaded yet (review r13b, D1) -------------------------
#
# Its own poll of the file loads it within about a second, and nothing runs the guard again.
# The way in: a writer that sends no reload (a hand-run --write-config while web was on), then
# web turned off and that publication failing before it writes.


def _with_generation(text: str, generation: str) -> str:
    head = text.split("\n", 1)[0] + "\n"
    return text.replace(head, f'{head}config_generation = "{generation}"\n', 1)


def _unloaded_on_disk(application: Any, monkeypatch: pytest.MonkeyPatch, disk: str) -> None:
    """The service holds QUIET (a replaced text, its reload unconfirmed); ``disk`` is on disk."""
    _held(application, QUIET)
    _replaced(application, monkeypatch, QUIET)
    _held(application, disk)


@pytest.mark.parametrize("holds", ["a replaced text", "a document the app never saw"])
def test_an_unloaded_document_on_disk_that_allows_web_is_ended(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    holds: str,
) -> None:
    """Before: only its status was asked (``said ''``), and the poll loaded the page's document.

    "a document the app never saw": a status whose ``config_generation`` matches no text, with
    ``allow_web_wallpapers: false``, which judged no text at all.
    """
    application = profiles().application
    _scene_installed()
    disk = _with_generation(_named(SCENE, allowing=True), "a" * 64)
    _unloaded_on_disk(application, monkeypatch, disk)
    generation = GENERATION if holds == "a replaced text" else "b" * 64
    sent = _service(monkeypatch, schemas=READS_EIGHT, allows=False, generation=generation)
    said = application._web_off_guard()
    assert _acted(sent) == ["stop", "reload"], said
    assert _plays_nothing()
    assert said.startswith(
        "Allow web wallpapers is off, but the configuration on disk, which the wallpaper "
        "service loads the next time it checks the file, allows web wallpapers. "
    ), said
    assert "the last still Noctalia was given stays on screen" in said


@pytest.mark.parametrize("names", ["a web page", "an id the scan can't classify"])
def test_an_unloaded_document_a_service_without_the_check_would_play_ends_it(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    names: str,
) -> None:
    """One that predates schema 8 plays what it loads. Before: only QUIET, which it holds, was
    judged, and the page's document was left for its poll."""
    application = profiles().application
    if names == "a web page":
        _steam_with_web()
        disk = _with_generation(DOCUMENT, "a" * 64)
    else:
        disk = _with_generation(_named(SCENE), "a" * 64)  # its folder isn't there
    _unloaded_on_disk(application, monkeypatch, disk)
    sent = _service(monkeypatch, schemas=OLDER, generation=GENERATION)
    said = application._web_off_guard()
    assert _acted(sent) == ["stop", "quit"], said
    assert "but the configuration on disk" in said
    assert ("Electric Eye" if names == "a web page" else SCENE) in said
    assert "The wallpaper service was stopped: it is older than this app" in said


def test_control_an_unloaded_document_on_disk_without_the_key_is_left_alone(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service that reads 8 refuses every page itself: the narrowing (L2) holds for the file
    on disk too, a web page it names included."""
    application = profiles().application
    _steam_with_web()
    disk = _with_generation(DOCUMENT, "a" * 64)
    _unloaded_on_disk(application, monkeypatch, disk)
    sent = _service(monkeypatch, schemas=READS_EIGHT, allows=False, generation=GENERATION)
    said = application._web_off_guard()
    assert _acted(sent) == [], said
    assert paths.runtime_config_path().read_text(encoding="utf-8") == disk


def test_control_an_unloaded_document_naming_an_installed_scene_is_left_alone(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under a service without the check too, when what it names is a scene."""
    application = profiles().application
    _scene_installed()
    disk = _with_generation(_named(SCENE), "a" * 64)
    _unloaded_on_disk(application, monkeypatch, disk)
    sent = _service(monkeypatch, schemas=OLDER, generation=GENERATION)
    assert application._web_off_guard() == ""
    assert _acted(sent) == []
    assert paths.runtime_config_path().read_text(encoding="utf-8") == disk


# -- --service's lane: a reload that fails, not only one that is refused ---------------------


def test_the_service_lane_judges_what_is_held_after_a_reload_that_times_out(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GUI's lane counts a reload that failed as a failed publication; ``--service``'s now
    does too. Before: "Runtime reload failed" said, and a page it may still hold left playing."""
    application = profiles(service=True).application
    _steam_with_web()
    _held(application, DOCUMENT)
    monkeypatch.setattr(application, "_update_runtime_document", lambda: (True, True))
    sent: list[str] = []

    def send(verb: str, *_a: object, **_k: object) -> Response:
        sent.append(verb)
        if verb == "reload":
            raise client.ControlError("timed out waiting for the wallpaper service")
        if verb == "status":
            return Response.success(
                json.dumps({"status_version": 2, "supported_config_schemas": OLDER})
            )
        return Response.success()

    monkeypatch.setattr(client, "send_runtime", send)
    reports: list[str] = []
    monkeypatch.setattr(application, "window_report", reports.append)
    assert application._publish_runtime() is False
    assert _acted(sent) == ["reload", "stop", "quit"], reports
    assert reports[-1].startswith("Runtime reload failed: timed out")
    assert "still plays Electric Eye" in reports[-1]


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
    # What it holds is only what the app remembers then.
    assert "still may hold a configuration that plays Electric Eye" in said
    # Nor does its next start play nothing: it plays no web wallpaper (L3).
    assert "plays nothing while" not in said


# -- allow_web_wallpapers left out for a service not confirmed for 8 (L5) ---------------------


def test_web_wallpapers_left_out_are_published_again_once_the_service_is_confirmed(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before: the key was left out without a word, and a service already listing 8 was never
    asked again: verifying its units brought it back only with the next edit.

    As the dials: asked again once while it waits, and once more when the service is started
    again (as the notice asks).
    """
    application = profiles().application
    root = _library(Path(os.environ["HOME"]), still_beside=True)
    settings = config.Settings(roots=(root,), allow_web_wallpapers=True)
    config.save(settings)
    application._settings = config.load_strict()
    publications: list[str] = []
    monkeypatch.setattr(application, "_publish_runtime_async", lambda: publications.append("p"))
    status = {
        "status_version": 2,
        "supported_config_schemas": READS_EIGHT,
        "runtime_instance": "a" * 32,
    }
    # The first status: 8 read, web wallpapers on, so the document is published for it.
    application._publish_again_when_presets_become_readable(status)
    assert publications == ["p"]
    # That publication, with units that can't be verified: the key is left out, and said.
    _units(monkeypatch, schemas=READS_EIGHT, units="unverified")
    session = Session(settings)
    try:
        session.refresh()
        application._installed_leaving_out(settings, session)
    finally:
        session.shutdown()
    application._note_left_out()
    assert "allow_web_wallpapers" not in paths.runtime_config_path().read_text(encoding="utf-8")
    assert application.left_out_notice.startswith(
        "Web wallpapers aren\u2019t played until the wallpaper service is confirmed to read them"
    )
    application._publish_again_when_presets_become_readable(status)
    assert publications == ["p", "p"], "asked again while it waits"
    application._publish_again_when_presets_become_readable(status)
    assert publications == ["p", "p"], "once, not on every poll"
    application._publish_again_when_presets_become_readable(
        {**status, "runtime_instance": "b" * 32}
    )
    assert publications == ["p", "p", "p"], "and once more when it is started again"


# -- no service running: what the next start loads (review r13c, D2) -------------------------
#
# Its start check (ExecStartPre, or the companion's own start) may already be past, made while
# web wallpapers were on, and the guard's status read lands between that check and the service
# binding its socket. Before, "not running" was taken as nothing to do: the service then started
# on a document that allows web wallpapers.


def _not_running(monkeypatch: pytest.MonkeyPatch, *, after_status: bool = False) -> list[str]:
    """No service answers (``after_status``: one that answers its status, then is gone)."""
    sent: list[str] = []

    def send(verb: str, *_a: object, **_k: object) -> Response:
        sent.append(verb)
        if after_status and verb == "status":
            return Response.success(
                json.dumps(
                    {
                        "status_version": 2,
                        "supported_config_schemas": READS_EIGHT,
                        "allow_web_wallpapers": True,
                        "playback_state": "playing",
                    }
                )
            )
        raise client.NotRunningError("no wallpaper service in this test")

    monkeypatch.setattr(client, "send_runtime", send)
    return sent


@pytest.mark.parametrize("gone", ["before-the-status", "before-the-stop"])
def test_with_no_service_running_a_document_that_allows_web_wallpapers_is_replaced(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    gone: str,
) -> None:
    """Before: ``''``, and the document on disk still allowed web wallpapers."""
    application = profiles().application
    _scene_installed()
    _held(application, _named(SCENE, allowing=True))
    sent = _not_running(monkeypatch, after_status=gone == "before-the-stop")
    said = application._web_off_guard()
    assert sent == (["status"] if gone == "before-the-status" else ["status", "stop"]), said
    assert _plays_nothing(), paths.runtime_config_path().read_text(encoding="utf-8")
    assert said.startswith(
        "Allow web wallpapers is off, but the configuration on disk, which the wallpaper "
        "service loads when it starts, allows web wallpapers. The service isn\u2019t running"
    ), said


@pytest.mark.parametrize("held", ["names-a-scene", "names-a-web-page"])
def test_control_with_no_service_running_a_document_without_the_key_is_left(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    held: str,
) -> None:
    """Its next start compiles it again, or refuses it with exit 78 when it plays a page
    (``--service-startup-prepare``), and a service that reads 8 refuses the page itself."""
    application = profiles().application
    if held == "names-a-web-page":
        _steam_with_web()
        text = DOCUMENT
    else:
        _scene_installed()
        text = _named(SCENE)
    _held(application, text)
    sent = _not_running(monkeypatch)
    assert application._web_off_guard() == ""
    assert sent == ["status"]
    assert paths.runtime_config_path().read_text(encoding="utf-8") == text


# -- review r13f, N1: a document the service reads as TOML 1.1 ---------------------------------


@pytest.mark.parametrize(
    "name", ["x-escape-key", "inline-table-over-lines", "control-off", "control-no-key"]
)
def test_a_document_the_service_reads_as_allowing_web_wallpapers_is_ended(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """The guard's judgement of a text, when the service's status doesn't say (a service from
    before the field). Before: ``[x-escape-key]`` and ``[inline-table-over-lines]``, which the
    service loads as allowing web wallpapers (`tests.test_web_off_toml_1_1`), were judged not
    to, and it went on holding them. The controls are TOML 1.0 and allow none: left alone."""
    from tests.test_web_off_toml_1_1 import document

    application = profiles().application
    text, _service_allows, _toml_1_0 = document(name)
    _held(application, text)
    sent = _service(monkeypatch, schemas=READS_EIGHT)
    said = application._web_off_guard()
    if name.startswith("control"):
        assert _acted(sent) == [], said
        assert said == ""
        assert paths.runtime_config_path().read_text(encoding="utf-8") == text
        return
    assert _acted(sent) == ["stop", "reload"], said
    assert _plays_nothing()
    assert "still holds a configuration that allows web wallpapers" in said


@pytest.mark.parametrize("name", ["x-escape-key", "control-no-key"])
def test_with_no_service_running_a_toml_1_1_document_that_allows_web_is_replaced(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """`_web_off_not_running`: the file is what the next service loads. Before: kept."""
    from tests.test_web_off_toml_1_1 import document

    application = profiles().application
    text, _service_allows, _toml_1_0 = document(name)
    _held(application, text)
    sent = _not_running(monkeypatch)
    said = application._web_off_guard()
    assert sent == ["status"], said
    if name.startswith("control"):
        assert said == ""
        assert paths.runtime_config_path().read_text(encoding="utf-8") == text
        return
    assert _plays_nothing(), paths.runtime_config_path().read_text(encoding="utf-8")
    assert "allows web wallpapers. The service isn\u2019t running" in said, said


# -- review r13f, N3's class: the setting is read again under the lock -------------------------


@pytest.mark.parametrize("variant", ["turned-on-meanwhile", "control-left-off"])
def test_what_another_writer_installs_while_the_guard_waits_for_the_lock_is_judged_as_it_is(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    """The guard reads the setting, off, then waits for the compiler lock, which another writer
    holds. That writer sees web wallpapers turned on again: it saves that, and installs a
    document compiled for them on, before it lets go. Before: the setting read before the wait
    stood, and the service was stopped and given nothing to play while web wallpapers were on.
    Now it is read again under the lock, and nothing is done. The control: the writer leaves them
    off, and the document it installs, which allows them, is ended."""
    application = profiles().application
    _scene_installed()
    _held(application, QUIET)
    allowing = _named(SCENE, allowing=True)
    sent = _service(monkeypatch, schemas=READS_EIGHT)
    holding = threading.Event()
    waiting = threading.Event()
    failures: list[str] = []
    lock = runtime_config.compiler_lock

    def watched(*arguments: Any, **keywords: Any) -> Any:
        if threading.current_thread() is threading.main_thread():
            waiting.set()
        return lock(*arguments, **keywords)

    def writer() -> None:
        with lock():
            holding.set()
            if not waiting.wait(60.0):
                failures.append("the guard never waited for the compiler lock")
                return
            if variant == "turned-on-meanwhile":
                config.update({"allow_web_wallpapers": True})
            paths.runtime_config_path().write_text(allowing, encoding="utf-8")

    monkeypatch.setattr(runtime_config, "compiler_lock", watched)
    thread = threading.Thread(target=writer, name="test-writer")
    thread.start()
    try:
        assert holding.wait(60.0), "the writer never took the compiler lock"
        said = application._web_off_guard()
    finally:
        waiting.set()
        thread.join(60.0)
    assert not failures, failures
    if variant == "turned-on-meanwhile":
        assert config.load_strict().allow_web_wallpapers is True
        assert _acted(sent) == [], said
        assert said == ""
        assert paths.runtime_config_path().read_text(encoding="utf-8") == allowing
        return
    assert _acted(sent) == ["stop", "reload"], said
    assert _plays_nothing()
