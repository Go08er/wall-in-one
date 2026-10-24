# Renderer settings: the dials, where they live, and what never gets through

The owner asked for "all the dials for both linux-wallpaperengine and
mpvpaper. Global ones and defaults in the main settings, and things that are
per wallpaper in the pairing settings", and for Wallpaper Engine's own
per-wallpaper settings to pass through. This note is the inventory that
request was built from, the line drawn through it, and the design.

Sources read: mpvpaper 1.9 (`/nix/store/b4mfmbxh…-source`), mpv 0.41.0
(`/nix/store/jqayyk76…-source`) and linux-wallpaperengine
0.0.1-unstable-2026-06-09 (`/nix/store/hjg9shb3…-source`), the versions this
flake's nixpkgs pins. Paths below are relative to each tree.

## What the app had before

| Setting (`settings.toml`) | Reaches | How |
|---|---|---|
| `video_muted`, `video_volume` | mpv `mute=`, `volume=` | `-o`, and live over mpv IPC |
| `video_hardware_decode` | mpv `hwdec=auto\|no` | `-o` |
| `video_interpolation` | mpv `video-sync`, `interpolation`, `display-fps-override`, `tscale` | `-o`, all four or none |
| `video_when_hidden` | mpvpaper `--auto-pause` / `--auto-stop` / neither | argv |
| `scene_fps` | lwpe `--fps` | argv |
| `scene_scaling`, `scene_clamp` | lwpe `--scaling`, `--clamp` | argv, per `--screen-root` |
| `own_scene_renderer` | whether the service starts lwpe at all | — |

Fixed, not settings: mpv `loop-file=inf`, `panscan=1.0`, `terminal=no`,
`input-ipc-server=<the service's socket>`; mpvpaper and lwpe `--layer
background`; lwpe `--silent` (`runtime.toml` writes `scene_muted = true`,
`scene_volume = 0`, `scene_pause_when_covered = true` as constants). Those
three keys already exist in `[renderer]`, so making them real settings needs
no new wire key at all.

## mpvpaper: how options reach mpv, and why that decides the allowlist

mpvpaper's own flags (`src/main.c:1154-1167`): `--fork`, `--auto-pause`,
`--auto-stop`, `--auto-mode FULL|MAX`, `--slideshow N`, `--layer`,
`--mpv-options`, `--verbose`, `--help-output`, and a hidden `-Z <file>`.

`--mpv-options` is not argv for mpv. mpvpaper turns every unquoted space into
a newline (`main.c:1268-1310`), writes the result to
`/tmp/mpvpaper_<pid>.config`, and has mpv load it as a config file
(`main.c:493-509`). **A value with a space, quote, backslash or newline in it
is a second config line**: anything at all could follow. So every value the
app hands mpv is one it formatted itself from a type: an enum member or a
bounded number, never a person's or a Workshop item's text. A test checks
that every rendered option is `name=value` with `value` matching
`[A-Za-z0-9.:_-]+`.

mpvpaper also sets `config=yes`, `input-default-bindings=yes` and
`input-terminal=yes` before the options (`main.c:478-484`): mpv loads the
user's own `~/.config/mpv/mpv.conf`, and its default script directory, as it
does for any mpv. That is the user's own configuration, unchanged by this
work, and nothing from the Workshop or from the app's stores ever reaches it;
the allowlist below is not what keeps scripts out, it is what keeps the
app's own dials from becoming a way in.

## linux-wallpaperengine's options

From `Application/ApplicationContext.cpp:255-640`: the background id or
path; `--window`, `--screen-root`, `--screen-span`, `--bg`, `--playlist`,
`--scaling stretch|fit|fill|default`, `--clamp clamp|border|repeat`,
`--layer`; `--fps` (default 30); `--no-fullscreen-pause`,
`--fullscreen-pause-only-active`, `--fullscreen-pause-ignore-appid` (repeatable
text); `--volume N` (default 15, clamped to 0-128, `:663`) **or** `--silent`
(a mutually exclusive group, `:518-528`); `--noautomute`,
`--no-audio-processing`; `--screenshot`, `--screenshot-delay`; `--assets-dir`;
`--disable-particles`, `--disable-mouse`, `--disable-parallax`;
`--list-properties`, `--set-property`; `--dump-structure`, `--render-debug`.

Audio processing is the engine recording the system's audio output
(`Audio/Drivers/Recorders/PulseAudioPlaybackRecorder.cpp`) so a wallpaper can
react to it: a privacy dial as much as a performance one.

## The dials exposed

Every one is a typed field with a closed range. "Global" is the default in
Settings, both interfaces; "per wallpaper" is an override in that
wallpaper's own settings (the classic pairing editor, the new interface's
details), shown as "Default (…)" or overridden, each with its own reset.

### Videos (mpvpaper and mpv)

| Dial | mpv / mpvpaper | Values | Global | Per wallpaper |
|---|---|---|---|---|
| Fit | `panscan=1.0` (fill, today's), `panscan=0.0` (fit), `keepaspect=no` (stretch) | fill, fit, stretch | yes | yes |
| Speed | `speed=` | 0.25-4.00, step 0.05 | yes | yes |
| Brightness, contrast, saturation, gamma | `brightness=` … `gamma=` | -100..100 | yes | yes |
| Rotation | `video-rotate=` | 0, 90, 180, 270 | yes | yes |
| Deband | `deband=yes\|no` | on/off | yes | yes |
| Scaling filter | `scale=` | mpv's default, bilinear, spline36, lanczos, ewa_lanczos, mitchell | yes | — |
| Pause or stop also when a window is | mpvpaper `--auto-mode` | hidden only (today), fullscreen, maximized | yes | — |
| (existing) Mute, volume, hardware decoding, smoothing, when hidden | as before | as before | yes | — |

### Scenes and web wallpapers (linux-wallpaperengine)

| Dial | lwpe | Values | Global | Per wallpaper |
|---|---|---|---|---|
| Frame rate | `--fps` | 1-240 | yes (existing) | yes |
| Scaling, texture edges | `--scaling`, `--clamp` | as before | yes (existing) | yes |
| Sound, volume | `--silent` or `--volume` (never both) | on/off, 0-100 | yes (existing keys) | yes (a scene that sounds by itself without a volume of its own takes the default's) |
| React to sound | `--no-audio-processing` when off | on/off | yes | yes |
| Particles, mouse, parallax | `--disable-particles`, `--disable-mouse`, `--disable-parallax` when off | on/off | yes | yes |
| Pause when a window covers it | `--no-fullscreen-pause` when off | on/off | yes (existing key) | — |
| Pause only for the active window | `--fullscreen-pause-only-active` | on/off | yes | — |
| Mute while other apps play | `--noautomute` when off | on/off | yes | — |

Every dial is a launch option, so changing one restarts the renderers it
reaches (a wallpaper's own: only the display showing it). Only the existing
video mute and volume stay live over mpv's IPC.

A dial that would do nothing as things are set is greyed out, never shown as
a working control: the `--auto-mode` trigger while videos keep playing when
covered, pausing for the active window only while scenes never pause, a
wallpaper's own volume while it is silent. Neither is written then either.
A **web** wallpaper's own settings hold only what reaches a page (frame rate,
scaling, texture edges, mouse): particles and parallax are a scene's, and a
page plays its own sound through the engine's browser, past `--silent` and
`--volume` (nothing in `WebBrowser/` touches audio).

### Wallpaper Engine's own properties (per wallpaper)

A scene's `general.properties` (`Data/Parsers/PropertyParser.cpp:7-46`)
becomes one control each in its settings: a switch for `bool`, a slider
(its `min`, `max`, `step`) for `slider`, a colour button for `color`, a list
of its labelled options for `combo`, a bounded single-line text for
`textinput`. Each starts from the wallpaper's declared default, then a
preset's value, then the person's own. A preset's values are passed exactly
as before, and the person's go over them, as `--set-property NAME=VALUE`,
each checked against the declaration with the same type rules and bounds
presets have (docs/wallpaper-engine.md). A label comes
from the declaration's `text`, which in real content is sometimes a
translation key: it is shown as written, as plain text.

`file`, `directory` and `scenetexture` properties are never offered (a path
from a user-writable file into the renderer). For a **web** wallpaper the
engine never hands properties to the page (CWeb.cpp), so its details say so
and show no controls that would do nothing.

## Left out, and why

- Anything that runs code or reads or writes a file of its choosing, in
  mpv: `script`, `scripts`, `load-scripts`, `include`, `config`,
  `config-dir`, `input-conf`, `input-ipc-server` (the service's own, never a
  dial), `sub-file`, `audio-file`, `external-files`, `cover-art-files`,
  `glsl-shader(s)`, `vf`, `af`, `lavfi-complex` (`movie=` opens any file),
  `demuxer-lavf-o`, `stream-lavf-o`, `ytdl*`, `script-opts`, `log-file`,
  `dump-stats`, `screenshot-*`, `stream-record`,
  `watch-later-*`, `playlist`, `cookies-file`, `profile` (loads named
  configuration), and every `--lua-*`/`--js-*`.
- mpvpaper `--fork` (escapes the service's supervision), `-Z` (writes a
  file), `--slideshow` (the service rotates wallpapers itself), `--verbose`,
  and `--layer` for either renderer (compositor rules depend on
  `background`; it stays fixed).
- lwpe `--assets-dir`, `--screenshot*` (paths; the app uses the screenshot
  only for its own captures), `--playlist` (reads Wallpaper Engine's
  config), `--window`, `--screen-root`, `--screen-span`, `--bg` (the
  service's), `--list-properties`, `--dump-structure`, `--render-debug`
  (debugging).
- mpv `start` ("start at"): `loop-file` seeks back to the file's start, not to
  `--start` (`player/playloop.c:935`), and an A-B loop needs both ends
  (`player/misc.c:135`), so it would hold for the first pass only.
- Deferred, not refused: `--fullscreen-pause-ignore-appid` (free text; would
  need its own bounded list), mpv `video-zoom`/`video-pan-*`/`video-align-*`,
  `video-aspect-override`, `framedrop`, track selection, per-wallpaper video
  volume (audio is live over IPC, and per-wallpaper levels need their own
  design), and `loop-file` (a wallpaper loops).

## Storage

Global dials are `settings.toml` fields; each new one is written only when it
differs from its default (as `stop_animations_on_battery` and
`allow_web_wallpapers` are), so an untouched profile keeps its bytes.

Per-wallpaper overrides ride on that wallpaper's record in `pairings.json`,
keyed by the existing pairing identity (a scene's Workshop id, a video's
path), as one optional `renderer` object:

```json
{"identity": "scene:1647046763", "renderer": {"scene": {"fps": 60},
  "properties": {"bloom": "false"}}}
```

No format version bump. `pairings.json` version 2 already carries keys it
doesn't model through every save, record by record (`state_file.Shape`,
`pairings.DOCUMENT_SHAPE`). Checked against 4c28b8b's own `pairings.py`
before writing a line (`.project-notes/wt/probe/pairings_carry.py`): a record
holding only `renderer`, and one with a palette as well, both keep their
`renderer` byte for byte through that build's edits. A bump would instead
have made every older build refuse to save pairings at all. An older build's
"Reset to defaults" removes the record, overrides included, which is what a
reset is. In this build "Reset to defaults" still means the still and the
colours; each override has a reset of its own, and "Use defaults" takes them
all back. A record holding only overrides is kept; one holding nothing goes.

A dial's value is kept as the file holds it, valid in this build or not, so a
newer build's wider range survives this one's saves (this build applies only
what it can check), and a dial this build doesn't know rides through: the
state file shape can now put back a nested object (`renderer`, `video`,
`scene`) that holds only what it carried.

## Wire

`[renderer]` gains a key per new global dial, **written only when it isn't
the default**; the service reads each with a default of its own. Per-entry
overrides are two optional tables on a playlist entry, typed, and written only
when that wallpaper has one:

```toml
video = { fit = "fit", speed = 1.5, brightness = 10 }
scene = { fps = 60, muted = false, volume = 40, particles = false }
```

A wallpaper's own Wallpaper Engine values travel in the existing `properties`
table (a preset's values merged under the person's own), which schema 6
already carries on any scene entry.

**Schema 7**, for any document with a `video` or `scene` table or a new
`[renderer]` key off its default. The three scene keys that already existed
(`scene_muted`, `scene_volume`, `scene_pause_when_covered`) are 0.4.0's and
need nothing newer; while scenes are silent `scene_volume = 0` is written, as
before, so an untouched profile compiles byte for byte what 4c28b8b does
(checked on a fixed library, both default and tuned settings). Not 6: schema 6 already shipped on r11's branch as "entries
may carry `scene_base`/`properties`", and if r11 is released first a 6-only
service must refuse what it can't read rather than half-apply it. Every
other document keeps 4, 5 or 6, byte for byte. As for presets, publishing a
schema-7 document to the live path asks the running service first; one that
doesn't list 7 gets the document without the tables and the new keys (then,
if it doesn't list 6, without presets and a scene's own values too: that scene
still plays), and the app says once that those settings apply after the
service is restarted. Saving is never refused over them, unlike the battery
option: an older service just doesn't apply them yet. The health sync
compiles exactly that for the status it holds (`dials_supported`). The legacy
Python `--service` mode (installs without the Rust service) doesn't apply
the dials.

A change to one wallpaper's overrides changes only its entries, so only that
wallpaper's renderer is handed over; a global change is a renderer-settings
change, which hands over every renderer as it does today.
