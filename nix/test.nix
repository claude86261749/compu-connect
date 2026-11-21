# The always-on setup in two virtual machines: `box` is the machine with the module on, `relay` stands in for the
# small cloud instance (relay/startup.sh makes the same sshd and Caddy configuration on Debian) and for whoever
# talks to it. Everything is real: TLS, the SSH tunnel, the gateway, systemd starting a sandboxed worker per token.
#
#   nix-build nix/test.nix
#
# Without KVM it still runs, on an emulated CPU, in about a quarter of an hour.
{
  pkgs ? import <nixpkgs> { },
}:

let
  # The relay's SSH host key, known before the machines exist so that `box` can pin it.
  relayHostKey = pkgs.runCommand "relay-host-key" { nativeBuildInputs = [ pkgs.openssh ]; } ''
    mkdir $out
    ssh-keygen -q -t ed25519 -N "" -C relay -f $out/key
  '';
  # A certificate for the relay's name, which `box` is told to trust: what Let's Encrypt provides in real life.
  tls = pkgs.runCommand "mcp-test-certificate" { nativeBuildInputs = [ pkgs.openssl ]; } ''
    mkdir $out
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
      -subj "/CN=mcp.test" -addext "subjectAltName=DNS:mcp.test" -keyout $out/key.pem -out $out/cert.pem
  '';
in
pkgs.testers.runNixOSTest {
  name = "compu-connect-always-on";

  nodes.box =
    { pkgs, nodes, ... }:
    {
      imports = [ ./module.nix ];
      virtualisation.memorySize = 2048;
      virtualisation.cores = 2;
      services.openssh.enable = true;

      # The client is run here too, but it goes the long way round: out to the relay's public name and back
      # in through the tunnel.
      environment.systemPackages = [
        pkgs.curl
        pkgs.python3
      ];
      environment.etc."client.py".source = ../client.py;
      networking.extraHosts = "${nodes.relay.networking.primaryIPAddress} mcp.test";
      security.pki.certificateFiles = [ "${tls}/cert.pem" ];

      # There is no Internet in the test: do not wait for the binary cache.
      nix.settings.substituters = pkgs.lib.mkForce [ ];

      services.compu-connect = {
        enable = true;
        domain = "mcp.test";
        workDirs = [ "/data/mcp-user" ];
        relay.host = "relay";
        relay.hostPublicKey = pkgs.lib.removeSuffix "\n" (builtins.readFile "${relayHostKey}/key.pub");
        hardware.devices = [ "/dev/fuse rw" ];
        nix.enable = true;
        packages = with pkgs; [
          bashInteractive
          coreutils
          gnugrep
          curl
          python3
          procps
          util-linux
          pciutils
          file
          iputils
        ];
      };
      # A port a session will listen on: open in the machine's firewall, so that the firewall is not what keeps
      # the home network out of it.
      networking.firewall.allowedTCPPorts = [ 9100 ];

      # Things of the machine that anyone on it can read, and a session must not see.
      systemd.tmpfiles.rules = [
        "d /data 0755 root root -"
        "d /data/other 0755 root root -"
        "f /data/other/readable 0644 root root - not-for-sessions"
        "f /var/lib/readable 0644 root root - not-for-sessions"
      ];
    };

  nodes.relay =
    { pkgs, ... }:
    {
      virtualisation.cores = 2;
      networking.firewall.allowedTCPPorts = [
        22
        443
        8080
      ];
      networking.firewall.allowedUDPPorts = [ 9200 ];
      environment.systemPackages = [ pkgs.curl ];

      # The account that holds the tunnel and can do nothing else: as in relay/startup.sh.
      users.groups.tunnel = { };
      users.users.tunnel = {
        isSystemUser = true;
        group = "tunnel";
        shell = "${pkgs.shadow}/bin/nologin";
      };
      environment.etc."ssh/ssh_host_ed25519_key" = {
        source = "${relayHostKey}/key";
        mode = "0600";
      };
      services.openssh = {
        enable = true;
        hostKeys = [ ];
        extraConfig = ''
          HostKey /etc/ssh/ssh_host_ed25519_key
          Match User tunnel
            AuthorizedKeysFile /etc/ssh/tunnel_authorized_keys
            AllowTcpForwarding remote
            PermitListen 127.0.0.1:8765
            GatewayPorts no
            AllowAgentForwarding no
            AllowStreamLocalForwarding no
            X11Forwarding no
            PermitTTY no
            PermitTunnel no
            ForceCommand /run/current-system/sw/bin/false
        '';
      };

      # The web server in front: as in relay/startup.sh, but with a certificate made for the test.
      services.caddy = {
        enable = true;
        virtualHosts."mcp.test".extraConfig = ''
          tls ${tls}/cert.pem ${tls}/key.pem
          @mcp path /mcp /health
          handle @mcp {
            reverse_proxy 127.0.0.1:8765
          }
          handle {
            respond 404
          }
        '';
      };

      # Something to reach: by its home-network address it stands for the router or the NAS, by the other for
      # the Internet. A web server, and a UDP port that writes down what arrives.
      systemd.services.web = {
        wantedBy = [ "multi-user.target" ];
        serviceConfig.ExecStart = "${pkgs.python3}/bin/python3 -m http.server 8080 --directory /var/empty";
      };
      systemd.services.udp-log = {
        wantedBy = [ "multi-user.target" ];
        serviceConfig.ExecStart = "${pkgs.python3}/bin/python3 -u ${pkgs.writeText "udp-log.py" ''
          import socket
          s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
          s.bind(("0.0.0.0", 9200))
          while True:
              print("datagram:", s.recvfrom(100)[0].decode(), flush=True)
        ''}";
      };
    };

  testScript =
    { nodes, ... }:
    ''
      import shlex

      # The relay's address in the network the two machines share, which stands for the home network, and one
      # outside every private range, which stands for the Internet.
      LAN = "${nodes.relay.networking.primaryIPAddress}"
      INTERNET = "203.0.113.2"
      BOX = "${nodes.box.networking.primaryIPAddress}"
      BOX6 = "${nodes.box.networking.primaryIPv6Address}"


      def client(token, *args):
          """client.py, through TLS and Caddy on the relay, the tunnel, the gateway and the token's worker."""
          env = f"REMOTE_COMPUTE_URL=https://mcp.test/mcp REMOTE_COMPUTE_TOKEN={token}"
          return box.succeed(f"{env} python3 /etc/client.py " + " ".join(shlex.quote(a) for a in args))


      def run(token, command):
          return client(token, "run", command + "; true")


      def verdict(token, command):
          out = run(token, f"if {command} >/dev/null 2>&1; then echo VERDICT=possible; else echo VERDICT=impossible; fi")
          assert "VERDICT=" in out, out
          return "VERDICT=possible" in out


      def can(token, command):
          assert verdict(token, command), f"a session should be able to: {command}"


      def cannot(token, command):
          assert not verdict(token, command), f"a session should NOT be able to: {command}"


      def http_status(token):
          return box.succeed(
              "curl -s -o /dev/null -w '%{http_code}' -X POST https://mcp.test/mcp "
              f"-H 'Authorization: Bearer {token}' -H 'Content-Type: application/json' "
              "-d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\"}'"
          ).strip()


      start_all()
      relay.wait_for_unit("sshd.service")
      relay.wait_for_unit("caddy.service")
      relay.wait_for_unit("web.service")
      box.wait_for_unit("mcp-gateway.service")
      box.wait_for_unit("mcp-sync.service")
      box.wait_for_unit("mcp-tunnel-key.service")
      box.succeed("ip addr add 203.0.113.1/24 dev eth1")
      relay.succeed("ip addr add 203.0.113.2/24 dev eth1")

      with subtest("the relay is given the tunnel's key, and the tunnel comes up by itself"):
          key = box.succeed("cat /var/lib/mcp-tunnel/id_ed25519.pub").strip()
          relay.succeed(
              f"echo 'restrict,port-forwarding,permitlisten=\"127.0.0.1:8765\" {key}' > /etc/ssh/tunnel_authorized_keys"
          )
          relay.wait_until_succeeds("curl -fsS http://127.0.0.1:8765/health", timeout=180)
          box.wait_until_succeeds("curl -fsS https://mcp.test/health", timeout=120)
          # Nothing but the two paths is passed on, and the tunnel's end is not open to the relay's network.
          box.succeed("test $(curl -s -o /dev/null -w '%{http_code}' https://mcp.test/) = 404")
          box.succeed("test $(curl -s -o /dev/null -w '%{http_code}' https://mcp.test/mcp/../admin) = 404")
          box.fail(f"curl -fsS -m 5 http://{LAN}:8765/health")

      with subtest("the tunnel's key opens the tunnel and nothing else on the relay"):
          ssh = (
              "runuser -u mcp-tunnel -- ssh -F none -i /var/lib/mcp-tunnel/id_ed25519 -o BatchMode=yes "
              "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ExitOnForwardFailure=yes tunnel@relay"
          )
          status, out = box.execute(f"{ssh} id 2>&1")
          assert status != 0 and "uid=" not in out, out
          box.fail(f"timeout 30 {ssh} -N -R 127.0.0.1:9999:127.0.0.1:22")
          box.fail(f"timeout 30 {ssh} -N -R 0.0.0.0:8765:127.0.0.1:22")
          box.succeed(f"({ssh} -N -L 127.0.0.1:9998:127.0.0.1:8080 >/dev/null 2>&1 &) ; sleep 8")
          box.fail("curl -fsS -m 5 http://127.0.0.1:9998/")

      with subtest("no token, no entry"):
          assert http_status("") == "401"
          assert http_status("cc_abcdefgh_" + "x" * 43) == "401"

      with subtest("a token is a shell as the account"):
          t1 = box.succeed("mcpctl create --label first -q").strip()
          assert t1.startswith("cc_"), t1
          out = run(t1, "id -un; id -u; id -gn; echo home=$HOME; echo cwd=$PWD; echo shell=$SHELL")
          print(out)
          for expected in ["mcp-user", "2000", "home=/var/lib/mcp-user", "cwd=/var/lib/mcp-user", "shell=/bin/bash"]:
              assert expected in out, f"{expected} not in {out}"
          out = run(t1, "grep -E '^(NoNewPrivs|CapEff|CapBnd|Seccomp):' /proc/self/status")
          print(out)
          assert "NoNewPrivs:\t1" in out and "Seccomp:\t2" in out, out
          assert "CapEff:\t0000000000000000" in out and "CapBnd:\t0000000000000000" in out, out

      with subtest("the token is stored nowhere"):
          secret = t1.split("_", 2)[2]
          box.fail(f"grep -rF -e {shlex.quote(secret)} /var/lib/compu-connect /run/compu-connect /etc/compu-connect")
          box.fail(f"journalctl -b --no-pager -o cat | grep -F -e {shlex.quote(secret)}")

      with subtest("a session sees its own directories and none of the machine's"):
          for path in [
              "/root",
              "/home",
              "/boot",
              "/etc/shadow",
              "/etc/nixos",
              "/etc/ssh",
              "/etc/compu-connect",
              "/var/lib/compu-connect",
              "/var/lib/readable",
              "/var/log",
              "/run/compu-connect",
              "/run/current-system",
              "/nix/var/nix/db",
              "/nix/var/nix/profiles",
              "/data/other",
              "/data/other/readable",
          ]:
              cannot(t1, f"ls -d {path}")
          for path in ["/x", "/etc/x", "/bin/x", "/usr/x", "/var/x", "/nix/store/x", "/data/x", "/run/x"]:
              cannot(t1, f"touch {path}")
          can(t1, "touch ~/kept && test -O ~/kept")
          can(t1, "touch /data/mcp-user/kept")
          can(t1, "touch /tmp/gone /var/tmp/gone /dev/shm/gone")
          box.succeed("test -e /var/lib/mcp-user/kept -a -e /data/mcp-user/kept")
          box.fail("test -e /tmp/gone -o -e /var/tmp/gone -o -e /dev/shm/gone")
          out = run(t1, "ls /; echo; ls /var/lib /data; echo; cat /etc/passwd /etc/resolv.conf")
          print(out)
          assert "1.1.1.1" in out, out
          # The programs it was given work, including ones that sandbox themselves.
          can(t1, "python3 -c 'import ssl, json, sqlite3'")
          can(t1, "file -L /bin/bash | grep -q ELF")
          can(t1, "test -s /etc/ssl/certs/ca-certificates.crt")

      with subtest("a session sees no process but its own and cannot gain privileges"):
          out = run(t1, "ps -eo user= | sort -u")
          print(out)
          users = out.split("--- stdout ---")[1].split()
          assert users == ["mcp-user"], users
          cannot(t1, "unshare -U true")
          cannot(t1, "unshare -n true")
          cannot(t1, "python3 -c 'import os; os.setuid(0)'")
          cannot(t1, "python3 -c 'import ctypes; assert ctypes.CDLL(None).ptrace(0, 0, 0, 0) == 0'")

      with subtest("devices: the harmless ones and the listed one, nothing else"):
          can(t1, "head -c 8 /dev/urandom")
          # /dev/fuse and /dev/net/tun are both open to every account on the machine; only the first is listed.
          can(t1, "python3 -c \"import os; os.open('/dev/fuse', os.O_RDWR)\"")
          cannot(t1, "python3 -c \"import os; os.open('/dev/net/tun', os.O_RDWR)\"")
          box.succeed("runuser -u mcp-user -- python3 -c \"import os; os.open('/dev/net/tun', os.O_RDWR)\"")
          cannot(t1, "head -c 1 /dev/kmsg")
          cannot(t1, "head -c 1 /dev/vda")
          cannot(t1, "head -c 1 /dev/mem")
          can(t1, "lspci | grep -q .")
          cannot(t1, "sh -c 'echo 1 > /sys/bus/pci/rescan'")
          cannot(t1, "sh -c 'echo 0 > /proc/sys/kernel/randomize_va_space'")

      with subtest("the network: out to the Internet, not into the home network"):
          box.succeed(f"curl -fsS -m 20 http://{LAN}:8080/")
          can(t1, f"curl -fsS -m 20 http://{INTERNET}:8080/")
          cannot(t1, f"curl -fsS -m 10 http://{LAN}:8080/")
          # Its own address in the home network is as closed to it as everyone else's.
          connect = "python3 -c \"import socket, sys; socket.create_connection((sys.argv[1], 22), 5)\""
          box.succeed(f"{connect} {BOX}")
          cannot(t1, f"{connect} {BOX}")
          # Not by ping.
          box.succeed(f"ping -c 1 -W 5 {LAN}")
          can(t1, f"ping -c 1 -W 5 {INTERNET}")
          cannot(t1, f"ping -c 1 -W 5 {LAN}")
          # Not by UDP: of two datagrams, only the one sent the Internet's way arrives.
          send = (
              "python3 -c \"import socket, sys; s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); "
              "s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1); "
              "s.sendto(sys.argv[2].encode(), (sys.argv[1], 9200))\""
          )
          cannot(t1, f"{send} {LAN} the-home-way")
          can(t1, f"{send} {INTERNET} the-internet-way")
          relay.wait_until_succeeds("journalctl -u udp-log -o cat --no-pager | grep -q the-internet-way", timeout=30)
          relay.fail("journalctl -u udp-log -o cat --no-pager | grep -q the-home-way")
          # Not by multicast or broadcast, which is how machines on a network find each other.
          cannot(t1, f"{send} 224.0.0.251 mdns")
          cannot(t1, f"{send} 239.255.255.250 ssdp")
          cannot(t1, f"{send} 255.255.255.255 broadcast")
          # And not the other way round: what a session listens on, the home network cannot connect to.
          client(t1, "spawn", "python3 -m http.server 9100 --bind 0.0.0.0", "--name", "listener")
          relay.wait_until_succeeds("curl -fsS -m 5 http://203.0.113.1:9100/", timeout=60)
          relay.fail(f"curl -fsS -m 5 http://{BOX}:9100/")
          cannot(t1, "curl -fsS -m 10 http://[::1]:9000/")
          cannot(t1, "python3 -c 'import socket; socket.socket(socket.AF_PACKET, socket.SOCK_RAW)'")
          # The gateway itself has no port: neither a session nor anything else on the machine can talk to it.
          box.fail("curl -fsS -m 5 http://127.0.0.1:8765/health")
          box.fail("runuser -u mcp-user -- curl -fsS -m 5 --unix-socket /run/compu-connect/gateway.sock http://x/health")
          box.succeed("curl -fsS -m 5 --unix-socket /run/compu-connect/gateway.sock http://x/health")
          # Known and written down: the machine's own loopback is the sessions' too, so they do reach what
          # other programs serve there.
          box.succeed("systemd-run --unit=on-loopback python3 -m http.server 9000 --bind 127.0.0.1 --directory /var/empty")
          box.wait_until_succeeds("curl -fsS -m 5 http://127.0.0.1:9000/")
          can(t1, "curl -fsS -m 10 http://127.0.0.1:9000/")

      with subtest("a session can build with Nix, and its builds cannot reach the home network either"):
          bash = run(t1, "readlink -f /bin/bash").split("--- stdout ---")[1].strip()


          def drv(name, script, extra=""):
              return (
                  "'derivation { name = \"%s\"; system = builtins.currentSystem; builder = builtins.storePath %s; "
                  "args = [ \"-c\" \"%s\" ]; %s }'" % (name, bash, script, extra)
              )


          out = run(t1, "cat $(nix build --impure --no-link --print-out-paths --expr " + drv("who", "echo built-by-uid-$UID > $out") + ")")
          print(out)
          # It is built by the daemon, in its sandbox, not by the account.
          assert "built-by-uid-" in out and "built-by-uid-2000" not in out, out
          # A build that may use the network (its output's hash is fixed in advance) gets out to the Internet and
          # not into the home network. The names differ, so that one cannot reuse the other's result.
          fixed = 'outputHashMode = \"flat\"; outputHashAlgo = \"sha256\"; outputHash = \"dc51b8c96c2d745df3bd5590d990230a482fd247123599548e0632fdbf97fc22\";'
          fetch = "exec 3<>/dev/tcp/%s/8080 && echo ok > $out"
          # Blocked packets are dropped, not refused, so a build that tries waits until its time is up.
          cannot(t1, "timeout 60 nix build --option timeout 20 --impure --no-link --expr " + drv("to-home", fetch % LAN, fixed))
          can(t1, "timeout 60 nix build --option timeout 20 --impure --no-link --expr " + drv("to-internet", fetch % INTERNET, fixed))
          # It is an ordinary user of the daemon: it cannot turn the build sandbox off, or write to the store.
          cannot(t1, "touch /nix/store/x")
          cannot(
              t1,
              "nix build --impure --no-link --option sandbox false --expr "
              + drv("unsandboxed", "grep -q mcp-user /etc/passwd && echo outside > $out"),
          )

      with subtest("the account can do nothing outside a session either"):
          box.fail("runuser -u mcp-user -- cat /var/lib/compu-connect/tokens.json")
          box.fail("runuser -u mcp-user -- ls /run/compu-connect/sessions")
          box.succeed("grep -q 'DenyUsers mcp-user' /etc/ssh/sshd_config")
          # The gateway reads the store and cannot change it.
          box.succeed("runuser -u mcp-gateway -- cat /var/lib/compu-connect/tokens.json > /dev/null")
          box.fail("runuser -u mcp-gateway -- sh -c 'echo >> /var/lib/compu-connect/tokens.json'")
          box.fail("runuser -u mcp-gateway -- touch /var/lib/compu-connect/x")

      with subtest("lifetimes: 12 hours unless asked, 7 days at most"):
          out = box.succeed("mcpctl tokens")
          print(out)
          assert "11h 5" in out, out
          box.fail("mcpctl create --ttl 8d")
          box.succeed("mcpctl create --ttl 7d --label week -q")
          assert "6d 23h" in box.succeed("mcpctl tokens")
          box.succeed("mcpctl revoke week")

      with subtest("the operator sees who is connected and what is running"):
          t2 = box.succeed("mcpctl create --label second -q").strip()
          client(t1, "spawn", "sleep 31415", "--name", "sleeper")
          # One that tries to get away from its session: a new session and process group, its parent gone.
          client(t1, "run", "setsid nohup sh -c 'sleep 16180 & sleep 16181' > /dev/null 2>&1 &")
          client(t2, "spawn", "sleep 27182", "--name", "bystander")
          for n in ["31415", "16180", "16181", "27182"]:
              box.succeed(f"pgrep -u mcp-user -f 'sleep {n}'")
          box.wait_until_succeeds("mcpctl sessions | grep -q second")
          out = box.succeed("mcpctl sessions; echo; mcpctl tokens; echo; mcpctl status")
          print(out)
          assert "first" in out and "second" in out and "spawn" in out, out
          assert BOX in out or BOX6 in out, "the session list should show where a client comes from: " + out
          assert "from outside" in out and "DOWN" not in out, out
          box.succeed("journalctl -u mcp-gateway -o cat --no-pager | grep -F 'sleep 27182' | grep -q '\"tool\": \"spawn\"'")

      with subtest("revoking a token kills everything it started, and nothing of another token"):
          out = box.succeed("mcpctl revoke first")
          print(out)
          assert "revoked" in out and "killed" in out, out
          for n in ["31415", "16180", "16181"]:
              box.fail(f"pgrep -u mcp-user -f 'sleep {n}'")
          box.succeed("pgrep -u mcp-user -f 'sleep 27182'")
          assert http_status(t1) == "401"
          assert http_status(t2) == "200"
          box.fail("test -e /run/compu-connect/sessions/" + t1.split("_")[1] + ".sock")
          # What it left in its directories is still there for the next token.
          assert "kept" in run(t2, "ls ~ /data/mcp-user")

      with subtest("restarting the gateway disturbs nothing"):
          box.succeed("systemctl restart mcp-gateway.service")
          box.wait_for_unit("mcp-gateway.service")
          box.wait_until_succeeds("curl -fsS https://mcp.test/health", timeout=60)
          assert http_status(t2) == "200"
          box.succeed("pgrep -u mcp-user -f 'sleep 27182'")
          assert "bystander" in client(t2, "jobs")

      with subtest("a token that runs out is cut off the same way, with nobody doing anything"):
          t3 = box.succeed("mcpctl create --ttl 60s --label short -q").strip()
          client(t3, "run", "setsid nohup sleep 14142 > /dev/null 2>&1 &")
          box.succeed("pgrep -u mcp-user -f 'sleep 14142'")
          box.wait_until_fails("pgrep -u mcp-user -f 'sleep 14142'", timeout=180)
          assert http_status(t3) == "401"
          box.succeed("journalctl -u mcp-sync -o cat --no-pager | grep -q 'its token is expired'")
          assert http_status(t2) == "200"
          box.succeed("pgrep -u mcp-user -f 'sleep 27182'")

      with subtest("revoke --all leaves nothing running as the account"):
          print(box.succeed("mcpctl revoke --all"))
          box.fail("pgrep -u mcp-user")
          assert http_status(t2) == "401"
    '';
}
