"""Settings → Tidy up and the one-time card, in the classic window.

The section is driven with a small application double over an isolated
profile. The card runs the real application on the golden profile, in
``tests/test_golden_tidy_card.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_tidy import _claim_folder, _noctalia_settings  # noqa: E402
from wall_in_one import paths, tidy  # noqa: E402
from wall_in_one.theme import noctalia  # noqa: E402
from wall_in_one.ui.tidy_section import TidyLane, TidySection  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class _Application:
    """What the section asks of the application, and nothing else."""

    def __init__(self) -> None:
        self.settings = SimpleNamespace(roots=())
        self.tidy_lane = TidyLane()
        self.reports: list[str] = []
        self.ready = True

    def window_report(self, message: str) -> None:
        self.reports.append(message)

    def require_authoring_ready(self) -> bool:
        return self.ready


@pytest.fixture
def section(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TidySection, _Application]]:
    reloads: list[str] = []
    monkeypatch.setattr(noctalia, "reload_config", lambda: reloads.append("config-reload"))
    application = _Application()
    page = Adw.PreferencesPage()
    group = TidySection(application)  # type: ignore[arg-type]
    page.add(group)
    window = Gtk.Window(child=page, default_width=900, default_height=900)
    window.present()
    try:
        yield group, application
    finally:
        application.tidy_lane.shutdown()
        window.destroy()


def _busy(group: TidySection) -> bool:
    return group.busy


def _idle(group: TidySection) -> bool:
    return not group.busy and group.row(tidy.LEFTOVERS) is not None


def _texts(group: TidySection, action: str) -> list[str]:
    row = group.row(action)
    assert row is not None
    found: list[str] = []
    for child in row.children:
        if isinstance(child, Adw.ActionRow):
            found.append(child.get_title())
        else:
            found.extend(label.get_text() for label in _labels(child))
    return found


def _labels(widget: Gtk.Widget) -> Iterator[Gtk.Label]:
    if isinstance(widget, Gtk.Label):
        yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _labels(child)
        child = child.get_next_sibling()


def test_the_section_previews_applies_and_undoes_each_action(
    section: tuple[TidySection, _Application],
) -> None:
    group, application = section
    settings = _noctalia_settings()
    original = settings.read_bytes()
    for name in ("entry-aaaaaaaa", "entry-bbbbbbbb", "entry-cccccccc"):
        _claim_folder(paths.app_state_dir(), name)
    group.refresh()
    spin_until(lambda: _idle(group) and group.row(tidy.PLUGIN_SETTINGS) is not None)

    leftovers = group.row(tidy.LEFTOVERS)
    plugin = group.row(tidy.PLUGIN_SETTINGS)
    old_template = group.row(tidy.OLD_PALETTE_TEMPLATE)
    cache = group.row(tidy.THUMBNAIL_CACHE)
    assert leftovers and plugin and old_template and cache
    assert "3 items: empty claim folder from a finished removal" in _texts(group, tidy.LEFTOVERS)
    assert any(text.startswith("Every path (3)") for text in _texts(group, tidy.LEFTOVERS))
    plugin_texts = _texts(group, tidy.PLUGIN_SETTINGS)
    assert any("remove hub_placement" in text for text in plugin_texts)
    assert any(text.startswith("Kept: refresh_interval_seconds") for text in plugin_texts)
    assert plugin.apply.get_visible() and plugin.apply.get_sensitive()
    assert not plugin.undo.get_visible()
    assert not old_template.apply.get_visible(), "nothing to archive while Noctalia uses it"
    assert not cache.apply.get_visible() and cache.apply.get_label() == "Clear Cache"

    application.ready = False
    plugin.apply.emit("clicked")
    assert not _busy(group), "nothing starts while authoring is paused"
    application.ready = True

    plugin.apply.emit("clicked")
    assert _busy(group) and plugin.spinner.get_visible() and not leftovers.apply.get_sensitive()
    spin_until(lambda: _idle(group) and plugin.plan.undo is not None, timeout=10)

    assert application.reports[-1].startswith("Removed 3 old plugin settings")
    assert "hub_placement" not in settings.read_text()
    assert plugin.undo.get_visible() and not plugin.apply.get_visible()
    assert not plugin.spinner.get_visible()

    plugin.undo.emit("clicked")
    spin_until(lambda: _idle(group) and plugin.plan.undo is None, timeout=10)

    assert settings.read_bytes() == original
    assert application.reports[-1] == "Noctalia's settings are back as they were."

    leftovers.apply.emit("clicked")
    spin_until(lambda: _idle(group) and leftovers.plan.undo is not None, timeout=10)
    assert not any((paths.app_state_dir() / ".wall-in-one-retained").iterdir())
    leftovers.undo.emit("clicked")
    spin_until(lambda: _idle(group) and leftovers.plan.undo is None, timeout=10)
    assert len(list((paths.app_state_dir() / ".wall-in-one-retained").iterdir())) == 3


def test_a_preview_gone_stale_is_refused_and_replaced(
    section: tuple[TidySection, _Application],
) -> None:
    group, application = section
    settings = _noctalia_settings()
    group.refresh()
    spin_until(lambda: _idle(group) and group.row(tidy.PLUGIN_SETTINGS) is not None)
    plugin = group.row(tidy.PLUGIN_SETTINGS)
    assert plugin is not None
    shown = plugin.plan
    settings.write_text(settings.read_text().replace('mode = "dark"', 'mode = "light"'))
    saved = settings.read_bytes()

    plugin.apply.emit("clicked")
    spin_until(lambda: _idle(group) and plugin.plan.token != shown.token, timeout=10)

    assert "changed since the preview" in application.reports[-1]
    assert settings.read_bytes() == saved
    assert plugin.plan.ready, "the new preview can be applied"
