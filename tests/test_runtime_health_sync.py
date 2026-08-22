"""The daemon-only health bridge stays headless, atomic and app-owned."""

from __future__ import annotations

import builtins
import json
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from wall_in_one import cli, config, paths, runtime_config, runtime_health
from wall_in_one.control import client
from wall_in_one.control.protocol import Response
from wall_in_one.library import pairings
from wall_in_one.library.model import Kind, MediaItem


def _media(tmp_path: Path) -> MediaItem:
    root = tmp_path / "library"
    root.mkdir()
    picture = root / "paper.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n")
    config.save(config.Settings(roots=(root,), scan_workshop=False))
    metadata = picture.stat()
    return MediaItem(
        path=picture,
        kind=Kind.STILL,
        size=metadata.st_size,
        mtime=int(metadata.st_mtime),
    )


def _write_runtime_config() -> str:
    assert cli.main(["--write-config"]) == 0
    return runtime_config.read_config_generation()


def _snapshot(
    item: MediaItem,
    *,
    reports: bool = True,
    omitted: int = 0,
    generation: str | None = None,
    durable: bool = False,
    config_epoch: int = 1,
    observed_config_epoch: int | None = None,
    extra_reports: tuple[dict[str, object], ...] = (),
) -> Response:
    effective_generation = generation or runtime_config.read_config_generation()
    taboo = (
        [
            {
                "playlist_id": runtime_config.FALLBACK_PLAYLIST_ID,
                "entry_id": runtime_config.entry_id_for_source(item.path),
                "reason": "renderer rejected this wallpaper",
                "source": "automatic-apply",
                "durable": durable,
                "observed_config_epoch": observed_config_epoch or config_epoch,
            }
        ]
        if reports
        else []
    )
    for extra in extra_reports:
        report = dict(extra)
        report.setdefault("observed_config_epoch", config_epoch)
        taboo.append(report)
    return Response.success(
        json.dumps(
            {
                "config_generation": effective_generation,
                "config_path": str(paths.runtime_config_path().absolute()),
                "runtime_instance": "a" * runtime_health.RUNTIME_INSTANCE_HEX_CHARS,
                "config_epoch": config_epoch,
                "playlist_id": runtime_config.FALLBACK_PLAYLIST_ID,
                "playlist": runtime_config.FALLBACK_PLAYLIST_NAME,
                "source": "schedule",
                "taboo_entries": taboo,
                "taboo_entries_omitted": omitted,
            }
        )
    )


def test_health_sync_returns_not_running_before_reading_authoring_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def headless_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        assert name != "gi" and not name.startswith("wall_in_one.ui"), name
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", headless_import)
    monkeypatch.delitem(sys.modules, "wall_in_one.ui.app", raising=False)
    monkeypatch.setattr(
        client,
        "send_runtime",
        lambda _verb: (_ for _ in ()).throw(client.NotRunningError("runtime absent")),
    )

    assert cli.main(["--sync-runtime-health"]) == client.EXIT_NOT_RUNNING
    assert not pairings.state_path().exists()
    assert not paths.runtime_config_path().exists()
    assert "wall_in_one.ui.app" not in sys.modules


def test_health_sync_requests_status_only_after_compiler_lock_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = False

    class ObservedLock:
        def __enter__(self) -> None:
            nonlocal entered
            entered = True

        def __exit__(
            self,
            _kind: type[BaseException] | None,
            _error: BaseException | None,
            _traceback: object,
        ) -> None:
            nonlocal entered
            entered = False

    def send(_verb: str) -> Response:
        assert entered
        raise client.NotRunningError("runtime absent")

    monkeypatch.setattr(runtime_config, "compiler_lock", ObservedLock)
    monkeypatch.setattr(client, "send_runtime", send)

    assert cli.main(["--sync-runtime-health"]) == client.EXIT_NOT_RUNNING
    assert not entered


def test_health_sync_persists_compiles_and_reloads_one_new_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _media(tmp_path)
    _write_runtime_config()
    calls: list[str] = []

    def send(verb: str) -> Response:
        calls.append(verb)
        return _snapshot(item) if verb == "status" else Response.success("reloaded")

    monkeypatch.setattr(client, "send_runtime", send)
    monkeypatch.delitem(sys.modules, "wall_in_one.ui.app", raising=False)

    assert cli.main(["--sync-runtime-health"]) == 0
    assert calls == ["status", "reload"]
    health = pairings.Store.open().health(pairings.Identity.of(item))
    assert health.is_borked
    assert health.reason == "renderer rejected this wallpaper"
    document = tomllib.loads(paths.runtime_config_path().read_text(encoding="utf-8"))
    entry = document["playlists"][0]["entries"][0]
    assert entry["taboo"] == {
        "reason": "renderer rejected this wallpaper",
        "source": "automatic-apply",
    }
    assert "wall_in_one.ui.app" not in sys.modules


def test_health_sync_recompiles_an_existing_marker_without_clearing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _media(tmp_path)
    pairings.Store.open().mark_borked(
        item,
        "renderer rejected this wallpaper",
        "automatic-apply",
    )
    _write_runtime_config()
    calls: list[str] = []
    real_update = runtime_config.update

    def update(*args: object, **kwargs: object) -> bool:
        calls.append("compile")
        return real_update(*args, **kwargs)  # type: ignore[arg-type]

    def send(verb: str) -> Response:
        calls.append(verb)
        return _snapshot(item, durable=True)

    monkeypatch.setattr(client, "send_runtime", send)
    monkeypatch.setattr(runtime_config, "update", update)

    assert cli.main(["--sync-runtime-health"]) == 0
    assert calls == ["status", "compile"]
    assert pairings.Store.open().health(pairings.Identity.of(item)).is_borked

    # A bounded snapshot with only omitted identities is not proof of recovery.
    monkeypatch.setattr(
        client,
        "send_runtime",
        lambda _verb: _snapshot(item, reports=False, omitted=3),
    )
    assert cli.main(["--sync-runtime-health"]) == 0
    assert pairings.Store.open().health(pairings.Identity.of(item)).is_borked


def test_explicit_clear_waits_for_inflight_sync_and_wins_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _media(tmp_path)
    pairings.Store.open().mark_borked(
        item,
        "renderer rejected this wallpaper",
        "automatic-apply",
    )
    _write_runtime_config()
    status_entered = threading.Event()
    release_status = threading.Event()
    clear_started = threading.Event()
    clear_acquired = threading.Event()

    def send(verb: str) -> Response:
        assert verb == "status"
        status_entered.set()
        assert release_status.wait(5), "test did not release the status response"
        return _snapshot(item, durable=True)

    def explicit_clear() -> int:
        clear_started.set()
        with runtime_config.compiler_lock():
            clear_acquired.set()
            assert pairings.Store.open().clear_borked(item)
            return cli.main(["--write-config"])

    monkeypatch.setattr(client, "send_runtime", send)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sync = pool.submit(cli.main, ["--sync-runtime-health"])
        assert status_entered.wait(5), "health sync did not acquire the compiler transaction"
        clear = pool.submit(explicit_clear)
        assert clear_started.wait(5)
        assert not clear_acquired.wait(0.05), "clear entered beside an in-flight health sync"
        release_status.set()
        assert sync.result(timeout=5) == 0
        assert clear.result(timeout=5) == 0

    assert not pairings.Store.open().health(pairings.Identity.of(item)).is_borked
    document = tomllib.loads(paths.runtime_config_path().read_text(encoding="utf-8"))
    assert "taboo" not in document["playlists"][0]["entries"][0]


@pytest.mark.parametrize(
    "message",
    (
        "not-json",
        "[]",
        json.dumps({"playlist": "All media", "source": "schedule", "taboo_entries": {}}),
        json.dumps(
            {
                "playlist": "All media",
                "source": "schedule",
                "taboo_entries": [{"playlist_id": "all-media"}],
            }
        ),
    ),
)
def test_malformed_status_fails_before_any_authoring_write(
    message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "send_runtime", lambda _verb: Response.success(message))

    assert cli.main(["--sync-runtime-health"]) == 1
    assert not pairings.state_path().exists()
    assert not paths.runtime_config_path().exists()


def test_compile_failure_preserves_last_known_good_runtime_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _media(tmp_path)
    target = paths.runtime_config_path()
    _write_runtime_config()
    previous = target.read_text(encoding="utf-8")
    calls: list[str] = []
    reloads = 0
    fail_next_compile = True
    real_update = runtime_config.update

    def send(verb: str) -> Response:
        nonlocal reloads
        calls.append(verb)
        if verb == "reload":
            reloads += 1
            return Response.success("reloaded")
        return _snapshot(item, durable=reloads > 0, config_epoch=reloads + 1)

    def update(*args: object, **kwargs: object) -> bool:
        nonlocal fail_next_compile
        if fail_next_compile:
            fail_next_compile = False
            raise runtime_config.RuntimeConfigError("injected compiler failure")
        return real_update(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(client, "send_runtime", send)
    monkeypatch.setattr(runtime_config, "update", update)

    assert cli.main(["--sync-runtime-health"]) == 1
    assert calls == ["status"]
    assert target.read_text(encoding="utf-8") == previous
    assert pairings.Store.open().health(pairings.Identity.of(item)).is_borked
    assert "health is saved" in capsys.readouterr().err

    # The next invocation sees that current authoring is newer than both the
    # runtime and installed document. It repairs that split state, but refuses
    # to consume the old snapshot because it could equally be a just-cleared
    # wallpaper. A later poll of the new generation is safe.
    assert cli.main(["--sync-runtime-health"]) == 1
    assert calls == ["status", "status", "reload"]
    assert runtime_config.read_config_generation() != runtime_config.document_generation(previous)
    assert "without consuming the stale health snapshot" in capsys.readouterr().err

    assert cli.main(["--sync-runtime-health"]) == 0
    assert calls == ["status", "status", "reload", "status"]


def test_mixed_display_status_is_valid_for_health_sync(tmp_path: Path) -> None:
    item = _media(tmp_path)
    generation = _write_runtime_config()
    response = _snapshot(item, generation=generation)
    payload = json.loads(response.message)
    payload["source"] = "mixed"

    parsed = runtime_health.parse_status(json.dumps(payload))

    assert parsed["source"] == "mixed"


def test_faulted_pairings_are_never_replaced_by_unattended_health_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _media(tmp_path)
    _write_runtime_config()
    target = pairings.state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    original = b"not json but still the user's authoring"
    target.write_bytes(original)
    monkeypatch.setattr(client, "send_runtime", lambda _verb: _snapshot(item))

    assert cli.main(["--sync-runtime-health"]) == 1
    assert target.read_bytes() == original
    assert not target.with_name(target.name + pairings.BROKEN_SUFFIX).exists()
    assert "authoring state is unreadable" in capsys.readouterr().err


def test_stale_snapshot_cannot_undo_a_user_clearing_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _media(tmp_path)
    store = pairings.Store.open()
    store.mark_borked(item, "renderer rejected this wallpaper", "automatic-apply")
    stale_generation = _write_runtime_config()
    assert store.clear_borked(item)
    calls: list[str] = []
    reloaded = False

    def send(verb: str) -> Response:
        nonlocal reloaded
        calls.append(verb)
        if verb == "reload":
            reloaded = True
            return Response.success("reloaded")
        return _snapshot(
            item,
            generation=(runtime_config.read_config_generation() if reloaded else stale_generation),
            config_epoch=2 if reloaded else 1,
            observed_config_epoch=1,
            durable=False,
        )

    monkeypatch.setattr(client, "send_runtime", send)

    assert cli.main(["--sync-runtime-health"]) == 1
    assert calls == ["status", "reload"]
    assert not pairings.Store.open().health(pairings.Identity.of(item)).is_borked
    document = tomllib.loads(paths.runtime_config_path().read_text(encoding="utf-8"))
    assert "taboo" not in document["playlists"][0]["entries"][0]
    assert "without consuming the stale health snapshot" in capsys.readouterr().err

    # Rust may retain a non-durable session finding across that reload. Its
    # row keeps the generation under which it was actually observed, so a new
    # status envelope cannot launder the old failure into current authoring.
    assert cli.main(["--sync-runtime-health"]) == 1
    assert calls == ["status", "reload", "status"]
    assert not pairings.Store.open().health(pairings.Identity.of(item)).is_borked
    assert "skipped 1 stale-generation runtime report" in capsys.readouterr().err


def test_unmapped_runtime_reports_fail_explicitly_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _media(tmp_path)
    _write_runtime_config()
    missing = {
        "playlist_id": runtime_config.FALLBACK_PLAYLIST_ID,
        "entry_id": "entry-that-does-not-exist",
        "reason": "old renderer failure",
        "source": "automatic-apply",
        "durable": False,
    }
    monkeypatch.setattr(
        client,
        "send_runtime",
        lambda _verb: _snapshot(item, reports=False, extra_reports=(missing,)),
    )

    assert cli.main(["--sync-runtime-health"]) == 1
    assert not pairings.state_path().exists()
    assert "1 runtime report which no longer map" in capsys.readouterr().err


@pytest.mark.parametrize("mismatch", ("path", "installed-generation"))
def test_health_from_a_different_runtime_document_is_never_mapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mismatch: str,
) -> None:
    item = _media(tmp_path)
    installed = _write_runtime_config()
    response = _snapshot(item, generation=installed)
    payload = json.loads(response.message)
    if mismatch == "path":
        payload["config_path"] = str((tmp_path / "hand-written-runtime.toml").absolute())
    else:
        payload["config_generation"] = "0" * runtime_config.CONFIG_GENERATION_HEX_CHARS
    monkeypatch.setattr(
        client,
        "send_runtime",
        lambda _verb: Response.success(json.dumps(payload)),
    )

    assert cli.main(["--sync-runtime-health"]) == 1
    assert not pairings.state_path().exists()
    assert "no health marker was written" in capsys.readouterr().err


def test_partial_mapping_is_saved_but_returns_a_diagnostic_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _media(tmp_path)
    _write_runtime_config()
    missing = {
        "playlist_id": runtime_config.FALLBACK_PLAYLIST_ID,
        "entry_id": "entry-that-does-not-exist",
        "reason": "old renderer failure",
        "source": "automatic-apply",
        "durable": False,
    }
    calls: list[str] = []

    def send(verb: str) -> Response:
        calls.append(verb)
        return (
            _snapshot(item, extra_reports=(missing,))
            if verb == "status"
            else Response.success("reloaded")
        )

    monkeypatch.setattr(client, "send_runtime", send)

    assert cli.main(["--sync-runtime-health"]) == 1
    assert calls == ["status", "reload"]
    assert pairings.Store.open().health(pairings.Identity.of(item)).is_borked
    assert "saved 1 current wallpaper health report" in capsys.readouterr().err


def test_one_status_snapshot_causes_one_pairings_store_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _media(tmp_path)
    second_path = first.path.with_name("second.png")
    second_path.write_bytes(b"second")
    second = MediaItem(
        path=second_path,
        kind=Kind.STILL,
        size=second_path.stat().st_size,
        mtime=int(second_path.stat().st_mtime),
    )
    _write_runtime_config()
    extra = {
        "playlist_id": runtime_config.FALLBACK_PLAYLIST_ID,
        "entry_id": runtime_config.entry_id_for_source(second.path),
        "reason": "second renderer failure",
        "source": "automatic-apply",
        "durable": False,
    }
    writes = 0
    real_save = pairings.save

    def save(records: object, path: Path | None = None) -> Path:
        nonlocal writes
        writes += 1
        return real_save(records, path)  # type: ignore[arg-type]

    def send(verb: str) -> Response:
        return (
            _snapshot(first, extra_reports=(extra,))
            if verb == "status"
            else Response.success("reloaded")
        )

    monkeypatch.setattr(pairings, "save", save)
    monkeypatch.setattr(client, "send_runtime", send)

    assert cli.main(["--sync-runtime-health"]) == 0
    assert writes == 1
    reopened = pairings.Store.open()
    assert reopened.health(pairings.Identity.of(first)).is_borked
    assert reopened.health(pairings.Identity.of(second)).is_borked


def test_reload_rejection_reports_that_health_was_already_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _media(tmp_path)
    _write_runtime_config()

    def send(verb: str) -> Response:
        return _snapshot(item) if verb == "status" else Response.failure("bad config")

    monkeypatch.setattr(client, "send_runtime", send)

    assert cli.main(["--sync-runtime-health"]) == 1
    assert pairings.Store.open().health(pairings.Identity.of(item)).is_borked
    assert "health was saved but reload was rejected" in capsys.readouterr().err
