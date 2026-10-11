"""What the released 0.2.2 leaves of a removal it didn't finish, and this version's start after it.

Review rule 4, run rather than read (AB-1's class, at startup). Removing a
wallpaper journals an intent, withdraws the download record (the authority to
delete), moves the media into a claim folder named after the intent, deletes
it there, discards its companions (the still, the selector) and marks the
intent committed; moving one to the trash journals the same intent around a
move. The released removal plan is the producer here, in a child
(`tests.released`), exactly as the released window ran it: a video the
released code downloaded and took a still of, removed; and a video of the
person's own, moved to the trash. Each run takes one fault
(`tests.released_faults`), and then a person may edit any one file it left.

This checkout is the consumer, starting twice (each start replays the journal
before it scans) and then running Tidy up. A 0.2.2 intent holds no claim on
the download record, which the released code had already withdrawn; replay
is pinned here as it is today. What must hold:

- Media that is at its own path when this version starts is there afterwards:
  replay finishes only what the released code had already moved into its
  claim, and deletes there only the exact file the intent names.
- The companions and the download record go only with a removal that
  finished, never while the media is back at its path.
- Nothing a person edited, and nothing in the trash, loses a byte.
- Every path a replay message names exists.
- A 0.2.2 intent holds no proof of the wallpaper's own files (the still, the
  selector) either: finishing its media claim takes neither, and says once
  that they were left.

And one transition more, on every state that has the media in its claim
(review finding AD-1): before this version starts, a download finishes at
the wallpaper's vacant path, with its record, and with a still and selector
under the same names as the removed wallpaper's (its own new ones, or the
old ones kept in place). All of it is the new wallpaper's, and none of it
may go when the old removal is replayed. The same campaign is run with this
version as the producer, whose intents do hold that proof
(`tests/test_removal_replay_companions.py` has each case on its own).
"""

from __future__ import annotations

import functools
import json
import os
import time
import types
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from tests import released_faults as faults
from tests import test_removal_replay_companions as companions
from tests.released import released_source  # noqa: F401 -- a fixture
from wall_in_one import config, file_io, tidy
from wall_in_one.library import favourites, pairings, playlists, removals
from wall_in_one.session import Session

MEDIA = b"validated media bytes"
OWN = b"a video of the person's own, as far as anything here looks"
#: In the name of the download record, which is not one of the wallpaper's own files.
RECORD_SUFFIX = ".motionbgs.json"

#: The released producer. Its job's argument: ``remove`` (a video the released
#: code downloaded) or ``trash`` (a video of the person's own).
PRODUCER = r"""
import hashlib
import os
from pathlib import Path

from wall_in_one import config, file_io
from wall_in_one.library import stills
from wall_in_one.providers import download, http
from wall_in_one.session import Session

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc0f01f00050001ff89993d1d0000000049454e44ae426082"
)
MEDIA = b"validated media bytes"


def _frame(video, target, seek, *, processes=None):
    # ffmpeg's part, and only that: one frame into the output it was given.
    with open(target, "wb") as sink:
        sink.write(PNG + b"a frame")
    return ""


stills._run = _frame
stills.is_available = lambda: True
trace_filesystem((file_io, "_rename_noreplace"))


def _downloaded(root):
    # The released download, to the end: the media, its record, the marker.
    location = download.MOTIONBGS_LOCATION
    directory, _marker = download.managed_directory(root, location)
    descriptor, staged = download._mkstemp(
        prefix=download.MEDIA_STAGING_PREFIX, directory=directory
    )
    with os.fdopen(descriptor, "wb") as sink:
        sink.write(MEDIA)
    destination = directory / "aurora.mp4"
    provenance = download.encode_sidecar(
        {
            "schema": 1,
            "plugin": "goober/wall-in-one",
            "provider": "MotionBGS",
            "path": str(Path(os.fspath(destination))),
            "bytes": len(MEDIA),
            "sha256": hashlib.sha256(MEDIA).hexdigest(),
        }
    )
    transfer = http.Transfer(
        url="https://motionbgs.com/media/42/aurora.mp4",
        status=200,
        content_type="video/mp4",
        size=len(MEDIA),
        path=staged,
    )
    with transfer:
        download.install(transfer.path, destination, location.sidecar_suffix, provenance)
    return destination


def operation(case):
    root = Path(os.environ["HOME"]) / "Wallpapers"
    disarm()
    settings = config.Settings(roots=(root,), scan_workshop=False)
    config.save(settings)
    media = _downloaded(root) if case == "remove" else root / "mine.mp4"
    stills.generate(media, root)
    session = Session(settings)
    session.refresh()
    item = next(item for item in session.library.items if item.path == media)
    had(
        *(
            Path(directory) / name
            for directory, _folders, names in os.walk(root)
            for name in names
        )
    )
    arm()
    result = session.prepare_removal_plan(item, trash=case == "trash").run()
    print(
        "DONE" if result.committed else "REFUSED",
        result.error_message,
        list(result.cleanup_failures),
        flush=True,
    )


serve(operation)
"""

#: This checkout as the producer of the same removal, as its window runs it:
#: its plan begins with a dry run, and one that can't be made raises with
#: nothing written (0.2.2 had no dry run, and its plan always returned).
THIS = Path(__file__).resolve().parents[1] / "src"
_RUN = '    result = session.prepare_removal_plan(item, trash=case == "trash").run()\n'
assert _RUN in PRODUCER
THIS_PRODUCER = PRODUCER.replace(
    _RUN,
    "    try:\n    "
    + _RUN
    + "    except Exception as error:\n"
    + '        print("REFUSED before anything was touched:", type(error).__name__, flush=True)\n'
    + "        return\n",
)

#: A day and an hour: past every wait Tidy up has.
LATER_SECONDS = 25 * 3600


def _root(base: Path) -> Path:
    return Path(faults.profile_environment(base)["HOME"]) / "Wallpapers"


def _prepare(base: Path) -> faults.Tree:
    """A profile with a library folder, and one video of the person's own in it."""
    for value in faults.profile_environment(base).values():
        Path(value).mkdir(parents=True, exist_ok=True)
    root = _root(base)
    root.mkdir()
    (root / "mine.mp4").write_bytes(OWN)
    return faults.tree(base)


class _Consumer:
    """This checkout's starts on one state, and what a finished removal may take.

    ``unproven``: the producer's intents hold no proof of the wallpaper's own
    files (0.2.2). A start may then finish the media claim and nothing
    beside it: the still and the selector stay.
    """

    def __init__(self, case: str, *, unproven: bool = False) -> None:
        self.case = case
        self.unproven = unproven
        #: (path, bytes) of each entry a start was entitled to destroy.
        self.allowed: set[tuple[Path, bytes]] = set()

    def media(self, base: Path) -> Path:
        root = _root(base)
        return (
            root / "Wall-in-One" / "MotionBGS" / "aurora.mp4"
            if self.case == "remove"
            else (root / "mine.mp4")
        )

    def exempt(self, entry: faults.Entry, _before: faults.Tree) -> bool:
        return (entry.path, entry.data) in self.allowed or _is_a_store(entry.path)

    def _beside(self, base: Path, found: faults.Tree) -> list[faults.Entry]:
        """What the app wrote for the media: its record, its selector, its still."""
        media = self.media(base)
        return [
            entry
            for entry in found.entries
            if entry.path != media
            and (
                entry.path.name.startswith(media.name + ".")
                or (entry.path.parent.name == "Automatic Stills" and entry.path.suffix == ".png")
            )
        ]

    def _start(self, base: Path) -> str:
        """One start: the journal replayed, then the library scanned."""
        root = _root(base)
        media = self.media(base)
        original = MEDIA if self.case == "remove" else OWN
        before = faults.tree(base)
        was_there = media.is_file() and media.read_bytes() == original
        session = Session(config.load_strict(require_present=True))
        try:
            failures = session.retry_removals()
            # What the replay finished and left in place on purpose is said
            # too, once: every path it names is checked like any other. (Read
            # loosely, so that a version before it reports what it lost.)
            left: tuple[str, ...] = getattr(session, "removal_replay_left", ())
            session.refresh()
        finally:
            session.shutdown()
        after = faults.tree(base)
        is_there = media.is_file()

        if was_there and (not is_there or media.read_bytes() != original):
            return (
                f"FINDING {media.relative_to(base)} was at its own path, and this start removed it"
            )
        if not was_there:
            # The wallpaper is not at its path: what the producer had claimed
            # for deletion (the exact media, under its claim) may be finished,
            # also when something else is at that path by now. Its download
            # record goes with it from its own claim, where this version (not
            # 0.2.2) keeps it whole until the removal's fate is known.
            self.allowed |= {
                (entry.path, entry.data)
                for entry in before.entries
                if ".wall-in-one-removal" in str(entry.path)
                and (entry.data == original or _is_the_record_of(entry.data, media))
            }
        if not is_there:
            # Nothing is at its path: the removal is finished, and what goes
            # with the wallpaper may go, where the intent can prove it.
            if not self.unproven:
                self.allowed |= {(entry.path, entry.data) for entry in self._beside(base, before)}
        else:
            for entry in self._beside(base, before):
                if not entry.path.is_file() or entry.path.read_bytes() != entry.data:
                    return (
                        f"FINDING {entry.path.relative_to(base)} went, though "
                        f"{media.relative_to(base)} is at its own path"
                    )
        if any("left untouched" in failure for failure in failures):
            changed = sorted(
                str(entry.path.relative_to(base))
                for entry in before.entries
                if root in entry.path.parents and after.at(entry.path) != entry
            )
            if changed:
                return f"FINDING said left untouched, and changed {changed}"
        # A claim's message names the removal by the media's own path, and
        # says where the claim is: only the second is somewhere a file is.
        said = "; ".join((*failures, *left)).replace(f"claim for {media}", "claim for it")
        said = said or "nothing to say"
        return f"media {'there' if is_there else 'gone'}: {said}"

    def consume(self, base: Path) -> Iterator[tuple[str, str]]:
        root = _root(base)
        self.allowed.clear()
        later = time.time() + LATER_SECONDS

        def tidy_up() -> str:
            said = []
            found = tidy.plan(roots=(root,), now=later)
            for action in found.actions:
                if action.action in tidy.OFFERED and action.ready:
                    said.append(tidy.apply(action.action, action, roots=(root,)).message)
            kept = sorted({kept.reason for kept in found.action(tidy.LEFTOVERS).kept})
            return " / ".join(said) + (f" [kept: {'; '.join(kept)}]" if kept else "")

        start = functools.partial(self._start, base)
        for name, step in (("start", start), ("start again", start), ("tidy up", tidy_up)):
            try:
                yield name, step()
            except (config.ConfigError, tidy.TidyError, OSError) as error:
                yield name, f"REFUSED {type(error).__name__}: {error}"


def _is_the_record_of(data: bytes, media: Path) -> bool:
    """Whether ``data`` is a download record naming ``media``, as the producers here write it."""
    try:
        record = json.loads(data)
    except ValueError:
        return False
    return isinstance(record, dict) and record.get("path") == str(media) and "sha256" in record


def _is_a_store(path: Path) -> bool:
    """A store's own file, or the journal: a finished removal takes its entry out."""
    return path in {module.state_path() for module in (favourites, pairings, playlists, removals)}


def _followed_by_a_download(
    consumer: _Consumer, drawn: list[bytes], *, full: bool
) -> Callable[[Mapping[str, object], faults.Tree, Path, Sequence[str]], Sequence[faults.Variant]]:
    """On a state with the media in its claim: a download finishes at its path first.

    Twice: the new wallpaper's capture writes its own still and selector (a
    person had cleared the old ones out), and it keeps the removed
    wallpaper's in place as its own. Whatever is the new wallpaper's then is
    as protected as what a person edited. A gate takes the first two such
    states; in full, every one.
    """
    taken = [0]

    def downloads(base: Path, *, recaptured: bool) -> frozenset[int]:
        drawn[0] = b"a frame of the old video"
        mine = companions.a_download_finishes(_root(base), drawn, recaptured=recaptured)
        return frozenset(path.stat().st_ino for path in mine)

    def variants(
        _plan: Mapping[str, object], state: faults.Tree, base: Path, _calls: Sequence[str]
    ) -> Sequence[faults.Variant]:
        claimed = any(
            entry.data == MEDIA and file_io.DELETION_CLAIM_PREFIX in str(entry.path)
            for entry in state.entries
        )
        own = [
            entry for entry in consumer._beside(base, state) if RECORD_SUFFIX not in entry.path.name
        ]
        if not claimed or state.at(consumer.media(base)) is not None or len(own) != 2:
            return ()  # not waiting in its claim, or a person has been at its own files
        taken[0] += 1
        if not full and taken[0] > 2:
            return ()
        return [
            faults.Variant(
                f"then a download finishes at the same path, {label}",
                functools.partial(downloads, recaptured=recaptured),
            )
            for label, recaptured in (
                ("with a still and selector of its own", True),
                ("keeping the still and selector it found", False),
            )
        ]

    return variants


def _campaign(
    case: str,
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    full: bool,
    producer: str = PRODUCER,
    unproven: bool = True,
) -> faults.Campaign:
    """``source``'s removal under every plan, then this checkout's starts on what it left.

    ``unproven``: the producer is a version whose intents hold no proof of
    the wallpaper's own files (0.2.2, the default).
    """
    # Hundreds of profiles: durability isn't what this probes.
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)
    later = time.time() + LATER_SECONDS
    monkeypatch.setattr(
        tidy, "time", types.SimpleNamespace(time=lambda: later, time_ns=time.time_ns)
    )
    drawn = companions.stub_capture(monkeypatch)
    consumer = _Consumer(case, unproven=unproven)

    def plans(trace: faults.Produced) -> list[dict[str, object]]:
        # One run is some hundreds of calls. A kill before, and an EIO at,
        # every change; in full, EACCES there too, EIO at every third call,
        # each edit of what was just published with what follows it, and
        # someone else changing each of the first three files before every
        # change.
        planned = faults.single_faults(trace, full=False)
        if full:
            planned += [{"fail": call.index, "errno": "EACCES"} for call in trace.changes]
            planned += [{"fail": call.index, "errno": "EIO"} for call in trace.calls[::3]]
            planned += faults.edits_while_public(trace, full=True)
            for call in trace.changes:
                planned += [
                    {"meddle": call.index, "which": which, "how": how}
                    for which in range(3)
                    for how in (*faults.EDITS, "replace")
                ]
        return planned

    found = faults.campaign(
        monkeypatch,
        source,
        producer,
        (case,),
        tmp_path,
        prepare=_prepare,
        consume=consumer.consume,
        exempt=consumer.exempt,
        plans=plans,
        # A trash move never puts the media in a claim.
        variants=_followed_by_a_download(consumer, drawn, full=full) if case == "remove" else None,
        edited_after=lambda plan: not plan or "kill" in plan,
        every=2 if full else 6,
        full=full,
    )

    whose = "0.2.2's" if unproven else "this version's"
    print("\n" + found.summary(f"removals, {case}, {whose}"))
    assert not found.findings, "\n".join(sorted(set(found.findings))[:30])
    return found


@pytest.mark.parametrize("case", ["remove", "trash"])
def test_nothing_a_released_removal_left_is_lost(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """What a gate runs: a kill before, and a failure at, every change."""
    _campaign(case, released_source, tmp_path, monkeypatch, full=False)


@faults.full_campaign
@pytest.mark.parametrize("case", ["remove", "trash"])
def test_nothing_a_released_removal_left_is_lost_under_any_plan(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    _campaign(case, released_source, tmp_path, monkeypatch, full=True)


def _this_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, full: bool) -> None:
    found = _campaign(
        "remove", THIS, tmp_path, monkeypatch, full=full, producer=THIS_PRODUCER, unproven=False
    )
    said = " ".join(found.outcomes)
    # Not vacuous: a download did follow an unfinished removal, and the
    # replay said what it left for it; and a removal nothing followed was
    # finished with nothing to say.
    assert found.varied, "no state had the media in its claim"
    assert "another file is at the wallpaper's path now" in said
    assert "start: media gone: nothing to say" in said


def test_nothing_is_lost_when_a_download_follows_this_versions_unfinished_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same campaign with this version as the producer: its intents hold
    proof of the wallpaper's own files, so a start may take them with a
    removal it finishes, and never when a download has finished at the same
    path in between. What a gate runs."""
    _this_versions(tmp_path, monkeypatch, full=False)


@faults.full_campaign
def test_nothing_is_lost_when_a_download_follows_this_versions_unfinished_removal_under_any_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _this_versions(tmp_path, monkeypatch, full=True)
