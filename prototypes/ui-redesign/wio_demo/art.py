"""Procedural wallpaper art, so the demo looks real without any media files.

Every wallpaper in `data.py` names a style, a seed and a time of day. The same
inputs always draw the same picture, at any size. Nothing here touches disk.
"""

from __future__ import annotations

import colorsys
import math
import random
from dataclasses import dataclass
from functools import lru_cache

import cairo
import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib

RGB = tuple[float, float, float]


def hls(h: float, l: float, s: float) -> RGB:  # noqa: E741 - color notation
    return colorsys.hls_to_rgb(h % 1.0, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))


def mix(a: RGB, b: RGB, t: float) -> RGB:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def to_hex(c: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(v * 255))) for v in c))


@dataclass(frozen=True)
class Look:
    """The handful of colors a picture is built from (also its 'dominant' colors)."""

    sky_top: RGB
    sky_low: RGB
    accent: RGB
    land: RGB
    hue: float


def _look(style: str, seed: int, night: bool) -> Look:
    r = random.Random(seed)
    land_hue = {
        "peaks": 0.60,
        "ocean": 0.55,
        "city": 0.70,
        "forest": 0.38,
        "aurora": 0.45,
        "pond": 0.33,
        "blocks": 0.28,
        "rain": 0.60,
        "dunes": 0.08,
        "abstract": r.random(),
    }.get(style, r.random()) + r.uniform(-0.05, 0.05)
    # Skies are blue-ish whatever the land is; deserts and abstract art may differ.
    sky_hue = land_hue if style in ("dunes", "abstract", "aurora", "rain") else 0.57 + r.uniform(-0.05, 0.04)
    if night:
        sky_hue = land_hue if style in ("abstract", "aurora") else 0.66 + r.uniform(-0.04, 0.04)
        return Look(
            sky_top=hls(sky_hue + 0.04, 0.07, 0.55),
            sky_low=hls(sky_hue - 0.02, 0.24, 0.45),
            accent=hls(sky_hue + 0.5 if style in ("city", "aurora") else 0.13, 0.74, 0.85),
            land=hls(land_hue, 0.16, 0.38),
            hue=land_hue,
        )
    warm = r.random() < 0.45
    return Look(
        sky_top=hls(sky_hue, 0.55, 0.60),
        sky_low=hls(0.07 if warm else sky_hue - 0.03, 0.80 if warm else 0.84, 0.80 if warm else 0.50),
        accent=hls(0.11 if warm else 0.13, 0.75, 0.95),
        land=hls(land_hue, 0.32, 0.45),
        hue=land_hue,
    )


def _sky(cr: cairo.Context, w: int, h: int, look: Look, horizon: float) -> None:
    g = cairo.LinearGradient(0, 0, 0, h * horizon)
    g.add_color_stop_rgb(0, *look.sky_top)
    g.add_color_stop_rgb(1, *look.sky_low)
    cr.set_source(g)
    cr.rectangle(0, 0, w, h)
    cr.fill()


def _glow(cr: cairo.Context, x: float, y: float, radius: float, color: RGB, alpha: float) -> None:
    g = cairo.RadialGradient(x, y, 0, x, y, radius)
    g.add_color_stop_rgba(0, *color, alpha)
    g.add_color_stop_rgba(1, *color, 0)
    cr.set_source(g)
    cr.arc(x, y, radius, 0, math.tau)
    cr.fill()


def _stars(cr: cairo.Context, r: random.Random, w: int, h: int, limit: float, count: int) -> None:
    for _ in range(count):
        x, y = r.uniform(0, w), r.uniform(0, h * limit) ** 1.0
        size = r.choice((0.6, 0.8, 1.0, 1.4)) * w / 960
        cr.set_source_rgba(1, 1, 1, r.uniform(0.35, 0.95))
        cr.arc(x, y, size, 0, math.tau)
        cr.fill()


def _ridge(cr, r, w, h, base_y, amplitude, roughness, color: RGB, alpha=1.0) -> None:
    cr.move_to(0, h)
    steps = 48
    phase = r.uniform(0, 10)
    for i in range(steps + 1):
        x = w * i / steps
        n = (
            math.sin(i * 0.31 * roughness + phase) * 0.55
            + math.sin(i * 0.77 * roughness + phase * 2) * 0.30
            + r.uniform(-0.25, 0.25) * roughness * 0.4
        )
        cr.line_to(x, base_y - amplitude * (0.5 + n * 0.5))
    cr.line_to(w, h)
    cr.close_path()
    cr.set_source_rgba(*color, alpha)
    cr.fill()


def _draw_peaks(cr, r, w, h, look: Look, night: bool) -> None:
    _sky(cr, w, h, look, 0.75)
    if night:
        _stars(cr, r, w, h, 0.6, 140)
    sx, sy = r.uniform(0.2, 0.8) * w, r.uniform(0.18, 0.4) * h
    _glow(cr, sx, sy, h * 0.35, look.accent, 0.45)
    cr.set_source_rgb(*mix(look.accent, (1, 1, 1), 0.35))
    cr.arc(sx, sy, h * 0.06, 0, math.tau)
    cr.fill()
    layers = 5
    for i in range(layers):
        t = i / (layers - 1)
        color = mix(mix(look.sky_low, look.sky_top, 0.3), look.land, 0.35 + 0.65 * t)
        if night:
            color = mix(color, (0.02, 0.02, 0.05), 0.3 * t)
        _ridge(cr, r, w, h, h * (0.45 + 0.12 * i), h * (0.32 - 0.04 * i), 1.6 - 0.2 * i, color)


def _draw_ocean(cr, r, w, h, look: Look, night: bool) -> None:
    horizon = h * 0.58
    _sky(cr, w, h, look, 0.58)
    if night:
        _stars(cr, r, w, h, 0.5, 120)
    sx, sy = r.uniform(0.3, 0.7) * w, horizon - h * r.uniform(0.04, 0.2)
    _glow(cr, sx, sy, h * 0.45, look.accent, 0.5)
    cr.set_source_rgb(*mix(look.accent, (1, 1, 1), 0.3))
    cr.arc(sx, sy, h * 0.07, 0, math.tau)
    cr.fill()
    sea = cairo.LinearGradient(0, horizon, 0, h)
    sea.add_color_stop_rgb(0, *mix(look.sky_low, look.sky_top, 0.55))
    sea.add_color_stop_rgb(1, *mix(look.sky_top, (0, 0, 0.05), 0.55))
    cr.set_source(sea)
    cr.rectangle(0, horizon, w, h - horizon)
    cr.fill()
    for i in range(36):
        y = horizon + (h - horizon) * (i / 36) ** 1.6
        half = (w * 0.03) + (w * 0.18) * (i / 36)
        cr.set_source_rgba(*look.accent, 0.55 * (1 - i / 40))
        cr.set_line_width(max(1.0, h / 400 * (1 + i / 12)))
        cr.move_to(sx - half * r.uniform(0.3, 1), y)
        cr.line_to(sx + half * r.uniform(0.3, 1), y)
        cr.stroke()


def _draw_city(cr, r, w, h, look: Look, night: bool) -> None:
    _sky(cr, w, h, look, 0.8)
    if night:
        _stars(cr, r, w, h, 0.4, 70)
        _glow(cr, w * r.uniform(0.6, 0.85), h * 0.2, h * 0.15, (0.95, 0.95, 0.85), 0.35)
    for layer in range(3):
        x = -r.uniform(0, 30)
        color = mix(look.sky_low, look.land, 0.45 + 0.27 * layer)
        if night:
            color = mix(color, (0.03, 0.02, 0.06), 0.4 + 0.2 * layer)
        while x < w:
            bw = r.uniform(0.04, 0.09) * w
            bh = r.uniform(0.18, 0.5 - 0.08 * layer) * h
            top = h * (0.92 - 0.03 * layer) - bh
            cr.set_source_rgb(*color)
            cr.rectangle(x, top, bw, h - top)
            cr.fill()
            if layer == 2 or (layer == 1 and night):
                lit = 0.35 if night else 0.08
                for wy in range(int(top + 8), int(h * 0.9), max(6, int(h / 70))):
                    for wx in range(int(x + 5), int(x + bw - 5), max(6, int(w / 140))):
                        if r.random() < lit:
                            cr.set_source_rgba(*look.accent, r.uniform(0.6, 1.0))
                            cr.rectangle(wx, wy, max(2, w / 360), max(2, h / 300))
                            cr.fill()
            x += bw + r.uniform(0, 0.01) * w
    floor = cairo.LinearGradient(0, h * 0.9, 0, h)
    floor.add_color_stop_rgba(0, *look.land, 0.0)
    floor.add_color_stop_rgba(1, *mix(look.land, (0, 0, 0), 0.6), 1.0)
    cr.set_source(floor)
    cr.rectangle(0, h * 0.86, w, h * 0.14)
    cr.fill()


def _tree(cr, x, base, height, color: RGB) -> None:
    cr.set_source_rgb(*color)
    tiers = 4
    for i in range(tiers):
        top = base - height * (1 - i / tiers * 0.8)
        width = height * 0.32 * (1 - i / (tiers + 1))
        cr.move_to(x, top - height * 0.18)
        cr.line_to(x - width, base - height * (i / tiers) * 0.7)
        cr.line_to(x + width, base - height * (i / tiers) * 0.7)
        cr.close_path()
        cr.fill()


def _draw_forest(cr, r, w, h, look: Look, night: bool) -> None:
    _sky(cr, w, h, look, 0.7)
    if night:
        _stars(cr, r, w, h, 0.45, 90)
    _glow(cr, w * r.uniform(0.3, 0.7), h * 0.35, h * 0.5, look.accent, 0.3)
    for layer in range(4):
        t = layer / 3
        color = mix(mix(look.sky_low, look.land, 0.4), mix(look.land, (0, 0.05, 0.02), 0.5), t)
        base = h * (0.62 + 0.12 * layer)
        x = -20.0
        while x < w + 20:
            height = h * r.uniform(0.22, 0.34) * (1 + t * 0.6)
            _tree(cr, x, base + h * 0.05, height, color)
            x += r.uniform(0.025, 0.06) * w * (1 + t)
        cr.set_source_rgb(*color)
        cr.rectangle(0, base, w, h - base)
        cr.fill()
        fog = cairo.LinearGradient(0, base - h * 0.08, 0, base + h * 0.04)
        fog.add_color_stop_rgba(0, *look.sky_low, 0)
        fog.add_color_stop_rgba(1, *look.sky_low, 0.25 * (1 - t))
        cr.set_source(fog)
        cr.rectangle(0, base - h * 0.08, w, h * 0.12)
        cr.fill()


def _draw_aurora(cr, r, w, h, look: Look, night: bool) -> None:
    sky_top = hls(look.hue + 0.1, 0.05, 0.6)
    _sky(cr, w, h, Look(sky_top, hls(look.hue, 0.18, 0.5), look.accent, look.land, look.hue), 0.8)
    _stars(cr, r, w, h, 0.8, 180)
    cr.save()
    cr.set_operator(cairo.OPERATOR_ADD)
    for band in range(3):
        hue = look.hue + band * 0.07
        phase, amp = r.uniform(0, 6), r.uniform(0.04, 0.09) * h
        y0 = h * (0.25 + band * 0.08)
        for i in range(0, w, max(2, w // 240)):
            y = y0 + math.sin(i / w * math.tau * 1.3 + phase) * amp
            g = cairo.LinearGradient(i, y - h * 0.22, i, y + h * 0.02)
            g.add_color_stop_rgba(0, *hls(hue + 0.12, 0.6, 0.9), 0.0)
            g.add_color_stop_rgba(0.75, *hls(hue, 0.55, 0.95), 0.30)
            g.add_color_stop_rgba(1, *hls(hue, 0.7, 0.9), 0.0)
            cr.set_source(g)
            cr.rectangle(i, y - h * 0.22, max(2, w // 240), h * 0.24)
            cr.fill()
    cr.restore()
    _ridge(cr, r, w, h, h * 0.86, h * 0.18, 1.2, hls(look.hue, 0.05, 0.3))


def _lily(cr, x, y, rx, color: RGB) -> None:
    cr.save()
    cr.translate(x, y)
    cr.scale(1.0, 0.45)
    cr.move_to(0, 0)
    cr.arc(0, 0, rx, 0.25, math.tau - 0.1)
    cr.close_path()
    cr.set_source_rgb(*color)
    cr.fill()
    cr.restore()


def _draw_pond(cr, r, w, h, look: Look, night: bool) -> None:
    water = cairo.LinearGradient(0, 0, 0, h)
    water.add_color_stop_rgb(0, *hls(look.hue + 0.15, 0.18 if night else 0.35, 0.45))
    water.add_color_stop_rgb(1, *hls(look.hue + 0.2, 0.08 if night else 0.22, 0.5))
    cr.set_source(water)
    cr.rectangle(0, 0, w, h)
    cr.fill()
    for _ in range(26):
        x, y = r.uniform(0, w), r.uniform(0, h)
        _glow(cr, x, y, r.uniform(0.05, 0.14) * h, hls(look.hue + 0.1, 0.6, 0.5), 0.12)
    for _ in range(14):
        x, y, rx = r.uniform(0, w), r.uniform(0.1, 1.0) * h, r.uniform(0.05, 0.11) * w
        _lily(cr, x, y, rx, hls(0.30 + r.uniform(-0.03, 0.03), 0.22 if night else 0.38, 0.55))
        if r.random() < 0.35:
            cr.set_source_rgb(*hls(0.92, 0.75, 0.7))
            cr.arc(x + rx * 0.2, y - rx * 0.15, rx * 0.16, 0, math.tau)
            cr.fill()
    # A frog, because the owner's playlists are frog-themed.
    fx, fy, size = w * 0.62, h * 0.55, h * 0.09
    _lily(cr, fx, fy + size * 0.6, size * 2.3, hls(0.31, 0.3 if night else 0.42, 0.6))
    body = hls(0.28, 0.3 if night else 0.45, 0.7)
    cr.set_source_rgb(*body)
    cr.save()
    cr.translate(fx, fy)
    cr.scale(1.25, 0.8)
    cr.arc(0, 0, size, 0, math.tau)
    cr.restore()
    cr.fill()
    for side in (-1, 1):
        cr.set_source_rgb(*body)
        cr.arc(fx + side * size * 0.55, fy - size * 0.75, size * 0.36, 0, math.tau)
        cr.fill()
        cr.set_source_rgb(0.97, 0.95, 0.85)
        cr.arc(fx + side * size * 0.55, fy - size * 0.78, size * 0.24, 0, math.tau)
        cr.fill()
        cr.set_source_rgb(0.05, 0.05, 0.05)
        cr.arc(fx + side * size * 0.55, fy - size * 0.78, size * 0.12, 0, math.tau)
        cr.fill()


def _draw_blocks(cr, r, w, h, look: Look, night: bool) -> None:
    _sky(cr, w, h, look, 0.65)
    block = max(6, w // 48)
    if night:
        _stars(cr, r, w, h, 0.5, 60)
        cr.set_source_rgb(0.92, 0.92, 0.85)
        cr.rectangle(w * 0.75, h * 0.12, block * 3, block * 3)
        cr.fill()
    else:
        cr.set_source_rgb(1.0, 0.92, 0.55)
        cr.rectangle(w * 0.18, h * 0.12, block * 4, block * 4)
        cr.fill()
        for _ in range(5):
            cx, cy = r.uniform(0, w), r.uniform(0.05, 0.35) * h
            cr.set_source_rgba(1, 1, 1, 0.85)
            for j in range(r.randint(3, 6)):
                cr.rectangle(cx + j * block, cy + (block if j % 2 else 0), block * 2, block)
            cr.fill()
    height = r.uniform(0.5, 0.65)
    shade = 0.45 if night else 1.0
    for col in range(0, w // block + 1):
        height += r.choice((-0.03, 0, 0, 0.03))
        height = max(0.4, min(0.75, height))
        top = int(h * height / block) * block
        for y in range(top, h, block):
            depth = (y - top) // block
            if depth == 0:
                color = hls(0.30, 0.42 * shade, 0.55)
            elif depth < 3:
                color = hls(0.07, 0.32 * shade, 0.45)
            else:
                color = hls(0.6, 0.38 * shade, 0.05)
            jitter = r.uniform(-0.03, 0.03)
            cr.set_source_rgb(*(min(1, max(0, c + jitter)) for c in color))
            cr.rectangle(col * block, y, block, block)
            cr.fill()


def _draw_rain(cr, r, w, h, look: Look, night: bool) -> None:
    g = cairo.LinearGradient(0, 0, w, h)
    g.add_color_stop_rgb(0, *hls(look.hue, 0.12 if night else 0.3, 0.35))
    g.add_color_stop_rgb(1, *hls(look.hue + 0.1, 0.06 if night else 0.18, 0.4))
    cr.set_source(g)
    cr.rectangle(0, 0, w, h)
    cr.fill()
    for _ in range(70):
        hue = r.choice((0.08, 0.12, 0.95, look.hue, look.hue + 0.5))
        _glow(
            cr, r.uniform(0, w), r.uniform(0, h), r.uniform(0.03, 0.12) * h, hls(hue, 0.6, 0.85), r.uniform(0.15, 0.45)
        )
    cr.set_line_width(max(1.0, w / 900))
    for _ in range(220):
        x, y, length = r.uniform(0, w), r.uniform(0, h), r.uniform(0.01, 0.05) * h
        cr.set_source_rgba(1, 1, 1, r.uniform(0.05, 0.25))
        cr.move_to(x, y)
        cr.line_to(x - length * 0.15, y + length)
        cr.stroke()


def _draw_dunes(cr, r, w, h, look: Look, night: bool) -> None:
    _sky(cr, w, h, look, 0.55)
    if night:
        _stars(cr, r, w, h, 0.5, 120)
    _glow(cr, w * r.uniform(0.2, 0.8), h * 0.4, h * 0.4, look.accent, 0.5)
    for i in range(5):
        t = i / 4
        color = mix(hls(0.08, 0.62 if not night else 0.25, 0.65), hls(0.05, 0.35 if not night else 0.12, 0.6), t)
        _ridge(cr, r, w, h, h * (0.55 + 0.1 * i), h * 0.12, 0.6, color)


def _draw_abstract(cr, r, w, h, look: Look, night: bool) -> None:
    cr.set_source_rgb(*hls(look.hue, 0.1 if night else 0.85, 0.4))
    cr.rectangle(0, 0, w, h)
    cr.fill()
    for _ in range(9):
        _glow(
            cr,
            r.uniform(-0.1, 1.1) * w,
            r.uniform(-0.1, 1.1) * h,
            r.uniform(0.3, 0.7) * w,
            hls(look.hue + r.uniform(-0.15, 0.15), r.uniform(0.35, 0.7), 0.8),
            r.uniform(0.4, 0.8),
        )


_DRAW = {
    "peaks": _draw_peaks,
    "ocean": _draw_ocean,
    "city": _draw_city,
    "forest": _draw_forest,
    "aurora": _draw_aurora,
    "pond": _draw_pond,
    "blocks": _draw_blocks,
    "rain": _draw_rain,
    "dunes": _draw_dunes,
    "abstract": _draw_abstract,
}
STYLES = tuple(_DRAW)


def look_for(style: str, seed: int, night: bool) -> Look:
    return _look(style, seed, night)


@lru_cache(maxsize=512)
def texture(style: str, seed: int, night: bool, width: int = 480, height: int = 270) -> Gdk.Texture:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    cr = cairo.Context(surface)
    look = _look(style, seed, night)
    _DRAW.get(style, _draw_abstract)(cr, random.Random(seed * 7919 + 13), width, height, look, night)
    # A whisper of vignette makes flat procedural art read as a photograph.
    v = cairo.RadialGradient(width / 2, height / 2, height * 0.3, width / 2, height / 2, width * 0.75)
    v.add_color_stop_rgba(0, 0, 0, 0, 0)
    v.add_color_stop_rgba(1, 0, 0, 0, 0.28)
    cr.set_source(v)
    cr.rectangle(0, 0, width, height)
    cr.fill()
    surface.flush()
    data = GLib.Bytes.new(bytes(surface.get_data()))
    return Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED, data, surface.get_stride())


@lru_cache(maxsize=64)
def mosaic(keys: tuple[tuple[str, int, bool], ...], size: int = 64) -> Gdk.Texture:
    """A 2x2 cover for a playlist, from up to four wallpapers."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    half = size // 2
    picks = list(keys[:4]) or [("abstract", 1, False)]
    while len(picks) < 4:
        picks.append(picks[len(picks) % max(1, len(keys[:4]))])
    for index, (style, seed, night) in enumerate(picks):
        tile = cairo.ImageSurface(cairo.FORMAT_ARGB32, half * 2, half)
        tcr = cairo.Context(tile)
        _DRAW.get(style, _draw_abstract)(
            tcr, random.Random(seed * 7919 + 13), half * 2, half, _look(style, seed, night), night
        )
        x, y = (index % 2) * half, (index // 2) * half
        cr.save()
        cr.rectangle(x, y, half, half)
        cr.clip()
        cr.set_source_surface(tile, x - half // 2, y)
        cr.paint()
        cr.restore()
    surface.flush()
    data = GLib.Bytes.new(bytes(surface.get_data()))
    return Gdk.MemoryTexture.new(size, size, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED, data, surface.get_stride())
