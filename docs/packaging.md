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
  line before it builds, with `tools/set-install-kind.sh <kind>`: the flake's
  package in `postPatch`, a PKGBUILD in `prepare()`. The script refuses a kind
  it does not know and a module without exactly that line, so a package
  cannot quietly ship `source`. The value is then part of the installed
  module, which is the same file whatever imports it: the console script,
  the package's own test run, the `golden-profile` check's interpreter, a
  subinterpreter worker.
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
- The service's `status` has a new top-level `install_kind`. It is the value
  the companion is meant to prefer: the companion polls the service's socket,
  not the Python CLI, and falls back to telling the kinds apart by where the
  programs are for a 0.3 service, which has no `install_kind`. (The companion
  reading it is the companion's own change.) `ctl status` prints the service's
  reply as it is, so it reports the kind of the *service* that answered.
- `wall-in-one --update-status` reports the app's own kind under
  `installed.install_kind`, next to the running service's whole status.

The status addition is additive. `status_version` stays 2: a new key with a
fixed value says nothing a client has to understand, and 0.3 clients and the
companion read only the keys they know.
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
state. Each has its own `PATH`, so the places every caller shares come first,
and for a program in one of them (steps 1 and 2) the bytes are the same
whatever each caller's `PATH`:

1. The Nix profiles, in NixOS's login order: `~/.nix-profile/bin`,
   `~/.local/state/nix/profile/bin`, `/etc/profiles/per-user/$USER/bin`,
   `/nix/var/nix/profiles/default/bin`, `/run/current-system/sw/bin`.
2. `~/.local/bin`, `/usr/local/bin`, `/usr/bin`, `/bin`.
3. Every other absolute directory on `PATH`, in `PATH` order, except the
   Nix store directories.

Step 3 finds an install somewhere unusual (`/opt/...`, `~/bin`, a custom
prefix), but only for a caller whose `PATH` has that directory. The user
manager's `PATH` is often shorter than a terminal's, so a renderer found only
there can compile to one path from a terminal and to another, or to none, at
the service's start-up. Such an install has to be on the `PATH` of every
compiler, the user manager's included (`~/.config/environment.d/`, see
environment.d(5)), or be linked into a directory of step 1 or 2 (a link in
`~/.local/bin` is enough; the link's path is the one written).

**Store directories on `PATH` are not looked in at all.** Until 0.4 they came
first, because the package's wrapper put its bundled renderers there, at the
front of every caller's `PATH`, so a store directory there was one build every
caller shared. With nothing bundled, a store directory on `PATH` is someone's
own: a `nix shell` or `nix develop`, or a program that launched this one with
its own wrapper's `PATH` in front. Only that caller sees it, so a terminal in a
`nix shell` would compile a path the service's start-up compile never does
(the same state, other bytes), and nothing keeps it alive: the next garbage
collection deletes the renderer the document names. Nothing legitimate needs
them: the package's tests stand in for the renderers, the golden profile
compares programs by name, the VMs install them through the module, and a
developer installs them as a user does (the dev shell still has them, to run
by hand). A renderer has to be installed to be found.

**The path found is written as found, not resolved.** A profile's `bin` is a
symbolic link that every profile switch re-points;
`~/.nix-profile/bin/mpvpaper` keeps working across switches, while the store
path it resolved to stops existing once the old generation is collected. It
also keeps the document's bytes the same across an update of the renderer,
so an update elsewhere in the profile no longer rewrites `runtime.toml`. For
the shared places the written path depends only on which of them holds the
program, which is the same for every caller, so the same-bytes rule holds;
for a directory only on `PATH` it holds when every caller has that directory
(above).

The trade-off: a resolved store path kept working until the next garbage
collection after the renderer left the profile; the profile's path stops
working at once, when a renderer is removed or moves to another profile
(switching `manualWallpaperInstalls`, moving from the NixOS module to
home-manager). Nothing recompiles on a profile change, so the recovery is the
app's **Check Again** (or `wall-in-one --write-config` and `wall-in-one ctl
reload`), which
[installing.md](installing.md#after-installing-removing-or-moving-a-renderer)
says next to the steps for installing one. Meanwhile those wallpapers show
their still and nothing is held against them
([missing components](missing-components.md#the-service-without-a-renderer)).

**A renderer found nowhere** is still written as `/usr/bin/<name>`, as since
0.1.4: the service's `[renderer]` needs an absolute path for each program (and
0.3.2's loader refuses a document without one), and it reports a renderer only
when an entry needs it and it cannot start (`missing_renderers` in `status`).
On a distribution that is also where a later package install puts it, and a
reload then starts it. The placeholder is not how the app knows:
`wall_in_one.programs.find_program(name)` answers found or not found, and the
path, for any of the three programs; the app's setup notices
([missing components](missing-components.md)) read that.

**Everything that starts one of them asks the same lookup.** The window's
scene stills and its scene-engine status (Settings), the legacy Python service
(`--service`) with its mpvpaper and scene engine, and the app's own `noctalia`
calls use `find_program` and start the program by the path it answers, not by
name from their own `PATH`. A window started by a launcher or a user unit whose
`PATH` lacks the profile (a standalone home-manager on another distribution, a
`nix profile` install) therefore still sees the renderer `runtime.toml` names:
it says it is installed, captures scene stills, and keeps those entries in the
document.

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

The NixOS module adds the packages to `environment.systemPackages`. That is
what [installing.md](installing.md) already describes: a profile exposes
`share/applications`, `share/icons` and `share/systemd/user`, and the user
manager finds the units through its `XDG_DATA_DIRS`, which NixOS's PAM
environment builds from every profile. (`systemd.packages` would be wrong
there: it reads a package's `lib/systemd`, not `share`.)

The home-manager module adds them to `home.packages`, and the app to
`systemd.user.packages` as well. A home-manager on another distribution has
no PAM environment that puts the profile on the user manager's
`XDG_DATA_DIRS`, so `home.packages` alone leaves the units unfound there;
`systemd.user.packages` links them into `$XDG_DATA_HOME/systemd/user`, which
every user manager reads, on any distribution. It is not
`~/.config/systemd/user`, which home-manager's own service switching manages,
so nothing is started or restarted when home-manager switches.

Neither module enables or starts a unit; `systemctl --user enable --now
wall-in-one.service` stays the user's step.

To import a module without the flake, use `nix/nixos-module.nix` or
`nix/home-manager-module.nix` by path and set `programs.wall-in-one.package`
(only the flake's modules can default it to the flake's package).

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
  paths become profile paths, or the placeholder for a renderer that is not
  installed. With the placeholder, every video or scene shows its still: the
  service reports the renderer as missing rather than the wallpaper as broken,
  so nothing is skipped or saved, and the app's notice says which renderer to
  install ([missing components](missing-components.md)). docs/updating.md says
  to install the renderers before the restart, and how to have them found
  after. If that compile fails, the last good document stays, and with it the
  old paths: they work for as long as the old package's generation is kept,
  and after that the service reports the renderer it cannot start until a
  compile succeeds.
- **A profile that still holds 0.3.x** has its bundled renderers only inside
  that package's wrapper, never in a profile's `bin`, so the lookup never
  finds them.

## Arch release checklist

`packaging/arch/PKGBUILD` is checked in with `sha256sums=('SKIP')`: CI builds
a tarball of the checkout (`packaging/arch/local-build.sh`), so it never
downloads the tag's tarball and has no real checksum to use. The PKGBUILD that
is published is made from it after the tag exists, in this order:

1. **Bump the versions** in one commit: `pyproject.toml`, `wall_in_one`'s
   `__version__`, `service/Cargo.toml` and `Cargo.lock`, `flake.nix`, and the
   PKGBUILD's `pkgver` (with `pkgrel=1`). `tests/test_packaging.py` checks the first
   four against each other, a test compares the PKGBUILD's `pkgver` with
   `__version__`, `tests/test_arch_srcinfo.py` checks `.SRCINFO` against the
   PKGBUILD, and the package fails to build if the programs' `--version` is not `pkgver`
   (`package()` and the Arch CI smoke test both compare line 1 exactly).
2. **Run the release gates and the Arch job** (`arch.yml` runs on `release/**`
   and on demand), and tag `v<pkgver>`.
3. **Run `packaging/arch/aur-release.sh v<pkgver> <directory>`.** It downloads
   GitHub's tarball for the tag, refuses unless its only top-level directory
   is `wall-in-one-<pkgver>`, computes the sha256, and writes the release
   `PKGBUILD` (real checksum, no `TODO`) and a matching `.SRCINFO` into the
   directory, changing nothing in the repository. The output directory must be
   new (or empty): it refuses one that is the packaging directory under any
   name, or that already has a `PKGBUILD` or `.SRCINFO`, and it publishes the
   files with `link(2)` so nothing is ever overwritten or written through a
   symlink. It refuses if the tag is not
   `v<pkgver>` or any `SKIP` would remain. Do not hand-edit its files, and
   do not commit them anywhere but `packaging/arch/release/` (step 6).
4. **Check the result on Arch** if you have a machine or container: in that
   directory, `makepkg --verifysource` downloads the source again and checks
   it against the sum; `makepkg --printsrcinfo | diff - .SRCINFO` shows the
   `.SRCINFO` is what makepkg writes. (Without Arch, the `.SRCINFO` is the
   checked-in one with only the sum filled in, which `tests/test_aur_release.py`
   verifies offline.)
5. **Attach to the GitHub Release** the release `PKGBUILD` from step 3 and
   `packaging/arch/arch-setup.sh`, as `PKGBUILD` and `arch-setup.sh`. That is
   what Arch users install from
   ([installing on Arch](installing.md#on-arch-linux-from-a-release)): the
   script downloads the latest release's `PKGBUILD` by that name. (Leave
   `.SRCINFO` off: GitHub renames a file whose name starts with a dot.)
6. **Commit the two files, unchanged, into `packaging/arch/release/`** on
   `main`, as a post-tag commit (`cp -p <directory>/PKGBUILD <directory>/.SRCINFO
   packaging/arch/release/`). That directory is what paru users update from
   ([installing on Arch](installing.md#on-arch-linux-from-a-release)): the
   `[wall-in-one]` section in their paru.conf points at it with `Path =
   packaging/arch/release`, and `paru -Sy` fetches `main`, so a release
   reaches them when this commit does, not when the tag does. Never put the
   development `packaging/arch/PKGBUILD` there (`SKIP`, and between a bump and
   its tag an untagged version).
   `tests/test_arch_release_dir.py` checks the directory: a real 64-hex sum
   and no `SKIP`, `.SRCINFO` matching, and a `pkgver` that is not ahead of
   the app's `__version__` (it trails the version bump until this commit).
7. **Publish to the AUR**, if it is published there. This pushes to the
   owner's AUR account, so it needs the owner's approval and is never done by
   an agent: copy the two files into the AUR repository `wall-in-one`, commit,
   and push.

If a later fix needs another package build of the same release, raise `pkgrel`
in both the checked-in PKGBUILD and `.SRCINFO` and repeat steps 3 to 7, with a
new output directory for step 3 (the script does not reuse one).

## How it is tested

- The lookup, with fake profiles, store directories and `PATH`s
  (`tests/test_write_config_reports.py`): a re-pointed profile link compiles
  the same bytes, a store directory on `PATH` is never looked in (a terminal
  inside a `nix shell` compiles what the service does), and `find_program`
  reports found and not found. The window, the legacy service and the
  Noctalia calls agree with it and start what it finds, with the profile off
  their `PATH` (`tests/test_programs.py`).
- The install kind: `tests/test_install_kind.py` (the CLI, the update report,
  the line a package rewrites) and the service's own tests (`status`,
  `--version`, the accepted values).
- The Arch package: `tests/test_arch_srcinfo.py` (every makepkg variable of
  the PKGBUILD is in `.SRCINFO`, and the other way round),
  `tests/test_aur_release.py` (the release script, offline against a tarball
  it is given) and the `arch.yml` job (the build, `check()`, install, smoke
  and GUI tests, in a clean Arch container).
- `desktop`: the installed package's two programs report the same kind,
  `nix`. `package-closure`: no renderer in the closure, FFmpeg in it.
- `modules`: both modules evaluated, through the flake and by path, with
  `manualWallpaperInstalls` false (both renderers installed, from the
  module's `pkgs`, and the app) and true (neither renderer); the
  home-manager one registers the app's units, the NixOS one leaves them to
  the profile. The home-manager one is evaluated by real home-manager
  (`homeManagerConfiguration`; the `home-manager` flake input exists for this
  check alone, and no package or module depends on it), so it must install
  the same and put the units in `xdg.dataFile."systemd/user"`; with
  `enable = false` it installs and links nothing. Evaluation only, no build.
- `vm-test` installs the app and the renderers through the NixOS module, as a
  user would; `upgrade` checks that a 0.1.4 document's program paths become
  profile paths on the first start, with nothing else changing.
