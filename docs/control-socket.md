# Control sockets

Every verb is one line of JSON over a mode-0600 socket. With a valid
`$XDG_RUNTIME_DIR`, runtime commands go directly to
`$XDG_RUNTIME_DIR/wall-in-one-runtime.sock`; authoring and provider commands go
to the Python app's `wall-in-one.sock`. Without a runtime directory, both the
Rust service and Python client use
`$XDG_STATE_HOME/wall-in-one/wall-in-one-runtime.sock` for the runtime endpoint.
`wall-in-one ctl` routes between the two services, so the Noctalia plugin needs
no socket code in Luau.

```console
$ wall-in-one ctl next
$ wall-in-one ctl previous
$ wall-in-one ctl random
$ wall-in-one ctl play|pause|stop|toggle
$ wall-in-one ctl cycle on|off|default
$ wall-in-one ctl shuffle on|off|default
$ wall-in-one ctl playlist-use Evening
$ wall-in-one ctl schedule-follow
$ wall-in-one ctl on DP-1 playlist-use Evening photos
$ wall-in-one ctl on DP-1 schedule-follow
$ wall-in-one ctl on DP-1 play|pause|stop|toggle
$ wall-in-one ctl on DP-1 previous|next|random
$ wall-in-one ctl on DP-1 cycle|shuffle on|off|default
$ wall-in-one ctl reload
$ wall-in-one ctl status
$ wall-in-one ctl providers
$ wall-in-one ctl search wallhaven aurora borealis
$ wall-in-one ctl download motionbgs <identifier> [hd|4k]
$ wall-in-one ctl list [everything|stills|videos|favourites] [query]
$ wall-in-one ctl select /path/to/wallpaper.png
$ wall-in-one ctl favourites
$ wall-in-one ctl favourite /path/to/wallpaper.png
$ wall-in-one ctl unfavourite /path/to/wallpaper.png
$ wall-in-one ctl remove /path/to/wallpaper.png
$ wall-in-one ctl pairing /path/to/wallpaper.png
$ wall-in-one ctl still /path/to/wallpaper.mp4 :: /path/to/picture.png
$ wall-in-one ctl still /path/to/wallpaper.mp4 default
$ wall-in-one ctl palette /path/to/wallpaper.png :: builtin:Nord
$ wall-in-one ctl reset-pairing /path/to/wallpaper.png
$ wall-in-one ctl playlists [name]
$ wall-in-one ctl playlist-new Evening
$ wall-in-one ctl playlist-add Evening /path/to/wallpaper.png
$ wall-in-one ctl playlist-remove Evening <entry-id>
$ wall-in-one ctl playlist-use Evening
$ wall-in-one ctl schedule-follow
$ wall-in-one ctl playlist-delete Evening
$ wall-in-one ctl schedule
$ wall-in-one ctl open browse|media|pairings|playlists|schedules|displays|settings
$ wall-in-one ctl schedule-add Evening days=sat,sun from=22:00 to=06:00
$ wall-in-one ctl schedule-remove <rule-id>
$ wall-in-one ctl schedule-name <rule-id> Frog day
$ wall-in-one ctl schedule-name <rule-id>
$ wall-in-one ctl quit
```

`quit` is intentional Rust-service shutdown. Merely closing the Python window
does not stop rotation or schedules. `pause` freezes a resident renderer and
also holds the playlist cursor; `stop` tears motion down while leaving the
paired still visible, and `play` resumes either state. A stopped runtime can
still rotate through static pairings when cycle remains on. `cycle on|off` is a
session override and `cycle default` returns to the authored setting.

`status` is JSON describing the active playlist and entry, whether the source
is `manual`, `schedule`, or (for divergent independent routes) `mixed`, the
explicit `playing` / `paused` / `stopped` / `mixed` state,
effective cycle value and its `config` or `manual` source, shuffle state, the
playlist inventory, each display's effective entry, and the last renderer
error. Top-level `renderer_failed` is true when the mirrored renderer is
degraded, or when any currently connected independent route is degraded;
detached route history remains visible in its row without poisoning the live
aggregate. The same snapshot includes every schedule rule, the rule currently
selected by the calendar, and configured versus effective display assignments,
so a bar menu does not need the Python app. `config_generation` and the
normalized absolute `config_path` identify the exact compiled document behind
the snapshot. Each taboo row's `durable` flag distinguishes app-acknowledged
health from a session-only finding. These are read-only; assignment and rule
edits still belong in the app-generated configuration. An open GUI is not
accepted as a substitute for runtime status: exit code 3 still means automation
is not running.

Each display row identifies `assignment_source` as `explicit` or `default`.
Status version 2 makes each connected display row authoritative for its route:
manual/schedule source and effective rule, three-state playback, mode defaults
and overrides, and route-attributed renderer/retry diagnostics. Top-level mode
values can say `mixed`; clients must not render their compatibility boolean as
“off” in that case. `theme_source` distinguishes the saved colour connector
from the effective live fallback.
Newer status-version-2 services also report observed power and automatic
animation inhibition. These fields do not replace manual playback state: a
route may be `playing` while battery control keeps its paired still on screen.
See [battery policy status](runtime-config.md#battery-policy-status) for the
additive fields and unavailable-power behavior.
`loaded_config_sha256` identifies the exact loaded file bytes, in addition to
the compiler's `config_generation`. Updates use an explicit normal restart;
see the [update procedure](updating.md).
Configured assignments remain in the same atomic inventory while unplugged,
with `connected = false`; each schedule row carries its optional connector, so
a runtime-only bar can distinguish global and targeted rules without querying
the authoring socket.
`output_discovery_error` is empty during normal operation and explains when
the service is temporarily using its last known connectors because niri could
not provide a fresh snapshot. Runtime verbs shown above without an operand are
strictly zero-argument; ignored trailing text is rejected, including for
`quit`.

`on <connector> ...` is the connector-scoped runtime envelope. The connector
is one whitespace-free token; `playlist-use` takes the remaining text as one
possibly multiword id/name, transport verbs take no argument, and Cycle or
Shuffle take exactly `on`, `off`, or `default`. The Python client validates
that grammar before contacting Rust and never falls back to the legacy global
Python renderer. Assignment and schedule edits are still configuration and are
not accepted through `on`.

An automatic startup, schedule or cycle hand-over which cannot apply gets three
attempts total, two seconds apart. The service keeps or restores the
last-known-good still between attempts; after the third failure it marks that
resolved entry taboo for the session and advances to another usable candidate.
An already-started renderer which later exits does not enter that retry machine:
the service reaps its process group, reapplies the paired still and records the
exact entry in `last_error`. A Wallpaper Engine scene crash becomes taboo
immediately; a failed video child may be tried on a later explicit visit rather
than being restarted in place. The app shows the failure as a one-time toast and
a persistent static-fallback subtitle, and removes Play for a taboo current
entry. The complete retry and persistence contract is in
[`runtime-config.md`](runtime-config.md).

`open` validates the page name, presents the requested workflow in an existing
app process, or requests a GUI launch when only the Rust service is running.
The detached launch reply deliberately says **launch requested** rather than
claiming a window was observed. With a window open, `open` only presents and
navigates: it never reloads the palette or rescans the library, so a panel link
can call it as often as it likes. When the last window was closed, the window
`open` builds shows the library and palette the process already holds at once,
then rescans the library once in the background, because nothing watches the
library folders and files may have been added meanwhile. F5 rescans an open
window. The `displays` spelling is an alias for the Display schedules page, so
a shell or panel integration does not have to know that both concepts share one
screen. The new interface, which an app starts with unless Classic is chosen
in Settings (or `--ui=classic` given), opens every page too: `media` (the
Library), `browse` (the Store), `playlists`, `settings` and `schedules`
(Schedule, which `displays` also opens). A page the new interface lacked
would still present the window and answer with kind `not-in-new-ui` and exit
status 1.

`providers`, `search` and `download` reach the same provider code the Store
page uses, so a wallpaper can be found and pulled into the library without
opening the window. `providers` and `search` print tab-separated rows with `#`
comment lines around them, which is what `cut -f1` and `while read` already
understand; the identifier comes first because it is the field `download` takes
back. Because `search` and `download` wait on a website, they are answered from
a worker rather than from the main loop, so the window does not freeze for the
duration.

`list` matches and orders through the same code the Library's search box and
sort use, so the two cannot disagree about which wallpapers `snow` names. `favourites` is a
separate listing from `list favourites` because it has to show entries whose
file is not in the library right now -- an unmounted drive -- which by
definition `list` cannot.

Every wallpaper resolves to a *pairing*: a representative still, an optional
moving source, and the colours it asks Noctalia for. Nothing has to be created
-- a still pairs with itself, a video pairs with whatever the conventions find
-- and only the ones you change are written down, so a better default still
reaches everything you have not spoken for. `pairing` shows one; `still` and
`palette` choose; `reset-pairing` forgets. A palette policy is `adaptive`,
`keep`, or `builtin:`/`community:`/`custom:` and a name. The path is split from
the right for the legacy one-word form; use the explicit ` :: ` separator when
both the path and the chosen value contain spaces.

A **playlist** is a named, ordered list, and `playlist-use` plays one now as a
temporary override. `schedule-follow` returns control to the calendar. A manual
override lasts until it is released or the service restarts; it does not erase
or disable schedule rules. Entries have identities of their own, printed by
`playlists <name>` and taken back by `playlist-remove`, so reordering never
renumbers anything and the same wallpaper can appear twice. A list naming
wallpapers that are not here keeps them -- an unmounted drive is not a deletion.
A playlist with no resolvable entries, like an empty one, is not in the
runtime configuration. Whatever names it is left out and said, and everything
else is published: the default plays "All media" instead, and a schedule rule
or a display's own playlist that names it doesn't apply until the media is
back or the playlist is repaired. The app says which and why once, and keeps
it as a notice; `wall-in-one --write-config` prints one `warning:` line for
each. A playlist whose name has a space in it is referred to by the id
`playlists` prints beside it.

Whichever window or verb asks, a change is refused when it would leave a
playlist that the default, a schedule rule or a display uses with nothing to
play, or any playlist holding only wallpapers playback can't use (it would
never reach playback, and whatever uses it would be left out):
`playlist-remove`, a `playlist-add` to an empty playlist, `schedule-add` and `display-assign` with a
playlist that has nothing to play, `remove`, `select` of a video waiting for
its still, and `still <video> default` or `reset-pairing` that would leave a
video without one. The reply's kind is `nothing-to-play`, and nothing is
written. The check is made against what is saved at that moment: the default
in `settings.toml`, the schedule rules, the displays' playlists and the
playlists themselves as their files read then, whoever saved them, not only
what the app has held since it started. These refusals are a courtesy at the
gesture: publication does not depend on them. Whatever another program saved
meanwhile, the runtime configuration is still built, without what can't play.

The same changes wait while the app knows its library isn't current: after a
removal, until the scan after it has finished, and after the library folders or
Wallpaper Engine scanning change, until their scan has. The library it holds
then can still name a file that is gone, so `playlist-add`, `playlist-remove`
and `select`, and `still` or `reset-pairing` for a wallpaper a playlist holds,
are refused with kind `library-not-current`, and nothing is written. Try again
once the scan has finished (refresh the library if it failed).

These refusals say the app couldn't check something, or couldn't finish what
the change needs, never that the thing is gone. Unless one says otherwise,
nothing is written or deleted:

- `playlist-delete` replies with kind `unreadable` while `settings.toml` can't
  be read or is malformed: the app can't tell whether that playlist is the
  default. With no `settings.toml` at all, the playlist is deleted as usual.
- `playlist-delete` of the playlist that is the saved default is refused while
  `settings.toml` can't be changed, because deleting it would have to clear
  the default. The kind is `local-io` when the file can't be saved where it is
  (its folder is read-only), and `read-only` when the file holds settings this
  version doesn't know. The reply ends "Nothing was deleted." A playlist the
  default doesn't name is deleted as usual.
- A change that would leave a playlist with nothing to play (`playlist-remove`
  of the last wallpaper it plays, `still <video> default`, `reset-pairing`,
  `remove`) replies with kind `unreadable` while `settings.toml`, the schedule
  file or the displays file can't be read or is damaged: whether anything uses
  that playlist can't be known, and the reply names the file. Nothing else
  waits on such a file, and a damaged one is still repaired by the next change
  saved to it (`schedule-add`, `display-assign`), which keeps the damaged file
  beside it as `.broken`. A file that isn't there at all is nothing saved, and
  the change goes ahead.
- A change that asks what a playlist holds (`playlist-remove`, `playlist-delete`,
  `still <video> default`, `reset-pairing`, and `schedule-add` or
  `display-assign`, which ask whether the playlist has something to play)
  replies with kind `unreadable`, naming the file, while the playlists file
  can't be read or is damaged. `playlist-new` and `playlist-add` are saved to
  that file, so they go by which of the two it is. A file that can't be read at
  all (its permissions, say) refuses them the same way, and is never written
  over. A damaged one is repaired by the save: the damaged file is kept beside
  it as `.broken`, and the new file holds what could still be read from it,
  with the change. `playlist-new` is not held up by a damaged file.
  `playlist-add` goes ahead when the playlist it names is among what could be
  read, and is refused as `unreadable` when it isn't; in a file that isn't JSON
  at all, none is.
- `remove`, whether it deletes the wallpaper or moves it to the trash, replies
  with kind `local-io` when the wallpaper, or its folder, can't be looked at.
  Before 0.3.0 that answered `missing`, "is no longer there".
- `remove` of a downloaded wallpaper the app can't open for writing is refused
  before anything changes.
- `remove` replies with kind `local-io` when the wallpaper, or its download
  record, was moved aside for the removal and that move couldn't be confirmed
  on disk (a full disk, say). This one has moved a file: the reply says where
  it is, and that the removal is still pending. The next library scan or start
  finishes it, or puts the download record back when the wallpaper itself was
  never touched.

Pairing commands use ` :: ` between their two operands. That delimiter is
required when both sides may contain spaces—for example a video path and a
representative-still path, or a wallpaper path and `community:Tokyo Night`.
The older single-space form remains accepted when the right side is one word.
The picture passed to `still` must be a still item in the current library scan;
an existing but unindexed path and an indexed video are both refused. Copy or
move an outside picture into a configured library folder, or add its folder in
Settings, then refresh before choosing it. The command never imports or copies
the file for you. `default` remains the way to return to the internal automatic
choice.

**Wallpaper Engine** content installed through Steam is picked up automatically
-- 49 wallpapers on the machine this was built on. Most Workshop items turn out
to be plain videos, which play through mpvpaper like any other; only true
`scene` wallpapers need `linux-wallpaperengine`, and their stills are captured
through it in a window without anything appearing on screen. The capture window
uses the display's physical mode as niri reports it, because the engine's
default window produces a small portrait screenshot; when niri cannot be asked,
no scene still is taken. A managed scene still smaller than the display in both
directions (the old portrait captures) is regenerated automatically and
atomically; one that is merely another shape, say after plugging in a 16:9
monitor, is kept. Custom still choices are never overwritten. A scene's
details in the Library also offer **Capture a new still** (in the classic
interface, **Regenerate** in its pairing editor).

**Play Wallpaper Engine scenes** is on by default and is under
**Settings → Advanced → Scenes**. The engine is single-instance per output and other
things drive it -- Noctalia's own `linux-wallpaperengine-controller` plugin
among them -- so Wall-in-One checks for an existing owner before it starts one.
If another engine already holds the screen, the app says so and leaves the
scene's still up instead. Turn the setting off if another controller should
always own scenes. Steam's files are never deleted, moved or written beside.

A **schedule** puts the calendar in charge of which playlist is in force. Rules
take `days=`, `months=`, `from=` and `to=`, all optional, all combined with and;
a rule with none of them matches always. Times are local, inclusive at the
start and exclusive at the end, and a window whose end is before its start
wraps midnight -- `from=22:00 to=06:00` is one window. The **last** matching
rule wins, so adding a rule is how you carve an exception out of an earlier
one. The Schedule page lists the rules the other way up: there the top rule
wins, and a rule you add lands on top. When nothing matches, the configured
default playlist applies. An on-demand playlist choice sits above the calendar
until you resume the schedule (**Resume schedule**, or `ctl schedule-follow`). The Rust runtime evaluates the local-time rules while the GUI is
closed; the rules themselves have one-minute resolution, and a boundary change
is applied without waiting for the Python app.

`schedule-name <rule-id> <name>` gives a rule a name of up to 120 characters,
and `schedule-name <rule-id>` with no name clears it. `schedule` prints the
name in its last column, `-` for none. A name is a label only: the rule keeps
its id, and the runtime never sees the name. Naming the first rule moves
`schedules.json` to version 3 (see
[Format versions](library.md#format-versions)).

`remove` is the only verb that destroys anything, and over a socket there is no
confirmation dialogue to fall back on. So the path must be absolute and must
match a wallpaper the scan actually produced; anything else is refused before
it reaches the code that deletes. What happens then is the same split as the
Library's **Delete from disk…** and **Move to Trash** -- a downloaded wallpaper
is deleted and the reply says it cannot be undone, one of your own is moved to
the trash, and if the trash is on another filesystem it is refused rather than
quietly unlinked.

### Deadlines and an unknown outcome

Every client wait is bounded, but the bound reflects the work behind the verb
rather than imposing the old five-second limit on every request:

| Request | Client deadline |
| --- | ---: |
| Cheap authoring reads and non-applying runtime commands | 5 seconds |
| `displays` compositor discovery | 10 seconds |
| One-store Settings/library authoring | 15 seconds |
| Direct runtime apply; legacy Next/Previous/Random; playlist-delete cascade | 45 seconds |
| `search`; journalled `remove`/trash and metadata cleanup | 60 seconds |
| `reload-palette` including an in-flight and replacement Noctalia fallback chain | 180 seconds |
| `select`; authoring-socket playlist fallback including compile, reload and apply | 110 seconds |
| `download` | 600 seconds |

Each exchange shares one deadline across connecting, sending and receiving;
partial response fragments do not reset it. These are client response
deadlines, not unsafe cancellation points. In
particular, an atomic write, directory fsync, trash move or metadata cascade
which has started must finish its durability boundary even if the caller goes
away. A renderer command, palette reload, window-open request or process quit
may likewise cross its observable boundary before its reply. A timed-out
mutation therefore reports that its outcome is **unknown** and tells the caller
to verify current state before retrying; it must not be interpreted as a
refusal. This matters for non-idempotent commands such as Toggle and Next.
Read-only timeouts retain the simple `timed out after ...` message. A client
deadline from `ctl status` exits **75**, meaning status is temporarily
unavailable, not that the runtime is absent or known healthy. Consumers may
retry status and display explicitly stale last-known data. Confirmed absence
remains exit **3**; other control failures and uncertain mutations remain **1**.
This meaning of 75 is specific to `ctl status`: upgrade/preparation commands
have their own failure contract and must not be treated as successful startup.
The graphical authoring actor refuses a second control mutation with
`authoring-busy` while one is active, so it does not queue a new socket write
which could begin only after that caller timed out.

A successful ordinary authoring reply confirms the store/config transaction
is durable and the running app adopted it. One success is weaker, and says
so: when a file was replaced but syncing its folder then failed, the change is
what every reader sees, so the app adopts it and replies with success, with
"... was saved, but syncing its folder failed, so the change may not survive a
power loss" appended to the message. It is never reported as a refusal or as
"nothing changed". A failure that comes after a write landed (the app
couldn't adopt or apply it) replies `saved-not-applied`, "Saved, but it
couldn't be applied: ...", and the saved change is published; a request that
wrote nothing (removing a rule that isn't there, say) fails with its ordinary
reason. Runtime-document compilation and
reload are queued afterward and may settle later; consult runtime `status` for
effective playback truth. `select` and the authoring-socket `playlist-use`
compatibility path are the deliberate exceptions: their reply waits through
compile, any required reload, and the final runtime action. Playlist deletion
confirms its durable cascade but does not wait for an independently requested
return from a deleted manual override to schedule control.

Exit code 3 means no instance is running -- distinct from 1, so a caller can
react by launching it instead of reporting a failure.
