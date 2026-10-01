"""Store widgets: result cards, the download ring and the preview dialog.

They stay dumb views. A *controller* (the Store page) owns downloads and
navigation and offers: ``progress_of(item)``, ``download(item, apply=False,
quality=None)``, ``cancel(item)``, ``apply_item(item)``, ``show_in_library(item)``,
``quality_of(item)``, ``open_site(item)``, ``open_item(item)``, ``preview(item)``, ``popup_menu(card, x, y)``,
``search_for(text)``, ``more_like(item)``, ``search_color(hex)`` and ``dialog_closed(dialog)``.
"""

from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

from .. import ui
from ..models import StoreItem
from . import store_catalog as catalog

CSS = """
.store-card .store-owned image { color: #8ff0a4; }
.store-scrim { background-color: alpha(black, 0.48); border-radius: 12px; }
button.store-cancel { min-width: 30px; min-height: 30px; padding: 0;
  background-color: alpha(black, 0.35); color: white; }
button.store-cancel:hover { background-color: alpha(black, 0.6); }
button.store-download { min-width: 30px; min-height: 30px; padding: 0; }
.store-detail { font-size: 0.86em; }
.store-detail.busy { color: var(--accent-color); font-weight: 700; }
.store-detail image { -gtk-icon-size: 12px; }
.store-fact-label { font-size: 0.78em; font-weight: 800; letter-spacing: 0.06em; opacity: 0.6; }
.store-fact-value { font-feature-settings: "tnum"; }
.store-preview-bar { padding: 10px 14px; }
.store-preview-badges { margin: 12px; }
button.store-play { min-width: 52px; min-height: 52px; padding: 0; }
button.store-play image { -gtk-icon-size: 22px; }
progressbar.store-motion > trough { min-height: 3px; background-color: alpha(white, 0.25); }
progressbar.store-motion > trough > progress { min-height: 3px; background-color: white; }
button.store-color { min-width: 0; min-height: 0; padding: 2px; border-radius: 999px; }
progressbar.store-dialog-progress > trough { min-height: 6px; border-radius: 3px; }
progressbar.store-dialog-progress > trough > progress { min-height: 6px; border-radius: 3px; }
"""


class ColorDot(Gtk.DrawingArea):
    """A filled color circle; ``selected`` adds a ring in the text color."""

    def __init__(self, hex_color: str, size: int = 24, selected: bool = False) -> None:
        super().__init__()
        self._rgba = ui.rgba("#" + hex_color.lstrip("#"))
        self._selected = selected
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_draw_func(self._draw)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.queue_draw()

    def _draw(self, _area, cr, width, height) -> None:
        cx, cy = width / 2, height / 2
        radius = min(width, height) / 2 - 1
        fg = self.get_color()
        if self._selected:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.9)
            cr.set_line_width(2)
            cr.arc(cx, cy, radius, 0, math.tau)
            cr.stroke()
            radius -= 3.5
        cr.arc(cx, cy, radius, 0, math.tau)
        cr.set_source_rgba(self._rgba.red, self._rgba.green, self._rgba.blue, 1)
        cr.fill_preserve()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.22)
        cr.set_line_width(1)
        cr.stroke()


def color_button(hex_color: str, tooltip: str, size: int = 24, selected: bool = False) -> Gtk.Button:
    button = Gtk.Button(tooltip_text=tooltip)
    button.add_css_class("flat")
    button.add_css_class("store-color")
    button.set_child(ColorDot(hex_color, size, selected))
    button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
    return button


def owned_pill() -> Gtk.Widget:
    badge = ui.pill("In library", "object-select-symbolic", "on-image", "store-owned")
    badge.set_tooltip_text("Already downloaded")
    return badge


def detail_text(item: StoreItem) -> tuple[str, str, str]:
    """(icon, text, tooltip) for the dim line under a card."""
    if item.provider == "MotionBGS":
        return ("media-playlist-repeat-symbolic", catalog.duration(item), f"A {catalog.duration(item)} loop")
    count = catalog.favorites(item)
    return ("starred-symbolic", f"{count:,}", f"{count:,} favorites on Wallhaven")


class ProgressRing(Gtk.DrawingArea):
    """A determinate progress ring, drawn white for use on a dark scrim."""

    def __init__(self, size: int = 46) -> None:
        super().__init__()
        self._fraction = 0.0
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)

    def set_fraction(self, fraction: float) -> None:
        self._fraction = max(0.0, min(1.0, fraction))
        self.queue_draw()

    def _draw(self, _area, cr, width, height) -> None:
        cx, cy = width / 2, height / 2
        radius = min(width, height) / 2 - 3
        cr.set_line_width(3.5)
        cr.set_source_rgba(1, 1, 1, 0.28)
        cr.arc(cx, cy, radius, 0, math.tau)
        cr.stroke()
        if self._fraction > 0:
            cr.set_line_cap(1)  # round
            cr.set_source_rgba(1, 1, 1, 1)
            cr.arc(cx, cy, radius, -math.pi / 2, -math.pi / 2 + math.tau * self._fraction)
            cr.stroke()


# ---------------------------------------------------------------------------
# Result card
# ---------------------------------------------------------------------------


class StoreCard(Gtk.Box):
    """A Store result. Looks like a Library card; acts like a shop shelf item.

    Click previews; the hover buttons preview and download (or apply, once the
    wallpaper is in the Library). While downloading, a ring with a cancel button
    covers the picture.
    """

    def __init__(self, item: StoreItem, controller, width: int = 220) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.item = item
        self._controller = controller
        self.add_css_class("wp-card")
        self.add_css_class("store-card")
        height = round(width * 9 / 16)

        overlay = Gtk.Overlay()
        self.frame = overlay
        overlay.add_css_class("wp-frame")
        overlay.set_overflow(Gtk.Overflow.HIDDEN)
        overlay.set_child(ui.Thumb.of(item, width, height, radius=12, fill=True))

        self._owned = owned_pill()
        self._owned.set_halign(Gtk.Align.END)
        self._owned.set_valign(Gtk.Align.START)
        self._owned.set_margin_top(8)
        self._owned.set_margin_end(8)
        overlay.add_overlay(self._owned)

        resolution = ui.pill(catalog.short_resolution(item), None, "on-image")
        resolution.set_valign(Gtk.Align.END)
        resolution.set_margin_start(8)
        resolution.set_margin_bottom(10)
        resolution.set_tooltip_text(
            f"{item.resolution if item.provider == 'Wallhaven' else ''}".strip()
            or ("Available in 4K and HD" if catalog.has_4k(item) else "Available in HD")
        )
        self._resolution = resolution
        overlay.add_overlay(resolution)

        actions = Gtk.Box(spacing=6, halign=Gtk.Align.END, valign=Gtk.Align.END, margin_end=8, margin_bottom=8)
        actions.add_css_class("hover-only")
        actions.append(
            ui.icon_button(
                "view-reveal-symbolic", "Preview", lambda *_: controller.preview(item), "circular", "on-image"
            )
        )
        self._download = ui.icon_button(
            "folder-download-symbolic",
            "Download",
            lambda *_: controller.download(item),
            "circular",
            "suggested-action",
            "store-download",
        )
        actions.append(self._download)
        self._apply = Gtk.Button(label="Apply", tooltip_text="Show it now")
        for css in ("pill", "suggested-action", "apply-button"):
            self._apply.add_css_class(css)
        self._apply.connect("clicked", lambda *_: controller.apply_item(item))
        actions.append(self._apply)
        self._actions = actions
        overlay.add_overlay(actions)

        # Selection mode: the shared ring-and-tick badge, top-left.
        self._selecting = False
        self._check = ui.PickBadge()
        self._check.set_margin_top(8)
        self._check.set_margin_start(8)
        self._check.set_visible(False)
        overlay.add_overlay(self._check)

        # Download layer: a scrim and a ring with a cancel button in its middle.
        self._scrim = Gtk.Box()
        self._scrim.add_css_class("store-scrim")
        overlay.add_overlay(self._scrim)
        self._ring = ProgressRing()
        ring_box = Gtk.Overlay(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        ring_box.set_child(self._ring)
        cancel = ui.icon_button(
            "window-close-symbolic", "Cancel download", lambda *_: controller.cancel(item), "circular", "store-cancel"
        )
        cancel.set_halign(Gtk.Align.CENTER)
        cancel.set_valign(Gtk.Align.CENTER)
        ring_box.add_overlay(cancel)
        self._ring_box = ring_box
        overlay.add_overlay(ring_box)

        self.append(overlay)

        row = Gtk.Box(spacing=6)
        name = Gtk.Label(label=item.title, xalign=0, hexpand=True, ellipsize=3)
        name.add_css_class("wp-name")
        row.append(name)
        self._detail = Gtk.Box(spacing=3, valign=Gtk.Align.CENTER)
        self._detail.add_css_class("store-detail")
        self._detail_icon = Gtk.Image()
        self._detail_label = Gtk.Label()
        self._detail_label.add_css_class("numeric")
        self._detail.append(self._detail_icon)
        self._detail.append(self._detail_label)
        row.append(self._detail)
        self.append(row)

        click = Gtk.GestureClick(button=1)
        click.connect("released", lambda *_: controller.open_item(item))
        overlay.add_controller(click)
        secondary = Gtk.GestureClick(button=3)
        secondary.connect("pressed", lambda _g, _n, x, y: controller.popup_menu(self, x, y))
        overlay.add_controller(secondary)
        long_press = Gtk.GestureLongPress()
        long_press.connect("pressed", lambda _g, x, y: controller.popup_menu(self, x, y))
        overlay.add_controller(long_press)
        self.set_focusable(True)
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)
        self.set_tooltip_text(item.title)
        self.update()

    def _on_key(self, _ctrl, keyval, _code, state) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            if state & Gdk.ModifierType.CONTROL_MASK:
                if self.item.in_library:
                    self._controller.apply_item(self.item)
                else:
                    self._controller.download(self.item)
            else:
                self._controller.open_item(self.item)
            return True
        if keyval == Gdk.KEY_Menu or (keyval == Gdk.KEY_F10 and state & Gdk.ModifierType.SHIFT_MASK):
            self._controller.popup_menu(self, 40, 40)
            return True
        return False

    def update(self) -> None:
        progress = self._controller.progress_of(self.item)
        busy = progress is not None
        owned = self.item.in_library
        self._scrim.set_visible(busy)
        self._ring_box.set_visible(busy)
        self._actions.set_visible(not busy and not self._selecting)
        # Only what can still be downloaded can be picked.
        self._check.set_visible(self._selecting and not busy and not owned)
        self._resolution.set_visible(not busy)
        self._owned.set_visible(owned and not busy)
        self._download.set_visible(not owned)
        self._apply.set_visible(owned)
        if busy:
            self._ring.set_fraction(progress)
            total = catalog.megabytes(self.item, self._controller.quality_of(self.item))
            self._detail_icon.set_visible(False)
            self._detail_label.set_label(f"{total * progress:.1f} of {total:.0f} MB")
            self._detail.add_css_class("busy")
            self._detail.remove_css_class("dimmed")
            self._detail.set_tooltip_text(None)
        else:
            icon, text, tooltip = detail_text(self.item)
            self._detail_icon.set_from_icon_name(icon)
            self._detail_icon.set_visible(True)
            self._detail_label.set_label(text)
            self._detail.remove_css_class("busy")
            self._detail.add_css_class("dimmed")
            self._detail.set_tooltip_text(tooltip)

    def set_selected(self, selected: bool) -> None:
        if selected:
            self.frame.add_css_class("selected")
        else:
            self.frame.remove_css_class("selected")

    def set_selectable(self, on: bool) -> None:
        """Selection mode: show the ring and park Preview/Download/Apply."""
        self._selecting = on
        if not on:
            self.set_checked(False)
        self.update()

    def set_checked(self, checked: bool) -> None:
        self._check.set_picked(checked)
        (self.frame.add_css_class if checked else self.frame.remove_css_class)("checked")


# ---------------------------------------------------------------------------
# Preview dialog
# ---------------------------------------------------------------------------


def _section(title: str, child: Gtk.Widget) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    label = Gtk.Label(label=title.upper(), xalign=0)
    label.add_css_class("section-label")
    box.append(label)
    box.append(child)
    return box


class StorePreview(Adw.Dialog):
    """A big look at one result, its facts and tags, and the download buttons.

    Left/Right step through the results without closing the dialog.
    """

    def __init__(self, controller, items: list[StoreItem], item: StoreItem) -> None:
        super().__init__(content_width=800, width_request=360, height_request=320)
        self._controller = controller
        self._items = items
        self.item = item
        self._quality = "4k"
        self._motion_timer = 0
        self._motion_fraction = 0.0
        self._mode: tuple[str, str] | None = None

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._title = Adw.WindowTitle()
        header.set_title_widget(self._title)
        nav = Gtk.Box()
        nav.add_css_class("linked")
        self._prev = ui.icon_button("go-previous-symbolic", "Previous", lambda *_: self.step(-1))
        self._next = ui.icon_button("go-next-symbolic", "Next", lambda *_: self.step(1))
        nav.append(self._prev)
        nav.append(self._next)
        header.pack_start(nav)
        view.add_top_bar(header)

        self._scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True)
        view.set_content(self._scroller)

        bar = Gtk.Box(spacing=8)
        bar.add_css_class("store-preview-bar")
        self._site = Gtk.Button()
        self._site.add_css_class("flat")
        self._site_content = Adw.ButtonContent(icon_name="adw-external-link-symbolic", can_shrink=True)
        self._site.set_child(self._site_content)
        self._site.connect("clicked", lambda *_: controller.open_site(self.item))
        bar.append(self._site)
        bar.append(Gtk.Box(hexpand=True))
        self._buttons = Gtk.Box(spacing=8)
        bar.append(self._buttons)
        view.add_bottom_bar(bar)
        view.set_bottom_bar_style(Adw.ToolbarStyle.RAISED_BORDER)
        self.set_child(view)

        # Narrow: stack the details under each other, keep the site link icon-only.
        self._details_box: Gtk.Box | None = None
        self._narrow = False
        narrow = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        narrow.connect("apply", lambda *_: self._set_narrow(True))
        narrow.connect("unapply", lambda *_: self._set_narrow(False))
        self.add_breakpoint(narrow)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.connect("closed", self._on_closed)
        self.show_item(item)

    # -- navigation -----------------------------------------------------------
    def _on_key(self, _ctrl, keyval, _code, state) -> bool:
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        focus = self.get_focus()
        if isinstance(focus, (Gtk.Editable, Gtk.Text)):
            return False
        if keyval == Gdk.KEY_Left:
            self.step(-1)
            return True
        if keyval == Gdk.KEY_Right:
            self.step(1)
            return True
        return False

    def step(self, direction: int) -> None:
        ids = [i.id for i in self._items]
        if self.item.id not in ids:
            return
        index = ids.index(self.item.id) + direction
        if 0 <= index < len(self._items):
            self.show_item(self._items[index])

    def _on_closed(self, *_args) -> None:
        self._stop_motion()
        self._controller.dialog_closed(self)

    def _set_narrow(self, narrow: bool) -> None:
        self._narrow = narrow
        if self._details_box is not None:
            self._details_box.set_orientation(Gtk.Orientation.VERTICAL if narrow else Gtk.Orientation.HORIZONTAL)
        self._site_content.set_label("" if narrow else f"Open on {catalog.PROVIDERS[self.item.provider].site}")

    # -- content ------------------------------------------------------------------
    def show_item(self, item: StoreItem) -> None:
        self._stop_motion()
        self.item = item
        self._quality = "4k" if catalog.has_4k(item) else "hd"
        provider = catalog.PROVIDERS[item.provider]
        self._title.set_title(item.title)
        self._title.set_subtitle(item.provider)
        self.set_title(item.title)
        ids = [i.id for i in self._items]
        index = ids.index(item.id) if item.id in ids else -1
        self._prev.set_sensitive(index > 0)
        self._next.set_sensitive(0 <= index < len(ids) - 1)
        self._site_content.set_label("" if self._narrow else f"Open on {provider.site}")
        self._site.set_tooltip_text(f"Open on {provider.site}")
        self._site.update_property([Gtk.AccessibleProperty.LABEL], [f"Open on {provider.site}"])

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_start=20,
            margin_end=20,
            margin_top=4,
            margin_bottom=20,
        )
        box.append(self._picture(item))

        details = Gtk.Box(spacing=28)
        self._details_box = details
        details.set_orientation(Gtk.Orientation.VERTICAL if self._narrow else Gtk.Orientation.HORIZONTAL)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, hexpand=True)
        left.append(_section("Tags", self._tags(item)))
        if item.provider == "Wallhaven":
            left.append(_section("Colors", self._colors(item)))
        else:
            left.append(_section("Quality", self._qualities(item)))
        details.append(left)
        details.append(self._facts(item))
        box.append(details)
        self._scroller.set_child(box)
        self._mode = None  # new badge and button boxes: build them from scratch
        self.refresh()

    def _picture(self, item: StoreItem) -> Gtk.Widget:
        overlay = Gtk.Overlay()
        # Nominal 320 × 180 (the narrowest it gets); fill=True scales it up to the space given.
        overlay.set_child(ui.Thumb.of(item, 320, 180, radius=12, fill=True, size=(1280, 720)))
        self._badges = Gtk.Box(spacing=6, halign=Gtk.Align.START, valign=Gtk.Align.START)
        self._badges.add_css_class("store-preview-badges")
        overlay.add_overlay(self._badges)
        if item.provider == "MotionBGS":
            play = Gtk.ToggleButton(
                icon_name="media-playback-start-symbolic", halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER
            )
            for css in ("circular", "on-image", "store-play"):
                play.add_css_class(css)
            play.set_tooltip_text("Play the preview loop")
            play.update_property([Gtk.AccessibleProperty.LABEL], ["Play the preview loop"])
            play.connect("toggled", self._on_play)
            overlay.add_overlay(play)
            self._motion = Gtk.ProgressBar(valign=Gtk.Align.END, margin_start=14, margin_end=14, margin_bottom=12)
            self._motion.add_css_class("store-motion")
            self._motion.add_css_class("osd")
            self._motion.set_visible(False)
            overlay.add_overlay(self._motion)
        return overlay

    def _on_play(self, button: Gtk.ToggleButton) -> None:
        playing = button.get_active()
        button.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")
        button.set_tooltip_text("Pause the preview" if playing else "Play the preview loop")
        button.set_opacity(0.55 if playing else 1.0)
        self._motion.set_visible(True)
        if playing and not self._motion_timer:
            self._motion_timer = GLib.timeout_add(50, self._advance_motion)
        elif not playing:
            self._stop_motion(keep_bar=True)

    def _advance_motion(self) -> bool:
        seconds = max(1, int(catalog.duration(self.item).split(":")[1] or 20))
        self._motion_fraction = (self._motion_fraction + 0.05 / seconds) % 1.0
        self._motion.set_fraction(self._motion_fraction)
        return True

    def _stop_motion(self, keep_bar: bool = False) -> None:
        if self._motion_timer:
            GLib.source_remove(self._motion_timer)
            self._motion_timer = 0
        if not keep_bar:
            self._motion_fraction = 0.0

    def _tags(self, item: StoreItem) -> Gtk.Widget:
        wrap = Adw.WrapBox(child_spacing=6, line_spacing=6)
        for tag in catalog.tags(item):
            button = Gtk.Button(label=tag)
            button.add_css_class("pill")
            button.add_css_class("chip")
            button.set_tooltip_text(f"Search “{tag}”")
            button.connect("clicked", lambda *_, t=tag: self._controller.search_for(t))
            wrap.append(button)
        more = Gtk.Button()
        more.set_child(Adw.ButtonContent(icon_name="edit-find-symbolic", label="More like this"))
        for css in ("flat", "pill", "chip"):
            more.add_css_class(css)
        more.set_tooltip_text("Find similar wallpapers")
        more.connect("clicked", lambda *_: self._controller.more_like(item))
        wrap.append(more)
        return wrap

    def _colors(self, item: StoreItem) -> Gtk.Widget:
        row = Gtk.Box(spacing=8)
        for color in catalog.colors(item):
            nearest = catalog.nearest_palette_color(color)
            button = color_button(color, f"{color} · find more in this color", 26)
            button.connect("clicked", lambda *_, c=nearest: self._controller.search_color(c))
            row.append(button)
        return row

    def _qualities(self, item: StoreItem) -> Gtk.Widget:
        if not catalog.has_4k(item):
            return ui.dim(f"HD only · {catalog.megabytes(item, 'hd'):.0f} MB", wrap=False)
        group = Adw.ToggleGroup(halign=Gtk.Align.START)
        options = [("hd", f"HD · {catalog.megabytes(item, 'hd'):.0f} MB")]
        if catalog.has_4k(item):
            options.append(("4k", f"4K · {catalog.megabytes(item, '4k'):.0f} MB"))
        for name, label in options:
            group.add(Adw.Toggle(name=name, label=label))
        group.set_active_name(self._quality)
        group.connect("notify::active-name", lambda g, _p: setattr(self, "_quality", g.get_active_name()))
        return group

    def _facts(self, item: StoreItem) -> Gtk.Widget:
        if item.provider == "Wallhaven":
            facts = [
                ("Resolution", f"{item.resolution} · {catalog.ratio(item)}"),
                ("Size", f"{catalog.megabytes(item)} MB · {catalog.file_type(item)}"),
                ("Category", catalog.category_label(item)),
                ("Uploader", catalog.uploader(item)),
                ("Source", catalog.source(item)),
                ("Added", catalog.added(item)),
                ("Views", f"{catalog.views(item):,}"),
                ("Favorites", f"{catalog.favorites(item):,}"),
            ]
        else:
            facts = [
                ("Length", f"{catalog.duration(item)} loop"),
                ("Category", catalog.category_label(item)),
                ("Type", catalog.file_type(item)),
                ("Added", catalog.added(item)),
            ]
        grid = Gtk.Grid(row_spacing=7, column_spacing=16, valign=Gtk.Align.START)
        grid.set_size_request(250, -1)
        for row, (label, value) in enumerate(facts):
            name = Gtk.Label(label=label.upper(), xalign=0)
            name.add_css_class("store-fact-label")
            text = Gtk.Label(label=value, xalign=0, selectable=True)
            text.add_css_class("store-fact-value")
            grid.attach(name, 0, row, 1, 1)
            grid.attach(text, 1, row, 1, 1)
        return grid

    # -- state ------------------------------------------------------------------------
    def refresh(self) -> None:
        """Repaint the badge and buttons for the item's download state.

        While downloading only the numbers change, so the Cancel button under
        the pointer stays the same widget.
        """
        item = self.item
        progress = self._controller.progress_of(item)
        mode = "busy" if progress is not None else "owned" if item.in_library else "get"
        if mode == "busy" and self._mode == ("busy", item.id):
            self._set_progress(progress)
            return
        self._mode = (mode, item.id)

        child = self._badges.get_first_child()
        while child:
            self._badges.remove(child)
            child = self._badges.get_first_child()
        if item.in_library:
            self._badges.append(owned_pill())
        self._badges.append(ui.pill(catalog.short_resolution(item), None, "on-image"))

        child = self._buttons.get_first_child()
        while child:
            self._buttons.remove(child)
            child = self._buttons.get_first_child()
        if mode == "busy":
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
            self._progress_label = Gtk.Label(xalign=0)
            self._progress_label.add_css_class("caption")
            self._progress_label.add_css_class("numeric")
            self._progress_bar = Gtk.ProgressBar()
            self._progress_bar.add_css_class("store-dialog-progress")
            self._progress_bar.set_size_request(180, -1)
            box.append(self._progress_label)
            box.append(self._progress_bar)
            cancel = Gtk.Button(label="Cancel")
            cancel.add_css_class("pill")
            cancel.connect("clicked", lambda *_: self._controller.cancel(item))
            self._buttons.append(box)
            self._buttons.append(cancel)
            self._set_progress(progress)
        elif mode == "owned":
            show = Gtk.Button(label="Show in Library")
            show.add_css_class("pill")
            show.connect("clicked", lambda *_: (self.close(), self._controller.show_in_library(item)))
            apply = Gtk.Button(label="Apply")
            apply.add_css_class("pill")
            apply.add_css_class("suggested-action")
            apply.set_tooltip_text("Show it now")
            apply.connect("clicked", lambda *_: (self.close(), self._controller.apply_item(item)))
            self._buttons.append(show)
            self._buttons.append(apply)
        else:
            download = Gtk.Button(label="Download")
            download.add_css_class("pill")
            download.set_tooltip_text("Save to your Library")
            download.connect("clicked", lambda *_: self._controller.download(item, quality=self._quality))
            apply = Gtk.Button(label="Download & apply")
            apply.add_css_class("pill")
            apply.add_css_class("suggested-action")
            apply.set_tooltip_text("Download, then show it right away")
            apply.connect(
                "clicked", lambda *_: (self.close(), self._controller.download(item, apply=True, quality=self._quality))
            )
            self._buttons.append(download)
            self._buttons.append(apply)

    def _set_progress(self, progress: float) -> None:
        total = catalog.megabytes(self.item, self._quality)
        self._progress_label.set_label(f"Downloading · {total * progress:.1f} of {total:.0f} MB")
        self._progress_bar.set_fraction(progress)
