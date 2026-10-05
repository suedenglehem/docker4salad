#!/bin/bash
# Check the status API endpoints (/startup, /live, /ready) of the llama servers
# running on this machine (docker compose services or run_9b_pure.sh).
#
# Usage: ./check_status.sh [-w|--watch [SECONDS]] [PORT...]
#   PORT...  one or more host ports to check, e.g. ./check_status.sh 9998
#            No ports given -> both compose defaults (9999 and 9998).
#   -w       re-check every SECONDS (default 5) until all probes are ok — handy
#            while a model is still downloading/loading; Ctrl-C to stop early.
#            A number <= 60 right after -w is the interval, otherwise a port.
# Env vars (same names as docker-compose.yml):
#   STATUS_API_HOST           host to probe (default localhost)
#   STATUS_API_HOST_PORT      default port for the 27B service (default 9999)
#   STATUS_API_9B_HOST_PORT   default port for the 9B service  (default 9998)
# Exit codes: 0 = all probes ok · 1 = some probe not ok · 3 = no API reachable.

set -u
TIMEOUT=3
WATCH=0
INTERVAL=5
if [[ "${1:-}" == "-w" || "${1:-}" == "--watch" ]]; then
  WATCH=1; shift
  if [[ "${1:-}" =~ ^[0-9]+$ ]] && (( $1 <= 60 )); then INTERVAL="$1"; shift; fi
fi
HOST="${STATUS_API_HOST:-localhost}"
PORTS=("$@")
(( ${#PORTS[@]} )) || PORTS=("${STATUS_API_HOST_PORT:-9999}" "${STATUS_API_9B_HOST_PORT:-9998}")
for p in "${PORTS[@]}"; do
  [[ "$p" =~ ^[0-9]+$ ]] || { echo "usage: $0 [-w [SECONDS]] [PORT...]" >&2; exit 64; }
done

# Colors (disabled when stdout is not a terminal)
if [[ -t 1 ]]; then
  GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  GREEN=; YELLOW=; RED=; BOLD=; DIM=; RESET=
fi

# Extract a string field from a flat JSON object: get_field '<json>' <key>
get_field() {
  sed -n 's/.*"'"$2"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' <<<"$1"
}

color_for() { # ok -> green, down/not_ready/unreachable/unknown -> red, else yellow
  case "$1" in
    ok) printf '%s' "$GREEN" ;;
    down|not_ready|unreachable|unknown) printf '%s' "$RED" ;;
    *) printf '%s' "$YELLOW" ;;
  esac
}

service_name() { # friendly label for the known compose ports, bare port otherwise
  case "$1" in
    "${STATUS_API_HOST_PORT:-9999}") echo "qwen38-llama (27B)" ;;
    "${STATUS_API_9B_HOST_PORT:-9998}") echo "qwen38-9b (9B)" ;;
    *) echo "port $1" ;;
  esac
}

# check_service <name> <port>
# Returns: 0 = all probes ok · 1 = reachable but some probe not ok · 2 = unreachable
check_service() {
  local name="$1" port="$2" base="http://${HOST}:${port}"
  local resp status detail ep color rc=0

  echo
  echo "${BOLD}== ${name} — ${base}${RESET}"

  if ! resp=$(curl -s --max-time "$TIMEOUT" "${base}/startup"); then
    printf '  %-9s %s%-12s%s %s(service not running / API still starting?)%s\n' \
      "API" "$RED" "unreachable" "$RESET" "$DIM" "$RESET"
    return 2
  fi

  for ep in startup live ready; do
    if ! resp=$(curl -s --max-time "$TIMEOUT" "${base}/${ep}"); then
      status="unreachable"; detail=""
    else
      status=$(get_field "$resp" status)
      detail=$(get_field "$resp" detail)
    fi
    [[ -n "$status" ]] || status="unknown"
    color=$(color_for "$status")
    printf '  %-9s %s%-12s%s' "$ep" "$color" "$status" "$RESET"
    [[ -n "$detail" ]] && printf '  %s(%s)%s' "$DIM" "$detail" "$RESET"
    echo
    [[ "$status" == "ok" ]] || rc=1
  done

  # Summary endpoint (informational, not part of the exit code)
  if resp=$(curl -s --max-time "$TIMEOUT" "${base}/"); then
    echo "  ${DIM}summary: $resp${RESET}"
  fi
  return $rc
}

run_checks() {
  local overall_rc=0 any_reachable=0 port rc
  for port in "${PORTS[@]}"; do
    check_service "$(service_name "$port")" "$port"; rc=$?
    case $rc in
      0) : ;;                    # all ok
      1) overall_rc=1 ;;         # reachable, some probe not ok
      2) : ;;                    # down: warn only, don't fail (other service may be up)
    esac
    [[ $rc -ne 2 ]] && any_reachable=1
  done

  echo
  if (( ! any_reachable )); then
    echo "${YELLOW}No status API reachable on ${HOST} (ports ${PORTS[*]}). Is a server running? Check: docker compose ps${RESET}"
    return 3
  fi
  if (( overall_rc )); then
    echo "${RED}Some probes not ok yet.${RESET}"
    return 1
  fi
  echo "${GREEN}All probes ok.${RESET}"
  return 0
}

if (( WATCH )); then
  echo "${DIM}watch mode: re-checking every ${INTERVAL}s until all probes are ok (Ctrl-C to stop)${RESET}"
  while :; do
    clear 2>/dev/null || true
    printf '[%s]\n' "$(date '+%Y-%m-%d %H:%M:%S')"
    run_checks && break
    sleep "$INTERVAL"
  done
else
  run_checks
fi
