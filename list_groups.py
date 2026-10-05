#!/usr/bin/env python3
"""List all container groups and their statuses — account-wide or one org.

Usage:
  python3 list_groups.py                 # every known org in the account
  python3 list_groups.py akl-on-salad    # just that org

The SaladCloud API has no list-orgs / list-projects operations, so this walks
a fixed map of the account's orgs and their projects. Add new projects to
KNOWN_PROJECTS below when you create them in the web UI.

For every group (active or not) it prints: status, age (since creation),
uptime (time in the current state), instance counts, and the
gateway URL from networking.dns (the host Salad assigns per group; the port is
routed by the gateway itself, so no :port suffix is needed). For groups that
are running it also fetches their live instances and prints the SSH line
(`ssh -p PORT root@IP`) plus the host-key fingerprint.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import salad_client as sc

# org -> projects known to exist in this account (web-UI created; no list API).
KNOWN_PROJECTS: dict[str, tuple[str, ...]] = {
    "ma-casa-in-paris": ("qwen38-27b", "llm"),
    "akl-on-salad": ("default", "comfy"),
}


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


def _ssh_lines(org: str, project: str, group_name: str, key: str) -> list[sc.ContainerGroupInstanceInfo]:
    """Live instances of a running group (for the SSH line); [] on API error."""
    try:
        return list(sc.list_container_group_instances(org, project, group_name, api_key=key))
    except sc.SaladApiError as e:
        print(f"{'':<34} {'':<28} {'':<12} {'':>9} {'':>9} {'(instances failed)':<16} HTTP {e.status_code}")
        return []


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    orgs = [argv[1]] if len(argv) == 2 else list(KNOWN_PROJECTS)
    unknown = [o for o in orgs if o not in KNOWN_PROJECTS]
    if unknown:
        print(
            f"error: no known projects for org(s): {', '.join(unknown)}\n"
            "known orgs: " + ", ".join(KNOWN_PROJECTS) + "\n"
            "(add the org's projects to KNOWN_PROJECTS in list_groups.py)",
            file=sys.stderr,
        )
        return 2

    key = sc.load_api_key()
    header = f"{'ORG/PROJECT':<34} {'GROUP':<28} {'STATUS':<12} {'AGE':>9} {'UPTIME':>9} {'INSTANCES':<16} URL"
    print(header)
    print("-" * len(header))

    total = 0
    for org in orgs:
        for project in KNOWN_PROJECTS[org]:
            scope = f"{org}/{project}"
            try:
                groups = sc.list_container_groups(org, project, api_key=key)
            except sc.SaladApiError as e:
                print(f"{scope:<34} {'(list failed)':<28} HTTP {e.status_code}")
                continue
            if not groups:
                print(f"{scope:<34} {'(no groups)':<28}")
                continue
            for g in sorted(groups, key=lambda x: x.name):
                total += 1
                url = _url(g) or "-"
                print(
                    f"{scope:<34} {g.name:<28} {(g.current_status or '?'):<12}"
                    f" {_age(g):>9} {_uptime(g):>9} {_instances(g):<16} {url}"
                )
                if g.current_status == "running":
                    for inst in _ssh_lines(org, project, g.name, key):
                        if not inst.ssh_line:  # API omits ssh_* when the group has no SSH config
                            continue
                        fp = f"  [{inst.ssh_host_key_fingerprint}]" if inst.ssh_host_key_fingerprint else ""
                        print(f"{'':<34} {'':<28} {'':<12} {'':>9} {'':>9} {inst.ssh_line}{fp}")

    print("-" * len(header))
    print(f"{total} group(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
