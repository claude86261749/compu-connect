#!/usr/bin/env python3
"""relay/gcp-tunnel against a fake gcloud (tests/fake_gcloud.py): what it creates, that running it again changes
nothing, that a changed setting changes only what it must, and that down removes it all.

    python3 -m unittest tests.test_gcp_tunnel -v
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "relay", "gcp-tunnel")


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
class GcpTunnel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gcp-tunnel-test-")
        self.dir = os.path.join(self.tmp, ".tunnel")
        self.state = os.path.join(self.tmp, "project.json")
        bin_dir = os.path.join(self.tmp, "bin")
        os.mkdir(bin_dir)
        with open(os.path.join(bin_dir, "gcloud"), "w") as f:
            f.write('#!/bin/sh\nexec %s %s "$@"\n' % (sys.executable, os.path.join(ROOT, "tests", "fake_gcloud.py")))
        os.chmod(os.path.join(bin_dir, "gcloud"), 0o755)
        self.env = dict(os.environ, PATH=bin_dir + ":" + os.environ["PATH"], FAKE_GCLOUD_STATE=self.state, GCP_TUNNEL_DIR=self.dir)
        self.key = os.path.join(self.tmp, "box.pub")
        with open(self.key, "w") as f:
            f.write("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBoxBoxBoxBoxBoxBoxBoxBoxBoxBoxBoxBoxBoxBoxBox mcp-tunnel@nixbox\n")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def cli(self, *args, ok=True, env=None):
        r = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True, env=dict(self.env, **(env or {})),
                           stdin=subprocess.DEVNULL)  # fmt: skip
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def project(self):
        with open(self.state) as f:
            return json.load(f)

    def changes(self):
        """The changes made since the last call."""
        p = self.project()
        new = p["changes"][getattr(self, "_seen", 0) :]
        self._seen = len(p["changes"])
        return new

    def instance(self):
        return self.project()["instances"]["us-east1-b/mcp-relay"]

    def metadata(self):
        return {i["key"]: i["value"] for i in self.instance()["metadata"]["items"]}

    def test_up_makes_everything_and_again_changes_nothing(self):
        r = self.cli("up", "--domain", "mcp.example.com", "--tunnel-key", self.key)
        made = self.changes()
        self.assertEqual(
            [c.split()[:3] for c in made],
            [
                ["services", "enable", "compute.googleapis.com"],
                ["compute", "networks", "create"],
                ["compute", "firewall-rules", "create"],
                ["compute", "firewall-rules", "create"],
                ["compute", "addresses", "create"],
                ["compute", "instances", "create"],
            ],
        )
        p = self.project()
        self.assertEqual(p["firewalls"]["mcp-relay-ssh"]["allowed"], [{"IPProtocol": "tcp", "ports": ["22"]}])
        self.assertEqual(sorted(p["firewalls"]["mcp-relay-web"]["allowed"][0]["ports"]), ["443", "80"])
        md = self.metadata()
        with open(os.path.join(ROOT, "relay", "startup.sh")) as f:
            self.assertEqual(md["startup-script"], f.read())
        self.assertEqual(md["mcp-domain"], "mcp.example.com")
        self.assertTrue(md["mcp-tunnel-key"].endswith("mcp-tunnel@nixbox"))
        self.assertEqual((md["block-project-ssh-keys"], md["enable-oslogin"]), ("TRUE", "FALSE"))
        self.assertTrue(md["ssh-keys"].startswith("relay-admin:ssh-ed25519 "))
        self.assertIn("Point an A record for mcp.example.com at 127.0.0.101", r.stdout)

        # What it keeps, and how.
        for name in ["config.json", "host_ed25519", "admin_ed25519", "tunnel_key.pub", "known_hosts", "relay.nix"]:
            self.assertTrue(os.path.exists(os.path.join(self.dir, name)), name)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        for secret in ["host_ed25519", "admin_ed25519", "config.json"]:
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, secret)).st_mode), 0o600, secret)
        with open(os.path.join(self.dir, "host_ed25519.pub")) as f:
            host_pub = " ".join(f.read().split()[:2])
        nix = self.cli("nix").stdout
        self.assertIn('relay.host = "127.0.0.101";', nix)
        self.assertIn('relay.hostPublicKey = "%s";' % host_pub, nix)
        self.assertIn('domain = "mcp.example.com";', nix)
        with open(os.path.join(self.dir, "known_hosts")) as f:
            self.assertEqual(f.read(), "127.0.0.101,mcp.example.com %s\n" % host_pub)

        # Again, with nothing given: it remembers, and changes nothing.
        self.cli("up")
        self.assertEqual(self.changes(), [])
        self.assertEqual(self.cli("up", "--dry-run").stdout.count("would run"), 0)

    def test_a_changed_setting_changes_only_that(self):
        self.cli("up", "--domain", "mcp.example.com", "--project", "fake-project")
        self.changes()
        rev = self.metadata()["mcp-revision"]
        self.assertNotIn("mcp-tunnel-key", self.metadata())

        # The tunnel key arrives later.
        self.cli("up", "--tunnel-key", self.key)
        made = self.changes()
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0].startswith("compute instances add-metadata mcp-relay"), made)
        self.assertNotEqual(self.metadata()["mcp-revision"], rev)
        self.assertEqual(self.instance()["guest"], "ready " + self.metadata()["mcp-revision"])

        # SSH only from one place: the rule is updated, not made again.
        self.cli("up", "--ssh-from", "198.51.100.0/24")
        self.assertEqual(self.changes(), ["compute firewall-rules update mcp-relay-ssh"])
        self.assertEqual(self.project()["firewalls"]["mcp-relay-ssh"]["sourceRanges"], ["198.51.100.0/24"])

        # An e-mail address for Let's Encrypt, and then none again.
        self.cli("up", "--acme-email", "ops@example.com")
        self.assertEqual(self.metadata()["mcp-acme-email"], "ops@example.com")
        self.changes()
        cfg = os.path.join(self.dir, "config.json")
        with open(cfg) as f:
            c = json.load(f)
        del c["acme_email"]
        with open(cfg, "w") as f:
            json.dump(c, f)
        self.cli("up")
        self.assertIn("compute instances remove-metadata mcp-relay", self.changes())
        self.assertNotIn("mcp-acme-email", self.metadata())

    def test_dry_run_changes_nothing(self):
        self.cli("up", "--domain", "mcp.example.com")
        self.changes()
        out = self.cli("up", "--tunnel-key", self.key, "--dry-run").stdout
        self.assertIn("would run: gcloud compute instances add-metadata", out)
        self.assertEqual(self.changes(), [])
        self.assertNotIn("mcp-tunnel-key", self.metadata())
        self.assertFalse(os.path.exists(os.path.join(self.dir, "tunnel_key.pub")))

    def test_a_stopped_relay_is_started(self):
        self.cli("up", "--domain", "mcp.example.com")
        self.changes()
        p = self.project()
        p["instances"]["us-east1-b/mcp-relay"]["status"] = "TERMINATED"
        with open(self.state, "w") as f:
            json.dump(p, f)
        self.cli("up")
        self.assertEqual(self.changes(), ["compute instances start mcp-relay"])

    def test_recreate_keeps_address_and_identity(self):
        self.cli("up", "--domain", "mcp.example.com")
        before = self.metadata()
        ip = self.instance()["networkInterfaces"][0]["accessConfigs"][0]["natIP"]
        self.changes()
        self.cli("up", "--recreate")
        self.assertEqual(self.changes(), ["compute instances delete mcp-relay", "compute instances create mcp-relay"])
        self.assertEqual(self.instance()["networkInterfaces"][0]["accessConfigs"][0]["natIP"], ip)
        self.assertEqual(self.metadata()["mcp-host-key"], before["mcp-host-key"])

    def test_cloud_dns(self):
        self.cli("up", "--domain", "mcp.example.com", "--dns-zone", "example-com")
        p = self.project()
        self.assertIn("dns.googleapis.com", p["services"])
        self.assertEqual(p["dns"]["example-com/mcp.example.com."]["rrdatas"], ["127.0.0.101"])
        self.changes()
        p["dns"]["example-com/mcp.example.com."]["rrdatas"] = ["192.0.2.9"]
        with open(self.state, "w") as f:
            json.dump(p, f)
        self.cli("up")
        self.assertEqual(self.changes(), ["dns record-sets update mcp.example.com."])
        self.cli("down", "--yes")
        self.assertEqual(self.project()["dns"], {})

    def test_a_failing_startup_script_is_reported(self):
        r = self.cli("up", "--domain", "mcp.example.com", ok=False, env={"FAKE_GCLOUD_FAIL": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("did not finish (failed", r.stdout)
        self.assertIn("installing packages", r.stdout)

    def test_down(self):
        self.cli("up", "--domain", "mcp.example.com")
        self.assertNotEqual(self.cli("down", ok=False).returncode, 0, "down must not go ahead unasked")
        self.assertTrue(self.project()["instances"])
        self.changes()
        self.cli("down", "--yes", "--keep-address")
        p = self.project()
        self.assertEqual((p["instances"], p["firewalls"], p["networks"]), ({}, {}, {}))
        self.assertEqual(list(p["addresses"]), ["us-east1/mcp-relay"])
        # Up again: the same address, and the same keys.
        self.cli("up")
        self.assertEqual(self.instance()["networkInterfaces"][0]["accessConfigs"][0]["natIP"], "127.0.0.101")
        self.cli("down", "--yes")
        p = self.project()
        self.assertEqual((p["instances"], p["firewalls"], p["networks"], p["addresses"]), ({}, {}, {}, {}))
        self.assertEqual(self.cli("down", "--yes").stdout.strip(), "nothing to delete")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "host_ed25519")), "down keeps the files")

    def test_bad_input(self):
        self.assertIn("which domain", self.cli("up", ok=False).stderr)
        self.assertIn("not a domain", self.cli("up", "--domain", "mcp.example.com; rm -rf ~", ok=False).stderr)
        bad = os.path.join(self.tmp, "bad.pub")
        with open(bad, "w") as f:
            f.write('ssh-ed25519 AAAA",command="x\n')
        self.assertIn("not one line of an SSH public key", self.cli("up", "--domain", "a.example.com", "--tunnel-key", bad, ok=False).stderr)
        self.assertIn("run gcp-tunnel up first", self.cli("status", ok=False).stderr)


if __name__ == "__main__":
    unittest.main()
