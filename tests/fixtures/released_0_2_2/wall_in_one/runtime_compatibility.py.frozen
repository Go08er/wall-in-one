"""Refuse newer configuration while an incompatible session runtime is alive.

This is a publication guard, not an updater. It never launches, stops, or
replaces a process. Offline preparation also checks the next service's loaded
definitions; an old or unverified generation must be resolved before a new
feature is enabled.
"""

from __future__ import annotations

import json
import time

from wall_in_one import predecessor_process, update_status
from wall_in_one.control import client


class RuntimeCompatibilityError(Exception):
    """A live runtime has not demonstrated support for the proposed schema."""


def _require_current_loaded_units(deadline: float) -> None:
    loaded = update_status.inspect_loaded_units(timeout=deadline - time.monotonic())
    if loaded.state not in ("current", "absent", "unmanaged"):
        raise RuntimeCompatibilityError(
            "Cannot confirm that the next wallpaper service start will use this update. "
            "No changes were saved. Run `wall-in-one --update-status` and finish updating "
            "the loaded service definitions through your installation's configuration owner. "
            "Pinned commands and custom overrides were left unchanged."
        )


def require_schema(schema: int) -> None:
    """Check the live runtime without falling back to or starting the GUI.

    Only additive schemas need this check. Schema 4 is the existing published
    contract. A missing socket allows an offline document to be prepared, but
    timeouts, malformed replies and ambiguous ownership never count as absence.
    The caller must run this on its I/O worker before committing any bytes.
    """
    if schema <= 4:
        return
    deadline = time.monotonic() + client.TIMEOUT
    try:
        response = client.send_runtime("status", timeout=client.TIMEOUT)
    except client.NotRunningError:
        # A predecessor can be alive before bind, or its bound socket name can
        # have been unlinked. A failed connection alone is not offline proof.
        # Reuse migration's bounded read-only process/kernel socket inspection;
        # this still cannot exclude a later concurrent launch by an old owner.
        try:
            predecessor_process.refuse_live_predecessor_runtime()
        except predecessor_process.PredecessorProcessError as error:
            raise RuntimeCompatibilityError(
                "Cannot confirm wallpaper service compatibility. No changes were saved; "
                "a service may still be starting or running. Finish updating it, then try again."
            ) from error
        _require_current_loaded_units(deadline)
        return
    except client.ControlError as error:
        raise RuntimeCompatibilityError(
            "Cannot confirm wallpaper service compatibility. No changes were saved; "
            "wait for the service to respond, or finish updating it, then try again."
        ) from error
    try:
        status = json.loads(response.message) if response.ok else None
    except ValueError, RecursionError:
        status = None
    supported = status.get("supported_config_schemas") if isinstance(status, dict) else None
    if (
        isinstance(status, dict)
        and type(status.get("status_version")) is int
        and status["status_version"] == 2
        and isinstance(supported, list)
        and 0 < len(supported) <= 16
        and all(type(version) is int and version > 0 for version in supported)
        and schema in supported
    ):
        _require_current_loaded_units(deadline)
        return
    raise RuntimeCompatibilityError(
        "The running wallpaper service does not support this setting yet. "
        "No changes were saved. Finish updating the wallpaper service, then try again."
    )
