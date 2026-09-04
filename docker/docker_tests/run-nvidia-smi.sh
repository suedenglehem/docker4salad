#!/bin/bash
# Run nvidia-smi in a one-shot container pinned to ONE physical GPU.
#
# Usage: ./run-nvidia-smi.sh [GPU_ID]     (default: 0)
#   GPU_ID = host GPU index (from `nvidia-smi`) that the container is pinned to.
#   Only that card is visible inside the container (as device 0), so plain
#   `nvidia-smi` shows exactly it — no --main-gpu or similar needed.
#
# The image's default CMD (llama-server) is replaced by nvidia-smi, so the
# container prints GPU info and exits; the long-running container is untouched.

set -euo pipefail

IMAGE="qwen38-llama-fa-api"
GPU_ID="${1:-0}"

if ! [[ "$GPU_ID" =~ ^[0-9]+$ ]]; then
    echo "Usage: $0 [GPU_ID]  (numeric host GPU index, e.g. 0/1/2)" >&2
    exit 1
fi

echo "Running nvidia-smi in a container pinned to host GPU ${GPU_ID}..."
docker run --rm --gpus "\"device=${GPU_ID}\"" "$IMAGE" nvidia-smi
