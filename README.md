# Wall-in-One

> [!WARNING]
> **Pre-alpha — in testing.** This is not ready to be relied on. It has never
> been run for more than a short stretch, much of its interface has had little
> clicked by a human, and the bugs found so far were found by running it rather
> than by its test suite — so assume running it longer will find more.
>
> Expect breakage, expect settings and on-disk formats to move without
> migration, and do not point it at wallpapers you would mind losing.

A wallpaper manager for Wayland, written in Python with GTK4 and libadwaita.

It manages stills and video wallpapers, keeps its own colours in sync with
[Noctalia](https://github.com/noctalia-dev/noctalia-shell)'s active palette, and
is driven either from its window or from a small control socket -- which is how
the companion Noctalia plugin talks to it.

## Status

**Working, and in use.** The library, pairings, automatic stills, playlists,
schedules, per-display assignment, the colour pipeline, and browsing two
wallpaper sites are all built and exercised against a live system.
[`DESIGN.md`](DESIGN.md) is the record: what each piece was checked against and
what the check actually showed, including the things that turned out to be
wrong.

Everything the Luau plugin it replaces could do, it now does. The one capability
not carried across is **per-output renderer settings** -- mute, FPS and scaling
are global rather than per screen.

Three things are worth knowing before you rely on it:

- **Two monitors showing two different playlists is unverified.** The machine
  this was written on has one output. Screen discovery is checked against real
  niri, and assignment is tested, but nobody has watched two screens disagree.
- **The GUI has had little human use.** More than 1,100 tests cover the logic and drive
  the widgets programmatically, which proves wiring rather than whether
  anything *looks* right. Most of the browsing interface is new.
- **It has not been through a long soak.** Bugs found so far were found by
  running it, not by reading it -- so assume running it longer will find more.

## The Noctalia plugin

There is a companion plugin at
[Go08er/goober-noctalia-plugins-v5](https://github.com/Go08er/goober-noctalia-plugins-v5),
under `wall-in-one/`. It is a thin client: a bar widget, a panel, a
Control Center shortcut and a service that launch this app and drive it over
the control socket described below. None of the wallpaper logic lives there.

The plugin is optional. This app is a complete wallpaper manager on its own;
the plugin exists so the bar can drive it without opening the window.

## Why it exists

This started as a Noctalia Luau plugin and outgrew it. Luau caps a function at
200 locals -- and a plugin entry file *is* one function, so the whole plugin
shares that budget -- and meters each callback against a CPU deadline (12 ms for
updates, 25 ms for callbacks). Those two limits trade against each other:
factoring code out to save locals adds call overhead against the deadline. A
wallpaper manager with providers, a library, and a palette browser does not fit.

So the manager became an application, and the plugin shrinks to what a plugin is
good at: a widget and a few shortcuts that poke the app.

## Install

A Nix flake, so there is nothing to build by hand and no Python environment to
manage.

```console
$ nix run github:Go08er/wall-in-one                # try it, install nothing
$ nix profile install github:Go08er/wall-in-one    # keep it
$ nix profile install .                            # from a checkout
```

Or as a NixOS / home-manager input:

```nix
{
  inputs.wall-in-one.url = "github:Go08er/wall-in-one";

  # then, in your packages list:
  #   inputs.wall-in-one.packages.${pkgs.system}.default
}
```

Flakes must be enabled. If `nix run` complains about an experimental feature,
add `experimental-features = nix-command flakes` to `/etc/nix/nix.conf` or pass
`--extra-experimental-features 'nix-command flakes'`.

A profile install also installs a launcher entry and an icon --
`share/applications/dev.goober.WallInOne.desktop` and
`share/icons/hicolor/scalable/apps/dev.goober.WallInOne.svg` -- so the app can be
started from a menu rather than only from a terminal. Both are named for the
app-id, which is what lets the compositor pair the window with the entry.
[`docs/installing.md`](docs/installing.md) covers why `nix run` gives you no
menu entry, why the packaged `Exec` is a store path, and why the entry claims
neither D-Bus activation nor a `StartupWMClass`.

`mpvpaper`, `ffmpeg`, and `linux-wallpaperengine` come with the package:
mpvpaper plays video wallpapers, ffmpeg makes thumbnails and video stills, and
linux-wallpaperengine renders true Workshop scenes and captures their stills.
The engine is deliberately a runtime dependency rather than an ambient-PATH
option because a packaged application must not make scene support depend on
how the desktop happened to launch it. A checkout/non-Nix launch reports the
renderer status under **Settings -> Playback**.

Noctalia is deliberately *not* a dependency. Without it the app falls back to a
neutral palette and everything except colour sync still works.

### Keep rotation running after the window closes

The window is a configuration surface; the service owns the control socket,
playlist timer, and schedule timer. Start it without a window with:

```console
$ wall-in-one --service
```

Opening `wall-in-one` later attaches to that same process and presents the
window. Closing the window leaves the service running. Stop it deliberately
with `wall-in-one ctl quit`.

The Nix package also installs `share/systemd/user/wall-in-one.service`. To start
the service with the graphical session:

```console
$ systemctl --user enable --now wall-in-one.service
```

The checked-in unit uses the bare `wall-in-one` command so it remains useful
outside Nix; the Nix package rewrites `ExecStart` to its wrapped store path.
If you copy the unit manually from `src/wall_in_one/data/systemd/`, make sure
`wall-in-one` is on the user manager's `PATH`, then run
`systemctl --user daemon-reload` before enabling it.

## Using it

The window follows the same path as the data: **Media -> Pairings -> Playlists
-> Display schedules**. The navigation bar at the bottom switches between four real
pages:

- **Media** is the complete crafting library, independent of what is currently
  playing. Clicking a tile updates and plays the visible one-entry **Quick
  choice** playlist; there is no separate direct-wallpaper playback path.
  Search, kind and sort narrow the library; the tile menu handles removal.
- **Pairings** gives every library item a full-page editor for its representative
  still and colour policy. Adaptive colours are generated from that still in
  the background, while installed community and custom palettes show their
  stored colour swatches. Built-in palettes can be selected, but Noctalia does
  not expose their colours without applying them, so the app says so instead of
  inventing a preview.
- **Playlists** creates, renames and deletes ordered rotations. Add media from
  the searchable library, remove it, move it earlier or later, and switch to it
  immediately; the entry's stable id is not changed by reordering.
- **Display schedules** switches the active playlist, resumes calendar control,
  chooses the default playlist, assigns a playlist to a connector, and edits
  month, weekday and local-time overrides in place. Rules lower in the list
  have higher priority: the last matching rule wins.

[`docs/library.md`](docs/library.md) is the detail: multiple library folders,
how a video finds the still that stands behind it, how search matches, what
favourites do to the rotation, which removal verb you get and why, and where
everything is stored.

### Keyboard shortcuts

| key | action |
|---|---|
| `Ctrl+Right` | Next wallpaper |
| `Ctrl+Left` | Previous wallpaper |
| `Ctrl+Shift+R` | Random wallpaper |
| `Ctrl+F` | Search the library |
| `F5` | Rescan the library |
| `Ctrl+B` | Find wallpapers online |
| `Ctrl+,` | Settings |
| `Ctrl+P` | Palettes |
| `Ctrl+?` | Keyboard shortcuts |
| `Ctrl+W` | Close the window |

The same list is in the app under **Keyboard Shortcuts** in the menu. Every
shortcut is modified, deliberately: the search box holds focus for whole
seconds at a time, and a bare `n` for "next wallpaper" would land in it. The
dialogue needs libadwaita 1.9 or newer; on anything older the keys still work
and the menu item reports that it cannot list them.

## Video wallpapers

Videos are played by mpvpaper, with the paired still set through Noctalia
underneath first, so the palette matches what is on screen even if the renderer
dies.

**Dynamics** (`Settings -> Playback`, or `wall-in-one ctl dynamics off`) pauses
videos and shows their stills instead. Blur is markedly more expensive over an
animated wallpaper, so this is a performance control as much as a battery one
-- see [`docs/niri.md`](docs/niri.md).

**Audio** is muted by default, because a wallpaper that makes noise is a
surprise. The track stays loaded rather than being disabled, which is what lets
mute and the volume setting take effect on the video already playing instead of
only on the next one -- they go over mpv's IPC, so the wallpaper does not blink.

**When covered by a window** chooses between pausing, stopping and carrying on.
This one is an mpvpaper launch flag rather than an mpv property, so it cannot be
retuned live and applies to the next video. mpvpaper warns that its automatic
options "might not work as intended", which is why "Keep playing" stays
reachable.

**Output** picks the monitor the wallpaper is applied to, or all of them. The
monitor list comes from GTK rather than from `niri msg`, so it is not tied to
one compositor, and a connector that is currently unplugged stays selected
rather than being reset. See the status caveat above for what this has and has
not been shown to do.

## Finding wallpapers

The search button in the header -- or **Find wallpapers** in the menu, or
`Ctrl+B` -- opens a dialog that searches [Wallhaven](https://wallhaven.cc) for
stills and [MotionBGS](https://motionbgs.com) for video wallpapers. Downloads
land under the first library root, and the library is rescanned when one
finishes, so the file shows up in the grid without being asked for.

Wallhaven works without an API key. The one thing a key buys is NSFW results.
Supply one through `WALLHAVEN_API_KEY`, or save it in **Settings -> Providers**,
which writes `~/.config/wall-in-one/wallhaven-api-key` at mode 0600 through a
temporary name.

That file is refused, with the reason and the `chmod` that mends it, when it is
a symlink, is not a regular file, is owned by another user, is readable or
writable by anyone else, is larger than 4 KB, or sits in a directory other
users can write to. A credential the whole machine can read is worth saying out
loud rather than using quietly. Wallhaven then simply runs unauthenticated.

[`docs/browsing.md`](docs/browsing.md) covers the filters each site understands,
how MotionBGS text searches continue through their validated tag pages, and
what the download path checks before it writes anything.

## Colour sync

Noctalia's palette generator is a pure CLI -- `noctalia theme <image>` is a
deterministic function of its arguments, with no running shell required. That
makes the whole colour story straightforward, and the app resolves its palette
in three tiers:

1. **Template** -- Noctalia renders its live 72-token palette into
   `~/.local/state/wall-in-one/palette.json` and runs
   `wall-in-one ctl reload-palette`. This is the only tier that can see a
   *built-in* palette such as Gruvbox or Nord, because those are compiled into
   the Noctalia binary and never exposed to the CLI.
2. **Generated** -- if the current palette comes from the wallpaper, regenerate
   it by running `noctalia theme` on that same wallpaper. Byte-identical to
   what Noctalia itself produced.
3. **Fallback** -- a neutral dark palette, so the app always starts.

Tier 1 needs one-time setup:

```console
$ wall-in-one --install-theme-template
$ noctalia msg templates-apply
```

That registers a `[theme.templates.user.wall-in-one]` entry in Noctalia's
settings. It is a real schema field, so Noctalia round-trips it through its own
settings writes rather than dropping it. The block is fenced with markers, the
previous file is backed up first, and a hand-written entry under the same id is
never overwritten. `--uninstall-theme-template` removes it again.

Check what you have with `--theme-status`, `--print-palette`, or `--print-css`.

**Palettes** in the menu browses what is installed: the ten built-ins,
community palettes Noctalia has cached, your own custom ones, and a fourth
group for the pre-5.x `colorschemes/` layout. That fourth group is listed with
Apply disabled and the reason stated -- Noctalia 5.0.0-beta.7 cannot apply one,
so offering the button would be a lie. The built-ins are listed without
swatches for a related reason: their names are strings in the binary and their
colours are not.

## Translucency and blur

The app draws its own translucency; blur behind it is the compositor's job. On
niri 26.04 that is a four-line window rule. See [`docs/niri.md`](docs/niri.md)
-- it covers the app-id to match, the xray caveat that matters when a video
wallpaper is running, and what to do on older niri.

## Control socket

Every verb is one line of JSON over a socket in `$XDG_RUNTIME_DIR`, created at
mode 0600. The Noctalia plugin drives the app entirely through these, so no
socket code has to live in Luau.

```console
$ wall-in-one ctl next
$ wall-in-one ctl prev
$ wall-in-one ctl random
$ wall-in-one ctl shuffle on|off|toggle
$ wall-in-one ctl cycle on|off|toggle
$ wall-in-one ctl cycle-interval 600
$ wall-in-one ctl dynamics on|off|toggle
$ wall-in-one ctl reload-palette
$ wall-in-one ctl status
$ wall-in-one ctl providers
$ wall-in-one ctl search wallhaven aurora borealis
$ wall-in-one ctl download motionbgs <identifier> [hd|4k]
$ wall-in-one ctl list [everything|stills|videos|favourites] [query]
$ wall-in-one ctl select /path/to/wallpaper.png
$ wall-in-one ctl favourites
$ wall-in-one ctl favourite /path/to/wallpaper.png
$ wall-in-one ctl unfavourite /path/to/wallpaper.png
$ wall-in-one ctl remove /path/to/wallpaper.png
$ wall-in-one ctl pairing /path/to/wallpaper.png
$ wall-in-one ctl still /path/to/wallpaper.mp4 /path/to/picture.png
$ wall-in-one ctl still /path/to/wallpaper.mp4 default
$ wall-in-one ctl palette /path/to/wallpaper.png builtin:Nord
$ wall-in-one ctl reset-pairing /path/to/wallpaper.png
$ wall-in-one ctl playlists [name]
$ wall-in-one ctl playlist-new Evening
$ wall-in-one ctl playlist-add Evening /path/to/wallpaper.png
$ wall-in-one ctl playlist-remove Evening <entry-id>
$ wall-in-one ctl playlist-use Evening|none
$ wall-in-one ctl playlist-delete Evening
$ wall-in-one ctl schedule
$ wall-in-one ctl schedule-add Evening days=sat,sun from=22:00 to=06:00
$ wall-in-one ctl schedule-remove <rule-id>
$ wall-in-one ctl quit
```

`quit` is intentional service shutdown: it releases the application's lifetime
hold and exits even when no window is open. Merely closing the window does not
stop rotation or schedules.

`providers`, `search` and `download` reach the same provider code the browse
dialog uses, so a wallpaper can be found and pulled into the library without
opening the window. `providers` and `search` print tab-separated rows with `#`
comment lines around them, which is what `cut -f1` and `while read` already
understand; the identifier comes first because it is the field `download` takes
back. Because `search` and `download` wait on a website, they are answered from
a worker rather than from the main loop, so the window does not freeze for the
duration.

`list` selects and orders through the same code the window's search box uses,
so the two cannot disagree about what `stills snow` means. `favourites` is a
separate listing from `list favourites` because it has to show entries whose
file is not in the library right now -- an unmounted drive -- which by
definition `list` cannot.

Every wallpaper resolves to a *pairing*: a representative still, an optional
moving source, and the colours it asks Noctalia for. Nothing has to be created
-- a still pairs with itself, a video pairs with whatever the conventions find
-- and only the ones you change are written down, so a better default still
reaches everything you have not spoken for. `pairing` shows one; `still` and
`palette` choose; `reset-pairing` forgets. A palette policy is `adaptive`,
`keep`, or `builtin:`/`community:`/`custom:` and a name. The path is split from
the right, so a wallpaper directory with a space in its name needs no quoting.

A **playlist** is a named, ordered list, and `playlist-use` plays one now as a
temporary override. `playlist-use none` returns control to the calendar. A
manual override lasts until it is released or the service restarts; it does
not erase or disable schedule rules. Entries have identities of their own, printed by `playlists <name>`
and taken back by `playlist-remove`, so reordering never renumbers anything and
the same wallpaper can appear twice. A list naming wallpapers that are not here
keeps them -- an unmounted drive is not a deletion -- and if none of them are
here the rotation quietly falls back to the whole library rather than stopping.
A playlist whose name has a space in it is referred to by the id `playlists`
prints beside it.

**Wallpaper Engine** content installed through Steam is picked up automatically
-- 49 wallpapers on the machine this was built on. Most Workshop items turn out
to be plain videos, which play through mpvpaper like any other; only true
`scene` wallpapers need `linux-wallpaperengine`, and their stills are captured
through it in a window without anything appearing on screen. The capture window
uses the target display's physical mode (or a 2560x1440 fallback), because the
engine's default window produces a small portrait screenshot. Managed scene
stills with the old portrait/wrong-resolution shape are regenerated
automatically and atomically; custom still choices are never overwritten. A
manual **Regenerate** control is also available on a scene's Pairings page.

**Play Wallpaper Engine scenes** is on by default and is visible under
**Settings -> Playback**. The engine is single-instance per output and other
things drive it -- Noctalia's own `linux-wallpaperengine-controller` plugin
among them -- so Wall-in-One checks for an existing owner before it starts one.
If another engine already holds the screen, the app says so and leaves the
scene's still up instead. Turn the setting off if another controller should
always own scenes. Steam's files are never deleted, moved or written beside.

A **schedule** puts the calendar in charge of which playlist is in force.
Rules take `days=`, `months=`, `from=` and `to=`, all optional, all combined
with and; a rule with none of them matches always. Times are local, inclusive
at the start and exclusive at the end, and a window whose end is before its
start wraps midnight -- `from=22:00 to=06:00` is one window. The **last**
matching rule wins, so adding a rule is how you carve an exception out of an
earlier one. When nothing matches, the configured default playlist applies.
An on-demand playlist choice sits above the calendar until **Follow schedule**
is selected. The calendar is re-read once a minute, and a changed result is
applied even while the GUI is closed.

`remove` is the only verb that destroys anything, and over a socket there is no
confirmation dialogue to fall back on. So the path must be absolute and must
match a wallpaper the scan actually produced; anything else is refused before it
reaches the code that deletes. What happens then is the same split as the tile
menu -- a downloaded wallpaper is deleted and the reply says it cannot be
undone, one of your own is moved to the trash, and if the trash is on another
filesystem it is refused rather than quietly unlinked.

Exit code 3 means no instance is running -- distinct from 1, so a caller can
react by launching it instead of reporting a failure.

## Settings

`~/.config/wall-in-one/settings.toml`, written by the Settings dialog and safe
to edit by hand. Anything out of range is clamped rather than rejected: a bad
settings file should degrade to something usable, not stop the app starting.

| key | meaning | default |
|---|---|---|
| `roots` | Folders scanned for wallpapers. Empty follows Noctalia's own `[wallpaper] directory`. The first one receives downloads and generated stills. | `[]` |
| `opacity` | Window background opacity; `1.0` is fully opaque. Clamped to a floor of `0.30`, below which the window stops being legible. | `1.0` |
| `preview_scheme` | Palette generator used when a palette is derived from a wallpaper. One of Noctalia's ten schemes. | `"m3-tonal-spot"` |
| `follow_noctalia_palette` | Apply Noctalia's palette to the app's own chrome. | `true` |
| `cycle_enabled` | Change wallpaper on a timer. | `false` |
| `cycle_interval` | Seconds between automatic changes. Clamped to 5-86400. | `300` |
| `cycle_favourites_only` | Narrow the rotation to starred wallpapers. Ignored while that would leave nothing to rotate through. | `false` |
| `shuffle` | Visit every wallpaper once before repeating. | `false` |
| `dynamics_enabled` | Play video wallpapers. Off shows their paired stills instead. | `true` |
| `video_muted` | Mute video wallpapers. Takes effect immediately. | `true` |
| `video_volume` | 0-100. Kept while muted, so unmuting lands where you left it. | `100` |
| `video_when_hidden` | What a video does when a window covers it: `pause`, `stop` or `play`. Takes effect on the next video. | `"pause"` |
| `output` | Connector the wallpaper is applied to, e.g. `eDP-1`. Empty means every output. | `""` |

The Wallhaven key is not in here -- it lives in its own 0600 file. Neither are
the favourites, which are app-maintained state rather than something you type;
both are in [`docs/library.md`](docs/library.md).

## Development

```console
$ nix develop
$ python -m wall_in_one
$ pytest tests -q
$ mypy --strict src tests
$ ruff check src tests && ruff format --check src tests
```

The last three run as flake checks: `nix flake check`. A fourth check, `desktop`,
validates the installed launcher entry and rasterises the icon, since neither
can be seen from the Python suite.

Tests that need a display are marked `gui`; tests that need a live Noctalia are
marked `noctalia`. The packaged build runs neither.

Anything touching Noctalia's settings file is tested against a sandboxed set of
XDG directories -- never the real one.

## Licence

MIT. See [`LICENSE`](LICENSE).
