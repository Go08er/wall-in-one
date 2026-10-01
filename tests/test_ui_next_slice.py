"""``--ui=next``'s first slice against real stores, a real library and a scripted runtime.

Each test drives `Application.run` with the new window, as
`test_ui_next_window` does: a sandboxed profile, a fake runtime behind
``client.send_runtime`` (here also recording each request's argument), no
Noctalia, a non-unique GApplication, and a deadline on every wait. The
library is real files: valid PNGs the thumbnail pipeline decodes.

The last test runs the committed golden profile (``tests/golden``): open,
idle through status polls, close, and nothing may be written beyond the
golden whitelist -- above all, no ``ui.toml``.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from tests.golden import harness, sandbox  # noqa: E402
from tests.golden.harness import Allowance, Change  # noqa: E402
from tests.golden.test_idle import first_start_writes  # noqa: E402
from tests.test_next_status_line import two_display_status  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402
    FakeRuntime,
    Step,
    application_lanes,
    run_application,
    sandboxed_runtime,
    settled,
)
from wall_in_one import paths, ui_prefs  # noqa: E402
from wall_in_one import thumbnails as thumbnail_cache  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.library import favourites, playlists  # noqa: E402
from wall_in_one.library.model import MediaItem  # noqa: E402
from wall_in_one.session import QUICK_CHOICE_ID  # noqa: E402
from wall_in_one.theme import css  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.grid import MEDIA_PAGE_SIZE  # noqa: E402
from wall_in_one.ui.next.library import LibraryPage  # noqa: E402
from wall_in_one.ui.next.shell import GlassDialog  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402
from wall_in_one.ui.stills import StillMaker  # noqa: E402

#: More than one page of cards: two placeholder files from the shared
#: sandbox (not decodable) and this many valid pictures.
PICTURES = 78
IDLE_SECONDS = 4.5  # two runtime status polls


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class Recorder:
    """Every runtime request the application sent, with its argument."""

    def __init__(self, fake: FakeRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sent: list[tuple[str, str | None]] = []
        self._fake = fake

        def send_runtime(
            verb: str,
            argument: str | None = None,
            *,
            timeout: float | None = None,
            cancellation: client.Cancellation | None = None,
        ) -> Response:
            self.sent.append((verb, argument))
            return fake.send_runtime(verb, argument, timeout=timeout, cancellation=cancellation)

        monkeypatch.setattr(client, "send_runtime", send_runtime)

    def answer(self, status: dict[str, object] | None) -> None:
        self._fake.answer(status)

    def requested(self, verb: str) -> list[str | None]:
        return [argument for sent, argument in self.sent if sent == verb]


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Recorder:
    fake = sandboxed_runtime(monkeypatch, tmp_path)
    for index in range(PICTURES):
        shade = (index * 37 % 200 + 30, index * 61 % 200 + 30, index * 13 % 200 + 30)
        (tmp_path / "wallpapers" / f"picture-{index:02d}.png").write_bytes(
            harness.tiny_png(shade, size=8)
        )
    return Recorder(fake, monkeypatch)


def picture(tmp_path: Path, index: int) -> str:
    """A library wallpaper's id: its path."""
    return str(tmp_path / "wallpapers" / f"picture-{index:02d}.png")


def status(stills: dict[str, str], routes: dict[str, str] | None = None) -> dict[str, object]:
    """Two displays (DP-1, HDMI-A-1) showing ``stills`` for ``routes`` (default: schedule)."""
    document = two_display_status()
    records = document["displays"]
    assert isinstance(records, list)
    for record in records:
        connector = record["connector"]
        record["still"] = stills[connector]
        record["entry_id"] = ""
        if (routes or {}).get(connector) == "manual":
            record["route_source"] = "manual"
            record["manual_override"] = True
            record["schedule_rule_id"] = None
            record["playlist_id"] = QUICK_CHOICE_ID
            record["playlist"] = "Quick choice"
    return document


def settled_after_a_refused_publication(application: Application) -> bool:
    """`settled`, where the runtime configuration was refused (a newer store).

    Compilation refuses while a store from a newer version exists, so the
    authoring request never becomes the compiled generation `settled` waits
    for: the refusal is the end of that work. Everything else is the same.
    """
    if settled(application):
        return True
    return (
        application._runtime_authoring_request is not None
        and application.authoring_ready
        and not application._authoring_active
        and not application._authoring_queue
        and not application._settings_authoring_running
        and not application._runtime_action_pending
        and application._library_scan_future is None
        and not application._theme_draining
        and not application._runtime_compile_pending
    )


def library_page(window: NextWindow) -> LibraryPage:
    page = window.pages["library"]
    assert isinstance(page, LibraryPage)
    return page


def finish(application: Application, window: NextWindow, lanes: list[object]) -> Iterator[Step]:
    yield "every tail to settle", lambda: settled(application)
    lanes.extend(application_lanes(application))
    window.close()


def run(application: Application, scenario: Iterator[Step], lanes: list[object]) -> None:
    try:
        assert run_application(application, scenario) == 0
    finally:
        for lane in lanes:
            lane.shutdown(wait=True)  # type: ignore[attr-defined]
    assert application._window is None


def test_the_library_pages_inspects_and_applies_through_the_runtime(
    runtime: Recorder, tmp_path: Path
) -> None:
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        page = library_page(window)
        yield (
            "the first scan",
            lambda: settled(application) and window.library_text == "80 wallpapers in the library",
        )

        # -- one page of cards, then another --------------------------------------
        assert len(page._cards) == MEDIA_PAGE_SIZE
        assert page._more.get_visible()
        assert page._more.get_label() == f"Show 8 more · {MEDIA_PAGE_SIZE} of 80 shown"
        assert page.count_text == "80 wallpapers"
        page._more.emit("clicked")
        assert len(page._cards) == 80 and not page._more.get_visible()
        yield (
            "the pictures to arrive",
            lambda: page._cards[picture(tmp_path, 3)].picture.paintable is not None,
        )

        # -- the inspector -----------------------------------------------------------
        target = window.state.wallpaper(picture(tmp_path, 5))
        page._on_card(target)  # what a click on its picture calls
        assert page.split.get_show_sidebar()
        inspector = page.inspector
        assert inspector.wallpaper is not None and inspector.wallpaper.id == target.id
        apply_button = inspector.apply_button
        assert apply_button is not None and apply_button.get_label() == "Apply to all displays"
        assert apply_button.get_sensitive()

        # -- the player bar says what the runtime says ---------------------------------
        runtime.answer(status({"DP-1": picture(tmp_path, 1), "HDMI-A-1": picture(tmp_path, 1)}))
        application.refresh_runtime_status_async()
        yield (
            "the scheduled wallpaper in the player bar",
            lambda: window.playerbar.reason_text == "Evening · from schedule",
        )
        assert "until" not in window.playerbar.reason_text, "the runtime reported no deadline"
        assert window.playerbar.title_text == "picture-01"
        assert window.state.current == {
            "DP-1": picture(tmp_path, 1),
            "HDMI-A-1": picture(tmp_path, 1),
        }
        assert window.status_text == "2 displays · playing Evening"

        # -- Apply: a Quick choice, saved, then sent; the bar waits for the runtime ------
        apply_button.emit("clicked")
        yield (
            "the Quick choice to be saved and sent",
            lambda: settled(application) and "quick-choice" in runtime.requested("playlist-use"),
        )
        chosen = application.session.playlists.get(QUICK_CHOICE_ID)
        assert chosen is not None and [str(e.path) for e in chosen.entries] == [target.id]
        assert window.playerbar.reason_text == "Evening · from schedule", "no optimistic change"

        manual = {"DP-1": "manual", "HDMI-A-1": "manual"}
        runtime.answer(status({"DP-1": target.id, "HDMI-A-1": target.id}, manual))
        application.refresh_runtime_status_async()
        yield (
            "the pick in the player bar and on its card",
            lambda: window.playerbar.reason_text == "Your pick",
        )
        assert window.playerbar.title_text == target.name
        assert window.state.current == {"DP-1": target.id, "HDMI-A-1": target.id}
        assert inspector.wallpaper is not None and inspector.wallpaper.id == target.id

        # -- Apply to the scoped display ------------------------------------------------
        window.state.set_scope("HDMI-A-1")
        relabelled = page.inspector.apply_button  # rebuilt when its on-screen badge changed
        assert relabelled is not None and relabelled.get_label() == "Apply to HDMI-A-1"
        other = window.state.wallpaper(picture(tmp_path, 9))
        page.inspect(other)
        scoped = page.inspector.apply_button
        assert scoped is not None and scoped.get_label() == "Apply to HDMI-A-1"
        scoped.emit("clicked")
        yield (
            "the display's Quick choice to be sent to that display",
            lambda: (
                settled(application)
                and any(
                    (argument or "").startswith("HDMI-A-1 playlist-use quick-choice:")
                    for argument in runtime.requested("on")
                )
            ),
        )
        assert runtime.requested("playlist-use") == ["quick-choice"], "only one global apply"
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_favorite_persists_and_the_star_follows_the_store(
    runtime: Recorder, tmp_path: Path
) -> None:
    application = Application(ui="next")
    lanes: list[object] = []
    wid = picture(tmp_path, 2)

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        page = library_page(window)
        yield (
            "the first scan",
            lambda: settled(application) and window.library_text == "80 wallpapers in the library",
        )
        star = page._cards[wid].favorite_button
        assert star is not None and star.get_icon_name() == "non-starred-symbolic"
        star.emit("clicked")
        yield (
            "the favourite to be saved and shown",
            lambda: settled(application) and window.state.wallpaper(wid).favorite,
        )
        assert Path(wid) in favourites.Store.open().paths, "persisted in favourites.json"
        star = page._cards[wid].favorite_button
        assert star is not None and star.get_icon_name() == "starred-symbolic"

        # The inspector's star asks the store too, and follows it back.
        page.inspect(window.state.wallpaper(wid))
        inspector_star = page.inspector.favorite_button
        assert inspector_star is not None and inspector_star.get_active()
        inspector_star.set_active(False)
        yield (
            "the favourite to be cleared",
            lambda: settled(application) and not window.state.wallpaper(wid).favorite,
        )
        assert Path(wid) not in favourites.Store.open().paths
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_a_newer_playlists_file_turns_apply_and_favorite_off_with_the_notice(
    runtime: Recorder, tmp_path: Path
) -> None:
    newer = paths.app_state_dir() / playlists.STATE_FILENAME
    newer.parent.mkdir(parents=True, exist_ok=True)
    document = b'{"version": 99, "playlists": [], "from": "a newer build"}\n'
    newer.write_bytes(document)
    application = Application(ui="next")
    lanes: list[object] = []
    wid = picture(tmp_path, 4)

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        page = library_page(window)
        yield (
            "the first scan",
            lambda: (
                settled_after_a_refused_publication(application)
                and window.library_text == "80 wallpapers in the library"
            ),
        )
        assert window.notice.get_revealed()
        assert window.notice.get_title().startswith(
            "playlists.json was saved by a newer version of Wall-in-One, so Apply and favorites "
            "are off here."
        )
        card = page._cards[wid]
        assert card.apply_button is not None and not card.apply_button.get_sensitive()
        assert "playlists.json" in (card.apply_button.get_tooltip_text() or "")
        assert card.favorite_button is not None and not card.favorite_button.get_sensitive()
        page.inspect(window.state.wallpaper(wid))
        apply_button = page.inspector.apply_button
        star = page.inspector.favorite_button
        assert apply_button is not None and not apply_button.get_sensitive()
        assert star is not None and not star.get_sensitive()

        # Even when asked directly, nothing is written or sent.
        window.state.apply(wid)
        window.state.toggle_favorite(wid)
        yield "the refusals to settle", lambda: settled_after_a_refused_publication(application)
        assert runtime.requested("playlist-use") == [] and runtime.requested("on") == []
        assert newer.read_bytes() == document
        assert Path(wid) not in favourites.Store.open().paths
        yield "every tail to settle", lambda: settled_after_a_refused_publication(application)
        lanes.extend(application_lanes(application))
        window.close()

    run(application, scenario(), lanes)


def _config_snapshot() -> dict[str, harness.Node]:
    return harness.snapshot(paths.app_config_dir())


def test_ui_toml_is_written_once_per_preference_change_and_never_while_idle(
    runtime: Recorder,
) -> None:
    application = Application(ui="next")
    lanes: list[object] = []
    target = paths.ui_prefs_path()
    before = _config_snapshot()

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield (
            "the first scan",
            lambda: settled(application) and window.library_text == "80 wallpapers in the library",
        )
        opened = time.monotonic()
        yield "an idle window", lambda: time.monotonic() > opened + IDLE_SECONDS
        assert not target.exists(), "opening and idling never write ui.toml"
        assert harness.diff(before, _config_snapshot()) == []

        window.activate_action("win.style", GLib.Variant("s", "frosted"))
        assert window.has_css_class("wio-glass") and window.backdrop.get_visible()
        assert application._window_glass == css.Glass(background=0.30, panel=0.60)
        yield (
            "the window style to be saved",
            lambda: target.exists() and not window.preferences.busy,
        )
        window.preferences.close(wait=True)  # the worker has finished its one write
        assert ui_prefs.load().prefs.window_style == "frosted"
        written = _config_snapshot()
        changes = harness.diff(before, written)
        assert sorted((change.path, change.kind) for change in changes) == [
            (".ui.toml.mutation.lock", "created"),
            ("ui.toml", "created"),
        ]
        assert window.preferences.saves == 1

        settled_at = time.monotonic()
        yield "more idle time", lambda: time.monotonic() > settled_at + IDLE_SECONDS
        assert harness.diff(written, _config_snapshot()) == [], "written once, then left alone"
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)
    # Closing the window wrote nothing more.
    assert ui_prefs.load().prefs.window_style == "frosted"


def test_the_thumbnail_size_is_a_saved_preference(runtime: Recorder) -> None:
    application = Application(ui="next")
    lanes: list[object] = []
    target = paths.ui_prefs_path()

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        page = library_page(window)
        yield (
            "the first scan",
            lambda: settled(application) and window.library_text == "80 wallpapers in the library",
        )
        assert page.flow.min_width == 208
        page.widget.activate_action("lib.size", GLib.Variant("s", "small"))
        assert page.flow.min_width == 156
        yield "the size to be saved", lambda: target.exists() and not window.preferences.busy
        window.preferences.close(wait=True)
        assert ui_prefs.load().prefs.thumbnail_size == "small"
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_the_opacity_dialog_saves_a_dial_once_it_rests(runtime: Recorder) -> None:
    application = Application(ui="next")
    lanes: list[object] = []
    target = paths.ui_prefs_path()

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield (
            "the first scan",
            lambda: settled(application) and window.library_text == "80 wallpapers in the library",
        )
        window.activate_action("win.style", GLib.Variant("s", "translucent"))
        yield "the style to be saved", lambda: target.exists() and not window.preferences.busy
        window.activate_action("win.glass-settings", None)
        dialog = window.get_visible_dialog()
        assert isinstance(dialog, GlassDialog)
        assert dialog._panel.get_sensitive() and not dialog._frost.get_sensitive()
        for value in (70, 74, 77):  # a drag: three values, one save
            dialog._panel.set_value(value)
        assert application._window_glass == css.Glass(background=0.55, panel=0.77)
        assert window.preferences.busy, "a moving dial is saved once it rests"
        yield "the dial to be saved", lambda: not window.preferences.busy
        window.preferences.close(wait=True)
        saved = ui_prefs.load().prefs
        assert saved.panel_opacity.translucent == 0.77
        assert saved.panel_opacity.frosted == ui_prefs.DEFAULT_PANEL_OPACITY.frosted
        assert window.preferences.saves == 2, "the style, then the dial: one write each"
        dialog.force_close()
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


# -- the golden profile ------------------------------------------------------------------


@pytest.fixture
def golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sandbox.Golden]:
    """The committed golden profile, sealed (no processes, fake runtime, no Noctalia)."""
    runtime_dir = Path(tempfile.mkdtemp(prefix="wio-run-"))
    try:
        yield sandbox.enter(harness.FIXTURE, tmp_path / "sandbox", runtime_dir, monkeypatch)
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def _cache_touch(change: Change) -> None:
    """The thumbnail cache marks a hit by re-stamping it: same bytes, new time."""
    assert change.before is not None and change.after is not None
    assert change.before.digest == change.after.digest, "a cached thumbnail's bytes changed"


def test_opening_idling_and_closing_the_golden_profile_writes_only_the_whitelist(
    golden: sandbox.Golden, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_ffmpeg(item: MediaItem, **_keywords: object) -> Path:
        raise thumbnail_cache.ThumbnailError(f"no processes in the golden sandbox: {item.path}")

    # The sandbox refuses every process. Say so the way a machine without
    # ffmpeg would, so the thumbnailer does not leave the temporary file it
    # made for the refused ffmpeg behind (a sandbox artefact, not a write).
    monkeypatch.setattr(thumbnail_cache, "generate", no_ffmpeg)
    # The application's automatic-still maker is not the window's: it runs
    # for every interface after a scan, and on this profile it may replace
    # the scene's automatic still depending on which renderers are on PATH.
    # Its writes have their own tests; this one is about the window.
    stills_asked: list[Path] = []

    def no_stills(_maker: StillMaker, _items: object, root: Path, _callback: object) -> None:
        stills_asked.append(root)

    monkeypatch.setattr(StillMaker, "request", no_stills)
    profile = golden.profile
    before = harness.snapshot(profile.home)
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield (
            "the first scan and the first status",
            lambda: (
                settled(application)
                and len(window.state.wallpapers) > 0
                and application.status_model.view.service == "running"
            ),
        )
        opened = time.monotonic()
        yield "an idle window", lambda: time.monotonic() > opened + IDLE_SECONDS
        assert window.playerbar.title_text, "the player bar shows the runtime's answer"
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)
    allowed = [
        *first_start_writes(profile, None),
        Allowance(
            ".cache/wall-in-one/thumbnails/*",
            frozenset({"rewritten"}),
            "the GUI's thumbnail cache re-stamps an entry it served (bytes unchanged)",
            _cache_touch,
        ),
    ]
    harness.check_changes(harness.diff(before, harness.snapshot(profile.home)), allowed)
    assert not (profile.app_config / "ui.toml").exists()
    assert not (profile.app_config / ".ui.toml.mutation.lock").exists()
