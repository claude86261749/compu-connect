#!/usr/bin/env python3
"""Drive a PCI device from user space through VFIO, with no privilege: what a session does with a device listed in
services.compu-connect.hardware.vfioPciDevices. Standard library only.

    python3 vfio_edu.py 0000:00:10.0

It reads and writes the registers of QEMU's `edu` test device (nix/test-vfio.nix runs it), but the first half, up to
the device's file descriptor, is the same for any device.
"""

import fcntl
import os
import struct
import sys

# linux/vfio.h: _IO(';', 100 + n)
VFIO_GET_API_VERSION = 0x3B64
VFIO_SET_IOMMU = 0x3B66
VFIO_GROUP_GET_STATUS = 0x3B67
VFIO_GROUP_SET_CONTAINER = 0x3B68
VFIO_GROUP_GET_DEVICE_FD = 0x3B6A
VFIO_DEVICE_GET_REGION_INFO = 0x3B6C
VFIO_TYPE1_IOMMU = 1
VFIO_GROUP_FLAGS_VIABLE = 1


def open_device(addr):
    """A file descriptor for the PCI device at `addr`, whose IOMMU group this account was given."""
    group_no = os.path.basename(os.readlink("/sys/bus/pci/devices/%s/iommu_group" % addr))
    container = os.open("/dev/vfio/vfio", os.O_RDWR)
    if fcntl.ioctl(container, VFIO_GET_API_VERSION) != 0:
        sys.exit("unknown VFIO API version")
    group = os.open("/dev/vfio/" + group_no, os.O_RDWR)
    status = bytearray(struct.pack("II", 8, 0))
    fcntl.ioctl(group, VFIO_GROUP_GET_STATUS, status)
    if not struct.unpack("II", status)[1] & VFIO_GROUP_FLAGS_VIABLE:
        sys.exit("IOMMU group %s is not viable: a device in it is still with a kernel driver" % group_no)
    fcntl.ioctl(group, VFIO_GROUP_SET_CONTAINER, struct.pack("i", container))
    # From here on the IOMMU lets the device reach only memory this process maps for it.
    fcntl.ioctl(container, VFIO_SET_IOMMU, VFIO_TYPE1_IOMMU)
    return fcntl.ioctl(group, VFIO_GROUP_GET_DEVICE_FD, bytearray(addr.encode() + b"\0"))


def region(dev, index):
    """(size, offset) of one of the device's regions; 0 to 5 are its BARs."""
    info = bytearray(struct.pack("IIIIQQ", 32, 0, index, 0, 0, 0))
    fcntl.ioctl(dev, VFIO_DEVICE_GET_REGION_INFO, info)
    _, _, _, _, size, offset = struct.unpack("IIIIQQ", info)
    return size, offset


def main():
    addr = sys.argv[1]
    dev = open_device(addr)
    size, bar0 = region(dev, 0)
    ident = struct.unpack("<I", os.pread(dev, 4, bar0))[0]
    print("BAR0 is %d bytes; identification 0x%x" % (size, ident))
    # edu's "liveness" register gives back the complement of what is written to it.
    os.pwrite(dev, struct.pack("<I", 0x12345678), bar0 + 4)
    back = struct.unpack("<I", os.pread(dev, 4, bar0 + 4))[0]
    print("wrote 0x12345678, read back 0x%x" % back)
    if ident >> 16 != 0x0100 or ident & 0xFF != 0xED or back != 0x12345678 ^ 0xFFFFFFFF:
        sys.exit("that is not what an edu device answers")


if __name__ == "__main__":
    main()
