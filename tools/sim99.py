#!/usr/bin/env python3
"""A small TMS9900 / TMS9918A simulator used to test the cartridge.

It implements the instruction subset the game uses, the banked
cartridge window, the 256 byte scratch pad, the VDP and the CRU lines
of the keyboard and joystick.  It is a test harness, not an emulator:
there is no timing, no GROM and no interrupt handling, and one "frame"
is simply one poll of the vertical blank flag.

    python tools/sim99.py 300                 run 300 frames
    python tools/sim99.py 300 --key=FIRE@100  hold fire on frame 100
    python tools/sim99.py 300 --png=build/screen.png

The screen shot is rendered from the VDP tables, so it shows what the
program has actually put into video memory.
"""

import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROM = os.path.join(ROOT, "build", "jawbreaker2-8.bin")

LGT, AGT, EQ, CAR, OVF = 0x8000, 0x4000, 0x2000, 0x1000, 0x0800

PALETTE = [
    (0, 0, 0), (0, 0, 0), (33, 200, 66), (94, 220, 120),
    (84, 85, 237), (125, 118, 252), (212, 82, 77), (66, 235, 245),
    (252, 85, 84), (255, 121, 120), (212, 193, 84), (230, 206, 128),
    (33, 176, 59), (201, 91, 186), (204, 204, 204), (255, 255, 255),
]


class Fault(Exception):
    pass


class Machine(object):
    def __init__(self, rom):
        self.rom = rom
        self.nbanks = len(rom) // 0x2000
        self.bank = 0
        self.pad = bytearray(256)
        self.wp = 0x83E0
        self.pc = 0
        self.st = 0
        self.vram = bytearray(0x4000)
        self.vreg = [0] * 8
        self.vaddr = 0
        self.vlatch = None
        self.sound = []
        self.col = 0
        self.keys = set()
        self.frames = 0
        # Rough cycle model: TMS9900 base cycles plus four wait state
        # cycles for every access outside the 16 bit scratch pad, which
        # is what the 8 bit multiplexer of the console costs.
        self.cycles = 0

    # ------------------------------------------------ memory
    def rb(self, a):
        a &= 0xFFFF
        if not (0x8300 <= a < 0x8400):
            self.cycles += 4
        if 0x6000 <= a < 0x8000:
            return self.rom[self.bank * 0x2000 + (a - 0x6000)]
        if 0x8000 <= a < 0x8400:
            return self.pad[a & 0xFF]
        if a & 0xFFFE == 0x8800:
            v = self.vram[self.vaddr]
            self.vaddr = (self.vaddr + 1) & 0x3FFF
            return v
        if a & 0xFFFE == 0x8802:
            self.frames += 1
            return 0x80                     # vertical blank, always ready
        raise Fault("read from %04x" % a)

    def wb(self, a, v):
        a &= 0xFFFF
        v &= 0xFF
        if not (0x8300 <= a < 0x8400):
            self.cycles += 4
        if 0x6000 <= a < 0x8000:
            self.bank = ((a - 0x6000) >> 1) % self.nbanks
            return
        if 0x8000 <= a < 0x8400:
            self.pad[a & 0xFF] = v
            return
        if a & 0xFFFE == 0x8400:
            self.sound.append(v)
            return
        if a & 0xFFFE == 0x8C00:
            self.vram[self.vaddr] = v
            self.vaddr = (self.vaddr + 1) & 0x3FFF
            self.vlatch = None
            return
        if a & 0xFFFE == 0x8C02:
            if self.vlatch is None:
                self.vlatch = v
            else:
                if v & 0x80:
                    self.vreg[v & 7] = self.vlatch
                elif v & 0x40:
                    self.vaddr = ((v & 0x3F) << 8) | self.vlatch
                else:
                    self.vaddr = ((v & 0x3F) << 8) | self.vlatch
                self.vlatch = None
            return
        raise Fault("write %02x to %04x" % (v, a))

    def rw(self, a):
        a &= 0xFFFE
        return (self.rb(a) << 8) | self.rb(a + 1)

    def ww(self, a, v):
        a &= 0xFFFE
        self.wb(a, (v >> 8) & 0xFF)
        self.wb(a + 1, v & 0xFF)

    def reg(self, n):
        return self.rw(self.wp + 2 * (n & 15))

    def setreg(self, n, v):
        self.ww(self.wp + 2 * (n & 15), v & 0xFFFF)

    # ------------------------------------------------ status bits
    def flags(self, v, byte=False):
        m = 0x80 if byte else 0x8000
        v &= (0xFF if byte else 0xFFFF)
        self.st &= ~(LGT | AGT | EQ)
        if v:
            self.st |= LGT
            if not (v & m):
                self.st |= AGT
        else:
            self.st |= EQ
        return v

    def compare(self, a, b, byte=False):
        mask = 0xFF if byte else 0xFFFF
        m = 0x80 if byte else 0x8000
        a &= mask
        b &= mask
        self.st &= ~(LGT | AGT | EQ)
        if a == b:
            self.st |= EQ
        if a > b:
            self.st |= LGT
        sa = a - 2 * m if a & m else a
        sb = b - 2 * m if b & m else b
        if sa > sb:
            self.st |= AGT

    def do_add(self, a, b, byte=False):
        mask = 0xFF if byte else 0xFFFF
        m = 0x80 if byte else 0x8000
        r = (a + b) & mask
        self.st &= ~(CAR | OVF)
        if a + b > mask:
            self.st |= CAR
        if (a & m) == (b & m) and (r & m) != (a & m):
            self.st |= OVF
        return self.flags(r, byte)

    def do_sub(self, a, b, byte=False):
        mask = 0xFF if byte else 0xFFFF
        m = 0x80 if byte else 0x8000
        r = (a - b) & mask
        self.st &= ~(CAR | OVF)
        if (a & mask) >= (b & mask):
            self.st |= CAR
        if (a & m) != (b & m) and (r & m) != (a & m):
            self.st |= OVF
        return self.flags(r, byte)

    # ------------------------------------------------ addressing
    def operand(self, mode, r, byte=False):
        if mode == 0:
            return ('R', r)
        if mode == 1:
            return ('M', self.reg(r))
        if mode == 2:
            w = self.rw(self.pc)
            self.pc += 2
            if r:
                w = (w + self.reg(r)) & 0xFFFF
            return ('M', w)
        a = self.reg(r)
        self.setreg(r, a + (1 if byte else 2))
        return ('M', a)

    def get(self, op, byte=False):
        kind, v = op
        if kind == 'R':
            w = self.reg(v)
            return (w >> 8) & 0xFF if byte else w
        return self.rb(v) if byte else self.rw(v)

    def put(self, op, val, byte=False):
        kind, v = op
        if kind == 'R':
            if byte:
                w = self.reg(v)
                self.setreg(v, ((val & 0xFF) << 8) | (w & 0xFF))
            else:
                self.setreg(v, val)
        elif byte:
            self.wb(v, val)
        else:
            self.ww(v, val)

    # ------------------------------------------------ one instruction
    def step(self):
        pc0 = self.pc
        op = self.rw(self.pc)
        self.pc += 2
        top = op >> 12
        if top >= 4:
            self.cycles += 14
        elif top == 3:
            self.cycles += 52 if (op & 0x0C00) else 20
        elif top == 2:
            self.cycles += 14
        elif top == 1:
            self.cycles += 10
        else:
            hi = op >> 8
            self.cycles += 12 if hi < 0x08 else 12 + 2 * ((op >> 4) & 15)

        if top >= 4:
            byte = top in (0x5, 0x7, 0x9, 0xB, 0xD, 0xF)
            src = self.operand((op >> 4) & 3, op & 15, byte)
            sv = self.get(src, byte)
            dst = self.operand((op >> 10) & 3, (op >> 6) & 15, byte)
            if top in (0xA, 0xB):
                self.put(dst, self.do_add(self.get(dst, byte), sv, byte), byte)
            elif top in (0x6, 0x7):
                self.put(dst, self.do_sub(self.get(dst, byte), sv, byte), byte)
            elif top in (0x8, 0x9):
                self.compare(sv, self.get(dst, byte), byte)
            elif top in (0xC, 0xD):
                self.flags(sv, byte)
                self.put(dst, sv, byte)
            elif top in (0x4, 0x5):
                self.put(dst, self.flags(self.get(dst, byte) & ~sv, byte), byte)
            else:
                self.put(dst, self.flags(self.get(dst, byte) | sv, byte), byte)
            return

        if top == 3:                                   # LDCR, STCR, MPY, DIV
            sub = (op >> 10) & 3
            if sub == 0:                               # LDCR
                cnt = (op >> 6) & 15
                byte = 1 <= cnt <= 8
                src = self.operand((op >> 4) & 3, op & 15, byte)
                v = self.get(src, byte)
                if (self.reg(12) & 0xFFFE) == 0x0024:
                    self.col = v & 7
                return
            if sub == 1:                               # STCR
                cnt = (op >> 6) & 15
                byte = 1 <= cnt <= 8
                dst = self.operand((op >> 4) & 3, op & 15, byte)
                self.put(dst, 0xFF if byte else 0xFFFF, byte)
                return
            src = self.operand((op >> 4) & 3, op & 15)
            d = (op >> 6) & 15
            sv = self.get(src)
            if sub == 2:                               # MPY
                p = self.reg(d) * sv
                self.setreg(d, (p >> 16) & 0xFFFF)
                self.setreg(d + 1, p & 0xFFFF)
                return
            num = (self.reg(d) << 16) | self.reg(d + 1)  # DIV
            if sv == 0 or self.reg(d) >= sv:
                self.st |= OVF
            else:
                self.setreg(d, num // sv)
                self.setreg(d + 1, num % sv)
            return

        if top == 2:                                   # COC, CZC, XOR, LDCR...
            sub = (op >> 10) & 3
            src = self.operand((op >> 4) & 3, op & 15)
            d = (op >> 6) & 15
            sv = self.get(src)
            if sub == 0:
                self.st = self.st | EQ if (sv & self.reg(d)) == sv else self.st & ~EQ
            elif sub == 1:
                self.st = self.st | EQ if (sv & self.reg(d)) == 0 else self.st & ~EQ
            elif sub == 2:
                self.setreg(d, self.flags(self.reg(d) ^ sv))
            else:
                raise Fault("opcode %04x at %04x" % (op, pc0))
            return

        if top == 1:                                   # jumps and CRU bits
            sub = (op >> 8) & 15
            off = op & 0xFF
            if off > 127:
                off -= 256
            st = self.st
            take = {
                0x0: True,
                0x1: not (st & AGT) and not (st & EQ),           # JLT
                0x2: not (st & LGT) or bool(st & EQ),            # JLE
                0x3: bool(st & EQ),                              # JEQ
                0x4: bool(st & LGT) or bool(st & EQ),            # JHE
                0x5: bool(st & AGT),                             # JGT
                0x6: not (st & EQ),                              # JNE
                0x7: not (st & CAR),                             # JNC
                0x8: bool(st & CAR),                             # JOC
                0x9: not (st & OVF),                             # JNO
                0xA: not (st & LGT) and not (st & EQ),           # JL
                0xB: bool(st & LGT) and not (st & EQ),           # JH
            }
            if sub in take:
                if take[sub]:
                    self.pc = (self.pc + 2 * off) & 0xFFFF
                return
            if sub == 0xF:                             # TB
                # The keyboard matrix is active low, so a pressed key
                # clears EQ and the game tests with JNE
                addr = (self.reg(12) & 0xFFFE) + 2 * off
                hit = (self.col, addr) in self.keys
                self.st = self.st & ~EQ if hit else self.st | EQ
                return
            if sub in (0xD, 0xE):                      # SBO, SBZ
                return
            raise Fault("opcode %04x at %04x" % (op, pc0))

        # top == 0
        hi = op >> 8
        if hi == 0x02:
            sub = (op >> 4) & 15
            r = op & 15
            imm = self.rw(self.pc)
            self.pc += 2
            if sub == 0:
                self.setreg(r, self.flags(imm))
            elif sub == 2:
                self.setreg(r, self.do_add(self.reg(r), imm))
            elif sub == 4:
                self.setreg(r, self.flags(self.reg(r) & imm))
            elif sub == 6:
                self.setreg(r, self.flags(self.reg(r) | imm))
            elif sub == 8:
                self.compare(self.reg(r), imm)
            elif sub == 0x0E:                          # LWPI
                self.wp = imm
            else:
                raise Fault("opcode %04x at %04x" % (op, pc0))
            return
        if hi == 0x03:
            sub = (op >> 4) & 15
            if sub == 0:                               # LIMI
                self.pc += 2
                return
            raise Fault("opcode %04x at %04x" % (op, pc0))
        if 0x04 <= hi <= 0x07:
            sub = (op >> 6) & 15
            src = self.operand((op >> 4) & 3, op & 15)
            if sub == 1:                               # B
                self.pc = src[1]
            elif sub == 3:
                self.put(src, 0)
            elif sub == 4:
                self.put(src, self.do_sub(0, self.get(src)))
            elif sub == 5:
                self.put(src, self.flags(~self.get(src) & 0xFFFF))
            elif sub == 6:
                self.put(src, self.do_add(self.get(src), 1))
            elif sub == 7:
                self.put(src, self.do_add(self.get(src), 2))
            elif sub == 8:
                self.put(src, self.do_sub(self.get(src), 1))
            elif sub == 9:
                self.put(src, self.do_sub(self.get(src), 2))
            elif sub == 0xA:                           # BL
                self.setreg(11, self.pc)
                self.pc = src[1]
            elif sub == 0xB:                           # SWPB
                v = self.get(src)
                self.put(src, ((v << 8) | (v >> 8)) & 0xFFFF)
            elif sub == 0xC:
                self.put(src, 0xFFFF)
            elif sub == 0xD:                           # ABS
                v = self.get(src)
                self.put(src, (0x10000 - v) & 0xFFFF if v & 0x8000 else v)
            else:
                raise Fault("opcode %04x at %04x" % (op, pc0))
            return
        if 0x08 <= hi <= 0x0B:                         # shifts
            sub = hi
            cnt = (op >> 4) & 15
            r = op & 15
            if cnt == 0:
                cnt = self.reg(0) & 15 or 16
            v = self.reg(r)
            # All four shifts set carry from the last bit shifted out.
            if sub == 0x0A:
                res = (v << cnt) & 0xFFFF
                carry = bool((v << cnt) & 0x10000)
            elif sub == 0x09:
                res = v >> cnt
                carry = bool(v & (1 << (cnt - 1)))
            elif sub == 0x08:
                sv = v - 0x10000 if v & 0x8000 else v
                res = (sv >> cnt) & 0xFFFF
                carry = bool(v & (1 << (cnt - 1)))
            else:
                cnt %= 16
                res = ((v >> cnt) | (v << (16 - cnt))) & 0xFFFF
                carry = bool(v & (1 << (cnt - 1))) if cnt else False
            self.setreg(r, self.flags(res))
            self.st = self.st | CAR if carry else self.st & ~CAR
            return
        raise Fault("opcode %04x at %04x" % (op, pc0))


# ---------------------------------------------------------------- screen
def render(m):
    w, h = 256, 192
    px = [[0] * w for _ in range(h)]
    for row in range(24):
        block = row // 8
        for col in range(32):
            ch = m.vram[0x1800 + row * 32 + col]
            pat = 0x0000 + block * 0x800 + ch * 8
            colr = 0x2000 + block * 0x800 + ch * 8
            for line in range(8):
                bits = m.vram[pat + line]
                cc = m.vram[colr + line]
                fg, bg = cc >> 4, cc & 15
                for bit in range(8):
                    c = fg if bits & (0x80 >> bit) else bg
                    px[row * 8 + line][col * 8 + bit] = c or 1
    # sprites
    mag = m.vreg[1] & 1
    size = 16 if m.vreg[1] & 2 else 8
    scale = 2 if mag else 1
    for i in range(32):
        y, x, pat, cc = m.vram[0x1B00 + i * 4: 0x1B00 + i * 4 + 4]
        if y == 0xD0:
            break
        y = (y + 1) & 0xFF
        if y > 192:
            y -= 256
        colour = cc & 15
        if cc & 0x80:
            x -= 32
        base = 0x3800 + (pat & 0xFC) * 8
        for sy in range(size):
            for sx in range(size):
                byte = m.vram[base + (sx // 8) * 16 + (sy % 16)]
                if not (byte & (0x80 >> (sx % 8))):
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        py, pxx = y + sy * scale + dy, x + sx * scale + dx
                        if 0 <= py < h and 0 <= pxx < w and colour:
                            px[py][pxx] = colour
    return px


def write_png(path, px):
    h = len(px)
    w = len(px[0])
    raw = b""
    for row in px:
        raw += b"\x00" + bytes(bytearray(
            [c for p in row for c in PALETTE[p]]))
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    open(path, "wb").write(png)


def dump(m):
    print("VDP registers: %s" % " ".join("%02x" % r for r in m.vreg))
    print("sound bytes: %d" % len(m.sound))
    print("--- name table ---")
    for row in range(24):
        line = ""
        for col in range(32):
            c = m.vram[0x1800 + row * 32 + col]
            line += chr(c) if 32 <= c < 127 else ("." if c == 0 else "#")
        print("%2d |%s|" % (row, line))
    print("--- sprite attributes ---")
    for i in range(12):
        y, x, pat, cc = m.vram[0x1B00 + i * 4: 0x1B00 + i * 4 + 4]
        print("  %2d: y=%3d x=%3d pat=%3d col=%02x" % (i, y, x, pat, cc))
        if y == 0xD0:
            break


KEYMAP = {
    "FIRE": (6, 0x06), "LEFT": (6, 0x08), "RIGHT": (6, 0x0A),
    "DOWN": (6, 0x0C), "UP": (6, 0x0E),
}


def main():
    frames = 200
    png = None
    presses = {}
    for arg in sys.argv[1:]:
        if arg.startswith("--png="):
            png = arg[6:]
        elif arg.startswith("--key="):
            name, at = arg[6:].split("@")
            for k in name.upper().split("+"):
                presses.setdefault(int(at), []).append(k)
        else:
            frames = int(arg)

    rom = open(ROM, "rb").read()
    m = Machine(rom)
    plist = (rom[0x06] << 8) | rom[0x07]
    m.pc = (rom[plist - 0x6000 + 2] << 8) | rom[plist - 0x6000 + 3]
    print("entry point %04x, %d banks" % (m.pc, m.nbanks))

    steps = 0
    last_frame = 0
    stall = 0
    try:
        while m.frames < frames:
            m.step()
            steps += 1
            if m.frames != last_frame:
                last_frame = m.frames
                stall = 0
                held = presses.get(m.frames)
                if held is not None:
                    m.keys = set(KEYMAP[k] for k in held)
                elif presses.get(m.frames - 3) is not None:
                    m.keys = set()
            else:
                stall += 1
                if stall > 4000000:
                    print("FAULT: no frame boundary for 4M instructions, "
                          "pc=%04x" % m.pc)
                    return 1
    except Fault as e:
        print("FAULT at pc %04x after %d steps: %s" % (m.pc, steps, e))
        dump(m)
        return 1

    print("ran %d frames, %d instructions (%d per frame)"
          % (m.frames, steps, steps // max(1, m.frames)))
    dump(m)
    if png:
        write_png(png, render(m))
        print("wrote %s" % png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
