# Wall-in-One UI prototype — design guide

This is an **unwired** GTK4/libadwaita prototype of a more intuitive Wall-in-One
UI. Everything runs on dummy data (`wio_demo/data.py`) and an in-memory state
object (`wio_demo/state.py`). Nothing touches the real app, runtime, Noctalia,
files or settings.

## Run it

```bash
cd /home/goober/Documents/wall-in-one
nix develop --command python prototypes/ui-redesign/demo.py
```

Headless screenshots (isolated XDG dirs, Xvfb, cairo renderer):

```bash
SP=/tmp/claude-1000/-home-goober-Documents-goober-noctalia-plugins-v5/2335eb06-27d6-437f-b751-eb02b77501da/scratchpad/proto
$SP/shoot.sh OUTDIR scene [scene ...]
```

A scene is `[flags@]navkey[+demo]`:
- flags (comma separated): `light`, `battery`, `service-off`, `welcome`, `narrow` (420 px wide), `mirrored`.
- navkey: a sidebar key — `library`, `store`, `playlist:<id>`, `schedule`, `displays`, `settings` — optionally `page:argument` (passed to `Page.activate`).
- demo: passed to the page's `demo()` hook to put it into a state worth capturing
  (open a dialog, select an item, etc.).

Examples: `library`, `light@library+inspector:lily-pond`, `narrow@schedule`,
`playlist:frog-day+picker`. PNGs are saved as `OUTDIR/<scene-with-symbols-replaced>.png`.
Look at them (they are the only way to review the design) and iterate.

## Architecture

```
demo.py                 entry point + screenshot runner
wio_demo/
  art.py                procedural wallpaper art: render() pixels (any thread) → texture() (cached)
  data.py               dummy domain data (wallpapers, playlists, rules, displays, folders, palettes)
  models.py             view-model types pages read: Wallpaper, Playlist, Rule, Display, Folder, Palette…
  catalog.py            fixed words and choices (kind labels, day and month names, interval presets)
  state.py              AppState(GObject): the only boundary between pages and data (see below)
  thumbs.py             thumbnails: drawn on worker threads, delivered to the main thread
  store_catalog.py      dummy Store providers (the real app's Browser): options, item facts, search
  ui.py                 shared widgets: Thumb, WallpaperCard, Swatches, pill(), heading(), dim(), add_css()
  style.css             shared styles
  shell.py              window: Adw.Sidebar navigation, ONE shared header bar, banner, toasts, demo menu
  playerbar.py          persistent bottom bar: what's on screen, why, controls
  pages/__init__.py     Page contract
  pages/library.py      Library grid + filters + selection + drag to playlists
  pages/inspector.py    wallpaper details: still, motion, colors (pairing editor)
  pages/store.py        Store (online providers); store_widgets.py
  pages/playlists.py    one playlist (sidebar lists them); playlists_picker.py
  pages/schedule.py     week calendar + rules; schedule_model.py, schedule_calendar.py, schedule_editor.py
  pages/displays.py     monitor arrangement + per-display assignment; displays_arrangement.py
  pages/settings.py     grouped settings + runtime log; settings_widgets.py, settings_palettes.py
run.sh                  launch the interactive demo
screenshots.sh          re-render screenshots/ (headless, isolated)
ruff.toml               lint settings for the prototype
```

`ui.CardGrid` is the shared equal-column grid for wallpaper cards. Prefer it to
`Gtk.FlowBox`, which hands some columns an extra pixel and upsets
height-for-width cards.

Route precedence in `state.py` matches the Rust runtime's `route_decision`:
your pick → a matching schedule rule (global or this display's) → the display's
own playlist → the app default.

### Page contract (`pages/__init__.py`)

A page module exposes `create(state) -> Page`. A `Page` has:
- `name`, `title`, `widget` (the body, placed in the shell's stack);
- `header_start()` / `header_end()` / `title_widget()` — widgets the shell packs
  into the single shared header bar when the page is shown (return the **same**
  objects every call). Header widgets live outside `widget`, so if they use a
  page-local `Gio.SimpleActionGroup`, call `insert_action_group` on them too;
- `activate(argument)` — called every time the page is shown (`playlist:frog-day` → `"frog-day"`);
- `focus_search()` — Ctrl+F;
- `demo(scene)` — screenshot hook.

Pages never import each other. They talk through `state`:
- `state.navigate("playlist:frog-day")`, `state.navigate("settings:log")`;
- `state.toast(text, undo=callable_or_None)` — prefer reversible actions with **Undo**;
- listen with `state.connect("changed", lambda _s, topic: ...)`. Topics: `now`,
  `playback`, `library`, `playlists` (the shell rebuilds the sidebar), `schedule`,
  `displays`, `settings`, `system`, `theme`, `appearance`, `scope`, `clock`,
  `folders`, `preferences`, `display-settings`.

`AppState` is the only boundary between pages and data, so a real-app adapter
can replace it without touching the pages:
- **Reads** go through its lists, lookups and queries: `state.wallpapers`,
  `state.playlists`, `state.rules`, `state.displays`, `state.wallpaper(id)`,
  `state.playlist(id)`, `state.playlist_name(id)`, `state.playlist_cover(id, size)`,
  `state.schemes()`, `state.palettes(origin)`, `state.store_search(query)`, …
- **Writes** only through its named methods (`rename_playlist`, `remove_entries`,
  `set_rule_enabled`, `add_rule`, `set_display_playlist`, `set_color_display`,
  `apply_palette`, `set_wallpaper_colors`, …). Never assign a state field or
  change one of its lists or view-model objects. Each method emits its own
  topics and, where the page offers Undo, returns the undo callable; the page
  words the toast.
- Pages never import `data`. Fixed words come from `catalog`; pictures from
  `ui.Thumb.of(...)` / `thumbs`; the Store's option tables and per-item facts from
  `store_catalog` (its searches go through `state`).
- `demo()` hooks may use the `state.demo_*` helpers to set up scenes.

Useful state: `state.display_mode` (`mirrored`/`independent`), `state.current`
(connector → wallpaper id), `state.manual`, `state.assigned` (connector → playlist id or
"" = follow schedule), `state.now` (fixed demo clock: Wed 30 Sep 2026 14:35),
`state.resolution(connector)` (playlist, winning rule, until, next), `state.apply()`,
`state.play_playlist()`, `state.resume_schedule()`, `state.add_to_playlist()`,
`state.dark`, `state.on_battery`, `state.service_running`.

Shared look: use `ui.add_css(...)` for page CSS; don't edit `style.css`.

## Visual language

- libadwaita idioms first: `Adw.ToolbarView`, boxed lists (`.boxed-list` + `Adw.*Row`),
  `Adw.ToggleGroup` for segmented choices, `Adw.Dialog`/`Adw.AlertDialog`, `Adw.StatusPage`
  for empty states, `Adw.Banner` for persistent conditions, toasts for results.
- Pictures carry the UI. Use `ui.Thumb.of(source, w, h, size=…)`/`ui.thumbnail(...)` (exact
  size, rounded, cover-fit) and `ui.WallpaperCard`: they draw off the main thread and show a
  flat placeholder until the picture arrives. `thumbs.texture(source, w, h)` draws at once
  (drag icons); playlist covers come from `state.playlist_cover(id, size)`.
- Long grids build cards a page at a time: the Library shows 72 (the real app's
  `MEDIA_PAGE_SIZE`) and offers "Show more".
- Section headings inside panels: small caps-like labels (`.section-label`).
- Reordering: `reorder.ReorderList` is the only way to make a list reorderable
  (rows lift and roll; no drag-and-drop). Call `append(row, handle)`, commit on
  `reordered`, and use `row_heights()` rather than `get_height()` for geometry.
- Selection mode: `ui.select_toggle()` hung over the list's top-right corner
  with `ui.hang_on_corner(overlay, list, toggle)`; leave `ui.CORNER_RESERVE`
  free at the right of the row above. Not in the header bar.
- Badges: `ui.pill(text, icon, "accent"|"subtle"|"warning"|"on-image"|"success")`.
- Colors: the app tints itself with the desktop's current palette, both the
  accent and the libadwaita surfaces. `glass.Look` is the only place that
  writes them: it rebuilds one stylesheet from `AppState.desktop_swatches()` on
  the now, library, settings, displays, theme and appearance topics, before the
  next frame. With the real Noctalia attached (`noctalia_live`, read-only) it
  maps `palette.json` tokens exactly like the real app's `theme/css.py`. Its
  provider sits at USER + 1, as in the real app, because Noctalia's
  `gtk.css` import sets opaque `:root` colors at USER priority and is never
  reloaded after startup. Pages must not set colors themselves; to recolor, change state
  and emit the topic. Never hard-code a blue or a gray; use
  `var(--accent-bg-color)`, `var(--accent-color)` and the `--*-bg-color`
  variables.
- Must look right in dark **and** light, and degrade gracefully at narrow widths
  (`narrow@...` scene, 420 px). Use `Adw.Breakpoint`/`Adw.BreakpointBin` where needed.
- Accessibility: every icon-only button gets a tooltip (`ui.icon_button` sets the
  accessible label too); keyboard reachable; never rely on color alone.

### Words

Short, plain, task-first. No paragraphs of explanation in the UI (owner's explicit
preference); a one-line subtitle is the maximum. Vocabulary:

| Concept | Say |
|---|---|
| local media | **Library** |
| online providers | **Store** |
| show it now | **Apply** (button), "is now on all displays" (toast) |
| manual override | **your pick**; undo it with **Resume schedule** |
| scheduled | **from schedule** |
| pairing | the wallpaper itself: its **still**, **motion** and **colors** |
| adaptive palette | colors **From wallpaper** (+ scheme name) |
| named palette | **Palette** |
| keep colors | **Don't change** |
| cycle | **Change every …** / "Change wallpaper automatically" |
| stop | **Stop animation (keep still)** |

## Owner decisions to respect (from `.project-notes/GUI_USABILITY_PLAN_20260904.md`)

1. Library and Store are the names. Keep pairings as the model (still + motion + color
   policy together), presented as the wallpaper's details — not a separate tab.
2. Visible **Apply** and **Edit** actions; conventional context menus; clear display scope.
3. No duplicate "Now playing" *dashboard* (the Noctalia companion covers that). The slim
   player bar is the single place for playback truth and controls.
4. Never auto-reorder library folders: the first folder is where downloads and captured
   stills are saved. Distinguish "remove from library" from deleting files.
5. Settings hold app-wide defaults; per-wallpaper color lives with the wallpaper.
6. Playlist editing must be efficient: add several at once, reorder, remove, undo.
7. Schedules use familiar calendar language: day/time/repeat controls, visual blocks,
   short labels, obvious overlaps. Preserve semantics: **a later rule wins** where rules
   overlap; the end time is exclusive; a window may wrap past midnight and the tail
   belongs to the day it started; empty days = every day; empty months = all year.
8. A modest, read-only, copyable **runtime log** — not a job-management center.
