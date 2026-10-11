"""A healthy profile, one of its folders made read-only, and what a refused change must leave.

For `tests.test_read_only_profile`. Nothing here injects a failure: a folder
is made read-only with its real permission bits (`read_only`), so every
refusal is the kernel's own EACCES. The modes are put back on the way out,
whatever happened, so pytest can delete the tree.

The profile (`build`) is made by a real application while everything is
writable, through the control socket's verb table and the application's own
public methods, so every store, every lock file beside a store and the trash
exist exactly as a running Wall-in-One leaves them.

What a probe then looks at, after every attempted change:

* `Snapshot`: every file's bytes and mode under the configuration folder, the
  state folder, the library and the trash;
* `held`: what the application still holds once its lanes are idle;
* `disagreements`: where what the application shows differs from what the
  files hold (a refused change must leave the durable value on show);
* `dangling`: a reference in one store to something another store no longer
  has (a change that landed in one store only);
* `Said`: every message a person or a ``ctl`` caller was given.
"""

from __future__ import annotations

import contextlib
import functools
import os
import re
import stat
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, cast

import pytest

from tests.golden import harness
from tests.gtk_helpers import spin_until
from tests.test_manage import downloaded, provenance
from tests.test_ui_runtime import FakeWindow, _application, _attach, _close
from wall_in_one import config, paths, runtime_config, ui_prefs
from wall_in_one.control import client, server
from wall_in_one.control.protocol import Request, Response
from wall_in_one.library import displays, favourites, pairings, playlists, removals, schedules
from wall_in_one.providers import credentials
from wall_in_one.session import QUICK_CHOICE_ID
from wall_in_one.ui.app import Application, _Commands

#: The three folders a probe makes read-only, one at a time.
CASES: Final = ("config", "state", "library")
#: How long anything may take before it counts as stuck (the machine is loaded).
BOUND: Final = 15.0
#: A refusal that says why: the kernel's own words, or the product's for them.
WHY: Final = re.compile(
    r"permission denied|read-only|read only|not writable"
    r"|(could|can) ?not be (saved|written|removed)|(was|were) not saved",
    re.IGNORECASE,
)
#: What a refused deletion leaves in a writable folder by design: `file_io`'s
#: retained namespace, which Tidy up lists (a Trash record copy is kept there).
RESIDUE: Final = re.compile(r"^\+(.*/)?\.wall-in-one-retained/")
#: Lock files beside a store: empty, never removed, and no change to anything.
LOCK: Final = re.compile(r"\.(mutation|operation|compiler)\.lock$|\.lock$")
#: A writer's private temporary (`state_file.write_atomic_bytes`, `runtime_config`, the key).
TEMPORARY: Final = re.compile(r"^\.[^/]*(\.tmp|\.[A-Za-z0-9_]{8})$|^\.wall-in-one-tmp-")

#: A temporary's own name inside a message, which differs on every attempt.
TEMPORARY_NAME: Final = re.compile(
    r"\.[\w.-]+\.(tmp|[A-Za-z0-9_]{8})(?=['\"\s]|$)|\.wall-in-one-tmp-\w+"
)

WALLPAPERS: Final = ("dawn.png", "noon.png", "dusk.png", "spare.png", "extra.png", "gone.png")
VIDEOS: Final = ("rain.mp4",)
KEY: Final = "a" * 32
#: The wallpaper the profile downloaded, below a library folder.
DOWNLOADED: Final = Path("Wall-in-One") / "Wallhaven" / "fetched.jpg"


def rebind_download(profile: Profile) -> None:
    """Write the downloaded wallpaper's ownership record again, for the file as it is now.

    The record names its file's inode, so a copy of the profile holds one
    that names another file's. Nothing else about the library changes.
    """
    path = profile.library / DOWNLOADED
    path.with_name(path.name + ".wallhaven.json").write_text(
        provenance(path, "Wallhaven"), encoding="utf-8"
    )


#: Whether the full campaigns run too (`tests.released_faults.FULL`).
FULL: Final = bool(os.environ.get("WIO_PROBE_FULL"))


def full_only[T: Callable[..., None]](test: T) -> T:
    """A full campaign: collected only with ``WIO_PROBE_FULL`` set.

    Not a skip mark. These tests run under the display-backed gate, which
    reads any skipped test as a display it didn't get, and fails.
    """
    test.__test__ = FULL  # type: ignore[attr-defined]
    return test


def also_in_full(*parameters: Any) -> tuple[Any, ...]:
    """More parameters for a test, in a full campaign only (see `full_only`)."""
    return parameters if FULL else ()


def pinned[T: Callable[..., None]](reason: str) -> Callable[[T], T]:
    """A finding kept as a test that must still fail, and says so once it doesn't.

    What ``xfail(strict=True)`` is, without the "skipped" it puts in a report:
    the display-backed gate fails on any. The test runs; its failing, however
    it fails, is the finding still standing, and its passing fails with
    ``reason``. The fix takes the pin off in its own commit.
    """

    def decorate(test: T) -> T:
        @functools.wraps(test)
        def run(*arguments: Any, **keywords: Any) -> None:
            try:
                test(*arguments, **keywords)
            except Exception, pytest.fail.Exception:
                return
            pytest.fail(f"no longer fails, so take its pin off: {reason}")

        return cast(T, run)

    return decorate


def skip_as_root() -> None:
    """Permission bits don't bind root, so there is nothing to probe."""
    if os.geteuid() == 0:
        pytest.skip("permission bits don't bind root")


@dataclass(frozen=True)
class Profile:
    """Where a probe's profile lives: everything under one folder."""

    home: Path

    @property
    def library(self) -> Path:
        return self.home / "lib"

    @property
    def other(self) -> Path:
        """A second folder of pictures, not a library folder until a probe adds it."""
        return self.home / "other"

    @property
    def config(self) -> Path:
        return self.home / "config" / paths.APP_ID

    @property
    def state(self) -> Path:
        return self.home / "state" / paths.APP_ID

    @property
    def trash(self) -> Path:
        return self.home / "data" / "Trash"

    @property
    def noctalia(self) -> Path:
        """Noctalia's own state folder, where its ``settings.toml`` is (never read-only here)."""
        return self.home / "state" / "noctalia"

    def folder(self, case: str) -> Path:
        return {"config": self.config, "state": self.state, "library": self.library}[case]

    def wallpaper(self, name: str) -> Path:
        return self.library / name

    def roots(self) -> dict[str, Path]:
        """Every tree a snapshot covers."""
        return {
            "config": self.config,
            "state": self.state,
            "library": self.library,
            "trash": self.trash,
            "noctalia": self.noctalia,
        }


def enter(profile: Profile, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the process at ``profile`` (the conftest guard pointed it at ``tmp_path``)."""
    for variable, name in (
        ("HOME", "home"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_STATE_HOME", "state"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_RUNTIME_DIR", "run"),
    ):
        directory = profile.home / name
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        monkeypatch.setenv(variable, str(directory))


# -- the application, without a window of its own ------------------------------------------------


def quiet(application: Application) -> bool:
    """Nothing is in flight: no write, batch, command, scan, palette work, poll or publication.

    A publication that was refused (the state folder is read-only) has
    finished too: its request stays the newest, with nothing compiling.

    A status poll counts while its answer is being read: what it reports can
    queue a health save, and a snapshot taken between the two would see that
    save land later and charge it to whatever change was being judged. (That
    was the one intermittent failure of this probe under load.)
    """
    with application._settings_authoring_lock:
        settings_waiting = bool(application._settings_authoring_pending)
    return (
        not application._runtime_status_pending
        and not application._authoring_active
        and not application._authoring_queue
        and not application._settings_authoring_running
        and not settings_waiting
        and not application._runtime_action_pending
        and not application._runtime_compile_pending
        and not application._runtime_reload_pending
        and not application._runtime_health_pending
        and application._library_scan_future is None
        and not application._theme_draining
        and not application._removal_active
        and not application.tidy_lane.busy
    )


def held(application: Application) -> list[str]:
    """What the application still holds, by name; empty when nothing would stop a quit."""
    with application._settings_authoring_lock:
        settings_waiting = bool(application._settings_authoring_pending)
    holds = {
        "an authoring task": application._authoring_active,
        "queued authoring tasks": bool(application._authoring_queue),
        "authoring lifetime holds": bool(application._authoring_lifetime_holds),
        "a settings batch": application._settings_authoring_running,
        "settings waiting to be saved": settings_waiting,
        "settings callbacks": bool(application._settings_authoring_callbacks),
        "a runtime command": application._runtime_action_pending,
        "a Quick choice": application._quick_choice_pending,
        "a removal": application._removal_active,
        "a removal's convergence hold": application._removal_convergence_held,
        "a publication hold": application._runtime_publication_held,
        "a library scan hold": application._runtime_library_scan_held,
        "a library scan": application._library_scan_future is not None,
    }
    return [name for name, value in holds.items() if value]


def boot(
    profile: Profile,
    monkeypatch: pytest.MonkeyPatch,
    *,
    service: bool = False,
    stills: bool = False,
) -> tuple[Application, FakeWindow]:
    """A real application over ``profile``, its first scan done, behind a recording window.

    As `tests.test_ui_runtime._application`: past the older-data check, with
    no window of its own and no control socket. The runtime is a stub that
    acknowledges every request. Stills for videos that have none are made
    after the scan only with ``stills`` (an ffmpeg process each).
    """
    enter(profile, monkeypatch)
    if service:
        application = Application(service=True, ui="next")
        application._authoring_migration_ready = True
    else:
        application = _application(profile.home, monkeypatch)
    window = FakeWindow()
    _attach(application, window)
    monkeypatch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())
    if not stills:
        monkeypatch.setattr(application, "_make_missing_stills", lambda: None)
    application.refresh_library()
    spin_until(lambda: quiet(application), BOUND * 2, what="the first scan and its publication")
    return application, window


def close(application: Application) -> None:
    """Stop every worker the application started (`tests.test_ui_runtime._close`)."""
    application.tidy_lane.shutdown()
    _close(application)


def ctl(application: Application, verb: str, argument: str | None = None) -> Response:
    """Dispatch one verb through the control socket's own table, in this process."""
    replies: list[Response] = []
    server.dispatch(
        server.build_verb_table(_Commands(application)),
        Request(verb, argument).encode(),
        replies.append,
    )
    spin_until(lambda: bool(replies), BOUND * 2, what=f"ctl {verb}")
    return replies[0]


# -- the healthy profile ---------------------------------------------------------------------


def build(home: Path, monkeypatch: pytest.MonkeyPatch) -> Profile:
    """A profile a running Wall-in-One left behind, everything writable.

    The library: six pictures, a video and a wallpaper Wall-in-One downloaded
    (with its ownership record). Morning [dawn, noon], Evening
    [dusk, noon] (the saved default playlist, and DP-1's own), Spare [extra];
    dawn is a favourite with colours of its own; rain is shown through noon;
    Morning plays on Monday and Tuesday mornings; a Wallhaven key and window
    preferences are saved; gone.png was moved to the trash, so the removal
    journal and its locks exist.
    """
    profile = Profile(home)
    enter(profile, monkeypatch)
    profile.library.mkdir(parents=True)
    profile.other.mkdir(parents=True)
    for index, name in enumerate(WALLPAPERS):
        shade = (40 * index + 20, 200 - 30 * index, 90)
        profile.wallpaper(name).write_bytes(harness.tiny_png(shade, size=8))
    for name in VIDEOS:
        profile.wallpaper(name).write_bytes(b"a video no still was captured from")
    (profile.other / "far.png").write_bytes(harness.tiny_png((9, 9, 200), size=8))
    downloaded(profile.library, DOWNLOADED.name)
    config.save(replace(config.Settings(), roots=(profile.library,), scan_workshop=False))
    application, window = boot(profile, monkeypatch)
    try:
        lib = profile.wallpaper
        for verb, argument in (
            ("playlist-new", "Morning"),
            ("playlist-add", f"Morning {lib('dawn.png')}"),
            ("playlist-add", f"Morning {lib('noon.png')}"),
            ("playlist-new", "Evening"),
            ("playlist-add", f"Evening {lib('dusk.png')}"),
            ("playlist-add", f"Evening {lib('noon.png')}"),
            ("playlist-new", "Spare"),
            ("playlist-add", f"Spare {lib('extra.png')}"),
            ("favourite", str(lib("dawn.png"))),
            ("schedule-add", "Morning days=mon,tue from=06:00 to=12:00"),
            ("display-assign", "DP-1 Evening"),
            ("palette", f"{lib('dawn.png')} adaptive"),
            ("still", f"{lib('rain.mp4')} :: {lib('noon.png')}"),
            ("remove", str(lib("gone.png"))),
        ):
            reply = ctl(application, verb, argument)
            assert reply.ok, f"building the profile: ctl {verb} {argument}: {reply.message}"
            spin_until(lambda: quiet(application), BOUND * 2, what=f"the profile's {verb}")
        evening = application.session.playlists.by_name("Evening")
        assert evening is not None
        problems: list[str] = []
        assert application.update_settings_async(
            active_playlist=evening.id, on_error=problems.append
        )
        spin_until(lambda: quiet(application), BOUND * 2, what="the profile's default playlist")
        assert problems == [] and window.reports == [], (problems, window.reports)
        assert application.publication_problem == ""
    finally:
        close(application)
    credentials.save_key(KEY)
    ui_prefs.update({"thumbnail_size": "small"})
    assert not profile.wallpaper("gone.png").exists()
    return profile


# -- read-only, for real ------------------------------------------------------------------------


@contextlib.contextmanager
def read_only(folder: Path, *, files_only: bool = False, files_too: bool = False) -> Iterator[None]:
    """Make ``folder`` read-only with its real permission bits; put them back afterwards.

    Every folder below it gets 0555, so nothing can be created, renamed or
    removed in it; files keep their modes (a store's own file is still
    writable in place, which no writer here does: a lock file is only
    opened). ``files_too``: every file gets 0444 as well, which is what
    ``chmod -R a-w`` leaves, and what a read-only mount amounts to: an
    existing lock file can't be opened for writing either. ``files_only``:
    every file gets 0444 and the folders stay writable (``chmod a-w *``).
    """
    skip_as_root()
    modes: list[tuple[Path, int]] = []
    folders: list[Path] = []
    try:
        for directory, _names, files in os.walk(folder):
            here = Path(directory)
            if files_only or files_too:
                for name in files:
                    path = here / name
                    modes.append((path, stat.S_IMODE(path.lstat().st_mode)))
                    path.chmod(0o444)
            if not files_only:
                modes.append((here, stat.S_IMODE(here.lstat().st_mode)))
                folders.append(here)
        for path in reversed(folders):
            path.chmod(0o555)
        yield
    finally:
        # Folders first (the deepest last made read-only, so top down), then what is in them.
        for path, mode in sorted(modes, key=lambda entry: not entry[0].is_dir()):
            with contextlib.suppress(FileNotFoundError):
                path.chmod(mode)


# -- what is on disk ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """Every file's bytes and mode, and every folder's mode, under the profile's trees."""

    trees: dict[str, dict[str, tuple[int, bytes]]]

    @classmethod
    def take(cls, profile: Profile) -> Snapshot:
        trees: dict[str, dict[str, tuple[int, bytes]]] = {}
        for name, root in profile.roots().items():
            found: dict[str, tuple[int, bytes]] = {}
            if root.exists():
                found["."] = (stat.S_IMODE(root.lstat().st_mode), b"<folder>")
            for directory, names, files in os.walk(root):
                for entry in names:
                    path = Path(directory) / entry
                    mode = stat.S_IMODE(path.lstat().st_mode)
                    found[str(path.relative_to(root)) + "/"] = (mode, b"<folder>")
                for entry in files:
                    path = Path(directory) / entry
                    mode = stat.S_IMODE(path.lstat().st_mode)
                    try:
                        body = path.read_bytes()
                    except OSError as error:
                        body = f"<unreadable: {error.strerror}>".encode()
                    found[str(path.relative_to(root))] = (mode, body)
            trees[name] = found
        return cls(trees)

    def changes(self, later: Snapshot, *, locks: bool = False) -> dict[str, list[str]]:
        """What differs in ``later``, per tree: ``+new``, ``-gone``, ``~bytes``, ``%mode``.

        Lock files beside a store are left out unless ``locks``: they are
        empty and their appearing changes nothing.
        """
        changed: dict[str, list[str]] = {}
        for name, before in self.trees.items():
            after = later.trees.get(name, {})
            lines: list[str] = []
            for path in sorted(before.keys() | after.keys()):
                if not locks and LOCK.search(path):
                    continue
                old, new = before.get(path), after.get(path)
                if old == new:
                    continue
                if old is None:
                    lines.append(f"+{path}")
                elif new is None:
                    lines.append(f"-{path}")
                elif old[1] != new[1]:
                    lines.append(f"~{path}")
                else:
                    lines.append(f"%{path} {old[0]:o}->{new[0]:o}")
            if lines:
                changed[name] = lines
        return changed

    def temporaries(self) -> list[str]:
        """Private temporaries a writer left behind."""
        return sorted(
            f"{name}/{path}"
            for name, tree in self.trees.items()
            for path in tree
            if TEMPORARY.search(Path(path).name) and not LOCK.search(path)
        )


# -- what the application shows against what the files hold ---------------------------------------


def _playlists(store: playlists.Store) -> list[tuple[object, ...]]:
    return sorted(
        (
            playlist.id,
            playlist.name,
            tuple((entry.id, str(entry.path)) for entry in playlist.entries),
            playlist.cycle_interval,
            playlist.shuffle,
        )
        for playlist in store.all()
    )


def _rules(store: schedules.Store) -> list[tuple[object, ...]]:
    return [
        (
            rule.id,
            rule.playlist,
            rule.months,
            rule.weekdays,
            rule.start,
            rule.end,
            rule.enabled,
            rule.connector,
            rule.name,
        )
        for rule in store.rules
    ]


def _pairings(store: pairings.Store) -> dict[str, object]:
    return {key: record.to_json() for key, record in store.records.items()}


def disagreements(application: Application) -> list[str]:
    """Where what the application holds differs from what a fresh read of the files gives."""
    session = application.session
    found: list[str] = []
    try:
        saved = config.load_strict()
    except config.ConfigError as error:
        return [f"settings.toml can't be read: {error}"]
    if application.settings != saved:
        found.append(f"settings: shown {_delta(saved, application.settings)}")
    if application.requested_settings != saved:
        found.append(f"requested settings: {_delta(saved, application.requested_settings)}")
    if _playlists(session.playlists) != _playlists(playlists.Store.open()):
        found.append("playlists")
    if _rules(session.schedules) != _rules(schedules.Store.open()):
        found.append("schedule rules")
    if session.displays.all() != displays.Store.open().all():
        found.append("display assignments")
    if session.favourites.paths != favourites.Store.open().paths:
        found.append("favourites")
    if _pairings(session.pairings) != _pairings(pairings.Store.open()):
        found.append("pairings")
    return found


def _delta(saved: config.Settings, shown: config.Settings) -> str:
    return ", ".join(
        f"{name}={getattr(shown, name)!r} (saved {getattr(saved, name)!r})"
        for name in config.Settings.__dataclass_fields__
        if getattr(saved, name) != getattr(shown, name)
    )


def dangling() -> list[str]:
    """References from one store to something another no longer has, and unfinished journals."""
    found: list[str] = []
    known = {playlist.id for playlist in playlists.Store.open().all()}
    names = {playlist.name.casefold() for playlist in playlists.Store.open().all()}
    builtin = {"", runtime_config.FALLBACK_PLAYLIST_ID, QUICK_CHOICE_ID}

    def missing(reference: str) -> bool:
        return (
            reference not in known
            and reference not in builtin
            and reference.casefold() not in names
        )

    default = config.load_strict().active_playlist
    if missing(default):
        found.append(f"settings.toml's default playlist {default!r} is no playlist")
    for rule in schedules.Store.open().rules:
        if missing(rule.playlist):
            found.append(f"schedule rule {rule.id} names playlist {rule.playlist!r}, which is gone")
    for connector, playlist in displays.Store.open().all():
        if missing(playlist):
            found.append(f"{connector}'s own playlist {playlist!r} is gone")
    journal = removals.Store.open()
    try:
        for intent in journal.records:
            found.append(f"an unfinished removal of {intent.path} is still in the journal")
    finally:
        journal.close()
    return found


# -- what was said ----------------------------------------------------------------------------


@dataclass
class Said:
    """Every message a person was given, in order, from every place one is shown."""

    texts: list[str] = field(default_factory=list)

    def hear(self, text: str) -> None:
        self.texts.append(text)

    def mark(self) -> int:
        return len(self.texts)

    def since(self, mark: int) -> list[str]:
        """What was said after ``mark``, each message once."""
        return list(dict.fromkeys(self.texts[mark:]))

    def follow_log(self, application: Application) -> None:
        """Everything the application reports, window or none (`Application.window_report`)."""

        def heard(entry: Any) -> None:
            if entry.source == "app":
                self.hear(entry.text)

        application.activity_log.subscribe(heard)


@dataclass
class Uncaught:
    """Exceptions nothing caught: on GTK's thread, on a worker, or in a finalizer."""

    errors: list[str] = field(default_factory=list)

    def watch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def on_main(kind: type[BaseException], error: BaseException, _trace: object) -> None:
            self.errors.append(f"uncaught on GTK's thread: {kind.__name__}: {error}")

        def on_thread(arguments: threading.ExceptHookArgs) -> None:
            name = arguments.thread.name if arguments.thread is not None else "a thread"
            self.errors.append(f"uncaught on {name}: {arguments.exc_value!r}")

        def unraisable(arguments: Any) -> None:
            self.errors.append(f"unraisable: {arguments.exc_value!r} in {arguments.object!r}")

        monkeypatch.setattr(sys, "excepthook", on_main)
        monkeypatch.setattr(threading, "excepthook", on_thread)
        monkeypatch.setattr(sys, "unraisablehook", unraisable)


# -- one attempted change, judged -----------------------------------------------------------------


@dataclass
class Attempt:
    """One change tried with a folder read-only, and everything that was wrong with it."""

    name: str
    said: list[str]
    changed: dict[str, list[str]]
    refused: bool
    violations: list[str]
    #: Worth a line in a report, not a failure: residue left by design.
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        outcome = "refused" if self.refused else "accepted"
        lines = [f"{self.name}: {outcome}; said {self.said}; changed {self.changed}"]
        lines.extend(f"    ! {violation}" for violation in self.violations)
        lines.extend(f"    . {note}" for note in self.notes)
        return "\n".join(lines)


@dataclass
class Probe:
    """An application over a profile with one folder read-only, and its bookkeeping."""

    profile: Profile
    case: str
    application: Application
    said: Said
    uncaught: Uncaught
    attempts: list[Attempt] = field(default_factory=list)

    def snapshot(self) -> Snapshot:
        return Snapshot.take(self.profile)

    def quiet(self) -> bool:
        return quiet(self.application)

    def judge(
        self,
        name: str,
        before: Snapshot,
        mark: int,
        *,
        touches: frozenset[str] | set[str],
        reply: Response | None = None,
        pending: Callable[[], list[str]] | None = None,
    ) -> Attempt:
        """Everything a change must have left, whether it was refused or not.

        ``touches``: the folders the change has to write. With one of them
        read-only it must be refused, saying why, and every tree must be
        byte-identical. Whatever it touches: the read-only folder is
        untouched, nothing was thrown, nothing is still held, what the
        application shows is what the files hold, and no store names
        something another store lost.
        """
        after = self.snapshot()
        said = self.said.since(mark)
        if reply is not None and reply.message:
            said = [reply.message, *said]
        changed = before.changes(after)
        notes: list[str] = []
        for tree, lines in list(changed.items()):
            residue = [line for line in lines if RESIDUE.search(line)]
            if residue:
                notes.append(f"left in the {tree}: {residue}")
                changed[tree] = [line for line in lines if line not in residue]
                if not changed[tree]:
                    del changed[tree]
        violations: list[str] = []
        violations.extend(self.uncaught.errors)
        self.uncaught.errors.clear()
        if self.case in changed:
            violations.append(f"the read-only {self.case} folder changed: {changed[self.case]}")
        still = held(self.application)
        if still:
            violations.append(f"still held once idle: {still}")
        if pending is not None:
            violations.extend(pending())
        differing = disagreements(self.application)
        if differing:
            violations.append(f"shown differs from what is saved: {differing}")
        loose = dangling()
        if loose:
            violations.append(f"half-done across stores: {loose}")
        new_temporaries = sorted(set(after.temporaries()) - set(before.temporaries()))
        if new_temporaries:
            violations.append(f"temporaries left behind: {new_temporaries}")
        must_refuse = self.case in touches
        refused = (reply is not None and not reply.ok) or (reply is None and not changed)
        if must_refuse:
            if changed:
                violations.append(f"refused, yet it changed {changed}")
            if reply is not None and reply.ok:
                violations.append(f"the reply was a success: {reply.message!r}")
            if not said:
                violations.append("refused in silence: nothing was said")
            elif not any(WHY.search(text) for text in said):
                violations.append(f"refused without saying why: {said}")
        elif reply is not None and not reply.ok and not reply.message:
            violations.append("failed without a message")
        attempt = Attempt(name, said, changed, refused, violations, notes)
        self.attempts.append(attempt)
        return attempt

    def report(self) -> str:
        return "\n".join(attempt.describe() for attempt in self.attempts)

    def violations(self) -> dict[str, list[str]]:
        return {attempt.name: attempt.violations for attempt in self.attempts if attempt.violations}
