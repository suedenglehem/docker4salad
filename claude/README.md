# claude/ — Claude Code ↔ Salad gateway stack

Runs **Claude Code** against the Qwen3.8-27B served by a SaladCloud container
group. The two halves that don't natively fit together:

- Claude Code speaks the **Anthropic Messages API**; the Salad container runs
  `llama-server`, which is **OpenAI-only**.
- The group's public gateway has auth enabled: **every** request must carry the
  Salad API key as a `Salad-Api-Key` header, and the tools above never let you
  add it by hand.

Flow for a full session:

```
cl_salad_deploy            cl_salad                    Claude Code
   │  (1) group status ──────▶  Salad mgmt API
   │  (2) start if stopped    │  (3) liveness check:
   │  (4) poll /v1/models     │      /v1/models == 200?
   │      until 200           │  (5) start salad_proxy.py
   └── export env, exec ─────▶│      on 127.0.0.1:8093
                              └──▶ ANTHROPIC_BASE_URL=127.0.0.1:8093, run claude
                                                     │
                              salad_proxy.py ── Salad-Api-Key on every request ──▶ https://<group>.salad.cloud
                                                     (Anthropic ↔ OpenAI translation)     └─▶ llama-server
```

| File | What it is |
|---|---|
| **`cl_salad`** | The **runner**. Checks the gateway once, **dies (exit 2) if it's dead**, then starts the proxy and runs Claude Code against it. Does not start or manage the group. |
| **`cl_salad_deploy`** | The **deploy half**. Looks up the group via the Salad management API, starts it if stopped (**this incurs cost**), waits for the model to be ready (default 45 min — a cold start re-downloads the model), then hands off to `cl_salad`. |
| **`salad_proxy.py`** | Stdlib-only **Anthropic↔OpenAI bridge**, bound to 127.0.0.1 only (it holds the key — never exposed to the network). Translates `/v1/messages` ↔ `/v1/chat/completions`, streaming SSE and tool calls, injects `Salad-Api-Key` on every upstream request. |
| **`curl_salad.sh`** | **Smoke test**: asks the model 2–3 known-answer questions straight through the gateway (no proxy). Fastest way to confirm a group is up and answering. |
| **`chat_salad.sh`** | Interactive chat via Simon Willison's `llm` package, direct against the gateway (`llm -H` carries the Salad-Api-Key). |
| **`salad_api.txt`** | The Salad API key (67 B). Gitignored, chmod 600, **never printed**. Read by everything above; the gateway rejects requests that lack it. |

`cl_salad` and `cl_salad_deploy` are also available on PATH via
`/usr/local/bin` symlinks (the scripts resolve their real directory, so the
symlinks still find `salad_proxy.py` + `salad_api.txt` here).

## cl_salad — run Claude Code (group must already be live)

```sh
cl_salad                                  # built-in default gateway (prod group)
cl_salad https://<group>.salad.cloud      # point at a (re)created group
cl_salad https://<dns> -p "Say hi"        # everything after the URL goes to claude
```

- One liveness check, then it dies fast with a specific message:
  `200` = serving · `404` = group stopped / model still loading ·
  `403` = gateway rejects group (deleted, wrong URL or key) ·
  `000` = host unreachable. Exit code `2` on all of them.
- Starts the proxy on `SALAD_PROXY_PORT` (default **8093**), exports
  `ANTHROPIC_BASE_URL=http://127.0.0.1:8093` + `ANTHROPIC_API_KEY=local`,
  and runs `claude --permission-mode bypassPermissions --model <alias>`.
  The proxy is killed when claude exits; claude's own exit code is returned.
- Token caps: `CLAUDE_CODE_MAX_CONTEXT_TOKENS` **64000** +
  `CLAUDE_CODE_MAX_OUTPUT_TOKENS` **16000** = 80000, deliberately under the
  served n_ctx **90112** (CTX_SIZE 90000, Q5_K_M). Lower them if you hit
  "context length exceeded".
- Qwen thinking is **off** by default (`SALAD_ENABLE_THINKING=1` to enable).
- Other overrides: `SALAD_GATEWAY_HOST`, `SALAD_MODEL_ALIAS`
  (default `qwen38-27b` — keep stable across group recreations; clients
  depend on it), `SALAD_KEYFILE`, `CLAUDE_BIN`.

## cl_salad_deploy — start (if needed) + wait + run

```sh
cl_salad_deploy                                  # defaults = prod group
cl_salad_deploy --group qwen38-27b-q5            # another group, same org/project
cl_salad_deploy --org akl-on-salad --project comfy --group qwen38-27b-q5
cl_salad_deploy --status                         # print group status, exit
cl_salad_deploy --no-run                         # start + wait only (no claude)
cl_salad_deploy --group qwen38-27b-q5 -- -p "Say hi"   # -- ends option parsing
```

- Options come **before** any claude argument (the first non-option token ends
  option parsing; use `--` if a claude flag could clash).
- Precedence: **option > env var > default**. Env vars: `SALAD_ORG`
  (ma-casa-in-paris), `SALAD_PROJECT` (qwen38-27b), `SALAD_GROUP`
  (qwen38-27b-q6k), `SALAD_GATEWAY_HOST`, `SALAD_MODEL_ALIAS`,
  `SALAD_KEYFILE`, `SALAD_PROXY_PORT`, `SALAD_ENABLE_THINKING`,
  `CLAUDE_CODE_MAX_*_TOKENS`.
- **No `--url` → the gateway DNS is looked up from the group itself**
  (`networking.dns`), so `--group <name>` is enough even after a group
  recreation.
- While waiting it polls the gateway `/v1/models` every 5 s and re-checks the
  group's management-API status every ~60 s — if the group flips back to
  `stopped` it bails early (it will never become ready; check the Salad UI).
- Handoff: exports the resolved config and `exec cl_salad <dns> <claude args>`;
  cl_salad's fast 200 path then takes over.

## salad_proxy.py — the bridge (run by the wrappers, standalone-able)

```sh
python3 salad_proxy.py --upstream https://<dns>/v1/chat/completions \
    --model qwen38-27b --keyfile salad_api.txt --port 8093
```

Flag-only CLI: `--upstream` / `--model` / `--keyfile` are **required**;
optional `--port` (8093), `--host` (127.0.0.1), `--thinking 0|1`,
`--max-tokens-cap 12000`, `--timeout 600`. Serves `POST /v1/messages`,
`GET /v1/models`, `GET /healthz` (alias `/health`). Upstream must be https.

## curl_salad.sh — smoke test (no proxy)

```sh
./curl_salad.sh -url https://<group>.salad.cloud -m qwen38-27b
# options: -url (required)  -p PORT (443 — Cloudflare fronts the access
# domain, only standard ports; the in-container port 8888 is NOT forwardable)
#          -api KEY (llama-server Bearer key — only sent if given; the deployed
#          group runs without one)  -m MODEL (request body "model", default qwen)
```

Asks 27×43 / capital of France / one-word language; prints just the answers.
Continues past a failed question (wants all results, not to stop at the first
hiccup). Thinking is disabled in the payload — otherwise short max_tokens
budgets get eaten by reasoning content and the answer comes back empty.

## chat_salad.sh — chat via the `llm` package

Chat with the model from the terminal: `./chat_salad.sh` for interactive,
`./chat_salad.sh "prompt"` for one-shot; extra args pass through to `llm`
(e.g. `-s "sys"`). `llm` 0.36+ sends the `Salad-Api-Key` header itself via
`-H`, so it talks to the gateway **directly** — no proxy (the proxy is the
Anthropic bridge `cl_salad` uses, and it does not serve OpenAI chat
completions). Defaults to the production `qwen38-27b-q6k` gateway; point it
elsewhere with `SALAD_UPSTREAM` / `SALAD_PORT` / `SALAD_MODEL` /
`SALAD_KEYFILE`. It checks `/v1/models` once and dies (exit 2, hint per
failure code) if the group is not serving; `SALAD_WAIT=1` polls for up to
5 min instead, for a group mid cold start.

## Keys — don't mix them up

| Key | File | Header | Notes |
|---|---|---|---|
| **Salad gateway key** | `salad_api.txt` (here) | `Salad-Api-Key: <key>` on **every** request | Gateway auth; also works against `api.salad.com` management API. The deployers keep their own copy at `deploy/salad_api.txt` (read by `salad_client.py`). |
| **LLM key** (optional) | passed via `-api` / `--api-key` / `$API_KEY` | `Authorization: Bearer <key>` | Only if llama-server was started with `--api-key`. The deployed group runs **without** one — the header is simply omitted. |

Both are secrets: gitignored, chmod 600, never printed by the scripts.
