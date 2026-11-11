"""A hostile `project.json` is answered in linear time (review-f3 M2).

The engine check runs in the GUI process, on the stills thread, and a Workshop
item's author wrote the file. Its first version prescanned the text with an
unanchored regex that rescanned to the end of the file from every escaped
quote: 8 s at 64 KB, 141 s at 256 KiB, with the GIL held. Each file here is
at the 256 KiB cap and must be answered (refused or read) in well under the
bound; one test in the old code would not have finished in the alarm.
"""

from __future__ import annotations

import contextlib
import signal
import time
from pathlib import Path

import pytest

from wall_in_one.library import workshop

CAP = workshop.MAX_PROJECT_BYTES
#: Generous for a slow CI machine: the reader takes a fraction of a second.
BOUND_SECONDS = 3.0
ALARM_SECONDS = 20


def _padded(head: str, unit: str, tail: str = "") -> bytes:
    body = unit * ((CAP - len(head) - len(tail)) // len(unit))
    return (head + body + tail).encode()


HOSTILE = {
    "an unterminated string of escaped quotes": ('"' + '\\"' * (CAP // 2 - 2)).encode(),
    "an object holding an unterminated string of escaped quotes": _padded(
        '{"type":"scene","x":"', '\\"'
    ),
    "a string of escaped backslashes and quotes": _padded('{"type":"scene","x":"', '\\\\"'),
    "quotes only": b'"' * (CAP - 1),
    "opening brackets only": b"[" * (CAP - 1),
    "opening objects only": _padded("", '{"a":'),
    "a deep array closed": b'{"type":"scene","x":' + b"[" * 100_000 + b"]" * 100_000 + b"}",
    "many short keys": _padded('{"type":"scene"', ',"k":1', "}"),
    "many numbers": _padded('{"type":"scene","x":[', "1,", "1]}"),
    "many escapes": _padded('{"type":"scene","x":"', "\\u0041"),
    "many surrogate pairs": _padded('{"type":"scene","x":"', "\\ud83d\\ude00"),
    "a very long number": _padded('{"type":"scene","x":1', "0", "}"),
    "many unicode escapes that are not closed": _padded('{"type":"scene","x":"', "\\ud800"),
}


class _SlowError(Exception):
    pass


@pytest.mark.parametrize("name", list(HOSTILE))
def test_a_256_kib_hostile_file_is_answered_quickly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    data = HOSTILE[name]
    assert len(data) <= CAP
    (tmp_path / "project.json").write_bytes(data)

    def too_slow(*_: object) -> None:
        raise _SlowError(name)

    previous = signal.signal(signal.SIGALRM, too_slow)
    signal.alarm(ALARM_SECONDS)
    started = time.perf_counter()
    try:
        with contextlib.suppress(workshop.EngineProjectError):
            workshop.engine_project_type(tmp_path)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    elapsed = time.perf_counter() - started
    assert elapsed < BOUND_SECONDS, f"{name}: {elapsed:.1f} s"


@pytest.mark.parametrize("name", list(HOSTILE))
def test_the_scans_forgiving_reader_is_linear_on_the_same_files(tmp_path: Path, name: str) -> None:
    (tmp_path / "project.json").write_bytes(HOSTILE[name])
    started = time.perf_counter()
    workshop._read_project(tmp_path)
    assert time.perf_counter() - started < BOUND_SECONDS


def test_the_answers_for_the_valid_ones_are_still_right(tmp_path: Path) -> None:
    (tmp_path / "project.json").write_bytes(HOSTILE["many short keys"])
    assert workshop.engine_project_type(tmp_path) == "scene"
    (tmp_path / "project.json").write_bytes(HOSTILE["a deep array closed"])
    assert workshop.engine_project_type(tmp_path) == "scene"
