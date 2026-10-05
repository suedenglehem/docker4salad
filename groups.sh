#!/usr/bin/env bash
# Report all container groups (status, instances, URLs, SSH lines).
# Usage: ./groups.sh [org]   — no arg = every known org in the account.
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/list_groups.py" "$@"
