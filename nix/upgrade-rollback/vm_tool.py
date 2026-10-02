"""Guest-side half of the upgrade-rollback VM test.

Runs inside the disposable VM with the guest's Python 3.14 and
``tests/golden/harness.py`` on ``PYTHONPATH``; it never runs on a host.

``seed --fixture DIR --home HOME``
    Materialize the golden profile at ``HOME`` exactly as
    :func:`harness.materialize` does in the sandbox -- same entries, same
    relocation of ``/home/user`` and of the path-derived tokens, same re-seal
    of the deployed-upgrade evidence -- except that the home is the VM user's
    real one rather than ``root/home``. The re-seal imports
    ``wall_in_one.library.adopted`` from whichever package is on
    ``PYTHONPATH`` (byte-identical in v0.1.4 and this build).

``snapshot --home HOME [--out FILE]``
    Print (or write) every object below the trees Wall-in-One owns or reads
    as its library, in :func:`harness.snapshot`'s shape, as JSON. Small files
    carry their bytes so the test driver can verify allowed writes.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any, Final

import harness

#: Below the home. Noctalia's own directory is deliberately not here: the VM's
#: shell settings come from vm-base.nix (the fixture's Noctalia files are not
#: seeded), and the shell itself rewrites them.
SCOPES: Final = (
    ".config/wall-in-one",
    ".local/state/wall-in-one",
    ".cache/wall-in-one",
    "Pictures",
    ".local/share/Steam",
)
APP_TREES: Final = (".config/wall-in-one", ".local/state/wall-in-one", ".cache/wall-in-one")


def seed(fixture: Path, home: Path) -> None:
    for tree in APP_TREES:
        if (home / tree).exists():
            raise SystemExit(f"refusing to seed over an existing {home / tree}")
    original_home, wanted = harness.entries(fixture)

    # The same path inventory harness.materialize relocates with.
    paths: set[str] = set()
    for entry in wanted:
        paths.add(entry.path)
        parent = entry.path
        while parent != original_home and parent.startswith(original_home + "/"):
            parent = parent.rsplit("/", 1)[0]
            paths.add(parent)
        text = harness._decoded_text(entry.data) if entry.kind == "file" else None
        if text is not None:
            paths.update(harness._embedded_paths(text, original_home))
    relocation = harness.Relocation(original_home, str(home), paths)

    for xdg in harness.XDG_LAYOUT.values():
        (home / xdg).mkdir(parents=True, exist_ok=True)
    directory_modes: list[tuple[Path, int]] = []
    for entry in wanted:
        target = Path(relocation.full_path(entry.path))
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == "dir":
            target.mkdir(exist_ok=True)
            directory_modes.append((target, entry.mode))
        elif entry.kind == "symlink":
            target.symlink_to(relocation.text(entry.target))
        else:
            text = harness._decoded_text(entry.data)
            data = relocation.text(text).encode("utf-8") if text is not None else entry.data
            with open(target, "wb") as handle:
                handle.write(data)
                if entry.sparse_size:
                    handle.truncate(entry.sparse_size)
            target.chmod(entry.mode)
            os.utime(target, ns=(entry.mtime_ns, entry.mtime_ns))
    harness.reseal_deployed_markers(home / harness.XDG_LAYOUT["state"] / "wall-in-one")
    for directory, mode in sorted(directory_modes, key=lambda item: -len(item[0].parts)):
        directory.chmod(mode)


def snapshot(home: Path) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for scope in SCOPES:
        root = home / scope
        if not root.is_dir():
            continue
        for relative, node in harness.snapshot(root).items():
            found[f"{scope}/{relative}"] = {
                "kind": node.kind,
                "mode": node.mode,
                "inode": node.inode,
                "mtime_ns": node.mtime_ns,
                "size": node.size,
                "digest": node.digest,
                "content": None
                if node.content is None
                else base64.b64encode(node.content).decode("ascii"),
                "target": node.target,
            }
    return found


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="vm_tool")
    commands = parser.add_subparsers(dest="command", required=True)
    seeding = commands.add_parser("seed")
    seeding.add_argument("--fixture", type=Path, required=True)
    seeding.add_argument("--home", type=Path, required=True)
    snapping = commands.add_parser("snapshot")
    snapping.add_argument("--home", type=Path, required=True)
    snapping.add_argument("--out", type=Path)
    options = parser.parse_args(arguments)
    if options.command == "seed":
        seed(options.fixture, options.home)
        return 0
    text = json.dumps(snapshot(options.home), sort_keys=True)
    if options.out is None:
        sys.stdout.write(text + "\n")
    else:
        options.out.parent.mkdir(parents=True, exist_ok=True)
        options.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
