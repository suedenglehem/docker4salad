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
> **Image custody (corrected 2026-10-12):** tr4 built the v2.3/v2.4/v2.5 sets and
> DOES hold them locally; the primary box built up to the v2.2 set. Work moved
> back to the primary box on 2026-10-12 — the v2.5 set was **copied from tr4**
> (`ssh tr4 'docker save … | pigz' | pigz -d | docker load`, §4) rather than
> rebuilt: ~11 s per image on the LAN, and the loaded IDs match the Hub digests
> exactly. All four images + the Hub + both boxes now carry the v2.5 set.

---

## 0. TL;DR — state as of 2026-10-12

- **Work is back on the PRIMARY box** (`i714700k`, `/dd2` on `/dev/sdi1`, 28 CPUs;
  builds here use `--build-arg BUILD_JOBS=24` per the user (raised from 4 on
  2026-10-12)). The v2.5 image set was
  **copied from tr4** rather than rebuilt (`tools/copy_images_from_tr4.sh`:
  `ssh tr4 'docker save … | pigz' | pigz -d | docker load`) — loaded IDs match the
  Hub digests exactly, ~11 s/image. The nproc trap (`nproc`=1) is present on this
  box too. **DONE 2026-10-13 — §15 item 3 (prod ATX ctx) VERIFIED.** The prod group
  was recreated as `llm/atx-swift-27b-q5-med` at **priority=medium** (the old
  `atx-swift-27b-q5-opt` was `priority=low`; priority is CREATE-only, §9k), started
  by the user through the **bandwidth arbitrator** (`--min-bw-mbps 100`, its first
  live run — it reallocated once, §15 item 3), and verified two ways: llama-server
  **argv has `--ctx-size 132768`**, and **`/slots` serves `n_ctx` = 132864**
  (23067/24576 MiB VRAM). Two gotchas fell out of it: the served ctx is on
  **`/slots`, not `/metrics`** (§9l), and the group access URL is
  **`networking.dns`**, not a top-level `endpoint` (§9j — that bug ate the first
  paid start). **Both ATX prod groups were then deleted (2026-10-13, user) — nothing
  in `llm` is running and no ATX prod group exists**; the verified config is the
  `deploy_ampere.py` defaults, so recreating is free. The arbitrator's first live run
  reallocated once and worked (§15 item 3).
- **Latest image set = v2.5 (bw-reporter v2 / xet fix), fleet re-PATCHed
  7/7.** The bandwidth reporter now reads the kernel NIC RX counter (the
  exact counters `bmon` reads) and logs `epoch rx-bytes model-bytes` —
  fixes the systematic 0.0 Mbps while xet moves shard bytes through its
  CAS cache outside MODEL_DIR (§15.6). Watchdog content unchanged (still
  v2.3: `SSH_GUARD_MAX_HOLD` cap; shell ergonomics still present). Hub
  digests base `0586ba35…`, generic `2fc912ae…`, q6 `a779732c…`, ampere
  `28417137…`; FROM re-pins + all 5 deployer re-pins done. Fleet PATCH 7/7
  `200 OK` (env byte-identical, no starts); propagation verified read-only
  via `tools/poll_images_v12.py`.
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
- **Bandwidth 0-Mbps bug: ROOT-CAUSED + FIXED in code (55aac06,
  2026-10-11): the xet fix.** The v1 reporter measured
  `MODEL_DIR` file growth, but the images bake `HF_XET_HIGH_PERFORMANCE=1`
  and xet moves most shard bytes through its CAS cache **outside** MODEL_DIR
  — the dir only grows at shard finalization, so the manager read 0.0 Mbps
  for long stretches of a healthy download (spikes at finalizations: the
  1081/616/0.0/877/0.0/295/0.0/1874 pattern from run 1). The reporter now
  logs `epoch rx-bytes model-bytes` — the kernel NIC RX counter (the exact
  counters `bmon` reads, read directly) is the bandwidth source; model bytes
  are the human-facing MB display only. The manager computes rates from the
  rx column and still parses the legacy 2-column log (rx := model), so old
  images keep measuring at v1 accuracy in a mixed fleet. **Shipped in the
  v2.5 image set (2026-10-11)** — built, pushed, re-pinned and fleet
  re-PATCHed 7/7 (no starts); only NEW instances run the fixed reporter.
- **All 7 groups stopped at $0 — the in-flight drop-test ended.** The
  tr4 reboot (~04:3xZ) killed the local `sl_admin.py` mid-rerun; the
  unmanaged `atx-hb-test2` then self-stopped via its own v2.3 watchdog
  (heartbeat timeout) — state verified read-only on resume (stopped,
  0 instances). Billing is stopped. The user's rerun terminal log (shot
  count + sample lines) is still the open artifact for §15.2.
- **10000-Mbps live test, attempt 3 (2026-10-11, v2.5 set): bailed at 4/4 ssh
  strikes; root cause = SSH public-key denial on node `71e4e672` (user-
  identified, fix deferred to the user).** Acquired b9c4bc30 06:09:15Z; all
  four probes `conn (TimeoutExpired)` → `bail: 4 consecutive ssh failures`
  (exit 3; the group is never stopped by the arbitrator and no shot is
  spent, so it kept billing unmanaged). Post-bail read-only probe: the
  instance had churned to c319ae15 on the SAME node; the relay was now
  listening but answered `Permission denied (publickey)`. Group now stopped,
  0 instances, billing stopped. §15 item 2.
- **Nothing else is left in the critical path except user-gated paid
  actions:** the next arbitrator live run — on a v2.5 instance the Mbps
  column is real per-interval wire speed. Offline suites green (watchdog
  arbitrator 26/26).
- **Custom chat template from a URL (added 2026-10-12, §8):** `CHAT_TEMPLATE_URL` is the
  **third template source** — `docker/chat_template_url.sh` downloads it at startup, validates
  it (size / UTF-8 / Jinja markup / not-HTML), serves it via `--chat-template-file`. It **wins
  over `CHAT_TEMPLATE` and `CLAUDE_TEMPLATE`**, the Claude patch is **not** applied to it, and a
  rejected download **kills the container** (STOPPED group, not billed). All five deployers take
  `--chat-template-url` (config-time rc2 guard). Images bumped: generic **v13** / q6 **v15** /
  ampere **v11** + `ENV CHAT_TEMPLATE_URL=none` baked. **Not built/pushed yet — the deployers
  still pin the older digests, so the flag needs a user-built image + re-pin to take effect on a
  live group.** Offline suite `tools/test_chat_template_offline.py`: 46/46 green (2026-10-12).
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
- **Anthropic proxy:** `claude/sl_proxy.py` — a sidecar that serves only the
  Anthropic Messages API shape, converts to llama-server, and strips empty
  `think` blocks (see §8). `cl_salad` verifies **its own** proxy owns the local
  port before launching claude (the proxy echoes a per-session nonce on
  `/healthz`): a second session on 8093 exits **3** with a "port held by another
  session" message instead of running against the first session's proxy — run it
  with `--port 8094`. (2026-10-13; a plain liveness probe passes in exactly that
  collision case, so liveness alone is not ownership.)
- **SSH access:** Salad exposes SSH into a running container
  (`ssh root@<ip> -p <port>`); that's how the user works inside the box and how
  the guard file `/.ssh` is touched (§3).
- **Idle/heartbeat self-shutdown watchdog** (`docker/idle_watchdog.py`): stops
  the group (via the Salad stop API, then SIGTERM) when idle or when the client
  heartbeat goes quiet — so an unattended group stops *billing*. §5.
- **Bandwidth arbitrator** (`utils/sl_admin.py --min-bw-mbps`): during the
  HF model download, if throughput is too slow it relocates the instance to a
  faster node via `POST …/reallocate` (never stop/SIGTERM). §6.
- **Deployers** (`deploy/*.py`): create/patch Salad container groups, digest-pin
  the image, inject the full env, manage the stop key.
- **Clients:** `claude/cl_salad`, `claude/sl_chat.sh`, `claude/sl_proxy.py`.
- **Manager:** `utils/sl_admin.py` — list/refresh/start/stop/wait/stats/**describe**
  (whole-config dump of one group) + create-org/create-project/deploy + the
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

**Arbitrator side** (`utils/sl_admin.py` → `_bw_ssh_guard()`, ~line 571):
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

**Tests.** Arbitrator offline harness **26/26** (`tools/test_arbitrator_offline.py`,
scenarios 14–17 cover the guard; `tools/fakebin/ssh` dispatches the bw probe vs the
guard probe by the last argv token). Watchdog offline suite **7/7**
(`tools/test_watchdog_offline.py`, rewritten for v2.3: held-off while `/.ssh`
present, `rm`→kill, absent→kill, `SSH_GUARD=0`→kill, uncapped (`SSH_GUARD_MAX_HOLD=0`)
+ guard forever → never kills, default cap 1200 + guard forever → cap-kill,
activity-bump resets the clock, cap=160 → early cap-kill). Both run with
`python3 -I`.

**Fleet PATCH — DONE 2026-10-10 (verified).** All 7 groups PATCHed zero-cost
(`--no-start`; `qwen38-9b` via direct `update_container_group`) with
`HEARTBEAT_TIMEOUT=300` + `SSH_GUARD=1`, images untouched at that step, and the
result settled (`pending_change=False` on all 7, confirmed by live GET).
Snapshot diff was taken in `tmp/fleet_env_{before,after}.json` (scratch, since
cleaned up — the verdict is the text below, that is the record). The two
heartbeat-armed groups kept their armed mode + real stop keys; the five
`IDLE_SHUTDOWN=none` groups got `IDLE_GRACE=1800` (neutral, the baked default).

---

## 4. The image chain (CURRENT 2026-10-11) — the v2.5 set (watchdog still v2.3)

**The v2.5 set (tr4, 32 jobs, warm layer cache — ~14 min, 05:22→05:36Z).**
All four images are BUILT + PUSHED + digest-verified (in situ: build id +
`net_rx_bytes` in the baked reporter + the 3-column write line), the fleet
is re-PATCHed onto it 7/7, and all 7 digests confirmed landed via
read-only GETs (2026-10-11). Content diff vs the v2.4 set = ONLY the
`bw_reporter.py` rewrite (the xet fix, §15.6); watchdog content is
unchanged (still v2.3). For these single-arch amd64 images the Hub
manifest digest == the local image ID (the registry-returned `digest:`
line on push):

| family | repo:tag | digest `sha256:` | what's in it |
|---|---|---|---|
| base | `boris271142/lmss:cuda128-v12` | `0586ba35fb439c4ea35271c17d6fa243b3605d993a5c3f3f32ba0b1070011e63` | llama.cpp server + proxy + watchdog **v2.3** + **bw_reporter v2 (kernel RX counter)** + ping + llama-stats + shell ergonomics |
| generic | `boris271142/lmss_generic:cuda128-v12` | `2fc912ae0050c8adc9b11e26ff53a777ff0a92cf4560b77841b2da19136683af` | base, any-model, no baked model |
| q6 | `boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:cuda128-v14` | `a779732c02b2ad8148d21c6ff6f33f5a6d252e9de2e78ab729878342e389ab88` | base + Qwen3.8 27B Q6 + MTP + vision + Claude template |
| ampere | `boris271142/lmss_generic_ampere:cuda130-v10` | `28417137144d2192093eada751b359dc13b482a0190d7eb37e87b7232feb259b` | llamAmpere turbo-KV fork, sm_86 cards |

**The chat-template-url set (built + pushed on tr4, 2026-10-12, `BUILD_JOBS=24`) — the
live pins.** Same watchdog v2.3 / bw_reporter v2 content, plus `chat_template_url.sh`
and the `CHAT_TEMPLATE_URL` control (base build id carries the feature note):

| family | repo:tag | Hub digest `sha256:` | deployer pins |
|---|---|---|---|
| base | `boris271142/lmss:cuda128-v13` | `d6e1b1d940608e25e0f11a84bebafc3a20c8df5b59d6c42673d98ef93bf337cd` | `deploy_qwen38_9b.py`, `deploy_qwen9b.py` |
| generic | `boris271142/lmss_generic:cuda128-v13` | `14cb99414aa6d233d66922ea8d7bf88b353f63828f3dc348c783dfe5ef75b0b4` | `deploy_generic.py` |
| q6 | `boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:cuda128-v15` | `75c8cf667590ee9096dc4e6bd11afe24acd84f988225d76ef89a202939d36e47` | `deploy_qwen38_27b.py` |
| ampere | `boris271142/lmss_generic_ampere:cuda130-v11` | `4ad7dd6b7ab2961f19644dc17d5cce9bd1ae64c0544ac6fa3ebd301de49e0645` | `deploy_ampere.py` |

The derivatives keep `FROM boris271142/lmss:cuda128-v12@sha256:0586ba35…` (each ships
its own `entry_*.sh` + `chat_template_url.sh`, so the feature needs no FROM re-pin).

**bw_reporter v2 (the v2.5 content change):** logs `epoch rx-bytes
model-bytes` every 10 s — `rx-bytes` is the kernel NIC RX counter
(`/sys/class/net/<if>/statistics/rx_bytes`, summed over non-lo — the exact
counters `bmon` reads, read directly: no binary, no extra process, our own
epoch timestamps over the shell-less relay) and `model-bytes` is MODEL_DIR
size (the human-facing MB display only). Root cause of the v1 0.0-Mbps bug:
the images bake `HF_XET_HIGH_PERFORMANCE=1` and xet moves most shard bytes
through its CAS cache OUTSIDE MODEL_DIR, so MODEL_DIR growth only spiked at
shard finalizations (§15.6). The manager computes rates from the rx column
and still parses the legacy 2-column log (rx := model), so old images keep
measuring at v1 accuracy in a mixed fleet. Wiring unchanged: all four entry
scripts launch `nohup python3 /usr/local/bin/bw_reporter.py … &`; base COPYs
the new file, ampere COPYs it itself, generic/q6 inherit it via their FROM
re-pin.

**Shell-ergonomics block (v2.4 content, still present; inherited by all four), appended at the END of
`/root/.bashrc` in the final stage** (`Dockerfile.multistage` +
`Dockerfile.lmss_generic_ampere`; generic/q6 inherit via their FROM re-pin).
The Salad ssh relay execs a bare `/bin/bash` on a pty with `TERM=xterm`
(breaks nvtop/htop) but DOES read `/root/.bashrc` (verified live):
`export TERM=xterm-256color` + aliases `nv`(nvtop) / `ht`(htop) / `gpu`
(nvidia-smi) / `wlog` (tail -f the watchdog log) / `alog` (tail -f the API
log) / `ready` (curl :9999/ready) / `als` (alias) / `ll` / `la` / `l` / `h`
(history).

**Superseded (v2.4 set, shell ergonomics, commit `c04ded0`):** base v11
`2bfbd69c`, generic v11 `888fc401`, q6 v13 `f6e3b9d0`, ampere v9
`d4f4b030`. **Superseded (v2.3 set, guard-v2.3, commit `29de3ec`):** base v10
`c363d00e`, generic v10 `95b4c77c`, q6 v12 `56579bc1`, ampere v8
`f713e04f`. **Superseded (v2.2 set):** base v9 `47d2d276`, generic v9
`9f7b0638`, q6 v11 `16c48781`, ampere v7 `314f77b8`. **Pre-guard (v2.1)**,
tags deleted from Hub: base v8 `0386058c`, generic v8 `9fe70c07`, q6 v10
`57656009`, ampere v6 `ceb9059f`.

**Build procedure** (tr4, 32-way): build the **base first**, push, capture its
digest; re-pin the **derivative `FROM`s** to the base digest
(`docker/Dockerfile.lmss_generic:75` and `docker/Dockerfile.lmss_q6_mtp_vision:59`
both `FROM boris271142/lmss:cuda128-v12@sha256:0586ba35…`); build+push the
three derivatives with `--build-arg BUILD_JOBS=32`. **Build-context gotcha:**
the generic/q6/ampere Dockerfiles `COPY docker/version.sh` → the build context
MUST be the **repo root**, and on tr4 the working invocation is
`cd <ctxdir> && docker build -f <Dockerfile> .` (a relative `-f` + separate
context arg trips this host's buildx arg-parsing); only `Dockerfile.multistage`
takes the `docker/` subdir as context. **Host gotcha (BOTH boxes):** `nproc`
misreports **1** on tr4 (`/proc/cpuinfo` shows 32 real CPUs) **and on the primary
box** (verified 2026-10-12: `nproc`=1, `/proc/cpuinfo` 28 × i7-14700K, affinity
mask 0-27, no readable cgroup v2 `cpu.max`) — always pass
`--build-arg BUILD_JOBS=N` explicitly; never trust an auto-detected job count.
The user's instruction for builds on the primary box is **24 CPUs**
(`--build-arg BUILD_JOBS=24`, raised from 4 on 2026-10-12; the box has 28).
(docker 29.7.2 / buildx v0.36.1 as of the v2.5 cycle.)
**Copying beats rebuilding** when the images already exist on the other box:
`ssh andrei@tr4 "docker save <ref> | pigz -p 8" | pigz -dc | docker load` — one
image per pipe (a break costs one image, not the set), no temp tarballs, ~11 s
per image on the LAN (`tools/copy_images_from_tr4.sh` verifies each loaded ID
against the expected Hub digest prefix). After **every push**, set the repo
`full_description` overview (standing rule, §11 — verified this round: all 4
repos already carry it, no PATCH needed).

**Cache reality (correction):** tr4 DOES accumulate a warm docker layer cache —
the first (v2.3) build set was genuinely from scratch (~1.5h+ for the
non-ampere trio), but the v2.4 rebuild of all four images took only **~14 min**
(01:42:05→01:55:48Z) because only the appended `.bashrc` RUN block and later
layers rebuilt. Plan ~15 min for small content changes; the long pole (ampere
llamAmpere CUDA template-instances pass) only bites on cold rebuilds.

**Deployer digest pins** (re-pinned 2026-10-11 to the v2.5 set, the live
`@sha256:` line in each): `deploy/deploy_qwen9b.py` +
`deploy/deploy_qwen38_9b.py` → base v12 `0586ba35…`;
`deploy/deploy_generic.py` → generic v12 `2fc912ae…`;
`deploy/deploy_qwen38_27b.py` → q6 v14 `a779732c…`;
`deploy/deploy_ampere.py` → ampere v10 `28417137…`. Each deployer also keeps a
commented history list of prior tag→digest for humans.

**v2.3 rebuild (COMPLETE on tr4, 2026-10-11).** tr4 had never built these
images → the whole set compiled from scratch at **32 jobs** (sequential — two
32-way CUDA compiles would OOM 62 GB; the ampere fork compile's CUDA
template-instances stage was the long pole). All digests captured, re-pinned
here and in the five deployers; the derivative `FROM`s point at the new base
digest. That set is now superseded by v2.5 above.

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
`cl_salad` / `sl_chat.sh` send a 1-token completion through the gateway every
~30 s. **`SALAD_HEARTBEAT=1` is now the default** (opt out `=0`; harmless on
watchdog-less groups). Keep `HEARTBEAT_TIMEOUT` ≥ ~2× the 30 s pinger.

**Cold-start defect + fix (commit `b5aa1db`):** a 27B cold start took ~37 min;
the baked `IDLE_GRACE=1800` expired before `/ready`, so the watchdog gave up
unarmed and the group ran the whole session with self-stop impossible. Fix:
`--idle-grace` deployer flag (default 1800; **3600** for big quants) sends
`IDLE_GRACE` in env. **atx-hb-test2 carries `IDLE_GRACE=3600`** (see §2).

---

## 6. Bandwidth arbitrator (`sl_admin.py --min-bw-mbps`)

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
`sl_admin.py`** (the orchestrator reads the log over SSH and decides).

**Constants** (`sl_admin.py`, ~line 114): `BW_POLL_INTERVAL=15`,
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
`tools/test_arbitrator_offline.py` + `tools/fakebin/ssh`, run with `python3 -I`.
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

**Prod group — DELETED 2026-10-13** `atx-swift-27b-q5-opt` (ma-casa-in-paris/llm,
alias `atx-swift-27b-opt`): user-tested OK 2026-10-07, then stopped. Its
medium-priority successor `atx-swift-27b-q5-med` was deleted the same day, so
**no ATX production group exists now** — `llm` keeps the 90K A/B baseline
`atx-swift-27b-q5`. Restore = a bare `deploy_ampere.py --no-start` run with a fresh
group name (free; the paid start is the user's call). **CTX VERIFIED
2026-10-13** on the recreated `atx-swift-27b-q5-med`: argv carries
`--ctx-size 132768` and **`/slots` reports slot 0 `n_ctx` = 132864** (132768 rounded
up to a 128-block) at 23067/24576 MiB VRAM. §15 item 3 closed; §9l for the method.
**`atx-swift-27b-q5-med` was then deleted (user's instruction, 2026-10-13, billing
off), so the surviving prod group is `atx-swift-27b-q5-opt` — stopped, and still
`priority=low`.** To restore the verified medium-priority group: a bare
`deploy_ampere.py --no-start` run (free; use a fresh name — `…-med` is tombstoned),
then the paid start.

**2026-10-12 paid attempt #1 (no verdict, ~18 min burned).** Started 23:14:06Z,
instance `25976502` reached `ready=True` at 23:25:38Z (pull 100 %), and the scrape
never ran: `tmp/verify_atx_ctx.py` read `g["endpoint"]` → `None` → exit 4. **The
access URL is `networking.dns`** (`damson-pepper-erau5a722gxf2qm2.salad.cloud`);
this API version has no top-level `endpoint`. The group then stopped at 23:31:54Z
before any scrape. Stop cause UNKNOWN from the API (`cs.description` empty,
container torn down, no `watchdog.log` survives): the watchdog is ruled out
(`IDLE_GRACE=1800` ⇒ earliest arm-kill ~23:55) and the readiness probe is ruled
out (ready passed inside its ~40-min window). Leading candidate: **preemption —
the live group was `priority="low"`** while `deploy_ampere.py` defaults to
`medium` (`:225`; `:219` documents that low/batch are evicted when higher-priority
work lands). `priority` is CREATE-only, so it cannot be PATCHed.
**Bonus from this run:** the v2.5 3-column bw reporter got its first live wire
verification — `bw.log` rows `epoch rx-bytes model-bytes`, ~520 Mbps with
16.58 GB of model bytes (§15 item 2's "never live-verified" is closed).

**Recreation (2026-10-12, free — create does not bill):** `llm/atx-swift-27b-q5-med`,
`MODEL_ALIAS` unchanged (`atx-swift-27b-opt`), new DNS
`fig-mushroom-udsq8tht4a4zy4qw.salad.cloud`. Built by a bare
`deploy_ampere.py --project llm --group atx-swift-27b-q5-med --idle-shutdown
heartbeat --stop-key deploy/salad_api.txt --no-start`; env diffed live against the
prod group — **22 keys, only `NAME` and `SALAD_GROUP` differ** (resources, image,
readiness probe and networking all identical; `--reasoning-format none` stripped by
`strip_none_value_args` as intended). Intended deltas: **`priority=medium`** and
`restart_policy=never` (armed watchdog; the old group carried `always` because it
was created unarmed and later PATCHed). Fresh name dodges the delete tombstone.

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

**THIRD template source — `CHAT_TEMPLATE_URL` (added 2026-10-12, `docker/chat_template_url.sh`):**
the two existing sources both need the file *inside* the image (`CHAT_TEMPLATE=<baked path>`,
`CLAUDE_TEMPLATE=1` = dump + patch), so a custom template meant building an image. Now
`CHAT_TEMPLATE_URL=<http(s) url>` downloads one at startup (same shape as the old
`DRAFT_MODEL_URL` / `VISION_MODEL_URL` mechanism) and serves it via `--chat-template-file`.
**Precedence: the URL wins over both** (most explicit source), and the Claude patch is **not**
applied to a downloaded template — served as its author wrote it (the helper prints a NOTE when
the download carries the `raise_exception('System message must be at the beginning.')` marker =
it will break Claude Code). Validation is hard-fail by design: 1 B–2 MiB, strict UTF-8, must
contain `{{`/`{%`, must not start like HTML, download to `…jinja.part.$$` + `mv` only a validated
file, cache re-validated before reuse; anything rejected → non-zero exit → under the entry
script's `set -euo pipefail` the **container dies** → `restart_policy=never` → STOPPED group
(loud, not billed). "Downloaded something, hope it is a template" is exactly what the path-loop
burned us on. Deployers: `--chat-template-url` (all five; validated at config time, rc 2, free).
Images: generic **v13** / q6 **v15** / ampere **v11** (build ids bumped; `ENV CHAT_TEMPLATE_URL=none`
baked so existing groups are safe) — **the deployers still pin the older digests, so the flag needs
a user-built/pushed image + re-pin to take effect.** Suite: `python3 -I tools/test_chat_template_offline.py`
(46 checks, all pass 2026-10-12: good/html/empty/oversized/non-UTF-8/git-lfs-pointer/404, cache
reuse + re-validate + drop, scheme rules, stdout contract, `set -e` caller propagation).

**MTP requirement:** the image hardcodes `--spec-type draft-mtp`, so the served
GGUF must contain the MTP/nextn layers — the `noMTP-*` files crash the server.
(`USE_DRAFT_MODEL=none` means use the GGUF's *embedded* MTP head.)

**3090 ctx math:** `(23.0 GiB − weights − ~1.1 GiB overhead) / ~35 KiB` ≈ max ctx.
That's why prod 27B runs **Q5_K_M @ 90 K** (`n_ctx` = **90112**, llama.cpp
rounding), not Q6_K (Q6_K @ 90 K needs ~3.1 GiB KV, only ~1 GiB headroom). A 32 GB
worker (5090) could serve full Q6_K at ~120 K+.

**Double-`think` echo on ATX groups (fixed 2026-10-09):** with
`enable_thinking=false` (what `sl_proxy` sends) the template ends the prompt
with the CLOSED empty block `think\n\n/think\n\n` as the generation prefix; the
model echoes it; llama-server strips think blocks **only** with its default
reasoning parser — but ATX groups' `EXTRA_ARGS` carried `--reasoning-format none`
(copied from the ATX post.txt), so the echo passed through. q6 groups use the
default parser → stripped → "didn't have that before." Fixes: (1)
`claude/sl_proxy.py` **`EmptyThinkStripper`** removes *empty* think blocks from
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
polling. `sl_admin.py wait` implements exactly this.

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

**9e-correction (2026-10-12, `tools/patch_images_v13.py` run on the live fleet).** Two things the
wording above got wrong, both measured: **(1)** the GET response has **no
`pending_update_in_progress` field at all** — its keys are `pending_change`, `current_state`,
`version`, `update_time`, `scaling-actions`, … — so `GET.get("pending_update_in_progress")` is
*always* None and any guard built on it silently does nothing. The parked state is observable
**only on the write path**: a repeat PATCH returns **400** with ProblemDetails
`type:"pending_update_in_progress"`. Use a same-digest repeat PATCH as a free, no-op probe.
**(2)** the consistency window is **minutes, not seconds** — re-GETs at 1 s, 2 s, 4 s, 8 s after a
200 all returned the OLD image with `pending_change=True`; the new digest read back with
`pending_change=False` only after ~3 min. So: **poll the readback, never assert on one GET**, and
treat "400-parked with `pending_change=True`" as the *expected* end state of a previously-started
group, not as a failure. **(3)** a parked update **becomes visible in the GET on its own** —
`atx-swift-27b-q5` read [STAGED] (old image, `pending_change=True`) and ~20 min later read the NEW
digest with `pending_change=False`, with no start in between. "Parked" = further PATCHes refused +
the queued spec is what RUNS at the next start; it does NOT mean the spec stays hidden.

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

**9i. While an update is in-flight, the group API returns a TRUNCATED bare
digest.** A converged group shows its image as the full `repo@sha256:<64>`;
while a re-pin is still propagating the API may hand back **only a truncated
bare `sha256:<12>`** (e.g. `sha256:a7797` — no repo prefix, no full hash). A
landing poller that compares whole strings — or bare-digest equality — never
matches and waits forever on a value that will never equal the target: the v2.5
landing poller (`tools/poll_images_v12.py`) sat at **0/7 for 12 minutes** on this
bug, not on real propagation. Rule: strip everything before `sha256:`, compare
the full 64-hex digest, and treat a 12-char bare digest as "still in-flight",
not as a mismatch. (Distinct from §9e's stale/transitional GETs — this GET is
live, just in a transient format.)

**9l. Served context is `/slots`, NOT `/metrics`.** This llama.cpp build's
`GET :8080/metrics` exposes counters (`prompt_tokens_total`,
`tokens_predicted_total`, `n_decode_total`, `n_tokens_max`, spec-decode counters)
plus the `n_busy_slots_per_decode` gauge — **there is no `n_ctx` gauge at all**, and
`GET /props` is trimmed to UI keys (no `params`, no `slots`, no `n_ctx`). The served
context is on **`GET :8080/slots`** → per-slot `n_ctx` (verified 2026-10-13:
`132864` for `CTX_SIZE=132768`). The canonical `utils/llama_stats.py` prints only
what `/metrics` has, so it can never show ctx. Over the relay:
`curl -s http://127.0.0.1:8080/slots` (curl IS in the ampere image; `wget` is not,
and `ps` is not — no procps, so find the pid with
`nvidia-smi --query-compute-apps=pid --format=csv,noheader`, then
`cat /proc/<pid>/cmdline` and `tr '\0' ' '` **on our side**, §9f).

**9j. The group ACCESS URL is `networking.dns`. There is NO top-level `endpoint`.**
`GET /container-groups/{org}/{proj}/{group}` returns
`networking: {dns: "<name>.salad.cloud", port: 8888, auth: true, …}` and **no**
`endpoint` key (reads `None`). Scrape the gateway at `https://<dns>` (443,
Cloudflare-fronted — `8888` is the port INSIDE the container, not reachable).
This cost a whole paid start on 2026-10-12: the verifier polled to `ready=True`,
read `g["endpoint"]`, got `None`, and exited without scraping. Prefer an SSH
fallback for spec facts (`curl -s http://127.0.0.1:8080/metrics` — the relay is
shell-less, one plain command, exits 1 even on success: judge by payload, §9f).

**9k. `priority` is CREATE-only, and `low` is preemptible.** `priority` and
`restart_policy` cannot be PATCHed — to change them, delete and recreate (fresh
name, §9c). A `low` group is placed best-effort and **evicted whenever
higher-priority work lands**; the live prod ATX group was `priority=low` while
`deploy_ampere.py` defaults to `medium` (`:225`), because it was created before
the flag existed. A ~18-min paid run ending in an unexplained stop is the
signature. Recreated 2026-10-12 as `llm/atx-swift-27b-q5-med` @ `medium`.

**9m. GPU availability is a POST, and its counts are per PRIORITY TIER, not per
card.** (measured 2026-10-14, `sl_admin --query` / `salad_client.get_gpu_availability`)
`POST /organizations/{org}/availability/sce-gpu-availability` (spec operationId
`get_gpu_availability`), body `GpuAvailabilityPrototype`: `gpu_classes` (UUID array,
minItems 1) required; optional `cpu`, `memory` (**the key is `memory`, in MB — not
`memory_mb`**), `storage_amount` (bytes), `country_codes` (`^[a-z]{2}$`). Answer:
`{"available_gpu_batch","available_gpu_high","available_gpu_low","available_gpu_medium",
"on_call_gpu"}` — ONE count per tier for the WHOLE class set in the request, so the
endpoint cannot answer "how many 3090s". Floors demonstrably change the answer
(`rtx3090,rtx4090,rtx5090`: no floors → high=341 medium=64 low=11 batch=0 on_call=0;
with cpu=8 memory=16384 storage=53687091200 → high=269 medium=42 low=0). **Hourly
prices are NOT in that answer** — they ride in `GpuClass.raw["prices"]` from
`GET /organizations/{org}/gpu-classes` as `[{"price":"0.170","priority":"high"},…]`
(3090 H0.170 M0.143 L0.117 B0.090 · 4090 H0.330 M0.273 L0.217 B0.160 ·
5090 H0.500 M0.417 L0.333 B0.250). Card UUIDs are the same in both orgs.

**9n. A priority recreate must rewrite the env values that ARE the group name.**
`docker/idle_watchdog.py:202-204` builds its self-stop URL from
`SALAD_ORG/SALAD_PROJECT/SALAD_GROUP`. A delete+create (§9k) under a new name that
copies the env verbatim leaves `SALAD_GROUP` pointing at the DELETED group: the
self-stop 404s, the container self-exit is RESCHEDULED (not stopped) and **billing
continues** — the exact failure the watchdog exists to prevent. `sl_admin._spec_from_raw`
rewrites every env value equal to the old name (`NAME`, `SALAD_GROUP`) and
deliberately leaves `MODEL_ALIAS` alone (client-facing, stays stable across
recreations). Also drop `networking.dns` from the CREATE body — it is
server-assigned (§9j).

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
├─ salad_client.py               (877 ln) Salad REST client. UpdateContainerGroupRequest
│                                 is a merge-patch (unset fields omitted; env, when
│                                 present, replaced wholesale). get_container_group(
│                                 …).raw = full group dict. update_container_group(…).
├─ deploy/
│  ├─ deploy_qwen38_27b.py       (669) 27B vanilla/q6 profile; pinned q6 v14 @a779732c02b2.
│  ├─ deploy_qwen38_9b.py        (471) 9B; base v12 @0586ba35fb43. **CREATE-ONLY** (refuses existing group).
│  ├─ deploy_qwen9b.py           (435) 9B (base v12); the other 9B deployer.
│  ├─ deploy_generic.py          (735) vanilla any-model; generic v12 @2fc912ae0050; --gpu any card.
│  ├─ deploy_ampere.py           (761) llamAmpere ATX prod profile; ampere v10 @28417137144d; sm_86 only.
│  │                              All five gained `--chat-template-url` (2026-10-12, §8) — the
│  │                              deployers' image pins predate it, so it needs a re-pin.
│  ├─ README.md                  the deployer command lines (5 deployers, zero-cost re-apply,
│  │                              watchdog arming, custom-template-from-URL, "rules that bite").
│  │                              Written 2026-10-13.
│  ├─ salad_api.txt              the Salad API key (ACCOUNT key; all orgs). NEVER print.
│  └─ hft.txt                    HF token. NEVER print.
├─ docker/
│  ├─ Dockerfile.multistage      base image build (context = docker/).
│  ├─ Dockerfile.lmss_generic    generic; FROM base v9 @digest. (context = repo root)
│  ├─ Dockerfile.lmss_q6_mtp_vision   q6 baked; FROM base v9 @digest. (repo root)
│  ├─ Dockerfile.lmss_generic_ampere llamAmpere fork. (repo root)
│  ├─ entry_{base,generic,q6,ampere}.sh  startup scripts (trapped-child tail; §5/§9h)
│  ├─ idle_watchdog.py           (467) self-shutdown watchdog v2.3 (guard + 1200 s cap; §3/§5).
│  ├─ bw_reporter.py             (160) in-image bandwidth sampler → /tmp/llama-api/bw.log
│  │                              (v2: kernel RX counter, xet-proof — §15 item 6).
│  ├─ claude_template.sh         startup dump+patch of the Claude chat template.
│  ├─ chat_template_url.sh       (124) download + validate a CUSTOM template from CHAT_TEMPLATE_URL
│  │                              (wins over CHAT_TEMPLATE/CLAUDE_TEMPLATE; hard-fail on junk — §8).
│  ├─ qwen3.8.q6.jinja           the (patched) Qwen template.
│  ├─ version.sh                 prints the image build id / generation fingerprint.
│  ├─ api_app.py / run_api.py    the Anthropic-only proxy app.
│  ├─ llama_stats.py / stats.sh  /metrics helpers.
│  └─ README.md                  (51 KB) the big image/deploy doc (linked from Hub).
├─ claude/                        (renamed 2026-10-12 → the `sl_` prefix: salad_proxy.py→sl_proxy.py,
│  │                              chat_salad.sh→sl_chat.sh, curl_salad.sh→sl_curl.sh,
│  │                              cl_salad_deploy→sl_claude_deploy. `cl_salad` keeps its name.)
│  ├─ sl_proxy.py              (705) Anthropic-only proxy + EmptyThinkStripper (§8); --nonce = /healthz ownership probe.
│  ├─ cl_salad                 (203) Claude Code → Salad launcher (heartbeat ON by default; exits 3 when the proxy port is another session's). NOT renamed.
│  ├─ sl_chat.sh               (137) curl-based chat client (URL positional arg).
│  ├─ sl_claude_deploy         (242) deployer wrapper (auto-exports SALAD_HEARTBEAT).
│  └─ sl_curl.sh               (104) the gateway smoke test (2–3 known-answer questions).
│                                 (A `cube.cpp` CUDA experiment lived here briefly: added
│                                 801520e, deleted fa0e06c — the 17 KB binary is in git
│                                 HISTORY at 801520e even though the tree is clean.)
├─ utils/
│  ├─ sl_admin.py          (2189) the fleet admin: list/start/stop/wait/stats/delete,
│  │                              bandwidth arbitrator (§6, --min-bw-mbps),
│  │                              create-org / create-project (fleet.json register),
│  │                              deploy (prompted DOCKER TYPE → its deployer),
│  │                              card/priority flags --gpu/--prio/--query/--y (§9m/§9n).
│  │                              Renamed from manage_groups.py 2026-10-12. Writes
│  │                              `fleet.json` (the org→project map; created on the
│  │                              first create-org/create-project, absent until then).
│  ├─ billing.py                 cost tracking.
│  ├─ llama_stats.py             /metrics scrape helper.
│  └─ portal_vault.gpg           a gpg key (vault). Treat as secret.
├─ docs/
│  ├─ README.md                  (28 KB) how-to / architecture doc.
│  ├─ container_group_create.md  Salad create-group API reference (schema + status enum).
│  ├─ salad/                     scraped Salad docs (api-usage.mdx etc.).
│  └─ SaladCloud API Knowledge Setup for Cline.md
├─ .clinerules/salad-api.md      Salad API notes for the Cline agent.
├─ tests/                        **LOCAL** Docker run/test scripts (curl1.sh, curl2.sh, run_*.sh)
│  │                              + README.md — localhost only, never the Salad API (added 2026-10-12).
├─ llama.cpp/                    vendored llama.cpp checkout (submodule-ish, 31 dirs).
├─ tools/                        **TRACKED tooling** (promoted from tmp/ 2026-10-13, so the
│  │                              paths §14 cites resolve on a fresh clone — tmp/ is gitignored):
│  ├─ test_arbitrator_offline.py  26/26 arbitrator scenarios (+ fakebin/ssh PATH shim).
│  ├─ test_watchdog_offline.py    7/7 watchdog scenarios (v2.3 guard + cap).
│  ├─ test_deploy_offline.py      deployer offline checks (GPU resolution, request validation).
│  ├─ test_gpu_prio_offline.py    72/72 --gpu/--prio/--query/--force checks (card parsing,
│  │                              prices, availability body, the priority recreate, parked
│  │                              PATCHes, main()'s guards) — fakes the API, no calls.
│  ├─ fakebin/ssh                canned-ssh shim the arbitrator harness runs as `ssh`.
│  ├─ fleet_status_now.py        read-only fleet status+guard snapshot (§14); 404 → `GONE`.
│  ├─ fleet_snapshot.py          broader read-only snapshot.
│  ├─ patch_images_v9/v10/v11/v12.py   image-only fleet re-pin per cycle (v9=guard, v10=v2.3, v11=v2.4, v12=v2.5).
│  ├─ patch_images_v13.py        …and the chat-template-url cycle: ENUMERATES the fleet (sl_admin._fleet,
│  │                            family read from each group's live repo) and POLLS the readback — [OK] digest
│  │                            reads back / [STAGED] 400-parked = queued for the next start (both = success).
│  ├─ sl_set_env.py              MERGE-SAFE env editor for one group (reads live env, applies KEY=VALUE /
│  │                            --unset on top, sends the FULL dict — §9e wholesale replace; [LOCKED] on a
│  │                            parked group; `--dry-run`; secrets never printed).
│  ├─ patch_9b.py / patch_qwen38_9b_rescue.py   9B group PATCH helpers.
│  ├─ poll_images_v12.py         read-only landing poller for the v2.5 re-pin (§9i).
│  ├─ build_push_v24/v25.sh      build→push→digest-capture→Hub-overview chains (v2.4 / v2.5).
│  ├─ build_chat_url_images.sh   chat-template-url cycle: build the 4 images on tr4 (24 jobs, no push).
│  ├─ push_chat_url_images.sh    …then push the 4 on tr4 + PATCH each overview + print the Hub digests.
│  ├─ sl_repin.py                move the 5 deployers' IMAGE pins to new digests in one pass (tag-form
│  │                            comment + history line kept in step; `--dry-run` prints the diff).
│  ├─ hub_describe.py            Docker Hub `full_description` PATCH — REQUIRED after every push (§11).
│  ├─ copy_images_from_tr4.sh    images tr4 → this box (`docker save | pigz` over ssh, verifies loaded IDs).
│  └─ watch_ready.py             read-only readiness watcher; prints transitions, incl. NEW INSTANCE when a /reallocate lands (§6).
└─ tmp/                          (gitignored) scratch: arbitrate_attach.py, arb_watch.py, guard_probe.py,
                                  check_images_v11.py, smoke_test_{start,stop}_cg.py, _grpimg.json,
                                  post.txt (the ATX operating point — deployer-defaults provenance), logs.
```
Key file line counts (2026-10-10; `sl_admin.py` re-counted 2026-10-12 after the
create/deploy + `describe` additions, and 2026-10-14 after the card/priority flags):
`sl_admin.py` 2189, `salad_client.py` 985,
`sl_proxy.py` 705, `deploy_ampere.py` 725, `deploy_generic.py` 697,
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
python3 -I tools/fleet_status_now.py

# List / wait / stats groups (key from deploy/salad_api.txt, never printed).
# The 2nd positional is the ORG (ma-casa-in-paris or akl-on-salad) — `llm` is a
# PROJECT, not a valid 2nd positional. The group is picked INTERACTIVELY
# (# or exact name) at the prompt, then y/N-confirmed for start/stop/delete:
python3 utils/sl_admin.py list  ma-casa-in-paris
python3 utils/sl_admin.py wait  ma-casa-in-paris     # polls per-instance `ready` (§9d)
python3 utils/sl_admin.py stats ma-casa-in-paris
# ONE group's WHOLE config, read-only (image repo/tag/digest + local-id line, container
# hash/resources/GPU names/CMD, probes, FULL env with secret VALUES redacted, live instance
# ids + ssh lines, derived client commands; `--raw` = the whole GET as JSON):
python3 utils/sl_admin.py describe ma-casa-in-paris --project llm --group qwen38-27b-q5
python3 utils/sl_admin.py describe ma-casa-in-paris            # no --group → numbered pick

# PAID start (burns credits — coordinate with the user). Add the arbitrator:
python3 utils/sl_admin.py start ma-casa-in-paris --min-bw-mbps 30   # bandwidth-aware start (§6)
# non-interactive variant (pipe the prompt answers: name, confirm):
printf 'atx-hb-test2\ny\n' | python3 utils/sl_admin.py start ma-casa-in-paris --min-bw-mbps 30

# CARDS & PRIORITY (added 2026-10-14; cards = rtx3080ti,rtx3080,rtx3090,rtx3090ti,
# rtx4080,rtx4090,rtx5090 — comma lists, spaces and repeated flags all accepted):
python3 utils/sl_admin.py --query --gpu rtx3090,rtx4090        # FREE: USD/hour per tier + availability (§9m)
python3 utils/sl_admin.py --query --gpu rtx3090 --project llm --group qwen38-27b-q5
                                                              # …with that group's resource floors
python3 utils/sl_admin.py start ma-casa-in-paris --gpu rtx3090          # prices → diff → confirm → PATCH card → start
python3 utils/sl_admin.py start ma-casa-in-paris --gpu rtx4090 --force  # skip the confirm (BILLS per second)
python3 utils/sl_admin.py start ma-casa-in-paris --prio high            # CREATE-only → delete+create <name>-high (§9k/§9n)
python3 utils/sl_admin.py deploy --type ampere --org ma-casa-in-paris --project llm --group atx \
    --gpu rtx3090 --prio med                                          # → the deployer's own --gpu / --priority
python3 -I tools/test_gpu_prio_offline.py                    # the offline suite (72 checks, no API)

# Deploy / zero-cost re-pin (deployers PATCH the full env; applies at next PAID start):
python3 deploy/deploy_ampere.py     --project llm --group atx-swift-27b-q5-opt --no-start \
    --idle-shutdown heartbeat --stop-key deploy/salad_api.txt --idle-grace 3600
python3 deploy/deploy_qwen38_27b.py --project qwen38-27b --group qwen38-27b-q5 --no-start
# NOTE: qwen38-9b's deployer is CREATE-ONLY — PATCH it via salad_client.update_container_group
#       (see tools/patch_9b.py / tools/patch_qwen38_9b_rescue.py).

# CUSTOM chat template from a URL (wins over CLAUDE_TEMPLATE; needs a chat-template-url
# image = generic v13 / q6 v15 / ampere v11 — re-pin first; use a RAW url, not a blob page).
# Canonical test URL = this repo's own Claude-patched template (live-verified 2026-10-12:
# 8941 bytes sha256 e8912f4cc41c, identical to docker/qwen3.8.q6.jinja, no NOTE):
python3 deploy/deploy_generic.py --project llm --group my-group \
    --model-repo Some/Org --model-file Model-Q5_K_M.gguf \
    --chat-template-url https://raw.githubusercontent.com/suedenglehem/docker4salad/main/docker/qwen3.8.q6.jinja \
    --no-start
# …or set it on an EXISTING group without re-running a deployer (merge-safe: reads the live
# env, adds the key, sends the FULL dict — a hand-written PATCH of one key would delete the rest):
python3 -I tools/sl_set_env.py ma-casa-in-paris/llm/qwen38-27b-q5 \
    CHAT_TEMPLATE_URL=https://raw.githubusercontent.com/suedenglehem/docker4salad/main/docker/qwen3.8.q6.jinja \
    --dry-run
# (a group parked by an earlier PATCH answers [LOCKED] 400 — its env edits must wait for the paid start)
# The helper itself, straight on the host (curl path — no image needed):
bash docker/chat_template_url.sh \
  https://raw.githubusercontent.com/suedenglehem/docker4salad/main/docker/qwen3.8.q6.jinja /tmp/t.jinja
python3 -I tools/test_chat_template_offline.py     # the offline suite (46 checks)

# Image-only fleet re-pin (env untouched):  python3 -I tools/patch_images_v9.py  (--dry-run ok)
# DEPLOYER re-pin after a push (one pass over all five; digests from the Hub — the
# local `docker images` id is a config hash, NOT the manifest digest):
python3 -I tools/sl_repin.py --dry-run \
  boris271142/lmss:cuda128-v13=sha256:<digest> \
  boris271142/lmss_generic:cuda128-v13=sha256:<digest> \
  boris271142/lmss_jonathancoletti_qwen38_q6_mtp_vision:cuda128-v15=sha256:<digest> \
  boris271142/lmss_generic_ampere:cuda130-v11=sha256:<digest>

# RECREATE a group to change a CREATE-only field (priority / restart_policy are NOT
# PATCHable): fresh name (dodges the delete tombstone — §9), MODEL_ALIAS unchanged,
# --no-start (CREATE is free; only START bills). Diff the env against the old group
# before starting anything:
python3 -I deploy/deploy_ampere.py --project llm --group atx-swift-27b-q5-med \
    --idle-shutdown heartbeat --stop-key deploy/salad_api.txt --no-start
# Then the PAID start is the USER's call (sl_admin.py start … --min-bw-mbps 100).
# VERIFY THE SERVED CONTEXT (the /metrics n_ctx gauge does NOT exist in this build — §9l):
#   argv is the proof, and the image has no procps, so over the shell-less relay (§9f),
#   ONE plain command per call (the relay exits 1 even on success — judge by payload):
ssh root@<ip> -p <port> 'nvidia-smi --query-compute-apps=pid --format=csv,noheader'
ssh root@<ip> -p <port> 'cat /proc/<pid>/cmdline'      # tr '\\0' ' ' on OUR side
curl -s https://<dns>/slots                            # per-slot n_ctx (132768 -> 132864)

# The group ACCESS URL is `networking.dns` (https://<dns>, port 443 — Cloudflare).
# There is NO top-level `endpoint` field on this API version (it reads None).

# Copy the v2.5 set from tr4 into this box's docker store (cheaper than a rebuild;
# streams save|pigz over ssh, verifies each loaded ID == the Hub digest prefix):
bash tools/copy_images_from_tr4.sh

# Build + push an image (primary box: --build-arg BUILD_JOBS=24 — the user's cap;
# tr4: 32. nproc lies (says 1) on BOTH boxes). Base first, then derivatives:
docker build -f docker/Dockerfile.multistage        docker/  --build-arg BUILD_JOBS=24 -t boris271142/lmss:cuda128-vN .
docker build -f docker/Dockerfile.lmss_generic       .       --build-arg BUILD_JOBS=24 -t boris271142/lmss_generic:cuda128-vN .
# (derivatives' context MUST be repo root — they COPY docker/version.sh)
docker push boris271142/<repo>:<tag>     # then PATCH the repo full_description (§11)

# SSH into a RUNNING container (get ip/port from `sl_admin list` / /instances):
ssh root@<ip> -p <port> 'tail -40 /tmp/llama-api/watchdog.log'   # ONE plain cmd, no shell (§9f)
# the GUARD:  touch /.ssh  (when you log in)   /   rm /.ssh  (when you leave)

# Offline tests (python3 -I to avoid site shadowing):
python3 -I tools/test_watchdog_offline.py
python3 -I tools/test_arbitrator_offline.py
python3 -I tools/test_deploy_offline.py
```

**Verify a live image in-container** (over the SSH relay): run `version.sh` (the
build id in `/etc/llama-image-build` is the reliable discriminator — §9a), and
check `llama-server` argv + `/models` contents.

---

## 15. Pending / next steps (USER-GATED — paid actions)

Items 1–2 need a **paid** Salad start (burns credits) — the user decides when.
2026-10-11: the approved guard smoke (item 1) is **FINISHED** — `atx-hb-test2`
stopped at 01:15Z; all 7 fleet groups are now stopped at $0.

> **GROUP POSITIONAL + `ssh` ACTION — COMMITTED `26184d5` + pushed (2026-10-14,
> zero-cost):** `utils/sl_admin.py` gained a **3rd positional (GROUP)** that selects a
> group **directly** (no table prompt) for the group-targeting actions
> (`start`/`stop`/`wait`/`stats`/`describe`/`delete`/`ssh`), and the 2nd positional now
> accepts `ORG/PROJECT`. A new **`ssh`** action opens an interactive session into a live
> instance of a RUNNING group. New `_resolve_group()` (direct GET when the project is
> named; org-wide search otherwise, erroring on ambiguous / not-found) and `_ssh_into()`
> (refuses a not-running group, finds the instance with a live `ssh_ip`/`ssh_port`, runs
> `ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p PORT root@IP` —
> ephemeral IP so host-key checking is off; propagates ssh's exit code). `describe` now
> resolves a named group via the same search when the project is unpinned. The parse loop
> is order-independent, so `start ma-casa-in-paris/llm atx-swift-27b-q5 --min-bw-mbps 100`
> works (flag consumed, group still resolves). Files: `utils/sl_admin.py`,
> `tools/test_group_ssh_offline.py` (NEW), `README.md`, `utils/README.md`. **Verified FREE
> only** (no paid start, no real ssh, no delete, no PATCH): **`python3 -I
> tools/test_group_ssh_offline.py` = 23/23 green** (resolve direct-GET vs search, positional
> selection, usage errors, flag ordering, ssh argv + not-running + no-live + exit-code,
> describe search/pinned) and **`python3 -I tools/test_gpu_prio_offline.py` still green**;
> `--help` renders the new usage/GROUP/ssh lines, exit 0.
>
> **NEXT UNFINISHED TASK (state 2026-10-14, `main` == `origin/main` @ `26184d5`):**
> zero-cost admin-surface work is exhausted — the group positional + `ssh` are in. What
> remains is ALL USER-GATED (paid): **① the first live `ssh` action** — start a group
> (paid), `wait` for it, then `sl_admin ssh ma-casa-in-paris/llm <group>` to confirm the
> interactive session lands (the argv is offline-verified; the live relay handshake is the
> untested bit, §9f). **② `start … --prio med` on `llm/qwen38-27b-q5`** (still `priority=low`,
> §9k) — the first live test of the card/priority recreate path (below). **③ the user's own
> `CHAT_TEMPLATE_URL` test** on `llm/qwen38-27b-q5` (unlocked, q6 v15) — set the key then a
> paid start. `llm/atx-swift-27b-q5` is **PATCH-locked** by its parked update (env edits 400
> until its next paid start).
>
> **CARD & PRIORITY FLAGS — COMMITTED `e228f71` + pushed (2026-10-14, zero-cost;
> `salad_client.py`, `utils/sl_admin.py`, `tools/test_gpu_prio_offline.py`, `README.md`,
> `utils/README.md`):**
> `sl_admin.py` gained **`--gpu CARD[,CARD…]`** (`rtx3080ti,rtx3080,rtx3090,rtx3090ti,
> rtx4080,rtx4090,rtx5090`), **`--prio low|med|high|batch`**, **`--query`** and
> **`--y`/`--force`/`-y`**. `deploy` passes both through as the deployer's own
> `--gpu`/`--priority` (free, `--no-start` kept). `start --gpu` prints the hourly
> prices, shows the card diff, confirms, PATCHes `gpu_classes` (a card change IS
> PATCH-able), and reports a parked PATCH honestly (a previously-started group lands
> the new card at the start this command is about to do). `start --prio` honours
> CREATE-only priority (§9k) as DELETE + CREATE under `<name>-<prio>` with the whole
> live config copied and the name-mirroring env rewritten (§9n), confirmed by typing
> the group name; `--force` skips every confirmation (and says so — it skips the gate
> on billing, not the billing). `--query --gpu …` is free: per-card USD/hour per tier
> + the availability counts (§9m), with `--project/--group` querying that group's
> resource floors. `salad_client.py` gained `GpuAvailabilityRequest` /
> `GpuAvailabilityInfo` / `get_gpu_availability`.
> **Verified FREE only** (no paid start, no delete, no PATCH sent): the guards, the
> client validation, `--query` live on both orgs (prices + tier counts, floors honored:
> 341/64/11 → 269/42/0), `start --gpu` diff + match, `start --prio` prompt + no-op,
> the recreate body. Suite **`python3 -I tools/test_gpu_prio_offline.py` = 72/72 green**
> (it found and fixed a real bug: `_fresh_name`'s suffix counter started at `-3`
> while its docstring promised `-2`). The destructive paths (the card PATCH landing,
> the delete+create) are exercised against a faked API only — they stay untested live
> by design, since a start is user-gated. **`llm/qwen38-27b-q5` is `priority=low`
> (preemptible, §9k)** — `start … --prio med` on it is the first live test of the
> recreate path.
>
> **NEXT UNFINISHED TASK (state 2026-10-12 late, `main` == `origin/main` @ `c2c07f3`, uncommitted:
> `PROGRESS.md` + root `README.md`):**
> items 1–6 below are all DONE / CLOSED / RESOLVED, and the **chat-template-url cycle is COMPLETE**:
> built + pushed on tr4, all five deployers re-pinned, and **all 5 live fleet groups image-only
> PATCHed onto the new digests and verified reading them** (`tools/patch_images_v13.py`; 5/5 with
> `pending_change=False`, env byte-identical 19/19/18/22/19 keys, zero cost — no start triggered).
> What remains is ALL USER-GATED:
> **① the user's own `CHAT_TEMPLATE_URL` test** — set the key on `llm/qwen38-27b-q5` (unlocked, on
> q6 v15) with `python3 -I tools/sl_set_env.py ma-casa-in-paris/llm/qwen38-27b-q5 CHAT_TEMPLATE_URL=<raw-url> --dry-run`
> (drop `--dry-run` to send), then a paid start. `llm/atx-swift-27b-q5` is **PATCH-locked** by its
> parked update — env edits there 400 until its next paid start.
> **② item 3's aftermath** — why the group ran ~19 min past ready without self-stopping
> (`HEARTBEAT_TIMEOUT=300`): read `/tmp/llama-api/watchdog.log` **BEFORE** stopping at the next paid
> start, then decide if the SSH guard's 1200 s cap explains it (it should: 19 min < 20 min) or the
> watchdog failed to arm. **③ tr4 is at `d1f5bff`** — `ssh b760one "cd /dd2/andrei/docker/on_salad && git pull"`
> before the next build cycle.
> Zero-cost work is exhausted: every group is queued on the chat-template-url images, so the next
> paid start of any of them runs the new set. `utils/sl_admin.py describe` now dumps any group's
> whole config (image/digest, resources, GPU names, CMD, probes, full env with secret VALUES
> redacted, instance ids + ssh lines) — the read-only side of every future check.
> **Root `README.md` rewritten 2026-10-12 (user's call, uncommitted):** the `## Most useful` section and
> the tail are GONE; a new **`## TD;LR`** section sits **before `## Layout`** with the full command
> sequence from a freshly created Salad account to Claude Code on a Salad GPU (web UI org/project →
> keys on disk → `create-org`/`create-project --register-only` → `deploy --type q6 … --no-start` (free)
> → `start --min-bw-mbps 100` (paid) + `wait` → `sl_curl.sh` smoke test → `sl_claude_deploy` /
> `cl_salad`). Intro prose and the `## Layout` bullets kept verbatim (the `describe` bullet updated).
>
> **ADMIN SURFACE (2026-10-12, zero-cost, uncommitted at write time):**
> `utils/manage_groups.py` → **`utils/sl_admin.py`** (`git mv`; every reference in
> README / utils / docs / docker / deploy / tools updated — 0 occurrences of the old
> name remain). New actions: **`deploy`** (numbered DOCKER-TYPE menu
> `q6|generic|ampere|9b|qwen9b` → org → project → group; `--type/--org/--project/--group`
> skip a prompt; flags after `--` are forwarded verbatim; runs the deployer with
> `--no-start` so it is FREE, `--start` needs a typed `y`; the 9B deployers accept no
> `--no-start` — they create STOPPED and never bill) and **`create-org` /
> `create-project`** (the public API cannot create orgs/projects — live-probed 404,
> `tools/probe_org_create.py`; so: portal attempt via the billing vault, `--no-portal`
> to skip, then REGISTER in `utils/fleet.json`, which now overlays `KNOWN_PROJECTS`;
> `--register-only` after a web-UI create). Verified offline: arbitrator harness still
> 26/26 on the renamed module, `list` unchanged, the paid gate aborts before any API
> call, and name validation + fleet.json merge/clobber-guard were exercised in a temp dir.

> **CUSTOM TEMPLATE FROM A URL (2026-10-12, COMMITTED `ef0c49d` + pushed):**
> `CHAT_TEMPLATE_URL` is the **third template source** — `docker/chat_template_url.sh`
> downloads a template at startup, validates it, serves it via `--chat-template-file`; it
> **wins over `CHAT_TEMPLATE` and `CLAUDE_TEMPLATE`**, the Claude patch is **not** applied to it,
> and a rejected download **kills the container** (STOPPED group, not billed — §8). Wired: all
> four entry scripts, all four Dockerfiles (`ENV CHAT_TEMPLATE_URL=none` baked + build ids
> generic **v13** / q6 **v15** / ampere **v11**), `version.sh` fingerprint, all five deployers
> (`--chat-template-url`, config-time rc2 guard), docs (`docker/`, `deploy/`, `docs/`, root
> `README.md`, `tools/hub_describe.py`). Offline suite `tools/test_chat_template_offline.py`
> **46/46 green**. Canonical test URL = this repo's own template
> `https://raw.githubusercontent.com/suedenglehem/docker4salad/main/docker/qwen3.8.q6.jinja`
> (live-verified on the host: 8941 bytes, sha256 `e8912f4cc41c`, byte-identical, no NOTE).
>
> **IMAGE CYCLE — DONE 2026-10-12 (user-authorized push from tr4):** all four images built on
> tr4 (`b760one`, 32 CPUs, `--build-arg BUILD_JOBS=24`, 42 min wall, log `/tmp/build_chat_url.log`,
> script `tools/build_chat_url_images.sh`), pushed from tr4 (its own Hub login), each repo's
> `full_description` PATCHed, and **all five deployers re-pinned** to the Hub digests with
> `tools/sl_repin.py` (§4 table; `test_deploy_offline.py` green after the re-pin). Verified
> **in-container** on the built q6 image: the canonical raw URL → `OK 8941 bytes
> sha256:e8912f4cc41c` (byte-identical to `docker/qwen3.8.q6.jinja`), and the GitHub *blob* URL →
> `REJECT … starts like an HTML document` with the caller killed under `set -e` and **no output
> file** — the hard-fail semantics proven inside the shipped image, not just on the host.
> **So `--chat-template-url` is live-capable now, at the next paid start of a group carrying these
> pins.** The live fleet groups are NOT moved by a deployer re-pin — moving them needs the free
> image-only PATCH, which was then **RUN ON THE WHOLE FLEET (user-authorized, 2026-10-12,
> `tools/patch_images_v13.py`)**: 5 live groups enumerated via `sl_admin._fleet()` (the ampere
> groups are gone — both ATX prod groups deleted), family kept per group (q6→q6, generic→generic,
> base→base), **env byte-identical on all 5** (19/19/18/22/19 keys). Result: **5/5 read back the new
> digest with `pending_change=False`** — `qwen38-27b/{q5,q6k,qwen38-9b}` → q6 v15 / q6 v15 / base v13,
> `llm/qwen38-27b-q5` → q6 v15, `llm/atx-swift-27b-q5` → generic v13 (v12). No start triggered,
> nothing billed. `atx-swift-27b-q5` (the only group with a paid start in its history) read as
> **[STAGED]** during the run — old image in the GET, `pending_change=True`, repeat PATCH → 400 — and
> ~20 min later the SAME GET showed the new digest with `pending_change=False`. **A parked update
> becomes visible in the GET without any start**: "parked" means *further PATCHes are refused* and
> *the queued spec is what runs at the next start*, not that the spec stays hidden.
>
> **API FACTS THIS RUN ESTABLISHED (see §9e — both are corrections):**
> ① the GET response has **no `pending_update_in_progress` field at all** (keys: `pending_change`,
> `current_state`, `version`, `update_time`, …) — a guard reading it is a silent no-op; the parked
> state is visible **only on the write path** as **400 + ProblemDetails
> `type:"pending_update_in_progress"`**, so a same-digest repeat PATCH is a free no-op *probe*.
> ② the eventual-consistency window is **minutes, not seconds**: re-GETs at 1–8 s after a 200 still
> showed the OLD image with `pending_change=True`; the new digest read back only after ~3 min.
> `patch_images_v13.py` therefore POLLS the readback (`--settle=SECONDS`) and classifies both end
> states — `[OK]` (digest reads back) and `[STAGED]` (400-parked = queued for the next start) —
> as success, and asserts env equality on both.

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
     before trusting it.) Offline suites — watchdog 7/7, arbitrator 26/26,
     deploy green (re-run 2026-10-13 from `tools/`, all green).
2. **Paid arbitrator live test:** `python3 utils/sl_admin.py start
   ma-casa-in-paris --min-bw-mbps 10000` (atx-hb-test2, user-run) — watch it
   `POST /reallocate` on a slow download (≤3 shots). §6. **Status 2026-10-11
   — attempt 1 hit the guard false-positive, now fixed; the rerun was in
   flight when the tr4 reboot killed it — see outcome below.**
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
   **Rerun confirmed in flight (~04:29Z, fixed code):** the user's terminal
   showed `image pull 0.70000833% (84s)` — a DISPLAY bug (the API sends a
   0–1 fraction, not a percent; now rendered `70.0%`, fixed in this same
   cycle) — and the new instance `b85289df` placed on
   FRESH node `84b984b8` (neither attempt 1's 0bf20a53 nor the fast node).
   Pull completed (1.0), model download started, `ready=False` at last
   read. The tr4 reboot (~04:3xZ) killed the local `sl_admin.py`; the
   group ran unmanaged, and its in-container v2.3 watchdog
   (`IDLE_SHUTDOWN=heartbeat`, `HEARTBEAT_TIMEOUT=300`, `IDLE_GRACE=3600`)
   then self-stopped it — billing stopped. **Reboot lesson:** after the build
   host reboots, do NOT panic-attach (no immediate `start`/attach re-run) —
   first a READ-ONLY group GET: the group may still be running unmanaged (and
   billing), and its in-container watchdog may self-stop it while you attach.
   **Resume outcome (read-only
   state check):** `atx-hb-test2` stopped, 0 instances — the run ended
   without ever reaching a bandwidth verdict on the terminal we have; the
   user's terminal log (shot count + per-poll sample lines) is still
   pending from the user. The run-1 sample lines the user pasted (0.0
   stalls interleaved with 300–1900 Mbps bursts) are what root-caused §15.6
   — the 0.0s were xet
   CAS-cache transfers that never grew MODEL_DIR, not a quiet wire.
   **Attempt 3 (user, ~06:08Z, v2.5 set, cap 10000) — bailed on ssh strikes;
   root cause = public-key denial, fix deferred (user, verbatim: "don't look,
   it's ssh problem. public key denied. will fix it later").** Acquired
   b9c4bc30 on fresh node `71e4e672-a278-095d-9f4c-8a01620aeb46` at
   06:09:15Z (no `image pull` lines — the instance was already `running`,
   pull 1.0, on the first poll; the image was evidently cached on that
   node). All four probes were TCP-level `conn (TimeoutExpired)` —
   06:09:30 / 06:10:06 / 06:10:41 / 06:11:17Z, ~35 s apart (= 15 s poll +
   20 s probe timeout) → verdict `ssh` → `bail: 4 consecutive ssh failures`,
   arbitrator exit 3. Bail semantics (from code, re-verified): the group is
   NEVER stopped by the arbitrator, no shot is spent, and there is no
   reallocate on an ssh bail — it kept billing unmanaged until stopped.
   Post-bail read-only diagnosis: (a) the instance had churned mid-test to
   c319ae15 on the SAME node (Salad recreated the container; the
   arbitrator's `churn` path would have re-acquired it had it not bailed);
   (b) a manual probe (the arbitrator's exact `tail -n 80
   /tmp/llama-api/bw.log`) on c319ae15 returned rc 255 `Permission denied
   (publickey)` — the relay now LISTENED (no longer a TCP timeout) but
   rejected the key. → the node's Salad ssh key config is the root cause;
   the fix is the user's, deferred. Final state (read-only): group
   `stopped`, 0 instances — it stopped between the first probe (saw
   c319ae15 running) and the ~10-min-later retry loop (who/what stopped it:
   UNKNOWN — user manual stop or Salad-side); billing stopped. The v2.5
   3-column reporter was therefore never live-verified on a wire (bw.log
   unread); the next run on a healthy node is the first chance. Candidate
   hardening (NOT implemented, user's call): the 4-strike budget is ~2 min
   from acquire, so a short grace before strikes count would survive slow
   relay starts — though this run's actual cause was auth denial, not lag.
   **CLOSED 2026-10-13 — hardening NOT needed (user: "i confirm, it works").**
   The arbitrator ran live on a healthy node with the v2.5 set and behaved:
   `bw.log` read over the relay, ONE `/reallocate` spent (instance 4c86e5f3
   @87804ed4 → 700ecc4a @7429adb8 at 00:37:17Z), that host kept, ready
   00:49:03Z, group never stopped. The public-key denial is gone, so the
   strike path is cold. Revisit the grace window only if a real slow-relay
   bail shows up (4 strikes in ~2 min from acquire, exit 3, no shot spent).
   The pre-reboot rerun terminal log (this item's pending artifact) remains
   un-pasted.
3. **Verify prod ATX context:** at the next start of `atx-swift-27b-q5-opt`,
   scrape `/metrics` and confirm `llama_model_n_ctx = 132864` (spec CTX is 132768).
   §7. **Attempt #1 2026-10-12: ready reached, no scrape** (my `endpoint`-field
   bug; URL is `networking.dns`), group stopped 23:31:54Z, cause unknown with
   `priority=low` preemption as the leading candidate. Script fixed (dns + SSH
   `:8080/metrics` fallback + start-retry for a fresh `pending` group); attempt #2
   runs on the recreated **`atx-swift-27b-q5-med`** (priority=medium).
   **Attempt #2 2026-10-13: placed fast (pull 100 % at 142 s — ampere v10 already
   cached on machine `702ff4d9`), then the USER stopped it for slow network**
   (verifier killed; no verdict, no argv capture). Next start is the arbitrator's
   occasion, per the user: `sl_admin.py start ma-casa-in-paris
   --min-bw-mbps 100`. Verification is then two facts, in this order: (1) the
   llama-server **argv** over SSH — `nvidia-smi --query-compute-apps=pid
   --format=csv,noheader`, then `cat /proc/<pid>/cmdline | tr '\0' ' '` (the ampere
   image has NO procps, so `ps` is absent, and the relay is shell-less: post-process
   on OUR side); (2) the `llama_model_n_ctx` gauge as the served-value confirmation.
   With `--fit off` baked in the CMD, llama.cpp does not shrink ctx to fit — a KV
   that does not fit fails at allocation — so "server up + `--ctx-size 132768` in
   argv" is essentially conclusive on its own.

   **DONE 2026-10-13 — VERIFIED (attempt #3, arbitrator start, user-launched
   `sl_admin.py start ma-casa-in-paris --min-bw-mbps 100`):** the arbitrator
   ran live for the first time and **reallocated once** (instance `4c86e5f3` @
   `87804ed4` → `700ecc4a` @ `7429adb8`, 00:37:17Z); ready 00:49:03Z. Two proofs:
   (1) **argv** (the user's `ps` method, via `nvidia-smi` pid → `/proc/399/cmdline`):
   `--ctx-size 132768`, `--chat-template-file /tmp/llama-api/claude_chat_template.jinja`,
   `--cache-type-k turbo5 --cache-type-v turbo4 --kv-unified --fit off --cache-ram 4096`,
   `--spec-type draft-mtp` + `--spec-draft-vocab-map …/atx_65536.txt`, `--n-gpu-layers 99`,
   `--threads 8`, and the EXTRA_ARGS tail applied last-wins (`--spec-draft-n-max 3`
   over the baked 5, `--temp 0.2 --top-k 40 --repeat-penalty 1.08 --repeat-last-n 256`).
   (2) **served ctx: `/slots` → slot 0 `n_ctx` = 132864** — exactly 132768 rounded up
   to a 128-block. VRAM 23067/24576 MiB (1.5 GiB headroom — the 132K turbo KV fits a
   3090, as designed). **Method correction: this llama.cpp build's `/metrics` has NO
   `n_ctx` gauge** (counters + `n_busy_slots_per_decode` only) and its `/props` is
   trimmed (no `params`/`slots` keys) — the served context is **`/slots`**, §9l.
   **Aftermath:** the group ran ~19 min past ready WITHOUT self-stopping despite
   `HEARTBEAT_TIMEOUT=300` — consistent with the **SSH guard** holding the watchdog
   (`SSH_GUARD=1`, `SSH_GUARD_MAX_HOLD=1200` = 20 min cap) during the user's relay
   session; NOT proven (the container was torn down before the log was read — read
   `watchdog.log` BEFORE stopping next time). Then stopped + DELETED on the user's
   instruction (billing off; `atx-swift-27b-q5-med` is 404, name tombstoned ≥10 min).
   The verified config is not lost: it IS the `deploy_ampere.py` defaults, so a bare
   `--no-start` run recreates it free (the paid start stays the user's call).
   `atx-swift-27b-q5-opt` survives, stopped, at **priority=low**.
4. **DONE (2026-10-11):** the v2.3 set (base v10 / generic v10 / q6 v12 /
   ampere v8) is re-pinned across the fleet — all 7 groups image-only PATCHed
   (env asserted byte-identical before/after, no starts;
   `tools/patch_images_v10.py`). The first paid start of each group runs v2.3.
5. **DONE (2026-10-11) — v2.4 shell-ergonomics cycle (build + push + re-pins
   + fleet re-PATCH, all verified).** Root cause: the Salad ssh relay execs a
   bare `/bin/bash` on a pty with `TERM=xterm` (breaks nvtop/htop: "Error
   opening terminal: unknown") but DOES read `/root/.bashrc` (verified live:
   skel PS1/HISTCONTROL/`ll` all loaded). Fix baked in the image's final
   stage: `export TERM=xterm-256color` + aliases `nv/ht/gpu/wlog/alog/ready/
   als/ll/la/l/h` appended to /root/.bashrc in `Dockerfile.multistage` and
   `Dockerfile.lmss_generic_ampere`; generic/q6 inherit via their FROM
   re-pin. `tools/build_push_v24.sh` (32 jobs, sequential: base cuda128-v11 →
   generic cuda128-v11 → q6 cuda128-v13 → ampere cuda130-v9) built + pushed
   in **~14 min** (WARM layer cache from the v2.3 cycle — only the .bashrc
   RUN block and later layers rebuilt; corrects §4's "no cache" assumption
   for this build form). Hub digests (digest-verified from push output):
   base `2bfbd69c…`, generic `888fc401…`, q6 `f6e3b9d0…`, ampere
   `d4f4b030…` (full digests in `tools/build_push_v24.sh` output + the
   deployers). Both derivative FROMs re-pinned mid-flight (match count 1
   each); the **5 deployer IMAGE constants** re-pinned to the new Hub digests
   (old digests kept in the history comments). Fleet re-PATCH **7/7 `200 OK`**
   via `tools/patch_images_v11.py` (env asserted byte-identical before/after,
   **no starts**). **Propagation is ASYNC**: the immediate re-GET shows the
   OLD digest and a re-PATCH 400s `pending_update_in_progress` even while the
   group GET's `pending_update_in_progress` field is falsy — so verify with
   READ-ONLY GET polling (`tmp/check_images_v11.py`, prints 'ALL LANDED' at
   7/7), never a re-PATCH. Watchdog unchanged (still v2.3); next live benefit
   = nvtop/htop + aliases over the relay on any group's next start.
   `restart_policy`/`priority` are CREATE-ONLY, so any change to those still
   requires delete+recreate (§9c: use a fresh name, keep `MODEL_ALIAS` stable).
6. **RESOLVED (2026-10-11, code committed 55aac06) — the bandwidth 0-Mbps
   discrepancy: xet, not a quiet wire.** bmon inside the container showed
   steady traffic while the manager reported 0 Mbps (both test nodes,
   0bf20a53 and 84b984b8). **Root cause:** the images bake
   `HF_XET_HIGH_PERFORMANCE=1`, and xet moves most shard bytes through its
   CAS cache **OUTSIDE** `MODEL_DIR` — the dir grows only when a finished
   shard is finalized. The v1 reporter's MODEL_DIR-growth proxy therefore
   read 0.0 through the long xet phases and spiked at finalizations (the
   run-1 pattern: 1081.5/616.7/0.0/877.3/0.0/295.7/0.0/1874.5 Mbps). Ruled
   out first: the rate math (`bytes*8/dt/1e6` — correct) and a MODEL_DIR
   mismatch (13.8 GB did land in MODEL_DIR in ~3 min). **Fix:** the
   reporter logs `epoch rx-bytes model-bytes`; the kernel NIC RX counter
   (`/sys/class/net/<if>/statistics/rx_bytes`, summed over non-lo) is the
   bandwidth source — the exact counters `bmon` reads, read directly (no
   binary, no extra process, our own epoch timestamps over the shell-less
   relay; the old anti-bmon "NIC noise" objection is KB/s, negligible vs an
   Mbps cap over a 120 s median). The model column is the human-facing MB
   progress display only. The manager rates from the rx column and parses
   the legacy 2-column log as rx := model, so a mixed fleet still measures
   (at v1 accuracy) until the fleet is re-imaged. Offline suite 26/26
   incl. two new legacy-format scenarios.
   - **The verdict, not just the display, was at risk:** the 0.0 intervals
     poison the 120 s median too — a node with long xet phases could median
     near 0.0 and eat reallocate shots it didn't deserve.
   - **Per-poll display note:** the Mbps column is the LAST 10 s interval
     (`rates[-1]`); with the rx counter it is now real per-interval wire
     speed (much closer to what bmon shows) instead of flat model-growth
     deltas.
   - **bmon units:** the user's "~10 Mbps" figure is no longer needed to
     discriminate — we read the same kernel counters bmon reads, so any
     residual gap is display/interval, not source.
   - **DONE (2026-10-11) — the v2.5 set carries the fix:** built + pushed +
     digest-verified on tr4 (32 jobs, ~14 min warm, 05:22→05:36Z), both
     derivative FROMs + all 5 deployer IMAGE constants re-pinned, fleet
     re-PATCHed 7/7 `200 OK` (env byte-identical, no starts), all 7 digests
     verified landed via read-only GETs. Only NEW instances (next paid
     starts) run the fixed reporter; the manager side was already live.
     Full digests: §4.

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
  **never trust a transitional/empty API GET** for spec state (§9f/9g), and
  remember an in-flight one returns a TRUNCATED bare digest — compare the full
  `sha256:` suffix, never string equality (§9i).
- **Digest-pin images** to move them past Salad's cache (§9a).
- **Downloaded/extracted files are untrusted** — put each in its own empty dir,
  keep your scripts elsewhere, and run Python that reads them with `python3 -I`.
- **`SALAD_STOP_KEY` in the container is user-approved** (account key, b64).

---

## 17. Recent git history (context for what each commit did)

```
26184d5 feat(sl_admin): GROUP positional + ssh action — 3rd positional selects a group directly; `ssh` opens a live session (test_group_ssh_offline 23/23)
73fad6e readme
7dee551 readme
e228f71 feat(sl_admin,salad_client): --gpu CARD / --prio PRIO / --query / --force (test_gpu_prio_offline 72/72)
9b58599 readme
6c98430 docs(readme): TD;LR run sequence — fresh Salad account to Claude Code on a GPU
c2c07f3 feat(tools,sl_admin): fleet image re-pin + whole-config `describe`
d1f5bff feat(images): chat-template-url set built+pushed on tr4; deployers re-pinned (v13/v13/v15/v11)
ef0c49d feat(chat-template): CHAT_TEMPLATE_URL — download a custom chat template from a URL
7da370f feat(sl_admin): create-org / create-project / deploy menu; tool surface renamed to sl_*
a9664a3 docs: PROGRESS.md savework checkpoint — §15 NEXT-task block, §12 tree de-staled, §17 + 428ed5a/fa0e06c/801520e
428ed5a docs: PROGRESS.md §15 — arbitrator ssh-strike grace hardening CLOSED as not needed (user: "i confirm, it works")
fa0e06c readme (user): README line + claude/cube, claude/cube.cpp DELETED from the tree
801520e readme (user): claude/cube (17 KB CUDA experiment binary) + cube.cpp ADDED — the binary stays in git history even after fa0e06c; only a history rewrite removes it
84f1de3 README: the "Most useful" 3-step flow (create group → sl_admin.py start --min-bw-mbps → cl_salad <dns>) + docker4salad docker/README.md link + auto-stop warning (user's own wording)
2a5ed08 cl_salad: verify OUR proxy owns the local port (nonce echoed on /healthz) — a second session exits 3 instead of hijacking the first session's proxy; + deploy/README.md command lines
8443c93 tools/: promote durable tooling out of gitignored tmp/ — §14 paths now resolve on a fresh clone; one-shot attempt artifacts deleted; test_deploy_offline + fleet_status_now de-staled
bd6d612 docs: PROGRESS.md — mycelium fold for cold-start on another box (attempt-3 outcome, §9i in-flight truncated-digest API gotcha, nproc trap, arbitrate_attach/reboot-lesson, §16 ref)
49e8585 docs: PROGRESS.md — v2.5 set current (xet fix shipped to the fleet), 870bc05 in history
870bc05 images: v2.5 set pushed (base/generic v12, q6 v14, ampere v10) — bw_reporter v2 kernel-RX (xet fix) + re-pins
06ed2ba docs: PROGRESS.md — xet root cause + RX-counter fix (55aac06), drop-test ended ($0), open item 6 resolved, history backfilled
55aac06 bw: measure from the kernel RX counter, not MODEL_DIR growth (xet fix)
b5177cc docs: PROGRESS.md — rerun in flight on fresh node 84b984b8 (fixed code), pull-% display fix, state at tr4 reboot
b0b3800 arbitrator: render image pull progress as a real percent
197b6d7 docs: PROGRESS.md — open item 6: bmon (in-container) vs manager 0 Mbps bandwidth measurement discrepancy, investigation deferred
111dff5 docs: PROGRESS.md — /.ssh guard false-positive (relay stderr-merge) found live + fixed b9eae88
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
