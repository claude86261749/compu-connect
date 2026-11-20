# Handing a PCI device to sessions, in two virtual machines that each have QEMU's `edu` test device at 0000:00:10.0:
#
#   box    has an (emulated) IOMMU: a session drives the device through VFIO, as the account, with no privilege
#   bare   has none: the device is not handed out, and sessions work without it
#
#   nix-build nix/test-vfio.nix
{
  pkgs ? import <nixpkgs> { },
}:

let
  common =
    { pkgs, ... }:
    {
      imports = [ ./module.nix ];
      virtualisation.memorySize = 2048;
      virtualisation.cores = 2;
      environment.systemPackages = [
        pkgs.python3
        pkgs.pciutils
      ];
      environment.etc."client.py".source = ../client.py;
      environment.etc."vfio_edu.py".source = ../tests/vfio_edu.py;
      services.compu-connect = {
        enable = true;
        # There is no relay here: the client talks to the gateway on this machine.
        localPort = 8765;
        hardware.vfioPciDevices = [ "0000:00:10.0" ];
        packages = with pkgs; [
          bashInteractive
          coreutils
          gnugrep
          python3
          pciutils
        ];
      };
    };
in
pkgs.testers.runNixOSTest {
  name = "compu-connect-vfio";

  nodes.box = {
    imports = [ common ];
    virtualisation.qemu.options = [
      "-machine q35,kernel-irqchip=split"
      "-device intel-iommu,intremap=on"
      "-device edu,addr=0x10"
    ];
  };

  nodes.bare = {
    imports = [ common ];
    virtualisation.qemu.options = [ "-device edu,addr=0x10" ];
  };

  testScript = ''
    import shlex

    DEVICE = "0000:00:10.0"


    def client(machine, token, *args):
        env = f"REMOTE_COMPUTE_URL=http://127.0.0.1:8765/mcp REMOTE_COMPUTE_TOKEN={token}"
        return machine.succeed(f"{env} python3 /etc/client.py " + " ".join(shlex.quote(a) for a in args))


    def session(machine):
        token = machine.succeed("mcpctl create -q").strip()
        client(machine, token, "push", "/etc/vfio_edu.py", "vfio_edu.py")
        return token


    start_all()

    with subtest("with an IOMMU, the listed device is handed to the account and to nobody else"):
        box.wait_for_unit("mcp-gateway.service")
        box.wait_for_unit("mcp-vfio.service")
        print(box.succeed("journalctl -u mcp-vfio -o cat --no-pager; lspci -k"))
        group = box.succeed(f"basename $(readlink -f /sys/bus/pci/devices/{DEVICE}/iommu_group)").strip()
        owner = box.succeed(f"stat -c '%U:%G %a' /dev/vfio/{group}").strip()
        assert owner == "root:mcp-user 660", owner
        box.succeed(f"test $(basename $(readlink -f /sys/bus/pci/devices/{DEVICE}/driver)) = vfio-pci")

    with subtest("a session drives the device from user space"):
        token = session(box)
        out = client(box, token, "run", f"id -un; python3 vfio_edu.py {DEVICE}")
        print(out)
        assert "mcp-user" in out and "identification 0x10000ed" in out and "read back 0xedcba987" in out, out
        out = client(box, token, "run", "grep -E '^Cap(Eff|Bnd)' /proc/self/status; ls -l /dev/vfio")
        print(out)
        assert "CapEff:\t0000000000000000" in out, out

    with subtest("and only that device: the others stay with the kernel"):
        out = client(
            box,
            token,
            "run",
            "for d in /sys/bus/pci/devices/*; do "
            "if sh -c \"echo 1 > $d/remove\" 2>/dev/null || sh -c \"head -c 4 $d/resource0\" >/dev/null 2>&1; "
            "then echo REACHED $d; fi; done; echo checked; true",
        )
        assert "checked" in out and "REACHED" not in out, out
        client(box, token, "run", "test ! -e /dev/mem || ! head -c 1 /dev/mem")

    with subtest("without an IOMMU the device is not handed out at all"):
        bare.wait_for_unit("mcp-gateway.service")
        bare.wait_until_succeeds("systemctl is-failed mcp-vfio.service")
        out = bare.succeed("journalctl -u mcp-vfio -o cat --no-pager")
        print(out)
        assert "no IOMMU" in out, out
        bare.fail(f"test -e /sys/bus/pci/devices/{DEVICE}/driver")
        token = session(bare)
        out = client(bare, token, "run", f"python3 vfio_edu.py {DEVICE} 2>&1; echo status=$?; ls /dev/vfio; true")
        print(out)
        assert "identification" not in out and "status=0" not in out, out
        # Sessions themselves are fine.
        assert "mcp-user" in client(bare, token, "run", "id -un")
        assert "mcp-vfio.service" in bare.fail("mcpctl status")
  '';
}
