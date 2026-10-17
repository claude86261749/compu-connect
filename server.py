#!/usr/bin/env python3
"""Remote Compute: a small MCP server for this machine, so a Claude Code session that can only reach the web over
HTTPS can work on it: run commands, start long jobs and read their logs, and move files both ways.

Python 3.7+, standard library only. It speaks MCP's streamable HTTP transport (JSON-RPC over POST, plain JSON
replies) on one endpoint, /mcp, and listens on 127.0.0.1: put a tunnel in front of it (start.sh uses a Cloudflare
quick tunnel). Every request needs `Authorization: Bearer <token>`.

    python3 server.py --port 8765 --token-file ~/.remote-compute/token

Anyone holding the token has a root shell on this machine: keep it private, stop the server when done.
"""

import argparse
import base64
import hmac
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
HOME = Path(os.environ.get("REMOTE_COMPUTE_HOME", Path.home() / ".remote-compute"))
JOBS = HOME / "jobs"
# A Cloudflare quick tunnel drops a request after about 100 s, so a command that runs longer must be a job.
RUN_MAX_S = 90
OUT_MAX = 64 * 1024
READ_MAX = 8 * 1024 * 1024

TOKEN = ""
DEFAULT_CWD = str(Path.home())


def tail(b, n=OUT_MAX):
    s = b.decode("utf-8", "replace") if isinstance(b, bytes) else b
    if len(s) <= n:
        return s
    return "[... %d characters cut ...]\n" % (len(s) - n) + s[-n:]


def path_of(p, cwd=None):
    q = Path(os.path.expanduser(p))
    if not q.is_absolute():
        q = Path(cwd or DEFAULT_CWD) / q
    return q


# ---------- tools ----------


def t_run(a):
    cmd = a["command"]
    timeout = min(float(a.get("timeout", 60)), RUN_MAX_S)
    cwd = str(path_of(a.get("cwd", DEFAULT_CWD)))
    t0 = time.time()
    p = subprocess.Popen(
        ["bash", "-lc", cmd],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = p.communicate(timeout=timeout)
        code = p.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
        code = None
        timed_out = True
    head = "exit %s in %.1f s" % ("timeout (killed)" if timed_out else code, time.time() - t0)
    parts = [head]
    if out:
        parts.append("--- stdout ---\n" + tail(out))
    if err:
        parts.append("--- stderr ---\n" + tail(err))
    return "\n".join(parts), bool(timed_out or code != 0)


def job_meta(jid):
    f = JOBS / jid / "meta.json"
    if not f.exists():
        raise ValueError("no job %s" % jid)
    return json.loads(f.read_text())


def job_state(jid):
    m = job_meta(jid)
    ex = JOBS / jid / "exit"
    p = _procs.get(m["pid"])
    if ex.exists():
        m["state"] = "exited %s" % ex.read_text().strip()
    elif p is not None and p.poll() is not None:
        # Killed before it could write its exit code.
        m["state"] = "killed by signal %d" % -p.returncode if p.returncode < 0 else "exited %d" % p.returncode
    else:
        try:
            os.kill(m["pid"], 0)
            m["state"] = "running"
        except ProcessLookupError:
            m["state"] = "gone (no exit code: killed, or the machine restarted)"
        except PermissionError:
            m["state"] = "running"
    m["elapsed_s"] = round(time.time() - m["started"])
    return m


# Jobs started by this run of the server, by pid: polling them reaps them, and says how a killed job ended.
_procs = {}


def t_spawn(a):
    cmd = a["command"]
    cwd = str(path_of(a.get("cwd", DEFAULT_CWD)))
    name = "".join(c if c.isalnum() or c in "-_" else "-" for c in a.get("name", "job"))[:32]
    jid = "%s-%s" % (time.strftime("%m%d-%H%M%S"), name)
    d = JOBS / jid
    d.mkdir(parents=True, exist_ok=False)
    # The exit code is written by the job itself, so it survives a restart of this server.
    wrapped = "( %s ) ; echo $? > %s" % (cmd, json.dumps(str(d / "exit")))
    log = open(d / "log", "ab")
    p = subprocess.Popen(
        ["bash", "-lc", wrapped],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    _procs[p.pid] = p
    (d / "meta.json").write_text(json.dumps({"id": jid, "command": cmd, "cwd": cwd, "pid": p.pid, "started": time.time()}))
    return "started job %s (pid %d); log at %s" % (jid, p.pid, d / "log"), False


def t_job(a):
    jid = a["id"]
    m = job_state(jid)
    n = int(a.get("tail", 8000))
    log = JOBS / jid / "log"
    size = log.stat().st_size if log.exists() else 0
    with open(log, "rb") as f:
        f.seek(max(0, size - n))
        text = f.read().decode("utf-8", "replace")
    return "%s · %s · %d s · log %d bytes\n$ %s\n--- last %d bytes ---\n%s" % (
        jid,
        m["state"],
        m["elapsed_s"],
        size,
        m["command"],
        min(n, size),
        text,
    ), False


def t_jobs(a):
    if not JOBS.exists():
        return "no jobs", False
    rows = []
    for d in sorted(JOBS.iterdir())[-int(a.get("limit", 30)) :]:
        try:
            m = job_state(d.name)
            rows.append("%-36s %-20s %7d s  %s" % (m["id"], m["state"][:20], m["elapsed_s"], m["command"][:80]))
        except Exception as e:  # a half-written job directory
            rows.append("%s: %s" % (d.name, e))
    return "\n".join(rows) or "no jobs", False


def t_kill(a):
    m = job_meta(a["id"])
    sig = getattr(signal, "SIG" + a.get("signal", "TERM").upper().replace("SIG", ""))
    try:
        os.killpg(m["pid"], sig)
    except ProcessLookupError:
        return "job %s is not running" % a["id"], False
    return "sent %s to job %s" % (sig.name, a["id"]), False


def t_read(a):
    p = path_of(a["path"])
    off = int(a.get("offset", 0))
    n = min(int(a.get("length", READ_MAX)), READ_MAX)
    size = p.stat().st_size
    with open(p, "rb") as f:
        f.seek(off)
        b = f.read(n)
    if a.get("encoding", "text") == "base64":
        body = base64.b64encode(b).decode()
    else:
        body = b.decode("utf-8", "replace")
    return json.dumps({"path": str(p), "size": size, "offset": off, "length": len(b), "data": body}), False


def t_write(a):
    p = path_of(a["path"])
    if a.get("mkdirs", True):
        p.parent.mkdir(parents=True, exist_ok=True)
    data = a.get("content", "")
    b = base64.b64decode(data) if a.get("encoding", "text") == "base64" else data.encode()
    with open(p, "ab" if a.get("append") else "wb") as f:
        f.write(b)
    if a.get("mode"):
        os.chmod(p, int(str(a["mode"]), 8))
    return "wrote %d bytes to %s (now %d bytes)" % (len(b), p, p.stat().st_size), False


def t_ls(a):
    p = path_of(a.get("path", "."))
    rows = []
    for e in sorted(p.iterdir()):
        try:
            st = e.stat()
            kind = "d" if e.is_dir() else "f"
            rows.append("%s %12d %s %s" % (kind, st.st_size, time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)), e.name))
        except OSError as x:
            rows.append("? %s (%s)" % (e.name, x))
    return "%s\n%s" % (p, "\n".join(rows)), False


def t_info(a):
    parts = []
    for cmd in [
        "nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv,noheader",
        "nproc",
        "free -g | head -2",
        "df -h %s | tail -1" % DEFAULT_CWD,
        "uptime",
    ]:
        if shutil.which(cmd.split()[0]):
            r = subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True, timeout=20)
            parts.append("$ %s\n%s" % (cmd, (r.stdout + r.stderr).strip()))
    return "\n".join(parts), False


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}


def schema(req, **props):
    return {"type": "object", "properties": props, "required": req}


TOOLS = {
    "run": (
        t_run,
        "Run a bash command (login shell) and wait for it: exit code, stdout and stderr (the last 64 KB of each). "
        "At most %d s, then it is killed: use spawn for anything longer." % RUN_MAX_S,
        schema(["command"], command=S, cwd=S, timeout={"type": "number", "description": "seconds, at most %d" % RUN_MAX_S}),
    ),
    "spawn": (
        t_spawn,
        "Start a bash command in the background, detached from this server, its output to a log. Returns a job id "
        "for job, jobs and kill.",
        schema(["command"], command=S, cwd=S, name={"type": "string", "description": "a short label for the job id"}),
    ),
    "job": (
        t_job,
        "A job's state (running, exited N, gone) and the end of its log.",
        schema(["id"], id=S, tail={"type": "integer", "description": "bytes of log to return, default 8000"}),
    ),
    "jobs": (t_jobs, "The most recent jobs and their states.", schema([], limit=I)),
    "kill": (
        t_kill,
        "Send a signal (default TERM) to a job's whole process group.",
        schema(["id"], id=S, signal={"type": "string", "description": "TERM, INT, KILL…"}),
    ),
    "read_file": (
        t_read,
        "Read a file, or part of it (at most 8 MB per call), as text or base64. Returns JSON with size, offset, "
        "length and data.",
        schema(["path"], path=S, offset=I, length=I, encoding={"type": "string", "enum": ["text", "base64"]}),
    ),
    "write_file": (
        t_write,
        "Write or append to a file, from text or base64; parent directories are made. Send large files in chunks "
        "with append.",
        schema(
            ["path", "content"],
            path=S,
            content=S,
            encoding={"type": "string", "enum": ["text", "base64"]},
            append=B,
            mkdirs=B,
            mode={"type": "string", "description": "octal, e.g. 755"},
        ),
    ),
    "list_dir": (t_ls, "List a directory: kind, size, modified time, name.", schema([], path=S)),
    "info": (t_info, "The GPUs (if any), CPU count, memory, disk and load.", schema([])),
}


# ---------- MCP over HTTP ----------


def handle(msg):
    """One JSON-RPC message; None for a notification."""
    mid = msg.get("id")
    method = msg.get("method", "")
    if mid is None:
        return None
    try:
        if method == "initialize":
            want = msg.get("params", {}).get("protocolVersion")
            result = {
                "protocolVersion": want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "remote-compute", "version": "1.0.0"},
                "instructions": "A shell on the Remote Compute machine. run waits at most %d s; spawn starts long jobs, job "
                "reads their logs. Files move with read_file and write_file (base64 for binary, in chunks)." % RUN_MAX_S,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": k, "description": d, "inputSchema": s} for k, (_, d, s) in TOOLS.items()]}
        elif method == "tools/call":
            p = msg.get("params", {})
            name = p.get("name")
            if name not in TOOLS:
                raise KeyError("unknown tool %s" % name)
            try:
                text, is_err = TOOLS[name][0](p.get("arguments") or {})
            except Exception as e:
                text, is_err = "%s: %s" % (type(e).__name__, e), True
            result = {"content": [{"type": "text", "text": text}], "isError": is_err}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "no method %s" % method}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as e:
        traceback.print_exc()
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "remote-compute-mcp"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def reply(self, code, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        got = self.headers.get("Authorization", "")
        return hmac.compare_digest(got.encode(), ("Bearer " + TOKEN).encode())

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            return self.reply(200, {"ok": True})
        # No server-initiated stream: every reply comes back on its POST.
        self.reply(405, {"error": "POST JSON-RPC to /mcp"})

    def do_DELETE(self):
        self.reply(200 if self.authorized() else 401, {})

    def do_POST(self):
        if self.path.split("?")[0].rstrip("/") != "/mcp":
            return self.reply(404, {"error": "not found"})
        if not self.authorized():
            time.sleep(1)
            return self.reply(401, {"error": "bad or missing bearer token"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            msg = json.loads(self.rfile.read(n) or b"null")
        except Exception as e:
            return self.reply(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}})
        if isinstance(msg, list):
            out = [r for r in (handle(m) for m in msg) if r is not None]
            return self.reply(200, out) if out else self.reply(202)
        r = handle(msg) if isinstance(msg, dict) else None
        return self.reply(200, r) if r is not None else self.reply(202)


def reap():
    while True:
        for p in list(_procs.values()):
            p.poll()
        time.sleep(5)


def main():
    global TOKEN, DEFAULT_CWD
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token-file", default=str(HOME / "token"))
    ap.add_argument("--cwd", default=DEFAULT_CWD, help="where commands run when no cwd is given")
    a = ap.parse_args()
    TOKEN = os.environ.get("REMOTE_COMPUTE_SERVER_TOKEN") or Path(a.token_file).expanduser().read_text().strip()
    if len(TOKEN) < 24:
        sys.exit("the token must be at least 24 characters")
    DEFAULT_CWD = str(Path(a.cwd).expanduser())
    JOBS.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=reap, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print("remote-compute MCP on http://%s:%d/mcp" % (a.host, a.port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
