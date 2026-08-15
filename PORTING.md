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

16K in two 8K banks, standard `paged378` switching (a write to `>6000` selects
bank 0, a write to `>6002` selects bank 1):

```
bank 0  >6000-7dff   code
bank 1  >6000-7dff   graphics, strings, start position tables
shared  >7e00-7fff   bank switching helpers, identical in both banks
```

Because a bank switch changes the memory under the program counter, everything
that touches bank 1 lives in the shared segment, where the same instructions
exist in both banks: `b1copy` (bank 1 → VRAM), `b1pat` (one sprite pattern),
`b1prnt` (print a string) and `b1ram` (copy a table into the scratch pad).

## Sound

This is the one part that could not be carried over. The MSX version uses the
AY-3-8910 with the ayFX effect player and PT3 music; the TI-99/4A has an
SN76489 with three tone channels, one noise channel and no envelopes.

`src/sound.a99` is a small effect player written for the SN76489. An effect is
a channel plus a list of `frequency, attenuation, frames` steps, one effect
plays at a time, and `sfxini` is called in exactly the places where the MSX
code calls `ayFX_INIT`. The PT3 music is not ported.

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

Not checked, because the simulator has no timing model: whether a frame always
fits inside one vertical blank on real hardware. The heaviest frame does the
same work the MSX version did in its interrupt handler, plus the four or seven
wall rows written cell by cell, so it is worth watching on hardware.
