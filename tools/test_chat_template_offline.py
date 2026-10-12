"""Offline checks for docker/chat_template_url.sh (no external network).

Run: python3 -I tools/test_chat_template_offline.py

What it exercises: the helper is driven against a throwaway http.server on
127.0.0.1 that serves the shapes a "downloaded template" actually arrives as --
a real Jinja template, an HTML error page, an empty body, an oversized body,
non-UTF-8 bytes, a git-lfs pointer, a 404. Plus the cache rules (reuse,
re-validate, drop), the scheme rules (https silent, http warns, anything else
rejected), and the stdout contract (success prints ONLY the out path).

Host note: this box has curl and NO wget2, so the curl fallback is what runs
here; the wget2 branch is the same shape. Failure exit codes are asserted as
"non-zero" -- under `set -e` the script propagates curl's own code (a 404 is 22),
which is what the caller needs anyway (any non-zero kills the container).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELPER = os.path.join(_ROOT, "docker", "chat_template_url.sh")

GOOD = """{% for message in messages %}
{% if message['role'] == 'system' %}
<|im_start|>system
{{ message['content'] }}
{% elif message['role'] == 'assistant' %}
<|im_start|>assistant
{{ message['content'] }}
{% else %}
<|im_start|>user
{{ message['content'] }}
{% endif %}
{% endfor %}
<|im_start|>assistant
"""

MARKER = ("{% if message['role'] == 'system' and loop.index > 1 %}"
          "{% raise_exception('System message must be at the beginning.') %}{% endif %}")

ROUTES = {
    "good.jinja": GOOD.encode(),
    "marker.jinja": (GOOD + MARKER).encode(),
    "html.html": b"<!doctype html>\n<html><body><h1>404 Not Found</h1></body></html>\n",
    "empty.jinja": b"",
    "big.jinja": b"{{ x }}" * 700000,                      # 4.2 MB, over the 2 MiB cap
    "binary.jinja": b"\xff\xfe{{ not utf-8 }}\n",
    "plain.jinja": b"just some text, no jinja markup at all\n",
    "lfs.jinja": (b"version https://git-lfs.github.com/spec/v1\n"
                  b"oid sha256:0000000000000000000000000000000000000000000000000000000000000000\n"
                  b"size 8192\n"),
}

REQUESTS = []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        REQUESTS.append(self.path)
        name = self.path.lstrip("/")
        if name not in ROUTES:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"<!doctype html>\n<html><body>nope</body></html>\n")
            return
        body = ROUTES[name]
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # keep the test output clean
        pass


def run(url, out):
    p = subprocess.run(["bash", HELPER, url, out], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def main() -> int:
    if not os.path.isfile(HELPER):
        print(f"missing helper: {HELPER}", file=sys.stderr)
        return 1
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    work = tempfile.mkdtemp(prefix="ctt_")
    fails = []
    try:
        def check(name, cond, detail=""):
            if cond:
                print(f"  ok   {name}")
            else:
                print(f"  FAIL {name}  {detail}")
                fails.append(name)

        # --- 1. usage -------------------------------------------------------
        p = subprocess.run(["bash", HELPER, "http://x/y.jinja"], capture_output=True, text=True)
        check("usage: 1 arg -> rc 2", p.returncode == 2, f"rc={p.returncode}")

        # --- 2. good template over plain http -------------------------------
        out = os.path.join(work, "t.jinja")
        url = f"http://127.0.0.1:{port}/good.jinja"
        rc, so, se = run(url, out)
        check("good: rc 0", rc == 0, f"rc={rc} {se[-200:]}")
        check("good: stdout is exactly the out path", so.strip() == out, repr(so))
        check("good: no stray stdout lines", len(so.strip().splitlines()) == 1, repr(so))
        check("good: file content is the template",
              os.path.isfile(out) and open(out, "rb").read() == ROUTES["good.jinja"])
        check("good: plain-http warning present", "WARNING plain http" in se, se[-200:])
        check("good: validation line (bytes + sha)", "OK" in se and "sha256:" in se, se[-200:])
        check("good: no .part leftovers",
              not [f for f in os.listdir(work) if ".part." in f], os.listdir(work))

        # --- 3. cache reuse (server must not be hit again) ------------------
        n = len(REQUESTS)
        rc, so, se = run(url, out)
        check("cache: rc 0", rc == 0, f"rc={rc}")
        check("cache: 'using cached'", "using cached" in se, se[-200:])
        check("cache: zero new HTTP requests", len(REQUESTS) == n, REQUESTS[n:])

        # --- 4. corrupt cache is re-validated, dropped, re-fetched ----------
        for label, junk in (("html", b"<!doctype html>\n<html>{{ x }}</html>\n"),
                            ("empty", b""),
                            ("binary", b"\xff\xfe{{ x }}")):
            with open(out, "wb") as f:
                f.write(junk)
            rc, so, se = run(url, out)
            check(f"cache: corrupt({label}) re-fetched, not served",
                  rc == 0 and open(out, "rb").read() == ROUTES["good.jinja"]
                  and "using cached" not in se,
                  f"rc={rc} {se[-200:]}")

        # --- 5. documented boundary: a cache that is valid Jinja is reused ---
        # The validator checks shape, not content: a valid-but-wrong template
        # is served. Content correctness stays the operator's responsibility.
        with open(out, "wb") as f:
            f.write(b"{{ wrong_template_but_valid_jinja }}\n")
        rc, so, se = run(url, out)
        check("cache: valid-Jinja cache reused (by design)",
              rc == 0 and "using cached" in se
              and open(out, "rb").read() == b"{{ wrong_template_but_valid_jinja }}\n",
              f"rc={rc} {se[-200:]}")

        # --- 6. every bad body is rejected, and leaves no out file ----------
        for route in ("html.html", "empty.jinja", "big.jinja", "binary.jinja",
                      "plain.jinja", "lfs.jinja", "missing.jinja"):
            out2 = os.path.join(work, f"bad_{route.replace('.', '_')}")
            rc, so, se = run(f"http://127.0.0.1:{port}/{route}", out2)
            check(f"reject {route}: non-zero rc", rc != 0, f"rc={rc}")
            check(f"reject {route}: out file not created", not os.path.exists(out2))
            check(f"reject {route}: nothing on stdout", so.strip() == "", repr(so))
        check("reject big: names the cap",
              "over the 2097152 byte cap" in run(f"http://127.0.0.1:{port}/big.jinja",
                                                 os.path.join(work, "big2"))[2], "")
        check("reject lfs: says no Jinja markup",
              "no Jinja markup" in run(f"http://127.0.0.1:{port}/lfs.jinja",
                                        os.path.join(work, "lfs2"))[2], "")
        check("no .part leftovers after failures",
              not [f for f in os.listdir(work) if ".part." in f], os.listdir(work))

        # --- 7. scheme rules -------------------------------------------------
        rc, so, se = run("ftp://example.com/t.jinja", os.path.join(work, "s1"))
        check("scheme: ftp rejected", rc != 0 and "must be an http(s) URL" in se, se[-120:])
        rc, so, se = run("none", os.path.join(work, "s2"))
        check("scheme: 'none' rejected (entry scripts guard it, helper agrees)",
              rc != 0 and "must be an http(s) URL" in se, se[-120:])
        rc, so, se = run(f"https://127.0.0.1:{port}/good.jinja", os.path.join(work, "s3"))
        check("scheme: https takes the silent branch (no plain-http warning)",
              "WARNING plain http" not in se, se[-200:])
        check("scheme: https download fails against a plain-http server (rc non-zero)",
              rc != 0, f"rc={rc}")

        # --- 8. the Claude-marker NOTE ---------------------------------------
        out3 = os.path.join(work, "m.jinja")
        rc, so, se = run(f"http://127.0.0.1:{port}/marker.jinja", out3)
        check("marker: accepted (rc 0) with a NOTE naming the Claude failure",
              rc == 0 and "NOTE" in se and "System message must be at the beginning" in se,
              se[-300:])
        check("marker: plain template gets no NOTE",
              "NOTE" not in run(f"http://127.0.0.1:{port}/good.jinja",
                                os.path.join(work, "m2.jinja"))[2], "")

        # --- 9. entry-script contract: the caller splices $(...) under set -e -
        # A failure must propagate as a non-zero exit of the whole snippet.
        snippet = ('set -euo pipefail; CT="$(bash "%s" "ftp://x/y.jinja" "%s" 2>/dev/null)"; '
                   'echo SURVIVED' % (HELPER, os.path.join(work, "e1")))
        p = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
        check("caller: helper failure kills the set -e caller (no llama-server start)",
              p.returncode != 0 and "SURVIVED" not in p.stdout,
              f"rc={p.returncode} out={p.stdout!r}")
        snippet = ('set -euo pipefail; CT="$(bash "%s" "%s" "%s" 2>/dev/null)"; '
                   'echo "CT=$CT"' % (HELPER, url, os.path.join(work, "e2")))
        p = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
        check("caller: success splices the path into $CT",
              p.returncode == 0 and f"CT={os.path.join(work, 'e2')}" in p.stdout,
              f"rc={p.returncode} out={p.stdout!r}")

        print(f"\n{len(fails)} failure(s)" if fails else "\nALL CHAT-TEMPLATE-URL CHECKS PASSED")
        return 1 if fails else 0
    finally:
        srv.shutdown()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
