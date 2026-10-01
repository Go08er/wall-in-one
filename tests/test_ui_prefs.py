"""ui.toml: typed defaults, unknown keys carried, newer versions read-only."""

from __future__ import annotations

import datetime
import math
import os
import re
import stat
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from wall_in_one import paths, ui_prefs
from wall_in_one.ui_prefs import GlassOpacity, UiPrefs


def _write(text: str) -> Path:
    target = paths.ui_prefs_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _siblings() -> set[str]:
    return {entry.name for entry in paths.app_config_dir().iterdir()}


def test_the_file_lives_beside_settings_in_the_config_directory() -> None:
    assert paths.ui_prefs_path() == paths.app_config_dir() / "ui.toml"
    assert paths.ui_prefs_path().parent == paths.settings_path().parent


def test_a_missing_file_means_defaults_and_loading_creates_nothing() -> None:
    loaded = ui_prefs.load()

    assert loaded.prefs == UiPrefs()
    assert loaded.prefs.window_style == "solid"
    assert loaded.prefs.background_opacity == GlassOpacity(translucent=0.55, frosted=0.30)
    assert loaded.prefs.panel_opacity == GlassOpacity(translucent=0.80, frosted=0.60)
    assert loaded.prefs.frost == 0.5
    assert loaded.prefs.thumbnail_size == "large"
    assert loaded.prefs.last_page == "library"
    assert loaded.writable
    assert loaded.raw == {}
    assert not paths.app_config_dir().exists()


def test_a_round_trip_keeps_every_field_and_writes_version_1_with_mode_0644() -> None:
    chosen = UiPrefs(
        window_style="frosted",
        background_opacity=GlassOpacity(translucent=0.4, frosted=0.25),
        panel_opacity=GlassOpacity(translucent=0.9, frosted=0.0),
        frost=1.0,
        thumbnail_size="small",
        last_page="playlist:caa7a4aa1265ee01",
    )

    saved = ui_prefs.save(chosen)

    assert saved.prefs == chosen
    assert ui_prefs.load().prefs == chosen
    target = paths.ui_prefs_path()
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    document = tomllib.loads(target.read_text(encoding="utf-8"))
    assert document == {
        "version": 1,
        "window_style": "frosted",
        "frost": 1.0,
        "thumbnail_size": "small",
        "last_page": "playlist:caa7a4aa1265ee01",
        "background_opacity": {"translucent": 0.4, "frosted": 0.25},
        "panel_opacity": {"translucent": 0.9, "frosted": 0.0},
    }


def test_update_changes_only_the_named_fields_of_the_latest_file() -> None:
    ui_prefs.save(UiPrefs(window_style="translucent", thumbnail_size="small"))

    updated = ui_prefs.update({"frost": 0.75})

    assert updated.prefs == UiPrefs(window_style="translucent", thumbnail_size="small", frost=0.75)
    assert ui_prefs.load().prefs == updated.prefs
    with pytest.raises(ui_prefs.UiPrefsError, match="unknown UI preference"):
        ui_prefs.update({"colour": "red"})


def test_an_unchanged_save_does_not_rewrite_the_file() -> None:
    ui_prefs.save(UiPrefs(frost=0.25))
    before = paths.ui_prefs_path().stat()

    ui_prefs.update({"frost": 0.25})

    after = paths.ui_prefs_path().stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_invalid_values_fall_back_to_the_default_for_that_field_only() -> None:
    _write(
        "version = 1\n"
        'window_style = "neon"\n'
        "frost = 1.5\n"
        "thumbnail_size = 3\n"
        'last_page = ""\n'
        'panel_opacity = "half"\n'
        "[background_opacity]\n"
        "translucent = -0.1\n"
        "frosted = 0.2\n"
    )

    prefs = ui_prefs.load().prefs

    assert prefs.window_style == "solid"
    assert prefs.frost == 0.5
    assert prefs.thumbnail_size == "large"
    assert prefs.last_page == "library"
    assert prefs.background_opacity == GlassOpacity(translucent=0.55, frosted=0.2)
    assert prefs.panel_opacity == ui_prefs.DEFAULT_PANEL_OPACITY

    # Saving writes those defaults; the valid frosted value survives.
    ui_prefs.update({"thumbnail_size": "small"})
    document = tomllib.loads(paths.ui_prefs_path().read_text(encoding="utf-8"))
    assert document["window_style"] == "solid"
    assert document["background_opacity"] == {"translucent": 0.55, "frosted": 0.2}
    assert document["panel_opacity"] == {"translucent": 0.8, "frosted": 0.6}


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        (True, 0.5),
        (1, 1.0),
        (0, 0.0),
        (2, 0.5),
        (10**400, 0.5),
        (math.nan, 0.5),
        (math.inf, 0.5),
        ("0.7", 0.5),
        (0.7, 0.7),
    ),
)
def test_unit_numbers_accept_only_finite_values_from_0_to_1(raw: object, expected: float) -> None:
    assert UiPrefs.from_mapping({"frost": raw}).frost == expected


@pytest.mark.parametrize("page", ("x" * 257, "tab\there", "\ud800", 7, ""))
def test_last_page_must_be_short_printable_text(page: object) -> None:
    assert UiPrefs.from_mapping({"last_page": page}).last_page == "library"


def test_programmatic_invalid_values_are_validated_before_saving() -> None:
    saved = ui_prefs.save(replace(UiPrefs(), window_style="glass", frost=-1.0))

    assert saved.prefs == UiPrefs()
    assert ui_prefs.load().prefs == UiPrefs()


UNKNOWN_DOCUMENT = """\
version = 1
window_style = "translucent"
future = "keep me"
"odd key" = "quote \\" backslash \\\\ newline \\n delete \\u007F tab \\t emoji 🎨"
when = 1979-05-27T07:32:00-08:00
local = 1979-05-27T07:32:00.999999
day = 1979-05-27
alarm = 07:32:00
huge = 9223372036854775807
tiny = 5e-324
edge = -inf
sizes = [1, 2.5, "three", [true, false], { inline = 1 }]

[[layouts]]
name = "wide"
columns = 6

[[layouts]]
name = "narrow"

[background_opacity]
translucent = 0.5
frosted = 0.3
acrylic = 0.45

[panel_opacity]
frosted = 0.7
mica = { tint = 0.2, blur = [1, 2] }

[future_table]
answer = 42
nested = { deeper = { deepest = "yes" }, empty = {} }
"""


def test_unknown_keys_anywhere_are_preserved_on_save() -> None:
    _write(UNKNOWN_DOCUMENT)
    original = tomllib.loads(UNKNOWN_DOCUMENT)
    loaded = ui_prefs.load()
    assert loaded.writable
    assert loaded.unknown_keys == (
        "future",
        "odd key",
        "when",
        "local",
        "day",
        "alarm",
        "huge",
        "tiny",
        "edge",
        "sizes",
        "layouts",
        "future_table",
        "background_opacity.acrylic",
        "panel_opacity.mica",
    )

    ui_prefs.update({"frost": 0.9, "panel_opacity": GlassOpacity(translucent=0.6, frosted=0.65)})

    saved = tomllib.loads(paths.ui_prefs_path().read_text(encoding="utf-8"))
    expected = {
        **original,
        "frost": 0.9,
        "thumbnail_size": "large",
        "last_page": "library",
        "background_opacity": {"translucent": 0.5, "frosted": 0.3, "acrylic": 0.45},
        "panel_opacity": {
            "translucent": 0.6,
            "frosted": 0.65,
            "mica": {"tint": 0.2, "blur": [1, 2]},
        },
    }
    assert saved == expected
    assert saved["edge"] == -math.inf
    assert saved["when"] == datetime.datetime(
        1979, 5, 27, 7, 32, tzinfo=datetime.timezone(datetime.timedelta(hours=-8))
    )
    # And a second round trip is stable byte for byte.
    first = paths.ui_prefs_path().read_bytes()
    ui_prefs.update({"frost": 0.9})
    assert paths.ui_prefs_path().read_bytes() == first


@pytest.mark.parametrize("version", (2, 99))
def test_a_newer_version_is_read_but_never_written(version: int) -> None:
    target = _write(
        f'version = {version}\nwindow_style = "frosted"\nfrost = 0.2\n'
        'new_style = "liquid"\n[background_opacity]\nfrosted = 0.1\n'
    )
    before = target.read_bytes()

    loaded = ui_prefs.load()

    assert loaded.prefs == UiPrefs(
        window_style="frosted",
        frost=0.2,
        background_opacity=GlassOpacity(translucent=0.55, frosted=0.1),
    )
    assert not loaded.writable
    assert f"newer version of Wall-in-One (version {version})" in loaded.read_only
    with pytest.raises(ui_prefs.UiPrefsReadOnlyError, match="won't change the file"):
        ui_prefs.save(UiPrefs())
    with pytest.raises(ui_prefs.UiPrefsReadOnlyError, match="won't change the file"):
        ui_prefs.update({"frost": 0.9})
    assert target.read_bytes() == before


@pytest.mark.parametrize("version", ('"1"', "0", "-3", "true", "1.5", "[1]"))
def test_an_unrecognized_version_is_also_read_only(version: str) -> None:
    target = _write(f'version = {version}\nwindow_style = "frosted"\n')
    before = target.read_bytes()

    loaded = ui_prefs.load()

    assert loaded.prefs.window_style == "frosted"
    assert "doesn't recognize" in loaded.read_only
    with pytest.raises(ui_prefs.UiPrefsReadOnlyError):
        ui_prefs.update({"frost": 0.9})
    assert target.read_bytes() == before


def test_a_file_without_a_version_is_version_1() -> None:
    _write('window_style = "translucent"\n')

    assert ui_prefs.load().writable
    ui_prefs.update({"frost": 0.4})

    document = tomllib.loads(paths.ui_prefs_path().read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert document["window_style"] == "translucent"


@pytest.mark.parametrize(
    "content",
    (
        b"window_style = \n",
        b"\xff\xfe not utf-8",
        b"x = " + b"[" * 2000 + b"]" * 2000 + b"\n",
        b"# " + b"x" * ui_prefs.MAX_BYTES + b"\n",
    ),
    ids=("malformed", "not-utf8", "too-deep", "too-large"),
)
def test_an_unreadable_file_means_defaults_and_is_never_overwritten(content: bytes) -> None:
    target = paths.ui_prefs_path()
    target.parent.mkdir(parents=True)
    target.write_bytes(content)

    loaded = ui_prefs.load()

    assert loaded.prefs == UiPrefs()
    assert "won't be changed until it's fixed or removed" in loaded.read_only
    with pytest.raises(ui_prefs.UiPrefsReadOnlyError):
        ui_prefs.save(UiPrefs(frost=0.1))
    assert target.read_bytes() == content
    assert not list(target.parent.glob("ui.toml.*"))


def test_a_directory_in_place_of_the_file_is_reported_not_replaced() -> None:
    paths.ui_prefs_path().mkdir(parents=True)

    loaded = ui_prefs.load()

    assert "can't be read" in loaded.read_only
    with pytest.raises(ui_prefs.UiPrefsReadOnlyError):
        ui_prefs.save(UiPrefs())
    assert paths.ui_prefs_path().is_dir()


def test_a_failed_write_leaves_the_previous_file_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    ui_prefs.save(UiPrefs(frost=0.3))
    target = paths.ui_prefs_path()
    before = target.read_bytes()
    entries = _siblings()

    def interrupted(_source: object, _destination: object) -> None:
        raise OSError("simulated power cut before the rename")

    # Scoped: undo() would also drop conftest's XDG isolation.
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupted)
        with pytest.raises(ui_prefs.UiPrefsError, match="simulated power cut"):
            ui_prefs.update({"frost": 0.9})

    assert target.read_bytes() == before
    # No temporary is left under a public name. As for every store, the
    # shared writer's cleanup may keep it as inert evidence in the retained
    # directory, which nothing may tidy.
    left = _siblings() - entries
    assert left <= {".wall-in-one-retained"}
    assert not [name for name in _siblings() if name.startswith(".ui.toml.") and "tmp" in name]
    assert ui_prefs.load().prefs.frost == 0.3


def test_the_writer_refuses_a_document_it_could_not_read_back() -> None:
    # Tabs are legal raw in a TOML string but escaped on output, so this
    # 40 KB file would render past the 64 KiB read limit.
    target = _write('blob = "' + "\t" * 40_000 + '"\n')
    before = target.read_bytes()

    with pytest.raises(ui_prefs.UiPrefsError, match="byte limit"):
        ui_prefs.update({"frost": 0.9})
    assert target.read_bytes() == before


def test_todays_ui_never_reads_or_writes_ui_toml() -> None:
    """ui.toml is infrastructure for the new UI; nothing else may use it yet."""
    use = re.compile(
        r"from\s+wall_in_one\s+import[^\n]*\bui_prefs\b"
        r"|import\s+wall_in_one\.ui_prefs|\bui_prefs\.\w|ui_prefs_path\("
    )
    package = Path(ui_prefs.__file__).parent
    users = sorted(
        str(source.relative_to(package))
        for source in package.rglob("*.py")
        if use.search(source.read_text(encoding="utf-8"))
    )
    assert users == ["paths.py", "ui_prefs.py"]
