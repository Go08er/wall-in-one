#!/usr/bin/env bash
# Render the prototype's screenshot gallery into ./screenshots (headless, isolated).
# Usage: prototypes/ui-redesign/screenshots.sh [scene ...]   (default: the full tour)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="$HERE/screenshots"
ISO="$(mktemp -d)"
trap 'rm -rf "$ISO"' EXIT
mkdir -p "$OUT" "$ISO"/{home,config,state,cache,data,run}
chmod 700 "$ISO/run"

TOUR=(
  "library"
  "library+inspector:lily-pond"
  "library+inspector:rain-window/colors"
  "library+hover:golden-coast"
  "library+select"
  "light@library+inspector:city-dusk/colors"
  "battery@library"
  "store"
  "store+search:night"
  "store+downloading"
  "store+preview:wallhaven-1"
  "light@store"
  "playlist:frog-day"
  "playlist:frog-day+picker"
  "playlist:frog-day+drag"
  "playlist:all-media"
  "light@playlist:frog-night"
  "schedule"
  "schedule+why"
  "schedule+edit:weekend-days"
  "schedule+reorder"
  "light@schedule"
  "displays"
  "mirrored@displays"
  "displays+unscheduled"
  "light@displays"
  "settings"
  "settings+search:battery"
  "settings+palettes"
  "settings+palette-edit"
  "settings:log"
  "frosted@library+inspector:lily-pond"
  "frosted,light@library"
  "frosted@schedule"
  "frosted,frost0,opacity35@library"
  "frosted,frost100,opacity20@library"
  "frosted@settings:appearance"
  "translucent,bg20,panel70@settings:appearance"
  "translucent,bg20,panel70@library+inspector:alpine"
  "welcome@library"
  "service-off@library"
  "narrow@library+inspector:alpine"
  "narrow@playlist:frog-day"
  "narrow@schedule"
  "evening@library"
)
if [ "$#" -gt 0 ]; then TOUR=("$@"); fi

cd "$REPO"
# Icons and fonts for a headless X server; the real desktop already has them.
ICONS="$(nix build --inputs-from . nixpkgs#adwaita-icon-theme --no-link --print-out-paths)/share"
nix develop --command env -u DBUS_SESSION_BUS_ADDRESS -u WAYLAND_DISPLAY \
  HOME="$ISO/home" XDG_CONFIG_HOME="$ISO/config" XDG_STATE_HOME="$ISO/state" \
  XDG_CACHE_HOME="$ISO/cache" XDG_DATA_HOME="$ISO/data" XDG_RUNTIME_DIR="$ISO/run" \
  XDG_DATA_DIRS="$ICONS:${XDG_DATA_DIRS:-/run/current-system/sw/share}" \
  GSK_RENDERER=cairo GDK_BACKEND=x11 GSETTINGS_BACKEND=memory PYTHONDONTWRITEBYTECODE=1 \
  xvfb-run --auto-servernum --server-args='-screen 0 1440x960x24 -nolisten tcp' \
  python prototypes/ui-redesign/demo.py --screenshot "$OUT" "${TOUR[@]}"
