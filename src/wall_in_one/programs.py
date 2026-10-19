"""Where the programs Wall-in-One drives are installed: Noctalia and the renderers.

One answer for every part of the app. The compiler names these paths in
``runtime.toml`` for the service, and the window and the legacy Python
service start the same programs themselves (a scene still, a video through
the compatibility service, a palette from Noctalia) and say whether a
renderer is installed. None of them asks its own ``PATH`` alone: a window
started by a launcher, the session and the service's user manager each have
their own, and the renderers are not bundled (docs/packaging.md), so the
places every caller shares come first. Nothing here imports the rest of the
app, so any module can ask.
"""

from __future__ import annotations

import os
import pwd
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: What Settings says when the scene engine is not found: where to get it,
#: now that the package does not bring it (docs/installing.md). The full
#: missing-component notices are a later step's.
SCENE_ENGINE_MISSING: Final = (
    "linux-wallpaperengine was not found. Install it with the programs.wall-in-one "
    "Nix module (manualWallpaperInstalls left off), with nix profile install "
    "nixpkgs#linux-wallpaperengine, or from your distribution (the AUR's "
    "linux-wallpaperengine-git on Arch). See docs/installing.md."
)

#: The Nix store. A directory inside it names one immutable build, which
#: nothing keeps alive unless a profile or another root refers to it.
NIX_STORE: Final = Path("/nix/store")


def _user_name() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return ""


def _home() -> Path | None:
    """The home directory the per-user places are under, or None without a usable one.

    An empty ``HOME`` is not "no home": ``Path.home()`` turns it into ``/``,
    which would put ``/.nix-profile/bin`` and ``/.local/bin`` on the list. A
    relative one would make every per-user place depend on the caller's
    working directory. Either way the per-user places are left out.
    """
    if os.environ.get("HOME") == "":
        return None
    try:
        home = Path.home()
    except RuntimeError:  # HOME unset and no password-database entry
        return None
    return home if home.is_absolute() else None


def program_directories() -> tuple[Path, ...]:
    """Where ``runtime.toml``'s programs are looked for, in this order.

    Not simply the caller's ``PATH``: a terminal, the graphical session and
    the service's user manager each have their own, and resolving from
    whichever one compiled made the same authoring state compile to different
    bytes (0.1.4's ``shutil.which``). So the places every caller shares come
    first:

    1. The per-user and system Nix profiles, in NixOS's own login order:
       ``~/.nix-profile``, ``~/.local/state/nix/profile``,
       ``/etc/profiles/per-user/<user>``, ``/nix/var/nix/profiles/default``,
       ``/run/current-system/sw``. The NixOS and home-manager modules install
       the renderers into one of these.
    2. The FHS places: ``~/.local/bin``, ``/usr/local/bin``, ``/usr/bin``, ``/bin``.
    3. Every other absolute directory on ``PATH``, in ``PATH`` order, so a
       renderer installed somewhere unusual (``/opt/...``, ``~/bin``, a
       custom prefix) is still found. Only such an install can still compile
       differently from a terminal and from the service, when their ``PATH``
       differ there. Relative entries are skipped: they would depend on the
       caller's working directory.

    Never a Nix store directory on ``PATH``. Until 0.4.0 those came first,
    because the package's wrapper put its bundled renderers there for every
    caller. Nothing is bundled now (see docs/packaging.md), so one is a
    caller's own -- a ``nix shell``, a dev shell, a launcher's wrapper --
    which the service never sees and no garbage-collector root keeps: a
    document naming it would differ from the one the service compiles from
    the same state, and stop working at the next collection. A program has to
    be installed (in a profile, a system place, or a directory of its own on
    ``PATH``) to be found.

    Every place is absolute: the per-user ones are left out without a usable
    home (`_home`), and relative ``PATH`` entries are skipped.

    A program found nowhere keeps 0.1.4's ``/usr/bin/<name>`` (`find_program`).
    """
    home = _home()
    search = [
        Path(entry)
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and Path(entry).is_absolute()
    ]
    user = _user_name()
    profiles = [
        *([home / ".nix-profile" / "bin"] if home is not None else []),
        *([home / ".local" / "state" / "nix" / "profile" / "bin"] if home is not None else []),
        *([Path("/etc/profiles/per-user") / user / "bin"] if user else []),
        Path("/nix/var/nix/profiles/default/bin"),
        Path("/run/current-system/sw/bin"),
    ]
    system = [
        *([home / ".local" / "bin"] if home is not None else []),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
    ]
    ordered: list[Path] = []
    for directory in (*profiles, *system, *search):
        if (
            directory.is_absolute()
            and directory not in ordered
            and not directory.is_relative_to(NIX_STORE)
        ):
            ordered.append(directory)
    return tuple(ordered)


@dataclass(frozen=True, slots=True)
class Program:
    """One of ``runtime.toml``'s programs, as the compiler writes it.

    ``path`` is what the document names; ``found`` says whether an executable
    is there. A program found nowhere is still written, as ``/usr/bin/<name>``:
    the service's ``[renderer]`` needs an absolute path for each (0.3.2's
    strict loader refuses a document without one), and it reports a renderer
    only when an entry needs it and it cannot start. So ``path`` alone does not
    say whether the renderer is installed; ``found`` does.
    """

    name: str
    path: Path
    found: bool


def find_program(name: str) -> Program:
    """Where the program ``name`` is, from `program_directories`, as found.

    The path is the one in the listed directory, not resolved: a profile's
    ``bin`` is re-pointed by every profile switch, so ``~/.nix-profile/bin/x``
    keeps working (and keeps compiling to the same bytes) across switches,
    where the store path it resolves to goes with the next garbage collection.
    """
    found = shutil.which(name, path=os.pathsep.join(str(d) for d in program_directories()))
    if found is not None and Path(found).is_absolute():
        return Program(name, Path(found), found=True)
    return Program(name, Path("/usr/bin") / name, found=False)
