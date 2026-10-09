#!/usr/bin/env python3
"""Idle/heartbeat self-shutdown watchdog for the Salad container.

Started as a background helper by every image CMD (the 4th `nohup … &` line,
before `exec llama-server`). Its ONLY job: when the group has been configured
for idle-shutdown, stop the GROUP after a configurable window of inactivity.
Kill = POST the Salad group /stop endpoint (the group stops — free), then
SIGTERM PID 1 (the container exits; belt-and-suspenders if the API call
failed). This is the billing hygiene: a lost client connection can no longer
leave a GPU group idling for hours.

WHY THE API CALL IS MANDATORY (paid test, 2026-10-09): container exit under
restart_policy=never does NOT leave the group STOPPED — Salad RESCHEDULES a
new instance and the group stays `running`, i.e. a self-restart loop that
bills ~6 min of GPU per cycle. restart_policy governs container restarts
WITHIN an instance, not group-level rescheduling. Only the /stop endpoint
actually stops the group. Without SALAD_STOP_KEY configured the watchdog
EXITS (feature disabled) — the old SIGTERM-only behavior was a reschedule
loop that bills, so arming without a key is pointless.

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
  - The timer ARMS ONLY after the group's readiness endpoint answers ok:
    api_app's `http://127.0.0.1:${READY_PORT}/ready` — the EXACT endpoint the
    Salad probe polls (probe :8889/ready -> socat -> api_app :9999/ready).
    NOT llama-server /health: that comes up minutes before the probe (period
    120 s) marks the group ready, and arming on it killed the group before
    clients waiting for "ready" ever got a window (proven by the 2026-10-09
    paid test). Model download takes hours; the watchdog never kills mid-boot.
  - After /ready first answers ok, the watchdog HOLDS IDLE_ARM_GRACE seconds
    (default 150 >= probe period 120 s) before starting the idle countdown:
    the probe needs a full tick to flip the group to running/ready, and
    clients polling for readiness must see it before the kill window opens.
  - If /ready never comes up within IDLE_GRACE seconds, the watchdog gives
    up (exit 0, no kill) — a broken boot is the readiness probe's problem,
    not the watchdog's.
  - A failed /metrics scrape is a skipped tick, never an "idle" tick: only
    confirmed no-activity advances the kill timer.
  - Kill = group /stop POST, THEN SIGTERM to PID 1. VERIFIED (container
    test, 2026-10-08): the kernel drops EVERY signal — SIGKILL included —
    sent from inside a PID namespace to PID 1 when PID 1 has no handler
    installed. A watchdog inside the container can therefore never kill a
    handler-less PID 1. The image CMDs make PID 1 killable by construction:
    llama-server runs as a CHILD of the CMD bash, which installs a SIGTERM
    trap (handler installed -> delivered), kills the child, and exits; PID
    -namespace teardown then reaps everything left.
  - IDLE_SHUTDOWN=none (default) -> exits immediately, does nothing.
  - No usable group /stop config (SALAD_STOP_KEY is "none"/missing/
    undecodable, or SALAD_ORG/SALAD_PROJECT/SALAD_GROUP incomplete) -> the
    watchdog exits immediately, feature DISABLED: a self-exit without the
    /stop call gets rescheduled by Salad and keeps billing, so arming
    without a key is pointless (cleaner than firing a useless kill).

Group stop API (stdlib urllib POST, no body, 202 Accepted):
  https://api.salad.com/api/public/organizations/${SALAD_ORG}/projects/
  ${SALAD_PROJECT}/containers/${SALAD_GROUP}/stop  with header Salad-Api-Key.
  Salad has NO group-scoped keys — the API token is per-user and account-wide
  (docs/salad/reference/api-usage.mdx); the deployer injects the account key
  (user-approved) stored obfuscated as "b64:<base64>" (see SALAD_STOP_KEY).
  The key is never logged.

Env knobs (all baked defaults, group-overridable via the deployers):
  IDLE_SHUTDOWN   none (off) | idle | heartbeat
  IDLE_TIMEOUT    seconds of no chat traffic -> kill (mode idle)
  HEARTBEAT_TIMEOUT seconds of no pings      -> kill (mode heartbeat)
  IDLE_GRACE      max seconds to wait for the first /ready ok before giving up
  IDLE_ARM_GRACE  seconds to hold after /ready ok before the countdown starts
  READY_PORT      api_app port serving /ready (default 9999)
  PORT            llama-server port (default 8080)
  API_KEY         llama-server auth key; sent as Bearer when set (local runs
                  pass it; Salad groups run llama-server without auth)
  SALAD_STOP_KEY  Salad API key for the /stop call (account-wide — Salad has
                  no group-scoped keys). Deployers store it obfuscated as
                  "b64:<base64>" (decoded here; basic obfuscation to keep the
                  plaintext out of the group env, NOT encryption — anyone who
                  can read the env can decode it). Plain keys keep working
                  (legacy groups); sentinel "none" = skip the API call,
                  SIGTERM only.
  SALAD_ORG / SALAD_PROJECT / SALAD_GROUP   stop-endpoint path components
  SALAD_API_BASE  API server override (default https://api.salad.com/api/public;
                  stub tests point it at localhost)

Stdlib only (urllib), matching the repo convention (api_app.py, llama_stats.py).
Logs go to ${API_STATE_DIR}/watchdog.log via the CMD's nohup redirect.
"""

from __future__ import annotations

import base64
import os
import signal
import sys
import time
import urllib.error
import urllib.request

POLL_S = 30.0            # default metrics scrape period (small timeouts poll faster)
POLL_MIN_S = 5.0         # floor on the adaptive scrape period (never hammer /metrics)
READY_PROBE_S = 30.0     # /ready poll period while arming
STATUS_EVERY_TICKS = 10  # log a heartbeat line every ~5 min
SIGKILL_GRACE_S = 10     # SIGTERM grace before escalating to SIGKILL
STOP_ATTEMPTS = 2        # group /stop POST attempts (the API is the real kill)
STOP_RETRY_S = 5.0       # pause between stop attempts
# Overridable for stub tests / staging; production uses the spec server.
SALAD_API_BASE = os.environ.get("SALAD_API_BASE") or "https://api.salad.com/api/public"

PORT = int(os.environ.get("PORT", "8080"))
READY_PORT = int(os.environ.get("READY_PORT", "9999"))
API_KEY = os.environ.get("API_KEY") or None
READY_URL = f"http://127.0.0.1:{READY_PORT}/ready"
METRICS_URL = f"http://127.0.0.1:{PORT}/metrics"

# Activity counters (names verified against llama.cpp b10572, see llama_stats.py).
_PROMPT_TOKENS = "llamacpp:prompt_tokens_total"
_PREDICTED_TOKENS = "llamacpp:tokens_predicted_total"
_REQ_PROCESSING = "llamacpp:requests_processing"


def log(msg: str) -> None:
    print(f"[idle_watchdog] {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}",
          flush=True)


# Group /stop config (deployer-injected). SALAD_STOP_KEY forms:
#   "b64:<base64>" - obfuscated key (deployer default; keeps the plaintext out
#                    of the group env, visible via API GET). Basic obfuscation,
#                    NOT encryption — anyone who can read the env can decode it.
#   "<key>"        - plain key (legacy groups keep working).
#   "none"/""/unset - no key: the watchdog DISABLES itself (see main()).
STOP_KEY = os.environ.get("SALAD_STOP_KEY")
if STOP_KEY in ("", "none"):
    STOP_KEY = None
elif STOP_KEY.startswith("b64:"):
    try:
        STOP_KEY = base64.b64decode(STOP_KEY[4:], validate=True).decode("ascii")
        if not STOP_KEY:
            raise ValueError("empty after decode")
    except Exception as e:  # noqa: BLE001 - malformed sentinel: disable, never crash
        log(f"SALAD_STOP_KEY has the 'b64:' prefix but failed to decode "
            f"({e.__class__.__name__}: {e}) — treating as no stop key")
        STOP_KEY = None
STOP_PATH = ""
if all(os.environ.get(k) for k in ("SALAD_ORG", "SALAD_PROJECT", "SALAD_GROUP")):
    STOP_PATH = (f"/organizations/{os.environ['SALAD_ORG']}/projects/"
                 f"{os.environ['SALAD_PROJECT']}/containers/{os.environ['SALAD_GROUP']}/stop")


def http_get(url: str, timeout: float) -> bytes:
    headers = {"User-Agent": "idle-watchdog/1.0"}
    if API_KEY:
        headers["Authorization"] = "Bearer " + API_KEY
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def ready_ok() -> bool:
    """api_app /ready is the endpoint the Salad probe polls; HTTP 200 = the
    group is ready (llama-server up AND model loaded). urllib raises
    HTTPError on 404/503, so any 2xx here means ready."""
    try:
        http_get(READY_URL, 5)
        return True
    except Exception:  # noqa: BLE001 - any failure = not ready yet
        return False


def stop_group() -> bool:
    """POST the Salad group /stop endpoint — the ONLY thing that truly stops
    a group (self-exit gets rescheduled; see module docstring). Key never
    logged. Best-effort: on failure the caller still SIGTERMs, and the log
    says the group will reschedule."""
    if not STOP_KEY or not STOP_PATH:
        log("no stop-API config (SALAD_STOP_KEY / SALAD_ORG / SALAD_PROJECT / "
            "SALAD_GROUP) — skipping group stop; SIGTERM only (Salad will "
            "reschedule a new instance — the group keeps billing)")
        return False
    for attempt in range(1, STOP_ATTEMPTS + 1):
        req = urllib.request.Request(SALAD_API_BASE + STOP_PATH, method="POST")
        req.add_header("Salad-Api-Key", STOP_KEY)
        req.add_header("User-Agent", "idle-watchdog/1.0")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if 200 <= resp.status < 300:
                    log(f"group /stop accepted (HTTP {resp.status}) — group stops, "
                        f"no reschedule")
                    return True
                log(f"group /stop attempt {attempt}: HTTP {resp.status} "
                    f"{resp.reason}")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                ConnectionError, OSError) as e:
            log(f"group /stop attempt {attempt} failed "
                f"({e.__class__.__name__}: {e})")
        if attempt < STOP_ATTEMPTS:
            time.sleep(STOP_RETRY_S)
    log("group /stop FAILED after all attempts — SIGTERM self-exit only; "
        "Salad will reschedule a new instance (group keeps billing until "
        "stopped externally)")
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
    if not (STOP_KEY and STOP_PATH):
        log("no usable group /stop config (SALAD_STOP_KEY is 'none'/missing/"
            "undecodable, or SALAD_ORG/SALAD_PROJECT/SALAD_GROUP incomplete) — "
            "watchdog DISABLED: a self-exit without the /stop call gets "
            "rescheduled by Salad and keeps billing, so arming without a key "
            "is pointless. Exiting, feature off (no kill)")
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
    arm_grace = seconds("IDLE_ARM_GRACE", 150)
    # Adaptive scrape period: at least 3 scrapes per timeout window, so a
    # small timeout (e.g. 30 s in testing) fires within ~timeout, not up to
    # timeout + 30 s late. The default 600 s keeps the 30 s period
    # (min() caps at POLL_S); a 5 s floor keeps /metrics unharmed.
    poll_s = min(POLL_S, max(timeout / 3.0, POLL_MIN_S))
    log(f"mode={mode} timeout={timeout}s grace={grace}s arm_grace={arm_grace}s "
        f"poll={poll_s:.0f}s stop_api={'configured' if (STOP_KEY and STOP_PATH) else 'OFF'} "
        f"— arming: waiting for {READY_URL} ok")

    # Arm only once the GROUP is ready (the probe's own endpoint); never kill
    # mid-download/boot, and never before clients can see "ready".
    arm_deadline = time.monotonic() + grace
    while not ready_ok():
        if time.monotonic() >= arm_deadline:
            log(f"/ready never came up within IDLE_GRACE={grace}s — giving up (no kill)")
            return 0
        time.sleep(READY_PROBE_S)
    log(f"group ready — holding IDLE_ARM_GRACE={arm_grace}s so the probe flips "
        f"the group running and clients get a window before the countdown")
    time.sleep(arm_grace)
    log("idle timer armed")

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
            log(f"idle {idle_s:.0f}s >= {timeout}s (mode {mode}) — stopping the "
                f"group, then terminating llama-server (PID 1)")
            stop_group()
            return terminate_pid1()
        if ticks % STATUS_EVERY_TICKS == 0:
            log(f"alive: idle {idle_s:.0f}s / {timeout}s, activity score {last:.0f}")


if __name__ == "__main__":
    sys.exit(main())
