# Decisions

Rulings taken during development, why, and what was rejected. `DESIGN.md` is
the record of what was *measured*; this is the record of what was *decided*.

A rejected option with no reason attached gets proposed again three weeks
later, so the reasons are here even when they are obvious today.

---

## The shape of the system

```
data  <-  app  ->  conf  ->  service  <-  plugin
```

Four parts, and the arrows are the whole design.

| part | language | owns |
|---|---|---|
| **app** | Python + GTK4 | the data. Library, stores, pairings, playlist authoring, schedules, still generation, browsing and downloading, thumbnails, palette browser. All the heavy lifting. |
| **conf** | a file | the app's output and the service's only input. Fully resolved. |
| **service** | Rust | runtime only. Timers, applying wallpapers, supervising renderers. Small, always on. |
| **plugin** | Luau | a way in. Opens the app; sends the service runtime commands. |

The user drew this out and then revised it, and the revision is the design.
The second version adds a legend that does real work: **orange is file
communication, green is startup, blue is IPC** — which separates who *starts*
whom from who *talks* to whom.

    startup   plugin --> app,  plugin --> service (at Noctalia start)
    file      data <--> app,  app --> conf --> service
    ipc       plugin --> service,  app --> service,
              service --> Noctalia | linux-wallpaperengine | mpvpaper,
              Noctalia --> app (the palette comes back)

Both gaps in the first drawing are closed. The plugin sends IPC rather than
editing the conf — no orange arrow leaves the plugin, so it never writes
configuration. And the app has its own IPC channel, so it can issue runtime
commands and read back what is actually playing rather than only what it last
wrote.

The IPC arrows are drawn one-way and that is correct: a request/response socket
carries `status` back on the same channel, so no return arrow is needed.

The palette arrow really does run backwards, and it is easy to misread as a
mistake. The service sets the still, Noctalia derives the colours from it, and
the app reads those colours to theme its own interface. It is a genuine cycle.

**`conf` is a node in the chain, not a hand-wave.** It is the only thing the
service reads from the app: no shared database, no shared store files, no
imported code, no second reader of the library. The service must run correctly
from a conf alone with the Python app not installed — which is also the
cleanest way to test it.

### What the service is not allowed to know

Pairing resolution, `medium:source` identity, the library scan, provider
stores, still generation, thumbnails, palettes as files, ownership and removal
rules. The app resolves all of it away before writing the conf. Keeping this
list short is what keeps the service small.

### Conf carries configuration, socket carries runtime

Neither crosses.

- **Conf**, written only by the app: playlists with fully resolved entries,
  pairing decisions, schedule rules, display assignments, renderer settings.
- **Socket**, spoken by the app and the plugin: play/pause/stop/toggle,
  next/previous/random, shuffle, cycle, `playlist-use` as a manual override,
  `schedule-follow` to drop it, `status`, `reload`, `quit`.

**Rejected: conf-only, with the plugin editing basic settings in the file.**
It looks simpler and is not. `next` and `random` are events rather than state,
so a file forces a counter the reader has to diff. Two writers to one file
means merge rules or clobbering. The bar needs to *read* status, so the service
would have to publish a status file as well. That is three files plus inotify
plus atomic writes from Luau, against roughly two hundred lines of line
protocol that the plugin already speaks through `wall-in-one ctl`.

### The service is bundled, and refuses to run unattended

It ships in the app's repository and Nix package; it is not a separate product.
"App-managed" is enforced as *the conf carries a schema version which the
service validates*, rather than the service sniffing for an installed app —
same protection, still testable.

### Schema 2 is strict TOML

The resolved runtime document is TOML, documented in
`docs/runtime-config.md`. TOML makes the standalone contract test genuinely
standalone: a person can read and hand-write it without the Python app or a
serializer, while both sides have bounded parsers. Schema 2 rejects unknown
fields as well as unknown versions, so mismatched app and service builds fail
out loud instead of silently ignoring a renderer setting.

All executable and media paths are absolute. The service performs no PATH
lookup, glob, library lookup or provider lookup. The app writes through a
same-directory temporary file, fsync and rename. A bad reload keeps the last
valid document in force; the runtime socket also exposes an explicit `reload`
verb. This preserves the one-way app-to-service configuration boundary.

### Lifetime: session-scoped, not a system daemon

Started by the Noctalia plugin, owned by the shell session, exits with it. The
plugin prefers `wall-in-one.service`, then the Rust executable, and retains
`wall-in-one --service` only for installations that predate the Rust runtime.
The packaged unit starts `wall-in-one-service`, and as of the plugin's
`fdaf4d1` the non-systemd path prefers it too: it derives the runtime as a
sibling of the configured application binary before searching `PATH`, because
an override names the application precisely to bypass `PATH`. The Python
`--service` command stays as the fallback, so an installation predating the
Rust runtime still starts rather than refusing to.

The systemd unit adds `--wait-for-config`: a first installation quietly polls
for the app's initial atomic document instead of failing into a five-second
journal loop. This mode waits only for absence. A present but invalid document
is still a hard error and the service's schema/version refusal remains visible.

The unit also runs `wall-in-one --write-config` as `ExecStartPre`. Schema
upgrades must not depend on a human opening GTK: the headless command loads the
app-owned settings, library and authoring stores, compiles the current contract
atomically, and exits before Rust starts. This preserves both invariants: the
Python app remains the only configuration writer, and the service continues to
refuse unknown bytes rather than attempting migrations itself.

An ordinary Python GUI owns no cycle or schedule timer. Only the explicit
legacy `--service` process may create those compatibility timers, and even it
defers whenever the runtime socket has an owner. A timeout or malformed status
reply is not treated as proof that Rust is absent; otherwise a temporarily busy
runtime would grant Python permission to double-drive the wallpaper.

The Python `--service` compatibility mode retires only after the companion
plugin no longer invokes it **and** a release containing that plugin migration
has shipped. Removing it before both conditions would strand installations
whose app package predates the Rust binary. Until then it remains explicitly
legacy and is not the packaged systemd lifetime.

Generated configuration is compared as complete bytes: an unchanged document
is not renamed into place. A changed document is reloaded explicitly, and that
request advances the watcher's known fingerprint so one edit produces one
renderer hand-over rather than an immediate reload plus a second watched
reload. The watcher remains for recovery when socket delivery fails.

Runtime discovery is one atomic `status` snapshot, not a family of listing
verbs. It includes every playlist, every schedule rule and the last matching
rule, plus configured and effective display assignments. A bar client can
therefore render a coherent menu while the Python app is closed, without
mixing generations from several round trips. Display assignment remains
configuration and is never writable through the runtime socket. Runtime
replies have a separate 1 MiB ceiling so the documented 512-playlist and
512-rule maxima fit; requests and Python authoring replies retain 64 KiB.

---

## Why the service is being rewritten in Rust

Measured RSS of the windowless Python service on the development machine:

| process | RSS |
|---|---|
| bare Python | 14.0 MB |
| + GLib | 27.9 MB |
| + GTK4 + libadwaita | 55.8 MB |
| **the service as it was** | **71.1 MB** |

31 MB of that is a GUI toolkit loaded into a process with no window. CPU was
irrelevant in the measurement — both implementations spend nearly all their
time sleeping. This is a memory argument only.
Target: single-digit MB.

The packaged release service, running by itself from the handwritten schema-2
fixture, measured **about 2.8 MB RSS** (repeat probes: `2784–2800 kB`; virtual
size: `3764–3768 kB`) on the same development machine. That is about 4% of the
former 71.1 MB process. The measurement deliberately did not start or import
the Python application.

### All-output Wallpaper Engine scenes query niri at apply time

`linux-wallpaperengine` treats a positional scene id as window-preview mode;
desktop rendering always requires one or more `--screen-root <connector> --bg
<scene>` pairs. A blank output in the resolved runtime configuration means all
currently attached displays, so the Rust service asks niri for its current JSON
output map immediately before launching an all-output scene and emits one pair
per connector. The app writes the absolute niri executable path into schema 2,
preserving the fully-resolved configuration contract.

Connectors deliberately are not compiled into the configuration: docking and
hot-plugging can change them while the GTK authoring app is closed. If niri
cannot provide at least one connector, the service refuses to open a preview
window and leaves the already-applied paired still visible.

### Adaptive generators belong to pairings

An adaptive palette policy may name one of Noctalia's ten generators. The old
bare `adaptive` value remains valid and follows the application default;
`adaptive:<scheme>` pins the choice for that media item. Preview generation is
lazy and cached by representative still plus scheme, so opening an editor does
not run ten CLI calls on the GTK thread. Community and custom palette files are
previewed directly. Built-in palette colours remain blank until Noctalia
exposes a reliable read API; invented swatches would be worse than an honest
unknown, and applying a palette merely to discover it would mutate the desktop.

**Rejected: dropping GTK from the Python service** (~25 MB, a 3x win for a
small change). Worth knowing as a fallback, but the user wants an always-on
component that is properly cheap, and the conf split makes the Rust version
much smaller than a port of the whole model would have been.

### The only model logic that crosses into Rust

- **Schedule resolution**, because "manual override, then back to schedule"
  requires knowing what the schedule currently says. Semantics: month /
  weekday / local time, **last match wins**, a window is exclusive at its end,
  a rule may wrap midnight, the clock is injectable.
- **Renderer hand-over**: every hop between renderers must make the one being
  left let go, and a refused scene stops the video renderer *before* refusing,
  so the still already applied is what remains on screen. Apply order is still
  → mode → palette → renderer, because Noctalia derives adaptive colours from
  whatever wallpaper is currently set.

Per-display assignments carry independent playlist cursors and shuffle orders.
A manual or scheduled override still intentionally replaces every display for
its duration; without one, advancing moves each distinct assigned playlist by
one entry rather than indexing all of them through the default playlist's
cursor.

### Renderer exits fall back; they never auto-restart

The runtime reaps every mpvpaper and linux-wallpaperengine child. An unexpected
exit reapplies the already-resolved paired still, marks motion inactive, and
keeps an entry-specific error in `status` until a later wallpaper applies
successfully. It performs **zero automatic retries**. In particular,
linux-wallpaperengine is an independent Wallpaper Engine reimplementation and
some otherwise-valid Workshop scenes reliably crash it; restarting such a
scene would create an unbounded crash loop while burning CPU and lying about
playback.

A scene ID that crashes is remembered for this service process and subsequent
visits use its still without launching the engine again. The memory is
deliberately session-scoped: an engine update may make that scene work, and the
runtime must not write compatibility judgements back into app-owned data.
Videos also fall back with no immediate retry, but are eligible when a later
rotation revisits them because an mpvpaper exit is more plausibly transient.

### Pause, stop and cycle are separate runtime controls

Pause freezes the renderer in place and keeps its memory and GPU allocations;
it also suspends timed advancement. Stop tears every owned renderer down and
leaves the paired still on screen. A stopped runtime may continue advancing
through playlist stills when cycle is on, but it does not launch motion again
until `play`. This keeps "free the renderer resources" independent from "hold
this playlist entry".

`play` unfreezes a paused renderer or reapplies the current stopped entry with
motion enabled. `toggle` pauses while playing and resumes from either paused or
stopped. `cycle on|off` is a session override over the config default;
`cycle default` drops that override. Turning it back on starts a fresh interval
instead of treating time spent off as an overdue advance. None of these runtime
choices is written into the app-owned configuration.

---

## Playback is always a playlist

The user, twice: *"the structure of the walpaper system is to only play
playlists and to craft and configure them from a library."* The library is
where you craft and configure; it is not where you pick tonight's wallpaper.
Nothing plays a single item ad hoc.

---

## Open questions

### gSlapper instead of mpvpaper — DECIDED: stay on mpvpaper, low priority

The user, after seeing the crash evidence below: *"currently as it stands
gstreamer has no real data proclaiming its actual efficency and isnt in the nix
repository so imma put it as low priority and probably keep using mpvpaper."*

Two reasons, both checked:

- **The efficiency claim is still unquantified.** The README asserts lower CPU,
  RAM and GPU than mpvpaper and publishes no numbers.
- **It is not in nixpkgs.** Confirmed — `nix eval nixpkgs#gslapper` suggests
  *clapper*. It ships its own flake, so adopting it means a flake input or a
  custom derivation. That is real friction for an app distributed as a flake,
  against an unmeasured benefit.

Supporting *both* remains open and is the shape the code already allows: the
video renderer sits behind a trait with mpvpaper as the only implementation.
Nothing further is planned.

**Evidence that this is a real trade rather than a hypothetical**, kept because
it is the strongest argument on the other side. Four mpvpaper crashes on this
machine — 2026-08-05 20:10:23, 20:10:24, and 2026-08-09 18:45:01, 18:53:26 —
are byte-for-byte the same signature:

    #0  _glGetString          (libnvidia-eglcore.so.595.84 + 0x8f58fa)
    #1  mpgl_load_functions2  (libmpv.so.2)
    #2  init                  (libmpv.so.2)
    #4  mpv_render_context_create
    #5  main

That is libmpv's GL loader crashing inside the nvidia EGL driver during
**context creation, in `main`, before a frame is decoded** — not a decoder
fault and not the wallpaper's fault. It is precisely the failure gSlapper's
author describes when he says libmpv "performs pretty bad on nvidia" and that
"the issue is with its backend"; GStreamer never takes that path.

The user's judgement is that the crashes are not affecting anything in
practice, so this stays low priority. Two cheap mitigations are noted rather
than taken:

- `--auto-stop` respawns mpvpaper whenever the wallpaper is hidden, so every
  occlusion is another attempt at the fragile nvidia context creation.
  `--auto-pause` freezes instead and would attempt it far less often.
- The mpv IPC socket is currently `wall-in-one-mpv-.sock` — a dangling dash
  where an output name belongs, so every output would share one socket.

### Retry policy has two cases, not one

Renderer supervision does **zero retries**, decided because some Workshop scenes
deterministically crash `linux-wallpaperengine`, where retrying loops forever.
The mpvpaper crash above is the opposite character: transient, at startup, and
very likely to succeed on a second attempt. If retries are ever revisited, the
split is: retry once on a *startup* failure, never retry a renderer that ran
successfully and then died, and never retry a scene already known to crash the
engine this session.

### Superseded: the original open question


<https://github.com/Nomadcxx/gSlapper>, and a Noctalia plugin wrapper at
`Nomadcxx/noctalia-gslapper`. GStreamer rather than libmpv. GPL-3.0. Packaged
for AUR, Debian, Fedora, and buildable with `nix build`.

What it offers: per-output wallpapers, pause/resume over a Unix socket,
scaling modes (`fill`, `stretch`, `original`, `panscan`), and images as well as
video. CLI shape is `gslapper -o loop DP-1 /path/to/video.mp4`, with `'*'` for
every output.

**The author's stated problem applies to this machine.** From the release post:
*"The issue with libmpv is it performs pretty bad on nvidia and multi-monitor
setups, was getting high memory use and low FPS. It's not mpvpaper's fault,
it's quite efficient, the issue is with its backend."* This machine has nvidia
(`/dev/nvidia0` and `nvidia-smi` both present) with a single output, so the
nvidia half of that applies even though the multi-monitor half does not yet.

The launch surface is closer to mpvpaper than an earlier review recorded.
`--layer`, `--auto-pause`, and `--auto-stop` are identical. Its `-o` is
`--gst-options`, the direct analogue of mpvpaper's mpv-options string, so these
flags are not in themselves a migration blocker. The current implementation
runs:

    mpvpaper --layer background [--auto-pause] -o "<mpv options>" ALL <video>

The real functional gap is audio. gSlapper has no volume control; audio is
only on/off at launch through `-o "no-audio"`, while Wall-in-One preserves and
changes mpv volume live. It therefore remains a **second renderer
implementation behind the seam**, not a symlink swap. The Rust service's video
renderer interface is start, stop, pause, volume and target-output, with
mpvpaper as its only implementation for now.

**The efficiency claim is unquantified.** The README says "lower CPU, RAM and
GPU use than mpvpaper on any Wayland compositor" and gives no numbers. Before
porting anything, measure both playing the same video: RSS, CPU, and GPU if it
can be had.

Two risks to check while measuring:

- **Mute and volume are not mentioned.** This app drives them live over
  mpv's IPC socket, and losing that would be a regression rather than a
  saving.
- **Images.** gSlapper can set stills, but this app deliberately sets stills
  through `noctalia msg wallpaper-set` so that Noctalia derives the palette
  from them. Stills should keep going through Noctalia regardless of what
  plays the video.

### Two monitors

Per-display assignment is built and unverified: the development machine has one
output. Per-output *renderer settings* (mute, FPS, scaling per screen) are not
built at all.

### Nobody has used it for long

Every real bug found so far — scenes with no stills, blank scene tiles,
pixelated wallpapers, MotionBGS truncated at 36 — was found by running the
thing, not by its test suite. Assume running it longer finds more.

## Filters are inline, not a popover

The browse screen froze the whole app: open Filters, pick a MotionBGS browse
mode, and every click was ignored until the window lost and regained focus.

That recovery-on-focus-change is the tell. A compute hang does not behave that
way, and measurement confirmed there was none — the whole sequence was driven
programmatically, and 45 pages of live results were loaded, with a worst
main-loop gap of 0.55 s, all of it process startup.

It was a **stale input grab**. The Filters button was a `Gtk.MenuButton`, so its
popover took a grab; the `Gtk.DropDown`s inside it each opened *their own*
popover and took another. Then the selection handlers hid sibling rows —
`_on_mode_changed` on the genre row, `_sync_top_range` on the top-range row —
from inside signal emission, forcing the outer popover to re-measure and its
Wayland surface to resize while the inner popup still held the grab. The grab
was never handed back.

**Ruling: no configuration lives in a popover on this screen.** The filters are
a `Gtk.Revealer` under the search bar. Nested autohide popovers are the hazard,
and the only reliable way to not have the bug is to not nest them. This also
matches the standing preference already stated for pairings ("should happen in
a new screen. not a pop up"), and filters are more useful visible beside the
results anyway.

Rejected: keeping the popover and deferring the visibility change to an idle
callback. It would probably have worked, but it leaves the nesting in place and
makes correctness depend on GTK's grab bookkeeping across a resize — untestable
here, and it would have to be rediscovered by whoever next adds a control.

## Browsing is capped at 600 results, and the number is a requirement

Infinite scroll released nothing. Measured on a real listing: 36 cards cost
232 MB RSS and 483 cost 488 MB, climbing at roughly 0.6 MB a card, with the
preview queue growing monotonically — 239 outstanding after 14 pages, so the
four workers were fetching thumbnails for cards scrolled past minutes earlier
while the visible ones waited behind them.

Both are now bounded, and re-measured: 483 cards cost 279 MB rather than 488,
the queue drains to zero, and growth is about 0.11 MB a card.

**The ceiling is 600, and it is set by a requirement rather than by the
memory.** A MotionBGS search returning about 250 results has to fit entirely,
because "if motionbg returns 250 results for naruto, i should have a way to view
more than just 36" is the complaint this grid exists to answer. A first pass set
it to 240 and would have quietly reintroduced that. Anything below ~300 is
wrong no matter how good it looks in a memory graph.

## Reordering knows both sides of a card

Playlist drag-reorder only understood "insert *before* this card" and discarded
the pointer's x, so which half you released over was never examined. Running the
model over every combination of a four-card playlist showed what that costs:

    drag A onto B -> ABCD   (nudging one slot right: a guaranteed no-op)
    drag A onto A -> BCDA   (an aborted drag silently jumps to the end)

`drop_position` now takes an explicit side, and dropping a card on itself is a
no-op instead of a move to the end. The table is pinned in `tests/test_playlists.py`,
because a table is what caught this and prose would not have.

Drop feedback is a real placeholder child in a `Gtk.Revealer` rather than an
animated CSS margin: margin animation inside a homogeneous `Gtk.FlowBox` was not
worth depending on. The hard requirement on it is that the gap can never get
stuck — it is cleared on drop, leave, cancel and editor teardown, and a test
pins that a cancelled drag restores the original child count. A playlist left
with a permanent hole would be worse than no feedback at all.

### The drop target has to preload

The first attempt at the gap shipped a working drag image and a completely dead
drop: nothing reordered at all. `Gtk.DropTarget.preload` defaults to false, so
the payload is only read once a drop has been accepted, and `get_value()` is
`None` for every motion event. The motion handler read that `None`, decided the
payload was not a string it understood, and answered `Gdk.DragAction(0)` —
which tells GTK the target refuses the drag, so no drop was ever delivered.

Two rules came out of it:

- **Preload on any drop target whose motion handler inspects the payload.**
- **A motion handler fails open.** Refusing is not recoverable later in the same
  drag, so "I cannot see the payload yet" must answer yes, not no.

The lesson for the tests is the sharper one. Every drag test called the handlers
directly, so the suite was green while the feature did not work at all. Motion
and acceptance are now pinned separately, and the preload test was checked by
removing the fix and watching it fail.
