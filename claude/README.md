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
sl_claude_deploy            cl_salad                    Claude Code
   │  (1) group status ──────▶  Salad mgmt API
   │  (2) start if stopped    │  (3) liveness check:
   │  (4) poll /v1/models     │      /v1/models == 200?
   │      until 200           │  (5) start sl_proxy.py
   └── export env, exec ─────▶│      on 127.0.0.1:8093
                              └──▶ ANTHROPIC_BASE_URL=127.0.0.1:8093, run claude
                                                     │
                              sl_proxy.py ── Salad-Api-Key on every request ──▶ https://<group>.salad.cloud
                                                     (Anthropic ↔ OpenAI translation)     └─▶ llama-server
```

| File | What it is |
|---|---|
| **`cl_salad`** | The **runner**. Checks the gateway once, **dies (exit 2) if it's dead**, then starts the proxy and runs Claude Code against it. Does not start or manage the group. |
| **`sl_claude_deploy`** | The **deploy half**. Looks up the group via the Salad management API, starts it if stopped (**this incurs cost**), waits for the model to be ready (default 45 min — a cold start re-downloads the model), then hands off to `cl_salad`. |
| **`sl_proxy.py`** | Stdlib-only **Anthropic↔OpenAI bridge**, bound to 127.0.0.1 only (it holds the key — never exposed to the network). Translates `/v1/messages` ↔ `/v1/chat/completions`, streaming SSE and tool calls, injects `Salad-Api-Key` on every upstream request. |
| **`sl_curl.sh`** | **Smoke test**: asks the model 2–3 known-answer questions straight through the gateway (no proxy). Fastest way to confirm a group is up and answering. |
| **`sl_chat.sh`** | Interactive chat via Simon Willison's `llm` package, direct against the gateway (`llm -H` carries the Salad-Api-Key). |
| **`salad_api.txt`** | The Salad API key (67 B). Gitignored, chmod 600, **never printed**. Read by everything above; the gateway rejects requests that lack it. |
| **`TESTS.md`** | Test instructions — the offline zero-content-fix suite (`tools/test_sl_proxy_offline.py`, no group) plus the live group tests (baseline, the fixed `cl_salad` path, and how the edge-kill failure now surfaces). |

`cl_salad` and `sl_claude_deploy` are also available on PATH via
`/usr/local/bin` symlinks (the scripts resolve their real directory, so the
symlinks still find `sl_proxy.py` + `salad_api.txt` here).

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
- **One session per port.** Before launching claude it verifies **our** proxy is
  the one answering: the proxy echoes a per-session nonce on `/healthz`, so a
  second session on the same port exits **3** with "the port is held by ANOTHER
  cl_salad session" instead of silently running against the first session's proxy
  (which dies when that session exits). Run the second session with
  `--port 8094`. Verified 2026-10-13 against a local fake gateway: foreign proxy →
  exit 3, free port → exit 0, two live sessions → second exits 3, no leaked proxies.
- Token caps: `CLAUDE_CODE_MAX_CONTEXT_TOKENS` **64000** +
  `CLAUDE_CODE_MAX_OUTPUT_TOKENS` **16000** = 80000, deliberately under the
  served n_ctx **90112** (CTX_SIZE 90000, Q5_K_M). Lower them if you hit
  "context length exceeded".
- Qwen thinking is **off** by default (`SALAD_ENABLE_THINKING=1` to enable).
- Other overrides: `SALAD_GATEWAY_HOST`, `SALAD_MODEL_ALIAS`
  (default `qwen38-27b` — keep stable across group recreations; clients
  depend on it), `SALAD_KEYFILE`, `CLAUDE_BIN`.
- **Heartbeat keepalive** (default **ON**, `SALAD_HEARTBEAT=0` to opt out):
  while the session lives, a background loop posts a **1-token completion** to
  the gateway every 30 s. Needed when the group runs with
  `IDLE_SHUTDOWN=heartbeat` — the in-container watchdog self-stops the group
  `HEARTBEAT_TIMEOUT` s after the pings stop, i.e. shortly after a dead
  connection (closed terminal, lost SSH), instead of billing for hours. ON by
  default so a bare `cl_salad https://<dns>` keeps its group alive (extra
  keepalive is harmless on groups without the watchdog); `sl_claude_deploy`
  also exports it from the group env. The pinger dies with the session
  (EXIT trap).

## sl_claude_deploy — start (if needed) + wait + run

```sh
sl_claude_deploy                                  # defaults = prod group
sl_claude_deploy --group qwen38-27b-q5            # another group, same org/project
sl_claude_deploy --org akl-on-salad --project comfy --group qwen38-27b-q5
sl_claude_deploy --status                         # print group status, exit
sl_claude_deploy --no-run                         # start + wait only (no claude)
sl_claude_deploy --group qwen38-27b-q5 -- -p "Say hi"   # -- ends option parsing
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
- **Heartbeat auto-detect**: before the handoff it reads the group's
  `IDLE_SHUTDOWN` env from the management API; when it's `heartbeat` it
  exports `SALAD_HEARTBEAT=1`, so cl_salad (and any `sl_chat.sh` run
  against this gateway) keeps the group alive with a 1-token call every 30 s
  — the client follows the docker side. Both clients now default the pinger
  ON, so this is a convenience, not a requirement; `SALAD_HEARTBEAT=0` opts
  out.

## sl_proxy.py — the bridge (run by the wrappers, standalone-able)

```sh
python3 sl_proxy.py --upstream https://<dns>/v1/chat/completions \
    --model qwen38-27b --keyfile salad_api.txt --port 8093
```

Flag-only CLI: `--upstream` / `--model` / `--keyfile` are **required**;
optional `--port` (8093), `--host` (127.0.0.1), `--thinking 0|1`,
`--max-tokens-cap 12000`, `--timeout 600`. Serves `POST /v1/messages`,
`GET /v1/models`, `GET /healthz` (alias `/health`). Upstream must be https.

The proxy **strips empty think blocks** (`<think>\s*</think>`) from response
text, streaming and non-streaming. With `enable_thinking=false` the chat
template ends the prompt with the closed empty block as the generation
prefix; groups whose EXTRA_ARGS carry `--reasoning-format none` don't strip
it server-side and the model echoes it — Claude Code then shows a literal
`<think>\n\n</think>` in every answer. Real (non-empty) think blocks pass
through untouched. Server-side fix: the deployers now drop `--flag none`
pairs from EXTRA_ARGS (`strip_none_value_args`), so llama.cpp keeps its
default think parsing.

A **zero-content or anomalous stream end** — upstream EOF with no text and no
tool calls, a non-SSE line, an unparseable `data:` payload, or an in-band
`data: {"error": …}` payload — is surfaced as ONE Anthropic `error` event
instead of a clean empty `message_stop`, so Claude Code fails loudly rather
than rendering a bogus empty answer. Detection lives in `_iter_sse` (anomaly
sentinels) plus `StreamBuilder.is_empty()`; a stream that delivered neither
text nor tool calls is never a valid turn (thinking is off, tool-only turns
carry `tool_calls`). See the latency note below for why long prompts hit
this.

## sl_curl.sh — smoke test (no proxy)

```sh
./sl_curl.sh -url https://<group>.salad.cloud -m qwen38-27b
# options: -url (required)  -p PORT (443 — Cloudflare fronts the access
# domain, only standard ports; the in-container port 8888 is NOT forwardable)
#          -api KEY (llama-server Bearer key — only sent if given; the deployed
#          group runs without one)  -m MODEL (request body "model", default qwen)
```

Asks 27×43 / capital of France / one-word language; prints just the answers.
Continues past a failed question (wants all results, not to stop at the first
hiccup). Thinking is disabled in the payload — otherwise short max_tokens
budgets get eaten by reasoning content and the answer comes back empty.

## sl_chat.sh — chat via the `llm` package

Chat with the model from the terminal: `./sl_chat.sh` for interactive,
`./sl_chat.sh "prompt"` for one-shot; extra args pass through to `llm`
(e.g. `-s "sys"`). A **leading URL argument** picks the gateway — same
convention as `cl_salad`: `./sl_chat.sh https://<dns>.salad.cloud
[prompt]`. `llm` 0.36+ sends the `Salad-Api-Key` header itself via `-H`, so
it talks to the gateway **directly** — no proxy (the proxy is the Anthropic
bridge `cl_salad` uses, and it does not serve OpenAI chat completions).
Defaults to the `atx-hb-test2` gateway (`honey-coleslaw-…`) with model alias
`atx-swift-27b-opt`; point it elsewhere with the URL argument or
`SALAD_UPSTREAM` / `SALAD_PORT` / `SALAD_MODEL` / `SALAD_KEYFILE`. It checks
`/v1/models` once and dies (exit 2, hint per failure code) if the group is
not serving; `SALAD_WAIT=1` polls for up to 5 min instead, for a group mid
cold start. The 1-token keepalive loop (every 30 s) is **ON by default**
(`SALAD_HEARTBEAT=0` opts out) for the duration of the chat — the pinger is
killed by the EXIT trap when the chat ends (the `llm` call runs in the
foreground, not via `exec`, so the trap fires).

## Large prompts — Cloudflare edge latency (measured 2026-10-19)

The `*.salad.cloud` domain is Cloudflare-fronted, and the edge has a ~100 s
origin timeout. Measured against a single-instance qwen3.8 group:

| Request | TTFB |
|---|---|
| small prompt (~1K tokens), group idle | ~1.7 s |
| worker-shaped prompt (~5K tokens, prompt_tokens=5189) | ~93 s ([DONE] at 95.5 s) |

Real agentic prompts (12–16K tokens) therefore sit at/past the edge timeout,
and one instance serializes requests, so a long generation delays everything
behind it. When the edge gives up, the request can still come back as
**HTTP 200 + `text/event-stream` headers followed by EOF with zero content**
(or an in-band `data: {"error": …}` payload mid-stream) — indistinguishable
from a "clean empty answer" unless the client checks.

`sl_proxy.py` (this bridge, updated 2026-10-19) handles it, as does the
dispatcher's vendored copy
(`/dd2/andrei/claude/dispatcher/local_dispatcher/salad_proxy.py`): a
zero-content or anomalous stream end emits ONE Anthropic `error` event
instead of an empty `message_stop`, so Claude Code fails loudly instead of
rendering a bogus empty answer (the dispatcher additionally retries it as a
backend disconnect). Practical expectation on a salad group: ~90–120 s per
turn for agent-sized prompts, not a throughput backend.

## Keys — don't mix them up

| Key | File | Header | Notes |
|---|---|---|---|
| **Salad gateway key** | `salad_api.txt` (here) | `Salad-Api-Key: <key>` on **every** request | Gateway auth; also works against `api.salad.com` management API. The deployers keep their own copy at `deploy/salad_api.txt` (read by `salad_client.py`). |
| **LLM key** (optional) | passed via `-api` / `--api-key` / `$API_KEY` | `Authorization: Bearer <key>` | Only if llama-server was started with `--api-key`. The deployed group runs **without** one — the header is simply omitted. |

Both are secrets: gitignored, chmod 600, never printed by the scripts.
