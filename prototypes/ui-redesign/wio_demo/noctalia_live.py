"""Read-only view of the real desktop's colors and wallpaper, kept live.

The prototype never writes anything, but it can *follow* the real Noctalia:

* ``$XDG_STATE_HOME/wall-in-one/palette.json`` is the palette Noctalia renders
  through Wall-in-One's template on every change (the same file the real app
  reads). It holds the Material tokens and the mode.
* ``$XDG_STATE_HOME/noctalia/settings.toml`` names the last applied wallpaper,
  which the frosted style blurs behind the window.

Both folders are watched, not the files: Noctalia replaces files atomically,
and a watch on the old file would go quiet after the first rename. Reads are
bounded and a half-written or invalid palette is ignored until the next event.
"""

from __future__ import annotations

import json
import os
import re
import threading
import tomllib
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, GObject

MAX_BYTES = 1024 * 1024
MAX_SETTINGS_BYTES = 8 * 1024 * 1024
_HEX = re.compile(r"#[0-9a-fA-F]{6}")
#: Tokens a palette must have before the app adopts it.
REQUIRED = ("primary", "on_primary", "surface", "on_surface", "error", "secondary", "tertiary")


def _state_home() -> Path:
    value = os.environ.get("XDG_STATE_HOME", "")
    return Path(value) if value.startswith("/") else Path.home() / ".local" / "state"


def _read(path: Path, limit: int) -> bytes | None:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return None
    return data if len(data) <= limit else None


class LiveNoctalia(GObject.Object):
    """Emits ``changed`` when the real palette or wallpaper changes."""

    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}  # noqa: RUF012

    def __init__(self) -> None:
        super().__init__()
        state = _state_home()
        self.palette_path = state / "wall-in-one" / "palette.json"
        self.settings_path = state / "noctalia" / "settings.toml"
        self.tokens: dict[str, str] = {}
        self.mode = "dark"
        self.wallpaper_path: Path | None = None
        self.wallpaper: Gdk.Texture | None = None
        self._pending = 0
        self._monitors = []
        for folder in {self.palette_path.parent, self.settings_path.parent}:
            if folder.is_dir():
                monitor = Gio.File.new_for_path(str(folder)).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
                monitor.connect("changed", self._on_event)
                self._monitors.append(monitor)
        self._reload()

    @property
    def found(self) -> bool:
        return bool(self.tokens)

    def describe(self) -> str:
        return f"Live from Noctalia · {self.mode}" if self.found else "Noctalia's palette wasn't found"

    # -- events ---------------------------------------------------------------
    def _on_event(self, _monitor, file: Gio.File, other: Gio.File | None, _event) -> None:
        names = {file.get_basename(), other.get_basename() if other else None}
        if names & {self.palette_path.name, self.settings_path.name} and not self._pending:
            # A burst of events (write, rename, attribute) settles into one reload.
            self._pending = GLib.timeout_add(60, self._settle)

    def _settle(self) -> bool:
        self._pending = 0
        self._reload()
        return False

    def _reload(self) -> None:
        changed = self._load_palette()
        changed |= self._load_wallpaper_path()
        if changed:
            self.emit("changed")

    # -- palette --------------------------------------------------------------
    def _load_palette(self) -> bool:
        raw = _read(self.palette_path, MAX_BYTES)
        try:
            document = json.loads(raw) if raw else None
        except ValueError, RecursionError:
            return False  # half-written; the next event brings the whole file
        if not isinstance(document, dict) or not isinstance(document.get("colors"), dict):
            return False
        tokens = {
            str(key): value.lower()
            for key, value in document["colors"].items()
            if isinstance(value, str) and _HEX.fullmatch(value)
        }
        mode = document.get("mode") if document.get("mode") in ("dark", "light") else "dark"
        if any(key not in tokens for key in REQUIRED) or (tokens, mode) == (self.tokens, self.mode):
            return False
        self.tokens, self.mode = tokens, mode
        return True

    def token(self, name: str, *fallbacks: str) -> str:
        for key in (name, *fallbacks):
            if key in self.tokens:
                return self.tokens[key]
        return self.tokens["surface"]

    def swatches(self) -> list[str]:
        """[surface, primary, secondary, tertiary, error], like the demo palettes."""
        return [self.token(name) for name in ("surface", "primary", "secondary", "tertiary", "error")]

    # -- wallpaper ------------------------------------------------------------
    def _load_wallpaper_path(self) -> bool:
        raw = _read(self.settings_path, MAX_SETTINGS_BYTES)
        path = None
        try:
            document = tomllib.loads(raw.decode()) if raw else {}
            last = document.get("wallpaper", {}).get("last", {})
            candidate = Path(str(last.get("path", ""))).expanduser()
            if candidate.is_absolute() and candidate.is_file():
                path = candidate
        except ValueError, UnicodeError, AttributeError, OSError:
            pass
        if path == self.wallpaper_path:
            return False
        self.wallpaper_path = path
        if path is not None:
            # Wallpapers are often 4K; decode a small copy off the main thread.
            threading.Thread(target=self._decode, args=(path,), daemon=True).start()
        else:
            self.wallpaper = None
        return True

    def _decode(self, path: Path) -> None:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 640, 360, True)
        except GLib.Error:
            return

        pixel_format = Gdk.MemoryFormat.R8G8B8A8 if pixbuf.get_has_alpha() else Gdk.MemoryFormat.R8G8B8
        pixels = pixbuf.read_pixel_bytes()

        def done() -> bool:
            if path == self.wallpaper_path:
                self.wallpaper = Gdk.MemoryTexture.new(
                    pixbuf.get_width(), pixbuf.get_height(), pixel_format, pixels, pixbuf.get_rowstride()
                )
                self.emit("changed")
            return False

        GLib.idle_add(done)
