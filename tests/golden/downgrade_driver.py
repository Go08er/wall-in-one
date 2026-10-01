"""Typical edits made by an *older* build, run as a child process.

``tests/test_golden_profile.py`` runs this with ``PYTHONPATH`` set to an older
checkout's ``src`` and every XDG variable pointed at a golden sandbox. It must
therefore import nothing from this checkout and use only Store APIs that
v0.1.4 (``dbfbaa0``) already had.

Usage: ``downgrade_driver.py version``, ``downgrade_driver.py edit [FILE...]``,
``downgrade_driver.py same-schedule-edit``, ``downgrade_driver.py compile`` or
``downgrade_driver.py prepare``. Prints one JSON object: the module and
version that ran, which records each edit touched, and any edit that was
refused (by message); ``compile`` reports what the old build's runtime
publication did, and ``prepare`` the exit status of its service unit's
``--service-startup-prepare``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import wall_in_one
from wall_in_one import config, paths
from wall_in_one.library import displays, favourites, pairings, playlists, schedules
from wall_in_one.library.model import Kind, MediaItem

OLD_PLAYLIST = "Written by the old build"


def _state(name: str) -> dict[str, object]:
    value = json.loads((paths.app_state_dir() / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _records(name: str, key: str) -> list[dict[str, object]]:
    """Read records leniently: a newer version must still name what to edit."""
    value = _state(name).get(key, [])
    return (
        [record for record in value if isinstance(record, dict)] if isinstance(value, list) else []
    )


def _a_picture() -> Path:
    roots = config.load().roots
    return sorted(roots[0].glob("*.png"))[0]


def edit_playlists() -> list[str]:
    first = str(_records("playlists.json", "playlists")[0]["id"])
    store = playlists.Store.open()
    store.add(first, _a_picture())
    store.create(OLD_PLAYLIST)
    return [first]


def edit_schedules() -> list[str]:
    rule = str(_records("schedules.json", "rules")[0]["id"])
    store = schedules.Store.open()
    store.set_enabled(rule, True)
    store.add("quick-choice", start="06:00", end="06:30")
    return [rule]


def same_schedule_edit() -> list[str]:
    """One deterministic, unnamed schedule edit that every build makes the same way.

    The test runs it here under the old build and again under this one from
    the same starting bytes: an unnamed edit must write identical bytes.
    """
    rules = _records("schedules.json", "rules")
    first = str(rules[0]["id"])
    second = rules[1]
    store = schedules.Store.open()
    store.set_enabled(first, rules[0].get("enabled") is False)
    store.update(str(second["id"]), str(second["playlist"]), weekdays=["sat"])
    store.add("quick-choice", weekdays=["sun"], start="06:00", end="06:30", rule_id="same-edit")
    store.move("same-edit", 0)
    return [first, str(second["id"]), "same-edit"]


def edit_pairings() -> list[str]:
    record = _records("pairings.json", "pairings")[1]
    identity = str(record["identity"])
    medium, _, source = identity.partition(":")
    item = (
        MediaItem(path=Path("/"), kind=Kind.SCENE, size=0, mtime=0, scene=source)
        if medium == "scene"
        else MediaItem(
            path=Path(source),
            kind=Kind.VIDEO if medium == "video" else Kind.STILL,
            size=0,
            mtime=0,
        )
    )
    pairings.Store.open().choose_palette(item, pairings.PalettePolicy(kind="builtin", name="Nord"))
    return [identity]


def edit_displays() -> list[str]:
    first = str(_records("playlists.json", "playlists")[0]["id"])
    displays.Store.open().assign("DP-7", first)
    return ["DP-7"]


def edit_favourites() -> list[str]:
    picture = _a_picture()
    store = favourites.Store.open()
    if picture in store.paths:
        store.discard(picture)
        store.add(picture)
    else:
        store.add(picture)
    return [str(picture)]


def edit_settings() -> list[str]:
    config.update({"opacity": 0.55})
    return ["opacity"]


def compile_runtime() -> str:
    """Publish ``runtime.toml`` the way the old GUI does after a rollback.

    The same calls in every build since v0.1.4: strict settings, a Session,
    one library scan, then ``runtime_config.update``. No child process may
    start (the scan and the compiler never need one), so the old build's own
    compile is all this exercises. Returns ``changed``, ``unchanged`` or
    ``refused: <why>``.
    """
    from wall_in_one import runtime_config
    from wall_in_one.session import Session

    _seal_processes()
    settings = config.load_strict()
    session = Session(settings)
    try:
        session.adopt_library_refresh(session.prepare_library_refresh().run())
        changed = runtime_config.update(settings, session)
    except runtime_config.RuntimeConfigError as error:
        return f"refused: {error}"
    finally:
        session.shutdown()
    return "changed" if changed else "unchanged"


def _seal_processes() -> None:
    """Neither the scan nor the compiler needs a child process; fail if one starts."""
    import subprocess

    def no_processes(*arguments: object, **_keywords: object) -> None:
        raise AssertionError(f"the old build tried to start {arguments!r}")

    subprocess.Popen = no_processes  # type: ignore[assignment,misc]


def prepare_service_start() -> int:
    """What the old service unit runs first after a rollback, before the old service.

    ``--service-startup-prepare`` publishes runtime.toml (or keeps the last
    good one); the unit's next step is the old service's own loader.
    """
    from wall_in_one import cli

    _seal_processes()
    return cli.main(["--service-startup-prepare"])


EDITS: dict[str, Callable[[], list[str]]] = {
    "playlists.json": edit_playlists,
    "schedules.json": edit_schedules,
    "pairings.json": edit_pairings,
    "displays.json": edit_displays,
    "favourites.json": edit_favourites,
    "settings.toml": edit_settings,
}


def main(arguments: list[str]) -> int:
    from wall_in_one.library import state_file

    report: dict[str, object] = {
        "module": wall_in_one.__file__,
        "version": getattr(wall_in_one, "__version__", "unknown"),
        # Release 1's forward-compatibility guard introduced this constant.
        "has_guard": hasattr(state_file, "NEWER_VERSION"),
        # The newest schedules.json it understands; 3 added rule names.
        "schedules_format": schedules.FORMAT_VERSION,
        # The newest playlists.json / displays.json it understands; 2 added
        # per-playlist rotation and the display opt-in.
        "playlists_format": playlists.FORMAT_VERSION,
        "displays_format": displays.FORMAT_VERSION,
        # The newest version of every store file it understands, so a test
        # can write one that is newer *for this build*, whichever it is.
        "formats": {
            "playlists.json": playlists.FORMAT_VERSION,
            "schedules.json": schedules.FORMAT_VERSION,
            "pairings.json": pairings.FORMAT_VERSION,
            "displays.json": displays.FORMAT_VERSION,
            "favourites.json": favourites.FORMAT_VERSION,
        },
    }
    if arguments[:1] == ["edit"]:
        chosen = arguments[1:] or list(EDITS)
        touched: dict[str, list[str]] = {}
        errors: dict[str, str] = {}
        for name in chosen:
            try:
                touched[name] = EDITS[name]()
            except Exception as error:  # the old build refusing is a result
                errors[name] = f"{type(error).__name__}: {error}"
        report["touched"] = touched
        report["errors"] = errors
    elif arguments[:1] == ["same-schedule-edit"]:
        report["touched"] = {"schedules.json": same_schedule_edit()}
        report["errors"] = {}
    elif arguments[:1] == ["compile"]:
        report["compile"] = compile_runtime()
    elif arguments[:1] == ["prepare"]:
        report["prepare"] = prepare_service_start()
    elif arguments[:1] != ["version"]:
        print(__doc__, file=sys.stderr)
        return 2
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
