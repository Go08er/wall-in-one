"""One fault at a time for the released version as a producer (`tests.released`).

A file the released 0.2.2 left behind is what this checkout's code meets after
an update, so a probe needs the released code to leave its leftovers the ways
it really can: its own error path after one failed call, a process that dies
between two steps, and a person editing a file while it sits at a public name.

`TRACER` goes in front of a child script, which defines what the released code
is to do and hands it to ``serve``. Each `Job` is then run in a fork of that
child, in its own scratch profile, under its one plan:

``{}``
    Nothing: the trace says how many calls there are and what each is.
``{"kill": n}``
    The process dies (``os._exit(77)``) just before change ``n``: every state a
    kill can leave is the state before some change.
``{"fail": n, "errno": "EIO"}``
    Call ``n`` fails once with that error, and the released code carries on
    through its ordinary error path.
``{"edit": n}``
    Right after change ``n`` put an entry at a public name, that entry is edited
    in place (same inode), as a person or another program would: one byte
    (``"as": "flip"``, the default, the same length), a note added at its end
    (``"extend"``) or its last third cut off (``"truncate"``). With ``"fail"``
    as well, a later call then fails: the released code finds its publication
    unverifiable and moves the edited entry wherever it does.

``{"pause": n, "gate": path}``
    Just before call ``n`` the process stops, alive, with whatever it holds
    (`paused`): another version takes its turn, and then this one carries on.

``{"meddle": n, "which": k, "how": "flip"}``
    Just before call ``n``, the ``k``-th file then in the profile is edited in
    place (``"how"``: ``"extend"`` or ``"truncate"`` as above; ``"replace"``:
    rewritten under a new inode, as a program that saves atomically does), and
    the released code carries on: a person, or Noctalia, changing a file while
    the released code is in the middle of it.

While armed, the tracer numbers every filesystem call the released
``wall_in_one`` makes (never the import system's), and records every inode
that was ever at a public name (one with no leading dot): that, and whatever
a person edited, is what must outlive any cleanup. ``fsync`` is numbered, and
can fail, but is not carried out: a killed process loses nothing the kernel
has, and a power cut is not something a test can stage. What a person already
had before the released code ran, a script lays out itself (with the released
code, where its format is the released code's) and names with ``had(...)``.

`Watch` is the same for this checkout's code, in this process: its filesystem
calls counted, and at one of them an entry made impossible to look at
(mode 000, so every failure after it is the kernel's own).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Final

import pytest

from tests import released

#: The exit status of a child killed by its plan.
KILLED: Final = 77

TRACER: Final = r"""
import errno as _errno
import json as _json
import os as _os
import tempfile as _tempfile

_PLAN = {}
_STATE = {"calls": 0, "armed": False, "trace": [], "public": [], "edited": []}
#: The calls as they were before wrapping, for this tracer's own use.
_REAL = {}


def emit_trace():
    print(
        "TRACE "
        + _json.dumps(
            {"calls": _STATE["trace"], "public": _STATE["public"], "edited": _STATE["edited"]}
        ),
        flush=True,
    )


def arm():
    _STATE["armed"] = True


def disarm():
    _STATE["armed"] = False


def _from_the_released_code():
    # Only what wall_in_one itself asks for: never the import system, which
    # would turn an injected failure into a module that half loaded.
    frame = sys._getframe(3)
    for _depth in range(12):
        if frame is None:
            return False
        name = frame.f_globals.get("__name__", "")
        if name.startswith("importlib") or name == "zipimport":
            return False
        if name.startswith("wall_in_one"):
            return True
        frame = frame.f_back
    return False


def _numbered(kind, name, where):
    if not _STATE["armed"] or not _from_the_released_code():
        return 0
    _STATE["calls"] += 1
    index = _STATE["calls"]
    _STATE["trace"].append([index, kind, name, where])
    if _PLAN.get("meddle") == index:
        _STATE["armed"] = False
        try:
            _meddle(index)
        finally:
            _STATE["armed"] = True
    if _PLAN.get("pause") == index:
        _STATE["armed"] = False
        try:
            _pause()
        finally:
            _STATE["armed"] = True
    if kind == "change" and _PLAN.get("kill") == index:
        emit_trace()
        _os._exit(77)
    if _PLAN.get("fail") == index:
        number = getattr(_errno, _PLAN.get("errno", "EIO"))
        raise OSError(number, "injected one-shot " + _os.strerror(number), where)
    return index


def had(*paths):
    # What a person already had at a public name before the released code ran.
    for path in paths:
        _STATE["public"].append([0, _os.fspath(path), _os.lstat(path).st_ino])


def _pause():
    # Held just before this call until the other version has had its turn.
    import time as _time

    gate = _PLAN["gate"]
    open(gate + ".reached", "w").close()
    while not _os.path.exists(gate + ".go"):
        _time.sleep(0.005)


def _flipped(data):
    at = next((i for i, byte in enumerate(data) if chr(byte).isascii() and chr(byte).isalpha()), 0)
    return at, bytes([(data or b" ")[at] ^ 0x20])


def _edit(path, how):
    # In place, the same inode: one byte, a note at the end, or the last third gone.
    descriptor = _REAL["open"](path, _os.O_RDWR | _os.O_NOFOLLOW)
    try:
        size = _os.fstat(descriptor).st_size
        if how == "extend":
            _os.pwrite(descriptor, b"\n# a person's note\n", size)
        elif how == "truncate" and size > 1:
            _os.ftruncate(descriptor, max(1, size * 2 // 3))
        else:
            at, byte = _flipped(_os.pread(descriptor, size, 0))
            _os.pwrite(descriptor, byte, at)
    finally:
        _os.close(descriptor)


def _meddle(index):
    # The k-th regular file in the profile, as it is now, changed by someone else.
    base = _os.path.dirname(_os.environ["HOME"])
    found = []
    for directory, _folders, names in _os.walk(base):
        found += [_os.path.join(directory, name) for name in names]
    # Public names first: those are the files a person or another program has.
    found = sorted(
        (path for path in found if _os.path.isfile(path) and not _os.path.islink(path)),
        key=lambda path: (not _is_public(path), path),
    )
    which = _PLAN.get("which", 0)
    if which >= len(found):
        return
    path = found[which]
    if _PLAN.get("how") == "replace":
        with open(path, "rb") as source:
            data = source.read()
        at, byte = _flipped(data)
        fresh = path + ".rewritten-by-someone"
        with open(fresh, "wb") as sink:
            sink.write(data[:at] + byte + data[at + 1 :])
        _os.chmod(fresh, _os.stat(path).st_mode & 0o777)
        _REAL["replace"](fresh, path)
    else:
        _edit(path, _PLAN.get("how", "flip"))
    _STATE["edited"].append([index, path, _REAL["lstat"](path).st_ino])


def _text(value):
    if isinstance(value, int):
        return "<descriptor>"
    try:
        return _os.fsdecode(value)
    except TypeError:
        return repr(value)


def _is_public(path):
    # A name people and other programs see: no leading dot, and not an entry
    # of the retained folder (which is reached through a pinned descriptor).
    name = _os.path.basename(path)
    return not name.startswith(".") and name != "entry" and not name.startswith("entry-")


def _published(index, path):
    # ``path`` now names an entry some change just put there.
    try:
        status = _REAL["lstat"](path)
    except OSError:
        return
    import stat as _stat

    if not _stat.S_ISREG(status.st_mode) or not _is_public(path):
        return
    _STATE["public"].append([index, path, status.st_ino])
    if _PLAN.get("edit") == index and status.st_size > 0:
        _edit(path, _PLAN.get("as", "flip"))
        _STATE["edited"].append([index, path, status.st_ino])


def _wrap(owner, attribute, kind, places=()):
    real = getattr(owner, attribute)

    def wrapped(*arguments, **keywords):
        where = _text(arguments[0]) if arguments else ""
        index = _numbered(kind, attribute, where)
        result = real(*arguments, **keywords)
        if index and places:
            _STATE["armed"] = False  # the tracer's own looking is not the released code's
            try:
                for place in places:
                    if place < len(arguments):
                        _published(index, _text(arguments[place]))
            finally:
                _STATE["armed"] = True
        return result

    setattr(owner, attribute, wrapped)
    return real


def _wrap_open():
    real = _os.open
    creating = _os.O_CREAT | _os.O_TRUNC | getattr(_os, "O_TMPFILE", 0)

    def opened(path, flags, *arguments, **keywords):
        kind = "change" if flags & creating & ~_os.O_DIRECTORY else "look"
        _numbered(kind, "open", _text(path))
        return real(path, flags, *arguments, **keywords)

    _os.open = opened
    return real


def _wrap_mkstemp():
    real = _tempfile.mkstemp

    def mkstemp(*arguments, **keywords):
        _numbered("change", "mkstemp", str(keywords.get("prefix", "")))
        return real(*arguments, **keywords)

    _tempfile.mkstemp = mkstemp


def trace_filesystem(*renames):
    # ``renames``: (module, attribute) of the released rename wrappers, each
    # taking (source, destination); an exchange publishes at both names.
    _REAL["lstat"] = _os.lstat
    _REAL["replace"] = _os.replace
    _REAL["open"] = _wrap_open()
    _wrap_mkstemp()
    for attribute in ("replace", "rename", "link"):
        _wrap(_os, attribute, "change", places=(1,))
    for attribute in ("unlink", "remove", "rmdir", "mkdir", "truncate", "ftruncate"):
        _wrap(_os, attribute, "change")
    for attribute in ("lstat", "stat", "fstat", "read", "pread", "listdir", "scandir"):
        _wrap(_os, attribute, "look")
    _os.fsync = _skipped_sync
    for owner, attribute in renames:
        _wrap(owner, attribute, "change", places=(0, 1) if "exchange" in attribute else (1,))


def _skipped_sync(descriptor):
    _numbered("sync", "fsync", "<descriptor>")


def serve(operation):
    # One fork per job: its own profile, its own plan, the released code as loaded.
    jobs = _json.loads(sys.stdin.read())
    for number, job in enumerate(jobs):
        sys.stdout.flush()
        sys.stderr.flush()
        child = _os.fork()
        if child == 0:
            status = 1
            try:
                _os.environ.update(job["environment"])
                _PLAN.update(job["plan"])
                print("JOB", number, flush=True)
                arm()
                try:
                    operation(*job["arguments"])
                finally:
                    disarm()
                    emit_trace()
                status = 0
            except BaseException:
                import traceback

                traceback.print_exc()
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
                _os._exit(status)
        _pid, waited = _os.waitpid(child, 0)
        print("END", number, _os.waitstatus_to_exitcode(waited), flush=True)
"""


@dataclass(frozen=True, slots=True)
class Call:
    index: int
    #: ``change`` (the file tree differs after it), ``look`` or ``sync``.
    kind: str
    name: str
    where: str


@dataclass(frozen=True, slots=True)
class Produced:
    """What one run of a released producer did."""

    status: int
    #: What the script printed, without the trace.
    output: str
    errors: str
    calls: tuple[Call, ...] = ()
    #: (call, path, inode) each time a change put a regular file at a public name.
    public: tuple[tuple[int, str, int], ...] = ()
    #: (call, path, inode) of each entry the plan edited in place.
    edited: tuple[tuple[int, str, int], ...] = ()

    @property
    def killed(self) -> bool:
        return self.status == KILLED

    @property
    def changes(self) -> tuple[Call, ...]:
        return tuple(call for call in self.calls if call.kind == "change")

    @property
    def once_public(self) -> frozenset[int]:
        """Inodes that were at a public name at some moment of this run."""
        return frozenset(inode for _call, _path, inode in self.public)


@dataclass(frozen=True, slots=True)
class Job:
    """One run of the producer: where (its scratch profile), and under which plan."""

    base: Path
    plan: Mapping[str, object] = field(default_factory=dict)
    arguments: tuple[str, ...] = ()


def profile_environment(base: Path) -> dict[str, str]:
    """The conventional directories, all under ``base``."""
    return {
        "HOME": str(base / "home"),
        "XDG_CONFIG_HOME": str(base / "config"),
        "XDG_STATE_HOME": str(base / "state"),
        "XDG_CACHE_HOME": str(base / "cache"),
        "XDG_DATA_HOME": str(base / "data"),
        "XDG_RUNTIME_DIR": str(base / "run"),
    }


def run(
    source: Path, script: str, jobs: Sequence[Job], *, timeout: float | None = None
) -> list[Produced]:
    """Run each job in a fork of one released child; what each produced, in order.

    ``script`` (after `TRACER`) imports the released modules, calls
    ``trace_filesystem`` and ends with ``serve(operation)``. The whole batch
    gets five minutes and half a second a job, unless ``timeout`` says.
    """
    if timeout is None:
        timeout = 300.0 + 0.5 * len(jobs)
    done = subprocess.run(
        _command(source, script),
        cwd=source,
        env=_environment(source),
        input=_request(jobs),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    produced = _parse(done.stdout, done.stderr)
    assert len(produced) == len(jobs), done.stdout[-2000:] + done.stderr[-4000:]
    return produced


def _command(source: Path, script: str) -> list[str]:
    return [sys.executable, "-c", released._PREAMBLE + TRACER + script, str(source)]


def _environment(source: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _request(jobs: Sequence[Job]) -> str:
    return json.dumps(
        [
            {
                "plan": dict(job.plan),
                "environment": profile_environment(job.base),
                "arguments": list(job.arguments),
            }
            for job in jobs
        ]
    )


def _parse(output: str, errors: str) -> list[Produced]:
    produced: list[Produced] = []
    lines: list[str] = []
    traced: dict[str, Any] = {}
    for line in output.splitlines():
        if line.startswith("JOB "):
            lines, traced = [], {}
        elif line.startswith("TRACE "):
            traced = json.loads(line.removeprefix("TRACE "))
        elif line.startswith("END "):
            produced.append(
                Produced(
                    status=int(line.split()[2]),
                    output="\n".join(lines),
                    errors=errors,
                    calls=tuple(Call(*call) for call in traced.get("calls", ())),
                    public=tuple((c, path, inode) for c, path, inode in traced.get("public", ())),
                    edited=tuple((c, path, inode) for c, path, inode in traced.get("edited", ())),
                )
            )
        else:
            lines.append(line)
    return produced


@contextlib.contextmanager
def paused(
    source: Path, script: str, job: Job, at: int, *, timeout: float = 120.0
) -> Iterator[list[Produced]]:
    """``job`` run up to its call ``at`` and held there, alive, while the block runs.

    Then it carries on to its end; the list given holds what it produced once
    the block is left. A run that ends before call ``at`` was simply not held.
    """
    gate = str(job.base) + ".gate"
    held = Job(job.base, {**job.plan, "pause": at, "gate": gate}, job.arguments)
    child = subprocess.Popen(
        _command(source, script),
        cwd=source,
        env=_environment(source),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    result: list[Produced] = []
    try:
        assert child.stdin is not None
        child.stdin.write(_request([held]))
        child.stdin.close()
        child.stdin = None
        deadline = time.monotonic() + timeout
        while not os.path.exists(gate + ".reached") and child.poll() is None:
            assert time.monotonic() < deadline, "the producer never reached its pause"
            time.sleep(0.005)
        yield result
    finally:
        Path(gate + ".go").touch()
        try:
            output, errors = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            output, errors = child.communicate()
            errors += "\nWEDGED: the held producer did not finish once released"
        result += _parse(output, errors) or [
            Produced(status=child.returncode or 1, output=output, errors=errors)
        ]


# -- a scratch profile per probe -------------------------------------------------------------


def scratch_profile(monkeypatch: pytest.MonkeyPatch, base: Path) -> Path:
    """Point this process's conventional directories at ``base`` too; returns it."""
    for variable, value in profile_environment(base).items():
        Path(value).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(variable, value)
    return base


# -- what must outlive a cleanup ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entry:
    path: Path
    inode: int
    size: int
    digest: str
    data: bytes = field(repr=False, default=b"")


@dataclass(frozen=True, slots=True)
class Tree:
    """Every regular file under some roots: where it is, which inode, and its bytes."""

    entries: tuple[Entry, ...] = field(default=())

    @property
    def digests(self) -> frozenset[str]:
        return frozenset(entry.digest for entry in self.entries)

    def at(self, path: Path) -> Entry | None:
        return next((entry for entry in self.entries if entry.path == path), None)

    def named(self, fragment: str) -> list[Entry]:
        return [entry for entry in self.entries if fragment in entry.path.name]


def _files(root: Path) -> Iterator[Path]:
    for directory, _folders, names in os.walk(root, followlinks=False):
        for name in names:
            yield Path(directory) / name


def _read_unnoticed(path: Path) -> bytes:
    """``path``'s bytes, read without touching its access time.

    A snapshot must not change what it looks at, and an access time can be
    evidence: the released removal stamps one on a file it is about to empty,
    and replay knows the emptied file by it.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NOATIME", 0))
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            return source.read()
    finally:
        os.close(descriptor)


def tree(*roots: Path) -> Tree:
    """Snapshot every regular file under ``roots`` (no links followed)."""
    found: list[Entry] = []
    for root in roots:
        for path in sorted(_files(root)):
            try:
                status = path.lstat()
            except OSError:
                continue
            if not os.path.isfile(path) or path.is_symlink():
                continue
            data = _read_unnoticed(path)
            found.append(
                Entry(path, status.st_ino, status.st_size, hashlib.sha256(data).hexdigest(), data)
            )
    return Tree(tuple(found))


#: The ways a person's edit changes a file here: one byte in place, a note
#: added at its end, its last third cut off. The last two change its length,
#: which a check of sizes meets differently from a check of bytes.
EDITS: Final = ("flip", "extend", "truncate")
NOTE: Final = b"\n# a person's note\n"


def edit_in_place(path: Path, how: str = "flip") -> None:
    """Edit ``path`` in place (the same inode), one of `EDITS`, as the tracer does.

    ``flip``: the case of its first letter (a comment's or a key's, in a text
    file), or its first byte if it has no letter; the same length.
    """
    with path.open("r+b") as handle:
        data = handle.read()
        if how == "extend":
            handle.write(NOTE)
        elif how == "truncate" and len(data) > 1:
            handle.truncate(max(1, len(data) * 2 // 3))
        else:
            data = data or b" "
            at = next(
                (
                    index
                    for index, byte in enumerate(data)
                    if chr(byte).isascii() and chr(byte).isalpha()
                ),
                0,
            )
            handle.seek(at)
            handle.write(bytes([data[at] ^ 0x20]))
        handle.flush()


def lost(
    before: Tree,
    after: Tree,
    must_survive: frozenset[int],
    exempt: Callable[[Entry], bool] = lambda _entry: False,
) -> list[Entry]:
    """Entries of ``before`` whose bytes are no longer anywhere in ``after``, counted.

    Only entries that hold something, and whose inode is in ``must_survive``
    (it was once public, or a person edited it): a private temporary nobody
    ever saw may be discarded. Counted, because two such files often hold the
    same bytes (a person's settings and the backup of them): emptying one is a
    loss even though the other still has the bytes, so each needs a copy of
    its own in ``after``. ``exempt`` entries (a file a step rewrites by
    design) are not counted.
    """
    kept = Counter(entry.digest for entry in after.entries)
    needed: Counter[str] = Counter()
    gone: list[Entry] = []
    for entry in before.entries:
        if entry.size > 0 and entry.inode in must_survive and not exempt(entry):
            needed[entry.digest] += 1
            if needed[entry.digest] > kept[entry.digest]:
                gone.append(entry)
    return gone


# -- every way to leave something behind ---------------------------------------------------------


#: Set (to anything) to run the full campaigns too: a failure at every call,
#: both errors, a wider window after each publication, every file meddled
#: with, and every case of every module. They take minutes, so a gate runs a
#: bounded one per module and skips these (`full_campaign`).
FULL: Final = "WIO_PROBE_FULL"
#: Marks a test as a full campaign: skipped, with the reason, unless `FULL` is set.
full_campaign: Final = pytest.mark.skipif(
    not os.environ.get(FULL),
    reason=f"a full campaign takes minutes; set {FULL}=1 to run it",
)


def single_faults(trace: Produced, *, full: bool) -> list[dict[str, object]]:
    """A kill before every change, and one failed call at a time.

    Bounded: EIO at every change. In full: EIO and EACCES at every call, the
    looks and syncs included (a verification that can't be read is how the
    released code comes to move an entry back).
    """
    plans: list[dict[str, object]] = [{"kill": call.index} for call in trace.changes]
    if full:
        for number in ("EIO", "EACCES"):
            plans += [{"fail": call.index, "errno": number} for call in trace.calls]
    else:
        plans += [{"fail": call.index, "errno": "EIO"} for call in trace.changes]
    return plans


def edits_while_public(trace: Produced, *, full: bool) -> list[dict[str, object]]:
    """An in-place edit right after each publication, alone and then followed by a
    failure at each of the next calls or a kill before each of the next changes:
    the released code finds what it published changed under it.

    Each of `EDITS` alone. What follows: after the one-byte edit; in full,
    after the two that change the length too, in the narrower window.
    """
    plans: list[dict[str, object]] = []
    for published, _path, _inode in trace.public:
        if not published:
            continue  # there before the released code ran (``had``)
        for how in EDITS:
            edit: dict[str, object] = {"edit": published, "as": how}
            plans.append(edit)
            if how != "flip" and not full:
                continue
            window = 24 if full and how == "flip" else 8
            later = [call for call in trace.calls if published < call.index <= published + window]
            plans += [{**edit, "fail": call.index, "errno": "EIO"} for call in later]
            plans += [{**edit, "kill": call.index} for call in later if call.kind == "change"]
    return plans


def meddling(trace: Produced, *, full: bool) -> list[dict[str, object]]:
    """Someone else changing one of the files then there, just before each change.

    Public names come first. Bounded: the first file, edited in place, one of
    `EDITS` in turn. In full: the first eight, each of `EDITS` and each also
    rewritten under a new inode.
    """
    plans: list[dict[str, object]] = []
    for number, call in enumerate(trace.changes):
        for which in range(8 if full else 1):
            for how in (*EDITS, "replace") if full else (EDITS[number % len(EDITS)],):
                plans.append({"meddle": call.index, "which": which, "how": how})
    return plans


_RANDOM: Final = re.compile(
    r"-[a-z0-9_]{8}$|\.[a-z0-9_]{8}(?=\.tmp)|\.[0-9a-f]{16}(?=\.tmp)"
    r"|entry-[0-9a-f]{32}|entry-[a-z0-9_]{8}|\d{8}-\d{6}(\.\d+)?|\d{4}-\d\d-\d\d-\d{6}"
    r"|[0-9a-f]{16,}"
)


def shape(found: Tree, base: Path) -> tuple[tuple[str, int], ...]:
    """``found`` with its random and dated name parts blanked: what kind of state it is."""
    return tuple(
        sorted(
            (_RANDOM.sub("*", str(entry.path.relative_to(base))), entry.size)
            for entry in found.entries
        )
    )


# -- this checkout's code, watched in this process ------------------------------------------------

_WATCHED_CHANGES: Final = (
    "replace",
    "rename",
    "link",
    "unlink",
    "remove",
    "rmdir",
    "mkdir",
    "truncate",
    "ftruncate",
)
_WATCHED_LOOKS: Final = ("lstat", "stat", "listdir", "scandir", "open")


class Watch:
    """This checkout's filesystem calls, listed; and at one, an entry no longer lookable.

    ``calls`` is what each call ``wall_in_one`` itself makes while this is
    entered was given: its changes and its looks by name. Just before call
    ``at`` (from 1), ``unseen`` is set to mode 000: a file that can't be read,
    or a folder nothing can be looked at in, natively (the errors after it are
    the kernel's). `restore` puts the mode back and hides nothing more.
    """

    def __init__(self, *, unseen: Path | None = None, at: int = 0) -> None:
        self.calls: list[str] = []
        self._unseen = unseen
        self._at = at
        self._names = _WATCHED_CHANGES + _WATCHED_LOOKS
        self._real: dict[str, Any] = {}
        self._mode: int | None = None

    def _wrapped(self, real: Callable[..., Any]) -> Callable[..., Any]:
        def call(*arguments: Any, **keywords: Any) -> Any:
            frame: Any = sys._getframe(1)
            for _depth in range(12):
                if frame is None:
                    break
                name = frame.f_globals.get("__name__", "")
                if name.startswith("importlib"):
                    break
                if name.startswith("wall_in_one"):
                    first = arguments[0] if arguments else ""
                    self.calls.append("" if isinstance(first, int) else os.fsdecode(first))
                    if len(self.calls) == self._at:
                        self._hide()
                    break
                frame = frame.f_back
            return real(*arguments, **keywords)

        return call

    def _hide(self) -> None:
        if self._unseen is None or self._mode is not None:
            return
        try:
            self._mode = stat.S_IMODE(os.lstat(self._unseen).st_mode)
            os.chmod(self._unseen, 0)
        except OSError:
            self._mode = None  # not there (yet, or any more): nothing to hide

    @property
    def hidden(self) -> bool:
        return self._mode is not None

    def restore(self) -> None:
        if self._mode is not None and self._unseen is not None:
            os.chmod(self._unseen, self._mode)
        self._mode = None
        self._at = 0

    def __enter__(self) -> Watch:
        for name in self._names:
            self._real[name] = getattr(os, name)
            setattr(os, name, self._wrapped(self._real[name]))
        return self

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _error: BaseException | None,
        _trace: TracebackType | None,
    ) -> None:
        for name, real in self._real.items():
            setattr(os, name, real)
        self._real.clear()
        self.restore()


def after_each_look_at(calls: Sequence[str], path: Path) -> list[int]:
    """The calls at which ``path`` going unlookable is a new case: the first, and
    the one after each call that named it (what was just looked at, or just
    put there, and can't be looked at again)."""
    named = [number + 2 for number, where in enumerate(calls) if path.name in where]
    return sorted({1, *(at for at in named if at <= len(calls))})


# -- a campaign: every leftover state, then this checkout's code on each ------------------------


@dataclass(frozen=True, slots=True)
class Variant:
    """One more thing done to a state the released code left, before this checkout runs.

    ``apply(profile)`` is what a person does there first, returning the inodes
    they edited; ``watch(profile)`` is what this checkout's first step runs
    under (an entry it can't look at from some call on). ``more(calls)`` names
    further variants of the same state once this one has run: ``calls`` is
    what each filesystem call of the first step was given, with this applied.
    """

    label: str
    apply: Callable[[Path], frozenset[int]] = lambda _base: frozenset()
    watch: Callable[[Path], Watch] | None = None
    more: Callable[[Sequence[str]], Sequence[Variant]] | None = None


@dataclass(slots=True)
class Campaign:
    """What a campaign found: nothing, if ``findings`` is empty."""

    findings: list[str] = field(default_factory=list)
    #: What each consumer step said, shortened, and how often.
    outcomes: Counter[str] = field(default_factory=Counter)
    #: Every distinct state the producer left, as `shape` gives it.
    states: list[tuple[tuple[str, int], ...]] = field(default_factory=list)
    faults: int = 0
    edits_after: int = 0
    #: How many states were run again under a `Variant`.
    varied: int = 0

    def summary(self, title: str) -> str:
        lines = [
            f"{title}: {self.faults} faults, {len(self.states)} distinct states, "
            f"{self.edits_after} more with one file edited afterwards"
            + (f", {self.varied} more varied" if self.varied else "")
        ]
        lines += [f"  {count:5d}  {said}" for said, count in sorted(self.outcomes.items())]
        return "\n".join(lines)


def _named(message: str, base: Path) -> list[Path]:
    """Every path under ``base`` that ``message`` names.

    A name may hold spaces, so each is read as far as it names something that
    is there; one that names nothing is given up to its first space.
    """
    found: list[Path] = []
    for match in re.finditer(rf"{re.escape(str(base))}[^,;:()\"'\n]*", message):
        words = match.group().split(" ")
        for count in range(len(words), 0, -1):
            candidate = Path(" ".join(words[:count]).rstrip(". "))
            if count == 1 or os.path.lexists(candidate):
                found.append(candidate)
                break
    return found


def campaign(
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
    script: str,
    arguments: tuple[str, ...],
    base: Path,
    *,
    prepare: Callable[[Path], Tree],
    consume: Callable[[Path], Iterator[tuple[str, str]]],
    superseded: Callable[[Path, bytes], bool] = lambda _relative, _data: False,
    exempt: Callable[[Entry, Tree], bool] = lambda _entry, _before: False,
    plans: Callable[[Produced], list[dict[str, object]]] | None = None,
    variants: Callable[[Mapping[str, object], Tree, Path, Sequence[str]], Sequence[Variant]]
    | None = None,
    edited_after: Callable[[Mapping[str, object]], bool] | None = None,
    every: int = 1,
    full: bool = False,
) -> Campaign:
    """Run the producer under every plan, and ``consume`` on each distinct state it left.

    ``prepare(base)`` lays out one scratch profile and returns what a person had
    there before anything ran. ``consume(base)`` runs this checkout's ordinary
    operations in it, yielding each step's name and what it said. After every
    step: the bytes of every entry that was there before the producer, that was
    ever at a public name, or that was edited, are still somewhere under the
    profile (in as many places as there were such entries holding them); and
    every path the step named exists. ``superseded(relative path,
    its bytes)`` says an entry is one a step rewrites by design (a store's own
    readable file), which ``consume`` then checks for itself: a step whose
    words start with ``FINDING`` is one. ``exempt(entry, the tree before the
    step)`` is the same with more to go on. ``plans`` replaces the usual plans
    (`single_faults`, `edits_while_public`, `meddling`) with its own; ``full``
    widens them (a `full_campaign` test), and edits every state afterwards
    in each of `EDITS`, not only those a kill leaves in one of them.
    ``edited_after(plan)`` chooses those states instead, and ``every`` takes
    one in so many of their files, for a producer whose every run is long.

    ``variants(plan, the state, its profile, calls)`` names what else to try on
    a state: each `Variant` gets the state made afresh. ``calls`` is what each
    filesystem call of this checkout's first step on it was given (`Watch`).
    """
    result = Campaign()
    plain = Job(base / "plain", {}, arguments)
    prepare(plain.base)
    (trace,) = run(source, script, [plain])
    assert trace.status == 0, trace.output + trace.errors[-2000:]
    assert trace.changes, "the released code was traced changing something"

    planned = (
        single_faults(trace, full=full)
        + edits_while_public(trace, full=full)
        + meddling(trace, full=full)
        if plans is None
        else plans(trace)
    )
    # The run with no fault first: what the released code leaves the ordinary way.
    jobs = [
        Job(base / f"a{number:05d}", plan, arguments) for number, plan in enumerate([{}, *planned])
    ]
    result.faults = len(planned)

    def examine(
        job: Job,
        first: Tree,
        made: Produced,
        edited: frozenset[int],
        label: str,
        variant: Variant | None = None,
    ) -> list[str]:
        calls: list[str] = []
        with monkeypatch.context() as scoped:
            scratch_profile(scoped, job.base)
            if variant is not None:
                edited |= variant.apply(job.base)
            before = tree(job.base)
            protected = (
                frozenset(entry.inode for entry in first.entries)
                | made.once_public
                | frozenset(inode for _call, _path, inode in made.edited)
                | edited
            )
            watch = (
                variant.watch(job.base)
                if variant is not None and variant.watch is not None
                else Watch()
                if variants is not None
                else None
            )
            if watch is not None:
                watch.__enter__()
            try:
                for step, (name, said) in enumerate(consume(job.base)):
                    if watch is not None:
                        calls = calls if step else list(watch.calls)
                        watch.restore()
                    told = _RANDOM.sub("*", said.replace(str(job.base), ""))
                    result.outcomes[f"{name}: {told[:170]}"] += 1
                    after = tree(job.base)
                    if said.startswith("FINDING"):
                        result.findings.append(f"{said} [{label}, at {name}]")
                    for entry in lost(
                        before,
                        after,
                        protected,
                        lambda entry: (
                            superseded(entry.path.relative_to(job.base), entry.data)
                            or exempt(entry, before)
                        ),
                    ):
                        result.findings.append(
                            f"LOST {label}: after {name}, the {entry.size} bytes of "
                            f"{entry.path.relative_to(job.base)} are nowhere (or in fewer places)"
                        )
                    for path in _named(said, job.base):
                        if not os.path.lexists(path):
                            result.findings.append(
                                f"SAID {label}: {name} names {path.relative_to(job.base)}, which "
                                f"isn't there: {said.replace(str(job.base), '')}"
                            )
            finally:
                if watch is not None:
                    watch.__exit__(None, None, None)
        return calls

    # Phase one: each plan once; one job per distinct state is examined.
    firsts = [prepare(job.base) for job in jobs]
    produced = run(source, script, jobs)
    distinct: dict[object, tuple[Job, tuple[tuple[str, int], ...]]] = {}
    wanted_variants: list[tuple[Job, Variant]] = []
    for job, first, made in zip(jobs, firsts, produced, strict=True):
        assert made.status in (0, KILLED), made.errors[-2000:]
        state = tree(job.base)
        found = shape(state, job.base)
        edited = tuple(sorted(_RANDOM.sub("*", path) for _call, path, _inode in made.edited))
        key = (found, edited, made.status)
        if key in distinct:
            continue
        distinct[key] = (job, found)
        result.states.append(found)
        calls = examine(job, first, made, frozenset(), f"{dict(job.plan)}")
        if variants is not None:
            wanted_variants += [
                (job, variant) for variant in variants(job.plan, state, job.base, calls)
            ]

    # Phase two: each distinct state again, once per file in it, with that file
    # edited in place by a person after the released code stopped. By default
    # only the states a kill leaves (what is there when nothing is running),
    # each file edited one of the `EDITS` in turn; in full, every state, each
    # file edited each way.
    again: list[tuple[Job, tuple[str, int], str]] = []
    chosen = edited_after if edited_after is not None else lambda plan: full or "kill" in plan
    files = 0
    for job, found in distinct.values():
        if not chosen(job.plan):
            continue
        for wanted in sorted({(name, size) for name, size in found if size > 0}):
            files += 1
            if files % every:
                continue
            for how in EDITS if full else (EDITS[len(again) % len(EDITS)],):
                again.append((Job(base / f"b{len(again):05d}", job.plan, arguments), wanted, how))
    result.edits_after = len(again)
    firsts = [prepare(job.base) for job, _wanted, _how in again]
    produced = run(source, script, [job for job, _wanted, _how in again])
    for (job, wanted, how), first, made in zip(again, firsts, produced, strict=True):
        matching = [
            entry
            for entry in tree(job.base).entries
            if shape(Tree((entry,)), job.base)[0] == wanted
        ]
        if not matching:
            continue  # a dated name that fell in another second: another state
        edit_in_place(matching[0].path, how)
        examine(
            job,
            first,
            made,
            frozenset({matching[0].inode}),
            f"{dict(job.plan)}, then a person edits {wanted[0]} ({how})",
        )

    # Phase three: each state a `Variant` was asked for, made afresh for it;
    # then whatever more each of those asks for, until none does.
    while wanted_variants:
        varied = [
            (Job(base / f"c{result.varied + number:05d}", job.plan, arguments), variant)
            for number, (job, variant) in enumerate(wanted_variants)
        ]
        result.varied += len(varied)
        wanted_variants = []
        firsts = [prepare(job.base) for job, _variant in varied]
        produced = run(source, script, [job for job, _variant in varied])
        for (job, variant), first, made in zip(varied, firsts, produced, strict=True):
            calls = examine(
                job, first, made, frozenset(), f"{dict(job.plan)}, {variant.label}", variant
            )
            if variant.more is not None:
                wanted_variants += [(job, further) for further in variant.more(calls)]
    return result


def round_trip(
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
    script: str,
    arguments: tuple[str, ...],
    base: Path,
    *,
    prepare: Callable[[Path], Tree],
    between: Callable[[Sequence[Path]], Sequence[Produced]],
    consume: Callable[[Path], Iterator[tuple[str, str]]],
    exempt_between: Callable[[Entry, Tree, Tree], bool] = lambda _entry, _before, _after: False,
    exempt: Callable[[Entry, Tree], bool] = lambda _entry, _before: False,
    judge: Callable[[Path, Tree, Tree, str], str] = lambda _base, _before, _after, _said: "",
    plans: Callable[[Produced], list[dict[str, object]]] | None = None,
    edited_after: int = 0,
    full: bool = False,
) -> Campaign:
    """A producer under every plan, another version's turn on what it left, then ``consume``.

    With ``edited_after`` (one file in so many), each state a kill left is made
    again and a person edits one of its files in place, one of `EDITS` in
    turn, before the other version's turn: that file is protected from then on.

    Going back and returning: ``script`` is run from ``source`` under each plan
    (the usual `single_faults`, or ``plans``); ``between(profiles)`` gives each
    distinct state to the other version, in one batch, and returns what it
    did; ``consume`` is this checkout's turn after it, as in `campaign`. After
    the other version's turn and after every step of ours, nothing that was
    there before, was ever public or was edited has lost its bytes, but for
    what ``exempt_between(entry, before, after)`` and ``exempt`` allow.
    ``judge(profile, before, after, what the other version said)`` adds its own
    words on the other version's turn: a finding if they start ``FINDING``.
    """
    result = Campaign()
    plain = Job(base / "plain", {}, arguments)
    prepare(plain.base)
    (trace,) = run(source, script, [plain])
    assert trace.status == 0, trace.output + trace.errors[-2000:]
    planned = single_faults(trace, full=full) if plans is None else plans(trace)
    jobs = [
        Job(base / f"a{number:05d}", plan, arguments) for number, plan in enumerate([{}, *planned])
    ]
    result.faults = len(planned)
    firsts = [prepare(job.base) for job in jobs]
    produced = run(source, script, jobs)
    chosen: list[tuple[Job, Tree, Produced, Tree]] = []
    seen: set[object] = set()
    for job, first, made in zip(jobs, firsts, produced, strict=True):
        assert made.status in (0, KILLED), made.errors[-2000:]
        state = tree(job.base)
        key = (shape(state, job.base), made.status)
        if key not in seen:
            seen.add(key)
            chosen.append((job, first, made, state))
            result.states.append(shape(state, job.base))
    labels = {job.base: f"{dict(job.plan)}" for job, _first, _made, _state in chosen}
    person: dict[Path, frozenset[int]] = {}
    if edited_after:
        again: list[tuple[Job, tuple[str, int], str]] = []
        files = 0
        for job, _first, _made, state in list(chosen):
            if "kill" not in job.plan:
                continue
            for wanted in sorted({pair for pair in shape(state, job.base) if pair[1] > 0}):
                files += 1
                if files % edited_after == 0:
                    how = EDITS[len(again) % len(EDITS)]
                    again.append(
                        (Job(base / f"b{len(again):05d}", job.plan, arguments), wanted, how)
                    )
        result.edits_after = len(again)
        firsts = [prepare(job.base) for job, _wanted, _how in again]
        produced = run(source, script, [job for job, _wanted, _how in again])
        for (job, wanted, how), first, made in zip(again, firsts, produced, strict=True):
            matching = [
                entry
                for entry in tree(job.base).entries
                if shape(Tree((entry,)), job.base)[0] == wanted
            ]
            if not matching:
                continue
            edit_in_place(matching[0].path, how)
            person[job.base] = frozenset({matching[0].inode})
            labels[job.base] = f"{dict(job.plan)}, then a person edits {wanted[0]} ({how})"
            chosen.append((job, first, made, tree(job.base)))
    turns = between([job.base for job, _first, _made, _state in chosen])
    for (job, first, made, before), turn in zip(chosen, turns, strict=True):
        label = labels[job.base]
        protected = (
            frozenset(entry.inode for entry in first.entries)
            | made.once_public
            | frozenset(inode for _call, _path, inode in made.edited)
            | person.get(job.base, frozenset())
        )
        after = tree(job.base)
        said = turn.output if turn.status == 0 else f"FINDING it ended {turn.status}: {turn.output}"
        said = judge(job.base, before, after, said) or said
        told = _RANDOM.sub("*", said.replace(str(job.base), ""))
        result.outcomes[f"the other version: {told[:200]}"] += 1
        if said.startswith("FINDING"):
            result.findings.append(f"{said} [{label}, at the other version's turn]")

        def spared(entry: Entry, before: Tree = before, after: Tree = after) -> bool:
            return exempt_between(entry, before, after)

        def kept(entry: Entry, after: Tree = after) -> bool:
            return exempt(entry, after)

        for entry in lost(before, after, protected, spared):
            result.findings.append(
                f"LOST {label}: after the other version's turn, the {entry.size} bytes of "
                f"{entry.path.relative_to(job.base)} are nowhere (or in fewer places)"
            )
        protected |= turn.once_public
        with monkeypatch.context() as scoped:
            scratch_profile(scoped, job.base)
            for name, said in consume(job.base):
                told = _RANDOM.sub("*", said.replace(str(job.base), ""))
                result.outcomes[f"{name}: {told[:170]}"] += 1
                now = tree(job.base)
                if said.startswith("FINDING"):
                    result.findings.append(f"{said} [{label}, at {name}]")
                for entry in lost(after, now, protected, kept):
                    result.findings.append(
                        f"LOST {label}: after {name}, the {entry.size} bytes of "
                        f"{entry.path.relative_to(job.base)} are nowhere (or in fewer places)"
                    )
                for path in _named(said, job.base):
                    if not os.path.lexists(path):
                        result.findings.append(
                            f"SAID {label}: {name} names {path.relative_to(job.base)}, which "
                            f"isn't there: {said.replace(str(job.base), '')}"
                        )
    return result
