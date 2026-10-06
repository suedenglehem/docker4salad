#!/bin/bash
# Quick API check for the qwen38-llama-fa-api server (port 8081, 9B on GPU 1)
# Thinking mode is disabled via chat_template_kwargs — otherwise short
# max_tokens budgets get consumed entirely by reasoning_content and the
# actual answer comes back empty (finish_reason=length). See crl2.sh.
cd "$(dirname "$0")"   # so api.txt resolves no matter where you call it from
API_KEY="$(tr -d '[:space:]' < api.txt 2>/dev/null || true)"
PORT=8080
while [ $# -gt 0 ]]
do
 case $1 in 
 -p|--port) shift; PORT=$1 ; shift ;;
 -a|--api)  shift; API_KEY="$1"; shift ;; 
  help|--help) echo `basename $0` "[-p port] [-a api]"; exit 0;;
 esac
done
 
curl -s http://localhost:${PORT}/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${API_KEY}" \
  -d '{"model":"qwen","messages":[{"role":"user","content":"That is the most populated city of the world?"}],"max_tokens":20,"chat_template_kwargs":{"enable_thinking":false}}'

