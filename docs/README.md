# docker4salad — user guide

Deploy **Qwen3.8 GGUF models on [SaladCloud](https://salad.com) GPUs** behind an
OpenAI-compatible `llama-server`, and drive them from **Claude Code**.

The pieces that make it useful are the scripts in this repo. This page starts
with what each script does, then walks through the two things you'll actually
do day-to-day — *run Claude Code against the model* and *smoke-test it with
curl* — and ends with the gotchas that cost real time to discover.

The two keys to the whole setup:

- **Claude Code talks Anthropic; the model talks OpenAI.** The Salad container
  runs `llama-server` (OpenAI-only: `/v1/chat/completions`, `/v1/models`). Claude
  Code speaks the Anthropic Messages API (`/v1/messages`). `salad_proxy.py`
  translates between the two — you never do it by hand.
- **The Salad gateway needs a key on every request.** The group's container
  gateway has auth on, so each request must carry a `Salad-Api-Key` header.
  Claude Code can't set that header itself, so `cl_salad` runs a local proxy
  that injects it for you. You never add the key manually.

---

## At a glance

| Script | Where | What it does |
|---|---|---|
| **`cl_salad`** | `claude/` (also `/usr/local/bin/cl_salad`) | **The Claude Code runner.** Checks the gateway once, **dies if it's dead**, then starts the proxy and runs Claude Code against the model. |
| **`cl_salad_deploy`** | `claude/` | **The deploy half.** Looks up the group's status, starts it if stopped (costs), waits for the model, then hands off to `cl_salad`. |
| **`salad_proxy.py`** | `claude/` | Stdlib Anthropic↔OpenAI bridge. Injects `Salad-Api-Key`, translates requests + streaming SSE + tool calls. Run by `cl_salad`. |
| `version.sh` | `docker/` | Run **inside a running instance** (via SSH) to confirm *which build* is actually live and what it's downloading. |
| `stats.sh` | `docker/` | Run **inside a running instance**: llama-server stats — tps, queue, token totals (scrapes the local `/metrics`). |
| `utils/llama_stats.py` | `utils/` | Same stats **from the host**, against a Salad gateway URL (key auto-loaded) or a local server. |
| `curl_salad.sh` | `claude/` | Quick smoke test — asks the running model 2-3 simple questions through the gateway. |
| `salad_client.py` | repo root | Stdlib-only SaladCloud OpenAPI client (create/start/stop/delete/patch groups, GPU classes, projects). |
| `deploy/deploy_qwen38_27b.py` | `deploy/` | **Canonical 27B deployer** — creates/updates the `qwen38-27b-q6k` group (baked MTP + vision image, main model fetched at runtime) and starts it. |
| `deploy/deploy_generic.py` | `deploy/` | Model-agnostic deployer on the **vanilla `lmss_generic` image**: any GGUF via `--model-repo` / `--model-file`, draft + vision opt-in, `--gpu` takes any card the org lists. |
| `deploy/deploy_ampere.py` | `deploy/` | Deployer for the **llamAmpere fork image** (turbo5/turbo4 KV cache — full 132k ATX ctx on a 24 GB card; ATX-Swift production profile is the built-in default; `--gpu` limited to the org's sm_86 30-series cards). |
| `deploy/deploy_qwen38_9b.py` / `deploy/deploy_qwen9b.py` | `deploy/` | 9B deployers (`qwen38-9b`, `qwen9b`) — plain, no draft/vision/template. |

Everything runs against org **`ma-casa-in-paris`**, project **`qwen38-27b`**.

---

## The scripts

### `cl_salad` — run Claude Code against the Salad model

The thing you'll type. It mirrors `/usr/local/bin/clov` (a wrapper that points
the `claude` CLI at a local model), except it speaks to a Salad public gateway,
so it first stands up the local `salad_proxy.py` and lets the proxy carry the
`Salad-Api-Key`.

It is a **dumb runner**: it does not start or manage the group. It checks the
gateway **once** and **dies immediately if it is not serving** (exit 2) — that
is the whole contract. To start a stopped group and wait for the model, use
`cl_salad_deploy`.

The **gateway URL is the first argument** — it's the one thing that changes when
the group is recreated, so you point `cl_salad` at a new group by passing the new
DNS, not by editing the script:

```bash
cl_salad                                        # built-in default gateway (below)
cl_salad https://corn-cabbage-2yk4e98r3rx752n0.salad.cloud   # point at a group
cl_salad https://<new-group-dns> -p "Say hello"  # + pass claude flags through
```

A bare host (no `https://`) works too. Omit the URL to use the built-in default
(or the `SALAD_GATEWAY_HOST` override). Everything after the URL is passed
straight to `claude`.

What it does, in order:

1. Resolves the **gateway URL** — first argument if given, else
   `SALAD_GATEWAY_HOST`, else the built-in default — and its own **real**
   directory (follows the `/usr/local/bin` symlink) so it finds `salad_proxy.py`
   and `salad_api.txt` next to the real file.
2. Reads the Salad key from `salad_api.txt` — **never printed**; exits if empty.
3. **Liveness check, one shot:** `GET /v1/models` with the key. Only `200`
   (group running **and** model loaded) continues; anything else exits 2 with a
   per-code hint — `404` group stopped / model still loading → run
   `cl_salad_deploy`; `403` gateway rejects the group (deleted / wrong URL or
   key); `000` host unreachable (DNS / connection).
4. Starts `salad_proxy.py` on `127.0.0.1:8093` (loopback only — it holds the key)
   pointing at `https://<gateway>/v1/chat/completions`, and waits for `/healthz`.
5. Exports `ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` and the token caps, then
   runs `claude --permission-mode bypassPermissions --model qwen38-27b "$@"`.
6. Kills the proxy on exit.

**Environment overrides** (all optional):

| Var | Default | Meaning |
|---|---|---|
| `SALAD_GATEWAY_HOST` | `corn-cabbage-2yk4e98r3rx752n0.salad.cloud` | The group's public gateway. A `GATEWAY_URL` first argument beats this. **Changes when the group is recreated.** |
| `SALAD_MODEL_ALIAS` | `qwen38-27b` | Model name sent upstream (the served `--alias`). |
| `SALAD_KEYFILE` | `<script dir>/salad_api.txt` | Where the `Salad-Api-Key` is read from. |
| `SALAD_PROXY_PORT` | `8093` | Local proxy port. |
| `SALAD_ENABLE_THINKING` | `0` | `1` to enable Qwen thinking (off by default → clean answers). |
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | `64000` | Input token cap (see [token caps](#token-caps-vs-context-length)). |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | `16000` | Output token cap. |

### `cl_salad_deploy` — start the group, wait, then run

The deploy half, split out of the old all-in-one `cl_salad`. It looks the group
up via the management API, **starts it if it's stopped** (a fresh start
re-downloads the model and **incurs cost** — it warns first), waits until the
gateway serves the model, then hands off to `cl_salad` with the resolved
configuration exported — so the proxy / claude logic lives in exactly one place.

```bash
cl_salad_deploy                                  # defaults = prod group
cl_salad_deploy --group qwen38-27b-q5            # the v5 test group
cl_salad_deploy --org akl-on-salad --project comfy --group qwen38-27b-q5
cl_salad_deploy --url https://<dns>              # other gateway / new DNS
cl_salad_deploy --status                         # print group status, exit
cl_salad_deploy --no-run                         # start + wait, no claude
cl_salad_deploy --group qwen38-27b-q5 -- -p "Say hi"
```

**Options** (an option beats the env var, the env var beats the default; options
must come *before* any claude argument — the first non-option token ends option
parsing and everything from there goes to claude; `--` ends options early):

| Option | Env var | Default | Meaning |
|---|---|---|---|
| `--url URL` | `SALAD_GATEWAY_HOST` | looked up from the group (`networking.dns`) | Gateway URL or bare DNS. |
| `--org ORG` | `SALAD_ORG` | `ma-casa-in-paris` | Salad org. |
| `--project NAME` | `SALAD_PROJECT` | `qwen38-27b` | Salad project. |
| `--group NAME` | `SALAD_GROUP` | `qwen38-27b-q6k` | Container group. |
| `--model ALIAS` | `SALAD_MODEL_ALIAS` | `qwen38-27b` | Served model alias. |
| `--keyfile PATH` | `SALAD_KEYFILE` | `<script dir>/salad_api.txt` | Salad key file. |
| `--port PORT` | `SALAD_PROXY_PORT` | `8093` | Local proxy port. |
| `--thinking 0\|1` | `SALAD_ENABLE_THINKING` | `0` | Enable Qwen thinking. |
| `--context-tokens N` | `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | `64000` | Input token cap. |
| `--output-tokens N` | `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | `16000` | Output token cap. |
| `--timeout SECS` | — | `2700` | Readiness wait (covers a cold start: image pull + ~18 GiB model download). |
| `--no-run` | — | off | Start + wait, then exit without launching claude. |
| `--status` | — | off | Print the group's status and exit. |

While waiting, it checks the group status every ~60 s and bails early if the
group flips back to `stopped` (it is failing to start — check the Salad UI
rather than burning the timeout).

### `salad_proxy.py` — the Anthropic↔OpenAI bridge

Stdlib-only (no dependencies), loopback-only. It's what makes a raw
`llama-server` usable by Claude Code.

- Listens on `127.0.0.1` (default `:8093`). Serves `POST /v1/messages`,
  `GET /v1/models`, `GET /healthz`.
- Translates an Anthropic request → OpenAI `/v1/chat/completions`, injecting the
  `Salad-Api-Key` header (read from a file, never printed) on every upstream call.
- Translates the response back to Anthropic — **streaming SSE**
  (`message_start` → `content_block_*` → `message_delta` → `message_stop`) or JSON.
- Maps `tool_use` / `tool_result` ↔ `tool_calls` / `role:tool`, preserving IDs, so
  agentic tool round-trips work.
- Disables Qwen "thinking" by default (`chat_template_kwargs.enable_thinking=False`)
  so answers aren't a long reasoning preamble.

Run it directly if you don't want the wrapper:

```bash
python3 claude/salad_proxy.py \
  --upstream https://<gateway>/v1/chat/completions \
  --model qwen38-27b \
  --keyfile claude/salad_api.txt \
  --port 8093 --thinking 0 --max-tokens-cap 12000
```

Then point anything at `http://127.0.0.1:8093`.

### `version.sh` — prove what is actually running

Run it from an **SSH session into a live instance** (Salad lets you attach a
shell). In seconds it tells you:

- **Which build** is running (baked build id — the worker image cache is keyed by
  repo *name*, so a worker can serve an older image under the same name; this is
  the only fast way to know for sure).
- Whether the **sentinel CMD** is live (draft/vision/template OFF unless set).
- The **effective feature env** and the PID-1 `llama-server` cmdline (sha256).
- What's in `/models` and what is **downloading right now** (`wget2` / `hf download`).

```bash
ssh <salad worker>            # from the Salad UI / CLI
version.sh                     # → build id, sentinel guards, /models, downloads
```

### `stats.sh` / `llama_stats.py` — server stats (tps, queue, totals)

Scrapes `/metrics` (every Dockerfile enables `--metrics`):

```bash
# inside a running instance (Salad SSH or docker exec):
stats.sh                     # one-shot
stats.sh --interval 2        # live view (Ctrl-C to stop)

# from the host, against a Salad gateway (key auto-loaded from deploy/salad_api.txt):
python3 utils/llama_stats.py https://<group>.salad.cloud
```

Generation/prompt tps, processing/deferred requests, token totals, max sequence
length, and — when a draft model is enabled — spec-decode accept rate. `--raw`
dumps the raw Prometheus body; `llamacpp:*` metrics not rendered above are
listed under "other" so a rename in a future llama.cpp build is visible.

### `curl_salad.sh` — quick smoke test

Asks the running model a couple of simple questions through the public gateway.
Fastest way to confirm the model is up and answering.

```bash
./claude/curl_salad.sh -url https://<gateway> -m qwen38-27b
```

- `-url` — the gateway access domain. `-p` — public port, **default 443** (the
  domain is Cloudflare-fronted; **never** use `8888`, the in-container port).
- `-m` — the model name in the request body (`qwen38-27b`, or `qwen` — the server
  matches loosely). `-api` — optional llama-server Bearer key (the deployed group
  runs without one, so it's omitted).
- It reads `salad_api.txt` itself and sends it as `Salad-Api-Key`.

### `salad_client.py` — the SaladCloud API client

Stdlib-only client for the Salad OpenAPI. Every deploy/update script imports it.
Public surface (see the file for full docs + the `...Request` dataclasses):

- `load_api_key()` — reads `salad_api.txt` (never printed).
- `get_container_group` / `create_container_group` / `update_container_group`
  (PATCH) / `start_container_group` / `stop_container_group` /
  `delete_container_group`.
- `list_gpu_classes` — resolve a card like `rtx5090` to its class UUID.
- `create_project`, and a typed `SaladApiError` with parsed problem details.

Base URL is `https://api.salad.com/api/public`.

### Deploy & update scripts

All read the Salad key from `deploy/salad_api.txt` and (optionally) a HF token
from `deploy/hft.txt` (validated against the Hub, added to the group env,
never printed).

- **`deploy/deploy_qwen38_27b.py`** — the **canonical** 27B deployer. Create-or-update the
  `qwen38-27b-q6k` group on the baked **q6-mtp-vision** image (digest-pinned v5):
  only the main model downloads at runtime (default
  `Qwen3.8-27B-Uncensored-Q5_K_M.gguf`, 18.19 GiB — the MTP head is embedded in
  the gguf); the MTP draft and mmproj vision projector are baked into the image.
  Starts it. Built-in defaults = the live production profile (RTX 3090, 24 GB,
  `CTX_SIZE` 90000 — served n_ctx 90112). Flags: `--org` (target org — an
  account can host several sharing one API key), `--project` (target project —
  must already exist, the API has no project-create endpoint), `--group`
  (group name), `--gpu rtx3090|rtx5090`,
  `--model-file`, `--ctx-size` (`132768` = full 128K-class with a matching quant),
  `--image`, `--use-draft-model` (`none` = use the gguf's embedded MTP head),
  `--no-start` (apply config only), `--disk-size` / `--memory-size` (create path).
- **`deploy/deploy_qwen38_9b.py`** / **`deploy/deploy_qwen9b.py`** — 9B (`qwen38-9b` / `qwen9b`),
  plain: no draft, no vision, no template (the image's `none` sentinel makes that
  the default). `--ctx-size`, `--disk-size`.

---

## Using it

### 1. Run Claude Code against the model

```bash
cl_salad           # group is up → straight to claude
cl_salad_deploy    # group might be asleep → start it, wait, then claude
```

- `cl_salad` checks the gateway **once** and dies if it's not serving (exit 2,
  with a hint per failure code). It never starts anything.
- `cl_salad_deploy` starts a stopped group (**this costs money** — a fresh start
  re-downloads the model, ~30 min) and waits for readiness, then runs
  `cl_salad`. `cl_salad_deploy --status` is the cheap way to peek first, and
  `--no-run` starts + waits without launching claude.
- If the gateway DNS has changed (the group was recreated), point either one at
  it: `cl_salad https://<new-dns>` / `cl_salad_deploy --url https://<new-dns>`.

### 2. Smoke-test with curl

```bash
./claude/curl_salad.sh -url https://<gateway> -m qwen38-27b
```

Expect short correct answers to the built-in questions. If you get an empty
answer with `finish_reason: length`, thinking is eating the token budget — the
script already disables it, so this usually means the model isn't the build you
think (run `version.sh`).

### 3. Deploy / re-deploy the Salad group

```bash
python3 deploy/deploy_qwen38_27b.py                  # create-or-update qwen38-27b-q6k + start
python3 deploy/deploy_qwen38_27b.py --ctx-size 132768      # full context (with a matching quant)
python3 deploy/deploy_qwen38_27b.py --gpu rtx5090      # switch card (re-point + restart)
```

After a deploy, "running" only means the **container process** is up — the model
may still be downloading. Confirm readiness with `curl_salad.sh`, and confirm the
**live build** with `version.sh` in the container.

### 4. Verify what's actually running

```bash
# from inside a live instance (SSH):
version.sh
# gateway-side:
curl -s https://<gateway>/v1/models -H "Salad-Api-Key: $(tr -d '[:space:]' < claude/salad_api.txt)"
```

---

## Things that bite you (gotchas)

- **Two keys, two headers — don't mix them.** `salad_api.txt` → `Salad-Api-Key`
  header (gateway auth, required on **every** request). `api.txt` → the
  llama-server Bearer key (only if the model is configured with one; the deployed
  group isn't). Neither is ever printed, and both are gitignored.
- **Public port is 443, not 8888.** `8888` is the port the service listens on
  *inside* the container. The public `*.salad.cloud` domain is Cloudflare-fronted
  and only proxies standard ports — connections to `https://<dns>:8888` time out.
  Always use `443`.
- **Token caps vs context length.** The deployed group serves n_ctx 90112
  (CTX_SIZE 90000, Q5_K_M). Claude Code's *input + requested output* must stay
  under it or the model errors with "context length exceeded". `cl_salad`
  defaults to 64000 + 16000 = 80000 for headroom — lower them if you hit the
  error.
- **The worker image cache is keyed by repo *name*, not tag or digest.** A new tag
  on a cached repo can still serve a **stale** image on workers that already had
  the old one. Never overwrite a tag a worker may hold; to force a known build use
  a **digest-pinned** image ref (`repo@sha256:...`). Verify with `version.sh`.
- **Group DELETE leaves a name tombstone.** Recreating a just-deleted group name
  400s with `name_conflict` for a while (name-specific, not a create outage).
  Use a fresh group name; keep `MODEL_ALIAS` stable so clients don't notice.
- **On-demand groups cost money when they start.** `cl_salad_deploy` warns
  before starting a stopped group; `--status` is the free way to peek first,
  and `cl_salad` alone never starts anything.
- **The chat template is server-side.** It lives in the GGUF (or `--chat-template-file`
  on `llama-server`). A "template" error is an upstream problem, not a proxy one —
  the proxy only translates messages. The deployed image uses the permissive
  `qwen3.8.q6.jinja` so Claude Code's Anthropic-format requests (system messages
  anywhere) are accepted.
- **`--chat-template` vs `--chat-template-file`.** The inline `--chat-template` flag
  takes the template *text*; `--chat-template-file` takes a path. Passing the
  `CHAT_TEMPLATE` path to the inline flag (a bug shipped in the first baked
  image) makes the path string itself the entire prompt — the model then loops
  on it (every "hello" answer was a wall of `/opt/llama.cpp/qwen3.8.q6.gguf`).
  The template content was never the problem; it was never being applied.

---

## Repo layout

```
on_salad/
├── salad_client.py                 # SaladCloud OpenAPI client (stdlib)
├── deploy/                         # create-or-update deployers + the keys they read
│   ├── deploy_qwen38_27b.py        # canonical 27B deployer (qwen38-27b-q6k)
│   ├── deploy_generic.py           # model-agnostic (vanilla lmss_generic, any card)
│   ├── deploy_ampere.py            # llamAmpere fork build (sm_86 cards, ATX profile)
│   ├── deploy_qwen38_9b.py / deploy_qwen9b.py   # 9B deployers
│   ├── salad_api.txt  hft.txt      # KEYS — gitignored, chmod 600, never printed
├── docker/
│   ├── Dockerfile.multistage       # llama.cpp (CUDA + FA + NCCL) image build
│   ├── Dockerfile.lmss_q6_mtp_vision # baked MTP draft + mmproj vision extension
│   ├── Dockerfile.lmss_generic     # model-agnostic image (FROM v3, nothing baked)
│   ├── Dockerfile.lmss_generic_ampere # llamAmpere fork build: turbo KV, compiled in-image (cuda 13)
│   ├── qwen3.8.q6.jinja            # permissive chat template (shipped in image)
│   ├── api_app.py                  # status API on :9999 (/startup /live /ready)
│   ├── run_api.py                  # status-API entrypoint (COPYed in — a build file)
│   ├── version.sh                  # in-container build/download inspector
│   ├── stats.sh / llama_stats.py   # in-container stats (tps/queue/totals) via /metrics
│   ├── docker-compose.yml          # local 27B + 9B services
│   ├── api.txt                     # local LLM key (gitignored, chmod 600)
│   └── README.md                   # Docker / local-run details (image, compose, probes)
├── tests/                          # local run/test helpers (run_* cd into docker/ themselves)
│   ├── run_27b.sh / run_9b*.sh     # local run helpers
│   ├── curl1.sh / curl2.sh         # API check / question→answer
│   └── run-nvidia-smi.sh
├── claude/
│   ├── cl_salad                    # ★ Claude Code runner (dies if gateway dead)
│   ├── cl_salad_deploy             # ★ deploy half: start + wait, then cl_salad
│   ├── salad_proxy.py              # Anthropic↔OpenAI bridge
│   ├── curl_salad.sh               # gateway smoke test
│   └── salad_api.txt               # gateway key (gitignored)
├── utils/
│   ├── manage_groups.py            # group manager: list/refresh/start/stop/wait/stats/delete
│   ├── llama_stats.py              # stats entry point (delegates to docker/llama_stats.py)
│   ├── billing.py                  # per-org portal credit balances (USD + EUR)
│   ├── portal_vault.gpg            # billing vault (GPG AES256, gitignored)
│   └── README.md                   # utils/ helper
└── docs/
    ├── README.md                   # ← this file
    ├── container_group_create.md   # derived API reference (create group)
    └── SaladCloud API Knowledge Setup for Cline.md
```

Local vs Salad: `docker/README.md` covers building the image and running the
27B/9B services on a local GPU with `docker compose`. This page covers the Salad
deployment and the Claude Code path.

---

## Secrets & git

Three key files, all **gitignored** and `chmod 600`, none ever printed:

| File | What it is | Used by |
|---|---|---|
| `deploy/salad_api.txt` (mirrored in `claude/`) | SaladCloud API key → `Salad-Api-Key` header | `cl_salad`, `cl_salad_deploy`, `salad_proxy.py`, `curl_salad.sh`, `salad_client.py`, deploy scripts |
| `deploy/hft.txt` | HuggingFace token (validated, added to group env for authenticated downloads) | deploy/update scripts |
| `docker/api.txt` | llama-server Bearer key (only if the model needs one) | local run scripts |

Before committing, the key files must stay out of the diff — `git check-ignore`
should confirm each is ignored.
