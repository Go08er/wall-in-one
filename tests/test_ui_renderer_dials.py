"""The renderer dials in both interfaces: Settings, and each wallpaper's own settings.

docs/renderer-settings.md. Settings, classic and new, has a row for every
global dial, each saving its own key, greyed out while it would do nothing.
A wallpaper's own settings (the classic pairing editor, the new interface's
details) show each dial as "Default (…)" until it is changed, then
"Overridden", with a button that takes it back; a scene's Wallpaper Engine
properties start from its declared values. Steam's folder is a fixture.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

import wall_in_one.ui.pairings_page as pairings_page  # noqa: E402
from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_codex030_round1b import _descendants  # noqa: E402
from tests.test_ui_next_settings import Opened, _close, _open  # noqa: E402
from tests.test_ui_next_slice import finish, library_page, run  # noqa: E402
from tests.test_ui_next_window import Step, sandboxed_runtime, settled  # noqa: E402
from tests.test_ui_pairing_picker import (  # noqa: E402
    PairingApp,
    QuietPreviewLoader,
    QuietThumbnailLoader,
)
from wall_in_one import config, dials, paths, runtime_compatibility  # noqa: E402
from wall_in_one.dials import SceneProperty  # noqa: E402
from wall_in_one.library import pairings, workshop  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.library.scan import WORKSHOP_PROVIDER  # noqa: E402
from wall_in_one.session import Session  # noqa: E402
from wall_in_one.ui import dial_rows, preferences  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402
from wall_in_one.ui.stills import StillMaker  # noqa: E402
from wall_in_one.wallpaper import renderer  # noqa: E402

GENERAL = {
    "properties": {
        "bloom": {"type": "bool", "text": "Bloom", "value": True, "order": 1},
        "rain": {"type": "slider", "text": "Rain", "value": 0.5, "min": 0, "max": 1, "step": 0.1},
        "mood": {
            "type": "combo",
            "text": "Mood",
            "value": "calm",
            "options": [{"value": "calm", "label": "Calm"}, {"value": "storm", "label": "Storm"}],
        },
        "picture": {"type": "file", "text": "Picture", "value": "/etc/passwd"},
    }
}


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Opened]:
    sandboxed_runtime(monkeypatch, tmp_path)
    # Asked of the live service for schema 7; it would consult systemd.
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    shown = _open(monkeypatch)
    try:
        yield shown
    finally:
        _close(shown)


def _another(dial: dials.Dial, value: dials.Value) -> dials.Value:
    """A different value ``dial`` takes than ``value``."""
    if dial.kind == "bool":
        return not value
    if dial.kind == "choice":
        keys = [choice for choice, _label in dial.choices]
        return keys[(keys.index(value) + 1) % len(keys)]
    candidate = dial.minimum + dial.step * 3
    return round(candidate, 2) if dial.kind == "float" else int(candidate)


def _reset_of(row: Gtk.Widget) -> Gtk.Button:
    """The button on a wallpaper's own row that takes it back to the default."""
    return next(
        widget
        for widget in _descendants(row)
        if isinstance(widget, Gtk.Button) and widget.get_icon_name() == "edit-undo-symbolic"
    )


def _own(item_path: Path, kind: Kind, scene: str = "") -> pairings.RendererOverrides:
    """What pairings.json holds for the wallpaper at ``item_path``, read afresh."""
    item = MediaItem(item_path, kind, 1, 1, scene=scene)
    record = pairings.Store.open().get(pairings.Identity.of(item))
    return record.renderer if record is not None else pairings.RendererOverrides()


# -- Settings ---------------------------------------------------------------------------


def test_classic_settings_saves_every_dial_and_greys_out_one_that_does_nothing(
    opened: Opened,
) -> None:
    page = preferences.PreferencesPage(opened.application)
    shown = {dial.setting for dial, _row in page._dial_rows}
    assert shown == {dial.setting for dial in dials.NEWER_GLOBAL}
    for dial, row in page._dial_rows:
        assert row.get_title() == dial.label
        before = config.load()
        wanted = _another(dial, getattr(before, dial.setting))
        dial_rows.show_value(dial, row, wanted)

        def saved(setting: str = dial.setting, want: Any = wanted) -> bool:
            return bool(getattr(config.load(), setting) == want)

        opened.wait(f"{dial.setting} to be saved", saved)
        changes: dict[str, Any] = {dial.setting: wanted}
        assert config.load() == replace(before, **changes), dial.setting

    rows = {dial.setting: row for dial, row in page._dial_rows}
    assert rows["video_hidden_trigger"].get_sensitive()
    page._when_hidden.set_selected(renderer.WHEN_HIDDEN_CHOICES.index("play"))
    opened.wait("play to be saved", lambda: config.load().video_when_hidden == "play")
    assert not rows["video_hidden_trigger"].get_sensitive(), "mpvpaper pauses nothing then"
    assert rows["scene_pause_only_active"].get_sensitive() is (
        config.load().scene_pause_when_covered
    )
    assert opened.reports == [], opened.reports


def test_the_new_settings_greys_out_a_dial_that_does_nothing(opened: Opened) -> None:
    trigger = opened.row("video_hidden_trigger")
    only_active = opened.row("scene_pause_only_active")
    assert trigger.get_sensitive() and only_active.get_sensitive()
    when_hidden = next(b for b in opened.page._bindings if b.key == "video_when_hidden")
    assert isinstance(when_hidden.row, Adw.ComboRow)
    when_hidden.row.set_selected([key for key, _label in when_hidden.choices].index("play"))
    opened.row("scene_pause_when_covered").set_active(False)
    opened.wait(
        "both to be saved",
        lambda: (
            config.load().video_when_hidden == "play" and not config.load().scene_pause_when_covered
        ),
    )
    spin_until(lambda: not trigger.get_sensitive() and not only_active.get_sensitive(), 5)
    assert opened.reports == [], opened.reports


# -- a wallpaper's own: the new interface's details -------------------------------------


def _install(content: Path, workshop_id: str, project: dict[str, object]) -> Path:
    directory = content / workshop_id
    directory.mkdir(parents=True)
    (directory / "project.json").write_text(json.dumps(project), encoding="utf-8")
    return directory


def test_the_details_set_a_wallpapers_own_dials_and_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    # No scene capture: the scan lists the scenes, nothing runs the engine.
    monkeypatch.setattr(StillMaker, "request", lambda *_arguments, **_keywords: None)
    folder = tmp_path / "wallpapers"
    clip = folder / "clip.mp4"
    clip.write_bytes(b"video")
    (folder / "clip-still.png").write_bytes(b"placeholder image bytes")
    # A still on record for the scenes, so the compiler keeps them.
    automatic = folder / "Wall-in-One" / "Automatic Stills"
    automatic.mkdir(parents=True)
    for workshop_id in ("1647046763", "2777242023"):
        (automatic / f"{workshop_id}.png").write_bytes(b"placeholder image bytes")
    config.update({"video_speed": 2.0})
    content = (
        tmp_path / "home" / ".local" / "share" / "Steam" / "steamapps" / "workshop" / "content"
    ) / workshop.APP_ID
    scene = _install(
        content,
        "1647046763",
        {"type": "scene", "title": "Toothless", "file": "scene.json", "general": GENERAL},
    )
    web = _install(
        content,
        "2777242023",
        {"type": "web", "title": "Electric Eye", "file": "index.html", "general": GENERAL},
    )
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        state = window.state
        yield (
            "the first scan, the video and the scenes included",
            lambda: (
                settled(application)
                and state.has_wallpaper(str(clip))
                and state.has_wallpaper(str(scene))
            ),
        )
        page = library_page(window)

        # A video: every dial a video may have, each following Settings.
        page.inspect(state.wallpaper(str(clip)))
        rows = page.inspector.renderer_rows
        assert set(rows) == {f"dial:video:{dial.key}" for dial in dials.PER_WALLPAPER["video"]}
        speed = rows["dial:video:speed"]
        assert isinstance(speed, Adw.SpinRow)
        assert speed.get_subtitle() == "Default (2.00)" and speed.get_value() == 2.0
        assert not _reset_of(speed).get_visible()
        assert page.inspector.renderer_reset is not None
        assert not page.inspector.renderer_reset.get_sensitive()
        speed.set_value(0.5)
        yield (
            "the video's own speed to be saved",
            lambda: settled(application) and _own(clip, Kind.VIDEO).dial("video", "speed") == 0.5,
        )
        speed = page.inspector.renderer_rows["dial:video:speed"]
        assert isinstance(speed, Adw.SpinRow)
        assert speed.get_subtitle() == "Overridden \u00b7 default 2.00"
        assert _reset_of(speed).get_visible()
        document = paths.runtime_config_path().read_text(encoding="utf-8")
        assert "video = { speed = 0.50 }" in document and "video_speed = 2.00" in document
        _reset_of(speed).emit("clicked")
        yield (
            "the speed back to the default",
            lambda: settled(application) and not _own(clip, Kind.VIDEO),
        )
        speed = page.inspector.renderer_rows["dial:video:speed"]
        assert isinstance(speed, Adw.SpinRow) and speed.get_subtitle() == "Default (2.00)"
        assert "video = {" not in paths.runtime_config_path().read_text(encoding="utf-8")

        # A scene: its dials, then what it declares, never a file.
        page.inspect(state.wallpaper(str(scene)))
        rows = page.inspector.renderer_rows
        assert {name for name in rows if name.startswith("property:")} == {
            "property:bloom",
            "property:rain",
            "property:mood",
        }
        bloom = rows["property:bloom"]
        assert isinstance(bloom, Adw.SwitchRow)
        assert bloom.get_active() and bloom.get_subtitle() == "Default (On)"
        bloom.set_active(False)
        mood = rows["property:mood"]
        assert isinstance(mood, Adw.ComboRow)
        mood.set_selected(1)
        yield (
            "the scene's own values to be saved",
            lambda: (
                settled(application)
                and _own(scene, Kind.SCENE, "1647046763").properties
                == (("bloom", "false"), ("mood", "storm"))
            ),
        )
        document = paths.runtime_config_path().read_text(encoding="utf-8")
        assert 'properties = { bloom = "false", mood = "storm" }' in document
        reset = page.inspector.renderer_reset
        assert reset is not None and reset.get_sensitive()
        reset.emit("clicked")
        yield (
            "every one of them back to the default",
            lambda: settled(application) and not _own(scene, Kind.SCENE, "1647046763"),
        )

        # A web page: no properties, no sound; it says why.
        page.inspect(state.wallpaper(str(web)))
        rows = page.inspector.renderer_rows
        assert set(rows) == {f"dial:scene:{key}" for key in dials.WEB_KEYS}
        labels = [
            widget.get_label()
            for widget in _descendants(page.inspector)
            if isinstance(widget, Gtk.Label)
        ]
        assert dials.WEB_PROPERTIES_NOTE in labels
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


# -- a wallpaper's own: the classic pairing editor --------------------------------------


def test_the_classic_pairing_editor_sets_a_wallpapers_own_dials_and_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietThumbnailLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietPreviewLoader)
    video = MediaItem(tmp_path / "clip.mp4", Kind.VIDEO, 1, 1, paired_still=tmp_path / "c.png")
    scene = MediaItem(
        tmp_path / "1647046763",
        Kind.SCENE,
        1,
        1,
        provider=WORKSHOP_PROVIDER,
        scene="1647046763",
        title="Toothless",
        workshop_type="scene",
        scene_settings=(
            SceneProperty("bloom", "bool", "Bloom", "true"),
            SceneProperty("caption", "textinput", "Caption", "hello"),
        ),
    )
    library = Library(roots=(tmp_path,), items=(video, scene))
    session = Session(
        config.Settings(roots=(tmp_path,), scene_muted=False, scene_volume=30),
        scanner=lambda _roots: library,
        pairing_store=pairings.Store(path=tmp_path / "pairings.json"),
    )
    session.refresh()
    application = PairingApp(session)
    page = pairings_page.PairingsPage(cast(Any, application), lambda: None)
    root = Gtk.Window()
    root.set_child(page)

    page.edit(session, scene)
    rows = page.renderer_rows
    assert {name for name in rows if name.startswith("dial:")} == {
        f"dial:scene:{dial.key}" for dial in dials.PER_WALLPAPER["scene"]
    }
    volume = rows["dial:scene:volume"]
    assert isinstance(volume, Adw.SpinRow)
    assert volume.get_subtitle() == "Default (30)" and volume.get_sensitive()
    muted = rows["dial:scene:muted"]
    assert isinstance(muted, Adw.SwitchRow)
    muted.set_active(True)
    assert session.pairings.get(pairings.Identity.of(scene)) is not None
    assert application.changes == 1
    rows = page.renderer_rows
    muted = rows["dial:scene:muted"]
    assert isinstance(muted, Adw.SwitchRow)
    assert muted.get_subtitle() == "Overridden \u00b7 default Off"
    assert not rows["dial:scene:volume"].get_sensitive(), "a silent scene has no volume"

    caption = rows["property:caption"]
    assert isinstance(caption, Adw.EntryRow)
    caption.set_text("good\x07night")
    caption.emit("apply")
    assert "error" in caption.get_css_classes(), "a control character is refused"
    caption.set_text("good night")
    caption.emit("apply")
    record = session.pairings.get(pairings.Identity.of(scene))
    assert record is not None and record.renderer.property("caption") == "good night"

    # "Reset this pairing" is the still and the colours; "Use defaults" is these.
    assert page.renderer_reset is not None and page.renderer_reset.get_sensitive()
    page.renderer_reset.emit("clicked")
    assert session.pairings.get(pairings.Identity.of(scene)) is None
    assert application.messages == ["Toothless\u2019s renderer settings are back to the defaults"]

    # A video's own speed: a spin through the range is one save, once it stops.
    page.edit(session, video)
    speed = page.renderer_rows["dial:video:speed"]
    assert isinstance(speed, Adw.SpinRow)
    before = application.changes
    for value in (1.1, 1.2, 1.3):
        speed.set_value(value)
    spin_until(lambda: application.changes > before, 5, what="the speed to be saved")
    assert application.changes == before + 1
    record = session.pairings.get(pairings.Identity.of(video))
    assert record is not None and record.renderer.dial("video", "speed") == 1.3
    root.destroy()
