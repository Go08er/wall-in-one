"""A module-scoped ``Gtk.init()`` already runs in the session sandbox.

GUI test modules initialize GTK in a module-scoped fixture, which runs before
conftest's function-scoped isolation. GLib also caches the user directories
the first time anything asks for them. So the sandbox has to be the process
baseline from conftest's import on, buses and portals included; this module
initializes GTK the same way and checks what it saw.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # noqa: E402

from tests import conftest  # noqa: E402

_SEEN: dict[str, str] = {}


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    """As every GUI module does it, and recording the environment it ran in."""
    _SEEN.update(os.environ)
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    _SEEN.update(
        {
            "glib-home": GLib.get_home_dir(),
            "glib-config": GLib.get_user_config_dir(),
            "glib-state": GLib.get_user_state_dir(),
            "glib-cache": GLib.get_user_cache_dir(),
            "glib-data": GLib.get_user_data_dir(),
            "glib-runtime": GLib.get_user_runtime_dir(),
        }
    )


def test_gtk_was_initialized_inside_the_session_sandbox() -> None:
    root = conftest.SESSION_ROOT
    for variable, name in conftest.SESSION_LAYOUT.items():
        assert _SEEN[variable] == str(root / name), variable
    runtime = root / conftest.SESSION_LAYOUT["XDG_RUNTIME_DIR"]
    assert _SEEN["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime / conftest.DEAD_SESSION_BUS}"
    assert _SEEN["DBUS_SYSTEM_BUS_ADDRESS"] == f"unix:path={runtime / conftest.DEAD_SYSTEM_BUS}"
    for variable, value in conftest.SESSION_SET.items():
        assert _SEEN[variable] == value, variable
    for variable in conftest.SESSION_UNSET:
        assert variable not in _SEEN, variable
    assert "DISPLAY" in _SEEN, "GUI tests keep Xvfb's display"


def test_glib_never_cached_the_real_home_directories() -> None:
    """GLib keeps its first answer for the whole process.

    Which sandbox directory that is depends on what asked first (the session
    root, or an earlier test's own); it is never the account's real ones.
    """
    real = conftest.REAL_HOME
    defaults = {
        real,
        real / ".config",
        real / ".local" / "state",
        real / ".cache",
        real / ".local" / "share",
    }
    for key in ("home", "config", "state", "cache", "data", "runtime"):
        cached = Path(_SEEN[f"glib-{key}"])
        assert cached not in defaults, (key, cached)
        for guarded in conftest.GUARDED_ROOTS:
            assert not cached.is_relative_to(guarded), (key, cached)
