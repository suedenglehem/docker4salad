#!/bin/bash
# Chat with the Qwen3.8-27B running on SaladCloud through Simon Willison's
# `llm` package, direct against the public gateway. llm 0.36+ can send the
# `Salad-Api-Key` header itself (via -H), so no local proxy is needed — the
# proxy is now the Anthropic bridge that cl_salad uses and does not serve
# OpenAI chat completions at all.
#
# Usage:
#   ./chat_salad.sh                 # interactive chat (default gateway)
#   ./chat_salad.sh https://<dns>.salad.cloud        # interactive, that gateway
#   ./chat_salad.sh "your prompt"   # one-shot, then exit
#   ./chat_salad.sh https://<dns>.salad.cloud "prompt"   # one-shot, that gateway
#   ./chat_salad.sh -s "sys" "prompt"   # extra args pass through to llm
#
# Config via env:
#   SALAD_UPSTREAM   access domain (default: the atx-hb-test2 gateway
#                    honey-coleslaw-w2pzfno529hedl3j.salad.cloud;
#                    a leading URL ARGUMENT overrides it, same as cl_salad)
#   SALAD_PORT       public port   (default 443 — Cloudflare only proxies
#                    standard ports; never the in-container gateway port 8888)
#   SALAD_MODEL      model alias   (default atx-swift-27b-opt — the alias
#                    atx-hb-test2 serves; q6k groups serve qwen38-27b)
#   SALAD_KEYFILE    file holding the Salad-Api-Key (default: ./salad_api.txt)
#   SALAD_HEARTBEAT  default 1 — send a 1-token keepalive to the gateway
#                    every 30 s while the chat runs, so a heartbeat-mode
#                    group survives the session (SALAD_HEARTBEAT=0 to opt
#                    out; harmless on groups without the watchdog)
#
# Readiness: /v1/models is checked ONCE — 200 means a ready instance is
# serving. 404 = group stopped or still downloading; 403 = gateway rejects
# the key/group. If not ready we die (exit 2, hint per code), like cl_salad.
# Set SALAD_WAIT=1 to poll for up to 5 min instead (group mid cold start).
set -u

cd "$(dirname "$0")"   # so salad_api.txt resolves no matter where you call it from

UPSTREAM="${SALAD_UPSTREAM:-honey-coleslaw-w2pzfno529hedl3j.salad.cloud}"
PORT="${SALAD_PORT:-443}"
MODEL="${SALAD_MODEL:-atx-swift-27b-opt}"
KEYFILE="${SALAD_KEYFILE:-salad_api.txt}"

# A leading URL argument overrides SALAD_UPSTREAM (cl_salad takes the gateway
# URL as its first argument — chat_salad follows the same convention).
if [ $# -gt 0 ]; then
  case "$1" in
    http://*|https://*)
      UPSTREAM="$(printf '%s' "$1" | sed -e 's#^[a-zA-Z]*://##' -e 's#/.*$##')"
      shift ;;
  esac
fi

if [ ! -f "$KEYFILE" ]; then
  echo "error: key file $KEYFILE not found (expected next to this script)" >&2
  exit 1
fi
SALAD_KEY="$(tr -d '[:space:]' < "$KEYFILE")"

check() {
  curl -s -o /dev/null -m 10 -w '%{http_code}' \
    -H "Salad-Api-Key: $SALAD_KEY" "https://${UPSTREAM}:${PORT}/v1/models" 2>/dev/null
}

code="$(check)" || code="000"
code="${code:-000}"
i=0
while [ "$code" != "200" ] && [ "${SALAD_WAIT:-0}" = "1" ] && [ $i -lt 60 ]; do
  [ $i -eq 0 ] && echo "gateway ${UPSTREAM} not ready (HTTP $code) — waiting up to 5 min (SALAD_WAIT=1)..."
  sleep 5
  i=$((i + 1))
  code="$(check)" || code="000"
  code="${code:-000}"
done

if [ "$code" != "200" ]; then
  echo "chat_salad: gateway $UPSTREAM is not serving (HTTP $code) — not starting chat." >&2
  case "$code" in
    000) echo "chat_salad: cannot reach the host at all (DNS / connection)." >&2
         echo "chat_salad: check the URL, or start the group with cl_salad_deploy." >&2 ;;
    404) echo "chat_salad: the group is not serving — it is stopped, or the model is" >&2
         echo "chat_salad: still loading. Start it with cl_salad_deploy (which waits" >&2
         echo "chat_salad: for readiness) and re-run chat_salad." >&2 ;;
    403) echo "chat_salad: the gateway rejects this key/group — it may be deleted, or" >&2
         echo "chat_salad: the URL / key may be wrong. Check with cl_salad_deploy --status." >&2 ;;
    *)   echo "chat_salad: unexpected response from /v1/models — the group may be" >&2
         echo "chat_salad: (re)starting. Use cl_salad_deploy, which waits for readiness." >&2 ;;
  esac
  exit 2
fi

# ---- heartbeat keepalive (opt-in: SALAD_HEARTBEAT=1) ------------------------
# When the group runs with IDLE_SHUTDOWN=heartbeat, the in-container watchdog
# kills the container HEARTBEAT_TIMEOUT s after token counters go flat. This
# loop posts a 1-token completion through the gateway every 30 s while the
# chat lives, so the group self-stops shortly after a dead connection (closed
# terminal, lost SSH) instead of billing for hours. Real chat traffic also
# counts as activity — the pinger is just the floor. ON BY DEFAULT
# (SALAD_HEARTBEAT=0 opts out).
HEARTBEAT_PID=""
cleanup() {
  [ -n "$HEARTBEAT_PID" ] && kill "$HEARTBEAT_PID" 2>/dev/null
}
trap cleanup EXIT
if [ "${SALAD_HEARTBEAT:-1}" = "1" ]; then
  (
    while :; do
      curl -m 10 -s -o /dev/null \
        -H "Salad-Api-Key: $SALAD_KEY" \
        -H "Content-Type: application/json" \
        -X POST "https://${UPSTREAM}:${PORT}/v1/completions" \
        -d "{\"model\":\"$MODEL\",\"prompt\":\".\",\"max_tokens\":1}" \
        2>/dev/null
      sleep 30
    done
  ) &
  HEARTBEAT_PID=$!
  echo "chat_salad: heartbeat keepalive ON (1-token call every 30 s; the group self-stops when this chat dies)" >&2
fi

# ---- chat --------------------------------------------------------------------
# `llm openai endpoint URL` sets the OpenAI SDK base_url; the SDK itself
# appends /chat/completions, so the URL must end at /v1 — NOT at
# /v1/chat/completions (that yields /v1/chat/completions/chat/completions → 404).
# -H sends the Salad-Api-Key header on every request — llm has no other way.
# No `exec` when the heartbeat runs: this shell must stay as the parent so
# the EXIT trap fires and kills the pinger when the chat ends.
LLM_BIN="$(command -v llm || echo "$HOME/.local/bin/llm")"
BASE="https://${UPSTREAM}:${PORT}/v1"

if [ $# -gt 0 ]; then
  # One-shot (extra args pass through to llm, e.g. -s "sys")
  "$LLM_BIN" openai endpoint "$BASE" -m "$MODEL" -H "Salad-Api-Key" "$SALAD_KEY" "$@"
else
  # Interactive chat
  "$LLM_BIN" openai endpoint "$BASE" -m "$MODEL" -H "Salad-Api-Key" "$SALAD_KEY" --chat
fi
status=$?
exit "$status"
