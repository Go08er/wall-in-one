"""What one wallpaper's own renderer settings show, for both interfaces.

Each dial a wallpaper may set (`dials.PER_WALLPAPER`) with what applies, the
default it follows and whether it has its own; each Wallpaper Engine
property its scene declares, starting from the declared value, then a
preset's, then the person's own. The same answer the compiler gives
(`runtime_config`), so a control never shows a value that isn't the one in
use, and nothing is offered that would do nothing.
"""

from __future__ import annotations

from wall_in_one import config, dials
from wall_in_one.library import workshop
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.library.pairings import RendererOverrides


def renderer_of(item: MediaItem) -> str:
    """``video`` or ``scene``: whose dials ``item`` has; empty for a still."""
    if item.kind is Kind.VIDEO:
        return "video"
    if item.kind is Kind.SCENE:
        return "scene"
    return ""


def wallpaper_dials(
    item: MediaItem, own: RendererOverrides, settings: config.Settings
) -> dials.WallpaperDials:
    """``item``'s own settings as they stand: ``own`` over ``settings``' defaults."""
    renderer = renderer_of(item)
    if not renderer or item.unplayable is not None:
        return dials.WallpaperDials()
    shown = dials.PER_WALLPAPER[renderer]
    if item.web_page:
        shown = tuple(dial for dial in shown if dial.key in dials.WEB_KEYS)
    values: dict[str, dials.Value] = {}
    views: list[tuple[dials.Dial, dials.Value, dials.Value | None]] = []
    for dial in shown:
        default = getattr(settings, dial.setting) if dial.setting else dial.default
        mine = own.dial(renderer, dial.key)
        value = default if mine is None else mine
        values[dial.key] = value
        views.append((dial, default, mine))
    dial_views = tuple(
        dials.DialView(
            dial,
            values[dial.key],
            default,
            own=mine is not None,
            available=dials.available(dial.key, values, dials.OWN_REQUIRES),
        )
        for dial, default, mine in views
    )
    if item.web_page:
        return dials.WallpaperDials(renderer, dial_views, note=dials.WEB_PROPERTIES_NOTE)
    preset = dict(item.scene_properties)
    properties: list[dials.PropertyView] = []
    for setting in item.scene_settings:
        base = preset.get(setting.name, setting.default)
        stored = own.property(setting.name)
        mine_text = workshop.own_value(setting, stored) if stored is not None else None
        properties.append(
            dials.PropertyView(
                setting,
                base if mine_text is None else mine_text,
                base,
                own=mine_text is not None,
            )
        )
    return dials.WallpaperDials(renderer, dial_views, tuple(properties))
