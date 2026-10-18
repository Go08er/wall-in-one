# Packages, install kinds and the wallpaper renderers

From 0.4.0 Wall-in-One never ships a wallpaper renderer. mpvpaper (video) and
linux-wallpaperengine (Wallpaper Engine scenes) are installed beside the app,
by the system, and the app finds and controls what is there. FFmpeg stays in
the package: it makes thumbnails and stills, and is not a wallpaper manager.
Every build also says what kind of install it is, so that the companion can
tell a Nix install from a distribution package, and two of them at once from
one.

## Install kind

A build-time value, one of three:

| Kind | Built by |
|---|---|
| `nix` | this flake's packages (`wall-in-one`, `wall-in-one-service`) |
| `system` | a distribution package built from source, such as the Arch PKGBUILD |
| `source` | anything else: a checkout, a pip install, a test run. The checked-in default. |

### Where it is set

The two halves are built separately, so each has its own mechanism, and a
check makes sure they agree.

- **Python.** `src/wall_in_one/install_kind.py` holds the line
  `INSTALL_KIND: Final[InstallKind] = "source"`. A package rewrites that one
  line before it builds: the flake's package with
  `substituteInPlace --replace-fail` in `postPatch`, a PKGBUILD with `sed` in
  `prepare()`. The value is then part of the installed module, which is the
  same file whatever imports it: the console script, the package's own test
  run, the `golden-profile` check's interpreter, a subinterpreter worker.
  An environment variable that the wrapper exports at run time would not
  be the same thing: only the Nix package has a wrapper, and a module
  imported any other way (a test, `python -m`, a subinterpreter that starts
  from a clean environment) would not see it.
- **Rust.** `option_env!("WALL_IN_ONE_INSTALL_KIND")` when the service is
  compiled, `source` when it is unset. Any other value fails the build. The
  flake sets it on the service's derivation; a PKGBUILD exports it in every
  function that runs cargo (`build()` and `check()`): cargo rebuilds when the
  variable changes, so a `check()` without it would replace the binary that
  `package()` installs with a `source` one.

`tools/check-install-kind.sh <kind> <wall-in-one> <wall-in-one-service>` runs
both programs' `--version` and fails unless both report `<kind>`. The flake's
`desktop` check runs it on the installed package; a PKGBUILD runs it on what it
installed into `$pkgdir`.

### Where it is reported

- `wall-in-one --version` and `wall-in-one-service --version` print a second
  line, `install kind: <kind>`. The first line is unchanged, so anything that
  matches it still does.
- The service's `status` has a new top-level `install_kind`. The companion
  polls the service's socket, not the Python CLI, so this is the value it
  reads. `ctl status` prints the service's reply as it is, so it reports the
  kind of the *service* that answered.
- `wall-in-one --update-status` reports the app's own kind under
  `installed.install_kind`, next to the running service's whole status.

The status addition is additive. `status_version` stays 2: a new key with a
fixed value says nothing a client has to understand, and 0.3 clients and the
companion read only the keys they know. A 0.3 service has no `install_kind`;
the companion falls back to telling the kinds apart by where the programs are.
The key and its value (at most six bytes) are a fixed part of the status, the
part both the service's and the compiler's status budgets reserve 256 KiB
for (`STATUS_STRUCTURAL_RESERVE`), so neither budget changes.

## The renderers are external

The package's closure and its wrapper's `PATH` have FFmpeg and no renderer.
The flake keeps two lists: `bundledTools` (FFmpeg), which the wrapper puts on
`PATH`, and `wallpaperRenderers` (mpvpaper, linux-wallpaperengine), which only
the package's test run, the dev shell and the VMs see. The VMs get them the
way a user does, through the NixOS module. The `package-closure` check fails
if either renderer is in the installed package's closure, or FFmpeg is not.

Who installs them:

- **NixOS and home-manager:** the flake's modules, by default (below).
- **Arch:** the PKGBUILD lists them as optional dependencies (both are in the
  AUR).
- **Anything else:** the distribution's packages, or any install that puts the
  program in one of the places below.

## Finding a renderer

`runtime.toml` names each program by absolute path, and every compiler (the
window, `--write-config` from a terminal, the service's start-up preparation
under the user manager) must compile the same bytes from the same authoring
state, whatever its own `PATH`. The places every caller shares come first:

1. The Nix profiles, in NixOS's login order: `~/.nix-profile/bin`,
   `~/.local/state/nix/profile/bin`, `/etc/profiles/per-user/$USER/bin`,
   `/nix/var/nix/profiles/default/bin`, `/run/current-system/sw/bin`.
2. `~/.local/bin`, `/usr/local/bin`, `/usr/bin`, `/bin`.
3. Every other absolute directory on `PATH`, in `PATH` order.
4. The Nix store directories on `PATH`, in `PATH` order.

**The store directories moved from first to last.** They came first because
the package's wrapper put its bundled renderers there, at the front of every
caller's `PATH`, so a store directory there was one build every caller
shared. With nothing bundled, a store directory on `PATH` is someone's own: a
`nix shell` or `nix develop`, or a program that launched this one with its own
wrapper's `PATH` in front. Only that caller sees it, so first
place made a terminal compile a path the service would not, and nothing keeps
it alive: the next garbage collection deletes the renderer the document names.
Last place keeps a renderer that exists only there usable (a developer's
shell still works), and never lets it shadow an installed one.

**The path found is written as found, not resolved.** A profile's `bin` is a
symbolic link that every profile switch re-points;
`~/.nix-profile/bin/mpvpaper` keeps working across switches, while the store
path it resolved to stops existing once the old generation is collected. It
also keeps the document's bytes the same across an update of the renderer,
so an update elsewhere in the profile no longer rewrites `runtime.toml`. The
written path depends only on which listed directory holds the program, which
is the same for every caller, so the same-bytes rule holds.

**A renderer found nowhere** is still written as `/usr/bin/<name>`, as since
0.1.4: the service's `[renderer]` needs an absolute path for each program (and
0.3.2's loader refuses a document without one), and it reports a renderer only
when an entry needs it and it cannot start. On a distribution that is also
where a later package install puts it. The placeholder is not how the app
knows: `runtime_config.find_program(name)` answers found or not found, and the
path, for any of the three programs; the missing-component notices (a later
step) read that.

The window's own scene stills and the legacy Python service (`--service`) still
start their renderer from their own `PATH`, as before. On NixOS,
home-manager and a distribution the renderers are on the session's `PATH`, so
they find the same program.

## The Nix modules

`nixosModules.default` and `homeManagerModules.default` (also exported as
`homeModules.default`), under `programs.wall-in-one`:

| Option | Default | What it does |
|---|---|---|
| `enable` | `false` | Install the app (and so its launcher entry, icon and user units). |
| `package` | this flake's `wall-in-one` | The app. |
| `manualWallpaperInstalls` | `false` | `true`: install neither renderer; you install and update them yourself. |
| `mpvpaperPackage` | `pkgs.mpvpaper` | The video renderer installed when `manualWallpaperInstalls` is `false`. |
| `linuxWallpaperenginePackage` | `pkgs.linux-wallpaperengine` | The scene renderer, likewise. |

The renderers come from the `pkgs` of the system or home configuration that
imports the module, not from this flake's nixpkgs, so no second nixpkgs is
pulled in for them and the user's overlays apply. The app itself is built
against this flake's lock, as the package always was.

The NixOS module adds the packages to `environment.systemPackages`, the
home-manager one to `home.packages`. That is what
[installing.md](installing.md) already describes: a profile exposes
`share/applications`, `share/icons` and `share/systemd/user`, and the user
manager finds the units through its `XDG_DATA_DIRS`, which NixOS's PAM
environment builds from every profile. Neither module enables or starts a
unit; `systemctl --user enable --now wall-in-one.service` stays the user's
step. A standalone home-manager on another distribution needs the profile's
`share` on the user manager's `XDG_DATA_DIRS` too (home-manager's
`targets.genericLinux` adds it), or the three units linked into
`~/.config/systemd/user`.

### Licences

In the pinned nixpkgs, mpvpaper is GPL-3.0-only and linux-wallpaperengine
GPL-3.0-or-later; neither has `meta.unfree`, so no `allowUnfree` is needed.
linux-wallpaperengine is built against `cef-binary`, a prebuilt Chromium
Embedded Framework (BSD-3-Clause, `sourceProvenance = binaryNativeCode`): a
configuration that refuses non-source packages refuses it, and has to set
`manualWallpaperInstalls` or its own `linuxWallpaperenginePackage`. Hydra
builds linux-wallpaperengine for x86_64-linux only, so on aarch64-linux it is
built locally. Wallpaper Engine itself and its Workshop items come from the
user's Steam library and are never packaged.

## What older versions left behind

- **`runtime.toml` from 0.1.4 to 0.3.x** names the renderers by the resolved
  store paths of that release's bundled copies. The first 0.4 start recompiles
  it in the unit's start-up preparation, before the service starts, so the
  paths become profile paths (or the placeholder). If that compile fails, the
  last good document stays, and with it the old paths: they work for as long
  as the old package's generation is kept, and after that the service reports
  the renderer it cannot start until a compile succeeds.
- **A profile that still holds 0.3.x** has its bundled renderers only inside
  that package's wrapper, never in a profile's `bin`, so the lookup never
  finds them.

## How it is tested

- The lookup, with fake profiles, store directories and `PATH`s
  (`tests/test_write_config_reports.py`): a re-pointed profile link compiles
  the same bytes, a store directory on `PATH` never shadows a profile, a
  renderer only in a store directory is still found, and `find_program`
  reports found and not found.
- The install kind: `tests/test_install_kind.py` (the CLI, the update report,
  the line a package rewrites) and the service's own tests (`status`,
  `--version`, the accepted values).
- `desktop`: the installed package's two programs report the same kind,
  `nix`. `package-closure`: no renderer in the closure, FFmpeg in it.
- `modules`: both modules evaluated, with `manualWallpaperInstalls` false
  (both renderers installed, from the module's `pkgs`, and the app) and true
  (neither renderer). The home-manager one is evaluated against a stand-in
  for home-manager's `home.packages`, not home-manager itself, which is not a
  flake input.
- `vm-test` installs the app and the renderers through the NixOS module, as a
  user would; `upgrade` checks that a 0.1.4 document's program paths become
  profile paths on the first start, with nothing else changing.
