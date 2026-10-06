#!/usr/bin/env python3
"""llama-server stats (tps / queue / totals) — repo-root entry point.

The implementation lives in docker/llama_stats.py because that directory is
Dockerfile.multistage's build context (context = docker/, the repo root is
not in it) and the image installs a copy at /usr/local/bin/llama_stats.py
(wrapped by stats.sh). This file only delegates, so there is one source of
truth:

    python3 llama_stats.py https://<group>.salad.cloud  # loops every 5s, Ctrl-C stops
    python3 llama_stats.py http://127.0.0.1:8080 --once # local server, single scrape
    # flags: [--raw] [--once | --interval N]

Salad-Api-Key is auto-loaded from salad_api.txt for *.salad.cloud targets.
"""

import os
import runpy

if __name__ == "__main__":
    runpy.run_path(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "docker", "llama_stats.py"),
        run_name="__main__",
    )
