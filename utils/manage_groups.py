#!/usr/bin/env python3
"""Manage Salad container groups — list, live-refresh, start, stop, wait, stats, delete.

Usage:
  python3 utils/manage_groups.py [ACTION] [ORG] [--interval N]

  ACTION    list (default)  one-shot status table
            refresh         re-render the table every N seconds (Ctrl-C to stop)
            start           pick a group (by # or name) and start it — INCURS COST
            stop            pick a group and stop it
            wait            poll a group until at least one instance reports
                            ready, then exit (group status alone is not enough:
                            Salad marks a group `running` while instances are
                            still allocating/downloading/creating)
            stats           print llama-server stats (like utils/llama_stats.py:
                            tps / queue / totals, --interval loop, Ctrl-C stops)
                            — but ONLY when the group is fully ready: status
                            running, all `replicas` instances present, and each
                            one ready=true (stricter than wait's "at least one
                            ready"). Otherwise it prints the state and exits 1
                            without scraping
            delete          pick a group and delete it — IRREVERSIBLE
  ORG       ma-casa-in-paris | akl-on-salad (default: every known org)
  --interval N
            poll period in seconds for refresh / wait / stats (default 5)
  --min-bw-mbps N
            start only: BANDWIDTH ARBITRATOR — after the start is accepted,
            watch the HF model download's speed (read from the container's
            ${API_STATE_DIR}/bw.log over SSH, written by docker/bw_reporter.py)
            and when the median rate over a 2-min window falls below N Mbps,
            force Salad to REALLOCATE the instance to a new node via the API
            (the rejected node is excluded from the account's allocation pool
            for 48 h, so each shot lands on a genuinely new host). Max 3
            shots; if no faster host appears in 3 shots the 4th host is KEPT
            and the download continues. The group is NEVER stopped/SIGTERMed
            for this — reallocate is the only lever (a stop/start loop across
            a row of slow nodes is exactly what kills billing hygiene).
            Needs the bw_reporter image generation (base/generic v8, q6 v10,
            ampere v6) and SSH access to the instance (account-level key).
            Exit codes: 0 accepted / download complete / kept 4th host;
            1 group bail (group stopped/failed); 2 usage; 3 arbitrator bail
            (persistent ssh failure, alloc timeout, reallocate API failure,
            missing bw.log = old image). Ctrl-C leaves the group running.

  python3 utils/manage_groups.py                     # status, all known orgs
  python3 utils/manage_groups.py akl-on-salad        # one org
  python3 utils/manage_groups.py refresh             # live table, Ctrl-C to stop
  python3 utils/manage_groups.py delete              # numbered table -> prompt -> confirm
  python3 utils/manage_groups.py start akl-on-salad  # start a group in that org
  python3 utils/manage_groups.py start --min-bw-mbps 30   # start + arbitrator
  python3 utils/manage_groups.py wait                # after start: poll until ready
  python3 utils/manage_groups.py stats               # stats, only if fully ready

The SaladCloud API has no list-orgs / list-projects operations, so this walks
a fixed map of the account's orgs and their projects. Add new projects to
KNOWN_PROJECTS below when you create them in the web UI.

For every group (active or not) it prints: status, age (since creation),
uptime (time in the current state), instance counts, and the gateway URL from
networking.dns (the host Salad assigns per group; the port is routed by the
gateway itself, so no :port suffix is needed). For groups that are running it
also fetches their live instances and prints the SSH line (`ssh -p PORT
root@IP`) plus the host-key fingerprint.

Notes:
  * start bills you: running instances are charged per second.
  * delete is irreversible and leaves a NAME TOMBSTONE: recreating a just-
    deleted group name 400s with name_conflict for 10+ minutes — use a fresh
    name if you recreate (keep the model alias stable for clients).
  * There is no group-level image "refresh" in the API (live-probed
    2026-10-06); to run a new image, PATCH a new image ref — the deployers do
    this (digest-pinned, to bypass the worker image cache).
"""

from __future__ import annotations

import os
import runpy
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

# salad_client.py lives at the repo root; running this as utils/manage_groups.py
# puts utils/ on sys.path, not the root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import salad_client as sc

# org -> projects known to exist in this account (web-UI created; no list API).
KNOWN_PROJECTS: dict[str, tuple[str, ...]] = {
    "ma-casa-in-paris": ("qwen38-27b", "llm"),
    "akl-on-salad": ("default", "comfy"),
}

ACTIONS = ("list", "refresh", "start", "stop", "wait", "stats", "delete")

# The canonical /metrics scraper (single source of truth; it lives in docker/
# because that directory is Dockerfile.multistage's build context). Delegated
# to via runpy — the same pattern as utils/llama_stats.py.
_LLM_STATS = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docker", "llama_stats.py")
)

# --- Bandwidth arbitrator (start --min-bw-mbps) ------------------------------
# The container's bw_reporter.py samples MODEL_DIR size every 10 s into
# BW_LOG_PATH; the arbitrator reads the tail over SSH (Salad SSH is a
# shell-less OCI exec — exactly ONE plain command per call), takes the
# median rate over BW_VERDICT_WINDOW, and POSTs the instance /reallocate
# endpoint when it is below the cap. The group is never stopped.
BW_POLL_INTERVAL = 15.0    # manager read period (reporter samples every 10 s)
BW_RAMP_GRACE = 60.0       # skip the first minute (baseline + ramp) before measuring
BW_VERDICT_WINDOW = 120.0  # median over rates inside the last 2 min decides
BW_MIN_SAMPLES = 6         # minimum rate samples in the window before a verdict
BW_MAX_SHOTS = 3           # reallocate attempts; the 4th host is kept unconditionally
BW_SSH_TIMEOUT = 20.0      # per-probe ssh timeout (seconds)
BW_SSH_STRIKES = 4         # consecutive ssh failures before bailing
BW_ALLOC_TIMEOUT = 600.0   # max wait for a running instance with ssh fields
BW_PULL_TIMEOUT = 900.0    # max time in image-pull state; a stuck pull counts as a shot
BW_LOG_FROZEN = 180.0      # no new bw.log lines while not ready => reporter dead
BW_TAIL_LINES = 80         # ~13 min of 10 s samples per probe
BW_LOG_PATH = "/tmp/llama-api/bw.log"
BW_SSH_OPTS = (
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
    "-o", "LogLevel=ERROR",
)


def _url(group: sc.ContainerGroupInfo) -> str | None:
    net = group.raw.get("networking") or {}
    dns = net.get("dns")
    return f"https://{dns}" if dns else None


def _fmt_delta(secs: int) -> str:
    d, rem = divmod(max(0, secs), 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _since(group: sc.ContainerGroupInfo, iso: str | None, running_only: bool) -> str:
    """Human-readable time since an ISO timestamp; '-' when not applicable."""
    if running_only and group.current_status != "running":
        return "-"
    if not iso:
        return "-"
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return "?"
    return _fmt_delta(int((datetime.now(timezone.utc) - then).total_seconds()))


def _age(group: sc.ContainerGroupInfo) -> str:
    """Time since the group was created; '-' when not running (like UPTIME)."""
    return _since(group, group.create_time, running_only=True)


def _uptime(group: sc.ContainerGroupInfo) -> str:
    """Time since the group entered its current state."""
    return _since(group, group.state_start_time, running_only=True)


def _instances(group: sc.ContainerGroupInfo) -> str:
    state = group.raw.get("current_state") or {}
    counts = state.get("instance_status_counts") or {}
    parts = [f"{k.replace('_count', '')}={v}" for k, v in sorted(counts.items()) if v]
    return ", ".join(parts) if parts else "-"


# Spec ContainerGroupInstanceState: allocating|downloading|creating|running|
# stopping — the live API has been seen reporting the final state as "ready"
# instead of "running", so the fallback accepts both.
_READY_STATES = ("ready", "running")


def _instance_ready(inst: sc.ContainerGroupInstanceInfo) -> bool:
    """Spec ContainerGroupInstance.ready (boolean): passing readiness checks,
    or — with no probe defined — fully started. Falls back to the state
    string for API payloads that omit the boolean."""
    if inst.ready is not None:
        return inst.ready
    return (inst.state or "") in _READY_STATES


def _instance_frag(inst: sc.ContainerGroupInstanceInfo) -> str:
    state = inst.state or "?"
    if inst.ready is None:
        return state
    return f"{state}{'(ready)' if inst.ready else ''}"


def _live_instances(
    org: str, project: str, group_name: str, key: str
) -> "list[sc.ContainerGroupInstanceInfo] | None":
    """Live instances of a group; None when the list call itself failed
    (transient — the caller keeps polling)."""
    try:
        return list(sc.list_container_group_instances(org, project, group_name, api_key=key))
    except sc.SaladApiError:
        return None


def _ssh_lines(org: str, project: str, group_name: str, key: str) -> list[sc.ContainerGroupInstanceInfo]:
    """Live instances of a running group (for the SSH line); [] on API error."""
    try:
        return list(sc.list_container_group_instances(org, project, group_name, api_key=key))
    except sc.SaladApiError as e:
        print(f"{'':<34} {'':<28} {'':<12} {'':>9} {'':>9} {'(instances failed)':<16} HTTP {e.status_code}")
        return []


def _rows(orgs: list[str], key: str) -> list[tuple[str, str, object]]:
    """Table rows in display order.

    ('group', scope, ContainerGroupInfo) per group, or ('note', scope,
    preformatted-rest) for scopes that are empty or unlistable.
    """
    rows: list[tuple[str, str, object]] = []
    for org in orgs:
        for project in KNOWN_PROJECTS[org]:
            scope = f"{org}/{project}"
            try:
                groups = sc.list_container_groups(org, project, api_key=key)
            except sc.SaladApiError as e:
                rows.append(("note", scope, f"{'(list failed)':<28} HTTP {e.status_code}"))
                continue
            if not groups:
                rows.append(("note", scope, f"{'(no groups)':<28}"))
                continue
            for g in sorted(groups, key=lambda x: x.name):
                rows.append(("group", scope, g))
    return rows


def _group_line(scope: str, g: sc.ContainerGroupInfo, prefix: str = "") -> str:
    url = _url(g) or "-"
    return (
        f"{prefix}{scope:<34} {g.name:<28} {(g.current_status or '?'):<12}"
        f" {_age(g):>9} {_uptime(g):>9} {_instances(g):<16} {url}"
    )


def _ssh_rows(scope: str, g: sc.ContainerGroupInfo, key: str, prefix: str = "") -> list[str]:
    if g.current_status != "running":
        return []
    org, project = scope.split("/", 1)
    lines = []
    for inst in _ssh_lines(org, project, g.name, key):
        if not inst.ssh_line:  # API omits ssh_* when the group has no SSH config
            continue
        fp = f"  [{inst.ssh_host_key_fingerprint}]" if inst.ssh_host_key_fingerprint else ""
        lines.append(f"{prefix}{'':<34} {'':<28} {'':<12} {'':>9} {'':>9} {inst.ssh_line}{fp}")
    return lines


def _print_table(rows: list[tuple[str, str, object]], key: str, as_of: str | None = None) -> int:
    """Classic status table (list mode). Returns the number of groups."""
    header = f"{'ORG/PROJECT':<34} {'GROUP':<28} {'STATUS':<12} {'AGE':>9} {'UPTIME':>9} {'INSTANCES':<16} URL"
    print(header)
    print("-" * len(header))
    if as_of:
        print(f"as of {as_of} UTC")
    total = 0
    for kind, scope, payload in rows:
        if kind == "note":
            print(f"{scope:<34} {payload}")
            continue
        total += 1
        print(_group_line(scope, payload))
        for line in _ssh_rows(scope, payload, key):
            print(line)
    print("-" * len(header))
    print(f"{total} group(s)")
    return total


def _print_action_table(
    rows: list[tuple[str, str, object]], key: str, action: str
) -> list[tuple[str, str, sc.ContainerGroupInfo]]:
    """Numbered table for the start/stop/delete prompt; returns the
    selectable (org, project, group) list in # order."""
    header = (
        f"{'#':>3} {'ORG/PROJECT':<34} {'GROUP':<28} {'STATUS':<12}"
        f" {'AGE':>9} {'UPTIME':>9} {'INSTANCES':<16} URL"
    )
    title = "pick a group to wait for" if action == "wait" else f"groups to {action}"
    print(f"{title} — enter the # (or name) below:")
    print(header)
    print("-" * len(header))
    selectable: list[tuple[str, str, sc.ContainerGroupInfo]] = []
    for kind, scope, payload in rows:
        if kind == "note":
            print(f"{'':>3} {scope:<34} {payload}")
            continue
        selectable.append((scope.split("/", 1)[0], scope.split("/", 1)[1], payload))
        print(_group_line(scope, payload, prefix=f"{len(selectable):>3} "))
        for line in _ssh_rows(scope, payload, key, prefix=f"{'':>3} "):
            print(line)
    print("-" * len(header))
    print(f"{len(selectable)} group(s)")
    return selectable


def _prompt(
    selectable: list[tuple[str, str, sc.ContainerGroupInfo]], action: str
) -> tuple[str, str, sc.ContainerGroupInfo] | None:
    """Ask for a # (or an unambiguous group name) in a loop; None to quit."""
    while True:
        try:
            raw = input(f"enter # to {action} (1..{len(selectable)}), a group name, or q: ").strip()
        except EOFError:
            print()
            return None
        if not raw:
            continue
        if raw.lower() in ("q", "quit"):
            return None
        if raw.isdigit():
            i = int(raw)
            if 1 <= i <= len(selectable):
                return selectable[i - 1]
            print(f"out of range: 1..{len(selectable)}")
            continue
        matches = [t for t in selectable if t[2].name == raw]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            print(
                f"ambiguous: {raw!r} exists in {', '.join(o + '/' + p for o, p, _ in matches)}"
                " — use its #"
            )
        else:
            print(f"no group named {raw!r} in the table — use its #")


def _confirm(action: str, sel: tuple[str, str, sc.ContainerGroupInfo]) -> bool:
    _org, _project, g = sel
    if action == "delete":
        if g.current_status == "running":
            print("\nWARNING: this group is RUNNING — billing continues until its instances stop;")
            print("consider `stop` first if you do not mean to leave it running.")
        print(f"WARNING: delete is IRREVERSIBLE — {g.name} (currently {g.current_status or '?'}) "
              "is removed with all its configuration.")
        try:
            ans = input(f"type the group name to confirm: ").strip()
        except EOFError:
            print()
            return False
        return ans == g.name
    if action == "start" and g.current_status != "running":
        print("\nnote: starting a group INCURS COST (running instances bill per second).")
    try:
        ans = input(f"confirm {action} of {g.name} [y/N]: ").strip().lower()
    except EOFError:
        print()
        return False
    return ans in ("y", "yes")


def _run_action(action: str, sel: tuple[str, str, sc.ContainerGroupInfo], key: str) -> int:
    org, project, g = sel
    name = g.name
    try:
        if action == "start":
            sc.start_container_group(sc.StartContainerGroupRequest(org, project, name), api_key=key)
            print(f"202 Accepted — {name} is starting.")
            print(f"wait for it to become ready:  python3 utils/manage_groups.py wait {org}")
            print(
                f"then run Claude Code:        cl_salad_deploy --org {org} --project {project} --group {name}"
            )
        elif action == "stop":
            sc.stop_container_group(sc.StopContainerGroupRequest(org, project, name), api_key=key)
            print(f"202 Accepted — {name} is stopping.")
        elif action == "delete":
            sc.delete_container_group(sc.DeleteContainerGroupRequest(org, project, name), api_key=key)
            print(f"202 Accepted — {name} deleted.")
            print("note: the name is TOMBSTONED — recreating it now 400s with name_conflict")
            print("      for 10+ minutes; use a fresh name if you need to recreate it.")
            try:
                sc.get_container_group(org, project, name, api_key=key)
                print("note: the group is still visible right away (202 is async) — re-check in a moment.")
            except sc.SaladApiError as e:
                if e.status_code == 404:
                    print("verified: group no longer exists.")
                else:
                    print(f"note: verification GET returned HTTP {e.status_code} — re-check with: python3 utils/manage_groups.py")
        else:
            return 2
        return 0
    except sc.SaladApiError as e:
        print(f"error: {e}")
        return 1
    except ValueError as e:
        print(f"error: {e}")
        return 2


def _refresh(orgs: list[str], key: str, interval: float) -> int:
    print(f"refreshing every {interval:g}s — Ctrl-C to stop")
    first = True
    while True:
        if not first:
            sys.stdout.write("\033[2J\033[H")  # clear screen + home, `watch`-style
            sys.stdout.flush()
        first = False
        rows = _rows(orgs, key)
        as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        _print_table(rows, key, as_of=as_of)
        time.sleep(interval)


def _wait(scope: str, name: str, key: str, interval: float) -> int:
    """Poll one group every `interval` seconds until an instance is ready.

    "Ready" = current_status running AND at least one live instance reports
    ready=true (spec ContainerGroupInstance.ready). The group status alone
    is NOT enough: Salad marks a group `running` while its instances are
    still allocating/downloading/creating (the model pull takes minutes),
    and the group's instance_status_counts carry no ready key at all
    (only allocating/creating/running/stopping counts). Before the first
    instance shows up we keep polling, same while none is ready yet.

    Returns 0 when ready; 1 when the group gives up (stopped/failed/...)
    or the group GET itself fails.
    """
    org, project = scope.split("/", 1)
    print(f"waiting for {name} to become ready (polling every {interval:g}s, Ctrl-C to stop)")
    while True:
        try:
            g = sc.get_container_group(org, project, name, api_key=key)
        except sc.SaladApiError as e:
            print(f"error: {e}")
            return 1
        status = g.current_status or "?"
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"{ts} UTC  {status:<12} {_instances(g)}"
        ready_now = False
        if status == "running":
            live = _live_instances(org, project, g.name, key)
            if live is None:
                line += "  (instance list failed — retrying)"
            elif live:
                line += "  " + ",".join(_instance_frag(i) for i in live)
                ready_now = any(_instance_ready(i) for i in live)
            else:
                line += "  (no instances yet)"
        print(line)
        if status in ("stopped", "succeeded", "failed", "error", "deleted"):
            print(f"bail: {name} is {status} — it will not become ready (check the Salad UI)")
            return 1
        if ready_now:
            print(f"READY: {name} has a ready instance — {_url(g) or '-'}")
            return 0
        time.sleep(interval)


def _stats(scope: str, name: str, key: str, interval: float) -> int:
    """Print llama-server stats for one group — but only when it is FULLY
    ready: status running, at least `replicas` live instances present, and
    every one of them ready=true. "Fully ready" is stricter than `wait`'s
    "at least one ready": a group with N expected instances is not serving at
    full capacity until all N are in.

    Single check, no polling: when the group is not fully ready we print its
    state and exit 1 without scraping (use `wait` to block until ready, then
    re-run stats). When it is, we hand off to the canonical scraper (runpy,
    like utils/llama_stats.py) against the gateway URL — the Salad-Api-Key
    auto-load for *.salad.cloud happens inside it. Ctrl-C stops the loop; a
    scrape failure exits 1 with the scraper's own hint.
    """
    org, project = scope.split("/", 1)
    try:
        g = sc.get_container_group(org, project, name, api_key=key)
    except sc.SaladApiError as e:
        print(f"error: {e}")
        return 1
    status = g.current_status or "?"
    want = int(g.raw.get("replicas") or 1)
    live: "list[sc.ContainerGroupInstanceInfo] | None" = None
    if status == "running":
        live = _live_instances(org, project, g.name, key)
    state = f"{status:<12} {_instances(g)}"
    if status == "running":
        if live is None:
            state += "  (instance list failed)"
        elif live:
            state += "  " + ",".join(_instance_frag(i) for i in live)
        else:
            state += "  (no instances yet)"

    if status != "running":
        print(f"not fully ready — no stats:  {name}  {state}")
        print(f"it is {status} — start it first:  python3 utils/manage_groups.py start {org}")
        return 1
    if live is None:
        print(f"cannot verify readiness — instance list failed:  {name}  {state}")
        print("transient API error — re-run stats, or `wait` if it persists.")
        return 1
    not_ready = [i for i in live if not _instance_ready(i)]
    if len(live) < want or not_ready:
        detail = []
        if len(live) < want:
            detail.append(f"only {len(live)}/{want} instance(s) present")
        if not_ready:
            detail.append("not ready yet: " + ",".join(_instance_frag(i) for i in not_ready))
        print(f"not fully ready — no stats:  {name}  {state}")
        print("; ".join(detail) + f" — wait for it, then re-run stats:  python3 utils/manage_groups.py wait {org}")
        return 1

    url = _url(g)
    if not url:
        print(f"error: {name} is ready but has no gateway DNS — nothing to scrape.")
        return 1
    print(f"fully ready: {name} ({_instances(g)}) — {url} — Ctrl-C stops")
    # The scraper parses sys.argv itself (url positional + --interval N).
    sys.argv = ["llama_stats.py", url, "--interval", f"{interval:g}"]
    runpy.run_path(_LLM_STATS, run_name="__main__")
    return 0


def _bw_read(inst: sc.ContainerGroupInstanceInfo) -> "tuple[list[tuple[int, int]] | None, str | None)":
    """Read the tail of the instance's bw.log over SSH. Salad's SSH exec is
    shell-less (one plain command per call — no pipes/;/quotes), so the probe
    is exactly `tail -n N <path>`. Returns (pairs, None) on success (pairs
    may be empty), or (None, kind) where kind is "missing" (file absent —
    the image predates bw_reporter) or a connection description (strike)."""
    cmd = [
        "ssh", *BW_SSH_OPTS, "-p", str(inst.ssh_port), f"root@{inst.ssh_ip}",
        "tail", "-n", str(BW_TAIL_LINES), BW_LOG_PATH,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=BW_SSH_TIMEOUT)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return None, f"conn ({e.__class__.__name__})"
    if p.returncode == 0:
        pairs: list[tuple[int, int]] = []
        for line in p.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                pairs.append((int(parts[0]), int(parts[1])))
        return pairs, None
    if p.returncode == 1 and "cannot open" in p.stderr:
        return None, "missing"
    return None, f"conn (exit {p.returncode}: {p.stderr.strip()[:120]})"


def _bw_rates(pairs: "list[tuple[int, int]]") -> "list[tuple[int, float]]":
    """Per-interval download rates as (end_epoch, Mbps). Negative deltas (xet
    restarts a partial) floor at 0 — the median absorbs the dip."""
    rates: list[tuple[int, float]] = []
    for (t0, b0), (t1, b1) in zip(pairs, pairs[1:]):
        dt = t1 - t0
        if dt <= 0:
            continue
        rates.append((t1, max(0, b1 - b0) * 8.0 / dt / 1e6))
    return rates


def _bw_acquire(
    org: str, project: str, name: str, key: str, excluded: "set[str]", interval: float
) -> "tuple[sc.ContainerGroupInstanceInfo | None, str | int | None]":
    """Poll until a live instance is `running` with ssh fields and a
    machine_id not already excluded. Returns (inst, None) when usable;
    (None, 1) when the group bailed (stopped/failed/...); (None, 3) on alloc
    timeout; (inst, "pull") when the image pull is stuck past
    BW_PULL_TIMEOUT (the caller spends a shot on it — registry pull
    bandwidth is a node-bandwidth proxy)."""
    no_inst_since = time.monotonic()
    pull_since: float | None = None
    while True:
        try:
            g = sc.get_container_group(org, project, name, api_key=key)
        except sc.SaladApiError as e:
            print(f"error: {e}")
            return None, 1
        status = g.current_status or "?"
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        if status in ("stopped", "succeeded", "failed", "error", "deleted"):
            print(f"bail: {name} is {status} — arbitrator stops, group left as-is")
            return None, 1
        if status == "running":
            live = _live_instances(org, project, name, key)
            if live:
                inst = live[-1]  # newest wins (replicas=1 prod assumption)
                if inst.state == "running" and inst.ssh_ip and inst.ssh_port \
                        and (inst.machine_id or "") not in excluded:
                    print(f"{ts} UTC  acquired: instance {inst.id[:8]} on node "
                          f"{inst.machine_id or '?'} — measuring download bandwidth")
                    return inst, None
                if inst.state == "downloading":
                    if pull_since is None:
                        pull_since = time.monotonic()
                    prog = inst.pulling_progress
                    prog_s = f"{prog}%" if prog is not None else "?"
                    print(f"{ts} UTC  image pull {prog_s} ({time.monotonic() - pull_since:.0f}s)")
                    if time.monotonic() - pull_since > BW_PULL_TIMEOUT:
                        print(f"image pull stuck > {BW_PULL_TIMEOUT:.0f}s at {prog_s} — "
                              f"counts as a shot")
                        return inst, "pull"
                    no_inst_since = time.monotonic()
                else:
                    pull_since = None
                    no_inst_since = time.monotonic()
                    print(f"{ts} UTC  instance {inst.state or '?'} — waiting for "
                          f"running + ssh fields")
            else:
                pull_since = None
                print(f"{ts} UTC  no instances yet")
                if time.monotonic() - no_inst_since > BW_ALLOC_TIMEOUT:
                    print(f"bail: no instance appeared within {BW_ALLOC_TIMEOUT:.0f}s")
                    return None, 3
        time.sleep(interval)


def _bw_measure(
    org: str, project: str, name: str, key: str,
    inst: sc.ContainerGroupInstanceInfo, cap_mbps: float, shots: int
) -> "tuple[str, float]":
    """Watch bw.log until a verdict. Returns (verdict, mbps):
      "fast"   median >= cap — host accepted
      "slow"   median < cap — spend a shot
      "done"   instance ready via API — download+load complete
      "churn"  instance id changed (Salad rescheduled on its own) — re-measure
      "group"  group stopped/failed — bail 1
      "ssh"    BW_SSH_STRIKES consecutive ssh failures — bail 3
      "noimg"  bw.log missing/empty — image predates bw_reporter, bail 3
      "dead"   log frozen BW_LOG_FROZEN while not ready — reporter dead, bail 3
    All timing is log-time (reporter epochs), so ssh gaps never skew it."""
    strikes = 0
    last_ts: int | None = None
    last_change = time.monotonic()
    while True:
        time.sleep(BW_POLL_INTERVAL)
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        try:
            g = sc.get_container_group(org, project, name, api_key=key)
        except sc.SaladApiError as e:
            print(f"{ts} UTC  group GET failed (HTTP {e.status_code}) — retrying")
            continue
        if (g.current_status or "?") in ("stopped", "succeeded", "failed", "error", "deleted"):
            return "group", 0.0
        live = _live_instances(org, project, name, key)
        cur = live[-1] if live else None
        if cur is None or cur.id != inst.id:
            print(f"{ts} UTC  instance churned — re-acquiring (shots unchanged)")
            return "churn", 0.0
        if _instance_ready(cur):
            return "done", 0.0
        inst = cur
        pairs, err = _bw_read(inst)
        if err == "missing" or (err is None and not pairs):
            print(f"bail: {BW_LOG_PATH} missing/empty — the image predates "
                  f"bw_reporter; the arbitrator needs the rebuilt images")
            return "noimg", 0.0
        if err is not None:
            strikes += 1
            print(f"{ts} UTC  ssh probe failed ({strikes}/{BW_SSH_STRIKES}): {err}")
            if strikes >= BW_SSH_STRIKES:
                print(f"bail: {BW_SSH_STRIKES} consecutive ssh failures")
                return "ssh", 0.0
            continue
        strikes = 0
        newest = pairs[-1][0]
        if last_ts is not None and newest > last_ts:
            last_change = time.monotonic()
        last_ts = newest
        rates = _bw_rates(pairs)
        span = newest - pairs[0][0]
        window = [mbps for (t, mbps) in rates if t > newest - BW_VERDICT_WINDOW]
        if span >= BW_RAMP_GRACE + BW_VERDICT_WINDOW and len(window) >= BW_MIN_SAMPLES:
            return "verdict", statistics.median(window)
        if time.monotonic() - last_change > BW_LOG_FROZEN:
            print(f"bail: {BW_LOG_PATH} frozen > {BW_LOG_FROZEN:.0f}s — reporter dead")
            return "dead", 0.0
        last_mbps = rates[-1][1] if rates else 0.0
        print(f"{ts} UTC  {pairs[-1][1] / 1e6:8.1f} MB  {last_mbps:6.1f} Mbps  "
              f"(shot {shots}/{BW_MAX_SHOTS})")


def _bw_reallocate(
    org: str, project: str, name: str, key: str,
    inst: sc.ContainerGroupInstanceInfo, excluded: "set[str]", shot: int, why: str
) -> bool:
    """POST the instance /reallocate endpoint — the ONLY kill the arbitrator
    uses (the group is never stopped; the rejected node is excluded from the
    account pool for 48 h, so the next placement is a new host)."""
    print(f"{why} — POST reallocate (shot {shot}/{BW_MAX_SHOTS}) on instance "
          f"{inst.id[:8]} node {inst.machine_id or '?'}")
    try:
        sc.reallocate_container_group_instance(org, project, name, inst.id, api_key=key)
    except sc.SaladApiError as e:
        print(f"bail: reallocate failed: {e}")
        return False
    if inst.machine_id:
        excluded.add(inst.machine_id)
    print("202 Accepted — node excluded from the account pool for 48 h; "
          "awaiting new placement")
    return True


def _arbitrate(
    org: str, project: str, name: str, key: str, cap_mbps: float, interval: float
) -> int:
    """Bandwidth arbitrator: after a start, keep the HF download on a host
    whose median download rate clears cap_mbps, reallocating (max
    BW_MAX_SHOTS times) until it does. The 4th host is kept unconditionally.
    Exit codes: 0 accepted/complete/kept-4th; 1 group bail; 3 arbitrator
    bail. Ctrl-C leaves the group running as-is (main's handler)."""
    print(f"bandwidth arbitrator: {name} — cap {cap_mbps:g} Mbps, max {BW_MAX_SHOTS} "
          f"reallocations, 4th host kept")
    print(f"(reads {BW_LOG_PATH} over SSH every {BW_POLL_INTERVAL:g}s; median over "
          f"{BW_VERDICT_WINDOW:.0f}s; the group is NEVER stopped — only /reallocate)")
    shots = 0
    excluded: set[str] = set()
    while True:
        inst, code = _bw_acquire(org, project, name, key, excluded, interval)
        if code == 1:
            return 1
        if code == 3:
            return 3
        if code == "pull" and inst is not None:
            if shots >= BW_MAX_SHOTS:
                print(f"kept the {shots + 1}th host ({inst.machine_id or '?'}) — pull-stuck "
                      f"and shots exhausted; download continues on it")
                return 0
            if not _bw_reallocate(org, project, name, key, inst, excluded,
                                  shots + 1, "image pull stuck"):
                return 3
            shots += 1
            continue
        if inst is None:
            return 3
        verdict, mbps = _bw_measure(org, project, name, key, inst, cap_mbps, shots)
        if verdict == "done":
            print(f"download complete — {name} has a ready instance")
            return 0
        if verdict == "group":
            return 1
        if verdict == "churn":
            continue
        if verdict in ("ssh", "noimg", "dead"):
            return 3
        if verdict == "verdict" and mbps >= cap_mbps:
            print(f"host accepted at {mbps:.1f} Mbps (>= {cap_mbps:g}) — download "
                  f"continues; use `wait` for readiness")
            return 0
        # slow (verdict with median < cap)
        if shots >= BW_MAX_SHOTS:
            print(f"kept the {shots + 1}th host ({inst.machine_id or '?'}) at {mbps:.1f} Mbps "
                  f"— no faster host in {BW_MAX_SHOTS} shots; download continues on it")
            return 0
        if not _bw_reallocate(org, project, name, key, inst, excluded,
                              shots + 1, f"median {mbps:.1f} Mbps < {cap_mbps:g}"):
            return 3
        shots += 1


def main(argv: list[str]) -> int:
    interval = 5.0
    min_bw_mbps: float | None = None
    positional: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--interval":
            if i + 1 >= len(argv):
                print("error: --interval needs a value", file=sys.stderr)
                return 2
            try:
                interval = float(argv[i + 1])
            except ValueError:
                print(f"error: --interval: not a number: {argv[i + 1]!r}", file=sys.stderr)
                return 2
            if interval <= 0:
                print("error: --interval must be > 0", file=sys.stderr)
                return 2
            i += 2
            continue
        if arg == "--min-bw-mbps":
            if i + 1 >= len(argv):
                print("error: --min-bw-mbps needs a value", file=sys.stderr)
                return 2
            try:
                min_bw_mbps = float(argv[i + 1])
            except ValueError:
                print(f"error: --min-bw-mbps: not a number: {argv[i + 1]!r}", file=sys.stderr)
                return 2
            if min_bw_mbps <= 0:
                print("error: --min-bw-mbps must be > 0", file=sys.stderr)
                return 2
            i += 2
            continue
        if arg in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        positional.append(arg)
        i += 1

    if len(positional) > 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    action, org = "list", None
    for p in positional:
        if p in ACTIONS:
            if action != "list":
                print(f"error: only one action (got {action!r} and {p!r})", file=sys.stderr)
                return 2
            action = p
        elif p in KNOWN_PROJECTS:
            if org is not None:
                print(f"error: only one org (got {org!r} and {p!r})", file=sys.stderr)
                return 2
            org = p
        else:
            print(
                f"error: unknown action or org: {p!r}\n"
                f"actions: {', '.join(ACTIONS)}\n"
                "known orgs: " + ", ".join(KNOWN_PROJECTS) + "\n"
                "(add the org's projects to KNOWN_PROJECTS in manage_groups.py)",
                file=sys.stderr,
            )
            return 2

    if min_bw_mbps is not None and action != "start":
        print("error: --min-bw-mbps applies to `start` only (the arbitrator "
              "watches the HF download after a start)", file=sys.stderr)
        return 2

    orgs = [org] if org is not None else list(KNOWN_PROJECTS)
    try:
        key = sc.load_api_key()
    except sc.SaladApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        if action == "list":
            _print_table(_rows(orgs, key), key)
            print(f"actions: {', '.join(ACTIONS)} — see --help")
            return 0
        if action == "refresh":
            return _refresh(orgs, key, interval)
        # start / stop / wait / delete: numbered table -> prompt
        rows = _rows(orgs, key)
        selectable = _print_action_table(rows, key, action)
        if not selectable:
            print("no groups to select from — nothing to do.")
            return 1
        sel = _prompt(selectable, action)
        if sel is None:
            print("aborted — nothing done.")
            return 1
        s_org, s_project, g = sel
        print(f"\nselected: {s_org}/{s_project} — {g.name} ({g.current_status or '?'})  {_url(g) or '-'}")
        if action == "wait":
            return _wait(f"{s_org}/{s_project}", g.name, key, interval)
        if action == "stats":
            return _stats(f"{s_org}/{s_project}", g.name, key, interval)
        # start / stop / delete need confirmation before the call
        if not _confirm(action, sel):
            print("aborted — nothing done.")
            return 1
        rc = _run_action(action, sel, key)
        if action == "start" and rc == 0 and min_bw_mbps is not None:
            # The arbitrator: keep the HF download on a host whose median
            # download rate clears the cap (max 3 reallocations, 4th kept).
            return _arbitrate(s_org, s_project, g.name, key, min_bw_mbps, interval)
        return rc
    except KeyboardInterrupt:
        print("\nstopped — nothing done.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
