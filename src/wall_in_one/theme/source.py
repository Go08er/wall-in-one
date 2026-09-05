"""Resolve the palette the app should paint itself with.

Three tiers, best first:

1. **The template output.** If `--install-theme-template` has been run,
   Noctalia renders the live palette to `palette.json` on every change and
   pushes us a reload. This is exact and covers all four palette sources
   (builtin, wallpaper, community, custom).
2. **Generated from the current wallpaper.** If no usable template is available
   but Noctalia is running and its palette is wallpaper-derived, the same CLI
   generator supplies an approximation. Settings and IPC are separate snapshots,
   and not every shell transform is available through the CLI.
3. **A built-in fallback.** So the app starts and is usable with no Noctalia at
   all.

Tier 2 cannot cover a builtin palette -- those are compiled into Noctalia's
binary and not exposed by the CLI -- which is the concrete reason the template
is worth installing rather than optional.
"""

from __future__ import annotations

import contextlib
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Final

from wall_in_one import file_io, paths
from wall_in_one.theme import css, noctalia
from wall_in_one.theme.palette import Colour, Mode, Palette, PaletteError

CancelCheck = Callable[[], bool]


class Origin(Enum):
    TEMPLATE = "template"
    GENERATED = "generated"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class ResolvedPalette:
    palette: Palette
    origin: Origin
    detail: str

    @property
    def is_live(self) -> bool:
        """Whether colours came from Noctalia's registered live template."""
        return self.origin is Origin.TEMPLATE


#: A neutral dark palette, used only when Noctalia is absent or silent. Kept to
#: the tokens `theme.css` actually maps, since `Palette.get` degrades the rest.
_FALLBACK_TOKENS: Final[dict[str, str]] = {
    "source_color": "#6f7fa8",
    "primary": "#adc6ff",
    "on_primary": "#002e69",
    "primary_container": "#284777",
    "on_primary_container": "#d8e2ff",
    "secondary": "#bfc6dc",
    "on_secondary": "#293041",
    "secondary_container": "#3f4759",
    "on_secondary_container": "#dbe2f9",
    "tertiary": "#debcdf",
    "on_tertiary": "#402843",
    "on_tertiary_container": "#fbd7fc",
    "error": "#ffb4ab",
    "on_error": "#690005",
    "surface": "#131318",
    "on_surface": "#e4e1e9",
    "surface_variant": "#44464f",
    "on_surface_variant": "#c5c6d0",
    "surface_dim": "#131318",
    "surface_bright": "#39383f",
    "surface_container_lowest": "#0e0e13",
    "surface_container_low": "#1b1b21",
    "surface_container": "#1f1f25",
    "surface_container_high": "#2a2930",
    "surface_container_highest": "#35343a",
    "outline": "#8f909a",
    "outline_variant": "#44464f",
    "shadow": "#000000",
    "scrim": "#000000",
    "inverse_surface": "#e4e1e9",
    "inverse_on_surface": "#303036",
    "inverse_primary": "#415e91",
    "background": "#131318",
    "on_background": "#e4e1e9",
}

_LIGHT_FALLBACK_TOKENS: Final[dict[str, str]] = {
    **_FALLBACK_TOKENS,
    "primary": "#415e91",
    "on_primary": "#ffffff",
    "primary_container": "#d8e2ff",
    "on_primary_container": "#284777",
    "secondary": "#565e71",
    "on_secondary": "#ffffff",
    "secondary_container": "#dbe2f9",
    "on_secondary_container": "#3f4759",
    "tertiary": "#745471",
    "on_tertiary": "#ffffff",
    "on_tertiary_container": "#5b3d59",
    "error": "#ba1a1a",
    "on_error": "#ffffff",
    "surface": "#f9f9ff",
    "on_surface": "#1a1b20",
    "surface_variant": "#e0e2ec",
    "on_surface_variant": "#44464f",
    "surface_dim": "#d9d9e0",
    "surface_bright": "#f9f9ff",
    "surface_container_lowest": "#ffffff",
    "surface_container_low": "#f3f3fa",
    "surface_container": "#ededf4",
    "surface_container_high": "#e7e7ee",
    "surface_container_highest": "#e1e2e9",
    "outline": "#74777f",
    "outline_variant": "#c4c6d0",
    "inverse_surface": "#303036",
    "inverse_on_surface": "#f1f0f7",
    "inverse_primary": "#adc6ff",
    "background": "#f9f9ff",
    "on_background": "#1a1b20",
}

MAX_SETTINGS_BYTES: Final = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _WallpaperSettings:
    last_wallpaper: Path | None = None
    pure_black: bool = False
    problem: str = ""


def _wallpaper_settings() -> _WallpaperSettings:
    """Bounded, read-only inputs the wallpaper IPC does not expose.

    Noctalia's palette follows its last applied wallpaper, which can differ
    from the default image returned by ``wallpaper-get`` after a per-output
    change. Missing/invalid image paths fall back to that IPC; unknown colour
    transforms must not silently be treated as disabled.
    """
    try:
        raw = file_io.read_regular_bytes(paths.noctalia_settings_path(), MAX_SETTINGS_BYTES)
        if raw is None:
            return _WallpaperSettings()
        document = tomllib.loads(raw.decode("utf-8"))
        theme = document.get("theme", {})
        accessibility = document.get("accessibility", {})
        if not isinstance(theme, dict) or not isinstance(accessibility, dict):
            raise ValueError("invalid colour settings")
        pure_black = theme.get("pure_black_dark", False)
        high_contrast = accessibility.get("high_contrast", False)
        if type(pure_black) is not bool or type(high_contrast) is not bool:
            raise ValueError("invalid colour transforms")
        if high_contrast:
            return _WallpaperSettings(
                problem="Noctalia high-contrast colours require the palette template"
            )
        wallpaper = document.get("wallpaper", {})
        last = wallpaper.get("last", {}) if isinstance(wallpaper, dict) else {}
        last_path = last.get("path") if isinstance(last, dict) else None
        selected = None
        if isinstance(last_path, str) and last_path:
            # Invalid or vanished image paths need not disable otherwise usable
            # generation from the shell's default wallpaper.
            with contextlib.suppress(OSError, ValueError, RuntimeError):
                candidate = Path(last_path).expanduser()
                if candidate.is_absolute() and candidate.is_file():
                    selected = candidate
        return _WallpaperSettings(last_wallpaper=selected, pure_black=pure_black)
    except OSError, ValueError, UnicodeError, RecursionError:
        return _WallpaperSettings(
            problem="Noctalia settings could not be read for palette generation"
        )


def fallback_palette(mode: Mode = "dark") -> Palette:
    tokens = _LIGHT_FALLBACK_TOKENS if mode == "light" else _FALLBACK_TOKENS
    colours = {token: Colour.parse(value) for token, value in tokens.items()}
    return Palette(mode=mode, colours=colours)


def fixed() -> ResolvedPalette:
    """The app-owned palette used when live Noctalia colours are disabled."""
    return ResolvedPalette(
        palette=fallback_palette(),
        origin=Origin.FALLBACK,
        detail="fixed Wall-in-One palette (Noctalia following is off)",
    )


def from_template(path: Path | None = None) -> ResolvedPalette | None:
    """Load the palette Noctalia last rendered for us, if it exists."""
    target = path if path is not None else paths.palette_path()
    if not target.is_file():
        return None
    try:
        palette = Palette.load_template_output(target)
        # Partial palettes are useful in previews, but a live app palette
        # must supply every token its stylesheet needs before being adopted.
        css.render(palette)
    except PaletteError:
        return None
    return ResolvedPalette(
        palette=palette,
        origin=Origin.TEMPLATE,
        detail=f"live from Noctalia via {target}",
    )


def template_health(*, rendered_mode: Mode | None = None) -> str:
    """Report inactive/misdirected registration without shell IPC or writes.

    An orphaned output is cached data, not evidence of a live subscription.
    Settings and rendered-palette changes wake the application's monitors;
    this bounded check keeps a disabled/uninstalled registration from winning
    forever just because its old JSON is still readable.
    """
    try:
        raw = file_io.read_regular_bytes(paths.noctalia_settings_path(), MAX_SETTINGS_BYTES)
        if raw is None:
            return "Noctalia palette template is not registered"
        document = tomllib.loads(raw.decode("utf-8"))
        theme = document.get("theme")
        selected_mode = theme.get("mode") if isinstance(theme, dict) else None
        if (
            rendered_mode is not None
            and selected_mode in ("dark", "light")
            and selected_mode != rendered_mode
        ):
            return "Noctalia palette template has not rendered the selected mode"
        entry: object = document
        for key in ("theme", "templates", "user", "wall-in-one"):
            entry = entry.get(key) if isinstance(entry, dict) else None
        if not isinstance(entry, dict):
            return "Noctalia palette template is not registered"
        if entry.get("enabled") is not True:
            return "Noctalia palette template is disabled"
        if entry.get("output_path") != str(paths.palette_path()):
            return "Noctalia palette template writes to a different location"
        input_path = entry.get("input_path")
        if not isinstance(input_path, str) or not Path(input_path).is_file():
            return "Noctalia palette template input is missing"
        output = paths.palette_path()
        if output.is_file() and Path(input_path).stat().st_mtime_ns > output.stat().st_mtime_ns:
            return "Noctalia palette template has not rendered its updated input"
    except OSError, ValueError, UnicodeError, RecursionError:
        return "Noctalia palette template registration could not be read"
    return ""


def from_current_wallpaper(
    scheme: str | None = None,
    *,
    cancelled: CancelCheck | None = None,
    _settings: _WallpaperSettings | None = None,
) -> ResolvedPalette | None:
    """Approximate Noctalia's wallpaper palette using supported CLI inputs."""
    if cancelled is not None and cancelled():
        return None
    settings = _wallpaper_settings() if _settings is None else _settings
    if settings.problem:
        return None
    try:
        wallpaper = settings.last_wallpaper or noctalia.current_wallpaper(cancelled=cancelled)
        if cancelled is not None and cancelled():
            return None
        if wallpaper is None or not wallpaper.is_file():
            return None
        selection = noctalia.current_scheme_selection(cancelled=cancelled)
        if cancelled is not None and cancelled():
            return None
        mode = noctalia.current_mode(cancelled=cancelled)
    except noctalia.NoctaliaError:
        return None
    if cancelled is not None and cancelled():
        return None

    # Only meaningful when Noctalia is actually deriving its palette from the
    # wallpaper. For any other source, regenerating would show colours the rest
    # of the desktop is not using.
    if selection.source != "wallpaper":
        return None

    effective = selection.name or scheme or noctalia.DEFAULT_SCHEME
    if cancelled is not None and cancelled():
        return None
    try:
        pair = noctalia.generate(
            wallpaper, effective, pure_black=settings.pure_black, cancelled=cancelled
        )
    except noctalia.NoctaliaError:
        return None
    if cancelled is not None and cancelled():
        return None

    return ResolvedPalette(
        palette=pair.for_mode(mode),
        origin=Origin.GENERATED,
        detail=f"approximate colours generated from {wallpaper.name} with {effective}",
    )


def resolve(
    *,
    scheme: str | None = None,
    cancelled: CancelCheck | None = None,
) -> ResolvedPalette:
    """Best available palette. Never raises -- always returns something usable."""
    if cancelled is not None and cancelled():
        return fixed()
    from_file = from_template()
    health = template_health(rendered_mode=from_file.palette.mode if from_file else None)
    if from_file is not None and not health:
        return from_file

    wallpaper_settings = _wallpaper_settings()
    generated = from_current_wallpaper(scheme, cancelled=cancelled, _settings=wallpaper_settings)
    if generated is not None:
        return replace(
            generated,
            detail=f"{generated.detail}; {health or 'no usable rendered palette'}",
        )
    if cancelled is not None and cancelled():
        return fixed()

    mode: Mode = "dark"
    with contextlib.suppress(noctalia.NoctaliaError):
        mode = noctalia.current_mode(cancelled=cancelled)

    degraded = "; ".join(
        reason
        for reason in (health or "no usable rendered palette", wallpaper_settings.problem)
        if reason
    )
    reason = (
        "Noctalia not available"
        if not noctalia.is_available()
        else degraded + "; run `wall-in-one --install-theme-template` and apply Noctalia templates"
    )
    return ResolvedPalette(
        palette=fallback_palette(mode),
        origin=Origin.FALLBACK,
        detail=reason,
    )
