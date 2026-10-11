"""A read-only profile or library: refused with a reason, nothing half-done, nothing stuck.

Three folders, one at a time, made read-only with their real permission bits
(`tests.read_only_probe.read_only`; no injected error anywhere): the
configuration folder (``settings.toml``, ``ui.toml``, the Wallhaven key), the
state folder (every store, the removal journal, ``runtime.toml``) and a
library folder. The profile is healthy first, made by a running application
while everything was writable (`tests.read_only_probe.build`); then the folder
goes read-only; then the application starts, as a person's would after a
remount, a restored backup or a ``chmod``.

Three surfaces: the control socket's verb table, the new window
(`NextWindow`, through its own pages and adapters) and the classic window
(`MainWindow`, through its pages' own handlers). After every attempted
change (`tests.read_only_probe.Probe.judge`):

* a change that has to write the read-only folder is refused, and somebody
  is told why;
* every file under the configuration folder, the state folder, the library
  and the trash is byte-identical (lock files beside a store aside);
* whatever it touches: the read-only folder is untouched, nothing was thrown,
  nothing is still held once the lanes are idle, what the application shows
  is what the files hold, and no store names something another store lost.

The window runs also start with the folder already read-only (bounded time,
a bounded number of reports, the library loaded), sit through status polls
that report a wallpaper the runtime couldn't play (a health write every two
seconds), and are closed or quit with the folder still read-only.

The gate runs a representative handful per case and surface; the whole table,
each ``ctl`` verb alone in a window's process and a ``--service`` one, and the
files-only variant (``chmod a-w`` on the files, the folders left writable) run
with ``WIO_PROBE_FULL=1``. What these found is pinned below as strict expected
failures, each with its recipe.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from tests import read_only_probe as ro  # noqa: E402
from tests import released_faults as faults  # noqa: E402
from tests.gtk_helpers import settle, spin_until  # noqa: E402
from tests.test_providers_fakes import FakeClient  # noqa: E402
from tests.test_providers_wallhaven import download_routes  # noqa: E402
from tests.test_ui_next_settings import NOCTALIA, _noctalia_settings  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402
    FakeRuntime,
    Step,
    application_lanes,
    control,
    first_frame_step,
    patient,
    run_application,
)
from wall_in_one import config, paths, runtime_config, runtime_health, tidy  # noqa: E402
from wall_in_one.control import client, server  # noqa: E402
from wall_in_one.control.protocol import Request, Response  # noqa: E402
from wall_in_one.library import pairings, playlists  # noqa: E402
from wall_in_one.library.model import MediaItem  # noqa: E402
from wall_in_one.providers import http  # noqa: E402
from wall_in_one.theme import noctalia  # noqa: E402
from wall_in_one.ui.app import Application, _Commands  # noqa: E402
from wall_in_one.ui.next.real_editing import RealEditing  # noqa: E402
from wall_in_one.ui.next.real_playlists import RealPlaylists  # noqa: E402
from wall_in_one.ui.next.real_schedule import RealScheduling  # noqa: E402
from wall_in_one.ui.next.real_settings import RealSettings  # noqa: E402
from wall_in_one.ui.next.schedule_state import RuleView  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402
from wall_in_one.ui.window import MainWindow  # noqa: E402

FULL: Final = bool(os.environ.get(faults.FULL))
CONFIG: Final = frozenset({"config"})
STATE: Final = frozenset({"state"})
#: A removal writes the journal first, then moves the file.
REMOVAL: Final = frozenset({"state", "library"})
#: A download lands in the first library folder: the file, its record, the folder's marker.
LIBRARY: Final = frozenset({"library"})
#: How long the window runs sit idle through status polls (one every two seconds).
IDLE_SECONDS: Final = 6.5 if FULL else 2.2
#: The action a classic tile menu item activates (its handlers don't look at it).
MENU: Final = Gio.SimpleAction.new("tile-menu", None)


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


# -- the profile: built once by a real application, copied for each test -------------------------

_BUILT: dict[str, Path] = {}


def _writable(root: Path) -> None:
    """Whatever a test left read-only, so the tree can be removed."""
    for directory, _names, files in os.walk(root):
        os.chmod(directory, 0o700)
        for name in files:
            os.chmod(os.path.join(directory, name), 0o600)


@pytest.fixture
def profile(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ro.Profile]:
    """The healthy profile, fresh for this test, at the path its stores name.

    Built once (a real application, a dozen changes, their fsyncs) and copied
    for each test back to the same folder, since every store holds absolute
    paths. The downloaded wallpaper's ownership record is written again after
    the copy: it names its file's inode.
    """
    ro.skip_as_root()
    if "base" not in _BUILT:
        base = tmp_path_factory.mktemp("wio-probe-profile")
        ro.build(base / "live", monkeypatch)
        shutil.copytree(base / "live", base / "template", symlinks=True)
        _BUILT["base"] = base
    live = _BUILT["base"] / "live"
    if live.exists():
        _writable(live)
        shutil.rmtree(live)
    shutil.copytree(_BUILT["base"] / "template", live, symlinks=True)
    made = ro.Profile(live)
    ro.enter(made, monkeypatch)
    ro.rebind_download(made)
    yield made
    _writable(live)


# -- one change, on one surface ------------------------------------------------------------------


@dataclass
class Ctx:
    """What a gesture reaches: the application, its window, and what came back."""

    probe: ro.Probe
    window: Any
    replies: list[Response] = field(default_factory=list)
    #: What a gesture's own completion callbacks were given (a refused one: False).
    outcomes: list[object] = field(default_factory=list)

    @property
    def application(self) -> Application:
        return self.probe.application

    @property
    def profile(self) -> ro.Profile:
        return self.probe.profile

    def wid(self, name: str) -> str:
        return str(self.profile.wallpaper(name))

    def item(self, name: str) -> MediaItem:
        found = self.application.session.library.find(self.profile.wallpaper(name))
        assert found is not None, f"{name} is not in the library"
        return found

    def playlist(self, name: str) -> playlists.Playlist:
        found = self.application.session.playlists.by_name(name)
        assert found is not None, f"no playlist called {name}"
        return found

    def rule_id(self) -> str:
        return self.application.session.schedules.rules[0].id

    def ctl(self, verb: str, argument: str | None = None) -> None:
        """Dispatch one verb through the socket's table; the reply lands in `replies`."""
        self.replies.append(Response.failure("no reply yet", kind="probe-waiting"))

        def reply(response: Response) -> None:
            self.replies[:] = [response]

        server.dispatch(
            server.build_verb_table(_Commands(self.application)),
            Request(verb, argument).encode(),
            reply,
        )

    def answered(self) -> bool:
        return not self.replies or self.replies[0].kind != "probe-waiting"


@dataclass(frozen=True)
class Change:
    """One kind of change: what it has to write, and the gesture that asks for it."""

    name: str
    touches: frozenset[str]
    #: What it returns is not looked at: what it did is read from the files and what was said.
    gesture: Callable[[Ctx], object]
    #: Part of the bounded campaign the gate runs; the rest need `faults.FULL`.
    gate: bool = True
    #: What else has to be true before it is judged (work on a lane `quiet` doesn't see).
    ready: Callable[[Ctx], bool] | None = None


def pending_in(pages: dict[str, object]) -> list[str]:
    """Queued choices, held intents and "Saving…" a page still shows."""
    found: list[str] = []
    for name, page in pages.items():
        for attribute, value in vars(page).items():
            if isinstance(value, Gtk.Spinner):
                shown = value.get_spinning() and value.get_visible()
            elif isinstance(value, Gtk.Widget):
                shown = attribute == "_saving" and value.get_visible()
            else:
                held = attribute.startswith("_pending") or attribute.endswith("_intent")
                shown = bool(value) and (held or attribute in ("_saving", "_template_busy"))
            if shown:
                found.append(f"{name}.{attribute} still shows {value!r}")
    return found


def pages_of(window: object) -> dict[str, object]:
    if isinstance(window, NextWindow):
        return dict(window.pages)
    if isinstance(window, MainWindow):
        return {
            "playlists": window._playlists_page,
            "schedules": window._schedules_page,
            "settings": window._settings_page,
            "pairings": window._pairings_page,
        }
    return {}


def drive(ctx: Ctx, changes: Sequence[Change]) -> Iterator[Step]:
    """Try each change in turn; judge it once everything it started has finished."""
    probe = ctx.probe
    for change in changes:
        before = probe.snapshot()
        mark = probe.said.mark()
        ctx.replies.clear()
        ctx.outcomes.clear()
        try:
            change.gesture(ctx)
        except Exception as error:
            probe.uncaught.errors.append(f"the gesture itself raised {error!r}")
        yield patient(
            (
                f"everything {change.name!r} started to finish",
                _finished(ctx, change),
            ),
            ro.BOUND * 2,
        )
        # Whatever a completion left for the next turn of the loop (a report
        # from another thread's sink, a row put back, the rescan a download asks for).
        yield "one more turn of the loop", _after(0.05)
        yield patient((f"what {change.name!r} left queued", probe.quiet), ro.BOUND * 2)
        outcomes = list(ctx.outcomes)

        def pending(outcomes: list[object] = outcomes) -> list[str]:
            found = pending_in(pages_of(ctx.window))
            if len(outcomes) > 1:
                found.append(f"its completion callback ran {len(outcomes)} times: {outcomes}")
            return found

        probe.judge(
            change.name,
            before,
            mark,
            touches=change.touches,
            reply=ctx.replies[0] if ctx.replies else None,
            pending=pending,
        )


def _finished(ctx: Ctx, change: Change) -> Callable[[], bool]:
    """Every lane is idle, the reply (if one is due) arrived, and ``ui.toml`` isn't being saved."""

    def ready() -> bool:
        return (
            ctx.probe.quiet()
            and ctx.answered()
            and not _keeper_busy(ctx.window)
            and (change.ready is None or change.ready(ctx))
        )

    return ready


def _after(seconds: float) -> Callable[[], bool]:
    """A wait for ``seconds`` of main loop, as a step's check."""
    deadline: list[float] = []

    def ready() -> bool:
        if not deadline:
            deadline.append(time.monotonic() + seconds)
        return time.monotonic() >= deadline[0]

    return ready


def _keeper_busy(window: object) -> bool:
    """Whether ``ui.toml`` is being saved (its own worker, not the authoring lane)."""
    if isinstance(window, NextWindow):
        return bool(window._prefs.busy)
    if isinstance(window, MainWindow):
        return bool(window._settings_page._ui_prefs.busy)
    return False


def chosen(
    changes: Sequence[Change], case: str, known: frozenset[str] = frozenset()
) -> list[Change]:
    """The changes that have to write ``case``'s folder: the gate's handful, or all of them."""
    return [
        change
        for change in changes
        if case in change.touches and change.name not in known and (FULL or change.gate)
    ]


def _spill(changes: Sequence[Change], case: str) -> list[Change]:
    """What is tried in every case though it needn't write the read-only folder.

    A rescan, which writes nothing a person asked for and is judged by the
    invariants alone. And, with the state folder read-only, a settings change:
    it writes ``settings.toml`` only, so it is saved, though it can't be
    published; what it must not do is leave a hold behind (the publication
    that follows fails, and a quit waits for publications).
    """
    wanted = ("rescan the library",) if case != "state" else ("rescan the library", "switch")
    return [change for change in changes if change.name.endswith(wanted)]


def assert_each(probe: ro.Probe, tried: Sequence[Change]) -> None:
    """Refused where it had to write the read-only folder; else saved, and told what wasn't."""
    for change, attempt in zip(tried, probe.attempts, strict=True):
        if not change.touches:
            continue  # nothing of its own to save: the invariants are the whole of it
        if probe.case in change.touches:
            assert attempt.refused, attempt.describe()
        else:
            assert not attempt.refused, attempt.describe()
            assert any(ro.WHY.search(text) for text in attempt.said), (
                f"saved, but it couldn't be published, and nobody was told: {attempt.describe()}"
            )


def verdict(probe: ro.Probe) -> str:
    """Every violation, with what was said and what changed, for the failure message."""
    return "\n".join(a.describe() for a in probe.attempts if a.violations).replace(
        str(probe.profile.home), "<profile>"
    )


# -- the control socket's verbs --------------------------------------------------------------------


def _verb(
    name: str, touches: frozenset[str], argument: Callable[[Ctx], str], *, gate: bool = True
) -> Change:
    verb = name.split()[0]
    return Change(f"ctl {name}", touches, lambda ctx: ctx.ctl(verb, argument(ctx)), gate)


#: Every mutating verb in `server.build_verb_table`. ``download`` goes through the real
#: Wallhaven provider over a scripted transport (`_no_network`): no socket is opened.
CTL: Final = (
    _verb("shuffle", CONFIG, lambda c: "toggle"),
    _verb("cycle", CONFIG, lambda c: "toggle", gate=False),
    _verb("cycle-interval", CONFIG, lambda c: "777"),
    _verb("dynamics", CONFIG, lambda c: "toggle", gate=False),
    _verb("select", STATE, lambda c: c.wid("spare.png")),
    _verb("favourite", STATE, lambda c: c.wid("noon.png")),
    _verb("unfavourite", STATE, lambda c: c.wid("dawn.png"), gate=False),
    _verb("still", STATE, lambda c: f"{c.wid('rain.mp4')} :: {c.wid('dawn.png')}"),
    _verb("still default", STATE, lambda c: f"{c.wid('rain.mp4')} :: default", gate=False),
    _verb("palette", STATE, lambda c: f"{c.wid('noon.png')} keep"),
    _verb("reset-pairing", STATE, lambda c: c.wid("dawn.png"), gate=False),
    _verb("playlist-new", STATE, lambda c: "Night"),
    _verb("playlist-add", STATE, lambda c: f"Spare {c.wid('spare.png')}"),
    _verb(
        "playlist-remove",
        STATE,
        lambda c: f"Morning {c.playlist('Morning').entries[0].id}",
        gate=False,
    ),
    _verb("playlist-delete", STATE, lambda c: "Spare"),
    _verb("playlist-delete the default", STATE | CONFIG, lambda c: "Evening"),
    _verb("display-assign", STATE, lambda c: "DP-2 Morning"),
    _verb("display-clear", STATE, lambda c: "DP-1", gate=False),
    _verb("schedule-add", STATE, lambda c: "Spare days=sat"),
    _verb("schedule-name", STATE, lambda c: f"{c.rule_id()} Workdays", gate=False),
    _verb("schedule-remove", STATE, lambda c: c.rule_id()),
    _verb("remove", REMOVAL, lambda c: c.wid("spare.png")),
    _verb("remove a download", REMOVAL, lambda c: c.wid("Wall-in-One/Wallhaven/fetched.jpg")),
    _verb("download", LIBRARY, lambda c: "wallhaven ab1234"),
)

#: What these probes found, by case; each has its own strict expected failure below.
KNOWN_CTL: Final = {"config": frozenset({"ctl playlist-delete the default"})}


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wallhaven's API and one picture, answered from memory (`download_routes`)."""
    monkeypatch.setattr(http, "default_client", lambda: FakeClient(routes=download_routes()))


def start(
    profile: ro.Profile, case: str, monkeypatch: pytest.MonkeyPatch, *, service: bool = False
) -> Ctx:
    """An application over the profile as it is now, behind a recording window."""
    uncaught = ro.Uncaught()
    uncaught.watch(monkeypatch)
    _no_network(monkeypatch)
    if service:
        for name in ("set_wallpaper", "set_scheme", "set_mode", "reload_config", "apply_templates"):
            monkeypatch.setattr(noctalia, name, lambda *_a, **_k: None)
    application, window = ro.boot(profile, monkeypatch, service=service)
    said = ro.Said()
    said.follow_log(application)
    return Ctx(ro.Probe(profile, case, application, said, uncaught), window)


def run(ctx: Ctx, changes: Sequence[Change]) -> None:
    """`drive`, where no main loop is running: each wait spins it, bounded."""
    for label, ready in drive(ctx, changes):
        spin_until(ready, getattr(ready, "seconds", ro.BOUND), what=label)


@pytest.mark.parametrize("case", ro.CASES)
def test_ctl_refuses_what_a_read_only_folder_cannot_take(
    profile: ro.Profile, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Every verb that has to write the read-only folder: refused, saying why, nothing changed."""
    with ro.read_only(profile.folder(case)):
        ctx = start(profile, case, monkeypatch)
        try:
            tried = chosen(CTL, case, KNOWN_CTL.get(case, frozenset()))
            run(ctx, tried)
        finally:
            ro.close(ctx.application)
    assert tried and len(ctx.probe.attempts) == len(tried)
    print(ctx.probe.report().replace(str(profile.home), "<profile>"))
    assert not ctx.probe.violations(), verdict(ctx.probe)
    assert all(attempt.refused for attempt in ctx.probe.attempts)


def _alone(
    profile: ro.Profile,
    case: str,
    change: Change,
    monkeypatch: pytest.MonkeyPatch,
    *,
    service: bool = False,
    files_only: bool = False,
) -> ro.Probe:
    """One verb, alone, over a fresh profile with ``case``'s folder read-only.

    ``files_only``: the folder itself stays writable, so nothing says it must
    stay as it was; the probe is told no folder is read-only.
    """
    with ro.read_only(profile.folder(case), files_only=files_only):
        ctx = start(profile, "" if files_only else case, monkeypatch, service=service)
        try:
            run(ctx, [change])
        finally:
            ro.close(ctx.application)
    print(ctx.probe.report().replace(str(profile.home), "<profile>"))
    return ctx.probe


@ro.full_only
@pytest.mark.parametrize("service", [False, True], ids=["window", "service"])
@pytest.mark.parametrize("case", ro.CASES)
@pytest.mark.parametrize("change", CTL, ids=[change.name for change in CTL])
def test_every_ctl_verb_alone_with_a_read_only_folder(
    profile: ro.Profile, monkeypatch: pytest.MonkeyPatch, change: Change, case: str, service: bool
) -> None:
    """The whole table, each verb on a fresh profile, in a window's process and a service's.

    A verb that doesn't have to write the read-only folder goes through, and
    is judged all the same: nothing thrown, nothing held, what is shown is what
    is saved, no store naming what another lost.
    """
    if change.name in KNOWN_CTL.get(case, frozenset()):
        pytest.skip("pinned below, as an expected failure")
    probe = _alone(profile, case, change, monkeypatch, service=service)
    assert not probe.violations(), verdict(probe)
    assert probe.attempts[0].refused is (case in change.touches), probe.report()


@ro.full_only
@pytest.mark.parametrize("case", ro.CASES)
@pytest.mark.parametrize("change", CTL, ids=[change.name for change in CTL])
def test_every_ctl_verb_alone_with_only_the_files_read_only(
    profile: ro.Profile, monkeypatch: pytest.MonkeyPatch, change: Change, case: str
) -> None:
    """``chmod a-w`` on every file, the folders left writable.

    A store is replaced by renaming a new file over it, which a read-only
    file doesn't stop, so most changes go through here, and the replaced file
    comes back 0600. What stops one is its lock file, read-only too. Either
    way: all of it or none of it, and nothing stuck.
    """
    if change.name in KNOWN_CTL.get(case, frozenset()):
        pytest.skip("the same half-done delete, pinned below (the lock file is read-only too)")
    probe = _alone(profile, case, replace_touches(change), monkeypatch, files_only=True)
    assert not probe.violations(), verdict(probe)
    attempt = probe.attempts[0]
    assert attempt.said, "nothing was said either way"


def replace_touches(change: Change) -> Change:
    """``change`` with nothing it must be refused for: judged by the invariants only."""
    return Change(change.name, frozenset(), change.gesture, change.gate, change.ready)


def test_a_setting_runtime_toml_cannot_take_is_said_and_published_later(
    profile: ro.Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A settings verb with the state folder read-only: saved, not published, and where that shows.

    ``settings.toml`` takes the change and the verb's own reply is a plain
    success ("shuffle on"): it is made before the publication is tried
    (`_Commands._adopt_control_settings`). That ``runtime.toml`` couldn't be
    written is said in the window, kept as the publication problem (the new
    window's notice under its header) and is the reply to a later verb that
    needs the document (``playlist-use``). It is not in ``ctl status`` from a
    window's process, nor in a later settings verb's reply. Nothing publishes
    by itself once the folder is writable again; the next change does, and
    then the document holds both.
    """
    (shuffle,) = [change for change in CTL if change.name == "ctl shuffle"]
    document = paths.runtime_config_path()
    with contextlib.ExitStack() as writable_again:
        writable_again.enter_context(ro.read_only(profile.state))
        ctx = start(profile, "state", monkeypatch)
        application = ctx.application
        try:
            published = document.read_bytes()
            wanted = not application.settings.shuffle
            run(ctx, [shuffle])
            attempt = ctx.probe.attempts[0]
            assert not ctx.probe.violations(), verdict(ctx.probe)
            assert attempt.changed == {"config": ["~settings.toml"]}, "saved: settings.toml only"
            reply, *shown = attempt.said
            assert reply == f"shuffle {'on' if wanted else 'off'}", "the verb's own reply"
            assert any("runtime.toml" in text and ro.WHY.search(text) for text in shown), shown
            assert "runtime.toml" in application.publication_problem
            assert ro.WHY.search(application.publication_problem)
            status = ro.ctl(application, "status")
            assert status.kind == "runtime-not-running" and "runtime.toml" not in status.message
            later = ro.ctl(application, "playlist-use", "Morning")
            spin_until(ctx.probe.quiet, ro.BOUND, what="the later verb")
            assert not later.ok and "runtime.toml" in later.message, later
            assert ro.WHY.search(later.message), later
            assert document.read_bytes() == published

            writable_again.close()
            settle(2.5)  # more than a status poll: nothing retries the publication
            assert document.read_bytes() == published
            assert application.publication_problem, "still the problem, until a publication"
            again = ro.ctl(application, "cycle-interval", "777")
            spin_until(ctx.probe.quiet, ro.BOUND, what="the next change's publication")
            assert again.ok, again
            assert application.publication_problem == ""
            text = document.read_text(encoding="utf-8")
            assert f"shuffle = {str(wanted).lower()}" in text, "the earlier change, published now"
            assert "cycle_interval_seconds = 777" in text
            assert not ro.disagreements(application) and not ro.held(application)
        finally:
            ro.close(application)


# -- the windows: started with the folder read-only, every page opened, then closed ----------------


@dataclass
class Life:
    """What one whole run of the application showed, start to quit."""

    probe: ro.Probe
    #: Messages from the start until the application first went quiet.
    startup: list[str] = field(default_factory=list)
    #: Messages while it sat idle through status polls reporting a failed wallpaper,
    #: every time each was said (the log's own entries, so a toast isn't counted twice).
    idle: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    #: Whether the failed wallpaper's marker reached ``pairings.json`` (the polls' own write).
    marker_saved: bool = False
    seconds_to_start: float = 0.0
    seconds_to_quit: float = 0.0


def _runtime(monkeypatch: pytest.MonkeyPatch) -> FakeRuntime:
    """A scripted runtime and no Noctalia (`tests.test_ui_next_window.sandboxed_runtime`)."""
    fake = FakeRuntime()
    monkeypatch.setattr(client, "send_runtime", fake.send_runtime)

    def no_shell(*_arguments: object, **_keywords: object) -> str:
        raise noctalia.NoctaliaError("noctalia is not reachable from this test")

    monkeypatch.setattr(noctalia, "_run", no_shell)
    paths.runtime_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    return fake


def _failed_wallpaper(application: Application) -> dict[str, object] | None:
    """A status in which the runtime couldn't play Evening's first wallpaper.

    Each poll that carries it asks the health lane to save a marker in
    ``pairings.json`` (`Application._queue_runtime_health`). None once
    Evening is gone.
    """
    evening = application.session.playlists.by_name("Evening")
    if evening is None or not evening.entries:
        return None
    return {
        "config_generation": runtime_config.read_config_generation(),
        "config_path": str(paths.runtime_config_path().absolute()),
        "runtime_instance": "a" * runtime_health.RUNTIME_INSTANCE_HEX_CHARS,
        "config_epoch": 1,
        "playlist_id": evening.id,
        "playlist": evening.name,
        "source": "schedule",
        "taboo_entries": [
            {
                "playlist_id": evening.id,
                "entry_id": evening.entries[0].id,
                "reason": "renderer rejected this wallpaper",
                "source": "automatic-apply",
                "durable": False,
                "observed_config_epoch": 1,
            }
        ],
        "taboo_entries_omitted": 0,
    }


def _marker_saved() -> bool:
    """Whether ``pairings.json`` now says a wallpaper couldn't be played."""
    return any(record.health.is_borked for record in pairings.Store.open().records.values())


def _hear_toasts(window: object, said: ro.Said, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every toast the window shows, whoever asked for it."""
    if isinstance(window, NextWindow):
        window.state.connect("toast", lambda _state, text, _button: said.hear(text))
        overlay = window.toasts
    else:
        assert isinstance(window, MainWindow)
        overlay = window._toast
    added = overlay.add_toast

    def add_toast(toast: Adw.Toast) -> None:
        said.hear(toast.get_title() or "")
        added(toast)

    monkeypatch.setattr(overlay, "add_toast", add_toast)


#: `show_page` names; the classic window has no page of its own for displays.
PAGES: Final = ("media", "browse", "playlists", "schedules", "settings")
WALLPAPERS: Final = len(ro.WALLPAPERS) - 1 + len(ro.VIDEOS) + 1  # gone.png went; one download


def live(
    profile: ro.Profile,
    case: str,
    ui: str,
    monkeypatch: pytest.MonkeyPatch,
    changes: Sequence[Change],
    *,
    service: bool = False,
    idle: float = IDLE_SECONDS,
    start_within: float = ro.BOUND * 3,
    files_too: bool = False,
) -> Life:
    """Run the real application over ``profile`` with ``case``'s folder read-only.

    Started with the folder already read-only; every page opened; then it
    sits through status polls that report a failed wallpaper (before any
    change, while the published document is still the current one, or the
    health lane has nothing to save); each change tried and judged; then the
    window is closed (a ``--service`` process is told
    ``ctl quit`` over its real socket instead) with the folder still
    read-only. The run itself is bounded (`run_application`), so a start that
    loops or a quit that never returns fails here naming what it waited for.
    """
    runtime = _runtime(monkeypatch)
    _no_network(monkeypatch)
    if service:
        # A resident process owns the compatibility renderer, which sets the wallpaper.
        for name in ("set_wallpaper", "set_scheme", "set_mode", "reload_config", "apply_templates"):
            monkeypatch.setattr(noctalia, name, lambda *_a, **_k: None)
    uncaught = ro.Uncaught()
    uncaught.watch(monkeypatch)
    lanes: list[ThreadPoolExecutor] = []
    requests = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ctl-client")
    answers: list[Future[Response]] = []
    began = time.monotonic()
    quitting: list[float] = []
    with ro.read_only(profile.folder(case), files_too=files_too):
        application = Application(service=service, ui=ui)  # type: ignore[arg-type]
        said = ro.Said()
        said.follow_log(application)
        probe = ro.Probe(profile, case, application, said, uncaught)
        life = Life(probe)

        def scenario() -> Iterator[Step]:
            window = application._window
            assert isinstance(window, NextWindow if ui == "next" else MainWindow)
            _hear_toasts(window, said, monkeypatch)
            yield first_frame_step(window)
            started = _started_within(application, probe, start_within)
            yield patient(
                ("the start to finish: the older-data check, the repair, the first scan", started),
                start_within + ro.BOUND,
            )
            yield "one more turn of the loop", _after(0.3)
            life.seconds_to_start = time.monotonic() - began
            life.startup = said.since(0)
            life.problems.extend(uncaught.errors)
            uncaught.errors.clear()
            if not application.authoring_ready:
                life.problems.append(
                    f"changes are not open after the start: {application._authoring_gate_state}"
                )
            found = len(application.session.library.items)
            if found != WALLPAPERS:
                life.problems.append(f"the library holds {found} wallpapers, not {WALLPAPERS}")
            for page in (*PAGES, "displays") if ui == "next" else PAGES:
                assert window.show_page(page) is True, page
                yield f"the {page} page to open", probe.quiet
            life.problems.extend(f"opening the pages: {error}" for error in uncaught.errors)
            uncaught.errors.clear()

            logged = len(application.activity_log.entries)
            if idle:
                runtime.answer(_failed_wallpaper(application))
                application.refresh_runtime_status_async()
                yield patient(("status polls reporting a failed wallpaper", _after(idle)), 30)
                runtime.answer(None)
                yield patient(("the last poll's work to finish", probe.quiet), ro.BOUND * 2)
            life.idle = [
                entry.text
                for entry in application.activity_log.entries[logged:]
                if entry.source == "app"
            ]
            life.marker_saved = _marker_saved()
            life.problems.extend(f"while idle: {error}" for error in uncaught.errors)
            uncaught.errors.clear()
            differing = ro.disagreements(application)
            if differing:
                life.problems.append(f"after the polls, shown differs from saved: {differing}")

            yield from drive(Ctx(probe, window), changes)

            still = ro.held(application)
            if still:
                life.problems.append(f"still held before the quit: {still}")
            lanes.extend(application_lanes(application))
            quitting.append(time.monotonic())
            if service:
                answers.append(control(requests, "quit"))
            else:
                window.close()

        def launch() -> bool:
            application.activate()  # a resident process isn't activated by starting it
            return GLib.SOURCE_REMOVE

        if service:
            GLib.idle_add(launch)
        try:
            status = run_application(application, scenario())
            life.seconds_to_quit = time.monotonic() - quitting[0]
            if status != 0:
                life.problems.append(f"the application exited with {status}")
            if answers and not answers[0].result(timeout=ro.BOUND).ok:
                life.problems.append(f"ctl quit failed: {answers[0].result().message}")
            if application._window is not None:
                life.problems.append("the window outlived the quit")
            life.problems.extend(f"at the quit: {error}" for error in uncaught.errors)
        finally:
            requests.shutdown(wait=True)
            for lane in lanes:
                lane.shutdown(wait=True)
    return life


def gate_settled(application: Application) -> bool:
    """The older-data check and the repair have an answer, whichever it is."""
    return application._authoring_gate_state != "checking"


def _started_within(
    application: Application, probe: ro.Probe, seconds: float
) -> Callable[[], bool]:
    """The start's work is done and has an answer; or, quiet for ``seconds`` without one.

    The second is not a pass: `live` then finds changes not open and says so.
    """
    waited = _after(seconds)

    def ready() -> bool:
        if not probe.quiet():
            return False
        return application.authoring_ready or gate_settled(application) or waited()

    return ready


def repeats(messages: Sequence[str]) -> dict[str, int]:
    """Messages said more than once, with temporary names (which differ each time) folded."""
    counts: dict[str, int] = {}
    for message in messages:
        folded = ro.TEMPORARY_NAME.sub("<temporary>", message)
        counts[folded] = counts.get(folded, 0) + 1
    return {message: count for message, count in counts.items() if count > 1}


def assert_lived(life: Life, tried: Sequence[Change], *, polled: bool = True) -> None:
    """The run started, judged each change clean, sat through the polls and quit."""
    probe = life.probe
    hide = str(probe.profile.home)
    assert not life.problems, "\n".join(life.problems).replace(hide, "<profile>")
    # One refused publication at the start is one message; a resident process
    # publishes once headless and once for its first window.
    assert len(life.startup) <= 3, f"the start said {len(life.startup)} things: {life.startup}"
    assert max(repeats(life.startup).values(), default=1) <= 2, repeats(life.startup)
    assert len(probe.attempts) == len(tried)
    assert not probe.violations(), verdict(probe)
    if polled:
        # The polls' own write: saved where the state folder takes it (so the
        # polls did ask for one) with nothing to say; refused, saying why,
        # where it can't. How often it is said is pinned on its own below.
        assert life.marker_saved is (probe.case != "state"), life.idle
        if probe.case == "state":
            assert life.idle and all(ro.WHY.search(text) for text in life.idle), life.idle
        else:
            assert not life.idle, life.idle


# -- the new window ------------------------------------------------------------------------------


def _settings(ctx: Ctx) -> RealSettings:
    access = ctx.window.state.settings_access
    assert isinstance(access, RealSettings)
    return access


def _lists(ctx: Ctx) -> RealPlaylists:
    part = ctx.window.state.playlist_editing
    assert isinstance(part, RealPlaylists)
    return part


def _scheduling(ctx: Ctx) -> RealScheduling:
    part = ctx.window.state.scheduling
    assert isinstance(part, RealScheduling)
    return part


def _editing(ctx: Ctx) -> RealEditing:
    part = ctx.window.state.editing
    assert isinstance(part, RealEditing)
    return part


def _other_scheme(ctx: Ctx) -> str:
    return next(s for s in noctalia.ALL_SCHEMES if s != ctx.application.settings.preview_scheme)


def _rescan(ctx: Ctx) -> None:
    folders = ctx.window.state.library_folders
    assert folders is not None
    folders.rescan()


NEXT: Final = (
    Change("rescan the library", frozenset(), _rescan),
    # -- settings.toml, ui.toml and the key: the configuration folder -------------------
    Change(
        "a settings switch",
        CONFIG,
        lambda c: _settings(c).change_settings(shuffle=not c.application.settings.shuffle),
    ),
    Change(
        "the default scheme",
        CONFIG,
        lambda c: _settings(c).change_settings(preview_scheme=_other_scheme(c)),
        gate=False,
    ),
    Change("add a library folder", CONFIG, lambda c: _settings(c).add_folder(str(c.profile.other))),
    Change(
        "remove a library folder",
        CONFIG,
        lambda c: _settings(c).remove_folder(str(c.profile.library)),
        gate=False,
    ),
    Change(
        "make a playlist the default",
        CONFIG,
        lambda c: _lists(c).make_default(c.playlist("Morning").id),
    ),
    Change(
        "the Schedule page's default playlist",
        CONFIG,
        lambda c: _scheduling(c).choose_default_playlist(
            c.playlist("Morning").id, c.outcomes.append
        ),
    ),
    Change(
        "the display mode",
        CONFIG,
        lambda c: _scheduling(c).choose_display_mode("independent", c.outcomes.append),
        gate=False,
    ),
    Change("the window style", CONFIG, lambda c: c.window.state.set_window_style("translucent")),
    Change(
        "the thumbnail size",
        CONFIG,
        lambda c: c.window.state.set_thumbnail_size("large"),
        gate=False,
    ),
    Change(
        "save a Wallhaven key",
        CONFIG,
        lambda c: _settings(c).save_key("b" * 32, c.outcomes.append),
    ),
    Change("clear the Wallhaven key", CONFIG, lambda c: _settings(c).clear_key()),
    Change(
        "delete the default playlist",
        STATE | CONFIG,
        lambda c: _lists(c).delete_playlist(c.playlist("Evening").id),
    ),
    # -- the stores: the state folder ----------------------------------------------------
    Change(
        "create a playlist",
        STATE,
        lambda c: _lists(c).create_playlist("Night", [c.wid("dawn.png")]) and None,
    ),
    Change(
        "rename a playlist",
        STATE,
        lambda c: _lists(c).rename_playlist(c.playlist("Spare").id, "Reserve"),
    ),
    Change("delete a playlist", STATE, lambda c: _lists(c).delete_playlist(c.playlist("Spare").id)),
    Change(
        "add a playlist entry",
        STATE,
        lambda c: _lists(c).add_entries(c.playlist("Spare").id, [c.wid("spare.png")]),
    ),
    Change(
        "remove a playlist entry",
        STATE,
        lambda c: _lists(c).remove_entries(
            c.playlist("Morning").id, [c.playlist("Morning").entries[0].id]
        ),
        gate=False,
    ),
    Change(
        "reorder a playlist",
        STATE,
        lambda c: _lists(c).move_entry(
            c.playlist("Morning").id, c.playlist("Morning").entries[0].id, 1
        ),
    ),
    Change(
        "a playlist's interval",
        STATE,
        lambda c: _lists(c).set_playlist_interval(c.playlist("Morning").id, 600),
        gate=False,
    ),
    Change(
        "a playlist's shuffle",
        STATE,
        lambda c: _lists(c).set_playlist_shuffle(c.playlist("Morning").id, True),
        gate=False,
    ),
    Change("a favourite", STATE, lambda c: c.window.state.toggle_favorite(c.wid("noon.png"))),
    Change(
        "a wallpaper's colours",
        STATE,
        lambda c: _editing(c).set_wallpaper_colors(c.wid("noon.png"), mode="keep"),
    ),
    Change(
        "choose a still",
        STATE,
        lambda c: _editing(c).choose_still(c.wid("rain.mp4"), c.wid("dawn.png")),
    ),
    Change(
        "reset a wallpaper",
        STATE,
        lambda c: _editing(c).reset_wallpaper(c.wid("dawn.png")),
        gate=False,
    ),
    Change(
        "save a schedule rule",
        STATE,
        lambda c: _scheduling(c).save_rule(
            "", RuleView("", c.playlist("Spare").id, weekdays=frozenset({5})), c.outcomes.append
        ),
    ),
    Change(
        "switch a schedule rule off",
        STATE,
        lambda c: _scheduling(c).enable_rule(c.rule_id(), False, c.outcomes.append),
    ),
    Change(
        "delete a schedule rule",
        STATE,
        lambda c: _scheduling(c).remove_rule(c.rule_id(), c.outcomes.append),
        gate=False,
    ),
    Change(
        "a display's own playlist",
        STATE,
        lambda c: _scheduling(c).assign_display(
            "DP-2", c.playlist("Morning").id, c.outcomes.append
        ),
    ),
    # -- the files themselves: the library, after the journal in the state folder ------------
    Change(
        "move a wallpaper to the trash",
        REMOVAL,
        lambda c: _editing(c).remove_wallpapers([c.wid("spare.png")]) and None,
    ),
    Change(
        "delete a downloaded wallpaper",
        REMOVAL,
        lambda c: (
            _editing(c).remove_wallpapers([c.wid("Wall-in-One/Wallhaven/fetched.jpg")]) and None
        ),
    ),
)

KNOWN_NEXT: Final = {"config": frozenset({"delete the default playlist"})}


@pytest.mark.parametrize("case", ro.CASES)
def test_the_new_window_lives_with_a_read_only_folder(
    profile: ro.Profile, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    tried = [*chosen(NEXT, case, KNOWN_NEXT.get(case, frozenset())), *_spill(NEXT, case)]
    life = live(profile, case, "next", monkeypatch, tried)
    print(_story(life))
    assert_lived(life, tried)
    assert_each(life.probe, tried)


def _story(life: Life) -> str:
    """The whole run, for ``-s`` and for a failure: what was said when, and how long it took."""
    hide = str(life.probe.profile.home)
    lines = [
        f"start {life.seconds_to_start:.1f}s, quit {life.seconds_to_quit:.1f}s",
        f"startup said: {life.startup}",
        f"idle said ({len(life.idle)}): {life.idle}",
        f"problems: {life.problems}",
        life.probe.report(),
    ]
    return "\n".join(lines).replace(hide, "<profile>")


# -- the classic window ----------------------------------------------------------------------------


def _selected(ctx: Ctx, name: str) -> Any:
    """The classic Playlists page with ``name`` selected, as a click on its row leaves it."""
    page = ctx.window._playlists_page
    page._selected = ctx.playlist(name).id
    return page


def _choice_row(ctx: Ctx, first: str, wanted: str) -> tuple[Adw.ComboRow, tuple[Any, ...]]:
    """A playlist row as the classic Schedules page builds one, with ``wanted`` chosen."""
    choices = ctx.application.session.playlists.all()
    row = Adw.ComboRow(model=Gtk.StringList.new([first, *(p.name for p in choices)]))
    row.set_selected([p.name for p in choices].index(wanted) + 1)
    return row, choices


def _classic_default(ctx: Ctx) -> None:
    row, choices = _choice_row(ctx, "All wallpapers", "Morning")
    ctx.window._schedules_page._make_default_changed(choices)(row, None)


def _classic_assign(ctx: Ctx) -> None:
    row, choices = _choice_row(ctx, "Default", "Morning")
    ctx.window._schedules_page._make_display_changed("DP-2", choices)(row, None)


def _classic_rule(ctx: Ctx) -> None:
    page = ctx.window._schedules_page
    choices = ctx.application.session.playlists.all()
    page._rule_playlist.set_selected([p.name for p in choices].index("Spare"))
    page._weekdays[5].set_active(True)
    page._rule_commit.emit("clicked")


def _classic_key(ctx: Ctx) -> None:
    page = ctx.window._settings_page
    page._api_key_entry.set_text("b" * 32)
    page._on_save_api_key(page._save_api_key)


def _classic_create(ctx: Ctx) -> None:
    page = ctx.window._playlists_page
    page._new_name.set_text("Night")
    page._create()


def _classic_scheme(ctx: Ctx) -> None:
    row = ctx.window._settings_page._scheme
    row.set_selected((row.get_selected() + 1) % len(noctalia.ALL_SCHEMES))


def _flip(row: Any) -> None:
    row.set_active(not row.get_active())


CLASSIC: Final = (
    Change("rescan the library", frozenset(), lambda c: c.application.refresh_library()),
    Change("a settings switch", CONFIG, lambda c: _flip(c.window._settings_page._shuffle)),
    Change("the default scheme", CONFIG, _classic_scheme, gate=False),
    Change(
        "add a library folder",
        CONFIG,
        lambda c: (
            c.window._settings_page._edit_roots(config.RootsEdit("add", c.profile.other)) and None
        ),
    ),
    Change("make a playlist the default", CONFIG, lambda c: _selected(c, "Morning")._set_default()),
    Change("the Schedules page's default playlist", CONFIG, _classic_default),
    Change("the next start's renderer", CONFIG, lambda c: _flip(c.window._settings_page._gpu)),
    Change("save a Wallhaven key", CONFIG, _classic_key),
    Change(
        "clear the Wallhaven key",
        CONFIG,
        lambda c: c.window._settings_page._on_clear_api_key(c.window._settings_page._clear_api_key),
    ),
    Change(
        "delete the default playlist",
        STATE | CONFIG,
        lambda c: c.window._playlists_page._delete_confirmed(c.playlist("Evening").id),
    ),
    Change("create a playlist", STATE, _classic_create),
    Change("rename a playlist", STATE, lambda c: _selected(c, "Spare")._rename("Reserve")),
    Change(
        "delete a playlist",
        STATE,
        lambda c: c.window._playlists_page._delete_confirmed(c.playlist("Spare").id),
    ),
    Change(
        "add a playlist entry",
        STATE,
        lambda c: _selected(c, "Spare")._make_add(c.item("spare.png"))(Gtk.Button()),
    ),
    Change(
        "remove a playlist entry",
        STATE,
        lambda c: _selected(c, "Morning")._make_remove(c.playlist("Morning").entries[0].id)(
            Gtk.Button()
        ),
        gate=False,
    ),
    Change(
        "reorder a playlist",
        STATE,
        lambda c: _selected(c, "Morning")._make_move(c.playlist("Morning").entries[0].id, 1)(
            Gtk.Button()
        ),
    ),
    Change("a favourite", STATE, lambda c: c.window._on_favourite(c.item("noon.png"), True)),
    Change(
        "a wallpaper's colours",
        STATE,
        lambda c: c.window._on_palette_path(
            MENU, GLib.Variant("(ss)", (c.wid("noon.png"), "keep"))
        ),
    ),
    Change(
        "choose a still",
        STATE,
        lambda c: c.window._store_still(c.item("rain.mp4"), c.profile.wallpaper("dawn.png")),
    ),
    Change(
        "reset a wallpaper",
        STATE,
        lambda c: c.window._on_reset_pairing(MENU, GLib.Variant("s", c.wid("dawn.png"))),
        gate=False,
    ),
    Change("save a schedule rule", STATE, _classic_rule),
    Change(
        "switch a schedule rule off",
        STATE,
        lambda c: c.window._schedules_page._make_enabled(c.rule_id())(
            Adw.SwitchRow(active=False), None
        ),
    ),
    Change(
        "delete a schedule rule",
        STATE,
        lambda c: c.window._schedules_page._make_remove(c.rule_id())(Gtk.Button()),
        gate=False,
    ),
    Change("a display's own playlist", STATE, _classic_assign),
    Change(
        "move a wallpaper to the trash",
        REMOVAL,
        lambda c: c.window._trash(c.item("spare.png")),
    ),
    Change(
        "delete a downloaded wallpaper",
        REMOVAL,
        lambda c: c.window._on_remove_confirmed(
            None, "remove", c.item("Wall-in-One/Wallhaven/fetched.jpg")
        ),
    ),
)

KNOWN_CLASSIC: Final = {"config": frozenset({"delete the default playlist"})}


@pytest.mark.parametrize("case", ro.CASES)
def test_the_classic_window_lives_with_a_read_only_folder(
    profile: ro.Profile, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """A resident (``--service``) process's classic window; it ends by ``ctl quit``.

    Over the real control socket, the window still open and the folder still
    read-only: the quit that waits for every accepted tail (`request_quit`).
    The status polls are the new window's test's here; both sit through them
    in the full campaign.
    """
    tried = [*chosen(CLASSIC, case, KNOWN_CLASSIC.get(case, frozenset())), *_spill(CLASSIC, case)]
    life = live(profile, case, "classic", monkeypatch, tried, service=True, idle=0)
    print(_story(life))
    assert_lived(life, tried, polled=False)
    assert_each(life.probe, tried)
    assert life.seconds_to_quit < ro.BOUND


@ro.full_only
@pytest.mark.parametrize("case", ro.CASES)
@pytest.mark.parametrize("ui", ["next", "classic"])
def test_each_window_the_other_way_out_with_a_read_only_folder(
    profile: ro.Profile, monkeypatch: pytest.MonkeyPatch, ui: str, case: str
) -> None:
    """The new window resident and told ``ctl quit``; the classic one closed, after the polls."""
    table, known = (NEXT, KNOWN_NEXT) if ui == "next" else (CLASSIC, KNOWN_CLASSIC)
    tried = [*chosen(table, case, known.get(case, frozenset())), *_spill(table, case)]
    life = live(profile, case, ui, monkeypatch, tried, service=ui == "next")
    print(_story(life))
    assert_lived(life, tried)
    assert_each(life.probe, tried)
    assert life.seconds_to_quit < ro.BOUND


# -- found, and fixed: deleting the saved default playlist while settings.toml can't be written ----
#
# With the configuration folder read-only, deleting the playlist that is the
# saved default took it out of playlists.json (and its display assignment out of
# displays.json) and then could not clear active_playlist in settings.toml. The
# settings named a playlist that was gone, every later publication was refused,
# and the next start could not repair it: the gate rested at "repair" and the
# library was never loaded. Since 6a6a8b3 the delete is refused whole, and a
# start over such a default opens (57008a4, 7e55112).


def _delete_the_default(surface: str) -> Change:
    table = {"ctl": CTL, "next": NEXT, "classic": CLASSIC}[surface]
    (change,) = [c for c in table if "delete" in c.name and "the default" in c.name]
    return change


@pytest.mark.parametrize(
    "surface",
    [
        "ctl",
        *ro.also_in_full("next", "classic"),
    ],
)
def test_deleting_the_default_playlist_with_read_only_settings_is_all_or_nothing(
    profile: ro.Profile, monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    change = _delete_the_default(surface)
    if surface == "ctl":
        with ro.read_only(profile.config):
            ctx = start(profile, "config", monkeypatch)
            try:
                run(ctx, [change])
            finally:
                ro.close(ctx.application)
        probe = ctx.probe
    else:
        probe = live(profile, "config", surface, monkeypatch, [change], idle=0).probe
    assert not probe.violations(), verdict(probe)


def test_the_start_after_that_delete_still_loads_the_library(
    profile: ro.Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    with ro.read_only(profile.config):
        ctx = start(profile, "config", monkeypatch)
        try:
            run(ctx, [_delete_the_default("ctl")])
        finally:
            ro.close(ctx.application)
        life = live(profile, "config", "next", monkeypatch, [], idle=0)
    print(_story(life))
    assert not life.problems, life.problems


# -- found, lower severity: a refusal that gives the wrong reason, or none -------------------------


def test_a_template_install_the_state_folder_refuses_says_why(
    profile: ro.Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FINDING (lower severity: the wrong reason).

    With the state folder read-only, Settings' Install for the palette
    template is refused and changes nothing, but says "cannot verify palette
    template <state>/palette-<digest>.json.tmpl: [Errno 2] No such file or
    directory": the template could not be written there, and the verification
    that follows reports the file it didn't find (theme/template.py:2704)
    instead of the folder it couldn't write.
    """
    monkeypatch.setattr(noctalia, "reload_config", lambda: None)
    monkeypatch.setattr(noctalia, "apply_templates", lambda: None)
    _noctalia_settings(NOCTALIA)
    tried = [
        Change(
            "read the template's state",
            frozenset(),
            lambda c: _settings(c).check_template(),
            ready=lambda c: _settings(c).template_status().action == "Install",
        ),
        Change("install the palette template", STATE, lambda c: _settings(c).install_template()),
    ]
    life = live(profile, "state", "next", monkeypatch, tried, idle=0)
    print(_story(life))
    assert not life.problems, life.problems
    install = life.probe.attempts[1]
    assert install.refused and not install.changed, install.describe()
    assert "weren't changed" in " ".join(install.said), "it did reach the install"
    assert not life.probe.violations(), verdict(life.probe)


SILENT_STILL: Final = (
    "FINDING (lower severity: silence). A video with no still in a read-only library never "
    "gets one, and nothing says so: not a toast, not the log. Where the library is writable "
    "the same start writes Wall-in-One/Automatic Stills/video-<id>.png and its sidecar. "
    "`StillMaker._run` (ui/stills.py:196) relies on `stills.ensure` swallowing its own "
    "failures, so a folder that can't be written looks like a video ffmpeg can't read"
)


def _clip(folder: Path) -> Path:
    """A real one-second video, so a still can be captured from it."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("no ffmpeg to make a video with")
    clip = folder / "clip.mp4"
    source = "color=c=teal:s=64x36:d=1"
    subprocess.run(
        [ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", source, "-pix_fmt", "yuv420p", clip],
        check=True,
        timeout=60,
    )
    return clip


def _made_stills(profile: ro.Profile) -> list[Path]:
    return sorted(profile.library.glob("Wall-in-One/Automatic Stills/*.png"))


def _start_and_make_stills(
    profile: ro.Profile, case: str, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Start with ``case``'s folder read-only, wait out the still maker; everything said."""
    with ro.read_only(profile.folder(case)):
        application, window = ro.boot(profile, monkeypatch, stills=True)
        try:
            for _round in range(2):  # a still that was made asks for one more scan
                spin_until(
                    lambda: not application._stills._pending and ro.quiet(application),
                    ro.BOUND * 2,
                    what="the still maker, and the scan after it",
                )
                settle(0.3)
            assert application.session.library.find(profile.wallpaper("clip.mp4")) is not None
            return [*(entry.text for entry in application.activity_log.entries), *window.reports]
        finally:
            ro.close(application)


def test_a_writable_library_gets_the_videos_still_at_the_start(
    profile: ro.Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the next test: the same start, the library writable."""
    _clip(profile.library)
    _start_and_make_stills(profile, "config", monkeypatch)
    assert len(_made_stills(profile)) == 1, "the start made the video's still"


@ro.pinned(SILENT_STILL)
def test_a_still_a_read_only_library_cannot_take_is_said(
    profile: ro.Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clip(profile.library)
    before = ro.Snapshot.take(profile)
    said = _start_and_make_stills(profile, "library", monkeypatch)
    assert before.changes(ro.Snapshot.take(profile)).get("library") is None, "nothing was written"
    assert _made_stills(profile) == []
    assert any(ro.WHY.search(text) for text in said), f"no still, and nothing said: {said}"


def _leftover(folder: Path) -> Path:
    """An empty deletion record, as a finished discard leaves one (`tidy`'s ``leftovers``)."""
    retained = folder / ".wall-in-one-retained"
    retained.mkdir(mode=0o700, exist_ok=True)
    entry = retained / ("entry-" + "ab" * 16)
    entry.write_bytes(b"")
    return entry


def test_tidy_up_says_why_a_leftover_in_a_read_only_folder_stayed(profile: ro.Profile) -> None:
    """FINDING (lower severity: the wrong reason).

    Tidy up's Archive, with the state folder read-only and a leftover in it:
    the leftover stays, correctly, and the result says it "changed meanwhile
    and stayed where it was". It didn't change; its folder is read-only. Every
    item not moved is counted as changed meanwhile (tidy.py:1399). Through
    `tidy.plan` and `tidy.apply`, as Settings' Tidy up section calls them.
    """
    stays, goes = _leftover(profile.state), _leftover(profile.config)
    roots = (profile.library,)
    preview = tidy.plan(roots=roots).action(tidy.LEFTOVERS)
    assert {change.path for change in preview.changes} == {stays, goes}
    with ro.read_only(profile.state):
        result = tidy.apply(tidy.LEFTOVERS, preview, roots=roots)
    assert stays.exists() and not goes.exists(), "each leftover is moved whole, or not at all"
    assert "changed meanwhile" not in result.message, result.message
    assert ro.WHY.search(result.message), result.message


def test_tidy_up_refuses_whole_when_its_archive_cannot_be_made(profile: ro.Profile) -> None:
    """The archive lives in the configuration folder: read-only, nothing moves, and it says so."""
    first, second = _leftover(profile.state), _leftover(profile.config)
    roots = (profile.library,)
    preview = tidy.plan(roots=roots).action(tidy.LEFTOVERS)
    with ro.read_only(profile.config):
        before = ro.Snapshot.take(profile)
        with pytest.raises(OSError, match="Permission denied"):
            tidy.apply(tidy.LEFTOVERS, preview, roots=roots)
        assert not before.changes(ro.Snapshot.take(profile))
    assert first.exists() and second.exists()
    # Then, writable: archived; and an Undo the read-only folder refuses puts nothing back.
    tidy.apply(tidy.LEFTOVERS, tidy.plan(roots=roots).action(tidy.LEFTOVERS), roots=roots)
    assert not first.exists() and not second.exists()
    with ro.read_only(profile.config):
        archived = ro.Snapshot.take(profile)
        with pytest.raises(OSError, match="Permission denied"):
            tidy.undo(tidy.LEFTOVERS)
        assert not archived.changes(ro.Snapshot.take(profile))
    assert tidy.undo(tidy.LEFTOVERS).changed
    assert first.exists() and second.exists()


# -- found: a health save the state folder refuses is said again on every status poll -------------

SAID_EVERY_POLL: Final = (
    "FINDING (the same failure, said without end). The state folder read-only and the "
    "runtime reporting a wallpaper it couldn't play: every status poll (one each two seconds) "
    "asks the health lane to save the marker in pairings.json, the save is refused, and "
    "'Could not save wallpaper playback availability: local-io: could not write "
    ".../pairings.json: Permission denied' is toasted and logged again, for as long as the "
    "runtime keeps reporting it. `_ingest_runtime_health` returns the error for a "
    "`PairingError` (ui/app.py:4792) and `_apply_runtime_health_result` reports whatever "
    "error it is given (ui/app.py:4820); nothing remembers that this one was said"
)


@ro.pinned(SAID_EVERY_POLL)
def test_a_health_save_the_state_folder_refuses_is_said_once(
    profile: ro.Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three status polls carrying the same failed wallpaper, the published document current."""
    with ro.read_only(profile.state):
        ctx = start(profile, "state", monkeypatch)
        application = ctx.application
        try:
            status = json.dumps(_failed_wallpaper(application))

            def runtime(verb: str, *_arguments: object, **_keywords: object) -> Response:
                return Response.success(status) if verb == "status" else Response.success()

            monkeypatch.setattr(client, "send_runtime", runtime)
            mark = ctx.probe.said.mark()
            for _poll in range(3):
                assert application.refresh_runtime_status_async()
                spin_until(
                    lambda: not application._runtime_status_pending and ctx.probe.quiet(),
                    ro.BOUND,
                    what="a status poll and the health save it asks for",
                )
                settle(0.05)
            said = ctx.probe.said.texts[mark:]
            assert not ro.held(application) and not ro.disagreements(application)
        finally:
            ro.close(application)
    assert not _marker_saved(), "refused: nothing reached pairings.json"
    assert said and all(ro.WHY.search(text) for text in said), "refused, saying why"
    assert len(said) == 1, f"three polls, and it was said {len(said)} times"


# -- found: a start that cannot take the migration lock stays "checking" for good ------------------

STUCK_CHECKING: Final = (
    "FINDING (wedged at the start). The state folder read-only in a way that keeps its "
    ".legacy-migration-v1.json.mutation.lock from being opened for writing: every file in it "
    "read-only too, which is what `chmod -R a-w` leaves and what a read-only mount amounts "
    "to; or the lock file not there (a profile copied without its dot files). The start's "
    "repair task is given to the authoring lane, whose guard can't take the lock "
    "(`guarded_work`, ui/app.py:3268). The failure is said once ('MigrationError: cannot lock "
    "the legacy migration transaction: ... Permission denied') and nothing rests the gate "
    "(`_open_authoring_after_migration`'s `replied`, ui/app.py:808, only reports): it stays "
    "'checking' with the repair marked started, so the library is never loaded, and every "
    "change, ctl's too, is answered 'current-profile authoring is still opening; retry "
    "shortly' until a restart, which does the same. With the folders read-only and the lock "
    "file openable, the same start loads the library and refuses each write with its reason"
)


@ro.pinned(STUCK_CHECKING)
@pytest.mark.parametrize(
    ("ui", "how"),
    [
        ("next", "chmod -R a-w"),
        *ro.also_in_full(
            ("next", "no lock file"), ("classic", "chmod -R a-w"), ("classic", "no lock file")
        ),
    ],
)
def test_a_start_that_cannot_take_the_migration_lock_does_not_stay_checking(
    profile: ro.Profile, monkeypatch: pytest.MonkeyPatch, ui: str, how: str
) -> None:
    if how == "no lock file":
        (profile.state / ".legacy-migration-v1.json.mutation.lock").unlink()
    (favourite,) = [change for change in CTL if change.name == "ctl favourite"]
    life = live(
        profile,
        "state",
        ui,
        monkeypatch,
        [favourite],
        idle=0,
        start_within=2.5,
        files_too=how == "chmod -R a-w",
    )
    print(_story(life))
    assert life.startup and all(ro.WHY.search(text) for text in life.startup), life.startup
    assert not life.problems, life.problems
    assert not life.probe.violations(), verdict(life.probe)


@ro.full_only
@pytest.mark.parametrize("case", ["config", "library"])
@pytest.mark.parametrize("ui", ["next", "classic"])
def test_each_window_with_every_file_in_the_folder_read_only_too(
    profile: ro.Profile, monkeypatch: pytest.MonkeyPatch, ui: str, case: str
) -> None:
    """``chmod -R a-w`` on the configuration folder, or on the library: as the folders alone.

    A lock file that can't be opened refuses the write it guards, naming
    itself. (The state folder made read-only this way is the finding above.)
    The downloaded wallpaper is left out: ``chmod`` moves its file's ctime,
    which its ownership record names, so it is no longer the app's to delete
    (a read-only mount wouldn't do that; this probe's ``chmod`` does).
    """
    table, known = (NEXT, KNOWN_NEXT) if ui == "next" else (CLASSIC, KNOWN_CLASSIC)
    tried = [
        change
        for change in (*chosen(table, case, known.get(case, frozenset())), *_spill(table, case))
        if change.name != "delete a downloaded wallpaper"
    ]
    life = live(profile, case, ui, monkeypatch, tried, idle=0, files_too=True)
    print(_story(life))
    assert_lived(life, tried, polled=False)
    assert_each(life.probe, tried)
