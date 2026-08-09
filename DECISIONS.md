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
- **Socket**, spoken by the app and the plugin: play/pause/toggle,
  next/previous/random, shuffle, `playlist-use` as a manual override,
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

### Schema 1 is strict TOML

The resolved runtime document is TOML, documented in
`docs/runtime-config.md`. TOML makes the standalone contract test genuinely
standalone: a person can read and hand-write it without the Python app or a
serializer, while both sides have bounded parsers. Schema 1 rejects unknown
fields as well as unknown versions, so mismatched app and service builds fail
out loud instead of silently ignoring a renderer setting.

All executable and media paths are absolute. The service performs no PATH
lookup, glob, library lookup or provider lookup. The app writes through a
same-directory temporary file, fsync and rename. A bad reload keeps the last
valid document in force; the runtime socket also exposes an explicit `reload`
verb. This preserves the one-way app-to-service configuration boundary.

### Lifetime: session-scoped, not a system daemon

Started by the Noctalia plugin, owned by the shell session, exits with it. The
plugin prefers `wall-in-one.service` and falls back to `wall-in-one --service`.

---

## Why the service is being rewritten in Rust

Measured RSS of the windowless Python service on the development machine:

| process | RSS |
|---|---|
| bare Python | 14.0 MB |
| + GLib | 27.9 MB |
| + GTK4 + libadwaita | 55.8 MB |
| **the service as it was** | **71.1 MB** |

31 MB of that is a GUI toolkit loaded into a process with no window. CPU is
irrelevant — it sleeps and wakes once a minute. This is a memory argument only.
Target: single-digit MB.

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

---

## Playback is always a playlist

The user, twice: *"the structure of the walpaper system is to only play
playlists and to craft and configure them from a library."* The library is
where you craft and configure; it is not where you pick tonight's wallpaper.
Nothing plays a single item ad hoc.

---

## Open questions

### gSlapper instead of mpvpaper — unresolved, needs measuring

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

**"Drop-in" does not hold for how this app invokes mpvpaper.** The post says a
symlink is enough and the same commands work. That is true for simple use and
false for ours. This app runs:

    mpvpaper --layer background [--auto-pause] -o "<mpv options>" ALL <video>

In mpvpaper `-o` takes a *string of mpv options*. In gSlapper `-o` selects a
*scaling mode* (`fill`, `stretch`, `original`, `panscan`). Same flag, different
meaning. Worse, the options this app pushes through `-o` include
`--input-ipc-server`, which is how mute and volume are changed on a wallpaper
that is already playing. gSlapper has its own socket for pause/resume, and its
README does not mention volume at all.

So gSlapper is a **second renderer implementation behind the seam**, not a
symlink swap. The service's video renderer is being written behind a small
interface (start, stop, pause, volume, target an output) so that adding it
later is cheap.

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
