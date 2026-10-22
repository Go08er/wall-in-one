# Wallpaper Engine items: what plays, and how

Wall-in-One lists what Steam installed for Wallpaper Engine (application 431960)
from each Steam library's `steamapps/workshop/content/431960/<id>/project.json`.
Until this change it kept two kinds and dropped the rest without a word: a
`video` item with its file on disk, and a `scene`. A `web` wallpaper and a
preset therefore never showed up, which is what the owner reported (one new
`web` item, one new preset, out of 51: 45 video, 4 scene, 1 web, 1 preset).

This note records what `linux-wallpaperengine` itself can run, read from the
source nixpkgs builds (`linux-wallpaperengine` 0.0.1-unstable-2026-06-09,
`/nix/store/hjg9shb30f613xzyb00x3vdrzzvmfv86-source`, the revision this flake's
`nixpkgs` pins), and the design that follows from it. Paths below are relative
to that tree's `src/WallpaperEngine/`.

## What linux-wallpaperengine runs

**Item types.** `--bg <id>` resolves the id to Steam's Workshop directory
(`Application/ApplicationContext.cpp:366-376`, `:709-715`) and the engine reads
that directory's `project.json` (`Application/WallpaperApplication.cpp:226-243`).
The parser requires a string `type` (`Data/Parsers/ProjectParser.cpp:20`), a
string `title` (`:36`) and a `file` (`:44`), lower-cases the type (`:33`) and
knows exactly three: `scene`, `video` and `web` (`:49-63`). Anything else ends
in `sLog.exception ("Unsupported project type ", type)` (`:62`), which `main`
catches and turns into exit status 1 (`main.cpp:74-77`).

So the **supported set is `scene`, `video` and `web`**. `application`, any
other type, and a missing type are not.

**Web.** A `web` item is a first-class wallpaper there: `WallpaperParser.cpp:89-94`
builds it, `WallpaperApplication.cpp:526-540` starts CEF only when a web
background is loaded, and `Render/Wallpapers/CWeb.cpp:33-37` opens
`wp<id>://root/<file>` in an off-screen browser. It is given with `--bg <id>`
exactly as a scene is, so the service's scene path plays it unchanged. The
nixpkgs package is built with CEF; a build without it would fail at start, and
the service's crash handling would cover that like any scene crash.

**Presets.** A preset's `project.json` has a `preset` object (property name to
value) and a `dependency` (the Workshop id of the wallpaper it is a preset of),
but no `type` and no `file`. Given the preset's own id the engine stops at
`ProjectParser.cpp:20` ("Project type missing"). It has no notion of presets:
nothing in the source reads `preset` or `dependency`.

**Properties.** `--set-property NAME=VALUE` (alias `--property`) is repeatable;
the value is everything after the first `=`, and a bare `NAME` means `1`
(`ApplicationContext.cpp:583-595`). The overrides are one map for the whole
process (`settings.general.properties`), applied when each background loads
(`WallpaperApplication.cpp:435`, `:502-524`): the loop walks the properties the
*background* declares in its `general.properties`
(`ProjectParser.cpp:65-90`) and looks each up in the overrides, so a name the
base does not declare is ignored. How a value is read depends on the type the
base declares (`Data/Model/Property.h`, `Data/Parsers/PropertyParser.cpp:7-46`):

| Declared type | Parsed by | A bad value |
|---|---|---|
| `bool` | `value == "true" \|\| value == "1"` (`Property.h:71-73`) | reads as false |
| `slider` | `std::stof` (`Property.h:48`) | throws: exit 1 |
| `color` | `ColorBuilder::parse` (`Property.h:93-95`) | throws: exit 1 |
| `combo` | must be one of the option values (`Property.h:116-124`); options are keyed by their value, an integer as `std::to_string` (`PropertyParser.cpp:57-68`) | logged, ignored |
| `text`, `scenetexture`, `file`, `directory`, `textinput`, `usershortcut` | stored as text | – |

`ColorBuilder::parse` (`Data/Builders/ColorBuilder.cpp:52-68`) reads three or
four numbers separated by spaces or commas, and **reads them as 0-255 integers
when the string has no `.`** (`:58-63`). Wallpaper Engine writes colours as
0-1 floats, so its `"1 1 0"` (yellow) would become almost black; every colour
is therefore written back with a decimal point.

A preset of a **scene** gets its properties through the scene's user
bindings (`general.user (...)` in `WallpaperParser.cpp:42-77` and the objects'
own). A preset of a **web** wallpaper does not: Wallpaper Engine hands a web
page its settings through `window.wallpaperPropertyListener`, and nothing in
this source calls into the page (no `ExecuteJavaScript`, no
`applyUserProperties`; `CWeb.cpp` only loads the URL and forwards the mouse).
Such a preset plays its base's defaults until the engine learns to forward
them. The owner's one preset (Y2K Matrix) is of a web wallpaper (Electric Eye).

**Screenshots.** `--screenshot` is taken from the output after
`--screenshot-delay` frames whatever the wallpaper is
(`WallpaperApplication.cpp:942-951`, `:542`), but the delay is clamped to at
most 5 frames (`ApplicationContext.cpp:664-667`). A web page is painted by
CEF asynchronously (`WebBrowser/CEF/RenderHandler.cpp:13-21`), so five frames
after start is very likely a blank page that would still count as a
successful capture, and then be somebody's wallpaper and palette. Stills for
anything but a true `scene` therefore come from the item's own `preview`
(see Stills below). CEF also makes a fresh cache directory in the system
temporary directory on every start (`WebBrowser/WebBrowserContext.cpp:78`).

## Design

### One category: scenes and other Wallpaper Engine items

Owner decision: every Workshop item that is not a playable video is a
**scene** in the library (`Kind.SCENE`), shown in the existing Scenes
category. Its own type is shown beside it ("Scene · web", "Scene · preset"),
in both interfaces' kind badges; the category keeps its name, so the classic
"Videos and scenes" filter and the new interface's "Scenes" tab are unchanged.

| Item | Listed as | Plays |
|---|---|---|
| `video` with its file | Video | mpvpaper, as before |
| `video` whose file is gone, or one the app won't open | not listed, as before | – |
| `scene` naming a `.json`, `web` naming a `.html`/`.htm` | Scene · its type | `--bg <id>` |
| preset whose base is installed and plays | Scene · preset, titled and previewed as the preset | `--bg <base>` plus its properties |
| preset whose base is not installed (or names none) | Scene · preset, "needs its base" | never |
| `application` (or a preset of one) | Scene · application, unsupported | never, whatever the engine supports |
| any other type, or none (or a preset of one) | Scene · its type, unsupported | never |
| `scene`/`web` whose file isn't what its type names | Scene · its type, unsupported | never |
| a supported type missing what the engine requires (`title`, `file`) | Scene · its type, unplayable | never |

An item that can't play is never handed to linux-wallpaperengine, by the
service, the app's own applier or a still capture: the compiler leaves it out
of `runtime.toml`, and Apply says why. That also settles the crash
quarantine question for unsupported types. Today a scene whose engine exits
for any reason other than "could not be started" is taboo at once
(`service/src/renderer.rs`, `permanent_for_session`) and the app then saves a
durable "Playback unavailable" marker; for a type the engine rejects
deterministically that would be the right outcome but a noisy way to reach
it, so such an item is not tried at all, and a later engine that supports it
needs no marker cleared.

A `video` item whose file is gone stays unlisted, as before, rather than
going to the engine: deleting a Workshop video's file through the app is how
it leaves the library, and the dry run of that delete must agree with it.

### Executable wallpapers are never run

Owner decision, for security: a Wallpaper Engine `application` wallpaper is a
program, and a Workshop item anyone can publish that runs as the user is the
easiest trojan there is. `library.workshop.NEVER_RUN = {"application"}` is
checked before the supported set, so a later change to `SUPPORTED_TYPES` (or
an engine that learns the type) can't re-enable one; a preset whose base is
one is refused the same way. Such items are listed, badged and counted with
the unsupported ones. (linux-wallpaperengine doesn't run them today either:
`ProjectParser.cpp:49-63`.)

The deny is by declared type, plus one cheap check on the entry file's
**name**: a `scene` must name a `.json` and a `web` wallpaper a `.html` or
`.htm`, or the item is unsupported. Neither file is ever executed (the engine
parses the one and opens the other in a browser), so a mislabelled item is
the only thing to catch, and its name is enough to catch it. Content is not
sniffed: that would mean reading files from a folder anyone can write to, to
protect against nothing that runs them. Nothing is executed or probed to
decide.

Two things the source shows that stay as they are, for the owner to weigh:

- A web wallpaper runs its own JavaScript in CEF, as in Wallpaper Engine
  itself, and linux-wallpaperengine starts CEF with `--disable-web-security`,
  site isolation off (`WebBrowser/CEF/BrowserApp.cpp:21-37`).
- CEF's helper processes are started with the engine's own arguments
  appended (`BrowserApp.cpp:49-53`), `--screen-root` included. The service
  doesn't look for other engines, but the app's legacy applier
  (`wallpaper.scenes.running_elsewhere`) would count those helpers as a
  foreign engine on that output.

A preset is its **own** library item: its identity (`MediaItem.scene`, which
keys pairings, stills, lifecycle locks and the service's taboo identity) is
the preset's own Workshop id. Keying it by the base would make a preset, its
base and every other preset of that base one wallpaper: one crash would
quarantine all of them and one still would represent all of them. What it
plays is carried separately (`MediaItem.scene_base`, `scene_properties`).

### Translating a preset

At scan time, against the base's own declarations (the base is found in the
same scan):

- names: `[A-Za-z0-9_][A-Za-z0-9_-]{0,63}`, and declared by the base; anything
  else is left out. That charset is a TOML bare key, cannot contain `=`, and
  never starts with `-`, so the argument can't read as an option;
- `bool`: a JSON boolean, written `true`/`false`;
- `slider`: a finite JSON number (not a boolean) within ±1e9, written as an
  integer or a decimal;
- `color`: three or four finite numbers in 0-1, written as fixed decimals
  (`0.501961 1.000000 0.000000`);
- `combo`: a string or integral number that is one of the base's option
  values;
- every other type, `null`, and anything else is left out;
- at most 512 properties, each value at most 256 bytes with no control
  characters. A preset with more is listed but not played, and says why.

### Carrying it to the service: additive, optional fields

A scene entry in `runtime.toml` gains two **optional** keys, written only for
a preset:

```toml
[[playlists.entries]]
id = "..."
kind = "scene"
still = "/.../Wall-in-One/Automatic Stills/2946165131.png"
scene_id = "2946165131"          # the preset's own id: identity, as before
scene_base = "2777242023"        # new: what to render
properties = { arcs = "true", arcsVisualsColor1 = "0.000000 0.501961 0.000000" }   # new
palette = { kind = "adaptive", scheme = "m3-tonal-spot", mode = "auto" }
```

The service renders `--bg <scene_base or scene_id>` and adds one
`--set-property` and one `NAME=VALUE` argument per property, in name order,
each a separate argv entry (no shell). It re-checks the bounds above and
refuses a document that breaks them, like any other malformed document; the
compiler never writes one. `properties` and `scene_base` are part of the
entry, so changing either is a hand-over, and the taboo identity stays the
scene id, the preset's own.

**Compatibility.** No schema bump. A document without presets is byte for
byte what this release compiled before (a test pins it), so a 0.4.0 service
keeps loading it. A document with a preset carries a field the 0.4.0 service
does not know; its `deny_unknown_fields` refuses the whole document, it keeps
its last good configuration (fail closed: nothing is applied wrongly), and
the app's `restart_hint` tells the user that restarting the service finishes
the update. That hint compares the running service's version with the app's,
so it speaks once this ships in a release numbered above 0.4.0; on this
branch both say 0.4.0.

### Stills

A true `scene` keeps the engine capture. Every other item listed as a scene
gets its automatic still (the same `<id>.png` in `Automatic Stills`) from its
own `preview` through ffmpeg: the first frame of a `.gif`, or the picture
itself. It is made once and not re-judged against the display's size, since
a preview is as large as it will ever be. An item that can't play gets no
still at all.

### Reporting

Nothing that is a Wallpaper Engine item is dropped any more, a missing-file
video apart (above). What the scan can't list at all is said with the scan's
other notes (the classic subtitle, the new interface's count tooltip): a
Workshop folder with no `project.json`, or one that isn't readable JSON.
Those notes use a new kind of unseen place, "not a wallpaper", which, like an
absent library folder, means "looked, and nothing usable is there": a
Workshop folder Steam is still writing must not make the compiler refuse over
"couldn't look".

The interfaces keep their category names: the new interface's **Scenes** tab
and the classic **Videos and scenes** filter now also hold web wallpapers,
presets and the rest, and each card's kind badge says what it is ("Scene ·
web", "Scene · preset", "Scene · application"). An item that never plays
says **Can't play** on its card, with why, and its Apply is off; the new
interface's details pane says it as "Not played here".

In the Scenes view (classic: "Videos and scenes") a small notice counts the
items no renderer here runs, executable ones included: "1 Wallpaper Engine
wallpaper needs a runtime linux-wallpaperengine doesn't support yet" (plural
otherwise; hidden at 0), with up to ten of their titles in its tooltip. A
preset waiting for its base is not counted there; its own card says what it
needs. The notice follows every scan.

### Checking for new items

Steam adds items whenever it likes. The Scenes and Videos views (classic:
"Videos and scenes") get a **Check Wallpaper Engine for new wallpapers**
button. It runs the existing rescan (the one on F5), so it is the same scan,
with the same cancellation and generations, and the notice above follows it.
It shows while Wallpaper Engine scanning is on (`scan_workshop`), is off
during a scan, and in the new interface needs what F5 needs (the library's
folder authoring).

A file monitor on the Workshop folders was considered and left out: the scan
is a whole-library generation with its own cancellation and dry runs, and a
monitor would need its own debounce and lifetime across library-folder and
Steam-library changes for a case the button covers.
