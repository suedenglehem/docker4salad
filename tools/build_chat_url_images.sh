#!/usr/bin/env bash
# CHAT-TEMPLATE-URL image cycle — BUILD ONLY (no push), on tr4, 24 jobs.
#
# Run ON tr4:  bash tools/build_chat_url_images.sh 2>&1 | tee /tmp/build_chat_url.log
#
# Four images, sequential, warm layer cache (the builder stages' compile layers
# are cached from the v2.5 cycle, so only the COPY/ENV layers rebuild):
#   base     boris271142/lmss:cuda128-v13                      (context = docker/)
#   generic  boris271142/lmss_generic:cuda128-v13              (context = repo root)
#   q6       ...qwen38_q6_mtp_vision:cuda128-v15               (context = repo root)
#   ampere   boris271142/lmss_generic_ampere:cuda130-v11       (context = repo root;
#                                                               needs llama.cpp/ v0.4)
# The derivatives keep their existing `FROM boris271142/lmss:cuda128-v12@sha256:0586…`
# pin: each derivative COPYs its OWN entry_*.sh + chat_template_url.sh, so the new
# helper ships in all four without re-pinning the FROM.
#
# PUSH IS NOT DONE HERE (user's call). After a push: PATCH the repo full_description
# (tools/hub_describe.py <repo> --set) and re-pin the deployer IMAGE constants.
set -euo pipefail

JOBS=24
ROOT=/dd2/andrei/docker/on_salad
log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

verify() {  # $1=image  -> prints the baked build id (the version.sh discriminator)
  docker run --rm --entrypoint bash "$1" -c 'cat /etc/llama-image-build' 2>&1 | tail -1
}
has_helper() {  # $1=image -> confirm the helper shipped + the entry script reads the URL
  docker run --rm --entrypoint bash "$1" -c \
    'ls -l /opt/llama.cpp/chat_template_url.sh; grep -c CHAT_TEMPLATE_URL /opt/llama.cpp/entry.sh' 2>&1
}

log "BUILD boris271142/lmss:cuda128-v13 (context docker/, ${JOBS} jobs)"
cd "${ROOT}/docker"
docker build -f Dockerfile.multistage \
  --build-arg BUILD_JOBS=${JOBS} \
  --build-arg LMSS_BUILD_ID="lmss-cuda128-v13 (chat_template_url: serve a CUSTOM chat template from CHAT_TEMPLATE_URL, validated; + bw_reporter v2 kernel RX + watchdog v2.3 /.ssh guard) 2026-10-12" \
  -t boris271142/lmss:cuda128-v13 .
log "  build id: $(verify boris271142/lmss:cuda128-v13)"
log "  helper:   $(has_helper boris271142/lmss:cuda128-v13 | tr '\n' ' ')"

log "BUILD boris271142/lmss_generic:cuda128-v13 (context repo root)"
cd "${ROOT}"
docker build -f docker/Dockerfile.lmss_generic \
  --build-arg BUILD_JOBS=${JOBS} \
  -t boris271142/lmss_generic:cuda128-v13 .
log "  build id: $(verify boris271142/lmss_generic:cuda128-v13)"
log "  helper:   $(has_helper boris271142/lmss_generic:cuda128-v13 | tr '\n' ' ')"

log "BUILD boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:cuda128-v15"
docker build -f docker/Dockerfile.lmss_q6_mtp_vision \
  --build-arg BUILD_JOBS=${JOBS} \
  -t boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:cuda128-v15 .
log "  build id: $(verify boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:cuda128-v15)"
log "  helper:   $(has_helper boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:cuda128-v15 | tr '\n' ' ')"

log "BUILD boris271142/lmss_generic_ampere:cuda130-v11 (fork source = llama.cpp/ @v0.4)"
docker build -f docker/Dockerfile.lmss_generic_ampere \
  --build-arg BUILD_JOBS=${JOBS} \
  -t boris271142/lmss_generic_ampere:cuda130-v11 .
log "  build id: $(verify boris271142/lmss_generic_ampere:cuda130-v11)"
log "  helper:   $(has_helper boris271142/lmss_generic_ampere:cuda130-v11 | tr '\n' ' ')"

log "ALL FOUR BUILT (no push). Local ids:"
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' | grep -E 'cuda128-v13|cuda128-v15|cuda130-v11'
log "NEXT (user): docker push each + tools/hub_describe.py <repo> --set + re-pin deployer IMAGE constants"
