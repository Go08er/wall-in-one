"""What the released 0.2.2 leaves of its capture cleanup, and this version's cleanup after it.

Review rule 4, run rather than read (AB-1's class, at startup). An upgrade
from the deployed application adopted its automatic stills: a manifest names
each old capture, with a record beside the capture and, beside the video, the
selector that says which still is the video's. At every start
`library.capture_upgrade.prepare` rebuilds each still under its new name,
rebinds the selector and retires the old capture and its record. The released
`prepare` is the producer here, in a child (`tests.released`), on an adoption
the released `library.adopted` rendered: run to the end, with a frame grabber
that fails (the capture is kept and its binding put back), and under one fault
at a time (`tests.released_faults`), after which a person may edit any one
file it left. Only ffmpeg and the wallpaper service are stood in for.

This checkout's `prepare` is the consumer, twice, as two starts. Before it, a
person may also have pointed the video's selector back at the old capture with
a note of their own, or at another still of theirs; and from any one of its
filesystem calls on, the selector or the captures' folder may be impossible to
look at (mode 000). Nothing a person edited may lose a byte. An old capture, its
record and the selector may go only as the adopted originals they were, and
never while the selector still names the capture: whatever file the selector's
bytes name afterwards is still there.

The manifest is rendered by the released code but not by a run of the released
upgrade itself, which talks to systemd.
"""

from __future__ import annotations

import json
import logging
import os
import re
import zlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from tests import released_faults as faults
from tests.released import released_source  # noqa: F401 -- a fixture
from wall_in_one import config, paths, predecessor_process
from wall_in_one.control import client
from wall_in_one.control.protocol import Response
from wall_in_one.library import adopted, capture_upgrade, pairing, scan, stills


def _png(pixel: bytes) -> bytes:
    """A complete one-pixel image: the cleanup reads a replacement's dimensions."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4) + kind + data + zlib.crc32(kind + data).to_bytes(4)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", (1).to_bytes(4) * 2 + b"\x08\x02\x00\x00\x00")
        + chunk(b"IDAT", zlib.compress(b"\x00" + pixel))
        + chunk(b"IEND", b"")
    )


#: The deployed application's capture, and what the stand-in for ffmpeg writes.
OLD = _png(b"\x12\x34\x56")
NEW = _png(b"\x78\x9a\xbc")
#: A still of the person's own, for a selector that names it.
THEIRS = _png(b"\xaa\xbb\xcc")

#: The released producer. Its job's arguments: ``upgrade`` (the frame grabber
#: works) or ``kept`` (it fails, so the capture stays and its binding is put
#: back); then ``renumbered`` (the adoption was recorded under another device
#: number, as after a remount) or ``same``.
PRODUCER = r"""
import hashlib
import os
import zlib
from dataclasses import replace
from pathlib import Path

from wall_in_one import config, file_io, predecessor_process
from wall_in_one.control import client
from wall_in_one.library import adopted, capture_upgrade, pairing, stills


def _png(pixel):
    def chunk(kind, data):
        return len(data).to_bytes(4) + kind + data + zlib.crc32(kind + data).to_bytes(4)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", (1).to_bytes(4) * 2 + b"\x08\x02\x00\x00\x00")
        + chunk(b"IDAT", zlib.compress(b"\x00" + pixel))
        + chunk(b"IEND", b"")
    )


OLD = _png(b"\x12\x34\x56")
NEW = _png(b"\x78\x9a\xbc")
grabber = {"works": True}


def _frame(video, target, seek, *, processes=None):
    # ffmpeg's part, and only that: one frame into the output it was given.
    if not grabber["works"]:
        return "ffmpeg: no frame could be decoded"
    with open(target, "wb") as sink:
        sink.write(NEW)
    return ""


def _stopped(*_arguments, **_keywords):
    raise client.NotRunningError("the wallpaper service is stopped")


stills._run = _frame
stills.is_available = lambda: True
client.send_runtime = _stopped
predecessor_process.refuse_live_predecessor_runtime = lambda: None
capture_upgrade.RUNTIME_WAIT_SECONDS = 0.0
trace_filesystem((file_io, "_rename_noreplace"))


def _renumbered(fingerprint):
    return (fingerprint[0] + 1, *fingerprint[1:])


def _adopt(root, source, mount):
    # What the released upgrade from the deployed application left: its marker
    # and capture, the manifest, and the record beside the capture.
    automatic = pairing.still_directory(root)
    automatic.mkdir(parents=True)
    marker = automatic / adopted.AUTOMATIC_MARKER_FILENAME
    marker.write_bytes(
        adopted.canonical_bytes(
            {
                "schema": 1,
                "plugin": adopted.PLUGIN_ID,
                "kind": "automatic-stills",
                "ownership": "managed",
            }
        )
    )
    capture = automatic / "clip.png"
    capture.write_bytes(OLD)
    fingerprints = [file_io.regular_file_fingerprint(path) for path in (source, capture, marker)]
    identities = [file_io.path_identity(root), file_io.path_identity(automatic)]
    if mount == "renumbered":
        fingerprints = [_renumbered(fingerprint) for fingerprint in fingerprints]
        identities = [_renumbered(identity) for identity in identities]
    authority = adopted.Authority(
        source_path=source,
        capture_path=capture,
        source_fingerprint=fingerprints[0],
        capture_fingerprint=fingerprints[1],
        capture_size=len(OLD),
        capture_sha256=hashlib.sha256(OLD).hexdigest(),
    )
    adoption = adopted.Adoption(
        adoption_id="",
        root=root,
        root_identity=identities[0],
        automatic_stills=automatic,
        automatic_stills_identity=identities[1],
        marker_path=marker,
        marker_fingerprint=fingerprints[2],
        marker_sha256=hashlib.sha256(marker.read_bytes()).hexdigest(),
        authorities=(authority,),
    )
    adoption = replace(adoption, adoption_id=adopted.adoption_id_for(adoption))
    authority.sidecar_path.write_bytes(adopted.render_sidecar(authority, adoption.adoption_id))
    manifest = adopted.state_path()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_bytes(adopted.render_manifest(adoption))
    return marker, capture, authority.sidecar_path, manifest


def operation(case, mount):
    root = Path(os.environ["HOME"]) / "Wallpapers"
    source = root / "videos" / "clip.mp4"
    disarm()
    left = _adopt(root, source, mount)
    config.save(config.Settings(roots=(root,), scan_workshop=False))
    selector = stills.write_sidecar(source, left[1])
    had(*left, selector)
    grabber["works"] = case == "upgrade"
    arm()
    print("DONE", capture_upgrade.prepare(), flush=True)


serve(operation)
"""

#: The plain reason of an OS error, without the path Python adds to it (a
#: missing file's own path is not somewhere a message says a thing is).
_ERRNO_PATH = re.compile(r"(\[Errno \d+\] [^:']+): '[^']*'")


def _root(base: Path) -> Path:
    return Path(faults.profile_environment(base)["HOME"]) / "Wallpapers"


def _source(base: Path) -> Path:
    return _root(base) / "videos" / "clip.mp4"


def _selector(base: Path) -> Path:
    source = _source(base)
    return source.with_name(source.name + pairing.SIDECAR_SUFFIX)


def _capture(base: Path) -> Path:
    return pairing.still_directory(_root(base)) / "clip.png"


def _record(base: Path) -> Path:
    capture = _capture(base)
    return capture.with_name(capture.name + pairing.SIDECAR_SUFFIX)


def _prepare(base: Path) -> faults.Tree:
    """A profile with the person's one video in its library folder."""
    for value in faults.profile_environment(base).values():
        Path(value).mkdir(parents=True, exist_ok=True)
    source = _source(base)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"deployed video generation")
    return faults.tree(base)


class _Consumer:
    """This checkout's cleanup on one state, and what it may retire there."""

    def __init__(self) -> None:
        #: (path, bytes) of each entry the cleanup may retire as it stands.
        self.retirable: set[tuple[Path, bytes]] = set()

    def exempt(self, entry: faults.Entry, _before: faults.Tree) -> bool:
        # The compiled configuration is derived, and rewritten at every start.
        return entry.path == paths.runtime_config_path() or (
            (entry.path, entry.data) in self.retirable
        )

    def _originals(self, base: Path) -> None:
        """The adopted originals, as they are now: only those may be retired."""
        capture, record, selector = _capture(base), _record(base), _selector(base)
        if capture.is_file() and capture.read_bytes() == OLD:
            self.retirable.add((capture, OLD))
            # The record is the released code's rendering; it goes with the
            # capture it describes, and only unedited (the cleanup compares it
            # with its own rendering, which the manifest fixes).
        if record.is_file():
            self.retirable.add((record, record.read_bytes()))
        if selector.is_file():
            # Only the exact automatic selector is ever withdrawn.
            automatic = [(pairing.SIDECAR_STILL_KEY, str(capture))]
            raw = selector.read_bytes()
            try:
                if json.loads(raw, object_pairs_hook=list) == automatic:
                    self.retirable.add((selector, raw))
            except ValueError:
                pass

    def consume(self, base: Path) -> Iterator[tuple[str, str]]:
        root = _root(base)
        self.retirable.clear()
        for start in ("prepare", "prepare again"):
            self._originals(base)
            there = {entry.path for entry in faults.tree(root).entries}
            edited_records = self._edited(base)
            yield start, _prepared()
            # The harness has put back whatever could not be looked at.
            yield f"{start}: checked", self._checked(base, there, edited_records)
        yield "scan", _scanned(root)

    def _edited(self, base: Path) -> list[tuple[Path, bytes]]:
        """The capture and its record, where they are not the adopted originals."""
        return [
            (path, path.read_bytes())
            for path in (_capture(base), _record(base))
            if path.is_file() and (path, path.read_bytes()) not in self.retirable
        ]

    def _checked(self, base: Path, there: set[Path], edited: list[tuple[Path, bytes]]) -> str:
        """What must hold after a start, whatever it retired."""
        selector = _selector(base)
        named = selector.read_bytes() if selector.is_file() else b""
        # Whatever file the selector's bytes name was not retired from under it.
        for path in sorted(there):
            if os.fsencode(path) in named and not path.is_file():
                return (
                    f"FINDING the selector still names {path.relative_to(base)}, "
                    "which this start retired"
                )
        for path, data in edited:
            if not path.is_file() or path.read_bytes() != data:
                return f"FINDING {path.relative_to(base)} was edited, and this start changed it"
        return "the selector's still is there" if named else "no selector"


class _Collected(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _prepared() -> str:
    """One start's cleanup: what it returned, and what it logged."""
    handler = _Collected()
    capture_upgrade.LOGGER.addHandler(handler)
    level = capture_upgrade.LOGGER.level
    capture_upgrade.LOGGER.setLevel(logging.INFO)
    try:
        result = capture_upgrade.prepare()
    finally:
        capture_upgrade.LOGGER.setLevel(level)
        capture_upgrade.LOGGER.removeHandler(handler)
    logged = "; ".join(_ERRNO_PATH.sub(r"\1", record.getMessage()) for record in handler.records)
    return f"migrated {result.migrated}, retained {result.retained}" + (
        f"; {logged}" if logged else ""
    )


def _scanned(root: Path) -> str:
    try:
        library = scan.scan((root,))
    except adopted.AdoptionError as error:
        return f"REFUSED {error}"  # a manifest a person edited: the app's scan says so too
    video = next((item for item in library.items if item.is_moving), None)
    if video is None:
        return "FINDING the video is no longer in the library"
    still = video.paired_still
    if still is not None and not still.is_file():
        return f"FINDING the video's still {still} isn't there"
    return f"still {'paired' if still is not None else 'none'}"


# -- what a person, or a lost folder, adds --------------------------------------------------------


def _points_back(base: Path) -> frozenset[int]:
    """The person's own selector: the old capture, and a note."""
    selector = _selector(base)
    selector.write_text(
        json.dumps({pairing.SIDECAR_STILL_KEY: str(_capture(base)), "note": "mine"}),
        encoding="utf-8",
    )
    return frozenset({selector.stat().st_ino})


def _picks_another(base: Path) -> frozenset[int]:
    """The person's own selector: another still of theirs."""
    other = _source(base).with_name("mine.png")
    other.write_bytes(THEIRS)
    selector = _selector(base)
    selector.write_text(json.dumps({pairing.SIDECAR_STILL_KEY: str(other)}), encoding="utf-8")
    return frozenset({selector.stat().st_ino, other.stat().st_ino})


#: What a person may have done to the selector before this start.
_SELECTORS = (
    ("as left", lambda _base: frozenset[int]()),
    ("the person points the selector back at the old capture, with a note", _points_back),
    ("the person picks another still", _picks_another),
)


def _variants(
    full: bool,
) -> Callable[[Mapping[str, object], faults.Tree, Path, Sequence[str]], Sequence[faults.Variant]]:
    """What else to try on a state that still has the old capture.

    On each state a kill left, or the run with no fault: each of the person's
    selectors. And an entry that can't be looked at from one of this start's
    calls on (`faults.after_each_look_at`). Bounded: the selector, pointed
    back, on the first state that has both captures. In full: on every such
    state, the selector and the captures' folder under each selector, and
    under the pointed-back one the capture, its record, the video and its
    folder as well.
    """
    hidden = [False]

    def unseen(label: str, has_both: bool) -> dict[str, Callable[[Path], Path]]:
        if not full:
            if hidden[0] or not has_both or "points" not in label:
                return {}
            hidden[0] = True
            return {"the selector": _selector}
        places: dict[str, Callable[[Path], Path]] = {
            "the selector": _selector,
            "the captures' folder": lambda base: _capture(base).parent,
        }
        if "points" in label:
            places |= {
                "the old capture": _capture,
                "its record": _record,
                "the video": _source,
                "the video's folder": lambda base: _source(base).parent,
            }
        return places

    def variants(
        plan: Mapping[str, object], state: faults.Tree, base: Path, calls: Sequence[str]
    ) -> Sequence[faults.Variant]:
        if state.at(_capture(base)) is None:
            return ()  # nothing a selector could still name
        if plan and "kill" not in plan:
            return ()
        has_both = any(
            entry.path.parent == _capture(base).parent and entry.path.name.startswith("video-")
            for entry in state.entries
        )

        def unlookable(
            label: str, apply: Callable[[Path], frozenset[int]], seen: Sequence[str]
        ) -> list[faults.Variant]:
            return [
                faults.Variant(
                    f"{label}; {what} can't be looked at from call {at}",
                    apply,
                    lambda base, where=where, at=at: faults.Watch(  # type: ignore[misc]
                        unseen=where(base), at=at
                    ),
                )
                for what, where in unseen(label, has_both).items()
                for at in faults.after_each_look_at(seen, where(base))
            ]

        wanted: list[faults.Variant] = []
        for label, apply in _SELECTORS:
            if label == "as left":
                wanted += unlookable(label, apply, calls)
            else:
                # Its calls differ once the selector is the person's: listed
                # again with it, and only then hidden from.
                wanted.append(
                    faults.Variant(
                        label,
                        apply,
                        more=lambda seen, label=label, apply=apply: unlookable(  # type: ignore[misc]
                            label, apply, seen
                        ),
                    )
                )
        return wanted

    return variants


def _plans(full: bool) -> Callable[[faults.Produced], list[dict[str, object]]]:
    """One run of this producer is some 1,500 calls, so not every plan at every call.

    A kill before every change. In full, also: each error at every change, EIO
    at every tenth call, each edit of what was just published with what
    follows it, and someone else changing each of the first two files
    before every change.
    """

    def plans(trace: faults.Produced) -> list[dict[str, object]]:
        planned: list[dict[str, object]] = [{"kill": call.index} for call in trace.changes]
        if not full:
            return planned
        for number in ("EIO", "EACCES"):
            planned += [{"fail": call.index, "errno": number} for call in trace.changes]
        planned += [{"fail": call.index, "errno": "EIO"} for call in trace.calls[::10]]
        planned += faults.edits_while_public(trace, full=False)
        for call in trace.changes:
            for which in range(2):
                planned += [
                    {"meddle": call.index, "which": which, "how": how}
                    for how in (*faults.EDITS, "replace")
                ]
        return planned

    return plans


def _stand_ins(monkeypatch: pytest.MonkeyPatch) -> None:
    """ffmpeg and the wallpaper service, and nothing else."""

    def frame(_video: Path, target: Path, _seek: float, **_keywords: object) -> str:
        Path(target).write_bytes(NEW)
        return ""

    def stopped(*_arguments: object, **_keywords: object) -> Response:
        raise client.NotRunningError("the wallpaper service is stopped")

    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(stills, "_run", frame)
    monkeypatch.setattr(stills, "is_available", lambda: True)
    monkeypatch.setattr(client, "send_runtime", stopped)
    monkeypatch.setattr(predecessor_process, "refuse_live_predecessor_runtime", lambda: None)
    monkeypatch.setattr(capture_upgrade, "RUNTIME_WAIT_SECONDS", 0.0)
    # Hundreds of profiles: durability isn't what this probes.
    monkeypatch.setattr(os, "fsync", lambda _descriptor: None)


#: A1: a selector that can't be read at the retire step read as "selects
#: nothing", and the capture it named was retired. Fixed by
#: ``pairing.read_sidecar_strict``; until that is here, this campaign finding
#: it is expected, and nothing else is.
A1_FIXED = hasattr(pairing, "read_sidecar_strict")


def _is_a1(finding: str) -> bool:
    return "the selector still names" in finding and "the selector can't be looked at" in finding


def _is_log_wording(finding: str) -> bool:
    """The cleanup speaks only to the log, and names each capture as its
    subject, so a path it names is not always somewhere a file is. Counted and
    printed, not failed on. The kinds a full campaign meets:

    - Once the released code retired a capture and died before its record, a
      start that keeps the record (a person edited it) logs "Could not retire
      automatic capture <the capture>": the capture that is already gone, not
      the record it kept.
    - With the captures' folder unlookable: "could not safely retain existing
      still <the new name>", where no still was ever at that name; and "cannot
      inspect <a temporary>", which it then removed.
    """
    return finding.startswith("SAID")


def _campaign(
    case: tuple[str, str],
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    full: bool,
) -> faults.Campaign:
    _stand_ins(monkeypatch)
    consumer = _Consumer()
    found = faults.campaign(
        monkeypatch,
        released_source,
        PRODUCER,
        case,
        tmp_path,
        prepare=_prepare,
        consume=consumer.consume,
        exempt=consumer.exempt,
        plans=_plans(full),
        variants=_variants(full),
        # The states a person finds when nothing is running; a file in five
        # of each, where a gate has to be quick.
        edited_after=lambda plan: not plan or "kill" in plan,
        every=2 if full else 5,
        full=full,
    )
    print("\n" + found.summary(f"captures, {' and '.join(case)}"))
    return found


def _judge(found: faults.Campaign) -> None:
    known = sorted({finding for finding in found.findings if _is_a1(finding)})
    notes = sorted({finding for finding in found.findings if _is_log_wording(finding)})
    others = sorted(set(found.findings) - set(known) - set(notes))
    print(f"A1: {len(known)}; the log names a path nothing is at: {len(notes)}")
    assert not others, "\n".join(others[:40])
    if known and not A1_FIXED:
        pytest.xfail(f"A1, as expected before its fix ({len(known)} plans): {known[0]}")
    assert not known, "\n".join(known[:40])


def test_nothing_the_released_capture_cleanup_left_is_lost(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What a gate runs: the upgrade, under the bounded plans."""
    _judge(_campaign(("upgrade", "renumbered"), released_source, tmp_path, monkeypatch, full=False))


@faults.full_campaign
@pytest.mark.parametrize("case", [("upgrade", "renumbered"), ("kept", "same")], ids="-".join)
def test_nothing_the_released_capture_cleanup_left_is_lost_under_any_plan(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, str],
) -> None:
    _judge(_campaign(case, released_source, tmp_path, monkeypatch, full=True))


def test_the_settings_the_released_code_saved_are_this_versions_too(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The producer's library folder is this version's: the cleanup isn't skipped
    for want of one (a campaign that only ever met "not configured" proves nothing)."""
    job = faults.Job(tmp_path / "plain", {}, ("kept", "renumbered"))
    _prepare(job.base)
    (made,) = faults.run(released_source, PRODUCER, [job])
    assert made.status == 0 and "retained=1" in made.output, made.output + made.errors[-2000:]
    faults.scratch_profile(monkeypatch, job.base)
    assert config.load_strict(require_present=True).roots == (_root(job.base),)
    assert _capture(job.base).read_bytes() == OLD
    assert pairing.read_sidecar(_source(job.base)) == _capture(job.base)
