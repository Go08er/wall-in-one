#!/usr/bin/env bash
# Run a command, normally the test suite, in a throwaway profile.
#
#   tools/isolated.sh python -m pytest tests -q -m "not gui"
#   tools/isolated.sh python -m pytest tests -q -m gui
#
# This, or one of the flake's checks, is how to run the tests. The command
# gets a new temporary HOME and XDG config, state, cache, data and runtime
# directories, removed afterwards; dead session and system bus addresses (left
# unset, GLib would look for a bus of its own); no portals; none of your
# session's Wayland, niri or X11 display addresses; no LD_LIBRARY_PATH (see the
# README); and a headless Wayland compositor of its own
# (tools/headless-wayland.sh) with the cairo renderer, as the gui-tests check
# uses. The compositor's directory is inside the throwaway root, so a KILL to
# that script, or this script's own KILL after the grace period, leaves nothing
# behind; a KILL to this script itself still leaves the root.
# Nothing here uses X11.
#
# tests/conftest.py sandboxes the test process as well, and refuses and
# reports writes to your real Wall-in-One and Noctalia files, but that is a
# backstop: it cannot see child processes, native code or other programs.
# A bare `pytest` from a developer shell is not a safe way to run the suite.
#
# Outside a Nix shell, the command runs in this flake's dev shell.
#
# Stopping it with TERM, INT (Ctrl-C) or HUP stops the whole workload first --
# the command, tools/headless-wayland.sh and its compositor, with KILL after a
# grace period -- and only then removes the throwaway profile. The exit status is then 128 + the
# signal's number. When the command ends by itself, anything it left running
# in its process group gets the same grace period, then TERM and KILL, before
# the profile goes: nothing is left writing into a removed profile.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

# The workload (the dev shell, or tools/headless-wayland.sh with its
# compositor and the command) runs in its own process group, led by `supervise`, a copy of this shell. A
# TERM, INT or HUP sent to this script goes to that whole group, which gets a
# short grace period before KILL; the throwaway profile is removed only once
# the group is gone, and the exit status is then 128 + the signal's number.
#
# The group is known by more than its number (sweep 5 U-2). A process group's
# number is free for reuse once its last member is gone, so "kill -0 -- -N"
# answering yes proved only that *some* group N exists. Here the leader stays
# in the group until every other member has left it, so the number cannot be
# reused while the leader lives; and before every signal, this script checks
# that the leader is still that process: its own child (parent = $$), with the
# start time it had when it was started, and not a zombie. Only then is the
# group signalled. What remains is the gap between that check and the kill,
# two builtins apart: a reuse there needs the leader to exit, be reaped and
# the kernel's whole PID space to wrap around in between.
GRACE_TENTHS=${WIO_ISOLATED_GRACE_TENTHS:-50}
root=""
leader=""
leader_started=""

remove_profile() {
  if [[ -n "$root" ]]; then
    rm -rf -- "$root"
    root=""
  fi
}

# Field N (0-based) of /proc/<pid>/stat after "pid (comm) ": 0 state,
# 1 ppid, 2 pgrp, 19 start time. Fails when the process is gone.
stat_fields() {
  local line
  read -r line 2>/dev/null <"/proc/$1/stat" || return 1
  # Split unquoted on purpose: after the command name come only numbers and
  # one state letter, so word splitting is all this does (no glob can match).
  # shellcheck disable=SC2206
  fields=(${line##*) })
}

# The group's other members, other than the leader: is any still here?
# shellcheck disable=SC2329  # called from supervise
group_has_others() {
  local self=$1 entry pid
  local -a fields
  for entry in /proc/[0-9]*; do
    pid=${entry#/proc/}
    [[ $pid != "$self" ]] || continue
    stat_fields "$pid" || continue
    if [[ ${fields[2]} == "$self" && ${fields[0]} != Z ]]; then
      return 0
    fi
  done
  return 1
}

# The group leader: runs the command, then stays (keeping the group's number
# its own) until every other member has gone. Signals are caught, not ignored,
# so the command still gets the default action. A member that outlives the
# command is given the grace period, then TERM, then KILL, each sent to the
# group by its own leader, which is alive and therefore still owns the number.
# shellcheck disable=SC2329  # started by run_workload
supervise() {
  trap - EXIT
  trap ':' TERM INT HUP
  local status=0 signal end
  "$@" || status=$?
  for signal in TERM KILL; do
    # Timed by bash's own clock (microseconds, whatever the locale's decimal
    # point), not by passes: one pass reads every /proc entry.
    end=$((${EPOCHREALTIME//[!0-9]/} + GRACE_TENTHS * 100000))
    while group_has_others "$BASHPID"; do
      ((${EPOCHREALTIME//[!0-9]/} < end)) || break
      sleep 0.1
    done
    group_has_others "$BASHPID" || exit "$status"
    if [[ $signal == TERM ]]; then
      kill -s TERM -- "-$BASHPID" 2>/dev/null || true
    fi
  done
  local -a fields
  for entry in /proc/[0-9]*; do
    pid=${entry#/proc/}
    if [[ $pid != "$BASHPID" ]] && stat_fields "$pid" && [[ ${fields[2]} == "$BASHPID" ]]; then
      kill -s KILL "$pid" 2>/dev/null || true
    fi
  done
  exit "$status"
}

# Whether the leader is still the process this script started (see above).
# shellcheck disable=SC2329  # called from stop_workload
leader_alive() {
  local -a fields
  [[ -n "$leader" && -n "$leader_started" ]] || return 1
  stat_fields "$leader" || return 1
  [[ ${fields[1]} == "$$" && ${fields[19]} == "$leader_started" && ${fields[0]} != Z ]]
}

# shellcheck disable=SC2329  # called from the traps run_workload sets
stop_workload() {
  local name=$1 number=$2
  trap '' TERM INT HUP
  if leader_alive; then
    kill -s "$name" -- "-$leader" 2>/dev/null || true
    for ((tenth = 0; tenth < GRACE_TENTHS; tenth++)); do
      leader_alive || break
      sleep 0.1
    done
    if leader_alive; then
      kill -s KILL -- "-$leader" 2>/dev/null || true
    fi
  fi
  if [[ -n "$leader" ]]; then
    wait "$leader" 2>/dev/null || true
  fi
  remove_profile
  exit $((128 + number))
}

run_workload() {
  trap 'stop_workload TERM 15' TERM
  trap 'stop_workload INT 2' INT
  trap 'stop_workload HUP 1' HUP
  set -m
  supervise "$@" &
  leader=$!
  set +m
  local -a fields
  if stat_fields "$leader"; then
    leader_started=${fields[19]}
  fi
  local status=0
  wait "$leader" || status=$?
  leader=""
  leader_started=""
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

# The runtime directory being replaced is the desktop's: tests/conftest.py
# refuses a compositor socket below it as well as below /run/user/<uid>.
live_runtime_dirs="${XDG_RUNTIME_DIR:-}${WIO_TEST_LIVE_RUNTIME_DIRS:+:$WIO_TEST_LIVE_RUNTIME_DIRS}"

status=0
run_workload env -u WAYLAND_DISPLAY -u WAYLAND_SOCKET -u NIRI_SOCKET -u DISPLAY -u LD_LIBRARY_PATH \
  -u WIO_TEST_SESSION_ROOT -u WIO_TEST_SESSION_PID -u WIO_TEST_WAYLAND_SOCKET \
  -u WIO_TEST_WAYLAND_REFUSAL \
  WIO_TEST_LIVE_RUNTIME_DIRS="$live_runtime_dirs" \
  WIO_WAYLAND_PARENT="$root" \
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
  GDK_BACKEND=wayland \
  GSK_RENDERER=cairo \
  PYTHONDONTWRITEBYTECODE=1 \
  bash "$repo/tools/headless-wayland.sh" "$@" ||
  status=$?
remove_profile
exit "$status"
