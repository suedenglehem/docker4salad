"""One-off rescue for the stopped 'qwen38-9b' group (2026-10-01).

The live v3 instances were running PRE-SENTINEL image content: the Salad
worker image cache is keyed by REPO NAME, so even the renamed `lmss` repo
served the stale cached build on workers that had pulled the old one.
The only airtight lever is a digest-pinned image ref to a digest no worker
has seen — this PATCHes the group to the fresh 2026-10-01 URL-only build
(wget2 + version.sh + the `none` sentinel; DRAFT_MODEL_URL is the sole
draft source), digest-pinned, with the 10-key env (explicit `none`
sentinels, the user's way), then starts it.

If the API rejects digest refs, falls back to the tag `lmss:cuda128-v3`
and records the rejection.

SALAD_API_KEY is read from salad_api.txt by salad_client (never printed).

Usage:
    python3 patch_qwen38_9b_rescue.py
    python3 patch_qwen38_9b_rescue.py --org akl-on-salad   # another org on the same account
    python3 patch_qwen38_9b_rescue.py --project llm --group qwen38-9b-llm
"""

import argparse
import json
import sys
import time

from salad_client import (
    SaladApiError,
    StartContainerGroupRequest,
    UpdateContainerGroupRequest,
    get_container_group,
    start_container_group,
    update_container_group,
)

# Default org (override with --org): an account can host several orgs, all
# sharing the same Salad API key.
ORGANIZATION_NAME = "ma-casa-in-paris"
PROJECT_NAME = "qwen38-27b"
GROUP_NAME = "qwen38-9b"
# Fresh 2026-10-01 URL-only build (wget2 + version.sh + sentinel; DRAFT_MODEL_URL
# is the sole draft source), pushed as lmss:cuda128-v3. Digest-pinned first
# (manifest list digest of the push); the tag is the fallback if the API
# rejects digest refs.
IMAGE_DIGEST = ("boris271142/lmss@"
                "sha256:8b755b6bac7467289624c9db5c969bf0a5f85a3299ebfadd35057bda0ea58c49")
IMAGE_TAG = "boris271142/lmss:cuda128-v3"

# The 10-key env: model + sizing, and the three optional features EXPLICITLY
# disabled with the `none` sentinel. The group env replaces the image env
# wholesale, so this also protects against any stale pre-sentinel build whose
# baked-in defaults were real 27B draft/vision URLs. (v3 has no
# DRAFT_MODEL_FILE at all — the URL is the only draft source.)
ENV = {
    "GPU_ID": "0",
    "MODEL_REPO": "empero-ai/Qwen3.8-9B-Distill-GGUF",
    "MODEL_FILE": "Qwen3.8-9B-Q4_K_M.gguf",
    "MODEL_ALIAS": "qwen9b",
    "CTX_SIZE": "32768",
    "N_GPU_LAYERS": "99",
    "NAME": GROUP_NAME,  # overridden in main() with the --group value
    "DRAFT_MODEL_URL": "none",
    "VISION_MODEL_URL": "none",
    "CHAT_TEMPLATE": "none",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Digest-pinned image rescue for the qwen38-9b group.")
    parser.add_argument("--org", default=ORGANIZATION_NAME,
                        help="Salad organization to patch (an account can host several "
                             "orgs sharing one API key)")
    parser.add_argument("--project", default=PROJECT_NAME,
                        help="Salad project holding the group")
    parser.add_argument("--group", default=GROUP_NAME,
                        help="container group name to patch")
    args = parser.parse_args()
    org = args.org
    project = args.project
    group = args.group
    env = {**ENV, "NAME": group}

    print(f"[1/3] reading current state of {group!r} in org {org!r}")
    before = get_container_group(org, project, group)
    print(f"      status={before.current_status!r} image={before.raw['container']['image']!r}")
    print(f"      env={json.dumps(before.raw['container'].get('environment_variables') or {})}")

    print("[2/3] PATCH: digest-pinned image first, tag fallback")
    image = IMAGE_DIGEST
    try:
        result = update_container_group(
            org, project, group,
            UpdateContainerGroupRequest(image=image, environment_variables=dict(env)),
        )
    except SaladApiError as e:
        detail = (e.problem.detail if e.problem else "") or str(e)
        if e.status_code in (400, 422):
            print(f"      digest ref rejected (HTTP {e.status_code}: {detail!r}) "
                  f"— falling back to tag {IMAGE_TAG!r}")
            image = IMAGE_TAG
            result = update_container_group(
                org, project, group,
                UpdateContainerGroupRequest(image=image, environment_variables=dict(env)),
            )
        else:
            raise
    print(f"      HTTP {result.status_code} {result.reason_phrase} status={result.current_status!r}")

    after = get_container_group(org, project, group)
    c = after.raw["container"]
    print(f"      stored image={c['image']!r} (sent {image!r})")
    print(f"      stored env={json.dumps(c.get('environment_variables') or {})}")

    print("[3/3] starting group")
    start_container_group(
        StartContainerGroupRequest(
            organization_name=org,
            project_name=project,
            container_group_name=group,
        )
    )
    print("      start accepted. First start on a new worker re-downloads the 9B "
          "(~5.8 GB) — no draft/vision this time, so nothing after that.")
    deadline = time.time() + 1800
    while time.time() < deadline:
        current = get_container_group(org, project, group)
        if current.current_status == "running":
            break
        print(f"      status={current.current_status!r} ...", flush=True)
        time.sleep(15)
    else:
        print("FAIL: group did not reach 'running' within 1800 s", file=sys.stderr)
        return 1

    final = get_container_group(org, project, group)
    net = final.raw.get("networking") or {}
    print(f"      status={final.current_status!r} image={final.raw['container']['image']!r}")
    print(f"      dns={net.get('dns')!r}")
    print("OK: group running. Note: 'running' = container process up; the model may still")
    print("      be downloading. Verify the LIVE build in the container with version.sh")
    print("      (expect build id 'lmss cuda128-v3 (URL-only) 2026-10-01',")
    print("      SENTINEL with guards = 3, only the 9B in /models), then /ready on 8889,")
    print(f"      then: docker/docker_tests/curl2_salad.sh -url https://{net.get('dns')} -m qwen9b")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SaladApiError, ValueError, TimeoutError) as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
