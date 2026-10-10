#!/usr/bin/env python3
"""Deploy container group 'qwen9b' into project 'qwen38-27b'
(defaults ma-casa-in-paris / qwen38-27b / qwen9b; override with
--org / --project / --group).

Serves Qwen3.8-9B (Distill Q4_K_M) on a single RTX 3090 (24 GB) from image
boris271142/lmss:cuda128-v3 (canonical repo; the old llama-server-on-salad
repo is frozen — the Salad worker cache serves a stale digest there by repo
name).

The 9B is deliberately a *plain* deployment — no MTP draft, no vision mmproj,
no custom chat template — and the image's own defaults make that the default
behavior. cuda128-v3 implements the `none` sentinel: its baked-in defaults for
DRAFT_MODEL_URL / VISION_MODEL_URL / CHAT_TEMPLATE are all `none`, so the CMD
skips each feature (no download, no flag) unless a group explicitly opts in.
A plain deployment therefore needs no env vars at all.

(Tag history: the OLD repo's llama-server-on-salad:cuda128-v2 baked the 27B's
MTP draft in as the DEFAULT DRAFT_MODEL_URL / DRAFT_MODEL_FILE with no way to
switch it off from a container group — the Salad API (and this client) reject
empty env values (spec minLength 1, see salad_client.CreateContainerGroupRequest).
Its cuda128 predates draft/vision/template handling entirely. The canonical
lmss repo's cuda128-v3 is the sentinel build: wget2 first-start downloads,
the `none` sentinel, URL-only draft handling (DRAFT_MODEL_URL is the sole
draft source — DRAFT_MODEL_FILE/REPO are gone), and
/usr/local/bin/version.sh for live verification.)

  Every other value below mirrors the live 'qwen38-27b-rtx5090' group in
  project 'qwen38-27b' (GET 2026-09-30): gateway port 8888 (http, auth true,
  round_robin, 100 s timeouts), readiness probe HTTP /ready on 8889 with
  120 s initial delay, restart 'always', autostart off, scheduled
  scaling on; placement priority defaults to 'medium' (the live group
  ran 'batch'; --priority overrides). The only differences are the model
  env, the card
  (RTX 3090, not 5090), and the image tag.

Env sized for the 24 GB card: Qwen3.8-9B Q4_K_M weights + KV are ~6 GB, so
N_GPU_LAYERS=99 (full offload) with wide headroom. CTX_SIZE defaults to the
local smoke-test value 32768 (raise with --ctx-size; the card has room).
GPU_ID=0: SaladCloud exposes one GPU per container, so the allocated card is
index 0 inside the container.

MODEL_ALIAS=qwen9b so the served model registers under a name that is
unambiguous in /v1/models and in the request body's "model" field (the image's
built-in default alias is qwen38-27b). llama-server matches the model name
loosely, so curl_salad.sh's default -m qwen still works too.

SALAD_API_KEY is read from deploy/salad_api.txt by salad_client (never printed).

Usage:
    python3 deploy/deploy_qwen9b.py
    python3 deploy/deploy_qwen9b.py --ctx-size 65536 --disk-size 20
    python3 deploy/deploy_qwen9b.py --org akl-on-salad
    python3 deploy/deploy_qwen9b.py --project llm --group qwen9b-llm
"""

import argparse
import base64
import json
import os
import sys

# salad_client.py lives at the repo root; running this as deploy/deploy_qwen9b.py
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
# Reuse the existing project (the only one that exists) — the SaladCloud API has
# no project-create operation, so a dedicated 'qwen9b' project can only be made in
# the web UI. The group itself stays named 'qwen9b'.
PROJECT_NAME = "qwen38-27b"
GROUP_NAME = "qwen9b"
# Digest-pinned (the airtight lever against the worker image cache, keyed by
# REPO NAME — see deploy_qwen38_9b.py's note). cuda128-v8 (2026-10-09):
# bw_reporter.py (MODEL_DIR growth -> ${API_STATE_DIR}/bw.log every 10 s, feeds
# `manage_groups.py start --min-bw-mbps`) + iputils-ping (CAP_NET_RAW) + scp +
# the `llama-stats` command; on top of cuda128-v7's
# watchdog v2.1 (arms on the group /ready endpoint, kills by POSTing the Salad
# group /stop with SALAD_STOP_KEY from --stop-key — stored b64-obfuscated,
# decoded at startup — then SIGTERM; a bare self-exit gets RESCHEDULED, not
# stopped, paid test 2026-10-09; key 'none'/missing/undecodable DISABLES the
# watchdog) on top of cuda128-v5's exec-form entry.sh CMD fix (v4's inline
# JSON CMD was malformed -> buildkit shell-form fallback -> dash exit 2
# crash-loop). IDLE_SHUTDOWN=
# none baked = off by default. Tag form for humans: boris271142/lmss:cuda128-v9
#   cuda128-v8: sha256:0386058c64d603430960c0b3d5ab03c2ad740f752d12ebd32b11dfc4ea31ebca
#   cuda128-v7: sha256:3db2ab311ed5c43b83f2c612966009524957f5fd3941dfdbaef0b263b3baa35d
IMAGE = "boris271142/lmss" \
        "@sha256:47d2d276b7991586613131aec1ed7a57b6245d12bc75abe20ebabba5cf189c15"
GPU_CLASS_BASE = "rtx3090"  # must match 'RTX 3090 (24 GB)', not a Laptop/variant class

# Mirrored from the live 'qwen38-27b-rtx5090' group (GET, 2026-09-30) — response-only
# 'dns' dropped; the create spec requires auth/port/protocol when networking is present.
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
        description="Create container group qwen9b (Qwen3.8-9B on RTX 3090, no draft/vision/template).",
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
    parser.add_argument("--image", default=IMAGE, help="Docker image to deploy (see module docstring for the tag choice)")
    parser.add_argument("--disk-size", type=float, default=25.0,
                        help="Disk space to allocate, in GB (sent as storage_amount bytes)")
    parser.add_argument("--memory-size", type=float, default=16.0,
                        help="Memory to allocate, in GB (sent as memory MB)")
    parser.add_argument("--model-repo", default="empero-ai/Qwen3.8-9B-Distill-GGUF", help="env MODEL_REPO")
    parser.add_argument("--model-file", default="Qwen3.8-9B-Q4_K_M.gguf", help="env MODEL_FILE")
    parser.add_argument("--model-alias", default="qwen9b",
                        help='env MODEL_ALIAS (the name the model registers under; the "model" id for the API)')
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
    parser.add_argument("--heartbeat-timeout", default="300",
                        help="env HEARTBEAT_TIMEOUT — seconds without a client keepalive call "
                             "(mode heartbeat) before the watchdog kills the container. The "
                             "client pinger fires every ~30 s, so keep this at ~2x the pinger "
                             "period or more (60 s minimum) or pinger jitter can false-kill. "
                             "Default 300 = 10 pinger periods (fleet-wide since 2026-10-09)")
    parser.add_argument("--idle-grace", default="1800",
                        help="env IDLE_GRACE — max seconds the watchdog waits for the FIRST "
                             "/ready ok before giving up (no kill). A cold start that "
                             "download+loads the model can blow past the 1800 s default "
                             "(2026-10-09: atx-hb-test2's 27B took ~37 min, the watchdog gave "
                             "up at 30 min and the group ran the whole session UNARMED) — use "
                             "3600 for big-quant groups")
    parser.add_argument("--ssh-guard", default="1",
                        help="env SSH_GUARD — 1 (default): the watchdog holds off its "
                             "self-stop while /.ssh exists in the container, and the "
                             "bandwidth arbitrator spends no /reallocate shot while it "
                             "exists. Contract: `touch /.ssh` after logging in over SSH, "
                             "`rm /.ssh` on the way out. 0 = guard off. Harmless on "
                             "images that predate the guard (the env is unread)")
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


def resolve_rtx3090(org: str) -> str:
    """Return the UUID of the GPU class whose base name is exactly 'rtx3090'.

    Normalization strips the parenthesized VRAM suffix, lowercases and removes
    spaces, so a 'RTX 3090 (24 GB) Laptop'-style variant does not accidentally match.
    """
    for gpu_class in list_gpu_classes(org):
        base = gpu_class.name.split("(")[0].strip().lower().replace(" ", "")
        if base == GPU_CLASS_BASE:
            return gpu_class.id
    available = ", ".join(c.name for c in list_gpu_classes(org))
    raise ValueError(f"GPU class {GPU_CLASS_BASE!r} not found among available classes: {available}")


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

    print(f"[1/5] resolving GPU class {GPU_CLASS_BASE!r} in org {org!r}")
    gpu_uuid = resolve_rtx3090(org)
    print(f"      {GPU_CLASS_BASE} -> {gpu_uuid}")

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

    # Plain 9B deployment: model + sizing only. No DRAFT_*/VISION_*/CHAT_TEMPLATE
    # — the 9B has none, and the image's baked-in defaults are the `none`
    # sentinel, so a plain deployment needs no env at all.
    env: dict[str, str] = {
        "GPU_ID": args.gpu_id,
        "MODEL_REPO": args.model_repo,
        "MODEL_FILE": args.model_file,
        "MODEL_ALIAS": args.model_alias,
        "CTX_SIZE": args.ctx_size,
        "N_GPU_LAYERS": args.n_gpu_layers,
        "NAME": group,
        # Idle/heartbeat self-shutdown (in-container idle_watchdog.py):
        # 'none' = off; 'idle'/'heartbeat' arm the watchdog (see --idle-shutdown).
        # Harmless on images that predate the watchdog (the env is unread).
        "IDLE_SHUTDOWN": args.idle_shutdown,
        "IDLE_TIMEOUT": args.idle_timeout,
        "HEARTBEAT_TIMEOUT": args.heartbeat_timeout,
        # Watchdog arming window: max seconds to wait for the first /ready ok
        # before it gives up (no kill). Big-quant cold starts exceed the 1800
        # default — the watchdog then exits and the group runs UNARMED.
        "IDLE_GRACE": args.idle_grace,
        # SSH-presence guard (watchdog v2.2 + bandwidth arbitrator): while
        # /.ssh exists in the container the watchdog holds off its self-stop
        # and the arbitrator spends no /reallocate shot. The user touches
        # /.ssh after logging in over SSH and removes it on the way out.
        # Always sent; harmless on images predating the guard (the env is
        # unread).
        "SSH_GUARD": args.ssh_guard,
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
        cpu=8,
        memory_mb=memory_mb,
        gpu_classes=(gpu_uuid,),
        shm_size=64,
        storage_amount=storage_amount,
        image_caching=True,
        priority=args.priority,
        networking=NETWORKING,
        readiness_probe=READINESS_PROBE,
        scheduled_scaling_enabled=True,
    )
    print(f"[4/5] creating container group {group!r} in project {project!r}")
    print(f"      image={args.image!r} replicas=1 cpu=8 memory={memory_mb} MB "
          f"disk={storage_amount} bytes shm={64} MB priority={args.priority}")
    print(f"      env={json.dumps(redact(env))}")
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

    print(f"OK: created group {result.name!r} in {org}/{project} on {GPU_CLASS_BASE}.")
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
