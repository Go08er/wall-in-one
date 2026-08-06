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
      stubable HTTP seam. Not yet wired into the session or the UI.
- [x] **7. Palette browser.** Built-in / community / custom, live scheme preview
      across all 10 generators, custom palette editing.
- [x] **8. Socket + `ctl`.** Then shrink the Noctalia plugin to widget +
      shortcut and ship `palette.json.tmpl` alongside it. Done in the plugin
      repo: 51,628 lines out, 653 in. Never loaded into a running shell, so
      the host-API surface is matched by pattern against a working plugin
      rather than executed -- that is the outstanding risk.

Steps 1–3 are the vertical slice that proves the colour pipeline. Nothing else
starts until that is green.

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

Not proven: the browser has never been on screen. Layout, the ten-card grid,
and the colour-picker rows are correct by construction and by API check only.

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
