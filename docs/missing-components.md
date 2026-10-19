# Missing components: what the app checks, and what plays meanwhile

From 0.4.0 Wall-in-One brings neither wallpaper renderer
([packaging](packaging.md#the-renderers-are-external)), and it never installed
Noctalia, the companion plugin or a user unit for its service on a system it
does not own. So it has to find out what is missing, say so, and keep the
desktop sensible until it is there. Two halves:

- **The service** tells a renderer that is not there from a wallpaper that is
  broken, so a missing program is never recorded against a wallpaper, and
  reports which program it could not start.
- **The app** checks for each component itself, says what is missing and how to
  get it for this kind of install, in the window, in Settings and from a
  terminal, and offers **Check again**.

## What can be missing

| Component | Required? | How the app knows |
|---|---|---|
| mpvpaper, linux-wallpaperengine | one of the two is required; each alone is optional | `programs.find_program(name).found`, and the service's `missing_renderers` |
| Noctalia | required | `programs.find_program("noctalia").found` |
| The wallpaper service | required: running, or at least installed | a `status` reply; else whether `wall-in-one-service` and its user unit are installed |
| The companion plugin | optional | a read-only look in Noctalia's plugin folders |

Required means the wallpaper cannot be managed at all without it: no service
running, no renderer of either kind, or no Noctalia (which sets every still and
palette). The window shows a notice for those. One renderer missing (its kind
shows stills) and the companion missing while the service runs anyway are
optional: Settings lists them, the window does not interrupt for them.

## The service without a renderer

### Missing is not broken

Starting a renderer can fail in two ways that look alike in a log:

- **The program itself cannot be started.** The spawn of the path
  `runtime.toml` names fails with `NotFound` (nothing there, or a script whose
  interpreter is missing) or `PermissionDenied` (a file that is not
  executable, or a mount that forbids it). No child ever existed.
- **A child started and then failed.** mpvpaper or linux-wallpaperengine ran
  and exited: a codec it lacks, a scene it cannot render, a crash.

Until now both were the same `Err` from the driver. An automatic apply (start-up,
rotation, a schedule change, a display plugged in) retried it twice, then marked
the entry taboo for the session, and the app's health sync saved that on the
wallpaper. A missing renderer therefore skipped every video or scene in turn,
and the skip outlived the install. That was the default outcome of an update
from 0.3 without the renderers.

Now only the second is a failure. The first is recorded on the driver as a
missing renderer, and the apply that met it succeeds. Nothing else changes for a
child that exits: `poll_failures` reports it as before (a video stays retryable,
a scene is taboo for the session), and any other spawn error (out of memory, too
many processes) is still a failure.

### What the display shows meanwhile

**The entry's own still**, in both display modes. The still and the palette are
applied before motion is started in every apply path (mirrored `apply`:
still, palette, motion; independent: every still, then the theme source's
palette, then motion), so when the spawn finds no program the still is already
on screen. The cursor stays on the entry, and rotation carries on at its
interval as for any still.

Two existing behaviours show a still in place of motion, and this follows the
closer one:

- **Battery and still-only playback** leave motion off by policy. Nothing is
  wrong, so the route is healthy: `renderer_failed` false, no `last_error`.
- **An entry that is taboo** shows its still because something is wrong
  (`taboo_fallback_diagnostic`): `renderer_failed` true, and `last_error` says
  why.

A missing renderer is a problem, and one the user can fix, so it is reported
like the second: the display's `renderer_failed` is true and its `last_error`
names the program, the path and the error. That is what every consumer of
`renderer_failed` already reads as "Play retries": the classic and new player
bars offer Retry, and `play` (mirrored) and `independent_play` re-apply a failed
route from any state. After installing the program, Play starts it. The
companion already shows `last_error` and a renderer-failed banner, so the bar
says what is missing with no change to the companion.

Skipping to the next entry the renderer can play was the alternative. It changes
what the user chose, it cannot help a playlist of videos only (there is nothing
to skip to), and in independent mode it would move one display's cursor because
of a program that every display shares. The still is the wallpaper the user
picked, minus its motion.

### What is reported

- **No taboo, no per-entry report, no durable record.** The entry never reaches
  `ApplyFailure`, so no retry is scheduled (`automatic_retry` stays null) and
  `taboo_entries` stays empty. That matters beyond the taboo itself: the
  companion runs `wall-in-one --sync-runtime-health` whenever `taboo_entries`
  holds a session-only report, so a missing renderer reported there would start
  the health sync again every retry interval, for as long as the renderer is
  missing.
- **A new top-level `missing_renderers`**, an array in a fixed order (mpvpaper,
  then linux-wallpaperengine), each `{"program": "mpvpaper", "path":
  "/usr/bin/mpvpaper"}`. Empty means none. It is additive: `status_version`
  stays 2, and a client that does not know it is unaffected. `path` is the path
  the document names, cut in the middle with the usual `[truncated]` marker
  past 1024 bytes, so the key is at most about 8 KiB of the reply whatever the
  document holds: a fixed part of the status, inside the 256 KiB both status
  budgets reserve for structure (`STATUS_STRUCTURAL_RESERVE`), as for
  `install_kind`, and neither budget changes. The configured program paths are
  not counted by either budget, so counting them there would refuse a document
  near the limit that loads today.

### When it clears

- **A spawn of that program succeeds**, on any display: the program is there.
- **A reload whose renderer settings changed** (a new path for a program, as
  when a Nix profile now has it): the driver is reconfigured, which forgets
  every record, and every display is applied again.
- **A reload whose renderer settings did not change**, when a recorded
  program's path is now an executable file: that record is forgotten and every
  display it left on a still is applied again. This is the Arch case: the
  document named the placeholder `/usr/bin/mpvpaper`, the package now put the
  program there, and the recompiled document has the same bytes. A program that
  is still missing stays reported; forgetting it would say "nothing missing"
  about a renderer that is not there.
- **Play** on a display that shows a still for it, which starts the renderer
  again (above).

A display whose route moves to a still, or that is stopped or unplugged, drops
its own mark; the top-level record stays until one of the above, because the
program is still not there.

## The app's checker

`wall_in_one.setup_check` has a pure half and an observing half:

- `findings(install_kind, local, service)` turns what was observed into
  findings, with no I/O. Each finding says what is missing, whether it is
  required, what that means, and how to fix it for this install kind. All the
  text the window, Settings and the terminal show comes from here, so the
  classic and the new interface say the same thing (as
  `programs.SCENE_ENGINE_MISSING` does for the scene-engine row).
- `local()` looks at this machine: the four programs through `find_program`,
  the user unit, the companion. `service_from_status(...)` reads a status reply.
  Neither starts a program, talks to systemd or D-Bus, or writes anything.

### The service

A `status` reply means it runs. With none, the app looks for what would start
it: `wall-in-one-service` (`find_program`) and `wall-in-one.service` in the
user unit search path (systemd.unit(5): `$XDG_CONFIG_HOME/systemd/user`,
`$XDG_CONFIG_DIRS`, `/etc/systemd/user`, `$XDG_RUNTIME_DIR/systemd/user`,
`/run/systemd/user`, `$XDG_DATA_HOME/systemd/user`, each `$XDG_DATA_DIRS`
entry, `/usr/local/lib/systemd/user`, `/usr/lib/systemd/user`), from this
process's environment, which on NixOS is built from the same profiles as the
user manager's. It never asks `systemctl`. A unit that is there gets "enable
it, or let the companion start it"; no unit but the program gets "the companion
starts it directly"; neither is "not installed", with that install kind's fix.
The window reads the service's state from the status it already polls, so it
says nothing until the first answer and follows a service the companion starts
later.

### The companion plugin

Noctalia 5 keeps plugins in two places it manages, from its own directory rules
(`NOCTALIA_STATE_HOME` or `NOCTALIA_DATA_HOME`, then the XDG variable, then the
XDG default, each followed by `noctalia`):

- `<data>/noctalia/plugins/<dir>/plugin.toml`: the implicit local source;
- `<state>/noctalia/plugins/materialized/<source>/<dir>/plugin.toml`: what a
  git source exports when a plugin is enabled (the clone itself is under
  `plugins/sources`). This is how the companion is installed:
  `noctalia msg plugins source add <name> git <url>`, then
  `noctalia msg plugins enable goober/wall-in-one`, as its VM tests do.

The check reads each `plugin.toml` there (at most 64 KiB, at most 4096 of
them) and matches its `id`, `goober/wall-in-one`, not the directory name. It
only reads. It answers:

- **installed**: a manifest with that id is in one of those places. Whether it
  is enabled is not claimed: that is in Noctalia's merged configuration (every
  `*.toml` in its config directory with its includes, then `settings.toml`),
  which the app does not reproduce.
- **not found**: every place could be listed and none has it. A third kind of
  source, a path source, is a folder only Noctalia's configuration names, so
  the finding says "not found where Noctalia installs plugins" rather than "not
  installed".
- **unknown**: no usable home or override, a place that exists but could not be
  listed, or more manifests than the bound. Said as such, never as missing.

The link is one constant, `setup_check.COMPANION_URL`
(`https://github.com/Go08er/goober-noctalia-plugins-v5`, the plugin is its
`wall-in-one` directory).

### Fixes per install kind

| | Renderers | Noctalia | The service |
|---|---|---|---|
| `nix` | the `programs.wall-in-one` module with `manualWallpaperInstalls` off, or `nix profile install nixpkgs#mpvpaper` / `nixpkgs#linux-wallpaperengine` | its flake, `github:noctalia-dev/noctalia` | the package brings it: the module, or `nix profile install github:Go08er/wall-in-one` |
| `system` | the AUR's `mpvpaper` and `linux-wallpaperengine-git` | the `noctalia` package | reinstall the `wall-in-one` package |
| `source` | your distribution's package, or the program in `~/.local/bin`, `/usr/local/bin` or `/usr/bin` | Noctalia 5 or later from its project | build and install `wall-in-one-service` from `service/` |

## Where it is said

- **The window**, both interfaces: a card above every page, the same widget in
  both (as the Tidy up card is), shown only while something required is
  missing. It has **Check again**, **Details** (Settings at this section) and
  **Not now**. Not now hides the findings it showed for the rest of the
  session; a finding that changes (the service goes from not running to not
  installed, another renderer goes missing) shows again. Nothing is written:
  the dismissal lives in the running app.
- **Settings → Setup**, both interfaces, one shared group: every finding,
  required and optional, with **Check again**.
- **`wall-in-one --setup-status`**: the same findings as text, for a terminal
  or a headless check. It exits 1 when something required is missing, else 0,
  as `--legacy-migration-status` does, and imports no GTK. Unlike the window it
  asks the service once, so a service that does not answer in time is a
  required finding there.

## Check again

On Nix, installing a renderer changes the path the compiler writes (a profile's
`bin`); on Arch, the placeholder `/usr/bin/<name>` becomes the program. Either
way the service has to load a document and try again. So Check again:

1. compiles `runtime.toml` on the window's own publication path, the one every
   save uses (the same compile `--write-config` runs), when authoring is ready;
2. asks the service to `reload` on the same ordered lane, whether or not the
   compile changed a byte (in the Arch case it does not, and only a reload
   makes the service look again, above);
3. checks again.

No new writer: the compile is the existing one, and a reload of an unchanged
document changes nothing else.

## What older versions left behind

- **Taboos saved for a missing renderer.** 0.1.4 to 0.3.x bundled the renderers
  and named them by store path, so in normal use they could not miss one. A
  source install without them, or an unreleased 0.4 build before this change,
  could save a skip whose reason is `cannot start mpvpaper: No such file or
  directory` on a wallpaper. Those markers stay. The app cannot prove from a
  saved reason that the program was the only problem, and clearing a marker
  is the user's choice: **Try again** in the wallpaper's details clears it, as
  docs/updating.md says.
- **A 0.3 service** has no `missing_renderers`; the app then relies on its own
  lookup.

## How it is tested

- The service (`service/tests/service.rs`), with the real driver and a missing
  or non-executable program path, in both modes: the apply succeeds, the still
  is set, no retry, no taboo, `missing_renderers` names it; the same programs
  failed and were quarantined before. Recovery by Play and by reload once the
  program is there; a child that exits is still reported and a crashed scene
  still quarantined. The status-shape fixture ignores the new key, and the
  ceiling document's status still fits with both renderers missing at the
  longest paths.
- The checker's pure half with built observations (`tests/test_setup_check.py`);
  `local()` with a fake home, fake Noctalia folders, overrides and units.
- `--setup-status`'s output and exit status.
- The card and the section in both interfaces, through the GUI runner.
