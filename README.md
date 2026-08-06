# Wall-in-One

A wallpaper manager for Wayland, written in Python with GTK4 and libadwaita.

It manages stills and video wallpapers, keeps its own colours in sync with
[Noctalia](https://github.com/noctalia-dev/noctalia-shell)'s active palette, and
is driven either from its window or from a small control socket — which is how
the companion Noctalia plugin talks to it.

**Status: early.** The colour pipeline is built and verified end to end. The
library, playlist, and provider layers are not written yet. See
[`DESIGN.md`](DESIGN.md) for the findings this is built on and the plan.

## Why it exists

This started as a Noctalia Luau plugin and outgrew it. Luau caps a function at
200 locals — and a plugin entry file *is* one function, so the whole plugin
shares that budget — and meters each callback against a CPU deadline (12 ms for
updates, 25 ms for callbacks). Those two limits trade against each other:
factoring code out to save locals adds call overhead against the deadline. A
wallpaper manager with providers, a library, and a palette browser does not fit.

So the manager became an application, and the plugin shrinks to what a plugin is
good at: a widget and a few shortcuts that poke the app.

## Colour sync

Noctalia's palette generator is a pure CLI — `noctalia theme <image>` is a
deterministic function of its arguments, with no running shell required. That
makes the whole colour story straightforward, and the app resolves its palette
in three tiers:

1. **Template** — Noctalia renders its live 72-token palette into
   `~/.local/state/wall-in-one/palette.json` and runs
   `wall-in-one ctl reload-palette`. This is the only tier that can see a
   *built-in* palette such as Gruvbox or Nord, because those are compiled into
   the Noctalia binary and never exposed to the CLI.
2. **Generated** — if the current palette comes from the wallpaper, regenerate
   it by running `noctalia theme` on that same wallpaper. Byte-identical to
   what Noctalia itself produced.
3. **Fallback** — a neutral dark palette, so the app always starts.

Tier 1 needs one-time setup:

```console
$ wall-in-one --install-theme-template
$ noctalia msg templates-apply
```

That registers a `[theme.templates.user.wall-in-one]` entry in Noctalia's
settings. It is a real schema field, so Noctalia round-trips it through its own
settings writes rather than dropping it. The block is fenced with markers, the
previous file is backed up first, and a hand-written entry under the same id is
never overwritten. `--uninstall-theme-template` removes it again.

Check what you have with `--theme-status`, `--print-palette`, or `--print-css`.

## Translucency and blur

The app draws its own translucency; blur behind it is the compositor's job. On
niri 26.04 that is a four-line window rule. See [`docs/niri.md`](docs/niri.md)
— it covers the app-id to match, the xray caveat that matters when a video
wallpaper is running, and what to do on older niri.

## Control socket

Every verb is one line of JSON over a socket in `$XDG_RUNTIME_DIR`. The Noctalia
plugin drives the app entirely through these.

```console
$ wall-in-one ctl next
$ wall-in-one ctl prev
$ wall-in-one ctl random
$ wall-in-one ctl shuffle on|off|toggle
$ wall-in-one ctl cycle on|off|toggle
$ wall-in-one ctl cycle-interval 600
$ wall-in-one ctl dynamics on|off|toggle
$ wall-in-one ctl reload-palette
$ wall-in-one ctl status
$ wall-in-one ctl quit
```

Exit code 3 means no instance is running.

## Install

```console
$ nix run github:goober/wall-in-one     # once published
$ nix develop                           # dev shell
$ python -m wall_in_one
```

Noctalia is deliberately *not* a dependency. Without it the app falls back to
tier 3 and everything except colour sync still works.

## Development

```console
$ nix develop
$ pytest tests -q
$ mypy --strict src tests
$ ruff check src tests && ruff format --check src tests
```

All three run as flake checks: `nix flake check`.

Tests that need a display are marked `gui`; tests that need a live Noctalia are
marked `noctalia`. The packaged build runs neither.

Anything touching Noctalia's settings file is tested against a sandboxed set of
XDG directories — never the real one.

## Licence

MIT. See [`LICENSE`](LICENSE).
