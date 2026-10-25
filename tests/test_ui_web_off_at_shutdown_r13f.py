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
* **L2:** with no status heard, or one from a service older than schema 8 (which has no check of
  an item's type), a file that names a web page was left: such a service plays it. 040b3aa
  replaced it, after a scan of the whole Workshop.

What the step can't settle itself (a turn-off still being written, a lock it didn't get) is
judged again on a thread once the save and this process's lane are done (`_web_off_deferred`);
each test joins it before it looks at the file.

Each case has a control that passes before and after.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_ui_codex030_round1b import profiles, toolkit  # noqa: E402, F401
from tests.test_ui_renderer_settle_shutdown import HEARD, _naming  # noqa: E402
from tests.test_ui_web_off_at_shutdown import _heard, _scans, _workshop  # noqa: E402
from tests.test_web_off_toml_1_1 import DOCUMENTS, document  # noqa: E402
from wall_in_one import config, paths, runtime_compatibility, runtime_config  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Request, Response  # noqa: E402
from wall_in_one.library import workshop  # noqa: E402
from wall_in_one.ui import app as app_module  # noqa: E402

#: How long anything here waits before it gives up: past it the test fails, it doesn't hang.
GIVE_UP: float = 60.0
#: The real runtime client, which `profiles` stands in for: the quit tests talk to a fake
#: service's socket through it.
REAL_SEND_RUNTIME = client.send_runtime


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
    of, with its running job still running, before the step. Let go of only once the job is
    running: `do_shutdown` cancels what is still queued."""
    started = threading.Event()

    def job() -> None:
        started.set()
        lane()

    pool = application._runtime_pool()
    running: Future[Any] = pool.submit(job)
    assert started.wait(GIVE_UP), "the lane's job never began"
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


# -- L2: a file that names a web page, for a service with no check of an item's type ------------


def _web_page(count: int) -> str:
    """The web page `_workshop` installs after ``count`` scenes."""
    return str(2_000_000_000 + count)


def _preset(content: Any, identifier: str, base: str) -> None:
    """A Wallpaper Engine preset (values for ``base``, no type of its own) in ``content``."""
    folder = content / identifier
    folder.mkdir()
    (folder / "project.json").write_text(
        json.dumps({"title": f"Preset of {base}", "preset": {"x": 1}, "dependency": base})
    )


def _as_0_4_0_wrote_it(scenes: list[str]) -> str:
    """A ``runtime.toml`` in 0.4.0's own shape (review r13f's legacy producer, q4): each Workshop
    item an entry of kind "scene" with its ``scene_id`` and the still it was paired with."""
    lines = ["schema_version = 4", f'config_generation = "{"e" * 64}"', 'default_playlist = "p"']
    lines += ["", "[[playlists]]", 'id = "p"', 'name = "All media"']
    for scene in scenes:
        lines += [
            "",
            "[[playlists.entries]]",
            f'id = "{scene[-16:]:0>16}"',
            'kind = "scene"',
            f'scene_id = "{scene}"',
            'still = "/home/someone/Pictures/wallpapers/plain.png"',
        ]
    return "\n".join(lines) + "\n"


NAMES = (
    "a-web-page",
    "0-4-0s-document-whose-scene-became-a-web-page",
    "a-preset-of-a-web-page",
    "a-hand-edit-naming-a-web-page",
    "a-hand-edit-in-toml-1-1",
    "control-scenes-only",
    "control-0-4-0s-document-as-published",
    "control-a-preset-of-a-scene",
    "control-a-hand-edit-naming-scenes",
    "control-toml-1-1-naming-scenes",
)


def _naming_document(content: Any, scenes: list[str], names: str) -> str:
    web = _web_page(len(scenes))
    if names == "a-web-page":
        return _naming([*scenes, web])
    if names.startswith("0-4-0s-document-whose"):
        # A Workshop update since: the item 0.4.0 compiled as a scene is a web page now.
        (content / scenes[0] / "project.json").write_text(
            json.dumps({"type": "web", "title": "Item 0", "file": "index.html"})
        )
        (content / scenes[0] / "index.html").write_text("<html></html>")
        return _as_0_4_0_wrote_it(scenes[:1])
    if names == "control-0-4-0s-document-as-published":
        return _as_0_4_0_wrote_it(scenes[:1])
    if "preset" in names:
        base = web if names == "a-preset-of-a-web-page" else scenes[0]
        _preset(content, "2100000000", base)
        return (
            _naming(scenes[1:])
            + 'scene_base = "'
            + base
            + '"\n'
            + ('[[playlists.entries]]\nscene_id = "2100000000"\n')
        )
    if names in ("a-hand-edit-naming-a-web-page", "control-a-hand-edit-naming-scenes"):
        named = [*scenes, web] if "web" in names else scenes
        return _naming([]) + "".join(f'[[playlists.entries]]\nscene_id="{one}"\n' for one in named)
    if names == "a-hand-edit-in-toml-1-1":
        return _naming([]) + f'name = "\\x41"\n[[playlists.entries]]\nscene_id="{scenes[0]}"\n'
    assert names in ("control-scenes-only", "control-toml-1-1-naming-scenes"), names
    text = _naming(scenes)
    return text.replace('id = "p"\n', 'id = "p"\nname = "\\x41"\n', 1) if "1-1" in names else text


@pytest.mark.parametrize("heard", list(HEARD))
@pytest.mark.parametrize("names", NAMES)
def test_a_file_naming_a_web_page_for_a_service_without_a_type_check(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    heard: str,
    names: str,
) -> None:
    """s1 and q4. Web wallpapers off, and a file that allows none but names a Workshop web page
    (or a preset of one, or 0.4.0's document whose scene became one since): a service older
    than schema 8 plays it. Before (f127a31): left, for none heard and for an older service.
    Now each id's own project.json is read, and nothing else: no scan. A hand edit is parsed for
    its ids; one that isn't TOML 1.0 can't be checked, which counts for an older service, and
    not with no status heard (only a web page the files confirm counts then). For a service
    that reads 8 (the control status) every one is left: it checks each item itself; and the
    files that name only scenes (the controls) are left whatever was heard."""
    application = profiles().application
    content, scenes = _workshop(3)
    text = _naming_document(content, scenes, names)
    _on_disk(text)
    _heard(monkeypatch, HEARD[heard])
    scans = _scans(monkeypatch)
    application._web_off_on_disk_at_exit()
    _judged_at_exit(application)
    now = paths.runtime_config_path().read_text(encoding="utf-8")
    assert not scans, f"the step scanned the Workshop {len(scans)} time(s)"
    confirmed = not names.startswith("control") and names != "a-hand-edit-in-toml-1-1"
    if (heard == "an-older-service" and not names.startswith("control")) or (
        heard == "nothing" and confirmed
    ):
        assert now == _nothing_to_play(), (
            f"web wallpapers are off, {heard} has no check of an item's type, and the file on "
            f"disk still names a web page ({names})"
        )
        return
    assert now == text, f"a file {heard} plays no web page from was replaced ({names})"


def _reads_of(
    monkeypatch: pytest.MonkeyPatch, then: Callable[[], None] = lambda: None
) -> list[str]:
    """Every ``project.json`` the Workshop look reads from here on; ``then`` after the first."""
    read_project = workshop._read_project
    reads: list[str] = []

    def recorded(directory: Any, *arguments: Any, **keywords: Any) -> Any:
        reads.append(directory.name)
        found = read_project(directory, *arguments, **keywords)
        if len(reads) == 1:
            then()
        return found

    monkeypatch.setattr(workshop, "_read_project", recorded)
    return reads


def test_only_the_ids_the_file_names_are_read(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded by the file, not by the library: of 40 installed scenes, the file names 3, and
    only theirs are read, once each; no folder is listed. Left: none is a web page."""
    application = profiles().application
    _content, scenes = _workshop(40)
    text = _naming(scenes[:3])
    _on_disk(text)
    _heard(monkeypatch, HEARD["nothing"])
    listed: list[bool] = []
    scan_content = workshop.scan_content_directories

    def recorded(*arguments: Any, **keywords: Any) -> Any:
        listed.append(True)
        return scan_content(*arguments, **keywords)

    monkeypatch.setattr(workshop, "scan_content_directories", recorded)
    reads = _reads_of(monkeypatch)
    application._web_off_on_disk_at_exit()
    assert not listed, "a content directory was listed"
    assert sorted(reads) == sorted(scenes[:3]), reads
    assert paths.runtime_config_path().read_text(encoding="utf-8") == text


@pytest.mark.parametrize("heard", ["an-older-service", "nothing"])
@pytest.mark.parametrize("variant", ["second-ctrl-c", "past-its-bound", "control"])
def test_a_second_ctrl_c_or_the_bound_ends_the_look_and_fails_closed(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    heard: str,
    variant: str,
) -> None:
    """The file names only scenes. A second Ctrl-C (a real SIGINT) after the first project.json
    is read ends the look; so does the look's bound, put behind it here. For an older service
    the ids it didn't reach count as web pages, and the file is replaced. With no status heard,
    only a web page the files confirm counts, and none was: the file is left. The control: the
    look ends by itself (every id read: `test_only_the_ids_the_file_names_are_read`), and the
    file is left."""
    application = profiles().application
    _content, scenes = _workshop(5)
    text = _naming(scenes)
    _on_disk(text)
    _heard(monkeypatch, HEARD[heard])
    if variant == "past-its-bound":
        # Not there at f127a31, which this test is also run against.
        monkeypatch.setattr(app_module, "SHUTDOWN_WORKSHOP_LOOK_SECONDS", -1.0, raising=False)
    failures: list[str] = []

    def second_ctrl_c() -> None:
        os.kill(os.getpid(), signal.SIGINT)
        # Handled on the main thread, which may be waiting for the look's own (review r13g,
        # S-L1): the look then asks for it before its next read.
        _until(lambda: application._settle_cut, "the second Ctrl-C to be handled", failures)

    reads = _reads_of(
        monkeypatch, then=second_ctrl_c if variant == "second-ctrl-c" else lambda: None
    )
    application._web_off_on_disk_at_exit()
    assert not failures, failures
    now = paths.runtime_config_path().read_text(encoding="utf-8")
    if variant == "control":
        assert now == text
        return
    assert len(reads) <= 1, f"the look went on after it was ended: {reads}"
    if heard == "nothing":
        assert now == text, "with no status heard, ids it didn't reach replaced the file"
        return
    assert now == _nothing_to_play(), "a file the look couldn't finish with was left"


#: The most the held call below holds the look, standing in for one slow filesystem call (a
#: library on a disk spinning up, a hung mount): before review r13g's S-L1 was fixed, the step
#: took this long, whatever its bound or a second Ctrl-C said.
SLOW_CALL_SECONDS: float = 3.0


def _held_call(
    monkeypatch: pytest.MonkeyPatch, where: str, order: list[str]
) -> tuple[threading.Event, threading.Event, threading.Event]:
    """One call of the Workshop look held, as one slow filesystem call holds it: Steam's library
    list and a stat of each library (``content-directories``: made before the look first asks
    whether to give up), or the first ``project.json`` read (``a-read``). It is held until the
    test lets it go, or `SLOW_CALL_SECONDS`, and then says so in ``order``. The Events: it
    began, let it go, it returned. ``control-fast``: nothing is held."""
    began, release, ended = threading.Event(), threading.Event(), threading.Event()

    def hold() -> None:
        began.set()
        release.wait(SLOW_CALL_SECONDS)
        order.append("the slow call returned")
        ended.set()

    if where == "content-directories":
        content_directories = workshop.content_directories

        def held_directories(*arguments: Any, **keywords: Any) -> Any:
            hold()
            return content_directories(*arguments, **keywords)

        monkeypatch.setattr(workshop, "content_directories", held_directories)
    elif where == "a-read":
        read_project = workshop._read_project

        def held_read(directory: Any, *arguments: Any, **keywords: Any) -> Any:
            if not began.is_set():
                hold()
            return read_project(directory, *arguments, **keywords)

        monkeypatch.setattr(workshop, "_read_project", held_read)
    else:
        assert where == "control-fast", where
    return began, release, ended


@pytest.mark.parametrize("where", ["content-directories", "a-read", "control-fast"])
def test_a_second_ctrl_c_ends_the_wait_for_a_slow_look(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    """Review r13g, S-L1 (its p1): nothing heard, and a file naming three scenes, one call of
    whose look is held (`_held_call`). The look's bound is put out of reach, so only a second
    Ctrl-C (a real SIGINT, sent once the call is held) can end the wait for it: the step returns
    while the call is still held, and, with no status heard, the ids it didn't reach leave the
    file. Before: the look ran on GTK's thread, where the SIGINT only set what the look asks
    between calls, and the step returned once the call did. The control: nothing held, no
    Ctrl-C; every id is read, and the file, naming only scenes, is left."""
    application = profiles().application
    _content, scenes = _workshop(3)
    text = _naming(scenes)
    _on_disk(text)
    _heard(monkeypatch, HEARD["nothing"])
    monkeypatch.setattr(app_module, "SHUTDOWN_WORKSHOP_LOOK_SECONDS", GIVE_UP)
    order: list[str] = []
    failures: list[str] = []
    began, release, ended = _held_call(monkeypatch, where, order)

    def second_ctrl_c() -> None:
        if not began.wait(GIVE_UP):
            failures.append("the look never made the held call")
            return
        os.kill(os.getpid(), signal.SIGINT)  # as the terminal sends it: to the process

    sender = threading.Thread(target=second_ctrl_c, name="test-ctrl-c")
    if where != "control-fast":
        sender.start()
    start = time.monotonic()
    try:
        application._web_off_on_disk_at_exit()
        took = time.monotonic() - start
        order.append("the step returned")
    finally:
        release.set()
        if where != "control-fast":
            sender.join(GIVE_UP)
            assert ended.wait(GIVE_UP), "the held call never returned"
    _judged_at_exit(application)
    assert not failures, failures
    now = paths.runtime_config_path().read_text(encoding="utf-8")
    if where == "control-fast":
        assert order == ["the step returned"], order
        assert now == text, "a file naming only scenes was replaced"
        return
    assert application._settle_cut, "the second Ctrl-C was never handled"
    assert order == ["the step returned", "the slow call returned"], (
        f"the step waited for the slow call: it took {took:.2f} s ({order})"
    )
    assert now == text, "with no status heard, ids the look didn't reach replaced the file"


@pytest.mark.parametrize("where", ["content-directories", "a-read", "control-fast"])
def test_the_bound_ends_the_wait_for_a_slow_look(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    """Review r13g, S-L1 (its p2): an older service heard, no Ctrl-C, and a file naming three
    scenes, one call of whose look is held (`_held_call`). The look's bound (put at 0.5 s here,
    well short of the hold) ends the wait for it: the step returns while the call is still
    held, and the ids it didn't reach count as web pages, as they do once the bound passes
    between calls, so the file is replaced. Before: the step returned once the call did, past
    the bound. The control: nothing held; every id is read within the bound, and the file,
    naming only scenes, is left."""
    application = profiles().application
    _content, scenes = _workshop(3)
    text = _naming(scenes)
    _on_disk(text)
    _heard(monkeypatch, HEARD["an-older-service"])
    monkeypatch.setattr(app_module, "SHUTDOWN_WORKSHOP_LOOK_SECONDS", 0.5)
    order: list[str] = []
    _began, release, ended = _held_call(monkeypatch, where, order)
    start = time.monotonic()
    try:
        application._web_off_on_disk_at_exit()
        took = time.monotonic() - start
        order.append("the step returned")
    finally:
        release.set()
        if where != "control-fast":
            assert ended.wait(GIVE_UP), "the held call never returned"
    _judged_at_exit(application)
    now = paths.runtime_config_path().read_text(encoding="utf-8")
    if where == "control-fast":
        assert order == ["the step returned"], order
        assert now == text, "a file naming only scenes was replaced"
        return
    assert order == ["the step returned", "the slow call returned"], (
        f"the step waited for the slow call: it took {took:.2f} s, past its 0.5 s bound ({order})"
    )
    assert now == _nothing_to_play(), "a file the look couldn't finish with was left"


def test_the_later_judgement_reads_the_ids_too(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second Ctrl-C while this process's lane holds the compiler lock, an older service, and a
    file that names a web page (no key): the step can't settle it, and the judgement once the
    lane is done reads the ids, uncut. Before: left."""
    application = profiles().application
    _content, scenes = _workshop(2)
    text = _naming([*scenes, _web_page(2)])
    _on_disk(text)
    _heard(monkeypatch, HEARD["an-older-service"])
    application._settle_cut = True
    holding = threading.Event()
    returned = threading.Event()
    failures: list[str] = []

    def lane() -> None:
        with runtime_config.compiler_lock():
            holding.set()
            if not returned.wait(GIVE_UP):
                failures.append("the step never returned")

    running = _on_the_runtime_lane(application, lane)
    try:
        assert holding.wait(GIVE_UP)
        application._web_off_on_disk_at_exit()
    finally:
        returned.set()
        running.result(GIVE_UP)
        _judged_at_exit(application)
    assert not failures, failures
    assert paths.runtime_config_path().read_text(encoding="utf-8") == _nothing_to_play()


@pytest.mark.parametrize(
    "case",
    [
        "an-unsubscribed-item",
        "a-preset-whose-base-isnt-installed",
        "a-library-that-isnt-mounted",
        "control-a-web-page",
        "control-a-preset-of-a-web-page",
        "control-an-older-service-and-an-unsubscribed-item",
    ],
)
def test_with_nothing_heard_only_a_web_page_the_files_confirm_counts(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """No status heard at all, web wallpapers off, and a file whose ids can't all be checked: an
    item unsubscribed since, a preset whose base isn't installed, an item on a Steam library
    that isn't mounted. Such a service is either not running (its start compiles again, or
    refuses a kept file with exit 78) or current (it refuses a page at launch itself). Before
    (a848d28): each of these replaced the file, so nothing played until a publication. Now they
    are left. The controls: a web page, or a preset of one, which the files confirm, is
    replaced; and for an older service heard, what can't be checked still counts."""
    application = profiles().application
    content, scenes = _workshop(2)
    named = list(scenes)
    if case in ("an-unsubscribed-item", "control-an-older-service-and-an-unsubscribed-item"):
        named.append("2099999999")
    elif case == "a-preset-whose-base-isnt-installed":
        _preset(content, "2100000000", "2099999999")
        named.append("2100000000")
    elif case == "a-library-that-isnt-mounted":
        steam = content.parents[3]  # .../Steam
        library = steam.parent / "SecondDisk"
        index = steam / "steamapps" / "libraryfolders.vdf"
        index.write_text(f'"libraryfolders"\n{{\n "1"\n {{\n  "path" "{library}"\n }}\n}}\n')
        named.append("2099999998")  # on that disk, which isn't there now
    elif case == "control-a-web-page":
        named.append(_web_page(2))
    else:
        assert case == "control-a-preset-of-a-web-page", case
        _preset(content, "2100000000", _web_page(2))
        named.append("2100000000")
    text = _naming(named)
    _on_disk(text)
    older = case == "control-an-older-service-and-an-unsubscribed-item"
    _heard(monkeypatch, HEARD["an-older-service" if older else "nothing"])
    application._web_off_on_disk_at_exit()
    now = paths.runtime_config_path().read_text(encoding="utf-8")
    if case.startswith("control"):
        assert now == _nothing_to_play(), f"{case}: left"
        return
    assert now == text, f"{case}: replaced, with no status heard and no web page confirmed"


# -- L2's residual: an older service still playing a web page is told to quit --------------------


class _FakeService:
    """A wallpaper service's socket, in a thread: what it is sent, and what it saw of each.

    ``hangs``: it reads the request and never answers; it then waits for the app to close the
    connection (``closed``), or for ``release``. ``on_request`` runs as each request arrives.
    """

    def __init__(self, address: Any, *, hangs: bool, on_request: Callable[[], None]) -> None:
        self.verbs: list[str] = []
        self.order: list[str] = []
        self.closed = threading.Event()
        self.release = threading.Event()
        self._stop = threading.Event()
        self._hangs = hangs
        self._on_request = on_request
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(address))
        self._listener.listen(4)
        self._listener.settimeout(0.05)
        self._thread = threading.Thread(target=self._serve, name="test-fake-service")
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(GIVE_UP)
                line = b""
                while not line.endswith(b"\n"):
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    line += chunk
                self.verbs.append(Request.decode(line).verb)
                self.order.append("request")
                self._on_request()
                if not self._hangs:
                    connection.sendall(Response.success("quitting").encode())
                    continue
                try:
                    if connection.recv(4096) == b"":
                        self.order.append("closed by the app")
                        self.closed.set()
                except OSError:
                    self.order.append("closed by the app")
                    self.closed.set()
                self.release.wait(GIVE_UP)

    def stop(self) -> None:
        self.release.set()
        self._stop.set()
        self._thread.join(GIVE_UP)
        self._listener.close()


@contextmanager
def _service_socket(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hangs: bool = False,
    on_request: Callable[[], None] = lambda: None,
) -> Iterator[_FakeService]:
    """A fake wallpaper service listening where the app looks for it (a short path in /tmp)."""
    with tempfile.TemporaryDirectory(prefix="wio-quit-") as directory:
        address = os.path.join(directory, "runtime.sock")
        monkeypatch.setattr(client, "send_runtime", REAL_SEND_RUNTIME)
        monkeypatch.setattr(paths, "runtime_socket_path", lambda: Path(address))
        service = _FakeService(address, hangs=hangs, on_request=on_request)
        try:
            yield service
        finally:
            service.stop()


@pytest.mark.parametrize(
    ("heard", "names"),
    [
        ("an-older-service", "a-web-page"),
        ("an-older-service", "a-preset-of-a-web-page"),
        ("control-reads-eight", "a-web-page"),
        ("nothing", "a-web-page"),
        ("an-older-service", "control-only-scenes"),
        ("an-older-service", "control-only-an-unsubscribed-item"),
    ],
)
def test_an_older_service_heard_while_the_file_names_a_web_page_is_told_to_quit(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    heard: str,
    names: str,
) -> None:
    """A service older than schema 8 was heard, web wallpapers are off, and the file names a web
    page its project.json confirms: the service can't load the document that plays nothing, and
    plays the page it holds. So it is sent ``quit`` (its next start runs the updated start
    check). Before (a848d28): nothing was sent. Never in any other case (the controls): a
    service that reads 8, none heard, a file naming only scenes, or only an item that can't be
    checked (replaced, but not confirmed a page)."""
    application = profiles().application
    content, scenes = _workshop(2)
    if names == "control-only-scenes":
        named = scenes
    elif names == "control-only-an-unsubscribed-item":
        named = [*scenes, "2099999999"]
    elif names == "a-preset-of-a-web-page":
        _preset(content, "2100000000", _web_page(2))
        named = [*scenes, "2100000000"]
    else:
        named = [*scenes, _web_page(2)]
    text = _naming(named)
    _on_disk(text)
    _heard(monkeypatch, HEARD[heard])
    caplog.set_level(logging.WARNING, logger=app_module.LOGGER.name)
    with _service_socket(monkeypatch) as service:
        application._web_off_on_disk_at_exit()
        _judged_at_exit(application)
    sent = heard == "an-older-service" and names not in (
        "control-only-scenes",
        "control-only-an-unsubscribed-item",
    )
    if not sent:
        assert service.verbs == [], f"{heard}, {names}: the service was sent {service.verbs}"
        return
    assert service.verbs == ["quit"], "the older service playing a web page wasn't told to quit"
    assert paths.runtime_config_path().read_text(encoding="utf-8") == _nothing_to_play()
    assert "was sent quit at shutdown" in caplog.text and "it is quitting" in caplog.text


def _older_service_playing_a_web_page(application: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _content, scenes = _workshop(1)
    _on_disk(_naming([*scenes, _web_page(1)]))
    _heard(monkeypatch, HEARD["an-older-service"])


def test_a_hung_service_cant_hold_the_quit_past_its_bound(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The older service reads ``quit`` and never answers. The step stops waiting at its bound
    (1 s), closes the connection, and returns, while the service is still holding it; the
    thread that sent it is a daemon, so the process's exit doesn't wait for it either. Before
    (a848d28): nothing was sent."""
    application = profiles().application
    _older_service_playing_a_web_page(application, monkeypatch)
    caplog.set_level(logging.WARNING, logger=app_module.LOGGER.name)
    with _service_socket(monkeypatch, hangs=True) as service:
        application._web_off_on_disk_at_exit()
        service.order.append("step returned")
        assert service.verbs == ["quit"], "the older service playing a web page wasn't told to quit"
        senders = [
            thread
            for thread in threading.enumerate()
            if thread.name == getattr(app_module, "SHUTDOWN_LOOK_THREAD", "wio-shutdown-look")
        ]
        assert service.closed.wait(GIVE_UP), "the app left the connection open"
        order = list(service.order)
    assert service.verbs == ["quit"], "the older service playing a web page wasn't told to quit"
    assert order[0] == "request" and "step returned" in order, order
    assert all(thread.daemon for thread in senders), "a thread that could hold the exit"
    # The wait's bound and the request's own timeout are both 1 s: whichever ends it first.
    assert "didn\u2019t answer within 1 s" in caplog.text or "timed out after 1s" in caplog.text, (
        caplog.text
    )


def test_a_second_ctrl_c_cuts_the_quit(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The same hung service, with the bound put out of reach: only a second Ctrl-C (a real
    SIGINT, sent once the service has the request) can end the wait. The step returns, the
    connection is closed, and the service is still holding it. Before (a848d28): nothing was
    sent."""
    application = profiles().application
    _older_service_playing_a_web_page(application, monkeypatch)
    # Not there at a848d28, which this test is also run against.
    monkeypatch.setattr(app_module, "SHUTDOWN_OLDER_SERVICE_QUIT_SECONDS", GIVE_UP, raising=False)
    caplog.set_level(logging.WARNING, logger=app_module.LOGGER.name)
    with _service_socket(
        monkeypatch, hangs=True, on_request=lambda: os.kill(os.getpid(), signal.SIGINT)
    ) as service:
        application._web_off_on_disk_at_exit()
        service.order.append("step returned")
        assert service.verbs == ["quit"], "the older service playing a web page wasn't told to quit"
        assert service.closed.wait(GIVE_UP), "the app left the connection open"
    assert "a second Ctrl-C ended the wait for its answer" in caplog.text, caplog.text


def test_control_no_quit_once_a_second_ctrl_c_has_come(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second Ctrl-C before the step: the file is still replaced (the lock is tried), but no
    socket call is begun."""
    application = profiles().application
    _older_service_playing_a_web_page(application, monkeypatch)
    application._settle_cut = True
    with _service_socket(monkeypatch) as service:
        application._web_off_on_disk_at_exit()
        _judged_at_exit(application)
    assert service.verbs == []
    assert paths.runtime_config_path().read_text(encoding="utf-8") == _nothing_to_play()


# -- after review r13g: what a cut look raises, and the look's one bound -------------------------


class _LookError(Exception):
    """What the held look raises (this test)."""


@pytest.mark.parametrize("variant", ["after-the-cut", "control-within-the-wait"])
def test_a_look_that_fails_once_its_wait_was_cut_is_logged(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    variant: str,
) -> None:
    """An older service heard, and a file naming scenes, whose look at the Workshop items
    (`workshop.web_among`) is held, then fails. Past the look's bound (put behind it here) the
    step stops waiting: the ids count as web pages, the file is replaced, and the failure, once
    the look is let go, is logged. Before: it landed on a future nothing read, and nothing said
    it. The control: the look fails within the wait; the step's own handler logs it, once, and
    nothing else does."""
    application = profiles().application
    _content, scenes = _workshop(2)
    text = _naming(scenes)
    _on_disk(text)
    _heard(monkeypatch, HEARD["an-older-service"])
    cut = variant == "after-the-cut"
    monkeypatch.setattr(app_module, "SHUTDOWN_WORKSHOP_LOOK_SECONDS", -1.0 if cut else GIVE_UP)
    began, release = threading.Event(), threading.Event()
    looks: list[threading.Thread] = []

    def failing(_ids: Any, _give_up: Any = None) -> tuple[bool, bool]:
        looks.append(threading.current_thread())
        began.set()
        release.wait(GIVE_UP)
        raise _LookError("the look failed (this test)")

    monkeypatch.setattr(workshop, "web_among", failing)
    caplog.set_level(logging.WARNING, logger=app_module.LOGGER.name)
    if not cut:
        release.set()
    try:
        application._web_off_on_disk_at_exit()
    finally:
        release.set()
    assert began.wait(GIVE_UP), "the look never began"
    for look in looks:
        look.join(GIVE_UP)
        assert not look.is_alive(), "the look never ended"
    _judged_at_exit(application)
    said = [
        record.getMessage()
        for record in caplog.records
        if record.exc_info is not None and isinstance(record.exc_info[1], _LookError)
    ]
    if not cut:
        assert len(said) == 1 and "could not be checked" in said[0], said
        return
    assert paths.runtime_config_path().read_text(encoding="utf-8") == _nothing_to_play()
    assert len(said) == 1, f"a look that failed once its wait was cut was logged {len(said)} times"


def _held_parse(
    monkeypatch: pytest.MonkeyPatch, name: str, text: str, order: list[str]
) -> tuple[threading.Event, threading.Event]:
    """`runtime_config`'s ``name`` (a parse) held for ``text``, standing in for a large one, until
    the test lets it go or `SLOW_CALL_SECONDS`; it says so in ``order``. The Events: let it go,
    it returned."""
    parse = getattr(runtime_config, name)
    release, ended = threading.Event(), threading.Event()

    def held(*arguments: Any, **keywords: Any) -> Any:
        if text in (*arguments, keywords.get("text")):
            release.wait(SLOW_CALL_SECONDS)
            order.append("the parse returned")
            ended.set()
        return parse(*arguments, **keywords)

    monkeypatch.setattr(runtime_config, name, held)
    return release, ended


@pytest.mark.parametrize(
    "parse",
    ["whether-it-allows", "what-it-names", "control-whether-it-allows", "control-what-it-names"],
)
def test_the_looks_bound_ends_the_wait_for_a_parse(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    parse: str,
) -> None:
    """A hand edit only a parse can judge, whose parse is held (`_held_parse`), and no Ctrl-C.
    The look's bound (0.5 s here, well short of the hold) ends the wait for it, as it ends the
    look at the Workshop items, and fails closed as that does: ``[whether-it-allows]``
    (`runtime_config.allows_web_wallpapers`, a service that reads 8 heard) counts as allowing
    web wallpapers, and ``[what-it-names]`` (`runtime_config.named_scenes`, an older service
    heard) as naming what can't be checked. Either file is replaced. Before: only a second
    Ctrl-C ended either wait, and the step returned once the parse did, leaving the file. The
    controls: nothing held; the parse ends within the bound, and the file, which allows none
    and names only scenes, is left."""
    application = profiles().application
    _content, scenes = _workshop(2)
    if parse.endswith("what-it-names"):
        text = _naming([]) + "".join(f'[[playlists.entries]]\nscene_id="{one}"\n' for one in scenes)
        name, heard = "named_scenes", "an-older-service"
    else:
        text, name = document("control-off")[0], "allows_web_wallpapers"
        heard = "control-reads-eight"
    _on_disk(text)
    _heard(monkeypatch, HEARD[heard])
    monkeypatch.setattr(app_module, "SHUTDOWN_WORKSHOP_LOOK_SECONDS", 0.5)
    control = parse.startswith("control")
    order: list[str] = []
    release, ended = _held_parse(monkeypatch, name, text, order)
    if control:
        release.set()
    start = time.monotonic()
    try:
        application._web_off_on_disk_at_exit()
        took = time.monotonic() - start
        order.append("the step returned")
    finally:
        release.set()
        ended.wait(GIVE_UP)
    _judged_at_exit(application)
    now = paths.runtime_config_path().read_text(encoding="utf-8")
    if control:
        assert order == ["the parse returned", "the step returned"], order
        assert now == text, "a file that allows no web wallpaper and names only scenes was replaced"
        return
    assert order == ["the step returned", "the parse returned"], (
        f"the step waited for the parse: it took {took:.2f} s, past its 0.5 s bound ({order})"
    )
    assert now == _nothing_to_play(), "a file whose parse the step couldn't finish was left"


@pytest.mark.parametrize("variant", ["held", "control-not-held"])
def test_the_looks_bound_ends_the_wait_for_a_parse_in_the_later_judgement(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    """The same, in the judgement made once a turn-off still being written has landed
    (`_web_off_judged_once_landed`), which nothing else ends: no Ctrl-C does there. A second
    Ctrl-C ended the step's wait for the save, which then lands off, with a hand edit on disk
    that only a parse can judge, held. The look's bound ends that wait too, and the file counts
    as allowing web wallpapers: replaced. Before: the judgement waited for the parse, however
    long, and the file was left. The control: nothing held; the parse ends, and the file, which
    allows none, is left."""
    application = profiles().application
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    text = document("control-off")[0]
    _on_disk(text)
    config.update({"allow_web_wallpapers": True})
    _heard(monkeypatch, HEARD["control-reads-eight"])
    monkeypatch.setattr(app_module, "SHUTDOWN_WORKSHOP_LOOK_SECONDS", 0.5)
    order: list[str] = []
    release, ended = _held_parse(monkeypatch, "allows_web_wallpapers", text, order)
    if variant == "control-not-held":
        release.set()
    saving = _turn_off_being_written(application)
    application._settle_cut = True  # a second Ctrl-C: the save isn't waited for
    try:
        application._web_off_on_disk_at_exit()
        assert not order, "the step itself parsed the file, with web wallpapers still on"
        config.update({"allow_web_wallpapers": False})
        saving.set_result(None)
        _judged_at_exit(application)
        order.append("judged")
    finally:
        release.set()
        if not saving.done():
            saving.set_result(None)
        ended.wait(GIVE_UP)
        _judged_at_exit(application)
    now = paths.runtime_config_path().read_text(encoding="utf-8")
    if variant == "control-not-held":
        assert order == ["the parse returned", "judged"], order
        assert now == text, "a file that allows no web wallpaper was replaced"
        return
    assert order == ["judged", "the parse returned"], (
        f"the judgement waited for the parse ({order})"
    )
    assert now == _nothing_to_play(), "a file whose parse the judgement couldn't finish was left"
