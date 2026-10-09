#!/usr/bin/env python3
"""Deploy container group 'qwen38-9b' into project 'qwen38-27b'
(defaults ma-casa-in-paris / qwen38-27b / qwen38-9b; override with
--org / --project / --group).

Replacement for the broken 'qwen9b' group. Serves Qwen3.8-9B (Distill Q4_K_M)
on a single GPU from image boris271142/lmss:cuda128-v3, placed
on ANY of the 7 card classes the old 'qwen9b' group used:

    RTX 3090 (24 GB), RTX 3090 Ti (24 GB), RTX 4090 (24 GB), RTX 4080 (16 GB),
    RTX 5070 Ti (16 GB), RTX 5080 (16 GB), RTX 5090 (32 GB)

gpu_classes semantics: the instance is placed on ANY ONE of these classes
(one card per instance; the allocated card is index 0 inside the container,
hence env GPU_ID=0).

Plain 9B deployment — no MTP draft, no vision mmproj, no custom chat
template. The image implements the `none` sentinel, and per the user's
preference the group env sets it EXPLICITLY rather than relying on the
baked-in defaults (the group env replaces the image env wholesale, so the
explicit `none` also future-proofs against an older image whose defaults
were real URLs): DRAFT_MODEL_URL=none, VISION_MODEL_URL=none,
CHAT_TEMPLATE=none. The env is the 10-key set. (v3 has no DRAFT_MODEL_FILE
— DRAFT_MODEL_URL is the sole draft source in that image.)

Everything else mirrors the live 'qwen9b' group as it was last read
(GET 2026-10-01): cpu 4 (user lowered it from 8 in the UI), memory 16 GB,
disk 25 GB, shm 64, autostart off, scheduled scaling on (placement
priority defaults to 'medium' — the live group ran 'low'; --priority
overrides). image_caching on, restart 'always', replicas 1, gateway
port 8888 (http,
auth, round_robin, 100 s timeouts).

Readiness probe per user spec (matches the live group): HTTP /ready on 8889,
initial_delay 120 s, period 60 s, failure_threshold 20 — i.e. the instance
gets 120 s + 20 x 60 s (~22 min) before being failed, which covers the ~5.4 GB
model download. NO 1200 s initial delay.

MODEL_ALIAS=qwen9b (kept from the old group) so the existing smoke test
curl_salad.sh -url https://<dns> -m qwen9b keeps working against the new DNS.

SALAD_API_KEY is read from deploy/salad_api.txt by salad_client (never printed).

Usage:
    python3 deploy/deploy_qwen38_9b.py
    python3 deploy/deploy_qwen38_9b.py --ctx-size 65536 --disk-size 20
    python3 deploy/deploy_qwen38_9b.py --org akl-on-salad
    python3 deploy/deploy_qwen38_9b.py --project llm --group qwen38-9b-llm
"""

import argparse
import base64
import json
import os
import sys

# salad_client.py lives at the repo root; running this as deploy/deploy_qwen38_9b.py
# puts deploy/ on sys.path, not the root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from salad_client import (
    CreateContainerGroupRequest,
    SaladApiError,
    create_container_group,
    create_project,
    get_container_group,
    list_gpu_classes,
)

# Default org (override with --org): an account can host several orgs, all
# sharing the same Salad API key.
ORGANIZATION_NAME = "ma-casa-in-paris"
PROJECT_NAME = "qwen38-27b"
GROUP_NAME = "qwen38-9b"
# Canonical repo: `lmss`. Do NOT deploy from the old `llama-server-on-salad`
# repo. cuda128-v3 = wget2 first-start downloads + version.sh + build id +
# the `none` sentinel, and URL-only draft handling (DRAFT_MODEL_URL is the
# sole draft source; the old DRAFT_MODEL_FILE/REPO env vars are gone). Caveat: the Salad worker image cache is keyed by
# REPO NAME, not tag or digest — even a renamed repo can serve a stale
# pre-sentinel build on workers that had cached the old one (observed live
# on lmss:cuda128 on 2026-10-01). The only airtight lever is a digest-pinned
# image ref to a digest no worker has seen (this group was PATCHed to
# lmss@sha256:... for exactly that reason); with a plain tag, verify the
# live build with version.sh in the container.
# cuda128-v7 (2026-10-09): watchdog v2.1 — arms on the group /ready endpoint
# (the probe's own, api_app :9999), holds IDLE_ARM_GRACE, then kills by
# POSTing the Salad group /stop (SALAD_STOP_KEY = account-wide key from
# --stop-key, stored b64-obfuscated and decoded at startup) and SIGTERM. A
# bare self-exit gets RESCHEDULED, not stopped (paid test 2026-10-09), so arm
# with --idle-shutdown + --stop-key together; key 'none'/missing/undecodable
# DISABLES the watchdog (no keyless self-exit — it would get rescheduled and
# keep billing).
# On top of cuda128-v5's exec-form entry.sh CMD fix (the v4 inline JSON CMD
# was malformed (stray quote) so buildkit's parseMaybeJSON silently fell back
# to shell form and dash exited 2 (crash-loop ~1-2s after "Running"); v5 runs
# `bash -l /opt/llama.cpp/entry.sh` with llama-server as a trapped child).
# IDLE_SHUTDOWN=none baked = off by default. Tag form: boris271142/lmss:cuda128-v7
#   cuda128-v6: sha256:c1a671ab6e75e11af85203a4ffea9863a0cf7e7059af9f2c37319392ea69dc11
IMAGE = "boris271142/lmss" \
        "@sha256:3db2ab311ed5c43b83f2c612966009524957f5fd3941dfdbaef0b263b3baa35d"

# The 7 card classes the old 'qwen9b' group ran on, by normalized base name
# (parenthesized VRAM suffix stripped, lowercased, spaces removed).
GPU_CLASS_BASES = (
    "rtx3090",
    "rtx3090ti",
    "rtx4090",
    "rtx4080",
    "rtx5070ti",
    "rtx5080",
    "rtx5090",
)

# Mirrored from the live 'qwen9b' group — response-only 'dns' dropped; the
# create spec requires auth/port/protocol when networking is present.
NETWORKING = {
    "auth": True,
    "port": 8888,
    "protocol": "http",
    "load_balancer": "round_robin",
    "client_request_timeout": 100000,
    "server_response_timeout": 100000,
    "single_connection_limit": False,
}

# User-specified: 120 s initial delay, 60 s period, 20 cycles max.
READINESS_PROBE = {
    "http": {"headers": [], "path": "/ready", "port": 8889, "scheme": "http"},
    "failure_threshold": 20,
    "initial_delay_seconds": 120,
    "period_seconds": 60,
    "success_threshold": 1,
    "timeout_seconds": 1,
}


# Idle/heartbeat self-shutdown (the in-container idle_watchdog.py): "none"
# (default) = off, today's behavior; "idle" = kill after IDLE_TIMEOUT s of no
# chat traffic; "heartbeat" = kill after HEARTBEAT_TIMEOUT s of no client
# keepalive pings (run the client with SALAD_HEARTBEAT=1).
IDLE_SHUTDOWN = "none"
IDLE_SHUTDOWN_CHOICES = ("none", "idle", "heartbeat")
# Placement priority (create-only, not PATCHable): Salad places low/batch
# instances best-effort and evicts them as soon as higher-priority work
# lands — observed live 2026-10-09: a 'low' group churned through 9
# placements in ~21 min while 372 'high' vs 5 'low' GPUs of its exact
# profile were available. medium is the floor for a group expected to
# actually run; high for latency-critical deployments.
PRIORITY = "medium"
PRIORITY_CHOICES = ("high", "medium", "low", "batch")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create container group qwen38-9b (Qwen3.8-9B, no draft/vision/template).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--org", default=ORGANIZATION_NAME,
                        help="Salad organization to deploy into (an account can host several "
                             "orgs sharing one API key)")
    parser.add_argument("--project", default=PROJECT_NAME,
                        help="Salad project to deploy into (projects must exist — the API has "
                             "no project-create endpoint, create it in the web UI)")
    parser.add_argument("--group", default=GROUP_NAME,
                        help="container group name (also used as the NAME env)")
    parser.add_argument("--image", default=IMAGE, help="Docker image to deploy")
    parser.add_argument("--disk-size", type=float, default=25.0,
                        help="Disk space to allocate, in GB (sent as storage_amount bytes)")
    parser.add_argument("--memory-size", type=float, default=16.0,
                        help="Memory to allocate, in GB (sent as memory MB)")
    parser.add_argument("--model-repo", default="empero-ai/Qwen3.8-9B-Distill-GGUF", help="env MODEL_REPO")
    parser.add_argument("--model-file", default="Qwen3.8-9B-Q4_K_M.gguf", help="env MODEL_FILE")
    parser.add_argument("--model-alias", default="qwen9b",
                        help='env MODEL_ALIAS (the name the model registers under; kept "qwen9b" '
                             "so the existing curl_salad.sh -m qwen9b smoke test keeps working)")
    parser.add_argument("--ctx-size", default="32768", help="env CTX_SIZE")
    parser.add_argument("--n-gpu-layers", default="99", help="env N_GPU_LAYERS")
    parser.add_argument("--gpu-id", default="0",
                        help="env GPU_ID (SaladCloud exposes one card per container -> index 0)")
    parser.add_argument("--idle-shutdown", choices=IDLE_SHUTDOWN_CHOICES, default=IDLE_SHUTDOWN,
                        help="env IDLE_SHUTDOWN — in-container watchdog mode: none (off), "
                             "idle (self-stop after --idle-timeout s with no chat traffic), "
                             "heartbeat (self-stop after --heartbeat-timeout s with no client "
                             "keepalive pings — run cl_salad/chat_salad.sh with "
                             "SALAD_HEARTBEAT=1). Needs a watchdog-capable image "
                             "(version.sh prints the WATCHDOG generation). The watchdog arms "
                             "on the group's /ready endpoint and kills by POSTing the group "
                             "/stop endpoint — pass --stop-key (account-wide key): "
                             "self-exit alone does NOT stop a group (Salad reschedules a new "
                             "instance — paid test 2026-10-09), so arming without --stop-key "
                             "bills. Armed mode also sets restart_policy=never (this deployer "
                             "is create-only, so the policy is set at creation)")
    parser.add_argument("--idle-timeout", default="600",
                        help="env IDLE_TIMEOUT — seconds of flat token counters on /metrics "
                             "(mode idle) before the watchdog SIGTERMs llama-server and the "
                             "container exits")
    parser.add_argument("--heartbeat-timeout", default="600",
                        help="env HEARTBEAT_TIMEOUT — seconds without a client keepalive call "
                             "(mode heartbeat) before the watchdog kills the container. The "
                             "client pinger fires every ~30 s, so keep this at ~2x the pinger "
                             "period or more (60 s minimum) or pinger jitter can false-kill")
    parser.add_argument("--stop-key", default=None, metavar="FILE",
                        help="file holding the Salad API key for the watchdog's group /stop "
                             "call (Salad keys are per-user and account-wide — no group-"
                             "scoped keys exist). Stored in group env as SALAD_STOP_KEY="
                             "b64:<base64> (basic obfuscation, not encryption: keeps the "
                             "plaintext out of the env visible via API GET; the watchdog "
                             "decodes it). The key is never printed. Without it the watchdog "
                             "DISABLES itself (no self-shutdown) — do not arm --idle-shutdown "
                             "without it")
    parser.add_argument("--priority", choices=PRIORITY_CHOICES, default=PRIORITY,
                        help="placement priority (create-only): high > medium > low > batch. "
                             "Default medium — low/batch instances are placed best-effort and "
                             "evicted whenever higher-priority work lands (2026-10-09: a 'low' "
                             "group churned 9 placements in ~21 min while 372 high vs 5 low "
                             "GPUs were available). high for latency-critical; batch only for "
                             "near-free tolerant placement")
    return parser.parse_args()


def resolve_gpu_classes(org: str) -> list[str]:
    """Return the UUIDs of all 7 GPU classes, matched by exact normalized base name."""
    classes = list_gpu_classes(org)
    by_base = {}
    for gpu_class in classes:
        base = gpu_class.name.split("(")[0].strip().lower().replace(" ", "")
        by_base.setdefault(base, []).append(gpu_class)
    resolved = []
    missing = []
    for base in GPU_CLASS_BASES:
        matches = by_base.get(base)
        if not matches:
            missing.append(base)
        else:
            for gpu_class in matches:
                print(f"      {gpu_class.name} -> {gpu_class.id}")
                resolved.append(gpu_class.id)
    if missing:
        available = ", ".join(c.name for c in classes)
        raise ValueError(f"GPU class base(s) {missing!r} not found among available classes: {available}")
    if len(resolved) != len(GPU_CLASS_BASES):
        raise ValueError(f"expected {len(GPU_CLASS_BASES)} gpu classes, resolved {len(resolved)}")
    return resolved


def project_exists(org: str, project: str) -> bool:
    status, _reason, _headers, _body = _http_get(
        f"/organizations/{org}/projects/{project}/containers"
    )
    return status == 200


def _http_get(path: str):
    from salad_client import _http, load_api_key

    return _http("GET", path, load_api_key())


def redact(env: dict[str, str]) -> dict[str, str]:
    """Mask secrets for printing (SALAD_STOP_KEY is a secret)."""
    return {k: "<REDACTED>" if k == "SALAD_STOP_KEY" else v for k, v in env.items()}


def main() -> int:
    args = parse_args()
    org = args.org
    project = args.project
    group = args.group
    stop_key = "none"
    if args.stop_key:
        try:
            with open(args.stop_key) as f:
                stop_key = f.read().strip()
        except OSError as e:
            print(f"      cannot read --stop-key file {args.stop_key!r}: {e}")
            return 2
        if not stop_key:
            print(f"      --stop-key file {args.stop_key!r} is empty")
            return 2
        # Basic obfuscation: store the key in group env as b64:<base64> so a GET
        # on the group doesn't expose the plaintext (idle_watchdog decodes the
        # sentinel; NOT encryption — the env itself is the exposure surface).
        stop_key = "b64:" + base64.b64encode(stop_key.encode()).decode()

    print(f"[1/5] resolving GPU classes {GPU_CLASS_BASES} in org {org!r}")
    gpu_uuids = resolve_gpu_classes(org)

    print(f"[2/5] ensuring project {project!r}")
    if project_exists(org, project):
        print("      project exists — nothing to create")
    else:
        try:
            proj = create_project(org, project)
            print(f"      created via POST /organizations/{{org}}/projects -> HTTP {proj.status_code}")
        except SaladApiError as e:
            if e.status_code == 404:
                print("      no create-project endpoint (HTTP 404, undocumented) — "
                      "relying on container-group creation to auto-create the project")
            else:
                raise

    print(f"[3/5] checking {group!r} does not already exist")
    try:
        existing = get_container_group(org, project, group)
    except SaladApiError as e:
        if e.status_code != 404:
            raise
        print("      absent (HTTP 404 as expected)")
    else:
        print(f"FAIL: group {group!r} already exists in {project!r} "
              f"(status={existing.current_status!r}) — refusing to create a duplicate",
              file=sys.stderr)
        return 1

    # Plain 9B deployment: model + sizing, and the three optional features
    # EXPLICITLY disabled with the `none` sentinel (user's way — don't rely
    # on the image's baked-in defaults; the group env replaces the image env
    # wholesale, so this also protects against an older image with real
    # default URLs).
    env: dict[str, str] = {
        "GPU_ID": args.gpu_id,
        "MODEL_REPO": args.model_repo,
        "MODEL_FILE": args.model_file,
        "MODEL_ALIAS": args.model_alias,
        "CTX_SIZE": args.ctx_size,
        "N_GPU_LAYERS": args.n_gpu_layers,
        "NAME": group,
        "DRAFT_MODEL_URL": "none",
        "VISION_MODEL_URL": "none",
        "CHAT_TEMPLATE": "none",
        # Idle/heartbeat self-shutdown (in-container idle_watchdog.py):
        # 'none' = off; 'idle'/'heartbeat' arm the watchdog (see --idle-shutdown).
        # Harmless on images that predate the watchdog (the env is unread).
        "IDLE_SHUTDOWN": args.idle_shutdown,
        "IDLE_TIMEOUT": args.idle_timeout,
        "HEARTBEAT_TIMEOUT": args.heartbeat_timeout,
        # Group /stop kill (watchdog v2): the ONLY action that truly stops a
        # group is the Salad group /stop endpoint — self-exit gets RESCHEDULED
        # (paid test 2026-10-09). SALAD_STOP_KEY = account-wide key from
        # --stop-key, stored obfuscated as b64:<base64> (basic obfuscation, not
        # encryption; the watchdog decodes it). 'none' = watchdog DISABLES
        # itself — no self-shutdown; do not arm without a key. Path components
        # ride along; never empty (the API rejects empty env values).
        "SALAD_STOP_KEY": stop_key,
        "SALAD_ORG": org,
        "SALAD_PROJECT": project,
        "SALAD_GROUP": group,
    }
    memory_mb = int(round(args.memory_size * 1024))
    storage_amount = int(round(args.disk_size * 1024**3))
    request = CreateContainerGroupRequest(
        name=group,
        display_name=group,
        autostart_policy=False,
        replicas=1,
        # 'never' when the watchdog is armed: the watchdog's real kill is
        # the group /stop POST (--stop-key), and 'never' additionally
        # stops crash-loop billing on any self-exit — self-exit alone
        # gets RESCHEDULED, not stopped (paid test 2026-10-09).
        restart_policy="never" if args.idle_shutdown != "none" else "always",
        container_image=args.image,
        environment_variables=env,
        cpu=4,
        memory_mb=memory_mb,
        gpu_classes=tuple(gpu_uuids),
        shm_size=64,
        storage_amount=storage_amount,
        image_caching=True,
        priority=args.priority,
        networking=NETWORKING,
        readiness_probe=READINESS_PROBE,
        scheduled_scaling_enabled=True,
    )
    print(f"[4/5] creating container group {group!r} in project {project!r}")
    print(f"      image={args.image!r} replicas=1 cpu=4 memory={memory_mb} MB "
          f"disk={storage_amount} bytes shm=64 MB priority={args.priority} "
          f"gpu_classes={len(gpu_uuids)}")
    print(f"      env={json.dumps(redact(env))}")
    print(f"      readiness_probe={json.dumps(READINESS_PROBE)}")
    result = create_container_group(org, project, request)
    print(f"      HTTP {result.status_code} {result.reason_phrase} "
          f"id={result.id!r} status={result.current_status!r} location={result.location!r}")

    print("[5/5] verifying created group")
    after = get_container_group(org, project, result.name)
    c = after.raw["container"]
    net = after.raw.get("networking") or {}
    r = c["resources"]
    print(f"      name={after.name!r} display={after.raw.get('display_name')!r} "
          f"status={after.current_status!r}")
    print(f"      image={c['image']!r} gpu_classes={r['gpu_classes']} "
          f"cpu={r['cpu']} memory={r['memory']} MB storage={r['storage_amount']} B "
          f"shm={r['shm_size']}")
    print(f"      env={redact(c.get('environment_variables') or {})}")
    print(f"      replicas={after.raw['replicas']} restart_policy={after.raw['restart_policy']} "
          f"priority={after.raw.get('priority')} autostart={after.raw.get('autostart_policy')} "
          f"scheduled_scaling={after.raw.get('scheduled-scaling-enabled')} "
          f"image_caching={c.get('image_caching')}")
    print(f"      readiness_probe={after.raw.get('readiness_probe')}")
    print(f"      networking port={net.get('port')} protocol={net.get('protocol')} "
          f"auth={net.get('auth')} dns={net.get('dns')!r}")

    print(f"OK: created group {result.name!r} in {org}/{project} "
          f"on {len(gpu_uuids)} gpu classes.")
    print(f"NOTE: it was created in the STOPPED state (autostart off + scheduled scaling),")
    print(f"      so it has 0 instances until started. To run it now:")
    print(f"        from salad_client import StartContainerGroupRequest, start_container_group")
    print(f"        start_container_group(StartContainerGroupRequest('{org}','{project}','{group}'))")
    print(f"      Once it has a live instance, smoke-test with:")
    print(f"      claude/curl_salad.sh -url https://{net.get('dns')} -m {args.model_alias}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SaladApiError, ValueError) as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
