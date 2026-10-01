"""Opening either window on the golden profile and idling, with the still maker live.

`test_ui_next_slice`'s golden idle test stubs `StillMaker.request`, and
`tests/golden/test_idle` never builds the application, so nothing checks what
the application's automatic-still maker writes while the app sits idle. Both
interfaces run it after every scan (`Application._make_missing_stills`).

The golden run is the nix check's machine: no linux-wallpaperengine and no
niri on PATH. No still can be captured, so the whitelist must hold as it is.
The still maker is spied on (its work runs) and waited for, since the
application's shutdown cancels it without waiting. Before the fix the golden
scene's 2x2 still was judged against a 2560x1440 guess, a capture was tried
with no engine, and two ``.wall-in-one-retained`` entries were left behind.

The last tests are the owner's machine, where the engine is installed and a
capture really replaces the still. An existing scene still is recaptured at
idle only when it is smaller than a display niri measured in both
directions: not for its shape (a 16:9 monitor listed first), and not
against a guess (niri unreachable).
"""

from __future__ import annotations

import shutil
import struct
import tempfile
import time
import zlib
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.golden import harness, sandbox  # noqa: E402
from tests.golden.harness import Allowance  # noqa: E402
from tests.golden.test_idle import first_start_writes  # noqa: E402
from tests.test_ui_next_slice import IDLE_SECONDS, _cache_touch  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402
    Step,
    application_lanes,
    run_application,
    settled,
)
from wall_in_one import thumbnails as thumbnail_cache  # noqa: E402
from wall_in_one.library import pairing, stills  # noqa: E402
from wall_in_one.library.model import Kind, MediaItem  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402
from wall_in_one.ui.stills import Callback, StillMaker  # noqa: E402
from wall_in_one.ui.window import MainWindow  # noqa: E402
from wall_in_one.wallpaper import outputs, scenes  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sandbox.Golden]:
    runtime_dir = Path(tempfile.mkdtemp(prefix="wio-run-"))
    try:
        yield sandbox.enter(harness.FIXTURE, tmp_path / "sandbox", runtime_dir, monkeypatch)
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


@pytest.mark.parametrize("ui", ["classic", "next"])
def test_idling_with_the_still_maker_writes_only_the_whitelist(
    golden: sandbox.Golden, monkeypatch: pytest.MonkeyPatch, ui: str
) -> None:
    def no_ffmpeg(item: MediaItem, **_keywords: object) -> Path:
        raise thumbnail_cache.ThumbnailError(f"no processes in the golden sandbox: {item.path}")

    # As in test_ui_next_slice: the thumbnailer's refused-ffmpeg temporary is a
    # sandbox artefact, not what this test is about.
    monkeypatch.setattr(thumbnail_cache, "generate", no_ffmpeg)
    monkeypatch.setattr(scenes, "is_available", lambda: False)
    monkeypatch.setattr(outputs, "is_available", lambda: False)
    asked: list[Path] = []
    request = StillMaker.request

    def spy(maker: StillMaker, items: Iterable[MediaItem], root: Path, callback: Callback) -> None:
        asked.append(root)
        request(maker, items, root, callback)

    monkeypatch.setattr(StillMaker, "request", spy)
    profile = golden.profile
    before = harness.snapshot(profile.home)
    application = Application() if ui == "classic" else Application(ui="next")
    lanes: list[object] = []

    def stills_idle() -> bool:
        maker = application._stills
        with maker._lock:
            return bool(asked) and not maker._pending

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, MainWindow if ui == "classic" else NextWindow)
        assert application.ui == ui
        yield (
            "the first scan and the still maker's batch",
            lambda: settled(application) and stills_idle(),
        )
        opened = time.monotonic()
        yield "an idle window", lambda: time.monotonic() > opened + IDLE_SECONDS
        yield "every tail to settle", lambda: settled(application) and stills_idle()
        lanes.extend(application_lanes(application))
        window.close()

    try:
        assert run_application(application, scenario()) == 0
    finally:
        for lane in lanes:
            lane.shutdown(wait=True)  # type: ignore[attr-defined]
    assert application._window is None
    assert asked, "the application asked its still maker for this library"
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


# -- the same selection on a real machine -----------------------------------------------

#: The owner's laptop panel (niri: eDP-1, 2560x1600 at scale 1.5) and the size
#: of its four existing scene stills.
LAPTOP = outputs.Output("eDP-1", physical_width=2560, physical_height=1600)
STILL_SIZE = (3840, 2400)


def _png_header(width: int, height: int) -> bytes:
    """Enough of a PNG for `stills._png_size`, which reads only the IHDR."""
    body = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    checksum = zlib.crc32(b"IHDR" + body) & 0xFFFFFFFF
    ihdr = struct.pack(">I", len(body)) + b"IHDR" + body + struct.pack(">I", checksum)
    return b"\x89PNG\r\n\x1a\n" + ihdr


def _asked_to_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    screens: tuple[outputs.Output, ...],
    still_size: tuple[int, int] = STILL_SIZE,
) -> list[MediaItem]:
    """What a scan's `StillMaker.request` hands to `stills.ensure`, for one scene."""
    root = tmp_path / "library"
    still = pairing.still_directory(root) / "2149140853.png"
    still.parent.mkdir(parents=True)
    still.write_bytes(_png_header(*still_size))
    scene_dir = tmp_path / "workshop" / "2149140853"
    scene_dir.mkdir(parents=True)
    item = MediaItem(
        path=scene_dir, kind=Kind.SCENE, size=0, mtime=0, scene="2149140853", paired_still=still
    )
    monkeypatch.setattr(outputs, "discover", lambda: screens)
    monkeypatch.setattr(scenes, "is_available", lambda: True)
    ensured: list[MediaItem] = []

    def ensure(item: MediaItem, _root: Path, **_keywords: object) -> None:
        ensured.append(item)

    monkeypatch.setattr(stills, "ensure", ensure)
    maker = StillMaker()
    try:
        maker.request([item], root, lambda _made: None)
        deadline = time.monotonic() + 10
        while True:
            with maker._lock:
                if not maker._pending:
                    break
            assert time.monotonic() < deadline, "the still maker never finished"
            time.sleep(0.01)
    finally:
        maker.shutdown()
    return ensured


def test_a_scene_still_the_size_of_the_display_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's machine as it is today: 3840x2400 stills on a 2560x1600 panel."""
    assert _asked_to_capture(tmp_path, monkeypatch, (LAPTOP,)) == []


def test_an_old_portrait_scene_still_is_still_recaptured_at_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deliberate since 3ba9ef9: smaller than the measured display both ways."""
    asked = _asked_to_capture(tmp_path, monkeypatch, (LAPTOP,), still_size=(1270, 1537))
    assert [item.scene for item in asked] == ["2149140853"]


@pytest.mark.parametrize(
    "screens",
    [
        pytest.param((), id="niri-not-reachable"),
    ],
)
def test_idle_maintenance_never_recaptures_an_existing_scene_still(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, screens: tuple[outputs.Output, ...]
) -> None:
    assert _asked_to_capture(tmp_path, monkeypatch, screens) == []
