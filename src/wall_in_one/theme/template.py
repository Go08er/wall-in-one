"""Register our palette template with Noctalia.

Noctalia supports user-defined templates as `[theme.templates.user.<id>]` in
its `settings.toml` (`noctalia/src/config/config_types.h:1411-1423`). Because
that is a real schema field, Noctalia round-trips it through its own settings
writes rather than dropping it.

Registering one gets us push-based palette sync: Noctalia re-renders the
template on every palette change and then runs its `post_hook`, which tells the
running app to reload. The app also monitors the rendered file because a hook
without XDG_RUNTIME_DIR cannot find its socket, and because nothing checks a
failed hook's exit status.

The plugin cannot do this itself -- the Luau host API has `writeFile` and
`getConfig` but no config setter -- so it shells out to
`wall-in-one --install-theme-template`, which is this module.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import stat
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from wall_in_one import file_io, paths
from wall_in_one.theme import noctalia

TEMPLATE_ID: Final = "wall-in-one"
TEMPLATE_FILENAME: Final = "palette.json.tmpl"

#: Marker written above the block we append, so a later uninstall can find the
#: exact region it owns instead of guessing.
_BEGIN_MARKER: Final = "# >>> wall-in-one palette template (managed) >>>"
_END_MARKER: Final = "# <<< wall-in-one palette template (managed) <<<"
MAX_NOCTALIA_SETTINGS_BYTES: Final = 8 * 1024 * 1024
MAX_TEMPLATE_BYTES: Final = 1024 * 1024


class TemplateInstallError(Exception):
    """Registering or removing the template failed."""


@dataclass(frozen=True, slots=True)
class InstallResult:
    changed: bool
    settings_path: Path
    template_path: Path
    output_path: Path
    backup_path: Path | None
    detail: str


@dataclass(frozen=True, slots=True)
class _SettingsSnapshot:
    """One safely-read settings inode and its exact bytes."""

    document: bytes
    text: str
    device: int
    inode: int


def bundled_template() -> Path:
    """The template shipped alongside this package.

    Looked up relative to the installed package so it works from a Nix store
    path, an editable install, or a source checkout alike.
    """
    candidates = (
        Path(__file__).resolve().parent.parent / "data" / TEMPLATE_FILENAME,
        Path(__file__).resolve().parents[3] / "templates" / TEMPLATE_FILENAME,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(path) for path in candidates)
    raise TemplateInstallError(f"cannot find {TEMPLATE_FILENAME}; looked in: {searched}")


def installed_template_path() -> Path:
    """Where the template lives once installed.

    Deliberately a stable path we own rather than the package's own directory:
    on Nix the package path changes on every rebuild, which would leave
    Noctalia's settings pointing at a garbage-collected store path.
    """
    return paths.app_state_dir() / TEMPLATE_FILENAME


def _read_settings_snapshot(path: Path) -> _SettingsSnapshot:
    """Read one bounded regular settings inode without following a link."""

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise TemplateInstallError(
            f"Noctalia settings not found at {path}; is Noctalia installed and has it run once?"
        ) from error
    except OSError as error:
        raise TemplateInstallError(f"cannot safely read {path}: {error}") from error

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise TemplateInstallError(f"cannot safely read {path}: it is not a regular file")
        if opened.st_size > MAX_NOCTALIA_SETTINGS_BYTES:
            raise TemplateInstallError(
                f"cannot safely read {path}: it exceeds its "
                f"{MAX_NOCTALIA_SETTINGS_BYTES}-byte limit"
            )

        chunks: list[bytes] = []
        remaining = MAX_NOCTALIA_SETTINGS_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        document = b"".join(chunks)
        if len(document) > MAX_NOCTALIA_SETTINGS_BYTES:
            raise TemplateInstallError(
                f"cannot safely read {path}: it exceeds its "
                f"{MAX_NOCTALIA_SETTINGS_BYTES}-byte limit"
            )

        current = path.lstat()
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise TemplateInstallError(f"cannot safely read {path}: it changed while being read")
    except OSError as error:
        raise TemplateInstallError(f"cannot safely read {path}: {error}") from error
    finally:
        os.close(descriptor)

    try:
        text = document.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TemplateInstallError(f"{path} is not UTF-8 text") from error
    return _SettingsSnapshot(
        document=document,
        text=text,
        device=opened.st_dev,
        inode=opened.st_ino,
    )


def _read_settings_text(path: Path) -> str:
    return _read_settings_snapshot(path).text


def _read_settings(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(_read_settings_text(path))
    except (tomllib.TOMLDecodeError, RecursionError) as error:
        raise TemplateInstallError(f"{path} is not valid TOML: {error}") from error


def _existing_entry(settings: dict[str, Any]) -> dict[str, Any] | None:
    node: Any = settings
    for key in ("theme", "templates", "user", TEMPLATE_ID):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
        if node is None:
            return None
    return node if isinstance(node, dict) else None


def _toml_escape(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_block(template_path: Path, output_path: Path, post_hook: str) -> str:
    return "\n".join(
        (
            _BEGIN_MARKER,
            f"[theme.templates.user.{TEMPLATE_ID}]",
            "enabled = true",
            f"input_path = {_toml_escape(str(template_path))}",
            f"output_path = {_toml_escape(str(output_path))}",
            f"post_hook = {_toml_escape(post_hook)}",
            _END_MARKER,
        )
    )


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _backup(path: Path, snapshot: _SettingsSnapshot) -> Path:
    """Publish a durable, no-replace recovery copy of ``snapshot``."""

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.backup-stage-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(snapshot.document)
            handle.flush()
            os.fsync(handle.fileno())
        for index in range(10_000):
            suffix = "" if index == 0 else f".{index}"
            destination = path.with_name(f"{path.name}.bak-{TEMPLATE_ID}-{stamp}{suffix}")
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError:
                continue
            temporary.unlink()
            _fsync_parent(destination)
            return destination
        raise OSError("too many same-second recovery backups")
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise TemplateInstallError(f"cannot back up {path}: {error}") from error


def _assert_settings_unchanged(path: Path, expected: _SettingsSnapshot) -> None:
    try:
        current = _read_settings_snapshot(path)
    except TemplateInstallError as error:
        raise TemplateInstallError(
            f"{path} changed while the template edit was being prepared; "
            f"settings were not replaced ({error})"
        ) from error
    if (current.device, current.inode) != (
        expected.device,
        expected.inode,
    ) or current.document != expected.document:
        raise TemplateInstallError(
            f"{path} changed while the template edit was being prepared; settings were not replaced"
        )


def _write_atomically(path: Path, text: str, expected: _SettingsSnapshot) -> None:
    """Replace ``path`` without ever leaving a truncated settings file behind.

    Noctalia watches this file, so a partial write is not merely a data risk --
    it can be observed and parsed mid-update.
    """
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        # Noctalia owns and rewrites this document too. Compare both the inode
        # and exact bytes at the last possible point so one of its writes is
        # never replaced with the stale document prepared above.
        _assert_settings_unchanged(path, expected)
        os.replace(temporary, path)
        _fsync_parent(path)
    except TemplateInstallError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise TemplateInstallError(f"cannot write {path}: {error}") from error


def _write_bytes_atomically(path: Path, document: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_parent(path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise TemplateInstallError(f"cannot write {path}: {error}") from error


def _post_hook_command() -> str:
    """The command Noctalia runs after each render.

    Resolved to an absolute path when possible, because Noctalia's hook runs
    with its own environment and may not share our PATH.
    """
    found = shutil.which("wall-in-one")
    executable = found if found else "wall-in-one"
    return shlex.join((executable, "ctl", "reload-palette"))


def install(*, reload_config: bool = True) -> InstallResult:
    """Register the template, copying it to a stable location first."""
    settings_path = paths.noctalia_settings_path()
    snapshot = _read_settings_snapshot(settings_path)
    try:
        settings = tomllib.loads(snapshot.text)
    except (tomllib.TOMLDecodeError, RecursionError) as error:
        raise TemplateInstallError(f"{settings_path} is not valid TOML: {error}") from error

    source = bundled_template()
    destination = installed_template_path()
    paths.ensure_directory(destination.parent)
    output_path = paths.palette_path()

    try:
        source_document = file_io.read_regular_bytes(source, MAX_TEMPLATE_BYTES)
        destination_document = file_io.read_regular_bytes(destination, MAX_TEMPLATE_BYTES)
    except OSError as error:
        raise TemplateInstallError(f"cannot safely read palette template: {error}") from error
    if source_document is None:
        raise TemplateInstallError(f"palette template disappeared: {source}")
    template_changed = destination_document != source_document
    if template_changed:
        _write_bytes_atomically(destination, source_document)

    block = _render_block(destination, output_path, _post_hook_command())
    existing = _existing_entry(settings)

    if existing is not None:
        matches = (
            existing.get("enabled") is True
            and existing.get("input_path") == str(destination)
            and existing.get("output_path") in (str(output_path), [str(output_path)])
        )
        if matches and not template_changed:
            return InstallResult(
                changed=False,
                settings_path=settings_path,
                template_path=destination,
                output_path=output_path,
                backup_path=None,
                detail="already registered",
            )
        # Rewriting an entry we do not provably own risks clobbering a hand-
        # edited one, so leave it and say what to fix.
        if _BEGIN_MARKER not in snapshot.text:
            raise TemplateInstallError(
                f"[theme.templates.user.{TEMPLATE_ID}] already exists in {settings_path} "
                "but was not written by us; remove it by hand and re-run"
            )

    original = snapshot.text
    if _BEGIN_MARKER in original:
        updated = _replace_managed_block(original, block)
    else:
        separator = (
            "" if original.endswith("\n\n") else ("\n" if original.endswith("\n") else "\n\n")
        )
        updated = f"{original}{separator}{block}\n"

    backup = _backup(settings_path, snapshot)
    _write_atomically(settings_path, updated, snapshot)

    if reload_config:
        # Not fatal if this fails: the settings file is already correct and
        # Noctalia will pick it up on its next start. Only immediacy is lost.
        with contextlib.suppress(noctalia.NoctaliaError):
            noctalia.reload_config()

    return InstallResult(
        changed=True,
        settings_path=settings_path,
        template_path=destination,
        output_path=output_path,
        backup_path=backup,
        detail="registered" if existing is None else "updated",
    )


def _replace_managed_block(text: str, block: str) -> str:
    start = text.index(_BEGIN_MARKER)
    end_marker = text.find(_END_MARKER, start)
    if end_marker == -1:
        raise TemplateInstallError(
            "found the start of our managed block but not its end; "
            "the settings file has been edited in a way we will not guess at"
        )
    end = end_marker + len(_END_MARKER)
    return text[:start] + block + text[end:]


def uninstall(*, reload_config: bool = True) -> InstallResult:
    """Remove the block we added, leaving anything else untouched."""
    settings_path = paths.noctalia_settings_path()
    snapshot = _read_settings_snapshot(settings_path)
    original = snapshot.text

    if _BEGIN_MARKER not in original:
        return InstallResult(
            changed=False,
            settings_path=settings_path,
            template_path=installed_template_path(),
            output_path=paths.palette_path(),
            backup_path=None,
            detail="not registered",
        )

    updated = _replace_managed_block(original, "").replace("\n\n\n", "\n\n")
    backup = _backup(settings_path, snapshot)
    _write_atomically(settings_path, updated, snapshot)

    if reload_config:
        with contextlib.suppress(noctalia.NoctaliaError):
            noctalia.reload_config()

    return InstallResult(
        changed=True,
        settings_path=settings_path,
        template_path=installed_template_path(),
        output_path=paths.palette_path(),
        backup_path=backup,
        detail="removed",
    )


def status() -> str:
    """One-line summary for the CLI."""
    settings_path = paths.noctalia_settings_path()
    if not settings_path.is_file():
        return f"not installed (no Noctalia settings at {settings_path})"
    entry = _existing_entry(_read_settings(settings_path))
    if entry is None:
        return "not installed"
    output = paths.palette_path()
    rendered = "rendered" if output.is_file() else "not yet rendered"
    enabled = "enabled" if entry.get("enabled") is True else "disabled"
    return f"installed, {enabled}; palette {rendered} at {output}"
