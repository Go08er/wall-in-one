"""A ``settings.toml`` that can't be saved: all or nothing, and a start that still opens.

An ordinary setup: the configuration folder is kept read-only (its permission
bits, a read-only mount, a declarative setup that owns it). Two things then
went wrong.

A change that spans files and saves the settings last was half done. Deleting
the playlist the saved default names took it out of ``playlists.json``, with
its rules and its displays, and then could not clear ``active_playlist``:
``settings.toml`` named a playlist that was gone, and every publication was
refused. Such a change now looks first whether that save can be made
(`config.why_not_writable`) and is refused whole when it can't.

And a start that found a default naming nothing could not clear it either,
rested its gate at "repair", and never loaded the library. It now goes on.
Since r6 every compile builds a default that names nothing as "All media"
and says so (`runtime_config.LeftOut`), whether or not the file can be
changed: the start has nothing to tell anybody, and says nothing itself.

Real permission bits throughout; each test restores them. Those that say so
fail on d38cb66.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(os.geteuid() == 0, reason="root writes to a read-only folder"),
]

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_runtime_health_sync import _snapshot as _failed_wallpaper  # noqa: E402
from tests.test_ui_astra030_ac_saved import _reply, _verb  # noqa: E402
from tests.test_ui_codex030_round1b import (  # noqa: E402, F401
    Profile,
    _lane_idle,
    profiles,
)
from tests.test_ui_codex030_round1b import _playlist as _saved  # noqa: E402
from tests.test_ui_codex030_round1c import (  # noqa: E402, F401
    Shown,
    _playlist,
    shown,
    toolkit,
)
from tests.whole_document import builds_whole  # noqa: E402
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.library import displays, playlists, schedules, state_file  # noqa: E402
from wall_in_one.session import Session  # noqa: E402
from wall_in_one.ui import playlists_page, schedules_page  # noqa: E402
from wall_in_one.ui.app import _RuntimeHealthRequest  # noqa: E402
from wall_in_one.ui.next import schedule_editor  # noqa: E402
from wall_in_one.ui.next.real_schedule import RealScheduling  # noqa: E402


@contextlib.contextmanager
def _settings_can_not_be_saved() -> Iterator[Path]:
    """The folder ``settings.toml`` is in, read-only for as long as this lasts."""
    folder = paths.settings_path().parent
    folder.chmod(0o500)
    try:
        yield folder
    finally:
        folder.chmod(0o700)


def _everything_saved() -> dict[Path, bytes | None]:
    files = (
        paths.settings_path(),
        playlists.state_path(),
        schedules.state_path(),
        displays.state_path(),
    )
    return {path: path.read_bytes() if path.exists() else None for path in files}


#: What a publication says of a default that names no playlist (`runtime_config.LeftOut`).
NAMES_NOTHING = (
    "The default names a playlist that doesn\u2019t exist ({}), so \u201cAll media\u201d "
    "plays instead."
)


def _pointed(reference: str) -> str:
    """The sentence for a default in a ``settings.toml`` that can't be changed.

    It ends with where to fix it: the file and the key (the reason before that
    depends on how the file is kept, so a test matches the two ends).
    """
    return NAMES_NOTHING.format(reference)


def _is_said_with_its_pointer(text: str, reference: str) -> bool:
    return text.startswith(
        _pointed(reference) + " The settings file can\u2019t be changed: "
    ) and text.endswith(f"To change the default, edit active_playlist in {paths.settings_path()}.")


def _builds(application: Any) -> bool:
    """Whether the runtime document is built whole: nothing left out, no playlist dropped.

    What "it builds" meant while one playlist that can't play refused the
    whole compile (`tests.whole_document`).
    """
    return builds_whole(config.load_strict(), application.session)


# -- all or nothing -------------------------------------------------------------------------


@pytest.mark.parametrize("via", ["ctl", "service", "a window"])
def test_deleting_the_default_changes_nothing_when_the_settings_can_not_be_saved(
    profiles: Any,  # noqa: F811
    via: str,
) -> None:
    """Both windows delete through `delete_playlist_async`; ``ctl`` on the lane
    and in the headless service through the same task."""
    profile: Profile = profiles(service=via == "service")
    application = profile.application
    lone = _saved(profile, "Lone", [profile.dawn], used=True)
    application.session.displays.assign("DP-1", lone.id)
    application.update_settings(active_playlist=lone.id)
    held = _everything_saved()

    with _settings_can_not_be_saved() as folder:
        if via == "a window":
            said: list[str] = []
            application.delete_playlist_async(
                lone.id, lambda _result, _failures: said.append("deleted"), on_error=said.append
            )
            spin_until(lambda: bool(said) and _lane_idle(application), 30, what="the delete")
            message = said[-1]
        else:
            reply = _reply(profile, "playlist-delete", lone.id)
            assert not reply.ok and reply.kind == "local-io", reply
            message = reply.message

    # Failed on d38cb66: the playlist, its rule and its display's playlist were
    # gone, and settings.toml still named it as the default.
    assert _everything_saved() == held
    assert message == (
        "local-io: this playlist is the saved default, which deleting it would clear, and "
        f"settings.toml can't be changed (a file can't be made in {folder}: Permission denied). "
        "Nothing was deleted."
    )
    assert _builds(application)


def test_a_playlist_the_default_does_not_name_is_deleted_all_the_same(
    profiles: Any,  # noqa: F811
) -> None:
    """Nothing in the settings would change, however read-only they are."""
    profile: Profile = profiles()
    application = profile.application
    lone = _saved(profile, "Lone", [profile.dawn])
    other = _saved(profile, "Other", [profile.dusk])
    application.update_settings(active_playlist=lone.id)

    with _settings_can_not_be_saved():
        reply = _reply(profile, "playlist-delete", other.id)

    assert reply.ok, reply
    assert playlists.Store.open().get(other.id) is None
    assert config.load_strict().active_playlist == lone.id
    assert _builds(application)


def test_forget_display_forgets_nothing_when_its_colors_step_could_not_be_saved(
    shown: Shown,  # noqa: F811
) -> None:
    """Forget Display takes the display's rules and playlist, then stops the
    desktop colors following it: a settings save, last."""
    opened = shown.opened
    application = opened.application
    evening = _playlist(shown, "Evening", [shown.dawn])
    application.session.displays.assign("DP-7", evening.id)
    application.update_settings(theme_source_connector="DP-7")
    assert application.settings.display_mode == config.DISPLAY_MODE_MIRRORED
    scheduling = opened.window.state.scheduling
    assert isinstance(scheduling, RealScheduling)
    held = _everything_saved()
    settled: list[bool] = []

    with _settings_can_not_be_saved():
        scheduling.forget_display("DP-7", settled.append)
        opened.wait("Forget to settle", lambda: bool(settled))

    # Failed on d38cb66: the display's playlist was forgotten, and the colors
    # were left following a display nothing knew.
    assert settled == [False]
    assert _everything_saved() == held
    assert opened.reports[-1].startswith(
        "Forgetting DP-7 was not saved; nothing changed: local-io: "
    )
    assert "settings.toml can't be changed" in opened.reports[-1]


# -- a refusal says once that nothing changed, on every surface ------------------------------


def _told(text: str) -> int:
    """How often ``text`` says that nothing changed."""
    sentences = ("nothing changed", config.NOTHING_DELETED, config.NOTHING_RENAMED)
    return sum(text.count(sentence) for sentence in sentences)


@contextlib.contextmanager
def _settings_that(cause: str) -> Iterator[tuple[str, str]]:
    """``settings.toml`` as each refusal finds it: (the reply's kind, words of its reason)."""
    target = paths.settings_path()
    if cause == "can't be saved":
        with _settings_can_not_be_saved() as folder:
            yield "local-io", f"a file can't be made in {folder}: Permission denied"
    elif cause == "can't be read":
        target.chmod(0o000)
        try:
            yield "unreadable", "Permission denied"
        finally:
            target.chmod(0o600)
    else:
        held = target.read_bytes()
        target.write_bytes(held + b"future_setting = 1\n")
        try:
            yield "read-only", "doesn't recognize (future_setting)"
        finally:
            target.write_bytes(held)


@pytest.mark.parametrize("cause", ["can't be saved", "can't be read", "an unknown key"])
@pytest.mark.parametrize("change", ["delete", "rename"])
def test_a_refused_delete_or_rename_says_once_that_nothing_changed(
    shown: Shown,  # noqa: F811
    cause: str,
    change: str,
) -> None:
    """Deleting the default playlist, or renaming one the default names by name,
    is refused before anything is written when ``settings.toml`` can't be saved,
    can't be read, or holds a key this build doesn't know. Whoever reads the
    refusal reads once that nothing changed: over ``ctl`` and in the classic
    rename, which pass it on as it is, and in each window's own report, which
    used to add "nothing changed" to a refusal that already said so. None of
    them shows a class name, and over ``ctl`` each has a kind."""
    opened = shown.opened
    application = opened.application
    evening = _playlist(shown, "Evening", [shown.dawn])
    config.update({"active_playlist": "evening"})  # by name: a rename would have to change it
    application._settings = config.load_strict()
    application.session.update_settings(application._settings, rescan_library=False)
    editing = opened.window.state.playlist_editing
    assert editing is not None
    sentence = config.NOTHING_DELETED if change == "delete" else config.NOTHING_RENAMED
    passed_on: list[str] = []

    def unprepared() -> Any:  # pragma: no cover - prepare always replaces it
        raise RuntimeError("not prepared")

    with _settings_that(cause) as (kind, reason):
        held = _everything_saved() if cause != "can't be read" else None
        if change == "delete":
            # ctl, and (the same text) what the lane hands both windows.
            reply = _verb(shown, "playlist-delete", evening.id)
            assert not reply.ok and reply.kind == kind, reply
            passed_on.append(reply.message)
            editing.delete_playlist(evening.id)
        else:
            # The classic window's rename passes the lane's failure on as it is.
            application.authoring_action_async(
                unprepared,
                lambda _result: passed_on.append("renamed"),
                prepare=lambda: application.prepare_playlist_rename(evening.id, "Dusk"),
                failure=passed_on.append,
            )
            opened.wait("the classic rename", lambda: bool(passed_on))
            editing.rename_playlist(evening.id, "Dusk")
        opened.wait(f"the new window's {change}", lambda: bool(opened.reports))
        if held is not None:
            assert _everything_saved() == held, "refused whole"

    (raw,) = passed_on
    shown_in_the_window = opened.reports[-1]
    # Failed on 7e55112 in ten of these twelve cells: 0 or 2, and a class name
    # with no kind for the unknown key.
    assert raw.startswith(f"{kind}: ") and raw.endswith(sentence), raw
    assert reason in raw and _told(raw) == 1, raw
    assert shown_in_the_window == f"\u201cEvening\u201d was not {change}d: {raw}"
    assert _told(shown_in_the_window) == 1
    assert "Error" not in raw, raw
    # The classic window's delete says it the same way.
    assert _told(config.unchanged_report("Evening was not deleted", raw)) == 1
    # And a failure that doesn't say it is still told so by the window.
    assert config.unchanged_report("X was not deleted", "local-io: could not write") == (
        "X was not deleted; nothing changed: local-io: could not write"
    )
    assert playlists.Store.open().get(evening.id) is not None
    assert playlists.Store.open().get(evening.id).name == "Evening"  # type: ignore[union-attr]


# -- a start that still opens ---------------------------------------------------------------


@pytest.mark.parametrize("default", ["names nothing", "by name"])
def test_a_start_goes_on_past_a_default_it_can_not_save(
    profiles: Any,  # noqa: F811
    default: str,
) -> None:
    """``settings.toml`` names a default the start would repair: one that names
    nothing (cleared), or one by name (given the id). It can't be saved. Neither
    is a reason to hold the library back: the first is left out by every
    compile, and said by the publication; the second names its playlist as it is."""
    profile: Profile = profiles()
    application = profile.application
    _saved(profile, "Lone", [profile.dawn])
    reference = "ghost" if default == "names nothing" else "lone"
    config.update({"active_playlist": reference})
    application._settings = config.load_strict()
    application.session.update_settings(application._settings, rescan_library=False)
    saved = paths.settings_path().read_bytes()

    with _settings_can_not_be_saved():
        partial = application._repair_dangling_playlist_references()
        response = application._finish_playlist_reference_repair(partial)

        # Failed on d38cb66: "authoring-repair-required", and writes stayed paused.
        assert response.ok, response
        assert bool(application.authoring_ready)
        assert paths.settings_path().read_bytes() == saved
        assert profile.window.reports == [], "the repair itself says nothing"
        # What is published is built from the settings as they are saved.
        _published(application)
        built = paths.runtime_config_path().read_text(encoding="utf-8")
        if default == "names nothing":
            assert f'default_playlist = "{runtime_config.FALLBACK_PLAYLIST_ID}"' in built

    about_it = [text for text in profile.window.reports if "The default names" in text]
    if default == "by name":
        assert about_it == [] and application.left_out_notice == ""
    else:
        assert len(about_it) == 1, "said by the publication, once"
        assert _is_said_with_its_pointer(about_it[0], "ghost"), about_it
        assert application.left_out_notice == about_it[0]
        # And once a playlist answers to it, it is the default again.
        made = _saved(profile, "ghost", [profile.dusk])
        _published(application)
        again = paths.runtime_config_path().read_text(encoding="utf-8")
        assert f'default_playlist = "{made.id}"' in again
        assert application.left_out_notice == ""


def _combo(widget: Any) -> Any:
    """The first combo row under ``widget``."""
    if isinstance(widget, Adw.ComboRow):
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = _combo(child)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


@pytest.mark.parametrize("default", ["by name", "names nothing"])
def test_a_default_the_start_could_not_save_is_shown_as_what_is_in_force(
    shown: Shown,  # noqa: F811
    default: str,
) -> None:
    """A default the start would have repaired, in settings it can't save, stays
    as it is saved for the whole session. One by name is still its playlist: every
    page shows that playlist as the default, not "All media" and not a row with
    no name. One that names nothing is left out, so what is in force, and shown,
    is "All media"."""
    opened = shown.opened
    application = opened.application
    session = application.session
    evening = _playlist(shown, "Evening", [shown.dawn])
    reference = "evening" if default == "by name" else "ghost"
    config.update({"active_playlist": reference})
    application._settings = config.load_strict()
    session.update_settings(application._settings, rescan_library=False)
    with _settings_can_not_be_saved():
        partial = application._repair_dangling_playlist_references()
        assert application._finish_playlist_reference_repair(partial).ok
    assert application.settings.active_playlist == reference, "nothing could be saved"

    scheduling = opened.window.state.scheduling
    editing = opened.window.state.playlist_editing
    assert isinstance(scheduling, RealScheduling) and editing is not None
    classic = schedules_page.SchedulesPage(application)
    chosen = _combo(classic._build_defaults(session)).get_selected_item().get_string()
    if default == "by name":
        # Each of these failed on 7e55112: the pages compared the playlist's id
        # with the saved name. The new window's default was "evening", which no
        # row could name, and classic showed "All media".
        assert scheduling.default_playlist == evening.id
        assert scheduling.playlist_label(scheduling.default_playlist) == "Evening"
        assert "the saved default selection" in editing.delete_warning(evening.id)
        assert chosen == "Evening"
        assert "the saved default selection" in playlists_page._deletion_body(session, evening)
        assert runtime_config.default_playlist_id(reference, session) == evening.id
        # And what is in force is that playlist, for the rotation, a page's
        # "playing" mark and `ctl playlists` (failed on edbd327: "evening").
        assert session.active_playlist() == evening.id
        # The session's own rotation is that playlist's wallpapers, as before:
        # `playlists.rotation` always looked by id and then by name, so only
        # the id `active_playlist()` reports changed, not what it selects.
        assert [item.path for item in session._rotation()] == [shown.dawn.path]
        # Resuming the schedule stores the same spelling of what is in force,
        # so the next tick sees no change (failed on e9fa88d: it stored the
        # saved name, the tick compared it with the id, and the wallpaper was
        # applied again from the top of the playlist).
        session.use_playlist(evening.id)
        session.resume_schedule()
        assert session.schedule_changed() is False
        listed = _verb(shown, "playlists", "").message.splitlines()
        assert [row.split("\t")[-1] for row in listed if row.startswith(evening.id)] == ["yes"]
    else:
        (left_out,) = runtime_config.compiled(application.settings, session).left_out
        assert (left_out.owner, left_out.reference) == (runtime_config.DEFAULT_OWNER, reference)
        # The new window's row keeps the saved reference and shows it as what
        # it is, so choosing "All media" there is a change the page can save.
        # Classic's row has no entry for it and shows "All media".
        assert scheduling.default_playlist == reference
        named = schedule_editor.playlist_name(opened.window.state, reference)
        assert named == schedule_editor.MISSING == "Missing playlist"
        assert "the saved default selection" not in editing.delete_warning(evening.id)
        assert chosen == "All media (built-in playlist)"
        assert "the saved default selection" not in playlists_page._deletion_body(session, evening)
        assert runtime_config.default_playlist_id(reference, session) == ""
        # A default can stop naming a playlist under a running app too: the
        # row shows that one the same way.
        application._settings = replace(application._settings, active_playlist="phantom")
        assert scheduling.default_playlist == "phantom"
        named = schedule_editor.playlist_name(opened.window.state, "phantom")
        assert named == schedule_editor.MISSING


def test_a_left_out_default_is_said_once(
    profiles: Any,  # noqa: F811
) -> None:
    """The first publication says the default was left out. Later passes of the
    repair in the same process (Try again, after something else failed) and later
    publications find the same thing and do not say it again; one does, when what
    it has to say is different."""
    profile: Profile = profiles()
    application = _a_start_over(profile, "ghost")

    def said() -> list[str]:
        return [text for text in profile.window.reports if "The default names" in text]

    with _settings_can_not_be_saved():
        for _pass in range(2):
            partial = application._repair_dangling_playlist_references()
            assert application._finish_playlist_reference_repair(partial).ok
            _published(application)
        # Failed on 7e55112: said twice.
        assert len(said()) == 1 and _is_said_with_its_pointer(said()[0], "ghost"), said()

    # Another reference that names nothing is another thing to say.
    paths.settings_path().write_text(
        paths.settings_path().read_text(encoding="utf-8").replace("ghost", "phantom"),
        encoding="utf-8",
    )
    application._settings = config.load_strict()
    application.session.update_settings(application._settings, rescan_library=False)
    with _settings_can_not_be_saved():
        partial = application._repair_dangling_playlist_references()
        assert application._finish_playlist_reference_repair(partial).ok
        _published(application)
    assert len(said()) == 2, said()
    assert all(
        _is_said_with_its_pointer(text, name)
        for text, name in zip(said(), ("ghost", "phantom"), strict=True)
    ), said()


# -- which refusals count: only a file that can't be changed for as long as it stays as it is ----


def _a_start_over(profile: Profile, reference: str) -> Any:
    """The profile with ``reference`` as its saved default, as a start finds it."""
    application = profile.application
    _saved(profile, "Lone", [profile.dawn])
    config.update({"active_playlist": reference})
    application._settings = config.load_strict()
    application.session.update_settings(application._settings, rescan_library=False)
    application._authoring_migration_ready = False
    return application


def test_a_default_whose_save_the_system_refuses_does_not_hold_the_start(
    profiles: Any,  # noqa: F811
) -> None:
    """The look says the folder takes a new file; the save itself is turned away.

    Here by the settings' own lock file, kept read-only (EACCES): what an
    immutable ``settings.toml`` does too, with EPERM. That is "can't be
    saved where it is" as much as a read-only folder is. The start goes on,
    and the publication after it leaves the default out and says so, with
    nobody having to be told which file can't be changed.
    """
    profile: Profile = profiles()
    application = _a_start_over(profile, "ghost")
    saved = paths.settings_path().read_bytes()
    lock = paths.settings_path().with_name(".settings.toml.mutation.lock")
    lock.chmod(0o400)
    try:
        assert config.why_not_writable() == "", "the folder itself takes a file"
        result = application._repair_dangling_playlist_references()
        response = application._finish_playlist_reference_repair(result)
        # The app's own lanes need no telling: every compile leaves it out.
        _published(application)
        assert application.publication_problem == ""
    finally:
        lock.chmod(0o600)

    assert result.failures == ()
    assert response.ok and application.authoring_ready
    assert application.left_out_notice == NAMES_NOTHING.format("ghost")
    assert profile.window.reports == [NAMES_NOTHING.format("ghost")]
    document = paths.runtime_config_path().read_text(encoding="utf-8")
    assert f'default_playlist = "{runtime_config.FALLBACK_PLAYLIST_ID}"' in document
    assert paths.settings_path().read_bytes() == saved


def _held_too_long(*_arguments: object, **_keywords: object) -> config.Settings | None:
    """What `config._rewrite_playlist_default` raises when another program keeps the lock."""
    try:
        raise TimeoutError("timed out after 5s waiting for the settings mutation lock")
    except TimeoutError as error:
        raise config.ConfigError(
            f"cannot safely update {paths.settings_path()}: {error}"
        ) from error


def _rewritten_meanwhile(real: Callable[..., config.Settings | None]) -> Any:
    """The real save, after another program has put bytes that don't parse in the file."""

    def save(*arguments: Any, **keywords: Any) -> config.Settings | None:
        paths.settings_path().write_bytes(b"active_playlist = \n")
        return real(*arguments, **keywords)

    return save


@pytest.mark.parametrize("default", ["ghost", "lone"], ids=["names nothing", "by name"])
@pytest.mark.parametrize("refusal", ["the lock is held too long", "the file stopped parsing"])
def test_a_default_whose_save_only_failed_this_time_still_holds_the_start(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
    default: str,
) -> None:
    """Neither is "can't be saved for good", so the start is not let past either.

    The folder is writable and the file holds only keys this build knows.
    The gate rests at "repair", as it always did; Try again runs the repair
    once more and has the file put right. A compile would leave the default
    that names nothing out in the meantime, like any other.
    """
    profile: Profile = profiles()
    application = _a_start_over(profile, default)
    for name in ("forget_playlist_default", "repoint_playlist_default"):
        real = getattr(config, name)
        failing = _held_too_long if refusal.startswith("the lock") else _rewritten_meanwhile(real)
        monkeypatch.setattr(config, name, failing)

    result = application._repair_dangling_playlist_references()
    response = application._finish_playlist_reference_repair(result)

    (failure,) = result.failures
    assert failure.startswith("saved default: ")
    assert ("timed out" in failure) is refusal.startswith("the lock"), failure
    assert not response.ok and response.kind == "authoring-repair-required", response
    assert not application.authoring_ready
    assert application._authoring_gate_state == "repair"
    assert profile.window.reports == [], "the failure is the lane's reply, and nothing else is said"


# -- "names no playlist" rests on playlists that were read in full -------------------------------


@pytest.mark.parametrize("playlists_file", ["from a newer build", "can't be read"])
def test_a_default_is_not_left_out_over_playlists_that_were_not_read(
    profiles: Any,  # noqa: F811
    playlists_file: str,
) -> None:
    """Settings that can't be changed and a default no playlist that was read answers to.

    "Couldn't look" is not "no such playlist": the default is not left out,
    nothing is said about leaving it out, and the compiler goes on refusing
    the playlists file, naming it, so the last good ``runtime.toml`` stays.
    A ``playlists.json`` a newer build saved is exactly this.
    """
    profile: Profile = profiles()
    application = _a_start_over(profile, "ghost")
    _published(application)
    good = paths.runtime_config_path().read_bytes()
    assert application.left_out_notice == NAMES_NOTHING.format("ghost"), "read in full: left out"
    said_before = list(profile.window.reports)
    target = playlists.state_path()
    if playlists_file == "from a newer build":
        newer = {"version": playlists.FORMAT_VERSION + 1, "lists": [{"id": "x", "name": "X"}]}
        target.write_text(json.dumps(newer), encoding="utf-8")
    else:
        target.chmod(0o000)
    try:
        with _settings_can_not_be_saved():
            result = application._repair_dangling_playlist_references()
            response = application._finish_playlist_reference_repair(result)
            # The publication lane reads the stores afresh, and refuses this one.
            _published(application)
        listing = playlists.Store.open()
        as_saved = Session(config.load_strict())
        try:
            with pytest.raises(runtime_config.RuntimeConfigError, match="playlists") as refused:
                runtime_config.compiled(config.load_strict(), as_saved)
        finally:
            as_saved.shutdown()
    finally:
        target.chmod(0o600)

    assert "playlists.json" in str(refused.value)
    assert "playlists" in application.publication_problem, "refused, naming the file"
    assert "left untouched" in application.publication_problem
    assert paths.runtime_config_path().read_bytes() == good, "the last good document stays"
    assert [text for text in profile.window.reports if "The default names" in text] == [
        NAMES_NOTHING.format("ghost")
    ], "nothing more was left out, or said to be"
    assert profile.window.reports[len(said_before) :] == [application.publication_problem]
    if playlists_file == "from a newer build":
        # Read-only here, not a failure: the gate opens and the compiler refuses the file.
        assert listing.fault_kind == state_file.NEWER_VERSION
        assert response.ok and result.failures == ()
    else:
        (failure,) = result.failures
        assert failure.startswith("playlists: ") and "playlists.json" in failure
        assert not response.ok and response.kind == "authoring-repair-required"


# -- the app's own compiles follow the rule too --------------------------------------------------


def _published(application: Any) -> None:
    assert application._publish_runtime_async()
    spin_until(
        lambda: not application._runtime_compile_pending and not application._authoring_active,
        30,
        what="the publication lane",
    )


def test_the_apps_own_lanes_build_without_a_default_that_names_nothing(
    profiles: Any,  # noqa: F811
) -> None:
    """The start said "playback goes on without a default", and it didn't.

    The publication lane and the health lane each compile with a `Session`
    of their own, made on their worker. Every change's publication was
    refused ("default playlist refers to unknown playlist 'ghost'; repair it
    in the app"), no ``runtime.toml`` was written, and the health lane saved
    nothing. Both now leave the default out, as every compile does, and it
    is said once between them.
    """
    profile: Profile = profiles()
    application = _a_start_over(profile, "ghost")
    saved = paths.settings_path().read_bytes()

    with _settings_can_not_be_saved():
        result = application._repair_dangling_playlist_references()
        assert application._finish_playlist_reference_repair(result).ok
        _published(application)

        assert application.publication_problem == ""
        document = paths.runtime_config_path().read_text(encoding="utf-8")
        assert f'default_playlist = "{runtime_config.FALLBACK_PLAYLIST_ID}"' in document

        # The health lane: the runtime couldn't play dawn, and says so.
        request = _RuntimeHealthRequest(
            application._runtime_authoring_generation,
            application.session.library,
            _failed_wallpaper(profile.dawn).message,
        )
        health = application._ingest_runtime_health(request)
        assert health.accepted and health.changed == 1, health
        assert health.publication is None or not health.publication.error, health.publication
        application._apply_runtime_health_result(health)
        assert paths.settings_path().read_bytes() == saved

    told = [text for text in profile.window.reports if "The default names" in text]
    assert len(told) == 1, "by the publication; not again by the poll"
    assert _is_said_with_its_pointer(told[0], "ghost"), told
    assert "renderer rejected this wallpaper" in paths.runtime_config_path().read_text("utf-8")


def test_the_apps_own_lanes_leave_a_default_out_where_the_settings_can_be_changed_too(
    profiles: Any,  # noqa: F811
) -> None:
    """A default that went dangling under a running app, settings writable.

    This stayed strict until r6: the publication was refused ("default
    playlist refers to unknown playlist 'ghost'"), the last document was
    kept, and every later change waited for the next start's repair. It is
    now left out like any other, and said; the next start's repair still
    clears it, and then there is nothing left to say.
    """
    profile: Profile = profiles()
    application = profile.application
    lone = _saved(profile, "Lone", [profile.dawn])
    application.update_settings(active_playlist=lone.id)
    _published(application)
    good = paths.runtime_config_path().read_text(encoding="utf-8")
    assert f'default_playlist = "{lone.id}"' in good and application.left_out_notice == ""
    config.update({"active_playlist": "ghost"})  # another program's edit

    _published(application)

    assert application.publication_problem == ""
    document = paths.runtime_config_path().read_text(encoding="utf-8")
    assert f'default_playlist = "{runtime_config.FALLBACK_PLAYLIST_ID}"' in document
    assert document.count("[[playlists]]") == good.count("[[playlists]]"), "the rest is there"
    assert application.left_out_notice == NAMES_NOTHING.format("ghost")
    assert profile.window.reports == [NAMES_NOTHING.format("ghost")]

    # The next start: the settings can be saved, so the default is cleared.
    application._settings = config.load_strict()
    application.session.update_settings(application._settings, rescan_library=False)
    partial = application._repair_dangling_playlist_references()
    assert application._finish_playlist_reference_repair(partial).ok
    assert config.load_strict().active_playlist == ""
    _published(application)
    assert application.left_out_notice == ""
    assert profile.window.reports == [NAMES_NOTHING.format("ghost")], "and nothing new is said"


@pytest.mark.parametrize("reference", ["All media", "all-media"])
def test_a_built_in_default_in_unsavable_settings_is_shown_as_all_wallpapers(
    shown: Shown,  # noqa: F811
    reference: str,
) -> None:
    """A managed ``settings.toml`` keeps the name "All media"; it is not a missing playlist.

    Failed on e0c011b: the new window's default lookup answered "" for it and
    showed "Missing playlist".
    """
    opened = shown.opened
    application = opened.application
    session = application.session
    _playlist(shown, "Evening", [shown.dawn])
    config.update({"active_playlist": reference})
    application._settings = config.load_strict()
    session.update_settings(application._settings, rescan_library=False)
    saved = paths.settings_path().read_bytes()
    with _settings_can_not_be_saved():
        partial = application._repair_dangling_playlist_references()
        assert application._finish_playlist_reference_repair(partial).ok
    assert application.settings.active_playlist == reference, "nothing could be saved"
    assert paths.settings_path().read_bytes() == saved

    scheduling = opened.window.state.scheduling
    assert isinstance(scheduling, RealScheduling)
    classic = schedules_page.SchedulesPage(application)
    chosen = _combo(classic._build_defaults(session)).get_selected_item().get_string()

    assert runtime_config.default_playlist_id(reference, session) == "all-media"
    assert scheduling.default_playlist == runtime_config.FALLBACK_PLAYLIST_ID
    named = schedule_editor.playlist_name(opened.window.state, scheduling.default_playlist)
    assert named != schedule_editor.MISSING
    assert chosen == "All media (built-in playlist)"
