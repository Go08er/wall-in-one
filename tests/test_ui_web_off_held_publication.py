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

import contextlib
import json
import socket
import tempfile
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
from wall_in_one import session as session_module  # noqa: E402
from wall_in_one.control import client, server  # noqa: E402
from wall_in_one.control.protocol import Request, Response  # noqa: E402
from wall_in_one.library import scan as scan_module  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.ui import app as app_module  # noqa: E402
from wall_in_one.ui.app import (  # noqa: E402
    _Commands,
    _RuntimeInventoryInvalidError,
    _RuntimeLibraryNotReadyError,
)

#: How long the service gets to hear of it while the scan is held.
AT_ONCE = 15.0
#: The client's own exchange, before any fixture puts a recorder in its place.
REAL_SEND_RUNTIME = client.send_runtime


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
        # What a reload loads is in place before the verb is seen: the tests read `sent`
        # on GTK while the lane sends, and judge `loaded` once "reload" is in it.
        if verb == "reload":
            self.loaded = paths.runtime_config_path().read_text(encoding="utf-8")
        self.sent.append(verb)
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


def _owed_clears(application: Any) -> None:
    """Nothing is owed on the lane once web off has landed or been guarded (`_publish_web_off`)."""
    spin_until(
        lambda: application._runtime_web_off_owed == 0, AT_ONCE, what="web off no longer owed"
    )


def _and_after_the_scan(application: Any, service: Service, scans: HeldScans) -> None:
    scans.release()
    _pump(4.0)
    assert application._runtime_web_off_owed == 0
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
    _and_after_the_scan(application, service, scans)


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
    _and_after_the_scan(application, service, scans)


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
    _and_after_the_scan(application, service, scans)
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
    _and_after_the_scan(application, service, scans)


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
    _and_after_the_scan(application, service, scans)
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
    _and_after_the_scan(application, service, scans)


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
        assert application._runtime_web_off_owed == 0
    finally:
        application._restore_runtime_after_uncommitted_removal()


#: How long the removal below takes, holding what a removal holds.
SLOW_REMOVAL = 3.0


@pytest.mark.parametrize("by", ["window", "ctl"])
@pytest.mark.parametrize("gesture", [False, True], ids=["no-gesture", "a-gesture-waits"])
def test_web_off_saved_just_before_a_removal_reaches_the_service_before_the_removal(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    gesture: bool,
    by: str,
) -> None:
    """f6 L2: a real removal, asked for just after web off, takes its time.

    Its run holds the runtime lane's lock and the compiler lock throughout, and web off's
    publication, or the guard it runs, needs both. On 86840ed, with a gesture waiting for a
    scan (which lets the lock go while it waits), the removal took them first, and the
    service heard nothing until the removal was over; without one, now and then. Now the
    removal lets web off out first: the service hears it at once, nothing left allows web
    wallpapers, the removal still runs, holding the lock, and nothing is owed after.

    The window's removal is queued behind the save in the same tick. ``ctl remove`` is
    refused while a save is being written, so it is sent the moment the save is done.
    """
    profile = profiles()
    application = profile.application
    service = _web_on(application, False, monkeypatch)
    scans = HeldScans(monkeypatch, fails=False)
    real_run = session_module.RemovalPlan.run
    held: list[bool] = []
    ran: list[float] = []

    def slow_run(plan: Any) -> Any:
        held.append(application._runtime_removal_lock.locked())
        ran.append(time.monotonic())
        time.sleep(SLOW_REMOVAL)
        try:
            return real_run(plan)
        finally:
            ran.append(time.monotonic())

    monkeypatch.setattr(session_module.RemovalPlan, "run", slow_run)
    if gesture:
        extra = tmp_path / "extra"
        extra.mkdir()
        _save(application, roots=(*application.settings.roots, extra))
        spin_until(lambda: application._runtime_library_scan_pending, 10, what="the scan pending")
        assert application.runtime_action_async("next")
        _pump(0.5)  # the gesture's job now waits on the lane for the scan
    reached: list[float] = []
    heard = service.send

    def send(verb: str, *arguments: object, **keywords: object) -> Response:
        answer = heard(verb, *arguments, **keywords)
        if verb in {"reload", "stop", "quit"} and not reached:
            reached.append(time.monotonic())
        return answer

    monkeypatch.setattr(client, "send_runtime", send)
    saved: list[object] = []
    removed: list[object] = []

    verbs = server.build_verb_table(_Commands(application))

    def by_ctl() -> bool:
        server.dispatch(verbs, Request("remove", str(profile.rain.path)).encode(), removed.append)
        return GLib.SOURCE_REMOVE

    def landed(outcome: object) -> None:
        saved.append(outcome)
        if by == "ctl":
            GLib.idle_add(by_ctl)

    assert application.update_settings_async(
        on_success=landed, on_error=saved.append, allow_web_wallpapers=False
    )
    if by == "window":
        assert application.remove_item_async(profile.rain, trash=True, finish=removed.append)
    spin_until(lambda: bool(removed), SLOW_REMOVAL + AT_ONCE, what="the removal")
    assert held == [True], "the removal's run holds the runtime lane's lock"
    assert reached, "web off never reached the service"
    assert reached[0] < ran[-1] - SLOW_REMOVAL / 2, (
        f"web off reached the service {reached[0] - ran[0]:.2f}s into a {SLOW_REMOVAL}s removal"
    )
    done = removed[0]
    assert done.ok if isinstance(done, Response) else getattr(done, "committed", False), done
    scans.release()
    _owed_clears(application)
    spin_until(lambda: not application._runtime_publication_held, AT_ONCE, what="the publications")
    assert saved and not any(isinstance(outcome, str) for outcome in saved), saved
    assert config.load_strict().allow_web_wallpapers is False
    assert not service.allows_web(), service.sent
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())
    assert application._runtime_web_off_owed == 0


@pytest.mark.parametrize("hold", [0.5, 3.0])
def test_web_off_owed_while_a_finished_jobs_completion_waits_still_gets_a_job(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    hold: float,
) -> None:
    """f7 G1: a job J done on the lane, its completion not yet run on GTK, so the lane counts
    as busy. Web off saved then queued no job of its own, the removal's prepare in the same
    tick withdrew the request, and J's completion found none to follow: nothing published or
    guarded web off, and the removal waited out its 10 s for it (on c41700b9; on 86840ed the
    removal didn't wait). Owed web off now gets a job of its own: it reaches the service as
    soon as J's completion lands, and the removal goes on then."""
    profile = profiles()
    application = profile.application
    service = _web_on(application, False, monkeypatch)
    real_post = app_module.Application._post_runtime_completion
    held: list[tuple[Any, Any]] = []

    def hold_the_first(future: Any, callback: Any) -> None:
        if held:
            real_post(future, callback)
        else:
            held.append((future, callback))

    monkeypatch.setattr(
        app_module.Application, "_post_runtime_completion", staticmethod(hold_the_first)
    )
    real_run = session_module.RemovalPlan.run
    ran: list[float] = []

    def run(plan: Any) -> Any:
        ran.append(time.monotonic())
        return real_run(plan)

    monkeypatch.setattr(session_module.RemovalPlan, "run", run)
    assert application._publish_runtime_for_context()  # J
    spin_until(lambda: bool(held) and held[0][0].done(), AT_ONCE, what="J done on the lane")
    assert application._runtime_compile_pending
    # What the service held before J: a document that allows web wallpapers.
    service.loaded = ALLOWING
    paths.runtime_config_path().write_text(ALLOWING, encoding="utf-8")
    service.sent.clear()
    removed: list[object] = []
    started = time.monotonic()
    assert application.update_settings_async(allow_web_wallpapers=False)
    assert application.remove_item_async(profile.rain, trash=True, finish=removed.append)
    _pump(hold)
    future, callback = held[0]
    GLib.idle_add(callback, future, priority=GLib.PRIORITY_DEFAULT)  # type: ignore[call-arg]
    spin_until(lambda: bool(removed), AT_ONCE, what="the removal")
    heard = next(
        (index for index, verb in enumerate(service.sent) if verb in {"reload", "stop", "quit"}),
        None,
    )
    assert heard is not None, service.sent
    assert ran and ran[0] - started < hold + 2.0, f"the removal waited {ran[0] - started:.1f}s"
    _owed_clears(application)
    spin_until(lambda: not application._runtime_publication_held, AT_ONCE, what="the publications")
    assert not service.allows_web(), service.sent
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())


def test_a_lane_job_that_raises_with_web_off_owed_runs_the_guard_and_owes_nothing(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """f7 G9: a job that raised left web off owed, unguarded, and every removal until the
    next publication waited out its bound for it. It is a failed publication now: the guard
    runs, nothing is owed, and the lane doesn't go round again for it."""
    application = profiles().application
    service = _web_on(application, False, monkeypatch)
    real = application._compile_runtime_until_current_locked
    jobs: list[object] = []

    def raising_once(**keywords: Any) -> Any:
        jobs.append(keywords)
        if len(jobs) == 1:
            raise RuntimeError("injected: the job failed")
        return real(**keywords)

    monkeypatch.setattr(application, "_compile_runtime_until_current_locked", raising_once)
    _save(application, allow_web_wallpapers=False)
    spin_until(lambda: "stop" in service.sent, AT_ONCE, what="the guard")
    _owed_clears(application)
    spin_until(lambda: not application._runtime_publication_held, AT_ONCE, what="the publication")
    _pump(1.0)
    assert len(jobs) <= 2, f"{len(jobs)} jobs"
    assert not service.allows_web(), service.sent
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())


def _counting_jobs(application: Any, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Each runtime lane job, as it starts."""
    real = application._compile_runtime_until_current_locked
    jobs: list[object] = []

    def counted(**keywords: Any) -> Any:
        jobs.append(keywords)
        return real(**keywords)

    monkeypatch.setattr(application, "_compile_runtime_until_current_locked", counted)
    return jobs


@pytest.mark.parametrize("raises", ["once", "always"])
def test_a_guard_that_raises_with_a_failing_compile_doesnt_send_the_lane_round(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    raises: str,
) -> None:
    """f7 H2: with the compile failing and the guard raising, the job's future raised, the
    completion numbered it 0, and the lane went round again for ever (hundreds of jobs a
    second, on 0b96417c). A guard that raises is a failed publication now, and a job that
    ends owing what it began owing has the guard run once more, and owes nothing."""
    application = profiles().application
    service = _web_on(application, False, monkeypatch)

    def refused(_request: object) -> bool:
        raise runtime_config.RuntimeConfigError("injected: the profile is read-only")

    real_guard = application._web_off_guard
    guards: list[object] = []

    def guard() -> str:
        guards.append(None)
        if raises == "always" or len(guards) == 1:
            raise RuntimeError("injected: the guard raised")
        return str(real_guard())

    monkeypatch.setattr(application, "_compile_runtime_request", refused)
    monkeypatch.setattr(application, "_web_off_guard", guard)
    jobs = _counting_jobs(application, monkeypatch)
    _save(application, allow_web_wallpapers=False)
    _owed_clears(application)
    _pump(2.0)
    assert len(jobs) <= 3, f"{len(jobs)} jobs"
    assert 2 <= len(guards) <= 4, f"{len(guards)} guards"
    if raises == "once":
        assert not service.allows_web(), service.sent
        assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())


def test_a_guard_that_raises_after_a_refused_reload_leaves_the_refusals_own_words(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """f9 F4: H2 made a raising guard its publication's error, and for a refused reload that
    error was all that was said: "the web-off guard failed: …", not why the service refused
    (on cab9225a). The failure is said beside the refusal's words now."""
    application = profiles().application
    service = _web_on(application, False, monkeypatch)
    said: list[str] = []
    report = application.window_report

    def reported(message: str) -> None:
        said.append(message)
        report(message)

    monkeypatch.setattr(application, "window_report", reported)

    def refusing(verb: str, *arguments: object, **keywords: object) -> Response:
        if verb == "reload":
            service.sent.append(verb)
            return Response.failure("refused: a bound this service enforces")
        return service.send(verb, *arguments, **keywords)

    def guard() -> str:
        raise RuntimeError("injected: the guard raised")

    monkeypatch.setattr(client, "send_runtime", refusing)
    monkeypatch.setattr(application, "_web_off_guard", guard)
    _save(application, allow_web_wallpapers=False)
    _owed_clears(application)
    _pump(1.0)

    problem = application.publication_problem
    assert problem.startswith("Runtime rejected the new configuration: refused: a bound"), problem
    assert "the web-off guard failed: injected: the guard raised" in problem, problem
    assert problem in said, said


def test_web_off_owed_under_a_number_no_request_carries_is_settled_in_one_job(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """f7 H2: what web off owed was cleared only by a job numbered at or above it, and a
    number no request carries sent the lane round for ever (on 0b96417c). Not reachable
    through `_publish_web_off`; now one job settles it, with the guard."""
    application = profiles().application
    service = _web_on(application, False, monkeypatch)
    _save(application, allow_web_wallpapers=False)
    _owed_clears(application)
    spin_until(lambda: not application._runtime_publication_held, AT_ONCE, what="web off")
    jobs = _counting_jobs(application, monkeypatch)
    real_guard = application._web_off_guard
    guards: list[object] = []

    def guard() -> str:
        guards.append(None)
        return str(real_guard())

    monkeypatch.setattr(application, "_web_off_guard", guard)
    with application._runtime_config_lock:
        application._runtime_authoring_request = None
        application._runtime_web_off_owed = application._runtime_authoring_generation + 10
    application._ensure_runtime_compile_queued()
    _owed_clears(application)
    _pump(2.0)
    assert len(jobs) <= 2, f"{len(jobs)} jobs"
    assert guards, "the guard didn't run"
    assert not service.allows_web(), service.sent
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())


class _PrivatePeer:
    """A service of the test's own on a private socket: it answers ``status`` as a schema-8
    service holding a document that allows web wallpapers, and holds ``stop`` until it is
    told to answer, noting when the app closes that connection."""

    def __init__(self, address: Path) -> None:
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(address))
        self.listener.listen()
        self.listener.settimeout(0.05)
        self.at_stop, self.answer, self.closed = (threading.Event() for _ in range(3))
        self.ending = threading.Event()
        self.thread = threading.Thread(target=self._serve, name="test-private-peer")
        self.thread.start()

    def _serve(self) -> None:
        while not self.ending.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(0.05)
                line = b""
                while not line.endswith(b"\n") and not self.ending.is_set():
                    try:
                        part = connection.recv(4096)
                    except TimeoutError:
                        continue
                    if not part:
                        break
                    line += part
                if not line.endswith(b"\n"):
                    continue
                verb = Request.decode(line).verb
                if verb == "stop":
                    self.at_stop.set()
                    while not self.answer.is_set() and not self.ending.is_set():
                        try:
                            if connection.recv(4096) == b"":
                                self.closed.set()
                                break
                        except TimeoutError:
                            pass
                        except OSError:
                            self.closed.set()
                            break
                status = {
                    "status_version": 2,
                    "supported_config_schemas": READS_EIGHT,
                    "allow_web_wallpapers": True,
                    "playback_state": "playing",
                }
                reply = Response.success(json.dumps(status) if verb == "status" else "")
                with contextlib.suppress(OSError):
                    connection.sendall(reply.encode())

    def close(self) -> None:
        self.answer.set()
        self.ending.set()
        self.listener.close()
        self.thread.join(5)
        assert not self.thread.is_alive()


@pytest.mark.parametrize("asking", ["a-window-request", "the-web-off-guard"])
def test_the_final_shutdown_closes_a_guards_exchange_in_flight(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    asking: str,
) -> None:
    """R42-26 (Codex's private-socket shape): the final shutdown cancels the application's
    requests (`_runtime_cancellation`), which closes a window's; the guard's own exchanges
    weren't registered, so one waiting on ``stop`` stayed open until its deadline (45 s; on
    19e7ac7b). It is closed now, the guard ends at once, and what is on disk plays no page."""
    application = profiles().application
    config.save(config.Settings(roots=application.settings.roots, allow_web_wallpapers=False))
    paths.runtime_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_config_path().write_text(ALLOWING, encoding="utf-8")
    monkeypatch.setattr(client, "send_runtime", REAL_SEND_RUNTIME)
    monkeypatch.setattr(client, "RUNTIME_ACTION_TIMEOUT", 20.0)  # the wait would show
    folder = tempfile.TemporaryDirectory(prefix="r42-26-")  # a socket's path is short
    address = Path(folder.name) / "peer.sock"
    monkeypatch.setattr(paths, "runtime_socket_path", lambda: address)
    peer = _PrivatePeer(address)
    work = (
        application._web_off_guard
        if asking == "the-web-off-guard"
        else lambda: application._send_gui_runtime("stop")
    )
    pending = application._runtime_pool().submit(work)
    try:
        spin_until(peer.at_stop.is_set, 10, what="the peer to be asked to stop")
        # `do_shutdown`'s last steps for the lane, past any settle wait it allowed.
        with application._runtime_config_lock:
            application._runtime_shutdown = True
        application._runtime_cancellation.cancel()
        spin_until(pending.done, 10, what="the exchange to end")
        assert peer.closed.wait(5), "the app's end of the exchange wasn't closed"
    finally:
        peer.answer.set()
        with contextlib.suppress(client.ControlError):
            pending.result(timeout=10)
        peer.close()
        folder.cleanup()
        with application._runtime_config_lock:
            application._runtime_shutdown = False
    if asking == "the-web-off-guard":
        assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())


class _SlowStop(Service):
    """A service whose ``stop`` takes a while, so a newer request lands while the guard runs."""

    def send(self, verb: str, *arguments: object, **keywords: object) -> Response:
        if verb == "stop":
            time.sleep(0.8)
        return super().send(verb, *arguments, **keywords)


def test_what_the_guard_did_goes_unsaid_when_a_newer_publication_gets_through(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """f9 F1 (Sonnet's real-lane probe): web off's compile fails once, the guard stops the page
    (slowly), and a newer request lands meanwhile, so the guard's publication is not current and
    goes unsaid. The newer one compiles and its reload goes through. Its success took the guard's
    words for an older one's leftovers and said them, and the banner kept them under "Publication
    failed", though the service plays what was just published (on cab9225a). A success as new as
    the guard's publication takes them away unsaid."""
    application = profiles().application
    _web_on(application, False, monkeypatch)
    service = _SlowStop()
    monkeypatch.setattr(client, "send_runtime", service.send)
    said: list[str] = []
    report = application.window_report

    def reported(message: str) -> None:
        said.append(message)
        report(message)

    monkeypatch.setattr(application, "window_report", reported)
    compile_request = application._compile_runtime_request
    compiles: list[object] = []

    def fails_once(request: Any) -> bool:
        compiles.append(request)
        if len(compiles) == 1:
            raise runtime_config.RuntimeConfigError("injected: a store file can't be read, once")
        return bool(compile_request(request))

    monkeypatch.setattr(application, "_compile_runtime_request", fails_once)
    done: list[object] = []
    assert application.update_settings_async(
        on_success=done.append, on_error=done.append, allow_web_wallpapers=False
    )
    spin_until(lambda: "stop" in service.sent, 20, what="the guard to stop the page")
    application._publish_runtime_async()  # another change publishes while it does
    spin_until(lambda: bool(done) and not application._runtime_compile_pending, 30, what="both")
    _pump(1.0)

    assert service.sent.count("reload") >= 2 and len(compiles) >= 2, (service.sent, compiles)
    assert not service.allows_web()
    assert application.publication_problem == ""
    assert not any("Allow web wallpapers is off" in message for message in said), said


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
    assert application._runtime_web_off_owed == 0


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
    assert application._runtime_web_off_owed == 0


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


# -- the owed flag's lifecycle (review f5, N1) ----------------------------------------------------


def _hold_the_first_compile(application: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """The first compile on the lane waits for `go_on`; returns (compiling, go_on)."""
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
    return compiling, go_on


@previews_param
def test_web_off_then_quickly_on_owes_nothing_while_the_compile_is_held(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    previews: bool,
) -> None:
    """Web on ends what web off owed at once, not when its compile ends (`_adopt_settings`)."""
    application = profiles().application
    _web_on(application, previews, monkeypatch)
    compiling, go_on = _hold_the_first_compile(application, monkeypatch)
    try:
        _save(application, allow_web_wallpapers=False)
        spin_until(compiling.is_set, AT_ONCE, what="web off's own compile")
        assert application._runtime_web_off_owed != 0, "owed while its compile is held"
        _save(application, allow_web_wallpapers=True)
        assert application._runtime_web_off_owed == 0, "web on: nothing is owed"
    finally:
        go_on.set()
    spin_until(lambda: not application._runtime_publication_held, AT_ONCE, what="the publications")
    assert application._runtime_web_off_owed == 0
    assert config.load_strict().allow_web_wallpapers is True


@previews_param
def test_web_on_off_on_owes_only_between_the_off_and_the_next_on(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    previews: bool,
) -> None:
    application = profiles().application
    service = _web_on(application, previews, monkeypatch)
    compiling, go_on = _hold_the_first_compile(application, monkeypatch)
    try:
        _save(application, allow_web_wallpapers=False)
        spin_until(compiling.is_set, AT_ONCE, what="the first compile")
        assert application._runtime_web_off_owed != 0
        _save(application, allow_web_wallpapers=True)
        assert application._runtime_web_off_owed == 0
        _save(application, allow_web_wallpapers=False)
        assert application._runtime_web_off_owed != 0, "the second off owes again"
    finally:
        go_on.set()
    _owed_clears(application)
    spin_until(lambda: not application._runtime_publication_held, AT_ONCE, what="the publications")
    assert config.load_strict().allow_web_wallpapers is False
    assert not service.allows_web()
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())
    _save(application, allow_web_wallpapers=True)
    assert application._runtime_web_off_owed == 0


@previews_param
def test_a_failing_compile_while_web_off_is_owed_runs_the_guard_and_clears_it(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    previews: bool,
) -> None:
    application = profiles().application
    service = _web_on(application, previews, monkeypatch)

    def failing(_request: object) -> bool:
        raise runtime_config.RuntimeConfigError("a library folder can't be read")

    monkeypatch.setattr(application, "_compile_runtime_request", failing)
    _save(application, allow_web_wallpapers=False)
    spin_until(lambda: "stop" in service.sent, AT_ONCE, what="the guard")
    _owed_clears(application)
    spin_until(lambda: not application._runtime_publication_held, AT_ONCE, what="the publication")
    assert application._runtime_web_off_owed == 0
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())


@previews_param
def test_a_folder_change_after_web_off_landed_waits_for_its_scan_again(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    previews: bool,
) -> None:
    """With nothing owed, a publication is held by its scan as before: no old library goes out."""
    application = profiles().application
    service = _web_on(application, previews, monkeypatch)
    _save(application, allow_web_wallpapers=False)
    _owed_clears(application)
    spin_until(lambda: not application._runtime_publication_held, AT_ONCE, what="web off settling")
    scans = HeldScans(monkeypatch, fails=False)
    extra = tmp_path / "extra"
    extra.mkdir()
    spin_until(lambda: not application._runtime_library_scan_pending, 10, what="no scan")
    service.sent.clear()
    _save(application, roots=(*application.settings.roots, extra))
    spin_until(lambda: application._runtime_library_scan_pending, 10, what="the scan pending")
    _pump(2.0)
    assert application._runtime_web_off_owed == 0
    assert "reload" not in service.sent, "the old library went out while the scan was held"
    scans.release()
    spin_until(lambda: not application._runtime_publication_held, AT_ONCE, what="the publication")
    assert application._runtime_web_off_owed == 0


# -- a gesture waiting after its compile was refused for the scan (R42-9) -------------------------


@previews_param
@scan_param
def test_a_gesture_waiting_after_a_refused_compile_lets_web_off_out_first(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    previews: bool,
    fails: bool,
) -> None:
    """R42-9: the wait after a compile the scan's library was refused for yields to web off too.

    Ordinary FIFO on the lane: a gesture queued first, then a publication. The gesture's
    compile reads the settings after a preview-policy save began its rescan, so the library
    it holds is refused (`Settings.covers`) and it waits for that scan: on 534a9da it waited
    for nothing else, holding the lane, and web off went out only when the scan ended.
    """
    application = profiles().application
    service = _web_on(application, previews, monkeypatch)
    monkeypatch.setattr(application, "_make_missing_stills", lambda: None)
    scans = HeldScans(monkeypatch, fails=fails)
    lane_held, release_lane = threading.Event(), threading.Event()
    compiling, read_settings = threading.Event(), threading.Event()
    refused, waiting_after_refusal = threading.Event(), threading.Event()
    real_compile = application._compile_runtime_request
    real_wait = application._runtime_library_ready.wait

    def compile_after_the_save(request: Any) -> bool:
        if not compiling.is_set():
            compiling.set()
            assert read_settings.wait(30), "the settings read was never released"
        try:
            return bool(real_compile(request))
        except _RuntimeLibraryNotReadyError:
            refused.set()
            raise

    def wait(timeout: float | None = None) -> bool:
        if refused.is_set():
            waiting_after_refusal.set()
        return bool(real_wait(timeout))

    monkeypatch.setattr(application, "_compile_runtime_request", compile_after_the_save)
    monkeypatch.setattr(application._runtime_library_ready, "wait", wait)

    def hold_the_lane() -> None:
        lane_held.set()
        assert release_lane.wait(30), "the lane was never released"

    blocker = application._runtime_pool().submit(hold_the_lane)
    try:
        spin_until(lane_held.is_set, AT_ONCE, what="the lane held")
        assert application.runtime_action_async("next")
        application.runtime_config_changed()
        release_lane.set()
        spin_until(compiling.is_set, AT_ONCE, what="the gesture's compile")
        # The other preview setting is a library source while web wallpapers are on.
        _save(application, web_wallpaper_previews=not previews)
        spin_until(lambda: application._runtime_library_scan_pending, 10, what="the rescan")
        read_settings.set()
        spin_until(refused.is_set, AT_ONCE, what="the compile refused for the rescan")
        spin_until(waiting_after_refusal.is_set, AT_ONCE, what="the gesture waiting for it")
        service.sent.clear()

        _save(application, allow_web_wallpapers=False)
        _reaches_the_service_while_held(application, service, scans, settles=False)
        _owed_clears(application)
    finally:
        release_lane.set()
        read_settings.set()
        scans.release()
        blocker.result(timeout=15)
    _and_after_the_scan(application, service, scans)
    spin_until(lambda: not application._runtime_action_pending, 20, what="the gesture")
    assert application._runtime_web_off_owed == 0


@previews_param
def test_a_gesture_whose_compile_cant_land_before_the_scan_runs_the_guard_once_web_is_off(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    previews: bool,
) -> None:
    """R42-9: a gesture let go by web off whose compile still waits for the scan is deferred.

    Its request can't be compiled before the scan ends (the inventory is being replaced),
    so with web off owed it neither waits for the scan nor compiles again in a loop: the
    web-off guard runs at once.
    """
    application = profiles().application
    service = _web_on(application, previews, monkeypatch)
    scans = HeldScans(monkeypatch, fails=False)
    extra = tmp_path / "extra"
    extra.mkdir()
    _save(application, roots=(*application.settings.roots, extra))
    spin_until(lambda: application._runtime_library_scan_pending, 10, what="the scan pending")
    compiles: list[int] = []

    def being_replaced(request: Any) -> bool:
        compiles.append(request.generation)
        raise _RuntimeInventoryInvalidError("the library is being scanned again")

    monkeypatch.setattr(application, "_compile_runtime_request", being_replaced)
    assert application.runtime_action_async("next")
    _pump(0.5)  # the gesture's job now waits on the lane for the scan
    service.sent.clear()
    try:
        _save(application, allow_web_wallpapers=False)
        spin_until(lambda: "stop" in service.sent, AT_ONCE, what="the guard")
        spin_until(lambda: not application._runtime_action_pending, AT_ONCE, what="the gesture")
        _pump(1.0)
        assert len(compiles) <= 4, f"compiled {len(compiles)} times while the scan was held"
        assert not scans.gate.is_set()
        _owed_clears(application)
    finally:
        scans.release()
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())


@pytest.mark.parametrize("web_off", [False, True], ids=["folders", "folders-and-web-off"])
def test_a_folder_save_lands_while_a_gesture_waits_for_a_scan(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    web_off: bool,
) -> None:
    """A gesture waiting for a folder scan doesn't hold the lock a folder save takes.

    Found beside R42-9: the lane held `_runtime_removal_lock` while it waited, a save
    that changes the folders waits for that lock, and the scan waited for is installed
    on the authoring lane behind the save. On 534a9da the second save never landed,
    nor did web off with it, until the app quit.
    """
    application = profiles().application
    service = _web_on(application, False, monkeypatch)
    scans = HeldScans(monkeypatch, fails=False)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _save(application, roots=(*application.settings.roots, first))
    spin_until(lambda: application._runtime_library_scan_pending, 10, what="the scan pending")
    assert application.runtime_action_async("next")
    _pump(0.5)  # the gesture's job now waits on the lane for the scan
    service.sent.clear()
    changes: dict[str, Any] = {"roots": (*application.settings.roots, second)}
    if web_off:
        changes["allow_web_wallpapers"] = False
    try:
        _save(application, **changes)
        assert not scans.gate.is_set(), "the save landed while the scan was held"
        assert second in config.load_strict().roots
        if web_off:
            _reaches_the_service_while_held(application, service, scans, settles=False)
            _owed_clears(application)
    finally:
        scans.release()
    spin_until(lambda: not application._runtime_action_pending, 20, what="the gesture")
    spin_until(lambda: not application._runtime_library_scan_pending, 20, what="the scans")
    spin_until(lambda: not application._runtime_publication_held, AT_ONCE, what="publications")
    assert second in application.session.library.roots
    assert application._runtime_web_off_owed == 0
    if web_off:
        assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())
