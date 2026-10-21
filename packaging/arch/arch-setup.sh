#!/usr/bin/env bash
# Set up Wall-in-One on Arch Linux from a GitHub release, in one go:
#
#   curl -fLO https://github.com/Go08er/wall-in-one/releases/latest/download/arch-setup.sh
#   bash arch-setup.sh
#
# It downloads the release's PKGBUILD, builds and installs it with
# `makepkg -si` (sudo asks for your password to install packages), offers to
# install the wallpaper renderers when you have paru or yay, and enables the
# user service. Run it again to update to a newer release. Run it as your
# normal user, from a terminal, not as root and not piped into bash.
#
#   arch-setup.sh [--version X.Y.Z] [--renderers | --no-renderers] [--no-enable]
#
#   --version X.Y.Z  install that release instead of the latest one
#   --renderers      install mpvpaper and linux-wallpaperengine-git without asking
#   --no-renderers   leave them out (videos and scenes then show their stills)
#   --no-enable      neither enable nor start the user service
set -euo pipefail

repo=https://github.com/Go08er/wall-in-one
companion=https://github.com/Go08er/goober-noctalia-plugins-v5
display_help="$repo/blob/main/docs/installing.md#the-service-needs-your-sessions-display"
version=""
renderers=ask
enable=1

die() {
  printf 'arch-setup: %s\n' "$*" >&2
  exit 1
}
say() { printf '==> %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case $1 in
    --version)
      [ $# -ge 2 ] || die "--version needs a version, e.g. --version 0.4.0"
      version=${2#v}
      shift 2
      ;;
    --renderers) renderers=yes; shift ;;
    --no-renderers) renderers=no; shift ;;
    --no-enable) enable=0; shift ;;
    -h | --help)
      sed -n '2,/^set -euo pipefail$/{/^set -euo pipefail$/d;s/^# \{0,1\}//;p;}' "$0"
      exit 0
      ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

# makepkg, pacman and sudo ask questions, and bash reading this script from a
# pipe would hand them the rest of the script as answers.
# (WIO_SETUP_NO_TTY_OK=1 lets the tests run it without a terminal.)
if [ ! -t 0 ] && [ "${WIO_SETUP_NO_TTY_OK:-}" != 1 ]; then
  die "run it from a terminal, as a file: curl -fLO $repo/releases/latest/download/arch-setup.sh && bash arch-setup.sh"
fi
[ "$(id -u)" -ne 0 ] ||
  die "run this as your normal user, not as root: makepkg refuses root, and sudo asks when it needs to"
command -v pacman >/dev/null 2>&1 || die "this is for Arch Linux, and pacman was not found"
command -v sudo >/dev/null 2>&1 ||
  die "sudo is not installed, and makepkg needs it to install packages: as root, run pacman -S sudo, and give your user sudo rights"

# One install only: the companion refuses to work with a Nix install and the
# Arch package side by side. (WIO_SETUP_NIX_DIRS replaces the list in tests.)
user=$(id -un)
nix_dirs=${WIO_SETUP_NIX_DIRS-"${HOME:-}/.nix-profile/bin:${HOME:-}/.local/state/nix/profile/bin:/etc/profiles/per-user/$user/bin:/run/current-system/sw/bin:/nix/var/nix/profiles/default/bin"}
IFS=: read -r -a nix_list <<<"$nix_dirs" || true
for dir in "${nix_list[@]}"; do
  if [ -n "$dir" ] && [ -x "$dir/wall-in-one" ]; then
    die "Wall-in-One is already installed with Nix ($dir/wall-in-one). Keep one install: remove that one first, or keep using it."
  fi
done

if ! pacman -Q base-devel >/dev/null 2>&1; then
  say "installing base-devel, which makepkg needs to build"
  sudo pacman -S --needed base-devel
fi

# The build goes in your cache directory, not /tmp: /tmp is RAM on Arch, and a
# Rust release build and its tests take a few GB. A failed build keeps its files.
cache=${XDG_CACHE_HOME:-${HOME:-}/.cache}
mkdir -p -- "$cache"
work=$(mktemp -d "$cache/wall-in-one-setup.XXXXXX")
building=0
cleanup() {
  if [ "$1" -ne 0 ] && [ "$building" = 1 ]; then
    printf 'arch-setup: the build files are kept in %s\n' "$work" >&2
  else
    rm -rf -- "$work"
  fi
}
trap 'cleanup $?' EXIT

if [ -n "$version" ]; then
  url="$repo/releases/download/v$version/PKGBUILD"
else
  url="$repo/releases/latest/download/PKGBUILD"
fi
say "downloading $url"
curl -fsSL --proto '=https' -o "$work/PKGBUILD" "$url" ||
  die "could not download the PKGBUILD from $url"
grep -q '^pkgname=wall-in-one$' "$work/PKGBUILD" || die "$url is not Wall-in-One's PKGBUILD"
grep -qE "^sha256sums=\('[0-9a-f]{64}'\)$" "$work/PKGBUILD" ||
  die "$url has no checksum for its source; use a release's PKGBUILD"
pkgver=$(sed -n 's/^pkgver=//p' "$work/PKGBUILD")
pkgrel=$(sed -n 's/^pkgrel=//p' "$work/PKGBUILD")
if [ -n "$version" ] && [ "$pkgver" != "$version" ]; then
  die "the PKGBUILD of release v$version says it is $pkgver; not installing it"
fi

installed_new=0
current=$(pacman -Q wall-in-one 2>/dev/null | sed -n 's/^wall-in-one //p') || current=""
order=-1
[ -z "$current" ] || order=$(vercmp "$current" "$pkgver-$pkgrel")
if [ "$order" -eq 0 ]; then
  say "Wall-in-One $current is installed already, which is this release"
elif [ "$order" -gt 0 ]; then
  die "Wall-in-One $current is installed, newer than $pkgver-$pkgrel; to go back, uninstall it first (pacman -R wall-in-one)"
else
  say "building Wall-in-One $pkgver: this builds the app and the service and runs their tests, so it takes a few minutes"
  say "makepkg runs the downloaded PKGBUILD as shell code; it is $url"
  building=1
  (cd "$work" && makepkg -si)
  building=0
  installed_new=1
  say "installed: $(wall-in-one --version 2>/dev/null | sed -n 1p || true)"
fi

# The renderers are optional dependencies.
missing=()
command -v mpvpaper >/dev/null 2>&1 || missing+=(mpvpaper)
command -v linux-wallpaperengine >/dev/null 2>&1 || missing+=(linux-wallpaperengine-git)
if [ ${#missing[@]} -gt 0 ]; then
  helper=""
  for candidate in paru yay; do
    if command -v "$candidate" >/dev/null 2>&1; then
      helper=$candidate
      break
    fi
  done
  if [ "$renderers" = ask ]; then
    renderers=no
    if [ -n "$helper" ] && [ -t 0 ]; then
      read -r -p "Install the wallpaper renderers (${missing[*]}) with $helper? [Y/n] " answer || answer=n
      case $answer in
        [nN]*) ;;
        *) renderers=yes ;;
      esac
    fi
  fi
  if [ "$renderers" = yes ] && [ -n "$helper" ]; then
    "$helper" -S --needed "${missing[@]}" ||
      say "$helper could not install ${missing[*]}; Wall-in-One works without them, and you can try again later"
  elif [ "$renderers" = yes ]; then
    say "no AUR helper (paru or yay) found: install ${missing[*]} from the AUR yourself"
  else
    say "not installing ${missing[*]}: videos and scenes show their stills until you install them (from the AUR)"
  fi
fi

if [ "$enable" = 1 ]; then
  if manager_env=$(systemctl --user show-environment 2>/dev/null); then
    has_display=0
    case $'\n'$manager_env in *$'\n'WAYLAND_DISPLAY=*) has_display=1 ;; esac
    if [ "$has_display" = 0 ] && [ -n "${WAYLAND_DISPLAY:-}" ]; then
      systemctl --user import-environment WAYLAND_DISPLAY
      has_display=1
      say "gave your user manager this session's WAYLAND_DISPLAY; so that it has it at every login, see $display_help"
    fi
    systemctl --user daemon-reload
    if [ "$has_display" = 1 ]; then
      if [ "$installed_new" = 1 ] && systemctl --user is-active --quiet wall-in-one.service; then
        systemctl --user enable wall-in-one.service
        systemctl --user restart wall-in-one.service
        say "the wallpaper service is restarted on the new version"
      else
        systemctl --user enable --now wall-in-one.service
        say "the wallpaper service is enabled and running"
      fi
    else
      systemctl --user enable wall-in-one.service
      say "the wallpaper service is enabled; it starts with your graphical session, which needs to give it its display: see $display_help"
    fi
  else
    say "no systemd user manager answers here: the companion plugin starts the service directly instead"
  fi
fi

say "what Wall-in-One still needs, if anything:"
wall-in-one --setup-status || true
say "add the companion plugin in Noctalia's plugin settings: $companion"
