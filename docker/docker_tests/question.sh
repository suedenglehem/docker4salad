#!/bin/sh
# Quick question to the llama-server: POST one fixed prompt to
# /v1/chat/completions and print the raw JSON response.
#
# Usage: ./question.sh [-p port] [-m model]
#   -p port    server port             (default 8080)
#   -m model   model id in the request body (default qwen38-27b = the image's MODEL_ALIAS default)
#
# Thinking mode is disabled via chat_template_kwargs — otherwise short
# max_tokens budgets get consumed entirely by reasoning_content and the
# answer comes back empty (finish_reason=length). See curl2.sh.
port=8080
model="qwen38-27b"

while [ $# -gt 0 ]; do
  case $1 in
    -p|--port)  shift; port=$1 ;;
    -m|--model) shift; model="$1" ;;
  esac
  shift
done

data="{\"model\":\"${model}\",\"messages\":[{\"role\":\"user\",\"content\":\"Capital of France?\"}],\"max_tokens\":200,\"chat_template_kwargs\":{\"enable_thinking\":false}}"

curl -sf "http://127.0.0.1:${port}/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "$data"
