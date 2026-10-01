"""The wallpaper inspector: everything about one wallpaper, beside the grid.

A wallpaper *is* its pairing, so the still, the motion and the colors are
edited together here — no separate Pairings page, no modal dialog.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk

from .. import art, data, thumbs, ui
from ..catalog import KIND_LABEL
from ..models import Wallpaper

ui_css = """
.inspector-preview-badges { margin: 10px; }
.scheme-name { font-weight: 700; }
.problem-card {
  border-radius: 12px; padding: 12px;
  background-color: alpha(#e5a50a, 0.14);
  border: 1px solid alpha(#e5a50a, 0.45);
}
.inspector-section { margin-top: 6px; }
.frame-time { font-feature-settings: "tnum"; }
"""


class DesktopPreview(Gtk.DrawingArea):
    """A miniature desktop: the wallpaper with a Noctalia-style bar in the
    colors this wallpaper would apply. Shows the effect, not just the dots."""

    def __init__(self, wallpaper: Wallpaper, colors: list[str]) -> None:
        super().__init__()
        self.wallpaper, self.colors = wallpaper, colors
        self.set_content_height(150)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def update(self, colors: list[str]) -> None:
        self.colors = colors
        self.queue_draw()

    def _draw(self, _area, cr, width, height) -> None:
        radius = 12
        cr.new_sub_path()
        for angle, (x, y) in enumerate(
            ((width - radius, radius), (width - radius, height - radius), (radius, height - radius), (radius, radius))
        ):
            cr.arc(x, y, radius, (angle - 1) * math.pi / 2, angle * math.pi / 2)
        cr.close_path()
        cr.clip()
        w = self.wallpaper
        art._DRAW.get(w.style, art._draw_abstract)(
            cr, random.Random(w.seed * 7919 + 13), width, height, art.look_for(*w.key), w.night
        )

        def color(index: int, alpha: float = 1.0) -> None:
            value = (self.colors[index] if index < len(self.colors) else "#888888").lstrip("#")
            r, g, b = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
            cr.set_source_rgba(r, g, b, alpha)

        def rounded(x, y, rw, rh, rr) -> None:
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
        # Bar
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


class Inspector(Gtk.Box):
    def __init__(self, state, on_close: Callable[[], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.state = state
        self.add_css_class("inspector")
        self.wallpaper: Wallpaper | None = None
        self._on_close = on_close
        ui.add_css(ui_css)

        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False, show_back_button=False)
        header.add_css_class("flat")
        self._header_title = Adw.WindowTitle(title="Details")
        header.set_title_widget(self._header_title)
        header.pack_end(ui.icon_button("window-close-symbolic", "Close details", lambda *_: on_close(), "flat"))
        self.append(header)

        self._scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.append(self._scroller)
        state.connect("changed", self._on_changed)

    def _on_changed(self, _state, topic: str) -> None:
        if not self.wallpaper:
            return
        if topic in ("scope", "displays") and getattr(self, "_apply", None) is not None:
            self._label_apply()
        on_screen = tuple(c for c in self.state.connectors() if self.state.current[c] == self.wallpaper.id)
        # Rebuild for "now" only when this wallpaper's on-screen badge changes.
        if topic in ("playlists", "theme") or (topic == "now" and on_screen != self._on_screen):
            self.show(self.wallpaper)

    # -- building --------------------------------------------------------------
    def show(self, wallpaper: Wallpaper) -> None:
        keep_scroll = self.wallpaper is wallpaper
        scroll = self._scroller.get_vadjustment().get_value() if keep_scroll else 0.0
        self.wallpaper = wallpaper
        self._on_screen = tuple(c for c in self.state.connectors() if self.state.current[c] == wallpaper.id)
        self._sections: dict[str, Gtk.Widget] = {}
        if keep_scroll:
            from gi.repository import GLib

            GLib.idle_add(lambda: (self._scroller.get_vadjustment().set_value(scroll), False)[1])
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
        overlay.set_child(ui.Thumb(thumbs.texture(wallpaper, 960, 540), 360, 203, radius=14, fill=True))
        badges = Gtk.Box(spacing=6, valign=Gtk.Align.START, halign=Gtk.Align.START)
        badges.add_css_class("inspector-preview-badges")
        if wallpaper.is_moving:
            badges.append(ui.kind_badge(wallpaper))
        on = [c for c in self.state.connectors() if self.state.current[c] == wallpaper.id]
        if on:
            badges.append(ui.pill("On " + " & ".join(on), "media-playback-start-symbolic", "accent"))
        overlay.add_overlay(badges)
        if wallpaper.is_moving:
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
                    "media-playback-pause-symbolic" if b.get_active() else "media-playback-start-symbolic"
                ),
            )
            overlay.add_overlay(preview)
        box.append(overlay)

        title = Gtk.Label(label=wallpaper.name, xalign=0, wrap=True)
        title.add_css_class("inspector-title")
        meta_bits = [KIND_LABEL[wallpaper.kind]]
        if wallpaper.duration:
            meta_bits.append(wallpaper.duration)
        if wallpaper.kind != "scene":
            meta_bits.append(wallpaper.resolution)
        meta_bits.append(wallpaper.size)
        meta = ui.dim(" · ".join(meta_bits))
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        titles.append(title)
        titles.append(meta)
        box.append(titles)

        if wallpaper.problem:
            box.append(self._problem(wallpaper))
        box.append(self._actions(wallpaper))
        if wallpaper.is_moving:
            # A plain image is its own still; only moving wallpapers need one.
            box.append(self._still(wallpaper))
            box.append(self._motion(wallpaper))
        box.append(self._colors(wallpaper))
        box.append(self._playlists(wallpaper))
        box.append(self._file(wallpaper))
        self._scroller.set_child(box)

    def scroll_to(self, section: str) -> None:
        """Scroll so a section ("still", "motion", "colors", ...) is at the top."""
        from gi.repository import GLib, Graphene

        def later() -> bool:
            target = self._sections.get(section)
            content = self._scroller.get_child()
            if target is None or content is None:
                return False
            ok, point = target.compute_point(content, Graphene.Point().init(0, 0))
            if ok:
                self._scroller.get_vadjustment().set_value(max(0, point.y - 8))
            return False

        GLib.timeout_add(150, later)

    def _section(self, title: str, child: Gtk.Widget, extra: Gtk.Widget | None = None) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("inspector-section")
        self._sections[title.lower()] = box
        row = Gtk.Box(spacing=6)
        label = Gtk.Label(label=title.upper(), xalign=0, hexpand=True)
        label.add_css_class("section-label")
        row.append(label)
        if extra:
            row.append(extra)
        box.append(row)
        box.append(child)
        return box

    def _problem(self, wallpaper: Wallpaper) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.add_css_class("problem-card")
        top = Gtk.Box(spacing=8)
        icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        top.append(icon)
        heading = Gtk.Label(label="Skipped after a playback problem", xalign=0)
        heading.add_css_class("heading")
        top.append(heading)
        card.append(top)
        card.append(ui.dim(wallpaper.problem))
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

    def _retry(self, wallpaper: Wallpaper) -> None:
        wallpaper.problem = ""
        self.state.emit_changed("library")
        self.show(wallpaper)
        self.state.toast(f"“{wallpaper.name}” will be tried again")

    def _actions(self, wallpaper: Wallpaper) -> Gtk.Widget:
        row = Gtk.Box(spacing=8)
        menu = Gio.Menu()
        menu.append("All displays", f"lib.apply::{wallpaper.id}|all")
        for display in self.state.displays:
            menu.append(f"{display.connector} only — {display.model}", f"lib.apply::{wallpaper.id}|{display.connector}")
        apply = Adw.SplitButton(menu_model=menu, hexpand=True)
        apply.add_css_class("suggested-action")
        apply.add_css_class("pill")
        apply.set_dropdown_tooltip("Choose a display")
        apply.connect("clicked", lambda *_: self.state.apply(wallpaper.id, self.state.scope))
        self._apply = apply
        self._label_apply()
        row.append(apply)

        fav = Gtk.ToggleButton(
            icon_name="starred-symbolic" if wallpaper.favorite else "non-starred-symbolic", active=wallpaper.favorite
        )
        fav.add_css_class("circular")
        fav.set_tooltip_text("Favorite")

        def flip(button: Gtk.ToggleButton) -> None:
            self.state.toggle_favorite(wallpaper.id)
            button.set_icon_name("starred-symbolic" if wallpaper.favorite else "non-starred-symbolic")

        fav.connect("toggled", flip)
        row.append(fav)
        return row

    def _label_apply(self) -> None:
        """Name the target: the player bar's scope, like Apply on the cards."""
        state = self.state
        where = "all displays" if state.scope == "all" or state.display_mode == "mirrored" else state.scope
        self._apply.set_label(f"Apply to {where}")
        self._apply.set_tooltip_text(f"Show it now on {where} (the player bar's choice)")

    def _playlist_menu(self, wallpaper: Wallpaper) -> Gio.Menu:
        menu = Gio.Menu()
        for playlist in self.state.playlists:
            if not playlist.automatic:
                menu.append(playlist.name, f"lib.add::{wallpaper.id}|{playlist.id}")
        new = Gio.Menu()
        new.append("New playlist…", "win.new-playlist")
        menu.append_section(None, new)
        return menu

    def _still(self, wallpaper: Wallpaper) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        row = Gtk.Box(spacing=12)
        row.append(ui.thumbnail(wallpaper, 112, 63, 8))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=2)
        text.append(
            Gtk.Label(
                label="Shown when paused, stopped or on battery" if wallpaper.is_moving else "Shown as your wallpaper",
                xalign=0,
                wrap=True,
            )
        )
        text.append(ui.dim(wallpaper.still_note))
        row.append(text)
        box.append(row)
        if wallpaper.kind == "video":
            frame_row = Gtk.Box(spacing=8)
            seconds = int(wallpaper.duration.split(":")[0]) * 60 + int(wallpaper.duration.split(":")[1])
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, max(1, seconds), 1)
            scale.set_value(3)
            scale.set_hexpand(True)
            scale.set_draw_value(False)
            time_label = Gtk.Label(label="0:03")
            time_label.add_css_class("frame-time")
            time_label.add_css_class("dimmed")
            scale.connect(
                "value-changed",
                lambda s: time_label.set_label(f"{int(s.get_value()) // 60}:{int(s.get_value()) % 60:02d}"),
            )
            use = Gtk.Button(label="Use frame")
            use.add_css_class("pill")
            use.set_tooltip_text("Take the still from this moment of the video")
            use.connect("clicked", lambda *_: self.state.toast(f"New still captured at {time_label.get_label()}"))
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

    def _motion(self, wallpaper: Wallpaper) -> Gtk.Widget:
        group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        group.add_css_class("boxed-list")
        group.append(Adw.SwitchRow(title="Animate", subtitle="Off shows the still instead", active=True))
        group.append(Adw.SwitchRow(title="Sound", subtitle="Muted by default", active=False))
        if wallpaper.kind == "scene":
            fps = Adw.ComboRow(title="Frame rate", subtitle="Lower saves power")
            fps.set_model(Gtk.StringList.new(["Use app setting (30 fps)", "15 fps", "24 fps", "30 fps", "60 fps"]))
            group.append(fps)
        return self._section("Motion", group)

    def _colors(self, wallpaper: Wallpaper) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        preview = DesktopPreview(wallpaper, data.wallpaper_swatches(wallpaper, self.state.dark))
        box.append(preview)

        # Sized to its labels: "From wallpaper" and "Don't change" ellipsize in equal thirds.
        mode = Adw.ToggleGroup(homogeneous=False, can_shrink=False, halign=Gtk.Align.FILL)
        for name, label, tooltip in (
            ("adaptive", "From wallpaper", "Generate desktop colors from this wallpaper"),
            ("palette", "Palette", "Use a named palette such as Nord or Catppuccin"),
            ("keep", "Don't change", "Leave the desktop colors as they are"),
        ):
            toggle = Adw.Toggle(name=name, label=label)
            toggle.set_tooltip(tooltip)
            mode.add(toggle)
        mode.set_active_name(wallpaper.color_mode)
        box.append(mode)
        detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(detail)

        def refresh_detail() -> None:
            child = detail.get_first_child()
            while child:
                detail.remove(child)
                child = detail.get_first_child()
            preview.update(data.wallpaper_swatches(wallpaper, self.state.dark))
            if wallpaper.color_mode == "adaptive":
                detail.append(self._scheme_grid(wallpaper, refresh_detail))
            elif wallpaper.color_mode == "palette":
                detail.append(self._palette_list(wallpaper, refresh_detail))
            else:
                detail.append(ui.dim("When this wallpaper shows, your desktop keeps its current colors."))

        def on_mode(group: Adw.ToggleGroup, _param) -> None:
            wallpaper.color_mode = group.get_active_name()
            if wallpaper.color_mode == "palette" and not wallpaper.palette:
                wallpaper.palette = "Catppuccin"
            refresh_detail()
            self.state.emit_changed("library")

        mode.connect("notify::active-name", on_mode)
        refresh_detail()

        theme_row = Gtk.Box(spacing=12)
        theme_row.append(Gtk.Label(label="Light or dark", xalign=0, hexpand=True))
        theme = Adw.ToggleGroup()
        for name, label, tooltip in (
            ("auto", "Auto", "Follow the time of day"),
            ("light", "Light", None),
            ("dark", "Dark", None),
            ("keep", "Don't change", "Leave the current mode"),
        ):
            toggle = Adw.Toggle(name=name, label=label)
            if tooltip:
                toggle.set_tooltip(tooltip)
            theme.add(toggle)
        theme.set_active_name(wallpaper.theme_mode)
        theme.connect("notify::active-name", lambda g, _p: setattr(wallpaper, "theme_mode", g.get_active_name()))
        theme_row.append(theme)
        box.append(theme_row)
        return self._section("Colors", box)

    def _scheme_grid(self, wallpaper: Wallpaper, refresh: Callable[[], None]) -> Gtk.Widget:
        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=True,
            min_children_per_line=2,
            max_children_per_line=2,
            column_spacing=8,
            row_spacing=8,
        )
        options: list[tuple[str | None, str, str]] = [
            (None, "Default", f"{data.SCHEME_NAME[self.state.default_scheme]} · from Settings")
        ]
        options += [(key, name, description) for key, name, description in data.SCHEMES]
        for key, name, description in options:
            button = Gtk.Button()
            button.add_css_class("choice-card")
            button.add_css_class("flat")
            if wallpaper.scheme == key:
                button.add_css_class("selected")
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            content.append(
                ui.Swatches(
                    data.scheme_swatches(wallpaper, key or self.state.default_scheme, self.state.dark)[:5], size=14
                )
            )
            label = Gtk.Label(label=name, xalign=0)
            label.add_css_class("scheme-name")
            content.append(label)
            sub = Gtk.Label(label=description, xalign=0, wrap=True, lines=2, ellipsize=3)
            sub.add_css_class("dimmed")
            sub.add_css_class("caption")
            content.append(sub)
            button.set_child(content)
            button.set_tooltip_text(
                description if key else f"Follow the default scheme set in Settings ({description.split(' · ')[0]})"
            )

            def choose(_button, scheme=key) -> None:
                wallpaper.scheme = scheme
                refresh()
                self.state.emit_changed("library")

            button.connect("clicked", choose)
            flow.append(button)
        return flow

    def _palette_list(self, wallpaper: Wallpaper, refresh: Callable[[], None]) -> Gtk.Widget:
        group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        group.add_css_class("boxed-list")
        for name, colors in data.PALETTES.items():
            row = Adw.ActionRow(title=name, activatable=True)
            row.add_prefix(ui.Swatches(colors, size=14, overlap=True))
            if wallpaper.palette == name:
                check = Gtk.Image.new_from_icon_name("object-select-symbolic")
                check.add_css_class("accent")
                row.add_suffix(check)

            def choose(_row, palette=name) -> None:
                wallpaper.palette = palette
                refresh()
                self.state.emit_changed("library")

            row.connect("activated", choose)
            group.append(row)
        community = Adw.ExpanderRow(title="Community palettes", subtitle="From Noctalia's online catalog")
        for name in data.COMMUNITY_PALETTES:
            community.add_row(Adw.ActionRow(title=name, activatable=True))
        group.append(community)
        return group

    def _playlists(self, wallpaper: Wallpaper) -> Gtk.Widget:
        wrap = Adw.WrapBox(child_spacing=6, line_spacing=6)
        member = [p for p in self.state.playlists if wallpaper.id in p.entries and not p.automatic]
        for playlist in member:
            button = Gtk.Button(label=playlist.name)
            button.add_css_class("pill")
            button.add_css_class("chip")
            button.set_tooltip_text(f"Open “{playlist.name}”")
            button.connect("clicked", lambda *_, pid=playlist.id: self.state.navigate(f"playlist:{pid}"))
            wrap.append(button)
        add = Gtk.MenuButton(label="Add…", menu_model=self._playlist_menu(wallpaper))
        add.add_css_class("flat")
        add.add_css_class("chip")
        wrap.append(add)
        if not member:
            empty = ui.dim("Not in any playlist yet.")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.append(empty)
            box.append(wrap)
            return self._section("In playlists", box)
        return self._section("In playlists", wrap)

    def _file(self, wallpaper: Wallpaper) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        group = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        group.add_css_class("boxed-list")
        location = Adw.ActionRow(title="Location", subtitle=wallpaper.folder, subtitle_selectable=True)
        location.add_suffix(ui.icon_button("folder-open-symbolic", "Open folder", None, "flat"))
        group.append(location)
        source = {"Local": "Your folder", "Workshop": "Wallpaper Engine (Steam Workshop)"}.get(
            wallpaper.source, f"Downloaded from {wallpaper.source}"
        )
        group.append(Adw.ActionRow(title="Source", subtitle=source))
        group.append(Adw.ActionRow(title="Added", subtitle=wallpaper.added))
        box.append(group)
        remove = Gtk.Button(label="Remove from library…")
        remove.add_css_class("flat")
        remove.add_css_class("error")
        remove.set_halign(Gtk.Align.START)
        remove.connect("clicked", lambda *_: self._confirm_remove(wallpaper))
        box.append(remove)
        return self._section("File", box)

    def _confirm_remove(self, wallpaper: Wallpaper) -> None:
        owned = wallpaper.source in ("Wallhaven", "MotionBGS")
        dialog = Adw.AlertDialog(
            heading=f"Remove “{wallpaper.name}”?",
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

        def done(_dialog, response: str) -> None:
            if response == "cancel":
                return
            undo = self.state.remove_wallpapers([wallpaper])
            self._on_close()
            where = "moved to the Trash" if response == "trash" else "hidden from the library"
            self.state.toast(f"“{wallpaper.name}” {where}", undo)

        dialog.connect("response", done)
        dialog.present(self.get_root())
