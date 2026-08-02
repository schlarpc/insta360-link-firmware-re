#!/usr/bin/env python3
"""Parse, verify, patch and rebuild Insta360WebCamFW.bin.

    ./fwpack.py verify     FW.bin
    ./fwpack.py unpack     FW.bin OUTDIR
    ./fwpack.py roundtrip  FW.bin              # rebuild and compare, byte for byte
    ./fwpack.py patch-led  FW.bin OUT.bin      # disable the indicator LED
    ./fwpack.py standalone FW.bin OUT.bin webcam

The package holds ten integrity values. This tool recomputes all ten:

  Ambarella partition (flpart_t, in the 0x100 header before each partition)
    crc32       zlib CRC32 of the partition payload
    flag        derived value, always img_len * 32

  outer header (0x230 bytes before the first partition, magic 0x8732DFE6)
    +0x24       zlib CRC32 over the whole partition region, headers included
    +0x30+8n    size of partition n, with its 0x100 header
    +0x34+8n    the un-finalized CRC32 register after partition n, which equals
                ~crc32(region[:end of partition n]).  One running register spans
                all partitions.  The packer stores it un-finalized after each
                partition, and writes the finalized value once into +0x24.

  trailer (README section 2)
    entry.md5   MD5 of each component
    final md5   MD5 of the whole file, less the last 16 bytes

The package holds no signature and no encryption.  Any of these values can be
recomputed, so this tool can build firmware that the camera accepts.
"""
import argparse
import hashlib
import os
import struct
import sys
import zlib

AMBA_MAGIC = 0xA324EB90
OUTER_MAGIC = 0x8732DFE6
TRAILER_MAGIC = b"WFNIMACW"
ENTRY_SIZE = 84
PART_HDR = 0x100
OUTER_HDR = 0x230
OUTER_MAGIC_OFF = 0x20
OUTER_CRC_OFF = 0x24
OUTER_TABLE_OFF = 0x30

# LED mode table: 16 entries x 16 bytes, three {level, brightness, period_u16}
# channels plus a u16 duration.  See README section 6.
LED_TABLE_VA = 0x01192070
LED_TABLE_ENTRIES = 16
LED_ENTRY_SIZE = 16
RTOS_BASE = 0x20000
LED_MODE_UPDATE = 9  # firmware-update-in-progress blink


class Partition:
    def __init__(self, hdr, data):
        self.hdr = bytearray(hdr)
        self.data = bytearray(data)

    @property
    def fields(self):
        return struct.unpack_from("<7I", self.hdr, 0)

    def refresh(self):
        """Recompute the derived flpart_t fields for the current payload."""
        crc, vnum, vdate, _ilen, mem, _flag, magic = self.fields
        ilen = len(self.data)
        struct.pack_into(
            "<7I", self.hdr, 0,
            zlib.crc32(self.data) & 0xFFFFFFFF, vnum, vdate,
            ilen, mem, ilen * 32, magic,
        )

    def blob(self):
        return bytes(self.hdr) + bytes(self.data)


class Component:
    def __init__(self, name, version, blob):
        self.name = name
        self.version = version
        self.blob = blob


class Package:
    def __init__(self, data):
        magic, count = struct.unpack("<8sQ", data[-32:-16])
        if magic != TRAILER_MAGIC:
            raise ValueError(f"not an Insta360 webcam package (magic {magic!r})")
        self.count = count
        self.file_md5 = data[-16:]
        self.entry_md5 = []

        tstart = len(data) - 32 - count * ENTRY_SIZE
        self.components = []
        off = 0
        for i in range(count):
            e = data[tstart + i * ENTRY_SIZE : tstart + (i + 1) * ENTRY_SIZE]
            size = struct.unpack("<I", e[:4])[0]
            name = e[4:36].split(b"\0")[0].decode()
            ver = e[36:68].split(b"\0")[0].decode()
            self.entry_md5.append(e[68:84])
            self.components.append(Component(name, ver, data[off : off + size]))
            off += size

        # the webcam component is an Ambarella multi-partition image
        self.outer = None
        self.parts = []
        for c in self.components:
            if c.name == "webcam":
                self._parse_ambarella(c.blob)

    def _parse_ambarella(self, img):
        if struct.unpack_from("<I", img, OUTER_MAGIC_OFF)[0] != OUTER_MAGIC:
            raise ValueError("webcam component has no Ambarella outer header")
        self.outer = bytearray(img[:OUTER_HDR])
        off = OUTER_HDR
        while off + PART_HDR <= len(img):
            if struct.unpack_from("<I", img, off + 24)[0] != AMBA_MAGIC:
                break
            ilen = struct.unpack_from("<I", img, off + 12)[0]
            self.parts.append(
                Partition(img[off : off + PART_HDR],
                          img[off + PART_HDR : off + PART_HDR + ilen])
            )
            off += PART_HDR + ilen
        if off != len(img):
            raise ValueError(f"trailing {len(img) - off} bytes after last partition")

    # ---- rebuild -------------------------------------------------------

    def build_webcam(self):
        for p in self.parts:
            p.refresh()
        region = b"".join(p.blob() for p in self.parts)

        outer = bytearray(self.outer)
        run = 0
        pos = 0
        for n, p in enumerate(self.parts):
            size = PART_HDR + len(p.data)
            run = zlib.crc32(region[pos : pos + size], run) & 0xFFFFFFFF
            struct.pack_into("<II", outer, OUTER_TABLE_OFF + n * 8,
                             size, run ^ 0xFFFFFFFF)
            pos += size
        struct.pack_into("<I", outer, OUTER_CRC_OFF, run)
        return bytes(outer) + region

    def build(self):
        for c in self.components:
            if c.name == "webcam" and self.parts:
                c.blob = self.build_webcam()

        body = b"".join(c.blob for c in self.components)
        trailer = b""
        for c in self.components:
            trailer += struct.pack(
                "<I32s32s16s", len(c.blob), c.name.encode(),
                c.version.encode(), hashlib.md5(c.blob).digest(),
            )
        trailer += struct.pack("<8sQ", TRAILER_MAGIC, len(self.components))
        out = body + trailer
        return out + hashlib.md5(out).digest()

    # ---- checks --------------------------------------------------------

    def verify(self, data, log=print):
        ok = True

        def chk(cond, label, detail=""):
            nonlocal ok
            ok &= bool(cond)
            log(f"  {'OK  ' if cond else 'FAIL'}  {label}{detail}")

        calc = hashlib.md5(data[:-16]).digest()
        chk(calc == self.file_md5, "package md5", f"  {calc.hex()}")
        for c, want in zip(self.components, self.entry_md5):
            got = hashlib.md5(c.blob).digest()
            chk(got == want, f"{c.name} md5",
                f"  {got.hex()}  {c.version}  size={len(c.blob):#x}")

        if not self.parts:
            return ok
        run = 0
        for n, p in enumerate(self.parts):
            crc, _vn, vdate, ilen, mem, flag, _m = p.fields
            got = zlib.crc32(p.data) & 0xFFFFFFFF
            chk(got == crc, f"partition {n} crc32",
                f"  {got:#010x}  load={mem:#x} size={ilen:#x} "
                f"date={vdate >> 16:04x}-{(vdate >> 8) & 0xFF:02x}-{vdate & 0xFF:02x}")
            chk(flag == ilen * 32, f"partition {n} flag", f"  {flag:#010x}")
            size, want = struct.unpack_from("<II", self.outer, OUTER_TABLE_OFF + n * 8)
            run = zlib.crc32(p.blob(), run) & 0xFFFFFFFF
            chk(size == PART_HDR + ilen, f"outer  {n} size", f"  {size:#x}")
            chk(want == run ^ 0xFFFFFFFF, f"outer  {n} crc reg", f"  {want:#010x}")
        want = struct.unpack_from("<I", self.outer, OUTER_CRC_OFF)[0]
        chk(want == run, "outer  crc32", f"  {want:#010x}")
        return ok


# ---- LED patch ---------------------------------------------------------


def patch_led(pkg, keep_update_blink=False):
    """Point every LED mode at mode 0's all-channels-off pattern.

    Purely a data edit: no code is touched, no lengths change, and the bytes
    written are the ones the gimbal MCU already receives whenever the firmware
    selects mode 0, so nothing novel reaches the MCU.
    """
    p0 = pkg.parts[0]
    off = LED_TABLE_VA - RTOS_BASE
    if off + LED_TABLE_ENTRIES * LED_ENTRY_SIZE > len(p0.data):
        raise ValueError("LED table lies outside partition 0")
    entry0 = bytes(p0.data[off : off + LED_ENTRY_SIZE])
    if any(entry0[i] for i in (0, 4, 8)):
        raise ValueError(f"mode 0 is not the off pattern: {entry0.hex(' ')}")

    changed = []
    for m in range(1, LED_TABLE_ENTRIES):
        if keep_update_blink and m == LED_MODE_UPDATE:
            continue
        a = off + m * LED_ENTRY_SIZE
        if bytes(p0.data[a : a + LED_ENTRY_SIZE]) != entry0:
            changed.append(m)
            p0.data[a : a + LED_ENTRY_SIZE] = entry0
    return entry0, changed


# ---- commands ----------------------------------------------------------


def cmd_verify(args):
    data = open(args.fw, "rb").read()
    pkg = Package(data)
    print(f"{args.fw}: {len(data)} bytes, {len(pkg.components)} components, "
          f"{len(pkg.parts)} partitions")
    return 0 if pkg.verify(data) else 1


def cmd_unpack(args):
    data = open(args.fw, "rb").read()
    pkg = Package(data)
    os.makedirs(args.outdir, exist_ok=True)
    ok = pkg.verify(data)
    labels = ["p0_rtos.bin", "p1_romfs.bin", "p2_romfs.bin"]
    for c in pkg.components:
        if c.name != "webcam":
            open(os.path.join(args.outdir, f"{c.name}.bin"), "wb").write(c.blob)
    if pkg.outer is not None:
        open(os.path.join(args.outdir, "outer_hdr.bin"), "wb").write(pkg.outer)
    for n, p in enumerate(pkg.parts):
        name = labels[n] if n < len(labels) else f"p{n}.bin"
        open(os.path.join(args.outdir, name), "wb").write(p.data)
        open(os.path.join(args.outdir, name + ".hdr"), "wb").write(p.hdr)
    print(f"wrote {args.outdir}/")
    return 0 if ok else 1


def cmd_roundtrip(args):
    data = open(args.fw, "rb").read()
    pkg = Package(data)
    if not pkg.verify(data):
        print("input failed verification")
        return 1
    out = pkg.build()
    if out == data:
        print(f"\nroundtrip OK: rebuilt {len(out)} bytes, byte-identical")
        return 0
    print(f"\nroundtrip FAILED: {len(out)} vs {len(data)} bytes")
    for i in range(min(len(out), len(data))):
        if out[i] != data[i]:
            print(f"  first difference at {i:#x}: built {out[i]:#04x} "
                  f"!= original {data[i]:#04x}")
            break
    return 1


def cmd_patch_led(args):
    data = open(args.fw, "rb").read()
    pkg = Package(data)
    if not pkg.verify(data, log=lambda *a: None):
        print("input failed verification, refusing to patch")
        return 1
    entry0, changed = patch_led(pkg, args.keep_update_blink)
    out = pkg.build()
    open(args.out, "wb").write(out)
    print(f"off pattern: {entry0.hex(' ')}")
    print(f"rewrote LED modes: {changed}")
    print(f"wrote {args.out}  ({len(out)} bytes, "
          f"sha256 {hashlib.sha256(out).hexdigest()})")
    print("\nre-verifying the built package:")
    return 0 if Package(out).verify(out) else 1


def cmd_standalone(args):
    data = open(args.fw, "rb").read()
    pkg = Package(data)
    keep = [c for c in pkg.components if c.name == args.component]
    if not keep:
        print(f"no component named {args.component!r}")
        return 1
    pkg.components = keep
    out = pkg.build()
    open(args.out, "wb").write(out)
    print(f"wrote {args.out}  ({len(out)} bytes, 1 component: {args.component})")
    print("\nre-verifying the built package:")
    return 0 if Package(out).verify(out) else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("verify"); p.add_argument("fw"); p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("unpack"); p.add_argument("fw"); p.add_argument("outdir")
    p.set_defaults(fn=cmd_unpack)
    p = sub.add_parser("roundtrip"); p.add_argument("fw"); p.set_defaults(fn=cmd_roundtrip)
    p = sub.add_parser("patch-led"); p.add_argument("fw"); p.add_argument("out")
    p.add_argument("--keep-update-blink", action="store_true",
                   help="leave mode 9 (firmware-update-in-progress) intact")
    p.set_defaults(fn=cmd_patch_led)
    p = sub.add_parser("standalone"); p.add_argument("fw"); p.add_argument("out")
    p.add_argument("component"); p.set_defaults(fn=cmd_standalone)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
