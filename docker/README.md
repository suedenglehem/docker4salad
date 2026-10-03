# Qwen3.8 llama.cpp (Docker)

Dockerized [llama.cpp](https://github.com/ggml-org/llama.cpp) server for Qwen3.8 GGUF models, pinned to **one** NVIDIA GPU via `GPU_ID`. OpenAI-compatible API on the host port you choose.

- Image: `boris271142/lmss:cuda128-v3` (container name `qwen38-llama-fa-api`) — built from `Dockerfile.multistage`: llama.cpp compiled with CUDA + flash attention + NCCL (`-DGGML_CUDA_NCCL=ON`, pinned commit `3af988fab`, build tag b10572) in a CUDA devel stage, shipped on the `nvidia/cuda:12.8.2-runtime` base. Binary lives at `/opt/llama.cpp/build/bin/llama-server`.
- Also ships a small status API on port **9999** (`/startup`, `/live`, `/ready` — see [Status API](#status-api-port-9999)) and debug tools inside the container: `curl`, `ssh`, `vi`, `htop`, `nvtop` (GPU monitor, built from source — not in Ubuntu 22.04 repos).
- Models are downloaded on first start into `/models` **inside** the container (no host bind mount), so `docker compose down` removes them — nothing is left behind on the host disk.

## Prerequisites

- Docker + compose v2
- `nvidia-container-toolkit` installed (so `device_ids` GPU pinning works)
- Free VRAM on the target card: ~23 GB for 27B at 131k ctx (noMTP Q4_K_M weights + MTP draft + q8_0 KV cache), ~6 GB for 9B at 32k ctx

## Choosing quant + context length by VRAM

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
- Repo `JonathanColetti/Qwen3.8-27B-Uncensored-GGUF`. Use the plain `Q…` / `IQ…` files when running **without** a separate draft file (the Salad v4 image: `USE_DRAFT_MODEL=none` → self-speculation from the model's embedded MTP/nextn layers). The `noMTP-…` files have no embedded MTP layers and crash llama-server at load **unless** you feed them a separate `DRAFT_MODEL_FILE` (the local multistage default: noMTP-Q4_K_M + draft-Q8_0).
- The 27B's trained context is **262144** — serving above it buys nothing; that's the table's ceiling.
- The ~2.1 GiB overhead includes the ~1 GiB vision projector the baked v4 image always loads; a text-only profile gets it back. A separate `DRAFT_MODEL_FILE` (~3 GiB for draft-Q8_0) costs that much ctx budget.
- Numbers assume `--cache-type-k/v q8_0` (the image default). f16 KV doubles the KV cost → halve the ctx.

## Run it

Full command with every overridable argument spelled out (defaults shown = the local compose profile: noMTP-Q4_K_M + draft-Q8_0 @ 131072). The 27B does NOT fit this box's 16 GB cards; locally, use the 9B service. The live SaladCloud production setup (Q5_K_M @ 90K on a 3090) is described in [SaladCloud](#saladcloud-27b-production-group).

```bash
cd /dd2/andrei/docker/on_salad/docker/docker_tests && \
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

### Arguments

| Arg | Change it if… |
|---|---|
| `GPU_ID` (script arg) | First argument of `run_27b.sh` / `run_9b.sh` (or export it before a raw `docker compose up`): selects the physical card by nvidia-smi index via `device_ids`. Defaults: 27B → `0`, 9B → `1`. Exposing a single card also stops llama.cpp spreading layers across all visible GPUs. On this box (verified 2026-10-01): `0` = RTX 4080 SUPER (16 GB), `1` = RTX 4060 Ti (16 GB) — the 27B needs ~23 GB free on one card, so it does not fit locally (use the [SaladCloud](#saladcloud-27b-production-group) group); the 9B fits either. Check free VRAM first with `nvidia-smi` |
| `HOST_PORT` | :8080 is taken by another server → use e.g. `8090` until you free it |
| `MODEL_REPO` / `MODEL_FILE` | Different quant or model. Default (verified in `JonathanColetti/Qwen3.8-27B-Uncensored-GGUF`): the noMTP `Q4_K_M` build — its MTP draft module is not embedded, it comes from `DRAFT_MODEL_FILE`. The repo's other variants (`Q4_K_M`, `Q5_K_M`, `Q6_K`, `Q8_0`, `IQ2_M`, `IQ4_XS`) embed the draft — run them with `DRAFT_MODEL_FILE=""`. Other 27B repos (unsloth, bartowski) have no draft file: also `DRAFT_MODEL_FILE=""`. For the 9B smoke test: repo `empero-ai/Qwen3.8-9B-Distill-GGUF`, file `Qwen3.8-9B-Q4_K_M.gguf` (the compose file already disables the draft there) |
| `DRAFT_MODEL_FILE` | MTP draft for `--spec-type draft-mtp` speculative decoding. Default `Qwen3.8-27B-Uncensored-draft-Q8_0.gguf` (~3 GB, same repo as the default model). Set to `""` to disable — the server falls back to n-gram self-speculation (needed for models whose draft isn't in `MODEL_REPO`, or on VRAM-tight cards) |
| `CHAT_TEMPLATE` | Jinja chat template **file path** passed as `--chat-template-file` (NOT the inline `--chat-template` flag — that one takes the template *text*; a path there becomes the literal prompt and the model degenerates into a loop of the path string, e.g. `/opt/llama.cpp/qwen3.8.q6.gguf` spam). Default empty/`none` → the template embedded in the GGUF is used (the default Uncensored model already embeds a permissive one that accepts system messages anywhere). Opt in with `CHAT_TEMPLATE=/opt/llama.cpp/qwen3.8.q6.jinja` (shipped in the image) for models whose embedded template is strict, e.g. Claude Code's Anthropic-format `/v1/messages` |
| `CTX_SIZE` | Lower it (e.g. `32768`) if VRAM is tight or you don't need 131k — KV cache scales with this |
| `N_GPU_LAYERS` | Keep `99` for full offload; lower only if the GPU is shared and you want some layers on CPU (slower) |
| `THREADS` / `BATCH_SIZE` / `UBATCH_SIZE` | Rarely needed; leave as-is unless tuning throughput |
| `HF_TOKEN` | Only if the repo is gated or HF rate-limits you (`hf auth token` to get one) |
| `API_KEY` | llama-server's auth key. The run scripts read it from `api.txt` in this folder (chmod 600); missing file → server runs without key auth. `crl.sh` / `crl2.sh` also read api.txt for their Bearer header |

Notes:
- The `timeout 30` wrapper just guards against a hung build — with the image already built, `up -d` returns in seconds. Drop it if you prefer.
- If you edit the Dockerfile later, run `docker compose build` first (or add `--build`). Compose reuses the existing `boris271142/lmss:cuda128-v3` image otherwise.
- First start downloads the model file into the container's `/models`; restarting a stopped container loads it from disk in seconds, but `docker compose down` removes it (the next `up` re-downloads).

### Smoke test variant — permanent 9B service on the RTX 4060 Ti

The 9B server is a second compose service (`qwen38-9b`, host port **8081**), pinned to the RTX 4060 Ti via `device_ids: ["1"]`. Same image as the 27B; only env vars differ.

```bash
cd /dd2/andrei/docker/on_salad/docker/docker_tests && ./run_9b.sh   # default: RTX 4060 Ti (nvidia-smi index 1)
./run_9b.sh 0                                                       # run it on a different card instead
```

Model + KV ≈ 5.8 GiB VRAM, ~100 tok/s generation (measured 2026-08-23 on the previous box's RTX 3080 Ti; the 9B fits either 16 GB card on this box). `crl.sh` / `crl2.sh` target it on :8081.

## SaladCloud (27B production group)

The 27B Claude Code backend lives in SaladCloud, not on this box: group `qwen38-27b-q6k` (org `ma-casa-in-paris`, project `qwen38-27b`), one **RTX 3090 (24 GB)**, on-demand (no autostart — `cl_salad` wakes it).

- **Image**: baked `boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:cuda128-v4`, digest-pinned live (`sha256:789ff2b3…3a4a`). A `FROM boris271142/lmss:cuda128-v3` extension (`Dockerfile.lmss_q6_mtp_vision`): the MTP draft (Q8_0, 2.95 GiB) and mmproj (F16, 0.86 GiB) are baked into the image, so at runtime **only the main model** is downloaded from HF via `hf` (v3's `wget2` `DRAFT_MODEL_URL` / `VISION_MODEL_URL` mechanism is gone — those env vars are no longer read). The local compose still uses the plain `cuda128-v3` multistage image.
- **Live config (2026-10-03)**: `MODEL_FILE=Qwen3.8-27B-Uncensored-Q5_K_M.gguf` (18.19 GiB, MTP head embedded in the gguf), `CTX_SIZE=90000` (served n_ctx 90112), `MODEL_ALIAS=qwen38-27b` (stable — clients reference the alias, not the group name), `CHAT_TEMPLATE=/opt/llama.cpp/qwen3.8.q6.jinja` (the patched Claude template — the GGUF-embedded one rejects mid-conversation system messages), `USE_DRAFT_MODEL=none` (self-speculation from the embedded MTP head; the baked draft is only used for noMTP quants), q8_0 KV, flash-attn, vision on. See [Choosing quant + context length by VRAM](#choosing-quant--context-length-by-vram). The group name `q6k` is a leftover from the original Q6_K deploy and is kept — renaming means delete + recreate = new DNS + the DELETE name-tombstone dance.
- **Deploy**: `python3 deploy_qwen38_27b.py` (repo root) — create-or-update in place (stop when running → PATCH image + full env → start), which keeps the group's DNS stable. Flags: `--gpu rtx5090|rtx3090`, `--model-file`, `--ctx-size`, `--image`, `--use-draft-model`, `--no-start`. Built-in defaults = the live production profile above (Q5_K_M @ CTX 90000 on rtx3090, digest-pinned v4 image), so a bare run re-applies exactly that — idempotent against the live group (verified byte-for-byte against a live GET). Caveat: the PATCH sends env **wholesale**, so every value comes from the flags/defaults, not from whatever is currently live — if you change the group's env out-of-band, update the defaults before the next deploy run.
- **Cold start**: every stop→start re-downloads the main model (~10 min observed for Q5_K_M, ~26 min for Q6_K). The readiness probe (30 s delay + 20 × 120 s ≈ 40.5 min failure window, `GET /ready` on 8889) is sized to tolerate that; the early 30 s first probe (not the 1200 s cap) makes the gateway open the moment the model is actually ready.
- **HF token**: `hft.txt` next to the deploy scripts (gitignored, chmod 600). If it validates against the Hub, the deployer adds `HF_TOKEN` to the group env; the image CMD forwards it to `hf download` as `--token` (authenticated, faster pulls). Missing/invalid → skipped with a warning, `hf` downloads anonymously (fine for public repos). Never printed.
- **Gateway** (port **443**, `Salad-Api-Key` header): `corn-cabbage-2yk4e98r3rx752n0.salad.cloud`.
- **Smoke test**: `./docker/docker_tests/curl2_salad.sh -url corn-cabbage-2yk4e98r3rx752n0.salad.cloud -m qwen38-27b` (from repo root).
- **Claude Code against it**: `cl_salad [GATEWAY_URL]` (in `docker/docker_tests/`, installed at `/usr/local/bin/cl_salad`) — the gateway URL is its first argument, auto-starts the group if it's asleep, runs the local `salad_proxy.py` (Anthropic↔OpenAI, injects the key), then the claude CLI with 64000/16000 token caps matching the 90112 ctx.

Superseded: `deploy_qwen38_27b_rtx5090.py` / `update_qwen38_27b_rtx5090.py` target the old `qwen38-27b-rtx5090` group (deleted 2026-10-01, ran `lmss:cuda128-v2`) — kept for reference only.

Note: the Salad worker image cache is keyed by REPO NAME, not tag or digest — a new tag on a cached repo can still serve a stale image on workers that had cached the old one (observed live on `lmss:cuda128` on 2026-10-01). Never overwrite a tag a worker may hold; the airtight lever is a digest-pinned image ref to a digest no worker has seen. With a plain tag, verify the live build with `version.sh` in the container.

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
docker compose up -d qwen38-9b        # start just the 9B server (or ./run_9b.sh)
docker compose down                   # stop and remove containers + downloaded models

# Debug tools inside the container: curl, ssh, vi, htop, nvtop (GPU monitor)
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
1. Copy the `docker_tests/` folder over (compose file, `run_*.sh`, `api.txt`) and `Dockerfile.multistage` + the app files it COPYs (or the whole `docker/` folder).
2. In docker-compose.yml set both services' `image:` to `boris271142/lmss:cuda128-v3` (the `build:` block then just becomes a local-rebuild fallback).
3. Pull and start — the model downloads from HF on first run:

```bash
docker login   # Docker Hub, if the repo is private
docker pull boris271142/lmss:cuda128-v3
./run_9b.sh    # or ./run_27b.sh; pass the GPU_ID arg for that machine's card layout
```

Air-gapped (no registry access): `docker save boris271142/lmss:cuda128-v3 | ssh remote 'docker load'`.

The API key is no longer baked into the image (it's passed via env from api.txt — see `API_KEY` above), so a public registry would be fine; copy `api.txt` to the remote machine too if you want the same auth.
