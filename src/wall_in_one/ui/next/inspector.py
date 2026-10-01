"""The wallpaper inspector: everything about one wallpaper, beside the grid.

A wallpaper *is* its pairing, so its still, motion and colors live here, with
Apply and Favorite on top. Without `AppState.editing` the inspector is
read-only below those two: it says which colors a wallpaper uses and where it
is, and leaves out every control that would change it.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any

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
from wall_in_one.ui.next.state import AppState, LibraryEditing, WallpaperView


class DesktopPreview(Gtk.Widget):
    """A miniature desktop: the wallpaper with a Noctalia-style bar in the
    colors this wallpaper would apply. Shows the effect, not just the dots."""

    def __init__(self, wallpaper: WallpaperView, colors: Sequence[str]) -> None:
        super().__init__()
        self.wallpaper = wallpaper
        self.colors = list(colors)
        self._texture: Gdk.Texture | None = None
        self.set_size_request(-1, 150)
        self.set_hexpand(True)
        self.set_overflow(Gtk.Overflow.HIDDEN)

        def deliver(texture: Gdk.Texture | None, preview: DesktopPreview = self) -> None:
            if texture is not None:
                preview._texture = texture
                preview.queue_draw()

        self._texture = thumbs.provider().request(wallpaper, 480, 270, deliver)

    def update(self, colors: Sequence[str]) -> None:
        self.colors = list(colors)
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
            cr.show_text("Desktop colors stay as they are")
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
            self._label_apply()
        if topic in ("library", "system") and self.state.has_wallpaper(wallpaper.id):
            # A new view of the same wallpaper: the favorite, its problem, its colors.
            self.show(self.state.wallpaper(wallpaper.id))
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
        self.wallpaper = wallpaper
        self._on_screen = self._on_screen_now(wallpaper.id)
        self._sections = {}
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
        if wallpaper.is_moving and self._editing is not None:
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
            box.append(self._still(wallpaper))
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
            retry.connect("clicked", lambda *_: self._retry(wallpaper))
            details = Gtk.Button(label="Show log")
            details.add_css_class("flat")
            details.connect("clicked", lambda *_: self.state.navigate("settings:log"))
            buttons.append(retry)
            buttons.append(details)
            card.append(buttons)
        return card

    def _retry(self, wallpaper: WallpaperView) -> None:
        """Forget the problem so playback tries the wallpaper again."""
        if self._editing is None:
            return
        self._editing.retry_wallpaper(wallpaper.id)
        self.show(self.state.wallpaper(wallpaper.id))
        self.state.toast(f"{quoted(wallpaper.name)} will be tried again")

    def _actions(self, wallpaper: WallpaperView) -> Gtk.Box:
        row = Gtk.Box(spacing=8)
        menu = Gio.Menu()
        menu.append("All displays", f"lib.apply::{wallpaper.id}|all")
        for display in self.state.displays:
            label = f"{display.connector} only"
            if display.model:
                label += f" \u2014 {display.model}"
            menu.append(label, f"lib.apply::{wallpaper.id}|{display.connector}")
        apply = Adw.SplitButton(menu_model=menu, hexpand=True)
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
        new.append("New playlist…", "win.new-playlist")
        menu.append_section(None, new)
        return menu

    def _still(self, wallpaper: WallpaperView) -> Gtk.Box:
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
        if wallpaper.kind == "video" and wallpaper.duration:
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
            box.append(frame_row)
        if wallpaper.is_moving:
            other = Gtk.Button(label="Use an image file instead…")
            other.add_css_class("link")
            other.add_css_class("caption")
            other.set_halign(Gtk.Align.START)
            box.append(other)
        return self._section("Still", box)

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
        preview = DesktopPreview(wallpaper, self.state.wallpaper_swatches(wallpaper, _dark()))
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

        def current() -> WallpaperView:
            return self.state.wallpaper(wallpaper.id)

        def refresh_detail() -> None:
            child = detail.get_first_child()
            while child is not None:
                detail.remove(child)
                child = detail.get_first_child()
            shown = current()
            preview.update(self.state.wallpaper_swatches(shown, _dark()))
            if shown.color_mode == "adaptive":
                detail.append(self._scheme_grid(editing, shown, refresh_detail))
            elif shown.color_mode == "palette":
                detail.append(self._palette_list(editing, shown, refresh_detail))
            else:
                detail.append(
                    widgets.dim("When this wallpaper shows, your desktop keeps its current colors.")
                )

        def on_mode(group: Adw.ToggleGroup, _param: object) -> None:
            editing.set_wallpaper_colors(wallpaper.id, mode=group.get_active_name())
            refresh_detail()

        mode_group.connect("notify::active-name", on_mode)
        refresh_detail()

        theme_row = Gtk.Box(spacing=12)
        theme_row.append(Gtk.Label(label="Light or dark", xalign=0, hexpand=True))
        theme = Adw.ToggleGroup()
        for name, label, tip in (
            ("auto", "Auto", "Follow the time of day"),
            ("light", "Light", ""),
            ("dark", "Dark", ""),
            ("keep", "Don\u2019t change", "Leave the current mode"),
        ):
            toggle = Adw.Toggle(name=name, label=label)
            if tip:
                toggle.set_tooltip(tip)
            theme.add(toggle)
        theme.set_active_name(wallpaper.theme_mode)
        theme.connect(
            "notify::active-name",
            lambda g, _p: editing.set_wallpaper_colors(
                wallpaper.id, theme_mode=g.get_active_name()
            ),
        )
        theme_row.append(theme)
        box.append(theme_row)
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

    def _palette_list(
        self, editing: LibraryEditing, wallpaper: WallpaperView, refresh: Callable[[], None]
    ) -> Gtk.ListBox:
        group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        group.add_css_class("boxed-list")
        for palette in editing.palettes("builtin"):
            name = palette.name
            row = Adw.ActionRow(title=name, activatable=True)
            row.add_prefix(widgets.Swatches(palette.strip(True), size=14, overlap=True))
            if wallpaper.palette == name:
                check = Gtk.Image.new_from_icon_name("object-select-symbolic")
                check.add_css_class("accent")
                row.add_suffix(check)

            def choose(_row: Adw.ActionRow, chosen: str = name) -> None:
                editing.set_wallpaper_colors(wallpaper.id, palette=chosen)
                refresh()

            row.connect("activated", choose)
            group.append(row)
        community = Adw.ExpanderRow(
            title="Community palettes", subtitle="From Noctalia\u2019s online catalog"
        )
        for palette in editing.palettes("community"):
            community.add_row(Adw.ActionRow(title=palette.name, activatable=True))
        group.append(community)
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
        if self._editing is not None:
            location.add_suffix(
                widgets.icon_button("folder-open-symbolic", "Open folder", None, "flat")
            )
        group.append(location)
        source = {"Local": "Your folder", "Workshop": "Wallpaper Engine (Steam Workshop)"}.get(
            wallpaper.source, f"Downloaded from {wallpaper.source}"
        )
        group.append(Adw.ActionRow(title="Source", subtitle=source))
        if wallpaper.added:
            group.append(Adw.ActionRow(title="Added", subtitle=wallpaper.added))
        box.append(group)
        if self._editing is not None:
            remove = Gtk.Button(label="Remove from library…")
            remove.add_css_class("flat")
            remove.add_css_class("error")
            remove.set_halign(Gtk.Align.START)
            remove.connect("clicked", lambda *_: self.confirm_remove(wallpaper))
            box.append(remove)
        return self._section("File", box)

    def confirm_remove(self, wallpaper: WallpaperView) -> None:
        editing = self._editing
        if editing is None:
            return
        owned = wallpaper.source in ("Wallhaven", "MotionBGS")
        dialog = Adw.AlertDialog(
            heading=f"Remove {quoted(wallpaper.name)}?",
            body="It will be taken out of every playlist. "
            + (
                "The file was downloaded by Wall-in-One and can be moved to the Trash."
                if owned
                else "Your file stays where it is unless you move it to the Trash."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("hide", "Hide from library")
        dialog.add_response("trash", "Move to Trash")
        dialog.set_response_appearance("trash", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def done(_dialog: Adw.AlertDialog, response: str) -> None:
            if response == "cancel":
                return
            undo = editing.remove_wallpapers([wallpaper.id])
            self._on_close()
            where = "moved to the Trash" if response == "trash" else "hidden from the library"
            self.state.toast(f"{quoted(wallpaper.name)} {where}", undo)

        dialog.connect("response", done)
        root = self.get_root()
        dialog.present(root if isinstance(root, Gtk.Widget) else None)
