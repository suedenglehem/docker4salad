"""Deploy container group 'qwen38-27b' into new project 'qwen38-27b' (org ma-casa-in-paris).

The script fetches the existing group 'qwen9bter' (project qwen9b-ter) LIVE and
mirrors its configuration — image, cpu/shm resources, networking (Container
Gateway config), readiness probe, replicas, restart policy, priority,
autostart/scheduled scaling — so everything stays identical to qwen9bter by
construction. Three things are parameterized:

  * --gpu {rtx3090,rtx5090} : GPU class, resolved live via list_gpu_classes
    ('RTX 3090 (24 GB)' / 'RTX 5090 (32 GB)')
  * --disk-size / --memory-size : disk space and memory allocated to the new
    group, given in GB on the CLI (defaults: 25 GB disk, 16 GB memory); sent as
    storage_amount bytes / memory MB per the OpenAPI spec
  * the six container environment variables, overridable via CLI flags
    (defaults = Qwen3.8-27B values)

Project creation note: no create-project operation exists in the OpenAPI spec
(v0.9.0-alpha.17). The script tries POST /organizations/{org}/projects (probed,
undocumented endpoint); if that is absent it relies on container-group creation
to auto-create the project and reports clearly either way.

SALAD_API_KEY is read from salad_api.txt by salad_client (never printed).

Usage:
    python3 deploy_qwen38_27b.py --gpu rtx3090
    python3 deploy_qwen38_27b.py --gpu rtx5090 --disk-size 40 --memory-size 24 \
        --ctx-size 16384 --n-gpu-layers 40
"""

import argparse
import sys
from dataclasses import replace

from salad_client import (
    ContainerGroupInfo,
    CreateContainerGroupRequest,
    GpuClassInfo,
    SaladApiError,
    create_container_group,
    create_project,
    get_container_group,
    list_gpu_classes,
)

ORGANIZATION_NAME = "ma-casa-in-paris"
SOURCE_PROJECT = "qwen9b-ter"
SOURCE_GROUP = "qwen9bter"
NEW_PROJECT = "qwen38-27b"
GROUP_NAMES = ("qwen38-27b", "qwen38_27b")  # fallback if the hyphenated name is rejected

GPU_CHOICES = ("rtx3090", "rtx5090")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create project + container group mirroring qwen9bter.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--gpu", required=True, choices=GPU_CHOICES, help="GPU class to deploy on")
    # Resource allocation for the new group (in GB; converted per spec: memory MB / storage bytes)
    parser.add_argument("--disk-size", type=float, default=25.0,
                        help="Disk space to allocate, in GB (sent as storage_amount bytes)")
    parser.add_argument("--memory-size", type=float, default=16.0,
                        help="Memory to allocate, in GB (sent as memory MB)")
    # The six environment variables of the container group (defaults = Qwen3.8-27B values)
    parser.add_argument("--gpu-id", default="1", help='env GPU_ID')
    parser.add_argument("--model-repo", default="bartowski/Qwen3.8-27B-GGUF", help="env MODEL_REPO")
    parser.add_argument("--model-file", default="Qwen3.8-27B-Q4_K_M.gguf", help="env MODEL_FILE")
    parser.add_argument("--ctx-size", default="132768", help="env CTX_SIZE")
    parser.add_argument("--n-gpu-layers", default="99", help="env N_GPU_LAYERS")
    parser.add_argument("--server-name", default="qwen38-27b", help='env NAME')
    return parser.parse_args()


def resolve_gpu_class(choice: str, classes: tuple[GpuClassInfo, ...]) -> GpuClassInfo:
    """Match a choice like 'rtx5090' to a class named e.g. 'RTX 5090 (32 GB)'.

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


def build_request(
    source: ContainerGroupInfo,
    gpu_uuid: str,
    env: dict[str, str],
    group_name: str,
    memory_mb: int,
    storage_amount: int,
) -> CreateContainerGroupRequest:
    """Map qwen9bter's live GET response onto a ContainerGroupPrototype create request.

    cpu and shm_size are mirrored from the source; memory (MB) and disk
    (storage_amount, bytes) come from --memory-size / --disk-size.

    Field mapping notes (spec-verified):
      * `priority` is top-level in the GET response but container-level in the
        create request;
      * networking `dns` is response-only and must be dropped;
      * country_codes/scaling-actions are empty on qwen9bter -> omitted (any
        country, no scaling actions), per spec semantics.
    """
    raw = source.raw
    container = raw["container"]
    resources = container["resources"]
    networking = {k: v for k, v in (raw.get("networking") or {}).items() if k != "dns"}
    return CreateContainerGroupRequest(
        name=group_name,
        display_name=group_name,
        autostart_policy=bool(raw.get("autostart_policy", False)),
        replicas=int(raw["replicas"]),
        restart_policy=str(raw["restart_policy"]),
        container_image=str(container["image"]),
        command=tuple(container.get("command") or []),
        environment_variables=dict(env),
        cpu=int(resources["cpu"]),
        memory_mb=memory_mb,
        gpu_classes=(gpu_uuid,),
        shm_size=resources.get("shm_size"),
        storage_amount=storage_amount,
        image_caching=container.get("image_caching"),
        priority=raw.get("priority"),
        networking=networking or None,
        readiness_probe=raw.get("readiness_probe"),
        scheduled_scaling_enabled=raw.get("scheduled-scaling-enabled"),
    )


def main() -> int:
    args = parse_args()

    print(f"[1/5] reading source group {SOURCE_GROUP} ({ORGANIZATION_NAME}/{SOURCE_PROJECT})")
    source = get_container_group(ORGANIZATION_NAME, SOURCE_PROJECT, SOURCE_GROUP)
    print(f"      status={source.current_status!r} image={source.raw['container']['image']!r}")

    print("[2/5] resolving GPU class")
    classes = list_gpu_classes(ORGANIZATION_NAME)
    gpu = resolve_gpu_class(args.gpu, classes)
    print(f"      {args.gpu} -> {gpu.name!r} ({gpu.id})")

    env = {
        "GPU_ID": args.gpu_id,
        "MODEL_REPO": args.model_repo,
        "MODEL_FILE": args.model_file,
        "CTX_SIZE": args.ctx_size,
        "N_GPU_LAYERS": args.n_gpu_layers,
        "NAME": args.server_name,
    }

    print(f"[3/5] ensuring project {NEW_PROJECT!r}")
    try:
        proj = create_project(ORGANIZATION_NAME, NEW_PROJECT)
        print(f"      created via POST /organizations/{{org}}/projects -> HTTP {proj.status_code}")
    except SaladApiError as e:
        if e.status_code == 404:
            print("      no create-project endpoint (HTTP 404, undocumented) — "
                  "relying on container-group creation to auto-create the project")
        else:
            raise

    memory_mb = int(round(args.memory_size * 1024))
    storage_amount = int(round(args.disk_size * 1024**3))
    request = build_request(
        source, gpu.id, env, GROUP_NAMES[0],
        memory_mb=memory_mb, storage_amount=storage_amount,
    )
    print(f"[4/5] creating container group {request.name!r} in project {NEW_PROJECT!r} "
          f"(memory={memory_mb} MB, disk={storage_amount} bytes)")
    try:
        result = create_container_group(ORGANIZATION_NAME, NEW_PROJECT, request)
    except SaladApiError as e:
        detail = (e.problem.detail if e.problem else "") or ""
        if "name" in detail.lower():
            fallback = GROUP_NAMES[1]
            print(f"      name rejected ({detail!r}) — retrying with fallback name {fallback!r}")
            request = replace(request, name=fallback, relaxed_name=True)
            result = create_container_group(ORGANIZATION_NAME, NEW_PROJECT, request)
        else:
            raise
    print(f"      HTTP {result.status_code} {result.reason_phrase} "
          f"id={result.id!r} status={result.current_status!r} location={result.location!r}")

    print("[5/5] verifying created group")
    after = get_container_group(ORGANIZATION_NAME, NEW_PROJECT, result.name)
    c = after.raw["container"]
    net = after.raw.get("networking") or {}
    print(f"      name={after.name!r} status={after.current_status!r}")
    print(f"      image={c['image']!r} gpu_classes={c['resources']['gpu_classes']}")
    print(f"      env={c.get('environment_variables')}")
    print(f"      replicas={after.raw['replicas']} restart_policy={after.raw['restart_policy']} "
          f"priority={after.raw.get('priority')} autostart={after.raw.get('autostart_policy')} "
          f"scheduled_scaling={after.raw.get('scheduled-scaling-enabled')}")
    print(f"      readiness_probe={after.raw.get('readiness_probe')}")
    print(f"      networking port={net.get('port')} protocol={net.get('protocol')} "
          f"auth={net.get('auth')} dns={net.get('dns')!r}")

    print(f"OK: created group {result.name!r} in {ORGANIZATION_NAME}/{NEW_PROJECT} on {gpu.name}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SaladApiError, ValueError) as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)

