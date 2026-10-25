"""The last step of a shutdown while web wallpapers are off (r13d N11; review r13e, L1 and L2).

`Application._web_off_on_disk_at_exit` replaces a ``runtime.toml`` that allows web wallpapers,
while ``settings.toml`` says they are off, with the document that plays nothing, and nothing else.
Review r13e found, at d4a369d:

* **L1:** with no status that lists schema 8 (none heard, or a service older than that), it
  scanned the whole Workshop on GTK's thread to judge a file that allows none, under the
  compiler lock and with nothing to end it: 1.6-3.4 s for 2,500-4,000 items (its p1).
* **L2:** when that scan couldn't list an id the file names (an item unsubscribed since), it put
  a compile of the Session in memory over the file, though another writer's newer document was
  there, and the playlist only that writer knew of was dropped (its p2). It also read the
  setting before it waited for the compiler lock, so a document compiled for web wallpapers on,
  by a writer that saw them turned on while the step waited, was replaced as well.

Each is called as `do_shutdown` calls it last, on a windowless application, with the service's
status what the case says. The Workshop is a handful of real items in the test's own $HOME.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_ui_codex030_round1b import profiles, toolkit  # noqa: E402, F401
from tests.test_ui_renderer_settle_shutdown import HEARD, _naming  # noqa: E402
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.library import scan  # noqa: E402
from wall_in_one.session import Session  # noqa: E402
from wall_in_one.ui import app as app_module  # noqa: E402

CONTENT = ".local/share/Steam/steamapps/workshop/content/431960"

#: How long anything here waits before it gives up: past it the test fails, it doesn't hang.
GIVE_UP: float = 60.0


def _workshop(count: int) -> tuple[Path, list[str]]:
    """``count`` Workshop scenes under $HOME's Steam, and one web page. Their folder, the scenes."""
    content = Path(os.environ["HOME"]) / CONTENT
    content.mkdir(parents=True)
    scenes: list[str] = []
    for index in range(count + 1):
        identifier = str(2_000_000_000 + index)
        folder = content / identifier
        folder.mkdir()
        kind, file = ("web", "index.html") if index == count else ("scene", "scene.json")
        (folder / file).write_text("{}" if kind == "scene" else "<html></html>")
        (folder / "scene.pkg").write_bytes(b"PKGV0001")
        (folder / "project.json").write_text(
            json.dumps({"type": kind, "title": f"Item {index}", "file": file})
        )
        if kind == "scene":
            scenes.append(identifier)
    return content, scenes


def _with_workshop(application: Any, count: int) -> tuple[config.Settings, Path, list[str]]:
    """Wallpaper Engine scanning on, web wallpapers off, and the library scanned with it."""
    content, scenes = _workshop(count)
    settings = replace(
        application.settings, scan_workshop=True, allow_web_wallpapers=False
    ).validated()
    config.save(settings)
    application._settings = settings
    application._settings_requested = settings
    application.session.update_settings(settings, rescan_library=False)
    application.session.refresh(mutate_removals=False)
    application._accepted_library_sources = (settings.roots, settings.scan_workshop)
    return settings, content, scenes


def _heard(monkeypatch: pytest.MonkeyPatch, status: dict[str, object] | None) -> None:
    """What the application last heard from the service."""
    monkeypatch.setattr(app_module.Application, "runtime_status", property(lambda _self: status))


def _scans(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Every Workshop scan from here on, recorded."""
    workshop_items = scan.workshop_items
    scans: list[bool] = []

    def recorded(*arguments: Any, **keywords: Any) -> Any:
        scans.append(True)
        return workshop_items(*arguments, **keywords)

    monkeypatch.setattr(scan, "workshop_items", recorded)
    return scans


@pytest.mark.parametrize("heard", list(HEARD))
@pytest.mark.parametrize("held", ["names-scenes", "names-one-unsubscribed", "control-allows"])
def test_the_step_judges_the_file_without_scanning_the_workshop(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    heard: str,
    held: str,
) -> None:
    """p1, judged by what the step does, not by how long it takes. The file on disk names the
    library's Workshop scenes, as one compiled for a service with no known schema does, and
    allows no web wallpaper; or one of them was unsubscribed since. Before, with no schema-8
    status: one scan of the Workshop, the cost that grows with the library, and for the
    unsubscribed one a compile installed over the file. Now: no scan in any case; one that names
    only scenes is left. For a service with no check of an item's type of its own (none heard,
    or one older than 8), an id that can't be checked may be a web page it plays, so that file is
    replaced with the document that plays nothing (review r13f, L2), built from settings.toml
    alone: never a compile. The control: a file that allows web wallpapers is replaced, whatever
    was heard."""
    application = profiles().application
    settings, content, scenes = _with_workshop(application, 4)
    if held == "control-allows":
        document = runtime_config.render(
            replace(settings, allow_web_wallpapers=True), application.session
        )
        assert runtime_config.allows_web_wallpapers(text=document)
    else:
        document = _naming(scenes)
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding="utf-8")
    if held == "names-one-unsubscribed":
        shutil.rmtree(content / scenes[-1])
    _heard(monkeypatch, HEARD[heard])
    scans = _scans(monkeypatch)
    application._web_off_on_disk_at_exit()
    now = target.read_text(encoding="utf-8")
    if held == "control-allows":
        assert not runtime_config.allows_web_wallpapers(text=now), (
            "settings.toml says web wallpapers are off, and the file on disk still allows them"
        )
        return
    assert not scans, f"the step scanned the Workshop {len(scans)} time(s)"
    if held == "names-one-unsubscribed" and heard != "control-reads-eight":
        assert now == runtime_config.nothing_to_play_document(
            config.load_strict(), runtime_config.WEB_OFF_REASON
        ), "an id that can't be checked, for a service with no check of its own, was left"
        return
    assert now == document, "a file that allows no web wallpaper was replaced"


@pytest.mark.parametrize("heard", list(HEARD))
def test_the_step_never_puts_a_compile_over_another_writers_newer_document(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    heard: str,
) -> None:
    """p2. Another writer gave two scenes a still, saved a playlist "Newer" of them and published
    it for a service that reads schema 8; this process's Session never loaded that playlist. Then
    one scene it names was unsubscribed. Before, with no schema-8 status: that id couldn't be
    checked, so the step installed its own compile of the Session over the newer document, and
    "Newer" was gone. Now nothing is compiled: for a service that reads 8, the document is
    left; for one with no check of an item's type of its own, that id may be a web page it plays
    (review r13f, L2), so the document is replaced with the one that plays nothing, which is
    built from settings.toml alone, and "Newer" comes back with the next publication."""
    application = profiles().application
    settings, content, scenes = _with_workshop(application, 4)
    other = Session(settings)
    try:
        other.refresh(mutate_removals=False)
        found = {item.path.name: item for item in other.library.items}
        dawn = found["dawn.png"]
        for scene in scenes[:2]:
            other.pairings.choose_still(found[scene], dawn.path)
        made = other.playlists.create("Newer")
        for scene in scenes[:2]:
            other.playlists.add(made.id, found[scene].path)
        newer = runtime_config.render(settings, other)
    finally:
        other.shutdown()
    assert "Newer" in newer and scenes[0] in newer
    assert not runtime_config.allows_web_wallpapers(text=newer)
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(newer, encoding="utf-8")
    shutil.rmtree(content / scenes[0])
    _heard(monkeypatch, HEARD[heard])
    application._web_off_on_disk_at_exit()
    now = target.read_text(encoding="utf-8")
    if heard == "control-reads-eight":
        assert now == newer, (
            "the step replaced another writer's newer document, which allows no web wallpaper"
        )
        return
    assert now == runtime_config.nothing_to_play_document(
        config.load_strict(), runtime_config.WEB_OFF_REASON
    ), "not the document that plays nothing"


@pytest.mark.parametrize("variant", ["turned-on-meanwhile", "control-left-off"])
def test_what_another_writer_installs_while_the_step_waits_for_the_lock_is_judged_as_it_is(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    """L2's other half. Web wallpapers are off, and the file on disk allows them (a publication
    from while they were on, the one after it cut): the step waits for the compiler lock, which
    another writer holds. That writer saw them turned on again: it saved that, and installed a
    newer document compiled for them on, before it let go. Before: the setting was read before
    the wait, and that newer document was replaced. Now the setting and the file are read again
    under the lock, and it is left. The control: the writer leaves them off, and its document,
    which allows them, is replaced."""
    application = profiles().application
    session = application.session
    settings = config.load_strict()
    assert settings.allow_web_wallpapers is False
    on = replace(settings, allow_web_wallpapers=True)
    stale = runtime_config.render(on, session)
    newer = runtime_config.render(replace(on, shuffle=not on.shuffle), session)
    assert stale != newer and runtime_config.allows_web_wallpapers(text=newer)
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(stale, encoding="utf-8")
    holding = threading.Event()
    waiting = threading.Event()
    failures: list[str] = []
    lock = runtime_config.compiler_lock

    def watched(*arguments: Any, **keywords: Any) -> Any:
        if threading.current_thread() is threading.main_thread():
            waiting.set()
        return lock(*arguments, **keywords)

    def writer() -> None:
        with lock():
            holding.set()
            if not waiting.wait(GIVE_UP):
                failures.append("the step never waited for the compiler lock")
                return
            if variant == "turned-on-meanwhile":
                config.update({"allow_web_wallpapers": True})
            target.write_text(newer, encoding="utf-8")

    monkeypatch.setattr(runtime_config, "compiler_lock", watched)
    thread = threading.Thread(target=writer, name="test-writer")
    thread.start()
    try:
        assert holding.wait(GIVE_UP), "the writer never took the compiler lock"
        application._web_off_on_disk_at_exit()
    finally:
        waiting.set()
        thread.join()
    assert not failures, failures
    now = target.read_text(encoding="utf-8")
    if variant == "turned-on-meanwhile":
        assert config.load_strict().allow_web_wallpapers is True
        assert now == newer, "the step replaced a document another writer installed for them on"
        return
    assert not runtime_config.allows_web_wallpapers(text=now), (
        "settings.toml says web wallpapers are off, and the file on disk still allows them"
    )
