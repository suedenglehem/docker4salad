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
| **`cl_salad`** | `docker/docker_tests/` (also `/usr/local/bin/cl_salad`) | **Your main entry point.** Starts the proxy, auto-starts the Salad group if it's asleep, and runs Claude Code against the model. |
| **`salad_proxy.py`** | `docker/docker_tests/` | Stdlib Anthropic↔OpenAI bridge. Injects `Salad-Api-Key`, translates requests + streaming SSE + tool calls. Run by `cl_salad`. |
| `version.sh` | `docker/docker_tests/` | Run **inside a running instance** (via SSH) to confirm *which build* is actually live and what it's downloading. |
| `curl2_salad.sh` | `docker/docker_tests/` | Quick smoke test — asks the running model 2-3 simple questions through the gateway. |
| `salad_client.py` | repo root | Stdlib-only SaladCloud OpenAPI client (create/start/stop/delete/patch groups, GPU classes, projects). |
| `deploy_qwen38_27b.py` | repo root | **Canonical 27B deployer** — creates/updates the `qwen38-27b-q6k` group (baked Q6_K + MTP + vision image) and starts it. |
| `update_qwen38_27b_rtx5090.py` | repo root | Re-point a live group at a new image / GPU class / env, then restart it. |
| `deploy_qwen38_9b.py` / `deploy_qwen9b.py` | repo root | 9B deployers (`qwen38-9b`, `qwen9b`) — plain, no draft/vision/template. |
| `deploy_qwen38_27b_rtx5090.py` | repo root | Earlier 27B deployer (`qwen38-27b-rtx5090`, `lmss:cuda128-v2`). |
| `patch_qwen38_9b_rescue.py` | repo root | One-off: digest-pinned PATCH to force a fresh image onto `qwen38-9b`. |

Everything runs against org **`ma-casa-in-paris`**, project **`qwen38-27b`**.

---

## The scripts

### `cl_salad` — run Claude Code against the Salad model

The thing you'll type. It mirrors `/usr/local/bin/clov` (a wrapper that points
the `claude` CLI at a local model), except it speaks to a Salad public gateway,
so it first stands up the local `salad_proxy.py` and lets the proxy carry the
`Salad-Api-Key`.

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
3. **Auto-starts the group if it's stopped** (this is an on-demand group — a
   fresh start re-downloads the model and **incurs cost**). Then waits until the
   gateway's `/v1/models` returns `200` (up to 900 s). Opt out with
   `SALAD_NO_AUTOSTART=1` (it will then just check readiness and bail if not up).
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
| `SALAD_ORG` / `SALAD_PROJECT` / `SALAD_GROUP` | `ma-casa-in-paris` / `qwen38-27b` / `qwen38-27b-q6k` | Used for the auto-start / status lookups. |
| `SALAD_ENABLE_THINKING` | `0` | `1` to enable Qwen thinking (off by default → clean answers). |
| `SALAD_NO_AUTOSTART` | unset | `1` = don't auto-start / wait; just check readiness. |
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS` | `18000` | Input token cap (see [token caps](#token-caps-vs-context-length)). |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | `8000` | Output token cap. |

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
python3 docker/docker_tests/salad_proxy.py \
  --upstream https://<gateway>/v1/chat/completions \
  --model qwen38-27b \
  --keyfile docker/docker_tests/salad_api.txt \
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

### `curl2_salad.sh` — quick smoke test

Asks the running model a couple of simple questions through the public gateway.
Fastest way to confirm the model is up and answering.

```bash
./docker/docker_tests/curl2_salad.sh -url https://<gateway> -m qwen38-27b
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

All read the Salad key from `salad_api.txt` and (optionally) a HF token from
`hft.txt` (validated against the Hub, added to the group env, never printed).

- **`deploy_qwen38_27b.py`** — the **canonical** 27B deployer. Create-or-update the
  `qwen38-27b-q6k` group on the baked **q6-mtp-vision** image: only the main model
  (`Qwen3.8-27B-Uncensored-Q6_K.gguf`, 20.9 GiB) downloads at runtime; the MTP
  draft and mmproj vision projector are baked into the image. RTX 5090. Starts it.
  Flags: `--gpu rtx5090|rtx3090`, `--ctx-size` (default `30000`, `132768` = full),
  `--use-draft-model` (`none` = use the gguf's embedded MTP head),
  `--no-start` (apply config only), `--disk-size` / `--memory-size` (create path).
- **`update_qwen38_27b_rtx5090.py`** — re-point a **live** group at a new image /
  GPU class / env, then restart (stop → PATCH → start). `--gpu rtx3090|rtx5090`
  switches the card (the 3090 env drops draft+vision — they don't fit in 24 GB);
  `--no-restart` = PATCH only.
- **`deploy_qwen38_9b.py`** / **`deploy_qwen9b.py`** — 9B (`qwen38-9b` / `qwen9b`),
  plain: no draft, no vision, no template (the image's `none` sentinel makes that
  the default). `--ctx-size`, `--disk-size`.
- **`deploy_qwen38_27b_rtx5090.py`** — the earlier 27B group
  (`qwen38-27b-rtx5090`, `lmss:cuda128-v2`, noMTP Q4_K_M + MTP draft + vision).
- **`patch_qwen38_9b_rescue.py`** — one-off rescue: PATCH `qwen38-9b` to a
  digest-pinned image (the airtight lever against the stale repo-name cache),
  falling back to the tag if the API rejects digests, then start.

---

## Using it

### 1. Run Claude Code against the model

Make sure the group is reachable (or let `cl_salad` start it), then:

```bash
cl_salad
```

- If the group is **asleep**, `cl_salad` starts it and waits for `/v1/models`. A
  fresh on-demand start re-downloads the main model — this can take a while and
  **costs money**. If you'd rather control that, pass `SALAD_NO_AUTOSTART=1` and
  start the group yourself when you're ready.
- If the gateway DNS has changed (the group was recreated), override it:
  `SALAD_GATEWAY_HOST=<new-dns> cl_salad`.

### 2. Smoke-test with curl

```bash
./docker/docker_tests/curl2_salad.sh -url https://<gateway> -m qwen38-27b
```

Expect short correct answers to the built-in questions. If you get an empty
answer with `finish_reason: length`, thinking is eating the token budget — the
script already disables it, so this usually means the model isn't the build you
think (run `version.sh`).

### 3. Deploy / re-deploy the Salad group

```bash
python3 deploy_qwen38_27b.py            # create-or-update qwen38-27b-q6k + start
python3 deploy_qwen38_27b.py --ctx-size 132768      # full context
python3 update_qwen38_27b_rtx5090.py --gpu rtx5090  # re-point + restart a live group
```

After a deploy, "running" only means the **container process** is up — the model
may still be downloading. Confirm readiness with `curl2_salad.sh`, and confirm the
**live build** with `version.sh` in the container.

### 4. Verify what's actually running

```bash
# from inside a live instance (SSH):
version.sh
# gateway-side:
curl -s https://<gateway>/v1/models -H "Salad-Api-Key: $(tr -d '[:space:]' < docker/docker_tests/salad_api.txt)"
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
- **Token caps vs context length.** The deployed group runs ~30k context. Claude
  Code's *input + requested output* must stay under it or the model errors with
  "context length exceeded". `cl_salad` defaults to 18000 + 8000 = 26000 for
  headroom — lower them if you hit the error.
- **The worker image cache is keyed by repo *name*, not tag or digest.** A new tag
  on a cached repo can still serve a **stale** image on workers that already had
  the old one. Never overwrite a tag a worker may hold; to force a known build use
  a **digest-pinned** image ref (`repo@sha256:...`). Verify with `version.sh`.
- **Group DELETE leaves a name tombstone.** Recreating a just-deleted group name
  400s with `name_conflict` for a while (name-specific, not a create outage).
  Use a fresh group name; keep `MODEL_ALIAS` stable so clients don't notice.
- **On-demand groups cost money when they start.** `cl_salad` warns before
  auto-starting; `SALAD_NO_AUTOSTART=1` gives you the decision.
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
├── deploy_qwen38_27b.py            # canonical 27B deployer (qwen38-27b-q6k)
├── deploy_qwen38_27b_rtx5090.py    # earlier 27B deployer
├── update_qwen38_27b_rtx5090.py    # re-point + restart a live 27B group
├── deploy_qwen38_9b.py / deploy_qwen9b.py   # 9B deployers
├── patch_qwen38_9b_rescue.py       # one-off digest-pinned rescue
├── repo.txt                        # image repo name (boris271142/lmss)
├── salad_api.txt  hft.txt  api.txt # KEYS — gitignored, chmod 600, never printed
├── docker/
│   ├── Dockerfile.multistage       # llama.cpp (CUDA + FA + NCCL) image build
│   ├── Dockerfile.lmss_q6_mtp_vision # baked Q6_K + MTP + vision extension
│   ├── qwen3.8.q6.jinja            # permissive chat template (shipped in image)
│   ├── api_app.py                  # status API on :9999 (/startup /live /ready)
│   ├── run_api.py
│   ├── README.md                   # Docker / local-run details (image, compose, probes)
│   └── docker_tests/
│       ├── cl_salad                # ★ Claude Code entry point
│       ├── salad_proxy.py          # Anthropic↔OpenAI bridge
│       ├── version.sh              # in-container build/download inspector
│       ├── curl2_salad.sh          # gateway smoke test
│       ├── docker-compose.yml      # local 27B + 9B services
│       ├── run_27b.sh / run_9b*.sh # local run helpers
│       └── salad_api.txt           # gateway key (gitignored)
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
| `salad_api.txt` | SaladCloud API key → `Salad-Api-Key` header | `cl_salad`, `salad_proxy.py`, `curl2_salad.sh`, `salad_client.py`, deploy scripts |
| `hft.txt` | HuggingFace token (validated, added to group env for authenticated downloads) | deploy/update scripts |
| `api.txt` | llama-server Bearer key (only if the model needs one) | local run scripts |

Before committing, the key files must stay out of the diff — `git check-ignore`
should confirm each is ignored.
