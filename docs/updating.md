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

   For a directly launched runtime, including one started through the
   companion's **Executable** override, disabling the companion does not stop
   the daemon. After disabling its automatic launcher, shut it down explicitly:

   ```sh
   wall-in-one ctl quit
   ```

   In either case, check `wall-in-one ctl status` before continuing. Exit code
   **3** means no runtime is listening; a successful status reply or a timeout
   does not confirm shutdown. Wait and check again if needed. Use the same XDG
   environment and executable as that installation; if the companion uses an
   **Executable** override, use that full path in these commands. Do not
   force-kill an app with unfinished work.
3. Back up the app's config and state folders, including hidden files, and keep
   the previous app/companion revisions available. Defaults are
   `~/.config/wall-in-one` and `~/.local/state/wall-in-one`; respect custom XDG
   locations and original library paths. Keep backups of your media too.
4. Update the app and companion through their existing installation method.
   For NixOS or Home Manager, update the declaration and activate it normally.
   For manually installed units, refresh only the links owned by that install.
   Installing a package does not update a copied unit pointing at an old store
   path. Preserve intentional unit overrides. Update an explicit companion
   **Executable** path if it still points at the old package, preserving the
   intended override rather than clearing it.
5. For systemd, reload definitions, check the package paths, then start:

   ```sh
   systemctl --user daemon-reload
   wall-in-one --update-status
   systemctl --user start wall-in-one.service
   ```

   `--update-status` is read-only. It distinguishes the installed package, loaded
   service commands and running process. Resolve old or unverified commands
   through the configuration that owns them; the app does not rewrite pins.
6. Open the app and check your folders. Re-enable the companion or your usual
   launcher; the companion prepares and starts a direct runtime when needed.
   Check playback, then apply any temporary playback choices you want again.

After an update, the app may show a **Tidy up** card at the top of its window
when older versions left things behind: leftover claim folders, a stale palette
template name in Noctalia's settings, or the retired plugin's settings.
Nothing changes until you review each item in Settings → Tidy up and choose
Apply; every change is archived or backed up first and can be undone there.
See [Tidy up](settings.md#tidy-up).

Keep **Stop animations on battery** off until the running service supports it.
The app refuses to enable it against an older runtime or unverified loaded
service commands, before writing an incompatible configuration.

### What opening the app writes

Opening the app and leaving it idle changes none of your settings, playlists,
schedules, displays, favourites or window preferences. It writes only:

- `runtime.toml`, when the document compiled on this machine differs from the
  one on disk, for example on the first start after an update or on another
  machine (the renderer paths are this machine's). What it plays stays the
  same.
- `pairings.json`, when the wallpaper service reported a wallpaper that failed
  to play: the health sync records that one marker on that wallpaper.
- Automatic stills, the one exception that writes into your library (since
  0.1.0). After each scan, including the first one, the app captures a still
  for a moving wallpaper that has none, and captures a scene's managed still
  again when it is smaller, in both directions, than a display niri measured.
  Only files in `<library>/Wall-in-One/Automatic Stills/` change, plus the
  pairing sidecar beside a video in that library. Without the capture tool, or
  without a measured display for a scene, nothing is written. See
  [Stills the app makes for itself](library.md#stills-the-app-makes-for-itself).

Thumbnails in `~/.cache/wall-in-one` are a cache, rebuilt as needed, and are
not counted.

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

Package rollback does not roll back application data, and there is no
automatic data downgrade. An older release can refuse or narrow files that a
newer one wrote (for 0.1.4, see below): keep or restore the newer package to
repair the configuration. Preserve current files before restoring
any backup, since a whole-folder restore would discard changes made after that
backup.

### Rolling back from 0.2.0 to 0.1.4

0.2.0 isn't designed to be rolled back, and there is no 0.1.5: 0.1.4 is the
only release to go back to. 0.2.0 reads every 0.1.4 file as it is, but moves
three of them to a newer version the first time you use what it adds:

- naming a schedule rule moves `schedules.json` from version 2 to 3;
- giving a playlist its own interval or shuffle moves `playlists.json` from
  version 1 to 2;
- letting a display's own playlist beat global schedule rules moves
  `displays.json` from version 1 to 2.

A profile that never used these is entirely in 0.1.4's formats. Otherwise,
to go back to 0.1.4, close the app and stop the service, then run the
rollback tool that comes with 0.2.0:

```sh
systemctl --user stop wall-in-one.service
wall-in-one-rollback            # shows what it would change; writes nothing
wall-in-one-rollback --apply    # backs up, then rewrites
```

It lists what will be dropped, for example `rule "Evening lights" loses its
name` or `playlist "Evenings" loses its interval (120 s)`, and keeps every
playlist, entry, rule and display assignment. `--apply` copies the files it
changes into one dated folder, `~/.local/state/wall-in-one/rollback-to-0.1.4-<date>/`,
rewrites the three files in the versions 0.1.4 reads, and removes
`runtime-overrides.toml`, which 0.1.4 never reads. It then prints the two
commands that put the copies back. It refuses, writing nothing, while the
app or the service is running, and when a file was saved by a version newer
than 0.2.0. Running it again finds nothing to do.

Then install 0.1.4 and start its service. Its first start compiles
`runtime.toml` again from the rewritten files. Everything else is left as it
is: `pairings.json`, `favourites.json`, `pending-removals.json` and
`settings.toml` are already in 0.1.4's formats (0.2.0 writes only settings
keys 0.1.4 knows), and 0.1.4 ignores `ui.toml`, the `.v<n>-backup` files and
`tidy-archive/`.

Without the tool, 0.1.4's app shows an empty library and saves nothing until
you update to 0.2.0 again, but nothing is lost, and the wallpaper keeps
running on the last `runtime.toml`.
