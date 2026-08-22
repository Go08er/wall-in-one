# Settings

`~/.config/wall-in-one/settings.toml` is written by the Settings tab and safe
to edit by hand. Anything out of range is clamped rather than rejected: a bad
settings file should degrade to something usable, not stop the app starting.
That recovery policy belongs to the interactive app. The unattended
`wall-in-one --write-config` compiler is deliberately strict: a present known
key with the wrong type, a non-finite or out-of-range number, an unknown key,
or an invalid enumerated value is reported and leaves the last resolved runtime
document untouched. This prevents a typo from silently publishing defaults at
login while still letting the Settings screen open for repair.

| key | meaning | default |
|---|---|---|
| `roots` | Folders scanned for wallpapers. Empty follows Noctalia's own `[wallpaper] directory`. The first one receives downloads and generated stills. | `[]` |
| `opacity` | Window background opacity; `1.0` is fully opaque. Clamped to a floor of `0.30`, below which the window stops being legible. | `1.0` |
| `preview_scheme` | Palette generator used when a palette is derived from a wallpaper. One of Noctalia's ten schemes. | `"m3-tonal-spot"` |
| `follow_noctalia_palette` | Apply Noctalia's palette to the app's own chrome. | `true` |
| `cycle_enabled` | Change wallpaper on a timer. | `false` |
| `cycle_interval` | Seconds between automatic changes. Clamped to 5-86400. | `300` |
| `cycle_favourites_only` | Narrow the rotation to starred wallpapers. Ignored while that would leave nothing to rotate through. | `false` |
| `shuffle` | Visit every wallpaper once before repeating. | `false` |
| `dynamics_enabled` | Play video wallpapers. Off shows their paired stills instead. | `true` |
| `video_muted` | Mute video wallpapers. Takes effect immediately over mpv IPC; Wallpaper Engine scenes remain silent. | `true` |
| `video_volume` | Video volume, 0-100. Kept while muted, so unmuting lands where you left it without restarting mpvpaper. | `100` |
| `video_when_hidden` | What a video does when a window covers it: `pause`, `stop` or `play`. Takes effect on the next video. | `"pause"` |
| `video_hardware_decode` | Let mpv choose a hardware decoder. Turn off only to diagnose corruption, tearing or driver trouble. | `true` |
| `video_interpolation` | Low-frame-rate smoothing: `off`, `oversample` or `linear`. Non-off modes use display-resample with the unambiguous active monitor refresh; mixed-rate All outputs stays unsmoothed. | `"off"` |
| `scene_fps` | Native linux-wallpaperengine render-rate limit, 1-240 FPS. Video wallpapers keep their source rate because an mpv post-decode FPS filter does not reduce decoding work. | `30` |
| `output` | Connector the wallpaper is applied to, e.g. `eDP-1`. Empty means every output. | `""` |
| `own_scene_renderer` | Let Wall-in-One start linux-wallpaperengine for true Workshop scenes. Existing ownership of the target output is still respected. | `true` |
| `scan_workshop` | Include Wallpaper Engine items found in Steam's Workshop libraries. | `true` |
| `active_playlist` | Stable id of the default named playlist when no schedule rule matches. Empty uses the built-in all-media playlist. Runtime overrides do not rewrite it. | `""` |

The Wallhaven key is not in here -- it lives in its own 0600 file. Neither are
the favourites, which are app-maintained state rather than something you type;
both are in [`library.md`](library.md).
