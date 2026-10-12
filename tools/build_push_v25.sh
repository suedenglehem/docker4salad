#!/usr/bin/env bash
# v2.5 image cycle — build + push (4 images, tr4, 32 jobs, sequential, WARM
# layer cache — only content change vs v2.4 is the bw_reporter.py rewrite:
# the 0-Mbps/xet fix. Reporter now logs `epoch rx-bytes model-bytes` from the
# kernel RX counter (/sys/class/net/<if>/statistics/rx_bytes, summed over
# non-lo — the same counters bmon reads); MODEL_DIR growth is display-only.
# Watchdog stays v2.3 (unchanged). base COPYs the new file; ampere COPYs it
# too; generic + q6 inherit it via their FROM re-pin (performed mid-flight
# below once the new base is on the Hub).
#
# NOT done here (interactive, after the script exits):
#   * re-pin of the 5 deployer IMAGE constants (deploy/deploy_*.py)
#   * new tools/patch_images_v12.py + fleet re-PATCH 7/7 (env asserts, no starts)
# Hub digests for all four are printed at the end for that step.
#
# Every push leaves the repo full_description set (PROGRESS.md §11).
set -euo pipefail
cd /dd2/andrei/docker/on_salad

JOBS=32
BASE_TAG=cuda128-v12
GEN_TAG=cuda128-v12
Q6_TAG=cuda128-v14
AMP_TAG=cuda130-v10
OLD_BASE_PIN='boris271142/lmss:cuda128-v11@sha256:2bfbd69c5730b9787f2b91cfd496ebbfdcce469ba99fc538fbe5a66b2a305455'

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

# --- 1. base (context = docker/) ---
log "BUILD boris271142/lmss:${BASE_TAG} (32 jobs, warm cache)"
docker build -f docker/Dockerfile.multistage \
  --build-arg BUILD_JOBS=${JOBS} \
  --build-arg LMSS_BUILD_ID="lmss-${BASE_TAG} (bw_reporter v2: kernel RX counter, xet 0-Mbps fix; watchdog v2.3) 2026-10-11" \
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
  --build-arg LMSS_BUILD_ID="lmss_generic-${GEN_TAG} (inherits base ${BASE_TAG}: bw_reporter v2 kernel-RX, xet fix) 2026-10-11" \
  -t boris271142/lmss_generic:${GEN_TAG} .
docker push boris271142/lmss_generic:${GEN_TAG}
python3 -I tools/hub_describe.py lmss_generic --set >/dev/null

log "BUILD boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:${Q6_TAG}"
docker build -f docker/Dockerfile.lmss_q6_mtp_vision \
  --build-arg BUILD_JOBS=${JOBS} \
  --build-arg LMSS_BUILD_ID="lmss_q6-${Q6_TAG} (inherits base ${BASE_TAG}: bw_reporter v2 kernel-RX, xet fix) 2026-10-11" \
  -t boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:${Q6_TAG} .
docker push boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:${Q6_TAG}
python3 -I tools/hub_describe.py lmss_jonathancoletti_qwen38_q6_mtp_vision --set >/dev/null

log "BUILD boris271142/lmss_generic_ampere:${AMP_TAG}"
docker build -f docker/Dockerfile.lmss_generic_ampere \
  --build-arg BUILD_JOBS=${JOBS} \
  --build-arg LMSS_BUILD_ID="lmss_generic_ampere-${AMP_TAG} (bw_reporter v2: kernel RX counter, xet 0-Mbps fix; llamAmpere v0.4, cuda 13.0.2, watchdog v2.3) 2026-10-11" \
  -t boris271142/lmss_generic_ampere:${AMP_TAG} .
docker push boris271142/lmss_generic_ampere:${AMP_TAG}
python3 -I tools/hub_describe.py lmss_generic_ampere --set >/dev/null

log "ALL FOUR BUILT + PUSHED — Hub digests for the re-pin step:"
for pair in "lmss ${BASE_TAG}" "lmss_generic ${GEN_TAG}" \
            "lmss_jonathancoletti_qwen38_q6_mtp_vision ${Q6_TAG}" "lmss_generic_ampere ${AMP_TAG}"; do
  set -- $pair
  echo "  boris271142/$1:$2  $(hub_tag_digest "$1" "$2")"
done
log "DONE — remaining (interactive): deployer re-pin x5, tools/patch_images_v12.py, fleet re-PATCH 7/7"
