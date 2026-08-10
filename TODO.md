# Outstanding

Work the user has asked for that is not yet done. `DECISIONS.md` records what
was decided and why; this records what is still owed.

Kept in the repository rather than in a session, because a conversation ends and
a file does not.

---

## In progress

**UI restructuring** — the app's interface as the user specified it:

- [ ] **Scenes apply as a window instead of the background.** `service/src/renderer.rs`
      `start_scene` falls back to the positional `background id` form when no
      connector is named, and the engine's own help says that form *previews on
      a window*. Only `--screen-root <connector> --bg <scene>` renders to the
      desktop; both flags are repeatable. Capture must keep using window mode —
      that part is correct, which is why stills look right. Needs the connector
      list: either the app writes it into the conf, or the service asks niri.
      Verify against the user's four real Workshop scenes.
- [ ] **The adaptive palette generator is choosable per pairing, with previews.**
      Ten generators exist (`m3-tonal-spot`, `m3-content`, `m3-fruit-salad`,
      `m3-rainbow`, `m3-monochrome`, `vibrant`, `faithful`, `soft`,
      `dysfunctional`, `muted`). `noctalia theme <image> --scheme <s> --both` is
      deterministic and ~0.25s, so all ten can be previewed per still and cached.
- [ ] **Preview static palettes.** Community and custom palettes are readable
      from files. The ten built-ins are genuinely opaque — names are strings in
      the binary, colours are not, no CLI prints them — so cache their colours
      the first time one is applied and show nothing before that. Do not invent
      colours.
- [ ] **Remove the Pairings tab.** Every media item *is* its pairing. Left click
      opens the pairing editor; right click quick-applies.
- [ ] **Playlist authoring by direct manipulation.** Searchable pane of pairing
      thumbnails, drag into the playlist, drag to reorder — like arranging
      photos, not selecting rows of text. Entry ids stay stable across reorder,
      and one wallpaper may appear twice.
- [ ] **Schedules as a calendar and clock**, not text fields. Time of day, time
      of year, day of week. Semantics unchanged: last match wins, window
      exclusive at its end, rules may wrap midnight.
- [ ] **Five bottom tabs**: Browse · Media/Pairings · Playlists · Schedules ·
      Settings. Browse moves out of its dialog; Settings becomes a tab.

## Queued next

**Runtime controls that do not exist yet** (service first, then the bar):

- [ ] **`cycle on|off` as a runtime verb.** `cycle_enabled` is configuration
      only — read at `service/src/runtime.rs:202`, reported at 588, changeable
      by nothing. Cycle off must stop *advancement* while the current wallpaper
      keeps playing. Should be a session override in the same shape as the
      playlist manual override.
- [ ] **A real `stop`, distinct from pause.** Pause is `SIGSTOP` to the renderer
      child (`service/src/renderer.rs:414`) — frozen but resident, still holding
      memory and GPU. Stop should tear the renderer down, keep the still on
      screen, release the resources, and stay there until resumed. `stop()`
      already exists on the renderer trait; it is simply not exposed as a verb.
- [ ] Surface both in the GUI as well as the bar.

**Plugin clarity:**

- [ ] **"Two shuffle buttons".** `Random` wears the `arrows-shuffle` glyph
      (`panel.luau:151`) so it looks like shuffle. And the shuffle button is
      labelled with its *state* rather than its action, so when shuffle is off it
      reads "Shuffle off" — which on a button means "click to turn it off",
      the opposite of what it does.
- [ ] Then add **Cycle** and **Stop** to the panel, once the verbs exist.

## Known gaps, not yet scheduled

- **Scene playback has never been verified on real hardware.** Stills are
  captured in window mode, which works; applying to the desktop is the path the
  bug above is in.
- **Two monitors** remains unverified — one output on the development machine.
  Per-output renderer settings (mute, FPS, scaling per screen) are not built.
- **gSlapper versus mpvpaper** is undecided and needs measuring. mpvpaper stays;
  the video renderer sits behind a trait so switching is cheap. See
  `DECISIONS.md` for what is actually known, including that "drop-in" does not
  hold for how this app invokes mpvpaper.
- **Full click-action configurability in the plugin** was deliberately not built;
  the minimal branch was taken. The user has asked about it once.
