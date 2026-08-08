"""Guards that apply to every test, whether or not it remembers to ask.

The suite drives an app whose whole job is changing the machine it runs on.
Individual tests have always patched the calls they knew about -- but "the
calls they knew about" is exactly the thing that goes stale, and it did:
step 10 made `session._apply` set the colour scheme as well as the wallpaper,
`tests/test_session.py` patched only `set_wallpaper`, and running the suite
quietly repainted the developer's desktop with the default generator.

So the rule is inverted here. Every call that changes Noctalia's state is
refused by default, at the module boundary, for every test in the suite. A
test that means to exercise one patches it, which is what the existing tests
already do; a test that does not mean to gets an `AssertionError` naming the
call instead of silently reaching out of the sandbox.

`noctalia msg` is a subprocess, so nothing here can be enforced by types or by
a fixture a test forgets to request. It has to be autouse and it has to be
here.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from wall_in_one.theme import noctalia

#: Everything in `theme.noctalia` that changes something outside this process.
#: Readers are left alone: they are harmless, several tests rely on them
#: failing naturally when the shell is absent, and pretending they are
#: dangerous would mean patching them everywhere for nothing.
MUTATORS: tuple[str, ...] = (
    "set_wallpaper",
    "set_scheme",
    "set_mode",
    "reload_config",
    "apply_templates",
)


@pytest.fixture(autouse=True)
def no_live_noctalia(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Refuse desktop mutation and isolate every conventional state path.

    The mutator guard protects Noctalia, but the app also owns playlists,
    schedules, caches and downloads. A test that constructs a default Store
    must not silently resolve those into the developer's home directory. The
    Nix sandbox exposed this when ``HOME=/homeless-shelter`` made such a write
    fail; outside the sandbox it had been succeeding against real app state.
    """

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))

    def refuse(name: str) -> Any:
        def called(*_arguments: object, **_keywords: object) -> None:
            raise AssertionError(
                f"a test called noctalia.{name}, which changes the live desktop. "
                "Patch it in the test if that is what you meant to exercise."
            )

        return called

    for name in MUTATORS:
        monkeypatch.setattr(noctalia, name, refuse(name))
    yield
