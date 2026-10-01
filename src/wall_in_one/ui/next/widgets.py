"""Small shared widgets of the new interface: pictures, cards, the card grid, badges.

Pictures come from the installed `wall_in_one.ui.next.thumbs` provider, so a
card never decodes or draws anything on the main thread. Words that name the
library come from `catalog`; everything a widget shows about a wallpaper comes
from a `WallpaperView`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Hashable, Iterator, Sequence
from typing import Any, Final

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, Gio, Graphene, Gsk, Gtk, Pango

from wall_in_one.ui.next import thumbs
from wall_in_one.ui.next.catalog import KIND_ICON, KIND_LABEL
from wall_in_one.ui.next.state import AppState, WallpaperView

_EXTRA: list[Gtk.CssProvider] = []
#: Before a picture arrives, when the provider has no colour of its own.
_NEUTRAL = (0.5, 0.5, 0.5, 0.18)


def add_css(css: str) -> None:
    """Register page CSS at APPLICATION priority without editing the shared styles."""
    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    provider.load_from_string(css)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
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
    of ``width:height`` (used for big previews). ``Thumb.of(source, …)`` asks
    the installed thumbnail provider for the picture and paints a flat
    placeholder until it arrives on the main thread.
    """

    def __init__(
        self,
        paintable: Gdk.Paintable | None,
        width: int,
        height: int,
        radius: float = 12,
        fill: bool = False,
    ) -> None:
        super().__init__()
        self._paintable = paintable
        self._placeholder: Gdk.RGBA | None = None
        #: The picture a pending request is for.
        self._wanted: Hashable | None = None
        self._width, self._height, self._radius, self._fill = width, height, radius, fill
        self.set_overflow(Gtk.Overflow.HIDDEN)
        if not fill:
            self.set_halign(Gtk.Align.START)
            self.set_valign(Gtk.Align.CENTER)

    @classmethod
    def of(
        cls,
        source: object,
        width: int,
        height: int,
        radius: float = 12,
        fill: bool = False,
        size: tuple[int, int] = (480, 270),
    ) -> Thumb:
        """A picture of ``source`` (a wallpaper or anything the provider draws)."""
        thumb = cls(None, width, height, radius, fill)
        thumb.show(source, *size)
        return thumb

    # Not GTK's ``show()``: GTK 4 deprecates that, and this is the name every
    # page of the prototype calls.
    def show(  # type: ignore[override]
        self, source: object, width: int = 480, height: int = 270
    ) -> None:
        """Show ``source``: at once when cached, otherwise when it is delivered."""
        provider = thumbs.provider()
        key = provider.key(source, width, height)
        if key == self._wanted:
            return
        self._wanted = key

        # A strong reference on purpose: PyGObject drops a widget's Python
        # wrapper (and so any weakref to it) while GTK still holds the widget.
        # The provider lets go of this callback once the picture is delivered.
        def deliver(texture: Gdk.Texture | None, thumb: Thumb = self) -> None:
            if thumb._wanted == key and texture is not None:
                thumb._wanted = None
                thumb.set_paintable(texture)

        texture = provider.request(source, width, height, deliver)
        if texture is not None:
            self._wanted = None
            self.set_paintable(texture)
        else:
            self._paintable = None
            self._placeholder = provider.placeholder(source)
            self.queue_draw()

    @property
    def paintable(self) -> Gdk.Paintable | None:
        """What is drawn now; None while the placeholder shows."""
        return self._paintable

    def set_paintable(self, paintable: Gdk.Paintable) -> None:
        self._paintable = paintable
        self.queue_draw()

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        if self._fill:
            return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH
        return Gtk.SizeRequestMode.CONSTANT_SIZE

    def do_measure(self, orientation: Gtk.Orientation, for_size: int) -> tuple[int, int, int, int]:
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
        bounds = Graphene.Rect().init(0, 0, width, height)
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(bounds, self._radius)
        snapshot.push_rounded_clip(rounded)
        if self._paintable is None:
            snapshot.append_color(self._placeholder or _neutral(), bounds)
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


def _neutral() -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.red, color.green, color.blue, color.alpha = _NEUTRAL
    return color


def thumbnail(source: object, width: int, height: int, radius: float = 10) -> Thumb:
    """A rounded, cropped picture at an exact size (delivered off the main thread)."""
    size = (max(320, width * 2), max(180, round(width * 2 * 9 / 16)))
    return Thumb.of(source, width, height, radius, size=size)


def texture_picture(texture: Gdk.Paintable, width: int, height: int, radius: float = 10) -> Thumb:
    return Thumb(texture, width, height, radius)


class Swatches(Gtk.DrawingArea):
    """A row of color dots. ``overlap=True`` draws them as an overlapping stack."""

    def __init__(self, colors: Sequence[str], size: int = 16, overlap: bool = False) -> None:
        super().__init__()
        self._colors = list(colors)
        self._size = size
        self._overlap = overlap
        self._fit()
        self.set_content_height(size)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)

    def _fit(self) -> None:
        step = self._size * (0.62 if self._overlap else 1.25)
        count = max(0, len(self._colors) - 1)
        self.set_content_width(int(self._size + step * count) if self._colors else self._size)

    def set_colors(self, colors: Sequence[str]) -> None:
        self._colors = list(colors)
        self._fit()
        self.queue_draw()

    def _draw(
        self, _area: Gtk.DrawingArea, cr: cairo.Context[Any], _width: int, height: int
    ) -> None:
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
            parsed = rgba(color)
            x = size / 2 + index * step
            cr.arc(x, height / 2, size / 2, 0, math.tau)
            cr.set_source_rgba(parsed.red, parsed.green, parsed.blue, 1)
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


def pill(text: str, icon: str | None = None, *classes: str) -> Gtk.Box:
    box = Gtk.Box(spacing=4)
    box.add_css_class("pill")
    for css in classes:
        box.add_css_class(css)
    if icon:
        image = Gtk.Image.new_from_icon_name(icon)
        image.set_pixel_size(12)
        box.append(image)
    box.append(Gtk.Label(label=text))
    box.set_valign(Gtk.Align.START)
    box.set_halign(Gtk.Align.START)
    return box


class PickBadge(Gtk.Box):
    """Round badge on a picture's corner: an empty ring while it can be picked,
    filled with the accent and a tick (or a number) once it is."""

    def __init__(
        self, halign: Gtk.Align = Gtk.Align.START, valign: Gtk.Align = Gtk.Align.START
    ) -> None:
        # Explicitly not expanding: the centered children would otherwise make it
        # share a card's spare width with the spacer and push badges inward.
        super().__init__(halign=halign, valign=valign, can_target=False)
        self.set_hexpand(False)
        self.set_vexpand(False)
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
        if picked:
            self.add_css_class("picked")
        else:
            self.remove_css_class("picked")
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
    icon: str,
    tooltip: str,
    callback: Callable[[Gtk.Button], None] | None = None,
    *classes: str,
) -> Gtk.Button:
    """A button with only an icon, a tooltip and the same accessible label."""
    button = Gtk.Button(icon_name=icon, tooltip_text=tooltip)
    button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
    for css in classes:
        button.add_css_class(css)
    if callback is not None:
        button.connect("clicked", callback)
    return button


def kind_badge(wallpaper: WallpaperView) -> Gtk.Box:
    text = KIND_LABEL.get(wallpaper.kind, wallpaper.kind.capitalize())
    if wallpaper.kind == "video" and wallpaper.duration:
        text = f"{text} · {wallpaper.duration}"
    return pill(text, KIND_ICON.get(wallpaper.kind), "on-image")


# ---------------------------------------------------------------------------
# Wallpaper card
# ---------------------------------------------------------------------------


class WallpaperCard(Gtk.Box):
    """Thumbnail card used by the Library, playlists and pickers.

    Click opens (or ticks, in selection mode); the hover button applies; a
    right-click or the ⋯ button opens ``menu``. Pages pass callbacks, so the
    card stays a dumb view. ``apply_blocked``/``favorite_blocked`` turn those
    buttons off and say why.
    """

    def __init__(
        self,
        wallpaper: WallpaperView,
        *,
        width: int = 220,
        playing_on: Sequence[str] | None = None,
        on_open: Callable[[WallpaperView], None] | None = None,
        on_apply: Callable[[WallpaperView], None] | None = None,
        on_favorite: Callable[[WallpaperView], None] | None = None,
        menu: Gtk.PopoverMenu | None = None,
        show_name: bool = True,
        apply_tooltip: Callable[[], str] | None = None,
        apply_blocked: str = "",
        favorite_blocked: str = "",
        swatches: Sequence[str] | None = None,
        swatch_tooltip: str = "",
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
        self.picture = Thumb.of(wallpaper, width, height, radius=12, fill=True)
        overlay.set_child(self.picture)

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
            where = playing_on[0] if len(playing_on) == 1 else "All displays"
            on_screen = pill(where, "media-playback-start-symbolic", "accent")
            on_screen.set_tooltip_text("On screen now: " + ", ".join(playing_on))
            top.append(on_screen)
        top.append(Gtk.Box(hexpand=True))
        if wallpaper.problem:
            warn = pill("", "dialog-warning-symbolic", "warning")
            warn.set_tooltip_text("Skipped after a playback problem")
            top.append(warn)
        if on_favorite is not None or wallpaper.favorite:
            favorite = wallpaper.favorite
            fav = Gtk.Button(icon_name="starred-symbolic" if favorite else "non-starred-symbolic")
            fav.add_css_class("circular")
            fav.add_css_class("on-image")
            fav.add_css_class("fav" if favorite else "hover-only")
            label = "Remove from favorites" if favorite else "Add to favorites"
            fav.set_tooltip_text(favorite_blocked or label)
            fav.update_property([Gtk.AccessibleProperty.LABEL], [label])
            if on_favorite is not None:
                fav.connect("clicked", lambda *_: on_favorite(wallpaper))
                fav.set_sensitive(not favorite_blocked)
            else:
                fav.set_can_target(False)
            top.append(fav)
            self._hover_actions.append(fav)
            self.favorite_button: Gtk.Button | None = fav
        else:
            self.favorite_button = None
        self._menu = menu
        if menu is not None:
            # Hover and focus reveal a ⋯ button, so the menu never needs a right-click.
            more = icon_button(
                "view-more-symbolic", "More actions", self._popup_from_button, "circular"
            )
            more.add_css_class("on-image")
            more.add_css_class("hover-only")
            top.append(more)
            self._hover_actions.append(more)
        overlay.add_overlay(top)

        self.apply_button: Gtk.Button | None = None
        if on_apply is not None:
            unavailable = bool(wallpaper.problem)
            apply = Gtk.Button(label="Skipped" if unavailable else "Apply")
            for css in ("pill", "suggested-action", "hover-only", "apply-button"):
                apply.add_css_class(css)
            apply.set_halign(Gtk.Align.END)
            apply.set_valign(Gtk.Align.END)
            apply.set_margin_end(8)
            apply.set_margin_bottom(8)
            apply.set_sensitive(not unavailable and not apply_blocked)
            if unavailable:
                apply.set_tooltip_text(
                    "Skipped after a playback problem \u2014 open details to see why"
                )
            elif apply_blocked:
                apply.set_tooltip_text(apply_blocked)
            elif apply_tooltip is not None:
                # Asked at hover time, so it always names the current target.
                apply.set_has_tooltip(True)
                apply.connect("query-tooltip", _tooltip_from(apply_tooltip))
            else:
                apply.set_tooltip_text("Show this wallpaper now")
            apply.connect("clicked", lambda *_: on_apply(wallpaper))
            overlay.add_overlay(apply)
            self._hover_actions.append(apply)
            self.apply_button = apply

        self.append(overlay)
        if show_name:
            row = Gtk.Box(spacing=6)
            name = Gtk.Label(
                label=wallpaper.name, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END
            )
            name.add_css_class("wp-name")
            row.append(name)
            # The colors it puts on the desktop: primary, secondary, tertiary.
            if swatches:
                dots = Swatches(list(swatches)[1:4], size=11, overlap=True)
                dots.set_tooltip_text(swatch_tooltip)
                row.append(dots)
            self.append(row)

        if on_open is not None:
            click = Gtk.GestureClick(button=1)
            click.connect("released", lambda *_: on_open(self.wallpaper))
            overlay.add_controller(click)
            self.set_focusable(True)
            key = Gtk.EventControllerKey()
            key.connect("key-pressed", self._on_key, on_open, on_apply)
            self.add_controller(key)
        if menu is not None:
            menu.set_parent(overlay)
            menu.set_has_arrow(False)
            secondary = Gtk.GestureClick(button=3)
            secondary.connect("pressed", lambda _g, _n, x, y: self.popup_menu(x, y))
            overlay.add_controller(secondary)
            long_press = Gtk.GestureLongPress()
            long_press.connect("pressed", lambda _g, x, y: self.popup_menu(x, y))
            overlay.add_controller(long_press)
        self.set_tooltip_text(wallpaper.name)

    @property
    def menu(self) -> Gtk.PopoverMenu | None:
        return self._menu

    def _on_key(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
        on_open: Callable[[WallpaperView], None],
        on_apply: Callable[[WallpaperView], None] | None,
    ) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            applies = state & Gdk.ModifierType.CONTROL_MASK
            if applies and on_apply is not None:
                if self.apply_button is None or self.apply_button.get_sensitive():
                    on_apply(self.wallpaper)
            else:
                on_open(self.wallpaper)
            return True
        shifted = bool(state & Gdk.ModifierType.SHIFT_MASK)
        menu_key = keyval == Gdk.KEY_Menu or (keyval == Gdk.KEY_F10 and shifted)
        if menu_key and self._menu is not None:
            self.popup_menu(self.frame.get_width() / 2, 40)
            return True
        return False

    def _popup_from_button(self, button: Gtk.Button) -> None:
        ok, point = button.compute_point(self.frame, Graphene.Point().init(0, button.get_height()))
        self.popup_menu(point.x if ok else 0, point.y if ok else 0)

    def popup_menu(self, x: float, y: float) -> None:
        """Open the card's menu pointing at (``x``, ``y``) on the picture."""
        if self._menu is None:
            return
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self._menu.set_pointing_to(rect)
        self._menu.popup()

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
        if checked:
            self.frame.add_css_class("checked")
        else:
            self.frame.remove_css_class("checked")


def _tooltip_from(
    text: Callable[[], str],
) -> Callable[[Gtk.Widget, int, int, bool, Gtk.Tooltip], bool]:
    def query(_widget: Gtk.Widget, _x: int, _y: int, _keyboard: bool, tip: Gtk.Tooltip) -> bool:
        tip.set_text(text())
        return True

    return query


class CardGrid(Gtk.Widget):
    """Equal-width columns of wallpaper cards, filled edge to edge.

    Gtk.FlowBox measures a row's height at one column width and then gives
    some columns a pixel more, so height-for-width cards trip "Allocation
    height too small" criticals. Here every card gets exactly the width its
    row was measured at. Arrow keys move focus between cards.
    """

    def __init__(
        self,
        min_width: int = 180,
        column_spacing: int = 10,
        row_spacing: int = 12,
        max_columns: int = 8,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._min, self._column_gap, self._row_gap = min_width, column_spacing, row_spacing
        self._max = max_columns
        self._children: list[Gtk.Widget] = []
        self._columns = 1
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

    @property
    def min_width(self) -> int:
        return self._min

    @min_width.setter
    def min_width(self, value: int) -> None:
        self._min = value
        self.queue_resize()

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
        fits = (width + self._column_gap) // (self._min + self._column_gap)
        columns = max(1, min(self._max, fits))
        return columns, max(1, (width - self._column_gap * (columns - 1)) // columns)

    def _rows(self, width: int) -> Iterator[tuple[list[Gtk.Widget], int, int]]:
        shown = self._shown()
        columns, item = self._layout(width)
        for start in range(0, len(shown), columns):
            row = shown[start : start + columns]
            yield row, item, max(child.measure(Gtk.Orientation.VERTICAL, item)[0] for child in row)

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation: Gtk.Orientation, for_size: int) -> tuple[int, int, int, int]:
        if orientation == Gtk.Orientation.HORIZONTAL:
            widths = [child.measure(orientation, -1)[0] for child in self._shown()] or [0]
            return max(widths), max(max(widths), self._min * 3), -1, -1
        width = for_size if for_size > 0 else self._min * 3
        heights = [height for _row, _item, height in self._rows(width)]
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
        Gtk.Widget.do_dispose(self)  # type: ignore[attr-defined]

    def _on_key(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _code: int,
        mods: Gdk.ModifierType,
    ) -> bool:
        steps = {
            Gdk.KEY_Left: -1,
            Gdk.KEY_Right: 1,
            Gdk.KEY_Up: -self._columns,
            Gdk.KEY_Down: self._columns,
        }
        if keyval not in steps or mods & (
            Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
        ):
            return False
        shown = self._shown()
        root = self.get_root()
        focus = root.get_focus() if isinstance(root, Gtk.Window) else None
        while focus is not None and focus.get_parent() is not self:
            focus = focus.get_parent()
        if focus is None or focus not in shown:
            return False
        target = shown.index(focus) + steps[keyval]
        if 0 <= target < len(shown):
            shown[target].grab_focus()
        return True


# ---------------------------------------------------------------------------
# Colors, as words and dots
# ---------------------------------------------------------------------------


def color_tooltip(state: AppState, wallpaper: WallpaperView) -> str:
    if wallpaper.color_mode == "palette":
        return f"Colors: {wallpaper.palette} palette"
    if wallpaper.color_mode == "keep":
        return "Colors: don't change"
    scheme = state.scheme_name(wallpaper.scheme or state.default_scheme)
    return f"Colors: from wallpaper · {scheme}" + ("" if wallpaper.scheme else " (default)")


def color_summary(state: AppState, wallpaper: WallpaperView) -> str:
    return color_tooltip(state, wallpaper).removeprefix("Colors: ").capitalize()


def card_colors(state: AppState, wallpaper: WallpaperView) -> dict[str, Any]:
    """The swatches a WallpaperCard shows under its picture (the dark variant)."""
    return {
        "swatches": state.wallpaper_swatches(wallpaper)[:4],
        "swatch_tooltip": color_tooltip(state, wallpaper),
    }


def menu_from(sections: Sequence[Sequence[tuple[str, str]]]) -> Gtk.PopoverMenu:
    """Build a PopoverMenu from [(label, detailed-action), ...] sections."""
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

_CORNER_CSS: Final = """
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
CORNER_RESERVE: Final = 96
#: The selection-mode icon (two tiles ticked in a grid). The prototype ships
#: it in its icon folder; elsewhere the icon theme's fallback shows.
SELECT_ICON: Final = "wio-select-multiple-symbolic"
_corner_styled = False


def select_toggle() -> Gtk.ToggleButton:
    """The selection-mode switch: "Select" with a ticked-tiles icon, "Done" while on."""
    global _corner_styled
    if not _corner_styled:
        add_css(_CORNER_CSS)
        _corner_styled = True
    button = Gtk.ToggleButton()
    content = Adw.ButtonContent(icon_name=SELECT_ICON, label="Select")
    button.set_child(content)
    button.add_css_class("corner-select")
    button.set_tooltip_text("Select several to add, favorite or remove them together")

    def relabel(toggle: Gtk.ToggleButton) -> None:
        on = toggle.get_active()
        content.set_label("Done" if on else "Select")
        content.set_icon_name("object-select-symbolic" if on else SELECT_ICON)

    button.connect("toggled", relabel)
    return button


def hang_on_corner(
    overlay: Gtk.Overlay, anchor: Gtk.Widget, button: Gtk.Widget, inset: int = 18
) -> None:
    """Hang ``button`` over the top-right corner of ``anchor`` (a list or grid
    inside ``overlay``): centered on its top edge, ``inset`` px in from the right.
    It floats, so it stays put while the list scrolls under it."""
    overlay.add_overlay(button)

    def position(_overlay: Gtk.Overlay, child: Gtk.Widget, allocation: Gdk.Rectangle) -> bool:
        # GtkOverlay hands over the rectangle to fill (a caller-allocated out argument).
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
