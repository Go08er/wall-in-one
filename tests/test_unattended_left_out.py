"""A reference to a playlist that can't play, where nobody watches: left out, one line each.

``--write-config``, ``--service-startup-prepare`` and ``--sync-runtime-health``
each compile with nobody watching. A default, a schedule rule or a display's
own playlist that names a playlist which doesn't exist, or has nothing to
play, used to refuse them: ``--write-config`` exited 1,
``--service-startup-prepare`` printed "repair it in the app" and wrote no
``runtime.toml`` (so a profile that had never published stopped its service),
and the health sync saved nothing.

They now build without that reference (`runtime_config.LeftOut`), print one
``warning:`` line for each saying which reference, whose it is and why, and
exit as for a clean compile. This holds whether or not ``settings.toml`` can
be changed: the rule that used to apply only to a default in a file that
can't be changed (kept read-only, or holding keys this build doesn't know) is
the rule everywhere, and nothing is tried on the file. A store that was not
read in full is different: the compile refuses that file by name, as it
always has, and the last good ``runtime.toml`` stays.

Real permission bits where a file is kept read-only; each test restores them.
"""

from __future__ import annotations

import contextlib
import json
import os
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests import released
from tests.released import released_source  # noqa: F401
from tests.test_runtime_health_sync import _media, _snapshot
from wall_in_one import cli, config, paths, runtime_config
from wall_in_one.control import client
from wall_in_one.control.protocol import Response
from wall_in_one.library import displays, pairings, playlists, schedules
from wall_in_one.library.model import MediaItem

COMPILES = ("--write-config", "--service-startup-prepare")
ALL_MEDIA = runtime_config.FALLBACK_PLAYLIST_ID


@contextlib.contextmanager
def _settings_kept_read_only() -> Iterator[None]:
    """The folder ``settings.toml`` is in, read-only for as long as this lasts."""
    folder = paths.settings_path().parent
    folder.chmod(0o500)
    try:
        yield
    finally:
        folder.chmod(0o700)


def _profile(
    tmp_path: Path, default: str, *, published: bool
) -> tuple[MediaItem, playlists.Playlist]:
    """One picture, one playlist ("Lone") holding it, and ``default`` saved as the default.

    ``published``: a ``runtime.toml`` was written first, while the default
    still named nothing wrong (none at all).
    """
    item = _media(tmp_path)
    store = playlists.Store.open()
    lone = store.add(store.create("Lone").id, item.path)
    if published:
        assert cli.main(["--write-config"]) == 0
    config.update({"active_playlist": default})
    return item, lone


def _document() -> dict[str, Any]:
    return tomllib.loads(paths.runtime_config_path().read_text(encoding="utf-8"))


def _warnings(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith("warning: ")]


def _default_warning(reference: str) -> str:
    return (
        f"warning: active_playlist in {paths.settings_path()} names a playlist that "
        f'doesn\'t exist ({reference!r}); building with "All media" as the default'
    )


@pytest.mark.parametrize(
    "read_only", [True, False], ids=["settings read-only", "settings writable"]
)
@pytest.mark.parametrize("published", [True, False], ids=["published before", "never published"])
@pytest.mark.parametrize("verb", COMPILES)
def test_a_compile_builds_a_default_that_names_nothing_as_all_media(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], verb: str, published: bool, read_only: bool
) -> None:
    """Before: ``--write-config`` exit 1; the prestart exit 0 with "repair it in the app" and,
    never having published, no ``runtime.toml`` at all. Where the settings could be changed
    that was still so until this round."""
    if read_only and os.geteuid() == 0:
        pytest.skip("root writes to a read-only folder")
    _profile(tmp_path, "ghost", published=published)
    saved = paths.settings_path().read_bytes()
    capsys.readouterr()

    with _settings_kept_read_only() if read_only else contextlib.nullcontext():
        code = cli.main([verb])
        said = capsys.readouterr()

    assert code == 0, said.err
    assert _warnings(said.err) == [_default_warning("ghost")]
    assert "error:" not in said.err and "repair it in the app" not in said.err
    assert "could not be compiled" not in said.err, "nothing was softened: it compiled"
    assert paths.settings_path().read_bytes() == saved, "nothing is tried on settings.toml"
    document = _document()
    assert document["default_playlist"] == ALL_MEDIA
    assert {playlist["name"] for playlist in document["playlists"]} == {"All media", "Lone"}
    # Published before, with no default at all: the same document, already current.
    assert ("nothing changed" if published else f"wrote: {paths.runtime_config_path()}") in said.out


def test_a_compile_says_unknown_keys_and_the_default_they_keep_separately(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other way a ``settings.toml`` can't be changed: one line for each thing."""
    _profile(tmp_path, "ghost", published=False)
    target = paths.settings_path()
    target.write_bytes(target.read_bytes() + b"from_the_future = true\n")
    saved = target.read_bytes()
    capsys.readouterr()

    code = cli.main(["--write-config"])
    said = capsys.readouterr()

    assert code == 0, said.err
    unknown, default = _warnings(said.err)
    assert "from_the_future" in unknown and "leaving the file unchanged" in unknown
    assert default == _default_warning("ghost")
    assert _document()["default_playlist"] == ALL_MEDIA
    assert target.read_bytes() == saved


@pytest.mark.parametrize("verb", COMPILES)
def test_a_compile_leaves_out_each_kind_of_reference_and_publishes_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    """The default, a rule and a display's own playlist; one that doesn't exist and one
    that has nothing to play. One line each, exit 0, and everything else in the document."""
    _item, lone = _profile(tmp_path, "Draft", published=True)
    config.update(
        {"display_mode": config.DISPLAY_MODE_INDEPENDENT, "theme_source_connector": "DP-1"}
    )
    store = playlists.Store.open()
    draft = store.create("Draft")
    rules = schedules.Store.open()
    rules.add(lone.id, rule_id="kept")
    rules.add("ghost", rule_id="dangling")
    rules.add(draft.id, rule_id="nothing")
    assigned = displays.Store.open()
    assigned.assign("DP-1", lone.id)
    assigned.assign("HDMI-A-1", "ghost")
    assigned.assign("eDP-1", draft.id)
    capsys.readouterr()

    code = cli.main([verb])
    said = capsys.readouterr()

    assert code == 0, said.err
    assert "error:" not in said.err
    assert _warnings(said.err) == [
        f"warning: active_playlist in {paths.settings_path()} names playlist 'Draft', which "
        'has nothing to play; building with "All media" as the default',
        "warning: schedule rule 'dangling' names a playlist that doesn't exist ('ghost'); "
        "building without that rule",
        "warning: schedule rule 'nothing' names playlist 'Draft', which has nothing to play; "
        "building without that rule",
        "warning: the display assignment for 'HDMI-A-1' names a playlist that doesn't exist "
        "('ghost'); building without it, so that display follows the schedule and the default",
        "warning: the display assignment for 'eDP-1' names playlist 'Draft', which has nothing "
        "to play; building without it, so that display follows the schedule and the default",
    ]
    document = _document()
    emitted = {playlist["id"] for playlist in document["playlists"]}
    assert emitted == {ALL_MEDIA, lone.id}
    assert document["default_playlist"] == ALL_MEDIA
    assert document["schedules"] == [{"id": "kept", "playlist": lone.id, "enabled": True}]
    assert document["displays"] == [{"connector": "DP-1", "playlist": lone.id}]

    # Said by every run, since each is a new process with nobody to remember;
    # and the document is already current the second time.
    again = cli.main([verb])
    said = capsys.readouterr()
    assert again == 0 and len(_warnings(said.err)) == 5
    assert "nothing changed" in said.out


def test_a_default_by_name_is_built_as_its_playlist_and_nothing_is_said(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _item, lone = _profile(tmp_path, "lone", published=False)
    capsys.readouterr()

    code = cli.main(["--write-config"])
    said = capsys.readouterr()

    assert code == 0 and said.err == "", said.err
    assert _document()["default_playlist"] == lone.id


def test_the_health_sync_saves_its_finding_past_references_it_leaves_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before: exit 1 ("default playlist refers to unknown playlist"), and no marker saved."""
    item, _lone = _profile(tmp_path, "ghost", published=True)
    schedules.Store.open().add("ghost", rule_id="dangling")
    assert cli.main(["--write-config"]) == 0, "the document the service reports from"
    saved = paths.settings_path().read_bytes()

    def send(verb: str) -> Response:
        return _snapshot(item) if verb == "status" else Response.success("reloaded")

    monkeypatch.setattr(client, "send_runtime", send)
    capsys.readouterr()

    code = cli.main(["--sync-runtime-health"])
    said = capsys.readouterr()

    assert code == 0, said.err
    assert _warnings(said.err) == [
        _default_warning("ghost"),
        "warning: schedule rule 'dangling' names a playlist that doesn't exist ('ghost'); "
        "building without that rule",
    ], "each said once, though this command compiles twice"
    assert "error:" not in said.err
    assert paths.settings_path().read_bytes() == saved
    assert pairings.Store.open().health(pairings.Identity.of(item)).is_borked
    document = _document()
    assert document["default_playlist"] == ALL_MEDIA and "schedules" not in document
    assert "renderer rejected this wallpaper" in paths.runtime_config_path().read_text("utf-8")


def _from_a_newer_build(path: Path) -> None:
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["version"] = 99
    path.write_text(json.dumps(saved), encoding="utf-8")


def _cannot_be_read(path: Path) -> None:
    path.chmod(0o000)


def _damaged(path: Path) -> None:
    path.write_text("{ this is not JSON", encoding="utf-8")


@pytest.mark.parametrize(
    ("state_path", "store_name"),
    (
        (playlists.state_path, "playlists"),
        (schedules.state_path, "schedules"),
        (displays.state_path, "display assignments"),
    ),
)
@pytest.mark.parametrize(
    "fault", (_from_a_newer_build, _cannot_be_read, _damaged), ids=lambda made: made.__name__
)
@pytest.mark.parametrize("verb", COMPILES)
def test_a_compile_leaves_nothing_out_over_a_store_that_was_not_read(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    verb: str,
    fault: Callable[[Path], None],
    state_path: Callable[[], Path],
    store_name: str,
) -> None:
    """A default, a rule and a display that nothing which was read answers to.

    "Couldn't look" is not "no such playlist": the compile refuses the file
    by name, as it always has, and the last good ``runtime.toml`` stays,
    rather than one without those references being published over it.
    """
    if fault is _cannot_be_read and os.geteuid() == 0:
        pytest.skip("root reads a file with no permission bits")
    _item, lone = _profile(tmp_path, "ghost", published=False)
    config.update(
        {"display_mode": config.DISPLAY_MODE_INDEPENDENT, "theme_source_connector": "DP-1"}
    )
    schedules.Store.open().add("ghost", rule_id="dangling")
    displays.Store.open().assign("HDMI-A-1", "ghost")
    assert cli.main(["--write-config"]) == 0, "read in full: left out, and published"
    assert len(_warnings(capsys.readouterr().err)) == 3
    good = paths.runtime_config_path().read_bytes()
    target = state_path()
    fault(target)

    try:
        code = cli.main([verb])
    finally:
        target.chmod(0o600)
    said = capsys.readouterr()

    assert "error:" in said.err and store_name in said.err and target.name in said.err
    assert "the existing runtime configuration was left untouched" in said.err
    assert [line for line in _warnings(said.err) if "building" in line] == [], "nothing left out"
    assert paths.runtime_config_path().read_bytes() == good
    if verb == "--write-config":
        assert code == 1
    else:
        assert code == 0 and "validating the last-known-good runtime" in said.err
    assert lone.id in good.decode("utf-8")


# -- what the released version left on disk ----------------------------------------------------

_RELEASED_SAVES_WHAT_CANNOT_PLAY = r"""
from dataclasses import replace
from pathlib import Path

from wall_in_one import config, runtime_config
from wall_in_one.library import displays, playlists, schedules
from wall_in_one.session import Session

store = playlists.Store.open()
draft = store.create("Draft")
gone = store.add(store.create("Gone").id, Path(sys.argv[1]))
schedules.Store.open().add(draft.id, rule_id="released-rule")
displays.Store.open().assign("HDMI-A-1", gone.id)
settings = replace(
    config.load(),
    active_playlist="Nowhere",
    display_mode="independent",
    theme_source_connector="DP-1",
)
config.save(settings)

session = Session(settings, owns_playback=False)
try:
    session.refresh(mutate_removals=False)
    try:
        runtime_config.render(settings, session)
    except runtime_config.RuntimeConfigError as error:
        print(f"refused: {error}", flush=True)
    else:
        print("compiled", flush=True)
finally:
    session.shutdown()
"""


def test_a_profile_the_released_version_saved_is_built_without_what_cannot_play(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    released_source: Path,  # noqa: F811
) -> None:
    """Review rule 4: the producer is 0.2.2 itself, not this checkout's stores.

    0.2.2 saves, through its own stores, a default that names nothing, a rule
    on an empty playlist and a display on a playlist whose only file is gone.
    Its own compiler refuses that profile. This checkout reads the same
    files, changes none of them, and publishes without those three.
    """
    _item, lone = _profile(tmp_path, "", published=True)
    stores = (playlists.state_path(), schedules.state_path(), displays.state_path())
    done = released.run(
        released_source, _RELEASED_SAVES_WHAT_CANNOT_PLAY, str(tmp_path / "unmounted" / "a.mp4")
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("refused: "), "0.2.2 compiles none of it: " + done.stdout
    saved = {path: path.read_bytes() for path in (*stores, paths.settings_path())}
    capsys.readouterr()

    code = cli.main(["--write-config"])
    said = capsys.readouterr()

    assert code == 0, said.err
    assert "error:" not in said.err
    left_out = [line for line in _warnings(said.err) if "building" in line]
    assert len(left_out) == 3, left_out
    assert left_out[0] == _default_warning("Nowhere")
    assert "'released-rule' names playlist 'Draft', which has nothing to play" in left_out[1]
    assert "'HDMI-A-1' names playlist 'Gone', which has nothing to play" in left_out[2]
    document = _document()
    assert document["default_playlist"] == ALL_MEDIA
    assert {playlist["id"] for playlist in document["playlists"]} == {ALL_MEDIA, lone.id}
    assert "schedules" not in document and "displays" not in document
    assert {path: path.read_bytes() for path in saved} == saved, "what 0.2.2 saved is as it was"
