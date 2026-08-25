from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import time
import types
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from wall_in_one import config, file_io, paths
from wall_in_one.control.protocol import (
    MAX_MESSAGE_BYTES,
    ProtocolError,
    Request,
    Response,
)
from wall_in_one.control.server import (
    Deferred,
    Outcome,
    Reply,
    UnknownWallpaperError,
    build_verb_table,
    dispatch,
    failed,
    handle,
    parse_download,
    parse_list,
    parse_pair,
    parse_pair_from_left,
    parse_path,
    parse_rule,
    parse_search,
    parse_toggle,
    remove_wallpaper,
    render_favourites,
    render_library,
    render_providers,
    render_search,
    resolve,
)
from wall_in_one.library import favourites, manage, pairing, pairings, removals
from wall_in_one.library.filter import Kinds, Query
from wall_in_one.library.manage import ManageError
from wall_in_one.library.model import Kind, Library, MediaItem, Ownership
from wall_in_one.library.playlists import PlaylistError
from wall_in_one.providers.base import ProviderError, SearchResult, WallpaperCandidate
from wall_in_one.providers.registry import ProviderInfo
from wall_in_one.session import Session
from wall_in_one.wallpaper.applier import Applied, Applier
from wall_in_one.wallpaper.outputs import Output

if TYPE_CHECKING:
    from wall_in_one.ui.app import Application, _Commands


def test_request_round_trip() -> None:
    for request in (Request("next"), Request("cycle-interval", "600")):
        assert Request.decode(request.encode()) == request


def test_response_round_trip() -> None:
    for response in (Response.success("ok"), Response.failure("nope")):
        assert Response.decode(response.encode()) == response


def test_encoding_is_one_line() -> None:
    encoded = Request("shuffle", "on\nnot-a-second-message").encode()
    assert encoded.count(b"\n") == 1
    assert encoded.endswith(b"\n")


@pytest.mark.parametrize("line", [b"not json", b"[]", b'{"argument":"x"}', b'{"verb":""}'])
def test_request_rejects_malformed(line: bytes) -> None:
    with pytest.raises(ProtocolError):
        Request.decode(line)


def test_oversized_message_is_refused() -> None:
    with pytest.raises(ProtocolError):
        Request.decode(b"x" * (MAX_MESSAGE_BYTES + 1))


def test_deeply_nested_bounded_request_is_a_protocol_error() -> None:
    line = b'{"verb":"next","padding":' + (b"[" * 2_000) + b"0" + (b"]" * 2_000) + b"}"
    assert len(line) < MAX_MESSAGE_BYTES
    with pytest.raises(ProtocolError, match="nesting exceeds"):
        Request.decode(line)


def test_json_parser_recursion_is_wrapped_as_a_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def recurse(_line: str) -> object:
        raise RecursionError("forced parser recursion")

    monkeypatch.setattr(json, "loads", recurse)
    with pytest.raises(ProtocolError, match="cannot decode message"):
        Response.decode(b'{"ok":true}')


@pytest.mark.parametrize(
    ("value", "current", "expected"),
    [
        ("on", False, True),
        ("off", True, False),
        ("toggle", False, True),
        ("toggle", True, False),
        (None, False, True),
        ("TRUE", False, True),
        ("disabled", True, False),
    ],
)
def test_parse_toggle(value: str | None, current: bool, expected: bool) -> None:
    assert parse_toggle(value, current) is expected


def test_parse_toggle_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="expected on, off or toggle"):
        parse_toggle("sideways", False)


class _StubCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def _record(self, verb: str, argument: str | None = None) -> Response:
        self.calls.append((verb, argument))
        return Response.success(verb)

    def next_wallpaper(self) -> Response:
        return self._record("next")

    def previous_wallpaper(self) -> Response:
        return self._record("prev")

    def random_wallpaper(self) -> Response:
        return self._record("random")

    def set_shuffle(self, value: str | None) -> Response:
        return self._record("shuffle", value)

    def set_cycle(self, value: str | None) -> Response:
        return self._record("cycle", value)

    def set_cycle_interval(self, value: str | None) -> Response:
        return self._record("cycle-interval", value)

    def set_dynamics(self, value: str | None) -> Response:
        return self._record("dynamics", value)

    def reload_palette(self) -> Response:
        return self._record("reload-palette")

    def open_page(self, value: str | None) -> Response:
        return self._record("open", value)

    def report_status(self) -> Response:
        return self._record("status")

    def list_library(self, value: str | None) -> Response:
        return self._record("list", value)

    def select_wallpaper(self, value: str | None) -> Response:
        return self._record("select", value)

    def list_favourites(self) -> Response:
        return self._record("favourites")

    def add_favourite(self, value: str | None) -> Response:
        return self._record("favourite", value)

    def remove_favourite(self, value: str | None) -> Response:
        return self._record("unfavourite", value)

    def remove_wallpaper(self, value: str | None) -> Response:
        return self._record("remove", value)

    def show_pairing(self, value: str | None) -> Response:
        return self._record("pairing", value)

    def set_still(self, value: str | None) -> Response:
        return self._record("still", value)

    def set_palette(self, value: str | None) -> Response:
        return self._record("palette", value)

    def reset_pairing(self, value: str | None) -> Response:
        return self._record("reset-pairing", value)

    def list_playlists(self, value: str | None) -> Response:
        return self._record("playlists", value)

    def make_playlist(self, value: str | None) -> Response:
        return self._record("playlist-new", value)

    def drop_playlist(self, value: str | None) -> Response:
        return self._record("playlist-delete", value)

    def add_to_playlist(self, value: str | None) -> Response:
        return self._record("playlist-add", value)

    def remove_from_playlist(self, value: str | None) -> Response:
        return self._record("playlist-remove", value)

    def use_playlist(self, value: str | None) -> Response:
        return self._record("playlist-use", value)

    def list_displays(self) -> Response:
        return self._record("displays")

    def assign_display(self, value: str | None) -> Response:
        return self._record("display-assign", value)

    def clear_display(self, value: str | None) -> Response:
        return self._record("display-clear", value)

    def show_schedule(self) -> Response:
        return self._record("schedule")

    def add_schedule_rule(self, value: str | None) -> Response:
        return self._record("schedule-add", value)

    def drop_schedule_rule(self, value: str | None) -> Response:
        return self._record("schedule-remove", value)

    def list_providers(self) -> Response:
        return self._record("providers")

    def search(self, value: str | None) -> Outcome:
        return self._record("search", value)

    def download(self, value: str | None) -> Outcome:
        return self._record("download", value)

    def quit(self) -> Response:
        return self._record("quit")


def test_verb_table_covers_the_documented_cli_surface() -> None:
    from wall_in_one.cli import CTL_VERBS, RUNTIME_ONLY_VERBS

    verbs = build_verb_table(_StubCommands())
    assert set(verbs) | set(RUNTIME_ONLY_VERBS) == set(CTL_VERBS)


def test_service_mode_reaches_the_windowless_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI flag must survive the lazy GTK import unchanged."""
    from wall_in_one import cli

    calls: list[tuple[bool, str | None]] = []
    fake = types.ModuleType("wall_in_one.ui.app")

    def run(
        _argv: list[str] | None = None,
        *,
        service: bool = False,
        initial_page: str | None = None,
    ) -> int:
        calls.append((service, initial_page))
        return 17

    fake.run = run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wall_in_one.ui.app", fake)

    assert cli.main(["--service"]) == 17
    assert calls == [(True, None)]


def test_open_page_reaches_the_gui_application(monkeypatch: pytest.MonkeyPatch) -> None:
    from wall_in_one import cli

    calls: list[tuple[bool, str | None]] = []
    fake = types.ModuleType("wall_in_one.ui.app")

    def run(
        _argv: list[str] | None = None,
        *,
        service: bool = False,
        initial_page: str | None = None,
    ) -> int:
        calls.append((service, initial_page))
        return 0

    fake.run = run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wall_in_one.ui.app", fake)

    assert cli.main(["--open-page", "schedules"]) == 0
    assert calls == [(False, "schedules")]


def test_open_launches_the_gui_when_only_the_runtime_exists(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    from wall_in_one.control import client

    launched: list[tuple[list[str], dict[str, object]]] = []

    def absent(*_args: object, **_kwargs: object) -> Response:
        raise client.NotRunningError("authoring app is closed")

    def launch(arguments: list[str], **kwargs: object) -> object:
        launched.append((arguments, kwargs))
        return object()

    monkeypatch.setattr(client, "send", absent)
    monkeypatch.setattr(subprocess, "Popen", launch)
    launcher = tmp_path / "wall-in-one"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(launcher)])

    assert client.dispatch("open", "playlists") == 0
    assert capsys.readouterr().out == "launch requested for playlists\n"
    assert launched[0][0] == [str(launcher), "--open-page", "playlists"]
    assert launched[0][1]["start_new_session"] is True


def test_open_from_a_module_launch_uses_the_current_interpreter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wall_in_one.control import client

    launched: list[list[str]] = []

    def absent(*_args: object, **_kwargs: object) -> Response:
        raise client.NotRunningError("authoring app is closed")

    monkeypatch.setattr(client, "send", absent)
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda arguments, **_kwargs: launched.append(arguments),
    )
    monkeypatch.setattr(sys, "argv", ["/source/wall_in_one/__main__.py"])

    assert client.dispatch("open", "settings") == 0
    assert launched == [[sys.executable, "-m", "wall_in_one", "--open-page", "settings"]]


def test_open_refuses_an_unknown_page_before_launching(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wall_in_one.control import client

    def absent(*_args: object, **_kwargs: object) -> Response:
        raise client.NotRunningError("authoring app is closed")

    monkeypatch.setattr(client, "send", absent)
    launched: list[object] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: launched.append(object()))

    assert client.dispatch("open", "not-a-page") == 1
    assert "usage: open" in capsys.readouterr().err
    assert launched == []


@pytest.mark.parametrize(
    ("verb", "legacy_request"),
    [
        ("previous", Request("prev")),
        ("schedule-follow", Request("playlist-use", "none")),
    ],
)
def test_runtime_aliases_reach_the_legacy_service_when_rust_is_absent(
    verb: str,
    legacy_request: Request,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wall_in_one import paths
    from wall_in_one.control import client

    seen: list[tuple[Request, Path | None]] = []

    def answer(request: Request, *, path: Path | None = None, **_kwargs: object) -> Response:
        seen.append((request, path))
        if path == paths.runtime_socket_path():
            raise client.NotRunningError("runtime is closed")
        return Response.success("ok")

    monkeypatch.setattr(client, "send", answer)

    assert client.dispatch(verb, None) == 0
    assert seen == [
        (Request(verb), paths.runtime_socket_path()),
        (legacy_request, None),
    ]


def test_status_does_not_accept_a_plain_gui_as_the_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from wall_in_one import paths
    from wall_in_one.control import client

    def answer(request: Request, *, path: Path | None = None, **_kwargs: object) -> Response:
        if path == paths.runtime_socket_path():
            raise client.NotRunningError("runtime is closed")
        assert request.verb == "status"
        return Response.failure(
            "the GUI is open, but the wallpaper runtime is not running",
            kind="runtime-not-running",
        )

    monkeypatch.setattr(client, "send", answer)

    assert client.dispatch("status", None) == client.EXIT_NOT_RUNNING
    assert "runtime is not running" in capsys.readouterr().err


@pytest.mark.parametrize(("verb", "argument"), [("cycle", "off"), ("stop", None)])
def test_live_cycle_and_stop_are_sent_to_the_runtime_socket(
    verb: str,
    argument: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wall_in_one import paths
    from wall_in_one.control import client

    seen: list[tuple[Request, Path | None]] = []

    def answer(request: Request, *, path: Path | None = None, **_kwargs: object) -> Response:
        seen.append((request, path))
        return Response.success("ok")

    monkeypatch.setattr(client, "send", answer)

    assert client.dispatch(verb, argument) == 0
    assert seen == [(Request(verb, argument), paths.runtime_socket_path())]


def test_handle_dispatches_and_passes_the_argument() -> None:
    commands = _StubCommands()
    verbs = build_verb_table(commands)
    response = handle(verbs, Request("cycle-interval", "600").encode())
    # Everything but the browsing verbs answers on the spot, which is what makes
    # it safe for them to run on the GTK main thread at all.
    assert isinstance(response, Response)
    assert response.ok
    assert commands.calls == [("cycle-interval", "600")]


def test_handle_reports_an_unknown_verb_without_raising() -> None:
    response = handle(build_verb_table(_StubCommands()), Request("fly").encode())
    assert isinstance(response, Response)
    assert not response.ok
    assert "unknown verb" in response.message


def test_handle_survives_a_handler_that_raises() -> None:
    class Exploding(_StubCommands):
        def next_wallpaper(self) -> Response:
            raise RuntimeError("disk on fire")

    response = handle(build_verb_table(Exploding()), Request("next").encode())
    assert isinstance(response, Response)
    assert not response.ok
    assert "disk on fire" in response.message


def test_handle_reports_malformed_input_without_raising() -> None:
    response = handle(build_verb_table(_StubCommands()), b"{{{")
    assert isinstance(response, Response)
    assert not response.ok


# -- answers that arrive later -------------------------------------------


def _collect() -> tuple[list[Response], Reply]:
    replies: list[Response] = []
    return replies, replies.append


def test_an_immediate_verb_answers_before_dispatch_returns() -> None:
    replies, reply = _collect()
    dispatch(build_verb_table(_StubCommands()), Request("next").encode(), reply)
    assert [response.message for response in replies] == ["next"]


def test_a_deferred_verb_answers_when_its_work_finishes() -> None:
    """The whole point: `search` returns without an answer and supplies one later."""
    pending: list[Reply] = []

    class Slow(_StubCommands):
        def search(self, value: str | None) -> Outcome:
            return Deferred(start=pending.append)

    replies, reply = _collect()
    dispatch(build_verb_table(Slow()), Request("search", "wallhaven sky").encode(), reply)

    assert replies == []
    pending[0](Response.success("found things"))
    assert [response.message for response in replies] == ["found things"]


def test_a_deferred_verb_cannot_answer_twice() -> None:
    """Two responses down a connection framed one per line would desynchronise it."""
    pending: list[Reply] = []

    class Twice(_StubCommands):
        def search(self, value: str | None) -> Outcome:
            return Deferred(start=pending.append)

    replies, reply = _collect()
    dispatch(build_verb_table(Twice()), Request("search", "wallhaven sky").encode(), reply)
    pending[0](Response.success("first"))
    pending[0](Response.success("second"))

    assert [response.message for response in replies] == ["first"]


def test_work_that_will_not_even_start_still_answers() -> None:
    class Broken(_StubCommands):
        def download(self, value: str | None) -> Outcome:
            def start(_reply: Reply) -> None:
                raise ProviderError("no-root", "nowhere to download to")

            return Deferred(start=start)

    replies, reply = _collect()
    dispatch(build_verb_table(Broken()), Request("download", "wallhaven ab1234").encode(), reply)

    assert [(r.ok, r.kind) for r in replies] == [(False, "no-root")]


# -- provider failures ----------------------------------------------------


def test_a_provider_error_travels_as_a_failure_with_its_kind() -> None:
    response = failed(ProviderError("rate-limit", "Wallhaven asked us to slow down"))
    assert not response.ok
    assert response.kind == "rate-limit"
    # The message reads as the browse dialog's toast does, so one print says both.
    assert response.message == "rate-limit: Wallhaven asked us to slow down"


def test_an_unreachable_network_is_a_failure_and_not_a_traceback() -> None:
    response = failed(OSError("Network is unreachable"))
    assert not response.ok
    assert response.message == "OSError: Network is unreachable"
    assert response.kind == ""


def test_a_handler_that_raises_a_provider_error_keeps_the_kind() -> None:
    class Refusing(_StubCommands):
        def search(self, value: str | None) -> Outcome:
            raise ProviderError("unknown-provider", "no such provider: 'wallheaven'")

    replies, reply = _collect()
    dispatch(build_verb_table(Refusing()), Request("search", "wallheaven sky").encode(), reply)

    assert replies[0].kind == "unknown-provider"


def test_a_kind_survives_the_wire() -> None:
    response = Response.failure("credential: that key was refused", kind="credential")
    assert Response.decode(response.encode()) == response


def test_a_response_without_a_kind_carries_no_such_field() -> None:
    """An older client parses exactly the two fields it always parsed."""
    assert b"kind" not in Response.success("ok").encode()


@pytest.mark.parametrize("line", [b'{"ok":true,"kind":7}', b'{"ok":true}'])
def test_a_missing_or_nonsense_kind_decodes_as_absent(line: bytes) -> None:
    assert Response.decode(line).kind == ""


# -- argument parsing -----------------------------------------------------


def test_a_search_query_keeps_its_spaces() -> None:
    assert parse_search("wallhaven aurora over the fjord") == ("wallhaven", "aurora over the fjord")


def test_a_search_with_no_query_is_allowed() -> None:
    """Both providers answer an empty query with whatever they are showing today."""
    assert parse_search("motionbgs") == ("motionbgs", "")


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_search_with_no_provider_is_a_usage_error(value: str | None) -> None:
    with pytest.raises(ValueError, match="usage: search"):
        parse_search(value)


def test_a_download_takes_a_provider_an_identifier_and_an_optional_variant() -> None:
    assert parse_download("motionbgs some-slug 4k") == ("motionbgs", "some-slug", "4k")
    assert parse_download("wallhaven ab1234") == ("wallhaven", "ab1234", "")


@pytest.mark.parametrize("value", [None, "wallhaven", "wallhaven ab1234 4k extra"])
def test_a_malformed_download_is_a_usage_error(value: str | None) -> None:
    with pytest.raises(ValueError, match="usage: download"):
        parse_download(value)


# -- what a terminal sees -------------------------------------------------


def test_providers_are_listed_with_their_limitations() -> None:
    text = render_providers(
        (
            ProviderInfo(name="motionbgs", title="MotionBGS", media_kind=Kind.VIDEO, usable=True),
            ProviderInfo(
                name="wallhaven",
                title="Wallhaven",
                media_kind=Kind.STILL,
                usable=True,
                limitations=("NSFW results need a Wallhaven API key",),
            ),
        )
    )
    header, motionbgs, wallhaven = text.splitlines()

    assert header.startswith("# fields:")
    assert motionbgs.split("\t") == ["motionbgs", "video", "yes", "-"]
    assert wallhaven.split("\t")[3] == "NSFW results need a Wallhaven API key"


def _result(
    *items: WallpaperCandidate,
    provider: str = "wallhaven",
    page: int = 2,
    total_hint: int = 1130,
    dropped: int = 0,
    cached: bool = False,
) -> SearchResult:
    return SearchResult(
        provider=provider,
        query_url="https://wallhaven.cc/api/v1/search",
        items=items,
        page=page,
        total_hint=total_hint,
        dropped=dropped,
        cached=cached,
    )


def _candidate(title: str = "Aurora", identifier: str = "ab1234") -> WallpaperCandidate:
    return WallpaperCandidate(
        provider="wallhaven",
        identifier=identifier,
        title=title,
        kind=Kind.STILL,
        page_url=f"https://wallhaven.cc/w/{identifier}",
        resolution="1920x1080",
    )


def test_a_page_of_results_is_two_comments_and_a_row_each() -> None:
    lines = render_search(_result(_candidate(), _candidate(identifier="cd5678"))).splitlines()

    assert lines[0] == "# wallhaven: 2 results - of about 1130 - page 2"
    assert lines[1] == "# fields: identifier, kind, resolution, title"
    assert lines[2].split("\t") == ["ab1234", "still", "1920x1080", "Aurora"]
    assert len(lines) == 4


def test_a_title_can_never_invent_a_column_or_a_row() -> None:
    """The title is a website's string; tabs and newlines in it are structure."""
    line = render_search(_result(_candidate(title="one\ttwo\nthree"))).splitlines()[2]
    assert line.split("\t") == ["ab1234", "still", "1920x1080", "one two three"]


def test_a_result_with_nothing_to_say_still_fills_its_columns() -> None:
    empty = WallpaperCandidate(
        provider="motionbgs",
        identifier="a-slug",
        title="",
        kind=Kind.VIDEO,
        page_url="",
    )
    line = render_search(_result(empty, provider="motionbgs")).splitlines()[2]
    # An identifier stands in for a missing title, exactly as the dialog's cards do.
    assert line.split("\t") == ["a-slug", "video", "-", "a-slug"]


def test_an_empty_page_is_the_summary_and_nothing_else() -> None:
    lines = render_search(_result(page=1, total_hint=0)).splitlines()
    assert lines[0] == "# wallhaven: 0 results - page 1"
    assert len(lines) == 2


def test_the_summary_reports_what_the_dialog_reports() -> None:
    """`unreadable` means the remote's schema moved, which a script should see too."""
    lines = render_search(_result(_candidate(), dropped=3, cached=True)).splitlines()
    assert lines[0] == "# wallhaven: 1 result - of about 1130 - page 2 - 3 unreadable - cached"


# -- the client's patience ------------------------------------------------


def test_the_slow_verbs_are_given_longer_than_the_others() -> None:
    """Every authoring verb deliberately owns one audited deadline."""
    from wall_in_one.control import client

    assert client.TIMEOUTS["search"] > client.TIMEOUT
    assert client.TIMEOUTS["download"] > client.TIMEOUTS["search"]
    assert set(client.TIMEOUTS) == set(build_verb_table(_StubCommands()))


def test_authoring_deadlines_cover_their_bounded_inner_work() -> None:
    """The client must not expire at an inner lock/helper's own boundary."""
    from wall_in_one.control import client
    from wall_in_one.library import state_file
    from wall_in_one.theme import noctalia
    from wall_in_one.wallpaper import outputs

    assert client.AUTHORING_TIMEOUT > state_file.MUTATION_LOCK_TIMEOUT_SECONDS
    assert client.DISPLAY_DISCOVERY_TIMEOUT > outputs.QUERY_TIMEOUT
    assert client.CASCADE_TIMEOUT > client.AUTHORING_TIMEOUT
    assert client.REMOVAL_TIMEOUT > client.CASCADE_TIMEOUT
    assert client.COMPOSED_RUNTIME_TIMEOUT >= (
        state_file.MUTATION_LOCK_TIMEOUT_SECONDS
        + 5  # runtime compiler lock
        + (2 * client.RUNTIME_ACTION_TIMEOUT)
    )
    one_palette_resolution = (4 * noctalia.MESSAGE_TIMEOUT) + noctalia.GENERATE_TIMEOUT
    assert (
        (2 * one_palette_resolution) + (2 * noctalia.MESSAGE_TIMEOUT)
    ) < client.PALETTE_RELOAD_TIMEOUT


class _ClientSocket:
    """Socket-shaped deadline probe; no wall-clock sleeps or real I/O."""

    def __init__(self, answer: bytes | BaseException) -> None:
        self.answer = answer
        self.timeout: float | None = None
        self.sent = b""
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def connect(self, _target: str) -> None:
        return

    def sendall(self, payload: bytes) -> None:
        self.sent = payload

    def recv(self, _size: int) -> bytes:
        if isinstance(self.answer, BaseException):
            raise self.answer
        answer, self.answer = self.answer, b""
        return answer

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("verb", "expected"),
    [
        ("list", 5.0),
        # This is the old five-second false-failure boundary: a durable store
        # may legally spend that whole interval waiting for its writer lock.
        ("favourite", 15.0),
        ("displays", 10.0),
        ("playlist-delete", 45.0),
        ("remove", 60.0),
        ("reload-palette", 180.0),
        ("select", 110.0),
        ("playlist-use", 110.0),
        ("search", 60.0),
        ("download", 600.0),
    ],
)
def test_authoring_socket_uses_the_per_verb_deadline_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verb: str,
    expected: float,
) -> None:
    from wall_in_one.control import client

    probe = _ClientSocket(Response.success().encode())
    monkeypatch.setattr(socket, "socket", lambda *_arguments: probe)

    assert client.send(Request(verb), path=tmp_path / "authoring.sock").ok
    assert probe.timeout == expected
    assert Request.decode(probe.sent) == Request(verb)
    assert probe.closed


@pytest.mark.parametrize(
    ("verb", "expected"),
    [
        ("status", 5.0),
        ("shuffle", 5.0),
        ("playlist-use", 45.0),
    ],
)
def test_runtime_deadlines_do_not_inherit_same_named_authoring_budgets(
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    expected: float,
) -> None:
    from wall_in_one.control import client

    probe = _ClientSocket(Response.success().encode())
    monkeypatch.setattr(socket, "socket", lambda *_arguments: probe)

    assert client.send(Request(verb), path=paths.runtime_socket_path()).ok
    assert probe.timeout == expected


def test_a_durable_timeout_reports_unknown_outcome_and_requires_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from wall_in_one.control import client

    probe = _ClientSocket(TimeoutError())
    monkeypatch.setattr(socket, "socket", lambda *_arguments: probe)

    with pytest.raises(client.ControlError) as caught:
        client.send(Request("favourite", "/wallpapers/sky.png"), path=tmp_path / "app.sock")

    message = str(caught.value)
    assert "timed out after 15s" in message
    assert "outcome is unknown" in message
    assert "Verify the current state before retrying" in message


@pytest.mark.parametrize(
    "control_request",
    [
        Request("list"),
        Request("search", "wallhaven mountains"),
        # Argument-less cycle-interval only asks for the current value.
        Request("cycle-interval"),
    ],
)
def test_a_read_timeout_retains_the_plain_failure_wording(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    control_request: Request,
) -> None:
    from wall_in_one.control import client

    probe = _ClientSocket(TimeoutError())
    monkeypatch.setattr(socket, "socket", lambda *_arguments: probe)

    with pytest.raises(client.ControlError) as caught:
        client.send(control_request, path=tmp_path / "app.sock")

    assert str(caught.value) == f"timed out after {probe.timeout:g}s"


def test_runtime_status_timeout_retains_plain_read_wording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wall_in_one.control import client

    probe = _ClientSocket(TimeoutError())
    monkeypatch.setattr(socket, "socket", lambda *_arguments: probe)

    with pytest.raises(client.ControlError) as caught:
        client.send(Request("status"), path=paths.runtime_socket_path())

    assert str(caught.value) == "timed out after 5s"


def test_an_explicit_durable_deadline_keeps_the_unknown_outcome_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from wall_in_one.control import client

    probe = _ClientSocket(TimeoutError())
    monkeypatch.setattr(socket, "socket", lambda *_arguments: probe)

    with pytest.raises(client.ControlError, match=r"timed out after 0\.25s;.*outcome is unknown"):
        client.send(
            Request("remove", "/wallpapers/sky.png"),
            path=tmp_path / "app.sock",
            timeout=0.25,
        )


@pytest.mark.parametrize(
    "control_request",
    (
        Request("reload-palette"),
        Request("playlist-use", "Evening"),
        Request("quit"),
    ),
)
def test_authoring_side_effect_timeout_reports_that_the_command_may_complete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    control_request: Request,
) -> None:
    from wall_in_one.control import client

    probe = _ClientSocket(TimeoutError())
    monkeypatch.setattr(socket, "socket", lambda *_arguments: probe)

    with pytest.raises(client.ControlError) as caught:
        client.send(control_request, path=tmp_path / "app.sock")

    message = str(caught.value)
    assert "command may still complete" in message
    assert "outcome is unknown" in message
    assert "Verify the current state before retrying" in message


@pytest.mark.parametrize(
    "control_request",
    (
        Request("reload"),
        Request("quit"),
        Request("on", "DP-1 next"),
    ),
)
def test_runtime_side_effect_timeout_reports_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
    control_request: Request,
) -> None:
    from wall_in_one.control import client

    probe = _ClientSocket(TimeoutError())
    monkeypatch.setattr(socket, "socket", lambda *_arguments: probe)

    with pytest.raises(client.ControlError) as caught:
        client.send(control_request, path=paths.runtime_socket_path())

    assert "command may still complete" in str(caught.value)
    assert "outcome is unknown" in str(caught.value)


def test_runtime_actions_allow_a_bounded_multi_display_apply_to_finish() -> None:
    from wall_in_one.control import client

    assert client.RUNTIME_ACTION_TIMEOUT > client.TIMEOUT
    assert client.RUNTIME_ACTION_TIMEOUT < 55
    assert "next" in client.RUNTIME_APPLY_VERBS
    assert "status" not in client.RUNTIME_APPLY_VERBS
    assert "shuffle" not in client.RUNTIME_APPLY_VERBS


def test_the_cli_joins_its_words_back_into_one_argument() -> None:
    from wall_in_one.cli import _build_parser

    options = _build_parser().parse_args(["ctl", "search", "wallhaven", "aurora", "borealis"])
    assert options.argument == ["wallhaven", "aurora", "borealis"]


# -- where a download lands -----------------------------------------------


def test_a_control_download_lands_where_the_dialog_puts_one(tmp_path: Path) -> None:
    """Both surfaces take the first configured root, or leave the browser to decide."""
    from wall_in_one.ui.app import download_root

    first, second = tmp_path / "one", tmp_path / "two"
    assert download_root(config.Settings(roots=(first, second))) == first
    assert download_root(config.Settings()) is None


# -- what the library verbs take ------------------------------------------


def test_a_list_with_no_argument_is_everything() -> None:
    assert parse_list(None) == (Kinds.EVERYTHING, "")


def test_a_list_takes_a_kind_and_a_query_that_keeps_its_spaces() -> None:
    assert parse_list("videos snow village") == (Kinds.VIDEOS, "snow village")


@pytest.mark.parametrize("value", ["favourites", "FAVOURITES", "  favourites  "])
def test_a_kind_is_read_however_it_is_typed(value: str) -> None:
    assert parse_list(value) == (Kinds.FAVOURITES, "")


def test_a_first_word_that_names_no_kind_is_a_usage_error() -> None:
    """`list videos` would otherwise be ambiguous between the filter and a
    search for the word, so the kind is required rather than guessed at."""
    with pytest.raises(ValueError, match="usage: list"):
        parse_list("snow")


@pytest.mark.parametrize("verb", ["select", "favourite", "unfavourite", "remove"])
def test_a_path_verb_with_no_path_says_what_it_takes(verb: str) -> None:
    with pytest.raises(ValueError, match=f"usage: {verb} <path>"):
        parse_path("   ", verb=verb)


def test_a_relative_path_is_refused_rather_than_resolved() -> None:
    """This process's working directory is the window's, not the caller's, so a
    relative path here would quietly name a file somewhere else entirely."""
    with pytest.raises(ValueError, match="absolute"):
        parse_path("../holiday.png", verb="remove")


def test_a_tilde_the_shell_did_not_expand_still_means_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert parse_path("~/holiday.png", verb="select") == tmp_path / "holiday.png"


# -- resolving one against the library ------------------------------------


def _wallpaper(
    name: str,
    kind: Kind = Kind.STILL,
    ownership: Ownership = Ownership.USER,
    root: str = "/w",
) -> MediaItem:
    suffix = ".png" if kind is Kind.STILL else ".mp4"
    return MediaItem(
        path=Path(f"{root}/{name}{suffix}"),
        kind=kind,
        size=1,
        mtime=0,
        ownership=ownership,
    )


def test_a_path_the_library_knows_resolves_to_its_wallpaper() -> None:
    item = _wallpaper("aurora")
    library = Library(roots=(Path("/w"),), items=(item,))
    assert resolve(library, "/w/aurora.png", verb="select") is item


def test_a_path_the_library_does_not_know_is_refused(tmp_path: Path) -> None:
    """The important half. The file below is real and readable, and that is
    exactly why being handed one must not be enough to act on it."""
    stray = tmp_path / "somebody-elses.png"
    stray.write_bytes(b"\x89PNG\r\n\x1a\n")
    library = Library(roots=(tmp_path,), items=())

    with pytest.raises(UnknownWallpaperError) as caught:
        resolve(library, str(stray), verb="remove")

    assert caught.value.kind == "not-in-library"
    assert stray.is_file()


def test_a_path_that_says_the_same_thing_differently_is_not_believed() -> None:
    """Deciding that two strings name one file is a decision worth getting
    wrong nowhere, and least of all in front of `remove`."""
    library = Library(roots=(Path("/w"),), items=(_wallpaper("aurora"),))
    with pytest.raises(UnknownWallpaperError):
        resolve(library, "/w/../w/aurora.png", verb="remove")


# -- what a terminal sees of the library ----------------------------------


def test_the_library_is_listed_in_the_rows_search_already_uses() -> None:
    lines = render_library(
        (_wallpaper("aurora"), _wallpaper("clip", Kind.VIDEO, Ownership.MANAGED)),
        Query(),
        (),
    ).splitlines()

    assert lines[0] == "# library: 2 of 2 wallpapers"
    assert lines[1] == "# fields: path, kind, ownership, favourite"
    assert lines[2].split("\t") == ["/w/aurora.png", "still", "user", "no"]
    assert lines[3].split("\t") == ["/w/clip.mp4", "video", "managed", "no"]


def test_a_listing_selects_and_orders_exactly_as_the_grid_does() -> None:
    """`library.filter` does the matching, so the words typed at a socket and
    the words typed into the search box cannot come to mean different things."""
    items = (_wallpaper("snowy-village"), _wallpaper("aurora"), _wallpaper("clip", Kind.VIDEO))
    lines = render_library(items, Query(text="vil snow"), ()).splitlines()

    assert lines[0] == '# library: 1 of 3 wallpapers matching "vil snow"'
    assert lines[2].split("\t")[0] == "/w/snowy-village.png"


def test_a_listing_of_one_kind_says_so_in_its_summary() -> None:
    items = (_wallpaper("aurora"), _wallpaper("clip", Kind.VIDEO))
    assert render_library(items, Query(kinds=Kinds.VIDEOS), ()).splitlines()[0] == (
        "# library: 1 of 2 videos or scenes"
    )


def test_the_favourites_column_and_the_favourites_view_agree() -> None:
    starred, plain = _wallpaper("aurora"), _wallpaper("clip", Kind.VIDEO)
    items = (starred, plain)

    everything = render_library(items, Query(), (starred.path,)).splitlines()
    assert [line.split("\t")[3] for line in everything[2:]] == ["yes", "no"]

    narrowed = render_library(items, Query(kinds=Kinds.FAVOURITES), (starred.path,)).splitlines()
    assert [line.split("\t")[0] for line in narrowed[2:]] == ["/w/aurora.png"]


def test_a_name_that_would_invent_a_column_is_counted_rather_than_mangled() -> None:
    """A path is the field with a use -- it is what `remove` takes back -- so
    unlike a provider's title it may not be tidied into something that no
    longer names the file."""
    odd = MediaItem(path=Path("/w/two\tcolumns.png"), kind=Kind.STILL, size=1, mtime=0)
    lines = render_library((odd, _wallpaper("aurora")), Query(), ()).splitlines()

    assert lines[0] == "# library: 1 of 2 wallpapers - 1 unlistable"
    assert [line.split("\t")[0] for line in lines[2:]] == ["/w/aurora.png"]


def _crowded(count: int) -> tuple[MediaItem, ...]:
    return tuple(_wallpaper(f"{index:04d}-{'wallpaper' * 20}") for index in range(count))


def test_a_library_too_large_for_one_reply_is_cut_between_rows() -> None:
    """Six hundred wallpapers with long names do not fit the authoring frame, and
    a size error instead of a listing would be a poor way to find that out."""
    lines = render_library(_crowded(600), Query(), ()).splitlines()

    assert "more than fit in one reply" in lines[0]
    assert len(lines) - 2 < 600
    # Every row that did survive is a whole one, so a script reading the last
    # line gets a path rather than the front half of one.
    assert all(line.endswith(("\tyes", "\tno")) for line in lines[2:])


def test_a_listing_always_fits_in_the_frame_it_has_to_travel_in() -> None:
    encoded = Response.success(render_library(_crowded(600), Query(), ())).encode()
    assert len(encoded) <= MAX_MESSAGE_BYTES


def test_an_empty_library_is_the_summary_and_the_columns() -> None:
    lines = render_library((), Query(), ()).splitlines()
    assert lines == ["# library: 0 of 0 wallpapers", "# fields: path, kind, ownership, favourite"]


# -- the starred list -----------------------------------------------------


def test_the_favourites_are_listed_in_the_order_they_were_marked() -> None:
    entries = (Path("/w/clip.mp4"), Path("/w/aurora.png"))
    lines = render_favourites(entries, entries).splitlines()

    assert lines[0] == "# favourites: 2 starred"
    assert lines[1] == "# fields: path, present"
    assert [line.split("\t")[0] for line in lines[2:]] == ["/w/clip.mp4", "/w/aurora.png"]


def test_a_favourite_whose_file_is_not_here_is_shown_rather_than_dropped() -> None:
    """`library.favourites` keeps it on purpose -- an unmounted drive is not the
    user changing their mind -- so the listing has to be able to say so."""
    entries = (Path("/w/aurora.png"), Path("/elsewhere/gone.png"))
    lines = render_favourites(entries, (Path("/w/aurora.png"),)).splitlines()

    assert lines[0] == "# favourites: 2 starred - 1 not in the library right now"
    assert lines[3].split("\t") == ["/elsewhere/gone.png", "no"]


def test_no_favourites_at_all_says_none_rather_than_nothing() -> None:
    assert render_favourites((), ()).splitlines()[0] == "# favourites: 0 starred"


# -- taking a wallpaper away ----------------------------------------------

#: The marker `library.scan` reads as "this app made this directory".
#: `tests/test_manage.py` owns the exhaustive version of all of this; what is
#: needed here is one file of each ownership.
MANAGED_MARKER = ".managed-by-wall-in-one-v1.json"


def _provenance(path: Path, *, expected_path: Path | None = None) -> dict[str, object]:
    contents = path.read_bytes()
    status = path.stat()
    return {
        "schema": 1,
        "plugin": "goober/wall-in-one",
        "provider": "Wallhaven",
        "path": str(path if expected_path is None else expected_path),
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


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A library root, with the state and data homes pointed away from the user's.

    The favourites live under the state home and the trash under the data home,
    and nothing here may go near either of the real ones.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    root = tmp_path / "wallpapers"
    root.mkdir()
    return root


def _downloaded(root: Path, name: str = "aurora.jpg", *, sidecar: bool = True) -> Path:
    """A file with both halves of ownership: the directory marker and the sidecar."""
    directory = root / "Wall-in-One" / "Wallhaven"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MANAGED_MARKER).write_text(
        json.dumps(
            {
                "schema": 1,
                "plugin": "goober/wall-in-one",
                "provider": "Wallhaven",
                "kind": "wallhaven",
                "ownership": "managed",
            }
        ),
        encoding="utf-8",
    )
    path = directory / name
    path.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
    if sidecar:
        path.with_name(path.name + ".wallhaven.json").write_text(
            json.dumps(_provenance(path)),
            encoding="utf-8",
        )
    return path


def _their_own(root: Path, name: str = "holiday.png") -> Path:
    path = root / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return path


def _on_disk(path: Path, ownership: Ownership) -> MediaItem:
    status = path.stat()
    return MediaItem(
        path=path,
        kind=Kind.STILL,
        size=status.st_size,
        mtime=int(status.st_mtime),
        ownership=ownership,
    )


def _source_identity(path: Path) -> manage.SourceIdentity:
    status = path.lstat()
    return status.st_dev, status.st_ino


def _source_fingerprint(path: Path) -> manage.SourceFingerprint:
    return file_io.regular_file_fingerprint(path)


REMOVAL_TOKEN = "0123456789abcdef0123456789abcdef"


def test_a_downloaded_wallpaper_is_deleted_and_the_reply_says_which(sandbox: Path) -> None:
    path = _downloaded(sandbox)
    message = remove_wallpaper(
        _on_disk(path, Ownership.MANAGED),
        (sandbox,),
        expected_source=_source_identity(path),
        expected_fingerprint=_source_fingerprint(path),
        operation_token=REMOVAL_TOKEN,
    )

    assert not path.exists()
    assert message == "removed aurora.jpg and 1 file beside it - deleted, which cannot be undone"


def test_a_wallpaper_of_the_users_own_is_trashed_and_the_reply_says_which(
    sandbox: Path, tmp_path: Path
) -> None:
    """Two very different things to have done to somebody's file, and only one
    of them can be undone, so the sentence may not be the same either."""
    path = _their_own(sandbox)
    message = remove_wallpaper(
        _on_disk(path, Ownership.USER),
        (sandbox,),
        expected_source=_source_identity(path),
        expected_fingerprint=_source_fingerprint(path),
    )

    assert not path.exists()
    landed = tmp_path / "data" / "Trash" / "files" / "holiday.png"
    assert landed.is_file()
    assert message == f"holiday.png moved to the trash - {landed}"


def test_a_stale_claim_of_ownership_still_does_not_delete_anything(sandbox: Path) -> None:
    """The scan may be minutes old. `library.manage` re-derives ownership from
    disk, and the socket has no confirmation dialogue to fall back on."""
    path = _downloaded(sandbox, sidecar=False)

    with pytest.raises(ManageError) as caught:
        remove_wallpaper(
            _on_disk(path, Ownership.MANAGED),
            (sandbox,),
            expected_source=_source_identity(path),
            expected_fingerprint=_source_fingerprint(path),
            operation_token=REMOVAL_TOKEN,
        )

    assert caught.value.kind == "not-ours"
    assert path.is_file()


def test_a_removal_refusal_travels_as_a_failure_with_its_kind() -> None:
    response = failed(ManageError("not-ours", "holiday.png is your own file"))
    assert not response.ok
    assert response.kind == "not-ours"
    assert response.message == "not-ours: holiday.png is your own file"


def test_a_favourites_write_failure_travels_with_its_kind_too() -> None:
    response = failed(favourites.FavouritesError("local-io", "the disk is full"))
    assert (response.ok, response.kind) == (False, "local-io")


# -- the verbs against a real session -------------------------------------


class _FakeRenderer:
    """Stands in for mpvpaper, which is not going to be started here."""

    def start(self, video: Path) -> None: ...

    def stop(self) -> None: ...


class _FakeApp:
    """Just enough `Application` for the library verbs.

    The session they read, the wrapper navigation goes through, and the three
    calls that leave the running window agreeing with whatever the socket has
    just done. Counting those is how the tests below check that a star, a
    deletion or a pairing is not left only in the session.
    """

    def __init__(self, session: Session) -> None:
        self.legacy_service = True
        self.session = session
        self.restarred = 0
        self.forgotten: list[Path] = []
        self.repaired: list[Path] = []
        self.relisted = 0
        self.rescheduled = 0
        self.settings_written: list[dict[str, object]] = []
        self.presented_pages: list[str] = []
        self.runtime_publications = 0

    def apply(self, action: Callable[[], Applied]) -> Response:
        return Response.success(action().describe())

    def play_item(self, item: MediaItem) -> Response:
        return self.apply(lambda: self.session.select(item.path))

    def favourites_changed(self) -> None:
        self.restarred += 1

    def forget(self, path: Path) -> None:
        self.forgotten.append(path)

    def prepare_item_removal(self, item: MediaItem) -> removals.Intent:
        return self.session.prepare_removal(item, self.session.library.roots)

    def cancel_item_removal(self, intent: removals.Intent) -> tuple[str, ...]:
        try:
            self.session.cancel_removal(intent)
        except removals.RemovalJournalError as error:
            return (f"removal journal: {error}",)
        return ()

    def forget_item(
        self,
        item: MediaItem,
        *,
        intent: removals.Intent,
        artifacts_already_clean: bool = True,
        artifact_source_root: Path | None = None,
        artifact_lookup_root: Path | None = None,
        artifact_lookup_parent: Path | None = None,
        artifact_source_context: file_io.PinnedDirectoryContext | None = None,
    ) -> tuple[str, ...]:
        self.forgotten.append(item.path)
        return self.session.commit_removal(
            intent,
            artifacts_already_clean=artifacts_already_clean,
            artifact_source_root=artifact_source_root,
            artifact_lookup_root=artifact_lookup_root,
            artifact_lookup_parent=artifact_lookup_parent,
            artifact_source_context=artifact_source_context,
        )

    def pairing_changed(self, item: MediaItem) -> None:
        self.repaired.append(item.path)

    def playlists_changed(self) -> None:
        self.relisted += 1

    def runtime_config_changed(self) -> None:
        self.runtime_publications += 1

    def schedule_edited(self) -> None:
        self.rescheduled += 1

    def update_settings(self, **changes: object) -> None:
        self.settings_written.append(changes)

    def activate_playlist(self, reference: str) -> Response:
        chosen = self.session.use_playlist(reference)
        response = self.apply(self.session.apply_current)
        return Response.success(f"playing {chosen.name}") if response.ok else response

    def resume_schedule(self) -> Response:
        self.session.resume_schedule()
        return self.apply(self.session.apply_current)

    def present_page(self, page: str) -> None:
        self.presented_pages.append(page)

    def runtime_off_thread(self, work: Callable[[], Response]) -> Deferred:
        """Deterministic stand-in for Application's ordered runtime worker."""
        return Deferred(start=lambda reply: reply(work()))


@pytest.fixture
def applied(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Nothing reaches the real shell.

    Applying shells out to `noctalia msg`, which would change the wallpaper --
    and, since pairings carry a palette, the colour scheme -- of whoever is
    running the suite. `conftest` refuses all three by default; this stands in
    for them, because these tests do mean to apply.
    """
    calls: list[Path] = []

    def record(path: Path, connector: str | None = None) -> None:
        calls.append(Path(path))

    monkeypatch.setattr("wall_in_one.theme.noctalia.set_wallpaper", record)
    monkeypatch.setattr("wall_in_one.theme.noctalia.set_scheme", lambda _selection: None)
    monkeypatch.setattr("wall_in_one.theme.noctalia.set_mode", lambda _mode: None)
    return calls


def _commands(sandbox: Path, items: Sequence[MediaItem]) -> tuple[_Commands, _FakeApp]:
    from wall_in_one.ui.app import _Commands

    library = Library(roots=(sandbox,), items=tuple(items))
    session = Session(
        config.Settings().validated(),
        applier=Applier(_FakeRenderer()),  # type: ignore[arg-type]
        scanner=lambda _roots: library,
        favourite_store=favourites.Store(path=sandbox / "favourites.json"),
    )
    session.refresh()
    app = _FakeApp(session)
    return _Commands(cast("Application", app)), app


def _immediate(outcome: Outcome) -> Response:
    """The explicit legacy-service fixture never defers its compatibility I/O."""
    assert isinstance(outcome, Response)
    return outcome


@pytest.mark.parametrize(
    ("requested", "shown"),
    [
        ("media", "media"),
        ("browse", "browse"),
        ("pairings", "media"),
        ("playlists", "playlists"),
        ("schedules", "schedules"),
        ("displays", "schedules"),
        ("settings", "settings"),
        (" SCHEDULES ", "schedules"),
    ],
)
def test_open_presents_the_singleton_on_the_requested_page(
    sandbox: Path, requested: str, shown: str
) -> None:
    commands, app = _commands(sandbox, [])

    response = commands.open_page(requested)

    assert response == Response.success(f"opened {shown}")
    assert app.presented_pages == [shown]


@pytest.mark.parametrize("requested", [None, "", "schedules now"])
def test_open_rejects_unknown_pages_without_presenting(
    sandbox: Path, requested: str | None
) -> None:
    commands, app = _commands(sandbox, [])

    response = commands.open_page(requested)

    assert not response.ok
    assert response.message == (
        "usage: open <browse|media|pairings|playlists|schedules|displays|settings>"
    )
    assert app.presented_pages == []


def test_plain_gui_status_refuses_to_masquerade_as_the_runtime(sandbox: Path) -> None:
    commands, app = _commands(sandbox, [])
    app.legacy_service = False

    response = commands.report_status()

    assert not response.ok
    assert response.kind == "runtime-not-running"


@pytest.mark.parametrize("verb", ("next_wallpaper", "previous_wallpaper", "random_wallpaper"))
def test_plain_gui_legacy_navigation_never_owns_a_renderer(sandbox: Path, verb: str) -> None:
    commands, app = _commands(sandbox, [_wallpaper("first"), _wallpaper("second")])
    app.legacy_service = False
    before = app.session.cursor

    response = getattr(commands, verb)()

    assert not response.ok
    assert response.kind == "runtime-not-running"
    assert app.session.cursor is before


def test_legacy_service_status_remains_available(sandbox: Path) -> None:
    commands, _app = _commands(sandbox, [])

    response = commands.report_status()

    assert response.ok


def test_runtime_quit_does_not_close_a_plain_authoring_window(sandbox: Path) -> None:
    commands, app = _commands(sandbox, [])
    app.legacy_service = False

    response = commands.quit()

    assert not response.ok
    assert response.kind == "runtime-not-running"


def test_the_listing_reports_what_the_session_is_holding(sandbox: Path) -> None:
    commands, _app = _commands(sandbox, [_wallpaper("aurora"), _wallpaper("clip", Kind.VIDEO)])
    response = commands.list_library("stills")

    assert response.ok
    assert response.message.splitlines()[0] == "# library: 1 of 2 stills"


def test_a_star_set_over_the_socket_is_the_sessions_own(sandbox: Path) -> None:
    """A store of its own here would mean the socket and the tile in the window
    disagreeing about the same wallpaper until the next launch."""
    item = _wallpaper("aurora")
    commands, app = _commands(sandbox, [item])

    response = _immediate(commands.add_favourite(str(item.path)))

    assert response.message == "aurora.png starred"
    assert app.session.favourites.is_favourite(item.path)
    assert app.restarred == 1


def test_starring_one_twice_says_it_was_already_starred(sandbox: Path) -> None:
    item = _wallpaper("aurora")
    commands, _app = _commands(sandbox, [item])
    commands.add_favourite(str(item.path))
    assert (
        _immediate(commands.add_favourite(str(item.path))).message
        == "aurora.png was already starred"
    )


def test_a_star_cannot_be_put_on_something_the_library_has_never_seen(
    sandbox: Path, tmp_path: Path
) -> None:
    stray = tmp_path / "somebody-elses.png"
    stray.write_bytes(b"\x89PNG\r\n\x1a\n")
    commands, app = _commands(sandbox, [])

    response = handle(build_verb_table(commands), Request("favourite", str(stray)).encode())

    assert isinstance(response, Response)
    assert (response.ok, response.kind) == (False, "not-in-library")
    assert len(app.session.favourites) == 0


def test_a_star_can_always_be_taken_off_even_when_the_file_has_gone(sandbox: Path) -> None:
    """The entries most worth removing by hand are exactly the ones a lookup in
    the library would refuse, which is why `unfavourite` does not do one."""
    gone = Path("/elsewhere/unmounted.png")
    commands, app = _commands(sandbox, [])
    app.session.favourites.add(gone)

    response = _immediate(commands.remove_favourite(str(gone)))

    assert response.message == "unmounted.png unstarred"
    assert len(app.session.favourites) == 0
    assert app.restarred == 1


def test_unstarring_something_that_was_never_starred_says_so(sandbox: Path) -> None:
    commands, _app = _commands(sandbox, [])
    assert (
        _immediate(commands.remove_favourite("/w/aurora.png")).message
        == "aurora.png was not starred"
    )


def test_a_star_that_could_not_be_saved_is_reported_and_rolled_back(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disk and memory remain one state when persistence fails."""
    item = _wallpaper("aurora")
    commands, app = _commands(sandbox, [item])

    def refuse(*_arguments: object, **_keywords: object) -> Path:
        raise favourites.FavouritesError("local-io", "no space left on device")

    monkeypatch.setattr("wall_in_one.library.favourites.save", refuse)
    response = _immediate(commands.add_favourite(str(item.path)))

    assert (response.ok, response.kind) == (False, "local-io")
    assert not app.session.favourites.is_favourite(item.path)
    assert app.restarred == 0


def test_the_starred_list_comes_back_from_the_socket(sandbox: Path) -> None:
    item = _wallpaper("aurora")
    commands, _app = _commands(sandbox, [item])
    commands.add_favourite(str(item.path))

    lines = commands.list_favourites().message.splitlines()

    assert lines[0] == "# favourites: 1 starred"
    assert lines[2].split("\t") == [str(item.path), "yes"]


def test_removing_a_wallpaper_deletes_it_and_tells_the_window(sandbox: Path) -> None:
    path = _downloaded(sandbox)
    item = _on_disk(path, Ownership.MANAGED)
    commands, app = _commands(sandbox, [item])

    response = _immediate(commands.remove_wallpaper(str(path)))

    assert response.ok
    assert "deleted, which cannot be undone" in response.message
    assert not path.exists()
    # Which is what drops the star and rescans, so no tile outlives the file.
    assert app.forgotten == [path]


def test_legacy_remove_uses_the_prepared_root_after_a_same_inode_root_swap(
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _downloaded(sandbox)
    item = _on_disk(path, Ownership.MANAGED)
    original_pairing = path.with_name(path.name + pairing.SIDECAR_SUFFIX)
    original_pairing.write_bytes(b"old pairing")
    replacement_root = sandbox.parent / "replacement-wallpapers"
    replacement_parent = replacement_root / path.parent.relative_to(sandbox)
    replacement_parent.mkdir(parents=True)
    (replacement_parent / MANAGED_MARKER).write_bytes((path.parent / MANAGED_MARKER).read_bytes())
    replacement_source = replacement_parent / path.name
    replacement_source.hardlink_to(path)
    # Creating the deliberate same-inode alias advances ctime. Refresh the
    # original app authority so this remains a root-capability test rather
    # than correctly failing the independent lifecycle binding first.
    path.with_name(path.name + ".wallhaven.json").write_text(
        json.dumps(_provenance(path)),
        encoding="utf-8",
    )
    replacement_source.with_name(path.name + ".wallhaven.json").write_text(
        json.dumps(_provenance(replacement_source, expected_path=path)),
        encoding="utf-8",
    )
    replacement_pairing = replacement_source.with_name(
        replacement_source.name + pairing.SIDECAR_SUFFIX
    )
    replacement_pairing.write_bytes(b"replacement pairing")
    commands, app = _commands(sandbox, [item])
    disconnected = sandbox.parent / "disconnected-wallpapers"
    real_source_pin = removals.Store.source_pin
    swapped = False

    def pin_then_replace(
        store: removals.Store,
        intent: removals.Intent,
    ) -> file_io.PinnedPath:
        nonlocal swapped
        pin = real_source_pin(store, intent)
        if not swapped:
            sandbox.rename(disconnected)
            replacement_root.rename(sandbox)
            swapped = True
        return pin

    monkeypatch.setattr(removals.Store, "source_pin", pin_then_replace)

    response = _immediate(commands.remove_wallpaper(str(path)))

    assert response.ok
    assert swapped
    disconnected_path = disconnected / path.relative_to(sandbox)
    assert not disconnected_path.exists()
    assert not disconnected_path.with_name(disconnected_path.name + pairing.SIDECAR_SUFFIX).exists()
    assert path.read_bytes().startswith(b"\xff\xd8\xff")
    assert path.with_name(path.name + pairing.SIDECAR_SUFFIX).read_bytes() == b"replacement pairing"
    assert app.session.removal_journal.records == ()


def test_legacy_remove_retains_the_journal_when_a_replacement_cannot_be_restored(
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _downloaded(sandbox)
    item = _on_disk(path, Ownership.MANAGED)
    commands, app = _commands(sandbox, [item])
    original = sandbox / "prepared-original.jpg"
    real_rename = file_io._rename_noreplace
    preserved: Path | None = None
    raced = False

    def move_replacement_then_reoccupy(source: Path, destination: Path) -> None:
        nonlocal preserved, raced
        if source.name == path.name and not raced:
            raced = True
            preserved = destination.resolve()
            source.rename(original)
            source.write_bytes(b"replacement B")
            real_rename(source, destination)
            source.write_bytes(b"replacement C")
            return
        real_rename(source, destination)

    monkeypatch.setattr(file_io, "_rename_noreplace", move_replacement_then_reoccupy)

    response = _immediate(commands.remove_wallpaper(str(path)))

    assert not response.ok
    assert response.kind == "changed"
    assert "pending removal record was retained" in response.message
    assert preserved is not None
    assert str(preserved) in response.message
    assert path.read_bytes() == b"replacement C"
    assert preserved.read_bytes() == b"replacement B"
    assert original.read_bytes().startswith(b"\xff\xd8\xff")
    (intent,) = app.session.removal_journal.records
    assert not intent.committed
    assert not app.session.removal_journal.operation_is_active()
    assert app.forgotten == []


def test_committed_removal_reports_incomplete_metadata_cleanup(
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _downloaded(sandbox)
    item = _on_disk(path, Ownership.MANAGED)
    commands, app = _commands(sandbox, [item])

    def incomplete(
        removed: MediaItem,
        *,
        intent: removals.Intent,
        **_cleanup: object,
    ) -> tuple[str, ...]:
        del intent
        app.forgotten.append(removed.path)
        return (
            "pairing: local-io: pairings.json is read-only",
            "playlists: local-io: disk is full",
        )

    monkeypatch.setattr(app, "forget_item", incomplete)

    response = _immediate(commands.remove_wallpaper(str(path)))

    assert not response.ok
    assert response.kind == "metadata-cleanup"
    assert not path.exists(), "the response must be honest that deletion already committed"
    assert "pairings.json is read-only" in response.message
    assert "disk is full" in response.message
    assert "refresh the library to retry" in response.message


def test_removal_journal_failure_refuses_before_touching_media(
    sandbox: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _downloaded(sandbox)
    item = _on_disk(path, Ownership.MANAGED)
    commands, app = _commands(sandbox, [item])

    def refuse(_records: object, _path: Path) -> None:
        raise removals.RemovalJournalError("local-io", "state directory is read-only")

    monkeypatch.setattr(removals, "_save", refuse)

    response = _immediate(commands.remove_wallpaper(str(path)))

    assert not response.ok
    assert response.kind == "local-io"
    assert "state directory is read-only" in response.message
    assert path.is_file(), "no physical operation may start without a durable intent"
    assert app.forgotten == []


def test_a_removal_the_library_did_not_authorise_never_reaches_the_disk(
    sandbox: Path, tmp_path: Path
) -> None:
    """The refusals in `library.manage` are the only protection a socket has,
    and they are only reached by wallpapers the scan actually found."""
    stray = tmp_path / "somebody-elses.png"
    stray.write_bytes(b"\x89PNG\r\n\x1a\n")
    commands, app = _commands(sandbox, [])

    response = handle(build_verb_table(commands), Request("remove", str(stray)).encode())

    assert isinstance(response, Response)
    assert (response.ok, response.kind) == (False, "not-in-library")
    assert stray.is_file()
    assert app.forgotten == []


def test_a_refused_removal_leaves_the_file_and_carries_the_reason(sandbox: Path) -> None:
    path = _downloaded(sandbox, sidecar=False)
    commands, app = _commands(sandbox, [_on_disk(path, Ownership.MANAGED)])

    response = handle(build_verb_table(commands), Request("remove", str(path)).encode())

    assert isinstance(response, Response)
    assert (response.ok, response.kind) == (False, "not-ours")
    assert path.is_file()
    assert app.forgotten == []


def test_selecting_a_wallpaper_by_path_applies_that_wallpaper(
    sandbox: Path, applied: list[Path]
) -> None:
    first, second = _wallpaper("aurora"), _wallpaper("clip")
    commands, _app = _commands(sandbox, [first, second])

    response = _immediate(commands.select_wallpaper(str(second.path)))

    assert response.message == "set clip.png"
    assert applied == [second.path]


def test_legacy_socket_select_refuses_a_borked_wallpaper(
    sandbox: Path, applied: list[Path]
) -> None:
    crasher = _wallpaper("crasher", Kind.VIDEO)
    commands, app = _commands(sandbox, [crasher])
    app.session.pairings.mark_borked(crasher, "decoder crashed", "renderer-crash")

    response = _immediate(commands.select_wallpaper(str(crasher.path)))

    assert not response.ok
    assert "marked Borked and cannot play" in response.message
    assert applied == []


def test_selecting_something_not_in_the_library_applies_nothing(
    sandbox: Path, applied: list[Path]
) -> None:
    commands, _app = _commands(sandbox, [_wallpaper("aurora")])

    response = handle(build_verb_table(commands), Request("select", "/w/nowhere.png").encode())

    assert isinstance(response, Response)
    assert (response.ok, response.kind) == (False, "not-in-library")
    assert applied == []


# -- the socket itself ----------------------------------------------------


def test_client_cancellation_wakes_an_active_runtime_deadline(tmp_path: Path) -> None:
    """App shutdown must not inherit the runtime apply's 45-second wait."""
    from wall_in_one.control import client

    path = tmp_path / "hung-runtime.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    cancellation = client.Cancellation()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(
        client.send,
        Request("play"),
        path=path,
        timeout=client.RUNTIME_ACTION_TIMEOUT,
        cancellation=cancellation,
    )
    connection, _address = listener.accept()
    try:
        connection.recv(MAX_MESSAGE_BYTES)
        started = time.monotonic()
        cancellation.cancel()
        with pytest.raises(client.ControlError):
            future.result(timeout=1)
        elapsed = time.monotonic() - started
        assert elapsed < 1
        with pytest.raises(client.ControlError, match="cancelled"):
            client.send(Request("play"), path=path, cancellation=cancellation)
    finally:
        cancellation.cancel()
        connection.close()
        listener.close()
        executor.shutdown(wait=True, cancel_futures=True)


def test_a_socket_path_too_long_to_bind_is_refused_before_anything_is_created(
    tmp_path: Path,
) -> None:
    """AF_UNIX caps the path near 108 bytes, and `Gio.SocketService.add_address`
    does not say so -- it returns having created nothing, and the failure used
    to surface only after the bind attempt, when no window ever came back.
    """
    from wall_in_one.control.server import SocketServer

    long_enough = tmp_path / ("d" * 120) / "wall-in-one.sock"
    server = SocketServer(_StubCommands(), long_enough)

    with pytest.raises(RuntimeError, match="too long"):
        server.start()

    assert not long_enough.parent.exists()


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_start_never_deletes_a_non_socket_control_path(tmp_path: Path, kind: str) -> None:
    from wall_in_one.control.server import SocketServer

    path = tmp_path / "wall-in-one.sock"
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("keep me", encoding="utf-8")
    if kind == "file":
        path.write_text("also keep me", encoding="utf-8")
    else:
        path.symlink_to(sentinel)

    server = SocketServer(_StubCommands(), path)
    with pytest.raises(RuntimeError, match="refusing to replace non-socket"):
        server.start()

    if kind == "file":
        assert path.read_text(encoding="utf-8") == "also keep me"
    else:
        assert path.is_symlink()
        assert path.readlink() == sentinel
    assert sentinel.read_text(encoding="utf-8") == "keep me"


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_start_refuses_an_unsafe_ownership_lock(tmp_path: Path, kind: str) -> None:
    from wall_in_one.control.server import SocketServer

    server = SocketServer(_StubCommands(), tmp_path / "wall-in-one.sock")
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("keep me", encoding="utf-8")
    if kind == "symlink":
        server.lock_path.symlink_to(sentinel)
    else:
        os.link(sentinel, server.lock_path)

    with pytest.raises(RuntimeError, match="ownership lock"):
        server.start()

    assert not server.path.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep me"
    if kind == "symlink":
        assert server.lock_path.is_symlink()
    else:
        assert server.lock_path.stat().st_nlink == 2


def test_a_dead_unix_socket_is_the_only_path_start_removes(tmp_path: Path) -> None:
    from wall_in_one.control.server import SocketServer

    path = tmp_path / "wall-in-one.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()

    server = SocketServer(_StubCommands(), path)
    server._acquire_instance_lock()
    try:
        server._clear_stale_socket()
    finally:
        server.stop()

    assert not path.exists()
    retained = tuple((tmp_path / file_io.RETAINED_ENTRY_DIRECTORY).iterdir())
    assert len(retained) == 1 and retained[0].is_socket()


def test_a_live_unix_socket_is_never_stolen(tmp_path: Path) -> None:
    from wall_in_one.control.server import SocketServer

    path = tmp_path / "wall-in-one.sock"
    live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    live.bind(str(path))
    live.listen(1)
    try:
        server = SocketServer(_StubCommands(), path)
        server._acquire_instance_lock()
        try:
            with pytest.raises(RuntimeError, match="another instance"):
                server._clear_stale_socket()
        finally:
            server.stop()
        assert path.is_socket()
    finally:
        live.close()
        path.unlink(missing_ok=True)


def test_stale_cleanup_preserves_a_replacement_after_the_final_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The atomic claim, not the preceding lstat, authorises removal."""
    from wall_in_one.control import server as server_module

    path = tmp_path / "wall-in-one.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    server = server_module.SocketServer(_StubCommands(), path)
    move = file_io.atomic_move_no_replace
    replaced = False

    def replace_after_recheck(
        source: Path,
        destination: Path,
        *,
        expected_identity: tuple[int, int],
        expected_file_type: int = stat.S_IFREG,
        expected_fingerprint: file_io.FileFingerprint | None = None,
        pinned_source: file_io.PinnedPath | None = None,
        externally_pinned: bool = False,
    ) -> None:
        nonlocal replaced
        if source == path and not replaced:
            assert expected_file_type == stat.S_IFSOCK
            replaced = True
            source.unlink()
            source.write_text("replacement", encoding="utf-8")
        move(
            source,
            destination,
            expected_identity=expected_identity,
            expected_file_type=expected_file_type,
            expected_fingerprint=expected_fingerprint,
            pinned_source=pinned_source,
            externally_pinned=externally_pinned,
        )

    monkeypatch.setattr(file_io, "atomic_move_no_replace", replace_after_recheck)
    server._acquire_instance_lock()
    try:
        with pytest.raises(RuntimeError, match="changed"):
            server._clear_stale_socket()
    finally:
        server.stop()

    assert replaced
    assert path.read_text(encoding="utf-8") == "replacement"


def test_stale_cleanup_pins_the_socket_inode_through_the_atomic_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A held O_PATH reference prevents same-type inode-number reuse."""
    from wall_in_one.control import server as server_module

    path = tmp_path / "wall-in-one.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    server = server_module.SocketServer(_StubCommands(), path)
    real_open = os.open
    real_move = file_io.atomic_move_no_replace
    pinned_descriptor: int | None = None

    def watch_open(
        candidate: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal pinned_descriptor
        descriptor = real_open(candidate, flags, mode, dir_fd=dir_fd)
        if os.fsdecode(candidate) == os.fsdecode(path) and flags & os.O_PATH:
            pinned_descriptor = descriptor
        return descriptor

    def observe_claim(
        source: Path,
        destination: Path,
        *,
        expected_identity: tuple[int, int],
        expected_file_type: int = stat.S_IFREG,
        expected_fingerprint: file_io.FileFingerprint | None = None,
        pinned_source: file_io.PinnedPath | None = None,
        externally_pinned: bool = False,
    ) -> None:
        assert pinned_descriptor is not None
        pinned = os.fstat(pinned_descriptor)
        assert stat.S_ISSOCK(pinned.st_mode)
        assert (pinned.st_dev, pinned.st_ino) == expected_identity
        assert expected_file_type == stat.S_IFSOCK
        real_move(
            source,
            destination,
            expected_identity=expected_identity,
            expected_file_type=expected_file_type,
            expected_fingerprint=expected_fingerprint,
            pinned_source=pinned_source,
            externally_pinned=externally_pinned,
        )

    monkeypatch.setattr(os, "open", watch_open)
    monkeypatch.setattr(file_io, "atomic_move_no_replace", observe_claim)
    server._acquire_instance_lock()
    try:
        server._clear_stale_socket()
        assert pinned_descriptor is not None
        with pytest.raises(OSError):
            os.fstat(pinned_descriptor)
    finally:
        server.stop()

    assert not path.exists()


@pytest.mark.parametrize("replacement_kind", ("regular", "socket"))
def test_stale_cleanup_preserves_a_post_claim_retained_name_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    from wall_in_one.control.server import SocketServer

    path = tmp_path / "wall-in-one.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    server = SocketServer(_StubCommands(), path)
    retain = file_io._retain_exact_entry
    preserved_original: Path | None = None
    replacement_socket: socket.socket | None = None

    def replace_after_claim(source: Path, **keywords: object) -> Path:
        nonlocal preserved_original, replacement_socket
        retained = retain(source, **keywords)  # type: ignore[arg-type]
        preserved_original = retained.with_name("preserved-original-socket")
        retained.rename(preserved_original)
        if replacement_kind == "regular":
            retained.write_text("unrelated replacement", encoding="utf-8")
        else:
            replacement_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # Bind at the shorter public name first: Linux's AF_UNIX pathname
            # limit is lower than pytest's nested temporary path plus the
            # retained random name. Renaming preserves the socket inode/type.
            replacement_socket.bind(str(path))
            path.rename(retained)
        return retained

    monkeypatch.setattr(file_io, "_retain_exact_entry", replace_after_claim)
    server._acquire_instance_lock()
    try:
        server._clear_stale_socket()

        assert not path.exists()
        assert preserved_original is not None and preserved_original.is_socket()
        retained_entries = tuple((tmp_path / file_io.RETAINED_ENTRY_DIRECTORY).iterdir())
        replacement = next(entry for entry in retained_entries if entry != preserved_original)
        if replacement_kind == "regular":
            assert replacement.read_text(encoding="utf-8") == "unrelated replacement"
        else:
            assert replacement.is_socket()
    finally:
        server.stop()
        if replacement_socket is not None:
            replacement_socket.close()


def test_stop_preserves_a_path_that_replaced_the_owned_socket(tmp_path: Path) -> None:
    from wall_in_one.control.server import SocketServer

    path = tmp_path / "wall-in-one.sock"
    server = SocketServer(_StubCommands(), path)
    server.start()
    path.unlink()
    path.write_text("replacement", encoding="utf-8")

    server.stop()

    assert path.read_text(encoding="utf-8") == "replacement"


def test_stop_pins_the_owned_socket_inode_through_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wall_in_one.control.server import SocketServer

    path = tmp_path / "wall-in-one.sock"
    server = SocketServer(_StubCommands(), path)
    server.start()
    pinned_descriptor = server._socket_descriptor
    identity = server._socket_identity
    assert pinned_descriptor is not None
    assert identity is not None
    real_move = file_io.atomic_move_no_replace

    def observe_claim(
        source: Path,
        destination: Path,
        *,
        expected_identity: tuple[int, int],
        expected_file_type: int = stat.S_IFREG,
        expected_fingerprint: file_io.FileFingerprint | None = None,
        pinned_source: file_io.PinnedPath | None = None,
        externally_pinned: bool = False,
    ) -> None:
        pinned = os.fstat(pinned_descriptor)
        assert stat.S_ISSOCK(pinned.st_mode)
        assert (pinned.st_dev, pinned.st_ino) == identity == expected_identity
        assert expected_file_type == stat.S_IFSOCK
        real_move(
            source,
            destination,
            expected_identity=expected_identity,
            expected_file_type=expected_file_type,
            expected_fingerprint=expected_fingerprint,
            pinned_source=pinned_source,
            externally_pinned=externally_pinned,
        )

    monkeypatch.setattr(file_io, "atomic_move_no_replace", observe_claim)

    server.stop()

    with pytest.raises(OSError):
        os.fstat(pinned_descriptor)
    assert not path.exists()


def test_start_failure_before_path_proof_preserves_the_public_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unpinned pathname is never inferred to be the listener we created."""
    from wall_in_one.control.server import SocketServer

    path = tmp_path / "wall-in-one.sock"
    server = SocketServer(_StubCommands(), path)
    real_open = os.open

    def fail_bound_socket_pin(
        candidate: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if os.fsdecode(candidate) == os.fsdecode(path) and flags & os.O_PATH and path.exists():
            raise PermissionError("injected O_PATH failure")
        return real_open(candidate, flags, mode, dir_fd=dir_fd)

    def refuse_unproven_removal(*_arguments: object, **_keywords: object) -> None:
        raise AssertionError("an unproven public socket must not be removed")

    monkeypatch.setattr(os, "open", fail_bound_socket_pin)
    monkeypatch.setattr(SocketServer, "_remove_exact_socket", refuse_unproven_removal)

    with pytest.raises(RuntimeError, match="injected O_PATH failure"):
        server.start()

    assert server._service is None
    assert path.is_socket()
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with pytest.raises(ConnectionRefusedError):
            probe.connect(str(path))
    finally:
        probe.close()


def test_start_never_adopts_a_socket_substituted_immediately_after_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listener proof binds ownership to reachability, not the first lstat."""
    from wall_in_one.control.server import SocketServer

    path = tmp_path / "wall-in-one.sock"
    server = SocketServer(_StubCommands(), path)
    lstat = Path.lstat
    replacement: socket.socket | None = None
    replacement_identity: tuple[int, int] | None = None
    replaced = False

    def replace_before_first_bound_inspection(candidate: Path) -> os.stat_result:
        nonlocal replaced, replacement, replacement_identity
        if candidate == path and not replaced:
            replaced = True
            candidate.unlink()
            replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            replacement.bind(str(candidate))
            replacement.listen(1)
            candidate.chmod(0o600)
            status = lstat(candidate)
            replacement_identity = status.st_dev, status.st_ino
        return lstat(candidate)

    monkeypatch.setattr(Path, "lstat", replace_before_first_bound_inspection)

    try:
        with pytest.raises(RuntimeError, match="does not reach the socket just bound"):
            server.start()

        assert replaced
        assert replacement is not None
        assert replacement_identity is not None
        assert path.is_socket()
        assert file_io.path_identity(path) == replacement_identity
        assert server._socket_identity is None
        assert server._socket_descriptor is None
        assert server._service is None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            probe.connect(str(path))
    finally:
        server.stop()
        if replacement is not None:
            replacement.close()


def test_only_one_server_owns_a_reachable_control_path(tmp_path: Path) -> None:
    from gi.repository import GLib

    from wall_in_one.control.server import SocketServer

    path = tmp_path / "wall-in-one.sock"
    owner = SocketServer(_StubCommands(), path)
    loser = SocketServer(_StubCommands(), path)
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    owner.start()
    try:
        with pytest.raises(RuntimeError, match="already owns"):
            loser.start()

        assert path.is_socket()
        peer.connect(str(path))
        peer.sendall(Request("status").encode())
        peer.setblocking(False)
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 2
        payload = bytearray()
        while b"\n" not in payload and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            try:
                chunk = peer.recv(4096)
            except BlockingIOError:
                time.sleep(0.001)
                continue
            if not chunk:
                break
            payload.extend(chunk)
        assert Response.decode(bytes(payload)) == Response.success("status")
    finally:
        peer.close()
        loser.stop()
        owner.stop()

    lock = path.with_name(f"{path.name}.lock")
    assert lock.is_file()
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_a_bound_socket_is_private_at_birth_and_restores_the_process_umask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Socket creation never exposes a permissive pathname, even briefly."""
    from wall_in_one.control.server import SocketServer

    def refuse_path_chmod(*_arguments: object, **_keywords: object) -> None:
        raise AssertionError("the bound socket pathname must never be chmodded")

    monkeypatch.setattr("os.chmod", refuse_path_chmod)
    server = SocketServer(_StubCommands(), tmp_path / "w.sock")
    original_umask = os.umask(0o027)
    try:
        server.start()
        restored = os.umask(original_umask)
        assert restored == 0o027
    finally:
        os.umask(original_umask)

    try:
        assert server.path.is_socket()
        assert stat.S_IMODE(server.path.lstat().st_mode) == 0o600
    finally:
        server.stop()


def test_bound_socket_start_never_chmods_a_replacement_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no pathname permission operation for a symlink to exploit."""
    from wall_in_one.control.server import SocketServer

    path = tmp_path / "chmod-race.sock"
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("keep me", encoding="utf-8")
    sentinel.chmod(0o640)
    real_chmod = os.chmod
    chmod_called = False

    def replace_before_path_chmod(
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes], mode: int
    ) -> None:
        nonlocal chmod_called
        chmod_called = True
        path.unlink()
        path.symlink_to(sentinel)
        real_chmod(target, mode)

    monkeypatch.setattr(os, "chmod", replace_before_path_chmod)
    server = SocketServer(_StubCommands(), path)
    try:
        server.start()
        assert not chmod_called
        assert sentinel.read_text(encoding="utf-8") == "keep me"
        assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640
    finally:
        server.stop()

    assert sentinel.read_text(encoding="utf-8") == "keep me"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o640


def test_an_oversized_unterminated_socket_request_is_bounded(tmp_path: Path) -> None:
    """The server must reject before a line delimiter, not buffer forever."""
    from gi.repository import GLib

    from wall_in_one.control.server import SocketServer

    server = SocketServer(_StubCommands(), tmp_path / "bounded.sock")
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.start()
    try:
        peer.connect(str(server.path))
        peer.sendall(b"x" * (MAX_MESSAGE_BYTES + 1))
        peer.setblocking(False)
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 3
        payload = bytearray()
        while b"\n" not in payload and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            try:
                chunk = peer.recv(4096)
            except BlockingIOError:
                time.sleep(0.001)
                continue
            if not chunk:
                break
            payload.extend(chunk)
        response = Response.decode(bytes(payload))
        assert not response.ok
        assert "message size limit" in response.message
    finally:
        peer.close()
        server.stop()


def test_a_silent_client_is_closed_at_the_framing_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gi.repository import GLib

    from wall_in_one.control import server as server_module

    monkeypatch.setattr(server_module, "READ_DEADLINE_MILLISECONDS", 20)
    server = server_module.SocketServer(_StubCommands(), tmp_path / "silent.sock")
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.start()
    try:
        peer.connect(str(server.path))
        peer.setblocking(False)
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 2
        payload = bytearray()
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            try:
                chunk = peer.recv(4096)
            except BlockingIOError:
                time.sleep(0.001)
                continue
            if not chunk:
                break
            payload.extend(chunk)
        response = Response.decode(bytes(payload))
        assert not response.ok
        assert response.message == "request framing timed out"
        assert server._connections == {}
        assert server._read_deadlines == {}
    finally:
        peer.close()
        server.stop()


def test_a_request_delivered_after_the_framing_deadline_is_never_dispatched(
    tmp_path: Path,
) -> None:
    from gi.repository import GLib

    from wall_in_one.control.server import SocketServer

    commands = _StubCommands()
    server = SocketServer(commands, tmp_path / "late-frame.sock")
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    context = GLib.MainContext.default()
    server.start()
    try:
        peer.connect(str(server.path))
        deadline = time.monotonic() + 1
        while not server._connections and time.monotonic() < deadline:
            context.iteration(False)
        assert len(server._connections) == 1
        key = next(iter(server._connections))

        # Deterministically order the timeout before the pending async read,
        # then make a valid side-effecting request available to that read.
        GLib.source_remove(server._read_deadlines[key])
        assert server._on_read_deadline(key) is False
        peer.sendall(Request("next").encode())

        deadline = time.monotonic() + 1
        while server._connections and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.001)
        peer.settimeout(0.5)
        response = Response.decode(peer.recv(MAX_MESSAGE_BYTES))
        assert response.message == "request framing timed out"
        assert commands.calls == []
        assert server._read_cancellables == {}
    finally:
        peer.close()
        server.stop()


def test_excess_silent_clients_are_closed_without_exceeding_the_cap(tmp_path: Path) -> None:
    from gi.repository import GLib

    from wall_in_one.control.server import MAX_ACTIVE_CONNECTIONS, SocketServer

    server = SocketServer(_StubCommands(), tmp_path / "busy.sock")
    peers: list[socket.socket] = []
    context = GLib.MainContext.default()
    server.start()
    try:
        for _ in range(MAX_ACTIVE_CONNECTIONS):
            peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            peers.append(peer)
            peer.connect(str(server.path))
            deadline = time.monotonic() + 1
            while len(server._connections) < len(peers) and time.monotonic() < deadline:
                context.iteration(False)
            assert len(server._connections) == len(peers)

        excess = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        peers.append(excess)
        excess.connect(str(server.path))
        excess.setblocking(False)
        deadline = time.monotonic() + 2
        closed = False
        while not closed and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            try:
                chunk = excess.recv(4096)
            except BlockingIOError:
                time.sleep(0.001)
                continue
            closed = not chunk
        assert closed
        assert len(server._connections) == MAX_ACTIVE_CONNECTIONS
    finally:
        for peer in peers:
            peer.close()
        server.stop()


def test_a_complete_deferred_request_outlives_the_read_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gi.repository import GLib

    from wall_in_one.control import server as server_module

    pending: list[Reply] = []

    class Slow(_StubCommands):
        def search(self, value: str | None) -> Outcome:
            return Deferred(start=pending.append)

    monkeypatch.setattr(server_module, "READ_DEADLINE_MILLISECONDS", 20)
    server = server_module.SocketServer(Slow(), tmp_path / "slow-framed.sock")
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    context = GLib.MainContext.default()
    server.start()
    try:
        peer.connect(str(server.path))
        peer.sendall(Request("search", "sky").encode())
        deadline = time.monotonic() + 2
        while not pending and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.001)
        assert pending
        assert server._read_deadlines == {}

        # Keep dispatching the context for several expired read-deadline
        # intervals. The complete request remains owned until its worker reply.
        deadline = time.monotonic() + 0.1
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.001)
        assert len(server._connections) == 1

        pending[0](Response.success("late but valid"))
        peer.settimeout(0.5)
        assert Response.decode(peer.recv(MAX_MESSAGE_BYTES)) == Response.success("late but valid")
        deadline = time.monotonic() + 0.5
        while server._connections and time.monotonic() < deadline:
            context.iteration(False)
        assert server._connections == {}
    finally:
        peer.close()
        server.stop()


def test_a_nonreading_client_cannot_block_gtk_reply_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gi.repository import Gio, GLib

    from wall_in_one.control import server as server_module

    class Large(_StubCommands):
        def list_library(self, value: str | None) -> Response:
            return Response.success("x" * (MAX_MESSAGE_BYTES - 128))

    monkeypatch.setattr(server_module, "WRITE_DEADLINE_MILLISECONDS", 80)
    server = server_module.SocketServer(Large(), tmp_path / "nonreader.sock")
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    context = GLib.MainContext.default()
    beats = 0

    def heartbeat() -> bool:
        nonlocal beats
        beats += 1
        return True

    heartbeat_source = GLib.timeout_add(5, heartbeat)
    server.start()
    try:
        peer.connect(str(server.path))
        deadline = time.monotonic() + 1
        while not server._connections and time.monotonic() < deadline:
            context.iteration(False)
        assert len(server._connections) == 1
        accepted = next(iter(server._connections.values()))
        assert isinstance(accepted, Gio.SocketConnection)
        # Force a protocol-valid 64 KiB reply above this accepted socket's
        # send buffer, then deliberately never read it from `peer`.
        assert accepted.get_socket().set_option(socket.SOL_SOCKET, socket.SO_SNDBUF, 1_024)
        peer.sendall(Request("list").encode())

        deadline = time.monotonic() + 1
        while not server._write_deadlines and time.monotonic() < deadline:
            context.iteration(False)
        assert server._write_deadlines

        deadline = time.monotonic() + 0.5
        while server._connections and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.001)
        assert beats >= 5, "the GTK context must keep dispatching while the write is blocked"
        assert server._connections == {}
        assert server._write_deadlines == {}
        assert server._write_cancellables == {}
    finally:
        GLib.source_remove(heartbeat_source)
        peer.close()
        server.stop()


def test_stop_cancels_an_inflight_async_reply(tmp_path: Path) -> None:
    from gi.repository import Gio, GLib

    from wall_in_one.control.server import SocketServer

    server = SocketServer(_StubCommands(), tmp_path / "stalled-on-stop.sock")
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    context = GLib.MainContext.default()
    server.start()
    try:
        peer.connect(str(server.path))
        deadline = time.monotonic() + 1
        while not server._connections and time.monotonic() < deadline:
            context.iteration(False)
        accepted = next(iter(server._connections.values()))
        assert isinstance(accepted, Gio.SocketConnection)
        assert accepted.get_socket().set_option(socket.SOL_SOCKET, socket.SO_SNDBUF, 1_024)

        server._answer(accepted, Response.success("x" * (MAX_MESSAGE_BYTES - 128)))
        assert server._write_deadlines
        server.stop()

        assert server._connections == {}
        assert server._read_cancellables == {}
        assert server._write_deadlines == {}
        assert server._write_cancellables == {}
        # Let Gio deliver its cancellation callback; it must be harmless after
        # ownership was cleared synchronously by stop().
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.001)
        assert server._connections == {}
    finally:
        peer.close()
        server.stop()


def test_stop_closes_an_accepted_deferred_connection_immediately(tmp_path: Path) -> None:
    """Suppressed late work must not leave its client waiting for a peer timeout."""
    from gi.repository import GLib

    from wall_in_one.control.server import SocketServer

    pending: list[Reply] = []

    class Slow(_StubCommands):
        def search(self, value: str | None) -> Outcome:
            return Deferred(start=pending.append)

    server = SocketServer(Slow(), tmp_path / "deferred.sock")
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.start()
    try:
        peer.connect(str(server.path))
        peer.sendall(Request("search", "sky").encode())
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 2
        while not pending and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.001)
        assert pending
        assert len(server._connections) == 1

        server.stop()

        peer.settimeout(0.5)
        assert peer.recv(1) == b""
        assert server._connections == {}
        # A worker which notices shutdown later may still invoke its guarded
        # callback. The already-closed connection remains harmless.
        pending[0](Response.success("too late"))
    finally:
        peer.close()
        server.stop()


def test_a_normal_socket_answer_releases_the_accepted_connection(tmp_path: Path) -> None:
    from gi.repository import GLib

    from wall_in_one.control.server import SocketServer

    server = SocketServer(_StubCommands(), tmp_path / "answered.sock")
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.start()
    try:
        peer.connect(str(server.path))
        peer.sendall(Request("status").encode())
        peer.setblocking(False)
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 2
        payload = bytearray()
        while b"\n" not in payload and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            try:
                chunk = peer.recv(4096)
            except BlockingIOError:
                time.sleep(0.001)
                continue
            if not chunk:
                break
            payload.extend(chunk)
        assert Response.decode(bytes(payload)) == Response.success("status")
        assert server._connections == {}
    finally:
        peer.close()
        server.stop()


# -- pairings over the socket ---------------------------------------------


def test_a_path_with_spaces_and_a_value_split_correctly() -> None:
    """Split from the right: the left side is a path and this machine's own
    library lives under a directory with a space in its name."""
    path, value = parse_pair("/home/me/customization stuff/a.png builtin:Nord", verb="p")
    assert (path, value) == ("/home/me/customization stuff/a.png", "builtin:Nord")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "/home/me/video walls/clip.mp4 :: /home/me/still walls/night sky.png",
            ("/home/me/video walls/clip.mp4", "/home/me/still walls/night sky.png"),
        ),
        (
            "/home/me/walls/night.png :: community:Tokyo Night",
            ("/home/me/walls/night.png", "community:Tokyo Night"),
        ),
    ],
)
def test_the_explicit_separator_supports_spaces_in_both_values(
    raw: str, expected: tuple[str, str]
) -> None:
    assert parse_pair(raw, verb="pairing") == expected


def test_more_than_one_explicit_separator_is_refused() -> None:
    with pytest.raises(ValueError, match="one :: separator"):
        parse_pair("/one :: /two :: /three", verb="still")


@pytest.mark.parametrize("raw", ["", "   ", "/only/a/path", "value-only "])
def test_a_pair_missing_half_of_itself_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_pair(raw, verb="palette")


def test_a_pairing_reads_back_as_rows(sandbox: Path, applied: list[Path]) -> None:
    wallpaper = _wallpaper("aurora")
    commands, _app = _commands(sandbox, [wallpaper])
    response = commands.show_pairing(str(wallpaper.path))
    assert response.ok
    assert "# fields: field, value" in response.message
    assert f"still\t{wallpaper.path}" in response.message
    assert "palette\tadaptive" in response.message


def test_choosing_a_still_over_the_socket_sticks(sandbox: Path, applied: list[Path]) -> None:
    clip = _wallpaper("clip.mp4", kind=Kind.VIDEO)
    chosen = sandbox / "chosen.png"
    chosen.write_bytes(b"\x89PNG\r\n\x1a\n")
    commands, app = _commands(sandbox, [clip, _on_disk(chosen, Ownership.USER)])

    response = _immediate(commands.set_still(f"{clip.path} {chosen}"))

    assert response.ok
    assert app.session.pairings.resolve(clip, ()).still == chosen


def test_a_still_that_is_not_there_is_refused(sandbox: Path, applied: list[Path]) -> None:
    """A record naming a picture that does not exist is a record that does
    nothing, and the caller would have no way to know."""
    wallpaper = _wallpaper("aurora")
    commands, _app = _commands(sandbox, [wallpaper])
    with pytest.raises(ValueError, match="not an indexed library item"):
        commands.set_still(f"{wallpaper.path} {sandbox / 'nowhere.png'}")


def test_an_existing_but_unindexed_still_is_refused_with_import_advice(
    sandbox: Path, applied: list[Path]
) -> None:
    wallpaper = _wallpaper("aurora")
    commands, app = _commands(sandbox, [wallpaper])
    outside = sandbox / "outside" / "chosen.png"
    outside.parent.mkdir()
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")

    with pytest.raises(ValueError) as caught:
        commands.set_still(f"{wallpaper.path} {outside}")

    message = str(caught.value)
    assert "not an indexed library item" in message
    assert "configured library folder" in message
    assert "add its folder in Settings" in message
    assert app.session.pairings.get(pairings.Identity.of(wallpaper)) is None


def test_an_indexed_video_cannot_be_chosen_as_a_representative_still(
    sandbox: Path, applied: list[Path]
) -> None:
    wallpaper = _wallpaper("aurora")
    candidate = _wallpaper("loop", Kind.VIDEO, root=str(sandbox))
    commands, app = _commands(sandbox, [wallpaper, candidate])

    with pytest.raises(ValueError, match="indexed as video, not as a still image"):
        commands.set_still(f"{wallpaper.path} {candidate.path}")

    assert app.session.pairings.get(pairings.Identity.of(wallpaper)) is None


def test_the_word_default_stops_choosing_a_still(sandbox: Path, applied: list[Path]) -> None:
    clip = _wallpaper("clip.mp4", kind=Kind.VIDEO)
    chosen = sandbox / "chosen.png"
    chosen.write_bytes(b"\x89PNG\r\n\x1a\n")
    commands, app = _commands(sandbox, [clip, _on_disk(chosen, Ownership.USER)])
    commands.set_still(f"{clip.path} {chosen}")

    commands.set_still(f"{clip.path} default")

    assert app.session.pairings.resolve(clip, ()).still is None


def test_a_palette_policy_is_stored(sandbox: Path, applied: list[Path]) -> None:
    wallpaper = _wallpaper("aurora")
    commands, app = _commands(sandbox, [wallpaper])

    assert _immediate(commands.set_palette(f"{wallpaper.path} builtin:Nord")).ok

    policy = app.session.pairings.resolve(wallpaper, ()).palette
    assert (policy.kind, policy.name) == ("builtin", "Nord")
    assert app.repaired == [wallpaper.path], "the window has to be told"


def test_a_policy_that_would_not_survive_a_round_trip_is_refused(
    sandbox: Path, applied: list[Path]
) -> None:
    """`decode` is deliberately forgiving, so the verb has to be the strict
    one: silently storing `adaptive` for a typo would be worse than refusing."""
    wallpaper = _wallpaper("aurora")
    commands, _app = _commands(sandbox, [wallpaper])
    with pytest.raises(ValueError):
        commands.set_palette(f"{wallpaper.path}   ")


def test_resetting_forgets_every_choice(sandbox: Path, applied: list[Path]) -> None:
    wallpaper = _wallpaper("aurora")
    commands, app = _commands(sandbox, [wallpaper])
    commands.set_palette(f"{wallpaper.path} builtin:Nord")

    assert _immediate(commands.reset_pairing(str(wallpaper.path))).ok

    assert not app.session.pairings.resolve(wallpaper, ()).customized


def test_resetting_something_untouched_says_so(sandbox: Path, applied: list[Path]) -> None:
    wallpaper = _wallpaper("aurora")
    commands, _app = _commands(sandbox, [wallpaper])
    assert "nothing customized" in _immediate(commands.reset_pairing(str(wallpaper.path))).message


def test_a_pairing_verb_refuses_a_path_outside_the_library(
    sandbox: Path, applied: list[Path]
) -> None:
    commands, _app = _commands(sandbox, [_wallpaper("aurora")])
    with pytest.raises(UnknownWallpaperError):
        commands.show_pairing("/etc/passwd")


# -- playlists over the socket --------------------------------------------


def test_a_playlist_reference_splits_from_the_left() -> None:
    """The mirror of `parse_pair`: here the short side is on the left."""
    name, rest = parse_pair_from_left("Evening /w/some path/a.png", verb="playlist-add")
    assert (name, rest) == ("Evening", "/w/some path/a.png")


@pytest.mark.parametrize("raw", ["", "   ", "Evening", " /only/a/path"])
def test_a_playlist_pair_missing_half_of_itself_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_pair_from_left(raw, verb="playlist-add")


def test_playlists_list_as_rows_marking_the_active_one(sandbox: Path, applied: list[Path]) -> None:
    commands, app = _commands(sandbox, [_wallpaper("aurora")])
    commands.make_playlist("Evening")
    commands.make_playlist("Morning")
    made = app.session.playlists.find("Evening")
    app.session.update_settings(replace(app.session.settings, active_playlist=made.id))

    message = commands.list_playlists(None).message

    assert "# fields: id, name, entries, active" in message
    evening = app.session.playlists.find("Evening")
    morning = app.session.playlists.find("Morning")
    assert f"{evening.id}\tEvening\t0\tyes" in message
    assert f"{morning.id}\tMorning\t0\tno" in message


def test_a_named_playlist_lists_its_entries_with_their_ids(
    sandbox: Path, applied: list[Path]
) -> None:
    wallpaper = _wallpaper("aurora")
    commands, app = _commands(sandbox, [wallpaper])
    commands.make_playlist("Evening")
    commands.add_to_playlist(f"Evening {wallpaper.path}")

    message = commands.list_playlists("Evening").message

    assert "# fields: entry, present, path" in message
    entry = app.session.playlists.find("Evening").entries[0]
    assert f"{entry.id}\tyes\t{wallpaper.path}" in message


def test_an_entry_the_library_lost_is_shown_as_absent(sandbox: Path, applied: list[Path]) -> None:
    """An unmounted drive is not a deletion, so the row stays and says so."""
    wallpaper = _wallpaper("aurora")
    commands, app = _commands(sandbox, [wallpaper])
    commands.make_playlist("Evening")
    app.session.playlists.add("Evening", Path("/w/unmounted.png"))

    assert "\tno\t/w/unmounted.png" in commands.list_playlists("Evening").message


def test_adding_something_outside_the_library_is_refused(
    sandbox: Path, applied: list[Path]
) -> None:
    commands, _app = _commands(sandbox, [_wallpaper("aurora")])
    commands.make_playlist("Evening")
    with pytest.raises(UnknownWallpaperError):
        commands.add_to_playlist("Evening /etc/passwd")


def test_using_a_playlist_switches_playback_now(sandbox: Path, applied: list[Path]) -> None:
    wallpaper = _wallpaper("aurora")
    commands, app = _commands(sandbox, [wallpaper])
    commands.make_playlist("Evening")
    app.session.playlists.add("Evening", wallpaper.path)

    assert _immediate(commands.use_playlist("Evening")).ok
    assert app.session.manual_playlist == app.session.playlists.find("Evening").id
    assert applied[-1] == wallpaper.path


def test_using_none_resumes_schedule_control(sandbox: Path, applied: list[Path]) -> None:
    commands, app = _commands(sandbox, [_wallpaper("aurora")])
    manual = app.session.playlists.set_singleton("manual", "Manual", Path("/w/aurora.png"))
    app.session.use_playlist(manual.id)
    assert _immediate(commands.use_playlist("none")).ok
    assert app.session.manual_playlist is None


def test_using_a_playlist_that_is_not_there_says_which(sandbox: Path, applied: list[Path]) -> None:
    commands, _app = _commands(sandbox, [_wallpaper("aurora")])
    with pytest.raises(PlaylistError) as caught:
        commands.use_playlist("nope")
    assert caught.value.kind == "no-such-playlist"


def test_display_listing_leads_with_the_reusable_connector(
    sandbox: Path,
    applied: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands, _app = _commands(sandbox, [_wallpaper("aurora")])
    monkeypatch.setattr(
        "wall_in_one.ui.app.outputs.discover",
        lambda: (Output("DP-2", make="Acme", model="Wide", width=2560, height=1440),),
    )

    outcome = commands.list_displays()
    assert isinstance(outcome, Deferred)
    replies: list[Response] = []
    outcome.start(replies.append)
    message = replies[0].message

    assert "# fields: connector, playlist, description" in message
    assert "DP-2\t(default)\tDP-2 (Acme Wide, 2560x1440)" in message


def test_editing_a_playlist_tells_the_window(sandbox: Path, applied: list[Path]) -> None:
    """Otherwise the rotation and the list disagree until something else
    happens to rebuild it."""
    wallpaper = _wallpaper("aurora")
    commands, app = _commands(sandbox, [wallpaper])
    commands.make_playlist("Evening")
    before = app.relisted
    commands.add_to_playlist(f"Evening {wallpaper.path}")
    assert app.relisted == before + 1


def test_removing_an_entry_by_its_id(sandbox: Path, applied: list[Path]) -> None:
    wallpaper = _wallpaper("aurora")
    commands, app = _commands(sandbox, [wallpaper])
    commands.make_playlist("Evening")
    commands.add_to_playlist(f"Evening {wallpaper.path}")
    entry = app.session.playlists.find("Evening").entries[0]

    assert _immediate(commands.remove_from_playlist(f"Evening {entry.id}")).ok

    assert len(app.session.playlists.find("Evening")) == 0


# -- schedules over the socket --------------------------------------------


def test_a_rule_is_written_as_keywords() -> None:
    """Four optional fields in a fixed order is a syntax nobody remembers."""
    playlist, options = parse_rule("Evening days=sat,sun from=22:00 to=06:00")
    assert playlist == "Evening"
    assert options == {"days": "sat,sun", "from": "22:00", "to": "06:00"}


def test_a_rule_needs_at_least_a_playlist() -> None:
    with pytest.raises(ValueError):
        parse_rule("  ")


def test_an_unknown_keyword_is_refused_rather_than_ignored() -> None:
    """Silently dropping `weekdays=` would schedule something for every day."""
    with pytest.raises(ValueError):
        parse_rule("Evening weekdays=sat")


def test_scheduling_a_playlist_stores_a_rule(sandbox: Path, applied: list[Path]) -> None:
    commands, app = _commands(sandbox, [_wallpaper("aurora")])
    commands.make_playlist("Evening")

    assert _immediate(commands.add_schedule_rule("Evening days=sat,sun")).ok

    rule = app.session.schedules.rules[0]
    assert rule.playlist == app.session.playlists.find("Evening").id
    assert rule.weekdays == frozenset({5, 6})
    assert app.rescheduled == 1


def test_scheduling_something_that_is_not_a_playlist_says_so(
    sandbox: Path, applied: list[Path]
) -> None:
    commands, _app = _commands(sandbox, [_wallpaper("aurora")])
    with pytest.raises(PlaylistError):
        commands.add_schedule_rule("Nope days=sat")


def test_the_schedule_lists_its_rules(sandbox: Path, applied: list[Path]) -> None:
    commands, _app = _commands(sandbox, [_wallpaper("aurora")])
    commands.make_playlist("Evening")
    commands.add_schedule_rule("Evening days=sat,sun")
    message = commands.show_schedule().message
    assert "# fields: rule, playlist, when, enabled, in-force" in message
    assert "sat,sun" in message


def test_a_rule_can_be_removed_by_its_id(sandbox: Path, applied: list[Path]) -> None:
    commands, app = _commands(sandbox, [_wallpaper("aurora")])
    commands.make_playlist("Evening")
    commands.add_schedule_rule("Evening")
    rule = app.session.schedules.rules[0]

    assert _immediate(commands.drop_schedule_rule(rule.id)).ok

    assert app.session.schedules.rules == ()


def test_removing_a_rule_that_is_not_there_says_so(sandbox: Path, applied: list[Path]) -> None:
    commands, _app = _commands(sandbox, [_wallpaper("aurora")])
    with pytest.raises(ValueError):
        commands.drop_schedule_rule("nope")


def test_deleting_a_playlist_takes_its_schedule_rules(sandbox: Path, applied: list[Path]) -> None:
    """A rule pointing at a playlist that is gone reads as the schedule
    silently not working."""
    commands, app = _commands(sandbox, [_wallpaper("aurora")])
    commands.make_playlist("Evening")
    commands.add_schedule_rule("Evening")

    commands.drop_playlist("Evening")

    assert app.session.schedules.rules == ()
