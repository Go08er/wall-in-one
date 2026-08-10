"""Full-page editor for named, ordered wallpaper rotations."""

from __future__ import annotations

import math
from functools import cmp_to_key
from pathlib import Path
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")

from gi.repository import Adw, Gdk, Graphene, Gsk, Gtk, Pango

from wall_in_one.library import playlists
from wall_in_one.library.model import MediaItem
from wall_in_one.ui.thumbnails import ThumbnailLoader

if TYPE_CHECKING:
    from wall_in_one.session import Session
    from wall_in_one.ui.app import Application


SOURCE_PREFIX = "media:"
FLIP_CURVE = (0.20, 0.75, 0.18, 1.0)
SETTLE_CURVE = (0.18, 0.82, 0.22, 1.0)
CSS_EASE_CURVE = (0.25, 0.10, 0.25, 1.0)


class _ReorderList(Gtk.Widget):
    """A vertical container whose children move without changing membership."""

    def __init__(self) -> None:
        super().__init__(valign=Gtk.Align.START)
        self._rows: list[Gtk.Widget] = []
        self._layout_offsets: dict[Gtk.Widget, float] = {}
        self._flip_offsets: dict[Gtk.Widget, float] = {}
        self._scales: dict[Gtk.Widget, float] = {}
        self._sort_func: Any = None
        self._lifted: Gtk.Widget | None = None
        self._row_tops: tuple[float, ...] = ()
        self._row_heights: tuple[float, ...] = ()
        self._flip_animations: dict[Gtk.Widget, Adw.Animation] = {}
        self._scale_animation: Adw.Animation | None = None
        self._settle_animation: Adw.Animation | None = None
        self._placeholder_source = -1
        self._placeholder_target = -1
        self._placeholder_height = 0.0
        self._gesture_row: _PlaylistEntryRow | None = None
        self._on_drag_started: Any = None
        self._on_drag_updated: Any = None
        self._on_drag_finished: Any = None
        self.reorder_gesture: Gtk.GestureDrag | None = None

    def attach_reorder_gesture(self, scrolled_window: Gtk.ScrolledWindow) -> None:
        # Measure a gesture in a coordinate space that nothing it causes can
        # move. Rows move during sorting and this list moves while scrolling;
        # the enclosing scrolled window moves for neither operation.
        self.reorder_gesture = Gtk.GestureDrag()
        self.reorder_gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.reorder_gesture.connect("drag-begin", self._drag_begin)
        self.reorder_gesture.connect("drag-update", self._drag_update)
        self.reorder_gesture.connect("drag-end", self._drag_end)
        self.reorder_gesture.connect("cancel", self._drag_cancel)
        scrolled_window.add_controller(self.reorder_gesture)

    def set_reorder_handlers(
        self,
        on_drag_started: Any,
        on_drag_updated: Any,
        on_drag_finished: Any,
    ) -> None:
        self._on_drag_started = on_drag_started
        self._on_drag_updated = on_drag_updated
        self._on_drag_finished = on_drag_finished

    def _handle_row_at(self, x: float, y: float) -> _PlaylistEntryRow | None:
        picked = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        while picked is not None and picked is not self:
            for row in self._rows:
                if isinstance(row, _PlaylistEntryRow) and picked is row.handle:
                    return row
            picked = picked.get_parent()
        return None

    def _drag_begin(self, gesture: Gtk.GestureDrag, start_x: float, start_y: float) -> None:
        point = Graphene.Point()
        point.init(start_x, start_y)
        controller_widget = gesture.get_widget()
        translated = (
            controller_widget.compute_point(self, point)
            if controller_widget is not None
            else (False, point)
        )
        row = self._handle_row_at(translated[1].x, translated[1].y) if translated[0] else None
        if row is None or self._on_drag_started is None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        # The list is inside a kinetic scroller. Claim before changing any
        # visual state so its drag gesture cannot take this same sequence.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._gesture_row = row
        row.handle.set_cursor(Gdk.Cursor.new_from_name("grabbing", None))
        self._on_drag_started(row, translated[1].x, translated[1].y)

    def _drag_update(self, _gesture: Gtk.GestureDrag, _offset_x: float, offset_y: float) -> None:
        if self._gesture_row is not None and self._on_drag_updated is not None:
            self._on_drag_updated(self._gesture_row, offset_y)

    def _drag_end(self, _gesture: Gtk.GestureDrag, _offset_x: float, offset_y: float) -> None:
        row = self._gesture_row
        self._gesture_row = None
        if row is None:
            return
        row.handle.set_cursor(Gdk.Cursor.new_from_name("grab", None))
        if self._on_drag_finished is not None:
            self._on_drag_finished(row, offset_y, False)

    def _drag_cancel(self, _gesture: Gtk.GestureDrag, _sequence: Gdk.EventSequence) -> None:
        row = self._gesture_row
        self._gesture_row = None
        if row is None:
            return
        row.handle.set_cursor(Gdk.Cursor.new_from_name("grab", None))
        if self._on_drag_finished is not None:
            self._on_drag_finished(row, 0.0, True)

    def append(self, row: Gtk.Widget) -> None:
        self._rows.append(row)
        self._layout_offsets[row] = 0.0
        self._flip_offsets[row] = 0.0
        self._scales[row] = 1.0
        row.set_parent(self)
        self.queue_resize()

    def remove(self, row: Gtk.Widget) -> None:
        self._rows.remove(row)
        self._layout_offsets.pop(row, None)
        self._flip_offsets.pop(row, None)
        self._scales.pop(row, None)
        animation = self._flip_animations.pop(row, None)
        if animation is not None:
            animation.pause()
        row.unparent()
        self.queue_resize()

    def set_sort_func(self, sort_func: Any) -> None:
        self._sort_func = sort_func

    def invalidate_sort(self) -> None:
        if self._sort_func is not None:
            self._rows.sort(key=cmp_to_key(self._sort_func))
        # Cached heights are in row order. A keyboard sort changes that order
        # even though the same stable widgets remain children.
        self._row_tops = ()
        self._row_heights = ()
        self.queue_allocate()
        self.queue_draw()

    def get_row_at_index(self, position: int) -> Gtk.Widget | None:
        if 0 <= position < len(self._rows):
            return self._rows[position]
        return None

    def rows(self) -> tuple[Gtk.Widget, ...]:
        return tuple(self._rows)

    def row_heights(self) -> tuple[float, ...]:
        if len(self._row_heights) == len(self._rows):
            return self._row_heights
        width = max(self.get_width(), 1)
        return tuple(float(row.measure(Gtk.Orientation.VERTICAL, width)[1]) for row in self._rows)

    def row_bounds(self, top: float) -> tuple[tuple[float, float], ...]:
        heights = self.row_heights()
        if len(self._row_tops) == len(heights):
            tops = self._row_tops
        else:
            natural = 0.0
            built: list[float] = []
            for height in heights:
                built.append(natural)
                natural += height
            tops = tuple(built)
        return tuple((top + y, top + y + height) for y, height in zip(tops, heights, strict=True))

    def set_lifted(self, row: Gtk.Widget | None) -> None:
        self._lifted = row
        if row is not None:
            self._animate_scale(row, 1.018, 140, CSS_EASE_CURVE)
        self.queue_draw()

    def set_offset(self, row: Gtk.Widget, offset: float) -> None:
        if self._layout_offsets.get(row) == offset:
            return
        self._layout_offsets[row] = offset
        self.queue_allocate()

    def animate_siblings(self, lifted: Gtk.Widget, targets: tuple[float, ...]) -> None:
        tops = self._natural_tops()
        first = tuple(
            top + self._layout_offsets.get(row, 0.0) + self._flip_offsets.get(row, 0.0)
            for row, top in zip(self._rows, tops, strict=True)
        )
        old_layout = tuple(self._layout_offsets.get(row, 0.0) for row in self._rows)
        for row, target in zip(self._rows, targets, strict=True):
            if row is not lifted:
                self._layout_offsets[row] = target
        last = tuple(
            top + self._layout_offsets.get(row, 0.0)
            for row, top in zip(self._rows, tops, strict=True)
        )
        deltas = playlists.flip_deltas(first, last)
        for row, old, new, delta in zip(self._rows, old_layout, targets, deltas, strict=True):
            # A row whose layout slot did not move keeps its current animation;
            # canceling it would create the stale-animation snap FLIP avoids.
            if row is not lifted and abs(old - new) > 0.5:
                self._start_flip(row, delta)
        self.queue_allocate()

    def capture_positions(self) -> dict[Gtk.Widget, float]:
        """Capture current presentation y, including an interrupted FLIP."""
        return {
            row: top + self._layout_offsets.get(row, 0.0) + self._flip_offsets.get(row, 0.0)
            for row, top in zip(self._rows, self._natural_tops(), strict=True)
        }

    def animate_from(self, first: dict[Gtk.Widget, float]) -> None:
        """Play the same measured FLIP path after a keyboard reorder."""
        tops = self._natural_tops()
        rows = tuple(row for row in self._rows if row in first)
        last_by_row = {
            row: top + self._layout_offsets.get(row, 0.0)
            for row, top in zip(self._rows, tops, strict=True)
        }
        deltas = playlists.flip_deltas(
            tuple(first[row] for row in rows), tuple(last_by_row[row] for row in rows)
        )
        for row, delta in zip(rows, deltas, strict=True):
            if delta != 0.0:
                self._start_flip(row, delta)
        self.queue_allocate()

    def set_placeholder(self, source: int, target: int, height: float) -> None:
        self._placeholder_source = source
        self._placeholder_target = target
        self._placeholder_height = height
        self.queue_draw()

    def _animations_enabled(self) -> bool:
        settings = Gtk.Settings.get_default()
        return settings is None or bool(settings.get_property("gtk-enable-animations"))

    def _start_flip(self, row: Gtk.Widget, delta: float) -> None:
        previous = self._flip_animations.pop(row, None)
        if previous is not None:
            previous.pause()
        self._flip_offsets[row] = 0.0 if not self._animations_enabled() else delta
        if self._flip_offsets[row] == 0.0:
            return

        def apply(progress: float) -> None:
            eased = playlists.cubic_bezier(progress, *FLIP_CURVE)
            self._flip_offsets[row] = delta * (1.0 - eased)
            self.queue_allocate()

        target = Adw.CallbackAnimationTarget.new(apply)
        animation = Adw.TimedAnimation.new(self, 0.0, 1.0, 230, target)
        animation.set_easing(Adw.Easing.LINEAR)
        self._flip_animations[row] = animation
        animation.play()

    def _animate_scale(
        self,
        row: Gtk.Widget,
        wanted: float,
        duration: int,
        curve: tuple[float, float, float, float],
    ) -> None:
        if self._scale_animation is not None:
            self._scale_animation.pause()
        start = self._scales.get(row, 1.0)
        if not self._animations_enabled():
            self._scales[row] = wanted
            self.queue_draw()
            return

        def apply(progress: float) -> None:
            eased = playlists.cubic_bezier(progress, *curve)
            self._scales[row] = start + (wanted - start) * eased
            self.queue_draw()

        target = Adw.CallbackAnimationTarget.new(apply)
        animation = Adw.TimedAnimation.new(self, 0.0, 1.0, duration, target)
        animation.set_easing(Adw.Easing.LINEAR)
        self._scale_animation = animation
        animation.play()

    def settle_lifted(self, row: Gtk.Widget, target: float, on_done: Any) -> None:
        if self._settle_animation is not None:
            self._settle_animation.pause()
        if self._scale_animation is not None:
            self._scale_animation.pause()
        start = self._layout_offsets.get(row, 0.0)
        distance = target - start
        self._scales[row] = 1.018

        if not self._animations_enabled() or abs(distance) <= 0.5:
            self._layout_offsets[row] = target
            self._scales[row] = 1.0
            self.queue_allocate()
            self.queue_draw()
            on_done(None)
            return

        def apply(progress: float) -> None:
            eased = playlists.cubic_bezier(progress, *SETTLE_CURVE)
            self._layout_offsets[row] = start + distance * eased
            self._scales[row] = 1.018 + (1.0 - 1.018) * eased
            self.queue_allocate()
            self.queue_draw()

        animation_target = Adw.CallbackAnimationTarget.new(apply)
        animation = Adw.TimedAnimation.new(self, 0.0, 1.0, 260, animation_target)
        animation.set_easing(Adw.Easing.LINEAR)
        animation.connect("done", on_done)
        self._settle_animation = animation
        animation.play()

    def clear_motion(self) -> None:
        for animation in self._flip_animations.values():
            animation.pause()
        if self._scale_animation is not None:
            self._scale_animation.pause()
        if self._settle_animation is not None:
            self._settle_animation.pause()
        self._flip_animations.clear()
        self._scale_animation = None
        self._settle_animation = None
        for row in self._rows:
            self._layout_offsets[row] = 0.0
            self._flip_offsets[row] = 0.0
            self._scales[row] = 1.0
        self._lifted = None
        self._placeholder_source = -1
        self._placeholder_target = -1
        self._placeholder_height = 0.0
        self.queue_allocate()
        self.queue_draw()

    def _natural_tops(self) -> tuple[float, ...]:
        tops: list[float] = []
        top = 0.0
        for height in self.row_heights():
            tops.append(top)
            top += height
        return tuple(tops)

    def do_measure(self, orientation: Gtk.Orientation, for_size: int) -> tuple[int, int, int, int]:
        measured = [row.measure(orientation, for_size) for row in self._rows]
        if orientation == Gtk.Orientation.HORIZONTAL:
            minimum = max((size[0] for size in measured), default=0)
            natural = max((size[1] for size in measured), default=0)
        else:
            minimum = sum(size[0] for size in measured)
            natural = sum(size[1] for size in measured)
        return minimum, natural, -1, -1

    def do_size_allocate(self, width: int, _height: int, _baseline: int) -> None:
        heights = tuple(
            max(row.measure(Gtk.Orientation.VERTICAL, width)[1], 1) for row in self._rows
        )
        natural = 0.0
        tops: list[float] = []
        for row, height in zip(self._rows, heights, strict=True):
            tops.append(natural)
            point = Graphene.Point()
            point.init(
                0.0,
                natural + self._layout_offsets.get(row, 0.0) + self._flip_offsets.get(row, 0.0),
            )
            row.allocate(width, height, -1, Gsk.Transform().translate(point))
            natural += height
        self._row_tops = tuple(tops)
        self._row_heights = tuple(float(height) for height in heights)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        self._snapshot_placeholder(snapshot)
        for row in self._rows:
            if row is not self._lifted:
                self._snapshot_row(row, snapshot)
        if self._lifted is not None:
            self._snapshot_row(self._lifted, snapshot)

    def _snapshot_row(self, row: Gtk.Widget, snapshot: Gtk.Snapshot) -> None:
        scale = self._scales.get(row, 1.0)
        if abs(scale - 1.0) < 0.0001:
            self.snapshot_child(row, snapshot)
            return
        index = self._rows.index(row)
        y = (
            self._natural_tops()[index]
            + self._layout_offsets.get(row, 0.0)
            + self._flip_offsets.get(row, 0.0)
        )
        height = self.row_heights()[index]
        centre = Graphene.Point()
        centre.init(float(self.get_width()) / 2.0, y + height / 2.0)
        inverse = Graphene.Point()
        inverse.init(-centre.x, -centre.y)
        snapshot.save()
        snapshot.translate(centre)
        snapshot.scale(scale, scale)
        snapshot.translate(inverse)
        self.snapshot_child(row, snapshot)
        snapshot.restore()

    def _snapshot_placeholder(self, snapshot: Gtk.Snapshot) -> None:
        y = self._placeholder_top()
        if y is None:
            return
        bounds = Graphene.Rect()
        bounds.init(
            1.0,
            y + 1.0,
            max(float(self.get_width()) - 2.0, 0.0),
            self._placeholder_height - 2.0,
        )
        context = snapshot.append_cairo(bounds)
        radius = min(24.0, bounds.size.width / 2.0, bounds.size.height / 2.0)
        left = bounds.origin.x
        top = bounds.origin.y
        right = left + bounds.size.width
        bottom = top + bounds.size.height
        context.new_sub_path()
        context.arc(right - radius, top + radius, radius, -math.pi / 2.0, 0.0)
        context.arc(right - radius, bottom - radius, radius, 0.0, math.pi / 2.0)
        context.arc(left + radius, bottom - radius, radius, math.pi / 2.0, math.pi)
        context.arc(left + radius, top + radius, radius, math.pi, math.pi * 1.5)
        context.close_path()
        found, accent = self.get_style_context().lookup_color("accent_color")
        if not found:
            accent = self.get_color()
        context.set_source_rgba(accent.red, accent.green, accent.blue, 0.06)
        context.fill_preserve()
        context.set_source_rgba(accent.red, accent.green, accent.blue, 0.44)
        context.set_line_width(2.0)
        context.set_dash((8.0, 7.0))
        context.stroke()

    def _placeholder_top(self) -> float | None:
        if self._placeholder_source < 0 or self._placeholder_target < 0:
            return None
        tops = self._natural_tops()
        if self._placeholder_source >= len(tops):
            return None
        heights = self.row_heights()
        target = min(max(self._placeholder_target, 0), len(tops) - 1)
        # Describe the landing slot from the target row's natural allocation,
        # not from the lifted row's transient position. For a downward move the
        # gap follows the target row after it slides up by the source height.
        y = tops[target]
        if target > self._placeholder_source:
            y += heights[target] - heights[self._placeholder_source]
        return y

    def do_dispose(self) -> None:
        for row in tuple(self._rows):
            row.unparent()
        self._rows.clear()
        self._layout_offsets.clear()
        self._flip_offsets.clear()
        self._scales.clear()
        Gtk.Widget.do_dispose(self)  # type: ignore[attr-defined]


class _MediaCard(Gtk.Box):
    """A pairing thumbnail that can be dragged, clicked, or removed."""

    def __init__(
        self,
        item: MediaItem,
        payload: str,
        on_activate: Any,
        *,
        on_drag_started: Any | None = None,
        on_drag_finished: Any | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.item = item
        self.add_css_class("card")
        self.set_size_request(180, -1)

        self.picture = Gtk.Picture(width_request=180, height_request=102)
        self.picture.set_content_fit(Gtk.ContentFit.COVER)
        self.append(self.picture)
        title = Gtk.Label(label=item.name, ellipsize=Pango.EllipsizeMode.END)
        title.add_css_class("caption")
        self.append(title)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        actions.set_halign(Gtk.Align.CENTER)
        add = Gtk.Button(
            icon_name="list-add-symbolic",
            tooltip_text="Add to playlist",
        )
        add.add_css_class("flat")
        add.connect("clicked", on_activate)
        actions.append(add)
        self.append(actions)

        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drag.connect(
            "prepare",
            lambda *_arguments: Gdk.ContentProvider.new_for_value(payload),
        )
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-cancel", self._drag_cancel)
        drag.connect("drag-end", self._drag_end)
        self._on_drag_started = on_drag_started
        self._on_drag_finished = on_drag_finished
        self.add_controller(drag)

    def _drag_begin(self, source: Gtk.DragSource, _drag: Gdk.Drag) -> None:
        """Keep the gesture visually tied to the image the person grabbed."""
        if self._on_drag_started is not None:
            self._on_drag_started()
        paintable = self.picture.get_paintable()
        if paintable is None:
            paintable = Gtk.WidgetPaintable.new(self)
        source.set_icon(paintable, 90, 51)

    def _drag_cancel(
        self, _source: Gtk.DragSource, _drag: Gdk.Drag, _reason: Gdk.DragCancelReason
    ) -> bool:
        self._finish_drag()
        return False

    def _drag_end(self, _source: Gtk.DragSource, _drag: Gdk.Drag, _delete_data: bool) -> None:
        self._finish_drag()

    def _finish_drag(self) -> None:
        if self._on_drag_finished is not None:
            self._on_drag_finished()

    def show_thumbnail(self, _item: MediaItem, texture: Gdk.Texture | None) -> None:
        self.picture.set_paintable(texture)


class _PlaylistEntryRow(Gtk.ListBoxRow):
    """A stable sortable row with one passive reorder affordance."""

    def __init__(
        self,
        identifier: str,
        item: MediaItem | None,
        source: Path,
        on_remove: Any,
        on_key: Any,
    ) -> None:
        super().__init__(selectable=False, activatable=False, focusable=True)
        self.identifier = identifier
        self.item = item

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # The landing space is never inserted into GTK's child sequence. The
        # revealer lives for exactly as long as its row, so drag motion only
        # changes a property even while its closing animation is running.
        self.spacer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            transition_duration=160,
        )
        self.spacer.set_child(Gtk.Box(height_request=24))
        body.append(self.spacer)

        self.surface = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.surface.add_css_class("wio-reorder-row")
        self.surface.set_margin_top(8)
        self.surface.set_margin_bottom(8)
        self.surface.set_margin_start(8)
        self.surface.set_margin_end(8)

        # A button brings a competing click gesture and also aligned the grip
        # against its own button chrome. The passive centre box remains the
        # same generous hit target while centring the six dots in the row.
        self.handle = Gtk.CenterBox(width_request=46, height_request=72)
        self.handle.set_tooltip_text("Drag to reorder")
        self.handle.set_valign(Gtk.Align.CENTER)
        self.handle.add_css_class("wio-reorder-handle")
        grip = Gtk.DrawingArea(width_request=21, height_request=32)

        def draw_grip(widget: Gtk.DrawingArea, context: Any, _width: int, _height: int) -> None:
            colour = widget.get_color()
            context.set_source_rgba(colour.red, colour.green, colour.blue, colour.alpha)
            for x in (4.0, 17.0):
                for y in (3.0, 16.0, 29.0):
                    context.arc(x, y, 3.0, 0.0, math.tau)
                    context.fill()

        grip.set_draw_func(draw_grip)
        grip.set_halign(Gtk.Align.CENTER)
        grip.set_valign(Gtk.Align.CENTER)
        grip.set_can_target(False)
        self.handle.set_center_widget(grip)
        self.handle.set_cursor(Gdk.Cursor.new_from_name("grab", None))
        self.surface.append(self.handle)

        self.picture = Gtk.Picture(width_request=96, height_request=54)
        self.picture.set_content_fit(Gtk.ContentFit.COVER)
        self.surface.append(self.picture)

        name = item.name if item is not None else f"Missing · {source.name}"
        self.title = Gtk.Label(label=name, xalign=0.0, hexpand=True)
        self.title.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.title.set_lines(2)
        self.title.set_wrap(True)
        self.title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.title.add_css_class("heading")
        self.surface.append(self.title)

        remove = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="Remove from playlist")
        remove.add_css_class("flat")
        remove.connect("clicked", on_remove)
        self.surface.append(remove)
        body.append(self.surface)
        self.set_child(body)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", on_key)
        self.add_controller(keys)

    def show_thumbnail(self, _item: MediaItem, texture: Gdk.Texture | None) -> None:
        self.picture.set_paintable(texture)


class PlaylistsPage(Gtk.Box):
    """Create playlists and arrange their stable entries without a terminal."""

    def __init__(self, application: Application) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._app = application
        self._session: Session | None = None
        self._selected = ""
        self._playlist_rows: dict[Gtk.ListBoxRow, str] = {}
        self._playlist_rows_by_id: dict[str, tuple[Gtk.ListBoxRow, Gtk.Label, Gtk.Label]] = {}
        self._playlist_positions: dict[str, int] = {}
        self._loader = ThumbnailLoader()
        self._source_cards: dict[_MediaCard, MediaItem] = {}
        self._source_cards_by_path: dict[Path, _MediaCard] = {}
        self._source_positions: dict[Path, int] = {}
        self._entry_rows: dict[str, _PlaylistEntryRow] = {}
        # Kept as an alias because permanence is about stable identity, not the
        # name callers used when the editor happened to be a card grid.
        self._entry_cards = self._entry_rows
        self._entry_items: dict[str, MediaItem | None] = {}
        self._entry_positions: dict[str, int] = {}
        self._revealed_slot = -1
        self._dragging = False
        self._playlist_change_after_drag = False
        self._dragged_row: _PlaylistEntryRow | None = None
        self._drag_source_index = -1
        self._drag_target_slot = -1
        self._drag_heights: tuple[float, ...] = ()
        self._drag_pointer_offset = 0.0
        self._drag_grab_offset_y = 0.0
        self._drag_pointer_y: float | None = None
        self._drag_start_scroll = 0.0
        self._autoscroll_tick = 0
        self._autoscroll_frame_time: int | None = None
        self._editor_id = ""

        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        split.set_position(300)
        split.set_shrink_start_child(False)
        split.set_shrink_end_child(False)
        split.set_start_child(self._sidebar())

        self._editor_scroll = Gtk.ScrolledWindow(vexpand=True)
        self._editor = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self._editor.set_margin_top(18)
        self._editor.set_margin_bottom(24)
        self._editor.set_margin_start(24)
        self._editor.set_margin_end(24)
        self._editor_scroll.set_child(self._editor)
        split.set_end_child(self._editor_scroll)
        self.append(split)
        self._show_empty()

    def shutdown(self) -> None:
        self._loader.shutdown()

    def _sidebar(self) -> Gtk.Widget:
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sidebar.set_margin_top(12)
        sidebar.set_margin_bottom(12)
        sidebar.set_margin_start(12)
        sidebar.set_margin_end(6)
        title = Gtk.Label(label="Playlists", xalign=0.0)
        title.add_css_class("title-2")
        sidebar.append(title)
        note = Gtk.Label(
            label="A one-item playlist is a fixed choice. Longer lists rotate in this order.",
            xalign=0.0,
            wrap=True,
        )
        note.add_css_class("dim-label")
        sidebar.append(note)

        add_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._new_name = Gtk.Entry(hexpand=True, placeholder_text="New playlist")
        self._new_name.connect("activate", self._create)
        add_box.append(self._new_name)
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Create playlist")
        add.connect("clicked", self._create)
        add_box.append(add)
        sidebar.append(add_box)

        self._sidebar_scroll = Gtk.ScrolledWindow(
            vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER
        )
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._list.add_css_class("boxed-list")
        self._list.set_sort_func(self._compare_playlist_rows)
        self._list.connect("row-selected", self._on_selected)
        self._sidebar_scroll.set_child(self._list)
        sidebar.append(self._sidebar_scroll)
        return sidebar

    def refresh(self, session: Session) -> None:
        """Diff authoring state without replacing the editor under the user.

        Playlist edits are frequent, small changes. Rebuilding this page used
        to destroy the search entry and all three scroll adjustments after
        every add, remove, rename, or drag. Rows and cards are keyed by their
        stable model identities instead, exactly like the main media grid.
        """
        self._session = session
        available = session.playlists.all()
        incoming = {playlist.id: playlist for playlist in available}
        for identifier, (row, _name, _detail) in list(self._playlist_rows_by_id.items()):
            if identifier not in incoming:
                self._list.remove(row)
                self._playlist_rows.pop(row, None)
                del self._playlist_rows_by_id[identifier]

        self._playlist_positions = {playlist.id: index for index, playlist in enumerate(available)}
        for playlist in available:
            existing = self._playlist_rows_by_id.get(playlist.id)
            if existing is None:
                row, name, detail = self._new_playlist_row()
                self._playlist_rows[row] = playlist.id
                self._playlist_rows_by_id[playlist.id] = (row, name, detail)
                self._list.append(row)
            else:
                row, name, detail = existing
            name.set_label(playlist.name)
            detail.set_label(self._playlist_detail(session, playlist))
        self._list.invalidate_sort()

        wanted = self._selected if self._selected in incoming else ""
        if not wanted and available:
            wanted = available[0].id
        selected_record = self._playlist_rows_by_id.get(wanted)
        selected_row = selected_record[0] if selected_record is not None else None
        self._list.select_row(selected_row)
        if wanted != self._selected:
            self._selected = wanted
            self._show_editor()
        elif wanted:
            self._sync_editor()
        else:
            self._show_empty()

    @staticmethod
    def _new_playlist_row() -> tuple[Gtk.ListBoxRow, Gtk.Label, Gtk.Label]:
        row = Gtk.ListBoxRow()
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        content.set_margin_top(8)
        content.set_margin_bottom(8)
        content.set_margin_start(10)
        content.set_margin_end(10)
        name = Gtk.Label(xalign=0.0)
        name.add_css_class("heading")
        detail = Gtk.Label(xalign=0.0)
        detail.add_css_class("caption")
        detail.add_css_class("dim-label")
        content.append(name)
        content.append(detail)
        row.set_child(content)
        return row, name, detail

    @staticmethod
    def _playlist_detail(session: Session, playlist: playlists.Playlist) -> str:
        return (
            f"{len(playlist)} item{'s' if len(playlist) != 1 else ''}"
            + (" · playing" if playlist.id == session.active_playlist() else "")
            + (" · default" if playlist.id == session.settings.active_playlist else "")
        )

    def _compare_playlist_rows(self, first: Gtk.ListBoxRow, second: Gtk.ListBoxRow) -> int:
        first_id = self._playlist_rows.get(first, "")
        second_id = self._playlist_rows.get(second, "")
        end = len(self._playlist_positions)
        return self._playlist_positions.get(first_id, end) - self._playlist_positions.get(
            second_id, end
        )

    def _on_selected(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        selected = self._playlist_rows.get(row, "") if row is not None else ""
        if selected == self._selected and self._editor_id == selected:
            return
        self._selected = selected
        self._show_editor()

    def _clear_editor(self) -> None:
        self._assert_not_dragging()
        self._clear_drop_slot()
        while (child := self._editor.get_first_child()) is not None:
            self._editor.remove(child)
        self._source_cards.clear()
        self._source_cards_by_path.clear()
        self._source_positions.clear()
        self._entry_rows.clear()
        self._entry_items.clear()
        self._entry_positions.clear()
        self._editor_id = ""

    def _show_empty(self) -> None:
        self._clear_editor()
        page = Adw.StatusPage(
            title="Make a playlist",
            description="Create a playlist on the left, then add media from your library.",
            icon_name="view-list-symbolic",
        )
        page.set_vexpand(True)
        self._editor.append(page)

    def _show_editor(self) -> None:
        session = self._session
        playlist = session.playlists.get(self._selected) if session is not None else None
        if session is None or playlist is None:
            self._show_empty()
            return
        self._clear_editor()
        self._editor_id = playlist.id

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._name_entry = Gtk.Entry(text=playlist.name, hexpand=True)
        self._name_entry.add_css_class("title-2")
        title_row.append(self._name_entry)
        rename = Gtk.Button(label="Rename")
        rename.connect("clicked", lambda _button: self._rename(self._name_entry.get_text()))
        title_row.append(rename)
        self._editor.append(title_row)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._play_button = Gtk.Button()
        self._play_button.add_css_class("suggested-action")
        self._play_button.connect("clicked", lambda _button: self._play_now())
        actions.append(self._play_button)
        self._default_button = Gtk.Button()
        self._default_button.connect("clicked", lambda _button: self._set_default())
        actions.append(self._default_button)
        delete = Gtk.Button(label="Delete playlist")
        delete.add_css_class("destructive-action")
        delete.connect("clicked", lambda _button: self._delete())
        actions.append(delete)
        self._editor.append(actions)

        arranger = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, wide_handle=True)
        # Playlist names need enough width to distinguish similarly prefixed
        # entries; the source cards remain usable as a single 180 px column.
        arranger.set_position(330)
        arranger.set_shrink_start_child(False)
        arranger.set_shrink_end_child(False)
        arranger.set_start_child(self._build_source_pane())
        arranger.set_end_child(self._build_playlist_pane())
        arranger.set_vexpand(True)
        self._editor.append(arranger)
        self._sync_editor()

    def _build_source_pane(self) -> Gtk.Widget:
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pane.set_margin_end(8)
        heading = Gtk.Label(label="Media/Pairings", xalign=0.0)
        heading.add_css_class("title-3")
        pane.append(heading)
        self._source_search = Gtk.SearchEntry(placeholder_text="Search pairings")
        pane.append(self._source_search)
        self._source_flow = self._new_flow()
        self._source_flow.set_filter_func(
            lambda child: self._source_visible(child, self._source_search.get_text())
        )
        self._source_flow.set_sort_func(self._compare_source_cards)
        self._source_search.connect(
            "search-changed", lambda _entry: self._source_flow.invalidate_filter()
        )
        self._source_scroll = Gtk.ScrolledWindow(
            vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER
        )
        self._source_scroll.set_child(self._source_flow)
        pane.append(self._source_scroll)
        return pane

    def _build_playlist_pane(self) -> Gtk.Widget:
        pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pane.set_margin_start(8)
        heading = Gtk.Label(label="Playlist order", xalign=0.0)
        heading.add_css_class("title-3")
        pane.append(heading)
        note = Gtk.Label(
            label=(
                "Drag pairings here. Use a row's handle or Ctrl+Up/Ctrl+Down to reorder; "
                "duplicates are allowed."
            ),
            xalign=0.0,
            wrap=True,
        )
        note.add_css_class("dim-label")
        pane.append(note)

        self._order_drop_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
        self._order_list = _ReorderList()
        self._order_list.add_css_class("boxed-list")
        self._order_list.set_sort_func(self._compare_entry_rows)
        self._order_list.set_reorder_handlers(
            self._begin_row_drag,
            self._update_row_drag,
            self._end_row_drag,
        )
        self._order_drop_area.append(self._order_list)
        # Source drags need one more slot than there are rows. This permanent
        # sibling represents "after the last row" without making a fake list
        # row or changing list membership during motion.
        self._order_end_spacer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            transition_duration=160,
        )
        self._order_end_spacer.set_child(Gtk.Box(height_request=24))
        self._order_drop_area.append(self._order_end_spacer)

        target = Gtk.DropTarget.new(str, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        # Motion decides both acceptance and the open slot from the payload.
        # Reading it only at drop time creates a circular refusal: GTK will not
        # deliver a drop after motion answered that it accepts no action.
        target.set_preload(True)
        target.connect("drop", self._drop_on_order_list)
        target.connect("motion", self._motion_on_order_list)
        target.connect("leave", self._leave_drop_target)
        self._order_drop_area.add_controller(target)
        self._order_scroll = Gtk.ScrolledWindow(
            vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER
        )
        self._order_scroll.set_child(self._order_drop_area)
        self._order_list.attach_reorder_gesture(self._order_scroll)
        pane.append(self._order_scroll)
        return pane

    def _sync_editor(self) -> None:
        session = self._session
        playlist = session.playlists.get(self._selected) if session is not None else None
        if session is None or playlist is None:
            self._show_empty()
            return
        if self._editor_id != playlist.id:
            self._show_editor()
            return
        if not self._name_entry.has_focus() and self._name_entry.get_text() != playlist.name:
            self._name_entry.set_text(playlist.name)
        self._play_button.set_label(
            "Playing now" if playlist.id == session.manual_playlist else "Play this playlist now"
        )
        self._play_button.set_sensitive(
            bool(playlist.entries) and playlist.id != session.manual_playlist
        )
        self._default_button.set_label(
            "Schedule default"
            if playlist.id == session.settings.active_playlist
            else "Use as schedule default"
        )
        self._default_button.set_sensitive(playlist.id != session.settings.active_playlist)
        self._sync_source_cards(session)
        self._sync_entry_cards(session, playlist)

    def _sync_source_cards(self, session: Session) -> None:
        incoming = {item.path: item for item in session.library.items}
        for path, card in list(self._source_cards_by_path.items()):
            replacement = incoming.get(path)
            if replacement is None or replacement != card.item:
                self._source_flow.remove(card)
                self._source_cards.pop(card, None)
                del self._source_cards_by_path[path]
        self._source_positions = {
            item.path: index for index, item in enumerate(session.library.items)
        }
        for item in session.library.items:
            if item.path in self._source_cards_by_path:
                continue
            card = _MediaCard(
                item,
                f"{SOURCE_PREFIX}{item.path}",
                self._make_add(item),
                on_drag_started=self._begin_drag,
                on_drag_finished=self._finish_drag,
            )
            self._source_cards[card] = item
            self._source_cards_by_path[item.path] = card
            self._source_flow.append(card)
            self._loader.request(item, card.show_thumbnail)
        self._source_flow.invalidate_sort()
        self._source_flow.invalidate_filter()

    def _sync_entry_cards(self, session: Session, playlist: playlists.Playlist) -> None:
        wanted = {entry.id: session.library.find(Path(entry.source)) for entry in playlist.entries}
        membership_changes = set(wanted) != set(self._entry_rows) or any(
            self._entry_items.get(identifier) != item for identifier, item in wanted.items()
        )
        if membership_changes:
            self._assert_not_dragging()
        for identifier, existing_row in list(self._entry_rows.items()):
            if identifier not in wanted or self._entry_items.get(identifier) != wanted[identifier]:
                self._remove_order_row(existing_row)
                self._entry_items.pop(identifier, None)
                del self._entry_rows[identifier]
        self._entry_positions = {entry.id: index for index, entry in enumerate(playlist.entries)}
        for entry in playlist.entries:
            if entry.id in self._entry_rows:
                continue
            item = wanted[entry.id]
            row = _PlaylistEntryRow(
                entry.id,
                item,
                Path(entry.source),
                self._make_remove(entry.id),
                self._make_reorder_key(entry.id),
            )
            if item is not None:
                self._loader.request(item, row.show_thumbnail)
            self._entry_rows[entry.id] = row
            self._entry_items[entry.id] = item
            self._append_order_row(row)
        if not self._dragging:
            self._order_list.invalidate_sort()

    def _compare_source_cards(self, first: Gtk.FlowBoxChild, second: Gtk.FlowBoxChild) -> int:
        first_card = first.get_child()
        second_card = second.get_child()
        first_item = (
            self._source_cards.get(first_card) if isinstance(first_card, _MediaCard) else None
        )
        second_item = (
            self._source_cards.get(second_card) if isinstance(second_card, _MediaCard) else None
        )
        end = len(self._source_positions)
        first_rank = self._source_positions.get(first_item.path, end) if first_item else end
        second_rank = self._source_positions.get(second_item.path, end) if second_item else end
        return first_rank - second_rank

    def _compare_entry_rows(self, first: Gtk.Widget, second: Gtk.Widget) -> int:
        end = len(self._entry_positions)
        first_id = first.identifier if isinstance(first, _PlaylistEntryRow) else ""
        second_id = second.identifier if isinstance(second, _PlaylistEntryRow) else ""
        return self._entry_positions.get(first_id, end) - self._entry_positions.get(second_id, end)

    @staticmethod
    def _new_flow() -> Gtk.FlowBox:
        flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=True,
            column_spacing=10,
            row_spacing=10,
            min_children_per_line=1,
            max_children_per_line=3,
            valign=Gtk.Align.START,
        )
        flow.set_margin_top(6)
        flow.set_margin_bottom(6)
        return flow

    def _source_visible(self, child: Gtk.FlowBoxChild, raw: str) -> bool:
        card = child.get_child()
        item = self._source_cards.get(card) if isinstance(card, _MediaCard) else None
        query = raw.strip().casefold()
        return item is None or not query or query in item.name.casefold()

    def _append_order_row(self, row: _PlaylistEntryRow) -> None:
        self._assert_not_dragging()
        self._order_list.append(row)

    def _remove_order_row(self, row: _PlaylistEntryRow) -> None:
        self._assert_not_dragging()
        self._order_list.remove(row)

    def _assert_not_dragging(self) -> None:
        assert not self._dragging, "playlist order list membership changed during a drag"

    def _row_bounds(self) -> tuple[tuple[float, float], ...]:
        list_top = float(self._order_list.get_allocation().y)
        return self._order_list.row_bounds(list_top)

    def _slot_anchor(self, slot: int) -> str | None:
        session = self._session
        playlist = session.playlists.get(self._selected) if session is not None else None
        if playlist is None or slot >= len(playlist.entries):
            return None
        return playlist.entries[max(slot, 0)].id

    def _drop_on_order_list(
        self, _target: Gtk.DropTarget, value: object, _x: float, y: float
    ) -> bool:
        slot = playlists.drop_slot(self._row_bounds(), y)
        return self._accept_drop(value, self._slot_anchor(slot))

    def _motion_on_order_list(self, target: Gtk.DropTarget, _x: float, y: float) -> Gdk.DragAction:
        slot = playlists.drop_slot(self._row_bounds(), y)
        return self._preview_drop(target.get_value(), self._slot_anchor(slot), slot=slot)

    def _preview_drop(
        self,
        value: object,
        anchor: str | None,
        *,
        after: bool = False,
        slot: int | None = None,
    ) -> Gdk.DragAction:
        session = self._session
        playlist = session.playlists.get(self._selected) if session is not None else None
        if playlist is None:
            self._clear_drop_slot()
            return Gdk.DragAction(0)
        if value is None:
            # The payload has not arrived yet. Refusing here would be refusing
            # the whole drag -- GTK never delivers a drop to a target whose
            # motion handler answered zero -- so say yes and let the next motion
            # event, or the drop itself, decide on the real value. This is only
            # reachable if preloading fails; it is a fail-open, not a fallback
            # anybody should rely on.
            return Gdk.DragAction.COPY | Gdk.DragAction.MOVE
        if not isinstance(value, str):
            self._clear_drop_slot()
            return Gdk.DragAction(0)
        if not value.startswith(SOURCE_PREFIX):
            self._clear_drop_slot()
            return Gdk.DragAction(0)
        entry_ids = tuple(entry.id for entry in playlist.entries)
        if slot is None:
            slot = entry_ids.index(anchor) + int(after) if anchor in entry_ids else len(entry_ids)
        self._show_drop_slot(slot)
        return Gdk.DragAction.COPY

    def _show_drop_slot(self, slot: int) -> None:
        slot = min(max(slot, 0), len(self._entry_rows))
        if slot == self._revealed_slot:
            return
        self._clear_drop_slot()
        row = self._order_list.get_row_at_index(slot)
        spacer = row.spacer if isinstance(row, _PlaylistEntryRow) else self._order_end_spacer
        spacer.set_reveal_child(True)
        self._revealed_slot = slot

    def _clear_drop_slot(self) -> None:
        if self._revealed_slot < 0:
            return
        row = self._order_list.get_row_at_index(self._revealed_slot)
        spacer = row.spacer if isinstance(row, _PlaylistEntryRow) else self._order_end_spacer
        spacer.set_reveal_child(False)
        self._revealed_slot = -1

    def _leave_drop_target(self, _target: Gtk.DropTarget) -> None:
        self._clear_drop_slot()

    def _begin_row_drag(self, row: _PlaylistEntryRow, start_x: float, start_y: float) -> None:
        self._begin_drag()
        children = self._order_list.rows()
        self._dragged_row = row
        self._drag_source_index = children.index(row)
        self._drag_target_slot = self._drag_source_index
        self._drag_heights = tuple(max(height, 1.0) for height in self._order_list.row_heights())
        self._drag_pointer_offset = 0.0
        source_top = sum(self._drag_heights[: self._drag_source_index])
        self._drag_grab_offset_y = start_y - source_top
        point = Graphene.Point()
        point.init(start_x, start_y)
        adjustment = self._order_scroll.get_vadjustment()
        self._drag_start_scroll = adjustment.get_value()
        translated, scroll_point = self._order_list.compute_point(self._order_scroll, point)
        self._drag_pointer_y = scroll_point.y if translated else None
        row.surface.add_css_class("wio-reorder-lifted")
        self._order_list.set_lifted(row)
        self._order_list.set_placeholder(
            self._drag_source_index,
            self._drag_source_index,
            self._drag_heights[self._drag_source_index],
        )
        if self._autoscroll_tick == 0:
            self._autoscroll_frame_time = None
            self._autoscroll_tick = self._order_scroll.add_tick_callback(
                self._scroll_while_row_dragged
            )

    def _update_row_drag(self, row: _PlaylistEntryRow, pointer_offset: float) -> None:
        if row is not self._dragged_row:
            return
        self._drag_pointer_offset = pointer_offset
        self._apply_row_drag()

    def _apply_row_drag(self) -> None:
        row = self._dragged_row
        if row is None:
            return
        scrolled = self._order_scroll.get_vadjustment().get_value() - self._drag_start_scroll
        offset = self._drag_pointer_offset + scrolled
        self._order_list.set_offset(row, offset)
        bounds: list[tuple[float, float]] = []
        top = 0.0
        for height in self._drag_heights:
            bounds.append((top, top + height))
            top += height
        source_top = bounds[self._drag_source_index][0]
        target = playlists.live_sort_slot_change(
            tuple(bounds),
            self._drag_source_index,
            source_top + self._drag_grab_offset_y + offset,
            self._drag_grab_offset_y,
            self._drag_heights[self._drag_source_index],
            self._drag_target_slot,
        )
        if target is None:
            return
        self._drag_target_slot = target
        self._order_list.set_placeholder(
            self._drag_source_index,
            target,
            self._drag_heights[self._drag_source_index],
        )
        sibling_offsets = playlists.live_sort_sibling_offsets(
            self._drag_heights, self._drag_source_index, target
        )
        self._order_list.animate_siblings(row, sibling_offsets)

    def _scroll_while_row_dragged(self, _widget: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        row = self._dragged_row
        pointer_start = self._drag_pointer_y
        if row is None or pointer_start is None:
            self._autoscroll_tick = 0
            self._autoscroll_frame_time = None
            return False
        frame_time = clock.get_frame_time()
        previous_time = self._autoscroll_frame_time
        self._autoscroll_frame_time = frame_time
        if previous_time is None:
            return True
        pointer_y = pointer_start + self._drag_pointer_offset
        viewport_height = float(self._order_scroll.get_height())
        speed = playlists.edge_scroll_speed(pointer_y, viewport_height)
        # The helper returns pixels per second, so frame-clock integration
        # keeps motion equally calm on 30, 60, and high-refresh displays.
        if speed == 0.0:
            return True
        elapsed = max(frame_time - previous_time, 0) / 1_000_000.0
        adjustment = self._order_scroll.get_vadjustment()
        maximum = max(adjustment.get_upper() - adjustment.get_page_size(), 0.0)
        value = min(max(adjustment.get_value() + speed * elapsed, 0.0), maximum)
        if value != adjustment.get_value():
            adjustment.set_value(value)
            self._apply_row_drag()
        return True

    def _end_row_drag(self, row: _PlaylistEntryRow, pointer_offset: float, cancelled: bool) -> None:
        if row is not self._dragged_row:
            return
        if self._autoscroll_tick != 0:
            self._order_scroll.remove_tick_callback(self._autoscroll_tick)
            self._autoscroll_tick = 0
        self._autoscroll_frame_time = None
        if not cancelled:
            self._drag_pointer_offset = pointer_offset
            self._apply_row_drag()
        source = self._drag_source_index
        target = source if cancelled else self._drag_target_slot
        settle = playlists.live_sort_settle_offset(self._drag_heights, source, target)
        self._dragged_row = None
        self._drag_pointer_y = None
        # Shadow and opacity fade when the gesture ends; the custom snapshot
        # keeps scale on the reference's longer 260 ms settle curve.
        row.surface.remove_css_class("wio-reorder-lifted")
        self._order_list.settle_lifted(
            row,
            settle,
            lambda _animation: self._commit_row_drag(row, source, target),
        )

    def _commit_row_drag(self, row: _PlaylistEntryRow, source: int, target: int) -> None:
        changed = target != source
        if changed:
            try:
                self._app.session.playlists.move_entry(self._selected, row.identifier, target)
            except playlists.PlaylistError as error:
                self._app.window_report(str(error))
                changed = False
        self._order_list.clear_motion()
        row.surface.remove_css_class("wio-reorder-lifted")
        self._drag_source_index = -1
        self._drag_target_slot = -1
        self._drag_heights = ()
        self._drag_pointer_offset = 0.0
        self._drag_grab_offset_y = 0.0
        self._drag_start_scroll = 0.0
        self._dragging = False
        if changed:
            self._app.playlists_changed()

    def _begin_drag(self) -> None:
        self._assert_not_dragging()
        self._dragging = True
        self._playlist_change_after_drag = False

    def _finish_drag(self) -> None:
        if not self._dragging:
            return
        self._clear_drop_slot()
        publish = self._playlist_change_after_drag
        self._playlist_change_after_drag = False
        self._dragging = False
        if publish:
            self._app.playlists_changed()

    def _accept_drop(
        self,
        value: object,
        anchor: str | None,
        *,
        after: bool = False,
    ) -> bool:
        """Apply one row drop, preserving entry ids and allowing duplicates."""
        self._clear_drop_slot()
        if not isinstance(value, str) or not self._selected:
            return False
        session = self._app.session
        playlist = session.playlists.get(self._selected)
        if playlist is None:
            return False
        try:
            if value.startswith(SOURCE_PREFIX):
                source = Path(value.removeprefix(SOURCE_PREFIX))
                if session.library.find(source) is None:
                    return False
                updated = session.playlists.add(self._selected, source)
                moving = updated.entries[-1].id
            else:
                return False
            current = session.playlists.find(self._selected)
            position = playlists.drop_position(
                tuple(entry.id for entry in current.entries), moving, anchor, after=after
            )
            session.playlists.move_entry(self._selected, moving, position)
        except playlists.PlaylistError as error:
            self._app.window_report(str(error))
            return False
        if self._dragging:
            # The store write is safe, but its normal synchronous refresh would
            # sort, append, or remove list rows before GTK has ended the drag.
            # Publish only after drag-end has lowered the membership guard.
            self._playlist_change_after_drag = True
        else:
            self._app.playlists_changed()
        return True

    def _create(self, *_arguments: object) -> None:
        session = self._session
        if session is None:
            return
        try:
            made = session.playlists.create(self._new_name.get_text())
        except playlists.PlaylistError as error:
            self._app.window_report(str(error))
            return
        self._selected = made.id
        self._new_name.set_text("")
        self._app.playlists_changed()

    def _rename(self, name: str) -> None:
        session = self._session
        if session is None:
            return
        try:
            session.playlists.rename(self._selected, name)
        except playlists.PlaylistError as error:
            self._app.window_report(str(error))
            return
        self._app.playlists_changed()

    def _delete(self) -> None:
        session = self._session
        playlist = session.playlists.get(self._selected) if session is not None else None
        if session is None or playlist is None:
            return
        session.playlists.delete(playlist.id)
        session.schedules.forget_playlist(playlist.id)
        session.displays.forget_playlist(playlist.id)
        if session.settings.active_playlist == playlist.id:
            self._app.update_settings(active_playlist="")
        if session.manual_playlist == playlist.id:
            self._app.resume_schedule()
        self._selected = ""
        self._app.playlists_changed()

    def _set_default(self) -> None:
        if self._selected:
            self._app.update_settings(active_playlist=self._selected)
            self._app.playlists_changed()

    def _play_now(self) -> None:
        if not self._selected:
            return
        response = self._app.activate_playlist(self._selected)
        if not response.ok:
            self._app.window_report(response.message)

    def _make_add(self, item: MediaItem) -> Any:
        def add(_button: Gtk.Button) -> None:
            try:
                self._app.session.playlists.add(self._selected, item.path)
            except playlists.PlaylistError as error:
                self._app.window_report(str(error))
                return
            self._app.playlists_changed()

        return add

    def _make_remove(self, entry_id: str) -> Any:
        def remove(_button: Gtk.Button) -> None:
            try:
                self._app.session.playlists.remove_entry(self._selected, entry_id)
            except playlists.PlaylistError as error:
                self._app.window_report(str(error))
                return
            self._app.playlists_changed()

        return remove

    def _make_reorder_key(self, entry_id: str) -> Any:
        def reorder(
            _controller: Gtk.EventControllerKey,
            keyval: int,
            _code: int,
            state: Gdk.ModifierType,
        ) -> bool:
            if not state & Gdk.ModifierType.CONTROL_MASK:
                return False
            if keyval == Gdk.KEY_Up:
                step = -1
            elif keyval == Gdk.KEY_Down:
                step = 1
            else:
                return False

            playlist = self._app.session.playlists.get(self._selected)
            if playlist is None:
                return True
            entry_ids = tuple(entry.id for entry in playlist.entries)
            if entry_id not in entry_ids:
                return True
            current = entry_ids.index(entry_id)
            position = min(max(current + step, 0), len(entry_ids) - 1)
            if position != current:
                first = self._order_list.capture_positions()
                try:
                    self._app.session.playlists.move_entry(self._selected, entry_id, position)
                except playlists.PlaylistError as error:
                    self._app.window_report(str(error))
                    return True
                self._app.playlists_changed()
                self._order_list.animate_from(first)

            # The keyed diff keeps this exact row alive. Reclaiming focus after
            # the synchronous FLIP sort makes repeated Ctrl+Arrow presses reliable.
            row = self._entry_rows.get(entry_id)
            if row is not None:
                row.grab_focus()
            return True

        return reorder

    def _make_move(self, entry_id: str, position: int) -> Any:
        def move(_button: Gtk.Button) -> None:
            try:
                self._app.session.playlists.move_entry(self._selected, entry_id, position)
            except playlists.PlaylistError as error:
                self._app.window_report(str(error))
                return
            self._app.playlists_changed()

        return move
