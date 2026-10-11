from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from tests import sync_faults
from wall_in_one import file_io, paths
from wall_in_one.theme import template


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every XDG root at a scratch directory.

    The installer edits Noctalia's real settings file, so nothing in this
    module may run against the user's actual configuration.
    """
    for variable, name in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_RUNTIME_DIR", "run"),
    ):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(variable, str(directory))
    # Never touch the real shell from a test.
    monkeypatch.setattr("wall_in_one.theme.noctalia.reload_config", lambda: None)
    return tmp_path


def _write_noctalia_settings(body: str) -> Path:
    path = paths.noctalia_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


SAMPLE_SETTINGS = """\
[theme]
source = "wallpaper"

    [theme.templates]
    builtin_ids = [ "gtk3", "qt" ]
    enable_builtin_templates = true

[wallpaper]
directory = "/home/someone/wallpapers"
"""


def _current_transaction_entries(settings_path: Path) -> tuple[Path, Path, Path]:
    locator = template._transaction_locator(settings_path)
    record = json.loads(locator.read_bytes())
    return (
        locator,
        settings_path.parent / record["candidate"],
        settings_path.parent / record["lock"],
    )


def _assert_no_current_transaction(settings_path: Path) -> None:
    assert not template._transaction_locator(settings_path).exists()


def _convert_current_transaction_to_legacy(settings_path: Path) -> Path:
    locator, candidate, lock = _current_transaction_entries(settings_path)
    record = json.loads(locator.read_bytes())
    legacy = template._transaction_directory(settings_path)
    legacy.mkdir(mode=0o700)
    candidate.rename(legacy / "swap")
    lock.rename(legacy / "lock")
    locator.rename(legacy / "record.json")
    record.pop("candidate")
    record.pop("lock")
    record.pop("lock_ctime_ns")
    record.pop("lock_device")
    record.pop("lock_inode")
    record.pop("lock_mtime_ns")
    record.pop("lock_size")
    record["version"] = 1
    (legacy / "record.json").write_bytes(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return legacy


def test_install_appends_a_valid_registration(fake_home: Path) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)

    result = template.install()

    assert result.changed
    assert result.backup_path is not None and result.backup_path.is_file()

    parsed = tomllib.loads(settings_path.read_text(encoding="utf-8"))
    entry = parsed["theme"]["templates"]["user"]["wall-in-one"]
    assert entry["enabled"] is True
    assert entry["input_path"] == str(template.installed_template_path())
    assert entry["output_path"] == str(paths.palette_path())
    assert entry["post_hook"].endswith("ctl reload-palette")

    # Pre-existing settings must survive untouched.
    assert parsed["theme"]["templates"]["builtin_ids"] == ["gtk3", "qt"]
    assert parsed["wallpaper"]["directory"] == "/home/someone/wallpapers"


def test_install_copies_the_template_to_a_stable_path(fake_home: Path) -> None:
    _write_noctalia_settings(SAMPLE_SETTINGS)
    template.install()

    installed = template.installed_template_path()
    assert installed.is_file()
    assert installed.read_bytes() == template.bundled_template().read_bytes()
    # Must not point into the package directory, which moves on every Nix
    # rebuild and would leave a dangling reference.
    assert "site-packages" not in str(installed)


def test_install_is_idempotent(fake_home: Path) -> None:
    _write_noctalia_settings(SAMPLE_SETTINGS)
    first = template.install()
    second = template.install()

    assert first.changed
    assert not second.changed
    assert second.detail == "already registered"

    body = paths.noctalia_settings_path().read_text(encoding="utf-8")
    assert body.count(template._BEGIN_MARKER) == 1


def test_existing_content_addressed_template_is_synced_before_settings_commit(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_noctalia_settings(SAMPLE_SETTINGS)
    source_document = template.bundled_template().read_bytes()
    destination = template.installed_template_path(source_document)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source_document)
    events: list[str] = []
    real_verify = template._verify_and_sync_installed_template
    real_write = template._write_atomically

    def observe_verify(path: Path, document: bytes) -> None:
        real_verify(path, document)
        events.append("template-synced")

    def observe_settings_write(
        path: Path,
        text: str,
        expected: template._SettingsSnapshot,
    ) -> Path:
        assert events == ["template-synced"]
        events.append("settings-commit")
        return real_write(path, text, expected)

    monkeypatch.setattr(template, "_verify_and_sync_installed_template", observe_verify)
    monkeypatch.setattr(template, "_write_atomically", observe_settings_write)

    result = template.install(reload_config=False)

    assert result.changed
    assert events == ["template-synced", "settings-commit"]


def test_concurrent_equal_content_addressed_publication_converges(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_noctalia_settings(SAMPLE_SETTINGS)
    real_publish = template._write_bytes_atomically
    raced = False

    def publish_then_report_conflict(path: Path, document: bytes) -> None:
        nonlocal raced
        real_publish(path, document)
        raced = True
        raise template.TemplateInstallError("simulated equal publication race")

    monkeypatch.setattr(template, "_write_bytes_atomically", publish_then_report_conflict)

    result = template.install(reload_config=False)

    assert raced
    assert result.changed
    assert (
        template.installed_template_path().read_bytes() == template.bundled_template().read_bytes()
    )


def test_content_addressed_template_conflict_preserves_settings_and_foreign_bytes(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    template.install(reload_config=False)
    registered = settings_path.read_bytes()
    registered_identity = file_io.path_identity(settings_path)
    backups = set(settings_path.parent.glob(f"{settings_path.name}.bak-{template.TEMPLATE_ID}-*"))
    template.installed_template_path().write_bytes(b"stale installed template")

    def forbid_settings_transaction(*_arguments: object, **_keywords: object) -> Path:
        raise AssertionError("a template conflict must not rewrite Noctalia settings")

    monkeypatch.setattr(template, "_write_atomically", forbid_settings_transaction)

    with pytest.raises(template.TemplateInstallError, match="refusing to replace existing"):
        template.install()

    assert template.installed_template_path().read_bytes() == b"stale installed template"
    assert settings_path.read_bytes() == registered
    assert file_io.path_identity(settings_path) == registered_identity
    assert (
        set(settings_path.parent.glob(f"{settings_path.name}.bak-{template.TEMPLATE_ID}-*"))
        == backups
    )
    _assert_no_current_transaction(settings_path)


@pytest.mark.parametrize("exchanged", (False, True))
@pytest.mark.parametrize("reload_config", (False, True))
def test_recovery_accepts_an_interrupted_equal_document_transaction(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    exchanged: bool,
    reload_config: bool,
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    template.install(reload_config=False)
    registered = settings_path.read_bytes()

    with template._read_settings_snapshot(settings_path) as snapshot:
        backup = template._backup(settings_path, snapshot)
        transaction = template._begin_publication_transaction(
            settings_path,
            snapshot.document,
            snapshot,
            backup,
        )
        if exchanged:
            template._rename_exchange(transaction.swap, settings_path)
            template._fsync_parent(settings_path)
        transaction.close()
    reloads = 0

    def observe_reload() -> None:
        nonlocal reloads
        reloads += 1

    monkeypatch.setattr("wall_in_one.theme.noctalia.reload_config", observe_reload)

    locator, candidate, lock = _current_transaction_entries(settings_path)
    assert locator.is_file()
    assert candidate.is_file()
    assert lock.is_file()
    events: list[tuple[str, Path]] = []
    retained_documents: dict[Path, tuple[Path, bytes]] = {}
    real_retire = template._retain_regular_intact
    real_sync = template._fsync_parent

    def observe_retirement(path: Path, **kwargs: object) -> Path:
        logical_path = kwargs.get("logical_path")
        source = logical_path if isinstance(logical_path, Path) else path
        document = path.read_bytes()
        retained = real_retire(path, **kwargs)  # type: ignore[arg-type]
        retained_documents[source] = retained, document
        events.append(("retire", source))
        return retained

    def observe_sync(path: Path) -> None:
        real_sync(path)
        events.append(("sync-parent", path.parent))

    monkeypatch.setattr(template, "_retain_regular_intact", observe_retirement)
    monkeypatch.setattr(template, "_fsync_parent", observe_sync)

    result = template.install(reload_config=reload_config)

    assert not result.changed
    assert result.detail == "already registered"
    assert settings_path.read_bytes() == registered
    _assert_no_current_transaction(settings_path)
    assert not candidate.exists()
    assert not lock.exists()
    assert reloads == int(reload_config)
    if not exchanged:
        candidate_retirement = events.index(("retire", candidate))
        locator_retirement = events.index(("retire", locator))
        parent_barrier = next(
            index
            for index, event in enumerate(events)
            if index > candidate_retirement and event == ("sync-parent", settings_path.parent)
        )
        assert candidate_retirement < parent_barrier < locator_retirement
        retained_candidate, candidate_document = retained_documents[candidate]
        assert candidate_document == registered
        assert retained_candidate.read_bytes() == registered


@pytest.mark.parametrize("reload_config", (False, True))
def test_recovery_reloads_after_an_interrupted_committed_uninstall(
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reload_config: bool,
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    template.install(reload_config=False)

    with template._read_settings_snapshot(settings_path) as snapshot:
        updated = template._replace_managed_block(snapshot.text, "").replace("\n\n\n", "\n\n")
        backup = template._backup(settings_path, snapshot)
        transaction = template._begin_publication_transaction(
            settings_path,
            updated.encode("utf-8"),
            snapshot,
            backup,
        )
        template._rename_exchange(transaction.swap, settings_path)
        template._fsync_parent(settings_path)
        transaction.close()
    reloads = 0

    def observe_reload() -> None:
        nonlocal reloads
        reloads += 1

    monkeypatch.setattr("wall_in_one.theme.noctalia.reload_config", observe_reload)

    result = template.uninstall(reload_config=reload_config)

    assert not result.changed
    assert result.detail == "not registered"
    assert template._BEGIN_MARKER not in settings_path.read_text(encoding="utf-8")
    _assert_no_current_transaction(settings_path)
    assert reloads == int(reload_config)


def test_recovery_rejects_a_record_that_reuses_one_inode_for_both_roles(
    fake_home: Path,
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    template.install(reload_config=False)

    with template._read_settings_snapshot(settings_path) as snapshot:
        backup = template._backup(settings_path, snapshot)
        transaction = template._begin_publication_transaction(
            settings_path,
            snapshot.document,
            snapshot,
            backup,
        )
        transaction.close()

    record_path = template._transaction_locator(settings_path)
    record = json.loads(record_path.read_bytes())
    record["candidate_device"] = record["expected_device"]
    record["candidate_inode"] = record["expected_inode"]
    record_path.write_bytes(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )

    with pytest.raises(template.TemplateInstallError, match="reuses one inode identity"):
        template.install(reload_config=False)

    assert template._transaction_locator(settings_path).is_file()


def test_recovery_rejects_a_replacement_for_the_creator_held_lock(fake_home: Path) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    with template._read_settings_snapshot(settings_path) as snapshot:
        backup = template._backup(settings_path, snapshot)
        transaction = template._begin_publication_transaction(
            settings_path,
            snapshot.document,
            snapshot,
            backup,
        )

    original_lock = transaction.lock.with_name(f"{transaction.lock.name}.original")
    transaction.lock.rename(original_lock)
    transaction.lock.write_bytes(b"")
    transaction.lock.chmod(0o600)
    try:
        with pytest.raises(template.TemplateInstallError, match="recorded private generation"):
            template.uninstall(reload_config=False)
    finally:
        transaction.close()

    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS
    assert original_lock.is_file()
    assert transaction.lock.is_file()
    assert transaction.record.is_file()
    assert transaction.swap.is_file()


def test_publication_revalidates_the_fixed_locator_before_exchange(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    original_identity = file_io.path_identity(settings_path)
    real_begin = template._begin_publication_transaction
    displaced_locator: Path | None = None

    def replace_locator_after_begin(
        path: Path,
        candidate_document: bytes,
        expected: template._SettingsSnapshot,
        backup: Path,
    ) -> template._PublicationTransaction:
        nonlocal displaced_locator
        transaction = real_begin(path, candidate_document, expected, backup)
        displaced_locator = transaction.record.with_name(f"{transaction.record.name}.original")
        transaction.record.rename(displaced_locator)
        transaction.record.write_text("{}", encoding="utf-8")
        return transaction

    def forbid_exchange(*_arguments: object) -> None:
        raise AssertionError("a replaced locator must be rejected before exchange")

    monkeypatch.setattr(template, "_begin_publication_transaction", replace_locator_after_begin)
    monkeypatch.setattr(template, "_rename_exchange", forbid_exchange)

    with pytest.raises(template.TemplateInstallError, match=r"being prepared.*locator"):
        template.install(reload_config=False)

    assert displaced_locator is not None and displaced_locator.is_file()
    assert template._transaction_locator(settings_path).read_text(encoding="utf-8") == "{}"
    assert file_io.path_identity(settings_path) == original_identity
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS


def test_install_refuses_to_replace_concurrently_changed_bytes(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    real_backup = template._backup
    concurrent = SAMPLE_SETTINGS + "\n# written by Noctalia while install was preparing\n"

    def change_after_backup(path: Path, snapshot: template._SettingsSnapshot) -> Path:
        backup = real_backup(path, snapshot)
        # This is the exact old-check/os.replace gap: Noctalia installs a new
        # document after our original inode has been claimed.
        path.write_text(concurrent, encoding="utf-8")
        return backup

    monkeypatch.setattr(template, "_backup", change_after_backup)

    with pytest.raises(template.TemplateInstallError, match=r"changed while.*not replaced"):
        template.install()

    assert settings_path.read_text(encoding="utf-8") == concurrent
    assert template._BEGIN_MARKER not in concurrent
    assert not list(settings_path.parent.glob(f".{settings_path.name}.*"))


def test_install_restores_claim_modified_through_an_open_writer(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    real_backup = template._backup
    concurrent = SAMPLE_SETTINGS + "\n# in-place writer retained the old inode\n"

    def change_claimed_inode(path: Path, snapshot: template._SettingsSnapshot) -> Path:
        backup = real_backup(path, snapshot)
        path.write_text(concurrent, encoding="utf-8")
        return backup

    monkeypatch.setattr(template, "_backup", change_claimed_inode)

    with pytest.raises(template.TemplateInstallError, match=r"changed while.*not replaced"):
        template.install()

    assert settings_path.read_text(encoding="utf-8") == concurrent
    backups = list(settings_path.parent.glob(f"{settings_path.name}.bak-{template.TEMPLATE_ID}-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == SAMPLE_SETTINGS
    assert not list(settings_path.parent.glob(f".{settings_path.name}.*"))


def test_uninstall_refuses_same_bytes_on_a_concurrently_replaced_inode(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    template.install()
    installed = settings_path.read_bytes()
    original_inode = settings_path.stat().st_ino
    real_backup = template._backup

    def replace_after_backup(path: Path, snapshot: template._SettingsSnapshot) -> Path:
        backup = real_backup(path, snapshot)
        replacement = path.with_name("settings.concurrent.toml")
        replacement.write_bytes(snapshot.document)
        assert replacement.stat().st_ino != original_inode
        os.replace(replacement, path)
        return backup

    monkeypatch.setattr(template, "_backup", replace_after_backup)

    with pytest.raises(template.TemplateInstallError, match=r"changed while.*not replaced"):
        template.uninstall()

    assert settings_path.read_bytes() == installed
    assert settings_path.stat().st_ino != original_inode
    assert template._BEGIN_MARKER in installed.decode("utf-8")
    backups = list(settings_path.parent.glob(f"{settings_path.name}.bak-{template.TEMPLATE_ID}-*"))
    assert any(candidate.read_bytes() == installed for candidate in backups)


def test_same_second_backups_are_unique_and_never_replace(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    original_inode = settings_path.stat().st_ino

    class FrozenDateTime:
        @staticmethod
        def now() -> datetime:
            return datetime(2026, 8, 21, 12, 34, 56)

    monkeypatch.setattr(template, "datetime", FrozenDateTime)
    first_result = template.install()
    installed = settings_path.read_bytes()
    installed_inode = settings_path.stat().st_ino
    second_result = template.uninstall()

    first = first_result.backup_path
    second = second_result.backup_path

    assert first is not None
    assert second is not None
    assert first != second
    assert first.read_text(encoding="utf-8") == SAMPLE_SETTINGS
    # The returned backup is the actual displaced settings inode. Keeping that
    # inode named means a writer which already had it open cannot write into an
    # unlinked file and silently lose its update.
    assert first.stat().st_ino == original_inode
    assert second.read_bytes() == installed
    assert second.stat().st_ino == installed_inode
    assert second.name != first.name
    assert not list(settings_path.parent.glob(f".{settings_path.name}.*"))


def test_sigkill_after_durable_backup_never_removes_canonical_settings(fake_home: Path) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    script = """
import os
import signal
from wall_in_one.theme import template

real_backup = template._backup

def kill_after_backup(path, snapshot):
    backup = real_backup(path, snapshot)
    os.kill(os.getpid(), signal.SIGKILL)
    return backup

template._backup = kill_after_backup
template.install(reload_config=False)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    assert completed.returncode == -signal.SIGKILL
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS
    backups = list(settings_path.parent.glob(f"{settings_path.name}.bak-{template.TEMPLATE_ID}-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == SAMPLE_SETTINGS
    assert not template._transaction_directory(settings_path).exists()
    _assert_no_current_transaction(settings_path)


def test_exchange_unavailable_fails_closed_with_original_canonical(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    monkeypatch.setattr(template, "_RENAMEAT2", None)

    with pytest.raises(template.TemplateInstallError, match="renameat2 is unavailable"):
        template.install()

    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS
    backups = list(settings_path.parent.glob(f"{settings_path.name}.bak-{template.TEMPLATE_ID}-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == SAMPLE_SETTINGS
    assert not template._transaction_directory(settings_path).exists()
    _assert_no_current_transaction(settings_path)


def test_transaction_publish_sync_failure_does_not_leave_a_fixed_name_wedge(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    transaction = template._transaction_locator(settings_path)
    real_sync = template._fsync_parent
    failed = False

    def fail_first_transaction_sync(path: Path) -> None:
        nonlocal failed
        if path == transaction and not failed:
            failed = True
            raise OSError(errno.EIO, "injected transaction parent sync failure")
        real_sync(path)

    monkeypatch.setattr(template, "_fsync_parent", fail_first_transaction_sync)

    with pytest.raises(template.TemplateInstallError, match="cannot stage template transaction"):
        template.install(reload_config=False)

    assert failed
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS
    assert not transaction.exists()

    result = template.install(reload_config=False)

    assert result.changed
    assert template._BEGIN_MARKER in settings_path.read_text(encoding="utf-8")


def test_next_operation_reconciles_sigkill_before_exchange(fake_home: Path) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    script = """
import os
import signal
from wall_in_one.theme import template

real_begin = template._begin_publication_transaction

def kill_after_begin(path, candidate_document, expected, backup):
    transaction = real_begin(path, candidate_document, expected, backup)
    os.kill(os.getpid(), signal.SIGKILL)
    return transaction

template._begin_publication_transaction = kill_after_begin
template.install(reload_config=False)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    assert completed.returncode == -signal.SIGKILL
    locator, candidate, lock = _current_transaction_entries(settings_path)
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS
    assert locator.is_file()
    assert candidate.is_file()
    assert lock.is_file()

    result = template.uninstall(reload_config=False)

    assert not result.changed
    assert result.detail == "not registered"
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS
    assert not locator.exists()
    assert not candidate.exists()
    assert not lock.exists()


def test_reconciliation_preserves_malformed_transaction_for_manual_repair(fake_home: Path) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    script = """
import os
import signal
from wall_in_one.theme import template

real_begin = template._begin_publication_transaction

def kill_after_begin(path, candidate_document, expected, backup):
    transaction = real_begin(path, candidate_document, expected, backup)
    os.kill(os.getpid(), signal.SIGKILL)
    return transaction

template._begin_publication_transaction = kill_after_begin
template.install(reload_config=False)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == -signal.SIGKILL
    record, candidate, lock = _current_transaction_entries(settings_path)
    outside = fake_home / "untrusted-record.json"
    outside.write_text("{}", encoding="utf-8")
    record.unlink()
    record.symlink_to(outside)

    with pytest.raises(
        template.TemplateInstallError,
        match=r"canonical entry preserved.*transaction entries preserved",
    ):
        template.uninstall(reload_config=False)

    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS
    assert record.is_symlink()
    assert outside.read_text(encoding="utf-8") == "{}"
    assert candidate.is_file()
    assert lock.is_file()


@pytest.mark.parametrize("kind", ("symlink", "fifo"))
def test_install_preserves_unsafe_entry_that_wins_publication_boundary(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    outside = fake_home / "concurrent-settings.toml"
    outside.write_text("owned by Noctalia\n", encoding="utf-8")
    real_backup = template._backup
    claimed_backup: Path | None = None

    def replace_after_backup(path: Path, snapshot: template._SettingsSnapshot) -> Path:
        nonlocal claimed_backup
        claimed_backup = real_backup(path, snapshot)
        replacement = path.with_name("settings.concurrent-entry.toml")
        if kind == "symlink":
            replacement.symlink_to(outside)
        else:
            os.mkfifo(replacement)
        os.replace(replacement, path)
        return claimed_backup

    monkeypatch.setattr(template, "_backup", replace_after_backup)

    with pytest.raises(template.TemplateInstallError, match=r"changed while.*not replaced"):
        template.install()

    assert claimed_backup is not None
    assert claimed_backup.read_text(encoding="utf-8") == SAMPLE_SETTINGS
    if kind == "symlink":
        assert settings_path.is_symlink()
        assert settings_path.readlink() == outside
        assert outside.read_text(encoding="utf-8") == "owned by Noctalia\n"
    else:
        assert stat.S_ISFIFO(settings_path.lstat().st_mode)
    assert not list(settings_path.parent.glob(f".{settings_path.name}.*"))


@pytest.mark.parametrize("kind", ("same-bytes", "symlink", "fifo"))
def test_install_restores_entry_that_wins_immediately_before_exchange(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    outside = fake_home / "pre-exchange-settings.toml"
    outside.write_text("owned by pre-exchange Noctalia writer\n", encoding="utf-8")
    real_exchange = template._rename_exchange
    replacement_identity: file_io.PathIdentity | None = None
    raced = False

    def replace_before_candidate_exchange(source: Path, destination: Path) -> None:
        nonlocal raced, replacement_identity
        if destination == settings_path and not raced:
            replacement = settings_path.with_name("settings.pre-exchange-replacement.toml")
            if kind == "same-bytes":
                replacement.write_text(SAMPLE_SETTINGS, encoding="utf-8")
            elif kind == "symlink":
                replacement.symlink_to(outside)
            else:
                os.mkfifo(replacement)
            replacement_identity = file_io.path_identity(replacement)
            os.replace(replacement, settings_path)
            raced = True
        real_exchange(source, destination)

    monkeypatch.setattr(template, "_rename_exchange", replace_before_candidate_exchange)

    with pytest.raises(template.TemplateInstallError, match=r"changed during atomic.*not replaced"):
        template.install()

    assert raced
    assert replacement_identity is not None
    assert file_io.path_identity(settings_path) == replacement_identity
    if kind == "same-bytes":
        assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS
    elif kind == "symlink":
        assert settings_path.is_symlink()
        assert settings_path.readlink() == outside
    else:
        assert stat.S_ISFIFO(settings_path.lstat().st_mode)
    assert not template._transaction_directory(settings_path).exists()
    _assert_no_current_transaction(settings_path)


def test_a_candidate_retired_but_not_verified_is_named_where_it_is(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Noctalia write wins immediately before the exchange, so the candidate is
    exchanged back and retired (moved into the retained namespace). That move lands,
    but its verification read fails (a real EIO, once) and moving it back is refused
    (the scratch settings folder turned read-only: a native EACCES). The candidate is
    in the retained namespace, and the message names it there, not at the swap name
    it has left."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    folder = settings_path.parent
    folder_mode = stat.S_IMODE(folder.stat().st_mode)
    real_exchange = template._rename_exchange
    real_rename = file_io._rename_noreplace
    real_lstat = Path.lstat
    retired: list[Path] = []
    swaps: list[Path] = []
    failing: Path | None = None

    def replace_before_candidate_exchange(source: Path, destination: Path) -> None:
        if destination == settings_path and not swaps:
            swaps.append(source)
            replacement = settings_path.with_name("settings.pre-exchange-replacement.toml")
            replacement.write_text(SAMPLE_SETTINGS, encoding="utf-8")
            os.replace(replacement, settings_path)
        real_exchange(source, destination)

    def rename(source: Path, destination: Path) -> None:
        nonlocal failing
        real_rename(source, destination)
        into_retained = destination.name.startswith(file_io.RETAINED_ENTRY_PREFIX)
        if swaps and source == swaps[0] and into_retained and not retired:
            retired.append(destination)
            failing = destination
            folder.chmod(0o500)

    def lstat(path: Path) -> os.stat_result:
        nonlocal failing
        if failing is not None and path == failing:
            failing = None
            raise OSError(errno.EIO, "injected one-shot verification read failure")
        return real_lstat(path)

    monkeypatch.setattr(template, "_rename_exchange", replace_before_candidate_exchange)
    monkeypatch.setattr(file_io, "_rename_noreplace", rename)
    monkeypatch.setattr(Path, "lstat", lstat)
    try:
        with pytest.raises(template.TemplateInstallError) as raised:
            template.install()
    finally:
        folder.chmod(folder_mode)

    said = str(raised.value)
    assert len(swaps) == 1 and len(retired) == 1
    swap = swaps[0]
    retained = folder / file_io.RETAINED_ENTRY_DIRECTORY / retired[0].name
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS, "Noctalia's write won"
    assert not swap.exists(), "the candidate left the swap name"
    assert retained.is_file(), "it is in the retained namespace"
    assert "settings were not replaced" in said, said
    # Failed on 0b273cc: "the candidate remains preserved at <swap>", which was empty.
    assert f"the candidate remains preserved at {swap}" not in said, said
    assert f"the candidate remains preserved at {retained}" in said, said
    # The locator stays for the next operation, which reconciles it and installs.
    assert template.install().changed
    _assert_no_current_transaction(settings_path)


def _double_fault_on_move(
    monkeypatch: pytest.MonkeyPatch,
    folder: Path,
    matches: Callable[[Path, Path], bool],
) -> list[Path]:
    """The first move ``matches`` accepts lands; its verification read then fails
    once (a real EIO) and moving it back is refused, the scratch settings folder
    having turned read-only (a native EACCES). The caller restores the folder.

    Returns the destinations it landed at (one, once it happened).
    """
    real_rename = file_io._rename_noreplace
    real_lstat = Path.lstat
    landed: list[Path] = []
    failing: Path | None = None

    def rename(source: Path, destination: Path) -> None:
        nonlocal failing
        real_rename(source, destination)
        if not landed and matches(source, destination):
            landed.append(destination)
            failing = destination
            folder.chmod(0o500)

    def lstat(path: Path) -> os.stat_result:
        nonlocal failing
        if failing is not None and path == failing:
            failing = None
            raise OSError(errno.EIO, "injected one-shot verification read failure")
        return real_lstat(path)

    monkeypatch.setattr(file_io, "_rename_noreplace", rename)
    monkeypatch.setattr(Path, "lstat", lstat)
    return landed


def _into_retained(source: Path) -> Callable[[Path, Path], bool]:
    """A retirement of ``source`` into the retained namespace."""
    return lambda moved, destination: (
        moved == source and destination.name.startswith(file_io.RETAINED_ENTRY_PREFIX)
    )


def _interrupted_transaction(settings_path: Path) -> tuple[Path, Path, Path]:
    """A staged transaction left as a process killed before its exchange leaves it."""
    with template._read_settings_snapshot(settings_path) as snapshot:
        backup = template._backup(settings_path, snapshot)
        transaction = template._begin_publication_transaction(
            settings_path,
            (SAMPLE_SETTINGS + "\n# the staged candidate\n").encode(),
            snapshot,
            backup,
        )
        transaction.close()
    return _current_transaction_entries(settings_path)


def test_a_retired_locator_left_unverified_is_the_named_remainder(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install is done; retiring its locator lands in the retained namespace but
    can be neither verified nor undone. The remainder named is where it is."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    folder = settings_path.parent
    mode = stat.S_IMODE(folder.stat().st_mode)
    locator = template._transaction_locator(settings_path)
    landed = _double_fault_on_move(monkeypatch, folder, _into_retained(locator))
    try:
        with pytest.raises(template.TemplateInstallError) as raised:
            template.install()
    finally:
        folder.chmod(mode)

    said = str(raised.value)
    retained = folder / file_io.RETAINED_ENTRY_DIRECTORY / landed[0].name
    assert not locator.exists() and retained.is_file()
    assert "was updated, but its recovery cleanup remains at" in said, said
    # Failed on e98f1c6: it named the locator, which had moved.
    assert said.endswith(f"recovery cleanup remains at {retained}"), said


def test_a_lock_claimed_but_unverified_is_the_named_remainder(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install is done and its locator retired; discarding the lock claims it
    (moves it into a deletion claim) but can neither verify nor undo that move."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    folder = settings_path.parent
    mode = stat.S_IMODE(folder.stat().st_mode)
    locks: list[Path] = []
    real_begin = template._begin_publication_transaction

    def begin(*args: object, **kwargs: object) -> object:
        transaction = real_begin(*args, **kwargs)  # type: ignore[arg-type]
        locks.append(transaction.lock)
        return transaction

    monkeypatch.setattr(template, "_begin_publication_transaction", begin)
    landed = _double_fault_on_move(
        monkeypatch,
        folder,
        lambda source, destination: (
            bool(locks) and source.name == locks[0].name and destination.name == "entry"
        ),
    )
    try:
        with pytest.raises(template.TemplateInstallError) as raised:
            template.install()
    finally:
        folder.chmod(mode)

    said = str(raised.value)
    assert len(landed) == 1 and not locks[0].exists()
    claimed = said.rpartition("recovery cleanup remains at ")[2]
    # Failed on e98f1c6: it named the lock's old name, now empty.
    assert claimed != str(locks[0]), said
    assert Path(claimed).name == "entry" and os.path.lexists(claimed), said


def test_displaced_settings_left_unverified_are_named_where_they_are(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exchange is done; moving the displaced settings to their backup name
    lands but can be neither verified nor undone: they are at that name."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    folder = settings_path.parent
    mode = stat.S_IMODE(folder.stat().st_mode)
    landed = _double_fault_on_move(
        monkeypatch, folder, lambda _source, destination: ".original" in destination.name
    )
    try:
        with pytest.raises(template.TemplateInstallError) as raised:
            template.install()
    finally:
        folder.chmod(mode)

    said = str(raised.value)
    assert len(landed) == 1 and landed[0].read_text(encoding="utf-8") == SAMPLE_SETTINGS
    assert "was updated, but its displaced settings remain at" in said, said
    # Failed on e98f1c6: it named the swap, which they had left.
    assert f"displaced settings remain at {landed[0]}:" in said, said


def test_reconciling_names_a_locator_left_unverified_where_it_is(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconciling an interrupted transaction: retiring its locator lands in the
    retained namespace but can be neither verified nor undone."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    folder = settings_path.parent
    mode = stat.S_IMODE(folder.stat().st_mode)
    locator, _candidate, lock = _interrupted_transaction(settings_path)
    landed = _double_fault_on_move(monkeypatch, folder, _into_retained(locator))
    try:
        with pytest.raises(template.TemplateInstallError) as raised:
            template.uninstall(reload_config=False)
    finally:
        folder.chmod(mode)

    said = str(raised.value)
    retained = folder / file_io.RETAINED_ENTRY_DIRECTORY / landed[0].name
    assert not locator.exists() and retained.is_file() and lock.exists()
    assert f"cannot safely retire transaction record {locator}" in said, said
    assert f"it remains preserved at {retained}" in said, said
    # Failed on e98f1c6: "transaction entries preserved at <locator>", which had moved.
    assert said.endswith(f"; transaction entries preserved at {retained}"), said


def test_reconciling_without_settings_does_not_say_they_are_preserved(
    fake_home: Path,
) -> None:
    """Something outside deleted Noctalia's settings while a transaction was
    interrupted: reconciling refuses, and doesn't claim they are preserved."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    locator, _candidate, _lock = _interrupted_transaction(settings_path)
    settings_path.unlink()

    with pytest.raises(template.TemplateInstallError) as raised:
        template.uninstall(reload_config=False)

    said = str(raised.value)
    assert "canonical settings are absent" in said, said
    # Failed on e98f1c6: "canonical entry preserved at <settings>", which was absent.
    assert f"canonical entry preserved at {settings_path}" not in said, said
    assert f"no canonical entry is at {settings_path}" in said, said
    assert said.endswith(f"; transaction entries preserved at {locator}"), said


def test_legacy_reconciling_after_its_entries_are_retired_names_none(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy transaction's record and lock are retired, then syncing its
    directory fails (a directory fsync EIO): no entry remains to be named."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    _interrupted_transaction(settings_path)
    legacy = _convert_current_transaction_to_legacy(settings_path)
    real_sync = template._fsync_pinned_directory
    syncs = 0

    def sync(pin: file_io.PinnedPath, logical_path: Path) -> None:
        nonlocal syncs
        syncs += 1
        if syncs == 3:  # after the lock, the last entry, is discarded
            raise template.TemplateInstallError(
                f"cannot sync template transaction directory {logical_path}: [Errno 5] EIO"
            )
        real_sync(pin, logical_path)

    monkeypatch.setattr(template, "_fsync_pinned_directory", sync)
    with pytest.raises(template.TemplateInstallError) as raised:
        template.uninstall(reload_config=False)

    said = str(raised.value)
    assert syncs == 3 and tuple(legacy.iterdir()) == ()
    # Failed on e98f1c6: "transaction entries preserved at <the empty directory>".
    assert "transaction entries preserved" not in said, said
    assert said.endswith(f"; canonical entry preserved at {settings_path}"), said


def test_staging_cleanup_names_a_candidate_retired_unverified_where_it_is(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging fails once its locator is published (a directory fsync EIO); the
    cleanup retires the candidate, and that move lands but can be neither verified
    nor undone. The entries said to remain include it, where it is."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    folder = settings_path.parent
    mode = stat.S_IMODE(folder.stat().st_mode)
    locator = template._transaction_locator(settings_path)
    real_sync = template._fsync_parent
    sync_failed: list[Path] = []

    def sync(path: Path) -> None:
        if path == locator and not sync_failed:
            sync_failed.append(path)
            raise OSError(errno.EIO, "injected directory sync failure")
        real_sync(path)

    monkeypatch.setattr(template, "_fsync_parent", sync)
    candidate = template._transaction_leaf_prefix(settings_path, "candidate")
    landed = _double_fault_on_move(
        monkeypatch,
        folder,
        lambda source, destination: (
            source.name.startswith(candidate)
            and destination.name.startswith(file_io.RETAINED_ENTRY_PREFIX)
        ),
    )
    try:
        with pytest.raises(template.TemplateInstallError) as raised:
            template.install()
    finally:
        folder.chmod(mode)

    said = str(raised.value)
    retained = folder / file_io.RETAINED_ENTRY_DIRECTORY / landed[0].name
    assert sync_failed and retained.is_file()
    assert "cannot stage template transaction" in said, said
    listed = said.partition("; cleanup entries remain at ")[2].split("; ")[0].split(", ")
    # Failed on d0a3b01: the retired candidate was not among them.
    assert str(retained) in listed, said
    assert all(os.path.lexists(entry) for entry in listed), said


def test_the_next_reconcile_discards_a_lock_left_without_its_record(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconciling retires the record, then discarding the lock fails (an EIO): the
    lock is left with no record. The next operation discards it, so such locks
    neither pile up nor stay."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    locator, _candidate, lock = _interrupted_transaction(settings_path)
    real_discard = file_io.discard_regular_if_same
    failed = False

    def fail_once(path: Path, **kwargs: object) -> bool:
        nonlocal failed
        if path == lock and not locator.exists() and not failed:
            failed = True
            raise OSError(errno.EIO, "injected lock discard failure")
        return real_discard(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(file_io, "discard_regular_if_same", fail_once)
    with pytest.raises(template.TemplateInstallError):
        template.uninstall(reload_config=False)
    assert failed and not locator.exists() and lock.exists(), "the lock is orphaned"

    result = template.uninstall(reload_config=False)

    assert not result.changed and result.detail == "not registered"
    # Failed on d0a3b01: the orphaned lock stayed.
    assert not lock.exists()
    prefix = template._transaction_leaf_prefix(settings_path, "lock")
    left = [entry for entry in settings_path.parent.iterdir() if entry.name.startswith(prefix)]
    assert left == []


def test_reconciling_leaves_a_held_lock_and_anything_not_a_lock(fake_home: Path) -> None:
    """The control: a lock an installer holds while it stages (no record yet), and
    a non-empty file under the lock name, are not orphaned locks: both stay."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    prefix = template._transaction_leaf_prefix(settings_path, "lock")
    descriptor, held = tempfile.mkstemp(prefix=prefix, dir=settings_path.parent)
    other = settings_path.parent / f"{prefix}not-a-lock"
    other.write_text("not empty\n", encoding="utf-8")
    other.chmod(0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        template.uninstall(reload_config=False)

        assert Path(held).exists()
        assert other.read_text(encoding="utf-8") == "not empty\n"
    finally:
        os.close(descriptor)


def _cannot_look_at(
    monkeypatch: pytest.MonkeyPatch,
    entry: Callable[[], Path | None],
    *,
    skip: int = 0,
    times: int = 1_000_000,
) -> list[int]:
    """Looking at ``entry()`` (``lstat`` and ``stat``) fails with a real EIO, after
    ``skip`` successful looks and for ``times`` looks. Returns the failure count."""
    real_lstat = os.lstat
    real_stat = os.stat
    seen = [0]
    failed: list[int] = []

    def looking(real: Callable[..., os.stat_result]) -> Callable[..., os.stat_result]:
        def look(path: object, *args: object, **kwargs: object) -> os.stat_result:
            target = entry()
            if (
                target is not None
                and not args
                and "dir_fd" not in kwargs
                and isinstance(path, (str, os.PathLike))
                and os.fspath(path) == str(target)
            ):
                seen[0] += 1
                if seen[0] > skip and len(failed) < times:
                    failed.append(seen[0])
                    raise OSError(errno.EIO, "injected read failure", os.fspath(path))
            return real(path, *args, **kwargs)

        return look

    monkeypatch.setattr(os, "lstat", looking(real_lstat))
    monkeypatch.setattr(os, "stat", looking(real_stat))
    return failed


def test_settings_that_cannot_be_looked_at_are_not_said_to_be_absent(
    fake_home: Path,
) -> None:
    """The cold review's repro: Noctalia's folder is readable but not searchable (a
    native EACCES). The settings are there; they are said to be unchecked."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    locator, _candidate, _lock = _interrupted_transaction(settings_path)
    folder = settings_path.parent
    mode = stat.S_IMODE(folder.stat().st_mode)
    folder.chmod(0o600)
    try:
        with pytest.raises(template.TemplateInstallError) as raised:
            template._reconcile_interrupted_transaction(settings_path)
    finally:
        folder.chmod(mode)

    said = str(raised.value)
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS and locator.exists()
    assert "cannot inspect transaction" in said, said
    # Failed on 778fbbc: "no canonical entry is at <settings>".
    assert f"no canonical entry is at {settings_path}" not in said, said
    assert f"whether a canonical entry is at {settings_path} could not be checked" in said, said
    assert f"whether transaction entries are at {locator} could not be checked" in said, said


def test_legacy_lock_claimed_but_unverified_is_not_said_to_be_in_the_directory(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cold review's repro: a legacy reconcile retires the record, then its lock
    discard claims the lock and can neither verify nor undo that (a one-shot EIO,
    then a native EACCES): the lock is in the claim, the directory is empty."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    _interrupted_transaction(settings_path)
    legacy = _convert_current_transaction_to_legacy(settings_path)
    mode = stat.S_IMODE(legacy.stat().st_mode)
    landed = _double_fault_on_move(
        monkeypatch,
        legacy,
        lambda source, destination: source.name == "lock" and destination.name == "entry",
    )
    try:
        with pytest.raises(template.TemplateInstallError) as raised:
            template.uninstall(reload_config=False)
    finally:
        legacy.chmod(mode)

    said = str(raised.value)
    assert len(landed) == 1, said
    assert tuple(legacy.iterdir()) == (), "record retired, lock claimed: nothing remains"
    assert "cannot safely discard transaction entry" in said, said
    claimed = said.split("it remains preserved at ")[1].split(";")[0]
    assert Path(claimed).name == "entry" and os.path.lexists(claimed), said
    # Failed on 778fbbc: the empty legacy directory was named as holding entries.
    assert f"transaction entries preserved at {legacy}" not in said, said
    assert said.endswith(f"; transaction entries preserved at {claimed}"), said


def test_a_lock_is_not_taken_for_orphaned_when_the_locator_cannot_be_looked_for(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock with no locator, but the locator can't be looked for the second time
    (a real EIO): whether it is orphaned isn't known, so it stays."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    locator, _candidate, lock = _interrupted_transaction(settings_path)
    locator.unlink()
    failed = _cannot_look_at(monkeypatch, lambda: locator, skip=1)

    template.uninstall(reload_config=False)

    assert failed, "the locator could not be looked for"
    # Failed on 778fbbc: a lexists False was taken for "no locator".
    assert lock.exists()


def test_staging_cleanup_says_entries_it_cannot_look_at_are_unchecked(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging fails once its locator is published (a directory fsync EIO) and the
    settings folder then turns unsearchable (a native EACCES): nothing can be
    retired or looked at. The entries are said to be unchecked, not left out."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    folder = settings_path.parent
    mode = stat.S_IMODE(folder.stat().st_mode)
    locator = template._transaction_locator(settings_path)
    real_sync = template._fsync_parent
    sync_failed: list[Path] = []

    def sync(path: Path) -> None:
        if path == locator and not sync_failed:
            sync_failed.append(path)
            folder.chmod(0o600)
            raise OSError(errno.EIO, "injected directory sync failure")
        real_sync(path)

    monkeypatch.setattr(template, "_fsync_parent", sync)
    try:
        with pytest.raises(template.TemplateInstallError) as raised:
            template.install()
    finally:
        folder.chmod(mode)

    said = str(raised.value)
    assert sync_failed and locator.exists()
    # Failed on 778fbbc: no entry was listed at all, as if nothing remained.
    assert "; whether cleanup entries remain at " in said, said
    assert f"{locator} (" in said.partition("; whether cleanup entries remain at ")[2], said


def test_publication_cleanup_keeps_the_locator_when_the_swap_cannot_be_looked_at(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exchange is done and the displaced settings moved to their backup; then
    the swap can't be looked at (a real EIO). Whether it is empty isn't known, so the
    locator is kept and named, and the next install finishes the transaction."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    locator = template._transaction_locator(settings_path)
    swaps: list[Path] = []
    preserved: list[Path] = []
    real_begin = template._begin_publication_transaction
    real_preserve = template._preserve_regular_at_backup

    def begin(*args: object, **kwargs: object) -> object:
        transaction = real_begin(*args, **kwargs)  # type: ignore[arg-type]
        swaps.append(transaction.swap)
        return transaction

    def preserve(*args: object, **kwargs: object) -> Path:
        destination = real_preserve(*args, **kwargs)  # type: ignore[arg-type]
        preserved.append(destination)
        return destination

    monkeypatch.setattr(template, "_begin_publication_transaction", begin)
    monkeypatch.setattr(template, "_preserve_regular_at_backup", preserve)
    failed = _cannot_look_at(
        monkeypatch, lambda: swaps[0] if swaps and preserved else None, times=1
    )

    with pytest.raises(template.TemplateInstallError) as raised:
        template.install()

    said = str(raised.value)
    assert failed and preserved and locator.exists()
    # Failed on 778fbbc: the swap was taken for gone, the locator retired, no error.
    assert said.endswith(f"recovery cleanup remains at {locator}"), said
    assert not template.install().changed
    _assert_no_current_transaction(settings_path)


def test_inspect_says_an_unlookable_transaction_is_unchecked(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whether an earlier change was interrupted can't be known when its locator
    can't be looked at (a real EIO): said so, not taken for "no transaction"."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    locator = template._transaction_locator(settings_path)
    failed = _cannot_look_at(monkeypatch, lambda: locator)

    check = template.inspect()

    assert failed
    # Failed on 778fbbc: classified as if no transaction existed.
    assert check.state == "unavailable", check
    assert "can't be checked" in check.detail, check


def test_status_says_settings_it_cannot_look_at_are_unknown(fake_home: Path) -> None:
    """Noctalia's folder is unsearchable (a native EACCES): the settings are there,
    and the status says it can't check them, not that there are none."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    folder = settings_path.parent
    mode = stat.S_IMODE(folder.stat().st_mode)
    folder.chmod(0o600)
    try:
        said = template.status()
    finally:
        folder.chmod(mode)

    # Failed on 778fbbc: "not installed (no Noctalia settings at ...)".
    assert said.startswith(f"unknown (Noctalia's settings at {settings_path} can't be checked")


def test_status_says_a_palette_it_cannot_look_at_is_unchecked(fake_home: Path) -> None:
    """The palette is rendered, but its folder is unsearchable (a native EACCES):
    the status says it can't check it, not "not yet rendered"."""
    _write_noctalia_settings(SAMPLE_SETTINGS)
    template.install()
    output = paths.palette_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("{}", encoding="utf-8")
    mode = stat.S_IMODE(output.parent.stat().st_mode)
    output.parent.chmod(0o600)
    try:
        said = template.status()
    finally:
        output.parent.chmod(mode)

    # Failed on 778fbbc: "palette not yet rendered".
    assert "not yet rendered" not in said, said
    assert "palette in a state that can't be checked (" in said, said


def test_a_lock_already_gone_is_no_remainder(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install is done; something outside deletes the transaction lock just
    before the cleanup discards it. Nothing remains, so nothing is said to."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    prefix = template._transaction_leaf_prefix(settings_path, "lock")
    real_discard = file_io.discard_regular_if_same
    gone: list[Path] = []

    def deleted_first(path: Path, **kwargs: object) -> bool:
        if path.name.startswith(prefix) and not gone:
            gone.append(path)
            path.unlink()
        return real_discard(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(file_io, "discard_regular_if_same", deleted_first)

    # Failed on 778fbbc: "... recovery cleanup remains at <the deleted lock>".
    assert template.install().changed
    assert gone and not gone[0].exists()
    _assert_no_current_transaction(settings_path)


def test_a_locator_already_gone_names_the_lock_that_remains(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install is done; something outside deletes the locator just before the
    cleanup retires it. The lock, which then stays, is the remainder named."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    locator = template._transaction_locator(settings_path)
    prefix = template._transaction_leaf_prefix(settings_path, "lock")
    real_retire = template._retain_regular_intact
    gone: list[Path] = []

    def deleted_first(path: Path, **kwargs: object) -> Path:
        if path == locator and not gone:
            gone.append(path)
            path.unlink()
        return real_retire(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(template, "_retain_regular_intact", deleted_first)
    with pytest.raises(template.TemplateInstallError) as raised:
        template.install()

    said = str(raised.value)
    locks = [entry for entry in settings_path.parent.iterdir() if entry.name.startswith(prefix)]
    assert gone and not locator.exists() and len(locks) == 1
    # Failed on 778fbbc: "... recovery cleanup remains at <the deleted locator>".
    assert said.endswith(f"recovery cleanup remains at {locks[0]}"), said


def _transaction_locks(settings_path: Path) -> list[str]:
    prefix = template._transaction_leaf_prefix(settings_path, "lock")
    return sorted(name for name in os.listdir(settings_path.parent) if name.startswith(prefix))


def test_a_lock_lost_before_it_is_held_refuses_the_install_before_settings_change(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cold review's race: another process's reconcile discards an installer's
    lock between its creation and its flock (modelled in-process: flock locks belong
    to open file descriptions). The install is refused, settings untouched, no
    locator published; a retry installs."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    before = settings_path.read_bytes()
    real_mkstemp = tempfile.mkstemp
    stolen: list[str] = []

    def mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        descriptor, name = real_mkstemp(*args, **kwargs)  # type: ignore[call-overload]
        prefix = template._transaction_leaf_prefix(settings_path, "lock")
        if not stolen and Path(name).name.startswith(prefix):
            template._discard_orphaned_locks(settings_path)  # the other process
            stolen.append(name)
        return descriptor, name

    monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
    with pytest.raises(template.TemplateInstallError) as raised:
        template.install(reload_config=False)

    said = str(raised.value)
    assert stolen and not os.path.lexists(stolen[0]), "the race happened: the lock was taken"
    assert "settings were not replaced" in said, said
    assert settings_path.read_bytes() == before, "Noctalia's settings untouched"
    _assert_no_current_transaction(settings_path)
    monkeypatch.setattr(tempfile, "mkstemp", real_mkstemp)
    assert template.install(reload_config=False).changed, "a retry is all it takes"
    assert _transaction_locks(settings_path) == []


def test_a_lock_lost_then_a_crash_never_wedges_reconcile(fake_home: Path) -> None:
    """The cold review's wedge: the same race, then the installer dies right after
    staging (SIGKILL). Its locator must never name a lock that is gone, which
    would refuse every later install, uninstall and repair."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    before = settings_path.read_bytes()
    script = f"""
import os, signal, tempfile
from pathlib import Path
from wall_in_one.theme import template

settings_path = Path({str(settings_path)!r})
real_mkstemp = tempfile.mkstemp
stolen = []

def mkstemp(*args, **kwargs):
    descriptor, name = real_mkstemp(*args, **kwargs)
    prefix = template._transaction_leaf_prefix(settings_path, "lock")
    if not stolen and Path(name).name.startswith(prefix):
        template._discard_orphaned_locks(settings_path)
        stolen.append(name)
    return descriptor, name

template.tempfile.mkstemp = mkstemp
real_begin = template._begin_publication_transaction

def kill_after_begin(*args, **kwargs):
    transaction = real_begin(*args, **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)
    return transaction

template._begin_publication_transaction = kill_after_begin
template.install(reload_config=False)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    # Now refused before staging (it used to stage, then die with the locator up).
    assert completed.returncode in (1, -signal.SIGKILL), completed.stderr
    assert settings_path.read_bytes() == before

    outcomes: list[str] = []
    for _attempt in range(2):
        try:
            template.install(reload_config=False)
            outcomes.append("installed")
        except template.TemplateInstallError as error:
            outcomes.append(str(error))

    # Failed on 6c7833c: "transaction lock is not an empty regular file", twice.
    assert outcomes[0] == "installed", outcomes
    _assert_no_current_transaction(settings_path)
    assert _transaction_locks(settings_path) == []


def test_a_backup_copy_edited_while_unverified_survives(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AA-1, the template's backup copy: its move to the backup name lands, it is
    edited in place there (same inode, same length), and the verification read
    fails once (a real EIO), so the rollback moves it back to its temporary. Its
    bytes are no longer the copy's: it is left, and said to be, not discarded."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    edited = SAMPLE_SETTINGS.replace("wallpaper", "WALLPAPER", 1)
    assert len(edited) == len(SAMPLE_SETTINGS) and edited != SAMPLE_SETTINGS
    real_rename = file_io._rename_noreplace
    real_lstat = Path.lstat
    landed: list[tuple[Path, Path]] = []
    failing: Path | None = None

    def rename(source: Path, destination: Path) -> None:
        nonlocal failing
        real_rename(source, destination)
        if not landed and ".backup-" in source.name and ".bak-" in destination.name:
            landed.append((source, destination))
            with destination.open("r+b") as handle:
                handle.write(edited.encode("utf-8"))
            failing = destination

    def lstat(path: Path) -> os.stat_result:
        nonlocal failing
        if failing is not None and path == failing:
            failing = None
            raise OSError(errno.EIO, "injected one-shot verification read failure")
        return real_lstat(path)

    monkeypatch.setattr(file_io, "_rename_noreplace", rename)
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(template.TemplateInstallError) as raised:
        template.install()

    said = str(raised.value)
    temporary, destination = landed[0]
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS, "untouched"
    assert not destination.exists()
    # Failed on d958c43: the cleanup discarded it by its current metadata.
    assert temporary.read_text(encoding="utf-8") == edited, "the edit survives"
    assert said.endswith(
        f"; what is at {temporary} is not provably the copy, so it was left as it is"
    ), said


def test_a_backup_copy_left_at_its_backup_name_is_said_to_be_there(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AB-3. The backup copy's move to its backup name lands and it is edited in
    place there (so it no longer verifies), while something else takes the
    temporary's name, so the rollback can't move it back (a native EEXIST).
    The copy is still at the backup name: the error names that place, not only
    the competing file at the temporary, and both files survive."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    edited = SAMPLE_SETTINGS + "# edited while unverified\n"
    real_rename = file_io._rename_noreplace
    landed: list[tuple[Path, Path]] = []

    def rename(source: Path, destination: Path) -> None:
        real_rename(source, destination)
        if not landed and ".backup-" in source.name and ".bak-" in destination.name:
            landed.append((source, destination))
            destination.write_text(edited, encoding="utf-8")  # in place: same inode
            source.write_text("somebody else's", encoding="utf-8")  # the competing source

    monkeypatch.setattr(file_io, "_rename_noreplace", rename)
    with pytest.raises(template.TemplateInstallError) as raised:
        template.install()

    said = str(raised.value)
    temporary, destination = landed[0]
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS, "untouched"
    assert destination.read_text(encoding="utf-8") == edited, "the copy, where it was left"
    assert temporary.read_text(encoding="utf-8") == "somebody else's", "and the other file"
    # Failed on 59e6a5d: only "what is at <temporary> is not provably the copy".
    assert f"; the backup copy could not be verified and was left at {destination}" in said, said
    assert said.endswith(f"; what is now at {temporary} is not that copy and was left as it is"), (
        said
    )


def _flip_first_letter(path: Path) -> str:
    """Rewrite ``path`` in place, same inode and length, one letter changed."""
    text = path.read_text(encoding="utf-8")
    index = next(i for i, char in enumerate(text) if char.isalpha())
    edited = text[:index] + text[index].swapcase() + text[index + 1 :]
    with path.open("r+b") as handle:
        handle.write(edited.encode("utf-8"))
    return edited


@pytest.mark.parametrize("timestamps", ["coarse", "fine"])
def test_a_late_write_to_a_proven_backup_copy_survives(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, timestamps: str
) -> None:
    """The residual, for the backup copy: its move to the backup name can't be
    verified (a one-shot EIO) and it is moved back; it proves to be exactly the
    copy, then a writer still holding it writes same-length bytes in place before
    it is settled. Nothing is truncated: with coarse timestamps it is retired
    intact, late bytes and all; with fine ones the move refuses it, and it stays,
    said to have changed."""
    from tests.test_state_file_landed_writes import _with_timestamps

    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    _with_timestamps(monkeypatch, timestamps)
    real_rename = file_io._rename_noreplace
    real_lstat = Path.lstat
    real_proof = template._assert_candidate_unchanged
    landed: list[tuple[Path, Path]] = []
    late: list[str] = []
    failing: Path | None = None

    def rename(source: Path, destination: Path) -> None:
        nonlocal failing
        real_rename(source, destination)
        if not landed and ".backup-" in source.name and ".bak-" in destination.name:
            landed.append((source, destination))
            failing = destination

    def lstat(path: Path) -> os.stat_result:
        nonlocal failing
        if failing is not None and path == failing:
            failing = None
            raise OSError(errno.EIO, "injected one-shot verification read failure")
        return real_lstat(path)

    def proof_then_late_write(
        path: Path, descriptor: int, identity: file_io.PathIdentity, document: bytes
    ) -> file_io.FileFingerprint:
        proven = real_proof(path, descriptor, identity, document)
        if ".backup-" in path.name and not late:
            late.append(_flip_first_letter(path))
        return proven

    monkeypatch.setattr(file_io, "_rename_noreplace", rename)
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(template, "_assert_candidate_unchanged", proof_then_late_write)
    with pytest.raises(template.TemplateInstallError) as raised:
        template.install()

    said = str(raised.value)
    temporary, _destination = landed[0]
    retained = settings_path.parent / file_io.RETAINED_ENTRY_DIRECTORY
    retired = [entry for entry in retained.iterdir()] if retained.is_dir() else []
    assert late and settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS
    if timestamps == "coarse":
        # Failed on a4fa2f0: discarded after the proof, truncating the late bytes.
        assert [entry.read_text(encoding="utf-8") for entry in retired] == late
        assert said.endswith(
            "; the copy, proven just before (it may have changed since), "
            f"was retired intact to {retired[0]}"
        ), said
    else:
        # Failed on a4fa2f0 for its words: "the copy remains at", though changed.
        assert retired == [] and temporary.read_text(encoding="utf-8") == late[0]
        assert said.endswith(
            f"; what is at {temporary} changed after it was proven to be the copy, "
            "so it was left as it is"
        ), said


def test_a_locator_left_at_its_name_unseen_keeps_everything_it_names(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transaction locator's move to its fixed name lands, and the settings
    folder can't be searched while that move is verified and undone (a native
    EACCES on both); it can be again by the cleanup. The locator is at its name
    and names the candidate and the lock, so they are kept, not discarded."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    folder = settings_path.parent
    locator = template._transaction_locator(settings_path)
    staged = (SAMPLE_SETTINGS + "\n# the staged candidate\n").encode()
    real_rename = file_io._rename_noreplace
    real_restore = file_io._restore_after_failed_verification
    landed: list[Path] = []

    def rename(source: Path, destination: Path) -> None:
        real_rename(source, destination)
        if not landed and destination == locator:
            landed.append(source)
            folder.chmod(0o000)

    def restore_then_see_again(source: Path, destination: Path) -> Path | None:
        try:
            return real_restore(source, destination)
        finally:
            folder.chmod(0o700)

    monkeypatch.setattr(file_io, "_rename_noreplace", rename)
    monkeypatch.setattr(file_io, "_restore_after_failed_verification", restore_then_see_again)
    try:
        with template._read_settings_snapshot(settings_path) as snapshot:
            backup = template._backup(settings_path, snapshot)
            with pytest.raises(template.TemplateInstallError) as raised:
                template._begin_publication_transaction(settings_path, staged, snapshot, backup)
    finally:
        folder.chmod(0o700)

    said = str(raised.value)
    assert landed and not landed[0].exists(), "the locator stayed at its fixed name"
    record = json.loads(locator.read_bytes())
    candidate, lock = folder / record["candidate"], folder / record["lock"]
    # Failed on 59e6a5d: both discarded, under a locator that still names them.
    assert candidate.read_bytes() == staged
    assert lock.is_file()
    assert f"cleanup entries remain at {locator}, {candidate}, {lock}" in said, said


def test_a_backup_copy_whose_retirement_could_not_be_verified_is_not_said_changed(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backup copy is moved back after an unverified move and proven to be
    the copy; then its retirement's move into the retained folder lands but its
    verification read fails once (a real EIO), so the move is undone. Nothing
    changed it: it is said to be left because it couldn't be retired."""
    _write_noctalia_settings(SAMPLE_SETTINGS)
    real_rename = file_io._rename_noreplace
    real_lstat = Path.lstat
    landed: list[tuple[Path, Path]] = []
    retiring: list[Path] = []
    failing: Path | None = None

    def rename(source: Path, destination: Path) -> None:
        nonlocal failing
        real_rename(source, destination)
        if not landed and ".backup-" in source.name and ".bak-" in destination.name:
            landed.append((source, destination))
            failing = destination
        elif landed and not retiring and destination.name.startswith(file_io.RETAINED_ENTRY_PREFIX):
            retiring.append(destination)
            failing = destination

    def lstat(path: Path) -> os.stat_result:
        nonlocal failing
        if failing is not None and path == failing:
            failing = None
            raise OSError(errno.EIO, "injected one-shot verification read failure")
        return real_lstat(path)

    monkeypatch.setattr(file_io, "_rename_noreplace", rename)
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(template.TemplateInstallError) as raised:
        template.install()

    said = str(raised.value)
    temporary, _destination = landed[0]
    assert retiring, "the retirement's move landed"
    assert temporary.read_text(encoding="utf-8") == SAMPLE_SETTINGS, "put back, unchanged"
    # Failed on 30ebf3f: "changed after it was proven to be the copy".
    assert "changed after it was proven" not in said, said
    assert f"; what is at {temporary} could not be retired intact (" in said, said
    assert said.endswith("), so it was left there"), said


def test_a_moved_back_locator_is_retired_intact(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staging: the locator's move to its fixed name lands, it is edited in place
    there (same inode and length), and its verification read fails once (a real
    EIO), so it is moved back to its staging name. It was public: it is retired
    intact, edit and all, never truncated, and said where."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    locator = template._transaction_locator(settings_path)
    stage = template._transaction_leaf_prefix(settings_path, "locator-stage")
    real_rename = file_io._rename_noreplace
    real_lstat = Path.lstat
    edited: list[str] = []
    staged: list[Path] = []
    failing = False

    def rename(source: Path, destination: Path) -> None:
        nonlocal failing
        real_rename(source, destination)
        if not edited and source.name.startswith(stage) and destination == locator:
            staged.append(source)
            edited.append(_flip_first_letter(locator))
            failing = True

    def lstat(path: Path) -> os.stat_result:
        nonlocal failing
        if failing and path == locator:
            failing = False
            raise OSError(errno.EIO, "injected one-shot verification read failure")
        return real_lstat(path)

    monkeypatch.setattr(file_io, "_rename_noreplace", rename)
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(template.TemplateInstallError) as raised:
        template.install()

    said = str(raised.value)
    retained = settings_path.parent / file_io.RETAINED_ENTRY_DIRECTORY
    holding = [entry for entry in retained.iterdir() if entry.stat().st_size > 0]
    assert edited and not locator.exists() and not staged[0].exists()
    # Failed on a4fa2f0: discarded by its descriptor's current metadata, edit and all.
    assert [entry.read_text(encoding="utf-8") for entry in holding] == edited
    assert f"{staged[0]} was retired intact to {holding[0]}" in said, said
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS


@pytest.mark.parametrize("kind", ("regular", "directory"))
def test_install_never_keeps_a_substituted_candidate_name_public(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    original_identity = file_io.path_identity(settings_path)
    real_exchange = template._rename_exchange
    replacement_identity: file_io.PathIdentity | None = None
    raced = False

    def replace_candidate_name(source: Path, destination: Path) -> None:
        nonlocal raced, replacement_identity
        if destination == settings_path and not raced:
            source.unlink()
            if kind == "regular":
                source.write_text("unrelated staged replacement\n", encoding="utf-8")
            else:
                source.mkdir()
                (source / "evidence").write_text("keep", encoding="utf-8")
            replacement_identity = file_io.path_identity(source)
            raced = True
        real_exchange(source, destination)

    monkeypatch.setattr(template, "_rename_exchange", replace_candidate_name)

    with pytest.raises(template.TemplateInstallError, match=r"changed during atomic.*not replaced"):
        template.install(reload_config=False)

    assert raced
    assert replacement_identity is not None
    assert file_io.path_identity(settings_path) == original_identity
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS
    locator, candidate, lock = _current_transaction_entries(settings_path)
    assert file_io.path_identity(candidate) == replacement_identity
    if kind == "regular":
        assert candidate.read_text(encoding="utf-8") == "unrelated staged replacement\n"
    else:
        assert (candidate / "evidence").read_text(encoding="utf-8") == "keep"
    assert locator.is_file()
    assert lock.is_file()


def test_recovery_restores_original_after_sigkill_with_substituted_candidate_directory(
    fake_home: Path,
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    original_identity = file_io.path_identity(settings_path)
    script = f"""
import os
import signal
from pathlib import Path
from wall_in_one.theme import template

settings = Path({str(settings_path)!r})
real_exchange = template._rename_exchange
raced = False

def kill_after_substituted_exchange(source, destination):
    global raced
    if destination == settings and not raced:
        source.unlink()
        source.mkdir()
        (source / "evidence").write_text("keep", encoding="utf-8")
        raced = True
        real_exchange(source, destination)
        os.kill(os.getpid(), signal.SIGKILL)
    real_exchange(source, destination)

template._rename_exchange = kill_after_substituted_exchange
template.install(reload_config=False)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    assert completed.returncode == -signal.SIGKILL
    assert settings_path.is_dir()
    locator, candidate, lock = _current_transaction_entries(settings_path)

    with pytest.raises(template.TemplateInstallError, match="original settings were restored"):
        template.uninstall(reload_config=False)

    assert file_io.path_identity(settings_path) == original_identity
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS
    assert (candidate / "evidence").read_text(encoding="utf-8") == "keep"
    assert locator.is_file()
    assert lock.is_file()


def test_sigkill_during_pre_exchange_writer_conflict_keeps_both_entries(fake_home: Path) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    concurrent = SAMPLE_SETTINGS + "\n# writer won immediately before exchange\n"
    script = f"""
import os
import signal
from pathlib import Path
from wall_in_one.theme import template

settings = Path({str(settings_path)!r})
concurrent = {concurrent!r}
real_exchange = template._rename_exchange
raced = False

def kill_after_conflicting_exchange(source, destination):
    global raced
    if destination == settings and not raced:
        replacement = settings.with_name("settings.pre-exchange-kill.toml")
        replacement.write_text(concurrent, encoding="utf-8")
        os.replace(replacement, settings)
        raced = True
        real_exchange(source, destination)
        os.kill(os.getpid(), signal.SIGKILL)
    real_exchange(source, destination)

template._rename_exchange = kill_after_conflicting_exchange
template.install(reload_config=False)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    assert completed.returncode == -signal.SIGKILL
    public = settings_path.read_text(encoding="utf-8")
    assert template._BEGIN_MARKER in public
    assert tomllib.loads(public)["theme"]["templates"]["user"][template.TEMPLATE_ID]
    locator, candidate, lock = _current_transaction_entries(settings_path)
    assert candidate.read_text(encoding="utf-8") == concurrent
    assert locator.is_file()
    assert lock.is_file()

    result = template.uninstall(reload_config=False)

    assert not result.changed
    assert settings_path.read_text(encoding="utf-8") == concurrent
    assert not locator.exists()
    assert not candidate.exists()
    assert not lock.exists()


def test_next_operation_reconciles_sigkill_after_committed_exchange(fake_home: Path) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    script = """
import os
import signal
from wall_in_one.theme import template

real_exchange = template._rename_exchange

def kill_after_exchange(source, destination):
    real_exchange(source, destination)
    os.kill(os.getpid(), signal.SIGKILL)

template._rename_exchange = kill_after_exchange
template.install(reload_config=False)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    assert completed.returncode == -signal.SIGKILL
    locator, candidate, lock = _current_transaction_entries(settings_path)
    assert template._BEGIN_MARKER in settings_path.read_text(encoding="utf-8")
    assert candidate.read_text(encoding="utf-8") == SAMPLE_SETTINGS

    result = template.install(reload_config=False)

    assert not result.changed
    assert result.detail == "already registered"
    assert template._BEGIN_MARKER in settings_path.read_text(encoding="utf-8")
    assert not locator.exists()
    assert not candidate.exists()
    assert not lock.exists()


@pytest.mark.parametrize("kind", ("same-bytes", "symlink", "fifo"))
def test_install_restores_entry_that_replaces_candidate_after_rename(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    outside = fake_home / "late-concurrent-settings.toml"
    outside.write_text("owned by late Noctalia writer\n", encoding="utf-8")
    original_identity = file_io.path_identity(settings_path)
    real_exchange = template._rename_exchange
    replacement_identity: file_io.PathIdentity | None = None
    raced = False

    def replace_after_candidate_rename(source: Path, destination: Path) -> None:
        nonlocal raced, replacement_identity
        real_exchange(source, destination)
        if destination != settings_path or raced:
            return
        replacement = settings_path.with_name("settings.late-replacement.toml")
        if kind == "same-bytes":
            replacement.write_text(SAMPLE_SETTINGS, encoding="utf-8")
        elif kind == "symlink":
            replacement.symlink_to(outside)
        else:
            os.mkfifo(replacement)
        replacement_identity = file_io.path_identity(replacement)
        os.replace(replacement, settings_path)
        raced = True

    monkeypatch.setattr(template, "_rename_exchange", replace_after_candidate_rename)

    with pytest.raises(template.TemplateInstallError, match=r"changed during atomic.*not replaced"):
        template.install()

    assert raced
    assert replacement_identity is not None
    assert file_io.path_identity(settings_path) == original_identity
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS
    locator, candidate, lock = _current_transaction_entries(settings_path)
    assert file_io.path_identity(candidate) == replacement_identity
    if kind == "same-bytes":
        assert candidate.read_text(encoding="utf-8") == SAMPLE_SETTINGS
    elif kind == "symlink":
        assert candidate.is_symlink()
        assert candidate.readlink() == outside
        assert outside.read_text(encoding="utf-8") == "owned by late Noctalia writer\n"
    else:
        assert stat.S_ISFIFO(candidate.lstat().st_mode)
    assert locator.is_file()
    assert lock.is_file()
    backups = list(settings_path.parent.glob(f"{settings_path.name}.bak-{template.TEMPLATE_ID}-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == SAMPLE_SETTINGS


def test_open_writer_updates_remain_in_the_displaced_inode_backup(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    original_inode = settings_path.stat().st_ino
    writer = os.open(settings_path, os.O_WRONLY)
    concurrent = b'[theme]\nsource = "open writer after exchange"\n'
    real_move = file_io.atomic_move_no_replace
    raced = False

    def write_after_displaced_move(source: Path, destination: Path, **kwargs: object) -> None:
        nonlocal raced
        real_move(source, destination, **kwargs)  # type: ignore[arg-type]
        if (
            "transaction-candidate-" in source.name
            and ".original" in destination.name
            and not raced
        ):
            os.ftruncate(writer, 0)
            os.lseek(writer, 0, os.SEEK_SET)
            os.write(writer, concurrent)
            os.fsync(writer)
            raced = True

    monkeypatch.setattr(file_io, "atomic_move_no_replace", write_after_displaced_move)
    try:
        result = template.install(reload_config=False)
    finally:
        os.close(writer)

    assert raced
    assert result.backup_path is not None
    assert result.backup_path.stat().st_ino == original_inode
    assert result.backup_path.read_bytes() == concurrent
    assert template._BEGIN_MARKER in settings_path.read_text(encoding="utf-8")
    copies = [
        path
        for path in settings_path.parent.glob(f"{settings_path.name}.bak-{template.TEMPLATE_ID}-*")
        if path != result.backup_path
    ]
    assert len(copies) == 1
    assert copies[0].read_text(encoding="utf-8") == SAMPLE_SETTINGS


def test_open_writer_change_before_displaced_move_fails_closed_and_recovers(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    writer = os.open(settings_path, os.O_WRONLY)
    concurrent = b'[theme]\nsource = "open writer before preservation"\n'
    real_move = file_io.atomic_move_no_replace
    raced = False

    def write_before_displaced_move(source: Path, destination: Path, **kwargs: object) -> None:
        nonlocal raced
        if (
            "transaction-candidate-" in source.name
            and ".original" in destination.name
            and not raced
        ):
            os.ftruncate(writer, 0)
            os.lseek(writer, 0, os.SEEK_SET)
            os.write(writer, concurrent)
            os.fsync(writer)
            raced = True
        real_move(source, destination, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(file_io, "atomic_move_no_replace", write_before_displaced_move)
    try:
        with pytest.raises(template.TemplateInstallError, match="displaced settings remain"):
            template.install(reload_config=False)
    finally:
        os.close(writer)

    locator, candidate, lock = _current_transaction_entries(settings_path)
    assert raced
    assert template._BEGIN_MARKER in settings_path.read_text(encoding="utf-8")
    assert candidate.read_bytes() == concurrent

    result = template.uninstall(reload_config=False)

    assert not result.changed
    assert settings_path.read_bytes() == concurrent
    assert not locator.exists()
    assert not candidate.exists()
    assert not lock.exists()


def test_next_operation_survives_sigkill_during_transaction_cleanup(fake_home: Path) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    original_inode = settings_path.stat().st_ino
    script = """
import os
import signal
from wall_in_one.theme import template

real_retire = template._retain_regular_intact

def kill_after_locator_retirement(path, **kwargs):
    retained = real_retire(path, **kwargs)
    if path.name.endswith("-transaction-record"):
        os.kill(os.getpid(), signal.SIGKILL)
    return retained

template._retain_regular_intact = kill_after_locator_retirement
template.install(reload_config=False)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    assert completed.returncode == -signal.SIGKILL
    locator = template._transaction_locator(settings_path)
    assert not locator.exists()
    assert template._BEGIN_MARKER in settings_path.read_text(encoding="utf-8")
    backups = list(settings_path.parent.glob(f"{settings_path.name}.bak-{template.TEMPLATE_ID}-*"))
    assert len(backups) == 2
    assert any(backup.stat().st_ino == original_inode for backup in backups)
    orphaned_locks = list(
        settings_path.parent.glob(template._transaction_leaf_prefix(settings_path, "lock") + "*")
    )
    assert len(orphaned_locks) == 1

    result = template.install(reload_config=False)

    assert not result.changed
    assert result.detail == "already registered"


def test_cleanup_fault_reports_an_inert_regular_lock_without_blocking_retry(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    locator = template._transaction_locator(settings_path)
    real_discard = file_io.discard_regular_if_same
    failed = False

    def fail_after_locator_retirement(path: Path, **kwargs: object) -> bool:
        nonlocal failed
        if "transaction-lock-" in path.name and not locator.exists() and not failed:
            failed = True
            raise OSError(errno.EIO, "injected cleanup failure")
        return real_discard(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(file_io, "discard_regular_if_same", fail_after_locator_retirement)

    with pytest.raises(template.TemplateInstallError, match="recovery cleanup remains") as caught:
        template.install(reload_config=False)

    assert failed
    assert not locator.exists()
    orphaned_locks = list(
        settings_path.parent.glob(template._transaction_leaf_prefix(settings_path, "lock") + "*")
    )
    assert len(orphaned_locks) == 1
    assert str(orphaned_locks[0]) in str(caught.value)

    result = template.install(reload_config=False)

    assert not result.changed
    assert result.detail == "already registered"


def test_recovery_cleanup_fault_reports_the_remaining_regular_lock(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    script = """
import os
import signal
from wall_in_one.theme import template

real_begin = template._begin_publication_transaction

def kill_after_begin(path, candidate_document, expected, backup):
    transaction = real_begin(path, candidate_document, expected, backup)
    os.kill(os.getpid(), signal.SIGKILL)
    return transaction

template._begin_publication_transaction = kill_after_begin
template.install(reload_config=False)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    assert completed.returncode == -signal.SIGKILL
    locator, _candidate, lock = _current_transaction_entries(settings_path)
    real_discard = file_io.discard_regular_if_same
    failed = False

    def fail_recovery_cleanup(path: Path, **kwargs: object) -> bool:
        nonlocal failed
        if path == lock and not locator.exists() and not failed:
            failed = True
            raise OSError(errno.EIO, "injected recovery cleanup failure")
        return real_discard(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(file_io, "discard_regular_if_same", fail_recovery_cleanup)

    with pytest.raises(template.TemplateInstallError) as caught:
        template.uninstall(reload_config=False)

    assert failed
    assert not locator.exists()
    assert lock.exists()
    # The locator is retired: what remains is the lock, and that is where the
    # message sends you. (It used to name the retired locator.)
    assert str(caught.value).endswith(f"; transaction entries preserved at {lock}")
    assert f"preserved at {locator}" not in str(caught.value)
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS

    result = template.uninstall(reload_config=False)

    assert not result.changed
    assert result.detail == "not registered"


def test_staged_locator_rebind_before_publication_preserves_replacement(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    fixed = template._transaction_locator(settings_path)
    real_move = file_io.atomic_move_no_replace
    staged: Path | None = None
    displaced: Path | None = None

    def rebind_staged_source(source: Path, destination: Path, **kwargs: object) -> None:
        nonlocal staged, displaced
        if destination == fixed and kwargs.get("expected_file_type") == stat.S_IFREG:
            staged = source
            displaced = source.with_name(f"{source.name}.displaced")
            source.rename(displaced)
            source.write_text("replacement-evidence", encoding="utf-8")
        real_move(source, destination, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(file_io, "atomic_move_no_replace", rebind_staged_source)

    with pytest.raises(template.TemplateInstallError, match="cannot stage template transaction"):
        template.install(reload_config=False)

    assert staged is not None
    assert displaced is not None
    assert staged.read_text(encoding="utf-8") == "replacement-evidence"
    assert displaced.is_file()
    assert not fixed.exists()
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS


def test_locator_cleanup_never_removes_a_replacement_at_the_fixed_name(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    fixed = template._transaction_locator(settings_path)
    real_retire = template._retain_regular_intact
    displaced: Path | None = None
    raced = False

    def rebind_fixed_source(source: Path, **kwargs: object) -> Path:
        nonlocal displaced, raced
        if source == fixed and not raced:
            displaced = fixed.with_name(f"{fixed.name}.displaced")
            fixed.rename(displaced)
            fixed.write_text("replacement-evidence", encoding="utf-8")
            raced = True
        return real_retire(source, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(template, "_retain_regular_intact", rebind_fixed_source)

    with pytest.raises(template.TemplateInstallError, match="recovery cleanup remains"):
        template.install(reload_config=False)

    assert raced
    assert displaced is not None and displaced.is_file()
    assert fixed.read_text(encoding="utf-8") == "replacement-evidence"
    assert template._BEGIN_MARKER in settings_path.read_text(encoding="utf-8")


def test_current_transaction_never_moves_or_retires_a_directory(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_noctalia_settings(SAMPLE_SETTINGS)
    real_move = file_io.atomic_move_no_replace
    moved_types: list[int] = []

    def observe_move(source: Path, destination: Path, **kwargs: object) -> None:
        moved_types.append(kwargs.get("expected_file_type", stat.S_IFREG))  # type: ignore[arg-type]
        real_move(source, destination, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(file_io, "atomic_move_no_replace", observe_move)

    result = template.install(reload_config=False)

    assert result.changed
    assert moved_types
    assert stat.S_IFDIR not in moved_types


def test_recovery_uses_anchored_children_after_transaction_name_rebind(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    with template._read_settings_snapshot(settings_path) as snapshot:
        backup = template._backup(settings_path, snapshot)
        transaction = template._begin_publication_transaction(
            settings_path,
            snapshot.document,
            snapshot,
            backup,
        )
        transaction.close()
    fixed = _convert_current_transaction_to_legacy(settings_path)

    displaced = fixed.with_name(f"{fixed.name}.displaced")
    real_validate = template._validate_transaction_directory

    def rebind_after_validation(directory: Path) -> file_io.PinnedPath:
        pin = real_validate(directory)
        directory.rename(displaced)
        directory.mkdir(mode=0o700)
        (directory / "replacement-evidence").write_text("keep", encoding="utf-8")
        return pin

    monkeypatch.setattr(template, "_validate_transaction_directory", rebind_after_validation)

    result = template.uninstall(reload_config=False)

    assert not result.changed
    assert (fixed / "replacement-evidence").read_text(encoding="utf-8") == "keep"
    assert tuple(displaced.iterdir()) == ()
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS


def test_legacy_recovery_first_pin_replacement_never_claims_either_directory(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    with template._read_settings_snapshot(settings_path) as snapshot:
        backup = template._backup(settings_path, snapshot)
        transaction = template._begin_publication_transaction(
            settings_path,
            snapshot.document,
            snapshot,
            backup,
        )
        transaction.close()
    fixed = _convert_current_transaction_to_legacy(settings_path)
    original = fixed.with_name(f"{fixed.name}.original")
    replacement = fixed.with_name(f"{fixed.name}.replacement")
    real_validate = template._validate_transaction_directory
    raced = False

    def replace_before_first_pin(directory: Path) -> file_io.PinnedPath:
        nonlocal raced
        if directory == fixed and not raced:
            fixed.rename(original)
            fixed.mkdir(mode=0o700)
            (fixed / "replacement-evidence").write_text("keep", encoding="utf-8")
            raced = True
        return real_validate(directory)

    monkeypatch.setattr(template, "_validate_transaction_directory", replace_before_first_pin)

    with pytest.raises(template.TemplateInstallError, match="cannot safely reconcile"):
        template.uninstall(reload_config=False)

    assert raced
    assert (fixed / "replacement-evidence").read_text(encoding="utf-8") == "keep"
    assert {entry.name for entry in original.iterdir()} == {"lock", "record.json", "swap"}

    fixed.rename(replacement)
    original.rename(fixed)
    result = template.uninstall(reload_config=False)

    assert not result.changed
    assert fixed.is_dir()
    assert tuple(fixed.iterdir()) == ()
    assert (replacement / "replacement-evidence").read_text(encoding="utf-8") == "keep"


def test_legacy_recovery_syncs_the_pinned_directory_after_each_retirement(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    with template._read_settings_snapshot(settings_path) as snapshot:
        backup = template._backup(settings_path, snapshot)
        transaction = template._begin_publication_transaction(
            settings_path,
            snapshot.document,
            snapshot,
            backup,
        )
        transaction.close()
    fixed = _convert_current_transaction_to_legacy(settings_path)
    synced: list[tuple[file_io.PathIdentity, Path]] = []
    events: list[tuple[str, Path]] = []
    retained_documents: dict[Path, tuple[Path, bytes]] = {}
    real_sync = template._fsync_pinned_directory
    real_retire = template._retain_regular_intact

    def observe_sync(pin: file_io.PinnedPath, logical_path: Path) -> None:
        real_sync(pin, logical_path)
        synced.append((pin.identity, logical_path))
        events.append(("sync", logical_path))

    def observe_retirement(path: Path, **kwargs: object) -> Path:
        logical_path = kwargs.get("logical_path")
        assert isinstance(logical_path, Path)
        document = path.read_bytes()
        retained = real_retire(path, **kwargs)  # type: ignore[arg-type]
        retained_documents[logical_path] = retained, document
        events.append(("retire", logical_path))
        return retained

    monkeypatch.setattr(template, "_fsync_pinned_directory", observe_sync)
    monkeypatch.setattr(template, "_retain_regular_intact", observe_retirement)

    result = template.uninstall(reload_config=False)

    assert not result.changed
    assert len(synced) == 3
    assert all(logical_path == fixed for _identity, logical_path in synced)
    assert len({identity for identity, _logical_path in synced}) == 1
    assert tuple(fixed.iterdir()) == ()
    swap = fixed / "swap"
    record = fixed / "record.json"
    swap_retirement = events.index(("retire", swap))
    first_barrier = events.index(("sync", fixed))
    record_retirement = events.index(("retire", record))
    assert swap_retirement < first_barrier < record_retirement
    retained_swap, swap_document = retained_documents[swap]
    assert swap_document == SAMPLE_SETTINGS.encode()
    assert retained_swap.read_bytes() == swap_document


def test_legacy_recovery_accepts_lock_only_residue_after_sigkill(
    fake_home: Path,
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    with template._read_settings_snapshot(settings_path) as snapshot:
        backup = template._backup(settings_path, snapshot)
        transaction = template._begin_publication_transaction(
            settings_path,
            snapshot.document,
            snapshot,
            backup,
        )
        transaction.close()
    fixed = _convert_current_transaction_to_legacy(settings_path)
    script = """
import os
import signal
from wall_in_one.theme import template

real_retire = template._retain_recovery_record_intact

def kill_after_legacy_record(path, entry, **kwargs):
    real_retire(path, entry, **kwargs)
    if path.name == "record.json":
        os.kill(os.getpid(), signal.SIGKILL)

template._retain_recovery_record_intact = kill_after_legacy_record
template.uninstall(reload_config=False)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    assert completed.returncode == -signal.SIGKILL
    assert {entry.name for entry in fixed.iterdir()} == {"lock"}

    result = template.uninstall(reload_config=False)

    assert not result.changed
    assert result.detail == "not registered"
    assert {entry.name for entry in fixed.iterdir()} == {"lock"}
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS


def test_recovery_tolerates_and_preserves_retained_transaction_residue(
    fake_home: Path,
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    with template._read_settings_snapshot(settings_path) as snapshot:
        backup = template._backup(settings_path, snapshot)
        transaction = template._begin_publication_transaction(
            settings_path,
            snapshot.document,
            snapshot,
            backup,
        )
        transaction.close()
    fixed = _convert_current_transaction_to_legacy(settings_path)

    retained = fixed / file_io.RETAINED_ENTRY_DIRECTORY
    retained.mkdir(mode=0o700)
    (retained / "prior-evidence").write_text("keep", encoding="utf-8")

    result = template.uninstall(reload_config=False)

    assert not result.changed
    assert fixed.is_dir()
    assert (retained / "prior-evidence").read_text(encoding="utf-8") == "keep"
    assert {entry.name for entry in fixed.iterdir()} == {file_io.RETAINED_ENTRY_DIRECTORY}


def test_install_syncs_every_published_transaction_entry(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    synced: list[Path] = []
    monkeypatch.setattr(template, "_fsync_parent", synced.append)

    result = template.install()

    assert result.backup_path is not None
    backup_sync = next(
        path
        for path in synced
        if path.name.startswith(f"{settings_path.name}.bak-{template.TEMPLATE_ID}-")
    )
    assert synced.index(template.installed_template_path().parent) < synced.index(backup_sync)
    assert result.backup_path in synced
    locator = template._transaction_locator(settings_path)
    assert locator in synced
    assert any("transaction-locator-stage-" in path.name for path in synced)
    lock_sync = next(path for path in synced if "transaction-lock-" in path.name)
    assert synced.index(locator) < synced.index(lock_sync)


def test_uncertain_locator_publication_preserves_every_replay_dependency(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    locator = template._transaction_locator(settings_path)
    real_move = file_io.atomic_move_no_replace
    injected = False

    def report_uncertain_after_locator_move(
        source: Path,
        destination: Path,
        **kwargs: object,
    ) -> None:
        nonlocal injected
        real_move(source, destination, **kwargs)  # type: ignore[arg-type]
        if destination == locator and not injected:
            injected = True
            raise file_io.PathChangedError(
                "injected post-move locator verification failure",
                preserved_path=destination,
            )

    monkeypatch.setattr(file_io, "atomic_move_no_replace", report_uncertain_after_locator_move)

    with pytest.raises(
        template.TemplateInstallError,
        match="post-move locator verification failure",
    ):
        template.install(reload_config=False)

    assert injected
    preserved_locator, candidate, lock = _current_transaction_entries(settings_path)
    assert preserved_locator == locator
    assert candidate.is_file()
    assert candidate.read_bytes()
    assert lock.is_file()
    assert settings_path.read_text(encoding="utf-8") == SAMPLE_SETTINGS

    monkeypatch.setattr(file_io, "atomic_move_no_replace", real_move)
    result = template.install(reload_config=False)

    assert result.changed
    assert template._BEGIN_MARKER in settings_path.read_text(encoding="utf-8")
    assert not locator.exists()
    assert not candidate.exists()
    assert not lock.exists()


def test_begin_failure_retires_and_syncs_candidate_before_locator(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    locator = template._transaction_locator(settings_path)
    events: list[tuple[str, Path]] = []
    real_sync = template._fsync_parent
    real_discard = file_io.discard_regular_if_same
    real_retire = template._retain_regular_intact
    retained_documents: dict[Path, tuple[Path, bytes]] = {}
    injected = False

    def fail_first_published_locator_sync(path: Path) -> None:
        nonlocal injected
        if path == locator and not injected:
            injected = True
            raise OSError(errno.EIO, "injected published-locator sync failure")
        real_sync(path)
        events.append(("sync-parent", path.parent))

    def observe_discard(path: Path, **kwargs: object) -> bool:
        events.append(("discard", path))
        return real_discard(path, **kwargs)  # type: ignore[arg-type]

    def observe_retirement(path: Path, **kwargs: object) -> Path:
        document = path.read_bytes()
        retained = real_retire(path, **kwargs)  # type: ignore[arg-type]
        retained_documents[path] = retained, document
        events.append(("retire", path))
        return retained

    monkeypatch.setattr(template, "_fsync_parent", fail_first_published_locator_sync)
    monkeypatch.setattr(file_io, "discard_regular_if_same", observe_discard)
    monkeypatch.setattr(template, "_retain_regular_intact", observe_retirement)

    with template._read_settings_snapshot(settings_path) as snapshot:
        backup = template._backup(settings_path, snapshot)
        with pytest.raises(template.TemplateInstallError, match="published-locator sync failure"):
            template._begin_publication_transaction(
                settings_path,
                snapshot.document,
                snapshot,
                backup,
            )

    candidate = next(path for path in retained_documents if "transaction-candidate-" in path.name)
    candidate_retirement = events.index(("retire", candidate))
    candidate_barrier = next(
        index
        for index, event in enumerate(events)
        if index > candidate_retirement and event == ("sync-parent", settings_path.parent)
    )
    locator_retirement = events.index(("retire", locator))
    locator_barrier = next(
        index
        for index, event in enumerate(events)
        if index > locator_retirement and event == ("sync-parent", settings_path.parent)
    )
    lock_discard = next(
        index
        for index, (operation, path) in enumerate(events)
        if index > locator_barrier and operation == "discard" and "transaction-lock-" in path.name
    )
    assert (
        candidate_retirement
        < candidate_barrier
        < locator_retirement
        < locator_barrier
        < lock_discard
    )
    retained_candidate, candidate_document = retained_documents[candidate]
    assert candidate_document == SAMPLE_SETTINGS.encode()
    assert retained_candidate.read_bytes() == candidate_document
    assert not locator.exists()


def test_preexchange_cleanup_retires_and_syncs_candidate_before_locator(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    with template._read_settings_snapshot(settings_path) as snapshot:
        expected_candidate = snapshot.document
        backup = template._backup(settings_path, snapshot)
        transaction = template._begin_publication_transaction(
            settings_path,
            expected_candidate,
            snapshot,
            backup,
        )

    events: list[tuple[str, Path]] = []
    retained_documents: dict[Path, tuple[Path, bytes]] = {}
    real_retire = template._retain_regular_intact
    real_sync = template._fsync_parent

    def observe_retirement(path: Path, **kwargs: object) -> Path:
        document = path.read_bytes()
        retained = real_retire(path, **kwargs)  # type: ignore[arg-type]
        retained_documents[path] = retained, document
        events.append(("retire", path))
        return retained

    def observe_sync(path: Path) -> None:
        real_sync(path)
        events.append(("sync-parent", path.parent))

    monkeypatch.setattr(template, "_retain_regular_intact", observe_retirement)
    monkeypatch.setattr(template, "_fsync_parent", observe_sync)
    try:
        remainder = template._cleanup_publication_transaction(
            transaction,
            discard_candidate=True,
        )
    finally:
        transaction.close()

    assert remainder is None
    candidate_retirement = events.index(("retire", transaction.swap))
    candidate_barrier = next(
        index
        for index, event in enumerate(events)
        if index > candidate_retirement and event == ("sync-parent", settings_path.parent)
    )
    locator_retirement = events.index(("retire", transaction.record))
    assert candidate_retirement < candidate_barrier < locator_retirement
    retained_candidate, candidate_document = retained_documents[transaction.swap]
    assert candidate_document == expected_candidate
    assert retained_candidate.read_bytes() == expected_candidate
    assert not transaction.swap.exists()
    assert not transaction.record.exists()
    assert not transaction.lock.exists()


def test_post_hook_quotes_an_executable_path_with_shell_metacharacters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = "/tmp/a helper's directory/wall-in-one"
    monkeypatch.setattr(shutil, "which", lambda _name: executable)

    command = template._post_hook_command()

    assert shlex.split(command) == [executable, "ctl", "reload-palette"]
    assert command != f"{executable} ctl reload-palette"


def test_install_refuses_to_clobber_a_hand_written_entry(fake_home: Path) -> None:
    _write_noctalia_settings(
        SAMPLE_SETTINGS
        + '\n[theme.templates.user.wall-in-one]\nenabled = true\ninput_path = "/somewhere/else"\n'
    )
    with pytest.raises(template.TemplateInstallError, match="not written by us"):
        template.install()


def test_uninstall_removes_only_our_block(fake_home: Path) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    template.install()

    result = template.uninstall()

    assert result.changed
    body = settings_path.read_text(encoding="utf-8")
    assert template._BEGIN_MARKER not in body
    parsed = tomllib.loads(body)
    assert "user" not in parsed["theme"]["templates"]
    assert parsed["theme"]["templates"]["builtin_ids"] == ["gtk3", "qt"]
    assert parsed["wallpaper"]["directory"] == "/home/someone/wallpapers"


def test_uninstall_is_a_no_op_when_absent(fake_home: Path) -> None:
    _write_noctalia_settings(SAMPLE_SETTINGS)
    result = template.uninstall()
    assert not result.changed
    assert result.detail == "not registered"


def test_install_reports_a_missing_settings_file(fake_home: Path) -> None:
    with pytest.raises(template.TemplateInstallError, match="not found"):
        template.install()


def test_install_reports_malformed_settings(fake_home: Path) -> None:
    _write_noctalia_settings("this = is = not = toml")
    with pytest.raises(template.TemplateInstallError, match="not valid TOML"):
        template.install()


def test_settings_descriptor_closes_after_utf8_read_failure(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = paths.noctalia_settings_path()
    settings_path.parent.mkdir(parents=True)
    settings_path.write_bytes(b"\xff")
    real_open = os.open
    opened: list[int] = []

    def capture_open(path: Path, flags: int) -> int:
        descriptor = real_open(path, flags)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", capture_open)

    with pytest.raises(template.TemplateInstallError, match="not UTF-8"):
        template.install()

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_settings_descriptor_closes_after_toml_parse_failure(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_noctalia_settings("this = is = not = toml")
    real_read = template._read_settings_snapshot
    snapshots: list[template._SettingsSnapshot] = []

    def capture_snapshot(path: Path) -> template._SettingsSnapshot:
        snapshot = real_read(path)
        snapshots.append(snapshot)
        return snapshot

    monkeypatch.setattr(template, "_read_settings_snapshot", capture_snapshot)

    with pytest.raises(template.TemplateInstallError, match="not valid TOML"):
        template.install()

    assert len(snapshots) == 1
    assert snapshots[0]._closed
    with pytest.raises(OSError):
        os.fstat(snapshots[0]._descriptor)


def test_snapshot_and_candidate_descriptors_close_after_publication_failure(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_noctalia_settings(SAMPLE_SETTINGS)
    destination = template.installed_template_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(template.bundled_template().read_bytes())
    real_read = template._read_settings_snapshot
    real_backup = template._backup
    real_begin = template._begin_publication_transaction
    snapshots: list[template._SettingsSnapshot] = []
    transactions: list[template._PublicationTransaction] = []

    def capture_snapshot(path: Path) -> template._SettingsSnapshot:
        snapshot = real_read(path)
        snapshots.append(snapshot)
        return snapshot

    def capture_transaction(
        path: Path,
        candidate_document: bytes,
        snapshot: template._SettingsSnapshot,
        backup: Path,
    ) -> template._PublicationTransaction:
        transaction = real_begin(path, candidate_document, snapshot, backup)
        transactions.append(transaction)
        return transaction

    def replace_after_backup(path: Path, snapshot: template._SettingsSnapshot) -> Path:
        backup = real_backup(path, snapshot)
        path.write_text(SAMPLE_SETTINGS + "\n# concurrent replacement\n", encoding="utf-8")
        return backup

    monkeypatch.setattr(template, "_read_settings_snapshot", capture_snapshot)
    monkeypatch.setattr(template, "_begin_publication_transaction", capture_transaction)
    monkeypatch.setattr(template, "_backup", replace_after_backup)

    with pytest.raises(template.TemplateInstallError, match="being prepared"):
        template.install()

    assert len(snapshots) == 1
    assert snapshots[0]._closed
    assert len(transactions) == 1
    transaction = transactions[0]
    assert transaction._closed
    for descriptor in (
        transaction._candidate_descriptor,
        transaction._record_descriptor,
        transaction._lock_descriptor,
    ):
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert not transaction.record.exists()
    assert not transaction.swap.exists()
    assert not transaction.lock.exists()


def test_validate_transaction_directory_closes_after_fstat_failure(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = fake_home / "private-transaction"
    directory.mkdir(mode=0o700)
    real_open = os.open
    real_fstat = os.fstat
    descriptor: int | None = None

    def capture_open(
        path: os.PathLike[str] | str,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        nonlocal descriptor
        opened = real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        if Path(path) == directory:
            descriptor = opened
        return opened

    def fail_pinned_fstat(opened: int) -> os.stat_result:
        if opened == descriptor:
            raise OSError(errno.EIO, "injected transaction fstat failure")
        return real_fstat(opened)

    monkeypatch.setattr(os, "open", capture_open)
    monkeypatch.setattr(os, "fstat", fail_pinned_fstat)

    with pytest.raises(template.TemplateInstallError, match="cannot safely open"):
        template._validate_transaction_directory(directory)

    assert descriptor is not None
    with pytest.raises(OSError):
        real_fstat(descriptor)


def test_backup_closes_and_preserves_unproven_temporary_after_initial_fstat_failure(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    real_mkstemp = tempfile.mkstemp
    real_fstat = os.fstat
    created: tuple[int, Path] | None = None

    def capture_mkstemp(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | os.PathLike[str] | None = None,
        text: bool = False,
    ) -> tuple[int, str]:
        nonlocal created
        descriptor, name = real_mkstemp(suffix=suffix, prefix=prefix, dir=dir, text=text)
        created = descriptor, Path(name)
        return descriptor, name

    def fail_created_fstat(descriptor: int) -> os.stat_result:
        if created is not None and descriptor == created[0]:
            raise OSError(errno.EIO, "injected backup fstat failure")
        return real_fstat(descriptor)

    with template._read_settings_snapshot(settings_path) as snapshot:
        monkeypatch.setattr(tempfile, "mkstemp", capture_mkstemp)
        monkeypatch.setattr(os, "fstat", fail_created_fstat)
        with pytest.raises(template.TemplateInstallError, match="cannot create a backup"):
            template._backup(settings_path, snapshot)

    assert created is not None
    descriptor, temporary = created
    with pytest.raises(OSError):
        real_fstat(descriptor)
    assert temporary.exists()


def test_backup_close_is_not_skipped_when_owned_cleanup_fails(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    real_mkstemp = tempfile.mkstemp
    real_fchmod = os.fchmod
    created_descriptor: int | None = None

    def capture_mkstemp(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | os.PathLike[str] | None = None,
        text: bool = False,
    ) -> tuple[int, str]:
        nonlocal created_descriptor
        descriptor, name = real_mkstemp(suffix=suffix, prefix=prefix, dir=dir, text=text)
        created_descriptor = descriptor
        return descriptor, name

    def fail_created_fchmod(descriptor: int, mode: int) -> None:
        if descriptor == created_descriptor:
            raise OSError(errno.EIO, "injected backup setup failure")
        real_fchmod(descriptor, mode)

    def fail_cleanup(*_args: object, **_kwargs: object) -> bool:
        raise OSError(errno.EIO, "injected backup cleanup failure")

    with template._read_settings_snapshot(settings_path) as snapshot:
        monkeypatch.setattr(tempfile, "mkstemp", capture_mkstemp)
        monkeypatch.setattr(os, "fchmod", fail_created_fchmod)
        monkeypatch.setattr(template, "_discard_candidate_if_owned", fail_cleanup)
        with pytest.raises(template.TemplateInstallError, match="cannot create a backup"):
            template._backup(settings_path, snapshot)

    assert created_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(created_descriptor)


def test_write_bytes_atomically_closes_unnamed_descriptor_when_link_fails(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = fake_home / "state" / "palette.json.tmpl"
    destination.parent.mkdir(parents=True, exist_ok=True)
    real_open = os.open
    created_descriptor: int | None = None

    def capture_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal created_descriptor
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if flags & os.O_TMPFILE:
            created_descriptor = descriptor
        return descriptor

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, "injected unnamed-template link failure")

    monkeypatch.setattr(os, "open", capture_open)
    monkeypatch.setattr(os, "link", fail_link)

    with pytest.raises(template.TemplateInstallError, match="injected unnamed-template"):
        template._write_bytes_atomically(destination, b"palette")

    assert created_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(created_descriptor)
    assert not destination.exists()


def test_publication_close_attempts_every_descriptor_after_first_close_failure(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    with template._read_settings_snapshot(settings_path) as snapshot:
        backup = template._backup(settings_path, snapshot)
        transaction = template._begin_publication_transaction(
            settings_path,
            snapshot.document,
            snapshot,
            backup,
        )

    owned = [
        transaction._candidate_descriptor,
        transaction._record_descriptor,
        transaction._lock_descriptor,
    ]
    real_close = os.close
    attempted: list[int] = []

    def fail_first_close(descriptor: int) -> None:
        if descriptor in owned:
            attempted.append(descriptor)
            real_close(descriptor)
            if descriptor == owned[0]:
                raise OSError(errno.EIO, "injected first close failure")
            return
        real_close(descriptor)

    monkeypatch.setattr(os, "close", fail_first_close)

    with pytest.raises(OSError, match="injected first close failure"):
        transaction.close()
    transaction.close()

    assert attempted == owned
    assert transaction._closed


def test_begin_failure_closes_later_descriptors_after_candidate_close_failure(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    real_mkstemp = tempfile.mkstemp
    real_close = os.close
    candidate_descriptor: int | None = None
    lock_descriptor: int | None = None
    close_attempts: list[int] = []

    def fail_record_creation(
        suffix: str | None = None,
        prefix: str | None = None,
        dir: str | os.PathLike[str] | None = None,
        text: bool = False,
    ) -> tuple[int, str]:
        nonlocal candidate_descriptor, lock_descriptor
        if prefix is not None and "transaction-locator-stage-" in prefix:
            raise OSError(errno.EIO, "injected record open failure")
        descriptor, name = real_mkstemp(suffix=suffix, prefix=prefix, dir=dir, text=text)
        if prefix is not None and "transaction-candidate-" in prefix:
            candidate_descriptor = descriptor
        elif prefix is not None and "transaction-lock-" in prefix:
            lock_descriptor = descriptor
        return descriptor, name

    def fail_candidate_close(descriptor: int) -> None:
        if descriptor in {candidate_descriptor, lock_descriptor}:
            close_attempts.append(descriptor)
        real_close(descriptor)
        if descriptor == candidate_descriptor:
            raise OSError(errno.EIO, "injected candidate close failure")

    monkeypatch.setattr(tempfile, "mkstemp", fail_record_creation)
    monkeypatch.setattr(os, "close", fail_candidate_close)

    with (
        template._read_settings_snapshot(settings_path) as snapshot,
        pytest.raises(template.TemplateInstallError, match="injected record open failure"),
    ):
        template._begin_publication_transaction(
            settings_path,
            snapshot.document,
            snapshot,
            settings_path.with_name("backup.toml"),
        )

    assert candidate_descriptor is not None
    assert lock_descriptor is not None
    assert close_attempts.count(candidate_descriptor) == 1
    assert close_attempts.count(lock_descriptor) == 1
    with pytest.raises(OSError):
        os.fstat(lock_descriptor)


def test_recovery_close_attempts_every_entry_after_first_close_failure(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    with template._read_settings_snapshot(settings_path) as snapshot:
        backup = template._backup(settings_path, snapshot)
        transaction = template._begin_publication_transaction(
            settings_path,
            snapshot.document,
            snapshot,
            backup,
        )
        transaction.close()

    real_open_entry = template._open_recovery_entry
    real_close_entry = template._RecoveryEntry.close
    opened_entries: list[template._RecoveryEntry] = []
    close_attempts = 0

    def capture_entry(*args: object, **kwargs: object) -> template._RecoveryEntry | None:
        entry = real_open_entry(*args, **kwargs)  # type: ignore[arg-type]
        if entry is not None:
            opened_entries.append(entry)
        return entry

    def fail_first_entry_close(entry: template._RecoveryEntry) -> None:
        nonlocal close_attempts
        close_attempts += 1
        real_close_entry(entry)
        if close_attempts == 1:
            raise OSError(errno.EIO, "injected recovery close failure")

    monkeypatch.setattr(template, "_open_recovery_entry", capture_entry)
    monkeypatch.setattr(template._RecoveryEntry, "close", fail_first_entry_close)

    with pytest.raises(OSError, match="injected recovery close failure"):
        template.uninstall(reload_config=False)

    assert len(opened_entries) >= 4
    assert close_attempts == len(opened_entries)
    assert all(entry._closed for entry in opened_entries)


def test_install_reports_pathologically_nested_settings(fake_home: Path) -> None:
    _write_noctalia_settings("value = " + "[" * 1_100 + "0" + "]" * 1_100)

    with pytest.raises(template.TemplateInstallError, match="not valid TOML"):
        template.install()


@pytest.mark.parametrize("kind", ("symlink", "fifo"))
def test_installer_refuses_unsafe_noctalia_settings_without_outside_writes(
    fake_home: Path, kind: str
) -> None:
    settings = paths.noctalia_settings_path()
    settings.parent.mkdir(parents=True)
    outside = fake_home / "precious.toml"
    outside.write_text(SAMPLE_SETTINGS, encoding="utf-8")
    if kind == "symlink":
        settings.symlink_to(outside)
    else:
        os.mkfifo(settings)

    with pytest.raises(template.TemplateInstallError, match="safely read"):
        template.install()

    assert outside.read_text(encoding="utf-8") == SAMPLE_SETTINGS


def test_status_tracks_installation(fake_home: Path) -> None:
    _write_noctalia_settings(SAMPLE_SETTINGS)
    assert template.status() == "not installed"
    template.install()
    assert template.status().startswith("installed, enabled")


def test_edit_settings_backs_up_then_exchanges_and_releases_the_file(fake_home: Path) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    settings_path.chmod(0o644)
    original = settings_path.read_bytes()
    expected = hashlib.sha256(original).hexdigest()

    edit = template.edit_settings(expected, lambda text: text.replace('wallpaper"', 'custom"'))

    assert settings_path.read_text() == SAMPLE_SETTINGS.replace('wallpaper"', 'custom"')
    assert edit.before_sha256 == expected
    assert edit.after_sha256 == hashlib.sha256(settings_path.read_bytes()).hexdigest()
    assert edit.backup_path.read_bytes() == original
    assert edit.displaced_path.read_bytes() == original
    assert edit.displaced_path.name == f"{edit.backup_path.name}.original"
    _assert_no_current_transaction(settings_path)
    assert template.read_settings_document() == settings_path.read_bytes()
    assert stat.S_IMODE(settings_path.stat().st_mode) == 0o644, "Noctalia's permissions stay"


def test_edit_settings_never_writes_a_file_its_readers_would_refuse(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Noctalia's settings are read back with a cap (MAX_NOCTALIA_SETTINGS_BYTES).
    An edit that would take the file past it is refused before anything is
    written, backups and transaction records included."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    original = settings_path.read_bytes()
    before = sorted(path.name for path in settings_path.parent.iterdir())
    monkeypatch.setattr(template, "MAX_NOCTALIA_SETTINGS_BYTES", len(original) + 8)

    with pytest.raises(template.TemplateInstallError, match="this version can read back"):
        template.edit_settings(
            hashlib.sha256(original).hexdigest(),
            lambda text: text + "\n# longer than the eight bytes left\n",
        )

    assert settings_path.read_bytes() == original
    assert sorted(path.name for path in settings_path.parent.iterdir()) == before
    assert template.read_settings_document() == original


def test_edit_settings_refuses_a_file_rewritten_since_it_was_read(fake_home: Path) -> None:
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    seen = hashlib.sha256(settings_path.read_bytes()).hexdigest()
    # Noctalia saves its own settings with an atomic rename.
    replacement = settings_path.with_name("settings.toml.noctalia-tmp")
    replacement.write_text(SAMPLE_SETTINGS + '\n[bar]\nposition = "top"\n')
    os.replace(replacement, settings_path)
    rewritten = settings_path.read_bytes()
    before = sorted(path.name for path in settings_path.parent.iterdir())

    with pytest.raises(template.SettingsChangedError):
        template.edit_settings(seen, lambda text: text + "\n# never written\n")

    assert settings_path.read_bytes() == rewritten
    assert sorted(path.name for path in settings_path.parent.iterdir()) == before


def test_a_reused_descriptor_never_names_an_unrelated_folder(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cold review's repro of 700ed2c: a legacy lock's discard claims it and
    can neither verify nor undo the move; by the time the template error is made
    the inner descriptor its text named is closed, and another thread has opened
    an unrelated folder, which gets that number. The message names the lock where
    it was (said at the source, while its descriptor was live), never that folder."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    _interrupted_transaction(settings_path)
    legacy = _convert_current_transaction_to_legacy(settings_path)
    mode = stat.S_IMODE(legacy.stat().st_mode)
    _double_fault_on_move(
        monkeypatch,
        legacy,
        lambda source, destination: source.name == "lock" and destination.name == "entry",
    )
    unrelated = tmp_path / "somebody-elses-unrelated-folder"
    unrelated.mkdir()
    held: list[int] = []
    real_cannot_retire = template._cannot_retire

    def cannot_retire(
        what: str, displayed_path: Path, error: OSError
    ) -> template.TemplateInstallError:
        held.append(os.open(unrelated, os.O_RDONLY | os.O_DIRECTORY))  # another thread
        return real_cannot_retire(what, displayed_path, error)

    monkeypatch.setattr(template, "_cannot_retire", cannot_retire)
    try:
        with pytest.raises(template.TemplateInstallError) as raised:
            template.uninstall(reload_config=False)
    finally:
        legacy.chmod(mode)
        for descriptor in held:
            os.close(descriptor)

    said = str(raised.value)
    # Failed on 700ed2c: "could not verify <the unrelated folder>/lock".
    assert str(unrelated) not in said, said
    assert "/proc/" not in said, said
    assert f"could not verify {legacy}/lock after its atomic claim" in said, said


def test_template_errors_never_carry_a_descriptor_spelling() -> None:
    """The last word: any descriptor spelling left in a template error becomes a
    fixed description; it is never looked up (its number may be someone else's)."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        error = template.TemplateInstallError(f"cannot sync /proc/self/fd/{descriptor}/x")
    finally:
        os.close(descriptor)

    assert str(error) == f"cannot sync {file_io.DESCRIPTOR_STAND_IN}/x"


# -- a retirement is durable where it went before it is called retired (AC-3's class) --


@pytest.mark.parametrize("synced", [True, False])
def test_a_moved_back_settings_copy_is_only_called_retired_once_that_is_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synced: bool
) -> None:
    """The backup copy of Noctalia's settings was public for a moment and moved
    back to its temporary name; it is retired intact. That is said with the syncs
    behind it: the retained folder's own name, the move, the new link, then the
    old one. When the retained folder's sync fails, what is said is where the
    copy is and that this could not be confirmed durable."""
    folder = tmp_path / "noctalia"
    folder.mkdir()
    temporary = folder / ".settings.toml.bak.tmp"
    document = b'[theme]\nmode = "dark"\n'
    temporary.write_bytes(document)
    identity = file_io.path_identity(temporary)
    events: list[str] = []
    real_rename = file_io._rename_noreplace
    real_sync = file_io._fsync_directory_capability

    def rename(source: Path, destination: Path) -> None:
        real_rename(source, destination)
        events.append("move")

    def sync(
        descriptor: int, *, expected_identity: file_io.PathIdentity, logical_path: Path
    ) -> None:
        events.append(f"sync {logical_path.name}")
        real_sync(descriptor, expected_identity=expected_identity, logical_path=logical_path)

    monkeypatch.setattr(file_io, "_rename_noreplace", rename)
    monkeypatch.setattr(file_io, "_fsync_directory_capability", sync)
    if not synced:
        sync_faults.fail_the_sync_after_a_retirement(monkeypatch)
    descriptor = os.open(temporary, os.O_RDONLY | os.O_CLOEXEC)
    try:
        said = template._settle_relocated_backup(temporary, descriptor, identity, document)
    finally:
        os.close(descriptor)

    (kept,) = (folder / file_io.RETAINED_ENTRY_DIRECTORY).iterdir()
    assert kept.read_bytes() == document and not temporary.exists()
    if synced:
        # Failed on 7bdc1a6: ["move"], with no sync at all.
        assert events == [
            "sync noctalia",
            "move",
            f"sync {file_io.RETAINED_ENTRY_DIRECTORY}",
            "sync noctalia",
        ]
        assert said.endswith(f"was retired intact to {kept}"), said
    else:
        # Failed on 7bdc1a6: "... was retired intact to <path>".
        assert said == f"; what was at {temporary} was moved to {kept}{sync_faults.UNCONFIRMED}"


# -- a folder that can't be written is said as that, not as something missing ----------


@pytest.mark.parametrize("state_folder", ["there, read-only", "not there, in a read-only folder"])
def test_install_says_the_template_folder_cannot_be_written_and_changes_nothing(
    fake_home: Path, state_folder: str
) -> None:
    """Wall-in-One's state folder can't be written (a native EACCES): the
    template can't be put there. The reason given is that, and that nothing was
    changed; not "cannot verify ... No such file", which is what the fallback
    for a concurrent installer said once the write had failed."""
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    before = settings_path.read_bytes()
    state = paths.app_state_dir()
    if state_folder == "there, read-only":
        state.mkdir(parents=True)
        unwritable = state
    else:
        unwritable = state.parent
    mode = stat.S_IMODE(unwritable.stat().st_mode)
    unwritable.chmod(0o500)
    try:
        # Failed on 824f750: "cannot verify palette template ...: [Errno 2] No such file
        # or directory", or a bare PermissionError when the folder was to be made.
        with pytest.raises(template.TemplateInstallError) as refused:
            template.install()
    finally:
        unwritable.chmod(mode)

    assert str(refused.value) == (
        f"{unwritable} can't be written (Permission denied), so the palette template "
        "wasn't installed; nothing was changed"
    )
    assert settings_path.read_bytes() == before
    assert sorted(entry.name for entry in settings_path.parent.iterdir()) == ["settings.toml"]
    assert not state.exists() or list(state.iterdir()) == []


# -- Noctalia's folder reached through a symbolic link ----------------------------------


def _linked_noctalia_folder(tmp_path: Path) -> Path:
    """Noctalia's state folder as a link to a folder on "another drive"; the real one."""
    real = tmp_path / "another-drive" / "noctalia"
    real.mkdir(parents=True)
    linked = paths.noctalia_state_dir()
    linked.parent.mkdir(parents=True, exist_ok=True)
    linked.symlink_to(real)
    return real


def _transaction_entries(folder: Path) -> list[str]:
    return sorted(
        entry.name for entry in folder.iterdir() if "wall-in-one-transaction" in entry.name
    )


def test_installs_and_uninstalls_in_a_linked_noctalia_folder_leave_no_lock(
    fake_home: Path, tmp_path: Path
) -> None:
    """Noctalia's folder being a symbolic link is an ordinary set-up. Each install
    and uninstall there finishes its own cleanup: no lock file is left, and none
    ends with "... its recovery cleanup remains at <lock>"."""
    real = _linked_noctalia_folder(tmp_path)
    settings_path = _write_noctalia_settings(SAMPLE_SETTINGS)
    assert settings_path.parent.is_symlink() and settings_path.resolve().parent == real

    for step in (template.install, template.uninstall, template.install, template.uninstall):
        # Failed on 824f750: TemplateInstallError("... was updated, but its recovery
        # cleanup remains at <lock>"), and one more lock file each time.
        step()
        assert _transaction_entries(real) == [], step.__name__

    assert (
        tomllib.loads(settings_path.read_text(encoding="utf-8"))["theme"]["source"] == "wallpaper"
    )


def test_locks_left_in_a_linked_noctalia_folder_are_cleaned_by_the_next_install(
    fake_home: Path, tmp_path: Path
) -> None:
    """What earlier versions left: one orphaned lock for every install or
    uninstall in a linked folder. The next install's reconcile discards them, as
    it does in a folder that is not a link."""
    real = _linked_noctalia_folder(tmp_path)
    _write_noctalia_settings(SAMPLE_SETTINGS)
    for suffix in ("aaaa1111", "bbbb2222"):
        left = real / f".settings.toml.wall-in-one-transaction-lock-{suffix}"
        left.write_bytes(b"")
        left.chmod(0o600)

    template.install()

    # Failed on 824f750: both stayed, and the install left a third.
    assert _transaction_entries(real) == []


@pytest.mark.parametrize("other_folder", ["holds nothing by that name", "holds another file there"])
def test_a_link_swapped_to_another_folder_is_still_refused(
    fake_home: Path, tmp_path: Path, other_folder: str
) -> None:
    """What is accepted is the folder that holds this exact entry, not whatever
    the link leads to. The link is swapped to another folder before the cleanup:
    nothing is discarded there, nothing is made there, and the entry stays where
    it really is."""
    real = _linked_noctalia_folder(tmp_path)
    linked = paths.noctalia_state_dir()
    lock = linked / ".settings.toml.wall-in-one-transaction-lock-abcd1234"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    fingerprint = file_io.regular_file_fingerprint(lock)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    theirs = elsewhere / lock.name
    if other_folder == "holds another file there":
        theirs.write_bytes(b"somebody else's")
    linked.unlink()
    linked.symlink_to(elsewhere)  # swapped

    removed = template._discard_beside_settings(
        lock, expected_identity=fingerprint[:2], expected_fingerprint=fingerprint
    )

    assert removed is False
    assert file_io.regular_file_fingerprint(real / lock.name)[:2] == fingerprint[:2], "still there"
    expected = [lock.name] if other_folder == "holds another file there" else []
    assert sorted(entry.name for entry in elsewhere.iterdir()) == expected, "nothing made there"
    if expected:
        assert theirs.read_bytes() == b"somebody else's"
