#!/usr/bin/env python3
"""Split Insta360WebCamFW.bin into its components.

    ./extract_partitions.py Insta360WebCamFW.bin outdir/

Produces, in outdir/:
    p0_rtos.bin      Ambarella ThreadX application image (ARM32, loads at 0x20000)
    p1_romfs.bin     ROMFS: DSP/ORC microcode
    p2_romfs.bin     ROMFS: NN models, audio, bitmaps, ISP tuning
    gimbal.bin       Cortex-M application for the gimbal MCU (loads at 0x8000)

and verifies every checksum in the container (3x Ambarella CRC32, 2x component
MD5, 1x whole-file MD5).
"""
import hashlib
import os
import struct
import sys
import zlib

AMBA_MAGIC = 0xA324EB90
TRAILER_MAGIC = b"WFNIMACW"
ENTRY_SIZE = 84
PART_HDR = 0x100


def split(path, outdir):
    data = open(path, "rb").read()
    os.makedirs(outdir, exist_ok=True)
    ok = True

    # --- trailer: entries[count] + magic(8) + count(8) + md5(16) ---
    magic, count = struct.unpack("<8sQ", data[-32:-16])
    if magic != TRAILER_MAGIC:
        sys.exit(f"not an Insta360 webcam package (trailer magic {magic!r})")
    whole = data[-16:]
    calc = hashlib.md5(data[:-16]).digest()
    print(f"package  md5 {'OK  ' if calc == whole else 'FAIL'} {calc.hex()}")
    ok &= calc == whole

    tstart = len(data) - 32 - count * ENTRY_SIZE
    off = 0
    names = {"webcam": "p0", "gimbal": "gimbal.bin"}
    for i in range(count):
        e = data[tstart + i * ENTRY_SIZE : tstart + (i + 1) * ENTRY_SIZE]
        size = struct.unpack("<I", e[:4])[0]
        name = e[4:36].split(b"\0")[0].decode()
        ver = e[36:68].split(b"\0")[0].decode()
        want = e[68:84]
        blob = data[off : off + size]
        got = hashlib.md5(blob).digest()
        print(f"{name:8s} md5 {'OK  ' if got == want else 'FAIL'} {got.hex()}  "
              f"{ver}  off={off:#x} size={size:#x}")
        ok &= got == want
        if name == "gimbal":
            open(os.path.join(outdir, "gimbal.bin"), "wb").write(blob)
        elif name == "webcam":
            ok &= split_ambarella(blob, outdir)
        off += size
    return ok


def split_ambarella(img, outdir):
    """Walk the Ambarella partition chain; each flpart_t sits 0x100 before its data."""
    ok = True
    labels = ["p0_rtos.bin", "p1_romfs.bin", "p2_romfs.bin"]
    hdr = 0x230  # first flpart_t, after the outer header
    for n in range(len(labels)):
        crc, _vnum, vdate, ilen, mem, _flag, magic = struct.unpack(
            "<7I", img[hdr : hdr + 28]
        )
        if magic != AMBA_MAGIC:
            print(f"  partition {n}: bad magic {magic:#x}")
            return False
        blob = img[hdr + PART_HDR : hdr + PART_HDR + ilen]
        got = zlib.crc32(blob) & 0xFFFFFFFF
        print(f"  {labels[n]:13s} crc32 {'OK  ' if got == crc else 'FAIL'} "
              f"{got:#010x}  load={mem:#x} size={ilen:#x}  "
              f"date={vdate >> 16:04x}-{(vdate >> 8) & 0xFF:02x}-{vdate & 0xFF:02x}")
        ok &= got == crc
        open(os.path.join(outdir, labels[n]), "wb").write(blob)
        hdr += PART_HDR + ilen
    return ok


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(0 if split(sys.argv[1], sys.argv[2]) else 1)
