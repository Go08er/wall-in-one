"""The user guide (docs/interface.md) names the window-level parts the code has.

Kept apart from ``test_readme_next_ui`` (the pages' sentences) so that the
window's sentences and the pages' are pinned independently. They moved from
the README's development section with the rest of what the pages do.
"""

from __future__ import annotations

from tests.test_readme_next_ui import _guide
from wall_in_one.ui import activity_log


def test_the_readme_describes_first_run_tidy_up_and_the_log() -> None:
    section = _guide()
    assert "on a welcome screen instead of in dialogs" in section
    assert '"Not now" saves nothing and the question comes back at the next start' in section
    assert "Classic's one-time Tidy up card is above every page" in section
    assert "Nothing there runs until you press its button" in section
    assert "kept in memory only, with **Copy** and **Hide file paths**" in section
    assert '"Use frame" and a log' not in section, "the log is there now"
    contract = " ".join((activity_log.__doc__ or "").split())
    assert "Nothing is written to disk" in contract, "as the code says"
