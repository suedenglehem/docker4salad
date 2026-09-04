"""Launcher for the status API (the app itself is api_app.py).

Why not just `python3 -m uvicorn api_app:app --host ...`? Because CPython's
asyncio create_server() forces IPV6_V6ONLY on any v6 socket it creates
itself, which breaks dual-stack binding when API_HOST="::" — a single [::]
bind would then stop accepting IPv4 connections (127.0.0.1 and the host's
v4 port-forward). Binding the socket here with V6ONLY=0 and handing it to
uvicorn via serve(sockets=[...]) skips that code path, so one listener
accepts both stacks.

Env config: API_HOST (default 0.0.0.0), API_PORT (default 9999).
"""

import asyncio
import os
import socket

import uvicorn


def _bind() -> socket.socket:
    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "9999"))
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        # Dual stack: also accept IPv4 (v4-mapped) connections on this bind.
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind((host, port))
    sock.listen(128)
    return sock


async def _serve(sock: socket.socket) -> None:
    config = uvicorn.Config("api_app:app", log_level="info")
    server = uvicorn.Server(config=config)
    await server.serve(sockets=[sock])


def main() -> None:
    sock = _bind()
    try:
        asyncio.run(_serve(sock))
    finally:
        sock.close()


if __name__ == "__main__":
    main()
