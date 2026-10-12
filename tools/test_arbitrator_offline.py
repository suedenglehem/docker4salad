#!/usr/bin/env python3
"""Offline harness for the manage_groups.py bandwidth arbitrator — ZERO credits.

Fakes the three Salad API calls the arbitrator makes (get_container_group,
list_container_group_instances, reallocate_container_group_instance), puts a
PATH shim for `ssh` (tools/fakebin/ssh) that answers from canned bw.log tails,
and swaps time.sleep/monotonic for a virtual clock so 15-min timeouts run in
microseconds. Then drives mg._arbitrate (and main()'s flag/hook plumbing)
through every branch:

  1  fast host                      -> 0, 0 reallocs
  2  slow -> realloc -> fast        -> 0, exactly 1 realloc
  3  3 slow hosts -> keep 4th       -> 0, exactly 3 reallocs, machine_ids unique
  4  persistent ssh failure         -> 3
  5  frozen log (reporter dead)     -> 3
  6  group flips stopped            -> 1
  7  bw.log missing (old image)     -> 3
  8  image pull stuck               -> 0, 3 shots spent on pull-stuck
  9  no instance ever appears       -> 3 (alloc timeout)
  10 instance churn (no realloc)    -> 0, 0 reallocs
  11 parser: bad/absent/non-start   -> 2; hook: flag absent -> _arbitrate
     never invoked, flag present -> invoked with the cap
  12 relay exit-1 on success (banner+payload) -> measured, not struck
  13 relay exit-1 with NO samples and no "cannot open" -> bail 3
  13b legacy 2-column log (pre-fix image)   -> fast host accepted
  13c legacy 2-column slow host             -> 1 realloc, fast host accepted

Run:  python3 -I tools/test_arbitrator_offline.py   (untrusted-data rule: -I)
"""
import contextlib
import io
import json
import os
import sys
import time as _time
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ["PATH"] = os.path.join(REPO, "tools", "fakebin") + os.pathsep + os.environ["PATH"]

import salad_client as sc  # noqa: E402  (repo import, after sys.path fix)
import utils.manage_groups as mg  # noqa: E402

# --- virtual clock: sleeps advance it, timeouts fire instantly in sim time ---
CLOCK = [1_000.0]
_time.sleep = lambda s: CLOCK.__setitem__(0, CLOCK[0] + s)
_time.monotonic = lambda: CLOCK[0]

SCENARIO = os.path.join(REPO, "tools", "fakebin", "scenario.json")
COUNTER = os.path.join(REPO, "tools", "fakebin", "counter")
os.environ["FAKE_SSH_SCENARIO"] = SCENARIO
os.environ["FAKE_SSH_COUNTER"] = COUNTER

REALLOC_LOG: list[str] = []


def ns(**kw):
    return types.SimpleNamespace(**kw)


def inst(i, state="running", ready=False, machine=None, progress=None):
    return sc.ContainerGroupInstanceInfo(
        id=f"i{i}", state=state, ready=ready,
        ssh_ip="1.2.3.4", ssh_port=22, ssh_host_key_fingerprint=None,
        machine_id=machine if machine is not None else f"m{i}",
        pulling_progress=progress, raw={})


def log_lines(mbps, n=19, base=1_791_500_000):
    """`epoch rx-bytes model-bytes` samples every 10 s at mbps; 19 lines =
    180 s span, so the first read already satisfies span >= grace+window and
    >= 6 window rates. rx and model carry the same value (the verdict comes
    from the rx column; model is display-only)."""
    step = int(mbps * 1e6 / 8 * 10)
    return "".join(f"{base + i * 10} {i * step} {i * step}\n" for i in range(n))


def legacy_lines(mbps, n=19, base=1_791_500_000):
    """Pre-fix image format: `epoch model-bytes` (2 columns). The manager
    parses these with rx := model — i.e. the v1 behavior — so a mixed fleet
    still measures (at the v1 accuracy: 0.0 while xet fills its CAS cache)."""
    step = int(mbps * 1e6 / 8 * 10)
    return "".join(f"{base + i * 10} {i * step}\n" for i in range(n))


def legacy_resp(mbps, n=19):
    return {"exit": 0, "stdout": legacy_lines(mbps, n)}


def set_scenario(scenario):
    # `scenario` is the whole scenario object: a legacy bw-only LIST of
    # responses, or a per-path DICT {"bw": [...], "guard": [...]} (the guard
    # scenarios need independent sequences per probe). Written verbatim —
    # never *unpacked* (unpacking a dict yields its keys, not the responses).
    with open(SCENARIO, "w", encoding="utf-8") as f:
        json.dump(scenario, f)
    # The fake ssh keeps a counter PER PROBE PATH (counter.bw, counter.guard)
    # so the two probes carry independent sequences — clear them all, or the
    # next scenario inherits this one's position in the response list.
    for f in os.listdir(os.path.dirname(COUNTER)):
        if f.startswith(os.path.basename(COUNTER)):
            os.remove(os.path.join(os.path.dirname(COUNTER), f))


def ok_resp(mbps, n=19):
    return {"exit": 0, "stdout": log_lines(mbps, n)}


def group_running():
    return ns(current_status="running")


def patches(d):
    """Install attribute patches on mg / mg.sc; return a restore() callable."""
    saved = []
    for obj, name, val in d:
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)
    def restore():
        for obj, name, val in saved:
            setattr(obj, name, val)
    return restore


def run_arbitrate(cap=30.0, interval=1.0):
    return mg._arbitrate("o", "p", "g", "key", cap, interval)


def scenario_test(name, expect_rc, expect_reallocs, group_fn, list_fn, scenario,
                  cap=30.0, interval=1.0):
    REALLOC_LOG.clear()
    set_scenario(scenario)

    def fake_realloc(org, project, group, instance_id, api_key=None, timeout=30.0):
        REALLOC_LOG.append(instance_id)
        return ns(status_code=202, reason_phrase="Accepted", headers={})

    restore = patches([
        (mg.sc, "get_container_group", group_fn),
        (mg.sc, "list_container_group_instances", list_fn),
        (mg.sc, "reallocate_container_group_instance", fake_realloc),
    ])
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = run_arbitrate(cap=cap, interval=interval)
    finally:
        restore()
    out = buf.getvalue()
    ok = rc == expect_rc and len(REALLOC_LOG) == expect_reallocs
    print(f"{'PASS' if ok else 'FAIL'}  {name}: rc={rc} (want {expect_rc}), "
          f"reallocs={len(REALLOC_LOG)} (want {expect_reallocs})")
    if not ok:
        print(out[-2000:])
    return ok, out, REALLOC_LOG[:]


def main():
    results = []

    # 1 — fast host: accepted, no reallocate
    ok, out, _ = scenario_test(
        "fast host", 0, 0,
        lambda *a, **k: group_running(),
        lambda *a, **k: (inst(1, machine="m1"),),
        [ok_resp(50.0)])
    results.append(ok)
    if ok and "host accepted at 50.0 Mbps" not in out:
        results[-1] = False
        print("FAIL  fast host: verdict line missing")

    # 2 — slow host -> 1 realloc -> fast host accepted
    def list2(*a, **k):
        return (inst(2, machine="m2"),) if len(REALLOC_LOG) >= 1 else (inst(1, machine="m1"),)
    ok, out, log = scenario_test(
        "slow->realloc->fast", 0, 1,
        lambda *a, **k: group_running(), list2,
        [ok_resp(10.0), ok_resp(50.0)])
    results.append(ok)
    if ok and (log != ["i1"] or "m2" not in out):
        results[-1] = False
        print(f"FAIL  slow->realloc->fast: realloc log {log}")

    # 3 — 3 slow hosts -> 3 reallocs -> 4th kept unconditionally
    def list3(*a, **k):
        n = min(len(REALLOC_LOG), 3)
        return (inst(n + 1, machine=f"m{n + 1}"),)
    ok, out, log = scenario_test(
        "3 slow -> keep 4th", 0, 3,
        lambda *a, **k: group_running(), list3,
        [ok_resp(10.0)])
    results.append(ok)
    if ok:
        machines = [f"m{i + 1}" for i in range(4)]
        if log != ["i1", "i2", "i3"] or not all(m in out for m in machines):
            results[-1] = False
            print(f"FAIL  3 slow -> keep 4th: realloc log {log}")
        if "kept the 4th host" not in out:
            results[-1] = False
            print("FAIL  3 slow -> keep 4th: keep line missing")

    # 4 — persistent ssh failure -> 3 strikes -> bail 3
    ok, out, _ = scenario_test(
        "ssh failure", 3, 0,
        lambda *a, **k: group_running(),
        lambda *a, **k: (inst(1, machine="m1"),),
        [{"exit": 255, "stderr": "ssh: connect to host 1.2.3.4 port 22: Connection refused"}])
    results.append(ok)

    # 5 — frozen log (5 samples = 40 s span < grace+window, never a verdict)
    ok, out, _ = scenario_test(
        "frozen log", 3, 0,
        lambda *a, **k: group_running(),
        lambda *a, **k: (inst(1, machine="m1"),),
        [ok_resp(50.0, n=5)])
    results.append(ok)
    if ok and "reporter dead" not in out:
        results[-1] = False
        print("FAIL  frozen log: dead line missing")

    # 6 — group flips stopped mid-measure -> bail 1
    calls = [0]
    def group6(*a, **k):
        calls[0] += 1
        return group_running() if calls[0] <= 1 else ns(current_status="stopped")
    ok, out, _ = scenario_test(
        "group stopped", 1, 0, group6,
        lambda *a, **k: (inst(1, machine="m1"),),
        [ok_resp(50.0, n=5)])
    results.append(ok)

    # 7 — bw.log missing (image predates bw_reporter) -> bail 3
    ok, out, _ = scenario_test(
        "missing bw.log", 3, 0,
        lambda *a, **k: group_running(),
        lambda *a, **k: (inst(1, machine="m1"),),
        [{"exit": 1, "stderr": "tail: cannot open '/tmp/llama-api/bw.log'"}])
    results.append(ok)
    if ok and "predates bw_reporter" not in out:
        results[-1] = False
        print("FAIL  missing bw.log: hint line missing")

    # 8 — image pull stuck -> each pull-stuck spends a shot -> 3 shots -> keep
    ok, out, log = scenario_test(
        "pull stuck", 0, 3,
        lambda *a, **k: group_running(),
        lambda *a, **k: (inst(1, state="downloading", progress=50, machine="m1"),),
        [ok_resp(50.0)], interval=1.0)
    results.append(ok)

    # 9 — no instance ever appears -> alloc timeout -> bail 3
    ok, out, _ = scenario_test(
        "alloc timeout", 3, 0,
        lambda *a, **k: group_running(),
        lambda *a, **k: (),
        [ok_resp(50.0)], interval=1.0)
    results.append(ok)

    # 10 — instance churns on its own (Salad reschedule): re-measure, no shot
    seen = [0]
    def list10(*a, **k):
        seen[0] += 1
        return (inst(2, machine="m2"),) if seen[0] > 1 else (inst(1, machine="m1"),)
    ok, out, log = scenario_test(
        "churn", 0, 0,
        lambda *a, **k: group_running(), list10,
        [ok_resp(50.0)])
    results.append(ok)
    if ok and "churned" not in out:
        results[-1] = False
        print("FAIL  churn: churn line missing")

    # 11 — parser + hook plumbing in main()
    restore = patches([
        (mg.sc, "load_api_key", lambda *a, **k: "key"),
        (mg.sc, "get_container_group", lambda *a, **k: group_running()),
        (mg.sc, "list_container_group_instances", lambda *a, **k: ()),
        (mg.sc, "reallocate_container_group_instance", lambda *a, **k: None),
    ])
    sel = ("ma-casa-in-paris", "llm", ns(name="g", current_status="stopped"))
    arbitrated = []
    restore2 = patches([
        (mg, "_rows", lambda orgs, key: []),
        (mg, "_print_action_table", lambda rows, key, action: [sel]),
        (mg, "_prompt", lambda selectable, action: sel),
        (mg, "_url", lambda g: None),
        (mg, "_confirm", lambda action, s: True),
        (mg, "_run_action", lambda action, s, key: 0),
        (mg, "_arbitrate", lambda *a, **k: arbitrated.append((a, k)) or 0),
    ])
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc_missing = mg.main(["mg", "start", "--min-bw-mbps"])
            rc_nan = mg.main(["mg", "start", "--min-bw-mbps", "x"])
            rc_zero = mg.main(["mg", "start", "--min-bw-mbps", "0"])
            rc_guard = mg.main(["mg", "stats", "--min-bw-mbps", "30"])
            rc_noflag = mg.main(["mg", "start"])
            n_after_noflag = len(arbitrated)
            rc_flag = mg.main(["mg", "start", "--min-bw-mbps", "30"])
    finally:
        restore2()
        restore()
    checks = [
        (rc_missing == 2, f"--min-bw-mbps missing value -> {rc_missing} (want 2)"),
        (rc_nan == 2, f"--min-bw-mbps NaN -> {rc_nan} (want 2)"),
        (rc_zero == 2, f"--min-bw-mbps 0 -> {rc_zero} (want 2)"),
        (rc_guard == 2, f"flag with `stats` -> {rc_guard} (want 2)"),
        (rc_noflag == 0 and n_after_noflag == 0,
         f"flag absent -> rc {rc_noflag}, _arbitrate invoked {n_after_noflag}x (want 0x)"),
        (rc_flag == 0 and len(arbitrated) == 1,
         f"flag present -> rc {rc_flag}, _arbitrate invoked {len(arbitrated)}x (want 1x)"),
    ]
    if arbitrated:
        a, k = arbitrated[0]
        cap_ok = a[4] == 30.0
        checks.append((cap_ok, f"cap passed to _arbitrate: {a[4]} (want 30.0)"))
    for good, line in checks:
        print(f"{'PASS' if good else 'FAIL'}  {line}")
        results.append(good)

    # 12 — Salad relay quirk (live-probed 2026-10-09): the OCI-exec relay exits
    # 1 on SUCCESS — stdout carries the payload plus a "Connecting to container"
    # banner, stderr carries ssh's own host-key warning. The probe must measure,
    # not strike. REGRESSION: the first paid test bailed on "4 consecutive ssh
    # failures" that were four perfectly good reads.
    relay = {
        "exit": 1,
        "stdout": "Connecting to container 4c410b3b-ddb0\n" + log_lines(120.0),
        "stderr": "Warning: Permanently added '[1.2.3.4]:22' (ED25519).\n",
    }
    ok, out, _ = scenario_test(
        "relay exit-1 success", 0, 0,
        lambda *a, **k: group_running(),
        lambda *a, **k: (inst(1, machine="m1"),),
        [relay])
    results.append(ok)
    if ok and "host accepted" not in out:
        results[-1] = False
        print("FAIL  relay exit-1 success: verdict line missing")

    # 13 — exit 1 with NO samples and no "cannot open" (container not exec-ready
    # yet): strikes, BW_SSH_STRIKES consecutive -> bail 3.
    ok, out, _ = scenario_test(
        "relay exit-1 no samples", 3, 0,
        lambda *a, **k: group_running(),
        lambda *a, **k: (inst(1, machine="m1"),),
        [{"exit": 1, "stdout": "Connecting to container 4c410b3b-ddb0\n",
          "stderr": "Warning: Permanently added '[1.2.3.4]:22' (ED25519).\n"}])
    results.append(ok)

    # 13b — legacy image log: 2 columns `epoch model-bytes` (the pre-fix
    # v1 format). The manager's fallback parses rx := model, so a mixed
    # fleet still measures at the v1 accuracy — a fast legacy host must
    # still be accepted (and a slow one judged from model growth).
    ok, out, _ = scenario_test(
        "legacy 2-column log", 0, 0,
        lambda *a, **k: group_running(),
        lambda *a, **k: (inst(1, machine="m1"),),
        [legacy_resp(50.0)])
    results.append(ok)
    if ok and "host accepted" not in out:
        results[-1] = False
        print("FAIL  legacy 2-column log: verdict line missing")

    # 13c — legacy log on a SLOW host: judged from model growth (v1
    # semantics) -> shot spent -> fast host accepted.
    def list13c(*a, **k):
        return (inst(2, machine="m2"),) if len(REALLOC_LOG) >= 1 else (inst(1, machine="m1"),)
    ok, out, log = scenario_test(
        "legacy slow -> shot", 0, 1,
        lambda *a, **k: group_running(), list13c,
        [legacy_resp(10.0), ok_resp(50.0)])
    results.append(ok)
    if ok and log != ["i1"]:
        results[-1] = False
        print(f"FAIL  legacy slow->shot: log {log} (want ['i1'])")

    # --- SSH-presence guard (/.ssh) — the arbitrator spends NO shot while the
    # guard file exists (the user touched it after logging in over SSH; a
    # /reallocate would yank their session's instance away). Payload decides
    # (relay exit-1 quirk): an exact stdout line naming /.ssh = present; ls's
    # "cannot access" = absent (old image / no session -> spend the shot);
    # 255 = transport strike (also spend — holding on probe failure would
    # silently disable the arbitrator).
    #
    # REGRESSION (2026-10-11, live hold-loop): the relay merges the remote's
    # stderr into the client's stdout, so when the file is ABSENT the error
    # `ls: cannot access '/.ssh': No such file or directory` arrives on
    # STDOUT — and contains the guard path. The old `"/.ssh" in p.stdout`
    # substring check matched its own error text -> "present" forever:
    # holding, zero shots, group stuck on a slow node billing. Both stream
    # mappings are pinned: GUARD_ABSENT = the real relay (error on stdout),
    # GUARD_ABSENT_STDERR = the old assumption (error on stderr).
    GUARD_PRESENT = {"exit": 1,
                     "stdout": "Connecting to container 4c410b3b-ddb0\n/.ssh\n",
                     "stderr": "Warning: Permanently added '[1.2.3.4]:22' (ED25519).\n"}
    GUARD_ABSENT = {"exit": 1,
                    "stdout": "Connecting to container 4c410b3b-ddb0\n"
                              "ls: cannot access '/.ssh': No such file or directory\n",
                    "stderr": ""}
    GUARD_ABSENT_STDERR = {"exit": 1,
                           "stdout": "Connecting to container 4c410b3b-ddb0\n",
                           "stderr": "ls: cannot access '/.ssh': No such file or directory\n"}
    GUARD_STRIKE = {"exit": 255,
                    "stderr": "ssh: connect to host 1.2.3.4 port 22: Connection refused"}

    def list_guard(*a, **k):
        return (inst(2, machine="m2"),) if len(REALLOC_LOG) >= 1 else (inst(1, machine="m1"),)

    # 14 — slow host, guard present at shot time -> hold (no shot); guard goes
    # absent (user rm /.ssh) -> shot spent -> fast host accepted.
    ok, out, log = scenario_test(
        "guard present -> hold, absent -> shot", 0, 1,
        lambda *a, **k: group_running(), list_guard,
        {"bw": [ok_resp(10.0), ok_resp(10.0), ok_resp(10.0), ok_resp(50.0)],
         "guard": [GUARD_PRESENT, GUARD_PRESENT, GUARD_ABSENT]})
    results.append(ok)
    if ok and ("ssh session active" not in out or log != ["i1"]):
        results[-1] = False
        print(f"FAIL  guard present->hold: log {log}, hold line in out: "
              f"{'ssh session active' in out}")

    # 15 — guard absent (image predates the guard, or no session) -> the shot
    # proceeds exactly like the pre-guard scenarios.
    ok, out, log = scenario_test(
        "guard absent -> shot proceeds", 0, 1,
        lambda *a, **k: group_running(), list_guard,
        {"bw": [ok_resp(10.0), ok_resp(50.0)], "guard": [GUARD_ABSENT]})
    results.append(ok)
    if ok and log != ["i1"]:
        results[-1] = False
        print(f"FAIL  guard absent->shot: log {log} (want ['i1'])")

    # 15b — guard absent, old stream model (ls error on stderr): the fix
    # checks the combined streams, so BOTH mappings must spend the shot.
    ok, out, log = scenario_test(
        "guard absent (stderr variant) -> shot proceeds", 0, 1,
        lambda *a, **k: group_running(), list_guard,
        {"bw": [ok_resp(10.0), ok_resp(50.0)], "guard": [GUARD_ABSENT_STDERR]})
    results.append(ok)
    if ok and log != ["i1"]:
        results[-1] = False
        print(f"FAIL  guard absent(stderr)->shot: log {log} (want ['i1'])")

    # 16 — guard probe strike (ssh transport failure) at shot time -> shot
    # spent (fail-open: a persistent ssh failure bails via the 4-strike path).
    ok, out, log = scenario_test(
        "guard probe strike -> shot spent", 0, 1,
        lambda *a, **k: group_running(), list_guard,
        {"bw": [ok_resp(10.0), ok_resp(50.0)], "guard": [GUARD_STRIKE]})
    results.append(ok)
    if ok and log != ["i1"]:
        results[-1] = False
        print(f"FAIL  guard strike->shot: log {log} (want ['i1'])")

    # 17 — pull-stuck branch: guard present -> hold, no shot; guard absent ->
    # shots proceed to the 3-shot keep-4th.
    ok, out, log = scenario_test(
        "pull-stuck + guard -> hold", 0, 3,
        lambda *a, **k: group_running(),
        lambda *a, **k: (inst(1, state="downloading", progress=50, machine="m1"),),
        {"bw": [ok_resp(50.0)], "guard": [GUARD_PRESENT, GUARD_ABSENT]},
        interval=1.0)
    results.append(ok)
    if ok and ("ssh session active" not in out
               or log != ["i1", "i1", "i1"]):
        results[-1] = False
        print(f"FAIL  pull-stuck+guard: log {log}, hold line in out: "
              f"{'ssh session active' in out}")

    n_ok = sum(1 for r in results if r)
    print(f"\n{n_ok}/{len(results)} checks passed")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
