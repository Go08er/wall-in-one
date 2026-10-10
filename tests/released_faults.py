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
    in place (same inode, same length), as a person or another program would.
    With ``"fail"`` as well, a later call then fails: the released code finds
    its publication unverifiable and moves the edited entry wherever it does.

``{"meddle": n, "which": k, "how": "edit"}``
    Just before call ``n``, the ``k``-th file then in the profile is edited in
    place (``"how": "replace"``: rewritten under a new inode, as a program that
    saves atomically does), and the released code carries on: a person, or
    Noctalia, changing a file while the released code is in the middle of it.

While armed, the tracer numbers every filesystem call the released
``wall_in_one`` makes (never the import system's), and records every inode
that was ever at a public name (one with no leading dot): that, and whatever
a person edited, is what must outlive any cleanup. ``fsync`` is numbered, and
can fail, but is not carried out: a killed process loses nothing the kernel
has, and a power cut is not something a test can stage.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
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
    if kind == "change" and _PLAN.get("kill") == index:
        emit_trace()
        _os._exit(77)
    if _PLAN.get("fail") == index:
        number = getattr(_errno, _PLAN.get("errno", "EIO"))
        raise OSError(number, "injected one-shot " + _os.strerror(number), where)
    return index


def _flipped(data):
    at = next((i for i, byte in enumerate(data) if chr(byte).isascii() and chr(byte).isalpha()), 0)
    return at, bytes([(data or b" ")[at] ^ 0x20])


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
    with open(path, "rb") as source:
        data = source.read()
    at, byte = _flipped(data)
    if _PLAN.get("how") == "replace":
        fresh = path + ".rewritten-by-someone"
        with open(fresh, "wb") as sink:
            sink.write(data[:at] + byte + data[at + 1 :])
        _os.chmod(fresh, _os.stat(path).st_mode & 0o777)
        _REAL["replace"](fresh, path)
    else:
        descriptor = _REAL["open"](path, _os.O_RDWR | _os.O_NOFOLLOW)
        try:
            _os.pwrite(descriptor, byte, at)
        finally:
            _os.close(descriptor)
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
        descriptor = _REAL["open"](path, _os.O_RDWR | _os.O_NOFOLLOW)
        try:
            at, byte = _flipped(_os.pread(descriptor, status.st_size, 0))
            _os.pwrite(descriptor, byte, at)
        finally:
            _os.close(descriptor)
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
    source: Path, script: str, jobs: Sequence[Job], *, timeout: float = 300.0
) -> list[Produced]:
    """Run each job in a fork of one released child; what each produced, in order.

    ``script`` (after `TRACER`) imports the released modules, calls
    ``trace_filesystem`` and ends with ``serve(operation)``.
    """
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    requested = json.dumps(
        [
            {
                "plan": dict(job.plan),
                "environment": profile_environment(job.base),
                "arguments": list(job.arguments),
            }
            for job in jobs
        ]
    )
    done = subprocess.run(
        [sys.executable, "-c", released._PREAMBLE + TRACER + script, str(source)],
        cwd=source,
        env=environment,
        input=requested,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    produced: list[Produced] = []
    lines: list[str] = []
    traced: dict[str, Any] = {}
    for line in done.stdout.splitlines():
        if line.startswith("JOB "):
            lines, traced = [], {}
        elif line.startswith("TRACE "):
            traced = json.loads(line.removeprefix("TRACE "))
        elif line.startswith("END "):
            produced.append(
                Produced(
                    status=int(line.split()[2]),
                    output="\n".join(lines),
                    errors=done.stderr,
                    calls=tuple(Call(*call) for call in traced.get("calls", ())),
                    public=tuple((c, path, inode) for c, path, inode in traced.get("public", ())),
                    edited=tuple((c, path, inode) for c, path, inode in traced.get("edited", ())),
                )
            )
        else:
            lines.append(line)
    assert len(produced) == len(jobs), done.stdout[-2000:] + done.stderr[-4000:]
    return produced


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
            data = path.read_bytes()
            found.append(
                Entry(path, status.st_ino, status.st_size, hashlib.sha256(data).hexdigest(), data)
            )
    return Tree(tuple(found))


def edit_in_place(path: Path) -> None:
    """Change one byte of ``path`` in place: the same inode, the same length.

    The case of its first letter (a comment's or a key's, in a text file),
    as the tracer's own edit does; its first byte if it has no letter.
    """
    with path.open("r+b") as handle:
        data = handle.read() or b" "
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
    the released code finds what it published changed under it."""
    window = 24 if full else 8
    plans: list[dict[str, object]] = []
    for published, _path, _inode in trace.public:
        plans.append({"edit": published})
        later = [call for call in trace.calls if published < call.index <= published + window]
        plans += [{"edit": published, "fail": call.index, "errno": "EIO"} for call in later]
        plans += [
            {"edit": published, "kill": call.index} for call in later if call.kind == "change"
        ]
    return plans


def meddling(trace: Produced, *, full: bool) -> list[dict[str, object]]:
    """Someone else changing one of the files then there, just before each change.

    Public names come first. Bounded: the first file, edited in place. In
    full: the first eight, each also rewritten under a new inode.
    """
    plans: list[dict[str, object]] = []
    for call in trace.changes:
        for which in range(8 if full else 1):
            plans.append({"meddle": call.index, "which": which, "how": "edit"})
            if full:
                plans.append({"meddle": call.index, "which": which, "how": "replace"})
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


# -- a campaign: every leftover state, then this checkout's code on each ------------------------


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

    def summary(self, title: str) -> str:
        lines = [
            f"{title}: {self.faults} faults, {len(self.states)} distinct states, "
            f"{self.edits_after} more with one file edited afterwards"
        ]
        lines += [f"  {count:5d}  {said}" for said, count in sorted(self.outcomes.items())]
        return "\n".join(lines)


def _named(message: str, base: Path) -> list[Path]:
    """Every path under ``base`` that ``message`` names."""
    found = re.findall(rf"{re.escape(str(base))}[^\s,;:()\"']*", message)
    return [Path(text.rstrip(".")) for text in found]


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
    plans: Callable[[Produced], list[dict[str, object]]] | None = None,
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
    words start with ``FINDING`` is one. ``plans`` replaces the usual plans
    (`single_faults`, `edits_while_public`, `meddling`) with its own; ``full``
    widens them (a `full_campaign` test), and edits every state afterwards,
    not only those a kill leaves.
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
    jobs = [Job(base / f"a{number:05d}", plan, arguments) for number, plan in enumerate(planned)]
    result.faults = len(jobs)

    def examine(job: Job, first: Tree, made: Produced, edited: frozenset[int], label: str) -> None:
        with monkeypatch.context() as scoped:
            scratch_profile(scoped, job.base)
            before = tree(job.base)
            protected = (
                frozenset(entry.inode for entry in first.entries)
                | made.once_public
                | frozenset(inode for _call, _path, inode in made.edited)
                | edited
            )
            for name, said in consume(job.base):
                told = _RANDOM.sub("*", said.replace(str(job.base), ""))
                result.outcomes[f"{name}: {told[:170]}"] += 1
                after = tree(job.base)
                if said.startswith("FINDING"):
                    result.findings.append(f"{said} [{label}, at {name}]")
                for entry in lost(
                    before,
                    after,
                    protected,
                    lambda entry: superseded(entry.path.relative_to(job.base), entry.data),
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

    # Phase one: each plan once; one job per distinct state is examined.
    firsts = [prepare(job.base) for job in jobs]
    produced = run(source, script, jobs)
    distinct: dict[object, tuple[Job, tuple[tuple[str, int], ...]]] = {}
    for job, first, made in zip(jobs, firsts, produced, strict=True):
        assert made.status in (0, KILLED), made.errors[-2000:]
        found = shape(tree(job.base), job.base)
        edited = tuple(sorted(_RANDOM.sub("*", path) for _call, path, _inode in made.edited))
        key = (found, edited, made.status)
        if key in distinct:
            continue
        distinct[key] = (job, found)
        result.states.append(found)
        examine(job, first, made, frozenset(), f"{dict(job.plan)}")

    # Phase two: each distinct state again, once per file in it, with that file
    # edited in place by a person after the released code stopped. By default
    # only the states a kill leaves: what is there when nothing is running.
    again = [
        (Job(base / f"b{number:05d}", job.plan, arguments), wanted)
        for number, (job, wanted) in enumerate(
            (job, wanted)
            for job, found in distinct.values()
            if full or "kill" in job.plan
            for wanted in sorted({(name, size) for name, size in found if size > 0})
        )
    ]
    result.edits_after = len(again)
    firsts = [prepare(job.base) for job, _wanted in again]
    produced = run(source, script, [job for job, _wanted in again])
    for (job, wanted), first, made in zip(again, firsts, produced, strict=True):
        matching = [
            entry
            for entry in tree(job.base).entries
            if shape(Tree((entry,)), job.base)[0] == wanted
        ]
        if not matching:
            continue  # a dated name that fell in another second: another state
        edit_in_place(matching[0].path)
        examine(
            job,
            first,
            made,
            frozenset({matching[0].inode}),
            f"{dict(job.plan)}, then a person edits {wanted[0]}",
        )
    return result
