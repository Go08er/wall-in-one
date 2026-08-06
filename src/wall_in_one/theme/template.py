"""Register our palette template with Noctalia.

Noctalia supports user-defined templates as `[theme.templates.user.<id>]` in
its `settings.toml` (`noctalia/src/config/config_types.h:1411-1423`). Because
that is a real schema field, Noctalia round-trips it through its own settings
writes rather than dropping it.

Registering one gets us push-based palette sync: Noctalia re-renders the
template on every palette change and then runs its `post_hook`, which tells the
running app to reload. No polling, no inotify, no drift.

The plugin cannot do this itself -- the Luau host API has `writeFile` and
`getConfig` but no config setter -- so it shells out to
`wall-in-one --install-theme-template`, which is this module.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from wall_in_one import paths
from wall_in_one.theme import noctalia

TEMPLATE_ID: Final = "wall-in-one"
TEMPLATE_FILENAME: Final = "palette.json.tmpl"

#: Marker written above the block we append, so a later uninstall can find the
#: exact region it owns instead of guessing.
_BEGIN_MARKER: Final = "# >>> wall-in-one palette template (managed) >>>"
_END_MARKER: Final = "# <<< wall-in-one palette template (managed) <<<"


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


def _read_settings(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as error:
        raise TemplateInstallError(
            f"Noctalia settings not found at {path}; is Noctalia installed and has it run once?"
        ) from error
    except OSError as error:
        raise TemplateInstallError(f"cannot read {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
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


def _backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = path.with_name(f"{path.name}.bak-{TEMPLATE_ID}-{stamp}")
    shutil.copy2(path, destination)
    return destination


def _write_atomically(path: Path, text: str) -> None:
    """Replace ``path`` without ever leaving a truncated settings file behind.

    Noctalia watches this file, so a partial write is not merely a data risk --
    it can be observed and parsed mid-update.
    """
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
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
    return f"{executable} ctl reload-palette"


def install(*, reload_config: bool = True) -> InstallResult:
    """Register the template, copying it to a stable location first."""
    settings_path = paths.noctalia_settings_path()
    settings = _read_settings(settings_path)

    source = bundled_template()
    destination = installed_template_path()
    paths.ensure_directory(destination.parent)
    output_path = paths.palette_path()

    template_changed = not destination.is_file() or destination.read_bytes() != source.read_bytes()
    if template_changed:
        shutil.copyfile(source, destination)

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
        if _BEGIN_MARKER not in settings_path.read_text(encoding="utf-8"):
            raise TemplateInstallError(
                f"[theme.templates.user.{TEMPLATE_ID}] already exists in {settings_path} "
                "but was not written by us; remove it by hand and re-run"
            )

    original = settings_path.read_text(encoding="utf-8")
    if _BEGIN_MARKER in original:
        updated = _replace_managed_block(original, block)
    else:
        separator = (
            "" if original.endswith("\n\n") else ("\n" if original.endswith("\n") else "\n\n")
        )
        updated = f"{original}{separator}{block}\n"

    backup = _backup(settings_path)
    _write_atomically(settings_path, updated)

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
    try:
        original = settings_path.read_text(encoding="utf-8")
    except OSError as error:
        raise TemplateInstallError(f"cannot read {settings_path}: {error}") from error

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
    backup = _backup(settings_path)
    _write_atomically(settings_path, updated)

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
