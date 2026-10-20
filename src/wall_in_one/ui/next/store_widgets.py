"""The Store's widgets: result cards, the inline filter pieces and the preview dialog.

They stay dumb views. The page (`wall_in_one.ui.next.store.StorePage`) owns
searching, paging and downloading, and hands each widget what to show and a
`StoreController` to call back. Pictures arrive from the page when the
adapter delivers them (`StoreCard.set_picture`); nothing here fetches or
decodes anything.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any, Final, Protocol

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, Gtk, Pango

from wall_in_one.ui.next import widgets
from wall_in_one.ui.next.store_state import (
    COLOURS,
    PROVIDER,
    WALLHAVEN,
    StoreDetail,
    StoreItemView,
    nearest_colour,
    short_resolution,
)

CSS: Final = """
.store-card .store-owned image { color: #8ff0a4; }
.store-scrim { background-color: alpha(black, 0.48); border-radius: 12px; }
.store-busy-label { color: white; font-weight: 700; }
button.store-download { min-width: 30px; min-height: 30px; padding: 0; }
.store-detail { font-size: 0.86em; }
.store-detail.busy { color: var(--accent-color); font-weight: 700; }
.store-fact-label { font-size: 0.78em; font-weight: 800; letter-spacing: 0.06em; opacity: 0.6; }
.store-fact-value { font-feature-settings: "tnum"; }
.store-preview-bar { padding: 10px 14px; }
.store-preview-badges { margin: 12px; }
button.store-colour { min-width: 0; min-height: 0; padding: 2px; border-radius: 999px; }
.store-heading { font-size: 1.3em; font-weight: 800; }
.store-dest { font-size: 0.88em; }
.store-filters { padding: 4px 18px 12px 18px; }
.store-filter-label { font-size: 0.78em; font-weight: 800; letter-spacing: 0.06em; opacity: 0.6; }
.store-key-hint { font-size: 0.88em; }
.store-key-hint button.link { padding: 0 2px; min-height: 0; }
"""

_styled = False


def install_css() -> None:
    global _styled
    if not _styled:
        widgets.add_css(CSS)
        _styled = True


class StoreController(Protocol):
    """What cards and the preview dialog ask of the page."""

    def open_item(self, item: StoreItemView) -> None:
        """A click: preview it, or pick it in selection mode."""
        ...

    def preview(self, item: StoreItemView) -> None: ...

    def download(
        self, item: StoreItemView, *, apply: bool = False, variant: str | None = None
    ) -> None: ...

    def apply_item(self, item: StoreItemView) -> None: ...

    def show_in_library(self, item: StoreItemView) -> None: ...

    def toggle_pick(self, item: StoreItemView) -> None: ...

    def popup_menu(self, card: StoreCard, x: float, y: float) -> None: ...

    def search_for(self, text: str, *, like: str = "") -> None: ...

    def search_colour(self, colour: str) -> None: ...

    def open_site(self, item: StoreItemView) -> None: ...

    def dialog_closed(self, dialog: StorePreview) -> None: ...


# -- small pieces --------------------------------------------------------------------


class ColourDot(Gtk.DrawingArea):
    """A filled colour circle; ``selected`` adds a ring in the text colour."""

    def __init__(self, hex_colour: str, size: int = 24, selected: bool = False) -> None:
        super().__init__()
        self._rgba = widgets.rgba("#" + hex_colour.lstrip("#"))
        self._selected = selected
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_draw_func(self._draw)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self.queue_draw()

    def _draw(
        self, _area: Gtk.DrawingArea, cr: cairo.Context[Any], width: int, height: int
    ) -> None:
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


def colour_button(hex_colour: str, tooltip: str, size: int = 24) -> Gtk.Button:
    button = Gtk.Button(tooltip_text=tooltip)
    button.add_css_class("flat")
    button.add_css_class("store-colour")
    button.set_child(ColourDot(hex_colour, size))
    button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
    return button


def owned_pill() -> Gtk.Widget:
    badge = widgets.pill("In library", "object-select-symbolic", "on-image", "store-owned")
    badge.set_tooltip_text("Already in your library")
    return badge


def filter_label(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text.upper(), xalign=0)
    label.add_css_class("store-filter-label")
    return label


class ColourPalette(Gtk.Box):
    """Wallhaven's palette, inline: one colour at a time, all 29 it accepts.

    Clicking the chosen swatch clears it, so "any colour" needs no button of
    its own that would be dead most of the time.
    """

    def __init__(self, on_change: Callable[[str], None], colours: Sequence[str] = COLOURS) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._on_change = on_change
        self._colour = ""
        self.buttons: dict[str, Gtk.Button] = {}
        self._dots: dict[str, ColourDot] = {}
        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            halign=Gtk.Align.START,
            homogeneous=True,
            min_children_per_line=6,
            max_children_per_line=15,
            column_spacing=2,
            row_spacing=2,
        )
        for colour in colours:
            button = colour_button(colour, f"#{colour}", 22)
            child = button.get_child()
            assert isinstance(child, ColourDot)
            self._dots[colour] = child
            button.connect("clicked", self._clicked, colour)
            self.buttons[colour] = button
            flow.append(button)
        self.append(flow)

    def _clicked(self, _button: Gtk.Button, colour: str) -> None:
        self.set_colour("" if self._colour == colour else colour)
        self._on_change(self._colour)

    @property
    def colour(self) -> str:
        return self._colour

    def set_colour(self, colour: str) -> None:
        """Show ``colour`` as chosen (no callback)."""
        self._colour = colour if colour in self._dots else ""
        for key, dot in self._dots.items():
            dot.set_selected(key == self._colour)


class ChipRow(Gtk.Box):
    """Toggle chips that read back as a set of names, at least one always on."""

    def __init__(
        self, names: Sequence[str], chosen: frozenset[str], on_change: Callable[[], None]
    ) -> None:
        super().__init__(spacing=6)
        self._names = tuple(names)
        self._on_change = on_change
        self._quiet = False
        self.chips: dict[str, Gtk.ToggleButton] = {}
        for name in names:
            chip = Gtk.ToggleButton(label=name, active=name in chosen)
            chip.add_css_class("chip")
            chip.connect("toggled", self._toggled)
            self.chips[name] = chip
            self.append(chip)

    def _toggled(self, chip: Gtk.ToggleButton) -> None:
        if self._quiet:
            return
        if not self.chosen:
            # At least one stays on: Wallhaven reads none as all of them.
            self._quiet = True
            chip.set_active(True)
            self._quiet = False
            return
        self._on_change()

    @property
    def chosen(self) -> frozenset[str]:
        return frozenset(name for name, chip in self.chips.items() if chip.get_active())

    def set_chosen(self, chosen: frozenset[str]) -> None:
        self._quiet = True
        for name, chip in self.chips.items():
            chip.set_active(name in chosen)
        self._quiet = False

    def set_enabled(self, name: str, enabled: bool, why: str = "") -> None:
        chip = self.chips[name]
        chip.set_sensitive(enabled)
        chip.set_tooltip_text(why or None)


# -- a result card -------------------------------------------------------------------


class StoreCard(Gtk.Box):
    """A Store result. Looks like a Library card; acts like a shop shelf item.

    Click previews; the hover buttons preview and download (or apply, once the
    wallpaper is in the library). While it downloads or waits its turn, a
    scrim says so: the backend reports no byte progress, so none is drawn.
    """

    def __init__(self, item: StoreItemView, controller: StoreController, width: int = 220) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.item = item
        self._controller = controller
        self.add_css_class("wp-card")
        self.add_css_class("store-card")
        self.in_library = False
        self.busy = ""
        self._selecting = False
        self._apply_blocked = ""
        self._download_blocked = ""

        overlay = Gtk.Overlay()
        self.frame = overlay
        overlay.add_css_class("wp-frame")
        overlay.set_overflow(Gtk.Overflow.HIDDEN)
        self.picture = widgets.Thumb(None, width, round(width * 9 / 16), radius=12, fill=True)
        overlay.set_child(self.picture)

        self._owned = owned_pill()
        self._owned.set_halign(Gtk.Align.END)
        self._owned.set_valign(Gtk.Align.START)
        self._owned.set_margin_top(8)
        self._owned.set_margin_end(8)
        overlay.add_overlay(self._owned)

        badge = short_resolution(item.resolution)
        self._resolution = widgets.pill(
            badge or ("Video" if item.is_moving else "Image"), None, "on-image"
        )
        self._resolution.set_valign(Gtk.Align.END)
        self._resolution.set_margin_start(8)
        self._resolution.set_margin_bottom(10)
        if item.resolution:
            self._resolution.set_tooltip_text(item.resolution)
        overlay.add_overlay(self._resolution)

        actions = Gtk.Box(
            spacing=6, halign=Gtk.Align.END, valign=Gtk.Align.END, margin_end=8, margin_bottom=8
        )
        actions.add_css_class("hover-only")
        actions.append(
            widgets.icon_button(
                "view-reveal-symbolic",
                "Preview",
                lambda *_: controller.preview(item),
                "circular",
                "on-image",
            )
        )
        self.download_button = widgets.icon_button(
            "folder-download-symbolic",
            "Download",
            lambda *_: controller.download(item),
            "circular",
            "suggested-action",
            "store-download",
        )
        actions.append(self.download_button)
        self.apply_button = Gtk.Button(label="Apply", tooltip_text="Show it now")
        for css in ("pill", "suggested-action", "apply-button"):
            self.apply_button.add_css_class(css)
        self.apply_button.connect("clicked", lambda *_: controller.apply_item(item))
        actions.append(self.apply_button)
        self._actions = actions
        overlay.add_overlay(actions)

        self._check = widgets.PickBadge()
        self._check.set_margin_top(8)
        self._check.set_margin_start(8)
        self._check.set_visible(False)
        overlay.add_overlay(self._check)

        # Busy: a scrim, a spinner and what is happening, in the middle.
        self._scrim = Gtk.Box()
        self._scrim.add_css_class("store-scrim")
        overlay.add_overlay(self._scrim)
        busy = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        busy.append(Adw.Spinner(width_request=28, height_request=28))
        self._busy_label = Gtk.Label()
        self._busy_label.add_css_class("store-busy-label")
        busy.append(self._busy_label)
        self._busy_box = busy
        overlay.add_overlay(busy)
        self.append(overlay)

        row = Gtk.Box(spacing=6)
        name = Gtk.Label(
            label=item.title, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END
        )
        name.add_css_class("wp-name")
        row.append(name)
        self._detail = Gtk.Label(label=item.detail)
        self._detail.add_css_class("store-detail")
        self._detail.add_css_class("dimmed")
        self._detail.add_css_class("numeric")
        row.append(self._detail)
        self.append(row)

        click = Gtk.GestureClick(button=1)
        click.connect("released", lambda *_: controller.open_item(self.item))
        overlay.add_controller(click)
        secondary = Gtk.GestureClick(button=3)
        secondary.connect("pressed", lambda _g, _n, x, y: controller.popup_menu(self, x, y))
        overlay.add_controller(secondary)
        long_press = Gtk.GestureLongPress()
        long_press.connect("pressed", lambda _g, x, y: controller.popup_menu(self, x, y))
        overlay.add_controller(long_press)
        self.set_focusable(True)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.set_tooltip_text(item.title)
        self._sync()

    def _on_key(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _code: int,
        state: Gdk.ModifierType,
    ) -> bool:
        """Enter previews, Space picks, Ctrl+Enter downloads (or applies).

        Space is the pick rather than the download because picking is the
        reversible one: pressing it by accident ticks a box, where the other
        reading would start pulling a 40 MB video off somebody's server.
        """
        controller = self._controller
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & Gdk.ModifierType.CONTROL_MASK:
                if self.in_library:
                    if not self._apply_blocked:
                        controller.apply_item(self.item)
                elif not self.busy:
                    controller.download(self.item)
            else:
                controller.preview(self.item)
            return True
        if keyval == Gdk.KEY_space and not state & Gdk.ModifierType.CONTROL_MASK:
            controller.toggle_pick(self.item)
            return True
        if keyval == Gdk.KEY_Menu or (
            keyval == Gdk.KEY_F10 and state & Gdk.ModifierType.SHIFT_MASK
        ):
            controller.popup_menu(self, 40, 40)
            return True
        return False

    @property
    def can_pick(self) -> bool:
        """Only what can still be downloaded can be picked."""
        return not self.in_library and not self.busy

    def update(
        self, *, in_library: bool, busy: str, apply_blocked: str, download_blocked: str = ""
    ) -> None:
        """Show the result's state: ``busy`` is "", "downloading" or "waiting"."""
        self.in_library = in_library
        self.busy = busy
        self._apply_blocked = apply_blocked
        self._download_blocked = download_blocked
        self._sync()

    def _sync(self) -> None:
        busy = bool(self.busy)
        owned = self.in_library
        self._scrim.set_visible(busy)
        self._busy_box.set_visible(busy)
        self._busy_label.set_label("Downloading…" if self.busy == "downloading" else "Waiting…")
        self._actions.set_visible(not busy and not self._selecting)
        self._check.set_visible(self._selecting and self.can_pick)
        self._resolution.set_visible(not busy)
        self._owned.set_visible(owned and not busy)
        self.download_button.set_visible(not owned)
        # Off, with the reason, when nothing can be downloaded (no library folder).
        self.download_button.set_sensitive(not self._download_blocked)
        self.download_button.set_tooltip_text(self._download_blocked or "Download")
        self.apply_button.set_visible(owned)
        self.apply_button.set_sensitive(not self._apply_blocked)
        self.apply_button.set_tooltip_text(self._apply_blocked or "Show it now")
        words = {"downloading": "Downloading…", "waiting": "Waiting…"}
        self._detail.set_label(words.get(self.busy, self.item.detail))
        if busy:
            self._detail.add_css_class("busy")
        else:
            self._detail.remove_css_class("busy")

    def set_picture(self, paintable: Gdk.Paintable) -> None:
        self.picture.set_paintable(paintable)

    def set_selectable(self, on: bool) -> None:
        """Selection mode: show the ring and park Preview/Download/Apply."""
        self._selecting = on
        if not on:
            self.set_checked(False)
        self._sync()

    def set_checked(self, checked: bool) -> None:
        self._check.set_picked(checked)
        if checked:
            self.frame.add_css_class("checked")
        else:
            self.frame.remove_css_class("checked")

    def set_selected(self, selected: bool) -> None:
        if selected:
            self.frame.add_css_class("selected")
        else:
            self.frame.remove_css_class("selected")


# -- the preview dialog --------------------------------------------------------------


def _section(title: str, child: Gtk.Widget) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    label = Gtk.Label(label=title.upper(), xalign=0)
    label.add_css_class("section-label")
    box.append(label)
    box.append(child)
    return box


def _clear(box: Gtk.Box) -> None:
    child = box.get_first_child()
    while child is not None:
        box.remove(child)
        child = box.get_first_child()


class StorePreview(Adw.Dialog):
    """A big look at one result, what its provider says, and the download buttons.

    Left/Right step through the page's results without closing the dialog.
    The facts arrive from the adapter after it opens (`show_detail`); the
    buttons follow the result's state (`refresh`).
    """

    def __init__(
        self,
        controller: StoreController,
        items: Sequence[StoreItemView],
        item: StoreItemView,
        *,
        quality: str = "",
    ) -> None:
        super().__init__(content_width=800, width_request=360, height_request=320)
        self._controller = controller
        self.items: list[StoreItemView] = list(items)
        self.item = item
        self._default_quality = quality
        self._quality = quality
        self.detail: StoreDetail | None = None
        self._state: tuple[str, str] | None = None
        self.in_library = False
        self.busy = ""
        self.apply_blocked = ""
        self.download_blocked = ""

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._title = Adw.WindowTitle()
        header.set_title_widget(self._title)
        nav = Gtk.Box()
        nav.add_css_class("linked")
        self._prev = widgets.icon_button(
            "go-previous-symbolic", "Previous", lambda *_: self.step(-1)
        )
        self._next = widgets.icon_button("go-next-symbolic", "Next", lambda *_: self.step(1))
        nav.append(self._prev)
        nav.append(self._next)
        header.pack_start(nav)
        view.add_top_bar(header)

        self._scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True
        )
        view.set_content(self._scroller)

        bar = Gtk.Box(spacing=8)
        bar.add_css_class("store-preview-bar")
        self._site = Gtk.Button()
        self._site.add_css_class("flat")
        self._site_content = Adw.ButtonContent(
            icon_name="adw-external-link-symbolic", can_shrink=True
        )
        self._site.set_child(self._site_content)
        self._site.connect("clicked", lambda *_: controller.open_site(self.item))
        bar.append(self._site)
        bar.append(Gtk.Box(hexpand=True))
        self.buttons = Gtk.Box(spacing=8)
        bar.append(self.buttons)
        view.add_bottom_bar(bar)
        view.set_bottom_bar_style(Adw.ToolbarStyle.RAISED_BORDER)
        self.set_child(view)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        self.connect("closed", lambda *_: controller.dialog_closed(self))
        self.show_item(item)

    # -- navigation -----------------------------------------------------------
    def _on_key(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _code: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        if isinstance(self.get_focus(), Gtk.Editable | Gtk.Text):
            return False
        if keyval == Gdk.KEY_Left:
            self.step(-1)
            return True
        if keyval == Gdk.KEY_Right:
            self.step(1)
            return True
        return False

    def step(self, direction: int) -> None:
        ids = [item.id for item in self.items]
        if self.item.id not in ids:
            return
        index = ids.index(self.item.id) + direction
        if 0 <= index < len(self.items):
            self._controller.preview(self.items[index])

    # -- content --------------------------------------------------------------
    def show_item(self, item: StoreItemView, picture: Gdk.Paintable | None = None) -> None:
        """Show ``item``: its title and picture now, the facts once `show_detail` brings them."""
        self.item = item
        self.detail = None
        self._quality = self._default_quality
        provider = PROVIDER.get(item.provider)
        site = provider.site if provider is not None else item.provider
        self._title.set_title(item.title)
        self._title.set_subtitle(provider.title if provider is not None else item.provider)
        self.set_title(item.title)
        ids = [entry.id for entry in self.items]
        index = ids.index(item.id) if item.id in ids else -1
        self._prev.set_sensitive(index > 0)
        self._next.set_sensitive(0 <= index < len(ids) - 1)
        self._site_content.set_label(f"Open on {site}")
        self._site.set_tooltip_text(f"Open on {site}")

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_start=20,
            margin_end=20,
            margin_top=4,
            margin_bottom=20,
        )
        overlay = Gtk.Overlay()
        self.picture = widgets.Thumb(picture, 320, 180, radius=12, fill=True)
        overlay.set_child(self.picture)
        self._badges = Gtk.Box(spacing=6, halign=Gtk.Align.START, valign=Gtk.Align.START)
        self._badges.add_css_class("store-preview-badges")
        overlay.add_overlay(self._badges)
        box.append(overlay)
        self._details = Gtk.Box(spacing=28)
        self.status = Gtk.Label(label="Loading the details…", xalign=0)
        self.status.add_css_class("dimmed")
        self._details.append(self.status)
        box.append(self._details)
        self._scroller.set_child(box)
        self._state = None
        self.refresh()

    def set_picture(self, paintable: Gdk.Paintable) -> None:
        if self.picture.paintable is None:
            self.picture.set_paintable(paintable)

    def show_detail(self, detail: StoreDetail) -> None:
        """What the provider said about the result shown."""
        self.detail = detail
        if detail.picture is not None:
            self.picture.set_paintable(detail.picture)
        _clear(self._details)
        if detail.problem and not detail.facts:
            # The download stays possible: the result itself is enough to fetch.
            self.status = Gtk.Label(label=detail.problem, xalign=0, wrap=True)
            self.status.add_css_class("dimmed")
            self._details.append(self.status)
            self._state = None
            self.refresh()
            return
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, hexpand=True)
        if detail.tags:
            left.append(_section("Tags", self._tags(detail.tags)))
        if detail.colours:
            left.append(_section("Colors", self._colours(detail.colours)))
        if detail.variants:
            left.append(_section("Quality", self._qualities(detail.variants)))
        self._details.append(left)
        self._details.append(self._facts(detail.facts))
        self._state = None  # the quality toggle may have changed what the buttons say
        self.refresh()

    def _tags(self, tags: Sequence[str]) -> Gtk.Widget:
        wrap = Adw.WrapBox(child_spacing=6, line_spacing=6)
        for tag in tags:
            button = Gtk.Button(label=tag)
            button.add_css_class("pill")
            button.add_css_class("chip")
            button.set_tooltip_text(f"Search “{tag}”")
            button.connect("clicked", lambda *_, t=tag: self._controller.search_for(t))
            wrap.append(button)
        if self.item.provider == WALLHAVEN:
            more = Gtk.Button()
            more.set_child(
                Adw.ButtonContent(icon_name="edit-find-symbolic", label="More like this")
            )
            for css in ("flat", "pill", "chip"):
                more.add_css_class(css)
            more.set_tooltip_text("Find similar wallpapers")
            identifier = self.item.id.partition(":")[2] or self.item.id
            more.connect(
                "clicked",
                lambda *_: self._controller.search_for(f"like:{identifier}", like=self.item.title),
            )
            wrap.append(more)
        return wrap

    def _colours(self, colours: Sequence[str]) -> Gtk.Widget:
        row = Gtk.Box(spacing=8)
        for colour in colours:
            nearest = nearest_colour(colour)
            button = colour_button(colour, f"#{colour} · find more in this colour", 26)
            button.set_sensitive(bool(nearest))
            button.connect("clicked", lambda *_, c=nearest: self._controller.search_colour(c))
            row.append(button)
        return row

    def _qualities(self, variants: Sequence[str]) -> Gtk.Widget:
        group = Adw.ToggleGroup(halign=Gtk.Align.START)
        group.add(Adw.Toggle(name="best", label="Automatic (as listed)"))
        for variant in variants:
            group.add(Adw.Toggle(name=variant, label=variant.upper()))
        group.set_active_name(self._quality if self._quality in variants else "best")

        def chosen(toggles: Adw.ToggleGroup, _param: object) -> None:
            name = toggles.get_active_name() or "best"
            self._quality = "" if name == "best" else name

        group.connect("notify::active-name", chosen)
        return group

    @staticmethod
    def _facts(facts: Sequence[tuple[str, str]]) -> Gtk.Widget:
        grid = Gtk.Grid(row_spacing=7, column_spacing=16, valign=Gtk.Align.START)
        grid.set_size_request(250, -1)
        for row, (label, value) in enumerate(facts):
            name = Gtk.Label(label=label.upper(), xalign=0)
            name.add_css_class("store-fact-label")
            text = Gtk.Label(label=value, xalign=0, selectable=True)
            text.set_ellipsize(Pango.EllipsizeMode.END)
            text.set_max_width_chars(40)
            text.add_css_class("store-fact-value")
            grid.attach(name, 0, row, 1, 1)
            grid.attach(text, 1, row, 1, 1)
        return grid

    # -- state ------------------------------------------------------------------------
    def update(
        self, *, in_library: bool, busy: str, apply_blocked: str, download_blocked: str
    ) -> None:
        self.in_library = in_library
        self.busy = busy
        self.apply_blocked = apply_blocked
        self.download_blocked = download_blocked
        self.refresh()

    def _close_then(self, act: Callable[[StoreItemView], None]) -> None:
        item = self.item
        self.close()
        act(item)

    def _download_and_apply(self, item: StoreItemView) -> None:
        self._controller.download(item, apply=True, variant=self._quality)

    def refresh(self) -> None:
        """The badge and buttons for the result's state, rebuilt only when it changes."""
        mode = "busy" if self.busy else "owned" if self.in_library else "get"
        state = (mode, f"{self.item.id}|{self.busy}|{self.apply_blocked}|{self.download_blocked}")
        if state == self._state:
            return
        self._state = state
        _clear(self._badges)
        if self.in_library:
            self._badges.append(owned_pill())
        badge = short_resolution(self.item.resolution)
        if badge:
            self._badges.append(widgets.pill(badge, None, "on-image"))
        _clear(self.buttons)
        item = self.item
        if mode == "busy":
            spinner = Adw.Spinner(width_request=18, height_request=18)
            self.buttons.append(spinner)
            self.buttons.append(
                Gtk.Label(
                    label="Downloading…"
                    if self.busy == "downloading"
                    else "Waiting for its turn\u2026"
                )
            )
        elif mode == "owned":
            show = Gtk.Button(label="Show in Library")
            show.add_css_class("pill")
            show.connect("clicked", lambda *_: self._close_then(self._controller.show_in_library))
            apply = Gtk.Button(label="Apply")
            apply.add_css_class("pill")
            apply.add_css_class("suggested-action")
            for button in (show, apply):
                button.set_sensitive(not self.apply_blocked)
                button.set_tooltip_text(self.apply_blocked or None)
            if not self.apply_blocked:
                apply.set_tooltip_text("Show it now")
            apply.connect("clicked", lambda *_: self._close_then(self._controller.apply_item))
            self.buttons.append(show)
            self.buttons.append(apply)
        else:
            download = Gtk.Button(label="Download")
            download.add_css_class("pill")
            download.connect(
                "clicked",
                lambda *_: self._controller.download(item, variant=self._quality),
            )
            apply = Gtk.Button(label="Download & apply")
            apply.add_css_class("pill")
            apply.add_css_class("suggested-action")
            apply.connect("clicked", lambda *_: self._close_then(self._download_and_apply))
            for button in (download, apply):
                button.set_sensitive(not self.download_blocked)
                button.set_tooltip_text(self.download_blocked or None)
            if not self.download_blocked:
                download.set_tooltip_text("Save it to your library")
                apply.set_tooltip_text("Download it, then show it right away")
            self.buttons.append(download)
            self.buttons.append(apply)
