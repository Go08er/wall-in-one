"""Application settings, persisted as TOML.

Deliberately small and flat. Anything Noctalia already owns -- the palette, the
theme mode, the wallpaper directory -- is read from Noctalia rather than
duplicated here, so there is only ever one source of truth.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Self

from wall_in_one import paths
from wall_in_one.theme.noctalia import ALL_SCHEMES, DEFAULT_SCHEME

#: Below this the window stops being legible against a busy wallpaper, and the
#: compositor's blur cannot rescue it.
MIN_OPACITY: Final = 0.30


class ConfigError(Exception):
    """The settings file could not be read or was malformed."""


@dataclass(frozen=True, slots=True)
class Settings:
    #: Window background opacity. 1.0 is fully opaque. Values below 1.0 let the
    #: compositor show through; on niri >= 26.04 a `background-effect` window
    #: rule can then blur what shows through. The app never requests blur
    #: itself -- see docs/niri.md.
    opacity: float = 1.0

    #: Scheme used when previewing or generating a palette from a wallpaper.
    #: One of `wall_in_one.theme.noctalia.ALL_SCHEMES`.
    preview_scheme: str = DEFAULT_SCHEME

    #: Follow Noctalia's palette, applying it to the app's own chrome.
    follow_noctalia_palette: bool = True

    #: Seconds between automatic wallpaper changes when cycling is on.
    cycle_interval: int = 300
    cycle_enabled: bool = False
    shuffle: bool = False

    #: When off, video wallpapers are paused and their paired stills shown
    #: instead. Blur is materially more expensive over an animated wallpaper,
    #: so this is a performance control as much as a battery one.
    dynamics_enabled: bool = True

    def validated(self) -> Self:
        """Clamp and correct anything out of range rather than failing.

        A bad settings file should degrade to something usable, not stop the
        app from starting.
        """
        opacity = min(1.0, max(MIN_OPACITY, self.opacity))
        scheme = self.preview_scheme if self.preview_scheme in ALL_SCHEMES else DEFAULT_SCHEME
        interval = min(24 * 60 * 60, max(5, self.cycle_interval))
        return replace(
            self,
            opacity=opacity,
            preview_scheme=scheme,
            cycle_interval=interval,
        )

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> Self:
        def boolean(key: str, fallback: bool) -> bool:
            value = raw.get(key, fallback)
            return value if isinstance(value, bool) else fallback

        def number(key: str, fallback: float) -> float:
            value = raw.get(key, fallback)
            return (
                float(value)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else fallback
            )

        def text(key: str, fallback: str) -> str:
            value = raw.get(key, fallback)
            return value if isinstance(value, str) else fallback

        return cls(
            opacity=number("opacity", 1.0),
            preview_scheme=text("preview_scheme", DEFAULT_SCHEME),
            follow_noctalia_palette=boolean("follow_noctalia_palette", True),
            cycle_interval=int(number("cycle_interval", 300)),
            cycle_enabled=boolean("cycle_enabled", False),
            shuffle=boolean("shuffle", False),
            dynamics_enabled=boolean("dynamics_enabled", True),
        ).validated()

    def to_toml(self) -> str:
        lines = (
            "# wall-in-one settings",
            "",
            f"opacity = {self.opacity:.2f}",
            f'preview_scheme = "{self.preview_scheme}"',
            f"follow_noctalia_palette = {str(self.follow_noctalia_palette).lower()}",
            f"cycle_interval = {self.cycle_interval}",
            f"cycle_enabled = {str(self.cycle_enabled).lower()}",
            f"shuffle = {str(self.shuffle).lower()}",
            f"dynamics_enabled = {str(self.dynamics_enabled).lower()}",
        )
        return "\n".join(lines) + "\n"


def load(path: Path | None = None) -> Settings:
    """Read settings, falling back to defaults when absent or unreadable."""
    target = path if path is not None else paths.settings_path()
    try:
        with target.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        return Settings()
    except (OSError, tomllib.TOMLDecodeError):
        # A corrupt settings file should not be fatal; defaults are always
        # usable and the user can fix or delete the file.
        return Settings()
    return Settings.from_mapping(raw)


def save(settings: Settings, path: Path | None = None) -> Path:
    target = path if path is not None else paths.settings_path()
    paths.ensure_directory(target.parent)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(settings.validated().to_toml())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ConfigError(f"cannot write {target}: {error}") from error
    return target
