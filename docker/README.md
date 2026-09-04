# Qwen3.8 llama.cpp (Docker)

Dockerized [llama.cpp](https://github.com/ggml-org/llama.cpp) server for Qwen3.8 GGUF models, pinned to **one** NVIDIA GPU via `GPU_ID`. OpenAI-compatible API on the host port you choose.

- Image: `qwen38-llama-fa-api` — built from `Dockerfile.multistage`: llama.cpp compiled with CUDA + flash attention + NCCL (`-DGGML_CUDA_NCCL=ON`, pinned commit `3af988fab`, build tag b10572) in a CUDA devel stage, shipped on the ~5 GB `nvidia/cuda:12.8.2-runtime` base. Binary lives at `/opt/llama.cpp/build/bin/llama-server`.
- Also ships a small status API on port **9999** (`/startup`, `/live`, `/ready` — see [Status API](#status-api-port-9999)) and debug tools inside the container: `curl`, `ssh`, `vi`, `htop`, `nvtop` (GPU monitor, built from source — not in Ubuntu 22.04 repos).
- Models are downloaded on first start into `/models` **inside** the container (no host bind mount), so `docker compose down` removes them — nothing is left behind on the host disk.

## Prerequisites

- Docker + compose v2
- `nvidia-container-toolkit` installed (so `device_ids` GPU pinning works)
- Free VRAM on the target card: ~17.6 GB for 27B at 131k ctx, ~6 GB for 9B at 32k ctx

## Run it

Full command with every overridable argument spelled out (defaults shown = production setup: Qwen3.8-27B on an idle RTX 3090):

```bash
cd /dd2/andrei/docker && \
GPU_ID=0 \
HOST_PORT=8080 \
MODEL_REPO="unsloth/Qwen3.8-27B-GGUF" \
MODEL_FILE="Qwen3.8-27B-UD-Q4_K_M.gguf" \
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
| `GPU_ID` (script arg) | First argument of `run_27b.sh` / `run_9b.sh` (or export it before a raw `docker compose up`): selects the physical card by nvidia-smi index via `device_ids`. Defaults: 27B → `0`, 9B → `1`. Exposing a single card also stops llama.cpp spreading layers across all visible GPUs. On this box: `0`/`2` = RTX 3090 (24 GB), `1` = RTX 3080 Ti (12 GB). Check free VRAM first with `nvidia-smi` — the 27B needs ~17.6 GB free on one card |
| `HOST_PORT` | :8080 is taken by another server → use e.g. `8090` until you free it |
| `MODEL_REPO` / `MODEL_FILE` | Different quant or model. Verified options in the unsloth repo: `Qwen3.8-27B-Q4_0.gguf`, `Qwen3.8-27B-UD-Q4_K_S.gguf`, `Qwen3.8-27B-UD-Q5_K_M.gguf` (18.4 GiB — tight at 131k ctx). For the 9B smoke test: repo `empero-ai/Qwen3.8-9B-Distill-GGUF`, file `Qwen3.8-9B-Q4_K_M.gguf` |
| `CTX_SIZE` | Lower it (e.g. `32768`) if VRAM is tight or you don't need 131k — KV cache scales with this |
| `N_GPU_LAYERS` | Keep `99` for full offload; lower only if the GPU is shared and you want some layers on CPU (slower) |
| `THREADS` / `BATCH_SIZE` / `UBATCH_SIZE` | Rarely needed; leave as-is unless tuning throughput |
| `HF_TOKEN` | Only if the repo is gated or HF rate-limits you (`hf auth token` to get one) |
| `API_KEY` | llama-server's auth key. The run scripts read it from `api.txt` in this folder (chmod 600); missing file → server runs without key auth. `crl.sh` / `crl2.sh` also read api.txt for their Bearer header |

Notes:
- The `timeout 30` wrapper just guards against a hung build — with the image already built, `up -d` returns in seconds. Drop it if you prefer.
- If you edit the Dockerfile later, run `docker compose build` first (or add `--build`). Compose reuses the existing `qwen38-llama` image otherwise.
- First start downloads the model file into the container's `/models`; restarting a stopped container loads it from disk in seconds, but `docker compose down` removes it (the next `up` re-downloads).

### Smoke test variant — permanent 9B service on the RTX 3080 Ti

The 9B server is a second compose service (`qwen38-9b`, host port **8081**), pinned to the 3080 Ti via `device_ids: ["1"]`. Same image as the 27B; only env vars differ.

```bash
cd /dd2/andrei/docker && ./run_9b.sh        # default: RTX 3080 Ti (nvidia-smi index 1)
./run_9b.sh 0                               # run it on a different card instead
```

Measured on this box: model + KV ≈ 5.8 GiB VRAM, ~100 tok/s generation (verified with the multi-stage image 2026-08-23). `crl.sh` / `crl2.sh` target it on :8081.

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
docker rmi qwen38-llama-fa-api nvidia/cuda:12.8.2-runtime-ubuntu22.04
```

That removes the container (including the ~15 GB model), the built image, and its base layers. Only your source folder remains. (On a machine you don't mind nuking more broadly, `docker system prune -af` does the same plus any other unused images/volumes.)

## Running on another machine (via registry)

Push exactly ONE image — `qwen38-llama-fa-api` (~5 GB). Both services share it; only env vars differ. CUDA base images come from Docker Hub, models download from HF on first start.

```bash
# On this machine: tag + push (use a meaningful tag, e.g. the llama.cpp build)
docker tag  qwen38-llama-fa-api:latest ghcr.io/YOUR_USER/qwen38-llama-fa-api:b10572
docker push ghcr.io/YOUR_USER/qwen38-llama-fa-api:b10572
```

On the remote machine (needs Docker + nvidia-container-toolkit):
1. Copy this folder over (compose file, `Dockerfile.multistage`, `.dockerignore`, `run_*.sh`).
2. In docker-compose.yml set both services' `image:` to `ghcr.io/YOUR_USER/qwen38-llama-fa-api:b10572` (the `build:` block then just becomes a local-rebuild fallback).
3. Pull and start — the model downloads from HF on first run:

```bash
docker login ghcr.io   # if the repo is private
docker pull ghcr.io/YOUR_USER/qwen38-llama-fa-api:b10572
./run_9b.sh            # or ./run_27b.sh; pass the GPU_ID arg for that machine's card layout
```

Air-gapped (no registry access): `docker save qwen38-llama-fa-api | ssh remote 'docker load'`.

The API key is no longer baked into the image (it's passed via env from api.txt — see `API_KEY` above), so a public registry would be fine; copy `api.txt` to the remote machine too if you want the same auth.
