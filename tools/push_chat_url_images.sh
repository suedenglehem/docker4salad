#!/usr/bin/env bash
# CHAT-TEMPLATE-URL cycle — PUSH the four built images (run ON tr4) + leave every
# repo's overview set (standing rule: PATCH full_description after every push).
#
# Run ON tr4:  bash tools/push_chat_url_images.sh 2>&1 | tee /tmp/push_chat_url.log
#
# Prints each Hub manifest digest at the end — those digests are what the deployer
# IMAGE constants get re-pinned to (pinned refs are the airtight lever: the worker
# image cache is repo-name keyed and tags never re-pull).
set -euo pipefail
cd /dd2/andrei/docker/on_salad
log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

hub_tag_digest() {  # $1=repo $2=tag -> Hub manifest digest
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

push_one() {  # $1=repo $2=tag
  log "PUSH boris271142/$1:$2"
  docker push "boris271142/$1:$2"
  log "  overview PATCH: $(python3 -I tools/hub_describe.py "$1" --set | head -1)"
}

push_one lmss cuda128-v13
push_one lmss_generic cuda128-v13
push_one lmss_jonathancoletti_qwen38_q6_mtp_vision cuda128-v15
push_one lmss_generic_ampere cuda130-v11

log "Hub manifest digests (re-pin the deployer IMAGE constants to these):"
echo "  lmss:cuda128-v13                                        $(hub_tag_digest lmss cuda128-v13)"
echo "  lmss_generic:cuda128-v13                                $(hub_tag_digest lmss_generic cuda128-v13)"
echo "  lmss_jonathancoletti_qwen38_q6_mtp_vision:cuda128-v15   $(hub_tag_digest lmss_jonathancoletti_qwen38_q6_mtp_vision cuda128-v15)"
echo "  lmss_generic_ampere:cuda130-v11                         $(hub_tag_digest lmss_generic_ampere cuda130-v11)"
log "PUSH DONE. Local ids:"
docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' | grep -E 'cuda128-v13|cuda128-v15|cuda130-v11'
