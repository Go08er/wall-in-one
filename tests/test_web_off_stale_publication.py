"""A publication installs only what "Allow web wallpapers" says when it installs (r13c, D2).

Every publication reads ``settings.toml`` before its library scan (ExecStartPre's compile, a
hand-run ``--write-config``, the health sync) or before its compile (the window's own snapshot,
under ``--service``), and the web toggle's save doesn't take the compiler lock. So a person who
turned web wallpapers off while such a publication was compiling got a document that allows
them: installed, or left as it was when the stale compile matched the file. The holder's own
guard runs only when it fails, and the toggle's own publication, waiting for the lock, gave up
after five seconds.

Now the setting is read again under its own lock, which every writer of ``settings.toml``
takes, just before the install, and held across it: a publication compiled for another value
compiles again for what the file says, and the toggle's save waits for an install that is
already under way. The service is faked as one that isn't running, on a profile whose units
verify (`tests.test_web_off_nothing_to_play._service`), so documents carry the key exactly as
they would for a confirmed service. No socket, systemctl or bus is touched.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from tests.test_web_off_nothing_to_play import _library, _service
from wall_in_one import cli, config, paths, runtime_config
from wall_in_one.session import Session


def _web_on() -> Path:
    """A web page and a picture, with web wallpapers on; the library folder."""
    root = _library(Path(os.environ["HOME"]), still_beside=True)
    config.save(config.Settings(roots=(root,), allow_web_wallpapers=True))
    return root


def _allows_on_disk() -> bool:
    return runtime_config.allows_web_wallpapers(paths.runtime_config_path())


def _off_during_the_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The person turns web wallpapers off in the window while the publication scans."""
    refresh = Session.refresh

    def refresh_then_toggle(self: Session, *args: object, **kwargs: object) -> object:
        done = refresh(self, *args, **kwargs)  # type: ignore[arg-type]
        config.update({"allow_web_wallpapers": False})
        return done

    monkeypatch.setattr(Session, "refresh", refresh_then_toggle)


# -- the review's probe: ExecStartPre's compile (or --write-config) ---------------------------


@pytest.mark.parametrize("when", ["during-the-scan", "control-before", "control-left-on"])
def test_a_compile_that_read_web_on_installs_what_the_settings_say_now(
    monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    """Before: ``[during-the-scan]`` installed ``allow_web_wallpapers = true`` while
    ``settings.toml`` said false."""
    _web_on()
    _service(monkeypatch, schemas=None)
    if when == "control-before":
        config.update({"allow_web_wallpapers": False})
    elif when == "during-the-scan":
        _off_during_the_scan(monkeypatch)
    assert cli._write_runtime_config() == 0
    left_on = when == "control-left-on"
    assert config.load_strict().allow_web_wallpapers is left_on
    assert _allows_on_disk() is left_on, (
        "the installed runtime.toml doesn't say what settings.toml says of web wallpapers"
    )


def test_an_unchanged_document_that_allows_web_wallpapers_isnt_kept_once_they_are_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before: the stale compile matched the file on disk, so nothing was written, and the file
    the service loads next still allowed web wallpapers."""
    _web_on()
    _service(monkeypatch, schemas=None)
    assert cli._write_runtime_config() == 0
    assert _allows_on_disk()  # the control: compiled while they were on
    _off_during_the_scan(monkeypatch)
    assert cli._write_runtime_config() == 0
    assert config.load_strict().allow_web_wallpapers is False
    assert not _allows_on_disk(), "a document that allows web wallpapers was kept"


# -- every publication, whoever compiled it: a snapshot read before the compile ---------------


@pytest.mark.parametrize("saved", ["off", "control-on"])
@pytest.mark.parametrize("publication", ["publish_changes", "write"])
def test_a_snapshot_that_says_web_on_isnt_installed_while_the_file_says_off(
    monkeypatch: pytest.MonkeyPatch, saved: str, publication: str
) -> None:
    """``--service`` publishes the window's settings, read when they were last saved; another
    window, ``ctl`` or a second app may have saved them off since."""
    _web_on()
    _service(monkeypatch, schemas=None)
    snapshot = config.load_strict()
    session = Session(snapshot)
    try:
        session.refresh(mutate_removals=False)
        if saved == "off":
            config.update({"allow_web_wallpapers": False})
        if publication == "write":
            runtime_config.write(snapshot, session)
        else:
            runtime_config.publish_changes(snapshot, session)
    finally:
        session.shutdown()
    assert _allows_on_disk() is (saved == "control-on")


# -- the window between that read and the rename ----------------------------------------------


@pytest.mark.parametrize("toggle", ["during-the-install", "control-none"])
def test_turning_web_off_waits_for_an_install_already_under_way(
    monkeypatch: pytest.MonkeyPatch, toggle: str
) -> None:
    """The toggle's save lands just before the rename. Before, nothing held it off: a document
    that allows web wallpapers was renamed into place while ``settings.toml`` said off. Now it
    waits for the install, and the publication that follows it (the toggle's own) compiles
    with them off."""
    _web_on()
    _service(monkeypatch, schemas=None)
    real_install = runtime_config._install
    at_install: list[bool] = []
    toggles: list[threading.Thread] = []
    failures: list[BaseException] = []

    def turn_off() -> None:
        try:
            config.update({"allow_web_wallpapers": False})
        except BaseException as error:  # said by the test, not lost in the thread
            failures.append(error)

    def install(document: str, target: Path) -> None:
        if runtime_config.allows_web_wallpapers(text=document):
            if toggle == "during-the-install" and not toggles:
                thread = threading.Thread(target=turn_off, name="web-toggle")
                toggles.append(thread)
                thread.start()
                thread.join(0.5)  # it lands now unless something holds it off
            at_install.append(config.load_strict().allow_web_wallpapers)
        real_install(document, target)

    monkeypatch.setattr(runtime_config, "_install", install)
    assert cli._write_runtime_config() == 0
    for thread in toggles:
        thread.join(10)
        assert not thread.is_alive()
    assert not failures, failures
    assert at_install and all(at_install), (
        "a document that allows web wallpapers was renamed into place while settings.toml "
        "said they are off"
    )
    if toggle == "during-the-install":
        assert config.load_strict().allow_web_wallpapers is False  # the toggle was saved
        assert cli._write_runtime_config() == 0  # the publication that follows it
        assert not _allows_on_disk()
    else:
        assert _allows_on_disk()


# -- the guard's own document -----------------------------------------------------------------


@pytest.mark.parametrize("document", ["allowing", "control-plays-nothing"])
def test_a_document_that_plays_nothing_is_never_installed_allowing_web_wallpapers(
    document: str,
) -> None:
    nothing = runtime_config.nothing_to_play_document(
        config.Settings(), runtime_config.WEB_OFF_REASON
    )
    target = paths.runtime_config_path()
    if document == "allowing":
        allowing = nothing.replace("[settings]\n", "[settings]\nallow_web_wallpapers = true\n", 1)
        assert runtime_config.allows_web_wallpapers(text=allowing)
        with pytest.raises(runtime_config.RuntimeConfigError, match="never allows web"):
            runtime_config.install_nothing_to_play(allowing, target)
        assert not target.exists()
    else:
        runtime_config.install_nothing_to_play(nothing, target)
        assert target.read_text(encoding="utf-8") == nothing
