"""The session log: bounded, in memory, said once per change, paths hidden on request."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from tests.test_next_status_line import BATTERY, two_display_status
from wall_in_one import paths
from wall_in_one.ui.activity_log import MAX_TEXT, ActivityLog, PathHider
from wall_in_one.ui.status_model import RuntimeStatusModel


def _clock() -> datetime.datetime:
    return datetime.datetime(2026, 10, 4, 9, 30, 0)


def _following() -> tuple[ActivityLog, RuntimeStatusModel]:
    log = ActivityLog(clock=_clock)
    model = RuntimeStatusModel()
    model.subscribe(log.observe)
    return log, model


def _said(log: ActivityLog) -> list[tuple[str, str, str]]:
    return [(entry.source, entry.level, entry.text) for entry in log.entries]


def test_a_repeated_poll_adds_nothing_and_each_change_is_said_once() -> None:
    log, model = _following()
    model.adopt(two_display_status())
    model.adopt(two_display_status())
    assert _said(log) == [
        ("service", "info", "The wallpaper service is running"),
        ("playback", "info", "Playing Evening (schedule)"),
    ]

    crashed = two_display_status(extra={"last_error": "linux-wallpaperengine exited (1)"})
    model.adopt(crashed)
    model.adopt(crashed)
    model.adopt(two_display_status(extra={"output_discovery_error": "niri msg outputs failed"}))
    model.adopt(two_display_status(extra=BATTERY))
    model.adopt(two_display_status())
    assert _said(log)[2:] == [
        ("renderer", "error", "linux-wallpaperengine exited (1)"),
        ("displays", "warning", "Display discovery is degraded: niri msg outputs failed"),
        ("power", "info", "Animations stopped on battery"),
        ("power", "info", "Animations are no longer stopped for power"),
    ]
    assert log.problems == 2


def test_the_service_going_and_coming_back_and_its_bad_replies() -> None:
    log, model = _following()
    model.mark_unavailable(forget=True)
    model.mark_unavailable(forget=True)
    model.mark_delayed()  # the next probe timed out instead
    model.mark_delayed()
    model.mark_invalid("status reply is not JSON")
    model.mark_invalid("status reply is not JSON")
    model.set_busy(True)
    model.adopt(two_display_status())
    model.mark_delayed()
    assert [text for _source, _level, text in _said(log)] == [
        "The wallpaper service isn\u2019t running",
        "A status reply was late; the last one is shown until the next",
        "Invalid status reply: status reply is not JSON",
        "The wallpaper service is running",
        "Playing Evening (schedule)",
        "A status reply was late; the last one is shown until the next",
    ]


def test_it_keeps_only_the_newest_entries_and_bounded_text() -> None:
    log = ActivityLog(clock=_clock, limit=3)
    for number in range(5):
        log.record("app", f"message {number}")
    log.record("app", "   ")  # nothing to say
    assert [entry.text for entry in log.entries] == ["message 2", "message 3", "message 4"]
    log.record("renderer", "x" * (MAX_TEXT + 50), "error")
    assert len(log.entries[-1].text) == MAX_TEXT and log.entries[-1].text.endswith("\u2026")
    log.record("app", "one\n  line")
    assert log.entries[-1].text == "one\n  line", "kept as written"
    assert log.shown(log.entries[-1], hide=False) == "one line", "shown on one line"


def test_listeners_hear_each_new_entry_until_they_leave() -> None:
    log = ActivityLog(clock=_clock)
    heard: list[str] = []
    leave = log.subscribe(lambda entry: heard.append(entry.text))
    log.record("app", "first")
    leave()
    leave()
    log.record("app", "second")
    assert heard == ["first"]


def _profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home folder with a space, both quotes and a non-ASCII letter, and that login."""
    home = tmp_path / 'J\u00fcrgen\'s "home" dir'
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOGNAME", "j\u00fcrgen")
    monkeypatch.setenv("USER", "j\u00fcrgen")
    return home


def test_known_folders_are_hidden_whole_whatever_their_names_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _profile(tmp_path, monkeypatch)
    inside = home / "My Walls (2024) \u00fc"  # a library inside the home folder
    shared = tmp_path / "mnt" / 'Shared "walls" here'
    # The home folder inside a library's path, under some other prefix.
    mirror = Path("/srv/backup") / str(home).lstrip("/") / "walls"
    folders = [inside, shared, mirror]
    log = ActivityLog(clock=_clock, folders=lambda: folders)
    for text, hidden in (
        (f"Using {inside} as the wallpaper library", "Using <library 1> as the wallpaper library"),
        (f"Using {inside}/ now.", "Using <library 1>/ now."),
        (f"Scanned {shared}/neon rain.mp4, 3 files", "Scanned <library 2>/neon rain.mp4, 3 files"),
        (f"Copied {mirror}/a b.png", "Copied <library 3>/a b.png"),
        (f"{home}/Downloads/x y.png is read-only", "~/Downloads/x y.png is read-only"),
        (f"Settings live in {home}.", "Settings live in ~."),
        (
            f"{paths.settings_path()} is read-only",
            "<config>/wall-in-one/settings.toml is read-only",
        ),
        ("Steam is at /opt/j\u00fcrgen/steam", "Steam is at /opt/<user>/steam"),
        ("https://wallhaven.cc/w/abc123 is 404", "https://wallhaven.cc/w/abc123 is 404"),
    ):
        log.record("app", text)
        assert log.shown(log.entries[-1], hide=True) == hidden, text
        assert log.shown(log.entries[-1], hide=False) == text, "the entry keeps its words"


def test_paths_quoted_in_other_forms_are_hidden_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _profile(tmp_path, monkeypatch)
    real = tmp_path / "real home"
    real.mkdir()
    linked = tmp_path / "linked home"
    linked.symlink_to(real)
    monkeypatch.setenv("HOME", str(linked))  # /home a link, as on some systems
    library = linked / "Walls \u00e9t\u00e9"
    hider = ActivityLog(folders=lambda: [library, home]).hider
    picture = str(library / "a b.png")
    assert hider(f"open {Path(picture).as_uri()}") == "open file://<library 1>/a%20b.png"
    assert hider(json.dumps({"path": picture})) == '{"path": "<library 1>/a b.png"}'
    assert hider(repr(str(home / "x"))) == "'<library 2>/x'"
    assert hider(json.dumps(str(home / "x"))) == '"<library 2>/x"'
    assert hider(f"mpv: cannot open {real}/clip.mp4") == "mpv: cannot open ~/clip.mp4"
    assert hider(f'Error: "{library}/a.mp4"') == 'Error: "<library 1>/a.mp4"'


def test_copying_with_paths_hidden_never_leaks_the_user_or_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _profile(tmp_path, monkeypatch)
    library = home / "Walls"
    folders = [library]
    log = ActivityLog(clock=_clock, folders=lambda: folders)
    log.record("app", f"Using {library} as the wallpaper library")
    folders.clear()  # the folder removed later: still hidden, still number 1
    log.record("renderer", f"mpv could not open {library}/clip one.mp4", "error")
    long = "x" * (MAX_TEXT - 10) + f"{home}/far/away.png"
    log.record("renderer", long, "error")  # cut before the home folder, never inside it
    assert log.entries[-1].text == "x" * (MAX_TEXT - 10) + "\u2026"
    copied = log.as_text(hide=True)
    assert copied.splitlines() == [
        "09:30:00  app       Using <library 1> as the wallpaper library",
        "09:30:00  renderer  mpv could not open <library 1>/clip one.mp4",
        "09:30:00  renderer  " + "x" * (MAX_TEXT - 10) + "\u2026",
    ]
    for secret in (str(home), "j\u00fcrgen", "J\u00fcrgen", str(tmp_path)):
        assert secret not in copied
    assert str(library) in log.as_text(hide=False)


def _hider(user: str) -> PathHider:
    """Codex's probe: a home of ``/home/<login>`` and no other folder in play."""
    return PathHider(home=f"/home/{user}", places=(), user=user)


def test_a_single_letter_login_is_hidden_where_it_is_a_path_component() -> None:
    """Q-4 (round 2): a one-letter login was never hidden, even inside a path."""
    hide = _hider("u")
    assert hide("cannot open /mnt/u/private/a.png") == "cannot open /mnt/<user>/private/a.png"
    assert hide("files under /srv/u") == "files under /srv/<user>"
    assert hide("files under /srv/u.") == "files under /srv/<user>."
    assert hide("relative u/private/a.png") == "relative <user>/private/a.png"
    assert hide("/home/u/x and /mnt/u/y") == "~/x and /mnt/<user>/y"
    # Not a component of its own: another name that starts or ends with it.
    for kept in ("/mnt/users/a.png", "/mnt/menu/a.png", "/mnt/u.bak/a.png", "/mnt/u-2/a"):
        assert hide(kept) == kept


def test_a_single_letter_login_is_hidden_in_uri_and_json_spellings() -> None:
    hide = _hider("\u00fc")  # a login of one non-ASCII letter: its spellings differ
    picture = "/mnt/\u00fc/private/a b.png"
    assert hide(f"open {Path(picture).as_uri()}") == "open file:///mnt/<user>/private/a%20b.png"
    assert hide(json.dumps({"path": picture})) == '{"path": "/mnt/<user>/private/a b.png"}'
    assert hide(json.dumps({"path": picture}, ensure_ascii=False)) == (
        '{"path": "/mnt/<user>/private/a b.png"}'
    )
    assert hide(picture) == "/mnt/<user>/private/a b.png"
    assert hide(r'"\/mnt\/\u00fc\/private"') == r'"\/mnt\/<user>\/private"'


def test_a_single_letter_login_is_left_alone_as_a_word_in_a_sentence() -> None:
    for login, sentence in (
        ("u", "u can't play this: the file is u-shaped, see section u"),
        ("a", "Added a wallpaper to a playlist"),
    ):
        assert _hider(login)(sentence) == sentence
    # A longer login still goes as a word, as before.
    assert _hider("ann")("Steam belongs to ann today") == "Steam belongs to <user> today"
