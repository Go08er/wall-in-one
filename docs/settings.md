# Settings

`~/.config/wall-in-one/settings.toml` is written by the Settings tab and safe
to edit by hand. Anything out of range is clamped rather than rejected: a bad
settings file should degrade to something usable, not stop the app starting.

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
| `video_muted` | Mute video wallpapers. Takes effect immediately. | `true` |
| `video_volume` | 0-100. Kept while muted, so unmuting lands where you left it. | `100` |
| `video_when_hidden` | What a video does when a window covers it: `pause`, `stop` or `play`. Takes effect on the next video. | `"pause"` |
| `output` | Connector the wallpaper is applied to, e.g. `eDP-1`. Empty means every output. | `""` |

The Wallhaven key is not in here -- it lives in its own 0600 file. Neither are
the favourites, which are app-maintained state rather than something you type;
both are in [`library.md`](library.md).
