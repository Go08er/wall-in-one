"""``--ui`` picks the window; the default launch is exactly the classic one.

No GTK here: the GUI entry point is replaced by a recording stub, the same
way `test_control` checks ``--service`` and ``--open-page``. The display-backed
half (both windows under the real application) is `test_ui_next_window`.
"""

from __future__ import annotations

import subprocess
import sys
import types
from typing import TYPE_CHECKING

import pytest

from wall_in_one import cli, paths, ui_prefs
from wall_in_one.control import client
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


#: (service, initial_page, ui, preferred_ui), as run() received them.
Call = tuple[bool, str | None, UiKind | None, UiKind | None]


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
        calls.append((service, initial_page, keywords.get("ui"), keywords.get("preferred_ui")))
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
    assert calls == [(False, None, None, None)], "no ui keyword reaches run() without --ui"
    assert events == ["upgrade gate", "run"]


@pytest.mark.parametrize("choice", UI_KINDS)
def test_an_explicit_choice_reaches_run_after_the_upgrade_gate(
    monkeypatch: pytest.MonkeyPatch, choice: UiKind
) -> None:
    events: list[object] = []
    calls = _install_gui(monkeypatch, events)

    assert cli.main([f"--ui={choice}", "--open-page", "settings"]) == 0

    assert calls == [(False, "settings", choice, None)]
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


# -- the interface chosen in Settings (ui.toml) ---------------------------------------------


def _choose(interface: str) -> None:
    ui_prefs.update({"interface": interface})


def test_the_interface_chosen_in_settings_is_built_without_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _choose("next")
    before = paths.ui_prefs_path().read_bytes()
    events: list[object] = []
    calls = _install_gui(monkeypatch, events)

    assert cli.main([]) == 0
    assert cli.main(["--service"]) == 0

    assert calls == [(False, None, None, "next"), (True, None, None, "next")]
    assert paths.ui_prefs_path().read_bytes() == before, "the launch only reads it"


@pytest.mark.parametrize(
    ("saved", "flag", "expected"),
    [("next", "classic", "classic"), ("classic", "next", "next"), ("next", "next", "next")],
)
def test_an_explicit_flag_always_wins(
    monkeypatch: pytest.MonkeyPatch, saved: str, flag: UiKind, expected: UiKind
) -> None:
    _choose(saved)
    calls = _install_gui(monkeypatch, [])

    assert cli.main([f"--ui={flag}"]) == 0

    assert calls == [(False, None, expected, None)], "the saved choice is not even passed"


@pytest.mark.parametrize(
    "document",
    [None, 'version = 2\ninterface = "fancy"\n', "not toml [[", "version = 9\nfrost = 0.1\n"],
)
def test_a_missing_unusable_or_unknown_choice_starts_classic_as_before(
    monkeypatch: pytest.MonkeyPatch, document: str | None
) -> None:
    target = paths.ui_prefs_path()
    if document is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(document)
    calls = _install_gui(monkeypatch, [])

    assert cli.main(["--open-page", "media"]) == 0

    assert calls == [(False, "media", None, None)], "run() is called exactly as before"
    if document is None:
        assert not target.exists(), "nothing is created"
    else:
        assert target.read_text() == document, "nothing is rewritten"


def test_ctl_open_with_no_instance_starts_the_interface_chosen_in_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ctl open` starts a plain `wall-in-one --open-page <page>`, which reads the choice."""
    _choose("next")
    started: list[list[str]] = []

    def no_socket(*_arguments: object, **_keywords: object) -> object:
        raise client.NotRunningError("no Wall-in-One instance in this test")

    class Started:
        def __init__(self, argv: list[str], **_keywords: object) -> None:
            started.append(list(argv))

    monkeypatch.setattr(client, "send", no_socket)
    monkeypatch.setattr(subprocess, "Popen", Started)

    assert client.dispatch("open", "settings") == 0

    [argv] = started
    assert not any(word.startswith("--ui") for word in argv), "the launch carries no --ui"
    arguments = argv[argv.index("--open-page") :]
    calls = _install_gui(monkeypatch, [])
    assert cli.main(arguments) == 0
    assert calls == [(False, "settings", None, "next")]
