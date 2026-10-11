# on_salad — PROGRESS.md / project knowledge base (tr4 mirror)

> **What this file is.** A single, self-contained knowledge base for the `on_salad`
> project (repo `docker4salad`), written so a fresh session on **tr4** — whose
> mycelium has **no memory** of the work done on the primary box — can pick the
> project up cold. Everything that lives in the primary box's mycelium is folded
> in here. Read top to bottom once; the **Gotchas** sections (§8–§11) are the
> load-bearing operational knowledge — `save()` each into mycelium as you meet it.
>
> **Source / as-of.** Compiled 2026-10-10 from box `i714700k` (the primary
> workstation; this mirror is `b760one`/tr4). Both working trees are at git HEAD
> **`c54dd9e`**. Fleet state and digests below were read live from the Salad API
> and local `docker image ls` at compile time — re-run the read-only snapshot
> (§14) if you need a fresher picture.
>
> **Two hosts share this repo path** but it is NOT shared storage: the primary box
> mounts `/dd2` on `/dev/sdi1`, tr4 mounts it on `/dev/sdb1` — separate copies.
> tr4 does **not** hold the built Docker images (only 2 unrelated ones); the
> images live on Docker Hub + the primary box.

---

## 0. TL;DR — state as of 2026-10-11

- **Latest image set = v2.4 (shell ergonomics), fleet re-PATCHed 7/7.** The
  image's `/root/.bashrc` now exports `TERM=xterm-256color` and defines the
  `nv/ht/gpu/wlog/alog/ready/als/ll/la/l/h` aliases — fixes the Salad ssh
  relay's `TERM=xterm` breaking nvtop/htop. Watchdog content unchanged (still
  v2.3: `SSH_GUARD_MAX_HOLD` cap). Hub digests base `2bfbd69c…`, generic
  `888fc401…`, q6 `f6e3b9d0…`, ampere `d4f4b030…`; FROM re-pins + all 5
  deployer re-pins done. Fleet PATCH 7/7 `200 OK` (env byte-identical, no
  starts); propagation verified read-only via `tmp/check_images_v11.py`.
- **Latest feature = the guard's 20-minute hard cap (watchdog v2.3),
  live-smoked 2026-10-11 — FULLY PASSED, user-confirmed ("watchdog cap-kill
  worked").** The SSH-presence guard (v2.2, commit `e115dc6`) has a hard cap:
  `SSH_GUARD_MAX_HOLD` (default **1200 s**; `0` = uncapped legacy). While
  `/.ssh` exists the watchdog still never stops the group — *unless*
  llama-server has been **continuously idle** (no token activity) past the
  cap, in which case it stops anyway. Rationale (user): you can lose internet
  while SSH-logged in; an uncapped hold on a dead session = unbounded
  billing. Any real activity resets the clock. **Paid smoke on
  `atx-hb-test2`: hold line captured verbatim (01:01:45Z); the group stopped
  01:15:14→01:15:28Z — within one 30 s poll of the exact 1200 s cap deadline
  after the last inference (00:54:45Z) — and the user confirms that was the
  watchdog's own cap-kill, not a manual GUI stop. (The verbatim kill line
  itself was missed — watcher armed ~27 min late.) See §15.1.**
- **Arbitrator guard false-positive FOUND LIVE + FIXED (b9eae88, 2026-10-11):**
  the first paid drop-test (atx-hb-test2, cap 10000) looped forever on `ssh
  session active (/.ssh present) — holding, no shot spent` with the file
  GENUINELY absent. The relay merges the remote stderr into the client's
  stdout, so `ls /.ssh`'s own error text — which contains `/.ssh` — sat on
  stdout and the old substring check matched it. The guard now checks ls's
  error markers first (combined streams) and requires an exact `/.ssh` line
  for presence; `_bw_read` hardened the same way. §9f.
- **`atx-hb-test2` is the ONLY live group — the user's paid arbitrator
  drop-test is in flight** (the other six are stopped at $0). Attempt 1's
  node finished the download anyway (READY 04:03:01Z, "download complete",
  zero shots — the bug ate the run's only reallocation opportunity); with the
  fixed code the user is re-running `start --min-bw-mbps 10000` for the
  3-reallocation demo. §15.2.
- **Nothing else is left in the critical path except user-gated paid tests:**
  the arbitrator drop-log live capture (§15.2) is running. Offline suites
  green (watchdog 7/7, arbitrator 24/24).
- **This Claude session runs on LOCAL vllm on tr4** (`qwen3.8-27b` fp8,
  `:8000`, TP2+MTP, since Oct 10) — NOT on a Salad group. Group stops never
  affect the session, and session turns never reset a group's watchdog.

---

## 1. What this project is

`on_salad` deploys **LLM inference** to [SaladCloud](https://salad.cloud) GPU
workers (distributed residential GPUs — 3090s, 5090s, etc.) as **Docker images**,
and wraps it so it's usable from **Claude Code** and plain curl as an
**Anthropic-only** endpoint (`/v1/messages`). The stack:

- **Inference:** `llama.cpp`'s `llama-server`, plus a second fork —
  **llamAmpere** (a custom turbo-KV build) for the production "ATX" profile.
- **Anthropic proxy:** `claude/salad_proxy.py` — a sidecar that serves only the
  Anthropic Messages API shape, converts to llama-server, and strips empty
  `think` blocks (see §8).
- **SSH access:** Salad exposes SSH into a running container
  (`ssh root@<ip> -p <port>`); that's how the user works inside the box and how
  the guard file `/.ssh` is touched (§3).
- **Idle/heartbeat self-shutdown watchdog** (`docker/idle_watchdog.py`): stops
  the group (via the Salad stop API, then SIGTERM) when idle or when the client
  heartbeat goes quiet — so an unattended group stops *billing*. §5.
- **Bandwidth arbitrator** (`utils/manage_groups.py --min-bw-mbps`): during the
  HF model download, if throughput is too slow it relocates the instance to a
  faster node via `POST …/reallocate` (never stop/SIGTERM). §6.
- **Deployers** (`deploy/*.py`): create/patch Salad container groups, digest-pin
  the image, inject the full env, manage the stop key.
- **Clients:** `claude/cl_salad`, `claude/chat_salad.sh`, `claude/salad_proxy.py`.
- **Manager:** `utils/manage_groups.py` — list/start/stop/wait/stats + the
  arbitrator. `utils/billing.py` — cost tracking.

**Images** are on Docker Hub under **`boris271142/`**. There are 4 current
families: the base (`lmss`), the generic any-model image (`lmss_generic`), the
baked Qwen3.8 27B Q6+MTP+vision+Claude-template image
(`lmss_jonathancoletti_qwen38_q6_mtp_vision`), and the llamAmpere fork
(`lmss_generic_ampere`). Two "flavors" of 27B: **vanilla llama.cpp** (generic /
q6) and the **llamAmpere turbo-KV fork** (ampere = production ATX).

**Orgs/projects** (Salad): the account has two orgs — `ma-casa-in-paris`
(production) and `akl-on-salad` (a second one for billing/team separation).
Projects under `ma-casa-in-paris`: `qwen38-27b` (prod 27B) and `llm` (ATX + test
copies). §10.

---

## 2. Fleet — the 7 live groups (CURRENT, read live 2026-10-10)

All 7 are in org **`ma-casa-in-paris`**, all **STOPPED** (0 instances), all
settled on their **guard-v2.2 image** (`pending_change=False`), all carrying
`SSH_GUARD=1` + `HEARTBEAT_TIMEOUT=300`. The two **heartbeat-armed** groups
(`atx-swift-27b-q5-opt`, `atx-hb-test2`) have a real `SALAD_STOP_KEY` +
`IDLE_SHUTDOWN=heartbeat`; the other five have `IDLE_SHUTDOWN=none`
(watchdog disabled — the stop key is present in env but unused).

| # | project / group | spec v | image (guard) | model (file) | ctx | alias | idle |
|---|---|---|---|---|---|---|---|
| 1 | `qwen38-27b/qwen38-27b-q5` | 6 | q6 v11 `16c48781` | Qwen3.8-27B … **Q5_K_M** | 90000 | `qwen38-27b` | none |
| 2 | `qwen38-27b/qwen38-27b-q6k` | 12 | q6 v11 `16c48781` | Qwen3.8-27B … **Q5_K_M** | 90000 | `qwen38-27b` | none |
| 3 | `qwen38-27b/qwen38-9b` | 10 | base v9 `47d2d276` | Qwen3.8-**9B** Q4_K_M | 32768 | `qwen9b` | none |
| 4 | `llm/qwen38-27b-q5` | 6 | q6 v11 `16c48781` | Qwen3.8-27B … **Q5_K_M** | 90000 | `qwen38-27b` | none |
| 5 | `llm/atx-swift-27b-q5` | 6 | generic v9 `9f7b0638` | ATX-Swift-1.5 … MTP **Q5_K_M** | 90000 | `atx-swift-27b` | none |
| 6 | `llm/atx-swift-27b-q5-opt` | 12 | ampere v7 `314f77b8` | ATX-Swift-1.5 … MTP **Q5_K_M** | **132768** | `atx-swift-27b-opt` | **heartbeat** |
| 7 | `llm/atx-hb-test2` | 9 | ampere v7 `314f77b8` | ATX-Swift-1.5 … MTP **Q5_K_M** | **132768** | `atx-swift-27b-opt` | **heartbeat** |

Per-group notes (env read live, secrets redacted):

- **`qwen38-27b/q5` and `/q6k` and `llm/q5`** (rows 1, 2, 4) — identical profile:
  the baked 27B image, `MODEL_FILE=Qwen3.8-27B-Uncensored-Q5_K_M.gguf`
  (repo `JonathanColetti/Qwen3.8-27B-Uncensored-GGUF`), `CLAUDE_TEMPLATE=1`,
  `USE_DRAFT_MODEL=none` (uses the GGUF's embedded MTP head — the image hardcodes
  `--spec-type draft-mtp`, so a noMTP file would crash), `CTX_SIZE=90000`,
  `IDLE_GRACE=1800`. Row 2's name is a **misnomer** (it runs Q5_K_M, not Q6_K);
  kept because renaming = delete+recreate = new DNS + name tombstone (§9). The
  served `MODEL_ALIAS=qwen38-27b` is what clients address.
- **`qwen38-27b/qwen38-9b`** (row 3) — the 9B model (repo
  `empero-ai/Qwen3.8-9B-Distill-GGUF`), base image, `CTX 32768`,
  `CHAT_TEMPLATE=none`, no draft/vision URLs. **Its deployer
  (`deploy_qwen38_9b.py`) is CREATE-ONLY** — it refuses an existing group, so it
  gets PATCHed via direct `salad_client.update_container_group` (see §14).
  Note its env has `IDLE_TIMEOUT=600` but **no `IDLE_GRACE`** key (harmless;
  `IDLE_SHUTDOWN=none`).
- **`llm/atx-swift-27b-q5`** (row 5) — the **baseline** ATX: generic (vanilla
  llama.cpp) image, `MODEL_FILE=ATX-Swift-1.5-Qwen3.8-27B-Uncensored-MTP-i1-Q5_K_M.gguf`
  (repo `bjivanovich/ATX-…-MTP-GGUF`), `SPEC_TYPE=draft-mtp`,
  `VISION_MODEL=hf://bjivanovich/…/mmproj-BF16.gguf`, `EXTRA_ARGS=none`
  (no speculative/sampling tuning — the plain reference), `CTX 90000`.
- **`llm/atx-swift-27b-q5-opt`** (row 6) — **PRODUCTION ATX** on the llamAmpere
  fork. `CTX 132768`, full spec `EXTRA_ARGS`
  (`--threads-batch 8 --prio 3 --spec-draft-n-max 3 --spec-draft-p-min 0.1
  --spec-draft-type-k q8_0 --spec-draft-type-v q8_0
  --spec-draft-vocab-map /opt/llama.cpp/mtp-vocab/atx_65536.txt --temp 0.2
  --top-p 0.90 --top-k 40 --repeat-penalty 1.08 --repeat-last-n 256`).
  **Note: `--reasoning-format none` is NOT in this list — the deployer's
  by-design strip removed it** (§8 double-think). `IDLE_SHUTDOWN=heartbeat`,
  `IDLE_GRACE=1800`, real stop key. **Pending verify at next start:** scrape
  `/metrics` for `llama_model_n_ctx` (expect **132864** = 132768 rounded up to a
  128-block). §7.
- **`llm/atx-hb-test2`** (row 7) — the **watchdog/heartbeat test group** (fork
  image, same ATX profile as row 6, `CTX 132768`). `IDLE_SHUTDOWN=heartbeat`,
  **`IDLE_GRACE=3600`** (raised for the big-quant cold start — §5), real stop
  key. This is the group the guard + heartbeat paid tests run on.

**`restart_policy` is CREATE-ONLY** (not changeable by PATCH; the GET here does
not surface it on stopped groups). The test group was created with
`restart_policy=never`; the prod `…-opt` with `always` (per the build history).
`priority` is also CREATE-ONLY.

---

## 3. The SSH-presence guard (v2.2, capped in v2.3) — the latest feature

**Problem.** The user works inside the containers over SSH (`ssh root@ip -p
port`). Two independent automated kills fought that: (1) the **idle/heartbeat
watchdog** self-stopped the group while the user was logged in; (2) the
**bandwidth arbitrator** reallocated the instance mid-download, killing the SSH
session. Both are billing/latency hygiene that must keep working when nobody is
in the box, but must not fire while the user *is* in it.

**The user's pivot** (the original `ssh_sentinel.py` daemon + marker-freshness +
`/proc` process-scanning design was **scrapped**, verbatim):
> "ok, forget it all, we can making extremly simple. after getting into
> container, i will touch file \"/.ssh\" so, simply check if that file exists and
> block reboot/shutdown actions. i'm fine with that way of working"

**The contract.** `touch /.ssh` right after logging in over SSH; `rm /.ssh` on
the way out. While the file exists: the watchdog holds its stop and the
arbitrator spends no reallocate shot. No daemon, no freshness, no process scan —
a plain existence check. **Commit `e115dc6`** (2026-10-09).

**Watchdog side** (`docker/idle_watchdog.py` → `ssh_guard_active()`, ~line 270):
at the moment it would kill, it checks `os.path.exists("/.ssh")`. v2.2: if
present it reset `last_activity` (uncapped hold). **v2.3 (current):** the hold
branch **no longer resets `last_activity`** — the guard merely `continue`s, so
`idle_s = now - last_activity` stays true continuous-idle-since-last-real-token-
activity; a hold line `"ssh guard /.ssh present — stop held off (idle …s, cap
1200s)"` logs every 300 s while held, and when `idle_s >= SSH_GUARD_MAX_HOLD`
the kill fires **regardless of `/.ssh`** (`…stopping the group anyway…`).
Knobs: **`SSH_GUARD=1|0` (default 1)**, **`SSH_GUARD_FILE` (default `/.ssh`)**,
**`SSH_GUARD_MAX_HOLD` (default 1200 s; `0` = uncapped legacy)**. A stat
failure counts as **absent** (fail-open toward the
kill = the billing-hygiene default — a broken guard doesn't accidentally keep a
group billing forever).

**Arbitrator side** (`utils/manage_groups.py` → `_bw_ssh_guard()`, ~line 571):
before spending a reallocate shot it probes `ls /.ssh` over SSH. The **payload
decides** (Salad's relay exits 1 even on success — §9): stdout naming the file ⇒
**present → hold, no shot**; `cannot access` in stderr ⇒ **absent → spend the
shot**; ssh exit 255 / inconclusive ⇒ **strike → spend the shot**. It **fails
OPEN** on probe failure so a broken probe never silently disables the
arbitrator. Both shot branches (pull-stuck + slow-download) are gated by it.

**Deployers ×5** (`deploy_qwen38_27b.py`, `deploy_qwen38_9b.py`,
`deploy_qwen9b.py`, `deploy_generic.py`, `deploy_ampere.py`): gained
**`--ssh-guard 1` (default 1)** → `SSH_GUARD` env (always sent — harmless on
pre-guard images), and **`--heartbeat-timeout` default raised to 300** (was 600;
the client pinger fires ~every 30 s so 300 s = a comfortable margin).

**Tests.** Arbitrator offline harness **23/23** (`tmp/test_arbitrator_offline.py`,
scenarios 14–17 cover the guard; `tmp/fakebin/ssh` dispatches the bw probe vs the
guard probe by the last argv token). Watchdog offline suite **7/7**
(`tmp/test_watchdog_offline.py`, rewritten for v2.3: held-off while `/.ssh`
present, `rm`→kill, absent→kill, `SSH_GUARD=0`→kill, uncapped (`SSH_GUARD_MAX_HOLD=0`)
+ guard forever → never kills, default cap 1200 + guard forever → cap-kill,
activity-bump resets the clock, cap=160 → early cap-kill). Both run with
`python3 -I`.

**Fleet PATCH — DONE 2026-10-10 (verified).** All 7 groups PATCHed zero-cost
(`--no-start`; `qwen38-9b` via direct `update_container_group`) with
`HEARTBEAT_TIMEOUT=300` + `SSH_GUARD=1`, images untouched at that step, and the
result settled (`pending_change=False` on all 7, confirmed by live GET).
Snapshot diff lives in `tmp/fleet_env_{before,after}.json`. The two
heartbeat-armed groups kept their armed mode + real stop keys; the five
`IDLE_SHUTDOWN=none` groups got `IDLE_GRACE=1800` (neutral, the baked default).

---

## 4. The image chain (CURRENT 2026-10-11) — the v2.4 set (watchdog still v2.3)

**The v2.4 set (tr4, 32 jobs, warm layer cache — ~14 min).** All four images
are BUILT + PUSHED + digest-verified (offline: the .bashrc block + build id
checked in each), the fleet is re-PATCHed onto it 7/7, and all 7 digests
confirmed landed via read-only GETs (2026-10-11 02:14Z). Content diff vs the
v2.3 set = ONLY the `/root/.bashrc` shell-ergonomics block (see below);
watchdog content is unchanged (still v2.3). For these single-arch amd64
images the Hub manifest digest == the local image ID (the registry-returned
`digest:` line on push):

| family | repo:tag | digest `sha256:` | what's in it |
|---|---|---|---|
| base | `boris271142/lmss:cuda128-v11` | `2bfbd69c5730b9787f2b91cfd496ebbfdcce469ba99fc538fbe5a66b2a305455` | llama.cpp server + proxy + watchdog **v2.3** + bw_reporter + ping + llama-stats + **shell ergonomics** |
| generic | `boris271142/lmss_generic:cuda128-v11` | `888fc401f1eba851888f22bf8f8caf9665df41f734cfe1432e6ed1df23f14b1b` | base, any-model, no baked model |
| q6 | `boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:cuda128-v13` | `f6e3b9d0c4f6b3fb08ecfabb94c4115d256cdce246054202f858b868115bbfb9` | base + Qwen3.8 27B Q6 + MTP + vision + Claude template |
| ampere | `boris271142/lmss_generic_ampere:cuda130-v9` | `d4f4b030fe2d88c1fdaaf5a31505ac0f4d357f1db38f8e7b369804d3502e6593` | llamAmpere turbo-KV fork, sm_86 cards |

**Shell-ergonomics block (the v2.4 content change), appended at the END of
`/root/.bashrc` in the final stage** (`Dockerfile.multistage` +
`Dockerfile.lmss_generic_ampere`; generic/q6 inherit via their FROM re-pin).
The Salad ssh relay execs a bare `/bin/bash` on a pty with `TERM=xterm`
(breaks nvtop/htop) but DOES read `/root/.bashrc` (verified live):
`export TERM=xterm-256color` + aliases `nv`(nvtop) / `ht`(htop) / `gpu`
(nvidia-smi) / `wlog` (tail -f the watchdog log) / `alog` (tail -f the API
log) / `ready` (curl :9999/ready) / `als` (alias) / `ll` / `la` / `l` / `h`
(history).

**Superseded (v2.3 set, guard-v2.3, commit `29de3ec`):** base v10
`c363d00e`, generic v10 `95b4c77c`, q6 v12 `56579bc1`, ampere v8
`f713e04f`. **Superseded (v2.2 set):** base v9 `47d2d276`, generic v9
`9f7b0638`, q6 v11 `16c48781`, ampere v7 `314f77b8`. **Pre-guard (v2.1)**,
tags deleted from Hub: base v8 `0386058c`, generic v8 `9fe70c07`, q6 v10
`57656009`, ampere v6 `ceb9059f`.

**Build procedure** (tr4, 32-way): build the **base first**, push, capture its
digest; re-pin the **derivative `FROM`s** to the base digest
(`docker/Dockerfile.lmss_generic:75` and `docker/Dockerfile.lmss_q6_mtp_vision:59`
both `FROM boris271142/lmss:cuda128-v11@sha256:2bfbd69c…`); build+push the
three derivatives with `--build-arg BUILD_JOBS=32`. **Build-context gotcha:**
the generic/q6/ampere Dockerfiles `COPY docker/version.sh` → the build context
MUST be the **repo root**, and on tr4 the working invocation is
`cd <ctxdir> && docker build -f <Dockerfile> .` (a relative `-f` + separate
context arg trips this host's buildx arg-parsing); only `Dockerfile.multistage`
takes the `docker/` subdir as context. After **every push**, set the repo
`full_description` overview (standing rule, §11 — verified this round: all 4
repos already carry it, no PATCH needed).

**Cache reality (correction):** tr4 DOES accumulate a warm docker layer cache —
the first (v2.3) build set was genuinely from scratch (~1.5h+ for the
non-ampere trio), but the v2.4 rebuild of all four images took only **~14 min**
(01:42:05→01:55:48Z) because only the appended `.bashrc` RUN block and later
layers rebuilt. Plan ~15 min for small content changes; the long pole (ampere
llamAmpere CUDA template-instances pass) only bites on cold rebuilds.

**Deployer digest pins** (re-pinned 2026-10-11 to the v2.4 set, the live
`@sha256:` line in each): `deploy/deploy_qwen9b.py` +
`deploy/deploy_qwen38_9b.py` → base v11 `2bfbd69c…`;
`deploy/deploy_generic.py` → generic v11 `888fc401…`;
`deploy/deploy_qwen38_27b.py` → q6 v13 `f6e3b9d0…`;
`deploy/deploy_ampere.py` → ampere v9 `d4f4b030…`. Each deployer also keeps a
commented history list of prior tag→digest for humans.

**v2.3 rebuild (COMPLETE on tr4, 2026-10-11).** tr4 had never built these
images → the whole set compiled from scratch at **32 jobs** (sequential — two
32-way CUDA compiles would OOM 62 GB; the ampere fork compile's CUDA
template-instances stage was the long pole). All digests captured, re-pinned
here and in the five deployers; the derivative `FROM`s point at the new base
digest. That set is now superseded by v2.4 above.

---

## 5. Idle/heartbeat self-shutdown watchdog (full history + design)

The core problem: an idle Salad group keeps **billing**. The watchdog
(`docker/idle_watchdog.py`, stdlib-only, baked into every image, runs inside the
container) stops the group when there's been no work. Version history — the
evolution was driven by real paid-test failures:

- **v1** — exited the container on timeout. **DEFECT:** under Salad, a container
  exit just makes Salad **reschedule a new instance** (group stays `running`),
  so it became a restart loop that *burned* ~6 min of 3090 per cycle. `restart_policy=never`
  governs container restarts *within* an instance, **not** group-level
  rescheduling. **Never arm a group with v1.**
- **v2** — the fix that works: arms on `http://127.0.0.1:9999/ready` (Salad's
  own readiness probe, `READY_PORT=9999` default), holds `IDLE_ARM_GRACE` seconds
  after ready, and on timeout **POSTs the Salad stop API**
  `POST /organizations/{org}/projects/{proj}/containers/{name}/stop` with
  `Salad-Api-Key: $SALAD_STOP_KEY` (retries, key never logged) and **only then**
  SIGTERMs. **Paid test PASSED 2026-10-09:** `atx-hb-test2` self-stopped **165 s
  after ready** with zero connections, zero manual action.
- **v2.1** — key obfuscation: deployers store `SALAD_STOP_KEY=b64:<base64>`; the
  watchdog decodes the `b64:` prefix at startup. **`none`/missing/undecodable key
  DISABLES the watchdog cleanly** (it exits immediately, feature off) — the v2
  "SIGTERM-only fallback" was removed because a keyless self-exit gets rescheduled
  and bills, so arming keyless is pointless.
- **v2.2** — v2.1 + the **SSH-presence guard** (§3).
- **v2.3** — the guard's **hard cap** (`SSH_GUARD_MAX_HOLD`, default **1200 s =
  20 min**; `0` = uncapped, pre-v2.3 behavior). User's billing-disaster
  rationale: you can lose internet access *while SSH-logged in* — with `/.ssh`
  present the v2.2 guard would hold the stop **forever**, so a dead SSH session
  = an unbounded billing run. v2.3: the guard still holds, but the hold branch
  **no longer resets `last_activity`**, so `idle_s` measures true *continuous
  idle since the last real token activity*; when `idle_s >= SSH_GUARD_MAX_HOLD`
  the watchdog logs `ssh guard /.ssh present but idle …s >= max hold 1200s —
  stopping the group anyway` and stops **regardless of `/.ssh`**. Any real
  activity (token counters move / a request is processing) resets the whole
  clock — an active 25-min session is never killed. Hold lines every 300 s
  while held: `ssh guard /.ssh present — stop held off (idle …s, cap 1200s)`.
  Current.

**The stop key is the account-wide key, injected into the container.** Key-scope
discovery: Salad has **no** per-group scoped keys (the docs say "each user has a
unique API token that grants API access to any Organization, Project, or
Container group"). The user chose (via a question) to put the **account key
inside the container** — user-approved. It rides in env as `b64:<base64>`.
`SALAD_STOP_KEY=none` = self-stop disabled.

**PID-1 supervisor requirement** (why the kill works at all — see §9 "PID 1"):
all four images run `llama-server` as a **trapped CHILD** of the CMD bash (PID 1),
not `exec`'d:
```sh
… & LLAMA_PID=$!; trap 'kill -TERM "$LLAMA_PID" 2>/dev/null; exit 0' TERM INT; wait "$LLAMA_PID"; exit $?
```
The watchdog's `kill(1, SIGTERM)` lands only because PID 1 installed a handler;
bash then kills the server and exits with the server's exit code. The CMD is the
plain 3-token exec form `CMD ["/bin/bash","-l","/opt/llama.cpp/entry.sh"]`
(entrypoint extracted to `docker/entry_{base,generic,q6,ampere}.sh` after the
malformed-JSON-CMD **exit-2 crash-loop** was fixed in `1bc86fd`).

**Env knobs** (from the watchdog header): `IDLE_SHUTDOWN` = `none` (default, off)
| `idle` | `heartbeat`; `IDLE_TIMEOUT` (idle seconds); `HEARTBEAT_TIMEOUT` (no-ping
seconds, default 300 now); `IDLE_GRACE` (max cold-start window before arming —
use **3600** for big-quant groups, default 1800); `IDLE_ARM_GRACE` (hold after
`/ready`, default 150 s); `SSH_GUARD` (1/0); `SSH_GUARD_FILE` (`/.ssh`);
`SSH_GUARD_MAX_HOLD` (v2.3, default **1200 s**; `0` = uncapped);
`SALAD_STOP_KEY` (`b64:…` / `none`); `SALAD_ORG`/`SALAD_PROJECT`/`SALAD_GROUP`;
`READY_PORT` (9999). Scrape period is adaptive: `min(30, max(timeout/3, 5))` s.
**Deployers** send the full idle env (`--idle-shutdown/--idle-timeout/
--heartbeat-timeout/--idle-grace/--ssh-guard`) — always, since env is
wholesale-replaced (§9). `--stop-key <file>` reads the account key from a file,
never prints it; `redact()` masks HF_TOKEN + SALAD_STOP_KEY in output.

**Client heartbeat** (the thing that keeps an *active* session from idling out):
`cl_salad` / `chat_salad.sh` send a 1-token completion through the gateway every
~30 s. **`SALAD_HEARTBEAT=1` is now the default** (opt out `=0`; harmless on
watchdog-less groups). Keep `HEARTBEAT_TIMEOUT` ≥ ~2× the 30 s pinger.

**Cold-start defect + fix (commit `b5aa1db`):** a 27B cold start took ~37 min;
the baked `IDLE_GRACE=1800` expired before `/ready`, so the watchdog gave up
unarmed and the group ran the whole session with self-stop impossible. Fix:
`--idle-grace` deployer flag (default 1800; **3600** for big quants) sends
`IDLE_GRACE` in env. **atx-hb-test2 carries `IDLE_GRACE=3600`** (see §2).

---

## 6. Bandwidth arbitrator (`manage_groups.py --min-bw-mbps`)

During the HF model download (10–30 GB GGUFs on slow residential nodes can take
hours), if median bandwidth is below a cap, relocate the instance to a faster
node. User-set constraints (verbatim intent): **never stop/SIGTERM** (a row of
slow nodes would create a stop/start billing loop) — only
**`POST …/instances/{id}/reallocate`** (202; the node is excluded from the
account pool ~48 h, so each shot is a genuinely new host); **max 3 shots, then
keep the 4th** and let it finish. Cap via `--min-bw-mbps` (default 30). The
container only **reports** bandwidth (`docker/bw_reporter.py` samples
`MODEL_DIR` size every 10 s → `/tmp/llama-api/bw.log`); all verdicts live in the
manager. The logic exists **only when the group is started via
`manage_groups.py`** (the orchestrator reads the log over SSH and decides).

**Constants** (`manage_groups.py`, ~line 114): `BW_POLL_INTERVAL=15`,
`BW_RAMP_GRACE=60` (skip first minute), `BW_VERDICT_WINDOW=120` (median over
last 2 min), `BW_MIN_SAMPLES=6`, `BW_MAX_SHOTS=3`, `BW_SSH_TIMEOUT=20`,
`BW_SSH_STRIKES=4`, `BW_ALLOC_TIMEOUT=600`, `BW_PULL_TIMEOUT=900` (stuck pull =
a shot), `BW_LOG_FROZEN=180`, `BW_TAIL_LINES=80`, `BW_LOG_PATH=/tmp/llama-api/bw.log`,
`BW_SSH_GUARD_FILE=/.ssh`. The SSH probe is **one plain command** (`tail -n 80
/tmp/llama-api/bw.log` — Salad SSH is a shell-less OCI exec, §9). Guard:
`_bw_ssh_guard()` (§3) — holds any shot while `/.ssh` is present, fails open
otherwise. Since b9eae88 (2026-10-11) "present" means an **exact `/.ssh` line
on stdout, after the ls error markers are checked on the combined
streams** — the relay's stderr-into-stdout merge (§9f) made the original
substring check match its own error text and hold forever. Requires an image with `bw_reporter` (base v8+/generic v8+/q6 v10+/
ampere v6+; current guard set all have it). Offline harness:
`tmp/test_arbitrator_offline.py` + `tmp/fakebin/ssh`, run with `python3 -I`.
**Naming:** it is the **arbitrator** (the user rejected "arbiter").

---

## 7. llamAmpere fork = production ATX

The **llamAmpere** v0.4 fork bakes **turbo5/turbo4 KV** into the CMD. It works on
Salad 3090s and — **user-confirmed 2026-10-07** — runs **faster** than the q8_0
KV baseline (the A/B goal of the fork is met). Why it matters: turbo KV costs
≈16 KiB/token (~45% of q8_0's ~35 KiB), so each GiB of VRAM headroom buys ~60 K
ctx instead of ~30 K — on a 3090 the full **132768** ATX context fits Q5_K_M
(~21 GiB total).

**Deployer split (commit `0271252`, user-mandated):** the fork gets its own
`deploy/deploy_ampere.py` (ATX production profile as built-in defaults — fork
image, ctx 132768, ATX `EXTRA_ARGS`; `--gpu` **limited to sm_86 30-series**
rtx3090/3090ti/3080/3080ti — the fork ships sm_86+89 cubins only, **no rtx5090**).
`deploy/deploy_generic.py` was reverted to the **vanilla** `lmss_generic` image
(`EXTRA_ARGS="none"`) and its `--gpu` takes **any** card the org lists. rtx3080/
3080ti are in the ampere list for sm_86 capability but **can't hold the 27B
default quant** (10/12 GB) — only 24 GB cards do. **New ATX 27B groups deploy via
`deploy_ampere.py`, not the vanilla generic.**

**Prod group** `atx-swift-27b-q5-opt` (ma-casa-in-paris/llm, alias
`atx-swift-27b-opt`): user-tested OK 2026-10-07, then stopped. **Pending verify
at next start:** scrape `/metrics` for `llama_model_n_ctx` (expect **132864** =
132768 rounded up to a 128-block). The spec CTX is 132768 (confirmed in §2).

**Image history** (all tags except current are deleted from Hub; deployer
comments are reference-only): v3 `7b5882f9` (the exec-form `entry.sh` CMD fix —
v2 `e9cae924` was a crash-loop, **do not deploy**), v4 `2df98d5b` (watchdog v2,
live-tested), v5 `58db9d23` (watchdog v2.1), v6 `ceb9059f` (watchdog v2.1 +
bw_reporter/ping/llama-stats, commit `91ce6c7`), **v7 `314f77b8` (current,
guard v2.2)**.

---

## 8. Model & chat-template gotchas

**Qwen3.8-27B is a hybrid** (only every 4th layer is full-attention). Claude Code
drives it via llama.cpp. The stock Qwen Jinja template **raises** on any
non-first system message (`raise_exception('System message must be at the
beginning.')`); Claude Code sends system context mid-conversation, so the
template must be **patched** (one line: replace that `raise` with an inline
render of the system message). Reference pair:
`/dd2/andrei/bench/llama.cpp/qwen3.8.q6.jinja.orig` → `…q6.jinja.claude`.
**Dump-first rule (user):** dump the template from the *exact* GGUF being served
(`gguf_dump.py --no-tensors --json <gguf> | jq -r '.metadata["tokenizer.chat_template"].value'`,
in the `.venv` under `/dd2/andrei/bench/llama.cpp`), then patch the one line.
The q6 image **dumps + patches the Claude template at startup**
(`docker/claude_template.sh`; `CLAUDE_TEMPLATE=1` → `--chat-template-file`,
`none` = embedded template / plain-curl mode).

**ROOT CAUSE of the "repeated path" bug (confirmed 2026-10-03):** the baked image
CMD passed the template **file path** to llama.cpp's **inline** `--chat-template`
flag (which takes template *text*) instead of **`--chat-template-file`** — so the
path string became the entire prompt and the model degenerated into a loop
echoing it. One word fixed it: `--chat-template-file`. The template content was
never the problem; it just had to actually be loaded. Verified live on Salad
(canary `27*43`→`1161` = no loop; mid-conversation system message → 200
`end_turn`).

**MTP requirement:** the image hardcodes `--spec-type draft-mtp`, so the served
GGUF must contain the MTP/nextn layers — the `noMTP-*` files crash the server.
(`USE_DRAFT_MODEL=none` means use the GGUF's *embedded* MTP head.)

**3090 ctx math:** `(23.0 GiB − weights − ~1.1 GiB overhead) / ~35 KiB` ≈ max ctx.
That's why prod 27B runs **Q5_K_M @ 90 K** (`n_ctx` = **90112**, llama.cpp
rounding), not Q6_K (Q6_K @ 90 K needs ~3.1 GiB KV, only ~1 GiB headroom). A 32 GB
worker (5090) could serve full Q6_K at ~120 K+.

**Double-`think` echo on ATX groups (fixed 2026-10-09):** with
`enable_thinking=false` (what `salad_proxy` sends) the template ends the prompt
with the CLOSED empty block `think\n\n/think\n\n` as the generation prefix; the
model echoes it; llama-server strips think blocks **only** with its default
reasoning parser — but ATX groups' `EXTRA_ARGS` carried `--reasoning-format none`
(copied from the ATX post.txt), so the echo passed through. q6 groups use the
default parser → stripped → "didn't have that before." Fixes: (1)
`claude/salad_proxy.py` **`EmptyThinkStripper`** removes *empty* think blocks from
response text (streaming state machine + non-streaming regex; real non-empty
blocks pass through so `--thinking 1` still works); (2) `deploy_ampere.py` /
`deploy_generic.py` **`strip_none_value_args()`** drops `--flag none` **pairs**
from EXTRA_ARGS before the env is sent (whole-string `EXTRA_ARGS="none"` is
preserved — the API rejects empty env). This is why the prod `…-opt`
`EXTRA_ARGS` in §2 no longer has `--reasoning-format none`.

**Known model quirk (not plumbing):** with the template applied, `/v1/messages`
`tool_choice:auto` sometimes returns an empty tool `name` because the 27B
confabulates Claude's own tool names (WebSearch/WebFetch) from training data;
forced `tool_choice` and `/v1/chat/completions` round-trip correctly.

---

## 9. Salad platform gotchas (the hard-won part — most valuable to remember)

These are platform behaviors that repeatedly cost time. `save()` each to mycelium.

**9a. Worker image cache is REPO-NAME-keyed; digest-pinning is the airtight lever.**
Salad workers serve a stale image if the repo name is already cached, **regardless
of tag** (a brand-new never-cached tag still served stale). `image_caching` is
not PATCH-writable. A **repo rename** only works if NO worker has ever pulled the
new name (it gets burned on the first start cycle). The one thing that reliably
forces a fresh pull: **PATCH the group to a digest-pinned ref**
`boris271142/lmss@sha256:<digest>` that no worker has seen — the API accepts it
verbatim. **Rule:** to ship an image change, build a NEW tag (never overwrite),
push, capture the digest from the push output, PATCH the group to the digest.
Verify the LIVE build in-container via the SSH relay + `version.sh` (the build id
in `/etc/llama-image-build` is the reliable discriminator).

**9b. Stuck-start signature:** `status=deploying` with `raw['instances']=None`
for 30+ min = the instance was **never placed** (capacity/placement), NOT a slow
download (a download populates `instances`). Remedy: stop (202) → fresh start.
Don't burn a 30-min poll.

**9c. Group DELETE leaves a sticky NAME tombstone.** Recreating a just-deleted
group name 400s `name_conflict` for 24 h+ even after the GET is 404 and it's gone
from the list (there's NO API endpoint to list projects / org-wide groups).
**Workaround:** create under a **fresh name** (suffix with the distinguishing
change, e.g. `-q6k`), and keep the served **`MODEL_ALIAS`** stable via env so
clients are unaffected. Do NOT write a retry loop against `name_conflict` — it
doesn't self-clear on the minutes scale. (Group-name regex is LOWERCASE-only:
`^[a-z][a-z0-9-]{0,61}[a-z0-9]$`.)

**9d. Group `running` ≠ instances ready.** `current_status` flips to `running`
while instances are still allocating/downloading/creating. The counts
(`allocating/creating/running/stopping_count`) have **no `ready_count`**. The
real signal is per-instance: `GET …/instances` → each instance's boolean
`ready`. Group ready = `running` AND ≥1 instance `ready=true`; before the first
instance the list can be empty while the group already shows `running` — keep
polling. `manage_groups.py wait` implements exactly this.

**9e. PATCH env is WHOLESALE replace, not merge.** `container.environment_variables`
is replaced — deployers must always send the **full** env dict. A new key lands
fine. On a **previously-started** group, a PATCH parks in
`pending_update_in_progress` and **blocks further PATCHes (HTTP 400)** until the
group is started (a paid action). There's **no cancel-pending endpoint**. A
never-started group applies PATCH instantly. **Image-only PATCH** (send
`image=` and omit env) merges into `container` and leaves env **byte-identical**
— the zero-cost way to re-pin a fleet (verified on all 7 groups). **Eventually
consistent GET:** a GET right after a PATCH can return the PRE-PATCH spec;
re-read before trusting; `pending_change=True` = "staged, not yet visible,"
flips to False when the new spec reads back.

**9f. SSH into a running container is a shell-less OCI exec.** `ssh root@<ip> -p
<port>`; the command is `exec`'d directly, **not through a shell** — no pipes,
`;`, quotes, or `$()`. `ssh h 'hostname; ps'` fails with `exec: "hostname;":
not found`. Pass **ONE plain command** with plain args (`ps aux`, `env`,
`tail -40 /tmp/llama-api/watchdog.log`). **The relay exits 1 even on SUCCESS** —
`ls /tmp/llama-api` printed the full listing and exited 1; a missing file also
exits 1. Only ssh transport failures exit **255** (those errors are the local
ssh client's own and stay on the LOCAL stderr). So **classify by payload,
never by exit code**. **The relay also MERGES the remote process's stderr into
the client's STDOUT** (live-probed 2026-10-11: `ls /.ssh` on an absent file →
client stdout = banner + `ls: cannot access '/.ssh': No such file or
directory`, client stderr EMPTY). Classify on the **combined** stdout+stderr:
samples ⇒ success; `cannot open`/`cannot access` in EITHER stream ⇒ absent;
empty combined output ⇒ connection problem. The `Connecting to container
<uuid>` banner lands on **stdout** (filter when parsing). This bit the
arbitrator twice: its `returncode==0` gate turned four good reads into "bail"
(b418ba3), and the guard's `"/.ssh" in p.stdout` substring check matched ls's
own error text (delivered on stdout via the merge, and containing the guard
path) — a false "present" and a zero-shot hold loop on the live drop-test
(b9eae88; the offline suite's absent-fixture modeled the error on stderr and
missed it until corrected). Filter secrets on the **local** side of the ssh
pipe (`| grep -v hf_`), never remotely.

**9g. Transitional API GETs can return an EMPTY container object.** While a group
is mid-start (`current_status: None`), a management-API GET can return
`environment: {}`, `restart_policy: None`, `pending_update_in_progress: None`.
That's a **transitional read, not a spec wipe** — the live container env (via SSH)
showed the full parked spec while the API said env was empty. Never conclude spec
state from a transitional GET; re-read after `running`, or read the container.
Also: `current_state.description` (e.g. "Pulling 4%") can be **stale leftover**
from an interrupted start — judge liveness by `current_state.status` +
`instance_status_counts` (all 0 = not live), never the description string.
`status` lives under `current_state.status`, not top-level.

**9h. PID 1 drops ALL in-namespace signals (incl. SIGKILL).** Verified in-container:
`os.kill(1, SIGKILL)` from inside a container returned success while it kept
running — the kernel discards every signal (SIGKILL included) sent from inside a
PID namespace to PID 1 **when PID 1 has no handler**. `docker stop` works only
because dockerd signals from the parent namespace. **So an in-container watchdog
can never kill a bare `exec llama-server` PID 1** — it must run the server as a
trapped CHILD of the CMD bash (§5). Never `exec` the server in a container that
must be self-killable.

---

## 10. Multi-org & projects

The account has a **second org `akl-on-salad`** (created 2026-10-04) alongside
production `ma-casa-in-paris`. **All orgs share the same Salad API key** — org is
just a path parameter in `/organizations/{org}/…`. All five deployers take
`--org` (default `ma-casa-in-paris`), `--project`, and `--group` (the group name
also feeds the `NAME` env); bare runs are byte-identical to pre-flag behavior.
`cl_salad` had a `SALAD_ORG` env override.

**Projects CANNOT be created via API** — `POST /organizations/{org}/projects`
and the list `GET` both 404 (endpoint doesn't exist); the client's `create_project`
is dead-letter best-effort (404 tolerated — "using existing project"). **Projects
must be created in the SaladCloud web UI.** 404 shapes disambiguate:
`{"type":"project_not_found_exception"}` = path valid, project missing; plain
RFC-9110 404 = endpoint doesn't exist. User-created projects: **`llm`** in
`ma-casa-in-paris` (next to prod `qwen38-27b`) and **`comfy`** in `akl-on-salad`
(next to the built-in lowercase `default` — the UI "Default" label is rejected by
the lowercase-only regex). `ma-casa-in-paris` has no `default`.
To deploy elsewhere: the project must already exist (web UI), then
`--org X --project Y --group Z [--no-start]`.

---

## 11. Docker Hub rules

**Standing rule (user, 2026-10-09): every push must leave the repo's
"repository overview" set** — a short line of what the image is for plus
`https://github.com/suedenglehem/docker4salad/blob/main/docker/README.md`, on
**every** `boris271142/lmss*` repo (and any new repo created by a push). Do it
via the Hub API: `PATCH https://hub.docker.com/v2/repositories/boris271142/{repo}`
with JWT (login `POST …/v2/users/login` using the creds in
`~/.docker/config.json` auths, **never printed**), body
`{"full_description": "…"}`. **The field is `full_description`** — `description`
is the 100-byte short blurb and 400s on longer text. Verify with a GET (check the
URL is present). New repos start with an empty overview.

**Registry hygiene:** older tags were deleted on Hub (only the current tag per
repo remains); the deployers' history digests are now **reference-only** (not
pullable).

---

## 12. Repo map / key files

```
on_salad/
├─ README.md                     top-level overview + deployer-flag table
├─ salad_client.py               (875 ln) Salad REST client. UpdateContainerGroupRequest
│                                 is a merge-patch (unset fields omitted; env, when
│                                 present, replaced wholesale). get_container_group(
│                                 …).raw = full group dict. update_container_group(…).
├─ deploy/
│  ├─ deploy_qwen38_27b.py       (633) 27B vanilla/q6 profile; pinned q6 v11.
│  ├─ deploy_qwen38_9b.py        (452) 9B; base v9. **CREATE-ONLY** (refuses existing group).
│  ├─ deploy_qwen9b.py           (415) 9B (base v9); the other 9B deployer.
│  ├─ deploy_generic.py          (697) vanilla any-model; generic v9; --gpu any card.
│  ├─ deploy_ampere.py           (725) llamAmpere ATX prod profile; ampere v7; sm_86 only.
│  ├─ salad_api.txt              the Salad API key (ACCOUNT key; all orgs). NEVER print.
│  └─ hft.txt                    HF token. NEVER print.
├─ docker/
│  ├─ Dockerfile.multistage      base image build (context = docker/).
│  ├─ Dockerfile.lmss_generic    generic; FROM base v9 @digest. (context = repo root)
│  ├─ Dockerfile.lmss_q6_mtp_vision   q6 baked; FROM base v9 @digest. (repo root)
│  ├─ Dockerfile.lmss_generic_ampere llamAmpere fork. (repo root)
│  ├─ entry_{base,generic,q6,ampere}.sh  startup scripts (trapped-child tail; §5/§9h)
│  ├─ idle_watchdog.py           (427) self-shutdown watchdog v2.2 (§3/§5).
│  ├─ bw_reporter.py             (124) in-image bandwidth sampler → /tmp/llama-api/bw.log.
│  ├─ claude_template.sh         startup dump+patch of the Claude chat template.
│  ├─ qwen3.8.q6.jinja           the (patched) Qwen template.
│  ├─ version.sh                 prints the image build id / generation fingerprint.
│  ├─ api_app.py / run_api.py    the Anthropic-only proxy app.
│  ├─ llama_stats.py / stats.sh  /metrics helpers.
│  └─ README.md                  (51 KB) the big image/deploy doc (linked from Hub).
├─ claude/
│  ├─ salad_proxy.py             (698) Anthropic-only proxy + EmptyThinkStripper (§8).
│  ├─ cl_salad                   Claude Code → Salad launcher (heartbeat default ON).
│  ├─ chat_salad.sh              curl-based chat client (URL positional arg).
│  ├─ cl_salad_deploy            deployer wrapper (auto-exports SALAD_HEARTBEAT).
│  └─ curl_salad.sh / cube*      curl helpers + a small CUDA experiment (untracked).
├─ utils/
│  ├─ manage_groups.py           (942) list/start/stop/wait/stats + bandwidth
│  │                              arbitrator (§6). --min-bw-mbps.
│  ├─ billing.py                 cost tracking.
│  ├─ llama_stats.py             /metrics scrape helper.
│  └─ portal_vault.gpg           a gpg key (vault). Treat as secret.
├─ docs/
│  ├─ README.md                  (28 KB) how-to / architecture doc.
│  ├─ container_group_create.md  Salad create-group API reference (schema + status enum).
│  ├─ salad/                     scraped Salad docs (api-usage.mdx etc.).
│  └─ SaladCloud API Knowledge Setup for Cline.md
├─ .clinerules/salad-api.md      Salad API notes for the Cline agent.
├─ tests/                        one-off curl/run scripts (curl1.sh, curl2.sh, run_*.sh).
├─ llama.cpp/                    vendored llama.cpp checkout (submodule-ish, 31 dirs).
└─ tmp/                          (gitignored) scratch + OFFLINE TESTS:
   ├─ test_arbitrator_offline.py  23/23 arbitrator scenarios (+ tmp/fakebin/ssh shim).
   ├─ test_watchdog_offline.py    4/4 watchdog guard scenarios.
   ├─ test_deploy_offline.py      deployer dry-run checks.
   ├─ fleet_status_now.py         read-only fleet status+guard snapshot (§14).
   ├─ fleet_snapshot.py           broader read-only snapshot.
   ├─ patch_images_v9.py          image-only PATCH of the 7 groups (the guard re-pin).
   ├─ patch_9b.py / patch_qwen38_9b_rescue.py   9B group PATCH helpers.
   └─ smoke_test_{start,stop}_cg.py   group start/stop smoke helpers.
```
Key file line counts (2026-10-10): `manage_groups.py` 942, `salad_client.py` 875,
`salad_proxy.py` 698, `deploy_ampere.py` 725, `deploy_generic.py` 697,
`deploy_qwen38_27b.py` 633, `deploy_qwen38_9b.py` 452, `deploy_qwen9b.py` 415,
`idle_watchdog.py` 427, `bw_reporter.py` 124.

**Untracked in git** (both boxes): `claude/cube`, `claude/cube.cpp` (a local CUDA
experiment — not part of the deploy pipeline).

---

## 13. Secrets — WHERE they are (NEVER the values)

Do **not** print or commit any of these. Reference by path only.

- **Salad API key (account-wide, all orgs):** `deploy/salad_api.txt` (also
  `claude/salad_api.txt`, `utils/portal_vault.gpg`). Used as the `Salad-Api-Key`
  header by `salad_client` / deployers, and injected into containers as
  `SALAD_STOP_KEY=b64:<base64>` for the watchdog's stop call.
- **Hugging Face token:** `deploy/hft.txt`, `docker/hft.txt`; rides in every
  group's env as `HF_TOKEN` (for gated model downloads). When reading group env,
  redact it; when SSHing, filter on the **local** side (`| grep -v hf_`).
- **Docker Hub credentials:** `~/.docker/config.json` `auths` (base64). Never
  printed; used for `docker push` + the `full_description` PATCH (§11).
- **`SALAD_STOP_KEY`:** the account key, base64-obfuscated, in group env. The
  deployer `redact()` masks HF_TOKEN + SALAD_STOP_KEY in any output it prints.
- **How to inspect presence without printing:** over SSH,
  `grep -c -a SALAD_STOP_KEY /proc/1/environ` (counts presence, doesn't dump it).

---

## 14. Commands cheat-sheet

```bash
cd /dd2/andrei/docker/on_salad

# READ-ONLY fleet status + guard posture (no writes, no cost):
python3 -I tmp/fleet_status_now.py

# List / wait / stats groups (key from deploy/salad_api.txt, never printed).
# The 2nd positional is the ORG (ma-casa-in-paris or akl-on-salad) — `llm` is a
# PROJECT, not a valid 2nd positional. The group is picked INTERACTIVELY
# (# or exact name) at the prompt, then y/N-confirmed for start/stop/delete:
python3 utils/manage_groups.py list  ma-casa-in-paris
python3 utils/manage_groups.py wait  ma-casa-in-paris     # polls per-instance `ready` (§9d)
python3 utils/manage_groups.py stats ma-casa-in-paris

# PAID start (burns credits — coordinate with the user). Add the arbitrator:
python3 utils/manage_groups.py start ma-casa-in-paris --min-bw-mbps 30   # bandwidth-aware start (§6)
# non-interactive variant (pipe the prompt answers: name, confirm):
printf 'atx-hb-test2\ny\n' | python3 utils/manage_groups.py start ma-casa-in-paris --min-bw-mbps 30

# Deploy / zero-cost re-pin (deployers PATCH the full env; applies at next PAID start):
python3 deploy/deploy_ampere.py     --project llm --group atx-swift-27b-q5-opt --no-start \
    --idle-shutdown heartbeat --stop-key deploy/salad_api.txt --idle-grace 3600
python3 deploy/deploy_qwen38_27b.py --project qwen38-27b --group qwen38-27b-q5 --no-start
# NOTE: qwen38-9b's deployer is CREATE-ONLY — PATCH it via salad_client.update_container_group
#       (see tmp/patch_9b.py / tmp/patch_qwen38_9b_rescue.py).

# Image-only fleet re-pin (env untouched):  python3 -I tmp/patch_images_v9.py  (--dry-run ok)

# Build + push an image (primary box, 24-way). Base first, then derivatives:
docker build -f docker/Dockerfile.multistage        docker/  --build-arg BUILD_JOBS=24 -t boris271142/lmss:cuda128-vN .
docker build -f docker/Dockerfile.lmss_generic       .       --build-arg BUILD_JOBS=24 -t boris271142/lmss_generic:cuda128-vN .
# (derivatives' context MUST be repo root — they COPY docker/version.sh)
docker push boris271142/<repo>:<tag>     # then PATCH the repo full_description (§11)

# SSH into a RUNNING container (get ip/port from `manage_groups list` / /instances):
ssh root@<ip> -p <port> 'tail -40 /tmp/llama-api/watchdog.log'   # ONE plain cmd, no shell (§9f)
# the GUARD:  touch /.ssh  (when you log in)   /   rm /.ssh  (when you leave)

# Offline tests (python3 -I to avoid site shadowing):
python3 -I tmp/test_watchdog_offline.py
python3 -I tmp/test_arbitrator_offline.py
python3 -I tmp/test_deploy_offline.py
```

**Verify a live image in-container** (over the SSH relay): run `version.sh` (the
build id in `/etc/llama-image-build` is the reliable discriminator — §9a), and
check `llama-server` argv + `/models` contents.

---

## 15. Pending / next steps (USER-GATED — paid actions)

Items 1–2 need a **paid** Salad start (burns credits) — the user decides when.
2026-10-11: the approved guard smoke (item 1) is **FINISHED** — `atx-hb-test2`
stopped at 01:15Z; all 7 fleet groups are now stopped at $0.

> **BACKEND NOTE (correction, 2026-10-11):** this Claude session is served by
> the **local vllm on tr4** (`qwen3.8-27b` fp8, `:8000`, TP2 + MTP, running
> since Oct 10 — verified via `ps`), **not** by any Salad group. Conversation
> turns never touch a group's watchdog clock, and the session does not go dark
> when groups stop. (Earlier text claiming the session was "served by
> atx-hb-test2" was a model-name conflation: local `qwen3.8-27b` vs the group's
> `atx-swift-27b-opt` alias.)

1. **Paid guard smoke test — v2.3** (proves the hold *and* the 20-min cap
   end-to-end). **Status: COMPLETE (2026-10-11) — FULLY PASSED,
   user-confirmed: the user did NOT stop the group manually; the 01:15 stop
   was the watchdog's own cap-kill ("watchdog cap-kill worked").** The v2.3
   set is built + pushed + re-pinned (tr4 from-scratch
   rebuild at 32 jobs: base v10 → generic v10 → q6 v12 → ampere v8, all
   digest-verified, §4); `atx-hb-test2` was image-only PATCHed to the ampere
   v8 digest, **paid-started (user-approved)**, `ready` 00:25Z, user touched
   `/.ssh` 00:27Z.
   - **Hold half — CAPTURED 01:01:45Z** (verbatim): `ssh guard /.ssh present —
     stop held off (idle 420s, cap 1200s)` — exactly in the expected
     T_ready_api + 450–510 s band.
   - **Cap half — TIMING EVIDENCE ONLY.** Last real inference 00:54:45Z
     (watchdog log `idle` counter + activity score 1715); 00:54:45Z + 1200 s
     cap = 01:14:45Z; first 30 s poll after the deadline = 01:15:14Z. API
     state: `running` at 01:14:27Z → `stopped (no instances)` at 01:15:28Z.
     I.e. the group stopped **within one poll interval of the exact
     cap-kill second**, with `/.ssh` still present — consistent with the
     watchdog's cap-kill, and with nothing else that could have stopped it
     being in flight (this session runs on local vllm, see note above).
     The idle clock is `time.monotonic()` (idle_watchdog.py:421/435), so a
     wall-clock shift cannot fast-forward it.
   - **Verbatim cap-kill line MISSED and why:** the relay-log watcher
     (task btzme29eu → `tmp/smoke_capkill_capture.log`) was actually armed at
     01:42:11Z — ~27 min AFTER the 01:15:14Z event — because background-task
     launches can be delayed in this environment (its first poll already saw a
     stopped group; the container, with its `watchdog.log`, is torn down on
     stop). Lesson baked in: arm capture watchers **before** the event window
     and verify their "armed" timestamp, never trust the assumed launch time.
   - **RESOLVED (user, 2026-10-11):** "watchdog cap-kill worked" — the stop
     was the watchdog's own cap-kill, not a manual GUI stop. The smoke is
     fully closed: hold verbatim + cap-kill behavior confirmed on a live paid
     run. (The verbatim kill *log line* itself remains uncaptured — timing
     evidence to the second + user confirmation stand in for it. If a future
     re-smoke is ever wanted, arm the capture watcher from t=0: background
     launches can be delayed here, so verify the watcher's "armed" timestamp
     before trusting it.) Offline suites — watchdog 7/7, arbitrator 23/23 —
     green.
2. **Paid arbitrator live test:** `python3 utils/manage_groups.py start
   ma-casa-in-paris --min-bw-mbps 10000` (atx-hb-test2, user-run) — watch it
   `POST /reallocate` on a slow download (≤3 shots). §6. **Status 2026-10-11
   — attempt 1 hit the guard false-positive, now fixed; user re-running.**
   Attempt 1 (started ~03:54Z, cap 10000): acquired ccf9203e on node
   0bf20a53 03:58:28Z; slow verdict ~04:01:27Z, but the guard reported
   `/.ssh present` (FALSE — the relay merged ls's own error text onto stdout;
   §9f) → infinite `ssh session active (/.ssh present) — holding, no shot
   spent`, ZERO shots. The node finished the download anyway (READY
   04:03:01Z, per the read-only watcher) and the run exited 0 "download
   complete" — the run's one reallocation opportunity was exactly the shot
   the bug ate. Fix b9eae88 committed+pushed (offline 24/24; the live
   scenario pinned as the regression). Rerun (user): fresh process — the
   running one held stale code in memory — and a fresh 3-shot budget; node
   0bf20a53 was never excluded (no shot ever spent). Known structural limit
   (explained to user, accepted): the `ready` check runs on every 15 s poll
   BEFORE verdict eligibility, so a fast node (~2 min download) finishes
   first regardless of cap — the 3-drop demo needs a slow landing node.
   Drop-log capture: user pastes the terminal section when the run ends;
   verify ≤3 `— POST reallocate (shot N/3)` lines + the final node.
3. **Verify prod ATX context:** at the next start of `atx-swift-27b-q5-opt`,
   scrape `/metrics` and confirm `llama_model_n_ctx = 132864` (spec CTX is 132768).
   §7.
4. **DONE (2026-10-11):** the v2.3 set (base v10 / generic v10 / q6 v12 /
   ampere v8) is re-pinned across the fleet — all 7 groups image-only PATCHed
   (env asserted byte-identical before/after, no starts;
   `tmp/patch_images_v10.py`). The first paid start of each group runs v2.3.
5. **DONE (2026-10-11) — v2.4 shell-ergonomics cycle (build + push + re-pins
   + fleet re-PATCH, all verified).** Root cause: the Salad ssh relay execs a
   bare `/bin/bash` on a pty with `TERM=xterm` (breaks nvtop/htop: "Error
   opening terminal: unknown") but DOES read `/root/.bashrc` (verified live:
   skel PS1/HISTCONTROL/`ll` all loaded). Fix baked in the image's final
   stage: `export TERM=xterm-256color` + aliases `nv/ht/gpu/wlog/alog/ready/
   als/ll/la/l/h` appended to /root/.bashrc in `Dockerfile.multistage` and
   `Dockerfile.lmss_generic_ampere`; generic/q6 inherit via their FROM
   re-pin. `tmp/build_push_v24.sh` (32 jobs, sequential: base cuda128-v11 →
   generic cuda128-v11 → q6 cuda128-v13 → ampere cuda130-v9) built + pushed
   in **~14 min** (WARM layer cache from the v2.3 cycle — only the .bashrc
   RUN block and later layers rebuilt; corrects §4's "no cache" assumption
   for this build form). Hub digests (digest-verified from push output):
   base `2bfbd69c…`, generic `888fc401…`, q6 `f6e3b9d0…`, ampere
   `d4f4b030…` (full digests in `tmp/build_push_v24.sh` output + the
   deployers). Both derivative FROMs re-pinned mid-flight (match count 1
   each); the **5 deployer IMAGE constants** re-pinned to the new Hub digests
   (old digests kept in the history comments). Fleet re-PATCH **7/7 `200 OK`**
   via `tmp/patch_images_v11.py` (env asserted byte-identical before/after,
   **no starts**). **Propagation is ASYNC**: the immediate re-GET shows the
   OLD digest and a re-PATCH 400s `pending_update_in_progress` even while the
   group GET's `pending_update_in_progress` field is falsy — so verify with
   READ-ONLY GET polling (`tmp/check_images_v11.py`, prints 'ALL LANDED' at
   7/7), never a re-PATCH. Watchdog unchanged (still v2.3); next live benefit
   = nvtop/htop + aliases over the relay on any group's next start.
   `restart_policy`/`priority` are CREATE-ONLY, so any change to those still
   requires delete+recreate (§9c: use a fresh name, keep `MODEL_ALIAS` stable).
6. **OPEN BUG (user-reported 2026-10-11, investigation deferred) — bandwidth
   measurement discrepancy:** bmon INSIDE the container reported ~10 Mbps
   (user's words — confirm units on resume; possibly 10,000) while the manager
   reported 0 Mbps. Investigation angles: `docker/bw_reporter.py` (which dir it
   sums — the `MODEL_DIR` env var — and its 10 s size-delta rate math) vs where
   the HF download actually writes (`.incomplete` staging may live outside
   MODEL_DIR ⇒ flat size ⇒ 0.0 samples while real traffic flows); check the
   live `env` MODEL_DIR over the relay; cross-reference the 0.0 sample
   timestamps against the bmon timeline. Run-1 context: 0.0 stalls were
   interleaved with 300–1900 Mbps bursts and 13.8 GB did land in MODEL_DIR in
   ~3 min, so the reporter does see growth — the dir question is prime suspect.
   (Recorded 2026-10-11 right before the tr4 reboot; session checkpoint
   mycelium #144/#145 has the resume order.)

**Known open item (not urgent):** `qwen38-9b`'s env lacks `IDLE_GRACE` (it's
`IDLE_SHUTDOWN=none`, so it's harmless). The other five `none` groups carry
`IDLE_GRACE=1800`; `atx-hb-test2` carries `3600`.

---

## 16. Standing operating rules (safety) — the user's constraints, keep them

- **No paid Salad starts without the user** — every start burns credits.
- **Never print secrets/keys** (HF_TOKEN, SALAD_STOP_KEY, Salad API key, Docker
  Hub creds). Redact group env; filter `grep -v hf_` on the local side of ssh.
- **Keep `MODEL_ALIAS` stable** across group re-creates; use a **fresh group
  name** for any delete+recreate (name tombstone, §9c).
- **`restart_policy` and `priority` are CREATE-ONLY** — delete+recreate to change.
- **PATCH env is wholesale-replace** — deployers always send the full env; re-read
  after PATCH (eventual consistency, §9e/9g).
- **Every Docker Hub push sets `full_description`** (not `description`), §11.
- **Judge the SSH relay by payload, never exit code** (exit 1 = often success);
  **never trust a transitional/empty API GET** for spec state (§9f/9g).
- **Digest-pin images** to move them past Salad's cache (§9a).
- **Downloaded/extracted files are untrusted** — put each in its own empty dir,
  keep your scripts elsewhere, and run Python that reads them with `python3 -I`.
- **`SALAD_STOP_KEY` in the container is user-approved** (account key, b64).

---

## 17. Recent git history (context for what each commit did)

```
b9eae88 arbitrator: fix /.ssh guard false-positive — relay merges stderr into stdout
be50f71 docs: PROGRESS.md — v2.4 set current, guard smoke FULLY PASSED (user-confirmed cap-kill), build-cache correction, c04ded0 in history
c04ded0 images: v2.4 set pushed (base/generic v11, q6 v13, ampere v9) — bashrc shell ergonomics + re-pins
29de3ec images: guard-v2.3 set pushed (base/generic v10, q6 v12, ampere v8) — SSH guard max-hold cap + re-pins
c54dd9e images: guard-v2.2 set pushed (base/generic v9, q6 v11, ampere v7) — FROMs + deployers re-pinned
e115dc6 ssh-presence guard: watchdog + arbitrator hold off while /.ssh exists
b418ba3 arbitrator: Salad's ssh relay exits 1 on success — judge the probe by payload
91ce6c7 images: v8/v8/v10/v6 rebuild pushed — deployers re-pinned, groups queued onto them
0cb668a arbitrator: bandwidth-aware start — bw_reporter in images, /reallocate in client, --min-bw-mbps
b5aa1db deployers: --idle-grace flag — the watchdog's arming window was unpatchable
ed01407 clients: heartbeat ON by default; proxy strips empty think blocks
c2459f9 idle_watchdog v2.1: b64-obfuscated key storage + key=none disables the watchdog
6610dd8 idle_watchdog v2: arm on /ready + POST group /stop — the only kill that stops billing
1bc86fd docker: exec-form entry.sh CMD — fixes the exit-2 crash-loop in all four images
fe347ab deploy/*.py: --priority flag, default medium (low/batch get preempted under load)
c70dc5e idle_watchdog: adaptive scrape period for small timeouts; deployer pairing note
```
(Older: the multi-org `--org/--project/--group` flags, the ampere/generic deployer
split `0271252`, the `--chat-template-file` path-loop fix, the `--priority`
history, etc. `git log` in either tree has the full record.)

---

*End of knowledge base. If a section here is stale, re-derive it with the
read-only commands in §14 and update this file in place (it is the tr4 mirror's
source of truth until tr4's mycelium is populated).*
