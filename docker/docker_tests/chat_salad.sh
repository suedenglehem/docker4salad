#!/bin/bash
# Chat with the Qwen3.8-27B running on SaladCloud through Simon Willison's
# `llm` package, via the local salad_proxy (which injects the Salad-Api-Key
# header the gateway demands — llm cannot set custom headers itself, and
# talking to the gateway directly makes llm hang).
#
# Usage:
#   ./chat_salad.sh                # interactive chat (--chat)
#   ./chat_salad.sh "your prompt"  # one-shot, then exit
#   ./chat_salad.sh -- -s "sys" ...  # everything after -- is passed to llm
#
# Config via env:
#   SALAD_UPSTREAM   access domain (default raisin-bean-gy0v5oyd2bt9wfvy.salad.cloud)
#   SALAD_PORT       public port   (default 443 — Cloudflare only proxies
#                    standard ports; never the in-container gateway port 8888)
#   SALAD_PROXY_PORT local proxy port (default 8931)
#
# The proxy is auto-started (or restarted if it points at a stale upstream).
# Readiness: the gateway's LB only routes to replicas whose readiness probe
# (GET /ready:8889, run inside the container) passed, so before chatting we
# poll /health through the proxy — a 200 means a ready replica is serving.
set -u

cd "$(dirname "$0")"   # so salad_proxy.py / salad_api.txt resolve

UPSTREAM="${SALAD_UPSTREAM:-raisin-bean-gy0v5oyd2bt9wfvy.salad.cloud}"
PORT="${SALAD_PORT:-443}"
PROXY_PORT="${SALAD_PROXY_PORT:-8931}"
MODEL="${SALAD_MODEL:-qwen}"

# ---- proxy: start / restart if stale ----------------------------------------
ensure_proxy() {
  local expected="https://${UPSTREAM}:${PORT}"
  local current
  current="$(curl -s --max-time 2 "http://127.0.0.1:${PROXY_PORT}/__upstream" 2>/dev/null)"
  if [ "$current" != "$expected" ]; then
    if [ -f "/tmp/salad_proxy.${PROXY_PORT}.pid" ]; then
      kill "$(cat "/tmp/salad_proxy.${PROXY_PORT}.pid")" 2>/dev/null || true
      sleep 1
    fi
    nohup python3 salad_proxy.py "${UPSTREAM}:${PORT}" "${PROXY_PORT}" \
      >>/tmp/salad_proxy.log 2>&1 &
    for _ in $(seq 1 40); do
      current="$(curl -s --max-time 2 "http://127.0.0.1:${PROXY_PORT}/__upstream" 2>/dev/null)"
      [ "$current" = "$expected" ] && return 0
      sleep 0.5
    done
    echo "error: proxy did not come up on port ${PROXY_PORT} (see /tmp/salad_proxy.log)" >&2
    return 1
  fi
  return 0
}

# ---- wait for a ready replica (gateway LB only routes ready ones) -----------
wait_ready() {
  local i status
  for i in $(seq 1 60); do
    status="$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PROXY_PORT}/health" 2>/dev/null)"
    [ "$status" = "200" ] && return 0
    [ $i -eq 1 ] && echo "waiting for a ready replica at ${UPSTREAM} (probe /ready:8889 inside the box)..."
    sleep 5
  done
  echo "warning: /health not returning 200 after 5 min — trying anyway" >&2
  return 0
}

ensure_proxy || exit 1
wait_ready

# ---- chat --------------------------------------------------------------------
# `llm openai endpoint URL` sets the openai SDK base_url; the SDK itself
# appends /chat/completions, so the URL must end at /v1 — NOT at
# /v1/chat/completions (that yields /v1/chat/completions/chat/completions → 404).
LLM_BIN="$(command -v llm || echo "$HOME/.local/bin/llm")"

if [ $# -gt 0 ]; then
  # One-shot (or explicit llm args after --)
  exec "$LLM_BIN" openai endpoint "http://127.0.0.1:${PROXY_PORT}/v1" -m "$MODEL" "$@"
else
  # Interactive chat
  exec "$LLM_BIN" openai endpoint "http://127.0.0.1:${PROXY_PORT}/v1" -m "$MODEL" --chat
fi
