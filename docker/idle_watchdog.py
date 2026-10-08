#!/usr/bin/env python3
"""Idle/heartbeat self-shutdown watchdog for the Salad container.

Started as a background helper by every image CMD (the 4th `nohup … &` line,
before `exec llama-server`). Its ONLY job: when the group has been configured
for idle-shutdown, terminate llama-server (PID 1) after a configurable window
of inactivity. llama-server is PID 1, so killing it exits the container, and
with the group's restart_policy=never (set by the deployers whenever the
feature is armed) Salad leaves the group STOPPED — free. This is the billing
hygiene: a lost client connection can no longer leave a GPU group idling for
hours.

Modes (env IDLE_SHUTDOWN, sentinel "none" = OFF, the baked default — every
existing group keeps today's behavior):

  idle      - kill when llama-server has served no chat traffic for
              IDLE_TIMEOUT seconds.
  heartbeat - same timer, HEARTBEAT_TIMEOUT seconds: the client pingers
              (claude/cl_salad, claude/chat_salad.sh, opt-in SALAD_HEARTBEAT=1)
              keep the group alive with 1-token completions through the
              gateway; pings stopping for the window means the client died.

Activity signal (both modes, same logic): llama-server's Prometheus counters
`llamacpp:prompt_tokens_total` + `llamacpp:tokens_predicted_total` (scraped
from 127.0.0.1:${PORT}/metrics, --metrics is baked in every CMD). Only chat
calls move them — /health and /v1/models polls do NOT count, so readiness
polling never keeps a group alive. The `llamacpp:requests_processing` gauge
> 0 also counts (a long generation in flight).

Scrape period is adaptive: at least 3 scrapes per timeout window
(timeout / 3, floored at 5 s, capped at the 30 s default), so a small testing
timeout (30 s) fires within ~timeout instead of up to timeout + 30 s late,
while the default 600 s keeps the 30 s period.

Safety rails:
  - The timer ARMS ONLY after llama-server's /health reports ok. Model
    download takes hours on slow workers; the watchdog never kills mid-boot.
  - If /health never comes up within IDLE_GRACE seconds, the watchdog gives
    up (exit 0, no kill) — a broken boot is the readiness probe's problem,
    not the watchdog's.
  - A failed /metrics scrape is a skipped tick, never an "idle" tick: only
    confirmed no-activity advances the kill timer.
  - Kill = SIGTERM to PID 1. VERIFIED (container test, 2026-10-08): the kernel
    drops EVERY signal — SIGKILL included — sent from inside a PID namespace to
    PID 1 when PID 1 has no handler installed. A watchdog inside the container
    can therefore never kill a handler-less PID 1. The image CMDs make PID 1
    killable by construction: llama-server runs as a CHILD of the CMD bash,
    which installs a SIGTERM trap (handler installed -> delivered), kills the
    child, and exits; PID-namespace teardown then reaps everything left.
  - IDLE_SHUTDOWN=none (default) -> exits immediately, does nothing.

Env knobs (all baked defaults, group-overridable via the deployers):
  IDLE_SHUTDOWN   none (off) | idle | heartbeat
  IDLE_TIMEOUT    seconds of no chat traffic -> kill (mode idle)
  HEARTBEAT_TIMEOUT seconds of no pings      -> kill (mode heartbeat)
  IDLE_GRACE      max seconds to wait for the first /health ok before giving up
  PORT            llama-server port (default 8080)
  API_KEY         llama-server auth key; sent as Bearer when set (local runs
                  pass it; Salad groups run llama-server without auth)

Stdlib only (urllib), matching the repo convention (api_app.py, llama_stats.py).
Logs go to ${API_STATE_DIR}/watchdog.log via the CMD's nohup redirect.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request

POLL_S = 30.0            # default metrics scrape period (small timeouts poll faster)
POLL_MIN_S = 5.0         # floor on the adaptive scrape period (never hammer /metrics)
HEALTH_PROBE_S = 30.0    # /health poll period while arming
STATUS_EVERY_TICKS = 10  # log a heartbeat line every ~5 min
SIGKILL_GRACE_S = 10     # SIGTERM grace before escalating to SIGKILL

PORT = int(os.environ.get("PORT", "8080"))
API_KEY = os.environ.get("API_KEY") or None
HEALTH_URL = f"http://127.0.0.1:{PORT}/health"
METRICS_URL = f"http://127.0.0.1:{PORT}/metrics"

# Activity counters (names verified against llama.cpp b10572, see llama_stats.py).
_PROMPT_TOKENS = "llamacpp:prompt_tokens_total"
_PREDICTED_TOKENS = "llamacpp:tokens_predicted_total"
_REQ_PROCESSING = "llamacpp:requests_processing"


def log(msg: str) -> None:
    print(f"[idle_watchdog] {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}",
          flush=True)


def http_get(url: str, timeout: float) -> bytes:
    headers = {"User-Agent": "idle-watchdog/1.0"}
    if API_KEY:
        headers["Authorization"] = "Bearer " + API_KEY
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def health_ok() -> bool:
    """/health is exempt from llama-server auth; {"status":"ok"} = serving."""
    try:
        body = json.loads(http_get(HEALTH_URL, 5).decode("utf-8", "replace"))
        return body.get("status") == "ok"
    except Exception:  # noqa: BLE001 - any failure = not ready yet
        return False


def activity() -> "float | None":
    """Scrape /metrics -> activity score (token counters sum), or None when the
    scrape failed (a failed scrape is a SKIPPED tick, never an idle tick).
    A request in flight (requests_processing > 0) counts as activity by
    returning a score bumped above the previous one."""
    try:
        body = http_get(METRICS_URL, 10).decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ConnectionError, OSError) as e:
        log(f"metrics scrape failed ({e.__class__.__name__}: {e}) — tick skipped")
        return None
    total = 0.0
    processing = 0.0
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition(" ")
        if name not in (_PROMPT_TOKENS, _PREDICTED_TOKENS, _REQ_PROCESSING):
            continue
        try:
            value = float(rest.rsplit(None, 1)[-1])
        except ValueError:
            continue
        if name == _REQ_PROCESSING:
            processing = value
        else:
            total += value
    if processing > 0:
        # A request is in flight: report "changed" so the timer resets.
        return total + 1.0
    return total


def pid1_alive() -> bool:
    """/proc/1 gone = PID 1 exited. A zombie PID 1 counts as dead (the
    container is on its way out; it can't be signalled meaningfully)."""
    try:
        with open("/proc/1/status") as f:
            for line in f:
                if line.startswith("State:"):
                    return "Z" not in line.split()[1]
        return True
    except OSError:
        return False


def terminate_pid1() -> int:
    """SIGTERM to PID 1 — the image CMDs make this work: PID 1 is the CMD bash
    running llama-server as a child with a SIGTERM trap, so the signal is
    delivered (handler installed), the child is killed, bash exits, and PID
    namespace teardown reaps the rest. llama-server itself installs a SIGTERM
    handler, so a direct SIGTERM (older images where it IS PID 1) shuts it
    down gracefully too.

    A handler-less PID 1 ignores SIGTERM, and the kernel DROPS the SIGKILL
    escalation as well (verified in-container 2026-10-08: kill(1, SIGKILL)
    returned success and PID 1 stayed alive — signals from inside a PID
    namespace never reach a handler-less init). The escalation is kept as
    best-effort; if it fails, the log names the real fix (CMD supervisor)."""
    try:
        os.kill(1, signal.SIGTERM)
    except OSError as e:
        log(f"kill(1) SIGTERM failed: {e} — exiting without kill")
        return 1
    for _ in range(SIGKILL_GRACE_S):
        time.sleep(1)
        if not pid1_alive():
            return 0
    log("PID 1 ignored SIGTERM (no handler installed) — trying SIGKILL "
        "(the kernel drops it too for a handler-less PID-namespace init; "
        "the image CMD must run llama-server under a trapping supervisor)")
    try:
        os.kill(1, signal.SIGKILL)
    except OSError as e:
        log(f"kill(1) SIGKILL failed: {e}")
    for _ in range(SIGKILL_GRACE_S):
        time.sleep(1)
        if not pid1_alive():
            return 0
    log("PID 1 survived SIGTERM+SIGKILL — unkillable from inside this "
        "namespace; container stays up")
    return 2


def main() -> int:
    mode = (os.environ.get("IDLE_SHUTDOWN") or "none").strip().lower()
    if mode in ("", "none"):
        log("IDLE_SHUTDOWN=none — feature off, exiting")
        return 0
    if mode not in ("idle", "heartbeat"):
        log(f"unknown IDLE_SHUTDOWN={mode!r} (want none|idle|heartbeat) — feature off, exiting")
        return 0

    def seconds(name: str, default: int) -> int:
        raw = (os.environ.get(name) or "").strip()
        try:
            value = int(raw)
        except ValueError:
            return default
        return value if value > 0 else default

    timeout = seconds("HEARTBEAT_TIMEOUT", 600) if mode == "heartbeat" else seconds("IDLE_TIMEOUT", 600)
    grace = seconds("IDLE_GRACE", 1800)
    # Adaptive scrape period: at least 3 scrapes per timeout window, so a
    # small timeout (e.g. 30 s in testing) fires within ~timeout, not up to
    # timeout + 30 s late. The default 600 s keeps the 30 s period
    # (min() caps at POLL_S); a 5 s floor keeps /metrics unharmed.
    poll_s = min(POLL_S, max(timeout / 3.0, POLL_MIN_S))
    log(f"mode={mode} timeout={timeout}s grace={grace}s poll={poll_s:.0f}s "
        f"— arming: waiting for {HEALTH_URL} ok")

    # Arm only once llama-server is serving; never kill mid-download/boot.
    arm_deadline = time.monotonic() + grace
    while not health_ok():
        if time.monotonic() >= arm_deadline:
            log(f"/health never came up within IDLE_GRACE={grace}s — giving up (no kill)")
            return 0
        time.sleep(HEALTH_PROBE_S)
    log("llama-server healthy — idle timer armed")

    last = activity()
    if last is None:
        last = 0.0
    last_activity = time.monotonic()
    ticks = 0

    while True:
        time.sleep(poll_s)
        ticks += 1
        score = activity()
        if score is None:
            continue  # scrape failure: skip, do not advance the idle timer
        if score != last:
            # Counters moved (chat traffic, keepalive ping, in-flight request,
            # or a counter reset after a server restart) — activity.
            last = score
            last_activity = time.monotonic()
        idle_s = time.monotonic() - last_activity
        if idle_s >= timeout:
            log(f"idle {idle_s:.0f}s >= {timeout}s (mode {mode}) — "
                f"terminating llama-server (PID 1); container exits, group stops")
            return terminate_pid1()
        if ticks % STATUS_EVERY_TICKS == 0:
            log(f"alive: idle {idle_s:.0f}s / {timeout}s, activity score {last:.0f}")


if __name__ == "__main__":
    sys.exit(main())
