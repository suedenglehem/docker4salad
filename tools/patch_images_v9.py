#!/usr/bin/env python3
"""Image-only PATCH of the 7 fleet groups onto the guard-v2.2 image set.

Sends ONLY the container `image` in each merge-patch (environment_variables
omitted -> the server's env is preserved byte-for-byte; the Salad PATCH is a
merge-patch, so omitted keys are kept). NO start is triggered — the group is
just re-queued onto the new image; the paid start stays user-gated.

Safety per group:
  * refuse if the group is parked in pending_update_in_progress (a second
    PATCH would 400 anyway),
  * capture before/after env (secrets redacted) and assert they are identical,
  * assert the new image digest landed.

Usage: python3 -I tools/patch_images_v9.py [--dry-run]
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import salad_client as sc  # noqa: E402

DRY = "--dry-run" in sys.argv

# new digests (Hub-assigned, confirmed via docker inspect RepoDigests /
# docker manifest inspect on the Hub). base v9 / generic v9 / ampere v7 are
# confirmed live; q6 v11 is confirmed at push-completion.
NEW = {
    "base":    "boris271142/lmss@sha256:47d2d276b7991586613131aec1ed7a57b6245d12bc75abe20ebabba5cf189c15",
    "generic": "boris271142/lmss_generic@sha256:9f7b063809d08c2cf897b6c9902f9d09e6f4574d8905f2a080be4839ff90f2a2",
    "ampere":  "boris271142/lmss_generic_ampere@sha256:314f77b808482d1202c17c3910c95d9ba01567b93ded19686c71fb808af5fea5",
    "q6":      "boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision@sha256:16c48781fd6a1a146703dec15a26ed1b87d255ff62f8344282c539bf3c4f4035",
}

# (org, project, group, new-image-key) — mapping verified against each group's
# CURRENT image (tmp/_grpimg.json) so no group is pointed at the wrong family.
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
        print(f"[DRY]  {label}: {before_img.split('@')[1][:19] if before_img and '@' in before_img else before_img}… -> {new_img.split('@')[1][:19]}…  (env {len(before_env)} keys untouched)")
        continue

    req = sc.UpdateContainerGroupRequest(image=new_img)  # env omitted -> preserved
    res = sc.update_container_group(org, proj, name, req, api_key=key)
    _, c2 = get(org, proj, name)
    after_img = c2.get("image")
    after_env = dict(c2.get("environment_variables") or {})
    env_same = after_env == before_env
    img_ok = after_img == new_img
    flag = "OK" if (env_same and img_ok) else "CHECK"
    print(f"[{flag}] {label}: {res.status_code} {res.reason_phrase}  image -> {after_img.split('@')[1][:19] if after_img and '@' in after_img else after_img}…  env {'unchanged' if env_same else 'CHANGED!!!'} ({len(after_env)} keys)")
    if not env_same:
        only_before = set(before_env) - set(after_env)
        only_after = set(after_env) - set(before_env)
        diff = {k for k in before_env if k in after_env and before_env[k] != after_env[k]}
        print(f"       env diff: removed={only_before or '-'} added={only_after or '-'} changed={diff or '-'}")
    if img_ok and env_same:
        ok += 1

print(f"\n{ok}/{len(GROUPS)} {'(dry-run) ' if DRY else ''}groups reconciled onto the guard-v2.2 image set (no start triggered)")
