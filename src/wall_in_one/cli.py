"""Command line entry point.

Six modes:

* no arguments -- launch the GUI
* ``--service`` -- legacy Python compatibility service for installations that
  predate ``wall-in-one-service``
* ``ctl <verb>`` -- talk to a running instance (this is what the Noctalia
  plugin uses; every plugin control is one ``runAsync`` of a verb)
* ``--write-config`` -- automatically complete a proven deployed-app upgrade,
  then compile authoring state for Rust without importing GTK or opening a
  window
* ``--service-startup-prepare`` -- the packaged service's migration-aware
  compiler preflight, with last-known-good fallback left to Rust validation
* ``--sync-runtime-health`` -- persist Rust's bounded failure inventory through
  the app-owned authoring/config path, also without GTK
* maintenance flags such as ``--install-theme-template`` and the explicit
  no-overwrite legacy importer

The GTK import is deliberately deferred so that ``ctl`` and the maintenance
flags stay fast and work with no display attached.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from wall_in_one import __version__, paths

EXIT_TEMPFAIL: Final = 75
EXIT_CONFIG: Final = 78

RUNTIME_ONLY_VERBS: Final[tuple[str, ...]] = (
    "on",
    "previous",
    "play",
    "pause",
    "stop",
    "toggle",
    "schedule-follow",
    "reload",
)

CTL_VERBS: Final[tuple[str, ...]] = (
    "on",
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
        "--service-startup-prepare",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--sync-runtime-health",
        action="store_true",
        help="persist newly reported runtime wallpaper failures without opening the GUI",
    )
    parser.add_argument(
        "--sync-runtime-health-on-stop",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    migration = parser.add_argument_group("Legacy Noctalia plugin migration")
    migration.add_argument(
        "--migrate-legacy",
        action="store_true",
        help="import an untouched goober/wall-in-one Noctalia plugin profile",
    )
    migration.add_argument(
        "--legacy-migration-status",
        action="store_true",
        help="report whether legacy plugin authoring needs an import decision",
    )

    deployed = parser.add_argument_group("Deployed application upgrade")
    deployed.add_argument(
        "--update-status",
        action="store_true",
        help="report installed, running and loaded service generations as read-only JSON",
    )
    deployed.add_argument(
        "--deployed-upgrade-status",
        action="store_true",
        help="inspect the schema-2 deployed-app upgrade boundary without writing",
    )
    deployed.add_argument(
        "--prepare-deployed-upgrade",
        action="store_true",
        help="complete and cut over an exact deployed profile to schema 4",
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


def _run_legacy_migration(options: argparse.Namespace) -> int | None:
    """Handle migration flags before GTK, service construction, or config writes."""
    from wall_in_one import legacy_migration

    if options.legacy_migration_status:
        found = legacy_migration.probe()
        print(f"{found.status}: {found.detail}")
        print(f"source: {found.source}")
        if found.schema is not None:
            print(f"schema: {found.schema}; playlists: {found.playlists}; outputs: {found.outputs}")
        for conflict in found.conflicts:
            print(f"conflict: {conflict}")
        return 1 if found.status in ("conflict", "corrupt", "in-progress") else 0

    if options.migrate_legacy:
        try:
            outcome = legacy_migration.migrate()
        except legacy_migration.MigrationError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(outcome.detail)
        if outcome.report is not None:
            print(f"report: {outcome.report}")
        return 0
    return None


def _deployed_upgrade_error(error: Exception) -> int:
    from wall_in_one import deployed_upgrade_transaction

    assert isinstance(error, deployed_upgrade_transaction.TransactionError)
    print(f"error: {error}", file=sys.stderr)
    return EXIT_TEMPFAIL if error.status == "retry" else EXIT_CONFIG


def _run_deployed_upgrade(options: argparse.Namespace) -> int | None:
    """Handle read-only status and the explicit bounded migration command."""
    from wall_in_one import deployed_upgrade_transaction, legacy_migration

    if options.deployed_upgrade_status:
        found = deployed_upgrade_transaction.probe()
        print(f"{found.status}: {found.detail}")
        if found.counts.videos:
            print(
                f"videos/captures: {found.counts.videos}; "
                f"All Media: {found.counts.all_media_entries}; "
                f"playlists/entries: {found.counts.playlists}/"
                f"{found.counts.playlist_entries}; schedules: {found.counts.schedules}"
            )
        return EXIT_CONFIG if found.status in ("conflict", "corrupt") else 0

    if options.prepare_deployed_upgrade:
        try:
            with legacy_migration.profile_transaction():
                outcome = deployed_upgrade_transaction.ensure()
        except deployed_upgrade_transaction.TransactionError as error:
            return _deployed_upgrade_error(error)
        except legacy_migration.MigrationError as error:
            print(f"error: {error}", file=sys.stderr)
            return EXIT_TEMPFAIL
        print(outcome.detail)
        return 0
    return None


def _run_unattended_writer(write: Callable[[], int]) -> int:
    """Cross both migration boundaries before one headless publication."""
    from wall_in_one import deployed_upgrade_transaction, legacy_migration

    try:
        with legacy_migration.profile_transaction():
            outcome = deployed_upgrade_transaction.ensure()
            legacy_migration.require_unattended_safe_locked()
            if outcome.changed:
                print(outcome.detail)
            return write()
    except deployed_upgrade_transaction.TransactionError as error:
        return _deployed_upgrade_error(error)
    except legacy_migration.MigrationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _run_graphical_startup_upgrade(
    *, require_legacy_safe: bool, retry: Callable[[str], bool] | None = None
) -> int | None:
    """Finish an exact deployed upgrade before GTK reads configuration."""
    from wall_in_one import deployed_upgrade_transaction, legacy_migration

    while True:
        try:
            with legacy_migration.profile_transaction():
                outcome = deployed_upgrade_transaction.ensure()
                if require_legacy_safe:
                    legacy_migration.require_unattended_safe_locked()
            break
        except deployed_upgrade_transaction.TransactionError as error:
            result, detail = _deployed_upgrade_error(error), str(error)
        except legacy_migration.MigrationError as error:
            print(f"error: {error}", file=sys.stderr)
            result, detail = 1, str(error)
        # The recovery window owns no authoring worker, service or migration
        # lock. A retry occurs only after an explicit user gesture; unsafe
        # evidence is never bypassed just to get the main window open.
        if require_legacy_safe or retry is None or not retry(detail):
            return result
    if outcome.changed:
        print(outcome.detail)
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
            # First-run defaults are suitable for the GUI, not for publishing
            # a runtime without its owning settings. Such an orphan makes the
            # next migration probe correctly refuse to assume a fresh install.
            settings = config.load_strict(require_present=True)
            session = Session(settings)
            try:
                # Runtime publication is a read-only authoring snapshot. The
                # GUI owns pending-removal reconciliation; letting this
                # short-lived process rewrite Favourites/Playlists from stale
                # snapshots could race an interactive edit. A malformed
                # journal is still present in ``authoring_faults`` below and
                # therefore fails publication closed.
                session.refresh(mutate_removals=False)
                changed = runtime_config.update(settings, session)
            finally:
                session.shutdown()
    except config.MissingSettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_CONFIG
    except (config.ConfigError, runtime_config.RuntimeConfigError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    state = "wrote" if changed else "already current"
    print(f"{state}: {paths.runtime_config_path()}")
    return 0


def _prepare_service_start() -> int:
    """Cross migration boundaries, then publish or retain a runtime candidate.

    Migration and legacy-boundary errors keep their actionable non-zero exit.
    Only an ordinary authoring/compiler failure is softened: the next systemd
    preflight asks Rust itself whether the untouched last-known-good document
    is safe to consume.
    """

    def publish_or_retain() -> int:
        result = _write_runtime_config()
        if result != 1:
            return result
        print(
            "warning: current authoring could not be compiled; validating the "
            "last-known-good runtime before service start",
            file=sys.stderr,
        )
        return 0

    return _run_unattended_writer(publish_or_retain)


def _sync_runtime_health(*, reload_runtime: bool = True) -> int:
    """Persist one atomic Rust failure snapshot through the sole app writer."""
    from wall_in_one import config, runtime_config, runtime_health
    from wall_in_one.control import client
    from wall_in_one.library import pairings
    from wall_in_one.session import Session

    session: Session | None = None
    omitted = 0
    changed = 0
    document_changed = False
    reload_needed = False
    refresh_only = False
    health_present = False
    mapped = 0
    unmapped = 0
    stale = 0
    try:
        # The status request itself is part of the compiler transaction. A GUI
        # Clear+retry takes this same gate around its Pairings write and nested
        # publication, so neither side can observe the other's half-finished
        # state and then persist an obsolete runtime finding.
        with runtime_config.compiler_lock():
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
            omitted_value = status.get("taboo_entries_omitted", 0)
            assert isinstance(raw_reports, list)
            assert type(omitted_value) is int
            omitted = omitted_value
            if not raw_reports:
                if omitted:
                    print(
                        f"warning: runtime omitted {omitted} older taboo entries; "
                        "none were cleared",
                        file=sys.stderr,
                    )
                print("runtime reported no visible wallpaper health changes")
                return 0

            settings = config.load_strict()
            session = Session(settings)
            # Health sync may update Pairings under its mutation lock, but it
            # must not opportunistically rewrite the other authoring stores.
            session.refresh(mutate_removals=False)
            faults = session.authoring_faults()
            if faults:
                details = "; ".join(f"{name}: {fault}" for name, fault in faults)
                raise runtime_config.RuntimeConfigError(
                    "cannot sync runtime health because authoring state is "
                    f"unreadable ({details}); no health marker was written"
                )
            expected_path = paths.runtime_config_path().absolute()
            status_path = status.get("config_path")
            status_generation = status.get("config_generation")
            assert isinstance(status_path, str)
            assert isinstance(status_generation, str)
            if Path(status_path) != expected_path:
                raise runtime_config.RuntimeConfigError(
                    "runtime health came from configuration "
                    f"{status_path}, not the app-managed {expected_path}; "
                    "no health marker was written"
                )
            installed_generation = runtime_config.read_config_generation(expected_path)
            if status_generation != installed_generation:
                raise runtime_config.RuntimeConfigError(
                    "runtime status is from configuration generation "
                    f"{status_generation}, but {expected_path} contains "
                    f"{installed_generation}; reload the runtime and retry; "
                    "no health marker was written"
                )
            authored_generation = runtime_config.document_generation(
                runtime_config.render(settings, session)
            )
            if status_generation != authored_generation:
                # The stores changed before their generated document landed,
                # or a prior health sync saved Pairings but failed to compile.
                # Publish that newer authoring truth, but never consume an old
                # runtime observation: it may be precisely what Clear+retry
                # was intended to retract.
                document_changed = runtime_config.update(settings, session)
                reload_needed = True
                refresh_only = True
            else:
                inventory = runtime_health.taboo_inventory(
                    status,
                    session.playlists.all(),
                    session.library.items,
                )
                mapped = len(inventory.reports)
                unmapped = inventory.unmapped
                stale = inventory.stale
                if mapped:
                    changed = session.pairings.mark_borked_many(
                        (report.item, report.reason, report.source) for report in inventory.reports
                    )
                    health_present = True
                    # Always render, even when every marker was already in the
                    # store. This repairs the split state left by a previous
                    # compiler failure instead of pinning it forever.
                    document_changed = runtime_config.update(settings, session)
                    reload_needed = document_changed or any(
                        not report.durable for report in inventory.reports
                    )
    except (config.ConfigError, pairings.PairingError, runtime_config.RuntimeConfigError) as error:
        if health_present:
            print(
                f"error: runtime health is saved, but its runtime configuration "
                f"could not be compiled: {error}",
                file=sys.stderr,
            )
        else:
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
    if reload_needed and reload_runtime:
        saved_subject = "current authoring configuration" if refresh_only else "runtime health"
        try:
            reload_response = client.send_runtime("reload")
        except client.NotRunningError:
            # Persistence is complete. A later service start will read the
            # generated metadata, so disappearance between status and reload
            # does not turn successful authoring into failure.
            print(
                f"warning: runtime stopped before reload; saved {saved_subject} "
                "will apply at next start"
            )
        except client.ControlError as error:
            print(f"error: {saved_subject} was saved but reload failed: {error}", file=sys.stderr)
            return 1
        else:
            if not reload_response.ok:
                print(
                    f"error: {saved_subject} was saved but reload was rejected: "
                    f"{reload_response.message}",
                    file=sys.stderr,
                )
                return 1
    if refresh_only:
        state = "published" if document_changed else "confirmed"
        print(
            "error: runtime status was older than current authoring; "
            f"{state} the current runtime configuration without consuming the stale "
            "health snapshot. Retry after the runtime reports the new generation.",
            file=sys.stderr,
        )
        return 1
    if unmapped or stale:
        problems: list[str] = []
        if stale:
            problems.append(f"{stale} stale-generation runtime report{'s' if stale != 1 else ''}")
        if unmapped:
            problems.append(
                f"{unmapped} runtime report{'s' if unmapped != 1 else ''} "
                "which no longer map to current authoring"
            )
        saved = (
            f"saved {mapped} current wallpaper health report{'s' if mapped != 1 else ''}"
            if mapped
            else "saved no wallpaper health reports"
        )
        print(
            f"error: {saved}; skipped {' and '.join(problems)}; retry after the runtime "
            "reports the current configuration generation",
            file=sys.stderr,
        )
        return 1
    if not mapped:
        print(
            "error: runtime reported wallpaper failures, but none map to current "
            "authoring; no health marker was written",
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
        if options.verb == "on":
            if len(words) < 2:
                return client.dispatch("on", " ".join(words) if words else None)
            return client.dispatch_on(
                words[0],
                words[1],
                " ".join(words[2:]) if len(words) > 2 else None,
            )
        return client.dispatch(options.verb, " ".join(words) if words else None)

    if options.update_status:
        import json

        from wall_in_one import update_status

        print(json.dumps(update_status.report(), indent=2))
        return 0

    migration = _run_legacy_migration(options)
    if migration is not None:
        return migration

    deployed = _run_deployed_upgrade(options)
    if deployed is not None:
        return deployed

    if options.write_config:
        return _run_unattended_writer(_write_runtime_config)

    if options.service_startup_prepare:
        return _prepare_service_start()

    if options.sync_runtime_health:
        return _run_unattended_writer(_sync_runtime_health)

    if options.sync_runtime_health_on_stop:
        # The systemd unit bounds this best-effort final persistence attempt.
        # The runtime is about to exit, so compiling its saved health into the
        # next-start document is sufficient and avoids a pointless reload.
        return _run_unattended_writer(lambda: _sync_runtime_health(reload_runtime=False))

    maintenance = _run_maintenance(options)
    if maintenance is not None:
        return maintenance

    def retry_startup(message: str) -> bool:
        from wall_in_one.ui.recovery import run

        return run(message)

    blocked = _run_graphical_startup_upgrade(
        require_legacy_safe=options.service, retry=retry_startup
    )
    if blocked is not None:
        return blocked

    from wall_in_one.ui.app import run

    return run(service=options.service, initial_page=options.open_page)


if __name__ == "__main__":
    raise SystemExit(main())
