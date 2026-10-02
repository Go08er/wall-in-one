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
#
# Stopping it with TERM, INT (Ctrl-C) or HUP stops the whole workload first --
# the command, xvfb-run and Xvfb, with KILL after a grace period -- and only
# then removes the throwaway profile. The exit status is then 128 + the
# signal's number.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

# The workload (the dev shell, or xvfb-run with its Xvfb and the command)
# runs as a child in its own process group. A TERM, INT or HUP sent to this
# script goes to that whole group, which gets a short grace period before
# KILL; the throwaway profile is removed only once the group is gone, and the
# exit status is 128 + the signal's number.
GRACE_TENTHS=50
root=""
workload=""

remove_profile() {
  if [[ -n "$root" ]]; then
    rm -rf -- "$root"
    root=""
  fi
}

# shellcheck disable=SC2329  # called from stop_workload
group_alive() {
  kill -0 -- "-$1" 2>/dev/null
}

# shellcheck disable=SC2329  # called from the traps run_workload sets
stop_workload() {
  local name=$1 number=$2 group=${workload:-${!:-}}
  trap '' TERM INT HUP
  if [[ -n "$group" ]]; then
    kill -s "$name" -- "-$group" 2>/dev/null || true
    for ((tenth = 0; tenth < GRACE_TENTHS; tenth++)); do
      group_alive "$group" || break
      sleep 0.1
    done
    if group_alive "$group"; then
      kill -s KILL -- "-$group" 2>/dev/null || true
    fi
    wait "$group" 2>/dev/null || true
  fi
  remove_profile
  exit $((128 + number))
}

run_workload() {
  trap 'stop_workload TERM 15' TERM
  trap 'stop_workload INT 2' INT
  trap 'stop_workload HUP 1' HUP
  set -m
  "$@" &
  workload=$!
  set +m
  local status=0
  wait "$workload" || status=$?
  workload=""
  return "$status"
}

trap remove_profile EXIT

if [[ -z "${IN_NIX_SHELL:-}" ]]; then
  # The inner run forwards the signal and waits out its own grace period;
  # give it time to finish that, and remove its profile, before any KILL.
  GRACE_TENTHS=150
  status=0
  run_workload env -u LD_LIBRARY_PATH nix develop --command "$repo/tools/isolated.sh" "$@" ||
    status=$?
  exit "$status"
fi

root="$(mktemp -d "${TMPDIR:-/tmp}/wio-isolated.XXXXXX")"
mkdir -m 700 "$root/home" "$root/config" "$root/state" "$root/cache" "$root/data" "$root/run"

status=0
run_workload env -u WAYLAND_DISPLAY -u NIRI_SOCKET -u DISPLAY -u LD_LIBRARY_PATH \
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
  xvfb-run --auto-servernum --server-args='-screen 0 1280x1024x24 -nolisten tcp' "$@" ||
  status=$?
remove_profile
exit "$status"
