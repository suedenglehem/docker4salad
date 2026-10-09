#!/usr/bin/env python3
"""Deploy a group on the generic (model-agnostic) `lmss_generic` image.

The built-in defaults ARE the ATX-Swift test profile (2026-10-05):
bjivanovich/ATX-Swift-1.5-Qwen3.8-27B-Uncensored-MTP-GGUF @ Q5_K_M (18.77
GiB), vision projector mmproj-BF16.gguf (0.87 GiB) from the same repo, the
MTP head EMBEDDED in the gguf (DRAFT_MODEL=none, SPEC_TYPE=draft-mtp),
CLAUDE_TEMPLATE=1, CTX_SIZE 90000 on an RTX 3090.

Image: `lmss_generic` (FROM boris271142/lmss:cuda128-v3, NO baked models,
Dockerfile.lmss_generic): at runtime EVERYTHING is downloaded from
HuggingFace via the fast `hf` xet path — the main model, and the OPTIONAL
draft (DRAFT_MODEL) and vision projector (VISION_MODEL). The v3 wget2
DRAFT_MODEL_URL / VISION_MODEL_URL (full URL) mechanism is gone; the new
refs take "hf://<org>/<repo>/<file>" or a bare file resolved against
MODEL_REPO, and a download failure dies the container loudly.

Unlike the baked qwen3.8 image, this one is model-agnostic:
  * draft + vision are opt-in per group (default none) — in the 27B
    q6-mtp-vision image they are baked and vision is always on
  * SPEC_TYPE selects the speculation mode explicitly (draft-mtp |
    ngram-mod | none): an embedded-MTP gguf without a separate draft file
    runs on its in-gguf head (SPEC_TYPE=draft-mtp + DRAFT_MODEL=none)
    instead of being downgraded to ngram-mod

Create-or-update semantics: if the group already exists it is updated in
place (stop when running -> PATCH image + full env -> start), which keeps
the group's DNS stable for clients. A missing group is created. The
readiness probe is the ~40-minute failure window that tolerates a cold
main-model download on a new worker (30 s delay + 20 x 120 s = 2430 s max,
the spec caps: delay 1200, period 120, failure_threshold 20). The 30 s
delay (not 1200) keeps the FIRST probe early, so the gateway — and
cloudflare in front of it — open as soon as the model is actually ready.

GPU: default RTX 3090 (24 GB): 18.77 GiB ATX Q5_K_M + mmproj (0.87 GiB) +
q8_0 KV (~3.0 GiB at the 90000 default ctx) ≈ 22.6 GiB, fits with ~1.4 GiB
headroom. The Q6_K build (20.89 GiB) + vision + 90K KV ≈ 24.8 GiB needs an
RTX 5090 (32 GB). Full 132768 ctx (KV ~4.6 GiB) fits Q5_K_M on a 5090.
--gpu takes ANY card the org lists — the class name lowercased, spaces
stripped (e.g. rtx3090, rtx4060ti, rtx5090); pass several and the group's
gpu_classes then lists them all and Salad may place the replica on any.
Names are resolved against the org's live GPU classes at deploy time, so an
unknown name fails with the list of what IS available.

Ctx: default 90000 (the Q5_K_M/3090 fit; served n_ctx is rounded up to a
multiple of the block size, 90112 on the Qwen3.8 27B line).

Disk: 50 GiB (create path; resources are NOT patchable, an existing group
keeps its resources). The main-model `hf download` can transiently hold ~2x
the file while it lands (~37.5 GiB for the 18.77 GiB Q5_K_M); 40 GiB was
too close for comfort on the 27B deployer, so the default is 50.

CLAUDE_TEMPLATE=1 (the default here) makes the image dump the chat template
from the freshly downloaded gguf at startup and apply the one-line Claude
Code patch (Anthropic-format requests send system messages mid-conversation;
the stock Qwen template raises on them). --no-claude-template sends 'none',
which serves the gguf's embedded template as-is (cline/py/opencode).
Non-Qwen ggufs (no raise-marker) get the patch skipped by the script.

EXTRA_ARGS (default 'none') is ONE whitespace-separated string of extra
llama-server args. At startup the image word-splits it and appends the
tokens LAST to the llama-server argv; llama.cpp resolves duplicate flags
LAST-WINS, so a group's EXTRA_ARGS can override the baked base flags
(e.g. '--threads-batch 8 --top-k 40 --temp 0.2 --ctx-size 131072') without a
per-tuning image. No shell quoting: every whitespace-separated token
becomes one argv entry (llama.cpp flag values don't contain whitespace in
practice). Requires the generic-v3+ image (the digest pin below IS that
push, cuda128-v3).

SALAD_API_KEY is read from deploy/salad_api.txt by salad_client; HF_TOKEN from
hft.txt (next to this script; validated via whoami-v2; neither is ever printed).

Usage:
    python3 deploy/deploy_generic.py                  # ATX-Swift test profile (create-or-update, start)
    python3 deploy/deploy_generic.py --no-start       # apply config only
    python3 deploy/deploy_generic.py --vision-model none            # no vision
    python3 deploy/deploy_generic.py --draft-model hf://<org>/<repo>/<draft.gguf>
    python3 deploy/deploy_generic.py --spec-type ngram-mod          # non-MTP gguf, no draft
    python3 deploy/deploy_generic.py --extra-args "--top-k 40 --repeat-last-n 256 --reasoning-format none"
    python3 deploy/deploy_generic.py --model-repo <org>/<name> --model-file <quant.gguf> \
        --model-alias my-model
"""

import argparse
import base64
import os
import sys
import time
import urllib.error
import urllib.request

# salad_client.py lives at the repo root; running this as deploy/deploy_generic.py
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
# 'llm' — the user-created LLM-experiments project (web UI; the API has no
# project-create endpoint), kept separate from the production 'qwen38-27b'.
PROJECT_NAME = "llm"
GROUP_NAME = "atx-swift-27b-q5"
# Served model name (llama-server --alias) — deliberately NOT the group
# name; the Claude Code client (cl_salad --model) references it.
MODEL_ALIAS = "atx-swift-27b"

# Digest-pinned: the API accepts the @sha256 ref verbatim, and it is the
# airtight lever against the worker image cache (keyed by repo NAME — a tag
# re-push can serve stale layers on workers that cached the old one). This
# is the cuda128-v8 push (2026-10-09, build id 'lmss generic-v8 (hf runtime
# download, claude-template, extra-args, idle_watchdog v2.1 [b64 key decode +
# key=none disables watchdog] + bw_reporter + iputils-ping + llama-stats,
# trapped-child supervisor, exec-form entry.sh CMD) 2026-10-09' — bw_reporter.py
# samples MODEL_DIR growth every 10 s into ${API_STATE_DIR}/bw.log (self-exits
# on llama-server /health) so `manage_groups.py start --min-bw-mbps N` can read
# it over SSH and reallocate a slow node; iputils-ping needs CAP_NET_RAW (Salad
# workers may deny it); `llama-stats` = stats.sh symlink, local-only; on top of
# watchdog v2.1 (arms on the group /ready endpoint, kills by POSTing the Salad
# group /stop with SALAD_STOP_KEY from --stop-key, stored b64-obfuscated and
# decoded at startup, then SIGTERM — a bare self-exit gets RESCHEDULED, not
# stopped, paid test 2026-10-09; key 'none'/missing/undecodable DISABLES the
# watchdog) on top of the exec-form entry.sh CMD fix; based on
# boris271142/lmss:cuda128-v8@sha256:0386058c…); the tag form, for humans:
#   boris271142/lmss_generic:cuda128-v8
#   cuda128-v7: sha256:43206e0a231ff60c7228ff7ca2894f6e80e995c6f5994f98d37067018e4fe576
#   cuda128-v6: sha256:d7a0a327cd70bd6b7617feb6ebb09ae11b1403681635bcac07b369ee281fc0e5
IMAGE = "boris271142/lmss_generic" \
        "@sha256:9fe70c073e1c11828cb7b611acb3b6685ea17b51797168cae1cd6ef1d869eac1"

MODEL_REPO = "bjivanovich/ATX-Swift-1.5-Qwen3.8-27B-Uncensored-MTP-GGUF"
# Q5_K_M — MTP head embedded in the gguf (the 'MTP' in the repo name), so no
# separate draft file is needed (DRAFT_MODEL=none, SPEC_TYPE=draft-mtp).
MODEL_FILE = "ATX-Swift-1.5-Qwen3.8-27B-Uncensored-MTP-i1-Q5_K_M.gguf"
# Optional auxiliary models: "none" = off; "hf://<org>/<repo>/<file>" or a
# bare "<file>" against MODEL_REPO (downloaded at runtime with `hf`).
DRAFT_MODEL = "none"
VISION_MODEL = ("hf://bjivanovich/ATX-Swift-1.5-Qwen3.8-27B-Uncensored-"
                "MTP-GGUF/mmproj-BF16.gguf")
# Speculation mode: draft-mtp | ngram-mod | none.
SPEC_TYPE = "draft-mtp"
# One whitespace-separated string of extra llama-server args, appended LAST
# to the argv (duplicate flags last-wins -> overrides the baked base flags).
# "none" = off (never empty: the SaladCloud API rejects empty env values).
EXTRA_ARGS = "none"


def strip_none_value_args(s):
    """Drop `--flag none` PAIRS from an extra-args string before it reaches
    llama-server. A literal "none" VALUE means the feature is off — and
    leaving `--reasoning-format none` in DISABLES llama.cpp's default
    think-block parsing, so the template's empty <think></think> generation
    prefix leaks into every answer (the 2026-10-09 double-<think> report).
    The whole-string sentinel "none" (EXTRA_ARGS off — the Salad API rejects
    empty env values) is preserved."""
    if s.strip() == "none":
        return s
    toks = s.split()
    out, i = [], 0
    while i < len(toks):
        if toks[i].startswith("--") and i + 1 < len(toks) and toks[i + 1] == "none":
            i += 2
            continue
        out.append(toks[i])
        i += 1
    return " ".join(out)

HF_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hft.txt")

SPEC_TYPE_CHOICES = ("draft-mtp", "ngram-mod", "none")
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
        description="Create-or-update a group on the generic (model-agnostic) lmss_generic image.",
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
    parser.add_argument("--gpu", nargs="+", default=["rtx3090"],
                        help="GPU class name(s) — the org's class name lowercased, spaces "
                             "stripped (e.g. rtx3090, rtx4090, rtx4060ti, rtx5090); pass several "
                             "to let Salad place on any of them. Resolved against the org's live "
                             "GPU classes — an unknown name lists what is available "
                             "(rtx3090 = 24 GB, fits ATX Q5_K_M + vision @ 90K; rtx5090 = 32 GB, "
                             "fits Q6_K / full ctx)")
    parser.add_argument("--image", default=IMAGE,
                        help="Docker image (generic image; digest-pin the ref after each push)")
    parser.add_argument("--model-repo", default=MODEL_REPO,
                        help="env MODEL_REPO — the HuggingFace repo the main model (and any "
                             "bare draft/vision file) is downloaded from")
    parser.add_argument("--model-file", default=MODEL_FILE,
                        help="env MODEL_FILE — the main gguf to serve (quant)")
    parser.add_argument("--model-alias", default=MODEL_ALIAS,
                        help="env MODEL_ALIAS — the served model name (--alias) clients use")
    parser.add_argument("--draft-model", default=DRAFT_MODEL,
                        help="'none' = the gguf's embedded MTP head (when SPEC_TYPE=draft-mtp); "
                             "'hf://<org>/<repo>/<file>' or a bare '<file>' against --model-repo "
                             "for a separate --model-draft")
    parser.add_argument("--vision-model", default=VISION_MODEL,
                        help="'none' = no vision; 'hf://<org>/<repo>/<file>' or a bare '<file>' "
                             "for the mmproj projector (~1 GiB of VRAM)")
    parser.add_argument("--spec-type", choices=SPEC_TYPE_CHOICES, default=SPEC_TYPE,
                        help="speculation mode: draft-mtp (embedded or separate MTP head), "
                             "ngram-mod (n-gram self-speculation, no MTP needed), none (off)")
    parser.add_argument("--extra-args", default=EXTRA_ARGS,
                        help="env EXTRA_ARGS — one whitespace-separated string of extra "
                             "llama-server args appended LAST to the argv (duplicate flags "
                             "last-wins: these override the baked base flags, e.g. "
                             "'--threads-batch 8 --top-k 40 --temp 0.2'); no shell quoting; "
                             "'none' = off")
    parser.add_argument("--ctx-size", default="90000",
                        help="env CTX_SIZE (90000 = the Q5_K_M/3090 fit; 132768 = full, 5090)")
    parser.add_argument("--disk-size", type=float, default=50.0,
                        help="disk in GiB, CREATE path only (resources are not patchable)")
    parser.add_argument("--memory-size", type=float, default=16.0,
                        help="memory in GB, CREATE path only (resources are not patchable)")
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
                             "(version.sh prints the WATCHDOG generation). The watchdog arms "
                             "on the group's /ready endpoint and kills by POSTing the group "
                             "/stop endpoint — pass --stop-key (account-wide key): "
                             "self-exit alone does NOT stop a group (Salad reschedules a new "
                             "instance — paid test 2026-10-09), so arming without --stop-key "
                             "bills. The create path also sets restart_policy=never when "
                             "armed (create-only field) to stop crash-loop billing")
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


def build_env(name: str, ctx_size: str, model_repo: str, model_file: str,
              model_alias: str, draft_model: str, vision_model: str,
              spec_type: str, hf_token: str | None,
              claude_template: str = "1",
              extra_args: str = "none",
              idle_shutdown: str = "none",
              idle_timeout: str = "600",
              heartbeat_timeout: str = "600",
              idle_grace: str = "1800",
              ssh_guard: str = "1",
              stop_key: str = "none",
              org: str = "", project: str = "", group: str = "") -> dict[str, str]:
    """Full env for the group (create sets it, PATCH replaces it wholesale).

    The generic image is model-agnostic: MODEL_REPO / MODEL_FILE /
    MODEL_ALIAS / CTX_SIZE select the model per group, and DRAFT_MODEL /
    VISION_MODEL (default 'none') opt into the hf-downloaded auxiliary
    models. SPEC_TYPE picks the speculation mode explicitly — draft-mtp on
    an embedded-MTP gguf with DRAFT_MODEL=none runs the in-gguf head (the
    derived draft-present/absent two-way would have downgraded it to
    ngram-mod).

    CLAUDE_TEMPLATE: '1' (default) = at startup the image dumps the chat
    template from the freshly downloaded gguf and applies the one-line
    Claude Code patch (mid-conversation system messages) via
    --chat-template-file; 'none' = serve the gguf's embedded template as-is.

    EXTRA_ARGS: one whitespace-separated string of extra llama-server args,
    word-split at startup and appended LAST to the argv (duplicate flags
    last-wins -> overrides the baked base flags); 'none' = off.
    """
    env = {
        "GPU_ID": "0",
        "MODEL_REPO": model_repo,
        "MODEL_FILE": model_file,
        # Hybrid model (1 in 4 layers is full attention): q8_0 KV ≈ 35 KiB/token
        # — ~3.0 GiB at the 90000 default, ~4.6 GiB at 132768.
        "CTX_SIZE": ctx_size,
        "N_GPU_LAYERS": "99",
        "NAME": name,
        "MODEL_ALIAS": model_alias,
        # Optional auxiliary models — "none" = off (never empty: the
        # SaladCloud API rejects empty env values, minLength 1).
        "DRAFT_MODEL": draft_model,
        "VISION_MODEL": vision_model,
        "SPEC_TYPE": spec_type,
        # Claude Code template: '1' = dump the gguf's own template at startup
        # and apply the one-line mid-conversation-system patch (--no-claude-
        # template sends 'none' -> embedded template as-is).
        "CLAUDE_TEMPLATE": claude_template,
        # Extra llama-server args: one whitespace-separated string, appended
        # LAST to the argv (last-wins override of the baked base flags);
        # 'none' = off (never empty: the API rejects empty env values).
        # `--flag none` PAIRS are stripped (strip_none_value_args) so
        # llama.cpp keeps its default parsing for those features.
        # Harmless on images that predate EXTRA_ARGS (the env is simply
        # unread).
        "EXTRA_ARGS": strip_none_value_args(extra_args),
        # Idle/heartbeat self-shutdown (in-container idle_watchdog.py):
        # 'none' = off; 'idle' = kill after IDLE_TIMEOUT s of no chat
        # traffic; 'heartbeat' = kill after HEARTBEAT_TIMEOUT s of no client
        # keepalive pings. Always sent (PATCH replaces the env wholesale);
        # harmless on images that predate the watchdog (the env is unread).
        "IDLE_SHUTDOWN": idle_shutdown,
        "IDLE_TIMEOUT": idle_timeout,
        "HEARTBEAT_TIMEOUT": heartbeat_timeout,
        # Watchdog arming window: max seconds to wait for the first /ready ok
        # before it gives up (no kill). Big-quant cold starts exceed the 1800
        # default — the watchdog then exits and the group runs UNARMED.
        "IDLE_GRACE": idle_grace,
        # SSH-presence guard (watchdog v2.2 + bandwidth arbitrator): while
        # /.ssh exists in the container the watchdog holds off its self-stop
        # and the arbitrator spends no /reallocate shot. The user touches
        # /.ssh after logging in over SSH and removes it on the way out.
        # Always sent; harmless on images predating the guard (the env is
        # unread).
        "SSH_GUARD": ssh_guard,
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
    if hf_token:
        env["HF_TOKEN"] = hf_token
    return env


def redact(env: dict[str, str]) -> dict[str, str]:
    """Mask secrets for printing (HF_TOKEN and SALAD_STOP_KEY are secrets)."""
    return {k: "<REDACTED>" if k in ("HF_TOKEN", "SALAD_STOP_KEY") else v
            for k, v in env.items()}


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
        print("      stop key loaded (account-wide key, stored b64-obfuscated, never printed)")
    env = build_env(group, args.ctx_size, args.model_repo, args.model_file,
                    args.model_alias, args.draft_model, args.vision_model,
                    args.spec_type, hf_token,
                    claude_template="none" if args.no_claude_template else "1",
                    extra_args=args.extra_args,
                    idle_shutdown=args.idle_shutdown,
                    idle_timeout=args.idle_timeout,
                    heartbeat_timeout=args.heartbeat_timeout,
                    idle_grace=args.idle_grace,
                    ssh_guard=args.ssh_guard,
                    stop_key=stop_key, org=org, project=project, group=group)

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
        if args.idle_shutdown != "none" and stop_key == "none":
            print(f"      WARNING: --idle-shutdown {args.idle_shutdown!r} WITHOUT --stop-key — "
                  "the in-container watchdog DISABLES itself when SALAD_STOP_KEY='none' (no "
                  "self-shutdown at all): the group runs until stopped externally — billing "
                  "continues. (Self-exit without the /stop call is worse than nothing: Salad "
                  f"reschedules a new instance — live restart_policy here: {live_policy!r}.) "
                  "Pass --stop-key <key file> so the watchdog can POST the group /stop — the "
                  "only real stop.")
        elif args.idle_shutdown != "none" and live_policy == "always":
            print("      NOTE: armed with --stop-key — the watchdog's group /stop call stops "
                  f"the group at GROUP level, so the live restart_policy={live_policy!r} is "
                  "fine (the stop endpoint wins over container-restart policy; no "
                  "delete+recreate needed).")
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
              f"disk={int(round(args.disk_size * 1024**3))} bytes, priority={args.priority}, "
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
            # 'never' when the watchdog is armed: the watchdog's real kill is
            # the group /stop POST (--stop-key), and 'never' additionally
            # stops crash-loop billing on any self-exit — self-exit alone
            # gets RESCHEDULED, not stopped (paid test 2026-10-09).
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
            priority=args.priority,
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
          "a cold worker re-downloads the main model, ~18.8 GiB for ATX Q5_K_M, "
          "plus the optional draft/vision if set)...")
    wait_for("running", 1800, org, project, group)
    print_group(get_group(org, project, group))
    print("OK: group running. 'running' means the container process is up —")
    print(f"      1. verify the LIVE build in-container: version.sh should print")
    print(f"         'lmss generic-v3 (hf runtime download, claude-template, extra-args) 2026-10-07'")
    print(f"      2. PID1 cmdline should carry --spec-type {args.spec_type}"
          + (f" --model-draft ..." if args.draft_model != "none" else " (no --model-draft, "
             f"DRAFT_MODEL={args.draft_model})")
          + (f" --mmproj ..." if args.vision_model != "none" else " (no --mmproj)")
          + f" --alias {args.model_alias}"
          + (f", and the EXTRA_ARGS tail appended last: {args.extra_args}"
             if args.extra_args != "none"
             else " (EXTRA_ARGS=none — no extra args)"))
    print(f"      3. gateway answers only after the probe passes (/ready, ~40-min window, first probe at 30 s):")
    print(f"         claude/curl_salad.sh -url https://{dns} -m {args.model_alias}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SaladApiError, ValueError, TimeoutError) as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
