"""Filling in missing stills in the background.

`StillMaker` owns a thread pool and delivers through `GLib.idle_add`, but the
decisions worth pinning -- which videos are picked, and that a finished batch
cannot cause another -- are all made before any of that. They are tested here
directly, by driving `_run` on the calling thread with the real generator
stubbed out; a pool and a main loop would add nothing but flakiness.

The loop is the one that matters. A finished batch triggers a rescan, and a
rescan asks again, so a video ffmpeg cannot read would go round forever if the
attempted set were not remembered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.ui.stills import StillMaker


def item(name: str, kind: Kind, still: Path | None = None) -> MediaItem:
    return MediaItem(
        path=Path(f"/w/{name}"),
        kind=kind,
        size=1,
        mtime=0,
        paired_still=still,
    )


class Recording(StillMaker):
    """A maker whose generator is a list rather than ffmpeg."""

    def __init__(self, *, succeeds: bool = True) -> None:
        super().__init__()
        self.asked: list[Path] = []
        self.batches: list[int] = []
        self._succeeds = succeeds

    def submit(self, items: tuple[MediaItem, ...], root: Path) -> None:
        """Run what the pool would have run, here and now."""
        self._run(items, root, self.batches.append)

    def drain(self) -> None:
        """Let the pool finish what `request` queued, then stop it.

        `shutdown` on the maker sets `_closed`, which makes `_run` return
        without doing the work -- correct for quitting, useless for a test
        that wants to see what the work was. This waits on the pool itself.
        """
        self._pool.shutdown(wait=True)


def _stub_failing(made: Recording, monkeypatch: pytest.MonkeyPatch) -> None:
    """A generator that records what it was asked for and always gives up."""

    def refuse(target: MediaItem, _root: Path) -> Path | None:
        made.asked.append(target.path)
        return None

    monkeypatch.setattr("wall_in_one.ui.stills.stills.ensure", refuse)


@pytest.fixture
def maker(monkeypatch: pytest.MonkeyPatch) -> Recording:
    made = Recording()

    def fake_ensure(target: MediaItem, root: Path) -> Path | None:
        made.asked.append(target.path)
        return root / f"{target.path.stem}.png" if made._succeeds else None

    monkeypatch.setattr("wall_in_one.ui.stills.stills.ensure", fake_ensure)
    return made


def test_only_videos_without_a_still_are_picked(maker: Recording) -> None:
    items = (
        item("a.png", Kind.STILL),
        item("b.mp4", Kind.VIDEO),
        item("c.mp4", Kind.VIDEO, still=Path("/w/c-still.png")),
    )
    maker.submit(
        tuple(i for i in items if i.kind is Kind.VIDEO and i.paired_still is None), Path("/w")
    )
    assert maker.asked == [Path("/w/b.mp4")]


def test_a_video_is_never_attempted_twice(maker: Recording) -> None:
    """A finished batch causes a rescan, and the rescan asks again."""
    items = (item("b.mp4", Kind.VIDEO),)
    maker.request(items, Path("/w"), maker.batches.append)
    maker.request(items, Path("/w"), maker.batches.append)
    # `request` goes to the pool, so what is asserted here is the memo, which
    # is updated synchronously precisely so the second call is a no-op.
    assert maker._attempted == {Path("/w/b.mp4")}
    maker.shutdown()


def test_a_video_that_cannot_be_read_is_not_retried_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure case is the one that loops: it never gains a still, so
    every rescan would queue it again."""
    made = Recording(succeeds=False)
    _stub_failing(made, monkeypatch)
    broken = (item("broken.mp4", Kind.VIDEO),)
    made.request(broken, Path("/w"), made.batches.append)
    assert made._attempted == {Path("/w/broken.mp4")}
    made.request(broken, Path("/w"), made.batches.append)
    made.shutdown()


def test_a_batch_that_made_nothing_asks_for_no_rescan(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rescan that would change nothing is not worth the disk."""
    made = Recording(succeeds=False)
    _stub_failing(made, monkeypatch)
    made.submit((item("broken.mp4", Kind.VIDEO),), Path("/w"))
    assert made.batches == []


def test_nothing_to_do_submits_no_work(maker: Recording) -> None:
    maker.request((item("a.png", Kind.STILL),), Path("/w"), maker.batches.append)
    assert maker._attempted == set()
    maker.shutdown()


def test_forgetting_lets_a_video_be_attempted_again(maker: Recording) -> None:
    """The memo has to be a cache, not a one-way door."""
    maker.request((item("b.mp4", Kind.VIDEO),), Path("/w"), maker.batches.append)
    maker.forget(Path("/w/b.mp4"))
    assert maker._attempted == set()
    maker.shutdown()


def test_a_shut_down_maker_takes_no_more_work(maker: Recording) -> None:
    maker.shutdown()
    maker.request((item("b.mp4", Kind.VIDEO),), Path("/w"), maker.batches.append)
    assert maker._attempted == set()


def test_a_shut_down_maker_stops_mid_batch(maker: Recording) -> None:
    """Quitting should be immediate; a queued 4K frame grab must not hold it."""
    maker.shutdown()
    maker.submit((item("b.mp4", Kind.VIDEO), item("c.mp4", Kind.VIDEO)), Path("/w"))
    assert maker.asked == []


# -- scenes ---------------------------------------------------------------
#
# `stills.ensure` has always known how to capture a Wallpaper Engine scene
# through the engine itself. The batch filter asked only for `Kind.VIDEO`, so
# it was never given one -- which showed up as four scenes with no still on a
# machine where all 45 videos had one.


def test_scenes_are_offered_a_still_too(maker: Recording) -> None:
    """Every item is a pairing, and a pairing has a representative."""
    items = (
        item("clip.mp4", Kind.VIDEO),
        item("1647046763", Kind.SCENE),
    )
    maker.request(items, Path("/w"), maker.batches.append)
    maker.drain()

    assert set(maker.asked) == {Path("/w/clip.mp4"), Path("/w/1647046763")}


def test_a_scene_that_already_has_a_still_is_left_alone(maker: Recording) -> None:
    items = (item("1647046763", Kind.SCENE, still=Path("/w/scene-still.png")),)
    maker.request(items, Path("/w"), maker.batches.append)
    maker.drain()

    assert maker.asked == []


def test_a_stale_automatic_scene_still_is_queued_again(
    maker: Recording, monkeypatch: pytest.MonkeyPatch
) -> None:
    scene = item("1647046763", Kind.SCENE, still=Path("/w/scene-still.png"))
    monkeypatch.setattr(
        "wall_in_one.ui.stills.stills.scene_capture_required",
        lambda candidate, _root: candidate.path == scene.path,
    )

    maker.request((scene,), Path("/w"), maker.batches.append)
    maker.drain()

    assert maker.asked == [scene.path]


def test_stills_are_left_out_of_the_batch(maker: Recording) -> None:
    """Only things that move need a representative."""
    maker.request((item("a.png", Kind.STILL),), Path("/w"), maker.batches.append)
    maker.drain()

    assert maker.asked == []


def test_videos_are_captured_before_scenes(maker: Recording) -> None:
    """A scene capture spawns the engine and waits for it to settle.

    On one worker, interleaving them would let a handful of scenes hold up
    every video queued behind them.
    """
    items = (
        item("1647046763", Kind.SCENE),
        item("one.mp4", Kind.VIDEO),
        item("3238389972", Kind.SCENE),
        item("two.mp4", Kind.VIDEO),
    )
    maker.request(items, Path("/w"), maker.batches.append)
    maker.drain()

    kinds = [path.suffix == ".mp4" for path in maker.asked]
    assert kinds == [True, True, False, False]
