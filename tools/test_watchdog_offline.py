#!/usr/bin/env python3
"""Offline harness for docker/idle_watchdog.py — the SSH-presence guard and
its SSH_GUARD_MAX_HOLD cap. ZERO side effects.

Stubs ready_ok / activity / stop_group / terminate_pid1, swaps time for a
virtual clock (sleep advances it), points SSH_GUARD_FILE at a temp file, and
drives main() through the guard branches (timeout 100 s, poll 30 s — idle
crosses 100 s at tick 4):

  1  guard present at timeout -> NO kill, stop held off; guard removed
     (rm /.ssh) -> kill fires on the same tick (the idle clock was never
     restarted, so it is already past the timeout)
  2  guard absent -> kill fires at timeout (normal billing hygiene)
  3  SSH_GUARD=0 -> guard file present but IGNORED -> kill fires
  4  SSH_GUARD_MAX_HOLD=0 + guard present forever -> NEVER kills (uncapped
     hold, the pre-v2.3 behavior; under the default cap this same run would
     have been killed at tick 40 / 1200 s)
  5  default cap (1200 s) + guard present forever -> the cap IS enforced:
     kill fires at tick 40 (idle 1200 s) with the "max hold" line, even
     though the guard file is still present
  6  activity during a hold RESETS the cap clock: one score bump at tick 10
     pushes the (cap=1000 s) kill from tick 34 past tick 40 -> no kill
  7  explicit small cap (160 s) -> kill at tick 6 (idle 180 s), proving the
     cap is configurable and fires at the right moment, not later

Run:  python3 -I tools/test_watchdog_offline.py
"""
import contextlib
import importlib
import io
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "docker"))

GUARD_FILE = os.path.join(tempfile.gettempdir(), "wd_offline_guard")

BASE_ENV = {
    "IDLE_SHUTDOWN": "heartbeat",
    "HEARTBEAT_TIMEOUT": "100",
    "IDLE_GRACE": "1000",
    "IDLE_ARM_GRACE": "10",
    "SALAD_STOP_KEY": "testkey",
    "SALAD_ORG": "o", "SALAD_PROJECT": "p", "SALAD_GROUP": "g",
    "SSH_GUARD_FILE": GUARD_FILE,
}


class FakeTime:
    def __init__(self):
        self.now = 1000.0

    def sleep(self, s):
        self.now += s

    def monotonic(self):
        return self.now

    def strftime(self, fmt, t):
        return "T"

    def gmtime(self):
        return None


def load(ssh_guard: str, max_hold: "str | None" = None):
    """Import idle_watchdog fresh with the scenario env (module constants read
    env at import time, so each scenario needs a reload). max_hold=None leaves
    the env var unset -> the code default (1200 s)."""
    for k, v in BASE_ENV.items():
        os.environ[k] = v
    os.environ["SSH_GUARD"] = ssh_guard
    if max_hold is None:
        os.environ.pop("SSH_GUARD_MAX_HOLD", None)
    else:
        os.environ["SSH_GUARD_MAX_HOLD"] = max_hold
    import idle_watchdog as iw
    return importlib.reload(iw)


def run(iw, ticks_before_kill: "int | None" = None, max_ticks: int = 40,
        bump_tick: "int | None" = None):
    """Drive iw.main(). ticks_before_kill: tick at which the guard file is
    REMOVED (simulates `rm /.ssh`); None = guard stays as the scenario set it.
    bump_tick: tick at which the activity score steps 42.0 -> 43.0 ONCE
    (simulates one chat request; the step is monotonic, so it counts as
    activity exactly once). Returns (rc, log_text, stop_calls, term_calls)."""
    iw.time = FakeTime()
    iw.ready_ok = lambda: True
    iw.stop_group = lambda: (STOPS.append(1), True)[1]
    iw.terminate_pid1 = lambda: (TERMS.append(1), 0)[1]
    state = {"ticks": 0}

    def activity():
        state["ticks"] += 1
        if ticks_before_kill is not None and state["ticks"] == ticks_before_kill:
            try:
                os.remove(GUARD_FILE)  # the user's `rm /.ssh`
            except FileNotFoundError:
                pass
        if state["ticks"] > max_ticks:
            raise SystemExit  # safety net: the loop must not run forever
        if bump_tick is not None and state["ticks"] >= bump_tick:
            return 43.0  # one chat request, then silence again
        return 42.0  # constant score = NO activity

    iw.activity = activity
    STOPS.clear()
    TERMS.clear()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = iw.main()
    except SystemExit:
        rc = "loop-forever"
    return rc, buf.getvalue(), len(STOPS), len(TERMS)


def set_guard(present: bool):
    try:
        os.remove(GUARD_FILE)
    except FileNotFoundError:
        pass
    if present:
        open(GUARD_FILE, "w").close()


STOPS: list = []
TERMS: list = []
results = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}")
    results.append(ok)


# 1 — guard present at timeout -> held off; removed at tick 22 -> kill fires
# on that same tick: the idle clock counts from the last real activity (no
# restart on hold), so at tick 22 idle is 660 s — far past the 100 s timeout.
# The "stop held off" line lands on tick 20 (STATUS_EVERY_TICKS rate-limit).
set_guard(True)
iw = load("1")
rc, out, stops, terms = run(iw, ticks_before_kill=22)
check("guard present -> held off, removed -> kill",
      rc == 0 and stops == 1 and terms == 1 and "stop held off" in out
      and "stopping the" in out,
      f"rc={rc} stops={stops} terms={terms} held_off={'stop held off' in out}")

# 2 — guard absent from the start -> kill fires at timeout, no hold line.
set_guard(False)
iw = load("1")
rc, out, stops, terms = run(iw)
check("guard absent -> kill fires",
      rc == 0 and stops == 1 and terms == 1 and "stop held off" not in out,
      f"rc={rc} stops={stops} terms={terms}")

# 3 — SSH_GUARD=0 -> the guard file is present but the check is disabled.
set_guard(True)
iw = load("0")
rc, out, stops, terms = run(iw)
set_guard(False)
check("SSH_GUARD=0 -> guard ignored, kill fires",
      rc == 0 and stops == 1 and terms == 1 and "stop held off" not in out,
      f"rc={rc} stops={stops} terms={terms}")

# 4 — SSH_GUARD_MAX_HOLD=0 (uncapped) + guard present forever -> NEVER kills
# over 45 ticks / 1350 s of idle. Under the default 1200 s cap this exact
# scenario dies at tick 40, so surviving proves the 0 = no-cap switch.
set_guard(True)
iw = load("1", max_hold="0")
rc, out, stops, terms = run(iw, max_ticks=45)
set_guard(False)
check("cap=0 + guard present -> never kills (uncapped, pre-v2.3)",
      rc == "loop-forever" and stops == 0 and terms == 0 and "no cap" in out,
      f"rc={rc} stops={stops} terms={terms}")

# 5 — default cap (1200 s) + guard present forever -> the cap is enforced:
# kill at tick 40 (idle 1200 s >= cap) even though the guard file is still
# there. Hold lines (ticks 10/20/30) carry the cap in the message.
set_guard(True)
iw = load("1")
rc, out, stops, terms = run(iw, max_ticks=45)
set_guard(False)
check("default cap 1200 s + guard present -> cap kill at 20 min idle",
      rc == 0 and stops == 1 and terms == 1
      and "max hold 1200s" in out and "stop held off" in out,
      f"rc={rc} stops={stops} terms={terms} cap_line={'max hold 1200s' in out}")

# 6 — activity during a hold RESETS the cap clock. cap=1000 s: without the
# bump the kill would land at tick 34 (idle 1020 s); the one score bump at
# tick 10 restarts the idle clock, so at tick 40 idle is only 900 s -> the
# group is still alive. No kill over 40 ticks proves the reset happened.
set_guard(True)
iw = load("1", max_hold="1000")
rc, out, stops, terms = run(iw, bump_tick=10, max_ticks=40)
set_guard(False)
check("activity during hold resets the cap clock",
      rc == "loop-forever" and stops == 0 and terms == 0
      and "stop held off" in out,
      f"rc={rc} stops={stops} terms={terms}")

# 7 — explicit small cap (160 s): kill at tick 6 (idle 180 s), i.e. at the
# cap, not at the next round number. Proves the knob is configurable and the
# firing point is the cap itself.
set_guard(True)
iw = load("1", max_hold="160")
rc, out, stops, terms = run(iw, max_ticks=40)
set_guard(False)
check("cap=160 s -> kill at tick 6 (idle 180 s)",
      rc == 0 and stops == 1 and terms == 1 and "max hold 160s" in out,
      f"rc={rc} stops={stops} terms={terms} "
      f"line={[l for l in out.splitlines() if 'max hold' in l]}")

n_ok = sum(1 for r in results if r)
print(f"\n{n_ok}/{len(results)} checks passed")
sys.exit(0 if n_ok == len(results) else 1)
