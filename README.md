First pass at deploy of custom dockers on salad. You have everything prepared for cline (or other harness, cline is (IMHO) just faster for routine tasks) to proceed.
I use this for deploying my llama-cpp on salad on demand, switching cards and quantities of cards. Today cline does all the work for me.

Docker image is a git cloned llama-server compiled with cuda sdk 12.8 (runs on 3090 & 5090) serving Qwen3.8 (quant + ctx-len per card — target matrix in `docker/README.md`: ≥90K → Q5_K_M on 24 GB / Q6_K on 32 GB; ≥128K → Q6_K on 32 GB, or Q4_K_M on 24 GB).

## Layout

- `salad_client.py` — stdlib-only Salad OpenAPI client (groups, projects, GPU classes, PATCH via `update_container_group`). API key from `salad_api.txt` (gitignored, never printed).
- `deploy_qwen38_27b.py` — canonical create-or-update deployer for the live group `qwen38-27b-q6k` (the production 27B Claude Code backend) on the baked q6-mtp-vision image (draft + mmproj baked in, only the main model fetched via `hf`). `--org`, `--project`, `--group`, `--gpu rtx5090|rtx3090`, `--model-file`, `--ctx-size`, `--image`, `--use-draft-model`, `--no-start`. Built-in defaults = the live production profile (Q5_K_M @ CTX 90000 on rtx3090, digest-pinned v4 image) — a bare run re-applies exactly that (idempotent against the live group).
- `billing.py` — per-org Salad portal credit balances (USD + live EUR column). The public API has no billing operation, so it logs into the portal backend with credentials from `portal_vault.gpg` (GPG AES256, gitignored; passphrase via `$PORTAL_VAULT_PASS` or prompt). First run creates the vault: prompts for email/password/org slugs and verifies the login before encrypting.
- `groups.sh` / `list_groups.py` — status reporter for every known org/project: group state, instance counts, gateway URLs, SSH lines for running groups.
- `docker/` — image builds (`Dockerfile.multistage` + baked `Dockerfile.lmss_q6_mtp_vision`), local compose (27B + 9B services), Salad test helpers (`curl2_salad.sh`, `cl_salad` + `salad_proxy.py`), run scripts. See `docker/README.md`.
