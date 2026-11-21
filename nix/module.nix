# The always-on Remote Compute, as a NixOS module: see ../docs/always-on.md.
#
#   imports = [ "${compu-connect}/nix/module.nix" ];
#   services.compu-connect.enable = true;
#
# What it sets up:
#   mcp-gateway.socket     where the gateway listens: a Unix socket that only the tunnel may connect to
#   mcp-gateway.service    checks tokens; runs nothing for a client
#   mcp-session@ID.socket  one per token, opened by mcpctl; only the gateway may connect
#   mcp-session@ID.service that token's worker: the sandbox, as the account
#   mcp-sync.service       stops the worker of every token that ran out or was revoked
#   mcp-tunnel.service     the connection out to the relay, when one is configured
#   mcpctl                 the operator's command, for root
{
  config,
  lib,
  pkgs,
  ...
}:

let
  inherit (lib)
    concatMapStringsSep
    concatStringsSep
    literalExpression
    mkEnableOption
    mkIf
    mkOption
    optional
    optionalAttrs
    optionals
    types
    ;

  cfg = config.services.compu-connect;
  python = pkgs.python3;
  gatewaySocket = "/run/compu-connect/gateway.sock";
  home = cfg.home;
  vfio = cfg.hardware.vfioPciDevices;

  package = pkgs.runCommand "compu-connect" { } ''
    install -Dm444 ${../cclib.py} $out/libexec/compu-connect/cclib.py
    install -Dm444 ${../server.py} $out/libexec/compu-connect/server.py
    install -Dm444 ${../gateway.py} $out/libexec/compu-connect/gateway.py
    install -Dm444 ${../mcpctl.py} $out/libexec/compu-connect/mcpctl.py
  '';
  run = script: "${python}/bin/python3 -E -s -B ${package}/libexec/compu-connect/${script}";

  mcpctl = pkgs.writeShellScriptBin "mcpctl" ''
    PATH=${config.systemd.package}/bin:$PATH exec ${run "mcpctl.py"} "$@"
  '';

  settings = {
    tokens = "/var/lib/compu-connect/tokens.json";
    state = "/run/compu-connect/state/sessions.json";
    sockets = "/run/compu-connect/sessions";
    unit = "mcp-session@{id}";
    services = [
      "mcp-gateway.service"
      "mcp-sync.service"
    ]
    ++ optional (cfg.relay.host != null) "mcp-tunnel.service"
    ++ optional (vfio != [ ]) "mcp-vfio.service";
    systemd = true;
    gateway_socket = gatewaySocket;
    host = "127.0.0.1";
    port = cfg.localPort;
    public_url = if cfg.domain == null then null else "https://${cfg.domain}/mcp";
    default_ttl = cfg.tokens.defaultTtl;
    max_ttl = cfg.tokens.maxTtl;
    account = cfg.user;
    audit_commands = cfg.auditCommands;
  };

  # ---------- what a session sees instead of the machine's file system ----------

  tools = pkgs.buildEnv {
    name = "mcp-session-tools";
    paths = cfg.packages ++ cfg.extraPackages ++ optional cfg.nix.enable config.nix.package;
    pathsToLink = [
      "/bin"
      "/share"
    ];
    ignoreCollisions = true;
  };

  # Groups a session is in for its devices, named where NixOS gives them a fixed number.
  namedGroups = lib.filter (g: (config.users.groups.${g}.gid or null) != null) cfg.hardware.groups;

  etc = {
    passwd = ''
      root:x:0:0:root:/:/bin/false
      ${cfg.user}:x:${toString cfg.uid}:${toString cfg.gid}:MCP sessions:${home}:/bin/bash
      nobody:x:65534:65534:nobody:/:/bin/false
    '';
    group = ''
      root:x:0:
      ${cfg.group}:x:${toString cfg.gid}:
      nogroup:x:65534:
    ''
    + concatMapStringsSep "" (
      g: "${g}:x:${toString config.users.groups.${g}.gid}:${cfg.user}\n"
    ) namedGroups;
    "nsswitch.conf" = ''
      passwd: files
      group: files
      shadow: files
      hosts: files dns
      networks: files
      services: files
      protocols: files
    '';
    hosts = ''
      127.0.0.1 localhost
      ::1 localhost
    '';
    "nix.conf" = "experimental-features = nix-command flakes\n";
    "resolv.conf" = concatMapStringsSep "" (s: "nameserver ${s}\n") cfg.network.dns + "options edns0\n";
  };

  # The root directory of every session: read-only, in the Nix store, holding nothing of the machine. The store,
  # the account's own directories and the kernel's file systems are mounted into it.
  root = pkgs.runCommand "mcp-session-root" { } (
    ''
      mkdir -p $out/{dev,proc,sys,run,tmp,var/tmp,nix/store,usr,etc/ssl/certs,etc/pki/tls/certs,etc/nix}
      mkdir -p $out/nix/var/nix/daemon-socket
      ${concatMapStringsSep "\n" (d: "mkdir -p $out${d}") (
        [ home ] ++ cfg.workDirs ++ cfg.sandbox.readOnlyPaths
      )}
      ln -s ${tools}/bin $out/bin
      ln -s ${tools}/bin $out/usr/bin
      ln -s ${tools}/share $out/usr/share
      ln -s ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt $out/etc/ssl/certs/ca-certificates.crt
      ln -s ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt $out/etc/ssl/certs/ca-bundle.crt
      ln -s ${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt $out/etc/pki/tls/certs/ca-bundle.crt
      ln -s ${pkgs.tzdata}/share/zoneinfo $out/etc/zoneinfo
    ''
    + lib.optionalString (config.time.timeZone != null) ''
      ln -s ${pkgs.tzdata}/share/zoneinfo/${config.time.timeZone} $out/etc/localtime
    ''
    + concatStringsSep "" (
      lib.mapAttrsToList (name: text: ''
        cp ${pkgs.writeText "mcp-session-etc-${name}" text} $out/etc/${
          if name == "nix.conf" then "nix/nix.conf" else name
        }
      '') etc
    )
  );

  # Kept from sessions unless asked for: every private, link-local and multicast range, so that a session cannot
  # reach the router, the NAS or anything else on the network this machine sits in.
  privateRanges = [
    "0.0.0.0/8"
    "10.0.0.0/8"
    "100.64.0.0/10"
    "169.254.0.0/16"
    "172.16.0.0/12"
    "192.0.0.0/24"
    "192.168.0.0/16"
    "198.18.0.0/15"
    "224.0.0.0/4"
    "240.0.0.0/4"
  ];

  knownHosts = pkgs.writeText "mcp-relay-known-hosts" ''
    ${
      if cfg.relay.port == 22 then cfg.relay.host else "[${cfg.relay.host}]:${toString cfg.relay.port}"
    } ${cfg.relay.hostPublicKey}
  '';

  # For the services of this module that are not sessions: none of them needs more than this.
  hardened = {
    NoNewPrivileges = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    PrivateTmp = true;
    PrivateDevices = true;
    ProtectKernelTunables = true;
    ProtectKernelModules = true;
    ProtectKernelLogs = true;
    ProtectControlGroups = true;
    ProtectClock = true;
    ProtectHostname = true;
    RestrictNamespaces = true;
    RestrictRealtime = true;
    RestrictSUIDSGID = true;
    LockPersonality = true;
    MemoryDenyWriteExecute = true;
    SystemCallArchitectures = "native";
    CapabilityBoundingSet = "";
    UMask = "0077";
  };
in
{
  options.services.compu-connect = {
    enable = mkEnableOption "the always-on Remote Compute MCP server";

    domain = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "mcp.example.com";
      description = ''
        The name the relay serves this machine under. It is only used to print the URL with a new token and to
        check, in `mcpctl status`, that the machine can be reached from outside.
      '';
    };

    localPort = mkOption {
      type = types.nullOr types.port;
      default = null;
      example = 8765;
      description = ''
        Also listen on this port of 127.0.0.1, to use the gateway from this machine without a relay. Leave it off
        otherwise: the gateway then has no port at all, only a Unix socket for the tunnel, and with a port every
        account on the machine, the sessions included, can talk to it.
      '';
    };

    user = mkOption {
      type = types.str;
      default = "mcp-user";
      description = "The account every session runs as. It has no password, no login shell and may not log in over SSH.";
    };

    group = mkOption {
      type = types.str;
      default = "mcp-user";
      description = "The account's group.";
    };

    uid = mkOption {
      type = types.int;
      default = 2000;
      description = "The account's user id: fixed, because the sessions' own /etc/passwd is built with it.";
    };

    gid = mkOption {
      type = types.int;
      default = 2000;
      description = "The account's group id.";
    };

    home = mkOption {
      type = types.path;
      default = "/var/lib/mcp-user";
      description = "The account's home directory: the one place, with {option}`workDirs`, a session can write to and keep.";
    };

    workDirs = mkOption {
      type = types.listOf types.path;
      default = [ ];
      example = [ "/data/mcp-user" ];
      description = ''
        More directories a session can read and write, at the same path as on the machine. One that does not exist
        when a session starts is left out of it, so a disk that failed to mount does not stop sessions.
      '';
    };

    createWorkDirs = mkOption {
      type = types.bool;
      default = true;
      description = ''
        Create {option}`workDirs`, owned by the account. Turn it off for a directory on a file system that may be
        missing at boot, and create it when the file system is mounted instead.
      '';
    };

    packages = mkOption {
      type = types.listOf types.package;
      default = with pkgs; [
        bashInteractive
        coreutils
        findutils
        gnugrep
        gnused
        gawk
        diffutils
        patch
        gnutar
        gzip
        bzip2
        xz
        zstd
        unzip
        curl
        git
        openssh
        python3
        gnumake
        gcc
        less
        which
        file
        procps
        util-linux
        pciutils
        jq
        rsync
      ];
      defaultText = literalExpression "a shell, the usual Unix tools, git, curl, python3, gcc, make, pciutils";
      description = ''
        The programs a session has, as /bin and /usr/bin. A session cannot use Nix to get more: it has no access
        to the Nix daemon.
      '';
    };

    extraPackages = mkOption {
      type = types.listOf types.package;
      default = [ ];
      example = literalExpression "[ pkgs.rustc pkgs.cargo ]";
      description = "Programs a session has on top of {option}`packages`.";
    };

    nix = {
      enable = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Let sessions use Nix: `nix build`, `nix develop`, `nix shell`, flakes. They talk to the machine's Nix
          daemon as an ordinary, untrusted user. Builds then run under the daemon, not in the session, so with
          this on the daemon and its builds are kept off the private network ranges too (when
          {option}`network.blockPrivateRanges` is on), and the machine resolves names through systemd-resolved,
          because the daemon can then no longer ask a name server on the home network. Builds are not counted
          against the sessions' memory and CPU limits, and what they add to the store stays until it is
          garbage-collected.
        '';
      };
    };

    auditCommands = mkOption {
      type = types.bool;
      default = true;
      description = ''
        Write the command of every `run` and `spawn`, and the path of every file read or written, to the gateway's
        journal, which the account cannot read or change. Without it only the tool's name is logged.
      '';
    };

    tokens = {
      defaultTtl = mkOption {
        type = types.str;
        default = "12h";
        description = "How long a token from `mcpctl create` lives when --ttl is not given.";
      };
      maxTtl = mkOption {
        type = types.str;
        default = "7d";
        description = "The longest a token may live. More than 7 days is not possible.";
      };
    };

    network = {
      blockPrivateRanges = mkOption {
        type = types.bool;
        default = true;
        description = ''
          Keep sessions off every private, link-local and multicast IPv4 range, so they reach the Internet and
          this machine's own loopback but not the network the machine sits in.
        '';
      };
      allowedRanges = mkOption {
        type = types.listOf types.str;
        default = [ ];
        example = [ "192.168.1.20/32" ];
        description = "Addresses inside the blocked ranges that sessions may reach all the same.";
      };
      extraDeniedRanges = mkOption {
        type = types.listOf types.str;
        default = [ ];
        example = [ "198.51.100.23/32" ];
        description = ''
          More addresses to keep sessions off: for instance your home's public address, if it is a fixed one,
          through which a router may hand connections back in to what it forwards.
        '';
      };
      ipv6 = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Let sessions use IPv6. It is off because the machines of a home network have public IPv6 addresses, which
          no list of private ranges covers.
        '';
      };
      dns = mkOption {
        type = types.listOf types.str;
        default = [
          "1.1.1.1"
          "9.9.9.9"
        ];
        description = ''
          The name servers of a session. The machine's own are usually the router, which sessions cannot reach.
        '';
      };
    };

    limits = {
      memoryMax = mkOption {
        type = types.str;
        default = "75%";
        description = "The memory all sessions together may use (systemd MemoryMax= of mcp.slice).";
      };
      tasksMax = mkOption {
        type = types.int;
        default = 8192;
        description = "The processes and threads all sessions together may have.";
      };
      cpuWeight = mkOption {
        type = types.int;
        default = 50;
        description = "The sessions' share of a busy CPU, against 100 for everything else on the machine.";
      };
      memlock = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "infinity";
        description = ''
          How much memory a session's process may lock or have pinned (systemd LimitMEMLOCK=). GPU compute
          drivers pin the memory they share with the card. What is pinned still counts against {option}`memoryMax`.
        '';
      };
      sessionMemoryMax = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "16G";
        description = "The memory one token's processes may use.";
      };
    };

    hardware = {
      devices = mkOption {
        type = types.listOf types.str;
        default = [ ];
        example = [
          "/dev/kvm rw"
          "char-drm rw"
          "char-nvidia rw"
          "char-nvidia-uvm rw"
        ];
        description = ''
          Device nodes a session may open, as systemd DeviceAllow= entries. Everything else in /dev, apart from
          null, zero, random and terminals, is closed to it. This is the way to hand out a device that has a
          kernel driver, such as a GPU.
        '';
      };
      groups = mkOption {
        type = types.listOf types.str;
        default = [ ];
        example = [
          "render"
          "kvm"
        ];
        description = "Groups the sessions are in, for device nodes that a group owns.";
      };
      vfioPciDevices = mkOption {
        type = types.listOf (types.strMatching "[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\\.[0-7]");
        default = [ ];
        example = [ "0000:03:00.0" ];
        description = ''
          PCI devices handed to sessions whole, through VFIO: a session maps their registers and drives them from
          user space, with no kernel driver. The IOMMU confines what the device can reach to the memory of the
          process driving it, so the machine must have one enabled (AMD-Vi or VT-d, in the firmware); without it
          the devices are not handed out. Every device that shares an IOMMU group with a listed one must be listed
          too. Do not list a device whose misuse you could not accept: a session can reprogram it, flash it where
          the device allows that, and use whatever it is wired to.
        '';
      };
    };

    sandbox = {
      readOnlyPaths = mkOption {
        type = types.listOf types.path;
        default = optional config.hardware.graphics.enable "/run/opengl-driver";
        defaultText = literalExpression ''[ "/run/opengl-driver" ] if hardware.graphics.enable'';
        description = "Directories of the machine a session can read, at the same path. The Nix store always is.";
      };
      allowDebugging = mkOption {
        type = types.bool;
        default = false;
        description = "Allow ptrace and perf, for gdb and strace.";
      };
      allowUserNamespaces = mkOption {
        type = types.bool;
        default = false;
        description = ''
          Allow sessions to create namespaces, which rootless containers need. Most local privilege escalations
          of recent years start there.
        '';
      };
    };

    relay = {
      host = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "203.0.113.10";
        description = ''
          The relay to connect out to: its address or name. While it is null there is no tunnel, and the gateway
          can only be reached on this machine.
        '';
      };
      port = mkOption {
        type = types.port;
        default = 22;
        description = "The relay's SSH port.";
      };
      user = mkOption {
        type = types.str;
        default = "tunnel";
        description = "The account on the relay that may do nothing but hold the tunnel.";
      };
      hostPublicKey = mkOption {
        type = types.nullOr types.str;
        default = null;
        example = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI...";
        description = ''
          The relay's SSH host key. With null, the key seen on the first connection is kept and required from
          then on.
        '';
      };
      remotePort = mkOption {
        type = types.port;
        default = 8765;
        description = "The port on the relay's loopback that leads here; its web server forwards to it.";
      };
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = lib.all (
          d:
          !(lib.any (p: d == p || lib.hasPrefix "${p}/" d) [
            "/nix"
            "/proc"
            "/sys"
            "/dev"
            "/run"
            "/etc"
            "/tmp"
            "/bin"
            "/usr"
          ])
          && d != "/"
        ) ([ home ] ++ cfg.workDirs);
        message = "services.compu-connect: home and workDirs cannot be / or lie under /nix, /proc, /sys, /dev, /run, /etc, /tmp, /bin or /usr.";
      }
      {
        assertion = cfg.user != "root" && cfg.uid != 0 && cfg.gid != 0;
        message = "services.compu-connect: the session account cannot be root.";
      }
    ];

    environment.systemPackages = [ mcpctl ];
    environment.etc."compu-connect/config.json".text = builtins.toJSON settings;

    users.groups.${cfg.group}.gid = cfg.gid;
    users.users.${cfg.user} = {
      isSystemUser = true;
      uid = cfg.uid;
      group = cfg.group;
      home = home;
      description = "MCP sessions";
      shell = "${pkgs.shadow}/bin/nologin";
    };
    users.groups.mcp-gateway = { };
    users.users.mcp-gateway = {
      isSystemUser = true;
      group = "mcp-gateway";
      description = "MCP gateway";
    };
    users.groups.mcp-tunnel = { };
    users.users.mcp-tunnel = {
      isSystemUser = true;
      group = "mcp-tunnel";
      home = "/var/lib/mcp-tunnel";
      description = "MCP tunnel to the relay";
    };

    # A session can write to its home, so it could leave itself an authorized_keys there.
    services.openssh.settings.DenyUsers = [ cfg.user ];

    systemd.tmpfiles.rules = [
      "d /var/lib/compu-connect 0750 root mcp-gateway -"
      "d /run/compu-connect 0755 root root -"
      "d /run/compu-connect/sessions 0750 root mcp-gateway -"
      "d /run/compu-connect/state 0750 mcp-gateway mcp-gateway -"
      "d ${home} 0700 ${cfg.user} ${cfg.group} -"
    ]
    ++ optionals cfg.createWorkDirs (map (d: "d ${d} 0700 ${cfg.user} ${cfg.group} -") cfg.workDirs);

    systemd.slices.mcp = {
      description = "Everything MCP sessions run";
      sliceConfig = {
        MemoryMax = cfg.limits.memoryMax;
        TasksMax = cfg.limits.tasksMax;
        CPUWeight = cfg.limits.cpuWeight;
        IOWeight = cfg.limits.cpuWeight;
      };
    };

    # systemd holds the gateway's sockets, so they stay open and in the same hands while the gateway restarts:
    # nothing else on the machine gets a moment to listen in its place and collect the tokens that arrive.
    systemd.sockets.mcp-gateway = {
      description = "MCP gateway";
      wantedBy = [ "sockets.target" ];
      listenStreams = [
        gatewaySocket
      ]
      ++ optional (cfg.localPort != null) "127.0.0.1:${toString cfg.localPort}";
      socketConfig = {
        SocketUser = "root";
        SocketGroup = "mcp-tunnel";
        SocketMode = "0660";
      };
    };

    systemd.services.mcp-gateway = {
      description = "MCP gateway: checks tokens and hands requests to the sessions";
      wantedBy = [ "multi-user.target" ];
      requires = [ "mcp-gateway.socket" ];
      after = [
        "mcp-gateway.socket"
        "systemd-tmpfiles-setup.service"
      ];
      restartTriggers = [ config.environment.etc."compu-connect/config.json".source ];
      serviceConfig = hardened // {
        ExecStart = "${run "gateway.py"} --systemd-socket";
        User = "mcp-gateway";
        Group = "mcp-gateway";
        Restart = "always";
        RestartSec = 2;
        ReadWritePaths = [ "/run/compu-connect/state" ];
        ProtectProc = "invisible";
        ProcSubset = "pid";
        # It talks to the tunnel and to the sessions over Unix sockets, and to nothing else.
        RestrictAddressFamilies = [ "AF_UNIX" ] ++ optional (cfg.localPort != null) "AF_INET";
        IPAddressDeny = "any";
        IPAddressAllow = optional (cfg.localPort != null) "localhost";
        SystemCallFilter = [
          "@system-service"
          "~@privileged @resources"
        ];
        MemoryMax = "512M";
        TasksMax = 512;
      };
    };

    systemd.sockets."mcp-session@" = {
      description = "MCP session socket of token %i";
      socketConfig = {
        ListenStream = "/run/compu-connect/sessions/%i.sock";
        SocketUser = "root";
        SocketGroup = "mcp-gateway";
        SocketMode = "0660";
        DirectoryMode = "0750";
        RemoveOnStop = true;
      };
    };

    systemd.services."mcp-session@" = {
      description = "MCP session of token %i";
      after = optional (vfio != [ ]) "mcp-vfio.service";
      # Rebuilding the system must not kill what sessions are running; a new configuration applies to new tokens.
      restartIfChanged = false;
      stopIfChanged = false;
      unitConfig.CollectMode = "inactive-or-failed";
      environment = {
        HOME = home;
        SHELL = "/bin/bash";
        PATH = lib.mkForce "${home}/.local/bin:/bin";
        LANG = "C.UTF-8";
        REMOTE_COMPUTE_HOME = "${home}/.remote-compute";
        SSL_CERT_FILE = "/etc/ssl/certs/ca-certificates.crt";
        NIX_SSL_CERT_FILE = "/etc/ssl/certs/ca-certificates.crt";
      }
      // optionalAttrs cfg.nix.enable { NIX_REMOTE = "daemon"; };
      serviceConfig = {
        ExecStart = "${run "server.py"} --systemd-socket --cwd ${home}";
        User = cfg.user;
        Group = cfg.group;
        SupplementaryGroups = cfg.hardware.groups;
        Slice = "mcp.slice";
        WorkingDirectory = home;
        UMask = "0077";

        # Stopping the unit is how a token is cut off: everything it started is in this control group and dies
        # with it. One process running out of memory does not take the rest of the session along.
        KillMode = "control-group";
        TimeoutStopSec = 5;
        OOMPolicy = "continue";
        Restart = "no";

        # The file system: an empty root from the store, the store itself, and the account's own directories.
        RootDirectory = root;
        MountAPIVFS = true;
        BindReadOnlyPaths = [
          "/nix/store"
        ]
        ++ optional cfg.nix.enable "/nix/var/nix/daemon-socket"
        ++ map (p: "-${p}") cfg.sandbox.readOnlyPaths;
        BindPaths = [ home ] ++ map (d: "-${d}") cfg.workDirs;
        TemporaryFileSystem = [ "/dev/shm:rw,nosuid,nodev,mode=1777" ];
        PrivateTmp = true;
        ProtectProc = "invisible";
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectClock = true;
        ProtectHostname = true;
        PrivateIPC = true;
        KeyringMode = "private";

        # Privileges: none, and no way to gain any. Set-user-id programs do nothing in here.
        NoNewPrivileges = true;
        CapabilityBoundingSet = "";
        AmbientCapabilities = "";
        RestrictSUIDSGID = true;
        RestrictRealtime = true;
        LockPersonality = true;
        RestrictNamespaces = !cfg.sandbox.allowUserNamespaces;
        SystemCallArchitectures = "native";
        SystemCallFilter = [
          "@system-service"
          "@sandbox"
          "@pkey"
        ]
        ++ optional cfg.sandbox.allowDebugging "@debug"
        ++ [ "~@keyring io_uring_setup io_uring_enter io_uring_register userfaultfd" ];
        SystemCallErrorNumber = "ENOSYS";
        LimitCORE = 0;

        # Devices: the harmless ones, and those listed.
        DevicePolicy = "closed";
        DeviceAllow =
          cfg.hardware.devices
          ++ optionals (vfio != [ ]) [
            "/dev/vfio/vfio rw"
            "char-vfio rw"
          ];

        # The network.
        RestrictAddressFamilies = [
          "AF_UNIX"
          "AF_INET"
          "AF_NETLINK"
        ]
        ++ optional cfg.network.ipv6 "AF_INET6";
        IPAddressDeny =
          optionals cfg.network.blockPrivateRanges privateRanges
          ++ cfg.network.extraDeniedRanges
          ++ optionals (!cfg.network.ipv6) [ "::/0" ]
          ++ optionals (cfg.network.ipv6 && cfg.network.blockPrivateRanges) [
            "fc00::/7"
            "fe80::/10"
            "ff00::/8"
          ];
        IPAddressAllow = [ "localhost" ] ++ cfg.network.allowedRanges;
      }
      // optionalAttrs (cfg.limits.sessionMemoryMax != null) { MemoryMax = cfg.limits.sessionMemoryMax; }
      # VFIO pins the memory a device may reach.
      // optionalAttrs (vfio != [ ]) { LimitMEMLOCK = "infinity"; }
      // optionalAttrs (cfg.limits.memlock != null) { LimitMEMLOCK = cfg.limits.memlock; };
    };

    systemd.services.mcp-sync = {
      description = "MCP sessions: stop the worker of every token that expired or was revoked";
      wantedBy = [ "multi-user.target" ];
      after = [ "systemd-tmpfiles-setup.service" ] ++ optional (vfio != [ ]) "mcp-vfio.service";
      serviceConfig = {
        ExecStart = "${mcpctl}/bin/mcpctl sync --watch";
        Restart = "always";
        RestartSec = 2;
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        ReadWritePaths = [ "/var/lib/compu-connect" ];
      };
    };

    # The tunnel's key is made as soon as the module is on, so that it can be given to the relay before the
    # relay's address is known here.
    systemd.services.mcp-tunnel-key = {
      description = "MCP tunnel: make the key the relay will know this machine by";
      wantedBy = [ "multi-user.target" ];
      serviceConfig = hardened // {
        Type = "oneshot";
        RemainAfterExit = true;
        User = "mcp-tunnel";
        Group = "mcp-tunnel";
        StateDirectory = "mcp-tunnel";
        StateDirectoryMode = "0700";
        ExecStart = pkgs.writeShellScript "mcp-tunnel-key" ''
          [ -s /var/lib/mcp-tunnel/id_ed25519 ] || ${pkgs.openssh}/bin/ssh-keygen -q -t ed25519 -N "" \
            -C "mcp-tunnel@${config.networking.hostName}" -f /var/lib/mcp-tunnel/id_ed25519
        '';
      };
    };

    systemd.services.mcp-tunnel = mkIf (cfg.relay.host != null) {
      description = "MCP tunnel: the relay's way in to the gateway";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      requires = [ "mcp-tunnel-key.service" ];
      after = [
        "network-online.target"
        "mcp-tunnel-key.service"
        "mcp-gateway.socket"
      ];
      # It never gives up: the relay or the line being down for a day must not need anyone to come and restart it.
      startLimitIntervalSec = 0;
      serviceConfig = hardened // {
        User = "mcp-tunnel";
        Group = "mcp-tunnel";
        StateDirectory = "mcp-tunnel";
        StateDirectoryMode = "0700";
        Restart = "always";
        RestartSec = 5;
        RestrictAddressFamilies = [
          "AF_UNIX"
          "AF_INET"
          "AF_INET6"
        ];
        ExecStart = concatStringsSep " " (
          [
            "${pkgs.openssh}/bin/ssh -NT -F none"
            "-i /var/lib/mcp-tunnel/id_ed25519"
            "-o IdentitiesOnly=yes -o BatchMode=yes"
            "-o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3"
            "-o ConnectTimeout=20 -o TCPKeepAlive=yes"
          ]
          ++ (
            if cfg.relay.hostPublicKey != null then
              [ "-o StrictHostKeyChecking=yes -o UserKnownHostsFile=${knownHosts}" ]
            else
              [ "-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/var/lib/mcp-tunnel/known_hosts" ]
          )
          ++ [
            "-p ${toString cfg.relay.port}"
            "-R 127.0.0.1:${toString cfg.relay.remotePort}:${gatewaySocket}"
            "${cfg.relay.user}@${cfg.relay.host}"
          ]
        );
      };
    };

    # ---------- Nix for sessions ----------

    # What a session builds runs here, under the daemon: so the daemon gets the sessions' network rule. The
    # daemon's own downloads come from the Internet and are not affected.
    systemd.services.nix-daemon.serviceConfig =
      mkIf (cfg.nix.enable && cfg.network.blockPrivateRanges)
        {
          IPAddressDeny = privateRanges ++ cfg.network.extraDeniedRanges ++ [ "::/0" ];
          IPAddressAllow = [ "localhost" ] ++ cfg.network.allowedRanges;
        };
    # With that rule the daemon cannot ask the router for names; the resolver on the loopback asks for it.
    services.resolved.enable = mkIf (cfg.nix.enable && cfg.network.blockPrivateRanges) (
      lib.mkDefault true
    );

    # ---------- PCI devices through VFIO ----------

    boot.kernelModules = mkIf (vfio != [ ]) [
      "vfio_pci"
      "vfio_iommu_type1"
    ];
    boot.kernelParams = mkIf (vfio != [ ]) [
      "intel_iommu=on"
      "amd_iommu=on"
    ];

    systemd.services.mcp-vfio = mkIf (vfio != [ ]) {
      description = "MCP sessions: hand the listed PCI devices over through VFIO";
      wantedBy = [ "multi-user.target" ];
      after = [ "systemd-modules-load.service" ];
      path = [
        pkgs.coreutils
        pkgs.kmod
      ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      script = ''
        set -u
        shopt -s nullglob
        listed=" ${concatStringsSep " " vfio} "
        groups=(/sys/kernel/iommu_groups/*)
        if [ ''${#groups[@]} -eq 0 ]; then
          echo "This machine has no IOMMU enabled (AMD-Vi or VT-d, in the firmware setup)." >&2
          echo "Without one a PCI device can read and write all memory, so none is handed out." >&2
          exit 1
        fi
        modprobe vfio-pci
        failed=0
        for addr in $listed; do
          dev=/sys/bus/pci/devices/$addr
          if [ ! -e "$dev" ]; then
            echo "$addr: there is no such PCI device" >&2; failed=1; continue
          fi
          if [ ! -e "$dev/iommu_group" ]; then
            echo "$addr: the device is in no IOMMU group; not handing it out" >&2; failed=1; continue
          fi
          group=$(basename "$(readlink -f "$dev/iommu_group")")
          # Devices in one group are not isolated from each other: a session that has one has all of them.
          alone=1
          for peer in "$dev"/iommu_group/devices/*; do
            p=$(basename "$peer")
            case "$listed" in *" $p "*) continue ;; esac
            # A bridge above the device belongs to the kernel and stays with it.
            case "$(cat "$peer/class")" in 0x0604*) continue ;; esac
            echo "$addr: shares IOMMU group $group with $p, which is not listed; not handing it out" >&2
            alone=0
          done
          if [ $alone -eq 0 ]; then failed=1; continue; fi
          echo vfio-pci > "$dev/driver_override"
          if [ -e "$dev/driver" ] && [ "$(basename "$(readlink -f "$dev/driver")")" != vfio-pci ]; then
            echo "$addr" > "$dev/driver/unbind"
          fi
          [ -e "$dev/driver" ] || echo "$addr" > /sys/bus/pci/drivers_probe
          if [ ! -e "/dev/vfio/$group" ]; then
            echo "$addr: vfio-pci did not take the device" >&2; failed=1; continue
          fi
          chown root:${cfg.group} "/dev/vfio/$group"
          chmod 0660 "/dev/vfio/$group"
          echo "$addr: handed to ${cfg.group} as /dev/vfio/$group"
        done
        exit $failed
      '';
    };
  };
}
