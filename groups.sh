#!/usr/bin/env bash
# Manage container groups: list (default), refresh, start, stop, wait, delete.
# Usage: ./groups.sh [action] [org]   — no action = status table, all known orgs.
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/manage_groups.py" "$@"
