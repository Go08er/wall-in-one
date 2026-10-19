# Updating

Wall-in-One uses a normal app and wallpaper-service restart for updates. Your
saved settings, library folders, pairings, playlists and schedules remain in
place. Temporary playback choices—such as a manually selected playlist, pause,
shuffle override, current position and cycle countdown—can reset to the saved
settings when the service restarts.

For the retired full Noctalia plugin or the older empty-root/schema-2 app, see
the [migration guide](migrating.md). Never delete settings or migration journals
to make an existing installation look like a fresh install.

## From 0.3 to 0.4: install the renderers first

From 0.4 the package no longer brings mpvpaper (video wallpapers) or
linux-wallpaperengine (Wallpaper Engine scenes). 0.3's `runtime.toml` names
the copies inside the 0.3 package. On its first start, 0.4's service compiles
the document again and names the renderers that are installed; one that is
not installed is written as `/usr/bin/mpvpaper` (or
`/usr/bin/linux-wallpaperengine`), and every video or scene then fails to
start. The service tries each three times, skips it, and the app saves the
skip on that wallpaper, so stills are all that play.

So before you restart the service in step 5 below, make sure the renderers are
installed:

- **NixOS:** replace the package in `environment.systemPackages` with the
  flake's module, `programs.wall-in-one.enable = true`, which installs both
  ([installing](installing.md#on-nixos-the-module)).
- **home-manager:** the same with `homeManagerModules.default`
  ([installing](installing.md#with-home-manager)).
- **`nix profile`:** install them beside the app:
  `nix profile install nixpkgs#mpvpaper nixpkgs#linux-wallpaperengine`.
- **`manualWallpaperInstalls = true` or another distribution:** install them
  yourself ([installing](installing.md#managing-the-renderers-yourself)).

**If videos and scenes stopped playing after the update:** install the
renderers as above, then compile the document again and restart the service,
which forgets the skips it made in its session:

```sh
wall-in-one --write-config
systemctl --user restart wall-in-one.service
```

A skip the app had already saved stays until you open that wallpaper in the
app and choose **Try again** in its details. The same applies after installing,
removing or moving a renderer later
([installing](installing.md#after-installing-removing-or-moving-a-renderer)).

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
3. Back up the app's config and state folders, including hidden files.
   Defaults are `~/.config/wall-in-one` and `~/.local/state/wall-in-one`;
   respect custom XDG locations and original library paths. Keep backups of
   your media too.
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

After an update, the app may show a **Tidy up** card at the top of its window,
in either interface, when older versions left things behind: leftover claim
folders, a stale palette template name in Noctalia's settings, or the retired
plugin's settings. Nothing changes until you review each item in Settings →
Tidy up and choose Apply; every change is archived or backed up first and can
be undone there. See [Tidy up](settings.md#tidy-up).

Keep **Stop animations on battery** off until the running service supports it.
The app refuses to enable it against an older runtime or unverified loaded
service commands, before writing an incompatible configuration.

### What opening the app writes

Opening the app and leaving it idle changes none of your choices: what your
settings, playlists, schedules, displays, favourites and window preferences
mean stays the same. The bytes of a few files can change, and it writes only:

- `settings.toml`, `schedules.json` and `displays.json`, when the start-up
  reference repair finds work, before any change can be saved. A default,
  schedule rule or display assignment that names a playlist by its name (as an
  older profile may) is given that playlist's id, so it keeps naming the same
  playlist and stays correct through a rename. One that names a playlist that
  no longer exists at all, by id or name, is removed (the default goes back to
  All wallpapers), as an interrupted deletion would have finished.

- `runtime.toml`, when the document compiled on this machine differs from the
  one on disk, for example on the first start after an update or on another
  machine (the renderer paths are this machine's). What it plays stays the
  same.
- `pairings.json`, when the wallpaper service reported a wallpaper that failed
  to play: the health sync records that one marker on that wallpaper.
- The copies 0.2.x kept before a format change, when there are any:
  `playlists.json.v1-backup`, `displays.json.v1-backup`,
  `schedules.json.v1-backup` and `schedules.json.v2-backup` in the state
  folder and `ui.toml.v1-backup` in the config folder are removed, each
  leaving one empty record in that folder's `.wall-in-one-retained`, but
  only when the file beside it still holds everything the copy does. A copy
  stays, and Tidy up lists it with the reason, when it can't be read in full, when the file
  beside it is missing, damaged or from a newer version, or when something
  in the copy is no longer in the file. The app removes the first four at its
  or its service's start, and `ui.toml.v1-backup` when a window opens. See
  [Format versions](library.md#format-versions).
- Automatic stills, the one exception that writes into your library (since
  0.1.0). After each scan, including the first one, the app captures a still
  for a moving wallpaper that has none, and captures a scene's managed still
  again when it is smaller, in both directions, than a measured display.
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
still needs the [migration recovery procedure](migrating.md#crash-recovery-and-backups).

For service details:

```sh
journalctl --user -u wall-in-one.service -b -n 80 --no-pager
```

After repairing the configuration, start the service again. Do not delete the
runtime or replace settings with defaults merely to silence an error.

## If the new interface doesn't open

If the new interface doesn't open, start `wall-in-one --ui=classic` and choose
Classic in Settings → Appearance → Interface. The classic window then opens on
every start until you choose New there again. `--ui` (listed in
`wall-in-one --help`) applies only to the start it is given on: an app that is
already running keeps its window and prints a note, so close it first.

## Updates are one-way

An update translates what older versions left. A profile from 0.1.4 or 0.2.x
opens in this version with its settings, library folders, playlists,
schedules, displays, favourites, still choices and downloads intact, and the
[migration guide](migrating.md) covers the older empty-root app and the
retired Noctalia plugin.

Nothing goes the other way. Installing an older version over a profile a
newer one has used is not supported, no version converts a profile back, and
this one keeps nothing so that an older one can run again: a file that moves
to a newer format is saved over, with no copy of the older one, and the
copies 0.2.x kept for that (the `.v1-backup` and `.v2-backup` files beside
`playlists.json`, `schedules.json`, `displays.json` and `ui.toml`) are
removed at a later start, once the file beside each still holds everything
the copy does (see [Format versions](library.md#format-versions)); one that
doesn't stays, and Tidy up lists it with the reason it stays. The upgrade from the
older empty-root app likewise removes its own pre-upgrade copies of
`settings.toml` and `runtime.toml` once it has finished, when it can prove
they are exactly what it translated; copies that an earlier version's
upgrade left behind stay, and Tidy up lists them (see the
[migration guide](migrating.md#crash-recovery-and-backups)). The
`wall-in-one-rollback` tool that 0.2.0 to 0.2.2 shipped is gone too. A
`rollback-to-0.1.4-<date>` folder in `~/.local/state/wall-in-one` is the
copy that tool made when you ran it: it is yours, nothing reads it, and you
can delete it by hand.
A system rollback of the package (an earlier NixOS or Home Manager
generation, `nix profile rollback`, an older package from your distribution)
is such a downgrade: it replaces the app and its service and leaves the
profile as the newer version saved it.

If you do it anyway, the older version does what it always did with files it
doesn't know, and what it changes or drops does not come back when you update
again:

- **0.2.0, 0.2.1 and 0.2.2** open `playlists.json`, `schedules.json`,
  `displays.json`, `favourites.json` or `pairings.json` read-only when it is
  in a format they don't know: they show what they understand of it, refuse
  every change to it, and the wallpaper service keeps the last setup it was
  given. A `settings.toml` key they don't know makes Settings read-only, and
  a newer `ui.toml` is read but never saved. They don't recognise what this
  version leaves while a download or a removal is unfinished, and their
  day-old cleanup empties partial downloads left under their own older
  temporary names, which this version keeps intact and lists in Tidy up.
- **0.1.4** has no such guard. While `playlists.json`, `schedules.json` or
  `displays.json` is in a format it doesn't know, its window shows an empty
  library and saves nothing, and the wallpaper keeps running on the last
  `runtime.toml`. Where it does save a file, it drops every field it doesn't
  know, and a file in a newer format is first moved aside as `<file>.broken`
  and written again in its own. A `settings.toml` key it doesn't know stops
  its service from starting, or from taking any change.
