#!/usr/bin/env python3
"""Recover the UVC extension-unit map from the Insta360 Link RTOS image.

    ./fwpack.py unpack Insta360WebCamFW.bin out/
    ./xu_static_map.py out/p0_rtos.bin

Reads the dispatch table of {unitID, selector, handler} triples that the XU request
router (FUN_00589A28) walks, then linearly scans each handler -- bounded by the next
handler's entry point -- to recover its GET_LEN payload size and the internal
SetParam/GetParam ids it touches.

The image is a flat ARM32 blob based at 0x20000 and uses MOVW/MOVT pairs rather than
literal pools, so constants are reconstructed by pairing the two halves per
destination register.
"""
import struct
import sys

BASE = 0x20000
XU_TABLE = 0x011683E0     # {u32 unit, u32 selector, u32 handler} x 38
XU_TABLE_N = 38
SET_PARAM = 0x005848E8    # InsApp_Webcam_SetParam(id, buf, arg)
GET_PARAM = 0x00582A8C    # InsApp_Webcam_GetParam(id, buf, arg)
SET_LEN = 0x00994B48      # reply to UVC GET_LEN with r1 = length


def main(path):
    d = open(path, "rb").read()

    def w(a):
        return struct.unpack("<I", d[a - BASE : a - BASE + 4])[0]

    def imm12(v):
        rot = ((v >> 8) & 0xF) * 2
        i8 = v & 0xFF
        return ((i8 >> rot) | (i8 << (32 - rot))) & 0xFFFFFFFF if rot else i8

    def cstr(a):
        o = a - BASE
        if not 0 <= o < len(d):
            return None
        s = d[o : d.find(b"\0", o)]
        if not 4 <= len(s) < 120 or not all(32 <= c < 127 for c in s):
            return None
        return s.decode("ascii")

    tbl = [
        struct.unpack("<III", d[XU_TABLE + 12 * i - BASE : XU_TABLE + 12 * i - BASE + 12])
        for i in range(XU_TABLE_N)
    ]
    # Handlers are laid out contiguously; bound each scan by the next one's start,
    # otherwise a handler bleeds into its successor and inherits its strings.
    order = sorted(tbl, key=lambda t: t[2])
    end = {h: (order[i + 1][2] if i + 1 < len(order) else h + 0x300)
           for i, (_, _, h) in enumerate(order)}

    print(f"{'XU':<4}{'sel':<5}{'handler':<12}{'wLen':<8}{'SET param':<20}"
          f"{'GET param':<20}strings")
    for unit, sel, h in tbl:
        regs, sets, gets, lens, strs = {}, [], [], [], []
        a = h
        while a < end[h]:
            v = w(a)
            if (v & 0x0FF00000) == 0x03000000:                       # movw
                regs[(v >> 12) & 0xF] = ((v >> 16) & 0xF) << 12 | (v & 0xFFF)
            elif (v & 0x0FF00000) == 0x03400000:                     # movt
                rd = (v >> 12) & 0xF
                hi = ((v >> 16) & 0xF) << 12 | (v & 0xFFF)
                regs[rd] = (regs.get(rd, 0) & 0xFFFF) | (hi << 16)
                t = cstr(regs[rd])
                if t and t not in strs:
                    strs.append(t)
            elif (v & 0x0FE00000) == 0x03A00000:                     # mov rd, #imm
                regs[(v >> 12) & 0xF] = imm12(v)
            elif (v & 0x0F000000) == 0x0B000000:                     # bl
                off = v & 0xFFFFFF
                off -= 0x1000000 if off & 0x800000 else 0
                tgt = a + 8 + off * 4
                if tgt == SET_PARAM:
                    sets.append(hex(regs.get(0, -1)))
                elif tgt == GET_PARAM:
                    gets.append(hex(regs.get(0, -1)))
                elif tgt == SET_LEN:
                    lens.append(regs.get(1, -1))
            a += 4
        print(f"{unit:<4}{sel:<5}{h:#010x}  {(hex(lens[0]) if lens else '?'):<8}"
              f"{','.join(dict.fromkeys(sets)) or '-':<20}"
              f"{','.join(dict.fromkeys(gets)) or '-':<20}{strs[:2]}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
