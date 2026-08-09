"""Command line entry point.

Four modes:

* no arguments -- launch the GUI
* ``--service`` -- legacy Python compatibility service for installations that
  predate ``wall-in-one-service``
* ``ctl <verb>`` -- talk to a running instance (this is what the Noctalia
  plugin uses; every plugin control is one ``runAsync`` of a verb)
* maintenance flags such as ``--install-theme-template``

The GTK import is deliberately deferred so that ``ctl`` and the maintenance
flags stay fast and work with no display attached.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Final

from wall_in_one import __version__, paths

RUNTIME_ONLY_VERBS: Final[tuple[str, ...]] = (
    "previous",
    "play",
    "pause",
    "toggle",
    "schedule-follow",
    "reload",
)

CTL_VERBS: Final[tuple[str, ...]] = (
    "next",
    "prev",
    "previous",
    "random",
    "play",
    "pause",
    "toggle",
    "shuffle",
    "schedule-follow",
    "reload",
    "cycle",
    "cycle-interval",
    "dynamics",
    "reload-palette",
    "open",
    "status",
    "list",
    "select",
    "favourites",
    "favourite",
    "unfavourite",
    "remove",
    "pairing",
    "still",
    "palette",
    "reset-pairing",
    "playlists",
    "playlist-new",
    "playlist-delete",
    "playlist-add",
    "playlist-remove",
    "playlist-use",
    "displays",
    "display-assign",
    "display-clear",
    "schedule",
    "schedule-add",
    "schedule-remove",
    "providers",
    "search",
    "download",
    "quit",
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=paths.APP_ID,
        description="A wallpaper manager for Wayland.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--service",
        action="store_true",
        help="run the legacy Python service (prefer wall-in-one-service)",
    )
    parser.add_argument(
        "--open-page",
        choices=("media", "pairings", "playlists", "schedules", "displays"),
        help="present the GUI on one workflow page",
    )

    maintenance = parser.add_argument_group("Noctalia integration")
    maintenance.add_argument(
        "--install-theme-template",
        action="store_true",
        help="register the palette template with Noctalia so colours stay in sync",
    )
    maintenance.add_argument(
        "--uninstall-theme-template",
        action="store_true",
        help="remove the palette template registration",
    )
    maintenance.add_argument(
        "--theme-status",
        action="store_true",
        help="report whether the palette template is installed",
    )
    maintenance.add_argument(
        "--print-palette",
        action="store_true",
        help="print the resolved palette and where it came from",
    )
    maintenance.add_argument(
        "--print-css",
        action="store_true",
        help="print the generated stylesheet for the resolved palette",
    )

    subcommands = parser.add_subparsers(dest="command")
    control = subcommands.add_parser("ctl", help="control a running instance")
    control.add_argument("verb", choices=CTL_VERBS)
    # Several words rather than one, joined back into the single argument the
    # protocol carries, so that `ctl search wallhaven aurora borealis` works
    # without quoting -- `search`, `download` and `list` all read a word off the
    # front and treat the rest as their own. It also means the path verbs take
    # an unquoted wallpaper with spaces in its name, which most of them have.
    control.add_argument(
        "argument",
        nargs="*",
        help="value for verbs that take one, e.g. `search wallhaven aurora`",
    )

    return parser


def _run_maintenance(options: argparse.Namespace) -> int | None:
    """Handle the non-GUI flags. Returns an exit code, or None if none applied."""
    from wall_in_one.theme import template

    if options.install_theme_template:
        try:
            result = template.install()
        except template.TemplateInstallError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        if result.changed:
            print(f"{result.detail}: {result.settings_path}")
            print(f"  template -> {result.template_path}")
            print(f"  palette  -> {result.output_path}")
            if result.backup_path is not None:
                print(f"  backup   -> {result.backup_path}")
            print("\nRun `noctalia msg templates-apply` to render it now.")
        else:
            print(result.detail)
        return 0

    if options.uninstall_theme_template:
        try:
            result = template.uninstall()
        except template.TemplateInstallError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(result.detail)
        return 0

    if options.theme_status:
        try:
            print(template.status())
        except template.TemplateInstallError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        return 0

    if options.print_palette or options.print_css:
        from wall_in_one import config
        from wall_in_one.theme import css, source

        resolved = source.resolve()
        if options.print_css:
            settings = config.load()
            sys.stdout.write(css.render(resolved.palette, opacity=settings.opacity))
            return 0
        print(f"origin: {resolved.origin.value} ({resolved.detail})")
        print(f"mode:   {resolved.palette.mode}")
        print(f"tokens: {len(resolved.palette.colours)}")
        missing = resolved.palette.missing_tokens
        if missing:
            print(f"missing: {', '.join(missing)}")
        for token in sorted(resolved.palette.colours):
            print(f"  {token:<32} {resolved.palette[token].hex}")
        return 0

    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    options = parser.parse_args(argv)

    if options.command == "ctl":
        from wall_in_one.control import client

        words: list[str] = options.argument
        return client.dispatch(options.verb, " ".join(words) if words else None)

    maintenance = _run_maintenance(options)
    if maintenance is not None:
        return maintenance

    from wall_in_one.ui.app import run

    return run(service=options.service, initial_page=options.open_page)


if __name__ == "__main__":
    raise SystemExit(main())
