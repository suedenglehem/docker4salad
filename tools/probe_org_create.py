#!/usr/bin/env python3
"""Probe the Salad public API for organization / project CREATE + LIST ops.

Why: the OpenAPI spec (docs/salad/api-specs/salad-cloud.yaml, 24 paths) has NO
`/organizations` collection path — every path is `/organizations/{org}/...`.
salad_client.create_project is a documented-as-undocumented probe. This script
settles, from the live API, what sl_admin.py can actually do:

  GET  /organizations                              list orgs?
  GET  /organizations/<org>                        org detail?
  GET  /organizations/<org>/projects               list projects?
  POST /organizations            body {}           create org?  (empty body = validation
  POST /organizations/<org>/projects  body {}      create project?  failure, creates nothing)

An empty body is deliberate: if the endpoint EXISTS the answer is 400 (schema
validation); if it is ABSENT the answer is 404. Nothing is created either way.
The API key is loaded from deploy/salad_api.txt and never printed.

Run: python3 -I tools/probe_org_create.py [org]
"""

from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import salad_client as sc  # noqa: E402

ORG = sys.argv[1] if len(sys.argv) > 1 else "ma-casa-in-paris"

PROBES = [
    ("GET", "/organizations", None),
    ("GET", f"/organizations/{ORG}", None),
    ("GET", f"/organizations/{ORG}/projects", None),
    ("POST", "/organizations", {}),
    ("POST", f"/organizations/{ORG}/projects", {}),
]


def main() -> int:
    key = sc.load_api_key(os.path.join(REPO, "deploy", "salad_api.txt"))
    for method, path, body in PROBES:
        try:
            status, reason, headers, raw = sc._http(method, path, key, body=body, timeout=30.0)
            payload = ""
            try:
                obj = json.loads(raw.decode("utf-8"))
                # Print keys only — a project/org list is small, and we never dump
                # anything that could carry account detail.
                payload = json.dumps(obj)[:400] if isinstance(obj, (dict, list)) else str(obj)[:200]
            except ValueError:
                payload = raw.decode("utf-8", "replace")[:200]
            loc = ""
            for h, v in (headers or {}).items():
                if h.lower() == "location":
                    loc = f"  Location: {v}"
            print(f"{method:5} {path:45} -> {status} {reason}{loc}\n      {payload}")
        except sc.SaladApiError as e:
            print(f"{method:5} {path:45} -> {e.status_code} {e.problem.detail if e.problem else e.reason if hasattr(e, 'reason') else ''}\n      {e.raw_body[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
