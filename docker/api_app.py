"""Status API for the qwen38-llama container (served by uvicorn, see Dockerfile.multistage).

Probe-style endpoints that mirror how the container boots:

  GET /startup -> 200 {"status": "ok"} once the model-download phase has begun.
                  The start script touches ${API_STATE_DIR}/download_started when it
                  launches `hf download` (or immediately if the model file is already
                  present, i.e. a container restart). 503 until then.
  GET /live    -> 200 while a llama-server process is running in this container
                  (checked via /proc, no extra packages needed). 503 otherwise.
  GET /ready   -> 200 when llama-server answers its own /health endpoint with
                  {"status": "ok"} — i.e. it is up AND healthy (model loaded,
                  serving). 503 otherwise.

  GET /        -> convenience summary of all three states (for `curl localhost:9999/`).

Env config:
  PORT           llama-server port inside the container (default 8080) — used by /ready
  API_STATE_DIR  directory holding the download_started flag (default /tmp/llama-api)

Only stdlib + fastapi/uvicorn are used (no requests/httpx), keeping the image lean.
"""

import json
import os
import time
import urllib.request

from fastapi import FastAPI
from fastapi.responses import JSONResponse

START_TIME = time.time()
STATE_DIR = os.environ.get("API_STATE_DIR", "/tmp/llama-api")
DOWNLOAD_FLAG = os.path.join(STATE_DIR, "download_started")
LLAMA_PORT = int(os.environ.get("PORT", "8080"))
HEALTH_URL = f"http://127.0.0.1:{LLAMA_PORT}/health"

app = FastAPI(title="qwen38 status API", version="1.0")


def _payload(status: str, **extra) -> dict:
    body = {
        "status": status,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    body.update(extra)
    return body


def _ok(detail: str) -> JSONResponse:
    return JSONResponse(_payload("ok", detail=detail))


def _fail(status: str, detail: str) -> JSONResponse:
    return JSONResponse(_payload(status, detail=detail), status_code=503)


# ---------------------------------------------------------------------------
# State checks — each returns (is_ok, human_readable_detail)
# ---------------------------------------------------------------------------

def _startup_state():
    if os.path.exists(DOWNLOAD_FLAG):
        return True, "model download phase started"
    return False, "model download has not started yet"


def _live_state():
    # Scan /proc for a running llama-server process. comm is truncated to 15
    # chars by the kernel; "llama-server" fits. No procps package needed.
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() == "llama-server":
                    return True, f"llama-server running (pid {pid})"
        except OSError:
            continue  # process exited between listdir and open
    return False, "no llama-server process found"


def _ready_state():
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as r:
            body = json.loads(r.read().decode("utf-8"))
            if r.status == 200 and body.get("status") == "ok":
                return True, "llama-server /health reports ok"
            return False, f"llama-server /health returned HTTP {r.status}: {body}"
    except Exception as e:  # noqa: BLE001 - any failure means "not ready yet"
        return False, f"llama-server health check failed ({e.__class__.__name__})"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/startup")
def startup():
    ok, detail = _startup_state()
    return _ok(detail) if ok else _fail("waiting", detail)


@app.get("/live")
def live():
    ok, detail = _live_state()
    return _ok(detail) if ok else _fail("down", detail)


@app.get("/ready")
def ready():
    ok, detail = _ready_state()
    return _ok(detail) if ok else _fail("not_ready", detail)


@app.get("/")
def index():
    """Debug helper: all three probe states in one response."""
    out = {}
    for name, check in (
        ("startup", _startup_state),
        ("live", _live_state),
        ("ready", _ready_state),
    ):
        ok, detail = check()
        out[name] = {"status": "ok" if ok else "not_ok", "detail": detail}
    out["uptime_seconds"] = round(time.time() - START_TIME, 1)
    return JSONResponse(out)
