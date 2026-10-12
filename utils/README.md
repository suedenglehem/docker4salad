# utils/

Operational helpers — the day-to-day scripts for managing the live Salad
groups, watching a running server, and checking the account balance. All
stdlib-only Python, run from the repo root: `python3 utils/<script>`.

| script | what it does |
|---|---|
| `sl_admin.py` | the fleet admin: `list` / `refresh` / `start` / `stop` / `wait` / `stats` / `delete` / `create-org` / `create-project` / `deploy` |
| `llama_stats.py` | llama-server stats (tps / queue / totals) from the host |
| `billing.py` | per-org portal credit balances (USD + live EUR) |

## sl_admin.py

Status and control for every group in the account's known orgs/projects. The
Salad API has no list-orgs/list-projects operation, so the tool walks a map:
`KNOWN_PROJECTS` in the script (the zero-config default: `ma-casa-in-paris`,
`akl-on-salad`) overlaid by `utils/fleet.json` — which `create-org` /
`create-project` write, so a new org/project is data, not a code edit.

```
python3 utils/sl_admin.py [ACTION] [ORG] [--interval N]
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
- `stats` — llama-server stats (same tps / queue / totals view as
  `llama_stats.py`), but **only when the group is fully ready**: status
  `running`, all `replicas` instances present, and every one of them
  `ready=true` — stricter than `wait`'s "at least one ready". Single check,
  no polling: if it isn't, it prints the group's state and exits 1 without
  scraping (`wait` to block until ready, then re-run). When it is, it hands
  off to the canonical `docker/llama_stats.py` against the gateway URL with
  `--interval N` (default 5 s, Ctrl-C stops).
- `deploy` — pick a **DOCKER TYPE** from the profiles our images support
  (`q6` = the Claude Code backend, `generic` = any GGUF on vanilla lmss,
  `ampere` = the llamAmpere fork on sm_86 cards, `9b` / `qwen9b` = plain 9B on
  base lmss) and run its deployer against an org/project/group. Everything is
  prompted when not given on the command line (`--type`, `--org`,
  `--project`, `--group`), and flags after `--` go to the deployer verbatim
  (`--ctx-size`, `--gpu`, `--model-repo`, `--idle-shutdown`, …).
  **Zero cost by default** — the deployer runs with `--no-start` (CREATE and
  PATCH are free, only START bills). `--start` is the paid opt-in and asks you
  to type `y`; the 9B deployers create the group STOPPED and never start, so
  they bill nothing either way.
- `create-org` / `create-project` — the honest version of "create an
  organization". Live-probed 2026-10-13 (`tools/probe_org_create.py`): the
  public API has NO create-org/create-project and NO list-orgs/list-projects —
  every one of those paths is 404, and orgs/projects exist only in the web UI.
  So these actions (1) ATTEMPT the portal backend (what the web UI calls, via
  the `billing.py` vault — undocumented and unverified, so a 404 there is the
  expected answer, not a bug; `--no-portal` skips it) and (2) REGISTER the
  org/project in `utils/fleet.json` so the rest of the tool sees it.
  Registration always works; after creating in the web UI, re-run with
  `--register-only`. Names are validated against Salad's own rule
  (`^[a-z][a-z0-9-]{0,61}[a-z0-9]$`, 2–63 chars).

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
