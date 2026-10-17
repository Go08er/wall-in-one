"""AE-4: an adopted still that could not be validated is not a known absence.

These use the accepted predecessor-adoption fixture, without a source-side
selector or a current hashed capture. The only failure is a read of one
exact private metadata inode; no live profile or media decoder is involved.
"""

from __future__ import annotations

import errno
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_adopted import Fixture, _fixture
from wall_in_one import config, file_io, runtime_config
from wall_in_one.library import adopted, scan, stills
from wall_in_one.library.model import UnseenKind
from wall_in_one.session import Session


def _profile(fixture: Fixture, use: str) -> tuple[config.Settings, Session, Path]:
    settings = config.Settings(
        roots=(fixture.root,),
        scan_workshop=False,
        display_mode=config.DISPLAY_MODE_INDEPENDENT,
        theme_source_connector="DP-1",
    )
    session = Session(settings, owns_playback=False)
    playlist = session.playlists.create("Adopted video")
    session.playlists.add(playlist.id, fixture.source)
    if use == "default":
        settings = replace(settings, active_playlist=playlist.id)
    elif use == "rule":
        session.schedules.add(playlist.id, rule_id="adopted-rule", start="08:00", end="09:00")
    else:
        assert use == "display"
        session.displays.assign("DP-1", playlist.id)
    session.adopt_library(scan.scan(settings.roots))
    item = session.library.find(fixture.source)
    assert item is not None and item.paired_still == fixture.capture
    target = fixture.root.parent / "runtime.toml"
    assert runtime_config.publish_changes(settings, session, target).left_out == ()
    return settings, session, target


def _fail_metadata_read(
    monkeypatch: pytest.MonkeyPatch, blocked: Path, error_number: int = errno.EACCES
) -> None:
    identity = file_io.path_identity(blocked)
    read = file_io.read_pinned_regular_bytes

    def fail_exact_metadata_read(
        pinned: file_io.PinnedPath,
        maximum_bytes: int,
        *,
        expected_fingerprint: file_io.FileFingerprint | None = None,
    ) -> bytes:
        # A pin may be named through its retained directory's /proc path.
        # The inode identity limits this failure to the exact private file.
        if pinned.identity == identity:
            raise OSError(error_number, "private adopted metadata cannot be read", str(blocked))
        return read(pinned, maximum_bytes, expected_fingerprint=expected_fingerprint)

    monkeypatch.setattr(file_io, "read_pinned_regular_bytes", fail_exact_metadata_read)


@pytest.mark.parametrize("use", ("default", "rule", "display"))
@pytest.mark.parametrize("which", ("sidecar", "marker"))
@pytest.mark.parametrize("error_number", (errno.EACCES, errno.EIO), ids=("permission", "io"))
def test_unreadable_adopted_proof_refuses_instead_of_leaving_out_its_playlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use: str,
    which: str,
    error_number: int,
) -> None:
    fixture = _fixture(tmp_path, central=True)
    settings, session, target = _profile(fixture, use)
    before = target.read_bytes()
    blocked = fixture.authority.sidecar_path if which == "sidecar" else fixture.marker
    _fail_metadata_read(monkeypatch, blocked, error_number)
    try:
        session.adopt_library(scan.scan(settings.roots))
        with pytest.raises(
            runtime_config.RuntimeConfigError, match="could not look|can.t be looked into"
        ):
            runtime_config.publish_changes(settings, session, target)
        assert target.read_bytes() == before
        assert any(
            place.path == fixture.source and place.kind is UnseenKind.UNREADABLE
            for place in session.library.unseen
        )
        assert fixture.capture.exists() and blocked.exists()
    finally:
        session.shutdown()


@pytest.mark.parametrize("which", ("sidecar", "marker"))
def test_unreadable_adoption_in_a_retired_root_does_not_block_the_current_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    fixture = _fixture(tmp_path, central=True)
    settings, session, target = _profile(fixture, "default")
    blocked = fixture.authority.sidecar_path if which == "sidecar" else fixture.marker
    _fail_metadata_read(monkeypatch, blocked)
    other = tmp_path / "other-library"
    other.mkdir()
    settings = replace(settings, roots=(other,))
    try:
        session.adopt_library(scan.scan(settings.roots))
        assert session.library.unseen == ()
        published = runtime_config.publish_changes(settings, session, target)
        assert len(published.left_out) == 1
        assert published.left_out[0].why == runtime_config.NOTHING_TO_PLAY
    finally:
        session.shutdown()


@pytest.mark.parametrize("which", ("sidecar", "marker"))
def test_a_valid_current_still_allows_publication_despite_unreadable_adoption_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    fixture = _fixture(tmp_path, central=True)
    settings, session, target = _profile(fixture, "default")
    current = stills.destination(fixture.source, fixture.root)
    current.write_bytes(fixture.capture.read_bytes())
    blocked = fixture.authority.sidecar_path if which == "sidecar" else fixture.marker
    _fail_metadata_read(monkeypatch, blocked)
    try:
        session.adopt_library(scan.scan(settings.roots))
        item = session.library.find(fixture.source)
        assert item is not None and item.paired_still == current
        loaded = adopted.load()
        assert loaded is not None and loaded.authorities == ()
        assert runtime_config.publish_changes(settings, session, target).left_out == ()
        assert str(current) in target.read_text(encoding="utf-8")
    finally:
        session.shutdown()


@pytest.mark.parametrize("which", ("sidecar", "marker"))
@pytest.mark.parametrize("change", ("absent", "changed"))
def test_absent_or_changed_adoption_proof_still_revokes_without_an_unreadable_place(
    tmp_path: Path, which: str, change: str
) -> None:
    fixture = _fixture(tmp_path, central=True)
    settings, session, target = _profile(fixture, "default")
    proof = fixture.authority.sidecar_path if which == "sidecar" else fixture.marker
    if change == "absent":
        proof.unlink()
    else:
        proof.write_bytes(b"{}\n")
    try:
        session.adopt_library(scan.scan(settings.roots))
        loaded = adopted.load()
        assert loaded is not None and loaded.authorities == ()
        assert session.library.unseen == ()
        published = runtime_config.publish_changes(settings, session, target)
        assert len(published.left_out) == 1
        assert published.left_out[0].why == runtime_config.NOTHING_TO_PLAY
    finally:
        session.shutdown()


def _implicit_profile(fixture: Fixture) -> tuple[config.Settings, Session, Path]:
    """No default playlist: All media is what plays."""
    settings = config.Settings(roots=(fixture.root,), scan_workshop=False)
    session = Session(settings, owns_playback=False)
    session.adopt_library(scan.scan(settings.roots))
    target = fixture.root.parent / "runtime.toml"
    runtime_config.publish_changes(settings, session, target)
    return settings, session, target


def test_a_denied_sidecar_keeps_the_capture_hidden_and_the_implicit_runtime_unchanged(
    tmp_path: Path,
) -> None:
    """Failed at 2b48044: the capture became a still of its own and replaced the video."""
    fixture = _fixture(tmp_path, central=True)
    settings, session, target = _implicit_profile(fixture)
    before = target.read_bytes()
    assert 'kind = "video"' in target.read_text(encoding="utf-8")
    sidecar = fixture.authority.sidecar_path
    sidecar.chmod(0)
    try:
        session.adopt_library(scan.scan(settings.roots))
        assert all(item.path != fixture.capture for item in session.library.items)
        with pytest.raises(runtime_config.RuntimeConfigError, match="can't be looked into"):
            runtime_config.publish_changes(settings, session, target)
        assert target.read_bytes() == before
    finally:
        sidecar.chmod(0o600)
        session.shutdown()


def test_a_denied_sidecar_does_not_add_the_capture_to_a_mixed_all_media(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, central=True)
    other = fixture.root / "other.png"
    other.write_bytes(fixture.capture.read_bytes())
    settings, session, target = _implicit_profile(fixture)
    sidecar = fixture.authority.sidecar_path
    sidecar.chmod(0)
    try:
        session.adopt_library(scan.scan(settings.roots))
        assert [item.path for item in session.library.items if item.path == fixture.capture] == []
        runtime_config.publish_changes(settings, session, target)
        text = target.read_text(encoding="utf-8")
        assert str(other) in text
        assert str(fixture.capture) not in text
    finally:
        sidecar.chmod(0o600)
        session.shutdown()


@pytest.mark.parametrize("change", ("absent", "changed"))
def test_an_absent_or_changed_sidecar_still_revokes_the_display_only_hiding(
    tmp_path: Path, change: str
) -> None:
    """Only a failed look keeps the capture hidden; absent or changed proof is revocation."""
    fixture = _fixture(tmp_path, central=True)
    settings, session, _ = _implicit_profile(fixture)
    sidecar = fixture.authority.sidecar_path
    if change == "absent":
        sidecar.unlink()
    else:
        sidecar.write_bytes(b"{}\n")
    try:
        session.adopt_library(scan.scan(settings.roots))
        assert session.library.unseen == ()
        assert any(item.path == fixture.capture for item in session.library.items)
    finally:
        session.shutdown()


@pytest.mark.parametrize("which", ("sidecar", "marker"))
def test_a_video_with_an_alternate_still_records_no_skipped_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    """Failed at 2b48044: the video still played, yet "1 skipped" deferred the cleanup."""
    fixture = _fixture(tmp_path, central=True)
    settings, session, _ = _profile(fixture, "default")
    current = stills.destination(fixture.source, fixture.root)
    current.write_bytes(fixture.capture.read_bytes())
    _fail_metadata_read(
        monkeypatch, fixture.authority.sidecar_path if which == "sidecar" else fixture.marker
    )
    try:
        library = scan.scan(settings.roots)
        item = library.find(fixture.source)
        assert item is not None and item.paired_still == current
        assert library.skipped == ()
        assert library.unseen == ()
    finally:
        session.shutdown()
