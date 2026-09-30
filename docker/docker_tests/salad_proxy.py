#!/usr/bin/env python3
"""salad_proxy — local reverse proxy that injects the Salad-Api-Key header.

SaladCloud's container gateway authenticates ONLY via a `Salad-Api-Key`
header — requests carrying `Authorization: Bearer <salad key>` get a 403.
Most LLM clients (claude CLI, Simon Willison's `llm`, ...) cannot set
arbitrary headers, so point them at this local proxy instead:

    ./salad_proxy.py [UPSTREAM] [LISTEN_PORT]

  UPSTREAM      access domain, e.g. raisin-bean-gy0v5oyd2bt9wfvy.salad.cloud
                (scheme/port optional; default: the qwen38-27b-rtx5090 group,
                https on 443). Do NOT pass the in-container gateway port
                8888 here — salad.cloud is behind Cloudflare, which only
                proxies standard ports; 8888 only exists inside the box.
  LISTEN_PORT   local port (default 8931, bind 127.0.0.1 only).

Every request is forwarded verbatim (any path: /v1/messages,
/v1/chat/completions, /health, ...) with `Salad-Api-Key` added. Responses —
including SSE streams — are passed through. Stdlib only, threaded.

The key is read from $SALAD_API_KEY, else from salad_api.txt next to this
script. GET /__upstream reports which upstream the proxy currently targets
(so callers can detect a stale proxy and restart it). A pidfile lands in
/tmp/salad_proxy.<port>.pid.
"""
import http.client
import os
import signal
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_UPSTREAM = "raisin-bean-gy0v5oyd2bt9wfvy.salad.cloud"
DEFAULT_PORT = 8931

# ---- parse args / env -------------------------------------------------------
upstream = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SALAD_UPSTREAM", DEFAULT_UPSTREAM)
listen_port = int(sys.argv[2] if len(sys.argv) > 2 else os.environ.get("SALAD_PROXY_PORT", DEFAULT_PORT))

https = not upstream.lower().startswith("http://")
rest = upstream.split("://")[-1].rstrip("/")
parts = rest.rsplit(":", 1)
up_host = parts[0]
if len(parts) > 1 and parts[1].isdigit():
    up_port = int(parts[1])
else:
    up_port = 443 if https else 80
UPSTREAM_LABEL = f"{'https' if https else 'http'}://{up_host}:{up_port}"

# ---- key --------------------------------------------------------------------
def load_key():
    k = os.environ.get("SALAD_API_KEY", "").strip()
    if k:
        return k
    try:
        with open(os.path.join(HERE, "salad_api.txt")) as f:
            return f.read().strip()
    except OSError:
        return ""

KEY = load_key()
if not KEY:
    sys.exit("salad_proxy: no key (set SALAD_API_KEY or drop salad_api.txt next to the script)")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "salad_proxy/1.0"

    def log_message(self, fmt, *args):  # one compact line per request
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- helpers --------------------------------------------------------------
    def _read_body(self):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            body = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()  # trailing CRLF
                    return body
                body += self.rfile.read(size)
                self.rfile.readline()
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else None

    def _open_upstream(self):
        if https:
            return http.client.HTTPSConnection(up_host, up_port, timeout=None,
                                                context=ssl.create_default_context())
        return http.client.HTTPConnection(up_host, up_port, timeout=None)

    # -- forwarding -----------------------------------------------------------
    def handle_any(self):
        if self.path == "/__upstream":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(UPSTREAM_LABEL)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(UPSTREAM_LABEL.encode())
            return

        body = self._read_body()
        hop = {"host", "connection", "transfer-encoding", "keep-alive",
               "proxy-authorization", "proxy-authenticate", "te", "upgrade",
               "salad-api-key"}
        headers = {k: v for k, v in self.headers.items() if k.lower() not in hop}
        headers["Salad-Api-Key"] = KEY

        try:
            conn = self._open_upstream()
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception as e:  # upstream unreachable / refused / DNS
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            msg = f"salad_proxy: upstream error: {e}".encode()
            self.send_header("Content-Length", str(len(msg)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(msg)
            return

        has_len = False
        for k, v in resp.getheaders():
            if k.lower() in {"connection", "transfer-encoding", "upgrade"}:
                continue
            if k.lower() == "content-length":
                has_len = True
        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() in {"connection", "transfer-encoding", "upgrade"}:
                continue
            self.send_header(k, v)
        if not has_len:
            self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            if has_len:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            else:
                while True:
                    chunk = resp.read(16384)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-stream; nothing to do

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_OPTIONS = do_PATCH = handle_any


def main():
    with open(f"/tmp/salad_proxy.{listen_port}.pid", "w") as f:
        f.write(str(os.getpid()))
    signal.signal(signal.SIGHUP, lambda *_: sys.exit(0))
    srv = ThreadingHTTPServer(("127.0.0.1", listen_port), Handler)
    srv.daemon_threads = True
    sys.stderr.write(f"salad_proxy: 127.0.0.1:{listen_port} -> {UPSTREAM_LABEL}\n")
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
