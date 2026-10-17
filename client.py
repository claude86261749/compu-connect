#!/usr/bin/env python3
"""Talk to the Remote Compute MCP server (server.py) from the command line. Standard library only.

The URL and token come from REMOTE_COMPUTE_URL and REMOTE_COMPUTE_TOKEN, or from a file of those two lines at
~/.remote-compute.env.

    client.py info
    client.py run 'nvidia-smi' [--cwd DIR] [--timeout S]
    client.py spawn 'python3 train.py' [--cwd DIR] [--name NAME]
    client.py job ID [--tail BYTES]      client.py jobs      client.py kill ID [--signal INT]
    client.py ls [PATH]
    client.py push LOCAL REMOTE          a file, or a directory (sent as a tarball, unpacked into REMOTE)
    client.py pull REMOTE LOCAL          a file
    client.py tools                      what the server offers
"""

import argparse
import base64
import io
import json
import os
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

CHUNK = 4 * 1024 * 1024


def config():
    env = {}
    f = Path.home() / ".remote-compute.env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    url = os.environ.get("REMOTE_COMPUTE_URL") or env.get("REMOTE_COMPUTE_URL")
    token = os.environ.get("REMOTE_COMPUTE_TOKEN") or env.get("REMOTE_COMPUTE_TOKEN")
    if not url or not token:
        sys.exit("set REMOTE_COMPUTE_URL and REMOTE_COMPUTE_TOKEN, or write them to ~/.remote-compute.env")
    return url, token


_id = 0


def rpc(method, params=None, timeout=120):
    global _id
    _id += 1
    url, token = config()
    body = json.dumps({"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + token,
            "MCP-Protocol-Version": "2025-06-18",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            msg = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit("HTTP %d: %s" % (e.code, e.read().decode(errors="replace")[:500]))
    if "error" in msg:
        sys.exit("error: %s" % msg["error"].get("message"))
    return msg["result"]


def call(tool, args):
    # The HTTP wait outlasts the longest run the server allows.
    r = rpc("tools/call", {"name": tool, "arguments": {k: v for k, v in args.items() if v is not None}}, 150)
    text = "".join(c.get("text", "") for c in r.get("content", []))
    return text, r.get("isError", False)


def show(tool, **args):
    text, err = call(tool, args)
    print(text)
    return 1 if err else 0


def push(local, remote):
    p = Path(local)
    if p.is_dir():
        buf = io.BytesIO()
        skip = {"node_modules", "target", ".git", "dist", ".cache", "__pycache__"}
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            t.add(p, arcname=".", filter=lambda ti: None if Path(ti.name).name in skip else ti)
        data, staged = buf.getvalue(), remote.rstrip("/") + ".upload.tgz"
    else:
        data, staged = p.read_bytes(), remote
    for i in range(0, max(len(data), 1), CHUNK):
        text, err = call(
            "write_file",
            dict(path=staged, content=base64.b64encode(data[i : i + CHUNK]).decode(), encoding="base64", append=i > 0),
        )
        if err:
            sys.exit(text)
    if p.is_dir():
        q = json.dumps(remote)
        return show("run", command="mkdir -p %s && tar -xzf %s -C %s && rm %s" % (q, json.dumps(staged), q, json.dumps(staged)))
    print("sent %d bytes to %s" % (len(data), remote))
    return 0


def pull(remote, local):
    out, off = bytearray(), 0
    while True:
        text, err = call("read_file", dict(path=remote, offset=off, length=CHUNK, encoding="base64"))
        if err:
            sys.exit(text)
        r = json.loads(text)
        out += base64.b64decode(r["data"])
        off += r["length"]
        if r["length"] == 0 or off >= r["size"]:
            break
    Path(local).parent.mkdir(parents=True, exist_ok=True)
    Path(local).write_bytes(bytes(out))
    print("got %d bytes into %s" % (len(out), local))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tools")
    sub.add_parser("info")
    sub.add_parser("jobs")
    s = sub.add_parser("run")
    s.add_argument("command")
    s.add_argument("--cwd")
    s.add_argument("--timeout", type=float, default=85)
    s = sub.add_parser("spawn")
    s.add_argument("command")
    s.add_argument("--cwd")
    s.add_argument("--name")
    s = sub.add_parser("job")
    s.add_argument("id")
    s.add_argument("--tail", type=int)
    s = sub.add_parser("kill")
    s.add_argument("id")
    s.add_argument("--signal")
    s = sub.add_parser("ls")
    s.add_argument("path", nargs="?", default=".")
    s = sub.add_parser("push")
    s.add_argument("local")
    s.add_argument("remote")
    s = sub.add_parser("pull")
    s.add_argument("remote")
    s.add_argument("local")
    a = ap.parse_args()

    if a.cmd == "tools":
        for t in rpc("tools/list")["tools"]:
            print("%-11s %s" % (t["name"], t["description"]))
        return 0
    if a.cmd == "info":
        return show("info")
    if a.cmd == "jobs":
        return show("jobs")
    if a.cmd == "run":
        return show("run", command=a.command, cwd=a.cwd, timeout=a.timeout)
    if a.cmd == "spawn":
        return show("spawn", command=a.command, cwd=a.cwd, name=a.name)
    if a.cmd == "job":
        return show("job", id=a.id, tail=a.tail)
    if a.cmd == "kill":
        return show("kill", id=a.id, signal=a.signal)
    if a.cmd == "ls":
        return show("list_dir", path=a.path)
    if a.cmd == "push":
        return push(a.local, a.remote)
    if a.cmd == "pull":
        return pull(a.remote, a.local)


if __name__ == "__main__":
    sys.exit(main())
