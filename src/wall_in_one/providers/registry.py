"""Which providers exist, and which of them can actually do anything.

"Available" is not a boolean in practice. Wallhaven works without an API key
but silently cannot reach NSFW results with one missing, and a UI that offers
the filter anyway produces an empty grid and a confused user. So a provider
reports its *limitations* alongside its usability, and the UI can grey out
precisely the controls that would not work.

That same channel carries the other bad news about a key. The Wallhaven key
may be stored in a file, and a file put there by hand -- restored from a
backup, copied out of a dotfiles repository -- can be readable by the whole
machine, owned by somebody else, or sitting in a directory anyone can write
to. None of those is a key worth sending, so the file is passed over and
Wallhaven runs unauthenticated exactly as it would with no key at all. But
silently declining to use a credential is how someone spends an evening
wondering why the key they saved does nothing, so the reason travels back as
a limitation, naming the fault and the command that fixes it.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from wall_in_one import paths
from wall_in_one.library.model import Kind
from wall_in_one.providers import http
from wall_in_one.providers.base import Provider, ProviderError
from wall_in_one.providers.motionbgs import MotionBgs
from wall_in_one.providers.wallhaven import Wallhaven, normalise_api_key

#: Read when no key is passed explicitly.
API_KEY_VARIABLE: Final = "WALLHAVEN_API_KEY"

#: Fallback to a file, so the key need not sit in the environment of every
#: process the user starts. One line, nothing else.
API_KEY_FILENAME: Final = "wallhaven-api-key"

#: A key file is small; anything larger is not one.
MAX_KEY_FILE_BYTES: Final = 4096

#: Access the key file must not grant: anything at all to group or other.
#: `credentials.save_key` writes 0600, but a file restored from a backup,
#: copied out of a dotfiles repository, or made by hand with `echo >` carries
#: whatever the umask allowed, and a credential the whole machine can read is
#: worth saying out loud rather than using quietly.
KEY_FILE_UNSAFE_MODE: Final = stat.S_IRWXG | stat.S_IRWXO

#: Access the containing directory must not grant. Read is harmless -- knowing
#: the file is there is not knowing the key -- but write lets another user
#: rename ours away and leave their own in its place, which makes every other
#: check on the file a check on a file they chose.
KEY_DIRECTORY_UNSAFE_MODE: Final = stat.S_IWGRP | stat.S_IWOTH

#: The one thing an unauthenticated Wallhaven cannot do. Named so the UI can
#: recognise this particular limitation rather than treating any limitation as
#: a missing key.
NSFW_NEEDS_KEY: Final = "NSFW results need a Wallhaven API key"


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    """What the UI needs to decide whether to show a provider at all."""

    name: str
    title: str
    media_kind: Kind
    usable: bool
    #: Empty when everything works. Each entry names something the user must
    #: supply before a particular feature is reachable.
    limitations: tuple[str, ...] = ()


def wallhaven_api_key(explicit: str = "") -> str:
    """The key from, in order: the caller, the environment, the config file."""
    key, _complaint = _resolve_key(explicit)
    return key


def _resolve_key(explicit: str = "") -> tuple[str, str]:
    """The key and the reason there is not one, in resolution order.

    Only the file can produce a complaint here: a caller or an environment
    that got the key wrong raises, which is `usable_api_key`'s to catch, while
    a file that got it wrong must never raise at all.
    """
    if explicit:
        return normalise_api_key(explicit), ""
    from_environment = os.environ.get(API_KEY_VARIABLE, "")
    if from_environment:
        return normalise_api_key(from_environment), ""
    return _key_from_file(paths.app_config_dir() / API_KEY_FILENAME)


def _complaint(fault: str, remedy: str) -> str:
    """One shape for every reason a key file on disk was passed over.

    The key itself is never a substitution here, and none of the callers has
    it to hand: a file is rejected before it is read, or on the strength of
    its first line being unusable, which is not a thing to quote back.
    """
    return f"the stored Wallhaven API key is being ignored because {fault}; {remedy}"


def key_directory_fault(directory: Path) -> str:
    """What makes ``directory`` unfit to hold a credential, or ``""``.

    Shared with `credentials.save_key`, so the half that writes and the half
    that reads cannot drift apart about what "safe enough" means. Only the
    immediate parent is examined: walking up would condemn every directory
    under a sticky, world-writable ancestor such as ``/tmp``, which is the one
    arrangement the sticky bit exists to make safe.
    """
    if directory.is_symlink():
        # Not followed, for the same reason the key file itself is not: whoever
        # controls the link chooses which file we read.
        return "is a symbolic link"
    if not directory.is_dir():
        # Nothing has been saved yet. That is the ordinary case, not a fault.
        return ""
    info = directory.stat()
    if info.st_uid != os.getuid():
        return "is owned by another user"
    mode = stat.S_IMODE(info.st_mode)
    # The sticky bit is the exemption that makes the check honest rather than
    # superstitious: with it set, other users cannot rename or remove a file
    # they do not own, which is precisely the swap being guarded against.
    if mode & KEY_DIRECTORY_UNSAFE_MODE and not mode & stat.S_ISVTX:
        return f"is writable by other users (mode {mode:04o})"
    return ""


def _key_from_file(path: Path) -> tuple[str, str]:
    """Read a key file, tolerating its absence and refusing its excesses.

    The second element says why a file that *is* there was passed over, so
    that a key silently going unused can be traced back to the thing wrong
    with it. A file that is simply absent says nothing: running without a key
    is the ordinary state, and the missing-key limitation already covers it.

    Nothing here raises. The whole point of the defences is that a config
    directory the user can edit by hand cannot stop the app from starting.
    """
    try:
        fault = key_directory_fault(path.parent)
        if fault:
            return "", _complaint(
                f"{path.parent} {fault}",
                "the key has to live in a real directory you own that others cannot write to",
            )
        if path.is_symlink():
            return "", _complaint(
                "it is a symbolic link",
                f"replace {path} with a regular file holding the key",
            )
        # A missing file lands in the OSError below, where it is silent.
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return "", _complaint(
                "it is not a regular file", f"remove {path} and save the key again"
            )
        if info.st_uid != os.getuid():
            return "", _complaint(
                "it is owned by another user", f"remove {path} and save the key again"
            )
        mode = stat.S_IMODE(info.st_mode)
        if mode & KEY_FILE_UNSAFE_MODE:
            exposure = "readable" if mode & (stat.S_IRGRP | stat.S_IROTH) else "writable"
            return "", _complaint(
                f"it is {exposure} by other users (mode {mode:04o})", f"run chmod 600 {path}"
            )
        if info.st_size > MAX_KEY_FILE_BYTES:
            return "", _complaint(
                f"it is larger than {MAX_KEY_FILE_BYTES} bytes",
                f"a key file holds one line, so remove {path} and save the key again",
            )
        raw = path.read_bytes()
    except OSError:
        # Absent, or in a directory we cannot even look into. Neither is
        # something to explain: there is no file we can claim to be ignoring.
        return "", ""
    if not raw.strip():
        # An empty file is a key that was never stored, not a broken one.
        return "", ""
    try:
        first_line = raw.decode("utf-8").splitlines()[0]
    except (UnicodeDecodeError, IndexError):
        return "", _malformed(path)
    try:
        return normalise_api_key(first_line), ""
    except ProviderError:
        # A malformed key file should not stop the app starting; Wallhaven
        # simply runs unauthenticated and says so through `limitations`.
        return "", _malformed(path)


def _malformed(path: Path) -> str:
    return _complaint(
        "its first line is not a valid key", f"correct {path}, or remove it and save the key again"
    )


def usable_api_key(explicit: str = "") -> tuple[str, str]:
    """The key to run Wallhaven with, and what is wrong with it if anything.

    A key the user fumbled is not the same situation as no key at all, and the
    difference matters: unauthenticated Wallhaven still works, so the right
    response is to carry on without the key and say why, not to refuse to build
    the provider at all. The second element is empty when nothing is wrong.
    """
    try:
        return _resolve_key(explicit)
    except ProviderError:
        if explicit:
            return "", "the Wallhaven API key supplied is not a valid key"
        return "", f"the {API_KEY_VARIABLE} in your environment is not a valid key"


def describe(*, api_key: str = "") -> tuple[ProviderInfo, ...]:
    """Every provider and what it can do right now. Never raises."""
    key, complaint = usable_api_key(api_key)
    return (
        ProviderInfo(
            name=MotionBgs.name,
            title=MotionBgs.title,
            media_kind=MotionBgs.media_kind,
            usable=True,
        ),
        ProviderInfo(
            name=Wallhaven.name,
            title=Wallhaven.title,
            media_kind=Wallhaven.media_kind,
            usable=True,
            limitations=(complaint or NSFW_NEEDS_KEY,) if not key else (),
        ),
    )


def names() -> tuple[str, ...]:
    return (MotionBgs.name, Wallhaven.name)


def build(name: str, *, client: http.Client | None = None, api_key: str = "") -> Provider:
    """Construct one provider by name, sharing ``client`` if one is given."""
    if name == MotionBgs.name:
        return MotionBgs(client)
    if name == Wallhaven.name:
        # Deliberately the forgiving resolution: `describe` has already told
        # the UI that Wallhaven is usable, so a malformed key in the
        # environment must not turn every attempt to build it into an
        # exception. It runs unauthenticated, and `describe` says why.
        key, _complaint = usable_api_key(api_key)
        return Wallhaven(client, api_key=key)
    raise ProviderError("unknown-provider", f"no such provider: {name!r}")


def build_all(*, client: http.Client | None = None, api_key: str = "") -> tuple[Provider, ...]:
    """Every provider, over one shared transport."""
    shared = client if client is not None else http.UrllibClient()
    return tuple(build(name, client=shared, api_key=api_key) for name in names())
