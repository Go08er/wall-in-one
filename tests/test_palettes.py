from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from wall_in_one.theme import palettes
from wall_in_one.theme.palette import Mode, PaletteError
from wall_in_one.theme.palettes import Origin, PaletteWriteError

#: The shape a real palette file has, cut down to what the parser needs. Keys,
#: nesting and casing are copied from a cached community palette rather than
#: invented, because the whole point of this module is to read those.
_VARIANT: dict[str, Any] = {
    "mPrimary": "#1E9177",
    "mOnPrimary": "#B8C8C4",
    "mSecondary": "#167A63",
    "mOnSecondary": "#B8C8C4",
    "mTertiary": "#26A589",
    "mOnTertiary": "#B8C8C4",
    "mError": "#933636",
    "mOnError": "#B8C8C4",
    "mSurface": "#081512",
    "mOnSurface": "#A6B5B1",
    "mSurfaceVariant": "#0F251F",
    "mOnSurfaceVariant": "#99A8A4",
    "mOutline": "#1B6352",
    "mShadow": "#040A09",
    "mHover": "#26A589",
    "mOnHover": "#B8C8C4",
    "terminal": {
        "foreground": "#dadada",
        "background": "#141b1e",
        "cursor": "#dadada",
        "cursorText": "#141b1e",
        "selectionFg": "#dadada",
        "selectionBg": "#141b1e",
        "normal": {
            "black": "#232a2d",
            "red": "#e57474",
            "green": "#8ccf7e",
            "yellow": "#e5c76b",
            "blue": "#67b0e8",
            "magenta": "#c47fd5",
            "cyan": "#6cbfbf",
            "white": "#b3b9b8",
        },
        "bright": {
            "black": "#464e50",
            "red": "#ef7e7e",
            "green": "#96d988",
            "yellow": "#f4d67a",
            "blue": "#71baf2",
            "magenta": "#ce89df",
            "cyan": "#67cbe7",
            "white": "#bdc3c2",
        },
    },
}


def _document(**overrides: str) -> dict[str, Any]:
    dark = dict(_VARIANT) | overrides
    return {"dark": dark, "light": dict(_VARIANT)}


def _write(path: Path, document: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def directories(tmp_path: Path) -> tuple[Path, Path]:
    """A custom and a community directory, both empty and both under tmp_path."""
    custom = tmp_path / "config" / "noctalia" / "palettes"
    community = tmp_path / "state" / "noctalia" / "community-palettes"
    custom.mkdir(parents=True)
    community.mkdir(parents=True)
    return custom, community


# -- parsing -------------------------------------------------------------


def test_file_format_maps_onto_canonical_tokens() -> None:
    pair = palettes.parse_document(json.dumps(_document()))

    # The core keys that survive the map exactly, measured against what
    # `noctalia theme --theme-json <file> --both` expands the same file into.
    assert pair.dark["primary"].hex == "#1e9177"
    assert pair.dark["surface"].hex == "#081512"
    assert pair.dark["background"].hex == "#081512"
    assert pair.dark["surface_container"].hex == "#0f251f"
    assert pair.dark["scrim"].hex == "#040a09"
    # ...and the terminal block, which maps one-for-one.
    assert pair.dark["terminal_normal_red"].hex == "#e57474"
    assert pair.dark["terminal_bright_white"].hex == "#bdc3c2"
    assert pair.dark["terminal_cursor_text"].hex == "#141b1e"


def test_derived_tokens_are_left_missing_rather_than_guessed() -> None:
    """Noctalia computes the tonal ramps; inventing them would look like a bug."""
    pair = palettes.parse_document(json.dumps(_document()))
    missing = set(pair.dark.missing_tokens)
    assert "surface_container_high" in missing
    assert "primary_container" in missing
    # `Palette.get` is what makes a partial palette usable, so check it lands.
    assert pair.dark.get("surface_container_high", "surface").hex == "#081512"


def test_a_variant_with_no_recognised_keys_is_rejected() -> None:
    with pytest.raises(PaletteError):
        palettes.parse_document(json.dumps({"dark": {"nope": "#ffffff"}, "light": _VARIANT}))


def test_a_missing_variant_is_rejected() -> None:
    with pytest.raises(PaletteError):
        palettes.parse_document(json.dumps({"dark": _VARIANT}))


# -- discovery -----------------------------------------------------------


def test_discovery_finds_all_three_sources(directories: tuple[Path, Path]) -> None:
    custom, community = directories
    _write(community / "Osaka%20jade.json", _document())
    _write(custom / "Mine.json", _document())

    found = palettes.discover(custom=custom, community=community)

    assert found.skipped == ()
    assert [entry.name for entry in found.of_origin(Origin.BUILTIN)] == list(palettes.BUILTIN_NAMES)
    # Community files are cached under a percent-encoded name; the id Noctalia
    # wants back is the decoded one.
    assert [entry.name for entry in found.of_origin(Origin.COMMUNITY)] == ["Osaka jade"]
    assert [entry.name for entry in found.of_origin(Origin.CUSTOM)] == ["Mine"]


def test_missing_directories_are_an_empty_source_not_an_error(tmp_path: Path) -> None:
    found = palettes.discover(custom=tmp_path / "nope", community=tmp_path / "also-nope")
    assert found.skipped == ()
    assert found.of_origin(Origin.COMMUNITY) == ()
    assert found.of_origin(Origin.CUSTOM) == ()
    assert len(found.of_origin(Origin.BUILTIN)) == len(palettes.BUILTIN_NAMES)


def test_builtins_are_listed_without_colours() -> None:
    """Their colours are compiled into the Noctalia binary and never on disk."""
    found = palettes.discover(custom=Path("/nonexistent"), community=Path("/nonexistent"))
    builtin = found.find(Origin.BUILTIN, "Nord")
    assert builtin is not None
    assert builtin.colours is None
    assert builtin.path is None
    assert builtin.for_mode("dark") is None
    assert not builtin.is_editable


def test_malformed_json_is_skipped_not_raised(directories: tuple[Path, Path]) -> None:
    custom, community = directories
    (custom / "Broken.json").write_text("{ not json", encoding="utf-8")
    _write(custom / "Good.json", _document())

    found = palettes.discover(custom=custom, community=community)

    assert [entry.name for entry in found.of_origin(Origin.CUSTOM)] == ["Good"]
    assert any("Broken.json" in note for note in found.skipped)


def test_a_bad_colour_is_skipped_not_raised(directories: tuple[Path, Path]) -> None:
    custom, community = directories
    _write(custom / "Bad.json", _document(mPrimary="not-a-colour"))

    found = palettes.discover(custom=custom, community=community)

    assert found.of_origin(Origin.CUSTOM) == ()
    assert any("Bad.json" in note for note in found.skipped)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_an_unreadable_file_is_skipped_not_raised(directories: tuple[Path, Path]) -> None:
    custom, community = directories
    unreadable = _write(custom / "Locked.json", _document())
    unreadable.chmod(0)
    try:
        found = palettes.discover(custom=custom, community=community)
    finally:
        unreadable.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert found.of_origin(Origin.CUSTOM) == ()
    assert any("Locked.json" in note for note in found.skipped)


def test_oversized_files_are_skipped(directories: tuple[Path, Path]) -> None:
    custom, community = directories
    padded = _document()
    padded["padding"] = "x" * (2 * 256 * 1024)
    _write(custom / "Huge.json", padded)

    found = palettes.discover(custom=custom, community=community)

    assert found.of_origin(Origin.CUSTOM) == ()
    assert any("over the" in note for note in found.skipped)


def test_dotfiles_and_the_catalog_directory_are_ignored(directories: tuple[Path, Path]) -> None:
    custom, community = directories
    _write(community / ".catalog" / "palettes.json", [{"name": "ADW"}])
    (community / ".hidden.json").write_text("{}", encoding="utf-8")
    _write(community / "Oxocarbon.json", _document())

    found = palettes.discover(custom=custom, community=community)

    assert [entry.name for entry in found.of_origin(Origin.COMMUNITY)] == ["Oxocarbon"]
    assert found.skipped == ()


def test_discovery_is_bounded(
    directories: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    custom, community = directories
    monkeypatch.setattr(palettes, "MAX_ENTRIES", 2)
    for index in range(4):
        _write(custom / f"P{index}.json", _document())

    found = palettes.discover(custom=custom, community=community)

    assert len(found.of_origin(Origin.CUSTOM)) == 2
    assert any("stopped at 2 palettes" in note for note in found.skipped)


# -- names ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "../../.config/noctalia/palettes/evil",
        "sub/dir",
        "back\\slash",
        ".hidden",
        "",
        "   ",
        "with\nnewline",
        "nul\x00byte",
        "x" * 65,
    ],
)
def test_names_that_could_escape_the_directory_are_refused(name: str) -> None:
    with pytest.raises(PaletteWriteError):
        palettes.validate_name(name)


def test_a_refused_name_never_reaches_the_filesystem(tmp_path: Path) -> None:
    custom = tmp_path / "palettes"
    custom.mkdir()
    outside = tmp_path / "escape.json"

    with pytest.raises(PaletteWriteError):
        palettes.write_custom("../escape", _document(), custom)

    assert not outside.exists()
    assert list(custom.iterdir()) == []


def test_accepted_names_stay_inside_the_directory(tmp_path: Path) -> None:
    target = palettes.custom_path("Osaka jade (mine)", tmp_path)
    assert target.parent == tmp_path
    assert target.name == "Osaka jade (mine).json"


# -- writing -------------------------------------------------------------


def test_writing_a_custom_palette_round_trips(directories: tuple[Path, Path]) -> None:
    custom, community = directories
    entry = palettes.write_custom("Mine", _document(), custom)

    assert entry.origin is Origin.CUSTOM
    assert entry.path == custom / "Mine.json"
    assert entry.is_editable
    assert palettes.discover(custom=custom, community=community).find(Origin.CUSTOM, "Mine")


def test_writing_leaves_no_temporary_behind(directories: tuple[Path, Path]) -> None:
    custom, _ = directories
    palettes.write_custom("Mine", _document(), custom)
    assert [path.name for path in custom.iterdir()] == ["Mine.json"]


def test_a_document_that_would_not_parse_is_never_written(directories: tuple[Path, Path]) -> None:
    custom, _ = directories
    with pytest.raises(PaletteError):
        palettes.write_custom("Mine", {"dark": {"mPrimary": "nonsense"}, "light": {}}, custom)
    assert list(custom.iterdir()) == []


def test_an_edit_preserves_everything_it_does_not_touch(directories: tuple[Path, Path]) -> None:
    custom, _ = directories
    entry = palettes.write_custom("Mine", _document(), custom)

    document = palettes.read_document(entry.path or Path())
    edited = palettes.with_overrides(document, {"dark": {"mPrimary": "#ff0000"}})
    palettes.save_edits(entry, edited)

    written = palettes.read_document(custom / "Mine.json")
    dark = written["dark"]
    assert isinstance(dark, dict)
    assert dark["mPrimary"] == "#ff0000"
    # The keys we do not model survive an edit untouched.
    assert dark["mHover"] == _VARIANT["mHover"]
    assert dark["terminal"] == _VARIANT["terminal"]
    assert written["light"] == _VARIANT


def test_overrides_refuse_keys_outside_the_editable_set() -> None:
    with pytest.raises(PaletteWriteError):
        palettes.with_overrides(_document(), {"dark": {"mSomethingElse": "#ffffff"}})


def test_editable_keys_are_exactly_what_overrides_accept() -> None:
    document = _document()
    overrides: dict[Mode, dict[str, str]] = {
        "dark": {key: "#123456" for key, _ in palettes.EDITABLE_KEYS}
    }
    updated = palettes.with_overrides(document, overrides)
    dark = updated["dark"]
    assert isinstance(dark, dict)
    assert all(dark[key] == "#123456" for key, _ in palettes.EDITABLE_KEYS)


# -- read-only enforcement -----------------------------------------------


def test_builtin_and_community_palettes_have_no_writable_target(
    directories: tuple[Path, Path],
) -> None:
    custom, community = directories
    _write(community / "Oxocarbon.json", _document())
    found = palettes.discover(custom=custom, community=community)

    for origin in (Origin.BUILTIN, Origin.COMMUNITY):
        entry = found.of_origin(origin)[0]
        assert not entry.origin.is_writable
        assert not entry.is_editable
        with pytest.raises(PaletteWriteError):
            palettes.target_for(entry)


def test_saving_over_a_community_palette_leaves_the_file_alone(
    directories: tuple[Path, Path],
) -> None:
    custom, community = directories
    original = _write(community / "Oxocarbon.json", _document())
    before = original.read_bytes()

    found = palettes.discover(custom=custom, community=community)
    entry = found.of_origin(Origin.COMMUNITY)[0]
    with pytest.raises(PaletteWriteError):
        palettes.save_edits(entry, _document(mPrimary="#ff0000"))

    assert original.read_bytes() == before


def test_duplicating_a_read_only_palette_is_the_way_in(
    directories: tuple[Path, Path],
) -> None:
    custom, community = directories
    original = _write(community / "Oxocarbon.json", _document())
    before = original.read_bytes()

    found = palettes.discover(custom=custom, community=community)
    entry = found.of_origin(Origin.COMMUNITY)[0]
    copy = palettes.duplicate(
        entry, "Oxocarbon mine", overrides={"dark": {"mPrimary": "#ff0000"}}, directory=custom
    )

    assert copy.origin is Origin.CUSTOM
    assert copy.path == custom / "Oxocarbon mine.json"
    assert copy.colours is not None
    assert copy.colours.dark["primary"].hex == "#ff0000"
    assert original.read_bytes() == before


def test_duplicating_a_builtin_fails_because_there_is_no_file() -> None:
    entry = palettes.PaletteEntry(name="Nord", origin=Origin.BUILTIN, path=None, colours=None)
    with pytest.raises(PaletteWriteError):
        palettes.duplicate(entry, "Nord mine")


# -- round trip through the canonical token set --------------------------


def test_a_generated_pair_can_be_saved_as_a_palette_file(
    directories: tuple[Path, Path],
) -> None:
    """The way back out: keep a scheme preview as a named palette."""
    custom, _ = directories
    pair = palettes.parse_document(json.dumps(_document()))

    document = palettes.document_from_pair(pair)
    entry = palettes.write_custom("Round trip", document, custom)

    assert entry.colours is not None
    for token in ("primary", "surface", "outline", "terminal_normal_red"):
        assert entry.colours.dark[token] == pair.dark[token]
