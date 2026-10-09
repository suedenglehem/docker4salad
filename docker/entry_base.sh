#!/bin/bash
# Entrypoint for the lmss base image (vanilla llama.cpp, q8_0 KV cache).
# Starts the helper daemons (api_app, two socat port forwarders, and the
# idle/heartbeat watchdog) in the background, downloads the model (hf) and the
# optional draft / vision (wget2) on first start, then runs llama-server as a
# TRAPPED CHILD of this PID-1 bash (not exec'd) so the watchdog's SIGTERM lands.
# With the group's restart_policy=never that self-exit leaves the Salad group
# STOPPED (free). A draft -> draft-mtp; no draft -> ngram-mod. CWD is
# /opt/llama.cpp (the image WORKDIR); helper paths are CWD-relative.

set -euo pipefail

mkdir -p "${MODEL_DIR}" "${DRAFT_MODEL_DIR}" "${VISION_MODEL_DIR}" "${API_STATE_DIR}"

nohup python3 run_api.py >>"${API_STATE_DIR}/api.log" 2>&1 &
nohup socat TCP6-LISTEN:8888,fork,reuseaddr TCP4:127.0.0.1:8080 >>"${API_STATE_DIR}/socat.log" 2>&1 &
nohup socat TCP6-LISTEN:8889,fork,reuseaddr TCP4:127.0.0.1:9999 >>"${API_STATE_DIR}/socat2.log" 2>&1 &
nohup python3 /usr/local/bin/idle_watchdog.py >>"${API_STATE_DIR}/watchdog.log" 2>&1 &

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

DRAFT_PATH=
if [ -n "${DRAFT_MODEL_URL}" ] && [ "${DRAFT_MODEL_URL}" != "none" ]; then
  DRAFT_PATH="${DRAFT_MODEL_DIR}/${DRAFT_MODEL_URL##*/}"
  if [ ! -f "${DRAFT_PATH}" ]; then
    wget2 -c -O "${DRAFT_PATH}" "${DRAFT_MODEL_URL}"
  fi
fi

VISION_PATH=
if [ -n "${VISION_MODEL_URL}" ] && [ "${VISION_MODEL_URL}" != "none" ]; then
  VISION_PATH="${VISION_MODEL_DIR}/${VISION_MODEL_URL##*/}"
  if [ ! -f "${VISION_PATH}" ]; then
    wget2 -c -O "${VISION_PATH}" "${VISION_MODEL_URL}"
  fi
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
  ${DRAFT_PATH:+--spec-type draft-mtp --spec-draft-n-max "${SPEC_DRAFT_N_MAX}" --model-draft "${DRAFT_PATH}"} \
  ${DRAFT_PATH:---spec-type ngram-mod --spec-ngram-mod-n-match 24 --spec-ngram-mod-n-min 48 --spec-ngram-mod-n-max 64} \
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
