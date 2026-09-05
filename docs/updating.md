# Updating

Wall-in-One uses a normal app and wallpaper-service restart for updates. Your
saved settings, library folders, pairings, playlists and schedules remain in
place. Temporary playback choices—such as a manually selected playlist, pause,
shuffle override, current position and cycle countdown—can reset to the saved
settings when the service restarts.

For the retired full Noctalia plugin or the older empty-root/schema-2 app, see
the [migration guide](migrating.md). Never delete settings or migration journals
to make an existing installation look like a fresh install.

## Update and restart

1. Finish downloads and pending saves, then close the app normally. A new
   package shows a restart notice if a different app version is still open.
2. Temporarily disable the companion and other automatic launchers. Stop the
   wallpaper service and its health helpers before backing up or changing
   packages. For the packaged systemd installation:

   ```sh
   systemctl --user stop wall-in-one.service
   systemctl --user stop wall-in-one-health-sync.timer wall-in-one-health-sync.service
   ```

   Confirm the service is stopped. Do not force-kill an app with unfinished work.
3. Back up the app's config and state folders, including hidden files, and keep
   the previous app/companion revisions available. Defaults are
   `~/.config/wall-in-one` and `~/.local/state/wall-in-one`; respect custom XDG
   locations and original library paths. Keep backups of your media too.
4. Update the app and companion through their existing installation method.
   For NixOS or Home Manager, update the declaration and activate it normally.
   For manually installed units, refresh only the links owned by that install.
   Installing a package does not update a copied unit pointing at an old store
   path. Preserve intentional overrides.
5. For systemd, reload definitions, check the package paths, then start:

   ```sh
   systemctl --user daemon-reload
   wall-in-one --update-status
   systemctl --user start wall-in-one.service
   ```

   `--update-status` is read-only. It distinguishes the installed package, loaded
   service commands and running process. Resolve old or unverified commands
   through the configuration that owns them; the app does not rewrite pins.
6. Open the app, check your folders and playback, then enable the companion.
   Apply any temporary playback choices you want again.

Keep **Stop animations on battery** off until the running service supports it.
The app refuses to enable it against an older runtime or unverified loaded
service commands, before writing an incompatible configuration.

## If configuration prevents startup

The packaged service first regenerates its runtime configuration from saved
app settings and library data, then checks it with Rust's parser. If authoring
cannot be compiled, it may keep a valid last-known-good runtime. An unsupported
or malformed runtime stops startup with an error instead of a restart loop.

The app does not require a working Rust daemon to open Settings. If the settings
or migration state itself cannot be read safely, a configuration-recovery
window shows the error and offers **Open settings file**, **Open app state
folder**, and **Try again**. Open the named file in your editor, preserve its
original paths and other values, repair the error, then try again. The recovery
window never resets or overwrites your files. A conflicting migration journal
still needs the [migration recovery procedure](migrating.md#crash-recovery-backups-and-rollback).

For service details:

```sh
journalctl --user -u wall-in-one.service -b -n 80 --no-pager
```

After repairing the configuration, start the service again. Do not delete the
runtime or replace settings with defaults merely to silence an error.

## Returning to an older package

Package rollback does not roll back application data. An older release can
reject newer settings—for example, battery control's schema 5. Keep or restore
the compatible newer package to repair the configuration; there is no automatic
data downgrade. Preserve current files before restoring any backup, since a
whole-folder restore would discard changes made after that backup.
