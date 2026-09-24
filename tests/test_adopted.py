"""Generation-bound use of captures adopted from the deployed application."""

from __future__ import annotations

import hashlib
import json
import os
import zlib
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from wall_in_one import config, file_io, paths, predecessor_process
from wall_in_one.control import client
from wall_in_one.control.protocol import Response
from wall_in_one.library import adopted, capture_upgrade, pairing, pairings, scan, stills
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


def _png(pixel: bytes = b"\x12\x34\x56") -> bytes:
    """A complete one-pixel RGB image, with real compressed pixels and CRCs."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4) + kind + data + zlib.crc32(kind + data).to_bytes(4)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", (1).to_bytes(4) * 2 + b"\x08\x02\x00\x00\x00")
        + chunk(b"IDAT", zlib.compress(b"\x00" + pixel))
        + chunk(b"IEND", b"")
    )


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
    capture_bytes = _png()
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


def _previous_mount(fixture: Fixture) -> Fixture:
    """Journal a different device number with otherwise identical generations."""
    authority = replace(
        fixture.authority,
        source_fingerprint=(
            fixture.authority.source_fingerprint[0] + 1,
            *fixture.authority.source_fingerprint[1:],
        ),
        capture_fingerprint=(
            fixture.authority.capture_fingerprint[0] + 1,
            *fixture.authority.capture_fingerprint[1:],
        ),
    )
    original = fixture.adoption
    publication = replace(
        original,
        root_identity=(original.root_identity[0] + 1, original.root_identity[1]),
        automatic_stills_identity=(
            original.automatic_stills_identity[0] + 1,
            original.automatic_stills_identity[1],
        ),
        marker_fingerprint=(original.marker_fingerprint[0] + 1, *original.marker_fingerprint[1:]),
        authorities=(authority,),
    )
    publication = replace(publication, adoption_id=adopted.adoption_id_for(publication))
    authority.sidecar_path.write_bytes(adopted.render_sidecar(authority, publication.adoption_id))
    fixture.manifest.write_bytes(adopted.render_manifest(publication))
    return replace(fixture, authority=authority, adoption=publication)


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


def test_scan_hides_migrated_captures_after_device_renumbering_without_granting_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _previous_mount(_fixture(tmp_path, central=True))
    current = stills.destination(fixture.source, fixture.root)
    current.write_bytes(b"new hashed capture")
    manifest_before = fixture.manifest.read_bytes()
    sidecar_before = fixture.authority.sidecar_path.read_bytes()

    def unexpected_hash(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("a library scan must not hash every migrated capture")

    monkeypatch.setattr(file_io, "hash_pinned_regular", unexpected_hash)
    library = scan.scan((fixture.root,))

    assert [item.path for item in library.items] == [fixture.source]
    assert library.items[0].paired_still == current
    assert library.stills == ()
    assert library.reusable_stills == ()
    assert {item.path for item in library.still_inventory} == {fixture.capture, current}
    assert library.adopted_stills == ()
    loaded = adopted.load()
    assert loaded is not None
    assert loaded.authorities == ()
    assert loaded.internal_captures == ((fixture.capture, _fingerprint(fixture.capture)),)
    for verify_contents in (False, True):
        assert adopted.authority_for(fixture.source, verify_contents=verify_contents) is None
    with pytest.raises(adopted.AdoptionError, match="no longer exact"):
        adopted.load(strict=True)
    assert fixture.manifest.read_bytes() == manifest_before
    assert fixture.authority.sidecar_path.read_bytes() == sidecar_before


@pytest.mark.parametrize("in_automatic_directory", (False, True))
def test_manually_created_separate_still_remains_visible_after_device_renumbering(
    tmp_path: Path,
    in_automatic_directory: bool,
) -> None:
    fixture = _previous_mount(_fixture(tmp_path, central=True))
    directory = fixture.capture.parent if in_automatic_directory else fixture.root
    manual = directory / "my-separate-still.png"
    manual.write_bytes(fixture.capture.read_bytes())
    source = _item(fixture.source, Kind.VIDEO)
    record = pairings.Pairing(identity=pairings.Identity.of(source), still=manual, customized=True)

    library = scan.scan((fixture.root,), records={record.identity.key: record})

    assert {item.path for item in library.items} == {fixture.source, manual}
    assert [item.path for item in library.reusable_stills] == [manual]
    assert (
        next(item for item in library.items if item.path == fixture.source).paired_still == manual
    )


@pytest.mark.parametrize("change", ("replace", "edit", "hardlink", "symlink", "sidecar"))
def test_changed_capture_is_not_hidden_after_device_renumbering(
    tmp_path: Path,
    change: str,
) -> None:
    fixture = _previous_mount(_fixture(tmp_path, central=True))
    if change == "replace":
        replacement = tmp_path / "replacement.png"
        replacement.write_bytes(fixture.capture.read_bytes())
        replacement.replace(fixture.capture)
    elif change == "edit":
        fixture.capture.write_bytes(b"user edited image")
    elif change == "hardlink":
        os.link(fixture.capture, tmp_path / "linked.png")
    elif change == "symlink":
        image = tmp_path / "user.png"
        fixture.capture.rename(image)
        fixture.capture.symlink_to(image)
    else:
        fixture.authority.sidecar_path.write_bytes(b"{}")

    loaded = adopted.load()

    assert loaded is not None
    assert loaded.internal_captures == ()
    assert loaded.authorities == ()
    if change != "symlink":  # Library traversal already excludes symlinks.
        library = scan.scan((fixture.root,))
        assert fixture.capture in {item.path for item in library.stills}


def test_scan_rechecks_display_only_capture_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _previous_mount(_fixture(tmp_path, central=True))
    original_load = adopted.load

    def load_then_replace(
        *, strict: bool = False, path: Path | None = None
    ) -> adopted.Adoption | None:
        publication = original_load(strict=strict, path=path)
        assert publication is not None and publication.internal_captures
        fixture.capture.write_bytes(b"replacement after display validation")
        return publication

    monkeypatch.setattr(adopted, "load", load_then_replace)

    library = scan.scan((fixture.root,))

    assert fixture.capture in {item.path for item in library.stills}
    assert library.adopted_stills == ()


@pytest.mark.parametrize("remove_source", (False, True))
def test_generated_capture_does_not_become_a_wallpaper_when_original_video_changes(
    tmp_path: Path,
    remove_source: bool,
) -> None:
    fixture = _fixture(tmp_path, central=True)
    if remove_source:
        fixture.source.unlink()
    else:
        fixture.source.write_bytes(b"a different video")

    library = scan.scan((fixture.root,))

    assert library.stills == ()
    assert library.reusable_stills == ()
    assert library.adopted_stills == ()
    assert fixture.capture in {item.path for item in library.still_inventory}
    assert fixture.capture.is_file()


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


@pytest.fixture
def cleanup_environment(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Real publication/compiler, fake frame renderer, no live service/bus."""
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    rendered: list[Path] = []

    def render(video: Path, target: Path, seek: float, **_kwargs: object) -> str:
        del seek
        rendered.append(video)
        target.write_bytes(_png(b"\x78\x9a\xbc"))
        return ""

    def stopped(*_args: object, **_kwargs: object) -> Response:
        raise client.NotRunningError("test runtime is stopped")

    monkeypatch.setattr(stills, "_run", render)
    monkeypatch.setattr(stills, "is_available", lambda: True)
    monkeypatch.setattr(client, "send_runtime", stopped)
    monkeypatch.setattr(predecessor_process, "refuse_live_predecessor_runtime", lambda: None)
    monkeypatch.setattr(capture_upgrade, "RUNTIME_WAIT_SECONDS", 0.0)
    return rendered


def _cleanup_fixture(tmp_path: Path) -> Fixture:
    fixture = _previous_mount(_fixture(tmp_path, central=True))
    config.save(config.Settings(roots=(fixture.root,), scan_workshop=False))
    return fixture


def _new_capture(fixture: Fixture) -> Path:
    target = stills.destination(fixture.source, fixture.root)
    target.write_bytes(_png(b"\x78\x9a\xbc"))
    return target


def test_rebuild_rebind_compile_and_purge_original(
    tmp_path: Path, cleanup_environment: list[Path]
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    stills.write_sidecar(fixture.source, fixture.capture)
    before = fixture.capture.read_bytes()
    manifest = fixture.manifest.read_bytes()
    target = stills.destination(fixture.source, fixture.root)

    with file_io.pin_regular_path(fixture.capture) as old:
        result = capture_upgrade.prepare()
        assert old.status().st_size == 0

    assert result == capture_upgrade.Result(migrated=1, reclaimed_bytes=len(before))
    assert len(cleanup_environment) == 1
    assert target.read_bytes() != before  # A fresh frame, not copied/renamed old bytes.
    assert pairing.read_sidecar(fixture.source) == target
    assert str(fixture.capture) not in paths.runtime_config_path().read_text()
    assert not fixture.capture.exists()
    assert not fixture.authority.sidecar_path.exists()
    assert fixture.manifest.read_bytes() == manifest


def test_rebuild_failure_restores_old_binding_and_can_retry(
    tmp_path: Path, cleanup_environment: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    stills.write_sidecar(fixture.source, fixture.capture)
    before = fixture.capture.read_bytes()
    original = stills.generate

    def fail(*_args: object, **_kwargs: object) -> Path:
        raise stills.StillError("ffmpeg is unavailable")

    monkeypatch.setattr(stills, "generate", fail)
    assert capture_upgrade.prepare().retained == 1
    assert pairing.read_sidecar(fixture.source) == fixture.capture
    assert fixture.capture.read_bytes() == before
    monkeypatch.setattr(stills, "generate", original)
    assert capture_upgrade.prepare().migrated == 1
    assert not fixture.capture.exists()


def test_existing_unbound_current_capture_needs_no_render_or_new_selector(
    tmp_path: Path, cleanup_environment: list[Path]
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    current = _new_capture(fixture)
    before = current.read_bytes()
    assert capture_upgrade.prepare().migrated == 1
    assert cleanup_environment == []
    assert pairing.read_sidecar(fixture.source) is None
    assert current.read_bytes() == before


@pytest.mark.parametrize("relative", (False, True))
def test_manual_sidecar_choice_needs_no_rebuild_and_retires_old_capture(
    tmp_path: Path, cleanup_environment: list[Path], relative: bool
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    manual = fixture.root / "manual.png"
    manual.write_bytes(_png(b"\x55\x66\x77"))
    selector = stills.write_sidecar(fixture.source, manual)
    if relative:
        selector.write_text(json.dumps({"still_path": "../manual.png", "note": "keep my choice"}))
    selector_before = selector.read_bytes()
    manual_before = manual.read_bytes()
    current = stills.destination(fixture.source, fixture.root)
    assert not current.exists()

    assert capture_upgrade.prepare() == capture_upgrade.Result(
        migrated=1, reclaimed_bytes=fixture.authority.capture_size
    )
    assert cleanup_environment == []
    assert not fixture.capture.exists()
    assert not fixture.authority.sidecar_path.exists()
    assert not current.exists()
    assert selector.read_bytes() == selector_before
    assert manual.read_bytes() == manual_before
    assert str(fixture.capture) not in paths.runtime_config_path().read_text()
    for _ in range(2):
        assert capture_upgrade.prepare() == capture_upgrade.Result()
    assert cleanup_environment == []


def test_missing_manual_choice_does_not_render_repeatedly(
    tmp_path: Path, cleanup_environment: list[Path]
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    missing = fixture.root / "deleted-manual.png"
    selector = stills.write_sidecar(fixture.source, missing)
    selector_before = selector.read_bytes()
    original = fixture.capture.read_bytes()

    for _ in range(3):
        assert capture_upgrade.prepare().retained == 1
        assert fixture.capture.read_bytes() == original
        assert fixture.authority.sidecar_path.exists()
        assert selector.read_bytes() == selector_before
    assert cleanup_environment == []
    assert not stills.destination(fixture.source, fixture.root).exists()


@pytest.mark.parametrize("chosen", ("legacy", "current"))
def test_relative_automatic_selector_is_not_mistaken_for_a_separate_choice(
    tmp_path: Path, cleanup_environment: list[Path], chosen: str
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    target = stills.destination(fixture.source, fixture.root)
    if chosen == "current":
        target.write_bytes(b"broken current capture")
    else:
        _new_capture(fixture)
    selected = fixture.capture if chosen == "legacy" else target
    selector = fixture.source.with_name(fixture.source.name + pairing.SIDECAR_SUFFIX)
    selector.write_text(
        json.dumps({"still_path": os.path.relpath(selected, fixture.source.parent)})
    )
    original = fixture.capture.read_bytes()

    assert not capture_upgrade._has_other_still(fixture.authority, target)
    assert capture_upgrade.prepare().retained == 1
    assert fixture.capture.read_bytes() == original
    assert fixture.authority.sidecar_path.exists()


def test_separate_choice_is_rechecked_before_original_deletion(
    tmp_path: Path, cleanup_environment: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    manual = fixture.root / "manual.png"
    manual.write_bytes(_png())
    selector = stills.write_sidecar(fixture.source, manual)
    before = selector.read_bytes()
    original = capture_upgrade._runtime_references

    def remove_choice_during_confirmation(document: bytes) -> frozenset[str]:
        references = original(document)
        manual.unlink()
        return references

    monkeypatch.setattr(capture_upgrade, "_runtime_references", remove_choice_during_confirmation)
    assert capture_upgrade.prepare().retained == 1
    assert fixture.capture.exists()
    assert selector.read_bytes() == before
    assert cleanup_environment == []


def test_separate_choice_does_not_bypass_saved_reference_protection(
    tmp_path: Path, cleanup_environment: list[Path]
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    manual = fixture.root / "manual.png"
    manual.write_bytes(_png())
    selector = stills.write_sidecar(fixture.source, manual)
    before = selector.read_bytes()
    identity = pairings.Identity.of(_item(fixture.source, Kind.VIDEO))
    pairings.save(
        {identity.key: pairings.Pairing(identity, still=fixture.capture, customized=True)}
    )

    assert capture_upgrade.prepare().retained == 1
    assert fixture.capture.exists()
    assert selector.read_bytes() == before
    assert cleanup_environment == []


def test_source_gone_purges_only_proven_orphan(
    tmp_path: Path, cleanup_environment: list[Path]
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    fixture.source.unlink()
    manual = fixture.capture.parent / "my-still.png"
    manual.write_bytes(fixture.capture.read_bytes())
    assert capture_upgrade.prepare().migrated == 1
    assert cleanup_environment == []
    assert not fixture.capture.exists()
    assert manual.exists()


def test_saved_reference_prevents_dangling_authoring(
    tmp_path: Path, cleanup_environment: list[Path]
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    _new_capture(fixture)
    identity = pairings.Identity.of(_item(fixture.source, Kind.VIDEO))
    record = pairings.Pairing(identity, still=fixture.capture, customized=True)
    pairings.save({identity.key: record})
    before = pairings.state_path().read_bytes()
    assert capture_upgrade.prepare().retained == 1
    assert fixture.capture.exists()
    assert pairings.state_path().read_bytes() == before


@pytest.mark.parametrize("change", ("size", "same-size", "sidecar", "symlink", "hardlink"))
def test_unproven_images_are_visible_and_never_deleted(
    tmp_path: Path, cleanup_environment: list[Path], change: str
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    _new_capture(fixture)
    if change == "size":
        fixture.capture.write_bytes(b"user picture")
    elif change == "same-size":
        fixture.capture.write_bytes(_png(b"\x11\x33\x55"))
        assert fixture.capture.stat().st_size == fixture.authority.capture_size
    elif change == "sidecar":
        fixture.authority.sidecar_path.write_bytes(b"{}")
    elif change == "symlink":
        elsewhere = tmp_path / "manual.png"
        fixture.capture.rename(elsewhere)
        fixture.capture.symlink_to(elsewhere)
    else:
        os.link(fixture.capture, tmp_path / "manual.png")
    before = fixture.capture.read_bytes()

    assert capture_upgrade.prepare().retained == 1
    assert fixture.capture.read_bytes() == before
    assert cleanup_environment == []
    if change != "symlink":
        assert fixture.capture in {item.path for item in scan.scan((fixture.root,)).items}


@pytest.mark.parametrize("state", ("old", "wrong-path", "missing-hash", "timeout", "starting"))
def test_unconfirmed_runtime_keeps_originals(
    tmp_path: Path,
    cleanup_environment: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    _new_capture(fixture)

    def status(*_args: object, **_kwargs: object) -> Response:
        if state == "timeout":
            raise client.ControlTimeoutError("busy")
        if state == "starting":
            raise client.NotRunningError("not bound yet")
        return Response.success(
            json.dumps(
                {
                    "config_path": "/other/runtime.toml"
                    if state == "wrong-path"
                    else str(paths.runtime_config_path()),
                    "loaded_config_sha256": None
                    if state == "missing-hash"
                    else (
                        "0" * 64
                        if state == "old"
                        else hashlib.sha256(paths.runtime_config_path().read_bytes()).hexdigest()
                    ),
                }
            )
        )

    def starting() -> None:
        raise predecessor_process.PredecessorProcessError("runtime is starting")

    monkeypatch.setattr(client, "send_runtime", status)
    if state == "starting":
        monkeypatch.setattr(predecessor_process, "refuse_live_predecessor_runtime", starting)
    assert capture_upgrade.prepare() == capture_upgrade.Result()
    assert fixture.capture.exists()


def test_waits_for_runtime_handover_before_deletion(
    tmp_path: Path, cleanup_environment: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    _new_capture(fixture)
    calls = 0

    def status(*_args: object, **_kwargs: object) -> Response:
        nonlocal calls
        calls += 1
        assert fixture.capture.exists()
        return Response.success(
            json.dumps(
                {
                    "config_path": str(paths.runtime_config_path()),
                    "loaded_config_sha256": "0" * 64
                    if calls == 1
                    else hashlib.sha256(paths.runtime_config_path().read_bytes()).hexdigest(),
                }
            )
        )

    monkeypatch.setattr(client, "send_runtime", status)
    monkeypatch.setattr(capture_upgrade, "RUNTIME_WAIT_SECONDS", 1.0)
    assert capture_upgrade.prepare().migrated == 1
    assert calls == 2


def test_current_runtime_path_is_also_protected(
    tmp_path: Path, cleanup_environment: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    _new_capture(fixture)

    def status(*_args: object, **_kwargs: object) -> Response:
        return Response.success(
            json.dumps(
                {
                    "config_path": str(paths.runtime_config_path()),
                    "loaded_config_sha256": hashlib.sha256(
                        paths.runtime_config_path().read_bytes()
                    ).hexdigest(),
                    "current_still": str(fixture.capture),
                }
            )
        )

    monkeypatch.setattr(client, "send_runtime", status)
    assert capture_upgrade.prepare().retained == 1
    assert fixture.capture.exists()


def test_restart_after_selector_removal_completes_rebuild(
    tmp_path: Path, cleanup_environment: list[Path]
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    # The original is still present but a prior process exited after unbinding.
    assert pairing.read_sidecar(fixture.source) is None
    assert capture_upgrade.prepare().migrated == 1
    assert len(cleanup_environment) == 1


def test_successful_cleanup_does_no_work_on_next_start(
    tmp_path: Path, cleanup_environment: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _cleanup_fixture(tmp_path)
    assert capture_upgrade.prepare().migrated == 1

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("completed cleanup must not scan, render or query runtime")

    monkeypatch.setattr(scan, "scan", unexpected)
    monkeypatch.setattr(stills, "generate", unexpected)
    monkeypatch.setattr(client, "send_runtime", unexpected)
    assert capture_upgrade.prepare() == capture_upgrade.Result()


@pytest.mark.parametrize("damage", ("header-only", "bad-crc", "hardlink"))
def test_invalid_current_capture_cannot_replace_original(
    tmp_path: Path, cleanup_environment: list[Path], damage: str
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    current = _new_capture(fixture)
    stills.write_sidecar(fixture.source, fixture.capture)
    if damage == "header-only":
        current.write_bytes(current.read_bytes()[:24])
    elif damage == "bad-crc":
        raw = bytearray(current.read_bytes())
        raw[-1] ^= 1
        current.write_bytes(raw)
    else:
        os.link(current, tmp_path / "shared.png")

    assert capture_upgrade.prepare().retained == 1
    assert fixture.capture.exists()
    assert pairing.read_sidecar(fixture.source) == fixture.capture


def test_crash_after_image_deletion_resumes_metadata_cleanup(
    tmp_path: Path, cleanup_environment: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    _new_capture(fixture)
    original = capture_upgrade._discard

    def interrupted(
        pin: file_io.PinnedPath, generation: file_io.FileFingerprint, parent: Path
    ) -> None:
        if pin.path.name == fixture.authority.sidecar_path.name:
            raise OSError("interrupted before metadata retirement")
        original(pin, generation, parent)

    monkeypatch.setattr(capture_upgrade, "_discard", interrupted)
    capture_upgrade.prepare()
    assert not fixture.capture.exists()
    assert fixture.authority.sidecar_path.exists()
    monkeypatch.setattr(capture_upgrade, "_discard", original)
    capture_upgrade.prepare()
    assert not fixture.authority.sidecar_path.exists()


def test_damaged_authoring_or_incomplete_scan_defers_cleanup(
    tmp_path: Path, cleanup_environment: list[Path]
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    _new_capture(fixture)
    config.save(config.Settings(roots=(fixture.root, tmp_path / "offline"), scan_workshop=False))
    assert capture_upgrade.prepare() == capture_upgrade.Result()
    assert fixture.capture.exists()
    config.save(config.Settings(roots=(fixture.root,), scan_workshop=False))
    pairings.state_path().write_bytes(b"not JSON")
    assert capture_upgrade.prepare() == capture_upgrade.Result()
    assert fixture.capture.exists()


def test_unconfigured_root_is_not_touched(tmp_path: Path, cleanup_environment: list[Path]) -> None:
    fixture = _cleanup_fixture(tmp_path)
    config.save(config.Settings(roots=(), scan_workshop=False))
    assert capture_upgrade.prepare() == capture_upgrade.Result()
    assert fixture.capture.exists()
    assert cleanup_environment == []


def test_unattended_health_writer_does_not_run_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from wall_in_one import cli, deployed_upgrade_transaction, legacy_migration

    def unexpected() -> None:
        raise AssertionError("recurring unattended writer must not run capture cleanup")

    monkeypatch.setattr(capture_upgrade, "prepare", unexpected)
    monkeypatch.setattr(
        deployed_upgrade_transaction, "ensure", lambda: SimpleNamespace(changed=False)
    )
    monkeypatch.setattr(legacy_migration, "require_unattended_safe_locked", lambda: None)
    assert cli._run_unattended_writer(lambda: 0) == 0


def test_service_start_prepare_runs_cleanup_before_compilation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wall_in_one import cli

    calls: list[str] = []

    def compile_runtime() -> int:
        calls.append("compile")
        return 0

    monkeypatch.setattr(cli, "_run_unattended_writer", lambda write: write())
    monkeypatch.setattr(capture_upgrade, "prepare", lambda: calls.append("cleanup"))
    monkeypatch.setattr(cli, "_write_runtime_config", compile_runtime)
    assert cli._prepare_service_start() == 0
    assert calls == ["cleanup", "compile"]


def test_failed_output_validation_restores_original_binding(
    tmp_path: Path, cleanup_environment: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    stills.write_sidecar(fixture.source, fixture.capture)

    def bad_render(_video: Path, target: Path, _seek: float, **_kwargs: object) -> str:
        target.write_bytes(b"not a PNG")
        return ""

    monkeypatch.setattr(stills, "_run", bad_render)
    assert capture_upgrade.prepare().retained == 1
    assert fixture.capture.exists()
    assert pairing.read_sidecar(fixture.source) == fixture.capture


def test_manual_selector_created_during_failure_is_not_overwritten(
    tmp_path: Path, cleanup_environment: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    stills.write_sidecar(fixture.source, fixture.capture)
    manual = fixture.root / "manual.png"
    manual.write_bytes(_png())

    def fail(*_args: object, **_kwargs: object) -> Path:
        stills.write_sidecar(fixture.source, manual)
        raise stills.StillError("renderer failed while user chose another still")

    monkeypatch.setattr(stills, "generate", fail)
    assert capture_upgrade.prepare().retained == 1
    assert fixture.capture.exists()
    assert pairing.read_sidecar(fixture.source) == manual
    assert manual.exists()


@pytest.mark.parametrize("changed", ("capture", "metadata"))
def test_proof_change_after_hashing_prevents_deletion(
    tmp_path: Path,
    cleanup_environment: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    _new_capture(fixture)
    original = capture_upgrade._validate_replacement
    validations = 0

    def edit_during_final_validation(path: Path) -> None:
        nonlocal validations
        original(path)
        validations += 1
        if validations == 3:  # Before rebuild, after rebuild, then before deletion.
            target = fixture.capture if changed == "capture" else fixture.authority.sidecar_path
            target.write_bytes(b"a user's edit after the original was hashed")

    monkeypatch.setattr(capture_upgrade, "_validate_replacement", edit_during_final_validation)
    assert capture_upgrade.prepare().retained == 1
    assert validations == 3
    assert fixture.capture.exists()
    assert fixture.authority.sidecar_path.exists()


def test_completed_deployed_upgrade_stays_complete_after_capture_cleanup(
    tmp_path: Path, cleanup_environment: list[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_deployed_upgrade import _profile
    from wall_in_one import deployed_upgrade, deployed_upgrade_transaction

    profile = _profile(tmp_path, monkeypatch, videos=2, named_playlist=True)
    assert deployed_upgrade_transaction.ensure().status == "complete"
    manifest = adopted.state_path().read_bytes()
    completed = deployed_upgrade.completion_path().read_bytes()

    assert capture_upgrade.prepare().migrated == 2
    assert all(not capture.exists() for _source, capture in profile.entries)
    assert deployed_upgrade_transaction.ensure().status == "complete"
    assert adopted.state_path().read_bytes() == manifest
    assert deployed_upgrade.completion_path().read_bytes() == completed


@pytest.mark.skipif(not stills.is_available(), reason="ffmpeg is not installed")
def test_cleanup_with_real_ffmpeg_rebuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_stills import make_video

    fixture = _cleanup_fixture(tmp_path)
    make_video(fixture.source, seconds=0.2, colour="blue")
    stills.write_sidecar(fixture.source, fixture.capture)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)

    def stopped(*_args: object, **_kwargs: object) -> Response:
        raise client.NotRunningError("test runtime is stopped")

    monkeypatch.setattr(client, "send_runtime", stopped)
    monkeypatch.setattr(predecessor_process, "refuse_live_predecessor_runtime", lambda: None)
    assert capture_upgrade.prepare().migrated == 1
    target = stills.destination(fixture.source, fixture.root)
    assert target.stat().st_size > 100
    assert pairing.read_sidecar(fixture.source) == target
    assert not fixture.capture.exists()


def test_existing_app_snapshot_prevents_startup_cleanup(
    tmp_path: Path, cleanup_environment: list[Path]
) -> None:
    fixture = _cleanup_fixture(tmp_path)
    _new_capture(fixture)
    socket = paths.socket_path()
    socket.parent.mkdir(parents=True, exist_ok=True)
    socket.touch()  # Even an ambiguous/stale socket path defers destructive work.
    assert capture_upgrade.prepare() == capture_upgrade.Result()
    assert fixture.capture.exists()
    assert cleanup_environment == []
