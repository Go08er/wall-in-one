"""Small shared widgets and helpers. Pages build on these for a consistent look."""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Graphene, Gsk, Gtk

from . import data, thumbs
from .catalog import KIND_ICON, KIND_LABEL
from .models import Wallpaper

_EXTRA: list[Gtk.CssProvider] = []


def load_css() -> None:
    display = Gdk.Display.get_default()
    provider = Gtk.CssProvider()
    provider.load_from_path(str(Path(__file__).with_name("style.css")))
    Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    # The prototype's own symbolic icons (e.g. wio-select-multiple), whatever the icon theme.
    Gtk.IconTheme.get_for_display(display).add_search_path(str(Path(__file__).with_name("icons")))


def add_css(css: str) -> None:
    """Pages may register their own CSS without editing style.css."""
    provider = Gtk.CssProvider()
    provider.load_from_string(css)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _EXTRA.append(provider)


def rgba(hex_color: str) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.parse(hex_color)
    return color


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


class Thumb(Gtk.Widget):
    """A paintable drawn cover-fit inside a rounded rectangle of an exact size.

    ``fill=True`` makes it take the available width and keep the aspect ratio
    of ``width:height`` (used for big previews). ``Thumb.of(source, …)`` loads
    the picture off the main thread (thumbs.LOADER) and paints a flat
    placeholder until it arrives.
    """

    def __init__(
        self, paintable: Gdk.Paintable | None, width: int, height: int, radius: float = 12, fill: bool = False
    ) -> None:
        super().__init__()
        self._paintable = paintable
        self._placeholder: Gdk.RGBA | None = None
        self._wanted: tuple | None = None  # the picture a pending load is for
        self._width, self._height, self._radius, self._fill = width, height, radius, fill
        self.set_overflow(Gtk.Overflow.HIDDEN)
        if not fill:
            self.set_halign(Gtk.Align.START)
            self.set_valign(Gtk.Align.CENTER)

    @classmethod
    def of(
        cls,
        source,
        width: int,
        height: int,
        radius: float = 12,
        fill: bool = False,
        size: tuple[int, int] = (480, 270),
    ) -> Thumb:
        """A picture of ``source`` (a wallpaper or Store item), drawn at ``size``."""
        thumb = cls(None, width, height, radius, fill)
        thumb.show(source, *size)
        return thumb

    def show(self, source, width: int = 480, height: int = 270) -> None:
        """Show ``source``: at once when cached, otherwise after a worker draws it."""
        key = (*source.key, width, height)
        if key == self._wanted:
            return
        self._wanted = key

        # A strong reference on purpose: PyGObject drops a widget's Python
        # wrapper (and so any weakref to it) while GTK still holds the widget.
        # The loader lets go of this callback once the picture is delivered.
        def deliver(texture: Gdk.Texture | None, thumb: Thumb = self) -> None:
            if thumb._wanted == key and texture is not None:
                thumb._wanted = None
                thumb.set_paintable(texture)

        texture = thumbs.LOADER.request(source, width, height, deliver)
        if texture is not None:
            self._wanted = None
            self.set_paintable(texture)
        else:
            self._paintable = None
            self._placeholder = thumbs.placeholder(source)
            self.queue_draw()

    def set_paintable(self, paintable: Gdk.Paintable) -> None:
        self._paintable = paintable
        self.queue_draw()

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH if self._fill else Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_measure(self, orientation: Gtk.Orientation, for_size: int):
        if not self._fill:
            size = self._width if orientation == Gtk.Orientation.HORIZONTAL else self._height
            return size, size, -1, -1
        minimum_width = min(150, self._width)
        if orientation == Gtk.Orientation.HORIZONTAL:
            return minimum_width, self._width, -1, -1
        ratio = self._height / self._width
        if for_size > 0:
            # Floor, not round: containers may re-query at slightly different widths.
            height = int(for_size * ratio)
            return height, height, -1, -1
        # Without a width, the minimum must match the narrowest width we accept.
        return int(minimum_width * ratio), int(self._width * ratio), -1, -1

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        if width <= 0 or height <= 0:
            return
        rounded = Gsk.RoundedRect()
        bounds = Graphene.Rect().init(0, 0, width, height)
        rounded.init_from_rect(bounds, self._radius)
        snapshot.push_rounded_clip(rounded)
        if self._paintable is None:
            if self._placeholder is not None:
                snapshot.append_color(self._placeholder, bounds)
            snapshot.pop()
            return
        source_w = self._paintable.get_intrinsic_width() or width
        source_h = self._paintable.get_intrinsic_height() or height
        scale = max(width / source_w, height / source_h)
        drawn_w, drawn_h = source_w * scale, source_h * scale
        snapshot.save()
        snapshot.translate(Graphene.Point().init((width - drawn_w) / 2, (height - drawn_h) / 2))
        self._paintable.snapshot(snapshot, drawn_w, drawn_h)
        snapshot.restore()
        snapshot.pop()


def thumbnail(wallpaper: Wallpaper, width: int, height: int, radius: float = 10) -> Thumb:
    """A rounded, cropped picture of a wallpaper at an exact size (loaded off the main thread)."""
    return Thumb.of(wallpaper, width, height, radius, size=(max(320, width * 2), max(180, round(width * 2 * 9 / 16))))


def texture_picture(texture: Gdk.Texture, width: int, height: int, radius: float = 10) -> Thumb:
    return Thumb(texture, width, height, radius)


class Swatches(Gtk.DrawingArea):
    """A row of color dots. Overlap=True draws them as an overlapping stack."""

    def __init__(self, colors: list[str], size: int = 16, overlap: bool = False) -> None:
        super().__init__()
        self._colors = colors
        self._size = size
        self._overlap = overlap
        step = size * (0.62 if overlap else 1.25)
        width = int(size + step * max(0, len(colors) - 1)) if colors else size
        self.set_content_width(width)
        self.set_content_height(size)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)

    def set_colors(self, colors: list[str]) -> None:
        self._colors = colors
        step = self._size * (0.62 if self._overlap else 1.25)
        self.set_content_width(int(self._size + step * max(0, len(colors) - 1)) if colors else self._size)
        self.queue_draw()

    def _draw(self, _area, cr, _width, height) -> None:
        size = self._size
        step = size * (0.62 if self._overlap else 1.25)
        if not self._colors:
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.5)
            cr.set_dash([2, 2])
            cr.set_line_width(1)
            cr.arc(size / 2, height / 2, size / 2 - 1, 0, math.tau)
            cr.stroke()
            return
        for index, color in enumerate(self._colors):
            rgba = Gdk.RGBA()
            rgba.parse(color)
            x = size / 2 + index * step
            cr.arc(x, height / 2, size / 2, 0, math.tau)
            cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, 1)
            cr.fill_preserve()
            cr.set_source_rgba(0, 0, 0, 0.25 if not self._overlap else 0.0)
            cr.set_line_width(1)
            cr.stroke()
            if self._overlap:
                cr.set_source_rgba(1, 1, 1, 0.9)
                cr.set_line_width(1.5)
                cr.arc(x, height / 2, size / 2, 0, math.tau)
                cr.stroke()


# ---------------------------------------------------------------------------
# Small pieces of text
# ---------------------------------------------------------------------------


def pill(text: str, icon: str | None = None, *classes: str) -> Gtk.Widget:
    box = Gtk.Box(spacing=4)
    box.add_css_class("pill")
    for css in classes:
        box.add_css_class(css)
    if icon:
        image = Gtk.Image.new_from_icon_name(icon)
        image.set_pixel_size(12)
        box.append(image)
    label = Gtk.Label(label=text)
    box.append(label)
    box.set_valign(Gtk.Align.START)
    box.set_halign(Gtk.Align.START)
    return box


class PickBadge(Gtk.Box):
    """Round badge on a thumbnail corner: an empty ring while a card can be
    picked, filled with the accent and a tick (or a number) once it is.
    Shared by selection mode and the "Add wallpapers" picker."""

    def __init__(self, halign: Gtk.Align = Gtk.Align.START, valign: Gtk.Align = Gtk.Align.START) -> None:
        # Explicitly not expanding: the centered children would otherwise make it
        # share a card's spare width with the spacer and push badges inward.
        super().__init__(halign=halign, valign=valign, can_target=False, hexpand=False, vexpand=False)
        self.add_css_class("wio-pick-badge")
        self._label = Gtk.Label(hexpand=True, halign=Gtk.Align.CENTER)
        self._tick = Gtk.Image.new_from_icon_name("object-select-symbolic")
        self._tick.set_pixel_size(14)
        self._tick.set_hexpand(True)
        self._tick.set_halign(Gtk.Align.CENTER)
        self._tick.set_visible(False)
        self.append(self._label)
        self.append(self._tick)

    def set_picked(self, picked: bool, number: str = "") -> None:
        """Picked shows ``number`` if given, otherwise a tick."""
        (self.add_css_class if picked else self.remove_css_class)("picked")
        self._label.set_label(number if picked else "")
        self._label.set_visible(bool(picked and number))
        self._tick.set_visible(picked and not number)


def heading(text: str, css: str = "heading") -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0)
    label.add_css_class(css)
    return label


def dim(text: str, wrap: bool = True) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0, wrap=wrap)
    label.add_css_class("dimmed")
    return label


def icon_button(
    icon: str, tooltip: str, callback: Callable[[Gtk.Button], None] | None = None, *classes: str
) -> Gtk.Button:
    button = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
    button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
    for css in classes:
        button.add_css_class(css)
    if callback:
        button.connect("clicked", callback)
    return button


def kind_badge(wallpaper: Wallpaper) -> Gtk.Widget:
    text = KIND_LABEL[wallpaper.kind]
    if wallpaper.kind == "video" and wallpaper.duration:
        text = f"{text} · {wallpaper.duration}"
    return pill(text, KIND_ICON[wallpaper.kind], "on-image")


# ---------------------------------------------------------------------------
# Wallpaper card
# ---------------------------------------------------------------------------


class WallpaperCard(Gtk.Box):
    """Thumbnail card used by the Library, playlists and pickers.

    Click selects/opens; the hover button applies. A right-click menu offers the
    rest. Pages pass callbacks so the card stays a dumb view.
    """

    def __init__(
        self,
        wallpaper: Wallpaper,
        *,
        width: int = 220,
        playing_on: list[str] | None = None,
        on_open: Callable[[Wallpaper], None] | None = None,
        on_apply: Callable[[Wallpaper], None] | None = None,
        on_favorite: Callable[[Wallpaper], None] | None = None,
        menu: Gtk.PopoverMenu | None = None,
        show_name: bool = True,
        selectable: bool = False,
        apply_tooltip: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.wallpaper = wallpaper
        self.add_css_class("wp-card")
        height = round(width * 9 / 16)

        overlay = Gtk.Overlay()
        self.frame = overlay
        overlay.add_css_class("wp-frame")
        overlay.set_overflow(Gtk.Overflow.HIDDEN)
        # Fill the grid cell at 16:9 so outlines and badges always hug the picture.
        overlay.set_child(Thumb.of(wallpaper, width, height, radius=12, fill=True))

        top = Gtk.Box(spacing=4, margin_top=8, margin_start=8, margin_end=8)
        top.set_valign(Gtk.Align.START)
        # Selection mode: a ring that fills with a tick (shown by set_selectable).
        self._check = PickBadge()
        self._check.set_visible(False)
        top.append(self._check)
        self._hover_actions: list[Gtk.Widget] = []
        if wallpaper.is_moving:
            top.append(kind_badge(wallpaper))
        if playing_on:
            # The play icon already says "on screen"; the label just names where.
            on_screen = pill(
                playing_on[0] if len(playing_on) == 1 else "All displays", "media-playback-start-symbolic", "accent"
            )
            on_screen.set_tooltip_text("On screen now: " + ", ".join(playing_on))
            top.append(on_screen)
        spacer = Gtk.Box(hexpand=True)
        top.append(spacer)
        if wallpaper.problem:
            warn = pill("", "dialog-warning-symbolic", "warning")
            warn.set_tooltip_text("Skipped after a playback problem")
            top.append(warn)
        if on_favorite or wallpaper.favorite:
            fav = Gtk.Button(icon_name="starred-symbolic" if wallpaper.favorite else "non-starred-symbolic")
            fav.add_css_class("circular")
            fav.add_css_class("on-image")
            fav.add_css_class("fav" if wallpaper.favorite else "hover-only")
            fav.set_tooltip_text("Remove from favorites" if wallpaper.favorite else "Add to favorites")
            if on_favorite:
                fav.connect("clicked", lambda *_: on_favorite(wallpaper))
            top.append(fav)
            self._hover_actions.append(fav)
        self._menu = menu
        if menu is not None:
            # Hover/focus reveal a ⋯ button, so the menu never needs a right-click.
            more = Gtk.Button(icon_name="view-more-symbolic")
            more.add_css_class("circular")
            more.add_css_class("on-image")
            more.add_css_class("hover-only")
            more.set_tooltip_text("More actions")
            more.update_property([Gtk.AccessibleProperty.LABEL], ["More actions"])
            more.connect("clicked", self._popup_from_button)
            top.append(more)
            self._hover_actions.append(more)
        overlay.add_overlay(top)

        if on_apply:
            unavailable = bool(wallpaper.problem)
            apply = Gtk.Button(label="Skipped" if unavailable else "Apply")
            apply.add_css_class("pill")
            apply.add_css_class("suggested-action")
            apply.add_css_class("hover-only")
            apply.add_css_class("apply-button")
            apply.set_halign(Gtk.Align.END)
            apply.set_valign(Gtk.Align.END)
            apply.set_margin_end(8)
            apply.set_margin_bottom(8)
            apply.set_sensitive(not unavailable)
            if unavailable:
                apply.set_tooltip_text("Skipped after a playback problem — open details to try again")
            elif apply_tooltip is not None:
                # Asked at hover time, so it always names the current target.
                apply.set_has_tooltip(True)
                apply.connect("query-tooltip", lambda _w, _x, _y, _k, tip: (tip.set_text(apply_tooltip()), True)[1])
            else:
                apply.set_tooltip_text("Show this wallpaper now")
            apply.connect("clicked", lambda *_: on_apply(wallpaper))
            overlay.add_overlay(apply)
            self._hover_actions.append(apply)

        self.append(overlay)
        if show_name:
            row = Gtk.Box(spacing=6)
            name = Gtk.Label(label=wallpaper.name, xalign=0, hexpand=True, ellipsize=3)  # Pango.EllipsizeMode.END
            name.add_css_class("wp-name")
            row.append(name)
            swatch_colors = data.wallpaper_swatches(wallpaper)[:4]
            if swatch_colors:
                swatches = Swatches(swatch_colors[1:4], size=11, overlap=True)
                swatches.set_tooltip_text(_color_tooltip(wallpaper))
                row.append(swatches)
            self.append(row)

        if on_open:
            click = Gtk.GestureClick(button=1)
            click.connect("released", lambda gesture, n, x, y: self._primary(gesture, on_open))
            overlay.add_controller(click)
            self.set_focusable(True)
            key = Gtk.EventControllerKey()
            key.connect("key-pressed", self._on_key, on_open, on_apply)
            self.add_controller(key)
        if menu is not None:
            menu.set_parent(overlay)
            menu.set_has_arrow(False)
            secondary = Gtk.GestureClick(button=3)
            secondary.connect("pressed", self._popup, menu)
            overlay.add_controller(secondary)
            long_press = Gtk.GestureLongPress()
            long_press.connect("pressed", lambda g, x, y: self._popup(g, 1, x, y, menu))
            overlay.add_controller(long_press)
        self.set_tooltip_text(wallpaper.name)

    def _primary(self, _gesture: Gtk.GestureClick, on_open) -> None:
        # Overlay buttons claim their own clicks, so this only fires on the picture.
        on_open(self.wallpaper)

    def _on_key(self, _ctrl, keyval, _code, state, on_open, on_apply) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            if state & Gdk.ModifierType.CONTROL_MASK and on_apply:
                on_apply(self.wallpaper)
            else:
                on_open(self.wallpaper)
            return True
        menu_key = keyval == Gdk.KEY_Menu or (keyval == Gdk.KEY_F10 and state & Gdk.ModifierType.SHIFT_MASK)
        if menu_key and self._menu is not None:
            width = self.frame.get_width()
            self._popup(None, 1, width / 2, 40, self._menu)
            return True
        return False

    def _popup_from_button(self, button: Gtk.Button) -> None:
        ok, point = button.compute_point(self.frame, Graphene.Point().init(0, button.get_height()))
        self._popup(None, 1, point.x if ok else 0, point.y if ok else 0, self._menu)

    def _popup(self, _gesture, _n, x, y, menu: Gtk.PopoverMenu) -> None:
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        menu.set_pointing_to(rect)
        menu.popup()

    def set_selected(self, selected: bool) -> None:
        """Outline only: the card whose details are open."""
        if selected:
            self.frame.add_css_class("selected")
        else:
            self.frame.remove_css_class("selected")

    def set_selectable(self, on: bool) -> None:
        """Selection mode: show the ring and park Apply, the star and the ⋯ menu
        so a click anywhere on the picture just toggles the tick."""
        self._check.set_visible(on)
        for widget in self._hover_actions:
            widget.set_can_target(not on)
            widget.set_visible(not on or "fav" in widget.get_css_classes())
        if not on:
            self.set_checked(False)

    def set_checked(self, checked: bool) -> None:
        self._check.set_picked(checked)
        (self.frame.add_css_class if checked else self.frame.remove_css_class)("checked")


class CardGrid(Gtk.Widget):
    """Equal-width columns of wallpaper cards, filled edge to edge.

    Gtk.FlowBox measures a row's height at one column width and then gives
    some columns a pixel more, so height-for-width cards (ui.WallpaperCard)
    trip “Allocation height too small” criticals. Here every card gets exactly
    the width its row was measured at. Arrow keys move focus between cards.
    """

    def __init__(
        self, min_width: int = 180, column_spacing: int = 10, row_spacing: int = 12, max_columns: int = 8, **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self._min, self._column_gap, self._row_gap, self._max = min_width, column_spacing, row_spacing, max_columns
        self._children: list[Gtk.Widget] = []
        self._columns = 1
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    def append(self, child: Gtk.Widget) -> None:
        child.set_parent(self)
        self._children.append(child)
        self.queue_resize()

    def remove_all(self) -> None:
        for child in self._children:
            child.unparent()
        self._children = []
        self.queue_resize()

    def cards(self) -> list[Gtk.Widget]:
        return list(self._children)

    def _shown(self) -> list[Gtk.Widget]:
        return [child for child in self._children if child.get_visible()]

    def _layout(self, width: int) -> tuple[int, int]:
        columns = max(1, min(self._max, (width + self._column_gap) // (self._min + self._column_gap)))
        return columns, max(1, (width - self._column_gap * (columns - 1)) // columns)

    def _rows(self, width: int):
        shown = self._shown()
        columns, item = self._layout(width)
        for start in range(0, len(shown), columns):
            row = shown[start : start + columns]
            yield row, item, max(child.measure(Gtk.Orientation.VERTICAL, item)[0] for child in row)

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation: Gtk.Orientation, for_size: int):
        if orientation == Gtk.Orientation.HORIZONTAL:
            widths = [child.measure(orientation, -1)[0] for child in self._shown()] or [0]
            return max(widths), max(max(widths), self._min * 3), -1, -1
        heights = [height for _row, _item, height in self._rows(for_size if for_size > 0 else self._min * 3)]
        total = sum(heights) + self._row_gap * max(0, len(heights) - 1)
        return total, total, -1, -1

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        self._columns = self._layout(width)[0]
        y = 0
        for row, item, row_height in self._rows(width):
            for index, child in enumerate(row):
                point = Graphene.Point().init(index * (item + self._column_gap), y)
                child.allocate(item, row_height, -1, Gsk.Transform().translate(point))
            y += row_height + self._row_gap

    def do_dispose(self) -> None:
        self.remove_all()
        Gtk.Widget.do_dispose(self)

    def _on_key(self, _ctrl, keyval: int, _code: int, mods: Gdk.ModifierType) -> bool:
        steps = {Gdk.KEY_Left: -1, Gdk.KEY_Right: 1, Gdk.KEY_Up: -self._columns, Gdk.KEY_Down: self._columns}
        if keyval not in steps or mods & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        shown = self._shown()
        root = self.get_root()
        focus = root.get_focus() if root else None
        while focus is not None and focus.get_parent() is not self:
            focus = focus.get_parent()
        if focus not in shown:
            return False
        target = shown.index(focus) + steps[keyval]
        if 0 <= target < len(shown):
            shown[target].grab_focus()
        return True


def _color_tooltip(wallpaper: Wallpaper) -> str:
    if wallpaper.color_mode == "palette":
        return f"Colors: {wallpaper.palette} palette"
    if wallpaper.color_mode == "keep":
        return "Colors: don't change"
    scheme = data.SCHEME_NAME.get(wallpaper.scheme or data.DEFAULT_SCHEME, "")
    return f"Colors: from wallpaper · {scheme}" + ("" if wallpaper.scheme else " (default)")


def color_summary(wallpaper: Wallpaper) -> str:
    return _color_tooltip(wallpaper).removeprefix("Colors: ").capitalize()


def menu_from(sections: list[list[tuple[str, str]]]) -> Gtk.PopoverMenu:
    """Build a PopoverMenu from [(label, detailed-action), ...] sections."""
    from gi.repository import Gio

    model = Gio.Menu()
    for section in sections:
        part = Gio.Menu()
        for label, action in section:
            part.append(label, action)
        model.append_section(None, part)
    return Gtk.PopoverMenu.new_from_model(model)


def clamp(child: Gtk.Widget, maximum: int = 900) -> Adw.Clamp:
    wrapper = Adw.Clamp(maximum_size=maximum, tightening_threshold=maximum - 100)
    wrapper.set_child(child)
    return wrapper


# ---------------------------------------------------------------------------
# "Select several": a pill that hangs over the top corner of a list or grid
# ---------------------------------------------------------------------------

_CORNER_CSS = """
.corner-select {
  border-radius: 999px;
  padding: 3px 12px 3px 9px;
  min-height: 26px;
  background-color: var(--popover-bg-color);
  color: var(--popover-fg-color);
  box-shadow: 0 1px 3px alpha(black, 0.30), 0 0 0 1px alpha(currentColor, 0.10);
}
.corner-select:hover { background-image: image(alpha(currentColor, 0.07)); }
.corner-select:checked {
  background-color: var(--accent-bg-color);
  color: var(--accent-fg-color);
  box-shadow: 0 1px 4px alpha(black, 0.35);
}
"""
#: Room a page leaves at the right of the row above the list, so the hanging
#: pill never covers that row's own controls.
CORNER_RESERVE = 96


def select_toggle() -> Gtk.ToggleButton:
    """The selection-mode switch: "Select" with a ticked-tiles icon, "Done" while on."""
    if not getattr(select_toggle, "_styled", False):
        add_css(_CORNER_CSS)
        select_toggle._styled = True
    button = Gtk.ToggleButton()
    content = Adw.ButtonContent(icon_name="wio-select-multiple-symbolic", label="Select")
    button.set_child(content)
    button.add_css_class("corner-select")
    button.set_tooltip_text("Select several to add, favorite or remove them together")

    def relabel(toggle: Gtk.ToggleButton) -> None:
        on = toggle.get_active()
        content.set_label("Done" if on else "Select")
        content.set_icon_name("object-select-symbolic" if on else "wio-select-multiple-symbolic")

    button.connect("toggled", relabel)
    return button


def hang_on_corner(overlay: Gtk.Overlay, anchor: Gtk.Widget, button: Gtk.Widget, inset: int = 18) -> None:
    """Hang ``button`` over the top-right corner of ``anchor`` (a list or grid
    inside ``overlay``): centered on its top edge, ``inset`` px in from the right.
    It floats, so it stays put while the list scrolls under it."""
    overlay.add_overlay(button)

    def position(_overlay: Gtk.Overlay, child: Gtk.Widget, allocation: Gdk.Rectangle) -> bool:
        # GtkOverlay hands over the rectangle to fill (caller-allocated out argument).
        if child is not button:
            return False
        ok, bounds = anchor.compute_bounds(overlay)
        if not ok:
            return False
        _minimum, natural = button.get_preferred_size()
        allocation.width, allocation.height = natural.width, natural.height
        allocation.x = round(bounds.get_x() + bounds.get_width() - natural.width - inset)
        allocation.y = round(bounds.get_y() - natural.height / 2)
        return True

    overlay.connect("get-child-position", position)
