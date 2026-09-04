#!/bin/bash
# Ask a question to the qwen38-llama-fa-api server (port 8081) and print ONLY the answer.
# Usage: ./crl2.sh [--think] [question...]     (default question if none given)
#   --think  enable Qwen's thinking mode (slower; bigger token budget).
# By default thinking is disabled via chat_template_kwargs — otherwise short
# max_tokens budgets get consumed entirely by reasoning_content and the
# actual answer comes back empty (finish_reason=length).
set -euo pipefail

cd "$(dirname "$0")"   # so api.txt resolves no matter where you call it from
API_KEY="$(tr -d '[:space:]' < api.txt 2>/dev/null || true)"

THINK=0
[ "${1:-}" = "--think" ] && { THINK=1; shift; }

QUESTION="$*"
[ -n "$QUESTION" ] || QUESTION="What is 27*43? Answer with just the number."

curl -s http://localhost:8081/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${API_KEY}" \
  --data "$(python3 -c 'import json,sys;q,t=sys.argv[1],sys.argv[2]=="1";p={"model":"qwen","messages":[{"role":"user","content":q}],"max_tokens":1500 if t else 500};p["chat_template_kwargs"]={} if t else {"enable_thinking":False};print(json.dumps(p))' "$QUESTION" "$THINK")" \
| python3 -c 'import json,sys; print(json.load(sys.stdin)["choices"][0]["message"]["content"])'