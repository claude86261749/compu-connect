# The relay

A small public machine that lends your domain to the box at home. Claude connects to `https://mcp.example.com/mcp`;
the relay's web server (Caddy, with a Let's Encrypt certificate it gets and renews itself) passes `/mcp` and `/health`
to `127.0.0.1:8765`, and that port is the far end of an SSH tunnel the box opened from inside its own network. The box
needs no open port and no fixed address.

`startup.sh` is the whole relay. It runs nothing of Remote Compute and stores no token. It does see every request on
its way through, tokens included, because the certificate lives here: whoever is root on the relay can use the box as
any connected client can. Keep it as bare as it is.

## On Google Cloud

An `e2-micro` is plenty. Google's free tier has covered one in `us-west1`, `us-central1` or `us-east1`; check the
current terms before you count on it, the external address and outbound traffic in particular.

You need the box's tunnel key first. It is made when the module is switched on:

```sh
cat /var/lib/mcp-tunnel/id_ed25519.pub > tunnel.pub      # on the box, as root
```

Then, wherever `gcloud` is logged in to your project, from a checkout of this repository:

```sh
DOMAIN=mcp.example.com
REGION=us-central1
ZONE=us-central1-a

gcloud compute addresses create mcp-relay --region "$REGION"
gcloud compute addresses describe mcp-relay --region "$REGION" --format='value(address)'
```

Point an A record for `$DOMAIN` at that address, with whoever serves your domain's DNS. The certificate can only be
issued once the name resolves to the relay.

```sh
gcloud compute firewall-rules create mcp-relay --target-tags mcp-relay \
  --allow tcp:22,tcp:80,tcp:443

gcloud compute instances create mcp-relay --zone "$ZONE" --machine-type e2-micro \
  --image-family debian-12 --image-project debian-cloud \
  --boot-disk-size 10GB --boot-disk-type pd-standard \
  --address mcp-relay --tags mcp-relay \
  --metadata mcp-domain="$DOMAIN" \
  --metadata-from-file startup-script=relay/startup.sh,mcp-tunnel-key=tunnel.pub
```

Port 80 is for the certificate authority's check and the redirect to HTTPS, 22 for the tunnel (and for you). After a
minute or two the script has finished, and says so on the serial console, together with the relay's SSH host key:

```sh
gcloud compute instances get-serial-port-output mcp-relay --zone "$ZONE" | grep mcp-relay:
```

Give the box the address and that host key (`services.compu-connect.relay.host` and `relay.hostPublicKey`), rebuild
it, and `mcpctl status` on the box should report the gateway up from outside.

To change the domain or the tunnel key later, change the metadata and give `mcp-revision` a new value: the relay
notices within a minute and runs the script again. Add `enable-guest-attributes=TRUE` to the metadata to have it
report its progress as the guest attribute `mcp/status`.

```sh
gcloud compute instances add-metadata mcp-relay --zone "$ZONE" \
  --metadata-from-file mcp-tunnel-key=tunnel.pub --metadata mcp-revision="$(date +%s)"
```

The script also takes the relay's SSH host key from the metadata (`mcp-host-key`), so that a relay made again keeps
the identity the box trusts: see the top of `startup.sh`.

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
