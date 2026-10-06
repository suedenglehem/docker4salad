#!/usr/bin/env python3
"""Manage Salad container groups — list, live-refresh, start, stop, wait, delete.

Usage:
  python3 manage_groups.py [ACTION] [ORG] [--interval N]

  ACTION    list (default)  one-shot status table
            refresh         re-render the table every N seconds (Ctrl-C to stop)
            start           pick a group (by # or name) and start it — INCURS COST
            stop            pick a group and stop it
            wait            poll a group until it is running and ready, then exit
            delete          pick a group and delete it — IRREVERSIBLE
  ORG       ma-casa-in-paris | akl-on-salad (default: every known org)
  --interval N
            poll period in seconds for refresh / wait (default 5)

  python3 manage_groups.py                     # status, all known orgs
  python3 manage_groups.py akl-on-salad        # one org
  python3 manage_groups.py refresh             # live table, Ctrl-C to stop
  python3 manage_groups.py delete              # numbered table -> prompt -> confirm
  python3 manage_groups.py start akl-on-salad  # start a group in that org
  python3 manage_groups.py wait                # after start: poll until ready

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

import sys
import time
from datetime import datetime, timezone

import salad_client as sc

# org -> projects known to exist in this account (web-UI created; no list API).
KNOWN_PROJECTS: dict[str, tuple[str, ...]] = {
    "ma-casa-in-paris": ("qwen38-27b", "llm"),
    "akl-on-salad": ("default", "comfy"),
}

ACTIONS = ("list", "refresh", "start", "stop", "wait", "delete")


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


def _ready_count(group: sc.ContainerGroupInfo) -> int | None:
    """Ready instance count, or None when the API reports no counts at all."""
    state = group.raw.get("current_state") or {}
    counts = state.get("instance_status_counts") or {}
    return counts.get("ready_count")


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
            print(f"wait for it to become ready:  python3 manage_groups.py wait {org}")
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
                    print(f"note: verification GET returned HTTP {e.status_code} — re-check with: python3 manage_groups.py")
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
    """Poll one group every `interval` seconds until it is running and ready.

    "Ready" = current_status running and (no instance counts reported, or
    at least one ready instance). Returns 0 when ready; 1 when the group
    gives up (stopped/failed/...) or the GET itself fails.
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
        print(f"{ts} UTC  {status:<12} {_instances(g)}")
        if status in ("stopped", "succeeded", "failed", "error", "deleted"):
            print(f"bail: {name} is {status} — it will not become ready (check the Salad UI)")
            return 1
        if status == "running":
            ready = _ready_count(g)
            if ready is None or ready > 0:
                print(f"READY: {name} is running — {_url(g) or '-'}")
                return 0
        time.sleep(interval)


def main(argv: list[str]) -> int:
    interval = 5.0
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

    orgs = [org] if org is not None else list(KNOWN_PROJECTS)
    try:
        key = sc.load_api_key()
    except sc.SaladApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        if action == "list":
            _print_table(_rows(orgs, key), key)
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
        # start / stop / delete need confirmation before the call
        if not _confirm(action, sel):
            print("aborted — nothing done.")
            return 1
        return _run_action(action, sel, key)
    except KeyboardInterrupt:
        print("\nstopped — nothing done.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
