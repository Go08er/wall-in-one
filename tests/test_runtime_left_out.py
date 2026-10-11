"""The build of the runtime document is tolerant: what can't play is left out and said.

One reference to a playlist that doesn't exist, or has nothing to play, used
to refuse the whole compile ("... refers to unknown playlist ...", "... has
no playable entries ..."): nothing was published, the service kept its last
document, and every later change waited behind it. So every save path had to
prove it could not create such a state, against what another program saved
meanwhile, and reviews kept finding holes in those proofs.

The compiler no longer relies on them. A reference it can't use is left out
and returned (`runtime_config.LeftOut`), and everything else is published:

* the default is built as "All media";
* a schedule rule is not emitted, so whatever would apply without it applies;
* a display's own playlist is not emitted, in ``runtime.toml`` or in
  ``runtime-overrides.toml``, so that display follows the rules and the default;
* a playlist holding entries of which none resolves is not emitted, like an
  empty one, where it used to refuse even with nothing referring to it.

Only over stores read in full: a store that couldn't be read, is damaged or
was saved by a newer build still refuses the build, naming the file. And the
service's contract is the same: nothing left out is ever named, so every
reference in either document names an emitted playlist.
"""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.test_runtime_config import _session
from wall_in_one import config, runtime_config
from wall_in_one.library import displays, pairings, playlists, schedules
from wall_in_one.runtime_config import (
    DEFAULT_OWNER,
    DISPLAY_OWNER,
    DOES_NOT_EXIST,
    NOTHING_TO_PLAY,
    SCHEDULE_OWNER,
    LeftOut,
)
from wall_in_one.session import Session

ALL_MEDIA = runtime_config.FALLBACK_PLAYLIST_ID


def _independent(settings: config.Settings) -> config.Settings:
    """Independent displays: assignments and connector rules reach the wire."""
    return replace(
        settings,
        display_mode=config.DISPLAY_MODE_INDEPENDENT,
        theme_source_connector="DP-1",
    )


def _unusable(session: Session, tmp_path: Path, how: str) -> tuple[str, str, str]:
    """A reference the compiler can't use: ``(reference, playlist name, why)``."""
    if how == "missing":
        return "ghost", "", DOES_NOT_EXIST
    if how == "empty":
        return session.playlists.create("Draft").id, "Draft", NOTHING_TO_PLAY
    if how == "empty, by name":
        session.playlists.create("Draft")
        return "draft", "Draft", NOTHING_TO_PLAY
    assert how == "nothing resolves"
    gone = session.playlists.create("Gone")
    session.playlists.add(gone.id, tmp_path / "not-in-library.mp4")
    return gone.id, "Gone", NOTHING_TO_PLAY


HOW = ("missing", "empty", "empty, by name", "nothing resolves")


def _documents(target: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    sidecar = runtime_config.overrides_path(target)
    return (
        tomllib.loads(target.read_text(encoding="utf-8")),
        tomllib.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else None,
    )


def _names_only_what_is_emitted(document: dict[str, Any], overrides: dict[str, Any] | None) -> None:
    """The service's contract (`service/src/config.rs`, ``reference``): nothing unknown is named."""
    emitted = {playlist["id"] for playlist in document["playlists"]}
    assert document["default_playlist"] in emitted
    for rule in document.get("schedules", []):
        assert rule["playlist"] in emitted, rule
    connectors = set()
    for display in document.get("displays", []):
        assert display["playlist"] in emitted, display
        connectors.add(display["connector"])
    for playlist in (overrides or {}).get("playlists", []):
        assert playlist["id"] in emitted, playlist
    for display in (overrides or {}).get("displays", []):
        assert display["connector"] in connectors, display


@pytest.mark.parametrize("how", HOW)
def test_a_default_that_cannot_play_is_built_as_all_media(tmp_path: Path, how: str) -> None:
    """Before: "default playlist refers to ...", nothing published, the old document kept."""
    settings, session = _session(tmp_path)
    expected = runtime_config.render(replace(settings, active_playlist=""), session)
    reference, name, why = _unusable(session, tmp_path, how)
    settings = replace(settings, active_playlist=reference)
    target = tmp_path / "runtime.toml"
    target.write_text('schema_version = 4\nlast_known_good = "replaced"\n', encoding="utf-8")

    published = runtime_config.publish_changes(settings, session, target)

    assert published and published.written == (target,)
    assert published.left_out == (LeftOut(DEFAULT_OWNER, "", reference, name, why),)
    document, overrides = _documents(target)
    assert document["default_playlist"] == ALL_MEDIA
    # Everything else is published exactly as with no default saved.
    assert target.read_text(encoding="utf-8") == expected
    assert [rule["id"] for rule in document["schedules"]] == ["night"]
    _names_only_what_is_emitted(document, overrides)
    session.shutdown()


@pytest.mark.parametrize("how", HOW)
def test_a_rule_that_cannot_play_is_not_emitted(tmp_path: Path, how: str) -> None:
    """Before: "schedule 'extra' refers to ...", and no change reached playback."""
    settings, session = _session(tmp_path)
    expected = runtime_config.render(settings, session)
    reference, name, why = _unusable(session, tmp_path, how)
    session.schedules.add(reference, rule_id="extra", start="08:00", end="09:00")
    target = tmp_path / "runtime.toml"

    published = runtime_config.publish_changes(settings, session, target)

    assert published.left_out == (LeftOut(SCHEDULE_OWNER, "extra", reference, name, why),)
    document, overrides = _documents(target)
    # The rule is gone whole, and whatever applies without it applies: the
    # document is the one built before the rule was added.
    assert target.read_text(encoding="utf-8") == expected
    assert [rule["id"] for rule in document["schedules"]] == ["night"]
    assert document["default_playlist"] == "evening"
    _names_only_what_is_emitted(document, overrides)
    session.shutdown()


@pytest.mark.parametrize("how", HOW)
def test_a_display_whose_own_playlist_cannot_play_follows_the_rules(
    tmp_path: Path, how: str
) -> None:
    """Before: "display assignment for 'HDMI-A-1' refers to ...", nothing published."""
    settings, session = _session(tmp_path)
    settings = _independent(settings)
    expected = runtime_config.compiled(settings, session)
    reference, name, why = _unusable(session, tmp_path, how)
    session.displays.assign("HDMI-A-1", reference)
    # Its opt-in to beat global rules must not reach the overrides either:
    # the service skips, and says, an override naming a display it lacks.
    session.displays.set_beats_global_rules("HDMI-A-1", True)
    session.displays.set_beats_global_rules("DP-1", True)
    target = tmp_path / "runtime.toml"

    published = runtime_config.publish_changes(settings, session, target)

    assert published.left_out == (LeftOut(DISPLAY_OWNER, "HDMI-A-1", reference, name, why),)
    document, overrides = _documents(target)
    assert target.read_text(encoding="utf-8") == expected.document
    assert document["displays"] == [{"connector": "DP-1", "playlist": "evening"}]
    assert overrides is not None
    assert overrides["displays"] == [{"connector": "DP-1", "beats_global_rules": True}]
    _names_only_what_is_emitted(document, overrides)
    session.shutdown()


def test_a_playlist_whose_entries_all_went_is_not_emitted_and_stops_nothing(
    tmp_path: Path,
) -> None:
    """Before: "playlist 'Gone' has no playable entries; all 1 authored entries are missing
    or unresolved", refused though nothing referred to it (FULL7 AD-2 and AD-3 end here).

    Its own interval goes with it: the overrides name only emitted playlists.
    """
    settings, session = _session(tmp_path)
    expected = runtime_config.render(settings, session)
    gone = session.playlists.create("Gone")
    session.playlists.add(gone.id, tmp_path / "not-in-library.mp4")
    session.playlists.set_rotation(gone.id, cycle_interval=60)
    target = tmp_path / "runtime.toml"

    published = runtime_config.publish_changes(settings, session, target)

    assert published and published.left_out == ()
    document, overrides = _documents(target)
    assert target.read_text(encoding="utf-8") == expected
    assert gone.id not in {playlist["id"] for playlist in document["playlists"]}
    assert overrides is None, "no emitted playlist has a rotation of its own"
    _names_only_what_is_emitted(document, overrides)
    session.shutdown()


def test_everything_that_cannot_play_is_left_out_at_once_and_the_rest_is_published(
    tmp_path: Path,
) -> None:
    settings, session = _session(tmp_path)
    settings = _independent(settings)
    draft = session.playlists.create("Draft")
    session.schedules.add("ghost", rule_id="one")
    session.schedules.add(draft.id, rule_id="two", connector="DP-1")
    session.schedules.add("evening", rule_id="three", enabled=False)
    session.displays.assign("HDMI-A-1", "ghost")
    session.displays.assign("eDP-1", draft.id)
    settings = replace(settings, active_playlist="nowhere")
    target = tmp_path / "runtime.toml"

    published = runtime_config.publish_changes(settings, session, target)

    assert set(published.left_out) == {
        LeftOut(DEFAULT_OWNER, "", "nowhere", "", DOES_NOT_EXIST),
        LeftOut(SCHEDULE_OWNER, "one", "ghost", "", DOES_NOT_EXIST),
        LeftOut(SCHEDULE_OWNER, "two", draft.id, "Draft", NOTHING_TO_PLAY),
        LeftOut(DISPLAY_OWNER, "HDMI-A-1", "ghost", "", DOES_NOT_EXIST),
        LeftOut(DISPLAY_OWNER, "eDP-1", draft.id, "Draft", NOTHING_TO_PLAY),
    }
    document, overrides = _documents(target)
    assert document["default_playlist"] == ALL_MEDIA
    assert [rule["id"] for rule in document["schedules"]] == ["night", "three"]
    assert document["displays"] == [{"connector": "DP-1", "playlist": "evening"}]
    assert {playlist["id"] for playlist in document["playlists"]} == {ALL_MEDIA, "evening"}
    _names_only_what_is_emitted(document, overrides)

    # Nothing changed on disk the second time, and it still says what is left out.
    again = runtime_config.publish_changes(settings, session, target)
    assert not again and set(again.left_out) == set(published.left_out)
    assert runtime_config.update(settings, session, target) is False
    session.shutdown()


def test_what_mirrored_mode_keeps_dormant_is_not_left_out(tmp_path: Path) -> None:
    """A display's own playlist and a rule aimed at one display are not compiled while
    mirrored, so there is nothing to say about them."""
    settings, session = _session(tmp_path)
    session.displays.assign("HDMI-A-1", "ghost")
    session.schedules.add("ghost", rule_id="aimed", connector="HDMI-A-1")

    built = runtime_config.compiled(settings, session)

    assert built.left_out == ()
    assert "ghost" not in built.document
    session.shutdown()


def test_all_media_is_never_left_out(tmp_path: Path) -> None:
    """By id or by name, wherever it is named; and with an empty library too."""
    settings, session = _session(tmp_path)
    settings = replace(_independent(settings), active_playlist="All media")
    session.schedules.add(ALL_MEDIA, rule_id="everything")
    session.displays.assign("HDMI-A-1", "All media")

    built = runtime_config.compiled(settings, session)

    assert built.left_out == ()
    document = tomllib.loads(built.document)
    assert document["default_playlist"] == ALL_MEDIA
    assert {"connector": "HDMI-A-1", "playlist": ALL_MEDIA} in document["displays"]
    assert [rule["playlist"] for rule in document["schedules"]] == ["evening", ALL_MEDIA]
    session.shutdown()


def test_the_notice_says_each_reference_once_in_a_fixed_order() -> None:
    left_out = (
        LeftOut(DISPLAY_OWNER, "HDMI-A-1", "ghost", "", DOES_NOT_EXIST),
        LeftOut(SCHEDULE_OWNER, "two", "draft", "Draft", NOTHING_TO_PLAY),
        LeftOut(SCHEDULE_OWNER, "one", "draft", "Draft", NOTHING_TO_PLAY),
        LeftOut(DEFAULT_OWNER, "", "Dusk", "", DOES_NOT_EXIST),
        LeftOut(DISPLAY_OWNER, "eDP-1", "draft", "Draft", NOTHING_TO_PLAY),
        LeftOut(SCHEDULE_OWNER, "three", "ghost", "", DOES_NOT_EXIST),
    )

    assert runtime_config.left_out_notice(left_out) == (
        "The default names a playlist that doesn\u2019t exist (Dusk), so \u201cAll media\u201d "
        "plays instead. "
        "2 schedule rules name \u201cDraft\u201d, which has nothing to play, so those rules "
        "are left out. "
        "A schedule rule names a playlist that doesn\u2019t exist (ghost), so that rule is "
        "left out. "
        "HDMI-A-1\u2019s own playlist doesn\u2019t exist (ghost), so that display follows the "
        "schedule and the default. "
        "eDP-1\u2019s own playlist, \u201cDraft\u201d, has nothing to play, so that display "
        "follows the schedule and the default."
    )
    assert runtime_config.left_out_notice(reversed(left_out)) == runtime_config.left_out_notice(
        left_out
    )
    assert runtime_config.left_out_notice(()) == ""


def _damaged(path: Path) -> None:
    path.write_text("{ this is not JSON", encoding="utf-8")


def _unreadable(path: Path) -> None:
    path.chmod(0o000)


def _newer(path: Path) -> None:
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["version"] = 99
    path.write_text(json.dumps(saved), encoding="utf-8")


@pytest.mark.parametrize(
    ("state_path", "store_name"),
    (
        (playlists.state_path, "playlists"),
        (schedules.state_path, "schedules"),
        (displays.state_path, "display assignments"),
        (pairings.state_path, "pairings"),
    ),
)
@pytest.mark.parametrize("fault", (_damaged, _unreadable, _newer), ids=lambda made: made.__name__)
def test_a_store_that_was_not_read_in_full_still_refuses_the_build(
    tmp_path: Path,
    state_path: Callable[[], Path],
    store_name: str,
    fault: Callable[[Path], None],
) -> None:
    """ "Couldn't look" is never "nothing there".

    Every reference here would be left out if the stores were what they seem
    to be once they have recovered to empty. Instead the build is refused,
    naming the file, nothing is left out, and the last document stays.
    """
    if fault is _unreadable and os.geteuid() == 0:
        pytest.skip("root reads a file with no permission bits")
    library = tmp_path / "library"
    library.mkdir()
    (library / "paper.png").write_bytes(b"fixture")
    settings = replace(
        _independent(config.Settings(roots=(library,), scan_workshop=False)),
        active_playlist="ghost",
    )
    first = Session(settings)
    first.refresh()
    kept = first.playlists.create("Kept")
    first.playlists.add(kept.id, library / "paper.png")
    first.schedules.add("ghost", rule_id="dangling")
    first.displays.assign("HDMI-A-1", "ghost")
    first.pairings.choose_palette(
        first.library.items[0],
        pairings.PalettePolicy(kind="community", name="Catppuccin", mode=pairings.Mode.DARK),
    )
    target = tmp_path / "runtime.toml"
    good = runtime_config.publish_changes(settings, first, target)
    first.shutdown()
    assert len(good.left_out) == 3, "read in full, the three dangling references are left out"
    before = target.read_bytes()

    fault(state_path())
    session = Session(settings)
    try:
        session.refresh(mutate_removals=False)
        with pytest.raises(runtime_config.RuntimeConfigError) as refused:
            runtime_config.publish_changes(settings, session, target)
        with pytest.raises(runtime_config.RuntimeConfigError):
            runtime_config.compiled(settings, session)
    finally:
        session.shutdown()
        state_path().chmod(0o600)

    message = str(refused.value)
    assert store_name in message and state_path().name in message
    assert "the existing runtime configuration was left untouched" in message
    assert target.read_bytes() == before


def test_the_rust_service_loads_a_document_built_without_what_cannot_play(
    tmp_path: Path,
) -> None:
    """Cross-binary: the strict loader, which rejects any reference to an unknown playlist."""
    configured = os.environ.get("WALL_IN_ONE_SERVICE_BINARY", "")
    if not configured:
        pytest.skip("set WALL_IN_ONE_SERVICE_BINARY to check the compiled files in Rust")
    settings, session = _session(tmp_path)
    settings = replace(_independent(settings), active_playlist="nowhere")
    draft = session.playlists.create("Draft")
    session.playlists.set_rotation(draft.id, cycle_interval=60)
    session.schedules.add("ghost", rule_id="one")
    session.schedules.add(draft.id, rule_id="two")
    session.displays.assign("HDMI-A-1", "ghost")
    session.displays.set_beats_global_rules("HDMI-A-1", True)
    session.displays.set_beats_global_rules("DP-1", True)
    folder = tmp_path / "compiled"
    folder.mkdir()
    target = folder / "runtime.toml"
    published = runtime_config.publish_changes(settings, session, target)
    session.shutdown()
    assert len(published.left_out) == 4

    checked = subprocess.run(
        [configured, "--config", str(target), "--check-config"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert checked.returncode == 0, checked.stderr
    assert "unknown playlist" not in checked.stderr
    assert "not applied" not in checked.stderr, "no override names what the document lacks"
