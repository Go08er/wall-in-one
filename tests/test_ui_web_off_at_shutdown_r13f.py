"""The last step of a shutdown while web wallpapers are off: review r13f's findings, made tests.

`Application._web_off_on_disk_at_exit`, called as `do_shutdown` calls it last, on a windowless
application. Review r13f found, at f127a31:

* **N1:** its look at the file read TOML 1.0, and the service reads 1.1: a hand edit the
  service loads as allowing web wallpapers was left.
* **N2:** that look parsed a compiled document whenever a value in it had a ``\\u``, ``\\U``
  or ``\\x`` (a still's path, a title): 0.13 s for each megabyte, about a second at the cap,
  and a second Ctrl-C couldn't end it.
* **L1:** a second Ctrl-C while this process's runtime lane held the compiler lock, with a
  turn-off save still being written: the step only tried the lock and returned, the lane
  installed the document for web wallpapers on that it had rightly compiled (the save hadn't
  landed), and the save landed off. Web wallpapers off, and a file that allows them.
* **N3:** a turn-off still being written was judged as landed once, before the lock: a document
  for them on, installed once they were turned on again after it landed, was replaced.

What the step can't settle itself (a turn-off still being written, a lock it didn't get) is
judged again on a thread once the save and this process's lane are done (`_web_off_deferred`);
each test joins it before it looks at the file.

Each case has a control that passes before and after.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
import tomllib
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import replace
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_ui_codex030_round1b import profiles, toolkit  # noqa: E402, F401
from tests.test_web_off_toml_1_1 import DOCUMENTS, document  # noqa: E402
from wall_in_one import config, paths, runtime_compatibility, runtime_config  # noqa: E402
from wall_in_one.ui import app as app_module  # noqa: E402

#: How long anything here waits before it gives up: past it the test fails, it doesn't hang.
GIVE_UP: float = 60.0


def _on_disk(text: str) -> None:
    """``text`` as ``runtime.toml``, with web wallpapers off in ``settings.toml``."""
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    config.update({"allow_web_wallpapers": False})


def _nothing_to_play() -> str:
    return runtime_config.nothing_to_play_document(
        config.load_strict(), runtime_config.WEB_OFF_REASON
    )


# -- N1: TOML 1.1 -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", DOCUMENTS)
def test_a_document_the_service_reads_as_allowing_web_wallpapers_is_replaced(
    profiles: Any,  # noqa: F811
    name: str,
) -> None:
    """Before: ``[x-escape-key]`` was left (its escape sent it to a parse that refused it), and
    so were ``[inline-table-over-lines]`` and ``[key-and-an-x-escape-elsewhere]``. Each one the
    service loads as allowing web wallpapers is replaced; the step judges exactly as the service
    does here, since a document with no spelling of the key anywhere (``[an-x-escape-and-no-
    key]``) can't allow them under any TOML, and is left."""
    application = profiles().application
    text, service_allows, _toml_1_0 = document(name)
    _on_disk(text)
    application._web_off_on_disk_at_exit()
    now = paths.runtime_config_path().read_text(encoding="utf-8")
    if service_allows:
        assert now == _nothing_to_play(), (
            f"{name}: the service loads it as allowing web wallpapers, and it was left"
        )
        return
    assert now == text, f"{name}: a document that allows no web wallpaper was replaced"


# -- N2: the look at the file -------------------------------------------------------------------


def _compiled(megabytes: float, sequence: str) -> str:
    """A compiler-shaped document of about ``megabytes`` MiB that allows no web wallpaper, with
    ``sequence`` (as `runtime_config._quote` writes it) in one still's path when it is given."""
    head = runtime_config.nothing_to_play_document(config.Settings(), runtime_config.WEB_OFF_REASON)
    lines = [head, "", "[[playlists]]", 'id = "big"', 'name = "Big"']
    size, index = len(head), 0
    while size < megabytes * 1024 * 1024 - 64 * 1024:
        still = f"/home/someone/Pictures/wallpapers/Wall-in-One/Automatic Stills/{index:08d}.png"
        if sequence and index == 1000:
            still = f"/home/someone/Pictures/odd{sequence}/still.png"
        block = [
            "[[playlists.entries]]",
            f"id = {json.dumps(f'/home/someone/Pictures/wallpapers/item-{index:08d}.png')}",
            'kind = "still"',
            f"still = {json.dumps(still, ensure_ascii=False)}",
        ]
        lines.extend(block)
        size += sum(len(one) + 1 for one in block)
        index += 1
    return "\n".join(lines) + "\n"


def _parses_of(monkeypatch: pytest.MonkeyPatch, text: str) -> list[bool]:
    """Every TOML parse of ``text`` from here on, recorded."""
    loads = tomllib.loads
    parsed: list[bool] = []

    def recorded(document: str, *arguments: Any, **keywords: Any) -> Any:
        if document == text:
            parsed.append(True)
        return loads(document, *arguments, **keywords)

    monkeypatch.setattr(tomllib, "loads", recorded)
    return parsed


@pytest.mark.parametrize(
    ("megabytes", "sequence"),
    [
        (1.0, "\\user"),  # a backslash before a u: written \\u
        (8.0, "\\user"),
        (1.0, "\\x41"),
        (1.0, "\a"),  # a control character: written \u0007
        (1.0, ""),  # control: no backslash anywhere
    ],
    ids=["backslash-u-1MiB", "backslash-u-8MiB", "backslash-x", "control-character", "control"],
)
def test_a_compiled_document_is_judged_without_a_parse(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    megabytes: float,
    sequence: str,
) -> None:
    """s4, judged by what the step does rather than by how long it takes. Before: each of these
    but the control was parsed whole, in up to a second at the cap, which a second Ctrl-C
    couldn't end; and it was left, as it is now. The control was never parsed."""
    application = profiles().application
    text = _compiled(megabytes, sequence)
    assert len(text.encode()) <= runtime_config.MAX_RUNTIME_CONFIG_BYTES
    assert "\\" in text or not sequence
    _on_disk(text)
    parsed = _parses_of(monkeypatch, text)
    application._web_off_on_disk_at_exit()
    assert not parsed, f"the step parsed a compiled document {len(parsed)} time(s)"
    assert paths.runtime_config_path().read_text(encoding="utf-8") == text


def test_control_a_hand_edit_that_names_the_key_is_parsed(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What only a parse can tell (here: ``= false``) is still parsed, and left."""
    application = profiles().application
    text, _service_allows, _toml_1_0 = document("control-off")
    _on_disk(text)
    parsed = _parses_of(monkeypatch, text)
    application._web_off_on_disk_at_exit()
    assert parsed, "a document that names the key wasn't parsed"
    assert paths.runtime_config_path().read_text(encoding="utf-8") == text


@pytest.mark.parametrize("variant", ["second-ctrl-c", "control-no-ctrl-c"])
def test_a_second_ctrl_c_ends_the_wait_for_a_parse(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    """A hand edit only a parse can judge, whose parse is held (standing for a large one). A
    second Ctrl-C (a real SIGINT, to this process) while the step waits for it ends the wait:
    the step returns before the parse is let go, and the file counts as allowing web wallpapers
    (fail closed). Before: the parse ran on GTK's thread, and the step returned only once it
    was let go, leaving the file. The control: no Ctrl-C; the parse ends, and the document,
    which allows none, is left."""
    application = profiles().application
    text, _service_allows, _toml_1_0 = document("control-off")
    _on_disk(text)
    parsing = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    order: list[str] = []
    failures: list[str] = []
    allows = runtime_config.allows_web_wallpapers

    def held(*arguments: Any, **keywords: Any) -> bool:
        if keywords.get("text") == text:
            parsing.set()
            if not release.wait(GIVE_UP):
                failures.append("the parse was never let go")
        return allows(*arguments, **keywords)

    monkeypatch.setattr(runtime_config, "allows_web_wallpapers", held)

    def second_ctrl_c() -> None:
        if not parsing.wait(GIVE_UP):
            failures.append("the step never parsed the document")
        elif variant == "second-ctrl-c":
            os.kill(os.getpid(), signal.SIGINT)  # as the terminal sends it: to the process
            if not returned.wait(GIVE_UP):
                failures.append("the step didn't return after the second Ctrl-C")
        order.append("let go")
        release.set()

    thread = threading.Thread(target=second_ctrl_c, name="test-ctrl-c")
    thread.start()
    try:
        application._web_off_on_disk_at_exit()
        order.append("returned")
        returned.set()
    finally:
        release.set()
        thread.join(GIVE_UP)
    assert not failures, failures
    now = paths.runtime_config_path().read_text(encoding="utf-8")
    if variant == "control-no-ctrl-c":
        assert order == ["let go", "returned"], order
        assert now == text, "a document that allows no web wallpaper was replaced"
        return
    assert order == ["returned", "let go"], f"the step waited for the parse: {order}"
    assert now == _nothing_to_play(), "a document the step couldn't judge was left"


# -- L1 and N3: a turn-off still being written, and the lock --------------------------------------


def _judged_at_exit(application: Any) -> None:
    """The step's later judgement, when it made one, joined."""
    thread = getattr(application, "_web_off_deferred", None)
    if thread is not None:
        thread.join(GIVE_UP)
        assert not thread.is_alive(), "the judgement at exit never ended"


def _in_the_steps_wait() -> bool:
    """Whether the step's Ctrl-C handler is installed, as it is for all of the step."""
    handler = signal.getsignal(signal.SIGINT)
    return "_second_interrupt_ends_the_settle" in getattr(handler, "__qualname__", "")


def _until(condition: Callable[[], bool], what: str, failures: list[str]) -> bool:
    """Off GTK: wait for ``condition``, or say that it never came."""
    deadline = time.monotonic() + GIVE_UP
    while not condition():
        if time.monotonic() > deadline:
            failures.append(f"gave up waiting for {what}")
            return False
        time.sleep(0.005)
    return True


def _turn_off_being_written(application: Any) -> Future[Any]:
    """A settings batch that turns web wallpapers off, at the head of the authoring lane, still
    being written: what the step waits for. Its result is set when the test lands it."""
    saving: Future[Any] = Future()
    application._settings_batch_generation = 7
    application._settings_batch_turns_web_off = True
    application._authoring_future = saving
    return saving


def _on_the_runtime_lane(application: Any, lane: Callable[[], None]) -> Future[Any]:
    """``lane`` running on this process's runtime lane, as `do_shutdown` leaves it: let go
    of, with its running job still running, before the step."""
    pool = application._runtime_pool()
    running: Future[Any] = pool.submit(lane)
    pool.shutdown(wait=False, cancel_futures=True)
    application._runtime_lane_at_exit = pool
    application._runtime_jobs = None
    return running


@pytest.mark.parametrize(
    "variant",
    ["lane-holds-the-lock", "lane-takes-it-after-the-step", "control-no-second-ctrl-c"],
)
def test_a_cut_turn_off_while_the_lane_publishes_for_web_on(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    """s2. Web wallpapers on, and the file allows them; a save that turns them off is still
    being written; this process's runtime lane publishes (a number, say) for them on, since the
    save hasn't landed: so it rightly installs a document that allows them, and the save lands
    only after that. A second Ctrl-C ends the step's wait for the save. Before:
    ``[lane-holds-the-lock]``: the step only tried the lock and returned; ``[lane-takes-it-after-
    the-step]``: it replaced the file first, and the lane's document came after. Both left a file
    that allows web wallpapers with them off. Now that save is judged once it has landed and the
    lane is done. The control: no second Ctrl-C; the step waits for the save, then the lock."""
    application = profiles().application
    session = application.session
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    monkeypatch.setattr(app_module, "SETTINGS_SAVE_SECONDS", GIVE_UP)
    on = config.update({"allow_web_wallpapers": True})
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    runtime_config.publish_changes(on, session)
    assert runtime_config.allows_web_wallpapers(target)
    saving = _turn_off_being_written(application)
    cut = variant != "control-no-second-ctrl-c"
    application._settle_cut = cut
    holding = threading.Event()
    returned = threading.Event()
    failures: list[str] = []
    order: list[str] = []

    def publish_then_land() -> None:
        runtime_config.publish_changes(replace(on, shuffle=not on.shuffle), session)
        order.append("lane installed")

    def lane() -> None:
        try:
            if variant == "lane-takes-it-after-the-step":
                holding.set()
                if not returned.wait(GIVE_UP):
                    failures.append("the step never returned")
                with runtime_config.compiler_lock():
                    publish_then_land()
            else:
                with runtime_config.compiler_lock():
                    holding.set()
                    if cut:
                        if not returned.wait(GIVE_UP):
                            failures.append("the step never returned")
                    else:
                        _until(_in_the_steps_wait, "the step's wait for the save", failures)
                    publish_then_land()
            # Then the save lands, with web wallpapers off: nothing publishes it.
            config.update({"allow_web_wallpapers": False})
            order.append("save landed")
            saving.set_result(None)
        except Exception as error:  # pragma: no cover - reported below
            failures.append(f"lane: {type(error).__name__}: {error}")
            if not saving.done():
                saving.set_exception(error)

    running = _on_the_runtime_lane(application, lane)
    try:
        assert holding.wait(GIVE_UP), "the lane never began"
        application._web_off_on_disk_at_exit()
        order.append("step returned")
    finally:
        returned.set()
        running.result(GIVE_UP)
        if not saving.done():
            saving.set_result(None)
        _judged_at_exit(application)
    assert not failures, failures
    if cut:
        assert order == ["step returned", "lane installed", "save landed"], order
    else:
        assert order == ["lane installed", "save landed", "step returned"], order
    assert config.load_strict().allow_web_wallpapers is False
    assert not runtime_config.allows_web_wallpapers(target), (
        f"settings.toml says web wallpapers are off, and the file on disk allows them ({order})"
    )


@pytest.mark.parametrize("variant", ["turned-off-landed-then-on", "control-no-batch"])
def test_a_turn_on_after_the_cut_turn_off_landed_is_left(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    """s5 (N3). A turn-off still being written past the step's wait for it, and another writer
    holding the compiler lock: the save lands, then that writer sees web wallpapers turned on
    again, saves that, and installs a document for them on before it lets go. Before: the step
    had judged the turn-off as landed, once, before the lock, and replaced that newer document.
    Now the setting is read under the lock, after the save has landed: left. The control (r13e
    L2's other half): no batch; the setting is off, then turned on while the step waits for the
    lock, and the newer document is left."""
    application = profiles().application
    session = application.session
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    monkeypatch.setattr(app_module, "SETTINGS_SAVE_SECONDS", 0.2)
    monkeypatch.setattr(app_module, "SHUTDOWN_WEB_OFF_LOCK_SECONDS", GIVE_UP)
    # Not there at f127a31, which this test is also run against.
    monkeypatch.setattr(
        app_module, "SHUTDOWN_WEB_OFF_DEFERRED_LOCK_SECONDS", GIVE_UP, raising=False
    )
    batch = variant != "control-no-batch"
    on = config.update({"allow_web_wallpapers": True}) if batch else config.load_strict()
    on = replace(on, allow_web_wallpapers=True)
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    stale = runtime_config.render(on, session)
    newer = runtime_config.render(replace(on, shuffle=not on.shuffle), session)
    assert stale != newer and runtime_config.allows_web_wallpapers(text=newer)
    target.write_text(stale, encoding="utf-8")
    saving = _turn_off_being_written(application) if batch else None
    holding = threading.Event()
    waiting = threading.Event()
    returned = threading.Event()
    failures: list[str] = []
    lock = runtime_config.compiler_lock
    writer_thread: list[threading.Thread] = []

    def watched(*arguments: Any, **keywords: Any) -> Any:
        if threading.current_thread() not in writer_thread:
            waiting.set()  # the step's wait for the lock, or its later judgement's
        return lock(*arguments, **keywords)

    def writer() -> None:
        with lock():
            holding.set()
            if not _until(
                lambda: waiting.is_set() or returned.is_set(), "the step's end", failures
            ):
                return
            if saving is not None:
                config.update({"allow_web_wallpapers": False})  # the cut save lands
                saving.set_result(None)
            if not waiting.wait(GIVE_UP):
                failures.append("nothing waited for the compiler lock")
                return
            # Another writer then turns them on, and installs a document for them on.
            config.update({"allow_web_wallpapers": True})
            target.write_text(newer, encoding="utf-8")

    monkeypatch.setattr(runtime_config, "compiler_lock", watched)
    thread = threading.Thread(target=writer, name="test-writer")
    writer_thread.append(thread)
    if not batch:
        config.update({"allow_web_wallpapers": False})
    thread.start()
    try:
        assert holding.wait(GIVE_UP)
        application._web_off_on_disk_at_exit()
    finally:
        returned.set()
        waiting.set()
        thread.join(GIVE_UP)
        if saving is not None and not saving.done():
            saving.set_result(None)
        _judged_at_exit(application)
    assert not failures, failures
    assert config.load_strict().allow_web_wallpapers is True
    assert target.read_text(encoding="utf-8") == newer, (
        "the step replaced a document another writer installed for web wallpapers on, after "
        "the save that turned them off had landed"
    )


@pytest.mark.parametrize("variant", ["the-save-then-fails", "control-the-save-lands"])
def test_a_cut_turn_off_that_then_fails_leaves_the_document(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    """s7. A second Ctrl-C, and a turn-off still being written, which then fails: web
    wallpapers stay on. Before (as documented then): it was judged as landed, and the good
    document was replaced, so nothing played while they were on. Now it is judged once it has
    ended: the setting is on, and the document is left. The control: it lands, and the file is
    replaced with the one that plays nothing."""
    application = profiles().application
    session = application.session
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    on = config.update({"allow_web_wallpapers": True})
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    runtime_config.publish_changes(on, session)
    good = target.read_text(encoding="utf-8")
    assert runtime_config.allows_web_wallpapers(text=good)
    saving = _turn_off_being_written(application)
    application._settle_cut = True  # a second Ctrl-C: not waited for
    try:
        application._web_off_on_disk_at_exit()
        if variant == "the-save-then-fails":
            saving.set_exception(config.ConfigError("the save failed (this test)"))
        else:
            config.update({"allow_web_wallpapers": False})
            saving.set_result(None)
    finally:
        if not saving.done():
            saving.set_result(None)
        _judged_at_exit(application)
    now = target.read_text(encoding="utf-8")
    if variant == "control-the-save-lands":
        assert config.load_strict().allow_web_wallpapers is False
        assert now == _nothing_to_play()
        return
    assert config.load_strict().allow_web_wallpapers is True
    assert now == good, "web wallpapers are on, and the good document was replaced"


@pytest.mark.parametrize("variant", ["the-lane-fails", "control-the-lane-installs-for-off"])
def test_the_lanes_lock_held_past_a_second_ctrl_c_is_waited_for_once_it_ends(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    """Web wallpapers already off, and a file that allows them (a publication the shutdown cut
    before it installed). This process's lane holds the compiler lock past a second Ctrl-C, and
    then fails without installing anything. Before (the docstring's own "what that leaves"): the
    step only tried the lock and returned, and the file allowed web wallpapers with them off.
    Now it is judged again once the lane is done. The control: the lane installs its compile for
    them off, which allows none."""
    application = profiles().application
    session = application.session
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    off = config.load_strict()
    assert off.allow_web_wallpapers is False
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        runtime_config.render(replace(off, allow_web_wallpapers=True), session), encoding="utf-8"
    )
    application._settle_cut = True
    holding = threading.Event()
    returned = threading.Event()
    failures: list[str] = []

    def lane() -> None:
        with runtime_config.compiler_lock():
            holding.set()
            if not returned.wait(GIVE_UP):
                failures.append("the step never returned")
            if variant == "control-the-lane-installs-for-off":
                runtime_config.publish_changes(off, session)

    running = _on_the_runtime_lane(application, lane)
    try:
        assert holding.wait(GIVE_UP), "the lane never took the lock"
        application._web_off_on_disk_at_exit()
    finally:
        returned.set()
        running.result(GIVE_UP)
        _judged_at_exit(application)
    assert not failures, failures
    assert not runtime_config.allows_web_wallpapers(target), (
        "settings.toml says web wallpapers are off, and the file on disk allows them"
    )
