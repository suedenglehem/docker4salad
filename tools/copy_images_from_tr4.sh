#!/usr/bin/env bash
# Copy the v2.5 image set from tr4 (b760one) into this box's docker store.
# Streaming: ssh tr4 'docker save REF | pigz' | pigz -d | docker load
# No intermediate tarballs. Sequential (one image at a time) so a break costs
# one image, not the whole set. Verifies each loaded image ID against the
# expected Hub digest prefix (single-arch amd64: local ID == manifest digest).
set -uo pipefail

TR4=andrei@tr4
LOG=/dd2/andrei/docker/on_salad/tools/copy_images_from_tr4.log
: > "$LOG"

# ref -> expected image-ID prefix (the Hub manifest digest, §4)
REFS=(
  "boris271142/lmss:cuda128-v12                              0586ba35fb43"
  "boris271142/lmss_generic:cuda128-v12                      2fc912ae0050"
  "boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:cuda128-v14 a779732c02b2"
  "boris271142/lmss_generic_ampere:cuda130-v10               28417137144d"
)

fail=0
for entry in "${REFS[@]}"; do
  ref=$(awk '{print $1}' <<<"$entry")
  want=$(awk '{print $2}' <<<"$entry")
  echo "=== $(date -u +%H:%M:%S)Z START $ref (want ID $want) ===" | tee -a "$LOG"
  t0=$(date +%s)
  # -p 8 on tr4: parallel gzip, leaves headroom for its own work.
  if ssh -o ConnectTimeout=15 "$TR4" "docker save $ref | pigz -p 8" | pigz -dc | docker load 2>>"$LOG"; then
    # {{.ID}} prints the 12-char SHORT id (no "sha256:" prefix) — compare it whole.
    got=$(docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}}' | awk -v r="$ref" '$1==r {print $2}')
    dt=$(( $(date +%s) - t0 ))
    if [[ "$got" == "$want" ]]; then
      echo "=== $(date -u +%H:%M:%S)Z OK   $ref  id=$got  ${dt}s ===" | tee -a "$LOG"
    else
      echo "=== $(date -u +%H:%M:%S)Z MISMATCH $ref  got=$got want=$want  ${dt}s ===" | tee -a "$LOG"
      fail=1
    fi
  else
    echo "=== $(date -u +%H:%M:%S)Z FAIL  $ref (pipe/docker load non-zero) ===" | tee -a "$LOG"
    fail=1
  fi
done

echo "=== $(date -u +%H:%M:%S)Z DONE fail=$fail ===" | tee -a "$LOG"
docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}' \
  | grep -E "lmss:cuda128-v12|lmss_generic:cuda128-v12|q6_mtp_vision:cuda128-v14|ampere:cuda130-v10" | tee -a "$LOG"
exit $fail
