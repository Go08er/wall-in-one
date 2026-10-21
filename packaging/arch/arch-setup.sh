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
# With paru installed it offers to register this repository as a paru
# "PKGBUILD repository" and to install through paru instead, so that
# `paru -Syu` updates Wall-in-One from GitHub from then on. It says which
# paru.conf it changed, adds a [wall-in-one] section only when none is there,
# and never rewrites the rest of the file.
#
#   arch-setup.sh [--version X.Y.Z] [--paru | --no-paru]
#                 [--renderers | --no-renderers] [--no-enable]
#
#   --version X.Y.Z  install that release instead of the latest one (by makepkg:
#                    paru follows the latest release)
#   --paru           install through paru, and register the repository for it
#   --no-paru        build with makepkg, and leave paru's config alone
#   --renderers      install mpvpaper and linux-wallpaperengine-git without asking
#   --no-renderers   leave them out (videos and scenes then show their stills)
#   --no-enable      neither enable nor start the user service
set -euo pipefail

repo=https://github.com/Go08er/wall-in-one
companion=https://github.com/Go08er/goober-noctalia-plugins-v5
display_help="$repo/blob/main/docs/installing.md#the-service-needs-your-sessions-display"
paru_url=$repo
paru_path=packaging/arch/release
version=""
use_paru=ask
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
    --paru) use_paru=yes; shift ;;
    --no-paru) use_paru=no; shift ;;
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

if [ "$use_paru" = yes ]; then
  command -v paru >/dev/null 2>&1 || die "--paru needs paru, which is not installed"
  [ -z "$version" ] || die "--paru follows the latest release; leave out --version, or use --no-paru"
fi

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
curl -fsSL --proto '=https' --proto-redir '=https' -o "$work/PKGBUILD" "$url" ||
  die "could not download the PKGBUILD from $url"
grep -q '^pkgname=wall-in-one$' "$work/PKGBUILD" || die "$url is not Wall-in-One's PKGBUILD"
grep -qE "^sha256sums=\('[0-9a-f]{64}'\)$" "$work/PKGBUILD" ||
  die "$url has no checksum for its source; use a release's PKGBUILD"
# The values may be quoted, and an epoch makes pacman print epoch:pkgver-pkgrel.
pkgfield() { sed -n "s/^$1=//p" "$work/PKGBUILD" | head -n 1 | tr -d "\"'"; }
pkgver=$(pkgfield pkgver)
pkgrel=$(pkgfield pkgrel)
epoch=$(pkgfield epoch)
full=${epoch:+$epoch:}$pkgver-$pkgrel
if [ -n "$version" ] && [ "$pkgver" != "$version" ]; then
  die "the PKGBUILD of release v$version says it is $pkgver; not installing it"
fi

# --- paru ----------------------------------------------------------------
# paru (2.0 and later) reads PKGBUILDs from a git repository named in a
# [section] of paru.conf, refreshes it on `paru -Sy`, and installs and updates
# its packages like AUR ones. paru reads ONE config file: $PARU_CONF (even an
# empty one: then it fails, the file does not exist), else
# $XDG_CONFIG_HOME/paru/paru.conf when that variable is an absolute path (else
# ~/.config/paru/paru.conf), else /etc/paru.conf. A user file therefore replaces the system one unless it pulls
# it in with `Include = /etc/paru.conf`, which paru accepts anywhere in the file
# (before any section too). (WIO_SETUP_PARU_SYSTEM_CONF replaces the system
# path in tests.)
paru_system_conf=${WIO_SETUP_PARU_SYSTEM_CONF-/etc/paru.conf}

paru_conf_path() {
  if [ -n "${PARU_CONF+set}" ]; then
    printf '%s\n' "$PARU_CONF"
  else
    case ${XDG_CONFIG_HOME:-} in
      /*) printf '%s\n' "$XDG_CONFIG_HOME/paru/paru.conf" ;;
      *) printf '%s\n' "${HOME:-}/.config/paru/paru.conf" ;;
    esac
  fi
}

# paru takes repository names literally, so a [Wall-in-One] section is another
# repository. Find any spelling, in the file or in the files it Includes (one
# level, read-only; Include takes an absolute path).
found_file="" found_header=""
find_section() {
  local file=$1 included
  local pattern='^[[:space:]]*(\[[wW][aA][lL][lL]-[iI][nN]-[oO][nN][eE]\])[[:space:]]*$'
  local candidates=("$file")
  while IFS= read -r included; do
    [ -f "$included" ] && candidates+=("$included")
  done < <(sed -n 's/^[[:space:]]*Include[[:space:]]*=[[:space:]]*//p' "$file" 2>/dev/null)
  for candidate in "${candidates[@]}"; do
    found_header=$(sed -nE "s/$pattern/\1/p" "$candidate" 2>/dev/null | head -n 1)
    if [ -n "$found_header" ]; then
      found_file=$candidate
      return 0
    fi
  done
  return 1
}

# section_value FILE KEY: the value of KEY in the file's wall-in-one section.
section_value() {
  local in=0 line key value
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line#"${line%%[![:space:]]*}"}
    case $line in
      \[*)
        key=${line%%]*}
        key=${key#[}
        [ "${key,,}" = wall-in-one ] && in=1 || in=0
        ;;
      *=*)
        if [ "$in" = 1 ]; then
          key=${line%%=*}
          key=${key%"${key##*[![:space:]]}"}
          if [ "$key" = "$2" ]; then
            value=${line#*=}
            value=${value#"${value%%[![:space:]]*}"}
            printf '%s\n' "${value%"${value##*[![:space:]]}"}"
            return 0
          fi
        fi
        ;;
    esac
  done <"$1"
}

# Say what an existing section points at, and warn when it is not the release
# directory of this repository.
report_section() {
  local url path
  url=$(section_value "$found_file" Url)
  path=$(section_value "$found_file" Path)
  say "$found_file has a $found_header section already, left as it is: Url = ${url:-(none)}, Path = ${path:-(none)}"
  if [ "$found_header" != "[wall-in-one]" ]; then
    say "WARNING: it is spelled $found_header, and paru takes repository names literally: rename it to [wall-in-one]"
  fi
  if [ "${url%.git}" != "$paru_url" ] || [ "${path%/}" != "$paru_path" ]; then
    say "WARNING: expected Url = $paru_url and Path = $paru_path. Other values may install a fork or the development PKGBUILD, which has no checksum: edit $found_file, or remove the section and run this again"
  fi
}

register_with_paru() {
  local conf
  conf=$(paru_conf_path)
  if [ -f "$conf" ]; then
    if find_section "$conf"; then
      report_section
      return 0
    fi
    # Append only: the rest of the file stays as it is.
    printf '\n[wall-in-one]\nUrl = %s\nPath = %s\n' "$paru_url" "$paru_path" >>"$conf" ||
      die "could not add the [wall-in-one] section to $conf"
    say "added a [wall-in-one] section to $conf (Url = $paru_url, Path = $paru_path)"
  elif [ -n "${PARU_CONF+set}" ]; then
    die "PARU_CONF is set to '$PARU_CONF', and that file does not exist; paru would refuse it too"
  elif [ -f "$paru_system_conf" ] && find_section "$paru_system_conf"; then
    report_section
  else
    mkdir -p -- "${conf%/*}" || die "could not create ${conf%/*}"
    {
      # A user config replaces the system one, so pull the system one in.
      if [ -f "$paru_system_conf" ]; then
        printf 'Include = %s\n\n' "$paru_system_conf"
      fi
      printf '[wall-in-one]\nUrl = %s\nPath = %s\n' "$paru_url" "$paru_path"
    } >"$conf" || die "could not write $conf"
    if [ -f "$paru_system_conf" ]; then
      say "created $conf: it includes $paru_system_conf, so your paru settings stay, and adds the [wall-in-one] section"
    else
      say "created $conf with a [wall-in-one] section (no $paru_system_conf to include)"
    fi
  fi
}

if [ "$use_paru" = ask ]; then
  use_paru=no
  if [ -z "$version" ] && [ -t 0 ] && command -v paru >/dev/null 2>&1; then
    read -r -p "Install through paru from GitHub, so that paru -Syu keeps Wall-in-One updated? [Y/n] " answer || answer=n
    case $answer in
      [nN]*) ;;
      *) use_paru=yes ;;
    esac
  fi
fi

installed_new=0
current=$(pacman -Q wall-in-one 2>/dev/null | sed -n 's/^wall-in-one //p') || current=""
order=-1
[ -z "$current" ] || order=$(vercmp "$current" "$full")
if [ "$order" -eq 0 ]; then
  say "Wall-in-One $current is installed already, which is this release"
  # Registering is still useful: from now on paru -Syu updates it.
  if [ "$use_paru" = yes ]; then register_with_paru; fi
elif [ "$order" -gt 0 ]; then
  die "Wall-in-One $current is installed, newer than $full; to go back, uninstall it first (pacman -R wall-in-one)"
else
  if [ "$use_paru" = yes ]; then
    register_with_paru
    say "paru shows the PKGBUILD for review, builds it (the app, the service and their tests: a few minutes) and installs it"
    # Only the PKGBUILD repositories are refreshed: no pacman database sync.
    paru -Sy --pkgbuilds || die "paru could not fetch the PKGBUILD repository; re-run with --no-paru to build with makepkg"
    paru -S wall-in-one/wall-in-one || die "paru could not install Wall-in-One; re-run with --no-paru to build with makepkg"
    now=$(pacman -Q wall-in-one 2>/dev/null | sed -n 's/^wall-in-one //p') || now=""
    # A paru run that had nothing to do must not restart the running service.
    if [ "$now" != "$current" ]; then installed_new=1; else say "paru had nothing to install"; fi
    if [ "$now" != "$full" ]; then
      say "paru installed ${now:-nothing}, GitHub's latest release is $full: the repository follows a release when its files are committed there; run paru -Syu again later"
    fi
  else
    say "building Wall-in-One $pkgver: this builds the app and the service and runs their tests, so it takes a few minutes"
    say "makepkg runs the downloaded PKGBUILD as shell code; it is $url"
    building=1
    (cd "$work" && makepkg -si)
    building=0
    installed_new=1
  fi
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
      systemctl --user enable wall-in-one.service
      # A first install has no saved settings yet, and the unit's preparation
      # then refuses to start (exit 78) until the app has chosen a library
      # folder: that is expected, not a failed setup.
      if [ "$installed_new" = 1 ] && systemctl --user is-active --quiet wall-in-one.service; then
        if systemctl --user restart wall-in-one.service; then
          say "the wallpaper service is restarted on the new version"
        else
          say "the wallpaper service did not restart; see: systemctl --user status wall-in-one.service"
        fi
      elif systemctl --user start wall-in-one.service; then
        say "the wallpaper service is enabled and running"
      else
        say "the wallpaper service is enabled, and starts once Wall-in-One has its settings: open Wall-in-One, choose your library folder, then run: systemctl --user start wall-in-one.service (or log in again)"
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
