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
    as its library, and Noctalia's, in :func:`harness.snapshot`'s shape, as
    JSON. Small files carry their bytes so the test driver can verify allowed
    writes.

The test driver also imports this module, for :func:`nodes` and
:func:`shell_writes`, which judge Noctalia's settings field by field.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tomllib
from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Final

import harness

NOCTALIA_SETTINGS: Final = ".local/state/noctalia/settings.toml"

#: Below the home. Noctalia's trees are here too: the golden profile seeds
#: its settings.toml (with the palette-template registration the app reads and
#: may rewrite), and the app writes custom palettes below its config tree.
#: What the shell itself rewrites is set aside by :func:`shell_writes`.
SCOPES: Final = (
    ".config/wall-in-one",
    ".local/state/wall-in-one",
    ".cache/wall-in-one",
    "Pictures",
    ".local/share/Steam",
    ".local/state/noctalia",
    ".config/noctalia",
)

#: The fields of Noctalia's settings.toml the shell itself writes whenever a
#: service applies a wallpaper, each with the reason. A ``*`` stands for any
#: one key (the output's name); the tables holding these fields may appear.
#: Every other key must keep its value in every phase.
SHELL_FIELDS: Final[Mapping[tuple[str, ...], str]] = {
    ("wallpaper", "last", "path"): "Noctalia records the wallpaper last applied",
    ("wallpaper", "default", "path"): "Noctalia records the wallpaper applied to every output",
    ("wallpaper", "monitors", "*", "path"): "Noctalia records the wallpaper per output",
}

#: What Noctalia changes only the first time it starts on the fixture's
#: settings (version 3, older than the VM's shell), and so is allowed only
#: across that start. It also reformats the whole file then, so untouched
#: keys keep their values there, not their bytes. ``**`` is a whole table.
SHELL_MIGRATION: Final[Mapping[tuple[str, ...], str]] = {
    ("config_version",): "Noctalia migrates the fixture's version-3 settings (to 14)",
    ("lockscreen_widgets", "**"): "the migration adds the lock-screen layout for this output",
}

#: Files below Noctalia's trees that only the shell writes, with the reason.
SHELL_FILES: Final[Mapping[str, str]] = {
    ".local/state/noctalia/state.toml": "Noctalia's own runtime state",
    ".local/state/noctalia/community-palettes": "its community palette catalog (offline: empty)",
    ".local/state/noctalia/community-palettes/.catalog": "its community palette catalog",
    ".local/state/noctalia/community-templates": "its community template catalog",
    ".local/state/noctalia/plugins": "its plugin sources",
    ".local/state/noctalia/plugins/sources": "its plugin sources",
    ".local/state/noctalia/plugins/sources/*": "its plugin sources (official, community)",
}
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


def nodes(raw: Mapping[str, Mapping[str, Any]]) -> dict[str, harness.Node]:
    """A :func:`snapshot` (as parsed JSON) as :func:`harness.diff` takes it."""
    found: dict[str, harness.Node] = {}
    for path, value in raw.items():
        data = value["content"]
        found[path] = harness.Node(
            value["kind"],
            value["mode"],
            value["inode"],
            value["mtime_ns"],
            value["size"],
            value["digest"],
            None if data is None else base64.b64decode(data),
            value["target"],
        )
    return found


def _leaves(table: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """Every key of a TOML document by its dotted path; a table also as itself."""
    found: dict[tuple[str, ...], Any] = {}
    for key, value in table.items():
        path = (*prefix, key)
        if isinstance(value, dict):
            found[path] = "<table>"
            found.update(_leaves(value, path))
        else:
            found[path] = value
    return found


def _allowed(key: tuple[str, ...], tables: bool, patterns: Iterable[tuple[str, ...]]) -> bool:
    """``key`` is a named field, inside a ``**`` table, or (``tables``) holds one."""
    for pattern in patterns:
        whole = pattern[-1] == "**"
        fixed = pattern[:-1] if whole else pattern
        head = key[: len(fixed)]
        if len(head) == len(fixed) and all(p in ("*", k) for p, k in zip(fixed, head)):
            if whole or len(key) == len(fixed):
                return True
        elif tables and len(key) < len(fixed):
            if all(p in ("*", k) for p, k in zip(fixed, key)):
                return True
    return False


def unexpected_settings_keys(before: bytes, after: bytes, *, first_start: bool = False) -> list[str]:
    """Keys of Noctalia's settings that changed other than the shell's own.

    Those are :data:`SHELL_FIELDS`, and :data:`SHELL_MIGRATION` across
    ``first_start``. Compared by value, key by key, tables included.
    """
    try:
        old = _leaves(tomllib.loads(before.decode("utf-8")))
        new = _leaves(tomllib.loads(after.decode("utf-8")))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        return [f"<not TOML any more: {error}>"]
    patterns = [*SHELL_FIELDS, *(SHELL_MIGRATION if first_start else ())]
    unexpected = []
    for key in set(old) | set(new):
        if old.get(key) == new.get(key):
            continue
        # A table that merely appears or goes, holding only allowed fields.
        # (Values may be arrays, so compare, never hash.)
        tables = all(value in ("<table>", None) for value in (old.get(key), new.get(key)))
        if not _allowed(key, tables, patterns):
            unexpected.append(".".join(key))
    return sorted(unexpected)


def shell_writes(
    changes: Iterable[harness.Change], *, first_start: bool = False
) -> tuple[list[harness.Change], list[str]]:
    """Set aside what Noctalia itself wrote, judging its settings field by field.

    Returns every other change, for the phase's own allowances, and what was
    wrong with the set-aside ones: any change to settings.toml beyond the
    shell's own fields (a key added, removed or changed, the file created,
    deleted or chmodded) is a finding whoever made it. ``first_start`` also
    allows Noctalia's one-time migration of the fixture's settings.

    A chmod is never set aside. :func:`harness.diff` reports it as its own
    ``mode`` change even beside a content change, so it is a finding on
    settings.toml and stays for the phase to judge on a shell-owned file.
    """
    rest: list[harness.Change] = []
    problems: list[str] = []
    for change in changes:
        if change.path == NOCTALIA_SETTINGS:
            if change.kind == "rewritten":
                continue
            if change.kind != "modified":
                problems.append(change.describe())
                continue
            assert change.before is not None and change.before.content is not None
            assert change.after is not None and change.after.content is not None
            unexpected = unexpected_settings_keys(
                change.before.content, change.after.content, first_start=first_start
            )
            if unexpected:
                problems.append(f"{change.path}: changed {', '.join(unexpected)}")
        elif change.kind in ("mode", "type") or not any(
            fnmatchcase(change.path, pattern) for pattern in SHELL_FILES
        ):
            rest.append(change)
    return rest, problems


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
