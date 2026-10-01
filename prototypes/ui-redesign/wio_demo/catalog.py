"""Fixed words and choices the UI shows. Pure constants: they never come from
the backend, so pages may import them directly (unlike live data, which only
comes through ``AppState``)."""

from __future__ import annotations

#: A wallpaper's kind, as a word and as an icon: the app's own words.
from wall_in_one.ui.next.catalog import KIND_ICON, KIND_LABEL

__all__ = ["DAYS", "DAYS_LONG", "INTERVALS", "KIND_ICON", "KIND_LABEL", "MONTHS", "MONTHS_LONG"]

#: Weekdays and months, short and long, indexed the way rules store them (Mon = 0, Jan = 0).
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAYS_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHS_LONG = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]

#: "Change every …" presets, in minutes (0 = don't change).
INTERVALS = [
    (0, "Don't change"),
    (5, "Every 5 minutes"),
    (15, "Every 15 minutes"),
    (30, "Every 30 minutes"),
    (60, "Every hour"),
    (120, "Every 2 hours"),
    (360, "Every 6 hours"),
    (1440, "Once a day"),
]
