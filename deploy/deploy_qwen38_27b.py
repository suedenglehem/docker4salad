#!/usr/bin/env python3
"""Deploy the canonical 27B group 'qwen38-27b-q6k' (org/project/group via
--org / --project / --group; defaults ma-casa-in-paris / qwen38-27b /
qwen38-27b-q6k).

2026-10-02: re-created under a FRESH name (replacing 'qwen38-27b-cuda128').
The original name 'qwen38-27b' is stuck in the DELETE name-conflict
tombstone (salad-group-name-tombstone memory) — still 400 name_conflict
24h+ after its 2026-10-01 DELETE, so the user approved the name
'qwen38-27b-Q6K'; the API name pattern is lowercase-only, hence
'qwen38-27b-q6k'. The served alias stays 'qwen38-27b' (MODEL_ALIAS env,
llama-server --alias), so clients and curl_salad.sh -m are unaffected.

Serves the baked image `lmss_jonathancoletti_qwen38_q6_mtp_vision` (a
FROM boris271142/lmss:cuda128-v3 extension, Dockerfile.lmss_q6_mtp_vision):
the MTP draft (Q8_0, 2.95 GiB) and mmproj (F16, 885 MiB) are baked into the
image layers, so at runtime ONLY the main model is downloaded from
HuggingFace via the fast `hf` xet path. The v3 wget2 DRAFT_MODEL_URL /
VISION_MODEL_URL mechanism is GONE — those env vars are no longer read.

Model: default Qwen3.8-27B-Uncensored-Q5_K_M.gguf (18.19 GiB) — the live
production quant since 2026-10-03. Like the other plain Q/IQ quants in the
repo, this build has the MTP head EMBEDDED in the gguf (nextn_predict_layers
metadata, verified via the GGUF header), so it runs on the in-gguf head:
USE_DRAFT_MODEL=none, --spec-type draft-mtp, NO --model-draft. For a quant
WITHOUT an embedded head (e.g. the noMTP-Q4_K_M build), pass
--use-draft-model 1 and the image passes the baked draft via --model-draft.
Other quants via --model-file (Q6_K, Q4_K_M, ...). Vision (--mmproj) is
ALWAYS on in this _vision image.

Create-or-update semantics: if the group already exists it is updated in
place (stop when running -> PATCH image + full env -> start), which keeps
the group's DNS stable for clients. A missing group is created. The
readiness probe is the ~40-minute failure window that tolerates a cold
main-model download on a new worker (~18.2 GiB Q5_K_M, ~20.9 GiB Q6_K):
30 s delay + 20 x 120 s = 2430 s max (the spec caps: delay 1200, period
120, failure_threshold 20). The 30 s delay (not 1200) keeps the FIRST
probe early, so the gateway — and cloudflare in front of it — open as soon
as the model is actually ready instead of a fixed 20 min after the
container starts.

GPU: default RTX 3090 (24 GB) — the live production card: 18.19 GiB Q5_K_M
+ mmproj (0.86 GiB) + q8_0 KV (~3.0 GiB at the 90000 default ctx) ≈ 22 GiB,
fits with ~2 GiB headroom (verified live 2026-10-03: served n_ctx 90112).
An RTX 5090 (32 GB) fits Q6_K at 128K+ with wide headroom — see
docker/README.md "Choosing quant + context length by VRAM" for the full
quant/ctx matrix. --gpu takes SEVERAL classes (e.g. --gpu rtx3090 rtx5090):
the group's gpu_classes then lists them all and Salad may place the replica
on any of them.

Ctx: the default is 90000 — the live production setting (served n_ctx
90112; the 27B's trained context is 262144). Pass --ctx-size 132768 for
the full context (fits on Q5_K_M/5090 or Q4_K_M/3090).

Disk: 50 GiB (create path; resources are NOT patchable, an existing group
keeps its resources). The main-model `hf download` can transiently hold ~2x
the file while it lands (~41.8 GiB for the 20.9 GiB Q6_K); 40 GiB was
observed to be too close for comfort, so the default is 50.

CLAUDE_TEMPLATE=1 (the default here) makes the image dump the chat template
from the freshly downloaded gguf at startup and apply the one-line Claude
Code patch (Anthropic-format requests send system messages mid-conversation;
the stock Qwen template raises on them). --no-claude-template sends 'none',
which serves the gguf's embedded template as-is (cline/py/opencode).

SALAD_API_KEY is read from deploy/salad_api.txt by salad_client; HF_TOKEN from
hft.txt (next to this script; validated via whoami-v2; neither is ever printed).

The built-in defaults ARE the live production profile (2026-10-03):
Q5_K_M @ CTX_SIZE 90000, digest-pinned cuda128-v5 image (2026-10-05) — a
bare run re-applies that, idempotent against an existing group.

Usage:
    python3 deploy/deploy_qwen38_27b.py                     # create-or-update, start (live prod profile)
    python3 deploy/deploy_qwen38_27b.py --no-start          # apply config only
    python3 deploy/deploy_qwen38_27b.py --use-draft-model 1 # gguf without MTP head
    python3 deploy/deploy_qwen38_27b.py --model-file Qwen3.8-27B-Uncensored-Q6_K.gguf \
        --gpu rtx5090 --ctx-size 132768              # other quant/card/ctx combos
    python3 deploy/deploy_qwen38_27b.py --org akl-on-salad  # deploy into another org on the same account
    python3 deploy/deploy_qwen38_27b.py --project llm --group qwen38-27b-q5 --no-start  # a copy elsewhere, not started
"""

import argparse
import os
import sys
import time
import urllib.error
import urllib.request

# salad_client.py lives at the repo root; running this as deploy/deploy_qwen38_27b.py
# puts deploy/ on sys.path, not the root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

# Default org (override with --org): an account can host several orgs, all
# sharing the same Salad API key.
ORGANIZATION_NAME = "ma-casa-in-paris"
PROJECT_NAME = "qwen38-27b"
# Fresh 2026-10-02 name (the user requested 'qwen38-27b-Q6K'; the API name
# pattern ^[a-z][a-z0-9-]{0,61}[a-z0-9]$ is lowercase-only, hence q6k).
# 'qwen38-27b' itself is stuck in the DELETE name-conflict tombstone.
GROUP_NAME = "qwen38-27b-q6k"
# Served model name (llama-server --alias) — deliberately NOT the group
# name; curl_salad.sh -m and the Claude Code client reference it.
MODEL_ALIAS = "qwen38-27b"

# Digest-pinned: the API accepts the @sha256 ref verbatim, and it is the
# airtight lever against the worker image cache (keyed by repo NAME — a tag
# re-push can serve stale layers on workers that cached the old one). This
# is the cuda128-v5 push (2026-10-05, build id 'lmss q6-mtp-vision-v5
# (baked draft+vision, claude-template) 2026-10-05'); the tag form, for
# humans:
#   boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:cuda128-v5
# Previous manifest-list digests, for reference:
#   cuda128-v4: sha256:789ff2b34000409d13d76c2f51d607502f65d96c739fa3adfc5d04ae6f353a4a
#   cuda128-v3: sha256:a1ab8bd22b9fd7aef5c00e902744cb3161d4a204c92e6265fe266a332bec51fa
IMAGE = ("boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision"
         "@sha256:4089a457281519033764868d5422786b7c48e0d2e81f7847354ceb5acd953839")

MODEL_REPO = "JonathanColetti/Qwen3.8-27B-Uncensored-GGUF"
# Default = live production quant (2026-10-03): Q5_K_M, MTP head embedded in
# the gguf — no separate draft needed. For a noMTP build use that filename
# + --use-draft-model 1.
MODEL_FILE = "Qwen3.8-27B-Uncensored-Q5_K_M.gguf"

HF_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hft.txt")

GPU_CHOICES = ("rtx5090", "rtx3090")
# Idle/heartbeat self-shutdown (the in-container idle_watchdog.py): "none"
# (default) = off, today's behavior; "idle" = kill after IDLE_TIMEOUT s of no
# chat traffic; "heartbeat" = kill after HEARTBEAT_TIMEOUT s of no client
# keepalive pings (run the client with SALAD_HEARTBEAT=1).
IDLE_SHUTDOWN = "none"
IDLE_SHUTDOWN_CHOICES = ("none", "idle", "heartbeat")

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
    parser.add_argument("--org", default=ORGANIZATION_NAME,
                        help="Salad organization to deploy into (an account can host several "
                             "orgs sharing one API key)")
    parser.add_argument("--project", default=PROJECT_NAME,
                        help="Salad project to deploy into (projects must exist — the API has "
                             "no project-create endpoint, create it in the web UI)")
    parser.add_argument("--group", default=GROUP_NAME,
                        help="container group name (also used as the NAME env)")
    parser.add_argument("--gpu", choices=GPU_CHOICES, nargs="+", default=["rtx3090"],
                        help="GPU class(es); pass several to let Salad place on any of them "
                             "(rtx3090 = live production card, 24 GB; rtx5090 = 32 GB, fits more "
                             "quant/ctx)")
    parser.add_argument("--image", default=IMAGE,
                        help="Docker image (baked q6-mtp-vision image; default is digest-pinned "
                             "cuda128-v5)")
    parser.add_argument("--model-file", default=MODEL_FILE,
                        help="env MODEL_FILE — the quant to serve (plain Q/IQ files embed the MTP "
                             "head; noMTP builds pair with --use-draft-model 1)")
    parser.add_argument("--disk-size", type=float, default=50.0,
                        help="disk in GiB, CREATE path only (resources are not patchable)")
    parser.add_argument("--memory-size", type=float, default=16.0,
                        help="memory in GB, CREATE path only (resources are not patchable)")
    parser.add_argument("--ctx-size", default="90000",
                        help="env CTX_SIZE (90000 = live production, served n_ctx 90112; "
                             "132768 = full)")
    parser.add_argument("--use-draft-model", default="none",
                        help="'none' = gguf's embedded MTP head; any other value adds "
                             "--model-draft with the baked draft (noMTP quants)")
    parser.add_argument("--no-claude-template", action="store_true",
                        help="env CLAUDE_TEMPLATE=none — serve the gguf's embedded chat "
                             "template as-is, no Claude Code patch (default CLAUDE_TEMPLATE=1 "
                             "applies the one-line mid-conversation-system patch at startup)")
    parser.add_argument("--idle-shutdown", choices=IDLE_SHUTDOWN_CHOICES, default=IDLE_SHUTDOWN,
                        help="env IDLE_SHUTDOWN — in-container watchdog mode: none (off), "
                             "idle (self-stop after --idle-timeout s with no chat traffic), "
                             "heartbeat (self-stop after --heartbeat-timeout s with no client "
                             "keepalive pings — run cl_salad/chat_salad.sh with "
                             "SALAD_HEARTBEAT=1). Needs a watchdog-capable image "
                             "(version.sh prints the WATCHDOG generation). Armed mode needs "
                             "restart_policy=never so the self-exit STAYS stopped — that is "
                             "CREATE-ONLY (not in the PATCH schema): an existing group with "
                             "restart_policy=always must be deleted + recreated with a fresh "
                             "name (names tombstone 10+ min; keep the MODEL_ALIAS stable)")
    parser.add_argument("--idle-timeout", default="600",
                        help="env IDLE_TIMEOUT — seconds of flat token counters on /metrics "
                             "(mode idle) before the watchdog SIGTERMs llama-server and the "
                             "container exits")
    parser.add_argument("--heartbeat-timeout", default="600",
                        help="env HEARTBEAT_TIMEOUT — seconds without a client keepalive call "
                             "(mode heartbeat) before the watchdog kills the container")
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


def build_env(name: str, ctx_size: str, use_draft_model: str, hf_token: str | None,
              model_file: str | None = None, claude_template: str = "1",
              idle_shutdown: str = "none", idle_timeout: str = "600",
              heartbeat_timeout: str = "600") -> dict[str, str]:
    """Full env for the group (create sets it, PATCH replaces it wholesale).

    The baked image drops v3's DRAFT_MODEL_URL / VISION_MODEL_URL: the draft +
    mmproj are baked in, vision is always on. USE_DRAFT_MODEL is the only
    draft knob — 'none' uses the gguf's embedded MTP head, any other value
    makes the CMD pass --model-draft with the baked draft file.

    CLAUDE_TEMPLATE (v5): '1' (default) = at startup the image dumps the chat
    template from the freshly downloaded gguf and applies the one-line Claude
    Code patch (mid-conversation system messages) via --chat-template-file;
    'none' = serve the gguf's embedded template as-is. The static
    CHAT_TEMPLATE=/opt/llama.cpp/qwen3.8.q6.jinja mechanism is retired.
    """
    env = {
        "GPU_ID": "0",
        "MODEL_REPO": MODEL_REPO,
        # Default = MODEL_FILE (Q5_K_M, live production quant); --model-file
        # overrides it (Q6_K, Q4_K_M, ...).
        "MODEL_FILE": model_file or MODEL_FILE,
        # Hybrid model (1 in 4 layers is full attention): q8_0 KV ≈ 35 KiB/token
        # — ~3.0 GiB at the 90000 default, ~4.6 GiB at 132768. On the 24 GB
        # 3090 that fits on top of Q5_K_M (~2 GiB headroom at 90K, verified
        # live) but not Q6_K; the 32 GB 5090 fits Q6_K at 128K+.
        "CTX_SIZE": ctx_size,
        "N_GPU_LAYERS": "99",
        "NAME": name,
        # Served alias stays 'qwen38-27b' (clients + curl_salad.sh -m),
        # independent of the group name.
        "MODEL_ALIAS": MODEL_ALIAS,
        # Claude Code template: '1' = dump the gguf's own template at startup
        # and apply the one-line mid-conversation-system patch (--no-claude-
        # template sends 'none' -> embedded template as-is).
        "CLAUDE_TEMPLATE": claude_template,
        # 'none' = the quant's embedded MTP head (Q5_K_M/Q6_K/Q4_K_M builds).
        # Any other value -> the image passes --model-draft
        # /models/...draft-Q8_0.gguf (needed for noMTP quants).
        "USE_DRAFT_MODEL": use_draft_model,
        # Idle/heartbeat self-shutdown (in-container idle_watchdog.py):
        # 'none' = off; 'idle' = kill after IDLE_TIMEOUT s of no chat
        # traffic; 'heartbeat' = kill after HEARTBEAT_TIMEOUT s of no client
        # keepalive pings. Always sent (PATCH replaces the env wholesale);
        # harmless on images that predate the watchdog (the env is unread).
        "IDLE_SHUTDOWN": idle_shutdown,
        "IDLE_TIMEOUT": idle_timeout,
        "HEARTBEAT_TIMEOUT": heartbeat_timeout,
    }
    if hf_token:
        env["HF_TOKEN"] = hf_token
    return env


def redact(env: dict[str, str]) -> dict[str, str]:
    """Mask secrets for printing (HF_TOKEN is the only secret in the env)."""
    return {k: "<REDACTED>" if k == "HF_TOKEN" else v for k, v in env.items()}


def get_group(org: str, project: str, group: str):
    try:
        return get_container_group(org, project, group)
    except SaladApiError as e:
        if e.status_code == 404:
            return None
        raise


def wait_for(status: str, timeout_s: int, org: str, project: str, group: str) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        current = get_container_group(org, project, group)
        if current.current_status == status:
            return
        print(f"      status={current.current_status!r} (waiting for {status!r}) ...", flush=True)
        time.sleep(10)
    raise TimeoutError(f"group did not reach {status!r} within {timeout_s} s")


def wait_until_not_pending(timeout_s: int, org: str, project: str, group: str) -> str:
    """Wait out the create-path 'pending' window (start 400s while pending).

    A freshly created group is 'pending' and the API refuses START with
    HTTP 400 'not allowed while in a Pending status' until it settles.
    Returns the status we settled on.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        current = get_container_group(org, project, group)
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
    org = args.org
    project = args.project
    group = args.group

    print(f"[1/5] resolving GPU class(es) {args.gpu!r} in org {org!r}")
    gpu_classes = list_gpu_classes(org)
    gpus = tuple(resolve_gpu_class(c, gpu_classes) for c in args.gpu)
    print("      -> " + ", ".join(f"{g.name!r} ({g.id})" for g in gpus))

    print("[2/5] loading HF token (validated, never printed)")
    hf_token = load_hf_token()
    print(f"      token {'validated' if hf_token else 'UNAVAILABLE — model download will run unauthenticated'}")
    env = build_env(group, args.ctx_size, args.use_draft_model, hf_token, args.model_file,
                    claude_template="none" if args.no_claude_template else "1",
                    idle_shutdown=args.idle_shutdown,
                    idle_timeout=args.idle_timeout,
                    heartbeat_timeout=args.heartbeat_timeout)

    existing = get_group(org, project, group)
    if existing is not None:
        print(f"[3/5] group exists (status={existing.current_status!r}, "
              f"image={existing.raw['container']['image']!r}) — updating in place "
              f"(DNS stays stable for clients)")
        if not args.no_start and existing.current_status in ("running", "scaling"):
            print("      stopping group before PATCH")
            stop_container_group(StopContainerGroupRequest(
                organization_name=org,
                project_name=project,
                container_group_name=group,
            ))
            wait_for("stopped", 120, org, project, group)
            print("      stopped")
        if args.no_start and existing.current_status in ("running", "scaling"):
            print("      NOTE: --no-start while running — the PATCH takes effect on the next start")
        live_policy = existing.raw.get("restart_policy")
        if args.idle_shutdown != "none" and live_policy == "always":
            print(f"      WARNING: --idle-shutdown {args.idle_shutdown!r} arms the in-container "
                  "watchdog, but this group's restart_policy is 'always' — after the watchdog's "
                  "self-exit Salad RESTARTS the container and billing continues. restart_policy "
                  "is CREATE-ONLY (not in the PATCH schema, so this update cannot flip it): to "
                  "arm, delete the group and recreate it with the same flags — fresh --group "
                  "name (the deleted name tombstones 10+ min), keep the MODEL_ALIAS stable for "
                  "clients. The env knobs are applied; the watchdog stays harmless (it kills, "
                  "the container restarts) — do NOT leave it armed on an 'always' group.")
        result = update_container_group(
            org, project, group,
            UpdateContainerGroupRequest(
                image=args.image,
                gpu_classes=tuple(g.id for g in gpus),
                environment_variables=env,
                readiness_probe=dict(READINESS_PROBE),
            ),
        )
        print(f"      HTTP {result.status_code} {result.reason_phrase} status={result.current_status!r}")
    else:
        print(f"[3/5] group absent — creating (memory={int(round(args.memory_size * 1024))} MB, "
              f"disk={int(round(args.disk_size * 1024**3))} bytes, "
              f"gpu={[g.name for g in gpus]!r})")
        try:
            proj = create_project(org, project)
            print(f"      created project via POST -> HTTP {proj.status_code}")
        except SaladApiError as e:
            if e.status_code in (404, 409):
                print(f"      project endpoint HTTP {e.status_code} — using existing project")
            else:
                raise
        request = CreateContainerGroupRequest(
            name=group,
            display_name=group,
            autostart_policy=False,
            replicas=1,
            # 'never' when the watchdog is armed: its SIGTERM of PID 1 exits
            # the container and Salad leaves the group STOPPED (free). With
            # 'always' the self-exit would just restart the container.
            restart_policy="never" if args.idle_shutdown != "none" else "always",
            container_image=args.image,
            command=(),
            environment_variables=env,
            cpu=8,
            memory_mb=int(round(args.memory_size * 1024)),
            gpu_classes=tuple(g.id for g in gpus),
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
        created = create_container_group(org, project, request)
        print(f"      HTTP {created.status_code} {created.reason_phrase} "
              f"id={created.id!r} status={created.current_status!r}")

    print("[4/5] verifying group config")
    dns = print_group(get_group(org, project, group))

    if args.no_start:
        print("OK: group config applied (NOT started, per --no-start)")
        return 0

    print("[5/5] starting group")
    # Create path: a fresh group is 'pending' and START 400s until it settles.
    if get_group(org, project, group).current_status == "pending":
        settled = wait_until_not_pending(600, org, project, group)
        print(f"      settled to {settled!r}")
    started = start_container_group(StartContainerGroupRequest(
        organization_name=org,
        project_name=project,
        container_group_name=group,
    ))
    print(f"      start -> HTTP {started.status_code} {started.reason_phrase}; dns={dns!r}")
    print("      waiting for running (container up; the model may still be downloading — "
          "a cold worker re-downloads the main model, ~18 GiB for Q5_K_M / ~21 GiB for "
          "Q6_K; draft + vision are baked)...")
    wait_for("running", 1800, org, project, group)
    print_group(get_group(org, project, group))
    print("OK: group running. 'running' means the container process is up —")
    print(f"      1. verify the LIVE build in-container: version.sh should print")
    print(f"         'lmss q6-mtp-vision-v5 (baked draft+vision, claude-template) 2026-10-05';")
    print(f"         /models holds the baked draft + mmproj while the main model hf-downloads")
    print(f"      2. PID1 cmdline: --spec-type draft-mtp --spec-draft-n-max 5 --mmproj "
          f"{'(no --model-draft, USE_DRAFT_MODEL=none)' if args.use_draft_model == 'none' else '--model-draft ...draft-Q8_0.gguf'} "
          f"--alias {MODEL_ALIAS}")
    print(f"      3. gateway answers only after the probe passes (/ready, ~40-min window, first probe at 30 s):")
    print(f"         claude/curl_salad.sh -url https://{dns} -m {MODEL_ALIAS}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SaladApiError, ValueError, TimeoutError) as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
