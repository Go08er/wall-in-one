# Wall-in-One

> [!WARNING]
> **Pre-alpha — in testing.** This is not ready to be relied on. It has never
> been run for more than a short stretch, much of its interface has had little
> clicked by a human, and the bugs found so far were found by running it rather
> than by its test suite — so assume running it longer will find more.
>
> Expect breakage. The retired Noctalia Luau plugin has an explicit,
> no-overwrite importer, and the one shipped schema-2 Python/Rust profile has a
> narrowly evidence-gated automatic upgrade. Other pre-alpha formats may still
> move without migration. Do not point it at wallpapers you would mind losing.

A wallpaper manager for Wayland, written in Python with GTK4 and libadwaita.

It manages stills and video wallpapers, keeps its own colours in sync with
[Noctalia](https://github.com/noctalia-dev/noctalia)'s active palette, and
is driven either from its window or through `wall-in-one ctl`. That client
routes commands across the two local control sockets and is how the companion
Noctalia plugin talks to it.

## Status

**Working, and in use.** The library, pairings, automatic stills, playlists,
schedules, the colour pipeline, and browsing two wallpaper sites are all built
and exercised against a live one-output system. Independent multi-display
behavior is implemented and covered separately by software tests. The test
suite checks the logic, protocols, packaging and widget wiring; the limits
below say what still has not been demonstrated by real use.

The core Luau workflows are present, but the authoring models are not identical.
The software supports per-display wallpaper routing, schedules and transport;
renderer tuning (mute, FPS, scaling/clamp) and cadence values remain global.
Legacy gesture bindings and several provider/capture tuning fields have no
app-owned equivalent. Scene scaling and clamp migrate from one designated
display into typed global settings. The exact import and intentional-reset list is in
[`docs/migrating.md`](docs/migrating.md).

Three things are worth knowing before you rely on it:

- **Multi-display support is theoretical until physical validation.**
  Independent routes, schedules, cursors, renderers and transport are supported
  in software and covered by automated tests. The development machine and
  desktop VM each expose one output; the behavior has not been validated with
  two physical monitors showing different playlists.
- **The GUI has still had limited human use.** Tests number in the thousands,
  cover the logic, and drive widgets programmatically, which proves wiring
  rather than whether anything *looks* right. The browse screen, playlist
  reordering and colour sync have now had real use and real bug reports; much
  of the rest has not.
- **It has not been through a long soak.** Bugs found so far were found by
  running it, not by reading it -- so assume running it longer will find more.

## The Noctalia plugin

There is a companion plugin at
[Go08er/goober-noctalia-plugins-v5](https://github.com/Go08er/goober-noctalia-plugins-v5),
under `wall-in-one/`. It is a thin client: a bar widget, a panel, a
service that launches this app and drives it through `wall-in-one ctl`. None of
the wallpaper logic lives there.

The plugin is optional. This app is a complete wallpaper manager on its own;
the plugin exists so the bar can drive it without opening the window.

Companion revisions through `a5e23c9` do not require status
version 2 or expose the authoritative independent-route fields, their
direct-runtime fallback does not persist a newly observed Borked item, and
their eight-second callback deadline is shorter than Wall-in-One's legitimate
45-second action bound. The matching companion requires status version 2,
requests the health hand-off after a non-durable crash finding, and allows 55
seconds for the bounded action. This tree's `flake.lock` pins the reviewed
companion revision `a17eb70f653afb4cf5c04afc912cdca8b14ac06e`. This v0.1.2
app follow-up does not change that source or protocol. A machine moving from an
older companion must complete the compatible app's schema-4 cutover before
loading the strict client; until then, use the packaged service/health timer
and the app's own status as the authority. The exact compatibility boundary is
recorded in
[`docs/migrating.md`](docs/migrating.md#companion-noctalia-plugin-compatibility).

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

Running `wall-in-one-service` directly is a schema-4-only diagnostic path; it
bypasses those Python migration preflights.

`wall-in-one --write-config` performs the same compilation without importing
GTK or opening a window. Before every systemd-managed service start, the
packaged unit runs a migration-aware form of that compiler and then asks
Rust's production parser to validate the exact surviving document without
claiming a socket or renderer. An upgrade can therefore regenerate an older
schema without waiting for somebody to open the GUI, while a missing,
malformed, or still-obsolete document stops before the Rust main process.
Opening and closing `wall-in-one` later has no effect on rotation.

`wall-in-one --sync-runtime-health` is the matching headless persistence bridge.
It reads one atomic Rust status snapshot, maps newly reported borked entries
back to app-owned media, writes their pairing health, recompiles, and asks Rust
to reload. It never opens GTK, never clears a marker merely because a bounded
snapshot omitted it, and leaves the last-known-good runtime document untouched
if compilation fails. A shell or bar integration may invoke it after status
reports a taboo entry; Rust never runs Python or writes authoring data itself.
The packaged service also couples a non-persistent systemd timer to Rust and
runs this bridge 30 seconds after startup and 30 seconds after each prior sync
finishes. A no-report status returns before any authoring scan/write.

The retained `wall-in-one --service` is a compatibility fallback during this
transition, not the packaged unit's implementation. Stop the Rust runtime
deliberately with `wall-in-one ctl quit`.

The Nix package also installs `share/systemd/user/wall-in-one.service` plus its
health-sync service/timer. To start the service with the graphical session:

```console
$ systemctl --user enable --now wall-in-one.service
```

The unit first runs `wall-in-one --service-startup-prepare`. Deployed-upgrade
and unresolved legacy-migration failures remain fatal; only an ordinary
current-profile compilation error may leave the previous resolved document
byte-for-byte intact as a candidate. A second `wall-in-one-service
--check-config` preflight loads that exact candidate through Rust's production
parser without claiming a socket or renderer. Only a consumable schema-4
document reaches the Rust main process, so a valid last-known-good document can
keep unattended rotation alive while schema 2, malformed TOML, and missing
state stop once. The compiler validates every known setting type/range and
likewise refuses unreadable pairings, playlists, schedules, display
assignments, or favourites until the named file is repaired.

The runtime claims its mode-0600 socket before applying anything, so a second
instance loses without changing the wallpaper. At graphical-session startup it
retries only desktop-readiness failures for a bounded eight seconds; Noctalia
and niri helper calls themselves time out after three seconds and keep only a
bounded stderr diagnostic. A deliberate `ctl quit` is a clean exit and is not
restarted. Abnormal Rust exits and its ordinary runtime-failure status use the
unit's five-second retry, capped at five starts per minute; configuration
preflight failures stop once. The checked-in units use bare commands so they
remain useful outside Nix; the Nix package rewrites every executable to a store
path.
If you copy the three units manually from
`src/wall_in_one/data/systemd/`, make sure
`wall-in-one`, `wall-in-one-service` and GNU `timeout` are on the user manager's
`PATH`, then run `systemctl --user daemon-reload` before enabling it.

The health timer is `BindsTo=` the Rust unit, so an explicit stop, crash, or
failed start cannot leave Python polling in the background. Runs cannot overlap
and missed intervals are not replayed after login or suspend. Its normal
no-change stdout is discarded while errors remain in the journal. Stop also
makes one best-effort persistence attempt capped at two seconds; it cannot hold a
wedged authoring store open during shutdown. Direct non-systemd daemon launches
need a bar/shell caller for the hand-off; the matching companion revision adds
that call. The companion lives in its separate repository; this v0.1.2 app
follow-up neither changes nor silently republishes it.

If the resolved runtime document itself is missing, invalid, or still uses an
obsolete schema, the exact Rust preflight leaves the unit failed after one
attempt. Repair the authoring source and explicitly recover with
`systemctl --user reset-failed wall-in-one.service` followed by
`systemctl --user restart wall-in-one.service`. A damaged authoring file alone
does not stop an otherwise-valid schema-4 last-known-good runtime.

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

The window follows the same path as the data: **Browse -> Media/Pairings ->
Playlists -> Schedules -> Settings**. The bottom navigation switches between
five real pages:

- **Browse** searches and downloads from Wallhaven and MotionBGS without
  leaving the main window.
- **Media/Pairings** is the complete crafting library, independent of what is
  currently playing. Left-clicking a tile opens that item's full-size pairing
  editor; right-clicking plays it through the visible one-entry **Quick choice**
  playlist. Adaptive colours offer all ten Noctalia generators with real,
  lazily cached previews. Community and custom palettes show their stored
  colours. Built-ins remain honestly unpreviewed because Noctalia does not
  expose their colours without applying them.
- **Playlists** creates, renames and deletes ordered rotations. Add media from
  the searchable thumbnail pane by clicking or ordinary drag-and-drop. Within
  the playlist, drag a row's handle to reorder the live sortable list. The
  entry's stable id is not changed and the same pairing may appear more than
  once. Borked media stays visible for repair but cannot be added or dragged
  into another playback route; retained Borked or missing entries are labelled
  unavailable, skipped when safe media remains, and disable Play when the list
  has no usable item. Large rotations load 72 stable order rows at a time while
  the complete stored order remains available to playback.
- **Schedules** switches the active playlist, resumes calendar control, chooses
  the default playlist, assigns playlists to connectors, and edits months,
  weekdays and local-time windows with visual selectors. Independent mode also
  exposes each live display's transport, Stop, Cycle and Shuffle controls with
  their saved-default/manual provenance. Rules lower in the list have higher
  priority: the last matching rule wins. The editor loads rules 48 at a time,
  so the supported 512-rule ceiling remains usable rather than a widget burst.
- **Settings** keeps library roots, playback, providers, colour and appearance
  controls visible as part of the main workflow rather than another window.

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

The search button in the header -- or **Find wallpapers** in the menu, or
`Ctrl+B` -- opens the Browse tab, which searches [Wallhaven](https://wallhaven.cc) for
stills and [MotionBGS](https://motionbgs.com) for video wallpapers. Downloads
land under the explicitly chosen first library root, and the library is
rescanned when one finishes, so the file shows up in the grid without being
asked for. With no root configured, Browse refuses the download and points to
Settings instead of guessing a destination.

Browse uses explicit Previous/Next photo pages with at most 40 heavyweight GTK
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
settings writes rather than dropping it. The bundled template is copied once to
a content-addressed file under Wall-in-One's state directory, without replacing
an existing name. The settings block is fenced with markers, the previous file
is backed up first, and a hand-written entry under the same id is never
overwritten. `--uninstall-theme-template` removes the settings block again.

Check what you have with `--theme-status`, `--print-palette`, or `--print-css`.

**Palettes** in the menu browses what is installed: the ten built-ins,
community palettes Noctalia has cached, your own custom ones, and a fourth
group for the pre-5.x `colorschemes/` layout. That fourth group is listed with
Apply disabled and the reason stated -- Noctalia 5.0.0-beta.7 cannot apply one,
so offering the button would be a lie. The built-ins are listed without
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

The Settings tab writes `~/.config/wall-in-one/settings.toml`; it is also safe
to edit by hand. The interactive loader clamps bad values so the repair screen
can still open. The unattended `--write-config` compiler instead rejects an
invalid typed value and preserves the last-known-good runtime document. The
complete key, meaning and default table is in
[`docs/settings.md`](docs/settings.md).

## Upgrades and migration

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
