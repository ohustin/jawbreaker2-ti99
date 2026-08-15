#!/usr/bin/env python3
"""Turn the PT3 modules into an SN76489 register stream for the TI-99/4A.

tools/pt3.py plays the module and reports what the AY-3-8910 registers
would be on each frame; this script maps those to the SN76489 and writes
the difference between one frame and the next, so that the player on the
TI only has to push a handful of bytes at the sound chip per frame.

The mapping is helped by a happy coincidence: the MSX drives its AY at
1.7897725 MHz and divides by 16, the TI-99/4A drives its SN76489 at
3.579545 MHz and divides by 32, so a tone period means the same pitch on
both machines and transfers unchanged.

What cannot transfer:
  - the AY envelope.  Where PT3 uses it for its buzzy bass the pitch is
    still in the tone register, so the note survives; the timbre does
    not.  Slow envelopes (used as fades) are simulated and become a
    volume, fast ones become a fixed level.
  - the AY plays noise and tone through one channel, the SN76489 has a
    separate noise channel, so drums are given that channel.

Frame format, read by sndmus in src/sound.a99:
    0x00-0x7e   n bytes follow, write them all to the sound chip
    0x7f        end of the stream, jump to the loop point
    0x80-0xff   (b & 0x7f) frames with nothing to do
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import pt3                                                   # noqa: E402

AY_CLOCK = 1789772.5
SN_CLOCK = 3579545.0

# AY amplitude (0-15, roughly 3 dB a step) to SN76489 attenuation
# (0 loudest, 15 silent, 2 dB a step)
AY2SN = [15, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1, 0]

# AY envelope shapes: does the level rise, and does it repeat
ENV_SHAPES = {
    8: ("down", True), 9: ("down", False), 10: ("downup", True),
    11: ("down", False), 12: ("up", True), 13: ("up", False),
    14: ("updown", True), 15: ("up", False),
}


def env_level(shape, phase):
    """Level 0-15 of the AY envelope generator at a given 32 step phase."""
    kind, repeat = ENV_SHAPES.get(shape & 15, ("down", False))
    if not repeat and phase >= 16:
        return 0 if kind in ("down", "downup") else 15
    step = phase & 15
    if kind == "down":
        return 15 - step
    if kind == "up":
        return step
    if kind == "downup":
        return 15 - step if (phase & 16) == 0 else step
    return step if (phase & 16) == 0 else 15 - step


def sn_tone(period):
    """AY tone period to SN76489 divider, shifted up until it fits."""
    if period < 1:
        return 1, 0
    shift = 0
    while period > 1023:
        period >>= 1
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


def convert(path, verbose=False):
    p, frames = pt3.render(path)
    loop_frame = p.loop_frame
    out = []                                                 # per frame: bytes
    state = {"tone": [None, None, None], "att": [15, 15, 15, 15], "noise": None}
    env_phase = 0.0
    shifted = 0
    for f in frames:
        writes = []
        # --- envelope level for this frame
        env_period = f["env"] or 1
        env_step_hz = AY_CLOCK / (256.0 * env_period)
        cycle_hz = env_step_hz / 16.0
        if cycle_hz < 30.0:                                  # slow: a fade
            env_phase = (env_phase + env_step_hz / 50.0) % 32
            level = env_level(f["envtp"], int(env_phase))
        else:                                                # fast: a timbre
            level = 13
        # --- the three tone channels
        noise_amp = 0
        for i in range(3):
            tone_on = not (f["mixer"] >> i) & 1
            noise_on = not (f["mixer"] >> (i + 3)) & 1
            amp = f["ampl"][i]
            amp = level if amp & 0x10 else amp & 15
            if noise_on and amp > noise_amp:
                noise_amp = amp
            att = AY2SN[amp] if tone_on else 15
            period = f["tone"][i] & 0xFFF
            if att < 15:
                div, sh = sn_tone(period)
                shifted += 1 if sh else 0
                if div != state["tone"][i]:
                    state["tone"][i] = div
                    writes.append(0x80 | (i << 5) | (div & 15))
                    writes.append((div >> 4) & 0x3F)
            if att != state["att"][i]:
                state["att"][i] = att
                writes.append(0x90 | (i << 5) | att)
        # --- the noise channel
        if noise_amp:
            mode = noise_mode(f["noise"])
            if mode != state["noise"]:
                state["noise"] = mode
                writes.append(mode)
        att = AY2SN[noise_amp] if noise_amp else 15
        if att != state["att"][3]:
            state["att"][3] = att
            writes.append(0xF0 | att)
        out.append(writes)

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
        print("%-24s %5d frames (%.1f s), %5d bytes, loop at %d, %d notes shifted up"
              % (os.path.basename(path), len(frames), len(frames) / 50.0,
                 len(packed), loop_off, shifted))
    return packed, loop_off, len(frames)


def emit(name, packed, loop_off, out):
    out.write("*\n* %s: %d bytes\n*\n" % (name, len(packed)))
    out.write("%s data %d\n" % (name, loop_off))
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
    for label, tune, fname in tunes:
        packed, loop_off, _ = convert(os.path.join(ROOT, "msx", "Music", tune), True)
        path = os.path.join(ROOT, "src", fname)
        with open(path, "w") as out:
            out.write("* ------------------------------------------------------------------\n")
            out.write("* Music - generated by tools/conv_music.py, do not edit\n")
            out.write("* Source: msx/Music/%s, played by the replayer in tools/pt3.py\n" % tune)
            out.write("* The first word is the offset of the loop point in the stream.\n")
            out.write("* ------------------------------------------------------------------\n")
            emit(label, packed, loop_off, out)
        print("   -> src/%s" % fname)


if __name__ == "__main__":
    main()
