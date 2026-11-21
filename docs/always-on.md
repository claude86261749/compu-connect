# Always-on Remote Compute

`start.sh` gives one Claude session a shell for an afternoon: a throwaway address, one token, the account you happened
to be logged in as. This is the other way to run it: a machine that is always reachable at `https://mcp.example.com/mcp`,
with tokens you hand out and take back, and everything a token does confined to one unprivileged account.

```
Claude ──HTTPS──▶ relay: a small cloud machine with your domain and its certificate
                    │   an SSH tunnel, opened by the box from inside its network
                    ▼
the box ─────────────────────────────────────────────────────────────────────────────
  mcp-gateway          checks the token; runs nothing for a client; a different account
    │   a Unix socket per token, which only the gateway may open
    ▼
  mcp-session@ID       one systemd unit per token: the sandbox, as mcp-user
                       stopping the unit is how a token ends, and it kills all the token started

  mcpctl (root)        mints, lists and revokes tokens; shows who is connected
```

No Cloudflare is involved, and the box needs no open port and no fixed address.

## Setting it up

### 1. The box

The box runs NixOS. Import the module from a pinned copy of this repository and switch it on:

```nix
{ ... }:
let
  compu-connect = builtins.fetchTarball {
    url = "https://github.com/claude86261749/compu-connect/archive/<commit>.tar.gz";
    sha256 = "<hash>"; # nix-prefetch-url --unpack <url>
  };
in
{
  imports = [ "${compu-connect}/nix/module.nix" ];

  services.compu-connect = {
    enable = true;
    domain = "mcp.example.com";
    workDirs = [ "/data/mcp-user" ]; # more room than the home directory, if you have a disk for it
  };
}
```

After `nixos-rebuild switch` the account, the gateway and `mcpctl` exist, and so does the key the relay will know the
box by: `/var/lib/mcp-tunnel/id_ed25519.pub`. Nothing outside can reach the gateway yet. To try it from the box itself
before there is a relay, set `localPort = 8765` for the time being and use `http://127.0.0.1:8765/mcp`.

### 2. The relay

On Google Cloud, one command, run again whenever you like:

```sh
relay/gcp-tunnel up --project my-project --domain mcp.example.com --tunnel-key tunnel.pub
```

with the key from step 1 as `tunnel.pub` (on the box itself it is found without `--tunnel-key`). It creates the
network, firewall, static address and instance, keeps its keys in `.tunnel/` (ignored by git), and prints the
settings for step 3; `relay/gcp-tunnel nix` prints them again. Point the domain's A record at the address it
prints, or pass `--dns-zone` and it does that too. Details, and other providers: [relay/README.md](../relay/README.md).

### 3. Connect them

```nix
services.compu-connect.relay = {
  host = "203.0.113.10";
  hostPublicKey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI...";
};
```

Rebuild, then:

```
# mcpctl status
mcp-gateway.service    active
mcp-sync.service       active
mcp-tunnel.service     active
gateway, on this host  up (HTTP 200)
gateway, from outside  up (HTTP 200)  https://mcp.example.com/mcp
0 active tokens, 0 sessions in the last 15m
```

### 4. Hand out a token

```
# mcpctl create --label laptop
Token vaac8b65 (laptop) is valid for 12h, until 2026-11-21 10:41 CET.

Give Claude these two lines. The token is not shown again and is not stored here:

  REMOTE_COMPUTE_URL=https://mcp.example.com/mcp
  REMOTE_COMPUTE_TOKEN=cc_vaac8b65_…

Whoever holds it has a shell here as mcp-user until then. Cut it off with: mcpctl revoke vaac8b65
```

## mcpctl

Root only: it writes the token store, and it starts and stops systemd units.

| command | what it does |
|---|---|
| `mcpctl create [--ttl 12h] [--label NAME]` | Mints a token. `--ttl` takes `90m`, `12h`, `3d`…; the default is 12 hours and nothing gets more than 7 days. `-q` prints only the token, `--json` everything. |
| `mcpctl tokens [--all]` | The active tokens: time left, last use, sessions, running processes and memory. `--all` adds expired and revoked ones, which are kept for 30 days. |
| `mcpctl sessions [--all]` | Who is connected: token, client, the address it comes from, calls made, last tool, and below that every token that still has processes running. `--all` includes sessions quiet for more than 15 minutes. |
| `mcpctl revoke ID\|LABEL… \| --all` | Cuts tokens off, now, and kills everything they started. |
| `mcpctl status` | Whether the services, the tunnel and the public address are up. Exit status 1 if not. |

```
# mcpctl sessions
TOKEN     LABEL   SESSION   CLIENT             FROM          STARTED     LAST SEEN  CALLS  LAST TOOL  LEFT
inb3esfq  ci      (no id)   Python-urllib/3.13 198.51.100.7  9s ago      9s ago     1      spawn      11h 59m
vaac8b65  laptop  Zk3q9fTa  claude-code 2.1    203.0.113.7   3m 23s ago  12s ago    53     run        11h 56m

Running as mcp-user:
  inb3esfq (ci): 4 processes and threads, 13.1M of memory, 11h 59m left
  vaac8b65 (laptop): 7 processes and threads, 19.9M of memory, 11h 56m left

# mcpctl revoke laptop
revoked vaac8b65 (laptop): 1 session cut off, 7 processes and threads killed
```

What each request did is in the gateway's journal, one JSON line per call, with the command of every `run` and `spawn`
and the path of every file read or written:

```sh
journalctl -u mcp-gateway -o cat | grep '"token": "vaac8b65"'
```

**A token's end is the end of its processes.** When a token expires or is revoked, its unit is stopped, and systemd
kills every process in the unit's control group: background jobs, things that detached themselves, all of it. A job
that must run for 20 hours needs a token that lives 20 hours (`--ttl 1d`). The files it wrote stay.

## What a session is

Every token runs as the same account, `mcp-user` (uid 2000), which has no password, no login shell, is refused by
`sshd`, and is in no group but its own unless you add it to one for a device.

| | what a session gets |
|---|---|
| **Files** | Its own root directory, read-only and nearly empty: the programs you list in `packages` (as `/bin` and `/usr/bin`), the Nix store, a small `/etc`, and its own `/tmp`. Read-write: its home `/var/lib/mcp-user` and the `workDirs`. Nothing else of the machine exists for it: no `/home`, `/root`, `/etc/nixos`, `/var`, `/boot`, no other directory on your data disk. |
| **Processes** | It sees only its own, has no capabilities, and cannot gain any: set-user-id programs do nothing, namespaces are off, a system call filter leaves out ptrace, mounting, kernel modules, keyrings, io_uring and the like. |
| **Devices** | `null`, `zero`, `random`, terminals. Nothing else unless listed (below). `/sys` is visible and read-only, so `lspci` works. |
| **Network** | Out to the Internet over IPv4, and the machine's own loopback. Not the network the machine is in: every private, link-local and multicast range is blocked, and IPv6 is off. Its name servers are public ones. |
| **Resources** | All sessions together get at most 75 % of memory, 8192 processes and threads, and half the CPU weight of the rest of the system. No core dumps. |
| **Software** | What `packages` and `extraPackages` hold, and, with `nix.enable = true`, anything Nix can build: `nix build`, `nix develop`, `nix shell nixpkgs#…`, flakes. As everywhere on NixOS, programs built for other distributions do not run as they are. |

All of it is `services.compu-connect.*` options: see [nix/module.nix](../nix/module.nix). A change applies to
tokens made after the rebuild; running sessions are not restarted.

### Nix

With `services.compu-connect.nix.enable = true`, a session uses the machine's Nix daemon as an ordinary user, like
any other account on the machine: it cannot change the daemon's settings, turn the build sandbox off, or name its
own binary caches. What it builds runs under the daemon, as the daemon's build users, so the module puts the
sessions' network rule on the daemon too: no build reaches the home network either, while downloads from the
Internet and the binary cache work as before. `nix/test.nix` checks both.

What this changes for the rest of the machine:

- **Everyone's builds** get the same rule, yours included: nothing built here can fetch from the home network
  (a cache or a Git server on it, say), and the daemon uses IPv4 only.
- **DNS:** the daemon can no longer ask the router for names, so the machine resolves through systemd-resolved,
  which the module turns on. It asks the same servers as before.
- **Not inside a session's limits:** builds use the daemon's memory and CPU, not the sessions' share, and they
  go on when the token that asked for them ends. What they add to the store stays until it is garbage-collected.

### The home network

A session does not exist for the network the box is in, and that network does not exist for it. The kernel drops,
for every process of a session and for anything such a process starts:

- every packet to or from a private address (10/8, 172.16/12, 192.168/16, 100.64/10), a link-local one (169.254/16),
  multicast and broadcast: so no router, no NAS, no printer, no other computer, not by TCP, UDP or ping, and no
  discovering them by mDNS or SSDP either. The box's own address in that network is included;
- all of IPv6, because machines at home have public IPv6 addresses that no list of private ranges would cover;
- connections coming in from those addresses to anything a session listens on.

It cannot send raw frames, ask the router to open a port (the router is one of those addresses), or use the router as
its name server. This is a filter on the sessions' control group, not a firewall rule that depends on which
interface or address the box has, and `nix/test.nix` checks each of the above against a second machine.

What it does not cover:

- The sessions' traffic to the Internet passes through your router like the box's own, and comes from your address.
- Your home's public address, as seen from inside. If your router hands connections to its own public address back
  in to what it forwards, a session reaches the forwarded services that way, as anyone on the Internet does. With a
  fixed address, add it to `network.extraDeniedRanges`.
- A program on the box that listens on `127.0.0.1` and passes traffic on (a proxy, a VPN client's SOCKS port) would
  carry a session into your network. The box's loopback is the one thing sessions share with it.
- A network card handed to sessions as a raw PCI device (below) is outside all of this.

## Devices

By default a session has none. There are two ways to give it some, and they differ in what can go wrong.

**A device with a kernel driver** (a GPU, `/dev/kvm`, a capture card): list the device nodes, and the group that owns
them if there is one. The driver stays in charge of the hardware; the session gets the same interface any local user
has.

```nix
services.compu-connect.hardware = {
  devices = [ "char-nvidia rw" "char-nvidia-uvm rw" ];   # or "char-drm rw" for AMD and Intel, "/dev/kvm rw"
  groups = [ "render" ];
};
```

**A whole PCIe device, driven from user space** (a board under development, an FPGA, anything without a driver you
want in the kernel): list its address.

```nix
services.compu-connect.hardware.vfioPciDevices = [ "0000:03:00.0" ];
```

At boot the device is taken from its driver, bound to `vfio-pci`, and its VFIO group is given to the account. A
session then maps the device's registers, handles its interrupts and sets up its DMA itself, with no privilege:
[tests/vfio_edu.py](../tests/vfio_edu.py) is a complete small example. The point of going through VFIO instead of
`/dev/mem` or the `resource` files in `/sys` is the IOMMU: a PCIe device that can do DMA can read and write all of the
machine's memory, and the IOMMU is what confines it to the memory of the process driving it. So:

- The IOMMU must be on (AMD-Vi or VT-d, in the firmware setup). If it is not, the device is **not** handed out, and
  `mcpctl status` shows `mcp-vfio.service` as failed. There is no override.
- Devices that share an IOMMU group are not isolated from each other. If the device shares its group with anything
  but a bridge, it is not handed out unless you list the others too. `ls /sys/bus/pci/devices/0000:03:00.0/iommu_group/devices`
  shows the group.
- What the IOMMU does not cover is the device itself. A session can reprogram it, flash its firmware if the device
  lets software do that, and use whatever it is connected to: a network card handed over this way is on your network
  without any of the filtering above, a storage controller exposes its disks. List only devices where that is
  acceptable.

## What can go wrong, and what is done about it

| risk | what limits it | what remains |
|---|---|---|
| **A token leaks** (it sits in a session's environment, a log, a screenshot). | 12 hours by default, 7 days at most, and one command to revoke. The store holds only hashes. `mcpctl sessions` shows the addresses a token is used from. | Until it ends, the holder has what the token gives. |
| **A session misbehaves**: an agent that was misled, or someone with a leaked token. | The sandbox above: one unprivileged account, none of the machine's files, no route into your network, no privilege to gain, bounded memory and processes. Every command is in a log the account cannot touch. | It has the Internet and can send anything in its directories anywhere. It can fill its directories; on ext4 the 5 % reserved for root keeps the system itself running. A kernel bug reachable from an unprivileged process is still a kernel bug. |
| **Staying after the token ends**: a background process, a cron job, an SSH key. | Ending a token kills its control group, which a process cannot leave. The account cannot log in over SSH even with an `authorized_keys` of its own, has no cron, no systemd user instance. With no token active, nothing runs as the account. | Files. Whatever one token left in the home directory, the next token's session finds: a changed `.bashrc`, a poisoned build script. Tokens are doors into one room, not separate rooms. |
| **Sessions against each other.** | Nothing: they are the same account. | Give tokens to parties that could share an account anyway. |
| **Taking over the gateway**, to mint tokens or collect other people's. | It runs as another account and can only read the store. It listens on a Unix socket that systemd holds and only the tunnel may open; a session cannot talk to it or listen in its place. Tokens are minted by root, on the machine, and by nothing else. | — |
| **Your home network.** | Closed to sessions in both directions: see [The home network](#the-home-network). | Your public address from inside, and anything on the box's loopback that relays. |
| **The machine's own loopback.** A session shares it. | The gateway is not on it. | Anything else you serve on `127.0.0.1` without authentication (a database, a notebook, a model server) is reachable from a session. |
| **A device handed to sessions.** | See above: nothing by default, an allow-list, and the IOMMU for raw devices. | The device itself, and its driver's bugs. |
| **The relay is broken into.** It ends TLS, so it sees tokens pass. | It is one script, three packages and automatic updates. Tokens are short-lived. The tunnel reaches only the gateway, so the relay gives no way onto the box except with a token, into the sandbox. | Whoever owns the relay can use tokens that pass while they own it. |
| **The tunnel's key is stolen from the box.** | On the relay it can only listen on the relay's own `127.0.0.1:8765`: no shell, no other forwarding. | It could stand in for the gateway and see tokens. It is readable by root and one service account only. |
| **You, working in the account's directories.** | — | Treat them as untrusted: do not run `make`, git hooks or scripts from there as yourself or root. |

## Other systems

The module is the packaged form, and the machine it was written for runs NixOS. Nothing in the three Python files
(`gateway.py`, `server.py`, `mcpctl.py`, with `cclib.py`) is specific to it; on another distribution, the units in
`nix/module.nix` are the reference for what to write.

## Tests

```sh
python3 -m unittest discover -s tests     # gateway, worker and mcpctl, without systemd
nix-build nix/test.nix                    # everything above, for real, in two virtual machines
nix-build nix/test-vfio.nix               # a PCI device through VFIO, with and without an IOMMU
```

`nix/test.nix` boots a box and a relay, brings the tunnel up, and checks from outside what this page claims: what a
session can and cannot read, write, open and reach, that revoking or outliving a token kills what it started, that
the token is nowhere on disk or in the journal.
