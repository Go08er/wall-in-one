"""Codex 0.3.0 round 3, in the real application: R-1, R-2 and R-3.

Each of Codex's reproductions, through the real settings writer, the real
still choice and the real removal, over real files, the real adoption
manifest and the real scan. On 7d5665c each change was allowed and left a
profile the compiler refuses (R-3 also published a generation naming a
deleted file); now the dry run on the authoring worker refuses it before
anything is saved, moved or deleted, naming the entry and the still. The
cases that must stay allowed stay allowed. That the dry run is what the
change really leaves is ``test_dry_run``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_adopted_removal import _case as _adoption_case  # noqa: E402
from tests.test_ui_codex030_round2 import _builds, _file, _lane_idle  # noqa: E402
from tests.test_ui_runtime import FakeWindow, _application, _attach, _close  # noqa: E402
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.control import client, server  # noqa: E402
from wall_in_one.control.protocol import Request, Response  # noqa: E402
from wall_in_one.library import scan  # noqa: E402
from wall_in_one.library.model import Library  # noqa: E402
from wall_in_one.session import RemovalResult  # noqa: E402
from wall_in_one.ui.app import Application, _Commands  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def made(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    applications: list[Application] = []

    def make(roots: tuple[Path, ...]) -> tuple[Application, FakeWindow]:
        """A windowless application over ``roots``, saved as its settings."""
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))  # the Trash
        application = _application(tmp_path, monkeypatch)
        applications.append(application)
        window = FakeWindow()
        _attach(application, window)
        monkeypatch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())
        settings = replace(application.settings, roots=roots, scan_workshop=False).validated()
        config.save(settings)
        application._settings = settings
        application._settings_requested = settings
        application.session.update_settings(settings, rescan_library=False)
        return application, window

    yield make
    for application in applications:
        _close(application)


def _adopt_a_real_scan(application: Application) -> Library:
    """What the application's own scan finds, adoption manifest and all."""
    settings = application.settings
    library = scan.scan(settings.roots, dict(application.session.pairings.records))
    application.session.adopt_library(library, reconcile_workshop=False)
    application._accepted_library_sources = (settings.roots, settings.scan_workshop)
    return library


def _scheduled(application: Application, name: str, sources: list[Path]) -> str:
    session = application.session
    made_playlist = session.playlists.create(name)
    for source in sources:
        session.playlists.add(made_playlist.id, source)
    session.schedules.add(made_playlist.id)
    return made_playlist.id


def _choose_still(application: Application, video: Path, still: Path) -> None:
    """The application's still-choice boundary, as ``ctl still`` reaches it."""
    reply: list[Response] = []
    server.dispatch(
        server.build_verb_table(_Commands(application)),
        Request("still", f"{video} :: {still}").encode(),
        reply.append,
    )
    spin_until(lambda: bool(reply) and _lane_idle(application), 10, what="the still choice")
    assert reply[0].ok, reply[0].message
    found = application.session.library.find(video)
    assert found is not None and found.paired_still == still


def _remove(application: Application, path: Path) -> list[RemovalResult]:
    item = application.session.library.find(path)
    assert item is not None, path
    finished: list[RemovalResult] = []
    done = threading.Event()

    def removed(result: RemovalResult) -> None:
        finished.append(result)
        done.set()

    application.remove_item_async(item, trash=True, finish=removed)
    spin_until(lambda: done.is_set() or _lane_idle(application), 30, what="the removal")
    spin_until(lambda: _lane_idle(application), 10, what="the lane")
    return finished


def _reported(window: Any, words: str) -> bool:
    return any(words in report for report in window.reports)


def test_r1_replacing_the_adoption_folder_by_the_one_above_it_is_refused(
    tmp_path: Path, made: Any
) -> None:
    """R-1: the scan pairs an adopted capture only while the adoption's own folder is one."""
    case = _adoption_case(tmp_path / "lib", managed=False)
    case.session.shutdown()
    application, _window = made((case.root,))
    library = _adopt_a_real_scan(application)
    found = library.find(case.source)
    assert found is not None and found.paired_still == case.capture, "the adoption is in force"
    _scheduled(application, "Adopted", [case.source])
    assert _builds(application)
    errors: list[str] = []

    application.update_settings_async(roots=(case.root.parent,), on_error=errors.append)
    spin_until(lambda: _lane_idle(application), 10, what="the folder change to settle")

    assert config.load_strict().roots == (case.root,), "nothing was saved"
    assert errors == [
        "that change to the library folders would break playback: \u201cAdopted\u201d (used by "
        "1 schedule rule) would have nothing left to play: clip.mp4 plays through clip.png, "
        "which would no longer be its still"
    ]
    assert _builds(application)


def test_r2_removing_the_source_of_an_adopted_capture_another_video_plays_is_refused(
    tmp_path: Path, made: Any
) -> None:
    """R-2: removing clip.mp4 deletes its adopted capture, which other.mp4 was given."""
    case = _adoption_case(tmp_path / "lib", managed=False)
    case.session.shutdown()
    other = _file(case.root / "personal" / "other.mp4")
    above = case.root.parent
    application, window = made((above,))
    # Scanning the folder above, the adoption is not in force: its capture is a picture,
    # which the person chooses for another video.
    assert _adopt_a_real_scan(application).find(case.capture) is not None
    _choose_still(application, other, case.capture)
    errors: list[str] = []
    application.update_settings_async(roots=(case.root,), on_error=errors.append)
    spin_until(
        lambda: (
            application.session.library.adopted_stills == ((case.source, case.capture),)
            and _lane_idle(application)
        ),
        20,
        what="the adoption folder's scan",
    )
    assert errors == []
    kept = application.session.library.find(other)
    assert kept is not None and kept.paired_still == case.capture, "its choice still stands"
    _scheduled(application, "Other", [other])
    assert _builds(application)

    finished = _remove(application, case.source)

    assert case.source.exists() and case.capture.exists(), "nothing was moved or deleted"
    assert finished == []
    assert _reported(
        window,
        "clip was not removed: \u201cOther\u201d (used by 1 schedule rule) would have nothing "
        "left to play: other.mp4 plays through clip.png, which this removes.",
    ), window.reports
    assert _builds(application)


class Aliased:
    def __init__(self, tmp_path: Path, made: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        walls = tmp_path / "lib" / "real" / "walls"
        self.rain = _file(walls / "rain.mp4")
        self.forest = _file(walls / "forest.png")
        self.dawn = _file(walls / "dawn.png")
        via = tmp_path / "lib" / "via"
        via.symlink_to(tmp_path / "lib" / "real", target_is_directory=True)
        self.alias = via / "walls" / "forest.png"
        self.application, self.window = made((walls, via / "walls"))
        library = _adopt_a_real_scan(self.application)
        assert library.find(self.alias) is not None, "both spellings are in the library"
        _choose_still(self.application, self.rain, self.alias)
        #: Each generation of the runtime configuration installed, as installed.
        self.loaded: list[str] = []
        # What the application's lanes install through (`ui.app._runtime_installed`).
        install = runtime_config.publish_changes

        def installed(
            settings: config.Settings, session: Any, path: Path | None = None
        ) -> runtime_config.Published:
            changed = install(settings, session, path)
            if changed:
                target = path if path is not None else paths.runtime_config_path()
                self.loaded.append(target.read_text(encoding="utf-8"))
            return changed

        monkeypatch.setattr(runtime_config, "publish_changes", installed)
        monkeypatch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())

    def publish(self) -> str:
        application = self.application
        assert application._publish_runtime_async()
        spin_until(
            lambda: (
                not application._runtime_compile_pending and paths.runtime_config_path().exists()
            ),
            10,
            what="a publication",
        )
        return paths.runtime_config_path().read_text(encoding="utf-8")


def test_r3_removing_a_real_file_a_video_plays_through_an_alias_of_is_refused(
    tmp_path: Path, made: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-3: real/walls/forest.png goes, and with it via/walls/forest.png, rain's still."""
    aliased = Aliased(tmp_path, made, monkeypatch)
    application = aliased.application
    _scheduled(application, "Alias", [aliased.rain])
    published = aliased.publish()
    assert str(aliased.alias) in published
    loaded = len(aliased.loaded)

    finished = _remove(application, aliased.forest)

    assert aliased.forest.exists() and aliased.alias.exists(), "nothing was moved"
    assert finished == []
    assert _reported(
        aliased.window,
        "forest was not removed: \u201cAlias\u201d (used by 1 schedule rule) would have nothing "
        "left to play: rain.mp4 plays through forest.png, which this removes.",
    ), aliased.window.reports
    assert len(aliased.loaded) == loaded, "no new generation"
    assert paths.runtime_config_path().read_text(encoding="utf-8") == published
    assert _builds(application)


def test_r3_an_allowed_removal_publishes_no_generation_naming_either_spelling(
    tmp_path: Path, made: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-3 allowed: Alias has another wallpaper. Every generation after names neither path."""
    aliased = Aliased(tmp_path, made, monkeypatch)
    application = aliased.application
    _scheduled(application, "Alias", [aliased.rain, aliased.dawn])
    assert str(aliased.alias) in aliased.publish()
    loaded = len(aliased.loaded)
    rescans: list[bool] = []
    scan_again = application.refresh_library

    def counted(*arguments: Any, **keywords: Any) -> None:
        rescans.append(True)
        scan_again(*arguments, **keywords)

    monkeypatch.setattr(application, "refresh_library", counted)

    finished = _remove(application, aliased.forest)

    assert [result.committed for result in finished] == [True]
    assert not aliased.forest.exists() and not aliased.alias.exists()
    spin_until(
        lambda: (
            len(aliased.loaded) > loaded
            and not application._runtime_compile_pending
            and application._library_scan_future is None
        ),
        10,
        what="the publication after the removal",
    )
    for document in aliased.loaded[loaded:]:
        assert str(aliased.alias) not in document
        assert str(aliased.forest) not in document
        assert str(aliased.dawn) in document
    assert rescans == [], "the removal's own scan after the commit is the new library"
    assert application.session.library.find(aliased.alias) is None
    assert _builds(application)


def test_a_chosen_still_outside_the_folders_that_stay_keeps_the_change_allowed(
    tmp_path: Path, made: Any
) -> None:
    """Allowed, as Codex checked: the scan attaches a chosen still wherever it is."""
    a, b = tmp_path / "lib" / "A", tmp_path / "lib" / "B"
    still = _file(a / "forest.png")
    video = _file(b / "rain.mp4")
    application, _window = made((a, b))
    _adopt_a_real_scan(application)
    _choose_still(application, video, still)
    _scheduled(application, "Evening", [video])
    errors: list[str] = []

    application.update_settings_async(roots=(b,), on_error=errors.append)
    spin_until(
        lambda: (
            application.settings.roots == (b,)
            and application.session.library.roots == (b,)
            and _lane_idle(application)
        ),
        20,
        what="the folder change and its scan",
    )

    assert errors == []
    assert config.load_strict().roots == (b,)
    found = application.session.library.find(video)
    assert found is not None and found.paired_still == still
    assert _builds(application)
