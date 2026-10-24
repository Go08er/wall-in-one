"""r11's web-off guard still finds a web page whose entry carries the renderer dials.

`runtime_config.retained_web_wallpapers` looks for every `scene_id` and
`scene_base` in a kept ``runtime.toml``. The dials add an entry's own `scene`
table and `[renderer]` keys, neither of which names a wallpaper, so a page
compiled with its own frame rate while web wallpapers were on is still found.
Steam's folder is a fixture under the test's own home.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from tests.test_web_retained import WEB, _web_profile
from wall_in_one import config, runtime_config
from wall_in_one.session import Session


def test_the_guard_finds_a_web_page_with_dials_of_its_own(tmp_path: Path) -> None:
    root = _web_profile(Path(os.environ["HOME"]))
    settings = config.Settings(roots=(root,), allow_web_wallpapers=True, scene_particles=False)
    session = Session(settings)
    session.refresh()
    (page,) = (item for item in session.library.items if item.scene == WEB)
    session.pairings.set_dial(page, "scene", "fps", 60)
    target = tmp_path / "runtime.toml"
    target.write_text(runtime_config.render(settings, session), encoding="utf-8")
    session.shutdown()

    document = tomllib.loads(target.read_text(encoding="utf-8"))
    # Schema 8 (r13) carries everything 7 does, and says web wallpapers are allowed.
    assert document["schema_version"] == runtime_config.NOTHING_TO_PLAY_SCHEMA_VERSION
    assert document["settings"]["allow_web_wallpapers"] is True
    (entry,) = (e for p in document["playlists"] for e in p["entries"] if "scene_id" in e)
    assert entry["scene"] == {"fps": 60}
    assert runtime_config.retained_web_wallpapers(target) == ("Electric Eye",)
