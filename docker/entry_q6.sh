#!/bin/bash
# Entrypoint for the lmss_qwen38_q6_mtp_vision image (baked Qwen3.8-27B-Uncensored
# draft + vision, q8_0 KV cache). Starts the helper daemons (api_app, two socat
# port forwarders, the idle/heartbeat watchdog, and the HF-download bandwidth
# reporter) in the background, downloads the main model on first start, then
# runs llama-server as a TRAPPED CHILD of this
# PID-1 bash (not exec'd) so the watchdog's SIGTERM lands. With the group's
# restart_policy=never that self-exit leaves the Salad group STOPPED (free).
# The draft and mmproj are baked into the image; USE_DRAFT_MODEL toggles the
# draft on/off. CWD is /opt/llama.cpp (the image WORKDIR); helpers are CWD-relative.

set -euo pipefail

mkdir -p "${MODEL_DIR}" "${API_STATE_DIR}"

nohup python3 run_api.py >>"${API_STATE_DIR}/api.log" 2>&1 &
nohup socat TCP6-LISTEN:8888,fork,reuseaddr TCP4:127.0.0.1:8080 >>"${API_STATE_DIR}/socat.log" 2>&1 &
nohup socat TCP6-LISTEN:8889,fork,reuseaddr TCP4:127.0.0.1:9999 >>"${API_STATE_DIR}/socat2.log" 2>&1 &
nohup python3 /usr/local/bin/idle_watchdog.py >>"${API_STATE_DIR}/watchdog.log" 2>&1 &
nohup python3 /usr/local/bin/bw_reporter.py >>"${API_STATE_DIR}/bw_reporter.log" 2>&1 &

if [ ! -f "${MODEL_DIR}/${MODEL_FILE}" ]; then
  touch "${API_STATE_DIR}/download_started"
  hf download \
    "${MODEL_REPO}" \
    "${MODEL_FILE}" \
    --local-dir "${MODEL_DIR}" \
    ${HF_TOKEN:+--token "$HF_TOKEN"}
else
  touch "${API_STATE_DIR}/download_started"
fi

CT=
if [ -n "${CHAT_TEMPLATE}" ] && [ "${CHAT_TEMPLATE}" != "none" ]; then
  CT="${CHAT_TEMPLATE}"
fi
if [ -n "${CLAUDE_TEMPLATE}" ] && [ "${CLAUDE_TEMPLATE}" != "none" ]; then
  CT="$("${LLAMA_CPP_DIR}/claude_template.sh" "${MODEL_DIR}/${MODEL_FILE}" "${API_STATE_DIR}/claude_chat_template.jinja" 2>>"${API_STATE_DIR}/claude_template.log")"
fi

DRAFT_BAKED="${MODEL_DIR}/Qwen3.8-27B-Uncensored-draft-Q8_0.gguf"
VISION_BAKED="${MODEL_DIR}/mmproj-Qwen3.8-27B-Uncensored-F16.gguf"
VISION_PATH="${VISION_BAKED}"
DRAFT_PATH=
if [ -n "${USE_DRAFT_MODEL}" ] && [ "${USE_DRAFT_MODEL}" != "none" ]; then
  DRAFT_PATH="${DRAFT_BAKED}"
fi

${LLAMA_CPP_DIR}/build/bin/llama-server \
  --model "${MODEL_DIR}/${MODEL_FILE}" \
  --alias "${MODEL_ALIAS}" \
  --host "${HOST}" \
  --port "${PORT}" \
  --ctx-size "${CTX_SIZE}" \
  --jinja \
  ${CT:+--chat-template-file "${CT}"} \
  --cache-prompt \
  --spec-type draft-mtp --spec-draft-n-max "${SPEC_DRAFT_N_MAX}" ${DRAFT_PATH:+--model-draft "${DRAFT_PATH}"} \
  ${VISION_PATH:+--mmproj "${VISION_PATH}"} \
  --parallel 1 \
  --cont-batching \
  --metrics \
  --temp 0.1 \
  --top-p 0.95 \
  --min-p 0.05 \
  --repeat-penalty 1.0 \
  --flash-attn on \
  ${API_KEY:+--api-key "$API_KEY"} \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --main-gpu "${GPU_ID}" \
  --n-gpu-layers "${N_GPU_LAYERS}" \
  --threads "${THREADS}" \
  --batch-size "${BATCH_SIZE}" \
  --ubatch-size "${UBATCH_SIZE}" \
  &
LLAMA_PID=$!
trap 'kill -TERM "$LLAMA_PID" 2>/dev/null; exit 0' TERM INT
wait "$LLAMA_PID"
exit $?
