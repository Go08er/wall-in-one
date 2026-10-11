"""Stills a read-only folder won't take are said: which folder, why, once.

`tests.test_read_only_profile` found a read-only library whose videos never
got their stills with nothing said, in the window or the log
(`StillMaker._run` relied on `stills.ensure` swallowing every failure). Now a
folder that refuses is told apart from a frame that couldn't be captured
(`stills.StillFolderError`, `tests.test_stills_read_only`), and the maker says
it. These drive the real application, started over a library folder made
read-only with its real permission bits, through its own scan and still
maker; ffmpeg is a stub, as in `tests.test_stills`.

* The folder and the kernel's reason are named, not "changed" and not ffmpeg.
* Once per folder per start in the window, with how many wallpapers: twenty
  videos are one report, and videos found later in the same folder go to the
  session log alone.
* Nothing is tried again: a refused batch makes nothing, so it asks for no
  rescan, and a rescan asks for nothing already attempted.
* ``Wall-in-One/Automatic Stills`` read-only on its own, and the library
  read-only on its own with that folder writable, each name the folder that
  refused.
* "Capture a new still" / "Regenerate" for a scene says why, each time asked.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests import read_only_probe as ro  # noqa: E402
from tests.gtk_helpers import settle, spin_until  # noqa: E402
from tests.test_stills_read_only import DISPLAY, FRAME, SCENE, read_only  # noqa: E402
from tests.test_ui_next_window import toolkit  # noqa: E402,F401 -- a fixture: GTK, once
from tests.test_ui_runtime import FakeWindow  # noqa: E402
from wall_in_one import config, worker_processes  # noqa: E402
from wall_in_one.library import pairing, stills  # noqa: E402
from wall_in_one.library.model import Kind, MediaItem  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.stills import StillMaker  # noqa: E402
from wall_in_one.wallpaper import scenes  # noqa: E402

DENIED = "Permission denied"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ro.Profile:
    """A profile whose one library folder is empty; each test puts its videos there."""
    ro.skip_as_root()
    profile = ro.Profile(tmp_path / "home")
    ro.enter(profile, monkeypatch)
    profile.library.mkdir(parents=True)
    config.save(replace(config.Settings(), roots=(profile.library,), scan_workshop=False))
    return profile


def videos(folder: Path, count: int, prefix: str = "clip") -> list[Path]:
    """``count`` videos; no frame is ever read from one (ffmpeg is a stub)."""
    made = [folder / f"{prefix}-{number:02}.mp4" for number in range(count)]
    for path in made:
        path.write_bytes(b"a video")
    return made


def ffmpeg(monkeypatch: pytest.MonkeyPatch, complaint: str = "") -> list[Path]:
    """ffmpeg installed; every run takes a frame, or fails with ``complaint``. The runs."""
    ran: list[Path] = []

    def render(
        source: Path,
        temporary: Path,
        _seek: float,
        *,
        processes: worker_processes.Cancellation | None = None,
    ) -> str:
        del processes
        ran.append(source)
        if not complaint:
            temporary.write_bytes(FRAME)
        return complaint

    monkeypatch.setattr(stills, "is_available", lambda: True)
    monkeypatch.setattr(stills, "_run", render)
    return ran


@dataclass
class Run:
    """A started application, its window, and every still it tried to make."""

    application: Application
    window: FakeWindow
    #: Each video `stills.ensure` went on to capture from, every time it did.
    tried: list[Path]

    def reports(self) -> list[str]:
        """The window's reports about stills."""
        return [text for text in self.window.reports if "still" in text.casefold()]

    def lines(self) -> list[str]:
        """The session log's lines about stills."""
        return [
            entry.text
            for entry in self.application.activity_log.entries
            if entry.source == "app" and "still" in entry.text.casefold()
        ]

    def wait(self) -> None:
        """The still maker done, and whatever scan it asked for (`_on_stills_made`)."""
        application = self.application
        for _round in range(2):
            spin_until(
                lambda: not application._stills._pending and ro.quiet(application),
                ro.BOUND * 2,
                what="the still maker, and the scan after it",
            )
            settle(0.3)

    def rescan(self) -> None:
        self.application.refresh_library()
        self.wait()


@contextlib.contextmanager
def started(profile: ro.Profile, monkeypatch: pytest.MonkeyPatch) -> Iterator[Run]:
    """The application over ``profile`` as it is now: first scan done, stills attempted."""
    tried: list[Path] = []
    generate = stills.generate

    def counted(video: Path, root: Path, **keywords: object) -> Path:
        tried.append(video)
        return generate(video, root, **keywords)  # type: ignore[arg-type]

    monkeypatch.setattr(stills, "generate", counted)
    application, window = ro.boot(profile, monkeypatch, stills=True)
    run = Run(application, window, tried)
    try:
        run.wait()
        yield run
    finally:
        ro.close(application)


def said_for(count: int, folder: Path) -> str:
    """What the maker says for ``count`` wallpapers ``folder`` refused a still for."""
    wallpapers = "1 wallpaper" if count == 1 else f"{count} wallpapers"
    return f"No still could be made for {wallpapers}: {folder} cannot be written ({DENIED})"


# -- the start, with the library read-only ---------------------------------------------------------


def test_twenty_videos_in_a_read_only_library_are_one_report_naming_the_folder(
    home: ro.Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = ffmpeg(monkeypatch)
    clips = videos(home.library, 20)
    with ro.read_only(home.library), started(home, monkeypatch) as run:
        reports, lines = run.reports(), run.lines()
    assert sorted(run.tried) == clips, "each video was tried, once"
    assert ran == [], "and refused before ffmpeg was run"
    (report,) = reports
    assert report.startswith(said_for(20, home.library)), report
    assert ro.WHY.search(report)
    assert "ffmpeg" not in report and "changed" not in report
    assert lines == reports, "one line in the log, with the count"
    assert list(home.library.glob("Wall-in-One/**/*")) == []


def test_a_refused_still_is_not_tried_again_however_long_the_application_runs(
    home: ro.Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two more scans and three seconds of main loop: twenty attempts, one report."""
    ffmpeg(monkeypatch)
    videos(home.library, 20)
    with ro.read_only(home.library), started(home, monkeypatch) as run:
        run.rescan()
        settle(3.0)
        run.rescan()
        reports, lines = run.reports(), run.lines()
    assert len(run.tried) == 20, f"{len(run.tried)} attempts for twenty videos"
    assert len(reports) == 1 and lines == reports


def test_a_video_ffmpeg_cannot_read_is_not_said_as_a_folder_that_refused(
    home: ro.Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The library writable and ffmpeg failing: as before, tried once and nothing said."""
    ran = ffmpeg(monkeypatch, "Invalid data found when processing input")
    clips = videos(home.library, 2)
    with started(home, monkeypatch) as run:
        reports, lines = run.reports(), run.lines()
    assert sorted(run.tried) == clips and len(ran) == 4, "both seeks, for each"
    assert reports == [] and lines == []


def test_videos_found_later_in_the_same_folder_go_to_the_log_alone(
    home: ro.Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg(monkeypatch)
    videos(home.library, 2)
    with ro.read_only(home.library), started(home, monkeypatch) as run:
        home.library.chmod(0o755)
        later = videos(home.library, 3, "later")
        home.library.chmod(0o555)
        run.rescan()
        reports, lines = run.reports(), run.lines()
    assert sorted(run.tried[2:]) == later, "only the new ones were tried"
    (report,) = reports
    assert report.startswith(said_for(2, home.library))
    assert len(lines) == 2 and lines[0] == report
    assert lines[1].startswith(said_for(3, home.library))


# -- the two folders, each read-only on its own ----------------------------------------------------


def test_the_automatic_stills_folder_read_only_on_its_own(
    home: ro.Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The library takes writes; ``Wall-in-One/Automatic Stills`` alone doesn't."""
    ran = ffmpeg(monkeypatch)
    videos(home.library, 3)
    folder = pairing.still_directory(home.library)
    folder.mkdir(parents=True)
    with read_only(folder), started(home, monkeypatch) as run:
        reports, lines = run.reports(), run.lines()
    assert len(run.tried) == 3 and ran == []
    (report,) = reports
    assert report.startswith(said_for(3, folder)), report
    assert lines == reports
    assert list(folder.iterdir()) == [], "nothing was left in it"


def test_the_library_read_only_on_its_own_with_a_writable_stills_folder(
    home: ro.Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``chmod a-w`` on the library alone: each frame is taken, its record beside the video isn't.

    A still is published with its pairing record or not at all, so none is
    made, and the folder that refused is the library itself.
    """
    ran = ffmpeg(monkeypatch)
    clips = videos(home.library, 3)
    folder = pairing.still_directory(home.library)
    folder.mkdir(parents=True)
    with read_only(home.library), started(home, monkeypatch) as run:
        reports, lines = run.reports(), run.lines()
    assert sorted(run.tried) == clips and len(ran) == 3, "a frame was taken from each"
    (report,) = reports
    assert report.startswith(said_for(3, home.library)), report
    assert lines == reports
    assert all(not stills.destination(clip, home.library).exists() for clip in clips)


def test_a_batch_that_makes_some_stills_says_the_ones_a_folder_refused(
    home: ro.Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One video the library takes a still for, one in a read-only folder below it."""
    ffmpeg(monkeypatch)
    (made,) = videos(home.library, 1)
    pack = home.library / "pack"
    pack.mkdir()
    (refused,) = videos(pack, 1)
    with read_only(pack), started(home, monkeypatch) as run:
        reports, lines = run.reports(), run.lines()
        shown = run.application.session.library.find(made)
    assert stills.destination(made, home.library).is_file()
    assert shown is not None and shown.paired_still == stills.destination(made, home.library)
    assert not stills.destination(refused, home.library).exists()
    assert sorted(run.tried) == [made, refused], "the scan the made still asked for retried nothing"
    (report,) = reports
    assert report.startswith(said_for(1, pack)), report
    assert lines == reports


# -- a scene's "Capture a new still" / "Regenerate" ------------------------------------------------


@pytest.fixture
def scene(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MediaItem:
    """A Workshop scene, the engine installed and a display measured; it is never run here."""
    folder = tmp_path / "workshop" / SCENE
    folder.mkdir(parents=True)
    monkeypatch.setattr(scenes, "is_available", lambda: True)
    monkeypatch.setattr(scenes, "measured_capture_size", lambda: DISPLAY)

    def engine(*_arguments: object, **_keywords: object) -> None:
        raise AssertionError("the engine was run for a still its folder can't take")

    monkeypatch.setattr(scenes, "screenshot", engine)
    return MediaItem(path=folder, kind=Kind.SCENE, size=0, mtime=0, scene=SCENE)


def _the_library(profile: ro.Profile) -> tuple[Path, str]:
    return profile.library, f"could not create {pairing.still_directory(profile.library)}"


def _the_stills_folder(profile: ro.Profile) -> tuple[Path, str]:
    folder = pairing.still_directory(profile.library)
    folder.mkdir(parents=True)
    return folder, f"could not create a temporary still in {folder}"


@pytest.mark.parametrize("which", [_the_library, _the_stills_folder])
def test_regenerating_a_scene_still_says_which_folder_refused_and_why(
    home: ro.Profile,
    scene: MediaItem,
    monkeypatch: pytest.MonkeyPatch,
    which: Callable[[ro.Profile], tuple[Path, str]],
) -> None:
    """`Application.regenerate_scene_still`, which both windows' buttons call."""
    folder, wording = which(home)
    with read_only(folder), started(home, monkeypatch) as run:
        assert run.application.regenerate_scene_still(scene) is True
        spin_until(lambda: len(run.reports()) >= 1, ro.BOUND * 2, what="the first answer")
        run.wait()
        assert run.application.regenerate_scene_still(scene) is True
        spin_until(lambda: len(run.reports()) >= 2, ro.BOUND * 2, what="the second answer")
        run.wait()
        reports, lines = run.reports(), run.lines()
    expected = f"Could not regenerate the still for {SCENE}: {wording}: {DENIED}"
    assert reports == [expected, expected], "asked twice, answered twice"
    assert lines == reports


# -- the maker on its own --------------------------------------------------------------------------


def _maker(monkeypatch: pytest.MonkeyPatch) -> tuple[StillMaker, list[str], list[str]]:
    """A maker whose deliveries run where they are queued, and what it reported and noted."""
    reports: list[str] = []
    notes: list[str] = []
    monkeypatch.setattr(
        "wall_in_one.ui.stills.GLib.idle_add",
        lambda callback, *arguments, **_keywords: callback(*arguments),
    )
    return StillMaker(report=reports.append, note=notes.append), reports, notes


def _refusing(monkeypatch: pytest.MonkeyPatch, folder_of: Callable[[MediaItem], Path]) -> None:
    def ensure(
        item: MediaItem,
        _root: Path,
        *,
        refused: Callable[[stills.StillFolderError], None],
        **_keywords: object,
    ) -> None:
        folder = folder_of(item)
        refused(stills.StillFolderError(f"could not create {folder}: {DENIED}", folder, DENIED))

    monkeypatch.setattr(stills, "ensure", ensure)


def _clips(folder: Path, *names: str) -> list[MediaItem]:
    return [MediaItem(path=folder / name, kind=Kind.VIDEO, size=1, mtime=0) for name in names]


def test_the_maker_reports_each_folder_once_and_notes_it_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    maker, reports, notes = _maker(monkeypatch)
    _refusing(monkeypatch, lambda item: item.path.parent)
    made: list[int] = []
    first, second = Path("/library/a"), Path("/library/b")
    try:
        maker.request(
            [*_clips(first, "1.mp4", "2.mp4"), *_clips(second, "3.mp4")], first, made.append
        )
        maker._pool.submit(lambda: None).result(timeout=ro.BOUND)
        assert [text.split(". ")[0] for text in reports] == [
            said_for(2, first),
            said_for(1, second),
        ]
        maker.request(_clips(first, "4.mp4", "5.mp4", "6.mp4"), first, made.append)
        maker._pool.submit(lambda: None).result(timeout=ro.BOUND)
    finally:
        maker.shutdown()
    assert len(reports) == 2, "a folder already said is not reported again"
    assert [text.split(". ")[0] for text in notes] == [said_for(3, first)]
    assert made == [], "nothing was made, so no rescan was asked for"


def test_a_maker_that_was_shut_down_says_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    maker, reports, notes = _maker(monkeypatch)
    _refusing(monkeypatch, lambda item: item.path.parent)
    maker._processes.cancel()
    try:
        maker.request(_clips(Path("/library"), "1.mp4"), Path("/library"), lambda _made: None)
        maker._pool.submit(lambda: None).result(timeout=ro.BOUND)
    finally:
        maker.shutdown()
    assert reports == [] and notes == []
