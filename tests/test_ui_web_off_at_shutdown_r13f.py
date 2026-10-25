"""The last step of a shutdown while web wallpapers are off: review r13f's findings, made tests.

`Application._web_off_on_disk_at_exit`, called as `do_shutdown` calls it last, on a windowless
application. Review r13f found, at f127a31:

* **N1:** its look at the file read TOML 1.0, and the service reads 1.1: a hand edit the
  service loads as allowing web wallpapers was left.
* **N2:** that look parsed a compiled document whenever a value in it had a ``\\u``, ``\\U``
  or ``\\x`` (a still's path, a title): 0.13 s for each megabyte, about a second at the cap,
  and a second Ctrl-C couldn't end it.

Each case has a control that passes before and after.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import tomllib
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_ui_codex030_round1b import profiles, toolkit  # noqa: E402, F401
from tests.test_web_off_toml_1_1 import DOCUMENTS, document  # noqa: E402
from wall_in_one import config, paths, runtime_config  # noqa: E402

#: How long anything here waits before it gives up: past it the test fails, it doesn't hang.
GIVE_UP: float = 60.0


def _on_disk(text: str) -> None:
    """``text`` as ``runtime.toml``, with web wallpapers off in ``settings.toml``."""
    target = paths.runtime_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    config.update({"allow_web_wallpapers": False})


def _nothing_to_play() -> str:
    return runtime_config.nothing_to_play_document(
        config.load_strict(), runtime_config.WEB_OFF_REASON
    )


# -- N1: TOML 1.1 -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", DOCUMENTS)
def test_a_document_the_service_reads_as_allowing_web_wallpapers_is_replaced(
    profiles: Any,  # noqa: F811
    name: str,
) -> None:
    """Before: ``[x-escape-key]`` was left (its escape sent it to a parse that refused it), and
    so were ``[inline-table-over-lines]`` and ``[key-and-an-x-escape-elsewhere]``. Each one the
    service loads as allowing web wallpapers is replaced; the step judges exactly as the service
    does here, since a document with no spelling of the key anywhere (``[an-x-escape-and-no-
    key]``) can't allow them under any TOML, and is left."""
    application = profiles().application
    text, service_allows, _toml_1_0 = document(name)
    _on_disk(text)
    application._web_off_on_disk_at_exit()
    now = paths.runtime_config_path().read_text(encoding="utf-8")
    if service_allows:
        assert now == _nothing_to_play(), (
            f"{name}: the service loads it as allowing web wallpapers, and it was left"
        )
        return
    assert now == text, f"{name}: a document that allows no web wallpaper was replaced"


# -- N2: the look at the file -------------------------------------------------------------------


def _compiled(megabytes: float, sequence: str) -> str:
    """A compiler-shaped document of about ``megabytes`` MiB that allows no web wallpaper, with
    ``sequence`` (as `runtime_config._quote` writes it) in one still's path when it is given."""
    head = runtime_config.nothing_to_play_document(config.Settings(), runtime_config.WEB_OFF_REASON)
    lines = [head, "", "[[playlists]]", 'id = "big"', 'name = "Big"']
    size, index = len(head), 0
    while size < megabytes * 1024 * 1024 - 64 * 1024:
        still = f"/home/someone/Pictures/wallpapers/Wall-in-One/Automatic Stills/{index:08d}.png"
        if sequence and index == 1000:
            still = f"/home/someone/Pictures/odd{sequence}/still.png"
        block = [
            "[[playlists.entries]]",
            f"id = {json.dumps(f'/home/someone/Pictures/wallpapers/item-{index:08d}.png')}",
            'kind = "still"',
            f"still = {json.dumps(still, ensure_ascii=False)}",
        ]
        lines.extend(block)
        size += sum(len(one) + 1 for one in block)
        index += 1
    return "\n".join(lines) + "\n"


def _parses_of(monkeypatch: pytest.MonkeyPatch, text: str) -> list[bool]:
    """Every TOML parse of ``text`` from here on, recorded."""
    loads = tomllib.loads
    parsed: list[bool] = []

    def recorded(document: str, *arguments: Any, **keywords: Any) -> Any:
        if document == text:
            parsed.append(True)
        return loads(document, *arguments, **keywords)

    monkeypatch.setattr(tomllib, "loads", recorded)
    return parsed


@pytest.mark.parametrize(
    ("megabytes", "sequence"),
    [
        (1.0, "\\user"),  # a backslash before a u: written \\u
        (8.0, "\\user"),
        (1.0, "\\x41"),
        (1.0, "\a"),  # a control character: written \u0007
        (1.0, ""),  # control: no backslash anywhere
    ],
    ids=["backslash-u-1MiB", "backslash-u-8MiB", "backslash-x", "control-character", "control"],
)
def test_a_compiled_document_is_judged_without_a_parse(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    megabytes: float,
    sequence: str,
) -> None:
    """s4, judged by what the step does rather than by how long it takes. Before: each of these
    but the control was parsed whole, in up to a second at the cap, which a second Ctrl-C
    couldn't end; and it was left, as it is now. The control was never parsed."""
    application = profiles().application
    text = _compiled(megabytes, sequence)
    assert len(text.encode()) <= runtime_config.MAX_RUNTIME_CONFIG_BYTES
    assert "\\" in text or not sequence
    _on_disk(text)
    parsed = _parses_of(monkeypatch, text)
    application._web_off_on_disk_at_exit()
    assert not parsed, f"the step parsed a compiled document {len(parsed)} time(s)"
    assert paths.runtime_config_path().read_text(encoding="utf-8") == text


def test_control_a_hand_edit_that_names_the_key_is_parsed(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What only a parse can tell (here: ``= false``) is still parsed, and left."""
    application = profiles().application
    text, _service_allows, _toml_1_0 = document("control-off")
    _on_disk(text)
    parsed = _parses_of(monkeypatch, text)
    application._web_off_on_disk_at_exit()
    assert parsed, "a document that names the key wasn't parsed"
    assert paths.runtime_config_path().read_text(encoding="utf-8") == text


@pytest.mark.parametrize("variant", ["second-ctrl-c", "control-no-ctrl-c"])
def test_a_second_ctrl_c_ends_the_wait_for_a_parse(
    profiles: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    """A hand edit only a parse can judge, whose parse is held (standing for a large one). A
    second Ctrl-C (a real SIGINT, to this process) while the step waits for it ends the wait:
    the step returns before the parse is let go, and the file counts as allowing web wallpapers
    (fail closed). Before: the parse ran on GTK's thread, and the step returned only once it
    was let go, leaving the file. The control: no Ctrl-C; the parse ends, and the document,
    which allows none, is left."""
    application = profiles().application
    text, _service_allows, _toml_1_0 = document("control-off")
    _on_disk(text)
    parsing = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    order: list[str] = []
    failures: list[str] = []
    allows = runtime_config.allows_web_wallpapers

    def held(*arguments: Any, **keywords: Any) -> bool:
        if keywords.get("text") == text:
            parsing.set()
            if not release.wait(GIVE_UP):
                failures.append("the parse was never let go")
        return allows(*arguments, **keywords)

    monkeypatch.setattr(runtime_config, "allows_web_wallpapers", held)

    def second_ctrl_c() -> None:
        if not parsing.wait(GIVE_UP):
            failures.append("the step never parsed the document")
        elif variant == "second-ctrl-c":
            os.kill(os.getpid(), signal.SIGINT)  # as the terminal sends it: to the process
            if not returned.wait(GIVE_UP):
                failures.append("the step didn't return after the second Ctrl-C")
        order.append("let go")
        release.set()

    thread = threading.Thread(target=second_ctrl_c, name="test-ctrl-c")
    thread.start()
    try:
        application._web_off_on_disk_at_exit()
        order.append("returned")
        returned.set()
    finally:
        release.set()
        thread.join(GIVE_UP)
    assert not failures, failures
    now = paths.runtime_config_path().read_text(encoding="utf-8")
    if variant == "control-no-ctrl-c":
        assert order == ["let go", "returned"], order
        assert now == text, "a document that allows no web wallpaper was replaced"
        return
    assert order == ["returned", "let go"], f"the step waited for the parse: {order}"
    assert now == _nothing_to_play(), "a document the step couldn't judge was left"
