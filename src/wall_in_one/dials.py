"""The renderer dials: one typed, bounded table for every dial the app exposes.

linux-wallpaperengine's and mpvpaper's own options, as far as they make sense
on a wallpaper (docs/renderer-settings.md). The same table serves the
global defaults in Settings, a wallpaper's own overrides in its pairing,
the compiler that writes both into ``runtime.toml``, and both interfaces.

A value is only ever one of these types, checked against these bounds,
and written by the compiler from that type: mpvpaper hands ``-o`` to mpv as a
config file with every space a new line, so nothing here is text a person or
a Workshop item typed. The service checks the same bounds again
(``service/src/dials.rs``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

#: Which renderer a dial belongs to.
Renderer = Literal["video", "scene"]

#: A dial's value: a choice's value, a number or a switch.
Value = str | int | float | bool


@dataclass(frozen=True, slots=True)
class Dial:
    """One dial: its type, its bounds, and where it lives."""

    renderer: Renderer
    #: Its name in a wallpaper's own table (``runtime.toml``'s ``video`` or
    #: ``scene``, ``pairings.json``'s ``renderer``).
    key: str
    #: ``choice``, ``int``, ``float`` or ``bool``.
    kind: Literal["choice", "int", "float", "bool"]
    label: str
    #: Its default, as the renderer would have it with nothing set.
    default: Value
    help: str = ""
    #: (value, label) for a choice.
    choices: tuple[tuple[Value, str], ...] = ()
    minimum: float = 0
    maximum: float = 0
    step: float = 1
    #: The `config.Settings` field and ``[renderer]`` key of its global
    #: default, or empty for one that is per wallpaper only.
    setting: str = ""
    #: Whether a wallpaper may override it.
    per_wallpaper: bool = True
    #: A ``[renderer]`` key the 0.4.0 service already reads: written always
    #: (its value then needs no newer schema). Empty for a dial added with
    #: schema 7, written only when it isn't its default.
    released: bool = False

    def valid(self, value: object) -> bool:
        """Whether ``value`` is one this dial takes."""
        if self.kind == "bool":
            return isinstance(value, bool)
        if self.kind == "choice":
            return any(
                value == choice and type(value) is type(choice) for choice, _ in self.choices
            )
        if isinstance(value, bool):
            return False
        if self.kind == "int":
            # Compared as an integer: JSON and TOML integers can have any number
            # of digits, and turning one that size into a float raises.
            return isinstance(value, int) and self.minimum <= value <= self.maximum
        if isinstance(value, float):
            return math.isfinite(value) and self.minimum <= value <= self.maximum
        return False

    def coerce(self, value: object) -> Value | None:
        """``value`` as this dial's type when it is one (an int for a float dial), else ``None``."""
        if (
            self.kind == "float"
            and isinstance(value, int)
            and not isinstance(value, bool)
            and self.minimum <= value <= self.maximum
        ):
            value = float(value)
        return value if self.valid(value) else None  # type: ignore[return-value]

    def label_of(self, value: Value) -> str:
        """``value`` for a person: a choice's label, a number as written."""
        for choice, label in self.choices:
            if choice == value:
                return label
        if self.kind == "bool":
            return "On" if value else "Off"
        if self.kind == "float":
            return f"{float(value):.2f}"
        return str(value)


_FIT: Final = (
    ("fill", "Fill the screen (crop)"),
    ("fit", "Fit inside (bars)"),
    ("stretch", "Stretch"),
)
_ROTATIONS: Final = ((0, "None"), (90, "90°"), (180, "180°"), (270, "270°"))
_SCALE: Final = (
    ("", "mpv's default"),
    ("bilinear", "Bilinear"),
    ("spline36", "Spline36"),
    ("lanczos", "Lanczos"),
    ("ewa_lanczos", "EWA Lanczos"),
    ("mitchell", "Mitchell"),
)
_HIDDEN_TRIGGER: Final = (
    ("", "Only when it is hidden"),
    ("full", "Also behind a fullscreen window"),
    ("max", "Also behind a fullscreen or maximized window"),
)
_SCENE_SCALING: Final = (
    ("", "Engine default"),
    ("stretch", "Stretch"),
    ("fit", "Fit"),
    ("fill", "Fill"),
)
_SCENE_CLAMP: Final = (
    ("", "Engine default"),
    ("clamp", "Clamp"),
    ("border", "Border"),
    ("repeat", "Repeat"),
)


def _equalizer(key: str, label: str) -> Dial:
    return Dial(
        "video",
        key,
        "int",
        label,
        0,
        minimum=-100,
        maximum=100,
        setting=f"video_{key}",
    )


#: Every dial, in the order the interfaces show them.
DIALS: Final[tuple[Dial, ...]] = (
    Dial("video", "fit", "choice", "Fit", "fill", choices=_FIT, setting="video_fit"),
    Dial(
        "video",
        "speed",
        "float",
        "Speed",
        1.0,
        "Playback speed; 1.00 is as recorded",
        minimum=0.25,
        maximum=4.0,
        step=0.05,
        setting="video_speed",
    ),
    _equalizer("brightness", "Brightness"),
    _equalizer("contrast", "Contrast"),
    _equalizer("saturation", "Saturation"),
    _equalizer("gamma", "Gamma"),
    Dial("video", "rotate", "choice", "Rotation", 0, choices=_ROTATIONS, setting="video_rotate"),
    Dial(
        "video",
        "deband",
        "bool",
        "Deband",
        False,
        "Smooth out banding in gradients, at some GPU cost",
        setting="video_deband",
    ),
    Dial(
        "video",
        "scale",
        "choice",
        "Scaling filter",
        "",
        "How mpv enlarges a video smaller than the screen",
        choices=_SCALE,
        setting="video_scale",
        per_wallpaper=False,
    ),
    Dial(
        "video",
        "hidden_trigger",
        "choice",
        "Pause or stop it",
        "",
        "Whether a window covering the screen counts as hidden too",
        choices=_HIDDEN_TRIGGER,
        setting="video_hidden_trigger",
        per_wallpaper=False,
    ),
    Dial(
        "scene",
        "fps",
        "int",
        "Frame rate",
        30,
        minimum=1,
        maximum=240,
        setting="scene_fps",
        released=True,
    ),
    Dial(
        "scene",
        "scaling",
        "choice",
        "Scaling",
        "",
        choices=_SCENE_SCALING,
        setting="scene_scaling",
        released=True,
    ),
    Dial(
        "scene",
        "clamp",
        "choice",
        "Texture edges",
        "",
        choices=_SCENE_CLAMP,
        setting="scene_clamp",
        released=True,
    ),
    Dial(
        "scene",
        "muted",
        "bool",
        "Silent",
        True,
        "Sound is set when the scene starts, so changing it restarts the scene",
        setting="scene_muted",
        released=True,
    ),
    Dial(
        "scene",
        "volume",
        "int",
        "Volume",
        50,
        "Used while it isn't silent",
        minimum=0,
        maximum=100,
        setting="scene_volume",
        released=True,
    ),
    Dial(
        "scene",
        "audio_processing",
        "bool",
        "React to sound",
        True,
        "The engine records what the computer plays so the scene can move to it",
        setting="scene_audio_processing",
    ),
    Dial("scene", "particles", "bool", "Particles", True, setting="scene_particles"),
    Dial(
        "scene",
        "mouse",
        "bool",
        "Follow the mouse",
        True,
        "Mouse interaction with the scene",
        setting="scene_mouse",
    ),
    Dial("scene", "parallax", "bool", "Parallax", True, setting="scene_parallax"),
    Dial(
        "scene",
        "pause_when_covered",
        "bool",
        "Pause behind a fullscreen window",
        True,
        setting="scene_pause_when_covered",
        per_wallpaper=False,
        released=True,
    ),
    Dial(
        "scene",
        "pause_only_active",
        "bool",
        "Only when that window is the active one",
        False,
        setting="scene_pause_only_active",
        per_wallpaper=False,
    ),
    Dial(
        "scene",
        "automute",
        "bool",
        "Silent while other apps play sound",
        True,
        setting="scene_automute",
        per_wallpaper=False,
    ),
)

_BY_KEY: Final[dict[tuple[str, str], Dial]] = {(dial.renderer, dial.key): dial for dial in DIALS}

#: The dials a wallpaper may override, by renderer.
PER_WALLPAPER: Final[Mapping[str, tuple[Dial, ...]]] = {
    renderer: tuple(dial for dial in DIALS if dial.renderer == renderer and dial.per_wallpaper)
    for renderer in ("video", "scene")
}

#: The dials with a global default, in Settings.
GLOBAL: Final[tuple[Dial, ...]] = tuple(dial for dial in DIALS if dial.setting)


def dial(renderer: str, key: str) -> Dial | None:
    """The dial ``key`` of ``renderer``, or ``None`` for one that doesn't exist."""
    return _BY_KEY.get((renderer, key))


def overrides(renderer: str, raw: object) -> tuple[tuple[str, Value], ...]:
    """The valid per-wallpaper overrides in ``raw``, by key in table order.

    Anything else is left out: an unknown key, one that isn't a wallpaper's
    to set, a value of the wrong type or out of bounds. Never raises.
    """
    if not isinstance(raw, Mapping):
        return ()
    kept: list[tuple[str, Value]] = []
    for known in PER_WALLPAPER.get(renderer, ()):
        if known.key in raw:
            value = known.coerce(raw[known.key])
            if value is not None:
                kept.append((known.key, value))
    return tuple(kept)


#: The Wallpaper Engine property types a person may set on a scene of
#: theirs. Never ``file``, ``directory`` or ``scenetexture``: a path, from a
#: file anyone can write, into the renderer.
PROPERTY_TYPES: Final = ("bool", "slider", "color", "combo", "textinput")


@dataclass(frozen=True, slots=True)
class SceneProperty:
    """One property a scene declares in its ``project.json``, that a person may set.

    Values are the text the engine reads (``--set-property NAME=VALUE``):
    ``true``/``false``, a number, ``r g b`` in 0-1, a combo option's value,
    or a line of text. Built by `library.workshop` from the declaration;
    shown by both interfaces as one control each.
    """

    name: str
    #: One of `PROPERTY_TYPES`.
    type: str
    #: The declaration's ``text``, as written (sometimes a translation key),
    #: or the name when it has none. Plain text, never markup.
    label: str
    #: The declared value, as the engine reads it.
    default: str
    #: A slider's range and step.
    minimum: float = 0.0
    maximum: float = 0.0
    step: float = 0.0
    #: A combo's options, (value, label), in the order declared.
    options: tuple[tuple[str, str], ...] = ()


def toml_value(value: Value) -> str:
    """``value`` as a TOML literal: never a string this module didn't choose."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.2f}"
    return '"' + value + '"'
