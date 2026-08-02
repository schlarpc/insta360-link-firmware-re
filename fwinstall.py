#!/usr/bin/env python3
"""Install firmware onto an Insta360 Link.

    ./fwinstall.py status  /dev/videoN [FW.bin]
    ./fwinstall.py install /dev/videoN FW.bin --yes
    ./fwinstall.py install /dev/videoN FW.bin --via msc --yes
    ./fwinstall.py mode    0 --yes

Both transports write the same file to the same place: `A:\\Insta360WebCamFW.bin` on
the internal volume of the camera. The camera installs it on the next boot. They
differ in how the bytes get there, and in whether a person must touch the camera.

  --via simple   (default) The vendor class of the camera, USB 4255:1234. Control
                 selector 4 arms an upload and bulk endpoint 0x01 carries the data.
                 Control selector 2 then reboots the camera, so the whole cycle runs
                 with no action from the user. Needs write access to /dev/bus/usb.

  --via msc      Mass storage, USB 070A:4026. The volume is mounted and the file is
                 copied. Mass-storage mode has no control channel, so the user must
                 replug the camera. Needs permission to mount a removable volume.

Both transports start with XU 9 selector 17, a UVC extension-unit request. Any
process that opens /dev/videoN can send it.

The camera authenticates nothing. It accepts any package whose MD5 digests and type
word agree with its contents. See README sections 3 to 5.

WARNING: An install replaces the firmware of the camera. Do not disconnect the camera
during an install.

The tool writes only with --yes. The `status` command never writes.
"""
import argparse
import ctypes
import fcntl
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fwpack

try:
    import usb.core
    import usb.util
    import usb.backend.libusb1 as _lb
except ImportError:  # only the simple transport needs pyusb
    usb = None

# ---- USB identities -----------------------------------------------------
# The camera presents a different identity in each mode.  A udev rule must
# list all three.
UVC_VID, UVC_PID = 0x2E1A, 0x4C01
SIMPLE_VID, SIMPLE_PID = 0x4255, 0x1234
MSC_VIDS = ("070a", "2e1a")

# ---- UVC extension unit -------------------------------------------------
UVCIOC_CTRL_QUERY = 0xC0107521  # _IOWR('u', 0x21, struct uvc_xu_control_query)
SET_CUR, GET_CUR, GET_LEN = 0x01, 0x81, 0x85
XU_MODE_UNIT, XU_MODE_SEL = 9, 17

# FUN_00587468 indexes a four-entry table at 0xB4E3E8 and does not test the
# range of the byte.  Values of 5 and more read past the table.
MODES = {0: "uvc", 1: "photo", 2: "msc", 3: "simple"}
MODE_MSC, MODE_SIMPLE = 2, 3

# ---- simple vendor class ------------------------------------------------
RT_OUT, RT_IN = 0x41, 0xC1
CS_MODE, CS_UPLOAD, CS_DOWNLOAD = 2, 4, 5
EP_OUT = 0x01
UPLOAD_FMT = "<IIB128s"  # total, written, state, name
UPLOAD_LEN = 0x89
ST_IDLE, ST_BUSY, ST_DONE = 0, 1, 2
ST_SIZE_ERR, ST_WRITE_ERR = 0x70, 0x71
STATES = {ST_IDLE: "idle", ST_BUSY: "in progress", ST_DONE: "complete",
          ST_SIZE_ERR: "size error", ST_WRITE_ERR: "write error"}
CHUNK = 256 * 1024  # the receive buffer of the camera is 0x80000

FW_PATH = "A:\\Insta360WebCamFW.bin"
FW_NAME = "Insta360WebCamFW.bin"


class _Query(ctypes.Structure):
    _fields_ = [("unit", ctypes.c_uint8), ("selector", ctypes.c_uint8),
                ("query", ctypes.c_uint8), ("size", ctypes.c_uint16),
                ("data", ctypes.POINTER(ctypes.c_uint8))]


def xu_query(video, query, size, payload=None):
    buf = (ctypes.c_uint8 * size)()
    if payload is not None:
        buf[: len(payload)] = payload
    q = _Query(XU_MODE_UNIT, XU_MODE_SEL, query, size,
               ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8)))
    fd = os.open(video, os.O_RDWR)
    try:
        fcntl.ioctl(fd, UVCIOC_CTRL_QUERY, q)
    finally:
        os.close(fd)
    return bytes(buf)


def get_mode(video):
    return xu_query(video, GET_CUR, 1)[0]


def set_mode(video, mode):
    if mode not in MODES:
        raise ValueError(f"refusing mode {mode}: only 0 to 3 are in range")
    xu_query(video, SET_CUR, 1, bytes([mode]))


# ---- simple transport ---------------------------------------------------


def _backend():
    so = os.environ.get("LIBUSB1_SO")
    return _lb.get_backend(find_library=(lambda _: so) if so else None)


def find_simple(timeout=0.0):
    if usb is None:
        raise RuntimeError("pyusb is not installed; the simple transport needs it")
    deadline = time.time() + timeout
    while True:
        dev = usb.core.find(idVendor=SIMPLE_VID, idProduct=SIMPLE_PID,
                            backend=_backend())
        if dev is not None:
            try:
                if dev.is_kernel_driver_active(0):
                    dev.detach_kernel_driver(0)
            except (usb.core.USBError, NotImplementedError):
                pass
            return dev
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def cs_get(dev, cs, length):
    return bytes(dev.ctrl_transfer(RT_IN, GET_CUR, cs, 0, length, timeout=3000))


def cs_set(dev, cs, payload):
    return dev.ctrl_transfer(RT_OUT, SET_CUR, cs, 0, payload, timeout=3000)


def upload_status(dev):
    total, written, state, name = struct.unpack(UPLOAD_FMT,
                                                cs_get(dev, CS_UPLOAD, UPLOAD_LEN))
    return total, written, state, name.split(b"\0")[0].decode(errors="replace")


def install_simple(video, data):
    dev = find_simple()
    if dev is None:
        print(f"switching to simple mode (XU 9/17 = {MODE_SIMPLE})")
        set_mode(video, MODE_SIMPLE)
        dev = find_simple(timeout=30)
        if dev is None:
            print(f"the camera did not appear as {SIMPLE_VID:04x}:{SIMPLE_PID:04x}")
            return 1
    print(f"simple mode: bus {dev.bus} addr {dev.address}")

    total, written, state, cur = upload_status(dev)
    if state == ST_BUSY:
        print(f"an upload is already active ({written}/{total} of {cur!r}). "
              f"Power-cycle the camera first.")
        return 1

    cs_set(dev, CS_UPLOAD, struct.pack(UPLOAD_FMT, len(data), 0, ST_BUSY,
                                       FW_PATH.encode()))
    print(f"armed upload: {FW_PATH} {len(data)} bytes")

    sent, t0 = 0, time.time()
    while sent < len(data):
        sent += dev.write(EP_OUT, data[sent : sent + CHUNK], timeout=20000)
        el = time.time() - t0
        print(f"\r  {sent}/{len(data)} bytes ({100 * sent // len(data)}%) "
              f"{sent / el / 1e6:.1f} MB/s", end="", flush=True)
    print()

    for _ in range(120):
        total, written, state, cur = upload_status(dev)
        if state == ST_DONE:
            print(f"the camera reports complete: {written}/{total}")
            break
        if state in (ST_SIZE_ERR, ST_WRITE_ERR):
            print(f"the camera reports {STATES[state]}: {written}/{total}")
            return 1
        time.sleep(0.5)
    else:
        print(f"no completion after 60s (state {state:#04x}, {written}/{total})")
        return 1

    print("sending CS 2 to reboot the camera")
    try:
        cs_set(dev, CS_MODE, bytes([0]))
    except usb.core.USBError as e:
        print(f"  CS 2 returned {e}. This is normal, the camera drops off the bus.")
    print("\nThe camera reboots, installs the package, reboots again and returns")
    print("in uvc mode. This takes about 30 seconds. No action is needed.")
    return 0


# ---- msc transport ------------------------------------------------------


def _usb_ancestor_vid(syspath):
    p = os.path.realpath(syspath)
    while p != "/":
        f = os.path.join(p, "idVendor")
        if os.path.exists(f):
            return open(f).read().strip()
        p = os.path.dirname(p)
    return None


def find_msc_disk():
    for name in sorted(os.listdir("/sys/block")):
        dev = f"/sys/block/{name}/device"
        if os.path.exists(dev) and _usb_ancestor_vid(dev) in MSC_VIDS:
            return f"/dev/{name}"
    return None


def wait_for_msc(timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        disk = find_msc_disk()
        if disk:
            time.sleep(1.0)  # let the partitions settle
            return disk
        time.sleep(0.5)
    return None


def volumes(disk):
    out = subprocess.run(
        ["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,FSTYPE,MOUNTPOINT", disk],
        capture_output=True, text=True, check=True)
    found = []
    for d in json.loads(out.stdout)["blockdevices"]:
        found.extend(d.get("children", []))
        if not d.get("children") and d.get("fstype"):
            found.append(d)
    return found


def install_msc(video, data):
    disk = find_msc_disk()
    if disk:
        print(f"already in mass-storage mode: {disk}")
    else:
        print(f"switching to mass-storage mode (XU 9/17 = {MODE_MSC})")
        set_mode(video, MODE_MSC)
        disk = wait_for_msc()
        if disk is None:
            print("no mass-storage device appeared within 30s")
            return 1
    vols = volumes(disk)
    if not vols:
        print(f"{disk} has no mountable filesystem")
        return 1
    vol = vols[0]
    print(f"volume: {vol['path']} ({vol.get('fstype')})")

    mounted_here = False
    mnt = vol.get("mountpoint")
    if not mnt:
        r = subprocess.run(["udisksctl", "mount", "-b", vol["path"]],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"mount failed: {r.stderr.strip() or r.stdout.strip()}")
            return 1
        mnt = r.stdout.strip().rsplit(" at ", 1)[-1].rstrip(".")
        mounted_here = True
    try:
        dest = os.path.join(mnt, FW_NAME)
        existing = os.path.getsize(dest) if os.path.exists(dest) else 0
        if shutil.disk_usage(mnt).free + existing < len(data):
            print(f"not enough free space on {mnt}")
            return 1
        print(f"writing {dest}")
        with open(dest, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        got = hashlib.sha256(open(dest, "rb").read()).hexdigest()
        if got != hashlib.sha256(data).hexdigest():
            print(f"the file read back different: {got}")
            return 1
        print(f"read back correct: {got}")
    finally:
        if mounted_here:
            subprocess.run(["udisksctl", "unmount", "-b", vol["path"]],
                           capture_output=True, text=True)
            print(f"unmounted {vol['path']}")

    print("\nUnplug the camera and plug it in again. The next boot installs the")
    print("package. Mass-storage mode has no control channel, so the camera")
    print("cannot restart by itself.")
    return 0


TRANSPORTS = {"simple": install_simple, "msc": install_msc}


# ---- commands -----------------------------------------------------------


def load_package(path):
    data = open(path, "rb").read()
    pkg = fwpack.Package(data)
    print(f"image: {path}  {len(data)} bytes")
    print(f"  sha256 {hashlib.sha256(data).hexdigest()}")
    for c in pkg.components:
        print(f"  component {c.name:8s} {c.version}")
    if not pkg.verify(data, log=lambda *a: None):
        return None
    print("  the package verifies")
    return data


def cmd_status(args):
    try:
        mode = get_mode(args.video)
        print(f"{args.video}: usb mode {mode} ({MODES.get(mode, '?')})")
    except OSError as e:
        print(f"{args.video}: not available ({e.strerror})")

    disk = find_msc_disk()
    print(f"mass-storage disk: {disk or 'none'}")
    if disk:
        for v in volumes(disk):
            print(f"  {v['path']}  {v.get('fstype')}  {v['size']} bytes  "
                  f"mounted={v.get('mountpoint') or 'no'}")
    if usb is None:
        print("vendor class: pyusb is not installed")
    else:
        dev = find_simple()
        if dev is None:
            print("vendor class: not present")
        else:
            total, written, state, name = upload_status(dev)
            print(f"vendor class: bus {dev.bus} addr {dev.address}")
            print(f"  upload slot: total={total} written={written} "
                  f"state={state:#04x} ({STATES.get(state, '?')}) name={name!r}")
    if args.fw:
        print()
        return 0 if load_package(args.fw) is not None else 1
    return 0


def cmd_mode(args):
    if args.mode not in MODES:
        print(f"refusing mode {args.mode}: only 0 to 3 are in range")
        return 1
    print(f"about to set USB mode {args.mode} ({MODES[args.mode]})")
    if not args.yes:
        print("refusing without --yes")
        return 1
    dev = find_simple() if usb is not None else None
    if dev is not None:
        cs_set(dev, CS_MODE, bytes([args.mode]))
        print("sent over the vendor class (CS 2). The camera reboots.")
    else:
        set_mode(args.video, args.mode)
        print("sent over UVC (XU 9/17)")
    return 0


def cmd_install(args):
    data = load_package(args.fw)
    if data is None:
        print("the package failed verification. Nothing was sent.")
        return 1
    print(f"\ntransport: {args.via}")
    if args.via == "simple":
        print("  1. XU 9/17 = 3, switch to simple mode")
        print(f"  2. CS 4 arms the upload, bulk 0x01 carries {len(data)} bytes")
        print("  3. CS 4 is read until the state is 2")
        print("  4. CS 2 reboots the camera, which then installs the package")
    else:
        print("  1. XU 9/17 = 2, switch to mass-storage mode")
        print("  2. mount the volume and write the file")
        print("  3. unmount")
        print("  4. you replug the camera, which then installs the package")
    print("\nThis replaces the firmware of the camera. Keep it connected.")
    if not args.yes:
        print("\nrefusing without --yes")
        return 1
    print()
    return TRANSPORTS[args.via](args.video, data)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="read-only: modes, volumes, package check")
    p.add_argument("video")
    p.add_argument("fw", nargs="?")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("install", help="write firmware onto the camera")
    p.add_argument("video")
    p.add_argument("fw")
    p.add_argument("--via", choices=sorted(TRANSPORTS), default="simple")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_install)

    p = sub.add_parser("mode", help="switch the USB mode")
    p.add_argument("mode", type=int)
    p.add_argument("--video", default="/dev/video0")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_mode)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
