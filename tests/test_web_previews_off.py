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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.web_previews import PNG, Fixture, _Watch
from tests.web_previews import both as _both
from wall_in_one import thumbnails, worker_processes
from wall_in_one.library import stills
from wall_in_one.library.model import MediaItem
from wall_in_one.wallpaper import scenes

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


def test_a_thumbnail_is_made_when_web_is_on(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[list[str]] = []

    def fake_run(argv: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
        ran.append(argv)
        Path(argv[-1]).write_bytes(PNG)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    made = thumbnails.generate(web_folder.web, allow_web=True)
    assert made.is_file() and thumbnails.lookup(web_folder.web, allow_web=True) == made
    assert str(web_folder.web.preview) in ran[0]


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


def test_a_still_is_taken_when_web_is_on(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken: list[MediaItem] = []

    def fake(item: MediaItem, output: Path, processes: object) -> None:
        taken.append(item)
        output.write_bytes(PNG)

    monkeypatch.setattr(stills, "_still_from_preview", fake)
    made = stills.ensure(web_folder.web, web_folder.root, allow_web=True)
    assert made is not None and made.is_file()
    assert taken == [web_folder.web]


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


def test_the_still_maker_queues_nothing_for_a_web_page_while_web_is_off_and_does_when_on(
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
    assert submitted == [_both(web_folder)]  # not remembered as attempted while off
    maker.shutdown()


def test_the_still_maker_drops_a_web_page_that_was_queued_before_web_went_off(
    web_folder: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one.ui.stills import StillMaker

    web = {"on": True}
    maker = StillMaker(web_allowed=lambda: web["on"])
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
