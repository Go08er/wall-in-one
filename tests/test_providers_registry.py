"""The registry: where the Wallhaven key comes from, and what it enables.

Two things are being pinned here. One is the resolution order -- caller, then
environment, then file -- because the settings dialogue tells the user which
source is winning and would be lying if the order drifted. The other is that
the key file is read defensively: it lives in a directory the user can edit by
hand, so every way of getting it wrong has to end in an unauthenticated
Wallhaven rather than an app that will not start.
"""

from __future__ import annotations

import os
import socket
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_providers_fakes import FakeClient
from wall_in_one.library.model import Kind
from wall_in_one.providers import registry
from wall_in_one.providers.base import ProviderError
from wall_in_one.providers.motionbgs import MotionBgs
from wall_in_one.providers.wallhaven import Wallhaven

KEY = "Abc123_key-value"
OTHER_KEY = "Zzz999_other-key"


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test in this file has any business reaching the network."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a test opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The key file's path under a config home of our own, with no key set.

    The file itself is not created; each test decides what, if anything, is
    there. Both the variable and the directory are redirected so that a
    developer with a real key in either place still runs the same suite.

    The umask is narrowed for the duration, so that a plain `write_text` lands
    at 0600 the way it would for a user whose umask is not 022. The tests below
    are about what is *in* the file; the ones about its mode set the mode
    themselves, and would be saying nothing if the default here were already a
    mode the reader rejects.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv(registry.API_KEY_VARIABLE, raising=False)
    previous = os.umask(0o077)
    try:
        directory = tmp_path / "config" / "wall-in-one"
        directory.mkdir(parents=True)
        yield directory / registry.API_KEY_FILENAME
    finally:
        os.umask(previous)


# -- resolution order ----------------------------------------------------


def test_no_key_anywhere_is_the_empty_string(key_file: Path) -> None:
    assert not key_file.exists()
    assert registry.wallhaven_api_key() == ""


def test_an_explicit_key_is_used_on_its_own(key_file: Path) -> None:
    assert not key_file.exists()
    assert registry.wallhaven_api_key(KEY) == KEY


def test_the_environment_is_used_on_its_own(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(registry.API_KEY_VARIABLE, KEY)
    assert registry.wallhaven_api_key() == KEY


def test_the_file_is_used_on_its_own(key_file: Path) -> None:
    key_file.write_text(KEY + "\n", encoding="utf-8")
    assert registry.wallhaven_api_key() == KEY


def test_an_explicit_key_beats_the_environment(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(registry.API_KEY_VARIABLE, OTHER_KEY)
    assert registry.wallhaven_api_key(KEY) == KEY


def test_an_explicit_key_beats_the_file(key_file: Path) -> None:
    key_file.write_text(OTHER_KEY + "\n", encoding="utf-8")
    assert registry.wallhaven_api_key(KEY) == KEY


def test_the_environment_beats_the_file(key_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(registry.API_KEY_VARIABLE, KEY)
    key_file.write_text(OTHER_KEY + "\n", encoding="utf-8")
    assert registry.wallhaven_api_key() == KEY


def test_an_empty_environment_variable_falls_through_to_the_file(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(registry.API_KEY_VARIABLE, "")
    key_file.write_text(KEY + "\n", encoding="utf-8")
    assert registry.wallhaven_api_key() == KEY


def test_a_malformed_key_from_the_caller_is_refused(key_file: Path) -> None:
    with pytest.raises(ProviderError) as caught:
        registry.wallhaven_api_key("not a key")
    assert caught.value.kind == "credential"


def test_a_malformed_key_in_the_environment_is_refused(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(registry.API_KEY_VARIABLE, "not a key")
    with pytest.raises(ProviderError) as caught:
        registry.wallhaven_api_key()
    assert caught.value.kind == "credential"


# -- the key file's defences ---------------------------------------------


def test_a_symlinked_key_file_is_refused(key_file: Path, tmp_path: Path) -> None:
    """The key is read with the user's privileges; a link is not followed."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text(KEY + "\n", encoding="utf-8")
    key_file.symlink_to(elsewhere)
    assert registry.wallhaven_api_key() == ""


def test_a_key_file_that_is_not_a_file_is_refused(key_file: Path) -> None:
    key_file.mkdir()
    assert registry.wallhaven_api_key() == ""


def test_an_oversized_key_file_is_refused(key_file: Path) -> None:
    padding = "x" * (registry.MAX_KEY_FILE_BYTES + 1)
    key_file.write_text(f"{KEY}\n{padding}\n", encoding="utf-8")
    assert key_file.stat().st_size > registry.MAX_KEY_FILE_BYTES
    assert registry.wallhaven_api_key() == ""


def test_a_key_file_at_the_size_ceiling_is_still_read(key_file: Path) -> None:
    filler = "x" * (registry.MAX_KEY_FILE_BYTES - len(KEY) - 2)
    key_file.write_text(f"{KEY}\n{filler}\n", encoding="utf-8")
    assert key_file.stat().st_size == registry.MAX_KEY_FILE_BYTES
    assert registry.wallhaven_api_key() == KEY


def test_a_malformed_key_file_does_not_stop_the_app_starting(key_file: Path) -> None:
    """The whole point of the file's defences: refuse the key, not the launch."""
    key_file.write_text("this is not a key at all\n", encoding="utf-8")
    assert registry.wallhaven_api_key() == ""


def test_a_key_file_that_is_not_utf8_is_refused(key_file: Path) -> None:
    key_file.write_bytes(b"\xff\xfe" + KEY.encode("utf-8"))
    assert registry.wallhaven_api_key() == ""


def test_an_empty_key_file_is_read_as_no_key(key_file: Path) -> None:
    key_file.write_text("", encoding="utf-8")
    assert registry.wallhaven_api_key() == ""


def test_a_blank_key_file_is_read_as_no_key(key_file: Path) -> None:
    key_file.write_text("   \n\n", encoding="utf-8")
    assert registry.wallhaven_api_key() == ""


def test_trailing_newlines_are_ignored(key_file: Path) -> None:
    key_file.write_text(KEY + "\n\n\n", encoding="utf-8")
    assert registry.wallhaven_api_key() == KEY


def test_a_key_file_without_a_trailing_newline_is_read(key_file: Path) -> None:
    key_file.write_text(KEY, encoding="utf-8")
    assert registry.wallhaven_api_key() == KEY


def test_surrounding_whitespace_on_the_key_line_is_trimmed(key_file: Path) -> None:
    key_file.write_text(f"  {KEY}\t\n", encoding="utf-8")
    assert registry.wallhaven_api_key() == KEY


def test_only_the_first_line_of_the_key_file_is_read(key_file: Path) -> None:
    key_file.write_text(f"{KEY}\n# a note the user left below it\n", encoding="utf-8")
    assert registry.wallhaven_api_key() == KEY


def test_a_second_line_cannot_rescue_a_malformed_first_line(key_file: Path) -> None:
    key_file.write_text(f"not a key\n{KEY}\n", encoding="utf-8")
    assert registry.wallhaven_api_key() == ""


def test_a_windows_line_ending_does_not_become_part_of_the_key(key_file: Path) -> None:
    key_file.write_bytes(f"{KEY}\r\n".encode())
    assert registry.wallhaven_api_key() == KEY


# -- describing what works -----------------------------------------------


def test_describe_reports_both_providers(key_file: Path) -> None:
    infos = registry.describe()
    assert tuple(info.name for info in infos) == registry.names()
    assert all(info.usable for info in infos)
    kinds = {info.name: info.media_kind for info in infos}
    assert kinds == {MotionBgs.name: Kind.VIDEO, Wallhaven.name: Kind.STILL}


def test_describe_reports_the_nsfw_limitation_when_there_is_no_key(key_file: Path) -> None:
    limitations = {info.name: info.limitations for info in registry.describe()}
    assert limitations[MotionBgs.name] == ()
    assert limitations[Wallhaven.name] == ("NSFW results need a Wallhaven API key",)


@pytest.mark.parametrize("source", ["explicit", "environment", "file"])
def test_any_source_of_a_key_clears_the_limitation(
    key_file: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    explicit = ""
    if source == "explicit":
        explicit = KEY
    elif source == "environment":
        monkeypatch.setenv(registry.API_KEY_VARIABLE, KEY)
    else:
        key_file.write_text(KEY + "\n", encoding="utf-8")
    limitations = {info.name: info.limitations for info in registry.describe(api_key=explicit)}
    assert limitations[Wallhaven.name] == ()


@pytest.mark.parametrize("bad", ["not a key", "x" * 512, "key\nwith\nnewlines"])
def test_describe_never_raises_on_a_malformed_explicit_key(key_file: Path, bad: str) -> None:
    """The UI calls this to draw itself, so it has to answer rather than fail."""
    limitations = {info.name: info.limitations for info in registry.describe(api_key=bad)}
    assert limitations[Wallhaven.name] == ("the Wallhaven API key supplied is not a valid key",)


def test_a_malformed_key_says_so_instead_of_blaming_the_absence_of_one(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Get a key" is unhelpful advice for someone who has one and mistyped it."""
    monkeypatch.setenv(registry.API_KEY_VARIABLE, "not a key")
    limitations = {info.name: info.limitations for info in registry.describe()}
    assert limitations[Wallhaven.name] == (
        f"the {registry.API_KEY_VARIABLE} in your environment is not a valid key",
    )
    assert registry.NSFW_NEEDS_KEY not in limitations[Wallhaven.name]


def test_describe_never_raises_when_the_config_home_is_unreadable(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: object, **_kwargs: object) -> bool:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "is_symlink", explode)
    limitations = {info.name: info.limitations for info in registry.describe()}
    assert limitations[Wallhaven.name] == ("NSFW results need a Wallhaven API key",)


# -- naming and building -------------------------------------------------


def test_names_lists_every_provider_once() -> None:
    assert registry.names() == (MotionBgs.name, Wallhaven.name)
    assert len(set(registry.names())) == len(registry.names())


@pytest.mark.parametrize(
    ("name", "expected"),
    [(MotionBgs.name, MotionBgs), (Wallhaven.name, Wallhaven)],
)
def test_each_name_builds_its_provider(key_file: Path, name: str, expected: type[object]) -> None:
    client = FakeClient()
    provider = registry.build(name, client=client)
    assert isinstance(provider, expected)
    assert provider.name == name


def test_a_built_provider_uses_the_client_it_was_given(key_file: Path) -> None:
    client = FakeClient()
    provider = registry.build(MotionBgs.name, client=client)
    assert isinstance(provider, MotionBgs)
    assert provider._client is client


def test_building_wallhaven_passes_the_resolved_key(key_file: Path) -> None:
    key_file.write_text(KEY + "\n", encoding="utf-8")
    provider = registry.build(Wallhaven.name, client=FakeClient())
    assert isinstance(provider, Wallhaven)
    assert provider.authenticated


def test_building_wallhaven_without_a_key_leaves_it_unauthenticated(key_file: Path) -> None:
    provider = registry.build(Wallhaven.name, client=FakeClient())
    assert isinstance(provider, Wallhaven)
    assert not provider.authenticated


def test_a_malformed_key_still_builds_an_unauthenticated_provider(
    key_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`describe` promised the UI that Wallhaven is usable; `build` must agree.

    Refusing here would draw a working provider and then raise every time the
    user pressed Search, which is the worst of both answers.
    """
    monkeypatch.setenv(registry.API_KEY_VARIABLE, "not a key")
    provider = registry.build(Wallhaven.name, client=FakeClient())
    assert isinstance(provider, Wallhaven)
    assert not provider.authenticated


def test_an_explicit_key_reaches_the_provider_it_is_built_with(key_file: Path) -> None:
    provider = registry.build(Wallhaven.name, client=FakeClient(), api_key=KEY)
    assert isinstance(provider, Wallhaven)
    assert provider.authenticated


@pytest.mark.parametrize("name", ["", "wallhaven ", "WALLHAVEN", "unsplash", "../etc"])
def test_an_unknown_provider_name_is_refused(key_file: Path, name: str) -> None:
    with pytest.raises(ProviderError) as caught:
        registry.build(name, client=FakeClient())
    assert caught.value.kind == "unknown-provider"


def test_build_all_returns_one_provider_per_name(key_file: Path) -> None:
    providers = registry.build_all(client=FakeClient())
    assert tuple(provider.name for provider in providers) == registry.names()


def test_build_all_shares_one_client_across_every_provider(key_file: Path) -> None:
    """One transport means one rate limiter and one connection pool, not two."""
    client = FakeClient()
    first, second = registry.build_all(client=client)
    assert isinstance(first, MotionBgs)
    assert isinstance(second, Wallhaven)
    assert first._client is client
    assert second._client is client


def test_build_all_shares_the_client_it_makes_for_itself(key_file: Path) -> None:
    first, second = registry.build_all()
    assert isinstance(first, MotionBgs)
    assert isinstance(second, Wallhaven)
    assert first._client is second._client


# -- the credential's permissions ----------------------------------------
#
# A key file lives in a directory the user edits by hand, restores from
# backups, and syncs between machines. None of those preserve a mode reliably,
# so the reader states its own requirements rather than trusting whatever it
# finds. Every refusal below has to end in an unauthenticated Wallhaven that
# says why, never in an exception: the app has to start.


@pytest.mark.parametrize(
    "mode",
    [0o640, 0o604, 0o644, 0o660, 0o606, 0o666, 0o700 | stat.S_IXGRP],
    ids=["group-read", "other-read", "world-read", "group-write", "other-write", "world", "exec"],
)
def test_a_key_reachable_by_anyone_else_is_not_used(key_file: Path, mode: int) -> None:
    key_file.write_text(KEY + "\n", encoding="utf-8")
    key_file.chmod(mode)
    assert registry.wallhaven_api_key() == ""


def test_an_exposed_key_says_which_mode_and_how_to_mend_it(key_file: Path) -> None:
    """The complaint has to be actionable: a mode and a command, not a scolding."""
    key_file.write_text(KEY + "\n", encoding="utf-8")
    key_file.chmod(0o644)
    _key, complaint = registry.usable_api_key()
    assert "0644" in complaint
    assert f"chmod 600 {key_file}" in complaint


def test_an_exposed_key_never_appears_in_the_complaint(key_file: Path) -> None:
    """Explaining that a key is too readable must not read it out loud."""
    key_file.write_text(KEY + "\n", encoding="utf-8")
    key_file.chmod(0o644)
    _key, complaint = registry.usable_api_key()
    assert KEY not in complaint


def test_a_key_only_its_owner_can_read_is_used(key_file: Path) -> None:
    key_file.write_text(KEY + "\n", encoding="utf-8")
    key_file.chmod(0o600)
    assert registry.wallhaven_api_key() == KEY


def test_a_read_only_key_is_still_used(key_file: Path) -> None:
    """0400 is a deliberate choice, not a mistake. Reading does not need write."""
    key_file.write_text(KEY + "\n", encoding="utf-8")
    key_file.chmod(0o400)
    assert registry.wallhaven_api_key() == KEY


def test_a_key_in_a_directory_others_can_write_to_is_not_used(key_file: Path) -> None:
    """Whoever can write the directory can swap the file every other check ran on."""
    key_file.write_text(KEY + "\n", encoding="utf-8")
    key_file.chmod(0o600)
    key_file.parent.chmod(0o777)
    try:
        key, complaint = registry.usable_api_key()
    finally:
        key_file.parent.chmod(0o700)
    assert key == ""
    assert "writable by other users" in complaint


def test_a_sticky_directory_others_can_write_to_is_allowed(key_file: Path) -> None:
    """The sticky bit is exactly the thing that makes /tmp-like modes safe:
    others can write, but cannot rename away a file they do not own."""
    key_file.write_text(KEY + "\n", encoding="utf-8")
    key_file.chmod(0o600)
    key_file.parent.chmod(0o1777)
    try:
        assert registry.wallhaven_api_key() == KEY
    finally:
        key_file.parent.chmod(0o700)


def test_a_symlinked_config_directory_is_not_followed(
    key_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whoever controls the link chooses which file we would read."""
    real = tmp_path / "elsewhere" / "wall-in-one"
    real.mkdir(parents=True)
    (real / registry.API_KEY_FILENAME).write_text(KEY + "\n", encoding="utf-8")
    (real / registry.API_KEY_FILENAME).chmod(0o600)
    link_home = tmp_path / "linked"
    link_home.mkdir()
    (link_home / "wall-in-one").symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(link_home))
    key, complaint = registry.usable_api_key()
    assert key == ""
    assert "symbolic link" in complaint


def test_a_directory_that_does_not_exist_yet_is_not_a_fault(key_file: Path) -> None:
    """Never having saved a key is the ordinary state, and says nothing."""
    assert registry.key_directory_fault(key_file.parent / "not-created") == ""


def test_a_missing_key_file_is_silent_rather_than_explained(key_file: Path) -> None:
    """There is no file to claim we are ignoring, so the generic limitation stands."""
    assert not key_file.exists()
    key, complaint = registry.usable_api_key()
    assert (key, complaint) == ("", "")


def test_an_exposed_key_reaches_the_ui_through_the_limitation(key_file: Path) -> None:
    """`describe` is the only channel the dialogue reads; a refusal must use it."""
    key_file.write_text(KEY + "\n", encoding="utf-8")
    key_file.chmod(0o644)
    wallhaven = next(info for info in registry.describe() if info.name == Wallhaven.name)
    assert wallhaven.usable
    assert len(wallhaven.limitations) == 1
    assert "chmod 600" in wallhaven.limitations[0]


def test_an_exposed_key_still_builds_an_unauthenticated_provider(key_file: Path) -> None:
    """The app has to start. An unusable key means no key, not no Wallhaven."""
    key_file.write_text(KEY + "\n", encoding="utf-8")
    key_file.chmod(0o644)
    provider = registry.build(Wallhaven.name, client=FakeClient())
    assert isinstance(provider, Wallhaven)
    assert not provider.authenticated


def test_an_explicit_key_is_unaffected_by_a_bad_file(key_file: Path) -> None:
    """The file is never consulted when the caller supplied a key."""
    key_file.write_text(OTHER_KEY + "\n", encoding="utf-8")
    key_file.chmod(0o666)
    assert registry.wallhaven_api_key(KEY) == KEY
