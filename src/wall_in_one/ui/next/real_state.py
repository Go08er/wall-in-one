"""The real `AppState`: the application's stores and the runtime's own status.

**Reads.** The library, playlists, favourites and pairings come from the
application's `Session` as it last installed them; the window hands every
newly installed library over (`RealAppState.reload`). What is on screen and
why comes from the `RuntimeStatusModel`: each connected display's route
(``route_source``: a pick, a schedule rule, the display's own playlist or the
default), its playlist and its entry, resolved to a library item by
`runtime_truth.media_playback`. Nothing here resolves a schedule: "until"
(when a reason ends) and "Next in" (when the rotation advances) are the
runtime's own per-display timing, and stay empty with a service that does not
report it.

**Writes.** Apply, Favorite and the player bar's playback controls, and
nothing else. Apply is the classic window's Quick choice (`play_item_async`,
or `play_item_on_async` for one display); Favorite is the classic star, on
the authoring lane; the playback controls are the classic runtime verbs
(`real_controls.RuntimeControls`). All of them report their failures through
the application; none changes what the window shows until the store or the
runtime says so (the favourite comes back through `reload`, what plays
through the next status). There is no undo.

**Read-only states.** A store saved by a newer version turns Apply, Favorite
and the playback controls off (the runtime configuration cannot be compiled
while one exists, so no change could reach the wallpaper service), and
unknown settings keys are named in the notice; see `read_only_notice`.

**The look.** The window style, opacity dials, frost and thumbnail size live
in ``ui.toml`` (`UiPrefsKeeper`); the glass dials are handed to the
application, which renders them into its own stylesheet.
"""

from __future__ import annotations

import datetime
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar, Final, Protocol, TypeVar

import gi

gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GObject

from wall_in_one import config, runtime_config
from wall_in_one.library import filter as library_filter
from wall_in_one.library import pairings
from wall_in_one.library.model import Kind, MediaItem, Ownership
from wall_in_one.library.playlists import DISPLAY_QUICK_CHOICE_ID_PREFIX
from wall_in_one.library.scan import WORKSHOP_PROVIDER
from wall_in_one.session import QUICK_CHOICE_ID, Session
from wall_in_one.theme import css
from wall_in_one.ui import runtime_truth
from wall_in_one.ui.next import status_line
from wall_in_one.ui.next.library_thumbnails import StillSource
from wall_in_one.ui.next.prefs import DIAL_SAVE_DELAY_MS, UiPrefsKeeper
from wall_in_one.ui.next.real_controls import RuntimeBackend, RuntimeControls
from wall_in_one.ui.next.state import (
    Banner,
    LibraryEditing,
    OnScreen,
    PlaybackControls,
    PlaybackState,
    Player,
    Reason,
    Route,
    ToastButton,
    Undo,
    WallpaperView,
)
from wall_in_one.ui.status_model import RuntimeStatusModel, RuntimeStatusView, StatusChange
from wall_in_one.ui_prefs import GLASS_STYLES, GlassOpacity

_T = TypeVar("_T")

#: The automatic playlist every library has: the runtime's own fallback.
ALL_WALLPAPERS: Final = "All wallpapers"
_ROUTES: Final[dict[str, Route]] = {
    "manual": "pick",
    "schedule": "schedule",
    "assignment": "display",
    "default": "default",
}


class Backend(RuntimeBackend, Protocol):
    """What the adapter uses of `wall_in_one.ui.app.Application`."""

    @property
    def session(self) -> Session: ...

    @property
    def status_model(self) -> RuntimeStatusModel: ...

    @property
    def settings(self) -> config.Settings: ...

    @property
    def settings_unknown_keys(self) -> tuple[str, ...]: ...

    def play_item_async(self, item: MediaItem) -> bool: ...

    def play_item_on_async(self, item: MediaItem, connector: str) -> bool: ...

    def authoring_action_async(
        self,
        work: Callable[[], _T],
        finish: Callable[[_T], None],
        *,
        prepare: Callable[[], Callable[[], _T]] | None = None,
        failure: Callable[[str], None] | None = None,
    ) -> bool: ...

    def current_item_for_authoring(self, item: MediaItem) -> MediaItem: ...

    def favourites_changed(self) -> None: ...

    def window_report(self, message: str) -> None: ...

    def set_window_glass(self, glass: css.Glass | None) -> None: ...


# -- view records ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LibraryWallpaper:
    """One `MediaItem` as the new interface shows it (`WallpaperView`)."""

    id: str
    name: str
    kind: str
    is_moving: bool
    source: str
    folder: str
    size: str
    added: str
    favorite: bool
    color_mode: str
    scheme: str | None
    palette: str | None
    theme_mode: str
    problem: str
    item: MediaItem
    resolution: str = ""
    duration: str = ""
    still_note: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LibraryPlaylist:
    """A playlist in the sidebar (`PlaylistView`); entries are wallpaper ids."""

    id: str
    name: str
    entries: tuple[str, ...]
    automatic: str = ""
    icon: str = ""


@dataclass(frozen=True, slots=True)
class LiveDisplay:
    """A connected display the runtime reports (`DisplayView`)."""

    connector: str
    model: str = ""


def human_size(size: int) -> str:
    if size >= 1_000_000_000:
        return f"{size / 1_000_000_000:.1f} GB"
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    return f"{max(1, round(size / 1000))} KB" if size else "0 KB"


def _home_relative(path: Path) -> str:
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)


def _source(item: MediaItem) -> str:
    if item.kind is Kind.SCENE or item.provider == WORKSHOP_PROVIDER:
        return "Workshop"
    if item.ownership is Ownership.MANAGED:
        return {"wallhaven": "Wallhaven", "motionbgs": "MotionBGS"}.get(
            item.provider.casefold(), item.provider
        )
    return "Local"


def wallpaper_view(
    item: MediaItem,
    *,
    favourite: bool,
    pairing: pairings.Pairing | None,
    health: pairings.Health,
) -> LibraryWallpaper:
    """What the new interface shows of ``item``, from the stores alone."""
    policy = pairing.palette if pairing is not None and pairing.customized else None
    policy = policy or pairings.PalettePolicy()
    if policy.keeps_palette:
        mode, scheme, palette = "keep", None, None
    elif policy.is_adaptive:
        pinned = policy.adaptive_scheme("")
        mode, scheme, palette = "adaptive", pinned or None, None
    else:
        mode, scheme, palette = "palette", None, policy.name or policy.kind
    added = datetime.datetime.fromtimestamp(item.mtime).strftime("%-d %b %Y") if item.mtime else ""
    return LibraryWallpaper(
        id=str(item.path),
        name=item.name,
        kind=item.kind.value,
        is_moving=item.is_moving,
        source=_source(item),
        folder=_home_relative(item.path.parent),
        size=human_size(item.size),
        added=added,
        favorite=favourite,
        color_mode=mode,
        scheme=scheme,
        palette=palette,
        theme_mode=policy.mode.value,
        problem=health.reason if health.is_borked else "",
        item=item,
    )


def scheme_name(key: str) -> str:
    """``m3-tonal-spot`` -> ``Tonal Spot``."""
    return key.removeprefix("m3-").replace("-", " ").title()


def _is_quick_choice(playlist_id: str) -> bool:
    return playlist_id == QUICK_CHOICE_ID or playlist_id.startswith(DISPLAY_QUICK_CHOICE_ID_PREFIX)


def _timing(rows: Sequence[runtime_truth.DisplayRuntimeTruth]) -> str:
    """The soonest rotation in scope as "Next in 12 min", or empty when none is due.

    Minutes round up, and never read 0, the way the companion panel counts.
    """
    deadlines = [row.next_cycle_in_s for row in rows if row.next_cycle_in_s is not None]
    if not deadlines:
        return ""
    return f"Next in {max(1, math.ceil(min(deadlines) / 60))} min"


def _listed(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"


# -- the adapter -----------------------------------------------------------------------


class RealAppState(GObject.Object):
    """`AppState` over the application's session, runtime status and ``ui.toml``."""

    __gsignals__: ClassVar[dict[str, Any]] = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "toast": (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
        "navigate": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, backend: Backend, prefs: UiPrefsKeeper) -> None:
        super().__init__()
        self._backend = backend
        self._prefs = prefs
        self._scope = "all"
        self._scanning = False
        self._wallpapers: tuple[LibraryWallpaper, ...] = ()
        self._index: dict[str, LibraryWallpaper] = {}
        self._playlists: tuple[LibraryPlaylist, ...] = ()
        self._playlist_index: dict[str, LibraryPlaylist] = {}
        self._displays: tuple[LiveDisplay, ...] = ()
        self._current: dict[str, str] = {}
        self._view = backend.status_model.view
        self._player = Player()
        self._banner: Banner | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._controls = RuntimeControls(backend, self, self._authoring_blocked)
        self.reload()
        self.listen()

    # -- lifetime ----------------------------------------------------------------
    def listen(self) -> None:
        """Follow the status model (again, after `close`)."""
        if self._unsubscribe is None:
            self._unsubscribe = self._backend.status_model.subscribe(self._on_status)
            self._adopt_status(self._backend.status_model.view)

    def close(self) -> None:
        """Stop following the status model; the window is going away."""
        unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()

    def emit_changed(self, *topics: str) -> None:
        for topic in topics:
            self.emit("changed", topic)

    # -- signals ----------------------------------------------------------------
    def toast(self, text: str, undo: Undo | None = None, action: ToastButton | None = None) -> None:
        """Show ``text``. There is no undo in this adapter, so ``undo`` is ignored."""
        del undo
        self.emit("toast", text, action)

    def navigate(self, page: str) -> None:
        self.emit("navigate", page)

    # -- reading the application ----------------------------------------------------
    @property
    def _session(self) -> Session:
        return self._backend.session

    def reload(self) -> None:
        """Adopt the session's newly installed library, playlists and favourites."""
        session = self._session
        favourites = session.favourites.paths
        store = session.pairings
        views = []
        for item in session.library.items:
            identity = pairings.Identity.of(item)
            views.append(
                wallpaper_view(
                    item,
                    favourite=item.path in favourites,
                    pairing=store.get(identity),
                    health=store.health(identity),
                )
            )
        self._wallpapers = tuple(views)
        self._index = {view.id: view for view in views}
        self._playlists = self._read_playlists()
        self._playlist_index = {playlist.id: playlist for playlist in self._playlists}
        self.emit_changed("library", "playlists")
        self._adopt_status(self._view)

    def _read_playlists(self) -> tuple[LibraryPlaylist, ...]:
        authored: list[LibraryPlaylist] = []
        for playlist in self._session.playlists.all():
            if _is_quick_choice(playlist.id):
                continue  # "your pick" is shown as such, never as a playlist
            entries = tuple(str(entry.path) for entry in playlist.entries)
            authored.append(LibraryPlaylist(playlist.id, playlist.name, entries))
        everything = LibraryPlaylist(
            runtime_config.FALLBACK_PLAYLIST_ID,
            ALL_WALLPAPERS,
            tuple(view.id for view in self._wallpapers),
            automatic="Every wallpaper in your library, kept up to date",
            icon="view-grid-symbolic",
        )
        return (*authored, everything)

    def reload_playlists(self) -> None:
        """Adopt a playlist store mutation (the library itself did not change)."""
        self._playlists = self._read_playlists()
        self._playlist_index = {playlist.id: playlist for playlist in self._playlists}
        self.emit_changed("playlists")
        self._adopt_status(self._view)

    def refresh_current(self) -> None:
        """Resolve the runtime's entries against the session again."""
        self._adopt_status(self._view)

    def set_scanning(self, scanning: bool) -> None:
        if scanning != self._scanning:
            self._scanning = scanning
            self.emit_changed("system")

    def settings_changed(self) -> None:
        """The application adopted a new settings snapshot."""
        self._adopt_status(self._view)
        self.emit_changed("settings")

    # -- the runtime's status ------------------------------------------------------
    def _on_status(self, _change: StatusChange, view: RuntimeStatusView) -> None:
        self._adopt_status(view)

    def _adopt_status(self, view: RuntimeStatusView) -> None:
        """Take one publication; say only what changed (the poll repeats every 2 s)."""
        self._view = view
        truth = view.truth if view.service == "running" else None
        displays = self._read_displays(truth)
        current = self._read_current(view.status if truth is not None else None)
        if self._scope != "all" and self._scope not in {d.connector for d in displays}:
            self._scope = "all"
        topics: list[str] = []
        if displays != self._displays:
            self._displays = displays
            topics.append("displays")
        if current != self._current:
            self._current = current
            topics.append("now")
        player = self._controls.annotate(self._read_player(view, truth), view)
        if player != self._player:
            self._player = player
            topics.append("playback")
        banner = self._read_banner(view, truth)
        if banner != self._banner:
            self._banner = banner
            topics.append("system")
        self.emit_changed(*topics)

    def _read_displays(self, truth: runtime_truth.RuntimeTruth | None) -> tuple[LiveDisplay, ...]:
        if truth is None:
            return ()
        models = _monitor_models()
        return tuple(
            LiveDisplay(display.connector, models.get(display.connector, ""))
            for display in truth.displays
            if display.connected
        )

    def _read_current(self, status: Mapping[str, object] | None) -> dict[str, str]:
        """Connector -> wallpaper id, for each connected display the runtime reports."""
        if status is None:
            return {}
        session = self._session
        authored = session.playlists.all()
        items = session.library.items
        records = status.get("displays")
        found: dict[str, str] = {}
        if isinstance(records, list) and records:
            for record in records:
                if not isinstance(record, Mapping) or record.get("connected") is False:
                    continue
                connector = record.get("connector")
                if not isinstance(connector, str):
                    continue
                single = {**status, "displays": [record]}
                playback = runtime_truth.media_playback(single, authored, items)
                if playback is not None and playback.current:
                    found[connector] = str(playback.current[0])
            return found
        playback = runtime_truth.media_playback(status, authored, items)
        if playback is not None and playback.current:
            found["Display"] = str(playback.current[0])
        return found

    def _read_player(
        self, view: RuntimeStatusView, truth: runtime_truth.RuntimeTruth | None
    ) -> Player:
        notes = status_line.marks(view)
        if view.service == "checking":
            return Player(service="checking", notes=notes)
        if view.service == "unavailable":
            return Player(service="stopped", notes=notes)
        if truth is None:
            # Running, but this snapshot says nothing this build can show.
            return Player(service="running", notes=notes)
        targets = set(self.targets())
        screens: list[OnScreen] = []
        states: list[str] = []
        following = True
        shuffle = rotate = False
        rows = [d for d in truth.displays if d.connected and d.connector in targets]
        for display in rows:
            wid = self._current.get(display.connector, "")
            route = _ROUTES.get(display.route_source, "default")
            picked_one = route == "pick" and _is_quick_choice(display.playlist_id)
            until = "" if route == "pick" else display.until or ""
            reason = Reason(route, "" if picked_one else display.playlist, until)
            name = self._index[wid].name if wid in self._index else ""
            screens.append(OnScreen(display.connector, wid, name, reason))
            states.append(display.playback_state)
            following = following and route != "pick"
            shuffle = shuffle or display.shuffle
            rotate = rotate or display.cycle_enabled
        if not rows:
            # A version-1 runtime: one answer for every display.
            route = "pick" if truth.is_manual else "schedule"
            picked_one = route == "pick" and _is_quick_choice(truth.playlist_id)
            wid = next(iter(self._current.values()), "")
            name = self._index[wid].name if wid in self._index else ""
            reason = Reason(route, "" if picked_one else truth.playlist)
            screens.append(OnScreen("Display", wid, name, reason))
            following = not truth.is_manual
        playback: PlaybackState = "playing"
        if states and all(state == "paused" for state in states):
            playback = "paused"
        elif states and all(state == "stopped" for state in states):
            playback = "stopped"
        return Player(
            service="running",
            screens=tuple(screens),
            playback=playback,
            following_schedule=following,
            shuffle=shuffle,
            rotate=rotate,
            timing=_timing(rows) if rotate else "Not changing",
            notes=notes,
        )

    @staticmethod
    def _read_banner(
        view: RuntimeStatusView, truth: runtime_truth.RuntimeTruth | None
    ) -> Banner | None:
        if view.service == "unavailable":
            return Banner(
                "The wallpaper service isn\u2019t running, so nothing changes on schedule"
            )
        power = view.power if truth is not None else None
        if power is not None and power.inhibited:
            if power.reason == "battery":
                return Banner("On battery: animations are paused and stills stay on screen")
            return Banner(power.message)
        return None

    # -- AppState: the library ------------------------------------------------------
    @property
    def wallpapers(self) -> Sequence[LibraryWallpaper]:
        return self._wallpapers

    def wallpaper(self, wid: str) -> LibraryWallpaper:
        return self._index[wid]

    def has_wallpaper(self, wid: str) -> bool:
        return wid in self._index

    def library_sorts(self) -> Sequence[tuple[str, str]]:
        return [(sort.value, sort.label) for sort in library_filter.SORT_CHOICES]

    def library_query(
        self, *, kind: str, favorites: bool, text: str, sort: str
    ) -> Sequence[LibraryWallpaper]:
        """The classic grid's matching (words in the name) and orders."""
        wanted = library_filter.terms(text)
        try:
            order = library_filter.Sort(sort)
        except ValueError:
            order = library_filter.Sort.NAME
        kept = [
            view
            for view in self._wallpapers
            if (kind == "all" or view.kind == kind)
            and (not favorites or view.favorite)
            and library_filter.matches(view.name, wanted)
        ]
        kept.sort(key=lambda view: library_filter.order_key(view.item, order))
        return kept

    @property
    def library_scanning(self) -> bool:
        return self._scanning

    # -- playlists ----------------------------------------------------------------
    @property
    def playlists(self) -> Sequence[LibraryPlaylist]:
        return self._playlists

    def playlist(self, pid: str) -> LibraryPlaylist:
        return self._playlist_index[pid]

    def has_playlist(self, pid: str) -> bool:
        return pid in self._playlist_index

    def playlist_cover(self, pid: str, size: int) -> Gdk.Paintable | None:
        return None

    # -- displays and what is on screen ---------------------------------------------
    @property
    def displays(self) -> Sequence[LiveDisplay]:
        return self._displays

    @property
    def display_mode(self) -> str:
        truth = self._view.truth
        if truth is not None and truth.status_version == 2:
            return truth.display_mode
        return self._backend.settings.display_mode

    @property
    def scope(self) -> str:
        return self._scope

    def set_scope(self, scope: str) -> None:
        self._scope = scope
        self._adopt_status(self._view)
        self.emit_changed("scope")

    def scope_label(self, scope: str | None = None) -> str:
        chosen = scope or self._scope
        if chosen == "all" or self.display_mode == "mirrored":
            return "All displays"
        return chosen

    def targets(self, scope: str | None = None) -> list[str]:
        chosen = scope or self._scope
        connectors = [display.connector for display in self._displays]
        if chosen == "all" or self.display_mode == "mirrored" or chosen not in connectors:
            return connectors
        return [chosen]

    @property
    def current(self) -> Mapping[str, str]:
        return self._current

    def player(self) -> Player:
        return self._player

    def backdrop(self) -> object | None:
        """The still on the display that colours the desktop (or the first one)."""
        status = self._view.status
        truth = self._view.truth
        if status is None or truth is None:
            return None
        preferred = truth.theme_source.effective if truth.theme_source else None
        raw = status.get("displays")
        records = [r for r in raw if isinstance(r, Mapping)] if isinstance(raw, list) else []
        records.sort(key=lambda record: record.get("connector") != preferred)
        library = self._session.library
        known = (*library.items, *library.still_inventory)
        for record in records or [status]:
            still = record.get("still")
            if isinstance(still, str) and still:
                path = Path(still)
                item = next((candidate for candidate in known if candidate.path == path), None)
                if item is not None:
                    return StillSource(item)
            connector = record.get("connector")
            wid = self._current.get(connector) if isinstance(connector, str) else None
            if wid is not None and wid in self._index:
                return self._index[wid]
        return None

    # -- persistent conditions ------------------------------------------------------
    def banner(self) -> Banner | None:
        return self._banner

    def banner_activated(self, action: str) -> None:
        """No banner here has a button yet."""

    def _newer_files(self) -> tuple[str, ...]:
        return self._session.newer_version_files()

    def read_only_notice(self) -> str:
        parts: list[str] = []
        newer = self._newer_files()
        if newer:
            verb, pronoun = ("was", "it") if len(newer) == 1 else ("were", "them")
            parts.append(
                f"{_listed(newer)} {verb} saved by a newer version of Wall-in-One, so Apply, "
                f"favorites and playback controls are off here. Open that version to change "
                f"{pronoun}."
            )
        unknown = self._backend.settings_unknown_keys
        if unknown:
            parts.append(f"Settings are read-only: {config.read_only_message(unknown)}.")
        return " ".join(parts)

    # -- the writes: Apply and Favorite -----------------------------------------------
    def _authoring_blocked(self, what: str) -> str:
        newer = self._newer_files()
        if not newer:
            return ""
        verb = "is" if len(newer) == 1 else "are"
        return (
            f"{what} {'is' if what == 'Apply' else 'are'} off while {_listed(newer)} {verb} "
            "from a newer version of Wall-in-One"
        )

    def apply_blocked(self, wid: str) -> str:
        blocked = self._authoring_blocked("Apply")
        if blocked:
            return blocked
        view = self._index.get(wid)
        if view is None:
            return "Not in the library"
        if view.problem:
            return f"Playback unavailable: {view.problem}"
        return ""

    def apply(self, wid: str, scope: str = "all") -> None:
        """Quick choice on every display, or on one; the status that follows shows it."""
        blocked = self.apply_blocked(wid)
        if blocked:
            self._backend.window_report(blocked)
            return
        item = self._index[wid].item
        connectors = [display.connector for display in self._displays]
        if scope == "all" or self.display_mode == "mirrored" or scope not in connectors:
            self._backend.play_item_async(item)
        else:
            self._backend.play_item_on_async(item, scope)

    def favorite_blocked(self) -> str:
        return self._authoring_blocked("Favorites")

    def toggle_favorite(self, wid: str) -> None:
        """Star or unstar on the authoring lane; the star moves when the store says so."""
        blocked = self.favorite_blocked()
        view = self._index.get(wid)
        if blocked or view is None:
            if blocked:
                self._backend.window_report(blocked)
            return
        backend = self._backend
        wanted = view.item.path not in self._session.favourites.paths

        def prepare() -> Callable[[], bool]:
            current = backend.current_item_for_authoring(view.item)
            store = backend.session.favourites
            return lambda: store.add(current.path) if wanted else store.discard(current.path)

        def saved(_changed: bool) -> None:
            # Republishes, and hands the window the session (which reloads us).
            backend.favourites_changed()

        def failed(error: str) -> None:
            backend.window_report(f"{view.name} could not be saved as a favorite: {error}")

        backend.authoring_action_async(lambda: False, saved, prepare=prepare, failure=failed)

    # -- colors, read-only ----------------------------------------------------------
    @property
    def default_scheme(self) -> str:
        return self._backend.settings.preview_scheme

    def scheme_name(self, key: str) -> str:
        return scheme_name(key)

    def wallpaper_swatches(self, wallpaper: WallpaperView, dark: bool = True) -> list[str]:
        """Unknown until Noctalia generates them; cards show no dots."""
        return []

    # -- this window's look: ui.toml ---------------------------------------------------
    @property
    def window_style(self) -> str:
        return self._prefs.prefs.window_style

    def _dial(self, opacity: GlassOpacity) -> float:
        style = self.window_style
        if style == "translucent":
            return opacity.translucent
        if style == "frosted":
            return opacity.frosted
        return 1.0

    @property
    def background_alpha(self) -> float:
        return self._dial(self._prefs.prefs.background_opacity)

    @property
    def panel_alpha(self) -> float:
        return self._dial(self._prefs.prefs.panel_opacity)

    @property
    def frost(self) -> float:
        return self._prefs.prefs.frost

    @property
    def thumbnail_size(self) -> str:
        return self._prefs.prefs.thumbnail_size

    def glass(self) -> css.Glass:
        """The dials the application renders into its stylesheet."""
        return css.Glass(background=self.background_alpha, panel=self.panel_alpha)

    def _changed_look(self, *, delay_ms: int = 0, **fields: Any) -> None:
        if not self._prefs.change(delay_ms=delay_ms, **fields):
            self._backend.window_report(self._prefs.read_only)
            return
        self._backend.set_window_glass(self.glass())
        self.emit_changed("appearance")

    def set_window_style(self, style: str) -> None:
        if style != self.window_style:
            self._changed_look(window_style=style)

    def _set_dial(self, name: str, value: float) -> None:
        style = self.window_style
        if style not in GLASS_STYLES:
            return
        current: GlassOpacity = getattr(self._prefs.prefs, name)
        value = max(0.0, min(1.0, value))
        if getattr(current, style) != value:
            dial = replace(current, **{style: value})
            self._changed_look(delay_ms=DIAL_SAVE_DELAY_MS, **{name: dial})

    def set_background_opacity(self, value: float) -> None:
        self._set_dial("background_opacity", value)

    def set_panel_opacity(self, value: float) -> None:
        self._set_dial("panel_opacity", value)

    def set_frost(self, value: float) -> None:
        value = max(0.0, min(1.0, value))
        if value != self.frost:
            self._changed_look(delay_ms=DIAL_SAVE_DELAY_MS, frost=value)

    def set_thumbnail_size(self, size: str) -> None:
        if size != self.thumbnail_size and not self._prefs.change(thumbnail_size=size):
            self._backend.window_report(self._prefs.read_only)

    def appearance_blocked(self) -> str:
        return self._prefs.read_only

    # -- optional parts: the playback controls; no editing yet --------------------------
    @property
    def controls(self) -> PlaybackControls | None:
        return self._controls

    @property
    def editing(self) -> LibraryEditing | None:
        return None


def _monitor_models() -> dict[str, str]:
    """Connector -> model, from GTK's monitors (cheap, and on this thread anyway)."""
    display = Gdk.Display.get_default()
    if display is None:
        return {}
    models: dict[str, str] = {}
    monitors = display.get_monitors()
    for index in range(monitors.get_n_items()):
        monitor = monitors.get_item(index)
        if isinstance(monitor, Gdk.Monitor):
            connector, model = monitor.get_connector(), monitor.get_model()
            if connector and model:
                models[connector] = model
    return models
