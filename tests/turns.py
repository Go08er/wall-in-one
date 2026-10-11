"""This checkout as a producer, and each version's ordinary turn, for the fault probes.

The startup probes run the released 0.2.2 as the producer and this checkout
as the consumer (`tests.released`, `tests.released_faults`). What several of
them share lives here, and is no test itself:

- `SOURCE`: this checkout's ``src``, to run the same producer scripts from.
- `REMOVAL`: a removal as this version's window runs it; `TIDY_UP`: this
  version's Tidy up archiving one of 0.2.x's older temporary names.
- `RELEASED_TURN`: what the released 0.2.2 ordinarily does at a start or at
  the download folder a day later, with its Tidy up. It is a producer too:
  what it leaves is what this checkout then meets.
- `_opened_a_day_later`: this checkout's own turn at the download folder.

Updates are one-way: nothing here runs an older version over what this one
left.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

from tests import released_faults as faults
from tests import test_released_leftovers_removals as removals
from wall_in_one import tidy
from wall_in_one.library import scan
from wall_in_one.providers import download
from wall_in_one.providers.base import ProviderError

#: This checkout, as a producer: the same scripts the released code runs.
SOURCE = Path(__file__).resolve().parents[1] / "src"
_RUN = '    result = session.prepare_removal_plan(item, trash=case == "trash").run()\n'
assert _RUN in removals.PRODUCER
#: The removal, as this version's window runs it: its plan begins with a dry
#: run, and one that can't be made raises with nothing written (0.2.2 had no
#: dry run, and its plan always returned).
REMOVAL = removals.PRODUCER.replace(
    _RUN,
    "    try:\n    "
    + _RUN
    + "    except Exception as error:\n"
    + '        print("REFUSED before anything was touched:", type(error).__name__, flush=True)\n'
    + "        return\n",
)
LATER_SECONDS = 25 * 3600
STORES = {"favourites.json", "pairings.json", "playlists.json", "pending-removals.json"}

#: The released 0.2.2's turn. ``downloads``: every file is a day old, and it
#: opens the download folder (its sweep). ``removals``: it starts (replay,
#: then the scan). Then, either way, its scan's verdict and its Tidy up.
RELEASED_TURN = r"""
import os
import time
import types
from pathlib import Path

from wall_in_one import config, file_io, tidy
from wall_in_one.library import scan
from wall_in_one.providers import download
from wall_in_one.providers.base import ProviderError
from wall_in_one.session import Session

trace_filesystem((file_io, "_rename_noreplace"))
LATER = 25 * 3600


def _tidied(root, later):
    said = []
    found = tidy.plan(roots=(root,), now=later)
    for action in found.actions:
        if action.action in tidy.OFFERED and action.ready:
            said.append(tidy.apply(action.action, action, roots=(root,)).message)
    return " + ".join(said) or "nothing offered"


def operation(what):
    root = Path(os.environ["HOME"]) / "Wallpapers"
    said = []
    disarm()
    if what == "downloads":
        old = time.time() - LATER
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                os.utime(path, (old, old))
    arm()
    try:
        if what == "downloads":
            try:
                download.managed_directory(root, download.MOTIONBGS_LOCATION)
                said.append("opened")
            except ProviderError as error:
                said.append(f"REFUSED {error}")
            library = scan.scan((root,))
        else:
            session = Session(config.load_strict(require_present=True))
            failures = session.retry_removals()
            library = session.refresh()
            said.append("replay: " + ("; ".join(failures) or "nothing to say"))
        hidden = sorted(
            item.path.name
            for item in library.items
            if any(part.startswith(".") for part in item.path.relative_to(root).parts)
        )
        said.append(f"scan: {len(library.items)}" + (f" HIDDEN {hidden}" if hidden else ""))
        later = time.time() + LATER
        tidy.time = types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
        said.append("tidy: " + _tidied(root, later))
    except Exception as error:
        said.append(f"RAISED {type(error).__name__}: {error}")
    print(" / ".join(said), flush=True)


serve(operation)
"""


def _root(base: Path) -> Path:
    return Path(faults.profile_environment(base)["HOME"]) / "Wallpapers"


def _is_new_name(path: Path, base: Path) -> bool:
    return any(
        part.startswith((".wall-in-one-v2-", download.COMMIT_PREFIX))
        for part in path.relative_to(base).parts
    )


def _profile_of(path: Path) -> Path:
    return next(parent for parent in path.parents if parent.name.startswith(("a0", "b0")))


def _tidied(root: Path) -> str:
    later = time.time() + LATER_SECONDS
    said = []
    found = tidy.plan(roots=(root,), now=later)
    for action in found.actions:
        if action.action in tidy.OFFERED and action.ready:
            said.append(tidy.apply(action.action, action, roots=(root,)).message)
    kept = sorted({kept.reason for kept in found.action(tidy.LEFTOVERS).kept})
    return " / ".join(said) + (f" [kept: {'; '.join(kept)}]" if kept else "")


def _opened_a_day_later(base: Path, *, tidy_up: bool = True) -> Iterator[tuple[str, str]]:
    """This checkout's turn at the download folder: its sweep, its scan, its Tidy up.

    A day later: this version's sweep takes only what was left alone for a day
    by both of a file's times, and a change time can't be set back. So besides
    dating every file a day back (what the released sweep goes by), the sweep
    that opening the folder runs is asked what it would do a day and an hour on.
    """
    root = _root(base)
    old = time.time() - LATER_SECONDS
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            os.utime(path, (old, old))
    try:
        directory, _marker = download.managed_directory(root, download.MOTIONBGS_LOCATION)
        download.recover_abandoned(
            directory, download.MOTIONBGS_LOCATION, now=time.time() + LATER_SECONDS
        )
        yield "open the folder", "opened"
    except ProviderError as error:
        yield "open the folder", f"REFUSED {error}"
    library = scan.scan((root,))
    hidden = [
        item.path.name
        for item in library.items
        if any(part.startswith(".") for part in item.path.relative_to(root).parts)
    ]
    yield "scan", f"FINDING hidden names became wallpapers: {hidden}" if hidden else "scanned"
    if not tidy_up:
        return
    try:
        yield "tidy up", _tidied(root)
    except (tidy.TidyError, OSError) as error:
        yield "tidy up", f"REFUSED {type(error).__name__}: {error}"


def _released_turn(source: Path, what: str, bases: Sequence[Path]) -> list[faults.Produced]:
    return faults.run(source, RELEASED_TURN, [faults.Job(base, {}, (what,)) for base in bases])


#: This checkout's Tidy up as the producer: a day-old file under one of 0.2.x's
#: older temporary names (which may have been public, so it is listed and
#: offered, never emptied) is archived, with whatever else is offered.
TIDY_UP = r"""
import os
import time
import types
from pathlib import Path

from wall_in_one import file_io, tidy
from wall_in_one.providers import download

trace_filesystem((file_io, "_rename_noreplace"))


def operation():
    root = Path(os.environ["HOME"]) / "Wallpapers"
    disarm()
    directory, _marker = download.managed_directory(root, download.MOTIONBGS_LOCATION)
    stage = directory / ".wall-in-one-media-stage-abcdefgh"
    stage.write_bytes(b"what 0.2.x left here, which a person may have edited")
    old = time.time() - 25 * 3600
    os.utime(stage, (old, old))
    had(stage)
    later = time.time() + 25 * 3600
    tidy.time = types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
    arm()
    said = []
    try:
        found = tidy.plan(roots=(root,), now=later)
        for action in found.actions:
            if action.action in tidy.OFFERED and action.ready:
                said.append(tidy.apply(action.action, action, roots=(root,)).message)
        print("DONE", " + ".join(said) or "nothing offered", flush=True)
    except Exception as error:
        print("REFUSED", type(error).__name__, error, flush=True)


serve(operation)
"""
