from __future__ import annotations

import os
import socket
import stat
import tomllib
from pathlib import Path

import pytest

from tests.test_deployed_upgrade import _Profile, _profile
from wall_in_one import config, deployed_upgrade, deployed_upgrade_transaction, file_io, paths
from wall_in_one.library import adopted, pairing, playlists, scan, stills
from wall_in_one.wallpaper import scenes


def _schema(path: Path) -> int:
    return int(tomllib.loads(path.read_text(encoding="utf-8"))["schema_version"])


def _watched_documents() -> tuple[Path, ...]:
    return (
        paths.settings_path(),
        paths.runtime_config_path(),
        deployed_upgrade.journal_path(),
        deployed_upgrade.authority_path(),
        deployed_upgrade.staged_runtime_path(),
        deployed_upgrade.completion_path(),
    )


def _snapshot(paths_to_watch: tuple[Path, ...] | None = None) -> dict[Path, bytes | None]:
    return {
        path: path.read_bytes() if path.is_file() and not path.is_symlink() else None
        for path in (paths_to_watch or _watched_documents())
    }


def _forbid_decoder(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("the deployed upgrade must not invoke a media decoder")


def _assert_prepared(profile: _Profile, old_runtime: bytes) -> None:
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert _schema(deployed_upgrade.staged_runtime_path()) == 4
    assert config.load_strict().roots == (profile.root,)
    adoption = adopted.load(strict=True)
    assert adoption is not None
    assert adoption.root == profile.root
    assert (
        tuple((authority.source_path, authority.capture_path) for authority in adoption.authorities)
        == profile.entries
    )
    assert deployed_upgrade.probe().status == "prepared"


def _assert_complete(profile: _Profile, *, recovery_artifacts: bool = False) -> None:
    assert _schema(paths.runtime_config_path()) == 4
    assert config.load_strict().roots == (profile.root,)
    assert deployed_upgrade.probe().status == "complete"
    adoption = adopted.load(strict=True)
    assert adoption is not None
    assert adoption.root == profile.root
    library = scan.scan((profile.root,), include_workshop=False)
    assert {item.path for item in library.items} == {source for source, _capture in profile.entries}
    assert {(item.path, item.paired_still) for item in library.items} == set(profile.entries)
    if not recovery_artifacts:
        assert not deployed_upgrade.journal_path().exists()
        assert not deployed_upgrade.staged_runtime_path().exists()


def test_prepare_keeps_public_schema2_and_builds_private_schema4_without_decoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch, videos=2, named_playlist=True)
    old_runtime = paths.runtime_config_path().read_bytes()
    monkeypatch.setattr(stills, "generate", _forbid_decoder)
    monkeypatch.setattr(stills, "capture_scene", _forbid_decoder)
    monkeypatch.setattr(scenes, "screenshot", _forbid_decoder)

    outcome = deployed_upgrade_transaction.ensure(cutover=False)

    assert outcome.changed
    assert outcome.status == "prepared"
    _assert_prepared(profile, old_runtime)


def test_cutover_installs_schema4_and_hides_adopted_basename_captures_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch, videos=2, named_playlist=True)
    monkeypatch.setattr(stills, "generate", _forbid_decoder)
    monkeypatch.setattr(stills, "capture_scene", _forbid_decoder)
    monkeypatch.setattr(scenes, "screenshot", _forbid_decoder)

    first = deployed_upgrade_transaction.ensure()
    completed = _snapshot()
    second = deployed_upgrade_transaction.ensure()

    assert first.changed and first.status == "complete"
    assert not second.changed and second.status == "complete"
    assert _snapshot() == completed
    _assert_complete(profile)


def test_absent_and_current_profiles_do_not_publish_migration_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", _forbid_decoder)
    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_install_exact_replacement",
        _forbid_decoder,
    )
    absent_before = _snapshot()
    absent = deployed_upgrade_transaction.ensure()
    assert absent == deployed_upgrade_transaction.Outcome(
        False,
        "absent",
        "no Wall-in-One settings have been created",
    )
    assert _snapshot() == absent_before
    assert not (tmp_path / "config").exists()
    assert not (tmp_path / "state").exists()

    config.save(config.Settings())
    current_before = _snapshot()
    current = deployed_upgrade_transaction.ensure()
    assert not current.changed and current.status == "current"
    assert _snapshot() == current_before


def test_additional_current_media_conflicts_before_any_migration_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    extra = profile.root / "later-user-image.jpg"
    extra.write_bytes(b"user image")
    before = _snapshot()
    root_before = {
        path: path.read_bytes()
        for path in profile.root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }

    found = deployed_upgrade_transaction.probe()
    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure(cutover=False)

    assert found.status == "conflict"
    assert "additional, missing, or reordered logical media" in found.detail
    assert raised.value.status == "conflict"
    assert _snapshot() == before
    assert {
        path: path.read_bytes()
        for path in profile.root.rglob("*")
        if path.is_file() and not path.is_symlink()
    } == root_before
    assert config.load_strict().roots == ()
    assert not deployed_upgrade.journal_path().exists()
    assert not deployed_upgrade.authority_path().exists()
    assert not deployed_upgrade.staged_runtime_path().exists()


@pytest.mark.parametrize(
    "filename",
    (
        deployed_upgrade.JOURNAL_FILENAME,
        deployed_upgrade.AUTHORITY_FILENAME,
        deployed_upgrade.STAGED_RUNTIME_FILENAME,
        deployed_upgrade.COMPLETION_FILENAME,
    ),
)
def test_random_reserved_artifact_is_corrupt_not_a_resume_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    paths.app_state_dir().mkdir(parents=True)
    (paths.app_state_dir() / filename).write_text("{}\n", encoding="utf-8")
    found = deployed_upgrade_transaction.probe()
    assert found.status == "corrupt"


def test_transaction_publication_refuses_symlinked_parent(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    logical_parent = tmp_path / "state" / "wall-in-one"
    logical_parent.parent.mkdir()
    logical_parent.symlink_to(outside, target_is_directory=True)
    target = logical_parent / "journal.json"

    with pytest.raises(
        deployed_upgrade_transaction.TransactionError,
        match=r"parent path.*is not a directory",
    ):
        deployed_upgrade_transaction._write_no_replace(
            target,
            b"{}\n",
            64,
            label="test journal",
        )

    assert not (outside / target.name).exists()


def test_different_existing_capture_sidecar_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    _source, capture = profile.entries[0]
    sidecar = capture.with_name(capture.name + pairing.SIDECAR_SUFFIX)
    sidecar.write_bytes(b"user-owned-sidecar\n")

    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure(cutover=False)

    assert raised.value.status == "conflict"
    assert sidecar.read_bytes() == b"user-owned-sidecar\n"
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert not deployed_upgrade.journal_path().exists()


def test_malformed_orphan_authority_is_corrupt_and_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    deployed_upgrade.authority_path().write_bytes(b"foreign\n")

    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure()

    assert raised.value.status == "corrupt"
    assert deployed_upgrade.authority_path().read_bytes() == b"foreign\n"
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert not deployed_upgrade.journal_path().exists()


def test_live_authoring_socket_requests_retry_before_journal_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _profile(tmp_path, monkeypatch)
    target = paths.socket_path()
    target.parent.mkdir(parents=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(target))
    server.listen(1)
    try:
        with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
            deployed_upgrade_transaction.ensure(cutover=False)
    finally:
        server.close()
        target.unlink(missing_ok=True)
    assert raised.value.status == "retry"
    assert not deployed_upgrade.journal_path().exists()


def test_resume_after_journal_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    original = deployed_upgrade_transaction._publish_capture_sidecars

    def interrupt(_journal: object) -> bool:
        raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_capture_sidecars", interrupt)
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    assert deployed_upgrade.journal_path().exists()
    assert paths.runtime_config_path().read_bytes() == old_runtime

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_capture_sidecars", original)
    resumed = deployed_upgrade_transaction.ensure(cutover=False)
    assert resumed.status == "prepared"
    _assert_prepared(profile, old_runtime)


def test_resume_rejects_authoring_generation_changed_after_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _profile(tmp_path, monkeypatch, named_playlist=True)
    old_runtime = paths.runtime_config_path().read_bytes()
    original = deployed_upgrade_transaction._publish_capture_sidecars

    def interrupt(_journal: object) -> bool:
        raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_capture_sidecars", interrupt)
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    playlist_state = playlists.state_path()
    playlist_state.write_text(
        playlist_state.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_publish_capture_sidecars",
        original,
    )

    with pytest.raises(deployed_upgrade_transaction.TransactionError) as raised:
        deployed_upgrade_transaction.ensure(cutover=False)

    assert raised.value.status == "corrupt"
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert deployed_upgrade.journal_path().exists()


def test_resume_after_capture_sidecar_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch, videos=2)
    old_runtime = paths.runtime_config_path().read_bytes()
    original = deployed_upgrade_transaction._write_no_replace

    def interrupt_after_first_sidecar(
        path: Path,
        contents: bytes,
        maximum: int,
        *,
        label: str,
        create_parent: bool = True,
    ) -> bool:
        changed = original(
            path,
            contents,
            maximum,
            label=label,
            create_parent=create_parent,
        )
        if path.name.endswith(pairing.SIDECAR_SUFFIX):
            raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")
        return changed

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_write_no_replace",
        interrupt_after_first_sidecar,
    )
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    assert config.load_strict().roots == ()
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert (
        sum(
            capture.with_name(capture.name + pairing.SIDECAR_SUFFIX).is_file()
            for _source, capture in profile.entries
        )
        == 1
    )

    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", original)
    resumed = deployed_upgrade_transaction.ensure(cutover=False)
    assert resumed.status == "prepared"
    _assert_prepared(profile, old_runtime)


def test_empty_durable_claim_slot_advances_without_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    original_move = file_io.atomic_move_no_replace
    interrupted = False

    def interrupt_after_claim_directory(
        source: Path,
        destination: Path,
        *,
        expected_identity: file_io.PathIdentity,
        expected_file_type: int = stat.S_IFREG,
        expected_fingerprint: file_io.FileFingerprint | None = None,
        pinned_source: file_io.PinnedPath | None = None,
        externally_pinned: bool = False,
    ) -> None:
        nonlocal interrupted
        if not interrupted and source.name == "settings.toml" and destination.name == "entry":
            interrupted = True
            raise OSError("interrupted before durable claim move")
        original_move(
            source,
            destination,
            expected_identity=expected_identity,
            expected_file_type=expected_file_type,
            expected_fingerprint=expected_fingerprint,
            pinned_source=pinned_source,
            externally_pinned=externally_pinned,
        )

    monkeypatch.setattr(
        file_io,
        "atomic_move_no_replace",
        interrupt_after_claim_directory,
    )
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    journal_record = deployed_upgrade_transaction._read_journal()
    assert journal_record is not None
    journal, _raw = journal_record
    first_slot = deployed_upgrade_transaction._claim_path(
        paths.settings_path(),
        journal.settings_token,
    )
    assert first_slot.is_dir()
    assert tuple(first_slot.iterdir()) == ()
    assert config.load_strict().roots == ()

    monkeypatch.setattr(
        file_io,
        "atomic_move_no_replace",
        original_move,
    )
    resumed = deployed_upgrade_transaction.ensure(cutover=False)

    assert resumed.status == "prepared"
    assert first_slot.is_dir()
    assert tuple(first_slot.iterdir()) == ()
    assert config.load_strict().roots == (profile.root,)
    second_token = deployed_upgrade_transaction._claim_tokens(journal.settings_token)[1]
    retained_predecessor = (
        deployed_upgrade_transaction._claim_path(paths.settings_path(), second_token) / "entry"
    )
    assert "roots = []" in retained_predecessor.read_text(encoding="utf-8")


def test_resume_after_old_settings_are_claimed_before_new_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    original = deployed_upgrade_transaction._write_no_replace_access
    interrupted = False

    def interrupt_settings_publication(
        logical: Path,
        access: Path,
        contents: bytes,
        maximum: int,
        *,
        label: str,
        context: file_io.PinnedDirectoryContext,
    ) -> bool:
        nonlocal interrupted
        if not interrupted and logical == paths.settings_path():
            interrupted = True
            raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")
        return original(
            logical,
            access,
            contents,
            maximum,
            label=label,
            context=context,
        )

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_write_no_replace_access",
        interrupt_settings_publication,
    )
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    assert not paths.settings_path().exists()
    assert deployed_upgrade.journal_path().exists()

    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace_access", original)
    resumed = deployed_upgrade_transaction.ensure(cutover=False)

    assert resumed.status == "prepared"
    assert config.load_strict().roots == (profile.root,)


def test_resume_after_settings_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    original = deployed_upgrade_transaction._publish_authority_manifest

    def interrupt(_journal: object) -> bool:
        raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_authority_manifest", interrupt)
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    assert config.load_strict().roots == (profile.root,)
    assert paths.runtime_config_path().read_bytes() == old_runtime

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_authority_manifest", original)
    resumed = deployed_upgrade_transaction.ensure(cutover=False)
    assert resumed.status == "prepared"
    _assert_prepared(profile, old_runtime)


def test_resume_after_authority_manifest_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    original = deployed_upgrade_transaction._publish_authority_manifest

    def interrupt(journal: deployed_upgrade_transaction._Journal) -> bool:
        original(journal)
        raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_authority_manifest", interrupt)
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure(cutover=False)
    assert config.load_strict().roots == (profile.root,)
    assert deployed_upgrade.authority_path().is_file()
    assert not deployed_upgrade.staged_runtime_path().exists()
    assert paths.runtime_config_path().read_bytes() == old_runtime

    monkeypatch.setattr(deployed_upgrade_transaction, "_publish_authority_manifest", original)
    resumed = deployed_upgrade_transaction.ensure(cutover=False)
    assert resumed.status == "prepared"
    _assert_prepared(profile, old_runtime)


def test_resume_after_staged_runtime_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    old_runtime = paths.runtime_config_path().read_bytes()
    original = deployed_upgrade_transaction._write_no_replace

    def interrupt_stage(
        path: Path,
        contents: bytes,
        maximum: int,
        *,
        label: str,
        create_parent: bool = True,
    ) -> bool:
        changed = original(
            path,
            contents,
            maximum,
            label=label,
            create_parent=create_parent,
        )
        if path == deployed_upgrade.staged_runtime_path():
            raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")
        return changed

    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", interrupt_stage)
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure()
    assert paths.runtime_config_path().read_bytes() == old_runtime
    assert _schema(deployed_upgrade.staged_runtime_path()) == 4
    assert deployed_upgrade.probe().status == "prepared"

    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", original)
    resumed = deployed_upgrade_transaction.ensure()
    assert resumed.status == "complete"
    _assert_complete(profile)


def test_resume_after_runtime_cutover_before_completion_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    original = deployed_upgrade_transaction._write_no_replace

    def interrupt_completion(
        path: Path,
        contents: bytes,
        maximum: int,
        *,
        label: str,
        create_parent: bool = True,
    ) -> bool:
        if path == deployed_upgrade.completion_path():
            raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")
        return original(
            path,
            contents,
            maximum,
            label=label,
            create_parent=create_parent,
        )

    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", interrupt_completion)
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure()
    assert _schema(paths.runtime_config_path()) == 4
    assert deployed_upgrade.journal_path().exists()
    assert deployed_upgrade.staged_runtime_path().exists()

    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", original)
    resumed = deployed_upgrade_transaction.ensure()
    assert resumed.status == "complete"
    _assert_complete(profile)


def test_completion_publication_is_a_durable_idempotency_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(tmp_path, monkeypatch)
    original = deployed_upgrade_transaction._write_no_replace

    def interrupt_after_completion(
        path: Path,
        contents: bytes,
        maximum: int,
        *,
        label: str,
        create_parent: bool = True,
    ) -> bool:
        changed = original(
            path,
            contents,
            maximum,
            label=label,
            create_parent=create_parent,
        )
        if path == deployed_upgrade.completion_path():
            raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")
        return changed

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_write_no_replace",
        interrupt_after_completion,
    )
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure()
    assert deployed_upgrade.journal_path().is_file()
    assert deployed_upgrade.staged_runtime_path().is_file()
    _assert_complete(profile, recovery_artifacts=True)

    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", original)
    resumed = deployed_upgrade_transaction.ensure()
    assert not resumed.changed
    assert resumed.status == "complete"
    _assert_complete(profile, recovery_artifacts=True)


def test_surviving_completion_journal_is_historical_after_changed_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _profile(tmp_path, monkeypatch)
    original = deployed_upgrade_transaction._write_no_replace

    def interrupt_after_completion(
        path: Path,
        contents: bytes,
        maximum: int,
        *,
        label: str,
        create_parent: bool = True,
    ) -> bool:
        changed = original(
            path,
            contents,
            maximum,
            label=label,
            create_parent=create_parent,
        )
        if path == deployed_upgrade.completion_path():
            raise deployed_upgrade_transaction.TransactionError("interrupted", status="retry")
        return changed

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_write_no_replace",
        interrupt_after_completion,
    )
    with pytest.raises(deployed_upgrade_transaction.TransactionError):
        deployed_upgrade_transaction.ensure()
    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", original)
    paths.settings_path().write_text(
        paths.settings_path().read_text(encoding="utf-8") + "# concurrent edit\n",
        encoding="utf-8",
    )
    edited_settings = paths.settings_path().read_bytes()

    resumed = deployed_upgrade_transaction.ensure()

    assert not resumed.changed
    assert resumed.status == "complete"
    assert paths.settings_path().read_bytes() == edited_settings
    assert not deployed_upgrade.journal_path().exists()
    assert not deployed_upgrade.staged_runtime_path().exists()
    assert deployed_upgrade.completion_path().exists()


def test_interrupted_completion_cleanup_does_not_bind_later_user_edits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _profile(tmp_path, monkeypatch, named_playlist=True)
    original_discard = deployed_upgrade_transaction._discard_exact_artifact

    def leave_recovery_artifact(
        _path: Path,
        _contents: bytes,
        _maximum: int,
        *,
        label: str,
    ) -> None:
        del label

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_discard_exact_artifact",
        leave_recovery_artifact,
    )
    completed = deployed_upgrade_transaction.ensure()
    assert completed.status == "complete"
    assert deployed_upgrade.journal_path().is_file()
    assert deployed_upgrade.staged_runtime_path().is_file()

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_discard_exact_artifact",
        original_discard,
    )
    paths.settings_path().write_text(
        paths.settings_path().read_text(encoding="utf-8") + "# later settings edit\n",
        encoding="utf-8",
    )
    paths.runtime_config_path().write_text(
        paths.runtime_config_path().read_text(encoding="utf-8") + "# later runtime edit\n",
        encoding="utf-8",
    )
    playlist_store = playlists.Store.open()
    playlist_store.rename("saved", "Saved later")
    edited_settings = paths.settings_path().read_bytes()
    edited_runtime = paths.runtime_config_path().read_bytes()
    edited_playlists = playlists.state_path().read_bytes()

    resumed = deployed_upgrade_transaction.ensure()

    assert not resumed.changed
    assert resumed.status == "complete"
    assert paths.settings_path().read_bytes() == edited_settings
    assert paths.runtime_config_path().read_bytes() == edited_runtime
    assert playlists.state_path().read_bytes() == edited_playlists
    assert not deployed_upgrade.journal_path().exists()
    assert not deployed_upgrade.staged_runtime_path().exists()
    assert deployed_upgrade.completion_path().is_file()


@pytest.mark.parametrize("artifact", ("stage", "journal"))
def test_completed_cleanup_never_discards_residue_replaced_after_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
) -> None:
    _profile(tmp_path, monkeypatch)
    original_discard = deployed_upgrade_transaction._discard_exact_artifact

    def leave_recovery_artifact(
        _path: Path,
        _contents: bytes,
        _maximum: int,
        *,
        label: str,
    ) -> None:
        del label

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_discard_exact_artifact",
        leave_recovery_artifact,
    )
    assert deployed_upgrade_transaction.ensure().status == "complete"
    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_discard_exact_artifact",
        original_discard,
    )

    target = (
        deployed_upgrade.staged_runtime_path()
        if artifact == "stage"
        else deployed_upgrade.journal_path()
    )
    replacement = b"unrelated private residue replacement\n"
    original_probe = deployed_upgrade_transaction.probe
    calls = 0

    def replace_after_locked_probe() -> deployed_upgrade.Probe:
        nonlocal calls
        calls += 1
        found = original_probe()
        if calls == 2:
            candidate = target.with_name(f".{target.name}.replacement")
            candidate.write_bytes(replacement)
            candidate.chmod(0o600)
            os.replace(candidate, target)
        return found

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "probe",
        replace_after_locked_probe,
    )
    resumed = deployed_upgrade_transaction.ensure()

    assert resumed.status == "complete"
    assert target.read_bytes() == replacement
    assert deployed_upgrade.completion_path().is_file()
    monkeypatch.setattr(deployed_upgrade_transaction, "probe", original_probe)
    assert deployed_upgrade_transaction.probe().status == "complete"
    assert deployed_upgrade_transaction.ensure().status == "complete"
    assert target.read_bytes() == replacement


def test_cutover_strictly_reproves_state_after_completion_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _profile(tmp_path, monkeypatch)
    original = deployed_upgrade_transaction._write_no_replace

    def edit_after_completion(
        path: Path,
        contents: bytes,
        maximum: int,
        *,
        label: str,
        create_parent: bool = True,
    ) -> bool:
        changed = original(
            path,
            contents,
            maximum,
            label=label,
            create_parent=create_parent,
        )
        if path == deployed_upgrade.completion_path():
            paths.settings_path().write_text(
                paths.settings_path().read_text(encoding="utf-8") + "# non-cooperating edit\n",
                encoding="utf-8",
            )
        return changed

    monkeypatch.setattr(
        deployed_upgrade_transaction,
        "_write_no_replace",
        edit_after_completion,
    )
    with pytest.raises(
        deployed_upgrade_transaction.TransactionError,
        match="migrated settings changed before cutover",
    ):
        deployed_upgrade_transaction.ensure()
    assert deployed_upgrade.completion_path().is_file()
    assert deployed_upgrade.journal_path().is_file()

    monkeypatch.setattr(deployed_upgrade_transaction, "_write_no_replace", original)
    resumed = deployed_upgrade_transaction.ensure()
    assert not resumed.changed
    assert resumed.status == "complete"


def test_cleaned_completion_is_historical_after_later_settings_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _profile(tmp_path, monkeypatch)
    completed = deployed_upgrade_transaction.ensure()
    assert completed.status == "complete"
    assert not deployed_upgrade.journal_path().exists()
    paths.settings_path().write_text(
        paths.settings_path().read_text(encoding="utf-8") + "# later user edit\n",
        encoding="utf-8",
    )

    resumed = deployed_upgrade_transaction.ensure()

    assert not resumed.changed
    assert resumed.status == "complete"
