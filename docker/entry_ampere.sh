#!/bin/bash
# Entrypoint for the lmss generic-ampere image (llamAmpere fork: turbo5/turbo4
# KV cache). Starts the helper daemons (api_app, two socat port forwarders, the
# idle/heartbeat watchdog, and the HF-download bandwidth reporter) in the
# background, downloads the model / draft / vision on first start, then runs
# llama-server as a TRAPPED CHILD of this
# PID-1 bash (not exec'd) so the watchdog's SIGTERM lands. With the group's
# restart_policy=never that self-exit leaves the Salad group STOPPED (free).
# CWD is /opt/llama.cpp (the image WORKDIR); helper paths are CWD-relative.

set -euo pipefail

mkdir -p "${MODEL_DIR}" "${API_STATE_DIR}"

nohup python3 run_api.py >>"${API_STATE_DIR}/api.log" 2>&1 &
nohup socat TCP6-LISTEN:8888,fork,reuseaddr TCP4:127.0.0.1:8080 >>"${API_STATE_DIR}/socat.log" 2>&1 &
nohup socat TCP6-LISTEN:8889,fork,reuseaddr TCP4:127.0.0.1:9999 >>"${API_STATE_DIR}/socat2.log" 2>&1 &
nohup python3 /usr/local/bin/idle_watchdog.py >>"${API_STATE_DIR}/watchdog.log" 2>&1 &
nohup python3 /usr/local/bin/bw_reporter.py >>"${API_STATE_DIR}/bw_reporter.log" 2>&1 &

# resolve_model_ref REF -> sets REPO_REF/FILE_REF, returns 1 for empty/none.
#   hf://user/repo/path/file.gguf -> REPO_REF=user/repo  FILE_REF=path/file.gguf
#   anything else (a bare file)    -> REPO_REF=$MODEL_REPO  FILE_REF=<the ref>
resolve_model_ref() {
  ref="${1:-}"; REPO_REF=""; FILE_REF=""
  case "$ref" in
    ""|none) return 1 ;;
    hf://*)
      rest="${ref#hf://}"
      REPO_REF="${rest%%/*}"
      rest="${rest#*/}"
      REPO_REF="${REPO_REF}/${rest%%/*}"
      FILE_REF="${rest#*/}"
      ;;
    *)
      REPO_REF="${MODEL_REPO}"; FILE_REF="$ref"
      ;;
  esac
}

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

DRAFT_PATH=
if resolve_model_ref "${DRAFT_MODEL}"; then
  DRAFT_PATH="${MODEL_DIR}/${FILE_REF}"
  if [ ! -f "${DRAFT_PATH}" ]; then
    hf download "${REPO_REF}" "${FILE_REF}" --local-dir "${MODEL_DIR}" ${HF_TOKEN:+--token "$HF_TOKEN"}
  fi
fi

VISION_PATH=
if resolve_model_ref "${VISION_MODEL}"; then
  VISION_PATH="${MODEL_DIR}/${FILE_REF}"
  if [ ! -f "${VISION_PATH}" ]; then
    hf download "${REPO_REF}" "${FILE_REF}" --local-dir "${MODEL_DIR}" ${HF_TOKEN:+--token "$HF_TOKEN"}
  fi
fi

CT=
if [ -n "${CHAT_TEMPLATE}" ] && [ "${CHAT_TEMPLATE}" != "none" ]; then
  CT="${CHAT_TEMPLATE}"
fi
if [ -n "${CLAUDE_TEMPLATE}" ] && [ "${CLAUDE_TEMPLATE}" != "none" ]; then
  CT="$("${LLAMA_CPP_DIR}/claude_template.sh" "${MODEL_DIR}/${MODEL_FILE}" "${API_STATE_DIR}/claude_chat_template.jinja" 2>>"${API_STATE_DIR}/claude_template.log")"
fi

SPEC=
case "${SPEC_TYPE}" in
  draft-mtp)
    SPEC="--spec-type draft-mtp --spec-draft-n-max ${SPEC_DRAFT_N_MAX}"
    if [ -n "${DRAFT_PATH}" ]; then SPEC="${SPEC} --model-draft ${DRAFT_PATH}"; fi
    ;;
  ngram-mod)
    SPEC="--spec-type ngram-mod --spec-ngram-mod-n-match 24 --spec-ngram-mod-n-min 48 --spec-ngram-mod-n-max 64"
    ;;
  none)
    SPEC=
    ;;
  *)
    echo "llama-image: unknown SPEC_TYPE '${SPEC_TYPE}' (want draft-mtp|ngram-mod|none)" >&2
    exit 1
    ;;
esac

EXTRA_ARGS_ARR=()
if [ -n "${EXTRA_ARGS}" ] && [ "${EXTRA_ARGS}" != "none" ]; then
  read -r -a EXTRA_ARGS_ARR <<< "${EXTRA_ARGS}"
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
  ${SPEC} \
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
  --cache-type-k turbo5 \
  --cache-type-v turbo4 \
  --kv-unified \
  --fit off \
  --cache-ram 4096 \
  --main-gpu "${GPU_ID}" \
  --n-gpu-layers "${N_GPU_LAYERS}" \
  --threads "${THREADS}" \
  --batch-size "${BATCH_SIZE}" \
  --ubatch-size "${UBATCH_SIZE}" \
  ${EXTRA_ARGS_ARR[@]+"${EXTRA_ARGS_ARR[@]}"} \
  &
LLAMA_PID=$!
trap 'kill -TERM "$LLAMA_PID" 2>/dev/null; exit 0' TERM INT
wait "$LLAMA_PID"
exit $?
