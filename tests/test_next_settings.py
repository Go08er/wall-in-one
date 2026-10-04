"""The new Settings page's adapter (`RealSettings`) saves through the classic paths.

No display: `RealSettings` over a fake application that keeps settings the way
`Application` does (requested = durable + what is being saved; a failure goes
back to durable) and queues authoring work until the test runs it. Files go to
the test's own XDG directories (conftest).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import pytest

from tests.gtk_helpers import spin_until
from tests.test_settings_v0_1_4_keys import V0_1_4_SETTINGS_KEYS
from wall_in_one import config, paths
from wall_in_one.library.model import Library
from wall_in_one.providers import credentials, registry
from wall_in_one.session import Session
from wall_in_one.theme import palettes, source
from wall_in_one.ui.next import real_settings
from wall_in_one.ui.next.real_settings import RealPalettes, RealSettings, Worker, role_key
from wall_in_one.ui.next.settings_state import (
    INTERPOLATION,
    KEY_FAILED,
    KEY_REFUSED,
    KEY_SAVED,
    PALETTE_ROLES,
    SCENE_CLAMP,
    SCENE_SCALING,
    SCHEMES,
    WHEN_HIDDEN,
    PaletteDraft,
)
from wall_in_one.ui.status_model import RuntimeStatusModel
from wall_in_one.wallpaper import renderer, scenes

if TYPE_CHECKING:
    from wall_in_one.ui.app import Application
    from wall_in_one.ui.next.settings_state import PaletteManager, SettingsAccess

    def _application_backs_it(application: Application) -> real_settings.SettingsBackend:
        return application

    def _part_conforms(part: RealSettings) -> SettingsAccess:
        return part

    def _palettes_conform(part: RealPalettes) -> PaletteManager:
        return part


_T = TypeVar("_T")


class FakeApplication:
    """`SettingsBackend`, keeping settings as `Application` does."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._model = RuntimeStatusModel()
        self.durable = session.settings
        self.requested = session.settings
        self.unknown_keys: tuple[str, ...] = ()
        self.saves: list[dict[str, Any]] = []
        self.fail_with = ""
        self.reports: list[str] = []
        self.authoring: deque[Callable[[], None]] = deque()
        self.authoring_threads: list[str] = []
        self.ready = True
        self.applied: list[tuple[str, str]] = []
        self.schemes: list[tuple[str, bool]] = []

    @property
    def session(self) -> Session:
        return self._session

    @property
    def status_model(self) -> RuntimeStatusModel:
        return self._model

    @property
    def settings(self) -> config.Settings:
        return self.durable

    @property
    def requested_settings(self) -> config.Settings:
        return self.requested

    @property
    def settings_unknown_keys(self) -> tuple[str, ...]:
        return self.unknown_keys

    @property
    def resolved_palette(self) -> source.ResolvedPalette | None:
        return None

    def update_settings_async(
        self,
        *,
        on_success: Callable[[config.Settings], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        **changes: Any,
    ) -> bool:
        self.saves.append(changes)
        self.requested = replace(self.requested, **changes).validated()

        def run() -> None:
            if self.fail_with:
                self.requested = self.durable
                if on_error is not None:
                    on_error(self.fail_with)
                return
            self.durable = config.update(changes)
            self.requested = self.durable
            if on_success is not None:
                on_success(self.durable)

        self.authoring.append(run)
        return True

    def authoring_action_async(
        self,
        work: Callable[[], _T],
        finish: Callable[[_T], None],
        *,
        prepare: Callable[[], Callable[[], _T]] | None = None,
        failure: Callable[[str], None] | None = None,
    ) -> bool:
        def run() -> None:
            done: list[_T] = []
            errors: list[str] = []

            def worker() -> None:
                self.authoring_threads.append(threading.current_thread().name)
                try:
                    done.append((prepare() if prepare is not None else work)())
                except Exception as error:
                    errors.append(str(error))

            # Off the test's (GTK's) thread, as the application's lane is.
            thread = threading.Thread(target=worker, name="control-authoring-fake")
            thread.start()
            thread.join()
            if done:
                finish(done[0])
            elif failure is not None:
                failure(errors[0])

        self.authoring.append(run)
        return True

    def run_authoring(self) -> None:
        while self.authoring:
            self.authoring.popleft()()

    def require_authoring_ready(self, *, report: bool = True) -> bool:
        if not self.ready and report:
            self.reports.append("current-profile authoring is still opening; retry shortly")
        return self.ready

    def window_report(self, message: str) -> None:
        self.reports.append(message)

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
        self.applied.append((palette_source, name))
        if on_complete is not None:
            on_complete("")
        return True

    def use_preview_scheme(
        self,
        scheme: str,
        *,
        sync_noctalia: bool,
        on_complete: real_settings.ThemeActionCallback | None = None,
    ) -> bool:
        self.schemes.append((scheme, sync_noctalia))
        return True


class Host:
    def __init__(self) -> None:
        self.topics: list[str] = []

    def emit_changed(self, *topics: str) -> None:
        self.topics.extend(topics)


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[FakeApplication]:
    root = tmp_path / "wallpapers"
    root.mkdir()
    settings = config.update({"roots": (root,)})
    session = Session(settings, owns_playback=False)
    session.adopt_library(Library(roots=(root,), items=()), reconcile_workshop=False)
    try:
        yield FakeApplication(session)
    finally:
        session.shutdown()


@pytest.fixture
def part(backend: FakeApplication) -> Iterator[tuple[RealSettings, Host]]:
    host = Host()
    settings = RealSettings(backend, host)
    try:
        yield settings, host
    finally:
        settings.close()


# -- the rows' vocabulary -----------------------------------------------------------------


def test_every_choice_is_one_the_validated_settings_accept() -> None:
    assert [key for key, _label in WHEN_HIDDEN] == list(renderer.WHEN_HIDDEN_CHOICES)
    assert [key for key, _label in INTERPOLATION] == list(renderer.INTERPOLATION_CHOICES)
    assert [key for key, _label in SCENE_SCALING] == list(scenes.SCALING_CHOICES)
    assert [key for key, _label in SCENE_CLAMP] == list(scenes.CLAMP_CHOICES)
    assert dict(SCHEMES)["m3-tonal-spot"] == "Tonal Spot"


def test_the_editor_roles_are_the_keys_a_palette_file_carries() -> None:
    assert [role_key(role) for role, _label in PALETTE_ROLES] == [
        key for key, _label in palettes.EDITABLE_KEYS
    ]


# -- settings.toml ------------------------------------------------------------------------


def test_a_change_saves_only_its_key_and_a_repeat_saves_nothing(
    backend: FakeApplication, part: tuple[RealSettings, Host]
) -> None:
    settings, _host = part
    target = paths.settings_path()
    settings.change_settings(video_volume=40)
    settings.change_settings(video_volume=40)  # already being saved: not queued again
    assert backend.saves == [{"video_volume": 40}]
    assert settings.setting("video_volume") == 40, "the row shows what is being saved"
    backend.run_authoring()
    assert config.load(target).video_volume == 40
    assert set(backend.saves[0]) <= V0_1_4_SETTINGS_KEYS
    settings.change_settings(video_volume=40)  # durable now: still nothing
    assert len(backend.saves) == 1


def test_a_failed_save_reports_and_shows_the_durable_value_again(
    backend: FakeApplication, part: tuple[RealSettings, Host]
) -> None:
    settings, host = part
    before = paths.settings_path().read_bytes()
    backend.fail_with = "disk full"
    settings.change_settings(shuffle=True, cycle_interval=900)
    assert settings.setting("shuffle") is True
    backend.run_authoring()
    assert settings.setting("shuffle") is False and settings.setting("cycle_interval") == 300
    assert backend.reports == ["Settings were not saved; nothing changed: disk full"]
    assert host.topics[-1] == "settings", "the page is told to show what is durable"
    assert paths.settings_path().read_bytes() == before
    backend.fail_with = ""
    settings.change_settings(shuffle=True)
    backend.run_authoring()
    assert config.load().shuffle is True, "the same choice saves on retry"


def test_independent_displays_take_a_display_to_follow_or_are_refused(
    backend: FakeApplication, part: tuple[RealSettings, Host], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, host = part
    monkeypatch.setattr(real_settings, "_connected_outputs", lambda: ())
    settings.change_settings(display_mode="independent")
    assert backend.saves == []
    assert "needs an attached display" in backend.reports[-1]
    assert host.topics[-1] == "settings", "the row goes back to mirrored"

    monkeypatch.setattr(real_settings, "_connected_outputs", lambda: ("DP-2", "HDMI-A-1"))
    settings.change_settings(display_mode="independent")
    assert backend.saves == [{"display_mode": "independent", "theme_source_connector": "DP-2"}]
    backend.run_authoring()
    assert config.load().theme_source_connector == "DP-2"
    sources = settings.theme_sources()
    assert sources.connectors == (("DP-2", True), ("HDMI-A-1", True))
    assert sources.selected == "DP-2" and "follows this display" in sources.note


def test_settings_from_a_newer_version_are_named_and_refused_by_the_app(
    backend: FakeApplication, part: tuple[RealSettings, Host]
) -> None:
    settings, _host = part
    assert settings.settings_blocked() == ""
    backend.unknown_keys = ("wallpaper_rules",)
    blocked = settings.settings_blocked()
    assert blocked.startswith("Settings are read-only.") and "wallpaper_rules" in blocked


def test_the_battery_note_follows_the_setting_and_the_runtime(
    backend: FakeApplication, part: tuple[RealSettings, Host]
) -> None:
    settings, _host = part
    assert settings.battery_note() == ""
    backend.durable = replace(backend.durable, stop_animations_on_battery=True)
    assert settings.battery_note() == "Waiting for the wallpaper service"
    backend.status_model.adopt({"status_version": 2})
    assert "Restart the updated wallpaper service" in settings.battery_note()


# -- library folders ------------------------------------------------------------------------


def test_folders_are_added_moved_to_the_front_and_removed_as_classic_does(
    backend: FakeApplication, part: tuple[RealSettings, Host], tmp_path: Path
) -> None:
    settings, _host = part
    first = backend.durable.roots[0]
    second = tmp_path / "second"
    second.mkdir()
    settings.add_folder(str(second))
    assert backend.reports == [], "nothing is claimed before the folder is saved"
    backend.run_authoring()
    assert config.load().roots == (first, second)
    assert backend.reports == ["Scanning second"]
    settings.add_folder(str(second))
    assert backend.reports[-1] == "second is already in the library"
    settings.make_download_folder(str(second))
    backend.run_authoring()
    assert config.load().roots == (second, first), "the download target is the first root"
    settings.make_download_folder(str(second))  # already first: nothing to save
    assert len(backend.saves) == 2
    settings.remove_folder(str(first))
    backend.run_authoring()
    assert config.load().roots == (second,)


def test_a_failed_folder_save_says_so_and_claims_no_scan(
    backend: FakeApplication, part: tuple[RealSettings, Host], tmp_path: Path
) -> None:
    settings, host = part
    backend.fail_with = "disk full"
    settings.add_folder(str(tmp_path / "elsewhere"))
    backend.run_authoring()
    assert backend.reports == ["Library folders were not saved; nothing changed: disk full"]
    assert host.topics[-1] == "settings"


def test_folder_checks_run_off_the_main_thread_and_a_suggestion_is_never_added(
    backend: FakeApplication, part: tuple[RealSettings, Host], tmp_path: Path
) -> None:
    settings, host = part
    gone = tmp_path / "unplugged"
    backend.durable = replace(backend.durable, roots=(*backend.durable.roots, gone))
    shown = settings.folder_list()
    assert [folder.missing for folder in shown] == [False, False], "unknown until checked"
    assert settings.scenes_available() is None
    spin_until(lambda: "settings" in host.topics, what="the folder check")
    assert [folder.missing for folder in settings.folder_list()] == [False, True]
    assert settings.scenes_available() in (True, False)

    suggested = Path.home() / "Pictures" / "Wallpapers"
    suggested.mkdir(parents=True)
    backend.durable = replace(backend.durable, roots=())
    host.topics.clear()
    assert settings.suggested_folder() == ""
    spin_until(lambda: "settings" in host.topics, what="the suggestion")
    assert settings.suggested_folder() == str(suggested)
    assert settings.folder_list() == () and backend.saves == [], "suggested, never added"


# -- the Wallhaven key ----------------------------------------------------------------------


def test_the_key_is_saved_and_cleared_on_the_authoring_lane_and_never_shown(
    backend: FakeApplication, part: tuple[RealSettings, Host], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, host = part
    monkeypatch.delenv(registry.API_KEY_VARIABLE, raising=False)
    outcomes: list[str] = []
    typed = "not a key, with spaces!"
    settings.save_key(typed, outcomes.append)
    assert outcomes == [], "nothing happens before the lane runs it"
    backend.run_authoring()
    assert outcomes == [KEY_REFUSED]
    assert backend.reports == ["That does not look like a Wallhaven API key"]
    assert all(typed not in report for report in backend.reports)

    key = "a" * 32
    settings.save_key(key, outcomes.append)
    backend.run_authoring()
    assert outcomes[-1] == KEY_SAVED and credentials.key_path().is_file()
    assert backend.authoring_threads == ["control-authoring-fake"] * 2
    assert all(key not in report for report in backend.reports)
    spin_until(lambda: settings.key_status().stored, what="the key status")
    assert settings.key_status().text == f"Saved in {credentials.key_path()}"

    host.topics.clear()
    settings.clear_key()
    backend.run_authoring()
    assert not credentials.key_path().exists()
    assert backend.reports[-1] == "Wallhaven API key removed"
    spin_until(lambda: not settings.key_status().stored, what="the cleared status")

    backend.ready = False
    settings.save_key(key, outcomes.append)
    assert outcomes[-1] == KEY_FAILED and not backend.authoring


def test_the_key_status_says_which_key_is_in_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(registry.API_KEY_VARIABLE, raising=False)
    assert real_settings.describe_key().text.startswith("Not set.")
    credentials.save_key("b" * 32)
    assert real_settings.describe_key().text == f"Saved in {credentials.key_path()}"
    monkeypatch.setenv(registry.API_KEY_VARIABLE, "c" * 32)
    assert "overrides the saved key" in real_settings.describe_key().text
    credentials.clear_key()
    assert "would stay unused" in real_settings.describe_key().text
    monkeypatch.setenv(registry.API_KEY_VARIABLE, "not a key!")
    assert "not a valid key" in real_settings.describe_key().text


# -- palettes ---------------------------------------------------------------------------


def _custom(name: str, primary: str = "#112233") -> Path:
    document = {
        "dark": {key: primary for key, _label in palettes.EDITABLE_KEYS},
        "light": {key: "#eeeeee" for key, _label in palettes.EDITABLE_KEYS},
    }
    return palettes.write_custom(name, document).path or Path()


def test_a_palette_is_duplicated_edited_and_applied_through_noctalia(
    backend: FakeApplication, part: tuple[RealSettings, Host]
) -> None:
    settings, host = part
    _custom("Mine")
    manager = settings.palette_manager
    manager.open_palettes()
    spin_until(lambda: manager.palette_list().phase == "ready", what="the catalogue")
    items = {(item.origin, item.name): item for item in manager.palette_list().items}
    nord = items[("builtin", "Nord")]
    assert not nord.can_copy and not nord.can_edit and nord.can_apply
    mine = items[("custom", "Mine")]
    assert mine.can_edit and mine.dark[1] == "#112233"

    manager.apply_catalog_palette(nord.key)
    assert backend.applied == [("builtin", "Nord")]
    assert backend.reports[-1] == "Nord applied"

    drafts: list[PaletteDraft | None] = []
    manager.palette_draft(mine.key, copy=True, done=drafts.append)
    spin_until(lambda: drafts, what="the document, read off the main thread")
    draft = drafts[0]
    assert draft is not None and draft.renamable and draft.name == "Mine copy"
    assert draft.colors["dark"]["primary"] == "#112233"

    saved: list[bool] = []
    manager.save_palette_draft(draft, "Mine", draft.colors, saved.append)
    assert saved == [False], "never over another palette of yours"
    assert "already have a palette called Mine" in backend.reports[-1]
    colors = {
        "dark": {**draft.colors["dark"], "primary": "#445566"},
        "light": draft.colors["light"],
    }
    manager.save_palette_draft(draft, "Mine blue", colors, saved.append)
    backend.run_authoring()
    assert saved[-1] is True and backend.reports[-1] == "Saved Mine blue"
    written = palettes.read_document(palettes.custom_path("Mine blue"))
    assert written["dark"]["mPrimary"] == "#445566"  # type: ignore[index]

    stale: list[PaletteDraft | None] = []
    manager.palette_draft(mine.key, copy=False, done=stale.append)
    assert stale == [None], "the snapshot before the save is not actionable"
    spin_until(
        lambda: any(item.name == "Mine blue" for item in manager.palette_list().items),
        what="the catalogue after the save",
    )
    drafts.clear()
    manager.palette_draft(mine.key, copy=False, done=drafts.append)
    spin_until(lambda: drafts, what="the in-place draft")
    in_place = drafts[0]
    assert in_place is not None and not in_place.renamable and in_place.name == "Mine"
    manager.save_palette_draft(in_place, "ignored", in_place.colors, saved.append)
    backend.run_authoring()
    assert saved[-1] is True and palettes.custom_path("Mine").is_file()
    assert "palettes" in host.topics
    manager.close_palettes()


def test_using_a_scheme_goes_through_the_application(
    backend: FakeApplication, part: tuple[RealSettings, Host]
) -> None:
    settings, _host = part
    settings.palette_manager.use_scheme("soft", sync_noctalia=True)
    assert backend.schemes == [("soft", True)]


def test_the_page_offers_only_settings_keys_that_0_1_4_reads() -> None:
    names = {field.name for field in fields(config.Settings)}
    assert names == V0_1_4_SETTINGS_KEYS, "settings.toml is frozen"


def test_a_worker_answer_after_close_is_dropped() -> None:
    worker = Worker("settings-io-test")
    delivered: list[int] = []
    gate = threading.Event()
    finished = threading.Event()

    def work() -> int:
        gate.wait(5)
        finished.set()
        return 1

    worker.run(work, delivered.append)
    worker.close()
    gate.set()
    assert finished.wait(5)
    end = time.monotonic() + 0.3
    spin_until(lambda: time.monotonic() > end, what="a moment of main loop")
    assert delivered == [], "the window had gone: nothing reaches GTK"

    reopened: list[int] = []
    worker.run(lambda: 2, reopened.append)
    spin_until(lambda: reopened == [2], what="a read after the window came back")
    worker.close()
