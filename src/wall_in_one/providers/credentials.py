"""Storing the Wallhaven API key on disk, without a toolkit in the way.

The registry reads the key; this writes it. The two halves are deliberately
separate files because reading has to work during startup in any process,
while writing only ever happens from the settings dialogue -- and a dialogue
is exactly what a unit test cannot construct without a display. Nothing here
imports GTK, so the file format, the permissions, and the refusals are all
testable on their own.

A credential file that is half written is worse than no credential file at
all: the app would read a truncated key, send it, and be told its key is
invalid rather than that it has none. So the payload goes to a temporary name
in the same directory, gets its mode and its bytes, and only then takes the
real name through `os.replace`.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Final

from wall_in_one import paths
from wall_in_one.providers.base import ProviderError
from wall_in_one.providers.registry import (
    API_KEY_FILENAME,
    API_KEY_VARIABLE,
    KEY_DIRECTORY_UNSAFE_MODE,
    key_directory_fault,
)
from wall_in_one.providers.wallhaven import normalise_api_key

#: The mode the config directory is created with when we are the one creating
#: it. `paths.ensure_directory` leaves it to the umask, which is the right
#: default for settings and the wrong one for a credential: on a machine with
#: a umask of 002 it would produce a group-writable directory that `registry`
#: then refuses to read a key out of.
DIRECTORY_MODE: Final = 0o700


def key_path() -> Path:
    """Where the Wallhaven key is stored when it is not in the environment."""
    return paths.app_config_dir() / API_KEY_FILENAME


def environment_key() -> str:
    """The key the environment supplies, or ``""``. Never raises."""
    try:
        return normalise_api_key(os.environ.get(API_KEY_VARIABLE, ""))
    except ProviderError:
        return ""


def environment_key_is_malformed() -> bool:
    """Whether the variable is set to something that is not a key.

    Distinct from `environment_key` returning ``""``, which also covers the
    variable being unset. Telling a user their key is "not set" when they have
    exported a mistyped one sends them looking in the wrong place.
    """
    return bool(os.environ.get(API_KEY_VARIABLE, "").strip()) and not environment_key()


def stored_key_present() -> bool:
    """Whether a key file exists, without reading the key out of it.

    Presence is all the settings dialogue is entitled to know. Answering with
    the key itself would invite putting it in a subtitle.
    """
    path = key_path()
    try:
        return not path.is_symlink() and path.is_file()
    except OSError:
        return False


def _refuse_an_unsafe_directory(directory: Path) -> None:
    """Raise unless a credential can honestly be created in ``directory``.

    Writing 0600 into a directory another user can write to, or one a symlink
    has redirected, only looks careful: whoever can write to the directory can
    rename our file away and leave their own under the same name. Worse, the
    reader applies the same rule, so saving into such a place would report
    success and then go on running Wallhaven unauthenticated -- a lie told to
    the one person who was trying to fix the problem.

    The mode is the single fault worth mending rather than reporting. The
    directory is ours either way, some other part of the app may have created
    it first at whatever the umask allowed, and taking access away from other
    users cannot break anything. Ownership and a symlink are not ours to mend:
    both mean the directory belongs to a decision somebody else made.
    """
    if not directory.is_symlink():
        try:
            mode = stat.S_IMODE(directory.stat().st_mode)
            if mode & KEY_DIRECTORY_UNSAFE_MODE:
                directory.chmod(mode & 0o700)
        except OSError:
            # No message of its own: the check below has the final say, and it
            # says the same thing whether the mode could not be read or not
            # be changed.
            pass
    fault = key_directory_fault(directory)
    if fault:
        raise ProviderError("local-io", f"{directory} {fault}, so no key was written there")


def save_key(value: str) -> Path:
    """Validate ``value`` and write it as the stored key, atomically and 0600.

    Raises `ProviderError` with kind ``credential`` when the value could not be
    a key -- including when it is empty, since clearing is `clear_key` and
    silently storing nothing in response to a typo would be a lie.
    """
    key = normalise_api_key(value)
    if not key:
        raise ProviderError("credential", "a Wallhaven API key cannot be empty")
    destination = key_path()
    directory = destination.parent
    try:
        os.makedirs(directory, mode=DIRECTORY_MODE, exist_ok=True)
    except OSError as error:
        raise ProviderError(
            "local-io", f"could not create {directory}: {error.strerror or error}"
        ) from error
    _refuse_an_unsafe_directory(directory)

    descriptor, name = tempfile.mkstemp(prefix=".wall-in-one-tmp-", dir=directory)
    temporary = Path(name)
    try:
        # mkstemp already opens at 0600, but the mode is restated because the
        # guarantee this file makes is about the file that survives, not about
        # the library that happened to create it.
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "wb") as sink:
            sink.write((key + "\n").encode("utf-8"))
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary, destination)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise ProviderError(
            "local-io", f"could not write {destination}: {error.strerror or error}"
        ) from error
    return destination


def clear_key() -> bool:
    """Remove the stored key. True when there was one to remove."""
    path = key_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ProviderError(
            "local-io", f"could not remove {path}: {error.strerror or error}"
        ) from error
    return True
