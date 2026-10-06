#!/bin/sh
# llama-server stats (tps / queue / totals) from INSIDE the container —
# thin wrapper over /usr/local/bin/llama_stats.py (installed by
# Dockerfile.multistage) pointed at the local llama-server. For Salad
# gateway URLs from the host, use utils/llama_stats.py instead.
# Usage: stats.sh [--raw] [--once | --interval N]   (default: 5s loop)
exec python3 /usr/local/bin/llama_stats.py "http://127.0.0.1:${PORT:-8080}" "$@"
