from __future__ import annotations

from pathlib import Path

import pytest

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
    build_verb_table,
    dispatch,
    failed,
    handle,
    parse_download,
    parse_search,
    parse_toggle,
    render_providers,
    render_search,
)
from wall_in_one.library.model import Kind
from wall_in_one.providers.base import ProviderError, SearchResult, WallpaperCandidate
from wall_in_one.providers.registry import ProviderInfo


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

    def report_status(self) -> Response:
        return self._record("status")

    def list_providers(self) -> Response:
        return self._record("providers")

    def search(self, value: str | None) -> Outcome:
        return self._record("search", value)

    def download(self, value: str | None) -> Outcome:
        return self._record("download", value)

    def quit(self) -> Response:
        return self._record("quit")


def test_verb_table_covers_the_documented_cli_surface() -> None:
    from wall_in_one.cli import CTL_VERBS

    verbs = build_verb_table(_StubCommands())
    assert set(verbs) == set(CTL_VERBS)


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
    """A five-second ceiling would time out every download and most searches."""
    from wall_in_one.control import client

    assert client.TIMEOUTS["search"] > client.TIMEOUT
    assert client.TIMEOUTS["download"] > client.TIMEOUTS["search"]
    assert set(client.TIMEOUTS) <= set(build_verb_table(_StubCommands()))


def test_the_cli_joins_its_words_back_into_one_argument() -> None:
    from wall_in_one.cli import _build_parser

    options = _build_parser().parse_args(["ctl", "search", "wallhaven", "aurora", "borealis"])
    assert options.argument == ["wallhaven", "aurora", "borealis"]


# -- where a download lands -----------------------------------------------


def test_a_control_download_lands_where_the_dialog_puts_one(tmp_path: Path) -> None:
    """Both surfaces take the first configured root, or leave the browser to decide."""
    from wall_in_one import config
    from wall_in_one.ui.app import download_root

    first, second = tmp_path / "one", tmp_path / "two"
    assert download_root(config.Settings(roots=(first, second))) == first
    assert download_root(config.Settings()) is None
