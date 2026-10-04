"""The real `LibraryEditing` and `LibraryFolders` write what the classic window writes.

No display: a `RealAppState` over a real `Session` (stores in the test's own
XDG dirs) with the application's authoring calls faked. Authoring tasks wait
in a queue until `run_authoring`, exactly one task per user gesture, so a
test can see what is queued, what the controls show meanwhile, and what is
durable once each task lands -- or fails, or is refused.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import pytest

from tests.gtk_helpers import spin_until
from tests.test_next_state import FakeApplication, _library, _session
from wall_in_one import config, paths
from wall_in_one.library import favourites, manage, pairings, playlists, scan
from wall_in_one.library.model import Kind, Library, MediaItem, Ownership
from wall_in_one.session import QUICK_CHOICE_ID, RemovalResult, Session
from wall_in_one.theme import noctalia, source
from wall_in_one.theme import palettes as theme_palettes
from wall_in_one.theme.palette import Colour, Palette, PalettePair
from wall_in_one.ui.next import real_settings
from wall_in_one.ui.next.prefs import UiPrefsKeeper
from wall_in_one.ui.next.real_editing import RealEditing
from wall_in_one.ui.next.real_folders import RealFolders
from wall_in_one.ui.next.real_state import RealAppState
from wall_in_one.ui.next.state import Choices, LibraryEditing, LibraryFolders, Removal

if TYPE_CHECKING:
    # mypy proves the application offers every authoring call the edits use.
    from wall_in_one.ui.app import Application
    from wall_in_one.ui.next.real_state import AuthoringBackend

    def _application_authors(application: Application) -> AuthoringBackend:
        return application

    def _editing_conforms(editing: RealEditing) -> LibraryEditing:
        return editing

    def _folders_conform(folders: RealFolders) -> LibraryFolders:
        return folders


_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class StillDone:
    item: MediaItem
    record: pairings.Pairing
    effective_still: Path | None


@dataclass(frozen=True, slots=True)
class ResetDone:
    item: MediaItem
    changed: bool
    effective_still: Path | None


class EditingApplication(FakeApplication):
    """`FakeApplication` plus every authoring call the library edits use."""

    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.pairing_changes: list[Path] = []
        self.playlist_changes = 0
        self.regenerated: list[Path] = []
        self.removals: list[tuple[Path, bool]] = []
        self.removal_finish: list[Callable[[RemovalResult], None]] = []
        self.refreshed = 0
        self.requested = session.settings
        #: Authoring refuses without a report, as while shutting down.
        self.refuse = False
        #: Raised by the next pairing prepare, once (a store that cannot be written).
        self.break_next: list[Exception] = []
        self.after_playlists: Callable[[], None] = lambda: None
        self.tasks = 0

    def authoring_action_async(
        self,
        work: Callable[[], _T],
        finish: Callable[[_T], None],
        *,
        prepare: Callable[[], Callable[[], _T]] | None = None,
        failure: Callable[[str], None] | None = None,
    ) -> bool:
        if self.refuse:
            return False
        self.tasks += 1
        return super().authoring_action_async(work, finish, prepare=prepare, failure=failure)

    def prepare_pairing_mutation(
        self, item: MediaItem, mutation: Callable[[pairings.Store, MediaItem], _T]
    ) -> Callable[[], _T]:
        if self.break_next:
            raise self.break_next.pop(0)
        current = self.current_item_for_authoring(item)
        store = self.session.pairings
        return lambda: mutation(store, current)

    def prepare_still_pairing_mutation(
        self,
        item: MediaItem,
        requested_still: Path | None,
        mutation: Callable[[pairings.Store, MediaItem, Path | None], pairings.Pairing],
    ) -> Callable[[], StillDone]:
        store = self.session.pairings

        def work() -> StillDone:
            record = mutation(store, item, requested_still)
            return StillDone(item, record, requested_still or item.paired_still)

        return work

    def prepare_pairing_reset(self, item: MediaItem) -> Callable[[], ResetDone]:
        store = self.session.pairings
        return lambda: ResetDone(item, store.reset(item), item.paired_still)

    def adopt_pairing_still(self, item: MediaItem, effective_still: Path | None) -> MediaItem:
        return item

    def pairing_changed(self, item: MediaItem) -> None:
        self.pairing_changes.append(item.path)

    def regenerate_scene_still(self, item: MediaItem) -> bool:
        self.regenerated.append(item.path)
        return True

    def remove_item_async(
        self, item: MediaItem, *, trash: bool, finish: Callable[[RemovalResult], None]
    ) -> bool:
        self.removals.append((item.path, trash))
        self.removal_finish.append(finish)
        return True

    def playlists_changed(self) -> None:
        self.playlist_changes += 1
        self.after_playlists()

    @property
    def requested_settings(self) -> config.Settings:
        return self.requested

    def update_settings_async(
        self,
        *,
        on_success: Callable[[config.Settings], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        **changes: Any,
    ) -> bool:
        if self.unknown_keys:
            if on_error is not None:
                on_error(config.read_only_message(self.unknown_keys))
            return False
        wanted = replace(self.requested, **changes).validated()
        self.requested = wanted

        def run() -> None:
            if on_success is not None:
                on_success(wanted)

        self.authoring.append(run)
        return True

    def refresh_library(self) -> None:
        self.refreshed += 1

    # -- the rest of `real_settings.SettingsBackend`, which the folders go through --
    @property
    def resolved_palette(self) -> source.ResolvedPalette | None:
        return None

    def require_authoring_ready(self, *, report: bool = True) -> bool:
        return True

    def reload_palette(
        self, on_complete: real_settings.PaletteReloadCallback | None = None
    ) -> source.ResolvedPalette:
        return source.fixed()

    def apply_noctalia_palette_async(
        self,
        palette_source: str,
        name: str,
        *,
        on_complete: real_settings.ThemeActionCallback | None = None,
    ) -> bool:
        return False

    def use_preview_scheme(
        self,
        scheme: str,
        *,
        sync_noctalia: bool,
        on_complete: real_settings.ThemeActionCallback | None = None,
    ) -> bool:
        return False


@pytest.fixture
def library(tmp_path: Path) -> tuple[MediaItem, ...]:
    return _library(tmp_path / "home" / "Pictures" / "Wallpapers")


@pytest.fixture
def backend(library: tuple[MediaItem, ...]) -> Iterator[EditingApplication]:
    session = _session(library)
    try:
        yield EditingApplication(session)
    finally:
        session.shutdown()


@pytest.fixture
def adapter(backend: EditingApplication) -> Iterator[RealAppState]:
    keeper = UiPrefsKeeper(backend.window_report)
    state = RealAppState(backend, keeper, authoring=backend)
    backend.after_favourites = state.reload
    backend.after_playlists = state.reload_playlists
    try:
        yield state
    finally:
        state.close()


def editing_of(adapter: RealAppState) -> RealEditing:
    editing = adapter.editing
    assert isinstance(editing, RealEditing)
    return editing


def folders_of(adapter: RealAppState) -> RealFolders:
    folders = adapter.library_folders
    assert isinstance(folders, RealFolders)
    return folders


def topics(adapter: RealAppState) -> list[str]:
    seen: list[str] = []
    adapter.connect("changed", lambda _state, topic: seen.append(topic))
    return seen


def toasts(adapter: RealAppState) -> list[tuple[str, object]]:
    seen: list[tuple[str, object]] = []
    adapter.connect("toast", lambda _state, text, button: seen.append((text, button)))
    return seen


def wid(library: tuple[MediaItem, ...], index: int) -> str:
    return str(library[index].path)


def durable(item: MediaItem) -> pairings.PalettePolicy:
    """What pairings.json holds for ``item``, read back from the file."""
    record = pairings.Store.open().get(pairings.Identity.of(item))
    return record.palette if record is not None and record.customized else pairings.PalettePolicy()


# -- what the adapter offers ---------------------------------------------------------


def test_the_authoring_calls_bring_editing_and_folders(adapter: RealAppState) -> None:
    editing = editing_of(adapter)
    assert adapter.library_folders is not None
    assert editing.extras == frozenset(), "no motion, frame, log or bulk removal backends"
    assert editing.editing_blocked() == ""
    keys = [key for key, _name, _words in editing.schemes()]
    assert keys == list(noctalia.ALL_SCHEMES)
    assert all(words for _key, _name, words in editing.schemes())


# -- colors: every choice round-trips through pairings.json ---------------------------


def test_colors_round_trip_adaptive_pinned_palette_keep_and_mode(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    item, target = library[1], wid(library, 1)

    # The default already is bare adaptive: choosing "Default" writes nothing.
    editing.set_wallpaper_colors(target, scheme=None)
    assert not backend.authoring

    editing.set_wallpaper_colors(target, scheme="vibrant")
    backend.run_authoring()
    assert durable(item).encode() == "adaptive:vibrant"
    assert (adapter.wallpaper(target).color_mode, adapter.wallpaper(target).scheme) == (
        "adaptive",
        "vibrant",
    )

    # "Default" again is the bare policy that follows Settings, not a pinned copy of it.
    editing.set_wallpaper_colors(target, scheme=None)
    backend.run_authoring()
    assert durable(item).encode() == "adaptive"
    assert adapter.wallpaper(target).scheme is None

    editing.set_wallpaper_colors(target, theme_mode="dark")
    backend.run_authoring()
    assert durable(item) == pairings.PalettePolicy("adaptive", "", pairings.Mode.DARK)

    # A palette keeps the recorded light/dark choice (the old palette-mode bug).
    editing.set_wallpaper_colors(target, palette="community:Nord")
    backend.run_authoring()
    assert durable(item) == pairings.PalettePolicy("community", "Nord", pairings.Mode.DARK)
    view = adapter.wallpaper(target)
    assert (view.color_mode, view.palette, view.theme_mode) == ("palette", "community:Nord", "dark")

    # "Palette" alone names no palette: nothing is written until one is chosen.
    editing.set_wallpaper_colors(target, mode="palette")
    assert not backend.authoring

    editing.set_wallpaper_colors(target, mode="keep")
    backend.run_authoring()
    assert durable(item) == pairings.PalettePolicy("keep", "", pairings.Mode.DARK)

    editing.set_wallpaper_colors(target, theme_mode="auto")
    backend.run_authoring()
    assert durable(item) == pairings.PalettePolicy("keep", "", pairings.Mode.AUTO)

    editing.set_wallpaper_colors(target, mode="adaptive")
    backend.run_authoring()
    assert durable(item) == pairings.PalettePolicy("adaptive", "", pairings.Mode.AUTO)
    assert backend.pairing_changes == [item.path] * 7, "each save is republished"
    assert backend.reports == []


def test_a_choice_that_is_not_one_is_refused_and_writes_nothing(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    seen = topics(adapter)
    editing.set_wallpaper_colors(wid(library, 0), scheme="no-such-scheme")
    editing.set_wallpaper_colors(wid(library, 0), palette="Nord")  # no origin: ambiguous
    editing.set_wallpaper_colors(wid(library, 0), palette="legacy:Old")
    assert not backend.authoring
    assert len(backend.reports) == 3
    assert all("were not saved" in report for report in backend.reports)
    assert seen.count("library") == 3, "the clicked control goes back each time"


def test_queued_color_gestures_show_the_latest_and_settle_to_it(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    item, target = library[2], wid(library, 2)
    editing.set_wallpaper_colors(target, palette="builtin:Ayu")
    editing.set_wallpaper_colors(target, theme_mode="light")
    assert len(backend.authoring) == 2, "one task per gesture, queued behind the first"
    view = adapter.wallpaper(target)
    assert (view.palette, view.theme_mode) == ("builtin:Ayu", "light"), "the controls show it"
    assert editing.choices(target).customized
    seen = topics(adapter)
    backend.authoring.popleft()()
    assert seen == [], "the first save lands while the second is queued: nothing to redraw"
    backend.run_authoring()
    # The last one redraws even though the colors read the same as queued: the
    # saved record now offers Reset to defaults, and no rescan has to come first.
    assert seen == ["library"]
    assert durable(item) == pairings.PalettePolicy("builtin", "Ayu", pairings.Mode.LIGHT)
    assert (adapter.wallpaper(target).palette, adapter.wallpaper(target).theme_mode) == (
        "builtin:Ayu",
        "light",
    )


def test_going_back_to_the_saved_choice_while_another_is_queued_is_not_dropped(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    item, target = library[2], wid(library, 2)
    editing.set_wallpaper_colors(target, palette="builtin:Nord")
    # Back to the durable default before the first save lands: a real gesture.
    editing.set_wallpaper_colors(target, mode="adaptive")
    assert len(backend.authoring) == 2
    backend.run_authoring()
    assert durable(item) == pairings.PalettePolicy("adaptive", "", pairings.Mode.KEEP)
    assert adapter.wallpaper(target).color_mode == "adaptive"


def test_a_failed_color_save_settles_to_what_is_durable(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    item, target = library[2], wid(library, 2)
    seen = topics(adapter)
    editing.set_wallpaper_colors(target, theme_mode="dark")
    editing.set_wallpaper_colors(target, palette="builtin:Nord")
    # The first save fails before it is written; the second is still queued, so
    # the controls keep showing the latest choice until it lands.
    backend.break_next.append(pairings.PairingError("local-io", "disk full"))
    backend.authoring.popleft()()
    assert backend.reports == [
        f"Colors for {item.name} were not saved; nothing changed: local-io: disk full"
    ]
    assert adapter.wallpaper(target).palette == "builtin:Nord", "the later choice may still save"
    backend.run_authoring()
    assert durable(item) == pairings.PalettePolicy("builtin", "Nord", pairings.Mode.KEEP)
    view = adapter.wallpaper(target)
    assert (view.palette, view.theme_mode) == ("builtin:Nord", "keep"), "dark was never saved"
    assert "library" in seen


def test_when_the_last_queued_color_save_fails_the_view_goes_back_to_durable(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    item, target = library[2], wid(library, 2)
    editing.set_wallpaper_colors(target, theme_mode="light")
    editing.set_wallpaper_colors(target, palette="builtin:Nord")
    backend.authoring.popleft()()  # light saves
    seen = topics(adapter)
    backend.break_next.append(pairings.PairingError("local-io", "disk full"))
    backend.run_authoring()  # Nord fails, and it was the last
    view = adapter.wallpaper(target)
    assert (view.color_mode, view.palette, view.theme_mode) == ("adaptive", None, "light")
    assert durable(item) == pairings.PalettePolicy("adaptive", "", pairings.Mode.LIGHT)
    assert seen == ["library"], "the inspector is rebuilt from what is durable"
    assert editing.choices(target) == Choices(customized=True)


def test_a_refused_color_save_shows_the_durable_choice_at_once(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    target = wid(library, 0)
    seen = topics(adapter)
    backend.refuse = True
    editing.set_wallpaper_colors(target, mode="keep")
    assert adapter.wallpaper(target).color_mode == "adaptive"
    assert seen == ["library"]
    assert editing.pending_policy(library[0]) is None


def test_a_reload_while_a_save_is_queued_keeps_showing_the_queued_choice(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    target = wid(library, 1)
    editing.set_wallpaper_colors(target, mode="keep")
    adapter.reload()  # a rescan landing mid-save
    assert adapter.wallpaper(target).color_mode == "keep"
    backend.run_authoring()
    adapter.reload()
    assert adapter.wallpaper(target).color_mode == "keep"


# -- scheme previews and palettes, off GTK ------------------------------------------


def _pair(seed: int) -> PalettePair:
    def variant(mode: str, shift: int) -> Palette:
        colours = {
            token: Colour(seed + shift + index, 10 * index, 200 - index)
            for index, token in enumerate(("surface", "primary", "secondary", "tertiary", "error"))
        }
        return Palette(mode, colours)  # type: ignore[arg-type]

    return PalettePair(dark=variant("dark", 0), light=variant("light", 40))


def test_scheme_swatches_are_generated_off_gtk_and_a_failure_is_remembered(
    adapter: RealAppState,
    library: tuple[MediaItem, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editing = editing_of(adapter)
    calls: list[tuple[Path, str]] = []

    def generate(image: Path, scheme: str = "", **_options: object) -> PalettePair:
        calls.append((image, scheme))
        if scheme == "muted":
            raise noctalia.NoctaliaError("noctalia is not installed")
        return _pair(len(calls))

    monkeypatch.setattr(noctalia, "generate", generate)
    seen = topics(adapter)
    view = adapter.wallpaper(wid(library, 0))
    assert editing.scheme_swatches(view, "vibrant") == []
    assert editing.scheme_swatches(view, "muted") == []
    assert editing.scheme_swatches(view, None) == [], "None is the Settings scheme"
    previews = editing._previews
    assert previews is not None
    spin_until(lambda: len(calls) == 3 and previews.pending == 0, timeout=5)
    spin_until(lambda: "colors" in seen, timeout=5)
    dark = editing.scheme_swatches(view, "vibrant")
    light = editing.scheme_swatches(view, "vibrant", dark=False)
    assert len(dark) == 5 and dark != light
    assert editing.scheme_swatches(view, "muted") == [], "a failure stays a failure"
    assert sorted(scheme for _image, scheme in calls) == ["m3-tonal-spot", "muted", "vibrant"]
    assert {image for image, _scheme in calls} == {library[0].path}, "the still itself"

    # The card dots only look up what is known; they never start a generation.
    assert adapter.wallpaper_swatches(adapter.wallpaper(wid(library, 1))) == []
    assert len(calls) == 3
    assert adapter.wallpaper_swatches(view) == editing.scheme_swatches(view, "m3-tonal-spot")
    assert len(adapter.wallpaper_swatches(view)) == 5, "it follows the Settings scheme"


def test_palettes_come_from_the_catalog_and_built_ins_have_no_colors(
    adapter: RealAppState,
    backend: EditingApplication,
    library: tuple[MediaItem, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = _pair(3)
    found = theme_palettes.Discovery(
        (
            theme_palettes.PaletteEntry("Nord", theme_palettes.Origin.BUILTIN, None, None),
            theme_palettes.PaletteEntry(
                "Nord", theme_palettes.Origin.COMMUNITY, Path("/x/Nord.json"), pair
            ),
            theme_palettes.PaletteEntry(
                "Mine", theme_palettes.Origin.CUSTOM, Path("/x/Mine.json"), pair
            ),
            theme_palettes.PaletteEntry(
                "Old", theme_palettes.Origin.LEGACY, Path("/x/Old.json"), pair
            ),
        )
    )
    monkeypatch.setattr(theme_palettes, "discover", lambda **_options: found)
    editing = editing_of(adapter)
    seen = topics(adapter)
    assert editing.palettes() == [], "the list loads off GTK on first use"
    spin_until(lambda: "colors" in seen, timeout=5)
    assert [p.key for p in editing.palettes()] == ["builtin:Nord", "community:Nord", "custom:Mine"]
    assert [p.key for p in editing.palettes("community")] == ["community:Nord"]
    builtin, community = editing.palettes("builtin")[0], editing.palettes("community")[0]
    assert builtin.strip(True) == [], "Noctalia does not say what a built-in looks like"
    assert community.strip(True) == [colour.hex for colour in pair.dark.colours.values()]

    target = wid(library, 0)
    editing.set_wallpaper_colors(target, palette=community.key)
    backend.run_authoring()
    view = adapter.wallpaper(target)
    assert view.palette == "community:Nord"
    assert adapter.wallpaper_swatches(view) == community.strip(True)


# -- still, reset, try again, regenerate ----------------------------------------------


def _with_scene(backend: EditingApplication, adapter: RealAppState) -> MediaItem:
    session = backend.session
    root = session.library.roots[0]
    scene = MediaItem(
        path=root / "workshop" / "123", kind=Kind.SCENE, size=0, mtime=0, scene="123",
        provider=scan.WORKSHOP_PROVIDER, title="Toothless",
    )  # fmt: skip
    session.adopt_library(
        Library(roots=session.library.roots, items=(*session.library.items, scene)),
        reconcile_workshop=False,
    )
    adapter.reload()
    return scene


def test_regenerate_asks_for_a_scene_still_only(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    scene = _with_scene(backend, adapter)
    assert editing.choices(str(scene.path)).regenerate
    assert not editing.choices(wid(library, 3)).regenerate, "a video's still is automatic"
    editing.regenerate_still(wid(library, 3))
    editing.regenerate_still(str(scene.path))
    assert backend.regenerated == [scene.path]
    assert backend.reports == ["Capturing a new still for Toothless in the background"]


def test_reset_forgets_every_choice_and_says_so(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    item, target = library[1], wid(library, 1)
    assert not editing.choices(target).customized
    editing.reset_wallpaper(target)
    backend.run_authoring()
    assert backend.reports == [f"{item.name} had nothing to reset"]

    editing.set_wallpaper_colors(target, palette="builtin:Nord", theme_mode="dark")
    backend.run_authoring()
    assert editing.choices(target).customized
    seen = topics(adapter)
    editing.reset_wallpaper(target)
    backend.run_authoring()
    assert durable(item) == pairings.PalettePolicy()
    assert not editing.choices(target).customized
    view = adapter.wallpaper(target)
    assert (view.color_mode, view.theme_mode) == ("adaptive", "keep")
    assert seen == ["library"] and backend.reports[-1] == f"{item.name} is back to its defaults"


def test_a_still_chosen_by_hand_must_be_a_library_still(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    video, target = library[3], wid(library, 3)
    editing.choose_still(target, str(video.path.parent / "not-indexed.png"))
    assert not backend.authoring and "not an indexed library item" in backend.reports[-1]
    editing.choose_still(target, str(library[0].path))
    backend.run_authoring()
    record = backend.session.pairings.get(pairings.Identity.of(video))
    assert record is not None and record.still == library[0].path
    assert editing.choices(target).still
    assert adapter.wallpaper(target).still_note == "Chosen by hand: alpine.png"
    editing.choose_still(target, None)
    backend.run_authoring()
    assert not editing.choices(target).still
    assert backend.reports[-1] == "rain uses its automatic still again"


def test_try_again_clears_the_warning_and_republishes(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    item, target = library[3], wid(library, 3)
    backend.session.pairings.choose_palette(item, pairings.PalettePolicy("keep"))
    backend.session.pairings.mark_borked(item, "codec missing", "runtime")
    adapter.reload()
    assert "codec missing" in adapter.apply_blocked(target), "Apply is off, with the reason"
    editing.retry_wallpaper(target)
    backend.run_authoring()
    assert adapter.wallpaper(target).problem == ""
    assert adapter.apply_blocked(target) == ""
    assert backend.pairing_changes == [item.path], "the configuration drops its taboo"
    assert durable(item).keeps_palette, "the choices made for it stay"
    assert backend.reports == ["rain will be tried again"]


# -- favorites --------------------------------------------------------------------------


def test_bulk_favorites_are_one_task_and_one_write(
    adapter: RealAppState,
    backend: EditingApplication,
    library: tuple[MediaItem, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editing = editing_of(adapter)
    backend.session.favourites.add(library[0].path)
    writes: list[int] = []
    original = favourites.save

    def counted(*arguments: Any, **keywords: Any) -> None:
        writes.append(1)
        original(*arguments, **keywords)

    monkeypatch.setattr(favourites, "save", counted)
    undo = editing.favorite_wallpapers([wid(library, i) for i in range(4)])
    assert undo is None, "saved asynchronously: the adapter says so when it lands"
    assert len(backend.authoring) == 1
    backend.run_authoring()
    assert len(writes) == 1
    assert backend.session.favourites.paths == {item.path for item in library}
    assert all(view.favorite for view in adapter.wallpapers)
    assert backend.reports == ["Added 3 wallpapers to favorites"]
    assert favourites.Store.open().paths == {item.path for item in library}


def test_add_many_marks_only_what_is_new(tmp_path: Path) -> None:
    store = favourites.Store.open(tmp_path / "favourites.json")
    store.add(tmp_path / "a.png")
    assert store.add_many([tmp_path / "a.png", tmp_path / "b.png", tmp_path / "b.png"]) == 1
    assert favourites.Store.open(tmp_path / "favourites.json").paths == {
        tmp_path / "a.png",
        tmp_path / "b.png",
    }
    assert store.add_many([]) == 0


# -- removal: by who owns the file -----------------------------------------------------


def _owned(
    backend: EditingApplication, adapter: RealAppState, library: tuple[MediaItem, ...]
) -> MediaItem:
    session = backend.session
    managed = replace(library[0], ownership=Ownership.MANAGED, provider="wallhaven")
    session.adopt_library(
        Library(roots=session.library.roots, items=(managed, *library[1:])),
        reconcile_workshop=False,
    )
    adapter.reload()
    return managed


def test_removal_is_named_for_who_owns_the_file(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    managed = _owned(backend, adapter, library)
    scene = _with_scene(backend, adapter)
    assert editing.removal(str(managed.path)) == Removal("delete")
    assert editing.removal(wid(library, 1)) == Removal("trash")
    assert editing.removal(str(scene.path)).how == ""
    assert "Steam" in editing.removal(str(scene.path)).reason
    assert editing.removal("/no/such") == Removal("", "It is no longer in the library")

    editing.remove_wallpapers([str(managed.path)])
    editing.remove_wallpapers([wid(library, 1)])
    editing.remove_wallpapers([str(scene.path)])
    assert backend.removals == [(managed.path, False), (library[1].path, True)]
    assert backend.reports == [
        "Toothless can\u2019t be removed here. " + editing.removal(str(scene.path)).reason
    ]


def test_files_outside_the_folders_and_dotfiles_are_never_removed(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    session = backend.session
    root = session.library.roots[0]
    outside = replace(library[1], path=root.parent / "elsewhere.png")
    hidden = replace(library[2], path=root / ".wall-in-one-removal-1" / "kept.png")
    session.adopt_library(
        Library(roots=session.library.roots, items=(*library, outside, hidden)),
        reconcile_workshop=False,
    )
    adapter.reload()
    assert editing.removal(str(outside.path)).how == ""
    assert "outside your library folders" in editing.removal(str(outside.path)).reason
    assert editing.removal(str(hidden.path)).how == ""
    editing.remove_wallpapers([str(outside.path)])
    editing.remove_wallpapers([str(hidden.path)])
    assert backend.removals == []


def test_one_removal_at_a_time_and_no_undo(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    assert editing.remove_wallpapers([wid(library, 0), wid(library, 1)]) is None
    assert backend.removals == []
    assert backend.reports == [
        "Remove one wallpaper at a time: each removal rescans the library first"
    ]
    assert editing.remove_wallpapers([wid(library, 0)]) is None, "nothing could restore it"
    assert backend.removals == [(library[0].path, True)]


def test_the_removal_outcome_is_said_as_the_classic_window_says_it(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    editing.remove_wallpapers([wid(library, 1)])
    finish = backend.removal_finish[0]
    finish(
        RemovalResult(library[1], trash=True, committed=False, error_message="the Trash is full")
    )
    assert backend.reports[-1] == "Nothing was moved: the Trash is full"
    finish(
        RemovalResult(
            library[1],
            trash=True,
            committed=True,
            physical=manage.Trashed(library[1], Path("/trash/city-dusk.png")),
        )
    )
    assert backend.reports[-1] == "city-dusk moved to the Trash"


def test_a_removal_that_would_empty_a_playlist_in_use_is_refused(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    session = backend.session
    made = session.playlists.create("Frog day")
    session.playlists.add(made.id, library[1].path)
    session.schedules.add(made.id)
    editing.remove_wallpapers([wid(library, 1)])
    assert backend.removals == []
    assert backend.reports == [
        "city-dusk was not removed: \u201cFrog day\u201d (used by a schedule rule) would "
        "have nothing left to play. Add another wallpaper there first."
    ]
    session.playlists.add(made.id, library[2].path)
    editing.remove_wallpapers([wid(library, 1)])
    assert backend.removals == [(library[1].path, True)]


def test_removing_the_last_wallpaper_of_your_pick_in_use_is_refused(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    """The Playlists page's rule, through its part: a hidden playlist counts too."""
    editing = editing_of(adapter)
    session = backend.session
    picked = session.playlists.set_singleton(QUICK_CHOICE_ID, "Quick choice", library[0].path)
    session.schedules.add(picked.id)
    editing.remove_wallpapers([wid(library, 0)])
    assert backend.removals == []
    assert backend.reports == [
        "alpine was not removed: \u201cYour pick\u201d (used by a schedule rule) would have "
        "nothing left to play. Add another wallpaper there first."
    ]


# -- playlists: the Playlists page's own saves --------------------------------------------


def test_adding_several_to_a_playlist_is_one_save_through_the_playlists_part(
    adapter: RealAppState,
    backend: EditingApplication,
    library: tuple[MediaItem, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editing = editing_of(adapter)
    seen = toasts(adapter)
    made = backend.session.playlists.create("Frog day")
    backend.session.playlists.add(made.id, library[0].path)
    adapter.reload_playlists()
    batches: list[str] = []
    original = playlists.Store.insert_entries

    def counted(store: playlists.Store, pid: str, placed: Any) -> playlists.Playlist:
        batches.append(pid)
        return original(store, pid, placed)

    monkeypatch.setattr(playlists.Store, "insert_entries", counted)
    editing.add_to_playlist(made.id, [wid(library, 1), wid(library, 2)])
    assert backend.tasks == 1
    backend.run_authoring()
    assert batches == [made.id], "one all-or-nothing insert"
    stored = playlists.Store.open().get(made.id)
    assert stored is not None
    assert [entry.path for entry in stored.entries] == [library[i].path for i in (0, 1, 2)]
    assert backend.playlist_changes == 1
    assert adapter.playlist(made.id).entries == tuple(wid(library, i) for i in (0, 1, 2))
    assert seen == [("Added 2 wallpapers to \u201cFrog day\u201d", None)]


def test_a_wallpaper_playback_skips_is_never_added(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    made = backend.session.playlists.create("Frog day")
    backend.session.pairings.mark_borked(library[3], "codec missing", "runtime")
    adapter.reload()
    editing.add_to_playlist(made.id, [wid(library, 3), wid(library, 0)])
    assert backend.tasks == 0
    assert backend.reports == [
        "Playback unavailable for rain; it can\u2019t be added: codec missing"
    ]


def test_a_wallpaper_skipped_while_its_add_waited_is_not_added(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    made = backend.session.playlists.create("Frog day")
    editing.add_to_playlist(made.id, [wid(library, 1)])
    backend.session.pairings.mark_borked(library[1], "renderer crashed", "runtime")
    backend.run_authoring()
    stored = playlists.Store.open().get(made.id)
    assert stored is not None and stored.entries == ()
    assert backend.reports == [
        "\u201ccity-dusk\u201d was not added to \u201cFrog day\u201d; nothing changed: "
        "playback became unavailable for city-dusk; it can\u2019t be added: renderer crashed"
    ]


def test_your_pick_and_all_wallpapers_are_never_targets(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    picked = backend.session.playlists.set_singleton(
        QUICK_CHOICE_ID, "Quick choice", library[0].path
    )
    display_pick = playlists.display_quick_choice_id("DP-1")
    backend.session.playlists.set_display_singleton("DP-1", library[0].path)
    adapter.reload_playlists()
    assert not any(p.id.startswith(QUICK_CHOICE_ID) for p in adapter.playlists)
    for pid in (picked.id, display_pick, "all-media"):
        editing.add_to_playlist(pid, [wid(library, 1)])
    assert backend.tasks == 0
    assert backend.reports == ["Wallpapers can\u2019t be added to that playlist"] * 3


def test_create_and_add_is_one_task_and_never_leaves_an_empty_playlist(
    adapter: RealAppState, backend: EditingApplication, library: tuple[MediaItem, ...]
) -> None:
    editing = editing_of(adapter)
    seen = toasts(adapter)
    backend.session.pairings.mark_borked(library[3], "codec missing", "runtime")
    adapter.reload()
    assert editing.create_playlist("Rainy", [wid(library, 3), wid(library, 1)]) == ""
    assert backend.tasks == 0, "a skipped one refuses it: no playlist that only looks full"
    assert backend.reports == [
        "Playback unavailable for rain; it can\u2019t be added: codec missing"
    ]

    pid = editing.create_playlist("Evening", [wid(library, 1), wid(library, 2)])
    assert pid and backend.tasks == 1
    backend.run_authoring()
    stored = playlists.Store.open().get(pid)
    assert stored is not None and stored.name == "Evening"
    assert [entry.path for entry in stored.entries] == [library[1].path, library[2].path]
    assert adapter.has_playlist(pid)
    assert seen == [("Created \u201cEvening\u201d with 2 wallpapers", None)]

    # Skipped between the click and the save: refused before anything is written.
    late = editing.create_playlist("Late", [wid(library, 0)])
    backend.session.pairings.mark_borked(library[0], "renderer crashed", "runtime")
    backend.run_authoring()
    assert playlists.Store.open().get(late) is None
    assert playlists.Store.open().by_name("Late") is None

    assert editing.create_playlist("Lost", ["/no/such.png"]) == ""
    assert backend.reports[-1].startswith("No playlist was created")


# -- read-only -------------------------------------------------------------------------


def test_a_newer_store_turns_every_edit_off_with_the_reason(
    library: tuple[MediaItem, ...],
) -> None:
    target = paths.app_state_dir() / pairings.STATE_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"version": 99, "pairings": []}\n')
    session = _session(library)
    backend = EditingApplication(session)
    adapter = RealAppState(backend, UiPrefsKeeper(backend.window_report), authoring=backend)
    try:
        editing = editing_of(adapter)
        blocked = editing.editing_blocked()
        assert blocked == (
            "Changes to wallpapers are off while pairings.json is from a newer version of "
            "Wall-in-One"
        )
        assert "library changes" in adapter.read_only_notice()
        target_id = wid(library, 1)
        editing.set_wallpaper_colors(target_id, mode="keep")
        editing.add_to_playlist("x", [target_id])
        editing.favorite_wallpapers([target_id])
        editing.remove_wallpapers([target_id])
        editing.reset_wallpaper(target_id)
        editing.retry_wallpaper(target_id)
        assert editing.create_playlist("New", [target_id]) == ""
        assert backend.tasks == 0 and backend.removals == []
        assert backend.reports == [blocked] * 7
    finally:
        adapter.close()
        session.shutdown()


# -- folders ---------------------------------------------------------------------------


def test_a_folder_is_added_through_the_settings_path_and_scanned_once_saved(
    adapter: RealAppState, backend: EditingApplication, tmp_path: Path
) -> None:
    folders = folders_of(adapter)
    settings = adapter.settings_access
    assert isinstance(settings, real_settings.RealSettings), "one path for adding a folder"
    first = backend.requested.roots[0]
    walls = tmp_path / "Walls"
    assert folders.add_library_folder(str(walls)) is None, "saved asynchronously, no Undo"
    assert backend.reports == [], "nothing is said until settings.toml has it"
    backend.run_authoring()
    assert backend.requested.roots == (first, walls)
    assert backend.reports == ["Scanning Walls"]

    folders.add_library_folder(str(walls))
    assert not backend.authoring
    assert backend.reports[-1] == "Walls is already in the library"

    folders.rescan()
    assert backend.refreshed == 1


def test_read_only_settings_add_no_folder(
    adapter: RealAppState, backend: EditingApplication, tmp_path: Path
) -> None:
    backend.unknown_keys = ("window_style",)
    folders = folders_of(adapter)
    assert folders.add_folder_blocked().startswith("Settings are read-only.")
    folders.add_library_folder(str(tmp_path / "Walls"))
    assert not backend.authoring
    assert backend.reports[-1].startswith("Library folders were not saved; nothing changed:")
