# compu-connect

**Remote Compute**: a small MCP server that gives a Claude Code session (one that can only reach the web over
HTTPS) a shell on a machine you own. It can run commands, start long jobs and read their logs, and move files
both ways. It sits behind a Cloudflare quick tunnel.

On the machine (bare Ubuntu/Debian or Amazon Linux 2/2023 is fine):

```sh
curl -fsSL https://raw.githubusercontent.com/claude86261749/compu-connect/HEAD/start.sh | bash
```

or, from a checkout, `bash start.sh`. It installs python3, curl and tar if they're missing, downloads
cloudflared, starts the server and the tunnel, and prints two lines to hand to Claude:

```
REMOTE_COMPUTE_URL=https://<random>.trycloudflare.com/mcp
REMOTE_COMPUTE_TOKEN=<token>
```

`bash ~/.remote-compute/start.sh status` shows whether it's running; `… stop` stops it. If you ran it from a
checkout, use that checkout's `start.sh` instead.

## Safety

- Nothing secret is in this repo. The token is made on the machine at first start (32 random bytes), kept in
  `~/.remote-compute/token` with mode 600, and printed only to the terminal you ran `start.sh` in.
- The server listens on 127.0.0.1 only. The tunnel is the only way in, and every request has to carry
  `Authorization: Bearer <token>`. A wrong token gets 401, after a one-second delay.
- **Whoever has the token has a shell as the user who ran `start.sh`**, which means root if you ran it as root.
  Keep the token private and stop the server when you're done. To rotate it, delete `~/.remote-compute/token`
  and start again.
- The tunnel URL changes every time it starts. The token stays until you delete it.

## Files

| file | what it is |
|---|---|
| `start.sh` | starts, stops and reports on the server and the tunnel |
| `server.py` | the MCP server (Python 3.7+, standard library only): `run`, `spawn`, `job`, `jobs`, `kill`, `read_file`, `write_file`, `list_dir`, `info` |
| `client.py` | a command-line client for the same server. It reads the two lines from the environment or from `~/.remote-compute.env` |
