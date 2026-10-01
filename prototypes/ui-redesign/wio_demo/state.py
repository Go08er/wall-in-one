"""In-memory app state for the prototype. Every action just updates this object.

Pages observe `changed(topic)` and repaint; nothing talks to a real runtime.
Topics: now, playback, library, playlists, schedule, displays, settings,
system (battery/service banners), theme, appearance (window style and dials),
scope (the player bar's display scope).
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
from gi.repository import Gdk, GObject

from . import art, data
from .models import Display, Palette, Playlist, RememberedDisplay, Rule, Wallpaper

#: An Undo callback, as returned by the actions that pages offer Undo for.
Undo = Callable[[], None]
#: "Leave this field alone" for keyword arguments where None is a real value.
UNCHANGED = object()


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


class AppState(GObject.Object):
    __gsignals__: ClassVar[dict] = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "toast": (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
        "navigate": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self) -> None:
        super().__init__()
        # A fixed demo clock keeps screenshots reproducible: Wednesday afternoon.
        self.now = dt.datetime(2026, 9, 30, 14, 35)
        self.wallpapers = data.WALLPAPERS
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
        self.template_ok = True
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
        self._rng = random.Random(4)

    # -- helpers ------------------------------------------------------------
    @property
    def default_scheme(self) -> str:
        """The adaptive scheme inherited by wallpapers that don't choose one."""
        return data.DEFAULT_SCHEME

    @default_scheme.setter
    def default_scheme(self, value: str) -> None:
        # data.scheme_swatches reads this at call time, so cards, the inspector
        # and the app tint all follow the new default.
        data.DEFAULT_SCHEME = value

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

    # -- colors -------------------------------------------------------------
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

    def wallpaper(self, wid: str) -> Wallpaper:
        return data.BY_ID[wid]

    def playlist(self, pid: str) -> Playlist:
        return data.PLAYLIST_BY_ID[pid]

    def connectors(self) -> list[str]:
        return [display.connector for display in self.displays]

    def targets(self, scope: str | None = None) -> list[str]:
        scope = scope or self.scope
        return self.connectors() if scope == "all" or self.display_mode == "mirrored" else [scope]

    def scope_label(self, scope: str | None = None) -> str:
        scope = scope or self.scope
        if scope == "all" or self.display_mode == "mirrored":
            return "All displays"
        return scope

    # -- schedule -----------------------------------------------------------
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

    # -- actions --------------------------------------------------------------
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
            if playlist_id in data.PLAYLIST_BY_ID:
                self.playlist(playlist_id).shuffle = value
        self.emit_changed("playback", "playlists")

    def shuffle_on(self, scope: str | None = None) -> bool:
        connector = self.targets(scope)[0]
        playlist_id = self.effective_playlist(connector)
        return playlist_id in data.PLAYLIST_BY_ID and self.playlist(playlist_id).shuffle

    # -- library: queries ------------------------------------------------------
    def has_wallpaper(self, wid: str) -> bool:
        return wid in data.BY_ID

    def schemes(self) -> list[tuple[str, str, str]]:
        """Noctalia's color schemes: (key, name, description)."""
        return list(data.SCHEMES)

    def scheme_name(self, key: str) -> str:
        return data.SCHEME_NAME.get(key, "")

    def scheme_swatches(self, wallpaper: Wallpaper, scheme: str | None, dark: bool = True) -> list[str]:
        """[surface, primary, secondary, tertiary, error] for ``wallpaper`` under ``scheme``
        (None = the default scheme)."""
        return data.scheme_swatches(wallpaper, scheme, dark)

    def wallpaper_swatches(self, wallpaper: Wallpaper, dark: bool = True) -> list[str]:
        """The colors ``wallpaper`` puts on the desktop; empty when it keeps them."""
        return data.wallpaper_swatches(wallpaper, dark)

    # -- library: actions -------------------------------------------------------
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

    # -- palettes ---------------------------------------------------------------
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

    def add_to_playlist(self, pid: str, wids: list[str]) -> None:
        playlist = self.playlist(pid)
        playlist.entries.extend(wids)
        self.emit_changed("playlists")
        noun = f"“{self.wallpaper(wids[0]).name}”" if len(wids) == 1 else f"{len(wids)} wallpapers"

        def undo() -> None:
            del playlist.entries[-len(wids) :]
            self.emit_changed("playlists")

        self.toast(f"Added {noun} to “{playlist.name}”", undo)

    # -- playlists: queries -----------------------------------------------------
    def has_playlist(self, pid: str) -> bool:
        return pid in data.PLAYLIST_BY_ID

    def playlist_name(self, pid: str) -> str:
        return data.PLAYLIST_BY_ID[pid].name if pid in data.PLAYLIST_BY_ID else "Missing playlist"

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

    # -- playlists: actions -----------------------------------------------------
    def create_playlist(self, name: str) -> str:
        """A new, empty playlist after the user's others; returns its id."""
        base = name.lower().replace(" ", "-")
        pid, number = base, 2
        while pid in data.PLAYLIST_BY_ID:  # a second "Frog day" must not replace the first
            pid, number = f"{base}-{number}", number + 1
        playlist = Playlist(pid, name, [])
        self.playlists.insert(len([p for p in self.playlists if not p.automatic]), playlist)
        data.PLAYLIST_BY_ID[pid] = playlist
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
        while copy_id in data.PLAYLIST_BY_ID:
            copy_id, number = f"{source.id}-copy-{number}", number + 1
        copy = Playlist(copy_id, name, list(source.entries), interval=source.interval, shuffle=source.shuffle)
        user_count = len([p for p in playlists if not p.automatic])
        playlists.insert(playlists.index(source) + 1 if not source.automatic else user_count, copy)
        data.PLAYLIST_BY_ID[copy_id] = copy
        self.emit_changed("playlists")

        def undo() -> None:
            if copy in playlists:
                playlists.remove(copy)
            data.PLAYLIST_BY_ID.pop(copy_id, None)
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
        data.PLAYLIST_BY_ID.pop(pid, None)
        self.emit_changed("playlists", "schedule", "displays", "now", "playback")

        def undo() -> None:
            playlists.insert(min(position, len(playlists)), playlist)
            data.PLAYLIST_BY_ID[pid] = playlist
            for index, rule in rules:
                self.rules.insert(min(index, len(self.rules)), rule)
            self.assigned.clear()
            self.assigned.update(assigned)
            self.manual.clear()
            self.manual.update(manual)
            self.emit_changed("playlists", "schedule", "displays", "now", "playback")

        return undo

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
        """Store a playlist's entries in a new order (a finished drag or keyboard move)."""
        self.playlist(pid).entries[:] = wids
        self.emit_changed("playlists")

    def move_entries_to_top(self, pid: str, indices: list[int]) -> None:
        entries = self.playlist(pid).entries
        chosen = set(indices)
        picked = [entries[i] for i in sorted(chosen)]
        rest = [wid for i, wid in enumerate(entries) if i not in chosen]
        entries[:] = picked + rest
        self.emit_changed("playlists")

    # -- displays: queries ------------------------------------------------------
    def display(self, connector: str) -> Display:
        return next(display for display in self.displays if display.connector == connector)

    def lead_connector(self) -> str:
        """The display the others follow when they are linked: the primary one."""
        for display in self.displays:
            if display.primary:
                return display.connector
        return self.connectors()[0]

    def display_paused(self, connector: str) -> bool:
        """Paused everywhere, or (each display separately) paused on its own."""
        return self.playback == "paused" or (self.display_mode != "mirrored" and connector in self._held)

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

    # -- displays: actions --------------------------------------------------------
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
                interval = self.playlist(playlist_id).interval if playlist_id in data.PLAYLIST_BY_ID else 0
                self.next_change_minutes += interval or self.default_interval
        self.emit_changed("schedule", "now", "playback", "clock")

    def set_battery(self, value: bool) -> None:
        self.on_battery = value
        self.emit_changed("system", "playback")

    def set_service_running(self, value: bool) -> None:
        self.service_running = value
        self.emit_changed("system", "playback", "now")

    # -- demo scenes ------------------------------------------------------------
    # Used only by the pages' demo() hooks for screenshots and the smoke test;
    # a real-app adapter doesn't need them.
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
