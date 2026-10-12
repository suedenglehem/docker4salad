#!/usr/bin/env python3
"""Image-only PATCH of the 7 fleet groups onto the bw-reporter-v2 (xet fix) v2.5 set.

Same merge-patch contract as patch_images_v10.py: sends ONLY the container
`image` (environment_variables omitted -> the server's env is preserved
byte-for-byte). NO start is triggered — the group is just re-queued onto the
new image; the paid start stays user-gated.

Safety per group:
  * refuse if the group is parked in pending_update_in_progress (a second
    PATCH would 400 anyway),
  * capture before/after env (secrets redacted) and assert they are identical,
  * assert the new image digest landed.

Usage: python3 -I tools/patch_images_v12.py [--dry-run]
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import salad_client as sc  # noqa: E402

DRY = "--dry-run" in sys.argv

# v2.5 set (watchdog unchanged, still v2.3) — the only content change vs v2.4
# is the bw_reporter.py rewrite: the 0-Mbps/xet fix. Reporter now logs
# `epoch rx-bytes model-bytes` from the kernel RX counter (/sys/class/net/
# <if>/statistics/rx_bytes, summed over non-lo — the same counters bmon reads);
# MODEL_DIR growth is display-only. All four digest-verified against the Hub
# (push output, 2026-10-11, tr4, tools/build_push_v25.sh).
NEW = {
    "base":    "boris271142/lmss@sha256:0586ba35fb439c4ea35271c17d6fa243b3605d993a5c3f3f32ba0b1070011e63",
    "generic": "boris271142/lmss_generic@sha256:2fc912ae0050c8adc9b11e26ff53a777ff0a92cf4560b77841b2da19136683af",
    "q6":      "boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision@sha256:a779732c02b2ad8148d21c6ff6f33f5a6d252e9de2e78ab729878342e389ab88",
    "ampere":  "boris271142/lmss_generic_ampere@sha256:28417137144d2192093eada751b359dc13b482a0190d7eb37e87b7232feb259b",
}

# (org, project, group, new-image-key) — family mapping re-verified against
# the live images (2026-10-11): all 7 on the guard-v2.3 set, all stopped.
GROUPS = [
    ("ma-casa-in-paris", "qwen38-27b", "qwen38-27b-q5",   "q6"),
    ("ma-casa-in-paris", "qwen38-27b", "qwen38-27b-q6k",  "q6"),
    ("ma-casa-in-paris", "qwen38-27b", "qwen38-9b",       "base"),
    ("ma-casa-in-paris", "llm",        "qwen38-27b-q5",   "q6"),
    ("ma-casa-in-paris", "llm",        "atx-swift-27b-q5",     "generic"),
    ("ma-casa-in-paris", "llm",        "atx-swift-27b-q5-opt", "ampere"),
    ("ma-casa-in-paris", "llm",        "atx-hb-test2",       "ampere"),
]

with open(os.path.join(REPO, "deploy", "salad_api.txt"), encoding="utf-8") as f:
    key = f.read().strip()


def redact(env):
    out = dict(env or {})
    for s in ("HF_TOKEN", "SALAD_STOP_KEY"):
        if s in out:
            out[s] = "<REDACTED>"
    return out


def get(org, proj, name):
    g = sc.get_container_group(org, proj, name, api_key=key).raw
    c = g.get("container") or {}
    return g, c


ok = 0
for org, proj, name, fam in GROUPS:
    label = f"{proj}/{name}"
    g, c = get(org, proj, name)
    if g.get("pending_update_in_progress"):
        print(f"[SKIP] {label}: pending_update_in_progress — not touching (would 400)")
        continue
    before_img = c.get("image")
    before_env = dict(c.get("environment_variables") or {})
    if not before_env:
        print(f"[SKIP] {label}: live env EMPTY (transitional GET) — refusing, re-check later")
        continue
    new_img = NEW[fam]
    if before_img == new_img:
        print(f"[OK]   {label}: already on {new_img.split('@')[1][:19]}… — nothing to do")
        ok += 1
        continue

    if DRY:
        b = before_img.split("@")[1][:19] if before_img and "@" in before_img else str(before_img)
        print(f"[DRY]  {label}: {b}… -> {new_img.split('@')[1][:19]}…  (env {len(before_env)} keys untouched)")
        continue

    req = sc.UpdateContainerGroupRequest(image=new_img)  # env omitted -> preserved
    res = sc.update_container_group(org, proj, name, req, api_key=key)
    _, c2 = get(org, proj, name)
    after_img = c2.get("image")
    after_env = dict(c2.get("environment_variables") or {})
    env_same = after_env == before_env
    img_ok = after_img == new_img
    flag = "OK" if (env_same and img_ok) else "CHECK"
    a = after_img.split("@")[1][:19] if after_img and "@" in after_img else str(after_img)
    print(f"[{flag}] {label}: {res.status_code} {res.reason_phrase}  image -> {a}…  env {'unchanged' if env_same else 'CHANGED!!!'} ({len(after_env)} keys)")
    if not env_same:
        only_before = set(before_env) - set(after_env)
        only_after = set(after_env) - set(before_env)
        diff = {k for k in before_env if k in after_env and before_env[k] != after_env[k]}
        print(f"       env diff: removed={only_before or '-'} added={only_after or '-'} changed={diff or '-'}")
    if img_ok and env_same:
        ok += 1

print(f"\n{ok}/{len(GROUPS)} {'(dry-run) ' if DRY else ''}groups reconciled onto the v2.5 (bw-reporter-v2, xet fix) image set (no start triggered)")
