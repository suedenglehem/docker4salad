# utils/

Operational helpers — the day-to-day scripts for managing the live Salad
groups, watching a running server, and checking the account balance. All
stdlib-only Python, run from the repo root: `python3 utils/<script>`.

| script | what it does |
|---|---|
| `sl_admin.py` | the fleet admin: `list` / `refresh` / `start` / `stop` / `wait` / `stats` / `delete` / `ssh` / `create-org` / `create-project` / `deploy`, plus the card & priority flags `--gpu` / `--prio` / `--query` / `--y` and the `ORG[/PROJECT] GROUP` positionals |
| `llama_stats.py` | llama-server stats (tps / queue / totals) from the host |
| `billing.py` | per-org portal credit balances (USD + live EUR) |

## sl_admin.py

Status and control for every group in the account's known orgs/projects. The
Salad API has no list-orgs/list-projects operation, so the tool walks a map:
`KNOWN_PROJECTS` in the script (the zero-config default: `ma-casa-in-paris`,
`akl-on-salad`) overlaid by `utils/fleet.json` — which `create-org` /
`create-project` write, so a new org/project is data, not a code edit.

```
python3 utils/sl_admin.py [ACTION] [ORG[/PROJECT]] [GROUP] [--interval N]
```

The 2nd positional may be `ORG/PROJECT` (e.g. `ma-casa-in-paris/llm`); the 3rd
positional is the **GROUP** name, which for `start`/`stop`/`wait`/`stats`/
`delete`/`describe`/`ssh` selects that group **directly** (no table prompt).
Flags may come before or after the positionals.

- `list` (default) — one-shot table: status, age, uptime, instance counts,
  gateway URL; running groups also get their `ssh -p PORT root@IP` line.
- `refresh` — re-render the table every N seconds (Ctrl-C stops).
- `start` / `stop` / `delete` — numbered table → pick by # or name → confirm
  (delete requires typing the group name). **`start` bills per second;
  `delete` is irreversible** and tombstones the name (recreating it 400s
  `name_conflict` for 10+ min — use a fresh name).
- `ssh` — open an **interactive SSH session** into a live instance of a
  RUNNING group (`ssh -p PORT root@IP`, host-key checking off, known_hosts
  `/dev/null` since the instance IP is ephemeral; auth via your default
  ssh key/agent). The group is named as the 3rd positional or picked from the
  table. A not-running group, or one with no live instance yet, is an error —
  `wait` for it first.
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

### Card & priority flags — `--gpu` / `--prio` / `--query` / `--y`

```
python3 utils/sl_admin.py --query --gpu rtx3090,rtx4090            # prices + availability (FREE)
python3 utils/sl_admin.py start  ORG --gpu rtx3090                 # reconfigure the card, then start
python3 utils/sl_admin.py deploy --type ampere ... --gpu rtx3090 --prio med
```

- `--gpu CARD[,CARD…]` — the card(s) to deploy on / start on. Cards:
  `rtx3080ti, rtx3080, rtx3090, rtx3090ti, rtx4080, rtx4090, rtx5090` (comma
  list, repeated flags, spaces all accepted; matched against the org's own GPU
  classes by the deployers' rule, so `RTX 3090 (24 GB)` == `rtx3090` and
  `RTX 5090 Laptop (24 GB)` is a **different** class). On `deploy` it becomes
  the deployer's own `--gpu`. On `start` it prints the card prices, shows the
  diff, asks for confirmation, then PATCHes `gpu_classes` — a card change IS
  PATCH-able. A group that has been started before **parks** the update
  (`pending_update_in_progress`): the new card lands at the next start, i.e.
  the start this command is about to do, and the tool says so.
- `--prio low|med|high|batch` — priority. **CREATE-only** (measured 2026-10-14:
  `PATCH container.priority` → 400 `pending_update_in_progress`; a top-level
  `priority` PATCH answers 200 and is **ignored** — the GET keeps the old
  value). On `deploy` it is passed to the deployer as `--priority`. On `start`
  it means **DELETE + CREATE under a fresh name** (`<name>-<prio>`): the tool
  copies the whole live config (image ref as-is, env wholesale, resources,
  probes, networking minus the server-assigned `dns`), rewrites the env values
  that ARE the group name (`SALAD_GROUP` is the idle watchdog's stop path —
  left pointing at the deleted name the self-stop 404s and billing continues;
  `MODEL_ALIAS` is deliberately kept stable for clients), confirms by typing
  the group name, then creates at the new priority and waits for it to settle.
- `--query` (needs `--gpu`) — free read-only: per-card **hourly prices** by
  priority tier (`H`igh / `M`edium / `L`ow / `B`atch, USD/hour) plus the
  account's availability counts. The endpoint answers for the whole card set in
  one shot, so the counts are **per priority tier, not per card**. Add
  `--project/--group` to query with that group's resource floors
  ("can a container of this shape be placed right now").
- `--y` / `--force` / `-y` — skip the confirmations of `start` and `deploy
  --start` (the card reconfigure, the priority recreate, and the paid-start `y`).
  **It skips the gate on billing, not the billing**: these runs bill per second.

Offline suite: `python3 -I tools/test_gpu_prio_offline.py` (no API calls).

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
