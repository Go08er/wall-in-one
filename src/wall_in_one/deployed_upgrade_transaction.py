"""Crash-resumable publication for the supported deployed-app upgrade.

The read-only evidence gate lives in :mod:`wall_in_one.deployed_upgrade`.
This module is the only write half.  It publishes an exact journal first,
adds generation-bound authority beside the already-existing captures, makes
the predecessor's Noctalia root explicit in current settings, and compiles a
private schema-4 document.  The public schema-2 runtime is not claimed until
that private document has been re-rendered and validated under every authoring
lock.  A completion marker is the final durable publication.

No source media, capture bytes, Noctalia setting, or Workshop path is moved or
modified.  Reserved transaction targets are always no-replace publications;
the two intentional replacements (settings and runtime) first atomically
claim the exact journaled generation so a crash is resumable without
overwriting a concurrent repair.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import socket
import stat
import tomllib
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal

from wall_in_one import (
    config,
    deployed_upgrade,
    file_io,
    legacy_migration,
    paths,
    predecessor_process,
    runtime_config,
)
from wall_in_one.library import (
    adopted,
    displays,
    favourites,
    pairings,
    playlists,
    removals,
    schedules,
    state_file,
)

TRANSACTION_VERSION: Final = 1
JOURNAL_KIND: Final = "deployed-upgrade-journal"
COMPLETION_KIND: Final = "deployed-upgrade-completion"
MAX_JOURNAL_BYTES: Final = 16 * 1024 * 1024
MAX_COMPLETION_BYTES: Final = 1024 * 1024
LOCK_TIMEOUT_SECONDS: Final = 60.0
AUTHORING_SOCKET_TIMEOUT_SECONDS: Final = 0.15
MAX_CLAIM_TOKEN_ATTEMPTS: Final = 64

TransactionStatus = Literal["retry", "conflict", "corrupt"]


class TransactionError(Exception):
    """The automatic upgrade could not safely make forward progress."""

    def __init__(self, message: str, *, status: TransactionStatus = "corrupt") -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class Outcome:
    changed: bool
    status: Literal["absent", "current", "prepared", "complete"]
    detail: str
    counts: deployed_upgrade.UpgradeCounts = field(default_factory=deployed_upgrade.UpgradeCounts)


@dataclass(frozen=True, slots=True)
class _Journal:
    adoption_id: str
    root: Path
    root_identity: file_io.PathIdentity
    automatic_stills: Path
    automatic_stills_identity: file_io.PathIdentity
    settings_path: Path
    settings_old_fingerprint: file_io.FileFingerprint
    settings_old_sha256: str
    settings_target: bytes
    settings_target_sha256: str
    runtime_path: Path
    runtime_old_fingerprint: file_io.FileFingerprint
    runtime_old_sha256: str
    runtime_target: bytes
    runtime_target_sha256: str
    noctalia_path: Path
    noctalia_fingerprint: file_io.FileFingerprint
    noctalia_sha256: str
    marker_path: Path
    marker_fingerprint: file_io.FileFingerprint
    marker_sha256: str
    authoring_evidence: tuple[deployed_upgrade.AuthoringEvidence, ...]
    authorities: tuple[adopted.Authority, ...]
    all_media_entry_ids: tuple[str, ...]
    counts: deployed_upgrade.UpgradeCounts
    authority_sha256: str

    @property
    def settings_token(self) -> str:
        return hashlib.sha256(f"{self.adoption_id}:settings".encode()).hexdigest()[:32]

    @property
    def runtime_token(self) -> str:
        return hashlib.sha256(f"{self.adoption_id}:runtime".encode()).hexdigest()[:32]

    @property
    def adoption(self) -> adopted.Adoption:
        return adopted.Adoption(
            adoption_id=self.adoption_id,
            root=self.root,
            root_identity=self.root_identity,
            automatic_stills=self.automatic_stills,
            automatic_stills_identity=self.automatic_stills_identity,
            marker_path=self.marker_path,
            marker_fingerprint=self.marker_fingerprint,
            marker_sha256=self.marker_sha256,
            authorities=self.authorities,
        )

    @property
    def authority_bytes(self) -> bytes:
        return adopted.render_manifest(self.adoption)


@dataclass(frozen=True, slots=True)
class _Completion:
    adoption_id: str
    root: Path
    authority_sha256: str
    settings_sha256: str
    runtime_sha256: str
    journal_sha256: str
    counts: deployed_upgrade.UpgradeCounts


def _sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _hex(value: object, *, length: int, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise TransactionError(f"{label} must be {length} lowercase hexadecimal characters")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _json_object(raw: bytes, target: Path, *, label: str) -> dict[str, Any]:
    try:
        value: object = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise TransactionError(f"{label} {target} is not duplicate-free JSON") from error
    if not isinstance(value, dict):
        raise TransactionError(f"{label} {target} is not a JSON object")
    return value


def _canonical(document: object) -> bytes:
    return adopted.canonical_bytes(document)


def _absolute_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str):
        raise TransactionError(f"{label} must be a path string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise TransactionError(f"{label} is not valid UTF-8") from error
    path = Path(value)
    if (
        not encoded
        or len(encoded) > deployed_upgrade.MAX_PATH_BYTES
        or not path.is_absolute()
        or path != Path(os.path.normpath(value))
        or any(ord(character) < 32 or 0x7F <= ord(character) <= 0x9F for character in value)
    ):
        raise TransactionError(f"{label} is not a normalized bounded absolute path")
    return path


def _integer_tuple(value: object, *, length: int, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise TransactionError(f"{label} must contain exactly {length} integers")
    if any(type(component) is not int or component < 0 for component in value):
        raise TransactionError(f"{label} contains an invalid integer")
    return tuple(value)


def _identity(value: object, *, label: str) -> file_io.PathIdentity:
    parsed = _integer_tuple(value, length=2, label=label)
    return parsed[0], parsed[1]


def _fingerprint(value: object, *, label: str) -> file_io.FileFingerprint:
    parsed = _integer_tuple(value, length=5, label=label)
    return parsed[0], parsed[1], parsed[2], parsed[3], parsed[4]


def _counts_document(counts: deployed_upgrade.UpgradeCounts) -> dict[str, int]:
    return {
        "videos": counts.videos,
        "captures": counts.captures,
        "all_media_entries": counts.all_media_entries,
        "playlists": counts.playlists,
        "playlist_entries": counts.playlist_entries,
        "schedules": counts.schedules,
    }


def _parse_counts(value: object, *, label: str) -> deployed_upgrade.UpgradeCounts:
    if not isinstance(value, dict) or set(value) != set(
        _counts_document(deployed_upgrade.UpgradeCounts())
    ):
        raise TransactionError(f"{label} has an incomplete or ambiguous shape")
    parsed: dict[str, int] = {}
    for key, raw in value.items():
        if type(raw) is not int or raw < 0:
            raise TransactionError(f"{label} {key} must be a non-negative integer")
        parsed[key] = raw
    counts = deployed_upgrade.UpgradeCounts(**parsed)
    if counts.videos != counts.captures:
        raise TransactionError(f"{label} video/capture counts do not match")
    if counts.videos > deployed_upgrade.MAX_CAPTURES:
        raise TransactionError(f"{label} exceeds the capture ceiling")
    return counts


def _file_document(
    path: Path, fingerprint: file_io.FileFingerprint, digest: str
) -> dict[str, object]:
    return {"path": str(path), "fingerprint": list(fingerprint), "sha256": digest}


def _authoring_document(
    evidence: deployed_upgrade.AuthoringEvidence,
) -> dict[str, object]:
    return {
        "kind": evidence.kind,
        "path": str(evidence.path),
        "fingerprint": (None if evidence.fingerprint is None else list(evidence.fingerprint)),
        "sha256": evidence.sha256,
    }


def _parse_file(value: object, *, label: str) -> tuple[Path, file_io.FileFingerprint, str]:
    if not isinstance(value, dict) or set(value) != {"path", "fingerprint", "sha256"}:
        raise TransactionError(f"{label} has an incomplete or ambiguous shape")
    return (
        _absolute_path(value["path"], label=f"{label} path"),
        _fingerprint(value["fingerprint"], label=f"{label} fingerprint"),
        _hex(value["sha256"], length=64, label=f"{label} sha256"),
    )


def _authoring_specs() -> tuple[
    tuple[deployed_upgrade.AuthoringKind, Path, int],
    ...,
]:
    return (
        ("pairings", pairings.state_path(), pairings.MAX_STATE_BYTES),
        ("playlists", playlists.state_path(), playlists.MAX_STATE_BYTES),
        ("schedules", schedules.state_path(), schedules.MAX_STATE_BYTES),
        ("displays", displays.state_path(), displays.MAX_STATE_BYTES),
        ("favourites", favourites.state_path(), favourites.MAX_STATE_BYTES),
        ("pending-removals", removals.state_path(), removals.MAX_STATE_BYTES),
    )


def _parse_authoring(value: object) -> tuple[deployed_upgrade.AuthoringEvidence, ...]:
    specs = _authoring_specs()
    if not isinstance(value, list) or len(value) != len(specs):
        raise TransactionError("journal authoring evidence has an incomplete shape")
    parsed: list[deployed_upgrade.AuthoringEvidence] = []
    for ordinal, (raw, (kind, expected_path, maximum)) in enumerate(
        zip(value, specs, strict=True),
        start=1,
    ):
        label = f"journal authoring evidence {ordinal}"
        if not isinstance(raw, dict) or set(raw) != {
            "kind",
            "path",
            "fingerprint",
            "sha256",
        }:
            raise TransactionError(f"{label} has an incomplete or ambiguous shape")
        if raw["kind"] != kind:
            raise TransactionError(f"{label} is not the expected {kind} record")
        path = _absolute_path(raw["path"], label=f"{label} path")
        if path != expected_path:
            raise TransactionError(f"{label} belongs to a different XDG path")
        raw_fingerprint = raw["fingerprint"]
        raw_sha256 = raw["sha256"]
        if raw_fingerprint is None and raw_sha256 is None:
            parsed.append(deployed_upgrade.AuthoringEvidence(kind, path, None, None))
            continue
        if raw_fingerprint is None or raw_sha256 is None:
            raise TransactionError(f"{label} mixes present and absent evidence")
        fingerprint = _fingerprint(raw_fingerprint, label=f"{label} fingerprint")
        if fingerprint[2] > maximum:
            raise TransactionError(f"{label} exceeds its authoring byte ceiling")
        parsed.append(
            deployed_upgrade.AuthoringEvidence(
                kind,
                path,
                fingerprint,
                _hex(raw_sha256, length=64, label=f"{label} sha256"),
            )
        )
    return tuple(parsed)


def _journal_document(journal: _Journal) -> dict[str, object]:
    return {
        "version": TRANSACTION_VERSION,
        "plugin": deployed_upgrade.PLUGIN_ID,
        "kind": JOURNAL_KIND,
        "adoption_id": journal.adoption_id,
        "root": str(journal.root),
        "root_identity": list(journal.root_identity),
        "automatic_stills": str(journal.automatic_stills),
        "automatic_stills_identity": list(journal.automatic_stills_identity),
        "settings": {
            **_file_document(
                journal.settings_path,
                journal.settings_old_fingerprint,
                journal.settings_old_sha256,
            ),
            "target": journal.settings_target.decode("utf-8"),
            "target_sha256": journal.settings_target_sha256,
        },
        "runtime": {
            **_file_document(
                journal.runtime_path,
                journal.runtime_old_fingerprint,
                journal.runtime_old_sha256,
            ),
            "target": journal.runtime_target.decode("utf-8"),
            "target_sha256": journal.runtime_target_sha256,
        },
        "noctalia": _file_document(
            journal.noctalia_path,
            journal.noctalia_fingerprint,
            journal.noctalia_sha256,
        ),
        "marker": _file_document(
            journal.marker_path,
            journal.marker_fingerprint,
            journal.marker_sha256,
        ),
        "authoring": [_authoring_document(evidence) for evidence in journal.authoring_evidence],
        "captures": [adopted.binding_document(authority) for authority in journal.authorities],
        "all_media_entry_ids": list(journal.all_media_entry_ids),
        "counts": _counts_document(journal.counts),
        "authority_sha256": journal.authority_sha256,
    }


def _journal_bytes(journal: _Journal) -> bytes:
    return _canonical(_journal_document(journal))


def _parse_authority(value: object, *, automatic: Path, ordinal: int) -> adopted.Authority:
    label = f"deployed-upgrade capture {ordinal}"
    expected = {
        "source_path",
        "capture_path",
        "dynamic_id",
        "source_fingerprint",
        "capture_fingerprint",
        "capture_size",
        "capture_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise TransactionError(f"{label} has an incomplete or ambiguous shape")
    source = _absolute_path(value["source_path"], label=f"{label} source")
    capture = _absolute_path(value["capture_path"], label=f"{label} capture")
    if capture.parent != automatic or capture.name != f"{source.stem}.png":
        raise TransactionError(f"{label} is not the exact predecessor basename relationship")
    if value["dynamic_id"] != f"video:{source}":
        raise TransactionError(f"{label} dynamic id does not match its source")
    source_fingerprint = _fingerprint(
        value["source_fingerprint"], label=f"{label} source fingerprint"
    )
    capture_fingerprint = _fingerprint(
        value["capture_fingerprint"], label=f"{label} capture fingerprint"
    )
    capture_size = value["capture_size"]
    if (
        type(capture_size) is not int
        or capture_size <= 0
        or capture_size > deployed_upgrade.MAX_CAPTURE_BYTES
        or capture_size != capture_fingerprint[2]
    ):
        raise TransactionError(f"{label} has an invalid capture size")
    return adopted.Authority(
        source_path=source,
        capture_path=capture,
        source_fingerprint=source_fingerprint,
        capture_fingerprint=capture_fingerprint,
        capture_size=capture_size,
        capture_sha256=_hex(value["capture_sha256"], length=64, label=f"{label} capture sha256"),
    )


def _parse_journal(raw: bytes, target: Path) -> _Journal:
    document = _json_object(raw, target, label="deployed-upgrade journal")
    expected = {
        "version",
        "plugin",
        "kind",
        "adoption_id",
        "root",
        "root_identity",
        "automatic_stills",
        "automatic_stills_identity",
        "settings",
        "runtime",
        "noctalia",
        "marker",
        "authoring",
        "captures",
        "all_media_entry_ids",
        "counts",
        "authority_sha256",
    }
    if set(document) != expected:
        raise TransactionError(
            f"deployed-upgrade journal {target} has an incomplete or ambiguous shape"
        )
    if (
        type(document["version"]) is not int
        or document["version"] != TRANSACTION_VERSION
        or document["plugin"] != deployed_upgrade.PLUGIN_ID
        or document["kind"] != JOURNAL_KIND
    ):
        raise TransactionError(f"deployed-upgrade journal {target} has an unsupported identity")
    adoption_id = _hex(document["adoption_id"], length=64, label="journal adoption_id")
    root = _absolute_path(document["root"], label="journal root")
    root_identity = _identity(document["root_identity"], label="journal root identity")
    automatic = _absolute_path(document["automatic_stills"], label="journal Automatic Stills")
    if automatic != root / "Wall-in-One" / "Automatic Stills":
        raise TransactionError("journal Automatic Stills does not match its root")
    automatic_identity = _identity(
        document["automatic_stills_identity"], label="journal Automatic Stills identity"
    )

    settings = document["settings"]
    if not isinstance(settings, dict) or set(settings) != {
        "path",
        "fingerprint",
        "sha256",
        "target",
        "target_sha256",
    }:
        raise TransactionError("journal settings record has an incomplete or ambiguous shape")
    settings_path = _absolute_path(settings["path"], label="journal settings path")
    settings_old_fingerprint = _fingerprint(
        settings["fingerprint"], label="journal settings fingerprint"
    )
    settings_old_sha256 = _hex(settings["sha256"], length=64, label="journal settings sha256")
    target_text = settings["target"]
    if not isinstance(target_text, str):
        raise TransactionError("journal target settings are not UTF-8 text")
    try:
        settings_target = target_text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise TransactionError("journal target settings are not valid UTF-8") from error
    if not settings_target or len(settings_target) > config.MAX_SETTINGS_BYTES:
        raise TransactionError("journal target settings exceed the settings byte bound")
    settings_target_sha256 = _hex(
        settings["target_sha256"], length=64, label="journal target settings sha256"
    )
    if _sha256(settings_target) != settings_target_sha256:
        raise TransactionError("journal target settings digest does not match its bytes")

    runtime_record = document["runtime"]
    if not isinstance(runtime_record, dict) or set(runtime_record) != {
        "path",
        "fingerprint",
        "sha256",
        "target",
        "target_sha256",
    }:
        raise TransactionError("journal runtime record has an incomplete or ambiguous shape")
    runtime_path = _absolute_path(runtime_record["path"], label="journal runtime path")
    runtime_fingerprint = _fingerprint(
        runtime_record["fingerprint"], label="journal runtime fingerprint"
    )
    runtime_sha256 = _hex(runtime_record["sha256"], length=64, label="journal runtime sha256")
    runtime_target_text = runtime_record["target"]
    if not isinstance(runtime_target_text, str):
        raise TransactionError("journal target runtime is not UTF-8 text")
    try:
        runtime_target = runtime_target_text.encode("utf-8")
    except UnicodeEncodeError as error:
        raise TransactionError("journal target runtime is not valid UTF-8") from error
    if not runtime_target or len(runtime_target) > runtime_config.MAX_RUNTIME_CONFIG_BYTES:
        raise TransactionError("journal target runtime exceeds the runtime byte bound")
    runtime_target_sha256 = _hex(
        runtime_record["target_sha256"],
        length=64,
        label="journal target runtime sha256",
    )
    if _sha256(runtime_target) != runtime_target_sha256:
        raise TransactionError("journal target runtime digest does not match its bytes")
    noctalia_path, noctalia_fingerprint, noctalia_sha256 = _parse_file(
        document["noctalia"], label="journal Noctalia settings"
    )
    marker_path, marker_fingerprint, marker_sha256 = _parse_file(
        document["marker"], label="journal Automatic Stills marker"
    )
    if marker_path != automatic / deployed_upgrade.AUTOMATIC_MARKER_FILENAME:
        raise TransactionError("journal marker is not the exact Automatic Stills marker")
    if settings_path != paths.settings_path() or runtime_path != paths.runtime_config_path():
        raise TransactionError("deployed-upgrade journal belongs to different XDG paths")
    if noctalia_path != paths.noctalia_settings_path():
        raise TransactionError("deployed-upgrade journal names a different Noctalia settings path")
    authoring_evidence = _parse_authoring(document["authoring"])

    captures = document["captures"]
    if (
        not isinstance(captures, list)
        or not captures
        or len(captures) > deployed_upgrade.MAX_CAPTURES
    ):
        raise TransactionError("journal captures are empty or exceed the capture ceiling")
    authorities = tuple(
        _parse_authority(value, automatic=automatic, ordinal=ordinal)
        for ordinal, value in enumerate(captures, start=1)
    )
    if len({authority.source_path for authority in authorities}) != len(authorities):
        raise TransactionError("journal contains duplicate capture source paths")
    if len({authority.capture_path for authority in authorities}) != len(authorities):
        raise TransactionError("journal contains duplicate capture paths")
    if (
        sum(authority.capture_size for authority in authorities)
        > deployed_upgrade.MAX_TOTAL_CAPTURE_BYTES
    ):
        raise TransactionError("journal captures exceed the aggregate byte ceiling")

    raw_entry_ids = document["all_media_entry_ids"]
    if (
        not isinstance(raw_entry_ids, list)
        or not raw_entry_ids
        or len(raw_entry_ids) > playlists.MAX_ENTRIES
        or any(
            not isinstance(identifier, str)
            or len(identifier) != 16
            or any(character not in "0123456789abcdef" for character in identifier)
            for identifier in raw_entry_ids
        )
        or len(set(raw_entry_ids)) != len(raw_entry_ids)
    ):
        raise TransactionError("journal All Media entry ids are invalid or ambiguous")
    all_media_entry_ids = tuple(raw_entry_ids)
    counts = _parse_counts(document["counts"], label="journal counts")
    if counts.captures != len(authorities) or counts.all_media_entries != len(all_media_entry_ids):
        raise TransactionError("journal counts do not match its captured relationships")
    authority_sha256 = _hex(
        document["authority_sha256"], length=64, label="journal authority sha256"
    )
    journal = _Journal(
        adoption_id=adoption_id,
        root=root,
        root_identity=root_identity,
        automatic_stills=automatic,
        automatic_stills_identity=automatic_identity,
        settings_path=settings_path,
        settings_old_fingerprint=settings_old_fingerprint,
        settings_old_sha256=settings_old_sha256,
        settings_target=settings_target,
        settings_target_sha256=settings_target_sha256,
        runtime_path=runtime_path,
        runtime_old_fingerprint=runtime_fingerprint,
        runtime_old_sha256=runtime_sha256,
        runtime_target=runtime_target,
        runtime_target_sha256=runtime_target_sha256,
        noctalia_path=noctalia_path,
        noctalia_fingerprint=noctalia_fingerprint,
        noctalia_sha256=noctalia_sha256,
        marker_path=marker_path,
        marker_fingerprint=marker_fingerprint,
        marker_sha256=marker_sha256,
        authoring_evidence=authoring_evidence,
        authorities=authorities,
        all_media_entry_ids=all_media_entry_ids,
        counts=counts,
        authority_sha256=authority_sha256,
    )
    if _sha256(journal.authority_bytes) != authority_sha256:
        raise TransactionError("journal authority digest does not match its capture evidence")
    _validate_staged_document(journal.runtime_target, journal)
    if raw != _journal_bytes(journal):
        raise TransactionError(f"deployed-upgrade journal {target} is not canonical")
    return journal


def _completion_document(completion: _Completion) -> dict[str, object]:
    return {
        "version": TRANSACTION_VERSION,
        "plugin": deployed_upgrade.PLUGIN_ID,
        "kind": COMPLETION_KIND,
        "adoption_id": completion.adoption_id,
        "root": str(completion.root),
        "authority_sha256": completion.authority_sha256,
        "settings_sha256": completion.settings_sha256,
        "runtime_sha256": completion.runtime_sha256,
        "journal_sha256": completion.journal_sha256,
        "counts": _counts_document(completion.counts),
    }


def _completion_bytes(completion: _Completion) -> bytes:
    return _canonical(_completion_document(completion))


def _parse_completion(raw: bytes, target: Path) -> _Completion:
    document = _json_object(raw, target, label="deployed-upgrade completion marker")
    expected = {
        "version",
        "plugin",
        "kind",
        "adoption_id",
        "root",
        "authority_sha256",
        "settings_sha256",
        "runtime_sha256",
        "journal_sha256",
        "counts",
    }
    if set(document) != expected:
        raise TransactionError(
            f"deployed-upgrade completion marker {target} has an incomplete or ambiguous shape"
        )
    if (
        type(document["version"]) is not int
        or document["version"] != TRANSACTION_VERSION
        or document["plugin"] != deployed_upgrade.PLUGIN_ID
        or document["kind"] != COMPLETION_KIND
    ):
        raise TransactionError(
            f"deployed-upgrade completion marker {target} has an unsupported identity"
        )
    completion = _Completion(
        adoption_id=_hex(document["adoption_id"], length=64, label="completion adoption_id"),
        root=_absolute_path(document["root"], label="completion root"),
        authority_sha256=_hex(
            document["authority_sha256"], length=64, label="completion authority sha256"
        ),
        settings_sha256=_hex(
            document["settings_sha256"], length=64, label="completion settings sha256"
        ),
        runtime_sha256=_hex(
            document["runtime_sha256"], length=64, label="completion runtime sha256"
        ),
        journal_sha256=_hex(
            document["journal_sha256"], length=64, label="completion journal sha256"
        ),
        counts=_parse_counts(document["counts"], label="completion counts"),
    )
    if raw != _completion_bytes(completion):
        raise TransactionError(f"deployed-upgrade completion marker {target} is not canonical")
    return completion


def _inspect(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise TransactionError(f"cannot inspect transaction path {path}: {error}") from error


@contextmanager
def _parent_access(
    path: Path,
    *,
    label: str,
    missing_ok: bool,
) -> Iterator[tuple[file_io.PinnedDirectoryContext, Path] | None]:
    """Retain a no-symlink capability for one transaction target's parent."""
    if not path.is_absolute() or path != Path(os.path.normpath(path)):
        raise TransactionError(f"{label} path {path} is not a normalized absolute path")
    try:
        parent_status = path.parent.lstat()
    except FileNotFoundError:
        if missing_ok:
            yield None
            return
        raise TransactionError(f"parent directory for {label} {path} is missing") from None
    except OSError as error:
        raise TransactionError(
            f"cannot inspect parent directory for {label} {path}: {error}"
        ) from error
    if not stat.S_ISDIR(parent_status.st_mode):
        raise TransactionError(f"parent path for {label} {path} is not a directory")
    try:
        root_identity = file_io.path_identity(Path("/"))
        context = file_io.pin_directory_beneath(
            Path("/"),
            path.parent,
            expected_root_identity=root_identity,
            expected_directory_identity=(parent_status.st_dev, parent_status.st_ino),
        )
    except (OSError, ValueError) as error:
        raise TransactionError(
            f"cannot pin parent directory for {label} {path} without symlinks: {error}"
        ) from error
    with context:
        try:
            context.verify_public()
        except (OSError, ValueError) as error:
            raise TransactionError(
                f"parent directory for {label} {path} changed before access: {error}"
            ) from error
        yield context, context.child(path.name)
        try:
            context.verify_public()
        except (OSError, ValueError) as error:
            raise TransactionError(
                f"parent directory for {label} {path} changed during access: {error}"
            ) from error


def _read_private_access(
    logical: Path,
    access: Path,
    maximum: int,
    *,
    label: str,
) -> bytes | None:
    """Read one private file through an already-retained parent capability."""
    status = _inspect(access)
    if status is None:
        try:
            logical.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise TransactionError(f"cannot inspect {label} {logical}: {error}") from error
        raise TransactionError(f"{label} {logical} changed while its parent was retained")
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_uid != os.getuid()
        or status.st_nlink != 1
        or stat.S_IMODE(status.st_mode) & 0o022
        or status.st_size > maximum
    ):
        raise TransactionError(f"{label} {logical} is not a bounded private regular file")
    fingerprint = file_io.file_fingerprint(status)
    try:
        pin = file_io.pin_regular_path(access, expected_fingerprint=fingerprint)
    except OSError as error:
        raise TransactionError(f"cannot pin {label} {logical}: {error}") from error
    try:
        raw = file_io.read_pinned_regular_bytes(
            pin,
            maximum,
            expected_fingerprint=fingerprint,
        )
        anchored = access.lstat()
        current = logical.lstat()
        if (
            file_io.file_fingerprint(anchored) != fingerprint
            or file_io.file_fingerprint(current) != fingerprint
            or pin.fingerprint != fingerprint
        ):
            raise TransactionError(f"{label} {logical} changed while it was read")
        return raw
    except (OSError, ValueError) as error:
        if isinstance(error, TransactionError):
            raise
        raise TransactionError(f"cannot safely read {label} {logical}: {error}") from error
    finally:
        pin.close()


def _read_private(path: Path, maximum: int, *, label: str) -> bytes | None:
    with _parent_access(path, label=label, missing_ok=True) as retained:
        if retained is None:
            return None
        _context, access = retained
        return _read_private_access(path, access, maximum, label=label)


def _exact_bytes(
    path: Path,
    expected: bytes,
    maximum: int,
    *,
    label: str,
) -> bool:
    raw = _read_private(path, maximum, label=label)
    return raw == expected if raw is not None else False


def _write_no_replace(
    path: Path,
    contents: bytes,
    maximum: int,
    *,
    label: str,
    create_parent: bool = True,
) -> bool:
    # Kept as an internal compatibility argument for interruption tests and
    # older callers. Transaction parents must already exist so they can be
    # pinned without following symlinks; no directory is created here.
    del create_parent
    with _parent_access(path, label=label, missing_ok=False) as retained:
        assert retained is not None
        context, access = retained
        return _write_no_replace_access(
            path,
            access,
            contents,
            maximum,
            label=label,
            context=context,
        )


def _write_no_replace_access(
    logical: Path,
    access: Path,
    contents: bytes,
    maximum: int,
    *,
    label: str,
    context: file_io.PinnedDirectoryContext,
) -> bool:
    """Publish through a retained parent without replacing any directory entry."""
    if not contents or len(contents) > maximum:
        raise TransactionError(f"{label} exceeds its publication byte bound")
    existing = _read_private_access(logical, access, maximum, label=label)
    if existing is not None:
        if existing == contents:
            return False
        raise TransactionError(
            f"{label} {logical} already exists with different bytes; it was not overwritten",
            status="conflict",
        )
    try:
        state_file.write_atomic_text(
            access,
            contents.decode("utf-8"),
            replace_existing=False,
        )
        access.chmod(0o600)
        context.verify_public()
    except (OSError, UnicodeDecodeError) as error:
        raise TransactionError(f"cannot publish {label} {logical}: {error}") from error
    if _read_private_access(logical, access, maximum, label=label) != contents:
        raise TransactionError(f"{label} {logical} changed immediately after publication")
    return True


def _read_journal() -> tuple[_Journal, bytes] | None:
    target = deployed_upgrade.journal_path()
    raw = _read_private(target, MAX_JOURNAL_BYTES, label="deployed-upgrade journal")
    return None if raw is None else (_parse_journal(raw, target), raw)


def _read_completion() -> tuple[_Completion, bytes] | None:
    target = deployed_upgrade.completion_path()
    raw = _read_private(target, MAX_COMPLETION_BYTES, label="deployed-upgrade completion marker")
    return None if raw is None else (_parse_completion(raw, target), raw)


def _journal_from_plan(plan: deployed_upgrade.UpgradePlan) -> _Journal:
    target_settings = replace(plan.settings, roots=(plan.root,)).validated()
    settings_target = target_settings.to_toml().encode("utf-8")
    authorities = tuple(
        adopted.Authority(
            source_path=capture.source_path,
            capture_path=capture.capture_path,
            source_fingerprint=capture.source_fingerprint,
            capture_fingerprint=capture.capture_fingerprint,
            capture_size=capture.capture_size,
            capture_sha256=capture.capture_sha256,
        )
        for capture in plan.captures
    )
    draft = adopted.Adoption(
        adoption_id="0" * 64,
        root=plan.root,
        root_identity=plan.root_identity,
        automatic_stills=plan.automatic_stills,
        automatic_stills_identity=plan.automatic_stills_identity,
        marker_path=plan.marker_path,
        marker_fingerprint=plan.marker_fingerprint,
        marker_sha256=plan.marker_sha256,
        authorities=authorities,
    )
    adoption_id = adopted.adoption_id_for(draft)
    adoption = replace(draft, adoption_id=adoption_id)
    authority_bytes = adopted.render_manifest(adoption)
    return _Journal(
        adoption_id=adoption_id,
        root=plan.root,
        root_identity=plan.root_identity,
        automatic_stills=plan.automatic_stills,
        automatic_stills_identity=plan.automatic_stills_identity,
        settings_path=plan.settings_path,
        settings_old_fingerprint=plan.settings_fingerprint,
        settings_old_sha256=plan.settings_sha256,
        settings_target=settings_target,
        settings_target_sha256=_sha256(settings_target),
        runtime_path=plan.runtime_path,
        runtime_old_fingerprint=plan.runtime_fingerprint,
        runtime_old_sha256=plan.runtime_sha256,
        runtime_target=plan.prepared_runtime_bytes,
        runtime_target_sha256=_sha256(plan.prepared_runtime_bytes),
        noctalia_path=plan.noctalia_path,
        noctalia_fingerprint=plan.noctalia_fingerprint,
        noctalia_sha256=plan.noctalia_sha256,
        marker_path=plan.marker_path,
        marker_fingerprint=plan.marker_fingerprint,
        marker_sha256=plan.marker_sha256,
        authoring_evidence=plan.authoring_evidence,
        authorities=authorities,
        all_media_entry_ids=plan.all_media_entry_ids,
        counts=plan.counts,
        authority_sha256=_sha256(authority_bytes),
    )


def _completion_matches_journal(completion: _Completion, journal: _Journal) -> None:
    if (
        completion.adoption_id != journal.adoption_id
        or completion.root != journal.root
        or completion.authority_sha256 != journal.authority_sha256
        or completion.settings_sha256 != journal.settings_target_sha256
        or completion.runtime_sha256 != journal.runtime_target_sha256
        or completion.counts != journal.counts
    ):
        raise TransactionError(
            "completion marker and surviving deployed-upgrade journal describe different states"
        )


def _replacement_publication_state(
    path: Path,
    *,
    old_fingerprint: file_io.FileFingerprint,
    old_sha256: str,
    target: bytes,
    maximum: int,
    token: str,
    label: str,
) -> Literal["old", "target", "claimed"]:
    """Classify one replacement exactly as the mutating CAS would.

    The status path must never restore a claim or publish a target, but it must
    prove that the next mutating call has one of those safe moves available.
    This mirrors :func:`_install_exact_replacement` without changing a name.
    """
    with _parent_access(path, label=label, missing_ok=False) as retained:
        assert retained is not None
        _context, access = retained
        current = _read_private_access(path, access, maximum, label=label)
        if current == target:
            return "target"

        claim, available_token = _recover_claim_slots(
            access,
            path,
            old_fingerprint=old_fingerprint,
            base_token=token,
            label=label,
            read_only=True,
        )
        try:
            if claim is not None and current is not None:
                raise TransactionError(
                    f"old {label} is retained in its durable claim but another public file exists",
                    status="conflict",
                )
            if current is None:
                if claim is None:
                    raise TransactionError(
                        f"{label} {path} is missing without its journaled old claim",
                        status="conflict",
                    )
                return "claimed"
            if available_token is None:
                raise TransactionError(
                    f"all {MAX_CLAIM_TOKEN_ATTEMPTS} durable claim slots for {label} are occupied",
                    status="conflict",
                )
            pin, _fingerprint = _pin_replacement_source(
                path,
                access,
                old_fingerprint=old_fingerprint,
                old_sha256=old_sha256,
                maximum=maximum,
                label=label,
            )
            pin.close()
            return "old"
        finally:
            if claim is not None:
                claim.close()


def _publication_states(
    journal: _Journal,
) -> tuple[Literal["old", "target", "claimed"], Literal["old", "target", "claimed"]]:
    """Validate both public CAS destinations for every journal phase."""
    settings = _replacement_publication_state(
        journal.settings_path,
        old_fingerprint=journal.settings_old_fingerprint,
        old_sha256=journal.settings_old_sha256,
        target=journal.settings_target,
        maximum=config.MAX_SETTINGS_BYTES,
        token=journal.settings_token,
        label="settings",
    )
    runtime = _replacement_publication_state(
        journal.runtime_path,
        old_fingerprint=journal.runtime_old_fingerprint,
        old_sha256=journal.runtime_old_sha256,
        target=journal.runtime_target,
        maximum=runtime_config.MAX_RUNTIME_CONFIG_BYTES,
        token=journal.runtime_token,
        label="runtime configuration",
    )
    return settings, runtime


def probe_future() -> deployed_upgrade.Probe | None:
    """Strictly classify reserved transaction artifacts without writing."""
    try:
        completion_record = _read_completion()
        authority_raw = _read_private(
            deployed_upgrade.authority_path(),
            adopted.MAX_MANIFEST_BYTES,
            label="capture-adoption manifest",
        )
        if completion_record is not None:
            completion, _raw = completion_record
            if authority_raw is None or _sha256(authority_raw) != completion.authority_sha256:
                raise TransactionError(
                    "completion marker has no byte-exact capture-adoption manifest"
                )
            # Journal and stage are non-authoritative cleanup residue after
            # the durable completion boundary. Authenticate an exact journal
            # when it survives, but an unrelated replacement must be retained
            # and ignored rather than turning completed state corrupt.
            try:
                journal_raw = _read_private(
                    deployed_upgrade.journal_path(),
                    MAX_JOURNAL_BYTES,
                    label="completed deployed-upgrade journal residue",
                )
            except TransactionError:
                journal_raw = None
            if journal_raw is not None and _sha256(journal_raw) == completion.journal_sha256:
                journal = _parse_journal(journal_raw, deployed_upgrade.journal_path())
                _completion_matches_journal(completion, journal)
                if authority_raw != journal.authority_bytes:
                    raise TransactionError(
                        "completion marker's authority differs from the surviving journal"
                    )
            # Completion is the durable commit boundary. A journal that
            # survived best-effort cleanup is immutable historical recovery
            # metadata, not continuing authority over settings, runtime, or
            # authoring generations that the user may legitimately edit after
            # the upgrade. The cutover path performs a separate strict proof
            # immediately after publishing this marker.
            loaded = adopted.load(strict=False, path=deployed_upgrade.authority_path())
            if (
                loaded is None
                or loaded.adoption_id != completion.adoption_id
                or loaded.root != completion.root
            ):
                raise TransactionError(
                    "completion marker and capture-adoption manifest do not share an identity "
                    "and root"
                )
            return deployed_upgrade.Probe(
                "complete",
                f"the deployed profile upgrade completed for explicit root {completion.root}",
                completion.counts,
            )

        journal_record = _read_journal()
        stage_raw = _read_private(
            deployed_upgrade.staged_runtime_path(),
            runtime_config.MAX_RUNTIME_CONFIG_BYTES,
            label="staged runtime",
        )
        if journal_record is None:
            if authority_raw is not None:
                try:
                    if adopted.load(strict=False, path=deployed_upgrade.authority_path()) is None:
                        raise TransactionError("orphan capture-adoption manifest disappeared")
                except adopted.AdoptionError as error:
                    raise TransactionError(
                        f"orphan capture-adoption manifest is malformed: {error}"
                    ) from error
            if stage_raw is not None:
                try:
                    stage_text = stage_raw.decode("utf-8")
                    stage_document = tomllib.loads(stage_text)
                    runtime_config.document_generation(stage_text)
                except (
                    UnicodeDecodeError,
                    tomllib.TOMLDecodeError,
                    runtime_config.RuntimeConfigError,
                ) as error:
                    raise TransactionError(
                        f"orphan staged runtime is malformed: {error}"
                    ) from error
                if stage_document.get("schema_version") != runtime_config.SCHEMA_VERSION:
                    raise TransactionError("orphan staged runtime is not schema 4")
            if authority_raw is not None or stage_raw is not None:
                raise TransactionError(
                    "deployed-upgrade authority or stage exists without its transaction journal",
                    status="conflict",
                )
            return None

        journal, _journal_raw = journal_record
        # A prepared status is a promise that ensure() can still make forward
        # progress, not merely that a journal and stage have familiar bytes.
        # Revalidate the same bounded evidence resume consumes so a reboot
        # cannot turn an externally changed predecessor profile into a false
        # green "prepared" result.
        _validate_static_evidence(journal)
        _validate_authoring_evidence(journal)
        _validate_capture_sidecars(journal)
        settings_state, runtime_state = _publication_states(journal)
        if authority_raw is not None:
            if authority_raw != journal.authority_bytes:
                raise TransactionError(
                    "capture-adoption manifest does not match the deployed-upgrade journal"
                )
            loaded = adopted.load(strict=True, path=deployed_upgrade.authority_path())
            if loaded != journal.adoption:
                raise TransactionError(
                    "capture-adoption manifest is not bound to the deployed-upgrade journal"
                )
        if stage_raw is not None:
            if authority_raw is None:
                raise TransactionError("staged runtime exists before capture authority")
            if stage_raw != journal.runtime_target:
                raise TransactionError(
                    "staged runtime does not match the dry-rendered journal target"
                )
            _validate_staged_document(stage_raw, journal)
            try:
                _observe_live_predecessor_writers()
            except TransactionError as error:
                if error.status != "retry":
                    raise
                return deployed_upgrade.Probe(
                    "in-progress",
                    f"the deployed profile upgrade is prepared but blocked: {error}",
                    journal.counts,
                )
            if runtime_state == "target":
                return deployed_upgrade.Probe(
                    "in-progress",
                    "the schema-4 runtime cutover committed and its completion marker is "
                    "still pending",
                    journal.counts,
                )
            if settings_state != "target" or runtime_state == "claimed":
                return deployed_upgrade.Probe(
                    "in-progress",
                    "the deployed profile upgrade has an exact predecessor claim which "
                    "ensure() must recover before cutover",
                    journal.counts,
                )
            return deployed_upgrade.Probe(
                "prepared",
                f"the deployed profile upgrade is prepared for root {journal.root}",
                journal.counts,
            )
        try:
            _observe_live_predecessor_writers()
        except TransactionError as error:
            if error.status != "retry":
                raise
            return deployed_upgrade.Probe(
                "in-progress",
                f"the deployed profile upgrade journal is blocked: {error}",
                journal.counts,
            )
        return deployed_upgrade.Probe(
            "in-progress",
            f"the deployed profile upgrade journal for {journal.root} awaits resume validation",
            journal.counts,
        )
    except adopted.AdoptionError as error:
        raise deployed_upgrade.UpgradeError(
            f"capture-adoption state is malformed: {error}", status="corrupt"
        ) from error
    except TransactionError as error:
        status: deployed_upgrade.FailureStatus = (
            "conflict" if error.status == "conflict" else "corrupt"
        )
        raise deployed_upgrade.UpgradeError(str(error), status=status) from error


def probe() -> deployed_upgrade.Probe:
    """Read-only exact status for fresh, predecessor, current, and future state."""
    found = deployed_upgrade.probe()
    if found.status != "ready":
        return found
    try:
        # ``deployed_upgrade.probe`` proves the predecessor snapshot but does
        # not expose its in-memory plan. Rebuild that bounded, read-only plan so
        # status can derive the exact durable-claim tokens which _begin_locked
        # will consume. A pre-existing slot is a persisted conflict, not a
        # writer-dependent reason to report a false-green ``ready``.
        journal = _journal_from_plan(deployed_upgrade.build_plan())
        _unjournaled_claim_slots_must_be_clear(journal)
    except deployed_upgrade.UpgradeError as error:
        return deployed_upgrade.Probe(error.status, str(error))
    except TransactionError as error:
        status: deployed_upgrade.FailureStatus = (
            "conflict" if error.status == "conflict" else "corrupt"
        )
        return deployed_upgrade.Probe(status, str(error), found.counts)
    except (OSError, ValueError) as error:
        return deployed_upgrade.Probe(
            "corrupt",
            f"cannot safely inspect deployed-upgrade claim slots: {error}",
            found.counts,
        )
    try:
        _observe_live_predecessor_writers()
    except TransactionError as error:
        if error.status != "retry":
            raise
        return deployed_upgrade.Probe(
            "in-progress",
            f"the exact predecessor profile is eligible but blocked: {error}",
            found.counts,
        )
    return found


def _authoring_paths() -> tuple[Path, ...]:
    return tuple(
        sorted(
            {
                paths.settings_path(),
                pairings.state_path(),
                playlists.state_path(),
                schedules.state_path(),
                displays.state_path(),
                favourites.state_path(),
                removals.state_path(),
            },
            key=str,
        )
    )


def _writer_lock_path(socket_path: Path) -> Path:
    return socket_path.with_name(f"{socket_path.name}.lock")


@contextmanager
def _writer_singleton_lock(socket_path: Path, *, owner: str) -> Iterator[None]:
    """Retain the singleton guard understood by current Python/Rust writers."""
    lock_path = _writer_lock_path(socket_path)
    descriptor: int | None = None
    locked = False
    try:
        paths.ensure_directory(lock_path.parent)
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        opened = os.fstat(descriptor)
        named = lock_path.lstat()
        identity = (opened.st_dev, opened.st_ino)
        if not (
            stat.S_ISREG(opened.st_mode)
            and stat.S_ISREG(named.st_mode)
            and opened.st_uid == os.getuid()
            and named.st_uid == os.getuid()
            and opened.st_nlink == 1
            and named.st_nlink == 1
            and (named.st_dev, named.st_ino) == identity
        ):
            raise TransactionError(
                f"cannot trust the {owner} singleton guard {lock_path}", status="retry"
            )
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            raise TransactionError(
                f"a Wall-in-One {owner} process still owns {lock_path}; stop it and retry",
                status="retry",
            ) from None
        secured = os.fstat(descriptor)
        renamed = lock_path.lstat()
        if not (
            stat.S_ISREG(secured.st_mode)
            and stat.S_ISREG(renamed.st_mode)
            and secured.st_uid == os.getuid()
            and renamed.st_uid == os.getuid()
            and secured.st_nlink == 1
            and renamed.st_nlink == 1
            and stat.S_IMODE(secured.st_mode) == 0o600
            and stat.S_IMODE(renamed.st_mode) == 0o600
            and (secured.st_dev, secured.st_ino) == identity
            and (renamed.st_dev, renamed.st_ino) == identity
        ):
            raise TransactionError(
                f"the {owner} singleton guard {lock_path} changed while being claimed",
                status="retry",
            )
        yield
    except TransactionError:
        raise
    except OSError as error:
        raise TransactionError(
            f"cannot acquire the {owner} singleton guard {lock_path}: {error}",
            status="retry",
        ) from error
    finally:
        if descriptor is not None:
            if locked:
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(descriptor)


@contextmanager
def _transaction_locks() -> Iterator[None]:
    stack = ExitStack()
    try:
        # Current Python and Rust processes take these before binding their
        # sockets and retain them for life. Claim both before the compiler and
        # Store locks so a writer cannot appear between a socket probe and
        # cutover. The exact deployed predecessor predates these locks, so its
        # process/socket checks remain a separate compatibility boundary.
        stack.enter_context(_writer_singleton_lock(paths.socket_path(), owner="authoring"))
        stack.enter_context(
            _writer_singleton_lock(
                paths.runtime_socket_path(),
                owner="wallpaper runtime",
            )
        )
        # Every normal authoring writer uses profile -> compiler -> Store.
        # Keep the same global order so startup migration cannot deadlock a
        # concurrent health/config writer while waiting for its Store lock.
        stack.enter_context(runtime_config.compiler_lock(timeout=LOCK_TIMEOUT_SECONDS))
        stack.enter_context(
            state_file.mutation_lock(
                deployed_upgrade.completion_path(),
                description="deployed profile upgrade",
                timeout=LOCK_TIMEOUT_SECONDS,
            )
        )
        for target in _authoring_paths():
            stack.enter_context(
                state_file.mutation_lock(
                    target,
                    description=f"deployed-upgrade authoring {target.name}",
                    timeout=LOCK_TIMEOUT_SECONDS,
                    process_gate=False,
                )
            )
    except TransactionError:
        stack.close()
        raise
    except (OSError, runtime_config.RuntimeConfigError) as error:
        stack.close()
        raise TransactionError(
            f"cannot acquire the deployed-upgrade exclusion: {error}", status="retry"
        ) from error
    with stack:
        yield


def _refuse_live_socket(target: Path, *, owner: str) -> None:
    """Refuse a socket which proves a predecessor writer is still running."""
    status = _inspect(target)
    if status is None:
        return
    if not stat.S_ISSOCK(status.st_mode):
        # A stale regular/symlink entry is not proof of a live writer.  The
        # current server owns its own hardened stale-socket handling.
        return
    probe_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe_socket.settimeout(AUTHORING_SOCKET_TIMEOUT_SECONDS)
        try:
            probe_socket.connect(str(target))
        except ConnectionRefusedError, FileNotFoundError:
            return
        except TimeoutError as error:
            raise TransactionError(
                f"cannot prove whether the {owner} socket {target} has a live writer; retry",
                status="retry",
            ) from error
        except OSError as error:
            raise TransactionError(
                f"cannot safely probe {owner} socket {target}: {error}", status="retry"
            ) from error
        raise TransactionError(
            f"a Wall-in-One {owner} process is still running; stop it before preparing or "
            "resuming the deployed-profile upgrade",
            status="retry",
        )
    finally:
        probe_socket.close()


def _refuse_live_predecessor_sockets() -> None:
    _refuse_live_socket(paths.socket_path(), owner="authoring")
    _refuse_live_socket(paths.runtime_socket_path(), owner="wallpaper runtime")


def _refuse_live_predecessor_process() -> None:
    """Detect the lock-unaware deployed Rust process before it binds."""
    try:
        predecessor_process.refuse_live_predecessor_runtime()
    except predecessor_process.PredecessorProcessError as error:
        raise TransactionError(str(error), status="retry") from error


def _observe_live_predecessor_writers() -> None:
    """Classify writer activity without connecting or attempting a lock.

    Status is observational: even a transient successful ``flock`` could make
    a concurrently starting writer fail its own nonblocking singleton claim,
    while connecting to a listening authoring socket can enqueue real work.
    Procfs exposes the held-lock and bound-socket facts without either side
    effect. The separate process walk retains coverage for the deployed Rust
    predecessor before it binds its endpoint.
    """
    try:
        predecessor_process.refuse_live_writer_status()
        predecessor_process.refuse_live_predecessor_runtime()
    except predecessor_process.PredecessorProcessError as error:
        raise TransactionError(str(error), status="retry") from error


def _refuse_lock_unaware_predecessor() -> None:
    _refuse_live_predecessor_sockets()
    _refuse_live_predecessor_process()


def _read_expected(
    path: Path,
    fingerprint: file_io.FileFingerprint,
    digest: str,
    maximum: int,
    *,
    label: str,
) -> bytes:
    with _parent_access(path, label=label, missing_ok=False) as retained:
        assert retained is not None
        _context, access = retained
        return _read_expected_access(
            path,
            access,
            fingerprint,
            digest,
            maximum,
            label=label,
        )


def _read_expected_access(
    logical: Path,
    access: Path,
    fingerprint: file_io.FileFingerprint,
    digest: str,
    maximum: int,
    *,
    label: str,
) -> bytes:
    """Validate journaled bytes through an already-retained parent."""
    try:
        pin = file_io.pin_regular_path(access, expected_fingerprint=fingerprint)
    except OSError as error:
        raise TransactionError(f"cannot pin journaled {label} {logical}: {error}") from error
    try:
        status = pin.status()
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.getuid()
            or status.st_nlink != 1
            or stat.S_IMODE(status.st_mode) & 0o022
        ):
            raise TransactionError(
                f"journaled {label} {logical} is not a private single-link regular file "
                "owned by this user"
            )
        raw = file_io.read_pinned_regular_bytes(
            pin,
            maximum,
            expected_fingerprint=fingerprint,
        )
        if _sha256(raw) != digest:
            raise TransactionError(f"journaled {label} {logical} changed contents")
        anchored = access.lstat()
        current = logical.lstat()
        if (
            file_io.file_fingerprint(anchored) != fingerprint
            or file_io.file_fingerprint(current) != fingerprint
            or pin.fingerprint != fingerprint
        ):
            raise TransactionError(f"journaled {label} {logical} changed generation")
        return raw
    except (OSError, ValueError) as error:
        if isinstance(error, TransactionError):
            raise
        raise TransactionError(f"cannot validate journaled {label} {logical}: {error}") from error
    finally:
        pin.close()


def _validate_noctalia_authority(journal: _Journal) -> None:
    """Accept ordinary Noctalia evolution only when its root stays identical.

    Noctalia replaces ``settings.toml`` when a wallpaper or theme changes, so
    an inode/hash pin is intentionally too strong for a transaction that may
    cross a reboot.  The deployed upgrade consumes one semantic fact from that
    document: the predecessor's effective wallpaper directory.  Preserve the
    exact fast path, then permit a safely re-read replacement only when that
    directory is still the journaled root.  Nothing is rewritten or rebased on
    disk, and a changed/malformed root remains a fail-closed user decision.
    """
    try:
        _read_expected(
            journal.noctalia_path,
            journal.noctalia_fingerprint,
            journal.noctalia_sha256,
            deployed_upgrade.MAX_NOCTALIA_BYTES,
            label="Noctalia settings",
        )
        return
    except TransactionError as exact_error:
        try:
            current = _read_private(
                journal.noctalia_path,
                deployed_upgrade.MAX_NOCTALIA_BYTES,
                label="current Noctalia settings after journal drift",
            )
        except TransactionError as error:
            raise TransactionError(
                "journaled Noctalia settings changed and the replacement cannot be safely "
                f"validated: {error}"
            ) from exact_error
        if current is None:
            raise TransactionError(
                "journaled Noctalia settings disappeared; restore a private settings.toml "
                f"whose [wallpaper].directory is {journal.root} and retry",
                status="conflict",
            ) from exact_error
        try:
            current_root = deployed_upgrade._noctalia_root(current, journal.noctalia_path)
        except deployed_upgrade.UpgradeError as error:
            raise TransactionError(
                "journaled Noctalia settings changed and the replacement no longer proves its "
                f"wallpaper directory: {error}; restore [wallpaper].directory to "
                f"{journal.root} and retry"
            ) from exact_error
        if current_root != journal.root:
            raise TransactionError(
                "Noctalia's wallpaper directory changed during the deployed-profile upgrade "
                f"from {journal.root} to {current_root}; restore it to {journal.root} and retry, "
                "or keep the preserved transaction for explicit recovery",
                status="conflict",
            ) from exact_error


def _validate_static_evidence(journal: _Journal) -> None:
    _validate_noctalia_authority(journal)
    _read_expected(
        journal.marker_path,
        journal.marker_fingerprint,
        journal.marker_sha256,
        deployed_upgrade.MAX_MARKER_BYTES,
        label="Automatic Stills marker",
    )
    try:
        with file_io.pin_directory_beneath(
            journal.root,
            journal.automatic_stills,
            expected_root_identity=journal.root_identity,
            expected_directory_identity=journal.automatic_stills_identity,
        ) as directory:
            directory.verify_public()
    except (OSError, ValueError) as error:
        raise TransactionError(
            f"journaled root or Automatic Stills changed before resume: {error}"
        ) from error


def _validate_authoring_evidence(journal: _Journal) -> None:
    specs = _authoring_specs()
    if tuple((record.kind, record.path) for record in journal.authoring_evidence) != tuple(
        (kind, path) for kind, path, _maximum in specs
    ):
        raise TransactionError("journal authoring evidence order or paths changed")
    for record, (_kind, path, maximum) in zip(
        journal.authoring_evidence,
        specs,
        strict=True,
    ):
        fingerprint = record.fingerprint
        digest = record.sha256
        if fingerprint is None and digest is None:
            with _parent_access(
                path,
                label=f"{record.kind} authoring state",
                missing_ok=True,
            ) as retained:
                if retained is None:
                    continue
                _context, access = retained
                if (
                    _read_private_access(
                        path,
                        access,
                        maximum,
                        label=f"{record.kind} authoring state",
                    )
                    is not None
                ):
                    raise TransactionError(
                        f"journaled-absent {record.kind} authoring state appeared",
                        status="conflict",
                    )
            continue
        if fingerprint is None or digest is None:
            raise TransactionError(f"journal {record.kind} authoring evidence is incomplete")
        _read_expected(
            path,
            fingerprint,
            digest,
            maximum,
            label=f"{record.kind} authoring state",
        )


def _capture_sidecars(journal: _Journal, *, publish: bool) -> bool:
    """Validate every captured generation and optionally publish its sidecar."""
    changed = False
    try:
        with (
            ExitStack() as stack,
            file_io.pin_directory_beneath(
                journal.root,
                journal.automatic_stills,
                expected_root_identity=journal.root_identity,
                expected_directory_identity=journal.automatic_stills_identity,
            ) as directory,
        ):
            retained: list[tuple[file_io.PinnedPath, file_io.FileFingerprint, str]] = []
            for authority in journal.authorities:
                if authority.capture_path.parent != journal.automatic_stills:
                    raise TransactionError(
                        f"journaled capture escaped Automatic Stills: {authority.capture_path}"
                    )
                scoped_source = file_io.pin_regular_path_beneath(
                    Path(authority.source_path.anchor), authority.source_path
                )
                source_pin = stack.enter_context(scoped_source.source)
                if source_pin.fingerprint != authority.source_fingerprint:
                    raise TransactionError(
                        f"video source changed before adoption: {authority.source_path}"
                    )
                source_status = source_pin.status()
                if source_status.st_uid != os.getuid() or source_status.st_nlink != 1:
                    raise TransactionError(
                        f"video source is no longer an owned single-link file: "
                        f"{authority.source_path}"
                    )
                capture_pin = stack.enter_context(
                    file_io.pin_regular_path(
                        directory.child(authority.capture_path.name),
                        expected_fingerprint=authority.capture_fingerprint,
                    )
                )
                capture_status = capture_pin.status()
                if capture_status.st_uid != os.getuid() or capture_status.st_nlink != 1:
                    raise TransactionError(
                        f"capture is no longer an owned single-link file: {authority.capture_path}"
                    )
                size, digest = file_io.hash_pinned_regular(
                    capture_pin,
                    expected_fingerprint=authority.capture_fingerprint,
                    maximum_bytes=authority.capture_size,
                )
                if (size, digest) != (authority.capture_size, authority.capture_sha256):
                    raise TransactionError(
                        f"capture bytes changed before adoption: {authority.capture_path}"
                    )
                retained.extend(
                    (
                        (source_pin, authority.source_fingerprint, "video source"),
                        (capture_pin, authority.capture_fingerprint, "capture"),
                    )
                )

            for authority in journal.authorities:
                sidecar = authority.sidecar_path
                expected = adopted.render_sidecar(authority, journal.adoption_id)
                if publish:
                    changed = (
                        _write_no_replace(
                            sidecar,
                            expected,
                            adopted.MAX_SIDECAR_BYTES,
                            label=f"capture authority sidecar {sidecar.name}",
                            create_parent=False,
                        )
                        or changed
                    )
                else:
                    current = _read_private_access(
                        sidecar,
                        directory.child(sidecar.name),
                        adopted.MAX_SIDECAR_BYTES,
                        label=f"capture authority sidecar {sidecar.name}",
                    )
                    if current is not None and current != expected:
                        raise TransactionError(
                            f"capture authority sidecar {sidecar} does not match the journal",
                            status="conflict",
                        )
            directory.verify_public()
            for pin, fingerprint, label in retained:
                if pin.fingerprint != fingerprint:
                    raise TransactionError(
                        f"journaled {label} changed while authority sidecars were published"
                    )
    except (OSError, ValueError) as error:
        if isinstance(error, TransactionError):
            raise
        raise TransactionError(f"cannot publish capture authority safely: {error}") from error

    return changed


def _validate_capture_sidecars(journal: _Journal) -> None:
    """Read-only half of sidecar publication used by status/resume parity."""
    _capture_sidecars(journal, publish=False)


def _publish_capture_sidecars(journal: _Journal) -> bool:
    return _capture_sidecars(journal, publish=True)


def _publish_authority_manifest(journal: _Journal) -> bool:
    changed = _write_no_replace(
        deployed_upgrade.authority_path(),
        journal.authority_bytes,
        adopted.MAX_MANIFEST_BYTES,
        label="capture-adoption manifest",
    )
    _require_exact_authority(journal)
    return changed


def _require_exact_authority(journal: _Journal) -> None:
    authority_raw = _read_private(
        deployed_upgrade.authority_path(),
        adopted.MAX_MANIFEST_BYTES,
        label="capture-adoption manifest",
    )
    if authority_raw != journal.authority_bytes:
        raise TransactionError("capture-adoption manifest does not match the journal bytes")
    try:
        exact = adopted.load(strict=True, path=deployed_upgrade.authority_path())
    except adopted.AdoptionError as error:
        raise TransactionError(f"published capture authority is not exact: {error}") from error
    if exact is None or exact != journal.adoption:
        raise TransactionError("published capture authority does not match the journal")


def _claim_path(path: Path, token: str) -> Path:
    return file_io.deletion_claim_directory(path, token)


def _claim_tokens(base: str) -> tuple[str, ...]:
    """Return bounded replay slots derived from one journaled operation id."""
    return (
        base,
        *(
            hashlib.sha256(f"{base}:{ordinal}".encode()).hexdigest()[:32]
            for ordinal in range(1, MAX_CLAIM_TOKEN_ATTEMPTS)
        ),
    )


def _recover_claim_slots(
    access: Path,
    logical: Path,
    *,
    old_fingerprint: file_io.FileFingerprint,
    base_token: str,
    label: str,
    read_only: bool = False,
) -> tuple[file_io.ClaimedPath | None, str | None]:
    """Recover one exact claim and find the first never-allocated slot.

    Empty durable claim directories are deliberately never trusted, removed,
    or reused. A crash before the atomic move therefore consumes one bounded
    slot; the next attempt advances while still scanning every slot for an
    exact retained predecessor.
    """
    recovered: file_io.ClaimedPath | None = None
    available: str | None = None
    try:
        for token in _claim_tokens(base_token):
            claim_directory = _claim_path(access, token)
            present = _inspect(claim_directory) is not None
            if not present:
                if available is None:
                    available = token
                continue
            try:
                candidate = file_io.recover_deletion_claim(
                    access,
                    logical_path=logical,
                    expected_identity=old_fingerprint[:2],
                    expected_fingerprint=old_fingerprint,
                    operation_token=token,
                    read_only=read_only,
                )
            except (OSError, ValueError) as error:
                raise TransactionError(f"cannot recover old {label} claim: {error}") from error
            if candidate is None:
                continue
            if recovered is not None:
                candidate.close()
                raise TransactionError(
                    f"multiple exact durable claims exist for old {label}",
                    status="conflict",
                )
            recovered = candidate
    except BaseException:
        if recovered is not None:
            recovered.close()
        raise
    return recovered, available


def _pin_replacement_source(
    logical: Path,
    access: Path,
    *,
    old_fingerprint: file_io.FileFingerprint,
    old_sha256: str,
    maximum: int,
    label: str,
) -> tuple[file_io.PinnedPath, file_io.FileFingerprint]:
    """Pin journaled old bytes, allowing only our prior rename round trip."""
    try:
        pin = file_io.pin_regular_path(access, expected_identity=old_fingerprint[:2])
    except OSError as error:
        raise TransactionError(f"cannot pin journaled old {label} {logical}: {error}") from error
    try:
        status = pin.status()
        current_fingerprint = pin.fingerprint
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.getuid()
            or status.st_nlink != 1
            or stat.S_IMODE(status.st_mode) & 0o022
            or current_fingerprint[:4] != old_fingerprint[:4]
        ):
            raise TransactionError(
                f"journaled old {label} {logical} is not the retained predecessor generation"
            )
        raw = file_io.read_pinned_regular_bytes(
            pin,
            maximum,
            expected_fingerprint=current_fingerprint,
        )
        if _sha256(raw) != old_sha256:
            raise TransactionError(f"journaled old {label} {logical} changed contents")
        anchored = access.lstat()
        public = logical.lstat()
        if (
            file_io.file_fingerprint(anchored) != current_fingerprint
            or file_io.file_fingerprint(public) != current_fingerprint
            or pin.fingerprint != current_fingerprint
        ):
            raise TransactionError(f"journaled old {label} {logical} changed generation")
        return pin, current_fingerprint
    except (OSError, ValueError) as error:
        pin.close()
        if isinstance(error, TransactionError):
            raise
        raise TransactionError(
            f"cannot validate journaled old {label} {logical}: {error}"
        ) from error


def _install_exact_replacement(
    path: Path,
    *,
    old_fingerprint: file_io.FileFingerprint,
    old_sha256: str,
    target: bytes,
    maximum: int,
    token: str,
    label: str,
) -> bool:
    """Install target after atomically claiming only the journaled old inode."""
    with _parent_access(path, label=label, missing_ok=False) as retained:
        assert retained is not None
        context, access = retained
        target_current = _read_private_access(path, access, maximum, label=label)
        if target_current == target:
            # The public CAS target is the committed value. A predecessor
            # retained in a private claim may have been mutated through an FD
            # opened before its rename; never re-hash-and-truncate user bytes
            # merely to clean inert recovery residue.
            return False
        claim, available_token = _recover_claim_slots(
            access,
            path,
            old_fingerprint=old_fingerprint,
            base_token=token,
            label=label,
        )
        old_present = target_current is not None
        if claim is not None and old_present:
            claim.close()
            raise TransactionError(
                f"old {label} is retained in its durable claim but another public file exists",
                status="conflict",
            )
        if claim is None and old_present:
            if available_token is None:
                raise TransactionError(
                    f"all {MAX_CLAIM_TOKEN_ATTEMPTS} durable claim slots for {label} are occupied",
                    status="conflict",
                )
            pin: file_io.PinnedPath | None = None
            try:
                pin, current_fingerprint = _pin_replacement_source(
                    path,
                    access,
                    old_fingerprint=old_fingerprint,
                    old_sha256=old_sha256,
                    maximum=maximum,
                    label=label,
                )
                claim = file_io.claim_for_deletion(
                    access,
                    logical_path=path,
                    retained_parent=access.parent,
                    logical_retained_parent=path.parent,
                    expected_identity=old_fingerprint[:2],
                    expected_fingerprint=current_fingerprint,
                    pinned_source=pin,
                    operation_token=available_token,
                )
            except (OSError, ValueError) as error:
                if pin is not None:
                    with contextlib.suppress(OSError):
                        pin.close()
                raise TransactionError(f"cannot atomically claim old {label}: {error}") from error
        elif claim is None:
            raise TransactionError(
                f"{label} {path} is missing without its journaled old claim",
                status="conflict",
            )

        assert claim is not None
        try:
            _write_no_replace_access(
                path,
                access,
                target,
                maximum,
                label=label,
                context=context,
            )
        except BaseException:
            # The journal and exact private claim are the recovery authority.
            # Restoring by name would advance ctime and make the original
            # fingerprint look like an unrelated generation on the next run.
            claim.close()
            raise
        # The public target is durable. Keep the predecessor claim inert:
        # deleting through a retained descriptor could destroy writes made by
        # an already-open noncooperating FD after the rename.
        claim.close()
        return True


def _validate_staged_document(raw: bytes, journal: _Journal) -> None:
    try:
        document = raw.decode("utf-8")
        parsed = tomllib.loads(document)
        runtime_config.document_generation(document)
    except (
        UnicodeDecodeError,
        tomllib.TOMLDecodeError,
        runtime_config.RuntimeConfigError,
    ) as error:
        raise TransactionError(f"staged schema-4 runtime is invalid: {error}") from error
    if parsed.get("schema_version") != runtime_config.SCHEMA_VERSION:
        raise TransactionError("staged runtime is not schema 4")
    raw_playlists = parsed.get("playlists")
    if not isinstance(raw_playlists, list) or not raw_playlists:
        raise TransactionError("staged runtime has no playlists")
    fallback = raw_playlists[0]
    if (
        not isinstance(fallback, dict)
        or fallback.get("id") != runtime_config.FALLBACK_PLAYLIST_ID
        or fallback.get("name") != runtime_config.FALLBACK_PLAYLIST_NAME
        or not isinstance(fallback.get("entries"), list)
    ):
        raise TransactionError("staged runtime has no exact All Media playlist")
    fallback_entries = fallback["entries"]
    assert isinstance(fallback_entries, list)
    entry_ids = tuple(
        entry.get("id") if isinstance(entry, dict) else None for entry in fallback_entries
    )
    if entry_ids != journal.all_media_entry_ids:
        raise TransactionError(
            "staged All Media does not preserve the predecessor entry identities/order"
        )
    video_stills: dict[Path, Path] = {}
    for entry in fallback_entries:
        if not isinstance(entry, dict):
            raise TransactionError("staged All Media contains a non-table entry")
        if entry.get("kind") == "video":
            motion = entry.get("motion")
            still = entry.get("still")
            if not isinstance(motion, str) or not isinstance(still, str):
                raise TransactionError("staged video entry has no motion/still path")
            video_stills[Path(motion)] = Path(still)
    expected_video_stills = {
        authority.source_path: authority.capture_path for authority in journal.authorities
    }
    if video_stills != expected_video_stills:
        raise TransactionError("staged All Media does not use every exact predecessor capture path")
    named = raw_playlists[1:]
    named_entry_count = 0
    for playlist in named:
        if not isinstance(playlist, dict) or not isinstance(playlist.get("entries"), list):
            raise TransactionError("staged named playlist has an invalid shape")
        named_entry_count += len(playlist["entries"])
    raw_schedules = parsed.get("schedules", [])
    if not isinstance(raw_schedules, list):
        raise TransactionError("staged schedules are not an array")
    if (
        len(fallback_entries) != journal.counts.all_media_entries
        or len(named) != journal.counts.playlists
        or named_entry_count != journal.counts.playlist_entries
        or len(raw_schedules) != journal.counts.schedules
    ):
        raise TransactionError("staged runtime does not preserve predecessor authoring counts")


def _claim_directory_must_be_clear(path: Path, token: str, *, label: str) -> None:
    """Observe every bounded replay slot without recovering or changing it."""
    with _parent_access(path, label=label, missing_ok=False) as retained:
        assert retained is not None
        _context, access = retained
        for candidate in _claim_tokens(token):
            logical_claim = _claim_path(path, candidate)
            access_claim = _claim_path(access, candidate)
            if _inspect(access_claim) is not None:
                raise TransactionError(
                    f"reserved {label} claim path already exists before migration: {logical_claim}",
                    status="conflict",
                )


def _unjournaled_claim_slots_must_be_clear(journal: _Journal) -> None:
    """Reject persisted CAS residue before a journal grants it authority.

    This inspector is strictly read-only: it retains each public parent and
    uses ``lstat``-style observations for all bounded token-derived names. It
    never calls claim recovery, creates a slot, or centralises a tombstone.
    """
    _claim_directory_must_be_clear(
        journal.settings_path,
        journal.settings_token,
        label="settings",
    )
    _claim_directory_must_be_clear(
        journal.runtime_path,
        journal.runtime_token,
        label="runtime",
    )


def _begin_locked() -> tuple[_Journal, bytes, bool]:
    _refuse_lock_unaware_predecessor()
    plan = deployed_upgrade.build_plan()
    journal = _journal_from_plan(plan)
    _validate_staged_document(journal.runtime_target, journal)
    raw = _journal_bytes(journal)
    if len(raw) > MAX_JOURNAL_BYTES:
        raise TransactionError("deployed-upgrade journal exceeds its byte bound")
    _unjournaled_claim_slots_must_be_clear(journal)
    changed = _write_no_replace(
        deployed_upgrade.journal_path(),
        raw,
        MAX_JOURNAL_BYTES,
        label="deployed-upgrade journal",
    )
    return journal, raw, changed


def _prepare_locked(journal: _Journal, journal_raw: bytes) -> tuple[bytes, bool]:
    _refuse_lock_unaware_predecessor()
    _validate_static_evidence(journal)
    _validate_authoring_evidence(journal)
    changed = _publish_capture_sidecars(journal)
    changed = (
        _install_exact_replacement(
            journal.settings_path,
            old_fingerprint=journal.settings_old_fingerprint,
            old_sha256=journal.settings_old_sha256,
            target=journal.settings_target,
            maximum=config.MAX_SETTINGS_BYTES,
            token=journal.settings_token,
            label="settings",
        )
        or changed
    )
    changed = _publish_authority_manifest(journal) or changed
    # Detection dry-rendered these bytes while every predecessor file and
    # authoring snapshot was pinned, then made that exact target part of the
    # durable journal. Do not rescan after publication: a newly appearing
    # media file must not strand an otherwise exact migration halfway through.
    stage = journal.runtime_target
    _validate_staged_document(stage, journal)
    changed = (
        _write_no_replace(
            deployed_upgrade.staged_runtime_path(),
            stage,
            runtime_config.MAX_RUNTIME_CONFIG_BYTES,
            label="staged runtime",
        )
        or changed
    )
    if (
        _read_private(
            deployed_upgrade.staged_runtime_path(),
            runtime_config.MAX_RUNTIME_CONFIG_BYTES,
            label="staged runtime",
        )
        != journal.runtime_target
    ):
        raise TransactionError("staged runtime changed immediately after publication")
    _validate_authoring_evidence(journal)
    if _sha256(journal_raw) != _sha256(_journal_bytes(journal)):
        raise TransactionError("in-memory deployed-upgrade journal changed during preparation")
    return stage, changed


def _completion_for(journal: _Journal, journal_raw: bytes, stage: bytes) -> _Completion:
    return _Completion(
        adoption_id=journal.adoption_id,
        root=journal.root,
        authority_sha256=journal.authority_sha256,
        settings_sha256=journal.settings_target_sha256,
        runtime_sha256=_sha256(stage),
        journal_sha256=_sha256(journal_raw),
        counts=journal.counts,
    )


def _prepared_runtime_state(journal: _Journal, stage: bytes) -> Literal["old", "cut-over"]:
    label = "runtime configuration"
    with _parent_access(journal.runtime_path, label=label, missing_ok=False) as retained:
        assert retained is not None
        _context, access = retained
        current = _read_private_access(
            journal.runtime_path,
            access,
            runtime_config.MAX_RUNTIME_CONFIG_BYTES,
            label=label,
        )
        if current == stage:
            return "cut-over"
        if current is not None:
            pin, _fingerprint = _pin_replacement_source(
                journal.runtime_path,
                access,
                old_fingerprint=journal.runtime_old_fingerprint,
                old_sha256=journal.runtime_old_sha256,
                maximum=runtime_config.MAX_RUNTIME_CONFIG_BYTES,
                label=label,
            )
            pin.close()
            return "old"
        claim, _available = _recover_claim_slots(
            access,
            journal.runtime_path,
            old_fingerprint=journal.runtime_old_fingerprint,
            base_token=journal.runtime_token,
            label=label,
        )
        if claim is None:
            raise TransactionError(
                "public runtime is missing without its exact predecessor claim",
                status="conflict",
            )
        try:
            restored = claim.restore()
        except (OSError, ValueError) as error:
            claim.close()
            raise TransactionError(
                f"cannot restore interrupted schema-2 runtime: {error}"
            ) from error
        if not restored:
            claim.close()
            raise TransactionError(
                "a concurrent runtime appeared while restoring the schema-2 predecessor",
                status="conflict",
            )
        pin, _fingerprint = _pin_replacement_source(
            journal.runtime_path,
            access,
            old_fingerprint=journal.runtime_old_fingerprint,
            old_sha256=journal.runtime_old_sha256,
            maximum=runtime_config.MAX_RUNTIME_CONFIG_BYTES,
            label=label,
        )
        pin.close()
        return "old"


def _discard_exact_artifact(path: Path, contents: bytes, maximum: int, *, label: str) -> None:
    try:
        with _parent_access(path, label=label, missing_ok=True) as retained:
            if retained is None:
                return
            _context, access = retained
            status = _inspect(access)
            if status is None:
                return
            if _read_private_access(path, access, maximum, label=label) != contents:
                return
            fingerprint = file_io.file_fingerprint(status)
            pin = file_io.pin_regular_path(access, expected_fingerprint=fingerprint)
            try:
                file_io.discard_regular_if_same(
                    access,
                    expected_identity=fingerprint[:2],
                    expected_fingerprint=fingerprint,
                    pinned_source=pin,
                    retained_parent=access.parent,
                    logical_retained_parent=path.parent,
                )
            except OSError, ValueError:
                return
            finally:
                with contextlib.suppress(OSError):
                    pin.close()
    except OSError, ValueError, TransactionError:
        # Completion is already durable. Exact recovery artifacts may safely
        # remain; later starts authenticate them against the historical marker
        # without treating them as authority over mutable user state.
        return


def _validate_cutover_snapshot(journal: _Journal, journal_raw: bytes, stage: bytes) -> None:
    """Re-prove every publication input immediately before completion."""
    _refuse_lock_unaware_predecessor()
    _validate_static_evidence(journal)
    if stage != journal.runtime_target or _sha256(stage) != journal.runtime_target_sha256:
        raise TransactionError("cutover runtime differs from the dry-rendered journal target")
    current_journal = _read_private(
        deployed_upgrade.journal_path(),
        MAX_JOURNAL_BYTES,
        label="deployed-upgrade journal",
    )
    if current_journal != journal_raw or journal_raw != _journal_bytes(journal):
        raise TransactionError("deployed-upgrade journal changed before cutover")
    current_stage = _read_private(
        deployed_upgrade.staged_runtime_path(),
        runtime_config.MAX_RUNTIME_CONFIG_BYTES,
        label="staged runtime",
    )
    if current_stage != stage:
        raise TransactionError("staged runtime changed before cutover")
    _validate_staged_document(stage, journal)
    if (
        _read_private(
            journal.settings_path,
            config.MAX_SETTINGS_BYTES,
            label="settings",
        )
        != journal.settings_target
    ):
        raise TransactionError("migrated settings changed before cutover")
    _require_exact_authority(journal)
    _validate_authoring_evidence(journal)
    _validate_capture_sidecars(journal)


def _validate_published_completion(
    completion: _Completion,
    completion_raw: bytes,
    journal: _Journal,
    journal_raw: bytes,
    stage: bytes,
) -> None:
    """Strictly prove the just-published commit before cleanup begins.

    Public probes deliberately treat completion as historical so ordinary
    edits made after this transaction cannot invalidate it. Cutover still has
    a narrower obligation: before it discards recovery metadata, re-prove the
    exact settings, runtime, authoring snapshot, capture authority, and marker
    that were present at the instant this completion marker was published.
    """
    expected = _completion_for(journal, journal_raw, stage)
    if completion != expected or completion_raw != _completion_bytes(expected):
        raise TransactionError("in-memory completion marker changed before verification")
    current = _read_completion()
    if current is None or current != (completion, completion_raw):
        raise TransactionError("completion marker changed immediately after publication")
    _validate_cutover_snapshot(journal, journal_raw, stage)
    if (
        _read_private(
            journal.runtime_path,
            runtime_config.MAX_RUNTIME_CONFIG_BYTES,
            label="runtime configuration",
        )
        != stage
    ):
        raise TransactionError("schema-4 runtime changed immediately after completion")
    if _read_completion() != (completion, completion_raw):
        raise TransactionError("completion marker changed during post-publication verification")


def _cutover_locked(journal: _Journal, journal_raw: bytes, stage: bytes) -> bool:
    _validate_cutover_snapshot(journal, journal_raw, stage)
    changed = _install_exact_replacement(
        journal.runtime_path,
        old_fingerprint=journal.runtime_old_fingerprint,
        old_sha256=journal.runtime_old_sha256,
        target=stage,
        maximum=runtime_config.MAX_RUNTIME_CONFIG_BYTES,
        token=journal.runtime_token,
        label="runtime configuration",
    )
    public_runtime = _read_private(
        journal.runtime_path,
        runtime_config.MAX_RUNTIME_CONFIG_BYTES,
        label="runtime configuration",
    )
    if public_runtime != stage:
        raise TransactionError("schema-4 runtime changed immediately after cutover")
    # The runtime claim/install is itself a durable publication boundary.
    # Re-prove the complete authoring snapshot after it and immediately before
    # the completion marker so a non-cooperating writer cannot be blessed.
    _validate_cutover_snapshot(journal, journal_raw, stage)
    if (
        _read_private(
            journal.runtime_path,
            runtime_config.MAX_RUNTIME_CONFIG_BYTES,
            label="runtime configuration",
        )
        != stage
    ):
        raise TransactionError("schema-4 runtime changed before completion")
    completion = _completion_for(journal, journal_raw, stage)
    completion_raw = _completion_bytes(completion)
    changed = (
        _write_no_replace(
            deployed_upgrade.completion_path(),
            completion_raw,
            MAX_COMPLETION_BYTES,
            label="deployed-upgrade completion marker",
        )
        or changed
    )
    _validate_published_completion(
        completion,
        completion_raw,
        journal,
        journal_raw,
        stage,
    )
    _discard_exact_artifact(
        deployed_upgrade.staged_runtime_path(),
        stage,
        runtime_config.MAX_RUNTIME_CONFIG_BYTES,
        label="staged runtime",
    )
    _discard_exact_artifact(
        deployed_upgrade.journal_path(),
        journal_raw,
        MAX_JOURNAL_BYTES,
        label="deployed-upgrade journal",
    )
    return changed


def _cleanup_completed_recovery() -> None:
    """Best-effort cleanup of only residue authenticated by completion.

    A completed profile no longer grants its journal or stage authority over
    mutable state. They may be removed only when a fresh read binds the exact
    canonical journal to the current completion marker and recovers the exact
    dry-rendered stage expectation from that journal. Any replacement is left
    untouched.
    """
    try:
        completion_record = _read_completion()
        if completion_record is None:
            return
        completion, completion_raw = completion_record
        journal_raw = _read_private(
            deployed_upgrade.journal_path(),
            MAX_JOURNAL_BYTES,
            label="completed deployed-upgrade journal residue",
        )
        if journal_raw is None or _sha256(journal_raw) != completion.journal_sha256:
            return
        journal = _parse_journal(journal_raw, deployed_upgrade.journal_path())
        _completion_matches_journal(completion, journal)
        if journal.runtime_target_sha256 != completion.runtime_sha256:
            return
        stage_raw = _read_private(
            deployed_upgrade.staged_runtime_path(),
            runtime_config.MAX_RUNTIME_CONFIG_BYTES,
            label="completed staged-runtime residue",
        )
        if stage_raw is not None:
            if stage_raw != journal.runtime_target:
                return
            _discard_exact_artifact(
                deployed_upgrade.staged_runtime_path(),
                journal.runtime_target,
                runtime_config.MAX_RUNTIME_CONFIG_BYTES,
                label="staged runtime",
            )
            # Keep the authenticated journal whenever stage cleanup did not
            # visibly commit; it remains the only source of exact stage bytes
            # for a later safe retry.
            if (
                _read_private(
                    deployed_upgrade.staged_runtime_path(),
                    runtime_config.MAX_RUNTIME_CONFIG_BYTES,
                    label="completed staged-runtime residue",
                )
                is not None
            ):
                return
        if _read_completion() != (completion, completion_raw):
            return
        _discard_exact_artifact(
            deployed_upgrade.journal_path(),
            journal_raw,
            MAX_JOURNAL_BYTES,
            label="deployed-upgrade journal",
        )
    except OSError, ValueError, TransactionError:
        return


def _ensure_locked(*, cutover: bool) -> Outcome:
    found = probe()
    if found.status == "absent":
        return Outcome(False, "absent", found.detail)
    if found.status == "current":
        return Outcome(False, "current", found.detail)
    if found.status in ("conflict", "corrupt"):
        raise TransactionError(found.detail, status=found.status)

    if found.status == "complete":
        _cleanup_completed_recovery()
        return Outcome(False, "complete", found.detail, found.counts)

    journal_record = _read_journal()
    changed = False
    if journal_record is None:
        # Public status may classify an otherwise-ready predecessor as blocked
        # by a live lock-unaware daemon. Once the transaction locks are held,
        # re-read the persisted profile shape and let _begin_locked perform the
        # immediate process/socket proof which either advances or raises retry.
        baseline = deployed_upgrade.probe()
        if baseline.status != "ready":
            raise TransactionError(
                f"deployed-upgrade status {baseline.status} has no resumable journal"
            )
        journal, journal_raw, changed = _begin_locked()
    else:
        journal, journal_raw = journal_record
    stage, prepared_changed = _prepare_locked(journal, journal_raw)
    changed = changed or prepared_changed
    if not cutover:
        runtime_state = _prepared_runtime_state(journal, stage)
        if runtime_state == "cut-over":
            changed = _cutover_locked(journal, journal_raw, stage) or changed
            return Outcome(
                changed,
                "complete",
                f"completed the already-committed runtime cutover for root {journal.root}",
                journal.counts,
            )
        return Outcome(
            changed,
            "prepared",
            f"prepared schema 4 for explicit predecessor root {journal.root}; "
            "the public schema-2 runtime remains unchanged",
            journal.counts,
        )
    changed = _cutover_locked(journal, journal_raw, stage) or changed
    return Outcome(
        changed,
        "complete",
        f"migrated the deployed profile in place using original root {journal.root}",
        journal.counts,
    )


def ensure(*, cutover: bool = True) -> Outcome:
    """Automatically migrate/resume exact predecessor state, or do nothing.

    ``cutover=False`` remains only as an internal compatibility/test seam for
    the v0.1.1 private-prepared boundary. Supported public and startup callers
    use the default: claim the exact old runtime, install the validated schema
    4 bytes, and record completion in one bounded invocation. Fresh and
    ordinary current profiles are intentionally unchanged, including their XDG
    trees: the first probe occurs before any persistent lock exists.
    """
    initial = probe()
    if initial.status == "absent":
        return Outcome(False, "absent", initial.detail)
    if initial.status == "current":
        return Outcome(False, "current", initial.detail)
    if initial.status in ("conflict", "corrupt"):
        raise TransactionError(initial.detail, status=initial.status)
    if initial.status == "complete":
        try:
            completion_record = _read_completion()
            journal_raw = _read_private(
                deployed_upgrade.journal_path(),
                MAX_JOURNAL_BYTES,
                label="completed deployed-upgrade journal residue",
            )
        except TransactionError:
            return Outcome(False, "complete", initial.detail, initial.counts)
        if (
            completion_record is None
            or journal_raw is None
            or _sha256(journal_raw) != completion_record[0].journal_sha256
        ):
            return Outcome(False, "complete", initial.detail, initial.counts)

    try:
        with (
            legacy_migration.profile_transaction(timeout=LOCK_TIMEOUT_SECONDS),
            _transaction_locks(),
        ):
            return _ensure_locked(cutover=cutover)
    except legacy_migration.MigrationError as error:
        raise TransactionError(
            f"cannot acquire the profile-wide upgrade exclusion: {error}",
            status="retry",
        ) from error
