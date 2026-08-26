"""Generation-bound compatibility for captures made by the deployed app.

The previously deployed Python application named video captures by basename.
That convention is useful evidence during the explicit upgrade transaction,
but it is not safe as a permanent lookup rule: two videos can share a stem and
a later user file can occupy the old name.  The migration writer therefore
records the fully validated relationships in one central manifest and places
an exact authority sidecar beside every adopted capture.

This module is deliberately the read/use half only.  It never creates,
replaces, moves, or removes a file.  A non-strict load is suitable for a normal
library scan: a removed or replaced media generation simply revokes that one
relationship.  A migration verifier uses ``strict=True`` so any incomplete
publication is reported instead.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from wall_in_one import file_io, paths
from wall_in_one.library import pairing

MANIFEST_FILENAME: Final = "deployed-capture-adoption-v1.json"
MANIFEST_VERSION: Final = 1
PLUGIN_ID: Final = "goober/wall-in-one"
MANIFEST_KIND: Final = "deployed-capture-adoption"
SIDECAR_SCHEMA: Final = 2
SIDECAR_KIND: Final = "deployed-automatic-still"
AUTOMATIC_MARKER_FILENAME: Final = ".managed-by-wall-in-one-v1.json"

MAX_MANIFEST_BYTES: Final = 16 * 1024 * 1024
MAX_MARKER_BYTES: Final = 64 * 1024
MAX_SIDECAR_BYTES: Final = 64 * 1024
MAX_BINDINGS: Final = 4096
MAX_PATH_BYTES: Final = 4096
MAX_CAPTURE_BYTES: Final = 512 * 1024 * 1024
MAX_TOTAL_CAPTURE_BYTES: Final = 8 * 1024 * 1024 * 1024

_HEX: Final = frozenset("0123456789abcdef")
_MANIFEST_KEYS: Final = frozenset(
    {
        "version",
        "plugin",
        "kind",
        "adoption_id",
        "root",
        "root_identity",
        "automatic_stills",
        "automatic_stills_identity",
        "marker_path",
        "marker_fingerprint",
        "marker_sha256",
        "bindings",
        "bindings_sha256",
    }
)
_BINDING_KEYS: Final = frozenset(
    {
        "source_path",
        "capture_path",
        "dynamic_id",
        "source_fingerprint",
        "capture_fingerprint",
        "capture_size",
        "capture_sha256",
    }
)
_SIDECAR_KEYS: Final = frozenset(
    {
        "schema",
        "plugin",
        "kind",
        "adoption_id",
        "path",
        "source_path",
        "dynamic_id",
        "source_fingerprint",
        "capture_fingerprint",
        "capture_size",
        "capture_sha256",
    }
)
_MARKER_KEYS: Final = frozenset({"schema", "plugin", "kind", "ownership"})


class AdoptionError(Exception):
    """The adoption manifest is malformed or its strict proof is incomplete."""


class _ChangedAdoptionError(Exception):
    """Valid persisted evidence no longer matches the current filesystem."""


@dataclass(frozen=True, slots=True)
class Authority:
    """One exact basename capture adopted as a generated child of a video."""

    source_path: Path
    capture_path: Path
    source_fingerprint: file_io.FileFingerprint
    capture_fingerprint: file_io.FileFingerprint
    capture_size: int
    capture_sha256: str
    # Populated only by ``authority_for``.  They are intentionally absent
    # from the persisted binding document: the canonical adjacent sidecar is
    # derived publication evidence, while the manifest binds the media and
    # capture generations.  A destructive caller needs both values so it can
    # carry the exact validation across a retained-root handoff without
    # rediscovering a basename or blessing a later sidecar generation.
    adoption_id: str = field(default="", compare=False, repr=False)
    sidecar_fingerprint: file_io.FileFingerprint | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    @property
    def dynamic_id(self) -> str:
        return f"video:{self.source_path}"

    @property
    def sidecar_path(self) -> Path:
        return self.capture_path.with_name(self.capture_path.name + pairing.SIDECAR_SUFFIX)


@dataclass(frozen=True, slots=True)
class Adoption:
    """The central adoption proof and the bindings still exact on this scan."""

    adoption_id: str
    root: Path
    root_identity: file_io.PathIdentity
    automatic_stills: Path
    automatic_stills_identity: file_io.PathIdentity
    marker_path: Path
    marker_fingerprint: file_io.FileFingerprint
    marker_sha256: str
    authorities: tuple[Authority, ...]

    @property
    def mapping(self) -> Mapping[Path, Path]:
        return {authority.source_path: authority.capture_path for authority in self.authorities}

    @property
    def pairs(self) -> tuple[tuple[Path, Path], ...]:
        return tuple(
            (authority.source_path, authority.capture_path) for authority in self.authorities
        )


def state_path() -> Path:
    """The central compatibility record below the app state directory."""
    return paths.app_state_dir() / MANIFEST_FILENAME


def canonical_bytes(document: object) -> bytes:
    """Render deterministic UTF-8 JSON used by manifests and authority files."""
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def binding_document(authority: Authority) -> dict[str, object]:
    """Return the canonical manifest entry for ``authority``."""
    return {
        "source_path": str(authority.source_path),
        "capture_path": str(authority.capture_path),
        "dynamic_id": authority.dynamic_id,
        "source_fingerprint": list(authority.source_fingerprint),
        "capture_fingerprint": list(authority.capture_fingerprint),
        "capture_size": authority.capture_size,
        "capture_sha256": authority.capture_sha256,
    }


def sidecar_document(authority: Authority, adoption_id: str) -> dict[str, object]:
    """Return the exact adjacent authority document a migration writer installs."""
    return {
        "schema": SIDECAR_SCHEMA,
        "plugin": PLUGIN_ID,
        "kind": SIDECAR_KIND,
        "adoption_id": adoption_id,
        "path": str(authority.capture_path),
        "source_path": str(authority.source_path),
        "dynamic_id": authority.dynamic_id,
        "source_fingerprint": list(authority.source_fingerprint),
        "capture_fingerprint": list(authority.capture_fingerprint),
        "capture_size": authority.capture_size,
        "capture_sha256": authority.capture_sha256,
    }


def render_sidecar(authority: Authority, adoption_id: str) -> bytes:
    """Render the byte-exact sidecar accepted by :func:`load`."""
    return canonical_bytes(sidecar_document(authority, adoption_id))


def bindings_digest(authorities: Sequence[Authority]) -> str:
    """Digest a binding sequence exactly as represented in the manifest."""
    documents = [binding_document(authority) for authority in authorities]
    return hashlib.sha256(canonical_bytes(documents)).hexdigest()


def adoption_id_for(adoption: Adoption) -> str:
    """Bind one publication id to all immutable manifest identity evidence.

    The id itself and the redundant bindings digest are excluded.  Everything
    they attest is represented directly, including the canonical binding
    documents, so neither a writer nor a later reader can assign an arbitrary
    valid-looking id to a different capture generation.
    """
    identity = {
        "version": MANIFEST_VERSION,
        "plugin": PLUGIN_ID,
        "kind": MANIFEST_KIND,
        "root": str(adoption.root),
        "root_identity": list(adoption.root_identity),
        "automatic_stills": str(adoption.automatic_stills),
        "automatic_stills_identity": list(adoption.automatic_stills_identity),
        "marker_path": str(adoption.marker_path),
        "marker_fingerprint": list(adoption.marker_fingerprint),
        "marker_sha256": adoption.marker_sha256,
        "bindings": [binding_document(authority) for authority in adoption.authorities],
    }
    return hashlib.sha256(canonical_bytes(identity)).hexdigest()


def manifest_document(adoption: Adoption) -> dict[str, object]:
    """Return a complete central manifest for a migration writer."""
    expected_adoption_id = adoption_id_for(adoption)
    if adoption.adoption_id != expected_adoption_id:
        raise AdoptionError(
            "adoption_id does not match the manifest identities and canonical bindings"
        )
    bindings = [binding_document(authority) for authority in adoption.authorities]
    return {
        "version": MANIFEST_VERSION,
        "plugin": PLUGIN_ID,
        "kind": MANIFEST_KIND,
        "adoption_id": adoption.adoption_id,
        "root": str(adoption.root),
        "root_identity": list(adoption.root_identity),
        "automatic_stills": str(adoption.automatic_stills),
        "automatic_stills_identity": list(adoption.automatic_stills_identity),
        "marker_path": str(adoption.marker_path),
        "marker_fingerprint": list(adoption.marker_fingerprint),
        "marker_sha256": adoption.marker_sha256,
        "bindings": bindings,
        "bindings_sha256": hashlib.sha256(canonical_bytes(bindings)).hexdigest(),
    }


def render_manifest(adoption: Adoption) -> bytes:
    """Render the deterministic central manifest accepted by :func:`load`."""
    return canonical_bytes(manifest_document(adoption))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON key {key!r}")
        document[key] = value
    return document


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number {value!r}")


def _reject_json_float(value: str) -> object:
    raise ValueError(f"floating-point JSON number {value!r}")


def _strict_json(raw: bytes) -> object:
    """Parse the integer-only JSON vocabulary used by adoption evidence."""
    return json.loads(
        raw,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_json_constant,
        parse_float=_reject_json_float,
    )


def _json_object(raw: bytes, target: Path) -> dict[str, Any]:
    try:
        parsed = _strict_json(raw)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise AdoptionError(
            f"adoption manifest {target} is not strict duplicate-free JSON"
        ) from error
    if not isinstance(parsed, dict):
        raise AdoptionError(f"adoption manifest {target} is not a JSON object")
    return parsed


def _validate_marker_document(raw: bytes, target: Path) -> None:
    """Require the predecessor Automatic Stills ownership marker exactly."""
    try:
        parsed = _strict_json(raw)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise _ChangedAdoptionError(
            f"Automatic Stills marker {target} is not strict duplicate-free JSON"
        ) from error
    if (
        not isinstance(parsed, dict)
        or set(parsed) != _MARKER_KEYS
        or type(parsed["schema"]) is not int
        or parsed["schema"] != 1
        or parsed["plugin"] != PLUGIN_ID
        or parsed["kind"] != "automatic-stills"
        or parsed["ownership"] != "managed"
    ):
        raise _ChangedAdoptionError(
            f"Automatic Stills marker {target} does not have the exact ownership identity"
        )


def _hex(value: object, *, length: int, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in _HEX for character in value)
    ):
        raise AdoptionError(f"{label} must be {length} lowercase hexadecimal characters")
    return value


def _absolute_path(value: object, *, label: str) -> Path:
    if not isinstance(value, str):
        raise AdoptionError(f"{label} is not a path")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise AdoptionError(f"{label} is not valid UTF-8") from error
    if (
        not value
        or len(encoded) > MAX_PATH_BYTES
        or any(ord(character) < 32 or 0x7F <= ord(character) <= 0x9F for character in value)
    ):
        raise AdoptionError(f"{label} is empty, too long, or contains control characters")
    path = Path(value)
    if not path.is_absolute() or path != Path(os.path.normpath(value)):
        raise AdoptionError(f"{label} is not a normalized absolute path")
    return path


def _integer_tuple(value: object, *, length: int, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) != length:
        raise AdoptionError(f"{label} must contain exactly {length} integers")
    if any(type(component) is not int or component < 0 for component in value):
        raise AdoptionError(f"{label} contains an invalid integer")
    return tuple(value)


def _identity(value: object, *, label: str) -> file_io.PathIdentity:
    parsed = _integer_tuple(value, length=2, label=label)
    return parsed[0], parsed[1]


def _fingerprint(value: object, *, label: str) -> file_io.FileFingerprint:
    parsed = _integer_tuple(value, length=5, label=label)
    return parsed[0], parsed[1], parsed[2], parsed[3], parsed[4]


def _parse_binding(raw: object, automatic_stills: Path, ordinal: int) -> Authority:
    label = f"adoption binding {ordinal}"
    if not isinstance(raw, dict) or set(raw) != _BINDING_KEYS:
        raise AdoptionError(f"{label} has an incomplete or ambiguous shape")
    source = _absolute_path(raw["source_path"], label=f"{label} source_path")
    capture = _absolute_path(raw["capture_path"], label=f"{label} capture_path")
    if capture.parent != automatic_stills:
        raise AdoptionError(f"{label} capture is not a direct child of Automatic Stills")
    if capture.name != f"{source.stem}.png":
        raise AdoptionError(f"{label} capture is not the exact predecessor basename")
    if source == capture:
        raise AdoptionError(f"{label} source and capture are the same path")
    dynamic_id = raw["dynamic_id"]
    if dynamic_id != f"video:{source}":
        raise AdoptionError(f"{label} has a mismatched dynamic_id")
    source_fingerprint = _fingerprint(
        raw["source_fingerprint"], label=f"{label} source_fingerprint"
    )
    capture_fingerprint = _fingerprint(
        raw["capture_fingerprint"], label=f"{label} capture_fingerprint"
    )
    capture_size = raw["capture_size"]
    if (
        type(capture_size) is not int
        or capture_size <= 0
        or capture_size > MAX_CAPTURE_BYTES
        or capture_size != capture_fingerprint[2]
    ):
        raise AdoptionError(f"{label} has an invalid capture_size")
    capture_sha256 = _hex(raw["capture_sha256"], length=64, label=f"{label} capture_sha256")
    return Authority(
        source_path=source,
        capture_path=capture,
        source_fingerprint=source_fingerprint,
        capture_fingerprint=capture_fingerprint,
        capture_size=capture_size,
        capture_sha256=capture_sha256,
    )


def _parse_manifest(document: dict[str, Any], target: Path) -> Adoption:
    if set(document) != _MANIFEST_KEYS:
        raise AdoptionError(f"adoption manifest {target} has an incomplete or ambiguous shape")
    if (
        type(document["version"]) is not int
        or document["version"] != MANIFEST_VERSION
        or document["plugin"] != PLUGIN_ID
        or document["kind"] != MANIFEST_KIND
    ):
        raise AdoptionError(f"adoption manifest {target} has an unsupported identity")
    adoption_id = _hex(document["adoption_id"], length=64, label="adoption_id")
    root = _absolute_path(document["root"], label="adoption root")
    automatic_stills = _absolute_path(
        document["automatic_stills"], label="adoption automatic_stills"
    )
    if automatic_stills != pairing.still_directory(root):
        raise AdoptionError("adoption automatic_stills does not match its root")
    marker_path = _absolute_path(document["marker_path"], label="adoption marker_path")
    if marker_path != automatic_stills / AUTOMATIC_MARKER_FILENAME:
        raise AdoptionError("adoption marker_path is not the exact Automatic Stills marker")
    raw_bindings = document["bindings"]
    if not isinstance(raw_bindings, list) or len(raw_bindings) > MAX_BINDINGS:
        raise AdoptionError(f"adoption bindings must be a list of at most {MAX_BINDINGS}")
    authorities = tuple(
        _parse_binding(raw, automatic_stills, ordinal)
        for ordinal, raw in enumerate(raw_bindings, start=1)
    )
    declared_digest = _hex(document["bindings_sha256"], length=64, label="bindings_sha256")
    try:
        actual_digest = bindings_digest(authorities)
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise AdoptionError("adoption bindings cannot be canonicalized safely") from error
    if declared_digest != actual_digest:
        raise AdoptionError("adoption bindings_sha256 does not match the binding list")
    sources = [authority.source_path for authority in authorities]
    captures = [authority.capture_path for authority in authorities]
    if len(set(sources)) != len(sources):
        raise AdoptionError("adoption manifest contains a duplicate source path")
    if len(set(captures)) != len(captures):
        raise AdoptionError("adoption manifest contains a duplicate capture path")
    if sum(authority.capture_size for authority in authorities) > MAX_TOTAL_CAPTURE_BYTES:
        raise AdoptionError("adoption captures exceed the total byte ceiling")
    adoption = Adoption(
        adoption_id=adoption_id,
        root=root,
        root_identity=_identity(document["root_identity"], label="root_identity"),
        automatic_stills=automatic_stills,
        automatic_stills_identity=_identity(
            document["automatic_stills_identity"], label="automatic_stills_identity"
        ),
        marker_path=marker_path,
        marker_fingerprint=_fingerprint(document["marker_fingerprint"], label="marker_fingerprint"),
        marker_sha256=_hex(document["marker_sha256"], length=64, label="marker_sha256"),
        authorities=authorities,
    )
    try:
        expected_adoption_id = adoption_id_for(adoption)
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise AdoptionError("adoption identity cannot be canonicalized safely") from error
    if adoption.adoption_id != expected_adoption_id:
        raise AdoptionError(
            "adoption_id does not match the manifest identities and canonical bindings"
        )
    return adoption


def _validate_sidecar_document(
    raw: bytes,
    target: Path,
    authority: Authority,
    adoption_id: str,
) -> None:
    """Require a canonical sidecar for this exact adopted generation."""
    try:
        parsed = _strict_json(raw)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise _ChangedAdoptionError(
            f"adoption sidecar {target} is not strict duplicate-free JSON"
        ) from error
    if not isinstance(parsed, dict) or set(parsed) != _SIDECAR_KEYS:
        raise _ChangedAdoptionError(
            f"adoption sidecar {target} has an incomplete or ambiguous shape"
        )
    try:
        if (
            type(parsed["schema"]) is not int
            or parsed["schema"] != SIDECAR_SCHEMA
            or parsed["plugin"] != PLUGIN_ID
            or parsed["kind"] != SIDECAR_KIND
            or _hex(parsed["adoption_id"], length=64, label="sidecar adoption_id") != adoption_id
        ):
            raise AdoptionError("sidecar has an unsupported or mismatched identity")
        parsed_authority = _parse_binding(
            {
                "source_path": parsed["source_path"],
                "capture_path": parsed["path"],
                "dynamic_id": parsed["dynamic_id"],
                "source_fingerprint": parsed["source_fingerprint"],
                "capture_fingerprint": parsed["capture_fingerprint"],
                "capture_size": parsed["capture_size"],
                "capture_sha256": parsed["capture_sha256"],
            },
            authority.capture_path.parent,
            1,
        )
        if parsed_authority != authority:
            raise AdoptionError("sidecar does not describe the adopted generation")
        canonical = canonical_bytes(parsed)
        expected = render_sidecar(authority, adoption_id)
    except (AdoptionError, TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise _ChangedAdoptionError(
            f"adoption sidecar {target} is not valid canonical authority"
        ) from error
    if raw != canonical or canonical != expected:
        raise _ChangedAdoptionError(f"adoption sidecar bytes changed at {target}")


def _owned_regular(
    pin: file_io.PinnedPath,
    *,
    label: str,
    private_authority: bool = False,
) -> os.stat_result:
    status = pin.status()
    if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid() or status.st_nlink != 1:
        raise _ChangedAdoptionError(f"{label} is not a single-link regular file owned by this user")
    if private_authority and status.st_mode & 0o022:
        raise _ChangedAdoptionError(f"{label} is writable by another user or group")
    return status


def _same_public_file(
    pin: file_io.PinnedPath,
    logical: Path,
    *,
    label: str,
    expected_fingerprint: file_io.FileFingerprint | None = None,
) -> None:
    try:
        current = logical.lstat()
    except OSError as error:
        raise _ChangedAdoptionError(f"{label} is no longer present at {logical}") from error
    try:
        pinned = pin.status()
        pinned_fingerprint = file_io.file_fingerprint(pinned)
        current_fingerprint = file_io.file_fingerprint(current)
    except ValueError as error:
        raise _ChangedAdoptionError(f"{label} is no longer a regular file at {logical}") from error
    if (
        pinned.st_uid != os.getuid()
        or current.st_uid != os.getuid()
        or pinned.st_nlink != 1
        or current.st_nlink != 1
        or current_fingerprint != pinned_fingerprint
        or (expected_fingerprint is not None and pinned_fingerprint != expected_fingerprint)
    ):
        raise _ChangedAdoptionError(f"{label} changed while its generation was validated")


def _pin_absolute_regular(
    path: Path,
    expected: file_io.FileFingerprint | None = None,
) -> file_io.PinnedPath:
    scoped = file_io.pin_regular_path_beneath(Path(path.anchor), path)
    pin = scoped.source
    try:
        if expected is not None and pin.fingerprint != expected:
            raise file_io.PathChangedError(f"{path} does not match its adopted generation")
    except BaseException:
        pin.close()
        raise
    return pin


def _validate_global(
    adoption: Adoption,
    stack: contextlib.ExitStack,
) -> tuple[file_io.PinnedDirectoryContext, file_io.PinnedPath]:
    context = stack.enter_context(
        file_io.pin_directory_beneath(
            adoption.root,
            adoption.automatic_stills,
            expected_root_identity=adoption.root_identity,
            expected_directory_identity=adoption.automatic_stills_identity,
        )
    )
    root_status = os.fstat(context.root_descriptor)
    automatic_status = os.fstat(context.directory_descriptor)
    if root_status.st_uid != os.getuid() or automatic_status.st_uid != os.getuid():
        raise _ChangedAdoptionError("adopted root or Automatic Stills is not owned by this user")
    marker_pin = stack.enter_context(
        file_io.pin_regular_path(
            context.child(adoption.marker_path.name),
            expected_fingerprint=adoption.marker_fingerprint,
        )
    )
    _owned_regular(
        marker_pin,
        label="Automatic Stills marker",
        private_authority=True,
    )
    marker = file_io.read_pinned_regular_bytes(
        marker_pin,
        MAX_MARKER_BYTES,
        expected_fingerprint=adoption.marker_fingerprint,
    )
    if hashlib.sha256(marker).hexdigest() != adoption.marker_sha256:
        raise _ChangedAdoptionError("Automatic Stills marker bytes changed after adoption")
    _validate_marker_document(marker, adoption.marker_path)
    context.verify_public()
    _same_public_file(
        marker_pin,
        adoption.marker_path,
        label="Automatic Stills marker",
        expected_fingerprint=adoption.marker_fingerprint,
    )
    return context, marker_pin


def _validate_authority(
    adoption: Adoption,
    authority: Authority,
    directory: file_io.PinnedDirectoryContext,
    *,
    verify_contents: bool,
) -> file_io.FileFingerprint:
    with contextlib.ExitStack() as stack:
        source_pin = stack.enter_context(
            _pin_absolute_regular(authority.source_path, authority.source_fingerprint)
        )
        capture_pin = stack.enter_context(
            file_io.pin_regular_path(
                directory.child(authority.capture_path.name),
                expected_fingerprint=authority.capture_fingerprint,
            )
        )
        sidecar_pin = stack.enter_context(
            file_io.pin_regular_path(directory.child(authority.sidecar_path.name))
        )
        _owned_regular(source_pin, label=f"adopted source {authority.source_path}")
        _owned_regular(capture_pin, label=f"adopted capture {authority.capture_path}")
        _owned_regular(
            sidecar_pin,
            label=f"adoption sidecar {authority.sidecar_path}",
            private_authority=True,
        )
        if verify_contents:
            size, digest = file_io.hash_pinned_regular(
                capture_pin,
                expected_fingerprint=authority.capture_fingerprint,
                maximum_bytes=authority.capture_size,
            )
            if (size, digest) != (authority.capture_size, authority.capture_sha256):
                raise _ChangedAdoptionError(
                    f"adopted capture bytes changed at {authority.capture_path}"
                )
        sidecar_fingerprint = sidecar_pin.fingerprint
        sidecar = file_io.read_pinned_regular_bytes(
            sidecar_pin,
            MAX_SIDECAR_BYTES,
            expected_fingerprint=sidecar_fingerprint,
        )
        _validate_sidecar_document(
            sidecar,
            authority.sidecar_path,
            authority,
            adoption.adoption_id,
        )
        directory.verify_public()
        _same_public_file(
            source_pin,
            authority.source_path,
            label="adopted source",
            expected_fingerprint=authority.source_fingerprint,
        )
        _same_public_file(
            capture_pin,
            authority.capture_path,
            label="adopted capture",
            expected_fingerprint=authority.capture_fingerprint,
        )
        _same_public_file(
            sidecar_pin,
            authority.sidecar_path,
            label="adoption sidecar",
            expected_fingerprint=sidecar_fingerprint,
        )
        directory.verify_public()
        return sidecar_fingerprint


def _read_manifest(target: Path) -> bytes | None:
    if not target.is_absolute():
        raise AdoptionError(f"adoption manifest path is not absolute: {target}")
    try:
        pin = _pin_absolute_regular(target)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise AdoptionError(f"cannot safely pin adoption manifest {target}: {error}") from error
    try:
        try:
            _owned_regular(
                pin,
                label="adoption manifest",
                private_authority=True,
            )
            fingerprint = pin.fingerprint
            raw = file_io.read_pinned_regular_bytes(
                pin,
                MAX_MANIFEST_BYTES,
                expected_fingerprint=fingerprint,
            )
            _same_public_file(
                pin,
                target,
                label="adoption manifest",
                expected_fingerprint=fingerprint,
            )
            return raw
        except (OSError, _ChangedAdoptionError) as error:
            raise AdoptionError(
                f"cannot safely read adoption manifest {target}: {error}"
            ) from error
    finally:
        pin.close()


def load(*, strict: bool = False, path: Path | None = None) -> Adoption | None:
    """Load the completed capture adoption, validating current generations.

    An absent manifest means this profile has no adopted basename captures.
    Manifest syntax and internal digests are always strict.  With the normal
    ``strict=False`` policy, filesystem changes revoke only the affected
    bindings; global root/marker replacement revokes all of them.  Migration
    verification uses ``strict=True`` to reject either condition.
    """
    target = path if path is not None else state_path()
    raw = _read_manifest(target)
    if raw is None:
        return None
    adoption = _parse_manifest(_json_object(raw, target), target)
    try:
        with contextlib.ExitStack() as stack:
            directory, marker_pin = _validate_global(adoption, stack)
            exact: list[Authority] = []
            for authority in adoption.authorities:
                try:
                    _validate_authority(
                        adoption,
                        authority,
                        directory,
                        verify_contents=strict,
                    )
                except (OSError, ValueError, _ChangedAdoptionError) as error:
                    if strict:
                        raise AdoptionError(
                            f"adopted capture binding for {authority.source_path} is no longer "
                            f"exact: {error}"
                        ) from error
                    continue
                exact.append(authority)
            directory.verify_public()
            _same_public_file(
                marker_pin,
                adoption.marker_path,
                label="Automatic Stills marker",
                expected_fingerprint=adoption.marker_fingerprint,
            )
            return replace(adoption, authorities=tuple(exact))
    except AdoptionError:
        raise
    except (OSError, ValueError, _ChangedAdoptionError) as error:
        if strict:
            raise AdoptionError(
                f"capture adoption filesystem proof is no longer exact: {error}"
            ) from error
        return replace(adoption, authorities=())


def authority_for(
    source: Path,
    *,
    capture: Path | None = None,
    verify_contents: bool = False,
) -> Authority | None:
    """Return one still-exact adopted relationship for an authority check.

    This deliberately validates only the requested binding.  Normal callers
    can rely on the generation fingerprints and exact authority sidecar;
    destructive callers opt into a content hash without paying to hash every
    other adopted capture in the manifest.

    A missing manifest, unknown relationship, or changed filesystem proof has
    no authority and returns ``None``.  A malformed central manifest remains
    an :class:`AdoptionError`, because silently treating ambiguous persisted
    state as an ordinary revocation would hide a broken migration publication.
    """
    target = state_path()
    raw = _read_manifest(target)
    if raw is None:
        return None
    adoption = _parse_manifest(_json_object(raw, target), target)
    selected = tuple(
        authority
        for authority in adoption.authorities
        if authority.source_path == source
        and (capture is None or authority.capture_path == capture)
    )
    if len(selected) != 1:
        return None

    try:
        with contextlib.ExitStack() as stack:
            directory, marker_pin = _validate_global(adoption, stack)
            authority = selected[0]
            sidecar_fingerprint = _validate_authority(
                adoption,
                authority,
                directory,
                verify_contents=verify_contents,
            )
            directory.verify_public()
            _same_public_file(
                marker_pin,
                adoption.marker_path,
                label="Automatic Stills marker",
                expected_fingerprint=adoption.marker_fingerprint,
            )
            return replace(
                authority,
                adoption_id=adoption.adoption_id,
                sidecar_fingerprint=sidecar_fingerprint,
            )
    except OSError, ValueError, _ChangedAdoptionError:
        return None
