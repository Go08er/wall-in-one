# Wall-in-One

> [!WARNING]
> **Pre-alpha — in testing.** The app has limited human testing and has not
> completed a long-running soak or physical multi-monitor validation. Back up
> your wallpaper library and settings before trying it. Supported upgrade paths
> are described in the [migration guide](docs/migrating.md); other early formats
> may not migrate automatically. Updates are one-way: installing an older version
> over a profile a newer one has used isn't supported (see the
> [update guide](docs/updating.md#updates-are-one-way)).

A wallpaper manager for Wayland, written in Python with GTK4 and libadwaita.
X11 sessions are not supported.

It manages stills and video wallpapers, keeps its own colours in sync with
[Noctalia](https://github.com/noctalia-dev/noctalia)'s active palette, and
is driven either from its window or through `wall-in-one ctl`. That client
routes commands across the two local control sockets and is how the companion
Noctalia plugin talks to it.

## Status

Current version: **0.3.2**, paired with Noctalia companion **0.2.1**
(Noctalia 5.2; older Noctalia hosts keep companion 0.1.3).
0.3.0 makes the redesigned interface the default, with every page ported;
the classic window stays one Settings choice (or `--ui=classic`) away. It
builds on 0.2.x, which updated 0.1.4 directly and reads every 0.1.4 file as
it is. See the release notes for [0.3.2](docs/releases/v0.3.2.md),
[0.3.1](docs/releases/v0.3.1.md),
[0.3.0](docs/releases/v0.3.0.md),
[0.2.2](docs/releases/v0.2.2.md), [0.2.1](docs/releases/v0.2.1.md) and
[0.2.0](docs/releases/v0.2.0.md), and the [update guide](docs/updating.md),
before updating an existing installation.

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
renderer status under **Settings → Advanced → Scenes**.

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
automated NixOS test that saves screenshots of five workflow pages:

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

Wall-in-One opens on your Library. The sidebar lists the pages, and `Alt+1` to
`Alt+6` reach them in order:

- **Library** shows every wallpaper in your library folders. Click one to open
  its details beside the grid: Apply, its favourite star, its colours and light
  or dark, and for a video or scene the still that stands behind it. A card's
  menu (right-click, or ⋯) applies it to all displays or one, adds it to a
  playlist, resets it or removes it, and **Select** acts on several cards at
  once. Applying uses the hidden one-entry "Your pick" playlist.
- **Store** searches and downloads from Wallhaven and MotionBGS without
  leaving the main window.
- **Playlists** has a sidebar row for each of your playlists, then **All
  wallpapers**, which follows the library, and **New playlist**. A playlist's
  page adds wallpapers from a picker or a drag from the Library, reorders them
  by a row's handle or `Ctrl+Up` / `Ctrl+Down`, removes them with Undo, and can
  give the playlist its own interval and shuffle. A wallpaper can appear more
  than once. Entries that are missing or that playback skips stay visible and
  are skipped; **Play now** is off if none can play. See [playlist behavior and
  limits](docs/library.md#playlist-behavior-and-limits).
- **Schedule** shows the week and the rules that choose a playlist by day, time
  of day, month and display. Where rules overlap, the higher one in the list
  plays. **When nothing is scheduled** is the default playlist, and the **Now**
  card says what is playing and why.
- **Displays** puts the same wallpaper on every display or runs each display
  separately, with its own playlist, cycle and shuffle, and chooses which
  display the desktop colours follow.
- **Settings** manages library folders, playback defaults, displays, colours
  and palettes, the window's look, the Wallhaven key, the video and scene
  renderers, Tidy up, and the interface to start, and keeps a log of this
  session.

The player bar along the bottom says what is on screen and why. It has
shuffle, previous, play/pause, next and "Change wallpaper automatically"; its ⋯
menu adds Resume schedule, Random wallpaper, Stop animation and "Why this
wallpaper?". While displays run separately it can act on one display.

On first run, Wall-in-One does not silently turn a detected Noctalia wallpaper
directory into a place it may write. A welcome screen shows the exact detected
default and offers **Use default**, **Choose folder manually** or **Not now**.
Until one is chosen, scans are empty and downloads and generated stills stay
disabled. **Not now** saves nothing: a notice under the header asks again, and
so does the next start. Existing configured roots are left alone. Settings from
the older Noctalia plugin are asked about first, on the same screen (see the
[migration guide](docs/migrating.md#importing-the-retired-noctalia-luau-plugin)).

Wall-in-One opens the interface saved in Settings → Appearance → Interface, or,
with nothing saved there, the new one, the default. To use the classic window
instead, choose **Settings → Appearance → Interface → Classic**. It takes
effect the next time Wall-in-One starts, and **New** in the same row brings the
new interface back. `wall-in-one --ui=next` or `--ui=classic` overrides the
saved choice for that start, and `wall-in-one --help` lists it. If the new
interface doesn't open, start `wall-in-one --ui=classic` and choose Classic in
Settings → Appearance → Interface. The classic window has the same workflows on
a bottom navigation (Store, Library, Playlists, Schedules and Settings), with a
full-size pairing editor where the new one has its details pane.

[`docs/interface.md`](docs/interface.md) describes every page of the new
interface in detail. [`docs/library.md`](docs/library.md) is the detail on the
library: multiple library folders, how a video finds the still that stands
behind it, how search matches, what favourites do to the rotation, which
removal you get and why, and where everything is stored.

### Keyboard shortcuts

| key | action |
|---|---|
| `Ctrl+Space` | Play or pause animation |
| `Ctrl+Right` | Next wallpaper |
| `Ctrl+Left` | Previous wallpaper |
| `Ctrl+Shift+R` | Random wallpaper |
| `Ctrl+F` | Search: the Library's, or the Store's or Settings' while one is shown |
| `F5` | Rescan the library (also in the main menu) |
| `Ctrl+B` | Open Store |
| `Ctrl+N` | New playlist |
| `Ctrl+,` | Settings |
| `Ctrl+P` | Palettes |
| `Alt+1` … `Alt+6` | Library, Store, Playlists, Schedule, Displays, Settings |
| `Enter` / `Ctrl+Enter` | On a Library card: open its details / apply it |
| `Ctrl+?` | Keyboard shortcuts |
| `Ctrl+W` | Close the window |

The same list is in the app under **Keyboard shortcuts**, in the main menu and
in Settings → About. These shortcuts use modifier keys so typing in a search
box does not change the wallpaper. The list needs a libadwaita that has its
shortcuts dialog; on an older one the keys still work and the menu item does
nothing. The classic window has the same keys except `Ctrl+Space`, `Ctrl+N`
and the `Alt` keys.

## Video wallpapers

Videos are played by mpvpaper, with the paired still set through Noctalia
underneath first, so the palette matches what is on screen even if the renderer
dies.

**Animate wallpapers** (Settings → Playback, or `wall-in-one ctl dynamics off`)
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

**Store** in the sidebar (or `Ctrl+B`) searches
[Wallhaven](https://wallhaven.cc) for stills and
[MotionBGS](https://motionbgs.com) for video wallpapers. Type in its search box,
or browse what is popular; the filters sit inline under the provider row.
Downloads go one at a time into the explicitly chosen first library folder, and
the library is rescanned when one finishes, so the file shows up in the Library
without being asked for. With no library folder configured, the Store refuses
the download instead of guessing a destination.

The Store shows at most 40 results to a page, with Previous and Next. A
250-result MotionBGS search remains fully reachable, but walking it no longer
leaves hundreds of decoded card widgets consuming memory; the search, the
filters and your picks survive page changes.

Wallhaven works without an API key. The one thing a key buys is NSFW results.
Supply one through `WALLHAVEN_API_KEY`, or save it in **Settings → Online
sources**, which writes `~/.config/wall-in-one/wallhaven-api-key` at mode 0600
through a temporary name.

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

In the new interface, Settings → Colours → **Noctalia template** shows whether
the registration is installed, missing or needs repair (with the reason), and
its Install or Repair button does the same as `--install-theme-template`. It
writes only when you press it, and not while Wall-in-One's own upgrade is
unfinished. Installing doesn't render: Noctalia renders the template the next
time its colours change, and until it has, the **Render the colours now** row's
**Render now** does what `noctalia msg templates-apply` does, re-rendering all
of Noctalia's templates, not only Wall-in-One's.

Check what you have with `--theme-status`, `--print-palette`, or `--print-css`.
Palette updates are event-driven. A valid template file does not prove that
every later palette change rendered successfully, particularly within the
same light/dark mode. If app colours stop matching, check
`wall-in-one --theme-status` and retry `noctalia msg templates-apply`; follow
any reported registration or template error before trying again.

**Palettes** (Settings → Colours → Palettes → **Manage…**, or `Ctrl+P`)
browses what is installed: the ten built-ins,
community palettes Noctalia has cached, your own custom ones, and a fourth
group for the pre-5.x `colorschemes/` layout. That fourth group is listed with
Apply disabled and the reason stated -- Noctalia v5 cannot apply that legacy format,
so its Apply button is unavailable. The built-ins are listed without
swatches for a related reason: their names are strings in the binary and their
colours are not. Palette directories are discovered and parsed on one bounded
filesystem worker rather than GTK's interface thread. The manager searches the
complete catalogue but builds only 24 rows at a time, with a button that loads
the next ones; a wallpaper's palette choices in the Library list the community
palettes 24 at a time the same way. A rescan says "Refreshing installed
palettes…" while it runs, and a palette saved by this app invalidates the old
snapshot until the post-write generation lands, so an edited entry cannot be
resurrected by a late scan. The manager also duplicates a palette into yours
and edits its colours, and previews every scheme on one of your stills.

No two palettes you can pick share a name. A copy starts as "Nord copy"
("Nord copy 2" when that is taken); the editor says as you type when a name is
taken, and Save waits until it is free. Saving a new palette under a name a
built-in, community or custom palette already has (ignoring case) is refused,
with nothing written over: every window saves through the same check. A
legacy palette's name is free to reuse, since copying it forward is how it
comes back.

## Translucency and blur

The app draws its own translucency: choose the Translucent or Frosted window
style in the main menu or Settings → Appearance. Frosted blurs the wallpaper
itself; behind Translucent, blur is the compositor's job. On niri 26.04 that is
a four-line window rule. See [`docs/niri.md`](docs/niri.md)
-- it covers the app-id to match, the xray caveat that matters when a video
wallpaper is running, and what to do on older niri.

## Control sockets

`wall-in-one ctl` routes runtime commands to the Rust service and authoring or
provider commands to the Python app, with one-line JSON over mode-0600 sockets.
The complete verb reference and the behaviour behind it are in
[`docs/control-socket.md`](docs/control-socket.md).

## Settings

Settings writes `~/.config/wall-in-one/settings.toml`, which can also be
edited by hand. Invalid or unreadable settings at startup open a recovery
window instead of the main window. Use **Open settings file**, repair the
reported problem, then select **Try again**. Settings edits also refuse to
overwrite a file that became invalid while the app was open. The unattended
`--write-config` compiler also rejects invalid settings and preserves the
last-known-good runtime document.
See [settings and manual repair](docs/settings.md) for the keys, defaults and
backup-first recovery steps.

### GPU acceleration for the window

**Settings → Appearance → GPU acceleration** (also on the new interface's
Settings page) decides how the next start draws Wall-in-One's own window. It
does not touch wallpaper playback, which the service's renderers do. On, the
default, the app leaves the choice to GTK as every earlier release did, and
GTK uses the GPU when it can. Off starts the window with GTK's software
renderer by setting `GSK_RENDERER=cairo`, which GTK programs the app starts
inherit too. A `GSK_RENDERER` you set yourself always wins over the setting.
It takes effect the next time Wall-in-One starts and is saved in `ui.toml`.
An instance that is already running keeps the renderer it started with, a
`--service` one included (closing its window doesn't end it): quit the app
and start it again to switch.

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
CLI status, schema mapping, known losses, and companion compatibility.

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
$ tools/isolated.sh python -m pytest tests -q -m "not gui"
$ tools/isolated.sh python -m pytest tests -q -m gui
$ mypy --strict src tests
$ ruff check src tests && ruff format --check src tests
```

Run the tests only through `tools/isolated.sh` or the flake's checks (below),
never as a bare `pytest`. The tests create, edit and delete Wall-in-One's
files and start child processes, and from a developer shell they would find
your real profile, session bus and desktop. `tools/isolated.sh` gives the
command a throwaway home and XDG directories, removed afterwards, dead D-Bus
addresses, no portals and none of your session's display addresses, and runs
it on a headless Wayland compositor of its own with the cairo renderer, the
way the `gui-tests` check does. It also works outside `nix develop`.

That compositor comes from `tools/headless-wayland.sh`: Weston's headless
backend with the pixman renderer (no GPU), one 1600×1200 output, and its
desktop shell with no panel, lock screen, animations or idle blanking, so a
window gets the size it asks for. Its socket is in a private directory of its
own, and it stops, with its directory, when the command ends. Wall-in-One
supports only Wayland, and nothing in testing uses X11: GTK is told
`GDK_BACKEND=wayland`, and no test gets a `DISPLAY`.

GUI tests reach that compositor and nothing else. `tests/conftest.py` drops
your session's `WAYLAND_DISPLAY`, `WAYLAND_SOCKET` and `NIRI_SOCKET`, and takes
the socket the script names in `WIO_TEST_WAYLAND_SOCKET` only after checking
that it is not your desktop's: not in `/run/user/<uid>` or the runtime
directory the run started with, not one of the sockets there under another
name, and served by a headless Weston. A GUI test without such a compositor
fails, naming the reason; it is never skipped.

`tests/conftest.py` sandboxes the test process too, and refuses and reports
any write to your real Wall-in-One and Noctalia files. That guard is a
backstop, not a substitute: it cannot see what a child process, native code
or another program does.

Without `GSK_RENDERER=cairo`, GTK picks Vulkan or GL when it finds a GPU, and
the tests' timing would then depend on the machine: the gate's sandbox has no
GPU, and software rendering is slow enough to change what the tests see.

If your login session exports `LD_LIBRARY_PATH`, `nix develop` inherits it. It
can point the flake's ffmpeg at a library built against a newer glibc. ffmpeg
then fails with an error such as `GLIBC_2.43 not found`, and about a dozen GUI
tests fail with it. `tools/isolated.sh` runs the command without it.

`nix flake check` is the complete local gate: it runs the packaged Python and
Rust tests, all display-backed GTK tests on an isolated headless Wayland
compositor, ruff, strict mypy, the desktop/unit packaging check, and (on
x86_64-linux) the niri + Noctalia desktop VM. The `desktop` check validates
the installed launcher and systemd unit and rasterises the icon, since none
can be seen from the Python suite.

The [GitHub Actions workflow](.github/workflows/ci.yml) evaluates the complete
flake and builds the Python, Rust, lint, type and packaging checks on every
push to `main` or `release/**` and on every pull request. The hardware-heavier
niri + Noctalia VM runs weekly, on manual dispatch, and for every release-branch
push. Hosted runners do not promise KVM, so ordinary main/PR changes keep that
software-emulated desktop boot out of the fast signal. The workflow is
read-only and cancels obsolete runs for the same ref.

Tests that need a display are marked `gui`; tests that need a live Noctalia are
marked `noctalia`. The packaged build remains display-independent and excludes
both. The separate `gui-tests` flake check supplies the headless compositor,
fails if a GUI test is skipped, and is part of the fast GitHub Actions job; it
does not contact a live desktop or Noctalia session.

Anything touching Noctalia's settings file is tested against a sandboxed set of
XDG directories -- never the real one.

### The new interface (the default)

The redesigned interface from `prototypes/ui-redesign` has every page:
the Library, Playlists, Schedule, Displays, the Store and Settings, all over
the app's real library, stores and runtime status. It is what Wall-in-One
opens: `ui/next/window.py`'s `NextWindow`. What each page does is in
[`docs/interface.md`](docs/interface.md); this section is how it is built.

The interface is chosen in **Settings → Appearance → Interface → Classic** (or
New), saved in `ui.toml`, and takes effect the next time Wall-in-One starts.
The flag `wall-in-one --ui=next` or `--ui=classic` overrides the saved choice
for that start. Without the flag, the app opens the interface saved in
Settings, otherwise the default, which is now New. With neither, the app opens
the new interface.

- **How it saves.** Each change goes through the save classic uses for it, one
  at a time on the app's ordered authoring lane. Each page shows what was
  saved: a change that fails is reported, and the page goes back to what is
  saved.

- **First run** is the application's (`Application._ask`): the new window is
  a `FirstRunHost` and asks the same questions on its welcome screen, while
  classic gets them as dialogs.

- **One set of widgets.** Every page in `ui/next/` is the prototype's own:
  `prototypes/ui-redesign` runs the same modules over its dummy data.
  - **The boundary** is the `AppState` Protocol in `ui/next/state.py`, plus
    one Protocol per page area: `store_state.py`, `playlists_state.py`,
    `schedule_state.py` and `settings_state.py`.
  - **The app's side** is `ui/next/real_state.py` and its `real_*.py`
    modules.

- **One process, one interface.**
  - **The choice belongs to the process that starts the app:** the desktop
    entry, `ctl open` with no app running, and `--service` all read the saved
    choice when they start it.
  - **A second launch** that reaches an already-running instance presents that
    instance's window unchanged. A second `wall-in-one --ui=…` prints a note
    that its `--ui` was not applied. Close the app and start it again to
    switch.
  - **The forwarded activation** carries only the package and the page,
    because older builds fix its shape.

- **`ctl open <page>`** presents the window either way.
  - Every page is in the new interface: the Library (`media`), the Store
    (`browse`), Playlists (`playlists`), Settings (`settings`) and Schedule
    (`schedules`, and its alias `displays`).
  - A page the new interface lacks would answer `not-in-new-ui`, with exit
    status 1.
  - When no app is running, `ctl open` launches the interface chosen in
    Settings.

- **The seams.**
  - `Application` talks to its window only through the `WindowServices`
    Protocol (`ui/window_services.py`), which mypy checks both windows against.
  - Runtime status goes through one `RuntimeStatusModel`
    (`ui/status_model.py`). The classic window receives it through a
    forwarding subscriber; the new window subscribes directly.

## Licence

MIT. See [`LICENSE`](LICENSE).
