#!/usr/bin/env python3
"""A PT3 replayer, ported from PT3-ROM.ASM, the Z80 replayer of the MSX version.

This is the same algorithm the MSX version runs at 50 Hz, only written in
Python and run at build time: it plays the module and records what the
AY-3-8910 registers would be on every frame.  tools/conv_music.py then
turns that register stream into something an SN76489 can play.

The channel and player variables keep the names they have in the Z80
source so that the two can be read side by side.
"""

import sys


# Note table 2, copied from the NT_ table at the end of PT3-ROM.ASM
NOTE_TABLE = [
    0x0D10, 0x0C55, 0x0BA4, 0x0AFC, 0x0A5F, 0x09CA, 0x093D, 0x08B8,
    0x083B, 0x07C5, 0x0755, 0x06EC, 0x0688, 0x062A, 0x05D2, 0x057E,
    0x052F, 0x04E5, 0x049E, 0x045C, 0x041D, 0x03E2, 0x03AB, 0x0376,
    0x0344, 0x0315, 0x02E9, 0x02BF, 0x0298, 0x0272, 0x024F, 0x022E,
    0x020F, 0x01F1, 0x01D5, 0x01BB, 0x01A2, 0x018B, 0x0174, 0x0160,
    0x014C, 0x0139, 0x0128, 0x0117, 0x0107, 0x00F9, 0x00EB, 0x00DD,
    0x00D1, 0x00C5, 0x00BA, 0x00B0, 0x00A6, 0x009D, 0x0094, 0x008C,
    0x0084, 0x007C, 0x0075, 0x006F, 0x0069, 0x0063, 0x005D, 0x0058,
    0x0053, 0x004E, 0x004A, 0x0046, 0x0042, 0x003E, 0x003B, 0x0037,
    0x0034, 0x0031, 0x002F, 0x002C, 0x0029, 0x0027, 0x0025, 0x0023,
    0x0021, 0x001F, 0x001D, 0x001C, 0x001A, 0x0019, 0x0017, 0x0016,
    0x0015, 0x0014, 0x0012, 0x0011, 0x0010, 0x000F, 0x000E, 0x000D,
]

EMPTY_SAMORN = bytes([0, 1, 0, 0x90])


def build_volume_table():
    """The INITV1/INITV2 loop of the Z80 player, carry flags and all."""
    vt = [0] * 256
    hl = 0x0011
    de = 0x0000
    ix = 16
    b = 15
    carry = 0
    while True:
        saved = hl
        t = hl + de
        carry = 1 if t > 0xFFFF else 0
        hl = t & 0xFFFF
        hl, de = de, hl                     # ex de,hl
        hl = (-carry) & 0xFFFF              # sbc hl,hl
        c = b
        b = 16
        while True:
            lo = hl & 0xFF
            hi = (hl >> 8) & 0xFF
            rla_carry = 1 if lo & 0x80 else 0   # ld a,l / rla
            a = hi + rla_carry                  # ld a,h / adc a,0
            carry = 1 if a > 0xFF else 0
            vt[ix] = a & 0xFF
            ix += 1
            t = hl + de
            carry = 1 if t > 0xFFFF else 0
            hl = t & 0xFFFF
            b -= 1
            if b == 0:
                break
        hl = saved
        if (de & 0xFF) == 0x77:
            de = (de & 0xFF00) | 0x78
        b = c - 1
        if b == 0:
            break
    return vt


VOLUME_TABLE = build_volume_table()


def s8(v):
    v &= 0xFF
    return v - 256 if v & 0x80 else v


def s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


class Channel(object):
    def __init__(self):
        self.PsInOr = 0
        self.PsInSm = 0
        self.CrAmSl = 0
        self.CrNsSl = 0
        self.CrEnSl = 0
        self.TSlCnt = 0
        self.CrTnSl = 0
        self.TnAcc = 0
        self.COnOff = 0
        self.OnOffD = 0
        self.OffOnD = 0
        self.OrnPtr = 0
        self.SamPtr = 0
        self.NNtSkp = 0
        self.Note = 0
        self.SlToNt = 0
        self.Env_En = 0
        self.Flags = 0
        self.TnSlDl = 0
        self.TSlStp = 0
        self.TnDelt = 0
        self.NtSkCn = 1
        self.Volume = 0xF0
        self.ptr = 0                        # position in the pattern data

    def reset_group(self):
        """The twelve bytes PD_RES clears by pushing zeroes."""
        self.PsInOr = 0
        self.PsInSm = 0
        self.CrAmSl = 0
        self.CrNsSl = 0
        self.CrEnSl = 0
        self.TSlCnt = 0
        self.CrTnSl = 0
        self.TnAcc = 0
        self.COnOff = 0


class PT3(object):
    def __init__(self, data):
        self.mod = bytearray(data)
        self.mem = self.mod                 # module addresses are file offsets
        self.version = 6
        if len(data) > 13 and 0x30 <= data[13] <= 0x39:
            self.version = data[13] - 0x30
        self.Delay = self.mod[100]
        self.NumPos = self.mod[101]
        self.LoopPos = self.mod[102]
        self.PatsPtr = self.word(103)
        self.SamPtrs = 105
        self.OrnPtrs = 169
        self.PosList = 201
        self.CrPsPtr = 200                  # the player increments first
        self.LPosPtr = 200 + self.LoopPos + 1
        self.chan = [Channel(), Channel(), Channel()]
        self.DelyCnt = 1
        self.Ns_Base = 0
        self.AddToNs = 0
        self.AddToEn = 0
        self.EnvBase = 0
        self.CurESld = 0
        self.CurEDel = 0
        self.Env_Del = 0
        self.ESldAdd = 0
        self.PrNote = 0
        self.PrSlide = 0
        self.looped = False
        self.loop_frame = 0
        self.frame = 0
        self.pos_frames = []
        self.empty = len(self.mod)
        self.mod += EMPTY_SAMORN            # EMPTYSAMORN lives outside the module
        for ch in self.chan:
            ch.OrnPtr = self.empty
            ch.SamPtr = self.empty
            ch.ptr = self.empty             # points at a zero, like AdInPtA
        # AY registers
        self.Ton = [0, 0, 0]
        self.Ampl = [0, 0, 0]
        self.Noise = 0
        self.Mixer = 0
        self.Env = 0
        self.EnvTp = 0xFF

    def byte(self, a):
        return self.mod[a] if 0 <= a < len(self.mod) else 0

    def word(self, a):
        return self.byte(a) | (self.byte(a + 1) << 8)

    # ---------------------------------------------------------------- decoder
    def set_orn(self, ch, n):
        ch.PsInOr = 0
        ch.OrnPtr = self.word(self.OrnPtrs + n * 2)

    def set_sam(self, ch, n):
        ch.SamPtr = self.word(self.SamPtrs + n * 2)

    def set_env(self, ch, envtp):
        ch.Env_En = 0x10
        self.EnvTp = envtp
        hi = self.byte(ch.ptr)
        ch.ptr += 1
        lo = self.byte(ch.ptr)
        ch.ptr += 1
        self.EnvBase = (hi << 8) | lo
        ch.PsInOr = 0
        self.CurEDel = 0
        self.CurESld = 0

    def ptdecod(self, ch):
        self.PrNote = ch.Note
        self.PrSlide = ch.CrTnSl
        commands = []
        while True:
            x = self.byte(ch.ptr)
            ch.ptr += 1
            if x >= 0xF0:                                   # ornament + sample
                ch.Env_En = 0
                self.set_orn(ch, x - 0xF0)
                b = self.byte(ch.ptr)
                ch.ptr += 1
                self.set_sam(ch, b >> 1)
                continue
            if x == 0xD0:                                   # end of the row
                break
            if x > 0xD0:                                    # sample
                self.set_sam(ch, x - 0xD0)
                continue
            if x == 0xC0:                                   # note off
                ch.Flags &= ~1
                ch.reset_group()
                break
            if x > 0xC0:                                    # volume
                ch.Volume = (x - 0xC0) << 4
                continue
            if x == 0xB0:                                   # envelope off
                ch.Env_En = 0
                ch.PsInOr = 0
                continue
            if x > 0xB0:
                if x == 0xB1:                               # rows to skip
                    ch.NNtSkp = self.byte(ch.ptr)
                    ch.ptr += 1
                else:                                       # envelope
                    self.set_env(ch, x - 0xB1)
                continue
            if x >= 0x50:                                   # note
                ch.Note = x - 0x50
                ch.Flags |= 1
                ch.reset_group()
                break
            if x >= 0x40:                                   # ornament
                self.set_orn(ch, x - 0x40)
                continue
            if x >= 0x20:                                   # noise base
                self.Ns_Base = x - 0x20
                continue
            if x >= 0x10:                                   # envelope + sample
                n = x - 0x10
                ch.Env_En = 0x10 if n else 0
                ch.PsInOr = 0
                if n:
                    self.set_env(ch, n)
                else:
                    ch.Env_En = 0
                b = self.byte(ch.ptr)
                ch.ptr += 1
                self.set_sam(ch, b >> 1)
                continue
            commands.append(x)                              # 1-15: a command
        ch.NtSkCn = ch.NNtSkp
        for cmd in reversed(commands):                      # they are stacked
            self.command(ch, cmd)

    def command(self, ch, cmd):
        if cmd == 1:                                        # gliss
            ch.Flags |= 4
            ch.TnSlDl = self.byte(ch.ptr)
            ch.TSlCnt = ch.TnSlDl
            ch.ptr += 1
            lo = self.byte(ch.ptr)
            ch.ptr += 1
            hi = self.byte(ch.ptr)
            ch.ptr += 1
            ch.TSlStp = s16(lo | (hi << 8))
            ch.COnOff = 0
        elif cmd == 2:                                      # portamento
            ch.Flags &= ~4
            ch.TnSlDl = self.byte(ch.ptr)
            ch.ptr += 3                     # skips the stale delta
            ch.TSlCnt = ch.TnSlDl
            ch.SlToNt = ch.Note
            target = NOTE_TABLE[min(ch.Note, 95)]
            ch.Note = self.PrNote
            src = NOTE_TABLE[min(ch.Note, 95)]
            ch.TnDelt = s16(target - src)
            ch.CrTnSl = self.PrSlide
            lo = self.byte(ch.ptr)
            ch.ptr += 1
            hi = self.byte(ch.ptr)
            ch.ptr += 1
            step = lo | (hi << 8)
            if hi:                          # the sign test swaps operands
                diff = s16(self.PrSlide - ch.TnDelt)
            else:
                diff = s16(ch.TnDelt - self.PrSlide)
            if diff < 0:
                step = -step
            ch.TSlStp = s16(step)
            ch.COnOff = 0
        elif cmd == 3:                                      # sample position
            ch.PsInSm = self.byte(ch.ptr)
            ch.ptr += 1
        elif cmd == 4:                                      # ornament position
            ch.PsInOr = self.byte(ch.ptr)
            ch.ptr += 1
        elif cmd == 5:                                      # vibrato
            ch.OnOffD = self.byte(ch.ptr)
            ch.COnOff = ch.OnOffD
            ch.ptr += 1
            ch.OffOnD = self.byte(ch.ptr)
            ch.ptr += 1
            ch.TSlCnt = 0
            ch.CrTnSl = 0
        elif cmd == 8:                                      # envelope glissando
            self.Env_Del = self.byte(ch.ptr)
            self.CurEDel = self.Env_Del
            ch.ptr += 1
            lo = self.byte(ch.ptr)
            ch.ptr += 1
            hi = self.byte(ch.ptr)
            ch.ptr += 1
            self.ESldAdd = s16(lo | (hi << 8))
        elif cmd == 9:                                      # tempo
            self.Delay = self.byte(ch.ptr)
            ch.ptr += 1

    # ---------------------------------------------------------------- channel
    def chregs(self, ch, index):
        ampl = 0
        if not (ch.Flags & 1):
            self.mixer_bits(0)
            self.vibrato(ch)
            self.Ampl[index] = 0
            return
        # ornament
        orn = ch.OrnPtr
        orn_loop = self.byte(orn)
        orn_len = self.byte(orn + 1)
        pos = ch.PsInOr
        offset = s8(self.byte(orn + 2 + pos))
        nxt = pos + 1
        ch.PsInOr = nxt if nxt < orn_len else orn_loop
        note = ch.Note + offset
        if note < 0:
            note = 0
        if note > 95:
            note = 95
        # sample
        sam = ch.SamPtr
        sam_loop = self.byte(sam)
        sam_len = self.byte(sam + 1)
        pos = ch.PsInSm
        entry = sam + 2 + pos * 4
        nxt = pos + 1
        ch.PsInSm = nxt if nxt < sam_len else sam_loop
        c = self.byte(entry)
        b = self.byte(entry + 1)
        delta = self.word(entry + 2)
        acc = (delta + ch.TnAcc) & 0xFFFF
        if b & 0x40:
            ch.TnAcc = acc
        tone = (NOTE_TABLE[note] + acc + ch.CrTnSl) & 0xFFFF
        # tone slide
        if ch.TSlCnt:
            ch.TSlCnt -= 1
            if ch.TSlCnt == 0:
                ch.TSlCnt = ch.TnSlDl
                negative_step = (ch.TSlStp >> 8) & 0xFF00 != 0 or ch.TSlStp < 0
                ch.CrTnSl = s16(ch.CrTnSl + ch.TSlStp)
                if not (ch.Flags & 4):
                    if negative_step:
                        reached = s16(ch.TnDelt - ch.CrTnSl) >= 0
                    else:
                        reached = s16(ch.CrTnSl - ch.TnDelt) >= 0
                    if reached:
                        ch.Note = ch.SlToNt
                        ch.TSlCnt = 0
                        ch.CrTnSl = 0
        # amplitude
        a = s8(ch.CrAmSl)
        if c & 0x80:
            if c & 0x40:
                if a != 15:
                    a += 1
            else:
                if a != -15:
                    a -= 1
            ch.CrAmSl = a & 0xFF
        a = (b & 15) + a
        if a < 0:
            a = 0
        if a > 15:
            a = 15
        ampl = VOLUME_TABLE[(ch.Volume | a) & 0xFF]
        if not (c & 1):
            ampl |= ch.Env_En
        self.Ampl[index] = ampl
        # envelope slide or noise
        if b & 0x80:
            v = ((c << 2) & 0xFF)
            v = s8(v) >> 3
            v = v + s8(ch.CrEnSl)
            if b & 0x20:
                ch.CrEnSl = v & 0xFF
            self.AddToEn = (self.AddToEn + v) & 0xFF
        else:
            v = (c >> 1) + s8(ch.CrNsSl)
            self.AddToNs = v & 0xFF
            if b & 0x20:
                ch.CrNsSl = v & 0xFF
        self.Ton[index] = tone
        self.mixer_bits((b >> 1) & 0x48)
        self.vibrato(ch)

    def mixer_bits(self, bits):
        m = (self.Mixer | bits) & 0xFF
        self.Mixer = ((m >> 1) | ((m & 1) << 7)) & 0xFF     # rrca

    def vibrato(self, ch):
        if ch.COnOff == 0:
            return
        ch.COnOff -= 1
        if ch.COnOff:
            return
        ch.Flags ^= 1
        ch.COnOff = ch.OnOffD if (ch.Flags & 1) else ch.OffOnD

    # ------------------------------------------------------------------ frame
    def play(self):
        """One frame.  Returns False once the module has looped."""
        self.frame += 1
        self.AddToEn = 0
        self.Mixer = 0
        self.EnvTp = 0xFF
        self.DelyCnt -= 1
        if self.DelyCnt == 0:
            for i, ch in enumerate(self.chan):
                ch.NtSkCn -= 1
                if ch.NtSkCn:
                    continue
                if i == 0 and self.byte(ch.ptr) == 0:
                    self.new_position()
                self.ptdecod(ch)
            self.DelyCnt = self.Delay
        for i, ch in enumerate(self.chan):
            self.chregs(ch, i)
        self.Noise = (self.Ns_Base + self.AddToNs) & 0xFF
        env = (self.EnvBase + s8(self.AddToEn) + self.CurESld) & 0xFFFF
        self.Env = env
        if self.CurEDel:
            self.CurEDel -= 1
            if self.CurEDel == 0:
                self.CurEDel = self.Env_Del
                self.CurESld = s16(self.CurESld + self.ESldAdd)
        return not self.looped

    def new_position(self):
        self.Ns_Base = 0
        self.CrPsPtr += 1
        pos = self.byte(self.CrPsPtr)
        if pos == 0xFF or self.CrPsPtr - self.PosList >= self.NumPos:
            self.looped = True
            self.CrPsPtr = self.LPosPtr
            pos = self.byte(self.CrPsPtr)
        index = self.CrPsPtr - self.PosList
        if index == self.LoopPos and not self.loop_frame:
            self.loop_frame = self.frame            # where the module repeats
        addr = self.PatsPtr + pos * 2
        for i, ch in enumerate(self.chan):
            ch.ptr = self.word(addr + i * 2)


def render(path, max_frames=20000):
    """Play the module and return the AY register state of every frame."""
    p = PT3(open(path, "rb").read())
    frames = []
    while len(frames) < max_frames:
        more = p.play()
        frames.append({
            "tone": list(p.Ton),
            "ampl": list(p.Ampl),
            "noise": p.Noise & 31,
            "mixer": p.Mixer,
            "env": p.Env,
            "envtp": p.EnvTp,
        })
        if not more:
            break
    return p, frames


def main():
    for path in sys.argv[1:]:
        p, frames = render(path)
        notes = sum(1 for f in frames if any(
            (f["ampl"][i] & 15) and not (f["mixer"] >> i) & 1 for i in range(3)))
        print("%s: %d positions, delay %d, %d frames (%.1f s), %d with tone"
              % (path, p.NumPos, p.Delay, len(frames), len(frames) / 50.0, notes))


if __name__ == "__main__":
    main()
