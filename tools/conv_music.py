#!/usr/bin/env python3
"""Turn the PT3 modules into an SN76489 register stream for the TI-99/4A.

tools/pt3.py plays the module and reports what the AY-3-8910 registers
would be on each frame; this script maps those to the SN76489 and writes
the difference between one frame and the next, so that the player on the
TI only has to push a handful of bytes at the sound chip per frame.

The mapping is helped by a happy coincidence: the MSX drives its AY at
1.7897725 MHz and divides by 16, the TI-99/4A drives its SN76489 at
3.579545 MHz and divides by 32, so a tone period means the same pitch on
both machines and transfers unchanged - up to 1023, where the SN76489's
ten bit divider runs out, about 110 Hz.

Below that is where both tunes keep their bass, and an octave shift puts
it in the wrong register (the in-game bass line would be almost all an
octave up, a quarter of it two).  The SN76489 has one way down: noise in
periodic mode clocked by tone channel 3 is a pulse at one fifteenth of
that channel's frequency, so tone 3 runs silently at fifteen times the
note and the noise channel plays the bass at its real pitch.  Measured
A-weighted, that pulse is as loud as the AY square it replaces at the
same attenuation (+0.3 dB), so the level carries over unchanged.

It costs two channels: tone 3 and the noise.  A frame gets the periodic
bass when one voice is below 1023, no drum wants the noise channel and
channel C is not playing a note of its own.  While a drum has the noise
channel the low note rests (in these tunes that is the attack frame of a
bass note, where the AY mixes noise into it), and the rare frame with two
low voices plays the higher one an octave up.

What cannot transfer:
  - the AY plays noise and tone through one channel, the SN76489 has a
    separate noise channel, so drums are given that channel.
  - the AY envelope.  Neither tune uses it, and the converter stops
    rather than guess if a tune ever does.

Stream format, read by musply in src/jawbreaker.a99:
    data  loop offset, from the start of the stream
    data  the tone channel sound effects should take while this plays
    0x00-0x7e   n bytes follow, write them all to the sound chip
    0x7f        end of the stream, jump to the loop point
    0x80-0xff   (b & 0x7f) frames with nothing to do
"""

import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import pt3                                                   # noqa: E402

AY_CLOCK = 1789772.5
SN_CLOCK = 3579545.0

# AY-3-8910 output level for each of its 16 volume settings, as measured
# for the ayumi emulator (github.com/true-grue/ayumi, AY_dac_table)
AY_DAC = [0.0, 0.00999465934234, 0.0144502937362, 0.0210574502174,
          0.0307011520562, 0.0455481803616, 0.0644998855573, 0.107362478065,
          0.126588845655, 0.20498970016, 0.292210269322, 0.372838941024,
          0.492530708782, 0.635324635691, 0.805584802014, 1.0]


def _attenuation(v):
    """SN76489 attenuation (2 dB a step) nearest to AY volume v.

    15 is silence on the SN76489, so anything the AY still plays gets 14
    at the most rather than disappear.
    """
    if v == 0:
        return 15
    db = -20.0 * math.log10(AY_DAC[v])
    return min(14, int(db / 2.0 + 0.5))


AY2SN = [_attenuation(v) for v in range(16)]

LOW = 1023                          # the largest SN76489 tone divider


def sn_tone(period):
    """AY tone period to SN76489 divider, an octave up until it fits."""
    if period < 1:
        return 1, 0
    shift = 0
    while period > LOW:
        period = (period + 1) >> 1                           # rounded
        shift += 1
    return period, shift


def noise_mode(period):
    """AY noise period to the closest of the three SN76489 noise rates."""
    if period < 1:
        period = 1
    freq = AY_CLOCK / (16.0 * period)
    best, bestd = 0, None
    for rate, div in enumerate((512.0, 1024.0, 2048.0)):
        d = abs(SN_CLOCK / div - freq)
        if bestd is None or d < bestd:
            best, bestd = rate, d
    return 0xE4 | best                                       # white noise


PERIODIC = 0xE3                     # periodic noise, clocked by tone 3


def plan(f):
    """What each SN76489 channel does on one frame.

    Returns (tones, noise): tones is three (divider, attenuation, shift)
    tuples, noise is (control byte or None, attenuation), and the voice
    that went to periodic noise, or None.
    """
    vol, per, tone_on, noise_on = [], [], [], []
    for i in range(3):
        a = f["ampl"][i]
        if a & 0x10:
            raise SystemExit("the tune uses the AY envelope, which this "
                             "converter does not handle")
        vol.append(a & 15)
        per.append(f["tone"][i] & 0xFFF)
        tone_on.append(bool(vol[i]) and not (f["mixer"] >> i) & 1)
        noise_on.append(bool(vol[i]) and not (f["mixer"] >> (i + 3)) & 1)

    drum = max((vol[i] for i in range(3) if noise_on[i]), default=0)
    low = [i for i in range(3) if tone_on[i] and per[i] > LOW]
    c_note = tone_on[2] and per[2] <= LOW
    bass = None
    if low and not drum and not c_note:
        bass = max(low, key=lambda i: per[i])                # the lowest

    tones = []
    for i in range(3):
        if bass is not None and i == 2:
            # tone 3 is the clock: fifteen times the bass note, silent
            tones.append((max(1, round(per[bass] / 15.0)), 15, 0))
        elif i == bass or not tone_on[i] or (i in low and drum):
            # a drum takes the noise channel: in these tunes that is the
            # one frame attack of a bass note, so the note starts a frame
            # late at the right pitch rather than a frame early an octave
            # too high
            tones.append((None, 15, 0))
        else:
            div, sh = sn_tone(per[i])
            tones.append((div, AY2SN[vol[i]], sh))
    if bass is not None:
        noise = (PERIODIC, AY2SN[vol[bass]])
    elif drum:
        noise = (noise_mode(f["noise"]), AY2SN[drum])
    else:
        noise = (None, 15)
    return tones, noise, bass


def convert(path, verbose=False):
    p, frames = pt3.render(path)
    loop_frame = p.loop_frame
    out = []                                                 # per frame: bytes
    state = {"tone": [None, None, None], "att": [15, 15, 15, 15], "noise": None}
    shifted = periodic = 0
    busy = [0, 0, 0]                                         # frames in use
    cents = []
    for f in frames:
        writes = []
        tones, noise, bass = plan(f)
        if bass is not None:
            periodic += 1
            n = tones[2][0]
            want = f["tone"][bass] & 0xFFF
            cents.append(abs(1200 * math.log2(15.0 * n / want)))
        for i, (div, att, sh) in enumerate(tones):
            shifted += 1 if sh else 0
            if div is not None and (att < 15 or (bass is not None and i == 2)):
                busy[i] += 1
                if div != state["tone"][i]:
                    state["tone"][i] = div
                    writes.append(0x80 | (i << 5) | (div & 15))
                    writes.append((div >> 4) & 0x3F)
            if att != state["att"][i]:
                state["att"][i] = att
                writes.append(0x90 | (i << 5) | att)
        mode, att = noise
        # writing the noise control restarts its shift register, so only
        # ever when the mode really changes
        if mode is not None and mode != state["noise"]:
            state["noise"] = mode
            writes.append(mode)
        if att != state["att"][3]:
            state["att"][3] = att
            writes.append(0xF0 | att)
        out.append(writes)

    # sound effects take whichever tone channel the tune needs least
    fx_channel = min(range(3), key=lambda i: (busy[i], -i))

    # --- pack
    packed = bytearray()
    frame_offset = []
    run = 0
    for writes in out:
        if not writes:
            run += 1
            if run == 0x7F:
                frame_offset.append(len(packed))
                packed.append(0x80 | run)
                run = 0
            else:
                frame_offset.append(None)
            continue
        if run:
            packed.append(0x80 | run)
            run = 0
        frame_offset.append(len(packed))
        packed.append(len(writes))
        packed.extend(writes)
    if run:
        packed.append(0x80 | run)
    packed.append(0x7F)                                      # loop

    # where in the packed stream does the loop point sit
    loop_off = 0
    for i in range(loop_frame, len(frame_offset)):
        if frame_offset[i] is not None:
            loop_off = frame_offset[i]
            break
    if verbose:
        print("%-16s %5d frames (%.1f s), %5d bytes, loop at %d"
              % (os.path.basename(path), len(frames), len(frames) / 50.0,
                 len(packed), loop_off))
        print("%16s %5d frames of bass on periodic noise (worst %.1f cents off),"
              " %d octave shifted" % ("", periodic, max(cents, default=0), shifted))
        print("%16s tone channels busy %s frames, effects go on channel %d"
              % ("", busy, fx_channel))
    return packed, loop_off, fx_channel


def emit(name, packed, loop_off, fx_channel, out):
    out.write("*\n* %s: %d bytes\n*\n" % (name, len(packed)))
    out.write("%s data %d\n" % (name, loop_off))
    out.write("       data %d                         ; Channel for sound effects\n"
              % fx_channel)
    data = bytes(packed)
    if len(data) % 2:
        data += b"\x00"
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        words = [">%02x%02x" % (chunk[j], chunk[j + 1]) for j in range(0, len(chunk), 2)]
        out.write("       data %s\n" % ",".join(words))


def main():
    # one tune per bank: together they are bigger than a bank
    tunes = [("musint", "jawintro.pt3", "music-intro.a99"),
             ("musgam", "jawingame.pt3", "music-game.a99")]
    print("AY volume -> SN attenuation:", AY2SN)
    for label, tune, fname in tunes:
        packed, loop_off, fx = convert(os.path.join(ROOT, "msx", "Music", tune), True)
        path = os.path.join(ROOT, "src", fname)
        with open(path, "w") as out:
            out.write("* ------------------------------------------------------------------\n")
            out.write("* Music - generated by tools/conv_music.py, do not edit\n")
            out.write("* Source: msx/Music/%s, played by the replayer in tools/pt3.py\n" % tune)
            out.write("* First word: offset of the loop point in the stream.  Second: the\n")
            out.write("* tone channel sound effects take while this tune plays.\n")
            out.write("* ------------------------------------------------------------------\n")
            emit(label, packed, loop_off, fx, out)
        print("   -> src/%s" % fname)


if __name__ == "__main__":
    main()
