#!/usr/bin/env bash
# Run a command, normally the test suite, in a throwaway profile.
#
#   tools/isolated.sh python -m pytest tests -q -m "not gui"
#   tools/isolated.sh python -m pytest tests -q -m gui
#   tools/isolated.sh tools/golden-downgrade.sh
#
# This, or one of the flake's checks, is how to run the tests. The command
# gets a new temporary HOME and XDG config, state, cache, data and runtime
# directories, removed afterwards; dead session and system bus addresses (left
# unset, GTK under Xvfb would autolaunch a bus and portals that outlive the
# run); no portals; none of your session's Wayland, niri or X display
# addresses; no LD_LIBRARY_PATH (see the README); and its own Xvfb server with
# the cairo renderer, as the gui-tests check uses.
#
# tests/conftest.py sandboxes the test process as well, and refuses and
# reports writes to your real Wall-in-One and Noctalia files, but that is a
# backstop: it cannot see child processes, native code or other programs.
# A bare `pytest` from a developer shell is not a safe way to run the suite.
#
# Outside a Nix shell, the command runs in this flake's dev shell.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
if [[ -z "${IN_NIX_SHELL:-}" ]]; then
  exec env -u LD_LIBRARY_PATH nix develop --command "$repo/tools/isolated.sh" "$@"
fi

root="$(mktemp -d "${TMPDIR:-/tmp}/wio-isolated.XXXXXX")"
trap 'rm -rf "$root"' EXIT
mkdir -m 700 "$root/home" "$root/config" "$root/state" "$root/cache" "$root/data" "$root/run"

env -u WAYLAND_DISPLAY -u NIRI_SOCKET -u DISPLAY -u LD_LIBRARY_PATH \
  -u WIO_TEST_SESSION_ROOT -u WIO_TEST_SESSION_PID \
  HOME="$root/home" \
  XDG_CONFIG_HOME="$root/config" \
  XDG_STATE_HOME="$root/state" \
  XDG_CACHE_HOME="$root/cache" \
  XDG_DATA_HOME="$root/data" \
  XDG_RUNTIME_DIR="$root/run" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=$root/run/no-session-bus" \
  DBUS_SYSTEM_BUS_ADDRESS="unix:path=$root/run/no-system-bus" \
  GDK_DEBUG=no-portals \
  GTK_USE_PORTAL=0 \
  GSETTINGS_BACKEND=memory \
  GDK_BACKEND=x11 \
  GSK_RENDERER=cairo \
  PYTHONDONTWRITEBYTECODE=1 \
  xvfb-run --auto-servernum --server-args='-screen 0 1280x1024x24 -nolisten tcp' "$@"
