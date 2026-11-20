#!/usr/bin/env python3
"""mcpctl: the operator's side of the always-on Remote Compute. Run it as root on the machine. Standard library only.

    mcpctl create [--ttl 12h] [--label NAME]   mint a token and print it, once
    mcpctl tokens [--all]                      the tokens: state, time left, last use, running processes
    mcpctl sessions [--all]                    who is connected now, from where, doing what
    mcpctl revoke ID|LABEL... | --all          cut tokens off and kill everything they started
    mcpctl status                              are the gateway, the tunnel and the public address up
    mcpctl sync [--watch]                      make the running workers match the tokens (a service does this)

A token lives 12 hours unless --ttl says otherwise, and never longer than 7 days. When it expires or is revoked, its
worker is stopped, which kills every process the token started.
"""

import argparse
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import cclib

CFG = dict(cclib.DEFAULTS)
# A session that has been quiet this long is not listed without --all.
SESSION_ACTIVE_S = 15 * 60


# ---------- systemd: one socket and one service per token ----------


def systemctl(*args):
    try:
        return subprocess.run(["systemctl", *args], capture_output=True, text=True)
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", "systemctl: not found")


def unit(tid, kind):
    return "%s.%s" % (CFG["unit"].format(id=tid), kind)


def unit_re():
    return re.compile("^" + re.escape(CFG["unit"]).replace(re.escape("{id}"), "([a-z0-9]+)") + r"\.(socket|service)$")


def start_worker(tid):
    """Open the token's socket; its service starts with the first request."""
    if not CFG["systemd"]:
        return True
    r = systemctl("start", unit(tid, "socket"))
    if r.returncode != 0:
        print("mcpctl: could not start %s: %s" % (unit(tid, "socket"), r.stderr.strip()), file=sys.stderr)
    return r.returncode == 0


def stop_worker(tid):
    """Close the token's socket and stop its service: systemd kills everything in the service's control group."""
    if not CFG["systemd"]:
        return True
    r = systemctl("stop", unit(tid, "socket"), unit(tid, "service"))
    if r.returncode != 0:
        print("mcpctl: could not stop the worker of %s: %s" % (tid, r.stderr.strip()), file=sys.stderr)
    return r.returncode == 0


def loaded_workers():
    """{token id: {"socket": state, "service": state}} for every worker unit systemd has loaded."""
    out = {}
    if not CFG["systemd"]:
        return out
    r = systemctl("list-units", "--all", "--plain", "--no-legend", "--no-pager", CFG["unit"].format(id="*") + ".*")
    pat = unit_re()
    for line in r.stdout.splitlines():
        f = line.split()
        m = pat.match(f[0]) if f else None
        if m and len(f) >= 4:
            out.setdefault(m.group(1), {})[m.group(2)] = f[2]
    return out


def worker_stats(tid):
    """What the token's service is using: {"state", "tasks", "memory"}; tasks and memory are None when it is not
    running."""
    stats = {"state": "-", "tasks": None, "memory": None}
    if not CFG["systemd"]:
        return stats
    r = systemctl("show", unit(tid, "service"), "-p", "ActiveState", "-p", "TasksCurrent", "-p", "MemoryCurrent")
    got = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    stats["state"] = got.get("ActiveState", "-")
    if stats["state"] in ("active", "deactivating"):
        for key, name in (("tasks", "TasksCurrent"), ("memory", "MemoryCurrent")):
            v = got.get(name, "")
            # systemd prints 2^64-1 or "[not set]" for a figure it does not have.
            stats[key] = int(v) if v.isdigit() and int(v) < 2**63 else None
    return stats


# ---------- showing things ----------


def ago(ts, now):
    return "never" if not ts else cclib.fmt_span(now - ts) + " ago"


def size(n):
    if n is None:
        return "-"
    for unit_ in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit_ == "T":
            return ("%d%s" if unit_ == "B" else "%.1f%s") % (n, unit_)
        n /= 1024.0


def plain(value, width=60):
    """A cell as text that is safe to print: what a client calls itself must not reach the operator's terminal as
    escape sequences."""
    s = "".join(c if c.isprintable() else "?" for c in str(value))
    return s if len(s) <= width else s[: width - 1] + "…"


def table(head, rows):
    if not rows:
        return
    rows = [[plain(c) for c in r] for r in rows]
    widths = [max(len(str(r[i])) for r in [head] + rows) for i in range(len(head))]
    for r in [head] + rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())


def when(ts):
    return time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(ts))


def read_state():
    """What the gateway last wrote about sessions. The gateway is a lesser account than the one reading this, so
    rows that are not what they should be are dropped, not believed."""
    empty = {"sessions": [], "tokens": {}}
    try:
        with open(CFG["state"]) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return empty
    if not isinstance(state, dict):
        return empty
    number = (int, float)
    sessions = [
        s
        for s in state.get("sessions") or []
        if isinstance(s, dict)
        and isinstance(s.get("token"), str)
        and isinstance(s.get("sid"), str)
        and isinstance(s.get("created"), number)
        and isinstance(s.get("last_seen"), number)
        and isinstance(s.get("calls"), int)
    ]
    usage = {k: v for k, v in (state.get("tokens") or {}).items() if isinstance(v, dict)}
    return {"sessions": sessions, "tokens": usage}


def find(tokens, ref):
    """The token an id or a label names; among tokens sharing a label, the active one."""
    if ref in tokens:
        return tokens[ref]
    named = [t for t in tokens.values() if t.get("label") == ref]
    live = [t for t in named if cclib.state(t) == "active"]
    if len(live) == 1 or (not live and len(named) == 1):
        return (live or named)[0]
    return None


# ---------- commands ----------


def cmd_create(a):
    ceiling = cclib.max_ttl(CFG)
    try:
        ttl = cclib.parse_span(a.ttl or CFG["default_ttl"])
    except ValueError as e:
        sys.exit("mcpctl: %s" % e)
    if ttl > ceiling:
        sys.exit("mcpctl: a token can live %s at most; %s is too long" % (cclib.fmt_span(ceiling), a.ttl))
    if ttl < cclib.MIN_TTL:
        sys.exit("mcpctl: a token lives at least %s" % cclib.fmt_span(cclib.MIN_TTL))
    if a.label and not cclib.LABEL_RE.match(a.label):
        sys.exit("mcpctl: a label is up to 40 letters, digits, dots, dashes and underscores")
    now = time.time()
    with STORE.edit() as data:
        tokens = data["tokens"]
        if a.label and any(t.get("label") == a.label and cclib.state(t, now) == "active" for t in tokens.values()):
            sys.exit("mcpctl: an active token is already labelled %r" % a.label)
        tid, token = cclib.new_token(tokens)
        tokens[tid] = {
            "id": tid,
            "label": a.label,
            "sha256": cclib.digest(token),
            "created": int(now),
            "expires": int(now) + ttl,
            "revoked": None,
        }
    started = start_worker(tid)
    url = CFG["public_url"] or ("http://%s:%d/mcp" % (CFG["host"], CFG["port"]) if CFG["port"] else None)
    if a.json:
        print(json.dumps({"id": tid, "label": a.label, "token": token, "url": url, "expires": int(now) + ttl}))
    elif a.quiet:
        print(token)
    else:
        name = tid + (" (%s)" % a.label if a.label else "")
        print("Token %s is valid for %s, until %s." % (name, cclib.fmt_span(ttl), when(now + ttl)))
        print("\nGive Claude these two lines. The token is not shown again and is not stored here:\n")
        print("  REMOTE_COMPUTE_URL=%s" % (url or "(none yet: no relay is configured, so nothing outside can reach the gateway)"))
        print("  REMOTE_COMPUTE_TOKEN=%s" % token)
        if url and not CFG["public_url"]:
            print("\nNo relay is configured, so that URL only works on this machine.")
        print("\nWhoever holds it has a shell here as %s until then. Cut it off with: mcpctl revoke %s" % (CFG["account"], tid))
    return 0 if started else 1


def cmd_tokens(a):
    now = time.time()
    tokens = STORE.read()["tokens"]
    state = read_state()
    rows = []
    for t in sorted(tokens.values(), key=lambda t: t["created"]):
        st = cclib.state(t, now)
        if st != "active" and not a.all:
            continue
        use = state["tokens"].get(t["id"], {})
        stats = worker_stats(t["id"]) if st == "active" else {"state": "-", "tasks": None, "memory": None}
        t = dict(t, state=st, last_used=use.get("last_used"), requests=use.get("requests", 0),
                 worker=stats["state"], tasks=stats["tasks"], memory=stats["memory"])  # fmt: skip
        t["sessions"] = sum(1 for s in state["sessions"] if s["token"] == t["id"])
        rows.append(t)
    if a.json:
        print(json.dumps([{k: v for k, v in t.items() if k != "sha256"} for t in rows], indent=1))
        return 0
    if not rows:
        print("no tokens" if a.all else "no active tokens (mcpctl create makes one; --all lists the dead ones)")
        return 0
    table(
        ["ID", "LABEL", "STATE", "CREATED", "LEFT", "LAST USED", "SESSIONS", "PROCS", "MEMORY"],
        [
            [
                t["id"],
                t.get("label") or "-",
                t["state"],
                ago(t["created"], now),
                cclib.fmt_span(t["expires"] - now) if t["state"] == "active" else "-",
                ago(t["last_used"], now),
                t["sessions"],
                "-" if t["tasks"] is None else t["tasks"],
                size(t["memory"]),
            ]
            for t in rows
        ],
    )
    return 0


def cmd_sessions(a):
    now = time.time()
    tokens = STORE.read()["tokens"]
    live = {tid: t for tid, t in tokens.items() if cclib.state(t, now) == "active"}
    rows = [s for s in read_state()["sessions"] if s["token"] in live]
    if not a.all:
        rows = [s for s in rows if now - s["last_seen"] <= SESSION_ACTIVE_S]
    rows.sort(key=lambda s: -s["last_seen"])
    workers = {tid: worker_stats(tid) for tid in live}
    busy = {tid: w for tid, w in workers.items() if w["tasks"]}
    if a.json:
        print(json.dumps({"sessions": rows, "workers": busy}, indent=1))
        return 0
    if rows:
        table(
            ["TOKEN", "LABEL", "SESSION", "CLIENT", "FROM", "STARTED", "LAST SEEN", "CALLS", "LAST TOOL", "LEFT"],
            [
                [
                    s["token"],
                    s.get("label") or "-",
                    "(no id)" if s.get("implicit") else s["sid"][:8],
                    s.get("client") or s.get("agent") or "-",
                    s.get("ip") or "-",
                    ago(s["created"], now),
                    ago(s["last_seen"], now),
                    s["calls"],
                    s.get("last_tool") or "-",
                    cclib.fmt_span(live[s["token"]]["expires"] - now),
                ]
                for s in rows
            ],
        )
    else:
        print("no sessions in the last %s" % cclib.fmt_span(SESSION_ACTIVE_S) if not a.all else "no sessions")
    # A token whose client went away can still have jobs running.
    if busy:
        print("\nRunning as %s:" % CFG["account"])
        for tid, w in sorted(busy.items()):
            label = live[tid].get("label")
            print("  %s%s: %d processes and threads, %s of memory, %s left" % (
                tid, " (%s)" % label if label else "", w["tasks"], size(w["memory"]),
                cclib.fmt_span(live[tid]["expires"] - now)))  # fmt: skip
    return 0


def cmd_revoke(a):
    if bool(a.tokens) == bool(a.all):
        sys.exit("mcpctl: name the tokens to revoke, by id or label, or pass --all")
    now = time.time()
    done, missing = [], []
    with STORE.edit() as data:
        tokens = data["tokens"]
        picked = [t for t in tokens.values() if cclib.state(t, now) == "active"] if a.all else []
        for ref in a.tokens:
            t = find(tokens, ref)
            if t is None:
                missing.append(ref)
            elif t not in picked:
                picked.append(t)
        for t in picked:
            was = cclib.state(t, now)
            if was == "active":
                t["revoked"] = int(now)
            done.append((t, was))
    sessions = read_state()["sessions"]
    ok = True
    for t, was in done:
        stats = worker_stats(t["id"])
        # Stop it even if the token had run out already: nothing of it may be left running.
        ok = stop_worker(t["id"]) and ok
        name = t["id"] + (" (%s)" % t["label"] if t.get("label") else "")
        if was != "active":
            print("%s was %s already; its worker is stopped" % (name, was))
        else:
            n = sum(1 for s in sessions if s["token"] == t["id"])
            killed = ", %d processes and threads killed" % stats["tasks"] if stats["tasks"] else ""
            print("revoked %s: %d session%s cut off%s" % (name, n, "" if n == 1 else "s", killed))
    for ref in missing:
        print("mcpctl: no token %r" % ref, file=sys.stderr)
    if a.all and not done:
        print("no active tokens")
    return 0 if ok and not missing else 1


def sync(verbose):
    """Make systemd match the store: a socket for every active token, nothing running for any other."""
    now = time.time()
    tokens = STORE.read()["tokens"]
    live = {tid for tid, t in tokens.items() if cclib.state(t, now) == "active"}
    ok = True
    loaded = loaded_workers()
    for tid in sorted(live):
        if loaded.get(tid, {}).get("socket") != "active":
            if verbose:
                print("starting the worker socket of %s" % tid, flush=True)
            ok = start_worker(tid) and ok
    for tid, units in sorted(loaded.items()):
        if tid not in live and any(s in ("active", "activating", "deactivating", "reloading") for s in units.values()):
            why = cclib.state(tokens[tid], now) if tid in tokens else "unknown"
            print("stopping the worker of %s: its token is %s" % (tid, why), flush=True)
            ok = stop_worker(tid) and ok
    stale = [tid for tid, t in tokens.items() if tid not in live and now - (t.get("revoked") or t["expires"]) > cclib.KEEP_DEAD_S]
    if stale:
        with STORE.edit() as data:
            for tid in stale:
                data["tokens"].pop(tid, None)
    return ok, live


def cmd_sync(a):
    if not a.watch:
        return 0 if sync(not a.quiet)[0] else 1
    # Look every second, which costs one stat; act when the set of active tokens changes (a token was made,
    # revoked, or ran out) and every few minutes regardless.
    reader = cclib.Reader(CFG["tokens"])
    last, again = None, 0
    while True:
        now = time.time()
        live = {tid for tid, t in reader.tokens().items() if cclib.state(t, now) == "active"}
        if live != last or now >= again:
            try:
                ok, last = sync(True)
                again = now + (300 if ok else 15)
            except Exception as e:  # keep watching: this loop is what enforces expiry
                print("sync failed: %s: %s" % (type(e).__name__, e), flush=True)
                again = now + 15
        time.sleep(1)


def probe(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return "up (HTTP %d)" % r.status
    except urllib.error.HTTPError as e:
        return "answers HTTP %d" % e.code
    except Exception as e:
        return "DOWN (%s)" % (getattr(e, "reason", None) or type(e).__name__)


def probe_socket(path):
    """The same question, asked on the gateway's Unix socket."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    try:
        s.connect(path)
        c = http.client.HTTPConnection("localhost", timeout=10)
        c.sock = s
        c.request("GET", "/health")
        status = c.getresponse().status
        return "up (HTTP %d)" % status if status == 200 else "answers HTTP %d" % status
    except Exception as e:
        return "DOWN (%s)" % (getattr(e, "strerror", None) or type(e).__name__)
    finally:
        s.close()


def cmd_status(a):
    now = time.time()
    ok = True
    if CFG["systemd"]:
        for name in CFG["services"]:
            r = systemctl("show", name, "-p", "LoadState", "-p", "ActiveState")
            got = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
            if got.get("LoadState") == "not-found":
                print("%-22s not set up" % name)
                continue
            st = got.get("ActiveState", "unknown")
            ok = ok and st == "active"
            print("%-22s %s" % (name, st))
    if CFG["gateway_socket"] and os.path.exists(CFG["gateway_socket"]):
        local = probe_socket(CFG["gateway_socket"])
    elif CFG["port"]:
        local = probe("http://%s:%d/health" % (CFG["host"], CFG["port"]))
    else:
        local = "DOWN (no socket at %s)" % CFG["gateway_socket"]
    ok = ok and local.startswith("up")
    print("%-22s %s" % ("gateway, on this host", local))
    if CFG["public_url"]:
        public = probe(re.sub(r"/mcp/?$", "", CFG["public_url"]) + "/health")
        ok = ok and public.startswith("up")
        print("%-22s %s  %s" % ("gateway, from outside", public, CFG["public_url"]))
    else:
        print("%-22s no relay is configured" % "gateway, from outside")
    tokens = STORE.read()["tokens"]
    live = [t for t in tokens.values() if cclib.state(t, now) == "active"]
    sessions = [s for s in read_state()["sessions"] if now - s["last_seen"] <= SESSION_ACTIVE_S]
    print("%d active token%s, %d session%s in the last %s" % (
        len(live), "" if len(live) == 1 else "s", len(sessions), "" if len(sessions) == 1 else "s",
        cclib.fmt_span(SESSION_ACTIVE_S)))  # fmt: skip
    return 0 if ok else 1


def main():
    global STORE
    ap = argparse.ArgumentParser(prog="mcpctl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="default %s" % cclib.CONFIG)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    s = sub.add_parser("create", help="mint a token")
    s.add_argument("--ttl", help="how long it lives, like 90m, 12h or 3d; at most 7d")
    s.add_argument("--label", help="a name for it in listings, and for revoke")
    s.add_argument("--json", action="store_true")
    s.add_argument("-q", "--quiet", action="store_true", help="print the token and nothing else")
    s = sub.add_parser("tokens", help="list the tokens")
    s.add_argument("--all", action="store_true", help="also the expired and revoked ones")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("sessions", help="list who is connected")
    s.add_argument("--all", action="store_true", help="also sessions that have been quiet for a while")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("revoke", help="cut tokens off and kill what they started")
    s.add_argument("tokens", nargs="*", metavar="ID|LABEL")
    s.add_argument("--all", action="store_true", help="every active token")
    sub.add_parser("status", help="is everything up")
    s = sub.add_parser("sync", help="make the running workers match the tokens")
    s.add_argument("--watch", action="store_true", help="keep doing it; this is what enforces expiry")
    s.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args()
    CFG.update(cclib.load_config(a.config))
    STORE = cclib.Store(CFG["tokens"])
    try:
        return {"create": cmd_create, "tokens": cmd_tokens, "sessions": cmd_sessions, "revoke": cmd_revoke,
                "status": cmd_status, "sync": cmd_sync}[a.cmd](a)  # fmt: skip
    except PermissionError as e:
        sys.exit("mcpctl: %s: run it as root" % e)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
