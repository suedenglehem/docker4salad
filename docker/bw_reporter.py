#!/usr/bin/env python3
"""HF-download bandwidth reporter for the Salad container.

Started as a background helper by every image entry script (the 5th
`nohup … &` line, right before the `hf download` block). Its ONLY job: sample
the total size of ${MODEL_DIR} every 10 s and append `epoch bytes` lines to
${API_STATE_DIR}/bw.log while the model downloads. The bandwidth arbitrator
(utils/manage_groups.py, manager side) reads that log over SSH — one plain
`tail` command, since Salad's SSH exec has no shell — and decides whether the
node's download speed clears the group's --min-bw-mbps cap; below the cap it
POSTs the Salad instance /reallocate endpoint. This script REPORTS ONLY: no
verdicts, no API calls, nothing else.

WHY A REPORTER IN THE CONTAINER: the HF download runs in the container (the
`hf` CLI prints progress to stdout only, and its partials live under
${MODEL_DIR}/.cache/huggingface/download/*.incomplete), so the container is
the only place that sees the download's byte growth. The manager cannot see
it from the API (pulling_progress covers the IMAGE pull, not the HF
download), and Salad SSH is shell-less, so a single `tail` of a log file is
the cheapest reliable read.

Log format (bw.log, append-only, one line per sample):
  <unix-epoch-seconds> <total-bytes-under-MODEL_DIR>
The first sample is taken before the download starts (the entry script
launches this helper first), so it is the baseline pair the manager needs.
A line is written EVERY tick even when the total is unchanged: a stalled
download shows as flat bytes (the manager floors the rate at 0), a dead
reporter shows as a frozen log (the manager bails) — the two are
distinguishable precisely because this script always writes.

Self-exit: when llama-server's own /health answers ok (the exact probe
api_app's /ready uses) — download and model load are done, the log has
served its purpose. If llama-server never gets healthy (crash-loop), the
reporter keeps sampling forever; the manager's frozen-log bail covers it.

Never raises: the loop body is wrapped in try/except — a failed walk or a
failed write is logged to stdout (the nohup redirect -> bw_reporter.log) and
sampling continues. Losing a sample is fine; dying would freeze the log and
make a healthy node look dead.

Byte counting uses lstat on every entry: the local-dir model file is a
symlink into the HF blob cache, so lstat counts the blob once (under
.cache/) and the symlink's own ~100 bytes — summing through symlinks would
double the growth and inflate the reported rate.

Stdlib only (os/time/urllib), matching the repo convention
(api_app.py, idle_watchdog.py, llama_stats.py).

Env knobs (all baked defaults, group-overridable):
  MODEL_DIR      directory to sample (default /models)
  API_STATE_DIR  directory holding bw.log (default /tmp/llama-api)
  PORT           llama-server port for the /health self-exit probe (8080)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

SAMPLE_S = 10.0        # sampling period (manager reads with BW_POLL_INTERVAL=15)
HEALTH_TIMEOUT_S = 2   # /health probe timeout (same as api_app._ready_state)


def log(msg: str) -> None:
    print(f"[bw_reporter] {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}",
          flush=True)


def model_bytes(root: str) -> int:
    """Total size of all files under MODEL_DIR (lstat: HF cache symlinks count
    their own tiny size, the blobs they point at are walked as real files —
    no double counting). os.walk swallows per-directory stat errors."""
    total = 0
    for _dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(_dirpath, name)).st_size
            except OSError:
                continue  # file vanished mid-walk (hf cleanup): skip it
    return total


def health_ok(url: str) -> bool:
    """llama-server /health ok — the exact probe api_app._ready_state uses
    (HTTP 200 and body status 'ok'). urllib raises HTTPError on 503 (model
    loading) and URLError on connection refused (server not up yet)."""
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT_S) as r:
            body = json.loads(r.read().decode("utf-8"))
            return r.status == 200 and body.get("status") == "ok"
    except Exception:  # noqa: BLE001 - any failure = not ready yet
        return False


def main() -> int:
    root = os.environ.get("MODEL_DIR") or "/models"
    state_dir = os.environ.get("API_STATE_DIR") or "/tmp/llama-api"
    port = int(os.environ.get("PORT", "8080"))
    health_url = f"http://127.0.0.1:{port}/health"
    log_path = os.path.join(state_dir, "bw.log")
    log(f"model_dir={root} log={log_path} sample={SAMPLE_S:.0f}s "
        f"self-exit on {health_url} ok")

    while True:
        try:
            ts = int(time.time())
            total = model_bytes(root)
            with open(log_path, "a") as f:
                f.write(f"{ts} {total}\n")
            if health_ok(health_url):
                log(f"llama-server /health ok — download+load done, exiting "
                    f"(final total {total} bytes)")
                return 0
            time.sleep(SAMPLE_S)
        except Exception as e:  # noqa: BLE001 - never die: a frozen log looks like a dead node
            log(f"sample failed ({e.__class__.__name__}: {e}) — continuing")
            time.sleep(SAMPLE_S)


if __name__ == "__main__":
    sys.exit(main())
