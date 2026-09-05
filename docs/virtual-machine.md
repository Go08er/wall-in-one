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
    path:/absolute/path/to/goober-noctalia-plugins-v5/noctalia_5 \
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

Release evidence uses the normal Git-backed commands above from a clean commit
whose complete intended path set is tracked. An explicit `path:.` input is
useful while developing untracked sources, but it can also copy ignored local
artifacts and is not publication proof. Record the tested commit and results
alongside a release. CI runs the VM for release branches, manual dispatches,
and its scheduled check; the ordinary PR job runs the separate fast checks.
The VM duration is diagnostic rather than a timing gate, and its embedded
package tests complement the separate packaged GUI and cross-binary socket
checks in the default flake suite.

The default checks also include a process-level memory contract for the Rust
runtime:

```console
$ nix build -L .#checks.x86_64-linux.service-rss
$ cat result/summary.txt
```

That check starts three fresh services for each one-/three-display shape and
power-policy case, from handwritten resolved configs representing a
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

The matrix covers the default disabled policy, an enabled policy with a missing
private bus, and enabled policies connected to a disposable UPower provider
reporting AC or battery. It validates the corresponding status fields before
and after idle measurement. The provider and bus run outside the measured
service; neither accesses the host's power service or desktop. Every run checks
resident thread counts (one disabled, two enabled) and bounded open descriptors.
`battery-summary.json` records missing-provider cases, and
`power-*-summary.json` records connected-provider cases.

A separate launch discovers the supported ceiling of 64 synthetic connectors,
routes commands to all of them, and validates the resulting atomic status
snapshot. That is a correctness stress, not a memory promise for a normal
desktop. `samples-kib.tsv`, `idle-cpu.tsv`, `summary.json`, the status snapshots,
per-process `status`/`smaps_rollup` snapshots, and service logs are retained in
the result for review. Peak observations also retain maps and available full
`smaps` alongside `process-counts.tsv`. `library-slope.tsv` records one-display
observations at
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
- corrupt authoring preserves a valid schema-4 last-known-good runtime, while
  the same state with a public schema-2 document fails once in the exact Rust
  preflight without starting the Rust main process, socket, or renderer; the
  test holds its invocation/restart counters stable across two retry windows
  and then proves recovery after restoring schema 4;
- Store, Library, Playlists, Schedules, and Settings open in the
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
  persists that finding as playback-failure metadata and recompiles the clean runtime
  document. The fixture deliberately leaves its injected renderer failure in
  place until stop: rewriting the runtime document immediately beforehand
  would wake Rust's file watcher and test a reload race instead of the stop
  persistence boundary. The final run logged `saved 1 new wallpaper health
  marker`, completed the stop in about 0.75 seconds and found Pairings durable
  immediately afterward;
- the health timer becomes inactive on an explicit daemon stop and immediately
  after `SIGKILL`, then returns with the daemon under the unit's selective
  abnormal/runtime-failure restart policy; its oneshot treats an absent runtime
  as a successful no-op;
- installed executable links, loaded unit commands (including both preflights
  and the stop-time health writer), and `/proc/PID/exe` identify the current
  package. Reloading unit definitions alone leaves the running instance intact;
  an explicit stop, validation and start produces a new instance while
  preserving settings, authored stores and source-media bytes. This checks
  the current package's manual lifecycle, not compatibility between releases
  or draining pending GUI work; and
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

Older dated VM runs used companion revisions `a5e23c9` and `a17eb70`; those
results do not establish the newer startup or battery-display behavior.
Application v0.1.3 and its VM pin companion v0.1.2 at
`c154c162fd650567ef8eda5d0e6d875b2b635a21`, including those changes.
Machines moving from an older companion still
complete the compatible app's schema-4 cutover before loading the strict
status-v2 client. The contract and old-revision limits are documented in
[`migrating.md`](migrating.md#companion-noctalia-plugin-compatibility).

The RSS check's 64-connector stress exercises bounded per-connector runtime state,
targeted socket routing, status serialization, and compositor hotplug input.
They are synthetic names returned by a test double, not 64 virtual monitors.
Consequently that check is useful for the memory ceiling and headless routing
contract, but it does not weaken the desktop VM's honest one-output limitation
or prove compositor behavior across physical displays.
