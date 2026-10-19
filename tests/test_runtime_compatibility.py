"""A new writer must not publish schema 5 under an older live service."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from wall_in_one import (
    __version__,
    config,
    paths,
    predecessor_process,
    runtime_compatibility,
    runtime_config,
)
from wall_in_one.control import client
from wall_in_one.control.protocol import Request, Response
from wall_in_one.session import Session

#: What a 0.3 service still running after the update says about a 0.4 document.
OLD_SERVICE_REFUSAL = "config is not valid TOML: missing field `niri_program`"
#: 0.3.2's own reply to a 0.4 document, verbatim, trailing newline included.
REAL_REFUSAL = (
    "config is not valid TOML: TOML parse error at line 11, column 1\n   |\n11 | [renderer]\n"
    "   | ^^^^^^^^^^\nmissing field `niri_program`\n"
)


def _reply(monkeypatch: pytest.MonkeyPatch, status: object) -> None:
    def send(verb: str, *, timeout: float) -> Response:
        assert verb == "status"
        assert timeout == client.TIMEOUT
        return Response(ok=True, message=json.dumps(status))

    monkeypatch.setattr(client, "send_runtime", send)


@pytest.mark.parametrize(
    "status",
    [
        {"status_version": 2},  # Actual released v0.1.2 status contract.
        {"status_version": 2, "supported_config_schemas": [4]},
        {"status_version": 2, "supported_config_schemas": ["5"]},
        {"status_version": 2, "supported_config_schemas": [True, 5]},
        {"status_version": True, "supported_config_schemas": [4, 5]},
        {"status_version": 3, "supported_config_schemas": [4, 5]},
        {"status_version": 2, "supported_config_schemas": "5"},
        {"status_version": 2, "supported_config_schemas": [5] * 17},
        [],
        None,
    ],
)
def test_incompatible_or_ambiguous_runtime_leaves_settings_exactly_unchanged(
    monkeypatch: pytest.MonkeyPatch, status: object
) -> None:
    path = config.save(config.Settings(scan_workshop=False))
    original = path.read_bytes()
    _reply(monkeypatch, status)
    with pytest.raises(config.ConfigError, match="No changes were saved"):
        config.update({"stop_animations_on_battery": True})
    assert path.read_bytes() == original
    assert not config.load_strict().stop_animations_on_battery


def test_compatible_service_allows_enabling_and_disabling(monkeypatch: pytest.MonkeyPatch) -> None:
    config.save(config.Settings())
    _reply(monkeypatch, {"status_version": 2, "supported_config_schemas": [4, 5]})
    assert config.update({"stop_animations_on_battery": True}).stop_animations_on_battery
    _reply(monkeypatch, {"status_version": 2})
    assert not config.update({"stop_animations_on_battery": False}).stop_animations_on_battery
    assert "stop_animations_on_battery" not in paths.settings_path().read_text()


def test_absent_runtime_allows_offline_preparation_without_starting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def absent(*_args: object, **_kwargs: object) -> Response:
        raise client.NotRunningError("no listener")

    monkeypatch.setattr(client, "send_runtime", absent)
    config.save(config.Settings(stop_animations_on_battery=True))
    assert config.load_strict().stop_animations_on_battery


def test_missing_socket_does_not_hide_a_starting_or_unlinked_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = config.save(config.Settings())
    before = target.read_bytes()

    def absent(*_args: object, **_kwargs: object) -> Response:
        raise client.NotRunningError("no listener")

    def still_live() -> None:
        raise predecessor_process.PredecessorProcessError("predecessor still targets this config")

    monkeypatch.setattr(client, "send_runtime", absent)
    monkeypatch.setattr(predecessor_process, "refuse_live_predecessor_runtime", still_live)
    with pytest.raises(config.ConfigError, match="still be starting or running"):
        config.update({"stop_animations_on_battery": True})
    assert target.read_bytes() == before


@pytest.mark.parametrize("failure", ["timed out", "cannot connect", "invalid response"])
def test_uncertain_runtime_is_not_treated_as_absent(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = config.save(config.Settings())
    original = path.read_bytes()

    def uncertain(*_args: object, **_kwargs: object) -> Response:
        raise client.ControlError(failure)

    monkeypatch.setattr(client, "send_runtime", uncertain)
    with pytest.raises(config.ConfigError, match="Cannot confirm"):
        config.update({"stop_animations_on_battery": True})
    assert path.read_bytes() == original


@pytest.mark.parametrize("reply", [Response(True, "{"), Response(False, "busy")])
def test_malformed_and_failed_status_refuse_publication(
    monkeypatch: pytest.MonkeyPatch, reply: Response
) -> None:
    monkeypatch.setattr(client, "send_runtime", lambda *_args, **_kwargs: reply)
    with pytest.raises(runtime_compatibility.RuntimeCompatibilityError):
        runtime_compatibility.require_schema(5)


def test_detached_settings_export_does_not_probe_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> Response:
        pytest.fail("detached export consulted the session runtime")

    monkeypatch.setattr(client, "send_runtime", unexpected)
    target = tmp_path / "export.toml"
    config.save(config.Settings(stop_animations_on_battery=True), target)
    assert config.load_strict(target).stop_animations_on_battery


@pytest.mark.parametrize("writer", [runtime_config.write, runtime_config.update])
def test_compiler_refuses_schema_5_and_preserves_last_known_good(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writer: object
) -> None:
    root = tmp_path / "Original Library"
    root.mkdir()
    (root / "wallpaper.png").write_bytes(b"fixture")
    settings = config.Settings(roots=(root,), scan_workshop=False)
    config.save(settings)
    session = Session(settings)
    target = runtime_config.write(settings, session)
    before = target.read_bytes()
    _reply(monkeypatch, {"status_version": 2})
    assert callable(writer)
    with pytest.raises(runtime_config.RuntimeConfigError, match="No changes were saved"):
        writer(replace(settings, stop_animations_on_battery=True), session)
    assert target.read_bytes() == before
    assert runtime_config.update(settings, session) is False


def test_low_level_default_settings_save_is_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    target = config.save(config.Settings())
    before = target.read_bytes()
    _reply(monkeypatch, {"status_version": 2})
    with pytest.raises(config.ConfigError, match="No changes were saved"):
        config.save(config.Settings(stop_animations_on_battery=True), paths.settings_path())
    assert target.read_bytes() == before


# -- a document refused by the service still running from before the update ----


@pytest.mark.parametrize(
    ("running", "said"),
    [
        ("0.0.1", True),
        (__version__, False),
        ("99.0.0", False),
        ("0.4.0-rc1", False),
        (7, False),
        (None, False),
    ],
)
def test_a_refusal_says_to_restart_only_a_service_older_than_the_app(
    running: object, said: bool
) -> None:
    status: dict[str, object] = {"status_version": 2}
    if running is not None:
        status["runtime_version"] = running

    hint = runtime_compatibility.restart_hint(status)

    assert ("Restart the wallpaper service to finish updating" in hint) is said
    assert runtime_compatibility.restart_hint(None) == ""


def test_ctl_reload_refused_by_an_older_service_says_to_restart_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asked: list[str] = []

    def send(request: Request, **_keywords: object) -> Response:
        asked.append(request.verb)
        if request.verb == "reload":
            return Response.failure(OLD_SERVICE_REFUSAL)
        return Response.success(json.dumps({"status_version": 2, "runtime_version": "0.0.1"}))

    monkeypatch.setattr(client, "send", send)

    assert client.dispatch("reload", None) == 1
    error = capsys.readouterr().err
    assert OLD_SERVICE_REFUSAL in error
    assert "Restart the wallpaper service to finish updating" in error
    assert asked == ["reload", "status"]


def test_ctl_reload_refused_by_a_current_service_says_only_why(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def send(request: Request, **_keywords: object) -> Response:
        if request.verb == "reload":
            return Response.failure("invalid config: scene_fps must be between 1 and 240")
        return Response.success(json.dumps({"status_version": 2, "runtime_version": __version__}))

    monkeypatch.setattr(client, "send", send)

    assert client.dispatch("reload", None) == 1
    assert "Restart" not in capsys.readouterr().err


def test_the_hint_follows_the_real_refusal_on_its_last_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def send(request: Request, **_keywords: object) -> Response:
        if request.verb == "reload":
            return Response.failure(REAL_REFUSAL)
        return Response.success(json.dumps({"status_version": 2, "runtime_version": "0.0.1"}))

    monkeypatch.setattr(client, "send", send)

    assert client.dispatch("reload", None) == 1
    error = capsys.readouterr().err
    assert "missing field `niri_program` Restart the wallpaper service" in error
    assert "\n Restart" not in error


def test_a_refusal_without_a_hint_is_left_as_it_came() -> None:
    status = {"status_version": 2, "runtime_version": __version__}
    assert runtime_compatibility.with_restart_hint(REAL_REFUSAL, status) == REAL_REFUSAL


def test_ctl_reload_says_only_why_when_the_status_does_not_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    asked: list[tuple[str, object]] = []

    def send(request: Request, **keywords: object) -> Response:
        asked.append((request.verb, keywords.get("timeout")))
        if request.verb == "reload":
            return Response.failure(REAL_REFUSAL)
        raise client.ControlTimeoutError("timed out after 5s")

    monkeypatch.setattr(client, "send", send)

    assert client.dispatch("reload", None) == 1
    error = capsys.readouterr().err
    assert "missing field `niri_program`" in error
    assert "Restart" not in error
    assert asked == [("reload", None), ("status", client.TIMEOUT)]
