#!/usr/bin/env bash
# The relay of the always-on Remote Compute: a small public machine that lends your domain to a box at home.
#
#   Claude ──HTTPS──▶ this machine (Caddy, a certificate for your domain)
#                          │ 127.0.0.1:8765
#                          ▼
#                     an SSH tunnel the box opened from inside its network ──▶ the gateway on the box
#
# It runs nothing of Remote Compute and holds no token. It does see the requests, tokens included, on their way
# through, because the certificate is here: treat it as part of the box.
#
# Meant as the startup script of a Google Compute Engine e2-micro with Debian 12 or later; relay/gcp-tunnel sets
# all of it up. It runs as root at every boot, and again whenever the instance's mcp-revision changes, and may be
# run again at any time. It reads from the instance's metadata:
#
#   mcp-domain        the name to serve, e.g. mcp.example.com; its DNS A record must point at this machine
#   mcp-tunnel-key    optional: the box's tunnel key, one line, /var/lib/mcp-tunnel/id_ed25519.pub there. Until it
#                     is given, the relay is up but no box can connect
#   mcp-host-key      optional: the SSH host key (private, ed25519) this machine is to have, so that it keeps one
#                     identity when the instance is made again; without it, the image's own host key is used
#   mcp-acme-email    optional: where the certificate authority may write about the certificate
#   mcp-revision      optional: a name for this set of settings, reported back when they are in place
#
# It reports how far it got as the guest attribute mcp/status: "running REV", "ready REV" or "failed REV: why".
#
# On any other Debian or Ubuntu machine, as root:
#
#   MCP_DOMAIN=mcp.example.com MCP_TUNNEL_KEY='ssh-ed25519 AAAA...' bash startup.sh
set -euo pipefail

PORT=8765
STATE=/var/lib/mcp-relay
METADATA=http://metadata.google.internal/computeMetadata/v1/instance

meta() {
  curl -fsS -m 5 -H 'Metadata-Flavor: Google' "$METADATA/attributes/$1" 2>/dev/null || true
}

on_gce() { curl -fsS -m 2 -o /dev/null -H 'Metadata-Flavor: Google' "$METADATA/id" 2>/dev/null; }

GCE=""
on_gce && GCE=1

report() {
  [[ -n $GCE ]] || return 0
  curl -fsS -m 5 -X PUT --data "$1" -H 'Metadata-Flavor: Google' "$METADATA/guest-attributes/mcp/status" \
    >/dev/null 2>&1 || true
}

# One run at a time: at boot, the runner and the watcher could both start one.
mkdir -p "$STATE"
chmod 700 "$STATE"
exec 9> "$STATE/lock"
flock 9

REVISION="${MCP_REVISION:-$(meta mcp-revision)}"
REVISION="${REVISION:-manual}"
step="reading the settings"
trap 'report "failed $REVISION: $step"; echo "mcp-relay: FAILED while $step" >&2' ERR
report "running $REVISION"

DOMAIN="${MCP_DOMAIN:-$(meta mcp-domain)}"
TUNNEL_KEY="${MCP_TUNNEL_KEY:-$(meta mcp-tunnel-key)}"
ACME_EMAIL="${MCP_ACME_EMAIL:-$(meta mcp-acme-email)}"
HOST_KEY="${MCP_HOST_KEY:-$(meta mcp-host-key)}"
# One line, whatever the file it came from ended with.
TUNNEL_KEY="$(printf '%s' "$TUNNEL_KEY" | tr -d '\r' | head -n 1)"

fail() {
  echo "mcp-relay: $1" >&2
  report "failed $REVISION: $1"
  exit 1
}

[[ $DOMAIN =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || fail "no usable mcp-domain (got '$DOMAIN')"
if [[ -n $TUNNEL_KEY && ! $TUNNEL_KEY =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp[0-9]+)\ [A-Za-z0-9+/=]+(\ [A-Za-z0-9@._-]+)?$ ]]; then
  fail "mcp-tunnel-key is not one line of an SSH public key"
fi
if [[ -n $ACME_EMAIL && ! $ACME_EMAIL =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$ ]]; then
  fail "mcp-acme-email does not look like an address"
fi

# ---------- packages ----------

step="installing packages"
need=""
command -v caddy >/dev/null || need="$need caddy"
command -v sshd >/dev/null || [[ -x /usr/sbin/sshd ]] || need="$need openssh-server"
dpkg -s unattended-upgrades >/dev/null 2>&1 || need="$need unattended-upgrades"
if [[ -n $need ]]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  # shellcheck disable=SC2086
  apt-get install -y -qq --no-install-recommends $need >/dev/null
fi
# Security updates install themselves: nobody logs in here to do it.
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

# ---------- SSH: this machine's identity, and the account that holds the tunnel and can do nothing else ----------

step="setting up SSH"
mkdir -p /etc/ssh/sshd_config.d
if [[ -n $HOST_KEY ]]; then
  # Kept apart from the image's own host keys, which the guest environment may make anew.
  install -d -m 0700 /etc/ssh/mcp
  (umask 077 && printf '%s\n' "$HOST_KEY" | tr -d '\r' > /etc/ssh/mcp/ssh_host_ed25519_key.new)
  ssh-keygen -y -f /etc/ssh/mcp/ssh_host_ed25519_key.new > /etc/ssh/mcp/ssh_host_ed25519_key.pub.new ||
    fail "mcp-host-key is not an OpenSSH private key"
  mv /etc/ssh/mcp/ssh_host_ed25519_key.new /etc/ssh/mcp/ssh_host_ed25519_key
  mv /etc/ssh/mcp/ssh_host_ed25519_key.pub.new /etc/ssh/mcp/ssh_host_ed25519_key.pub
  printf '# Written by the Remote Compute relay startup.sh.\nHostKey /etc/ssh/mcp/ssh_host_ed25519_key\n' \
    > /etc/ssh/sshd_config.d/40-mcp-host-key.conf
  HOST_PUB=/etc/ssh/mcp/ssh_host_ed25519_key.pub
else
  rm -f /etc/ssh/sshd_config.d/40-mcp-host-key.conf
  HOST_PUB=/etc/ssh/ssh_host_ed25519_key.pub
fi

id tunnel >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin tunnel

# The key lives where the account cannot change it, and is good for one thing: listening on the relay's loopback.
install -m 0644 -o root -g root /dev/null /etc/ssh/mcp-tunnel.keys.new
if [[ -n $TUNNEL_KEY ]]; then
  printf 'restrict,port-forwarding,permitlisten="127.0.0.1:%s" %s\n' "$PORT" "$TUNNEL_KEY" > /etc/ssh/mcp-tunnel.keys.new
fi
mv /etc/ssh/mcp-tunnel.keys.new /etc/ssh/mcp-tunnel.keys

cat > /etc/ssh/sshd_config.d/50-mcp-tunnel.conf <<EOF
# Written by the Remote Compute relay startup.sh.
Match User tunnel
    AuthorizedKeysFile /etc/ssh/mcp-tunnel.keys
    AuthenticationMethods publickey
    PasswordAuthentication no
    AllowTcpForwarding remote
    PermitListen 127.0.0.1:$PORT
    GatewayPorts no
    AllowAgentForwarding no
    AllowStreamLocalForwarding no
    X11Forwarding no
    PermitTTY no
    PermitTunnel no
    ForceCommand /bin/false
    ClientAliveInterval 15
    ClientAliveCountMax 3
# What follows in other files applies to everyone again.
Match all
EOF
grep -qE '^\s*Include\s+/etc/ssh/sshd_config\.d/' /etc/ssh/sshd_config ||
  sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' /etc/ssh/sshd_config
mkdir -p /run/sshd
"$(command -v sshd || echo /usr/sbin/sshd)" -t || fail "sshd does not accept its configuration"
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || systemctl restart ssh

# ---------- the web server: your domain, a certificate, and the two paths that lead to the box ----------

step="setting up Caddy"
{
  if [[ -n $ACME_EMAIL ]]; then
    printf '{\n\temail %s\n}\n\n' "$ACME_EMAIL"
  fi
  cat <<EOF
# Written by the Remote Compute relay startup.sh.
$DOMAIN {
	@mcp path /mcp /health
	handle @mcp {
		request_body {
			max_size 48MB
		}
		reverse_proxy 127.0.0.1:$PORT
	}
	handle {
		respond 404
	}
}
EOF
} > /etc/caddy/Caddyfile
if ! out="$(caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile 2>&1)"; then
  echo "$out" >&2
  fail "Caddy does not accept its configuration"
fi
systemctl enable caddy >/dev/null 2>&1 || true
systemctl reload caddy 2>/dev/null || systemctl restart caddy

# ---------- on Compute Engine: run again by itself when the settings change ----------

if [[ -n $GCE ]]; then
  step="setting up the watcher"
  cat > /usr/local/sbin/mcp-relay-watch <<'EOF'
#!/usr/bin/env bash
# Run the startup script again when the instance's mcp-revision changes. Written by the relay startup.sh.
url=http://metadata.google.internal/computeMetadata/v1/instance/attributes/mcp-revision
tried=""
while true; do
  want="$(curl -fsS -m 90 -H 'Metadata-Flavor: Google' "$url?wait_for_change=true&timeout_sec=60" 2>/dev/null)" || {
    sleep 10
    continue
  }
  have="$(cat /var/lib/mcp-relay/revision 2>/dev/null || true)"
  if [[ -n $want && $want != "$have" && $want != "$tried" ]]; then
    tried="$want"
    echo "settings changed to $want: running the startup script again"
    google_metadata_script_runner startup || echo "the startup script failed"
  fi
done
EOF
  chmod 755 /usr/local/sbin/mcp-relay-watch
  cat > /etc/systemd/system/mcp-relay-watch.service <<'EOF'
[Unit]
Description=Remote Compute relay: apply changed settings
After=network-online.target google-startup-scripts.service
Wants=network-online.target

[Service]
ExecStart=/usr/local/sbin/mcp-relay-watch
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  # Started, not restarted: this script may be running inside it.
  systemctl enable --now mcp-relay-watch.service >/dev/null 2>&1
fi

trap - ERR
printf '%s\n' "$REVISION" > "$STATE/revision"
echo "mcp-relay: serving https://$DOMAIN/mcp from 127.0.0.1:$PORT"
if [[ -n $TUNNEL_KEY ]]; then
  echo "mcp-relay: the tunnel may be opened with: $TUNNEL_KEY"
else
  echo "mcp-relay: no tunnel key yet, so no box can connect"
fi
echo "mcp-relay: this machine's SSH host key: $(cut -d' ' -f1,2 "$HOST_PUB")"
echo "mcp-relay: ready $REVISION"
report "ready $REVISION"
