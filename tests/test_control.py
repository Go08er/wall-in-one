from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from wall_in_one import config
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
from wall_in_one.library import favourites
from wall_in_one.library.filter import Kinds, Query
from wall_in_one.library.manage import ManageError
from wall_in_one.library.model import Kind, Library, MediaItem, Ownership
from wall_in_one.library.playlists import PlaylistError
from wall_in_one.providers.base import ProviderError, SearchResult, WallpaperCandidate
from wall_in_one.providers.registry import ProviderInfo
from wall_in_one.session import Session
from wall_in_one.wallpaper.applier import Applied, Applier

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
    """Six hundred wallpapers with long names do not fit in a 64 KB frame, and
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
    (directory / MANAGED_MARKER).write_text("{}", encoding="utf-8")
    path = directory / name
    path.write_bytes(b"\xff\xd8\xff" + b"0" * 32)
    if sidecar:
        path.with_name(path.name + ".wallhaven.json").write_text("{}", encoding="utf-8")
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


def test_a_downloaded_wallpaper_is_deleted_and_the_reply_says_which(sandbox: Path) -> None:
    path = _downloaded(sandbox)
    message = remove_wallpaper(_on_disk(path, Ownership.MANAGED), (sandbox,))

    assert not path.exists()
    assert message == "removed aurora.jpg and 1 file beside it - deleted, which cannot be undone"


def test_a_wallpaper_of_the_users_own_is_trashed_and_the_reply_says_which(
    sandbox: Path, tmp_path: Path
) -> None:
    """Two very different things to have done to somebody's file, and only one
    of them can be undone, so the sentence may not be the same either."""
    path = _their_own(sandbox)
    message = remove_wallpaper(_on_disk(path, Ownership.USER), (sandbox,))

    assert not path.exists()
    landed = tmp_path / "data" / "Trash" / "files" / "holiday.png"
    assert landed.is_file()
    assert message == f"holiday.png moved to the trash - {landed}"


def test_a_stale_claim_of_ownership_still_does_not_delete_anything(sandbox: Path) -> None:
    """The scan may be minutes old. `library.manage` re-derives ownership from
    disk, and the socket has no confirmation dialogue to fall back on."""
    path = _downloaded(sandbox, sidecar=False)

    with pytest.raises(ManageError) as caught:
        remove_wallpaper(_on_disk(path, Ownership.MANAGED), (sandbox,))

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
        self.session = session
        self.restarred = 0
        self.forgotten: list[Path] = []
        self.repaired: list[Path] = []
        self.relisted = 0
        self.rescheduled = 0
        self.settings_written: list[dict[str, object]] = []

    def apply(self, action: Callable[[], Applied]) -> Response:
        return Response.success(action().describe())

    def favourites_changed(self) -> None:
        self.restarred += 1

    def forget(self, path: Path) -> None:
        self.forgotten.append(path)

    def pairing_changed(self, item: MediaItem) -> None:
        self.repaired.append(item.path)

    def playlists_changed(self) -> None:
        self.relisted += 1

    def schedule_edited(self) -> None:
        self.rescheduled += 1

    def update_settings(self, **changes: object) -> None:
        self.settings_written.append(changes)


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

    response = commands.add_favourite(str(item.path))

    assert response.message == "aurora.png starred"
    assert app.session.favourites.is_favourite(item.path)
    assert app.restarred == 1


def test_starring_one_twice_says_it_was_already_starred(sandbox: Path) -> None:
    item = _wallpaper("aurora")
    commands, _app = _commands(sandbox, [item])
    commands.add_favourite(str(item.path))
    assert commands.add_favourite(str(item.path)).message == "aurora.png was already starred"


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

    response = commands.remove_favourite(str(gone))

    assert response.message == "unmounted.png unstarred"
    assert len(app.session.favourites) == 0
    assert app.restarred == 1


def test_unstarring_something_that_was_never_starred_says_so(sandbox: Path) -> None:
    commands, _app = _commands(sandbox, [])
    assert commands.remove_favourite("/w/aurora.png").message == "aurora.png was not starred"


def test_a_star_that_could_not_be_saved_is_reported_and_still_shown(
    sandbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store keeps the change in memory whatever the disk did, so the window
    has to be told either way; the failure is only about the next launch."""
    item = _wallpaper("aurora")
    commands, app = _commands(sandbox, [item])

    def refuse(*_arguments: object, **_keywords: object) -> Path:
        raise favourites.FavouritesError("local-io", "no space left on device")

    monkeypatch.setattr("wall_in_one.library.favourites.save", refuse)
    response = commands.add_favourite(str(item.path))

    assert (response.ok, response.kind) == (False, "local-io")
    assert app.session.favourites.is_favourite(item.path)
    assert app.restarred == 1


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

    response = commands.remove_wallpaper(str(path))

    assert response.ok
    assert "deleted, which cannot be undone" in response.message
    assert not path.exists()
    # Which is what drops the star and rescans, so no tile outlives the file.
    assert app.forgotten == [path]


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

    response = commands.select_wallpaper(str(second.path))

    assert response.message == "set clip.png"
    assert applied == [second.path]


def test_selecting_something_not_in_the_library_applies_nothing(
    sandbox: Path, applied: list[Path]
) -> None:
    commands, _app = _commands(sandbox, [_wallpaper("aurora")])

    response = handle(build_verb_table(commands), Request("select", "/w/nowhere.png").encode())

    assert isinstance(response, Response)
    assert (response.ok, response.kind) == (False, "not-in-library")
    assert applied == []


# -- the socket itself ----------------------------------------------------


def test_a_socket_path_too_long_to_bind_is_refused_before_anything_is_created(
    tmp_path: Path,
) -> None:
    """AF_UNIX caps the path near 108 bytes, and `Gio.SocketService.add_address`
    does not say so -- it returns having created nothing, and the failure used
    to surface two lines later as the `os.chmod` no window ever came back from.
    """
    from wall_in_one.control.server import SocketServer

    long_enough = tmp_path / ("d" * 120) / "wall-in-one.sock"
    server = SocketServer(_StubCommands(), long_enough)

    with pytest.raises(RuntimeError, match="too long"):
        server.start()

    assert not long_enough.parent.exists()


def test_a_socket_that_cannot_be_secured_leaves_nothing_listening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half-started is worse than not started: the app would report that it has
    no control socket while one sat there readable by the rest of the machine.
    """
    from wall_in_one.control.server import SocketServer

    def refuse(*_arguments: object, **_keywords: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("os.chmod", refuse)
    # Short, because this one really does bind: the address has to fit.
    server = SocketServer(_StubCommands(), tmp_path / "w.sock")

    with pytest.raises(RuntimeError, match="cannot secure"):
        server.start()

    assert not server.path.exists()


# -- pairings over the socket ---------------------------------------------


def test_a_path_with_spaces_and_a_value_split_correctly() -> None:
    """Split from the right: the left side is a path and this machine's own
    library lives under a directory with a space in its name."""
    path, value = parse_pair("/home/me/customization stuff/a.png builtin:Nord", verb="p")
    assert (path, value) == ("/home/me/customization stuff/a.png", "builtin:Nord")


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
    commands, app = _commands(sandbox, [clip])
    chosen = sandbox / "chosen.png"
    chosen.write_bytes(b"\x89PNG\r\n\x1a\n")

    response = commands.set_still(f"{clip.path} {chosen}")

    assert response.ok
    assert app.session.pairings.resolve(clip, ()).still == chosen


def test_a_still_that_is_not_there_is_refused(sandbox: Path, applied: list[Path]) -> None:
    """A record naming a picture that does not exist is a record that does
    nothing, and the caller would have no way to know."""
    wallpaper = _wallpaper("aurora")
    commands, _app = _commands(sandbox, [wallpaper])
    with pytest.raises(ValueError):
        commands.set_still(f"{wallpaper.path} {sandbox / 'nowhere.png'}")


def test_the_word_default_stops_choosing_a_still(sandbox: Path, applied: list[Path]) -> None:
    clip = _wallpaper("clip.mp4", kind=Kind.VIDEO)
    commands, app = _commands(sandbox, [clip])
    chosen = sandbox / "chosen.png"
    chosen.write_bytes(b"\x89PNG\r\n\x1a\n")
    commands.set_still(f"{clip.path} {chosen}")

    commands.set_still(f"{clip.path} default")

    assert app.session.pairings.resolve(clip, ()).still is None


def test_a_palette_policy_is_stored(sandbox: Path, applied: list[Path]) -> None:
    wallpaper = _wallpaper("aurora")
    commands, app = _commands(sandbox, [wallpaper])

    assert commands.set_palette(f"{wallpaper.path} builtin:Nord").ok

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

    assert commands.reset_pairing(str(wallpaper.path)).ok

    assert not app.session.pairings.resolve(wallpaper, ()).customized


def test_resetting_something_untouched_says_so(sandbox: Path, applied: list[Path]) -> None:
    wallpaper = _wallpaper("aurora")
    commands, _app = _commands(sandbox, [wallpaper])
    assert "nothing customized" in commands.reset_pairing(str(wallpaper.path)).message


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

    assert "# fields: name, entries, active" in message
    assert "Evening\t0\tyes" in message
    assert "Morning\t0\tno" in message


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


def test_using_a_playlist_writes_the_setting(sandbox: Path, applied: list[Path]) -> None:
    commands, app = _commands(sandbox, [_wallpaper("aurora")])
    commands.make_playlist("Evening")

    assert commands.use_playlist("Evening").ok

    written = app.settings_written[-1]
    assert written["active_playlist"] == app.session.playlists.find("Evening").id


def test_using_none_goes_back_to_the_whole_library(sandbox: Path, applied: list[Path]) -> None:
    commands, app = _commands(sandbox, [_wallpaper("aurora")])
    assert commands.use_playlist("none").ok
    assert app.settings_written[-1]["active_playlist"] == ""


def test_using_a_playlist_that_is_not_there_says_which(sandbox: Path, applied: list[Path]) -> None:
    commands, _app = _commands(sandbox, [_wallpaper("aurora")])
    with pytest.raises(PlaylistError) as caught:
        commands.use_playlist("nope")
    assert caught.value.kind == "no-such-playlist"


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

    assert commands.remove_from_playlist(f"Evening {entry.id}").ok

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

    assert commands.add_schedule_rule("Evening days=sat,sun").ok

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

    assert commands.drop_schedule_rule(rule.id).ok

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
