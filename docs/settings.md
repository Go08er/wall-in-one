# Settings

The Settings tab writes `~/.config/wall-in-one/settings.toml` (or
`$XDG_CONFIG_HOME/wall-in-one/settings.toml` when that variable is set). You can
also edit it by hand.

The window can open with recovery defaults for inspection: out-of-range values
may be clamped, and an unreadable file falls back to defaults in memory. This
does not repair the saved file. Each Settings edit strictly reloads the
persisted document before saving, so invalid persisted settings must be
corrected in the file before new changes can be saved.

The unattended `wall-in-one --write-config` compiler is also strict. A present
known key with the wrong type, a non-finite or out-of-range number, an unknown
key, or an invalid enumerated value is reported and leaves the last resolved
runtime document untouched. Recovery values shown in the window do not
authorize publishing them over the saved configuration.

## Repairing an invalid settings file

1. Read the reported error and use the exact settings path it names. Close the
   app window and avoid other settings edits while repairing the file.
2. Copy the existing file to a separate backup before changing it. Keep its
   `roots` paths and order, display mode, colour-source choice and other
   settings intact.
3. Correct the reported TOML syntax, type or value using the reference below.
   Do not delete or reset the file as a repair shortcut: that can lose your
   chosen library paths and playback settings.
4. Reopen the app, check that the saved choices look right, and retry the
   Settings change that failed. If it still cannot save, address the next
   reported error; opening the window alone does not prove the file is valid.

If the error mentions an interrupted migration, conflicting upgrade state or
settings from a newer app, follow the [migration guide](migrating.md) first.
An unknown key after a downgrade may be a version mismatch, not a typo. Do not
remove migration journals or discard newer settings to force an older build
to accept them. `--write-config` publishes a runtime document when successful;
it is not a read-only settings checker.

## Settings reference

| key | meaning | default |
|---|---|---|
| `roots` | Folders scanned for wallpapers. Empty is unconfigured and triggers the graphical first-run choice; the first chosen root receives downloads and generated stills. | `[]` |
| `opacity` | Window background opacity, 0.30-1.0; `1.0` is fully opaque. | `1.0` |
| `preview_scheme` | Default adaptive colour scheme for previews and pairings that inherit it. Explicit pairing schemes take precedence; the app's Noctalia-following UI colours are separate. | `"m3-tonal-spot"` |
| `follow_noctalia_palette` | Apply Noctalia's palette to the app's own chrome. | `true` |
| `cycle_enabled` | Change wallpaper on a timer. | `false` |
| `cycle_interval` | Seconds between automatic changes, 5-86400. | `300` |
| `cycle_favourites_only` | Narrow the rotation to starred wallpapers. Ignored while that would leave nothing to rotate through. | `false` |
| `shuffle` | Visit every wallpaper once before repeating. | `false` |
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
