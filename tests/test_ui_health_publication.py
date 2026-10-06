"""Health markers saved before their publication was refused are adopted and said.

The runtime reports a wallpaper it couldn't play; the health lane saves that
as a pairing marker, then compiles and installs a new runtime document. Here
the install fails for real: ``runtime-overrides.toml`` can't be read (mode
000). That `RuntimeConfigError` was caught as a busy or stale snapshot: the
saved markers were never adopted (the app kept offering the wallpaper as
playable) and nothing was said. Now the markers are adopted and the failure
goes the way every publication problem does: said in the window, and kept as
the publication problem until one gets through.

Driven through the real application: startup, the first scan, a default
playlist, then the fake runtime's status poll reporting the taboo.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_ui_next_editing import first_scan, reports_of, wallpaper  # noqa: E402
from tests.test_ui_next_slice import finish, run  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402,F401
    FakeRuntime,
    Step,
    sandboxed_runtime,
    settled,
    toolkit,
)
from wall_in_one import paths, runtime_config, runtime_health  # noqa: E402
from wall_in_one.library import pairings, playlists  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402

REFUSED = "runtime-overrides.toml"


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRuntime:
    return sandboxed_runtime(monkeypatch, tmp_path)


def test_markers_saved_before_a_refused_publication_are_adopted_and_said(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        said = reports_of(window, monkeypatch)
        yield first_scan(application, window)
        dawn = wallpaper(tmp_path, "dawn.png")
        made = application.session.playlists.create(
            "Evening", entries=[playlists.Entry("dawn-entry", dawn)]
        )
        application.playlists_changed()
        yield "the playlist to be published", lambda: settled(application)
        saved: list[bool] = []
        assert application.update_settings_async(
            active_playlist=made.id, on_success=lambda _settings: saved.append(True)
        )
        yield "the default to be published", lambda: bool(saved) and settled(application)
        (item,) = [found for found in application.session.library.items if str(found.path) == dawn]
        identity = pairings.Identity.of(item)
        assert not application.session.pairings.health(identity).is_borked
        heard = len(said)
        overrides = runtime_config.overrides_path(paths.runtime_config_path())
        assert not overrides.exists()
        overrides.write_text("")
        overrides.chmod(0)  # the install after the markers can't read it
        runtime.answer(
            {
                "config_generation": runtime_config.read_config_generation(),
                "config_path": str(paths.runtime_config_path().absolute()),
                "runtime_instance": "a" * runtime_health.RUNTIME_INSTANCE_HEX_CHARS,
                "config_epoch": 1,
                "playlist_id": made.id,
                "playlist": made.name,
                "source": "schedule",
                "taboo_entries": [
                    {
                        "playlist_id": made.id,
                        "entry_id": "dawn-entry",
                        "reason": "renderer rejected this wallpaper",
                        "source": "automatic-apply",
                        "durable": False,
                        "observed_config_epoch": 1,
                    }
                ],
                "taboo_entries_omitted": 0,
            }
        )
        yield (
            "the status poll's health to land",
            lambda: any(REFUSED in m for m in said[heard:]),
        )

        assert pairings.Store.open().health(identity).is_borked, "the marker is saved"
        # Before: never adopted (still playable here) and never said.
        assert application.session.pairings.health(identity).is_borked
        (problem,) = [m for m in said[heard:] if REFUSED in m]
        assert problem.startswith("cannot read") and "Permission denied" in problem, problem
        assert application.publication_problem == problem
        runtime.answer(None)
        overrides.unlink()
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)
