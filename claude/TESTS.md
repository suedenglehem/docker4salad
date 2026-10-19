# claude/ — tests

Two layers. **Offline** runs anytime (no group, no TLS, no money) and proves the
zero-content stream fix in `sl_proxy.py` is intact. **Live** runs against a
running Salad group and proves the full path (real proxy, real gateway, real
Claude Code).

- Run the **offline** suite whenever you touch `sl_proxy.py` — it is the fast
  regression gate for the fix.
- Run the **live** steps when you (re)start a group and want the end-to-end
  path confirmed.

Replace `<group-dns>` with your group's public access domain (the
`networking.dns` from the management API — the `*.salad.cloud` host. It
changes if the group was recreated.)

---

## 0. Offline — zero-content fix logic (no group, no money, anytime)

```sh
python3 -I tools/test_sl_proxy_offline.py
```

Expect **10/10 PASS, exit 0**. It drives the *real* `sl_proxy.py` code —
`_iter_sse()` (anomaly sentinels) and `StreamBuilder` (`saw_content` /
`anomalies` / `is_empty` / `finish`) — over in-memory byte streams and checks:

| # | stream shape | expected |
|---|---|---|
| 1 | empty (200 + headers, then EOF, zero content) | one `error` event |
| 2 | empty think block only (stripped by the stripper) | one `error` event |
| 3 | a non-SSE line mid-stream (e.g. an error page) | `error` + anomaly recorded |
| 4 | in-band upstream `{"error": …}` payload | `error` + anomaly recorded |
| 5 | an unparseable `data:` line | `error` + anomaly recorded |
| 6 | normal text turn | clean finish, text present, **no** error |
| 7 | tool-only turn | clean finish, tool registered, **no** error |
| 8 | content then an anomaly | still `error` (anomaly overrides content) |
| 9 | `is_empty()` contract | true before content, false after |
| 10 | an **unclosed** think block | clean finish (released as real content — no over-trigger) |

No Salad group, no TLS, no billing: the proxy's upstream client
hard-exits on a non-https URL, so a full HTTP round trip is the live test
below, and this harness covers the fix logic deterministically at the unit
level. The only thing it mirrors from the source is the single decision
expression in `_handle_messages` (the block starting `tail = b.strip.flush()`);
it is copied verbatim, so if that block changes, change the test with it.

---

## 1. Live — group is up (baseline, no proxy)

After you (re)start the group:

```sh
cd claude
./sl_curl.sh -url https://<group-dns>.salad.cloud -m qwen38-27b
```

Expect the three known-answer results (27×43, capital of France, one-word
language). This talks to the gateway **directly** (no proxy) — it only proves
the group is serving. If this fails, the group isn't ready; stop here and
sort out the group before running 2–3.

---

## 2. Live — the fixed path (Claude Code through `sl_proxy.py`)

This is the path the fix actually touches — the proxy is what turns a
zero-content stream into an `error` event. Run a short turn and confirm a real
answer comes back with no false error:

```sh
cd claude
./cl_salad https://<group-dns>.salad.cloud -p "Reply with exactly: pong"
```

Expect: `pong` (a short real answer), exit 0, and **no** line mentioning
`upstream stream produced no content`. The proxy log for this run is
`/tmp/cl_salad_proxy.<pid>.log` (the pid is in cl_salad's banner).

Why this matters: the fix's main regression risk is a normal answer being
misread as "empty". A clean `pong` through the proxy proves the change didn't
over-trigger — real content still lands as a clean finish. (A short prompt
deliberately stays far under the Cloudflare edge's ~100 s origin timeout, so
this is a pure no-false-error check, not a trigger attempt.)

---

## 3. The original failure mode (if it occurs)

The bug only fires when the Cloudflare edge kills a long stream — the ~100 s
origin timeout on a big prompt. Measured against a single-instance qwen38 group:

| prompt | TTFB |
|---|---|
| small (~1K tokens), group idle | ~1.7 s |
| worker-shaped (~5K tokens) | ~93 s (done at ~95.5 s) |
| real agentic (12–16K tokens) | at / past the ~100 s edge timeout |

It is timing-dependent and **not reliably reproducible on demand**, so don't
expect to force it. When it does happen during real use you'll now see the
proxy's error instead of a silent empty answer:

```
upstream stream produced no content: (finish_reason=…, output_tokens=0)
# or, for an anomaly:
upstream stream produced no content: non-SSE line from upstream: '…'
upstream stream produced no content: upstream error payload: {"message": …}
upstream stream produced no content: unparseable 'data:' line: '…'
```

That error line **is** the fix working — the same situation previously rendered
as a bogus empty answer. To raise the odds of catching it, run a long agentic
task through `cl_salad` (big context) and watch `/tmp/cl_salad_proxy.<pid>.log`;
a 200 that turns into a zero-content stream is now logged and surfaced, not
swallowed.
