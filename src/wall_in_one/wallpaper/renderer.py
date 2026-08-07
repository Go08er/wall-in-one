"""Driving mpvpaper for video wallpapers.

The predecessor to this app supervised mpvpaper from 886 lines of shell, which
needed `socat` or a sufficiently capable `nc` just to reach mpv's IPC socket.
Python speaks AF_UNIX directly, so that whole dependency and its capability
probing are gone -- see `command`.

One mpvpaper process at a time. It renders to every output by default, which is
what `ALL` means to mpvpaper.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
from pathlib import Path
from typing import Any, Final

from wall_in_one import paths

#: mpvpaper's selector for every connected output.
ALL_OUTPUTS: Final = "ALL"

#: Wayland layer to render on. `background` puts it under everything.
DEFAULT_LAYER: Final = "background"

#: What to do with a video nobody can see because a window is covering it.
#: mpvpaper spells these `--auto-pause` and `--auto-stop`, and its own help
#: warns that they "might not work as intended" -- which is exactly why turning
#: them off has to stay reachable rather than being decided here for everyone.
#:
#: `pause` keeps the process and its decoded state, so coming back is instant
#: and only CPU is saved. `stop` gives the memory back too and resumes more
#: abruptly. `play` keeps decoding a picture nobody is looking at, which is
#: only sensible when the auto options misbehave on a particular compositor.
WHEN_HIDDEN_CHOICES: Final[tuple[str, ...]] = ("pause", "stop", "play")
DEFAULT_WHEN_HIDDEN: Final = "pause"

#: mpv's own scale, where 100 is the file's own level. It accepts more, but
#: amplifying a wallpaper past its own volume is not something to reach by
#: dragging a slider to the end.
MAX_VOLUME: Final = 100

#: How long to wait for a polite shutdown before insisting.
TERMINATE_TIMEOUT: Final = 3.0

#: mpv IPC replies are small; this only stops a wedged socket eating memory.
MAX_IPC_REPLY_BYTES: Final = 64 * 1024
IPC_TIMEOUT: Final = 2.0

#: AF_UNIX paths are capped near 108 bytes. Past that, mpv silently fails to
#: create the socket, so we skip IPC rather than pretend we have it.
MAX_SOCKET_PATH_BYTES: Final = 100


class RendererError(Exception):
    """The video renderer could not be started or controlled."""


class MpvpaperUnavailableError(RendererError):
    """mpvpaper is not installed."""


def is_available() -> bool:
    return shutil.which("mpvpaper") is not None


class Renderer:
    """Supervises at most one mpvpaper process."""

    def __init__(
        self,
        *,
        output: str = ALL_OUTPUTS,
        layer: str = DEFAULT_LAYER,
        when_hidden: str = DEFAULT_WHEN_HIDDEN,
        hardware_decode: bool = True,
        muted: bool = True,
        volume: int = MAX_VOLUME,
    ) -> None:
        self.output = output
        self.layer = layer
        self.when_hidden = when_hidden
        self.hardware_decode = hardware_decode
        self.muted = muted
        self.volume = volume
        self._process: subprocess.Popen[bytes] | None = None
        self._video: Path | None = None
        self._socket: Path | None = None

    # -- state -----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def video(self) -> Path | None:
        """The video currently playing, or None."""
        return self._video if self.is_running else None

    @property
    def ipc_socket(self) -> Path | None:
        """mpv's IPC socket, if one could be created."""
        return self._socket if self.is_running else None

    # -- lifecycle -------------------------------------------------------

    def _mpv_options(self, ipc_socket: Path | None) -> str:
        options = [
            "loop-file=inf",
            # Fill the screen rather than letterboxing; a wallpaper with black
            # bars is not a wallpaper.
            "panscan=1.0",
            "terminal=no",
            # Keep the audio track loaded even when muted, so it can be unmuted
            # later over IPC. `no-audio` would throw that control away.
            "mute=yes" if self.muted else "mute=no",
            # Set even while muted, so unmuting over IPC lands at the level the
            # user chose rather than at whatever mpv defaulted to.
            f"volume={max(0, min(MAX_VOLUME, self.volume))}",
            "hwdec=auto" if self.hardware_decode else "hwdec=no",
        ]
        if ipc_socket is not None:
            options.append(f"input-ipc-server={ipc_socket}")
        return " ".join(options)

    def _socket_path(self) -> Path | None:
        candidate = paths.runtime_dir() / f"{paths.APP_ID}-mpv.sock"
        if len(os.fsencode(candidate)) > MAX_SOCKET_PATH_BYTES:
            return None
        candidate.unlink(missing_ok=True)
        return candidate

    def start(self, video: Path) -> None:
        """Play ``video``, replacing whatever was playing."""
        if not is_available():
            raise MpvpaperUnavailableError("mpvpaper is not installed")
        if not video.is_file():
            raise RendererError(f"no such video: {video}")

        self.stop()
        ipc_socket = self._socket_path()
        command = ["mpvpaper", "--layer", self.layer]
        if self.when_hidden == "pause":
            command.append("--auto-pause")
        elif self.when_hidden == "stop":
            command.append("--auto-stop")
        command += ["-o", self._mpv_options(ipc_socket), self.output, str(video)]

        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Its own session, so stopping it takes the whole process group
                # and mpv is never left orphaned behind mpvpaper.
                start_new_session=True,
            )
        except OSError as error:
            raise RendererError(f"cannot start mpvpaper: {error}") from error

        self._process = process
        self._video = video
        self._socket = ipc_socket

    def stop(self) -> None:
        """Stop playback. Safe to call when nothing is running."""
        process = self._process
        self._process = None
        self._video = None
        socket_path = self._socket
        self._socket = None

        if process is not None and process.poll() is None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                process.terminate()
            try:
                process.wait(timeout=TERMINATE_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    process.kill()
                # Reap it, or it lingers as a zombie for the app's lifetime.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=TERMINATE_TIMEOUT)

        if socket_path is not None:
            socket_path.unlink(missing_ok=True)

    # -- mpv IPC ---------------------------------------------------------

    def command(self, *arguments: Any) -> dict[str, Any] | None:
        """Send one mpv IPC command. None if IPC is unavailable.

        IPC is a convenience, not the control path -- if mpv never created the
        socket the renderer still works, you just cannot retune it live.
        """
        socket_path = self.ipc_socket
        if socket_path is None or not socket_path.exists():
            return None
        payload = json.dumps({"command": list(arguments)}).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(IPC_TIMEOUT)
                client.connect(str(socket_path))
                client.sendall(payload)
                reply = b""
                while b"\n" not in reply and len(reply) < MAX_IPC_REPLY_BYTES:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    reply += chunk
        except OSError:
            return None
        line = reply.split(b"\n", 1)[0]
        try:
            decoded = json.loads(line)
        except ValueError:
            return None
        return decoded if isinstance(decoded, dict) else None

    def set_property(self, name: str, value: Any) -> bool:
        reply = self.command("set_property", name, value)
        return bool(reply and reply.get("error") == "success")

    def set_muted(self, muted: bool) -> bool:
        self.muted = muted
        return self.set_property("mute", muted)

    def set_volume(self, volume: int) -> bool:
        """Retune the volume of the video already playing.

        The value is kept even when IPC is unavailable, so the next `start`
        launches at the right level. That is the difference worth preserving
        between "the setting did not take" and "the setting did not take *yet*".
        """
        self.volume = max(0, min(MAX_VOLUME, volume))
        return self.set_property("volume", self.volume)

    def apply_audio(self, *, muted: bool, volume: int) -> None:
        """Push both audio settings at a running video, tolerating no IPC.

        Volume first: unmuting at the previous level and then correcting it
        would put a moment of the wrong loudness through the speakers, which is
        the one mistake here that a person actually hears.
        """
        self.set_volume(volume)
        self.set_muted(muted)

    def set_paused(self, paused: bool) -> bool:
        return self.set_property("pause", paused)
