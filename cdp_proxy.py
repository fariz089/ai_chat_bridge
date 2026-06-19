#!/usr/bin/env python3
"""
cdp_proxy.py — tiny TCP proxy that republishes Chrome's CDP port on 0.0.0.0
and fixes the things that break cross-container CDP access.

Why this exists
---------------
Chrome 111+ ALWAYS binds the DevTools/CDP port to 127.0.0.1 (it ignores
``--remote-debugging-address``). To let the ``web`` container reach it we must
forward 0.0.0.0:<public> -> 127.0.0.1:<internal>. Three problems appear:

1. HTTP Host check: since Chrome 66 the HTTP endpoints (/json/version,
   /json/list) REFUSE any request whose Host header is not an IP or "localhost".
   The web container connects to http://chrome:9301 -> Host "chrome:9301" ->
   rejected. We rewrite the request Host header to "localhost".

2. webSocketDebuggerUrl: the JSON Chrome returns embeds a websocket URL built
   from the (rewritten) Host, e.g. ws://localhost/devtools/browser/<id>.
   Playwright then connects to ws://localhost (port 80) -> ECONNREFUSED. We
   rewrite that URL in the response body back to ws://<public>:<port>/... so the
   client reconnects through this proxy.

3. Content-Length: rewriting the body changes its length, so we recompute the
   Content-Length header. We therefore buffer the HTTP response head+body for
   non-websocket responses. WebSocket upgrades (101) and CDP frames stream
   through untouched.

Usage:
    python cdp_proxy.py <listen_port> <target_port> [public_host]
"""
from __future__ import annotations

import re
import socket
import sys
import threading

TARGET_HOST = "127.0.0.1"
BUFSIZE = 65536
HOST_RE = re.compile(rb"\r\nHost:[^\r\n]*\r\n", re.IGNORECASE)
CL_RE = re.compile(rb"\r\nContent-Length:\s*\d+\r\n", re.IGNORECASE)
WS_RE = re.compile(rb"ws://localhost(?::\d+)?/")


def rewrite_request_host(chunk: bytes) -> bytes:
    if HOST_RE.search(chunk):
        return HOST_RE.sub(b"\r\nHost: localhost\r\n", chunk, count=1)
    return chunk


def forward_request(client: socket.socket, upstream: socket.socket):
    """client -> Chrome: rewrite Host header on first chunk, then passthrough."""
    first = True
    try:
        while True:
            data = client.recv(BUFSIZE)
            if not data:
                break
            if first:
                data = rewrite_request_host(data)
                first = False
            upstream.sendall(data)
    except OSError:
        pass
    finally:
        _close(client, upstream)


def forward_response(upstream: socket.socket, client: socket.socket,
                     ws_target: bytes):
    """Chrome -> client.

    For a normal HTTP response we buffer head+body, rewrite ws URLs, fix
    Content-Length, and send. For a 101 Switching Protocols (websocket) we send
    the head and then stream the rest untouched.
    """
    buf = b""
    try:
        # Read until we have the full header block.
        while b"\r\n\r\n" not in buf:
            chunk = upstream.recv(BUFSIZE)
            if not chunk:
                # Connection closed before headers completed; flush what we have.
                if buf:
                    client.sendall(buf)
                _close(upstream, client)
                return
            buf += chunk

        head, _, rest = buf.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0]

        if b" 101 " in status_line:
            # WebSocket upgrade: pass head through, then stream raw.
            client.sendall(head + b"\r\n\r\n" + rest)
            _stream(upstream, client)
            return

        # Determine body length from Content-Length if present.
        m = re.search(rb"Content-Length:\s*(\d+)", head, re.IGNORECASE)
        if m:
            want = int(m.group(1))
            body = rest
            while len(body) < want:
                chunk = upstream.recv(BUFSIZE)
                if not chunk:
                    break
                body += chunk
        else:
            # No Content-Length: read until close.
            body = rest
            while True:
                chunk = upstream.recv(BUFSIZE)
                if not chunk:
                    break
                body += chunk

        new_body = WS_RE.sub(ws_target, body)
        new_head = head
        # Force connection close so neither side waits on keep-alive, and so the
        # recomputed Content-Length is unambiguous. CDP clients open a fresh
        # connection for the websocket anyway.
        if re.search(rb"Connection:", new_head, re.IGNORECASE):
            new_head = re.sub(rb"\r\nConnection:[^\r\n]*\r\n",
                              b"\r\nConnection: close\r\n", new_head, count=1,
                              flags=re.IGNORECASE)
        else:
            new_head = new_head + b"\r\nConnection: close"
        if CL_RE.search(new_head):
            new_head = CL_RE.sub(
                b"\r\nContent-Length: %d\r\n" % len(new_body), new_head, count=1)
        else:
            new_head = new_head + b"\r\nContent-Length: %d" % len(new_body)
        client.sendall(new_head + b"\r\n\r\n" + new_body)
        _close(upstream, client)
    except OSError:
        pass
    finally:
        _close(upstream, client)


def _stream(src, dst):
    try:
        while True:
            data = src.recv(BUFSIZE)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass


def _close(a, b):
    for s in (a, b):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def handle(client: socket.socket, target_port: int, ws_target: bytes):
    try:
        upstream = socket.create_connection((TARGET_HOST, target_port), timeout=10)
    except OSError:
        client.close()
        return
    threading.Thread(target=forward_request, args=(client, upstream),
                     daemon=True).start()
    threading.Thread(target=forward_response, args=(upstream, client, ws_target),
                     daemon=True).start()


def main():
    if len(sys.argv) not in (3, 4):
        print("usage: cdp_proxy.py <listen_port> <target_port> [public_host]",
              file=sys.stderr)
        sys.exit(2)
    listen_port = int(sys.argv[1])
    target_port = int(sys.argv[2])
    public_host = sys.argv[3] if len(sys.argv) == 4 else "localhost"
    ws_target = f"ws://{public_host}:{listen_port}/".encode()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", listen_port))
    srv.listen(128)
    print(f"[cdp_proxy] 0.0.0.0:{listen_port} -> {TARGET_HOST}:{target_port} "
          f"(Host->localhost, ws->{public_host}:{listen_port})", flush=True)

    while True:
        try:
            client, _ = srv.accept()
        except OSError:
            break
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=handle, args=(client, target_port, ws_target),
                         daemon=True).start()


if __name__ == "__main__":
    main()
