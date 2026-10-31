"""Web-off shutdown log: the restart advice (r13l S-N13, S-N14, S-N15).

A fake service only: a monkeypatched `client.send_runtime`; the install refused by a monkeypatch.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_ui_codex030_round1b import profiles, toolkit  # noqa: E402, F401
from tests.test_ui_renderer_settle_shutdown import HEARD, _naming  # noqa: E402
from tests.test_ui_web_off_at_shutdown import _heard, _workshop  # noqa: E402
from tests.test_web_off_toml_1_1 import document  # noqa: E402
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.ui import app as app_module  # noqa: E402

RESTART = "systemctl --user restart wall-in-one"
OLDER = "older than this app"
CURRENT_SERVICE = "That service keeps the configuration it holds until it is restarted"
IF_RUNNING = "If the wallpaper service is running"


def _on_disk(text: str) -> None:
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    config.update({"allow_web_wallpapers": False})


def _judged_at_exit(application: Any) -> None:
    thread = getattr(application, "_web_off_deferred", None)
    if thread is not None:
        thread.join(60.0)
        assert not thread.is_alive()


def _step(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    *,
    heard: str,
    text: str | None,
    installed: bool,
    quit_answer: Any = None,
    web: int | None = None,
) -> tuple[list[str], list[str]]:
    """The step's log, and the verbs sent. ``web``: ``_workshop(web)`` first; the text is then
    ``_naming`` of its web page when ``text`` is None."""
    application = profiles().application
    if web is not None:
        _workshop(web)
        if text is None:
            text = _naming([str(2_000_000_000 + web)])
    assert text is not None
    _on_disk(text)
    _heard(monkeypatch, HEARD[heard])
    sent: list[str] = []

    def send(verb: str, *_a: object, **_k: object) -> Response:
        sent.append(verb)
        if isinstance(quit_answer, BaseException):
            raise quit_answer
        return quit_answer if quit_answer is not None else Response.success()

    monkeypatch.setattr(client, "send_runtime", send)
    if not installed:

        def refuse(*_a: object, **_k: object) -> None:
            raise runtime_config.RuntimeConfigError("the disk is full (r13m)")

        monkeypatch.setattr(runtime_config, "install_nothing_to_play", refuse)
    caplog.set_level(logging.INFO, logger=app_module.LOGGER.name)
    application._web_off_on_disk_at_exit()
    _judged_at_exit(application)
    said = [r.getMessage() for r in caplog.records if r.name == app_module.LOGGER.name]
    return sent, said


# -- p1 (S-N13) ------------------------------------------------------------------------------


def test_s_n13_a_failed_install_doesnt_call_a_service_that_reads_eight_older(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent, said = _step(
        profiles,
        monkeypatch,
        caplog,
        heard="control-reads-eight",
        text=document("control-compiler-line")[0],
        installed=False,
    )
    assert sent == []
    assert any("couldn\u2019t be replaced at shutdown" in one for one in said), said
    assert not any(OLDER in one for one in said), said


def test_control_s_n13_a_failed_install_for_an_older_service_says_it_is_older(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent, said = _step(
        profiles,
        monkeypatch,
        caplog,
        heard="an-older-service",
        text=document("control-compiler-line")[0],
        installed=False,
    )
    assert sent == []
    assert any(OLDER in one and RESTART in one for one in said), said


# -- p2 (S-N14) ------------------------------------------------------------------------------


def test_s_n14_an_installed_file_that_allowed_web_says_the_older_service_keeps_what_it_holds(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent, said = _step(
        profiles,
        monkeypatch,
        caplog,
        heard="an-older-service",
        text=document("control-compiler-line")[0],
        installed=True,
    )
    assert sent == [], "no quit for `_ALLOWS`"
    assert any("it was replaced at shutdown" in one for one in said), said
    assert any(RESTART in one for one in said), said


def test_control_s_n14_an_installed_file_naming_an_unchecked_item_says_it(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent, said = _step(
        profiles,
        monkeypatch,
        caplog,
        heard="an-older-service",
        text=_naming(["4000000099"]),  # no such item: `_NAMES`
        installed=True,
    )
    assert sent == []
    assert any("it was replaced at shutdown" in one and RESTART in one for one in said), said


# -- p3 (S-N15) ------------------------------------------------------------------------------


@pytest.mark.parametrize("installed", [True, False], ids=["installed", "failed"])
@pytest.mark.parametrize(
    "answer",
    ["refused", "couldnt-be-told"],
)
def test_s_n15_a_quit_that_failed_says_to_restart_the_older_service(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    installed: bool,
    answer: str,
) -> None:
    quit_answer: Any = (
        Response.failure("busy (r13m)")
        if answer == "refused"
        else client.ControlError("timed out (r13m)")
    )
    sent, said = _step(
        profiles,
        monkeypatch,
        caplog,
        heard="an-older-service",
        text=None,
        web=1,
        installed=installed,
        quit_answer=quit_answer,
    )
    assert sent == ["quit"], "the premise: `_NAMES_WEB`, the quit is sent"
    assert any("was sent quit at shutdown" in one for one in said), said
    assert any(RESTART in one for one in said), said


def test_control_s_n15_a_quit_answered_ok_says_it_is_quitting(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent, said = _step(
        profiles,
        monkeypatch,
        caplog,
        heard="an-older-service",
        text=None,
        web=1,
        installed=True,
    )
    assert sent == ["quit"]
    assert any("it is quitting" in one for one in said), said


# -- r13m S-N16 --------------------------------------------------------------------------------


def test_s_n16_a_failed_install_tells_a_current_service_to_restart_without_calling_it_older(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The service reads schema 8 and holds a file that allows web wallpapers: the install
    failed, so it keeps that file until restarted. The control above has an older one."""
    sent, said = _step(
        profiles,
        monkeypatch,
        caplog,
        heard="control-reads-eight",
        text=document("control-compiler-line")[0],
        installed=False,
    )
    assert sent == []
    assert any("couldn\u2019t be replaced at shutdown" in one for one in said), said
    advice = [one for one in said if RESTART in one]
    assert len(advice) == 1 and advice[0].startswith(CURRENT_SERVICE), said
    assert not any(one.startswith(IF_RUNNING) for one in said), said  # the twin's line
    assert not any(OLDER in one for one in said), said


@pytest.mark.parametrize("installed", [False, True], ids=["failed", "control-installed"])
def test_s_n16_twin_a_failed_install_with_no_service_heard_says_to_restart_if_it_runs(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    installed: bool,
) -> None:
    """No status was heard, so the service may or may not be running; the control installed."""
    sent, said = _step(
        profiles,
        monkeypatch,
        caplog,
        heard="nothing",
        text=document("control-compiler-line")[0],
        installed=installed,
    )
    assert sent == []
    advice = [one for one in said if one.startswith("If the wallpaper service is running")]
    if installed:
        assert advice == [], said
        return
    assert any("couldn\u2019t be replaced at shutdown" in one for one in said), said
    assert len(advice) == 1 and RESTART in advice[0], said
    assert not any(OLDER in one for one in said), said


# -- r13n S-N18 --------------------------------------------------------------------------------


def _lock_never_had(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    heard: str,
    *,
    text: str | None = None,
    web: int | None = None,
    install_while_held: str | None = None,
    turn_web_on_while_held: bool = False,
) -> tuple[list[str], list[str]]:
    """Another writer holds the compiler lock past the step's wait and the later judgement's
    (0.2 s and 0.5 s here), over ``text`` (default: a file that allows web wallpapers; ``web``:
    ``_workshop(web)`` first). ``install_while_held``: that writer installs this document, by
    temp file and rename, after the step's own judgement and while it still holds the lock.
    The log lines, the verbs."""
    import time

    application = profiles().application
    if web is not None:
        _workshop(web)
    _on_disk(text if text is not None else document("control-compiler-line")[0])
    _heard(monkeypatch, HEARD[heard])
    monkeypatch.setattr(app_module, "SHUTDOWN_WEB_OFF_LOCK_SECONDS", 0.2)
    monkeypatch.setattr(app_module, "SHUTDOWN_WEB_OFF_DEFERRED_LOCK_SECONDS", 0.5)
    sent: list[str] = []

    def send(verb: str, *_a: object, **_k: object) -> Response:
        sent.append(verb)
        return Response.success()

    monkeypatch.setattr(client, "send_runtime", send)
    holding = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with runtime_config.compiler_lock():
            holding.set()
            if install_while_held is not None:
                time.sleep(0.35)
                path = paths.runtime_config_path()
                tmp = path.with_name(path.name + ".other")
                tmp.write_text(install_while_held, encoding="utf-8")
                tmp.replace(path)
            if turn_web_on_while_held:
                time.sleep(0.35)
                config.update({"allow_web_wallpapers": True})
            release.wait(30.0)

    thread = threading.Thread(target=holder)
    thread.start()
    caplog.set_level(logging.INFO, logger=app_module.LOGGER.name)
    try:
        assert holding.wait(10.0)
        application._web_off_on_disk_at_exit()
        _judged_at_exit(application)
    finally:
        release.set()
        thread.join(30.0)
    said = [r.getMessage() for r in caplog.records if r.name == app_module.LOGGER.name]
    return sent, said


@pytest.mark.parametrize(
    ("heard", "line"),
    [
        ("control-reads-eight", CURRENT_SERVICE),
        ("nothing", IF_RUNNING),
        ("an-older-service", "That service is older than this app"),
    ],
)
def test_s_n18_a_lock_never_had_over_a_file_that_allows_web_says_to_restart(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    heard: str,
    line: str,
) -> None:
    """The file was judged as one to replace and is left as it is: the restart is said in the
    service's form, and no quit is sent from this path."""
    sent, said = _lock_never_had(profiles, monkeypatch, caplog, heard)
    advice = [one for one in said if RESTART in one]
    assert len(advice) == 1 and advice[0].startswith(line), said
    assert sent == []


@pytest.mark.parametrize("why", ["names", "names-web"])
def test_t_4_a_lock_never_had_over_a_file_naming_an_item_an_older_service_may_play_says_to_restart(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    why: str,
) -> None:
    """`_NAMES` (an item that can't be checked) and `_NAMES_WEB` (a confirmed web page): an older
    service has no check of its own, and no quit is sent from this path, so the restart line is
    the only remedy."""
    if why == "names":
        sent, said = _lock_never_had(
            profiles, monkeypatch, caplog, "an-older-service", text=_naming(["2000000099"])
        )
    else:
        sent, said = _lock_never_had(
            profiles,
            monkeypatch,
            caplog,
            "an-older-service",
            text=_naming([str(2_000_000_001)]),  # the web page `_workshop(1)` makes
            web=1,
        )
    advice = [one for one in said if RESTART in one]
    assert len(advice) == 1 and advice[0].startswith("That service is older than this app"), said
    assert sent == [], "no quit is sent from the lock-never-had path"
    assert not any("was sent quit at shutdown" in one for one in said), said


@pytest.mark.parametrize("heard", ["control-reads-eight", "nothing", "an-older-service"])
def test_s_n19_the_advice_on_the_lock_never_had_path_follows_a_line_saying_why(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    heard: str,
) -> None:
    """The file was judged (it could let a web wallpaper play) and not replaced: the line before
    the advice says so and why, and nothing says the file was not checked or leaves it to the next
    start."""
    _, said = _lock_never_had(profiles, monkeypatch, caplog, heard)
    at = next(i for i, one in enumerate(said) if RESTART in one)
    why = said[at - 1]
    assert "could let a web wallpaper play" in why, said
    assert "couldn\u2019t be replaced at exit" in why, said
    assert "compiler lock" in why, said
    stale = ("was not checked", "the next start judges it", "another writer held")
    assert not any(one_stale in one for one in said for one_stale in stale), said


@pytest.mark.parametrize("why", ["names", "names-web"])
def test_s_n19_the_advice_follows_a_line_saying_why_for_a_file_naming_an_item(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    why: str,
) -> None:
    """r13p N-2: the same pin as above for `_NAMES` and `_NAMES_WEB`, not only `_ALLOWS`."""
    if why == "names":
        _, said = _lock_never_had(
            profiles, monkeypatch, caplog, "an-older-service", text=_naming(["2000000099"])
        )
    else:
        _, said = _lock_never_had(
            profiles,
            monkeypatch,
            caplog,
            "an-older-service",
            text=_naming([str(2_000_000_001)]),
            web=1,
        )
    at = next(i for i, one in enumerate(said) if RESTART in one)
    line = said[at - 1]
    assert "could let a web wallpaper play" in line, said
    assert "couldn\u2019t be replaced at exit" in line, said
    assert "compiler lock" in line, said
    stale = ("was not checked", "the next start judges it", "another writer held")
    assert not any(one_stale in one for one in said for one_stale in stale), said


def test_control_s_n18_a_lock_never_had_over_a_file_the_holder_replaced_says_no_restart(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The step judges the file as one to replace and can't have the lock. The other writer, holding
    it throughout, installs a document that names nothing: the later read-only judgement reads
    that one and has nothing to advise."""
    sent, said = _lock_never_had(
        profiles,
        monkeypatch,
        caplog,
        "an-older-service",
        install_while_held=_naming([]),
    )
    assert any("needs no replacement at exit" in one for one in said), (
        f"the premise (the path): {said}"
    )
    assert not any("was not checked" in one for one in said), said
    assert not any(RESTART in one for one in said), said
    assert sent == []


def test_control_s_n18_a_lock_never_had_while_the_holder_turns_web_on_says_so(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The holder turns Allow web wallpapers on while it holds the lock: the file is never read
    then, and the setting is on. The line must not say the file was read or the setting is off."""
    sent, said = _lock_never_had(
        profiles, monkeypatch, caplog, "an-older-service", turn_web_on_while_held=True
    )
    assert config.load_strict(paths.settings_path()).allow_web_wallpapers, "the premise"
    lines = [one for one in said if "needs no replacement at exit" in one]
    assert len(lines) == 1, said
    assert "Allow web wallpapers is on now" in lines[0], lines
    assert "was read" not in lines[0] and "being off" not in lines[0], lines
    assert not any("was not checked" in one for one in said), said
    assert not any(RESTART in one for one in said), said
    assert sent == []


# -- r13m S-N17 --------------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["no-answer-in-time", "second-ctrl-c"])
def test_s_n17_a_quit_not_answered_in_time_or_cut_says_to_restart(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    how: str,
) -> None:
    application = profiles().application
    _workshop(1)
    _on_disk(_naming([str(2_000_000_001)]))  # the web page `_workshop(1)` makes
    _heard(monkeypatch, HEARD["an-older-service"])
    monkeypatch.setattr(app_module, "SHUTDOWN_OLDER_SERVICE_QUIT_SECONDS", 0.3)
    sent: list[str] = []
    release = threading.Event()

    def send(verb: str, *_a: object, **_k: object) -> Response:
        sent.append(verb)
        if how == "second-ctrl-c":
            application._settle_cut = True
        release.wait(5.0)
        return Response.success()

    monkeypatch.setattr(client, "send_runtime", send)
    caplog.set_level(logging.INFO, logger=app_module.LOGGER.name)
    try:
        application._web_off_on_disk_at_exit()
        _judged_at_exit(application)
    finally:
        release.set()
    said = [r.getMessage() for r in caplog.records if r.name == app_module.LOGGER.name]
    assert sent == ["quit"], sent
    lines = [one for one in said if "was sent quit at shutdown" in one]
    assert len(lines) == 1, said
    expected = (
        "didn\u2019t answer within 0.3 s" if how == "no-answer-in-time" else "a second Ctrl-C"
    )
    assert expected in lines[0], lines
    assert RESTART in lines[0], lines
