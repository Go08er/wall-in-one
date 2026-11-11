"""S3-1: turning Allow web wallpapers off reaches the service at once, whatever the scan does.

An upgraded 0.4.1 profile (web on, no previews key) reads the previews sub-option as on, so
turning web off also moves `Settings.preview_stills`, which is a library source. That sent
`_adopt_settings` down the async-scan lane, which skipped the immediate publication: the
page kept playing until a rescan landed, and for good if it failed. The owner contract
since 0.4.0: web off always reaches the service at once; if it can't, the guard stops the
page. A library resolved under the other preview policy is as good as any while web is off
(`Settings.covers`).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_codex030_round1b import profiles  # noqa: E402, F401
from tests.test_ui_web_off_failed_publish import _steam_with_web  # noqa: E402
from tests.test_ui_web_off_lanes import ALLOWING  # noqa: E402
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.library import scan as scan_module  # noqa: E402


def _pump(seconds: float) -> None:
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


@pytest.mark.parametrize("previews", [False, True], ids=["previews-off", "upgraded-previews-on"])
@pytest.mark.parametrize("scan", ["held", "fails"])
def test_web_off_is_published_while_the_scan_is_held_or_failing(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    previews: bool,
    scan: str,
) -> None:
    profile = profiles()
    application = profile.application
    _steam_with_web()
    settings = config.Settings(
        roots=application.settings.roots,
        allow_web_wallpapers=True,
        web_wallpaper_previews=previews,
    )
    config.save(settings)
    application._settings = config.load_strict()
    application._settings_requested = application._settings
    application._accepted_library_sources = application._settings.library_sources
    application.session.update_settings(application._settings, rescan_library=False)
    paths.runtime_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.runtime_config_path().write_text(ALLOWING, encoding="utf-8")
    sent: list[str] = []

    def send(verb: str, *_a: object, **_k: object) -> Response:
        sent.append(verb)
        return Response.success()

    monkeypatch.setattr(client, "send_runtime", send)
    gate = threading.Event()
    real = scan_module.scan

    def held(*args: object, **keywords: object) -> object:
        gate.wait(30)
        if scan == "fails":
            raise OSError("injected: the scan failed")
        return real(*args, **keywords)  # type: ignore[arg-type]

    monkeypatch.setattr(scan_module, "scan", held)
    done: list[object] = []
    assert application.update_settings_async(
        allow_web_wallpapers=False, on_success=done.append, on_error=done.append
    )
    spin_until(lambda: bool(done), 30, what="the save")
    spin_until(lambda: "reload" in sent, 15, what="web off reaching the service")
    # Still held: the scan has not ended, and web is already off for the service.
    assert not gate.is_set()
    assert config.load_strict().allow_web_wallpapers is False
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())

    gate.set()
    _pump(3.0)
    assert not runtime_config.allows_web_wallpapers(paths.runtime_config_path())
    assert "quit" not in sent, "web-off was delivered, so nothing needs the guard's last resort"
    # The library it was published from is current for web off (`Settings.covers`): a
    # rescan that failed leaves nothing unpublished (review-f4 F4-2).
    assert application.publication_problem == "", application.publication_problem


def test_covers_ignores_the_preview_policy_only_while_web_is_off() -> None:
    roots = config.Settings().roots
    scanned = (roots, True, True)
    on = config.Settings(allow_web_wallpapers=True, web_wallpaper_previews=False)
    off = config.Settings(allow_web_wallpapers=False, web_wallpaper_previews=True)
    assert not on.covers(scanned), "web on: a library resolved under the other policy is refused"
    assert off.covers(scanned), "web off: every web page is left out whatever its still was"
    assert not off.covers((roots, False, True)), "the folders and Workshop scanning still count"


def _web_on_application(profiles: Callable[..., Any], previews: bool) -> Any:  # noqa: F811
    application = profiles().application
    settings = config.Settings(
        roots=application.settings.roots,
        allow_web_wallpapers=True,
        web_wallpaper_previews=previews,
    )
    config.save(settings)
    application._settings = config.load_strict()
    application._settings_requested = application._settings
    application._accepted_library_sources = application._settings.library_sources
    return application


def test_moving_the_previews_option_with_web_on_rescans_the_library(
    profiles: Callable[..., Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The setting is resolved by the scan, so the window rescans (S3-N3, mutant killed)."""
    from dataclasses import replace

    application = _web_on_application(profiles, True)
    scans: list[str] = []
    monkeypatch.setattr(application, "refresh_library", lambda *a, **k: scans.append("scan"))
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    previous = application._settings
    application._adopt_settings(previous, replace(previous, web_wallpaper_previews=False))
    assert scans == ["scan"]
    application._adopt_settings(
        application._settings, replace(application._settings, cycle_interval=99)
    )
    assert scans == ["scan"], "an unrelated setting does not"


def test_a_scan_begun_for_a_moved_previews_option_is_a_publication_barrier(
    profiles: Callable[..., Any],  # noqa: F811
) -> None:
    """`_begin_runtime_library_scan` names the previews option in its sources (S3-N3)."""
    from dataclasses import replace

    application = _web_on_application(profiles, False)
    moved = replace(application._settings, web_wallpaper_previews=True)
    application._settings = moved
    application._begin_runtime_library_scan(7)
    assert application._runtime_library_scan_sources == moved.library_sources
    assert application._runtime_library_scan_hold_generation == 7, "a barrier: sources moved"
    application._finish_runtime_library_scan(7)
