"""`workshop.engine_refusal` against the cases the Rust service is held to.

`service/tests/fixtures/engine-types/cases.json` is the oracle: each case
builds one Workshop item under a fresh home and records whether the service's
`refusal` lets linux-wallpaperengine start it, web wallpapers off and on
(`service/tests/workshop_agreement.rs` runs the same file against the
service). The app's check must give the same answer to every one (review-f2
L1, L2, L3 and R42-3).
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from wall_in_one.library import workshop

CASES_FILE = Path(__file__).resolve().parents[1] / "service/tests/fixtures/engine-types/cases.json"
CASES: list[dict[str, Any]] = json.loads(CASES_FILE.read_text(encoding="utf-8"))
SCENE = '{"type":"scene"}'


def _project_bytes(spec: dict[str, Any]) -> bytes:
    if "text" in spec:
        return str(spec["text"]).encode("utf-8")
    if "hex" in spec:
        return bytes.fromhex(str(spec["hex"]))
    if "fill" in spec:
        fill = spec["fill"]
        head, tail = str(fill["head"]).encode(), str(fill["tail"]).encode()
        body = str(fill["byte"]).encode() * (int(fill["total"]) - len(head) - len(tail))
        return head + body + tail
    nest = spec["nest"]
    depth = int(nest["depth"])
    return "".join(
        [nest["before"], nest["open"] * depth, nest["inner"], nest["close"] * depth, nest["after"]]
    ).encode()


def _write_project(directory: Path, spec: dict[str, Any]) -> None:
    path = directory / "project.json"
    match spec.get("special"):
        case "fifo":
            os.mkfifo(path)
        case "symlink":
            (directory / "real.json").write_text(SCENE)
            path.symlink_to("real.json")
        case "dangling":
            path.symlink_to("nowhere.json")
        case "directory":
            path.mkdir()
        case "absent":
            pass
        case None:
            path.write_bytes(_project_bytes(spec))
        case other:
            raise AssertionError(f"unknown special {other}")


def _id_of(index: int, case: dict[str, Any]) -> str:
    return str(case["id"]) if "id" in case else str(100_000 + index)


def _build(home: Path, index: int, case: dict[str, Any]) -> str:
    identifier = _id_of(index, case)

    def folder(at: int) -> Path:
        return home / workshop.ENGINE_WORKSHOP_FOLDERS[at]

    item = case.get("item")
    if item is not None:
        folder(0).mkdir(parents=True)
        place = folder(0) / identifier
        if item == "file":
            place.write_text("x")
        elif item == "link":
            elsewhere = home / "elsewhere" / identifier
            elsewhere.mkdir(parents=True)
            (elsewhere / "project.json").write_text(SCENE)
            place.symlink_to(elsewhere)
        elif item == "dangling":
            place.symlink_to(home / "nowhere")
        else:
            raise AssertionError(f"unknown item kind {item}")
    for copy in case.get("copies", []):
        directory = folder(copy["folder"]) / identifier
        directory.mkdir(parents=True, exist_ok=True)
        _write_project(directory, copy["project"])
    for name in case.get("ancestor_files", []):
        (home / name).write_text("not a folder")
    return identifier


class _HangError(Exception):
    pass


@pytest.mark.parametrize("allow_web", [False, True], ids=["web-off", "web-on"])
@pytest.mark.parametrize(
    ("index", "case"), list(enumerate(CASES)), ids=[case["name"] for case in CASES]
)
def test_the_app_gives_the_answer_the_service_gives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    index: int,
    case: dict[str, Any],
    allow_web: bool,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    identifier = _build(tmp_path, index, case)

    def hang(*_: object) -> None:
        raise _HangError

    previous = signal.signal(signal.SIGALRM, hang)
    signal.alarm(20)
    try:
        reason = workshop.engine_refusal(identifier, allow_web=allow_web)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    expected = case["web_on" if allow_web else "web_off"]
    assert ("refused" if reason else "allowed") == expected, reason


def test_every_case_is_named_once() -> None:
    names = [case["name"] for case in CASES]
    assert len(names) == len(set(names))
    assert len(CASES) >= 150
