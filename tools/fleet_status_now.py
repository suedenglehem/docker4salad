#!/usr/bin/env python3
"""Read-only: status + guard posture for the 7 fleet groups. No writes, no starts."""
import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import salad_client as sc

GROUPS = [
    ("ma-casa-in-paris", "qwen38-27b", "qwen38-27b-q5"),
    ("ma-casa-in-paris", "qwen38-27b", "qwen38-27b-q6k"),
    ("ma-casa-in-paris", "qwen38-27b", "qwen38-9b"),
    ("ma-casa-in-paris", "llm", "qwen38-27b-q5"),
    ("ma-casa-in-paris", "llm", "atx-swift-27b-q5"),
    ("ma-casa-in-paris", "llm", "atx-swift-27b-q5-opt"),
    ("ma-casa-in-paris", "llm", "atx-hb-test2"),
]
with open(os.path.join(REPO, "deploy", "salad_api.txt"), encoding="utf-8") as f:
    key = f.read().strip()

def s(x):
    return "-" if x is None else str(x)

def short(img):
    if not img: return "-"
    return img.split("@sha256:")[-1][:12]

# v2.5 set (current, re-pinned by tools/patch_images_v12.py) + the v2.4 generation.
NEW = {
    "0586ba35fb43": "base v12 (v2.5)",
    "2fc912ae0050": "generic v12 (v2.5)",
    "a779732c02b2": "q6 v14 (v2.5)",
    "28417137144d": "ampere v10 (v2.5)",
    "2bfbd69c5730": "base v11 (v2.4)",
    "888fc401f1eb": "generic v11 (v2.4)",
    "16c48781fd6a": "q6 v11 (v2.4)",
    "d4f4b030fe2d": "ampere v9 (v2.4)",
}

for org, proj, name in GROUPS:
    try:
        g = sc.get_container_group(org, proj, name, api_key=key).raw
    except sc.SaladApiError as e:
        # Deleted groups 404 — the watchlist is fixed, so report and move on
        # (a traceback here used to kill the whole snapshot mid-run).
        print(f"{proj}/{name:<20} {'GONE' if e.status_code == 404 else 'GET ' + str(e.status_code)}")
        continue
    c = g.get("container") or {}
    env = dict(c.get("environment_variables") or {})
    img = c.get("image")
    cs = g.get("current_state") or {}
    st = s(cs.get("status"))
    desc = (cs.get("description") or "").replace("\n", " ")
    counts = cs.get("instance_status_counts") or {}
    imgkey = img.split("@sha256:")[-1][:12] if img and "@" in img else (img or "")
    fam = NEW.get(imgkey, "UNKNOWN")
    print(f"{proj}/{name:<20} cs.status={st:<9} v{s(g.get('version'))} pc={s(g.get('pending_change'))} "
          f"SSH_GUARD={s(env.get('SSH_GUARD'))}")
    print(f"    active img = {imgkey} [{fam}]   counts={counts or '-'}")
    if desc:
        print(f"    {desc[:120]}")
