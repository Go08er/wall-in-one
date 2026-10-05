"""Forget Display, through the real application, when a store fails partway (review X-2).

Each test runs `Application.run` with the new window (`test_ui_next_editing`'s
sandbox: two wallpapers, a fake runtime, no Noctalia). A detached DP-2 has
its own playlist and two schedule rules aimed at it, beside a shared rule,
and a runtime configuration published from that. Faults are native: a store's
private mutation lock, or the store itself, made unreadable with its mode,
restored afterwards. Forget Display then either removes nothing and says so,
removes everything and says so, or says exactly what it removed and what is
still saved, and publishes what it removed.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.test_ui_next_editing import first_scan, reports_of, wallpaper  # noqa: E402
from tests.test_ui_next_slice import finish, run  # noqa: E402
from tests.test_ui_next_window import FakeRuntime, Step, sandboxed_runtime, settled  # noqa: E402
from wall_in_one import paths  # noqa: E402
from wall_in_one.library import displays, playlists, schedules  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRuntime:
    return sandboxed_runtime(monkeypatch, tmp_path)


def lock_of(store: Path) -> Path:
    """A store's private mutation lock (`state_file.mutation_lock`)."""
    return store.absolute().with_name(f".{store.name}.mutation.lock")


@contextlib.contextmanager
def unreadable(path: Path) -> Iterator[None]:
    """``path`` with no permissions, as a native fault; put back afterwards."""
    mode = path.stat().st_mode
    path.chmod(0)
    try:
        yield
    finally:
        path.chmod(mode)


class Profile:
    """DP-2's saved playlist and two rules, a shared rule, and the published baseline.

    Independent displays, so the runtime configuration names DP-2's own
    playlist and rules (mirrored displays share one schedule).
    """

    def __init__(self, application: Application, tmp_path: Path) -> None:
        session = application.session
        made = session.playlists.create(
            "Dock",
            entries=[playlists.Entry(playlists.new_id(), wallpaper(tmp_path, "dawn.png"))],
        )
        self.pid = made.id
        session.displays.assign("DP-2", made.id)
        self.first = session.schedules.add(made.id, connector="DP-2", weekdays=("mon",))
        self.second = session.schedules.add(made.id, connector="DP-2", weekdays=("tue",))
        self.shared = session.schedules.add(made.id, weekdays=("wed",))
        # Displays controlled on their own: only then does the runtime hold a
        # display's own rules and playlist. Colors follow the other display.
        assert application.update_settings_async(
            display_mode="independent", theme_source_connector="DP-1"
        )
        application.schedule_edited()
        self.application = application

    def baseline(self) -> None:
        """What the runtime holds once the setup above is published."""
        self.generation = self.application._runtime_compiled_generation
        self.runtime = paths.runtime_config_path().read_bytes()
        text = self.runtime.decode()
        assert self.first.id in text and self.second.id in text and "DP-2" in text

    @staticmethod
    def rule_ids() -> list[str]:
        return [rule.id for rule in schedules.Store.open().rules]

    @staticmethod
    def assigned() -> dict[str, str]:
        return dict(displays.Store.open().all())

    def published(self) -> bool:
        return self.application._runtime_compiled_generation > self.generation


def forget(window: NextWindow, outcomes: list[bool]) -> None:
    scheduling = window.state.scheduling
    assert scheduling is not None
    plan = scheduling.forget_plan("DP-2")
    assert (plan.rules, plan.refused) == (2, ""), plan
    scheduling.forget_display("DP-2", outcomes.append)


def scenario_with(
    application: Application,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lanes: list[object],
    act: Callable[[NextWindow, Profile, list[bool]], Any],
    check: Callable[[Profile, list[bool], list[str]], None],
) -> Iterator[Step]:
    window = application._window
    assert isinstance(window, NextWindow)
    said = reports_of(window, monkeypatch)
    yield first_scan(application, window)
    profile = Profile(application, tmp_path)
    yield "the baseline's publication", lambda: settled(application)
    profile.baseline()
    outcomes: list[bool] = []
    with contextlib.ExitStack() as faults:
        faults.enter_context(act(window, profile, outcomes))
        yield "Forget Display to settle", lambda: bool(outcomes) and settled(application)
    check(profile, outcomes, said)
    yield from finish(application, window, lanes)


@contextlib.contextmanager
def after_the_first_schedule_commit(lock: Path) -> Iterator[None]:
    """Make ``lock`` unreadable once one schedule transaction has committed."""
    real = schedules.Store._mutate
    commits = [0]
    holder: list[contextlib.ExitStack] = []

    def mutate(store: schedules.Store, change: Any, **keywords: Any) -> Any:
        result = real(store, change, **keywords)
        commits[0] += 1
        if commits[0] == 1:
            stack = contextlib.ExitStack()
            stack.enter_context(unreadable(lock))
            holder.append(stack)
        return result

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(schedules.Store, "_mutate", mutate)
        try:
            yield
        finally:
            for stack in holder:
                stack.close()


def test_a_fault_before_the_first_rule_changes_nothing_and_says_so(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra's step 2: the schedule's lock is unreadable from the start."""
    application = Application(ui="next")
    lanes: list[object] = []

    @contextlib.contextmanager
    def act(window: NextWindow, _profile: Profile, outcomes: list[bool]) -> Iterator[None]:
        with unreadable(lock_of(schedules.state_path())):
            forget(window, outcomes)
            yield

    def check(profile: Profile, outcomes: list[bool], said: list[str]) -> None:
        assert outcomes == [False]
        assert profile.assigned() == {"DP-2": profile.pid}, "the assignment is kept"
        assert profile.rule_ids() == [profile.first.id, profile.second.id, profile.shared.id]
        assert said and "nothing changed" in said[-1]
        assert not profile.published()
        assert paths.runtime_config_path().read_bytes() == profile.runtime

    run(
        application,
        scenario_with(application, tmp_path, monkeypatch, lanes, act, check),
        lanes,
    )


def test_a_fault_after_the_first_rule_never_leaves_part_of_the_schedule(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra's second probe: the lock goes once one schedule transaction committed.

    The rules go in one transaction now, so none is left behind, and the rest
    of Forget Display completes, is said to have, and is published.
    """
    application = Application(ui="next")
    lanes: list[object] = []

    @contextlib.contextmanager
    def act(window: NextWindow, _profile: Profile, outcomes: list[bool]) -> Iterator[None]:
        with after_the_first_schedule_commit(lock_of(schedules.state_path())):
            forget(window, outcomes)
            yield

    def check(profile: Profile, outcomes: list[bool], said: list[str]) -> None:
        assert outcomes == [True], said
        assert profile.rule_ids() == [profile.shared.id], "both of DP-2's rules, together"
        assert profile.assigned() == {}
        assert not any("nothing changed" in message for message in said), said
        assert profile.published()
        text = paths.runtime_config_path().read_text()
        assert profile.first.id not in text and profile.second.id not in text

    run(
        application,
        scenario_with(application, tmp_path, monkeypatch, lanes, act, check),
        lanes,
    )


def test_an_unreadable_schedule_refuses_before_anything_is_written(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra's variation: schedules.json itself can't be read. Never a false success."""
    application = Application(ui="next")
    lanes: list[object] = []

    @contextlib.contextmanager
    def act(window: NextWindow, _profile: Profile, outcomes: list[bool]) -> Iterator[None]:
        with unreadable(schedules.state_path()):
            forget(window, outcomes)
            yield

    def check(profile: Profile, outcomes: list[bool], said: list[str]) -> None:
        assert outcomes == [False]
        assert profile.assigned() == {"DP-2": profile.pid}, "nothing written"
        assert profile.rule_ids() == [profile.first.id, profile.second.id, profile.shared.id]
        assert said and "nothing changed" in said[-1] and "schedules.json" in said[-1], said

    run(
        application,
        scenario_with(application, tmp_path, monkeypatch, lanes, act, check),
        lanes,
    )


def test_a_display_store_failing_after_the_rules_is_reported_exactly_and_published(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one partial outcome left: the rules went, the display's playlist could not."""
    application = Application(ui="next")
    lanes: list[object] = []

    @contextlib.contextmanager
    def act(window: NextWindow, _profile: Profile, outcomes: list[bool]) -> Iterator[None]:
        with after_the_first_schedule_commit(lock_of(displays.state_path())):
            forget(window, outcomes)
            yield

    def check(profile: Profile, outcomes: list[bool], said: list[str]) -> None:
        assert outcomes == [False]
        assert profile.rule_ids() == [profile.shared.id]
        assert profile.assigned() == {"DP-2": profile.pid}
        message = said[-1]
        assert "nothing changed" not in message, message
        assert message.startswith("Forgetting DP-2 was only partly saved"), message
        assert "its 2 schedule rules were removed" in message, message
        assert "its own playlist (“Dock”) is still saved" in message, message
        assert profile.published(), "what was removed reaches the runtime"
        text = paths.runtime_config_path().read_text()
        assert profile.first.id not in text and profile.second.id not in text

    run(
        application,
        scenario_with(application, tmp_path, monkeypatch, lanes, act, check),
        lanes,
    )


def test_permissions_are_native_here() -> None:
    """The faults above rely on file modes, which root ignores; the gates never run as root."""
    assert os.geteuid() != 0
