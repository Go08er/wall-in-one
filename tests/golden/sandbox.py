"""The sealed sandbox every golden-profile test runs in, and shared helpers.

The golden-profile tests are the compatibility gate for every later step of
the UI integration. They run the production code paths against a complete
profile -- the committed synthetic one in ``tests/golden/profile`` and,
opt-in, a sanitized copy of a real one -- and fail on any write nobody asked
for:

``test_idle``
    Start the service and the app the way the packaged install does (without
    GTK), idle through one 30 s health sync, close, and diff every file in
    the home against a whitelist of expected writes.
``test_forward_compat``
    A store file from a newer build is never rewritten, unknown keys survive
    edits, and ``settings.toml`` fails safe.
``test_downgrade`` (``-m downgrade``, needs ``WIO_OLD_SRC``)
    An older build edits what this build wrote; ``tools/golden-downgrade.sh``.
``test_sanitizer``
    ``tools/golden-profile-sanitize.py`` round-trips a profile without secrets.

``WIO_GOLDEN_PROFILE=<dir>`` adds a local profile directory (made by the
sanitizer) to the idle round trip.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

from tests.golden import harness
from tests.golden.harness import FakeRuntime, Profile
from wall_in_one import paths
from wall_in_one.control import client
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.theme import noctalia

LOCAL_PROFILE_ENV: Final = "WIO_GOLDEN_PROFILE"
STATE: Final = ".local/state/wall-in-one"
UNKNOWN_TOP: Final = "x-wall-in-one-next"
UNKNOWN_RECORD: Final = "x-note"
REAL_POPEN: Final = subprocess.Popen


@dataclass(frozen=True, slots=True)
class Golden:
    profile: Profile
    runtime: FakeRuntime
    processes: list[str]


def sources() -> list[Any]:
    """The committed fixture, plus ``WIO_GOLDEN_PROFILE`` when it is set."""
    found = [pytest.param(harness.FIXTURE, id="synthetic")]
    local = os.environ.get(LOCAL_PROFILE_ENV)
    if local:
        found.append(pytest.param(Path(local), id="local"))
    return found


def enter(source: Path, root: Path, runtime_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Golden:
    """Materialize ``source`` and seal this process inside it.

    Besides the XDG redirection, three doors to the live session are shut:
    the session bus (``update_status`` would inspect the real user manager),
    every child process (``noctalia msg``, ffmpeg, systemctl -- an idle start
    has no business spawning anything, so one is a finding), and the runtime
    socket, which a fake answers instead.
    """
    profile = harness.materialize(source, root, runtime_dir)
    for name, value in profile.environment().items():
        monkeypatch.setenv(name, value)
    for name in ("DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY", "DISPLAY", "NIRI_SOCKET"):
        monkeypatch.delenv(name, raising=False)
    assert paths.app_state_dir() == profile.app_state
    assert paths.settings_path().is_relative_to(profile.home)

    processes: list[str] = []

    class NoProcesses:
        def __init__(self, arguments: object, *_args: object, **_kwargs: object) -> None:
            processes.append(repr(arguments))
            raise AssertionError(f"the golden profile run tried to start a process: {arguments!r}")

    monkeypatch.setattr(subprocess, "Popen", NoProcesses)

    def no_noctalia(*_arguments: object, **_keywords: object) -> str:
        raise noctalia.NoctaliaError("Noctalia is not reachable from the golden profile sandbox")

    monkeypatch.setattr(noctalia, "_run", no_noctalia)
    runtime = FakeRuntime()
    monkeypatch.setattr(client, "send_runtime", runtime.send_runtime)
    return Golden(profile, runtime, processes)


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


def write_json(path: Path, document: dict[str, Any]) -> bytes:
    data = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()
    path.write_bytes(data)
    return data


def decorate(document: dict[str, Any]) -> set[str]:
    """Add what a newer build would: a top-level key and a key on every record.

    Returns the identities (``id`` or ``identity``) of the records it marked.
    """
    document[UNKNOWN_TOP] = {"written-by": "a newer build"}
    keys: set[str] = set()
    for value in list(document.values()):
        if isinstance(value, list):
            for record in value:
                if isinstance(record, dict):
                    record[UNKNOWN_RECORD] = "kept"
                    keys.add(str(record.get("id", record.get("identity"))))
    return keys


STORE_FILES: Final = (
    "pairings.json",
    "playlists.json",
    "schedules.json",
    "displays.json",
    "favourites.json",
    "pending-removals.json",
)


def broken_copies(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".broken*"))


def runtime_document(profile: Profile) -> dict[str, Any]:
    return tomllib.loads((profile.app_state / "runtime.toml").read_text())


def playlist_ids(profile: Profile) -> list[str]:
    return [entry["id"] for entry in read_json(profile.app_state / "playlists.json")["playlists"]]


def pairing_item(profile: Profile, index: int = 0) -> MediaItem:
    """The media item a stored pairing record names."""
    record = read_json(profile.app_state / "pairings.json")["pairings"][index]
    medium, _, source = record["identity"].partition(":")
    if medium == "scene":
        return MediaItem(path=profile.home, kind=Kind.SCENE, size=0, mtime=0, scene=source)
    kind = {"video": Kind.VIDEO, "still": Kind.STILL}[medium]
    return MediaItem(path=Path(source), kind=kind, size=0, mtime=0)
