First pass at deploy of custom dockers on salad. You have everything prepared for cline (or other harness, cline is (IMHO) just faster for routine tasks) to proceed.
I use this for deploying my llama-cpp on salad on demand, switching cards and quantities of cards. Today cline does all the work for me.

Docker image is a git cloned llama-server compiled with cuda sdk 12.8 (runs on 3090 & 5090) serving Qwen3.8 (define quantisation & ctx-len per card: ~16 GB weights for 27B Q4_K_M → 24 GB card tightly, 32 GB with headroom).

## Layout

- `salad_client.py` — stdlib-only Salad OpenAPI client (groups, projects, GPU classes, PATCH via `update_container_group`). API key from `salad_api.txt` (gitignored, never printed).
- `deploy_qwen38_27b.py` — mirrors the existing `qwen9bter` group for a 24 GB card (image `cuda128`, no draft support).
- `deploy_qwen38_27b_rtx5090.py` — creates the production group `qwen38-27b-rtx5090` from scratch on an RTX 5090 (image `cuda128-v2`, draft + vision model downloads, q8_0 KV, permissive chat template).
- `update_qwen38_27b_rtx5090.py` — re-applies image/env/GPU class to the live group, then restarts it. `--gpu rtx3090|rtx5090` picks the card (per-card env: the 3090 drops draft+vision — they don't fit in 24 GB), `--no-restart` for PATCH only. Optional `HF_TOKEN` from `hft.txt` (gitignored, validated against the Hub, never printed) is added to the group env so the image's `hf download` uses it.
- `docker/` — image build (`Dockerfile.multistage`), local compose (27B + 9B services), Salad test helpers (`curl2_salad.sh`, `clov_salad` + `salad_proxy.py`), run scripts. See `docker/README.md`.
