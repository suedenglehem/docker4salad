#!/usr/bin/env python3
"""Merge-safe env editor for one Salad group — add/change keys without retyping the dict.

WHY THIS EXISTS: `container.environment_variables` is REPLACED wholesale, not
merged (PROGRESS.md §9e). A hand-written PATCH that sends only the new key
DELETES every other key on the group. This tool reads the live env, applies the
edits on top of it, and sends the FULL dict, so untouched keys are preserved
byte-for-byte (including HF_TOKEN / SALAD_STOP_KEY, which are never printed).

Group arg is `org/project/group`. Edits are `KEY=VALUE`; `--unset KEY` removes.
`CHAT_TEMPLATE_URL=none` is the sentinel that turns the template download off.

Locked groups: a group with a paid start in its history parks a PATCH
(`pending_update_in_progress`) and REFUSES further PATCHes with HTTP 400 until it
is started. The GET cannot show this — it has no such field — so this tool
discovers it on the write and reports `[LOCKED]` (nothing was changed).

Readback is POLLED: the GET is eventually consistent over MINUTES, so a single
GET after a 200 is not a verdict.

Examples:
  python3 -I tools/sl_set_env.py ma-casa-in-paris/llm/qwen38-27b-q5 \
      CHAT_TEMPLATE_URL=https://raw.githubusercontent.com/suedenglehem/docker4salad/main/docker/qwen3.8.q6.jinja \
      --dry-run
  python3 -I tools/sl_set_env.py ma-casa-in-paris/llm/qwen38-27b-q5 CHAT_TEMPLATE_URL=none
"""
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import salad_client as sc  # noqa: E402

SECRETS = ("HF_TOKEN", "SALAD_STOP_KEY")
SETTLE_S = 240


def redact(env):
    return {k: ("<REDACTED>" if k in SECRETS else v) for k, v in (env or {}).items()}


def read_env(org, proj, name, key):
    """Live env, retried: a transitional GET can return an empty container."""
    for _ in range(4):
        g = sc.get_container_group(org, proj, name, api_key=key).raw
        c = g.get("container") or {}
        if c.get("image"):
            return dict(c.get("environment_variables") or {}), c.get("image")
        time.sleep(0.6)
    return {}, (c or {}).get("image")


def main(argv):
    dry = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    unsets = []
    while "--unset" in argv:
        i = argv.index("--unset")
        if i + 1 >= len(argv):
            print("--unset needs a KEY"); return 2
        unsets.append(argv[i + 1]); del argv[i:i + 2]
    if len(argv) < 2:
        print(__doc__); return 2
    scope = argv[0]
    parts = scope.split("/")
    if len(parts) != 3:
        print(f"scope must be org/project/group, got {scope!r}"); return 2
    org, proj, name = parts
    edits = {}
    for a in argv[1:]:
        if "=" not in a:
            print(f"edit must be KEY=VALUE, got {a!r}"); return 2
        k, v = a.split("=", 1)
        if not k.strip():
            print(f"empty key in {a!r}"); return 2
        edits[k.strip()] = v
    if not edits and not unsets:
        print("nothing to do — no KEY=VALUE and no --unset"); return 2

    with open(os.path.join(REPO, "deploy", "salad_api.txt"), encoding="utf-8") as f:
        key = f.read().strip()

    before, image = read_env(org, proj, name, key)
    if not before:
        print(f"[ABORT] env reads EMPTY after 4 GETs — refusing to send a wholesale "
              f"replace against an unread dict")
        return 1
    after = dict(before)
    for k in unsets:
        after.pop(k, None)
    after.update(edits)

    added = set(after) - set(before)
    removed = set(before) - set(after)
    changed = {k for k in before if k in after and before[k] != after[k]}
    print(f"{org}/{proj}/{name}  image {image}")
    print(f"  env {len(before)} -> {len(after)} keys   "
          f"added={added or '-'} removed={removed or '-'} changed={changed or '-'}")
    for k in sorted(added | changed):
        print(f"    {k} = {redact(after).get(k)}")
    if dry:
        print("  (dry-run — nothing sent)")
        return 0

    try:
        res = sc.update_container_group(
            org, proj, name, sc.UpdateContainerGroupRequest(environment_variables=after),
            api_key=key)
        code = res.status_code
    except sc.SaladApiError as e:
        if e.status_code == 400 and bool(e.problem) and e.problem.type == "pending_update_in_progress":
            print(f"[LOCKED] HTTP 400 pending_update_in_progress — this group is parked from an "
                  f"earlier PATCH and refuses writes until its next paid start. NOTHING CHANGED.")
            return 3
        raise

    t0 = time.time()
    landed = False
    while time.time() - t0 < SETTLE_S:
        now, _ = read_env(org, proj, name, key)
        if now == after:
            landed = True
            break
        time.sleep(5)
    print(f"[{'OK' if landed else 'CHECK'}] HTTP {code}  env {len(after)} keys  "
          f"{time.time() - t0:.0f}s"
          + ("" if landed else "  readback does not match yet — re-check with "
                               "tools/fleet_status_now.py (the GET lags minutes)"))
    return 0 if landed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
