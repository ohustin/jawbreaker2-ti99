#!/usr/bin/env python3
"""Check what the sound chip actually plays against what the tune asks for.

Runs the cartridge in tools/sim99.py through the attraction screen, the
menu and a stretch of play with sound effects going off, and records
every byte written to the SN76489 frame by frame.  In parallel it
decodes the music stream itself, following the player's position in
the scratch pad, into a "music only" chip.  Then, on every frame:

  - with no effect playing, the real chip must be exactly the music chip
  - with an effect on a channel, every other channel must be the music
    chip, and if the effect sits on tone 3 while the tune has the noise
    in periodic mode (the bass), the noise must be silent rather than
    clocked by the effect
  - whenever periodic noise is audible, tone 3 must be the tune's own
    divider, so the bass is never played at an effect's pitch

    python tools/musicheck.py [frames]
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import sim99                                                 # noqa: E402

SN = 3579545.0


def symbols():
    syms = {}
    name = None
    for line in open(os.path.join(ROOT, "build", "sym.a99")):
        line = line.strip()
        if line.endswith(":"):
            name = line[:-1].lower()
        elif line.startswith("equ") and name:
            syms[name] = int(line.split()[1].lstrip(">"), 16)
            name = None
    return syms


class Chip(object):
    """SN76489 register state, fed byte by byte."""

    def __init__(self):
        self.div = [0, 0, 0]
        self.att = [15, 15, 15, 15]
        self.noise = None
        self.latch = 0

    def write(self, b):
        if b & 0x80:
            ch = (b >> 5) & 3
            self.latch = ch
            if b & 0x10:
                self.att[ch] = b & 15
            elif ch == 3:
                self.noise = b & 7
            else:
                self.div[ch] = (self.div[ch] & 0x3F0) | (b & 15)
        elif self.latch < 3:
            ch = self.latch
            self.div[ch] = (self.div[ch] & 15) | ((b & 0x3F) << 4)

    def voice(self, ch):
        """What channel ch sounds like, or None when silent."""
        if self.att[ch] == 15:
            return None
        if ch < 3:
            return ("tone", self.div[ch], self.att[ch])
        if self.noise is not None and self.noise == 3:
            return ("periodic", self.div[2], self.att[3])
        return ("noise", self.noise, self.att[3])


def main():
    frames = int(sys.argv[1]) if len(sys.argv) > 1 else 2400
    s = symbols()
    rom = open(sim99.ROM, "rb").read()
    m = sim99.Machine(rom)
    plist = (rom[6] << 8) | rom[7]
    m.pc = (rom[plist - 0x6000 + 2] << 8) | rom[plist - 0x6000 + 3]

    def word(a):
        return (m.pad[a - 0x8300] << 8) | m.pad[a - 0x8300 + 1]

    def byte(a):
        return m.pad[a - 0x8300]

    # attraction screen, menu (moving the selection sets off effects over
    # the intro tune), then play, turning every so often to eat dots, and
    # a new game whenever the last one is over
    script = {20: "FIRE", 30: "RIGHT", 36: "LEFT", 42: "RIGHT", 60: "FIRE"}
    turns = ["LEFT", "UP", "RIGHT", "DOWN", "RIGHT", "UP", "LEFT", "DOWN"]
    for i, f in enumerate(range(200, frames, 45)):
        script[f] = turns[i % len(turns)]
    in_menu = (3, 4)                        # mmenu, mattr

    real, music = Chip(), Chip()
    fed = 0
    last_ptr = 0
    errors = []
    stats = {"frames": 0, "effect": 0, "periodic": 0, "effects on": {},
             "bass notes": set(), "tunes": set()}
    last_frame = 0
    while m.frames < frames:
        m.step()
        if m.frames == last_frame:
            continue
        last_frame = m.frames
        key = script.get(m.frames)
        if m.frames > 100 and byte(s["cmode"]) in in_menu:
            key = "FIRE" if m.frames % 40 < 3 else None
            if key is None:
                m.keys = set()
        if key:
            m.keys = {sim99.KEYMAP[key]}
        elif script.get(m.frames - 3) and script[m.frames - 3] == "FIRE":
            m.keys = set()

        # what reached the chip this frame
        for b in m.sound[fed:]:
            real.write(b)
        fed = len(m.sound)

        # what the tune asked for: follow the player through the stream
        ptr = word(s["musptr"])
        if ptr == 0:
            music = Chip()
            last_ptr = 0
        else:
            bank = (word(s["musbnk"]) - 0x6000) >> 1
            stats["tunes"].add(bank)

            def rb(a):
                return rom[bank * 0x2000 + a - 0x6000]
            p = last_ptr
            if last_ptr == 0 or (bank, 0) != stats.get("playing"):
                # a tune has just started and the player may have played
                # its first frame already: start from the top of the
                # stream, after the two header words
                music = Chip()
                p = s["musint" if bank == 2 else "musgam"] + 4
                stats["playing"] = (bank, 0)
            guard = 0
            while p != ptr and guard < 400:
                guard += 1
                b = rb(p)
                if b == 0x7F:
                    p = word(s["muslop"])
                    continue
                if b & 0x80:
                    p += 1
                    continue
                for i in range(b):
                    music.write(rb(p + 1 + i))
                p += 1 + b
            if guard >= 400:
                errors.append((m.frames, "lost track of the stream"))
            last_ptr = ptr
        if ptr == 0 or m.frames < 3:
            continue

        stats["frames"] += 1
        k = "intro" if bank == 2 else "game"
        stats[k] = stats.get(k, 0) + 1
        eff = word(s["sndptr"])
        chn = word(s["sndchn"]) if eff else None
        if eff:
            stats["effect"] += 1
            k = ("intro" if bank == 2 else "game", chn)
            stats["effects on"][k] = stats["effects on"].get(k, 0) + 1
        periodic_music = music.noise == 3
        for ch in range(4):
            if chn is not None and ch == chn:
                continue
            want = music.voice(ch)
            got = real.voice(ch)
            if ch == 3 and chn == 2 and periodic_music:
                want = None                      # muted while clocked
            if ch == 2 and want is None and got is None:
                continue
            if want != got:
                errors.append((m.frames, "channel %d: tune %s, chip %s, effect on %s"
                               % (ch, want, got, chn)))
        v = real.voice(3)
        if v and v[0] == "periodic":
            stats["periodic"] += 1
            stats["bass notes"].add(round(SN / (32.0 * real.div[2] * 15), 1))
            if chn == 2:
                errors.append((m.frames, "bass clocked by an effect"))
            if real.div[2] != music.div[2]:
                errors.append((m.frames, "bass clock %d, tune wants %d"
                               % (real.div[2], music.div[2])))

    print("checked %d frames with music (intro %d, in-game %d), %d of them "
          "with an effect playing" % (stats["frames"], stats.get("intro", 0),
                                      stats.get("game", 0), stats["effect"]))
    print("tunes seen: %s, effect frames by (tune, channel): %s"
          % (sorted(stats["tunes"]), stats["effects on"]))
    print("periodic bass audible on %d frames, notes (Hz): %s"
          % (stats["periodic"], sorted(stats["bass notes"])))
    if errors:
        print("%d mismatches, first ones:" % len(errors))
        for e in errors[:15]:
            print("  frame %d: %s" % e)
        return 1
    print("no mismatches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
