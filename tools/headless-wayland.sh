#!/usr/bin/env bash
# Run a command on a headless Wayland compositor of its own, and nothing else.
#
#   tools/headless-wayland.sh <command...>
#
# Wall-in-One supports only Wayland, so this is where everything in testing
# draws: the gui-tests check, tools/isolated.sh and the prototype's smoke
# script. Nothing in testing uses X11.
#
# The compositor is Weston with the headless backend and the pixman renderer
# (no GPU): one 1600x1200 output at scale 1 and a seat with a keyboard and a
# pointer that no device drives. Its shell is desktop-shell, quietened: no
# panel, no lock screen, no animations, no idle blanking, no input method, no
# Xwayland. desktop-shell maps a window at the size the client asks for, as a
# desktop does; kiosk-shell would make every window fullscreen, and Weston's
# other shells do not map an ordinary window at all.
#
# Its socket is in a new private directory of its own (mode 700) below
# $WIO_WAYLAND_PARENT, which defaults to ${TMPDIR:-/tmp}: never in $XDG_RUNTIME_DIR and never in /run/user. The
# command gets the socket's absolute path as WAYLAND_DISPLAY and as
# WIO_TEST_WAYLAND_SOCKET (tests/conftest.py takes only the latter, and only
# after checking it), and GDK_BACKEND=wayland, so that a missing compositor
# fails instead of falling back to another backend. DISPLAY, WAYLAND_SOCKET
# and NIRI_SOCKET are removed: the command can reach no other display.
#
# The compositor stays in this script's process group, so whatever stops the
# group (tools/isolated.sh, timeout, Ctrl-C) stops it too, and setpriv gives it
# KILL if this script dies first. Weston starts its shell's helper client in a
# session of its own; that client exits when the compositor goes. When the
# command ends, by itself or by a signal, the compositor gets TERM, then KILL
# after a grace period, and its directory is removed. The exit status is the
# command's (128 + the number of a TERM, INT or HUP this script caught); a
# compositor that died during a run the command passed makes it 1.
#
# A KILL sent to this script itself (`timeout -s KILL`, or anything else that
# cannot be caught) stops Weston and its client but leaves the directory
# behind: nothing can run after a KILL. tools/isolated.sh therefore sets
# WIO_WAYLAND_PARENT to its throwaway profile root, which it removes on every
# exit path. Used bare, this script leaves wio-wl.* in the parent after a KILL
# and does not sweep other runs' directories (one may be live).
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "usage: $0 <command...>" >&2
  exit 2
fi
if ! command -v weston >/dev/null; then
  echo "headless-wayland: weston is not on PATH (it is in the dev shell and the gui-tests check)" >&2
  exit 127
fi

parent=${WIO_WAYLAND_PARENT:-${TMPDIR:-/tmp}}
if [[ ! -d $parent ]]; then
  echo "headless-wayland: WIO_WAYLAND_PARENT/TMPDIR $parent is not a directory" >&2
  exit 1
fi

unset DISPLAY WAYLAND_DISPLAY WAYLAND_SOCKET NIRI_SOCKET WIO_TEST_WAYLAND_SOCKET

WIDTH=1600
HEIGHT=1200
GRACE_TENTHS=${WIO_WAYLAND_GRACE_TENTHS:-50}
START_TENTHS=${WIO_WAYLAND_START_TENTHS:-600}
dir=""
compositor=""
caught=0

# Whether process $1 is alive and not a zombie (`kill -0` answers yes for a
# zombie, and the compositor stays one until it is waited for).
alive() {
  local line
  read -r line 2>/dev/null <"/proc/$1/stat" || return 1
  line=${line##*) }
  [[ ${line%% *} != Z ]]
}

show_log() {
  local name
  for name in weston.log weston.out; do
    if [[ -s $dir/$name ]]; then
      echo "--- $name" >&2
      tail -n 40 "$dir/$name" >&2
    fi
  done
}

# The compositor is this script's own child and is not waited for until it
# has stopped, so its number cannot have been reused by another process.
stop_compositor() {
  local tenth
  if [[ -n $compositor ]]; then
    kill -s TERM "$compositor" 2>/dev/null || true
    for ((tenth = 0; tenth < GRACE_TENTHS; tenth++)); do
      alive "$compositor" || break
      sleep 0.1
    done
    kill -s KILL "$compositor" 2>/dev/null || true
    wait "$compositor" 2>/dev/null || true
    compositor=""
  fi
  if [[ -n $dir ]]; then
    rm -rf -- "$dir"
    dir=""
  fi
}

trap stop_compositor EXIT
# Caught, not ignored: the command still gets the default action. Bash runs
# these once the command in the foreground has returned.
trap 'caught=15' TERM
trap 'caught=2' INT
trap 'caught=1' HUP

dir="$(mktemp -d "$parent/wio-wl.XXXXXX")"
chmod 700 "$dir"
socket="$dir/wayland-0"
# A Unix socket's path holds at most 107 bytes; Weston would fail to bind a
# longer one and the wait below would only see a socket that never appears.
if ((${#socket} > 107)); then
  echo "headless-wayland: $socket is too long for a Unix socket; use a shorter TMPDIR (or WIO_WAYLAND_PARENT, when you run this script yourself; tools/isolated.sh sets it)" >&2
  exit 1
fi

cat >"$dir/weston.ini" <<'EOF'
[core]
idle-time=0
require-input=false
xwayland=false

[shell]
panel-position=none
locking=false
animation=none
close-animation=none
startup-animation=none
focus-animation=none
background-color=0xff202020

[input-method]
path=
EOF

launcher=()
if command -v setpriv >/dev/null; then
  launcher=(setpriv --pdeathsig KILL --)
fi
XDG_RUNTIME_DIR="$dir" "${launcher[@]}" weston \
  --backend=headless --renderer=pixman --width="$WIDTH" --height="$HEIGHT" \
  --fake-seat --shell=desktop --idle-time=0 --config="$dir/weston.ini" \
  --socket=wayland-0 --log="$dir/weston.log" \
  </dev/null >"$dir/weston.out" 2>&1 &
compositor=$!

for ((tenth = 0; tenth < START_TENTHS; tenth++)); do
  [[ -S $socket ]] && break
  if ((caught != 0)); then
    exit $((128 + caught))
  fi
  if ! alive "$compositor"; then
    echo "headless-wayland: weston exited before its socket appeared" >&2
    show_log
    exit 1
  fi
  sleep 0.1
done
if [[ ! -S $socket ]]; then
  echo "headless-wayland: weston's socket did not appear within $((START_TENTHS / 10)) s" >&2
  show_log
  exit 1
fi

if ((caught != 0)); then
  exit $((128 + caught))
fi
status=0
WAYLAND_DISPLAY="$socket" WIO_TEST_WAYLAND_SOCKET="$socket" GDK_BACKEND=wayland "$@" ||
  status=$?

# A TERM, INT or HUP to the group reaches Weston as well: that is not a crash.
if ((caught == 0)) && ! alive "$compositor"; then
  echo "headless-wayland: the compositor exited during the run" >&2
  show_log
  if ((status == 0)); then
    status=1
  fi
fi
stop_compositor
if ((caught != 0)); then
  exit $((128 + caught))
fi
exit "$status"
