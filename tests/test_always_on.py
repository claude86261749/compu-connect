#!/usr/bin/env python3
"""The gateway, a worker and mcpctl together, without systemd: tokens, expiry, revocation, sessions.

    python3 -m unittest discover -s tests -v

What only systemd can show (the sandbox, the account, killing a token's processes) is in nix/test.nix.
"""

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import cclib  # noqa: E402

FAKE_SYSTEMCTL = """#!/bin/sh
# Records what mcpctl asks of systemd, and answers list-units from a file.
echo "$*" >> "$FAKE_SYSTEMD/calls"
case "$1" in
  list-units) cat "$FAKE_SYSTEMD/units" 2>/dev/null ;;
  show) printf 'ActiveState=active\\nTasksCurrent=3\\nMemoryCurrent=1048576\\nLoadState=loaded\\n' ;;
esac
exit 0
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(what, check, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting for " + what)


class AlwaysOn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="cc-test-")
        cls.port = free_port()
        cls.cfg = {
            "tokens": cls.dir + "/tokens.json",
            "state": cls.dir + "/sessions.json",
            "sockets": cls.dir + "/sockets",
            "systemd": False,
            "gateway_socket": None,
            "port": cls.port,
        }
        os.mkdir(cls.cfg["sockets"])
        cls.config = cls.dir + "/config.json"
        with open(cls.config, "w") as f:
            json.dump(cls.cfg, f)
        cls.procs = []
        cls.gateway = cls.spawn([sys.executable, ROOT + "/gateway.py", "--config", cls.config])
        wait_for("the gateway", lambda: cls.http("GET", "/health")[0] == 200)

    @classmethod
    def tearDownClass(cls):
        for p in cls.procs:
            p.terminate()
        for p in cls.procs:
            p.wait(5)
        shutil.rmtree(cls.dir)

    @classmethod
    def spawn(cls, argv, **env):
        p = subprocess.Popen(argv, env=dict(os.environ, **env), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.procs.append(p)
        return p

    @classmethod
    def http(cls, method, path, token=None, body=None, sid=None, forwarded=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if sid:
            headers["Mcp-Session-Id"] = sid
        if forwarded:
            headers["X-Forwarded-For"] = forwarded
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (cls.port, path), data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return r.status, dict(r.headers), json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, dict(e.headers), json.loads(raw) if raw else None
        except OSError:
            return 0, {}, None

    def rpc(self, token, method, params=None, **kw):
        return self.http("POST", "/mcp", token, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, **kw)

    def mcpctl(self, *args, ok=True, env=None):
        r = subprocess.run(
            [sys.executable, ROOT + "/mcpctl.py", "--config", self.config, *args],
            capture_output=True,
            text=True,
            env=dict(os.environ, **(env or {})),
        )
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def create(self, *args):
        """A new token with a worker behind it, as mcpctl and systemd would arrange: (id, token)."""
        made = json.loads(self.mcpctl("create", "--json", *args).stdout)
        path = "%s/%s.sock" % (self.cfg["sockets"], made["id"])
        self.spawn([sys.executable, ROOT + "/server.py", "--unix", path, "--cwd", self.dir], REMOTE_COMPUTE_HOME=self.dir + "/home")
        wait_for("the worker", lambda: os.path.exists(path))
        return made["id"], made["token"]

    def stored(self):
        with open(self.cfg["tokens"]) as f:
            return json.load(f)["tokens"]

    def sessions(self, *args):
        time.sleep(1.5)  # the gateway writes its state once a second
        return json.loads(self.mcpctl("sessions", "--json", *args).stdout)["sessions"]

    # ---------- who gets in ----------

    def test_no_token_no_entry(self):
        self.assertEqual(self.rpc(None, "tools/list")[0], 401)
        self.assertEqual(self.rpc("hunter2", "tools/list")[0], 401)
        self.assertEqual(self.rpc("cc_abcdefgh_" + "x" * 43, "tools/list")[0], 401)
        self.assertEqual(self.http("GET", "/health")[0], 200)
        self.assertEqual(self.http("POST", "/elsewhere", body={})[0], 404)

    def test_a_token_reaches_its_worker(self):
        tid, token = self.create("--label", "works")
        status, headers, body = self.rpc(token, "initialize", {"clientInfo": {"name": "unit-test", "version": "1"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["serverInfo"]["name"], "remote-compute")
        sid = headers["Mcp-Session-Id"]
        status, _, body = self.rpc(token, "tools/call", {"name": "run", "arguments": {"command": "echo it-ran"}}, sid=sid)
        self.assertEqual(status, 200)
        self.assertIn("it-ran", body["result"]["content"][0]["text"])
        # A notification has no reply.
        self.assertEqual(self.http("POST", "/mcp", token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid=sid)[0], 202)

    def test_right_id_wrong_secret(self):
        tid, token = self.create()
        forged = "cc_%s_%s" % (tid, "A" * 43)
        status, _, body = self.rpc(forged, "tools/list")
        self.assertEqual(status, 401)
        self.assertNotIn("expired", body["error"])

    def test_the_store_holds_no_token(self):
        tid, token = self.create()
        with open(self.cfg["tokens"]) as f:
            raw = f.read()
        self.assertNotIn(token, raw)
        self.assertNotIn(token.split("_", 2)[2], raw)
        self.assertEqual(self.stored()[tid]["sha256"], cclib.digest(token))
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg["tokens"]).st_mode), 0o640)

    def test_valid_token_without_worker(self):
        made = json.loads(self.mcpctl("create", "--json").stdout)
        status, _, body = self.rpc(made["token"], "tools/list")
        self.assertEqual(status, 503)
        self.assertEqual(body["id"], 1)

    # ---------- how long a token lives ----------

    def test_lifetimes(self):
        tid, _ = self.create()
        t = self.stored()[tid]
        self.assertEqual(t["expires"] - t["created"], 12 * 3600)
        tid, _ = self.create("--ttl", "7d")
        t = self.stored()[tid]
        self.assertEqual(t["expires"] - t["created"], 7 * 86400)
        for too_long in ("8d", "7d1s", "2w", "700000"):
            r = self.mcpctl("create", "--ttl", too_long, ok=False)
            self.assertNotEqual(r.returncode, 0, too_long)
            self.assertIn("7d", r.stderr)
        self.assertNotEqual(self.mcpctl("create", "--ttl", "5s", ok=False).returncode, 0)
        self.assertNotEqual(self.mcpctl("create", "--ttl", "soon", ok=False).returncode, 0)

    def test_a_config_can_lower_the_ceiling_but_not_raise_it(self):
        self.assertEqual(cclib.max_ttl({"max_ttl": "30d"}), 7 * 86400)
        self.assertEqual(cclib.max_ttl({"max_ttl": "1d"}), 86400)

    def test_expiry(self):
        tid, token = self.create()
        self.assertEqual(self.rpc(token, "tools/list")[0], 200)
        # Time passes.
        with cclib.Store(self.cfg["tokens"]).edit() as data:
            data["tokens"][tid]["expires"] = int(time.time()) - 1
        status, _, body = self.rpc(token, "tools/list")
        self.assertEqual(status, 401)
        self.assertIn("expired", body["error"])
        self.assertNotIn(tid, self.mcpctl("tokens").stdout)
        self.assertIn("expired", self.mcpctl("tokens", "--all").stdout)

    def test_revoke(self):
        tid, token = self.create("--label", "to-revoke")
        other_id, other = self.create("--label", "bystander")
        self.assertEqual(self.rpc(token, "tools/list")[0], 200)
        out = self.mcpctl("revoke", "to-revoke").stdout
        self.assertIn("revoked " + tid, out)
        status, _, body = self.rpc(token, "tools/list")
        self.assertEqual(status, 401)
        self.assertIn("revoked", body["error"])
        self.assertEqual(self.rpc(other, "tools/list")[0], 200)
        self.assertNotIn(tid, [s["token"] for s in self.sessions("--all")])
        self.assertNotEqual(self.mcpctl("revoke", "no-such-token", ok=False).returncode, 0)

    def test_labels_name_one_active_token(self):
        self.create("--label", "only-one")
        r = self.mcpctl("create", "--label", "only-one", ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotEqual(self.mcpctl("create", "--label", "no spaces", ok=False).returncode, 0)

    # ---------- who is connected ----------

    def test_sessions_are_listed(self):
        tid, token = self.create("--label", "watched")
        _, headers, _ = self.rpc(token, "initialize", {"clientInfo": {"name": "claude-code", "version": "9.9"}},
                                 forwarded="198.51.100.7")  # fmt: skip
        sid = headers["Mcp-Session-Id"]
        for _ in range(3):
            self.rpc(token, "tools/call", {"name": "list_dir", "arguments": {"path": "."}}, sid=sid, forwarded="198.51.100.7")
        mine = [s for s in self.sessions() if s["token"] == tid]
        self.assertEqual(len(mine), 1)
        s = mine[0]
        self.assertEqual((s["sid"], s["client"], s["ip"], s["calls"], s["last_tool"]), (sid, "claude-code 9.9", "198.51.100.7", 3, "list_dir"))
        text = self.mcpctl("sessions").stdout
        self.assertIn("claude-code 9.9", text)
        self.assertIn("198.51.100.7", text)
        # A client that keeps no session, like client.py, is listed too.
        self.rpc(token, "tools/list", forwarded="198.51.100.8")
        mine = [s for s in self.sessions() if s["token"] == tid]
        self.assertEqual(sorted(s["implicit"] for s in mine), [False, True])
        # Ending a session takes it off the list.
        self.assertEqual(self.http("DELETE", "/mcp", token, sid=sid)[0], 200)
        self.assertEqual([s["implicit"] for s in self.sessions() if s["token"] == tid], [True])

    def test_one_token_cannot_occupy_the_gateway(self):
        import threading

        tid, token = self.create()
        other_id, other = self.create()
        statuses = []

        def slow():
            statuses.append(self.rpc(token, "tools/call", {"name": "run", "arguments": {"command": "sleep 3"}})[0])

        threads = [threading.Thread(target=slow) for _ in range(24)]
        for t in threads:
            t.start()
        time.sleep(1)
        # While the first token has all it may have waiting, another token is served as usual.
        self.assertEqual(self.rpc(other, "tools/list")[0], 200)
        for t in threads:
            t.join()
        self.assertEqual(sorted(set(statuses)), [200, 429], statuses)
        self.assertEqual(statuses.count(200), 16)
        self.assertEqual(self.rpc(token, "tools/list")[0], 200)

    def test_garbage_in_the_state_file_does_not_break_mcpctl(self):
        state = self.dir + "/garbage-state.json"
        config = self.dir + "/config-garbage.json"
        with open(config, "w") as f:
            json.dump(dict(self.cfg, state=state), f)
        for content in ("[]", '{"sessions": [1, {"token": 5}, {"token": "x"}], "tokens": {"a": 1}}', "not json"):
            with open(state, "w") as f:
                f.write(content)
            for cmd in ("sessions", "tokens", "status"):
                r = subprocess.run([sys.executable, ROOT + "/mcpctl.py", "--config", config, cmd], capture_output=True, text=True)
                self.assertNotIn("Traceback", r.stderr, (content, cmd))

    def test_forwarded_address_is_the_last_hop(self):
        tid, token = self.create()
        self.rpc(token, "tools/list", forwarded="10.9.9.9, 198.51.100.44")
        self.assertEqual([s["ip"] for s in self.sessions() if s["token"] == tid], ["198.51.100.44"])

    # ---------- what mcpctl asks of systemd ----------

    def test_sync_starts_and_stops_workers(self):
        fake = self.dir + "/fake"
        os.makedirs(fake + "/bin", exist_ok=True)
        with open(fake + "/bin/systemctl", "w") as f:
            f.write(FAKE_SYSTEMCTL)
        os.chmod(fake + "/bin/systemctl", 0o755)
        config = self.dir + "/config-systemd.json"
        with open(config, "w") as f:
            json.dump(dict(self.cfg, systemd=True, tokens=fake + "/tokens.json"), f)
        env = dict(os.environ, PATH=fake + "/bin:" + os.environ["PATH"], FAKE_SYSTEMD=fake)

        def run(*args):
            open(fake + "/calls", "w").close()
            r = subprocess.run([sys.executable, ROOT + "/mcpctl.py", "--config", config, *args], capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            with open(fake + "/calls") as f:
                return f.read().splitlines()

        live = json.loads(subprocess.run([sys.executable, ROOT + "/mcpctl.py", "--config", config, "create", "--json"],
                                         capture_output=True, text=True, env=env).stdout)["id"]  # fmt: skip
        dead = json.loads(subprocess.run([sys.executable, ROOT + "/mcpctl.py", "--config", config, "create", "--json"],
                                         capture_output=True, text=True, env=env).stdout)["id"]  # fmt: skip
        with cclib.Store(fake + "/tokens.json").edit() as data:
            data["tokens"][dead]["expires"] = int(time.time()) - 1
        with open(fake + "/units", "w") as f:
            f.write("mcp-session@%s.socket loaded active listening x\n" % dead)
            f.write("mcp-session@%s.service loaded active running x\n" % dead)
            f.write("mcp-session@strangerx.service loaded active running x\n")
            f.write("mcp-session@%s.service loaded failed failed x\n" % "gonegone")
        calls = run("sync")
        self.assertIn("start mcp-session@%s.socket" % live, calls)
        self.assertIn("stop mcp-session@%s.socket mcp-session@%s.service" % (dead, dead), calls)
        # A worker with no token at all is stopped too; one that is not running is left alone.
        self.assertIn("stop mcp-session@strangerx.socket mcp-session@strangerx.service", calls)
        self.assertFalse([c for c in calls if "gonegone" in c])
        self.assertFalse([c for c in calls if c.startswith("stop") and live in c])
        calls = run("revoke", live)
        self.assertIn("stop mcp-session@%s.socket mcp-session@%s.service" % (live, live), calls)


class Spans(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(cclib.parse_span("12h"), 43200)
        self.assertEqual(cclib.parse_span("2d12h"), 216000)
        self.assertEqual(cclib.parse_span("90m"), 5400)
        self.assertEqual(cclib.parse_span("3600"), 3600)
        self.assertEqual(cclib.parse_span("1w"), 7 * 86400)
        for bad in ("", "h", "12 parsecs", "-1h", "1.5h"):
            with self.assertRaises(ValueError):
                cclib.parse_span(bad)

    def test_format(self):
        self.assertEqual(cclib.fmt_span(43200), "12h")
        self.assertEqual(cclib.fmt_span(43199), "11h 59m")
        self.assertEqual(cclib.fmt_span(7 * 86400 - 60), "6d 23h")
        self.assertEqual(cclib.fmt_span(0), "0s")

    def test_token_ids(self):
        tid, token = cclib.new_token()
        self.assertEqual(cclib.token_id(token), tid)
        for bad in ("", "cc_", "cc_short_" + "x" * 43, "xx_abcdefgh_" + "x" * 43, "cc_ABCDEFGH_" + "x" * 43, "cc_abcdefgh_short"):
            self.assertIsNone(cclib.token_id(bad), bad)


if __name__ == "__main__":
    unittest.main()
