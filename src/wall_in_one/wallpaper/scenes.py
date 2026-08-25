"""Driving `linux-wallpaperengine` for Wallpaper Engine scenes.

The third renderer, beside Noctalia for stills and mpvpaper for video. Most
Workshop content never reaches here: 45 of the 49 wallpapers installed on the
development machine are plain `.mp4` files that mpvpaper plays, and only the
four `scene` ones need this. It is worth having anyway, because a scene is the
thing Wallpaper Engine is actually *for* and there is no other way to show one.

Shaped deliberately like `wallpaper.renderer`: one child process at a time,
its own session so stopping takes the whole group, and the same
break-before-make discipline. What differs is that a scene is named by its
Steam Workshop id rather than by a file, and that the program finds its own
assets -- verified against the real installation, where `--list-properties`
worked with no `--assets-dir` at all.

Taking a still is the other half. `--screenshot` writes one frame and then
carries on rendering forever, so the capture waits for the file to appear
*and settle* before stopping the process. Waiting only for it to appear
catches a half-written PNG, which would become somebody's wallpaper.

**The app takes the renderer over by default**, because a selected scene must
actually animate without a hidden setting edit. `linux-wallpaperengine` is a
single-instance-per-output program that other things may also drive, though,
so a foreign instance on the target output is reported rather than shouldered
aside. Ownership remains an explicit Settings switch for installations where
another controller should remain authoritative.

Capturing a still is exempt: it renders in window mode, touches no output, and
is how a scene gets a representative without anything appearing on screen.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from wall_in_one import worker_processes
from wall_in_one.wallpaper import outputs

#: What `--layer` should be on niri. Its own help says so: pairing with the
#: `place-within-backdrop` rule, without which the wallpaper is cloned into
#: every workspace in the overview.
DEFAULT_LAYER: Final = "background"

#: Frames to render before the screenshot is taken. Scenes animate in, and the
#: first frame is often the least representative one there is.
SCREENSHOT_DELAY_FRAMES: Final = 30

#: How long to wait for a screenshot before giving up. Generous: a scene has
#: shaders to compile on first run, and this is a ceiling rather than a budget.
SCREENSHOT_TIMEOUT: Final = 90.0

#: The file is written while the process keeps rendering, so it is only taken
#: once its size has stopped changing across two looks this far apart.
SETTLE_SECONDS: Final = 0.4

#: How long to wait for a polite shutdown before insisting.
TERMINATE_TIMEOUT: Final = 5.0
#: A cancelled capture has already received SIGKILL from its UI owner.  This
#: short wait is only for reaping it; quitting must not inherit two ordinary
#: five-second renderer shutdown windows.
CANCEL_WAIT_TIMEOUT: Final = 0.5

#: Native linux-wallpaperengine render-rate bounds. Unlike an mpv post-decode
#: filter, ``--fps`` limits the scene engine's own rendering work.
MIN_FPS: Final = 1
MAX_FPS: Final = 240
DEFAULT_FPS: Final = 30

#: ``linux-wallpaperengine`` accepts these exact values.  Empty means do not
#: pass the option and let the renderer use its own default.  Keeping that
#: state explicit lets the Settings UI offer a safe reset without exposing an
#: arbitrary command-line string.
SCALING_CHOICES: Final[tuple[str, ...]] = ("", "stretch", "fit", "fill")
DEFAULT_SCALING: Final = ""
CLAMP_CHOICES: Final[tuple[str, ...]] = ("", "clamp", "border", "repeat")
DEFAULT_CLAMP: Final = ""

#: `linux-wallpaperengine`'s own default is 15; scenes with audio are a
#: surprise on a wallpaper, so this app starts them silent like the video half.
DEFAULT_VOLUME: Final = 0

#: Landscape fallback when the compositor cannot report a mode. This is the
#: geometry proven against linux-wallpaperengine rather than its portrait
#: window-mode default.
DEFAULT_CAPTURE_SIZE: Final = (2560, 1440)


class SceneError(Exception):
    """A scene could not be started or captured."""


class SceneEngineUnavailableError(SceneError):
    """`linux-wallpaperengine` is not installed.

    Distinct from a failed run: without it the app is still a perfectly good
    wallpaper manager for stills and video, and the four scenes simply cannot
    be shown.
    """


def is_available() -> bool:
    return shutil.which("linux-wallpaperengine") is not None


def running_elsewhere(output: str = "", proc: Path = Path("/proc")) -> tuple[int, ...]:
    """Process ids of engines this app did not start.

    Read from `/proc` rather than by shelling out to `pgrep`, whose own help
    points out that a name over fifteen characters never matches -- and
    `linux-wallpaperengine` is twenty-two.

    ``output`` narrows it to instances rendering on one screen. An engine
    previewing in a window is not a conflict: it owns no output.
    """
    found: list[int] = []
    try:
        entries = sorted(proc.iterdir())
    except OSError:
        return ()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        arguments = [part for part in raw.split(b"\0") if part]
        if not arguments or not arguments[0].endswith(b"linux-wallpaperengine"):
            continue
        if output:
            wanted = output.encode()
            if b"--screen-root" not in raw or wanted not in arguments:
                continue
        elif b"--screen-root" not in raw:
            # No output asked about, and this one owns none either.
            continue
        found.append(int(entry.name))
    return tuple(found)


class SceneRenderer:
    """Supervises at most one `linux-wallpaperengine` process."""

    def __init__(
        self,
        *,
        output: str = "",
        layer: str = DEFAULT_LAYER,
        fps: int = DEFAULT_FPS,
        volume: int = DEFAULT_VOLUME,
        silent: bool = True,
        pause_when_covered: bool = True,
        scaling: str = "",
        clamp: str = "",
    ) -> None:
        self.output = output
        self.layer = layer
        self.fps = fps
        self.volume = volume
        self.silent = silent
        self.pause_when_covered = pause_when_covered
        self.scaling = scaling if scaling in SCALING_CHOICES else DEFAULT_SCALING
        self.clamp = clamp if clamp in CLAMP_CHOICES else DEFAULT_CLAMP
        self._process: subprocess.Popen[bytes] | None = None
        self._scene: str = ""

    @property
    def is_running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    @property
    def scene(self) -> str:
        """The Workshop id currently rendering, or ``""``."""
        return self._scene if self.is_running else ""

    def command(
        self,
        scene: str,
        screenshot: Path | None = None,
        *,
        window: tuple[int, int] | None = None,
        available_outputs: tuple[str, ...] | None = None,
    ) -> list[str]:
        """The argument list, built once so the tests can read it.

        `--screen-root` has to come *before* `--bg`: the help is explicit that
        the following options apply to the preceding screen, so ordering is
        part of the meaning rather than a style choice.
        """
        arguments = ["linux-wallpaperengine", "--layer", self.layer, "--fps", str(self.fps)]
        if self.silent:
            arguments.append("--silent")
        else:
            arguments += ["--volume", str(max(0, self.volume))]
        if not self.pause_when_covered:
            arguments.append("--no-fullscreen-pause")
        if screenshot is not None:
            arguments += [
                "--screenshot",
                str(screenshot),
                "--screenshot-delay",
                str(SCREENSHOT_DELAY_FRAMES),
            ]
            if window is not None:
                width, height = window
                arguments += ["--window", f"0x0x{width}x{height}"]
            # The positional form is deliberately window preview mode here.
            # Capture must never claim a compositor output.
            if self.scaling:
                arguments += ["--scaling", self.scaling]
            if self.clamp:
                arguments += ["--clamp", self.clamp]
            arguments.append(scene)
            return arguments

        targets = (
            (self.output,)
            if self.output
            else (
                available_outputs
                if available_outputs is not None
                else outputs.names(outputs.discover())
            )
        )
        if not targets:
            raise SceneError(
                "niri reported no usable outputs; refusing to open a scene preview window"
            )
        for target in targets:
            arguments += ["--screen-root", target]
            if self.scaling:
                arguments += ["--scaling", self.scaling]
            if self.clamp:
                arguments += ["--clamp", self.clamp]
            arguments += ["--bg", scene]
        return arguments

    def start(self, scene: str) -> None:
        """Render ``scene``, replacing whatever was rendering."""
        if not is_available():
            raise SceneEngineUnavailableError("linux-wallpaperengine is not installed")
        if not scene.strip():
            raise SceneError("no scene given")

        self.stop()
        try:
            process = subprocess.Popen(
                self.command(scene),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Its own session, so stopping takes the whole process group
                # and nothing is left orphaned behind it.
                start_new_session=True,
            )
        except OSError as error:
            raise SceneError(f"cannot start linux-wallpaperengine: {error}") from error
        self._process = process
        self._scene = scene

    def stop(self) -> None:
        process = self._process
        self._process = None
        self._scene = ""
        if process is None or process.poll() is not None:
            return
        _end(process)


def _end(process: subprocess.Popen[bytes], *, immediate: bool = False) -> None:
    """Ask the process group to stop, then insist.

    The group rather than the process: `linux-wallpaperengine` is started in
    its own session precisely so that whatever it spawned goes with it.
    """
    requested = signal.SIGKILL if immediate else signal.SIGTERM
    wait_timeout = CANCEL_WAIT_TIMEOUT if immediate else TERMINATE_TIMEOUT
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(process.pid, requested)
    try:
        process.wait(timeout=wait_timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=wait_timeout)


def screenshot(
    scene: str,
    destination: Path,
    *,
    timeout: float = SCREENSHOT_TIMEOUT,
    renderer: SceneRenderer | None = None,
    size: tuple[int, int] | None = None,
    processes: worker_processes.Cancellation | None = None,
    prepared_output: bool = False,
) -> Path:
    """Render ``scene`` until it has written one frame, then stop it.

    `--screenshot` writes the file and carries on rendering forever, so this
    waits for it to appear *and* for its size to settle before stopping. Only
    waiting for it to appear catches a half-written PNG, which would go on to
    become somebody's wallpaper and the source of their colour scheme.

    Always renders in window mode, whatever the renderer is configured for:
    taking a still must not put a scene on the desktop.
    """
    if not is_available():
        raise SceneEngineUnavailableError("linux-wallpaperengine is not installed")

    capture = SceneRenderer(
        layer=(renderer.layer if renderer else DEFAULT_LAYER),
        fps=(renderer.fps if renderer else DEFAULT_FPS),
        silent=True,
        pause_when_covered=False,
    )
    if prepared_output:
        try:
            opened = destination.stat()
        except OSError as error:
            raise SceneError(f"prepared scene screenshot output is unavailable: {error}") from error
        if not stat.S_ISREG(opened.st_mode) or opened.st_size != 0:
            raise SceneError("prepared scene screenshot output is not an empty regular file")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.unlink(missing_ok=True)

    try:
        process = subprocess.Popen(
            capture.command(
                scene,
                screenshot=destination,
                window=size or capture_size(renderer.output if renderer else ""),
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise SceneError(f"cannot start linux-wallpaperengine: {error}") from error

    if processes is not None and not processes.register(process):
        _end(process, immediate=True)
        raise SceneError(f"cancelled screenshot of the scene {scene}")

    try:
        _wait_for(
            destination,
            process,
            timeout,
            cancelled=processes.cancelled if processes is not None else None,
        )
    finally:
        if processes is not None:
            processes.unregister(process)
        _end(process, immediate=bool(processes is not None and processes.cancelled()))

    if not destination.is_file() or destination.stat().st_size == 0:
        if not prepared_output:
            destination.unlink(missing_ok=True)
        raise SceneError(f"linux-wallpaperengine wrote no screenshot for {scene}")
    return destination


def capture_size(
    output: str = "", found: tuple[outputs.Output, ...] | None = None
) -> tuple[int, int]:
    """Physical capture geometry for ``output``, with a safe landscape fallback."""
    screens = outputs.discover() if found is None else found
    selected = next((screen for screen in screens if screen.name == output), None)
    if selected is None and not output and screens:
        selected = screens[0]
    if selected is None:
        return DEFAULT_CAPTURE_SIZE
    if selected.physical_width > 0 and selected.physical_height > 0:
        return selected.physical_width, selected.physical_height
    if selected.width > 0 and selected.height > 0:
        return round(selected.width * selected.scale), round(selected.height * selected.scale)
    return DEFAULT_CAPTURE_SIZE


def _wait_for(
    destination: Path,
    process: subprocess.Popen[bytes],
    timeout: float,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Block until the screenshot has been written and has stopped growing."""
    deadline = time.monotonic() + timeout
    settled_at: int | None = None
    while time.monotonic() < deadline:
        if cancelled is not None and cancelled():
            raise SceneError("scene screenshot was cancelled")
        try:
            size = destination.stat().st_size
        except OSError:
            size = 0
        if process.poll() is not None and size <= 0:
            raise SceneError("linux-wallpaperengine stopped before writing a screenshot")
        if size > 0:
            if settled_at == size:
                return
            settled_at = size
        time.sleep(SETTLE_SECONDS)
    raise SceneError(f"timed out waiting for a screenshot of the scene after {timeout:.0f}s")
