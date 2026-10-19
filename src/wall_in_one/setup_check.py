"""What Wall-in-One needs beside itself, and what is missing (docs/missing-components.md).

Two halves. `findings` is pure: from what was observed it makes the findings,
each saying what is missing, whether it is required, what that means and how
to fix it for this kind of install. `local`, `service_from_status` and
`ask_service` observe: the programs (`programs.find_program`), the service's
user unit, the companion plugin in Noctalia's folders, and a status reply.
None of them starts a program, asks systemd or D-Bus, or writes anything.

Every text the window's notice, Settings and ``wall-in-one --setup-status``
show is here, so the classic and the new interface say the same thing.
Nothing here imports GTK.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from wall_in_one import programs
from wall_in_one.install_kind import InstallKind
from wall_in_one.programs import Program

#: Where to get the companion plugin: its repository, whose ``wall-in-one``
#: directory is the plugin. The one place the link is written.
COMPANION_URL: Final = "https://github.com/Go08er/goober-noctalia-plugins-v5"
#: The ``id`` in the companion's ``plugin.toml``, which Noctalia enables it by.
COMPANION_ID: Final = "goober/wall-in-one"
#: How to install it from Noctalia's command line, as its README says.
COMPANION_INSTALL: Final = (
    f"noctalia msg plugins source add goober-v5 git {COMPANION_URL}, then "
    f"noctalia msg plugins enable {COMPANION_ID}"
)

MPVPAPER: Final = "mpvpaper"
SCENE_ENGINE: Final = "linux-wallpaperengine"
#: The renderers, in the order the service reports them.
RENDERERS: Final = (MPVPAPER, SCENE_ENGINE)
NOCTALIA: Final = "noctalia"
SERVICE_PROGRAM: Final = "wall-in-one-service"
SERVICE_UNIT: Final = "wall-in-one.service"
#: Every program `local` looks for.
PROGRAMS: Final = (*RENDERERS, NOCTALIA, SERVICE_PROGRAM)

#: The window's notice, shown while something required is missing.
NOTICE_TITLE: Final = "Wall-in-One is missing something it needs"
#: Settings' section.
SECTION_TITLE: Final = "Setup"
SECTION_DESCRIPTION: Final = (
    "The programs Wall-in-One works with, and how to get any that are missing. "
    "After installing one, choose Check Again."
)
ALL_PRESENT: Final = "Everything Wall-in-One needs is installed."
#: After installing something, outside the app.
AFTER_INSTALLING: Final = (
    "After installing something, run wall-in-one --write-config and "
    "wall-in-one ctl reload, or choose Check Again in the app."
)

#: How much of a ``plugin.toml`` is read, and how many are read at most.
MAX_MANIFEST_BYTES: Final = 64 * 1024
MAX_MANIFESTS: Final = 4096
#: How many entries all of Noctalia's plugin folders are listed for, at most:
#: a folder far past it is no plugin folder, and listing it would stall.
MAX_LISTED: Final = 16384

#: `--setup-status`'s exit statuses (docs/missing-components.md).
EXIT_ALL_PRESENT: Final = 0
EXIT_REQUIRED_MISSING: Final = 1
EXIT_COULD_NOT_TELL: Final = 2

#: What the classic window's bar says while only a missing renderer stops
#: motion, and the new window's Play button: the setup wording, not "Retry".
RENDERER_MISSING_STATE: Final = "Renderer missing · Settings → Setup"
RENDERER_MISSING_SUMMARY: Final = "renderer missing; paired still shown"
RENDERER_MISSING_PLAY: Final = (
    "Its renderer is missing or can't run: Settings → Setup says how to get it. Play tries again."
)

type ServiceState = Literal["running", "not-running", "not-answering", "unknown"]
type CompanionState = Literal["installed", "absent", "unknown"]


@dataclass(frozen=True, slots=True)
class MissingRenderer:
    """A renderer the service reported it could not run, where it looked, and why.

    ``reason`` is ``not-found``, ``not-executable`` or ``cannot-run`` (an
    executable that can't run here: its interpreter or a library is missing);
    a service without it said nothing more than "not found".
    """

    program: str
    path: str
    reason: str = "not-found"


@dataclass(frozen=True, slots=True)
class Service:
    """What the wallpaper service said, or that it said nothing.

    ``unknown`` is "not asked yet" (the window before its first status), and
    is never a finding.
    """

    state: ServiceState
    missing: tuple[MissingRenderer, ...] = ()
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Companion:
    """Whether the companion plugin is where Noctalia keeps plugins.

    ``where`` is the plugin's folder when it was found, or why it could not
    be told.
    """

    state: CompanionState
    where: str = ""


@dataclass(frozen=True, slots=True)
class Local:
    """What this machine has, as `local` found it."""

    programs: tuple[Program, ...]
    #: Whether ``wall-in-one.service`` is where the user manager looks; None
    #: when a place could not be looked in and it was not found elsewhere.
    unit: bool | None
    companion: Companion

    def program(self, name: str) -> Program:
        for found in self.programs:
            if found.name == name:
                return found
        return Program(name, Path("/usr/bin") / name, found=False)


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing that is missing, and how to get it."""

    #: Stable for one component: ``service``, ``noctalia``, ``renderers``,
    #: ``renderer:<name>``, ``companion``.
    key: str
    required: bool
    title: str
    detail: str
    fix: str
    #: The companion's repository when the fix involves the companion.
    link: str = ""

    @property
    def fingerprint(self) -> str:
        """Equal for the same finding said the same way; any change differs."""
        return "\0".join((self.key, self.title, self.detail, self.fix, self.link))


# -- the pure half --------------------------------------------------------------


def _renderer_fix(name: str, kind: InstallKind) -> str:
    if kind == "nix":
        return (
            f"Install it with the programs.wall-in-one Nix module (manualWallpaperInstalls "
            f"left off), or with nix profile install nixpkgs#{name}."
        )
    if kind == "system":
        package = "linux-wallpaperengine-git" if name == SCENE_ENGINE else name
        return f"Install {package} from the AUR."
    return (
        f"Install {name} from your distribution, or put it in ~/.local/bin, "
        "/usr/local/bin or /usr/bin."
    )


def _noctalia_fix(kind: InstallKind) -> str:
    if kind == "nix":
        return (
            "Install Noctalia 5 or later, for example nix profile install "
            "github:noctalia-dev/noctalia."
        )
    if kind == "system":
        return "Install the noctalia package (Noctalia 5 or later)."
    return "Install Noctalia 5 or later, from your distribution or its project."


def _service_fix(kind: InstallKind) -> str:
    if kind == "nix":
        return (
            "The wall-in-one package brings it: install it with the programs.wall-in-one "
            "Nix module, or with nix profile install github:Go08er/wall-in-one."
        )
    if kind == "system":
        return "Reinstall the wall-in-one package, which brings it and its user unit."
    return (
        "Build and install it from the service folder of the source "
        "(cargo install --path service), so that it is on PATH."
    )


_KIND_OF: Final = {MPVPAPER: "Videos", SCENE_ENGINE: "Wallpaper Engine scenes"}


def _service_findings(local: Local, service: Service, kind: InstallKind) -> list[Finding]:
    if service.state in ("running", "unknown"):
        return []
    if service.state == "not-answering":
        detail = "It may be starting or busy: nothing reached it in time."
        if service.detail:
            detail = f"{detail} ({service.detail})"
        return [
            Finding(
                "service",
                True,
                "The wallpaper service isn't answering",
                detail,
                "Check again in a moment. If it stays like this, restart it: "
                "systemctl --user restart wall-in-one.service.",
            )
        ]
    program = local.program(SERVICE_PROGRAM)
    if not program.found:
        return [
            Finding(
                "service",
                True,
                "The wallpaper service isn't installed",
                f"{SERVICE_PROGRAM} plays the wallpapers; without it nothing changes on the "
                "desktop.",
                _service_fix(kind),
            )
        ]
    installed = local.companion.state == "installed"
    if local.unit is False:
        return [
            Finding(
                "service",
                True,
                "The wallpaper service isn't running",
                f"There is no user unit for {program.path} where systemd looks, so the "
                "companion plugin is what starts it.",
                (
                    "The companion plugin is installed but hasn't started it: check that "
                    f"it is enabled (noctalia msg plugins enable {COMPANION_ID}) and that "
                    "Noctalia is running."
                )
                if installed
                else (
                    "Install the companion plugin: it starts the service directly when "
                    f"there is no unit ({COMPANION_INSTALL})."
                ),
                COMPANION_URL,
            )
        ]
    unit = (
        f"Its user unit, {SERVICE_UNIT}, is installed"
        if local.unit
        else f"Wall-in-One couldn't check whether its user unit, {SERVICE_UNIT}, is installed"
    )
    return [
        Finding(
            "service",
            True,
            "The wallpaper service isn't running",
            f"{unit}, but nothing answers.",
            f"Start it with systemctl --user enable --now {SERVICE_UNIT}, or "
            + (
                "make sure the companion plugin, which starts it, is enabled in Noctalia."
                if installed
                else "install the companion plugin, which starts it for you."
            ),
            COMPANION_URL,
        )
    ]


def _renderer_findings(
    local: Local, service: Service, kind: InstallKind, motion: bool | None
) -> list[Finding]:
    absent = [name for name in RENDERERS if not local.program(name).found]
    if len(absent) == len(RENDERERS):
        # Required only when the library has something that needs one: a
        # library of stills plays whole without either.
        return [
            Finding(
                "renderers",
                motion is True,
                "No wallpaper renderer is installed",
                "Videos and Wallpaper Engine scenes show only their stills: mpvpaper plays "
                "videos, linux-wallpaperengine plays scenes."
                + (
                    ""
                    if motion is True
                    else " Nothing in your library needs one yet, so this can wait."
                ),
                " ".join(
                    f"{name}: {_renderer_fix(name, kind)}"
                    if kind != "system"
                    else _renderer_fix(name, kind)
                    for name in RENDERERS
                ),
            )
        ]
    found: list[Finding] = []
    reported = {missing.program: missing for missing in service.missing}
    for name in RENDERERS:
        program = local.program(name)
        record = reported.get(name)
        cannot_run = record is not None and record.reason in ("cannot-run", "not-executable")
        same_path = record is not None and record.path == str(program.path)
        if cannot_run and (same_path or not program.found):
            # Installed, and the service tried it and it can't run here:
            # Check Again can't fix that, a working install can.
            assert record is not None
            found.append(
                Finding(
                    f"renderer:{name}",
                    False,
                    f"{name} is installed but can't run here",
                    (
                        f"{record.path} is not an executable file."
                        if record.reason == "not-executable"
                        else f"The wallpaper service found {record.path} but it couldn't "
                        "run: a program or library it needs is missing, or its folder "
                        "doesn't allow running programs."
                    )
                    + f" Until it can, {_KIND_OF[name].lower()} show their still.",
                    f"Run {record.path} in a terminal to see why. Reinstalling it usually "
                    f"fixes it: {_renderer_fix(name, kind)}",
                )
            )
        elif not program.found:
            found.append(
                Finding(
                    f"renderer:{name}",
                    False,
                    f"{name} isn't installed",
                    f"{_KIND_OF[name]} show their still instead.",
                    _renderer_fix(name, kind),
                )
            )
        elif record is not None:
            detail = (
                f"Its configuration names {record.path}, and {name} is at {program.path} now."
                if not same_path
                else f"It couldn't start {record.path} when it last tried, and {name} is there now."
            )
            found.append(
                Finding(
                    f"renderer:{name}",
                    False,
                    f"The wallpaper service can't start {name} yet",
                    f"{detail} Until it does, {_KIND_OF[name].lower()} show their still.",
                    "Choose Check Again, or run wall-in-one --write-config and "
                    "wall-in-one ctl reload.",
                )
            )
    return found


def _noctalia_findings(local: Local, kind: InstallKind) -> list[Finding]:
    if local.program(NOCTALIA).found:
        return []
    return [
        Finding(
            "noctalia",
            True,
            "Noctalia isn't installed",
            "Wall-in-One sets every wallpaper and palette through Noctalia 5 or later.",
            _noctalia_fix(kind),
        )
    ]


def _companion_findings(local: Local) -> list[Finding]:
    companion = local.companion
    if companion.state == "installed":
        return []
    detail = (
        "It keeps the wallpaper service running and puts Wall-in-One's controls in Noctalia's bar."
    )
    if companion.state == "absent":
        return [
            Finding(
                "companion",
                False,
                "The companion plugin wasn't found",
                f"{detail} It isn't where Noctalia installs plugins (one added as a path "
                "source can't be seen from here).",
                f"Install it in Noctalia: {COMPANION_INSTALL}.",
                COMPANION_URL,
            )
        ]
    reason = f" ({companion.where})" if companion.where else ""
    return [
        Finding(
            "companion",
            False,
            "Wall-in-One couldn't tell whether the companion plugin is installed",
            f"{detail} Noctalia's plugin folders couldn't be read{reason}.",
            f"If it isn't installed, install it in Noctalia: {COMPANION_INSTALL}.",
            COMPANION_URL,
        )
    ]


def findings(
    kind: InstallKind, local: Local, service: Service, *, motion: bool | None = None
) -> tuple[Finding, ...]:
    """What is missing, most important first: the service, Noctalia, renderers, companion.

    ``motion`` is whether the library has a video or a scene (`motion_in_library`,
    `motion_in_document`); having no renderer at all is required only then, so
    a library of stills gets no notice for it. None is "not known", which
    counts as no.
    """
    return (
        *_service_findings(local, service, kind),
        *_noctalia_findings(local, kind),
        *_renderer_findings(local, service, kind, motion),
        *_companion_findings(local),
    )


def missing_set(status: object) -> frozenset[tuple[str, str]]:
    """The renderers a status reports it could not run, as (program, path) pairs."""
    return frozenset(
        (missing.program, missing.path) for missing in service_from_status(status).missing
    )


def only_missing_renderers(last_error: str) -> bool:
    """Whether a display's ``last_error`` says nothing but that renderers are missing.

    The service's own wording (`MissingRenderer.diagnostic` there), with or
    without a connector in front, any number of times: then the window has
    said it already when `missing_renderers` is unchanged, and the bar uses
    the setup wording rather than offering Retry for a broken renderer.
    """
    rest = _MISSING_DIAGNOSTIC.sub("", last_error)
    return bool(last_error.strip()) and not rest.strip(" ;")


_MISSING_DIAGNOSTIC: Final = re.compile(
    r"(?:[^\s;:]+: )?(?:mpvpaper|linux-wallpaperengine) (?:is missing|cannot run here): "
    r".*?; paired still is active",
    re.DOTALL,
)


def required(found: Iterable[Finding]) -> tuple[Finding, ...]:
    """The findings the window's notice is for."""
    return tuple(finding for finding in found if finding.required)


def report(found: Iterable[Finding]) -> str:
    """``--setup-status``'s text: required findings first, each with its fix."""
    ordered = sorted(found, key=lambda finding: not finding.required)
    if not ordered:
        return f"ok: {ALL_PRESENT}"
    lines: list[str] = []
    for finding in ordered:
        lines.append(f"{'required' if finding.required else 'optional'}: {finding.title}.")
        lines.append(f"  {finding.detail}")
        lines.append(f"  fix: {finding.fix}")
        if finding.link:
            lines.append(f"  link: {finding.link}")
    lines.append(AFTER_INSTALLING)
    return "\n".join(lines)


# -- the observing half -----------------------------------------------------------


def _absolute(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else None


def _list(value: str | None, default: str) -> tuple[Path, ...]:
    entries = (value or default).split(os.pathsep)
    return tuple(Path(entry) for entry in entries if entry and Path(entry).is_absolute())


def user_unit_directories(environ: Mapping[str, str]) -> tuple[Path, ...]:
    """Where the user manager looks for units (systemd.unit(5)), from ``environ``.

    The per-user places are left out without an absolute ``HOME`` (or XDG
    variable), as `programs` does.
    """
    home = _absolute(environ.get("HOME"))
    config_home = _absolute(environ.get("XDG_CONFIG_HOME")) or (
        home / ".config" if home is not None else None
    )
    data_home = _absolute(environ.get("XDG_DATA_HOME")) or (
        home / ".local" / "share" if home is not None else None
    )
    runtime = _absolute(environ.get("XDG_RUNTIME_DIR"))
    unit = Path("systemd") / "user"
    ordered: list[Path] = [
        *([config_home / unit] if config_home is not None else []),
        *(directory / unit for directory in _list(environ.get("XDG_CONFIG_DIRS"), "/etc/xdg")),
        Path("/etc/systemd/user"),
        *([runtime / unit] if runtime is not None else []),
        Path("/run/systemd/user"),
        *([data_home / unit] if data_home is not None else []),
        *(
            directory / unit
            for directory in _list(environ.get("XDG_DATA_DIRS"), "/usr/local/share:/usr/share")
        ),
        Path("/usr/local/lib/systemd/user"),
        Path("/usr/lib/systemd/user"),
    ]
    return tuple(dict.fromkeys(ordered))


def unit_installed(environ: Mapping[str, str]) -> bool | None:
    """Whether ``wall-in-one.service`` is a file where the user manager looks.

    None when it is in none of the places that could be looked in, and one
    could not (a permission error, say).
    """
    uncertain = False
    for directory in user_unit_directories(environ):
        try:
            info = os.stat(directory / SERVICE_UNIT)
        except FileNotFoundError, NotADirectoryError:
            continue
        except OSError:
            uncertain = True
            continue
        if stat.S_ISREG(info.st_mode):
            return True
    return None if uncertain else False


def _noctalia_dir(
    environ: Mapping[str, str], override: str, xdg: str, fallback: tuple[str, ...]
) -> Path | None:
    """One of Noctalia 5's own directories, as it resolves it (``util/file_utils.h``).

    Its override, then the XDG variable, then the XDG default under ``HOME``,
    each followed by ``noctalia``. A relative value is Noctalia's working
    directory's business, which is not known here: None.
    """
    for variable in (override, xdg):
        value = environ.get(variable)
        if value:
            path = Path(value)
            return path / "noctalia" if path.is_absolute() else None
    home = environ.get("HOME")
    if home and Path(home).is_absolute():
        return Path(home).joinpath(*fallback, "noctalia")
    return None


class _UnreadableError(Exception):
    """A place that is there but could not be looked into."""


class _Listing:
    """What one look through Noctalia's plugin folders has listed, within `MAX_LISTED`.

    Each entry that cannot be looked at is noted, and the look carries on:
    one broken link must not hide the companion beside it.
    """

    def __init__(self) -> None:
        self.listed = 0
        self.unreadable: list[str] = []
        self.too_many = False

    def subdirectories(self, directory: Path) -> list[Path]:
        """The folders in ``directory``, symbolic links followed as Noctalia does.

        [] when it is not there, or cannot be listed (noted), or past the
        listing's bound (noted).
        """
        found: list[Path] = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    self.listed += 1
                    if self.listed > MAX_LISTED:
                        self.too_many = True
                        break
                    path = Path(entry.path)
                    try:
                        info = os.stat(path)
                    except FileNotFoundError:
                        continue  # a dangling link, or gone since the listing
                    except OSError:
                        self.unreadable.append(str(path))
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        found.append(path)
        except FileNotFoundError, NotADirectoryError:
            return []
        except OSError:
            self.unreadable.append(str(directory))
        return sorted(found)


def _manifest_id(folder: Path) -> object:
    """The ``id`` in ``folder/plugin.toml``, read in full or not at all.

    None for a folder without a manifest Noctalia could load: none, not a
    regular file (a FIFO there would block an ordinary open for ever), past
    `MAX_MANIFEST_BYTES`, or not TOML Python can read (an integer past
    Python's digit limit raises a plain ValueError). `_UnreadableError` when
    one is there but cannot be read.
    """
    manifest_path = folder / "plugin.toml"
    try:
        descriptor = os.open(
            manifest_path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
        )
    except FileNotFoundError, NotADirectoryError, IsADirectoryError:
        return None
    except OSError as error:
        raise _UnreadableError(str(manifest_path)) from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_MANIFEST_BYTES:
            chunk = os.read(descriptor, MAX_MANIFEST_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError as error:
        raise _UnreadableError(str(manifest_path)) from error
    finally:
        os.close(descriptor)
    if size > MAX_MANIFEST_BYTES:
        return None
    try:
        parsed = tomllib.loads(b"".join(chunks).decode("utf-8"))
    except ValueError, RecursionError:  # UnicodeDecodeError and TOMLDecodeError are ValueErrors
        return None
    return parsed.get("id")


def companion(environ: Mapping[str, str]) -> Companion:
    """Whether the companion is in a place Noctalia 5 keeps plugins, by its manifest's id.

    The implicit local source, ``<data>/noctalia/plugins/<folder>``, and what
    each git source exports when a plugin is enabled,
    ``<state>/noctalia/plugins/materialized/<source>/<folder>``. Only reads.
    Whether the plugin is enabled lives in Noctalia's merged configuration,
    which is not reproduced here, so it is not claimed.
    """
    data = _noctalia_dir(environ, "NOCTALIA_DATA_HOME", "XDG_DATA_HOME", (".local", "share"))
    state = _noctalia_dir(environ, "NOCTALIA_STATE_HOME", "XDG_STATE_HOME", (".local", "state"))
    if data is None or state is None:
        return Companion("unknown", "no usable home folder for Noctalia's")
    listing = _Listing()
    candidates = listing.subdirectories(data / "plugins")
    for source in listing.subdirectories(state / "plugins" / "materialized"):
        candidates.extend(listing.subdirectories(source))
    for read, folder in enumerate(candidates):
        if read >= MAX_MANIFESTS:
            listing.too_many = True
            break
        try:
            if _manifest_id(folder) == COMPANION_ID:
                return Companion("installed", str(folder))
        except _UnreadableError as error:
            listing.unreadable.append(str(error))
    if listing.too_many:
        return Companion("unknown", f"more than {MAX_LISTED} entries or {MAX_MANIFESTS} plugins")
    if listing.unreadable:
        return Companion("unknown", ", ".join(listing.unreadable))
    return Companion("absent")


def local(environ: Mapping[str, str] | None = None) -> Local:
    """What this machine has: the programs, the service's unit, the companion.

    The unit and the companion are optional checks: one that fails for any
    reason is "couldn't tell", never an error that would hide what is
    required (the programs and the service).
    """
    environ = os.environ if environ is None else environ
    try:
        unit = unit_installed(environ)
    except Exception:  # an optional check never hides a required one
        unit = None
    try:
        found = companion(environ)
    except Exception as error:  # likewise
        found = Companion("unknown", f"the check failed: {error}")
    return Local(
        programs=tuple(programs.find_program(name) for name in PROGRAMS),
        unit=unit,
        companion=found,
    )


#: Entry kinds that need a renderer.
_MOTION: Final = frozenset({"video", "scene"})


def motion_in_library(kinds: Iterable[object]) -> bool:
    """Whether any of a library's item kinds (``Kind`` or its value) needs a renderer."""
    return any(getattr(kind, "value", kind) in _MOTION for kind in kinds)


def motion_in_document(path: Path) -> bool | None:
    """Whether the compiled ``runtime.toml`` at ``path`` has a video or scene entry.

    What a terminal knows of the library without scanning it. False when
    there is no document (nothing was ever compiled, so nothing plays);
    None when it can't be read.
    """
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except OSError, ValueError, RecursionError:
        return None
    playlists = document.get("playlists")
    if not isinstance(playlists, list):
        return None
    return any(
        isinstance(entry, Mapping) and entry.get("kind") in _MOTION
        for playlist in playlists
        if isinstance(playlist, Mapping)
        for entry in (playlist.get("entries") or [])
    )


def service_from_status(status: object) -> Service:
    """A running service's status reply: what it could not run (0.4's ``missing_renderers``).

    A 0.3 service has no such key; then it is "nothing reported". Rows that
    are not what 0.4 sends are skipped; a row without a ``reason`` is
    ``not-found``.
    """
    raw = status.get("missing_renderers") if isinstance(status, Mapping) else None
    missing: list[MissingRenderer] = []
    if isinstance(raw, list):
        for row in raw[: len(RENDERERS)]:
            if not isinstance(row, Mapping):
                continue
            program, path, reason = row.get("program"), row.get("path"), row.get("reason")
            if program in RENDERERS and isinstance(path, str) and isinstance(program, str):
                missing.append(
                    MissingRenderer(
                        program,
                        path,
                        reason if reason in _REASONS and isinstance(reason, str) else "not-found",
                    )
                )
    return Service("running", tuple(missing))


_REASONS: Final = frozenset({"not-found", "not-executable", "cannot-run"})


def ask_service(timeout: float) -> Service:
    """Ask the wallpaper service for its status once, for ``--setup-status``."""
    from wall_in_one.control import client

    try:
        response = client.send_runtime("status", timeout=timeout)
    except client.NotRunningError:
        return Service("not-running")
    except client.ControlError as error:
        return Service("not-answering", detail=str(error))
    if not response.ok:
        return Service("not-answering", detail=response.message)
    try:
        status = json.loads(response.message)
    except ValueError, RecursionError:
        return Service("not-answering", detail="its status reply was not JSON")
    return service_from_status(status)
