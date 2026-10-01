"""``--ui`` picks the window; the default launch is exactly the classic one.

No GTK here: the GUI entry point is replaced by a recording stub, the same
way `test_control` checks ``--service`` and ``--open-page``. The display-backed
half (both windows under the real application) is `test_ui_next_window`.
"""

from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING

import pytest

from wall_in_one import cli
from wall_in_one.ui.window_services import DEFAULT_UI, UI_KINDS, UiKind, WindowServices

if TYPE_CHECKING:
    # The conformance proof: mypy --strict checks these assignments against
    # every WindowServices member and signature. Nothing runs at test time.
    from wall_in_one.ui.next.window import NextWindow
    from wall_in_one.ui.window import MainWindow

    def _classic_conforms(window: MainWindow) -> WindowServices:
        return window

    def _next_conforms(window: NextWindow) -> WindowServices:
        return window


Call = tuple[bool, str | None, UiKind | None]


def _install_gui(monkeypatch: pytest.MonkeyPatch, events: list[object]) -> list[Call]:
    """Stand in for ui.app with the pre-``--ui`` run() signature plus ``ui``."""
    calls: list[Call] = []
    fake = types.ModuleType("wall_in_one.ui.app")

    def run(
        _argv: list[str] | None = None,
        *,
        service: bool = False,
        initial_page: str | None = None,
        **keywords: UiKind,
    ) -> int:
        events.append("run")
        calls.append((service, initial_page, keywords.get("ui")))
        return 0

    fake.run = run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "wall_in_one.ui.app", fake)

    def gate(**_keywords: object) -> int | None:
        events.append("upgrade gate")
        return None

    monkeypatch.setattr(cli, "_run_graphical_startup_upgrade", gate)
    return calls


def test_the_default_is_classic_and_calls_run_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []
    calls = _install_gui(monkeypatch, events)

    assert cli.main([]) == 0

    assert DEFAULT_UI == "classic"
    assert calls == [(False, None, None)], "no ui keyword reaches run() without --ui"
    assert events == ["upgrade gate", "run"]


@pytest.mark.parametrize("choice", UI_KINDS)
def test_an_explicit_choice_reaches_run_after_the_upgrade_gate(
    monkeypatch: pytest.MonkeyPatch, choice: UiKind
) -> None:
    events: list[object] = []
    calls = _install_gui(monkeypatch, events)

    assert cli.main([f"--ui={choice}", "--open-page", "settings"]) == 0

    assert calls == [(False, "settings", choice)]
    assert events == ["upgrade gate", "run"]


def test_a_blocked_upgrade_never_reaches_either_window(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []
    calls = _install_gui(monkeypatch, events)
    monkeypatch.setattr(cli, "_run_graphical_startup_upgrade", lambda **_keywords: cli.EXIT_CONFIG)

    assert cli.main(["--ui", "next"]) == cli.EXIT_CONFIG
    assert calls == []


def test_an_unknown_ui_is_refused_before_any_upgrade_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[object] = []
    calls = _install_gui(monkeypatch, events)

    with pytest.raises(SystemExit) as raised:
        cli.main(["--ui", "modern"])

    assert raised.value.code == 2
    assert events == [] and calls == []
    assert "invalid choice: 'modern'" in capsys.readouterr().err


def test_the_flag_stays_out_of_help_while_the_new_ui_is_a_placeholder() -> None:
    assert "--ui" not in cli._build_parser().format_help()


def test_choosing_a_window_does_not_load_gtk_on_the_command_line() -> None:
    """The choices come from a GTK-free module, so ``ctl`` stays fast."""
    import wall_in_one.ui.window_services as services

    assert "gi" not in services.__dict__
    assert UI_KINDS == ("classic", "next")
