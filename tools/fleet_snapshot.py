#!/usr/bin/env python3
"""Snapshot the live env/image/pending flags of the 7 fleet groups to JSON.

Usage: python3 -I tools/fleet_snapshot.py <out.json>
Secrets (HF_TOKEN, SALAD_STOP_KEY) are REDACTED in the output — the point is
the diff of the NON-secret env across the fleet PATCH, not the secrets.
"""
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import salad_client as sc  # noqa: E402

GROUPS = [
    ("ma-casa-in-paris", "qwen38-27b", "qwen38-27b-q5"),
    ("ma-casa-in-paris", "qwen38-27b", "qwen38-27b-q6k"),
    ("ma-casa-in-paris", "qwen38-27b", "qwen38-9b"),
    ("ma-casa-in-paris", "llm", "atx-hb-test2"),
    ("ma-casa-in-paris", "llm", "atx-swift-27b-q5"),
    ("ma-casa-in-paris", "llm", "atx-swift-27b-q5-opt"),
    ("ma-casa-in-paris", "llm", "qwen38-27b-q5"),
]

with open(os.path.join(REPO, "deploy", "salad_api.txt"), encoding="utf-8") as f:
    key = f.read().strip()

out = {}
for org, proj, name in GROUPS:
    try:
        g = sc.get_container_group(org, proj, name, api_key=key)
    except Exception as e:  # noqa: BLE001
        out[f"{org}/{proj}/{name}"] = {"error": str(e)}
        continue
    g = g.raw
    # The group SPEC lives in the singular "container" object (the plural
    # "containers" array is runtime instances — empty on stopped groups).
    c = g.get("container") or {}
    env = dict(c.get("environment_variables") or {})
    for secret in ("HF_TOKEN", "SALAD_STOP_KEY"):
        if secret in env:
            env[secret] = "<REDACTED>"
    out[f"{org}/{proj}/{name}"] = {
        "env": env,
        "image": c.get("image"),
        "restart_policy": g.get("restart_policy"),
        "priority": g.get("priority"),
        "pending_change": g.get("pending_change"),
        "pending_update_in_progress": g.get("pending_update_in_progress"),
        "update_time": g.get("update_time"),
    }

with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, sort_keys=True)
print(f"wrote {sys.argv[1]}: {len(out)} groups")
