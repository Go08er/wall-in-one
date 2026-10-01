"""The sandbox narrowing probes, kept as regressions.

Each case is the probe's own document and edit (``narrowing-probes/probe.py``
from the 2026-09-30 compatibility survey). Before the store guard:

1. editing a ``version: 99`` playlists.json moved the original to ``.broken``
   and rewrote it as version 1 without the unknown ``description``;
2. editing a same-version playlists.json silently dropped a record's unknown
   ``description``, with no fault and no backup;
3. editing schedules.json silently dropped a rule's unknown ``name``.

Since 0.2.0 a rule ``name`` is a modeled field of schedules version 3, so
probe three now proves the name is read as one and kept, the file moves to
version 3, and the version-2 bytes are kept beside it first.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wall_in_one.library import playlists, schedules, state_file


def test_probe_one_an_older_build_never_narrows_a_newer_playlists_file(tmp_path: Path) -> None:
    target = tmp_path / "state" / "playlists.json"
    target.parent.mkdir()
    target.write_text(
        json.dumps(
            {
                "version": 99,
                "playlists": [{"id": "x", "name": "X", "entries": [], "description": "new"}],
            }
        )
    )
    original = target.read_bytes()
    store = playlists.Store.open(target)
    assert store.fault is not None
    assert store.fault_kind == state_file.NEWER_VERSION

    with pytest.raises(playlists.PlaylistError) as caught:
        store.create("Y")

    assert caught.value.kind == "newer-version"
    assert not target.with_name(target.name + ".broken").exists()
    assert target.read_bytes() == original
    document = json.loads(target.read_text())
    assert document["version"] == 99
    assert [sorted(record) for record in document["playlists"]] == [
        ["description", "entries", "id", "name"]
    ]


def test_probe_two_a_same_version_record_keeps_its_unknown_key(tmp_path: Path) -> None:
    target = tmp_path / "state" / "pl2.json"
    target.parent.mkdir()
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "playlists": [{"id": "x", "name": "X", "entries": [], "description": "new"}],
            }
        )
    )
    store = playlists.Store.open(target)
    assert store.fault is None

    store.create("Z")

    document = json.loads(target.read_text())
    records = {record["id"]: record for record in document["playlists"]}
    assert records["x"] == {"id": "x", "name": "X", "entries": [], "description": "new"}
    assert sorted(sorted(record) for record in document["playlists"]) == [
        ["description", "entries", "id", "name"],
        ["entries", "id", "name"],
    ]
    assert not target.with_name(target.name + ".broken").exists()


def test_probe_three_a_schedule_rule_keeps_its_name(tmp_path: Path) -> None:
    target = tmp_path / "state" / "schedules.json"
    target.parent.mkdir()
    target.write_text(
        json.dumps({"version": 2, "rules": [{"id": "a", "playlist": "X", "name": "Evening rule"}]})
    )
    original = target.read_bytes()
    store = schedules.Store.open(target)
    assert store.fault is None
    assert store.rules[0].name == "Evening rule"

    store.add("Y", rule_id="b")

    document = json.loads(target.read_text())
    assert document["version"] == 3
    assert document["rules"] == [
        {"id": "a", "playlist": "X", "name": "Evening rule"},
        {"id": "b", "playlist": "Y"},
    ]
    assert target.with_name("schedules.json.v2-backup").read_bytes() == original
