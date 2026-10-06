#!/bin/bash
# Chat with the Qwen3.8-27B running on SaladCloud through Simon Willison's
# `llm` package, direct against the public gateway. llm 0.36+ can send the
# `Salad-Api-Key` header itself (via -H), so no local proxy is needed — the
# proxy is now the Anthropic bridge that cl_salad uses and does not serve
# OpenAI chat completions at all.
#
# Usage:
#   ./chat_salad.sh                 # interactive chat
#   ./chat_salad.sh "your prompt"   # one-shot, then exit
#   ./chat_salad.sh -s "sys" "prompt"   # extra args pass through to llm
#
# Config via env:
#   SALAD_UPSTREAM   access domain (default: the production qwen38-27b-q6k
#                    gateway corn-cabbage-2yk4e98r3rx752n0.salad.cloud)
#   SALAD_PORT       public port   (default 443 — Cloudflare only proxies
#                    standard ports; never the in-container gateway port 8888)
#   SALAD_MODEL      model alias   (default qwen38-27b)
#   SALAD_KEYFILE    file holding the Salad-Api-Key (default: ./salad_api.txt)
#
# Readiness: /v1/models is checked ONCE — 200 means a ready instance is
# serving. 404 = group stopped or still downloading; 403 = gateway rejects
# the key/group. If not ready we die (exit 2, hint per code), like cl_salad.
# Set SALAD_WAIT=1 to poll for up to 5 min instead (group mid cold start).
set -u

cd "$(dirname "$0")"   # so salad_api.txt resolves no matter where you call it from

UPSTREAM="${SALAD_UPSTREAM:-corn-cabbage-2yk4e98r3rx752n0.salad.cloud}"
PORT="${SALAD_PORT:-443}"
MODEL="${SALAD_MODEL:-qwen38-27b}"
KEYFILE="${SALAD_KEYFILE:-salad_api.txt}"

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

# ---- chat --------------------------------------------------------------------
# `llm openai endpoint URL` sets the OpenAI SDK base_url; the SDK itself
# appends /chat/completions, so the URL must end at /v1 — NOT at
# /v1/chat/completions (that yields /v1/chat/completions/chat/completions → 404).
# -H sends the Salad-Api-Key header on every request — llm has no other way.
LLM_BIN="$(command -v llm || echo "$HOME/.local/bin/llm")"
BASE="https://${UPSTREAM}:${PORT}/v1"

if [ $# -gt 0 ]; then
  # One-shot (extra args pass through to llm, e.g. -s "sys")
  exec "$LLM_BIN" openai endpoint "$BASE" -m "$MODEL" -H "Salad-Api-Key" "$SALAD_KEY" "$@"
else
  # Interactive chat
  exec "$LLM_BIN" openai endpoint "$BASE" -m "$MODEL" -H "Salad-Api-Key" "$SALAD_KEY" --chat
fi
