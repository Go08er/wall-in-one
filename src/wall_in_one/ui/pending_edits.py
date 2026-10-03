"""Edits still queued on the ordered authoring actor, per control.

The actor accepts gestures while it is busy, so a control can have several
writes outstanding. Each control records the value it shows meanwhile and how
many writes are outstanding. Only the last one to settle (saved, failed, or
refused) puts the control back on what is durable; an earlier failure leaves
the latest queued choice showing, because that one may still be saved.
"""

from __future__ import annotations


def queue_intent[T](intents: dict[str, tuple[T, int]], key: str, value: T) -> None:
    """One more write queued for ``key``; the control shows ``value`` meanwhile."""
    _previous, outstanding = intents.get(key, (value, 0))
    intents[key] = (value, outstanding + 1)


def settle_intent[T](intents: dict[str, tuple[T, int]], key: str) -> bool:
    """One queued write for ``key`` finished; True when it was the last one."""
    entry = intents.get(key)
    if entry is None:
        return True
    value, outstanding = entry
    if outstanding > 1:
        intents[key] = (value, outstanding - 1)
        return False
    del intents[key]
    return True
