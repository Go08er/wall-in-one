# Wallpaper Engine items: what plays, and how

Wall-in-One lists what Steam installed for Wallpaper Engine (application 431960)
from each Steam library's `steamapps/workshop/content/431960/<id>/project.json`.
Up to 0.4.0 it kept two kinds and dropped the rest without a word: a `video`
item with its file on disk, and a `scene`. A `web` wallpaper and a preset
therefore never showed up, which is what the owner reported (one new `web`
item, one new preset, out of 51: 45 video, 4 scene, 1 web, 1 preset). The
other kinds are now listed too, each with its type and, when it can't play,
why; [the table below](#one-category-scenes-and-other-wallpaper-engine-items)
says what is listed as what, and what still isn't.

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
successful capture, and then be somebody's wallpaper and palette. A web page
therefore gets no engine screenshot, and no automatic still unless a setting
asks for its own preview (see Stills below); a true `scene`, a preset of one,
and a Workshop video get theirs from the engine or ffmpeg. CEF also makes a fresh cache directory in the system
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

**Where a web wallpaper was.** The compiler follows the owner's rule (quoted
in full under "The service checks too", below): it leaves a web wallpaper out
of its playlist, and the rest of that playlist plays. A playlist left
with nothing (a web wallpaper alone) is left out too, and so is what names
it: a display whose own playlist it is follows the schedule and the default
playlist, a schedule rule that names it doesn't apply, and a default that
names it is built as All media. The app says each of these
([runtime-config.md](runtime-config.md)).

**When web pages are all that plays.** Only then, when nothing in the
library can play with the setting off, does All media have nothing to play,
and no wallpaper is played. The app says "Nothing to play: web wallpapers are
off." The document says so
([schema 8](runtime-config.md#nothing-to-play-schema-8)), so `play`, the
companion's Resume, `ctl toggle` and the service's own reload of the file
can't bring the page back. Noctalia keeps the last still it was given, and
the service has no way to clear it: that is the still of whatever played
last, which is a page's own only if the page played while web wallpapers
were on.

**When the setting can't reach the service.** A compile that fails, or a
reload the service refuses, leaves it on the document it holds; its status
says which (`config_generation`), and the app judges that one. It also
judges the file on disk when the service hasn't loaded it (one written by
something that doesn't reload the service, such as a hand-run
`wall-in-one --write-config`): the service's own check of the file loads it
within about a second. When either allows web wallpapers
(`allow_web_wallpapers = true`, from while the setting was on), whatever it
names, or when the service predates schema 8 (it has no check of its own) and
the document plays a web page, an item the scan can't classify, or one the
app no longer knows, the app stops it. A service that reads 8 is then handed
a document that plays nothing, which also replaces the file on disk; one that
can't read it is told to quit, and its next start is guarded. A service that
reads 8, holding a document that doesn't allow web wallpapers with none on
disk that does, is left alone: it refuses every page itself (below, where an
`application` or an item it can't read is refused with the setting on too),
and the app only says that the setting hasn't reached it yet. With no service
running, nothing is stopped, but a file on disk that allows web wallpapers is
replaced the same way, so that a start already past its check can't load it.
A publication that read the setting before it was turned off (a service start's
compile, say, while the person turns it off in the window) installs nothing
that allows web wallpapers either: the setting is read again just before the
install, under the lock its own save takes, and the document is compiled
again for what it says ([runtime-config.md](runtime-config.md)).
When the app ends, a publication of the setting that the quit cut off before
it installed anything (it was turned off just before Ctrl-C, say) leaves the
file it would have replaced. A Ctrl-C that quits without the changes still
waiting to be saved never drops this one: turning web wallpapers off is the
safe direction, so it is saved all the same, and judged here ([quitting from
a terminal](../README.md#quitting-from-a-terminal)). So, last, while the
setting is off, a file on
disk that allows web wallpapers is replaced with the document that plays
nothing, without asking the service anything: its own check of the file
loads it. No wallpaper plays then until a publication gets through: the
app's next start, or the service's, whose start compiles again. Nothing is
compiled at the end: a compile that can't put an older document over a newer
one reads all the library's authoring again from disk, which is too long to
hold a quit for. The setting and the file are read again under the compiler
lock, and the replacement is installed in that same hold, so a document
another writer installed meanwhile is judged as it is. A file that doesn't
allow web wallpapers is left as it is when the service last heard reads schema
8: it plays no web page from it (below). When the one heard is older than that
(it has no check of its own), the Workshop items the file names are looked up,
each by its own `project.json` (no scan of the Workshop), and the file is
replaced in the same way when one is a web page, a preset of one, or can't be
checked. The wait for that look ends at another Ctrl-C, or after two seconds,
even while a slow disk or a hung mount holds it, and what it hasn't reached
counts as a web page. A hand edit's parses share those same two seconds, and
one cut counts as allowing web wallpapers (or as naming what can't be checked).
The two seconds cover the judgements before the lock and under it together. When no status was heard at all, only a
web page the files confirm counts (or a preset whose base is one): an item that
can't be checked, such as one unsubscribed since or on a Steam library that
isn't mounted, leaves the file as it is. A service not heard from is either not
running, and its start compiles again (or refuses with exit 78 a kept file that
plays a web page), or current, and it refuses a page at launch itself. An older service
that is still running after an update can't load the document that plays
nothing, though: it would keep playing what it holds. So when one was heard and
the file names a web page its `project.json` confirms, the app tells it to quit,
the one request the end of a quit sends: it waits a second for the answer at
most, another Ctrl-C ends that wait, and none is sent once another Ctrl-C has
come. The service then stays stopped, and its next start runs the updated start
check below. For an item that can't be checked it isn't told to quit; it plays
what it holds until it is restarted, as it should be after an update
([installing.md](installing.md)), and the app's log says so. The log also says
what was sent and what came of it. What the replacement does is make sure
nothing started next plays the page: an older service can't load it, and the
updated one plays nothing until a publication gets through. A save of the setting still being written then is
waited for, as long as such a save can take (its locks, and the check of the
service that "Stop animations on battery" makes: about 70 seconds at most);
with none, nothing is waited for. The lock is waited for two seconds at most,
and only when there is a file to replace. Another Ctrl-C ends both waits at
once. What they leave is judged again before the process exits, which waits
for it anyway: a save the app stopped waiting for (that Ctrl-C, or its bound)
still lands, and a lock it didn't get is let go. Once that save has landed and
the app's own publication has ended, the setting and the file are read again
under the lock (waited for five seconds), and a file that allows web
wallpapers while the setting is off is replaced. That covers the app's own
publication that read the setting before the save landed, and so rightly, then,
installed a file that allows them. That later judgement's look at Workshop
items is bounded by two seconds, and its quit by one. A save that fails leaves the setting on, and
the file as it is. Only one more Ctrl-C after that, which ends the process at once, or
another program holding the lock past those five seconds, leaves such a file;
the next start's compile, or its check of a kept file (below), judges it.
At service start, a kept document (the one there already, kept because the
compile failed) is refused with exit 78 while the setting is off, if it plays
a web page, names a Workshop item whose type can't be checked, allows web
wallpapers, or isn't TOML the app can read (below). The unit isn't restarted
on 78; the companion tries it again every 300 s, which runs the compile again.

**A file the app reads differently from the service.** The service reads
`runtime.toml` as TOML 1.1; the app's checks read TOML 1.0, which refuses what
1.1 added: `\xHH` and `\e` escapes, an inline table spread over lines or with
a trailing comma, a time without seconds. Only a hand edit has any of them, and
one such as `"allow_web_wallpap\x65rs" = true` in `[settings]` is loaded by the
service as allowing web wallpapers. So the guard (for what the service's
status doesn't say), its replacement of the file when no service runs, and the
start's check of a kept file each count a file the app can't read as TOML 1.0
as one that allows web wallpapers, whether or not the key is in it. A plainly
malformed file is counted so too, since the two can't be told apart; the
service refuses it anyway. The end of a quit counts such a file so only when
the key may be in it: its name anywhere, or a quoted key that spells it with
escapes. One with neither can't allow web wallpapers under either TOML (the
service loads it allowing none), so the end of a quit judges it as a file that
allows none (above), by the Workshop items it names: those it can read off the
compiler's own lines, and otherwise ones that can't be checked. A file the
service can't read either (a symbolic link, not UTF-8, over 8 MiB) counts as
nothing, as before.

**When it is on and can't reach the service either.** A service that lists
schema 8 but isn't confirmed to read it (its installed units can't be
verified, say) gets the document without `allow_web_wallpapers`, so it plays
no web page. The app says "Web wallpapers aren't played until the wallpaper
service is confirmed to read them", and publishes again once it lists 8 and
again when it is started. One that predates 8 gets the same document and
plays the pages without a check, as 0.4.0 did.

**The service checks too.** linux-wallpaperengine reads `project.json` again
when it starts, so an item whose type changed to `web` after the compile (a
Workshop update) would play as one, and one that became an `application`
would rest on the engine's own parser alone. Whatever the document says, the
service reads the item's type just before it starts the engine, in every
place the engine looks for `--bg <id>` (above): a regular file of at most
256 KiB (never a link or a FIFO), strict UTF-8 and strict JSON (no `NaN` or
infinity, no number that overflows, no lone surrogate escape), nested to any
depth (once nesting first reaches 128 levels, everything after that point in the file is
checked for its structure only, not for numbers or escapes, shallow content that
follows the deep part included; the top level's own keys and `type`, and the keys of an
object that is the `type`, stay strict), the
type read exactly as the engine reads it (ASCII lower-case, nothing trimmed:
`" web "` is an unknown type, not `web`). The app's own engine starts (a
scene's still, the legacy play) use `workshop.engine_refusal`, the same
check; one case file, `service/tests/fixtures/engine-types/cases.json`, is run
against both. The app's scan, which only lists, stays forgiving. For a preset
it reads the base. A `scene` or a `video` may start; a `web` page only while the document
allows web wallpapers (`allow_web_wallpapers`, written only while the setting
is on); anything else is refused, as is an item it can't find or read, with
web wallpapers on or off: a scene in a Steam library on another drive is one,
since the engine never looks there. Each reason says what it is; only a web
page is put down to web wallpapers ("this configuration doesn't allow web
wallpapers", since a document can lack the key for other reasons than the
setting being off). The engine isn't started, and the entry is skipped, as the
owner put it: "skip to next, if it's in a playlist alone, then use the default playlist
in schedule". While motion can start, the service asks before anything of the
entry is shown, so its still doesn't go up, and the next entry of the same
playlist takes over. While motion is off (on battery with the battery policy
on, stopped, in a pause held from battery, or with motion turned off) nothing
is asked, since nothing would be started: the entry's still shows, as any
still-only entry's does, and the service asks once motion resumes. An item
whose type changes after that check is refused by the service's second check,
just before it starts the engine: its still is up for that moment, and the
service moves on at once. Both checks run before the engine is started. A
change after the second one, in the moment before the engine reads the file
itself, isn't caught. A playlist with nothing left that can play gives way
to the document's default playlist, then to All media; with nothing left at
all the display plays nothing, but for the known exceptions
([schema 8](runtime-config.md#nothing-to-play-schema-8)). It is never a taboo,
so nothing is saved against the item. The service's journal says what it
skipped and why, and so does its status
([fields](runtime-config.md#nothing-to-play-schema-8)): `refused` lists the
items, each with its reason, and a display that plays a stand-in names the
playlist it asked for (`stand_in_for`), so the app doesn't present the
stand-in as what was chosen. The app marks such an item "Can't play here",
with the service's reason. The pinned companion (0.3.0) reads neither field:
it names what plays with the reason that chose the refused playlist, so a
refused pick shows as "<stand-in> · your pick", with Resume schedule offered,
and nothing names the pick; refused items aren't mentioned at all (its
"being skipped" count is of taboo entries only). Showing either needs a
companion update. Each item is asked once per document,
in one pass along the playlist however many are refused: a reload (which
turning the setting on or off is) asks again. A document from before schema 8
has no such key, so it allows no web page. The check is the same while the
document does allow web wallpapers, except that a `web` page then passes.

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

**Read again before a preview is made.** The scan's word is as old as the
scan, and a Steam update can make a scene, a video or a preset's base a web
wallpaper without one. So while the setting is off, a Workshop item's
`project.json`, and a preset's base's where the scan found it, is read again
just before ffmpeg is given one of its files (`workshop.web_files_now`), and
the copies under the home folder, where linux-wallpaperengine looks, just
before it is given the item's id (`workshop.engine_refusal`). One that reads
`web`, or can't be read, gets nothing. Known gap, which does not open a web
page: a preset whose own `project.json` now names another base is judged by the
base the scan found. (A Workshop video's still is never a picture beside it, so
a scan has none to keep.) (A library folder you add that covers Steam's Workshop folder
no longer lists an item's files as your own: see "Workshop files in a library
folder" below.)

### Workshop files in a library folder

Workshop content is untrusted, so the walk of a library folder decides by place
(`workshop.folder_gate`, the test every other gate uses, `workshop.is_workshop_content`):

- a file under a `steamapps/workshop/content/431960` folder is not listed as
  local media at all: the Workshop scan lists the items it lists;
- a folder with a `project.json` is an item folder, and everything below it, at
  any depth up to the library folder, is the item's too; it fails closed. If any
  `project.json` from a file's folder up to the library folder is a `web`,
  `application` or unknown type, or can't be read, nothing there is listed, so a
  `project.json` nested inside a web item can't choose what is. Otherwise (every
  one a `video`) only the file the item folder's own `project.json` names is
  listed, so an item copied in by hand still works, and never its preview, its
  other files, or anything in its subfolders.

Whatever ffmpeg opens that somebody else wrote (a still's frame, a thumbnail, a
downloaded preview) is told what it may open (`decoding.input_options`):
`-protocol_whitelist file` (plus `pipe` for stdin) and a `-format_whitelist` of
picture and video containers. A playlist named like a picture or a video (an
HLS playlist, an `ffconcat` script) is therefore refused, not followed to other
files or the network.

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
still = "/.../pictures/y2k.png"   # the preset's still: one made for it or one you picked (Stills, below)
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

Owner decision ("auto/manual previews, skipping the W-Engine conf-included
preview"; stills only): a Workshop item's still, and with it the colours made
from it, comes only from one of two places, and never from a file the item
brought, because Workshop content is untrusted.

- **An automatic still Wall-in-One makes from the item itself**, stored in
  `Automatic Stills`: the engine's screenshot of a true `scene` (`<id>.png`), the
  engine's screenshot of a **preset of a true scene** (its base rendered with the
  preset's own values; `<id>-capture.png`), or ffmpeg's
  frame of a Workshop video (named by the video's path, like any video's).
- **A still you picked** in the app, saved in Wall-in-One's own pairing records.

A preset's capture shows what the service plays except for one thing: the
service puts the person's own property overrides (a scene's own settings, kept
on its pairing record) over the preset's values, and the capture passes the
preset's values only. So the still can differ from the wallpaper where the
person changed a property by hand.

So a picture named like a Workshop video (`foo.png` beside `foo.mp4`) is not its
still, and its `<video>.wall-in-one.json` sidecar is never read. Which files are
Workshop content is decided by place, not by label (`workshop.is_workshop_content`):
the resolved path under a `steamapps/workshop/content/431960` folder on any drive
(even inside a library root), a `project.json` beside the file, or the scan's
Workshop label. The preview shows on the library card, which is a picture of the
item and not its wallpaper.

**The one exception, a setting.** The engine can't screenshot a web page (it is
captured before it paints), so a **web wallpaper**, and a **preset of one**, have
no still of the first kind. (A preset of a true scene does: the engine renders
its base with its values, so it never depends on the author's preview or on the
setting. The type read before the engine starts is the base's, the id the
engine is given; an `application`, or a web base while web is off, is refused.) Owner decision: "a
separate toggle to use the built-in preview, only enablable when web is enabled,
like a sub-option". That is `web_wallpaper_previews` ("Use a web wallpaper's own
preview as its still", covering web wallpapers and presets of them; under Allow web wallpapers in Settings → Library, greyed
out while web wallpapers are off). The effective value is
`allow_web_wallpapers` AND `web_wallpaper_previews` (`Settings.preview_stills`),
and it is the only thing asked (`workshop.preview_still` /
`workshop.no_automatic_still`, which the scan, `stills.ensure`, the still queue,
the pairing pages and the compiler all use).

- **Effective: on.** Such an item's automatic still is `<id>.png`, drawn through
  ffmpeg from its own preview (a preset of a web wallpaper: from that
  wallpaper's), needs no display, and is made once. This is 0.4.1's behaviour; an
  existing preview-made still counts again.
- **Off, or web off.** Such an item has no automatic still, only one you pick.
  Until then it plays nothing (the compiler leaves it out, as for a video with
  none), the new interface's Library inspector says "No automatic still for this
  kind of wallpaper: choose one by hand", and there is no "Regenerate" (the
  classic pairing page just drops the row). A `<id>.png` already on disk
  is left alone and not used; a record naming it counts as no choice.
- Turning web off keeps the saved value; it just doesn't apply.
- A picked still works in every case. An item that can't play gets no still.

**Where the setting comes from.** A new profile has it off. A profile from 0.4.1
whose `settings.toml` has `allow_web_wallpapers = true` and no such key reads it
as on, so nothing drops out of rotation; with web off (or no file) an absent key
is off. An explicit value is always kept. The key is written only when it
differs from what its absence would mean, so an untouched 0.4.1 profile keeps its
bytes. Downgrading to 0.4.1 is therefore harmless in the usual case: 0.4.1 reads
settings with a key it doesn't know as read-only (changes disabled, nothing
lost), and it only meets this key in an unusual combination: preview stills off
while web is on, or on while web is off, which includes an upgraded profile where
you turn Allow web wallpapers off (the saved "on" is kept, so the key is written). 0.4.1 always uses the
preview for web pages and presets. The wallpaper service never reads a preview
or this setting, and is unchanged.

**Upgrading from 0.4.1.** Nothing deletes the preview-made `<id>.png` files: they
stay in `Automatic Stills`, hidden from the library as before. A preset of a true
scene never uses its `<id>.png` (it is never "already captured", whatever its
size, and a record naming it counts as no choice): it gets a fresh engine still
under its own name. Until Wall-in-One has taken that still the preset is left out
of the published `runtime.toml` (its card says "it has no still for playback to
show yet"), so it drops out of rotation after the update and comes back once its
capture exists. The automatic pass that takes it runs after every library scan the
app makes, so it happens the first time Wall-in-One is open after the update, and
needs linux-wallpaperengine installed and a display size it can measure (the
service's snapshot, or the window's monitors). The service's own pre-start compile
(`--service-startup-prepare`) never captures, so restarting the service alone does
not bring them back. For a web page or a preset of one, with the setting on (the default
for a profile that allowed web pages) the files are the stills they were. With it
off they are not used, a still you had picked is kept and used, and
the next compile leaves an item without one out of its playlists (a playlist left
with nothing playable is left out whole and the default falls back, as for any
item with no still). The Tidy-up page does not touch `Automatic Stills`, and a
Steam uninstall clears only Wall-in-One's records, never files
(`manage.discard_pairing_artifacts` has no caller). Under the 0.4.1 service,
which keeps its loaded `runtime.toml` until it is reloaded, an item plays as
before until the app's next publication. A library whose only playable items
were such compiles to "All media plays nothing"; while the service is not
confirmed to read that (schema 8), the publication is refused with the existing
"needs a newer wallpaper service" message and nothing is written. The wallpaper
service reads only a Workshop item's `project.json` type.

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
