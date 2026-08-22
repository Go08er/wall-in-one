"""Command line entry point.

Six modes:

* no arguments -- launch the GUI
* ``--service`` -- legacy Python compatibility service for installations that
  predate ``wall-in-one-service``
* ``ctl <verb>`` -- talk to a running instance (this is what the Noctalia
  plugin uses; every plugin control is one ``runAsync`` of a verb)
* ``--write-config`` -- compile authoring state for the Rust service without
  importing GTK or opening a window
* ``--sync-runtime-health`` -- persist Rust's bounded failure inventory through
  the app-owned authoring/config path, also without GTK
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
    "stop",
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
    "stop",
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
        choices=(
            "browse",
            "media",
            "pairings",
            "playlists",
            "schedules",
            "displays",
            "settings",
        ),
        help="present the GUI on one workflow page",
    )
    parser.add_argument(
        "--write-config",
        action="store_true",
        help="compile the resolved runtime config without opening the GUI",
    )
    parser.add_argument(
        "--sync-runtime-health",
        action="store_true",
        help="persist newly reported runtime wallpaper failures without opening the GUI",
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


def _write_runtime_config() -> int:
    """Compile authoring state for Rust without constructing a GTK application."""
    from wall_in_one import config, runtime_config
    from wall_in_one.session import Session

    try:
        # This lock begins before the first authoring read. If the GUI publishes
        # while systemd's preflight is scanning, that newer GUI generation must
        # land after this older snapshot rather than be rolled back by it.
        with runtime_config.compiler_lock():
            settings = config.load_strict()
            session = Session(settings)
            try:
                session.refresh()
                changed = runtime_config.update(settings, session)
            finally:
                session.shutdown()
    except (config.ConfigError, runtime_config.RuntimeConfigError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    state = "wrote" if changed else "already current"
    print(f"{state}: {paths.runtime_config_path()}")
    return 0


def _sync_runtime_health() -> int:
    """Persist one atomic Rust failure snapshot through the sole app writer."""
    from wall_in_one import config, runtime_config, runtime_health
    from wall_in_one.control import client
    from wall_in_one.library import pairings
    from wall_in_one.session import Session

    try:
        response = client.send_runtime("status")
    except client.NotRunningError as error:
        print(error, file=sys.stderr)
        return client.EXIT_NOT_RUNNING
    except client.ControlError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if not response.ok:
        print(f"error: runtime rejected status: {response.message}", file=sys.stderr)
        return 1
    try:
        status = runtime_health.parse_status(response.message)
    except runtime_health.RuntimeHealthError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    raw_reports = status.get("taboo_entries", [])
    omitted = status.get("taboo_entries_omitted", 0)
    assert isinstance(raw_reports, list)
    assert type(omitted) is int
    if not raw_reports:
        if omitted:
            print(
                f"warning: runtime omitted {omitted} older taboo entries; none were cleared",
                file=sys.stderr,
            )
        print("runtime reported no visible wallpaper health changes")
        return 0

    session: Session | None = None
    changed = 0
    document_changed = False
    try:
        # Hold the compiler gate from the first authoring read through the
        # generated document. This command is another app invocation, never a
        # second writer implementation; Rust remains read-only.
        with runtime_config.compiler_lock():
            settings = config.load_strict()
            session = Session(settings)
            session.refresh()
            faults = session.authoring_faults()
            if faults:
                details = "; ".join(f"{name}: {fault}" for name, fault in faults)
                raise runtime_config.RuntimeConfigError(
                    "cannot sync runtime health because authoring state is "
                    f"unreadable ({details}); no health marker was written"
                )
            inventory = runtime_health.taboo_inventory(
                status,
                session.playlists.all(),
                session.library.items,
            )
            for report in inventory.reports:
                if session.pairings.mark_borked(
                    report.item,
                    report.reason,
                    report.source,
                ):
                    changed += 1
            if changed:
                document_changed = runtime_config.update(settings, session)
    except (config.ConfigError, pairings.PairingError, runtime_config.RuntimeConfigError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        if session is not None:
            session.shutdown()

    if omitted:
        print(
            f"warning: runtime omitted {omitted} older taboo entries; none were cleared",
            file=sys.stderr,
        )
    if document_changed:
        try:
            reload_response = client.send_runtime("reload")
        except client.NotRunningError:
            # Persistence is complete. A later service start will read the
            # generated metadata, so disappearance between status and reload
            # does not turn successful authoring into failure.
            print("warning: runtime stopped before reload; saved health will apply at next start")
        except client.ControlError as error:
            print(f"error: runtime health was saved but reload failed: {error}", file=sys.stderr)
            return 1
        else:
            if not reload_response.ok:
                print(
                    f"error: runtime health was saved but reload was rejected: "
                    f"{reload_response.message}",
                    file=sys.stderr,
                )
                return 1
    print(f"saved {changed} new wallpaper health marker{'s' if changed != 1 else ''}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    options = parser.parse_args(argv)

    if options.command == "ctl":
        from wall_in_one.control import client

        words: list[str] = options.argument
        return client.dispatch(options.verb, " ".join(words) if words else None)

    if options.write_config:
        return _write_runtime_config()

    if options.sync_runtime_health:
        return _sync_runtime_health()

    maintenance = _run_maintenance(options)
    if maintenance is not None:
        return maintenance

    from wall_in_one.ui.app import run

    return run(service=options.service, initial_page=options.open_page)


if __name__ == "__main__":
    raise SystemExit(main())
