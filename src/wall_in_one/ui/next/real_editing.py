"""The real `LibraryEditing`: the classic window's library edits, for the new inspector.

Every change here is one the classic window (its tile menu, the pairing
editor, the playlists page) already makes, through the same calls:

* **Colors** are the pairing's palette policy (`pairings.PalettePolicy`):
  "From wallpaper" is ``adaptive`` (the Settings scheme) or
  ``adaptive:<scheme>`` (pinned for this wallpaper), "Palette" is
  ``builtin|community|custom:<name>``, "Don't change" is ``keep``. Light or
  dark is the policy's mode. A palette choice keeps the recorded mode and a
  mode choice keeps the recorded palette (`Store.choose_palette` with
  ``keep_mode``, `Store.choose_mode`), so a gesture queued behind another
  builds on what that one saved, as the classic pairing editor does.
* **Still**: a library image chosen by hand, or the automatic one again;
  Regenerate for a scene's automatic still; Reset to defaults.
* **Try again** clears the durable playback warning (`Store.clear_borked`)
  and republishes, so the runtime's configuration stops marking it taboo.
* **Remove** is the classic removal, worded for who owns the file: a
  download Wall-in-One made is deleted (after a confirmation), the user's own
  file goes to the Trash, and Workshop scenes or files outside the library
  folders are not Wall-in-One's to remove. Removal deletes its favorite,
  colors and playlist entries too, so there is no Undo: nothing could put
  them back. One wallpaper at a time, because each removal holds the library
  until its rescan lands, and never the last thing a playlist in use can
  play (the Playlists page's own rule, `RealPlaylists.emptied_by_removing`).
* **Playlists**: adding, and making a playlist with wallpapers in it, is the
  Playlists page's own save (`real_playlists.RealPlaylists`): one task,
  refused for a wallpaper playback skips, never "your pick" or All.

**Lanes.** Every write runs on the application's ordered authoring lane
(`Application.authoring_action_async`); a queued gesture is revalidated when
it reaches the head (``prepare``). Scheme previews run `noctalia theme` on
their own small pool, and the palette list is discovered by a
`PaletteCatalog`; neither ever runs on GTK, and both deliver at
``PRIORITY_DEFAULT``.

**What the controls show.** While color saves for a wallpaper are queued,
its view shows the latest queued choice (`pending_policy`); when the last one
settles -- saved, failed or refused -- the view is rebuilt from what the
store holds, so a failed save never keeps showing its choice
(`ui.pending_edits`). Everything else shows only what is durable.

**Read-only.** While a store saved by a newer version exists, nothing here
writes (`editing_blocked`), and the controls say why.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Protocol, TypeVar

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk

from wall_in_one import config
from wall_in_one.library import manage, pairings, scan
from wall_in_one.library.model import Kind, MediaItem, RepresentativeStillError
from wall_in_one.session import RemovalResult, Session
from wall_in_one.theme import noctalia
from wall_in_one.theme.palette import Palette, PalettePair
from wall_in_one.ui.next import real_playlists
from wall_in_one.ui.next.state import (
    UNCHANGED,
    Choices,
    Removal,
    ToastButton,
    Undo,
    WallpaperView,
)
from wall_in_one.ui.palette_catalog import CatalogState, PaletteCatalog
from wall_in_one.ui.pending_edits import queue_intent, settle_intent

_T = TypeVar("_T")

#: Noctalia's generators in words (`noctalia.ALL_SCHEMES` order).
SCHEME_DESCRIPTIONS: Final[Mapping[str, str]] = {
    "m3-tonal-spot": "Calm, built around the main color",
    "m3-content": "Stays close to the picture",
    "m3-fruit-salad": "Playful, shifted hues",
    "m3-rainbow": "Bold accents in several hues",
    "m3-monochrome": "Grays only",
    "vibrant": "Saturated and bright",
    "faithful": "The picture's own colors",
    "soft": "Pastel and gentle",
    "dysfunctional": "Deliberately clashing",
    "muted": "Low saturation",
}
#: The palette sources a policy may name (`palettes.Origin`, minus legacy).
PALETTE_ORIGINS: Final = ("builtin", "community", "custom")
#: The colors an inspector swatch strip shows, in order.
STRIP_TOKENS: Final = ("surface", "primary", "secondary", "tertiary", "error")
#: Scheme previews generated at once: `noctalia theme` peaks around 119 MB.
PREVIEW_WORKERS: Final = 2
#: Previews kept per window: ten schemes for each of a few dozen stills.
MAX_PREVIEWS: Final = 400


class StillResult(Protocol):
    """A saved still choice, with the default its worker resolved."""

    @property
    def item(self) -> MediaItem: ...

    @property
    def effective_still(self) -> Path | None: ...


class ResetResult(StillResult, Protocol):
    """A saved reset: whether there was anything to forget."""

    @property
    def changed(self) -> bool: ...


class EditingBackend(Protocol):
    """What the editing uses of `wall_in_one.ui.app.Application`."""

    @property
    def session(self) -> Session: ...

    @property
    def settings(self) -> config.Settings: ...

    def window_report(self, message: str) -> None: ...

    def authoring_action_async(
        self,
        work: Callable[[], _T],
        finish: Callable[[_T], None],
        *,
        prepare: Callable[[], Callable[[], _T]] | None = None,
        failure: Callable[[str], None] | None = None,
    ) -> bool: ...

    def current_item_for_authoring(self, item: MediaItem) -> MediaItem: ...

    def prepare_pairing_mutation(
        self,
        item: MediaItem,
        mutation: Callable[[pairings.Store, MediaItem], _T],
    ) -> Callable[[], _T]: ...

    def prepare_still_pairing_mutation(
        self,
        item: MediaItem,
        requested_still: Path | None,
        mutation: Callable[[pairings.Store, MediaItem, Path | None], pairings.Pairing],
    ) -> Callable[[], StillResult]: ...

    def prepare_pairing_reset(self, item: MediaItem) -> Callable[[], ResetResult]: ...

    def adopt_pairing_still(self, item: MediaItem, effective_still: Path | None) -> MediaItem: ...

    def pairing_changed(self, item: MediaItem) -> None: ...

    def regenerate_scene_still(self, item: MediaItem) -> bool: ...

    def remove_item_async(
        self,
        item: MediaItem,
        *,
        trash: bool,
        finish: Callable[[RemovalResult], None],
    ) -> bool: ...

    def playlists_changed(self) -> None: ...

    def favourites_changed(self) -> None: ...


class Shown(Protocol):
    """What the editing uses of the adapter whose views it changes (`RealAppState`)."""

    @property
    def default_scheme(self) -> str: ...

    def item_of(self, wid: str) -> MediaItem | None:
        """The library item behind a wallpaper id, as the views last saw it."""
        ...

    def restate(self, wid: str) -> bool:
        """Rebuild one view from the stores (and queued colors); True if it changed."""
        ...

    def emit_changed(self, *topics: str) -> None: ...

    def toast(
        self, text: str, undo: Undo | None = None, action: ToastButton | None = None
    ) -> None: ...


class PlaylistWork(Protocol):
    """What the edits use of the Playlists page's part (`real_playlists.RealPlaylists`)."""

    def add_entries(self, pid: str, wids: Sequence[str], position: int | None = None) -> None: ...

    def create_playlist(self, name: str, wids: Sequence[str] = ()) -> str: ...

    def emptied_by_removing(self, sources: Iterable[str]) -> list[str]: ...


# -- palettes -------------------------------------------------------------------------


def _strip(palette: Palette | None) -> list[str]:
    if palette is None:
        return []
    colours = palette.colours
    return [colours[token].hex for token in STRIP_TOKENS if token in colours]


@dataclass(frozen=True, slots=True)
class RealPalette:
    """One installed palette (`PaletteView`); built-ins have no colors to show."""

    name: str
    origin: str
    colours: PalettePair | None = None

    @property
    def key(self) -> str:
        return f"{self.origin}:{self.name}"

    def strip(self, dark: bool) -> list[str]:
        if self.colours is None:
            return []
        return _strip(self.colours.for_mode("dark" if dark else "light"))


def palette_key(policy: pairings.PalettePolicy) -> str | None:
    """The `PaletteView.key` a named-palette policy chooses, or None for any other."""
    if policy.kind in PALETTE_ORIGINS and policy.name:
        return f"{policy.kind}:{policy.name}"
    return None


# -- scheme previews ------------------------------------------------------------------

Generate = Callable[[Path, str], PalettePair]


def _generate(image: Path, scheme: str) -> PalettePair:
    return noctalia.generate(image, scheme)


class SchemePreviews:
    """`noctalia theme` for (still, scheme) pairs, off GTK, remembered both ways.

    A failure is remembered too: the inspector asks again on every redraw, and
    asking a broken or missing ``noctalia`` again would loop. ``on_ready``
    runs on GTK (at ``PRIORITY_DEFAULT``) after any result lands.
    """

    def __init__(self, on_ready: Callable[[], None], generate: Generate = _generate) -> None:
        self._on_ready = on_ready
        self._generate = generate
        self._pool: ThreadPoolExecutor | None = None
        self._done: dict[tuple[Path, str], PalettePair | None] = {}
        self._pending: set[tuple[Path, str]] = set()
        self._closed = False

    def lookup(self, image: Path, scheme: str) -> PalettePair | None:
        return self._done.get((image, scheme))

    def known(self, image: Path, scheme: str) -> bool:
        return (image, scheme) in self._done

    def request(self, image: Path, scheme: str) -> None:
        key = (image, scheme)
        if self._closed or key in self._done or key in self._pending:
            return
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=PREVIEW_WORKERS, thread_name_prefix="next-scheme"
            )
        self._pending.add(key)
        future = self._pool.submit(self._generate, image, scheme)
        future.add_done_callback(lambda done: self._finished(key, done))

    def _finished(self, key: tuple[Path, str], future: Future[PalettePair]) -> None:
        try:
            pair: PalettePair | None = future.result()
        except Exception:  # a preview is never worth an exception
            pair = None

        def deliver() -> bool:
            self._pending.discard(key)
            if self._closed:
                return GLib.SOURCE_REMOVE
            if len(self._done) >= MAX_PREVIEWS:
                self._done.pop(next(iter(self._done)))
            self._done[key] = pair
            self._on_ready()
            return GLib.SOURCE_REMOVE

        # Ends a "generating" state the inspector shows: above redraw.
        GLib.idle_add(deliver, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]

    @property
    def pending(self) -> int:
        return len(self._pending)

    def shutdown(self) -> None:
        self._closed = True
        self._pending.clear()
        pool, self._pool = self._pool, None
        if pool is not None:
            # Nothing is written; an abandoned generation leaves nothing behind.
            pool.shutdown(wait=False, cancel_futures=True)


def scheme_name(key: str) -> str:
    """``m3-tonal-spot`` -> ``Tonal Spot``."""
    return key.removeprefix("m3-").replace("-", " ").title()


def _listed(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"


def _quoted(text: str) -> str:
    return f"\u201c{text}\u201d"


def _wallpapers(count: int, items: Sequence[MediaItem]) -> str:
    """``"rain"`` when the one asked for is the one saved, else ``"3 wallpapers"``."""
    if count == 1 and len(items) == 1:
        return _quoted(items[0].name)
    return f"{count} wallpaper{'' if count == 1 else 's'}"


def _hidden(item: MediaItem, roots: Sequence[Path]) -> bool:
    """Whether any part of ``item``'s path below its library folder is a dotfile.

    The scanner never lists one, so this only guards a stale or forged id:
    dotfiles, and the ``.wall-in-one-removal-*`` folders above all, are never
    removed from here.
    """
    for root in roots:
        if item.path.is_relative_to(root):
            return any(part.startswith(".") for part in item.path.relative_to(root).parts)
    return any(part.startswith(".") for part in item.path.parts[1:])


def _wanted_policy(
    shown: pairings.PalettePolicy,
    *,
    mode: str | None,
    scheme: object,
    palette: str | None,
    theme_mode: str | None,
) -> pairings.PalettePolicy:
    """The policy one color gesture asks for, built on what the controls show."""
    kind, name = shown.kind, shown.name
    if mode is not None:
        if mode == "adaptive":
            if kind != pairings.ADAPTIVE:
                kind, name = pairings.ADAPTIVE, ""
        elif mode == "keep":
            kind, name = pairings.KEEP, ""
        elif mode != "palette":
            raise ValueError(f"unknown color choice {mode!r}")
        # "palette" alone names no palette: the policy stays until one is chosen.
    if scheme is not UNCHANGED:
        if scheme is not None and scheme not in noctalia.ALL_SCHEMES:
            raise ValueError(f"unknown scheme {scheme!r}")
        kind, name = pairings.ADAPTIVE, scheme if isinstance(scheme, str) else ""
    if palette is not None:
        origin, separator, chosen = palette.partition(":")
        if not separator or origin not in PALETTE_ORIGINS or not chosen.strip():
            raise ValueError(f"not a palette: {palette!r}")
        kind, name = origin, chosen
    chosen_mode = shown.mode if theme_mode is None else pairings.Mode(theme_mode)
    return pairings.PalettePolicy(kind, name, chosen_mode)


class RealEditing:
    """`LibraryEditing` over the application's stores, for one `RealAppState`."""

    def __init__(
        self,
        backend: EditingBackend,
        shown: Shown,
        playlists_part: PlaylistWork,
        *,
        generate: Generate = _generate,
        catalog: Callable[[], PaletteCatalog] = PaletteCatalog,
    ) -> None:
        self._backend = backend
        self._shown = shown
        self._playlists = playlists_part
        self._generate = generate
        self._make_catalog = catalog
        #: Color saves still queued, per pairing identity: what the controls
        #: show meanwhile, and how many are outstanding (`ui.pending_edits`).
        self._palette_intent: dict[str, tuple[pairings.PalettePolicy, int]] = {}
        self._previews: SchemePreviews | None = None
        self._catalog: PaletteCatalog | None = None
        self._catalog_listener = 0
        self._colors_queued = False
        self._closed = False

    # -- lifetime --------------------------------------------------------------------
    def listen(self) -> None:
        """The window is back: previews and the palette list may start again."""
        self._closed = False

    def close(self) -> None:
        """The window is going away: stop the preview and palette workers."""
        self._closed = True
        previews, self._previews = self._previews, None
        if previews is not None:
            previews.shutdown()
        catalog, self._catalog = self._catalog, None
        if catalog is not None:
            catalog.unsubscribe(self._catalog_listener)
            catalog.shutdown()
        self._catalog_listener = 0

    def _colors_changed(self, *_arguments: object) -> None:
        """Previews or palettes arrived: one ``colors`` redraw for the batch."""
        if self._closed or self._colors_queued:
            return
        self._colors_queued = True

        def emit() -> bool:
            self._colors_queued = False
            if not self._closed:
                self._shown.emit_changed("colors")
            return GLib.SOURCE_REMOVE

        # After the deliveries already queued at the same priority.
        GLib.idle_add(emit, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]

    def _scheme_previews(self) -> SchemePreviews | None:
        if self._previews is None and not self._closed:
            self._previews = SchemePreviews(self._colors_changed, self._generate)
        return self._previews

    def _palette_catalog(self, *, load: bool) -> PaletteCatalog | None:
        if self._catalog is None and load and not self._closed:
            catalog = self._make_catalog()
            self._catalog_listener = catalog.subscribe(self._on_catalog, replay=False)
            catalog.ensure_loaded()
            self._catalog = catalog
        return self._catalog

    def _on_catalog(self, state: CatalogState) -> None:
        if not state.loading:
            self._colors_changed()

    # -- shared helpers ------------------------------------------------------------------
    @property
    def _session(self) -> Session:
        return self._backend.session

    def _report(self, message: str) -> None:
        self._backend.window_report(message)

    def _refused(self) -> bool:
        blocked = self.editing_blocked()
        if blocked:
            self._report(blocked)
        return bool(blocked)

    def _items(self, wids: Iterable[str]) -> list[MediaItem]:
        found: list[MediaItem] = []
        for wid in wids:
            item = self._shown.item_of(wid)
            if item is not None and item not in found:
                found.append(item)
        return found

    def _skipped_reason(self, item: MediaItem) -> str:
        health = self._session.pairings.health(pairings.Identity.of(item))
        return health.reason if health.is_borked else ""

    def _current(self, items: Iterable[MediaItem]) -> list[MediaItem]:
        """``items`` as the library holds them now; removed ones are left out."""
        current: list[MediaItem] = []
        for item in items:
            try:
                current.append(self._backend.current_item_for_authoring(item))
            except ValueError:
                continue
        return current

    def _settle_view(self, wid: str, *, force: bool = False) -> None:
        """Show what the stores hold for ``wid`` (``force``: redraw even if the same)."""
        if self._shown.restate(wid) or force:
            self._shown.emit_changed("library")

    # -- LibraryEditing: availability ---------------------------------------------------
    @property
    def extras(self) -> frozenset[str]:
        """No per-wallpaper motion, frame stills, log or bulk removal exist yet."""
        return frozenset()

    def editing_blocked(self) -> str:
        newer = self._session.newer_version_files()
        if not newer:
            return ""
        verb = "is" if len(newer) == 1 else "are"
        return (
            f"Changes to wallpapers are off while {_listed(newer)} {verb} from a newer "
            "version of Wall-in-One"
        )

    def choices(self, wid: str) -> Choices:
        item = self._shown.item_of(wid)
        if item is None:
            return Choices()
        identity = pairings.Identity.of(item)
        record = self._session.pairings.get(identity)
        customized = record is not None and record.customized
        return Choices(
            customized=customized or identity.key in self._palette_intent,
            still=customized and record is not None and record.still is not None,
            regenerate=item.kind is Kind.SCENE and bool(item.scene),
        )

    def pending_policy(self, item: MediaItem) -> pairings.PalettePolicy | None:
        """The latest queued color choice for ``item``, while one is queued."""
        entry = self._palette_intent.get(pairings.Identity.of(item).key)
        return entry[0] if entry is not None else None

    def _shown_policy(self, item: MediaItem) -> pairings.PalettePolicy:
        pending = self.pending_policy(item)
        if pending is not None:
            return pending
        record = self._session.pairings.get(pairings.Identity.of(item))
        if record is not None and record.customized:
            return record.palette
        return pairings.PalettePolicy()

    # -- colors --------------------------------------------------------------------------
    def set_wallpaper_colors(
        self,
        wid: str,
        *,
        mode: str | None = None,
        scheme: object = UNCHANGED,
        palette: str | None = None,
        theme_mode: str | None = None,
    ) -> None:
        """Queue one color gesture; the view shows it until the last queued one settles."""
        item = self._shown.item_of(wid)
        if item is None:
            return
        if self._refused():
            self._settle_view(wid, force=True)  # the clicked control goes back
            return
        shown = self._shown_policy(item)
        try:
            wanted = _wanted_policy(
                shown, mode=mode, scheme=scheme, palette=palette, theme_mode=theme_mode
            )
        except ValueError as error:
            self._report(f"Colors for {item.name} were not saved: {error}")
            self._settle_view(wid, force=True)
            return
        # Against what the controls show, never the durable record alone: going
        # back to the saved choice while another is queued is a real gesture.
        if wanted == shown:
            return
        same_colours = (wanted.kind, wanted.name) == (shown.kind, shown.name)
        mutation: Callable[[pairings.Store, MediaItem], pairings.Pairing]
        if same_colours:

            def mutation(store: pairings.Store, current: MediaItem) -> pairings.Pairing:
                return store.choose_mode(current, wanted.mode)

        elif wanted.mode == shown.mode:
            # The mode is whatever is durable when this reaches the head of the
            # lane (a record's, or the default): never one a failed save showed.
            colours_only = replace(wanted, mode=pairings.Mode.KEEP)

            def mutation(store: pairings.Store, current: MediaItem) -> pairings.Pairing:
                return store.choose_palette(current, colours_only, keep_mode=True)

        else:

            def mutation(store: pairings.Store, current: MediaItem) -> pairings.Pairing:
                return store.choose_palette(current, wanted)

        self._queue_colors(item, wid, wanted, mutation)

    def _queue_colors(
        self,
        item: MediaItem,
        wid: str,
        wanted: pairings.PalettePolicy,
        mutation: Callable[[pairings.Store, MediaItem], pairings.Pairing],
    ) -> None:
        backend = self._backend
        key = pairings.Identity.of(item).key
        store = self._session.pairings
        queue_intent(self._palette_intent, key, wanted)
        self._shown.restate(wid)  # quietly: the clicked control already shows it

        def saved(_record: pairings.Pairing) -> None:
            last = settle_intent(self._palette_intent, key)
            backend.pairing_changed(item)
            if last:
                # Redrawn even when the colors read the same: what the saved
                # record offers changed too (Reset to defaults), and the rescan
                # `pairing_changed` asks for comes later, at idle.
                self._settle_view(wid, force=True)

        reported = False

        def failed(error: str) -> None:
            nonlocal reported
            reported = True
            last = settle_intent(self._palette_intent, key)
            self._report(f"Colors for {item.name} were not saved; nothing changed: {error}")
            if last:
                self._settle_view(wid, force=True)

        accepted = backend.authoring_action_async(
            lambda: mutation(store, item),
            saved,
            prepare=lambda: backend.prepare_pairing_mutation(item, mutation),
            failure=failed,
        )
        # Refused without a report (shutting down, or authoring not open yet):
        # nothing is queued, so the controls show what is saved.
        if not accepted and not reported and settle_intent(self._palette_intent, key):
            self._settle_view(wid, force=True)

    def schemes(self) -> list[tuple[str, str, str]]:
        return [
            (key, scheme_name(key), SCHEME_DESCRIPTIONS.get(key, ""))
            for key in noctalia.ALL_SCHEMES
        ]

    def _representative(self, item: MediaItem) -> Path | None:
        """The still adaptive colors are generated from (no disk access)."""
        session = self._session
        current = session.library.find(item.path) or item
        return session.pairings.resolve_accepted(current, session.library).still

    def scheme_swatches(
        self, wallpaper: WallpaperView, scheme: str | None, dark: bool = True
    ) -> list[str]:
        """``scheme``'s colors for this wallpaper, or empty while they are generated."""
        item = self._shown.item_of(wallpaper.id)
        previews = self._scheme_previews()
        if item is None or previews is None:
            return []
        image = self._representative(item)
        chosen = scheme or self._shown.default_scheme
        if image is None or chosen not in noctalia.ALL_SCHEMES:
            return []
        pair = previews.lookup(image, chosen)
        if pair is None:
            previews.request(image, chosen)
            return []
        return _strip(pair.for_mode("dark" if dark else "light"))

    def effective_swatches(self, wallpaper: WallpaperView, dark: bool = True) -> list[str]:
        """The colors ``wallpaper`` puts on the desktop, when already known (never asks)."""
        item = self._shown.item_of(wallpaper.id)
        if item is None or wallpaper.color_mode == "keep":
            return []
        if wallpaper.color_mode == "palette":
            catalog = self._palette_catalog(load=False)
            if catalog is None:
                return []
            for entry in catalog.state.discovery.entries:
                if f"{entry.origin.value}:{entry.name}" == wallpaper.palette:
                    return _strip(entry.for_mode("dark" if dark else "light"))
            return []
        previews = self._previews
        image = self._representative(item)
        if previews is None or image is None:
            return []
        pair = previews.lookup(image, wallpaper.scheme or self._shown.default_scheme)
        return _strip(pair.for_mode("dark" if dark else "light")) if pair is not None else []

    def palettes(self, origin: str | None = None) -> list[RealPalette]:
        """Installed palettes Noctalia can apply (the list loads off GTK on first use)."""
        catalog = self._palette_catalog(load=True)
        if catalog is None:
            return []
        return [
            RealPalette(entry.name, entry.origin.value, entry.colours)
            for entry in catalog.state.discovery.entries
            if entry.origin.is_applicable and (origin is None or entry.origin.value == origin)
        ]

    def palettes_loading(self) -> bool:
        catalog = self._catalog
        return catalog is None or catalog.state.loading or catalog.state.phase == "idle"

    # -- still, reset, try again ------------------------------------------------------------
    def choose_still(self, wid: str, path: str | None) -> None:
        item = self._shown.item_of(wid)
        if item is None or not item.is_moving or self._refused():
            return
        backend = self._backend
        still = Path(path) if path is not None else None
        if still is not None:
            try:
                still = self._session.library.require_representative_still(still).path
            except RepresentativeStillError as error:
                self._report(str(error))
                return

        def choose(
            current_store: pairings.Store, current: MediaItem, chosen: Path | None
        ) -> pairings.Pairing:
            return current_store.choose_still(current, chosen)

        def saved(result: StillResult) -> None:
            current = backend.adopt_pairing_still(result.item, result.effective_still)
            backend.pairing_changed(current)
            self._settle_view(wid, force=True)
            self._report(
                f"{item.name} now uses {still.name} as its still"
                if still is not None
                else f"{item.name} uses its automatic still again"
            )

        def unprepared() -> StillResult:  # pragma: no cover - prepare always replaces it
            raise RuntimeError("the still choice was not prepared")

        backend.authoring_action_async(
            unprepared,
            saved,
            prepare=lambda: backend.prepare_still_pairing_mutation(item, still, choose),
            failure=lambda error: self._report(
                f"The still for {item.name} could not be saved; nothing changed: {error}"
            ),
        )

    def reset_wallpaper(self, wid: str) -> None:
        item = self._shown.item_of(wid)
        if item is None or self._refused():
            return
        backend = self._backend

        def reset(result: ResetResult) -> None:
            current = backend.adopt_pairing_still(result.item, result.effective_still)
            if not result.changed:
                self._report(f"{current.name} had nothing to reset")
                return
            backend.pairing_changed(current)
            self._settle_view(wid, force=True)
            self._report(f"{current.name} is back to its defaults")

        def unprepared() -> ResetResult:  # pragma: no cover - prepare always replaces it
            raise RuntimeError("the reset was not prepared")

        backend.authoring_action_async(
            unprepared,
            reset,
            prepare=lambda: backend.prepare_pairing_reset(item),
            failure=lambda error: self._report(
                f"{item.name} could not be reset; nothing changed: {error}"
            ),
        )

    def regenerate_still(self, wid: str) -> None:
        item = self._shown.item_of(wid)
        if item is None or item.kind is not Kind.SCENE or self._refused():
            return
        if self._backend.regenerate_scene_still(item):
            self._report(f"Capturing a new still for {item.name} in the background")

    def retry_wallpaper(self, wid: str) -> None:
        """Clear the saved playback warning; the republished configuration drops the taboo."""
        item = self._shown.item_of(wid)
        if item is None or self._refused():
            return
        if not self._skipped_reason(item):
            self._settle_view(wid)
            return
        backend = self._backend
        store = self._session.pairings

        def cleared(changed: bool) -> None:
            backend.pairing_changed(item)
            self._settle_view(wid, force=True)
            if changed:
                self._report(f"{item.name} will be tried again")

        backend.authoring_action_async(
            lambda: store.clear_borked(item),
            cleared,
            prepare=lambda: backend.prepare_pairing_mutation(
                item, lambda current_store, current: current_store.clear_borked(current)
            ),
            failure=lambda error: self._report(
                f"{item.name} is still skipped; nothing changed: {error}"
            ),
        )

    # -- favorites -----------------------------------------------------------------------
    def favorite_wallpapers(self, wids: list[str]) -> Undo | None:
        """Star every one in one save; the stars follow the store, which says so."""
        if self._refused():
            return None
        starred = self._session.favourites.paths
        items = [item for item in self._items(wids) if item.path not in starred]
        if not items:
            self._report("They are already favorites")
            return None
        backend = self._backend
        store = self._session.favourites

        def prepare() -> Callable[[], int]:
            paths = [current.path for current in self._current(items)]
            if not paths:
                raise ValueError("they were removed before they could be saved")
            return lambda: store.add_many(paths)

        def saved(added: int) -> None:
            backend.favourites_changed()
            noun = _wallpapers(added, items)
            self._report(f"Added {noun} to favorites")

        backend.authoring_action_async(
            lambda: 0,
            saved,
            prepare=prepare,
            failure=lambda error: self._report(
                f"Favorites were not saved; nothing changed: {error}"
            ),
        )
        return None

    # -- removal -------------------------------------------------------------------------
    def removal(self, wid: str) -> Removal:
        item = self._shown.item_of(wid)
        if item is None:
            return Removal("", "It is no longer in the library")
        roots = self._session.library.roots
        if manage.is_removable(item, roots) and not _hidden(item, roots):
            return Removal("delete" if item.deletable else "trash")
        if item.kind is Kind.SCENE or item.provider == scan.WORKSHOP_PROVIDER:
            return Removal(
                "",
                "Steam installed it: unsubscribe in Steam, then rescan "
                "(a later reinstall starts clean)",
            )
        return Removal("", "It is outside your library folders: remove it there, then rescan")

    def remove_wallpapers(self, wids: list[str]) -> Undo | None:
        """Delete or trash one wallpaper by who owns it; the outcome is reported, no Undo.

        Removal also drops its favorite, colors and playlist entries, which
        nothing could restore, so an Undo would be a lie.
        """
        if self._refused():
            return None
        items = self._items(wids)
        if not items:
            return None
        if len(items) > 1:
            self._report("Remove one wallpaper at a time: each removal rescans the library first")
            return None
        item = items[0]
        removal = self.removal(str(item.path))
        if not removal.how:
            self._report(f"{item.name} can\u2019t be removed here. {removal.reason}")
            return None
        emptied = self._playlists.emptied_by_removing([str(item.path)])
        if emptied:
            self._report(
                f"{item.name} was not removed: {_listed(emptied)} would have nothing left "
                "to play. Add another wallpaper there first."
            )
            return None
        self._backend.remove_item_async(item, trash=removal.how == "trash", finish=self._removed)
        return None

    def _removed(self, result: RemovalResult) -> None:
        """Say what happened, as the classic window does after a removal."""
        if not result.committed:
            action = "moved" if result.trash else "deleted"
            detail = (
                "; the file is still present, but its prepared removal intent "
                f"could not be cleared: {'; '.join(result.cancellation_failures)}"
                if result.cancellation_failures
                else ""
            )
            self._report(f"Nothing was {action}: {result.error_message}{detail}")
            return
        metadata = manage.metadata_cleanup_note(result.cleanup_failures)
        if result.error_message:
            self._report(result.error_message + metadata)
            return
        physical = result.physical
        if isinstance(physical, manage.Removal):
            said = physical.describe()
            self._report(f"{said[:1].upper()}{said[1:]}{physical.cleanup_note()}{metadata}")
        elif isinstance(physical, manage.Trashed):
            self._report(
                f"{result.item.name} moved to the Trash{physical.cleanup_note()}{metadata}"
            )

    def show_in_files(self, wid: str) -> None:
        item = self._shown.item_of(wid)
        if item is None:
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(item.path)))

        def opened(source: Gtk.FileLauncher, result: Gio.AsyncResult) -> None:
            try:
                source.open_containing_folder_finish(result)
            except GLib.Error as error:
                self._report(f"{item.name} could not be shown in Files: {error.message}")

        launcher.open_containing_folder(None, None, opened)

    # -- playlists: the Playlists page's own saves ----------------------------------------
    def add_to_playlist(self, pid: str, wids: list[str]) -> None:
        """Append, as the Playlists page adds: one save, refused for a skipped wallpaper."""
        if self._refused():
            return
        if real_playlists.hidden(pid) or self._session.playlists.get(pid) is None:
            self._report("Wallpapers can\u2019t be added to that playlist")
            return
        self._playlists.add_entries(pid, wids)

    def create_playlist(self, name: str, wids: Sequence[str] = ()) -> str:
        """A new playlist made with ``wids`` in it, the one way playlists are made."""
        if self._refused():
            return ""
        return self._playlists.create_playlist(name, wids)
