"""Rebuild/rebind proven legacy automatic stills, then retire their old files.

Startup only, never the recurring health-sync writer. The immutable adoption
manifest identifies originals; ordinary adoption/deletion authority stays strict.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from wall_in_one import config, file_io, paths, predecessor_process, runtime_config
from wall_in_one.control import client
from wall_in_one.library import (
    adopted,
    favourites,
    pairing,
    pairings,
    playlists,
    removals,
    state_file,
    stills,
)
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.providers import wallhaven
from wall_in_one.providers.base import ProviderError

LOGGER = logging.getLogger(__name__)
RUNTIME_WAIT_SECONDS = 5.0


@dataclass(frozen=True)
class Result:
    migrated: int = 0
    reclaimed_bytes: int = 0
    retained: int = 0


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _strings(key)
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _runtime_references(document: bytes) -> frozenset[str]:
    """Wait for the running daemon to adopt these exact bytes, or defer."""
    references = set(_strings(tomllib.loads(document.decode("utf-8"))))
    deadline = time.monotonic() + RUNTIME_WAIT_SECONDS
    while True:
        try:
            response = client.send_runtime("status", timeout=0.25)
        except client.NotRunningError:
            # An unbound socket alone does not prove the process is stopped.
            predecessor_process.refuse_live_predecessor_runtime()
            return frozenset(references)
        except client.ControlError as error:
            raise OSError(f"cannot confirm runtime capture references: {error}") from error
        status = json.loads(response.message) if response.ok else None
        if (
            isinstance(status, dict)
            and status.get("config_path") == str(paths.runtime_config_path().absolute())
            and status.get("loaded_config_sha256") == hashlib.sha256(document).hexdigest()
        ):
            references.update(_strings(status))
            return frozenset(references)
        if time.monotonic() >= deadline:
            raise OSError("runtime has not loaded the rebuilt configuration; cleanup deferred")
        time.sleep(0.1)


def _discard(pin: file_io.PinnedPath, generation: file_io.FileFingerprint, parent: Path) -> None:
    if not file_io.discard_regular_if_same(
        pin.path,
        expected_identity=generation[:2],
        expected_fingerprint=generation,
        pinned_source=pin,
        logical_retained_parent=parent,
    ):
        raise OSError(f"capture cleanup retained a changed file: {parent / pin.path.name}")


def _pin_owned(path: Path) -> file_io.PinnedPath:
    pin = file_io.pin_regular_path_beneath(Path(path.anchor), path).source
    status = pin.status()
    if status.st_uid != os.getuid() or status.st_nlink != 1:
        pin.close()
        raise OSError(f"capture cleanup does not own {path}")
    return pin


@contextlib.contextmanager
def _metadata(
    adoption: adopted.Adoption, entry: adopted.Authority
) -> Iterator[tuple[file_io.PinnedPath, file_io.FileFingerprint]]:
    with _pin_owned(entry.sidecar_path) as pin:
        if pin.status().st_mode & 0o022:
            raise OSError("automatic-capture metadata is writable by another user or group")
        generation = pin.fingerprint
        raw = file_io.read_pinned_regular_bytes(
            pin, adopted.MAX_SIDECAR_BYTES, expected_fingerprint=generation
        )
        # These are schema-2 deployed-adoption records, not the schema-1
        # predecessor records accepted by pairing.legacy_automatic_identity.
        if raw != adopted.render_sidecar(entry, adoption.adoption_id):
            raise OSError(f"not the original automatic-capture metadata: {entry.sidecar_path}")
        yield pin, generation


@contextlib.contextmanager
def _original(
    adoption: adopted.Adoption, entry: adopted.Authority
) -> Iterator[
    tuple[file_io.PinnedPath, file_io.FileFingerprint, file_io.PinnedPath, file_io.FileFingerprint]
]:
    with (
        _metadata(adoption, entry) as (metadata, metadata_generation),
        _pin_owned(entry.capture_path) as capture,
    ):
        generation = capture.fingerprint
        # Content proof survives a device renumber without blessing modified
        # images. Canonical adjacent metadata also has to name this exact file.
        if file_io.hash_pinned_regular(
            capture, expected_fingerprint=generation, maximum_bytes=entry.capture_size
        ) != (entry.capture_size, entry.capture_sha256):
            raise OSError(f"not the original automatic capture: {entry.capture_path}")
        if (
            metadata.fingerprint != metadata_generation
            or file_io.regular_file_fingerprint(entry.sidecar_path) != metadata_generation
            or file_io.regular_file_fingerprint(entry.capture_path) != generation
        ):
            raise OSError("automatic-capture proof changed while reading it")
        yield capture, generation, metadata, metadata_generation


def _withdraw_old_selector(entry: adopted.Authority) -> bool:
    """Remove only the exact automatic selector; never overwrite custom metadata."""
    target = entry.source_path.with_name(entry.source_path.name + pairing.SIDECAR_SUFFIX)
    try:
        pin = _pin_owned(target)
    except FileNotFoundError:
        return False
    with pin:
        generation = pin.fingerprint
        raw = file_io.read_pinned_regular_bytes(
            pin, pairing.MAX_SIDECAR_BYTES, expected_fingerprint=generation
        )
        # Reject duplicate keys and preserve unknown metadata/other choices.
        if json.loads(raw, object_pairs_hook=list) != [
            (pairing.SIDECAR_STILL_KEY, str(entry.capture_path))
        ]:
            return False
        _discard(pin, generation, target.parent)
        return True


def _validate_replacement(path: Path) -> None:
    with _pin_owned(path) as pin:
        generation = pin.fingerprint
        if not 0 < generation[2] <= adopted.MAX_CAPTURE_BYTES:
            raise OSError(f"invalid replacement size: {path}")
        width, height = wallhaven.png_dimensions(Path("/proc/self/fd") / str(pin.descriptor))
        if (
            width <= 0
            or height <= 0
            or file_io.regular_file_fingerprint(path) != generation
            or pin.fingerprint != generation
        ):
            raise OSError(f"invalid or changed replacement: {path}")


def _has_other_still(entry: adopted.Authority, target: Path) -> bool:
    """An existing separate choice makes this source's old capture redundant."""
    selected = pairing.read_sidecar(entry.source_path)
    if selected is None or selected in (entry.capture_path, target):
        return False
    # Relative paths and symlink aliases must not disguise either automatic
    # capture as a separate choice, bypassing its protection/validation.
    return not selected.samefile(entry.capture_path) and (
        not target.exists() or not selected.samefile(target)
    )


def _rebuild(adoption: adopted.Adoption, entry: adopted.Authority) -> None:
    source = MediaItem(entry.source_path, Kind.VIDEO, 0, 0)
    target = stills.destination(entry.source_path, adoption.root)
    with stills.source_lifecycle_lock(source), _original(adoption, entry):
        try:
            entry.source_path.lstat()
        except FileNotFoundError:
            return  # An orphan needs no replacement.
        if _has_other_still(entry, target):
            return  # Keep the separate choice; no automatic replacement is needed.
        if os.path.lexists(target):
            _validate_replacement(target)
        removed = _withdraw_old_selector(entry)
        selector = entry.source_path.with_name(entry.source_path.name + pairing.SIDECAR_SUFFIX)
        if not removed and os.path.lexists(selector):
            # The normal publisher cannot overwrite this choice, even if its
            # image is missing. Do not render an output it must roll back.
            return
    try:
        if removed or not os.path.lexists(target):
            # generate takes its own source lock, uses atomic publication and
            # never overwrites a different source-sidecar choice.
            stills.generate(entry.source_path, adoption.root)
        _validate_replacement(target)
    except OSError, ValueError, stills.StillError, ProviderError:
        if removed:
            try:
                with stills.source_lifecycle_lock(source):
                    # Validation can fail after generate published its selector.
                    # Withdraw only that exact auto binding, not a user's edit.
                    _withdraw_old_selector(replace(entry, capture_path=target))
                    stills.write_sidecar(entry.source_path, entry.capture_path)
            except (OSError, ValueError, stills.StillError) as error:
                LOGGER.warning("Could not restore the old still binding: %s", error)
        raise


def prepare() -> Result:
    """Best-effort startup housekeeping; caller owns the profile transaction."""
    from wall_in_one.session import Session

    try:
        adoption = adopted.recorded()
        if adoption is None or not any(
            os.path.lexists(entry.capture_path) or os.path.lexists(entry.sidecar_path)
            for entry in adoption.authorities
        ):
            return Result()
        # An existing GUI owns an accepted Library snapshot. Do not delete
        # files underneath it when a second launch or service restart arrives.
        # A stale socket is harmless: normal app startup handles its removal.
        if os.path.lexists(paths.socket_path()):
            LOGGER.info("Automatic-capture cleanup will run after the app is closed and reopened")
            return Result()
        with runtime_config.compiler_lock(), contextlib.ExitStack() as stack:
            settings = config.load_strict(require_present=True)
            if adoption.root not in settings.roots:
                return Result()
            protected: set[str] = set()
            for module in (pairings, playlists, favourites, removals):
                path = module.state_path()
                stack.enter_context(state_file.mutation_lock(path, description="capture cleanup"))
                document, fault = state_file.read_object(
                    path, maximum_bytes=module.MAX_STATE_BYTES, description="capture references"
                )
                if fault:
                    raise OSError(fault)
                protected.update(_strings(document))

            failed: set[Path] = set()
            for entry in adoption.authorities:
                if not os.path.lexists(entry.capture_path):
                    continue
                try:
                    _rebuild(adoption, entry)
                except (OSError, ValueError, stills.StillError, ProviderError) as error:
                    failed.add(entry.capture_path)
                    LOGGER.warning(
                        "Could not rebuild automatic still %s: %s", entry.capture_path, error
                    )

            session = Session(settings)
            try:
                session.refresh(mutate_removals=False)
                if session.library.skipped:
                    raise OSError("library scan was incomplete; capture cleanup deferred")
                # Include all scanned representative choices, even wallpapers
                # not currently present in any compiled playlist.
                protected.update(
                    str(item.paired_still)
                    for item in session.library.items
                    if item.paired_still is not None
                )
                runtime_config.update(settings, session)
            finally:
                session.shutdown()
            runtime_document = file_io.read_regular_bytes(
                paths.runtime_config_path(), runtime_config.MAX_RUNTIME_CONFIG_BYTES
            )
            if runtime_document is None:
                raise OSError("rebuilt runtime configuration is missing")
            protected.update(_runtime_references(runtime_document))

            migrated = reclaimed = retained = 0
            for entry in adoption.authorities:
                old = str(entry.capture_path)
                if entry.capture_path in failed or old in protected or f"still:{old}" in protected:
                    retained += 1
                    LOGGER.info(
                        "Automatic capture is still referenced or could not be rebuilt: %s", old
                    )
                    continue
                source = MediaItem(entry.source_path, Kind.VIDEO, 0, 0)
                try:
                    with stills.source_lifecycle_lock(source):
                        if not os.path.lexists(entry.capture_path):
                            if os.path.lexists(entry.sidecar_path):
                                with _metadata(adoption, entry) as (metadata, metadata_generation):
                                    if os.path.lexists(entry.capture_path):
                                        raise OSError("old capture pathname was repopulated")
                                    _discard(
                                        metadata, metadata_generation, entry.sidecar_path.parent
                                    )
                            continue
                        with _original(adoption, entry) as (
                            capture,
                            generation,
                            metadata,
                            metadata_generation,
                        ):
                            target = stills.destination(entry.source_path, adoption.root)
                            if os.path.lexists(entry.source_path):
                                selected = pairing.read_sidecar(entry.source_path)
                                if selected is not None and selected.samefile(entry.capture_path):
                                    raise OSError("the original capture is still selected")
                                if not _has_other_still(entry, target):
                                    _validate_replacement(target)
                            if (
                                metadata.fingerprint != metadata_generation
                                or file_io.regular_file_fingerprint(entry.sidecar_path)
                                != metadata_generation
                            ):
                                raise OSError("automatic-capture metadata changed before deletion")
                            _discard(capture, generation, entry.capture_path.parent)
                            migrated += 1
                            reclaimed += entry.capture_size
                            _discard(metadata, metadata_generation, entry.sidecar_path.parent)
                except (OSError, ValueError, ProviderError) as error:
                    retained += 1
                    LOGGER.warning("Could not retire automatic capture %s: %s", old, error)
            return Result(migrated, reclaimed, retained)
    except (
        OSError,
        ValueError,
        RecursionError,
        config.ConfigError,
        adopted.AdoptionError,
        runtime_config.RuntimeConfigError,
        predecessor_process.PredecessorProcessError,
    ) as error:
        LOGGER.warning("Generated-still cleanup deferred: %s", error)
        return Result()
