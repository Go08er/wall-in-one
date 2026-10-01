# Wall-in-One — interactive UI redesign prototype

A clickable GTK4/libadwaita prototype of a more intuitive Wall-in-One, built
with the same toolkit as the real app so it can be wired up later page by page.
It runs on dummy data and generated artwork and **never changes your real
library, settings, wallpaper service or Noctalia**. The one thing it reads is
Noctalia's current palette and wallpaper, read-only, so its colors and frosted
glass match your desktop live. Every control does something visible: state
changes, dialogs, or toasts with Undo.

```bash
/home/goober/Documents/wall-in-one/prototypes/ui-redesign/run.sh          # dark
/home/goober/Documents/wall-in-one/prototypes/ui-redesign/run.sh --light  # light
```

Add `--style translucent` or `--style frosted` to open in a glass style, and
`--simulated` to take colors from the demo's wallpapers instead of your desktop.

The sidebar's ☰ menu has **Demo** switches:
- **Real Noctalia colors** (on when your palette is found);
- dark style;
- simulate battery power;
- simulate a stopped service;
- show the welcome screen;
- **Simulate time passing**, which runs the clock 20 minutes per second so you
  can watch rotation and the schedule take over.

Screenshots of every page are in [`screenshots/`](screenshots/); re-render them
with `./screenshots.sh`. The code layout and design rules are in
[`DESIGN.md`](DESIGN.md).

## Window styles: transparency and frosted glass

Choose one in the ☰ menu (**Window style**) or in **Settings → Appearance**.
**☰ → Opacity and frost…** jumps straight to the dials.

- **Solid:** today's look.
- **Translucent:** your desktop shows through the window. This mirrors the
  real app's opacity setting.
  - On niri, a `background-effect { blur true }` window rule turns this into
    real frosted glass. Your `aesthetics.kdl` already has one for
    `dev.goober.WallInOne`.
  - The prototype runs under that same app ID, without claiming the real app's
    D-Bus name, so the rule applies to it too.
  - **Settings → Appearance → Copy rule** copies the snippet for other users.
- **Frosted:** works on any compositor. The app draws your *current wallpaper*
  behind itself and crossfades when it changes; try it with **Simulate time
  passing**. Nothing is painted over the wallpaper except the panels.

Both glass styles have two separate opacity dials, and Frosted adds a third:

- **Background opacity:** the page behind the grid and lists.
- **Panel opacity:** the sidebar, header, player bar, details pane and cards.
- **Frost** (Frosted only): how strongly the wallpaper is blurred, from 0% (a
  clear picture) to 100%.

All dials run 0–100%; the real app's own setting stops at 30%. Each style
remembers its own values. The panels are tinted by the desktop's current
palette, not a fixed gray (see below).

Every region paints exactly one layer, so the numbers mean what they say. The
earlier build stacked panels on a see-through window: 75% on 75% shows as 94%,
which is why it still looked solid. Measured on the rendered window, the
sidebar, header, player bar and cards now show the panel value, and the page
shows the background value. Cards sit on the page, so they can't be clearer
than it.

## Colors follow the desktop, live

By default the window follows the **real Noctalia**:
- It reads `~/.local/state/wall-in-one/palette.json`, the file Noctalia renders
  through Wall-in-One's template on every change, and maps its tokens exactly
  as the real app does.
- It follows Noctalia's light/dark mode.
- Frosted blurs your real current wallpaper.
- Change your wallpaper or palette in Noctalia and the demo recolors within a
  frame or two. It only ever reads these files.

The demo's own styles load just above `~/.config/gtk-4.0/gtk.css`. Noctalia's
`noctalia.css`, imported there, defines opaque colors and is only read when GTK
starts, so anything lower kept the launch colors and solid panels.

**☰ → Real Noctalia colors** off (or `run.sh --simulated`) switches to the
simulated desktop: colors then come from the demo's own wallpapers. That covers
the accent and every surface: window, sidebar, header, lists, cards and
popovers. Everything below recolors on the next frame:

- a new wallpaper (by hand, by rotation, or by the schedule);
- a different scheme, palette or color mode in the inspector;
- a palette applied in **Settings → Palettes** (until the wallpaper changes);
- the display chosen to drive colors;
- dark or light mode.

Like the real desktop it has memory:
- A wallpaper set to **Keep**, desktop colors switched off, or a missing
  template leave the last colors in place.
- **Follow Noctalia for app colors** off returns to plain libadwaita.

## Things to try

- **Library:**
  - Click a wallpaper: details open beside the grid.
  - Scroll to **Colors** and pick schemes, which update a mini desktop preview.
  - Use the frame slider on a video's still.
  - **Apply** from the card, the right-click or ⋯ menu, or Ctrl+Enter.
  - Drag a card onto a playlist in the sidebar.
  - **Select** (the pill on the grid's top-right corner), then add several to a playlist at once.
- **Player bar:** press **Next**, apply something, and watch "your pick" and
  **Resume schedule** appear. Switch the scope to one display.
- **Store:**
  - Search, filter and preview.
  - **Download & apply**: the download lands in the Library and in "All wallpapers".
  - Switch to MotionBGS.
- **Playlists:**
  - Rename inline.
  - Drag a row by its handle: it lifts, and the rows roll out of its way. Or press Alt+↑/↓.
  - **Add wallpapers…**: picks are numbered in the order you'll get them.
  - Remove, then Undo.
  - Create a new playlist from the sidebar.
- **Schedule:**
  - Read the **Now** card, then press **Why?**.
  - Click a block to edit it, or drag across empty days and hours to create a rule.
  - Reorder priority (top wins) and toggle rules off.
  - Jump to December.
- **Displays:**
  - Switch **Same on all displays** / **Each display separately**.
  - Pick which display drives the desktop colors.
  - Set "When nothing is scheduled".
  - Forget a disconnected display.
- **Settings:**
  - Search for "battery".
  - **Palettes → Manage…**, then duplicate a palette and edit it.
  - Change the download folder.
  - Copy the runtime log with paths hidden.

## What changes, and why

The pain points come from the inventory of the current app
(`.project-notes/UI_INVENTORY_20260930.md`, with 43 screenshots of today's UI).

| Today | Prototype | Pain point it answers |
|---|---|---|
| Five bottom tabs; playlists buried in a page | **Sidebar**: Library, Store, every playlist with its cover and count (drop targets too), Schedule, Displays, Settings | Navigation; "which playlist plays" scattered |
| Status in a header subtitle plus a playback popover | One slim **player bar**: what's on screen, *why* ("from schedule until 18:00" / "your pick"), **Resume schedule**, controls, display scope | Status split across 4 places; jargon |
| Pairing editor takes over the window | **Inspector** beside the grid: Apply, still (frame slider), motion, colors, playlists, file | Editor hides navigation; tile menu and editor disagree |
| Scheme ids and dot strips | **Mini desktop preview** + named scheme cards; palettes with swatches | Hard to judge color choices |
| Silent saves, raw errors, nothing reversible | Toasts with **Undo** everywhere; banners for battery/service; problem badge + **Try again** | No feedback; no undo |
| "Quick choice" playlists in every list | A pick is just "your pick" | Generated playlists clutter lists |
| Schedules as a long form, inverted priority arrows | **Week calendar** with blocks, overrides shown, now-line; priority list (top wins); **Why?** | No "now"; ~16–20 clicks for one rule |
| Display assignment inside Schedules | **Displays** page with the monitor layout and color source | Per-display features hidden far away |
| Store dead-ends after download | **Download & apply**, "In library" badges, real filters, preview with tags | No Apply after download; hidden filters |
| One long Settings scroll | Grouped, **searchable** settings with deep links; palette manager; read-only **runtime log** | Unrelated options mixed; no search |

## Decisions for you

1. **Player bar.** You previously said no "Now playing" dashboard. This is a
   slim bar that replaces the header popover and subtitle, not a page. Keep it?
2. **A display's own playlist vs the schedule.** The real runtime plays a
   matching schedule rule *before* a display's own playlist. With rules
   covering the whole day, a display's playlist never plays. The prototype is
   honest about this ("When nothing is scheduled", "Not in use now"). Should a
   display's playlist instead beat *global* rules (but not rules for that display)?
3. **Rule names.** Rules have no names today, so the UI says "Every day ·
   07:00–18:00". Optional names ("Daytime") would read better in **Why?** and the
   player bar.

## Status

- **Wiring:** none. The pages call an in-memory `AppState`
  (`wio_demo/state.py`), the only boundary between them and the dummy data:
  they read it and change anything only through its named methods, so wiring
  replaces that one class with the app's stores and the runtime's status. A
  review noted the schedule resolution should then come from runtime status
  rather than be recomputed in Python.
- **Checks:**
  - an end-to-end headless smoke test of 222 steps passes with no exceptions
    (`tools/smoke.sh`), including a 2,000-wallpaper Library and thumbnails drawn
    off the main thread;
  - every page has been checked in dark, light and narrow (420 px) layouts;
  - ruff is clean.
