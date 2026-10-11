"""The proof a pending removal record keeps of the wallpaper's own files.

A removal's selector and captured still are not moved into a claim, so the
replay of a removal that stopped has nothing of its own that says which file
at those names was the removed wallpaper's. From this version on the intent
records it when it is prepared (`removals.Intent.companions`): each of those
files that is there, by generation and contents. Here: what is recorded, how
it is saved, and that anything other than a well-formed record of it is no
proof at all.

The field is added inside journal version 1. The reader goes by the key's
presence: absent, the intent was prepared by a version that kept no proof
(0.2.2, and 0.3.0 before this), and it still reads.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from wall_in_one import file_io
from wall_in_one.library import manage, pairing, removals, stills
from wall_in_one.library.model import Kind, MediaItem

#: Larger than any selector, so a bound can leave out the still alone.
FRAME = b"\x89PNG\r\n\x1a\n" + b"a frame " * 1024


def _video(root: Path) -> MediaItem:
    """A video with a selector, a captured still and that still's own record."""
    root.mkdir(exist_ok=True)
    video = root / "clip.mp4"
    video.write_bytes(b"a video")
    still = stills.destination(video, root)
    still.parent.mkdir(parents=True)
    still.write_bytes(FRAME)
    still.with_name(still.name + pairing.SIDECAR_SUFFIX).write_text("{}\n", encoding="utf-8")
    video.with_name(video.name + pairing.SIDECAR_SUFFIX).write_text(
        json.dumps({pairing.SIDECAR_STILL_KEY: str(still)}) + "\n", encoding="utf-8"
    )
    return MediaItem(path=video, kind=Kind.VIDEO, size=7, mtime=1)


def _own(item: MediaItem, root: Path) -> tuple[Path, ...]:
    return manage.pairing_artifact_paths(item, (root,))


def _saved(journal: Path) -> list[dict[str, Any]]:
    saved: list[dict[str, Any]] = json.loads(journal.read_text(encoding="utf-8"))["removals"]
    return saved


def test_prepare_records_each_of_the_wallpapers_own_files_that_is_there(tmp_path: Path) -> None:
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    item = _video(root)
    selector, still, still_record = _own(item, root)
    still_record.unlink()  # one that is not there is not recorded

    intent = removals.Store.open(journal).prepare(item, (root,))

    assert intent.companions == tuple(
        manage.CompanionProof(
            path,
            file_io.regular_file_fingerprint(path),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in (selector, still)
    )
    # Saved before anything of the removal can move, and read back the same.
    (record,) = _saved(journal)
    assert [proof["path"] for proof in record["companions"]] == [str(selector), str(still)]
    assert record["companions"][1] == {
        "path": str(still),
        "device": still.stat().st_dev,
        "inode": still.stat().st_ino,
        "size": len(FRAME),
        "mtime_ns": still.stat().st_mtime_ns,
        "ctime_ns": still.stat().st_ctime_ns,
        "sha256": hashlib.sha256(FRAME).hexdigest(),
    }
    assert removals.Store.open(journal).records == (intent,)
    assert removals.FORMAT_VERSION == 1, "added inside this version: its presence is the marker"


def test_a_download_record_is_never_among_them(tmp_path: Path) -> None:
    """A removal takes the download record out of its name before the media,
    whole, into a claim named from its token, and replay finds it only there.
    It is not one of the files replay looks for at a public name."""
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    item = _video(root)
    for record in manage.provider_sidecar_paths(item.path):
        record.write_text("{}", encoding="utf-8")

    intent = removals.Store.open(journal).prepare(item, (root,))

    assert intent.companions is not None
    assert {proof.path for proof in intent.companions} == set(_own(item, root))


def _unreadable(path: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    path.chmod(0)


def _over_the_bound(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manage, "MAX_COMPANION_PROOF_BYTES", path.stat().st_size - 1)


def _not_a_file(path: Path, _monkeypatch: pytest.MonkeyPatch) -> None:
    path.unlink()
    path.mkdir()


@pytest.mark.parametrize("unprovable", [_unreadable, _over_the_bound, _not_a_file])
def test_proof_that_cannot_be_taken_is_left_out_and_never_refuses_the_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unprovable: Callable[[Path, pytest.MonkeyPatch], None],
) -> None:
    """A file that can't be read whole, is larger than the bound, or is not a
    regular file is simply not recorded, so a replay leaves whatever is at
    its name. The removal itself is prepared all the same."""
    if os.geteuid() == 0:
        pytest.skip("root reads a file of mode 000")
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    item = _video(root)
    selector, still, still_record = _own(item, root)
    unprovable(still, monkeypatch)

    intent = removals.Store.open(journal).prepare(item, (root,))

    assert intent.companions is not None
    assert [proof.path for proof in intent.companions] == [selector, still_record]
    assert len(_saved(journal)) == 1


def test_a_folder_that_cannot_be_looked_at_proves_nothing_and_refuses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    item = _video(root)

    def closed(*_arguments: object, **_options: object) -> file_io.PinnedDirectoryContext:
        raise OSError("the folder went away")

    monkeypatch.setattr(file_io, "pin_directory_beneath", closed)

    intent = removals.Store.open(journal).prepare(item, (root,))

    # Nothing could be proven, which is not the same as no proof kept: this
    # version prepared it, and the record says so.
    assert intent.companions == ()
    assert _saved(journal)[0]["companions"] == []


def test_no_proof_and_nothing_proven_are_saved_differently(tmp_path: Path) -> None:
    """Without the key, the intent is one from before the field, and it is
    never written back with one. With an empty list, this version prepared it
    and found nothing it could prove."""
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    item = _video(root)
    for path in _own(item, root):
        path.unlink()
    store = removals.Store.open(journal)
    proven = store.prepare(item, (root,))
    assert proven.companions == () and proven.to_json()["companions"] == []
    store.close()

    document = json.loads(journal.read_text(encoding="utf-8"))
    del document["removals"][0]["companions"]
    journal.write_text(json.dumps(document), encoding="utf-8")
    before = copy.deepcopy(document["removals"][0])

    reopened = removals.Store.open(journal)
    (older,) = reopened.records
    assert reopened.fault is None and older.companions is None
    assert "companions" not in older.to_json()
    # A save that is about another removal carries it as it was.
    other = MediaItem(path=root / "elsewhere.png", kind=Kind.STILL, size=1, mtime=1)
    reopened.record_external(other, (root,))
    kept = next(record for record in _saved(journal) if record["identity"] == before["identity"])
    assert kept == before
    # Null is no proof either, never an empty list.
    document["removals"][0]["companions"] = None
    journal.write_text(json.dumps(document), encoding="utf-8")
    assert removals.Store.open(journal).records[0].companions is None


def _prepared(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    store = removals.Store.open(journal)
    store.prepare(_video(root), (root,))
    store.close()
    document: dict[str, Any] = json.loads(journal.read_text(encoding="utf-8"))
    assert len(document["removals"][0]["companions"]) == 3
    return journal, document


def _set(key: str, value: object) -> Callable[[list[dict[str, Any]]], object]:
    def change(proofs: list[dict[str, Any]]) -> object:
        proofs[0][key] = value
        return proofs

    return change


def _many(proofs: list[dict[str, Any]]) -> object:
    return [
        {**proofs[0], "path": f"{proofs[0]['path']}.{number}"}
        for number in range(removals.MAX_COMPANIONS_PER_REMOVAL + 1)
    ]


MALFORMED: dict[str, Callable[[list[dict[str, Any]]], object]] = {
    "not a list": lambda proofs: {"0": proofs[0]},
    "a string": lambda _proofs: "none",
    "an entry that is not an object": lambda proofs: [*proofs, "x"],
    "a null entry": lambda proofs: [None, *proofs],
    "more than a wallpaper can have": _many,
    "a key missing": lambda proofs: [
        {key: value for key, value in proofs[0].items() if key != "sha256"}
    ],
    "a key this version does not know": _set("why", "a newer version's"),
    "a relative path": _set("path", "clip.mp4.wall-in-one.json"),
    "a path outside the removal's folders": _set("path", "/etc/hostname"),
    "the same path twice": lambda proofs: [proofs[0], proofs[0]],
    "a number that is not one": _set("inode", "12"),
    "true for a number": _set("device", True),
    "a negative size": _set("size", -1),
    "a time that is not a number": _set("ctime_ns", 1.5),
    "a digest in capitals": _set("sha256", "A" * 64),
    "a short digest": _set("sha256", "a" * 63),
    "a digest that is not hex": _set("sha256", "g" * 64),
    "a digest that is not text": _set("sha256", 7),
}


@pytest.mark.parametrize("how", MALFORMED)
def test_a_malformed_proof_makes_its_intent_unusable(tmp_path: Path, how: str) -> None:
    """Each entry is authority to remove a file, so the reading is strict: one
    wrong thing and the intent is not an intent. The journal is then faulted
    as for any malformed intent: nothing in it is acted on, and it is not
    rewritten."""
    journal, document = _prepared(tmp_path)
    record = document["removals"][0]
    record["companions"] = MALFORMED[how](record["companions"])
    written = json.dumps(document).encode()
    journal.write_bytes(written)

    store = removals.Store.open(journal)

    assert store.records == ()
    assert store.fault is not None and "1 malformed removal intents" in store.fault
    other = tmp_path / "wallpapers" / "other.png"
    other.write_bytes(b"another")
    with pytest.raises(removals.RemovalJournalError) as refused:
        store.prepare(MediaItem(path=other, kind=Kind.STILL, size=7, mtime=1), (other.parent,))
    assert refused.value.kind == "invalid-state"
    assert journal.read_bytes() == written


def test_an_external_uninstall_carries_no_proof(tmp_path: Path) -> None:
    """A Workshop uninstall is recorded after the fact and removes no file: a
    record of one that claims proof is not one this version wrote."""
    root, journal = tmp_path / "wallpapers", tmp_path / "pending-removals.json"
    root.mkdir()
    item = MediaItem(path=root / "gone.mp4", kind=Kind.VIDEO, size=1, mtime=1)
    external = removals.Store.open(journal).record_external(item, (root,))
    assert external.companions is None and "companions" not in _saved(journal)[0]

    document = json.loads(journal.read_text(encoding="utf-8"))
    document["removals"][0]["companions"] = []
    journal.write_text(json.dumps(document), encoding="utf-8")

    store = removals.Store.open(journal)
    assert store.records == () and store.fault is not None
