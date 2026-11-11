"""review-f4 F4-2: turning web wallpapers off reaches the service at once, whatever holds it.

S3-1 met the contract for one shape (the scan web-off itself starts). Three more held it:

1. a folder or Workshop-scanning scan already in flight from an earlier save: the publication's
   library was the older folders', so its compile was refused and deferred until the scan ended;
2. the same save changing the folders: the same, whichever of the scan and the compile came
   first;
3. ``--service``, previews on: its synchronous rescan ran (and raised) before the publication.

Now web-off is published from the library held, whatever sources it was scanned for: while web
wallpapers are off every web page and all a preview could draw a still for is left out
(`workshop.web_off`), so that is safe. Nothing holds it, the scan's own publication follows, and
one held anyway (a removal in its way) runs the web-off guard. Each test holds the scan (or
makes it fail) and checks that ``reload`` went out while it was held, and that no
``runtime.toml`` that allows web wallpapers is left; every shape runs with the previews
sub-option on (an upgraded 0.4.1 profile) and off.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_codex030_round1b import profiles  # noqa: E402, F401
from tests.test_ui_service_library_currency import services, toolkit  # noqa: E402, F401
from tests.test_ui_web_off_failed_publish import _steam_with_web  # noqa: E402
from tests.test_ui_web_off_lanes import ALLOWING, READS_EIGHT  # noqa: E402
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.library import scan as scan_module  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402

#: How long the service gets to hear of it while the scan is held.
AT_ONCE = 15.0


def _pump(seconds: float) -> None:
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


class Service:
    """A recorder for the service's socket that loads what a reload finds on disk.

    Its status says what a service that reads schema 8 says: whether the document it
    holds allows web wallpapers, so the guard (after a scan fails) judges what it holds.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.loaded = ALLOWING

    def send(self, verb: str, *_arguments: object, **_keywords: object) -> Response:
        self.sent.append(verb)
        if verb == "reload":
            self.loaded = paths.runtime_config_path().read_text(encoding="utf-8")
        if verb == "status":
            return Response.success(
                json.dumps(
                    {
                        "status_version": 2,
                        "supported_config_schemas": READS_EIGHT,
                        "playback_state": "playing",
                        "allow_web_wallpapers": runtime_config.allows_web_wallpapers(
                            text=self.loaded
                        ),
                    }
                )
            )
        return Response.success()

    def allows_web(self) -> bool:
        return runtime_config.allows_web_wallpapers(text=self.loaded)


def _web_on(application: Any, previews: bool, monkeypatch: pytest.MonkeyPatch) -> Service:
    """Web wallpapers on (previews as given), a service holding a document that allows them."""
    _steam_with_web()
    config.save(
        config.Settings(
            roots=application.settings.roots,
            allow_web_wallpapers=True,
            web_wallpaper_previews=previews,
        )
    )
    application._settings = config.load_strict()
    application._settings_requested = application._settings
    application._accepted_library_sources = application._settings.library_sources
    application.session.update_settings(application._settings, rescan_library=False)
    paths.runtime_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_config_path().write_text(ALLOWING, encoding="utf-8")
    service = Service()
    monkeypatch.setattr(client, "send_runtime", service.send)
    return service


class HeldScans:
    """Every library scan on a scan worker waits for `release`, then fails or not."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, fails: bool) -> None:
        self.gate = threading.Event()
        self.fails = fails
        real = scan_module.scan

        def held(*arguments: Any, **keywords: Any) -> Any:
            if threading.current_thread().name.startswith("library-scan"):
                self.gate.wait(60)
                if self.fails:
                    raise OSError("injected: the scan failed")
            return real(*arguments, **keywords)

        monkeypatch.setattr(scan_module, "scan", held)

    def release(self) -> None:
        self.gate.set()


def _save(application: Any, **changes: Any) -> None:
    done: list[object] = []
    assert application.update_settings_async(
        on_success=done.append, on_error=done.append, **changes
    )
    spin_until(lambda: bool(done), 30, what=f"the save of {sorted(changes)}")
    assert not any(isinstance(outcome, str) for outcome in done), done


def _reaches_the_service_while_held(
    application: Any, service: Service, scans: HeldScans, *, settles: bool = True
) -> None:
    spin_until(lambda: "reload" in service.sent, AT_ONCE, what="web off reaching the service")
    if settles:
        # It settles as a publication of its own, not one deferred to the scan's.
        spin_until(
            lambda: not application._runtime_publication_held, AT_ONCE, what="web off settling"
        )
    assert not scans.gate.is_set(), "the scan was still held"
    assert config.load_strict().allow_web_wallpapers is False
    assert not service.allows_web(), "the service loaded a document that allows web wallpapers"
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())


def _and_after_the_scan(service: Service, scans: HeldScans) -> None:
    scans.release()
    _pump(4.0)
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())
    assert not service.allows_web()
    assert "stop" not in service.sent and "quit" not in service.sent, service.sent


previews_param = pytest.mark.parametrize(
    "previews", [False, True], ids=["previews-off", "previews-on"]
)
scan_param = pytest.mark.parametrize("fails", [False, True], ids=["scan-held", "scan-fails"])


@previews_param
@scan_param
def test_web_off_while_an_earlier_folder_scan_is_in_flight(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    previews: bool,
    fails: bool,
) -> None:
    """Shape 1: on 396f103 nothing was sent until the scan ended (and then the guard ran)."""
    application = profiles().application
    service = _web_on(application, previews, monkeypatch)
    scans = HeldScans(monkeypatch, fails=fails)
    extra = tmp_path / "extra"
    extra.mkdir()
    _save(application, roots=(*application.settings.roots, extra))
    spin_until(lambda: application._runtime_library_scan_pending, 10, what="the scan pending")
    service.sent.clear()

    _save(application, allow_web_wallpapers=False)
    _reaches_the_service_while_held(application, service, scans)
    _and_after_the_scan(service, scans)


@previews_param
@scan_param
def test_web_off_in_the_same_save_as_a_folder_change(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    previews: bool,
    fails: bool,
) -> None:
    """Shape 2: the save's own scan is pending before or after the publication: either way."""
    application = profiles().application
    service = _web_on(application, previews, monkeypatch)
    scans = HeldScans(monkeypatch, fails=fails)
    extra = tmp_path / "extra"
    extra.mkdir()

    _save(application, roots=(*application.settings.roots, extra), allow_web_wallpapers=False)
    _reaches_the_service_while_held(application, service, scans)
    assert application._runtime_library_scan_pending, "the folders' scan is what is held"
    _and_after_the_scan(service, scans)


def test_a_publication_during_the_previews_rescan_survives_its_failure(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3-1's shape, upgraded profile: web off moves the previews option, so a rescan runs.

    A publication made while it runs (any edit) waits for it. When it fails, the library
    that publication was compiled from is still current for web off (`Settings.covers`):
    it is no refusal "for older sources", and nothing is said to be unpublished.
    """
    application = profiles().application
    service = _web_on(application, True, monkeypatch)
    scans = HeldScans(monkeypatch, fails=True)
    _save(application, allow_web_wallpapers=False)
    _reaches_the_service_while_held(application, service, scans)
    assert application._runtime_library_scan_pending, "the previews rescan is held"
    application.runtime_config_changed()
    _pump(1.0)
    _and_after_the_scan(service, scans)
    assert application.publication_problem == "", application.publication_problem


@previews_param
def test_a_publication_made_while_web_off_compiles_still_sends_it(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    previews: bool,
) -> None:
    """The review's "later" probe: any publication while web off's own compile runs.

    It supersedes the web-off request after that one wrote its document, and compiles the
    same document, so nothing changed on its own account: the reload must still go out.
    """
    application = profiles().application
    service = _web_on(application, previews, monkeypatch)
    scans = HeldScans(monkeypatch, fails=False)
    compiling = threading.Event()
    go_on = threading.Event()
    real_compile = application._compile_runtime_request
    compiled: list[int] = []

    def slow(request: Any) -> bool:
        compiled.append(request.generation)
        changed = bool(real_compile(request))
        if len(compiled) == 1:
            compiling.set()
            go_on.wait(30)
        return changed

    monkeypatch.setattr(application, "_compile_runtime_request", slow)
    _save(application, allow_web_wallpapers=False)
    spin_until(compiling.is_set, AT_ONCE, what="web off's own compile")
    if not previews:
        application.refresh_library()  # a scan for it to be held by, as with previews on
    spin_until(lambda: application._runtime_library_scan_pending, 10, what="a scan pending")
    application.runtime_config_changed()  # any later publication request
    go_on.set()
    _reaches_the_service_while_held(application, service, scans, settles=False)
    assert len(compiled) >= 2, compiled
    _and_after_the_scan(service, scans)


@previews_param
@pytest.mark.parametrize("same_save", [False, True], ids=["earlier-save", "same-save"])
def test_web_off_after_dropping_a_folder_a_playlist_names(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    previews: bool,
    same_save: bool,
) -> None:
    """The library held may hold a wallpaper from a folder the settings no longer name.

    A playlist names it. Web off is published from that library all the same (it
    names no web page), and the scan for the new folders publishes after it.
    """
    profile = profiles()
    application = profile.application
    root = application.settings.roots[0]
    second = tmp_path / "second"
    second.mkdir()
    (second / "aside.png").write_bytes(b"media")
    aside = MediaItem(second / "aside.png", Kind.STILL, 5, 1)
    application._settings = replace(application.settings, roots=(root, second))
    service = _web_on(application, previews, monkeypatch)
    application.session.adopt_library(
        Library((root, second), (profile.dawn, profile.dusk, profile.rain, aside))
    )
    mine = application.session.playlists.create("Mine")
    application.session.playlists.add(mine.id, profile.dawn.path)
    application.session.playlists.add(mine.id, aside.path)
    scans = HeldScans(monkeypatch, fails=False)
    if same_save:
        _save(application, roots=(root,), allow_web_wallpapers=False)
    else:
        _save(application, roots=(root,))
        spin_until(lambda: application._runtime_library_scan_pending, 10, what="the scan")
        service.sent.clear()
        _save(application, allow_web_wallpapers=False)
    _reaches_the_service_while_held(application, service, scans)
    assert "stop" not in service.sent, "published, not guarded"
    assert application.publication_problem == "", application.publication_problem
    assert "aside.png" in service.loaded, "published from the library held, older folders and all"
    _and_after_the_scan(service, scans)
    assert "aside.png" not in service.loaded, "the scan for the new folders published after it"


@previews_param
def test_a_playback_gesture_waiting_for_a_scan_lets_web_off_out_first(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    previews: bool,
) -> None:
    """A gesture holds the lane until the folders' scan ends; web off does not wait for it."""
    application = profiles().application
    service = _web_on(application, previews, monkeypatch)
    scans = HeldScans(monkeypatch, fails=False)
    extra = tmp_path / "extra"
    extra.mkdir()
    _save(application, roots=(*application.settings.roots, extra))
    spin_until(lambda: application._runtime_library_scan_pending, 10, what="the scan pending")
    assert application.runtime_action_async("next")
    _pump(0.5)  # the gesture's job now waits on the lane for the scan
    service.sent.clear()

    _save(application, allow_web_wallpapers=False)
    # The gesture's job delivers it; the compile job queued behind it then finds nothing
    # new and, with the scan still pending, settles with the scan's publication.
    _reaches_the_service_while_held(application, service, scans, settles=False)
    _and_after_the_scan(service, scans)


@previews_param
def test_web_off_during_a_removal_runs_the_guard_at_once(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    previews: bool,
) -> None:
    """A removal holds every publication until it settles; web off can't wait: the guard runs."""
    application = profiles().application
    service = _web_on(application, previews, monkeypatch)
    application._invalidate_runtime_for_pending_removal()
    try:
        _save(application, allow_web_wallpapers=False)
        spin_until(lambda: "stop" in service.sent, AT_ONCE, what="the guard")
        _pump(0.5)
        assert not service.allows_web(), service.sent
        assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())
    finally:
        application._restore_runtime_after_uncommitted_removal()


@pytest.mark.parametrize("previews", [False, True], ids=["previews-off", "previews-on"])
@pytest.mark.parametrize("rescan", ["ok", "raises"])
def test_service_publishes_web_off_before_its_rescan(
    services: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    previews: bool,
    rescan: str,
) -> None:
    """Shape 3: ``--service`` rescans synchronously; with previews on that ran first, and raised."""
    service = services(start=True)
    application = service.application
    recorder = _web_on(application, previews, monkeypatch)
    if rescan == "raises":
        service.fail_the_next_rescan()
    try:
        if rescan == "raises" and previews:
            # Web off moves the previews option, a library source: the rescan runs, after.
            with pytest.raises(OSError, match="the rescan failed"):
                application.update_settings(allow_web_wallpapers=False)
        else:
            application.update_settings(allow_web_wallpapers=False)
    finally:
        service.failing[0] = False
    assert "reload" in recorder.sent
    assert config.load_strict().allow_web_wallpapers is False
    assert not recorder.allows_web()
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())


@pytest.mark.parametrize("previews", [False, True], ids=["previews-off", "previews-on"])
def test_service_publishes_web_off_before_the_rescan_of_a_dropped_folder(
    services: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    previews: bool,
) -> None:
    """``--service``, one save: drop a folder a playlist names, and turn web off."""
    service = services(start=True)
    application = service.application
    recorder = _web_on(application, previews, monkeypatch)
    mine = application.session.playlists.create("Mine")
    application.session.playlists.add(mine.id, service.noon)
    application.session.playlists.add(mine.id, service.dusk)
    loaded: list[str] = []
    reload = recorder.send

    def send(verb: str, *arguments: object, **keywords: object) -> Response:
        answer = reload(verb, *arguments, **keywords)
        if verb == "reload":
            loaded.append(recorder.loaded)
        return answer

    monkeypatch.setattr(client, "send_runtime", send)
    application.update_settings(roots=(service.root,), allow_web_wallpapers=False)
    assert len(loaded) >= 2, "web off first, then the rescan's publication"
    assert "dusk.png" in loaded[0], "published from the library held, before the rescan"
    assert "dusk.png" not in loaded[-1]
    assert not any(runtime_config.allows_web_wallpapers(text=text) for text in loaded)
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())


def test_a_scan_begun_after_web_off_in_an_upgraded_profile_is_not_a_barrier(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The barrier asks `Settings.covers`, not equal sources: previews don't count with web off."""
    application = profiles().application
    _web_on(application, True, monkeypatch)
    upgraded = application._accepted_library_sources
    assert upgraded is not None and upgraded[2], "resolved with previews on"
    application._settings = replace(application._settings, allow_web_wallpapers=False)
    assert application._settings.library_sources != upgraded
    application._begin_runtime_library_scan(7)
    try:
        assert application._runtime_library_scan_hold_generation == 0
        assert not application._runtime_library_scan_held
    finally:
        application._finish_runtime_library_scan(7)
