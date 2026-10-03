"""The one boundary between the new interface's widgets and their data.

The shell, the player bar, the Library page and the inspector read everything
they show through an `AppState` and change anything only by calling its named
methods. Two adapters implement it:

* the design prototype's in-memory ``AppState`` (``prototypes/ui-redesign``),
  which simulates a whole desktop over dummy data, and
* `wall_in_one.ui.next.real_state.RealAppState`, which reads the
  application's `Session` and the runtime's own status and writes through
  the application's authoring lane.

So the widgets the app ships are the widgets the prototype's smoke test
drives. Both adapters are checked against these Protocols by mypy.

**Signals.** An adapter is a ``GObject.Object`` with three signals:
``changed(topic)``, ``toast(text, button)`` and ``navigate(key)``. Widgets
listen with ``state.connect("changed", lambda _state, topic: ...)``. Topics
the slice uses: ``now`` (what is on screen), ``playback``, ``library``,
``playlists``, ``displays``, ``system`` (service, power and read-only
notices), ``scope`` (the player bar's display scope), ``settings``,
``theme`` and ``appearance`` (window style, glass dials).

**Optional parts.** `PlaybackControls`, `LibraryEditing` and
`LibraryFolders` are separate Protocols an adapter offers through
``controls``, ``editing`` and ``library_folders``, or not at all (``None``).
Widgets hide or disable what an adapter does not offer, so the real adapter
never has to pretend: in this slice its only writes are Apply, Favorite and
the playback controls. Undo exists only on the editing and folder paths, so
it never shows under an adapter without them.

GTK-free: annotations only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

if TYPE_CHECKING:
    from gi.repository import Gdk

#: An Undo callback, as returned by the editing actions that offer Undo.
Undo = Callable[[], None]
#: A toast's single button: (label, callback).
ToastButton = tuple[str, Callable[[], None]]


class _Unchanged:
    """``set_wallpaper_colors``: leave this field alone (None is a real value)."""

    def __repr__(self) -> str:
        return "UNCHANGED"


UNCHANGED: Final = _Unchanged()

#: The library's kinds, as a wallpaper view names them.
WallpaperKind = Literal["still", "video", "scene"]


# -- view models ---------------------------------------------------------------
# Read-only shapes. Adapters build their own records; widgets read these
# attributes and nothing else.


class WallpaperView(Protocol):
    """One wallpaper in the library, as the cards and the inspector show it."""

    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str: ...

    #: ``still``, ``video`` or ``scene``.
    @property
    def kind(self) -> str: ...

    @property
    def is_moving(self) -> bool: ...

    #: Where it came from: ``Local``, ``Workshop`` or a Store provider's name.
    @property
    def source(self) -> str: ...

    @property
    def folder(self) -> str: ...

    #: ``"3840 x 2160"``, or empty when unknown.
    @property
    def resolution(self) -> str: ...

    @property
    def size(self) -> str: ...

    @property
    def added(self) -> str: ...

    #: ``"0:42"`` for a video whose length is known, else empty.
    @property
    def duration(self) -> str: ...

    @property
    def favorite(self) -> bool: ...

    #: ``adaptive`` (generated from the still), ``palette`` or ``keep``.
    @property
    def color_mode(self) -> str: ...

    #: The adaptive scheme it pins, or None for the app default.
    @property
    def scheme(self) -> str | None: ...

    @property
    def palette(self) -> str | None: ...

    #: ``auto``, ``dark``, ``light`` or ``keep``.
    @property
    def theme_mode(self) -> str: ...

    @property
    def still_note(self) -> str: ...

    #: Why playback skips it, or empty.
    @property
    def problem(self) -> str: ...

    @property
    def tags(self) -> tuple[str, ...]: ...


class PlaylistView(Protocol):
    """A playlist in the sidebar and in the inspector's "In playlists"."""

    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str: ...

    #: Wallpaper ids, in order.
    @property
    def entries(self) -> Sequence[str]: ...

    #: Empty for the user's own; otherwise why it is automatic (a tooltip).
    @property
    def automatic(self) -> str: ...

    @property
    def icon(self) -> str: ...


class DisplayView(Protocol):
    """A display the player bar's scope can name."""

    @property
    def connector(self) -> str: ...

    #: The monitor's model, or empty when unknown.
    @property
    def model(self) -> str: ...


class PaletteView(Protocol):
    """A Noctalia palette, for the inspector's palette list."""

    @property
    def name(self) -> str: ...

    def strip(self, dark: bool) -> list[str]: ...


# -- what is on screen, and why ----------------------------------------------------

#: Why a display shows what it shows: a pick by hand, a schedule rule, the
#: display's own playlist, or the default because nothing else applies.
Route = Literal["pick", "schedule", "display", "default"]
ServiceState = Literal["checking", "running", "stopped"]
PlaybackState = Literal["playing", "paused", "stopped"]


@dataclass(frozen=True, slots=True)
class Reason:
    """Why one display shows its wallpaper, as the runtime reported it."""

    route: Route
    #: The playlist's name. Empty for a pick of a single wallpaper.
    playlist: str = ""
    #: When this ends ("18:00"), only when the runtime reports it.
    until: str = ""


@dataclass(frozen=True, slots=True)
class OnScreen:
    """One display: what it shows and why."""

    connector: str
    #: The wallpaper's id, or empty when the library has no match for it.
    wallpaper: str
    #: What to call what is showing.
    name: str
    reason: Reason


@dataclass(frozen=True, slots=True)
class Player:
    """Everything the player bar shows, for the displays in its scope."""

    service: ServiceState = "checking"
    screens: tuple[OnScreen, ...] = ()
    playback: PlaybackState = "playing"
    #: False while any display in scope plays a pick (offer Resume schedule).
    following_schedule: bool = True
    shuffle: bool = False
    rotate: bool = False
    #: "Next in 12 min", "Not changing", or empty when unknown.
    timing: str = ""
    #: Freshness marks such as "status delayed", shown after the reason.
    notes: tuple[str, ...] = ()
    #: A playback command is in flight; the controls wait for its answer.
    busy: bool = False
    #: Why the playback controls are off right now (the service is not
    #: running, a file is from a newer version), or empty when they work.
    controls_off: str = ""
    #: A renderer stopped on a display in scope; Play retries it.
    retry: bool = False
    #: Why Play cannot restart what is on screen (playback skips it this
    #: session), or empty. Play then leads to the wallpaper instead.
    play_refused: str = ""


def reason_text(reason: Reason) -> str:
    """Say why, e.g. ``Frog day · from schedule until 18:00`` or ``Your pick``.

    "until" appears only when the runtime said when the reason ends.
    """
    if reason.route == "pick":
        return f"{reason.playlist} · your pick" if reason.playlist else "Your pick"
    why = {
        "schedule": "from schedule",
        "display": "this display\u2019s playlist",
        "default": "nothing scheduled",
    }[reason.route]
    text = f"{reason.playlist} · {why}" if reason.playlist else why.capitalize()
    return f"{text} until {reason.until}" if reason.until else text


@dataclass(frozen=True, slots=True)
class Banner:
    """A persistent condition the window shows under its header."""

    title: str
    #: The button's label, or empty for no button.
    button: str = ""
    #: Handed back to `AppState.banner_activated` when the button is pressed.
    action: str = ""


# -- the adapters' surface -----------------------------------------------------------


class PlaybackControls(Protocol):
    """The runtime's transport verbs. Absent: the player bar's controls are off.

    Each acts on the player bar's displays: ``scope`` is ``all`` or a
    connector, and None (or no ``scope`` parameter at all) means
    `AppState.scope`. None of them changes what `AppState.player` says by
    itself; the adapter's next reading of the runtime does. While
    `Player.controls_off` gives a reason, the bar calls none of them.
    """

    def toggle_play(self) -> None:
        """Pause what plays, or play what is paused or stopped (or retry a stopped renderer)."""
        ...

    def stop(self) -> None:
        """Stop the animation and keep its still on screen."""
        ...

    def step(self, direction: int, scope: str | None = None) -> None:
        """The next (1) or previous (-1) wallpaper."""
        ...

    def random(self, scope: str | None = None) -> None: ...

    def resume_schedule(self, scope: str = "all") -> None:
        """Drop a pick by hand and follow the schedule again."""
        ...

    def set_shuffle(self, value: bool, scope: str | None = None) -> None: ...

    def set_rotate(self, value: bool) -> None:
        """Change wallpaper automatically (the runtime's cycle), or not."""
        ...

    def start_service(self) -> None: ...


class LibraryEditing(Protocol):
    """Changing the library beyond Apply and Favorite. Absent: those controls are hidden."""

    def add_to_playlist(self, pid: str, wids: list[str]) -> None: ...

    def favorite_wallpapers(self, wids: list[str]) -> None: ...

    def remove_wallpapers(self, wids: list[str]) -> Undo: ...

    def create_playlist(self, name: str) -> str: ...

    def retry_wallpaper(self, wid: str) -> None: ...

    def set_wallpaper_colors(
        self,
        wid: str,
        *,
        mode: str | None = None,
        scheme: object = UNCHANGED,
        palette: str | None = None,
        theme_mode: str | None = None,
    ) -> None: ...

    def schemes(self) -> list[tuple[str, str, str]]: ...

    def scheme_swatches(
        self, wallpaper: WallpaperView, scheme: str | None, dark: bool = True
    ) -> list[str]: ...

    def palettes(self, origin: str | None = None) -> Sequence[PaletteView]: ...


class LibraryFolders(Protocol):
    """Adding a library folder. Absent: the Library's "Add a folder" is hidden."""

    def add_library_folder(self, path: str) -> Undo:
        """Make ``path`` a library folder and scan it."""
        ...


class AppState(Protocol):
    """What the new interface's shell, player bar, Library and inspector use."""

    # -- signals: changed(topic), toast(text, button), navigate(key) ------------

    def connect(self, detailed_signal: str, handler: Callable[..., Any], *args: Any) -> int: ...

    def disconnect(self, id: int) -> None: ...

    def toast(
        self,
        text: str,
        undo: Undo | None = None,
        action: ToastButton | None = None,
    ) -> None: ...

    def navigate(self, page: str) -> None: ...

    # -- the library --------------------------------------------------------------

    @property
    def wallpapers(self) -> Sequence[WallpaperView]: ...

    def wallpaper(self, wid: str) -> WallpaperView: ...

    def has_wallpaper(self, wid: str) -> bool: ...

    def library_sorts(self) -> Sequence[tuple[str, str]]: ...

    def library_query(
        self, *, kind: str, favorites: bool, text: str, sort: str
    ) -> Sequence[WallpaperView]:
        """Every wallpaper that passes, in ``sort`` order (``kind`` "all" = any)."""
        ...

    @property
    def library_scanning(self) -> bool: ...

    # -- playlists ----------------------------------------------------------------

    @property
    def playlists(self) -> Sequence[PlaylistView]: ...

    def playlist(self, pid: str) -> PlaylistView: ...

    def has_playlist(self, pid: str) -> bool: ...

    def playlist_cover(self, pid: str, size: int) -> Gdk.Paintable | None: ...

    # -- displays and what is on screen ---------------------------------------------

    @property
    def displays(self) -> Sequence[DisplayView]: ...

    #: ``mirrored`` or ``independent``.
    @property
    def display_mode(self) -> str: ...

    #: The player bar's scope: ``all`` or a connector.
    @property
    def scope(self) -> str: ...

    def set_scope(self, scope: str) -> None: ...

    def scope_label(self, scope: str | None = None) -> str: ...

    def targets(self, scope: str | None = None) -> list[str]: ...

    #: Connector -> id of the wallpaper on that display.
    @property
    def current(self) -> Mapping[str, str]: ...

    def player(self) -> Player: ...

    def backdrop(self) -> object | None:
        """What the frosted style blurs: a ``Gdk.Texture`` or a thumbnail source."""
        ...

    # -- persistent conditions ----------------------------------------------------

    def banner(self) -> Banner | None: ...

    def banner_activated(self, action: str) -> None: ...

    def read_only_notice(self) -> str:
        """Why some changes are off (files from a newer version, unknown settings)."""
        ...

    # -- the writes every adapter offers: Apply and Favorite --------------------------

    def apply(self, wid: str, scope: str = "all") -> None:
        """Apply on the player bar's ``scope`` ("all" or a connector).

        A connector that is mirrored, or gone, means every display: the
        scope's documented fallback.
        """
        ...

    def apply_only(self, wid: str, connector: str) -> None:
        """Apply on ``connector`` alone, as a menu's "<connector> only" asks.

        Never widened: when ``connector`` is not among `solo_displays` (it went
        away, or the displays are mirrored), nothing is sent and a notice
        (`not_on_its_own`) says so.
        """
        ...

    def apply_blocked(self, wid: str) -> str:
        """Why ``wid`` cannot be applied now, or empty when it can."""
        ...

    def toggle_favorite(self, wid: str) -> None: ...

    def favorite_blocked(self) -> str:
        """Why favorites cannot change now, or empty when they can."""
        ...

    # -- colors, read-only ----------------------------------------------------------

    @property
    def default_scheme(self) -> str: ...

    def scheme_name(self, key: str) -> str: ...

    def wallpaper_swatches(self, wallpaper: WallpaperView, dark: bool = True) -> list[str]: ...

    # -- this window's look (ui.toml in the app) -----------------------------------

    #: ``solid``, ``translucent`` or ``frosted``.
    @property
    def window_style(self) -> str: ...

    @property
    def background_alpha(self) -> float: ...

    @property
    def panel_alpha(self) -> float: ...

    @property
    def frost(self) -> float: ...

    #: ``large`` or ``small``.
    @property
    def thumbnail_size(self) -> str: ...

    def set_window_style(self, style: str) -> None: ...

    def set_background_opacity(self, value: float) -> None: ...

    def set_panel_opacity(self, value: float) -> None: ...

    def set_frost(self, value: float) -> None: ...

    def set_thumbnail_size(self, size: str) -> None: ...

    def appearance_blocked(self) -> str:
        """Why the window's look cannot be saved, or empty when it can."""
        ...

    # -- optional parts ---------------------------------------------------------------

    @property
    def controls(self) -> PlaybackControls | None: ...

    @property
    def editing(self) -> LibraryEditing | None: ...

    @property
    def library_folders(self) -> LibraryFolders | None: ...


def solo_displays(state: AppState) -> list[DisplayView]:
    """The displays a menu may offer as "<connector> only": none while they are mirrored."""
    return [] if state.display_mode == "mirrored" else list(state.displays)


def not_on_its_own(connector: str) -> str:
    """The notice when an explicit one-display target is no longer one."""
    return f"{connector} is no longer controlled on its own, so nothing was sent"
