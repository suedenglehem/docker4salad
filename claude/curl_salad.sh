#!/bin/bash
# Smoke-test the LLM running on SaladCloud (the qwen38-27b-q6k container
# group) by asking 2-3 simple questions through its container gateway.
#
# Two INDEPENDENT keys, two different headers — do not mix them up:
#
#   * Salad gateway key (./salad_api.txt): the group's Container Gateway has
#     auth enabled, so EVERY request must carry the Salad API key as a
#     `Salad-Api-Key` header. Docs: container-engine/reference/recipes —
#     "Enabled: every request must include the Salad-Api-Key header."
#
#   * LLM (llama-server) key (-api): sent as `Authorization: Bearer KEY`.
#     Only sent when passed — the deployed group runs llama-server without a
#     key, so the header is omitted by default.
#
# Usage: ./curl_salad.sh -url URL [-p PORT] [-api APIKEY] [-m MODEL]
#   -url URL    SaladCloud access domain, e.g. "https://raisin-bean-...salad.cloud"
#   -p PORT     port of the PUBLIC endpoint (default 443 — the access domain is
#               fronted by Cloudflare, which only proxies standard ports). Do NOT
#               put the "Container Gateway" port (8888) here: that is the port the
#               service listens on INSIDE the container, and salad.cloud will not
#               forward it — connections to https://<dns>:8888 just time out.
#   -api KEY    llama-server API key, sent as "Authorization: Bearer KEY".
#               When not passed, no Authorization header is sent.
#   -m MODEL    model id in the request body (default "qwen")
#
# The endpoint becomes "<url>:<port>/v1/chat/completions" (port 443 by default).
#
# Thinking mode is off via chat_template_kwargs — otherwise short max_tokens
# budgets get consumed entirely by reasoning_content and the answer comes back
# empty (finish_reason=length).
#
# Note: `set -u` only (no `-e`) so that if one question fails the rest are
# still attempted — a test wants all results, not to stop at the first hiccup.
set -u

cd "$(dirname "$0")"   # so salad_api.txt resolves no matter where you call it from

URL=""
PORT=443
API_KEY=""
MODEL="qwen"

while [ $# -gt 0 ]; do
  case $1 in
    -url|--url)   shift; URL="$1" ;;
    -p|--port)    shift; PORT="$1" ;;
    -api|--api)   shift; API_KEY="$1" ;;
    -m|--model)   shift; MODEL="$1" ;;
    *) echo "unknown argument: $1 (see header for usage)" >&2; exit 2 ;;
  esac
  shift
done

# Salad gateway key: read from ./salad_api.txt (Salad's own auth key — NOT the
# LLM's). The gateway with auth enabled rejects requests that lack it.
SALAD_KEY="$(tr -d '[:space:]' < salad_api.txt 2>/dev/null || true)"

if [ -z "$URL" ]; then
  echo "error: -url URL is required (e.g. https://raisin-bean-...salad.cloud)" >&2
  exit 2
fi

# Be forgiving about a missing scheme and a trailing slash.
case $URL in http://*|https://*) ;; *) URL="https://${URL}" ;; esac
ENDPOINT="${URL%/}:${PORT}/v1/chat/completions"

CURL_HEADERS=(-H 'Content-Type: application/json')
[ -n "$SALAD_KEY" ] && CURL_HEADERS+=(-H "Salad-Api-Key: ${SALAD_KEY}")
[ -n "$API_KEY" ] && CURL_HEADERS+=(-H "Authorization: Bearer ${API_KEY}")

QUESTIONS=(
  "What is 27*43? Answer with just the number."
  "What is the capital of France? Answer with just the city name."
  "Name a programming language in one word."
)

# POST one question with the header set built above.
ask() {
  local payload
  payload="$(python3 -c 'import json,sys
print(json.dumps({"model":sys.argv[1],"messages":[{"role":"user","content":sys.argv[2]}],"max_tokens":200,"chat_template_kwargs":{"enable_thinking":False}}))' "$MODEL" "$1")"
  curl -s "${ENDPOINT}" "${CURL_HEADERS[@]}" --data "$payload"
}

echo "endpoint:   ${ENDPOINT}"
echo "gateway key: $([ -n "$SALAD_KEY" ] && echo 'Salad-Api-Key set (from ./salad_api.txt)' || echo 'none (no ./salad_api.txt)')"
echo "llm key:    $([ -n "$API_KEY" ] && echo 'Authorization Bearer set (from -api)' || echo 'none (no -api)')"
echo

for q in "${QUESTIONS[@]}"; do
  printf '### Q: %s\n' "$q"
  resp="$(ask "$q")"
  if [ -z "$resp" ]; then
    echo "!! no response (is the container up and reachable?)"
  else
    printf '%s\n' "$resp" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin)["choices"][0]["message"]["content"])
except Exception as e:
    print("!! could not parse response:", e)'
  fi
  echo
done
