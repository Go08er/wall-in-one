"""Codex review 0.4.2: Move to Trash and an ignored sidecar (R42-2); files are private."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_manage import _trash, item_for
from wall_in_one import file_io
from wall_in_one.library import manage, pairing
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


# -- R42-11: the generation judged is the generation taken ----------------------------------------


def _selector_case(tmp_path: Path, foreign_first: bool) -> tuple[Path, Path, Path, bytes]:
    """A library video whose selector names a still in the root, or (``foreign_first``) one
    outside every root. Returns the root, the video, its selector, and the foreign one's bytes."""
    root = tmp_path / "library"
    root.mkdir()
    video = root / "clip.mp4"
    video.write_bytes(b"private fixture video")
    trusted = root / "chosen.png"
    trusted.write_bytes(b"\x89PNG\r\n\x1a\n")
    foreign = tmp_path / "personal" / "chosen.png"
    foreign.parent.mkdir()
    foreign.write_bytes(b"\x89PNG\r\n\x1a\n")
    selector = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    owned = (json.dumps({"still_path": str(trusted)}) + "\n").encode()
    other = (json.dumps({"still_path": str(foreign)}) + "\n").encode()
    selector.write_bytes(other if foreign_first else owned)
    return root, video, selector, other


def _replaced_once_judged(
    monkeypatch: pytest.MonkeyPatch, selector: Path, replacement: bytes
) -> list[int]:
    """Once the selector is judged the app's own, another writer replaces it, atomically.

    The judgement's last step is `pairing._trusted_location`, after the selector was read;
    the removal pins its companions after that. Returns the replaced inodes.
    """
    original = pairing._trusted_location
    replaced: list[int] = []

    def then_replaced(video: Path, still: Path, roots: tuple[Path, ...]) -> bool:
        answer = original(video, still, roots)
        if answer and not replaced:
            before = selector.stat().st_ino
            written = selector.with_name(selector.name + ".new")
            written.write_bytes(replacement)
            written.replace(selector)
            assert selector.stat().st_ino != before
            replaced.append(before)
        return answer

    monkeypatch.setattr(pairing, "_trusted_location", then_replaced)
    return replaced


@pytest.mark.parametrize("case", ["replaced-once-judged", "trusted-control", "foreign-control"])
def test_trash_takes_only_the_selector_generation_it_judged_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A selector replaced between its judgement and the pin is another writer's: kept.

    On 534a9da the removal pinned whatever was at the name after it judged the old one
    trusted, and deleted the replacement, which names a picture outside every root.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "private-data"))
    root, video, selector, other = _selector_case(tmp_path, case == "foreign-control")
    replaced = (
        _replaced_once_judged(monkeypatch, selector, other)
        if case == "replaced-once-judged"
        else []
    )

    result = _trash(item_for(video, Kind.VIDEO), (root,))

    assert not video.exists() and result.destination.read_bytes() == b"private fixture video"
    if case == "trusted-control":
        assert not selector.exists(), "an unchanged trusted selector is the app's to take"
    else:
        if case == "replaced-once-judged":
            assert replaced, "the window between the judgement and the pin was reached"
        assert selector.read_bytes() == other, "Move to Trash deleted another writer's selector"


def test_a_removal_proves_no_selector_generation_it_did_not_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof a removal keeps for its replay is of the generation judged, or none."""
    root, video, selector, other = _selector_case(tmp_path, False)
    replaced = _replaced_once_judged(monkeypatch, selector, other)
    folder = video.parent.stat()
    with file_io.pin_directory_beneath(
        root,
        video.parent,
        expected_root_identity=(root.stat().st_dev, root.stat().st_ino),
        expected_directory_identity=(folder.st_dev, folder.st_ino),
    ) as context:
        proofs = manage.prove_companions(
            item_for(video, Kind.VIDEO), (root,), source_root=root, source_context=context
        )

    assert replaced, "the window between the judgement and the proof was reached"
    assert selector not in {proof.path for proof in proofs}, "a replay would delete it"
