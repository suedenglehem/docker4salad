#!/usr/bin/env python3
"""Read-only poller: wait until the v2.5 digests land on all 7 fleet groups
after the async image-only PATCH (mycelium #147). GETs only — never writes.

Exit 0 when all 7 landed, 1 on timeout (12 min cap). Prints one status line
per 20 s and a per-group table at the end.
"""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import salad_client as sc  # noqa: E402

NEW = {
    "base":    "sha256:0586ba35fb439c4ea35271c17d6fa243b3605d993a5c3f3f32ba0b1070011e63",
    "generic": "sha256:2fc912ae0050c8adc9b11e26ff53a777ff0a92cf4560b77841b2da19136683af",
    "q6":      "sha256:a779732c02b2ad8148d21c6ff6f33f5a6d252e9de2e78ab729878342e389ab88",
    "ampere":  "sha256:28417137144d2192093eada751b359dc13b482a0190d7eb37e87b7232feb259b",
}

GROUPS = [
    ("ma-casa-in-paris", "qwen38-27b", "qwen38-27b-q5",    "q6"),
    ("ma-casa-in-paris", "qwen38-27b", "qwen38-27b-q6k",  "q6"),
    ("ma-casa-in-paris", "qwen38-27b", "qwen38-9b",       "base"),
    ("ma-casa-in-paris", "llm",        "qwen38-27b-q5",   "q6"),
    ("ma-casa-in-paris", "llm",        "atx-swift-27b-q5",     "generic"),
    ("ma-casa-in-paris", "llm",        "atx-swift-27b-q5-opt", "ampere"),
    ("ma-casa-in-paris", "llm",        "atx-hb-test2",       "ampere"),
]

with open(os.path.join(REPO, "deploy", "salad_api.txt"), encoding="utf-8") as f:
    key = f.read().strip()

def short(d):
    return d.split("@")[1][:12] if d and "@" in d else str(d)[:24]

t0 = time.time()
deadline = t0 + 12 * 60
last_print = 0.0
state = {}
while time.time() < deadline:
    landed = 0
    for org, proj, name, fam in GROUPS:
        label = f"{proj}/{name}"
        try:
            g = sc.get_container_group(org, proj, name, api_key=key).raw
            img = (g.get("container") or {}).get("image") or ""
        except Exception as e:  # transitional GETs — retry next round, never touch the group
            state[label] = (None, f"GET error: {e}")
            continue
        want = NEW[fam]
        pending = bool(g.get("pending_update_in_progress"))
        if img == want:
            landed += 1
            state[label] = (img, "landed" + (" [pending=True!]" if pending else ""))
        else:
            state[label] = (img, "pending" if pending else "in-flight")
    now = time.time()
    if now - last_print >= 20 or landed == len(GROUPS):
        print(f"[{int(now - t0):4d}s] {landed}/{len(GROUPS)} landed", flush=True)
        last_print = now
        if landed == len(GROUPS):
            break
    time.sleep(20)

print(f"\n=== final state ({int(time.time() - t0)}s) ===")
ok = True
for org, proj, name, fam in GROUPS:
    label = f"{proj}/{name}"
    img, note = state.get(label, (None, "no data"))
    want = NEW[fam]
    mark = "OK " if img == want else "NOT"
    if img != want:
        ok = False
    print(f"  [{mark}] {label:32s} want {short(want)}…  have {short(img)}  ({note})")
sys.exit(0 if ok else 1)
