"""Generation-bound use of captures adopted from the deployed application."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from wall_in_one import file_io
from wall_in_one.library import adopted, pairing, pairings, scan, stills
from wall_in_one.library.model import Kind, MediaItem, Ownership


@dataclass(frozen=True)
class Fixture:
    root: Path
    source: Path
    capture: Path
    marker: Path
    manifest: Path
    authority: adopted.Authority
    adoption: adopted.Adoption


def _fingerprint(path: Path) -> file_io.FileFingerprint:
    return file_io.regular_file_fingerprint(path)


def _fixture(
    tmp_path: Path,
    *,
    central: bool = False,
    capture_name: str = "clip.png",
) -> Fixture:
    root = tmp_path / "wallpapers"
    source = root / "videos" / "clip.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"deployed video generation")
    automatic = pairing.still_directory(root)
    automatic.mkdir(parents=True)
    marker = automatic / adopted.AUTOMATIC_MARKER_FILENAME
    marker.write_bytes(
        adopted.canonical_bytes(
            {
                "schema": 1,
                "plugin": adopted.PLUGIN_ID,
                "kind": "automatic-stills",
                "ownership": "managed",
            }
        )
    )
    capture = automatic / capture_name
    capture_bytes = b"\x89PNG\r\n\x1a\n" + b"deployed full-resolution capture"
    capture.write_bytes(capture_bytes)
    authority = adopted.Authority(
        source_path=source,
        capture_path=capture,
        source_fingerprint=_fingerprint(source),
        capture_fingerprint=_fingerprint(capture),
        capture_size=len(capture_bytes),
        capture_sha256=hashlib.sha256(capture_bytes).hexdigest(),
    )
    adoption = adopted.Adoption(
        adoption_id="",
        root=root,
        root_identity=file_io.path_identity(root),
        automatic_stills=automatic,
        automatic_stills_identity=file_io.path_identity(automatic),
        marker_path=marker,
        marker_fingerprint=_fingerprint(marker),
        marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest(),
        authorities=(authority,),
    )
    adoption = replace(adoption, adoption_id=adopted.adoption_id_for(adoption))
    authority.sidecar_path.write_bytes(adopted.render_sidecar(authority, adoption.adoption_id))
    manifest = adopted.state_path() if central else tmp_path / adopted.MANIFEST_FILENAME
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_bytes(adopted.render_manifest(adoption))
    return Fixture(root, source, capture, marker, manifest, authority, adoption)


def _item(path: Path, kind: Kind) -> MediaItem:
    status = path.stat()
    return MediaItem(path, kind, status.st_size, int(status.st_mtime))


def _republish_marker(fixture: Fixture, marker: bytes) -> None:
    fixture.marker.write_bytes(marker)
    publication = replace(
        fixture.adoption,
        adoption_id="",
        marker_fingerprint=_fingerprint(fixture.marker),
        marker_sha256=hashlib.sha256(marker).hexdigest(),
    )
    publication = replace(publication, adoption_id=adopted.adoption_id_for(publication))
    fixture.authority.sidecar_path.write_bytes(
        adopted.render_sidecar(fixture.authority, publication.adoption_id)
    )
    fixture.manifest.write_bytes(adopted.render_manifest(publication))


def test_exact_manifest_and_sidecar_round_trip(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    loaded = adopted.load(strict=True, path=fixture.manifest)

    assert loaded == fixture.adoption
    assert loaded is not None
    assert loaded.mapping == {fixture.source: fixture.capture}
    assert json.loads(adopted.render_manifest(loaded))["bindings_sha256"] == (
        adopted.bindings_digest(loaded.authorities)
    )
    assert fixture.authority.sidecar_path.read_bytes() == adopted.render_sidecar(
        fixture.authority, fixture.adoption.adoption_id
    )


def test_normal_load_does_not_hash_capture_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)

    def unexpected_hash(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("normal adoption loading must not hash capture contents")

    monkeypatch.setattr(file_io, "hash_pinned_regular", unexpected_hash)

    loaded = adopted.load(path=fixture.manifest)

    assert loaded is not None
    assert loaded.authorities == (fixture.authority,)


def test_authority_for_validates_only_the_selected_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, central=True)
    calls = 0
    original_hash = file_io.hash_pinned_regular

    def counted_hash(
        pinned: file_io.PinnedPath,
        *,
        expected_fingerprint: file_io.FileFingerprint | None = None,
        maximum_bytes: int | None = None,
    ) -> tuple[int, str]:
        nonlocal calls
        calls += 1
        assert maximum_bytes == fixture.authority.capture_size
        return original_hash(
            pinned,
            expected_fingerprint=expected_fingerprint,
            maximum_bytes=maximum_bytes,
        )

    monkeypatch.setattr(file_io, "hash_pinned_regular", counted_hash)

    assert adopted.authority_for(fixture.source) == fixture.authority
    assert calls == 0
    destructive = adopted.authority_for(
        fixture.source,
        capture=fixture.capture,
        verify_contents=True,
    )
    assert destructive == fixture.authority
    assert destructive is not None
    assert destructive.adoption_id == fixture.adoption.adoption_id
    assert destructive.sidecar_fingerprint == _fingerprint(fixture.authority.sidecar_path)
    assert calls == 1
    assert adopted.authority_for(fixture.source, capture=fixture.root / "other.png") is None


def test_missing_manifest_means_there_is_no_adoption(tmp_path: Path) -> None:
    assert adopted.load(path=tmp_path / "absent.json") is None


def test_manifest_json_rejects_duplicate_keys(tmp_path: Path) -> None:
    target = tmp_path / "manifest.json"
    target.write_text('{"version":1,"version":1}\n', encoding="utf-8")

    with pytest.raises(adopted.AdoptionError, match="duplicate-free JSON"):
        adopted.load(path=target)


@pytest.mark.parametrize(
    "number",
    ("NaN", "Infinity", "-Infinity", "1.0", "1e10000"),
)
def test_manifest_json_rejects_noninteger_numbers(tmp_path: Path, number: str) -> None:
    fixture = _fixture(tmp_path)
    raw = fixture.manifest.read_bytes().replace(
        b'"version":1',
        f'"version":{number}'.encode(),
        1,
    )
    fixture.manifest.write_bytes(raw)

    with pytest.raises(adopted.AdoptionError, match="strict duplicate-free JSON"):
        adopted.load(path=fixture.manifest)


def test_manifest_binding_is_typed_before_it_is_canonicalized(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    document = adopted.manifest_document(fixture.adoption)
    bindings = document["bindings"]
    assert isinstance(bindings, list)
    binding = bindings[0]
    assert isinstance(binding, dict)
    binding["source_path"] = "\ud800"
    fixture.manifest.write_bytes(
        (json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    )

    with pytest.raises(adopted.AdoptionError, match="source_path is not valid UTF-8"):
        adopted.load(path=fixture.manifest)


def test_manifest_rejects_an_id_not_derived_from_its_exact_evidence(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    document = adopted.manifest_document(fixture.adoption)
    document["adoption_id"] = "f" * 64
    fixture.manifest.write_bytes(adopted.canonical_bytes(document))

    with pytest.raises(adopted.AdoptionError, match="does not match"):
        adopted.load(path=fixture.manifest)


def test_capture_must_use_the_exact_predecessor_basename(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, capture_name="unrelated.png")

    with pytest.raises(adopted.AdoptionError, match="exact predecessor basename"):
        adopted.load(path=fixture.manifest)


def test_marker_semantics_are_checked_independently_of_its_digest(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.marker.write_bytes(
        adopted.canonical_bytes(
            {
                "schema": 1,
                "plugin": adopted.PLUGIN_ID,
                "kind": "automatic-stills",
                "ownership": "user",
            }
        )
    )
    publication = replace(
        fixture.adoption,
        adoption_id="",
        marker_fingerprint=_fingerprint(fixture.marker),
        marker_sha256=hashlib.sha256(fixture.marker.read_bytes()).hexdigest(),
    )
    publication = replace(publication, adoption_id=adopted.adoption_id_for(publication))
    fixture.authority.sidecar_path.write_bytes(
        adopted.render_sidecar(fixture.authority, publication.adoption_id)
    )
    fixture.manifest.write_bytes(adopted.render_manifest(publication))

    loaded = adopted.load(path=fixture.manifest)

    assert loaded is not None
    assert loaded.authorities == ()
    with pytest.raises(adopted.AdoptionError, match="exact ownership identity"):
        adopted.load(strict=True, path=fixture.manifest)


@pytest.mark.parametrize("number", ("NaN", "Infinity", "-Infinity"))
def test_marker_json_rejects_nonfinite_numbers(tmp_path: Path, number: str) -> None:
    fixture = _fixture(tmp_path)
    marker = fixture.marker.read_bytes().replace(
        b'"schema":1',
        f'"schema":{number}'.encode(),
        1,
    )
    _republish_marker(fixture, marker)

    loaded = adopted.load(path=fixture.manifest)

    assert loaded is not None
    assert loaded.authorities == ()
    with pytest.raises(adopted.AdoptionError, match="strict duplicate-free JSON"):
        adopted.load(strict=True, path=fixture.manifest)


def test_marker_lone_surrogate_is_a_revoked_filesystem_proof(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    marker = (
        json.dumps(
            {
                "schema": 1,
                "plugin": adopted.PLUGIN_ID,
                "kind": "automatic-stills",
                "ownership": "\ud800",
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    _republish_marker(fixture, marker)

    loaded = adopted.load(path=fixture.manifest)

    assert loaded is not None
    assert loaded.authorities == ()
    with pytest.raises(adopted.AdoptionError, match="exact ownership identity"):
        adopted.load(strict=True, path=fixture.manifest)


def test_manifest_authority_must_not_be_group_writable(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.manifest.chmod(0o664)

    with pytest.raises(adopted.AdoptionError, match="writable by another user or group"):
        adopted.load(path=fixture.manifest)


def test_marker_authority_must_not_be_world_writable(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.marker.chmod(0o666)
    publication = replace(
        fixture.adoption,
        adoption_id="",
        marker_fingerprint=_fingerprint(fixture.marker),
    )
    publication = replace(publication, adoption_id=adopted.adoption_id_for(publication))
    fixture.authority.sidecar_path.write_bytes(
        adopted.render_sidecar(fixture.authority, publication.adoption_id)
    )
    fixture.manifest.write_bytes(adopted.render_manifest(publication))

    loaded = adopted.load(path=fixture.manifest)

    assert loaded is not None
    assert loaded.authorities == ()
    with pytest.raises(adopted.AdoptionError, match="writable by another user or group"):
        adopted.load(strict=True, path=fixture.manifest)


def test_sidecar_authority_must_not_be_group_writable(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.authority.sidecar_path.chmod(0o664)

    loaded = adopted.load(path=fixture.manifest)

    assert loaded is not None
    assert loaded.authorities == ()
    with pytest.raises(adopted.AdoptionError, match="writable by another user or group"):
        adopted.load(strict=True, path=fixture.manifest)


@pytest.mark.parametrize("strict", (False, True), ids=("scan", "migration-verifier"))
def test_capture_replacement_revokes_only_the_binding(tmp_path: Path, *, strict: bool) -> None:
    fixture = _fixture(tmp_path)
    fixture.capture.unlink()
    fixture.capture.write_bytes(b"user replacement")

    if strict:
        with pytest.raises(adopted.AdoptionError, match="no longer exact"):
            adopted.load(strict=True, path=fixture.manifest)
    else:
        loaded = adopted.load(path=fixture.manifest)
        assert loaded is not None
        assert loaded.authorities == ()


def test_hard_link_surprise_is_not_accepted(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    os.link(fixture.capture, tmp_path / "unexpected-second-name.png")

    loaded = adopted.load(path=fixture.manifest)

    assert loaded is not None
    assert loaded.authorities == ()
    with pytest.raises(adopted.AdoptionError, match="no longer exact"):
        adopted.load(strict=True, path=fixture.manifest)


def test_noncanonical_or_replaced_sidecar_revokes_the_binding(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    document = adopted.sidecar_document(
        fixture.authority,
        fixture.adoption.adoption_id,
    )
    fixture.authority.sidecar_path.write_text(json.dumps(document), encoding="utf-8")

    loaded = adopted.load(path=fixture.manifest)

    assert loaded is not None
    assert loaded.authorities == ()


@pytest.mark.parametrize("number", ("NaN", "Infinity", "-Infinity"))
def test_sidecar_json_rejects_nonfinite_numbers(tmp_path: Path, number: str) -> None:
    fixture = _fixture(tmp_path)
    sidecar = fixture.authority.sidecar_path
    sidecar.write_bytes(
        sidecar.read_bytes().replace(
            b'"schema":2',
            f'"schema":{number}'.encode(),
            1,
        )
    )

    loaded = adopted.load(path=fixture.manifest)

    assert loaded is not None
    assert loaded.authorities == ()
    with pytest.raises(adopted.AdoptionError, match="strict duplicate-free JSON"):
        adopted.load(strict=True, path=fixture.manifest)


def test_sidecar_lone_surrogate_is_a_revoked_binding(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    document = adopted.sidecar_document(
        fixture.authority,
        fixture.adoption.adoption_id,
    )
    document["source_path"] = "\ud800"
    fixture.authority.sidecar_path.write_bytes(
        (json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    )

    loaded = adopted.load(path=fixture.manifest)

    assert loaded is not None
    assert loaded.authorities == ()
    with pytest.raises(adopted.AdoptionError, match="no longer exact"):
        adopted.load(strict=True, path=fixture.manifest)


def test_scan_pairs_and_hides_an_exact_adopted_capture(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, central=True)

    library = scan.scan((fixture.root,))

    assert [item.path for item in library.items] == [fixture.source]
    assert library.items[0].paired_still == fixture.capture
    assert library.adopted_stills == ((fixture.source, fixture.capture),)
    assert fixture.capture in {item.path for item in library.still_inventory}


def test_scan_exposes_a_replacement_at_an_adopted_capture_path(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, central=True)
    fixture.capture.unlink()
    fixture.capture.write_bytes(b"user replacement generation")

    library = scan.scan((fixture.root,))

    by_path = {item.path: item for item in library.items}
    assert set(by_path) == {fixture.source, fixture.capture}
    assert by_path[fixture.source].paired_still is None
    assert by_path[fixture.capture].ownership is Ownership.USER
    assert by_path[fixture.capture].provider == "local"
    assert library.adopted_stills == ()
    assert fixture.capture in {item.path for item in library.still_inventory}


def test_scan_rechecks_a_loaded_adoption_against_the_walked_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, central=True)
    original_load = adopted.load
    replaced = False

    def load_then_replace(
        *,
        strict: bool = False,
        path: Path | None = None,
    ) -> adopted.Adoption | None:
        nonlocal replaced
        publication = original_load(strict=strict, path=path)
        if publication is not None and not replaced:
            fixture.capture.unlink()
            fixture.capture.write_bytes(b"replacement after adoption validation")
            replaced = True
        return publication

    monkeypatch.setattr(adopted, "load", load_then_replace)

    library = scan.scan((fixture.root,))

    by_path = {item.path: item for item in library.items}
    assert replaced
    assert set(by_path) == {fixture.source, fixture.capture}
    assert by_path[fixture.source].paired_still is None
    assert by_path[fixture.capture].ownership is Ownership.USER
    assert library.adopted_stills == ()


def test_schema_one_legacy_sidecar_cannot_hide_a_current_file_generation(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    fixture.authority.sidecar_path.write_bytes(
        adopted.canonical_bytes(
            {
                "schema": 1,
                "plugin": adopted.PLUGIN_ID,
                "kind": "automatic-still",
                "path": str(fixture.capture),
                "dynamic_id": f"video:{fixture.source}",
            }
        )
    )
    assert pairing.legacy_automatic_identity(fixture.capture) == f"video:{fixture.source}"

    library = scan.scan((fixture.root,))

    by_path = {item.path: item for item in library.items}
    assert set(by_path) == {fixture.source, fixture.capture}
    assert by_path[fixture.source].paired_still is None
    assert by_path[fixture.capture].ownership is Ownership.USER
    assert library.adopted_stills == ()


def test_authored_choice_wins_while_the_adopted_child_stays_hidden(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, central=True)
    manual = fixture.root / "manual.png"
    manual.write_bytes(b"manual still")
    source_item = _item(fixture.source, Kind.VIDEO)
    record = pairings.Pairing(
        identity=pairings.Identity.of(source_item),
        still=manual,
        customized=True,
    )

    library = scan.scan((fixture.root,), records={record.identity.key: record})

    by_path = {item.path: item for item in library.items}
    assert set(by_path) == {fixture.source, manual}
    assert by_path[fixture.source].paired_still == manual
    assert library.adopted_stills == ((fixture.source, fixture.capture),)


def test_current_hashed_capture_precedes_adopted_capture(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, central=True)
    current = stills.destination(fixture.source, fixture.root)
    current.write_bytes(b"current hashed capture")

    library = scan.scan((fixture.root,))

    assert [item.path for item in library.items] == [fixture.source]
    assert library.items[0].paired_still == current
    assert library.adopted_stills == ((fixture.source, fixture.capture),)


def test_manifest_for_an_unconfigured_root_does_not_change_a_scan(tmp_path: Path) -> None:
    _fixture(tmp_path, central=True)
    other = tmp_path / "other"
    other.mkdir()
    picture = other / "picture.png"
    picture.write_bytes(b"ordinary image")

    library = scan.scan((other,))

    assert [item.path for item in library.items] == [picture]
    assert library.adopted_stills == ()
