"""In-memory app state for the prototype: the only boundary between pages and data.

Pages read AppState's lists, lookups and queries, and change anything only by
calling its named methods (rename_playlist, set_rule_enabled, …). Each method
emits the topics it touches and, where the page offers Undo, returns the undo
callable; the page words the toast. (apply, play_playlist, resume_schedule,
stop and add_to_playlist are older and still toast themselves.) Pages never
import ``data``, so a real-app adapter can replace this class without touching
them. View-model types are in ``models``, fixed words in ``catalog``, and
pictures come from ``thumbs``.

Nothing here talks to a real runtime: the demo's data is shared module state
(``data``), and "now" is simulated (resolution(), advance()).

It implements the app's ``wall_in_one.ui.next.state.AppState`` Protocol (and
its optional ``PlaybackControls`` and ``LibraryEditing``), so the shell,
player bar, Library and inspector the app ships run over it unchanged; the
app's own adapter is ``wall_in_one.ui.next.real_state.RealAppState``.

Pages observe ``changed(topic)`` and repaint. Topics: now, playback, library,
playlists, schedule, displays, settings, system (battery/service banners),
theme, appearance (window style and dials), scope (the player bar's display
scope), clock (the demo clock ticked), folders (library folders), preferences
(rows only Settings shows) and display-settings (per-display renderer settings).
"""

from __future__ import annotations

import copy
import datetime as dt
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, GObject

from wall_in_one.ui.next.state import UNCHANGED, Banner, OnScreen, Player, Reason, WallpaperView

from . import art, data, store_catalog
from .catalog import KIND_LABEL
from .models import Display, Folder, Palette, Playlist, RememberedDisplay, Rule, StoreItem, Wallpaper

#: An Undo callback, as returned by the actions that pages offer Undo for.
Undo = Callable[[], None]
#: The Library's orders: (key, label).
LIBRARY_SORTS = [("added", "Recently added"), ("name", "Name"), ("kind", "Type"), ("color", "Color")]
__all__ = ["UNCHANGED", "AppState", "Resolution", "Undo", "resolve", "rule_matches"]


@dataclass
class Resolution:
    playlist: str
    rule: Rule | None  # None = fallback
    until: str  # human "18:00" or ""
    next_playlist: str
    next_at: str


def _minutes(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def rule_matches(rule: Rule, at: dt.datetime, display: str | None) -> bool:
    """Same semantics as the Rust runtime: end exclusive, wraps midnight, the
    after-midnight tail belongs to the day the window started."""
    if not rule.enabled:
        return False
    if rule.display and display not in (None, rule.display):
        return False
    if rule.display and display is None:
        return False
    minute = at.hour * 60 + at.minute
    tail = False
    if rule.start and rule.end:
        start, end = _minutes(rule.start), _minutes(rule.end)
        if start == end:
            within = True
        elif start < end:
            within = start <= minute < end
        else:
            within = minute >= start or minute < end
            tail = minute < end
        if not within:
            return False
    calendar = at - dt.timedelta(days=1) if tail else at
    if rule.months and calendar.month - 1 not in rule.months:
        return False
    return not rule.days or calendar.weekday() in rule.days


def resolve(rules: list[Rule], at: dt.datetime, display: str | None = None) -> Rule | None:
    chosen = None
    for rule in rules:
        if rule_matches(rule, at, display):
            chosen = rule
    return chosen


# Demo scenes start the schedule from the original rules, copied at import
# before anything can edit them (data.RULES is shared by every AppState).
_SEED_RULES = [copy.deepcopy(rule) for rule in data.RULES]


class AppState(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "toast": (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
        "navigate": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    # -- signals and plumbing -------------------------------------------------
    def __init__(self, library: list[Wallpaper] | None = None) -> None:
        """``library`` replaces the demo's wallpapers (e.g. thousands, to test paging)."""
        super().__init__()
        # A fixed demo clock keeps screenshots reproducible: Wednesday afternoon.
        self.now = dt.datetime(2026, 9, 30, 14, 35)
        # The demo's lists are shared module data (every AppState sees the same
        # library, playlists and rules), as the screenshot tour and smoke test expect.
        if library is None:
            self.wallpapers = data.WALLPAPERS
            self._wallpaper_index = data.BY_ID
        else:
            self.wallpapers = library
            self._wallpaper_index = {wallpaper.id: wallpaper for wallpaper in library}
        self._playlist_index = data.PLAYLIST_BY_ID
        self.playlists = data.PLAYLISTS
        self.rules = data.RULES
        self.fallback = data.FALLBACK_PLAYLIST
        self.displays = data.DISPLAYS
        self.display_mode = "independent"  # "mirrored" | "independent"
        # Per-display truth. In mirrored mode both entries move together.
        self.current = {"DP-1": "lily-pond", "HDMI-A-1": "rain-window"}
        self.manual: dict[str, str] = {}  # connector -> playlist id chosen by hand ("Play now")
        # A display's own playlist, used only when no schedule rule matches it
        # ("" = the app default, state.fallback). Mirrors the runtime's order.
        self.assigned = {"DP-1": "", "HDMI-A-1": ""}
        # Noctalia has one shell-wide palette; in independent mode one display drives it.
        self.color_display = "DP-1"
        self._held: set[str] = set()  # displays paused on their own while the rest play
        self._kept_assigned: dict[str, str] = {}  # display playlists kept aside while linked
        self._display_settings: dict[str, dict] = {}  # per-display renderer settings
        self.remembered_displays: list[RememberedDisplay] = [copy.copy(item) for item in data.REMEMBERED_DISPLAYS]
        self._demo_hidden_rules: list[Rule] | None = None
        self.playback = "playing"  # playing | paused | stopped
        self.rotate = True
        self.next_change_minutes = 12
        self.on_battery = False
        self.stop_on_battery = True
        self.service_running = True
        self.dark = True
        self.follow_noctalia_colors = True
        # What drives Noctalia's palette (Settings → Colors). The app follows
        # whatever the desktop shows, see desktop_swatches().
        self.desktop_colors = True
        self.template_status = "working"  # Noctalia's template: working | busy | missing
        self._override: tuple[Palette, str] | None = None  # (palette applied by hand, wallpaper id it covers)
        self._palettes: list[Palette] = data.make_palettes()
        self._desktop_source: object | None = None  # what last set the desktop's colors
        # The real Noctalia, read-only (see noctalia_live). When it is attached
        # and use_live_colors is on, the app's colors come from the real desktop
        # and the simulated wallpapers no longer recolor it.
        self.live = None
        self.use_live_colors = False
        self.default_interval = 30
        # Shared between Settings and the Store.
        self.wallhaven_key_saved = True
        self.download_folder = data.LIBRARY_FOLDERS[0][0]
        self.folders = [
            Folder(path, "" if "not found" in note.lower() else note.split(" · ")[0], "not found" in note.lower())
            for path, note, _primary in data.LIBRARY_FOLDERS
        ]
        # Preferences only the Settings page shows (nothing else in the demo acts on them).
        self.preferences = dict(data.PREFERENCES)
        # Window look: "solid", "translucent" (the compositor shows/blurs the
        # desktop behind) or "frosted" (the app draws your wallpaper, blurred).
        self.window_style = "solid"
        # Two dials per glass style, each remembered per style: the page
        # background behind lists and grids, and the panels and elements
        # (sidebar, header, player bar, inspector, cards). See glass.glass_layers.
        self.background_opacity = {"translucent": 0.55, "frosted": 0.30}
        self.panel_opacity = {"translucent": 0.80, "frosted": 0.60}
        self.frost = 0.5  # in-app blur strength for the frosted style, 0 (clear) – 1
        self.scope = "all"  # player-bar scope: "all" or a connector
        self.thumbnail_size = "large"  # the Library's cards: "large" or "small"
        self.library_scanning = False
        self._rng = random.Random(4)
        self._rule_serial = 0

    def emit_changed(self, *topics: str) -> None:
        for topic in topics:
            self.emit("changed", topic)

    def toast(
        self, text: str, undo: Callable[[], None] | None = None, action: tuple[str, Callable[[], None]] | None = None
    ) -> None:
        """Show a toast. ``undo`` adds an Undo button; ``action`` = (label, callback)
        adds any other single button (e.g. ("Apply", ...))."""
        self.emit("toast", text, ("Undo", undo) if undo else action)

    def navigate(self, page: str) -> None:
        self.emit("navigate", page)

    # -- lookups --------------------------------------------------------------
    def wallpaper(self, wid: str) -> Wallpaper:
        return self._wallpaper_index[wid]

    def has_wallpaper(self, wid: str) -> bool:
        return wid in self._wallpaper_index

    def playlist(self, pid: str) -> Playlist:
        return self._playlist_index[pid]

    def has_playlist(self, pid: str) -> bool:
        return pid in self._playlist_index

    def playlist_name(self, pid: str) -> str:
        return self._playlist_index[pid].name if pid in self._playlist_index else "Missing playlist"

    def playlist_cover(self, pid: str, size: int) -> Gdk.Texture:
        """A 2x2 mosaic of the playlist's first four different wallpapers (cached)."""
        keys: list[tuple[str, int, bool]] = []
        for wid in self.playlist(pid).entries:
            key = self.wallpaper(wid).key
            if key not in keys:
                keys.append(key)
            if len(keys) == 4:
                break
        return art.mosaic(tuple(keys), size)

    def rule(self, rule_id: str) -> Rule | None:
        return next((rule for rule in self.rules if rule.id == rule_id), None)

    def display(self, connector: str) -> Display:
        return next(display for display in self.displays if display.connector == connector)

    def connectors(self) -> list[str]:
        return [display.connector for display in self.displays]

    def lead_connector(self) -> str:
        """The display the others follow when they are linked: the primary one."""
        for display in self.displays:
            if display.primary:
                return display.connector
        return self.connectors()[0]

    def targets(self, scope: str | None = None) -> list[str]:
        scope = scope or self.scope
        return self.connectors() if scope == "all" or self.display_mode == "mirrored" else [scope]

    def scope_label(self, scope: str | None = None) -> str:
        scope = scope or self.scope
        if scope == "all" or self.display_mode == "mirrored":
            return "All displays"
        return scope

    # -- now ------------------------------------------------------------------
    # What plays where, and why. The runtime answers these; an adapter reads its status.
    def unscheduled_playlist(self, connector: str | None) -> str:
        """What a display plays when no rule matches: its own playlist, else the default."""
        return (self.assigned.get(connector or "") or self.fallback) if connector else self.fallback

    def resolution(self, connector: str | None = None) -> Resolution:
        # Same order as the Rust runtime's route_decision: a matching schedule
        # rule (global or for this display), then the display's own playlist,
        # then the default. A manual pick (handled by effective_playlist) beats all.
        rule = resolve(self.rules, self.now, connector)
        playlist = rule.playlist if rule else self.unscheduled_playlist(connector)
        # Find the next change by stepping forward in 5-minute increments.
        probe, until, nxt, nxt_at = self.now, "", playlist, ""
        for _ in range(12 * 48):
            probe += dt.timedelta(minutes=5)
            other = resolve(self.rules, probe, connector)
            other_pl = other.playlist if other else self.unscheduled_playlist(connector)
            if other is not rule:
                until = probe.strftime("%H:%M")
                nxt, nxt_at = other_pl, probe.strftime("%a %H:%M" if probe.date() != self.now.date() else "%H:%M")
                break
        return Resolution(playlist, rule, until, nxt, nxt_at)

    def effective_playlist(self, connector: str) -> str:
        if connector in self.manual:
            return self.manual[connector]
        return self.resolution(connector).playlist

    def following_schedule(self, connector: str | None = None) -> bool:
        targets = self.targets(connector) if connector else self.connectors()
        return not any(target in self.manual for target in targets)

    def shuffle_on(self, scope: str | None = None) -> bool:
        connector = self.targets(scope)[0]
        playlist_id = self.effective_playlist(connector)
        return playlist_id in self._playlist_index and self.playlist(playlist_id).shuffle

    def display_paused(self, connector: str) -> bool:
        """Paused everywhere, or (each display separately) paused on its own."""
        return self.playback == "paused" or (self.display_mode != "mirrored" and connector in self._held)

    def reason(self, connector: str) -> Reason:
        """Why this display shows what it shows (the simulation's answer)."""
        playlist_id = self.effective_playlist(connector)
        if connector in self.manual:
            return Reason("pick", "" if playlist_id == "quick" else self.playlist(playlist_id).name)
        resolution = self.resolution(connector)
        name = self.playlist(playlist_id).name
        if resolution.rule is not None:
            route = "schedule"
        elif self.assigned.get(connector) == playlist_id:
            route = "display"
        else:
            route = "default"
        return Reason(route, name, resolution.until)

    def player(self) -> Player:
        """What the player bar shows for its scope."""
        if not self.service_running:
            return Player(service="stopped", playback=self.playback)
        targets = self.targets()
        screens = tuple(
            OnScreen(
                connector, self.current[connector], self.wallpaper(self.current[connector]).name, self.reason(connector)
            )
            for connector in targets
        )
        playlist_id = self.effective_playlist(targets[0])
        if playlist_id == "quick":
            timing = ""
        elif self.rotate:
            timing = f"Next in {self.next_change_minutes} min"
        else:
            timing = "Not changing"
        return Player(
            service="running",
            screens=screens,
            playback=self.playback,
            following_schedule=self.following_schedule(self.scope if self.scope != "all" else None),
            shuffle=self.shuffle_on(),
            rotate=self.rotate,
            timing=timing,
        )

    def backdrop(self):
        """What the frosted style blurs: the real desktop's wallpaper, or the demo's."""
        live = self.live if self.live_colors() else None
        if live is not None and live.wallpaper is not None:
            return live.wallpaper
        return self.color_wallpaper()

    @property
    def controls(self) -> AppState:
        return self

    @property
    def editing(self) -> AppState:
        return self

    def banner(self) -> Banner | None:
        if not self.service_running:
            return Banner("The wallpaper service isn't running, so nothing changes on schedule", "Start", "start")
        if self.on_battery and self.stop_on_battery:
            return Banner("On battery: animations are paused and stills stay on screen", "Battery settings", "settings")
        return None

    def banner_activated(self, action: str) -> None:
        if action == "start":
            self.start_service()
        else:
            self.navigate("settings:playback")

    def read_only_notice(self) -> str:
        return ""

    # -- playback -------------------------------------------------------------
    # The runtime's verbs (play, pause, next, …) and the demo clock.
    def apply(self, wid: str, scope: str = "all") -> None:
        before = dict(self.current), dict(self.manual)
        for connector in self.targets(scope):
            self.current[connector] = wid
            self.manual[connector] = "quick"
        if self.playback == "stopped":
            self._set_playback("playing")
        self.emit_changed("now", "playback")
        where = "all displays" if scope == "all" or self.display_mode == "mirrored" else scope

        def undo() -> None:
            self.current, self.manual = before
            self.emit_changed("now", "playback")

        self.toast(f"“{self.wallpaper(wid).name}” is now on {where}", undo)

    def play_playlist(self, pid: str, scope: str = "all", start: int = 0) -> None:
        """Your pick: play a playlist (from entry ``start``) until you resume the schedule."""
        playlist = self.playlist(pid)
        if not playlist.entries:
            self.toast(f"“{playlist.name}” is empty — add wallpapers first")
            return
        for connector in self.targets(scope):
            self.manual[connector] = pid
            self.current[connector] = playlist.entries[start]
        self._set_playback("playing")
        self.emit_changed("now", "playback")
        self.toast(f"Playing “{playlist.name}” until you resume the schedule")

    def resume_schedule(self, scope: str = "all") -> None:
        for connector in self.targets(scope):
            self.manual.pop(connector, None)
            entries = self.playlist(self.effective_playlist(connector)).entries
            self.current[connector] = entries[0]
        self.emit_changed("now", "playback")
        self.toast("Following the schedule again")

    def _set_playback(self, playback: str) -> None:
        if playback != self.playback:
            self._held.clear()  # the player bar paused, resumed or stopped every display
        self.playback = playback

    def toggle_play(self) -> None:
        self._set_playback("paused" if self.playback == "playing" else "playing")
        self.emit_changed("playback")

    def stop(self) -> None:
        self._set_playback("stopped")
        self.emit_changed("playback")
        self.toast("Animation stopped — the still stays on screen")

    def step(self, direction: int, scope: str | None = None) -> None:
        for connector in self.targets(scope):
            playlist_id = self.effective_playlist(connector)
            entries = self.playlist(playlist_id).entries if playlist_id != "quick" else [self.current[connector]]
            current = self.current[connector]
            index = entries.index(current) if current in entries else -1
            self.current[connector] = entries[(index + direction) % len(entries)]
        self.next_change_minutes = self.default_interval
        self.emit_changed("now")

    def random(self, scope: str | None = None) -> None:
        for connector in self.targets(scope):
            playlist_id = self.effective_playlist(connector)
            entries = self.playlist(playlist_id).entries if playlist_id != "quick" else [w.id for w in self.wallpapers]
            self.current[connector] = self._rng.choice(entries)
        self.emit_changed("now")

    def set_rotate(self, value: bool) -> None:
        self.rotate = value
        self.emit_changed("playback")

    def set_shuffle(self, value: bool, scope: str | None = None) -> None:
        for connector in self.targets(scope):
            playlist_id = self.effective_playlist(connector)
            if playlist_id in self._playlist_index:
                self.playlist(playlist_id).shuffle = value
        self.emit_changed("playback", "playlists")

    def set_scope(self, scope: str) -> None:
        """The player bar's display scope: "all" or a connector."""
        self.scope = scope
        self.emit_changed("scope")

    def start_service(self) -> None:
        self.set_service_running(True)
        self.toast("Wallpaper service started")

    def set_battery(self, value: bool) -> None:
        self.on_battery = value
        self.emit_changed("system", "playback")

    def set_service_running(self, value: bool) -> None:
        self.service_running = value
        self.emit_changed("system", "playback", "now")

    def advance(self, minutes: int) -> None:
        """Demo clock: move time forward, letting the schedule and rotation act."""
        before = {connector: self.effective_playlist(connector) for connector in self.connectors()}
        self.now += dt.timedelta(minutes=minutes)
        for connector in self.connectors():
            if connector in self.manual:
                continue
            after = self.effective_playlist(connector)
            if after != before[connector] and self.playlist(after).entries:
                self.current[connector] = self.playlist(after).entries[0]
        if self.rotate and self.playback != "stopped" and self.service_running:
            self.next_change_minutes -= minutes
            while self.next_change_minutes <= 0:
                for connector in self.connectors():
                    playlist_id = self.effective_playlist(connector)
                    if playlist_id == "quick":
                        continue
                    entries = self.playlist(playlist_id).entries
                    current = self.current[connector]
                    index = entries.index(current) if current in entries else -1
                    self.current[connector] = (
                        self._rng.choice(entries)
                        if self.playlist(playlist_id).shuffle
                        else entries[(index + 1) % len(entries)]
                    )
                connector = self.connectors()[0]
                playlist_id = self.effective_playlist(connector)
                interval = self.playlist(playlist_id).interval if playlist_id in self._playlist_index else 0
                self.next_change_minutes += interval or self.default_interval
        self.emit_changed("schedule", "now", "playback", "clock")

    # -- library --------------------------------------------------------------
    def toggle_favorite(self, wid: str) -> None:
        wallpaper = self.wallpaper(wid)
        wallpaper.favorite = not wallpaper.favorite
        self.emit_changed("library")

    def favorite_wallpapers(self, wids: list[str]) -> None:
        for wid in wids:
            self.wallpaper(wid).favorite = True
        self.emit_changed("library")

    def retry_wallpaper(self, wid: str) -> None:
        """Forget a playback problem so the runtime tries the wallpaper again."""
        self.wallpaper(wid).problem = ""
        self.emit_changed("library")

    def library_sorts(self) -> list[tuple[str, str]]:
        return list(LIBRARY_SORTS)

    def library_query(self, *, kind: str, favorites: bool, text: str, sort: str) -> list[Wallpaper]:
        """Every wallpaper that passes the Library's filters, in ``sort`` order.

        Words match anywhere in the name, folder, source, tags, style or kind.
        """
        words = text.lower().split()

        def visible(wallpaper: Wallpaper) -> bool:
            if kind != "all" and wallpaper.kind != kind:
                return False
            if favorites and not wallpaper.favorite:
                return False
            haystack = " ".join(
                (
                    wallpaper.name,
                    wallpaper.folder,
                    wallpaper.source,
                    " ".join(wallpaper.tags),
                    wallpaper.style,
                    KIND_LABEL[wallpaper.kind],
                )
            ).lower()
            return all(word in haystack for word in words)

        order = {wallpaper.id: index for index, wallpaper in enumerate(self.wallpapers)}

        def key(wallpaper: Wallpaper):
            if sort == "name":
                return wallpaper.name.lower()
            if sort == "kind":
                return (wallpaper.kind, wallpaper.name.lower())
            if sort == "color":
                return art.look_for(*wallpaper.key).hue
            return order[wallpaper.id]

        return sorted((w for w in self.wallpapers if visible(w)), key=key)

    def apply_blocked(self, wid: str) -> str:
        """The demo never refuses Apply (a skipped wallpaper says so on its own)."""
        return ""

    def favorite_blocked(self) -> str:
        return ""

    def set_wallpaper_colors(
        self, wid: str, *, mode: str | None = None, scheme=UNCHANGED, palette: str | None = None, theme_mode=None
    ) -> None:
        """Change how a wallpaper colors the desktop: ``mode`` (adaptive, palette or
        keep), its ``scheme`` (None = the default), its ``palette`` and its light or
        dark ``theme_mode``. Choosing "palette" without one picks Catppuccin."""
        wallpaper = self.wallpaper(wid)
        if mode is not None:
            wallpaper.color_mode = mode
            if mode == "palette" and not wallpaper.palette:
                wallpaper.palette = "Catppuccin"
        if scheme is not UNCHANGED:
            wallpaper.scheme = scheme
        if palette is not None:
            wallpaper.palette = palette
        if theme_mode is not None:
            wallpaper.theme_mode = theme_mode
        self.emit_changed("library")

    def remove_wallpapers(self, wids: list[str]) -> Undo:
        """Take wallpapers out of the library; returns an undo that puts them back in place."""
        library = self.wallpapers
        chosen = [self.wallpaper(wid) for wid in wids]
        spots = [(library.index(w), w) for w in chosen if w in library]
        for _index, wallpaper in spots:
            library.remove(wallpaper)
        self.emit_changed("library")

        def undo() -> None:
            for index, wallpaper in sorted(spots, key=lambda spot: spot[0]):
                if wallpaper not in library:
                    library.insert(min(index, len(library)), wallpaper)
            self.emit_changed("library")

        return undo

    # -- store ----------------------------------------------------------------
    def store_search(self, query: store_catalog.Query) -> list[StoreItem]:
        """A provider's results for ``query`` (the real app: Browser.search)."""
        return store_catalog.search(query)

    def store_item(self, item_id: str) -> StoreItem | None:
        return store_catalog.by_id(item_id)

    def store_like_source(self, text: str) -> StoreItem | None:
        """The item a Wallhaven "like:<id>" search refers to."""
        return store_catalog.like_source(text)

    def import_store_item(self, item_id: str, quality: str | None = None, fresh: bool = False) -> str:
        """The Library entry for a downloaded Store item, created on first use (and
        added to the automatic playlists); returns its wallpaper id. ``fresh`` = it
        was downloaded just now."""
        item = self.store_item(item_id)
        item.in_library = True
        wid = f"store-{item.id}"
        if self.has_wallpaper(wid):
            return wid
        moving = item.provider == "MotionBGS"
        width, height = (1920, 1080) if moving and quality == "hd" else store_catalog.size(item)
        wallpaper = Wallpaper(
            id=wid,
            name=item.title,
            kind="video" if moving else "still",
            style=item.style,
            seed=item.seed,
            night=item.night,
            source=item.provider,
            folder=f"{self.download_folder}/Wall-in-One/Downloads/{item.provider}",
            resolution=f"{width} × {height}",
            size=f"{store_catalog.megabytes(item, quality):.1f} MB",
            added="Just now" if fresh else "12 Sep",
            duration=store_catalog.duration(item),
            still_note="Captured from the video at 0:03" if moving else "This image is its own still",
            tags=tuple(store_catalog.tags(item)),
        )
        self.wallpapers.insert(0, wallpaper)
        self._wallpaper_index[wid] = wallpaper
        for playlist in self.playlists:
            if playlist.automatic:
                playlist.entries.append(wid)
        self.emit_changed("library")
        return wid

    # -- colors ---------------------------------------------------------------
    @property
    def default_scheme(self) -> str:
        """The adaptive scheme inherited by wallpapers that don't choose one."""
        return data.DEFAULT_SCHEME

    def schemes(self) -> list[tuple[str, str, str]]:
        """Noctalia's color schemes: (key, name, description)."""
        return list(data.SCHEMES)

    def scheme_name(self, key: str) -> str:
        return data.SCHEME_NAME.get(key, "")

    def scheme_swatches(self, wallpaper: WallpaperView, scheme: str | None, dark: bool = True) -> list[str]:
        """[surface, primary, secondary, tertiary, error] for ``wallpaper`` under ``scheme``
        (None = the default scheme)."""
        return data.scheme_swatches(self.wallpaper(wallpaper.id), scheme, dark)

    def wallpaper_swatches(self, wallpaper: WallpaperView, dark: bool = True) -> list[str]:
        """The colors ``wallpaper`` puts on the desktop; empty when it keeps them."""
        return data.wallpaper_swatches(self.wallpaper(wallpaper.id), dark)

    def color_connector(self) -> str:
        """The display whose wallpaper drives the desktop palette."""
        lead = self.connectors()[0]
        chosen = self.color_display
        return chosen if self.display_mode == "independent" and chosen in self.current else lead

    def color_wallpaper(self) -> Wallpaper:
        return self.wallpaper(self.current[self.color_connector()])

    def palette_override(self) -> Palette | None:
        """The palette applied by hand in Settings, until the wallpaper changes."""
        if self._override and self._override[1] != self.color_wallpaper().id:
            self._override = None
        return self._override[0] if self._override else None

    def desktop_swatches(self) -> list[str]:
        """The colors Noctalia shows right now: [surface, primary, secondary, tertiary, error].

        Like the real desktop this has memory: a wallpaper set to "keep",
        desktop colors turned off, or a missing template all leave the last
        colors in place. An empty list means Noctalia's own default.
        """
        if self.live_colors():
            return self.live.swatches()
        wallpaper = self.color_wallpaper()
        override = self.palette_override()
        if self.desktop_colors and self.template_ok:
            if override is not None:
                self._desktop_source = override
            elif wallpaper.color_mode != "keep":
                self._desktop_source = wallpaper
        source = self._desktop_source
        if source is None:
            return []
        if isinstance(source, Wallpaper):
            return data.wallpaper_swatches(source, self.dark)
        return source.strip(self.dark)

    def attach_live(self, live, follow: bool = True) -> None:
        """Follow the real Noctalia: its palette, its light/dark mode and its wallpaper."""
        self.live = live
        self.use_live_colors = follow and live.found
        live.connect("changed", lambda _live: self.sync_live())
        self.sync_live()

    def sync_live(self) -> None:
        if not self.live_colors():
            return
        dark = self.live.mode == "dark"
        if dark != self.dark:
            self.dark = dark
            self.emit_changed("theme")
        self.emit_changed("settings", "now")

    def live_colors(self) -> bool:
        return bool(self.use_live_colors and self.live is not None and self.live.found)

    def live_tokens(self) -> dict[str, str] | None:
        """The real palette's tokens, when they fit the current light/dark mode."""
        if self.live_colors() and (self.live.mode == "dark") == self.dark:
            return self.live.tokens
        return None

    def set_use_live_colors(self, use: bool) -> None:
        """Follow the real Noctalia's colors (read-only) instead of the simulated desktop."""
        self.use_live_colors = use
        self.sync_live()  # adopt the real mode when switching on
        self.emit_changed("settings", "now")

    def set_dark(self, dark: bool) -> None:
        self.dark = dark
        self.emit_changed("theme", "now")

    # -- palettes -------------------------------------------------------------
    def palettes(self, origin: str | None = None) -> list[Palette]:
        """Noctalia's palettes, optionally of one origin (custom, builtin, community)."""
        return [palette for palette in self._palettes if origin is None or palette.origin == origin]

    def find_palette(self, name: str) -> Palette | None:
        return next((palette for palette in self._palettes if palette.name == name), None)

    def applied_palette(self) -> Palette | None:
        """The palette applied by hand, until the wallpaper changes."""
        return self.palette_override()

    def apply_palette(self, name: str | None) -> Undo:
        """Put a palette on the desktop until the wallpaper changes (None = stop)."""
        before = self.applied_palette()
        palette = self.find_palette(name) if name else None
        self._override = (palette, self.color_wallpaper().id) if palette else None
        self.emit_changed("settings")

        def undo() -> None:
            self._override = (before, self.color_wallpaper().id) if before else None
            self.emit_changed("settings")

        return undo

    def duplicate_palette(self, name: str) -> tuple[Palette, Undo]:
        """Copy a palette into "Yours", where it can be edited."""
        source = self.find_palette(name)
        copy = Palette(
            self._unique_palette_name(f"{source.name} copy"), "custom", dict(source.light), dict(source.dark)
        )
        self._palettes.insert(len(self.palettes("custom")), copy)
        self.emit_changed("settings")

        def undo() -> None:
            if copy in self._palettes:
                self._palettes.remove(copy)
                self.emit_changed("settings")

        return copy, undo

    def save_palette(self, name: str, new_name: str, light: dict[str, str], dark: dict[str, str]) -> None:
        """Store an edited custom palette under ``new_name``."""
        palette = self.find_palette(name)
        palette.name = new_name
        palette.light = dict(light)
        palette.dark = dict(dark)
        self.emit_changed("settings")

    def delete_palette(self, name: str) -> Undo:
        palette = self.find_palette(name)
        index = self._palettes.index(palette)
        self._palettes.remove(palette)
        applied = self.applied_palette()
        was_applied = applied is not None and applied.name == name
        if was_applied:
            self._override = None
        self.emit_changed("settings")

        def undo() -> None:
            self._palettes.insert(index, palette)
            if was_applied:
                self._override = (palette, self.color_wallpaper().id)
            self.emit_changed("settings")

        return undo

    def _unique_palette_name(self, base: str) -> str:
        names = {palette.name for palette in self._palettes}
        if base not in names:
            return base
        index = 2
        while f"{base} {index}" in names:
            index += 1
        return f"{base} {index}"

    # -- playlists ------------------------------------------------------------
    def create_playlist(self, name: str) -> str:
        """A new, empty playlist after the user's others; returns its id."""
        base = name.lower().replace(" ", "-")
        pid, number = base, 2
        while pid in self._playlist_index:  # a second "Frog day" must not replace the first
            pid, number = f"{base}-{number}", number + 1
        playlist = Playlist(pid, name, [])
        self.playlists.insert(len([p for p in self.playlists if not p.automatic]), playlist)
        self._playlist_index[pid] = playlist
        self.emit_changed("playlists")
        return pid

    def rename_playlist(self, pid: str, name: str) -> Undo:
        playlist = self.playlist(pid)
        old = playlist.name
        playlist.name = name
        self.emit_changed("playlists")

        def undo() -> None:
            playlist.name = old
            self.emit_changed("playlists")

        return undo

    def set_playlist_interval(self, pid: str, minutes: int) -> None:
        """How often a playlist changes wallpaper (0 = it doesn't)."""
        self.playlist(pid).interval = minutes
        self.emit_changed("playlists", "playback")

    def set_playlist_shuffle(self, pid: str, shuffle: bool) -> None:
        self.playlist(pid).shuffle = shuffle
        self.emit_changed("playlists", "playback")

    def duplicate_playlist(self, pid: str) -> tuple[str, str, Undo]:
        """Copy a playlist, just after it (or after the user's own ones for an
        automatic one). Returns (new id, new name, undo)."""
        source, playlists = self.playlist(pid), self.playlists
        base = f"{source.name} (copy)"
        name, number = base, 2
        names = {p.name for p in playlists}
        while name in names:
            name, number = f"{source.name} (copy {number})", number + 1
        copy_id, number = f"{source.id}-copy", 2
        while copy_id in self._playlist_index:
            copy_id, number = f"{source.id}-copy-{number}", number + 1
        copy = Playlist(copy_id, name, list(source.entries), interval=source.interval, shuffle=source.shuffle)
        user_count = len([p for p in playlists if not p.automatic])
        playlists.insert(playlists.index(source) + 1 if not source.automatic else user_count, copy)
        self._playlist_index[copy_id] = copy
        self.emit_changed("playlists")

        def undo() -> None:
            if copy in playlists:
                playlists.remove(copy)
            self._playlist_index.pop(copy_id, None)
            self.emit_changed("playlists")

        return copy_id, name, undo

    def delete_playlist(self, pid: str) -> Undo:
        """Delete a playlist with the rules that use it; displays that played it
        follow the schedule again, and so does a pick of it."""
        playlist, playlists = self.playlist(pid), self.playlists
        position = playlists.index(playlist)
        rules = [(i, rule) for i, rule in enumerate(self.rules) if rule.playlist == pid]
        assigned, manual = dict(self.assigned), dict(self.manual)
        for index, _rule in reversed(rules):
            del self.rules[index]
        for connector, value in self.assigned.items():
            if value == pid:
                self.assigned[connector] = ""
        for connector in [c for c, p in self.manual.items() if p == pid]:
            del self.manual[connector]
        playlists.remove(playlist)
        self._playlist_index.pop(pid, None)
        self.emit_changed("playlists", "schedule", "displays", "now", "playback")

        def undo() -> None:
            playlists.insert(min(position, len(playlists)), playlist)
            self._playlist_index[pid] = playlist
            for index, rule in rules:
                self.rules.insert(min(index, len(self.rules)), rule)
            self.assigned.clear()
            self.assigned.update(assigned)
            self.manual.clear()
            self.manual.update(manual)
            self.emit_changed("playlists", "schedule", "displays", "now", "playback")

        return undo

    def add_to_playlist(self, pid: str, wids: list[str]) -> None:
        playlist = self.playlist(pid)
        playlist.entries.extend(wids)
        self.emit_changed("playlists")
        noun = f"“{self.wallpaper(wids[0]).name}”" if len(wids) == 1 else f"{len(wids)} wallpapers"

        def undo() -> None:
            del playlist.entries[-len(wids) :]
            self.emit_changed("playlists")

        self.toast(f"Added {noun} to “{playlist.name}”", undo)

    def insert_entries(self, pid: str, position: int, wids: list[str]) -> Undo:
        """Put wallpapers into a playlist at ``position`` (the drop line)."""
        entries = self.playlist(pid).entries
        entries[position:position] = wids
        self.emit_changed("playlists")

        def undo() -> None:
            del entries[position : position + len(wids)]
            self.emit_changed("playlists")

        return undo

    def remove_entries(self, pid: str, indices: list[int]) -> tuple[list[tuple[int, str]], Undo]:
        """Take entries out of a playlist; returns [(index, wallpaper id)] and an undo
        that puts each back where it was."""
        entries = self.playlist(pid).entries
        removed = [(i, entries[i]) for i in sorted(set(indices)) if 0 <= i < len(entries)]
        for index, _wid in reversed(removed):
            del entries[index]
        self.emit_changed("playlists")

        def undo() -> None:
            for index, wid in removed:
                entries.insert(min(index, len(entries)), wid)
            self.emit_changed("playlists")

        return removed, undo

    def reorder_entries(self, pid: str, wids: list[str]) -> None:
        """Store a playlist's entries in a new order (a finished drag or keyboard move).
        Ignored unless ``wids`` holds exactly the current entries: a move that settled
        after entries were added or removed must not drop or bring back any."""
        entries = self.playlist(pid).entries
        if sorted(wids) != sorted(entries):
            return
        entries[:] = wids
        self.emit_changed("playlists")

    def move_entries_to_top(self, pid: str, indices: list[int]) -> None:
        entries = self.playlist(pid).entries
        chosen = set(indices)
        picked = [entries[i] for i in sorted(chosen)]
        rest = [wid for i, wid in enumerate(entries) if i not in chosen]
        entries[:] = picked + rest
        self.emit_changed("playlists")

    # -- schedule -------------------------------------------------------------
    # Every change lets the displays that follow the schedule switch playlist,
    # as the runtime would, then emits "schedule" and "now".
    # Every change lets displays that follow the schedule switch playlist, as the
    # runtime would, then emits "schedule" and "now".
    def _schedule_changed(self) -> None:
        for connector in self.connectors():
            if connector in self.manual:
                continue
            pid = self.resolution(connector).playlist
            entries = self.playlist(pid).entries if pid in self._playlist_index else []
            if entries and self.current.get(connector) not in entries:
                self.current[connector] = entries[0]
        self.emit_changed("schedule", "now")

    def _new_rule_id(self) -> str:
        existing = {rule.id for rule in self.rules}
        while True:
            self._rule_serial += 1
            candidate = f"rule-{self._rule_serial}"
            if candidate not in existing:
                return candidate

    def add_rule(self, draft: Rule) -> tuple[Rule, Undo]:
        """Add a rule at the top of the priority list (it wins where rules overlap)."""
        rule = Rule(
            self._new_rule_id(),
            draft.playlist,
            draft.days,
            draft.start,
            draft.end,
            draft.months,
            draft.display,
            draft.enabled,
        )
        self.rules.append(rule)
        self._schedule_changed()

        def undo() -> None:
            if rule in self.rules:
                self.rules.remove(rule)
                self._schedule_changed()

        return rule, undo

    def update_rule(self, rule_id: str, draft: Rule) -> Undo | None:
        """Give a rule the draft's playlist, days, times, months, display and on/off,
        keeping its place. None when nothing changed."""
        rule = self.rule(rule_id)

        def values(source: Rule) -> tuple:
            return (
                source.playlist,
                list(source.days),
                source.start,
                source.end,
                list(source.months),
                source.display,
                source.enabled,
            )

        before, after = values(rule), values(draft)
        if before == after:
            return None

        def put(fields: tuple) -> None:
            (rule.playlist, rule.days, rule.start, rule.end, rule.months, rule.display, rule.enabled) = fields
            self._schedule_changed()

        put(after)
        return lambda: put(before)

    def delete_rule(self, rule_id: str) -> Undo | None:
        rule = self.rule(rule_id)
        if rule is None:
            return None
        index = self.rules.index(rule)
        self.rules.remove(rule)
        self._schedule_changed()

        def undo() -> None:
            self.rules.insert(min(index, len(self.rules)), rule)
            self._schedule_changed()

        return undo

    def duplicate_rule(self, rule_id: str) -> tuple[Rule, Undo]:
        """A copy just above the rule (one step higher priority)."""
        rule = self.rule(rule_id)
        copy = Rule(
            self._new_rule_id(),
            rule.playlist,
            list(rule.days),
            rule.start,
            rule.end,
            list(rule.months),
            rule.display,
            rule.enabled,
        )
        self.rules.insert(self.rules.index(rule) + 1, copy)
        self._schedule_changed()

        def undo() -> None:
            if copy in self.rules:
                self.rules.remove(copy)
                self._schedule_changed()

        return copy, undo

    def reorder_rules(self, rule_ids: list[str]) -> Undo | None:
        """Store a new priority order, lowest first (a later rule wins). None when
        the order is unchanged, or when it doesn't list exactly the current rules
        (a move that settled after a rule was added or deleted)."""
        before = list(self.rules)
        if sorted(rule_ids) != sorted(rule.id for rule in before):
            return None
        order = [self.rule(rule_id) for rule_id in rule_ids]
        if before == order:
            return None
        self.rules[:] = order
        self._schedule_changed()

        def undo() -> None:
            self.rules[:] = before
            self._schedule_changed()

        return undo

    def set_rule_enabled(self, rule_id: str, enabled: bool) -> None:
        self.rule(rule_id).enabled = enabled
        self._schedule_changed()

    def set_fallback(self, pid: str) -> Undo | None:
        """What plays when no rule applies and a display has no playlist of its own."""
        before = self.fallback
        if pid == before:
            return None
        self.fallback = pid
        self._schedule_changed()

        def undo() -> None:
            self.fallback = before
            self._schedule_changed()

        return undo

    # -- displays -------------------------------------------------------------
    def kept_assignments(self) -> dict[str, str]:
        """Display playlists kept aside while the displays are linked."""
        return dict(self._kept_assigned)

    def display_settings(self, connector: str) -> dict:
        """A display's renderer settings (frame rate, sound, scaling, covered)."""
        return dict(self._display_settings.setdefault(connector, dict(data.DISPLAY_SETTINGS_DEFAULT)))

    def display_settings_are_default(self, connector: str) -> bool:
        return self.display_settings(connector) == data.DISPLAY_SETTINGS_DEFAULT

    def displays_snapshot(self) -> tuple:
        """Everything the Displays page can change, for Undo (and demo resets)."""
        return (
            self.display_mode,
            dict(self.current),
            dict(self.manual),
            dict(self.assigned),
            self.playback,
            set(self._held),
            dict(self._kept_assigned),
            self.color_display,
        )

    def _restore_displays(self, snapshot: tuple, emit: bool = True) -> None:
        (mode, current, manual, assigned, playback, held, kept, colors) = snapshot
        self.display_mode = mode
        self.current, self.manual, self.assigned = dict(current), dict(manual), dict(assigned)
        self.playback = playback
        self._held, self._kept_assigned = set(held), dict(kept)
        self.color_display = colors
        if emit:
            self.emit_changed("displays", "now", "playback")

    def _undo_displays(self) -> Undo:
        before = self.displays_snapshot()
        return lambda: self._restore_displays(before)

    def set_display_mode(self, mode: str) -> Undo:
        """ "mirrored": every display shows the lead display's wallpaper, and their own
        playlists are kept aside; "independent": each display plays its own again."""
        undo = self._undo_displays()
        if mode == "mirrored":
            self._link_displays()
        else:
            for connector, pid in self._kept_assigned.items():
                self.assigned[connector] = pid
                # Its own playlist only plays where nothing is scheduled right now.
                if connector not in self.manual and self.resolution(connector).rule is None:
                    self.current[connector] = self.playlist(pid).entries[0]
            self._kept_assigned = {}
        self.display_mode = mode
        self.emit_changed("displays", "now")
        return undo

    def link_displays(self) -> None:
        """Make every display match the lead one (what "Same on all displays" means)."""
        if self._link_displays():
            self.emit_changed("displays", "now")

    def _link_displays(self) -> bool:
        lead, changed = self.lead_connector(), False
        for connector in self.connectors():
            if self.assigned.get(connector):
                self._kept_assigned[connector] = self.assigned[connector]
                self.assigned[connector] = ""
                changed = True
            if connector == lead:
                continue
            if self.current[connector] != self.current[lead]:
                self.current[connector] = self.current[lead]
                changed = True
            if self.manual.get(connector) != self.manual.get(lead):
                if lead in self.manual:
                    self.manual[connector] = self.manual[lead]
                else:
                    self.manual.pop(connector, None)
                changed = True
        if self._held:
            self._held.clear()
            changed = True
        return changed

    def set_display_playlist(self, connector: str, pid: str) -> Undo | None:
        """What a display plays when nothing is scheduled ("" = the default). None
        when that is already its playlist."""
        if self.assigned.get(connector, "") == pid:
            return None
        undo = self._undo_displays()
        self.assigned[connector] = pid
        resolution = self.resolution(connector)
        in_use = resolution.rule is None and connector not in self.manual
        playlist = self.playlist(resolution.playlist)
        if in_use and self.current[connector] not in playlist.entries:
            self.current[connector] = playlist.entries[0]
        self.emit_changed("displays", "now")
        return undo

    def set_color_display(self, connector: str) -> Undo:
        """Which display's wallpaper colors the desktop (each display separately)."""
        before = self.color_display
        self.color_display = connector
        self.emit_changed("displays", "now")

        def undo() -> None:
            self.color_display = before
            self.emit_changed("displays", "now")

        return undo

    def toggle_display_pause(self, connector: str) -> None:
        """Pause or resume one display; the player bar's own Pause covers them all."""
        if self.playback == "stopped":
            self._held.clear()
            self.playback = "playing"
            self.emit_changed("playback")
            return
        if self.display_mode == "mirrored":
            self.toggle_play()
            return
        connectors = set(self.connectors())
        if self.display_paused(connector):
            if self.playback != "playing":
                self.playback = "playing"
                self._held = connectors - {connector}  # the others stay paused
            else:
                self._held.discard(connector)
        else:
            self._held.add(connector)
            if self._held >= connectors:  # everything paused: that's plain "paused"
                self._held.clear()
                self.playback = "paused"
        self.emit_changed("playback")

    def resume_display_playlist(self, connector: str) -> Undo:
        """Drop a display's pick and go back to its own playlist (nothing is scheduled)."""
        undo = self._undo_displays()
        self.manual.pop(connector, None)
        self.current[connector] = self.playlist(self.assigned[connector]).entries[0]
        self.emit_changed("now", "playback")
        return undo

    def set_display_setting(self, connector: str, key: str, value) -> None:
        self._display_settings.setdefault(connector, dict(data.DISPLAY_SETTINGS_DEFAULT))[key] = value
        self.emit_changed("display-settings")

    def reset_display_settings(self, connector: str) -> Undo:
        """Use the app's defaults on a display again."""
        before = self.display_settings(connector)
        self._display_settings[connector] = dict(data.DISPLAY_SETTINGS_DEFAULT)
        self.emit_changed("display-settings")

        def undo() -> None:
            self._display_settings[connector] = before
            self.emit_changed("display-settings")

        return undo

    def forget_display(self, connector: str) -> Undo:
        """Stop remembering a disconnected display, with its playlist and settings."""
        item = next(item for item in self.remembered_displays if item.connector == connector)
        index = self.remembered_displays.index(item)
        self.remembered_displays.remove(item)
        self.emit_changed("displays")

        def undo() -> None:
            self.remembered_displays.insert(index, item)
            self.emit_changed("displays")

        return undo

    # -- settings -------------------------------------------------------------
    @property
    def template_ok(self) -> bool:
        """Noctalia's template is installed (a reinstall in progress counts)."""
        return self.template_status != "missing"

    def setting(self, key: str):
        """A preference the rest of the demo doesn't act on (Settings rows only)."""
        return self.preferences[key]

    def runtime_log(self) -> list[str]:
        return list(data.RUNTIME_LOG) + list(data.RUNTIME_LOG_EXTRA)

    def set_setting(self, key: str, value) -> None:
        self.preferences[key] = value
        self.emit_changed("preferences")

    def set_default_interval(self, minutes: int) -> None:
        self.default_interval = minutes
        self.emit_changed("settings")

    def set_stop_on_battery(self, stop: bool) -> None:
        self.stop_on_battery = stop
        self.emit_changed("settings", "system", "playback")

    def set_default_scheme(self, scheme: str) -> Undo:
        """The scheme wallpapers without their own use; cards, the inspector and the
        app tint follow it (data.scheme_swatches reads it at call time)."""
        before = data.DEFAULT_SCHEME
        data.DEFAULT_SCHEME = scheme
        self.emit_changed("settings", "library")
        return lambda: self.set_default_scheme(before)

    def set_desktop_colors(self, on: bool) -> None:
        """Whether wallpapers recolor the desktop (Noctalia's palette)."""
        if on != self.desktop_colors:
            self.desktop_colors = on
            self.emit_changed("settings")

    def set_template_status(self, status: str) -> None:
        """Noctalia's template: "working", "busy" (being reinstalled) or "missing"."""
        was_ok = self.template_ok
        self.template_status = status
        if self.template_ok != was_ok:
            self.emit_changed("settings")

    def set_wallhaven_key_saved(self, saved: bool) -> None:
        """Shared with the Store, which offers NSFW only with a key."""
        self.wallhaven_key_saved = saved
        self.emit_changed("settings")

    def set_follow_noctalia_colors(self, follow: bool) -> None:
        """Whether this window takes its colors from the desktop at all."""
        self.follow_noctalia_colors = follow
        self.emit_changed("settings", "playback")

    # -- appearance -----------------------------------------------------------
    # This window's style and glass dials.
    @property
    def background_alpha(self) -> float:
        """Background opacity of the current window style (1.0 when solid)."""
        return self.background_opacity.get(self.window_style, 1.0)

    @background_alpha.setter
    def background_alpha(self, value: float) -> None:
        if self.window_style in self.background_opacity:
            self.background_opacity[self.window_style] = max(0.0, min(1.0, value))

    @property
    def panel_alpha(self) -> float:
        """Panel and element opacity of the current window style (1.0 when solid)."""
        return self.panel_opacity.get(self.window_style, 1.0)

    @panel_alpha.setter
    def panel_alpha(self, value: float) -> None:
        if self.window_style in self.panel_opacity:
            self.panel_opacity[self.window_style] = max(0.0, min(1.0, value))

    def set_window_style(self, style: str) -> None:
        """ "solid", "translucent" (the compositor shows the desktop behind) or "frosted"."""
        self.window_style = style
        self.emit_changed("appearance")

    def set_background_opacity(self, value: float) -> None:
        """The current glass style's page background opacity, 0–1."""
        self.background_alpha = value
        self.emit_changed("appearance")

    def set_panel_opacity(self, value: float) -> None:
        """The current glass style's panel and card opacity, 0–1."""
        self.panel_alpha = value
        self.emit_changed("appearance")

    def set_frost(self, value: float) -> None:
        """How strongly the frosted style blurs the wallpaper, 0 (clear) – 1."""
        self.frost = value
        self.emit_changed("appearance")

    def set_thumbnail_size(self, size: str) -> None:
        """The Library's card size ("large" or "small"); the page rebuilds itself."""
        self.thumbnail_size = size

    def appearance_blocked(self) -> str:
        return ""

    # -- library folders ------------------------------------------------------
    def add_library_folder(self, path: str) -> Undo:
        """Add a folder; it shows as scanning for a moment."""
        folder = Folder(path, "38 files", scanning=True)
        self.folders.append(folder)
        self.emit_changed("folders")

        def scanned() -> bool:
            folder.scanning = False
            if folder in self.folders:
                self.emit_changed("folders")
            return False

        GLib.timeout_add(1600, scanned)

        def undo() -> None:
            if folder in self.folders:
                self.folders.remove(folder)
                self.emit_changed("folders")

        return undo

    def locate_folder(self, path: str, found: str) -> Undo:
        """A missing folder turned up at ``found`` (e.g. a drive mounted elsewhere)."""
        folder = next(folder for folder in self.folders if folder.path == path)
        before = (folder.path, folder.count, folder.missing)
        folder.path, folder.count, folder.missing = found, "2,310 files", False
        self.emit_changed("folders")

        def undo() -> None:
            folder.path, folder.count, folder.missing = before
            self.emit_changed("folders")

        return undo

    def remove_library_folder(self, path: str) -> Undo:
        """Take a folder out of the library (files stay on disk). Removing the
        downloads folder moves downloads to the next one."""
        folder = next(folder for folder in self.folders if folder.path == path)
        index = self.folders.index(folder)
        before = self.download_folder
        self.folders.remove(folder)
        if index == 0 and self.folders:
            self.download_folder = self.folders[0].path
        self.emit_changed("folders", "library")
        if self.download_folder != before:
            self.emit_changed("settings")

        def undo() -> None:
            self.folders.insert(index, folder)
            moved = self.download_folder != before
            self.download_folder = before
            self.emit_changed("folders", "library")
            if moved:
                self.emit_changed("settings")

        return undo

    def set_download_folder(self, path: str) -> Undo:
        """Save downloads and captured stills in ``path``: it becomes the first folder."""
        before = list(self.folders)
        chosen = next(folder for folder in self.folders if folder.path == path)
        # An explicit choice: the chosen folder becomes first. Nothing else moves.
        self.folders.remove(chosen)
        self.folders.insert(0, chosen)
        self.download_folder = chosen.path
        self.emit_changed("folders", "settings")

        def undo() -> None:
            self.folders[:] = before
            self.download_folder = before[0].path
            self.emit_changed("folders", "settings")

        return undo

    # -- demo scenes ----------------------------------------------------------
    # Only the pages' demo() hooks use these (screenshots, the smoke test); a
    # real-app adapter doesn't need them.
    def demo_set_pick(self, connector: str, pid: str, index: int) -> None:
        """A pick of ``pid`` on one display, showing its entry ``index``."""
        self.manual[connector] = pid
        self.current[connector] = self.playlist(pid).entries[index]
        self.emit_changed("now")

    def demo_hold_display(self, connector: str) -> None:
        """One display paused on its own while the others play."""
        self._held.add(connector)
        self.emit_changed("playback")

    def demo_unscheduled(self, connector: str, pid: str) -> None:
        """Nothing scheduled on ``connector`` right now, so its own ``pid`` plays.
        Hides the rules that match now from this state only (rules are shared data)."""
        if self._demo_hidden_rules is None:
            self._demo_hidden_rules = self.rules
        self.rules = [rule for rule in self.rules if not rule_matches(rule, self.now, connector)]
        self.assigned[connector] = pid
        self.current[connector] = self.playlist(pid).entries[0]
        self.emit_changed("schedule", "displays", "now")

    def demo_restore_displays(self, snapshot: tuple, mode: str) -> None:
        """Back to ``snapshot`` (displays_snapshot()) in ``mode``, with the hidden
        rules, display settings and remembered displays restored too."""
        if self._demo_hidden_rules is not None:
            self.rules = self._demo_hidden_rules
            self._demo_hidden_rules = None
        self._restore_displays(snapshot, emit=False)
        self.display_mode = mode
        self._display_settings = {}
        self.remembered_displays = [copy.copy(item) for item in data.REMEMBERED_DISPLAYS]
        if mode == "mirrored":
            self._link_displays()
        self.emit_changed("schedule", "displays", "now", "playback")

    def demo_reset_schedule(self, clear_manual: bool, unassign: list[str]) -> None:
        """The schedule as the demo data has it: the original rules and default, and
        without the picks and display playlists earlier scenes set."""
        self.rules[:] = [copy.deepcopy(rule) for rule in _SEED_RULES]
        self.fallback = data.FALLBACK_PLAYLIST
        if clear_manual:
            self.manual.clear()
        for connector in unassign:
            self.assigned[connector] = ""
        self._schedule_changed()
