"""Opening either window on the golden profile and idling, with the still maker live.

`test_ui_next_slice`'s golden idle test stubs `StillMaker.request`, and
`tests/golden/test_idle` never builds the application, so nothing checks what
the application's automatic-still maker writes while the app sits idle. Both
interfaces run it after every scan (`Application._make_missing_stills`).

The golden run is the nix check's machine: no linux-wallpaperengine and no
niri on PATH. No still can be captured, so the whitelist must hold as it is.
A second machine has the engine and a measured display but refuses the
engine's process: there the only extra write allowed is the one claim pair a
withdrawn named temporary leaves (the engine needs a ``.png`` name).
A third is the documented exception to the idle rule (docs/updating.md, "What
opening the app writes"): the engine captures, and the 2x2 golden scene still
is smaller than the measured display both ways, so idling replaces exactly
that managed still with the captured frame (plus the claim pair the replaced
inode leaves) and asks the engine once, rescan included.
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
import subprocess
import sys
import tempfile
import time
import zlib
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.golden import harness, sandbox  # noqa: E402
from tests.golden.harness import Allowance, Change  # noqa: E402
from tests.golden.test_idle import first_start_writes  # noqa: E402
from tests.golden.test_stills_idle import RETAINED, assert_one_claim_pair  # noqa: E402
from tests.test_stills import _real_png  # noqa: E402
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


#: The automatic stills folder of the golden profile's first library root.
STILLS = RETAINED.removesuffix(".wall-in-one-retained/")
#: The display niri measured on the capturing machine: larger than the golden
#: scene's 2x2 still in both directions, and small enough for a tiny PNG.
CAPTURED_SIZE = (64, 40)
CAPTURED_FRAME = _real_png(*CAPTURED_SIZE)
#: Stands in for linux-wallpaperengine: writes one frame to the exact
#: ``--screenshot`` path it is given, then keeps running like the engine.
FAKE_ENGINE = (
    "import sys, time\nopen(sys.argv[1], 'wb').write(bytes.fromhex(sys.argv[2]))\ntime.sleep(60)\n"
)


@pytest.mark.parametrize("machine", ["nix-check", "engine-refused", "engine-captures"])
@pytest.mark.parametrize("ui", ["classic", "next"])
def test_idling_with_the_still_maker_writes_only_the_whitelist(
    golden: sandbox.Golden, monkeypatch: pytest.MonkeyPatch, ui: str, machine: str
) -> None:
    def no_ffmpeg(item: MediaItem, **_keywords: object) -> Path:
        raise thumbnail_cache.ThumbnailError(f"no processes in the golden sandbox: {item.path}")

    # As in test_ui_next_slice: the thumbnailer's refused-ffmpeg temporary is a
    # sandbox artefact, not what this test is about.
    monkeypatch.setattr(thumbnail_cache, "generate", no_ffmpeg)
    engine_started: list[object] = []
    if machine == "nix-check":
        # No engine, no niri: nothing at all may be written.
        monkeypatch.setattr(scenes, "is_available", lambda: False)
        monkeypatch.setattr(outputs, "is_available", lambda: False)
    elif machine == "engine-captures":
        # The documented exception to the idle rule: the engine is installed,
        # niri measured a display larger than the 2x2 golden scene still in
        # both directions, and the capture succeeds. The still is replaced.
        monkeypatch.setattr(scenes, "is_available", lambda: True)
        monkeypatch.setattr(scenes, "measured_capture_size", lambda *_arguments: CAPTURED_SIZE)
        guarded: Callable[..., object] = subprocess.Popen

        def capturing_engine(arguments: object, *args: Any, **kwargs: Any) -> object:
            if isinstance(arguments, list) and arguments[:1] == ["linux-wallpaperengine"]:
                engine_started.append(arguments)
                path = arguments[arguments.index("--screenshot") + 1]
                command = [sys.executable, "-c", FAKE_ENGINE, path, CAPTURED_FRAME.hex()]
                return sandbox.REAL_POPEN(command, *args, **kwargs)
            return guarded(arguments, *args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", capturing_engine)
    else:
        # The engine is installed and niri measured the panel, so the 2x2
        # golden still is due; but the engine's process is refused. Its named
        # temporary then leaves one claim pair (accepted for now), and nothing
        # else may change.
        monkeypatch.setattr(scenes, "is_available", lambda: True)
        monkeypatch.setattr(scenes, "measured_capture_size", lambda *_arguments: (2560, 1600))
        sealed: Callable[..., object] = subprocess.Popen

        def refuse_the_engine(arguments: object, *args: object, **kwargs: object) -> object:
            if isinstance(arguments, list) and arguments[:1] == ["linux-wallpaperengine"]:
                engine_started.append(arguments)
                raise PermissionError(13, "process start refused")
            return sealed(arguments, *args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", refuse_the_engine)
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
    # Exactly one: the rescan a capture causes must not ask for the same scene again.
    assert len(engine_started) == (machine != "nix-check"), "one capture was tried"
    changes = harness.diff(before, harness.snapshot(profile.home))
    if machine != "nix-check":
        # The named temporary (refused engine) or the replaced still (capture)
        # is withdrawn through file_io's claim-and-retain, which leaves one
        # empty claim pair; the rmdir follow-up will remove it.
        assert_one_claim_pair([c for c in changes if c.path.startswith(RETAINED)])
        changes = [c for c in changes if not c.path.startswith(RETAINED)]
    allowed = [
        *first_start_writes(profile, None),
        Allowance(
            ".cache/wall-in-one/thumbnails/*",
            frozenset({"rewritten"}),
            "the GUI's thumbnail cache re-stamps an entry it served (bytes unchanged)",
            _cache_touch,
        ),
    ]
    if machine == "engine-captures":
        replaced = [c for c in changes if c.path.startswith(STILLS) and c.path.endswith(".png")]
        assert len(replaced) == 1, [c.describe() for c in replaced]
        allowed.append(
            Allowance(
                f"{STILLS}*.png",
                frozenset({"modified"}),
                "the documented idle exception: the scene's managed still was smaller than "
                "the measured display in both directions, so it is captured again",
                _captured_frame,
            )
        )
    harness.check_changes(changes, allowed)


def _captured_frame(change: Change) -> None:
    """The 2x2 golden scene still, replaced by exactly the frame the engine wrote."""
    assert change.before is not None and change.before.content is not None
    assert change.after is not None and change.after.content == CAPTURED_FRAME
    assert struct.unpack(">II", change.before.content[16:24]) == (2, 2)


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
        pytest.param(
            (outputs.Output("DP-1", physical_width=1920, physical_height=1080), LAPTOP),
            id="a-16:9-monitor-listed-first",
        ),
    ],
)
def test_idle_maintenance_never_recaptures_an_existing_scene_still(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, screens: tuple[outputs.Output, ...]
) -> None:
    assert _asked_to_capture(tmp_path, monkeypatch, screens) == []
