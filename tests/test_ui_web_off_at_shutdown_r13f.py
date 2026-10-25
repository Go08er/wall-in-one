"""The last step of a shutdown while web wallpapers are off: review r13f's findings, made tests.

`Application._web_off_on_disk_at_exit`, called as `do_shutdown` calls it last, on a windowless
application. Review r13f found, at f127a31:

* **N1:** its look at the file read TOML 1.0, and the service reads 1.1: a hand edit the
  service loads as allowing web wallpapers was left.

Each case has a control that passes before and after.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from tests.test_ui_codex030_round1b import profiles, toolkit  # noqa: E402, F401
from tests.test_web_off_toml_1_1 import DOCUMENTS, document  # noqa: E402
from wall_in_one import config, paths, runtime_config  # noqa: E402


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
    so were ``[inline-table-over-lines]`` and ``[key-and-an-x-escape-elsewhere]``. What isn't
    TOML 1.0 is replaced (fail closed); a TOML 1.0 document is judged as before."""
    application = profiles().application
    text, service_allows, toml_1_0 = document(name)
    _on_disk(text)
    application._web_off_on_disk_at_exit()
    now = paths.runtime_config_path().read_text(encoding="utf-8")
    if service_allows or not toml_1_0:
        assert now == _nothing_to_play(), (
            f"{name}: the service may load it as allowing web wallpapers, and it was left"
        )
        return
    assert now == text, f"{name}: a TOML 1.0 document that allows no web wallpaper was replaced"
