"""A boxed list whose rows you reorder by hand, like objects on rollers.

Press a row's handle and it lifts and follows the pointer like a slider
thumb; drag a row by its body (mouse, mostly vertical) and it does the same.
As the lifted row passes a neighbor's middle, that neighbor glides into the
slot it left, so rows trade places continuously while you drag. Releasing
settles the row into its slot; Escape glides everything back. Keyboard moves
(:meth:`ReorderList.move`) animate the same way.

The list never touches the model itself. It emits ``order-changed`` live while
rows trade places (for numbering) and ``reordered(row)`` once, after
everything has settled, so the page commits the new order a single time.

It is a Gtk.ListBox (``boxed-list``), so focus, keyboard navigation,
activation, the placeholder and libadwaita's card, separator and corner
styles are the stock ones. Its sort function follows the drag: rows are
reordered as real children while they move, which keeps ``:first-child`` and
``:last-child`` (rounded corners, no last separator) right mid-drag.
Allocation chains up to the list box, which keeps each row's logical slot for
clicks and keyboard navigation, then places rows where they are being drawn.
"""

from __future__ import annotations

from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, GLib, GObject, Graphene, Gsk, Gtk

from . import ui

#: How long a row takes to roll into a new slot, in milliseconds.
DURATION = 220
#: Distance from the scroller's edge where dragging starts to scroll, in pixels.
EDGE = 56
#: A body drag starts after this much mostly-vertical movement, in pixels.
BODY_THRESHOLD = 6

CSS = """
list.reorder-list > row {
  transition: outline-color 200ms ease-out, outline-width 200ms ease-out, outline-offset 200ms ease-out,
    background-color 200ms ease-out, border-radius 200ms ease-out,
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
  box-shadow: 0 0 0 1px alpha(black, 0.07), 0 2px 4px alpha(black, 0.14), 0 10px 26px alpha(black, 0.28);
  transform: scale(1.012);
}
list.reorder-list > row.reorder-moving {
  box-shadow: 0 0 0 1px alpha(black, 0.06), 0 4px 14px alpha(black, 0.22);
}
.reorder-handle { min-width: 24px; min-height: 32px; opacity: 0.40; }
list.reorder-list > row:hover .reorder-handle,
list.reorder-list > row:focus-within .reorder-handle,
list.reorder-list > row.reorder-lifted .reorder-handle { opacity: 0.95; }
"""

_css_loaded = False


def _ensure_css() -> None:
    global _css_loaded
    if not _css_loaded:
        ui.add_css(CSS)
        _css_loaded = True


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
    animation: Adw.TimedAnimation | None = None


@dataclass
class _Drag:
    row: Gtk.Widget
    order: list[Gtk.Widget]  # the order when the drag began, for Escape
    start_y: float  # pointer y in list coordinates at the start
    grab: float  # pointer y minus the row's top, at the start
    offset: float = 0.0  # latest vertical offset from the gesture
    scroll_at_event: float = 0.0  # scroller position at that offset
    gesture: Gtk.GestureDrag | None = None  # None for programmatic drags
    handle: Gtk.Widget | None = None
    escape: Gtk.EventControllerKey | None = None


class ReorderList(Gtk.ListBox):
    """A boxed list with direct-manipulation reordering.

    ``row-activated``, the placeholder and keyboard navigation are Gtk.ListBox's.
    """

    __gtype_name__ = "WioReorderList"
    __gsignals__ = {  # noqa: RUF012
        # Rows traded places (live, during a drag or a keyboard move).
        "order-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # A move finished and everything settled: commit get_rows() once.
        "reordered": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # Nothing is dragging, rolling or waiting to be committed any more.
        "settled": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, *, body_drag: bool = True) -> None:
        super().__init__(selection_mode=Gtk.SelectionMode.NONE)
        _ensure_css()
        self.add_css_class("boxed-list")
        self.add_css_class("reorder-list")
        self.body_drag = body_drag
        self._rows: list[Gtk.Widget] = []
        self._slots: dict[Gtk.Widget, _Slot] = {}
        self._handles: dict[Gtk.Widget, Gtk.Widget] = {}  # handle -> row
        self._order: dict[Gtk.Widget, int] = {}
        self._drag: _Drag | None = None
        self._candidate: tuple[Gtk.Widget, float] | None = None  # a body press that may become a drag
        self._floating: set[Gtk.Widget] = set()  # drawn above the rest while they move
        self._pending: Gtk.Widget | None = None  # moved row waiting to be committed
        self._commit_source = 0
        self._scroller: Gtk.ScrolledWindow | None = None
        self._scroll_speed = 0.0
        self._scroll_source = 0
        self.set_sort_func(lambda a, b: self._order.get(a, 0) - self._order.get(b, 0))

        # Capture phase: decide before the rows' own controllers (drag-and-drop,
        # the list's click) whether a press is a reorder.
        gesture = Gtk.GestureDrag(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        gesture.connect("drag-begin", self._on_drag_begin)
        gesture.connect("drag-update", self._on_drag_update)
        gesture.connect("drag-end", self._on_drag_end)
        gesture.connect("cancel", self._on_drag_cancel)
        self.add_controller(gesture)

    # -- rows ------------------------------------------------------------------
    def append(self, row: Gtk.Widget, handle: Gtk.Widget | None = None) -> None:
        """Add a row at the end. ``handle`` (inside the row) grabs it like a slider."""
        self._order[row] = len(self._rows)
        self._rows.append(row)
        self._slots[row] = _Slot()
        super().append(row)
        if handle is not None:
            handle.add_css_class("reorder-handle")
            handle.set_cursor_from_name("grab")
            self._handles[handle] = row

    def remove_all(self) -> None:
        """Drop every row without committing anything still pending."""
        self._stop_drag(restore=False)
        self._stop_autoscroll()
        if self._commit_source:
            GLib.source_remove(self._commit_source)
            self._commit_source = 0
        self._pending = None
        for slot in self._slots.values():
            if slot.animation is not None:
                slot.animation.reset()
        self._rows.clear()
        self._slots.clear()
        self._handles.clear()
        self._order.clear()
        self._floating.clear()
        super().remove_all()

    def get_rows(self) -> list[Gtk.Widget]:
        """The rows in their current order (including moves not yet committed)."""
        return list(self._rows)

    def row_heights(self) -> list[int]:
        """Each row's slot height in the current order (its full allocation, unlike get_height())."""
        return [self._slots[row].height for row in self._rows]

    # -- state for pages ----------------------------------------------------------
    @property
    def dragging(self) -> bool:
        return self._drag is not None

    @property
    def dragging_by_hand(self) -> bool:
        """A real pointer drag (as opposed to a demo or test drag)."""
        return self._drag is not None and self._drag.gesture is not None

    @property
    def busy(self) -> bool:
        return self._drag is not None or self._pending is not None or self._rolling()

    def flush(self) -> bool:
        """Finish every roll now and commit a pending move. True if it committed."""
        if self._drag is not None and self._drag.gesture is None:
            self._stop_drag(restore=False)  # a frozen demo drag: let it go where it is
        for slot in list(self._slots.values()):
            if slot.animation is not None and slot.animation.get_state() == Adw.AnimationState.PLAYING:
                slot.animation.skip()
        if self._commit_source:
            GLib.source_remove(self._commit_source)
            self._commit_source = 0
        if self._pending is not None and self._drag is None:
            row, self._pending = self._pending, None
            self.emit("reordered", row)
            return True
        return False

    # -- moving ------------------------------------------------------------------------
    def move(self, row: Gtk.Widget, index: int) -> bool:
        """Roll ``row`` to ``index`` (keyboard and menu moves). False if nothing moved."""
        if self._drag is not None or row not in self._rows:
            return False
        index = max(0, min(len(self._rows) - 1, index))
        current = self._rows.index(row)
        if index == current:
            return False
        self._rows.insert(index, self._rows.pop(current))
        self._sync_children()
        self._floating.add(row)
        if animations_enabled(self):
            row.add_css_class("reorder-moving")
        self._retarget()
        self._pending = row
        self.emit("order-changed")
        self._maybe_commit()
        return True

    def begin_drag(self, row: Gtk.Widget, grab: float | None = None, gesture=None, handle=None) -> None:
        """Lift ``row``; ``grab`` is where the pointer holds it, from its top."""
        if self._drag is not None or row not in self._rows:
            return
        if self._commit_source:  # a drag right after a move: commit both together later
            GLib.source_remove(self._commit_source)
            self._commit_source = 0
        slot = self._slots[row]
        top = slot.y if slot.y is not None else self._slot_tops()[row]
        grab = slot.height / 2 if grab is None else grab
        self._find_scroller()
        self._drag = _Drag(
            row=row,
            order=list(self._rows),
            start_y=top + grab,
            grab=grab,
            scroll_at_event=self._scroll_value(),
            gesture=gesture,
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
        if gesture is not None:
            self._drag.escape = escape = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
            escape.connect("key-pressed", self._on_escape)
            root = self.get_root()
            if root is not None:
                root.add_controller(escape)
        self.queue_allocate()

    def update_drag(self, offset: float, autoscroll: bool | None = None) -> None:
        """Move the lifted row by ``offset`` pixels from where the drag began.

        Pointer drags scroll near the scroller's edges; demo and test drags only
        when ``autoscroll`` asks for it.
        """
        if self._drag is None:
            return
        self._drag.offset = offset
        self._drag.scroll_at_event = self._scroll_value()
        self._place_drag()
        if self._drag.gesture is not None if autoscroll is None else autoscroll:
            self._autoscroll()

    def end_drag(self) -> None:
        """Drop the lifted row into its slot; commits once it has settled."""
        drag = self._drag
        if drag is None:
            return
        self._stop_drag(restore=False)
        if drag.order != self._rows:
            self._pending = drag.row
        self._roll(drag.row, self._slot_tops()[drag.row])
        self._maybe_commit()

    def cancel_drag(self) -> None:
        """Escape: glide everything back to where it was before the drag."""
        drag = self._drag
        if drag is None:
            return
        self._stop_drag(restore=True)
        self._roll(drag.row, self._slot_tops()[drag.row])
        self._maybe_commit()

    def _stop_drag(self, restore: bool) -> None:
        drag, self._drag = self._drag, None
        self._stop_autoscroll()
        if drag is None:
            return
        drag.row.remove_css_class("reorder-lifted")
        drag.row.set_cursor(None)
        if drag.handle is not None:
            drag.handle.set_cursor_from_name("grab")
        if drag.escape is not None and drag.escape.get_widget() is not None:
            drag.escape.get_widget().remove_controller(drag.escape)
        if drag.gesture is not None and drag.gesture.is_active():
            drag.gesture.reset()
        if restore and drag.order != self._rows and set(drag.order) == set(self._rows):
            self._rows[:] = drag.order
            self._sync_children()
            self._retarget(exclude=drag.row)
            self.emit("order-changed")

    def _place_drag(self) -> None:
        drag = self._drag
        row = drag.row
        slot = self._slots[row]
        total = sum(self._slots[r].height for r in self._rows)
        pointer = drag.start_y + drag.offset + (self._scroll_value() - drag.scroll_at_event)
        slot.y = max(0.0, min(total - slot.height, pointer - drag.grab))
        center = slot.y + slot.height / 2
        index, moved = self._rows.index(row), False
        # Trade places with a neighbor once the lifted row's middle passes theirs.
        while index > 0:
            above = self._rows[index - 1]
            if center < self._slot_tops()[above] + self._slots[above].height / 2:
                self._rows[index - 1], self._rows[index] = row, above
                index, moved = index - 1, True
            else:
                break
        while index < len(self._rows) - 1:
            below = self._rows[index + 1]
            if center > self._slot_tops()[below] + self._slots[below].height / 2:
                self._rows[index + 1], self._rows[index] = row, below
                index, moved = index + 1, True
            else:
                break
        if moved:
            self._sync_children()
            self._retarget(exclude=row)
            self.emit("order-changed")
        self.queue_allocate()

    # -- rolling ------------------------------------------------------------------------
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
                continue  # not allocated yet: it simply appears in place
            if abs(slot.target - top) > 0.5 or abs(slot.y - top) > 0.5:
                self._roll(row, top)

    def _roll(self, row: Gtk.Widget, top: float) -> None:
        """Glide ``row`` from where it is drawn now to ``top``."""
        slot = self._slots[row]
        slot.target = top
        if slot.y is None or not animations_enabled(self):
            slot.y = top
            self._rolled(row)
            self.queue_allocate()
            return
        if slot.animation is None:
            target = Adw.CallbackAnimationTarget.new(lambda value, r=row: self._on_roll(r, value))
            slot.animation = Adw.TimedAnimation(
                widget=self, value_from=0.0, value_to=1.0, duration=DURATION, target=target
            )
            slot.animation.set_easing(Adw.Easing.EASE_OUT_CUBIC)
            slot.animation.connect("done", lambda _a, r=row: self._rolled(r))
        slot.start, slot.end = slot.y, top
        slot.animation.reset()
        slot.animation.play()

    def _on_roll(self, row: Gtk.Widget, value: float) -> None:
        slot = self._slots.get(row)
        if slot is None:
            return
        slot.y = slot.start + (slot.end - slot.start) * value
        self.queue_allocate()

    def _rolled(self, row: Gtk.Widget) -> None:
        if self._drag is None or self._drag.row is not row:
            self._floating.discard(row)
            row.remove_css_class("reorder-moving")
        self._maybe_commit()

    def _rolling(self) -> bool:
        return any(
            slot.animation is not None and slot.animation.get_state() == Adw.AnimationState.PLAYING
            for slot in self._slots.values()
        )

    def _maybe_commit(self) -> None:
        if self._commit_source or self._drag is not None or self._rolling():
            return
        # Idle: the page may rebuild the whole list in response.
        self._commit_source = GLib.idle_add(self._commit)

    def _commit(self) -> bool:
        self._commit_source = 0
        if self._drag is not None or self._rolling():
            return False  # the next roll to finish tries again
        row, self._pending = self._pending, None
        if row is not None and row in self._rows:
            self.emit("reordered", row)
        if not self.busy:
            self.emit("settled")
        return False

    def _sync_children(self) -> None:
        # Re-sorting moves the real children (no unparenting: focus and the
        # pointer grab survive), so :first-child / :last-child follow at once.
        self._order = {row: index for index, row in enumerate(self._rows)}
        self.invalidate_sort()

    # -- scrolling ------------------------------------------------------------------------
    def _find_scroller(self) -> None:
        widget = self.get_parent()
        while widget is not None and not isinstance(widget, Gtk.ScrolledWindow):
            widget = widget.get_parent()
        self._scroller = widget

    def _scroll_value(self) -> float:
        return self._scroller.get_vadjustment().get_value() if self._scroller is not None else 0.0

    def _autoscroll(self) -> None:
        drag, scroller = self._drag, self._scroller
        if drag is None or scroller is None:
            return
        pointer = drag.start_y + drag.offset + (self._scroll_value() - drag.scroll_at_event)
        ok, point = self.compute_point(scroller, Graphene.Point().init(0, pointer))
        if not ok:
            return
        height = scroller.get_height()
        if point.y < EDGE:
            self._scroll_speed = -16 * min(1.0, (EDGE - point.y) / EDGE)
        elif point.y > height - EDGE:
            self._scroll_speed = 16 * min(1.0, (point.y - (height - EDGE)) / EDGE)
        else:
            self._scroll_speed = 0.0
        if self._scroll_speed and not self._scroll_source:
            self._scroll_source = GLib.timeout_add(16, self._scroll_tick)

    def _scroll_tick(self) -> bool:
        if self._drag is None or not self._scroll_speed or self._scroller is None:
            self._scroll_source = 0
            return False
        adjustment = self._scroller.get_vadjustment()
        top = adjustment.get_upper() - adjustment.get_page_size()
        value = max(adjustment.get_lower(), min(top, adjustment.get_value() + self._scroll_speed))
        if value == adjustment.get_value():
            self._scroll_source = 0
            return False
        adjustment.set_value(value)
        self._place_drag()
        return True

    def _stop_autoscroll(self) -> None:
        self._scroll_speed = 0.0
        if self._scroll_source:
            GLib.source_remove(self._scroll_source)
            self._scroll_source = 0

    # -- input ------------------------------------------------------------------------------
    def _row_and_handle_at(self, x: float, y: float) -> tuple[Gtk.Widget | None, Gtk.Widget | None]:
        widget = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        handle = None
        while widget is not None and widget is not self:
            if widget in self._handles:
                handle = widget
            if widget in self._slots:
                return widget, handle
            widget = widget.get_parent()
        return None, None

    def _on_drag_begin(self, gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        self._candidate = None
        row, handle = self._row_and_handle_at(x, y)
        if row is None or self._drag is not None:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            return
        top = self._slots[row].y or 0.0
        if handle is not None:
            # The handle is a slider thumb: it grabs on press.
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self.begin_drag(row, y - top, gesture, handle)
            return
        device = gesture.get_device()
        if not self.body_drag or (device is not None and device.get_source() == Gdk.InputSource.TOUCHSCREEN):
            gesture.set_state(Gtk.EventSequenceState.DENIED)  # touch scrolls; only the handle reorders
            return
        self._candidate = (row, y - top)

    def _on_drag_update(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        candidate = self._candidate
        if candidate is not None and self._drag is None:
            if abs(dy) >= BODY_THRESHOLD and abs(dy) > 1.5 * abs(dx):
                self._candidate = None
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                self.begin_drag(candidate[0], candidate[1], gesture)
            elif abs(dx) >= BODY_THRESHOLD:
                self._candidate = None  # sideways: leave it to drag-and-drop
                gesture.set_state(Gtk.EventSequenceState.DENIED)
                return
        if self._drag is not None and self._drag.gesture is gesture:
            self.update_drag(dy)

    def _on_drag_end(self, gesture: Gtk.GestureDrag, _dx: float, _dy: float) -> None:
        self._candidate = None
        if self._drag is not None and self._drag.gesture is gesture:
            self.end_drag()

    def _on_drag_cancel(self, gesture: Gtk.Gesture, _sequence) -> None:
        self._candidate = None
        if self._drag is not None and self._drag.gesture is gesture:
            self.cancel_drag()

    def _on_escape(self, _ctrl, keyval: int, _code: int, _mods) -> bool:
        if keyval == Gdk.KEY_Escape and self._drag is not None:
            self.cancel_drag()
            return True
        return False

    # -- layout ------------------------------------------------------------------------------
    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        # The list box lays rows out in their slots (and remembers them for
        # clicks and keyboard navigation); then rows go where they are drawn.
        Gtk.ListBox.do_size_allocate(self, width, height, baseline)
        if not self._rows:
            return
        top = 0.0
        for row in self._rows:
            slot = self._slots[row]
            # Measured (not get_height()): the allocation includes the separator border.
            slot.height = row.measure(Gtk.Orientation.VERTICAL, width)[0]
            dragging = self._drag is not None and self._drag.row is row
            rolling = slot.animation is not None and slot.animation.get_state() == Adw.AnimationState.PLAYING
            if not dragging:
                if rolling:
                    slot.end = top  # a resize mid-roll: keep rolling, to the new place
                else:
                    slot.y = top
                slot.target = top
            top += slot.height
        if self._drag is not None:  # the lifted row stays inside the list
            slot = self._slots[self._drag.row]
            slot.y = max(0.0, min(top - slot.height, slot.y or 0.0))
        dragged = self._drag.row if self._drag is not None else None
        for row in self._rows:
            slot = self._slots[row]
            # Rows resting in their slot were placed by the list box already. The
            # lifted row never is: its slot moves under it as it trades places.
            if row is not dragged and (slot.y is None or abs(slot.y - (slot.target or 0.0)) < 0.5):
                continue
            transform = Gsk.Transform.new().translate(Graphene.Point().init(0, round(slot.y)))
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
