helpers for deploying dockers with llama-server serving models downloaded from huggins face/ on salad (qwen, swift etc). It's using all knobs available in docker groups of salad, just basic which enough for running llm in docker, e.g. we're only using readiness probe, not using at all "command", "ext log", "scheduled scaling..." Salad is limited to 1 gpu per container, so, you're maxed by 5090. You have everything prepared for claude / cline (cline is (IMHO) just faster for routine tasks, claude seems to be more capable). I use this for deploying my llama-cpp on salad on demand, switching cards and quantities of cards. Cline runs on default gguf's templates, template for claude is rectified in docker (1 line edit), so, you will need to tell script if you're using llm for claude or not. I started coding it with cline, switched to claude, so, initial export of promt for working with salad's api is left in .clinerules. 

Salad uses it's own api key to secure acces to cloudflare gw, so, you have to run harness via proxy which will add salad's key to llama's requests (if you're a security maniac, you can set llama's api key also, it's coded/tested, but i don't use it, since i work 100% with local llms). Everything was coded with qwen3.8 27b q6k served by vllm on 2 3090.

Docker images can be : https://github.com/suedenglehem/docker4salad/blob/main/docker/README.md 
Beware, there's auto-stop in these dockers after idling too long (useful not to pay much if you loose connection mid-flight)

## TD;LR

The run sequence: freshly created Salad account → Claude Code running on a Salad GPU. Steps 1–2 are the web UI (orgs and projects **cannot** be created through the public API), everything after is this repo. `yours` = your own org / project / group names; `https://<group>.salad.cloud` is the group's `networking.dns` (https, 443 — Cloudflare fronts it, the in-container port is not forwardable), printed by `start` / `list` / `describe`.

**1. portal.salad.com** — sign up, create the **organization** and a **project**, copy the account **API key** (it is account-wide; group-scoped keys don't exist).

```bash
# 2. keys on disk — gitignored, chmod 600, never printed
printf '%s' "<SALAD_API_KEY>" > deploy/salad_api.txt && chmod 600 deploy/salad_api.txt
cp deploy/salad_api.txt claude/salad_api.txt          # the gateway stack reads its own copy
printf '%s' "<HF_TOKEN>" > deploy/hft.txt && chmod 600 deploy/hft.txt

# 3. register them in the fleet map (the API has no list-orgs / list-projects,
#    so this tool walks utils/fleet.json)
python3 utils/sl_admin.py create-org     yours --project your_project --register-only
python3 utils/sl_admin.py create-project yours your_project --register-only

# 4. CREATE the group — FREE (--no-start; CREATE and PATCH are free, only START bills)
#    pick a card first (FREE): hourly prices per priority tier + availability counts —
#    rtx3080ti,rtx3080,rtx3090,rtx3090ti,rtx4080,rtx4090,rtx5090
python3 utils/sl_admin.py --query --gpu rtx3090,rtx4090
python3 utils/sl_admin.py deploy --type q6 --org yours --project your_project --group your_group \
    --gpu rtx3090 --prio med -- --idle-shutdown heartbeat --stop-key deploy/salad_api.txt
#   the same, straight from the deployer (its built-in defaults ARE the Claude Code
#   backend profile: q6 mtp-vision image, Q5_K_M, CTX 90000, rtx3090):
python3 -I deploy/deploy_qwen38_27b.py --org yours --project your_project --group your_group \
    --idle-shutdown heartbeat --stop-key deploy/salad_api.txt --no-start

# 5. START it — PAID. --min-bw-mbps is the bandwidth arbitrator: a slow HF download
#    POSTs /reallocate (new node, max 3 shots, 4th host kept), the group is never stopped
python3 utils/sl_admin.py start yours --min-bw-mbps 100
python3 utils/sl_admin.py wait  yours          # polls until an instance reports ready

# 6. smoke test the gateway (no proxy — known-answer questions)
./claude/sl_curl.sh -url https://<group>.salad.cloud -m qwen38-27b

# 7. run claude code via proxy (proxy adds salad's api key to headers)
claude/cl_salad https://<group>.salad.cloud    # group already live: proxy + claude only

# 8. stop group (you will be asked which group to stop)
python3 utils/sl_admin.py stop yours

# 9 check money left
python3 utils/billing.py

# 10. On next runs, you simply start group and fire claude 
python3 utils/sl_admin.py start yours --min-bw-mbps 100
python3 utils/sl_admin.py wait  yours
claude/cl_salad https://<group>.salad.cloud    # group already live: proxy + claude only
```

Free by construction: 1–4 cost nothing, 5 is the only paid step, and the group auto-stops itself when the client connection dies (`--idle-shutdown heartbeat` + the `/.ssh` presence guard — `touch /.ssh` after SSH-ing into the container, `rm /.ssh` on the way out). `python3 utils/sl_admin.py describe yours --project your_project --group your_group` dumps the whole config of a group (image + digests, resources, GPU classes, CMD, probes, full env with secret values redacted, instances + SSH lines).

## Layout

- `salad_client.py` — stdlib-only Salad OpenAPI client (groups, projects, GPU classes, PATCH via `update_container_group`). API key from `deploy/salad_api.txt` (gitignored, never printed).

- `deploy/` — create-or-update deployers for the live groups (all take `--org`, `--project`, `--group`, `--no-start`), with the keys they read next to the scripts: `salad_api.txt` (Salad API, via `salad_client.py`) and `hft.txt` (HF token) — gitignored, chmod 600, never printed.

  - `deploy_qwen38_27b.py` — canonical, for the production 27B Claude Code backend `qwen38-27b-q6k` (baked q6-mtp-vision image, main model fetched via `hf`). Built-in defaults = the live production profile (Q5_K_M @ CTX 90000 on rtx3090) — a bare run re-applies exactly that (idempotent against the live group).
  
  - `deploy_generic.py` — model-agnostic, **vanilla** `lmss_generic` image (digest-pinned): any GGUF via `--model-repo` / `--model-file`, draft + vision opt-in, `--gpu` takes any card the org lists.
  
  - `deploy_ampere.py` — the llamAmpere **fork** build (`lmss_generic_ampere`, turbo5/turbo4 KV): the ATX-Swift production profile is the built-in default (full 132768 ctx on an RTX 3090), `--gpu` limited to the org's sm_86 30-series cards.
  
  - `deploy_qwen38_9b.py` / `deploy_qwen9b.py` — plain 9B (no draft/vision/template).
  
  - All five take `--idle-shutdown none|idle|heartbeat` (+ `--idle-timeout` / `--heartbeat-timeout`, default 600 / 300 s; **`--stop-key <file>`** = the account-wide per-user Salad API key — group-scoped keys don't exist — stored b64-obfuscated in the group env): the in-container `idle_watchdog.py` arms on the group `/ready` endpoint, holds `IDLE_ARM_GRACE`, then **POSTs the group `/stop`** and SIGTERMs — the only kill that truly stops billing (a bare self-exit gets RESCHEDULED, not stopped — paid test 2026-10-09; key `none`/missing **disables the watchdog**, the deployers warn). Clients ping via `SALAD_HEARTBEAT=1` (1-token call every 30 s; `sl_claude_deploy` auto-detects it from the group env). `--ssh-guard 1` (default): while `/.ssh` exists in the container the watchdog holds off its stop and the bandwidth arbitrator spends no reallocate shot — `touch /.ssh` after logging in over SSH, `rm /.ssh` on the way out.
  
  - **Chat template — three sources** (all five deployers, added 2026-10-12): nothing set = the template **embedded in the GGUF**; `CLAUDE_TEMPLATE=1` (the production default, `--no-claude-template` to turn off) = dump the GGUF's template at startup and apply the one-line mid-conversation-system patch Claude Code needs; **`--chat-template-url <http(s)-url>`** = **download a custom template** from a URL at startup (your own repo/gist — use a *raw* URL, not a GitHub blob page; test URL = `https://raw.githubusercontent.com/suedenglehem/docker4salad/main/docker/qwen3.8.q6.jinja`, live-verified 2026-10-12). The URL **wins over both** others and the Claude patch is NOT applied to it; the image validates the download (size/UTF-8/Jinja-markup/not-HTML) and **kills the container on junk** (STOPPED group, no billing). Needs a chat-template-url-capable image (generic **v13** / q6 **v15** / ampere **v11**; `bash /opt/llama.cpp/version.sh` prints the generation). Docs: `docker/README.md` → [The Claude Code template](docker/README.md#the-claude-code-template); offline suite `python3 -I tools/test_chat_template_offline.py`.
  
- `utils/` — operational helpers (see `utils/README.md`):
  - `utils/llama_stats.py` — host entry point for llama-server stats (generation/prompt tps, queue, token totals, spec-decode) scraped from `/metrics`. Run it from the host against a Salad gateway URL (Salad key auto-loaded from `deploy/salad_api.txt`) or any local server: `python3 utils/llama_stats.py https://<group>.salad.cloud [--raw] [--once | --interval N]` (defaults to a 5s loop). Implementation lives in `docker/llama_stats.py` (must be inside the build context); the in-container wrapper `stats.sh` is installed in the image by `Dockerfile.multistage`, aliased there as **`llama-stats`** (local-only stats, works over Salad's shell-less SSH exec: `ssh -p PORT root@IP llama-stats --once`).
  
  - `utils/billing.py` — per-org Salad portal credit balances (USD + live EUR column). The public API has no billing operation, so it logs into the portal backend with credentials from `utils/portal_vault.gpg` (GPG AES256, gitignored; passphrase via `$PORTAL_VAULT_PASS` or prompt). First run creates the vault: prompts for email/password/org slugs and verifies the login before encrypting.
  - `utils/sl_admin.py` — the fleet admin for every known org/project: `list` (default, status table with age/uptime, instance counts, gateway URLs, SSH lines for running groups), `refresh` (live re-render, Ctrl-C), `start` (bills), `stop`, `wait` (poll one group until an instance reports ready), `stats` (llama-server stats — only when the group is fully ready: running, all replicas present, all instances `ready`; otherwise prints the state and exits 1), `describe` (whole-config dump of one group: image repo/tag/manifest digest/Hub link + the `docker image inspect` line, resources and GPU classes by name, CMD, probes, the full env with secret values redacted, live instances with ids and SSH lines, `--raw` for the whole GET as JSON), `delete` (typed-name confirm; leaves a name tombstone). Actions pick a group from a numbered table by # or name.
    - `deploy` — **pick a DOCKER TYPE from a menu** (`q6` = the Claude Code backend, `generic` = any GGUF on vanilla lmss, `ampere` = the llamAmpere fork (sm_86 30-series cards), `9b` / `qwen9b` = plain 9B on base lmss) and it runs that deployer against an org/project/group, prompting for whatever you don't pass (`--type/--org/--project/--group`; flags after `--` go to the deployer verbatim). **Free by default** — it runs the deployer with `--no-start` (CREATE/PATCH are free, only START bills); `--start` is the paid opt-in and asks for a typed `y`.
    - **card & priority flags** — `--gpu CARD[,CARD…]` (`rtx3080ti, rtx3080, rtx3090, rtx3090ti, rtx4080, rtx4090, rtx5090`) on `deploy` (create/update on that card) and on `start` (print the hourly prices, show the card diff, confirm, PATCH `gpu_classes`, then start — a group that has been started before **parks** the change and it lands at this very start); `--prio low|med|high|batch` — priority is **CREATE-only** (measured 2026-10-14: `PATCH container.priority` → 400, a top-level `priority` PATCH → 200 and IGNORED), so on `deploy` it goes to the deployer as `--priority` and on `start` it means DELETE + CREATE as `<name>-<prio>`, copying the whole live config (env values that ARE the group name follow the new name — `SALAD_GROUP` is the idle watchdog's stop path; `MODEL_ALIAS` stays stable for clients); `--query --gpu …` (**free**) = per-card USD/hour by priority tier + availability counts (counts are per tier for the whole card set, not per card; add `--project/--group` to query with that group's resource floors); `--y` / `--force` skips the `start` / `deploy --start` confirmations — it skips the gate on billing, not the billing. Offline suite: `python3 -I tools/test_gpu_prio_offline.py`.
    - `create-org` / `create-project` — orgs and projects **cannot be created through the public API** (live-probed 2026-10-13: every create/list path is 404; they exist only in the web UI). These actions attempt the portal backend (the web UI's own API, via the `billing.py` vault — undocumented, so a 404 is the expected answer; `--no-portal` skips it) and then **register** the org/project in `utils/fleet.json`, which is the map this tool walks (the Salad API has no list-orgs/list-projects). `--register-only` after creating in the web UI.
  
  - `start --min-bw-mbps N` — **bandwidth arbitrator** (default cap 30): after the start, the manager reads the in-container `bw_reporter` log over SSH (`/tmp/llama-api/bw.log`, `epoch bytes` of `MODEL_DIR` every 10 s) and takes the median HF-download rate. Below the cap it POSTs the instance `/reallocate` endpoint — Salad places the group on a **new node** and excludes the rejected one from the account pool for 48 h, so every shot is a genuinely new host. Max **3 reallocations**; if no faster host turns up in 3 shots the **4th host is kept** and the download continues. The group is **never stopped** for bandwidth (a row of slow nodes would make a stop/start loop). Needs the bw_reporter images (base/generic `cuda128-v8`, q6 `cuda128-v10`, ampere `cuda130-v6`); Ctrl-C leaves the group running as-is.
  
- `docker/` — image builds (`Dockerfile.multistage` + baked `Dockerfile.lmss_q6_mtp_vision` + model-agnostic `Dockerfile.lmss_generic`), local compose (27B + 9B services), plus `check_status.sh` / `version.sh` / `stats.sh` (in-container stats via `/metrics`) / `bw_reporter.py` (HF-download bandwidth sampler feeding the arbitrator). Images also ship `ping` (needs `CAP_NET_RAW`), `scp` (via `openssh-client`) and `llama-stats`. See `docker/README.md`.

- `tests/` — **local** run/test helpers for the compose services (nothing here touches the Salad API): `run_27b.sh` / `run_9b*.sh` (they cd into `docker/` themselves), `curl1.sh` / `curl2.sh` (API check / question→answer, read the key through a local `api.txt` symlink), `run-nvidia-smi.sh` (one-shot GPU info). See `tests/README.md`.

- `claude/` — the Claude Code ↔ Salad gateway stack: `cl_salad` / `sl_claude_deploy` + `sl_proxy.py` (Anthropic↔OpenAI bridge), gateway test helpers (`sl_curl.sh`, `sl_chat.sh`), and the gateway `salad_api.txt`. See `claude/README.md`.

## sladmin --help

`python3 utils/sl_admin.py --help` prints the tool's own docstring; this is that
reference, formatted. The docstring in `utils/sl_admin.py` is the source of truth.

```
python3 utils/sl_admin.py [ACTION] [ORG[/PROJECT]] [GROUP] [--interval N]
```

### Actions

| action | what it does |
|---|---|
| `list` *(default)* | one-shot status table — status, age (since creation), uptime (time in the current state), instance counts, gateway URL from `networking.dns` (no `:port` suffix; the gateway routes it). Running groups also get their `ssh -p PORT root@IP` line + host-key fingerprint |
| `refresh` | re-render the table every `--interval N` seconds (Ctrl-C stops) |
| `start` | pick a group (# or name) and start it — **INCURS COST**, running instances bill per second. `--gpu CARD` reconfigures the card(s) first, `--prio` recreates the group at that priority (CREATE-only) |
| `stop` | pick a group and stop it |
| `wait` | poll a group until at least one instance reports `ready`, then exit — group status alone is not enough: Salad marks a group `running` while instances are still allocating/downloading/creating |
| `describe` | **dump one group's whole config, read-only**: identity (id/version/pending_change/timestamps), state, lifecycle (replicas, restart/autostart, priority, scaling), networking (dns/port/auth), **image** (repo, tag, manifest digest, Hub link, the `docker image inspect` line), container (hash, image_caching, cpu/memory/shm/storage, **GPU classes by NAME**, volume, CMD), probes, the **full env** (secret-looking values redacted), live instances (id, state, ready, machine_id, pull progress, ssh line) + the derived client lines. `--raw` also dumps the whole GET as JSON. `describe ORG --project P --group G [--raw]`, or pick from the numbered table |
| `stats` | llama-server stats (tps / queue / totals, `--interval` loop, Ctrl-C stops) — **only when the group is fully ready**: status `running`, all `replicas` instances present, every one `ready=true` (stricter than `wait`'s "at least one ready"). Otherwise prints the state and exits 1 without scraping |
| `delete` | pick a group and delete it — **IRREVERSIBLE**, leaves a name tombstone |
| `ssh` | open an **interactive SSH session** into a live instance of a RUNNING group (`ssh -p PORT root@IP`). The group is named as the 3rd positional (`ORG[/PROJECT] GROUP`) or picked from the table. Not-running group, or a group with no live instance yet, is an error — `wait` for it first |
| `create-org` / `create-project` | register (and try to create) a new ORGANIZATION / PROJECT — see *Org & project creation* below |
| `deploy` | pick a **DOCKER TYPE** (the profiles our images support) and run its deployer against an org/project/group — **zero cost by default** (`--no-start`; `--start` bills) |

**Positional args** (in order, after the action):

| arg | |
|---|---|
| `ORG` | `ma-casa-in-paris` \| `akl-on-salad` (default: every known org); may be `ORG/PROJECT` to pin the project (e.g. `ma-casa-in-paris/llm`) |
| `GROUP` | the group name. For `start`/`stop`/`wait`/`stats`/`delete`/`describe`/`ssh` it selects that group **directly** (no table prompt); without it those actions show the numbered table. Flags may come before or after the positionals — `… atx-swift-27b-q5 --min-bw-mbps 100` |

### Flags

**general**

| flag | |
|---|---|
| `--interval N` | poll period in seconds for `refresh` / `wait` / `stats` (default 5) |
| `--min-bw-mbps N` | `start` only: the **bandwidth arbitrator** — after the start is accepted, watch the HF model download's speed (read from the container's `${API_STATE_DIR}/bw.log` over SSH, written by `docker/bw_reporter.py`); when the median rate over a 2-min window falls below N Mbps, POST the instance `/reallocate` so Salad places it on a **new node** (the rejected node is excluded from the account's pool for 48 h, so every shot is a genuinely new host). Max **3 shots**; if no faster host appears in 3, the **4th host is KEPT** and the download continues. The group is **never stopped/SIGTERMed** for bandwidth. Needs a bw_reporter image (base/generic v8, q6 v10, ampere v6) + SSH access. Exit codes: **0** accepted / download complete / kept 4th host · **1** group bail (stopped/failed) · **2** usage · **3** arbitrator bail (persistent ssh failure, alloc timeout, reallocate API failure, missing `bw.log` = old image). Ctrl-C leaves the group running |

**card & priority**

| flag | |
|---|---|
| `--gpu CARD` | `CARD` = **comma-separated cards** from `rtx3080ti, rtx3080, rtx3090, rtx3090ti, rtx4080, rtx4090, rtx5090` (repeated `--gpu` flags and spaces are fine). **`deploy`**: passed to the deployer as `--gpu <cards>` (its own per-type choices still apply — q6 = rtx5090/rtx3090, ampere = sm_86 30-series). **`start`**: the group's `gpu_classes` are PATCHed to `CARD` before the start; if the configured card(s) differ it asks for confirmation (`--y`/`--force` answers it). A card change IS PATCH-able; a group that has been started before **parks** the update — the new card(s) land at the next start, i.e. the start this command does. Prints each card's hourly price |
| `--prio PRIO` | `PRIO` = `low \| med \| medium \| high \| batch` (`med` == `medium`). **`deploy`**: passed to the deployer as `--priority` (a CREATE field, so a new group gets it directly). **`start`**: priority is **CREATE-only** (measured 2026-10-14: `PATCH container.priority` → 400 `pending_update_in_progress`; a top-level `priority` PATCH → 200 and the GET proves it was IGNORED), so this **deletes** the group and **creates** it as `'<name>-<prio>'` at that priority, copying image/env/resources/probes/networking. Asks for the typed group name; `--y`/`--force` skips the ask. The old name is then tombstoned 10+ minutes |
| `--query` | with `--gpu CARD`: each card's **hourly price** (all four tiers) + the org's GPU availability. The counts come from `POST …/availability/sce-gpu-availability` and are **BY PRIORITY TIER for the whole card set** (high/medium/low/batch + on_call), not per card. With `--project P --group G` it queries with that group's cpu/memory/storage floors ("can a container of this shape be placed now"). Read-only, free |
| `--y` / `--force` | skip confirmations on `start` (card diff, paid start) and on `deploy` (the paid `--start` 'y'). Every bypass prints a PAID notice |

**deploy**

| flag | |
|---|---|
| `--type T` | the DOCKER TYPE key (`q6` / `generic` / `ampere` / `9b` / `qwen9b`); prompted with a numbered menu when absent |
| `--org O` / `--project P` / `--group G` | answer the corresponding prompt on the command line; anything unspecified is prompted (org/project from the fleet map, group name with the type's default in brackets) |
| `--start` | **PAID opt-in**: run the deployer without `--no-start`, after typing 'y'. Default is config-only (CREATE/PATCH are free; only START bills). The 9B deployers create the group STOPPED and never start, so nothing bills on those types either way |
| `-- <flags>` | everything after `--` goes to the deployer verbatim (`--ctx-size`, `--gpu`, `--model-repo`, `--idle-shutdown`, …) |

**create**

| flag | |
|---|---|
| `--project P` | (`create-org`) project to register with the org; repeatable |
| `--register-only` | skip the API/portal attempts, only write `fleet.json` |
| `--no-portal` | skip the portal (web-UI backend) attempt |

### Examples

```bash
python3 utils/sl_admin.py                                   # status, all known orgs
python3 utils/sl_admin.py akl-on-salad                      # one org
python3 utils/sl_admin.py refresh                           # live table, Ctrl-C to stop
python3 utils/sl_admin.py delete                            # numbered table -> prompt -> confirm
python3 utils/sl_admin.py start akl-on-salad                # start a group in that org
python3 utils/sl_admin.py start --min-bw-mbps 30            # start + arbitrator
python3 utils/sl_admin.py wait                              # after start: poll until ready
python3 utils/sl_admin.py stats                             # stats, only if fully ready
python3 utils/sl_admin.py start ma-casa-in-paris/llm atx-swift-27b-q5   # start that group
python3 utils/sl_admin.py ssh ma-casa-in-paris/llm atx-swift-27b-q5     # ssh into it
python3 utils/sl_admin.py start ma-casa-in-paris/llm atx-swift-27b-q5 --min-bw-mbps 100
python3 utils/sl_admin.py deploy                            # prompt: type -> org/project/group
python3 utils/sl_admin.py deploy --type ampere --project llm --group my-atx
python3 utils/sl_admin.py deploy --type generic --group my-gguf -- \
        --model-repo Some/Org --model-file Model-Q5_K_M.gguf --ctx-size 32768
python3 utils/sl_admin.py create-org my-new-org --project default
python3 utils/sl_admin.py create-project my-new-org llm

python3 utils/sl_admin.py --query --gpu rtx3090,rtx4090                      # prices + availability
python3 utils/sl_admin.py --query --gpu rtx3090 --project llm --group atx-swift-27b-q5
python3 utils/sl_admin.py start akl-on-salad --gpu rtx3090                   # card diff -> ask -> start
python3 utils/sl_admin.py start akl-on-salad --gpu rtx4090 --force           # no ask (PAID)
python3 utils/sl_admin.py start akl-on-salad --prio high                     # recreate at high priority
python3 utils/sl_admin.py deploy --type ampere --project llm --group atx-med --gpu rtx3090 --prio med
```

### Notes

- **`start` bills you** — running instances are charged per second.
- **`delete` is irreversible** and leaves a **NAME TOMBSTONE**: recreating a
  just-deleted group name 400s with `name_conflict` for 10+ minutes — use a
  fresh name if you recreate (keep the model alias stable for clients).
- There is **no group-level image "refresh"** in the API (live-probed
  2026-10-06); to run a new image, PATCH a new image ref — the deployers do
  this (digest-pinned, to bypass the worker image cache).
- The API has **no list-orgs / list-projects**, so this walks a fixed map of
  the account's orgs and projects: `KNOWN_PROJECTS` in the script, overlaid by
  `utils/fleet.json` (what `create-org` / `create-project` write).

### Org & project creation — the honest state

Live-probed 2026-10-13 (`tools/probe_org_create.py`): the **public** API
(`api.salad.com/api/public`) has NO create-org, NO create-project, NO list-orgs
and NO list-projects — every one of those paths answers 404 (the OpenAPI spec's
24 paths are all `/organizations/{org}/…`, none on the collection itself). Orgs
and projects are created in the **web UI** (portal.salad.com), whose backend
(`portal-api.salad.com/api/portal`) is what `utils/billing.py` logs into. So
`create-org` / `create-project` do two things: **(1)** ATTEMPT the portal
endpoint with the billing vault's credentials (best-effort, `--no-portal` to
skip — the portal API is undocumented and unverified, so a 404 there is
expected, not a bug), and **(2)** REGISTER the org/project in the local fleet
map (`utils/fleet.json`) so the rest of the tool sees it. Registration is the
part that always works; use `--register-only` after you create in the web UI.
