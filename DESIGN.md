# Wall-in-One — design and findings

A standalone wallpaper manager for Wayland, distributed as a Nix flake, with a
thin Noctalia plugin as a companion.

This replaces the previous implementation, which lived entirely inside a
Noctalia plugin as ~25,000 lines of Luau. That approach hit two hard runtime
ceilings (see [Why the rewrite](#why-the-rewrite)) and traded them against each
other with no headroom left.

Every claim in the *Findings* section below was verified against
Noctalia `5.0.0-beta.7` and niri `26.04` on a live machine, not inferred.
Sources are cited so they can be re-checked when either project moves.

---

## Why the rewrite

The old design put the entire UI inside a Noctalia plugin entry. Two limits
bound it, and relieving either one worsened the other:

**Luau's 200-local-per-function register limit.** A plugin entry file is
compiled as a single function, so top-level `local` declarations are a fixed
budget of 200. Exceeding it is a hard `CompileError` that disables the entry.
Measured spare registers in `panel.luau` across its history:

| commit | spare |
|---|---|
| `5fd5d3c` | 20 |
| `563b6d8` | 15 |
| `e8069d2` | 15 |
| `4b226a8` | 0 |
| `12b7269` | **fail** — `Out of local registers`, exceeded limit 200 |
| `bf1a8ce` | **fail** |
| `HEAD` (after refactor) | 33 |

At roughly 5 registers per feature commit, that is about six commits of runway.

**Noctalia's per-callback CPU budget.** From `src/scripting/script_runtime.cpp:39-41`:

```cpp
constexpr auto kLoadBudget     = std::chrono::milliseconds(100);
constexpr auto kUpdateBudget   = std::chrono::milliseconds(12);
constexpr auto kCallbackBudget = std::chrono::milliseconds(25);
```

Metered against `CLOCK_THREAD_CPUTIME_ID`, and time spent inside host functions
counts. Repeated overruns disable the plugin outright.

**The two trade against each other.** The 116 remaining convertible top-level
locals in `panel.luau` would buy ~114 registers, but each conversion turns a
register read into a table lookup inside a callback metered at 12 ms.

Meanwhile the UI has no in-process requirement: `panel.luau`'s host API usage is
506 `noctalia.tr`, 133 `ui.label`, 83 `ui.button`, 82 `ui.row`, 40 `ui.column`,
23 `ui.select`, 22 `ui.input` — pure UI-tree construction plus translations.
Only ~1,300 lines genuinely need to stay in-process (`renderer.luau` needs
`noctalia.outputs` and layer-shell; `widget.luau` needs bar placement and
`togglePanel`; `shortcut.luau` needs `focusedOutputName`).

---

## Findings

### 1. Palette generation is a pure CLI, and it is exactly reproducible

Noctalia ships its generator as a subcommand with no event loop and no config
dependency. From `src/theme/cli.h`: *"pure function of (argv, stdout, stderr)"*.

```
noctalia theme <image> --scheme m3-tonal-spot --both
```

Verified on a real wallpaper:

- Emits all **72 tokens** (`src/theme/tokens.h`) for dark *and* light.
- **Byte-for-byte deterministic** across runs (identical SHA-256).
- **0.21–0.27 s**, ~119 MB peak RSS.

Ten schemes (`src/theme/scheme.h`):

| family | schemes |
|---|---|
| Material Design 3 | `m3-tonal-spot` (default), `m3-content`, `m3-fruit-salad`, `m3-rainbow`, `m3-monochrome` |
| Custom HSL-space | `vibrant`, `faithful`, `soft`, `dysfunctional`, `muted` |

The M3 path is built on `material_color_utilities`; the custom path is a
separate HSL generator with deliberately different aesthetics. Both populate the
same token set, so consumers can treat them interchangeably.

The generator takes a forced 112×112×3 RGB buffer
(`src/theme/palette_generator.h`), which is why it is fast and stable.

**Consequence:** "predict what the wallpaper-generated palette will be" needs no
reverse engineering. Shell out to the same binary Noctalia itself calls. A live
preview of all 10 schemes × dark/light costs ~2.5 s; the single scheme in view
costs ~0.25 s.

### 2. The colour scheme is fully controllable over IPC

```
color-scheme-get                  -> "<source> <name>"   source ∈ builtin|wallpaper|community|custom
color-scheme-set <source> <name>
theme-mode-get | theme-mode-set <dark|light|auto> | theme-mode-toggle
templates-apply
wallpaper-get [connector]
wallpaper-set [connector] <path>
wallpaper-next | wallpaper-previous | wallpaper-random [connector]
config-reload
```

### 3. All three palette sources are readable, and custom ones are writable

| source | location | notes |
|---|---|---|
| built-in | compiled into the binary | 10: Ayu, Catppuccin, Dracula, Eldritch, Gruvbox, Kanagawa, Noctalia, Nord, Rosé Pine, Tokyo-Night |
| community | `https://api.noctalia.dev/palettes` (catalog), `.../palette/<name>` | cached to `~/.local/state/noctalia/community-palettes/` |
| custom | `$XDG_CONFIG_HOME/noctalia/palettes/*.json` | plain JSON, one file per palette, read/write |

Sources: `src/theme/builtin_palettes.cpp`, `src/theme/community_palettes.cpp:26-27`,
`src/theme/custom_palettes.cpp:215-220`.

**A palette file is not a 72-token document.** Read off the real cache: it is
Noctalia's `mPrimary` shape -- fourteen core keys plus a nested `terminal`
block -- so `PalettePair.from_json`, which parses `noctalia theme --both`,
cannot read one. `theme/palettes.py` maps it instead. Measured by diffing a
cached community palette against `noctalia theme <img> --theme-json <file>
--both`, which expands the same file into all 72:

| file key | tokens it *is* | file key | tokens it *is* |
|---|---|---|---|
| `mPrimary` | `source_color`, `primary`, `surface_tint` | `mSurface` | `surface`, `background` |
| `mSecondary` | `secondary` | `mSurfaceVariant` | `surface_variant`, `surface_container` |
| `mTertiary` | `tertiary` | `mOutline` | `outline` |
| `mError` | `error` | `mShadow` | `shadow`, `scrim` |

Each `mOn*` maps to its `on_*`, and `terminal.normal.red` and friends map
one-for-one onto the 22 terminal tokens. The other ~30 core tokens are tonal
ramps Noctalia derives at apply time and are left missing rather than guessed;
`Palette.get` already degrades through them.

Three more things the real directories settled:

- Community files are cached under a **percent-encoded** name
  (`Osaka%20jade.json`), while the id Noctalia takes back over IPC -- and
  writes as `[theme] community_palette` -- is the decoded `Osaka jade`.
- Built-in colours really are unreachable. The ten names are plain strings in
  the binary; their colours are not, and no CLI surface prints them. So the
  browser lists built-ins without swatches and gets their colours the ordinary
  way, by applying one and letting the template render.
- `~/.config/noctalia/colorschemes/<Name>/<Name>.json` is a **pre-5.x** layout
  still present on this machine. 5.0.0-beta.7 builds the flat `palettes`
  directory instead ("failed to create palettes directory", "failed to write
  custom palette file" are both in the binary), which is what `paths.py`
  already points at.

### 4. Noctalia already writes the Qt colour scheme — but do not sync that way

Confirmed live: `~/.local/state/noctalia/settings.toml` has
`builtin_ids = ["gtk3", "gtk4", "kcolorscheme", "niri", "qt"]`, and the `qt`
template writes to both `~/.config/qt5ct/colors/noctalia.conf` and
`~/.config/qt6ct/colors/noctalia.conf` (both present, 1098 bytes).

That path is a bad fit for us regardless:

- It only takes effect if the platform theme is qt5ct/qt6ct **and** the user has
  selected the noctalia scheme in qt6ct's own UI. Fragile.
- It is lossy — a ~20-colour Qt palette instead of 72 semantic tokens.

**Use a user template instead.** `UserTemplateConfig` is a real schema field
(`src/config/config_types.h:1411-1423`, `src/config/schema/config_schema.cpp:1120`),
addressed in TOML as `[theme.templates.user.<id>]`:

```toml
[theme.templates.user.wall-in-one]
enabled = true
input_path = "<materialized plugin dir>/palette.json.tmpl"
output_path = "$XDG_STATE_HOME/wall-in-one/palette.json"
post_hook = "wall-in-one ctl reload-palette"
```

Because it is in the schema, Noctalia round-trips it through its own settings
writes rather than dropping it.

Template placeholder syntax, taken from the shipped `assets/templates/qt/qtct.conf`:

```
{{colors.<token>.default.hex}}
```

The `post_hook` field means palette sync is **push-based** — Noctalia pings the
app on every change. No polling, no inotify, no drift. This is the single
cleanest finding in the whole investigation.

### 5. niri blur is config-driven; the app does not need Wayland protocol access

*(An earlier reading of this was wrong. niri bundles its wiki in the `-doc`
output; that is authoritative and disagreed with binary string-mining.)*

- The protocol is **`ext-background-effect-v1`** — confirmed in the binary as
  `ext_background_effect_manager_v1`. The `org_kde_kwin_blur` strings present in
  the binary are unused smithay code.
- The blur node lives inside a **`background-effect {}`** block, not directly
  under `window-rule`. This config validates against `niri validate`:

```kdl
blur {
    passes 3
    offset 3.0
    noise 0.02
    saturation 1.5
}

window-rule {
    match app-id=r#"^dev\.goober\.WallInOne$"#
    geometry-corner-radius 12
    background-effect {
        blur true
    }
}
```

So the app ships a **translucency setting** and a documented niri snippet.
No `ctypes`, no C shim, no `wl_surface`. This removed the only significant
technical risk in the design.

Caveats:

- The window must be semitransparent or the effect is invisible. Focus ring and
  border can paint over it.
- **`xray` is on by default** whenever blur is active — it sees through to the
  wallpaper only, ignoring windows below, and is much cheaper because niri blurs
  once and reuses the result. `xray false` gives true blur-through-everything
  but is **flagged experimental** and vanishes during open/close animations and
  tiled drags.
- **The xray optimisation does not help this app.** Its premise is that the
  wallpaper changes rarely; the niri docs state that with an animated wallpaper
  it recomputes every frame. Driving video wallpapers is precisely this app's
  job, so blur has real GPU cost whenever dynamics are running. This is an
  independent reason for the "pause dynamics" control to exist.
- `background-effect` is **`Since: 26.04`**. Older niri gets nothing, so the app
  must degrade silently to plain translucency.

**Requirement:** the app must set a stable `app-id` so window rules can match it.
Measured, not assumed: GtkApplication sets the Wayland app-id from the
GApplication id, so the matchable string is `dev.goober.WallInOne`, not the
`wall-in-one` command name. `GLib.set_prgname()` does not affect it. Confirmed
by reading `niri msg -j windows` against a live instance. Note also that KDL
rejects `\.` in a plain quoted string, so the matcher must be a raw string.
Written up in `docs/niri.md`.

### 6. Toolkit and language

**Python**, GTK4 + libadwaita via PyGObject (`pygobject3` 3.56.3,
`libadwaita` 1.9.2, both in nixpkgs).

The case for Rust rested on Wayland protocol access, and finding 5 removed it.
What remains:

- `noctalia_5/wall-in-one-backend/wall-in-one-backend` is **5,493 lines of
  pure-stdlib Python** — zero third-party imports — with a **2,513-line test
  suite**. It already implements Wallhaven search/detail/download/clear,
  motionbgs search/download, palette inventory, bounded fetch, HTTP caching,
  file locking, rate limiting, and hardened path handling. That is the hard part
  and it is already debugged. In a standalone app it gets *simpler*: the
  file-based RPC transport existed only because Luau cannot do networking.
- The workload is not CPU-bound, and the expensive parts are not our code.
  Palette generation is the `noctalia` binary at 0.24 s. Thumbnail decode and
  grid scrolling are the toolkit's C. Library scans and provider fetches are I/O.
- Rust would win on startup (~20 ms vs a few hundred), RSS (~30 MB vs 100+), and
  single-binary distribution. For an occasionally-opened wallpaper manager, none
  of that justifies re-earning fixed bugs.

GTK4 over Qt for one specific reason: the 72 tokens map directly onto a
generated CSS string handed to `Gtk.CssProvider`. Exact, trivial, and
re-appliable on every palette change.

**The real Python risk is not speed — it is drifting back into an untyped
25k-line codebase.** Mitigation: `mypy --strict` in the flake checks from the
first commit. The existing backend is already written with
`from __future__ import annotations` and typed signatures.

---

## Architecture

```
wall-in-one/                    ← this repo (standalone app)
  src/wall_in_one/
    theme/      palette model, noctalia CLI wrapper, template install, CSS gen
    providers/  ← lifted from wall-in-one-backend, RPC transport stripped
    wayland/    output enumeration (niri msg), mpvpaper supervision
    ui/         GTK4 / libadwaita
  templates/    palette.json.tmpl  (the Noctalia user template)
  flake.nix     app package, devShell, checks, VM tests

noctalia-plugin/                ← shrinks to ~350 lines, in the plugins repo
    widget + shortcut only; every control shells out to `wall-in-one ctl <verb>`
```

### The statics/dynamics split

This is the seam that makes colour sync free:

- **Static wallpapers go through Noctalia.** The app calls
  `noctalia msg wallpaper-set`, so Noctalia's own engine does the transition
  *and* regenerates the palette, which fires our template's `post_hook`.
- **Video wallpapers the app owns**, driven directly via mpvpaper.

The "pause dynamics" control is exactly the toggle between those two paths:
stop video, show each entry's paired still, resume later. Still/video pairing
and `capture-still` already exist in the old implementation.

### Plugin control surface

| plugin control | invocation |
|---|---|
| launch | `wall-in-one` |
| stop/start dynamics | `wall-in-one ctl dynamics off` / `on` |
| forward / reverse | `wall-in-one ctl next` / `prev` |
| shuffle on/off | `wall-in-one ctl shuffle toggle` |
| cycle on/off | `wall-in-one ctl cycle toggle` |
| cycle duration | `wall-in-one ctl cycle-interval <sec>` |

No socket client in Luau — the plugin only ever `runAsync`es a CLI verb. The
app owns a Unix socket at `$XDG_RUNTIME_DIR/wall-in-one.sock`; `ctl` is a thin
client for it.

The plugin **ships** `palette.json.tmpl` as a data file (plugins already ship
`scripts/`, `translations/`, `thumbnail.webp`, all materialized under
`~/.local/state/noctalia/plugins/materialized/<source>/<plugin>/`) but does
**not** register it: the Luau host API has `writeFile` and `getConfig` but no
config setter. Registration is `wall-in-one --install-theme-template`, which
does an atomic read-modify-write of `settings.toml` plus `noctalia msg
config-reload`. One tested code path instead of TOML editing in Luau.

---

## Plan

Ordered so the riskiest integration is proven before any bulk code moves.

- [x] **0. Findings** — verify every integration point against live sources.
- [x] **1. Repo + flake skeleton.** devShell, package, `mypy --strict`, `ruff`
      and `pytest` as flake checks.
- [x] **2. Theme pipeline end to end.** Token model, `noctalia theme` wrapper,
      palette → GTK CSS, template install, `ctl reload-palette`. Proven before
      any UI exists.
- [x] **3. App shell.** Adw window with a stable `app-id`, translucency setting,
      live palette applied as CSS. Ship the niri snippet (`docs/niri.md`).
- [x] **4. Library + playlist model.** Scan roots, still/video pairing,
      shuffle/cycle state. Thumbnails still to come.
- [x] **5. Wallpaper application.** Statics via `noctalia msg wallpaper-set`;
      dynamics via mpvpaper. The 886-line `renderer-supervisor` shrank to
      ~200 lines because Python speaks mpv's AF_UNIX IPC directly -- the old
      script needed `socat` or a capable `nc`, and probed for which. Not
      carried over: per-output renderer children, `--auto-mode` (mpvpaper 1.8
      has no such flag), and live volume control.
- [x] **6. Providers.** Lift the 5,493-line backend, strip the RPC transport,
      keep the test suite. The transport was the bulk of it: MotionBGS and
      Wallhaven came across as ~2,000 lines of typed Python behind one
      stubable HTTP seam. Wired up in `browse.Browser` (root selection,
      downloads, bounded preview fetches) and `ui.browse_dialog`; a finished
      download rescans the library, so the file appears in the grid unasked.
- [x] **7. Palette browser.** Built-in / community / custom, live scheme preview
      across all 10 generators, custom palette editing.
- [x] **8. Socket + `ctl`.** Then shrink the Noctalia plugin to widget +
      shortcut and ship `palette.json.tmpl` alongside it. Done in the plugin
      repo: 51,628 lines out, 653 in. Never loaded into a running shell, so
      the host-API surface is matched by pattern against a working plugin
      rather than executed -- that is the outstanding risk.

Steps 1–3 are the vertical slice that proves the colour pipeline. Nothing else
starts until that is green.

### After step 8

The plan above ends at "the pipeline works". What followed is the difference
between that and something worth using daily, found by listing what the app
still could not do rather than by extending the plan.

- [x] **Legacy palettes.** The four schemes in Noctalia's older
      `colorschemes/<Name>/<Name>.json` layout were invisible. Shown under a
      fourth origin, with Apply disabled and the reason stated: the string
      `colorschemes` appears nowhere in the 5.0.0-beta.7 binary, so
      `color-scheme-set` cannot apply one.
- [x] **The Wallhaven key from Settings.** Written 0600 through a temporary
      name, in a GTK-free module so the permissions and the refusals are
      testable without a display. A key the rest of the machine can read is
      refused on the way *in* as well, and says which `chmod` mends it.
- [x] **A still for every video.** `pairing` documented three routes to one and
      only the user's own naming convention could ever happen -- nothing wrote
      a sidecar and nothing filled `Automatic Stills`. Pausing dynamics on an
      unpaired video raised, jumped to an unrelated wallpaper, and left
      Noctalia's palette derived from a picture no longer on screen.
      `library.stills` takes a frame three seconds in, at full resolution, as
      PNG.
- [x] **A bounded thumbnail cache.** Every thumbnail goes through ffmpeg
      because this closure's GdkPixbuf has no webp loader; 230–330 ms per
      wallpaper was being paid on every launch. 256 MB ceiling, LRU eviction,
      a one-minute grace so nothing about to be drawn is thrown away. The
      plugin this replaces left 108 files and 2.4 MB with nothing that could
      remove them.
- [x] **More than one root.** The library was Noctalia's single
      `wallpaper.directory`. Resolution moved inside `session.refresh`, so
      every route into a rescan honours the setting; order is kept because the
      first root receives downloads and generated stills.
- [x] **Taking a wallpaper away.** `MediaItem.deletable` had existed since the
      library model was written with nothing implementing it. Ownership is
      re-derived from disk at the moment of deletion, because the item came
      from a scan that may be minutes old; the user's own file is trashed
      rather than unlinked.
- [x] **Finding one.** Search, kind filter and sort, with the matching in a
      GTK-free `library.filter` and the grid filtering through
      `set_filter_func` rather than rebuilding tiles -- an item whose thumbnail
      failed has no cache entry, so rebuilding would resubmit the same failing
      ffmpeg call on every keystroke.
- [x] **`ctl` reaches the providers.** `providers`, `search`, `download`.
      Handlers may now return a `Deferred`, because the socket server runs
      inside the GTK main loop and a blocking download would freeze the window
      for its duration.
- [x] **Video playback controls.** Mute, volume and what to do with a video
      nobody can see. Audio is retuned over mpv's IPC so the wallpaper does not
      blink; the hidden policy is an mpvpaper launch flag and waits for the
      next video.
- [x] **A launcher entry and an icon**, installed where XDG looks. Fixing this
      turned up two things quietly broken: `nix build` failed in `checkPhase`
      because ffmpeg is only on PATH via the runtime wrapper, so 29 tests were
      skipping and one passed for the wrong reason; and `nix flake check`
      failed outright because `cd`ing into the store leaves ruff unable to
      create its cache.

- [x] **Every missing still, not just the paused one.** `StillMaker` fills the
      rest in after each rescan. The loop needed care: a finished batch causes
      a rescan and a rescan asks again, so a video ffmpeg cannot read would go
      round forever if attempts were not remembered.
- [x] **Taking one away, from the tile.** A menu with "Set as wallpaper" and a
      removal verb named for what it does to that file -- "Remove" for
      something downloaded, "Move to Trash" for the user's own -- and a
      confirmation on only the one that cannot be undone.
- [x] **Favourites**, in their own state file rather than in the settings, with
      the rotation narrowed to them. Narrowing is skipped when it would leave
      nothing to rotate through: a manager that stops changing the wallpaper is
      a worse answer to "you have no favourites right now" than one that falls
      back and keeps working.
- [x] **Per-output wallpapers.** The blocking unknown was whether Noctalia
      could do it at all; `wallpaper-set [connector] <path>` says it can. Both
      halves are aimed, since a still and a video reach the screen by different
      routes. The monitor list comes from `Gdk.Display` rather than from `niri
      msg -j outputs`, so the core is not tied to one compositor.

Still not done: the plugin has never been loaded into a running shell. It has
since been audited statically, which narrows the risk without closing it:

- Every one of the **19 host API symbols** the new plugin uses -- `noctalia.tr`,
  `noctalia.state`, `noctalia.runAsync`, `ui.button` and the rest -- also
  appears in plugins that are installed and working on this machine. There is
  nothing in its surface that has not been seen to run.
- `plugin.toml` is structurally what a working plugin's manifest is: the same
  key set, `[[setting]]` / `[[widget]]` / `[[panel]]` / `[[shortcut]]` blocks in
  the same shapes.
- It declares `plugin_api = 17`. Another plugin in the same source repo
  (`nocvox`) declares 17 and has been *materialized* by Noctalia, but it is not
  enabled, so 17 has been accepted into the plugin store and not observed to
  load. Everything else installed here is 15 or below. This is the one part of
  the manifest that a static check cannot settle.

The remaining risk is therefore narrower than "the host API is matched by
pattern": it is whether `plugin_api = 17` loads on 5.0.0-beta.7, and whether
the four entry points behave once running.

### What the rewrite has not carried across

The plan above was written as "lift the backend and shrink the plugin", and
that is what happened. It was never checked against what the plugin actually
*did*, and the difference is large enough that the 51,390-line deletion in the
plugin repo is not a like-for-like replacement. Nothing in this document
mentioned Wallpaper Engine, Steam, schedules or named playlists before this
section existed, so these were not weighed and dropped -- they went out with
`renderer.luau` and nobody wrote it down.

Read off the deleted plugin's own README, ADAPTERS.md and `plugin.toml`:

| capability | 0.8.0 plugin | this app |
|---|---|---|
| still images | yes | yes |
| local video via mpvpaper | yes | yes |
| **Wallpaper Engine Workshop scenes** | `linux-wallpaperengine`, owned per display | **nothing** |
| Wallhaven shop | yes | yes |
| MotionBGS shop | yes | yes |
| **Steam Workshop shop** | browse and acquire | **nothing** |
| still/motion pairing | yes | partial -- video only |
| automatic stills | video *and* Workshop screenshot | video only |
| manual still override | paged picker plus path escape hatch | **nothing** |
| **per-item palette policy** | mode plus builtin/generated/community/custom/keep | **nothing** |
| managed library and ownership | yes | yes |
| **named playlists** | visual editor, stable IDs, drag to reorder | **nothing** -- `Playlist` here is a cursor |
| **schedules** | month/weekday/time rules, ordered, lowest match wins | **nothing** |
| **per-display assignment** | pinned default plus schedule per output | partial -- one global output field |
| per-display engine settings | layer, mute, hwdec, auto-pause mode, FPS, scaling | partial -- global, no Workshop half |

Three of those are structural rather than additive, and one of them changes a
type everything else is built on.

**A pairing is the unit, not an attribute of a video.** Today `MediaItem` is a
file and `paired_still` is an optional field that only videos ever set. What is
wanted is that *every* library item resolves to a bundle -- a representative
still, an optional motion source, and a palette policy -- with the still
synthesized by default for both moving kinds and replaceable by hand, and no
separate "create a pairing" step. The old plugin arrived at exactly this and
said so plainly: "Library items synthesize a validated default bundle. Saving a
customization creates or updates the stable profile with `customized = true`."
That is the shape to copy, because it is the one that makes the common case
free and the customized case durable.

**Identity has to survive the file moving.** Profiles are keyed by static path
or by dynamic medium/source identity, so a customization updates one record
rather than accumulating duplicates. Nothing here has an identity for a
wallpaper beyond its path.

**One shell, one palette.** Noctalia has a single palette, so per-display
palettes cannot all be live at once; the old plugin nominated a leader display
and otherwise let the latest successful apply win. Any per-display work here
inherits that constraint.

### Plan, part two

Ordered so that the type change lands before anything depends on it, and so
each step is independently useful if the next never happens.

- [x] **9. The pairing model.** `MediaItem` becomes a file record; a new
      `Pairing` carries representative still, optional motion source, and
      palette policy, keyed by a stable identity rather than a path. Every
      scanned item synthesizes a default pairing; saving one marks it
      customized so a later default change cannot silently overwrite it.
      Persisted beside the favourites, atomically, degrading to defaults when
      unreadable. This is a migration: `library.pairing` and
      `MediaItem.paired_still` are the read side of the old model and both have
      callers.
      **Done.** `library.pairings` owns the record, the choice and the file;
      `library.pairing` shrank to the conventions that synthesize a default,
      and its `apply` is gone rather than left as a second implementation of
      the same drop-logic. Pairing happens exactly once, inside the scan, with
      the store's records carried in -- resolving a second time elsewhere
      recomputed the defaults from disk and overwrote the first pass, which is
      how the first attempt at this failed. `MediaItem.paired_still` survives
      as the derived view the applier and the grid already read.
- [x] **10. Per-item palette policy.** Each pairing resolves dark/light/auto
      plus one of builtin, wallpaper-generated, community, custom, or
      keep-current. The default stays adaptive-from-wallpaper, which is what
      happens today. `theme/palettes.py` already discovers all four sources and
      `theme/source.py` already resolves them; what is missing is storing a
      choice per item and applying it in the order still, mode, palette,
      renderer.
      **Done.** `PalettePolicy` is `kind`, `name` and `mode`, stored as the
      wire form `kind` or `kind:name` so a source Noctalia adds later survives
      a round trip through a build that predates it. `adaptive` is Noctalia's
      own `wallpaper` source named by the generator from settings, so
      "adaptive" and "generated from this wallpaper with m3-tonal-spot" are one
      request rather than two code paths. The order matters and is tested:
      Noctalia derives adaptive colours from whatever wallpaper is set, so
      asking before the still lands generates them from the previous picture.
      A palette that will not apply is never fatal -- the wallpaper is already
      on screen by then.
- [x] **11. Manual still override.** A picker over indexed stills plus an
      absolute-path escape hatch. `library/stills.py` already writes the
      sidecar that records one, so this is UI and a verb, not new machinery.
- [x] **12. Named playlists.** Named, ordered, persisted, with stable entry IDs
      so reordering and rebinding do not lose an entry's identity. The existing
      `Playlist` becomes the runtime cursor over whichever list is active,
      which is roughly what it already is.
- [x] **13. Schedules.** Month, weekday and local-time rules evaluated in
      visible order, lowest match winning, with a pinned default when none
      match. Resolution must be pure and testable without a clock; the timer
      belongs in the UI layer, as the cycle timer already does.
- [x] **14. Per-display assignment.** *Done, with one caveat — see below.* Each output gets a default playlist and
      its own schedule and engine settings. Requires the leader-display rule
      above for palettes, and turns the single `output` setting into a map.
- [x] **15. Wallpaper Engine.** The survey moved the goalposts in a good
      direction, and both halves are in.
      **Surveyed first, against the 49 items installed here.** 45 of them are
      `type: video` whose entry file is a real `.mp4` -- those need nothing but
      mpvpaper, which this app already drives, and they are in the library now.
      Only **4** are `scene`, whose `file` names a `scene.json` that does not
      exist on disk because the content is packed inside `scene.pkg`; those are
      what actually need `linux-wallpaperengine`, and they are reported by
      `workshop.unplayable` rather than hidden. The type is spelled both `video`
      and `Video` in the real content, so it is compared case-folded -- trusting
      the casing hid eight wallpapers. Steam's files are `Ownership.USER`
      without exception, so removal refuses them.
      **The scene renderer exists** (`wallpaper.scenes`): the command line,
      process ownership, and `--screenshot` capture, all verified against the
      real engine -- a 3.1 MB render of "China Town" in 2.1 seconds.
      **The open question answered itself.** This machine runs Noctalia's
      `linux-wallpaperengine-controller` plugin, and its engine had been
      rendering the desktop for two and a half hours. The engine is
      single-instance per output, so this app does *not* take it over by
      default; `own_scene_renderer` is how somebody says otherwise, and a
      foreign instance on the same output is detected and reported rather than
      shouldered aside. Capturing a still is exempt -- window mode, no output.
      `Kind.SCENE` runs through the model now, so all 49 wallpapers are
      library items. A scene is keyed by its Workshop id rather than by its
      directory, so a reinstall that moves the directory does not lose the
      still somebody chose. `MediaItem.title` exists because a scene's
      directory is called `1647046763` and its wallpaper is called "Toothless
      in a Field" -- which the video items get too.
      Bringing someone else's collection into the library turned up a real
      hole in removal: a Workshop wallpaper is `Ownership.USER`, so `remove`
      correctly refused to *delete* it -- and then fell through to `trash`,
      which moved a 129 MB file out of Steam's directory on this machine.
      "Not ours to delete" and "not ours to move" are different claims. Neither
      verb will now touch anything outside the configured roots.
      The largest piece and the one with a real
      question in front of it. It needs a third renderer alongside mpvpaper and
      Noctalia stills, Workshop scanning under Steam's directories, and
      `--screenshot` capture for pairings. **Open: this machine already runs the
      separate `linux-wallpaperengine-controller` Noctalia plugin.** Owning the
      renderer here means two things driving one `linux-wallpaperengine`, which
      the old plugin avoided by owning it outright and telling users to disable
      the other. That is a decision to take deliberately, not to discover.
- [x] **16. Steam Workshop shop.** *Done.* Browse and acquire, which in the old plugin
      meant links out to Steam rather than a scraper. Depends on 15 for
      anything to do with what it acquires.
- [x] **17. Browsing worth using.** *Done — see "How step 17 was proven".* The stores no longer live inside the
      Noctalia shell, so the limits that shape them are self-imposed and can
      go. The dialog is still the plugin's: prev/next page buttons, a flow of
      cards, one download button each. Meanwhile the provider layer already
      parses ratios, colours, `order`, `seed`, the toplist range and a
      per-wallpaper detail response carrying tags and a palette — and the UI
      offers sorting, categories, purity and "at least". **Most of this step is
      surfacing capability that is already written and untouched**, which is
      also why it is worth doing before anything that adds more of it.
      What it means concretely:
      - *Continuous scrolling* in place of paging. Pages were a way to bound
        the work per request; nothing bounds it now. The next page loads as the
        bottom approaches, and the page number becomes an implementation detail
        rather than two buttons.
      - *A detail view.* Clicking a card should give the full-size preview and
        what the provider already knows about it — resolution, ratio, file
        size, tags, colours, source URL. The detail endpoint is implemented and
        has no caller.
      - *The filters that exist.* Ratios, colours, ordering, toplist range.
        Wallhaven's colour search in particular pairs with this app's whole
        premise: find a wallpaper by the palette it will generate.
      - *Multi-select and a download queue.* Downloading is one-at-a-time and
        modal-ish; browsing should continue while things land, with progress
        per item and a failure that does not take the queue with it.
      - *Library awareness.* A result already in the library should say so
        rather than offer itself again. The library knows; the browser does not
        ask.
      - *Keyboard navigation*, because a grid of six hundred results is not a
        mouse target.
      - *A thumbnail cache with a disk tier.* `providers.cache` is a bounded
        dict, deliberately — the predecessor's JSON file existed only because
        each request was a fresh process. A long-lived app does not need the
        file for *sharing*, but re-browsing after a restart still re-downloads
        every preview, and that is now the only reason left to keep one.
      Explicitly not carried across, still: the routed hub panel and the
      external-backend install flow. Dropping the CPU budget is not a reason to
      rebuild the things the budget was not the problem with.

Not planned, and worth saying so: the routed hub panel and the
external-backend install flow are artefacts of living inside a Luau plugin
with a CPU budget. This app is the backend; it does not need to reinstall
itself.

The provider-preview cache was on that list and has come off it, under step
17. The reasoning it was struck for is still sound — the predecessor cached
previews to a JSON file because each request was a fresh process that shared
nothing with the last, and a long-lived app shares everything, so the file,
the lock and the schema version are all answers to a question this app does
not have. What that argument does not cover is *restarting*. Sharing within a
session is free; nothing carries across sessions, so re-opening the browser
re-downloads every preview from the CDN. That is a second, weaker reason for a
disk tier, and it survives the first one being wrong.

### Two moving renderers, not one

The tempting simplification is to drop mpvpaper and let `linux-wallpaperengine`
play everything. It was measured rather than assumed, and it does not work out.

The engine will not take a video file. Handed one directly it answers `the
specified mount cannot be handled by any of the filesystem adapters`. What it
*will* take is a **directory containing a `project.json`** naming the file —
a synthesised one, with no Workshop id anywhere, rendered a plain test clip
without complaint. So unification is possible, at the price of writing a shim
directory for every ordinary video in the library, and then keeping those shims
in step as files are added, renamed and removed. That trades a renderer for a
cache-invalidation problem, and gives up mpv's hardware decode and its IPC
socket — which is what makes volume and mute changeable on a playing wallpaper
rather than only at start.

So both renderers stay, and the cost is that **every hop between them has to
hand over cleanly**. Three rules, one per direction:

- *scene → video*: the engine is stopped before mpvpaper starts. This was the
  bug. `Renderer.start` stops mpvpaper itself, so the video→video and
  video→scene cases were covered by accident and the missing case looked like
  symmetry that was already there. It was not: a scene followed by a video left
  two programs drawing on one output.
- *video → scene*: mpvpaper is stopped before the engine starts.
- *either → still*: both are stopped, which `_set_still` already did.

And a fourth, for the transition that fails: a scene the app may not start is
refused **after** its still and palette have been applied, so mpvpaper is
stopped *before* the refusal rather than after. Otherwise the previous video
keeps playing over the next wallpaper's colours — the one outcome that belongs
to neither item. Stopping first degrades to the scene's own still, which under
the pairing model is a legitimate way to show that wallpaper, not a failure.

That last case is the common one rather than the exotic one: owning the scene
renderer is off by default, and on the development machine another engine holds
`eDP-1` permanently. Four of the 49 installed Workshop items are scenes, so an
automatic rotation meets this path regularly and must stay on its feet.

### How the slice was proven

All three resolution tiers were exercised against the real Noctalia, with every
write confined to a sandboxed set of XDG directories — the user's own
`~/.local/state/noctalia/settings.toml` was copied, never edited.

| tier | check | result |
|---|---|---|
| 1 template | `--install-theme-template` into a copy of the real 679-line settings file | block written, file still parses, `builtin_ids` and `wallpaper.directory` untouched, 679 → 688 lines |
| 1 template | render it the way Noctalia does: `noctalia theme <wp> -r <in>:<out>` | 2610 bytes, valid JSON, 72 tokens |
| 1 template | `--print-palette` in the same sandbox | `origin: template`, 72 tokens, and the colours are **identical** to `noctalia theme --both` run directly |
| 2 generated | `--print-palette` with no template installed | read `m3-fruit-salad` from `color-scheme-get` and regenerated all 72 tokens |
| 3 fallback | `--print-palette` with neither | neutral dark palette, 34 tokens, missing ones reported |
| uninstall | `--uninstall-theme-template` | our block gone, nothing else changed |
| app-id | launch, then `niri msg -j windows` | `dev.goober.WallInOne` |

Gates: `ruff` clean, `mypy --strict` clean, `pytest` green.

### How steps 4–5 were proven

Scanning and playback were exercised against the real library, with the live
wallpaper captured beforehand and restored afterwards.

| check | result |
|---|---|
| `scan()` with no arguments | found the root from Noctalia's `[wallpaper] directory`, classified all 5 items |
| ownership | both MotionBGS downloads `managed`; a file dropped into a managed directory by hand stays `user` |
| dynamics off | playable count 5 → 3, exactly the unpaired videos dropping out |
| `ctl next` on a video | mpvpaper started; mpv answered `get_property path` over the IPC socket with the right file |
| `ctl dynamics off` mid-video | renderer stopped, fell back to a still |
| `ctl quit` | no mpvpaper or mpv process left, both sockets unlinked |

One bug came out of that live run: pausing dynamics on a video with no still
raised, leaving the setting changed but the video still playing. Fixed, with a
regression test.

### How step 7 was proven

The palette browser was built against the real cache and the real generator,
read-only throughout: nothing was written to `~/.config/noctalia/palettes`,
`~/.local/state/noctalia/community-palettes`, or `settings.toml`. Every test
write goes to `tmp_path`.

| check | result |
|---|---|
| file format | diffed `Oxocarbon.json` against `noctalia theme --theme-json <it> --both`; the mapping table in finding 3 is those equalities, not a guess |
| discovery | ran against the live directories: 10 built-ins, both cached community palettes, 42 tokens each, `.catalog` ignored, nothing skipped |
| community naming | `Osaka%20jade.json` decodes to `Osaka jade`, which is exactly what `[theme] community_palette` holds |
| built-ins | `grep` of the binary finds the ten names and no colours next to them, so they are listed without swatches by design rather than by omission |
| bounded | ceiling on entries, `MAX_PALETTE_BYTES` per file, and malformed / unreadable / oversized files land in `Discovery.skipped` |
| read-only | `save_edits` on a community entry raises and the file is byte-identical afterwards; `write_custom` resolves only inside the custom directory, so `../escape` never reaches the filesystem |
| off-thread previews | `SchemePreviewLoader` drove a real `noctalia theme` and delivered through `GLib.idle_add` into a pumped main loop |
| widget API | every method, signal and construct property the browser uses checked against the installed GTK 4.22 / libadwaita 1.9 by introspection |

Gates: `ruff` clean, `mypy --strict` clean, 182 tests green (36 new).

Since revisited. The browser has been opened in the running app -- over
`org.gtk.Actions` on the session bus, which is also how the other dialogues get
driven from a terminal -- and its tree read back: four groups (Built-in,
Community, Custom, Legacy), all ten built-in palettes discovered with nothing
skipped, and a card for every one of the ten generators with its swatch strip
and buttons. What is still unconfirmed is only how it *looks*: the layout has
been checked for existing, not for being right.

### How the post-step-8 work was proven

Against the real machine, with the wallpaper and colour scheme captured before
each live run and checked unchanged after. Nothing was written to the user's
Noctalia directories; every write went to a sandboxed XDG home or `tmp_path`.

| claim | check | result |
|---|---|---|
| thumbnail cache pays for itself | generate then look up four real wallpapers | 230–330 ms cold, ~0.1 ms cached |
| eviction is LRU and bounded | age four entries past the grace, prune to a chosen ceiling | evicted oldest-first down to 90% of the ceiling; a foreign file in the directory survived both `prune` and `clear` |
| deletion refuses what is not ours | ran `manage.remove` over a copy of the real library | both real marker formats recognised, sidecars taken along, markers left, the user's own files refused |
| search and sort | drove the real grid over the real library | `"vil snow"` = `"snow vil"`, case-folded, `narrows` false for a sort alone, newest Aug 6 → Jul 18, largest 24.7 MB → 0.46 MB |
| `ctl` reaches the providers | real socket, real GTK loop, both providers | `search wallhaven aurora` returned the same 741-result page the dialog shows; `search motionbgs naruto` returned 250; unknown provider → `unknown-provider`, exit 1 |
| key status never lies | six combinations of environment and saved key | absent, valid and malformed each distinguished, and the key never appears in a message |
| the package installs | `nix build` then read `$out` | entry and icon in `share/applications` and `share/icons/hicolor/scalable/apps`, `Exec` rewritten to the wrapped binary |
| the whole suite runs packaged | `nix build` check phase | 638 passed, 0 skipped, where it had been 608 passed / 29 skipped / 1 failed |
| every gate | `nix flake check` | all five checks pass |

| the grid survives a real library | 600 synthetic wallpapers, profiled | first show 367 ms, rescan 15 ms, ~196 MB resident. Three costs found by measuring rather than reading: a full tile rebuild on every rescan (620 ms), `Gdk.Texture` decoding on the main thread (372 ms), and eager popover construction (160 ms) |
| the app survives its own socket failing | a 95-character runtime directory | window comes up, warning on stderr. Before the fix this killed startup outright -- `Gio.SocketService` bound without complaint and the `chmod` two lines later raised `FileNotFoundError` inside `do_startup` |
| `ctl` cannot be talked into deleting the wrong thing | `remove` against a live instance | `/etc/passwd` refused as not in the library, a relative path refused, a download deleted with its sidecar, one of the user's own refused rather than unlinked when the trash was on another filesystem |
| stills come out usable | took one from the real 24.7 MB 4K video | 1.0 s, full 3840x2160, mean luma 0.31 -- a real frame, not the black opening the three-second seek exists to avoid; found afterwards by both the sidecar and the directory convention |

| favourites survive the round trip | starred through the real buttons, reloaded | view narrows, search combines with it, persists, and reflecting the store fires no spurious toggles |
| removal refuses correctly | menu + `manage` over a copy of the real library | right verb per ownership, sidecar taken along, the user's own file refused by name and left on disk, trashing it instead leaves it recoverable |
| stills fill themselves in | launched against a copy, untouched | still and sidecar written within 25 s, badge went from "Video (no still)" to "Video", grid refreshed itself |
| the output picker | three settings against the real display | offers "All outputs" plus `eDP-1`, keeps an unplugged connector selected rather than resetting it |

Not proven: that two monitors show different things, because only one output is
connected here -- what is checked is that the connector reaches both commands.
The star also revealed something no test would have: in Papirus,
`starred-symbolic` and `non-starred-symbolic` are both solid stars, so the
state had to be carried by colour from the palette rather than by the icon.

---

## Verification sources

| claim | source |
|---|---|
| CPU budgets | `noctalia/src/scripting/script_runtime.cpp:39-41` |
| palette tokens (72) | `noctalia/src/theme/tokens.h` |
| schemes (10) | `noctalia/src/theme/scheme.h` |
| generator is pure | `noctalia/src/theme/cli.h`, `palette_generator.h` |
| community palette API | `noctalia/src/theme/community_palettes.cpp:26-27` |
| custom palette dir | `noctalia/src/theme/custom_palettes.cpp:215-220` |
| user templates in schema | `noctalia/src/config/config_types.h:1411-1423` |
| template syntax | `noctalia/share/noctalia/assets/templates/qt/qtct.conf` |
| qt output paths | `noctalia/share/noctalia/assets/templates/builtin.toml:157-159` |
| niri blur config | `niri-doc/share/doc/niri/wiki/Configuration:-Miscellaneous.md:332-410` |
| niri background-effect | `niri-doc/.../Configuration:-Window-Rules.md:925-1010`, `Window-Effects.md` |
| niri blur protocol | `ext_background_effect_manager_v1` in the niri binary |

### How step 17 was proven

Four of the seven pieces were verified against the live sites rather than
against a fake, and three of those turned up defects a unit test would have
agreed with.

| Claim | How it was checked | Result |
| --- | --- | --- |
| Results already downloaded are recognised | `owned.read` over the real library | 2 found — the Wallhaven one by `id`, a MotionBGS one by page URL |
| A colour search works end to end | Live Wallhaven: toplist + last week + `#0066cc` + 1920x1080 | 19 results, correct URL |
| `describe` returns something worth showing | Live, both providers | Wallhaven: 10 facts, 6 tags, 5 colours. MotionBGS: duration and two qualities |
| The preview cache is faster than the CDN | Live fetch, sandboxed `XDG_CACHE_HOME` | 112.8 ms → 0.1 ms, byte-identical |

**What the live checks caught that the fakes did not.** Wallhaven returns
colours as `#424153`, and neither the colour filter nor a swatch takes the
hash. MotionBGS publishes durations as ISO 8601, so a 27-second loop rendered
as `PT26.9S`. And `to_displayable` passes JPEG through untouched, so caching
previews through the existing PNG-only integrity check would have called every
Wallhaven preview corrupt — caching nothing, while appearing to work.

**Three defects found by writing the tests rather than the code.** A first page
shorter than the window never scrolls, so without an idle check after the first
result the second page is never requested at all. Overlapping pages showed the
same wallpaper twice and left `_card_for` picking whichever copy it met first.
And a page whose results have all been seen would be followed by another, and
another, at scroll speed.

**One caught by the type checker.** The card's `pick()` was overriding
`Gtk.Widget.pick(x, y, flags)` — GTK's hit-testing. `mypy --strict` reported it
as a signature mismatch; it would have shown up as a widget that could not be
clicked.

The GUI tests build a real `BrowseDialog` and drive the whole loop — search,
append, dedupe, stop, pick, queue, keyboard — through the main loop against a
scripted browser. They carry the `gui` marker, so the Nix check sandbox skips
them, and they stub `registry.describe` so they cannot read real credentials.

Not carried across, deliberately: the routed hub panel and the
external-backend install flow.

### How steps 14 and 16 were proven, and what 14 still owes

**16** needed no proof beyond reading the constraint: subscribing to a Workshop
item has no unauthenticated API, so a scraper could browse and never install.
Steam already owns the account, the payment and the download, so the app links
out. What *was* worth testing is the reader underneath it, which had no tests at
all — and writing them found that `extra_roots` adds to the default Steam paths
rather than replacing them, so a test pointing at a temp directory was quietly
scanning the developer's real Steam and getting 49 wallpapers back. "No Steam
installed" passed for entirely the wrong reason. `include_defaults=False` closes
it.

**14** is in two halves, and only the first is fully proven.

| Half | State |
| --- | --- |
| Discovery — what the connectors are | Verified against real niri 26.04: `eDP-1`, BOE 0x0A9B, 1706x1066 at scale 1.5 |
| Assignment — which playlist a screen shows | Store tested; listing, not-attached marking and delete-cleanup exercised live |
| Two screens actually showing two playlists | **Not verified. This machine has one output.** |

The multi-output parsing is exercised against recorded JSON from a two-monitor
layout, which proves the shape and the ordering rule and nothing about how it
feels. The ordering rule is deliberate: niri answers with a JSON object, and
while insertion order happens to be preserved, nothing in the protocol promises
the same order twice — a display list that reshuffles between openings is worse
than one in an arbitrary but fixed order.

Two decisions worth keeping written down. A screen is assigned a *playlist*
rather than a wallpaper, because a wallpaper pinned to a screen is a screen that
never changes again. And an assignment for an unplugged screen is kept and
listed rather than pruned, because unplugging a dock at the end of the day
should not forget the arrangement — which only works if the entry stays visible.

One defect came out of running it rather than reading it: `displays` printed the
stored playlist id where a person expects its name. Storing the id is correct —
it survives a rename — so the fix was to resolve it at the point of display, and
to label an id that no longer resolves rather than print a bare hash.
