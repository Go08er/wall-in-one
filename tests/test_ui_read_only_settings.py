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
rested its gate at "repair", and never loaded the library. It now goes on
without a default, says so, and names the file to change.

Real permission bits throughout; each test restores them. Those that say so
fail on d38cb66.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable, Iterator
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
from tests.test_ui_astra030_ac_saved import _reply  # noqa: E402
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
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.library import displays, playlists, schedules, state_file  # noqa: E402
from wall_in_one.session import Session  # noqa: E402
from wall_in_one.ui import playlists_page, schedules_page  # noqa: E402
from wall_in_one.ui.app import _RuntimeHealthRequest  # noqa: E402
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


def _builds(application: Any) -> bool:
    try:
        runtime_config.render(config.load_strict(), application.session)
    except runtime_config.RuntimeConfigError:
        return False
    return True


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
        f"settings.toml can't be changed (a file can't be made in {folder}: Permission denied)"
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


# -- a start that still opens ---------------------------------------------------------------


@pytest.mark.parametrize("default", ["names nothing", "by name"])
def test_a_start_goes_on_past_a_default_it_can_not_save(
    profiles: Any,  # noqa: F811
    default: str,
) -> None:
    """``settings.toml`` names a default the start would repair: one that names
    nothing (cleared), or one by name (given the id). It can't be saved. Neither
    is a reason to hold the library back: the first is left out for this
    process, and said; the second names its playlist as it is."""
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
        # What is published is built from the settings as they are saved.
        built = runtime_config.render(config.load_strict(), application.session)
        if default == "names nothing":
            assert f'default_playlist = "{runtime_config.FALLBACK_PLAYLIST_ID}"' in built

    about_it = [text for text in profile.window.reports if "default playlist" in text]
    if default == "by name":
        assert about_it == []
    else:
        (told,) = about_it
        assert told.startswith("The saved default playlist (ghost) doesn\u2019t exist")
        assert f"edit active_playlist in {paths.settings_path()}" in told
        # And once a playlist answers to it, it is the default again.
        made = _saved(profile, "ghost", [profile.dusk])
        again = runtime_config.render(config.load_strict(), application.session)
        assert f'default_playlist = "{made.id}"' in again


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
    else:
        assert session.left_out_default == reference
        # Failed on 7e55112: "ghost", a default row with no name.
        assert scheduling.default_playlist == runtime_config.FALLBACK_PLAYLIST_ID
        assert "the saved default selection" not in editing.delete_warning(evening.id)
        assert chosen == "All media (built-in playlist)"
        assert "the saved default selection" not in playlists_page._deletion_body(session, evening)
        assert runtime_config.default_playlist_id(reference, session) == ""


def test_a_left_out_default_is_said_once(
    profiles: Any,  # noqa: F811
) -> None:
    """The start says it left the default out. A second pass of the repair in the
    same process (Try again, after something else failed) finds the same thing
    and does not say it again; it would, if what it had to say were different."""
    profile: Profile = profiles()
    application = _a_start_over(profile, "ghost")

    def said() -> list[str]:
        return [text for text in profile.window.reports if "default playlist" in text]

    with _settings_can_not_be_saved():
        for _pass in range(2):
            partial = application._repair_dangling_playlist_references()
            assert application._finish_playlist_reference_repair(partial).ok
        # Failed on 7e55112: said twice.
        assert len(said()) == 1, said()

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
    assert len(said()) == 2 and "(phantom)" in said()[-1], said()


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


def test_a_default_whose_save_the_system_refuses_is_left_out(
    profiles: Any,  # noqa: F811
) -> None:
    """The look says the folder takes a new file; the save itself is turned away.

    Here by the settings' own lock file, kept read-only (EACCES): what an
    immutable ``settings.toml`` does too, with EPERM. That is "can't be
    saved where it is" as much as a read-only folder is.
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
        # The app's own lanes can't see this by looking, only by having been
        # told: they build without what the start left out.
        _published(application)
        assert application.publication_problem == ""
    finally:
        lock.chmod(0o600)

    assert result.failures == ()
    assert result.unsaved_default is not None
    reference, why = result.unsaved_default
    assert reference == "ghost" and "Permission denied" in why
    assert response.ok and application.authoring_ready
    assert application.session.left_out_default == "ghost"
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
    """Neither is "can't be saved for good", so neither leaves the default out.

    The folder is writable and the file holds only keys this build knows.
    The gate rests at "repair", as it did before a default could be left out;
    Try again runs the repair once more.
    """
    profile: Profile = profiles()
    application = _a_start_over(profile, default)
    for name in ("forget_playlist_default", "repoint_playlist_default"):
        real = getattr(config, name)
        failing = _held_too_long if refusal.startswith("the lock") else _rewritten_meanwhile(real)
        monkeypatch.setattr(config, name, failing)

    result = application._repair_dangling_playlist_references()
    response = application._finish_playlist_reference_repair(result)

    assert result.unsaved_default is None
    (failure,) = result.failures
    assert failure.startswith("saved default: ")
    assert ("timed out" in failure) is refusal.startswith("the lock"), failure
    assert not response.ok and response.kind == "authoring-repair-required", response
    assert not application.authoring_ready
    assert application._authoring_gate_state == "repair"
    assert application.session.left_out_default == ""
    assert not [text for text in profile.window.reports if "couldn\u2019t be cleared" in text]


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
    A profile taken back to an older build is exactly this: a newer
    ``playlists.json``, and settings with keys this build doesn't know.
    """
    profile: Profile = profiles()
    application = _a_start_over(profile, "ghost")
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
        listing = playlists.Store.open()
        loaded = config.load_strict_document()
        with _settings_can_not_be_saved():
            assert config.why_settings_cannot_change(loaded), "the settings can't be changed"
            assert runtime_config.unclearable_default(listing, loaded) is None
    finally:
        target.chmod(0o600)

    assert result.unsaved_default is None
    assert application.session.left_out_default == ""
    assert not [text for text in profile.window.reports if "couldn\u2019t be cleared" in text]
    if playlists_file == "from a newer build":
        # Read-only here, not a failure: the gate opens and the compiler refuses the file.
        assert listing.fault_kind == state_file.NEWER_VERSION
        assert response.ok and result.failures == ()
        as_saved = Session(config.load_strict())
        try:
            with pytest.raises(runtime_config.RuntimeConfigError, match="playlists"):
                runtime_config.render(config.load_strict(), as_saved)
        finally:
            as_saved.shutdown()
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


def test_the_apps_own_lanes_build_without_the_default_the_start_left_out(
    profiles: Any,  # noqa: F811
) -> None:
    """The start said "playback goes on without a default", and it didn't.

    The publication lane and the health lane each compile with a `Session`
    of their own, made on their worker, that was never told. Every change's
    publication was refused ("default playlist refers to unknown playlist
    'ghost'; repair it in the app"), no ``runtime.toml`` was written, and the
    health lane saved nothing.
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
        assert paths.settings_path().read_bytes() == saved

    told = [text for text in profile.window.reports if "default playlist" in text]
    assert len(told) == 1 and "couldn\u2019t be cleared" in told[0], told
    assert "renderer rejected this wallpaper" in paths.runtime_config_path().read_text("utf-8")


def test_the_apps_own_lanes_stay_strict_where_the_settings_can_be_changed(
    profiles: Any,  # noqa: F811
) -> None:
    """A default that went dangling under a running app, settings writable.

    Nothing is left out: the publication is refused, saying which default,
    and the next start's repair clears it.
    """
    profile: Profile = profiles()
    application = profile.application
    _saved(profile, "Lone", [profile.dawn])
    _published(application)
    good = paths.runtime_config_path().read_bytes()
    config.update({"active_playlist": "ghost"})  # another program's edit

    _published(application)

    assert "default playlist refers to unknown playlist 'ghost'" in application.publication_problem
    assert paths.runtime_config_path().read_bytes() == good
