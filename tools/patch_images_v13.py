#!/usr/bin/env python3
"""Image-only PATCH of EVERY fleet group onto the chat-template-url image set.

Same merge-patch contract as patch_images_v12.py: sends ONLY the container
`image` (environment_variables omitted -> the server's env is preserved
byte-for-byte). NO start is triggered — the group is just re-queued onto the new
image; the paid start stays user-gated.

Why a digest and not a tag: the Salad worker image cache is keyed by REPO NAME
and a tag never re-pulls, so retagging a group does not guarantee a fresh pull.
A digest the worker has never run is a cache miss — that is the airtight lever.

Every group keeps its own FAMILY (it moves lmss->lmss, lmss_generic->
lmss_generic, q6->q6, ampere->ampere); the family is read from the group's
current image, so a group never gets switched between image lines by this tool.
Groups on an unrecognised repo are reported and skipped.

Two end states, both success (verified live 2026-10-12 on 5 groups):
  * [OK]     the new digest READS BACK — a never-started group applies instantly.
  * [STAGED] the update is QUEUED and applies at the next paid start — the state
             of every previously-started group. Its signature is a 400
             `pending_update_in_progress` on the write path: the GET response has
             NO `pending_update_in_progress` field (only `pending_change`), so the
             parked state is only observable on a WRITE. The GET keeps reporting
             the OLD image with pending_change=True the whole time — that is the
             documented "staged, not yet visible" behaviour (PROGRESS.md §9e), not
             a failed PATCH. Evidence a PATCH landed: HTTP 200 + version bump +
             update_time advanced + pending_change=True.

Both are counted; the paid start stays user-gated.

Safety per group:
  * a transitional GET can return an EMPTY container object — env is re-read a
    few times before anything is sent (an empty env is then treated as real,
    since an image-only PATCH is env-neutral either way),
  * the readback is POLLED (SETTLE_S), never a single GET,
  * capture before/after env (secrets redacted) and assert they are identical.

Re-running is safe: a group already on target is skipped, and a repeat PATCH of
the same digest is idempotent — on a staged group it just returns the 400 that
this tool reads as "already queued".

Usage: python3 -I tools/patch_images_v13.py [--dry-run] [--settle=SECONDS]
"""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "utils"))
import salad_client as sc  # noqa: E402
import sl_admin  # noqa: E402  (for the org -> projects map, fleet.json overlaid)

DRY = "--dry-run" in sys.argv
SETTLE_S = 180
for _a in sys.argv:
    if _a.startswith("--settle="):
        SETTLE_S = int(_a.split("=", 1)[1])

# The chat-template-url set, built + pushed on tr4 2026-10-12 at BUILD_JOBS=24
# (tools/build_chat_url_images.sh / tools/push_chat_url_images.sh; digests from
# the push output = the Hub manifest digests the deployers are pinned to).
NEW = {
    "base":    "boris271142/lmss@sha256:d6e1b1d940608e25e0f11a84bebafc3a20c8df5b59d6c42673d98ef93bf337cd",
    "generic": "boris271142/lmss_generic@sha256:14cb99414aa6d233d66922ea8d7bf88b353f63828f3dc348c783dfe5ef75b0b4",
    "q6":      "boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision@sha256:75c8cf667590ee9096dc4e6bd11afe24acd84f988225d76ef89a202939d36e47",
    "ampere":  "boris271142/lmss_generic_ampere@sha256:4ad7dd6b7ab2961f19644dc17d5cce9bd1ae64c0544ac6fa3ebd301de49e0645",
}

FAMILY_BY_REPO = {
    "lmss": "base",
    "lmss_generic": "generic",
    "lmss_jonathancoletti_qwen38_q6_mtp_vision": "q6",
    "lmss_generic_ampere": "ampere",
}

with open(os.path.join(REPO, "deploy", "salad_api.txt"), encoding="utf-8") as f:
    key = f.read().strip()


def redact(env):
    out = dict(env or {})
    for s in ("HF_TOKEN", "SALAD_STOP_KEY"):
        if s in out:
            out[s] = "<REDACTED>"
    return out


def family_of(image):
    """'boris271142/lmss_generic@sha256:…' / '…:cuda128-v13' -> 'generic'."""
    if not image:
        return None
    repo = image.split("@")[0].split(":")[0]
    return FAMILY_BY_REPO.get(repo.rsplit("/", 1)[-1])


def short(ref):
    return (ref.split("@")[1][:19] + "…") if ref and "@" in ref else str(ref)


def read_group(org, proj, name):
    """Full GET, retried: a transitional response can carry an empty container."""
    for attempt in range(3):
        g = sc.get_container_group(org, proj, name, api_key=key).raw
        c = g.get("container") or {}
        if c.get("image"):
            return g, c
        time.sleep(0.6)
    return g, c


def parked(org, proj, name, img):
    """Is this group's update staged (queued for the next start)?

    The GET cannot tell us: its response has NO `pending_update_in_progress`
    field at all (only `pending_change`), so the parked state is only visible on
    a WRITE. A repeat PATCH of the SAME digest is therefore a free, no-op probe:
    400 + type `pending_update_in_progress` = staged (the write path refuses a
    second update until the paid start); 200 = the group accepts writes, i.e. a
    never-started group whose spec applies instantly.
    """
    try:
        sc.update_container_group(org, proj, name,
                                  sc.UpdateContainerGroupRequest(image=img), api_key=key)
        return False
    except sc.SaladApiError as e:
        return e.status_code == 400 and bool(e.problem) and e.problem.type == "pending_update_in_progress"


def settle(org, proj, name, want):
    """Poll the GET until `want` reads back and pending_change clears.

    Returns (container, pending_change, elapsed_s, settled). Running out the
    deadline with pending_change=True is the legitimate end state of a staged
    update — the spec applies at the next start — not a failure.
    """
    t0, g, c = time.time(), None, {}
    while time.time() - t0 < SETTLE_S:
        g, c = read_group(org, proj, name)
        if c.get("image") == want and not g.get("pending_change"):
            return c, g.get("pending_change"), time.time() - t0, True
        time.sleep(5)
    return c, (g or {}).get("pending_change"), time.time() - t0, False


total = ok = 0
for org, projects in sl_admin._fleet().items():
    for proj in projects:
        try:
            groups = sc.list_container_groups(org, proj, api_key=key)
        except sc.SaladApiError as e:
            print(f"[SKIP] {org}/{proj}: list failed ({e.status_code})")
            continue
        for item in groups:
            name = item.name
            label = f"{org}/{proj}/{name}"
            total += 1
            g, c = read_group(org, proj, name)
            fam = family_of(c.get("image"))
            if fam is None:
                print(f"[SKIP] {label}: image {c.get('image')!r} is not one of our four "
                      f"repos — not touching")
                continue
            before_img = c.get("image")
            before_env = dict(c.get("environment_variables") or {})
            if not before_env:
                print(f"[NOTE] {label}: env reads EMPTY (3 GETs) — proceeding, an "
                      f"image-only PATCH cannot change env")
            new_img = NEW[fam]
            if before_img == new_img:
                print(f"[OK]   {label}: {fam} already on {short(new_img)} — nothing to do")
                ok += 1
                continue

            if DRY:
                print(f"[DRY]  {label}: {fam} {short(before_img)} -> {short(new_img)}  "
                      f"(env {len(before_env)} keys untouched)")
                continue

            code, staged, landed = None, False, False
            try:  # env omitted -> preserved byte-for-byte
                code = sc.update_container_group(org, proj, name,
                                                 sc.UpdateContainerGroupRequest(image=new_img),
                                                 api_key=key).status_code
            except sc.SaladApiError as e:
                if e.status_code == 400 and bool(e.problem) and e.problem.type == "pending_update_in_progress":
                    code, staged = e.status_code, True  # this group's update is already queued
                else:
                    raise
            if not staged:
                _, pc, el, landed = settle(org, proj, name, new_img)
                if not landed:
                    staged = parked(org, proj, name, new_img)
            g2, c2 = read_group(org, proj, name)
            after_img = c2.get("image")
            after_env = dict(c2.get("environment_variables") or {})
            env_same = after_env == before_env
            verdict = "OK" if (landed and env_same) else ("STAGED" if (staged and env_same) else "CHECK")
            print(f"[{verdict.ljust(6)}] {label}: HTTP {code}  {fam} {short(before_img)} -> "
                  f"{short(after_img)}  env {'unchanged' if env_same else 'CHANGED!!!'} "
                  f"({len(after_env)} keys)"
                  + ("" if landed else f"  pending_change={g2.get('pending_change')} — queued, "
                                        f"applies at the next paid start"))
            if not env_same:
                only_before = set(before_env) - set(after_env)
                only_after = set(after_env) - set(before_env)
                diff = {k for k in before_env if k in after_env and before_env[k] != after_env[k]}
                print(f"       env diff: removed={only_before or '-'} added={only_after or '-'} "
                      f"changed={diff or '-'}")
                print(f"       before={redact(before_env)}")
                print(f"       after ={redact(after_env)}")
            if (landed or staged) and env_same:
                ok += 1

print(f"\n{ok}/{total} {'(dry-run) ' if DRY else ''}groups on the chat-template-url set "
      f"(base v13 / generic v13 / q6 v15 / ampere v11) — no start triggered, billing untouched")
