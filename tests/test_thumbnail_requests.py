"""Thumbnail requests for the same path settle independently when the item changed (R32-1)."""

from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from tests.gtk_helpers import spin_until
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.ui.next.library_thumbnails import LibraryThumbnails
from wall_in_one.ui.thumbnails import ThumbnailLoader

pytestmark = pytest.mark.gui


@pytest.mark.parametrize("change", ("unchanged", "metadata", "paired still"))
def test_same_path_requests_all_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    started = Event()
    release = Event()
    texture = object()

    def load(_item: MediaItem, _processes: object | None = None) -> object:
        started.set()
        assert release.wait(5)
        return texture

    monkeypatch.setattr(ThumbnailLoader, "_texture_for", staticmethod(load))
    monkeypatch.setattr("wall_in_one.ui.thumbnails.thumbnails.prune", lambda: 0)
    loader = ThumbnailLoader(max_workers=1)
    provider = LibraryThumbnails(loader)
    first = MediaItem(tmp_path / "wallpaper", Kind.SCENE, 10, 1, paired_still=tmp_path / "old.png")
    second = (
        replace(first, size=20, mtime=2)
        if change == "metadata"
        else replace(first, paired_still=tmp_path / "new.png")
        if change == "paired still"
        else first
    )
    delivered: list[str] = []
    try:
        provider.request(first, 320, 180, lambda _texture: delivered.append("first"))
        assert started.wait(2)
        provider.request(second, 320, 180, lambda _texture: delivered.append("second"))
        release.set()
        spin_until(lambda: len(delivered) == 2, 2, what="both thumbnail deliveries")
        assert provider.pending() == 0, (delivered, tuple(provider._waiting))
        assert delivered == ["first", "second"]
    finally:
        release.set()
        provider.shutdown()
        loader._pool.shutdown(wait=True)
