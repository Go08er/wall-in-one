# Installing, and getting a launcher entry

Wall-in-One runs on any Wayland compositor that Noctalia 5 or later supports.
It needs no compositor-specific tool: the wallpaper service learns the displays
from the compositor's standard Wayland output protocols
([display discovery](display-discovery.md)).

Wall-in-One is a GUI, so it needs to be startable from a menu and not only from
a terminal. Two files do that, and the Nix package installs both:

```
share/applications/dev.goober.WallInOne.desktop
share/icons/hicolor/scalable/apps/dev.goober.WallInOne.svg
```

Both are named for `wall_in_one.paths.APPLICATION_ID`, and that is not
decoration. A compositor pairs the window on screen with the launcher entry by
matching the Wayland app-id against the entry's *filename*; nothing inside the
file can make that association. Since GtkApplication takes the app-id from the
GApplication id, `dev.goober.WallInOne` is the app-id, the desktop file's name
and the icon's name, all three at once. Rename any one of them and the window
becomes an anonymous square in the taskbar with no icon and no menu entry
behind it. See [`docs/compositor-window-rules.md`](compositor-window-rules.md)
for the other half of that story.

## Installing it

```console
$ nix profile install github:Go08er/wall-in-one
$ nix profile install .                           # from a checkout
```

A profile install is what puts the launcher entry somewhere a desktop shell
looks: `~/.local/state/nix/profile/share` is on `XDG_DATA_DIRS` in a normal Nix
session, and `share/applications` beneath it is scanned. On NixOS, the
[module](#on-nixos-the-module) (or listing the package in
`environment.systemPackages` or a Home Manager `home.packages`) does the same
thing through the system or user profile.

`nix run` deliberately does not. It builds the package and executes
`bin/wall-in-one` without adding anything to a profile, so nothing is installed
and no menu entry appears -- which is the right behaviour for trying it out.

### The wallpaper renderers

The package brings FFmpeg (thumbnails and stills) and no wallpaper renderer.
Video wallpapers need [mpvpaper](https://github.com/GhostNaN/mpvpaper), and
Wallpaper Engine scenes need
[linux-wallpaperengine](https://github.com/Almamu/linux-wallpaperengine);
Wall-in-One finds what is installed and controls it. Stills play without
either. The NixOS and home-manager modules below install both by default; a
plain `nix profile install` does not, so install them beside it:

```console
$ nix profile install nixpkgs#mpvpaper nixpkgs#linux-wallpaperengine
```

The app looks in the Nix profiles (`~/.nix-profile/bin`,
`~/.local/state/nix/profile/bin`, `/etc/profiles/per-user/$USER/bin`,
`/nix/var/nix/profiles/default/bin`, `/run/current-system/sw/bin`), then in
`~/.local/bin`, `/usr/local/bin`, `/usr/bin` and `/bin`, then on `PATH`, but
never in a store directory on `PATH`: a renderer only in a `nix shell` is not
installed ([why](packaging.md#finding-a-renderer)). It names a renderer by the
path it found it at, so updating a renderer in the same profile needs nothing
from you.

#### After installing, removing or moving a renderer

`runtime.toml` is compiled when the app saves a change and when the service
starts, not when a profile changes. So after you install a renderer that was
missing, remove one, or move one to another place (switching
`manualWallpaperInstalls`, moving from the NixOS module to home-manager or the
other way, a `nix profile remove`), the document still names where the
renderer was, which may no longer exist at once. Compile it again and let the
service load it:

```console
$ wall-in-one --write-config
$ systemctl --user restart wall-in-one.service
```

Until then, a video or scene whose renderer the document names wrongly cannot
start. The service tries it three times, then skips it for the rest of its
session, saying `cannot start mpvpaper` (or linux-wallpaperengine) in
`wall-in-one ctl status`, and the app saves that skip on the wallpaper. The
restart above forgets the skips of the session. A skip the app has already
saved stays until you open that wallpaper in the app and choose **Try again**
in its details.

### On NixOS: the module

```nix
{
  inputs.wall-in-one.url = "github:Go08er/wall-in-one";

  outputs = { nixpkgs, wall-in-one, ... }: {
    nixosConfigurations.my-machine = nixpkgs.lib.nixosSystem {
      modules = [
        wall-in-one.nixosModules.default
        { programs.wall-in-one.enable = true; }
        # ... the rest of your configuration
      ];
    };
  };
}
```

`programs.wall-in-one.enable` installs the app and both renderers into
`environment.systemPackages`: the launcher entry, the icon and the user units
come with the app, as with any profile install. It enables no unit; start the
service with `systemctl --user enable --now wall-in-one.service` (see the
README), once your session gives it its display
([below](#the-service-needs-your-sessions-display)). The renderers come from
your configuration's own nixpkgs, so your overlays apply;
`programs.wall-in-one.mpvpaperPackage` and
`programs.wall-in-one.linuxWallpaperenginePackage` choose others, and
`programs.wall-in-one.package` another build of the app.

linux-wallpaperengine is free software (GPL-3.0-or-later), so no `allowUnfree`
is needed, but it is built against a prebuilt Chromium Embedded Framework; a
configuration that refuses packages not built from source refuses it. On
aarch64-linux it is not in the binary cache and builds locally.

### With home-manager

```nix
{
  imports = [ inputs.wall-in-one.homeManagerModules.default ];
  programs.wall-in-one.enable = true;
}
```

The same options, installing into `home.packages`. The module also links the
app's user units into `~/.local/share/systemd/user` (home-manager's
`systemd.user.packages`), so the user manager finds them on any distribution;
it enables and starts nothing. After a switch that changes them, run
`systemctl --user daemon-reload`, as for any update.

### Managing the renderers yourself

```nix
programs.wall-in-one = {
  enable = true;
  manualWallpaperInstalls = true;
};
```

installs the app only. Install mpvpaper and linux-wallpaperengine however you
like, in any of the places above, before the service next starts. A renderer
that is not there can't play its kind of wallpaper: each video or scene the
service tries to play is skipped and the skip saved, as
[above](#after-installing-removing-or-moving-a-renderer), which also says how
to recover.

Outside Nix, install them from your distribution: on Arch, `mpvpaper` and
`linux-wallpaperengine-git` from the AUR (the Wall-in-One package lists both
as optional dependencies). Anything that puts the program in `/usr/bin`,
`/usr/local/bin` or `~/.local/bin` works.

### The service needs your session's display

`wall-in-one.service` is a Wayland client: it asks the compositor for its
displays, and the renderers it starts draw on them. A user unit takes its
environment from the systemd user manager, not from your compositor, so the
compositor's `WAYLAND_DISPLAY` has to be in the user manager's environment
before the unit starts. Noctalia's own unit, if you run it as one, needs the
same. See whether it is there:

```console
$ systemctl --user show-environment | grep '^WAYLAND_DISPLAY='
```

- **niri**, started with `niri-session`, puts it there. Nothing to add.
- **Sway** does not. Add to your Sway config:

  ```
  exec systemctl --user import-environment WAYLAND_DISPLAY
  ```

  `exec dbus-update-activation-environment --systemd WAYLAND_DISPLAY` does
  the same and updates D-Bus activation's environment too. On NixOS,
  `programs.sway` ships both in `/etc/sway/config.d/nixos.conf`, which only
  counts if your config keeps the default `include /etc/sway/config.d/*`.

  `systemctl --user enable` ties the unit to `graphical-session.target`, which
  plain Sway never starts. The same `nixos.conf` starts it through
  `sway-session.target`. Without such a target, start the unit after the
  import, in the same `exec` line, because Sway runs separate `exec` lines at
  the same time:

  ```
  exec "systemctl --user import-environment WAYLAND_DISPLAY; systemctl --user start wall-in-one.service"
  ```

  The companion also starts the unit when it finds the runtime stopped. Even
  then, the import has to have happened first. The Sway VM test
  (`vm-test-sway`) runs the NixOS form:
  `exec "systemctl --user import-environment WAYLAND_DISPLAY; systemctl --user start sway-session.target"`.
- **Hyprland**, in its current source, imports it itself when it starts from
  a login, unless `HYPRLAND_NO_SD_VARS` is set. It does not when it runs as a
  systemd unit, under uwsm for example, which imports it instead. If the
  check above finds nothing, add to `hyprland.conf`:

  ```
  exec-once = dbus-update-activation-environment --systemd WAYLAND_DISPLAY
  ```

  This one is not tested here: the VM tests run niri and Sway.

A unit that started before the import keeps the environment it started with:
`wall-in-one ctl status` says `WAYLAND_DISPLAY is not set` in
`output_discovery_error`. Once the import has run, restart it:

```console
$ systemctl --user restart wall-in-one.service
```

### Updating an existing app installation

Follow [Updating an existing installation](updating.md) before replacing a
running app or service. Installing a new package does not necessarily update
loaded user-unit commands or replace an older process. Keep your library paths
and config/state backups; do not rerun fresh setup as an upgrade shortcut.

### Upgrading from the retired Noctalia plugin

Do not copy the old plugin's `config.json` into the current config directory.
The formats only look related. Install the package, stop using the retired full
Luau service, and open the graphical app: its first question offers a bounded,
no-overwrite import before any fresh library setup. Headless compilation
refuses unresolved legacy data so login cannot silently publish empty defaults.

Back up both profiles first and follow
[`migrating.md`](migrating.md) for source paths, supported schemas, exact field
mapping, intentional resets, service handover and companion-plugin
limitations. Do not run the retired plugin and `wall-in-one.service` together.

The icon is found the way any themed icon is: `hicolor` is the fallback theme
every icon lookup ends at, so an SVG at `hicolor/scalable/apps/<id>.svg` under
any `XDG_DATA_DIRS` entry is picked up with no cache to regenerate. SVG needs no
`gtk-update-icon-cache` run.

## Why `Exec` is a store path

In the source tree the entry says `Exec=wall-in-one`, so the same bytes suit a
checkout, a wheel and the store. The package rewrites it during `postInstall` to
the absolute path of the wrapped binary:

```
Exec=/nix/store/...-wall-in-one-0.3.2/bin/wall-in-one
```

That rewrite is the difference between a menu entry that works and one that
reports "command not found" in a session whose `PATH` never picked up the
profile -- a common enough state when the display manager starts before the
profile is on `PATH`. It also guarantees the *wrapped* binary runs, the one
carrying `GI_TYPELIB_PATH`, the GSettings schemas and ffmpeg on `PATH`. The
renderers are not on the wrapper's `PATH`; the app looks for them in the
places [above](#the-wallpaper-renderers). Finding the displays needs no
program on `PATH`: the service asks the compositor over Wayland. `TryExec`
holds the same string and is rewritten with it.

## What the entry deliberately does not say

**`DBusActivatable`.** The app is a `GtkApplication`, so it does own the
`dev.goober.WallInOne` bus name and export `org.gtk.Actions` -- that is how
single-instance activation works, and it happens whatever this file says. Being
*activatable* is a further claim: it means the session bus may start the program
on demand, which requires a `share/dbus-1/services/dev.goober.WallInOne.service`
file naming the binary. Nothing here ships one. Setting the key without it buys
nothing where a launcher falls back to `Exec`, and costs a launch that silently
fails where it does not. If the app ever wants to be started by an action
invocation rather than by its command line, the service file and the key go in
together.

**`StartupWMClass`.** That key exists to match X11's `WM_CLASS`. Wall-in-One
supports only Wayland, where it is not consulted: the filename already does the
job. X11 sessions are not supported.

## Outside Nix

A wheel built from this tree carries both files as package data, under
`wall_in_one/data/`, because a source distribution that dropped them would leave
nothing for a packager to install. They are only data there: `site-packages` is
not a directory any desktop shell reads. A non-Nix install has to place them
itself, which is two commands:

```console
$ install -Dm644 src/wall_in_one/data/dev.goober.WallInOne.desktop \
    ~/.local/share/applications/dev.goober.WallInOne.desktop
$ install -Dm644 src/wall_in_one/data/dev.goober.WallInOne.svg \
    ~/.local/share/icons/hicolor/scalable/apps/dev.goober.WallInOne.svg
```

`Exec=wall-in-one` is left alone there, which is correct: a pip install puts
that command on the same `PATH` the session already has. The renderers come
from the distribution, as [above](#managing-the-renderers-yourself).

A distribution package also sets the install kind, so the companion can tell
it from a Nix install: `tools/set-install-kind.sh system` before building the
app, and `WALL_IN_ONE_INSTALL_KIND=system` while cargo builds the service.
`tools/check-install-kind.sh system <wall-in-one> <wall-in-one-service>`
checks the installed pair (see [packaging](packaging.md#install-kind)).

## Checking it

`nix flake check` builds a `desktop` check that runs `desktop-file-validate`
over the installed entry and rasterises the icon at 16px and 128px. It works
from the package's output paths, so a `postInstall` that put either file
somewhere else fails there rather than on someone's menu; it also checks that
the installed app and service report the same install kind. `package-closure`
fails if either renderer is in the package's closure, and `modules` evaluates
both modules with and without `manualWallpaperInstalls`.
`tests/test_packaging.py` covers the half that is about agreement rather than
validity: that the entry's filename and `Icon` key are the application id, and
that its `Exec` names a console script `pyproject.toml` actually installs.
