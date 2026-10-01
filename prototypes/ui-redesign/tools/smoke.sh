#!/usr/bin/env bash
# Headless smoke test of the prototype: isolated profile, no session bus, Xvfb.
# Prints "SMOKE steps=N failures=M"; exits non-zero on any failure or GTK warning.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ISO="$(mktemp -d)"
trap 'rm -rf "$ISO"' EXIT
mkdir -p "$ISO"/{home,config,state,cache,data,run}
chmod 700 "$ISO/run"
cd "$REPO"
# The demo's AppState must still implement the app's Protocols (mypy).
"$HERE/tools/typecheck.sh"
ICONS="$(nix build --inputs-from . nixpkgs#adwaita-icon-theme --no-link --print-out-paths)/share"
OUT="$ISO/smoke.out"
nix develop --command env -u DBUS_SESSION_BUS_ADDRESS -u WAYLAND_DISPLAY -u LD_LIBRARY_PATH \
  HOME="$ISO/home" XDG_CONFIG_HOME="$ISO/config" XDG_STATE_HOME="$ISO/state" \
  XDG_CACHE_HOME="$ISO/cache" XDG_DATA_HOME="$ISO/data" XDG_RUNTIME_DIR="$ISO/run" \
  XDG_DATA_DIRS="$ICONS:${XDG_DATA_DIRS:-/run/current-system/sw/share}" \
  GSK_RENDERER=cairo GDK_BACKEND=x11 GSETTINGS_BACKEND=memory GTK_A11Y=none PYTHONDONTWRITEBYTECODE=1 \
  xvfb-run --auto-servernum --server-args='-screen 0 1440x960x24 -nolisten tcp' \
  python "$HERE/tools/smoke.py" "$ISO/failures" 2>&1 \
  | grep -v "^warning: Git tree\|dev shell -- run\|g_settings\|accessibility bus" | tee "$OUT"
grep -q "failures=0" "$OUT" || exit 1
if grep -qE "(Gtk|Adw|GLib|Gdk)-(CRITICAL|WARNING)" "$OUT"; then echo "GTK warnings above"; exit 1; fi
