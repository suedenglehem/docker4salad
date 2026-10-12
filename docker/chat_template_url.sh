#!/bin/bash
# =============================================================================
# chat_template_url.sh — fetch a CUSTOM chat template from an http(s) URL,
# validate it, and print the local path for llama-server's --chat-template-file.
#
# WHY: the image offers two template sources — CHAT_TEMPLATE (a static path
# baked into the image) and CLAUDE_TEMPLATE (dump the template out of the
# served GGUF and apply the one-line Claude patch). Both need the file to be
# inside the image, so a template from anywhere else means building an image.
# This is the third source: point CHAT_TEMPLATE_URL at a URL and the container
# downloads it at startup (same shape as the old DRAFT_MODEL_URL /
# VISION_MODEL_URL mechanism).
#
# Usage: chat_template_url.sh <url> <out.jinja>
#
# Contract (the caller splices this into the entry script under
# set -euo pipefail, redirecting stderr to ${API_STATE_DIR}/chat_template_url.log):
#   * ok        -> exit 0, prints <out> on STDOUT
#   * any failure -> exit 1 with the reason on stderr, and the caller DIES.
#     Deliberate: a silently-wrong chat template is the exact failure mode
#     this project has already been burned by (the mid-conversation-system
#     path loop), so "downloaded something, hope it is a template" is not an
#     option. A dead container + restart_policy=never = a STOPPED group, i.e.
#     a loud failure that does not bill.
#
# Validation (the downloaded bytes are untrusted data — an HTML error page, a
# 404 body, a git-lfs pointer or a truncated file all "download fine"):
#   * 1 byte .. MAX_BYTES (2 MiB; real templates are single-digit KB)
#   * strict UTF-8
#   * contains Jinja markup ({{ or {%)
#   * does not start like an HTML document
# A cached copy is re-validated before reuse, so a truncated download from a
# killed container is re-fetched instead of being served forever.
#
# No dependencies beyond stdlib python3 + wget2 (curl fallback). The template
# is DATA handed to llama.cpp's Jinja renderer — it is never executed here.
# =============================================================================
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "chat_template_url: usage: $0 <url> <out.jinja>" >&2
  exit 2
fi

URL="$1"
OUT="$2"
MAX_BYTES=2097152

# validate PATH -> 0 when the file is usable as a chat template, 1 with a reason.
validate() {
  python3 - "$1" "$MAX_BYTES" <<'PY'
import hashlib
import os
import sys

path, cap = sys.argv[1], int(sys.argv[2])
MARKER = "raise_exception('System message must be at the beginning.')"


def fail(msg):
    sys.stderr.write("chat_template_url: REJECT %s: %s\n" % (path, msg))
    sys.exit(1)


size = os.path.getsize(path)
if size == 0:
    fail("empty file")
if size > cap:
    fail("%d bytes, over the %d byte cap (a chat template is a few KB)" % (size, cap))
raw = open(path, "rb").read()
try:
    text = raw.decode("utf-8")
except UnicodeDecodeError as e:
    fail("not valid UTF-8 (%s)" % e)
head = text.lstrip()[:512].lower()
if head.startswith("<!doctype") or head.startswith("<html"):
    fail("starts like an HTML document, not a Jinja template")
if "{{" not in text and "{%" not in text:
    fail("no Jinja markup (no '{{' or '{%') — not a chat template")
sys.stderr.write(
    "chat_template_url: OK %d bytes sha256:%s\n" % (size, hashlib.sha256(raw).hexdigest()[:12]))
if MARKER in text:
    sys.stderr.write(
        "chat_template_url: NOTE this template raises on a system message that is not "
        "first ('System message must be at the beginning.'). Claude Code sends system "
        "context mid-conversation, so it will fail against this template — use "
        "CLAUDE_TEMPLATE=1 (which patches that line) instead of a URL, or patch the "
        "template at its source.\n")
PY
}

case "$URL" in
  https://*) : ;;
  http://*)
    echo "chat_template_url: WARNING plain http (the template content is unverified, and a" \
         "template shapes model behavior): $URL" >&2 ;;
  *)
    echo "chat_template_url: REJECT CHAT_TEMPLATE_URL must be an http(s) URL, got: $URL" >&2
    exit 1 ;;
esac

if [ -f "$OUT" ] && validate "$OUT" >/dev/null 2>&1; then
  echo "chat_template_url: using cached $OUT" >&2
  echo "$OUT"
  exit 0
fi
# A cached file that fails validation is dropped — never served, never reused.
rm -f "$OUT"

TMP="${OUT}.part.$$"
trap 'rm -f "$TMP"' EXIT
echo "chat_template_url: fetching $URL" >&2
if command -v wget2 >/dev/null 2>&1; then
  wget2 -q -O "$TMP" "$URL"
elif command -v curl >/dev/null 2>&1; then
  curl -fsSL -o "$TMP" "$URL"
else
  echo "chat_template_url: REJECT neither wget2 nor curl in this image" >&2
  exit 1
fi
validate "$TMP" >&2
mv -f "$TMP" "$OUT"
trap - EXIT
echo "$OUT"
