#!/usr/bin/env python3
"""Offline suite for the `--wait` / `--tmax` flags in utils/sl_admin.py.

Zero API calls and zero real time: the Salad client is monkeypatched (the
start POST is recorded), and `time.monotonic` / `time.sleep` are faked so a
`--tmax` budget runs on a fake clock. Run:  python3 -I tools/test_start_wait_offline.py

Covers:
  * `--tmax` value parsing — `90` (seconds) and `5m` (minutes) accepted;
    `abc` / `5x` / `0` / `-5` / missing value are usage errors (rc=2);
  * `--wait` / `--tmax` are start-only (rc=2 with any other action);
  * `start … --wait` — accepted start (202) seamlessly keeps polling in the
    same process until an instance reports ready (rc=0);
  * `start … --tmax N` implies the wait (rc=0 when ready within budget);
  * `--tmax` expiry — the wait stops trying (rc=1) and the group is LEFT
    RUNNING (no stop call is ever made);
  * the group bailing (status failed) mid-wait -> rc=1;
  * a plain `start` (no --wait/--tmax) does NOT poll — one start POST, done.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    """Import utils/sl_admin.py as a module (it is not a package)."""
    path = os.path.join(_ROOT, "utils", "sl_admin.py")
    spec = importlib.util.spec_from_file_location("sl_admin", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ADMIN = _load()
sc = ADMIN.sc
ORG, PROJ, GRP = "ma-casa-in-paris", "llm", "atx-swift-27b-q5"
fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        fails.append(f"{name}  [{detail}]")
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + (f"  ({detail})" if detail and not cond else ""))


# --- fakes --------------------------------------------------------------------

class FakeGroup:
    """Minimal stand-in for ContainerGroupInfo (name/status + the raw dict the
    table/wait printers read)."""

    def __init__(self, name: str, status: str = "running"):
        self.name = name
        self.current_status = status
        self.raw = {
            "name": name,
            "current_state": {"status": status, "instance_status_counts": {}},
            "networking": {},
        }


class FakeInstance:
    def __init__(self, id: str, state: str, ready: bool | None = None):
        self.id = id
        self.state = state
        self.ready = ready


class FakeTime:
    """Fake clock: monotonic() starts at `start` and each sleep() jumps `step`
    seconds. Records the number of sleeps so a wait's poll count is visible."""

    def __init__(self, start: float = 0.0, step: float = 1.0):
        self.now = start
        self.step = step
        self.sleeps = 0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps += 1
        self.now += self.step


class FakeAPI:
    """Fakes the client calls the start/wait path uses; records every call.

    group_status:  the group's current_state.status on each GET (a callable of
                   the call count, so a test can change it mid-flight).
    instance_plan: a callable of the instance-list call count -> list of
                   FakeInstance (so readiness can flip between polls).
    """

    def __init__(self, group_status, instance_plan):
        self.group_status = group_status
        self.instance_plan = instance_plan
        self.calls: list = []
        self._get_n = 0
        self._inst_n = 0

    def install(self) -> None:
        def get_container_group(org, project, name, api_key=None):
            self.calls.append(("get", org, project, name))
            self._get_n += 1
            return FakeGroup(name, self.group_status(self._get_n))

        def list_container_group_instances(org, project, name, api_key=None):
            self.calls.append(("instances", org, project, name))
            self._inst_n += 1
            return tuple(self.instance_plan(self._inst_n))

        def start_container_group(req, api_key=None):
            self.calls.append(("start", req.organization_name, req.project_name,
                               req.container_group_name))

        sc.get_container_group = get_container_group
        sc.list_container_group_instances = list_container_group_instances
        sc.start_container_group = start_container_group
        sc.load_api_key = lambda: "fake-key"


def answer(*replies):
    """Feed canned stdin to input() (a module-level name shadows the builtin)."""
    it = iter(replies)

    def _input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    ADMIN.input = _input


def run(argv: list[str], clock: FakeTime) -> tuple[int, str]:
    """main() with the fake clock installed; returns (rc, captured stdout)."""
    ADMIN.time = clock
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = ADMIN.main(["sl_admin.py"] + argv)
    return rc, buf.getvalue()


def main() -> int:
    fleet = ADMIN._fleet()

    # --- 1. --tmax value parsing (usage errors: rc=2, no API) ----------------
    for bad in ("abc", "5x", "0", "0m", "-5", "1.5"):
        api = FakeAPI(lambda n: "running", lambda n: [])
        api.install()
        answer("y")
        rc = ADMIN.main(["sl_admin.py", "start", f"{ORG}/{PROJ}", GRP, "--tmax", bad])
        check(f"tmax parse: {bad!r} -> rc=2", rc == 2, f"rc={rc}")
        check(f"tmax parse: {bad!r} makes no API call", api.calls == [], repr(api.calls))

    api = FakeAPI(lambda n: "running", lambda n: [])
    api.install()
    rc = ADMIN.main(["sl_admin.py", "start", f"{ORG}/{PROJ}", GRP, "--tmax"])
    check("tmax parse: missing value -> rc=2", rc == 2, f"rc={rc}")

    # --- 2. start-only flags -------------------------------------------------
    api = FakeAPI(lambda n: "running", lambda n: [])
    api.install()
    rc = ADMIN.main(["sl_admin.py", "stop", f"{ORG}/{PROJ}", GRP, "--wait"])
    check("--wait on stop -> rc=2", rc == 2, f"rc={rc}")
    check("--wait on stop makes no API call", api.calls == [], repr(api.calls))

    api = FakeAPI(lambda n: "running", lambda n: [])
    api.install()
    rc = ADMIN.main(["sl_admin.py", "wait", f"{ORG}/{PROJ}", GRP, "--wait"])
    check("--wait on the wait action -> rc=2", rc == 2, f"rc={rc}")

    api = FakeAPI(lambda n: "running", lambda n: [])
    api.install()
    rc = ADMIN.main(["sl_admin.py", "stop", f"{ORG}/{PROJ}", GRP, "--tmax", "5m"])
    check("--tmax on stop -> rc=2", rc == 2, f"rc={rc}")

    api = FakeAPI(lambda n: "running", lambda n: [])
    api.install()
    rc = ADMIN.main(["sl_admin.py", "list", "--tmax", "5m"])
    check("--tmax on list -> rc=2", rc == 2, f"rc={rc}")

    # --- 3. start … --wait: poll in-process until ready ----------------------
    # poll 1: not ready; poll 2: ready -> rc=0, one start POST, two instance lists.
    api = FakeAPI(lambda n: "running", lambda n: [FakeInstance("a" * 8, "downloading", False)]
                  if n == 1 else [FakeInstance("a" * 8, "running", True)])
    api.install()
    answer("y")
    clock = FakeTime(start=0.0, step=1.0)
    rc, out = run(["start", f"{ORG}/{PROJ}", GRP, "--wait", "--interval", "1"], clock)
    check("start --wait: ready on 2nd poll -> rc=0", rc == 0, f"rc={rc}")
    check("start --wait: exactly one start POST",
          [c for c in api.calls if c[0] == "start"] ==
          [("start", ORG, PROJ, GRP)], repr(api.calls))
    check("start --wait: polled instances twice",
          [c[0] for c in api.calls].count("instances") == 2, repr(api.calls))
    check("start --wait: the wait announcement replaced the run-wait-hint",
          "keeps polling until it is ready" in out and "wait for it to become ready:" not in out,
          out)
    check("start --wait: printed the READY line", "READY:" in out, out)

    # --- 4. start … --tmax N implies the wait --------------------------------
    # budget 90 s (fake clock crawls 1 s/sel sleep), ready on 2nd poll -> rc=0.
    api = FakeAPI(lambda n: "running", lambda n: [FakeInstance("a" * 8, "downloading", False)]
                  if n == 1 else [FakeInstance("a" * 8, "running", True)])
    api.install()
    answer("y")
    clock = FakeTime(start=0.0, step=1.0)
    rc, out = run(["start", f"{ORG}/{PROJ}", GRP, "--tmax", "90", "--interval", "1"], clock)
    check("tmax 90 (seconds) implies wait: rc=0", rc == 0, f"rc={rc}")
    check("tmax 90 implies wait: budget shown in the wait header", "time budget 90s" in out, out)
    check("tmax 90 implies wait: polled instances twice",
          [c[0] for c in api.calls].count("instances") == 2, repr(api.calls))

    # minutes: 5m = 300 s budget in the header.
    api = FakeAPI(lambda n: "running", lambda n: [FakeInstance("a" * 8, "running", True)])
    api.install()
    answer("y")
    clock = FakeTime(start=0.0, step=1.0)
    rc, out = run(["start", f"{ORG}/{PROJ}", GRP, "--tmax", "5m", "--interval", "1"], clock)
    check("tmax 5m (minutes) implies wait: rc=0", rc == 0, f"rc={rc}")
    check("tmax 5m: budget shown as 300s", "time budget 300s" in out, out)

    # --- 5. --tmax expiry: stop trying, leave the group running --------------
    # tmax 10 s, each sleep jumps 6 s: polls at t=0 and t=6, deadline at t=10
    # is hit before the 3rd poll -> rc=1, no stop ever issued.
    api = FakeAPI(lambda n: "running", lambda n: [FakeInstance("a" * 8, "downloading", False)])
    api.install()
    answer("y")
    clock = FakeTime(start=0.0, step=6.0)
    rc, out = run(["start", f"{ORG}/{PROJ}", GRP, "--wait", "--tmax", "10",
                   "--interval", "1"], clock)
    check("tmax expiry: rc=1", rc == 1, f"rc={rc}")
    check("tmax expiry: printed the budget-exhausted notice",
          "time budget exhausted" in out, out)
    check("tmax expiry: polled twice (t=0, t=6), gave up before t=12",
          [c[0] for c in api.calls].count("instances") == 2, repr(api.calls))
    check("tmax expiry: the group is LEFT RUNNING (no stop call)",
          [c[0] for c in api.calls].count("start") == 1
          and not any(c[0] in ("stop", "delete") for c in api.calls), repr(api.calls))

    # --- 6. the group bails mid-wait -> rc=1 ----------------------------------
    # GETs: 1 = selection (stopped, pre-start), 2 = first wait poll -> failed.
    api = FakeAPI(lambda n: "stopped" if n == 1 else "failed",
                  lambda n: [FakeInstance("a" * 8, "downloading", False)])
    api.install()
    answer("y")
    clock = FakeTime(start=0.0, step=1.0)
    rc, out = run(["start", f"{ORG}/{PROJ}", GRP, "--wait", "--interval", "1"], clock)
    check("group bails (failed) mid-wait -> rc=1", rc == 1, f"rc={rc}")
    check("group bails: the bail notice was printed", "bail:" in out, out)

    # --- 7. a plain start does NOT poll ---------------------------------------
    api = FakeAPI(lambda n: "running", lambda n: [FakeInstance("a" * 8, "running", True)])
    api.install()
    answer("y")
    clock = FakeTime(start=0.0, step=1.0)
    rc, out = run(["start", f"{ORG}/{PROJ}", GRP, "--interval", "1"], clock)
    check("plain start: rc=0", rc == 0, f"rc={rc}")
    check("plain start: no instance polling at all",
          [c[0] for c in api.calls].count("instances") == 0, repr(api.calls))
    check("plain start: keeps the run-wait-hint",
          "wait for it to become ready:" in out, out)
    check("plain start: no fake sleep happened", clock.sleeps == 0, f"sleeps={clock.sleeps}")

    # --- 8. --wait + --min-bw-mbps + --tmax parse together (confirm abort) ----
    # The arbitrator itself is not exercised offline; this pins that all three
    # start-only flags coexist on one line without a usage error (rc=1 = the
    # confirm was reached and answered 'n').
    api = FakeAPI(lambda n: "stopped", lambda n: [])
    api.install()
    answer("n")
    rc = ADMIN.main(["sl_admin.py", "start", f"{ORG}/{PROJ}", GRP,
                     "--wait", "--tmax", "5m", "--min-bw-mbps", "100"])
    check("start --wait --tmax 5m --min-bw-mbps 100 parses (rc=1 = aborted at confirm)",
          rc == 1, f"rc={rc}")
    check("… and only the selection GET ran before the confirm (no start POST)",
          [c[0] for c in api.calls] == ["get"], repr(api.calls))

    # --- summary ---------------------------------------------------------------
    if fails:
        print(f"\n{len(fails)} failure(s)")
        for f in fails:
            print(f"  FAIL {f}")
        return 1
    print("\nALL START-WAIT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
