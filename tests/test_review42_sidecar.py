"""Codex review 0.4.2: Move to Trash and an ignored sidecar (R42-2); files are private."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_manage import _trash, item_for
from wall_in_one.library import pairing
from wall_in_one.library.model import Kind


@pytest.mark.parametrize("trusted", [False, True], ids=["ignored-outside-root", "trusted-control"])
def test_trash_preserves_an_ignored_pairing_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted: bool
) -> None:
    """An ignored selector is not app-owned; a trusted one remains normal cleanup.

    The user's video is moved by the real managed trash implementation, not
    Gio or the desktop trash service. The representative image is never
    removed in either variant. Only the selector's ownership is at issue.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "private-data"))
    root = tmp_path / "library"
    root.mkdir()
    video = root / "clip.mp4"
    source = b"private fixture video" * 4
    video.write_bytes(source)
    image_directory = root if trusted else tmp_path / "personal"
    image_directory.mkdir(exist_ok=True)
    selected = image_directory / "chosen.png"
    selected.write_bytes(b"\x89PNG\r\n\x1a\n")
    selector = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    contents = (json.dumps({"still_path": str(selected)}) + "\n").encode()
    selector.write_bytes(contents)

    assert pairing.read_sidecar(video, (root,)) == (selected if trusted else None)
    result = _trash(item_for(video, Kind.VIDEO), (root,))

    assert not video.exists()
    assert result.destination.read_bytes() == source
    assert selected.is_file(), "the named picture is never a deletion authority"
    if trusted:
        assert not selector.exists(), "the trusted selector is ordinary app cleanup"
    else:
        assert selector.exists(), "Move to Trash permanently deleted an ignored selector"
        assert selector.read_bytes() == contents
