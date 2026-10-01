"""The real AppState adapter maps the session and the runtime's status, and nothing else.

No display: `RealAppState` is a GObject over a real `Session` (stores in the
test's own XDG dirs) and a real `RuntimeStatusModel`, with a fake application
behind the `Backend` Protocol that records what the adapter asks for.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar, get_protocol_members

import pytest

from tests.test_next_status_line import BATTERY, two_display_status
from wall_in_one import config, paths, ui_prefs
from wall_in_one.library import pairings, playlists
from wall_in_one.library.model import Kind, Library, MediaItem, Ownership
from wall_in_one.session import QUICK_CHOICE_ID, QUICK_CHOICE_NAME, Session
from wall_in_one.theme import css
from wall_in_one.ui import playback_verbs, runtime_truth
from wall_in_one.ui.next.prefs import UiPrefsKeeper
from wall_in_one.ui.next.real_state import RealAppState, human_size, scheme_name, wallpaper_view
from wall_in_one.ui.next.state import AppState, Reason, reason_text
from wall_in_one.ui.status_model import RuntimeStatusModel

if TYPE_CHECKING:
    # The conformance proof: mypy --strict checks these against every member
    # and signature of the Protocols. Nothing runs at test time.
    from wall_in_one.ui.app import Application
    from wall_in_one.ui.next.library_thumbnails import LibraryThumbnails
    from wall_in_one.ui.next.real_state import Backend
    from wall_in_one.ui.next.thumbs import ThumbnailProvider

    def _adapter_conforms(state: RealAppState) -> AppState:
        return state

    def _application_backs_it(application: Application) -> Backend:
        return application

    def _pictures_conform(provider: LibraryThumbnails) -> ThumbnailProvider:
        return provider


_T = TypeVar("_T")


class FakeApplication:
    """The `Backend` the adapter talks to, recording every request.

    Authoring tasks wait in a queue until `run_authoring`, so a test can see
    that nothing changes before the store does.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._model = RuntimeStatusModel()
        self.unknown_keys: tuple[str, ...] = ()
        self.played: list[tuple[Path, str | None]] = []
        self.reports: list[str] = []
        self.glass: list[css.Glass | None] = []
        self.published = 0
        self.authoring: deque[Callable[[], None]] = deque()
        self.after_favourites: Callable[[], None] = lambda: None

    @property
    def session(self) -> Session:
        return self._session

    @property
    def status_model(self) -> RuntimeStatusModel:
        return self._model

    @property
    def settings(self) -> config.Settings:
        return self._session.settings

    @property
    def settings_unknown_keys(self) -> tuple[str, ...]:
        return self.unknown_keys

    def play_item_async(self, item: MediaItem) -> bool:
        self.played.append((item.path, None))
        return True

    def play_item_on_async(self, item: MediaItem, connector: str) -> bool:
        self.played.append((item.path, connector))
        return True

    def authoring_action_async(
        self,
        work: Callable[[], _T],
        finish: Callable[[_T], None],
        *,
        prepare: Callable[[], Callable[[], _T]] | None = None,
        failure: Callable[[str], None] | None = None,
    ) -> bool:
        def run() -> None:
            try:
                result = (prepare() if prepare is not None else work)()
            except Exception as error:
                if failure is not None:
                    failure(str(error))
                return
            finish(result)

        self.authoring.append(run)
        return True

    def run_authoring(self) -> None:
        while self.authoring:
            self.authoring.popleft()()

    def current_item_for_authoring(self, item: MediaItem) -> MediaItem:
        return item

    def favourites_changed(self) -> None:
        self.published += 1
        self.after_favourites()

    def window_report(self, message: str) -> None:
        self.reports.append(message)

    def set_window_glass(self, glass: css.Glass | None) -> None:
        self.glass.append(glass)


NAMES = ("alpine.png", "city-dusk.png", "lily-pond.png", "rain.mp4")


def _library(root: Path) -> tuple[MediaItem, ...]:
    root.mkdir(parents=True, exist_ok=True)
    items = []
    for index, name in enumerate(NAMES):
        path = root / name
        path.write_bytes(b"x" * 1500 * (index + 1))
        kind = Kind.VIDEO if name.endswith(".mp4") else Kind.STILL
        items.append(
            MediaItem(path=path, kind=kind, size=path.stat().st_size, mtime=1_788_220_800 + index)
        )
    return tuple(items)


@pytest.fixture
def library(tmp_path: Path) -> tuple[MediaItem, ...]:
    return _library(tmp_path / "home" / "Pictures" / "Wallpapers")


def _session(items: tuple[MediaItem, ...]) -> Session:
    root = items[0].path.parent
    session = Session(config.Settings(roots=(root,)), owns_playback=False)
    session.adopt_library(Library(roots=(root,), items=items), reconcile_workshop=False)
    return session


@pytest.fixture
def backend(library: tuple[MediaItem, ...]) -> Iterator[FakeApplication]:
    session = _session(library)
    try:
        yield FakeApplication(session)
    finally:
        session.shutdown()


def _adapter(backend: FakeApplication) -> tuple[RealAppState, UiPrefsKeeper]:
    keeper = UiPrefsKeeper(backend.window_report)
    adapter = RealAppState(backend, keeper)
    backend.after_favourites = adapter.reload
    return adapter, keeper


def _topics(adapter: RealAppState) -> list[str]:
    seen: list[str] = []
    adapter.connect("changed", lambda _state, topic: seen.append(topic))
    return seen


def _status(still: dict[str, Path], **routes: str) -> dict[str, object]:
    """``two_display_status`` with each display's still and route source."""
    status = two_display_status()
    displays = status["displays"]
    assert isinstance(displays, list)
    for record in displays:
        connector = record["connector"]
        record["still"] = str(still[connector])
        record["route_source"] = routes.get(connector, "schedule")
        if record["route_source"] == "manual":
            record["playlist_id"] = f"{playlists.DISPLAY_QUICK_CHOICE_ID_PREFIX}{connector}"
            record["playlist"] = f"Quick choice · {connector}"
            record["schedule_rule_id"] = None
    return status


# -- the Protocol's runtime half --------------------------------------------------


def test_the_real_adapter_defines_every_member_of_the_protocol() -> None:
    for name in get_protocol_members(AppState):
        assert hasattr(RealAppState, name), name


# -- wording ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "text"),
    [
        (Reason("pick"), "Your pick"),
        (Reason("pick", "Evening"), "Evening · your pick"),
        (Reason("schedule", "Evening"), "Evening · from schedule"),
        (Reason("schedule", "Evening", until="18:00"), "Evening · from schedule until 18:00"),
        (Reason("display", "Focus"), "Focus · this display\u2019s playlist"),
        (Reason("default", "All media"), "All media · nothing scheduled"),
    ],
)
def test_the_reason_says_until_only_when_the_runtime_reported_it(reason: Reason, text: str) -> None:
    assert reason_text(reason) == text


def test_sizes_and_scheme_names_read_as_words() -> None:
    assert human_size(6_200_000) == "6.2 MB"
    assert human_size(1500) == "2 KB"
    assert human_size(2_500_000_000) == "2.5 GB"
    assert scheme_name("m3-tonal-spot") == "Tonal Spot"
    assert scheme_name("vibrant") == "Vibrant"


# -- the library --------------------------------------------------------------------


def test_a_media_item_becomes_a_wallpaper_view(library: tuple[MediaItem, ...]) -> None:
    still, video = library[0], library[3]
    plain = wallpaper_view(still, favourite=False, pairing=None, health=pairings.Health())
    assert (plain.id, plain.name, plain.kind, plain.is_moving) == (
        str(still.path),
        "alpine",
        "still",
        False,
    )
    assert plain.folder == "~/Pictures/Wallpapers"
    assert plain.source == "Local"
    assert (plain.color_mode, plain.scheme, plain.palette, plain.theme_mode) == (
        "adaptive",
        None,
        None,
        "keep",
    )
    assert plain.problem == ""

    identity = pairings.Identity.of(video)
    nord = pairings.Pairing(
        identity,
        palette=pairings.PalettePolicy("builtin", "Nord", pairings.Mode.DARK),
        customized=True,
    )
    borked = pairings.Health.borked("renderer rejected this wallpaper", "runtime")
    view = wallpaper_view(video, favourite=True, pairing=nord, health=borked)
    assert view.is_moving and view.kind == "video" and view.favorite
    assert (view.color_mode, view.palette, view.theme_mode) == ("palette", "Nord", "dark")
    assert view.problem == "renderer rejected this wallpaper"

    pinned = pairings.Pairing(
        identity, palette=pairings.PalettePolicy("adaptive", "vibrant"), customized=True
    )
    assert wallpaper_view(video, favourite=False, pairing=pinned, health=borked).scheme == "vibrant"
    kept = pairings.Pairing(identity, palette=pairings.PalettePolicy("keep"), customized=True)
    assert wallpaper_view(video, favourite=False, pairing=kept, health=borked).color_mode == "keep"

    downloaded = MediaItem(
        path=still.path, kind=Kind.STILL, size=1, mtime=1, ownership=Ownership.MANAGED,
        provider="wallhaven",
    )  # fmt: skip
    assert (
        wallpaper_view(downloaded, favourite=False, pairing=None, health=pairings.Health()).source
        == "Wallhaven"
    )
    scene = MediaItem(path=still.path.parent, kind=Kind.SCENE, size=0, mtime=0, scene="123")
    assert wallpaper_view(
        scene, favourite=False, pairing=None, health=pairings.Health()
    ).source == ("Workshop")


def test_the_library_comes_from_the_session(backend: FakeApplication) -> None:
    session = backend.session
    items = session.library.items
    session.favourites.add(items[1].path)
    session.pairings.choose_palette(
        items[2], pairings.PalettePolicy("builtin", "Nord", pairings.Mode.LIGHT)
    )
    session.pairings.mark_borked(items[3], "codec missing", "runtime")
    adapter, _keeper = _adapter(backend)

    assert [view.name for view in adapter.wallpapers] == [
        "alpine",
        "city-dusk",
        "lily-pond",
        "rain",
    ]
    by_name = {view.name: view for view in adapter.wallpapers}
    assert by_name["city-dusk"].favorite and not by_name["alpine"].favorite
    assert (by_name["lily-pond"].color_mode, by_name["lily-pond"].palette) == ("palette", "Nord")
    assert by_name["rain"].problem == "codec missing"
    assert adapter.has_wallpaper(str(items[0].path)) and not adapter.has_wallpaper("/nope")

    # The classic grid's matching and orders, over the whole library.
    assert [s for s, _label in adapter.library_sorts()] == ["name", "newest", "largest"]
    newest = adapter.library_query(kind="all", favorites=False, text="", sort="newest")
    assert [view.name for view in newest] == ["rain", "lily-pond", "city-dusk", "alpine"]
    assert [
        v.name for v in adapter.library_query(kind="video", favorites=False, text="", sort="name")
    ] == ["rain"]
    assert [
        v.name for v in adapter.library_query(kind="all", favorites=True, text="", sort="name")
    ] == ["city-dusk"]
    assert [
        v.name
        for v in adapter.library_query(kind="all", favorites=False, text="DUSK cit", sort="name")
    ] == ["city-dusk"]


def test_playlists_hide_quick_choice_and_end_with_every_wallpaper(
    backend: FakeApplication,
) -> None:
    session = backend.session
    items = session.library.items
    evening = session.playlists.create("Evening")
    session.playlists.add(evening.id, items[1].path)
    session.playlists.add(evening.id, items[2].path)
    session.playlists.set_singleton(QUICK_CHOICE_ID, QUICK_CHOICE_NAME, items[0].path)
    adapter, _keeper = _adapter(backend)

    assert [playlist.name for playlist in adapter.playlists] == ["Evening", "All wallpapers"]
    shown = adapter.playlist(evening.id)
    assert shown.entries == (str(items[1].path), str(items[2].path)) and not shown.automatic
    everything = adapter.playlists[-1]
    assert everything.automatic and len(everything.entries) == len(items)
    assert not adapter.has_playlist(QUICK_CHOICE_ID)


# -- the runtime's status ---------------------------------------------------------------


def test_what_plays_and_why_come_from_the_runtime(backend: FakeApplication) -> None:
    items = backend.session.library.items
    adapter, _keeper = _adapter(backend)
    assert adapter.player().service == "checking" and adapter.current == {}

    stills = {"DP-1": items[1].path, "HDMI-A-1": items[0].path}
    backend.status_model.adopt(_status(stills, **{"HDMI-A-1": "manual"}))

    assert adapter.current == {"DP-1": str(items[1].path), "HDMI-A-1": str(items[0].path)}
    assert [display.connector for display in adapter.displays] == ["DP-1", "HDMI-A-1"]
    assert adapter.display_mode == "independent"
    player = adapter.player()
    assert player.service == "running" and player.playback == "playing"
    assert not player.following_schedule, "a pick on one display offers Resume schedule"
    reasons = {screen.connector: reason_text(screen.reason) for screen in player.screens}
    assert reasons == {"DP-1": "Evening · from schedule", "HDMI-A-1": "Your pick"}
    assert all("until" not in text for text in reasons.values()), "no deadline was reported"
    assert [screen.name for screen in player.screens] == ["city-dusk", "alpine"]
    assert player.rotate and player.timing == ""

    adapter.set_scope("DP-1")
    assert adapter.targets() == ["DP-1"] and adapter.scope_label() == "DP-1"
    assert adapter.player().following_schedule
    assert [screen.connector for screen in adapter.player().screens] == ["DP-1"]


def test_a_repeated_poll_says_nothing(backend: FakeApplication) -> None:
    items = backend.session.library.items
    adapter, _keeper = _adapter(backend)
    stills = {"DP-1": items[1].path, "HDMI-A-1": items[0].path}
    backend.status_model.adopt(_status(stills))
    seen = _topics(adapter)
    backend.status_model.adopt(_status(stills))
    backend.status_model.adopt(_status(stills))
    assert seen == [], "the Library must not rebuild on every two-second poll"

    backend.status_model.adopt(_status({"DP-1": items[2].path, "HDMI-A-1": items[0].path}))
    assert "now" in seen


def test_service_and_power_banners(backend: FakeApplication) -> None:
    items = backend.session.library.items
    adapter, _keeper = _adapter(backend)
    backend.status_model.mark_unavailable(forget=True)
    assert adapter.player().service == "stopped"
    banner = adapter.banner()
    assert banner is not None and "isn\u2019t running" in banner.title

    status = _status({"DP-1": items[0].path, "HDMI-A-1": items[0].path})
    status.update(BATTERY)
    backend.status_model.adopt(status)
    banner = adapter.banner()
    assert banner is not None and banner.title.startswith("On battery")
    backend.status_model.mark_delayed()
    assert adapter.player().notes == ("status delayed",)


# -- the writes ------------------------------------------------------------------------


def test_apply_is_a_quick_choice_and_waits_for_the_runtime(backend: FakeApplication) -> None:
    items = backend.session.library.items
    adapter, _keeper = _adapter(backend)
    stills = {"DP-1": items[1].path, "HDMI-A-1": items[0].path}
    backend.status_model.adopt(_status(stills))
    before = dict(adapter.current)

    adapter.apply(str(items[2].path), "all")
    adapter.apply(str(items[2].path), "HDMI-A-1")
    assert backend.played == [(items[2].path, None), (items[2].path, "HDMI-A-1")]
    assert adapter.current == before, "nothing changes until the runtime says so"

    backend.status_model.adopt(_status({"DP-1": items[2].path, "HDMI-A-1": items[0].path}))
    assert adapter.current["DP-1"] == str(items[2].path)


def test_a_skipped_wallpaper_cannot_be_applied(backend: FakeApplication) -> None:
    items = backend.session.library.items
    backend.session.pairings.mark_borked(items[3], "codec missing", "runtime")
    adapter, _keeper = _adapter(backend)
    assert adapter.apply_blocked(str(items[3].path)) == "Playback unavailable: codec missing"
    adapter.apply(str(items[3].path))
    assert backend.played == [] and backend.reports == ["Playback unavailable: codec missing"]


def test_favorite_waits_for_the_store(backend: FakeApplication) -> None:
    items = backend.session.library.items
    adapter, _keeper = _adapter(backend)
    wid = str(items[0].path)
    seen = _topics(adapter)

    adapter.toggle_favorite(wid)
    assert not adapter.wallpaper(wid).favorite, "no star before the store has it"
    assert seen == []
    backend.run_authoring()
    assert items[0].path in backend.session.favourites.paths
    assert backend.published == 1 and "library" in seen
    assert adapter.wallpaper(wid).favorite

    adapter.toggle_favorite(wid)
    backend.run_authoring()
    assert not adapter.wallpaper(wid).favorite


# -- read-only states ----------------------------------------------------------------


def test_a_newer_playlists_file_turns_apply_and_favorite_off(
    library: tuple[MediaItem, ...],
) -> None:
    target = paths.app_state_dir() / playlists.STATE_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"version": 99, "playlists": []}\n')
    session = _session(library)
    try:
        backend = FakeApplication(session)
        adapter, _keeper = _adapter(backend)
        wid = str(library[0].path)
        assert session.newer_version_files() == ("playlists.json",)
        assert "playlists.json" in adapter.apply_blocked(wid)
        assert "playlists.json" in adapter.favorite_blocked()
        notice = adapter.read_only_notice()
        assert "playlists.json was saved by a newer version of Wall-in-One" in notice
        assert "Apply and favorites are off" in notice

        adapter.apply(wid)
        adapter.toggle_favorite(wid)
        backend.run_authoring()
        assert backend.played == [] and backend.authoring == deque()
        assert len(backend.reports) == 2 and all("newer version" in m for m in backend.reports)
        assert target.read_text() == '{"version": 99, "playlists": []}\n'
    finally:
        session.shutdown()


def test_unknown_settings_keys_are_in_the_notice(backend: FakeApplication) -> None:
    adapter, _keeper = _adapter(backend)
    assert adapter.read_only_notice() == ""
    backend.unknown_keys = ("ui_glass_frost",)
    notice = adapter.read_only_notice()
    assert notice.startswith("Settings are read-only: settings.toml has settings")
    assert "ui_glass_frost" in notice
    assert adapter.apply_blocked(str(backend.session.library.items[0].path)) == ""


# -- the window's look: ui.toml --------------------------------------------------------


def test_the_look_is_read_from_ui_toml_and_saved_only_on_a_change(
    backend: FakeApplication,
) -> None:
    target = paths.ui_prefs_path()
    adapter, keeper = _adapter(backend)
    assert (adapter.window_style, adapter.thumbnail_size) == ("solid", "large")
    assert (adapter.background_alpha, adapter.panel_alpha) == (1.0, 1.0)
    assert not target.exists(), "opening never writes ui.toml"

    seen = _topics(adapter)
    adapter.set_window_style("frosted")
    keeper.close(wait=True)
    assert seen == ["appearance"]
    saved = ui_prefs.load()
    assert saved.prefs.window_style == "frosted"
    assert backend.glass[-1] == css.Glass(background=0.30, panel=0.60)

    # Dials are saved once, a moment after they stop moving.
    keeper = UiPrefsKeeper(backend.window_report)
    adapter = RealAppState(backend, keeper)
    adapter.set_panel_opacity(0.7)
    adapter.set_panel_opacity(0.72)
    adapter.set_frost(0.25)
    assert keeper.busy and ui_prefs.load().prefs.frost == 0.5
    keeper.close(wait=True)
    prefs = ui_prefs.load().prefs
    assert (prefs.panel_opacity.frosted, prefs.frost) == (0.72, 0.25)
    assert keeper.saves == 1

    keeper = UiPrefsKeeper(backend.window_report)
    adapter = RealAppState(backend, keeper)
    adapter.set_window_style("frosted")  # unchanged: nothing to save
    adapter.set_thumbnail_size("small")
    keeper.close(wait=True)
    assert keeper.saves == 1 and ui_prefs.load().prefs.thumbnail_size == "small"


def test_a_ui_toml_from_a_newer_version_is_never_written(backend: FakeApplication) -> None:
    target = paths.ui_prefs_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    document = 'version = 7\nwindow_style = "translucent"\n'
    target.write_text(document)
    adapter, keeper = _adapter(backend)
    assert adapter.window_style == "translucent"
    assert "newer version" in adapter.appearance_blocked()

    adapter.set_window_style("frosted")
    adapter.set_thumbnail_size("small")
    keeper.close(wait=True)
    assert target.read_text() == document
    assert keeper.saves == 0 and len(backend.reports) == 2


# -- Play's verb: one rule for every window ----------------------------------------------


@pytest.mark.parametrize(
    ("state", "renderer_failed", "verb"),
    [
        ("playing", False, "pause"),
        ("paused", False, "play"),
        ("stopped", False, "play"),
        ("mixed", False, "toggle"),
        ("playing", True, "play"),
        ("mixed", True, "play"),
    ],
)
def test_play_pauses_plays_synchronizes_or_retries(
    state: str, renderer_failed: bool, verb: str
) -> None:
    assert playback_verbs.play_verb(state, renderer_failed=renderer_failed) == verb


def test_the_overall_state_falls_back_to_an_older_runtimes_paused_flag() -> None:
    assert playback_verbs.playback_state(two_display_status("mixed")) == "mixed"
    assert playback_verbs.playback_state({"paused": True}) == "paused"
    assert playback_verbs.playback_state({"playback_state": "sideways"}) == "playing"


def test_a_display_is_taboo_by_its_flag_or_the_runtimes_inventory() -> None:
    status = two_display_status()
    truth = runtime_truth.from_status(status)
    assert truth is not None
    display = truth.displays[0]
    assert not playback_verbs.display_is_taboo(status, display)
    listed = {**status, "taboo_entries": [{"playlist_id": "evening", "entry_id": "entry-1"}]}
    assert playback_verbs.display_is_taboo(listed, display)
    flagged = two_display_status()
    records = flagged["displays"]
    assert isinstance(records, list)
    records[0]["entry_taboo"] = True
    truth = runtime_truth.from_status(flagged)
    assert truth is not None and playback_verbs.display_is_taboo(flagged, truth.displays[0])
