# Development VM

Wall-in-One ships two NixOS VM fixtures. They are isolated from the host and
answer different questions:

- the development VM is a real desktop a person can explore;
- the automated VM test exercises the service and captures every workflow
  page without opening anything in the host session.

Both use the flake's locked niri and Noctalia packages, the Wall-in-One package
built from the checkout, and the companion plugin fetched by the locked
`noctalia-plugins` input. Neither mounts the host home directory.

## Launch the desktop

From the repository root:

```console
$ nix run .#vm
```

The guest account is `wallpaper`, with password `wall-in-one`. The graphical
session starts automatically. Wall-in-One's user service starts at login,
Noctalia loads the companion plugin from its immutable Nix-store path source,
and the app's library contains three generated stills and a four-second
generated video on first boot. No file from the host wallpaper collection is
copied or mounted.

The flake input normally uses the published companion-plugin repository. To
test an uncommitted local plugin checkout whose `wall-in-one/` directory is
under `noctalia_5/`, override just that input:

```console
$ nix run \
    --override-input noctalia-plugins \
    path:/home/goober/Documents/goober-noctalia-plugins-v5/noctalia_5 \
    .#vm
```

The VM allocates four virtual CPUs, 6 GiB of memory, and a 16 GiB virtual disk.
It uses QEMU's virtio display with software rendering. Niri deliberately
rejects software EGL on its direct TTY backend, so a minimal Cage session owns
the virtual framebuffer and runs the real niri compositor as its sole,
full-screen nested client. Noctalia, Wall-in-One, and the test controls all use
niri's nested Wayland and IPC sockets.

## Run the automated desktop test

The VM test is part of the default flake checks:

```console
$ nix flake check -L
```

It can also be built on its own:

```console
$ nix build -L .#checks.x86_64-linux.vm-test
```

The final 2026-08-24 release-tree run used the explicit working-tree form,
because the migration sources were intentionally still untracked:

```console
$ nix build -L 'path:.#checks.x86_64-linux.vm-test'
```

It passed. The package check embedded in that build reported 1,745 passed,
1 intentional skip and 271 GUI tests deselected; the booted-desktop script
completed in 88.07 seconds. The duration records that run, not a timing gate.
The separate packaged GUI check reported 271 passed and 1,746 non-GUI tests
deselected; the cross-binary runtime-socket fallback check passed both tests.
The `path:.` source included the required untracked migration sources, but it
also copied ignored local artifacts into its temporary source snapshot,
including `.claude`, tool caches and `service/target` (about 651 MiB total,
roughly 601 MiB of it the Rust target tree). Package-specific filesets and the
installed 8.5 MiB output excluded those artifacts, so the functional result is
valid; it was not a clean publication-source proof. Before any later
publication, stage the intended sources and rerun the normal Git-backed flake.
This release run did not stage or publish them.

The default checks also include a process-level memory contract for the Rust
runtime:

```console
$ nix build -L .#checks.x86_64-linux.service-rss
$ cat result/summary.txt
```

That check starts three fresh one-display services and three fresh
three-display services from handwritten resolved configs representing a
600-item library (900 resolved occurrences across All media and three authored
playlists), warms every connector through the public runtime socket, and
samples each service's own `VmRSS` 20 times. A one-display run must stay at or
below 5 MiB; a three-display run must stay at or below 10 MiB. Each run also
records the service's CPU-tick delta over ten idle seconds after warm-up. That
window spans two five-second compositor polls, and idle consumption must remain
at or below 2% of one CPU core. The reported millipercent value is
`cpu_ticks * 100000000 / (CLK_TCK * elapsed_ms)`. Elapsed nanoseconds are
rounded down to milliseconds, which conservatively overstates CPU use, and the
hard comparison uses the unrounded numerator and denominator so quotient
truncation cannot hide a value just over the limit.

A separate launch discovers the supported ceiling of 64 synthetic connectors,
routes commands to all of them, and validates the resulting atomic status
snapshot. That is a correctness stress, not a memory promise for a normal
desktop. `samples-kib.tsv`, `idle-cpu.tsv`, `summary.json`, the status snapshots,
per-process `status`/`smaps_rollup` snapshots, and service logs are retained in
the result for review. `library-slope.tsv` records one-display observations at
64, 300, and 600 library items to make allocator growth diagnosable; those
smaller shapes do not replace the 600-item gate. The Python app and GUI are
never started; a small isolated Python client only speaks the public socket and
validates the returned JSON, including all four playlist entry counts.

The 2026-08-24 release-package run with that 600-item fixture produced
one-display peaks of `4900, 4900, 4900 KiB` (4.785 MiB) and three-display peaks
of `4824, 4824, 4824 KiB` (4.711 MiB). The maximum includes observations before
any client connects, after the first atomic status response, after the routed
control warm-up, and after the idle window; the contract therefore does not
depend on the plugin running. Ten-second idle windows used at most 0.199% of
one CPU for one display and 0.299% for three. Optimizing the always-resident
release for size reduced the stripped service binary from `1,612,184` to
`1,396,504` bytes (13.4%) without an observed idle-CPU regression. These
numbers document that exact build rather than replacing the gates; CI repeats
the measurement because libc, allocator, and toolchain updates can change RSS.

The result contains PNG screenshots named `wall-in-one-browse.png`,
`wall-in-one-media.png`, `wall-in-one-playlists.png`,
`wall-in-one-schedules.png`, and `wall-in-one-settings.png`. The test also
verifies that:

- niri exposes a visible output and Noctalia loads the companion plugin;
- the packaged headless Python compiler upgrades seeded authoring state and an
  obsolete schema-1 runtime document before `wall-in-one-service` starts; the
  Rust service then owns a responsive runtime socket;
- Browse, Media/Pairings, Playlists, Schedules, and Settings open in the
  running app; each capture first verifies the page-specific compositor title,
  and its comparison image excludes Noctalia's clock-bearing panel so a stuck
  app cannot pass merely because the clock changed;
- closing the GUI leaves the same service process alive and rotation advances;
- an isolated Noctalia probe starts recording before the GUI launches, checks
  every `wallpaper-set` across its open/close lifecycle has the Rust service as
  its parent, and observes exactly one new application for the deliberately
  released timer deadline; this proves the GUI did not start a second Python
  wallpaper driver;
- a control-socket playlist switch changes the active media;
- a generated video launches one instrumented mpvpaper child with the compiled
  hardware-decode and interpolation options; Pause freezes it, Stop releases
  it, and Play creates exactly one new child before handing back to a still;
- a schedule timer observes an injected local-time boundary and applies its
  playlist;
- an automatic video transition which cannot start its renderer is attempted
  three times, appears first as an exact non-durable `automatic-apply` health
  finding, and is not written to Pairings by either a GUI poll or the periodic
  health timer;
- the packaged unit's bounded, best-effort two-second `ExecStop` bridge then
  persists that finding as Borked metadata and recompiles the clean runtime
  document. The fixture deliberately leaves its injected renderer failure in
  place until stop: rewriting the runtime document immediately beforehand
  would wake Rust's file watcher and test a reload race instead of the stop
  persistence boundary. The final run logged `saved 1 new wallpaper health
  marker`, completed the stop in about 0.75 seconds and found Pairings durable
  immediately afterward;
- the health timer becomes inactive on an explicit daemon stop and immediately
  after `SIGKILL`, restarts with the daemon under `Restart=on-failure`, and its
  oneshot treats an absent runtime as a successful no-op; and
- the completed desktop session has no matching coredump, GTK/GLib critical,
  Python traceback or Rust thread panic.

The guest clock is changed only inside the disposable VM. All guest config,
state, cache, media, and runtime sockets live under the guest's own home or
`/run/user/1000`; the test never reads the host's Noctalia configuration,
library, Steam directory, or home directory.

## Honest limits

This fixture has one virtual output and software rendering. Steam is not
installed, no Workshop content is copied into it, and no Wallpaper Engine scene
is available to render. The app therefore reports an empty scene library and
degrades normally. The VM proves the application/service lifecycle, still
rotation, video discovery in the library, Noctalia/plugin integration,
schedule control, the five GTK tabs, and mpvpaper's process/argument lifecycle
through an instrumented renderer substitute. It does **not** prove actual video
decoding or GPU rendering, Wallpaper Engine scene playback, Steam integration,
or physical multi-monitor behavior. Independent routing, schedules, cursors,
renderer ownership and transport are supported in software and covered by
automated unit/process tests, including synthetic connector stress. Neither the
development machine nor this VM exposes two outputs, so different playlists on
two physical monitors remain unverified.

That dated VM run deliberately used the then-locked preceding companion
revision, `a5e23c9`. It proves that revision loaded and drove the basic
integration; it is not evidence for the coordinated release pair. The current
tree instead pins reviewed companion candidate
`a17eb70f653afb4cf5c04afc912cdca8b14ac06e`. Its final normal Git-backed VM and
flake results must be recorded here after the release tree is clean. At
publication the app is promoted before immediate promotion and tagging of
companion `0.1.1`, so the strict status-v2 client is never served to users of
the older app. The contract and old-revision limits are documented in
[`migrating.md`](migrating.md#companion-noctalia-plugin-compatibility).

The RSS check's 64-connector stress exercises bounded per-connector runtime state,
targeted socket routing, status serialization, and compositor hotplug input.
They are synthetic names returned by a test double, not 64 virtual monitors.
Consequently that check is useful for the memory ceiling and headless routing
contract, but it does not weaken the desktop VM's honest one-output limitation
or prove compositor behavior across physical displays.
