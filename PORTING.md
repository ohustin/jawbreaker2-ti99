# Porting notes: MSX1 → TI-99/4A

How the MSX sources in `msx/` map onto the TMS9900 sources in `src/`, and the
handful of places where the two machines forced a different solution.

## The easy half: the video chip is the same

The MSX1 and the TI-99/4A both use a TMS9918A. MSX SCREEN 2 is TI Graphics II,
the same 6144 byte pattern table, the same 6144 byte colour table, the same
sprites. That means:

- Every byte of graphics data is reused unchanged. `tools/conv_gfx.py` only
  changes the container: sjasm `DB` lists and `incbin` blobs become xas99
  `DATA` words.
- The port keeps the MSX VRAM layout instead of the usual TI one, so the
  addresses in the MSX code are still valid:

  | | address |
  |---|---|
  | pattern table | `>0000` |
  | name table | `>1800` |
  | sprite attributes | `>1b00` |
  | colour table | `>2000` |
  | sprite patterns | `>3800` |

  VDP registers are set to match (`>02 >c2 >06 >ff >03 >36 >07 >01`).
- Sprite magnification is used the same way: Jawbreaker II runs 16x16 sprites
  magnified to 32x32, Jawbreaker I runs them unmagnified.

What changes is only how the CPU reaches the chip: `out (#98),a` becomes
`movb rX,*r15` with r15 holding `>8c00`, and `vpoke` becomes `vwad` writing the
address to `>8c02`. `src/vdp.a99` holds the whole translation.

## The hard half: no RAM

The MSX version keeps a full 768 byte copy of the name table in RAM
(`leveldata`) and works on that copy: it looks up what the player is standing
on, edits the wall rows and uploads them every frame.

A TI-99/4A cartridge without the 32K expansion has the 256 byte scratch pad and
nothing else. Rather than require the expansion, the port drops the shadow copy:

- **Reading the playfield** — `gstdot` and `celbel` read the two or three
  bytes they need straight back out of the name table with a VDP read.
- **The sliding walls** — `wallrw` generates each wall row directly into VRAM.
  A row is wall everywhere except the six (Jawbreaker II) or four
  (Jawbreaker I) cells of the opening, and the edge characters carry the sub
  cell offset (characters 128-143), which is how the opening slides smoothly.
  The MSX code achieves the same picture by patching its shadow row; the
  visible result is identical.
- **The bonus sweet** — written straight to the four name table cells.
- **The ready screen** — "LEVEL n / n LEFT / READY?" is printed over the
  middle of the playfield. With no shadow to restore from, the four rows
  underneath are copied into unused video memory at `>1c00` first (`savtxt`)
  and copied back afterwards (`restxt`).

The scratch pad ends up as: workspace `>8300`-`>831f`, word variables from
`>8320`, byte variables, the game type parameter block, the six monster
records, the wall openings, the sound state and a small return address stack,
ending at `>83d4`.

## Cartridge layout

32K in four 8K banks, standard `paged378` switching (a write to `>6000 + 2n`
selects bank n):

```
bank 0  >6000-7dff   code
bank 1  >6000-7dff   graphics, strings, start position tables
bank 2  >6000-7dff   the intro tune
bank 3  >6000-7dff   the in-game tune
shared  >7e00-7fff   bank switching helpers, identical in every bank
```

Every bank begins with the cartridge header, and `>7e00` upward is the same in
all four.


The cartridge header (`src/header.a99`) sits at the start of **every** bank,
not only bank 0. The bank latch on a real cartridge board - a 74LS378, or a
'379 or '377 - has no defined state when the power comes on, so the console may
well be looking at bank 2 when it scans `>6000` for the `>aa` that tells it a
cartridge is there. With the header in one bank only, the game simply does not
appear on the selection screen when the latch comes up anywhere else. All four
headers point at `cstart` in the shared segment, which selects bank 0 and
branches to `start`; because the shared segment is identical in every bank,
that switch is safe wherever it runs from.

Because a bank switch changes the memory under the program counter, everything
that touches another bank lives in the shared segment, where the same
instructions exist in every bank: `b1copy` (bank 1 → VRAM), `b1pat` (one
sprite pattern), `b1prnt` (print a string), `b1ram` (copy a table into the
scratch pad) and the music player `musply`.

## Sound

The MSX version uses the AY-3-8910 with the ayFX effect player and PT3 music;
the TI-99/4A has an SN76489: three tone channels, one noise channel, four bit
attenuation, no envelopes.

**Effects.** `msx/fullsfx.afb` is the ayFX bank the MSX version plays.
`tools/conv_sfx.py` reads it with the format `msx/Code/ayFX-ROM.ASM` defines -
a control byte per frame carrying a volume, with a tone or noise period when
the bits say so - and writes `src/sfx.a99`, so the ten effects keep their real
envelopes. Tone periods carry over unchanged, as they do for the music,
volumes become attenuations, and frame counts are stretched by 6/5 for the
60 Hz frame. `src/sound.a99` plays one effect at a time as a channel plus a
list of `frequency, attenuation, frames` steps, and `sfxini` is called in
exactly the places where the MSX calls `ayFX_INIT`.

One thing does not carry over. ayFX plays every sample on one fixed channel,
while these effects pick a tone channel or the noise channel depending on what
the sample uses, so an effect that lands on a different channel than the one
playing has to silence that one first - otherwise the old channel keeps
sounding its last note forever. That is what made the tooth brush sequence
drone: the level-finished effect is a tone, the brush is noise, and nothing
turned the tone off.

**Music.** The PT3 replayer in `msx/Code/PT3-ROM.ASM` is ported to Python in
`tools/pt3.py` - pattern decoding, samples, ornaments, envelopes, portamento,
vibrato and all, with the variables keeping their Z80 names. Instead of
running on the Z80 at 50 Hz it runs at build time and records what the AY
registers would be on every frame; `tools/conv_music.py` maps that to the
SN76489 and writes the difference between one frame and the next.

The mapping gets one thing for free: the MSX clocks its AY at 1.7897725 MHz
and divides by 16, the TI-99/4A clocks its SN76489 at 3.579545 MHz and divides
by 32, so **a tone period means the same pitch on both machines** and
transfers unchanged.

What does not transfer:

- **The envelope.** PT3 uses it for the buzzy bass, where the envelope runs at
  audio rate and the tone register carries the note. The note survives, the
  timbre does not. Envelopes slow enough to be heard as a fade are simulated
  and become a volume.
- **The bottom octave.** The SN76489 divider stops at 1023, about 110 Hz.
  Lower notes are shifted up an octave: 224 of them in the intro tune, 2201
  frames worth in the in-game tune, which is mostly that bass line.
- **Noise.** The AY mixes noise into a tone channel; the SN76489 has a
  separate noise channel with three fixed rates, so drums get that channel and
  the closest rate.

The stream is a byte per frame plus the register writes:

    >00->7e   that many bytes follow, they go straight to the chip
    >7f       end of the stream, carry on from the loop point
    >80->ff   (b and >7f) frames with nothing to do

which comes to 4337 bytes for the intro and 5152 for the in-game tune, a bank
each, and about one byte a frame to play. Two details matter: the stream is
50 Hz and the TI runs at 60, so the player skips every sixth frame, which is
what `PlayMus` does on the MSX; and while an effect is playing the music
leaves that channel alone, the same way ayFX takes priority on the MSX.

## Instruction level notes

Things that bit during the port, kept here because they will bite again:

- **The Z80 carries flags out of a subroutine; the TMS9900 loses them.** The
  return sequence `dect r10 / mov *r10,r11` sets the status from the address it
  just popped, so any routine that answers a yes/no question has to re-create
  the flags after the pop (`rdfire`, `anykey`, `celbel` all end with
  `mov r1,r1`).
- **`movb` only touches the most significant byte.** A byte read back from
  VRAM lands in the MSB of r1 with whatever was in the LSB left alone, so
  testing the whole word gives the wrong answer. Byte variables are always
  loaded with `movb` + `srl rX,8`.
- **Word accesses ignore bit 0 of the address.** A misaligned table silently
  reads the wrong word instead of faulting, so the monster records, the
  parameter block and the stack all have to start on even addresses. There is
  a deliberate pad byte in the scratch pad map to keep them there.
- **Every register used to hold a return address is a resource.** `lodgfx`
  held its return in r8 and called `lodtit`, which also used r8: an infinite
  loop before the first frame. The convention now is that the leaf VDP
  routines use r9, and everything above them pushes r11 on the r10 stack.
- The self-modifying-ish "call through a table of routine addresses" that the
  MSX code uses for the dot handler (`lookuptable`) translates directly:
  `mov @dottab(r1),r1` then `bl *r1`.

## What was checked

The cartridge has been run in **Classic99** with the real console ROMs: the
console recognises the header ("2 FOR JAWBREAKER II"), the attraction screen,
menu, both game types, play, death and game over all behave.

Before that, `tools/sim99.py` drove the following through the built cartridge
image:

- boot, attraction screen with the intro script and credits, menu
- selecting easy/hard and Jawbreaker I/II, starting a game
- movement, eating dots (score and the dots-left counter), the energizer
  (all monsters scared, 250 frame timer), eating a scared monster (multiplier
  and score), being caught (life lost, death sequence, ready screen)
- finishing a level (tooth brush sequence, next level built)
- running out of lives and dropping back to the attraction screen
- the bonus sweet appearing on its timer with the right characters per level
- easy mode running the game at exactly half the speed of hard mode

## Frame timing, and the flicker it caused

`tools/sim99.py` counts cycles with a rough model: TMS9900 base cycles plus
four wait states for every access outside the scratch pad, which is what the
console's eight bit bus costs. At 3 MHz a frame is 16.6 ms and the vertical
blanking interval about 4.3 ms.

The first version sat at **17.0 ms per frame**, and finished writing the sprite
attribute table **6.1 ms** into the frame, i.e. a quarter of the way down the
visible screen. On top of that, every sprite call closed the list with its own
y=208 marker, so while the monsters were being drawn the list was repeatedly
truncated and extended under the raster. Together that made the sprites
flicker. Four changes fixed it:

1. **One end-of-list marker per frame.** `endspr` writes it once, after
   everything has been drawn, instead of `putspr` and `putmst` each writing
   their own.
2. **Draw first, think afterwards.** The frame now writes the sprite table
   straight after the frame flag and runs the game logic below it; monster
   drawing (`drwmst`) is split out of the monster update (`updmst`).
3. **Attributes in one burst.** The entries are consecutive in VRAM, so the
   write address is set once per group, and the player's patterns are uploaded
   after the list is closed (`putpat`), only when the animation frame has
   actually changed.
4. **Cheaper wall rows.** The MSX version uploads all 32 cells of every wall
   row every frame. Only the eight cells of the opening ever change, and they
   are now written straight out with no per cell test: 5.2 ms down to 2.2 ms.
   The score is likewise only redrawn when it changes.

That leaves **11.8 ms per frame**, with the sprite table finished after
**3.9 ms**, inside the blanking interval on every frame.

The one thing still worth watching on real hardware: the level restart uploads
1792 bytes of sprite patterns, which takes about 30 ms. It runs with the
display disabled, so it shows as a short pause rather than a glitch.
