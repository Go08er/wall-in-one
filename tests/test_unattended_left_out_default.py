"""A default that names no playlist, where nobody watches: one rule for every compiler.

``settings.toml`` names a default playlist that doesn't exist and can't be
changed: its folder is kept read-only (permission bits, a read-only mount, a
declarative setup that owns it), or it holds keys this build doesn't know.
The app's start leaves such a default out and says so. The unattended
compiles each built a fresh `Session` that was never told, and refused:
``--write-config`` exited 1, ``--service-startup-prepare`` printed "repair it
in the app" (which the app can't) and wrote no ``runtime.toml``, so a profile
that had never published stopped its service; the health sync refused too.

They now follow the same rule (`runtime_config.unclearable_default`): the
document is built as with no default, one ``warning:`` line names the file
and the key, and the exit is a clean compile's. Where the settings can be
changed they stay strict, as ``docs/settings.md`` says: the app clears such a
default at its next start. And nothing is left out over playlists that were
not read in full.

Real permission bits throughout; each test restores them.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_runtime_health_sync import _media, _snapshot
from wall_in_one import cli, config, paths, runtime_config
from wall_in_one.control import client
from wall_in_one.control.protocol import Response
from wall_in_one.library import pairings, playlists
from wall_in_one.library.model import MediaItem

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root writes to a read-only folder")

COMPILES = ("--write-config", "--service-startup-prepare")
NO_DEFAULT = f'default_playlist = "{runtime_config.FALLBACK_PLAYLIST_ID}"'
STRICT = "default playlist refers to unknown playlist 'ghost'; repair it in the app"


@contextlib.contextmanager
def _settings_kept_read_only() -> Iterator[None]:
    """The folder ``settings.toml`` is in, read-only for as long as this lasts."""
    folder = paths.settings_path().parent
    folder.chmod(0o500)
    try:
        yield
    finally:
        folder.chmod(0o700)


def _profile(
    tmp_path: Path, default: str, *, published: bool
) -> tuple[MediaItem, playlists.Playlist]:
    """One picture, one playlist ("Lone") holding it, and ``default`` saved as the default.

    ``published``: a ``runtime.toml`` was written first, while the default
    still named nothing wrong (none at all).
    """
    item = _media(tmp_path)
    store = playlists.Store.open()
    lone = store.add(store.create("Lone").id, item.path)
    if published:
        assert cli.main(["--write-config"]) == 0
    config.update({"active_playlist": default})
    return item, lone


def _default_line() -> str:
    target = paths.runtime_config_path()
    if not target.exists():
        return "<no runtime.toml>"
    lines = target.read_text(encoding="utf-8").splitlines()
    return next(line for line in lines if line.startswith("default_playlist"))


def _warnings(err: str) -> list[str]:
    return [line for line in err.splitlines() if "building without a default playlist" in line]


@pytest.mark.parametrize("published", [True, False], ids=["published before", "never published"])
@pytest.mark.parametrize("verb", COMPILES)
def test_a_compile_builds_without_a_default_it_could_never_clear(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], verb: str, published: bool
) -> None:
    """Before: ``--write-config`` exit 1; the prestart exit 0 with "repair it in the app" and,
    never having published, no ``runtime.toml`` at all."""
    _profile(tmp_path, "ghost", published=published)
    saved = paths.settings_path().read_bytes()
    capsys.readouterr()

    with _settings_kept_read_only():
        code = cli.main([verb])
        said = capsys.readouterr()
        assert paths.settings_path().read_bytes() == saved

    assert code == 0, said.err
    (warning,) = _warnings(said.err)
    assert warning == (
        f"warning: active_playlist in {paths.settings_path()} names a playlist that "
        "doesn't exist ('ghost'), and the file can't be changed (a file can't be made in "
        f"{paths.settings_path().parent}: Permission denied); building without a default playlist"
    )
    assert "error:" not in said.err and "repair it in the app" not in said.err
    assert "could not be compiled" not in said.err, "nothing was softened: it compiled"
    assert _default_line() == NO_DEFAULT
    assert f"wrote: {paths.runtime_config_path()}" in said.out or published


def test_a_compile_leaves_out_a_default_that_unknown_keys_keep(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other way a ``settings.toml`` can't be changed: the folder is writable here."""
    _profile(tmp_path, "ghost", published=False)
    target = paths.settings_path()
    target.write_bytes(target.read_bytes() + b"from_the_future = true\n")
    saved = target.read_bytes()
    capsys.readouterr()

    code = cli.main(["--write-config"])
    said = capsys.readouterr()

    assert code == 0, said.err
    (warning,) = _warnings(said.err)
    why = config.read_only_message(["from_the_future"])
    assert warning == (
        f"warning: active_playlist in {target} names a playlist that doesn't exist "
        f"('ghost'), and the file can't be changed ({why}); building without a default playlist"
    )
    assert "error:" not in said.err
    assert _default_line() == NO_DEFAULT
    assert target.read_bytes() == saved


@pytest.mark.parametrize("verb", COMPILES)
def test_a_compile_stays_strict_where_the_settings_can_be_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    """The app clears this default at its next start, so "repair it in the app" is true."""
    _profile(tmp_path, "ghost", published=True)
    good = paths.runtime_config_path().read_bytes()
    capsys.readouterr()

    code = cli.main([verb])
    said = capsys.readouterr()

    assert f"error: {STRICT}" in said.err
    assert _warnings(said.err) == []
    assert paths.runtime_config_path().read_bytes() == good, "the last good document stays"
    if verb == "--write-config":
        assert code == 1
    else:
        assert code == 0 and "validating the last-known-good runtime" in said.err


def test_a_default_by_name_is_built_as_its_playlist_and_nothing_is_said(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _item, lone = _profile(tmp_path, "lone", published=False)
    capsys.readouterr()

    with _settings_kept_read_only():
        code = cli.main(["--write-config"])
    said = capsys.readouterr()

    assert code == 0 and said.err == "", said.err
    assert _default_line() == f'default_playlist = "{lone.id}"'


def test_the_health_sync_saves_its_finding_past_a_default_it_could_never_clear(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before: exit 1 ("default playlist refers to unknown playlist"), and no marker saved."""
    item, _lone = _profile(tmp_path, "ghost", published=True)
    saved = paths.settings_path().read_bytes()

    def send(verb: str) -> Response:
        return _snapshot(item) if verb == "status" else Response.success("reloaded")

    monkeypatch.setattr(client, "send_runtime", send)
    capsys.readouterr()

    with _settings_kept_read_only():
        code = cli.main(["--sync-runtime-health"])
        said = capsys.readouterr()
        assert paths.settings_path().read_bytes() == saved

    assert code == 0, said.err
    (warning,) = _warnings(said.err)
    assert "doesn't exist ('ghost')" in warning
    assert "error:" not in said.err
    assert pairings.Store.open().health(pairings.Identity.of(item)).is_borked
    assert _default_line() == NO_DEFAULT
    assert "renderer rejected this wallpaper" in paths.runtime_config_path().read_text("utf-8")


@pytest.mark.parametrize("playlists_file", ["from a newer build", "can't be read"])
def test_a_compile_leaves_nothing_out_over_playlists_that_were_not_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], playlists_file: str
) -> None:
    """Settings that can't be changed, a default nothing that was read answers to.

    "Couldn't look" is not "no such playlist": the compile refuses the
    playlists file by name, as it always has, and the last good
    ``runtime.toml`` stays, rather than a no-default one being published
    over it. A profile taken back to an older build is this case.
    """
    _profile(tmp_path, "ghost", published=True)
    good = paths.runtime_config_path().read_bytes()
    target = playlists.state_path()
    if playlists_file == "from a newer build":
        newer = {"version": playlists.FORMAT_VERSION + 1, "lists": [{"id": "x", "name": "X"}]}
        target.write_text(json.dumps(newer), encoding="utf-8")
    else:
        target.chmod(0o000)
    capsys.readouterr()

    try:
        with _settings_kept_read_only():
            code = cli.main(["--write-config"])
    finally:
        target.chmod(0o600)
    said = capsys.readouterr()

    assert code == 1
    assert "error:" in said.err and "playlists" in said.err
    assert _warnings(said.err) == [], "nothing was left out"
    assert paths.runtime_config_path().read_bytes() == good
