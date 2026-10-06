#!/bin/sh
# llama-server stats (tps / queue / totals) from INSIDE the container —
# thin wrapper over /usr/local/bin/llama_stats.py (installed by
# Dockerfile.multistage) pointed at the local llama-server. For Salad
# gateway URLs from the host, use the repo-root llama_stats.py instead.
# Usage: stats.sh [--raw] [--interval N]
exec python3 /usr/local/bin/llama_stats.py "http://127.0.0.1:${PORT:-8080}" "$@"
