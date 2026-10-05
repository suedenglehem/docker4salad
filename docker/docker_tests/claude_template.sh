#!/bin/bash
# =============================================================================
# claude_template.sh — recover the chat template from the freshly downloaded
# GGUF and apply the one-line Claude Code patch, at container start.
#
# WHY: the stock Qwen-family template RAISES on any non-first system message
# ({{- raise_exception('System message must be at the beginning.') }}), and
# Claude Code sends system context mid-conversation. A static jinja file baked
# into the image can drift from the GGUF actually being served — so instead of
# shipping one, we DUMP the template from the exact model file and replace that
# single line with an inline render of the system message. The result is
# passed to llama-server via --chat-template-file.
#
# Zero dependencies: pure stdlib Python 3 (struct only). No pip, no gguf-py
# (numpy/tqdm/pyyaml/requests), no network. The GGUF v2/v3 header layout is
# fixed; we read kvs until tokenizer.chat_template and skip every earlier
# value correctly (scalars, strings, nested arrays).
#
# Usage: claude_template.sh <model.gguf> <out.jinja>
#
# Contract (the caller splices this into the image CMD under set -euo pipefail):
#   * patched            -> exit 0, prints the output path on STDOUT
#   * marker absent      -> exit 0, prints nothing, WARNING on stderr
#                           (model is not Qwen-family / already permissive;
#                            caller keeps whatever template it had before)
#   * hard error (file
#     missing/malformed) -> exit 1 on stderr -> container dies loudly
# =============================================================================
set -euo pipefail

MODEL="$1"
OUT="$2"

python3 - "$MODEL" "$OUT" <<'PY'
import os
import struct
import sys

path, out = sys.argv[1], sys.argv[2]

MARKER = "raise_exception('System message must be at the beginning.')"
# The Claude patch, one line (byte-verified against the proven
# qwen3.8.q6.jinja.claude reference, whose line 106 is exactly this):
# render the mid-conversation system message in the template's own
# system framing instead of raising. The two backslash-n sequences
# are LITERAL two-char Jinja escapes (bytes 5c 6e — same idiom as the
# stock template's own '\n' literals); built with chr(92) so no
# editor/model round-trip can ever turn them into real newlines.
BS = chr(92)  # one backslash
NEW_EXPR = ("{{- '<|im_start|>system" + BS + "n' + content + "
            "'<|im_end|>' + '" + BS + "n' }}")
TARGET = "tokenizer.chat_template"
SCALAR_SIZES = {
    0: 1,  # GGUF_TYPE_U8
    1: 1,  # GGUF_TYPE_I8
    2: 2,  # GGUF_TYPE_U16
    3: 2,  # GGUF_TYPE_I16
    4: 4,  # GGUF_TYPE_U32
    5: 4,  # GGUF_TYPE_I32
    6: 4,  # GGUF_TYPE_F32
    7: 1,  # GGUF_TYPE_BOOL
    # (GGUF_TYPE_STR = 8 is NOT a scalar here: it is length-prefixed and
    #  handled by Reader.string() in skip_value below)
    10: 8, # GGUF_TYPE_U64
    11: 8, # GGUF_TYPE_I64
    12: 8, # GGUF_TYPE_F64
}


class GgufError(Exception):
    pass


def die(msg):
    raise GgufError(msg)


def u32(b):
    return struct.unpack("<I", b)[0]


def u64(b):
    return struct.unpack("<Q", b)[0]


class Reader:
    """Offset-based reader over the file, raising GgufError on overrun."""

    def __init__(self, f, size):
        self.f = f
        self.off = 0
        self.size = size

    def take(self, n, what):
        if n < 0 or self.off + n > self.size:
            die("truncated read of %s at offset %d (file size %d)" % (what, self.off, self.size))
        self.f.seek(self.off)
        data = self.f.read(n)
        if len(data) != n:
            die("short read of %s at offset %d" % (what, self.off))
        self.off += n
        return data

    def string(self, what="string"):
        (ln,) = struct.unpack("<Q", self.take(8, what + " length"))
        raw = self.take(ln, what)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            die("invalid UTF-8 in %s at offset %d" % (what, self.off - ln))


def skip_value(r, vtype, what):
    """Advance past one GGUF value of the given type."""
    if vtype in SCALAR_SIZES:
        r.take(SCALAR_SIZES[vtype], what)
        return
    if vtype == 8:  # GGUF_TYPE_STR
        r.string(what)
        return
    if vtype == 9:  # GGUF_TYPE_ARR: element type + count + elements
        etype = u32(r.take(4, what + " array element type"))
        count = u64(r.take(8, what + " array count"))
        for _ in range(count):
            skip_value(r, etype, what + " element")
        return
    die("unknown GGUF value type %d in %s" % (vtype, what))


def main():
    try:
        with open(path, "rb") as f:
            r = Reader(f, os.fstat(f.fileno()).st_size)
            magic = u32(r.take(4, "magic"))
            if magic != 0x46554747:  # "GGUF" (bytes 47 47 55 46) as u32 LE
                die("not a GGUF file (bad magic 0x%08x)" % magic)
            version = u32(r.take(4, "version"))
            if version not in (2, 3):
                die("unsupported GGUF version %d" % version)
            # v2 and v3 share the same header layout (magic, version,
            # tensor_count, kv_count, kvs, tensor infos, data) — no per-version
            # field to skip.
            r.take(8, "tensor_count")
            kv_count = u64(r.take(8, "metadata_kv_count"))
            for i in range(kv_count):
                key = r.string("kv key %d" % i)
                vtype = u32(r.take(4, "kv %r type" % key))
                if key == TARGET:
                    if vtype != 8:
                        die("unexpected: %s is not a string (type %d)" % (TARGET, vtype))
                    template = r.string(TARGET)
                    if MARKER not in template:
                        sys.stderr.write(
                            "WARNING: %s: no Claude patch marker in embedded chat "
                            "template (not stock Qwen-family, or already patched) — "
                            "keeping whatever template the caller had\n" % path
                        )
                        return
                    if template.count(MARKER) != 1:
                        die("expected exactly 1 marker occurrence, found %d"
                            % template.count(MARKER))
                    # Repair guard: NEW_EXPR must be ONE line containing the
                    # LITERAL two-char escape sequence backslash+n (10 chars of
                    # Jinja source: backslash, n) — never a real line break.
                    # If the line was ever corrupted (an editor or model
                    # round-trip turning the escapes into real newlines),
                    # re-escape them; if it still contains a raw newline
                    # afterwards, refuse to write a corrupted template.
                    nl = chr(10)          # real newline
                    bsn = chr(92) + "n"   # the two chars: backslash, n
                    fixed = NEW_EXPR.replace(nl, bsn)
                    if nl in fixed:
                        die("NEW_EXPR still contains a raw newline after repair"
                            + " — refusing to write a corrupted template")
                    if fixed != NEW_EXPR:
                        sys.stderr.write(
                            "WARNING: NEW_EXPR had real newlines; re-escaped to"
                            " literal backslash-n before writing\n")
                    # Line-based replacement (per design): swap the WHOLE line
                    # containing the marker. The stock line is a complete
                    # `{{- raise_exception(...) }}` tag, so a substring swap
                    # would nest a fresh `{{- ... }}` inside the old one. Keep
                    # the line's own indentation (8 spaces in 27B, 12 in the
                    # 35B/Ornith variant); the expression is the proven
                    # reference line verbatim.
                    lines = template.split("\n")
                    hit = next(i for i, l in enumerate(lines) if MARKER in l)
                    line = lines[hit]
                    eol = "\r" if line.endswith("\r") else ""
                    if eol:
                        line = line[:-1]
                    stripped = line.lstrip()
                    indent = line[: len(line) - len(stripped)]
                    lines[hit] = indent + fixed + eol
                    patched = "\n".join(lines)
                    with open(out, "w") as o:
                        o.write(patched)
                    print(out)
                    return
                skip_value(r, vtype, "kv %r value" % key)
            die("%s: metadata has no %s key (read all %d kvs)" % (path, TARGET, kv_count))
    except GgufError as e:
        sys.stderr.write("claude_template: ERROR: %s\n" % e)
        sys.exit(1)
    except OSError as e:
        sys.stderr.write("claude_template: ERROR: %s\n" % e)
        sys.exit(1)


main()
PY
