# llama.cpp in Docker — local + SaladCloud

Dockerized [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` for GGUF models, pinned to **one** NVIDIA GPU via `GPU_ID`. OpenAI-compatible API on the host port you choose. The same images run two ways:

- **locally** (docker compose, `docker/`) — this box's 16 GB cards → the 9B service;
- **on SaladCloud** — one card per container, public `*.salad.cloud` gateway; this is where the 27B Claude Code backend lives (deploy + run it on demand, switching cards and quants).

Image build: llama.cpp compiled with CUDA + flash attention + NCCL (`-DGGML_CUDA_NCCL=ON`, pinned commit `3af988fab`, build tag b10572) in a CUDA devel stage, shipped on the `nvidia/cuda:12.8.2-runtime` base (`Dockerfile.multistage`). Binary at `/opt/llama.cpp/build/bin/llama-server`. Also ships a small status API on port **9999** (`/startup`, `/live`, `/ready` — see [Status API](#status-api-port-9999)) and debug tools inside the container: `curl`, `ssh`, `scp` (ships with `openssh-client`), `vi`, `htop`, `bmon` (interface monitor, for eyeballing throughput by hand), `nvtop` (GPU monitor, built from source — not in Ubuntu 22.04 repos), `ping` (`iputils-ping`; needs `CAP_NET_RAW`, so `docker run --cap-add=NET_RAW` — Salad workers may deny it, and `ping` will report "Operation not permitted"), and `llama-stats` (symlink to `stats.sh`: local-only llama-server stats, one plain command, so it works under Salad's shell-less SSH exec). The fourth image is a **custom build of a llama.cpp fork** (llamAmpere, TurboQuant KV cache) — [the fork build](#the-llamampere-fork-build-lmss_generic_ampere).

Models are downloaded on first start into `/models` **inside** the container (no host bind mount), so `docker compose down` removes them — nothing is left behind on the host disk.

### Image family

| Image | Built from | Baked in | Feature env vars (all accept the `none` sentinel = off) |
|---|---|---|---|
| `boris271142/lmss:cuda128-v8` | `Dockerfile.multistage` (base; local compose) | nothing — model downloads on first start | `CHAT_TEMPLATE` (jinja **file path**), `DRAFT_MODEL_FILE` / `DRAFT_MODEL_URL`, `VISION_MODEL_URL` |
| `boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision` (tag `cuda128-v10`) | `Dockerfile.lmss_q6_mtp_vision` (FROM base v8) | 27B MTP draft (Q8_0, 2.95 GiB) + vision mmproj (F16, 0.86 GiB) — only the main model downloads at runtime (fast `hf` path) | `CLAUDE_TEMPLATE`, `USE_DRAFT_MODEL` — vision is always on |
| `boris271142/lmss_generic` (tag `cuda128-v8`) | `Dockerfile.lmss_generic` (FROM base v8) | nothing — model-agnostic; main model + optional draft / vision all download at runtime via `hf` | `CLAUDE_TEMPLATE`, `DRAFT_MODEL`, `VISION_MODEL` (`hf://org/repo/file` or a bare file against `MODEL_REPO`), `SPEC_TYPE` (`draft-mtp` \| `ngram-mod` \| `none`) |
| `boris271142/lmss_generic_ampere` (tag `cuda130-v6`) | `Dockerfile.lmss_generic_ampere` (2-stage: the **llamAmpere v0.4 fork** compiled in-container on CUDA 13.0.2, sm_86+89) | the fork binary itself + TurboQuant KV flags `--cache-type-k turbo5 --cache-type-v turbo4 --kv-unified --fit off --cache-ram 4096` in the CMD + MTP vocab maps at `/opt/llama.cpp/mtp-vocab/` — still no models | same as `lmss_generic` + `EXTRA_ARGS` (one string of extra args appended LAST, last-wins) |

> **CMD generation (2026-10-09, the `entry.sh` fix).** Every image's runtime
> script now lives in a standalone `docker/entry_*.sh` COPYed to
> `/opt/llama.cpp/entry.sh`, launched by a plain 3-token exec-form CMD
> `CMD ["/bin/bash", "-l", "/opt/llama.cpp/entry.sh"]`. The previous
> generation inlined the whole startup script as a multi-line JSON-exec CMD
> (`CMD ["/bin/bash","-lc","…"]`); that blob was malformed (a stray `"` at the
> tail), so buildkit's `parseMaybeJSON` **silently** fell back to shell form,
> re-encoded it as `/bin/sh -c '["/bin/bash",…]'`, and dash choked → **exit 2
> crash-loop ~1–2 s after "Running"** on every worker. A plain exec-form CMD
> carries no embedded JSON, so it cannot be mis-parsed. The base is
> `cuda128-v8`, generic `cuda128-v8`, q6 `cuda128-v10`, ampere `cuda130-v6`
> (v8/v10/v6 = the `bw_reporter` + `iputils-ping` + `llama-stats` rebuild of
> 2026-10-09, on top of the watchdog-v2.1 b64-key build).

A Salad group's env **replaces** the image env wholesale, so a plain deployment sets the `none` sentinels explicitly rather than relying on the image defaults (which also future-proofs against an older image whose defaults were real URLs).

### The llamAmpere fork build (`lmss_generic_ampere`)

The fourth image ships a **custom build of a llama.cpp fork**: [JakeATX/llamAmpere](https://github.com/JakeATX/llamAmpere) v0.4 (cloned at `llama.cpp/` in the repo root — gitignored, **never committed**). Upstream llama.cpp cannot run the ATX-Swift 27B MTP profile at 131k ctx on a 24 GB card — the q8_0 KV cache alone would be ~3 GiB at 90K ctx. The fork adds **TurboQuant KV-cache quantization** (`turbo2`–`turbo6`: a 128×128 Walsh-Hadamard rotation before quantization); `turbo5`/`turbo4` (= `tq5_0`/`tq4_0`) cost ≈ 45% of the q8_0 KV size, which is what lets 131k ctx fit in 24 GB.

**Baked in** — unlike the rest of the family, where the *binary* is upstream and only args differ, here the llama.cpp binary itself is the fork:

- the fork binary, compiled **in the image** on CUDA 13.0.2, arches `86;89` (86 = the RTX 3090 deploy target, 89 = the local smoke box)
- `--cache-type-k turbo5 --cache-type-v turbo4 --kv-unified --fit off --cache-ram 4096` in the CMD
- the MTP spec-draft vocab maps at `/opt/llama.cpp/mtp-vocab/` — the deployer's `EXTRA_ARGS` points `--spec-draft-vocab-map` at them

Everything else — the `hf` download machinery, the control surface (`DRAFT_MODEL` / `VISION_MODEL` / `SPEC_TYPE` / `CLAUDE_TEMPLATE` / `EXTRA_ARGS`, the last one appended LAST so group args override the baked base flags), the status API, the probes — is identical to `lmss_generic`. There is **no per-tuning image**: the per-model operating point comes from the group env + `EXTRA_ARGS` (the deployer's defaults ARE the ATX profile from `tmp/post.txt`).

**Why the compile runs inside the image (and never as a host-build overlay):** the build host (Ubuntu 24.04, gcc 13) produces binaries that need glibc 2.38; every base in this family is ubuntu22.04 (glibc 2.35), so a host build cannot run on any of them. Stage 1 compiles the fork from source inside `nvidia/cuda:13.0.2-devel-ubuntu22.04` so the glibc floor matches. Rebuild rule: always build in-container like this; never COPY a host `build/` tree from a newer distro into stage 2.

```bash
# Build (context = REPO ROOT; the root .dockerignore keeps llama.cpp/.git,
# llama.cpp/build and the secret files out of it). ~45 min at 24 jobs.
docker build -f docker/Dockerfile.lmss_generic_ampere \
  -t lmss_generic_ampere:dev --build-arg BUILD_JOBS=24 .

# Push: the NEW repo name is a fresh Salad worker image cache key, then pin
# the manifest-list digest in deploy_ampere.py (the airtight re-pull lever).
docker tag  lmss_generic_ampere:dev boris271142/lmss_generic_ampere:cuda130
docker push boris271142/lmss_generic_ampere:cuda130
```

Stage 1: cmake Release, `GGML_CUDA=ON`, `GGML_CUDA_NCCL=OFF` (single-GPU image — this also drops the `libnccl.so.2` DT_NEEDED the multistage Dockerfile has to satisfy by hand), `-j${BUILD_JOBS}` (default 24 — nvcc/cicc instances are memory-hungry), and a `libcuda.so.1` stub symlink on the link-time rpath-link (the real driver is injected by nvidia-container-toolkit at runtime). Stage 2: `nvidia/cuda:13.0.2-runtime-ubuntu22.04` + the family's apt/pip set + the control-surface COPYs + the vocab maps + the build id in `/etc/llama-image-build` (read by `version.sh`). CUDA 13 for this image because the deploy target is the RTX 3090; the RTX 5090 keeps the CUDA 12.8 multistage image.

**Validated (2026-10-07):** `llama-server --help` lists `turbo2 (tq2) … turbo6 (tq6_0)` for `-ctk`/`-ctv` — names upstream does not know, so their acceptance is proof of the fork binary; `tq5_0`/`tq4_0` kernel references present in `libggml-cuda.so`; a live run on the 4080 SUPER (sm_89) with the full baked turbo KV flag set + `--flash-attn on` produced coherent generation at 109 t/s (a missing turbo kernel would have errored, not run). Pushed as `cuda130` @ `sha256:f9532a85…cc45a`.

## Prerequisites

- Docker + compose v2
- `nvidia-container-toolkit` installed (so `device_ids` GPU pinning works)
- Free VRAM on the target card: ~23 GB for 27B at 131k ctx (noMTP Q4_K_M weights + MTP draft + q8_0 KV cache), ~6 GB for 9B at 32k ctx

## Qwen3.8

Everything in this section is specific to the Qwen3.8 27B/9B line. The image and the [deployers](#deployers) are model-agnostic — any GGUF repo works with `deploy/deploy_generic.py`.

### Choosing quant + context length by VRAM

**Target: min 90K ctx, 128K ideal** (Claude Code's working context). Weights + KV cache + ~2.1 GiB of CUDA/compute/vision overhead must fit on the card. With the image's q8_0 KV cache, KV costs ≈ 35 KiB/token (≈ 30K tokens per GiB free):

**max_ctx ≈ (VRAM_GiB − 2.1 − weights_GiB) × 30K**

Calibrated on the 3090 — all three live data points match within a few percent: Q6_K serves exactly 30208, Q5_K_M serves 90112, Q4_K_M ran 132768 at ~21 GB live.

**Meeting the target:**

| Card | ≥ 90K (floor) | ≥ 128K (ideal) |
|---|---|---|
| **32 GB (5090)** | Q6_K @ 90K (near-lossless) | **Q6_K @ 128K** (near-lossless, max ~270K) — the clean choice |
| **24 GB (3090/4090)** | **Q5_K_M @ 90K** (current production, max ~110K) | **Q4_K_M @ 128K** (max ~185K) — Q5_K_M is ~0.6 GiB short of the KV budget, so 128K on 24GB costs a quant drop |
| **16 GB (4060 Ti / 5060 Ti)** | IQ2_M @ 90K (steep quality cut, max ~120K) | doesn't fit — IQ2_M maxes ~120K |

**Bottom line:** 128K at good quality only lands on a 32 GB card — **Q6_K @ 128K on a 5090** (the 2×16 GB local rig serves Q6_K at 122768, and a 5090 does it on one card). On the current 24 GB 3090, 90K = Q5_K_M with no quality drop, but 128K forces Q4_K_M. A 16 GB card reaches 90K only at IQ2_M, a heavy cut for a 27B.

**Full max-ctx reference** (highest ctx each quant reaches per card, from the formula):

| Quant | Size | 16 GB | 24 GB | 32 GB |
|---|---|---|---|---|
| Q8_0 | 27.05 GiB | — | — | ~85K |
| Q6_K | 20.89 GiB | — | ~30K (verified ceiling 30208) | ~270K |
| Q5_K_M | 18.19 GiB | — | ~110K (current prod: 90000 → served 90112) | 262144 (trained ceiling) |
| Q4_K_M | 15.66 GiB | — | ~185K | 262144 |
| IQ4_XS | 14.26 GiB | — | ~225K | 262144 |
| IQ2_M | 9.90 GiB | ~120K | 262144 | 262144 |

Notes:
- Repo `JonathanColetti/Qwen3.8-27B-Uncensored-GGUF`. Use the plain `Q…` / `IQ…` files when running **without** a separate draft file (the baked q6-mtp-vision image: `USE_DRAFT_MODEL=none` → self-speculation from the model's embedded MTP/nextn layers). The `noMTP-…` files have no embedded MTP layers and crash llama-server at load **unless** you feed them a separate draft (the local multistage default: noMTP-Q4_K_M + `DRAFT_MODEL_FILE` draft-Q8_0; the v5 image: `USE_DRAFT_MODEL=<file>`).
- The 27B's trained context is **262144** — serving above it buys nothing; that's the table's ceiling.
- The ~2.1 GiB overhead includes the ~1 GiB vision projector the baked q6-mtp-vision image always loads; a text-only profile gets it back. A separate draft file (~3 GiB for draft-Q8_0) costs that much ctx budget.
- Numbers assume `--cache-type-k/v q8_0` (the image default). f16 KV doubles the KV cost → halve the ctx.
- The [ampere fork image](#the-llamampere-fork-build-lmss_generic_ampere) changes the 24 GB card: its baked turbo5/turbo4 KV costs ≈ 16 KiB/token (~45% of q8_0's ~35), so each GiB of headroom buys ~60K ctx instead of ~30K. On a 3090: **Q5_K_M reaches the full 132768** (18.77 weights + 0.87 mmproj + ~2.1 GiB KV ≈ 21 GiB — the ATX profile) and Q6_K + vision fits at 90K.

### Run locally (docker compose)

Full command with every overridable argument spelled out (defaults shown = the local compose profile: noMTP-Q4_K_M + draft-Q8_0 @ 131072). The 27B does NOT fit this box's 16 GB cards; locally, use the 9B service. The live SaladCloud production setup (Q5_K_M @ 90K on a 3090) is described in [SaladCloud](#saladcloud).

```bash
cd /dd2/andrei/docker/on_salad/docker && \
GPU_ID=0 \
HOST_PORT=8080 \
MODEL_REPO="JonathanColetti/Qwen3.8-27B-Uncensored-GGUF" \
MODEL_FILE="Qwen3.8-27B-Uncensored-noMTP-Q4_K_M.gguf" \
CTX_SIZE=131072 \
N_GPU_LAYERS=99 \
THREADS=8 \
BATCH_SIZE=256 \
UBATCH_SIZE=256 \
HF_TOKEN="" \
timeout 30 docker compose up -d
```

#### Arguments

| Arg | Change it if… |
|---|---|
| `GPU_ID` (script arg) | First argument of `run_27b.sh` / `run_9b.sh` (or export it before a raw `docker compose up`): selects the physical card by nvidia-smi index via `device_ids`. Defaults: 27B → `0`, 9B → `1`. Exposing a single card also stops llama.cpp spreading layers across all visible GPUs. On this box (verified 2026-10-01): `0` = RTX 4080 SUPER (16 GB), `1` = RTX 4060 Ti (16 GB) — the 27B needs ~23 GB free on one card, so it does not fit locally (use the [SaladCloud](#saladcloud) group); the 9B fits either. Check free VRAM first with `nvidia-smi` |
| `HOST_PORT` | :8080 is taken by another server → use e.g. `8090` until you free it |
| `MODEL_REPO` / `MODEL_FILE` | Different quant or model. Default (verified in `JonathanColetti/Qwen3.8-27B-Uncensored-GGUF`): the noMTP `Q4_K_M` build — its MTP draft module is not embedded, it comes from `DRAFT_MODEL_FILE`. The repo's other variants (`Q4_K_M`, `Q5_K_M`, `Q6_K`, `Q8_0`, `IQ2_M`, `IQ4_XS`) embed the draft — run them with `DRAFT_MODEL_FILE=""`. Other 27B repos (unsloth, bartowski) have no draft file: also `DRAFT_MODEL_FILE=""`. For the 9B smoke test: repo `empero-ai/Qwen3.8-9B-Distill-GGUF`, file `Qwen3.8-9B-Q4_K_M.gguf` (the 9B service sets no draft env → the image default `none`) |
| `DRAFT_MODEL_FILE` | MTP draft for `--spec-type draft-mtp` speculative decoding. Default `Qwen3.8-27B-Uncensored-draft-Q8_0.gguf` (~3 GB, same repo as the default model; resolved via `hf` against `MODEL_REPO`). Set to `""` to disable — the server falls back to n-gram self-speculation (needed for models whose draft isn't in `MODEL_REPO`, or on VRAM-tight cards) |
| `CHAT_TEMPLATE` | Jinja chat template **file path** passed as `--chat-template-file` (NOT the inline `--chat-template` flag — that one takes the template *text*; a path there becomes the literal prompt and the model degenerates into a loop of the path string, e.g. `/opt/llama.cpp/qwen3.8.q6.gguf` spam). Default empty/`none` → the template embedded in the GGUF is used (the default Uncensored model already embeds a permissive one that accepts system messages anywhere). The Claude Code variant is handled automatically by `CLAUDE_TEMPLATE` in the v5/generic images — see [The Claude Code template](#the-claude-code-template) |
| `CTX_SIZE` | Lower it (e.g. `32768`) if VRAM is tight or you don't need 131k — KV cache scales with this |
| `N_GPU_LAYERS` | Keep `99` for full offload; lower only if the GPU is shared and you want some layers on CPU (slower) |
| `THREADS` / `BATCH_SIZE` / `UBATCH_SIZE` | Rarely needed; leave as-is unless tuning throughput |
| `HF_TOKEN` | Only if the repo is gated or HF rate-limits you (`hf auth token` to get one) |
| `API_KEY` | llama-server's auth key. The run scripts read it from `api.txt` in this folder (they cd here first; chmod 600); missing file → server runs without key auth. `curl1.sh` / `curl2.sh` (in `tests/` at the repo root) read it through the local `tests/api.txt` symlink |

Notes:
- The `timeout 30` wrapper just guards against a hung build — with the image already built, `up -d` returns in seconds. Drop it if you prefer.
- If you edit the Dockerfile later, run `docker compose build` first (or add `--build`). Compose reuses the existing `boris271142/lmss:cuda128-v3` image otherwise.
- First start downloads the model file into the container's `/models`; restarting a stopped container loads it from disk in seconds, but `docker compose down` removes it (the next `up` re-downloads).

#### Smoke test variant — permanent 9B service on the RTX 4060 Ti

The 9B server is a second compose service (`qwen38-9b`, host port **8081**), pinned to the RTX 4060 Ti via `device_ids: ["1"]`. Same image as the 27B; only env vars differ.

```bash
cd /dd2/andrei/docker/on_salad/tests && ./run_9b.sh   # default: RTX 4060 Ti (nvidia-smi index 1)
./run_9b.sh 0                                                       # run it on a different card instead
```

Model + KV ≈ 5.8 GiB VRAM, ~100 tok/s generation (measured 2026-08-23 on the previous box's RTX 3080 Ti; the 9B fits either 16 GB card on this box). `curl2.sh` targets it on :8081 (`curl1.sh` checks :8080).

### The Claude Code template

Claude Code speaks the Anthropic Messages API: requests arrive as `/v1/messages` with `system` messages that can appear **mid-conversation** (per-turn instructions). Qwen3.8's stock template raises on that (`Jinja Exception: System message must be at the beginning`), so for Claude Code the served template must be the patched one. The image handles it at startup instead of shipping a static template:

- **v5 / generic images — `CLAUDE_TEMPLATE=1`** (the default in both deployers): at startup the image dumps the chat template from the freshly downloaded gguf (`claude_template.sh`) and applies the one-line mid-conversation-system patch, passing the result to llama-server via **`--chat-template-file`**. `CLAUDE_TEMPLATE=none` (deployer flag `--no-claude-template`) serves the gguf's embedded template as-is — what cline / python / opencode want. Non-Qwen ggufs (no raise-marker) get the patch skipped automatically.
- **base v3 image (local compose) — `CHAT_TEMPLATE=<path>`**: a jinja **file path** (see the Arguments table above); opt in with `CHAT_TEMPLATE=/opt/llama.cpp/qwen3.8.q6.jinja` for models whose embedded template is strict.

**Gotcha (root cause of the old path-loop):** the template must go through `--chat-template-file`. The inline `--chat-template` flag takes the template *text* — pass a path there and it becomes the literal prompt, and the model degenerates into a loop of the path string.

Verified e2e (2026-10-05, local GPU): the dumped template is the gguf's own 170-line template (only diff vs the reference file is whitespace on one line — renders byte-identical); a mid-conversation system message is honored (the answer comes back in the instructed language); negative control `CLAUDE_TEMPLATE=none` → HTTP 500 "System message must be at the beginning" at exactly the line the patch rewrites.

## SaladCloud

One card per container: Salad allocates the GPU, and inside the container it is always index `0` (hence `GPU_ID=0` in every group env). A group with `auth` on gets a public `*.salad.cloud` gateway (443 in front of the group's HTTP port 8888) that requires the `Salad-Api-Key` header.

Common shape of every group here:

- **Readiness probe**: HTTP `GET /ready` on 8889 (the status API), sized to survive a cold model download on a fresh worker — the 27B groups use 30 s initial delay + 20 × 120 s ≈ 40 min failure window (the spec caps: delay 1200, period 120, failure_threshold 20). The early 30 s first probe (not the 1200 s max) keeps the gateway — and cloudflare in front of it — open the moment the model is actually ready.
- **On-demand**: autostart off + scheduled scaling — groups sit stopped (no cost) until someone starts them.
- **The worker image cache is keyed by REPO NAME**, not tag or digest: a re-pushed tag can still serve a stale image on workers that cached the old one (observed live on `lmss:cuda128` on 2026-10-01). Never overwrite a tag a worker may hold; the airtight lever is a digest-pinned `@sha256:` ref to a digest no worker has seen (the API accepts it verbatim — both smart deployers pin digests). With a plain tag, verify the live build with `version.sh` in the container.
- **DELETE leaves a name tombstone**: recreating a just-deleted group name 400s `name_conflict` for 10+ min. A fresh group name works immediately; keep `MODEL_ALIAS` stable so clients don't notice.
- **Keys & projects**: `SALAD_API_KEY` comes from `deploy/salad_api.txt` (gitignored, never printed), read by the stdlib-only `salad_client.py` (repo root); one key covers every org on the account. Projects have no API create endpoint (web UI only), but container-group creation auto-creates a missing project, which the deployers rely on.

### Idle/heartbeat self-shutdown (`idle_watchdog.py`)

Salad bills while a group runs. If a client session dies (lost connection, closed terminal), an idle group burns credits for hours. Every image ships a stdlib-only watchdog — a fourth `nohup … &` helper in the CMD — that **stops the group from inside the container**: it POSTs the Salad group `/stop` endpoint (with the account API key, stored b64-obfuscated), then SIGTERMs the container. The watchdog is the only component that can end billing after the client is gone.

> **Why the stop API (paid test, 2026-10-09).** The v1 design assumed container exit + `restart_policy=never` leaves the group STOPPED. **Falsified**: a container exit under `restart_policy=never` does NOT stop the group — Salad **reschedules a new instance** (the group stays `running`, billing continues; `restart_policy` governs container restarts *within* an instance only). The live test burned a full billing loop before manual stop. Only the group `/stop` endpoint truly stops a group, so the watchdog calls it. `restart_policy=never` is still set on the create path — it kills crash-loop billing on any self-exit — but it is not the kill mechanism.

- **Kill mechanism (v2):** on timeout the watchdog `POST`s `…/containers/{group}/stop` (header `Salad-Api-Key`, no body, 202 = accepted; a custom `User-Agent` is mandatory — the CDN 403s the default urllib UA), **then** SIGTERMs PID 1 as the local teardown. The key is the **account-wide per-user API key** — group-scoped keys do not exist (verified against the Salad docs); the deployer passes it via `--stop-key <file>`. It enters the container **b64-obfuscated**: the deployer stores `SALAD_STOP_KEY=b64:<base64>`, the watchdog detects the `b64:` sentinel and decodes at startup. This is basic obfuscation, not encryption — the group env is the exposure surface, and base64 hides the key's shape from casual `env` output/screenshots while keeping the deployer's file the only plaintext copy. **`SALAD_STOP_KEY=none` (or missing, or an undecodable `b64:` payload) DISABLES the watchdog entirely** — it exits at once, feature off. A keyless self-exit gets **rescheduled**, not stopped (that was v1's billing defect), so a SIGTERM-only fallback is pointless; disabling is the clean behavior.
  - SIGTERM reaches the server because llama-server runs as a **trapped child** of the CMD bash (PID 1): the kernel **drops every signal — SIGKILL included — sent inside a PID namespace to a handler-less PID 1**, so a watchdog can never kill a handler-less init; the entry scripts install a SIGTERM trap, bash forwards it to the server child, and bash exiting tears down the namespace (verified in-container 2026-10-08).
- **Arm point (v2):** the watchdog arms on `http://127.0.0.1:9999/ready` — the **group readiness endpoint the Salad probe itself uses** (api_app, mirroring llama-server `/health`) — not on llama's `/health` directly. v1 armed on `/health` and could kill **before the probe ever flipped the group `running`** (the paid test's second defect). After `/ready` goes ok it holds `IDLE_ARM_GRACE` (default 150 s ≥ the 120 s probe period) so the probe marks the group running and clients get a connect window **before** the countdown starts. Arming gives up after `IDLE_GRACE` s (default 1800) — it never kills mid-download.
- **`IDLE_SHUTDOWN=none`** (the baked default, and every deployer's default) → the watchdog exits at once; existing groups keep today's behavior.
- **`IDLE_SHUTDOWN=idle`** → kill after `IDLE_TIMEOUT` s (default 600) with no chat traffic.
- **`IDLE_SHUTDOWN=heartbeat`** → kill after `HEARTBEAT_TIMEOUT` s (default 600) with no client keepalive. Salad exposes exactly **one** service port, so the keepalive is not a dedicated port: the client (`cl_salad` / `chat_salad.sh` with `SALAD_HEARTBEAT=1`) posts a **1-token completion** through the existing gateway every 30 s. llama-server IS the agent — its token counters are the heartbeat signal.
- **Activity signal**: `llamacpp:prompt_tokens_total` + `llamacpp:tokens_predicted_total` scraped from `/metrics` every 30 s (all CMDs bake `--metrics`; adaptive to a shorter period when the timeout is small). Only chat calls move the counters — `/health` and `/v1/models` polls do NOT count. An in-flight request (`llamacpp:requests_processing` > 0) counts as activity; any counter change (including a reset) resets the timer.
- **Safety rails**: a failed `/metrics` scrape is a skipped tick, never an idle tick; the stop POST is retried once; the key is never logged; a malformed `b64:` payload logs the decode failure and disables the watchdog (never crashes it).
- **Group side**: armed mode = `--idle-shutdown idle|heartbeat` **plus `--stop-key <file>`** (the deployer refuses to arm loudly — it warns — without it). Deployers also create armed groups with `restart_policy=never` (create-only, not in the PATCH schema) so crash-loop self-exits stay stopped. The group-level stop wins over `restart_policy`, so an existing `always` group does NOT need delete+recreate for the watchdog to work.
- Env knobs: `IDLE_SHUTDOWN`, `IDLE_TIMEOUT`, `HEARTBEAT_TIMEOUT`, `IDLE_GRACE`, `IDLE_ARM_GRACE`, `READY_PORT` (9999), `SALAD_STOP_KEY` + `SALAD_ORG`/`SALAD_PROJECT`/`SALAD_GROUP` (the stop path; all four required, set by the deployer), `SALAD_API_BASE` (override for tests only).
- Log: `${API_STATE_DIR}/watchdog.log` (i.e. `/tmp/llama-api/watchdog.log`); live check `ps -eo pid,args | grep idle_watchdog`. `version.sh` prints the **WATCHDOG generation** — from the PID1 cmdline, or (on the exec-form `entry.sh` images, where PID 1 is just `bash -l /opt/llama.cpp/entry.sh`) by fingerprinting the `entry.sh` script itself — and flags **watchdog v2** when `SALAD_STOP_KEY` is in the script, plus **watchdog v2.1** when the script carries the `b64:` sentinel (b64 key decode + key `none` disables the watchdog).

### HF-download bandwidth reporter (`bw_reporter.py`) + the arbitrator

Salad's residential nodes vary wildly in bandwidth, and a 10–30 GB GGUF download is the
bulk of a cold start. The container side **only reports**; all verdicts and the kill
lever live in `utils/manage_groups.py`.

- **What it does**: a fifth `nohup … &` helper in the CMD. Every 10 s it sums the file
  sizes under `MODEL_DIR` and appends `epoch bytes` to `${API_STATE_DIR}/bw.log`
  (i.e. `/tmp/llama-api/bw.log`), then self-exits once llama-server `/health` answers
  ok — the same probe `api_app._ready_state` uses, so download+load done = sampling
  done. It writes a sample on **every** tick, so a frozen log means the reporter is
  dead (the manager's bail signal), not that the download is quiet.
- **Why file growth, not `bmon`**: `bmon -o ascii` (also in the image, for manual use)
  measures the NIC — watchdog ticks, status-API probes and the manager's own SSH
  sessions all inflate it — and its output is a periodic human-readable block with no
  epoch timestamps. `MODEL_DIR` growth is the ground truth of "how fast the model is
  arriving": xet retries, TLS overhead and re-transmissions show on the wire, not in
  the file. Sizes are summed with `os.lstat` because the model file in `MODEL_DIR` is a
  **symlink** into the blob cache — `os.stat` through it counts the blob twice.
- **Never dies**: the whole loop body is wrapped in `try/except`; a failed sample is
  logged to `${API_STATE_DIR}/bw_reporter.log` and sampling continues.
- **The arbitrator** (`manage_groups.py start --min-bw-mbps N`, default 30): reads the
  log tail over SSH every 15 s, takes the median rate over a 120 s window after a 60 s
  ramp grace, and below the cap POSTs the instance `/reallocate` endpoint — new node,
  rejected node excluded from the account pool for 48 h. Max 3 shots, **4th host kept**.
  The group is **never stopped** for bandwidth. See `docs/README.md` → *Bandwidth
  arbitrator*; live check `ps -eo pid,args | grep bw_reporter`, and `version.sh` prints
  the **BW-REPORTER generation**.

The 27B Claude Code backend: org `ma-casa-in-paris`, project `qwen38-27b`, one **RTX 3090 (24 GB)**, on-demand. Live config (GET-verified 2026-10-06):

- **Image**: baked `boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision` — deployer pins the `cuda128-v10` digest (bw_reporter + ping + llama-stats, 2026-10-09; older tags deleted from the registry per user request 2026-10-09, digests kept as history in the deploy script: v9 `755c43df…` = watchdog v2.1, v8 `7d635166…`, v7 `b7ea4893…` = entry.sh CMD fix, v6 `97aa3d72…`, v5 `4089a457…`). The MTP draft (Q8_0, 2.95 GiB) and mmproj (F16, 0.86 GiB) are baked in, so at runtime **only the main model** is downloaded from HF (fast `hf` path).
- **Env**: `MODEL_FILE=Qwen3.8-27B-Uncensored-Q5_K_M.gguf` (18.19 GiB, MTP head embedded), `CTX_SIZE=90000` (served n_ctx **90112** — rounded up to a block multiple), `MODEL_ALIAS=qwen38-27b` (stable — clients reference the alias, not the group name), `CLAUDE_TEMPLATE=1`, `USE_DRAFT_MODEL=none` (self-speculation from the embedded MTP head; the baked draft is only for noMTP quants), `N_GPU_LAYERS=99`, + `HF_TOKEN` when `hft.txt` validates. q8_0 KV, flash-attn, vision on. See [Choosing quant + context length by VRAM](#choosing-quant--context-length-by-vram).
- **Resources**: cpu 8, 16 GB RAM, 50 GB disk, shm 64, replicas 1, restart always, priority low.
- **Gateway**: `https://corn-cabbage-2yk4e98r3rx752n0.salad.cloud` (443, `Salad-Api-Key` header).
- The group name `q6k` is a leftover from the original Q6_K deploy and is kept — renaming means delete + recreate = new DNS + the name-tombstone dance.

- **Deploy**: `python3 deploy/deploy_qwen38_27b.py` — create-or-update in place (stop when running → PATCH image + full env → start), which keeps the group's DNS stable. A bare run re-applies exactly the production profile above (the built-in defaults are it) — idempotent against the live group. Flags: `--org`, `--project`, `--group`, `--gpu rtx5090|rtx3090`, `--model-file`, `--ctx-size`, `--image`, `--use-draft-model`, `--no-claude-template`, `--no-start`. Caveat: the PATCH sends env **wholesale** (and resources are not patchable at all), so every value comes from the flags/defaults, not from whatever is currently live — if you change the group's env out-of-band, update the defaults before the next deploy run.
- **Cold start**: every stop→start re-downloads the main model (~10 min observed for Q5_K_M, ~26 min for Q6_K) — the probe window above is sized for it.
- **HF token**: `hft.txt` next to the deploy scripts (gitignored, chmod 600). The deployer validates it (`whoami-v2`) and, if it passes, adds `HF_TOKEN` to the group env; the image CMD forwards it to `hf download` (authenticated, faster pulls). Missing/invalid → skipped with a warning, anonymous download (fine for public repos). Never printed.
- **Smoke test**: `./claude/curl_salad.sh -url corn-cabbage-2yk4e98r3rx752n0.salad.cloud -m qwen38-27b` (canary question `27*43?` → `1161`).
- **Claude Code against it**: `cl_salad [GATEWAY_URL]` — or `cl_salad_deploy` when the group is asleep (it starts and waits for it). See [Service scripts](#service-scripts).

### Other live groups (all stopped as of 2026-10-07)

| Group | Project | Deployer | What it runs | Gateway |
|---|---|---|---|---|
| `qwen38-27b-q5` | `qwen38-27b` | `deploy/deploy_qwen38_27b.py --group` | v5 test/canary, same profile as prod | `starfruit-watercress-1wnfoj9fdzbao3xj.salad.cloud` |
| `qwen38-9b` | `qwen38-27b` | `deploy/deploy_qwen38_9b.py` | plain 9B (Q4_K_M @ 32K), 7 card classes | `parmesan-cayenne-q0cfrqiksj7jhgrp.salad.cloud` |
| `atx-swift-27b-q5` | `llm` | `deploy/deploy_generic.py` | ATX-Swift Q5_K_M @ 90K + vision — **A/B baseline, q8_0 KV** (the vanilla `lmss_generic` image) | `tamarind-caraway-1rpqcqcnbbkmqdbv.salad.cloud` |
| `atx-swift-27b-q5-opt` | `llm` | `deploy/deploy_ampere.py` | ATX-Swift Q5_K_M @ **full 132768 ctx** (PATCH applied 2026-10-07, takes effect at the next start) + vision + **turbo5/turbo4 KV** (the [ampere fork image](#the-llamampere-fork-build-lmss_generic_ampere); A/B-confirmed faster than the baseline) | `damson-pepper-erau5a722gxf2qm2.salad.cloud` |

A second org `akl-on-salad` shares the same API key; every deployer takes `--org` / `--project` / `--group` to target it.

## Service scripts

All of it is stdlib-only Python / POSIX sh. The Salad key is read from `deploy/salad_api.txt` by `salad_client.py` (repo root) and never printed; the HF token comes from `deploy/hft.txt` (see above).

### Deployers

All five take `--org` / `--project` / `--group` (defaults are the groups they're named after) and use create-or-update semantics: an existing group is updated in place (stop when running → PATCH image + full env → start), which keeps its DNS stable for clients; a missing group is created. All support `--no-start` (apply config, leave stopped). Resources (cpu / RAM / disk) are set at create time only — an existing group keeps its resources.

| Deployer | Default group | Image | Default profile |
|---|---|---|---|
| `deploy/deploy_qwen38_27b.py` | `qwen38-27b-q6k` | baked q6-mtp-vision `@4089a457` (v5) | **production**: Q5_K_M @ CTX 90000, RTX 3090, `CLAUDE_TEMPLATE=1`, `USE_DRAFT_MODEL=none`, vision on |
| `deploy/deploy_generic.py` | `atx-swift-27b-q5` (project `llm`) | `lmss_generic` `@a238efd5` (tag `cuda128-v3`, vanilla) | ATX-Swift 27B Q5_K_M @ 90000 + vision, **q8_0 KV** (A/B baseline); `--gpu` takes any card the org lists |
| `deploy/deploy_ampere.py` | `atx-swift-27b-q5-opt` (project `llm`) | `lmss_generic_ampere` `@f9532a85` (tag `cuda130`) | ATX-Swift 27B Q5_K_M @ **132768** + vision + **turbo5/turbo4 KV** (ATX operating point in `EXTRA_ARGS`), sm_86 30-series cards only (RTX 3090 default) |
| `deploy/deploy_qwen38_9b.py` | `qwen38-9b` | `lmss:cuda128-v3` (plain tag) | plain 9B Q4_K_M @ 32768, **7 card classes**, explicit `none` sentinels |
| `deploy/deploy_qwen9b.py` | `qwen9b` (retired) | `lmss:cuda128-v3` (plain tag) | plain 9B Q4_K_M @ 32768, single RTX 3090, priority batch |

- **`deploy/deploy_qwen38_27b.py`** — the canonical deployer for the production group (see [above](#production-27b-group-qwen38-27b-q6k)). `--gpu rtx5090|rtx3090` (repeatable — placement may land on any listed class), `--model-file` swaps the quant, `--use-draft-model none|<file>` (`none` = the gguf's embedded MTP head; a filename = the baked draft, needed for noMTP quants), `--no-claude-template` for non-Claude clients.
- **`deploy/deploy_generic.py`** — model-agnostic on the **vanilla** `lmss_generic` image: any GGUF repo/file via `--model-repo` / `--model-file`, served name via `--model-alias`. Draft and vision are **opt-in** per group (default `none`): `--draft-model` / `--vision-model` take `none`, `hf://org/repo/file`, or a bare file resolved against `MODEL_REPO`. `--spec-type draft-mtp|ngram-mod|none` selects speculation explicitly — an MTP-embedded gguf without a separate draft file runs `draft-mtp` on its in-gguf head instead of being downgraded to ngram-mod. `--gpu` takes ANY card the org lists (the class name lowercased, spaces stripped — e.g. `rtx4060ti`, `rtx5090`); pass several and Salad may place on any. `--extra-args` defaults to `none` — vanilla llama-server, nothing tuned. Because nothing is baked, a fresh worker downloads everything (a 27B `hf download` transiently holds ~2× the file → 50 GB disk default). Default profile: ATX-Swift Q5_K_M @ 90000 + vision, q8_0 KV, RTX 3090 (the A/B baseline `atx-swift-27b-q5`); `--ctx-size 132768` runs the full ATX context (q8_0 KV ≈ 4.6 GiB — a 5090-class card).
- **`deploy/deploy_ampere.py`** — the [llamAmpere fork build](#the-llamampere-fork-build-lmss_generic_ampere): the same model-agnostic runtime (any GGUF via `--model-repo` / `--model-file`, draft/vision opt-in, same `--spec-type` and disk/probe behavior), but on the `lmss_generic_ampere` image with the turbo KV flags baked in the CMD, and `--gpu` limited to the org's sm_86 30-series cards (`rtx3090` / `rtx3090ti` / `rtx3080` / `rtx3080ti` — the fork ships sm_86+89 cubins only, so no rtx5090; only the 24 GB cards hold the 27B default quant). The built-in defaults ARE the ATX-Swift production profile: `--extra-args` defaults to the ATX operating point from `tmp/post.txt` — spec-draft MTP params + the baked vocab map + sampling; the KV types are already baked, and `--host`/`--port` are deliberately absent (the Salad gateway needs 0.0.0.0, not post's 127.0.0.1); `--ctx-size` defaults to **132768** (the full ATX context — it fits a 3090 only thanks to the turbo KV, ~2.1 GiB vs ~4.6 for q8_0). A bare `--no-start` run re-applies the production `atx-swift-27b-q5-opt` group exactly.
- **`deploy/deploy_qwen38_9b.py`** — replacement for the broken `qwen9b` group. Plain 9B: the three optional features are set to `none` **explicitly** (v3 env names: `DRAFT_MODEL_URL` / `VISION_MODEL_URL` / `CHAT_TEMPLATE`) rather than relying on the image defaults. Runs on any of RTX 3090 / 3090 Ti / 4090 / 4080 / 5070 Ti / 5080 / 5090; probe 120 s + 20 × 60 s (covers the ~5.4 GB download). **Created in the stopped state** — start it explicitly after creation.
- **`deploy/deploy_qwen9b.py`** — the original 9B deployer (single 3090, priority batch, probe 120 s + 10 × 5 s). Its group was superseded by `qwen38-9b` and has been deleted (as of 2026-10-06); kept as the 9B reference for a single-3090, no-7-class deployment.
- **Idle/heartbeat self-shutdown — all five** take `--idle-shutdown none|idle|heartbeat` (default `none` = today's behavior), `--idle-timeout 600`, `--heartbeat-timeout 600`, and **`--stop-key <file>`** (the account-wide per-user Salad API key — group-scoped keys don't exist; stored in the group env b64-obfuscated as `b64:<base64>` — the watchdog's real kill is the group `/stop` POST; **key `none`/missing DISABLES the watchdog**: a keyless self-exit gets RESCHEDULED, not stopped, paid test 2026-10-09, so arming without a usable key is pointless). The env knobs are **always sent** (PATCH replaces env wholesale; harmless on images that predate the watchdog). Armed mode creates the group with `restart_policy=never` (create-only — kills crash-loop billing on self-exit; the group-level stop wins over the policy, so an existing `always` group needs no delete+recreate). See [Idle/heartbeat self-shutdown](#idleheartbeat-self-shutdown-idle_watchdogpy).

### `cl_salad` — run Claude Code against a live gateway

`cl_salad [GATEWAY_URL] [claude args…]` (installed at `/usr/local/bin/cl_salad`). The **simple runner**: it checks the gateway once — `GET /v1/models` must return 200 (group running **and** model loaded) — and **dies immediately if it's dead** (exit 2, with distinct messages for unreachable / 403 / other). It does NOT start or manage the group. Alive, it starts `salad_proxy.py` on a local port, points the claude CLI at it, and exits when claude exits.

- The gateway URL is the first argument — the one thing that changes when a group is recreated (a bare host works, scheme optional; a first arg starting with `-` is a claude flag, not a URL). Omit it → `SALAD_GATEWAY_HOST` → built-in default (the prod group's DNS, `corn-cabbage-…`).
- Token caps default to 64000 input + 16000 output = 80000: the served n_ctx is 90112, so total input + requested output must stay under it (or the model errors with "context length exceeded"). Lower them if you hit that.
- Env overrides: `SALAD_GATEWAY_HOST`, `SALAD_MODEL_ALIAS` (default `qwen38-27b`), `SALAD_KEYFILE`, `SALAD_PROXY_PORT` (8093), `SALAD_ENABLE_THINKING` (0 = clean answers), `SALAD_HEARTBEAT` (default 0; `1` = post a 1-token completion to the gateway every 30 s so a group deployed with `IDLE_SHUTDOWN=heartbeat` self-stops when this session dies — `cl_salad_deploy` sets it automatically from the group env). The local (non-Salad) equivalents are `clov` / `cl_tr4v` in `/usr/local/bin`.

### `cl_salad_deploy` — start + wait, then run

`cl_salad_deploy [options] [claude args…]` (installed at `/usr/local/bin/cl_salad_deploy`). The **deploy half** (split out of the old cl_salad): looks up the group's status, starts it if it's stopped, waits for the model to be ready (default **2700 s** — covers image pull + an ~18 GiB cold download; exits early if the group flips back to stopped), then hands off to `cl_salad`.

- Without `--url`, the gateway DNS is looked up from the group's own `networking.dns` — so `cl_salad_deploy --group <name>` is enough.
- Options must come **before** any claude argument (the first non-option token ends parsing; put `--` before claude flags if a name could clash): `--url`, `--org`, `--project`, `--group`, `--model`, `--keyfile`, `--port`, `--thinking`, `--context-tokens`, `--output-tokens`, `--timeout`, `--no-run` (start + wait, no claude), `--status` (print the group's status and exit). Each mirrors the env var cl_salad reads: option > env > default.
- Examples: `cl_salad_deploy` (prod group), `cl_salad_deploy --group qwen38-27b-q5 -- -p "Say hi"`, `cl_salad_deploy --org akl-on-salad --project llm --group atx-swift-27b-q5`.
- Note the paid path: starting a stopped group triggers a real cold start (image pull + model download = money). `--status` and the dead-gateway paths cost nothing.
- Heartbeat auto-detect: before handing off, it reads the group's `IDLE_SHUTDOWN` env from the management API; when it's `heartbeat`, it exports `SALAD_HEARTBEAT=1` so `cl_salad` (and any `chat_salad.sh` run against the gateway) keeps the group alive with a 1-token call every 30 s — the group self-stops shortly after the session dies.

### `salad_proxy.py` — the Anthropic↔OpenAI bridge

Stdlib-only; listens on **127.0.0.1 only** (it holds the Salad key — never exposed to the network). Claude Code speaks the Anthropic Messages API; llama-server is OpenAI-only. The proxy translates `POST /v1/messages` → `/v1/chat/completions` and the response back (SSE streaming or JSON), maps `tool_use` / `tool_result` ↔ `tool_calls` / `role:tool`, injects the `Salad-Api-Key` header on every upstream request (read from the key file, never printed), and disables Qwen "thinking" by default so the model emits clean answers rather than a long reasoning preamble (`--thinking 1` to change). Also serves `GET /v1/models` and `GET /healthz`.

Other helpers — local run/test ones in `tests/` at the repo root: `run_27b.sh` / `run_9b*.sh` (compose/run helpers, see above), `curl1.sh` / `curl2.sh` (API check / question→answer on :8080 / :8081, read `api.txt` for the Bearer header — via a local symlink), and `run-nvidia-smi.sh`; the rest of the local ones in `docker/`: `check_status.sh` (status-API probe on :9999 / :9998), `question.sh`, `version.sh` (in-container build fingerprint + env table — the live check for the image-cache caveat above), and `stats.sh` (in-container llama-server stats — tps/queue/totals scraped from `/metrics`; the parser is `llama_stats.py`, also installed in the image and mirrored by `utils/llama_stats.py` for Salad-gateway URLs from the host; aliased in-image as `llama-stats`) and `bw_reporter.py` (HF-download bandwidth sampler — feeds the `manage_groups.py` arbitrator, see [HF-download bandwidth reporter](#hf-download-bandwidth-reporter-bw_reporterpy--the-arbitrator)); Salad-gateway ones in `claude/`: `curl_salad.sh` (the same smoke test against a gateway, with the key) and `chat_salad.sh` (`llm` chat direct against the gateway — `llm -H` carries the Salad-Api-Key).

## Verify

```bash
# GPU placement (expect your card's memory.used to jump by the model size)
nvidia-smi --query-gpu=index,name,memory.used --format=csv,noheader

# API check (replace 8080 with your HOST_PORT)
curl -s http://localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen","messages":[{"role":"user","content":"What is 27*43? Answer with just the number."}],"max_tokens":500}'
```

Qwen3.8 runs in thinking mode: short `max_tokens` values may be consumed entirely by `reasoning_content`. The response includes a `timings` block (prompt/generation tok/s).

## Status API (port 9999)

A small uvicorn app (`api_app.py`) runs alongside llama-server and reports container lifecycle state — useful as k8s-style probes or for scripting startup waits. It starts before the model download, so it's reachable from the first second of container life.

| Endpoint | Returns `"ok"` when… | Otherwise |
|---|---|---|
| `GET /startup` | the model-download phase has begun (or the model was already present, i.e. a restart) | 503 while still booting |
| `GET /live` | a `llama-server` process is running in the container (checked via `/proc`) | 503 if it's not up |
| `GET /ready` | llama-server answers its own `/health` with `{"status":"ok"}` — up **and** healthy (model loaded, serving) | 503 until then |

```bash
curl -s http://localhost:9999/startup   # ok once hf download has begun
curl -s http://localhost:9999/live      # ok while llama-server is running
curl -s http://localhost:9999/ready     # ok when /health says ok — safe to send requests
curl -s http://localhost:9999/          # all three states at once (debug helper)
```

Host port mapping: 27B service → `${STATUS_API_HOST_PORT:-9999}`, 9B service → `${STATUS_API_9B_HOST_PORT:-9998}` (both containers listen on 9999 internally). The uvicorn log lives at `/tmp/llama-api/api.log` inside the container (`docker compose exec qwen38-llama cat /tmp/llama-api/api.log`).

Bind address: `API_HOST` (default `0.0.0.0`, IPv4-only). This machine has Docker's IPv6 enabled (`/etc/docker/daemon.json`: `"ipv6": true`), so compose/run scripts set `API_HOST=::` — a dual-stack bind that accepts both stacks, which matters because `localhost` now resolves to `::1` first and Docker forwards v6 traffic to the container's IPv6 address. (`run_api.py` binds the socket itself: CPython's asyncio forces `IPV6_V6ONLY` on sockets it creates, which would break dual-stack.) If you disable Docker IPv6 again, set `API_HOST=0.0.0.0`.

## Day-to-day

```bash
docker compose logs -f qwen38-llama   # follow server logs (qwen38-9b for the smoke test)
docker compose ps                     # status + port mapping
docker compose up -d qwen38-9b        # start just the 9B server (or ../tests/run_9b.sh)
docker compose down                   # stop and remove containers + downloaded models

# Debug tools inside the container: curl, ssh, scp, vi, htop, bmon, nvtop (GPU monitor),
# ping (needs --cap-add=NET_RAW), llama-stats
docker compose exec -it qwen38-llama nvtop
docker compose exec qwen38-llama curl -s localhost:8080/health
```

## Leave nothing behind (deploy on someone else's machine)

The image itself also lives in Docker's storage (`/var/lib/docker`). Full cleanup:

```bash
docker compose down && \
docker rmi boris271142/lmss:cuda128-v3 nvidia/cuda:12.8.2-runtime-ubuntu22.04
```

That removes the container (including the ~20 GB of model files), the built image, and its base layers. Only your source folder remains. (On a machine you don't mind nuking more broadly, `docker system prune -af` does the same plus any other unused images/volumes.)

## Running on another machine (via registry)

Push exactly ONE image — `boris271142/lmss:cuda128-v3`. Both services share it; only env vars differ. CUDA base images come from Docker Hub, models download from HF on first start.

```bash
# On this machine: tag + push (use a meaningful tag, e.g. the llama.cpp build)
docker tag  qwen38-llama:latest boris271142/lmss:cuda128-v3
docker push boris271142/lmss:cuda128-v3
```

On the remote machine (needs Docker + nvidia-container-toolkit):
1. Copy the `docker/` and `tests/` folders over (compose file, `Dockerfile.multistage`, the app files it COPYs, `tests/run_*.sh`, `api.txt`).
2. In docker-compose.yml set both services' `image:` to `boris271142/lmss:cuda128-v3` (the `build:` block then just becomes a local-rebuild fallback).
3. Pull and start — the model downloads from HF on first run:

```bash
docker login   # Docker Hub, if the repo is private
docker pull boris271142/lmss:cuda128-v3
./tests/run_9b.sh    # or ./tests/run_27b.sh; pass the GPU_ID arg for that machine's card layout
```

Air-gapped (no registry access): `docker save boris271142/lmss:cuda128-v3 | ssh remote 'docker load'`.

The API key is no longer baked into the image (it's passed via env from api.txt — see `API_KEY` above), so a public registry would be fine; copy `api.txt` to the remote machine too if you want the same auth.
