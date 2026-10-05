#!/bin/sh
# Image/build identity for a running instance. Run it from an SSH session:
#   version.sh
#
# Purpose: in seconds, tell you WHICH build is actually running on a Salad
# worker (the worker image cache is keyed by repo name, not by digest — a
# worker can serve an older image under the same repo name), which env vars
# the Salad group sets over the baked defaults, and what is being downloaded
# right now.
set -u

echo "== build id (baked in at image build time) =="
if [ -r /etc/llama-image-build ]; then
  cat /etc/llama-image-build
else
  echo "(no /etc/llama-image-build -> image predates version.sh)"
fi
echo

echo "== env vars the container reads: effective vs baked default =="
# Every var the CMD (or the run_api.py status API) reads, with the default
# baked into THIS image. A * marks values that differ from the baked
# default, i.e. were set by the Salad group env (a group env PATCH replaces
# the env wholesale — removing a key reverts it to the baked value).
# Baked defaults are kept in sync with docker/Dockerfile.lmss_q6_mtp_vision:
# the boris271142/lmss:cuda128-v3 base ENV, plus USE_DRAFT_MODEL (v4) and
# CLAUDE_TEMPLATE (v5).
show_var() {
  # $1 = var name, $2 = baked default (literal __nobaked__ = the image does
  # not bake it at all; empty string = baked but empty)
  name="$1"
  def="${2-__nobaked__}"
  if val=$(printenv "$name" 2>/dev/null); then
    present=1
  else
    val=""
    present=0
  fi
  raw="$val"
  case "$name" in
    HF_TOKEN|API_KEY)
      # never print secrets: presence only
      if [ -n "$val" ]; then val="<set, redacted>"; fi
      ;;
  esac
  if [ "$present" = 1 ]; then
    if [ -z "$val" ]; then val="'' (empty)"; fi
    if [ "$def" = "__nobaked__" ]; then
      star="* group-set (not baked)"
    elif [ "$raw" != "$def" ]; then
      star="* group-set"
    else
      star=""
    fi
  else
    if [ "$def" = "__nobaked__" ]; then star="(optional, unset)"; else star=""; fi
  fi
  case "$def" in
    __nobaked__) def="(not baked)" ;;
    "")          def="'' (empty)" ;;
  esac
  printf '  %-16s  %-44s  %-44s  %s\n' "$name" "$val" "$def" "$star"
}
printf '  %-16s  %-44s  %-44s  %s\n' "NAME" "EFFECTIVE (what the container sees)" "BAKED IMAGE DEFAULT" "differs from baked?"
show_var MODEL_DIR        /models
show_var MODEL_FILE       Qwen3.8-27B-Uncensored-noMTP-Q4_K_M.gguf
show_var MODEL_REPO       JonathanColetti/Qwen3.8-27B-Uncensored-GGUF
show_var MODEL_ALIAS      qwen38-27b
show_var CTX_SIZE         131072
show_var CHAT_TEMPLATE    none
show_var CLAUDE_TEMPLATE  none
show_var USE_DRAFT_MODEL  none
show_var SPEC_DRAFT_N_MAX 5
show_var HF_TOKEN         __nobaked__
show_var API_KEY          ""
show_var GPU_ID           0
show_var N_GPU_LAYERS     99
show_var THREADS          8
show_var BATCH_SIZE       256
show_var UBATCH_SIZE      256
show_var HOST             0.0.0.0
show_var PORT             8080
show_var API_HOST         0.0.0.0
show_var API_PORT         9999
show_var API_STATE_DIR    /tmp/llama-api
show_var LLAMA_CPP_DIR    /opt/llama.cpp
echo "  (NAME is set by the deployer but nothing in the image reads it; the"
echo "   inherited DRAFT_MODEL_URL / DRAFT_MODEL_DIR / VISION_MODEL_URL /"
echo "   VISION_MODEL_DIR are no longer read by this CMD — draft and vision"
echo "   are baked into the image and the draft is gated on USE_DRAFT_MODEL)"
echo

echo "== CMD fingerprint (PID 1 — which sentinel generation) =="
CMDLINE=$(tr '\0' ' ' < /proc/1/cmdline)
GUARDS=$(printf '%s' "$CMDLINE" | grep -o '!= "none"' | wc -l)
echo "sentinel guards (!= \"none\") in PID1 cmdline: $GUARDS (informational)"
# Marker-based classification: each generation's CMD contains env var names
# the previous ones never did, so their presence identifies the build.
# (The old '>= 3 guards' count misclassified v4, which has exactly 2.)
if printf '%s' "$CMDLINE" | grep -q 'DRAFT_MODEL_URL'; then
  echo "  -> V3 (wget2) image: draft/vision downloaded at runtime via"
  echo "     DRAFT_MODEL_URL / VISION_MODEL_URL"
elif printf '%s' "$CMDLINE" | grep -q 'CLAUDE_TEMPLATE'; then
  echo "  -> V5 image: Claude template gated on CLAUDE_TEMPLATE (dumps the"
  echo "     chat template from the downloaded gguf + one-line Claude patch),"
  echo "     draft gated on USE_DRAFT_MODEL, static template on CHAT_TEMPLATE"
elif printf '%s' "$CMDLINE" | grep -q 'USE_DRAFT_MODEL'; then
  echo "  -> SENTINEL image (v4): draft/vision/template OFF unless set to a"
  echo "     real value (CHAT_TEMPLATE takes a static file path)"
else
  echo "  -> PRE-SENTINEL image: baked-in defaults are live"
fi
echo "cmdline sha256: $(printf '%s' "$CMDLINE" | sha256sum | cut -d' ' -f1)"
echo

echo "== downloads / model files =="
ls -la /models/ 2>/dev/null || echo "(no /models)"
ps -eo pid,etime,args | grep -E 'wget2|wget |hf download|llama-server' | grep -v grep | cut -c1-220
echo

echo "== container =="
echo "hostname: $(hostname)   PID1 uptime: $(ps -p 1 -o etime=)"
