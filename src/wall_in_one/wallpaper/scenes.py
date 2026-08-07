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

**This app does not take the renderer over by default**, and that is not
timidity. `linux-wallpaperengine` is a single-instance-per-output program that
other things also drive: the development machine has Noctalia's own
`linux-wallpaperengine-controller` plugin enabled and an engine process that
has been rendering the desktop for hours. Starting a second one on the same
output is two programs fighting for one wallpaper, and the loser is whichever
the user was actually looking at. So owning the renderer is a decision the user
makes -- `config.Settings.own_scene_renderer` -- and even then a foreign
instance on the same output is reported rather than shouldered aside.

Capturing a still is exempt: it renders in window mode, touches no output, and
is how a scene gets a representative without anything appearing on screen.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Final

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

#: Matches mpvpaper's default in `renderer`, for the same battery reason.
DEFAULT_FPS: Final = 30

#: `linux-wallpaperengine`'s own default is 15; scenes with audio are a
#: surprise on a wallpaper, so this app starts them silent like the video half.
DEFAULT_VOLUME: Final = 0


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
        self.scaling = scaling
        self.clamp = clamp
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

    def command(self, scene: str, screenshot: Path | None = None) -> list[str]:
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

        if self.output:
            arguments += ["--screen-root", self.output]
            if self.scaling:
                arguments += ["--scaling", self.scaling]
            if self.clamp:
                arguments += ["--clamp", self.clamp]
            arguments += ["--bg", scene]
            return arguments

        # No output named: the scene is the positional argument, which is how
        # its own help spells "everywhere" and also how it previews in a window.
        if self.scaling:
            arguments += ["--scaling", self.scaling]
        if self.clamp:
            arguments += ["--clamp", self.clamp]
        arguments.append(scene)
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


def _end(process: subprocess.Popen[bytes]) -> None:
    """Ask the process group to stop, then insist.

    The group rather than the process: `linux-wallpaperengine` is started in
    its own session precisely so that whatever it spawned goes with it.
    """
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=TERMINATE_TIMEOUT)
        return
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=TERMINATE_TIMEOUT)


def screenshot(
    scene: str,
    destination: Path,
    *,
    timeout: float = SCREENSHOT_TIMEOUT,
    renderer: SceneRenderer | None = None,
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
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)

    try:
        process = subprocess.Popen(
            capture.command(scene, screenshot=destination),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise SceneError(f"cannot start linux-wallpaperengine: {error}") from error

    try:
        _wait_for(destination, process, timeout)
    finally:
        _end(process)

    if not destination.is_file() or destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise SceneError(f"linux-wallpaperengine wrote no screenshot for {scene}")
    return destination


def _wait_for(destination: Path, process: subprocess.Popen[bytes], timeout: float) -> None:
    """Block until the screenshot has been written and has stopped growing."""
    deadline = time.monotonic() + timeout
    settled_at: int | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None and not destination.is_file():
            raise SceneError("linux-wallpaperengine stopped before writing a screenshot")
        try:
            size = destination.stat().st_size
        except OSError:
            size = 0
        if size > 0:
            if settled_at == size:
                return
            settled_at = size
        time.sleep(SETTLE_SECONDS)
    raise SceneError(f"timed out waiting for a screenshot of the scene after {timeout:.0f}s")
