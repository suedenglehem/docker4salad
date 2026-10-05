#!/bin/sh

model="qwen3.8-fn"

case $1 in 
  -m) shift; model=$1 ;;
esac

llm openai endpoint http://localhost:8080/v1  -m "$model" --chat

exit $?
