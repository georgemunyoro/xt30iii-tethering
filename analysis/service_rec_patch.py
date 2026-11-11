"""Exact experimental RAM payloads; offline construction only, no USB."""
import struct

HELLO, ATOMIC = 0x03273E78, 0x03273F34
JIG_BRANCH, PLAY_BRANCH, REC_WRAPPER = 0x02E19EE0, 0x02DE6614, 0x02E174D4
MODE = 0x02A305F8


def branch(pc, target, link=False):
    offset = target-pc
    if offset % 4 or not -(1 << 27) <= offset < (1 << 27): raise ValueError('Branch out of range/alignment')
    return (0x94000000 if link else 0x14000000) | ((offset//4) & 0x3FFFFFF)


def trampoline():
    # Keep the JIG caller's original 32-byte frame and x19 saved register.
    # Use a local conditional branch; REC is beyond B.cond's +/-1MiB range.
    pages = ((MODE & ~4095)-(HELLO & ~4095))//4096
    if not -(1 << 20) <= pages < (1 << 20): raise ValueError('ADRP out of range')
    imm = pages & 0x1FFFFF
    adrp = 0x90000000 | ((imm & 3) << 29) | ((imm >> 2) << 5)
    words = (adrp, 0xB9400000 | ((MODE & 4095)//4 << 10),
             0x71000C1F, 0x54000041,  # cmp w0,3; b.ne pc+8
             branch(HELLO+16, 0x02E18DAC),
             branch(HELLO+20, 0x03060068, True),
             branch(HELLO+24, JIG_BRANCH+4), 0xD503201F)
    return struct.pack('<8I', *words)


def atomic_store(address, word):
    if address % 4 or not 0 <= word <= 0xFFFFFFFF: raise ValueError('Invalid aligned word store')
    # ldr x1, literal+16; ldr w2, literal+24; str w2,[x1]; ret.
    # x0 retains the CMD parser's aligned stack pointer (as the IC leaf does).
    return struct.pack('<4IQII', 0x58000081, 0x180000A2, 0xB9000022, 0xD65F03C0, address, word, 0)


PATCH_WORDS = {JIG_BRANCH: branch(JIG_BRANCH, HELLO),
               PLAY_BRANCH: branch(PLAY_BRANCH, REC_WRAPPER, True)}
