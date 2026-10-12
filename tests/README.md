# tests/

**These scripts test things running LOCALLY, in Docker, on this box** — the
compose services and containers built from `docker/`, reached over
`localhost`. Nothing here talks to the SaladCloud API, starts a group, or
costs money. For the Salad side of testing use `tools/` (offline harnesses +
read-only fleet snapshots) and `claude/` (`sl_curl.sh` smoke test, `cl_salad`,
`sl_claude_deploy`) — those point at a gateway URL, these point at `localhost`.

Same image family as the Salad groups (`boris271142/lmss*`), so a script here
proves the model/flags/serve path before any paid start — but the compose file
pins an **older tag** (`cuda128-v3`) than the live groups, whose digest pins
live in the deployers (q6 → `...q6_mtp_vision:cuda128-v14`, ampere →
`lmss_generic_ampere:cuda130-v10`). A pass here proves the *shape*, not the
exact production image.

## The scripts

| script | what it does |
|---|---|
| `run_9b.sh` | start the **9B smoke-test service** via `docker compose up -d qwen38-9b` (container `qwen38-9b`, image `boris271142/lmss:cuda128-v3`). Takes `[GPU_ID]` (default **1**). |
| `run_9b_pure.sh` | the same 9B container with a plain `docker run -it` — logs stream to your terminal, Ctrl-C stops the server. No compose. |
| `run_9b_pull.sh` | same as `run_9b_pure.sh`, but **pulls the image from Docker Hub** first — for machines with no local build/cache. |
| `run_27b.sh` | start the **27B service** via `docker compose up -d qwen38-llama` (container `qwen38-llama-fa-api`). Takes `[GPU_ID]` (default **0**). **The 27B does not fit this box's cards** (Q4_K_M weights ≈15.4 GB, live config ≈24 GB) — use it for smaller models / partial CPU offload; for the 27B use the Salad group. |
| `run-nvidia-smi.sh` | one-shot container pinned to **one physical GPU**, with the image's CMD replaced by `nvidia-smi` — prints that card and exits, leaving the long-running server untouched. Needs a local image tagged `qwen38-llama-fa-api` (see Gotchas). |
| `curl1.sh` | quick API check against a local server: one fixed question to `/v1/chat/completions`. `[-p PORT] [-a APIKEY]` (default port **8080**). |
| `curl2.sh` | ask **your own question** and print only the answer. `[--think] [question…]`. Default port **8081**. |

## Ports (host side)

| port | what is on it |
|---|---|
| `8080` | the 27B service (`qwen38-llama-fa-api`); override with `HOST_PORT=8090` when something else owns 8080 |
| `8081` | the 9B service (`qwen38-9b`) — both services can run at once |
| `9999` | the 27B's status API (`STATUS_API_HOST_PORT`) |
| `9998` | the 9B's status API (`STATUS_API_9B_HOST_PORT`) — the container-side port is `9999` in **both** services, hence the different host ports |

Both compose services run the same base image, `boris271142/lmss:cuda128-v3`;
the model is chosen by the service's env, not by the image.

## GPU indices on this box

`GPU_ID` is the **host** index from `nvidia-smi`; the compose services expose
only that card, so inside the container it is always device `0`.

Live box (verified 2026-10-12): **0 = RTX 4080 SUPER (16 GB), 1 = RTX 4060 Ti
(16 GB)**. The `[GPU_ID]` defaults baked into the scripts (27B → 0, 9B → 1)
are still right; the "RTX 3080 Ti" text in their header comments is from an
earlier box.

## The API key: `api.txt`

`tests/api.txt` is a **gitignored symlink** to `docker/api.txt` — the local
server's key, kept out of the image and out of the compose file.

- `curl1.sh` / `curl2.sh` do `cd "$(dirname "$0")"` and read `api.txt` here
  (through the symlink), with `tr -d '[:space:]'`. Missing file → the request
  goes out with no `Authorization` header.
- `run_*.sh` `cd` to `docker/` (hardcoded absolute path — moving the repo
  breaks them) and read `docker/api.txt` directly, exporting it as `API_KEY`.
  Missing file → the server runs **without** key auth.
- Keep it `chmod 600`, never print it, never commit it — `api.txt` is in
  `.gitignore` at the repo root.

## Gotchas

- **No host bind mount by design** (see `docker/docker-compose.yml`): switching
  `GPU_ID` recreates the container, which **re-downloads the model**.
- `run_9b_pure.sh` / `run_9b_pull.sh` use `--restart unless-stopped` (same as
  compose), so a Ctrl-C exit can be restarted in the background —
  `docker stop qwen38-9b` is the full stop. Both remove any old container with
  the same name first, so `--name` never collides.
- `run-nvidia-smi.sh` sets `IMAGE="qwen38-llama-fa-api"` — that is the compose
  **container name**, not an image tag, so on a fresh box it fails (Docker tries
  to pull `qwen38-llama-fa-api:latest`). Tag one first, e.g.
  `docker tag boris271142/lmss:cuda128-v3 qwen38-llama-fa-api`.
- `curl1.sh`'s header comment says port 8081; the script's default is
  **8080** (`-p 8081` for the 9B).
- **Thinking is off by default** in `curl1.sh` / `curl2.sh`
  (`chat_template_kwargs: {"enable_thinking": false}`). With a short
  `max_tokens` budget, reasoning eats the whole budget and the answer comes
  back empty with `finish_reason: length`. `curl2.sh --think` turns it on
  (slower, needs a bigger budget).
