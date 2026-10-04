"""The wallpaper inspector: everything about one wallpaper, beside the grid.

A wallpaper *is* its pairing, so its still, motion and colors live here, with
Apply and Favorite on top. Without `AppState.editing` the inspector is
read-only below those two: it says which colors a wallpaper uses and where it
is, and leaves out every control that would change it. With it, controls the
adapter has no backend for (`LibraryEditing.extras`) are left out too, and
while `LibraryEditing.editing_blocked` gives a reason every change is off and
says it.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any, Final

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango

from wall_in_one.ui.next import thumbs, widgets
from wall_in_one.ui.next.catalog import KIND_LABEL, quoted
from wall_in_one.ui.next.state import (
    EXTRA_FRAME,
    EXTRA_LOG,
    EXTRA_MOTION,
    AppState,
    LibraryEditing,
    PaletteView,
    WallpaperView,
    solo_displays,
)

#: Community palettes listed at first; "Show more" adds this many again.
COMMUNITY_PAGE: Final = 24


class DesktopPreview(Gtk.Widget):
    """A miniature desktop: the wallpaper with a Noctalia-style bar in the
    colors this wallpaper would apply. Shows the effect, not just the dots."""

    def __init__(self, wallpaper: WallpaperView, colors: Sequence[str], unknown: str = "") -> None:
        super().__init__()
        self.wallpaper = wallpaper
        self.colors = list(colors)
        #: Said instead of the colors while they are not known.
        self.unknown = unknown
        self._texture: Gdk.Texture | None = None
        self.set_size_request(-1, 150)
        self.set_hexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)

        def deliver(texture: Gdk.Texture | None, preview: DesktopPreview = self) -> None:
            if texture is not None:
                preview._texture = texture
                preview.queue_draw()

        self._texture = thumbs.provider().request(wallpaper, 480, 270, deliver)

    def update(self, colors: Sequence[str], unknown: str = "") -> None:
        self.colors = list(colors)
        self.unknown = unknown
        self.queue_draw()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        bounds = Graphene.Rect().init(0, 0, width, height)
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(bounds, 12)
        snapshot.push_rounded_clip(rounded)
        texture = self._texture
        if texture is None:
            placeholder = thumbs.provider().placeholder(self.wallpaper) or widgets.rgba("#8888884d")
            snapshot.append_color(placeholder, bounds)
        else:
            scale = max(width / texture.get_width(), height / texture.get_height())
            drawn_w, drawn_h = texture.get_width() * scale, texture.get_height() * scale
            area = Graphene.Rect().init(
                (width - drawn_w) / 2, (height - drawn_h) / 2, drawn_w, drawn_h
            )
            snapshot.append_scaled_texture(texture, Gsk.ScalingFilter.LINEAR, area)
        self._draw_desktop(snapshot.append_cairo(bounds), width, height)
        snapshot.pop()

    def _draw_desktop(self, cr: cairo.Context[Any], width: int, height: int) -> None:
        def color(index: int, alpha: float = 1.0) -> None:
            value = (self.colors[index] if index < len(self.colors) else "#888888").lstrip("#")
            r, g, b = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
            cr.set_source_rgba(r, g, b, alpha)

        def rounded(x: float, y: float, rw: float, rh: float, rr: float) -> None:
            cr.new_sub_path()
            cr.arc(x + rw - rr, y + rr, rr, -math.pi / 2, 0)
            cr.arc(x + rw - rr, y + rh - rr, rr, 0, math.pi / 2)
            cr.arc(x + rr, y + rh - rr, rr, math.pi / 2, math.pi)
            cr.arc(x + rr, y + rr, rr, math.pi, 3 * math.pi / 2)
            cr.close_path()

        if not self.colors:
            cr.set_source_rgba(0, 0, 0, 0.45)
            rounded(8, 8, width - 16, 22, 11)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.85)
            cr.select_font_face("sans")
            cr.set_font_size(11)
            cr.move_to(18, 23)
            cr.show_text(self.unknown or "Desktop colors stay as they are")
            return
        # The bar
        color(0, 0.92)
        rounded(8, 8, width - 16, 22, 11)
        cr.fill()
        color(1)
        rounded(14, 12, 54, 14, 7)
        cr.fill()
        for index, x in ((2, 74), (3, 92)):
            color(index)
            cr.arc(x + 6, 19, 5, 0, math.tau)
            cr.fill()
        color(1)
        cr.select_font_face("sans")
        cr.set_font_size(10.5)
        cr.move_to(width / 2 - 14, 23)
        cr.show_text("14:35")
        # A window
        color(0, 0.94)
        rounded(width * 0.18, 42, width * 0.5, height - 58, 10)
        cr.fill()
        color(1)
        rounded(width * 0.18 + 12, 56, width * 0.18, 9, 4.5)
        cr.fill()
        for row in range(3):
            color(2 if row != 1 else 3, 0.55)
            rounded(width * 0.18 + 12, 74 + row * 14, width * (0.34 - 0.06 * row), 6, 3)
            cr.fill()
        color(1)
        rounded(width * 0.18 + width * 0.5 - 64, height - 36, 52, 16, 8)
        cr.fill()
        # A notification
        color(0, 0.94)
        rounded(width * 0.72, 42, width * 0.25, 34, 8)
        cr.fill()
        color(3)
        cr.arc(width * 0.72 + 14, 59, 6, 0, math.tau)
        cr.fill()


def _dark() -> bool:
    """The window's light/dark mode as it is on screen right now."""
    return Adw.StyleManager.get_default().get_dark()


def _palette_label(key: str | None) -> str:
    """A palette key (``community:Nord``) as its name; a bare name stays as it is."""
    if not key:
        return ""
    return key.partition(":")[2] or key


def _unknown_colors(wallpaper: WallpaperView) -> str:
    """What the desktop preview says while a wallpaper's colors are not known."""
    if wallpaper.color_mode == "keep":
        return ""
    if wallpaper.color_mode == "palette":
        return f"{_palette_label(wallpaper.palette)}: colors show once Noctalia applies it"
    # Not "generating": a preview Noctalia cannot make never arrives.
    return "Colors come from this wallpaper when it shows"


class Inspector(Gtk.Box):
    def __init__(self, state: AppState, on_close: Callable[[], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.state = state
        self.add_css_class("inspector")
        self.wallpaper: WallpaperView | None = None
        self._editing: LibraryEditing | None = state.editing
        self._on_close = on_close
        self._apply: Adw.SplitButton | None = None
        self._favorite: Gtk.ToggleButton | None = None
        self._on_screen: tuple[str, ...] = ()
        self._sections: dict[str, Gtk.Widget] = {}
        #: Redraws the colors section in place (``colors``: previews or palettes arrived).
        self._refresh_colors: Callable[[], None] | None = None
        self._community_limit = COMMUNITY_PAGE
        #: The editing controls of the wallpaper shown, while it has them.
        self.color_mode: Adw.ToggleGroup | None = None
        self.theme_mode: Adw.ToggleGroup | None = None
        self.color_detail: Gtk.Box | None = None
        self.reset_button: Gtk.Button | None = None
        self.retry_button: Gtk.Button | None = None
        self.remove_button: Gtk.Button | None = None

        header = Adw.HeaderBar(
            show_start_title_buttons=False, show_end_title_buttons=False, show_back_button=False
        )
        header.add_css_class("flat")
        self._header_title = Adw.WindowTitle(title="Details")
        header.set_title_widget(self._header_title)
        header.pack_end(
            widgets.icon_button(
                "window-close-symbolic", "Close details", lambda *_: on_close(), "flat"
            )
        )
        self.append(header)

        self._scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.append(self._scroller)
        state.connect("changed", self._on_changed)

    def _on_screen_now(self, wid: str) -> tuple[str, ...]:
        current = self.state.current
        return tuple(connector for connector, shown in current.items() if shown == wid)

    def _on_changed(self, _state: AppState, topic: str) -> None:
        wallpaper = self.wallpaper
        if wallpaper is None:
            return
        if topic in ("scope", "displays") and self._apply is not None:
            if topic == "displays":
                # Only targets that mean something now: a display that went
                # away, or that mirroring took over, is no longer offered.
                self._apply.set_menu_model(self._apply_menu(wallpaper))
            self._label_apply()
        if topic in ("library", "system") and self.state.has_wallpaper(wallpaper.id):
            # A new view of the same wallpaper: the favorite, its problem, its colors.
            self.show(self.state.wallpaper(wallpaper.id))
            return
        if topic in ("colors", "settings") and self._refresh_colors is not None:
            # New previews or palettes, or another default scheme in Settings.
            self._refresh_colors()
            return
        # Rebuild for "now" only when this wallpaper's on-screen badge changes.
        on_screen = self._on_screen_now(wallpaper.id)
        if topic in ("playlists", "theme") or (topic == "now" and on_screen != self._on_screen):
            self.show(wallpaper)

    # -- building --------------------------------------------------------------
    # Not GTK's ``show()`` (deprecated in GTK 4): the pages call this name.
    def show(self, wallpaper: WallpaperView) -> None:  # type: ignore[override]
        same = self.wallpaper is not None and self.wallpaper.id == wallpaper.id
        scroll = self._scroller.get_vadjustment().get_value() if same else 0.0
        if not same:
            self._community_limit = COMMUNITY_PAGE
        self.wallpaper = wallpaper
        self._on_screen = self._on_screen_now(wallpaper.id)
        self._sections = {}
        self._refresh_colors = None
        self.color_mode = self.theme_mode = self.color_detail = None
        self.reset_button = self.retry_button = self.remove_button = None
        if same:

            def restore() -> bool:
                self._scroller.get_vadjustment().set_value(scroll)
                return GLib.SOURCE_REMOVE

            # After the new content's first layout, which a higher priority
            # would run ahead of. Nothing waits on it: no spinner ends here.
            GLib.idle_add(restore, priority=GLib.PRIORITY_DEFAULT_IDLE)  # type: ignore[call-arg]
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            margin_start=16,
            margin_end=16,
            margin_bottom=24,
            margin_top=2,
        )

        # Preview with badges
        overlay = Gtk.Overlay()
        overlay.set_child(
            widgets.Thumb.of(wallpaper, 360, 203, radius=14, fill=True, size=(960, 540))
        )
        badges = Gtk.Box(spacing=6, valign=Gtk.Align.START, halign=Gtk.Align.START)
        badges.add_css_class("inspector-preview-badges")
        if wallpaper.is_moving:
            badges.append(widgets.kind_badge(wallpaper))
        if self._on_screen:
            badges.append(
                widgets.pill(
                    "On " + " & ".join(self._on_screen), "media-playback-start-symbolic", "accent"
                )
            )
        overlay.add_overlay(badges)
        if wallpaper.is_moving and self._offers(EXTRA_MOTION):
            preview = Gtk.ToggleButton(icon_name="media-playback-start-symbolic")
            preview.add_css_class("circular")
            preview.add_css_class("on-image")
            preview.set_halign(Gtk.Align.END)
            preview.set_valign(Gtk.Align.END)
            preview.set_margin_end(10)
            preview.set_margin_bottom(10)
            preview.set_tooltip_text("Preview the motion here (does not change your desktop)")
            preview.connect(
                "toggled",
                lambda b: b.set_icon_name(
                    "media-playback-pause-symbolic"
                    if b.get_active()
                    else "media-playback-start-symbolic"
                ),
            )
            overlay.add_overlay(preview)
        box.append(overlay)

        title = Gtk.Label(label=wallpaper.name, xalign=0, wrap=True)
        title.add_css_class("inspector-title")
        meta_bits = [KIND_LABEL.get(wallpaper.kind, wallpaper.kind.capitalize())]
        if wallpaper.duration:
            meta_bits.append(wallpaper.duration)
        if wallpaper.kind != "scene" and wallpaper.resolution:
            meta_bits.append(wallpaper.resolution)
        if wallpaper.size:
            meta_bits.append(wallpaper.size)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        titles.append(title)
        titles.append(widgets.dim(" · ".join(meta_bits)))
        box.append(titles)

        if wallpaper.problem:
            box.append(self._problem(wallpaper))
        box.append(self._actions(wallpaper))
        if wallpaper.is_moving and self._editing is not None:
            # A plain image is its own still; only moving wallpapers need one.
            box.append(self._still(self._editing, wallpaper))
            if self._offers(EXTRA_MOTION):
                box.append(self._motion(wallpaper))
        box.append(self._colors(wallpaper))
        box.append(self._playlists(wallpaper))
        box.append(self._file(wallpaper))
        self._scroller.set_child(box)

    def scroll_to(self, section: str) -> None:
        """Scroll so a section ("still", "motion", "colors", ...) is at the top."""

        def later() -> bool:
            target = self._sections.get(section)
            content = self._scroller.get_child()
            if target is None or content is None:
                return GLib.SOURCE_REMOVE
            ok, point = target.compute_point(content, Graphene.Point().init(0, 0))
            if ok:
                self._scroller.get_vadjustment().set_value(max(0, point.y - 8))
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(150, later)

    def _offers(self, extra: str) -> bool:
        """Whether the adapter backs a control beyond the editing Protocol's methods."""
        return self._editing is not None and extra in self._editing.extras

    def _lock(self, *controls: Gtk.Widget) -> str:
        """Turn ``controls`` off, saying why, while editing is blocked; return the reason."""
        blocked = self._editing.editing_blocked() if self._editing is not None else ""
        for control in controls:
            control.set_sensitive(not blocked)
            if blocked:
                control.set_tooltip_text(blocked)
        return blocked

    def _section(self, title: str, child: Gtk.Widget, extra: Gtk.Widget | None = None) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("inspector-section")
        self._sections[title.lower()] = box
        row = Gtk.Box(spacing=6)
        label = Gtk.Label(label=title.upper(), xalign=0, hexpand=True)
        label.add_css_class("section-label")
        row.append(label)
        if extra is not None:
            row.append(extra)
        box.append(row)
        box.append(child)
        return box

    def _problem(self, wallpaper: WallpaperView) -> Gtk.Box:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("problem-card")
        top = Gtk.Box(spacing=8)
        top.append(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
        heading = Gtk.Label(label="Skipped after a playback problem", xalign=0)
        heading.add_css_class("heading")
        top.append(heading)
        card.append(top)
        card.append(widgets.dim(wallpaper.problem))
        if self._editing is not None:
            buttons = Gtk.Box(spacing=6)
            retry = Gtk.Button(label="Try again")
            retry.add_css_class("pill")
            retry.set_tooltip_text("Forget the problem, so playback tries this wallpaper again")
            retry.connect("clicked", lambda *_: self._retry(wallpaper))
            self._lock(retry)
            self.retry_button = retry
            buttons.append(retry)
            if self._offers(EXTRA_LOG):
                details = Gtk.Button(label="Show log")
                details.add_css_class("flat")
                details.connect("clicked", lambda *_: self.state.navigate("settings:log"))
                buttons.append(details)
            card.append(buttons)
        return card

    def _retry(self, wallpaper: WallpaperView) -> None:
        """Forget the problem so playback tries the wallpaper again (the adapter says so)."""
        if self._editing is None:
            return
        self._editing.retry_wallpaper(wallpaper.id)

    def _actions(self, wallpaper: WallpaperView) -> Gtk.Box:
        row = Gtk.Box(spacing=8)
        apply = Adw.SplitButton(menu_model=self._apply_menu(wallpaper), hexpand=True)
        apply.add_css_class("suggested-action")
        apply.add_css_class("pill")
        apply.set_dropdown_tooltip("Choose a display")
        apply.connect("clicked", lambda *_: self._apply_now(wallpaper))
        self._apply = apply
        self._label_apply()
        row.append(apply)

        fav = Gtk.ToggleButton(
            icon_name="starred-symbolic" if wallpaper.favorite else "non-starred-symbolic",
            active=wallpaper.favorite,
        )
        fav.add_css_class("circular")
        blocked = self.state.favorite_blocked()
        fav.set_tooltip_text(blocked or "Favorite")
        fav.update_property([Gtk.AccessibleProperty.LABEL], ["Favorite"])
        fav.set_sensitive(not blocked)

        reflecting = False

        def flip(button: Gtk.ToggleButton) -> None:
            # The star shows the saved state and follows it when the state says
            # it changed ("library"), never the click itself.
            nonlocal reflecting
            if reflecting:
                return
            reflecting = True
            try:
                button.set_active(wallpaper.favorite)
            finally:
                reflecting = False
            self.state.toggle_favorite(wallpaper.id)

        fav.connect("toggled", flip)
        self._favorite = fav
        row.append(fav)
        return row

    def _apply_now(self, wallpaper: WallpaperView) -> None:
        if not self.state.apply_blocked(wallpaper.id):
            self.state.apply(wallpaper.id, self.state.scope)

    @property
    def apply_button(self) -> Adw.SplitButton | None:
        return self._apply

    @property
    def favorite_button(self) -> Gtk.ToggleButton | None:
        return self._favorite

    def _apply_menu(self, wallpaper: WallpaperView) -> Gio.Menu:
        """Apply's explicit targets: every display, and each one that stands on its own."""
        menu = Gio.Menu()
        menu.append("All displays", f"lib.apply::{wallpaper.id}|all")
        for display in solo_displays(self.state):
            label = f"{display.connector} only"
            if display.model:
                label += f" \u2014 {display.model}"
            menu.append(label, f"lib.apply::{wallpaper.id}|{display.connector}")
        return menu

    def _label_apply(self) -> None:
        """Name the target: the player bar's scope, like Apply on the cards."""
        if self._apply is None or self.wallpaper is None:
            return
        state = self.state
        mirrored = state.scope == "all" or state.display_mode == "mirrored"
        where = "all displays" if mirrored else state.scope
        blocked = state.apply_blocked(self.wallpaper.id)
        self._apply.set_label(f"Apply to {where}")
        self._apply.set_sensitive(not blocked)
        self._apply.set_tooltip_text(
            blocked or f"Show it now on {where} (the player bar\u2019s choice)"
        )

    def _playlist_menu(self, wallpaper: WallpaperView) -> Gio.Menu:
        menu = Gio.Menu()
        for playlist in self.state.playlists:
            if not playlist.automatic:
                menu.append(playlist.name, f"lib.add::{wallpaper.id}|{playlist.id}")
        new = Gio.Menu()
        # Created with this wallpaper already in it, in one save.
        new.append("New playlist\u2026", f"lib.new-playlist::{wallpaper.id}")
        menu.append_section(None, new)
        return menu

    def _still(self, editing: LibraryEditing, wallpaper: WallpaperView) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        row = Gtk.Box(spacing=12)
        row.append(widgets.thumbnail(wallpaper, 112, 63, 8))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=2)
        text.append(
            Gtk.Label(
                label="Shown when paused, stopped or on battery"
                if wallpaper.is_moving
                else "Shown as your wallpaper",
                xalign=0,
                wrap=True,
            )
        )
        text.append(widgets.dim(wallpaper.still_note))
        row.append(text)
        box.append(row)
        if wallpaper.kind == "video" and wallpaper.duration and self._offers(EXTRA_FRAME):
            box.append(self._frame_row(wallpaper))
        choices = editing.choices(wallpaper.id)
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        other = Gtk.Button(label="Use an image file instead\u2026")
        other.add_css_class("link")
        other.add_css_class("caption")
        other.set_halign(Gtk.Align.START)
        other.set_tooltip_text("Choose a picture already in your library")
        other.connect("clicked", lambda *_: self._choose_still_file(editing, wallpaper))
        buttons.append(other)
        controls: list[Gtk.Widget] = [other]
        if choices.still:
            automatic = Gtk.Button(label="Use the automatic still")
            automatic.add_css_class("link")
            automatic.add_css_class("caption")
            automatic.set_halign(Gtk.Align.START)
            automatic.connect("clicked", lambda *_: editing.choose_still(wallpaper.id, None))
            buttons.append(automatic)
            controls.append(automatic)
        if choices.regenerate:
            regenerate = Gtk.Button(label="Capture a new still")
            regenerate.add_css_class("link")
            regenerate.add_css_class("caption")
            regenerate.set_halign(Gtk.Align.START)
            regenerate.set_tooltip_text(
                "Capture the scene again at your display\u2019s size and shape"
            )
            regenerate.connect("clicked", lambda *_: editing.regenerate_still(wallpaper.id))
            buttons.append(regenerate)
            controls.append(regenerate)
        self._lock(*controls)
        box.append(buttons)
        return self._section("Still", box)

    def _frame_row(self, wallpaper: WallpaperView) -> Gtk.Box:
        """The prototype's "Use frame" (no backend in the app yet: `EXTRA_FRAME`)."""
        frame_row = Gtk.Box(spacing=8)
        minutes, _, seconds_part = wallpaper.duration.partition(":")
        seconds = int(minutes or 0) * 60 + int(seconds_part or 0)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, max(1, seconds), 1)
        scale.set_value(3)
        scale.set_hexpand(True)
        scale.set_draw_value(False)
        time_label = Gtk.Label(label="0:03")
        time_label.add_css_class("frame-time")
        time_label.add_css_class("dimmed")
        scale.connect(
            "value-changed",
            lambda s: time_label.set_label(
                f"{int(s.get_value()) // 60}:{int(s.get_value()) % 60:02d}"
            ),
        )
        use = Gtk.Button(label="Use frame")
        use.add_css_class("pill")
        use.set_tooltip_text("Take the still from this moment of the video")
        use.connect(
            "clicked",
            lambda *_: self.state.toast(f"New still captured at {time_label.get_label()}"),
        )
        frame_row.append(scale)
        frame_row.append(time_label)
        frame_row.append(use)
        return frame_row

    def _choose_still_file(self, editing: LibraryEditing, wallpaper: WallpaperView) -> None:
        """A picture from the library as the still (the classic "Choose a still...")."""
        dialog = Gtk.FileDialog(title=f"Choose a still for {wallpaper.name}", modal=True)
        images = Gtk.FileFilter()
        images.set_name("Images")
        # The library's still formats; the adapter checks it is an indexed still.
        for pattern in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.avif"):
            images.add_pattern(pattern)
            images.add_pattern(pattern.upper())
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(images)
        dialog.set_filters(filters)
        dialog.set_default_filter(images)
        root = self.get_root()

        def chosen(source: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                picked = source.open_finish(result)
            except GLib.Error:
                return  # dismissed
            path = picked.get_path() if picked is not None else None
            if path is None:
                self.state.toast("That file is not on this machine\u2019s filesystem")
                return
            editing.choose_still(wallpaper.id, path)

        dialog.open(root if isinstance(root, Gtk.Window) else None, None, chosen)

    def _motion(self, wallpaper: WallpaperView) -> Gtk.Box:
        group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        group.add_css_class("boxed-list")
        group.append(
            Adw.SwitchRow(title="Animate", subtitle="Off shows the still instead", active=True)
        )
        group.append(Adw.SwitchRow(title="Sound", subtitle="Muted by default", active=False))
        if wallpaper.kind == "scene":
            fps = Adw.ComboRow(title="Frame rate", subtitle="Lower saves power")
            fps.set_model(
                Gtk.StringList.new(
                    ["Use app setting (30 fps)", "15 fps", "24 fps", "30 fps", "60 fps"]
                )
            )
            group.append(fps)
        return self._section("Motion", group)

    def _colors(self, wallpaper: WallpaperView) -> Gtk.Box:
        editing = self._editing
        if editing is None:
            # Read-only: say which colors it asks for, and nothing to change them with.
            group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            group.add_css_class("boxed-list")
            group.append(
                Adw.ActionRow(
                    title="Desktop colors", subtitle=widgets.color_summary(self.state, wallpaper)
                )
            )
            mode = {"auto": "Auto", "dark": "Dark", "light": "Light", "keep": "Don\u2019t change"}
            group.append(
                Adw.ActionRow(
                    title="Light or dark",
                    subtitle=mode.get(wallpaper.theme_mode, wallpaper.theme_mode.capitalize()),
                )
            )
            return self._section("Colors", group)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        preview = DesktopPreview(
            wallpaper,
            self.state.wallpaper_swatches(wallpaper, _dark()),
            _unknown_colors(wallpaper),
        )
        box.append(preview)

        # Sized to its labels: "From wallpaper" and "Don't change" ellipsize in equal thirds.
        mode_group = Adw.ToggleGroup(homogeneous=False, can_shrink=False, halign=Gtk.Align.FILL)
        for name, label, tooltip in (
            ("adaptive", "From wallpaper", "Generate desktop colors from this wallpaper"),
            ("palette", "Palette", "Use a named palette such as Nord or Catppuccin"),
            ("keep", "Don\u2019t change", "Leave the desktop colors as they are"),
        ):
            toggle = Adw.Toggle(name=name, label=label)
            toggle.set_tooltip(tooltip)
            mode_group.add(toggle)
        mode_group.set_active_name(wallpaper.color_mode)
        box.append(mode_group)
        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(detail)
        self.color_mode, self.color_detail = mode_group, detail

        def current() -> WallpaperView:
            return self.state.wallpaper(wallpaper.id)

        def refresh_detail() -> None:
            if not self.state.has_wallpaper(wallpaper.id):
                return
            child = detail.get_first_child()
            while child is not None:
                detail.remove(child)
                child = detail.get_first_child()
            shown = current()
            preview.update(self.state.wallpaper_swatches(shown, _dark()), _unknown_colors(shown))
            # The toggle names what to choose from; the view says what is chosen.
            # "Palette" with none chosen yet lists them and changes nothing.
            wanted = mode_group.get_active_name() or shown.color_mode
            if wanted == "adaptive" and shown.color_mode == "adaptive":
                detail.append(self._scheme_grid(editing, shown, refresh_detail))
            elif wanted == "palette":
                if shown.color_mode != "palette":
                    detail.append(
                        widgets.dim("Choose a palette below. Until you do, nothing changes.")
                    )
                detail.append(self._palette_list(editing, shown, refresh_detail))
            elif wanted == "keep" and shown.color_mode == "keep":
                detail.append(
                    widgets.dim("When this wallpaper shows, your desktop keeps its current colors.")
                )

        def on_mode(group: Adw.ToggleGroup, _param: object) -> None:
            chosen = group.get_active_name()
            if chosen and chosen != current().color_mode:
                editing.set_wallpaper_colors(wallpaper.id, mode=chosen)
            refresh_detail()

        mode_group.connect("notify::active-name", on_mode)
        refresh_detail()
        self._refresh_colors = refresh_detail

        theme_row = Gtk.Box(spacing=12)
        theme_row.append(Gtk.Label(label="Light or dark", xalign=0, hexpand=True))
        theme = Adw.ToggleGroup()
        for name, label, tip in (
            ("auto", "Auto", "Let Noctalia decide"),
            ("light", "Light", ""),
            ("dark", "Dark", ""),
            ("keep", "Don\u2019t change", "Leave the current mode"),
        ):
            toggle = Adw.Toggle(name=name, label=label)
            if tip:
                toggle.set_tooltip(tip)
            theme.add(toggle)
        theme.set_active_name(wallpaper.theme_mode)

        def on_theme(group: Adw.ToggleGroup, _param: object) -> None:
            chosen = group.get_active_name()
            if chosen and chosen != current().theme_mode:
                editing.set_wallpaper_colors(wallpaper.id, theme_mode=chosen)

        theme.connect("notify::active-name", on_theme)
        self.theme_mode = theme
        theme_row.append(theme)
        box.append(theme_row)
        controls: list[Gtk.Widget] = [mode_group, detail, theme]
        if editing.choices(wallpaper.id).customized:
            reset = Gtk.Button(label="Reset to defaults")
            reset.add_css_class("flat")
            reset.set_halign(Gtk.Align.START)
            reset.set_tooltip_text("Forget the colors, light or dark and still chosen for it")
            reset.connect("clicked", lambda *_: editing.reset_wallpaper(wallpaper.id))
            self.reset_button = reset
            box.append(reset)
            controls.append(reset)
        self._lock(*controls)
        return self._section("Colors", box)

    def _scheme_grid(
        self, editing: LibraryEditing, wallpaper: WallpaperView, refresh: Callable[[], None]
    ) -> Gtk.FlowBox:
        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=True,
            min_children_per_line=2,
            max_children_per_line=2,
            column_spacing=8,
            row_spacing=8,
        )
        default = self.state.scheme_name(self.state.default_scheme)
        options: list[tuple[str | None, str, str]] = [
            (None, "Default", f"{default} · from Settings")
        ]
        options += editing.schemes()
        for key, name, description in options:
            button = Gtk.Button()
            button.add_css_class("choice-card")
            button.add_css_class("flat")
            if wallpaper.scheme == key:
                button.add_css_class("selected")
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            colors = editing.scheme_swatches(wallpaper, key or self.state.default_scheme, _dark())
            content.append(widgets.Swatches(colors[:5], size=14))
            label = Gtk.Label(label=name, xalign=0)
            label.add_css_class("scheme-name")
            content.append(label)
            sub = Gtk.Label(
                label=description, xalign=0, wrap=True, lines=2, ellipsize=Pango.EllipsizeMode.END
            )
            sub.add_css_class("dimmed")
            sub.add_css_class("caption")
            content.append(sub)
            button.set_child(content)
            button.set_tooltip_text(
                description
                if key
                else f"Follow the default scheme set in Settings ({description.split(' · ')[0]})"
            )

            def choose(_button: Gtk.Button, scheme: str | None = key) -> None:
                editing.set_wallpaper_colors(wallpaper.id, scheme=scheme)
                refresh()

            button.connect("clicked", choose)
            flow.append(button)
        return flow

    def _palette_row(
        self,
        editing: LibraryEditing,
        wallpaper: WallpaperView,
        palette: PaletteView,
        refresh: Callable[[], None],
    ) -> Adw.ActionRow:
        row = Adw.ActionRow(title=palette.name, activatable=True)
        strip = palette.strip(_dark())
        if strip:
            row.add_prefix(widgets.Swatches(strip, size=14, overlap=True))
        else:
            row.set_subtitle("Colors show once Noctalia applies it")
        if wallpaper.color_mode == "palette" and wallpaper.palette == palette.key:
            check = Gtk.Image.new_from_icon_name("object-select-symbolic")
            check.add_css_class("accent")
            row.add_suffix(check)

        def choose(_row: Adw.ActionRow, chosen: str = palette.key) -> None:
            editing.set_wallpaper_colors(wallpaper.id, palette=chosen)
            refresh()

        row.connect("activated", choose)
        return row

    def _palette_list(
        self, editing: LibraryEditing, wallpaper: WallpaperView, refresh: Callable[[], None]
    ) -> Gtk.ListBox:
        group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        group.add_css_class("boxed-list")
        listed: set[str] = set()
        for palette in editing.palettes("builtin"):
            group.append(self._palette_row(editing, wallpaper, palette, refresh))
            listed.add(palette.key)
        custom = list(editing.palettes("custom"))
        if custom:
            mine = Adw.ExpanderRow(
                title="Your palettes", subtitle="Saved in Noctalia\u2019s folder"
            )
            for palette in custom:
                mine.add_row(self._palette_row(editing, wallpaper, palette, refresh))
                listed.add(palette.key)
            mine.set_expanded(any(p.key == wallpaper.palette for p in custom))
            group.append(mine)
        community = list(editing.palettes("community"))
        if community:
            expander = Adw.ExpanderRow(
                title="Community palettes", subtitle="From Noctalia\u2019s online catalog"
            )
            shown = community[: self._community_limit]
            chosen = next(
                (p for p in community[self._community_limit :] if p.key == wallpaper.palette),
                None,
            )
            for palette in [*shown, *([chosen] if chosen is not None else [])]:
                expander.add_row(self._palette_row(editing, wallpaper, palette, refresh))
                listed.add(palette.key)
            remaining = len(community) - len(shown)
            if remaining > 0:
                more = Adw.ButtonRow(title=f"Show {min(COMMUNITY_PAGE, remaining)} more")

                def show_more(_row: Adw.ButtonRow) -> None:
                    self._community_limit += COMMUNITY_PAGE
                    refresh()

                more.connect("activated", show_more)
                expander.add_row(more)
            expander.set_expanded(any(p.key == wallpaper.palette for p in community))
            group.append(expander)
        if wallpaper.color_mode == "palette" and wallpaper.palette not in listed:
            # A choice whose palette is not installed right now stays visible as chosen.
            missing = Adw.ActionRow(
                title=_palette_label(wallpaper.palette),
                subtitle="Chosen, but not among the installed palettes right now",
            )
            check = Gtk.Image.new_from_icon_name("object-select-symbolic")
            check.add_css_class("accent")
            missing.add_suffix(check)
            group.append(missing)
        if not listed and wallpaper.color_mode != "palette":
            group.append(Adw.ActionRow(title="Looking for installed palettes…"))
        return group

    def _playlists(self, wallpaper: WallpaperView) -> Gtk.Box:
        wrap = Adw.WrapBox(child_spacing=6, line_spacing=6)
        member = [p for p in self.state.playlists if wallpaper.id in p.entries and not p.automatic]
        for playlist in member:
            button = Gtk.Button(label=playlist.name)
            button.add_css_class("pill")
            button.add_css_class("chip")
            button.set_tooltip_text(f"Open {quoted(playlist.name)}")
            button.connect(
                "clicked", lambda *_, pid=playlist.id: self.state.navigate(f"playlist:{pid}")
            )
            wrap.append(button)
        if self._editing is not None:
            add = Gtk.MenuButton(label="Add…", menu_model=self._playlist_menu(wallpaper))
            add.add_css_class("flat")
            add.add_css_class("chip")
            self._lock(add)
            wrap.append(add)
        if not member:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.append(widgets.dim("Not in any playlist yet."))
            box.append(wrap)
            return self._section("In playlists", box)
        return self._section("In playlists", wrap)

    def _file(self, wallpaper: WallpaperView) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        group.add_css_class("boxed-list")
        location = Adw.ActionRow(
            title="Location", subtitle=wallpaper.folder, subtitle_selectable=True
        )
        editing = self._editing
        if editing is not None:
            location.add_suffix(
                widgets.icon_button(
                    "folder-open-symbolic",
                    "Show in Files",
                    lambda *_: editing.show_in_files(wallpaper.id),
                    "flat",
                )
            )
        group.append(location)
        source = {"Local": "Your folder", "Workshop": "Wallpaper Engine (Steam Workshop)"}.get(
            wallpaper.source, f"Downloaded from {wallpaper.source}"
        )
        group.append(Adw.ActionRow(title="Source", subtitle=source))
        if wallpaper.added:
            group.append(Adw.ActionRow(title="Added", subtitle=wallpaper.added))
        box.append(group)
        if editing is not None:
            removal = editing.removal(wallpaper.id)
            if removal.how:
                label = "Delete from disk…" if removal.how == "delete" else "Move to Trash"
                remove = Gtk.Button(label=label)
                remove.add_css_class("flat")
                remove.add_css_class("error")
                remove.set_halign(Gtk.Align.START)
                remove.set_tooltip_text(
                    "Wall-in-One downloaded it: deleted for good, with its favorite, colors "
                    "and playlist entries"
                    if removal.how == "delete"
                    else "Your own file: moved to the Trash, with its favorite, colors and "
                    "playlist entries removed"
                )
                remove.connect("clicked", lambda *_: self.confirm_remove(wallpaper))
                self._lock(remove)
                self.remove_button = remove
                box.append(remove)
            else:
                box.append(widgets.dim(f"Wall-in-One can\u2019t remove it. {removal.reason}"))
        return self._section("File", box)

    def confirm_remove(self, wallpaper: WallpaperView) -> None:
        """Remove by who owns the file: ask before deleting for good, never before the Trash.

        Asking about everything trains people to confirm everything, so the
        question is kept for the one removal nothing can undo.
        """
        editing = self._editing
        if editing is None:
            return
        removal = editing.removal(wallpaper.id)
        if not removal.how:
            self.state.toast(
                f"{quoted(wallpaper.name)} can\u2019t be removed here. {removal.reason}"
            )
            return
        if removal.how == "trash":
            self._remove_now(editing, wallpaper, "moved to the Trash")
            return
        dialog = Adw.AlertDialog(
            heading=f"Delete {quoted(wallpaper.name)}?",
            body="Wall-in-One downloaded it, so it is deleted from disk for good, along with "
            "its favorite, colors and playlist entries. This cannot be undone.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def done(_dialog: Adw.AlertDialog, response: str) -> None:
            if response == "delete":
                self._remove_now(editing, wallpaper, "deleted")

        dialog.connect("response", done)
        root = self.get_root()
        dialog.present(root if isinstance(root, Gtk.Widget) else None)

    def _remove_now(self, editing: LibraryEditing, wallpaper: WallpaperView, done: str) -> None:
        undo = editing.remove_wallpapers([wallpaper.id])
        if undo is not None:  # already done: say so here (else the adapter reports)
            self._on_close()
            self.state.toast(f"{quoted(wallpaper.name)} {done}", undo)
        # Otherwise the details close when it leaves the library (the Library
        # page sees to that), and stay open if the removal is refused.
