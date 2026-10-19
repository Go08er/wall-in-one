# Where display information comes from

Wall-in-One runs on any Wayland compositor that Noctalia 5 or later supports.
Nothing asks one compositor's own IPC; every process learns about displays
from standard Wayland sources, and there is one source of truth: the
wallpaper service.

## The service (`wall-in-one-service`)

The service is a long-lived Wayland client. It connects to `WAYLAND_DISPLAY`
(an absolute path, or a name under `XDG_RUNTIME_DIR`), which it already needs
for its renderers. `WAYLAND_SOCKET` is ignored: it hands over one connection
once, and the service has to be able to reconnect. The connection is opened
the first time displays are needed and reopened after the compositor goes
away.

It reads:

| Protocol | What it gives |
|---|---|
| `wl_output` (version 4, or older with the name from xdg-output) | connector name, current mode (size and refresh in mHz), integer scale, physical size in mm, make and model |
| `zxdg_output_manager_v1` | logical position and logical size |
| `zwlr_output_manager_v1`, when the compositor offers it | disabled heads, fractional scale, make and model |

The displays a wallpaper can be put on are the enabled outputs: the
`wl_output` globals with a usable connector name. A compositor advertises no
`wl_output` for a disabled head, so disabled heads come only from
wlr-output-management (niri 26.04, Sway, Hyprland and the wlroots compositors
offer it). They are listed with `enabled: false` and never become a renderer target.
A compositor without wlr-output-management has no disabled heads to list, and
nothing else changes. Nor is a compositor followed that announces more heads
or modes over it than the service keeps (256 heads, 4096 modes): the service
drops that connection and goes on without wlr-output-management, listing no
switched-off heads, until the compositor or the service restarts, and says so
once in its journal. The compositor counts as restarted once it closes the
connection, its socket refuses connections or is gone, or another socket is
in its place; a question it merely leaves unanswered does not bring the
protocol back. A compositor that keeps announcing heads or modes anew over
one connection, finished heads and their modes included, gets a fresh
connection past twice those numbers, with wlr-output-management still in use.
Events the compositor sends to objects the service has already released are
dropped by the Wayland library before the service sees them, so they are not
counted; a compositor can make them cost memory, which the service accepts:
it takes a hostile compositor, and that one owns the screen anyway.

The service asks once per wallpaper hand-over and every five seconds, as it
asked niri before: one round trip that also takes in any outputs added or
removed since the last one. Each question, connecting included, ends within
three seconds, as the niri helper did, and a shutdown ends it at once. A
compositor that has stopped accepting or answering therefore holds the
service for at most three seconds per question, and is not asked again until
the next one. A service that starts before
the compositor finds no socket; it reports that in `output_discovery_error`,
keeps the explicit display assignments, retries the initial apply inside the
startup window, and connects on a later probe.

It publishes the last successful snapshot in `status` as a new top-level
`outputs` array (see [control-socket.md](control-socket.md#outputs)). The
fields the companion reads (`displays`, `displays[].connected`,
`output_discovery_error`) keep their shape, and the wording of a discovery
error changes. One meaning changes: a display switched off in the compositor
is `connected: false` and is treated exactly like an unplugged one (its
renderer stops; switched back on, its assignment is applied again), where
niri's answer, and so 0.3.x, counted it as connected.

## The window (GTK)

When something in the window needs display sizes or names (`ctl displays`,
sizing a scene still), the worker that does the job asks the service for its
snapshot. When no service answers, or it has no snapshot yet, the job uses
GTK's monitors (`Gdk.Display.get_monitors()`: connector, geometry, scale,
refresh rate, manufacturer and model), read on the main thread before the job
is handed off, because GTK may only be touched there. GTK lists enabled
outputs only.

The Displays, Schedule and Settings pages already merge GTK's monitor list
with the service's `displays[].connected`; they are unchanged.

## Headless Python paths

`--service` (the legacy Python service), `--write-config`,
`--sync-runtime-health` and `ctl` have no window. Of these only the legacy
service needs displays: its renderer for the refresh rate of video
interpolation and the connectors of a scene on all outputs, and its still
maker for a scene still's size. They ask the service like everything else. In
legacy mode no service is running, so the renderer gets nothing, which is what
happened before when niri could not be asked: interpolation stays off, and an
all-output scene is refused rather than opened as a preview window. (The still
maker uses GTK's monitors when the process happens to have a display, as the
window does, and otherwise takes no scene still.) The other paths never needed
display information; `ctl displays` is answered by the window or the legacy
service, not by `ctl` itself.

## `runtime.toml`

`[renderer]` no longer has `niri_program`. The compiler stops writing it, and
the service accepts and ignores it, so a document written by 0.3.x loads.

The schema version stays 4 (5 with the battery option). Updates are one-way: a
newer build must read what older builds wrote, and it does. The other
direction, a 0.4 document reaching a 0.3 service that has not been restarted
yet, fails closed: the old service's strict loader refuses the reload (a
missing field) and keeps its last good configuration, which is what any failed
reload does. Schema 5 needed its own number because a schema-4 service would
otherwise have applied a document without the battery policy it asked for;
here nothing can be applied wrongly. A bump would also have sent every save
through the live-service capability check that schema 5 uses, refusing every
save until the service restarted.

## The renderer layer

mpvpaper and linux-wallpaperengine both draw through wlr-layer-shell, and both
are started with `--layer background`. The background layer is the one the
protocol sets aside for wallpapers, below the `bottom` layer that panels and
desktop widgets use, and every compositor Noctalia supports implements
layer-shell, because Noctalia's own surfaces are layer-shell surfaces. niri's
`place-within-backdrop` rule is an optional niri example in
[compositor-window-rules.md](compositor-window-rules.md).

## How it is tested

- The service's discovery, over the wire, against a fake compositor in its own
  tests (`service/tests/support/compositor.rs`): with and without
  wlr-output-management and xdg-output, `wl_output` before version 4, hot-plug
  in and out, switched-off heads, a compositor that starts late, restarts or
  never answers, and the 64-output ceiling.
- `service-rss` runs the service against a fake compositor with 1, 3 and 64
  outputs (`nix/fake-wayland-outputs.py`), so its memory and idle CPU include
  the Wayland connection.
- The window's fallback against the test run's own headless Weston
  (`tests/test_ui_monitors.py`), and the Python side's choice of source with
  fakes (`tests/test_outputs.py`, `tests/test_ui_stills.py`).
- The niri VM test checks that the service published the VM's real output.
  Nothing here depends on niri, so a second compositor in the VM tests needs
  no change to the code, only a VM.
