#!/usr/bin/env python3
"""Direct fleet PATCH for qwen38-9b (its deployer is CREATE-ONLY).

GETs the live env, sets HEARTBEAT_TIMEOUT=300 + SSH_GUARD=1, PATCHes the
full env wholesale (image untouched — the group is already on base v8).
Secrets are never printed.
"""
import sys

sys.path.insert(0, ".")
import salad_client as sc  # noqa: E402

ORG, PROJ, NAME = "ma-casa-in-paris", "qwen38-27b", "qwen38-9b"
key = open("deploy/salad_api.txt", encoding="utf-8").read().strip()

g = sc.get_container_group(ORG, PROJ, NAME, api_key=key).raw
env = dict((g.get("container") or {}).get("environment_variables") or {})
if not env:
    sys.exit("refusing: live env is empty (transitional GET) — not trusting it")

before = {"HEARTBEAT_TIMEOUT": env.get("HEARTBEAT_TIMEOUT"), "SSH_GUARD": env.get("SSH_GUARD")}
env["HEARTBEAT_TIMEOUT"] = "300"
env["SSH_GUARD"] = "1"

req = sc.UpdateContainerGroupRequest(environment_variables=env)
res = sc.update_container_group(ORG, PROJ, NAME, req, api_key=key)
print(f"PATCH status: {res.status_code} {res.reason_phrase}")
print(f"HEARTBEAT_TIMEOUT {before['HEARTBEAT_TIMEOUT']} -> 300, SSH_GUARD {before['SSH_GUARD']} -> 1")
print(f"env keys sent: {len(env)} (image untouched)")
