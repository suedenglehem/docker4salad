#!/usr/bin/env python3
"""Image-only PATCH of the 7 fleet groups onto the shell-ergonomics v2.4 set.

Same merge-patch contract as patch_images_v10.py: sends ONLY the container
`image` (environment_variables omitted -> the server's env is preserved
byte-for-byte). NO start is triggered — the group is just re-queued onto the
new image; the paid start stays user-gated.

Safety per group:
  * refuse if the group is parked in pending_update_in_progress (a second
    PATCH would 400 anyway),
  * capture before/after env (secrets redacted) and assert they are identical,
  * assert the new image digest landed.

Usage: python3 -I tools/patch_images_v11.py [--dry-run]
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import salad_client as sc  # noqa: E402

DRY = "--dry-run" in sys.argv

# v2.4 set (watchdog unchanged, still v2.3) — the only content change vs v2.3
# is the /root/.bashrc shell-ergonomics block (TERM=xterm-256color +
# nv/ht/gpu/wlog/alog/ready/als/ll/la/l/h aliases). All four digest-verified
# against the Hub (push output, 2026-10-11, tr4, tools/build_push_v24.sh).
NEW = {
    "base":    "boris271142/lmss@sha256:2bfbd69c5730b9787f2b91cfd496ebbfdcce469ba99fc538fbe5a66b2a305455",
    "generic": "boris271142/lmss_generic@sha256:888fc401f1eba851888f22bf8f8caf9665df41f734cfe1432e6ed1df23f14b1b",
    "q6":      "boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision@sha256:f6e3b9d0c4f6b3fb08ecfabb94c4115d256cdce246054202f858b868115bbfb9",
    "ampere":  "boris271142/lmss_generic_ampere@sha256:d4f4b030fe2d88c1fdaaf5a31505ac0f4d357f1db38f8e7b369804d3502e6593",
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

print(f"\n{ok}/{len(GROUPS)} {'(dry-run) ' if DRY else ''}groups reconciled onto the v2.4 (shell-ergonomics) image set (no start triggered)")
