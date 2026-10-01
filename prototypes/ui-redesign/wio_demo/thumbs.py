"""Thumbnails: the one place pages and widgets get pictures of wallpapers and
Store items from. The demo draws procedural art; a real-app adapter would
decode files here instead."""

from __future__ import annotations

import gi

gi.require_version("Gdk", "4.0")
from gi.repository import Gdk

from . import art


def texture(source, width: int = 480, height: int = 270) -> Gdk.Texture:
    """A picture of ``source`` (anything with an art ``key``), drawn now and cached."""
    return art.texture(*source.key, width, height)
