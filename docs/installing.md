# Installing, and getting a launcher entry

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
behind it. See [`docs/niri.md`](niri.md) for the other half of that story.

## Installing it

```console
$ nix profile install github:goober/wall-in-one   # once published
$ nix profile install .                           # from a checkout
```

A profile install is what puts the launcher entry somewhere a desktop shell
looks: `~/.local/state/nix/profile/share` is on `XDG_DATA_DIRS` in a normal Nix
session, and `share/applications` beneath it is scanned. On NixOS, listing the
package in `environment.systemPackages` or a Home Manager `home.packages` does
the same thing through the system or user profile.

`nix run` deliberately does not. It builds the package and executes
`bin/wall-in-one` without adding anything to a profile, so nothing is installed
and no menu entry appears — which is the right behaviour for trying it out.

The icon is found the way any themed icon is: `hicolor` is the fallback theme
every icon lookup ends at, so an SVG at `hicolor/scalable/apps/<id>.svg` under
any `XDG_DATA_DIRS` entry is picked up with no cache to regenerate. SVG needs no
`gtk-update-icon-cache` run.

## Why `Exec` is a store path

In the source tree the entry says `Exec=wall-in-one`, so the same bytes suit a
checkout, a wheel and the store. The package rewrites it during `postInstall` to
the absolute path of the wrapped binary:

```
Exec=/nix/store/...-wall-in-one-0.1.0/bin/wall-in-one
```

That rewrite is the difference between a menu entry that works and one that
reports "command not found" in a session whose `PATH` never picked up the
profile — a common enough state when the display manager starts before the
profile is on `PATH`. It also guarantees the *wrapped* binary runs, the one
carrying `GI_TYPELIB_PATH`, the GSettings schemas, and mpvpaper and ffmpeg on
`PATH`. `TryExec` holds the same string and is rewritten with it.

## What the entry deliberately does not say

**`DBusActivatable`.** The app is a `GtkApplication`, so it does own the
`dev.goober.WallInOne` bus name and export `org.gtk.Actions` — that is how
single-instance activation works, and it happens whatever this file says. Being
*activatable* is a further claim: it means the session bus may start the program
on demand, which requires a `share/dbus-1/services/dev.goober.WallInOne.service`
file naming the binary. Nothing here ships one. Setting the key without it buys
nothing where a launcher falls back to `Exec`, and costs a launch that silently
fails where it does not. If the app ever wants to be started by an action
invocation rather than by its command line, the service file and the key go in
together.

**`StartupWMClass`.** That key exists to match X11's `WM_CLASS`, which GTK
derives from the program name rather than from the application id — so under
XWayland it would have to read `wall-in-one`, and under Wayland it is not
consulted at all. The filename already does the job for every case that matters
here.

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
that command on the same `PATH` the session already has.

## Checking it

`nix flake check` builds a `desktop` check that runs `desktop-file-validate`
over the installed entry and rasterises the icon at 16px and 128px. It works
from the package's output paths, so a `postInstall` that put either file
somewhere else fails there rather than on someone's menu. `tests/test_packaging.py`
covers the half that is about agreement rather than validity: that the entry's
filename and `Icon` key are the application id, and that its `Exec` names a
console script `pyproject.toml` actually installs.
