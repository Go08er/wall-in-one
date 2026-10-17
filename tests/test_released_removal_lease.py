"""A live 0.2.2 removal beside this build: the operation lease keeps them apart.

Updates are one-way, but a released window can still be open when this build
starts. 0.2.2 holds ``pending-removals.json.operation.lock`` from the moment it
journals a removal until it finishes or cancels it; this build's replay of the
journal (`Session.retry_removals`) skips an unfinished intent while that lease
is held, so it never moves, claims or deletes media the other process is in the
middle of removing. The released code is the holder, in a child.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests import released
from tests.released import released_source  # noqa: F401 -- a fixture
from wall_in_one import config
from wall_in_one.library import removals
from wall_in_one.session import Session

OWN = b"a video of the person's own"

#: The released code journals a removal, holds its lease, and waits to be told.
HOLDER = r"""
import os
import sys
from pathlib import Path

from wall_in_one import config
from wall_in_one.session import Session

root = Path(os.environ["HOME"]) / "Wallpapers"
session = Session(config.Settings(roots=(root,), scan_workshop=False))
session.refresh()
item = next(item for item in session.library.items if item.path == root / "mine.mp4")
intent = session._removals.prepare(item, tuple(session.library.roots))
print("PREPARED", flush=True)
sys.stdin.readline()
session.cancel_removal(intent)
session.retain_removal(intent)
print("FINISHED", flush=True)
"""


def _journal() -> bytes:
    return removals.state_path().read_bytes()


def test_a_replay_skips_a_removal_a_live_released_window_has_prepared(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    root = Path(os.environ["HOME"]) / "Wallpapers"
    root.mkdir(parents=True)
    media = root / "mine.mp4"
    media.write_bytes(OWN)
    settings = config.Settings(roots=(root,), scan_workshop=False)

    child = released.start(released_source, HOLDER)
    assert child.stdin is not None and child.stdout is not None
    try:
        assert child.stdout.readline().strip() == "PREPARED", child.stderr and child.stderr.read()
        journal = _journal()
        assert media.read_bytes() == OWN
        listing = sorted(str(path.relative_to(root)) for path in root.rglob("*"))

        session = Session(settings)
        try:
            failures = session.retry_removals()
            assert len(failures) == 1
            assert "removal is still active in another process" in failures[0], failures
        finally:
            session.shutdown()

        # Touched nothing: the media, its folder and the journal are as they were.
        assert media.read_bytes() == OWN
        assert sorted(str(path.relative_to(root)) for path in root.rglob("*")) == listing
        assert _journal() == journal
    finally:
        try:
            child.stdin.write("\n")
            child.stdin.flush()
            assert child.stdout.readline().strip() == "FINISHED"
        finally:
            child.stdin.close()
            try:
                child.wait(timeout=60)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    assert child.returncode == 0

    # The released window cancelled it: this build's replay has nothing to do.
    session = Session(settings)
    try:
        assert session.retry_removals() == ()
    finally:
        session.shutdown()
    assert media.read_bytes() == OWN
    assert sorted(str(path.relative_to(root)) for path in root.rglob("*")) == listing
