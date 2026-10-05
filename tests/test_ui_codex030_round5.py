"""Codex 0.3.0 round 5, in the real application: every way a removal's aftermath can fail.

One parametrized matrix over the real application crosses what can go wrong
after a removal commits with what a person can do meanwhile:

- the commit: full, or partial (a cleanup failed, so a refresh follows);
- the removal's own scan after the commit: fine, failing, held (then fine),
  or held and then failing;
- the fallback refresh, where one runs: fine, failing, or held (then fine);
- writing the runtime configuration: fine, or failing for real (the state
  directory made read-only, EACCES), from the first scan that succeeds;
- the action interleaved at the failure point (while a scan is held, or once
  everything has landed): none, Quit, Next, a favourite from the window, a
  favourite, a playlist addition and a removal over ctl, a settings change,
  and a second removal from the window.

Only the faults are injected (the scan wrapper, a cleanup failure, chmod).
For every cell: every runtime document installed at any time names only
files that exist when it is installed; Quit completes within a bound; no
worker spins; every hold settles; the action completes or refuses with a
reason within a bound; and once the faults are lifted, a refresh recovers
and what is installed names nothing missing. Codex's T-1, T-2 and T-3 are
named cells; each fails on 9fddd73.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
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
from wall_in_one.library import pairings, scan  # noqa: E402
from wall_in_one.session import RemovalPlan, RemovalResult  # noqa: E402
from wall_in_one.ui.app import Application, _Commands  # noqa: E402

#: How long anything here may take before it counts as stuck.
BOUND = 15.0
#: An idle stretch: a spinning worker burns about all of it in CPU time.
IDLE = 0.2


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
    write: str  # ok | fails
    action: str


@dataclass
class Injected:
    """The fault injection, and nothing else: scans, a cleanup failure, a read-only directory."""

    faults: Faults
    forest: Path
    state: Path
    lifted: bool = False
    postscan_used: bool = False
    fallback_used: bool = False
    read_only: bool = False
    reached: dict[str, threading.Event] = field(
        default_factory=lambda: {"postscan": threading.Event(), "fallback": threading.Event()}
    )
    release: threading.Event = field(default_factory=threading.Event)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_scan = scan.scan

        def faulty(*arguments: Any, **keywords: Any) -> Any:
            stage = self._stage()
            behaviour = getattr(self.faults, stage) if stage else "ok"
            if behaviour.startswith("held"):
                self.reached[stage].set()
                assert self.release.wait(BOUND * 2), "the test never released a held scan"
            if behaviour.endswith("fails"):
                raise OSError("injected scan fault")
            library = real_scan(*arguments, **keywords)
            if stage and self.faults.write == "fails" and not self.read_only:
                # From the first post-commit scan that succeeds: EACCES on every write.
                os.chmod(self.state, 0o500)
                self.read_only = True
            return library

        monkeypatch.setattr(scan, "scan", faulty)
        if self.faults.commit == "partial":
            commit = RemovalPlan.run

            def partial(plan: RemovalPlan) -> RemovalResult:
                result = commit(plan)
                if plan.item.path == self.forest and result.committed:
                    return replace(result, cleanup_failures=("favourites: injected",))
                return result

            monkeypatch.setattr(RemovalPlan, "run", partial)

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

    def lift(self) -> None:
        self.lifted = True
        self.release.set()
        if self.read_only:
            os.chmod(self.state, 0o700)
            self.read_only = False


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


def _act(cell: Cell, action: str) -> Callable[[], str | None]:
    """Start ``action``; returns how it ended so far (None while it hasn't)."""
    application = cell.application
    reports_before = len(cell.window.reports)
    if action == "none":
        return lambda: "nothing"
    if action == "quit":
        application.request_quit()
        return lambda: "quit" if cell.quits else None
    if action == "next":
        replies: list[Response] = []
        started = application._start_gui_runtime_call(
            lambda: application._send_gui_runtime("next"),
            on_complete=lambda response, _answered: replies.append(response),
        )
        if not started:
            return lambda: "refused: " + (cell.window.reports[-1] if cell.window.reports else "")
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
    if action == "ctl-remove":
        replies_of = _ctl(application, "remove", str(cell.other))
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
    if action == "second-removal":
        item = application.session.library.find(cell.other)
        assert item is not None
        finished: list[RemovalResult] = []
        accepted = application.remove_item_async(item, trash=True, finish=finished.append)
        if not accepted:
            return lambda: "refused: not accepted"

        def outcome() -> str | None:
            if finished:
                return (
                    "removed" if finished[0].committed else f"refused: {finished[0].error_message}"
                )
            if len(cell.window.reports) > reports_before:
                return f"refused: {cell.window.reports[-1]}"
            return None

        return outcome
    raise AssertionError(action)


def _said(replies: list[Response]) -> str | None:
    if not replies:
        return None
    reply = replies[0]
    return "done" if reply.ok else f"refused: {reply.message}"


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
    assert application.remove_item_async(item, trash=True, finish=removed.append)

    _point(cell)
    ended = _act(cell, faults.action)
    cell.injected.release.set()  # held scans go on now (and succeed or fail as set)

    # The action completes, or refuses with a reason, within the bound.
    spin_until(lambda: ended() is not None, BOUND, what=f"the {faults.action} to end")
    outcome = ended()
    assert outcome is not None
    if outcome.startswith("refused"):
        assert len(outcome) > len("refused: "), "a refusal says why"
    spin_until(lambda: bool(removed), BOUND, what="the removal")
    assert removed[0].committed and not cell.forest.exists()

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
        application.request_quit()

    # Quit completes within the bound, whatever happened.
    spin_until(lambda: cell.quits == [True], BOUND, what="Quit to complete")
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
WRITES = ("ok", "fails")
ACTIONS = (
    "none",
    "quit",
    "next",
    "favourite",
    "ctl-favourite",
    "ctl-playlist-add",
    "settings",
    "second-removal",
    "ctl-remove",
)

#: Codex's reproductions, as cells.
NAMED = {
    "T-1-terminal-scan-failure-then-quit": Faults("full", "held-fails", "fails", "ok", "quit"),
    "T-1-runtime-write-failure-then-quit": Faults("full", "ok", "-", "fails", "quit"),
    "T-2-next-after-the-scans-failed": Faults("full", "fails", "fails", "ok", "next"),
    "T-3-edit-after-the-fallback-failed": Faults("full", "fails", "fails", "ok", "ctl-favourite"),
    "T-3-edit-while-the-fallback-is-held": Faults("full", "fails", "held", "ok", "ctl-favourite"),
}


def _grid() -> list[Any]:
    named = set(NAMED.values())
    cells: list[Any] = [pytest.param(faults, id=name) for name, faults in NAMED.items()]
    for commit, postscan, fallback in SCANS:
        for write in WRITES:
            if write == "fails" and postscan.endswith("fails") and fallback == "fails":
                continue  # no scan succeeds, so nothing is ever written
            for action in ACTIONS:
                faults = Faults(commit, postscan, fallback, write, action)
                if faults in named:
                    continue
                cells.append(
                    pytest.param(
                        faults,
                        id=f"{commit}-own:{postscan}-fallback:{fallback}-write:{write}-{action}",
                    )
                )
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
