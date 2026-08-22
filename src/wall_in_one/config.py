"""Application settings, persisted as TOML.

Deliberately small and flat. Anything Noctalia already owns -- the palette, the
theme mode, the wallpaper directory -- is read from Noctalia rather than
duplicated here, so there is only ever one source of truth.
"""

from __future__ import annotations

import contextlib
import json
import math
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final, Self

from wall_in_one import file_io, paths
from wall_in_one.library import state_file
from wall_in_one.theme.noctalia import ALL_SCHEMES, DEFAULT_SCHEME
from wall_in_one.wallpaper import renderer, scenes

#: Below this the window stops being legible against a busy wallpaper, and the
#: compositor's blur cannot rescue it.
MIN_OPACITY: Final = 0.30
MAX_SETTINGS_BYTES: Final = 1024 * 1024

# These are part of the versioned Python -> Rust configuration wire contract,
# not arbitrary UI limits. Keep them aligned with ``service/src/config.rs`` so
# systemd's headless compiler refuses a value *before* replacing the last-known
# good runtime document with one the service cannot load.
MAX_RUNTIME_PATH_BYTES: Final = 4096
MAX_RUNTIME_REFERENCE_BYTES: Final = 120 * 4
MAX_RUNTIME_CONNECTOR_BYTES: Final = 256


class ConfigError(Exception):
    """The settings file could not be read or was malformed."""


def _tidy_roots(roots: Sequence[Path]) -> tuple[Path, ...]:
    """Absolute, `~`-expanded, in order, with the duplicates dropped.

    Scanning the same directory twice would put every wallpaper in it into the
    rotation twice, and an entry that only *looks* different -- `~/Pictures`
    against `/home/you/Pictures` -- is exactly the duplicate a person would add
    by accident. Order is kept because the first root is where downloads and
    generated stills land, which makes it the user's choice rather than ours.
    """
    seen: dict[Path, None] = {}
    for root in roots:
        expanded = Path(root).expanduser()
        with contextlib.suppress(OSError):
            expanded = expanded.absolute()
        if str(expanded):
            seen.setdefault(expanded, None)
    return tuple(seen)


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

    #: A wallpaper that makes noise is a surprise, so silence is the default.
    #: The audio track is still loaded rather than disabled, which is what lets
    #: this be undone live instead of only at the next video.
    video_muted: bool = True

    #: 0 to `renderer.MAX_VOLUME`. Kept while muted, so unmuting lands at the
    #: level the user chose rather than at whatever mpv would have picked.
    video_volume: int = 100

    #: What to do with a video no one can see because a window covers it. One
    #: of `renderer.WHEN_HIDDEN_CHOICES`. mpvpaper warns its auto options
    #: "might not work as intended", so `play` stays reachable.
    video_when_hidden: str = renderer.DEFAULT_WHEN_HIDDEN

    #: Use mpv's display-resample presentation and temporal interpolation for
    #: low-frame-rate video. This changes presentation cadence, not decode FPS.
    video_interpolation: str = renderer.DEFAULT_INTERPOLATION

    #: ``auto`` lets mpv use a hardware decoder; false is a useful diagnostic
    #: escape hatch for driver artifacts and forces software decoding.
    video_hardware_decode: bool = True

    #: Native linux-wallpaperengine rendering limit. Video FPS is deliberately
    #: left alone: mpv's post-decode FPS filter does not reduce decode work.
    scene_fps: int = scenes.DEFAULT_FPS

    #: Which output the wallpaper is applied to. Empty means every one of
    #: them, which is what Noctalia's `wallpaper-set` does with no connector
    #: and what mpvpaper's `ALL` does for videos.
    output: str = ""

    #: Let this app start `linux-wallpaperengine` for Workshop scenes.
    #:
    #: On by default so selecting a scene works without hand-editing this file.
    #: The renderer still refuses to start when another process owns the target
    #: output, so the default does not turn coexistence into a fight. Scene
    #: *stills* are captured in a window and touch no output either way.
    own_scene_renderer: bool = True

    #: Include Wallpaper Engine wallpapers installed through Steam. On by
    #: default because finding them costs one directory listing and somebody
    #: with none simply has none; off is for anyone who keeps the two
    #: collections deliberately apart.
    scan_workshop: bool = True

    #: Default named playlist when no schedule rule matches. Empty selects the
    #: built-in all-media playlist. On-demand choices are runtime overrides and
    #: deliberately do not overwrite this calendar fallback.
    active_playlist: str = ""

    #: Narrow the rotation to the starred wallpapers. Ignored when that would
    #: leave nothing to rotate through -- see `session._rotation`.
    cycle_favourites_only: bool = False

    #: Directories to scan for wallpapers. Empty means "whatever
    #: `library.scan.default_roots` decides", which follows Noctalia's own
    #: `wallpaper.directory`. That is the right default and the wrong thing to
    #: be stuck with: Noctalia has exactly one, so a library spread across two
    #: places was previously half invisible with no way to say so.
    roots: tuple[Path, ...] = ()

    def validated(self) -> Self:
        """Clamp and correct anything out of range rather than failing.

        A bad settings file should degrade to something usable, not stop the
        app from starting.
        """
        opacity = min(1.0, max(MIN_OPACITY, self.opacity))
        scheme = self.preview_scheme if self.preview_scheme in ALL_SCHEMES else DEFAULT_SCHEME
        interval = min(24 * 60 * 60, max(5, self.cycle_interval))
        hidden = (
            self.video_when_hidden
            if self.video_when_hidden in renderer.WHEN_HIDDEN_CHOICES
            else renderer.DEFAULT_WHEN_HIDDEN
        )
        interpolation = (
            self.video_interpolation
            if self.video_interpolation in renderer.INTERPOLATION_CHOICES
            else renderer.DEFAULT_INTERPOLATION
        )
        return replace(
            self,
            opacity=opacity,
            preview_scheme=scheme,
            cycle_interval=interval,
            video_volume=min(renderer.MAX_VOLUME, max(0, self.video_volume)),
            video_when_hidden=hidden,
            video_interpolation=interpolation,
            scene_fps=min(scenes.MAX_FPS, max(scenes.MIN_FPS, self.scene_fps)),
            roots=_tidy_roots(self.roots),
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

        def directories(key: str) -> tuple[Path, ...]:
            value = raw.get(key)
            if not isinstance(value, list):
                return ()
            # Each entry is checked on its own: one bad line in a hand-edited
            # file should cost that line, not the whole list.
            return tuple(Path(entry) for entry in value if isinstance(entry, str) and entry.strip())

        return cls(
            opacity=number("opacity", 1.0),
            preview_scheme=text("preview_scheme", DEFAULT_SCHEME),
            follow_noctalia_palette=boolean("follow_noctalia_palette", True),
            cycle_interval=int(number("cycle_interval", 300)),
            cycle_enabled=boolean("cycle_enabled", False),
            shuffle=boolean("shuffle", False),
            dynamics_enabled=boolean("dynamics_enabled", True),
            video_muted=boolean("video_muted", True),
            video_volume=int(number("video_volume", 100)),
            video_when_hidden=text("video_when_hidden", renderer.DEFAULT_WHEN_HIDDEN),
            video_interpolation=text("video_interpolation", renderer.DEFAULT_INTERPOLATION),
            video_hardware_decode=boolean("video_hardware_decode", True),
            scene_fps=int(number("scene_fps", scenes.DEFAULT_FPS)),
            cycle_favourites_only=boolean("cycle_favourites_only", False),
            active_playlist=text("active_playlist", ""),
            scan_workshop=boolean("scan_workshop", True),
            own_scene_renderer=boolean("own_scene_renderer", True),
            output=text("output", ""),
            roots=directories("roots"),
        ).validated()

    def to_toml(self) -> str:
        lines = (
            "# wall-in-one settings",
            "",
            _roots_line(self.roots),
            f"opacity = {self.opacity:.2f}",
            f'preview_scheme = "{self.preview_scheme}"',
            f"follow_noctalia_palette = {str(self.follow_noctalia_palette).lower()}",
            f"cycle_interval = {self.cycle_interval}",
            f"cycle_enabled = {str(self.cycle_enabled).lower()}",
            f"shuffle = {str(self.shuffle).lower()}",
            f"dynamics_enabled = {str(self.dynamics_enabled).lower()}",
            f"video_muted = {str(self.video_muted).lower()}",
            f"video_volume = {self.video_volume}",
            f'video_when_hidden = "{self.video_when_hidden}"',
            f'video_interpolation = "{self.video_interpolation}"',
            f"video_hardware_decode = {str(self.video_hardware_decode).lower()}",
            f"scene_fps = {self.scene_fps}",
            f"cycle_favourites_only = {str(self.cycle_favourites_only).lower()}",
            f'active_playlist = "{self.active_playlist}"',
            f"scan_workshop = {str(self.scan_workshop).lower()}",
            f"own_scene_renderer = {str(self.own_scene_renderer).lower()}",
            f'output = "{self.output}"',
        )
        return "\n".join(lines) + "\n"


def _roots_line(roots: Sequence[Path]) -> str:
    """The `roots` array, written so a person can edit it by hand.

    Empty is written as an empty array with the default spelled out beside it,
    rather than omitted: a setting nobody can see is a setting nobody knows
    they have.
    """
    if not roots:
        return "# empty follows Noctalia's own wallpaper directory\nroots = []"
    # A TOML basic string takes the same escapes a JSON string does, which is
    # what keeps a directory with a quote or a backslash in its name writable.
    inner = ", ".join(json.dumps(str(root)) for root in roots)
    return f"roots = [{inner}]"


def load(path: Path | None = None) -> Settings:
    """Read settings, falling back to defaults when absent or unreadable."""
    target = path if path is not None else paths.settings_path()
    try:
        document = file_io.read_regular_bytes(target, MAX_SETTINGS_BYTES)
        if document is None:
            return Settings()
        raw = tomllib.loads(document.decode("utf-8"))
    except OSError, UnicodeDecodeError, tomllib.TOMLDecodeError:
        # A corrupt settings file should not be fatal; defaults are always
        # usable and the user can fix or delete the file.
        return Settings()
    return Settings.from_mapping(raw)


def load_strict(path: Path | None = None) -> Settings:
    """Read settings for unattended compilation, rejecting unreadable bytes.

    The interactive application deliberately recovers from a damaged file so
    somebody can still reach Settings and repair it. The systemd
    ``ExecStartPre`` path is different: silently compiling defaults from a
    typo would replace the user's intended library and renderer configuration
    just before starting automation. Missing remains a valid first-run state;
    a present document must parse.
    """
    target = path if path is not None else paths.settings_path()
    try:
        document = file_io.read_regular_bytes(target, MAX_SETTINGS_BYTES)
        if document is None:
            return Settings()
        raw = tomllib.loads(document.decode("utf-8"))
    except OSError as error:
        raise ConfigError(f"cannot read {target}: {error}") from error
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot parse {target}: {error}") from error
    _validate_strict_mapping(raw, target)
    return Settings.from_mapping(raw)


def _validate_strict_mapping(raw: dict[str, Any], target: Path) -> None:
    """Reject values the interactive loader would otherwise repair silently.

    ``load`` is intentionally forgiving because it has a Settings screen a
    person can use to recover.  The headless compiler has no such interaction:
    substituting defaults there can atomically publish a valid but unintended
    runtime document.  Keep the two policies separate and make every known
    unattended value unambiguous before calling the shared mapper.
    """

    known = frozenset(Settings.__dataclass_fields__)
    unknown = sorted(set(raw) - known)
    if unknown:
        joined = ", ".join(unknown)
        raise ConfigError(f"cannot use {target}: unknown setting(s): {joined}")

    booleans = (
        "follow_noctalia_palette",
        "cycle_enabled",
        "shuffle",
        "dynamics_enabled",
        "video_muted",
        "video_hardware_decode",
        "cycle_favourites_only",
        "scan_workshop",
        "own_scene_renderer",
    )
    for key in booleans:
        if key in raw and not isinstance(raw[key], bool):
            raise ConfigError(f"cannot use {target}: {key} must be a boolean")

    integer_ranges = {
        "cycle_interval": (5, 24 * 60 * 60),
        "video_volume": (0, renderer.MAX_VOLUME),
        "scene_fps": (scenes.MIN_FPS, scenes.MAX_FPS),
    }
    for key, (minimum, maximum) in integer_ranges.items():
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(f"cannot use {target}: {key} must be an integer")
        if not minimum <= value <= maximum:
            raise ConfigError(f"cannot use {target}: {key} must be between {minimum} and {maximum}")

    if "opacity" in raw:
        opacity = raw["opacity"]
        if (
            not isinstance(opacity, (int, float))
            or isinstance(opacity, bool)
            or not math.isfinite(opacity)
        ):
            raise ConfigError(f"cannot use {target}: opacity must be a finite number")
        if not MIN_OPACITY <= opacity <= 1.0:
            raise ConfigError(
                f"cannot use {target}: opacity must be between {MIN_OPACITY:.2f} and 1.0"
            )

    strings = (
        "preview_scheme",
        "video_when_hidden",
        "video_interpolation",
        "active_playlist",
        "output",
    )
    for key in strings:
        if key in raw and not isinstance(raw[key], str):
            raise ConfigError(f"cannot use {target}: {key} must be a string")

    choices = {
        "preview_scheme": ALL_SCHEMES,
        "video_when_hidden": renderer.WHEN_HIDDEN_CHOICES,
        "video_interpolation": renderer.INTERPOLATION_CHOICES,
    }
    for key, allowed in choices.items():
        if key in raw and raw[key] not in allowed:
            choices_text = ", ".join(allowed)
            raise ConfigError(f"cannot use {target}: {key} must be one of {choices_text}")

    for key, maximum in (
        ("active_playlist", MAX_RUNTIME_REFERENCE_BYTES),
        ("output", MAX_RUNTIME_CONNECTOR_BYTES),
    ):
        if key not in raw:
            continue
        value = raw[key]
        assert isinstance(value, str)
        _validate_runtime_text(value, maximum, key=key, target=target, optional=True)
        if value and value != value.strip():
            raise ConfigError(
                f"cannot use {target}: {key} cannot have leading or trailing whitespace"
            )

    if "roots" in raw:
        roots = raw["roots"]
        if not isinstance(roots, list) or any(
            not isinstance(entry, str) or not entry.strip() for entry in roots
        ):
            raise ConfigError(f"cannot use {target}: roots must be an array of non-empty strings")
        assert isinstance(roots, list)
        for index, root in enumerate(roots):
            assert isinstance(root, str)
            expanded = str(Path(root).expanduser().absolute())
            _validate_runtime_text(
                expanded,
                MAX_RUNTIME_PATH_BYTES,
                key=f"roots[{index}]",
                target=target,
            )


def _validate_runtime_text(
    value: str,
    maximum_bytes: int,
    *,
    key: str,
    target: Path,
    optional: bool = False,
) -> None:
    """Apply Rust's UTF-8 byte and control-character bounds to one value."""
    if not value and optional:
        return
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ConfigError(f"cannot use {target}: {key} must be valid UTF-8 text") from error
    if len(encoded) > maximum_bytes:
        raise ConfigError(f"cannot use {target}: {key} must be at most {maximum_bytes} UTF-8 bytes")
    if any(ord(character) < 32 or 0x7F <= ord(character) <= 0x9F for character in value):
        raise ConfigError(f"cannot use {target}: {key} cannot contain control characters")


def save(settings: Settings, path: Path | None = None) -> Path:
    target = path if path is not None else paths.settings_path()
    try:
        paths.ensure_directory(target.parent)
        state_file.write_atomic_text(target, settings.validated().to_toml())
    except OSError as error:
        raise ConfigError(f"cannot write {target}: {error}") from error
    return target
