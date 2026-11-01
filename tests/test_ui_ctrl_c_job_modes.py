"""R14T, adopted: under a shell's job control the countdown leaves the terminal right.

The real `Countdown` on a pty of the test's own, under a shell-like harness (the review's probe,
`.project-notes/review-r14t/jobctl_probe.py`). Its job has SIGTTOU, SIGTTIN and SIGTSTP at their
defaults, as a shell's child has them; the "shell" holds the terminal in a prompt mode of its own
while it has it, so a job that overwrites it shows; and the shell is bash-style (on a stop it
puts its own mode back, on ``fg`` it gives the job a cooked terminal) or fish-style (it saves the
stopped job's mode and gives it back at ``fg``). The job exits by ``sys.exit``, so the atexit
backstop runs.

* **T-L1:** a countdown sent to the background (Ctrl-Z, then ``bg``) that then saw input retired
  its key watch and forgot what to put back, so under a fish-style shell, back in the
  foreground, the countdown's end, a SIGTERM and the process's exit all left cbreak on.
* **R41-5 and the shell's mode:** in the background nothing reads the terminal (no SIGTTIN), and
  nothing sets it: the shell's own mode stays.

Only PIDs this file started are signalled: the harness, and its job by a pidfd.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

REPO = Path(__file__).resolve().parents[1]

HARNESS = r"""
import fcntl, json, os, select, signal, sys, termios, time
cfg = json.loads(sys.argv[1])
M = cfg["master"]
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
for s in (signal.SIGTTOU, signal.SIGTTIN, signal.SIGTSTP):
    signal.signal(s, signal.SIG_IGN)  # an interactive shell ignores them
if cfg.get("tostop"):
    m = termios.tcgetattr(0); m[3] |= termios.TOSTOP; termios.tcsetattr(0, termios.TCSANOW, m)
ORIG = termios.tcgetattr(0)
PROMPT = termios.tcgetattr(0)
PROMPT[3] &= ~(termios.ICANON | termios.ECHO | termios.ECHOCTL)
PROMPT[6][termios.VMIN] = 1; PROMPT[6][termios.VTIME] = 0
go_r, go_w = os.pipe(); ready_r, ready_w = os.pipe()
rep_r, rep_w = os.pipe(); win_r, win_w = os.pipe()
job = os.fork()
if job == 0:
    os.setpgid(0, 0)
    os.close(M)
    os.close(go_w)  # a harness that dies before the go is an end of file here, not a wait
    for s in (signal.SIGTTOU, signal.SIGTTIN, signal.SIGTSTP):
        signal.signal(s, signal.SIG_DFL)  # as a shell's child has them
    if cfg.get("stderr_tty"):
        os.dup2(0, 2)
    if not os.read(go_r, 1):
        os._exit(1)
    from gi.repository import GLib
    import wall_in_one.ui.quit_countdown as qc
    if cfg.get("race"):
        original = qc._in_the_background
        state = {"n": 0}
        def windowed(fd):
            answer = original(fd)
            state["n"] += 1
            if state["n"] == 1 and not answer:
                os.write(win_w, b"W")  # after the check, before the SIGTTOU block and tcsetattr
                time.sleep(1.5)
            return answer
        qc._in_the_background = windowed
    loop = GLib.MainLoop()
    end = {}
    def done():
        end["end"] = "done"; loop.quit()
    def cancelled():
        end["end"] = "cancelled"; loop.quit(); return None
    countdown = qc.Countdown(1, cfg.get("seconds", 5), 1000, done=done, cancelled=cancelled)
    countdown.start()
    os.write(ready_w, b"1" if countdown.keys else b"0")
    if cfg.get("abandon_after"):
        def abandon():
            end["end"] = "abandoned"; loop.quit(); return False
        GLib.timeout_add(int(cfg["abandon_after"] * 1000), abandon)
    loop.run()
    if end.get("end") != "abandoned":
        countdown.stop()
    if cfg.get("linger"):
        time.sleep(cfg["linger"])  # the quit's own work after the countdown, before the exit
    report = {"end": end.get("end"), "saved_at_exit": sorted(qc._SAVED),
              "fd_at_exit": countdown._fd}
    os.write(rep_w, json.dumps(report).encode())
    sys.stdout.flush()
    sys.exit(0)  # atexit (_restore_all) runs
for fd in (go_r, ready_w, rep_w, win_w):
    os.close(fd)
print(f"SPAWNED {job}", flush=True)
log = []
stops = []
status = None
saved_job_mode = None
def name(mode):
    if mode == ORIG: return "ORIG"
    if mode == PROMPT: return "PROMPT"
    if CBREAK is not None and mode == CBREAK: return "CBREAK"
    return "OTHER:" + repr(mode[:4])
def poll(seconds, fg_on_stop=False):
    global status
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found, value = os.waitpid(job, os.WNOHANG | os.WUNTRACED)
        if found:
            if os.WIFSTOPPED(value):
                stops.append(os.WSTOPSIG(value))
                stopped_by = signal.Signals(os.WSTOPSIG(value)).name
                log.append(f"stopped by {stopped_by}, mode {name(termios.tcgetattr(0))}")
                if not fg_on_stop:
                    return
                log.append("fg after the stop (bash style)")
                os.tcsetpgrp(0, job); os.kill(job, signal.SIGCONT)
                continue
            status = value
            return
        time.sleep(0.01)
CBREAK = None
try:
    os.setpgid(job, job)
    if not cfg.get("background_from_start"):
        os.tcsetpgrp(0, job)
    os.write(go_w, b"1")
    r, _, _ = select.select([ready_r], [], [], 15)
    if not r:
        raise RuntimeError("job never became ready")
    keys = os.read(ready_r, 1) == b"1"
    CBREAK = termios.tcgetattr(0)
    log.append(f"ready: watch={keys}, mode {name(CBREAK)} (ORIG? {CBREAK == ORIG})")
    for step in cfg["steps"]:
        if status is not None:
            log.append(f"(job gone before {step})"); break
        if step == "tstp":
            os.kill(job, signal.SIGTSTP)
            _, v = os.waitpid(job, os.WUNTRACED)
            assert os.WIFSTOPPED(v), v
            if cfg["style"] == "fish":
                saved_job_mode = termios.tcgetattr(0)
            os.tcsetpgrp(0, os.getpgrp())
            termios.tcsetattr(0, termios.TCSANOW, ORIG)  # the shell's own mode back
            kept = name(saved_job_mode) if saved_job_mode else None
            log.append(f"tstp: stopped; job mode saved by shell: {kept}")
        elif step == "prompt":
            termios.tcsetattr(0, termios.TCSANOW, PROMPT)
        elif step == "cooked":
            termios.tcsetattr(0, termios.TCSANOW, ORIG)
        elif step == "bg":
            os.kill(job, signal.SIGCONT); log.append("bg")
        elif step == "fg":
            if cfg["style"] == "fish" and saved_job_mode is not None:
                termios.tcsetattr(0, termios.TCSANOW, saved_job_mode)
            os.tcsetpgrp(0, job); os.kill(job, signal.SIGCONT)
            log.append(f"fg: mode given to the job {name(termios.tcgetattr(0))}")
        elif step == "input":
            os.write(M, b"input\n"); log.append("input line written")
        elif step == "esc":
            os.write(M, b"\x1b"); log.append("Esc written")
        elif step.startswith("sleep:"):
            poll(float(step.split(":")[1]))
        elif step.startswith("kill:"):
            sig = getattr(signal, step.split(":")[1]); os.kill(job, sig)
            log.append(f"sent {sig.name}")
        elif step == "await_window":
            r, _, _ = select.select([win_r], [], [], 15)
            assert r, "no window"
            os.read(win_r, 1); log.append("job is between _in_the_background and tcsetattr")
        elif step == "wait_exit":
            poll(20, fg_on_stop=True)
        else:
            raise ValueError(step)
    if status is None:
        poll(20, fg_on_stop=True)
    if status is None:
        raise RuntimeError("job did not finish")
finally:
    if status is None:
        try:
            os.tcsetpgrp(0, job); os.kill(job, signal.SIGCONT)
            os.kill(job, signal.SIGKILL); os.waitpid(job, 0)
        except Exception:
            pass
    os.tcsetpgrp(0, os.getpgrp())
final = termios.tcgetattr(0)
r, _, _ = select.select([rep_r], [], [], 0.5)
data = os.read(rep_r, 65536) if r else b""
report = json.loads(data.decode()) if data else None
if status is None:
    exitinfo = None
elif os.WIFSIGNALED(status):
    exitinfo = "signal " + signal.Signals(os.WTERMSIG(status)).name
else:
    exitinfo = f"exit {os.WEXITSTATUS(status)}"
print("RESULT " + json.dumps({"stops": [signal.Signals(s).name for s in stops], "exit": exitinfo,
      "final_mode": name(final), "job": report, "log": log}), flush=True)
"""

#: Each scenario: the harness's steps, and the terminal's mode once the job has gone: "ORIG",
#: as it was before the countdown, or "PROMPT", the shell's own, left alone.
SCENARIOS: dict[str, tuple[dict[str, Any], str]] = {
    "bg-input-fg/fish": (
        {
            "style": "fish",
            "steps": [
                "tstp",
                "prompt",
                "bg",
                "cooked",
                "input",
                "sleep:0.8",
                "prompt",
                "cooked",
                "fg",
                "wait_exit",
            ],
        },
        "ORIG",
    ),
    "bg-input-fg/fish/SIGTERM": (
        {
            "style": "fish",
            "steps": [
                "tstp",
                "prompt",
                "bg",
                "cooked",
                "input",
                "sleep:0.8",
                "cooked",
                "fg",
                "sleep:0.5",
                "kill:SIGTERM",
                "wait_exit",
            ],
        },
        "ORIG",
    ),
    "bg-input-fg/fish/atexit": (
        {
            "style": "fish",
            "abandon_after": 2.0,
            "steps": [
                "tstp",
                "prompt",
                "bg",
                "cooked",
                "input",
                "sleep:0.6",
                "cooked",
                "fg",
                "wait_exit",
            ],
        },
        "ORIG",
    ),
    "bg-end-then-fg/fish": (
        {
            "style": "fish",
            "linger": 2.5,
            "steps": ["tstp", "prompt", "bg", "sleep:5.6", "cooked", "fg", "wait_exit"],
        },
        "ORIG",
    ),
    "control-bg-input-fg/bash": (
        {
            "style": "bash",
            "steps": [
                "tstp",
                "prompt",
                "bg",
                "cooked",
                "input",
                "sleep:0.8",
                "prompt",
                "cooked",
                "fg",
                "wait_exit",
            ],
        },
        "ORIG",
    ),
    "control-bg-end": ({"style": "bash", "steps": ["tstp", "prompt", "bg", "wait_exit"]}, "PROMPT"),
    "control-bg-atexit": (
        {"style": "bash", "abandon_after": 1.5, "steps": ["tstp", "prompt", "bg", "wait_exit"]},
        "PROMPT",
    ),
    "control-fg/SIGTERM": (
        {"style": "bash", "steps": ["sleep:0.8", "kill:SIGTERM", "wait_exit"]},
        "ORIG",
    ),
    "control-fg-roundtrip/bash": (
        {"style": "bash", "steps": ["tstp", "prompt", "cooked", "fg", "wait_exit"]},
        "ORIG",
    ),
}


def _run(cfg: dict[str, Any]) -> dict[str, Any]:
    master, slave = os.openpty()
    cfg = {**cfg, "master": master}
    child = subprocess.Popen(
        [sys.executable, "-c", HARNESS, json.dumps(cfg)],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        pass_fds=(master,),
        bufsize=0,
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
    )
    stop_drain = threading.Event()

    def drain() -> None:
        # The job's line goes to its stderr, a pipe; this keeps the pty's output from filling.
        while not stop_drain.is_set():
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    os.read(master, 65536)
                except OSError:
                    return

    drainer = threading.Thread(target=drain, daemon=True)
    drainer.start()
    job_fd = None
    try:
        assert child.stdout is not None
        line = child.stdout.readline()
        assert line.startswith(b"SPAWNED "), (line, child.poll())
        job_fd = os.pidfd_open(int(line.split()[1]))
        out, err = child.communicate(timeout=60)
        results = [text for text in out.decode().splitlines() if text.startswith("RESULT ")]
        assert results, err.decode()[-2000:]
        result: dict[str, Any] = json.loads(results[0][len("RESULT ") :])
        return result
    finally:
        if job_fd is not None:
            # The job, whether or not the harness is still there to end it.
            with contextlib.suppress(ProcessLookupError):
                signal.pidfd_send_signal(job_fd, signal.SIGKILL)
        if child.poll() is None:
            child.kill()
            child.wait(10)
        if job_fd is not None:
            os.close(job_fd)
        stop_drain.set()
        drainer.join(2)
        os.close(slave)
        os.close(master)


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_r14t_the_terminal_under_job_control(scenario: str) -> None:
    cfg, expected = SCENARIOS[scenario]
    result = _run(cfg)
    assert result["stops"] == [], f"{scenario}: the job was stopped: {result}"
    assert result["final_mode"] == expected, f"{scenario}: {result}"
