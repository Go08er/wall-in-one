"""Driving mpvpaper for video wallpapers.

The predecessor to this app supervised mpvpaper from 886 lines of shell, which
needed `socat` or a sufficiently capable `nc` just to reach mpv's IPC socket.
Python speaks AF_UNIX directly, so that whole dependency and its capability
probing are gone -- see `command`.

One mpvpaper process at a time. It renders to every output by default, which is
what `ALL` means to mpvpaper.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from wall_in_one import file_io, paths
from wall_in_one.wallpaper import outputs

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

#: Temporal presentation modes forwarded to mpv.  ``oversample`` is the
#: inexpensive, sharper choice recommended for low-frame-rate animation;
#: ``linear`` blends more strongly and can therefore ghost more.  Neither is
#: an FPS filter: mpv still decodes the source at its native rate.
INTERPOLATION_CHOICES: Final[tuple[str, ...]] = ("off", "oversample", "linear")
DEFAULT_INTERPOLATION: Final = "off"

#: mpv's own scale, where 100 is the file's own level. It accepts more, but
#: amplifying a wallpaper past its own volume is not something to reach by
#: dragging a slider to the end.
MAX_VOLUME: Final = 100

#: How long to wait for a polite shutdown before insisting.
TERMINATE_TIMEOUT: Final = 3.0
PROCESS_GROUP_POLL_SECONDS: Final = 0.05

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


def _signal_process_group(pgid: int, requested: signal.Signals) -> None:
    """Signal the retained process group, tolerating an already-dead group."""
    try:
        os.killpg(pgid, requested)
    except ProcessLookupError:
        return
    except OSError as error:
        raise RendererError(f"cannot signal mpvpaper process group {pgid}: {error}") from error


def _process_group_exists(pgid: int) -> bool:
    """Whether the retained process-group identity still names any process."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It still exists even when a changed credential would forbid a signal.
        return True
    except OSError as error:
        raise RendererError(f"cannot inspect mpvpaper process group {pgid}: {error}") from error
    return True


def _wait_for_process_group_shutdown(
    process: subprocess.Popen[bytes], pgid: int, timeout: float
) -> tuple[bool, bool]:
    """Boundedly reap the leader and prove that every group member is gone."""
    deadline = time.monotonic() + max(timeout, 0.0)
    try:
        leader_reaped = process.poll() is not None
    except OSError as error:
        raise RendererError(f"cannot inspect mpvpaper while stopping it: {error}") from error

    while True:
        group_gone = not _process_group_exists(pgid)
        if leader_reaped and group_gone:
            return True, True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return leader_reaped, group_gone
        interval = min(PROCESS_GROUP_POLL_SECONDS, remaining)
        if leader_reaped:
            time.sleep(interval)
            continue
        try:
            process.wait(timeout=interval)
        except subprocess.TimeoutExpired:
            pass
        except OSError as error:
            raise RendererError(f"cannot reap mpvpaper while stopping it: {error}") from error
        else:
            leader_reaped = True


def _stop_process_group(process: subprocess.Popen[bytes], pgid: int) -> None:
    """Stop one exact process group or fail without surrendering ownership."""
    _signal_process_group(pgid, signal.SIGTERM)
    graceful_error: RendererError | None = None
    try:
        leader_reaped, group_gone = _wait_for_process_group_shutdown(
            process, pgid, TERMINATE_TIMEOUT
        )
    except RendererError as error:
        # A broken wait/probe is not evidence of death.  Still make the bounded
        # best effort to stop the owned group, then report the lost proof.
        graceful_error = error
        leader_reaped = group_gone = False

    if graceful_error is None and leader_reaped and group_gone:
        return

    _signal_process_group(pgid, signal.SIGKILL)
    try:
        leader_reaped, group_gone = _wait_for_process_group_shutdown(
            process, pgid, TERMINATE_TIMEOUT
        )
    except RendererError as error:
        raise RendererError(f"cannot confirm mpvpaper shutdown: {error}") from error

    if graceful_error is not None:
        raise RendererError(
            f"cannot confirm mpvpaper shutdown after an earlier wait failure: {graceful_error}"
        ) from graceful_error
    if not group_gone:
        raise RendererError(f"mpvpaper process group {pgid} did not exit after SIGKILL")
    if not leader_reaped:
        raise RendererError("mpvpaper did not exit after SIGKILL")


@dataclass(slots=True)
class _SocketNamespace:
    """One exact private directory retained for an mpv IPC lifetime."""

    directory: Path
    pin: file_io.PinnedPath = field(repr=False)

    @property
    def logical_socket(self) -> Path:
        return self.directory / "ipc.sock"

    @property
    def access_socket(self) -> Path:
        return Path("/proc/self/fd") / str(self.pin.descriptor) / "ipc.sock"

    @property
    def writer_socket(self) -> Path:
        return Path("/proc") / str(os.getpid()) / "fd" / str(self.pin.descriptor) / "ipc.sock"


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
        interpolation: str = DEFAULT_INTERPOLATION,
        muted: bool = True,
        volume: int = MAX_VOLUME,
    ) -> None:
        self.output = output
        self.layer = layer
        self.when_hidden = when_hidden
        self.hardware_decode = hardware_decode
        self.interpolation = (
            interpolation if interpolation in INTERPOLATION_CHOICES else DEFAULT_INTERPOLATION
        )
        self.muted = muted
        self.volume = volume
        self._process: subprocess.Popen[bytes] | None = None
        self._pgid: int | None = None
        self._video: Path | None = None
        self._socket: _SocketNamespace | None = None

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
        return self._socket.access_socket if self.is_running and self._socket is not None else None

    # -- lifecycle -------------------------------------------------------

    def _mpv_options(self, ipc_socket: Path | None, refresh_hz: float | None = None) -> str:
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
        # All four options are one feature. Without a trustworthy refresh rate
        # the setting would be partly configured and may remain inert inside
        # mpvpaper, so mixed/unknown outputs retain the source cadence.
        if self.interpolation != "off" and refresh_hz is not None and refresh_hz > 0:
            options.extend(
                (
                    "video-sync=display-resample",
                    "interpolation=yes",
                    f"tscale={self.interpolation}",
                )
            )
            options.append(f"display-fps-override={refresh_hz:.3f}")
        if ipc_socket is not None:
            options.append(f"input-ipc-server={ipc_socket}")
        return " ".join(options)

    def _allocate_socket_path(self) -> _SocketNamespace | None:
        """Reserve a private namespace for one mpv IPC socket.

        A fixed public pathname would need to decide whether an entry left
        there belongs to an old renderer.  A fresh mode-0700 directory avoids
        that destructive guess entirely and lets an old socket remain merely
        stale rather than making an unrelated replacement disposable.
        """
        try:
            directory = Path(
                tempfile.mkdtemp(prefix=f".{paths.APP_ID}-mpv-", dir=paths.runtime_dir())
            )
        except OSError:
            # IPC is optional; wallpaper playback still works without it.
            return None
        namespace: _SocketNamespace | None = None
        try:
            created = directory.lstat()
            pin = file_io.pin_directory_path(
                directory,
                expected_identity=(created.st_dev, created.st_ino),
                require_private=True,
            )
            namespace = _SocketNamespace(directory, pin)
            if len(os.fsencode(namespace.writer_socket)) <= MAX_SOCKET_PATH_BYTES:
                return namespace
        except OSError:
            pass
        if namespace is not None:
            self._cleanup_socket_path(namespace)
        # If the directory could not be pinned, its random name is not enough
        # authority to remove whatever now occupies it.
        return None

    @staticmethod
    def _cleanup_socket_path(namespace: _SocketNamespace | None) -> None:
        """Release the capability without treating a random name as ownership.

        Linux cannot create a directory and return its descriptor atomically.
        Even a random mode-0700 name can therefore be replaced before it is
        pinned.  The descriptor keeps later socket access on one exact
        directory generation, but it does not authorize deleting or moving
        that directory.  Leaving the now-inert namespace is the safe outcome.
        """
        if namespace is None:
            return
        namespace.pin.close()

    def start(self, video: Path) -> None:
        """Play ``video``, replacing whatever was playing."""
        if not is_available():
            raise MpvpaperUnavailableError("mpvpaper is not installed")
        if not video.is_file():
            raise RendererError(f"no such video: {video}")

        self.stop()
        ipc_namespace = self._allocate_socket_path()
        handed_off = False
        try:
            command = ["mpvpaper", "--layer", self.layer]
            if self.when_hidden == "pause":
                command.append("--auto-pause")
            elif self.when_hidden == "stop":
                command.append("--auto-stop")
            refresh_hz = None
            if self.interpolation != "off":
                refresh_hz = outputs.unambiguous_refresh_hz(
                    outputs.discover(), "" if self.output == ALL_OUTPUTS else self.output
                )
            ipc_socket = ipc_namespace.writer_socket if ipc_namespace is not None else None
            command += ["-o", self._mpv_options(ipc_socket, refresh_hz), self.output, str(video)]

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
        else:
            self._process = process
            # start_new_session makes the child both session leader and group
            # leader.  Retain that identity now: querying it after the leader
            # exits loses the descendants which are precisely what stop owns.
            self._pgid = process.pid
            self._video = video
            self._socket = ipc_namespace
            handed_off = True
        finally:
            if not handed_off:
                self._cleanup_socket_path(ipc_namespace)

    def stop(self) -> None:
        """Stop playback. Safe to call when nothing is running."""
        process = self._process
        socket_namespace = self._socket
        if process is not None:
            # All successful starts set this alongside the process.  Falling
            # back to its pid keeps manually injected/legacy state safe too;
            # store it before any fallible work so a failed stop retains it.
            pgid = self._pgid if self._pgid is not None else process.pid
            self._pgid = pgid
            _stop_process_group(process, pgid)
        elif self._pgid is not None and _process_group_exists(self._pgid):
            raise RendererError("mpvpaper process-group ownership has no process handle")

        # Only a proven-dead group releases process and socket ownership.  If
        # any bounded wait above fails, start() will call stop() again and
        # cannot launch a second renderer over the still-owned group.
        self._process = None
        self._pgid = None
        self._video = None
        self._socket = None
        self._cleanup_socket_path(socket_namespace)

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
        except ValueError, RecursionError:
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
