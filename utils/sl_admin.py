#!/usr/bin/env python3
"""Manage the Salad fleet — list, live-refresh, start, stop, wait, stats, delete,
create-org, create-project, deploy.

Usage:
  python3 utils/sl_admin.py [ACTION] [ORG] [--interval N]

  ACTION    list (default)  one-shot status table
            refresh         re-render the table every N seconds (Ctrl-C to stop)
            start           pick a group (by # or name) and start it — INCURS COST
            stop            pick a group and stop it
            wait            poll a group until at least one instance reports
                            ready, then exit (group status alone is not enough:
                            Salad marks a group `running` while instances are
                            still allocating/downloading/creating)
            describe        DUMP ONE GROUP'S WHOLE CONFIG, read-only: identity
                            (id/version/pending_change/timestamps), state,
                            lifecycle (replicas, restart/autostart policy,
                            priority, scaling), networking (dns/port/auth),
                            IMAGE (repo, tag, manifest digest, Hub link, the
                            `docker image inspect` line for the local image id),
                            container (hash, image_caching, size, cpu/memory/shm/
                            storage, GPU classes by NAME, volume, the CMD),
                            probes, the FULL env (secret-looking values redacted),
                            live instances (instance id, state, ready, machine_id,
                            pull progress, ssh line) and the derived client lines.
                            `--raw` also dumps the whole GET as JSON (env values
                            redacted). Pick from the numbered table, or name it:
                              describe ORG --project P --group G [--raw]
            stats           print llama-server stats (like utils/llama_stats.py:
                            tps / queue / totals, --interval loop, Ctrl-C stops)
                            — but ONLY when the group is fully ready: status
                            running, all `replicas` instances present, and each
                            one ready=true (stricter than wait's "at least one
                            ready"). Otherwise it prints the state and exits 1
                            without scraping
            delete          pick a group and delete it — IRREVERSIBLE
            create-org      register (and try to create) a new ORGANIZATION
            create-project  register (and try to create) a new PROJECT in an org
            deploy          pick a DOCKER TYPE (the profiles our images support)
                            and run its deployer against an org/project/group —
                            ZERO COST by default (--no-start; --start bills)
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

  deploy flags
  --type T
            the DOCKER TYPE key (see the `deploy` action's table). Prompted
            with a numbered menu when absent.
  --org O / --project P / --group G
            answer the corresponding prompt on the command line; anything
            unspecified is prompted (org/project from the fleet map, group
            name with the type's default in brackets).
  --start
            PAID opt-in: run the deployer without `--no-start`, after typing
            'y' at the confirmation. Default is config-only (CREATE/PATCH are
            free; only START bills). The 9B deployers create the group STOPPED
            and never start, so nothing bills on those types either way.
  -- <flags>
            everything after `--` is passed to the deployer verbatim
            (--ctx-size, --gpu, --model-repo, --idle-shutdown, …).
  create flags
  --project P        (create-org) project to register with the org; repeatable
  --register-only    skip the API/portal attempts, only write fleet.json
  --no-portal        skip the portal (web-UI backend) attempt

  python3 utils/sl_admin.py                     # status, all known orgs
  python3 utils/sl_admin.py akl-on-salad        # one org
  python3 utils/sl_admin.py refresh             # live table, Ctrl-C to stop
  python3 utils/sl_admin.py delete              # numbered table -> prompt -> confirm
  python3 utils/sl_admin.py start akl-on-salad  # start a group in that org
  python3 utils/sl_admin.py start --min-bw-mbps 30   # start + arbitrator
  python3 utils/sl_admin.py wait                # after start: poll until ready
  python3 utils/sl_admin.py stats               # stats, only if fully ready
  python3 utils/sl_admin.py deploy              # prompt: docker type -> org/project/group
  python3 utils/sl_admin.py deploy --type ampere --project llm --group my-atx
  python3 utils/sl_admin.py deploy --type generic --group my-gguf -- \
      --model-repo Some/Org --model-file Model-Q5_K_M.gguf --ctx-size 32768
  python3 utils/sl_admin.py create-org my-new-org --project default
  python3 utils/sl_admin.py create-project my-new-org llm

ORGANIZATION / PROJECT CREATION — the honest state (live-probed 2026-10-13,
tools/probe_org_create.py): the PUBLIC API (api.salad.com/api/public) has NO
create-org, NO create-project, NO list-orgs and NO list-projects — every one of
those paths answers 404 (the OpenAPI spec's 24 paths are all
`/organizations/{org}/...`, none on the collection itself). Orgs and projects
are created in the WEB UI (portal.salad.com), whose backend
(portal-api.salad.com/api/portal) is what utils/billing.py logs into. So
`create-org` / `create-project` do two things: (1) ATTEMPT the portal endpoint
with the billing vault's credentials (best-effort, `--no-portal` to skip — the
portal API is undocumented and unverified, so a 404 there is expected, not a
bug), and
(2) REGISTER the org/project in the local fleet map (utils/fleet.json) so the
rest of this tool sees it. Registration is the part that always works; use
`--register-only` after you create in the web UI.

The SaladCloud API has no list-orgs / list-projects operations, so this walks
a fixed map of the account's orgs and their projects: KNOWN_PROJECTS below,
overlaid by utils/fleet.json (tracked; what create-org/create-project write).

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

import getpass
import json
import os
import re
import runpy
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

# salad_client.py lives at the repo root; running this as utils/sl_admin.py
# puts utils/ on sys.path, not the root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import salad_client as sc

# org -> projects known to exist in this account (web-UI created; no list API).
# fleet.json (next to this file, TRACKED) overlays this map — create-org /
# create-project write there, so the built-in map stays the zero-config default
# for a fresh clone and the account's real shape lives in data, not in code.
KNOWN_PROJECTS: dict[str, tuple[str, ...]] = {
    "ma-casa-in-paris": ("qwen38-27b", "llm"),
    "akl-on-salad": ("default", "comfy"),
}
FLEET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet.json")

# Salad's own name rules (OpenAPI OrganizationName / ProjectName): 2-63 chars,
# lowercase, start [a-z], end [a-z0-9], inner [a-z0-9-].
SALAD_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")

# The portal backend the WEB UI uses (utils/billing.py logs into this).
PORTAL_BASE = "https://portal-api.salad.com/api/portal"

ACTIONS = ("list", "refresh", "start", "stop", "wait", "stats", "describe", "delete",
           "create-org", "create-project", "deploy")

# --- deploy: the docker types we support (one entry per deployer) ------------
# Each is (script, one-line profile, default group, accepts_--no-start). The
# deployers own the profiles; this table only routes the prompt. Flags after
# `--` in `deploy … -- <flags>` go to the deployer verbatim, so anything the
# deployer accepts (--ctx-size, --gpu, --model-repo, --idle-shutdown, …) is
# reachable. The 9B deployers create the group STOPPED (autostart off) and
# have no --no-start flag — they never bill, so nothing to pass and nothing
# to confirm.
DOCKER_TYPES: dict[str, tuple[str, str, str, bool]] = {
    "q6":      ("deploy_qwen38_27b.py",
                "27B q6 MTP+vision (the Claude Code backend) — Q5_K_M @ CTX 90000, rtx3090",
                "qwen38-27b-q6k", True),
    "generic": ("deploy_generic.py",
                "vanilla lmss_generic — ANY GGUF (--model-repo/--model-file), any GPU class",
                "atx-swift-27b-q5", True),
    "ampere":  ("deploy_ampere.py",
                "llamAmpere fork (turbo5/turbo4 KV) — ATX profile CTX 132768, sm_86 30-series ONLY",
                "atx-swift-27b-q5-opt", True),
    "9b":      ("deploy_qwen38_9b.py",
                "plain 9B on base lmss — CREATE-ONLY (refuses an existing group)",
                "qwen38-9b", False),
    "qwen9b":  ("deploy_qwen9b.py",
                "plain 9B on base lmss — CREATE-ONLY (refuses an existing group)",
                "qwen9b", False),
}
DEPLOY_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "deploy")
)

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
# endpoint when it is below the cap. The group is never stopped. While /.ssh
# exists (the user touched it after logging in over SSH) every shot is held
# off — the arbitrator never reallocates the instance out from under a session.
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
BW_SSH_GUARD_FILE = "/.ssh"  # SSH-presence guard the user touches when logged in
BW_SSH_OPTS = (
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=10",
    "-o", "BatchMode=yes",
    "-o", "LogLevel=ERROR",
)


def _fleet() -> dict[str, tuple[str, ...]]:
    """The account's org -> projects map: KNOWN_PROJECTS overlaid by fleet.json.

    fleet.json is tracked data: {"organizations": {"slug": ["project", …]}}.
    A malformed/unreadable file is ignored with a warning — the built-in map
    must keep working (this file is the tool's spine; a typo in data must not
    make `list` unusable).
    """
    fleet = {org: tuple(projects) for org, projects in KNOWN_PROJECTS.items()}
    try:
        with open(FLEET_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return fleet
    except (OSError, ValueError) as e:
        print(f"warning: ignoring {FLEET_FILE}: {e}", file=sys.stderr)
        return fleet
    orgs = data.get("organizations") if isinstance(data, dict) else None
    if isinstance(orgs, dict):
        for org, projects in orgs.items():
            if isinstance(projects, (list, tuple)):
                fleet[org] = tuple(dict.fromkeys((*fleet.get(org, ()), *projects)))
    return fleet


def _save_fleet(org: str, projects: tuple[str, ...]) -> None:
    """Merge one org + its projects into fleet.json (idempotent, creates it)."""
    data: dict = {}
    try:
        with open(FLEET_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as e:
        print(f"error: {FLEET_FILE} is unreadable, refusing to write it: {e}", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(data, dict):
        print(f"error: {FLEET_FILE} is not a JSON object, refusing to write it", file=sys.stderr)
        raise SystemExit(2)
    orgs = data.setdefault("organizations", {})
    if not isinstance(orgs, dict):
        print(f"error: {FLEET_FILE} 'organizations' is not an object, refusing to write it",
              file=sys.stderr)
        raise SystemExit(2)
    orgs[org] = sorted(dict.fromkeys((*orgs.get(org, []), *projects)))
    with open(FLEET_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"registered in {os.path.relpath(FLEET_FILE, os.getcwd())}: "
          f"{org} -> {', '.join(orgs[org])}")


def _pick_numbered(items: list[str], title: str, default: int = 0) -> int | None:
    """Numbered menu on stdin. Returns the 0-based pick, `default` on an empty
    line, None on EOF/Ctrl-C (the caller treats that as abort)."""
    print(f"{title} ({len(items)} options):")
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item}")
    hint = f" [Enter = {default + 1}]" if default else ""
    while True:
        try:
            raw = input(f"  pick 1-{len(items)}{hint}: ").strip()
        except EOFError:
            return None
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return int(raw) - 1
        print(f"  not a number in 1-{len(items)} — try again (Ctrl-C aborts)")


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
            print(f"wait for it to become ready:  python3 utils/sl_admin.py wait {org}")
            print(
                f"then run Claude Code:        sl_claude_deploy --org {org} --project {project} --group {name}"
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
                    print(f"note: verification GET returned HTTP {e.status_code} — re-check with: python3 utils/sl_admin.py")
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
        print(f"it is {status} — start it first:  python3 utils/sl_admin.py start {org}")
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
        print("; ".join(detail) + f" — wait for it, then re-run stats:  python3 utils/sl_admin.py wait {org}")
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


# --- describe: one group's whole config in a single read-only dump -----------
# Secret-looking env VALUES are never printed (names are, so you can see what is
# set). HF_TOKEN / SALAD_STOP_KEY are the ones that matter; the pattern is
# deliberately broad so a future secret lands redacted by default.
_SECRET_RE = re.compile(r"TOKEN|SECRET|PASSWORD|PASSPHRASE|STOP_KEY|^HF_", re.I)


def _redact_env(env: "dict | None") -> dict:
    return {k: ("<REDACTED>" if _SECRET_RE.search(str(k)) else v) for k, v in (env or {}).items()}


def _group_json(org: str, project: str, name: str, key: str) -> dict:
    """Full group GET, retried: a transitional response can carry an EMPTY container."""
    g: dict = {}
    for _ in range(4):
        g = sc.get_container_group(org, project, name, api_key=key).raw
        if (g.get("container") or {}).get("image"):
            return g
        time.sleep(0.6)
    return g


def _sec(label: str, lines: list[str]) -> None:
    """One section: label on the first line, continuations aligned under it."""
    lines = [line for line in lines if line]
    for i, line in enumerate(lines):
        print(f"{label + ' ' if i == 0 else ' ' * (len(label) + 1)}{line}")


def _gpu_names(org: str, ids: list, key: str) -> str:
    """GPU class UUIDs -> 'rtx3090 [a5db5c50-…]' (names come from the org)."""
    if not ids:
        return "(none)"
    try:
        names = {gc.id: gc.name for gc in sc.list_gpu_classes(org, api_key=key)}
    except sc.SaladApiError as e:
        return f"{ids} (gpu-classes GET failed: HTTP {e.status_code})"
    return ", ".join(f"{names.get(i, '?')} [{i}]" for i in ids)


def _describe(org: str, project: str, name: str, key: str, raw: bool = False) -> int:
    g = _group_json(org, project, name, key)
    if not g:
        print(f"error: {org}/{project}/{name} — 4 GETs returned no container")
        return 1
    c = g.get("container") or {}
    env = dict(c.get("environment_variables") or {})
    img = str(c.get("image") or "")
    ref, _, digest = img.partition("@")
    repo, _, tag = ref.partition(":")
    st = g.get("current_state") or {}
    net = g.get("networking") or {}
    res = c.get("resources") or {}

    print(f"=== {org}/{project}/{name} ===")
    _sec("identity", [
        f"name={g.get('name')}  display_name={g.get('display_name')}  id={g.get('id')}",
        f"version={g.get('version')}  pending_change={g.get('pending_change')}  "
        f"create_time={g.get('create_time')}  update_time={g.get('update_time')}",
    ])
    _sec("state", [
        f"status={st.get('status')}  description={st.get('description')}",
        f"start_time={st.get('start_time')}  finish_time={st.get('finish_time')}  "
        f"instance_status_counts={st.get('instance_status_counts')}",
    ])
    _sec("lifecycle", [
        f"replicas={g.get('replicas')}  restart_policy={g.get('restart_policy')}  "
        f"autostart_policy={g.get('autostart_policy')}  priority={g.get('priority')}",
        f"scheduled-scaling-enabled={g.get('scheduled-scaling-enabled')}  "
        f"scaling-actions={g.get('scaling-actions')}  country_codes={g.get('country_codes')}",
    ])
    _sec("networking", [
        f"dns={net.get('dns')}  port={net.get('port')}  protocol={net.get('protocol')}  "
        f"auth={net.get('auth')}  load_balancer={net.get('load_balancer')}",
        f"single_connection_limit={net.get('single_connection_limit')}  "
        f"client_request_timeout={net.get('client_request_timeout')}  "
        f"server_response_timeout={net.get('server_response_timeout')}",
    ])
    _sec("image", [
        f"ref={img}",
        f"repo={repo}  tag={tag or '(none — digest-pinned)'}  manifest_digest={digest}",
        "(no digest = TAG REF: the worker image cache is repo-keyed and a tag never "
        "re-pulls — pin a digest to force a fresh pull)" if not digest else "",
        f"hub=https://hub.docker.com/r/{repo}  tag={tag or '-'}  "
        f"local image id: docker image inspect --format '{{{{.Id}}}}' '{ref}'",
    ])
    lines = [
        f"hash={c.get('hash')}  image_caching={c.get('image_caching')}  size={c.get('size')}",
        f"cpu={res.get('cpu')}  memory={res.get('memory')}MiB  shm_size={res.get('shm_size')}MiB  "
        f"storage={int(res.get('storage_amount') or 0) // (1024 ** 3)}GiB",
        f"gpu_classes={_gpu_names(org, list(res.get('gpu_classes') or []), key)}",
    ]
    if c.get("volume"):
        lines.append(f"volume={json.dumps(c['volume'])}")
    cmd = str(c.get("command") or "")
    lines.append(f"command={cmd if cmd else '(image default CMD — none set on the group)'}")
    _sec("container", lines)
    for probe in ("readiness_probe", "liveness_probe", "startup_probe"):
        p = g.get(probe)
        if not p:
            continue
        h = p.get("http") or {}
        _sec(probe, [
            f"http path={h.get('path')} port={h.get('port')} scheme={h.get('scheme')}  "
            f"initial_delay={p.get('initial_delay_seconds')}s period={p.get('period_seconds')}s "
            f"timeout={p.get('timeout_seconds')}s success={p.get('success_threshold')} "
            f"failure={p.get('failure_threshold')}",
        ])
    _sec(f"env ({len(env)} keys)",
         [f"{k}={v}" for k, v in sorted(_redact_env(env).items())] or ["(EMPTY — no env on the group)"])

    try:
        insts = list(sc.list_container_group_instances(org, project, name, api_key=key))
        note = ""
    except sc.SaladApiError as e:
        insts, note = [], f" (GET instances -> HTTP {e.status_code})"
    rows = []
    for i in insts:
        r = i.raw
        rows.append(f"id={i.id}  state={i.state}  ready={i.ready}  machine_id={i.machine_id}  "
                    f"pull={i.pulling_progress}")
        rows.append(f"ssh={i.ssh_line or '-'}  image={r.get('image') or '-'}  "
                    f"create_time={r.get('create_time')}  update_time={r.get('update_time')}")
    _sec(f"instances ({len(insts)}){note}", rows or ["(none — the group has no live instances)"])

    _sec("derived", [
        f"url={_url(sc.ContainerGroupInfo.from_json(g)) or '-'}",
        f"claude: sl_claude_deploy --org {org} --project {project} --group {name}",
        f"env edit (merge-safe): python3 -I tools/sl_set_env.py {org}/{project}/{name} KEY=VALUE",
    ])

    if raw:
        dump = json.loads(json.dumps(g))
        cc = dump.get("container")
        if isinstance(cc, dict) and "environment_variables" in cc:
            cc["environment_variables"] = _redact_env(cc["environment_variables"])
        print("--- raw GET (env VALUES redacted) ---")
        print(json.dumps(dump, indent=2, sort_keys=True))
    return 0


def _bw_read(inst: sc.ContainerGroupInstanceInfo) -> "tuple[list[tuple[int, int, int]] | None, str | None)":
    """Read the tail of the instance's bw.log over SSH. Salad's SSH exec is
    shell-less (one plain command per call — no pipes/;/quotes), so the probe
    is exactly `tail -n N <path>`. Returns (samples, None) on success
    (samples non-empty — the relay gives no exit-code signal), or (None,
    kind) where kind is "missing" (file absent — the image predates
    bw_reporter) or a connection description (strike).

    Each sample is (epoch, rx_bytes, model_bytes). Current images log the
    kernel RX counter first — the bandwidth source, since xet (baked
    HF_XET_HIGH_PERFORMANCE=1) transfers most shard bytes through its CAS
    cache OUTSIDE MODEL_DIR, so file growth alone reads 0.0 mid-transfer —
    and MODEL_DIR size second (progress display only). Old images logged
    `epoch model-bytes` (2 columns); those parse with rx := model, i.e. the
    pre-fix behavior, so a mixed fleet still measures."""
    cmd = [
        "ssh", *BW_SSH_OPTS, "-p", str(inst.ssh_port), f"root@{inst.ssh_ip}",
        "tail", "-n", str(BW_TAIL_LINES), BW_LOG_PATH,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=BW_SSH_TIMEOUT)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return None, f"conn ({e.__class__.__name__})"
    samples: list[tuple[int, int, int]] = []
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(x.isdigit() for x in parts):
            samples.append((int(parts[0]), int(parts[1]), int(parts[2])))
        elif len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            # legacy image: only the model counter existed — rate from it
            samples.append((int(parts[0]), int(parts[1]), int(parts[1])))
    # Salad's SSH is an OCI exec behind a relay: it forwards stdout/stderr but
    # ALWAYS exits 1 — success and failure are indistinguishable by exit code
    # (live-probed 2026-10-09: `ls /tmp/llama-api` printed the listing and
    # exited 1). Only ssh's own transport failures exit 255. So the payload
    # decides, not the code: samples ⇒ success; "cannot open" (tail's own
    # stderr) ⇒ the log is absent; anything else ⇒ connection strike.
    # The relay's "Connecting to container <id>" banner lands on stdout and is
    # skipped by the digit-pair filter.
    if p.returncode == 255:
        return None, f"conn (ssh: {p.stderr.strip()[:120]})"
    # The relay merges the remote process's stderr into the client's stdout
    # (live-probed 2026-10-11), so tail's "cannot open" error lands on stdout
    # — check both streams. (255 errors above are ssh's own and stay local.)
    combined = p.stdout + p.stderr
    if samples:
        return samples, None
    if "cannot open" in combined:
        return None, "missing"
    return None, f"conn (no samples, exit {p.returncode}: {combined.strip()[:120]})"


def _bw_ssh_guard(inst: sc.ContainerGroupInstanceInfo) -> "tuple[bool, str]":
    """SSH-presence gate for reallocate shots — the simple contract (user's
    design, 2026-10-09): the user touches /.ssh after logging into the
    container over SSH (the same file the idle watchdog honors), and removes
    it on the way out. While it exists, a /reallocate would yank the instance
    — and their session — out from under them, so NO shot is spent. Probe =
    `ls /.ssh` (one plain command; the relay exits 1 on success — the PAYLOAD
    decides, see _bw_read): an exact stdout line naming the file => guard
    present; ls's "cannot access" => absent; anything else => inconclusive.
    Absent and inconclusive both return False (spend the shot) — holding on
    every probe failure would silently disable the arbitrator.

    REGRESSION (2026-10-11, live hold-loop on instance ccf9203e): the relay
    merges the remote's stderr into the client's stdout, so when the file is
    ABSENT, ls's error — `ls: cannot access '/.ssh': No such file or
    directory` — arrives on STDOUT and contains the guard path itself. The
    old `"/.ssh" in p.stdout` substring check matched its own error text and
    reported "present" forever: holding, zero shots, group stuck billing.
    Hence: error markers are checked FIRST, on the combined streams, and
    presence requires an exact line (ls of an existing file prints the path
    alone, one line)."""
    cmd = [
        "ssh", *BW_SSH_OPTS, "-p", str(inst.ssh_port), f"root@{inst.ssh_ip}",
        "ls", BW_SSH_GUARD_FILE,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=BW_SSH_TIMEOUT)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, f"probe failed ({e.__class__.__name__})"
    if p.returncode == 255:
        return False, f"ssh transport failed ({p.stderr.strip()[:120]})"
    combined = p.stdout + p.stderr  # the relay merges remote stderr into stdout
    if "cannot access" in combined or "No such file" in combined:
        return False, f"no {BW_SSH_GUARD_FILE}"
    if any(line.strip() == BW_SSH_GUARD_FILE for line in p.stdout.splitlines()):
        return True, f"{BW_SSH_GUARD_FILE} present"
    return False, f"probe inconclusive (exit {p.returncode}: {combined.strip()[:120]})"


def _bw_rates(samples: "list[tuple[int, int, int]]") -> "list[tuple[int, float]]":
    """Per-interval download rates as (end_epoch, Mbps), computed from the
    MIDDLE (rx-bytes) column of each (epoch, rx, model) sample — the kernel
    NIC counter, which sees xet's CAS-cache traffic that never lands in
    MODEL_DIR. The model column is display-only. The kernel counter is
    monotonic, but negative deltas still floor at 0 defensively (a container
    restart mid-window resets it) — the median absorbs the dip."""
    rates: list[tuple[int, float]] = []
    for (t0, r0, _m0), (t1, r1, _m1) in zip(samples, samples[1:]):
        dt = t1 - t0
        if dt <= 0:
            continue
        rates.append((t1, max(0, r1 - r0) * 8.0 / dt / 1e6))
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
                    # Salad reports pulling_progress as a 0..1 FRACTION, not a
                    # percent (live-verified 2026-10-11: 0.70000833 read as
                    # "0.7%" was actually 70%) — scale before printing.
                    prog_s = f"{prog * 100:.1f}%" if prog is not None else "?"
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
        samples, err = _bw_read(inst)
        if err == "missing" or (err is None and not samples):
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
        newest = samples[-1][0]
        if last_ts is not None and newest > last_ts:
            last_change = time.monotonic()
        last_ts = newest
        rates = _bw_rates(samples)
        span = newest - samples[0][0]
        window = [mbps for (t, mbps) in rates if t > newest - BW_VERDICT_WINDOW]
        if span >= BW_RAMP_GRACE + BW_VERDICT_WINDOW and len(window) >= BW_MIN_SAMPLES:
            return "verdict", statistics.median(window)
        if time.monotonic() - last_change > BW_LOG_FROZEN:
            print(f"bail: {BW_LOG_PATH} frozen > {BW_LOG_FROZEN:.0f}s — reporter dead")
            return "dead", 0.0
        last_mbps = rates[-1][1] if rates else 0.0
        print(f"{ts} UTC  {samples[-1][2] / 1e6:8.1f} MB  {last_mbps:6.1f} Mbps  "
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
    BW_MAX_SHOTS times) until it does. The 4th host is kept unconditionally.
    SSH-presence gate: before spending a shot, _bw_ssh_guard probes /.ssh —
    the user touches it after logging in over SSH, and no /reallocate is
    spent while it exists (their session rides on that instance). Exit codes:
    0 accepted/complete/kept-4th; 1 group bail; 3 arbitrator bail. Ctrl-C
    leaves the group running as-is (main's handler)."""
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
            active, why = _bw_ssh_guard(inst)
            if active:
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"{ts} UTC  ssh session active ({why}) — holding, no shot spent")
                continue
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
        active, why = _bw_ssh_guard(inst)
        if active:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"{ts} UTC  ssh session active ({why}) — holding, no shot spent")
            continue
        if not _bw_reallocate(org, project, name, key, inst, excluded,
                              shots + 1, f"median {mbps:.1f} Mbps < {cap_mbps:g}"):
            return 3
        shots += 1


def _portal_session():
    """Portal (web-UI backend) session from the billing vault.

    Returns (billing_module, cookie), or None when the vault is missing or the
    passphrase is not available. Credentials come from utils/portal_vault.gpg
    (same vault utils/billing.py uses) and are NEVER printed.
    """
    vault = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portal_vault.gpg")
    if not os.path.exists(vault):
        print(f"portal: no vault at {os.path.relpath(vault, os.getcwd())} "
              f"(run `python3 utils/billing.py` once to create it)")
        return None
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import billing
    except ImportError as e:
        print(f"portal: cannot import utils/billing.py: {e}")
        return None
    phrase = os.environ.get("PORTAL_VAULT_PASS")
    try:
        data = billing.load_vault(vault, phrase or getpass.getpass("portal vault passphrase: "))
        cookie = billing.login(data["user"], data["password"])
    except SystemExit as e:  # billing raises SystemExit on decrypt/login failure
        print(f"portal: {e}")
        return None
    except (KeyError, OSError, ValueError) as e:
        print(f"portal: vault unusable: {e}")
        return None
    return billing, cookie


def _portal_call(billing, cookie: str, path: str, body: dict | None) -> tuple[int, str]:
    """One portal call. Returns (status, short body excerpt) — excerpts are
    truncated so a portal payload can never dump account detail into the log."""
    url = f"{PORTAL_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    status, _headers, raw = billing._request(url, data=data, cookie=cookie)
    return status, raw.decode("utf-8", "replace")[:300]


def _create_names(slug: str, projects: tuple[str, ...]) -> bool:
    """Validate against Salad's own pattern (2-63, lowercase, [a-z] start)."""
    bad = [n for n in (slug, *projects) if not SALAD_NAME_RE.match(n)]
    for name in bad:
        print(f"error: {name!r} violates Salad's name rule "
              f"^[a-z][a-z0-9-]{{0,61}}[a-z0-9]$ (2-63 chars, lowercase, "
              f"starts [a-z], ends [a-z0-9])", file=sys.stderr)
    return not bad


def _create_org(slug: str, projects: tuple[str, ...], key: str,
                register_only: bool, use_portal: bool) -> int:
    """Create/register an ORGANIZATION.

    The public API cannot create orgs (live-probed 404 on POST /organizations,
    2026-10-13, tools/probe_org_create.py) — the portal backend is the web UI's
    own path and is undocumented, so this attempts it and reports the real
    status. Registration in fleet.json is what the tool needs, and it always
    works.
    """
    if not _create_names(slug, projects):
        return 2
    print(f"org: {slug}  projects: {', '.join(projects)}")

    created = False
    if not register_only:
        # 1) The public API: documented-absent, attempted anyway so the tool
        #    reports today's truth instead of a cached assumption.
        try:
            status, reason, _h, raw = sc._http(
                "POST", "/organizations", key, body={"name": slug}, timeout=30.0
            )
            print(f"public API POST /organizations -> {status} {reason} — org created")
            created = status in (201, 202)
        except sc.SaladApiError as e:
            print(f"public API POST /organizations -> {e.status_code} "
                  f"{'(no such endpoint — expected, orgs are not API-creatable)' if e.status_code == 404 else e.raw_body[:200]}")

        # 2) The portal backend (what the web UI calls). Undocumented: a 404
        #    here is an expected answer, not a failure of this tool.
        if use_portal:
            session = _portal_session()
            if session is None:
                print("portal: skipped (no session) — create the org in the web UI, "
                      "then re-run with --register-only")
            else:
                billing, cookie = session
                status, excerpt = _portal_call(billing, cookie, "/organizations", {"name": slug})
                if status in (200, 201, 202):
                    print(f"portal POST /organizations -> {status} — org created")
                    created = True
                elif status == 404:
                    print("portal POST /organizations -> 404 (no such endpoint; the web UI "
                          "uses a path we have not identified) — create it in the web UI")
                else:
                    print(f"portal POST /organizations -> {status} {excerpt}")

        if not created:
            print("next: create the organization at https://portal.salad.com (web UI), "
                  "then re-run with --register-only to add it to the fleet map")

    _save_fleet(slug, projects)
    print(f"{'created' if created else 'registered'}: {slug} — "
          f"`python3 utils/sl_admin.py list {slug}` should now walk it")
    return 0


def _create_project(org: str, project: str, key: str,
                    register_only: bool, use_portal: bool) -> int:
    """Create/register a PROJECT in an org. Same API reality as _create_org
    (POST /organizations/{org}/projects is 404 on the public API — the
    salad_client.create_project docstring's 2026-08-31 probe is dead)."""
    if not _create_names(org, (project,)):
        return 2
    print(f"org: {org}  project: {project}")

    created = False
    if not register_only:
        try:
            res = sc.create_project(org, project, api_key=key)
            print(f"public API POST /organizations/{org}/projects -> {res.status_code} "
                  f"{res.reason_phrase} — project created")
            created = True
        except sc.SaladApiError as e:
            print(f"public API POST /organizations/{org}/projects -> {e.status_code} "
                  f"{'(no such endpoint — expected, projects are not API-creatable)' if e.status_code == 404 else e.raw_body[:200]}")
        if use_portal:
            session = _portal_session()
            if session is None:
                print("portal: skipped (no session) — create the project in the web UI, "
                      "then re-run with --register-only")
            else:
                billing, cookie = session
                status, excerpt = _portal_call(
                    billing, cookie, f"/organizations/{org}/projects", {"name": project}
                )
                if status in (200, 201, 202):
                    print(f"portal POST projects -> {status} — project created")
                    created = True
                elif status == 404:
                    print("portal POST projects -> 404 (no such endpoint) — create it in the web UI")
                else:
                    print(f"portal POST projects -> {status} {excerpt}")
        if not created:
            print(f"next: create the project inside {org} at https://portal.salad.com, "
                  "then re-run with --register-only")

    _save_fleet(org, (project,))
    print(f"{'created' if created else 'registered'}: {org}/{project}")
    return 0


def _deploy(key: str, type_key: str | None, org: str | None, project: str | None,
            group: str | None, start: bool, extra: list[str]) -> int:
    """Deploy a group by picking a DOCKER TYPE (one of the profiles our images
    support) and routing to its deployer.

    Zero cost by default: the deployer runs with --no-start (CREATE/PATCH are
    free, only START bills). `--start` is the paid opt-in and is confirmed.
    """
    types = list(DOCKER_TYPES)
    if type_key is None:
        pick = _pick_numbered(
            [f"{k} — {DOCKER_TYPES[k][1]}" for k in types], "docker type")
        if pick is None:
            print("aborted — nothing done.")
            return 1
        type_key = types[pick]
    elif type_key not in DOCKER_TYPES:
        print(f"error: unknown docker type {type_key!r}; supported: {', '.join(types)}",
              file=sys.stderr)
        return 2

    script, profile, default_group, accepts_no_start = DOCKER_TYPES[type_key]
    path = os.path.join(DEPLOY_DIR, script)
    if not os.path.exists(path):
        print(f"error: deployer not found at {path}", file=sys.stderr)
        return 2
    print(f"type: {type_key} — {profile}")

    fleet = _fleet()
    if org is None:
        pick = _pick_numbered(list(fleet), "organization")
        if pick is None:
            print("aborted — nothing done.")
            return 1
        org = list(fleet)[pick]
    projects = list(fleet.get(org, ()))
    if not projects:
        print(f"error: {org} has no known projects — add one with create-project "
              f"(or --register-only after creating it in the web UI)", file=sys.stderr)
        return 2
    if project is None:
        pick = _pick_numbered(projects, "project")
        if pick is None:
            print("aborted — nothing done.")
            return 1
        project = projects[pick]
    if group is None:
        try:
            group = input(f"  group name [{default_group}]: ").strip() or default_group
        except EOFError:
            print("aborted — nothing done.")
            return 1
    if not SALAD_NAME_RE.match(group):
        print(f"error: group name {group!r} violates Salad's name rule "
              f"^[a-z][a-z0-9-]{{0,61}}[a-z0-9]$", file=sys.stderr)
        return 2

    cmd = [sys.executable, "-I", path, "--org", org, "--project", project, "--group", group]
    if not start and accepts_no_start:
        cmd.append("--no-start")
    cmd.extend(extra)

    # Does THIS run bill? Deployers that accept --no-start bill only when we
    # omit it. The 9B deployers create the group STOPPED (autostart off,
    # scheduled scaling) and never start anything, so `--start` on those types
    # bills nothing here — the paid start is a later `start` action.
    will_bill = start and accepts_no_start

    print(f"\ncommand: {' '.join(cmd)}")
    print(f"target:  {org}/{project}/{group}  "
          f"{'START (bills per second)' if will_bill else 'config only, NO start (free)'}")
    if not accepts_no_start:
        print("note:  this deployer creates the group STOPPED (autostart off) — "
              "it never starts, so nothing bills; start it later with the `start` action")
    if will_bill:
        try:
            if input("  type 'y' to START this group (it bills): ").strip().lower() != "y":
                print("aborted — nothing done.")
                return 1
        except EOFError:
            print("aborted — nothing done.")
            return 1

    rc = subprocess.call(cmd)
    if rc != 0:
        print(f"deployer exited {rc} — see its own output above")
        return rc

    # Read-only confirmation of what the group is now, including its gateway URL
    # (networking.dns — the access URL; there is no top-level `endpoint` field).
    try:
        g = sc.get_container_group(org, project, group, api_key=key)
        print(f"group:   {g.name}  status={g.current_status or '?'}  url={_url(g) or '-'}")
        if not will_bill:
            print("next: the paid start is your call — "
                  f"`python3 utils/sl_admin.py start {org} --min-bw-mbps 100`")
        print(f"claude:  python3 claude/cl_salad {_url(g) or '<gateway-url>'}")
    except sc.SaladApiError as e:
        print(f"deployer succeeded, but the status GET failed: HTTP {e.status_code}")
    return 0


def main(argv: list[str]) -> int:
    interval = 5.0
    min_bw_mbps: float | None = None
    type_key: str | None = None
    org_flag: str | None = None
    project_flag: str | None = None
    group_flag: str | None = None
    projects_flag: list[str] = []
    start = register_only = raw_dump = False
    use_portal = True
    positional: list[str] = []
    extra: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            # Everything after `--` goes to the deployer verbatim (deploy only).
            extra = argv[i + 1:]
            break
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
        if arg in ("--type", "--org", "--project", "--group"):
            if i + 1 >= len(argv):
                print(f"error: {arg} needs a value", file=sys.stderr)
                return 2
            val = argv[i + 1]
            if arg == "--type":
                type_key = val
            elif arg == "--org":
                org_flag = val
            elif arg == "--project":
                project_flag = val          # last one wins (deploy)
                projects_flag.append(val)   # all of them (create-org)
            else:
                group_flag = val
            i += 2
            continue
        if arg == "--start":
            start = True
            i += 1
            continue
        if arg == "--raw":
            raw_dump = True
            i += 1
            continue
        if arg == "--register-only":
            register_only = True
            i += 1
            continue
        if arg == "--no-portal":
            use_portal = False
            i += 1
            continue
        if arg in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        positional.append(arg)
        i += 1

    action: str | None = None
    for p in positional:
        if p in ACTIONS:
            if action is not None and action != p:
                print(f"error: only one action (got {action!r} and {p!r})", file=sys.stderr)
                return 2
            action = p
    action = action or "list"
    rest = [p for p in positional if p not in ACTIONS]

    # Flags that only mean something to one action — say so instead of ignoring
    # them silently.
    if type_key is not None and action != "deploy":
        print("error: --type applies to `deploy` only", file=sys.stderr)
        return 2
    if group_flag is not None and action not in ("deploy", "describe"):
        print("error: --group applies to `deploy` and `describe`", file=sys.stderr)
        return 2
    if raw_dump and action != "describe":
        print("error: --raw applies to `describe` only (the full GET as JSON)", file=sys.stderr)
        return 2
    if (register_only or not use_portal) and action not in ("create-org", "create-project"):
        print("error: --register-only / --no-portal apply to `create-org` and "
              "`create-project` only", file=sys.stderr)
        return 2
    if start and action != "deploy":
        print("error: --start applies to `deploy` only (the paid start for other "
              "groups is the `start` action)", file=sys.stderr)
        return 2
    if extra and action != "deploy":
        print("error: flags after `--` are passed to the deployer — `deploy` only",
              file=sys.stderr)
        return 2

    if min_bw_mbps is not None and action != "start":
        print("error: --min-bw-mbps applies to `start` only (the arbitrator "
              "watches the HF download after a start)", file=sys.stderr)
        return 2

    fleet = _fleet()
    org = None
    if action not in ("create-org", "create-project", "deploy"):
        for p in rest:
            if p in fleet:
                if org is not None:
                    print(f"error: only one org (got {org!r} and {p!r})", file=sys.stderr)
                    return 2
                org = p
            else:
                print(
                    f"error: not an action, and not a known org: {p!r}\n"
                    f"actions: {', '.join(ACTIONS)}\n"
                    "known orgs: " + ", ".join(fleet) + "\n"
                    f"(register a new org/project with `create-project <org> <project> "
                    f"--register-only` — it writes "
                    f"{os.path.relpath(FLEET_FILE, os.getcwd())})",
                    file=sys.stderr,
                )
                return 2

    key: str | None = None
    try:
        key = sc.load_api_key()
    except sc.SaladApiError as e:
        # Registering an org/project in the fleet map needs no key; everything
        # else talks to the API.
        if not (register_only and action in ("create-org", "create-project")):
            print(f"error: {e}", file=sys.stderr)
            return 1

    orgs = [org] if org is not None else list(fleet)

    try:
        if action == "create-org":
            slug = rest[0] if rest else None
            if slug is None:
                try:
                    slug = input("new organization slug: ").strip()
                except EOFError:
                    print("aborted — nothing done.")
                    return 1
            if not slug:
                print("error: an organization slug is required", file=sys.stderr)
                return 2
            return _create_org(slug, tuple(projects_flag) or ("default",), key,
                               register_only, use_portal)

        if action == "create-project":
            org = org_flag or (rest[0] if rest else None)
            project = project_flag or (rest[1] if len(rest) > 1 else None)
            if org is None:
                pick = _pick_numbered(list(fleet), "organization")
                if pick is None:
                    print("aborted — nothing done.")
                    return 1
                org = list(fleet)[pick]
            if project is None:
                try:
                    project = input("new project slug: ").strip()
                except EOFError:
                    print("aborted — nothing done.")
                    return 1
            if not project:
                print("error: a project slug is required", file=sys.stderr)
                return 2
            return _create_project(org, project, key, register_only, use_portal)

        if action == "deploy":
            return _deploy(key, type_key, org_flag, project_flag, group_flag,
                           start, extra)
        if action == "list":
            _print_table(_rows(orgs, key), key)
            print(f"actions: {', '.join(ACTIONS)} — see --help")
            return 0
        if action == "refresh":
            return _refresh(orgs, key, interval)
        if action == "describe":
            if group_flag is not None:  # named on the command line: describe ORG --project P --group G
                d_org = org or next(iter(fleet))
                d_project = project_flag
                if d_project is None:
                    projs = list(fleet.get(d_org, ()))
                    if len(projs) == 1:
                        d_project = projs[0]
                    else:
                        pick = _pick_numbered(projs, f"project in {d_org}")
                        if pick is None:
                            print("aborted — nothing done.")
                            return 1
                        d_project = projs[pick]
                return _describe(d_org, d_project, group_flag, key, raw_dump)
            rows = _rows(orgs, key)
            selectable = _print_action_table(rows, key, action)
            if not selectable:
                print("no groups to select from — nothing to do.")
                return 1
            sel = _prompt(selectable, action)
            if sel is None:
                print("aborted — nothing done.")
                return 1
            d_org, d_project, g = sel
            return _describe(d_org, d_project, g.name, key, raw_dump)
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
