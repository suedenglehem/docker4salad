#!/usr/bin/env bash
# v2.4 image cycle — PART 1: build + push only (4 images, from scratch on tr4,
# 32 jobs, sequential). The only content change vs v2.3 is the shell-ergonomics
# block appended to /root/.bashrc in the final stage (TERM=xterm-256color +
# nv/ht/gpu/wlog/alog/ready/als/ll/la/l/h aliases) — in Dockerfile.multistage
# and Dockerfile.lmss_generic_ampere; lmss_generic + q6 inherit it via their
# FROM re-pin, which this script performs mid-flight once the new base is on
# the Hub. Watchdog stays v2.3 (unchanged).
#
# NOT done here (interactive, when the model backend is back):
#   * re-pin of the 5 deployer IMAGE constants (deploy/deploy_*.py)
#   * new tools/patch_images_v11.py + fleet re-PATCH 7/7 (env asserts, no starts)
# Hub digests for all four are printed at the end for that step.
#
# Every push leaves the repo full_description set (PROGRESS.md §11).
set -euo pipefail
cd /dd2/andrei/docker/on_salad

JOBS=32
BASE_TAG=cuda128-v11
GEN_TAG=cuda128-v11
Q6_TAG=cuda128-v13
AMP_TAG=cuda130-v9
OLD_BASE_PIN='boris271142/lmss:cuda128-v10@sha256:c363d00e5eaebb2b88bffab5a8bddd44a6afa3687e0377c78ac63d134fdd019d'

log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

hub_tag_digest() {  # $1=repo $2=tag -> prints the Hub manifest digest
python3 -I - "$1" "$2" <<'EOF'
import base64, json, os, sys, urllib.request
repo, tag = sys.argv[1], sys.argv[2]
cfg = json.load(open(os.path.expanduser("~/.docker/config.json")))
b64 = cfg["auths"]["https://index.docker.io/v1/"]["auth"]
user, _, pw = base64.b64decode(b64).decode().partition(":")
body = json.dumps({"username": user, "password": pw}).encode()
req = urllib.request.Request("https://hub.docker.com/v2/users/login", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
tok = json.load(urllib.request.urlopen(req))["token"]
req = urllib.request.Request(
    f"https://hub.docker.com/v2/repositories/boris271142/{repo}/tags/{tag}",
    headers={"Authorization": f"JWT {tok}"})
print(json.load(urllib.request.urlopen(req))["digest"])
EOF
}

# --- 1. base (context = docker/; stage 1 git-clones llama.cpp, so nothing else
#        from the context — docker/.dockerignore applies) ---
log "BUILD boris271142/lmss:${BASE_TAG} (32 jobs, from scratch)"
docker build -f docker/Dockerfile.multistage \
  --build-arg BUILD_JOBS=${JOBS} \
  --build-arg LMSS_BUILD_ID="lmss-${BASE_TAG} (bashrc shell ergonomics: TERM=xterm-256color + aliases; watchdog v2.3) 2026-10-11" \
  -t boris271142/lmss:${BASE_TAG} docker/
log "PUSH boris271142/lmss:${BASE_TAG}"
docker push boris271142/lmss:${BASE_TAG}
python3 -I tools/hub_describe.py lmss --set >/dev/null

# --- 2. re-pin the two derivative FROMs to the new base digest (FROM line
#        only; verified before any build proceeds) ---
NEW_BASE_DIGEST=$(hub_tag_digest lmss ${BASE_TAG})
[ -n "${NEW_BASE_DIGEST}" ] || { log "FATAL: could not read the new base digest from the Hub"; exit 1; }
log "new base digest: ${NEW_BASE_DIGEST}"
for f in docker/Dockerfile.lmss_generic docker/Dockerfile.lmss_q6_mtp_vision; do
  sed -i "/^FROM /s|${OLD_BASE_PIN}|boris271142/lmss:${BASE_TAG}@${NEW_BASE_DIGEST}|" "$f"
  n=$(grep -c "^FROM boris271142/lmss:${BASE_TAG}@${NEW_BASE_DIGEST}\$" "$f" || true)
  [ "$n" = "1" ] || { log "FATAL: FROM re-pin failed in $f (match count ${n})"; exit 1; }
  log "FROM re-pinned: $f"
done

# --- 3. derivatives (context = repo root; they COPY docker/*.sh etc.) ---
log "BUILD boris271142/lmss_generic:${GEN_TAG}"
docker build -f docker/Dockerfile.lmss_generic \
  --build-arg BUILD_JOBS=${JOBS} \
  --build-arg LMSS_BUILD_ID="lmss_generic-${GEN_TAG} (inherits base ${BASE_TAG} bashrc shell ergonomics) 2026-10-11" \
  -t boris271142/lmss_generic:${GEN_TAG} .
docker push boris271142/lmss_generic:${GEN_TAG}
python3 -I tools/hub_describe.py lmss_generic --set >/dev/null

log "BUILD boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:${Q6_TAG}"
docker build -f docker/Dockerfile.lmss_q6_mtp_vision \
  --build-arg BUILD_JOBS=${JOBS} \
  --build-arg LMSS_BUILD_ID="lmss_q6-${Q6_TAG} (inherits base ${BASE_TAG} bashrc shell ergonomics) 2026-10-11" \
  -t boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:${Q6_TAG} .
docker push boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:${Q6_TAG}
python3 -I tools/hub_describe.py lmss_jonathancoletti_qwen38_q6_mtp_vision --set >/dev/null

log "BUILD boris271142/lmss_generic_ampere:${AMP_TAG}"
docker build -f docker/Dockerfile.lmss_generic_ampere \
  --build-arg BUILD_JOBS=${JOBS} \
  --build-arg LMSS_BUILD_ID="lmss_generic_ampere-${AMP_TAG} (bashrc shell ergonomics baked; llamAmpere v0.4, cuda 13.0.2, watchdog v2.3) 2026-10-11" \
  -t boris271142/lmss_generic_ampere:${AMP_TAG} .
docker push boris271142/lmss_generic_ampere:${AMP_TAG}
python3 -I tools/hub_describe.py lmss_generic_ampere --set >/dev/null

log "ALL FOUR BUILT + PUSHED — Hub digests for the re-pin step:"
for pair in "lmss ${BASE_TAG}" "lmss_generic ${GEN_TAG}" \
            "lmss_jonathancoletti_qwen38_q6_mtp_vision ${Q6_TAG}" "lmss_generic_ampere ${AMP_TAG}"; do
  set -- $pair
  echo "  boris271142/$1:$2  $(hub_tag_digest "$1" "$2")"
done
log "DONE — remaining (interactive): deployer re-pin x5, tools/patch_images_v11.py, fleet re-PATCH 7/7"
