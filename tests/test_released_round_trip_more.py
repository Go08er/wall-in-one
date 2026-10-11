"""Going back to 0.2.2 and returning, for stills, the template and the stores; and a person's edit.

`tests/test_released_round_trip.py` makes the trip for a download and a
removal. Here this checkout produces a still (a forced second one), a template
install and a store save under one fault at a time; the released 0.2.2 then
takes its ordinary turn (its scan, `ensure` and Tidy up; its own install; its
own save of the same store); and this checkout returns with the steps of its
startup probes. Each state a kill left is also made again with one file
edited by a person before going back. Nothing that was ever public or that a
person edited may lose a byte at either hop, each version must still read what
the other wrote, and this version must come back without a refusal it didn't
have a reason for.

Last, the known template lock case, with the crash: 0.2.2 is held in the
middle of an install, this version reconciles (which can discard a
transaction lock nobody holds just then), and 0.2.2 then dies instead of
finishing. Known: reconcile may refuse from then on. Pinned here: that is all
it is. The person's settings keep every line, and every refusal names what it
preserved.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import time
import types
from collections import Counter
from pathlib import Path

import pytest

from tests import released_faults as faults
from tests import test_released_leftovers_download as downloads
from tests import test_released_leftovers_stills as stills_probe
from tests import test_released_leftovers_stores as stores
from tests import test_released_leftovers_template as templates
from tests.released import released_source  # noqa: F401 -- a fixture
from tests.test_released_round_trip import (
    SOURCE,
    _archived_holding_bytes,
    _opened_a_day_later,
    _released_turn,
)
from wall_in_one import paths, tidy
from wall_in_one.library import stills
from wall_in_one.providers import download
from wall_in_one.theme import noctalia

LATER_SECONDS = 25 * 3600

#: The released 0.2.2's turn at a library folder: its scan, a still for the
#: video if it has none, its Tidy up a day later.
RELEASED_STILLS = r"""
import os
import time
import types
from pathlib import Path

from wall_in_one import file_io, tidy
from wall_in_one.library import scan, stills

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc0f01f00050001ff89993d1d0000000049454e44ae426082"
)


def _frame(video, target, seek, *, processes=None):
    with open(target, "wb") as sink:
        sink.write(PNG + b"the released frame")
    return ""


stills._run = _frame
stills.is_available = lambda: True
trace_filesystem((file_io, "_rename_noreplace"))


def operation():
    root = Path(os.environ["HOME"]) / "Wallpapers"
    said = []
    try:
        library = scan.scan((root,))
        hidden = sorted(
            item.path.name
            for item in library.items
            if any(part.startswith(".") for part in item.path.relative_to(root).parts)
        )
        video = next((item for item in library.items if item.is_moving), None)
        paired = video is not None and video.paired_still is not None
        said.append(f"scan: still {'paired' if paired else 'none'}" + (" HIDDEN" if hidden else ""))
        if video is not None:
            said.append("ensure: " + ("a still" if stills.ensure(video, root) else "none"))
        later = time.time() + 25 * 3600
        tidy.time = types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
        moved = []
        found = tidy.plan(roots=(root,), now=later)
        for action in found.actions:
            if action.action in tidy.OFFERED and action.ready:
                moved.append(tidy.apply(action.action, action, roots=(root,)).message)
        said.append("tidy: " + (" + ".join(moved) or "nothing offered"))
    except Exception as error:
        said.append(f"RAISED {type(error).__name__}: {error}")
    print(" / ".join(said), flush=True)


serve(operation)
"""


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


def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    later = time.time() + LATER_SECONDS
    monkeypatch.setattr(
        tidy, "time", types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
    )


def _profile(path: Path) -> Path:
    """The scratch profile a file of a campaign is in."""
    return next(parent for parent in path.parents if re.fullmatch(r"plain|[ab]\d{5}", parent.name))


def _said(base: Path, before: faults.Tree, after: faults.Tree, said: str) -> str:
    """0.2.2's turn, judged: it must not raise or adopt a hidden name; and what
    its Tidy up archived that holds bytes is said, since the bytes are kept."""
    if "RAISED" in said or "HIDDEN" in said:
        return f"FINDING {said}"
    archived = _archived_holding_bytes(base, before, after)
    return f"{said} [its Tidy up archived files that hold bytes]" if archived else ""


def test_a_still_survives_going_back_and_returning(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _quiet(monkeypatch)

    def frame(_video: Path, target: Path, _seek: float, **_keywords: object) -> str:
        Path(target).write_bytes(stills_probe.PNG + b"this version's frame")
        return ""

    monkeypatch.setattr(stills, "_run", frame)
    monkeypatch.setattr(stills, "is_available", lambda: True)
    full = bool(os.environ.get(faults.FULL))
    found = faults.round_trip(
        monkeypatch,
        SOURCE,
        stills_probe.PRODUCER,
        ("again",),
        tmp_path,
        prepare=stills_probe._prepare,
        between=lambda bases: faults.run(
            released_source, RELEASED_STILLS, [faults.Job(base) for base in bases]
        ),
        consume=stills_probe._consume,
        judge=_said,
        edited_after=1 if full else 4,
        full=full,
    )
    print("\n" + found.summary("a still: this version, 0.2.2, this version"))
    assert not found.findings, "\n".join(sorted(set(found.findings))[:30])


def test_a_template_install_survives_going_back_and_returning(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _quiet(monkeypatch)
    monkeypatch.setattr(noctalia, "reload_config", lambda: None)
    full = bool(os.environ.get(faults.FULL))
    found = faults.round_trip(
        monkeypatch,
        SOURCE,
        templates.PRODUCER,
        ("install",),
        tmp_path,
        prepare=templates._prepare,
        # 0.2.2's turn is its own install, which reconciles first.
        between=lambda bases: faults.run(
            released_source,
            templates.PRODUCER,
            [faults.Job(base, {}, ("install",)) for base in bases],
        ),
        consume=templates._consume,
        edited_after=1 if full else 6,
        full=full,
    )
    print("\n" + found.summary("a template install: this version, 0.2.2's install, this version"))
    assert not found.findings, "\n".join(sorted(set(found.findings))[:30])


def test_a_tidy_archive_survives_going_back_and_returning(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This version's Tidy up archives a leftover under faults; 0.2.2 then opens
    the folder a day later, scans and runs its own Tidy up; this version
    returns and does the same. What was archived keeps its bytes throughout,
    and 0.2.2's Tidy up neither raises on this version's archive nor moves it."""
    _quiet(monkeypatch)
    full = bool(os.environ.get(faults.FULL))
    found = faults.round_trip(
        monkeypatch,
        SOURCE,
        TIDY_UP,
        (),
        tmp_path,
        prepare=downloads._prepare,
        between=lambda bases: _released_turn(released_source, "downloads", bases),
        consume=_opened_a_day_later,
        # 0.2.x empties its own older names once they are a day old (the docs).
        exempt_between=lambda entry, _before, _after: entry.path.name.startswith(
            download.LEGACY_STAGING_PREFIXES
        ),
        judge=_said,
        edited_after=1 if full else 3,
        full=full,
    )
    print("\n" + found.summary("a Tidy up archive: this version, 0.2.2 a day later, this version"))
    assert any("tidy-archive" in name and size for state in found.states for name, size in state), (
        "one state had an archive holding bytes"
    )
    assert not found.findings, "\n".join(sorted(set(found.findings))[:30])


STORE_FILES = {"playlists": "playlists.json", "ui_prefs": "ui.toml"}


@pytest.mark.parametrize(
    "store",
    [
        "playlists",
        pytest.param("ui_prefs", marks=faults.full_campaign),
    ],
)
def test_a_store_save_survives_going_back_and_returning(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store: str,
) -> None:
    """This version saves into the newest format under faults; 0.2.2 saves a
    change of its own over what was left; this version opens and saves again."""
    _quiet(monkeypatch)
    consumer = stores._Consumer(store)
    full = bool(os.environ.get(faults.FULL))

    def judge(_base: Path, before: faults.Tree, after: faults.Tree, said: str) -> str:
        # What 0.2.2 could read, it keeps: a file a person damaged it moves
        # aside whole (its bytes are checked like any others) and starts anew.
        def readable(found: faults.Tree) -> bool:
            for entry in found.entries:
                if entry.path.name == STORE_FILES[store]:
                    try:
                        json.loads(entry.data)
                    except ValueError:
                        return False
                    return b"evening" in entry.data
            return False

        if store == "playlists" and readable(before) and not readable(after):
            return f"FINDING after 0.2.2's save the first playlist is not in the file: {said}"
        return ""

    found = faults.round_trip(
        monkeypatch,
        SOURCE,
        stores.PRODUCER,
        (store, "valid", "newer"),
        tmp_path,
        prepare=stores._prepare,
        between=lambda bases: faults.run(
            released_source,
            stores.PRODUCER,
            [faults.Job(base, {}, (store, "absent", "second")) for base in bases],
        ),
        consume=consumer.consume,
        # Each version rewrites the store's own file when it reads it cleanly.
        exempt_between=lambda entry, _before, _after: entry.path.name == STORE_FILES[store],
        exempt=lambda entry, _before: consumer.superseded(
            entry.path.relative_to(_profile(entry.path)), entry.data
        ),
        judge=judge,
        edited_after=1 if full else 4,
        full=full,
    )
    print("\n" + found.summary(f"a {store} save: this version, 0.2.2's save, this version"))
    assert not found.findings, "\n".join(sorted(set(found.findings))[:30])


def test_the_template_lock_case_with_the_crash_is_no_worse_than_known(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.2.2 held mid-install, this version reconciles, 0.2.2 dies before its
    next change. Beside it, the same death with nobody reconciling in between.
    Reconcile may refuse afterwards where it would not have (the known case);
    the person's settings keep every line either way, and a refusal names
    what it preserved."""
    _quiet(monkeypatch)
    monkeypatch.setattr(noctalia, "reload_config", lambda: None)
    plain = faults.Job(tmp_path / "plain", {}, ("install",))
    templates._prepare(plain.base)
    (alone,) = faults.run(released_source, templates.PRODUCER, [plain])
    changes = [call.index for call in alone.changes]
    every = 1 if os.environ.get(faults.FULL) else 4
    outcomes: Counter[str] = Counter()
    problems: list[str] = []

    def returned(base: Path) -> tuple[bool, list[str]]:
        """This version's steps after the death: is it stuck, and what it said."""
        said = [f"{name}: {words}" for name, words in templates._consume(base)]
        settings = paths.noctalia_settings_path().read_text(encoding="utf-8")
        lost = [line for line in templates.SETTINGS.splitlines() if line and line not in settings]
        # Mid-exchange the settings sit at the candidate's name, whole.
        whole = any(
            entry.data == templates.SETTINGS.encode() for entry in faults.tree(base).entries
        )
        if lost and not whole:
            problems.append(f"LOST {base.name}: the person's settings lines {lost}")
        problems.extend(f"{base.name}: {words}" for words in said if "FINDING" in words)
        stuck = any(words.startswith("install: REFUSED") for words in said)
        return stuck, said

    for held_at, dies_at in list(itertools.pairwise(changes))[::every]:
        between = faults.Job(tmp_path / f"h{held_at:05d}", {"kill": dies_at}, ("install",))
        control = faults.Job(tmp_path / f"c{held_at:05d}", {"kill": dies_at}, ("install",))
        for job in (between, control):
            templates._prepare(job.base)
        faults.run(released_source, templates.PRODUCER, [control])
        with monkeypatch.context() as scoped:
            faults.scratch_profile(scoped, control.base)
            stuck_alone, _said = returned(control.base)
        with monkeypatch.context() as scoped:
            faults.scratch_profile(scoped, between.base)
            with faults.paused(released_source, templates.PRODUCER, between, held_at) as theirs:
                for _name, _words in zip(range(2), templates._consume(between.base), strict=False):
                    pass  # this version looks, and reconciles, while 0.2.2 is held
            assert theirs[0].killed, theirs[0].output + theirs[0].errors[-1000:]
            stuck, said = returned(between.base)
        outcomes[
            f"{'stuck' if stuck else 'recovers'} after reconciling in between; "
            f"{'stuck' if stuck_alone else 'recovers'} without"
        ] += 1
        if stuck and not stuck_alone:
            refusal = next(words for words in said if words.startswith("install: REFUSED"))
            if "preserved" not in refusal and "another" not in refusal and "lock" not in refusal:
                problems.append(
                    f"{between.base.name}: stuck without saying what is kept: {refusal}"
                )
    print("\nthe template lock case, with the crash:")
    for outcome, count in sorted(outcomes.items()):
        print(f"  {count:4d}  {outcome}")
    assert outcomes and not problems, "\n".join(problems[:20])
