"""Destructive lifecycle rules for captures adopted during deployed upgrade."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from wall_in_one import config, file_io, paths
from wall_in_one.library import (
    adopted,
    favourites,
    manage,
    pairing,
    pairings,
    playlists,
    removals,
    stills,
)
from wall_in_one.library.model import Kind, Library, MediaItem, Ownership
from wall_in_one.session import Session
from wall_in_one.wallpaper.applier import Applier


class _Renderer:
    def start(self, _video: Path) -> None:
        pass

    def stop(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class _Case:
    root: Path
    source: Path
    capture: Path
    authority: adopted.Authority
    item: MediaItem
    session: Session


def _fingerprint(path: Path) -> file_io.FileFingerprint:
    return file_io.regular_file_fingerprint(path)


def _download_sidecar(path: Path) -> dict[str, object]:
    status = path.stat()
    contents = path.read_bytes()
    return {
        "schema": 1,
        "plugin": adopted.PLUGIN_ID,
        "provider": "Wallhaven",
        "path": str(path),
        "bytes": len(contents),
        "sha256": hashlib.sha256(contents).hexdigest(),
        "media_generation": {
            "device": status.st_dev,
            "inode": status.st_ino,
            "bytes": status.st_size,
            "mtime_ns": status.st_mtime_ns,
            "ctime_ns": status.st_ctime_ns,
        },
    }


def _case(tmp_path: Path, *, managed: bool) -> _Case:
    root = tmp_path / "wallpapers"
    source_parent = root / ("Wall-in-One/Wallhaven" if managed else "personal")
    source_parent.mkdir(parents=True)
    source = source_parent / "clip.mp4"
    source.write_bytes(b"exact deployed video generation")
    if managed:
        (source_parent / ".managed-by-wall-in-one-v1.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "plugin": adopted.PLUGIN_ID,
                    "provider": "Wallhaven",
                    "kind": "wallhaven",
                    "ownership": "managed",
                }
            ),
            encoding="utf-8",
        )
        source.with_name(source.name + ".wallhaven.json").write_text(
            json.dumps(_download_sidecar(source)),
            encoding="utf-8",
        )

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
    capture = automatic / "clip.png"
    capture_bytes = b"\x89PNG\r\n\x1a\n" + b"deployed capture generation"
    capture.write_bytes(capture_bytes)
    authority = adopted.Authority(
        source_path=source,
        capture_path=capture,
        source_fingerprint=_fingerprint(source),
        capture_fingerprint=_fingerprint(capture),
        capture_size=len(capture_bytes),
        capture_sha256=hashlib.sha256(capture_bytes).hexdigest(),
    )
    publication = adopted.Adoption(
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
    publication = replace(
        publication,
        adoption_id=adopted.adoption_id_for(publication),
    )
    authority.sidecar_path.write_bytes(adopted.render_sidecar(authority, publication.adoption_id))
    manifest = adopted.state_path()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_bytes(adopted.render_manifest(publication))

    source_status = source.stat()
    item = MediaItem(
        source,
        Kind.VIDEO,
        source_status.st_size,
        int(source_status.st_mtime),
        ownership=Ownership.MANAGED if managed else Ownership.USER,
        provider="Wallhaven" if managed else "local",
        paired_still=capture,
    )
    settings = replace(config.Settings(), roots=(root,)).validated()
    session = Session(
        settings,
        applier=Applier(_Renderer()),  # type: ignore[arg-type]
        favourite_store=favourites.Store(path=tmp_path / "favourites.json"),
        pairing_store=pairings.Store(path=tmp_path / "pairings.json"),
        playlist_store=playlists.Store(path=tmp_path / "playlists.json"),
        removal_store=removals.Store.open(tmp_path / "pending-removals.json"),
    )
    session.adopt_library(
        Library(
            roots=(root,),
            items=(item,),
            adopted_stills=((source, capture),),
        )
    )
    return _Case(root, source, capture, authority, item, session)


@pytest.mark.parametrize("trash", (True, False), ids=("trash-user", "remove-managed"))
def test_live_removal_pins_before_commit_and_then_withdraws_exact_adopted_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    trash: bool,
) -> None:
    case = _case(tmp_path, managed=not trash)
    plan = case.session.prepare_removal_plan(case.item, trash=trash)
    reached_source_commit = False

    if trash:
        original_move = file_io.atomic_move_no_replace

        def observe_move(source: Path, destination: Path, **keywords: object) -> None:
            nonlocal reached_source_commit
            if source.name == case.source.name and case.source.exists():
                reached_source_commit = True
                assert case.capture.exists()
                assert case.authority.sidecar_path.exists()
            original_move(source, destination, **keywords)  # type: ignore[arg-type]

        monkeypatch.setattr(file_io, "atomic_move_no_replace", observe_move)
    else:
        original_claim = file_io.claim_for_deletion

        def observe_claim(path: Path, **keywords: object) -> file_io.ClaimedPath:
            nonlocal reached_source_commit
            if path.name == case.source.name and case.source.exists():
                reached_source_commit = True
                assert case.capture.exists()
                assert case.authority.sidecar_path.exists()
            return original_claim(path, **keywords)  # type: ignore[arg-type]

        monkeypatch.setattr(file_io, "claim_for_deletion", observe_claim)

    try:
        result = plan.run()
    finally:
        case.session.shutdown()

    assert result.committed
    assert reached_source_commit
    assert not case.capture.exists()
    assert not case.authority.sidecar_path.exists()
    assert result.physical is not None
    removed_artifacts = (
        result.physical.removed
        if isinstance(result.physical, manage.Removal)
        else result.physical.removed_artifacts
    )
    assert case.capture in removed_artifacts
    assert case.authority.sidecar_path in removed_artifacts
    assert result.cleanups
    assert case.capture in result.cleanups[0].removed_stills


@pytest.mark.parametrize("trash", (True, False), ids=("trash-user", "remove-managed"))
def test_precommit_failure_rolls_back_without_consuming_adopted_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    trash: bool,
) -> None:
    case = _case(tmp_path, managed=not trash)
    plan = case.session.prepare_removal_plan(case.item, trash=trash)
    if trash:
        original_ensure = paths.ensure_directory

        def refuse_trash(directory: Path) -> None:
            if directory == manage.trash_directory() / "files":
                raise PermissionError("injected trash setup failure")
            original_ensure(directory)

        monkeypatch.setattr(paths, "ensure_directory", refuse_trash)
    else:
        original_claim = file_io.claim_for_deletion

        def refuse_source_claim(path: Path, **keywords: object) -> file_io.ClaimedPath:
            if path.name == case.source.name:
                raise PermissionError("injected source claim failure")
            return original_claim(path, **keywords)  # type: ignore[arg-type]

        monkeypatch.setattr(file_io, "claim_for_deletion", refuse_source_claim)

    try:
        result = plan.run()
    finally:
        case.session.shutdown()

    assert not result.committed
    assert result.error_kind == "local-io"
    assert case.source.exists()
    assert case.capture.exists()
    assert case.authority.sidecar_path.exists()


@pytest.mark.parametrize("mutation", ("capture", "sidecar", "hardlink"))
def test_post_validation_adopted_changes_are_retained_without_basename_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    case = _case(tmp_path, managed=False)
    plan = case.session.prepare_removal_plan(case.item, trash=True)
    original_authority_for = adopted.authority_for
    extra_link = tmp_path / "capture-second-link.png"
    changed = False

    def validate_then_change(
        source: Path,
        *,
        capture: Path | None = None,
        verify_contents: bool = False,
    ) -> adopted.Authority | None:
        nonlocal changed
        result = original_authority_for(
            source,
            capture=capture,
            verify_contents=verify_contents,
        )
        if result is not None and not changed:
            changed = True
            if mutation == "capture":
                case.capture.unlink()
                case.capture.write_bytes(b"user replacement capture")
            elif mutation == "sidecar":
                case.authority.sidecar_path.unlink()
                case.authority.sidecar_path.write_bytes(b'{"replacement":true}\n')
            else:
                os.link(case.capture, extra_link)
        return result

    monkeypatch.setattr(adopted, "authority_for", validate_then_change)
    try:
        result = plan.run()
    finally:
        case.session.shutdown()

    assert changed
    assert result.committed
    assert not case.source.exists()
    assert case.capture.exists()
    assert case.authority.sidecar_path.exists()
    if mutation == "capture":
        assert case.capture.read_bytes() == b"user replacement capture"
    elif mutation == "sidecar":
        assert case.authority.sidecar_path.read_bytes() == b'{"replacement":true}\n'
    else:
        assert extra_link.exists()
    assert result.physical is not None
    kept = (
        result.physical.kept
        if isinstance(result.physical, manage.Removal)
        else result.physical.kept_artifacts
    )
    assert case.capture in kept or case.authority.sidecar_path in kept
    assert result.cleanups
    assert case.capture not in result.cleanups[0].removed_stills


def test_unpopulated_authority_is_not_deletion_authority(tmp_path: Path) -> None:
    case = _case(tmp_path, managed=False)
    plan = case.session.prepare_removal_plan(case.item, trash=True)
    # A plain manifest binding deliberately lacks the validation-only
    # adoption id and sidecar fingerprint which only ``authority_for`` adds.
    unvalidated = case.authority
    try:
        result = manage.trash(
            case.item,
            (case.root,),
            expected_source=file_io.path_identity(case.source),
            expected_fingerprint=_fingerprint(case.source),
            adopted_authority=unvalidated,
        )
    finally:
        # ``plan`` exists to prove the path came from accepted Library truth;
        # no worker is run because this test exercises manage's final guard.
        del plan
        case.session.shutdown()

    assert result.destination.is_file()
    assert case.capture.exists()
    assert case.authority.sidecar_path.exists()
    assert case.capture in result.kept_artifacts
    assert case.authority.sidecar_path in result.kept_artifacts


def test_revoked_adoption_suppresses_a_deterministic_name_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(tmp_path, managed=False)
    plan = case.session.prepare_removal_plan(case.item, trash=True)
    original_authority_for = adopted.authority_for
    calls = 0

    def revoke_between_validations(
        source: Path,
        *,
        capture: Path | None = None,
        verify_contents: bool = False,
    ) -> adopted.Authority | None:
        nonlocal calls
        result = original_authority_for(
            source,
            capture=capture,
            verify_contents=verify_contents,
        )
        calls += 1
        if calls == 1:
            case.authority.sidecar_path.write_bytes(b'{"revoked":true}\n')
        return result

    monkeypatch.setattr(adopted, "authority_for", revoke_between_validations)
    # Model the adversarial (but valid) case where the predecessor basename
    # also happens to equal a deterministic current destination.
    monkeypatch.setattr(
        stills,
        "automatic_destination",
        lambda _item, _root: case.capture,
    )
    try:
        result = plan.run()
    finally:
        case.session.shutdown()

    assert calls == 2
    assert result.committed
    assert case.capture.exists()
    assert case.authority.sidecar_path.exists()
    assert result.physical is not None
    assert isinstance(result.physical, manage.Trashed)
    assert case.capture in result.physical.kept_artifacts
    assert result.cleanups
    assert case.capture not in result.cleanups[0].removed_stills


def test_pairing_reset_returns_to_the_adopted_scan_default(tmp_path: Path) -> None:
    case = _case(tmp_path, managed=False)
    manual = case.root / "manual.png"
    manual.write_bytes(b"manual still")
    store = pairings.Store(path=tmp_path / "reset-pairings.json")
    store.choose_still(case.item, manual)
    store.choose_still(case.item, None)

    resolved = store.resolve_accepted(case.item, case.session.library)

    assert resolved.still == case.capture
    case.session.shutdown()
