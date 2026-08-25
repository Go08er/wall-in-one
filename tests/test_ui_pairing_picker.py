"""The pairing editor previews what it authors without hiding reusable stills."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

import wall_in_one.theme.palettes as palettes  # noqa: E402
import wall_in_one.ui.pairings_page as pairings_page  # noqa: E402
from wall_in_one import config  # noqa: E402
from wall_in_one.library import pairings, scan  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem, Ownership  # noqa: E402
from wall_in_one.session import Session  # noqa: E402
from wall_in_one.theme import noctalia  # noqa: E402
from wall_in_one.theme.palette import Palette, PalettePair  # noqa: E402
from wall_in_one.ui.palette_catalog import PaletteCatalog  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class QuietThumbnailLoader:
    def __init__(self, **_arguments: object) -> None: ...

    def request(self, _item: MediaItem, _callback: Any) -> None: ...

    def shutdown(self) -> None: ...


class QuietPreviewLoader:
    def __init__(self, **_arguments: object) -> None: ...

    def request(self, _image: Path, _scheme: str, _callback: Any) -> None: ...

    def shutdown(self) -> None: ...


class PairingApp:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = session.settings
        self.resolved_palette = None
        self.changes = 0
        self.messages: list[str] = []

    def pairing_changed(self, _item: MediaItem) -> None:
        self.changes += 1

    def window_report(self, message: str) -> None:
        self.messages.append(message)

    def authoring_action_async(
        self,
        work: Any,
        finish: Any,
        *,
        prepare: Any = None,
        failure: Any = None,
        **_keywords: object,
    ) -> bool:
        """Run the editor's worker contract synchronously in this tiny fake."""
        try:
            result = (prepare() if prepare is not None else work)()
        except Exception as error:
            if failure is not None:
                failure(str(error))
            return False
        finish(result)
        return True

    def prepare_pairing_mutation(self, item: MediaItem, mutation: Any) -> Any:
        current = self.session.library.find(item.path)
        assert current is not None
        return lambda: mutation(self.session.pairings, current)

    def prepare_still_pairing_mutation(
        self,
        item: MediaItem,
        still: Path | None,
        mutation: Any,
    ) -> Any:
        current = self.session.library.find(item.path)
        assert current is not None

        def run() -> SimpleNamespace:
            effective = still
            if effective is None:
                effective = pairings.synthesize(current, self.session.library.roots).still
            record = mutation(self.session.pairings, current, still)
            return SimpleNamespace(item=current, record=record, effective_still=effective)

        return run

    def prepare_pairing_reset(self, item: MediaItem) -> Any:
        current = self.session.library.find(item.path)
        assert current is not None

        def run() -> SimpleNamespace:
            effective = pairings.synthesize(current, self.session.library.roots).still
            changed = self.session.pairings.reset(current)
            return SimpleNamespace(item=current, changed=changed, effective_still=effective)

        return run

    def adopt_pairing_still(self, item: MediaItem, effective: Path | None) -> MediaItem:
        current = self.session.library.find(item.path)
        assert current is not None
        adopted = current.with_still(effective) if current.is_moving else current
        library = self.session.library
        self.session.adopt_library(
            Library(
                roots=library.roots,
                items=tuple(
                    adopted if candidate.path == current.path else candidate
                    for candidate in library.items
                ),
                skipped=library.skipped,
                still_inventory=library.still_inventory,
            ),
            reconcile_workshop=False,
        )
        return adopted


def _item(path: Path, kind: Kind = Kind.STILL) -> MediaItem:
    return MediaItem(path=path, kind=kind, size=1, mtime=1)


def _session(
    tmp_path: Path,
    *,
    item: MediaItem,
    stills: tuple[MediaItem, ...],
) -> Session:
    library = Library(
        roots=(tmp_path,),
        items=(item, *stills),
        still_inventory=stills,
    )
    session = Session(
        config.Settings(roots=(tmp_path,)),
        scanner=lambda _roots: library,
        pairing_store=pairings.Store(path=tmp_path / "pairings.json"),
    )
    session.refresh()
    return session


def _put_scroll_at(scroller: Gtk.ScrolledWindow, value: float) -> None:
    scroller.get_vadjustment().configure(value, 0.0, 200.0, 1.0, 10.0, 20.0)


def test_borked_pairing_is_obvious_and_offers_removal_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietThumbnailLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietPreviewLoader)
    picture = tmp_path / "paper.png"
    picture.write_bytes(b"image")
    media = _item(picture)
    session = _session(tmp_path, item=media, stills=())
    session.pairings.mark_borked(media, "linux-wallpaperengine crashed", "renderer-crash")
    application = PairingApp(session)
    removals: list[MediaItem] = []
    page = pairings_page.PairingsPage(cast(Any, application), lambda: None, removals.append)
    page.edit(session, media)

    assert page._health_group is not None
    assert page._health_action is not None
    assert "Borked" in page._health_group.get_title()
    assert "Playback, Quick choice, and transport retry are disabled" in (
        page._health_group.get_description() or ""
    )
    assert page._health_action.get_label() == "Move to Trash"
    page._health_action.emit("clicked")
    assert removals == [media]
    assert session.pairings.health(pairings.Identity.of(media)).is_borked

    page.shutdown()
    session.shutdown()


def test_borked_workshop_item_honestly_defers_uninstall_to_steam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietThumbnailLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietPreviewLoader)
    scene_path = tmp_path / "steamapps" / "workshop" / "content" / "431960" / "42"
    scene_path.mkdir(parents=True)
    scene = MediaItem(
        path=scene_path,
        kind=Kind.SCENE,
        size=1,
        mtime=1,
        ownership=Ownership.USER,
        provider=scan.WORKSHOP_PROVIDER,
        scene="42",
    )
    session = _session(tmp_path, item=scene, stills=())
    session.pairings.mark_borked(scene, "scene crashed", "renderer-crash")
    page = pairings_page.PairingsPage(
        cast(Any, PairingApp(session)),
        lambda: None,
        lambda _item: pytest.fail("must not delete Workshop"),
    )

    page.edit(session, scene)

    assert page._health_action is None
    assert page._health_row is not None
    subtitle = page._health_row.get_subtitle() or ""
    assert "uninstall this Workshop item in Steam" in subtitle
    assert "reinstall starts clean" in subtitle

    page.shutdown()
    session.shutdown()


def test_still_picker_is_searchable_bounded_and_survives_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietThumbnailLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietPreviewLoader)
    video_path = tmp_path / "motion" / "clip.mp4"
    video_path.parent.mkdir()
    video_path.write_bytes(b"video")
    video = _item(video_path, Kind.VIDEO)
    stills: list[MediaItem] = []
    for index in range(60):
        group = "group-b" if index == 57 else "group-a"
        path = tmp_path / group / f"cover-{index:02d}.png"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"image")
        stills.append(_item(path))

    session = _session(tmp_path, item=video, stills=tuple(stills))
    application = PairingApp(session)
    page = pairings_page.PairingsPage(cast(Any, application), lambda: None)
    page.edit(session, video)
    root = Gtk.Window()
    root.set_child(page)

    assert len(page._still_cards_by_path) == pairings_page.STILL_PICKER_PAGE_SIZE
    assert page._still_more.get_visible()
    search = page._still_search
    editor = page._editor.get_first_child()
    search.set_text("group-b")
    search.emit("search-changed")
    search.set_position(4)
    _put_scroll_at(page._still_scroll, 23.0)
    chosen = stills[57]
    card = page._still_cards_by_path[chosen.path]
    card.set_active(True)

    saved = session.pairings.get(pairings.Identity.of(video))
    assert saved is not None and saved.still == chosen.path
    assert application.changes == 1
    # The store changes before the asynchronous library rescan lands.  A
    # refresh in that window must compare the same canonical item-plus-pairing
    # fingerprint used after the scan lands, or one of those two phases tears
    # down the editor even though the authored choice is unchanged.
    page.refresh(session)
    assert page._editor.get_first_child() is editor
    assert page._still_search is search
    assert search.get_text() == "group-b"
    assert search.get_position() == 4
    assert page._still_scroll.get_vadjustment().get_value() == 23.0

    # Model the asynchronous rescan that follows a pairing edit. The same
    # widgets are reconciled in place rather than rebuilding the editor.
    focused = search.grab_focus()
    focused_widget = root.get_focus()
    session.adopt_library(
        Library(
            roots=(tmp_path,),
            items=(video.with_still(chosen.path), *stills),
            still_inventory=tuple(stills),
        )
    )
    page.refresh(session)
    assert page._editor.get_first_child() is editor
    assert page._still_search is search
    assert page._still_cards_by_path[chosen.path] is card
    assert search.get_text() == "group-b"
    assert search.get_position() == 4
    assert page._still_scroll.get_vadjustment().get_value() == 23.0
    if focused:
        assert root.get_focus() is focused_widget

    page.shutdown()
    root.destroy()
    session.shutdown()


def test_palette_swatches_follow_the_pairing_mode_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietThumbnailLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietPreviewLoader)
    picture = tmp_path / "wall.png"
    picture.write_bytes(b"image")
    media = _item(picture)
    session = _session(tmp_path, item=media, stills=(media,))
    application = PairingApp(session)
    pair = PalettePair(
        dark=Palette.from_mapping("dark", {"primary": "#000000"}),
        light=Palette.from_mapping("light", {"primary": "#ffffff"}),
    )
    entry = palettes.PaletteEntry(
        name="Test palette",
        origin=palettes.Origin.CUSTOM,
        path=None,
        colours=pair,
    )
    monkeypatch.setattr(
        pairings_page,
        "swatch_strip",
        lambda palette, **_arguments: Gtk.Label(label=palette.mode),
    )

    catalog = PaletteCatalog(initial=palettes.Discovery(entries=(entry,)))
    page = pairings_page.PairingsPage(cast(Any, application), lambda: None, palette_catalog=catalog)
    page.edit(session, media)
    box = page._adaptive_boxes["custom:Test palette"]
    first = cast(Gtk.Label, box.get_first_child())
    assert first.get_label() == "dark"

    page._mode_row.set_selected(2)  # Light

    assert page._adaptive_boxes["custom:Test palette"] is box
    second = cast(Gtk.Label, box.get_first_child())
    assert second.get_label() == "light"
    assert second is not first
    saved = session.pairings.get(pairings.Identity.of(media))
    assert saved is not None and saved.palette.mode is pairings.Mode.LIGHT

    page.shutdown()
    catalog.shutdown()
    session.shutdown()


def test_failed_palette_write_restores_mode_and_policy_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietThumbnailLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietPreviewLoader)
    picture = tmp_path / "wall.png"
    picture.write_bytes(b"image")
    media = _item(picture)
    session = _session(tmp_path, item=media, stills=(media,))
    application = PairingApp(session)
    page = pairings_page.PairingsPage(cast(Any, application), lambda: None)
    page.edit(session, media)

    durable = session.pairings.resolve(media, session.library.roots).palette
    original_name = durable.adaptive_scheme(application.settings.preview_scheme)
    original = page._palette_buttons[f"adaptive:{original_name}"]
    replacement_name = next(name for name in noctalia.ALL_SCHEMES if name != original_name)
    replacement = page._palette_buttons[f"adaptive:{replacement_name}"]

    def fail(_item: MediaItem, _policy: pairings.PalettePolicy) -> None:
        raise pairings.PairingError("local-io", "disk full")

    monkeypatch.setattr(session.pairings, "choose_palette", fail)
    page._mode_row.set_selected(2)  # Light

    assert page._mode_row.get_selected() == 0  # Keep current mode
    assert original.get_active()
    assert session.pairings.get(pairings.Identity.of(media)) is None

    replacement.set_active(True)

    assert original.get_active()
    assert not replacement.get_active()
    assert session.pairings.get(pairings.Identity.of(media)) is None
    assert application.changes == 0
    assert len(application.messages) == 2
    assert all("not saved; nothing changed" in message for message in application.messages)

    page.shutdown()
    session.shutdown()


def test_file_picker_refuses_an_existing_but_unindexed_still(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietThumbnailLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietPreviewLoader)
    video = _item(tmp_path / "clip.mp4", Kind.VIDEO)
    manual = tmp_path / "outside" / "chosen.png"
    manual.parent.mkdir()
    manual.write_bytes(b"image")
    session = _session(tmp_path, item=video, stills=())
    application = PairingApp(session)
    page = pairings_page.PairingsPage(cast(Any, application), lambda: None)
    page.edit(session, video)

    page._choose_picker_still(video, manual)

    assert session.pairings.get(pairings.Identity.of(video)) is None
    assert application.changes == 0
    assert len(application.messages) == 1
    assert "not an indexed library item" in application.messages[0]
    assert "add its folder in Settings" in application.messages[0]
    assert "latest library refresh" in (page._manual_still.get_subtitle() or "")
    page.shutdown()
    session.shutdown()


def test_file_picker_refuses_an_indexed_video_as_a_still(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietThumbnailLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietPreviewLoader)
    source = _item(tmp_path / "source.mp4", Kind.VIDEO)
    candidate = _item(tmp_path / "candidate.mp4", Kind.VIDEO)
    session = _session(tmp_path, item=source, stills=(candidate,))
    application = PairingApp(session)
    page = pairings_page.PairingsPage(cast(Any, application), lambda: None)
    page.edit(session, source)

    page._choose_picker_still(source, candidate.path)

    assert session.pairings.get(pairings.Identity.of(source)) is None
    assert application.changes == 0
    assert len(application.messages) == 1
    assert "indexed as video, not as a still image" in application.messages[0]
    page.shutdown()
    session.shutdown()
