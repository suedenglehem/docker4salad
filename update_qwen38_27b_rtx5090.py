"""Update group 'qwen38-27b-rtx5090' (org ma-casa-in-paris, project qwen38-27b).

Re-points the live group at the desired image / GPU class / full env and
restarts it. Used for:

  * the 2026-10-01 repair: the group ran a STALE cached `cuda128` image
    (image_caching served the cached digest of an overwritten tag) and was
    bound to the RTX 3090 class while the full 27B + draft + mmproj + 132k
    ctx config needs the RTX 5090 (32 GB).
  * switching the running card, e.g. 5090 -> 3090 to test on the cheaper
    class: --gpu rtx3090.

Per-card env:

  * rtx5090 (32 GB): full set — noMTP Q4_K_M weights + MTP draft (wget URL)
    + mmproj (wget URL) + 132768 ctx.
  * rtx3090 (24 GB): the 3 GB draft and ~1 GB mmproj do not fit on top of
    the 15.4 GB weights, so DRAFT_* / VISION_* are omitted — the image CMD
    then falls back to n-gram self-speculation and text-only serving.
    Same weights, same 132768 ctx (the documented 24 GB config: ~21 GB
    live, no draft/vision).

HF_TOKEN (optional, from hft.txt next to this file — gitignored, never
printed): added to the group env when present AND valid against the Hub
API (one whoami-v2 call); the image CMD forwards it to `hf download` as
--token when set (Dockerfile.multistage). A missing/invalid token is
skipped with a warning — `hf` then downloads anonymously, which works for
public repos (just slower and without the authenticated rate limit).

PATCH semantics (observed live): the merge patch is shallow — `container.*`
keys merge, but `container.environment_variables` is REPLACED wholesale, so
this sends the FULL desired env set, not the delta.

SALAD_API_KEY is read from salad_api.txt by salad_client (never printed).

Usage:
    python3 update_qwen38_27b_rtx5090.py                 # 5090: stop -> PATCH -> start
    python3 update_qwen38_27b_rtx5090.py --gpu rtx3090   # switch the group to a 3090
    python3 update_qwen38_27b_rtx5090.py --no-restart    # PATCH only
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from salad_client import (
    SaladApiError,
    UpdateContainerGroupRequest,
    get_container_group,
    update_container_group,
    list_gpu_classes,
    start_container_group,
    stop_container_group,
    StartContainerGroupRequest,
    StopContainerGroupRequest,
)

ORGANIZATION_NAME = "ma-casa-in-paris"
PROJECT_NAME = "qwen38-27b"
GROUP_NAME = "qwen38-27b-rtx5090"
# cuda128-v2: CMD supports DRAFT_MODEL_URL / VISION_MODEL_URL (wget), q8_0 KV
# cache, and forwards HF_TOKEN to `hf download` as --token when set.
IMAGE = "boris271142/llama-server-on-salad:cuda128-v2"

HF = "https://huggingface.co/JonathanColetti/Qwen3.8-27B-Uncensored-GGUF/resolve/main"
HF_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hft.txt")

GPU_CHOICES = ("rtx3090", "rtx5090")
GPU_CLASS_BASE = {"rtx3090": "rtx3090", "rtx5090": "rtx5090"}  # exact base-name match


def build_env(gpu: str) -> dict[str, str]:
    """Full desired env for the card (PATCH replaces the map wholesale)."""
    env = {
        "GPU_ID": "0",
        "MODEL_REPO": "JonathanColetti/Qwen3.8-27B-Uncensored-GGUF",
        "MODEL_FILE": "Qwen3.8-27B-Uncensored-noMTP-Q4_K_M.gguf",
        # Hybrid model (only 1 in 4 layers is full attention): 132768 ctx costs
        # ~4.6 GB of q8_0 KV — fits a 24 GB card even without draft/vision.
        "CTX_SIZE": "132768",
        "N_GPU_LAYERS": "99",
        "NAME": GROUP_NAME,
        "MODEL_ALIAS": "qwen38-27b",
        # Permissive template the image ships: Claude Code's Anthropic-format
        # /v1/messages requests need system messages accepted anywhere.
        "CHAT_TEMPLATE": "/opt/llama.cpp/qwen3.8.q6.jinja",
    }
    if gpu == "rtx5090":
        env.update({
            "DRAFT_MODEL_URL": f"{HF}/Qwen3.8-27B-Uncensored-draft-Q8_0.gguf",
            "DRAFT_MODEL_FILE": "Qwen3.8-27B-Uncensored-draft-Q8_0.gguf",
            "VISION_MODEL_URL": f"{HF}/mmproj-Qwen3.8-27B-Uncensored-F16.gguf",
        })
    # rtx3090: draft/vision omitted on purpose (24 GB has no room for them).
    return env


def load_hf_token(path: str = HF_TOKEN_FILE) -> str | None:
    """Read + validate the HF token from hft.txt (never printed).

    Validation is one whoami-v2 call; a token the Hub rejects is not sent
    (it would only produce an 'unauthenticated requests' warning in the
    container, and an explicit invalid --token is no one's friend).
    """
    try:
        with open(path, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return None
    if not token:
        return None
    req = urllib.request.Request(
        "https://huggingface.co/api/whoami-v2",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return token if resp.status == 200 else None
    except (urllib.error.URLError, OSError):
        return None


def redact(env: dict[str, str]) -> dict[str, str]:
    """Mask secrets for printing (HF_TOKEN is the only secret in the env)."""
    return {k: "***" if k == "HF_TOKEN" else v for k, v in env.items()}


def resolve_gpu_class(base: str) -> str:
    """UUID of the GPU class whose base name is exactly `base`.

    The base name is the class name without the parenthesized VRAM suffix,
    lowercased, spaces removed — so 'RTX 5090 (32 GB)' matches 'rtx5090'
    and nothing else (e.g. no 'RTX 5090 Laptop' false friends on Salad).
    """
    for gpu_class in list_gpu_classes(ORGANIZATION_NAME):
        name = gpu_class.name.split("(")[0].strip().lower().replace(" ", "")
        if name == base:
            return gpu_class.id
    available = ", ".join(c.name for c in list_gpu_classes(ORGANIZATION_NAME))
    raise ValueError(f"GPU class {base!r} not found among: {available}")


def wait_for(group: str, status: str, timeout_s: int) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        current = get_container_group(ORGANIZATION_NAME, PROJECT_NAME, group)
        if current.current_status == status:
            return
        print(f"      status={current.current_status!r} (waiting for {status!r}) ...", flush=True)
        time.sleep(10)
    raise TimeoutError(f"group did not reach {status!r} within {timeout_s} s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gpu", choices=GPU_CHOICES, default="rtx5090",
                        help="GPU class the group should run on (changes env: 3090 = no draft/vision)")
    parser.add_argument("--no-restart", action="store_true", help="PATCH only, no stop/start")
    args = parser.parse_args()

    print(f"[1/5] reading current group state")
    before = get_container_group(ORGANIZATION_NAME, PROJECT_NAME, GROUP_NAME)
    print(f"      status={before.current_status!r} image={before.raw['container']['image']!r} "
          f"gpu_classes={before.raw['container']['resources']['gpu_classes']}")
    print(f"      dns={before.raw.get('networking', {}).get('dns')!r}")
    print(f"      env={json.dumps(redact(before.raw['container'].get('environment_variables') or {}), indent=6)}")

    print(f"[2/5] resolving GPU class {GPU_CLASS_BASE[args.gpu]!r}")
    gpu_uuid = resolve_gpu_class(GPU_CLASS_BASE[args.gpu])
    print(f"      {args.gpu} -> {gpu_uuid}")

    hf_token = load_hf_token()
    if hf_token:
        print("      HF_TOKEN: hft.txt valid against the Hub — will be passed to the group env")
    else:
        print("      HF_TOKEN: not sent (hft.txt missing or invalid) — the image's `hf download` "
              "runs anonymously (fine for public repos)")

    if not args.no_restart and before.current_status in ("running", "scaling"):
        print("[3/5] stopping group")
        stop_container_group(
            StopContainerGroupRequest(
                organization_name=ORGANIZATION_NAME,
                project_name=PROJECT_NAME,
                container_group_name=GROUP_NAME,
            )
        )
        wait_for(GROUP_NAME, "stopped", timeout_s=120)
        print("      stopped")
    else:
        print("[3/5] group not running — skipping stop")

    env = build_env(args.gpu)
    if hf_token:
        env["HF_TOKEN"] = hf_token

    print(f"[4/5] PATCH: image + gpu_classes + full env ({len(env)} vars)")
    print(f"      image={IMAGE!r}")
    print(f"      gpu_classes=({gpu_uuid},)")
    print(f"      env={json.dumps(redact(env), indent=6)}")
    result = update_container_group(
        ORGANIZATION_NAME, PROJECT_NAME, GROUP_NAME,
        UpdateContainerGroupRequest(
            image=IMAGE,
            gpu_classes=(gpu_uuid,),
            environment_variables=env,
        ),
    )
    print(f"      HTTP {result.status_code} {result.reason_phrase} status={result.current_status!r}")

    if args.no_restart:
        print("OK: PATCH applied (no restart requested)")
        return 0

    print("[5/5] starting group")
    start_container_group(
        StartContainerGroupRequest(
            organization_name=ORGANIZATION_NAME,
            project_name=PROJECT_NAME,
            container_group_name=GROUP_NAME,
        )
    )
    download_note = ("main model + draft + mmproj (~19.2 GB)" if args.gpu == "rtx5090"
                     else "main model only (~16.5 GB; no draft/vision on 24 GB)")
    print(f"      waiting for running (first start on a new worker re-downloads: "
          f"{download_note}, at Salad worker bandwidth that can take hours)...")
    wait_for(GROUP_NAME, "running", timeout_s=1800)

    after = get_container_group(ORGANIZATION_NAME, PROJECT_NAME, GROUP_NAME)
    c = after.raw["container"]
    print(f"      status={after.current_status!r} image={c['image']!r}")
    print(f"      gpu_classes={c['resources']['gpu_classes']}")
    print(f"      env={json.dumps(redact(c.get('environment_variables') or {}), indent=6)}")
    print(f"      dns={after.raw.get('networking', {}).get('dns')!r}")
    print("OK: group updated and running. Note: 'running' means the container process "
          "is up — the model may still be downloading. Watch /v1/chat/completions on the "
          "gateway (curl2_salad.sh) until it answers, then verify in-container if needed "
          "(/proc/1/cmdline, nvidia-smi).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SaladApiError, ValueError, TimeoutError) as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
