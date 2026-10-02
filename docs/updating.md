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
newer one wrote (the table below says what 0.1.4 does with each): keep or
restore the newer package to repair the configuration. Preserve current files before restoring
any backup, since a whole-folder restore would discard changes made after that
backup.

### Rolling back from 0.2.0 to 0.1.4

There is no 0.1.5. 0.2.0 updates 0.1.4 directly, and 0.1.4 is the only
release to roll back to. 0.1.4 predates the guard that makes 0.2.0 and later
open a file from a newer release read-only (see
[State-file recovery](library.md#state-file-recovery)), so what a rollback
keeps depends on the file.

0.2.0 reads every 0.1.4 file as it is. It moves a file to a newer version only
when you first use what that version adds:

- naming a schedule rule moves `schedules.json` to version 3;
- giving a playlist its own interval or shuffle moves `playlists.json` to
  version 2;
- letting a display's own playlist beat global schedule rules moves
  `displays.json` to version 2.

Just before that first save, 0.2.0 keeps the file as it was beside it, once:
`schedules.json.v2-backup`, `playlists.json.v1-backup` or
`displays.json.v1-backup`. A profile that never uses these features keeps
every file in 0.1.4's formats, and a rollback loses nothing.
`runtime.toml` never changes shape. The per-playlist and display settings
reach the service through `runtime-overrides.toml` instead, and the new
interface's window preferences live in `ui.toml`.

After a rollback, 0.1.4 does this with each file in `~/.config/wall-in-one`
and `~/.local/state/wall-in-one`:

| File | What 0.1.4 does |
| --- | --- |
| `runtime.toml` | Its service loads it as 0.2.0 left it, so the wallpaper keeps running. While any of the three files above is still at 0.2.0's version, 0.1.4 can't compile a new one: the service start keeps the last `runtime.toml`, and changes you save in the 0.1.4 app don't reach the wallpaper. |
| `runtime-overrides.toml` | Never opened or changed. Per-playlist timing and display opt-ins stop applying. |
| `ui.toml` | Never opened or changed. |
| `settings.toml` | Read as it is. 0.2.0 writes only keys and values 0.1.4 knows. A key added by hand stops 0.1.4 from using the file: the service exits 78, or keeps the last `runtime.toml` and never publishes a new one. |
| `schedules.json` (version 3), `playlists.json` or `displays.json` (version 2), until 0.1.4 edits it | Not changed. 0.1.4 shows what it can read of it, and its compile refuses it as an unsupported version. |
| The same file after any 0.1.4 schedule, playlist or display edit | Moved aside as `<file>.broken`, then saved in 0.1.4's version with your edit and without the rule names, per-playlist intervals and shuffle, or display opt-ins. Once all three are back in 0.1.4's versions, 0.1.4 publishes `runtime.toml` again. |
| `favourites.json`, `pairings.json`, `pending-removals.json` | Read and edited as usual; 0.2.0 keeps them in 0.1.4's formats. |
| `schedules.json.v2-backup`, `playlists.json.v1-backup`, `displays.json.v1-backup` | Ignored and left as they are. |
| `<file>.broken` | Made as above, then ignored. |
| `tidy-archive/` | Ignored and left as it is. What Tidy up archived stays there, and 0.2.0's Undo still works after a return. |

If you didn't edit those three files under 0.1.4, returning to 0.2.0 finds
everything as it left it. If 0.1.4 did narrow one, returning to 0.2.0 doesn't
bring the settings back: 0.2.0 reads the narrowed file like any 0.1.4 file,
and its next compile removes `runtime-overrides.toml` if nothing in it is in
use any more. 0.2.0 leaves the `.broken` and backup files alone. Using a
feature again moves the file to the new version again and keeps the existing
backup as it is.

### Getting narrowed settings back by hand

| File | What it holds |
| --- | --- |
| `schedules.json.broken` | 0.2.0's schedule, with the rule names. |
| `playlists.json.broken` | 0.2.0's playlists, with their own intervals and shuffle. |
| `displays.json.broken` | 0.2.0's display assignments, with the opt-ins (`beats_global_rules`). |
| `schedules.json`, `playlists.json`, `displays.json` | 0.1.4's narrowed file, with the edits you made under 0.1.4. |
| `schedules.json.v2-backup`, `playlists.json.v1-backup`, `displays.json.v1-backup` | The file from before 0.2.0 first used the feature. None of the 0.2.0 settings are in it. |

Each `.broken` file is the file as 0.2.0 left it, just before 0.1.4's first
edit of it. That edit, and anything else you changed in that file under 0.1.4,
is only in the live file. If a file was narrowed more than once, the copies are
`.broken`, `.broken.1`, `.broken.2` and so on, and the highest number is the
most recent. The backups are for returning a file to how it was under 0.1.4,
not for recovering 0.2.0 settings.

To get the settings back:

1. Update to 0.2.0 again first. A `.broken` file restored under 0.1.4 is
   narrowed again by its next edit.
2. Close the app and stop the service as in [Update and restart](#update-and-restart),
   then back up the state folder.
3. For each file, choose one:
   - If you don't need what you changed in it under 0.1.4, copy the `.broken`
     file over it and keep the copy, for example:

     ```sh
     cd ~/.local/state/wall-in-one
     cp schedules.json.broken schedules.json
     ```

   - Otherwise keep the narrowed file and set the names, intervals, shuffle or
     opt-ins again in 0.2.0, reading them from the `.broken` file. For
     example, `jq '.rules[] | select(.name) | {id, name}' schedules.json.broken`
     lists the rule names.
4. Start the service. Its startup compile reads the restored files and
   publishes `runtime-overrides.toml` again. Open the app and check the
   schedule, playlists and displays.
