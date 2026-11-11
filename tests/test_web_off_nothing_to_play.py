"""Turning "Allow web wallpapers" off when web pages are all that plays: the service plays nothing.

The owner's decision (2026-10-24): a display whose only wallpaper is a web page
shows nothing while web wallpapers are off, and the app says "Nothing to play:
web wallpapers are off". Before, the compiler wrote an All media with no
``entries`` key (P1, already so at 0.4.0), which no service could parse, so the
service refused the reload and kept playing the page (review r11c, M1).

These compile the app's own documents for a library whose only playable item
is a web page, with Steam a fixture under the test's own home and the service
faked (no socket is opened). The documents are also the Rust service's
fixtures (``service/tests/fixtures/web-off/``), so the service is tested on
exactly what this compiler writes; `test_the_service_fixtures_are_what_the_compiler_writes`
keeps them so.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests import workshop_picks
from wall_in_one import cli, config, paths, runtime_config, update_status
from wall_in_one.control import client
from wall_in_one.control.protocol import Response
from wall_in_one.library import scan
from wall_in_one.session import Session

WEB = "2777242023"
FIXTURES = Path(__file__).resolve().parents[1] / "service/tests/fixtures/web-off"
ALL_MEDIA = runtime_config.FALLBACK_PLAYLIST_ID


def _library(home: Path, *, still_beside: bool) -> Path:
    """A web page (with its still) under the home's Steam; beside it a picture, if asked."""
    item = home / ".local/share/Steam/steamapps/workshop/content/431960" / WEB
    item.mkdir(parents=True)
    (item / "project.json").write_text(
        json.dumps(
            {"type": "web", "title": "Electric Eye", "file": "index.html", "preview": "p.jpg"}
        )
    )
    (item / "index.html").write_text("<p>x</p>")
    (item / "p.jpg").write_bytes(b"x")
    root = home / "Pictures/wallpapers"
    automatic = root / "Wall-in-One" / "Automatic Stills"
    automatic.mkdir(parents=True)
    # A web page has no automatic still: its one is a still the person picked.
    workshop_picks.save(workshop_picks.pick(WEB, home / "picked-electric-eye.png"))
    if still_beside:
        (root / "dawn.png").write_bytes(b"\x89PNG")
    return root


def _service(
    monkeypatch: pytest.MonkeyPatch, *, schemas: list[int] | None, units: str = "current"
) -> list[str]:
    """A faked service listing ``schemas`` (None: none is running); what it was sent."""
    sent: list[str] = []

    def send(verb: str, *_a: object, **_k: object) -> Response:
        sent.append(verb)
        if schemas is None:
            raise client.NotRunningError("no wallpaper service in this test")
        if verb == "status":
            return Response.success(
                json.dumps({"status_version": 2, "supported_config_schemas": schemas})
            )
        return Response.success("reloaded")

    monkeypatch.setattr(client, "send_runtime", send)
    monkeypatch.setattr(
        update_status, "inspect_loaded_units", lambda **_k: update_status.LoadedUnits(units, "t")
    )
    return sent


def _publish(root: Path, *, allow_web: bool) -> runtime_config.Published:
    settings = config.Settings(roots=(root,), allow_web_wallpapers=allow_web)
    config.save(settings)
    session = Session(settings)
    try:
        session.refresh()
        return runtime_config.publish_changes(settings, session)
    finally:
        session.shutdown()


def _document() -> dict[str, Any]:
    return tomllib.loads(paths.runtime_config_path().read_text(encoding="utf-8"))


@pytest.fixture
def web_only(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    _service(monkeypatch, schemas=[4, 5, 6, 7, 8])
    yield _library(Path(os.environ["HOME"]), still_beside=False)


def test_turning_web_off_when_web_pages_are_all_that_plays_publishes_nothing_to_play(
    web_only: Path,
) -> None:
    """Review probe `probe_web_only_off.py`. Before: All media without `entries`, unparseable."""
    on = _publish(web_only, allow_web=True)
    on_text = paths.runtime_config_path().read_text()
    off = _publish(web_only, allow_web=False)
    document = _document()
    # The probe's point first: what the service is given to play once web is off.
    assert document["playlists"] == [
        {
            "id": ALL_MEDIA,
            "name": "All media",
            "nothing_to_play": "web wallpapers are off",
            "entries": [],
        }
    ]
    assert document["schema_version"] == 8
    assert "allow_web_wallpapers" not in document["settings"]
    assert document["default_playlist"] == ALL_MEDIA
    assert runtime_config.left_out_notice(off.left_out) == (
        "Nothing to play: web wallpapers are off."
    )
    # What it replaced is kept, for a service that refuses it (the GUI's guard).
    assert off.replaced == on_text
    assert runtime_config.retained_web_wallpapers(text=off.replaced) == ("Electric Eye",)
    assert runtime_config.retained_web_wallpapers() == ()
    # While it was on, the page played, and the document said it may.
    allowed = tomllib.loads(on_text)
    assert allowed["schema_version"] == runtime_config.NOTHING_TO_PLAY_SCHEMA_VERSION
    assert allowed["settings"]["allow_web_wallpapers"] is True
    assert f'scene_id = "{WEB}"' in on_text
    assert not on.left_out


def test_control_a_still_beside_the_page_plays_and_says_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control (`with-still`): it played before this change and still does, at schema 4."""
    _service(monkeypatch, schemas=[4, 5, 6, 7, 8])
    root = _library(Path(os.environ["HOME"]), still_beside=True)
    _publish(root, allow_web=True)
    off = _publish(root, allow_web=False)
    document = _document()
    assert document["schema_version"] == runtime_config.SCHEMA_VERSION
    [all_media] = document["playlists"]
    assert "nothing_to_play" not in all_media
    assert [entry["kind"] for entry in all_media["entries"]] == ["still"]
    assert not off.left_out


def test_a_service_that_predates_schema_8_is_refused_nothing_and_keeps_its_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 for an older service: a clear refusal, never a document it can't parse."""
    _service(monkeypatch, schemas=[4, 5, 6, 7, 8])
    root = _library(Path(os.environ["HOME"]), still_beside=False)
    _publish(root, allow_web=True)
    before = paths.runtime_config_path().read_bytes()
    _service(monkeypatch, schemas=[4, 5])
    with pytest.raises(runtime_config.RuntimeConfigError, match="No changes were saved") as refused:
        _publish(root, allow_web=False)
    assert "has nothing to play (web wallpapers are off)" in str(refused.value)
    assert "schema_version 8" in str(refused.value)
    assert paths.runtime_config_path().read_bytes() == before


def test_an_older_service_gets_web_pages_without_allow_web_wallpapers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The step-down: schema 8's key is left out first, so 0.4.0 still loads the document."""
    _service(monkeypatch, schemas=[4, 5])
    root = _library(Path(os.environ["HOME"]), still_beside=True)
    _publish(root, allow_web=True)
    text = paths.runtime_config_path().read_text()
    document = _document()
    assert document["schema_version"] == runtime_config.SCHEMA_VERSION
    assert "allow_web_wallpapers" not in text
    assert f'scene_id = "{WEB}"' in text
    # The health sync finds what was published for that service.
    settings = config.load_strict()
    session = Session(settings)
    try:
        session.refresh()
        status = {"status_version": 2, "supported_config_schemas": [4, 5]}
        built = runtime_config.compiled_as_published(
            settings, session, status, runtime_config.document_generation(text)
        )
        assert built.document == text
        # And for a service that reads 8, the document with the key.
        full = runtime_config.compiled(settings, session).document
        status8 = {"status_version": 2, "supported_config_schemas": [4, 5, 6, 7, 8]}
        found = runtime_config.compiled_as_published(
            settings, session, status8, runtime_config.document_generation(full)
        )
        assert "allow_web_wallpapers = true" in found.document
    finally:
        session.shutdown()


def test_the_health_sync_finds_a_published_document_that_plays_nothing(web_only: Path) -> None:
    _publish(web_only, allow_web=False)
    text = paths.runtime_config_path().read_text()
    settings = config.load_strict()
    session = Session(settings)
    try:
        session.refresh()
        for schemas in ([4, 5, 6, 7, 8], [4, 5]):
            status = {"status_version": 2, "supported_config_schemas": schemas}
            built = runtime_config.compiled_as_published(
                settings, session, status, runtime_config.document_generation(text)
            )
            assert built.document == text, schemas
    finally:
        session.shutdown()


def test_service_start_publishes_the_document_that_plays_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ExecStartPre (`--service-startup-prepare`) with no service running and current units."""
    root = _library(Path(os.environ["HOME"]), still_beside=False)
    config.save(config.Settings(roots=(root,), allow_web_wallpapers=False))
    _service(monkeypatch, schemas=None)
    assert cli.main(["--service-startup-prepare"]) == 0
    err = capsys.readouterr().err
    assert "nothing to play: web wallpapers are off" in err
    document = _document()
    assert document["schema_version"] == runtime_config.NOTHING_TO_PLAY_SCHEMA_VERSION
    assert document["playlists"][0]["entries"] == []
    assert runtime_config.retained_web_wallpapers() == ()


def test_service_start_with_units_that_cant_be_verified_keeps_the_exit_78_guard(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not confirmed for 8, the compile is refused; the kept document names the page: 78."""
    root = _library(Path(os.environ["HOME"]), still_beside=False)
    _service(monkeypatch, schemas=[4, 5, 6, 7, 8])
    _publish(root, allow_web=True)
    config.save(config.Settings(roots=(root,), allow_web_wallpapers=False))
    _service(monkeypatch, schemas=None, units="unverified")
    assert cli.main(["--service-startup-prepare"]) == cli.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "still plays web wallpapers (Electric Eye)" in err
    assert f'scene_id = "{WEB}"' in paths.runtime_config_path().read_text()


def test_the_unattended_warning_doesnt_promise_a_blank_display(
    web_only: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review r13 L3. Before: "the wallpaper service shows nothing"; Noctalia keeps its still."""
    config.save(config.Settings(roots=(web_only,), allow_web_wallpapers=False))
    assert cli.main(["--write-config"]) == 0
    err = capsys.readouterr().err
    assert "nothing to play: web wallpapers are off" in err
    assert "shows nothing" not in err
    assert "plays no wallpaper" in err and "the last still Noctalia was given stays on it" in err


# -- web wallpapers on, the key left out for a service not confirmed for 8 (r13 L5) -----------


def test_web_on_with_units_that_cant_be_verified_says_web_wallpapers_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review r13 probe F. Before: the key was left out and nothing said so, and the service
    then refused every page with "web wallpapers are off" to someone who had turned them on."""
    root = _library(Path(os.environ["HOME"]), still_beside=True)
    _service(monkeypatch, schemas=[4, 5, 6, 7, 8], units="unverified")
    published = _publish(root, allow_web=True)
    text = paths.runtime_config_path().read_text()
    assert "allow_web_wallpapers" not in text  # the step-down happened
    assert f'scene_id = "{WEB}"' in text
    assert runtime_config.left_out_notice(published.left_out) == (
        "Web wallpapers aren\u2019t played until the wallpaper service is confirmed to read "
        "them: restart it after an update, and make sure its installed units are current."
    )
    warnings = [one.warning() for one in published.left_out]
    assert len(warnings) == 1 and "allow_web_wallpapers is left out" in warnings[0], warnings
    assert "schema_version 8" in warnings[0]


def test_control_web_on_with_units_verified_writes_the_key_and_says_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _library(Path(os.environ["HOME"]), still_beside=True)
    _service(monkeypatch, schemas=[4, 5, 6, 7, 8], units="current")
    published = _publish(root, allow_web=True)
    assert "allow_web_wallpapers = true" in paths.runtime_config_path().read_text()
    assert not published.left_out


def test_an_older_service_that_plays_web_pages_isnt_told_they_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One that predates 8 plays them without the key, as 0.4.0 did: nothing waits."""
    root = _library(Path(os.environ["HOME"]), still_beside=True)
    _service(monkeypatch, schemas=[4, 5])
    published = _publish(root, allow_web=True)
    text = paths.runtime_config_path().read_text()
    assert f'scene_id = "{WEB}"' in text and "allow_web_wallpapers" not in text
    assert not published.left_out


def test_web_on_without_a_web_page_says_nothing_of_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """Units that can't be verified, web on, but no web page: the key changes nothing."""
    root = Path(os.environ["HOME"]) / "Pictures/wallpapers"
    root.mkdir(parents=True)
    (root / "dawn.png").write_bytes(b"\x89PNG")
    _service(monkeypatch, schemas=[4, 5, 6, 7, 8], units="unverified")
    published = _publish(root, allow_web=True)
    assert "allow_web_wallpapers" not in paths.runtime_config_path().read_text()
    assert not published.left_out


def test_the_document_that_plays_nothing_is_built_from_settings_alone() -> None:
    settings = config.Settings(display_mode="independent", theme_source_connector="DP-1")
    text = runtime_config.nothing_to_play_document(settings, runtime_config.WEB_OFF_REASON)
    document = tomllib.loads(text)
    assert document["schema_version"] == runtime_config.NOTHING_TO_PLAY_SCHEMA_VERSION
    assert document["default_playlist"] == ALL_MEDIA
    assert document["playlists"] == [
        {
            "id": ALL_MEDIA,
            "name": "All media",
            "nothing_to_play": "web wallpapers are off",
            "entries": [],
        }
    ]
    assert document["settings"]["display_mode"] == "independent"
    assert "allow_web_wallpapers" not in document["settings"]
    assert runtime_config.document_generation(text) == document["config_generation"]
    with pytest.raises(runtime_config.RuntimeConfigError, match="at most 128"):
        runtime_config.nothing_to_play_document(settings, "x" * 129)


def test_what_a_held_text_plays_is_judged_with_one_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _library(Path(os.environ["HOME"]), still_beside=False)
    named = (
        'schema_version = 4\n[[playlists]]\nid = "p"\n'
        f'[[playlists.entries]]\nscene_id = "{WEB}"\n'
        '[[playlists.entries]]\nscene_id = "4000000001"\n'
    )
    scans: list[object] = []
    workshop_items = scan.workshop_items

    def counted(*args: object, **kwargs: object) -> object:
        scans.append(args)
        return workshop_items(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(scan, "workshop_items", counted)
    titles, unchecked = runtime_config.web_wallpapers_in([named, "not toml ["])
    assert (titles, unchecked) == (("Electric Eye",), ("4000000001",))
    assert len(scans) == 1
    assert runtime_config.web_wallpapers_in(["schema_version = 4\n"]) == ((), ())


# -- the Rust service's fixtures are this compiler's documents --------------------------------

_PROGRAM = re.compile(r'^(noctalia|mpvpaper|linux_wallpaperengine)_program = ".*"$', re.MULTILINE)


_ENTRY_ID = re.compile(r'^id = "[0-9a-f]{16}"$', re.MULTILINE)


def _as_fixture(text: str, names: dict[str, str]) -> str:
    """``text`` with this run's home, programs and entry ids (path hashes) made fixed.

    ``names``: each id's fixed name so far, shared by the documents of one run, so that
    a wallpaper keeps its id from one document to the next as it does in the app.
    """
    text = text.replace(os.environ["HOME"], "/home/user")
    text = _PROGRAM.sub(lambda match: f'{match[1]}_program = "/bin/true"', text)

    def named(match: re.Match[str]) -> str:
        fixed = names.setdefault(match[0], f"entry-{len(names) + 1}")
        return f'id = "{fixed}"'

    return _ENTRY_ID.sub(named, text)


def _fixtures(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The four documents of the review's probes: web only and with a still, on then off."""
    _service(monkeypatch, schemas=[4, 5, 6, 7, 8])
    root = _library(Path(os.environ["HOME"]), still_beside=False)
    made: dict[str, str] = {}
    names: dict[str, str] = {}
    for name in ("web-only", "with-still"):
        if name == "with-still":
            (root / "dawn.png").write_bytes(b"\x89PNG")
        for allow, state in ((True, "on"), (False, "off")):
            _publish(root, allow_web=allow)
            made[f"{name}-{state}.toml"] = _as_fixture(
                paths.runtime_config_path().read_text(), names
            )
    return made


def test_the_service_fixtures_are_what_the_compiler_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`WALL_IN_ONE_BLESS_WEB_FIXTURES=1` writes them after a deliberate change."""
    made = _fixtures(monkeypatch)
    if os.environ.get("WALL_IN_ONE_BLESS_WEB_FIXTURES"):
        FIXTURES.mkdir(parents=True, exist_ok=True)
        for name, text in made.items():
            (FIXTURES / name).write_text(text, encoding="utf-8")
    generation = re.compile(r'^config_generation = ".*"$', re.MULTILINE)
    for name, text in made.items():
        recorded = (FIXTURES / name).read_text(encoding="utf-8")
        # The generation hashes the paths of the run that made it. The page's
        # still is a pick now; the service's fixtures keep the path they always
        # named, which to the service is only a path.
        text = text.replace(
            "/home/user/picked-electric-eye.png",
            f"/home/user/Pictures/wallpapers/Wall-in-One/Automatic Stills/{WEB}.png",
        )
        assert generation.sub("", recorded) == generation.sub("", text), name
