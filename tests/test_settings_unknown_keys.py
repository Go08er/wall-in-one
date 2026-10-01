"""settings.toml with keys this build does not know: read them, never rewrite.

The file has no version, so a key from a typo, a hand edit or a newer build is
indistinguishable from any other. Every reader uses the keys it knows and
reports the rest; every writer refuses rather than drop them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from wall_in_one import config, paths

UNKNOWN = b"future_setting = true\n"


def _saved_with_unknown_key(tmp_path: Path, *, extra: bytes = UNKNOWN, **changes: Any) -> bytes:
    root = tmp_path / "Library"
    root.mkdir(exist_ok=True)
    settings = replace(
        config.Settings(roots=(root,), scan_workshop=False, cycle_interval=42), **changes
    )
    target = config.save(settings)
    document = target.read_bytes() + extra
    target.write_bytes(document)
    return document


def _directory_entries() -> set[str]:
    return {entry.name for entry in paths.app_config_dir().iterdir()}


def test_loaders_use_known_keys_and_name_the_unknown_ones(tmp_path: Path) -> None:
    _saved_with_unknown_key(tmp_path, extra=b"zeta = 1\nfuture_setting = true\n")

    for loaded in (config.load_document(), config.load_strict_document()):
        assert loaded.unknown_keys == ("future_setting", "zeta")
        assert loaded.read_only
        assert loaded.settings.cycle_interval == 42
        assert loaded.settings.roots == (tmp_path / "Library",)
    assert config.load_strict().cycle_interval == 42
    assert config.load().cycle_interval == 42


def test_a_known_document_is_not_read_only(tmp_path: Path) -> None:
    _saved_with_unknown_key(tmp_path, extra=b"")

    assert config.load_document().unknown_keys == ()
    assert not config.load_strict_document().read_only


@pytest.mark.parametrize(
    ("extra", "message"),
    (
        (b"cycle_interval = 2\n", "cycle_interval must be between"),
        (b'cycle_enabled = "yes"\n', "cycle_enabled must be a boolean"),
        (b"roots = [\n", "cannot parse"),
    ),
)
def test_strict_loading_still_refuses_invalid_known_values_and_malformed_toml(
    tmp_path: Path, extra: bytes, message: str
) -> None:
    target = paths.settings_path()
    target.parent.mkdir(parents=True)
    # A duplicate known key is itself malformed TOML, so write a minimal file.
    target.write_bytes(b"future_setting = true\n" + extra)

    with pytest.raises(config.ConfigError) as caught:
        config.load_strict()
    assert not isinstance(caught.value, config.SettingsReadOnlyError)
    assert message in str(caught.value)


@pytest.mark.parametrize(
    "write",
    (
        pytest.param(lambda: config.update({"cycle_interval": 60}), id="update"),
        pytest.param(lambda: config.mutate(lambda current: current), id="mutate-unchanged"),
        pytest.param(lambda: config.save(config.Settings()), id="save"),
        pytest.param(lambda: config.forget_playlist_default("gone"), id="forget-default"),
    ),
)
def test_every_settings_writer_refuses_and_leaves_the_file_byte_identical(
    tmp_path: Path, write: Callable[[], object]
) -> None:
    before = _saved_with_unknown_key(tmp_path, active_playlist="gone")
    entries = _directory_entries()

    with pytest.raises(config.SettingsReadOnlyError) as caught:
        write()

    assert caught.value.unknown_keys == ("future_setting",)
    message = str(caught.value)
    assert "future_setting" in message
    assert "doesn't recognize" in message
    assert "changes are disabled so they aren't lost" in message
    assert paths.settings_path().read_bytes() == before
    # No temporary, backup or other sibling was left behind either.
    assert _directory_entries() == entries


def test_clearing_a_default_that_is_not_saved_needs_no_write(tmp_path: Path) -> None:
    before = _saved_with_unknown_key(tmp_path, active_playlist="kept")

    assert config.forget_playlist_default("other") is None
    assert paths.settings_path().read_bytes() == before


def test_deleting_the_saved_default_is_refused_up_front_only_when_read_only(
    tmp_path: Path,
) -> None:
    _saved_with_unknown_key(tmp_path, active_playlist="favorite")

    with pytest.raises(config.SettingsReadOnlyError):
        config.require_playlist_deletable("favorite")
    config.require_playlist_deletable("another")

    paths.settings_path().write_bytes(b'active_playlist = "favorite"\n')
    config.require_playlist_deletable("favorite")

    # Malformed settings keep their existing outcome: the deletion goes ahead
    # and clearing the default reports its own error afterwards.
    paths.settings_path().write_bytes(b"roots = [\n")
    config.require_playlist_deletable("favorite")


def test_writes_resume_once_the_unknown_key_is_gone(tmp_path: Path) -> None:
    before = _saved_with_unknown_key(tmp_path)
    paths.settings_path().write_bytes(before.replace(UNKNOWN, b""))

    assert config.update({"cycle_interval": 60}).cycle_interval == 60
    assert config.load_strict().cycle_interval == 60


def test_save_still_replaces_a_malformed_file(tmp_path: Path) -> None:
    """Only a parseable document with unknown keys is protected by save."""
    target = paths.settings_path()
    target.parent.mkdir(parents=True)
    target.write_bytes(b"roots = [\n")

    config.save(config.Settings(cycle_interval=77))

    assert config.load_strict().cycle_interval == 77


def test_key_descriptions_are_bounded_and_escaped() -> None:
    assert config.describe_keys(("plain_key-1",)) == "plain_key-1"
    assert config.describe_keys(("two words",)) == '"two words"'
    assert config.describe_keys(("line\nbreak",)) == '"line\\nbreak"'
    assert config.describe_keys(("bell\x07",)) == '"bell\\u0007"'
    assert config.describe_keys(("x" * 100,)) == "x" * 64 + "…"
    many = [f"key{index}" for index in range(8)]
    assert config.describe_keys(many) == "key0, key1, key2, key3, key4, and 3 more"
