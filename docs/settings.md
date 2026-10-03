# Settings

The Settings tab writes `~/.config/wall-in-one/settings.toml` (or
`$XDG_CONFIG_HOME/wall-in-one/settings.toml` when that variable is set). You can
also edit it by hand.

Normal startup checks saved settings before opening the main window. Invalid
or unreadable settings lead to a configuration-recovery window with the error,
**Open settings file**, and **Try again**. It does not reset your settings or
open the normal Settings tab with clamped defaults. Repair the named file,
then retry; the Rust wallpaper service does not need to be running.

Each Settings edit also strictly reloads the persisted document before saving.
If it becomes invalid while the app is open, the attempted edit fails without
replacing it; repair the file before retrying the change.

The unattended `wall-in-one --write-config` compiler is also strict. A present
known key with the wrong type, a non-finite or out-of-range number, or an
invalid enumerated value is reported and leaves the last resolved runtime
document untouched. It never substitutes defaults for invalid saved settings.
A key it doesn't know is different: see [Unknown keys](#unknown-keys).

## Repairing an invalid settings file

1. Read the reported error and use the exact settings path it names. Use
   **Open settings file** if the recovery window is shown; it can stay open
   while you repair the file. Avoid other Settings edits during the repair.
2. Copy the existing file to a separate backup before changing it. Keep its
   `roots` paths and order, display mode, colour-source choice and other
   settings intact.
3. Correct the reported TOML syntax, type or value using the reference below.
   Do not delete or reset the file as a repair shortcut: that can lose your
   chosen library paths and playback settings.
4. Save the file and select **Try again** in the recovery window, or reopen the
   app if you closed it. Check that the saved choices look right and retry any
   Settings change that failed. If another error is reported, address it before
   continuing.

If the error mentions an interrupted migration or conflicting upgrade state,
follow the [migration guide](migrating.md) first. Do not remove migration
journals to force an older build to start. `--write-config` publishes a
runtime document when successful; it is not a read-only settings checker.

## Unknown keys

`settings.toml` has no version number, so Wall-in-One can't tell a key from a
newer release apart from a typo. 0.1.4 refuses the whole file, which is why
0.2.0 writes only the keys 0.1.4 knows (see
[Rolling back from 0.2.0 to 0.1.4](updating.md#rolling-back-from-020-to-014)).
Since 0.2.0, a key this version doesn't know never stops anything. Instead:

- Every reader uses the keys it knows. The unknown ones are ignored, never
  removed.
- Settings become read-only. The Settings page shows a banner naming the keys
  and its controls are disabled. Any other way of changing a saved setting
  (the default-playlist pickers, the first-run folder choice, `ctl dynamics`
  and `ctl cycle-interval`, deleting the playlist that is the saved default)
  is refused with the same message. Wall-in-One doesn't write the file while
  the keys are there, because writing it would drop them. Temporary playback
  overrides such as `ctl shuffle` still work.
- The wallpaper service still starts. Its startup check and `--write-config`
  print a warning naming the keys and compile the runtime document from the
  known ones. That document is exactly the one the file would produce without
  the unknown keys, so regenerating it loses nothing, and other library,
  playlist and schedule changes keep reaching the service.

To edit settings in the app again, correct a typo in the file and reopen
Wall-in-One. If a newer release added the key, keep it and change settings
with that release instead: removing the key throws away what it saved.

None of this applies to a file that isn't valid TOML. That is still damage,
handled as described above, because its list of keys can't be trusted.

## The settings.toml freeze and ui.toml

`settings.toml` is frozen. No release adds a key to it, or a new value to an
existing key, ever again; an older release would read either one as a typo,
and 0.1.4 can't use a file with one at all.

- New preferences for the window itself (glass style, opacity, frost,
  thumbnail size, the page to reopen, the interface to start) go in
  `ui.toml`, beside `settings.toml`. A start without `--ui` reads it, without
  writing, for `interface` (`classic` or `next`): the interface it builds. A
  missing or unusable value means classic, and `--ui=classic` or `--ui=next`
  always overrides it. The classic interface also reads it for whether you
  dismissed the Tidy up card after an update (`tidy_offer_dismissed`), and
  writes it only when you do or when you change Settings → Appearance →
  Interface. The new interface (`--ui=next`) reads it when its window opens
  and writes it only when you change the window style, an opacity or frost
  dial, the thumbnail size, or the Interface row on its Settings page.
  Neither writes it on start or while idle, and there is no unconditional
  save on close: a pending change you made (a dial you had just let go of) is
  flushed when the window closes or the app quits. Neither saves the page to
  reopen or the window size.
- New wallpaper behavior goes in a versioned authoring store, with a version
  bump.

`ui.toml` carries a `version`. A release that finds a newer version uses the
preferences it understands and never writes the file. Version 2 adds
`interface`. A file moves to it only when you first choose New (preview): its
version 1 bytes are kept first as `ui.toml.v1-backup` (never overwritten, and
without it nothing is saved), and it stays at version 2 after that, even if
you choose Classic again. A profile that never chooses New stays at version
1, which 0.2.0 and 0.2.1 keep reading and writing. Keys it doesn't know are
kept through every save, an invalid value falls back to that preference's
default, and a missing file means defaults. An unreadable or malformed
`ui.toml` also means defaults, but is left exactly as it is until you fix or
delete it. The file never affects the wallpaper service.

## Tidy up

Settings → Tidy up cleans up what older versions left behind. Nothing runs by
itself or during an update: each action lists exactly what it will change and
what it keeps (and why), and changes nothing until you choose Apply. If
anything changed since that list was made, Apply refuses and shows the new
list instead. Clearing the thumbnail cache is the one deliberate exception:
thumbnails come and go while you browse, so Clear Cache removes whatever
thumbnails the cache folder holds when you click it, never anything outside
that folder.

| Action | What it does | Kept safe by |
| --- | --- | --- |
| Archive old leftovers | Moves empty claim folders, empty deletion records, the finished upgrade's `.wall-in-one-removal-*` folders (with the pre-upgrade copies of `settings.toml` and `runtime.toml`) and empty removal folders no pending removal uses into one dated folder under `~/.config/wall-in-one/tidy-archive`. Anything holding data, recent, or not provably finished stays, with the reason shown. | The archive and its `manifest.json`, which records where each item really is, even one that changed while it was being archived; Undo moves everything back. If something new now has an item's name, Undo leaves that item archived and can be tried again later, or **Keep Archived** stops offering it. |
| Fix the palette template name | Points Noctalia's `[theme.templates.user.wall-in-one]` `input_path` from the old `palette.json.tmpl` at the current content-addressed template, then asks Noctalia to reload. | `settings.toml.bak-wall-in-one-<date>-<time>` (and the replaced file as `….original`) beside Noctalia's settings, plus a copy in the archive. Undo restores the file byte for byte, or, if Noctalia saved it since, reverses just this change. If Noctalia doesn't confirm reloading after an Undo, the new template file stays in place and Retry Reload finishes the Undo; Apply waits until it has. |
| Archive the old palette template | Moves the old `palette.json.tmpl` into the archive, but only after Noctalia confirmed it reloaded its settings and has since rendered your colors (the next time your colors change). If Noctalia didn't confirm the reload, the switch counts as unfinished: the old file stays, and Fix the palette template name offers Retry Reload. | The archive; Undo moves it back, or waits and can be tried again while something else has its name. |
| Remove the old plugin's settings | Removes the keys the retired Wall-in-One plugin left under `[plugin_settings."goober/wall-in-one"]`. The current companion uses the same id, so every key it reads stays. | The same backups and Undo as the template fix. |
| Clear the thumbnail cache | Deletes the cached thumbnails. | Nothing is needed: it is only a cache, rebuilt as you browse, so there is no archive and no Undo. |

After an update, a card at the top of the window offers the first four once,
when there is something to tidy. Showing it writes nothing; choosing Review
or Not Now records the dismissal in `ui.toml` (creating it, and its
`.ui.toml.mutation.lock`, if they don't exist yet).

## Settings reference

| key | meaning | default |
|---|---|---|
| `roots` | Folders scanned for wallpapers. Empty is unconfigured and triggers the graphical first-run choice; the first chosen root receives downloads and generated stills. | `[]` |
| `opacity` | Window background opacity, 0.30-1.0; `1.0` is fully opaque. | `1.0` |
| `preview_scheme` | Default adaptive colour scheme for previews and pairings that inherit it. Explicit pairing schemes take precedence; the app's Noctalia-following UI colours are separate. | `"m3-tonal-spot"` |
| `follow_noctalia_palette` | Apply Noctalia's palette to the app's own chrome. | `true` |
| `cycle_enabled` | Change wallpaper on a timer. | `false` |
| `cycle_interval` | Seconds between automatic changes, 5-86400. A playlist may set its own. | `300` |
| `cycle_favourites_only` | Narrow the rotation to starred wallpapers. Ignored while that would leave nothing to rotate through. | `false` |
| `shuffle` | Visit every wallpaper once before repeating. A playlist may set its own. | `false` |
| `dynamics_enabled` | Animate videos and Wallpaper Engine scenes. Off releases their renderers and shows paired stills. | `true` |
| `stop_animations_on_battery` | Temporarily show paired stills on battery without changing manual playback choices. See below. | `false` |
| `video_muted` | Mute video wallpapers. Takes effect immediately over mpv IPC; Wallpaper Engine scenes remain silent. | `true` |
| `video_volume` | Video volume, 0-100. Kept while muted, so unmuting lands where you left it without restarting mpvpaper. | `100` |
| `video_when_hidden` | What a video does when a window covers it: `pause`, `stop` or `play`. Takes effect on the next video. | `"pause"` |
| `video_hardware_decode` | Let mpv choose a hardware decoder. Turn off only to diagnose corruption, tearing or driver trouble. | `true` |
| `video_interpolation` | Low-frame-rate smoothing: `off`, `oversample` or `linear`. Non-off modes use display-resample with the unambiguous active monitor refresh; mixed-rate All outputs stays unsmoothed. | `"off"` |
| `scene_fps` | Native linux-wallpaperengine render-rate limit, 1-240 FPS. Video wallpapers keep their source rate because an mpv post-decode FPS filter does not reduce decoding work. | `30` |
| `scene_scaling` | Global linux-wallpaperengine scene fit: empty uses the renderer default; otherwise `stretch`, `fit`, or `fill`. It is typed rather than an arbitrary renderer argument. | `""` |
| `scene_clamp` | Global linux-wallpaperengine texture-edge mode: empty uses the renderer default; otherwise `clamp`, `border`, or `repeat`. | `""` |
| `display_mode` | `mirrored` keeps every display on one wallpaper, cursor and schedule (the default). `independent` gives each live connector its own playlist route, schedule winner, cursor, Shuffle/Cycle overrides and renderer; saved assignments and targeted rules remain dormant rather than being deleted when mirrored mode is restored. | `"mirrored"` |
| `theme_source_connector` | Display whose wallpaper drives Noctalia's one shell-wide palette. Independent mode requires one connector token with no whitespace; its UI initially designates the first discovered live display. A detached saved connector is preserved rather than rewritten, while runtime status reports the lexically-first live fallback actually in force. Mirrored mode may keep the choice dormant. | `""` |
| `output` | Legacy single-output compatibility value. New display authoring uses `display_mode`, display assignments and targeted schedule rules; the Settings UI no longer exposes this ambiguous control. | `""` |
| `own_scene_renderer` | Let Wall-in-One start linux-wallpaperengine for true Workshop scenes. Existing ownership of the target output is still respected. | `true` |
| `scan_workshop` | Include Wallpaper Engine items found in Steam's Workshop libraries. | `true` |
| `active_playlist` | Stable id of the default named playlist when no schedule rule matches. Empty uses the built-in all-media playlist. Runtime overrides do not rewrite it. | `""` |

The Wallhaven key is not in here -- it lives in its own 0600 file. Neither are
the favourites, which are app-maintained state rather than something you type;
both are in [`library.md`](library.md).

## Battery animation control

Enable **Stop animations on battery** under Playback to release Wall-in-One's
video and scene renderers while the system is running on battery. This also
works with the app window closed and without the Noctalia plugin. It requires
the updated Rust service and a working UPower service on the system bus.

Battery control does not change **Animate wallpapers**, your Play/Pause/Stop
choice, the selected wallpaper, or your playlist overrides. Schedules and
enabled cycling can keep choosing paired still images. When AC power returns,
animations resume only where your current settings permit them. Manually paused
or stopped displays stay that way. A released video or scene may restart from
its beginning; this option does not preserve its animation timestamp.

If power detection is unavailable at startup, normal playback continues and
the UI reports **Power information unavailable**. If detection disappears
after a confirmed battery state, the existing restriction stays in place until
AC is confirmed or you turn the option off. The UI identifies that retained
restriction separately from a current battery observation.

The option is off by default and is omitted from the saved file while false,
preserving compatibility with older settings and interrupted upgrade records.
Enabling it generates runtime schema 5, which requires the updated service.
With it off the compiler keeps the existing schema-4 format. The new service
accepts both; an older service will reject schema 5 rather than silently ignore
the enabled option. Public status remains version 2 with additional power
fields described in [the runtime contract](runtime-config.md#battery-policy-status).
