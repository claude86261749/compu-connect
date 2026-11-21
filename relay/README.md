# The relay

A small public machine that lends your domain to the box at home. Claude connects to `https://mcp.example.com/mcp`;
the relay's web server (Caddy, with a Let's Encrypt certificate it gets and renews itself) passes `/mcp` and `/health`
to `127.0.0.1:8765`, and that port is the far end of an SSH tunnel the box opened from inside its own network. The box
needs no open port and no fixed address.

`startup.sh` is the whole relay. It runs nothing of Remote Compute and stores no token. It does see every request on
its way through, tokens included, because the certificate lives here: whoever is root on the relay can use the box as
any connected client can. Keep it as bare as it is.

## On Google Cloud: `gcp-tunnel`

`relay/gcp-tunnel` sets up the relay with `gcloud`, and keeps it set up: `up` creates what is missing, changes what
differs from its settings, and leaves the rest alone, so it is safe to run as often as you like. Each subcommand
does one thing:

| command | what it does |
|---|---|
| `gcp-tunnel up [--project P] [--domain D] [...]` | Creates or updates everything, then waits until the relay has applied its settings. Remembers what it was given. `--dry-run` only says what it would change; `--recreate` makes the instance anew, keeping its address and identity. |
| `gcp-tunnel status` | What exists in the project, whether the settings are applied, and from outside: DNS, the certificate, and whether the box is connected. Exit status 1 if anything is missing. |
| `gcp-tunnel nix` | The box's settings for this relay (`relay.nix`). |
| `gcp-tunnel ssh [COMMAND]` | Logs in to the relay as its administrator, with a key and host key of its own. |
| `gcp-tunnel logs` | What the relay's startup script printed, from the serial console. |
| `gcp-tunnel down [--keep-address] [--yes]` | Deletes it all from the project. The files stay. |

It needs `gcloud`, logged in (`gcloud auth login`), a project with billing enabled, and `ssh`/`ssh-keygen`. On
NixOS without `gcloud` it fetches it with `nix shell` by itself.

### Files: `.tunnel/`

Everything it makes and needs is in `.tunnel/` at the top of this checkout, which git ignores. Keep it: it is what
makes `up` repeatable.

| file | what it is |
|---|---|
| `config.json` | the settings `up` was given |
| `host_ed25519` | the relay's SSH host key. The box trusts the relay by it, so a relay made again with `--recreate` or after `down` is still trusted. It is also in the instance's metadata, where anyone who can read the project's instances can read it. |
| `admin_ed25519` | the key `gcp-tunnel ssh` logs in with. Project-wide SSH keys and OS Login are off for the instance, so only this key gets an administrator in. |
| `tunnel_key.pub` | the box's tunnel key, from `--tunnel-key` or, when run on the box itself, from `/var/lib/mcp-tunnel/id_ed25519.pub` |
| `known_hosts` | the relay's host key, for `ssh` |
| `relay.nix` | `services.compu-connect.domain`, `relay.host` and `relay.hostPublicKey` for the box |

There is no TLS certificate among them: Caddy, on the relay, gets one from Let's Encrypt as soon as the domain
points at it, and renews it. Let's Encrypt issues at most five certificates a week for the same name, which only
matters if the relay is made anew more often than that.

### From nothing

```sh
relay/gcp-tunnel up --project my-project --domain mcp.example.com --tunnel-key tunnel.pub
```

where `tunnel.pub` is `/var/lib/mcp-tunnel/id_ed25519.pub` from the box (on the box itself, as root, it is found
without `--tunnel-key`). It then:

1. turns on the Compute Engine API;
2. makes a VPC network `mcp-relay` with two firewall rules: 80 and 443 from anywhere, 22 from `--ssh-from`
   (anywhere by default, since the box's own address may change);
3. reserves a static external address, so that the DNS record and the box's settings stay right;
4. with `--dns-zone ZONE`, sets the domain's A record in that Cloud DNS zone; otherwise it tells you what to set;
5. creates an `e2-micro` Debian instance with no service account, so the relay has no access to the project, and
   Shielded VM on. Its startup script is `relay/startup.sh`;
6. waits until the startup script reports that it has applied these exact settings;
7. writes `relay.nix` and checks from outside, saying what is left to do.

The defaults are the free-tier ones: `us-east1`, `e2-micro`, a 10 GB standard disk. Check Google's current terms
before you count on it being free, the external address in particular. `--region europe-north1` is closer to
Sweden but not in the free tier.

Afterwards, `gcp-tunnel up` with no arguments brings it back to the same state. A changed setting (a new tunnel
key, `--ssh-from`, `--acme-email`, a new version of `startup.sh`) is applied in place: the instance's metadata is
updated, a watcher on the relay runs the startup script again, and `up` waits for it. Nothing is restarted, and
the box's tunnel stays up.

Give the box what `gcp-tunnel nix` prints, rebuild it, and `gcp-tunnel status` should end with "the box is
connected".

## Anywhere else

Any Debian or Ubuntu machine with a public address, ports 22, 80 and 443 open, and the domain pointing at it:

```sh
MCP_DOMAIN=mcp.example.com MCP_TUNNEL_KEY="$(cat tunnel.pub)" bash startup.sh
```

## What the script sets up

- **Caddy** for the domain: `/mcp` and `/health` go to `127.0.0.1:8765`, everything else is 404, request bodies are
  capped at 48 MB. It adds the client's address as `X-Forwarded-For`, which is what `mcpctl sessions` shows.
- **An account, `tunnel`**, that can do one thing. Its key is in `/etc/ssh/mcp-tunnel.keys`, owned by root, and
  `sshd` gives it no shell, no terminal, no forwarding except listening on the relay's own `127.0.0.1:8765`. Someone
  who steals the tunnel key from the box can stand in for the box's gateway and nothing more.
- **Unattended security updates.**

`nix/test.nix` runs the same `sshd` and Caddy configuration between two virtual machines.
