"""In-memory app state for the prototype. Every action just updates this object.

Pages observe `changed(topic)` and repaint; nothing talks to a real runtime.
Topics: now, playback, library, playlists, schedule, displays, settings,
system (battery/service banners), theme, appearance (window style and dials),
scope (the player bar's display scope).
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from gi.repository import GObject

from . import data
from .models import Palette, Playlist, Rule, Wallpaper

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
            self.playback = "playing"
        self.emit_changed("now", "playback")
        where = "all displays" if scope == "all" or self.display_mode == "mirrored" else scope

        def undo() -> None:
            self.current, self.manual = before
            self.emit_changed("now", "playback")

        self.toast(f"“{self.wallpaper(wid).name}” is now on {where}", undo)

    def play_playlist(self, pid: str, scope: str = "all") -> None:
        playlist = self.playlist(pid)
        if not playlist.entries:
            self.toast(f"“{playlist.name}” is empty — add wallpapers first")
            return
        for connector in self.targets(scope):
            self.manual[connector] = pid
            self.current[connector] = playlist.entries[0]
        self.playback = "playing"
        self.emit_changed("now", "playback")
        self.toast(f"Playing “{playlist.name}” until you resume the schedule")

    def resume_schedule(self, scope: str = "all") -> None:
        for connector in self.targets(scope):
            self.manual.pop(connector, None)
            entries = self.playlist(self.effective_playlist(connector)).entries
            self.current[connector] = entries[0]
        self.emit_changed("now", "playback")
        self.toast("Following the schedule again")

    def toggle_play(self) -> None:
        self.playback = "paused" if self.playback == "playing" else "playing"
        self.emit_changed("playback")

    def stop(self) -> None:
        self.playback = "stopped"
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
