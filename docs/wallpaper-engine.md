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

**Where it looks.** An id given to `--bg` is looked for only under the home
folder's Steam installations: `~/.local/share/Steam`, `~/.steam/steam`, the
Flatpak's and the Snap's `steamapps/workshop/content/431960/<id>`
(`Steam/FileSystem/FileSystem.cpp:16-21`, `:39-53`), and Wallpaper Engine's
own assets the same way (`:9-14`, `:55-69`). Wall-in-One lists the items of
every library folder `libraryfolders.vdf` names, a second drive's included.
A video there plays (mpvpaper is given its path), but a scene, web
wallpaper or preset in a library outside those home folders may not: the
engine won't find it, or its assets. This is documented rather than worked
around. Handing the engine a directory instead of an id (it takes a path
for `--bg`) would mean a new wire field, and it would still not find the
assets, which have no such option per item. The wire is unchanged for every
existing scene.

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
| `video` with its file (the type read leniently, stripped and case-folded, and in a folder of any name) | Video | mpvpaper, as before |
| `video` whose file is gone, or one the app won't open | not listed, as before | – |
| `scene` naming a `.json` | Scene | `--bg <id>` |
| `web` naming a `.html`/`.htm` | Scene · web | `--bg <id>`, only with Allow web wallpapers on (off by default) |
| preset whose base is installed and plays | Scene · preset, titled and previewed as the preset | `--bg <base>` plus its properties |
| preset of a web wallpaper | Scene · preset, "plays as <base>" | `--bg <base>`, no properties; only with web wallpapers on |
| preset whose base is not installed (or names none) | Scene · preset, "needs its base" | never |
| `application` (or a preset of one) | Scene · application, unsupported | never, whatever the engine supports |
| any other type, or none (or a preset of one) | Scene · its type, unsupported | never |
| `scene`/`web` whose file isn't what its type names | Scene · its type, unsupported | never |
| a supported type missing what the engine requires (`title`, `file`), or a `file` padded with spaces | Scene · its type, unplayable | never |
| a type the engine wouldn't read as written (`" scene"`, a long-s "ſcene"), other than a video's | Scene · unknown, unsupported | never |
| preset of a `video` whose file is gone or is a link | Scene · preset, "needs its base" | never |

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

### Web wallpapers are off until allowed

Owner decision, after the review of r11: a `web` wallpaper, and a preset of
one, plays only with **Allow web wallpapers** (`allow_web_wallpapers`, off by
default) turned on. What it means is said where it is turned on: the page's
JavaScript runs in linux-wallpaperengine's built-in browser, an older
Chromium, with web security and site isolation off
(`WebBrowser/CEF/BrowserApp.cpp:21-37`), so the page can reach web services on
the computer and its network and send data out.

The item's classification doesn't depend on the setting (it is a scan fact,
`MediaItem.web_page`); every consumer asks `workshop.why_not_played(item,
allow_web)`. While it is off the compiler leaves such items out of
`runtime.toml`, Apply and the legacy applier refuse them with "web wallpapers
are off: turn on Allow web wallpapers in Settings → Library to play it", the
stills worker gets none of them, and the new interface's details pane leads to
the setting. They are not counted with the unsupported ones: the Scenes view
says "N web wallpapers are off" apart. A settings change recompiles and
reloads as any other does, and turning it on also makes their stills. The key
is written to `settings.toml` only when on, so a profile that never turns it
on keeps its bytes. Executables stay refused either way.

**When web pages are all that plays.** Turning the setting off then leaves
All media with nothing to play. The display shows nothing, and the app says
"Nothing to play: web wallpapers are off." The document says so
([schema 8](runtime-config.md#nothing-to-play-schema-8)), so `play`, the
companion's Resume, `ctl toggle` and the service's own reload of the file
can't bring the page back. Noctalia keeps the last still it was given, which
for a page is its own still: the service has no way to clear it.

**When the setting can't reach the service.** A compile that fails, or a
reload the service refuses, leaves it on the document it holds. When that
plays a web page, or an item the scan can't classify, the app stops it and
hands the service a document that plays nothing; an older service that can't
read that is told to quit, and its next start is guarded. At service start,
a kept document that plays a web page is still refused with exit 78.

**The service checks too.** linux-wallpaperengine reads `project.json` again
when it starts, so an item whose type changed to `web` after the compile (a
Workshop update) would play as one. While the document doesn't allow web
wallpapers (`allow_web_wallpapers`, written only while the setting is on),
the service reads the item's type just before it starts the engine, in every
place the engine looks for `--bg <id>` (above), bounded like the app's scan
(256 KiB, a regular file, nested to any depth). For a preset it reads the
base. Anything but `scene` or `video` is refused, and so is an item it can't
find or read: a scene in a Steam library on another drive is one, since the
engine never looks there. Each reason says what it is; only a web page is put
down to web wallpapers ("this configuration doesn't allow web wallpapers",
since a document can lack the key for other reasons than the setting being
off). The engine isn't started, and the entry is skipped, as the owner put
it: "skip to next, if it's in a playlist alone, then use the default playlist
in schedule". The service asks before anything of the entry is shown, so its
still never goes up; the next entry of the same playlist takes over. A
playlist with nothing left that can play gives way to the document's default
playlist, then to All media; with nothing left at all the display plays
nothing ([schema 8](runtime-config.md#nothing-to-play-schema-8)). It is never
a taboo, so nothing is saved against the item; the service's journal says
what it skipped and why. Each item is asked once per document: a reload
(which turning the setting on or off is) asks again. A document from before
schema 8 has no such key, so it allows no web page. While the document does
allow web wallpapers nothing is checked, and such a scene fails in the engine
as at 0.4.0.

**No previews while it is off.** Owner: "it shouldn't load the papers to make a
preview if web wallpapers are off." While the setting is off, nothing in a web
wallpaper's folder (`MediaItem.web_files`, below) is opened
except its `project.json`, which the scan reads to know what the item is.
No thumbnail, still, colour or screenshot is made from its preview, its page
or anything else there, and linux-wallpaperengine is never started for one.
The Library tile, the details pane and the pairing editor show a plain tile
like the "Can't play" ones, still badged "Scene · web", and say "Web
wallpapers are off". Pictures the app made while it was on are its own files,
but they are not shown either: the placeholder is the same whether or not one
is cached, and a cache miss never starts a generation. Every step reads the
setting when it runs (`workshop.previews_off(item, allow_web)`), not when the
work was queued. Turning it on makes the previews as usual. Turning it off
again drops what is queued for web wallpapers, discards a picture that was
being made, and ends a running ffmpeg or linux-wallpaperengine started for
one by its own process group (`worker_processes.GatedCancellation`); nothing
is signalled by name. The preview gate fails closed and is wider than
`web_page`, which stays playback's word: `MediaItem.web_files` (set from
`project.json` alone, `workshop.holds_web`) is also true for an item whose type
reads `web` leniently (stripped, case-folded) though it can't play, and for a
preset whose base is web, not installed, unnamed or itself a preset. A malformed
scene keeps its "Can't play" preview. The scan only `lstat`s the files `project.json` names
(it never opens them).

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

Something else the source shows, which stays as it is:

- CEF's helper processes are started with the engine's own arguments
  appended (`BrowserApp.cpp:49-53`), `--screen-root` included. The service
  doesn't look for other engines. The app's legacy applier
  (`wallpaper.scenes.running_elsewhere`) stops its own engine, helpers and
  all, before it looks, so it no longer counts them (or its own engine) as a
  foreign one.

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
  integer or a decimal; an integer is judged as one (JSON allows any number
  of digits), and a non-zero value below the smallest normal float, which
  `std::stof` refuses (`1e-38` does, `1.2e-38` doesn't), is written as 0;
- `color`: three or four finite numbers in 0-1, written as fixed decimals
  (`0.501961 1.000000 0.000000`);
- `combo`: a string or integral number that is one of the base's option
  values, a number within 32 bits (the engine reads it as an `int`);
- every other type, `null`, and anything else is left out;
- at most 512 properties, each value at most 256 bytes with no control
  characters. A preset with more is listed but not played, and says why.

### Carrying it to the service: additive, optional fields, schema 6

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

**Compatibility: schema 6, asked for first.** A document with a preset
entry is `schema_version = 6`, and only such a document; every other one
keeps 4 (or 5 with the battery option), and a test pins one without presets
byte for byte to what 0.4.0's compiler writes. The 0.4.0 service refuses a
preset document whole: its `--check-config` says "unknown field
`properties`" and exits 78, while it accepts the same document with the
presets left out (both checked against the installed 0.4.0 binary).

So the app follows the battery option's precedent (`require_schema`): before
publishing a schema-6 document to the live path it asks the running service
for `supported_config_schemas`. When 6 isn't there, or the service can't be
asked, or its loaded units can't be verified as current, the document is
compiled again **without the presets**, which are said once as left out
("The Wallpaper Engine preset "…" isn't played until the wallpaper service is
confirmed to read presets: restart it after an update, and make sure its
installed units are current"), and everything else
is published as usual: one older service never stops the library updating.
A service that isn't running gets the presets, as an offline preparation
does for schema 5. The health sync accepts the service's generation when it
matches either compile, with or without the presets
(`runtime_config.compiled_as_published`), because the status shows the
schemas but not whether the units were verified.

### A scene's own settings

The owner asked for Wallpaper Engine's per-wallpaper settings to pass through
([renderer dials](renderer-settings.md)). A scene's settings, in both
interfaces, list what its `project.json` declares under `general.properties`
that a person may set: a switch for `bool`, a slider in its own `min`-`max`
for `slider`, a colour for `color`, its labelled options for `combo`, a line
of text for `textinput`, labelled with the declaration's `text` as written
(sometimes a translation key) and in the author's `order`. A `file`,
`directory` or `scenetexture` is never offered (a path, from a file anyone
can write, into the renderer), and neither is a property whose declared value
the engine couldn't read, a slider with no range, or a combo with no options.
A preset shows its base's, starting from its own values.

Each starts from the declared value, then a preset's, then the person's own,
which is kept on the scene's pairing record (`renderer.properties`, by name,
as the engine reads it). At compile time each is checked against the
declaration again, as a preset's is: a switch is `true` or `false`, a slider
a finite number in its range, a colour three numbers in 0-1, a combo one of
its option values, a line of text at most 256 bytes with no control
characters. The person's go over a preset's in `properties`, so a document
with one is schema 6, and a service that doesn't read 6 gets the scene
without them, playing as it is, and a notice. A web page has none: the engine
hands it no properties (CWeb.cpp only loads its URL), so its settings say so
instead of showing controls that would do nothing, and nothing is ever
compiled for one, whatever a hand-edited file says.

A `textinput`'s declared value is what the author wrote; the engine itself
starts one from its JSON text (quoted), and a value set here replaces that.

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
Workshop folder with no `project.json`, one that isn't readable JSON, one
whose reading raised something nothing foresaw ("its project.json could not
be read": one item never stops the scan), and one whose name isn't an ASCII
Workshop id (`str.isdigit` would take "١٢٣"), unless it holds a video, which
mpvpaper plays by its file, so neither the engine's type and file rules nor
the id rule apply to it. Those notes use a new kind of unseen place, "not a
wallpaper", which, like an absent library folder, means "looked, and nothing
usable is there": a Workshop folder Steam is still writing must not make the
compiler refuse over "couldn't look", and such a note is kept in `unseen`
and out of `Library.skipped` (the upgrade and the capture cleanup wait only
for what couldn't be looked at).

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
