# Control sockets

Every verb is one line of JSON over a mode-0600 socket in `$XDG_RUNTIME_DIR`.
Runtime commands go directly to `wall-in-one-runtime.sock`; authoring and
provider commands go to the Python app's `wall-in-one.sock`. `wall-in-one ctl`
routes between them, so the Noctalia plugin needs no socket code in Luau.

```console
$ wall-in-one ctl next
$ wall-in-one ctl previous
$ wall-in-one ctl random
$ wall-in-one ctl play|pause|stop|toggle
$ wall-in-one ctl cycle on|off|default
$ wall-in-one ctl shuffle on|off
$ wall-in-one ctl playlist-use Evening
$ wall-in-one ctl schedule-follow
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
$ wall-in-one ctl still /path/to/wallpaper.mp4 /path/to/picture.png
$ wall-in-one ctl still /path/to/wallpaper.mp4 default
$ wall-in-one ctl palette /path/to/wallpaper.png builtin:Nord
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
$ wall-in-one ctl quit
```

`quit` is intentional Rust-service shutdown. Merely closing the Python window
does not stop rotation or schedules. `pause` freezes a resident renderer and
also holds the playlist cursor; `stop` tears motion down while leaving the
paired still visible, and `play` resumes either state. A stopped runtime can
still rotate through static pairings when cycle remains on. `cycle on|off` is a
session override and `cycle default` returns to the authored setting.

`status` is JSON describing the active playlist and entry, whether the source
is `manual` or `schedule`, the explicit `playing` / `paused` / `stopped` state,
effective cycle value and its `config` or `manual` source, shuffle state, the
playlist inventory, each display's effective entry, and the last renderer
error. The same snapshot includes every schedule rule, the rule currently
selected by the calendar, and configured versus effective display assignments,
so a bar menu does not need the Python app. These are read-only; assignment and
rule edits still belong in the app-generated configuration. An open GUI is not
accepted as a substitute for runtime status: exit code 3 still means automation
is not running.

If mpvpaper or linux-wallpaperengine exits, the runtime immediately falls back
to that entry's paired still and reports the exact entry in `last_error`; it
does not enter an automatic restart loop. A Workshop scene that crashes the
engine stays static for the rest of that service session, so later rotations
do not repeatedly launch a known-incompatible scene. The app shows the same
failure as a one-time toast and a persistent static-fallback subtitle.

`open` presents the requested workflow in an existing app process, or launches
the app when only the Rust service is running. The `displays` spelling is an
alias for the Display schedules page, so a shell or panel integration does not
have to know that both concepts share one screen.

`providers`, `search` and `download` reach the same provider code the Browse
tab uses, so a wallpaper can be found and pulled into the library without
opening the window. `providers` and `search` print tab-separated rows with `#`
comment lines around them, which is what `cut -f1` and `while read` already
understand; the identifier comes first because it is the field `download` takes
back. Because `search` and `download` wait on a website, they are answered from
a worker rather than from the main loop, so the window does not freeze for the
duration.

`list` selects and orders through the same code the window's search box uses,
so the two cannot disagree about what `stills snow` means. `favourites` is a
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
the right, so a wallpaper directory with a space in its name needs no quoting.

A **playlist** is a named, ordered list, and `playlist-use` plays one now as a
temporary override. `schedule-follow` returns control to the calendar. A manual
override lasts until it is released or the service restarts; it does not erase
or disable schedule rules. Entries have identities of their own, printed by
`playlists <name>` and taken back by `playlist-remove`, so reordering never
renumbers anything and the same wallpaper can appear twice. A list naming
wallpapers that are not here keeps them -- an unmounted drive is not a deletion
-- and if none of them are here the rotation quietly falls back to the whole
library rather than stopping. A playlist whose name has a space in it is
referred to by the id `playlists` prints beside it.

**Wallpaper Engine** content installed through Steam is picked up automatically
-- 49 wallpapers on the machine this was built on. Most Workshop items turn out
to be plain videos, which play through mpvpaper like any other; only true
`scene` wallpapers need `linux-wallpaperengine`, and their stills are captured
through it in a window without anything appearing on screen. The capture window
uses the target display's physical mode (or a 2560x1440 fallback), because the
engine's default window produces a small portrait screenshot. Managed scene
stills with the old portrait/wrong-resolution shape are regenerated
automatically and atomically; custom still choices are never overwritten. A
manual **Regenerate** control is also available in a scene's pairing editor.

**Play Wallpaper Engine scenes** is on by default and is visible under
**Settings -> Playback**. The engine is single-instance per output and other
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
one. When nothing matches, the configured default playlist applies. An
on-demand playlist choice sits above the calendar until **Follow schedule** is
selected. The Rust runtime evaluates the local-time rules while the GUI is
closed; the rules themselves have one-minute resolution, and a boundary change
is applied without waiting for the Python app.

`remove` is the only verb that destroys anything, and over a socket there is no
confirmation dialogue to fall back on. So the path must be absolute and must
match a wallpaper the scan actually produced; anything else is refused before
it reaches the code that deletes. What happens then is the same split as the
tile menu -- a downloaded wallpaper is deleted and the reply says it cannot be
undone, one of your own is moved to the trash, and if the trash is on another
filesystem it is refused rather than quietly unlinked.

Exit code 3 means no instance is running -- distinct from 1, so a caller can
react by launching it instead of reporting a failure.
