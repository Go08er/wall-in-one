"""A removal whose dry run can't be made is refused aloud, and the lane is free again.

`RemovalPlan.run` begins with a dry run of the removal, and one that can't be
made raises `dry_run.CheckFailed` with nothing written (the released 0.2.2
had no dry run; its plan always returned a result). The probes found that an
ordinary failure reaches it: the adoption manifest the dry run's scan reads
can't be opened. Here that is native (the manifest at mode 000), and both
callers in the real application are driven: the window's removal
(`Application.remove_item_async`) and ``ctl remove``.

Each time: the person, or ctl, is told why; the removal lane is released (no
held barrier, and the same gesture goes through once the manifest can be read
again); and nothing was withdrawn or claimed: the media and its download
record are as they were, no claim folder exists, and the journal is empty.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests import test_ui_codex030_round5 as round5  # noqa: E402
from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_control import _downloaded  # noqa: E402
from tests.test_ui_runtime import _application, _close  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.library import adopted, removals  # noqa: E402
from wall_in_one.session import RemovalResult  # noqa: E402
from wall_in_one.ui.app import _Commands  # noqa: E402

BOUND = 15.0


@pytest.mark.parametrize("caller", ["the window", "ctl"])
def test_a_removal_whose_dry_run_cannot_be_made_is_refused_and_the_lane_is_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caller: str
) -> None:
    root = tmp_path / "lib" / "R"
    root.mkdir(parents=True)
    managed = _downloaded(root)
    media = managed.read_bytes()
    beside = {path: path.read_bytes() for path in managed.parent.iterdir() if path.is_file()}
    application = _application(tmp_path, monkeypatch)
    application._service_start = True
    monkeypatch.setattr(client, "send_runtime", lambda *_a, **_k: Response.success())
    said: list[str] = []
    results: list[RemovalResult] = []
    manifest = adopted.state_path()
    try:
        round5._configured(application, root)
        item = application.session.library.find(managed)
        assert item is not None
        assert application._publish_runtime()
        monkeypatch.setattr(application, "window_report", said.append)
        commands = _Commands(application)

        def remove() -> Response | None:
            if caller == "ctl":
                answer = commands.remove_wallpaper(str(managed))
                assert isinstance(answer, Response), answer
                return answer
            assert application.remove_item_async(item, trash=False, finish=results.append)
            spin_until(
                lambda: not application._removal_active and bool(said or results),
                BOUND,
                what="the removal to end, one way or the other",
            )
            return None

        # The adoption manifest can't be opened: the dry run's scan can't be made.
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_bytes(b"{}")
        manifest.chmod(0o000)
        try:
            refused = remove()
        finally:
            manifest.chmod(0o600)

        told = refused.message if refused is not None else " ".join(said)
        if refused is not None:
            assert not refused.ok, "ctl is refused, not answered with success"
        assert "was not removed" in told and "could not be checked" in told, told
        assert results == [], "the window's removal did not finish as a removal"
        # The lane is free: no barrier held, nothing waiting on a removal.
        assert not application._removal_active
        assert not application._removal_convergence_held
        # Nothing was withdrawn or claimed.
        assert managed.read_bytes() == media
        now = {path: path.read_bytes() for path in managed.parent.iterdir() if path.is_file()}
        assert now == beside, "the download record and the marker are as they were"
        assert not [path for path in managed.parent.iterdir() if "removal" in path.name]
        assert removals.Store.open().records == ()

        # And the same gesture goes through once the manifest can be read again.
        manifest.unlink()
        said.clear()
        accepted = remove()
        if accepted is not None:
            assert accepted.ok, accepted
        else:
            assert results and results[0].committed, (said, results)
        assert not managed.exists()
    finally:
        if manifest.exists():
            manifest.chmod(0o600)
        _close(application)
