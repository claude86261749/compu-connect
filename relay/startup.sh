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
# Meant as the startup script of a Google Compute Engine e2-micro with Debian 12 or later (see README.md here). It
# runs as root at every boot and may be run again at any time. It reads from the instance's metadata:
#
#   mcp-domain        the name to serve, e.g. mcp.example.com; its DNS A record must point at this machine
#   mcp-tunnel-key    the box's tunnel key: one line, the contents of /var/lib/mcp-tunnel/id_ed25519.pub there
#   mcp-acme-email    optional: where the certificate authority may write about the certificate
#
# On any other Debian or Ubuntu machine, as root:
#
#   MCP_DOMAIN=mcp.example.com MCP_TUNNEL_KEY='ssh-ed25519 AAAA...' bash startup.sh
set -euo pipefail

PORT=8765

meta() {
  curl -fsS -m 5 -H 'Metadata-Flavor: Google' \
    "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1" 2>/dev/null || true
}

DOMAIN="${MCP_DOMAIN:-$(meta mcp-domain)}"
TUNNEL_KEY="${MCP_TUNNEL_KEY:-$(meta mcp-tunnel-key)}"
ACME_EMAIL="${MCP_ACME_EMAIL:-$(meta mcp-acme-email)}"
# One line, whatever the file it came from ended with.
TUNNEL_KEY="$(printf '%s' "$TUNNEL_KEY" | tr -d '\r' | head -n 1)"

if [[ ! $DOMAIN =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
  echo "mcp-relay: no usable mcp-domain (got '$DOMAIN')" >&2
  exit 1
fi
if [[ ! $TUNNEL_KEY =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp[0-9]+)\ [A-Za-z0-9+/=]+(\ [A-Za-z0-9@._-]+)?$ ]]; then
  echo "mcp-relay: mcp-tunnel-key is not one line of an SSH public key" >&2
  exit 1
fi
if [[ -n $ACME_EMAIL && ! $ACME_EMAIL =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$ ]]; then
  echo "mcp-relay: mcp-acme-email does not look like an address" >&2
  exit 1
fi

# ---------- packages ----------

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

# ---------- the account that holds the tunnel and can do nothing else ----------

id tunnel >/dev/null 2>&1 || useradd --system --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin tunnel

# The key lives where the account cannot change it, and is good for one thing: listening on the relay's loopback.
install -m 0644 -o root -g root /dev/null /etc/ssh/mcp-tunnel.keys
printf 'restrict,port-forwarding,permitlisten="127.0.0.1:%s" %s\n' "$PORT" "$TUNNEL_KEY" > /etc/ssh/mcp-tunnel.keys

mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/50-mcp-tunnel.conf <<EOF
# Written by the Remote Compute relay's startup.sh.
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
EOF
grep -qE '^\s*Include\s+/etc/ssh/sshd_config\.d/' /etc/ssh/sshd_config ||
  sed -i '1i Include /etc/ssh/sshd_config.d/*.conf' /etc/ssh/sshd_config
mkdir -p /run/sshd
"$(command -v sshd || echo /usr/sbin/sshd)" -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || systemctl restart ssh

# ---------- the web server: your domain, a certificate, and the two paths that lead to the box ----------

{
  if [[ -n $ACME_EMAIL ]]; then
    printf '{\n\temail %s\n}\n\n' "$ACME_EMAIL"
  fi
  cat <<EOF
# Written by the Remote Compute relay's startup.sh.
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
  exit 1
fi
systemctl enable caddy >/dev/null 2>&1 || true
systemctl reload caddy 2>/dev/null || systemctl restart caddy

echo "mcp-relay: serving https://$DOMAIN/mcp from 127.0.0.1:$PORT, which the tunnel of this key provides:"
echo "mcp-relay:   $TUNNEL_KEY"
echo "mcp-relay: this machine's SSH host key, for relay.hostPublicKey on the box:"
echo "mcp-relay:   $(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)"
