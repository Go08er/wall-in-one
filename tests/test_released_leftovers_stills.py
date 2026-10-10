"""What the released 0.2.2 leaves of making a still, and this version after it.

Review rule 4, run rather than read (AB-1's class, outside downloads). The
released `library.stills` is the producer, in a child (`tests.released`): it
takes a still from a video into the managed ``Automatic Stills`` folder and
records the pairing beside the video; or does so again, forced, over a still
that is already there (which it first moves into a removal claim folder, and
discards once the new one is public). Only ffmpeg is stood in for: the frame
is written to the output the released code opened. Each run takes one fault
(`tests.released_faults`), and then a person may edit any one file it left:
the still, the pairing record, a temporary, the claimed prior still.

This checkout is the consumer, doing what it ordinarily does next: it scans
the library, makes sure the video has a still (which reuses one or takes
another), runs Tidy up over the library folder, and scans again. Nothing that
was ever public or that a person edited may lose a byte, wherever it was
left; nothing left behind becomes a wallpaper; and what Tidy up says about a
leftover is true of it.
"""

from __future__ import annotations

import os
import time
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests import released_faults as faults
from tests.released import released_source  # noqa: F401 -- a fixture
from wall_in_one import tidy
from wall_in_one.library import scan, stills
from wall_in_one.library.model import Kind

#: A real, tiny PNG: what the stand-in for ffmpeg writes.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc0f01f00050001ff89993d1d0000000049454e44ae426082"
)

#: The released producer. Its job's argument: ``first`` (no still yet) or
#: ``again`` (a forced second still over the one the released code made).
PRODUCER = r"""
import os
from pathlib import Path

from wall_in_one import file_io
from wall_in_one.library import stills

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc0f01f00050001ff89993d1d0000000049454e44ae426082"
)
frames = [0]


def _frame(video, target, seek, *, processes=None):
    # ffmpeg's part, and only that: one frame into the output it was given.
    frames[0] += 1
    with open(target, "wb") as sink:
        sink.write(PNG + b"frame %d" % frames[0])
    return ""


stills._run = _frame
stills.is_available = lambda: True
trace_filesystem((file_io, "_rename_noreplace"))


def operation(case):
    root = Path(os.environ["HOME"]) / "Wallpapers"
    video = root / "clip.mp4"
    try:
        if case == "again":
            disarm()
            stills.generate(video, root)
            arm()
            made = stills.generate(video, root, force=True)
        else:
            made = stills.generate(video, root)
        print("DONE", made, flush=True)
    except stills.StillError as error:
        print("REFUSED", error, flush=True)
    except OSError as error:
        print("ERROR", error, flush=True)


serve(operation)
"""

#: A day and an hour: past every wait Tidy up has.
LATER_SECONDS = 25 * 3600


def _root(base: Path) -> Path:
    return Path(faults.profile_environment(base)["HOME"]) / "Wallpapers"


def _prepare(base: Path) -> faults.Tree:
    """A profile with one video in its library folder."""
    for value in faults.profile_environment(base).values():
        Path(value).mkdir(parents=True, exist_ok=True)
    root = _root(base)
    root.mkdir()
    (root / "clip.mp4").write_bytes(b"a video, as far as anything here looks")
    return faults.tree(base)


def _consume(base: Path) -> Iterator[tuple[str, str]]:
    root = _root(base)
    later = time.time() + LATER_SECONDS

    def scanned() -> str:
        library = scan.scan((root,))
        strays = [
            item.path
            for item in library.items
            if any(part.startswith(".") for part in item.path.relative_to(root).parts)
        ]
        if strays:
            return f"FINDING a leftover became a wallpaper: {strays}"
        still = next((item.paired_still for item in library.items if item.is_moving), None)
        if still is not None and not still.is_file():
            return f"FINDING the video's still {still} isn't there"
        kinds = sorted(item.kind.value for item in library.items)
        return f"{kinds}, still {'paired' if still is not None else 'none'}"

    def ensured() -> str:
        library = scan.scan((root,))
        (video,) = [item for item in library.items if item.kind is Kind.VIDEO]
        made = stills.ensure(video, root)
        if made is not None and not made.is_file():
            return f"FINDING ensure returned {made}, which isn't there"
        if made is None:
            # The forgiving entry point says nothing; ask the strict one why.
            try:
                stills.generate(video.path, root)
            except stills.StillError as error:
                return f"no still: {error}"
        return "a still" if made is not None else "no still"

    def tidy_up() -> str:
        said = []
        found = tidy.plan(roots=(root,), now=later)
        for action in found.actions:
            if action.action in tidy.OFFERED and action.ready:
                said.append(tidy.apply(action.action, action, roots=(root,)).message)
        kept = sorted({kept.reason for kept in found.action(tidy.LEFTOVERS).kept})
        return " / ".join(said) + (f" [kept: {'; '.join(kept)}]" if kept else "")

    for name, step in (
        ("scan", scanned),
        ("ensure", ensured),
        ("tidy up", tidy_up),
        ("scan again", scanned),
    ):
        try:
            yield name, step()
        except (stills.StillError, tidy.TidyError, OSError) as error:
            yield name, f"REFUSED {type(error).__name__}: {error}"


def _campaign(
    case: str,
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    full: bool,
) -> None:
    # Hundreds of profiles: durability isn't what this probes.
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    later = time.time() + LATER_SECONDS
    monkeypatch.setattr(
        tidy, "time", types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
    )

    def frame(_video: Path, target: Path, _seek: float, **_keywords: object) -> str:
        Path(target).write_bytes(PNG + b"this version's frame")
        return ""

    monkeypatch.setattr(stills, "_run", frame)
    monkeypatch.setattr(stills, "is_available", lambda: True)

    found = faults.campaign(
        monkeypatch,
        released_source,
        PRODUCER,
        (case,),
        tmp_path,
        prepare=_prepare,
        consume=_consume,
        full=full,
    )

    print("\n" + found.summary(f"stills, {case}"))
    if case == "again":
        # The state this is for was among them: the prior still, claimed, with
        # the process gone before it was discarded or put back.
        assert any(
            any(".wall-in-one-retained/" in name and size > len(PNG) for name, size in state)
            for state in found.states
        ), "no state had the prior still in its claim folder"
    assert not found.findings, "\n".join(sorted(set(found.findings))[:40])


def test_nothing_the_released_still_maker_left_is_lost(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What a gate runs: the forced second still (its states include a first
    still's), under the bounded plans."""
    _campaign("again", released_source, tmp_path, monkeypatch, full=False)


@faults.full_campaign
@pytest.mark.parametrize("case", ["first", "again"])
def test_nothing_the_released_still_maker_left_is_lost_under_any_plan(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    _campaign(case, released_source, tmp_path, monkeypatch, full=True)
