"""Deploy container group 'qwen38-27b-rtx5090' into project 'qwen38-27b' (org ma-casa-in-paris).

Serves Qwen3.8-27B (Uncensored noMTP Q4_K_M + MTP draft speculative decoding +
vision mmproj) on a single RTX 5090 (32 GB) from image
boris271142/llama-server-on-salad:cuda128-v2 — the CUDA 12.8 build, required
for Blackwell (RTX 5090, sm_120). cuda128-v2 = cuda128 + wget first-start
downloads for DRAFT_MODEL_URL / VISION_MODEL_URL + q8_0 KV cache. The bare
cuda128 tag is STALE on Salad workers (image_caching serves the old digest:
ngram spec, q4_0 KV, no draft download) — always pin -v2 or newer.

Every value below was verified before deployment:

  * Networking / readiness probe / priority / restart / autostart /
    image_caching / scheduled scaling mirror the live group 'qwen38-27b' in
    the same project (GET on 2026-09-30): gateway port 8888 (protocol http,
    auth true, round_robin, 100 s timeouts), readiness probe HTTP /ready on
    port 8889 with 120 s initial delay, priority 'batch', restart 'always',
    autostart off, scheduled scaling on.
  * IPv6: no such field exists in the OpenAPI spec (grep-verified). The
    image's CMD hardcodes the listeners — socat TCP6-LISTEN:8888 ->
    llama-server and TCP6-LISTEN:8889 -> status API — i.e. dual-stack IPv6
    binds, so the gateway is IPv6-capable by construction.
  * Env vars sized for the 32 GB card: noMTP Q4_K_M weights ~15.4 GB + MTP
    draft Q8_0 ~3 GB + mmproj F16 ~1 GB + ~132k q8_0 KV cache ~4.6 GB (only
    16 of 64 layers are full-attention; see Dockerfile.multistage) -> ~24 GB,
    comfortable headroom. N_GPU_LAYERS=99 (full offload). The MTP draft is
    fetched on first start from DRAFT_MODEL_URL (full URL, wget) or, when
    that is empty, DRAFT_MODEL_FILE + MODEL_REPO (hf, repo+file); both empty
    disables the draft (server falls back to n-gram self-speculation).
    VISION_MODEL_URL (full URL, wget) adds the mmproj projector for vision;
    empty disables it. CHAT_TEMPLATE points at the permissive jinja the image
    ships at /opt/llama.cpp/qwen3.8.q6.jinja (empty -> the model's embedded
    template); the permissive one is required for Claude Code /v1/messages.
  * GPU_ID=0: SaladCloud supports one GPU per container
    (container-engine/docker-run.mdx), so the allocated card is index 0
    inside the container. (The old 'qwen38-27b' group's GPU_ID=1 was a
    mirror artifact from the local box's card layout; that group never ran.)
  * RTX 5090 GPU class UUID resolved live via list_gpu_classes (exact match
    'RTX 5090 (32 GB)', never the 'RTX 5090 Laptop' class).
  * Project 'qwen38-27b' already exists (verified via list_container_groups);
    creation is only attempted as a 404-tolerated fallback.

SALAD_API_KEY is read from salad_api.txt by salad_client (never printed).

Usage:
    python3 deploy_qwen38_27b_rtx5090.py
    python3 deploy_qwen38_27b_rtx5090.py --ctx-size 131072 --memory-size 16
"""

import argparse
import json
import sys

from salad_client import (
    CreateContainerGroupRequest,
    SaladApiError,
    create_container_group,
    create_project,
    get_container_group,
    list_gpu_classes,
)

ORGANIZATION_NAME = "ma-casa-in-paris"
PROJECT_NAME = "qwen38-27b"
GROUP_NAME = "qwen38-27b-rtx5090"
IMAGE = "boris271142/llama-server-on-salad:cuda128-v2"
GPU_CLASS_BASE = "rtx5090"  # must match 'RTX 5090 (32 GB)', not the Laptop class

# Mirrored from the live 'qwen38-27b' group (GET, 2026-09-30) — response-only
# 'dns' dropped; spec requires auth/port/protocol when networking is present.
NETWORKING = {
    "auth": True,
    "port": 8888,
    "protocol": "http",
    "load_balancer": "round_robin",
    "client_request_timeout": 100000,
    "server_response_timeout": 100000,
    "single_connection_limit": False,
}

# Mirrored from the live group; all five timing fields are required by spec.
READINESS_PROBE = {
    "http": {"headers": [], "path": "/ready", "port": 8889, "scheme": "http"},
    "failure_threshold": 10,
    "initial_delay_seconds": 120,
    "period_seconds": 5,
    "success_threshold": 1,
    "timeout_seconds": 1,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create container group qwen38-27b-rtx5090 (Qwen3.8-27B on RTX 5090).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--image", default=IMAGE, help="Docker image to deploy")
    parser.add_argument("--disk-size", type=float, default=30.0,
                        help="Disk space to allocate, in GB (sent as storage_amount bytes)")
    parser.add_argument("--memory-size", type=float, default=16.0,
                        help="Memory to allocate, in GB (sent as memory MB)")
    parser.add_argument("--model-repo", default="JonathanColetti/Qwen3.8-27B-Uncensored-GGUF", help="env MODEL_REPO")
    parser.add_argument("--model-file", default="Qwen3.8-27B-Uncensored-noMTP-Q4_K_M.gguf", help="env MODEL_FILE")
    parser.add_argument("--draft-model-url",
                        default="https://huggingface.co/JonathanColetti/Qwen3.8-27B-Uncensored-GGUF/resolve/main/Qwen3.8-27B-Uncensored-draft-Q8_0.gguf",
                        help="env DRAFT_MODEL_URL (full URL, wget; takes precedence over --draft-model-file)")
    parser.add_argument("--draft-model-file", default="Qwen3.8-27B-Uncensored-draft-Q8_0.gguf",
                        help="env DRAFT_MODEL_FILE (repo+file fallback when DRAFT_MODEL_URL is empty)")
    parser.add_argument("--vision-model-url",
                        default="https://huggingface.co/JonathanColetti/Qwen3.8-27B-Uncensored-GGUF/resolve/main/mmproj-Qwen3.8-27B-Uncensored-F16.gguf",
                        help="env VISION_MODEL_URL (full URL of the mmproj projector, wget; empty disables vision)")
    parser.add_argument("--chat-template", default="/opt/llama.cpp/qwen3.8.q6.jinja",
                        help="env CHAT_TEMPLATE (permissive jinja the image ships; empty uses the model's embedded template)")
    parser.add_argument("--ctx-size", default="132768", help="env CTX_SIZE")
    return parser.parse_args()


def resolve_rtx5090() -> str:
    """Return the UUID of the GPU class whose base name is exactly 'rtx5090'.

    Normalization strips the parenthesized VRAM suffix, lowercases and removes
    spaces, so 'RTX 5090 Laptop (24 GB)' does NOT match.
    """
    for gpu_class in list_gpu_classes(ORGANIZATION_NAME):
        base = gpu_class.name.split("(")[0].strip().lower().replace(" ", "")
        if base == GPU_CLASS_BASE:
            return gpu_class.id
    available = ", ".join(c.name for c in list_gpu_classes(ORGANIZATION_NAME))
    raise ValueError(f"GPU class {GPU_CLASS_BASE!r} not found among available classes: {available}")


def project_exists() -> bool:
    status, _reason, _headers, body = _http_get(
        f"/organizations/{ORGANIZATION_NAME}/projects/{PROJECT_NAME}/containers"
    )
    return status == 200


def _http_get(path: str):
    from salad_client import _http, load_api_key

    return _http("GET", path, load_api_key())


def main() -> int:
    args = parse_args()

    print(f"[1/5] resolving GPU class {GPU_CLASS_BASE!r}")
    gpu_uuid = resolve_rtx5090()
    print(f"      {GPU_CLASS_BASE} -> {gpu_uuid}")

    print(f"[2/5] ensuring project {PROJECT_NAME!r}")
    if project_exists():
        print("      project exists — nothing to create")
    else:
        try:
            proj = create_project(ORGANIZATION_NAME, PROJECT_NAME)
            print(f"      created via POST /organizations/{{org}}/projects -> HTTP {proj.status_code}")
        except SaladApiError as e:
            if e.status_code == 404:
                print("      no create-project endpoint (HTTP 404, undocumented) — "
                      "relying on container-group creation to auto-create the project")
            else:
                raise

    print(f"[3/5] checking {GROUP_NAME!r} does not already exist")
    try:
        existing = get_container_group(ORGANIZATION_NAME, PROJECT_NAME, GROUP_NAME)
    except SaladApiError as e:
        if e.status_code != 404:
            raise
        print("      absent (HTTP 404 as expected)")
    else:
        print(f"FAIL: group {GROUP_NAME!r} already exists in {PROJECT_NAME!r} "
              f"(status={existing.current_status!r}) — refusing to create a duplicate",
              file=sys.stderr)
        return 1

    env: dict[str, str] = {
        "GPU_ID": "0",
        "MODEL_REPO": args.model_repo,
        "MODEL_FILE": args.model_file,
        "DRAFT_MODEL_FILE": args.draft_model_file,
        "CTX_SIZE": args.ctx_size,
        "N_GPU_LAYERS": "99",
        "NAME": GROUP_NAME,
    }
    # URL envs are optional: the spec rejects empty env values, so an empty
    # flag leaves the variable unset (the image's ENV default then applies).
    if args.draft_model_url:
        env["DRAFT_MODEL_URL"] = args.draft_model_url
    if args.vision_model_url:
        env["VISION_MODEL_URL"] = args.vision_model_url
    if args.chat_template:
        env["CHAT_TEMPLATE"] = args.chat_template
    memory_mb = int(round(args.memory_size * 1024))
    storage_amount = int(round(args.disk_size * 1024**3))
    request = CreateContainerGroupRequest(
        name=GROUP_NAME,
        display_name=GROUP_NAME,
        autostart_policy=False,
        replicas=1,
        restart_policy="always",
        container_image=args.image,
        environment_variables=env,
        cpu=8,
        memory_mb=memory_mb,
        gpu_classes=(gpu_uuid,),
        shm_size=64,
        storage_amount=storage_amount,
        image_caching=True,
        priority="batch",
        networking=NETWORKING,
        readiness_probe=READINESS_PROBE,
        scheduled_scaling_enabled=True,
    )
    print(f"[4/5] creating container group {GROUP_NAME!r} in project {PROJECT_NAME!r}")
    print(f"      image={args.image!r} replicas=1 cpu=8 memory={memory_mb} MB "
          f"disk={storage_amount} bytes shm={64} MB")
    print(f"      env={json.dumps(env)}")
    result = create_container_group(ORGANIZATION_NAME, PROJECT_NAME, request)
    print(f"      HTTP {result.status_code} {result.reason_phrase} "
          f"id={result.id!r} status={result.current_status!r} location={result.location!r}")

    print("[5/5] verifying created group")
    after = get_container_group(ORGANIZATION_NAME, PROJECT_NAME, result.name)
    c = after.raw["container"]
    net = after.raw.get("networking") or {}
    r = c["resources"]
    print(f"      name={after.name!r} display={after.raw.get('display_name')!r} "
          f"status={after.current_status!r}")
    print(f"      image={c['image']!r} gpu_classes={r['gpu_classes']} "
          f"cpu={r['cpu']} memory={r['memory']} MB storage={r['storage_amount']} B "
          f"shm={r['shm_size']}")
    print(f"      env={c.get('environment_variables')}")
    print(f"      replicas={after.raw['replicas']} restart_policy={after.raw['restart_policy']} "
          f"priority={after.raw.get('priority')} autostart={after.raw.get('autostart_policy')} "
          f"scheduled_scaling={after.raw.get('scheduled-scaling-enabled')} "
          f"image_caching={c.get('image_caching')}")
    print(f"      readiness_probe={after.raw.get('readiness_probe')}")
    print(f"      networking port={net.get('port')} protocol={net.get('protocol')} "
          f"auth={net.get('auth')} dns={net.get('dns')!r}")

    print(f"OK: created group {result.name!r} in {ORGANIZATION_NAME}/{PROJECT_NAME} "
          f"on {GPU_CLASS_BASE}; start it manually from the Portal or via start_container_group")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SaladApiError, ValueError) as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
