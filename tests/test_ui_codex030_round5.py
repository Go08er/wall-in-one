"""Codex 0.3.0 rounds 5 and 6, in the real application: every way a removal's aftermath fails.

One parametrized matrix over the real application crosses what can go wrong
after a removal commits with what a person can do meanwhile:

- the commit: full, or partial: a real failed cleanup (the favourites store's
  lock held by someone else while the removal cleans up, so its mutation
  times out and the removal journal keeps the intent for the refresh to
  replay);
- the removal's own scan after the commit: fine, failing, held (then fine),
  or held and then failing;
- the fallback refresh, where one runs: fine, failing, or held (then fine);
- writing the runtime configuration, from the first scan that succeeds:
  fine, failing for real (the state directory made read-only: EACCES), or
  refused (the size cap lowered below the document);
- the window at completion: open, or the last one closed through the real
  `_on_close_request` at the failure point, before a held scan is released;
- the action interleaved at the failure point (while a scan is held, or once
  everything has landed): none, Quit, Next, a favourite from the window, a
  favourite and a playlist addition over ctl, a settings change, a second
  removal (of a removable wallpaper, of the one already removed, of one a
  used playlist can't lose, and of one whose dry run can't scan), from the
  window and over ctl.

Every action is crossed with the full commits' scan outcomes and the first
two write modes; partial commits, whose difference is the replay rather than
how actions interleave, with one action of each kind; and the closed window
with every write fault and the actions that bear on completion (none, Quit,
an edit), as is the refused write with the window open.

Only the faults are injected (a scan wrapper, a held lock with a short wait,
chmod, the size cap). For every cell: every runtime document installed at
any time names only files that exist when it is installed; the action
completes or refuses with a reason within a bound; every hold settles; no
worker spins; once the faults are lifted, a refresh recovers and what is
installed names nothing missing; and Quit completes. Codex's T-1, T-2, T-3
(round 5) and U-1, U-2 (round 6) are named cells; each fails on the tip it
was found on.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_dry_run import _file, _missing  # noqa: E402
from tests.test_ui_codex030_round2 import _lane_idle  # noqa: E402
from tests.test_ui_runtime import FakeWindow, _application, _attach, _close  # noqa: E402
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.control import client, server  # noqa: E402
from wall_in_one.control.protocol import Request, Response  # noqa: E402
from wall_in_one.library import favourites, pairings, scan, state_file  # noqa: E402
from wall_in_one.session import RemovalResult  # noqa: E402
from wall_in_one.ui.app import Application, _Commands  # noqa: E402

#: How long anything here may take before it counts as stuck.
BOUND = 15.0
#: An idle stretch: a spinning worker burns about all of it in CPU time.
IDLE = 0.15
#: A size cap no runtime configuration fits under.
TINY_CAP = 64


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


@dataclass(frozen=True, slots=True)
class Faults:
    commit: str  # full | partial
    postscan: str  # ok | fails | held | held-fails
    fallback: str  # ok | fails | held (only where a fallback refresh runs)
    write: str  # ok | eacces | size-cap
    action: str
    window: str = "open"  # open | closed


@dataclass
class Injected:
    """The fault injection, and nothing else."""

    faults: Faults
    forest: Path
    state: Path
    lifted: bool = False
    postscan_used: bool = False
    fallback_used: bool = False
    #: The second removal's own dry-run scan fails while this is set.
    dry_runs_fail: bool = False
    write_failing: bool = False
    original_cap: int = runtime_config.MAX_RUNTIME_CONFIG_BYTES
    reached: dict[str, threading.Event] = field(
        default_factory=lambda: {"postscan": threading.Event(), "fallback": threading.Event()}
    )
    release: threading.Event = field(default_factory=threading.Event)
    #: Held by someone else while the removal cleans up (a partial commit).
    favourites_held: threading.Event = field(default_factory=threading.Event)
    favourites_free: threading.Event = field(default_factory=threading.Event)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_scan = scan.scan
        # Restored at teardown whatever happens.
        monkeypatch.setattr(runtime_config, "MAX_RUNTIME_CONFIG_BYTES", self.original_cap)

        def faulty(*arguments: Any, **keywords: Any) -> Any:
            stage = self._stage()
            if not stage and self.dry_runs_fail and not self.lifted:
                raise OSError("injected dry-run scan fault")
            behaviour = getattr(self.faults, stage) if stage else "ok"
            if behaviour.startswith("held"):
                self.reached[stage].set()
                assert self.release.wait(BOUND * 2), "the test never released a held scan"
            if behaviour.endswith("fails"):
                raise OSError("injected scan fault")
            library = real_scan(*arguments, **keywords)
            if stage and not self.write_failing:
                self._fail_writes()
            return library

        monkeypatch.setattr(scan, "scan", faulty)
        if self.faults.commit == "partial":
            # A real failed cleanup: the favourites lock is held elsewhere while the
            # removal cleans up; its mutation waits briefly, then fails for real.
            real_lock = state_file.mutation_lock

            def briefly(
                target: Path, *, description: str, **keywords: Any
            ) -> AbstractContextManager[None]:
                if description == "favourites" and self.favourites_held.is_set():
                    keywords["timeout"] = 0.1
                return real_lock(target, description=description, **keywords)

            monkeypatch.setattr(state_file, "mutation_lock", briefly)

            def hold() -> None:
                with real_lock(
                    favourites.state_path(), description="favourites", process_gate=False
                ):
                    self.favourites_held.set()
                    self.favourites_free.wait(BOUND * 2)
                self.favourites_held.clear()

            threading.Thread(target=hold, daemon=True).start()
            assert self.favourites_held.wait(5)

    def _fail_writes(self) -> None:
        """From the first post-commit scan that succeeds, the runtime write fails or is refused."""
        if self.faults.write == "eacces":
            os.chmod(self.state, 0o500)
            self.write_failing = True
        elif self.faults.write == "size-cap":
            setattr(runtime_config, "MAX_RUNTIME_CONFIG_BYTES", TINY_CAP)  # noqa: B010
            self.write_failing = True

    def _stage(self) -> str:
        """Which scan this is: the removal's own after its commit, the fallback, or neither."""
        if self.lifted or self.forest.exists():
            return ""
        if threading.current_thread().name.startswith("library-scan"):
            if self.fallback_used:
                return ""
            self.fallback_used = True
            return "fallback"
        if self.postscan_used:
            return ""
        self.postscan_used = True
        return "postscan"

    def free_favourites(self) -> None:
        self.favourites_free.set()

    def lift(self) -> None:
        self.lifted = True
        self.dry_runs_fail = False
        self.release.set()
        self.free_favourites()
        if self.write_failing:
            os.chmod(self.state, 0o700)
            setattr(runtime_config, "MAX_RUNTIME_CONFIG_BYTES", self.original_cap)  # noqa: B010
            self.write_failing = False


class Installed:
    """Every runtime configuration installed, and what it named that wasn't there then."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.documents: list[tuple[str, list[str]]] = []
        install = runtime_config.update

        def installed(settings: config.Settings, session: Any, path: Path | None = None) -> bool:
            changed = install(settings, session, path)
            if changed:
                target = path if path is not None else paths.runtime_config_path()
                document = target.read_text(encoding="utf-8")
                self.documents.append((document, _missing(document)))
            return changed

        monkeypatch.setattr(runtime_config, "update", installed)


@dataclass
class Cell:
    application: Application
    window: FakeWindow
    forest: Path
    keep: Path
    other: Path
    playlist: str
    installed: Installed
    injected: Injected
    quits: list[bool]


@pytest.fixture
def cells(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[Faults], Cell]]:
    made: list[Cell] = []

    def make(faults: Faults) -> Cell:
        root = tmp_path / "lib" / "R"
        forest, keep, other = (
            _file(root / name) for name in ("forest.png", "keep.png", "other.png")
        )
        application = _application(tmp_path, monkeypatch)
        window = FakeWindow()
        _attach(application, window)
        monkeypatch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())
        monkeypatch.setattr(application, "_make_missing_stills", lambda: None)
        quits: list[bool] = []
        monkeypatch.setattr(Application, "quit", lambda _self: quits.append(True))
        settings = replace(application.settings, roots=(root,)).validated()
        config.save(settings)
        application._settings = settings
        application._settings_requested = settings
        session = application.session
        session.update_settings(settings, rescan_library=False)
        session.refresh(mutate_removals=False)
        application._accepted_library_sources = (settings.roots, settings.scan_workshop)
        evening = session.playlists.create("Evening")
        session.playlists.add(evening.id, forest)
        session.playlists.add(evening.id, keep)
        session.schedules.add(evening.id)
        session.favourites.add(forest)  # so its removal has a favourite to clear
        installed = Installed(monkeypatch)
        _publish(application)
        injected = Injected(faults, forest, paths.runtime_config_path().parent)
        injected.install(monkeypatch)
        cell = Cell(
            application, window, forest, keep, other, evening.id, installed, injected, quits
        )
        made.append(cell)
        return cell

    yield make
    for cell in made:
        cell.injected.lift()
        _close(cell.application)


def _settled(application: Application) -> bool:
    return (
        _lane_idle(application)
        and not application._runtime_compile_pending
        and application._library_scan_future is None
        and not application._runtime_action_pending
    )


def _publish(application: Application) -> None:
    assert application._publish_runtime_async()
    spin_until(lambda: _settled(application), BOUND, what="a publication")


def _ctl(application: Application, verb: str, argument: str) -> Callable[[], list[Response]]:
    replies: list[Response] = []
    server.dispatch(
        server.build_verb_table(_Commands(application)),
        Request(verb, argument).encode(),
        replies.append,
    )
    return lambda: replies


def _said(replies: list[Response]) -> str | None:
    if not replies:
        return None
    reply = replies[0]
    return "done" if reply.ok else f"refused: {reply.message}"


#: A second removal through the window, and over ctl: which wallpaper, and whether its own
#: dry run can scan.
SECOND_REMOVALS = {
    "second-removal": ("other", False),
    "retry-removed": ("forest", False),
    "remove-emptying": ("keep", False),
    "second-removal-scan-fails": ("other", True),
}


def _second_removal(cell: Cell, action: str) -> Callable[[], str | None]:
    application = cell.application
    over_ctl = action.startswith("ctl-")
    target, dry_run_fails = SECOND_REMOVALS[action.removeprefix("ctl-")]
    path = {"other": cell.other, "forest": cell.forest, "keep": cell.keep}[target]
    cell.injected.dry_runs_fail = dry_run_fails
    if over_ctl:
        replies_of = _ctl(application, "remove", str(path))
        return lambda: _said(replies_of())
    item = application.session.library.find(path)
    if item is None:
        return lambda: "refused: it is not in the library"
    said_before = len(application.activity_log.entries)
    finished: list[RemovalResult] = []
    accepted = application.remove_item_async(item, trash=True, finish=finished.append)
    if not accepted:
        return lambda: "refused: not accepted"

    def outcome() -> str | None:
        if finished:
            result = finished[0]
            return "removed" if result.committed else f"refused: {result.error_message}"
        said = application.activity_log.entries[said_before:]
        if said and _lane_idle(application):
            return f"refused: {said[-1].text}"
        return None

    return outcome


def _act(cell: Cell, action: str) -> Callable[[], str | None]:
    """Start ``action``; returns how it ended so far (None while it hasn't)."""
    application = cell.application
    if action == "none":
        return lambda: "nothing"
    if action == "quit":
        application.request_quit()
        return lambda: "quit" if cell.quits else None
    if action == "next":
        replies: list[Response] = []
        said_before = len(application.activity_log.entries)
        started = application._start_gui_runtime_call(
            lambda: application._send_gui_runtime("next"),
            on_complete=lambda response, _answered: replies.append(response),
        )
        if not started:
            said = application.activity_log.entries[said_before:]
            return lambda: "refused: " + (said[-1].text if said else "busy")
        return lambda: _said(replies)
    if action == "favourite":
        # As the window's star does (`RealState.toggle_favorite`).
        ended: list[str] = []
        star = application.session.library.find(cell.keep)
        assert star is not None

        def prepare() -> Callable[[], bool]:
            current = application.current_item_for_authoring(star)
            store = application.session.favourites
            return lambda: store.add(current.path)

        def saved(_changed: bool) -> None:
            application.favourites_changed()
            ended.append("saved")

        accepted = application.authoring_action_async(
            lambda: False,
            saved,
            prepare=prepare,
            failure=lambda error: ended.append(f"refused: {error}"),
        )
        if not accepted:
            return lambda: "refused: not accepted"
        return lambda: ended[0] if ended else None
    if action == "ctl-favourite":
        replies_of = _ctl(application, "favourite", str(cell.keep))
        return lambda: _said(replies_of())
    if action == "ctl-playlist-add":
        replies_of = _ctl(application, "playlist-add", f"{cell.playlist} {cell.other}")
        return lambda: _said(replies_of())
    if action == "settings":
        errors: list[str] = []
        wanted = not application.settings.shuffle
        application.update_settings_async(shuffle=wanted, on_error=errors.append)
        return lambda: (
            f"refused: {errors[0]}"
            if errors
            else ("saved" if application.settings.shuffle == wanted else None)
        )
    if action.removeprefix("ctl-") in SECOND_REMOVALS:
        return _second_removal(cell, action)
    raise AssertionError(action)


def _cpu() -> float:
    times = os.times()
    return times.user + times.system


def _point(cell: Cell) -> None:
    """Wait for the failure point: a held scan reached, or everything landed."""
    faults = cell.injected.faults
    if faults.postscan.startswith("held"):
        assert cell.injected.reached["postscan"].wait(BOUND), "the removal's own scan never ran"
        return
    fallback_runs = faults.commit == "partial" or faults.postscan.endswith("fails")
    if fallback_runs and faults.fallback == "held":
        spin_until(cell.injected.reached["fallback"].is_set, BOUND, what="the fallback refresh")
        return
    # Every scan after the commit has finished, however it ended (the holds are checked
    # later: a stranded one is a finding of its own).
    spin_until(
        lambda: (
            _settled(cell.application)
            and not cell.application._runtime_library_scan_pending
            and not cell.application._removal_runtime_invalidated
            and not cell.application._removal_active
        ),
        BOUND,
        what="the scans after the removal",
    )


def _run(cell: Cell) -> None:
    application = cell.application
    faults = cell.injected.faults
    item = application.session.library.find(cell.forest)
    assert item is not None
    removed: list[RemovalResult] = []

    def finished(result: RemovalResult) -> None:
        removed.append(result)
        cell.injected.free_favourites()  # whoever held them is done: the replay can run

    assert application.remove_item_async(item, trash=True, finish=finished)

    _point(cell)
    ended = _act(cell, faults.action)
    if faults.window == "closed":
        application._on_close_request(cell.window)  # type: ignore[arg-type]
        assert application._window is None
    cell.injected.release.set()  # held scans go on now (and succeed or fail as set)

    # The action completes, or refuses with a reason, within the bound.
    spin_until(lambda: ended() is not None, BOUND, what=f"the {faults.action} to end")
    outcome = ended()
    assert outcome is not None
    if outcome.startswith("refused"):
        assert len(outcome) > len("refused: "), "a refusal says why"
    spin_until(lambda: bool(removed), BOUND, what="the removal")
    assert removed[0].committed and not cell.forest.exists()
    if faults.commit == "partial":
        assert removed[0].cleanup_failures, "the partial commit really failed a cleanup"
    cell.injected.dry_runs_fail = False

    # Every hold settles; no worker spins.
    spin_until(
        lambda: (
            _settled(application)
            and not application._removal_convergence_held
            and not application._runtime_publication_held
            and not application._runtime_library_scan_held
        ),
        BOUND,
        what="every hold to settle",
    )
    # Nothing is reported pending once there is no work for it to be waiting on.
    assert not application._runtime_library_scan_pending, "a scan pending with no work behind it"
    spent = _cpu()
    time.sleep(IDLE)
    assert _cpu() - spent < IDLE / 2, "a worker is spinning"

    if faults.action != "quit":
        # Recovery once the faults are lifted: a fresh scan, and what is installed is valid.
        cell.injected.lift()
        application.refresh_library()
        spin_until(
            lambda: _settled(application) and application.session.library.find(cell.forest) is None,
            BOUND,
            what="the recovering scan",
        )
        _publish(application)
        document = paths.runtime_config_path().read_text(encoding="utf-8")
        assert _missing(document) == []
        assert str(cell.forest) not in document
        assert application.publication_problem == ""
        if faults.commit == "partial":
            item_now = pairings.Identity.of(item)
            assert pairings.Store.open().get(item_now) is None
            assert cell.forest not in favourites.Store.open().paths, "the replay cleaned up"
        application.request_quit()

    # Quit completes within the bound, whatever happened.
    spin_until(lambda: bool(cell.quits), BOUND, what="Quit to complete")
    spin_until(lambda: _settled(application), BOUND, what="the last work to land")
    assert not application._runtime_library_scan_pending, "a scan pending with no work behind it"
    assert application._authoring_lifetime_holds == 0
    assert not application._removal_convergence_held

    # The invariant: nothing installed at any time named a file that wasn't there.
    named_missing = [missing for _document, missing in cell.installed.documents if missing]
    assert named_missing == [], "a generation named a file that wasn't there when installed"


# -- the matrix ----------------------------------------------------------------------------

#: (commit, own scan, fallback): only combinations that happen (a fallback runs after a
#: partial commit, or when the removal's own scan fails).
SCANS: tuple[tuple[str, str, str], ...] = (
    ("full", "ok", "-"),
    ("full", "held", "-"),
    ("full", "fails", "ok"),
    ("full", "fails", "fails"),
    ("full", "fails", "held"),
    ("full", "held-fails", "ok"),
    ("full", "held-fails", "fails"),
    ("partial", "ok", "ok"),
    ("partial", "ok", "fails"),
    ("partial", "ok", "held"),
    ("partial", "fails", "ok"),
    ("partial", "fails", "fails"),
    ("partial", "fails", "held"),
)
ACTIONS = (
    "none",
    "quit",
    "next",
    "favourite",
    "ctl-favourite",
    "ctl-playlist-add",
    "settings",
    "second-removal",
    "ctl-second-removal",
    "retry-removed",
    "ctl-retry-removed",
    "remove-emptying",
    "ctl-remove-emptying",
    "second-removal-scan-fails",
)
#: The window closing, crossed with the write faults: the actions that bear on completion.
CLOSING_ACTIONS = ("none", "quit", "ctl-favourite")
#: A partial commit differs from a full one in the replay its refresh does, not in how
#: actions interleave: one of each kind of action.
PARTIAL_ACTIONS = (
    "none",
    "quit",
    "next",
    "ctl-favourite",
    "settings",
    "second-removal",
    "ctl-retry-removed",
    "remove-emptying",
)

#: Codex's reproductions, as cells.
NAMED = {
    # Round 5, found on 9fddd73.
    "T-1-terminal-scan-failure-then-quit": Faults("full", "held-fails", "fails", "ok", "quit"),
    "T-1-runtime-write-failure-then-quit": Faults("full", "ok", "-", "eacces", "quit"),
    "T-2-next-after-the-scans-failed": Faults("full", "fails", "fails", "ok", "next"),
    "T-3-edit-after-the-fallback-failed": Faults("full", "fails", "fails", "ok", "ctl-favourite"),
    "T-3-edit-while-the-fallback-is-held": Faults("full", "fails", "held", "ok", "ctl-favourite"),
    # Round 6, found on 83f38dc.
    "U-1-window-retry-of-the-removed-source": Faults(
        "full", "fails", "fails", "ok", "retry-removed"
    ),
    "U-1-ctl-retry-of-the-removed-source": Faults(
        "full", "fails", "fails", "ok", "ctl-retry-removed"
    ),
    "U-1-window-removal-emptying-a-used-playlist": Faults(
        "full", "fails", "fails", "ok", "remove-emptying"
    ),
    "U-1-ctl-removal-emptying-a-used-playlist": Faults(
        "full", "fails", "fails", "ok", "ctl-remove-emptying"
    ),
    "U-1-second-removal-whose-dry-run-scan-fails": Faults(
        "full", "fails", "fails", "ok", "second-removal-scan-fails"
    ),
    "U-2-last-window-closed-then-eacces": Faults("full", "held", "-", "eacces", "quit", "closed"),
    "U-2-last-window-closed-then-size-cap": Faults(
        "full", "held", "-", "size-cap", "quit", "closed"
    ),
}


def _writes_happen(postscan: str, fallback: str) -> bool:
    """Whether any scan after the commit succeeds, so that anything is ever written."""
    return not (postscan.endswith("fails") and fallback == "fails")


def _grid() -> list[Any]:
    named = set(NAMED.values())
    cells: list[Any] = [pytest.param(faults, id=name) for name, faults in NAMED.items()]

    def add(faults: Faults) -> None:
        if faults in named:
            return
        named.add(faults)
        cells.append(
            pytest.param(
                faults,
                id=(
                    f"{faults.commit}-own:{faults.postscan}-fallback:{faults.fallback}"
                    f"-write:{faults.write}-window:{faults.window}-{faults.action}"
                ),
            )
        )

    for commit, postscan, fallback in SCANS:
        writes = ("ok", "eacces", "size-cap") if _writes_happen(postscan, fallback) else ("ok",)
        actions = ACTIONS if commit == "full" else PARTIAL_ACTIONS
        # Window open: the actions, with the two write modes the fault rounds began with.
        for write in ("ok", "eacces"):
            if write in writes:
                for action in actions:
                    add(Faults(commit, postscan, fallback, write, action))
        # The window closing, crossed with every write fault; and the refused write open.
        for write in writes:
            for action in CLOSING_ACTIONS:
                add(Faults(commit, postscan, fallback, write, action, "closed"))
            if write == "size-cap":
                for action in ("none", "quit"):
                    add(Faults(commit, postscan, fallback, write, action))
    return cells


@pytest.mark.parametrize("faults", _grid())
def test_a_removals_aftermath_is_bounded_and_installs_nothing_missing(
    faults: Faults, cells: Callable[[Faults], Cell]
) -> None:
    _run(cells(faults))


# -- every installer checks the inventory --------------------------------------------------


def _configured(application: Application, root: Path) -> None:
    settings = replace(application.settings, roots=(root,)).validated()
    config.save(settings)
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    application.session.refresh(mutate_removals=False)
    application._accepted_library_sources = (settings.roots, settings.scan_workshop)


def test_the_headless_service_installs_nothing_between_a_removal_and_its_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compatibility service's own removal and edit, over ctl, with no window."""
    from tests.test_control import _downloaded

    root = tmp_path / "lib" / "R"
    root.mkdir(parents=True)
    managed = _downloaded(root)
    keep = _file(root / "keep.png")
    application = _application(tmp_path, monkeypatch)  # no window: the headless paths
    application._service_start = True
    monkeypatch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())
    try:
        _configured(application, root)
        assert application.session.library.find(managed) is not None
        assert application._publish_runtime()
        installed = Installed(monkeypatch)
        commands = _Commands(application)

        removed = commands.remove_wallpaper(str(managed))
        assert isinstance(removed, Response) and removed.ok, removed
        assert not managed.exists()
        # Before the refresh its commit asked for: the edit is saved, nothing is installed.
        starred = commands.add_favourite(str(keep))
        assert isinstance(starred, Response) and starred.ok, starred
        assert installed.documents == []

        spin_until(
            lambda: application.session.library.find(managed) is None and installed.documents,
            BOUND,
            what="the refresh and its publication",
        )
        assert [missing for _document, missing in installed.documents if missing] == []
        assert str(managed) not in paths.runtime_config_path().read_text(encoding="utf-8")
    finally:
        _close(application)


def test_no_installer_installs_from_an_invalid_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authoring (ctl, quick choice and the window publish this way), playback, health and
    the headless path all check the inventory right before they would install."""
    import json

    from wall_in_one import runtime_health
    from wall_in_one.ui.app import _RuntimeHealthRequest

    root = tmp_path / "lib" / "R"
    paper = _file(root / "paper.png")
    application = _application(tmp_path, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    monkeypatch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())
    try:
        _configured(application, root)
        _publish(application)
        installed = Installed(monkeypatch)
        problem = "test: the library is being scanned again"
        application._invalidate_inventory(problem)
        application.session.favourites.add(paper)  # a durable change to publish

        # Authoring publication: refused before the install, and said.
        _publish(application)
        assert installed.documents == []
        assert application.publication_problem == problem

        # A playback gesture: refused at once, with the reason.
        replies: list[Response] = []
        assert application._start_gui_runtime_call(
            lambda: application._send_gui_runtime("next"),
            on_complete=lambda response, _answered: replies.append(response),
        )
        spin_until(lambda: bool(replies), BOUND, what="the gesture")
        assert not replies[0].ok and problem in replies[0].message
        assert installed.documents == []

        # Runtime health: its markers are saved; nothing is installed.
        with application._runtime_config_lock:
            stale = application._inventory_stamp
        status = json.dumps(
            {
                "config_generation": runtime_config.read_config_generation(),
                "config_path": str(paths.runtime_config_path().absolute()),
                "runtime_instance": "a" * runtime_health.RUNTIME_INSTANCE_HEX_CHARS,
                "config_epoch": 1,
                "playlist_id": runtime_config.FALLBACK_PLAYLIST_ID,
                "playlist": runtime_config.FALLBACK_PLAYLIST_NAME,
                "source": "schedule",
                "taboo_entries": [
                    {
                        "playlist_id": runtime_config.FALLBACK_PLAYLIST_ID,
                        "entry_id": runtime_config.entry_id_for_source(paper),
                        "reason": "renderer rejected this wallpaper",
                        "source": "automatic-apply",
                        "durable": True,
                        "observed_config_epoch": 1,
                    }
                ],
                "taboo_entries_omitted": 0,
            }
        )
        with application._runtime_config_lock:
            generation = application._runtime_authoring_generation
        application._ingest_runtime_health(
            _RuntimeHealthRequest(generation, application.session.library, status, stale)
        )
        item = application.session.library.find(paper)
        assert item is not None
        saved = pairings.Store.open().health(pairings.Identity.of(item))
        assert saved.is_borked, "it got as far as saving, right before the install"
        assert installed.documents == []

        # The headless installer.
        assert application._update_runtime_document() == (False, False)
        assert installed.documents == []

        # A fresh inventory: the next publication installs, carrying all of it.
        application._validate_inventory()
        _publish(application)
        assert len(installed.documents) == 1
        assert application.publication_problem == ""
    finally:
        _close(application)


# -- what the hold audit found ---------------------------------------------------------------


def test_a_removal_committing_during_a_source_change_scan_still_lets_quit_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit cancels the source-changing scan holding the scan barrier; the scan after
    the commit replaces it and ends the hold, so Quit completes."""
    root = tmp_path / "lib" / "R"
    added = tmp_path / "lib" / "S"
    forest, _keep = _file(root / "forest.png"), _file(root / "keep.png")
    _file(added / "dawn.png")
    application = _application(tmp_path, monkeypatch)
    _attach(application, FakeWindow())
    monkeypatch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())
    monkeypatch.setattr(application, "_make_missing_stills", lambda: None)
    quits: list[bool] = []
    monkeypatch.setattr(Application, "quit", lambda _self: quits.append(True))
    release = threading.Event()
    real_scan = scan.scan

    def held(*arguments: Any, **keywords: Any) -> Any:
        if threading.current_thread().name.startswith("library-scan"):
            release.wait(BOUND * 2)
        return real_scan(*arguments, **keywords)

    try:
        _configured(application, root)
        _publish(application)
        monkeypatch.setattr(scan, "scan", held)
        errors: list[str] = []
        application.update_settings_async(roots=(root, added), on_error=errors.append)
        spin_until(lambda: application._runtime_library_scan_held, BOUND, what="the source scan")
        item = application.session.library.find(forest)
        assert item is not None
        removed: list[RemovalResult] = []
        assert application.remove_item_async(item, trash=True, finish=removed.append)
        spin_until(lambda: bool(removed) and _settled_lane(application), BOUND, what="removal")
        assert removed[0].committed and errors == []
        release.set()
        application.request_quit()
        spin_until(lambda: quits == [True], BOUND, what="Quit to complete")
        assert not application._runtime_library_scan_held
        assert application.session.library.find(added / "dawn.png") is not None
    finally:
        release.set()
        _close(application)


def _settled_lane(application: Application) -> bool:
    return _lane_idle(application) and not application._removal_active


def test_a_publication_captured_during_a_refused_removal_ends_while_still_invalid(
    cells: Callable[[Faults], Cell],
) -> None:
    """After a removal whose scans failed, a second removal is refused while a publication is
    waiting on it: that publication ends (nothing installed), and its hold with it."""
    cell = cells(Faults("full", "fails", "fails", "ok", "none"))
    application = cell.application
    item = application.session.library.find(cell.forest)
    assert item is not None
    removed: list[RemovalResult] = []
    assert application.remove_item_async(item, trash=True, finish=removed.append)
    spin_until(
        lambda: (
            bool(removed)
            and _settled(application)
            and not application._runtime_library_scan_pending
            and not application._removal_convergence_held
        ),
        BOUND,
        what="the first removal and its failed scans",
    )
    assert not application._inventory_valid
    count = len(cell.installed.documents)

    # A second removal (Keep: Evening would have nothing left), its dry run held a moment.
    holding = threading.Event()
    go = threading.Event()
    real_scan = scan.scan

    def held_dry_run(*arguments: Any, **keywords: Any) -> Any:
        holding.set()
        go.wait(BOUND)
        return real_scan(*arguments, **keywords)

    keep = application.session.library.find(cell.keep)
    assert keep is not None
    cell.injected.lift()  # nothing else fails now; the scan below is held instead
    setattr(scan, "scan", held_dry_run)  # noqa: B010 - restored by the fixture's monkeypatch
    said_before = len(application.activity_log.entries)
    assert application.remove_item_async(keep, trash=True, finish=removed.append)
    assert holding.wait(BOUND)
    # A publication captured while that removal is in progress.
    assert application._publish_runtime_async()
    assert application._runtime_publication_held
    go.set()

    spin_until(
        lambda: (
            _settled(application)
            and not application._runtime_publication_held
            and len(application.activity_log.entries) > said_before
        ),
        BOUND,
        what="the refusal and the waiting publication to end",
    )
    assert cell.keep.exists() and len(removed) == 1, "the second removal was refused"
    assert not application._inventory_valid, "a refusal changed nothing, and validated nothing"
    assert cell.installed.documents[count:] == []
    assert application.publication_problem != ""
    application.request_quit()
    spin_until(lambda: bool(cell.quits), BOUND, what="Quit to complete")
