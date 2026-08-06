# Wall-in-One

A wallpaper manager for Wayland, written in Python with GTK4 and libadwaita.

It manages stills and video wallpapers, keeps its own colours in sync with
[Noctalia](https://github.com/noctalia-dev/noctalia-shell)'s active palette, and
is driven either from its window or from a small control socket — which is how
the companion Noctalia plugin talks to it.

**Status: early but working.** The colour pipeline, the library and playlist
model, and wallpaper application (stills and video) are built and verified
against a live system. Thumbnails, the palette browser, and searching two
wallpaper sites are built on top of that. See [`DESIGN.md`](DESIGN.md) for the
findings this is built on and the plan.

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

## The library

Roots default to Noctalia's own `[wallpaper] directory`, so the two agree about
what the library is without configuring it twice.

Stills are set through Noctalia. Videos are played by mpvpaper, with the paired
still set underneath first — so if the renderer dies the right image is still
on screen, and Noctalia's palette matches what the video looks like.

A video pairs with a still three ways, in order: a `<video>.wall-in-one.json`
sidecar naming one; a file of the same name under `Wall-in-One/Automatic
Stills/`; or a sibling named `foo-still.png` (or plain `foo.png`) next to
`foo.mp4`. A still that exists only to represent a video is not listed
separately — otherwise the same picture turns up twice in the rotation.

`dynamics off` pauses videos and shows their stills instead. Videos with no
still simply drop out of the rotation until dynamics come back on. Blur is
markedly more expensive over an animated wallpaper, so this is a performance
control as much as a battery one — see [`docs/niri.md`](docs/niri.md).

Files are only ever considered deletable when we downloaded them: a directory
marker says we made the directory, and a per-file sidecar says we fetched that
particular file. Both are required, so anything you drop into a managed
directory by hand stays yours.

## Finding wallpapers

The search button in the header — or **Find wallpapers** in the menu — opens a
dialog that searches two sites: [Wallhaven](https://wallhaven.cc) for stills and
[MotionBGS](https://motionbgs.com) for video wallpapers. Each result is a card
with a preview and a download button.

Downloads land in `Wall-in-One/Wallhaven/` or `Wall-in-One/MotionBGS/` beneath
the first library root — the same directory the library was scanned from, so
they arrive inside the library rather than beside it. The library is rescanned
when a download finishes, so the file shows up in the grid without being asked
for. Each download writes both the directory marker and the per-file sidecar
described above, which is what makes it deletable from the app.

Wallhaven works without an API key. The one thing a key buys is NSFW results:
without one, that rating is greyed out in the filters, because sending it
anyway gets a silent downgrade and an unexplained empty grid. Supply a key
through `WALLHAVEN_API_KEY` or in `~/.config/wall-in-one/wallhaven-api-key`.
MotionBGS needs no credentials at all.

[`docs/browsing.md`](docs/browsing.md) covers the filters each site
understands, MotionBGS's paging constraints, and what the download path checks
before it writes anything.

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
