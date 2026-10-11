"""No two palettes a person can pick share a name, and nothing writes over one.

The owner: "there shouldn't be competing palette names". So a copy is named
uniquely ("Nord copy", "Nord copy 2"), and the shared write path both windows
save through (`palettes.write_custom`) refuses a name a built-in, community or
custom palette already has, without case, instead of replacing a file. Legacy
palettes don't count: Noctalia no longer reads them, and copying one forward
under its own name is how it comes back.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path

import pytest

from wall_in_one.theme import palettes
from wall_in_one.theme.palettes import Origin, PaletteEntry, PaletteNameTakenError

_VARIANT = {key: "#203040" for key, _label in palettes.EDITABLE_KEYS}


def _document() -> dict[str, object]:
    return {"dark": dict(_VARIANT), "light": dict(_VARIANT)}


def _entry(name: str, origin: Origin) -> PaletteEntry:
    return PaletteEntry(name=name, origin=origin, path=None, colours=None)


def _community(name: str) -> Path:
    directory = palettes.community_directory()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name.replace(' ', '%20')}.json"
    path.write_text(json.dumps(_document()), encoding="utf-8")
    return path


def test_a_copy_is_numbered_until_no_palette_has_its_name() -> None:
    entries = [
        _entry("Nord", Origin.BUILTIN),
        _entry("Nord copy", Origin.CUSTOM),
        _entry("nord COPY 2", Origin.COMMUNITY),
        _entry("Nord copy 3", Origin.LEGACY),  # not pickable: free to take
    ]
    assert palettes.unique_name("Nord copy", entries) == "Nord copy 3"
    assert palettes.unique_name("Dracula copy", entries) == "Dracula copy"
    assert palettes.unique_name("a/b: c?", []) == "ab c", "only what a file name can hold"
    long = palettes.unique_name("x" * 80, [_entry("x" * 64, Origin.CUSTOM)])
    assert long == "x" * 62 + " 2" and len(long) == 64


def test_a_clash_is_any_pickable_palette_without_case_and_never_a_legacy_one() -> None:
    entries = [
        _entry("Nord", Origin.BUILTIN),
        _entry("Osaka Jade", Origin.COMMUNITY),
        _entry("Mine", Origin.CUSTOM),
        _entry("Old", Origin.LEGACY),
    ]
    assert palettes.name_clash("nord", entries) == entries[0]
    assert palettes.name_clash(" osaka jade ", entries) == entries[1]
    assert palettes.name_clash("MINE", entries) == entries[2]
    assert palettes.name_clash("Old", entries) is None
    assert palettes.name_taken_message("nord", entries[0]) == (
        "“nord” is already a built-in palette; choose another name"
    )


@pytest.mark.parametrize(
    ("taken", "origin"),
    [("Nord", "builtin"), ("Osaka Jade", "community"), ("Mine", "custom")],
)
def test_the_write_path_refuses_a_taken_name_and_writes_nothing(taken: str, origin: str) -> None:
    custom = palettes.custom_directory()
    custom.mkdir(parents=True, exist_ok=True)
    if origin == "community":
        _community(taken)
    if origin == "custom":
        palettes.write_custom(taken, _document())
    before = {path.name: path.read_bytes() for path in custom.iterdir()}

    with pytest.raises(PaletteNameTakenError, match="already"):
        palettes.write_custom(taken.upper(), _document())
    with pytest.raises(PaletteNameTakenError):
        palettes.write_custom(taken, _document())

    assert {path.name: path.read_bytes() for path in custom.iterdir()} == before


def test_a_file_that_is_no_palette_still_holds_its_name(tmp_path: Path) -> None:
    custom = palettes.custom_directory()
    custom.mkdir(parents=True, exist_ok=True)
    broken = custom / "Broken.json"
    broken.write_text("not json", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("{}", encoding="utf-8")
    (custom / "Linked.json").symlink_to(elsewhere)

    for name in ("Broken", "Linked"):
        with pytest.raises(PaletteNameTakenError):
            palettes.write_custom(name, _document())
    assert broken.read_text(encoding="utf-8") == "not json"
    assert elsewhere.read_text(encoding="utf-8") == "{}"


def test_a_duplicate_takes_a_free_name_and_editing_in_place_keeps_its_own() -> None:
    _community("Osaka Jade")
    found = palettes.discover()
    source = found.find(Origin.COMMUNITY, "Osaka Jade")
    assert source is not None
    with pytest.raises(PaletteNameTakenError):
        palettes.duplicate(source, "Osaka Jade")
    name = palettes.unique_name("Osaka Jade copy", found.entries)
    copy = palettes.duplicate(source, name)
    assert copy.name == "Osaka Jade copy"
    again = palettes.unique_name("Osaka Jade copy", palettes.discover().entries)
    assert again == "Osaka Jade copy 2"

    assert copy.path is not None
    edited = palettes.save_edits(copy, _document(), opened=palettes.read_document(copy.path))
    assert edited.path == copy.path and edited.name == copy.name


def test_the_check_reads_file_names_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """No palette document is parsed to learn the names a new one competes with."""
    _community("Osaka Jade")

    def refuse(_path: Path, _origin: Origin) -> PaletteEntry:
        raise AssertionError("a document was parsed")

    monkeypatch.setattr(palettes, "load_entry", refuse)
    entry = palettes.write_custom("Fresh", _document())
    assert entry.path is not None and os.path.isfile(entry.path)


# -- publication never replaces (Codex Q-5) ----------------------------------------------


def _outsider_during_serialisation(
    monkeypatch: pytest.MonkeyPatch, target: Path, contents: str
) -> None:
    """Another writer creates ``target`` after the name check, while the palette is rendered."""
    real = palettes._serialise

    def serialise(document: object, path: Path) -> object:
        rendered = real(document, path)  # type: ignore[arg-type]
        target.write_text(contents, encoding="utf-8")
        return rendered

    monkeypatch.setattr(palettes, "_serialise", serialise)


def test_palette_check_is_not_no_clobber(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check passes, then an independent writer creates the file: it must survive."""
    custom = palettes.custom_directory()
    custom.mkdir(parents=True, exist_ok=True)
    target = custom / "Racing.json"
    _outsider_during_serialisation(monkeypatch, target, "the outsider's palette")

    with pytest.raises(PaletteNameTakenError, match="Racing"):
        palettes.write_custom("Racing", _document())

    assert target.read_text(encoding="utf-8") == "the outsider's palette"
    assert [path.name for path in custom.iterdir()] == ["Racing.json"], "no temporary left"


def test_a_refused_new_palette_leaves_no_temporary_behind() -> None:
    custom = palettes.custom_directory()
    custom.mkdir(parents=True, exist_ok=True)
    palettes.write_custom("Mine", _document())
    before = sorted(path.name for path in custom.iterdir())
    for name in ("Mine", "MINE", "Nord"):
        with pytest.raises(PaletteNameTakenError):
            palettes.write_custom(name, _document())
    assert sorted(path.name for path in custom.iterdir()) == before == ["Mine.json"]


def test_an_in_place_edit_still_replaces_its_own_file() -> None:
    entry = palettes.write_custom("Mine", _document())
    assert entry.path is not None
    opened = palettes.read_document(entry.path)
    edited = palettes.with_overrides(opened, {"dark": {"mPrimary": "#ff0000"}})
    saved = palettes.save_edits(entry, edited, opened=opened)
    assert saved.path == entry.path
    written = palettes.read_document(entry.path)
    assert written["dark"]["mPrimary"] == "#ff0000"  # type: ignore[index]
    assert [path.name for path in entry.path.parent.iterdir()] == ["Mine.json"]


def test_without_o_tmpfile_publication_still_never_replaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filesystem that can't make an anonymous file uses the stores' RENAME_NOREPLACE."""
    real_open = os.open

    def no_tmpfile(path: object, flags: int, *rest: object, **keywords: object) -> int:
        if flags & os.O_TMPFILE == os.O_TMPFILE:
            raise OSError(errno.EOPNOTSUPP, "not on this filesystem")
        return real_open(path, flags, *rest, **keywords)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", no_tmpfile)
    custom = palettes.custom_directory()
    custom.mkdir(parents=True, exist_ok=True)
    entry = palettes.write_custom("Fresh", _document())
    assert entry.path is not None and entry.path.is_file()

    target = custom / "Racing.json"
    _outsider_during_serialisation(monkeypatch, target, "the outsider's palette")
    with pytest.raises(PaletteNameTakenError):
        palettes.write_custom("Racing", _document())
    assert target.read_text(encoding="utf-8") == "the outsider's palette"
    shown = sorted(path.name for path in custom.iterdir() if not path.name.startswith("."))
    assert shown == ["Fresh.json", "Racing.json"]
    assert not any(path.name.endswith(".tmp") for path in custom.iterdir()), "no temporary"


def test_a_new_palette_is_private_to_its_owner_as_before() -> None:
    entry = palettes.write_custom("Mine", _document())
    assert entry.path is not None
    assert entry.path.stat().st_mode & 0o777 == 0o600
    assert entry.path.stat().st_nlink == 1
