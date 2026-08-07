"""Storing the Wallhaven key: one line, mode 0600, and never half of one.

These tests run without a display on purpose. The settings dialogue is a thin
wrapper over this module precisely so that the parts worth being careful about
-- validation, permissions, and the replace-in-one-step -- can be asserted
here rather than clicked through.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from wall_in_one.providers import credentials, registry
from wall_in_one.providers.base import ProviderError

KEY = "Abc123_key-value"
OTHER_KEY = "Zzz999_other-key"


@pytest.fixture(autouse=True)
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config home of our own, and no key in the environment."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv(registry.API_KEY_VARIABLE, raising=False)
    return tmp_path / "config" / "wall-in-one"


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def stray_files(directory: Path) -> list[Path]:
    return [entry for entry in directory.iterdir() if entry.name != registry.API_KEY_FILENAME]


# -- writing -------------------------------------------------------------


def test_saving_creates_the_config_directory_and_the_file(config_home: Path) -> None:
    written = credentials.save_key(KEY)
    assert written == config_home / registry.API_KEY_FILENAME
    assert written.read_text(encoding="utf-8") == KEY + "\n"


def test_a_saved_key_is_readable_only_by_its_owner() -> None:
    written = credentials.save_key(KEY)
    assert mode_of(written) == 0o600


def test_a_saved_key_is_what_the_registry_reads_back() -> None:
    credentials.save_key(KEY)
    assert registry.wallhaven_api_key() == KEY


def test_surrounding_whitespace_is_stripped_before_writing() -> None:
    written = credentials.save_key(f"  {KEY}\n")
    assert written.read_text(encoding="utf-8") == KEY + "\n"


def test_saving_over_an_existing_key_replaces_it() -> None:
    credentials.save_key(KEY)
    written = credentials.save_key(OTHER_KEY)
    assert written.read_text(encoding="utf-8") == OTHER_KEY + "\n"
    assert registry.wallhaven_api_key() == OTHER_KEY


def test_the_replacement_is_a_single_step(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new key must be complete and private *before* it takes the name."""
    credentials.save_key(KEY)
    destination = config_home / registry.API_KEY_FILENAME
    observed: list[tuple[str, int, str]] = []
    real_replace = os.replace

    def watch(source: object, target: object) -> None:
        staged = Path(str(source))
        observed.append(
            (
                staged.read_text(encoding="utf-8"),
                mode_of(staged),
                destination.read_text(encoding="utf-8"),
            )
        )
        assert staged.parent == destination.parent
        real_replace(source, target)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", watch)
    credentials.save_key(OTHER_KEY)
    # The old key was still intact at the moment the new one was swapped in.
    assert observed == [(OTHER_KEY + "\n", 0o600, KEY + "\n")]


def test_a_failed_write_leaves_the_old_key_and_no_debris(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credentials.save_key(KEY)

    def explode(_source: object, _target: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(ProviderError) as caught:
        credentials.save_key(OTHER_KEY)
    assert caught.value.kind == "local-io"
    assert registry.wallhaven_api_key() == KEY
    assert stray_files(config_home) == []


@pytest.mark.parametrize("bad", ["not a key", "x" * 257, "key\nsecond", "key;rm -rf /", "\t"])
def test_an_invalid_key_is_refused_rather_than_stored(config_home: Path, bad: str) -> None:
    with pytest.raises(ProviderError) as caught:
        credentials.save_key(bad)
    assert caught.value.kind == "credential"
    assert not (config_home / registry.API_KEY_FILENAME).exists()


def test_an_empty_key_is_refused_because_clearing_is_a_different_verb() -> None:
    with pytest.raises(ProviderError) as caught:
        credentials.save_key("")
    assert caught.value.kind == "credential"


def test_a_refused_key_does_not_disturb_the_stored_one(config_home: Path) -> None:
    credentials.save_key(KEY)
    with pytest.raises(ProviderError):
        credentials.save_key("not a key")
    assert registry.wallhaven_api_key() == KEY
    assert stray_files(config_home) == []


# -- clearing ------------------------------------------------------------


def test_clearing_removes_the_stored_key() -> None:
    credentials.save_key(KEY)
    assert credentials.clear_key() is True
    assert not credentials.key_path().exists()
    assert registry.wallhaven_api_key() == ""


def test_clearing_nothing_is_not_an_error() -> None:
    assert credentials.clear_key() is False


def test_clearing_twice_reports_the_second_time_as_a_no_op() -> None:
    credentials.save_key(KEY)
    assert credentials.clear_key() is True
    assert credentials.clear_key() is False


# -- reporting presence --------------------------------------------------


def test_presence_follows_the_file() -> None:
    assert credentials.stored_key_present() is False
    credentials.save_key(KEY)
    assert credentials.stored_key_present() is True
    credentials.clear_key()
    assert credentials.stored_key_present() is False


def test_presence_refuses_a_symlink_exactly_as_the_reader_does(
    config_home: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text(KEY + "\n", encoding="utf-8")
    config_home.mkdir(parents=True)
    credentials.key_path().symlink_to(elsewhere)
    assert credentials.stored_key_present() is False
    assert registry.wallhaven_api_key() == ""


def test_the_environment_key_is_reported_when_it_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(registry.API_KEY_VARIABLE, KEY)
    assert credentials.environment_key() == KEY


def test_a_malformed_environment_key_reads_as_absent_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(registry.API_KEY_VARIABLE, "not a key")
    assert credentials.environment_key() == ""


def test_a_malformed_environment_key_is_distinguishable_from_an_absent_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`environment_key` flattens both to ``""``; the dialogue needs them apart."""
    monkeypatch.setenv(registry.API_KEY_VARIABLE, "not a key")
    assert credentials.environment_key_is_malformed()


@pytest.mark.parametrize("value", ["", "   "])
def test_an_unset_or_blank_variable_is_not_called_malformed(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(registry.API_KEY_VARIABLE, value)
    assert not credentials.environment_key_is_malformed()


def test_a_usable_environment_key_is_not_called_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(registry.API_KEY_VARIABLE, KEY)
    assert not credentials.environment_key_is_malformed()


def test_a_malformed_variable_is_not_rescued_by_a_saved_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The environment is read first and stops there, so the file is not reached."""
    monkeypatch.setenv(registry.API_KEY_VARIABLE, "not a key")
    credentials.save_key(KEY)
    assert registry.usable_api_key() == (
        "",
        f"the {registry.API_KEY_VARIABLE} in your environment is not a valid key",
    )


def test_the_environment_still_wins_over_a_key_we_just_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saving cannot override the variable, and the dialogue has to say so."""
    monkeypatch.setenv(registry.API_KEY_VARIABLE, OTHER_KEY)
    credentials.save_key(KEY)
    assert registry.wallhaven_api_key() == OTHER_KEY


# -- the directory the key lands in --------------------------------------
#
# Writing 0600 into a directory somebody else can write to only looks careful:
# whoever can write the directory can rename our file away and leave their own
# under the same name. The reader applies exactly that rule, so saving into
# such a place would report success and then run unauthenticated anyway.


def test_the_config_directory_is_created_private_whatever_the_umask(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ensure_directory` leaves the mode to the umask, which is right for
    settings and wrong for a credential: 002 would produce a directory the
    reader then refuses to take a key out of."""
    previous = os.umask(0o002)
    try:
        credentials.save_key(KEY)
    finally:
        os.umask(previous)
    assert mode_of(config_home) == 0o700


def test_a_key_saved_under_a_loose_umask_is_read_back(config_home: Path) -> None:
    """The round trip is the thing that matters: save must produce something
    the reader's own defences accept, on any machine."""
    previous = os.umask(0o022)
    try:
        credentials.save_key(KEY)
    finally:
        os.umask(previous)
    assert registry.wallhaven_api_key() == KEY


def test_a_directory_others_can_write_to_is_mended_rather_than_refused(
    config_home: Path,
) -> None:
    """The directory is ours either way, and taking access away from other
    users cannot break anything -- so this fault is worth fixing silently."""
    config_home.mkdir(parents=True)
    config_home.chmod(0o777)
    try:
        credentials.save_key(KEY)
    finally:
        config_home.chmod(0o700)
    assert mode_of(config_home) == 0o700
    assert registry.wallhaven_api_key() == KEY


def test_a_symlinked_config_directory_is_refused_rather_than_written_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whose directory this really is was somebody else's decision, not ours."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    link_home = tmp_path / "linked"
    link_home.mkdir()
    (link_home / "wall-in-one").symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(link_home))
    with pytest.raises(ProviderError) as caught:
        credentials.save_key(KEY)
    assert caught.value.kind == "local-io"
    assert "symbolic link" in str(caught.value)


def test_a_refused_directory_is_left_without_debris(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing has to happen before anything is written, not after."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    link_home = tmp_path / "linked"
    link_home.mkdir()
    (link_home / "wall-in-one").symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(link_home))
    with pytest.raises(ProviderError):
        credentials.save_key(KEY)
    assert list(real.iterdir()) == []


def test_the_saved_key_never_appears_in_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    link_home = tmp_path / "linked"
    link_home.mkdir()
    (link_home / "wall-in-one").symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(link_home))
    with pytest.raises(ProviderError) as caught:
        credentials.save_key(KEY)
    assert KEY not in str(caught.value)


def test_the_writer_and_the_reader_agree_on_what_is_safe(config_home: Path) -> None:
    """One function decides, so the two halves cannot drift apart."""
    config_home.mkdir(parents=True)
    config_home.chmod(0o700)
    assert registry.key_directory_fault(config_home) == ""
    credentials.save_key(KEY)
    assert registry.wallhaven_api_key() == KEY
