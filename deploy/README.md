# deploy/ — the create-or-update deployers

Five scripts, same shape: **CREATE is free, PATCH is free, only START bills.**
A bare run re-applies that deployer's built-in profile to its group (`--no-start`
never touches credits; a run WITHOUT `--no-start` starts the group = paid).
The full doc is `docker/README.md`; the command cheat-sheet is `PROGRESS.md` §14.

Keys live here and are **never printed / never committed**: `salad_api.txt`
(account-wide Salad key), `hft.txt` (Hugging Face token). Pass the first with
`--stop-key deploy/salad_api.txt`; the HF token is read automatically.

## The five deployers

| script | default group (org/project) | image | notes |
|---|---|---|---|
| `deploy_qwen38_27b.py` | `qwen38-27b/qwen38-27b-q6k` | q6 mtp-vision (pinned digest) | the Claude Code backend; Q5_K_M @ CTX 90000 |
| `deploy_ampere.py` | `llm/atx-swift-27b-q5-opt` ⚠ **deleted 2026-10-13** | lmss_generic_ampere (turbo KV) | ATX profile = defaults: CTX **132768**, `--gpu` sm_86 30-series only |
| `deploy_generic.py` | `llm/atx-swift-27b-q5` | lmss_generic (vanilla) | any GGUF via `--model-repo/--model-file`, `--gpu` takes any card |
| `deploy_qwen38_9b.py` | `qwen38-27b/qwen38-9b` | lmss (base) | **CREATE-ONLY** (refuses an existing group) |
| `deploy_qwen9b.py` | `qwen38-27b/qwen9b` | lmss (base) | **CREATE-ONLY** (the group is gone; the deployer's default project is `qwen38-27b`) |

## Command lines

```bash
cd /dd2/andrei/docker/on_salad

# Re-apply a production profile, zero cost (PATCH parks; applies at the next start):
python3 -I deploy/deploy_qwen38_27b.py --project qwen38-27b --group qwen38-27b-q6k --no-start
python3 -I deploy/deploy_generic.py    --project llm --group atx-swift-27b-q5 --no-start

# The ATX profile (the default group name is DELETED — pass --group, fresh name):
python3 -I deploy/deploy_ampere.py --project llm --group atx-swift-27b-q5-med \
    --idle-shutdown heartbeat --stop-key deploy/salad_api.txt --no-start

# A different model on the generic image (draft + vision opt-in):
python3 -I deploy/deploy_generic.py --project llm --group my-group \
    --model-repo Some/Org --model-file Model-Q5_K_M.gguf --ctx-size 32768 \
    --gpu rtx3090 --draft-model none --no-start

# Watchdog arming (the only kill that stops billing): heartbeat mode + grace window
#   + the /.ssh presence guard (touch /.ssh in the container = watchdog holds off)
... --idle-shutdown heartbeat --heartbeat-timeout 300 --idle-grace 1800 --ssh-guard 1 \
    --stop-key deploy/salad_api.txt --no-start

# 9B groups are CREATE-ONLY: PATCH them with salad_client instead —
python3 -I tools/patch_9b.py            # see tools/patch_qwen38_9b_rescue.py for the rescue variant
```

## Rules that bite

- **`priority` and `restart_policy` are CREATE-only** — to change them, DELETE the
  group and CREATE it with a **fresh name** (the deleted name 400s `name_conflict`
  for 10+ min). Keep `MODEL_ALIAS` stable — clients use it.
- **PATCH replaces the env wholesale** (not merged), and a group that has been
  started before parks the update in `pending_update_in_progress` → further
  PATCHes 400 until the paid start. No cancel endpoint.
- **Image refs are digest-pinned** (worker caches are repo-name keyed, tags never
  re-pull). Fleet-wide re-pin: `python3 -I tools/patch_images_v12.py --dry-run`.
- **Paid start is the user's call.** `python3 utils/manage_groups.py start
  ma-casa-in-paris --min-bw-mbps 100` (bandwidth arbitrator: slow node →
  `/reallocate`, max 3 shots, never stops the group).
- **Verify the served context** after a start — `/metrics` has NO n_ctx gauge:
  `curl -s https://<networking.dns>/slots` (CTX 132768 → n_ctx 132864), and the
  server argv via `nvidia-smi --query-compute-apps=pid --format=csv,noheader` →
  `cat /proc/<pid>/cmdline` over the shell-less SSH relay (one plain command per
  call; the relay exits 1 even on success).
- The group **access URL is `networking.dns`** (https, 443, Cloudflare-fronted).
  There is no top-level `endpoint` field.
- `running` ≠ ready: readiness is the per-instance `ready` boolean
  (`python3 utils/manage_groups.py wait <org>`).
