"""What the new interface's Settings page needs from the app.

The app's settings, its library folders, the Wallhaven key, its own palette
and Noctalia's palettes. `AppState.settings_access` returns a
`SettingsAccess`, or None when an adapter can't offer it; the page then says
Settings is not in the new interface yet. The real one lives beside the
adapter in ``real_settings.py``; the prototype's demo state is the other.

**Settings.** Each row reads one ``settings.toml`` key (`SettingsAccess.setting`)
and saves through `SettingsAccess.change_settings`, which in the app is the
classic window's validated path: coalesced on the authoring lane, rolled back
on failure. What a row shows after a save is what the adapter reports once
it is durable (the ``settings`` topic); a failed save puts every row back.
No new key: every row saves a key the classic Settings page saves.

**Topics.** ``settings`` (any value, the read-only state, folders, the key's
status, the battery and colour-source notes), ``theme`` (the app's palette)
and ``palettes`` (the palette catalogue, the scheme previews).

Choices and ranges come from the modules that validate them, so a row can
never offer what a save would refuse.

GTK-free: annotations, records and constants only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from wall_in_one import config
from wall_in_one.theme.noctalia import ALL_SCHEMES
from wall_in_one.wallpaper import renderer, scenes

#: (value, label) for a choice row.
Choices = tuple[tuple[str, str], ...]


def scheme_label(key: str) -> str:
    """``m3-tonal-spot`` -> ``Tonal Spot``."""
    return key.removeprefix("m3-").replace("-", " ").title()


# -- what each row may offer ----------------------------------------------------

#: mpvpaper's vocabulary is `--auto-pause` and `--auto-stop`; these say what you get.
WHEN_HIDDEN: Final[Choices] = tuple(
    (
        key,
        {
            "pause": "Pause (resumes instantly)",
            "stop": "Stop (frees memory too)",
            "play": "Keep playing",
        }[key],
    )
    for key in renderer.WHEN_HIDDEN_CHOICES
)
INTERPOLATION: Final[Choices] = tuple(
    (
        key,
        {
            "off": "Off (source cadence)",
            "oversample": "Oversample (recommended)",
            "linear": "Linear (stronger blending)",
        }[key],
    )
    for key in renderer.INTERPOLATION_CHOICES
)
SCENE_SCALING: Final[Choices] = tuple(
    (
        key,
        {
            "": "Renderer default",
            "stretch": "Stretch (ignore aspect ratio)",
            "fit": "Fit (show the whole scene)",
            "fill": "Fill (crop to the display)",
        }[key],
    )
    for key in scenes.SCALING_CHOICES
)
SCENE_CLAMP: Final[Choices] = tuple(
    (
        key,
        {
            "": "Renderer default",
            "clamp": "Clamp to edge",
            "border": "Border colour outside the scene",
            "repeat": "Repeat texture outside the scene",
        }[key],
    )
    for key in scenes.CLAMP_CHOICES
)
DISPLAY_MODES: Final[Choices] = (
    (config.DISPLAY_MODE_MIRRORED, "Same wallpaper on every display"),
    (config.DISPLAY_MODE_INDEPENDENT, "Control displays independently"),
)
SCHEMES: Final[Choices] = tuple((key, scheme_label(key)) for key in ALL_SCHEMES)

#: (lowest, highest) for the number rows, as `config.Settings.validated` clamps them.
INTERVAL_RANGE: Final = (5, 24 * 60 * 60)
VOLUME_RANGE: Final = (0, renderer.MAX_VOLUME)
FPS_RANGE: Final = (scenes.MIN_FPS, scenes.MAX_FPS)
OPACITY_RANGE: Final = (config.MIN_OPACITY, 1.0)

#: The fourteen colours a palette file carries, as the editor names them.
PALETTE_ROLES: Final[tuple[tuple[str, str], ...]] = (
    ("primary", "Primary"),
    ("on_primary", "On primary"),
    ("secondary", "Secondary"),
    ("on_secondary", "On secondary"),
    ("tertiary", "Tertiary"),
    ("on_tertiary", "On tertiary"),
    ("error", "Error"),
    ("on_error", "On error"),
    ("surface", "Surface"),
    ("on_surface", "On surface"),
    ("surface_variant", "Surface variant"),
    ("on_surface_variant", "On surface variant"),
    ("outline", "Outline"),
    ("shadow", "Shadow"),
)
#: Where a palette comes from, in the order the manager lists them.
PALETTE_ORIGINS: Final[tuple[tuple[str, str, str], ...]] = (
    ("custom", "Yours", "The only palettes this app writes to."),
    ("builtin", "Built-in", "Compiled into Noctalia; their colours show once one is applied."),
    ("community", "Community", "Downloaded by Noctalia and cached on disk."),
    (
        "legacy",
        "Legacy",
        "From an older Noctalia layout this version no longer reads. Duplicate one "
        "to make it applicable again.",
    ),
)
#: Palettes listed per page; "Load more" adds as many, fairly across origins.
PALETTE_PAGE_SIZE: Final = 24

#: How saving a Wallhaven key ended: saved, refused as not a key, or not saved.
KEY_SAVED: Final = "saved"
KEY_REFUSED: Final = "refused"
KEY_FAILED: Final = "failed"


# -- records ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Folder:
    """One library folder. The first receives downloads and generated stills."""

    path: str
    name: str
    #: Not there right now (an unmounted drive): said, never dropped.
    missing: bool = False
    #: Why it shows nothing though it is there (a symbolic link is not
    #: scanned), and what to add instead; empty for an ordinary folder.
    note: str = ""


@dataclass(frozen=True, slots=True)
class KeyStatus:
    """Where the Wallhaven key in force comes from, never the key itself."""

    text: str
    #: A key is saved in the key file, so Remove can offer to delete it.
    stored: bool = False


@dataclass(frozen=True, slots=True)
class ThemeSources:
    """The displays Noctalia's one palette may follow (independent displays only)."""

    #: (connector, attached right now), in the order to offer them.
    connectors: tuple[tuple[str, bool], ...] = ()
    #: The saved connector, or empty.
    selected: str = ""
    #: What the row says about the saved choice and any fallback.
    note: str = ""


@dataclass(frozen=True, slots=True)
class AppPalette:
    """Where this window's colours come from, with a few of them."""

    detail: str
    swatches: tuple[str, ...] = ()


#: The template row's states (`TemplateStatus.state`).
TEMPLATE_STATES: Final = ("checking", "installed", "missing", "stale", "unavailable")


@dataclass(frozen=True, slots=True)
class TemplateStatus:
    """Wall-in-One's palette template in Noctalia's settings, as last read."""

    #: One of `TEMPLATE_STATES`.
    state: str = "checking"
    #: Why, in a sentence.
    detail: str = "Checking Noctalia's settings…"
    #: The button's label (``Install`` or ``Repair``), or empty for no button.
    action: str = ""
    #: Why the button is off right now (an upgrade still running), or empty.
    blocked: str = ""
    #: A press is being carried out.
    busy: bool = False
    #: Installed but not rendered yet: offer Render now, which asks Noctalia to
    #: re-render all of its templates, not only Wall-in-One's.
    can_render: bool = False


@dataclass(frozen=True, slots=True)
class PaletteItem:
    """One palette in the manager."""

    #: Stable within one catalogue: origin, name and file.
    key: str
    name: str
    #: One of `PALETTE_ORIGINS`.
    origin: str
    detail: str = ""
    #: A few colours of each variant; empty while Noctalia keeps them to itself.
    dark: tuple[str, ...] = ()
    light: tuple[str, ...] = ()
    #: Noctalia can apply it (a legacy palette has to be duplicated first).
    can_apply: bool = True
    #: It has a file to copy (a built-in has none).
    can_copy: bool = True
    #: Its file is the user's own and may be edited in place.
    can_edit: bool = False


@dataclass(frozen=True, slots=True)
class PaletteList:
    """The palette catalogue as discovery last left it."""

    #: ``idle``, ``loading``, ``ready`` or ``error``.
    phase: str = "idle"
    items: tuple[PaletteItem, ...] = ()
    #: Files in a palette folder that couldn't be read.
    skipped: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True, slots=True)
class PaletteDraft:
    """What the palette editor starts from."""

    title: str
    name: str
    #: False while editing a palette in place: its name is its file.
    renamable: bool
    #: ``dark``/``light`` -> role (`PALETTE_ROLES`) -> ``#rrggbb``.
    colors: Mapping[str, Mapping[str, str]]
    #: The adapter's own handle on what is being edited; the page passes it back.
    origin: object = None


@dataclass(frozen=True, slots=True)
class SchemeSources:
    """The still wallpapers a scheme can be previewed on."""

    #: (id, label).
    items: tuple[tuple[str, str], ...] = ()
    #: Index of the one to start with.
    selected: int = 0


@dataclass(frozen=True, slots=True)
class SchemePreview:
    """What one scheme makes of one wallpaper, or why it couldn't."""

    source: str
    scheme: str
    dark: tuple[str, ...] = ()
    light: tuple[str, ...] = ()
    error: str = ""


# -- the capability ----------------------------------------------------------------


class PaletteManager(Protocol):
    """Browse, apply, duplicate and edit Noctalia's palettes; preview schemes.

    There is no delete or rename: the app has neither. Work that reads files
    or runs Noctalia happens off the GTK thread; results arrive through the
    ``palettes`` topic or the given callbacks, on GTK.
    """

    def open_palettes(self) -> None:
        """The manager is showing: follow the catalogue (loading it once)."""
        ...

    def close_palettes(self) -> None:
        """The manager closed: stop following, drop scheme previews."""
        ...

    def palette_list(self) -> PaletteList: ...

    def rescan_palettes(self) -> None: ...

    def apply_catalog_palette(self, key: str) -> None:
        """Put the palette on the desktop through Noctalia; a toast says how it went."""
        ...

    def palette_draft(
        self, key: str, *, copy: bool, done: Callable[[PaletteDraft | None], None]
    ) -> None:
        """Read the palette for the editor: as a copy, or in place. None: reported why."""
        ...

    def save_palette_draft(
        self,
        draft: PaletteDraft,
        name: str,
        colors: Mapping[str, Mapping[str, str]],
        done: Callable[[bool], None],
    ) -> None:
        """Save as the user's own palette (or in place); ``done(saved)``."""
        ...

    def scheme_sources(self) -> SchemeSources: ...

    def preview_schemes(self, source: str, deliver: Callable[[SchemePreview], None]) -> None:
        """Every scheme on ``source``; each arrives through ``deliver`` when ready."""
        ...

    def scheme_draft(self, source: str, scheme: str) -> PaletteDraft | None:
        """A finished preview as an editor draft, or None while it isn't ready."""
        ...

    def use_scheme(self, scheme: str, *, sync_noctalia: bool) -> None:
        """Make ``scheme`` the default adaptive scheme; optionally Noctalia's too."""
        ...

    def palette_name_problem(self, draft: PaletteDraft, name: str) -> str:
        """Why ``draft`` can't be saved as ``name``, or empty when it can.

        No two palettes a person can pick share a name: the editor's Save
        waits until this is empty, and the write path refuses a clash too.
        """
        ...


class SettingsAccess(Protocol):
    """The Settings page's view of the app."""

    # -- settings.toml -----------------------------------------------------------

    def setting(self, key: str) -> Any:
        """The value of ``key`` as saved (or being saved); never a refused one."""
        ...

    def change_settings(self, **changes: Any) -> None:
        """Save through the validated path; a failure puts every row back."""
        ...

    def settings_blocked(self) -> str:
        """Why settings can't change here (keys from a newer version), or empty."""
        ...

    # -- library folders ---------------------------------------------------------

    def folder_list(self) -> Sequence[Folder]: ...

    def suggested_folder(self) -> str:
        """A folder found on this machine, offered while there is none. Never added."""
        ...

    def add_folder(self, path: str) -> None: ...

    def remove_folder(self, path: str) -> None: ...

    def make_download_folder(self, path: str) -> None:
        """Move ``path`` to the front: downloads and generated stills go there."""
        ...

    # -- what the runtime and this machine report ----------------------------------

    def battery_note(self) -> str:
        """How the battery setting is doing, or empty to hide the note."""
        ...

    def scenes_available(self) -> bool | None:
        """linux-wallpaperengine is installed; None until that is known."""
        ...

    def folder_check_problem(self) -> str:
        """Why the folders and the renderer couldn't be checked, or empty."""
        ...

    def theme_sources(self) -> ThemeSources: ...

    # -- the Wallhaven key ---------------------------------------------------------

    def key_status(self) -> KeyStatus: ...

    def save_key(self, key: str, done: Callable[[str], None]) -> None:
        """Save ``key``; ``done`` gets `KEY_SAVED`, `KEY_REFUSED` or `KEY_FAILED`.

        Messages never contain the key.
        """
        ...

    def clear_key(self) -> None: ...

    # -- colours -------------------------------------------------------------------

    def app_palette(self) -> AppPalette: ...

    def reload_app_palette(self) -> None:
        """Re-read the colours Noctalia last rendered."""
        ...

    @property
    def palette_manager(self) -> PaletteManager: ...

    # -- Noctalia's palette template -----------------------------------------------

    def template_status(self) -> TemplateStatus:
        """What Noctalia's settings say, read off GTK; ``checking`` until known."""
        ...

    def check_template(self) -> None:
        """Read Noctalia's settings again (never writes)."""
        ...

    def install_template(self) -> None:
        """Install or repair the registration: the row's button, the only writer."""
        ...

    def render_templates(self) -> None:
        """Render now: ask Noctalia to re-render all of its templates (its own press)."""
        ...
