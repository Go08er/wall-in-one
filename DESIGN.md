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
- [ ] **4. Library + playlist model.** Scan roots, still/video pairing,
      thumbnails, shuffle/cycle state.
- [ ] **5. Wallpaper application.** Statics via `noctalia msg wallpaper-set`;
      dynamics via mpvpaper supervision (port `renderer-supervisor`).
- [ ] **6. Providers.** Lift the 5,493-line backend, strip the RPC transport,
      keep the test suite.
- [ ] **7. Palette browser.** Built-in / community / custom, live scheme preview
      across all 10 generators, custom palette editing.
- [ ] **8. Socket + `ctl`.** Then shrink the Noctalia plugin to widget +
      shortcut and ship `palette.json.tmpl` alongside it.

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
