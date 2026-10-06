#!/usr/bin/env python3
"""Print llama-server statistics (tps / queue / totals) from /metrics.

Works against any llama-server endpoint with `--metrics` enabled (all the
Dockerfiles in this repo pass it):

  In-container (installed at /usr/local/bin/ by Dockerfile.multistage,
  wrapped by stats.sh which passes the local URL):
      stats.sh
  From the host, against a local server:
      python3 llama_stats.py http://127.0.0.1:8080
  From the host, against a Salad group's Container Gateway:
      python3 llama_stats.py https://<group>.salad.cloud

Salad gateways require the Salad API key as a `Salad-Api-Key` header (same
convention as claude/curl_salad.sh); for a *.salad.cloud target it is
loaded automatically from salad_api.txt (repo root, or claude/salad_api.txt).
If the server was started with --api-key, pass the LLM key too:
`--api-key KEY` or env `API_KEY`.

`--raw` dumps the raw Prometheus body (useful when a newer llama.cpp build
renames metrics). By default it re-scrapes every 5 seconds (Ctrl-C stops);
`--once` (or `--interval 0`) makes it a single scrape, `--interval N`
changes the period.

Metric names below were verified 2026-10-06 against a live server from the
pinned llama.cpp build (commit 3af988fa, build b10572); anything else under
the llamacpp: prefix is printed under "other metrics" so a name change in a
future build shows up instead of being silently dropped.

The repo-root llama_stats.py is a thin delegator to THIS file: the canonical
code lives here because it must be inside Dockerfile.multistage's build
context (context = docker/), which the repo root is not.

Stdlib only (urllib), matching salad_client.py convention.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "llama-stats/1.0"  # the CDN in front of salad.cloud rejects urllib's default UA
TIMEOUT_S = 15

HERE = os.path.dirname(os.path.abspath(__file__))
# Key-file search order; resolves to repo-root salad_api.txt when the
# canonical file sits in docker/ (the runpy delegator sets __file__ here).
_SALAD_KEY_CANDIDATES = (
    os.path.join(HERE, "salad_api.txt"),
    os.path.join(HERE, "..", "salad_api.txt"),
    os.path.join(HERE, "..", "claude", "salad_api.txt"),
)

# Metric names (verified against the live b10572 server, 2026-10-06):
_GEN_TPS = "llamacpp:predicted_tokens_seconds"        # gauge, t/s
_PROMPT_TPS = "llamacpp:prompt_tokens_seconds"        # gauge, t/s
_PROMPT_TOKENS = "llamacpp:prompt_tokens_total"       # counter
_PROMPT_TOKENS_CACHED = "llamacpp:prompt_tokens_cached_total"
_PREDICTED_TOKENS = "llamacpp:tokens_predicted_total"
_N_DECODE = "llamacpp:n_decode_total"
_N_TOKENS_MAX = "llamacpp:n_tokens_max"               # "counter" per HELP, but a max
_REQ_PROCESSING = "llamacpp:requests_processing"      # gauge
_REQ_DEFERRED = "llamacpp:requests_deferred"          # gauge
_BUSY_SLOTS = "llamacpp:n_busy_slots_per_decode"      # gauge
_SPEC_DRAFT_TOKENS = "llamacpp:spec_decode_num_draft_tokens_total"
_SPEC_ACCEPTED = "llamacpp:spec_decode_num_accepted_tokens_total"
_SPEC_DRAFTS = "llamacpp:spec_decode_num_drafts_total"

_SHOWN = frozenset({
    _GEN_TPS, _PROMPT_TPS, _PROMPT_TOKENS, _PROMPT_TOKENS_CACHED,
    _PREDICTED_TOKENS, _N_DECODE, _N_TOKENS_MAX, _REQ_PROCESSING,
    _REQ_DEFERRED, _BUSY_SLOTS, _SPEC_DRAFT_TOKENS, _SPEC_ACCEPTED, _SPEC_DRAFTS,
})


def parse_metrics(body: str) -> dict:
    """Prometheus text -> {name: [(labels, value), ...]} (first sample per
    series is what's rendered; this build emits no label sets)."""
    out: dict = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition(" ")
        try:
            value = float(rest.rsplit(None, 1)[-1])
        except ValueError:
            continue
        labels = rest[: len(rest) - len(rest.rsplit(None, 1)[-1])].strip().strip("{}")
        out.setdefault(name, []).append((labels, value))
    return out


def first(metrics: dict, name: str) -> float:
    samples = metrics.get(name)
    return samples[0][1] if samples else 0.0


def load_salad_key(url: str) -> "str | None":
    """Salad API key for a *.salad.cloud target; None otherwise (local
    endpoints never need it)."""
    host = urllib.parse.urlparse(url).hostname or ""
    if not host.endswith(".salad.cloud"):
        return None
    for path in _SALAD_KEY_CANDIDATES:
        path = os.path.normpath(path)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
    return None


def fetch_metrics(url: str, salad_key: "str | None", api_key: "str | None") -> str:
    headers = {"User-Agent": USER_AGENT}
    if salad_key:
        headers["Salad-Api-Key"] = salad_key
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url.rstrip("/") + "/metrics", headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        return resp.read().decode("utf-8", "replace")


def _http_hint(url: str, code: int) -> str:
    host = urllib.parse.urlparse(url).hostname or ""
    if host.endswith(".salad.cloud") and code in (401, 403):
        return "\nhint: gateway rejected auth — Salad key missing/wrong, or pass --api-key for the LLM key."
    if code == 404:
        return "\nhint: a Salad group gateway 404s on EVERY path while the group is STOPPED (python3 manage_groups.py)."
    return ""


def render(url: str, m: dict) -> str:
    g = lambda n: first(m, n)
    lines = [
        f"llama-server stats  {url}",
        "",
        f"  generation   {g(_GEN_TPS):9.2f} t/s    prompt   {g(_PROMPT_TPS):9.2f} t/s",
        f"  processing   {g(_REQ_PROCESSING):3.0f}    deferred {g(_REQ_DEFERRED):3.0f}    "
        f"busy slots/decode {g(_BUSY_SLOTS):4.1f}",
        f"  prompt tokens   {g(_PROMPT_TOKENS):>10,.0f}    (cached {g(_PROMPT_TOKENS_CACHED):,.0f})",
        f"  generated tok   {g(_PREDICTED_TOKENS):>10,.0f}    "
        f"decode calls {g(_N_DECODE):,.0f}    max seq len {g(_N_TOKENS_MAX):,.0f}",
    ]
    if g(_SPEC_DRAFTS) > 0:
        drafted = g(_SPEC_DRAFT_TOKENS)
        acc = 100.0 * g(_SPEC_ACCEPTED) / drafted if drafted else 0.0
        lines.append(
            f"  spec decode:   {g(_SPEC_DRAFTS):,.0f} drafts    "
            f"{drafted:,.0f} draft tok    {g(_SPEC_ACCEPTED):,.0f} accepted ({acc:.0f}%)"
        )
    other = {n: s for n, s in m.items() if n not in _SHOWN}
    if other:
        lines.append("")
        lines.append("  other metrics:")
        for n in sorted(other):
            label, value = other[n][0]
            lines.append(f"    {n}{'{' + label + '}' if label else ''} = {value}")
    return "\n".join(lines)


def main(argv: list) -> None:
    args = list(argv[1:])
    raw = "--raw" in args
    args = [a for a in args if a != "--raw"]
    interval = 5.0
    once = "--once" in args
    args = [a for a in args if a != "--once"]
    if "--interval" in args:
        i = args.index("--interval")
        try:
            interval = float(args[i + 1])
        except (IndexError, ValueError):
            sys.exit("usage: --interval N (seconds)")
        del args[i:i + 2]
    if once:
        interval = 0.0
    api_key = os.environ.get("API_KEY") or None
    if "--api-key" in args:
        i = args.index("--api-key")
        api_key = args[i + 1]
        del args[i:i + 2]
    url = args[0] if args else "http://127.0.0.1:8080"
    salad_key = load_salad_key(url)

    while True:
        try:
            body = fetch_metrics(url, salad_key, api_key)
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode("utf-8", "replace")
            sys.exit(
                f"HTTP {e.code} from {url}/metrics"
                + (f": {detail}" if detail.strip() else "")
                + _http_hint(url, e.code)
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            reason = getattr(e, "reason", e)
            sys.exit(
                f"cannot reach {url}: {reason}\n"
                "is llama-server up? (in-container: `docker ps` + logs; "
                "Salad: is the group RUNNING — `python3 manage_groups.py`)"
            )
        print(body if raw else render(url, parse_metrics(body)))
        if interval <= 0:
            break
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main(sys.argv)
    except KeyboardInterrupt:
        pass
