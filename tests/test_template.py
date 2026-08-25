from __future__ import annotations

import errno
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
from datetime import datetime
from pathlib import Path

import pytest

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
    assert str(locator) in str(caught.value)
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
