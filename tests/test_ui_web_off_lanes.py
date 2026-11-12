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
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.previews_param import CURRENT, _previews_param, web_on  # noqa: E402, F401
from tests.test_ui_codex030_round1b import profiles  # noqa: E402, F401
from tests.test_ui_web_off_failed_publish import DOCUMENT, WEB, _steam_with_web  # noqa: E402
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.ui.app import _RuntimePublication  # noqa: E402

READS_EIGHT = [4, 5, 6, 7, 8]
OLDER = [4, 5]
#: What a service that reads schema 8 held while web wallpapers were on: the page, and the
#: key that allows it. Without the key that service refuses the page itself when it would
#: start it, and nothing is ended (`test_web_off_guard_scope.py`); the schema-4 `DOCUMENT`
#: is what an older one holds.
ALLOWING = DOCUMENT.replace(
    "schema_version = 4\n", "schema_version = 8\n[settings]\nallow_web_wallpapers = true\n", 1
)


def _turn_off(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    *,
    service: bool,
    fail_compile: bool,
    refuse_reload: bool,
    schemas: list[int] | None = None,
    playback: str = "stopped",
) -> tuple[Any, list[str], list[str]]:
    """Turn the setting off; what was sent to the service, and what the app said.

    ``refuse_reload``: the service refuses every document but one that plays
    nothing. ``schemas``: what its status lists (None: a reply that isn't one);
    it holds `ALLOWING` when that lists 8, `DOCUMENT` otherwise.
    """
    profile = profiles(service=service)
    application = profile.application
    _steam_with_web()
    config.save(web_on(application.settings.roots))
    application._settings = config.load_strict()
    application._settings_requested = application._settings
    paths.runtime_config_path().parent.mkdir(parents=True, exist_ok=True)
    held = ALLOWING if schemas == READS_EIGHT else DOCUMENT
    paths.runtime_config_path().write_text(held, encoding="utf-8")
    sent: list[str] = []
    reports: list[str] = []

    def send(verb: str, *_a: object, **_k: object) -> Response:
        if "quit" in sent:
            # A service told to quit is gone: whatever asks next finds none (``--service``
            # publishes web off before its rescan and again after it, F4-2).
            raise client.NotRunningError("the service quit")
        sent.append(verb)
        if verb == "status" and schemas is not None:
            return Response.success(
                json.dumps(
                    {
                        "status_version": 2,
                        "supported_config_schemas": schemas,
                        "playback_state": playback,
                    }
                )
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


def _acted(sent: list[str]) -> list[str]:
    """What was sent besides ``status``, which the profile's own poll may also send.

    And the guard read the service's status before it stopped anything.
    """
    if "stop" in sent:
        assert "status" in sent[: sent.index("stop")], sent
    return [verb for verb in sent if verb != "status"]


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
    assert _acted(sent) == ["stop", "reload"]
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
    assert _acted(sent)[-1] == "reload"
    # With previews on the save also moves the library sources, so a rescan follows and
    # publishes the real document (without the page: web is off) over the guard's
    # plays-nothing one; this service refuses every document but that one, which is
    # its own doing. Either way no page is named and none is allowed.
    assert _plays_nothing() or CURRENT[0]
    assert not _names_page() or _plays_nothing()
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())
    problem = profile.application.publication_problem
    assert problem.startswith("Runtime rejected the new configuration: refused")
    if not CURRENT[0]:  # the rescan's own refusal replaces the guard's wording otherwise
        # The held document carries the key, so what it plays is hedged as the allow is
        # (r13k S-N12).
        assert "still may play Electric Eye" in problem
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

    Why stands until a publication gets through. With previews on, the save also starts a
    rescan in the GUI, whose publication fails to compile too, with nothing to say of the
    service (gone by then); it is the newest, and it may land after the one that stopped the
    service or replace that one before it was said (only a current publication is said). Either
    way the stop's words are said, and stay with the newest problem (R42-22).
    """
    profile, sent, reports = _turn_off(
        profiles,
        monkeypatch,
        service=service,
        fail_compile=True,
        refuse_reload=False,
        schemas=OLDER,
    )
    assert _acted(sent) == ["stop", "quit"]
    said = [" ".join(reports)]
    if not service:
        problem = profile.application.publication_problem
        assert "a library folder can't be read" in problem
        said.append(problem)
    for words in said:
        assert "still plays Electric Eye" in words
        assert "It was stopped: it is older than this app" in words
        assert "systemctl --user start wall-in-one" in words
    # Nothing it can't load was written.
    assert _names_page()


STOPPED = (
    "Allow web wallpapers is off, but the wallpaper service still plays Electric Eye. It was "
    "stopped: it is older than this app. Starting it again (systemctl --user start wall-in-one)"
)
FAILED = "a library folder can't be read"


def _lane(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, list[str], list[str], dict[str, str]]:
    """An application whose lane steps run here, on publications made for them: no service,
    no compile. What it said, what the new window was shown each time it was told, and what
    the guard says next."""
    application = profiles().application
    said: list[str] = []
    shown: list[str] = []
    monkeypatch.setattr(application, "window_report", said.append)
    monkeypatch.setattr(application, "_ensure_runtime_compile_queued", lambda: None)

    class Window:
        """Records what the header would show; every other call does nothing."""

        def publication_changed(self) -> None:
            shown.append(application.publication_problem)

        def __getattr__(self, _name: str) -> Callable[..., None]:
            return lambda *_arguments, **_keywords: None

    monkeypatch.setattr(application, "_window", Window())
    guard = {"says": STOPPED}
    monkeypatch.setattr(application, "_web_off_guard", lambda: guard["says"])
    return application, said, shown, guard


def _ran(application: Any, generation: int, error: str) -> _RuntimePublication:
    """The lane's tail (`_compile_runtime_until_current`): the guard, on a failure."""
    publication: _RuntimePublication = application._stopping_web_after_failure(
        _RuntimePublication(generation, error=error)
    )
    return publication


def _settled(application: Any, publication: _RuntimePublication, newest: int) -> None:
    """GTK's tail (`_finish_runtime_compile`), with ``newest`` the latest request."""
    application._runtime_authoring_request = SimpleNamespace(generation=newest)
    done: Future[_RuntimePublication] = Future()
    done.set_result(publication)
    application._finish_runtime_compile(done)


@pytest.mark.parametrize("standing", ["nothing-failed-yet", "already-failing"])
@pytest.mark.parametrize("first", ["replaced-before-it-was-said", "said"])
def test_what_the_guard_did_is_said_and_stays_whatever_publication_comes_next(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    first: str,
    standing: str,
) -> None:
    """R42-22: only a current publication is said, so the one that carried what the guard did
    (the older service stopped, and how to start it again) went unsaid when a later request had
    replaced it, and the later one's compile error was all that was said and shown (seen once in
    a larger run, on fd680432). What the guard did is kept apart: said with the next publication
    that is said, once, shown with the newest problem (the new window told so, though that
    problem may read as the one standing before), and gone when one doesn't fail."""
    application, said, shown, guard = _lane(profiles, monkeypatch)
    before: list[str] = []
    if standing == "already-failing":
        guard["says"] = ""
        _settled(application, _ran(application, 0, FAILED), 0)
        before = [FAILED]
        guard["says"] = STOPPED
        assert application.publication_problem == FAILED

    _settled(application, _ran(application, 1, FAILED), 2 if first.startswith("replaced") else 1)
    guard["says"] = ""  # the service is gone: nothing more to do or say
    _settled(application, _ran(application, 2, FAILED), 2)

    # Said once, with whichever was said first; shown with the newest problem.
    replaced = first.startswith("replaced")
    assert said == [*before, f"{FAILED}. {STOPPED}"] + ([] if replaced else [FAILED])
    assert application.publication_problem == f"{FAILED}. {STOPPED}"
    assert shown and shown[-1] == f"{FAILED}. {STOPPED}", shown
    reported = list(said)
    _settled(application, _RuntimePublication(3), 3)  # one doesn't fail
    assert application.publication_problem == ""
    assert shown[-1] == ""
    assert said == reported


def test_an_older_success_settling_after_what_the_guard_did_doesnt_take_it_away_unsaid(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R42-22: a lane job's guard can run before GTK settles the job before it. That one went
    through, which takes what the guard did away; but only once it was said: here it is said
    by that publication, and stays until a later one doesn't fail."""
    application, said, _shown, guard = _lane(profiles, monkeypatch)
    failed = _ran(application, 2, FAILED)  # the guard runs on the lane
    guard["says"] = ""
    _settled(application, _RuntimePublication(1), 1)  # the success before it settles after

    assert said == [STOPPED]
    assert application.publication_problem == STOPPED
    _settled(application, failed, 2)
    assert application.publication_problem == f"{FAILED}. {STOPPED}"
    _settled(application, _RuntimePublication(3), 3)
    assert application.publication_problem == ""


def test_a_newer_success_takes_what_the_guard_did_away_unsaid(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """f9 F1 (a): what the guard did for a publication goes with one as new that gets through,
    said or not, and is not said: it is over. Only an older success leaves it (above)."""
    application, said, shown, guard = _lane(profiles, monkeypatch)
    failed = _ran(application, 1, FAILED)
    guard["says"] = ""
    _settled(application, failed, 2)  # replaced before it was said
    _settled(application, _RuntimePublication(2), 2)

    assert said == []
    assert application.publication_problem == ""
    assert shown[-1:] == [""], shown


def test_a_service_started_again_is_published_to_while_what_the_guard_did_stands(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """f9 F1 (b): the guard quit an older service, and a later publication found none, which
    keeps the guard's words but clears the problem itself. A service started again, as the words
    ask, was published to only while that problem stood, so the banner kept telling the person
    to start it (on cab9225a). What is shown decides: it is published to, once."""
    application, _said, _shown, guard = _lane(profiles, monkeypatch)
    _settled(application, _ran(application, 1, FAILED), 1)  # said with the problem
    guard["says"] = ""
    _settled(application, _RuntimePublication(2, unavailable=True), 2)
    assert application._publication_problem == "" and STOPPED in application.publication_problem
    published: list[bool] = []

    def publish() -> bool:
        published.append(True)
        return True

    monkeypatch.setattr(application, "_publish_runtime_for_context", publish)
    application._nothing_readable_seen = False
    started = {"status_version": 2, "supported_config_schemas": READS_EIGHT}

    application._publish_again_when_presets_become_readable(started)

    assert published == [True]


def test_turning_web_wallpapers_back_on_ends_what_the_guard_did(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """f9 F1 (c): with the words standing and the compile still failing, web wallpapers going
    back on left the banner saying "Allow web wallpapers is off, but…" with the next failure
    (on cab9225a). They go with the save that turns them on; that save's own publication, which
    fails too, says only why."""
    application, said, _shown, guard = _lane(profiles, monkeypatch)
    _settled(application, _ran(application, 1, FAILED), 1)
    guard["says"] = ""
    previous = application.settings
    assert not previous.allow_web_wallpapers

    application._adopt_settings(previous, replace(previous, allow_web_wallpapers=True))
    _settled(application, _ran(application, 2, FAILED), 2)

    assert application.publication_problem == FAILED
    assert said[-1] == FAILED, said


def test_a_held_document_without_web_is_left_alone(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused reload over a document that plays no page: nothing is stopped or replaced.

    Its status is read (what the service holds may be a document this app never saw
    replaced, and the service says whether that allows web wallpapers), and nothing else.
    """
    profile = profiles()
    application = profile.application
    paths.runtime_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_config_path().write_text(
        'schema_version = 4\n[[playlists]]\nid = "p"\n', encoding="utf-8"
    )
    config.save(config.Settings(roots=application.settings.roots, allow_web_wallpapers=False))
    sent = _recorded(monkeypatch)
    assert application._web_off_guard() == ""
    assert sent == ["status"]


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


def test_a_busy_compiler_lock_still_stops_the_page(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another writer holding the lock past its timeout: stop now, write nothing, quit nothing."""
    application = profiles().application
    _steam_with_web()
    paths.runtime_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_config_path().write_text(DOCUMENT, encoding="utf-8")
    config.save(config.Settings(roots=application.settings.roots, allow_web_wallpapers=False))
    sent = _recorded(monkeypatch)

    def busy(*_a: object, **_k: object) -> object:
        raise runtime_config.RuntimeConfigError("timed out waiting for the compiler lock")

    monkeypatch.setattr(runtime_config, "compiler_lock", busy)
    said = application._web_off_guard()
    assert _acted(sent) == ["stop"]
    # Its status has no body here: what it holds is only what the app remembers.
    assert "may hold a configuration that plays Electric Eye" in said
    assert "Playback was stopped" in said
    assert _names_page(), "nothing was written without the lock"


def test_play_is_put_back_once_nothing_to_play_is_loaded(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stop only bridges: once the service holds nothing to play, its Play intent returns.

    So turning web wallpapers on again plays as before, not stills only. Nothing can
    start from the document it now holds.
    """
    _profile, sent, _reports = _turn_off(
        profiles,
        monkeypatch,
        service=False,
        fail_compile=True,
        refuse_reload=False,
        schemas=READS_EIGHT,
        playback="playing",
    )
    assert _acted(sent) == ["stop", "reload", "play"]
    assert _plays_nothing()
