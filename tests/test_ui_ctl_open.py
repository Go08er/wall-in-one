"""``ctl open <page>`` presents a window; it never runs the first activation again.

The Noctalia panel's footer links are ``ctl open`` calls, so each one has to be
cheap: an open window is presented and navigated, nothing more, and a window
built after the user closed the last one is shown the palette and library the
process already holds. The palette is resolved, the library walked and the
predecessor probed once per process, by its first window.

These drive the real `Application.run` and the real control socket with the
sandbox, fake runtime and bounded waits of `tests.test_ui_next_window`. The
counters wrap the real entry points and delegate to them, so they observe the
work without changing it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.test_ui_next_window import (  # noqa: E402
    FakeRuntime,
    Step,
    application_lanes,
    control,
    run_application,
    sandboxed_runtime,
    settled,
)
from wall_in_one import legacy_migration  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.library import scan  # noqa: E402
from wall_in_one.theme import source  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402
from wall_in_one.ui.window import MainWindow  # noqa: E402
from wall_in_one.ui.window_services import WindowServices  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRuntime:
    """A sandboxed profile with a two-wallpaper library, a fake runtime, no Noctalia."""
    return sandboxed_runtime(monkeypatch, tmp_path)


def window_now(application: Application) -> WindowServices | None:
    """The application's window, read afresh after an assertion narrowed it to None."""
    return application._window


def count_first_activation_work(
    application: Application, monkeypatch: pytest.MonkeyPatch
) -> Counter[str]:
    """Count, from now on, every entry into the first activation's expensive work.

    The application's own entry points (``reload_palette``, ``refresh_library``)
    and what they lead to off GTK's thread: the live palette resolution, the
    filesystem walk, and the predecessor probe.
    """
    calls: Counter[str] = Counter()

    def counted(name: str, original: Callable[..., object]) -> Callable[..., object]:
        def wrapper(*arguments: object, **keywords: object) -> object:
            calls[name] += 1
            return original(*arguments, **keywords)

        return wrapper

    monkeypatch.setattr(
        application, "reload_palette", counted("palette reload", application.reload_palette)
    )
    monkeypatch.setattr(
        application, "refresh_library", counted("library refresh", application.refresh_library)
    )
    monkeypatch.setattr(source, "resolve", counted("palette resolution", source.resolve))
    monkeypatch.setattr(scan, "scan", counted("library walk", scan.scan))
    monkeypatch.setattr(
        legacy_migration, "probe", counted("migration probe", legacy_migration.probe)
    )
    return calls


def test_repeated_ctl_open_navigates_the_open_window_and_does_nothing_else(
    runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = Application()
    requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctl-client")
    lanes: list[ThreadPoolExecutor] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, MainWindow)
        yield "the first activation", lambda: settled(application) and window._playable == 2
        calls = count_first_activation_work(application, monkeypatch)

        for requested, shown in (
            ("settings", "settings"),
            ("playlists", "playlists"),
            ("displays", "schedules"),
            ("media", "media"),
            ("settings", "settings"),
        ):
            opened = control(requests, "open", requested)
            yield f"ctl open {requested}", opened.done
            assert opened.result() == Response.success(f"opened {shown}")
            assert application._window is window, "the open window is reused"
            assert window._stack.get_visible_child_name() == shown

        yield "every tail to settle", lambda: settled(application)
        assert calls == Counter(), "ctl open re-ran first-activation work"
        assert window._playable == 2
        lanes.extend(application_lanes(application))
        window.close()

    try:
        assert run_application(application, scenario()) == 0
    finally:
        requests.shutdown(wait=True)
        for lane in lanes:
            lane.shutdown(wait=True)
    assert application._window is None


def test_ctl_open_after_the_window_closed_builds_one_from_what_the_process_holds(
    runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = Application()
    requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctl-client")
    lanes: list[ThreadPoolExecutor] = []
    palettes: list[MainWindow] = []
    real_show_palette = MainWindow.show_palette

    def show_palette(window: MainWindow, resolved: source.ResolvedPalette) -> None:
        palettes.append(window)
        real_show_palette(window, resolved)

    def scenario() -> Iterator[Step]:
        first = application._window
        assert isinstance(first, MainWindow)
        yield (
            "the first activation and its palette",
            lambda: (
                settled(application)
                and first._playable == 2
                and application.resolved_palette is not None
            ),
        )
        calls = count_first_activation_work(application, monkeypatch)
        monkeypatch.setattr(MainWindow, "show_palette", show_palette)

        # The process outlives its window, as `--service` does by its own hold.
        application.hold()
        first.close()
        assert application._window is None

        opened = control(requests, "open", "media")
        yield "ctl open media with no window", opened.done
        assert opened.result() == Response.success("opened media")
        second = window_now(application)
        assert isinstance(second, MainWindow) and second is not first
        assert second._stack.get_visible_child_name() == "media"
        assert second._playable == 2, "the new window shows the library the process holds"
        assert palettes == [second], "the new window is shown the current palette"

        again = control(requests, "open", "playlists")
        yield "ctl open playlists on the new window", again.done
        assert again.result() == Response.success("opened playlists")
        assert window_now(application) is second
        assert second._stack.get_visible_child_name() == "playlists"

        yield "every tail to settle", lambda: settled(application)
        assert calls == Counter(), "a new window re-ran first-activation work"
        lanes.extend(application_lanes(application))
        application.release()
        second.close()

    try:
        assert run_application(application, scenario()) == 0
    finally:
        requests.shutdown(wait=True)
        for lane in lanes:
            lane.shutdown(wait=True)
    assert application._window is None


def test_ctl_open_under_the_new_ui_still_answers_not_in_new_ui_and_does_nothing_else(
    runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = Application(ui="next")
    requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctl-client")
    lanes: list[ThreadPoolExecutor] = []

    def not_in_new_ui(answer: Response, page: str) -> None:
        assert not answer.ok and answer.kind == "not-in-new-ui"
        assert f"the {page} page is not available in the new UI yet" in answer.message

    def scenario() -> Iterator[Step]:
        first = application._window
        assert isinstance(first, NextWindow)
        yield (
            "the first activation",
            lambda: settled(application) and first.library_text == "2 wallpapers in the library",
        )
        calls = count_first_activation_work(application, monkeypatch)

        for page in ("settings", "media", "settings"):
            opened = control(requests, "open", page)
            yield f"ctl open {page}", opened.done
            not_in_new_ui(opened.result(), page)
            assert application._window is first

        application.hold()
        first.close()
        assert application._window is None
        opened = control(requests, "open", "playlists")
        yield "ctl open playlists with no window", opened.done
        not_in_new_ui(opened.result(), "playlists")
        second = window_now(application)
        assert isinstance(second, NextWindow) and second is not first
        assert second.library_text == "2 wallpapers in the library"
        assert second.note_text.startswith("Playlists is not in the new interface yet")

        yield "every tail to settle", lambda: settled(application)
        assert calls == Counter(), "ctl open re-ran first-activation work"
        lanes.extend(application_lanes(application))
        application.release()
        second.close()

    try:
        assert run_application(application, scenario()) == 0
    finally:
        requests.shutdown(wait=True)
        for lane in lanes:
            lane.shutdown(wait=True)
    assert application._window is None
