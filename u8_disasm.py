"""Pure-Python disassembler for Ultima 8 usecode (EUSECODE.FLX).

Replaces the C++ `pentagram/tools/disasm/disasm` binary that
`parse_schedules.py` used to shell out to. We walk EUSECODE.FLX directly,
decode every class's bytecode, and yield a structured representation that
downstream scripts pattern-match against.

Each instruction is reported as an `Instr(offset, op, mnemonic, args)`.
`op` is the raw opcode byte (callers usually match on this); `args` is a
dict whose keys depend on the opcode (see the OPCODES table). The
mnemonic strings are short tags — not formatted the same way Pentagram
printed them — so don't pattern-match on free text.

References:
  - FLX layout / class iteration: pentagram/tools/disasm/Disasm.cpp:678+
  - U8 class header (0x0C bytes) + 32-event table:
    pentagram/convert/u8/ConvertUsecodeU8.h (readheader/readevents).
  - Opcode encoding: pentagram/convert/Convert.h (readOpGeneric).

We support U8 only — Crusader-specific opcode quirks (0x79 globaladdr)
aren't decoded.
"""

import struct
from typing import NamedTuple, Optional


class Instr(NamedTuple):
    offset: int          # bytecode offset within the class (matches disasm)
    op:     int          # raw opcode byte
    mnemonic: str        # short tag, e.g. 'push_byte', 'spawn', 'calli'
    args:   dict         # decoded operands, keys depend on opcode


class Function(NamedTuple):
    offset: int                # bytecode offset of the first instruction
    event:  Optional[int]      # 0..31 event id, or None for non-event helper
    instrs: list               # list[Instr]


class UClass(NamedTuple):
    class_id: int    # 0-based class index (matches Pentagram's "Usecode class N")
    name:     str
    functions: list  # list[Function]


# ──────────────────────────────────────────────
# Byte reader
# ──────────────────────────────────────────────
class _R:
    """Tiny cursor over a bytes object. `pos` is the bytecode offset; the
    decoders below consume bytes through this and the resulting `pos`
    becomes the next instruction's offset (matches Pentagram's curOffset)."""
    __slots__ = ("data", "pos")
    def __init__(self, data, pos=0):
        self.data = data; self.pos = pos
    def u8(self):
        v = self.data[self.pos]; self.pos += 1; return v
    def u16(self):
        v = self.data[self.pos] | (self.data[self.pos + 1] << 8)
        self.pos += 2; return v
    def u32(self):
        v = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4; return v


# ──────────────────────────────────────────────
# Per-opcode operand decoders
# ──────────────────────────────────────────────
# Each decoder consumes the operand bytes for one opcode and returns a
# dict of named fields. Keys are intentionally short: 'b' for an unsigned
# byte, 'w' for an unsigned word, 'bp' for the raw BP-offset byte (signed
# when >= 0x80 — caller does the sign-extend), etc.

def _none(r):       return {}
def _b(r):          return {"b":   r.u8()}
def _w(r):          return {"w":   r.u16()}
def _d(r):          return {"d":   r.u32()}
def _bp(r):         return {"bp":  r.u8()}
def _b_b(r):        return {"b0":  r.u8(), "b1":  r.u8()}
def _b_w(r):        return {"b":   r.u8(), "w":   r.u16()}
def _w_b(r):        return {"w":   r.u16(), "b":  r.u8()}
def _huge(r):       return {"bp":  r.u8(),  "size": r.u8()}
def _calli(r):      return {"argbytes": r.u8(), "intrinsic": r.u16()}
def _call(r):       return {"classid":  r.u16(), "offset":   r.u16()}
def _spawn(r):      return {"argbytes": r.u8(),  "thissize": r.u8(),
                            "classid":  r.u16(), "offset":   r.u16()}
def _spawn_inline(r):
    return {"classid": r.u16(), "offset": r.u16(),
            "iofs":    r.u16(), "thissize": r.u8(), "unk": r.u8()}
def _jmp(r):        return {"rel": r.u16()}      # signed when used
def _global(r):     return {"addr": r.u16(), "size": r.u8()}
def _loop(r):       return {"bp": r.u8(), "scriptsize": r.u8(), "searchtype": r.u8()}
def _foreach(r):    return {"bp": r.u8(), "datasize":   r.u8(), "jumpto":     r.u16()}
def _elem(r):       return {"bp": r.u8(), "size":       r.u8(), "slist":      r.u8()}
def _string(r):
    n = r.u16()
    out = bytearray()
    while True:
        c = r.u8()
        if c == 0: break
        out.append(c)
    return {"len": n, "str": out.decode("latin-1", errors="replace")}
def _dbgsym(r):
    rel = r.u16()
    name = bytearray()
    for _ in range(8): name.append(r.u8())
    r.u8()   # trailing 0
    return {"rel": rel, "name": name.decode("latin-1", errors="replace")}


OPCODES = {
    0x00: ("pop_byte_bp",       _bp),
    0x01: ("pop_bp",            _bp),
    0x02: ("pop_dword_bp",      _bp),
    0x03: ("pop_huge_bp",       _b_b),
    0x08: ("pop_res",           _none),
    0x09: ("pop_element",       _elem),
    0x0A: ("push_byte",         _b),
    0x0B: ("push_word",         _w),
    0x0C: ("push_dword",        _d),
    0x0D: ("push_string",       _string),
    0x0E: ("create_list",       _b_b),
    0x0F: ("calli",             _calli),
    0x11: ("call",              _call),
    0x12: ("pop_temp",          _none),
    0x14: ("add",               _none),
    0x15: ("add_dword",         _none),
    0x16: ("concat",            _none),
    0x17: ("append",            _none),
    0x19: ("append_slist",      _b),
    0x1A: ("remove_slist",      _b),
    0x1B: ("remove_list",       _b),
    0x1C: ("sub",               _none),
    0x1D: ("sub_dword",         _none),
    0x1E: ("mul",               _none),
    0x1F: ("mul_dword",         _none),
    0x20: ("div",               _none),
    0x21: ("div_dword",         _none),
    0x22: ("mod",               _none),
    0x23: ("mod_dword",         _none),
    0x24: ("cmp",               _none),
    0x25: ("cmp_dword",         _none),
    0x26: ("strcmp",            _none),
    0x28: ("lt",                _none),
    0x29: ("lt_dword",          _none),
    0x2A: ("le",                _none),
    0x2B: ("le_dword",          _none),
    0x2C: ("gt",                _none),
    0x2D: ("gt_dword",          _none),
    0x2E: ("ge",                _none),
    0x2F: ("ge_dword",          _none),
    0x30: ("not_",              _none),
    0x31: ("not_dword",         _none),
    0x32: ("and_",              _none),
    0x33: ("and_dword",         _none),
    0x34: ("or_",               _none),
    0x35: ("or_dword",          _none),
    0x36: ("ne",                _none),
    0x37: ("ne_dword",          _none),
    0x38: ("in_list",           _b_b),
    0x39: ("bit_and",           _none),
    0x3A: ("bit_or",            _none),
    0x3B: ("bit_not",           _none),
    0x3C: ("lsh",               _none),
    0x3D: ("rsh",               _none),
    0x3E: ("push_byte_bp",      _bp),
    0x3F: ("push_bp",           _bp),
    0x40: ("push_dword_bp",     _bp),
    0x41: ("push_string_bp",    _bp),
    0x42: ("push_list_bp",      _b_b),
    0x43: ("push_slist_bp",     _bp),
    0x44: ("push_element",      _b_b),
    0x45: ("push_huge",         _huge),
    0x4B: ("push_addr_bp",      _bp),
    0x4C: ("push_indirect",     _b),
    0x4D: ("pop_indirect",      _b),
    0x4E: ("push_global",       _global),
    0x4F: ("pop_global",        _global),
    0x50: ("ret",               _none),
    0x51: ("jne",               _jmp),
    0x52: ("jmp",               _jmp),
    0x53: ("suspend",           _none),
    0x54: ("implies",           _b_b),
    0x57: ("spawn",             _spawn),
    0x58: ("spawn_inline",      _spawn_inline),
    0x59: ("push_pid",          _none),
    0x5A: ("init",              _b),
    0x5B: ("line_number",       _w),
    0x5C: ("symbol_info",       _dbgsym),
    0x5D: ("push_byte_retval",  _none),
    0x5E: ("push_retval",       _none),
    0x5F: ("push_dword_retval", _none),
    0x60: ("word_to_dword",     _none),
    0x61: ("dword_to_word",     _none),
    0x62: ("free_string_bp",    _b),
    0x63: ("free_slist_bp",     _b),
    0x64: ("free_list_bp",      _b),
    0x65: ("free_string_sp",    _b),
    0x66: ("free_list_sp",      _b),
    0x67: ("free_slist_sp",     _b),
    0x69: ("push_strptr_bp",    _b),
    0x6B: ("str_to_ptr",        _none),
    0x6C: ("param_pid_chg",     _b_b),
    0x6D: ("push_dword_procres", _none),
    0x6E: ("add_sp",            _b),
    0x6F: ("push_addr_sp",      _b),
    0x70: ("loop",              _loop),
    0x73: ("loopnext",          _none),
    0x74: ("loopscr",           _b),
    0x75: ("foreach_list",      _foreach),
    0x76: ("foreach_slist",     _foreach),
    0x77: ("set_info",          _none),
    0x78: ("process_exclude",   _none),
    0x79: ("end",               _none),   # U8: end-of-function (Crusader: globaladdr — unsupported)
    0x7A: ("end",               _none),
}


# Helpers callers can use without re-implementing the sign math.
def bp_offset(raw):
    """Return the signed BP offset from a raw `bp` operand byte.
    Disasm prints `[BP-05h]` for raw 0xFB and `[BP+06h]` for raw 0x06."""
    return raw - 0x100 if raw >= 0x80 else raw


def jmp_target(instr, next_offset):
    """Resolve a jne/jmp absolute target. `next_offset` is the offset of
    the instruction immediately after this one (Pentagram's nextoffset)."""
    rel = instr.args["rel"]
    if rel >= 0x8000: rel -= 0x10000
    return next_offset + rel


# ──────────────────────────────────────────────
# FLX file + class iteration
# ──────────────────────────────────────────────
def _flx_entries(data):
    """Return [(offset, length), ...] for every FLX entry. Entries 0/1 are
    the globals table and class-name table; classes start at index 2."""
    count = struct.unpack_from("<I", data, 0x54)[0]
    out = []
    for i in range(count):
        off = struct.unpack_from("<I", data, 0x80 + i * 8)[0]
        ln  = struct.unpack_from("<I", data, 0x80 + i * 8 + 4)[0]
        out.append((off, ln))
    return out


def _class_name(name_table, class_idx):
    """Read a class name from the names FLX entry. Layout per
    Disasm.cpp:686: 4-byte header, then 13 bytes per class (9-char name
    + 4 trailing flag bytes). Names are NUL-padded."""
    base = 4 + 13 * class_idx
    raw = name_table[base:base + 9]
    end = raw.find(b"\x00")
    if end >= 0: raw = raw[:end]
    return raw.decode("latin-1", errors="replace").strip()


def _decode_function(body, start, max_offset):
    """Decode a function starting at bytecode offset `start`. Stops at
    the next end-of-function opcode (0x79 or 0x7A) or `max_offset`.
    Returns (Function, position-of-next-function)."""
    r = _R(body, start)
    instrs = []
    while r.pos < max_offset:
        op_off = r.pos
        op = r.u8()
        spec = OPCODES.get(op)
        if spec is None:
            # Unknown opcode — append a sentinel and stop walking this
            # function so we don't desync from the bytecode stream.
            instrs.append(Instr(op_off, op, f"db_{op:02x}", {}))
            break
        mnem, dec = spec
        args = dec(r)
        instrs.append(Instr(op_off, op, mnem, args))
        if op in (0x79, 0x7A):
            break
    return Function(start, None, instrs), r.pos


def parse_eusecode(path):
    """Iterate every class in EUSECODE.FLX, yielding UClass records.

    The bytecode region of each class starts at curOffset 0x80 (right
    after the 0x0C-byte header and 32×4-byte event table). Functions
    chain back-to-back from there to maxOffset; each ends at an `end`
    opcode. Event-handler functions are tagged with their event id by
    matching their start offset against the event table.
    """
    with open(path, "rb") as f:
        data = f.read()
    entries = _flx_entries(data)
    if len(entries) < 3:
        return
    n_off, n_len = entries[1]
    name_table = data[n_off:n_off + n_len]

    for class_idx in range(len(entries) - 2):
        off, ln = entries[class_idx + 2]
        if off == 0 or ln == 0:
            continue
        if ln < 12 + 128:    # need header + event table at minimum
            continue
        body = data[off + 12:off + ln]
        # `body` matches Pentagram's curOffset coordinate system: events
        # live at offsets 0x00..0x7F (32 × 4 bytes), opcodes at 0x80+.
        max_offset = len(body)
        event_for = {}
        for ev in range(32):
            ev_off = struct.unpack_from("<I", body, ev * 4)[0]
            if 0x80 <= ev_off < max_offset:
                event_for.setdefault(ev_off, ev)

        functions = []
        cur = 0x80
        while cur < max_offset:
            fn, cur = _decode_function(body, cur, max_offset)
            if fn.event is None and fn.offset in event_for:
                fn = Function(fn.offset, event_for[fn.offset], fn.instrs)
            functions.append(fn)
            if not fn.instrs:    # decoder bailed — don't loop forever
                break

        yield UClass(class_idx, _class_name(name_table, class_idx), functions)


if __name__ == "__main__":
    # Smoke test: print class count + the first few instructions of class 0.
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else "ULTIMA8/USECODE/EUSECODE.FLX"
    classes = list(parse_eusecode(p))
    print(f"{len(classes)} classes parsed")
    for c in classes[:3]:
        print(f"class {c.class_id} ({c.name}): {len(c.functions)} funcs, "
              f"{sum(len(f.instrs) for f in c.functions)} instrs")
