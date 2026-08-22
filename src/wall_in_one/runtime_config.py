"""Compile the rich application model into the Rust service's strict TOML.

This is intentionally a compiler rather than another store. It reads the
library and authoring stores the Python app already owns, resolves every entry,
and atomically replaces one throw-away runtime document. The service never
imports this module and never reads those source stores.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Final

from wall_in_one import config, file_io, paths
from wall_in_one.library import pairings
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.session import Session

SCHEMA_VERSION: Final = 3
FALLBACK_PLAYLIST_ID: Final = "all-media"
FALLBACK_PLAYLIST_NAME: Final = "All media"

# Versioned Rust wire bounds. The compiler must enforce these before atomic
# installation: publishing syntactically valid TOML that the service rejects
# would stop rotation while destroying the previous working document.
MAX_RUNTIME_CONFIG_BYTES: Final = 8 * 1024 * 1024
MAX_PLAYLISTS: Final = 513
MAX_ENTRIES_PER_PLAYLIST: Final = 10_000
MAX_SCHEDULES: Final = 512
MAX_DISPLAYS: Final = 64
MAX_PLAYLIST_NAME_CHARS: Final = 120
MAX_IDENTIFIER_BYTES: Final = 256
MAX_REFERENCE_BYTES: Final = MAX_PLAYLIST_NAME_CHARS * 4
MAX_CONNECTOR_BYTES: Final = 256
MAX_OPTION_BYTES: Final = 256
MAX_PATH_BYTES: Final = 4096


class RuntimeConfigError(Exception):
    """The resolved document could not be produced or installed."""


def _encoded_length(value: str, *, label: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise RuntimeConfigError(f"{label} must be valid UTF-8 text") from error


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or 0x7F <= ord(character) <= 0x9F for character in value)


def _bounded_text(
    value: str,
    *,
    label: str,
    maximum_bytes: int,
    optional: bool = False,
) -> None:
    if optional and not value:
        return
    if not value.strip():
        raise RuntimeConfigError(f"{label} cannot be empty")
    if _encoded_length(value, label=label) > maximum_bytes:
        raise RuntimeConfigError(f"{label} must be at most {maximum_bytes} UTF-8 bytes")
    if _has_control(value):
        raise RuntimeConfigError(f"{label} cannot contain control characters")


def _bounded_name(value: str, *, label: str) -> None:
    if not value.strip():
        raise RuntimeConfigError(f"{label} cannot be empty")
    if len(value) > MAX_PLAYLIST_NAME_CHARS:
        raise RuntimeConfigError(f"{label} must be at most {MAX_PLAYLIST_NAME_CHARS} characters")
    if _has_control(value):
        raise RuntimeConfigError(f"{label} cannot contain control characters")
    _encoded_length(value, label=label)


def _absolute_path(value: Path, *, label: str) -> None:
    if not value.is_absolute():
        raise RuntimeConfigError(f"{label} must be an absolute path: {value}")
    _bounded_text(str(value), label=label, maximum_bytes=MAX_PATH_BYTES)


def _quote(value: str | Path) -> str:
    # TOML basic strings and JSON strings share the escaping needed here.
    return json.dumps(str(value), ensure_ascii=False)


def _program(name: str) -> Path:
    found = shutil.which(name)
    if found is not None:
        program = Path(found).resolve()
        _absolute_path(program, label=f"{name} program")
        return program
    # Fully resolved still means absolute when an optional renderer is absent.
    # The service reports spawn failure only when an entry actually needs it.
    program = Path("/usr/bin") / name
    _absolute_path(program, label=f"{name} program")
    return program


def _entry_id(source: Path) -> str:
    return hashlib.sha256(os.fsencode(source)).hexdigest()[:16]


def _palette(policy: pairings.PalettePolicy, generator: str) -> str:
    mode = policy.mode.value
    if policy.keeps_palette:
        return f'{{ kind = "keep", mode = {_quote(mode)} }}'
    if policy.is_adaptive:
        scheme = policy.adaptive_scheme(generator)
        _bounded_text(scheme, label="adaptive scheme", maximum_bytes=MAX_OPTION_BYTES)
        return f'{{ kind = "adaptive", scheme = {_quote(scheme)}, mode = {_quote(mode)} }}'
    if policy.kind not in ("builtin", "community", "custom") or not policy.name:
        return f'{{ kind = "keep", mode = {_quote(mode)} }}'
    _bounded_text(policy.name, label="palette name", maximum_bytes=MAX_OPTION_BYTES)
    return (
        f'{{ kind = "named", source = {_quote(policy.kind)}, '
        f"name = {_quote(policy.name)}, mode = {_quote(mode)} }}"
    )


def _resolved_entry(
    item: MediaItem,
    session: Session,
    entry_id: str,
) -> tuple[str, ...] | None:
    _bounded_text(entry_id, label="playlist entry id", maximum_bytes=MAX_IDENTIFIER_BYTES)
    bundle = session.pairings.resolve(item, session.library.roots)
    if bundle.still is None or not bundle.still.is_absolute():
        return None
    _absolute_path(bundle.still, label="playlist entry still")
    lines = [
        "[[playlists.entries]]",
        f"id = {_quote(entry_id)}",
        f"kind = {_quote(item.kind.value)}",
        f"still = {_quote(bundle.still)}",
    ]
    if item.kind is Kind.VIDEO:
        if bundle.motion is None or not bundle.motion.is_absolute():
            return None
        _absolute_path(bundle.motion, label="playlist entry motion")
        lines.append(f"motion = {_quote(bundle.motion)}")
    elif item.kind is Kind.SCENE:
        if not item.scene.isdigit():
            return None
        _bounded_text(item.scene, label="scene id", maximum_bytes=MAX_IDENTIFIER_BYTES)
        lines.append(f"scene_id = {_quote(item.scene)}")
    lines.append(f"palette = {_palette(bundle.palette, session.settings.preview_scheme)}")
    return tuple(lines)


def _validate_playlist_identity(
    compiled: list[tuple[str, str, tuple[tuple[str, ...], ...]]],
) -> None:
    """Reject references the Rust runtime could resolve to two playlists.

    Authoring ids and names are separate conveniences, but the runtime accepts
    either in schedule/default references.  Duplicate names, duplicate ids,
    or one playlist's id equalling another playlist's name would make the
    selected object depend on iteration order.  Catch that before the atomic
    install so the last-known-good runtime document remains usable.
    """
    identities: dict[str, tuple[int, str, str]] = {}
    for index, (identifier, name, _entries) in enumerate(compiled):
        _bounded_text(identifier, label="playlist id", maximum_bytes=MAX_IDENTIFIER_BYTES)
        _bounded_name(name, label=f"playlist {identifier!r} name")
        for label, value in (("id", identifier), ("name", name)):
            folded = value.casefold()
            previous = identities.get(folded)
            if previous is not None and previous[0] != index:
                _previous_index, previous_label, previous_value = previous
                raise RuntimeConfigError(
                    f"playlist {label} {value!r} conflicts with another playlist's "
                    f"{previous_label} {previous_value!r}; rename it in the app"
                )
            identities[folded] = (index, label, value)


def _validate_settings_wire(settings: config.Settings) -> None:
    """Validate every Settings value that can reach schema-3 TOML."""
    if not 5 <= settings.cycle_interval <= 24 * 60 * 60:
        raise RuntimeConfigError("cycle interval must be between 5 and 86400 seconds")
    if not 0 <= settings.video_volume <= 100:
        raise RuntimeConfigError("video volume must be between 0 and 100")
    if not 1 <= settings.scene_fps <= 240:
        raise RuntimeConfigError("scene fps must be between 1 and 240")
    if settings.video_when_hidden not in ("pause", "stop", "play"):
        raise RuntimeConfigError("video hidden policy is not supported by the runtime")
    if settings.video_interpolation not in ("off", "oversample", "linear"):
        raise RuntimeConfigError("video interpolation mode is not supported by the runtime")
    _bounded_text(
        settings.active_playlist,
        label="active playlist setting",
        maximum_bytes=MAX_REFERENCE_BYTES,
        optional=True,
    )
    _bounded_text(
        settings.output,
        label="output connector setting",
        maximum_bytes=MAX_CONNECTOR_BYTES,
        optional=True,
    )
    for index, root in enumerate(settings.roots):
        _absolute_path(root, label=f"library root {index + 1}")


def render(settings: config.Settings, session: Session) -> str:
    """Return schema-3 TOML with every authoring decision resolved."""
    _validate_settings_wire(settings)
    faults = session.authoring_faults()
    if faults:
        details = "; ".join(f"{name}: {fault}" for name, fault in faults)
        raise RuntimeConfigError(
            "cannot compile runtime configuration because authoring state is "
            f"unreadable ({details}). Repair or restore the named file; the "
            "existing runtime configuration was left untouched."
        )
    known = {item.path: item for item in session.library.items}
    playlists: list[tuple[str, str, tuple[tuple[str, ...], ...]]] = []

    fallback_items = session.library.items
    if settings.cycle_favourites_only:
        starred = tuple(item for item in fallback_items if item.path in session.favourites.paths)
        if starred:
            fallback_items = starred

    fallback_entries: list[tuple[str, ...]] = []
    for item in fallback_items:
        compiled_lines = _resolved_entry(item, session, _entry_id(item.path))
        if compiled_lines is not None:
            fallback_entries.append(compiled_lines)
    playlists.append((FALLBACK_PLAYLIST_ID, FALLBACK_PLAYLIST_NAME, tuple(fallback_entries)))

    existing_ids = {FALLBACK_PLAYLIST_ID}
    for playlist in session.playlists.all():
        resolved: list[tuple[str, ...]] = []
        for authored_entry in playlist.entries:
            found_item = known.get(authored_entry.path)
            if found_item is None:
                continue
            compiled = _resolved_entry(found_item, session, authored_entry.id)
            if compiled is not None:
                resolved.append(compiled)
        if not resolved:
            if playlist.entries:
                missing = len(playlist.entries)
                raise RuntimeConfigError(
                    f"playlist {playlist.name!r} has no playable entries; all {missing} "
                    "authored entries are missing or unresolved. Restore the media or "
                    "remove those entries; the existing runtime configuration was left "
                    "untouched."
                )
            continue
        playlists.append((playlist.id, playlist.name, tuple(resolved)))
        existing_ids.add(playlist.id)

    if len(playlists) > MAX_PLAYLISTS:
        raise RuntimeConfigError(f"no more than {MAX_PLAYLISTS} runtime playlists are supported")
    for identifier, _name, entries in playlists:
        if len(entries) > MAX_ENTRIES_PER_PLAYLIST:
            raise RuntimeConfigError(
                f"playlist {identifier!r} has more than {MAX_ENTRIES_PER_PLAYLIST} entries"
            )
    _validate_playlist_identity(playlists)

    def runtime_playlist(reference: str, *, owner: str) -> str:
        if reference in (FALLBACK_PLAYLIST_ID, FALLBACK_PLAYLIST_NAME):
            return FALLBACK_PLAYLIST_ID
        authored = session.playlists.get(reference)
        if authored is None:
            authored = session.playlists.by_name(reference)
        if authored is None:
            raise RuntimeConfigError(
                f"{owner} refers to unknown playlist {reference!r}; repair it in the app. "
                "The existing runtime configuration was left untouched."
            )
        if authored.id not in existing_ids:
            raise RuntimeConfigError(
                f"{owner} refers to playlist {authored.name!r}, which has no playable "
                "entries. Add media or choose another playlist; the existing runtime "
                "configuration was left untouched."
            )
        return authored.id

    default = (
        runtime_playlist(settings.active_playlist, owner="default playlist")
        if settings.active_playlist
        else FALLBACK_PLAYLIST_ID
    )
    lines = [
        "# Generated by Wall-in-One. Edit the library in the app, not this file.",
        f"schema_version = {SCHEMA_VERSION}",
        f"default_playlist = {_quote(default)}",
        "",
        "[settings]",
        f"cycle_interval_seconds = {settings.cycle_interval}",
        f"cycle_enabled = {str(settings.cycle_enabled).lower()}",
        f"shuffle = {str(settings.shuffle).lower()}",
        f"dynamics_enabled = {str(settings.dynamics_enabled).lower()}",
        "",
        "[renderer]",
        f"noctalia_program = {_quote(_program('noctalia'))}",
        f"niri_program = {_quote(_program('niri'))}",
        f"mpvpaper_program = {_quote(_program('mpvpaper'))}",
        f"linux_wallpaperengine_program = {_quote(_program('linux-wallpaperengine'))}",
        f"own_scene_renderer = {str(settings.own_scene_renderer).lower()}",
        'layer = "background"',
        f"video_when_hidden = {_quote(settings.video_when_hidden)}",
        f"video_hardware_decode = {str(settings.video_hardware_decode).lower()}",
        f"video_interpolation = {_quote(settings.video_interpolation)}",
        f"video_muted = {str(settings.video_muted).lower()}",
        f"video_volume = {settings.video_volume}",
        f"scene_fps = {settings.scene_fps}",
        # The exposed controls are explicitly video controls and mpv can
        # retune them live. linux-wallpaperengine only accepts audio at launch;
        # coupling it here made every slider step restart a scene while the UI
        # claimed it was changing a video. Scenes stay safely silent until they
        # receive their own deliberate controls.
        "scene_muted = true",
        "scene_volume = 0",
        "scene_pause_when_covered = true",
        'scene_scaling = ""',
        'scene_clamp = ""',
    ]

    for playlist_id, name, entries in playlists:
        lines.extend(("", "[[playlists]]", f"id = {_quote(playlist_id)}", f"name = {_quote(name)}"))
        for compiled_entry in entries:
            lines.extend(("", *compiled_entry))

    emitted_schedules = [
        (rule, runtime_playlist(rule.playlist, owner=f"schedule {rule.id!r}"))
        for rule in session.schedules.rules
    ]
    if len(emitted_schedules) > MAX_SCHEDULES:
        raise RuntimeConfigError(f"no more than {MAX_SCHEDULES} schedule rules are supported")
    schedule_ids: set[str] = set()
    for rule, playlist_id in emitted_schedules:
        _bounded_text(rule.id, label="schedule id", maximum_bytes=MAX_IDENTIFIER_BYTES)
        if rule.id in schedule_ids:
            raise RuntimeConfigError(f"duplicate schedule id {rule.id!r}")
        schedule_ids.add(rule.id)
        _bounded_text(
            playlist_id,
            label=f"schedule {rule.id!r} playlist reference",
            maximum_bytes=MAX_REFERENCE_BYTES,
        )
        lines.extend(
            ("", "[[schedules]]", f"id = {_quote(rule.id)}", f"playlist = {_quote(playlist_id)}")
        )
        if rule.months:
            lines.append(
                "months = [" + ", ".join(str(value) for value in sorted(rule.months)) + "]"
            )
        if rule.weekdays:
            lines.append(
                "weekdays = [" + ", ".join(str(value) for value in sorted(rule.weekdays)) + "]"
            )
        if rule.start is not None and rule.end is not None:
            lines.append(f'start = "{rule.start // 60:02d}:{rule.start % 60:02d}"')
            lines.append(f'end = "{rule.end // 60:02d}:{rule.end % 60:02d}"')
        lines.append(f"enabled = {str(rule.enabled).lower()}")

    assignments = session.displays.all()
    if not assignments and settings.output:
        assignments = ((settings.output, default),)
    emitted_assignments = [
        (
            connector,
            runtime_playlist(
                assigned_playlist,
                owner=f"display assignment for {connector!r}",
            ),
        )
        for connector, assigned_playlist in assignments
    ]
    if len(emitted_assignments) > MAX_DISPLAYS:
        raise RuntimeConfigError(f"no more than {MAX_DISPLAYS} display assignments are supported")
    seen_connectors: set[str] = set()
    for connector, assigned_playlist in emitted_assignments:
        _bounded_text(
            connector,
            label="display connector",
            maximum_bytes=MAX_CONNECTOR_BYTES,
        )
        if connector in seen_connectors:
            raise RuntimeConfigError(f"duplicate display connector {connector!r}")
        seen_connectors.add(connector)
        _bounded_text(
            assigned_playlist,
            label=f"display {connector!r} playlist reference",
            maximum_bytes=MAX_REFERENCE_BYTES,
        )
        lines.extend(
            (
                "",
                "[[displays]]",
                f"connector = {_quote(connector)}",
                f"playlist = {_quote(assigned_playlist)}",
            )
        )
    document = "\n".join(lines) + "\n"
    if _encoded_length(document, label="runtime configuration") > MAX_RUNTIME_CONFIG_BYTES:
        raise RuntimeConfigError(
            f"runtime configuration is larger than {MAX_RUNTIME_CONFIG_BYTES} bytes"
        )
    return document


def write(settings: config.Settings, session: Session, path: Path | None = None) -> Path:
    """Compile and install atomically, including the containing directory."""
    target = path if path is not None else paths.runtime_config_path()
    _install(render(settings, session), target)
    return target


def update(settings: config.Settings, session: Session, path: Path | None = None) -> bool:
    """Atomically install only a changed document; return whether bytes changed.

    Reloading the runtime applies its current entry.  A GUI refresh that found
    the same library must therefore not rewrite the generated file and restart
    a video or scene for no configuration change.
    """
    target = path if path is not None else paths.runtime_config_path()
    document = render(settings, session)
    try:
        current = file_io.read_regular_text(target, MAX_RUNTIME_CONFIG_BYTES)
        if current == document:
            return False
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeConfigError(f"cannot read {target}: {error}") from error
    _install(document, target)
    return True


def _install(document: str, target: Path) -> None:
    """Durably replace ``target`` with already-rendered configuration."""
    paths.ensure_directory(target.parent)
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise RuntimeConfigError(f"cannot write {target}: {error}") from error
