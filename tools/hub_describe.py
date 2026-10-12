"""Set a Docker Hub repo's 'repository overview' (full_description).

Standing rule (PROGRESS.md §11): every push must leave the repo overview set.
The field is `full_description` (the 100-byte `description` blurb 400s on long
text). Credentials come from ~/.docker/config.json (auths) — never printed.

Usage:
  python3 -I tools/hub_describe.py <repo>            # show current overview
  python3 -I tools/hub_describe.py <repo> --set      # set the standard overview
"""
import base64
import json
import os
import sys
import urllib.request

USER = "boris271142"
README_URL = "https://github.com/suedenglehem/docker4salad/blob/main/docker/README.md"

STANDARD = {
    "lmss": "llama.cpp CUDA 12.8 runtime + Anthropic-only /v1/messages proxy + idle/heartbeat watchdog (v2.3: /.ssh presence guard with 20-min max-hold cap) + bw_reporter + llama-stats + CHAT_TEMPLATE_URL (custom chat template downloaded from a URL, validated). Base image of the family.\n" + README_URL,
    "lmss_generic": "Generic any-model llama.cpp image (CUDA 12.8): main/draft/vision models hf-downloaded at runtime; DRAFT_MODEL/VISION_MODEL/SPEC_TYPE/CLAUDE_TEMPLATE/CHAT_TEMPLATE_URL/EXTRA_ARGS control surface; watchdog v2.3 (/.ssh guard, 20-min max-hold cap).\n" + README_URL,
    "lmss_jonathancoletti_qwen38_q6_mtp_vision": "Baked Qwen3.8-27B (draft + vision in image, main model hf-downloaded at runtime), MTP speculation, Claude Code template patch, CHAT_TEMPLATE_URL (custom template from a URL), watchdog v2.3 (/.ssh guard, 20-min max-hold cap).\n" + README_URL,
    "lmss_generic_ampere": "llamAmpere turbo-KV fork (turbo5/turbo4 KV cache, CUDA 13.0.2, sm_86/sm_89) for ATX on 24 GB cards; CHAT_TEMPLATE_URL (custom template from a URL); watchdog v2.3 (/.ssh guard, 20-min max-hold cap).\n" + README_URL,
}


def creds():
    cfg = json.load(open(os.path.expanduser("~/.docker/config.json")))
    b64 = cfg["auths"]["https://index.docker.io/v1/"]["auth"]
    user, _, pw = base64.b64decode(b64).decode().partition(":")
    return user, pw


def token():
    user, pw = creds()
    body = json.dumps({"username": user, "password": pw}).encode()
    req = urllib.request.Request(
        "https://hub.docker.com/v2/users/login", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as r:
        return json.load(r)["token"]


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    repo = sys.argv[1]
    tok = token()
    url = f"https://hub.docker.com/v2/repositories/{USER}/{repo}"
    if "--set" in sys.argv:
        if repo not in STANDARD:
            sys.exit(f"no standard overview for {repo!r}; known: {sorted(STANDARD)}")
        body = json.dumps({"full_description": STANDARD[repo]}).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"JWT {tok}"}, method="PATCH")
        with urllib.request.urlopen(req) as r:
            r.read()
    req = urllib.request.Request(url, headers={"Authorization": f"JWT {tok}"})
    with urllib.request.urlopen(req) as r:
        cur = json.load(r).get("full_description", "")
    print(f"--- {repo} full_description ({len(cur)} bytes) ---")
    print(cur)


if __name__ == "__main__":
    main()
