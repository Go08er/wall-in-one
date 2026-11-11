"""review-f4 F4-4: while web wallpapers are off, no still a preview drew plays.

`Settings.covers` lets a library resolved under the other preview setting publish
while web wallpapers are off, because everything the preview setting decides a
still for is then left out. A preset whose own `project.json` says ``web`` over a
true scene was not: it plays as its base (not a web page), yet the preview setting
decided its still (`MediaItem.preview_kind`), so a library resolved with previews
on published the author's preview as its still with web off. Exclusion and
coverage are now one predicate (`workshop.web_off`).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from wall_in_one import config, runtime_config
from wall_in_one.library import scan, workshop
from wall_in_one.session import Session

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _world() -> tuple[Path, Path]:
    home = Path(os.environ["HOME"])
    content = home / ".local/share/Steam/steamapps/workshop/content/431960"

    def put(name: str, data: bytes | str) -> None:
        path = content / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())

    put("111/project.json", json.dumps({"type": "scene", "title": "Base", "file": "scene.json"}))
    put("111/scene.json", "{}")
    put(
        "222/project.json",
        json.dumps(
            {
                "type": "web",
                "title": "Looks like a preset",
                "file": "index.html",
                "preset": {"general": {"properties": {}}},
                "dependency": "111",
                "preview": "preview.gif",
            }
        ),
    )
    put("222/index.html", "<p>x</p>")
    put("222/preview.gif", b"GIF89a-author-picture")
    library = home / "wallpapers"
    stills = library / "Wall-in-One" / "Automatic Stills"
    stills.mkdir(parents=True)
    # What 0.4.1, or a run with previews on, drew from the author's preview.
    (stills / "222.png").write_bytes(PNG)
    (stills / "111.png").write_bytes(PNG)
    return home, library


def _document(library: Path, home: Path, *, previews: bool, web: bool) -> str:
    scanned = scan.scan(
        [library],
        records={},
        include_workshop=True,
        workshop_roots=(home / ".local/share/Steam",),
        previews=previews,
    )
    settings = config.Settings(
        roots=(library,), allow_web_wallpapers=web, web_wallpaper_previews=True
    )
    session = Session(settings)
    try:
        session.adopt_library(scanned, reconcile_workshop=False)
        return runtime_config.render(settings, session)
    finally:
        session.shutdown()


def test_a_web_typed_preset_of_a_scene_is_left_out_while_web_is_off() -> None:
    home, library = _world()
    scanned = scan.scan(
        [library],
        records={},
        include_workshop=True,
        workshop_roots=(home / ".local/share/Steam",),
        previews=True,
    )
    preset = next(item for item in scanned.items if item.scene == "222")
    assert preset.holds_web and not preset.web_page and preset.unplayable is None
    assert preset.preview_kind
    assert workshop.web_off(preset, False)
    assert not workshop.web_off(preset, True)
    assert workshop.why_not_played(preset, False) == workshop.WEB_OFF_REASON


def test_with_web_off_either_preview_setting_publishes_the_same_document() -> None:
    """The library web-off is published from may be one resolved with previews on."""
    home, library = _world()
    on = _document(library, home, previews=True, web=False)
    off = _document(library, home, previews=False, web=False)
    assert on == off
    assert "222.png" not in on
    assert 'scene_id = "222"' not in on
    covers = config.Settings(roots=(library,), allow_web_wallpapers=False)
    assert covers.covers(((library,), True, True)) and covers.covers(((library,), True, False))
