#!/usr/bin/env python3
"""READ-ONLY readiness watcher for the arbitrator start (no writes, no cost).

Polls the group + its instances every 30 s and prints ONLY transitions:
status change, a new instance id (= a /reallocate landed), ready=True (with the
ssh line, so the argv capture can run), and group stopped. Exits on ready.

Started by the user via: manage_groups.py start ma-casa-in-paris --min-bw-mbps 100
"""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import salad_client as sc  # noqa: E402

ORG, PROJ, NAME = "ma-casa-in-paris", "llm", "atx-swift-27b-q5-med"
POLL_S = 30.0
MAX_S = 60 * 60.0

with open(os.path.join(REPO, "deploy", "salad_api.txt"), encoding="utf-8") as f:
    KEY = f.read().strip()


def ts() -> str:
    return time.strftime("%H:%M:%S", time.gmtime()) + "Z"


def main() -> int:
    last_status = None
    seen = set()
    ready_line = None
    t0 = time.time()
    while time.time() - t0 < MAX_S:
        try:
            g = sc.get_container_group(ORG, PROJ, NAME, api_key=KEY).raw
        except sc.SaladApiError as e:
            print(f"{ts()} group GET -> {e.status_code} (retry)", flush=True)
            time.sleep(POLL_S)
            continue
        cs = g.get("current_state") or {}
        status = cs.get("status")
        counts = cs.get("instance_status_counts") or {}
        live = {k: v for k, v in counts.items() if v}
        if status != last_status:
            print(f"{ts()} status={status} {live}", flush=True)
            last_status = status
        try:
            insts = sc.list_container_group_instances(ORG, PROJ, NAME, api_key=KEY)
        except sc.SaladApiError as e:
            print(f"{ts()} instances GET -> {e.status_code}", flush=True)
            insts = ()
        for i in insts:
            if i.id not in seen:
                first = not seen
                seen.add(i.id)
                tag = "INSTANCE" if first else "NEW INSTANCE (a /reallocate landed)"
                print(f"{ts()} {tag} {i.id[:8]} machine={i.machine_id}", flush=True)
            if i.ready:
                print(f"{ts()} READY {i.id[:8]} machine={i.machine_id} ssh={i.ssh_line}",
                      flush=True)
                ready_line = i.ssh_line
                break
        if ready_line:
            print(f"{ts()} capture argv: ssh <ip> -p <port> "
                  f"'nvidia-smi --query-compute-apps=pid --format=csv,noheader'",
                  flush=True)
            return 0
        if status == "stopped":
            print(f"{ts()} GROUP STOPPED — nothing to capture", flush=True)
            return 1
        time.sleep(POLL_S)
    print(f"{ts()} TIMEOUT after {int(MAX_S / 60)} min — not ready", flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
