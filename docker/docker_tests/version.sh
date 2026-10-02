#!/bin/sh
# Image/build identity for a running instance. Run it from an SSH session:
#   version.sh
#
# Purpose: in seconds, tell you WHICH build is actually running on a Salad
# worker (the worker image cache is keyed by repo name, not by digest — a
# worker can serve an older image under the same repo name), whether the
# sentinel CMD is live, and what is being downloaded right now.
set -u

echo "== build id (baked in at image build time) =="
if [ -r /etc/llama-image-build ]; then
  cat /etc/llama-image-build
else
  echo "(no /etc/llama-image-build -> image predates version.sh)"
fi
echo

echo "== effective feature env (group env overrides the baked-in image defaults) =="
env | grep -E '^(MODEL_REPO|MODEL_FILE|MODEL_ALIAS|DRAFT_MODEL_URL|VISION_MODEL_URL|CHAT_TEMPLATE)=' | sort
echo

echo "== CMD fingerprint (PID 1) =="
CMDLINE=$(tr '\0' ' ' < /proc/1/cmdline)
GUARDS=$(printf '%s' "$CMDLINE" | grep -o '!= "none"' | wc -l)
echo "sentinel guards (!= \"none\") in PID1 cmdline: $GUARDS"
if [ "$GUARDS" -ge 3 ]; then
  echo "  -> SENTINEL image: draft/vision/template are OFF unless set to a real value"
else
  echo "  -> PRE-SENTINEL image: baked-in DRAFT_*/VISION_* defaults are live (wget2 downloads them)"
fi
echo "cmdline sha256: $(printf '%s' "$CMDLINE" | sha256sum | cut -d' ' -f1)"
echo

echo "== downloads / model files =="
ls -la /models/ 2>/dev/null || echo "(no /models)"
ps -eo pid,etime,args | grep -E 'wget2|wget |hf download|llama-server' | grep -v grep | cut -c1-220
echo

echo "== container =="
echo "hostname: $(hostname)   PID1 uptime: $(ps -p 1 -o etime=)"
