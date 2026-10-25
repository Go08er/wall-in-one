# Runtime configuration contract

`wall-in-one-service` reads one input: a fully resolved TOML document written
by the Python application, plus, from 0.2.0, an optional sibling
[`runtime-overrides.toml`](#per-playlist-rotation-and-display-precedence-runtime-overridestoml).
Schemas `4`, `5`, `6`, `7` and `8` are intentionally strict. An
unknown version, unknown field, relative path, dangling playlist reference, or
kind-specific entry missing its motion source makes the service refuse to
start. It never falls back to the application's database or library files.

The compiler retains schema 4, byte for byte for unchanged inputs, while
`stop_animations_on_battery` is false. Enabling that option emits schema 5 with
`settings.stop_animations_on_battery = true`. The new service accepts both
versions; a schema-4 document cannot enable the new policy. This preserves
older upgrade journals and prevents an older service from silently ignoring
an enabled battery preference. A document with a Wallpaper Engine preset is
schema 6, which carries everything 5 does; only such a document is (see
below, after the entry fields). One with a renderer dial off its default is
schema 7, which carries everything 6 does ([renderer dials](#renderer-dials-schema-7)).
One with a playlist that plays nothing, or that allows web wallpapers, is
schema 8, which carries everything 7 does ([nothing to play](#nothing-to-play-schema-8)).

From 0.4.0 the compiler no longer writes `[renderer] niri_program`, which
0.3.x and earlier wrote when the service asked niri for displays. The service
accepts the key and ignores it, so a document an older build wrote still loads
and means the same. The schema numbers did not change: the version marks what
a reader must understand, and every reader since 0.4.0 reads both shapes. A
0.4 document is refused by a 0.3 service that has not been restarted yet (a
missing field, like any document it cannot load), and that service keeps its
last good configuration until it restarts; nothing is applied wrongly. Until
then the window, `ctl reload` and the health sync say that the running
service is older than the app and that restarting it finishes the update. A new
schema number would instead have sent every save through the live-service
check below, refusing all of them until the restart. See
[display discovery](display-discovery.md#runtimetoml).

The service advertises `supported_config_schemas = [4, 5, 6, 7, 8]` in status version 2.
It also reports `runtime_version` and `runtime_executable` so update inspection
can distinguish the responding executable from the package selected on disk.
Executable identity does not by itself acknowledge the intended configuration;
check `config_generation` and `config_path` too.
The app checks that capability before saving an enabled battery preference or
publishing schema 5 to its normal runtime path. An incompatible or unconfirmed
live service leaves the attempted write uncommitted. Offline preparation also
checks for a starting service or an unlinked live socket; detached exports do
not consult the session. This is not a lifecycle lock: finish the
[update procedure](updating.md) before allowing another old runtime to start.

The default path is `$XDG_STATE_HOME/wall-in-one/runtime.toml` (normally
`~/.local/state/wall-in-one/runtime.toml`). `--config PATH` overrides it. The
app writes a same-directory temporary file, flushes it, and renames it over the
destination. The service reloads only a complete new document; a failed reload
keeps the last valid configuration.

The packaged unit first runs `wall-in-one --service-startup-prepare`. This
headless path crosses deployed/legacy migration boundaries, reads the same app
settings and authoring stores as the GUI, scans the library, atomically
replaces an outdated document, and exits without importing GTK. Migration and
unresolved legacy failures remain fatal. Only an ordinary current-profile
compiler failure is softened, leaving the previous resolved document untouched
as a candidate for the next preflight.

That second preflight is `wall-in-one-service --check-config`. It uses Rust's
production loader without claiming a socket or renderer; only a consumable
supported document reaches the service main process. A valid supported
last-known-good document may therefore continue, while a missing, malformed,
or schema-2 candidate stops the unit once instead of entering a restart loop.

Every compiler finds the three `[renderer]` programs (`noctalia`,
`mpvpaper`, `linux-wallpaperengine`) the same way, so the GUI, a
`wall-in-one --write-config` from a terminal and the service's preflight
compile the same bytes from the same authoring state
([why this order](packaging.md#finding-a-renderer)). First come the
Nix profiles, in NixOS's login order (`~/.nix-profile/bin`,
`~/.local/state/nix/profile/bin`, `/etc/profiles/per-user/$USER/bin`,
`/nix/var/nix/profiles/default/bin`, `/run/current-system/sw/bin`), where the
NixOS and home-manager modules install the renderers, then `~/.local/bin`,
`/usr/local/bin`, `/usr/bin` and `/bin`, then every other directory on `PATH`,
in `PATH` order. A Nix store directory on `PATH` (a `nix shell`, a dev shell) is
never looked in: only that caller sees it, and nothing keeps it from garbage
collection.
The path is written where it was found, not resolved: `~/.nix-profile/bin/mpvpaper`
survives a profile switch and the collection of the old generation, and a
renderer update leaves the document's bytes alone. A program found nowhere
is written as `/usr/bin/<name>`, and the service reports it only when an
entry needs it; `wall_in_one.programs.find_program` tells the app whether it
was found, and the window starts its own scene stills by the same path. A
renderer in one of the standard places therefore compiles to the same path
from any caller. One installed only somewhere else (`/opt/...`, `~/bin`, a
custom prefix) is still found, but a terminal and the
service can disagree about it when their `PATH` differ there, and each run then
rewrites `runtime.toml`; a link in `~/.local/bin` or `/usr/local/bin` avoids
that.
`--write-config` names each file it wrote (`wrote:`) or removed (`removed:`),
or says `nothing changed`.

The compiler never refuses over a playlist that can't play. A playlist with
nothing to play (it is empty, or every wallpaper in it is missing or has no
still yet, as far as the scan could tell) is not written. All media is the
exception: it is always written, and when it has nothing to play it is
written as a playlist that plays nothing, with its reason
([schema 8](#nothing-to-play-schema-8)). A reference to one, or to a playlist that doesn't
exist, is left out and said, and everything else is published: such a
`default_playlist` is written as `all-media`, and a `[[schedules]]` rule or a
`[[displays]]` assignment that names one is not written, nor is that
display's entry in `runtime-overrides.toml`. So every reference in both files
names a playlist that is there, which is what the service checks. The app
says what it left out once and keeps it as a notice; `--write-config`,
`--service-startup-prepare` and `--sync-runtime-health` print one `warning:`
line for each and exit as for a clean compile. This holds only where nothing
that matters was unseen. A playlists, schedules, displays, pairings, favourites or pending
removals file that can't be read, is damaged or was saved by a newer version
still refuses the compile, naming the file, and the last document stays. So
does a playlist with nothing to play that the default, a rule or a display
names, when the library scan **could not look into** a place its files may
be in: a listing or stat of the folder (or one above it) failed, or a scan
limit stopped it (65536 entries examined, 4096 wallpapers, 8 levels deep).
"All media" counts, whether it is named, implicit (no default chosen), or what
plays because the chosen default was left out. For All media, "a place its files
may be in" is any library folder or, with favourites only, the folder of a
favourite (when no favourite is visible in the library, any library folder). The same goes for a video with no still only because a place its
still would be in could not be looked at (a stat or read of its sidecar, a
generated still, the proof of an adopted capture, meaning its metadata or the
Automatic Stills marker, or the folder holding them failed): a video with no
still at all is "no still yet" and is left out.
"Couldn't look" is never "nothing there". The refusal names the folder (or the video), and
the last document stays until the folder can be read (or, past a limit, the
library made smaller), the affected playlist has an entry that compiles, the
folder is taken out of the library, or the reference is removed. For an
implicit All media there is no reference to remove: choose a default
playlist, or remove the folder from the library. A library folder that is
absent, not a folder, or a symbolic link (an unmounted drive, or one removed
for good) was *looked at*: nothing usable is there, so such a playlist is left
out and said like any other that can't play, and the rest is published until
the drive comes back. A rule that is
turned off and names a playlist that can't be used is not written, and is
neither said nor a reason to refuse. Everything else the
compiler checks still refuses too: the size and count limits of the wire,
duplicate ids, text limits and renderer settings.

The compiler compares the complete generated bytes before replacing the file.
An unchanged rescan therefore does not restart a video or scene. For a changed
generation the app sends `reload` immediately; that request advances the
service's file-watcher fingerprint, so the same atomic rename is not applied a
second time. The one-second watcher remains the fallback for an app that wrote
the file but could not reach the socket.

Every Python publisher shares a private advisory compiler lock beside the
runtime document. The headless `--write-config` path acquires it before reading
settings, authoring stores, or the library and holds it through the atomic
rename; the GUI takes the same lock around rendering and installation. A
preflight holding an earlier snapshot publishes before a later GUI publication;
the lock serializes writers, not package versions. Lock acquisition is bounded
and fail-closed: an unsafe lock path or timeout leaves the last-known-good
runtime document untouched.

Saving `settings.toml` doesn't take the compiler lock, so a publication can
hold settings read before the person changed them. For
`allow_web_wallpapers` that matters: a publication that read it on must not
install a document that allows web wallpapers once it is off. So, just before
the atomic rename, a publication compiled for web wallpapers on takes the
settings file's own lock (the one every save of it takes, inside the compiler
lock), reads the setting again, and holds that lock through the rename. One
compiled for them off waits for no lock: its document neither allows web
wallpapers nor names a web page that can play, so installing it can't break
the rule. It reads the setting once without the lock, so that a snapshot from
before the person turned them on is compiled again for that; a turn-on saved
after that read publishes after it. When the setting no longer says what the
document was compiled for, the document is compiled again for what it says,
and nothing is written meanwhile. A settings file that can't be read is taken
as web wallpapers off, their default. A hand edit takes no lock; the next
publication or service start reads it.

Loading changed bytes is also not synonymous with restarting the current
wallpaper. The service compares the fully resolved effective output/entry set,
renderer settings, and dynamics setting across the reload. Editing an inactive
playlist or changing only a timer keeps the existing renderer child; changing
the active pairing, output, renderer options, or dynamics performs the normal
break-before-make hand-over.

That hand-over is transactional at the runtime boundary. If a decoded
generation cannot be applied, the service restores the prior configuration,
cursor and renderer settings, then reapplies the last-known-good entry. The
failed generation is never exposed through `status`. A rollback failure is
reported explicitly because the service can preserve the old configuration but
cannot truthfully promise that an external renderer or desktop helper recovered.

Both schemas contain `schema_version`, `config_generation`, `default_playlist`, `[settings]`,
`[renderer]`, `[[playlists]]`, `[[schedules]]`, and `[[displays]]`. Executable
and media paths are absolute. Live connector discovery needs no program: the
service asks the compositor over Wayland ([display discovery](display-discovery.md)). Every playlist entry has a stable `id`, `kind`,
absolute `still`, and inline `palette`. A video additionally has an absolute
`motion`; a scene has a numeric `scene_id`. An entry may also carry app-owned
`taboo = { reason, source }` metadata. That is a durable compatibility decision,
not runtime state: Rust never creates or edits it.

A Wallpaper Engine preset's scene entry carries two more optional keys, and no
other entry does:

```toml
scene_id = "2946165131"           # the preset itself: the entry's identity
scene_base = "2777242023"         # what linux-wallpaperengine renders (--bg)
properties = { bloom = "true", schemecolor = "0.500000 0.200000 1.000000" }
```

The service passes each property as `--set-property` and `NAME=VALUE`, two
arguments of their own, in name order. It refuses a document with more than
512 properties on an entry, a name outside `[A-Za-z0-9_][A-Za-z0-9_-]{0,63}`,
a value over 256 bytes or with control characters, a non-numeric
`scene_base`, or either key on a still or video, or in a document below
schema 6.

A document with a preset is **schema 6**, and only such a document: every
other one keeps schema 4 (or 5 with the battery option), byte for byte what
0.4.0 writes and reads. Schema 6 carries everything schema 5 does. A 0.4.0
service refuses a preset document whole (an unknown field), so before
publishing one the app asks the running service, as it does for schema 5;
when it doesn't list 6 in `supported_config_schemas` (or can't be asked, or
its loaded units can't be verified as current), the document is published
**without the presets**, and the app says once that they play after the
service is restarted and its units are current. Everything else keeps updating. A
service that isn't running gets the document with them, as an offline
preparation does for schema 5. See
[Wallpaper Engine items](wallpaper-engine.md#carrying-it-to-the-service-additive-optional-fields-schema-6).

`config_generation` is a 64-character lowercase SHA-256 token. The Python
compiler hashes its canonical semantic body before inserting this field, so
the identity is deterministic and non-recursive. Rust validates and echoes the
token but does not derive it or write it. Together with the normalized absolute
`config_path` in `status`, this lets an app client prove that an atomic runtime
snapshot came from the exact app-managed document it is about to map back to
authoring state. A hand-written standalone config may supply any canonical
token; it still needs no Python installation to run.

`[settings]` now carries `display_mode = "mirrored" | "independent"` and
`theme_source_connector`. A schedule may carry a connector target, and display
assignments remain app-authored configuration. Mirrored mode deliberately
omits connector-targeted rules and assignments from the resolved document, but
keeps them in the authoring stores so a dock setup survives a temporary mode
change. Independent mode compiles them and gives every live connector its own
route, cursor, shuffle bag, renderer and calendar winner. It requires a
non-empty designated colour-source connector; mirrored mode may preserve that
selection dormant.

That is software support, not a claim of physical multi-monitor validation.
Automated unit and process tests cover independent routing, schedules, cursors,
renderer ownership, transport and synthetic connector changes. The development
machine and the niri desktop VM each expose one output. The Sway VM has three
headless outputs and gives one its own video in independent mode, with
stand-in renderers. Different playlists on two physical monitors remain
unverified.

The renderer section carries `scene_fps`, which linux-wallpaperengine consumes
through its native `--fps N` option. `scene_scaling` is empty, `stretch`,
`fit`, or `fill`; `scene_clamp` is empty, `clamp`, `border`, or `repeat`.
Empty omits the corresponding launch flag and restores the packaged renderer's
default. These are closed enums rather than raw command-line fragments and are
global launch defaults; changing one performs a deliberate scene hand-over.
`video_hardware_decode` selects mpv's
`hwdec=auto` or `hwdec=no`. `video_interpolation` is `off`, `oversample`, or
`linear`. A non-off mode is applied only as the complete display-synchronised
set (`video-sync=display-resample`, `interpolation=yes`, a
`display-fps-override` from the compositor's live output snapshot, and the
selected `tscale`). If the selected outputs do
not all report one unambiguous refresh, playback stays unsmoothed rather than
using a guessed or partial configuration.

Video wallpapers still keep their source rate: mpv's `fps` filter runs after
decoding, so it is not presented as a decoder-performance control without
measurements proving an end-to-end benefit.

`video_muted` and `video_volume` are live mpv properties. An audio-only reload
updates active video children through IPC and changes later launch defaults
without a renderer hand-over. A scene's sound is its own, `scene_muted` and
`scene_volume` (linux-wallpaperengine's `--silent` or `--volume N`, never
both): the engine has no live control, and a video slider must not restart a
scene. While scenes are silent the compiler writes `scene_volume = 0`, as
every document did before scenes had a sound setting; `scene_pause_when_covered`
is now a setting too. All three keys are 0.4.0's, so they need no newer schema.

### Renderer dials (schema 7)

The legacy Python `--service` mode (installs without the Rust service) does
not apply the dials, though Settings shows them.

Every other dial ([settings](settings.md#renderer-dials),
[design](renderer-settings.md)) is a `[renderer]` key written **only when it
isn't its default**, with a default of its own on the service side, so a
document nobody tuned is byte for byte what it was:

```toml
video_fit = "fit"             # fill (panscan=1.0), fit (panscan=0.0), stretch (keepaspect=no)
video_speed = 1.50            # 0.25-4.00 -> speed=
video_brightness = -10        # video_contrast, video_saturation, video_gamma: -100..100
video_rotate = 90             # 0, 90, 180, 270 -> video-rotate=
video_deband = true           # deband=yes
video_scale = "lanczos"       # bilinear, spline36, lanczos, ewa_lanczos, mitchell -> scale=
video_hidden_trigger = "max"  # full, max -> mpvpaper --auto-mode, only with --auto-pause/--auto-stop
scene_pause_only_active = true  # --fullscreen-pause-only-active
scene_automute = false          # --noautomute
scene_audio_processing = false  # --no-audio-processing
scene_particles = false         # --disable-particles
scene_mouse = false             # --disable-mouse
scene_parallax = false          # --disable-parallax
```

A playlist entry may carry its own, in one table matching its kind, every
field optional; a scene's also takes `fps`, `scaling` and `clamp`:

```toml
video = { fit = "fit", speed = 1.50, rotate = 270 }
scene = { fps = 60, muted = false, volume = 40, particles = false }
```

The service refuses a value of the wrong type or out of its range, a field it
doesn't know (`deny_unknown_fields`: no option reaches mpv that isn't one of
these), a `video` table on anything but a video or a `scene` table on anything
but a scene, and any of it below schema 7. mpvpaper writes `-o` into a config
file with every space a new line, so each value is formatted from its type and
is one `name=value` token. A scene's own Wallpaper Engine values travel in
`properties` (a preset's, with the person's over them), schema 6's, on any
scene but a web page.

A change to one wallpaper's own changes only its entries. With independent
displays only the display showing it is handed over; mirrored displays are
all applied again, as for any change to what they show. A `[renderer]` change
reconfigures every renderer, as before.

Publishing works as for presets: a running service that isn't confirmed to
read 7 (it doesn't list it, or its installed units can't be verified) gets the
document without the dials (the global keys and every entry's table), and one
not confirmed for 6 without presets and a scene's own values too (that scene
still plays); the app says once that renderer settings apply once the service
is confirmed, and publishes again when a status newly lists 7. Saving them is
never refused, unlike the battery option. The health sync accepts any document
publishing could have made for the service it asked (`compiled_as_published`:
everything, without the dials, without both, and the one its status calls
for), so a document without the dials still matches its generation.

### Nothing to play (schema 8)

A playlist may play nothing. It has no entries and says why:

```toml
[[playlists]]
id = "all-media"
name = "All media"
nothing_to_play = "web wallpapers are off"
entries = []
```

The reason is required exactly when there are no entries, at most 128 bytes,
with no control characters. Below schema 8 a playlist with no entries is
refused by name, as before. (0.4.0's compiler wrote an empty All media
without `entries`, which no service could parse; it now writes this.) The
compiler writes it for All media only. Its reasons are `web wallpapers are
off` (web pages were all All media had), `none of the favourites can play`
and `nothing in the library can play`. The app says "Nothing to play: web
wallpapers are off." when that is why.

A display on such a playlist shows nothing of the service's: its renderer is
stopped, and Noctalia keeps the last still it was given (there is no way to
clear it). `play`, `toggle`, `next`, the rotation, a schedule and the file
poll can't change that; `next`, `previous` and `random` answer `nothing to
play: <reason>`, and `play` succeeds with the same words. It is a state, not a
fault: `last_error` stays empty. Status says it on the display row and, when
every connected display plays nothing, at the top:

| Field | Where | Meaning |
|---|---|---|
| `nothing_to_play` | top level, each display row | The reason; `null` while something plays (absent instead from a reply at its size limit, 0 below). |
| `entry_name` | each display row | `"Nothing to play: <reason>"` on such a row, `null` otherwise (absent instead at the limit, as `nothing_to_play`). The companion shows it as the display's title. |

On such a row `entry_id`, `kind` and `still` are empty and there is no
rotation countdown.

`[settings] allow_web_wallpapers = true` is schema 8 too, written only while
the setting is on. Absent, web wallpapers aren't allowed: before the service
starts linux-wallpaperengine for a scene, it reads that item's `project.json`
where the engine finds it, and refuses a web page or an item it can't classify
([wallpaper-engine.md](wallpaper-engine.md#web-wallpapers-are-off-until-allowed)).
A change of the setting starts every display again. Status says what the
loaded document allows:

| Field | Where | Meaning |
|---|---|---|
| `allow_web_wallpapers` | top level | `true` when the document the service holds has `allow_web_wallpapers = true`, `false` otherwise (no key, or a document below 8). Status is only answered with a document loaded; after a refused reload it is the one kept. Absent instead of `false` from a reply at its size limit (0 below), never while `true`; the app then judges the document the service holds itself, as for a service that doesn't say. Added in 0.4.1. |

**An entry the service refuses is skipped.** Such an entry is never started.
While motion can start, the service asks before its still goes up, so it isn't
shown either, and moves on to the next entry of the same playlist. With motion
off (stopped, on battery with the battery policy on, a pause held from
battery, `dynamics_enabled = false`) nothing is started, so nothing is asked:
a scene's still shows as before, and the next start of motion asks. An item
whose type changes between the service's read and the engine's own is refused
by the engine's start instead; its still is up until the service moves on, at
once. When every entry of the playlist a display is on was refused (a taboo
entry counts as unable to play too, but a playlist of taboo entries only keeps
its taboo rules), the display plays the document's `default_playlist`, then
`all-media`, each only when something of it can play. The display row then
names that stand-in as its `playlist_id`, as `entry_id` is its entry, and the
playlist asked for as `stand_in_for`; `assigned_playlist_id`, `route_source`
and `schedule_rule_id` stay what chose the refused one. In both modes a row's
`route_source` and `schedule_rule_id` are those of the decision it shows: from
a rule's boundary to the tick that applies it, the rule that still decides,
and for a pick, no rule. (The top-level `schedule` says what the schedule
chooses now.) A rule's window
starting or ending onto a playlist whose stand-in is already shown changes
nothing on screen, and a pick of a playlist known to be refused leaves the
stand-in's rotation where it is. When nothing can play at all, the display
plays nothing, exactly as above, with no `stand_in_for`: the reason is `this
configuration doesn't allow web wallpapers` when every item it was refused
(on its own playlist and on each that could stand in) is a web page, and `no
wallpaper here can start` otherwise. `next`, `previous`, `random` and the
rotation step over refused entries; with nothing else to move to, `next` and
`random` answer `no other wallpaper here can start`, which `last_error`
doesn't keep. A skip is no fault either: nothing is a taboo, nothing is saved,
and `last_error` and `renderer_failed` say nothing about it. The service's
journal names each skipped entry with its reason, and status lists the items.
The service remembers what it refused for the document it holds and asks each
item once, in one pass along a playlist however long the run of refused
entries; a reload, the file poll's included, forgets.

| Field | Where | Meaning |
|---|---|---|
| `stand_in_for` | each display row; top level | The playlist the display asked for (its pick, rule, assignment or the default) while `playlist_id` stands in for it. Absent otherwise, never `null`. At the top level, only when every connected display plays the same stand-in for the same playlist. Added in 0.4.1. |
| `refused` | top level | The Wallpaper Engine items the service refused in the document it holds, in the order it found them: `{"scene_id": "<Workshop id>", "web_page": true/false, "reason": "..."}`. `scene_id` is the item an entry renders (a preset's base). `web_page` is true for a web page refused because the document doesn't allow web wallpapers; every other is an item it can't find, read or play. The reason names the item and is at most 256 bytes. At most 16 rows; `[]` when none (absent instead at the limit, 0 below). Web pages are listed too: one that reaches the service is one the app's scan didn't call a page (its type changed after the compile), which the app's own web-off state doesn't explain. Added in 0.4.1. |
| `refused_omitted` | top level | How many more items were refused than `refused` lists; absent instead of `0` at the limit. Added in 0.4.1. |
| `stand_in_for_omitted` | top level | `true` when the reply left out at least one `stand_in_for`, a row's or the top level's, to fit its size limit (2 below). Absent otherwise, never `false`. A row without `stand_in_for` may then still play a stand-in. Added in 0.4.1. |
| `schedule_rule_id_omitted` | top level | `true` when the reply left at least one row's `schedule_rule_id` `null` to fit at all (6 below). Absent otherwise. A row whose `route_source` is `schedule` and whose `schedule_rule_id` is `null` may then have had one; a row that names one names its own, and `schedules[].in_force` still says which rules are in force. Added in 0.4.1. |
| `diagnostics_omitted` | top level | `true` when the reply left at least one `last_error`, a row's or the top level's, or `output_discovery_error` empty that wasn't (6 and 7 below). Absent otherwise. An empty one may then not mean "no error"; one that isn't empty is whole, and `renderer_failed` still says what failed. Added in 0.4.1. |

Neither status budget counts these, so the budget is 0.4.0's, and any
document 0.4.0 published still loads. (Counting them refused a document near
the limit that 0.4.0 published, and the unit then stopped on its own start
check.) They are fitted when the reply is built instead, and give way before
anything a 0.4.0 reply carries. When a reply would not fit, the service leaves
out, in order:

0. what says nothing: a row's `nothing_to_play` and `entry_name` while `null`,
   the top level's `nothing_to_play` while `null`, `allow_web_wallpapers` while
   `false`, `refused` while `[]` and `refused_omitted` while `0`, all added in
   0.4.1; and 0.4.0's own `loaded_overrides_sha256` and `overrides_ignored`
   while `null` (one of them always is). An absent one reads as that value,
   and no flag says so. On 128 rows the 0.4.1 ones took about 5.8 KB, the room
   of about 30 small taboo records that 0.4.0 sent. Without them a reply that
   carries what 0.4.0's carried is at least 21 bytes smaller than 0.4.0's on
   the wire (54 with no overrides file), though `supported_config_schemas`
   lists three more (0.4.0 loads no document that allows web wallpapers);
1. `refused` rows, the last listed first, each counted in `refused_omitted`;
2. every `stand_in_for`, on the rows and at the top level, and says so with
   `stand_in_for_omitted`, whose own bytes are counted in every reply measured
   without them. A row's `playlist_id` is then the stand-in, and what was asked
   for is left to what 0.4.0's fields say. When the flag is set, the app works
   it out from `assigned_playlist_id` (a display on its assignment or the
   default) and from `schedules[].playlist_id` of the row's `schedule_rule_id`
   (a rule); for a pick (`route_source` `manual`) nothing names the playlist.
   Mirrored, every row shares the pick's decision and the flag says one had a
   stand-in, so the app says the pick is stood in for without naming it;
   independent, it hedges;
3. as at 0.4.0, `outputs`;
4. `missing_renderers`' paths, then its rows;
5. the oldest taboo records.

Only a document at the schema's ceilings comes near it, and only one with
about as many display rows as there can be (64 connected and 64 more
assigned): a mirrored document as the app compiles it, with one row, at the
budget's edge, leaves more than 600 KB to spare. With all of these gone, what
is left is what the budget bounds, apart from what it doesn't count on the
rows. That isn't always within the limit: a rule's id of 256 quotes is about
1 KB on the wire, on each of up to 128 rows, so a document at the budget's
edge with such a rule in force had no reply small enough to send, at 0.4.0 as
well, and every status query got none. So, last, as little as the reply then
needs of:

6. the rows' `schedule_rule_id`s, left `null` (`schedule_rule_id_omitted`
   says so), and the rows' `last_error`s, left empty (`diagnostics_omitted`),
   together: the longest first, an error before a rule id as long, and a
   detached row's before a connected row's;
7. then the top level's `output_discovery_error`, then its `last_error`,
   left empty (`diagnostics_omitted`).

These are measured, not tried: with every one of them left out, the service
gives each back, the last first, wherever it still fits, so a short rule id
stays while a longer error will do, and no more goes than the room needs. A
flag goes with the last of its kind, and its bytes count toward that: what is
still out of a kind comes back whole where it fits beside the bytes its flag
then gives up (`schedule_rule_id_omitted` takes 34 bytes on the wire,
`diagnostics_omitted` 29). Whatever room the reply then has goes back to
what was left out before, in the reverse order: taboo records,
`missing_renderers` whole or without its paths, `outputs`, the stand-ins,
then `refused` rows (beside the stand-ins, or without them when they don't
fit). Nothing given back is ever kept at the expense of what comes before it,
so a reply may carry one taboo record more than 0.4.0's where 0.4.0 had room
only for `missing_renderers`' rows instead.

A document that 0.4.0 answered, and none of whose items the service refuses,
keeps every one of 6 and 7: the reply that carries what 0.4.0's carried is at
least 21 bytes smaller (0 above). One whose items it refuses is not a reply
0.4.0 sent, since 0.4.0 refused none: its rows play other entries, and it can
carry `refused_omitted` (21 bytes on the wire and its digits),
`stand_in_for_omitted` (30) and, for a display that plays nothing, why. That
can take more than the lean saving, which is 21 bytes with an overrides file
applied (54 without one), so where 0.4.0 had less to spare than the
difference, some of 6 and 7 go.

With every one of 6 and 7 left out, the reply carries the text the budget
counts (at most four times over on the wire, as the budget counts it), its
fixed keys and the structure of the playlists, rules and rows, and what
neither budget counts but every reply carries:

- the rows' timing fields, about 2.4 KB on 64 live rows, and their retry
  fields, about 7.7 KB when every row retries;
- the top level `schedule`'s second copy of a playlist's id and name, and of
  a rule's id;
- `theme_source`'s `configured` and `effective` connectors, up to 1 KB each
  on the wire;
- `config_path`, the document's path: at most 4,096 bytes, about 16 KB on the
  wire when it is all quotes;
- `runtime_executable`, the service's own path, the one string here that can
  carry control characters: up to about 28.6 KB on the wire.

At the ceilings (578 playlists, 512 rules, 128 rows), with every string the
budget counts made of quotes and backslashes, so that none is shorter on the
wire than the budget counts it, that reply has about 68 KB to spare (66 KB in
independent mode). With every one of the others above at its longest at once
it still has about 13 KB to spare (16 KB in independent mode). Errors have to
go only when they are long and full of quotes, such as an apply that failed
on every row on an entry whose id is 256 quotes.

Publishing steps down as for schemas 6 and 7. A running service that isn't confirmed
to read 8 gets the document without `allow_web_wallpapers`. One that predates
8 then plays web pages as 0.4.0 did, while the setting is on. One that lists 8
but isn't confirmed (its units can't be verified, or it didn't answer) refuses
every page, so the app says "Web wallpapers aren't played until the wallpaper
service is confirmed to read them", and `--write-config` prints a `warning:`
line; the app publishes again once such a service lists 8, and again when it
is started. A playlist that plays nothing
has no older shape, so for such a service that document is refused and
nothing is written. While web wallpapers are off, the app then ends the page
the service still holds (see below). The health sync tries the stepped-down
variants too.

**When a publication fails while web wallpapers are off.** The setting is
saved first, so a compile or write that fails, or a reload the service
refuses, leaves the service on what it holds: the file on disk, or a text a
publication replaced since the service last confirmed a reload (a reload
confirms only the texts replaced before it was sent). The app asks the
service's status, and judges the one whose `config_generation` it reports,
or all of them when it reports none of theirs. The file on disk is judged
too when the service doesn't hold it, since the service's own check of the
file loads it within about a second (one written by something that doesn't
reload the service, such as a hand-run `wall-in-one --write-config`). It
stops playback only when one of them lets a page play: a document with
`allow_web_wallpapers = true` (as the service's status says of the one it
holds, where it says it), whatever it names; or, for a service not confirmed
to read 8, which has no check of its own, a document that plays a web page or
an item the scan can't classify, or one the app no longer knows. A service
that reads 8 and holds a document that doesn't allow web wallpapers refuses
every page itself, and is left alone while the file on disk doesn't allow
them either. Otherwise a
service that reads 8 is
given a document that plays nothing anywhere and reloads it: unlike a `stop`,
nothing it is asked afterwards brings the page back. If it was playing, it is
told to play again, so that turning web wallpapers on later plays as before;
nothing can start from that document. A service that can't
load it (one older than the app, still running after an update, or one whose
status can't be read) is told to
quit, as `wall-in-one ctl quit` does. It exits cleanly, so systemd leaves it
stopped, and whatever starts it next (the companion, or `systemctl --user
start wall-in-one`) runs the updated service through the start guard below.
The app says which it did. This holds for the window, `--service` and the
health lane.

```toml
palette = { kind = "adaptive", scheme = "m3-tonal-spot", mode = "auto" }
palette = { kind = "named", source = "community", name = "catppuccin", mode = "dark" }
palette = { kind = "keep", mode = "keep" }
taboo = { reason = "scene crashed linux-wallpaperengine", source = "renderer-crash" }
```

The allowed modes are `keep`, `dark`, `light`, and `auto`; named sources are
`builtin`, `community`, and `custom`. The service does not read palette files.
It forwards the already-selected source and name to Noctalia.

Schedule months use `1..12`; weekdays use Monday=`0` through Sunday=`6`.
Times are local `HH:MM`, inclusive at the start and exclusive at the end. An
end earlier than its start wraps midnight. Empty month/weekday lists and an
absent time window mean “any”. Rules stay ordered because the last match wins.

This file contains configuration only. Cursor, playing/paused/stopped state,
shuffle order, the manual playlist and cycle overrides, renderer PIDs, and
current status are runtime state and never get written into it. The configured
`cycle_enabled` is the session default. `cycle on|off` overrides it until the
service exits or `cycle default` drops the override, in either display mode and
across a reload that changes it.

The cycle interval is residency time for the wallpaper currently in force, not
wall-clock debt carried by the one it replaced. A successful manual or scheduled
playlist transition, next/previous/random action, or resume starts a fresh
interval. In particular, a schedule change and a cycle advancement cannot both
happen in the same tick.

Shuffle uses a fresh bag per playlist round. Every eligible entry appears once,
the next bag is reshuffled without repeating the seam entry, and `previous`
retraces actual playback rather than walking backwards through an unrelated
permutation. `next` after `previous` follows the actual forward branch first.
Both history directions are capped at 128 indexes per playlist, so a long-lived
daemon does not accumulate an unbounded session log.

An automatic startup, schedule or cycle hand-over which cannot apply gets three
attempts total, two seconds apart. Schedule and cycle transitions restore the
prior selection and last-known-good wallpaper between attempts. Startup has no
prior selection, so its paired still remains visible; after the third failure
the route advances to another usable entry. In both cases the attributable
playlist/entry becomes taboo for later automatic selection for the rest of the
service session. Rejected socket input and failed reloads preserve a promised
retry; an accepted explicit command or successful reload supersedes it. A
disconnected route cancels its pending candidate and evaluates the current
clock when it reconnects. This is distinct from a child renderer exit: a
renderer which started and later died is never automatically restarted, and a
linux-wallpaperengine scene crash becomes taboo immediately. A renderer program
that could not be started at all (nothing at its path, or not executable) is
neither: the entry's still is shown, the hand-over succeeds, and status reports
the program in `missing_renderers`, never the entry
([missing components](missing-components.md#the-service-without-a-renderer)).

Display assignments are the baseline when no schedule rule matches. A matching
schedule rule overrides assignments for its window, and a manual
`playlist-use` overrides both until `schedule-follow`. From 0.2.0 a display
may opt in to its own playlist beating global rules; see
[Per-playlist rotation and display precedence](#per-playlist-rotation-and-display-precedence-runtime-overridestoml). Each connector owns its
own mpvpaper or linux-wallpaperengine child; switching one does not stop a
renderer owned for another connector. Noctalia's colour palette is global, so
one user-selected connector is the colour source. If it is detached, the
lexically first live connector is the temporary source while the saved choice
is preserved. A non-source targeted change never recolours the shell. Crash
fallback reasserts the palette only when the failed renderer was the effective
colour source.

Assignments need not enumerate every monitor. At apply time the service takes
one live output snapshot from the compositor; a connected connector with no explicit
assignment follows `default_playlist`. That same snapshot feeds renderer
refresh and scene-target decisions, avoiding duplicate compositor queries.
The service checks for connector changes every five seconds and reapplies only
after a real hot-plug change. A transient discovery failure retains the last
successful snapshot, falling back to the explicit assignment list before the
first success. `status.output_discovery_error` reports that degraded state.
Only enabled outputs are connectors here; a head the compositor reports switched
off is listed in `status.outputs` and is never a target, as long as another head
is on. With every head switched off the snapshot is a discovery failure like the
ones above: the last successful snapshot is kept, and
`status.output_discovery_error` says the compositor reported no enabled outputs.

`status_version = 2` retains top-level compatibility fields for compact bar
clients and also returns a `displays` array with the effective playlist and
entry for the union of live connectors and configured assignments (`ALL` in
mirrored mode). A configured but detached row has `connected = false` and does
not claim active motion. When independent
routes disagree, top-level `source`, `playback_state`, `cycle_source`, or
`shuffle_source` can be `mixed`; their per-display fields are authoritative.
Several different playlists report `Multiple displays` with no singular entry
instead of naming a wallpaper that is not actually the common state.
It also returns a `playlists` inventory so a runtime-only client can switch
lists without opening the Python authoring socket. Each assigned playlist owns
an independent cursor and shuffle order; a baseline playlist with more entries
than the global default is not truncated by the default's cursor.

The companion revision paired with this application requires status version 2,
performs the direct-launch health hand-off, and permits the runtime's bounded
45-second action. Companion revisions through `a5e23c9` understand only the
compatibility summary and must not be combined with this application release.
The original v0.1.3 release pinned companion v0.1.2 at
`c154c162fd650567ef8eda5d0e6d875b2b635a21`, including direct-start migration
preflight and battery-status display. App v0.1.4 pins companion v0.1.3 in
`flake.lock`, retaining that contract and adding temporary-status handling.
The earlier
`a17eb70` companion provides only the base contract, not those newer safeguards.
See [companion compatibility](migrating.md#companion-noctalia-plugin-compatibility)
for the new requirements and [updating an existing installation](updating.md)
for loaded/running service checks. A machine still upgrading from an older
companion must complete the app's schema-4 cutover before loading the strict
client.

The same atomic snapshot carries `schedule` (whether the calendar is being
followed, the playlist it currently selects, and the last matching rule), the
complete read-only `schedules` array, and each display's configured assignment
alongside its currently effective playlist. `assignment_source` distinguishes
an `explicit` assignment from a connector following the `default` playlist.
Each display row also carries its route source, manual/schedule-rule identity,
three-state playback, Cycle and Shuffle values/defaults/sources, and
route-attributed retry or renderer failure. `theme_source` names both the
configured palette connector and the effective live fallback, since Noctalia
still has only one shell-wide palette.
There are deliberately no separate
runtime listing verbs: combining independent replies could mix two config
generations, while display assignment itself belongs only in this generated
file. Runtime status replies are bounded at 1 MiB; runtime requests and the
Python authoring protocol remain bounded at 64 KiB.

Every display and the top-level summary also report `motion_active`. If an
owned renderer exits, the service explicitly reaps its process group, reapplies
that entry's still and records an attributed `last_error`. That automatic
failure fallback is not a playable mode: explicit Next, Previous, Random,
playlist and schedule choices skip taboo entries, and Play refuses a taboo
current entry. If every candidate is taboo, the command fails without changing
the selection. Status keeps `renderer_failed = true`, `motion_active = false`,
and the attributable reason while a crash fallback remains visible. Its durable
marker is part of the app-owned Pairing metadata and is removed only with an
explicit Delete/Trash or confirmed external uninstall, so reinstalling the
media starts clean without turning a temporarily missing drive into recovery.
`entry_taboo` is reported both per display and at the top level (where it means
any connected current route). It is complete playback-safety truth rather than
a diagnostic list entry, so the GUI still removes Play when the matching
`taboo_entries` record is older than the bounded inventory or the crash was
observed by an equivalent occurrence on another display.

The same snapshot reports `automatic_retry` while a candidate is waiting,
along with `taboo_entries` containing stable playlist/entry identities, kind,
scene id where applicable, attributable reason, source, and `durable` state.
`durable = true` means that exact finding was loaded from the app-owned config;
`false` is still session-only and needs acknowledgement. The most recent 64
records fit in the bounded atomic reply; `taboo_entries_omitted` counts older
records while the complete loaded set remains skipped. An omitted record is
intentionally not a recovery signal.

This inventory is also the only persistence hand-off. The service is read-only:
it never edits this config or an auxiliary blacklist. When the app reconnects,
it maps stable playlist/entry ids back to the media pairing, persists one health
marker for that wallpaper, and compiles the marker into every occurrence. A
service restart seeds its skip set from those optional fields. Removing the
marker in the app compiles their absence; reload then clears only a previously
durable record before retrying. An unrelated reload cannot clear a newly found,
session-only failure which the app has not consumed yet.

The GUI performs that hand-off while open. For daemon-and-bar-only sessions,
`wall-in-one --sync-runtime-health` performs the same operation once without
importing GTK: one status read, one app-owned authoring update, one atomic
compile, and a reload only when bytes changed. Runtime absence or malformed
status fails before an authoring read; a compiler failure preserves the prior
runtime document. A bounded snapshot with an omitted count is never interpreted
as permission to clear an existing marker. Rust neither spawns this command nor
writes its result.

The packaged `wall-in-one-health-sync.timer` runs the command 30 seconds after
activation and then 30 seconds after the previous one-shot becomes inactive.
That completion-based schedule prevents overlap. It has no persistent catch-up
and is both `PartOf=` and `BindsTo=` the Rust service, so it stops on explicit
stop, crash, and failed activation. Exit 3 (runtime disappeared) is an expected
one-shot result; routine stdout is suppressed, but stderr stays journaled. The
main service's final no-reload sync is bounded by GNU `timeout` to two seconds.

On the 2026-08-24 x86_64 Nix test host, the packaged no-report path took 0.15 s
elapsed cold (0.11 s user + 0.01 s system, 32,508 KiB peak RSS) and five warm
runs each took 0.14 s (0.10–0.11 s user, 0.01–0.02 s system, 32,296–32,556 KiB
peak RSS). This is a short-lived Python process, not resident daemon memory;
at the minimum cadence its measured CPU time averages about 0.4% of one core.
The no-report regression proves it returns before `config.load_strict()` or a
library/session scan. The Rust RSS contract is measured separately and is not
changed by this timer. A direct non-systemd launch has no in-repo lifetime
manager and still requires its integration caller to invoke the bridge.

The top-level snapshot reports `playback_state` as `playing`, `paused`, or
`stopped`; the older `paused` boolean remains for compatibility and `stopped`
is also explicit. It reports effective `cycle_enabled`, authored
`cycle_default`, and `cycle_source` (`config` or `manual`) so clients never have
to infer which switch is actually in force.

## Battery policy status

Status version 2 keeps manual `playback_state` unchanged and adds these fields:

| Field | Meaning |
| --- | --- |
| `power_source` | `ac`, `battery`, or `unknown`; unknown is not treated as AC. |
| `power_available` | Whether the current power observation is usable. |
| `stop_animations_on_battery` | The runtime's currently applied saved option. |
| `animations_inhibited` | Automatic battery policy is currently suppressing motion. |
| `animation_inhibition_reason` | Empty, `battery`, or `power-unavailable` for a retained restriction. |

`motion_active` and per-display rows describe actual renderer activity.
Clients should show battery inhibition separately from manual Pause/Stop and
renderer errors. An older version-2 runtime may omit all power fields; that
does not prove automatic battery handling is available.

The runtime observes UPower without the GUI or companion. Known battery at
startup prevents initial motion. Unplugging releases owned video/scene
renderers and preserves route intent, selections, schedules, and cycle timers.
Plugging in recomputes eligible motion from the latest choices. Manually paused
routes whose renderer was released remain static until Play. Intentional
inhibition never records a media failure.

On unavailable initial power information, playback retains its ordinary
behavior. If an observation is lost after confirmed battery, the runtime
retains inhibition until confirmed AC or the option is disabled. See
[the user-facing settings guide](settings.md#battery-animation-control).

## Route and rotation timing

Status stays version 2. From 0.2.0 every display row also says when its
wallpaper next changes on its own. All six fields are appended after the
released ones, every one is `null` when there is nothing to report, and an
older service simply omits them.

| Field | Meaning |
| --- | --- |
| `route_change_at` | The local time, `YYYY-MM-DDTHH:MM:SS`, the clock will show when this display's automatic playlist or `route_source` next changes: at a schedule boundary, or when the clock itself jumps across a daylight-saving change. |
| `route_change_in_s` | Real seconds until then, rounded up. |
| `next_cycle_at` | The local time the clock will show when the rotation next advances: `next_cycle_in_s` added to the real current time and read in the local zone. |
| `next_cycle_in_s` | Whole seconds until then, rounded up; `0` when it is due. Elapsed time, and the authority for the rotation. |
| `until` | `route_change_at` as local `HH:MM`, only when it is less than a day away. |
| `next_change_in_s` | The same value as `next_cycle_in_s`. |

`until` and `next_change_in_s` are the names the Noctalia companion reads from
`wall-in-one ctl status`; the companion shows "Not changing" whenever
`cycle_enabled` is false, whatever `next_change_in_s` says.

The route change uses the same winner status reports in `route_source`: in
mirrored mode the last matching global rule, else the display's assignment,
else the default playlist; in independent mode the display's own precedence,
including `beats_global_rules`. It is reported while the route is paused,
because the schedule still moves a paused route, and is `null`:

- while a manual `playlist-use` holds (the schedule is not consulted until
  `schedule-follow`);
- for a configured display that is not connected;
- when no boundary within eight days changes the winner. A handover between
  two rules choosing the same playlist with the same source is not a change.
  Only midnight, each enabled rule's start and end minute, and the clock's
  own daylight-saving jumps are examined.
  One status reply spends at most two million rule checks on these searches;
  hundreds of rules on dozens of displays can exhaust that, and the displays
  left over report `null`.

At the change the winner plays on every display it moves: a mirrored rule
that names the default playlist moves a display assigned to another playlist
onto the default, and back when it ends, though the playlist in force is the
default either way.

The cycle deadline is `null` when the route is paused, does not cycle, or is
not connected. Stopped routes keep rotating stills, so they keep a deadline.
Mirrored mode has one timer, so every mirrored row carries the same deadline.
A successful rotation, schedule change or explicit command restarts it.

Every field holds across a daylight-saving change. The seconds are real
elapsed time, and the wall-clock fields are what the local clock will show
then.

- The schedule is written and read in local wall-clock time, and the search
  follows the clock's readings in the order they happen. After clocks go
  forward, a boundary in the skipped hour (02:30 on a spring change) never
  shows, so the schedule first sees it, and status reports it, at 03:00; a
  window lying wholly inside the gap changes nothing that day. When clocks go
  back, the rewind is itself a boundary: at the first 01:55, a 01:30–02:30
  window matches, but five minutes later the clock reads 01:00, where it no
  longer does, so status reports a change at 01:00, five minutes away, not at
  02:30. The repeated hour's boundaries then come round a second time.
- Rotation runs on elapsed time, so `next_cycle_at` is `next_cycle_in_s` from
  now as the local clock will then read: ten minutes after 01:55 on a spring
  change is 03:05, and ten minutes after the first 01:55 on an autumn change is
  01:05.

In the repeated hour a reading such as 01:00 names two moments; the one meant
is the next, which `route_change_in_s` makes explicit.

Each status reply reads the local zone afresh, as the schedule does on every
tick. The zone is re-read at most once a second, from `TZ` when the service's
environment sets it, otherwise when `/etc/localtime` itself is replaced, as
changing the system time zone does. A tzdata update that leaves
`/etc/localtime` alone, or a `TZ` naming a zone whose file changes, takes
effect when the service next starts, for the schedule and status alike.

## Per-playlist rotation and display precedence (`runtime-overrides.toml`)

A playlist's own interval and shuffle, and a display whose own playlist beats
global schedule rules, never appear in `runtime.toml`. With them in use,
`runtime.toml` is byte for byte what the same profile compiles with them
cleared, so every released service keeps loading it. They live in a sibling
file in the same directory, found by name and never referenced from
`runtime.toml`:

```toml
# runtime-overrides.toml, beside runtime.toml
schema_version = 1

[[playlists]]
id = "8f3c2a1b9d4e6f70"          # a playlist id in runtime.toml
cycle_interval_seconds = 120     # 5..86400, like settings.cycle_interval_seconds
shuffle = true

[[displays]]
connector = "DP-1"               # an assignment in runtime.toml
beats_global_rules = true
```

Every key is optional except `schema_version`, `id` and `connector`, and the
file rejects unknown keys like `runtime.toml` does. The app writes it only
while at least one of these is in use and removes it, durably, when the last
one is cleared, so a profile that uses neither has exactly the files Release
1 has. It publishes the overrides before `runtime.toml`, under the same
compiler lock, and rewrites each file only when its bytes change; a change to
either is a change, and the app sends `reload`.

Each file is replaced atomically on its own; the pair is not. What a save
guarantees:

- A `runtime.toml` the running service could not load is refused before
  either file is written.
- If writing `runtime.toml` fails after the overrides were published, the
  app puts the previous overrides back before it reports the error: the same
  bytes through a fresh temporary, flushed and renamed, or, when there was
  none, it removes the new file. The pair on disk is the previous one. If
  that restore fails too, the error says so: the two files may disagree
  until the next successful save.
- If a file was replaced but syncing its folder then failed, it is already
  what every reader sees, so nothing is put back: the save goes on to the
  new pair and keeps it. The app treats that pair as published, sends
  `reload` for it like any other change, and says the change may not
  survive a power loss.
- Between the two writes, and after a crash between them, the new overrides
  sit beside the previous `runtime.toml`. The service loads that pair (see
  below) until `runtime.toml` lands or the next save replaces it.

The service reads it after `runtime.toml`, at start, on `reload` and when the
watcher sees either file change, appear or disappear; `--check-config`
validates both. A missing file is the ordinary case. The service first reads
only `schema_version`, ignoring every other key:

- A schema it supports (`1`) is then parsed strictly. A malformed file of
  that schema is a config error like a malformed `runtime.toml` (a failed
  reload keeps the running configuration), because it claims to be this
  release's format.
- Any other integer means a newer release wrote the file. The service runs on
  `runtime.toml` alone, logs one journal line, reports `overrides_ignored`
  (for example `unsupported schema_version 2`) in status, and
  `--check-config` passes with that warning. A file that is not TOML or names
  no integer schema is a config error.

So the rule for later releases is: any addition to this file bumps its
`schema_version`, and services ignore versions they do not know. The app
side matches it: a compiler never rewrites or removes an overrides file that
declares a newer schema than it writes, so rolling forward again finds the
file as the newer release left it.

An entry naming a playlist or connector that `runtime.toml` lacks is skipped
with a journal line rather than refused, which covers the moment between the
two writes. An entry naming one both documents have applies to the older
document for that moment: the watcher checks both files once a second, so
it can reload on the overrides alone and again when `runtime.toml` lands.
Each reload applies the current entry again.

The service resolves shuffle as: a live `shuffle on|off` override, else the
playlist's own `shuffle`, else `settings.shuffle`; `shuffle default` returns
to the playlist's value. The interval of the playlist a route is playing
replaces `settings.cycle_interval_seconds` for that route, and a route that
changes playlist (manual choice, schedule, reload, hot-plug) builds its
cursor with the new playlist's shuffle. Mirrored mode has one timer: it uses
the interval every effective playlist agrees on, else the global one. (The
app compiles one effective playlist in mirrored mode, so this only matters
for a hand-written mirrored document with display assignments.)

Precedence for one connector's automatic route (a manual `playlist-use`
always wins first):

| `beats_global_rules` | rule for this connector matches | global rule matches | winner |
| --- | --- | --- | --- |
| off (absent) | any | any | last matching rule visible to it (global or its own, in authored order), else its assignment, else `default_playlist` |
| on | yes | any | the last matching rule aimed at this connector |
| on | no | yes | its own assignment; the global rule does not apply here |
| on | no | no | its own assignment |

Rules aimed at a display always beat its playlist; only untargeted (global)
rules lose to it. Mirrored mode has one route, so an opt-in stays dormant
there, like the assignment itself; the app leaves it out of the file.

Status stays version 2 and only gains fields. `route_source` keeps its four
values: a display whose own playlist wins reports `assignment`, as it always
did. A beaten global rule is not `selected` or `in_force` for that display
(it still is where another display follows it).

| Field | Where | Meaning |
| --- | --- | --- |
| `supported_override_schemas` | top level | `[1]`. A service without this field never reads `runtime-overrides.toml`. |
| `loaded_overrides_sha256` | top level | SHA-256 of the applied overrides file; `null` without one (absent instead from a reply at its size limit; see 0 under the status's size limit above). |
| `overrides_ignored` | top level | Why a present overrides file is not applied (a newer schema); `null` otherwise (absent instead at the limit, as `loaded_overrides_sha256`). |
| `cycle_interval_seconds` | top level | The summary route's interval; `null` when independent routes disagree. |
| `cycle_interval_seconds` | each display row | The interval of the playlist the route is playing. |
| `beats_global_rules` | each display row | The opt-in as it applies to that route; `false` in mirrored mode. |

`shuffle_default` reports the saved default the route would use without a
manual override: the playlist's own `shuffle` when set, else
`settings.shuffle`.

The authoring side keeps these in `playlists.json` and `displays.json`
version 2, written only once one is used (see
[Format versions](library.md#format-versions)).

### An updated app with an older service

Saving one of these never asks the running service anything. A service that
predates `runtime-overrides.toml` never opens it, so with the new app and the
old service still running (between an update and the service restart) the
setting is saved, `runtime.toml` stays loadable, and the setting starts
applying once the updated service runs; a page can tell from
`supported_override_schemas` and say so.

This is deliberately unlike the battery option, which lives in `runtime.toml`
itself and so needs schema 5, refused while an older service runs. Putting
these two in `runtime.toml` would have meant a schema 6 that every released
service refuses to load, leaving a service that has not been restarted yet
unable to take it.
