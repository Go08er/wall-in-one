"""Codex 0.3.0 round 7: removals, scans, writes and gestures in any reachable order.

Crossing post-commit outcomes cell by cell can't reach every ordering: V-1
was a refresh scan, already running when a removal prepared, ending while
that removal was refused. So this drives the real application with a seeded
random interleaving of everything that can happen, each triggered through
the same fault controls the matrix uses, and only when it can happen:

- a refresh scan starts; any scan held at its gate (a refresh, a removal's
  dry run, its scan after the commit, a fallback refresh) is let go, to
  succeed or to fail, in whatever order the seed picks;
- a removal starts, from the window or over ctl, of a wallpaper whose dry run
  allows it, refuses it (a used playlist would lose its last), or finds it
  gone; its commit full, or partial (the favourites lock held elsewhere, so
  its cleanup really fails);
- the runtime write becomes fine, EACCES (a read-only state directory) or
  refused (the size cap lowered);
- a favourite from the window, a favourite over ctl, a settings change, Next,
  the last window closing, Quit.

Window gestures (a removal or the star from the window, a settings change,
Next) are offered only while the window is open; ctl's stay offered after it
closes, and a gesture made before it closed still lands. The star toggles,
as the window's does. Each user action is counted as admitted, refused or a
no-op (the gesture found nothing to act on), and the module prints the tally.

After every step: every runtime document installed so far names only files
that existed when it was installed; no scan is reported pending without the
work that will end it behind it -- the current scan generation's future or
request, that generation's reconciliation on the authoring lane, or a
committed removal's scan tail -- never just any unrelated work on the lane;
nothing spins (lock acquisitions per second stay bounded). At the
end, with every gate open and the faults lifted: every hold settles, nothing
spins (process CPU over an idle stretch), a refresh recovers to a valid
document, and Quit completes. A failing seed prints its steps, to replay
with ``WIO_INTERLEAVING_SEED=<seed>``; ``WIO_INTERLEAVING_SEEDS=<n>`` runs more
seeds locally, and ``WIO_INTERLEAVING_STEPS=<n>`` makes them longer. Codex's
V-1 sequences, through the window and over ctl, with the refresh failing
before and after the refusal, are fixed named cases, as are the orderings
this test found itself.
"""

from __future__ import annotations

import os
import random
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import settle, spin_until  # noqa: E402
from tests.test_dry_run import _file, _missing  # noqa: E402
from tests.test_ui_codex030_round2 import _lane_idle  # noqa: E402
from tests.test_ui_runtime import FakeWindow, _application, _attach, _close  # noqa: E402
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.control import client, server  # noqa: E402
from wall_in_one.control.protocol import Request, Response  # noqa: E402
from wall_in_one.library import favourites, scan, state_file  # noqa: E402
from wall_in_one.session import RemovalResult  # noqa: E402
from wall_in_one.ui.app import Application, _Commands  # noqa: E402

#: How long anything may take before it counts as stuck.
BOUND = 15.0
#: Seeds in the gate; more locally with WIO_INTERLEAVING_SEEDS, one with WIO_INTERLEAVING_SEED.
SEEDS = int(os.environ.get("WIO_INTERLEAVING_SEEDS", "200"))
#: Steps in one interleaving (longer ones locally with WIO_INTERLEAVING_STEPS).
STEPS = int(os.environ.get("WIO_INTERLEAVING_STEPS", "10"))
#: More acquisitions of the runtime lock per second than this is a worker spinning.
SPIN_RATE = 20_000
#: How long each step's work gets to land before the next; also the window the
#: lock rate is measured over.
DRAIN = 0.01
#: An idle stretch at the end, and the CPU the process may spend in it.
IDLE, IDLE_CPU = 0.05, 0.025
TINY_CAP = 64


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class CountingLock:
    """The application's runtime lock, counted: a spinning compiler takes it constantly."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.taken = 0

    def __enter__(self) -> bool:
        self._lock.acquire()
        self.taken += 1
        return True

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()


@dataclass
class Held:
    """One scan call stopped at its gate until the driver lets it go."""

    kind: str  # refresh | authoring
    go: threading.Event = field(default_factory=threading.Event)
    fail: bool = False


class Gates:
    """Every scan the application runs stops here; the driver decides when, and how it ends."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.held: list[Held] = []
        self.open = True  # setup and the end let everything through

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = scan.scan

        def gated(*arguments: Any, **keywords: Any) -> Any:
            if not self.open:
                kind = (
                    "refresh"
                    if threading.current_thread().name.startswith("library-scan")
                    else "authoring"
                )
                call = Held(kind)
                with self.lock:
                    self.held.append(call)
                assert call.go.wait(BOUND * 2), "a held scan was never let go"
                if call.fail:
                    raise OSError("injected scan fault")
            return real(*arguments, **keywords)

        monkeypatch.setattr(scan, "scan", gated)

    def waiting(self, kind: str | None = None) -> list[Held]:
        with self.lock:
            return [one for one in self.held if kind is None or one.kind == kind]

    def let_go(self, call: Held, *, fail: bool) -> None:
        with self.lock:
            self.held.remove(call)
        call.fail = fail
        call.go.set()

    def open_all(self) -> None:
        self.open = True
        for call in self.waiting():
            self.let_go(call, fail=False)


@dataclass
class ScanWork:
    """What a pending scan can be waiting for, tracked at the application's own seams (W-2)."""

    #: Generations whose reconciliation is queued or running on the authoring lane.
    reconciling: set[int] = field(default_factory=set)
    #: A committed removal's scan tail: the generation its adoption left pending,
    #: with the refresh after it scheduled but not started.
    tail: int | None = None

    def backs(self, application: Application) -> bool:
        """The work that will end the current pending scan, if any is live."""
        generation = application._runtime_library_scan_generation
        current_scan = application._library_scan_generation == generation and (
            application._library_scan_future is not None
            or application._library_scan_request is not None
        )
        return current_scan or generation in self.reconciling or self.tail == generation


#: How one user action ended up (W-1).
ADMITTED, REFUSED, NO_OP = "admitted", "refused", "no-op"


@dataclass
class Action:
    """One user action, resolved once its work has settled."""

    event: str
    outcome: Callable[[], str]


#: Every seed's resolved actions, for the module's tally: (action kind, outcome) -> count.
TALLY: dict[tuple[str, str], int] = {}


def _kind(event: str) -> str:
    """``remove:window:keep:full`` -> ``remove:window``; ``star`` -> ``star``."""
    parts = event.split(":")
    return ":".join(parts[:2]) if parts[0] == "remove" else parts[0]


@dataclass
class World:
    application: Application
    window: FakeWindow
    files: dict[str, Path]
    playlist: str
    gates: Gates
    installed: list[tuple[str, list[str]]]
    quits: list[bool]
    lock: CountingLock
    state: Path
    original_cap: int
    trace: list[str] = field(default_factory=list)
    replies: list[Callable[[], bool]] = field(default_factory=list)
    write: str = "ok"
    favourites_free: threading.Event | None = None
    quit_requested: bool = False
    window_open: bool = True
    scans: ScanWork = field(default_factory=ScanWork)
    actions: list[Action] = field(default_factory=list)

    def tally(self) -> dict[tuple[str, str], int]:
        counts: dict[tuple[str, str], int] = {}
        for action in self.actions:
            key = (_kind(action.event), action.outcome())
            counts[key] = counts.get(key, 0) + 1
        return counts

    def set_write(self, mode: str) -> None:
        os.chmod(self.state, 0o500 if mode == "eacces" else 0o700)
        setattr(  # noqa: B010 - restored at teardown by the fixture's monkeypatch
            runtime_config,
            "MAX_RUNTIME_CONFIG_BYTES",
            TINY_CAP if mode == "size-cap" else self.original_cap,
        )
        self.write = mode

    def hold_favourites(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A real failed cleanup: someone else holds the favourites lock while a removal runs."""
        if self.favourites_free is not None:
            return
        free = threading.Event()
        held = threading.Event()
        self.favourites_free = free
        real_lock = state_file.mutation_lock

        def hold() -> None:
            with real_lock(favourites.state_path(), description="favourites", process_gate=False):
                held.set()
                free.wait(BOUND * 2)

        threading.Thread(target=hold, daemon=True).start()
        assert held.wait(5)

    def free_favourites(self) -> None:
        if self.favourites_free is not None:
            self.favourites_free.set()
            self.favourites_free = None


@pytest.fixture
def worlds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., World]]:
    made: list[World] = []
    # This checks ordering, not durability (test_ui_runtime has the fsync
    # contract), and an fsync per state write is most of a seed on a real disk.
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)

    def make(evening: tuple[str, ...] = ("forest", "keep")) -> World:
        root = tmp_path / "lib" / "R"
        files = {name: _file(root / f"{name}.png") for name in ("forest", "keep", "other")}
        application = _application(tmp_path, monkeypatch)
        lock = CountingLock()
        application._runtime_config_lock = lock  # type: ignore[assignment]
        window = FakeWindow()
        _attach(application, window)
        monkeypatch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())
        monkeypatch.setattr(application, "_make_missing_stills", lambda: None)
        quits: list[bool] = []
        monkeypatch.setattr(Application, "quit", lambda _self: quits.append(True))
        # The favourites lock waits briefly while someone else holds it (a partial commit).
        real_lock = state_file.mutation_lock
        world_box: list[World] = []

        def briefly(
            target: Path, *, description: str, **keywords: Any
        ) -> AbstractContextManager[None]:
            if description == "favourites" and world_box and world_box[0].favourites_free:
                keywords["timeout"] = 0.1
            return real_lock(target, description=description, **keywords)

        monkeypatch.setattr(state_file, "mutation_lock", briefly)
        monkeypatch.setattr(
            runtime_config, "MAX_RUNTIME_CONFIG_BYTES", runtime_config.MAX_RUNTIME_CONFIG_BYTES
        )
        settings = replace(application.settings, roots=(root,)).validated()
        config.save(settings)
        application._settings = settings
        application._settings_requested = settings
        session = application.session
        session.update_settings(settings, rescan_library=False)
        session.refresh(mutate_removals=False)
        application._accepted_library_sources = (settings.roots, settings.scan_workshop)
        playlist = session.playlists.create("Evening")
        for name in evening:
            session.playlists.add(playlist.id, files[name])
        session.schedules.add(playlist.id)
        for path in files.values():
            session.favourites.add(path)  # so a removal's cleanup has a favourite to clear
        installed: list[tuple[str, list[str]]] = []
        install = runtime_config.update

        def recorded(current: config.Settings, snapshot: Any, path: Path | None = None) -> bool:
            changed = install(current, snapshot, path)
            if changed:
                target = path if path is not None else paths.runtime_config_path()
                document = target.read_text(encoding="utf-8")
                installed.append((document, _missing(document)))
            return changed

        monkeypatch.setattr(runtime_config, "update", recorded)
        scans = _watch_scan_work(application, monkeypatch)
        assert application._publish_runtime_async()
        spin_until(lambda: _quiet(application), BOUND, what="the first publication")
        gates = Gates()
        gates.install(monkeypatch)
        gates.open = False
        world = World(
            application,
            window,
            files,
            playlist.id,
            gates,
            installed,
            quits,
            lock,
            paths.runtime_config_path().parent,
            runtime_config.MAX_RUNTIME_CONFIG_BYTES,
            scans=scans,
        )
        world_box.append(world)
        made.append(world)
        return world

    yield make
    for world in made:
        world.gates.open_all()
        world.free_favourites()
        world.set_write("ok")
        _close(world.application)


def _quiet(application: Application) -> bool:
    return (
        _lane_idle(application)
        and not application._runtime_compile_pending
        and application._library_scan_future is None
        and not application._runtime_action_pending
    )


def _watch_scan_work(application: Application, monkeypatch: pytest.MonkeyPatch) -> ScanWork:
    """Record, at the application's own seams, the work each pending scan waits for."""
    scans = ScanWork()
    queue = application._queue_library_reconciliation
    finish = application._finish_runtime_library_scan
    failed = application._library_reconciliation_failed
    adopt = application._adopt_item_removal

    def queued(generation: int, request: Any, library: Any) -> None:
        scans.reconciling.add(generation)
        queue(generation, request, library)

    def finished(generation: int, *, error: str = "") -> None:
        scans.reconciling.discard(generation)
        if scans.tail == generation:
            scans.tail = None
        finish(generation, error=error)

    def reconciliation_failed(generation: int, message: str) -> None:
        scans.reconciling.discard(generation)
        failed(generation, message)

    def adopted(result: RemovalResult) -> None:
        adopt(result)
        if (
            result.committed
            and application._runtime_library_scan_pending
            and application._library_scan_future is None
            and application._library_scan_request is None
        ):
            # The refresh after the commit is scheduled; until it starts, this is its tail.
            scans.tail = application._runtime_library_scan_generation

    monkeypatch.setattr(application, "_queue_library_reconciliation", queued)
    monkeypatch.setattr(application, "_finish_runtime_library_scan", finished)
    monkeypatch.setattr(application, "_library_reconciliation_failed", reconciliation_failed)
    monkeypatch.setattr(application, "_adopt_item_removal", adopted)
    return scans


def _scan_work_live(world: World) -> bool:
    """The work that will end the pending scan: its future or request, its
    reconciliation, or a committed removal's tail (W-2: not any authoring work)."""
    return world.scans.backs(world.application)


# -- events -------------------------------------------------------------------------------


def _ctl(world: World, verb: str, argument: str) -> Callable[[], str]:
    """Send one ctl request; its outcome is its reply's."""
    replies: list[Response] = []
    server.dispatch(
        server.build_verb_table(_Commands(world.application)),
        Request(verb, argument).encode(),
        replies.append,
    )
    world.replies.append(lambda: bool(replies))
    return lambda: ADMITTED if replies and replies[0].ok else REFUSED


def _admitted(accepted: bool | None) -> Callable[[], str]:
    return lambda: ADMITTED if accepted is not False else REFUSED


def _no_op() -> str:
    return NO_OP


def _remove(
    world: World, via: str, target: str, commit: str, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], str]:
    application = world.application
    path = world.files[target]
    item = application.session.library.find(path)
    if commit == "partial":
        world.hold_favourites(monkeypatch)
    if via == "ctl":
        replied = _ctl(world, "remove", str(path))
        return _no_op if item is None else replied
    if item is None:
        return _no_op  # the window shows no card for what the library lacks
    finished: list[RemovalResult] = []
    accepted = application.remove_item_async(item, trash=True, finish=finished.append)

    def outcome() -> str:
        # A queued removal can still be refused by its own dry run (or be busy).
        committed = accepted and finished and finished[0].committed
        return ADMITTED if committed else REFUSED

    return outcome


def _star(world: World) -> Callable[[], str]:
    """As the window's star does (`RealState.toggle_favorite`): add or discard, by what is saved."""
    application = world.application
    item = application.session.library.find(world.files["keep"])
    if item is None:
        return _no_op
    wanted = item.path not in application.session.favourites.paths
    changed: list[bool] = []

    def prepare() -> Callable[[], bool]:
        current = application.current_item_for_authoring(item)
        store = application.session.favourites
        return lambda: store.add(current.path) if wanted else store.discard(current.path)

    def saved(did: bool) -> None:
        changed.append(did)
        application.favourites_changed()

    accepted = application.authoring_action_async(
        lambda: False, saved, prepare=prepare, failure=lambda _error: None
    )
    return lambda: ADMITTED if accepted and changed else REFUSED


def _apply(world: World, event: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Do ``event``; every one of them can happen in the state it is chosen in."""
    application = world.application
    kind, _, rest = event.partition(":")
    outcome: Callable[[], str] | None = None
    if kind == "refresh":
        application.refresh_library()
        outcome = _admitted(None)
    elif kind == "let-go":
        which, result = rest.split(":")
        waiting = world.gates.waiting(which)
        assert waiting, f"no {which} scan is held to let go"
        world.gates.let_go(waiting[0], fail=result == "fail")
    elif kind == "remove":
        via, target, commit = rest.split(":")
        outcome = _remove(world, via, target, commit, monkeypatch)
    elif kind == "write":
        world.set_write(rest)
    elif kind == "star":
        outcome = _star(world)
    elif kind == "ctl-favourite":
        outcome = _ctl(world, "favourite", str(world.files["other"]))
    elif kind == "settings":
        outcome = _admitted(
            application.update_settings_async(shuffle=not application.settings.shuffle)
        )
    elif kind == "next":
        outcome = _admitted(
            application._start_gui_runtime_call(lambda: application._send_gui_runtime("next"))
        )
    elif kind == "close":
        application._on_close_request(world.window)  # type: ignore[arg-type]
        world.window_open = False
        outcome = _admitted(None)
    elif kind == "quit":
        application.request_quit()
        world.quit_requested = True
        outcome = _admitted(None)
    else:  # pragma: no cover - a typo in a script
        raise AssertionError(event)
    if outcome is not None:
        world.actions.append(Action(event, outcome))


def _choices(world: World) -> list[tuple[str, int]]:
    """The events possible now, weighted: letting held scans go keeps things moving."""
    options: list[tuple[str, int]] = []
    for which in ("refresh", "authoring"):
        if world.gates.waiting(which):
            options += [(f"let-go:{which}:ok", 4), (f"let-go:{which}:fail", 2)]
    if world.quit_requested:
        return options
    options += [("refresh", 2), ("ctl-favourite", 1)]
    # A closed window makes no new gestures (one made before it closed still lands).
    if world.window_open:
        options += [("star", 1), ("settings", 1), ("next", 1)]
    for via in ("window", "ctl") if world.window_open else ("ctl",):
        for target in ("forest", "keep", "other"):
            for commit in ("full", "partial"):
                options.append((f"remove:{via}:{target}:{commit}", 1))
    options += [
        (f"write:{mode}", 1) for mode in ("ok", "eacces", "size-cap") if mode != world.write
    ]
    if world.window_open:
        options.append(("close", 1))
    options.append(("quit", 1))
    return options


# -- the run ----------------------------------------------------------------------------------


def _step_invariants(world: World) -> None:
    application = world.application
    named_missing = [missing for _document, missing in world.installed if missing]
    assert named_missing == [], "a generation named a file that wasn't there when installed"
    # A pending scan has real work behind it (a transition may take a moment to land;
    # a ghost never does).
    try:
        spin_until(
            lambda: not application._runtime_library_scan_pending or _scan_work_live(world),
            2.0,
            what="a pending scan to have real work behind it",
        )
    except AssertionError as failure:
        raise AssertionError(f"{failure}; state: {_state(application)}") from failure


def _drain(world: World) -> None:
    """Let what the step started land, and check that nothing spun meanwhile."""
    lock = world.lock
    taken, started = lock.taken, time.monotonic()
    settle(DRAIN)
    application = world.application
    if (
        world.favourites_free is not None
        and _lane_idle(application)
        and not application._removal_active
    ):
        world.free_favourites()  # whoever held them is done
    elapsed = max(time.monotonic() - started, 1e-3)
    assert (lock.taken - taken) / elapsed < SPIN_RATE, "a worker is spinning on the runtime lock"


def _state(application: Application) -> str:
    """What is held and pending, for a failure message."""
    return (
        f"authoring_active={application._authoring_active} "
        f"queue={len(application._authoring_queue)} "
        f"lifetime_holds={application._authoring_lifetime_holds} "
        f"compile_pending={application._runtime_compile_pending} "
        f"action_pending={application._runtime_action_pending} "
        f"scan_future={application._library_scan_future is not None} "
        f"scan_pending={application._runtime_library_scan_pending} "
        f"scan_held={application._runtime_library_scan_held} "
        f"publication_held={application._runtime_publication_held} "
        f"convergence_held={application._removal_convergence_held} "
        f"removal_invalidated={application._removal_runtime_invalidated} "
        f"inventory_valid={application._inventory_valid} "
        f"request={application._runtime_authoring_request is not None} "
        f"authoring_generation={application._runtime_authoring_generation} "
        f"compiled={application._runtime_compiled_generation} "
        f"settings_running={application._settings_authoring_running}"
    )


def _finish(world: World) -> None:
    """Every gate open, the faults lifted: holds settle, nothing spins, recovery, Quit."""
    try:
        _finish_checked(world)
    except AssertionError as failure:
        raise AssertionError(f"{failure}; state: {_state(world.application)}") from failure


def _finish_checked(world: World) -> None:
    application = world.application
    world.gates.open_all()
    world.free_favourites()
    world.set_write("ok")
    spin_until(
        lambda: (
            _quiet(application)
            and all(done() for done in world.replies)
            and not application._removal_convergence_held
            and not application._runtime_publication_held
            and not application._runtime_library_scan_held
            and not application._runtime_library_scan_pending
        ),
        BOUND,
        what="every hold to settle once nothing is pending",
    )
    spent = time.process_time()
    time.sleep(IDLE)
    assert time.process_time() - spent < IDLE_CPU, "a worker spins"
    if not world.quit_requested:
        application.refresh_library()
        spin_until(lambda: _quiet(application), BOUND, what="the recovering scan")
        assert application._publish_runtime_async()
        spin_until(lambda: _quiet(application), BOUND, what="the recovering publication")
        document = paths.runtime_config_path().read_text(encoding="utf-8")
        assert _missing(document) == [], "the recovered runtime names a missing file"
        assert application._inventory_valid
        assert application.publication_problem == ""
        application.request_quit()
    # Completed at least once (with the real quit the main loop stops there; the
    # recording stand-in lets later callbacks ask again).
    spin_until(lambda: bool(world.quits), BOUND, what="Quit to complete")
    assert application._authoring_lifetime_holds == 0
    named_missing = [missing for _document, missing in world.installed if missing]
    assert named_missing == [], "a generation named a file that wasn't there when installed"
    for key, count in world.tally().items():
        TALLY[key] = TALLY.get(key, 0) + count


def _play(world: World, events: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    for event in events:
        world.trace.append(event)
        _apply(world, event, monkeypatch)
        _drain(world)
        _step_invariants(world)
    _finish(world)


def _seeded(seed: int, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    rng = random.Random(seed)
    for _ in range(STEPS):
        options = _choices(world)
        if not options:
            break
        events, weights = zip(*options, strict=True)
        event = rng.choices(events, weights=weights)[0]
        world.trace.append(event)
        _apply(world, event, monkeypatch)
        _drain(world)
        _step_invariants(world)
    _finish(world)


def _seeds() -> list[int]:
    one = os.environ.get("WIO_INTERLEAVING_SEED")
    return [int(one)] if one else list(range(SEEDS))


@pytest.fixture(scope="module", autouse=True)
def tally(pytestconfig: pytest.Config) -> Iterator[None]:
    """Print how the module's user actions ended: what was exercised, not just how much."""
    TALLY.clear()
    yield
    lines = ["interleaving actions (admitted / refused / no-op):"]
    for kind in sorted({kind for kind, _outcome in TALLY}):
        counts = [TALLY.get((kind, outcome), 0) for outcome in (ADMITTED, REFUSED, NO_OP)]
        lines.append(f"  {kind}: {counts[0]} / {counts[1]} / {counts[2]}")
    totals = [
        sum(count for (_kind, outcome), count in TALLY.items() if outcome == wanted)
        for wanted in (ADMITTED, REFUSED, NO_OP)
    ]
    lines.append(f"  all: {totals[0]} / {totals[1]} / {totals[2]}")
    # Module teardown runs inside the last test's output capture, which a
    # passing test discards: print past it, as `capsys.disabled()` does.
    reporter = pytestconfig.pluginmanager.get_plugin("terminalreporter")
    capture = pytestconfig.pluginmanager.get_plugin("capturemanager")
    with capture.global_and_fixture_disabled() if capture is not None else nullcontext():
        for line in lines:
            if reporter is not None:
                reporter.write_line(line)
            else:  # pragma: no cover - pytest always has one
                print(line)


@pytest.mark.parametrize("seed", _seeds(), ids=lambda seed: f"seed-{seed}")
def test_any_reachable_interleaving_keeps_every_invariant(
    seed: int, worlds: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = worlds()
    try:
        _seeded(seed, world, monkeypatch)
    except AssertionError as failure:
        raise AssertionError(
            f"seed {seed} failed after {world.trace} ({world.tally()}); replay with "
            f"WIO_INTERLEAVING_SEED={seed}: {failure}"
        ) from failure


# -- Codex's V-1, fixed ---------------------------------------------------------------------

#: A refresh is already running when a removal of Keep (Evening's only wallpaper) prepares;
#: the refresh fails before, or after, that removal is refused.
V1 = {
    "V-1-window-refresh-fails-before-the-refusal": [
        "refresh",
        "remove:window:keep:full",
        "let-go:refresh:fail",
        "let-go:authoring:ok",
    ],
    "V-1-ctl-refresh-fails-before-the-refusal": [
        "refresh",
        "remove:ctl:keep:full",
        "let-go:refresh:fail",
        "let-go:authoring:ok",
    ],
    "V-1-window-refresh-fails-before-the-refusal-then-the-window-closes": [
        "refresh",
        "remove:window:keep:full",
        "let-go:refresh:fail",
        "let-go:authoring:ok",
        "close",
    ],
    "V-1-ctl-dry-run-throws-after-the-refresh-failed": [
        "refresh",
        "remove:ctl:keep:full",
        "let-go:refresh:fail",
        "let-go:authoring:fail",
    ],
    # Controls: the refresh fails after the refusal (both tips pass these). A refusing dry
    # run scans twice (the second from disk, to blame only what the removal breaks), so
    # both are let go before the refresh ends.
    "V-1-control-window-refresh-fails-after-the-refusal": [
        "refresh",
        "remove:window:keep:full",
        "let-go:authoring:ok",
        "let-go:authoring:ok",
        "let-go:refresh:fail",
    ],
    "V-1-control-ctl-refresh-fails-after-the-refusal": [
        "refresh",
        "remove:ctl:keep:full",
        "let-go:authoring:ok",
        "let-go:authoring:ok",
        "let-go:refresh:fail",
    ],
}


def _wait_held(world: World, kind: str) -> None:
    spin_until(lambda: bool(world.gates.waiting(kind)), BOUND, what=f"a held {kind} scan")


def _scripted(world: World, events: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Play ``events`` in order (each held scan waited for), then finish as a seed does."""
    for event in events:
        if event.startswith("let-go:"):
            _wait_held(world, event.split(":")[1])
        world.trace.append(event)
        _apply(world, event, monkeypatch)
        _drain(world)
        _step_invariants(world)

    # Whatever the script left running: a dry run may scan once more, from disk, before
    # it refuses.
    def settled() -> bool:
        for call in world.gates.waiting("authoring"):
            world.gates.let_go(call, fail=False)
        return _quiet(world.application)

    spin_until(settled, BOUND, what="the script's work to settle")
    _step_invariants(world)
    world.application.request_quit()
    world.quit_requested = True
    _finish(world)


@pytest.mark.parametrize("name", V1)
def test_v1_a_refresh_ending_during_a_refused_removal_leaves_nothing_pending(
    name: str, worlds: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = worlds(evening=("keep",))
    try:
        _scripted(world, V1[name], monkeypatch)
    except AssertionError as failure:
        raise AssertionError(f"{name} failed after {world.trace}: {failure}") from failure
    assert world.files["keep"].exists(), "the removal was refused: Keep stays"


#: Found by the interleaving test itself (seeds noted), kept as fixed cases.
FOUND = {
    # Seed 166 (20 steps): a publication deferred behind a held refresh, its request then
    # withdrawn by a removal's prepare; the removal is refused while the inventory stays
    # invalid and the refresh fails, so nothing published again and its hold never ended.
    "seed-166-publication-hold-of-a-withdrawn-request": [
        "remove:window:forest:full",
        "let-go:authoring:ok",
        "let-go:authoring:fail",
        "let-go:refresh:fail",
        "refresh",
        "ctl-favourite",
        "remove:window:keep:full",
        "let-go:authoring:ok",
        "let-go:authoring:ok",
        "let-go:refresh:fail",
    ],
}


@pytest.mark.parametrize("name", FOUND)
def test_orderings_the_interleaving_test_found(
    name: str, worlds: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = worlds()
    try:
        _scripted(world, FOUND[name], monkeypatch)
    except AssertionError as failure:
        raise AssertionError(f"{name} failed after {world.trace}: {failure}") from failure


def test_next_while_a_refresh_is_held_waits_without_spinning(
    worlds: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found by seed 4: Next waiting for an ordinary (same-sources) refresh found the ready
    event still set and looped for as long as the scan ran. Now it sleeps until the scan ends."""
    world = worlds()
    world.trace.append("refresh")
    _apply(world, "refresh", monkeypatch)
    _wait_held(world, "refresh")
    world.trace.append("next")
    _apply(world, "next", monkeypatch)
    for _ in range(5):  # a while, with the scan still held
        _drain(world)
    assert world.application._runtime_action_pending, "Next waits for the scan"
    world.trace.append("let-go:refresh:ok")
    _apply(world, "let-go:refresh:ok", monkeypatch)
    spin_until(lambda: not world.application._runtime_action_pending, BOUND, what="Next to finish")
    _finish(world)
