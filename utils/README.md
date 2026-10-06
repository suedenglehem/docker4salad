# utils/

Operational helpers — the day-to-day scripts for managing the live Salad
groups, watching a running server, and checking the account balance. All
stdlib-only Python, run from the repo root: `python3 utils/<script>`.

| script | what it does |
|---|---|
| `manage_groups.py` | group manager: `list` / `refresh` / `start` / `stop` / `wait` / `delete` |
| `llama_stats.py` | llama-server stats (tps / queue / totals) from the host |
| `billing.py` | per-org portal credit balances (USD + live EUR) |

## manage_groups.py

Status and control for every group in the account's known orgs/projects
(`ma-casa-in-paris`, `akl-on-salad` — fixed map in `KNOWN_PROJECTS`, add new
projects there; the Salad API has no list-orgs/list-projects operation).

```
python3 utils/manage_groups.py [ACTION] [ORG] [--interval N]
```

- `list` (default) — one-shot table: status, age, uptime, instance counts,
  gateway URL; running groups also get their `ssh -p PORT root@IP` line.
- `refresh` — re-render the table every N seconds (Ctrl-C stops).
- `start` / `stop` / `delete` — numbered table → pick by # or name → confirm
  (delete requires typing the group name). **`start` bills per second;
  `delete` is irreversible** and tombstones the name (recreating it 400s
  `name_conflict` for 10+ min — use a fresh name).
- `wait` — after `start`: poll until at least one instance reports `ready`.
  The group flipping to `running` is NOT readiness — instances are still
  allocating/downloading/creating and the model pull takes minutes.

The Salad API key is loaded from `deploy/salad_api.txt` via
`salad_client.py` (repo root).

## llama_stats.py

Host entry point for `/metrics` stats from any llama-server: generation /
prompt t/s, processing / deferred / busy-slot gauges, prompt + generated
token totals, spec-decode stats. Loops every 5 s by default (Ctrl-C stops);
`--once` for a single scrape, `--interval N` to change the period, `--raw`
for the raw Prometheus body.

```
python3 utils/llama_stats.py https://<group>.salad.cloud   # Salad gateway
python3 utils/llama_stats.py http://127.0.0.1:8080 --once  # local server
```

For a `*.salad.cloud` target the `Salad-Api-Key` header is auto-loaded from
`deploy/salad_api.txt`. This file only delegates to
`docker/llama_stats.py` — the canonical parser lives there because `docker/`
is the image build context, and the same file is installed in the image
(wrapped by `stats.sh` for in-container use).

## billing.py

Per-org "Current Credit Amount" in USD plus an EUR column at the live ECB
rate (frankfurter.app; USD-only if the FX lookup fails). The public API has
no billing operation, so it logs into the portal backend with credentials
from `portal_vault.gpg` (GPG AES256, next to this script, gitignored).

```
python3 utils/billing.py                 # passphrase from $PORTAL_VAULT_PASS, or prompt
PORTAL_VAULT_PASS='...' python3 utils/billing.py
```

First run (no vault) prompts for the portal email/password and org slugs,
verifies the login against the portal, then encrypts and saves them. Every
org in the vault gets a balance line.
