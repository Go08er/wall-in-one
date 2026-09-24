# Wall-in-One

> [!WARNING]
> **Pre-alpha — in testing.** The app has limited human testing and has not
> completed a long-running soak or physical multi-monitor validation. Back up
> your wallpaper library and settings before trying it. Supported upgrade paths
> are described in the [migration guide](docs/migrating.md); other early formats
> may not migrate automatically.

A wallpaper manager for Wayland, written in Python with GTK4 and libadwaita.

It manages stills and video wallpapers, keeps its own colours in sync with
[Noctalia](https://github.com/noctalia-dev/noctalia)'s active palette, and
is driven either from its window or through `wall-in-one ctl`. That client
routes commands across the two local control sockets and is how the companion
Noctalia plugin talks to it.

## Status

Current version: **0.1.4**, paired with Noctalia companion **0.1.3**.
This maintenance update improves upgrade stability and cleans up verified
legacy automatic stills without resetting settings or manual choices.
See the [release notes](docs/releases/v0.1.4.md) and
[update guide](docs/updating.md) before updating an existing installation.

The app manages a local library, video/still pairings, playlists, schedules,
Wallpaper Engine scenes, colour sync, and downloads from two wallpaper sites.
The core workflows have been exercised on a single-display desktop.

The core Luau workflows are present, but the authoring models are not identical.
The software supports per-display wallpaper routing, schedules and transport;
renderer tuning (mute, FPS, scaling/clamp) and cadence values remain global.
Legacy gesture bindings and several provider/capture tuning fields have no
app-owned equivalent. Scene scaling and clamp migrate from one designated
display into typed global settings. The exact import and intentional-reset list is in
[`docs/migrating.md`](docs/migrating.md).

Independent display routing, schedules, and playback controls have automated
coverage, but have not been validated with different playlists on two physical
monitors. Automated GUI checks also do not replace human visual and usability
testing.

## The Noctalia plugin

There is a companion plugin at
[Go08er/goober-noctalia-plugins-v5](https://github.com/Go08er/goober-noctalia-plugins-v5),
under `wall-in-one/`. It is a thin client: a bar widget, a panel, a
service that launches this app and drives it through `wall-in-one ctl`. None of
the wallpaper logic lives there.

The plugin is optional. This app is a complete wallpaper manager on its own;
the plugin exists so the bar can drive it without opening the window.

Upgrade the app before enabling an updated companion. Compatibility details and
the older-plugin handover are in
[`docs/migrating.md`](docs/migrating.md#companion-noctalia-plugin-compatibility).

## Why it exists

This started as a Noctalia plugin. A separate application gives library editing,
downloads, and previews their own window and worker processes, while a small
runtime keeps wallpaper playback running after that window closes.

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

Noctalia is the wallpaper-and-colour application endpoint. The library and
authoring screens still open without it, but applying a resolved entry requires
a running Noctalia shell; renderer failures remain visible in runtime status.

### Keep rotation running after the window closes

The Python window is a configuration surface. It resolves the library,
pairings, playlists and schedules into
`$XDG_STATE_HOME/wall-in-one/runtime.toml`; the small Rust service reads only
that file and owns the playlist timer, schedule timer, renderer children and
runtime socket. Start it through the packaged user unit so migration and schema
preflights run before Rust:

```console
$ systemctl --user enable --now wall-in-one.service
```

Running `wall-in-one-service` directly bypasses Python's migration preparation;
use the packaged service for normal operation.

Opening and closing the window has no effect on rotation. To compile library
changes without a window, use `wall-in-one --write-config`. The service runs
migration preparation and runtime validation before startup; a valid
last-known-good configuration can keep playback working after an ordinary
compilation error. Migration conflicts still require attention. See the
[runtime contract](docs/runtime-config.md) and [migration guide](docs/migrating.md)
for recovery details.

The packaged health timer saves renderer-failure reports while the window is
closed. It stops with the runtime and does not replay missed intervals. The
companion supplies this handoff for direct launches outside systemd.

Stop playback with `wall-in-one ctl stop`, or shut down the runtime with
`wall-in-one ctl quit`. The older `wall-in-one --service` command is a
compatibility fallback, not the packaged service implementation.

If you copy the three units manually from
`src/wall_in_one/data/systemd/`, make sure
`wall-in-one`, `wall-in-one-service` and GNU `timeout` are on the user manager's
`PATH`, then run `systemctl --user daemon-reload` before enabling it.

If the resolved runtime document itself is missing, invalid, or still uses an
obsolete schema, the exact Rust preflight leaves the unit failed after one
attempt. Repair the authoring source and explicitly recover with
`systemctl --user reset-failed wall-in-one.service` followed by
`systemctl --user restart wall-in-one.service`. A damaged authoring file alone
does not stop an otherwise-valid last-known-good runtime. Migration conflicts
remain fatal until the reported problem is resolved.

### Test it away from your desktop

The flake includes a bootable niri + Noctalia development desktop and an
automated NixOS test that saves screenshots of all five workflow tabs:

```console
$ nix run .#vm
$ nix build -L .#checks.x86_64-linux.vm-test
```

The fixtures contain only build-generated sample media and never mount the
host home, wallpaper library, Noctalia state, or Steam directory. See
[`docs/virtual-machine.md`](docs/virtual-machine.md) for guest credentials,
local companion-plugin overrides, screenshot locations, and the explicit
software-rendering/Steam/multi-monitor limits.

## Using it

The bottom navigation offers **Store, Library, Playlists, Schedules and
Settings**. The window opens on your Library. A **pairing** brings together a
wallpaper's still image, optional animation and colour policy:

- **Store** searches and downloads from Wallhaven and MotionBGS without
  leaving the main window.
- **Library** contains your pairings, independent of what is currently playing.
  Clicking a wallpaper or **Edit** opens its full-size pairing editor. Use the
  visible **Apply** action to play it; independent-display mode offers an
  explicit display choice. Right-click opens the actions menu without changing
  the wallpaper. Applying uses the one-entry **Quick choice** playlist.
  The editor also lets you choose the representative still and colours.
- **Playlists** creates, renames and deletes ordered rotations. Add pairings from
  the searchable Library rows with **+**, double-click, Enter or drag-and-drop.
  Drag a playlist row's handle to reorder it, or focus the row and use
  `Ctrl+Up` / `Ctrl+Down`. A pairing can appear more than once. Unavailable
  entries stay visible but are skipped; Play is disabled if none can play.
  See [playlist behavior and limits](docs/library.md#playlist-behavior-and-limits).
- **Schedules** chooses the default playlist and when other playlists take
  over, using month, weekday and local-time selectors. You can also choose a
  playlist temporarily or resume calendar control. Independent mode adds
  playlist assignments and playback controls for each display. Rules lower in
  the list have higher priority: the last matching rule wins.
- **Settings** manages library folders, playback defaults, providers, colours
  and appearance.

On first run, Wall-in-One does not silently turn a detected Noctalia wallpaper
directory into a place it may write. A one-time prompt shows the exact detected
default and offers either **Use default** or a folder chooser. Until one is
chosen, scans are empty and downloads and generated stills stay disabled.
Existing configured roots are left alone.

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
| `Ctrl+B` | Open Store |
| `Ctrl+,` | Settings |
| `Ctrl+P` | Palettes |
| `Ctrl+?` | Keyboard shortcuts |
| `Ctrl+W` | Close the window |

The same list is in the app under **Keyboard Shortcuts** in the menu. These
shortcuts use modifier keys so typing in the search box does not change the
wallpaper. The dialogue needs libadwaita 1.9 or newer; on anything older the
keys still work and the menu item reports that it cannot list them.

## Video wallpapers

Videos are played by mpvpaper, with the paired still set through Noctalia
underneath first, so the palette matches what is on screen even if the renderer
dies.

**Animate wallpapers** (`Settings -> Playback`, or `wall-in-one ctl dynamics off`)
controls both videos and Wallpaper Engine scenes. Turning it off releases their
renderers and shows paired still images. This also avoids repeatedly blurring
moving wallpaper behind translucent windows; see [`docs/niri.md`](docs/niri.md).

**Stop animations on battery** is a separate, optional switch, off by default.
It shows paired stills while UPower reports battery power, even with the app
window closed. Playlists and schedules can continue choosing stills. Plugging
in resumes only animations allowed by your current playback settings; it does
not undo a manual Pause or Stop. Restarted videos/scenes may begin again from
the start. The interface reports when power information is unavailable. See
[`docs/settings.md`](docs/settings.md#battery-animation-control) for details.

**Audio** is muted by default, because a wallpaper that makes noise is a
surprise. The track stays loaded rather than being disabled, which is what lets
mute and the volume setting take effect on the video already playing instead of
only on the next one -- they go over mpv's IPC, so the wallpaper does not blink.
Wallpaper Engine scenes remain silent. Their engine accepts audio only at
launch, so coupling it to this live video slider would restart a scene on every
step while claiming otherwise; separate scene-audio controls are deferred.

**Wallpaper Engine frame rate** is a 1–240 FPS native scene-rendering limit (30
by default). Video wallpapers keep their source rate: mpv's post-decode FPS
filter would still decode every frame, so Wall-in-One does not claim it as a
performance control without an end-to-end measurement showing a real benefit.

**Smooth low-frame-rate videos** can use mpv's display-resample interpolation.
Oversample is the sharper, cheaper default experiment; Linear blends more
strongly and may ghost. Wall-in-One reads the active refresh from niri at each
video hand-over and supplies the complete mpv option set together. A named
output uses its rate; All outputs is smoothed only when every attached display
reports the same rate. Unknown or mixed rates keep ordinary source cadence
rather than using a hard-coded number or a partly effective setup. This does
not alter or synthesize the source FPS.

**Hardware video decoding** remains on by default. Turning it off forces
software decoding and is intended as a diagnostic comparison for corruption,
tearing, or driver-specific artifacts—not as a general performance tweak.

**When covered by a window** chooses between pausing, stopping and carrying on.
This one is an mpvpaper launch flag rather than an mpv property, so it cannot be
retuned live and applies to the next video. mpvpaper warns that its automatic
options "might not work as intended", which is why "Keep playing" stays
reachable.

**Displays** defaults to one mirrored wallpaper, cursor and schedule everywhere.
Independent mode gives every live connector its own assignment, schedule
winner, cursor, renderer and temporary playlist/Cycle/Shuffle overrides. GTK
monitor names are merged with the Rust service's niri snapshot, so a connector
the compositor knows remains targetable even when GTK cannot name it; detached
saved assignments and rules remain visible rather than being reset. Noctalia
still has one shell-wide palette, so **Colours follow** is an explicit display
choice. When that display is detached the saved choice stays intact and status
shows the deterministic live fallback actually in force. See the status caveat
above for what has and has not been watched on physical multi-monitor hardware.

## Finding wallpapers

The search button in the header -- or **Store** in the menu, or
`Ctrl+B` -- opens Store, which searches [Wallhaven](https://wallhaven.cc) for
stills and [MotionBGS](https://motionbgs.com) for video wallpapers. Downloads
land under the explicitly chosen first library root, and the library is
rescanned when one finishes, so the file shows up in the grid without being
asked for. With no root configured, Store refuses the download and points to
Settings instead of guessing a destination.

Store uses explicit Previous/Next photo pages with at most 40 heavyweight GTK
cards alive at once. A 250-result MotionBGS search remains fully reachable, but
walking it no longer leaves hundreds of decoded card widgets consuming memory;
search text, cursor, filters and batch picks survive page changes.

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

The app follows Noctalia's colours using three tiers:

1. **Template** -- Noctalia renders its live 72-token palette into
   `~/.local/state/wall-in-one/palette.json` and runs
   `wall-in-one ctl reload-palette`. This is the only tier that can see a
   *built-in* palette such as Gruvbox or Nord, because those are compiled into
   the Noctalia binary and never exposed to the CLI.
2. **Generated approximation** -- without a usable template, a wallpaper-based
   palette can be generated using Noctalia's selected generator, saved
   palette-driving image and pure-black setting. This is not a confirmed
   snapshot of the shell's colours; high-contrast mode requires the template.
3. **Fallback** -- a neutral light or dark palette matching the shell's mode
   when available, so the app still starts without working colour integration.

Tier 1 needs one-time setup:

```console
$ wall-in-one --install-theme-template
$ noctalia msg templates-apply
```

That registers a `[theme.templates.user.wall-in-one]` entry in Noctalia's
settings. It is a real schema field, so Noctalia round-trips it through its own
settings writes rather than dropping it. The bundled template is copied once to
a content-addressed file under Wall-in-One's state directory, without replacing
an existing name. The settings block is fenced with markers, the previous file
is backed up first, and a hand-written entry under the same id is never
overwritten. `--uninstall-theme-template` removes the settings block again.

Check what you have with `--theme-status`, `--print-palette`, or `--print-css`.
Palette updates are event-driven. A valid template file does not prove that
every later palette change rendered successfully, particularly within the
same light/dark mode. If app colours stop matching, check
`wall-in-one --theme-status` and retry `noctalia msg templates-apply`; follow
any reported registration or template error before trying again.

**Palettes** in the menu browses what is installed: the ten built-ins,
community palettes Noctalia has cached, your own custom ones, and a fourth
group for the pre-5.x `colorschemes/` layout. That fourth group is listed with
Apply disabled and the reason stated -- Noctalia v5 cannot apply that legacy format,
so its Apply button is unavailable. The built-ins are listed without
swatches for a related reason: their names are strings in the binary and their
colours are not. Palette directories are discovered and parsed on one bounded
filesystem worker rather than GTK's interface thread. The browser and each
pairing's colour-policy picker search the complete catalogue but construct only
24 rows at a time, with an explicit loader for the next page. An ordinary
rescan keeps unchanged row, focus and scroll identity while showing
"Refreshing"; a palette saved by this app invalidates the old snapshot until
the post-write generation lands, so an edited entry cannot be resurrected by a
late scan.

## Translucency and blur

The app draws its own translucency; blur behind it is the compositor's job. On
niri 26.04 that is a four-line window rule. See [`docs/niri.md`](docs/niri.md)
-- it covers the app-id to match, the xray caveat that matters when a video
wallpaper is running, and what to do on older niri.

## Control sockets

`wall-in-one ctl` routes runtime commands to the Rust service and authoring or
provider commands to the Python app, with one-line JSON over mode-0600 sockets.
The complete verb reference and the behaviour behind it are in
[`docs/control-socket.md`](docs/control-socket.md).

## Settings

The Settings tab writes `~/.config/wall-in-one/settings.toml`, which can also be
edited by hand. Invalid or unreadable settings at startup open a recovery
window instead of the main window. Use **Open settings file**, repair the
reported problem, then select **Try again**. Settings edits also refuse to
overwrite a file that became invalid while the app was open. The unattended
`--write-config` compiler also rejects invalid settings and preserves the
last-known-good runtime document.
See [settings and manual repair](docs/settings.md) for the keys, defaults and
backup-first recovery steps.

## Upgrades and migration

For an existing installation, start with [updating Wall-in-One](docs/updating.md).
It covers replacing the app and service together and checking which build is
actually running before enabling new features. Installing a package alone does
not switch an already-running service to that build.

The GUI, headless compiler, and packaged unit's Python preflight first check
for the exact shipped schema-2 Python/Rust profile. When its independent
settings, runtime, authoring, Noctalia-root, managed-marker, and capture
evidence all agree, Wall-in-One automatically keeps the original root and
basename captures, compiles schema 4, and resumes the active playlist. A
genuinely fresh install writes no migration state and continues to the
library-root prompt; ambiguous or malformed evidence fails closed.

Separately, the first graphical launch can detect retired
`goober/wall-in-one` Noctalia data before creating a fresh profile. That import
is an explicit user choice, leaves every legacy byte untouched, refuses to
merge with current authoring, and can resume an exact interrupted transaction.
Start with [`docs/migrating.md`](docs/migrating.md); it covers both boundaries,
CLI status, schema mapping, known losses, companion compatibility, and rollback.

## Development

Development uses CPython 3.14. Provider HTML parsing runs in one lazily shared
`InterpreterPoolExecutor`, so CPU-heavy scraper work has an independent GIL
instead of interrupting GTK's frame loop. Network waits, subprocess work and
all PyGObject code deliberately stay out of that pool. This is the ordinary
GIL-enabled CPython build, not free-threaded Python: GTK and every widget stay
on one main thread, bounded worker lanes do only I/O/subprocess or immutable
data work, and the small Rust daemon owns the renderer processes. The goal is
a responsive interface separated from rendering, not unconstrained shared
state across threads.

```console
$ nix develop
$ python -m wall_in_one
$ pytest tests -q
$ mypy --strict src tests
$ ruff check src tests && ruff format --check src tests
```

`nix flake check` is the complete local gate: it runs the packaged Python and
Rust tests, all display-backed GTK tests under an isolated Xvfb server, ruff,
strict mypy, the desktop/unit packaging check, and (on x86_64-linux) the niri +
Noctalia desktop VM. The `desktop` check validates the installed launcher and
systemd unit and rasterises the icon, since none can be seen from the Python
suite.

The [GitHub Actions workflow](.github/workflows/ci.yml) evaluates the complete
flake and builds the Python, Rust, lint, type and packaging checks on every
push to `main` or `release/**` and on every pull request. The hardware-heavier
niri + Noctalia VM runs weekly, on manual dispatch, and for every release-branch
push. Hosted runners do not promise KVM, so ordinary main/PR changes keep that
software-emulated desktop boot out of the fast signal. The workflow is
read-only and cancels obsolete runs for the same ref.

Tests that need a display are marked `gui`; tests that need a live Noctalia are
marked `noctalia`. The packaged build remains display-independent and excludes
both. The separate `gui-tests` flake check supplies Xvfb, fails if a GUI test is
skipped, and is part of the fast GitHub Actions job; it does not contact a live
desktop or Noctalia session.

Anything touching Noctalia's settings file is tested against a sandboxed set of
XDG directories -- never the real one.

## Licence

MIT. See [`LICENSE`](LICENSE).
