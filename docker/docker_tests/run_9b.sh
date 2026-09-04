#!/bin/bash
# Qwen3.8-9B smoke-test server (see README.md "Smoke test variant").
# Usage: ./run_9b.sh [GPU_ID]
#   GPU_ID: nvidia-smi index of the card to run on (default: 1 = RTX 3080 Ti).
#           On this box: 0/2 = RTX 3090 (24 GB), 1 = RTX 3080 Ti (12 GB).
# Note: switching cards recreates the container, which re-downloads the model
#       (no host bind mount by design — see docker-compose.yml).
cd /dd2/andrei/docker/on_salad/docker/docker_tests || exit 1

GPU_ID="${1:-1}"
[[ "$GPU_ID" =~ ^[0-9]+$ ]] || { echo "usage: $0 [GPU_ID]" >&2; exit 1; }
export GPU_ID

# API key: read from api.txt in this folder (chmod 600) so the secret stays out
# of the image and the compose file. Missing -> server runs without auth.
if [[ -f api.txt ]]; then
  export API_KEY="$(tr -d '[:space:]' < api.txt)"
else
  echo "note: no api.txt here -> server will run WITHOUT API key auth" >&2
fi

timeout 30 docker compose up -d qwen38-9b
