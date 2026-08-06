"""XDG base directories, and the specific files this app and Noctalia use."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

APP_ID: Final = "wall-in-one"
"""XDG directory name and the `ctl` command name.

Names `~/.config/wall-in-one`, `~/.local/state/wall-in-one`, and the control
socket. Not the Wayland app-id -- see :data:`APPLICATION_ID`.
"""

APPLICATION_ID: Final = "dev.goober.WallInOne"
"""GApplication id, and therefore the Wayland app-id.

GtkApplication sets the Wayland app-id from the GApplication id at startup, so
this string is what a niri `window-rule { match app-id=... }` has to match.
Verified by reading `niri msg -j windows` against a live instance.

Must stay stable: changing it silently breaks every user's blur and opacity
window rules.
"""


def _xdg(variable: str, default: Path) -> Path:
    raw = os.environ.get(variable)
    if not raw:
        return default
    candidate = Path(raw)
    # The spec says relative paths are invalid and must be ignored.
    return candidate if candidate.is_absolute() else default


def config_home() -> Path:
    return _xdg("XDG_CONFIG_HOME", Path.home() / ".config")


def state_home() -> Path:
    return _xdg("XDG_STATE_HOME", Path.home() / ".local" / "state")


def cache_home() -> Path:
    return _xdg("XDG_CACHE_HOME", Path.home() / ".cache")


def runtime_dir() -> Path:
    raw = os.environ.get("XDG_RUNTIME_DIR")
    if raw and Path(raw).is_absolute():
        return Path(raw)
    # No runtime dir is unusual but survivable; the socket just lives somewhere
    # less appropriate rather than the app refusing to start.
    return cache_home()


def app_config_dir() -> Path:
    return config_home() / APP_ID


def app_state_dir() -> Path:
    return state_home() / APP_ID


def app_cache_dir() -> Path:
    return cache_home() / APP_ID


def settings_path() -> Path:
    return app_config_dir() / "settings.toml"


def palette_path() -> Path:
    """Where Noctalia's user template renders the live palette for us."""
    return app_state_dir() / "palette.json"


def socket_path() -> Path:
    return runtime_dir() / f"{APP_ID}.sock"


def noctalia_state_dir() -> Path:
    return state_home() / "noctalia"


def noctalia_settings_path() -> Path:
    return noctalia_state_dir() / "settings.toml"


def noctalia_custom_palettes_dir() -> Path:
    """`noctalia/src/theme/custom_palettes.cpp:215-220`."""
    return config_home() / "noctalia" / "palettes"


def noctalia_community_palettes_dir() -> Path:
    return noctalia_state_dir() / "community-palettes"


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
