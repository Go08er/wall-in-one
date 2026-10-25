"""Web-off shutdown log: the restart advice (r13l S-N13, S-N14, S-N15).

A fake service only: a monkeypatched `client.send_runtime`; the install refused by a monkeypatch.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_ui_codex030_round1b import profiles, toolkit  # noqa: E402, F401
from tests.test_ui_renderer_settle_shutdown import HEARD, _naming  # noqa: E402
from tests.test_ui_web_off_at_shutdown import _heard, _workshop  # noqa: E402
from tests.test_web_off_toml_1_1 import document  # noqa: E402
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.control import client  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.ui import app as app_module  # noqa: E402

RESTART = "systemctl --user restart wall-in-one"
OLDER = "older than this app"


def _on_disk(text: str) -> None:
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    config.update({"allow_web_wallpapers": False})


def _judged_at_exit(application: Any) -> None:
    thread = getattr(application, "_web_off_deferred", None)
    if thread is not None:
        thread.join(60.0)
        assert not thread.is_alive()


def _step(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    *,
    heard: str,
    text: str | None,
    installed: bool,
    quit_answer: Any = None,
    web: int | None = None,
) -> tuple[list[str], list[str]]:
    """The step's log, and the verbs sent. ``web``: ``_workshop(web)`` first; the text is then
    ``_naming`` of its web page when ``text`` is None."""
    application = profiles().application
    if web is not None:
        _workshop(web)
        if text is None:
            text = _naming([str(2_000_000_000 + web)])
    assert text is not None
    _on_disk(text)
    _heard(monkeypatch, HEARD[heard])
    sent: list[str] = []

    def send(verb: str, *_a: object, **_k: object) -> Response:
        sent.append(verb)
        if isinstance(quit_answer, BaseException):
            raise quit_answer
        return quit_answer if quit_answer is not None else Response.success()

    monkeypatch.setattr(client, "send_runtime", send)
    if not installed:

        def refuse(*_a: object, **_k: object) -> None:
            raise runtime_config.RuntimeConfigError("the disk is full (r13m)")

        monkeypatch.setattr(runtime_config, "install_nothing_to_play", refuse)
    caplog.set_level(logging.INFO, logger=app_module.LOGGER.name)
    application._web_off_on_disk_at_exit()
    _judged_at_exit(application)
    said = [r.getMessage() for r in caplog.records if r.name == app_module.LOGGER.name]
    return sent, said


# -- p1 (S-N13) ------------------------------------------------------------------------------


def test_s_n13_a_failed_install_doesnt_call_a_service_that_reads_eight_older(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent, said = _step(
        profiles,
        monkeypatch,
        caplog,
        heard="control-reads-eight",
        text=document("control-compiler-line")[0],
        installed=False,
    )
    assert sent == []
    assert any("couldn\u2019t be replaced at shutdown" in one for one in said), said
    assert not any(OLDER in one for one in said), said


def test_control_s_n13_a_failed_install_for_an_older_service_says_it_is_older(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent, said = _step(
        profiles,
        monkeypatch,
        caplog,
        heard="an-older-service",
        text=document("control-compiler-line")[0],
        installed=False,
    )
    assert sent == []
    assert any(OLDER in one and RESTART in one for one in said), said


# -- p2 (S-N14) ------------------------------------------------------------------------------


def test_s_n14_an_installed_file_that_allowed_web_says_the_older_service_keeps_what_it_holds(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent, said = _step(
        profiles,
        monkeypatch,
        caplog,
        heard="an-older-service",
        text=document("control-compiler-line")[0],
        installed=True,
    )
    assert sent == [], "no quit for `_ALLOWS`"
    assert any("it was replaced at shutdown" in one for one in said), said
    assert any(RESTART in one for one in said), said


def test_control_s_n14_an_installed_file_naming_an_unchecked_item_says_it(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sent, said = _step(
        profiles,
        monkeypatch,
        caplog,
        heard="an-older-service",
        text=_naming(["4000000099"]),  # no such item: `_NAMES`
        installed=True,
    )
    assert sent == []
    assert any("it was replaced at shutdown" in one and RESTART in one for one in said), said
