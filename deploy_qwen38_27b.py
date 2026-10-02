"""Deploy the canonical 27B group 'qwen38-27b-q6k' (project 'qwen38-27b', org ma-casa-in-paris).

2026-10-02: re-created under a FRESH name (replacing 'qwen38-27b-cuda128').
The original name 'qwen38-27b' is stuck in the DELETE name-conflict
tombstone (salad-group-name-tombstone memory) — still 400 name_conflict
24h+ after its 2026-10-01 DELETE, so the user approved the name
'qwen38-27b-Q6K'; the API name pattern is lowercase-only, hence
'qwen38-27b-q6k'. The served alias stays 'qwen38-27b' (MODEL_ALIAS env,
llama-server --alias), so clients and curl2_salad.sh -m are unaffected.

Serves the baked image `lmss_jonathancoletti_qwen38_q6_mtp_vision` (a
FROM boris271142/lmss:cuda128-v3 extension, Dockerfile.lmss_q6_mtp_vision):
the MTP draft (Q8_0, 2.95 GiB) and mmproj (F16, 885 MiB) are baked into the
image layers, so at runtime ONLY the main model is downloaded from
HuggingFace via the fast `hf` xet path. The v3 wget2 DRAFT_MODEL_URL /
VISION_MODEL_URL mechanism is GONE — those env vars are no longer read.

Model: Qwen3.8-27B-Uncensored-Q6_K.gguf (20.9 GiB). This quant has the MTP
head EMBEDDED in the gguf (nextn_predict_layers metadata), so it runs on the
in-gguf head: USE_DRAFT_MODEL=none, --spec-type draft-mtp, NO --model-draft.
For a quant WITHOUT an embedded head (e.g. the noMTP-Q4_K_M build), pass
--use-draft-model 1 and the image passes the baked draft via --model-draft.
Vision (--mmproj) is ALWAYS on in this _vision image.

Create-or-update semantics: if the group already exists it is updated in
place (stop when running -> PATCH image + full env -> start), which keeps
the group's DNS stable for clients. A missing group is created. The
readiness probe is the ~40-minute failure window that tolerates a cold
20.9 GiB download on a new worker: 30 s delay + 20 x 120 s = 2430 s max
(the spec caps: delay 1200, period 120, failure_threshold 20). The 30 s
delay (not 1200) keeps the FIRST probe early, so the gateway — and
cloudflare in front of it — open as soon as the model is actually ready
instead of a fixed 20 min after the container starts.

GPU: RTX 5090 (32 GB) — 20.9 GiB Q6_K + mmproj (0.86 GiB) + q8_0 KV
(~4.6 GiB at the full 132768 ctx, ~1 GiB at the 30000 test ctx) — fits
with wide headroom. A 24 GB card (--gpu rtx3090) does NOT fit this config
(vision is unconditional in this image); the choice is kept as an explicit
escape hatch for reduced-ctx experiments.

Ctx: the default is 30000 — the simple-test setting requested 2026-10-02.
Pass --ctx-size 132768 for the full production context.

Disk: 50 GiB (create path; resources are NOT patchable, an existing group
keeps its resources). The main-model `hf download` can transiently hold ~2x
the file while it lands (~41.8 GiB for the 20.9 GiB Q6_K); 40 GiB was
observed to be too close for comfort, so the default is 50.

CHAT_TEMPLATE is the permissive jinja the image ships (Claude Code's
Anthropic-format requests need system messages accepted anywhere).

SALAD_API_KEY is read from salad_api.txt by salad_client; HF_TOKEN from
hft.txt (validated via whoami-v2; neither is ever printed).

Usage:
    python3 deploy_qwen38_27b.py                     # create-or-update, start
    python3 deploy_qwen38_27b.py --no-start          # apply config only
    python3 deploy_qwen38_27b.py --use-draft-model 1 # gguf without MTP head
"""

import argparse
import os
import sys
import time
import urllib.error
import urllib.request

from salad_client import (
    CreateContainerGroupRequest,
    GpuClassInfo,
    SaladApiError,
    StartContainerGroupRequest,
    StopContainerGroupRequest,
    UpdateContainerGroupRequest,
    create_container_group,
    create_project,
    get_container_group,
    list_gpu_classes,
    start_container_group,
    stop_container_group,
    update_container_group,
)

ORGANIZATION_NAME = "ma-casa-in-paris"
PROJECT_NAME = "qwen38-27b"
# Fresh 2026-10-02 name (the user requested 'qwen38-27b-Q6K'; the API name
# pattern ^[a-z][a-z0-9-]{0,61}[a-z0-9]$ is lowercase-only, hence q6k).
# 'qwen38-27b' itself is stuck in the DELETE name-conflict tombstone.
GROUP_NAME = "qwen38-27b-q6k"
# Served model name (llama-server --alias) — deliberately NOT the group
# name; curl2_salad.sh -m and the Claude Code client reference it.
MODEL_ALIAS = "qwen38-27b"

# Plain tag: the repo NAME is the Salad worker cache key, and this repo is
# fresh (no worker has cached it), so the tag is safe. Manifest-list digest
# of the 2026-10-02 push, for reference / the airtight pin:
#   boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision@sha256:a1ab8bd22b9f
#   d7aef5c00e902744cb3161d4a204c92e6265fe266a332bec51fa
IMAGE = "boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:cuda128-v3"

MODEL_REPO = "JonathanColetti/Qwen3.8-27B-Uncensored-GGUF"
# MTP head embedded in the gguf (qwen35.nextn_predict_layers) — no separate
# draft needed. For a noMTP build use that filename + --use-draft-model 1.
MODEL_FILE = "Qwen3.8-27B-Uncensored-Q6_K.gguf"

HF_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hft.txt")

GPU_CHOICES = ("rtx5090", "rtx3090")

# Readiness failure window for a cold worker: 30 s delay + 20 x 120 s =
# 2430 s (~40 min). The spec caps failure_threshold at 20, period at 120,
# delay at 1200. The delay is kept at 30 (not the 1200 max) so the first
# probe fires early and the API goes live the moment the model is ready,
# not 20 min after the container starts.
READINESS_PROBE = {
    "initial_delay_seconds": 30,
    "period_seconds": 120,
    "timeout_seconds": 1,
    "success_threshold": 1,
    "failure_threshold": 20,
    "http": {"path": "/ready", "port": 8889, "scheme": "http", "headers": []},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create-or-update the canonical qwen38-27b group on the baked q6-mtp-vision image.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--gpu", choices=GPU_CHOICES, default="rtx5090",
                        help="GPU class (rtx5090 is the supported card for this config)")
    parser.add_argument("--image", default=IMAGE, help="Docker image (baked q6-mtp-vision image, tag)")
    parser.add_argument("--disk-size", type=float, default=50.0,
                        help="disk in GiB, CREATE path only (resources are not patchable)")
    parser.add_argument("--memory-size", type=float, default=16.0,
                        help="memory in GB, CREATE path only (resources are not patchable)")
    parser.add_argument("--ctx-size", default="30000",
                        help="env CTX_SIZE (30000 = simple-test default; 132768 = full)")
    parser.add_argument("--use-draft-model", default="none",
                        help="'none' = gguf's embedded MTP head; any other value adds "
                             "--model-draft with the baked draft (noMTP quants)")
    parser.add_argument("--no-start", action="store_true", help="apply config only, do not start")
    return parser.parse_args()


def resolve_gpu_class(choice: str, classes: tuple[GpuClassInfo, ...]) -> GpuClassInfo:
    """Match 'rtx5090' to a class named e.g. 'RTX 5090 (32 GB)' exactly.

    Normalization strips the parenthesized VRAM suffix and lowercases/removes
    spaces, so 'RTX 5090 Laptop (24 GB)' does NOT match choice 'rtx5090'.
    """
    target = choice.lower()
    for gpu_class in classes:
        base = gpu_class.name.split("(")[0].strip().lower().replace(" ", "")
        if base == target:
            return gpu_class
    available = ", ".join(c.name for c in classes)
    raise ValueError(f"GPU class {choice!r} not found among available classes: {available}")


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


def build_env(ctx_size: str, use_draft_model: str, hf_token: str | None) -> dict[str, str]:
    """Full env for the group (create sets it, PATCH replaces it wholesale).

    The baked image drops v3's DRAFT_MODEL_URL / VISION_MODEL_URL: the draft +
    mmproj are baked in, vision is always on. USE_DRAFT_MODEL is the only
    draft knob — 'none' uses the gguf's embedded MTP head, any other value
    makes the CMD pass --model-draft with the baked draft file.
    """
    env = {
        "GPU_ID": "0",
        "MODEL_REPO": MODEL_REPO,
        "MODEL_FILE": MODEL_FILE,
        # Hybrid model (1 in 4 layers is full attention): q8_0 KV is ~4.6 GB
        # at 132768 ctx, ~1 GB at 30000 — fits the 32 GB card on top of the
        # Q6_K weights either way.
        "CTX_SIZE": ctx_size,
        "N_GPU_LAYERS": "99",
        "NAME": GROUP_NAME,
        # Served alias stays 'qwen38-27b' (clients + curl2_salad.sh -m),
        # independent of the group name.
        "MODEL_ALIAS": MODEL_ALIAS,
        # Permissive template the image ships: Claude Code's Anthropic-format
        # /v1/messages requests need system messages accepted anywhere.
        "CHAT_TEMPLATE": "/opt/llama.cpp/qwen3.8.q6.jinja",
        # 'none' = embedded MTP head (this Q6_K build). Any other value ->
        # the image passes --model-draft /models/...draft-Q8_0.gguf.
        "USE_DRAFT_MODEL": use_draft_model,
    }
    if hf_token:
        env["HF_TOKEN"] = hf_token
    return env


def redact(env: dict[str, str]) -> dict[str, str]:
    """Mask secrets for printing (HF_TOKEN is the only secret in the env)."""
    return {k: "<REDACTED>" if k == "HF_TOKEN" else v for k, v in env.items()}


def get_group():
    try:
        return get_container_group(ORGANIZATION_NAME, PROJECT_NAME, GROUP_NAME)
    except SaladApiError as e:
        if e.status_code == 404:
            return None
        raise


def wait_for(status: str, timeout_s: int) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        current = get_container_group(ORGANIZATION_NAME, PROJECT_NAME, GROUP_NAME)
        if current.current_status == status:
            return
        print(f"      status={current.current_status!r} (waiting for {status!r}) ...", flush=True)
        time.sleep(10)
    raise TimeoutError(f"group did not reach {status!r} within {timeout_s} s")


def wait_until_not_pending(timeout_s: int) -> str:
    """Wait out the create-path 'pending' window (start 400s while pending).

    A freshly created group is 'pending' and the API refuses START with
    HTTP 400 'not allowed while in a Pending status' until it settles.
    Returns the status we settled on.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        current = get_container_group(ORGANIZATION_NAME, PROJECT_NAME, GROUP_NAME)
        if current.current_status != "pending":
            return current.current_status
        print(f"      status={current.current_status!r} (waiting for the group to settle) ...", flush=True)
        time.sleep(15)
    raise TimeoutError(f"group was still 'pending' after {timeout_s} s — not started")


def print_group(after) -> str:
    """Print the group's config (env redacted); return the gateway DNS."""
    c = after.raw["container"]
    net = after.raw.get("networking") or {}
    cenv = dict(c.get("environment_variables") or {})
    print(f"      name={after.name!r} status={after.current_status!r}")
    print(f"      image={c['image']!r}")
    print(f"      resources={c['resources']}")
    print(f"      env={redact(cenv)}")
    print(f"      replicas={after.raw['replicas']} restart_policy={after.raw['restart_policy']} "
          f"priority={after.raw.get('priority')} autostart={after.raw.get('autostart_policy')} "
          f"scheduled_scaling={after.raw.get('scheduled-scaling-enabled')}")
    print(f"      readiness_probe={after.raw.get('readiness_probe')}")
    print(f"      networking port={net.get('port')} protocol={net.get('protocol')} "
          f"auth={net.get('auth')} dns={net.get('dns')!r}")
    return net.get("dns")


def main() -> int:
    args = parse_args()

    print(f"[1/5] resolving GPU class {args.gpu!r}")
    gpu = resolve_gpu_class(args.gpu, list_gpu_classes(ORGANIZATION_NAME))
    print(f"      -> {gpu.name!r} ({gpu.id})")

    print("[2/5] loading HF token (validated, never printed)")
    hf_token = load_hf_token()
    print(f"      token {'validated' if hf_token else 'UNAVAILABLE — model download will run unauthenticated'}")
    env = build_env(args.ctx_size, args.use_draft_model, hf_token)

    existing = get_group()
    if existing is not None:
        print(f"[3/5] group exists (status={existing.current_status!r}, "
              f"image={existing.raw['container']['image']!r}) — updating in place "
              f"(DNS stays stable for clients)")
        if not args.no_start and existing.current_status in ("running", "scaling"):
            print("      stopping group before PATCH")
            stop_container_group(StopContainerGroupRequest(
                organization_name=ORGANIZATION_NAME,
                project_name=PROJECT_NAME,
                container_group_name=GROUP_NAME,
            ))
            wait_for("stopped", 120)
            print("      stopped")
        if args.no_start and existing.current_status in ("running", "scaling"):
            print("      NOTE: --no-start while running — the PATCH takes effect on the next start")
        result = update_container_group(
            ORGANIZATION_NAME, PROJECT_NAME, GROUP_NAME,
            UpdateContainerGroupRequest(
                image=args.image,
                gpu_classes=(gpu.id,),
                environment_variables=env,
                readiness_probe=dict(READINESS_PROBE),
            ),
        )
        print(f"      HTTP {result.status_code} {result.reason_phrase} status={result.current_status!r}")
    else:
        print(f"[3/5] group absent — creating (memory={int(round(args.memory_size * 1024))} MB, "
              f"disk={int(round(args.disk_size * 1024**3))} bytes, gpu={gpu.name!r})")
        try:
            proj = create_project(ORGANIZATION_NAME, PROJECT_NAME)
            print(f"      created project via POST -> HTTP {proj.status_code}")
        except SaladApiError as e:
            if e.status_code in (404, 409):
                print(f"      project endpoint HTTP {e.status_code} — using existing project")
            else:
                raise
        request = CreateContainerGroupRequest(
            name=GROUP_NAME,
            display_name=GROUP_NAME,
            autostart_policy=False,
            replicas=1,
            restart_policy="always",
            container_image=args.image,
            command=(),
            environment_variables=env,
            cpu=8,
            memory_mb=int(round(args.memory_size * 1024)),
            gpu_classes=(gpu.id,),
            shm_size=64,
            storage_amount=int(round(args.disk_size * 1024**3)),
            image_caching=True,
            priority="low",
            networking={
                "auth": True,
                "client_request_timeout": 100000,
                "server_response_timeout": 100000,
                "port": 8888,
                "protocol": "http",
                "load_balancer": "round_robin",
                "single_connection_limit": False,
            },
            readiness_probe=dict(READINESS_PROBE),
            scheduled_scaling_enabled=True,
        )
        created = create_container_group(ORGANIZATION_NAME, PROJECT_NAME, request)
        print(f"      HTTP {created.status_code} {created.reason_phrase} "
              f"id={created.id!r} status={created.current_status!r}")

    print("[4/5] verifying group config")
    dns = print_group(get_group())

    if args.no_start:
        print("OK: group config applied (NOT started, per --no-start)")
        return 0

    print("[5/5] starting group")
    # Create path: a fresh group is 'pending' and START 400s until it settles.
    if get_group().current_status == "pending":
        settled = wait_until_not_pending(600)
        print(f"      settled to {settled!r}")
    started = start_container_group(StartContainerGroupRequest(
        organization_name=ORGANIZATION_NAME,
        project_name=PROJECT_NAME,
        container_group_name=GROUP_NAME,
    ))
    print(f"      start -> HTTP {started.status_code} {started.reason_phrase}; dns={dns!r}")
    print("      waiting for running (container up; the model may still be downloading — "
          "a cold worker re-downloads the 20.9 GiB main model, draft + vision are baked)...")
    wait_for("running", 1800)
    print_group(get_group())
    print("OK: group running. 'running' means the container process is up —")
    print(f"      1. verify the LIVE build in-container: version.sh should print")
    print(f"         'lmss q6-mtp-vision-v1 (baked draft+vision) 2026-10-02'; /models")
    print(f"         holds the baked draft + mmproj while the main model hf-downloads")
    print(f"      2. PID1 cmdline: --spec-type draft-mtp --spec-draft-n-max 5 --mmproj "
          f"{'(no --model-draft, USE_DRAFT_MODEL=none)' if args.use_draft_model == 'none' else '--model-draft ...draft-Q8_0.gguf'} "
          f"--alias {MODEL_ALIAS}")
    print(f"      3. gateway answers only after the probe passes (/ready, ~40-min window, first probe at 30 s):")
    print(f"         docker/docker_tests/curl2_salad.sh -url https://{dns} -m {MODEL_ALIAS}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SaladApiError, ValueError, TimeoutError) as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
