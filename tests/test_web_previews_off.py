"""While web wallpapers are off, nothing of one is opened but its project.json.

Owner: "it shouldn't load the papers to make a preview if web wallpapers are
off." A web wallpaper (or a preset of one) is listed, but no thumbnail, still,
colour or engine screenshot is made from its folder: not its preview, not its
page, not anything else in it. Turned on, they are made as usual; turned off
again, whatever is still queued or running for one is dropped.

The web_folder is a Steam Workshop folder built in ``tmp_path``. Every file in it
but ``project.json`` is mode 000, so a read would fail loudly, and an audit hook
records every ``open`` below those folders and every ffmpeg or
linux-wallpaperengine start, so a swallowed failure is seen too.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.web_previews import PNG, Fixture, _Watch
from tests.web_previews import both as _both
from wall_in_one import thumbnails, worker_processes
from wall_in_one.library import stills, workshop
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.wallpaper import outputs, scenes

# -- thumbnails ---------------------------------------------------------------------


def test_no_thumbnail_is_made_or_looked_up_for_a_web_page_while_web_is_off(
    web_folder: Fixture, watch: _Watch
) -> None:
    for item in _both(web_folder):
        assert thumbnails.lookup(item) is None
        with pytest.raises(thumbnails.ThumbnailError):
            thumbnails.generate(item)
        with pytest.raises(thumbnails.ThumbnailError):
            thumbnails.generate(item, force=True)
    assert watch.clean(), (watch.opens, watch.started)
    assert not web_folder.cache.exists()


def test_a_thumbnail_made_while_web_was_on_is_not_shown_while_it_is_off(
    web_folder: Fixture, watch: _Watch
) -> None:
    """The cached picture is derived from the wallpaper's files: the placeholder wins."""
    cached = thumbnails.cached_path(web_folder.web)
    cached.parent.mkdir(parents=True)
    cached.write_bytes(PNG)
    assert thumbnails.lookup(web_folder.web, allow_web=True) == cached
    assert thumbnails.lookup(web_folder.web) is None
    assert thumbnails.lookup(web_folder.web, allow_web=False) is None
    with pytest.raises(thumbnails.ThumbnailError):
        thumbnails.generate(web_folder.web)
    # A true scene is not a web page: it is looked up as ever.
    assert thumbnails.lookup(web_folder.scene) is None  # nothing cached for it, and no error


def _decoded_inodes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The inode each faked ffmpeg was handed as its input (`decoding.opened`'s descriptor)."""
    decoded: list[int] = []

    def fake_run(argv: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        decoded.append(Path(argv[argv.index("-i") + 1]).stat().st_ino)
        Path(argv[-1]).write_bytes(PNG)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return decoded


def test_a_thumbnail_is_made_when_web_is_on(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoded = _decoded_inodes(monkeypatch)
    preview = web_folder.web.preview
    assert preview is not None
    preview.chmod(0o600)  # web is on: reading it is the point
    made = thumbnails.generate(web_folder.web, allow_web=True)
    assert made.is_file() and thumbnails.lookup(web_folder.web, allow_web=True) == made
    assert decoded == [preview.stat().st_ino], "ffmpeg is handed the preview itself, opened"


# -- stills -------------------------------------------------------------------------


def test_no_still_is_taken_for_a_web_page_while_web_is_off(
    web_folder: Fixture, watch: _Watch, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*_arguments: object, **_options: object) -> None:
        raise AssertionError("the engine was asked for a web page")

    monkeypatch.setattr(scenes, "screenshot", never)
    for item in _both(web_folder):
        assert stills.ensure(item, web_folder.root) is None
        with pytest.raises(stills.StillError):
            stills.capture_scene(item, web_folder.root, force=True, size=(1920, 1080))
        with pytest.raises(stills.StillError):
            stills.capture_scene(item, web_folder.root, force=True, allow_web=False)
    assert watch.clean(), (watch.opens, watch.started)
    assert list((web_folder.root / "Wall-in-One" / "Automatic Stills").iterdir()) == []


def test_no_still_is_taken_for_a_web_page_or_preset_even_when_web_is_on(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(*_arguments: object, **_options: object) -> None:
        raise AssertionError("a still was made for a web page or a preset")

    monkeypatch.setattr(scenes, "screenshot", never)
    monkeypatch.setattr(subprocess, "run", never)
    assert stills.ensure(web_folder.web, web_folder.root, allow_web=True) is None
    with pytest.raises(stills.StillError):
        stills.capture_scene(web_folder.web, web_folder.root, force=True, allow_web=True)


# -- colours ------------------------------------------------------------------------


def test_no_colours_are_taken_from_a_web_pages_still_while_web_is_off(
    web_folder: Fixture,
) -> None:
    from wall_in_one.ui.next.real_editing import RealEditing

    still = web_folder.root / "Wall-in-One" / "Automatic Stills" / "300.png"
    still.write_bytes(PNG)
    allowed = {"on": False}
    session = SimpleNamespace(
        library=SimpleNamespace(find=lambda path: web_folder.web),
        pairings=SimpleNamespace(
            resolve_accepted=lambda item, library: SimpleNamespace(still=still)
        ),
        settings=SimpleNamespace(allow_web_wallpapers=False),
    )
    editing = SimpleNamespace(_session=session)
    assert RealEditing._representative(editing, web_folder.web) is None  # type: ignore[arg-type]
    assert RealEditing._representative(editing, web_folder.preset) is None  # type: ignore[arg-type]
    session.settings.allow_web_wallpapers = True
    allowed["on"] = True
    assert RealEditing._representative(editing, web_folder.web) == still  # type: ignore[arg-type]


# -- the cancellation ---------------------------------------------------------------


def test_a_gated_cancellation_ends_exactly_the_child_it_started(tmp_path: Path) -> None:
    allowed = threading.Event()
    allowed.set()
    parent = worker_processes.Cancellation()
    gated = worker_processes.GatedCancellation(parent, allowed.is_set)
    pids: list[int] = []
    real_spawn = worker_processes.spawn

    def spawn(*arguments: Any, **options: Any) -> Any:
        child, selector = real_spawn(*arguments, **options)
        pids.append(child.process.pid)
        return child, selector

    outcome: list[object] = []

    def work() -> None:
        try:
            gated.run(["sleep", "30"], timeout=60)
        except worker_processes.ProcessCancelledError as error:
            outcome.append(error)

    original = worker_processes.spawn
    worker_processes.spawn = spawn
    try:
        thread = threading.Thread(target=work)
        thread.start()
        deadline = time.monotonic() + 5
        while not pids and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pids
        allowed.clear()
        thread.join(10)
    finally:
        worker_processes.spawn = original
    assert not thread.is_alive() and outcome
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)
    assert not parent.cancelled()  # the shared owner carries on for the other jobs
    with pytest.raises(worker_processes.ProcessCancelledError):
        gated.run(["sleep", "30"], timeout=60)  # and nothing new starts while off


# -- the classic loader and the still maker -----------------------------------------


def _pump(until: Callable[[], bool], seconds: float = 10.0) -> None:
    from gi.repository import GLib

    deadline = time.monotonic() + seconds
    context = GLib.MainContext.default()
    while not until() and time.monotonic() < deadline:
        while context.iteration(False):
            pass
        time.sleep(0.005)


def test_the_loader_answers_a_web_page_with_nothing_while_web_is_off(
    web_folder: Fixture, watch: _Watch
) -> None:
    from wall_in_one.ui.thumbnails import ThumbnailLoader

    def asked(loader: ThumbnailLoader) -> list[object]:
        got: list[object] = []

        def deliver(_item: MediaItem, texture: object) -> None:
            got.append(texture)

        loader.request(web_folder.web, deliver)
        loader.request(web_folder.preset, deliver)
        _pump(lambda: len(got) == 2)
        loader.shutdown()
        return got

    assert asked(ThumbnailLoader()) == [None, None]
    assert asked(ThumbnailLoader(web_allowed=lambda: False)) == [None, None]
    assert watch.clean(), (watch.opens, watch.started)


def test_turning_web_off_ends_a_running_thumbnail_and_discards_it(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one.ui.thumbnails import ThumbnailLoader

    web = {"on": True}
    running = threading.Event()
    pids: list[int] = []

    def slow_generate(
        item: MediaItem,
        *,
        force: bool = False,
        processes: worker_processes.Cancellation | None = None,
        allow_web: bool = False,
    ) -> Path:
        assert processes is not None and allow_web
        real_spawn = worker_processes.spawn

        def spawn(*arguments: Any, **options: Any) -> Any:
            child, selector = real_spawn(*arguments, **options)
            pids.append(child.process.pid)
            running.set()
            return child, selector

        monkeypatch.setattr(worker_processes, "spawn", spawn)
        try:
            processes.run(["sleep", "30"], timeout=60)
        except worker_processes.ProcessCancelledError as error:
            raise thumbnails.ThumbnailError("cancelled") from error
        raise AssertionError("the child finished")

    monkeypatch.setattr(thumbnails, "generate", slow_generate)
    loader = ThumbnailLoader(web_allowed=lambda: web["on"])
    got: list[object] = []
    loader.request(web_folder.web, lambda item, texture: got.append(texture))
    assert running.wait(10)
    web["on"] = False
    _pump(lambda: bool(got))
    loader.shutdown()
    assert got == [None]
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


def test_the_loader_makes_a_web_pages_thumbnail_when_web_is_on(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one.ui.thumbnails import ThumbnailLoader

    asked: list[tuple[MediaItem, bool]] = []

    def fake_generate(item: MediaItem, **options: Any) -> Path:
        asked.append((item, options.get("allow_web", False)))
        raise thumbnails.ThumbnailError("not here")

    monkeypatch.setattr(thumbnails, "generate", fake_generate)
    loader = ThumbnailLoader(web_allowed=lambda: True)
    got: list[object] = []
    loader.request(web_folder.web, lambda item, texture: got.append(texture))
    _pump(lambda: bool(got))
    loader.shutdown()
    assert asked == [(web_folder.web, True)]


def test_the_still_maker_queues_nothing_for_a_web_page_or_preset_whether_web_is_off_or_on(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one.ui.stills import StillMaker

    web = {"on": False}
    maker = StillMaker(web_allowed=lambda: web["on"])
    submitted: list[tuple[MediaItem, ...]] = []
    monkeypatch.setattr(
        maker, "_submit", lambda work, items, *rest, context: submitted.append(items)
    )
    monkeypatch.setattr(maker, "_local_displays", lambda: ())
    maker.request(_both(web_folder), web_folder.root, lambda made: None)
    assert submitted == []
    web["on"] = True
    maker.request(_both(web_folder), web_folder.root, lambda made: None)
    # Nothing to make for either: a web page and a preset get no automatic
    # still, with web on or off.
    assert submitted == []
    maker.shutdown()


def test_the_still_maker_drops_a_web_page_that_was_queued_before_web_went_off(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one.ui.stills import StillMaker

    web = {"on": True}
    # Previews on: a web page gets an automatic still only from its preview, so
    # the web setting is what this is about.
    maker = StillMaker(web_allowed=lambda: web["on"], previews=lambda: True)
    ensured: list[MediaItem] = []

    def ensure(item: MediaItem, root: Path, **options: object) -> None:
        ensured.append(item)

    monkeypatch.setattr(stills, "ensure", ensure)
    monkeypatch.setattr(scenes, "measured_capture_size", lambda *_a: (1920, 1080))
    web["on"] = False  # between the queueing and the worker reaching it
    maker._attempted.add(web_folder.web.path)
    maker._run(_both(web_folder), web_folder.root, lambda made: None)
    assert ensured == []
    assert web_folder.web.path not in maker._attempted  # so turning web on makes it
    web["on"] = True
    maker._run((web_folder.web,), web_folder.root, lambda made: None)
    assert ensured == [web_folder.web]
    maker.shutdown()


def test_a_capture_cancelled_by_web_going_off_is_no_failure_and_is_tried_again(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review f5 N2: the engine ended because web went off, not because it gave no still."""
    from wall_in_one.ui.stills import StillMaker

    web = {"on": True}
    messages: list[str] = []
    maker = StillMaker(web_allowed=lambda: web["on"], report=messages.append)
    monkeypatch.setattr(
        "wall_in_one.ui.stills.GLib.idle_add",
        lambda callback, *arguments, **_keywords: callback(*arguments),
    )
    monkeypatch.setattr(scenes, "measured_capture_size", lambda *_a: (1920, 1080))
    monkeypatch.setattr(scenes, "is_available", lambda: True)
    monkeypatch.setattr(workshop, "engine_refusal", lambda *_a, **_k: "")
    asked: list[Path] = []

    def cancelled(scene: object, *_a: object, **_k: object) -> None:
        asked.append(folder)
        web["on"] = False  # the gate turns false; the engine is ended, the wait gives up
        raise scenes.SceneCancelledError("scene screenshot was cancelled")

    monkeypatch.setattr(scenes, "screenshot", cancelled)
    # A scene whose folder may hold a web page (`holds_web`) is captured with web on only.
    folder = web_folder.root / "700"
    folder.mkdir()
    item = MediaItem(folder, Kind.SCENE, 1, 0, scene="700", web_files=True)
    assert item.holds_web and not item.preview_kind
    maker._attempted.add(item.path)
    maker._run((item,), web_folder.root, lambda made: None)
    assert asked, "the capture did not reach the engine"
    assert messages == [], messages
    assert item.path not in maker._attempted  # so turning web on makes it
    maker.shutdown()


@pytest.mark.parametrize("when", ["mid-capture", "before-start", "decoder-fails"])
def test_a_regenerated_still_cancelled_by_web_going_off_is_no_failure_and_is_tried_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    """R42-13: the still a person asks for again is held to N2's rule too.

    Web turned off while its decoder runs (here, as it would start) is no failure to say,
    and it is not attempted, so turning web on makes it. On 534a9da it said "Could not
    regenerate" and stayed attempted. A decoder that fails is still said, and attempted.
    No decoder is started: the decoder boundary is replaced.
    """
    from tests.test_scene_preset_stills import WEB, Tree
    from wall_in_one.ui import stills as ui_stills

    tree = Tree(tmp_path)
    (tree.automatic / f"{WEB}.png").unlink()
    item = tree.scan(previews=True)[WEB]
    assert item.holds_web and item.paired_still is None
    web = {"on": True}
    messages: list[str] = []
    queued: list[tuple[Callable[..., None], tuple[Any, ...], str]] = []
    decoded: list[list[str]] = []
    maker = ui_stills.StillMaker(
        web_allowed=lambda: web["on"],
        previews=lambda: True,
        displays=lambda: (),
        report=messages.append,
    )

    def queue(work: Callable[..., None], *arguments: Any, context: str) -> None:
        queued.append((work, arguments, context))

    def decoder(
        _processes: object, argv: list[str], **_options: object
    ) -> subprocess.CompletedProcess[bytes]:
        decoded.append(argv)
        if when == "decoder-fails":
            return subprocess.CompletedProcess(argv, 1, b"", b"the fixture decoder failed")
        web["on"] = False  # the gate closes as the decoder starts: it is not started
        raise worker_processes.ProcessCancelledError("web wallpapers went off")

    monkeypatch.setattr(maker, "_submit", queue)
    monkeypatch.setattr(outputs, "discover", lambda *_a, **_k: ())
    monkeypatch.setattr(scenes, "measured_capture_size", lambda *_a, **_k: (64, 64))
    monkeypatch.setattr(stills, "is_available", lambda: True)
    monkeypatch.setattr(worker_processes.GatedCancellation, "run", decoder)
    monkeypatch.setattr(
        "wall_in_one.ui.stills.GLib.idle_add",
        lambda callback, *arguments, **_k: callback(*arguments),
    )
    try:
        maker.regenerate_scene(item, tree.root, lambda _made: None)
        assert len(queued) == 1
        if when == "before-start":
            web["on"] = False
        work, arguments, context = queued[0]
        done: Future[None] = Future()
        try:
            work(*arguments)
        except stills.StillError as error:
            done.set_exception(error)
        else:
            done.set_result(None)
        maker._finished(done, context)

        if when == "decoder-fails":
            assert decoded and len(messages) == 1, messages
            assert "the fixture decoder failed" in messages[0]
            assert item.path in maker._attempted
        else:
            assert bool(decoded) is (when == "mid-capture")
            assert messages == [], messages
            assert item.path not in maker._attempted
            web["on"] = True
            maker.request((item,), tree.root, lambda _made: None)
            assert len(queued) == 2, "turning web on asks for it again"
    finally:
        maker.shutdown()


@pytest.mark.parametrize("when", ["off-before-it-starts", "off-as-it-starts", "decoder-fails"])
def test_a_regenerated_still_withdrawn_by_the_preview_setting_is_no_failure_and_is_tried_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    """f7 R42-19: R42-13's rule for the preview setting. Turned off after a page's still was
    asked for again, the queued capture refused it as a failure ("Could not regenerate"), and
    the page stayed attempted, so turning the setting back on asked for nothing (on c8b2fddb).
    The worker withdraws it quietly, unattempted, as the automatic one does; the setting is
    read once there, so one turned off as it starts is no failure either. A decoder that fails
    is still said, and attempted. No decoder is started: its boundary is replaced."""
    from tests.test_scene_preset_stills import WEB, Tree
    from wall_in_one.ui import stills as ui_stills

    tree = Tree(tmp_path)
    (tree.automatic / f"{WEB}.png").unlink()
    item = tree.scan(previews=True)[WEB]
    assert item.preview_kind and item.paired_still is None
    previews = {"on": True}
    asked: list[bool] = []
    messages: list[str] = []
    queued: list[tuple[Callable[..., None], tuple[Any, ...], str]] = []
    made: list[int] = []

    def setting() -> bool:
        asked.append(previews["on"])
        if when == "off-as-it-starts" and len(asked) == 2:
            previews["on"] = False  # read on the worker, then turned off
        return asked[-1]

    maker = ui_stills.StillMaker(
        web_allowed=lambda: True, previews=setting, displays=lambda: (), report=messages.append
    )

    def queue(work: Callable[..., None], *arguments: Any, context: str) -> None:
        queued.append((work, arguments, context))

    def decoder(_source: Path, output: Path, _seek: float, **_options: object) -> str:
        if when == "decoder-fails":
            return "the fixture decoder failed"
        output.write_bytes(PNG)
        return ""

    monkeypatch.setattr(maker, "_submit", queue)
    monkeypatch.setattr(outputs, "discover", lambda *_a, **_k: ())
    monkeypatch.setattr(scenes, "measured_capture_size", lambda *_a, **_k: (64, 64))
    monkeypatch.setattr(stills, "is_available", lambda: True)
    monkeypatch.setattr(stills, "_run", decoder)
    monkeypatch.setattr(
        "wall_in_one.ui.stills.GLib.idle_add",
        lambda callback, *arguments, **_k: callback(*arguments),
    )
    try:
        maker.regenerate_scene(item, tree.root, made.append)
        assert len(queued) == 1 and item.path in maker._attempted
        if when == "off-before-it-starts":
            previews["on"] = False
        work, arguments, context = queued[0]
        done: Future[None] = Future()
        try:
            work(*arguments)
        except stills.StillError as error:
            done.set_exception(error)
        else:
            done.set_result(None)
        maker._finished(done, context)
        if when == "decoder-fails":
            assert len(messages) == 1 and "the fixture decoder failed" in messages[0], messages
            assert item.path in maker._attempted
        elif when == "off-as-it-starts":
            assert (messages, made) == ([], [1])
        else:
            assert (messages, made) == ([], [])
            assert item.path not in maker._attempted
            previews["on"] = True
            maker.request((item,), tree.root, lambda _made: None)
            assert len(queued) == 2, "turning the setting back on asks for it again"
    finally:
        maker.shutdown()


def test_a_refused_web_item_and_a_preset_of_nothing_get_nothing_while_web_is_off(
    web_folder: Fixture, watch: _Watch
) -> None:
    """Fail closed: a web-typed item that can't play, or a preset whose base is unknown."""
    assert web_folder.malformed.holds_web and web_folder.orphan.holds_web
    assert not web_folder.malformed.web_page  # playback's word is unchanged
    for item in (web_folder.malformed, web_folder.orphan):
        assert thumbnails.lookup(item) is None
        with pytest.raises(thumbnails.ThumbnailError):
            thumbnails.generate(item)
        assert stills.ensure(item, web_folder.root) is None
    assert watch.clean(), (watch.opens, watch.started)


def test_a_malformed_scene_still_gets_its_cant_play_preview(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoded = _decoded_inodes(monkeypatch)
    preview = web_folder.bad_scene.preview
    assert preview is not None
    preview.chmod(0o600)  # not a web page: its preview is read
    made = thumbnails.generate(web_folder.bad_scene)
    assert made.is_file() and decoded == [preview.stat().st_ino]
