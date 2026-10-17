#!/usr/bin/env bash
# Start this machine's Remote Compute MCP server behind a Cloudflare quick tunnel; print the URL and token.
#
#     bash start.sh          start (or restart) both, print URL and token
#     bash start.sh stop     stop both
#     bash start.sh status   are they running, and the URL
#
# Or without a checkout:
#
#     curl -fsSL https://raw.githubusercontent.com/claude86261749/compu-connect/HEAD/start.sh | bash
#
# Runs on a bare Ubuntu/Debian or Amazon Linux 2/2023: python3, curl, tar and CA certificates are installed with
# apt-get, dnf or yum if they are missing (as root, or through sudo). cloudflared is downloaded into
# ~/.remote-compute if it isn't installed. The quick tunnel's URL changes every time it starts.
#
# Nothing secret is in this repo. The token is made here, on first start, and kept in ~/.remote-compute/token
# (readable only by you); it stays until you delete that file. The server listens on 127.0.0.1 only, so the
# tunnel is the one way in, and every request through it must carry the token.
set -euo pipefail

RAW="${COMPU_CONNECT_RAW:-https://raw.githubusercontent.com/claude86261749/compu-connect/HEAD}"
HOME_DIR="${REMOTE_COMPUTE_HOME:-$HOME/.remote-compute}"
PORT="${PORT:-8765}"
WORKDIR="${WORKDIR:-$HOME}"
# Empty when the script comes in on stdin (curl | bash).
SRC="${BASH_SOURCE[0]:-}"
HERE=""
[[ -n $SRC && -f $SRC ]] && HERE="$(cd "$(dirname "$SRC")" && pwd)"

(umask 077 && mkdir -p "$HOME_DIR")
chmod 700 "$HOME_DIR"
cd "$HOME_DIR"

stop() {
  for p in server tunnel; do
    if [[ -f $p.pid ]] && kill -0 "$(cat $p.pid)" 2>/dev/null; then
      kill "$(cat $p.pid)" && echo "stopped $p"
    fi
    rm -f $p.pid
  done
}

url() { grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' tunnel.log 2>/dev/null | tail -1; }

case "${1:-start}" in
  stop)
    stop
    exit 0
    ;;
  status)
    for p in server tunnel; do
      if [[ -f $p.pid ]] && kill -0 "$(cat $p.pid)" 2>/dev/null; then echo "$p: running"; else echo "$p: stopped"; fi
    done
    if [[ -f tunnel.pid ]] && kill -0 "$(cat tunnel.pid)" 2>/dev/null; then echo "url: $(url)/mcp"; fi
    exit 0
    ;;
  start) ;;
  *)
    echo "usage: $0 [start|stop|status]" >&2
    exit 2
    ;;
esac

# ---------- what a bare machine is missing ----------

have_certs() {
  [[ -s /etc/ssl/certs/ca-certificates.crt || -s /etc/pki/tls/certs/ca-bundle.crt || -s /etc/ssl/cert.pem ]]
}

need=""
command -v python3 >/dev/null || need="$need python3"
command -v curl >/dev/null || need="$need curl"
command -v tar >/dev/null || need="$need tar"
have_certs || need="$need ca-certificates"

if [[ -n $need ]]; then
  if [[ $(id -u) -eq 0 ]]; then
    SUDO=""
  elif command -v sudo >/dev/null; then
    SUDO="sudo"
  else
    echo "missing:$need, and this isn't root and there is no sudo to install them" >&2
    exit 1
  fi
  echo "installing:$need"
  if command -v apt-get >/dev/null; then
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get update -qq < /dev/null
    # shellcheck disable=SC2086
    $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends $need < /dev/null >/dev/null
  elif command -v dnf >/dev/null; then
    # Amazon Linux 2023 ships curl-minimal, which conflicts with the curl package; either one does here.
    # shellcheck disable=SC2086
    $SUDO dnf install -y -q --allowerasing $need < /dev/null >/dev/null
  elif command -v yum >/dev/null; then
    # shellcheck disable=SC2086
    $SUDO yum install -y -q $need < /dev/null >/dev/null
  else
    echo "missing:$need, and there is no apt-get, dnf or yum here to install them" >&2
    exit 1
  fi
fi

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 7))'; then
  echo "server.py needs Python 3.7 or later; this is $(python3 --version 2>&1)" >&2
  exit 1
fi

# ---------- the server, from this checkout or from the repo ----------

if [[ -n $HERE && -f $HERE/server.py ]]; then
  SERVER="$HERE/server.py"
  SELF="$HERE/start.sh"
else
  for f in server.py client.py start.sh; do
    if curl -fsSL -o "$f.new" "$RAW/$f"; then
      mv "$f.new" "$f"
    else
      rm -f "$f.new"
      [[ -f $f ]] || { echo "could not download $RAW/$f" >&2; exit 1; }
      echo "could not refresh $f; using the copy from last time" >&2
    fi
  done
  SERVER="$HOME_DIR/server.py"
  SELF="$HOME_DIR/start.sh"
fi

stop >/dev/null

if [[ ! -s token ]]; then
  (umask 077 && python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > token)
fi
chmod 600 token

CF="$(command -v cloudflared || true)"
if [[ -z $CF ]]; then
  CF="$HOME_DIR/cloudflared"
  if [[ ! -x $CF ]]; then
    case "$(uname -m)" in
      x86_64 | amd64) arch=amd64 ;;
      aarch64 | arm64) arch=arm64 ;;
      armv7l | armv6l) arch=arm ;;
      *) echo "no cloudflared build for $(uname -m)" >&2; exit 1 ;;
    esac
    echo "downloading cloudflared ($arch)…"
    curl -fsSL -o "$CF.new" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch"
    chmod +x "$CF.new"
    mv "$CF.new" "$CF"
  fi
fi

REMOTE_COMPUTE_HOME="$HOME_DIR" nohup python3 "$SERVER" --port "$PORT" --token-file "$HOME_DIR/token" --cwd "$WORKDIR" \
  > server.log 2>&1 < /dev/null &
echo $! > server.pid
up=""
for _ in $(seq 20); do
  kill -0 "$(cat server.pid)" 2>/dev/null || break
  if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/health" 2>/dev/null; then up=1; break; fi
  sleep 0.5
done
if [[ -z $up ]]; then
  echo "the server did not come up on 127.0.0.1:$PORT:" >&2
  cat server.log >&2
  stop >/dev/null
  exit 1
fi

: > tunnel.log
nohup "$CF" tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" > tunnel.log 2>&1 < /dev/null &
echo $! > tunnel.pid

# A URL is handed out before the tunnel reaches Cloudflare; it only works once a connection is registered.
connected() { [[ -n $(url) ]] && grep -q 'Registered tunnel connection' tunnel.log; }
for _ in $(seq 60); do
  connected && break
  kill -0 "$(cat tunnel.pid)" 2>/dev/null || break
  sleep 1
done
if ! connected; then
  echo "the tunnel did not come up; see $HOME_DIR/tunnel.log" >&2
  tail -20 tunnel.log >&2
  stop >/dev/null
  exit 1
fi

cat <<EOF

MCP server is up. Give Claude these two lines:

  REMOTE_COMPUTE_URL=$(url)/mcp
  REMOTE_COMPUTE_TOKEN=$(cat token)

Anyone with the token has a shell here as $(id -un). Stop with: bash $SELF stop
Logs: $HOME_DIR/server.log, $HOME_DIR/tunnel.log, jobs in $HOME_DIR/jobs/
EOF
