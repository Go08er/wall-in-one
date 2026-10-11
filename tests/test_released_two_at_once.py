"""The released 0.2.2 and this version at once, against the same folders.

A person can have 0.2.2 running (its service, or a window) while this version
starts. Here a 0.2.2 operation is held alive at one of its filesystem calls
(`released_faults.paused`), with whatever it holds open or locked; this
checkout does what it ordinarily does there; and 0.2.2 is let go. Neither
may destroy a byte of the other's live files, and neither may wedge.

- A 0.2.2 download in progress while this version opens the download folder
  (its sweep), scans, and runs Tidy up, now: the download completes, with its
  media and its record as 0.2.2 wrote them.
- A 0.2.2 removal in progress while this version starts and replays: the
  removal completes as it would alone, and the next start has nothing to say.
- A 0.2.2 template install in progress while this version looks and
  reconciles: the install ends as it would alone, or refuses; the person's
  Noctalia settings keep every line; and this version's own install and
  uninstall still go through afterwards.

Known, and not probed past: this version's reconcile can discard a 0.2.2
transaction lock nobody holds at that moment, and if 0.2.2 then dies before
finishing, reconcile refuses until the person moves the leftovers. Without
that death nothing sticks, which is what the template case here confirms.
"""

from __future__ import annotations

import itertools
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests import released_faults as faults
from tests import test_released_leftovers_download as downloads
from tests import test_released_leftovers_removals as removals
from tests import test_released_leftovers_template as templates
from tests.released import released_source  # noqa: F401 -- a fixture
from wall_in_one import config, paths, tidy
from wall_in_one.library import scan
from wall_in_one.providers import download
from wall_in_one.providers.base import ProviderError
from wall_in_one.session import Session
from wall_in_one.theme import noctalia


def _held(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: str,
    arguments: tuple[str, ...],
    prepare: Callable[[Path], faults.Tree],
    *,
    full: bool,
) -> Iterator[tuple[str, Path, list[faults.Produced], faults.Produced]]:
    """0.2.2 held at each point in turn: yields inside the hold, then once more after.

    Each point gives ``("held", profile, [], alone)`` while 0.2.2 is stopped
    there, and ``("after", profile, [what it produced], alone)`` once it has
    been let go and has ended. ``alone`` is the same run with nobody else.
    A gate takes every sixth change; a full campaign every change and every
    fourth call besides (each point is a process of its own).
    """
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    plain = faults.Job(tmp_path / "plain", {}, arguments)
    prepare(plain.base)
    (alone,) = faults.run(released_source, script, [plain])
    assert alone.status == 0, alone.output + alone.errors[-2000:]
    points = (
        [call for number, call in enumerate(alone.calls) if call.kind == "change" or not number % 4]
        if full
        else alone.changes[::6]
    )
    for call in points:
        job = faults.Job(tmp_path / f"p{call.index:05d}", {}, arguments)
        prepare(job.base)
        with monkeypatch.context() as scoped:
            faults.scratch_profile(scoped, job.base)
            with faults.paused(released_source, script, job, call.index) as theirs:
                yield "held", job.base, theirs, alone
            yield "after", job.base, theirs, alone


def _ended_as_alone(theirs: list[faults.Produced], alone: faults.Produced) -> str:
    (made,) = theirs
    if "WEDGED" in made.errors:
        return "WEDGED: 0.2.2 never finished once let go"
    if made.status != alone.status or made.output.split()[:1] != alone.output.split()[:1]:
        return f"0.2.2 ended {made.status} saying {made.output[:200]!r}, not {alone.output[:60]!r}"
    return ""


def _download(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    full: bool,
) -> None:
    problems: list[str] = []
    points = 0
    for moment, base, theirs, alone in _held(
        released_source,
        tmp_path,
        monkeypatch,
        downloads.PRODUCER,
        (),
        downloads._prepare,
        full=full,
    ):
        root = Path(faults.profile_environment(base)["HOME"]) / "Wallpapers"
        if moment == "held":
            points += 1
            try:
                download.managed_directory(root, download.MOTIONBGS_LOCATION)
            except ProviderError as error:
                print(f"this version, while 0.2.2 downloads: REFUSED {error}")
            scan.scan((root,))
            found = tidy.plan(roots=(root,), now=time.time())
            for action in found.actions:
                if action.action in tidy.OFFERED and action.ready:
                    tidy.apply(action.action, action, roots=(root,))
            continue
        where = f"0.2.2's download, point {points}"
        if said := _ended_as_alone(theirs, alone):
            problems.append(f"{where}: {said}")
        media = root / "Wall-in-One" / "MotionBGS" / "aurora.mp4"
        record = media.with_name(media.name + ".motionbgs.json")
        if not media.is_file() or media.read_bytes() != b"validated media bytes":
            problems.append(f"LOST {where}: the downloaded media is not as 0.2.2 wrote it")
        if not record.is_file() or not record.stat().st_size:
            problems.append(f"LOST {where}: the download record is not there")
    print(f"\na 0.2.2 download with this version at the folder: {points} points, {len(problems)}")
    assert points and not problems, "\n".join(problems[:20])


def test_a_released_download_in_progress_survives_this_version_at_the_folder(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _download(released_source, tmp_path, monkeypatch, full=False)


@faults.full_campaign
def test_a_released_download_in_progress_survives_this_version_at_any_call(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _download(released_source, tmp_path, monkeypatch, full=True)


def _started() -> str:
    session = Session(config.load_strict(require_present=True))
    try:
        failures = session.retry_removals()
        session.refresh()
    finally:
        session.shutdown()
    return "; ".join(failures) or "nothing to say"


def _removal(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    full: bool,
) -> None:
    problems: list[str] = []
    said_while_held: set[str] = set()
    points = 0
    for moment, base, theirs, alone in _held(
        released_source,
        tmp_path,
        monkeypatch,
        removals.PRODUCER,
        ("remove",),
        removals._prepare,
        full=full,
    ):
        root = Path(faults.profile_environment(base)["HOME"]) / "Wallpapers"
        media = root / "Wall-in-One" / "MotionBGS" / "aurora.mp4"
        if moment == "held":
            points += 1
            try:
                said_while_held.add(_started().replace(str(base), "")[:90])
            except (config.ConfigError, OSError) as error:
                said_while_held.add(f"REFUSED {type(error).__name__}")
            continue
        where = f"0.2.2's removal, point {points}"
        if said := _ended_as_alone(theirs, alone):
            problems.append(f"{where}: {said}")
        if media.exists():
            problems.append(f"{where}: the media is still at its path after 0.2.2 finished")
        try:
            later = _started()
        except (config.ConfigError, OSError) as error:
            later = f"REFUSED {error}"
        if later != "nothing to say":
            problems.append(f"WEDGED {where}: the next start says {later[:200]}")
    print(f"\na 0.2.2 removal with this version starting: {points} points, {len(problems)}")
    print("   this version said, while it was held:", sorted(said_while_held))
    assert points and not problems, "\n".join(problems[:20])


def test_a_released_removal_in_progress_survives_this_version_starting(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _removal(released_source, tmp_path, monkeypatch, full=False)


@faults.full_campaign
def test_a_released_removal_in_progress_survives_this_version_starting_at_any_call(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _removal(released_source, tmp_path, monkeypatch, full=True)


def _template(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    full: bool,
) -> None:
    monkeypatch.setattr(noctalia, "reload_config", lambda: None)
    problems: list[str] = []
    outcomes: set[str] = set()
    points = 0
    for moment, base, theirs, _alone in _held(
        released_source,
        tmp_path,
        monkeypatch,
        templates.PRODUCER,
        ("install",),
        templates._prepare,
        full=full,
    ):
        if moment == "held":
            points += 1
            # This version looks, and reconciles: nothing more while 0.2.2 is mid-install.
            for name, said in itertools.islice(templates._consume(base), 2):
                outcomes.add(f"held, {name}: {said.replace(str(base), '')[:70]}")
                if said.startswith("FINDING"):
                    problems.append(f"point {points}, {name}, while 0.2.2 was held: {said}")
            continue
        where = f"0.2.2's install, point {points}"
        (made,) = theirs
        outcomes.add(f"0.2.2 then: {made.output.split(' ')[0]}")
        if "WEDGED" in made.errors or made.status != 0:
            problems.append(f"WEDGED {where}: 0.2.2 ended {made.status}: {made.output[:200]}")
        for name, said in templates._consume(base):
            outcomes.add(f"after, {name}: {said.replace(str(base), '')[:70]}")
            if said.startswith("FINDING"):
                problems.append(f"{where}, {name} afterwards: {said}")
            if name in ("install", "uninstall") and said.startswith("REFUSED"):
                problems.append(f"WEDGED {where}: this version's {name} afterwards: {said[:200]}")
        settings = paths.noctalia_settings_path().read_text(encoding="utf-8")
        for line in templates.SETTINGS.splitlines():
            if line and line not in settings:
                problems.append(f"LOST {where}: the person's settings line {line!r}")
    print(
        f"\na 0.2.2 template install with this version reconciling: {points} points, "
        f"{len(problems)} problems"
    )
    print("  ", "\n   ".join(sorted(outcomes)))
    assert points and not problems, "\n".join(problems[:20])


def test_a_released_template_install_in_progress_survives_this_version_reconciling(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _template(released_source, tmp_path, monkeypatch, full=False)


@faults.full_campaign
def test_a_released_template_install_in_progress_survives_reconciling_at_any_call(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _template(released_source, tmp_path, monkeypatch, full=True)
