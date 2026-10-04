"""A boxed list whose rows you reorder by their handle, like objects on rollers.

Press a row's handle and the row lifts and follows the pointer; as its middle
passes a neighbour's, that neighbour glides into the slot it left, so rows
trade places continuously. Releasing settles the row into its slot; Escape
glides everything back. The list never touches the model: once the dropped
row has settled it emits ``reordered(row, from, to)`` a single time, and
``settled`` when nothing moves any more. Keyboard reordering is the page's
(it asks the model, then `set_order` rolls the rows to what was saved).

The decisions this follows (``DECISIONS.md``):

* *A live sortable list, not drag-and-drop.* Rows move by arithmetic on
  offsets (FLIP): a slot changes at once, then each displaced row glides
  from where it was drawn to where it now belongs. 230 ms on
  ``cubic-bezier(.20,.75,.18,1)`` for the glide, 260 ms on
  ``cubic-bezier(.18,.82,.22,1)`` for the dropped row, driven by a linear
  `Adw.TimedAnimation` through `cubic_bezier`.
* *Handle-driven.* Only a row's handle (a passive picture, not a button)
  starts a reorder; pressing or dragging a row's body never does.
* *Measure a gesture where nothing it causes can move.* The drag gesture is
  on the `Gtk.ScrolledWindow` around the list (`attach`), never on the list
  or a row: dragging moves rows and edge auto-scroll moves the list, and a
  gesture measured in either would feed its own result back into the next
  reading. The press is hit-tested to find which handle was grabbed, and
  the pointer is turned into list coordinates only when it is used.
* *Auto-scroll* within 92 px of the scrolled window's edges, up to 300 px a
  second at the very edge, eased from zero at the band's inner boundary and
  integrated against the frame clock, so every refresh rate travels alike.
* *No widget joins or leaves the list during a drag.* `remove_row` refuses
  then; pages wait for ``settled``. Sorting (`invalidate_sort`) moves the
  real children without unparenting them, so focus and the pointer grab
  survive and ``:first-child``/``:last-child`` follow at once.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Final

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")

from gi.repository import Adw, Gdk, GObject, Graphene, Gsk, Gtk

from wall_in_one.ui.next.widgets import add_css

#: The glide of a row that makes room, and of one rolled by `set_order`.
GLIDE_MS: Final = 230
GLIDE_CURVE: Final = (0.20, 0.75, 0.18, 1.0)
#: The dropped row settling into its slot.
SETTLE_MS: Final = 260
SETTLE_CURVE: Final = (0.18, 0.82, 0.22, 1.0)
#: The auto-scroll band at each edge of the scrolled window, and its top speed.
EDGE_BAND: Final = 92.0
EDGE_SPEED: Final = 300.0

_CSS: Final = """
list.reorder-list > row {
  transition: background-color 200ms ease-out, border-radius 200ms ease-out,
    box-shadow 160ms ease-out, transform 160ms ease-out;
}
/* Lifted: a floating card above the others, solid even in glass styles. */
list.reorder-list > row.reorder-lifted,
list.reorder-list > row.reorder-moving {
  background-color: var(--popover-bg-color);
  border-radius: 12px;
  border-bottom-color: transparent;
}
list.reorder-list > row.reorder-lifted {
  box-shadow: 0 0 0 1px alpha(black, 0.07), 0 2px 4px alpha(black, 0.14),
    0 10px 26px alpha(black, 0.28);
  transform: scale(1.018);
}
list.reorder-list > row.reorder-moving {
  box-shadow: 0 0 0 1px alpha(black, 0.06), 0 4px 14px alpha(black, 0.22);
}
.reorder-handle { min-width: 24px; min-height: 32px; opacity: 0.40; }
list.reorder-list > row:hover .reorder-handle,
list.reorder-list > row:focus-within .reorder-handle,
list.reorder-list > row.reorder-lifted .reorder-handle { opacity: 0.95; }
"""
_styled = False


def cubic_bezier(progress: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """``progress`` (0..1) through a CSS ``cubic-bezier(x1, y1, x2, y2)`` timing curve.

    Both coordinates are parametric, so the curve's parameter for x is found
    first (Newton, with bisection when the slope is flat) and y read there.
    """
    if progress <= 0.0:
        return 0.0
    if progress >= 1.0:
        return 1.0

    def at(t: float, first: float, second: float) -> float:
        inverse = 1.0 - t
        return 3 * inverse * inverse * t * first + 3 * inverse * t * t * second + t * t * t

    t = progress
    for _attempt in range(8):
        error = at(t, x1, x2) - progress
        if abs(error) < 1e-7:
            return at(t, y1, y2)
        inverse = 1.0 - t
        slope = 3 * inverse * inverse * x1 + 6 * inverse * t * (x2 - x1) + 3 * t * t * (1 - x2)
        if abs(slope) < 1e-7:
            break
        t = min(1.0, max(0.0, t - error / slope))
    low, high = 0.0, 1.0
    for _attempt in range(30):
        t = (low + high) / 2
        if math.isclose(at(t, x1, x2), progress, abs_tol=1e-7):
            break
        if at(t, x1, x2) < progress:
            low = t
        else:
            high = t
    return at(t, y1, y2)


def edge_speed(pointer_y: float, height: float) -> float:
    """Auto-scroll pixels per second for a pointer ``pointer_y`` down a viewport."""
    if pointer_y < EDGE_BAND:
        depth = min(1.0, (EDGE_BAND - pointer_y) / EDGE_BAND)
        return -EDGE_SPEED * depth * depth
    if pointer_y > height - EDGE_BAND:
        depth = min(1.0, (pointer_y - (height - EDGE_BAND)) / EDGE_BAND)
        return EDGE_SPEED * depth * depth
    return 0.0


def animations_enabled(widget: Gtk.Widget) -> bool:
    settings = widget.get_settings() or Gtk.Settings.get_default()
    return bool(settings is None or settings.props.gtk_enable_animations)


@dataclass
class _Slot:
    """Where a row is drawn (``y``), where it belongs (``target``), and its roll."""

    y: float | None = None
    target: float | None = None
    height: int = 0
    start: float = 0.0
    end: float = 0.0
    curve: tuple[float, float, float, float] = GLIDE_CURVE
    animation: Adw.TimedAnimation | None = None


@dataclass
class _Drag:
    row: Gtk.Widget
    #: The order when the drag began, for Escape, and the row's index then.
    order: list[Gtk.Widget]
    origin: int
    #: Pointer y minus the row's top, at the press (the grab point is kept).
    grab: float
    #: Programmatic drags: the pointer's list y at the start and its offset.
    start_y: float = 0.0
    offset: float = 0.0
    #: Pointer drags: where the pointer is, in the scrolled window's coordinates.
    pointer: tuple[float, float] | None = None
    handle: Gtk.Widget | None = None
    escape: Gtk.EventControllerKey | None = None


class ReorderList(Gtk.ListBox):
    """A boxed list with handle-driven live reordering (see the module notes)."""

    __gtype_name__ = "WioNextReorderList"
    __gsignals__: ClassVar[dict[str, Any]] = {
        # Rows traded places (live, during a drag).
        "order-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # A drag ended and the row settled: (row, index at the press, index now).
        "reordered": (GObject.SignalFlags.RUN_FIRST, None, (object, int, int)),
        # Nothing is dragging or rolling any more.
        "settled": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__(selection_mode=Gtk.SelectionMode.NONE)
        global _styled
        if not _styled:
            add_css(_CSS)
            _styled = True
        self.add_css_class("boxed-list")
        self.add_css_class("reorder-list")
        self._rows: list[Gtk.Widget] = []
        self._slots: dict[Gtk.Widget, _Slot] = {}
        self._handles: dict[Gtk.Widget, Gtk.Widget] = {}  # handle -> row
        self._order: dict[Gtk.Widget, int] = {}
        self._drag: _Drag | None = None
        self._floating: set[Gtk.Widget] = set()  # drawn above the rest while they move
        self._dropped: tuple[Gtk.Widget, int, int] | None = None  # waiting to settle
        self._scroller: Gtk.ScrolledWindow | None = None
        self._gesture: Gtk.GestureDrag | None = None
        self._tick = 0
        self._tick_time: int | None = None
        self.set_sort_func(self._compare)

    def _compare(self, first: Gtk.ListBoxRow, second: Gtk.ListBoxRow) -> int:
        return self._order.get(first, 0) - self._order.get(second, 0)

    # -- the gesture ---------------------------------------------------------------
    def attach(self, scroller: Gtk.ScrolledWindow) -> Gtk.GestureDrag:
        """Measure reorder drags on ``scroller``, which holds this list and never moves.

        Capture phase, so a handle press is claimed before the scrolled
        window's own kinetic drag or any row controller sees it; every other
        press is denied at once and goes where it always went.
        """
        if self._gesture is not None and self._scroller is not None:
            self._scroller.remove_controller(self._gesture)
        gesture = Gtk.GestureDrag(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        gesture.connect("drag-begin", self._on_drag_begin)
        gesture.connect("drag-update", self._on_drag_update)
        gesture.connect("drag-end", self._on_drag_end)
        gesture.connect("cancel", self._on_drag_cancel)
        scroller.add_controller(gesture)
        self._scroller, self._gesture = scroller, gesture
        return gesture

    @property
    def gesture(self) -> Gtk.GestureDrag | None:
        """The reorder gesture (on the scrolled window `attach` was given)."""
        return self._gesture

    # -- rows ------------------------------------------------------------------------
    def append(self, row: Gtk.Widget, handle: Gtk.Widget | None = None) -> None:
        """Add a row at the end. ``handle`` (inside the row) is what grabs it."""
        if self._drag is not None:
            raise RuntimeError("rows cannot join the list during a drag")
        self._order[row] = len(self._rows)
        self._rows.append(row)
        self._slots[row] = _Slot()
        super().append(row)
        if handle is not None:
            handle.add_css_class("reorder-handle")
            handle.set_cursor_from_name("grab")
            self._handles[handle] = row

    def remove_row(self, row: Gtk.Widget) -> None:
        """Take one row out (never during a drag)."""
        if self._drag is not None:
            raise RuntimeError("rows cannot leave the list during a drag")
        if row not in self._slots:
            return
        slot = self._slots.pop(row)
        if slot.animation is not None:
            slot.animation.reset()
        self._rows.remove(row)
        self._order.pop(row, None)
        self._floating.discard(row)
        for handle in [handle for handle, owner in self._handles.items() if owner is row]:
            del self._handles[handle]
        if self._dropped is not None and self._dropped[0] is row:
            self._dropped = None
        super().remove(row)
        self._order = {each: index for index, each in enumerate(self._rows)}

    def remove_all(self) -> None:
        """Drop every row (ends a drag without committing it)."""
        self._stop_drag(restore=False)
        for slot in self._slots.values():
            if slot.animation is not None:
                slot.animation.reset()
        self._rows.clear()
        self._slots.clear()
        self._handles.clear()
        self._order.clear()
        self._floating.clear()
        self._dropped = None
        super().remove_all()

    def get_rows(self) -> list[Gtk.Widget]:
        """The rows in the order they are shown now."""
        return list(self._rows)

    def set_order(self, rows: Sequence[Gtk.Widget]) -> None:
        """Show the same rows in ``rows``' order; the ones that move glide there."""
        if self._drag is not None or list(rows) == self._rows:
            return
        if set(rows) != set(self._rows) or len(rows) != len(self._rows):
            raise ValueError("set_order takes exactly the rows the list has")
        self._rows[:] = rows
        self._sync_children()
        self._retarget()

    def row_heights(self) -> list[int]:
        """Each row's slot height in the current order (its whole allocation)."""
        return [self._slots[row].height for row in self._rows]

    # -- state for pages ---------------------------------------------------------------
    @property
    def dragging(self) -> bool:
        return self._drag is not None

    @property
    def busy(self) -> bool:
        """Dragging, rolling, or a dropped row not settled yet."""
        return self._drag is not None or self._dropped is not None or self._rolling()

    # -- dragging ----------------------------------------------------------------------
    def begin_drag(
        self,
        row: Gtk.Widget,
        grab: float | None = None,
        *,
        pointer: tuple[float, float] | None = None,
        handle: Gtk.Widget | None = None,
    ) -> None:
        """Lift ``row``; ``grab`` is where it is held, from its top (default: its middle)."""
        if self._drag is not None or row not in self._rows:
            return
        slot = self._slots[row]
        top = slot.y if slot.y is not None else self._slot_tops()[row]
        grab = slot.height / 2 if grab is None else grab
        self._drag = _Drag(
            row=row,
            order=list(self._rows),
            origin=self._rows.index(row),
            grab=grab,
            start_y=top + grab,
            pointer=pointer,
            handle=handle,
        )
        if slot.animation is not None:
            slot.animation.pause()
        slot.y = top
        self._floating.add(row)
        row.remove_css_class("reorder-moving")
        row.add_css_class("reorder-lifted")
        row.set_cursor_from_name("grabbing")
        if handle is not None:
            handle.set_cursor_from_name("grabbing")
        if pointer is not None:
            escape = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
            escape.connect("key-pressed", self._on_escape)
            root = self.get_root()
            if isinstance(root, Gtk.Window):
                root.add_controller(escape)
                self._drag.escape = escape
            self._start_autoscroll()
        self.queue_allocate()

    def update_drag(self, offset: float) -> None:
        """A programmatic drag: move the lifted row ``offset`` px from where it began."""
        if self._drag is None:
            return
        self._drag.offset = offset
        self._place_drag()

    def end_drag(self) -> None:
        """Drop the lifted row into its slot; ``reordered`` follows once it settles."""
        drag = self._drag
        if drag is None:
            return
        self._stop_drag(restore=False)
        target = self._rows.index(drag.row)
        if target != drag.origin:
            self._dropped = (drag.row, drag.origin, target)
        self._roll(drag.row, self._slot_tops()[drag.row], SETTLE_CURVE, SETTLE_MS)
        self._maybe_settled()

    def cancel_drag(self) -> None:
        """Escape: glide everything back to where it was before the drag."""
        drag = self._drag
        if drag is None:
            return
        self._stop_drag(restore=True)
        self._roll(drag.row, self._slot_tops()[drag.row], SETTLE_CURVE, SETTLE_MS)
        self._maybe_settled()

    def _stop_drag(self, restore: bool) -> None:
        drag, self._drag = self._drag, None
        self._stop_autoscroll()
        if drag is None:
            return
        drag.row.remove_css_class("reorder-lifted")
        drag.row.set_cursor(None)
        if drag.handle is not None:
            drag.handle.set_cursor_from_name("grab")
        if drag.escape is not None:
            owner = drag.escape.get_widget()
            if owner is not None:
                owner.remove_controller(drag.escape)
        if self._gesture is not None and self._gesture.is_active():
            self._gesture.reset()
        if restore and drag.order != self._rows and set(drag.order) == set(self._rows):
            self._rows[:] = drag.order
            self._sync_children()
            self._retarget(exclude=drag.row)
            self.emit("order-changed")

    def _pointer_y(self, drag: _Drag) -> float:
        """The pointer's y in list coordinates, from where it is in the scrolled window."""
        if drag.pointer is not None and self._scroller is not None:
            x, y = drag.pointer
            ok, point = self._scroller.compute_point(self, Graphene.Point().init(x, y))
            if ok:
                return float(point.y)
        return drag.start_y + drag.offset

    def _place_drag(self) -> None:
        drag = self._drag
        if drag is None:
            return
        row = drag.row
        slot = self._slots[row]
        total = sum(self._slots[each].height for each in self._rows)
        slot.y = max(0.0, min(total - slot.height, self._pointer_y(drag) - drag.grab))
        centre = slot.y + slot.height / 2
        index, moved = self._rows.index(row), False
        tops = self._slot_tops()
        # Trade places with a neighbour once the lifted row's middle passes theirs.
        while index > 0:
            above = self._rows[index - 1]
            if centre < tops[above] + self._slots[above].height / 2:
                self._rows[index - 1], self._rows[index] = row, above
                index, moved = index - 1, True
                tops = self._slot_tops()
            else:
                break
        while index < len(self._rows) - 1:
            below = self._rows[index + 1]
            if centre > tops[below] + self._slots[below].height / 2:
                self._rows[index + 1], self._rows[index] = row, below
                index, moved = index + 1, True
                tops = self._slot_tops()
            else:
                break
        if moved:
            self._sync_children()
            self._retarget(exclude=row)
            self.emit("order-changed")
        self.queue_allocate()

    # -- rolling -----------------------------------------------------------------------
    def _slot_tops(self) -> dict[Gtk.Widget, float]:
        tops, y = {}, 0.0
        for row in self._rows:
            tops[row] = y
            y += self._slots[row].height
        return tops

    def _retarget(self, exclude: Gtk.Widget | None = None) -> None:
        for row, top in self._slot_tops().items():
            if row is exclude:
                continue
            slot = self._slots[row]
            if slot.target is None or slot.y is None:
                slot.target = top  # not allocated yet: it simply appears in place
                continue
            if abs(slot.target - top) > 0.5 or abs(slot.y - top) > 0.5:
                self._roll(row, top)

    def _roll(
        self,
        row: Gtk.Widget,
        top: float,
        curve: tuple[float, float, float, float] = GLIDE_CURVE,
        duration: int = GLIDE_MS,
    ) -> None:
        """Glide ``row`` from where it is drawn now to ``top``."""
        slot = self._slots[row]
        slot.target = top
        if slot.y is None or not animations_enabled(self) or abs(slot.y - top) <= 0.5:
            slot.y = top
            self._rolled(row)
            self.queue_allocate()
            return
        if slot.animation is None:
            target = Adw.CallbackAnimationTarget.new(lambda value, r=row: self._on_roll(r, value))
            slot.animation = Adw.TimedAnimation(
                widget=self, value_from=0.0, value_to=1.0, duration=duration, target=target
            )
            slot.animation.set_easing(Adw.Easing.LINEAR)
            slot.animation.connect("done", lambda _animation, r=row: self._rolled(r))
        slot.animation.set_duration(duration)
        slot.curve = curve
        self._floating.add(row)
        if self._drag is None or self._drag.row is not row:
            row.add_css_class("reorder-moving")
        slot.start, slot.end = slot.y, top
        slot.animation.reset()
        slot.animation.play()

    def _on_roll(self, row: Gtk.Widget, value: float) -> None:
        slot = self._slots.get(row)
        if slot is None:
            return
        eased = cubic_bezier(value, *slot.curve)
        slot.y = slot.start + (slot.end - slot.start) * eased
        self.queue_allocate()

    def _rolled(self, row: Gtk.Widget) -> None:
        if self._drag is None or self._drag.row is not row:
            self._floating.discard(row)
            row.remove_css_class("reorder-moving")
        self._maybe_settled()

    def _rolling(self) -> bool:
        return any(
            slot.animation is not None and slot.animation.get_state() == Adw.AnimationState.PLAYING
            for slot in self._slots.values()
        )

    def _maybe_settled(self) -> None:
        if self._drag is not None or self._rolling():
            return
        dropped, self._dropped = self._dropped, None
        if dropped is not None and dropped[0] in self._slots:
            row, origin, target = dropped
            self.emit("reordered", row, origin, target)
        if not self.busy:
            self.emit("settled")

    def _sync_children(self) -> None:
        # Re-sorting moves the real children (no unparenting: focus and the
        # pointer grab survive), so :first-child / :last-child follow at once.
        self._order = {row: index for index, row in enumerate(self._rows)}
        self.invalidate_sort()

    # -- auto-scroll -------------------------------------------------------------------
    def _start_autoscroll(self) -> None:
        if self._scroller is None or self._tick:
            return
        self._tick_time = None
        self._tick = self._scroller.add_tick_callback(self._on_tick)

    def _stop_autoscroll(self) -> None:
        if self._tick and self._scroller is not None:
            self._scroller.remove_tick_callback(self._tick)
        self._tick = 0
        self._tick_time = None

    def _on_tick(self, scroller: Gtk.Widget, clock: Gdk.FrameClock) -> bool:
        drag = self._drag
        if drag is None or drag.pointer is None or not isinstance(scroller, Gtk.ScrolledWindow):
            self._tick = 0
            return False
        now = clock.get_frame_time()
        last, self._tick_time = self._tick_time, now
        if last is None:
            return True
        speed = edge_speed(drag.pointer[1], scroller.get_height())
        if speed:
            adjustment = scroller.get_vadjustment()
            top = adjustment.get_upper() - adjustment.get_page_size()
            value = adjustment.get_value() + speed * (now - last) / 1_000_000
            value = max(adjustment.get_lower(), min(top, value))
            if value != adjustment.get_value():
                adjustment.set_value(value)
                self._place_drag()
        return True

    # -- input -------------------------------------------------------------------------
    def _handle_at(self, x: float, y: float) -> tuple[Gtk.Widget | None, Gtk.Widget | None]:
        """The row whose handle is at list point (``x``, ``y``), and that handle."""
        widget = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        handle = None
        while widget is not None and widget is not self:
            if widget in self._handles:
                handle = widget
            if widget in self._slots:
                return (widget, handle) if handle is not None else (None, None)
            widget = widget.get_parent()
        return None, None

    def _on_drag_begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        scroller = self._scroller
        if scroller is None or self._drag is not None or not self.get_sensitive():
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        ok, point = scroller.compute_point(self, Graphene.Point().init(x, y))
        row, handle = self._handle_at(point.x, point.y) if ok else (None, None)
        if row is None or not row.get_sensitive():
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        # Claimed at once: the kinetic scroll would otherwise compete for it.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        top = self._slots[row].y or 0.0
        self.begin_drag(row, point.y - top, pointer=(x, y), handle=handle)

    def _on_drag_update(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        drag = self._drag
        if drag is None or drag.pointer is None:
            return
        ok, x, y = gesture.get_start_point()
        if ok:
            drag.pointer = (x + dx, y + dy)
        self._place_drag()

    def _on_drag_end(self, _gesture: Gtk.GestureDrag, _dx: float, _dy: float) -> None:
        if self._drag is not None and self._drag.pointer is not None:
            self.end_drag()

    def _on_drag_cancel(self, _gesture: Gtk.Gesture, _sequence: Gdk.EventSequence | None) -> None:
        if self._drag is not None and self._drag.pointer is not None:
            self.cancel_drag()

    def _on_escape(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _code: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval == Gdk.KEY_Escape and self._drag is not None:
            self.cancel_drag()
            return True
        return False

    # -- layout ------------------------------------------------------------------------
    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        # The list box lays rows out in their slots (and remembers them for
        # clicks and keyboard navigation); then rows go where they are drawn.
        Gtk.ListBox.do_size_allocate(self, width, height, baseline)
        if not self._rows:
            return
        top = 0.0
        dragged = self._drag.row if self._drag is not None else None
        for row in self._rows:
            slot = self._slots[row]
            # Measured, not get_height(): the allocation includes the separator.
            slot.height = row.measure(Gtk.Orientation.VERTICAL, width)[0]
            rolling = (
                slot.animation is not None
                and slot.animation.get_state() == Adw.AnimationState.PLAYING
            )
            if row is not dragged:
                if rolling:
                    slot.end = top  # a resize mid-roll keeps rolling, to the new place
                else:
                    slot.y = top
                slot.target = top
            top += slot.height
        if dragged is not None:  # the lifted row stays inside the list
            slot = self._slots[dragged]
            slot.y = max(0.0, min(top - slot.height, slot.y or 0.0))
        for row in self._rows:
            slot = self._slots[row]
            # Rows resting in their slot were placed by the list box already.
            if row is not dragged and (slot.y is None or abs(slot.y - (slot.target or 0.0)) < 0.5):
                continue
            transform = Gsk.Transform.new().translate(
                Graphene.Point().init(0, round(slot.y or 0.0))
            )
            row.allocate(width, slot.height, -1, transform)

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        # Rows that are lifted or rolling over others are drawn last, on top.
        above = [row for row in self._rows if row in self._floating]
        if self._drag is not None and self._drag.row in above:
            above.remove(self._drag.row)
            above.append(self._drag.row)
        child = self.get_first_child()
        while child is not None:
            if child not in self._floating:
                self.snapshot_child(child, snapshot)
            child = child.get_next_sibling()
        for row in above:
            self.snapshot_child(row, snapshot)
