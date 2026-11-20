#!/usr/bin/env python3
"""What gateway.py and mcpctl share in the always-on setup: the configuration and the token store. Standard library
only.

A token is `cc_<id>_<secret>`. The id is public: it names the token in listings and logs, and the systemd unit that
runs its commands. The secret is 32 random bytes. Only the SHA-256 of the whole token is stored, so reading the store
gives nobody a token. The store is written by mcpctl (root) and only read by the gateway.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import time

CONFIG = os.environ.get("COMPU_CONNECT_CONFIG", "/etc/compu-connect/config.json")
DEFAULTS = {
    "tokens": "/var/lib/compu-connect/tokens.json",
    "state": "/run/compu-connect/state/sessions.json",
    "sockets": "/run/compu-connect/sessions",
    # The systemd unit of one token's worker, without ".socket" / ".service".
    "unit": "mcp-session@{id}",
    "services": ["mcp-gateway.service", "mcp-sync.service", "mcp-tunnel.service"],
    "systemd": True,
    # Where the gateway listens: a Unix socket that the tunnel and root may connect to, and, only if a port is
    # given, 127.0.0.1 as well, which every account on the machine can reach.
    "gateway_socket": "/run/compu-connect/gateway.sock",
    "host": "127.0.0.1",
    "port": None,
    "public_url": None,
    "default_ttl": "12h",
    "max_ttl": "7d",
    "account": "mcp-user",
    "audit_commands": True,
}

PREFIX = "cc"
DEFAULT_TTL = 12 * 3600
# The ceiling. A configuration can lower it; nothing raises it.
MAX_TTL = 7 * 86400
MIN_TTL = 60
# Expired and revoked tokens stay listed (mcpctl tokens --all) this long, then they are forgotten.
KEEP_DEAD_S = 30 * 86400

ID_ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
ID_RE = re.compile(r"^[a-z0-9]{8}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def load_config(path=None):
    cfg = dict(DEFAULTS)
    path = path or CONFIG
    try:
        with open(path) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg


def parse_span(text):
    """'12h', '90m', '2d12h', or plain seconds: how long, in seconds."""
    s = str(text).strip().lower().replace(" ", "")
    if s.isdigit():
        return int(s)
    parts = re.findall(r"(\d+)([smhdw])", s)
    if not parts or "".join(n + u for n, u in parts) != s:
        raise ValueError("not a duration: %r (write it like 12h, 90m, 2d12h or 7d)" % text)
    return sum(int(n) * _UNITS[u] for n, u in parts)


def fmt_span(seconds):
    """The two largest units of a duration: '6d 23h', '11h 59m', '45s'."""
    s = max(0, int(seconds))
    out = []
    for name, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if s >= size or (name == "s" and not out):
            out.append("%d%s" % (s // size, name))
            s %= size
        if len(out) == 2:
            break
    return " ".join(out)


def max_ttl(cfg):
    return min(MAX_TTL, parse_span(cfg.get("max_ttl", MAX_TTL)))


def new_token(taken=()):
    """(id, token): a fresh token whose id is not in `taken`."""
    while True:
        tid = "".join(secrets.choice(ID_ALPHABET) for _ in range(8))
        if tid not in taken:
            return tid, "%s_%s_%s" % (PREFIX, tid, secrets.token_urlsafe(32))


def token_id(token):
    """The id inside a token, or None if it does not look like one of ours."""
    parts = token.split("_", 2)
    if len(parts) == 3 and parts[0] == PREFIX and ID_RE.match(parts[1]) and 40 <= len(parts[2]) <= 128:
        return parts[1]
    return None


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def state(tok, now=None):
    """'active', 'expired' or 'revoked'."""
    if tok.get("revoked"):
        return "revoked"
    return "active" if (now or time.time()) < tok["expires"] else "expired"


def _parse(raw):
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("tokens"), dict):
        raise ValueError("not a token store")
    return data


class Store:
    """The token store, for mcpctl: read it, or change it under a lock and write it back whole."""

    def __init__(self, path):
        self.path = path

    def read(self):
        try:
            with open(self.path) as f:
                return _parse(f.read())
        except FileNotFoundError:
            return {"version": 1, "tokens": {}}

    @contextlib.contextmanager
    def edit(self):
        fd = os.open(self.path + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = self.read()
            yield data
            self._write(data)
        finally:
            os.close(fd)

    def _write(self, data):
        tmp = self.path + ".new"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        try:
            # Readable by the directory's group, which is the gateway's, and by nobody else.
            try:
                os.fchown(fd, -1, os.stat(os.path.dirname(self.path) or ".").st_gid)
            except OSError:
                pass
            os.fchmod(fd, 0o640)
            os.write(fd, (json.dumps(data, indent=1, sort_keys=True) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)


class Reader:
    """The token store, for the gateway: read again whenever the file changes. A store that cannot be read holds no
    tokens, so a broken store lets nobody in."""

    def __init__(self, path):
        self.path = path
        self._sig = None
        self._tokens = {}

    def tokens(self):
        try:
            st = os.stat(self.path)
            sig = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            self._sig, self._tokens = None, {}
            return self._tokens
        if sig != self._sig:
            try:
                with open(self.path) as f:
                    self._tokens = _parse(f.read())["tokens"]
            except (OSError, ValueError) as e:
                print("token store %s is unreadable (%s): no token is accepted" % (self.path, e), flush=True)
                self._tokens = {}
            self._sig = sig
        return self._tokens
