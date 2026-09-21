#!/usr/bin/env python3
"""Build the TI-99/4A cartridge.

Assembles the two 8K banks with xas99, pads them, glues them into a
paged378 cartridge image and, if java is available, packs an .rpk.
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(ROOT, "build")
BANK_SIZE = 0x2000
NAME = "jawbreaker2"


def find_xas99():
    env = os.environ.get("XAS99")
    if env and os.path.isfile(env):
        return [sys.executable, env]
    for cand in (os.path.join(ROOT, "..", "tmp_xdt99", "xas99.py"),
                 os.path.join(ROOT, "tools", "xas99.py")):
        cand = os.path.abspath(cand)
        if os.path.isfile(cand):
            return [sys.executable, cand]
    exe = shutil.which("xas99.py")
    if exe:
        return [sys.executable, exe]
    raise SystemExit("xas99.py not found, set the XAS99 environment variable")


def run(cmd, **kw):
    r = subprocess.run(cmd, cwd=ROOT, **kw)
    if r.returncode:
        raise SystemExit(r.returncode)


def report_usage():
    """How much room is left in each bank, read back from the listing."""
    lst = os.path.join(BUILD, "jawbreaker.lst")
    if not os.path.isfile(lst):
        return
    import re
    addr = re.compile(r"^(?:\d{4})?\s+([0-9A-F]{4}) [0-9A-F]{4}")
    bank = re.compile(r"^\s*\d{4}\s+bank\s+(0|1|2|3|all)\b", re.I)
    names = {"0": "bank 0 (code)", "1": "bank 1 (data)", "2": "bank 2 (intro)",
             "3": "bank 3 (game)", "all": "shared"}
    shared = 0x7E00
    current = None
    top = {}
    for line in open(lst, errors="replace"):
        b = bank.match(line)
        if b:
            current = names[b.group(1).lower()]
            continue
        m = addr.match(line)
        if m and current:
            a = int(m.group(1), 16)
            if 0x6000 <= a < 0x8000:
                top[current] = max(top.get(current, 0), a)
    for name in ("bank 0 (code)", "bank 1 (data)", "bank 2 (intro)",
                 "bank 3 (game)", "shared"):
        if name in top:
            end = top[name] + 2
            limit = 0x8000 if name == "shared" else shared
            print("%-14s ends at >%04x, %5d bytes free" % (name, end, limit - end))


def main():
    os.makedirs(BUILD, exist_ok=True)
    run([sys.executable, os.path.join("tools", "conv_gfx.py")])

    run(find_xas99() + ["-b", "-R", "-q",
                        "-L", os.path.join("build", "jawbreaker.lst"),
                        "-E", os.path.join("build", "sym.a99"),
                        os.path.join("src", "jawbreaker.a99"),
                        "-o", os.path.join("build", "jaw")])

    banks = []
    for n in (0, 1, 2, 3):
        path = os.path.join(BUILD, "jaw_b%d" % n)
        if not os.path.isfile(path):
            path += ".bin"
        data = open(path, "rb").read()
        if len(data) > BANK_SIZE:
            raise SystemExit("bank %d is %d bytes, %d too many"
                             % (n, len(data), len(data) - BANK_SIZE))
        banks.append(data + b"\x00" * (BANK_SIZE - len(data)))
    report_usage()

    rom = os.path.join(BUILD, "%s-8.bin" % NAME)
    with open(rom, "wb") as f:
        for b in banks:
            f.write(b)
    print("wrote %s (%d bytes)" % (os.path.relpath(rom, ROOT), len(banks) * BANK_SIZE))

    shutil.copy(os.path.join(ROOT, "layout.xml"), os.path.join(BUILD, "layout.xml"))
    if shutil.which("jar"):
        rpk = "%s.rpk" % NAME
        subprocess.run(["jar", "-cfM", rpk, "%s-8.bin" % NAME, "layout.xml"],
                       cwd=BUILD, check=True)
        print("wrote %s" % os.path.join("build", rpk))
    else:
        print("java not found, skipping the .rpk")
    return 0


if __name__ == "__main__":
    sys.exit(main())
