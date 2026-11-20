#!/usr/bin/env python3
"""The front door of the always-on Remote Compute: it checks the bearer token of every MCP request and hands the
request to that token's worker. It runs no command and touches no file for a client; the workers (server.py, one per
token, each in its own systemd unit under the sandboxed account) do that.

Python 3.9+, standard library only. Under systemd it listens on the sockets it is handed (--systemd-socket): a Unix
socket that only the tunnel to the relay may connect to, so that nothing on this machine, the sessions least of
all, can talk to it or take its place. Without systemd it listens on 127.0.0.1:

    python3 gateway.py --config /etc/compu-connect/config.json --port 8765

Tokens come from the store mcpctl writes. This process can read the store, which holds hashes and no tokens, and
cannot write it. It keeps the list of sessions (who is connected, from where, doing what) in a state file for
`mcpctl sessions`, and prints one JSON line per request as the audit log.
"""

import argparse
import hmac
import http.client
import json
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cclib

BODY_MAX = 48 * 1024 * 1024
# A worker's run waits at most 90 s; this outlasts it.
WORKER_TIMEOUT_S = 130
# A worker runs as the account its commands run as, so what it answers is no more trusted than they are: the size
# of a reply and the number of requests one token can keep waiting are bounded, so that one token cannot take the
# gateway away from the others.
REPLY_MAX = 64 * 1024 * 1024
INFLIGHT_MAX = 16
# A session nobody has used for this long is forgotten.
SESSION_IDLE_DROP_S = 24 * 3600
SESSIONS_MAX = 2000
AUDIT_DETAIL_MAX = 2000
SID_RE = re.compile(r"^[\x21-\x7e]{1,128}$")
IP_RE = re.compile(r"^[0-9A-Fa-f:.]{2,45}$")

CFG = dict(cclib.DEFAULTS)
READER = None
SESSIONS = None


def audit(event, **fields):
    row = {"ts": round(time.time(), 3), "event": event}
    row.update((k, v) for k, v in fields.items() if v is not None)
    print(json.dumps(row, ensure_ascii=False), flush=True)


class Quiet:
    """Lets an event through at most `burst` times a minute, so strangers knocking cannot flood the log."""

    def __init__(self, burst=20):
        self.burst, self.lock, self.minute, self.count = burst, threading.Lock(), 0, 0

    def ok(self):
        with self.lock:
            minute = int(time.time() // 60)
            if minute != self.minute:
                if self.count > self.burst:
                    audit("auth_fail_suppressed", count=self.count - self.burst)
                self.minute, self.count = minute, 0
            self.count += 1
            return self.count <= self.burst


AUTH_FAIL_LOG = Quiet()


class Inflight:
    """How many requests of each token are with its worker right now."""

    def __init__(self):
        self.lock, self.count = threading.Lock(), {}

    def enter(self, tid):
        with self.lock:
            if self.count.get(tid, 0) >= INFLIGHT_MAX:
                return False
            self.count[tid] = self.count.get(tid, 0) + 1
            return True

    def leave(self, tid):
        with self.lock:
            self.count[tid] -= 1
            if not self.count[tid]:
                del self.count[tid]


INFLIGHT = Inflight()


class Sessions:
    """Who is connected. A session is one MCP session (an initialize and what follows it) of one token; a client that
    sends no session id is one session per token and address."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.rows = {}
        self.usage = {}
        self.dirty = True
        self.started = time.time()
        try:
            with open(path) as f:
                old = json.load(f)
            self.rows = {(r["token"], r["sid"]): r for r in old.get("sessions", [])}
            self.usage = old.get("tokens", {})
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def touch(self, tok, sid, ip, agent, client=None, messages=()):
        now = time.time()
        with self.lock:
            key = (tok["id"], sid)
            row = self.rows.get(key)
            if row is None:
                row = self.rows[key] = {
                    "token": tok["id"],
                    "sid": sid,
                    "implicit": sid.startswith("-"),
                    "created": now,
                    "requests": 0,
                    "calls": 0,
                    "last_tool": None,
                    "client": None,
                }
                audit("session_start", token=tok["id"], label=tok.get("label"), session=sid, ip=ip, client=client)
            row.update(label=tok.get("label"), ip=ip, agent=agent, last_seen=now)
            if client:
                row["client"] = client
            row["requests"] += 1
            for _, tool, _ in messages:
                if tool:
                    row["calls"] += 1
                    row["last_tool"] = tool
            use = self.usage.setdefault(tok["id"], {"first_used": now, "requests": 0})
            use["last_used"] = now
            use["requests"] += 1
            self.dirty = True

    def end(self, tid, sid):
        with self.lock:
            if self.rows.pop((tid, sid), None) is not None:
                self.dirty = True
                audit("session_end", token=tid, session=sid)

    def flush(self, force=False):
        tokens = READER.tokens()
        now = time.time()
        with self.lock:
            for key, row in list(self.rows.items()):
                tok = tokens.get(row["token"])
                if tok is None or cclib.state(tok, now) != "active" or now - row["last_seen"] > SESSION_IDLE_DROP_S:
                    del self.rows[key]
                    self.dirty = True
            if len(self.rows) > SESSIONS_MAX:
                for key in sorted(self.rows, key=lambda k: self.rows[k]["last_seen"])[: len(self.rows) - SESSIONS_MAX]:
                    del self.rows[key]
            for tid in [t for t in self.usage if t not in tokens]:
                del self.usage[tid]
                self.dirty = True
            if not (self.dirty or force):
                return
            self.dirty = False
            data = {"written": now, "started": self.started, "sessions": list(self.rows.values()), "tokens": self.usage}
        tmp = self.path + ".new"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except OSError as e:
            print("cannot write %s: %s" % (self.path, e), flush=True)


def flusher():
    while True:
        time.sleep(1)
        try:
            SESSIONS.flush()
        except Exception as e:  # the gateway must not die of its bookkeeping
            print("flush: %s: %s" % (type(e).__name__, e), flush=True)


def describe(msg):
    """(method, tool, detail) of one JSON-RPC message, for the audit log and the session list."""
    if not isinstance(msg, dict):
        return None, None, None
    method = msg.get("method")
    tool = detail = None
    if method == "tools/call":
        p = msg.get("params") or {}
        tool = str(p.get("name"))[:64]
        a = p.get("arguments") or {}
        if CFG["audit_commands"] and isinstance(a, dict):
            for k in ("command", "path", "id"):
                if k in a:
                    detail = str(a[k])[:AUDIT_DETAIL_MAX]
                    break
    return (str(method)[:64] if method else None), tool, detail


class WorkerConnection(http.client.HTTPConnection):
    """HTTP to a worker's Unix socket."""

    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self.unix_path)
        except OSError:
            s.close()
            raise
        self.sock = s


def forward(tid, body):
    """POST the request to the token's worker: (status, content type, body)."""
    c = WorkerConnection(os.path.join(CFG["sockets"], tid + ".sock"), WORKER_TIMEOUT_S)
    try:
        c.request("POST", "/mcp", body=body, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read(REPLY_MAX + 1)
        if len(data) > REPLY_MAX:
            raise http.client.HTTPException("reply too large")
        return r.status, r.getheader("Content-Type"), data
    finally:
        c.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "remote-compute-gateway"
    sys_version = ""
    # An idle keep-alive connection is dropped after this long.
    timeout = 300

    def log_message(self, fmt, *args):
        pass

    def reply(self, code, body=None, raw=None, ctype="application/json", headers=()):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        self.send_response(code)
        if data:
            self.send_header("Content-Type", ctype or "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def client_ip(self):
        # On the Unix socket there is no peer address: the peer is the tunnel.
        peer = self.client_address[0] if isinstance(self.client_address, tuple) else "local"
        # The relay appends the address it saw to this header: the last one is the client's.
        fwd = self.headers.get("X-Forwarded-For")
        if fwd and peer in ("local", "127.0.0.1", "::1"):
            last = fwd.split(",")[-1].strip()
            if IP_RE.match(last):
                return last
        return peer

    def token(self):
        """(token record, None), or (None, why not)."""
        got = self.headers.get("Authorization", "")
        if not got.startswith("Bearer "):
            return None, "missing"
        raw = got[7:].strip()
        tid = cclib.token_id(raw)
        if tid is None:
            return None, "malformed"
        tok = READER.tokens().get(tid)
        if tok is None or not hmac.compare_digest(cclib.digest(raw), str(tok.get("sha256"))):
            return None, "unknown"
        st = cclib.state(tok)
        return (tok, None) if st == "active" else (None, st + ":" + tid)

    def refuse(self, why):
        # The body is not read, so the connection cannot be used again.
        self.close_connection = True
        if AUTH_FAIL_LOG.ok():
            audit("auth_fail", why=why, ip=self.client_ip(), path=self.path[:80])
        # Only someone who holds the token learns that it ran out, so that an agent can say what to ask for.
        kind = why.split(":")[0]
        if kind in ("expired", "revoked"):
            text = "this token has %s; ask the operator for a new one (mcpctl create)" % (
                "expired" if kind == "expired" else "been revoked"
            )
        else:
            text = "bad or missing bearer token"
        self.reply(401, {"error": text}, headers=[("WWW-Authenticate", "Bearer")])

    def session_id(self, ip):
        sid = self.headers.get("Mcp-Session-Id", "")
        return sid if SID_RE.match(sid) and not sid.startswith("-") else "-" + ip

    def do_GET(self):
        if self.path.split("?")[0].rstrip("/") == "/health":
            return self.reply(200, {"ok": True})
        # No server-initiated stream: every reply comes back on its POST.
        self.reply(405, {"error": "POST JSON-RPC to /mcp"}, headers=[("Allow", "POST, DELETE")])

    def do_DELETE(self):
        tok, why = self.token()
        if tok is None:
            return self.refuse(why)
        SESSIONS.end(tok["id"], self.session_id(self.client_ip()))
        self.reply(200, {})

    def do_POST(self):
        if self.path.split("?")[0].rstrip("/") != "/mcp":
            self.close_connection = True
            return self.reply(404, {"error": "not found"})
        tok, why = self.token()
        if tok is None:
            return self.refuse(why)
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.close_connection = True
            return self.reply(411, {"error": "Content-Length is required"})
        if n < 0 or n > BODY_MAX:
            self.close_connection = True
            return self.reply(413, {"error": "the request is larger than %d bytes; send files in chunks" % BODY_MAX})
        body = self.rfile.read(n)
        try:
            msg = json.loads(body or b"null")
        except ValueError as e:
            return self.reply(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}})

        msgs = msg if isinstance(msg, list) else [msg]
        seen = [describe(m) for m in msgs]
        ip = self.client_ip()
        client = None
        starts = any(m == "initialize" for m, _, _ in seen)
        if starts:
            sid = secrets.token_urlsafe(18).lstrip("-") or "s"
            for m in msgs:
                if isinstance(m, dict) and m.get("method") == "initialize":
                    info = (m.get("params") or {}).get("clientInfo") or {}
                    if isinstance(info, dict):
                        client = ("%s %s" % (info.get("name", "?"), info.get("version", ""))).strip()[:80]
        else:
            sid = self.session_id(ip)
        SESSIONS.touch(tok, sid, ip, self.headers.get("User-Agent", "")[:120], client, seen)
        for method, tool, detail in seen:
            if method and method != "ping" and not method.startswith("notifications/"):
                audit("call", token=tok["id"], label=tok.get("label"), session=sid, ip=ip, method=method, tool=tool,
                      detail=detail)  # fmt: skip

        if not INFLIGHT.enter(tok["id"]):
            return self.reply(429, raw=self.rpc_error(msg, "this token already has %d requests waiting" % INFLIGHT_MAX))
        try:
            status, ctype, data = forward(tok["id"], body)
        except (FileNotFoundError, ConnectionRefusedError):
            status, ctype, data = 503, None, self.rpc_error(msg, "this token's worker is not running; try again shortly")
        except socket.timeout:
            status, ctype, data = 504, None, self.rpc_error(msg, "the worker did not answer in %d s" % WORKER_TIMEOUT_S)
        except (OSError, http.client.HTTPException) as e:
            status, ctype, data = 502, None, self.rpc_error(msg, "the worker failed: %s" % (e or type(e).__name__))
        finally:
            INFLIGHT.leave(tok["id"])
        if status >= 500:
            audit("worker_error", token=tok["id"], status=status)
        headers = [("Mcp-Session-Id", sid)] if starts and status == 200 else []
        self.reply(status, raw=data, ctype=ctype, headers=headers)

    @staticmethod
    def rpc_error(msg, text):
        mid = msg.get("id") if isinstance(msg, dict) else None
        return json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": text}}).encode()


class Inherited(ThreadingHTTPServer):
    """A listener on a socket systemd opened and keeps open, whatever happens to this process."""

    daemon_threads = True

    def __init__(self, sock):
        self.address_family = sock.family
        super().__init__("" if sock.family == socket.AF_UNIX else ("", 0), Handler, bind_and_activate=False)
        self.socket.close()
        self.socket = sock
        self.server_name, self.server_port = "localhost", 0


def listeners(from_systemd):
    """[(server, where it listens)]"""
    if from_systemd:
        n = int(os.environ.get("LISTEN_FDS", "0")) if os.environ.get("LISTEN_PID") == str(os.getpid()) else 0
        if n < 1:
            sys.exit("--systemd-socket: systemd passed no socket in (this is started with a .socket unit)")
        socks = [socket.socket(fileno=3 + i) for i in range(n)]
        return [(Inherited(s), str(s.getsockname())) for s in socks]
    if not CFG["port"]:
        sys.exit("nowhere to listen: pass --port, or --systemd-socket under a .socket unit")
    ThreadingHTTPServer.request_queue_size = 128
    srv = ThreadingHTTPServer((CFG["host"], CFG["port"]), Handler)
    srv.daemon_threads = True
    return [(srv, "http://%s:%d/mcp" % (CFG["host"], CFG["port"]))]


def main():
    global READER, SESSIONS
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", help="default %s" % cclib.CONFIG)
    ap.add_argument("--systemd-socket", action="store_true", help="listen on the sockets systemd passed in")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--tokens", help="the token store")
    ap.add_argument("--sockets", help="the directory of the workers' sockets, <token id>.sock")
    ap.add_argument("--state", help="where the session list is written")
    a = ap.parse_args()
    CFG.update(cclib.load_config(a.config))
    CFG.update((k, v) for k, v in vars(a).items() if k not in ("config", "systemd_socket") and v is not None)

    READER = cclib.Reader(CFG["tokens"])
    SESSIONS = Sessions(CFG["state"])
    threading.Thread(target=flusher, daemon=True).start()
    servers = listeners(a.systemd_socket)

    def stop(*_):
        SESSIONS.flush(force=True)
        os._exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print("remote-compute gateway on %s, tokens from %s" % (", ".join(w for _, w in servers), CFG["tokens"]), flush=True)
    for srv, _ in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers[0][0].serve_forever()


if __name__ == "__main__":
    sys.exit(main())
