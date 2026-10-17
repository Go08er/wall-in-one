"""The compatibility service's own scans are evidence its library is current (review Z-2).

Y-1's guard (`Application.library_currency_refusal`) lets nothing be taken
from, or judged by, a library that belongs to older sources. It exempted
"no sources accepted yet" as "never scanned", but ``--service`` scans
synchronously and never recorded what it scanned, so the exemption outlived
every real scan. Codex's sequence: the service scans two folders; a folder
is dropped through the forwarded GUI's route; the rescan after the save
fails; the library still names the dropped folder's wallpaper; the guard
said nothing, ``ctl playlist-add`` put that wallpaper in a playlist, and once
a later scan landed the compiler refused it, and every publication with it.

Now every successful synchronous scan records ``(roots, scan_workshop)`` as
it read them (`Session.refresh` → `Application._library_scanned`); a failed
one records nothing, so the earlier evidence stands. Real applications in
service mode, real scans; only the one failing scan is injected (on the main
thread, where the service rescans, so the batch's dry run on its worker
still passes, as in Codex's run). Each test fails on a44e22d.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_dry_run import _file  # noqa: E402
from tests.test_ui_runtime import FakeWindow, _attach, _close  # noqa: E402
from tests.whole_document import builds_whole  # noqa: E402
from wall_in_one import config  # noqa: E402
from wall_in_one.control import client, server  # noqa: E402
from wall_in_one.control.protocol import Request, Response  # noqa: E402
from wall_in_one.library import playlists, scan  # noqa: E402
from wall_in_one.ui.app import Application, _Commands  # noqa: E402

BOUND = 15.0
#: `runtime_config.LibraryNotCurrentError.kind`, spelled out.
NOT_CURRENT = "library-not-current"


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@dataclass
class Service:
    application: Application
    window: FakeWindow
    root: Path  # dawn.png, noon.png
    other: Path  # dusk.png
    failing: list[bool]

    @property
    def dusk(self) -> Path:
        return self.other / "dusk.png"

    @property
    def noon(self) -> Path:
        return self.root / "noon.png"

    def fail_the_next_rescan(self) -> None:
        self.failing[0] = True

    def holds_dusk(self) -> bool:
        return self.application.session.library.find(self.dusk) is not None


@pytest.fixture
def services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    made: list[Application] = []

    def make(*, start: bool = False) -> Service:
        """A real compatibility service over two folders, scanned as it scans.

        ``start``: through the service's own start (`_open_authoring_after_migration`),
        else its synchronous `refresh_library`, as a refresh asks for.
        """
        root, other = tmp_path / "lib", tmp_path / "other"
        _file(root / "dawn.png")
        _file(root / "noon.png")
        _file(other / "dusk.png")
        config.save(replace(config.Settings(), roots=(root, other), scan_workshop=False))
        application = Application(service=True, ui="next")
        made.append(application)
        window = FakeWindow()
        _attach(application, window)
        monkeypatch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())
        monkeypatch.setattr(application, "_make_missing_stills", lambda: None)
        failing = [False]
        real = scan.scan

        def scanning(*arguments: Any, **keywords: Any) -> Any:
            # Only the service's own rescan, on the main thread: a dry run on a
            # worker scans the same folders and must still pass.
            if failing[0] and threading.current_thread() is threading.main_thread():
                failing[0] = False
                raise OSError("injected: the rescan failed")
            return real(*arguments, **keywords)

        monkeypatch.setattr(scan, "scan", scanning)
        if start:
            application._open_authoring_after_migration()
            spin_until(
                lambda: application._authoring_migration_ready and lane_idle(application),
                BOUND,
                what="the service's start",
            )
        else:
            application._authoring_migration_ready = True
            application.refresh_library()
        service = Service(application, window, root, other, failing)
        assert service.holds_dusk(), "scanned both folders"
        return service

    yield make
    for application in made:
        _close(application)


def lane_idle(application: Application) -> bool:
    return (
        not application._authoring_active
        and not application._authoring_queue
        and not application._settings_authoring_running
    )


def ctl(application: Application, verb: str, argument: str) -> Response:
    replies: list[Response] = []
    server.dispatch(
        server.build_verb_table(_Commands(application)),
        Request(verb, argument).encode(),
        replies.append,
    )
    spin_until(lambda: bool(replies), BOUND, what=f"ctl {verb}")
    return replies[0]


def stale_and_refused(service: Service, mine: playlists.Playlist) -> None:
    """The library is the old one: the guard says so, and ctl takes nothing from it."""
    application = service.application
    assert service.holds_dusk(), "the old library, still"
    assert "being scanned for its changed folders" in application.library_currency_refusal()
    answer = ctl(application, "playlist-add", f"{mine.id} {service.dusk}")
    assert not answer.ok and answer.kind == NOT_CURRENT, answer
    assert application.session.playlists.find(mine.id).entries == ()


def current_again(service: Service, mine: playlists.Playlist, *, dusk: bool = False) -> None:
    """A rescan that succeeds is the evidence: the guard clears, and publication works."""
    application = service.application
    application.refresh_library()
    assert application.library_currency_refusal() == ""
    assert service.holds_dusk() is dusk, "the library the new sources make"
    answer = ctl(application, "playlist-add", f"{mine.id} {service.noon}")
    assert answer.ok, answer
    assert builds_whole(application.settings, application.session)


def test_codexs_sequence_a_dropped_folder_whose_rescan_failed_is_not_current(
    services: Any,
) -> None:
    """Through `update_settings_async`, the forwarded GUI's route. Before: guard empty, ctl ok."""
    service = services()
    application = service.application
    assert application._accepted_library_sources == ((service.root, service.other), False)
    mine = application.session.playlists.create("Mine")

    service.fail_the_next_rescan()
    assert application.update_settings_async(roots=(service.root,))
    spin_until(
        lambda: lane_idle(application) and application.settings.roots == (service.root,),
        BOUND,
        what="the saved folder change",
    )
    assert config.load_strict().roots == (service.root,), "saved"
    assert application._accepted_library_sources == ((service.root, service.other), False)
    stale_and_refused(service, mine)

    current_again(service, mine)
    assert application._accepted_library_sources == ((service.root,), False)


def test_after_the_services_own_start_a_folder_dropped_elsewhere_and_a_failed_rescan(
    services: Any,
) -> None:
    """The service's real start scans and records; settings.toml changed by another process."""
    service = services(start=True)
    application = service.application
    assert application._accepted_library_sources == ((service.root, service.other), False)
    mine = application.session.playlists.create("Mine")

    service.fail_the_next_rescan()
    config.update({"roots": (service.root,)})
    with pytest.raises(OSError, match="the rescan failed"):
        application._on_settings_changed(config.load_strict())  # as the file monitor does
    assert application.settings.roots == (service.root,)
    stale_and_refused(service, mine)

    current_again(service, mine)


def test_turning_on_workshop_scanning_whose_rescan_failed_is_not_current(
    services: Any,
) -> None:
    """The same roots: comparing only them would call the old library current."""
    service = services()
    application = service.application
    mine = application.session.playlists.create("Mine")

    service.fail_the_next_rescan()
    assert application.update_settings_async(scan_workshop=True)
    spin_until(
        lambda: lane_idle(application) and application.settings.scan_workshop,
        BOUND,
        what="the saved Workshop change",
    )
    assert application._accepted_library_sources == ((service.root, service.other), False)
    stale_and_refused(service, mine)

    current_again(service, mine, dusk=True)  # its folder is still a library folder
    assert application._accepted_library_sources == ((service.root, service.other), True)
