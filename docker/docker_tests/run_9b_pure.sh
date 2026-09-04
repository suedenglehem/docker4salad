#!/bin/bash
# Qwen3.8-9B smoke-test server — pure-Docker variant of run_9b.sh (no compose).
# Starts the same container as the `qwen38-9b` service in docker-compose.yml,
# but with a plain `docker run -it`: logs stream straight into your terminal
# and Ctrl-C stops the server process.
# Usage: ./run_9b_pure.sh [GPU_ID]
#   GPU_ID: nvidia-smi index of the card to run on (default: 1 = RTX 3080 Ti).
#           On this box: 0/2 = RTX 3090 (24 GB), 1 = RTX 3080 Ti (12 GB).
# Note: every run recreates the container, which re-downloads the model (no
#       host bind mount by design — see docker-compose.yml). Compose would
#       reuse an unchanged running container instead.
# Note: with --restart unless-stopped (same as compose), a Ctrl-C exit can be
#       restarted in the background; `docker stop qwen38-9b` is the full stop.
cd /dd2/andrei/docker/on_salad/docker/docker_tests || exit 1

IMAGE="boris271142/llama-server-on-salad:cuda128"
NAME="qwen38-9b"

GPU_ID="${1:-1}"
[[ "$GPU_ID" =~ ^[0-9]+$ ]] || { echo "usage: $0 [GPU_ID]" >&2; exit 1; }
export GPU_ID

# The image is built by the qwen38-llama service (Dockerfile.multistage).
# Without this check, docker run would try to PULL it from Docker Hub and
# fail with a confusing auth error.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "error: image $IMAGE not found locally." >&2
  echo "       build it first: docker compose build qwen38-llama" >&2
  exit 1
fi

# API key: read from api.txt in this folder (chmod 600) so the secret stays out
# of the image and the compose file. Missing -> server runs without auth.
if [[ -f api.txt ]]; then
  export API_KEY="$(tr -d '[:space:]' < api.txt)"
else
  echo "note: no api.txt here -> server will run WITHOUT API key auth" >&2
fi

# Recreate-on-run, like `compose up` does when the config changes: drop any
# old container with this name (running or stopped) so --name never collides.
docker rm -f "$NAME" >/dev/null 2>&1

# -it keeps stdin open (compose's stdin_open) and attaches the terminal.
# --gpus '"device=N"' exposes exactly ONE card (compose's device_ids); inside
# the container it is always index 0 — hence -e GPU_ID=0 regardless of the
# host-side $GPU_ID above (a bare `-e GPU_ID` would leak the host value).
# Port 9999 = status API (/startup, /live, /ready), same mapping as compose.
docker run -it \
  --name "$NAME" \
  --restart unless-stopped \
  --gpus "\"device=${GPU_ID}\"" \
  -p 8081:8080 \
  -p "${STATUS_API_9B_HOST_PORT:-9998}:9999" \
  -e MODEL_REPO="empero-ai/Qwen3.8-9B-Distill-GGUF" \
  -e MODEL_FILE="Qwen3.8-9B-Q4_K_M.gguf" \
  -e CTX_SIZE=32768 \
  -e GPU_ID=0 \
  -e N_GPU_LAYERS=99 \
  # Dual-stack status-API bind (Docker IPv6 is enabled on this machine).
  -e API_HOST="${API_HOST:-::}" \
  -e API_KEY="${API_KEY:-}" \
  "$IMAGE"
