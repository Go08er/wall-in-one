"""Removing a wallpaper, and refusing to.

This is the only code in the program that destroys anything, so most of what
is pinned here is what it declines to do. The scenario worth keeping in mind
throughout: the app downloads into a directory the user also keeps their own
photographs in, and this module is the whole of what stands between a delete
button and those photographs.

Nothing here touches the user's real directories. Every path is under
`tmp_path`, and the trash is redirected with `XDG_DATA_HOME`.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

import pytest

from wall_in_one import file_io, paths
from wall_in_one.library import manage, pairing, scan, stills
from wall_in_one.library.manage import ManageError
from wall_in_one.library.model import Kind, MediaItem, Ownership

MARKER = ".managed-by-wall-in-one-v1.json"
REMOVAL_TOKEN = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A trash of our own, so no test can reach the real one."""
    home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(home))
    return home


@pytest.fixture
def root(tmp_path: Path) -> Path:
    directory = tmp_path / "wallpapers"
    directory.mkdir()
    return directory


def managed_directory(root: Path, name: str = "Wallhaven") -> Path:
    """A directory carrying the marker that says this app created it."""
    directory = root / "Wall-in-One" / name
    directory.mkdir(parents=True)
    if name == "MotionBGS":
        marker = directory / ".wall-in-one-motionbgs-managed.json"
        payload = {
            "schema": 1,
            "owner": "goober/wall-in-one",
            "provider": "MotionBGS",
        }
    else:
        marker = directory / MARKER
        payload = {
            "schema": 1,
            "plugin": "goober/wall-in-one",
            "provider": "Wallhaven",
            "kind": "wallhaven",
            "ownership": "managed",
        }
    marker.write_text(json.dumps(payload), encoding="utf-8")
    return directory


def provenance(path: Path, provider: str) -> str:
    status = path.stat()
    contents = path.read_bytes()
    return json.dumps(
        {
            "schema": 1,
            "plugin": "goober/wall-in-one",
            "provider": provider,
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
    )


def downloaded(root: Path, name: str = "picture.jpg") -> MediaItem:
    """A wallpaper with both halves of ownership: marker and sidecar."""
    directory = managed_directory(root)
    path = directory / name
    path.write_bytes(b"\xff\xd8\xff" + b"0" * 64)
    path.with_name(path.name + ".wallhaven.json").write_text(
        provenance(path, "Wallhaven"), encoding="utf-8"
    )
    return item_for(path, Kind.STILL, Ownership.MANAGED)


def item_for(
    path: Path,
    kind: Kind = Kind.STILL,
    ownership: Ownership = Ownership.USER,
    still: Path | None = None,
    provider: str = "local",
) -> MediaItem:
    status = path.stat()
    return MediaItem(
        path=path,
        kind=kind,
        size=status.st_size,
        mtime=int(status.st_mtime),
        ownership=ownership,
        paired_still=still,
        provider=provider,
    )


def _expected(path: Path) -> manage.SourceIdentity:
    try:
        status = path.lstat()
    except OSError:
        return 0, 0
    return status.st_dev, status.st_ino


def _fingerprint(path: Path) -> manage.SourceFingerprint | None:
    try:
        return file_io.regular_file_fingerprint(path)
    except OSError:
        return None


def _remove(item: MediaItem, roots: tuple[Path, ...] = ()) -> manage.Removal:
    """Unit-level authority matching the identity a journal prepare records."""
    return manage.remove(
        item,
        roots,
        expected_source=_expected(item.path),
        expected_fingerprint=_fingerprint(item.path),
        operation_token=REMOVAL_TOKEN,
    )


def _trash(item: MediaItem, roots: tuple[Path, ...] = ()) -> manage.Trashed:
    """Unit-level authority matching the identity a journal prepare records."""
    return manage.trash(
        item,
        roots,
        expected_source=_expected(item.path),
        expected_fingerprint=_fingerprint(item.path),
    )


# -- what it removes ------------------------------------------------------


def test_a_downloaded_wallpaper_goes_away(root: Path) -> None:
    item = downloaded(root)
    result = _remove(item, (root,))
    assert not item.path.exists()
    assert item.path in result.removed


def test_remove_refuses_a_same_path_replacement_after_prepare(root: Path) -> None:
    item = downloaded(root)
    expected = _expected(item.path)
    expected_fingerprint = _fingerprint(item.path)
    original = item.path.with_name("original.jpg")
    item.path.rename(original)
    replacement = item.path
    replacement.write_bytes(b"different download")

    with pytest.raises(ManageError) as caught:
        manage.remove(
            item,
            (root,),
            expected_source=expected,
            expected_fingerprint=expected_fingerprint,
            operation_token=REMOVAL_TOKEN,
        )

    assert caught.value.kind == "changed"
    assert replacement.read_bytes() == b"different download"


def test_remove_restores_a_replacement_that_arrives_during_the_atomic_claim(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = downloaded(root)
    expected = _expected(item.path)
    expected_fingerprint = _fingerprint(item.path)
    original = root / "prepared-original.jpg"
    claimed = file_io.deletion_claim_directory(item.path, REMOVAL_TOKEN) / "entry"
    real_rename = file_io._rename_noreplace
    real_lstat = Path.lstat
    raced = False

    def replace_then_rename(source: Path, destination: Path) -> None:
        nonlocal raced
        if source == item.path and not raced:
            raced = True
            source.rename(original)
            source.write_bytes(b"late replacement")
        real_rename(source, destination)

    def spoof_reused_identity(candidate: Path) -> os.stat_result:
        status = real_lstat(candidate)
        if candidate == claimed and raced:
            fields = list(status)
            fields[stat.ST_DEV] = expected[0]
            fields[stat.ST_INO] = expected[1]
            return os.stat_result(fields)
        return status

    monkeypatch.setattr(file_io, "_rename_noreplace", replace_then_rename)
    monkeypatch.setattr(Path, "lstat", spoof_reused_identity)

    with pytest.raises(ManageError) as caught:
        manage.remove(
            item,
            (root,),
            expected_source=expected,
            expected_fingerprint=expected_fingerprint,
            operation_token=REMOVAL_TOKEN,
        )

    assert caught.value.kind == "changed"
    assert item.path.read_bytes() == b"late replacement"
    assert original.read_bytes().startswith(b"\xff\xd8\xff")


def test_remove_retains_its_journal_when_restore_conflict_preserves_a_replacement(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = downloaded(root)
    expected = _expected(item.path)
    expected_fingerprint = _fingerprint(item.path)
    original = root / "prepared-original.jpg"
    claim = file_io.deletion_claim_directory(item.path, REMOVAL_TOKEN) / "entry"
    real_rename = file_io._rename_noreplace
    raced = False

    def move_replacement_then_reoccupy(source: Path, destination: Path) -> None:
        nonlocal raced
        if source == item.path and not raced:
            raced = True
            source.rename(original)
            source.write_bytes(b"replacement B")
            real_rename(source, destination)
            source.write_bytes(b"replacement C")
            return
        real_rename(source, destination)

    monkeypatch.setattr(file_io, "_rename_noreplace", move_replacement_then_reoccupy)

    with pytest.raises(ManageError) as caught:
        manage.remove(
            item,
            (root,),
            expected_source=expected,
            expected_fingerprint=expected_fingerprint,
            operation_token=REMOVAL_TOKEN,
        )

    assert caught.value.kind == "changed"
    assert not caught.value.committed
    assert caught.value.retain_intent
    assert str(claim) in str(caught.value)
    assert item.path.read_bytes() == b"replacement C"
    assert claim.read_bytes() == b"replacement B"
    assert original.read_bytes().startswith(b"\xff\xd8\xff")


def test_remove_retains_its_journal_when_a_claimed_file_cannot_be_discarded_or_restored(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = downloaded(root)
    expected_bytes = item.path.read_bytes()
    claim_path = file_io.deletion_claim_directory(item.path, REMOVAL_TOKEN) / "entry"
    real_discard = file_io.ClaimedPath.discard

    def fail_after_reoccupying_source(claim: file_io.ClaimedPath) -> None:
        if claim.original != item.path:
            real_discard(claim)
            return
        claim.original.write_bytes(b"replacement C")
        raise PermissionError("injected claim unlink failure")

    monkeypatch.setattr(file_io.ClaimedPath, "discard", fail_after_reoccupying_source)

    with pytest.raises(ManageError) as caught:
        _remove(item, (root,))

    assert caught.value.kind == "local-io"
    assert not caught.value.committed
    assert caught.value.retain_intent
    assert str(claim_path) in str(caught.value)
    assert item.path.read_bytes() == b"replacement C"
    assert claim_path.read_bytes() == expected_bytes


def test_lifecycle_teardown_failure_preserves_a_claim_recovery_intent(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = downloaded(root)
    expected_bytes = item.path.read_bytes()
    claim_path = file_io.deletion_claim_directory(item.path, REMOVAL_TOKEN) / "entry"
    real_discard = file_io.ClaimedPath.discard

    def fail_after_reoccupying_source(claim: file_io.ClaimedPath) -> None:
        if claim.original != item.path:
            real_discard(claim)
            return
        claim.original.write_bytes(b"replacement")
        raise PermissionError("injected claim unlink failure")

    @contextlib.contextmanager
    def failing_lifecycle(_item: MediaItem) -> Iterator[None]:
        try:
            yield
        finally:
            raise OSError("injected lifecycle-lock close failure")

    monkeypatch.setattr(file_io.ClaimedPath, "discard", fail_after_reoccupying_source)
    monkeypatch.setattr(stills, "source_lifecycle_lock", failing_lifecycle)

    with pytest.raises(ManageError) as caught:
        _remove(item, (root,))

    assert caught.value.kind == "local-io"
    assert not caught.value.committed
    assert caught.value.retain_intent
    assert claim_path.read_bytes() == expected_bytes
    assert item.path.read_bytes() == b"replacement"


@pytest.mark.parametrize("move_to_trash", (False, True))
def test_lifecycle_teardown_failure_preserves_the_committed_physical_outcome(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    move_to_trash: bool,
) -> None:
    if move_to_trash:
        source = root / "holiday.png"
        source.write_bytes(b"personal image")
        item = item_for(source)
    else:
        item = downloaded(root)

    @contextlib.contextmanager
    def failing_lifecycle(_item: MediaItem) -> Iterator[None]:
        try:
            yield
        finally:
            raise OSError("injected post-commit lifecycle-lock close failure")

    monkeypatch.setattr(stills, "source_lifecycle_lock", failing_lifecycle)

    with pytest.raises(ManageError) as caught:
        if move_to_trash:
            _trash(item, (root,))
        else:
            _remove(item, (root,))

    assert caught.value.kind == "local-io"
    assert caught.value.committed
    assert caught.value.physical is not None
    assert caught.value.physical.item == item
    if move_to_trash:
        assert isinstance(caught.value.physical, manage.Trashed)
        assert caught.value.physical.destination.read_bytes() == b"personal image"
    else:
        assert isinstance(caught.value.physical, manage.Removal)
        assert item.path in caught.value.physical.removed
    assert not item.path.exists()


@pytest.mark.parametrize("move_to_trash", (False, True))
def test_committed_operation_ignores_a_source_pin_close_failure_and_closes_both_owners(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    move_to_trash: bool,
) -> None:
    downloaded_item = downloaded(root)
    item = item_for(downloaded_item.path) if move_to_trash else downloaded_item
    root_status = root.lstat()
    parent_status = item.path.parent.lstat()
    context = file_io.pin_directory_beneath(
        root,
        item.path.parent,
        expected_root_identity=(root_status.st_dev, root_status.st_ino),
        expected_directory_identity=(parent_status.st_dev, parent_status.st_ino),
    )
    prepared = file_io.pin_regular_path(item.path)
    expected_identity = prepared.identity
    expected_fingerprint = prepared.fingerprint
    lookup = context.child(item.path.name)
    real_close = file_io.PinnedPath.close
    failed = False
    retained_close_count = 0
    closed: list[Path] = []

    def fail_after_closing_retained_source(pin: file_io.PinnedPath) -> None:
        nonlocal failed, retained_close_count
        closed.append(pin.path)
        real_close(pin)
        if pin.path == lookup:
            retained_close_count += 1
        expected_close = 1 if move_to_trash else 3
        if pin.path == lookup and retained_close_count == expected_close and not failed:
            failed = True
            raise OSError("injected source-pin close failure after release")

    monkeypatch.setattr(file_io.PinnedPath, "close", fail_after_closing_retained_source)
    options = {
        "expected_source": expected_identity,
        "expected_fingerprint": expected_fingerprint,
        "prepared_pin": prepared,
        "lookup_path": lookup,
        "source_root": root,
        "lookup_root": context.root_anchor,
        "lookup_parent": context.directory_anchor,
        "source_context": context,
    }
    try:
        if move_to_trash:
            trashed = manage.trash(item, (root,), **options)  # type: ignore[arg-type]
            assert trashed.destination.is_file()
        else:
            removed = manage.remove(
                item,
                (root,),
                operation_token=REMOVAL_TOKEN,
                **options,  # type: ignore[arg-type]
            )
            assert item.path in removed.removed
    finally:
        context.close()

    assert failed
    assert lookup in closed
    assert item.path in closed
    with pytest.raises(OSError, match="closed"):
        prepared.status()
    assert not item.path.exists()


def test_remove_withdraws_download_authority_before_the_media_claim(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = downloaded(root)
    sidecar = item.path.with_name(item.path.name + ".wallhaven.json")
    real_claim = file_io.claim_for_deletion

    def stop_at_media_claim(path: Path, **options: object) -> file_io.ClaimedPath:
        if path.name == item.path.name:
            assert not sidecar.exists()
            raise OSError("injected crash boundary before media claim")
        return real_claim(path, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(file_io, "claim_for_deletion", stop_at_media_claim)

    with pytest.raises(ManageError) as caught:
        _remove(item, (root,))

    assert caught.value.kind == "local-io"
    assert item.path.is_file()
    assert not sidecar.exists()
    assert not scan.has_download_sidecar(item.path)


def test_remove_withdraws_every_valid_download_authority(root: Path) -> None:
    item = downloaded(root)
    wallhaven = item.path.with_name(item.path.name + ".wallhaven.json")
    motionbgs = item.path.with_name(item.path.name + ".motionbgs.json")
    motionbgs.write_text(provenance(item.path, "MotionBGS"), encoding="utf-8")

    result = _remove(item, (root,))

    assert not item.path.exists()
    assert not wallhaven.exists()
    assert not motionbgs.exists()
    assert {wallhaven, motionbgs}.issubset(result.removed)
    item.path.write_bytes(b"unrelated later media")
    assert not scan.has_download_sidecar(item.path)


def test_remove_requires_exact_media_digest_before_withdrawing_authority(root: Path) -> None:
    item = downloaded(root)
    sidecar = item.path.with_name(item.path.name + ".wallhaven.json")
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    document["sha256"] = "0" * 64
    assert document["sha256"] != hashlib.sha256(item.path.read_bytes()).hexdigest()
    sidecar.write_text(json.dumps(document), encoding="utf-8")
    # Scanning deliberately uses the cheap generation binding. Destruction
    # must additionally hash the exact retained media before trusting it.
    assert scan.download_provenance(item.path) == "Wallhaven"

    with pytest.raises(ManageError) as caught:
        _remove(item, (root,))

    assert caught.value.kind == "changed"
    assert item.path.is_file()
    assert sidecar.is_file()


def test_remove_rejects_same_inode_lifecycle_with_restored_size_and_mtime(root: Path) -> None:
    item = downloaded(root)
    original = item.path.read_bytes()
    original_mtime_ns = item.path.stat().st_mtime_ns
    writer = item.path.with_name("writer-hardlink")
    writer.hardlink_to(item.path)
    replacement = bytes(byte ^ 0xFF for byte in original)
    assert len(replacement) == len(original) and replacement != original
    writer.write_bytes(replacement)
    os.utime(writer, ns=(original_mtime_ns, original_mtime_ns))
    # Device, inode, size and mtime still match the durable authority. The
    # ctime binding rejects this changed lifecycle before deletion; the exact
    # retained-content hash remains defense in depth for authority documents.
    assert scan.download_provenance(item.path) is None

    with pytest.raises(ManageError) as caught:
        _remove(item, (root,))

    assert caught.value.kind == "not-ours"
    assert item.path.read_bytes() == replacement
    assert writer.read_bytes() == replacement


def test_remove_rejects_byte_identical_media_after_a_ctime_only_change(root: Path) -> None:
    item = downloaded(root)
    sidecar = item.path.with_name(item.path.name + ".wallhaven.json")
    before = item.path.stat()
    contents = item.path.read_bytes()

    original_mode = stat.S_IMODE(before.st_mode)
    item.path.chmod(original_mode ^ stat.S_IXUSR)
    item.path.chmod(original_mode)
    after = item.path.stat()

    assert item.path.read_bytes() == contents
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    assert after.st_ctime_ns != before.st_ctime_ns
    assert scan.download_provenance(item.path) is None

    with pytest.raises(ManageError) as caught:
        _remove(item, (root,))

    assert caught.value.kind == "not-ours"
    assert item.path.read_bytes() == contents
    assert sidecar.is_file()


def test_trash_preserves_provider_document_bound_to_another_generation(root: Path) -> None:
    path = root / "holiday.png"
    path.write_bytes(b"user media")
    unrelated = root / "other.png"
    unrelated.write_bytes(b"provider media")
    document = json.loads(provenance(unrelated, "Wallhaven"))
    document["path"] = str(path)
    sidecar = path.with_name(path.name + ".wallhaven.json")
    sidecar.write_text(json.dumps(document), encoding="utf-8")

    result = _trash(item_for(path), (root,))

    assert result.destination.read_bytes() == b"user media"
    assert sidecar.is_file()


def test_remove_validates_provenance_from_the_exact_pinned_sidecar_generation(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = downloaded(root)
    sidecar = item.path.with_name(item.path.name + ".wallhaven.json")
    valid_generation = sidecar.with_name("saved-valid.wallhaven.json")
    invalid_generation = sidecar.with_name("saved-invalid.wallhaven.json")
    real_read = file_io.read_regular_bytes
    real_pin = file_io.pin_regular_path
    sidecar_reads = 0
    swapped = False

    def pathname_read(path: Path, maximum: int) -> bytes | None:
        nonlocal sidecar_reads
        if path == sidecar:
            sidecar_reads += 1
            if sidecar_reads >= 3:
                # A pathname-bracketed validator could read valid inode A and
                # then see pinned invalid inode B at the same name. The exact
                # retained reader below never reaches this ABA branch.
                path.rename(invalid_generation)
                valid_generation.rename(path)
                try:
                    return real_read(path, maximum)
                finally:
                    path.rename(valid_generation)
                    invalid_generation.rename(path)
        return real_read(path, maximum)

    def pin_invalid_sidecar(
        path: Path,
        *,
        expected_identity: file_io.PathIdentity | None = None,
        expected_fingerprint: file_io.FileFingerprint | None = None,
    ) -> file_io.PinnedPath:
        nonlocal swapped
        if path.name == sidecar.name and not swapped:
            swapped = True
            path.rename(valid_generation)
            path.write_bytes(b"{}")
        return real_pin(
            path,
            expected_identity=expected_identity,
            expected_fingerprint=expected_fingerprint,
        )

    monkeypatch.setattr(file_io, "read_regular_bytes", pathname_read)
    monkeypatch.setattr(file_io, "pin_regular_path", pin_invalid_sidecar)

    with pytest.raises(ManageError) as caught:
        _remove(item, (root,))

    assert caught.value.kind == "not-ours"
    assert swapped
    assert sidecar_reads == 1
    assert item.path.is_file()
    assert sidecar.read_bytes() == b"{}"
    assert valid_generation.is_file()


def test_trash_withdraws_new_download_authority_before_the_media_move(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed = downloaded(root)
    item = item_for(managed.path)
    sidecar = item.path.with_name(item.path.name + ".wallhaven.json")
    real_move = file_io.atomic_move_no_replace

    def stop_at_media_move(source: Path, destination: Path, **options: object) -> None:
        if source == item.path:
            assert not sidecar.exists()
            raise OSError("injected crash boundary before trash move")
        real_move(source, destination, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(file_io, "atomic_move_no_replace", stop_at_media_move)

    with pytest.raises(ManageError) as caught:
        _trash(item, (root,))

    assert caught.value.kind == "local-io"
    assert item.path.is_file()
    assert not sidecar.exists()
    assert not scan.has_download_sidecar(item.path)


def test_remove_without_a_prepared_source_identity_is_refused(root: Path) -> None:
    item = downloaded(root)

    with pytest.raises(ManageError) as caught:
        manage.remove(item, (root,))

    assert caught.value.kind == "unrecorded"
    assert item.path.is_file()


_CRASH_AFTER_REMOVAL_CLAIM = r"""
import os
import sys
from pathlib import Path
from wall_in_one import file_io

path = Path(sys.argv[1])
identity = (int(sys.argv[2]), int(sys.argv[3]))
file_io.claim_for_deletion(
    path,
    expected_identity=identity,
    operation_token=sys.argv[4],
)
os._exit(77)
"""


def test_process_death_after_media_claim_is_replayable(root: Path) -> None:
    item = downloaded(root)
    expected = _expected(item.path)
    expected_fingerprint = _fingerprint(item.path)
    project = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(project / "src") + (os.pathsep + existing if existing else "")

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _CRASH_AFTER_REMOVAL_CLAIM,
            str(item.path),
            str(expected[0]),
            str(expected[1]),
            REMOVAL_TOKEN,
        ],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 77, completed.stderr
    claim_directory = file_io.deletion_claim_directory(item.path, REMOVAL_TOKEN)
    assert not item.path.exists()
    assert (claim_directory / "entry").is_file()
    assert manage.recover_removal_claim(
        item.path,
        expected_source=expected,
        expected_fingerprint=expected_fingerprint,
        operation_token=REMOVAL_TOKEN,
    )
    assert claim_directory.is_dir()
    assert tuple(claim_directory.iterdir()) == ()


def test_the_sidecar_goes_with_it(root: Path) -> None:
    """Left behind, it would be an orphan claiming a file that is not there."""
    item = downloaded(root)
    sidecar = item.path.with_name(item.path.name + ".wallhaven.json")
    _remove(item, (root,))
    assert not sidecar.exists()


def test_an_unrelated_provider_named_json_is_not_removed_as_a_companion(root: Path) -> None:
    item = downloaded(root)
    unrelated = item.path.with_name(item.path.name + ".motionbgs.json")
    unrelated.write_text("{}", encoding="utf-8")

    _remove(item, (root,))

    assert unrelated.read_text(encoding="utf-8") == "{}"


def test_the_marker_stays_because_the_directory_is_still_ours(root: Path) -> None:
    item = downloaded(root)
    _remove(item, (root,))
    assert (item.path.parent / MARKER).is_file()


def test_a_generated_still_goes_with_its_video(root: Path) -> None:
    directory = managed_directory(root, "MotionBGS")
    video = directory / "clip.mp4"
    video.write_bytes(b"0" * 64)
    video.with_name(video.name + ".motionbgs.json").write_text(
        provenance(video, "MotionBGS"), encoding="utf-8"
    )
    still_directory = pairing.still_directory(root)
    still_directory.mkdir(parents=True)
    still = still_directory / f"{pairing.automatic_still_stem(video)}.png"
    still.write_bytes(b"\x89PNG\r\n\x1a\n")
    still_sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    still_sidecar.write_text(json.dumps({"still_path": str(still)}), encoding="utf-8")

    _remove(item_for(video, Kind.VIDEO, Ownership.MANAGED, still), (root,))

    assert not video.exists()
    assert not still.exists()
    assert not still_sidecar.exists()


def test_an_old_generated_still_is_removed_even_when_a_custom_still_is_selected(
    root: Path,
) -> None:
    """The resolved choice must not hide an app-owned capture during cleanup."""
    directory = managed_directory(root, "MotionBGS")
    video = directory / "clip.mp4"
    video.write_bytes(b"0" * 64)
    video.with_name(video.name + ".motionbgs.json").write_text(
        provenance(video, "MotionBGS"), encoding="utf-8"
    )
    automatic = pairing.still_directory(root) / (f"{pairing.automatic_still_stem(video)}.png")
    automatic.parent.mkdir(parents=True)
    automatic.write_bytes(b"\x89PNG\r\n\x1a\n")
    sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    sidecar.write_text(json.dumps({"still_path": str(automatic)}), encoding="utf-8")
    custom = root / "my-chosen-frame.png"
    custom.write_bytes(b"\x89PNG\r\n\x1a\n")

    _remove(item_for(video, Kind.VIDEO, Ownership.MANAGED, custom), (root,))

    assert not automatic.exists()
    assert not sidecar.exists()
    assert custom.is_file(), "a representative image the user chose is not app-owned"


def test_trashing_a_user_video_cleans_only_its_app_owned_pairing_artifacts(
    root: Path,
) -> None:
    video = root / "clip.mp4"
    video.write_bytes(b"0" * 64)
    automatic = pairing.still_directory(root) / (f"{pairing.automatic_still_stem(video)}.png")
    automatic.parent.mkdir(parents=True)
    automatic.write_bytes(b"\x89PNG\r\n\x1a\n")
    sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    sidecar.write_text(json.dumps({"still_path": str(automatic)}), encoding="utf-8")
    custom = root / "chosen.png"
    custom.write_bytes(b"\x89PNG\r\n\x1a\n")
    media = item_for(video, Kind.VIDEO, Ownership.USER, custom)

    landed = _trash(media, (root,))

    assert landed.destination.is_file()
    assert not automatic.exists()
    assert not sidecar.exists()
    assert custom.is_file()


def test_delete_that_wins_during_video_capture_prevents_late_still_publication(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffmpeg stays outside the lifecycle lock, then fails closed at commit."""
    video = root / "clip.mp4"
    video.write_bytes(b"video source")
    media = item_for(video, Kind.VIDEO)
    captured = threading.Event()
    release_capture = threading.Event()

    def held_capture(
        _video: Path,
        temporary: Path,
        _seek: float,
        *,
        processes: object = None,
    ) -> str:
        del processes
        temporary.write_bytes(b"\x89PNG\r\n\x1a\nrendered")
        captured.set()
        assert release_capture.wait(5), "test did not release the held ffmpeg capture"
        return ""

    monkeypatch.setattr(stills, "is_available", lambda: True)
    monkeypatch.setattr(stills, "_run", held_capture)
    target = stills.destination(video, root)

    with ThreadPoolExecutor(max_workers=1) as pool:
        generated = pool.submit(stills.generate, video, root)
        assert captured.wait(5), "capture did not reach its intentionally unlocked wait"
        try:
            trashed = _trash(media, (root,))
        finally:
            release_capture.set()
        with pytest.raises(stills.StillError, match="removed while its still was being made"):
            generated.result(timeout=5)

    assert trashed.destination.is_file()
    assert not target.exists()
    assert not video.with_name(video.name + pairing.SIDECAR_SUFFIX).exists()
    assert not any(path.name.endswith(".tmp.png") for path in target.parent.iterdir())


def test_workshop_delete_that_wins_during_scene_capture_prevents_late_publication(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed scene uninstall and screenshot publication share one lease."""
    scene_id = "1647046763"
    installation = root.parent / "steam" / "workshop" / "content" / "431960" / scene_id
    installation.mkdir(parents=True)
    scene = MediaItem(
        path=installation,
        kind=Kind.SCENE,
        size=installation.stat().st_size,
        mtime=int(installation.stat().st_mtime),
        provider=scan.WORKSHOP_PROVIDER,
        scene=scene_id,
    )
    captured = threading.Event()
    release_capture = threading.Event()

    def held_capture(
        _scene_id: str,
        temporary: Path,
        *,
        size: tuple[int, int],
        prepared_output: bool,
    ) -> Path:
        del size
        assert prepared_output
        temporary.write_bytes(b"\x89PNG\r\n\x1a\nrendered")
        captured.set()
        assert release_capture.wait(5), "test did not release the held scene capture"
        return temporary

    monkeypatch.setattr("wall_in_one.wallpaper.scenes.screenshot", held_capture)
    target = pairing.still_directory(root) / f"{scene_id}.png"

    with ThreadPoolExecutor(max_workers=1) as pool:
        generated = pool.submit(stills.capture_scene, scene, root)
        assert captured.wait(5), "capture did not reach its intentionally unlocked wait"
        installation.rmdir()
        try:
            assert manage.discard_pairing_artifacts(scene, (root,)) == ((), ())
        finally:
            release_capture.set()
        with pytest.raises(stills.StillError, match="removed while its still was being made"):
            generated.result(timeout=5)

    assert not target.exists()
    assert not any(path.name.endswith(".tmp.png") for path in target.parent.iterdir())


def test_cleanup_waits_for_a_video_publication_commit_then_removes_its_artifacts(
    root: Path,
) -> None:
    """If publication owns the lease first, cleanup observes all of its output."""
    video = root / "clip.mp4"
    video.write_bytes(b"video source")
    media = item_for(video, Kind.VIDEO)
    target = stills.destination(video, root)
    target.parent.mkdir(parents=True)
    sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    cleanup_started = threading.Event()

    def cleanup() -> tuple[tuple[Path, ...], tuple[Path, ...]]:
        cleanup_started.set()
        return manage.discard_pairing_artifacts(media, (root,))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with stills.source_lifecycle_lock(media):
            # External removal may commit while publication holds the lease;
            # its artifact phase must wait until the whole commit is visible.
            target.write_bytes(b"\x89PNG\r\n\x1a\npublished")
            stills.write_sidecar(video, target)
            video.unlink()
            cleaned = pool.submit(cleanup)
            assert cleanup_started.wait(5)
            assert not cleaned.done()
        removed, kept = cleaned.result(timeout=5)

    assert kept == ()
    assert set(removed) == {target, sidecar}
    assert not target.exists()
    assert not sidecar.exists()


def test_lifecycle_lock_timeout_retains_future_candidates_for_journal_retry(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stuck publisher cannot hang removal or make a later orphan invisible."""
    video = root / "clip.mp4"
    video.write_bytes(b"video source")
    media = item_for(video, Kind.VIDEO)
    cleanup_started = threading.Event()
    monkeypatch.setattr(stills, "LIFECYCLE_LOCK_TIMEOUT_SECONDS", 0.05)

    def cleanup() -> tuple[tuple[Path, ...], tuple[Path, ...]]:
        cleanup_started.set()
        return manage.discard_pairing_artifacts(media, (root,))

    with ThreadPoolExecutor(max_workers=1) as pool, stills.source_lifecycle_lock(media):
        pending = pool.submit(cleanup)
        assert cleanup_started.wait(5)
        removed, kept = pending.result(timeout=2)

    assert removed == ()
    assert set(kept) == set(manage.pairing_artifact_paths(media, (root,)))


def test_secondary_root_source_cleans_a_primary_root_automatic_still(
    root: Path,
    tmp_path: Path,
) -> None:
    primary = root
    secondary = tmp_path / "secondary-wallpapers"
    secondary.mkdir()
    video = secondary / "clip.mp4"
    video.write_bytes(b"video source")
    media = item_for(video, Kind.VIDEO)
    target = stills.destination(video, primary)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\nautomatic")
    root_status = secondary.lstat()
    parent_status = video.parent.lstat()
    context = file_io.pin_directory_beneath(
        secondary,
        video.parent,
        expected_root_identity=(root_status.st_dev, root_status.st_ino),
        expected_directory_identity=(parent_status.st_dev, parent_status.st_ino),
    )
    try:
        removed, kept = manage.discard_pairing_artifacts(
            media,
            (primary, secondary),
            source_root=secondary,
            lookup_root=context.root_anchor,
            lookup_parent=context.directory_anchor,
            source_context=context,
        )
    finally:
        context.close()

    assert removed == (target,)
    assert kept == ()
    assert not target.exists()


def test_cross_root_artifact_parent_rebind_never_mixes_directory_generations(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = root
    secondary = tmp_path / "secondary-wallpapers"
    secondary.mkdir()
    video = secondary / "clip.mp4"
    video.write_bytes(b"video source")
    media = item_for(video, Kind.VIDEO)
    target = stills.destination(video, primary)
    sidecar = target.with_name(target.name + pairing.SIDECAR_SUFFIX)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old automatic still")
    sidecar.write_bytes(b"old automatic sidecar")
    saved_parent = target.parent.with_name("saved-automatic-stills")
    replacement_target = b"replacement automatic still"
    replacement_sidecar = b"replacement automatic sidecar"
    real_access = manage._independently_pinned_artifact_access
    swapped = False

    def swap_after_first_access(
        candidate: Path,
        roots: tuple[Path, ...],
        *,
        access_stack: contextlib.ExitStack,
        parent_contexts: dict[Path, file_io.PinnedDirectoryContext | None],
        root_pins: dict[Path, file_io.PinnedPath | None],
    ) -> Path:
        nonlocal swapped
        access = real_access(
            candidate,
            roots,
            access_stack=access_stack,
            parent_contexts=parent_contexts,
            root_pins=root_pins,
        )
        if candidate == target and not swapped:
            swapped = True
            target.parent.rename(saved_parent)
            target.parent.mkdir()
            target.write_bytes(replacement_target)
            sidecar.write_bytes(replacement_sidecar)
        return access

    monkeypatch.setattr(manage, "_independently_pinned_artifact_access", swap_after_first_access)
    root_status = secondary.lstat()
    parent_status = video.parent.lstat()
    context = file_io.pin_directory_beneath(
        secondary,
        video.parent,
        expected_root_identity=(root_status.st_dev, root_status.st_ino),
        expected_directory_identity=(parent_status.st_dev, parent_status.st_ino),
    )
    try:
        removed, kept = manage.discard_pairing_artifacts(
            media,
            (primary, secondary),
            source_root=secondary,
            lookup_root=context.root_anchor,
            lookup_parent=context.directory_anchor,
            source_context=context,
        )
    finally:
        context.close()

    assert swapped
    assert removed == (target,)
    assert kept == (sidecar,)
    assert target.read_bytes() == replacement_target
    assert sidecar.read_bytes() == replacement_sidecar
    assert (saved_parent / sidecar.name).read_bytes() == b"old automatic sidecar"


def test_cross_root_missing_parent_cannot_admit_a_later_sibling_generation(
    root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = root
    secondary = tmp_path / "secondary-wallpapers"
    secondary.mkdir()
    video = secondary / "clip.mp4"
    video.write_bytes(b"video source")
    media = item_for(video, Kind.VIDEO)
    target = stills.destination(video, primary)
    sidecar = target.with_name(target.name + pairing.SIDECAR_SUFFIX)
    real_access = manage._independently_pinned_artifact_access
    raced = False

    def create_parent_after_absence(
        candidate: Path,
        roots: tuple[Path, ...],
        *,
        access_stack: contextlib.ExitStack,
        parent_contexts: dict[Path, file_io.PinnedDirectoryContext | None],
        root_pins: dict[Path, file_io.PinnedPath | None],
    ) -> Path:
        nonlocal raced
        try:
            return real_access(
                candidate,
                roots,
                access_stack=access_stack,
                parent_contexts=parent_contexts,
                root_pins=root_pins,
            )
        except FileNotFoundError:
            if candidate == target and not raced:
                raced = True
                target.parent.mkdir(parents=True)
                sidecar.write_bytes(b"later automatic sidecar generation")
            raise

    monkeypatch.setattr(
        manage,
        "_independently_pinned_artifact_access",
        create_parent_after_absence,
    )
    root_status = secondary.lstat()
    parent_status = video.parent.lstat()
    context = file_io.pin_directory_beneath(
        secondary,
        video.parent,
        expected_root_identity=(root_status.st_dev, root_status.st_ino),
        expected_directory_identity=(parent_status.st_dev, parent_status.st_ino),
    )
    try:
        removed, kept = manage.discard_pairing_artifacts(
            media,
            (primary, secondary),
            source_root=secondary,
            lookup_root=context.root_anchor,
            lookup_parent=context.directory_anchor,
            source_context=context,
        )
    finally:
        context.close()

    assert raced
    assert removed == ()
    assert kept == (target, sidecar)
    assert sidecar.read_bytes() == b"later automatic sidecar generation"


def test_cross_root_missing_root_cannot_admit_a_later_sibling_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = tmp_path / "offline-primary"
    secondary = tmp_path / "secondary-wallpapers"
    secondary.mkdir()
    video = secondary / "clip.mp4"
    video.write_bytes(b"video source")
    media = item_for(video, Kind.VIDEO)
    target = stills.destination(video, primary)
    sidecar = target.with_name(target.name + pairing.SIDECAR_SUFFIX)
    real_access = manage._independently_pinned_artifact_access
    raced = False

    def create_root_after_absence(
        candidate: Path,
        roots: tuple[Path, ...],
        *,
        access_stack: contextlib.ExitStack,
        parent_contexts: dict[Path, file_io.PinnedDirectoryContext | None],
        root_pins: dict[Path, file_io.PinnedPath | None],
    ) -> Path:
        nonlocal raced
        try:
            return real_access(
                candidate,
                roots,
                access_stack=access_stack,
                parent_contexts=parent_contexts,
                root_pins=root_pins,
            )
        except FileNotFoundError:
            if candidate == target and not raced:
                raced = True
                target.parent.mkdir(parents=True)
                sidecar.write_bytes(b"later root sidecar generation")
            raise

    monkeypatch.setattr(
        manage,
        "_independently_pinned_artifact_access",
        create_root_after_absence,
    )
    root_status = secondary.lstat()
    parent_status = video.parent.lstat()
    context = file_io.pin_directory_beneath(
        secondary,
        video.parent,
        expected_root_identity=(root_status.st_dev, root_status.st_ino),
        expected_directory_identity=(parent_status.st_dev, parent_status.st_ino),
    )
    try:
        removed, kept = manage.discard_pairing_artifacts(
            media,
            (primary, secondary),
            source_root=secondary,
            lookup_root=context.root_anchor,
            lookup_parent=context.directory_anchor,
            source_context=context,
        )
    finally:
        context.close()

    assert raced
    assert removed == ()
    assert kept == (target, sidecar)
    assert sidecar.read_bytes() == b"later root sidecar generation"


def test_retained_root_artifact_parent_rebind_never_mixes_generations(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video_parent = root / "videos"
    video_parent.mkdir()
    video = video_parent / "clip.mp4"
    video.write_bytes(b"video source")
    media = item_for(video, Kind.VIDEO)
    target = stills.destination(video, root)
    sidecar = target.with_name(target.name + pairing.SIDECAR_SUFFIX)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old automatic still")
    sidecar.write_bytes(b"old automatic sidecar")
    saved_parent = target.parent.with_name("saved-source-root-stills")
    replacement_target = b"replacement automatic still"
    replacement_sidecar = b"replacement automatic sidecar"
    real_access = manage._retained_artifact_access_path
    swapped = False

    def swap_after_first_access(
        candidate: Path,
        *,
        source_root: Path | None,
        lookup_root: Path | None,
        source_parent: Path,
        lookup_parent: Path | None,
        source_context: file_io.PinnedDirectoryContext | None,
        access_stack: contextlib.ExitStack,
        parent_accesses: dict[Path, Path | None],
    ) -> Path | None:
        nonlocal swapped
        access = real_access(
            candidate,
            source_root=source_root,
            lookup_root=lookup_root,
            source_parent=source_parent,
            lookup_parent=lookup_parent,
            source_context=source_context,
            access_stack=access_stack,
            parent_accesses=parent_accesses,
        )
        if candidate == target and not swapped:
            swapped = True
            target.parent.rename(saved_parent)
            target.parent.mkdir()
            target.write_bytes(replacement_target)
            sidecar.write_bytes(replacement_sidecar)
        return access

    monkeypatch.setattr(manage, "_retained_artifact_access_path", swap_after_first_access)
    root_status = root.lstat()
    parent_status = video.parent.lstat()
    context = file_io.pin_directory_beneath(
        root,
        video.parent,
        expected_root_identity=(root_status.st_dev, root_status.st_ino),
        expected_directory_identity=(parent_status.st_dev, parent_status.st_ino),
    )
    try:
        removed, kept = manage.discard_pairing_artifacts(
            media,
            (root,),
            source_root=root,
            lookup_root=context.root_anchor,
            lookup_parent=context.directory_anchor,
            source_context=context,
        )
    finally:
        context.close()

    assert swapped
    assert removed == (target,)
    assert kept == (sidecar,)
    assert target.read_bytes() == replacement_target
    assert sidecar.read_bytes() == replacement_sidecar
    assert (saved_parent / sidecar.name).read_bytes() == b"old automatic sidecar"


def test_a_committed_trash_reports_pairing_artifacts_it_could_not_clean(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = root / "clip.mp4"
    video.write_bytes(b"0" * 64)
    sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    sidecar.write_text(json.dumps({"still_path": "/nowhere"}), encoding="utf-8")
    real_discard = file_io.discard_regular_if_same

    def discard(
        path: Path,
        *,
        expected_identity: file_io.PathIdentity,
        expected_fingerprint: file_io.FileFingerprint | None = None,
        pinned_source: file_io.PinnedPath | None = None,
        retained_parent: Path | None = None,
        logical_retained_parent: Path | None = None,
    ) -> bool:
        if path.name == sidecar.name:
            return False
        return real_discard(
            path,
            expected_identity=expected_identity,
            expected_fingerprint=expected_fingerprint,
            pinned_source=pinned_source,
            retained_parent=retained_parent,
            logical_retained_parent=logical_retained_parent,
        )

    monkeypatch.setattr(file_io, "discard_regular_if_same", discard)

    result = _trash(item_for(video, Kind.VIDEO, Ownership.USER), (root,))

    assert result.destination.is_file(), "cleanup cannot roll back a committed trash move"
    assert result.kept_artifacts == (sidecar,)
    assert sidecar.name in result.cleanup_note()


def test_a_committed_remove_reports_pairing_artifacts_it_could_not_clean(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = managed_directory(root, "MotionBGS")
    video = directory / "clip.mp4"
    video.write_bytes(b"0" * 64)
    video.with_name(video.name + ".motionbgs.json").write_text(
        provenance(video, "MotionBGS"), encoding="utf-8"
    )
    sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    sidecar.write_text(json.dumps({"still_path": "/nowhere"}), encoding="utf-8")
    real_discard = file_io.discard_regular_if_same

    def discard(
        path: Path,
        *,
        expected_identity: file_io.PathIdentity,
        expected_fingerprint: file_io.FileFingerprint | None = None,
        pinned_source: file_io.PinnedPath | None = None,
        retained_parent: Path | None = None,
        logical_retained_parent: Path | None = None,
    ) -> bool:
        if path.name == sidecar.name:
            return False
        return real_discard(
            path,
            expected_identity=expected_identity,
            expected_fingerprint=expected_fingerprint,
            pinned_source=pinned_source,
            retained_parent=retained_parent,
            logical_retained_parent=logical_retained_parent,
        )

    monkeypatch.setattr(file_io, "discard_regular_if_same", discard)

    result = _remove(item_for(video, Kind.VIDEO, Ownership.MANAGED), (root,))

    assert not video.exists(), "artifact cleanup cannot roll back committed media removal"
    assert result.kept == (sidecar,)
    assert sidecar.name in result.cleanup_note()


@pytest.mark.parametrize("move_to_trash", (False, True))
def test_postcommit_source_sync_failure_still_consumes_pinned_pairing_artifacts(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    move_to_trash: bool,
) -> None:
    if move_to_trash:
        video = root / "clip.mp4"
        video.write_bytes(b"video")
        media = item_for(video, Kind.VIDEO, Ownership.USER)
    else:
        media = downloaded(root, "clip.mp4")
        video = media.path
        media = item_for(video, Kind.VIDEO, Ownership.MANAGED, provider="Wallhaven")
    sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    sidecar.write_text(json.dumps({"still_path": "/nowhere"}), encoding="utf-8")
    real_sync = paths.fsync_directory
    failed = False
    absent_source_syncs = 0

    def fail_final_source_sync(directory: Path) -> None:
        nonlocal absent_source_syncs, failed
        if directory == video.parent and not video.exists():
            absent_source_syncs += 1
            wanted = 1 if move_to_trash else 2
            if absent_source_syncs == wanted and not failed:
                failed = True
                raise OSError("injected final source-directory fsync failure")
        real_sync(directory)

    monkeypatch.setattr(paths, "fsync_directory", fail_final_source_sync)

    with pytest.raises(ManageError) as caught:
        if move_to_trash:
            _trash(media, (root,))
        else:
            _remove(media, (root,))

    assert failed
    assert caught.value.committed
    assert caught.value.physical is not None
    assert not sidecar.exists()
    if move_to_trash:
        assert isinstance(caught.value.physical, manage.Trashed)
        assert sidecar in caught.value.physical.removed_artifacts
    else:
        assert isinstance(caught.value.physical, manage.Removal)
        assert sidecar in caught.value.physical.removed


def test_an_artifact_that_vanishes_before_claim_is_an_idempotent_success(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = root / "clip.mp4"
    video.write_bytes(b"0" * 64)
    sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    sidecar.write_text(json.dumps({"still_path": "/nowhere"}), encoding="utf-8")
    real_identity = file_io.path_identity

    def vanish(path: Path) -> file_io.PathIdentity:
        if path.name == sidecar.name:
            path.unlink()
            raise FileNotFoundError(path)
        return real_identity(path)

    monkeypatch.setattr(file_io, "path_identity", vanish)

    result = _trash(item_for(video, Kind.VIDEO, Ownership.USER), (root,))

    assert result.kept_artifacts == ()
    assert not sidecar.exists()


def test_artifact_cleanup_never_unlinks_a_same_path_replacement(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = root / "clip.mp4"
    video.write_bytes(b"0" * 64)
    sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    sidecar.write_text(json.dumps({"still_path": "/nowhere"}), encoding="utf-8")
    expected = _expected(sidecar)
    original = root / "prepared-pairing-sidecar"
    real_rename = file_io._rename_noreplace
    real_lstat = Path.lstat
    raced = False
    quarantine: Path | None = None

    def replace_then_rename(source: Path, destination: Path) -> None:
        nonlocal quarantine, raced
        if source.name == sidecar.name and not raced:
            raced = True
            quarantine = destination
            source.rename(original)
            source.write_bytes(b"late replacement")
        real_rename(source, destination)

    def spoof_reused_identity(candidate: Path) -> os.stat_result:
        status = real_lstat(candidate)
        if candidate == quarantine and raced:
            fields = list(status)
            fields[stat.ST_DEV] = expected[0]
            fields[stat.ST_INO] = expected[1]
            return os.stat_result(fields)
        return status

    monkeypatch.setattr(file_io, "_rename_noreplace", replace_then_rename)
    monkeypatch.setattr(Path, "lstat", spoof_reused_identity)

    result = _trash(item_for(video, Kind.VIDEO, Ownership.USER), (root,))

    assert result.kept_artifacts == (sidecar,)
    assert sidecar.read_bytes() == b"late replacement"
    assert json.loads(original.read_text(encoding="utf-8")) == {"still_path": "/nowhere"}


@pytest.mark.parametrize("trash", (False, True), ids=("managed-delete", "user-trash"))
@pytest.mark.parametrize(
    "mutate_in_place",
    (False, True),
    ids=("replacement-inode", "in-place-mutation"),
)
def test_media_commit_does_not_remove_artifacts_from_a_new_same_path_lifecycle(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    trash: bool,
    mutate_in_place: bool,
) -> None:
    if trash:
        path = root / "picture.jpg"
        path.write_bytes(b"old user source")
        item = item_for(path)
        artifacts: dict[Path, bytes] = {}
    else:
        item = downloaded(root)
        path = item.path
        provider_sidecar = path.with_name(path.name + ".wallhaven.json")
        artifacts = {
            provider_sidecar: provenance(path, "Wallhaven").encode(),
        }
    pairing_sidecar = path.with_name(path.name + pairing.SIDECAR_SUFFIX)
    pairing_sidecar.write_text(json.dumps({"still_path": "/old"}), encoding="utf-8")
    artifacts[pairing_sidecar] = json.dumps({"still_path": "/new"}).encode()
    backups: dict[Path, Path] = {}
    real_discard = manage._discard_pinned_artifacts

    def install_replacement_then_discard(
        pinned: Sequence[manage._PinnedArtifact],
        *,
        kept: Sequence[Path] = (),
    ) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
        assert not path.exists(), "the replacement must arrive after the media commit"
        path.write_bytes(b"new source generation")
        for index, (artifact, replacement) in enumerate(artifacts.items()):
            if artifact.exists():
                if mutate_in_place and artifact == pairing_sidecar:
                    artifact.write_bytes(replacement)
                    continue
                backup = artifact.with_name(f"old-{index}-{artifact.name}")
                artifact.rename(backup)
                backups[artifact] = backup
            artifact.write_bytes(replacement)
        return real_discard(pinned, kept=kept)

    monkeypatch.setattr(manage, "_discard_pinned_artifacts", install_replacement_then_discard)

    result = _trash(item, (root,)) if trash else _remove(item, (root,))
    retained = result.kept_artifacts if isinstance(result, manage.Trashed) else result.kept

    assert path.read_bytes() == b"new source generation"
    assert set(retained) == {pairing_sidecar}
    for artifact, replacement in artifacts.items():
        assert artifact.read_bytes() == replacement
    if mutate_in_place:
        assert backups == {}
    else:
        assert set(backups) == {pairing_sidecar}
        assert backups[pairing_sidecar].is_file()


def test_artifact_cleanup_reports_a_replacement_preserved_after_restore_conflict(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = root / "clip.mp4"
    video.write_bytes(b"0" * 64)
    media = item_for(video, Kind.VIDEO)
    sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    sidecar.write_text(json.dumps({"still_path": "/nowhere"}), encoding="utf-8")
    original = root / "prepared-pairing-sidecar"
    real_rename = file_io._rename_noreplace
    preserved: Path | None = None
    raced = False

    def move_replacement_then_reoccupy(source: Path, destination: Path) -> None:
        nonlocal preserved, raced
        if source.name == sidecar.name and not raced:
            raced = True
            preserved = destination.resolve()
            source.rename(original)
            source.write_bytes(b"replacement B")
            real_rename(source, destination)
            source.write_bytes(b"replacement C")
            return
        real_rename(source, destination)

    monkeypatch.setattr(file_io, "_rename_noreplace", move_replacement_then_reoccupy)

    removed, kept = manage.discard_pairing_artifacts(media, (root,))

    assert preserved is not None
    assert removed == ()
    assert len(kept) == 1
    assert sidecar.read_bytes() == b"replacement C"
    assert kept[0].read_bytes() == b"replacement B"
    assert json.loads(original.read_text(encoding="utf-8")) == {"still_path": "/nowhere"}


def test_a_legacy_shared_still_is_not_deleted_with_one_video(root: Path) -> None:
    """Old releases keyed generated stills by basename, so two videos could
    share one. Leaving that legacy file is safer than breaking the survivor."""
    directory = managed_directory(root, "MotionBGS")
    video = directory / "clip.mp4"
    video.write_bytes(b"0" * 64)
    video.with_name(video.name + ".motionbgs.json").write_text(
        provenance(video, "MotionBGS"), encoding="utf-8"
    )
    still_directory = pairing.still_directory(root)
    still_directory.mkdir(parents=True)
    legacy = still_directory / "clip.png"
    legacy.write_bytes(b"\x89PNG\r\n\x1a\n")
    video.with_name(video.name + pairing.SIDECAR_SUFFIX).write_text(
        json.dumps({"still_path": str(legacy)}), encoding="utf-8"
    )

    _remove(item_for(video, Kind.VIDEO, Ownership.MANAGED, legacy), (root,))

    assert legacy.is_file()


def test_a_still_the_user_made_themselves_stays(root: Path) -> None:
    """`foo-still.png` beside `foo.mp4` is the user's own file and their choice
    to keep, even once it has nothing left to pair with."""
    directory = managed_directory(root, "MotionBGS")
    video = directory / "clip.mp4"
    video.write_bytes(b"0" * 64)
    video.with_name(video.name + ".motionbgs.json").write_text(
        provenance(video, "MotionBGS"), encoding="utf-8"
    )
    sibling = directory / "clip-still.png"
    sibling.write_bytes(b"\x89PNG\r\n\x1a\n")

    _remove(item_for(video, Kind.VIDEO, Ownership.MANAGED, sibling), (root,))

    assert not video.exists()
    assert sibling.is_file()


def test_the_report_says_what_went(root: Path) -> None:
    item = downloaded(root)
    assert _remove(item, (root,)).describe() == "removed picture.jpg and 1 file beside it"


# -- what it refuses ------------------------------------------------------


def test_a_file_the_user_put_there_is_refused(root: Path) -> None:
    """The whole point. A wallpaper of their own in a directory of their own."""
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(ManageError) as caught:
        _remove(item_for(theirs), (root,))
    assert caught.value.kind == "not-ours"
    assert theirs.is_file()


def test_a_file_in_a_managed_directory_but_without_a_sidecar_is_refused(root: Path) -> None:
    """The user dropping their own picture into our download folder is the
    exact case a marker alone would get wrong."""
    directory = managed_directory(root)
    theirs = directory / "theirs.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(ManageError) as caught:
        _remove(item_for(theirs, ownership=Ownership.MANAGED), (root,))
    assert caught.value.kind == "not-ours"
    assert theirs.is_file()


@pytest.mark.parametrize(
    "document",
    (
        {},
        {
            "schema": 1,
            "plugin": "goober/wall-in-one",
            "provider": "Wallhaven",
            "path": "/some/other/picture.jpg",
        },
        {
            "schema": 1,
            "plugin": "somebody-else",
            "provider": "Wallhaven",
        },
    ),
)
def test_a_forged_or_copied_sidecar_cannot_authorise_deletion(
    root: Path, document: dict[str, object]
) -> None:
    directory = managed_directory(root)
    theirs = directory / "family.jpg"
    theirs.write_bytes(b"precious")
    payload = dict(document)
    payload.setdefault("path", str(theirs))
    theirs.with_name(theirs.name + ".wallhaven.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    with pytest.raises(ManageError) as caught:
        _remove(item_for(theirs, ownership=Ownership.MANAGED), (root,))

    assert caught.value.kind == "not-ours"
    assert theirs.read_bytes() == b"precious"


def test_pairing_metadata_never_grants_download_ownership(root: Path) -> None:
    """Customising a user video must not turn the download folder marker into
    authority to unlink it. Pairing and provenance are separate facts."""
    directory = managed_directory(root, "MotionBGS")
    theirs = directory / "family-video.mp4"
    theirs.write_bytes(b"0" * 64)
    theirs.with_name(theirs.name + pairing.SIDECAR_SUFFIX).write_text(
        json.dumps({"still_path": "chosen.png"}), encoding="utf-8"
    )

    library = scan.scan((root,))
    item = next(entry for entry in library.items if entry.path == theirs)
    assert item.ownership is Ownership.USER

    with pytest.raises(ManageError) as caught:
        _remove(item, (root,))
    assert caught.value.kind == "not-ours"
    assert theirs.is_file()


def test_a_sidecar_without_a_marker_is_refused(root: Path) -> None:
    """Ownership needs both halves; either alone is forgeable by accident."""
    directory = root / "elsewhere"
    directory.mkdir()
    path = directory / "picture.jpg"
    path.write_bytes(b"\xff\xd8\xff")
    path.with_name(path.name + ".wallhaven.json").write_text(
        provenance(path, "Wallhaven"), encoding="utf-8"
    )
    with pytest.raises(ManageError) as caught:
        _remove(item_for(path, ownership=Ownership.MANAGED), (root,))
    assert caught.value.kind == "not-ours"
    assert path.is_file()


def test_the_item_claiming_to_be_managed_does_not_make_it_so(root: Path) -> None:
    """A `MediaItem` comes from a scan that may be minutes old, and the marker
    can be removed in between. A stale record must never authorise an unlink."""
    item = downloaded(root)
    (item.path.parent / MARKER).unlink()
    with pytest.raises(ManageError) as caught:
        _remove(item, (root,))
    assert caught.value.kind == "not-ours"
    assert item.path.is_file()


def test_a_file_outside_every_root_is_refused(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "somewhere-else"
    outside.mkdir()
    path = outside / "picture.jpg"
    path.write_bytes(b"\xff\xd8\xff")
    with pytest.raises(ManageError) as caught:
        _remove(item_for(path, ownership=Ownership.MANAGED), (root,))
    assert caught.value.kind == "outside-root"
    assert path.is_file()


def test_a_symlink_is_refused_rather_than_followed(root: Path, tmp_path: Path) -> None:
    """Otherwise a link inside the library is a way to delete anything."""
    precious = tmp_path / "precious.png"
    precious.write_bytes(b"\x89PNG\r\n\x1a\n")
    directory = managed_directory(root)
    link = directory / "picture.png"
    link.symlink_to(precious)
    link.with_name(link.name + ".wallhaven.json").write_text(
        provenance(link, "Wallhaven"), encoding="utf-8"
    )
    through_the_link = MediaItem(
        path=link, kind=Kind.STILL, size=1, mtime=0, ownership=Ownership.MANAGED
    )
    with pytest.raises(ManageError) as caught:
        _remove(through_the_link, (root,))
    assert caught.value.kind == "symlink"
    assert precious.is_file()


def test_a_symlinked_still_is_not_followed_either(root: Path, tmp_path: Path) -> None:
    precious = tmp_path / "precious.png"
    precious.write_bytes(b"\x89PNG\r\n\x1a\n")
    directory = managed_directory(root, "MotionBGS")
    video = directory / "clip.mp4"
    video.write_bytes(b"0" * 64)
    video.with_name(video.name + ".motionbgs.json").write_text(
        provenance(video, "MotionBGS"), encoding="utf-8"
    )
    still_directory = pairing.still_directory(root)
    still_directory.mkdir(parents=True)
    link = still_directory / "clip.png"
    link.symlink_to(precious)

    _remove(item_for(video, Kind.VIDEO, Ownership.MANAGED, link), (root,))

    assert not video.exists()
    assert precious.is_file()


def test_a_file_already_gone_says_so(root: Path) -> None:
    item = downloaded(root)
    item.path.unlink()
    with pytest.raises(ManageError) as caught:
        _remove(item, (root,))
    assert caught.value.kind == "missing"


def test_no_roots_given_refuses_to_delete(root: Path) -> None:
    """An absent containment boundary can never authorise an unlink."""
    item = downloaded(root)
    with pytest.raises(ManageError) as caught:
        _remove(item)
    assert caught.value.kind == "outside-root"
    assert item.path.is_file()


# -- the trash ------------------------------------------------------------


def test_a_users_own_file_can_be_trashed(root: Path, data_home: Path) -> None:
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")
    landed = _trash(item_for(theirs), (root,))
    assert not theirs.exists()
    assert landed.destination.is_file()
    assert landed.destination.parent == data_home / "Trash" / "files"


def test_the_trash_record_can_restore_it(root: Path, data_home: Path) -> None:
    """A file with no record is a file the user cannot get back."""
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")
    landed = _trash(item_for(theirs), (root,))
    record = data_home / "Trash" / "info" / f"{landed.destination.name}.trashinfo"
    text = record.read_text(encoding="utf-8")
    assert text.startswith("[Trash Info]\n")
    recorded = next(line for line in text.splitlines() if line.startswith("Path="))
    assert unquote(recorded.removeprefix("Path=")) == str(theirs.absolute())
    assert "DeletionDate=" in text


def test_trash_persists_record_destination_and_source_directories(
    root: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")
    synced: list[Path] = []
    monkeypatch.setattr("wall_in_one.library.manage.paths.fsync_directory", synced.append)

    _trash(item_for(theirs), (root,))

    assert data_home / "Trash" / "info" in synced
    assert data_home / "Trash" / "files" in synced
    assert root in synced


def test_trash_refuses_a_replaced_staging_generation_before_record_link(
    root: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    theirs = root / "holiday.png"
    theirs.write_bytes(b"image")
    info = data_home / "Trash" / "info"
    real_link = os.link
    saved_staging: Path | None = None
    raced = False

    def replace_before_link(
        source: Path,
        destination: Path,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal raced, saved_staging
        if not raced and source.name.startswith(".wall-in-one-trashinfo-"):
            raced = True
            saved_staging = source.with_name("saved-exact-trash-metadata")
            source.rename(saved_staging)
            source.write_bytes(b"attacker replacement")
        real_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "link", replace_before_link)

    with pytest.raises(ManageError) as caught:
        _trash(item_for(theirs), (root,))

    assert raced
    assert caught.value.kind == "changed"
    assert not caught.value.committed
    assert theirs.read_bytes() == b"image"
    assert saved_staging is not None
    assert saved_staging.read_text(encoding="utf-8").startswith("[Trash Info]\n")
    assert (info / f"{theirs.name}.trashinfo").read_bytes() == b"attacker replacement"
    assert tuple((data_home / "Trash" / "files").iterdir()) == ()


def test_trash_rechecks_record_bytes_at_the_media_commit_boundary(
    root: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    theirs = root / "holiday.png"
    theirs.write_bytes(b"image")
    real_require = manage._require_exact_trash_record
    checks = 0

    def mutate_before_final_check(
        record: Path,
        record_pin: file_io.PinnedPath,
        payload: bytes,
    ) -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            record.write_bytes(b"corrupt metadata")
        real_require(record, record_pin, payload)

    monkeypatch.setattr(manage, "_require_exact_trash_record", mutate_before_final_check)

    with pytest.raises(ManageError) as caught:
        _trash(item_for(theirs), (root,))

    assert checks == 2
    assert caught.value.kind == "changed"
    assert not caught.value.committed
    assert theirs.read_bytes() == b"image"
    assert tuple((data_home / "Trash" / "files").iterdir()) == ()
    assert not tuple((data_home / "Trash" / "info").glob("*.trashinfo"))


def test_trash_reports_record_mutation_during_the_media_move_as_committed(
    root: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    theirs = root / "holiday.png"
    theirs.write_bytes(b"image")
    info = data_home / "Trash" / "info"
    files = data_home / "Trash" / "files"
    real_move = file_io.atomic_move_no_replace
    corrupted = b"corrupt after final precommit check"

    def mutate_record_then_move(
        source: Path,
        destination: Path,
        **keywords: object,
    ) -> None:
        if source == theirs and destination.parent == files:
            (info / f"{destination.name}.trashinfo").write_bytes(corrupted)
        real_move(source, destination, **keywords)  # type: ignore[arg-type]

    monkeypatch.setattr(file_io, "atomic_move_no_replace", mutate_record_then_move)

    with pytest.raises(ManageError) as caught:
        _trash(item_for(theirs), (root,))

    assert caught.value.kind == "changed"
    assert caught.value.committed
    assert isinstance(caught.value.physical, manage.Trashed)
    assert not theirs.exists()
    assert caught.value.physical.destination.read_bytes() == b"image"
    assert (info / f"{theirs.name}.trashinfo").read_bytes() == corrupted


def test_trash_refuses_a_same_path_replacement_after_prepare(root: Path) -> None:
    path = root / "holiday.png"
    path.write_bytes(b"original")
    item = item_for(path)
    expected = _expected(path)
    expected_fingerprint = _fingerprint(path)
    path.rename(root / "old-holiday.png")
    path.write_bytes(b"replacement")

    with pytest.raises(ManageError) as caught:
        manage.trash(
            item,
            (root,),
            expected_source=expected,
            expected_fingerprint=expected_fingerprint,
        )

    assert caught.value.kind == "changed"
    assert path.read_bytes() == b"replacement"


def test_trash_restores_a_replacement_that_arrives_during_the_atomic_move(
    root: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = root / "holiday.png"
    path.write_bytes(b"expected")
    item = item_for(path)
    expected = _expected(path)
    expected_fingerprint = _fingerprint(path)
    original = root / "prepared-original.png"
    destination = manage.trash_directory() / "files" / path.name
    real_rename = file_io._rename_noreplace
    real_lstat = Path.lstat
    raced = False

    def replace_then_rename(source: Path, destination: Path) -> None:
        nonlocal raced
        if source == path and not raced:
            raced = True
            source.rename(original)
            source.write_bytes(b"late replacement")
        real_rename(source, destination)

    def spoof_reused_identity(candidate: Path) -> os.stat_result:
        status = real_lstat(candidate)
        if candidate == destination and raced:
            fields = list(status)
            fields[stat.ST_DEV] = expected[0]
            fields[stat.ST_INO] = expected[1]
            return os.stat_result(fields)
        return status

    monkeypatch.setattr(file_io, "_rename_noreplace", replace_then_rename)
    monkeypatch.setattr(Path, "lstat", spoof_reused_identity)

    with pytest.raises(ManageError) as caught:
        manage.trash(
            item,
            (root,),
            expected_source=expected,
            expected_fingerprint=expected_fingerprint,
        )

    assert caught.value.kind == "changed"
    assert path.read_bytes() == b"late replacement"
    assert original.read_bytes() == b"expected"
    assert list((data_home / "Trash" / "files").iterdir()) == []
    assert {
        entry.name
        for entry in (data_home / "Trash" / "info").iterdir()
        if entry.name != file_io.RETAINED_ENTRY_DIRECTORY
    } == set()


def test_remove_syncs_the_media_and_artifact_directories(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = downloaded(root)
    pairing_sidecar = item.path.with_name(item.path.name + pairing.SIDECAR_SUFFIX)
    pairing_sidecar.write_text('{"still_path": "/nowhere"}', encoding="utf-8")
    synced: list[Path] = []
    monkeypatch.setattr("wall_in_one.library.manage.paths.fsync_directory", synced.append)

    _remove(item, (root,))

    assert item.path.parent in synced
    assert synced.count(item.path.parent) >= 2


def test_trash_error_identifies_a_move_already_committed_at_the_source(
    root: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    theirs = root / "holiday.png"
    theirs.write_bytes(b"image")

    def fail_source_sync(directory: Path) -> None:
        if directory == root:
            raise OSError("source directory is read-only")

    monkeypatch.setattr("wall_in_one.library.manage.paths.fsync_directory", fail_source_sync)

    with pytest.raises(ManageError) as caught:
        _trash(item_for(theirs), (root,))

    assert caught.value.kind == "local-io"
    assert caught.value.committed
    assert not theirs.exists()
    assert tuple((data_home / "Trash" / "files").iterdir())


def test_trash_reports_a_temporary_record_replacement_preserved_after_commit(
    root: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    theirs = root / "holiday.png"
    theirs.write_bytes(b"image")
    info = data_home / "Trash" / "info"
    files = data_home / "Trash" / "files"
    real_rename = file_io._rename_noreplace
    preserved: Path | None = None
    temporary: Path | None = None
    raced = False

    def replace_temporary_then_claim(source: Path, destination: Path) -> None:
        nonlocal preserved, raced, temporary
        if (
            not raced
            and source.parent == info
            and source.name.startswith(".wall-in-one-trashinfo-")
        ):
            raced = True
            temporary = source
            source.unlink()
            source.write_bytes(b"replacement B")
            real_rename(source, destination)
            preserved = destination.resolve()
            source.write_bytes(b"replacement C")
            return
        real_rename(source, destination)

    monkeypatch.setattr(file_io, "_rename_noreplace", replace_temporary_then_claim)

    with pytest.raises(ManageError) as caught:
        _trash(item_for(theirs), (root,))

    assert caught.value.kind == "local-io"
    assert caught.value.committed
    assert isinstance(caught.value.physical, manage.Trashed)
    assert caught.value.physical.destination == files / theirs.name
    assert preserved is not None
    assert temporary is not None
    assert file_io.RETAINED_ENTRY_DIRECTORY in str(caught.value)
    assert any(
        candidate.is_file() and candidate.read_bytes() == b"replacement B"
        for candidate in info.parent.rglob("entry")
    )
    assert temporary.read_bytes() == b"replacement C"
    assert not theirs.exists()
    assert (files / theirs.name).read_bytes() == b"image"
    assert (info / f"{theirs.name}.trashinfo").is_file()


def test_trash_keeps_a_precommit_error_when_temporary_record_recovery_conflicts(
    root: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    theirs = root / "holiday.png"
    theirs.write_bytes(b"image")
    info = data_home / "Trash" / "info"
    real_rename = file_io._rename_noreplace
    preserved: Path | None = None
    temporary: Path | None = None
    raced = False

    def fail_move_and_replace_temporary(source: Path, destination: Path) -> None:
        nonlocal preserved, raced, temporary
        if source == theirs:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        if (
            not raced
            and source.parent == info
            and source.name.startswith(".wall-in-one-trashinfo-")
        ):
            raced = True
            temporary = source
            source.unlink()
            source.write_bytes(b"replacement B")
            real_rename(source, destination)
            preserved = destination.resolve()
            source.write_bytes(b"replacement C")
            return
        real_rename(source, destination)

    monkeypatch.setattr(file_io, "_rename_noreplace", fail_move_and_replace_temporary)

    with pytest.raises(ManageError) as caught:
        _trash(item_for(theirs), (root,))

    assert caught.value.kind == "cross-device"
    assert not caught.value.committed
    assert preserved is not None
    assert temporary is not None
    assert "cannot be moved to the trash" in str(caught.value)
    assert file_io.RETAINED_ENTRY_DIRECTORY in str(caught.value)
    assert any(
        candidate.is_file() and candidate.read_bytes() == b"replacement B"
        for candidate in info.parent.rglob("entry")
    )
    assert temporary.read_bytes() == b"replacement C"
    assert theirs.read_bytes() == b"image"
    assert not (info / f"{theirs.name}.trashinfo").exists()


def test_a_path_with_a_space_is_recorded_encoded(root: Path, data_home: Path) -> None:
    """The user's own library really is under a directory with a space in it."""
    awkward = root / "holiday photo.png"
    awkward.write_bytes(b"\x89PNG\r\n\x1a\n")
    landed = _trash(item_for(awkward), (root,))
    record = data_home / "Trash" / "info" / f"{landed.destination.name}.trashinfo"
    text = record.read_text(encoding="utf-8")
    assert "%20" in text
    recorded = next(line for line in text.splitlines() if line.startswith("Path="))
    assert unquote(recorded.removeprefix("Path=")) == str(awkward.absolute())


def test_a_second_file_of_the_same_name_keeps_its_extension(root: Path, data_home: Path) -> None:
    """A restored `foo (1).mp4` is still obviously a video; `foo.mp4 (1)` is not."""
    first = root / "a" / "holiday.png"
    second = root / "b" / "holiday.png"
    for path in (first, second):
        path.parent.mkdir()
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
    _trash(item_for(first), (root,))
    landed = _trash(item_for(second), (root,))
    assert landed.destination.name == "holiday (1).png"
    assert (data_home / "Trash" / "info" / "holiday (1).png.trashinfo").is_file()


def test_trashing_something_that_is_not_there_says_so(root: Path) -> None:
    absent = MediaItem(
        path=root / "absent.png",
        kind=Kind.STILL,
        size=0,
        mtime=0,
        ownership=Ownership.USER,
    )
    with pytest.raises(ManageError) as caught:
        _trash(absent, (root,))
    assert caught.value.kind == "missing"


def test_a_failed_move_leaves_no_orphan_record(
    root: Path, data_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record with no file is a stale entry a file manager ignores, but it
    should still not be left behind on a failure we saw happen."""
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")

    real_rename = file_io._rename_noreplace

    def explode(source: Path, target: Path) -> None:
        if source == theirs:
            raise OSError(18, "Invalid cross-device link")
        real_rename(source, target)

    monkeypatch.setattr(file_io, "_rename_noreplace", explode)
    with pytest.raises(ManageError) as caught:
        _trash(item_for(theirs), (root,))
    assert caught.value.kind == "cross-device"
    assert theirs.is_file()
    assert {
        entry.name
        for entry in (data_home / "Trash" / "info").iterdir()
        if entry.name != file_io.RETAINED_ENTRY_DIRECTORY
    } == set()


def test_concurrent_same_name_trash_never_replaces_an_earlier_file(
    root: Path, data_home: Path
) -> None:
    sources: list[MediaItem] = []
    expected: set[bytes] = set()
    for index in range(16):
        path = root / f"source-{index}" / "same.png"
        path.parent.mkdir()
        payload = f"unique-{index}".encode()
        path.write_bytes(payload)
        sources.append(item_for(path))
        expected.add(payload)

    with ThreadPoolExecutor(max_workers=8) as pool:
        destinations = tuple(pool.map(lambda item: _trash(item, (root,)), sources))

    assert len({result.destination for result in destinations}) == len(sources)
    assert {result.destination.read_bytes() for result in destinations} == expected
    records = data_home / "Trash" / "info"
    assert {
        path.name for path in records.iterdir() if path.name != file_io.RETAINED_ENTRY_DIRECTORY
    } == {f"{result.destination.name}.trashinfo" for result in destinations}


# -- somebody else's collection -------------------------------------------
#
# The case that cost a 129 MB file. A Wallpaper Engine wallpaper is scanned
# into the library and is `Ownership.USER`, so `remove` correctly refuses to
# delete it -- and then the caller fell through to `trash`, which moved it.
# "Not ours to delete" and "not ours to move" are different claims.


def test_workshop_media_is_not_offered_or_trashed_even_inside_a_root(root: Path) -> None:
    steam = root / "Steam" / "workshop" / "431960" / "12345"
    steam.mkdir(parents=True)
    wallpaper = steam / "someone elses.mp4"
    wallpaper.write_bytes(b"0" * 64)
    workshop = item_for(wallpaper, Kind.VIDEO, provider="Wallpaper Engine")

    assert not manage.is_removable(workshop, (root,))

    with pytest.raises(ManageError) as caught:
        _trash(workshop, (root,))

    assert caught.value.kind == "not-ours"
    assert wallpaper.is_file()


def test_workshop_identity_overrides_managed_markers(root: Path) -> None:
    """Even accidental provider sidecars may not turn Steam media deletable."""
    directory = managed_directory(root, "Steam-shaped")
    wallpaper = directory / "scene-video.mp4"
    wallpaper.write_bytes(b"0" * 64)
    wallpaper.with_name(wallpaper.name + ".motionbgs.json").write_text(
        provenance(wallpaper, "MotionBGS"), encoding="utf-8"
    )
    workshop = item_for(
        wallpaper,
        Kind.VIDEO,
        Ownership.MANAGED,
        provider="Wallpaper Engine",
    )

    with pytest.raises(ManageError) as caught:
        _remove(workshop, (root,))
    assert caught.value.kind == "not-ours"
    assert wallpaper.is_file()


def test_a_file_inside_the_roots_is_still_trashed(root: Path) -> None:
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert _trash(item_for(theirs), (root,)).destination.is_file()
    assert not theirs.exists()


def test_with_no_roots_given_trash_fails_closed(root: Path) -> None:
    """Without a configured boundary, no path is safe to offer or move."""
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(ManageError) as caught:
        _trash(item_for(theirs))
    assert caught.value.kind == "not-ours"
    assert theirs.is_file()


def test_removability_is_about_the_roots_not_the_ownership(root: Path, tmp_path: Path) -> None:
    inside = root / "a.png"
    inside.write_bytes(b"\x89PNG\r\n\x1a\n")
    outside = tmp_path / "elsewhere" / "b.png"
    outside.parent.mkdir()
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")

    assert manage.is_removable(item_for(inside), (root,))
    assert not manage.is_removable(item_for(inside, provider="external"), (root,))
    assert not manage.is_removable(item_for(outside), (root,))
    assert not manage.is_removable(item_for(inside))
