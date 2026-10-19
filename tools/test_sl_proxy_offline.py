"""Offline checks for claude/sl_proxy.py — the zero-content stream fix (no network).

Run: python3 -I tools/test_sl_proxy_offline.py

What it exercises: the fix makes a stream that ends with NO usable content
(no text, no tool calls) — or with an upstream anomaly — surface as ONE
Anthropic `error` SSE event instead of a clean empty `message_stop`. That was
the bug that turned a Cloudflare-edge-killed long stream (200 + SSE headers,
then EOF with zero content) into a bogus empty answer.

The test drives the REAL `claude/sl_proxy.py` code — `_iter_sse()` (anomaly
sentinels) and `StreamBuilder` (saw_content / anomalies / is_empty / finish) —
over in-memory byte streams. The only thing mirrored from the source is the
single decision expression in `_handle_messages` (the block starting
`tail = b.strip.flush()`); it is copied verbatim below, so if that block
changes the test must change with it.

No upstream, no TLS, no Salad group, no money: `sl_proxy.py`'s `Upstream`
client hard-exits on a non-https URL, so a full end-to-end HTTP round trip is
the LIVE test (see claude/TESTS.md), and this harness covers the fix logic
deterministically at the unit level.

Expect: 10/10 PASS, exit 0.
"""
import importlib.util
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROXY = os.path.join(_ROOT, "claude", "sl_proxy.py")

_spec = importlib.util.spec_from_file_location("sl_proxy", _PROXY)
sl_proxy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sl_proxy)

_iter_sse = sl_proxy._iter_sse
StreamBuilder = sl_proxy.StreamBuilder
_ev = sl_proxy._ev


class FakeResp:
    """Duck-typed response: readline() returns the next bytes line, b"" at EOF."""

    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        return self._lines.pop(0) if self._lines else b""


def sse(obj):
    """A well-formed OpenAI SSE data line."""
    return ("data: " + json.dumps(obj) + "\n").encode("utf-8")


def delta(content=None, tool=None, fr=None, usage=None):
    """Build one OpenAI chat-completion chunk."""
    d = {}
    if content is not None:
        d["content"] = content
    if tool is not None:
        d["tool_calls"] = [tool]
    ch = {"delta": d, "finish_reason": fr}
    out = {"choices": [ch]}
    if usage is not None:
        out["usage"] = usage
    return out


def run_stream(lines):
    """Drive the proxy's real streaming path over `lines` (bytes, one per SSE
    line) and return (frames, error_message_or_None, builder).

    `frames` is the list of SSE strings the client would receive.
    The `if b.anomalies or (not tail and b.is_empty()):` test is copied
    verbatim from `_handle_messages` in claude/sl_proxy.py.
    """
    b = StreamBuilder("qwen38-27b", "test")
    frames = [b.message_start()]
    err = None
    for chunk in _iter_sse(FakeResp(lines)):
        for frame in b.handle(chunk):
            frames.append(frame)
    tail = b.strip.flush()
    if b.anomalies or (not tail and b.is_empty()):
        detail = "; ".join(b.anomalies[:3]) or (
            "stream ended with no content "
            "(finish_reason=%s, output_tokens=%d)"
            % (b.finish_reason, b.usage_out))
        err = "upstream stream produced no content: %s" % detail
        frames.append(_ev("error", {"type": "error",
                     "error": {"type": "api_error", "message": err}}))
    else:
        for frame in b.finish(tail):
            frames.append(frame)
    return frames, err, b


def has_text(frames):
    return any('content_block_delta' in f and '"text_delta"' in f for f in frames)


def has_tool(frames):
    return any('"tool_use"' in f for f in frames)


# Build the think tags from the module's own open-tag constant so the test can
# never carry a stray backslash in them. An EMPTY block (open + whitespace +
# close) is what the stripper swallows; an UNCLOSED open tag is real content.
_THINK_OPEN = sl_proxy._THINK_OPEN                 # literal open tag
_THINK_CLOSE = "<" + "/think" + ">"                # literal close tag
EMPTY_BLOCK = _THINK_OPEN + "\n\n" + _THINK_CLOSE
UNCLOSED_BLOCK = _THINK_OPEN + "\n\n"


CHECKS = []


def check(name, cond, note=""):
    CHECKS.append((name, bool(cond), note))


# --- 1. empty stream (the core bug: 200 + headers, then EOF, zero content) ---
frames, err, b = run_stream([])
check("empty stream -> error event",
      err is not None and not has_text(frames) and not has_tool(frames),
      err or "(no error emitted)")

# --- 2. think-only: an empty (closed) think block is stripped, no content ---
frames, err, b = run_stream([sse(delta(content=EMPTY_BLOCK))])
check("think-only (empty block stripped) -> error",
      err is not None and not has_text(frames),
      err or "(no error emitted)")

# --- 3. non-SSE line mid-stream (e.g. an HTML error page) ---
frames, err, b = run_stream([b"<html>502 Bad Gateway</html>\n"])
check("non-SSE line -> error + anomaly recorded",
      err is not None and len(b.anomalies) >= 1 and "non-SSE" in err,
      err or "(no error emitted)")

# --- 4. in-band upstream error payload (terminal) ---
frames, err, b = run_stream([sse({"error": {"message": "engine died"}})])
check("in-band error payload -> error + anomaly recorded",
      err is not None and len(b.anomalies) >= 1 and "error payload" in err,
      err or "(no error emitted)")

# --- 5. unparseable 'data:' line ---
frames, err, b = run_stream([b"data: {not valid json\n"])
check("unparseable data line -> error + anomaly recorded",
      err is not None and len(b.anomalies) >= 1 and "unparseable" in err,
      err or "(no error emitted)")

# --- 6. normal text: NO error, content passed through ---
frames, err, b = run_stream([sse(delta(content="Hello there", fr="stop"))])
check("normal text -> clean success, content present",
      err is None and b.saw_content and not b.is_empty() and has_text(frames),
      err or "ok")

# --- 7. tool-only: NO error (tool_calls count as content) ---
frames, err, b = run_stream([
    sse(delta(tool={"index": 0, "id": "call_1",
                    "function": {"name": "get_weather",
                                 "arguments": '{"city":"Paris"}'}})),
    sse(delta(fr="tool_calls")),
])
check("tool-only -> clean success, tool registered",
      err is None and not b.is_empty() and b.tools and has_tool(frames),
      err or "ok")

# --- 8. content THEN anomaly: the anomaly still wins (error) ---
frames, err, b = run_stream([
    sse(delta(content="partial answer")),
    b"connection reset by peer\n",
])
check("content + anomaly -> still error (anomaly overrides)",
      err is not None and len(b.anomalies) >= 1,
      err or "(no error emitted)")

# --- 9. is_empty() contract on a bare builder ---
b = StreamBuilder("qwen38-27b", "t2")
fresh_empty = b.is_empty()
b.handle(delta(content="x"))
check("is_empty(): True before content, False after",
      fresh_empty and not b.is_empty(),
      "fresh=%s after_text=%s" % (fresh_empty, b.is_empty()))

# --- 10. unclosed think block: real content, NOT flagged empty (no over-trigger) ---
frames, err, b = run_stream([sse(delta(content=UNCLOSED_BLOCK))])
check("unclosed think block -> clean success (released as content)",
      err is None and has_text(frames),
      err or "ok")

# --- report ---
failed = 0
for name, ok, note in CHECKS:
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        failed += 1
        print("         %s" % note)
print("\n%d/%d checks passed" % (len(CHECKS) - failed, len(CHECKS)))
sys.exit(1 if failed else 0)
