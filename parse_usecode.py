#!/usr/bin/env python3
"""
Parse Ultima 8 usecode (EUSECODE.FLX) and emit every JSON the viewer needs.

One symbolic interpreter over a typed stack drives four extractions in a
single pass:

  json/barks.json     - the literal string argument flowing into every
                        Item::bark / guardianBark (intrinsics 0x49 / 0x6D).
                        See the "Barks" section of main().

  json/readables.json - book / scroll / tombstone / plaque text recovered
                        from the read intrinsics (0x6E–0x71). Tombstones
                        and plaques gate their text inline; books and
                        scrolls dispatch into a shared library class via a
                        process spawn. See the "Readables" section.

  json/dialog.json    - NPC and titan conversation lines. For each non-monster
                        NPC, usecode class = objid + 1024. For titans (and
                        crowd actors with no npcnum), the shape itself is the
                        class. See the "Dialogue" section.

  json/locks.json     - lock-id constants compared against the held item's
                        K_QUALITY in the key/lock shape classes. Useful for
                        the inspector's key↔chest annotation. See the
                        "Locks" section.

Reference: ScummVM engines/ultima/ultima8/usecode/uc_machine.cpp for opcode
semantics; engines/ultima/ultima8/usecode/u8_intrinsics.h for intrinsic IDs.

The previous version of the bark walker tracked only the most recent
push-string opcode (0x0D) and attributed that text to the next bark call.
That heuristic misattributes the bark argument whenever:

  - Strings are built by concatenation (0x16): the literal we see is only a
    fragment, and the actual argument comes from a local variable.
  - Several literals are pushed for unrelated purposes (list creation, 0x0E)
    before a bark is issued.
  - The bark argument comes from a BP-relative local (0x69) without a fresh
    literal push at all.

This version interprets the bytecode opcode-by-opcode, maintaining a typed
symbolic stack. At each bark call site we inspect the stack at the exact
byte offset where ARG_STRING is read and emit the text only when it is a
verified literal.
"""

import argparse
import json
import os
import struct
import sys

FLEX_TABLE_OFFSET = 0x80
FLEX_HDR_PAD = 0x1A

# U8 class layout (see usecode.cpp::get_class_event):
#   bytes 0..11   : 12-byte class header
#   bytes 12..139 : 32 event offsets, 4 bytes each
#   bytes 140+    : bytecode
CLASS_HEADER = 12
EVENT_TABLE = 32 * 4
CODE_OFFSET = CLASS_HEADER + EVENT_TABLE  # = 140 = 0x8C

# Item::I_bark = U8Intrinsics[0x49], 8 arg bytes (4 = item ptr, 4 = string ptr).
# Item::I_guardianBark = U8Intrinsics[0x6D], 6 arg bytes (4 = item ptr, 2 = bark id).
INTRINSIC_BARK = 0x49
# Item::I_ask = U8Intrinsics[0x4A] — presents a UCList of answer strings as
# clickable buttons (AskGump). 6 arg bytes: 4 = item ptr (unused), 2 = list id.
INTRINSIC_ASK = 0x4A
INTRINSIC_GUARDIAN_BARK = 0x6D
# Readable-text intrinsics. Each takes the item pointer then a string; grave
# and plaque additionally take a uint16 gump-shape number between the two.
#   BookGump::I_readBook       (item, str)            -> u8intrinsics[0x6E]
#   ScrollGump::I_readScroll   (item, str)            -> u8intrinsics[0x6F]
#   ReadableGump::I_readGrave  (item, u16 shape, str) -> u8intrinsics[0x70]
#   ReadableGump::I_readPlaque (item, u16 shape, str) -> u8intrinsics[0x71]
INTRINSIC_READ_BOOK = 0x6E
INTRINSIC_READ_SCROLL = 0x6F
INTRINSIC_READ_GRAVE = 0x70
INTRINSIC_READ_PLAQUE = 0x71
# Pentagram hardcodes these gump shapes for books and scrolls (BookGump.cpp /
# ScrollGump.cpp); grave/plaque gumps come from the intrinsic argument.
READABLE_BOOK_GUMP = 6
READABLE_SCROLL_GUMP = 19
# Intrinsics whose return value we track symbolically (for frame/quality gating).
# UCMachine::I_getName = U8Intrinsics[0xBC] — returns the main actor's name
# as a string. NPC dialogue splices it into greetings.
INTRINSIC_GETNAME = 0xBC
INTRINSIC_GETSHAPE = 0x0D
INTRINSIC_GETFRAME = 0x0F
INTRINSIC_GETQUALITY = 0x11
INTRINSIC_GETQ = 0x19
# Npc::isDead — NPC look() handlers gate the "dead <role>" bark on this.
INTRINSIC_ISDEAD = 0x8C

# Slot kinds on the symbolic stack.
K_UNKNOWN = "unknown"
K_INT = "int"           # known integer literal
K_STR_ID = "str_id"     # 16-bit string id, value=text if literal
K_STR_PTR = "str_ptr"   # 32-bit string pointer, value=text if literal
K_FRAME = "frame"       # return value of I_getFrame
K_QUALITY = "quality"   # return value of I_getQuality / I_getQ
K_CMP = "cmp"           # boolean compare result; value=(field, intervals)
                        #   field is K_FRAME|K_QUALITY|None
                        #   intervals is a normalised tuple of (lo,hi) inclusive
                        #   ranges where the compare is TRUE, or None when the
                        #   true-set isn't a clean enumerable range (e.g. !=).
K_SLIST = "slist"       # 16-bit string-list id; value = list of element texts
                        # (None per element that isn't a known literal)
K_PROC_RESULT = "result"  # used after 5D/5E/5F when result type isn't tracked
K_DEAD = "dead"           # K_CMP field for an isDead() gate; intervals is a
                          # bool — True when the true-branch means "is dead"


# ---------------------------------------------------------------------------
# Interval arithmetic for frame/quality range gates.
#
# look() handlers gate barks with getFrame()/getQuality() compared by ==, <=,
# >=, < , > and combined with && / ||. Each compare is reduced to the set of
# values for which it is true, expressed as inclusive (lo,hi) intervals.
# ---------------------------------------------------------------------------

FRAME_CAP = 255  # values past this are treated as "unbounded" / not enumerable


def _iv_norm(ivs):
    """Clamp, drop empty, sort and merge touching/overlapping intervals."""
    cleaned = []
    for lo, hi in ivs:
        lo = max(0, lo)
        hi = min(FRAME_CAP, hi)
        if lo <= hi:
            cleaned.append((lo, hi))
    cleaned.sort()
    out = []
    for lo, hi in cleaned:
        if out and lo <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return tuple(out)


def _iv_rel(op_kind, const):
    """Intervals where `value op_kind const` holds (value >= 0)."""
    if op_kind == "eq":
        return _iv_norm([(const, const)])
    if op_kind == "lt":
        return _iv_norm([(0, const - 1)])
    if op_kind == "le":
        return _iv_norm([(0, const)])
    if op_kind == "gt":
        return _iv_norm([(const + 1, FRAME_CAP)])
    if op_kind == "ge":
        return _iv_norm([(const, FRAME_CAP)])
    return None


def _iv_and(a, b):
    if a is None or b is None:
        return None
    return _iv_norm([(max(l1, l2), min(h1, h2))
                     for l1, h1 in a for l2, h2 in b])


def _iv_or(a, b):
    if a is None or b is None:
        return None
    return _iv_norm(list(a) + list(b))


def _iv_not(a):
    if a is None:
        return None
    out = []
    cur = 0
    for lo, hi in a:
        if lo > cur:
            out.append((cur, lo - 1))
        cur = max(cur, hi + 1)
    if cur <= FRAME_CAP:
        out.append((cur, FRAME_CAP))
    return _iv_norm(out)


def _iv_frames(a, limit=64):
    """Enumerate an interval set to a sorted list, or None if it is empty,
    unbounded (touches FRAME_CAP) or larger than `limit` — i.e. not a clean
    per-frame gate."""
    if not a:
        return None
    frames = []
    for lo, hi in a:
        if hi >= FRAME_CAP:
            return None
        frames.extend(range(lo, hi + 1))
        if len(frames) > limit:
            return None
    return sorted(set(frames))


# ---------------------------------------------------------------------------
# Symbolic stack
# ---------------------------------------------------------------------------

class Stack:
    """Byte-accounted symbolic stack.

    Each entry is (kind, size_bytes, value). When opcodes pop a byte count
    that isn't aligned to existing entries, entries get split. The top of the
    stack is `slots[-1]`.
    """

    __slots__ = ("slots", "_lost")

    def __init__(self):
        self.slots = []
        self._lost = False  # set when we encounter an unmodelled op / underflow

    def push(self, kind, size, value=None):
        self.slots.append((kind, size, value))

    def pop_bytes(self, n):
        """Pop exactly n bytes off the top. Returns list of removed slots (top-first)."""
        if n <= 0:
            return []
        removed = []
        while n > 0:
            if not self.slots:
                self._lost = True
                return removed
            kind, size, val = self.slots[-1]
            if size <= n:
                removed.append(self.slots.pop())
                n -= size
            else:
                # split: keep (size - n) bytes at this slot, peel n bytes off the top
                self.slots[-1] = (kind, size - n, val)
                removed.append((kind, n, val))
                n = 0
        return removed

    def peek_slot_at(self, byte_offset_from_top, byte_count):
        """Return the slot covering [offset, offset+count) measured from the top
        of stack (offset 0 = topmost byte). Returns None if the range crosses a
        slot boundary or goes beyond the stack.
        """
        cum = 0
        for slot in reversed(self.slots):
            kind, size, val = slot
            slot_start = cum
            slot_end = cum + size
            if byte_offset_from_top >= slot_start and byte_offset_from_top < slot_end:
                if byte_offset_from_top + byte_count > slot_end:
                    return None
                return slot
            cum = slot_end
        return None

    def clear(self):
        self.slots.clear()
        self._lost = False

    def total_bytes(self):
        return sum(sz for _, sz, _ in self.slots)


# ---------------------------------------------------------------------------
# Opcode table.
#
# Each entry: (operand_bytes, handler). For most opcodes the handler is the
# simple form (pop_bytes, push_kind, push_size, push_value_from_operand) but
# many opcodes need custom logic, so we just use functions throughout.
# ---------------------------------------------------------------------------

def _store_local(state, ops, nbytes):
    """Pop nbytes into local BP+ops[0], propagating a frame/quality tag so a
    later push of that local can still be recognised as a frame compare."""
    slot = state.stack.peek_slot_at(0, 2)
    state.stack.pop_bytes(nbytes)
    off = ops[0]
    if slot is not None and slot[0] in (K_FRAME, K_QUALITY):
        state.locals[off] = slot[0]
    else:
        state.locals.pop(off, None)
    # Track plain integer constants too, so a later push of the local can
    # resolve to its value (e.g. the gump number readGrave is handed via a
    # local set once at the top of the reader function).
    if slot is not None and slot[0] == K_INT and slot[2] is not None:
        state.int_locals[off] = slot[2]
    else:
        state.int_locals.pop(off, None)
    # Track a recovered string-list stashed in a local (answer lists are
    # often built, stored, appended to, then loaded back for I_ask).
    if slot is not None and slot[0] == K_SLIST:
        state.slist_locals[off] = slot[2]
    else:
        state.slist_locals.pop(off, None)
    # Track a bark-local's accumulated literal text. A store of a known
    # string fragment also records an "accum" bark under the gates active at
    # this point, so each frame branch contributes its own description.
    if off in state.bark_locals:
        text = slot[2] if (slot is not None and slot[0] == K_STR_ID) else None
        state.str_acc[off] = text if text is not None else ""
        if text:
            state.record_bark(INTRINSIC_BARK, text, "accum")


def _h_pop_imm_byte(state, ops):
    # 0x00 pop byte: pops 2 bytes off stack, stores low 8 in local
    _store_local(state, ops, 2)


def _h_pop_imm16(state, ops):
    _store_local(state, ops, 2)


def _h_pop_imm32(state, ops):
    _store_local(state, ops, 4)


def _h_pop_huge(state, ops):
    # 03 xx yy: pop yy bytes into BP+xx
    yy = ops[1]
    state.stack.pop_bytes(yy)


def _h_pop_result_long(state, ops):
    # 08: pop 4 bytes into result
    slot = state.stack.peek_slot_at(0, 4) if state.stack.total_bytes() >= 4 else None
    state.stack.pop_bytes(4)
    if slot is not None:
        state.result = slot
    else:
        state.result = (K_UNKNOWN, 4, None)


def _h_assign_element(state, ops):
    # 09 xx yy zz: index (pop 2) + value (pop yy)
    yy = ops[1]
    state.stack.pop_bytes(2)
    state.stack.pop_bytes(yy)


def _h_push_sbyte(state, ops):
    # 0A xx: push sign-extended 8 -> 16
    v = ops[0]
    if v >= 0x80:
        v -= 0x100
    state.stack.push(K_INT, 2, v & 0xFFFF)


def _h_push_u16(state, ops):
    v = ops[0] | (ops[1] << 8)
    state.stack.push(K_INT, 2, v)


def _h_push_u32(state, ops):
    v = ops[0] | (ops[1] << 8) | (ops[2] << 16) | (ops[3] << 24)
    state.stack.push(K_INT, 4, v)


def _h_create_list(state, ops):
    # 0E xx yy: pop yy values of size xx, push 16-bit list id. For a string
    # list (element size 2) keep the element texts so I_ask answer options
    # can be recovered. Popped slots come off top-first, so reverse them back
    # into code order.
    elem_size = ops[0]
    count = ops[1]
    if elem_size == 2:
        texts = []
        for _ in range(count):
            slot = state.stack.peek_slot_at(0, 2)
            state.stack.pop_bytes(2)
            texts.append(slot[2] if (slot is not None
                                     and slot[0] == K_STR_ID) else None)
        texts.reverse()
        state.stack.push(K_SLIST, 2, texts)
    else:
        state.stack.pop_bytes(elem_size * count)
        state.stack.push(K_UNKNOWN, 2)


def _h_calli(state, ops):
    """0F nn ffff: intrinsic call. nn args on stack (NOT popped by op, but we
    treat them as popped for symbolic purposes since the call consumes them
    semantically). Result goes to process result register.
    """
    arg_bytes = ops[0]
    intrinsic = ops[1] | (ops[2] << 8)

    # Record bark sites BEFORE popping args so we can inspect arguments.
    if intrinsic == INTRINSIC_BARK and arg_bytes == 8:
        # args layout: top 4 bytes = item ptr, next 4 bytes = string ptr
        # (ARG_ITEM_FROM_PTR then ARG_STRING in I_bark)
        str_slot = state.stack.peek_slot_at(4, 4)
        text, kind = _classify_string_arg(str_slot)
        state.record_bark(intrinsic, text, kind)
    elif intrinsic == INTRINSIC_GUARDIAN_BARK and arg_bytes == 6:
        # args: top 4 = item ptr, next 2 = bark id (uint16)
        id_slot = state.stack.peek_slot_at(4, 2)
        if id_slot is not None and id_slot[0] == K_INT and id_slot[2] is not None:
            text = f"<guardianBark id={id_slot[2]}>"
            state.record_bark(intrinsic, text, "guardian")
        else:
            state.record_bark(intrinsic, "<non-literal guardianBark id>", "guardian_unknown")
    elif intrinsic == INTRINSIC_READ_BOOK and arg_bytes == 8:
        # args: top 4 = item ptr, next 4 = string ptr
        text, kind = _classify_string_arg(state.stack.peek_slot_at(4, 4))
        state.record_readable("book", text, kind, READABLE_BOOK_GUMP)
    elif intrinsic == INTRINSIC_READ_SCROLL and arg_bytes == 8:
        text, kind = _classify_string_arg(state.stack.peek_slot_at(4, 4))
        state.record_readable("scroll", text, kind, READABLE_SCROLL_GUMP)
    elif intrinsic == INTRINSIC_ASK and arg_bytes == 6:
        # args: top 4 = item ptr (unused), next 2 = answer list id
        state.saw_ask = True
        list_slot = state.stack.peek_slot_at(4, 2)
        if list_slot is not None and list_slot[0] == K_SLIST and list_slot[2]:
            opts = [t for t in list_slot[2] if t]
            if opts:
                state.record_ask(opts)
    elif intrinsic in (INTRINSIC_READ_GRAVE, INTRINSIC_READ_PLAQUE) and arg_bytes == 10:
        # args: top 4 = item ptr, next 2 = gump shape, next 4 = string ptr
        gump_slot = state.stack.peek_slot_at(4, 2)
        gump = (gump_slot[2] if gump_slot is not None
                and gump_slot[0] == K_INT else None)
        text, kind = _classify_string_arg(state.stack.peek_slot_at(6, 4))
        rtype = "tombstone" if intrinsic == INTRINSIC_READ_GRAVE else "plaque"
        state.record_readable(rtype, text, kind, gump)

    # Pop the arg bytes.
    state.stack.pop_bytes(arg_bytes)

    # Record what the result register now holds.
    if intrinsic == INTRINSIC_GETFRAME:
        state.result = (K_FRAME, 4, None)
    elif intrinsic in (INTRINSIC_GETQUALITY, INTRINSIC_GETQ):
        state.result = (K_QUALITY, 4, None)
    elif intrinsic == INTRINSIC_ISDEAD:
        # Treat the bool result as a compare so 5D/5E + JNE filters it.
        state.result = (K_CMP, 4, (K_DEAD, True))
    elif intrinsic == INTRINSIC_GETNAME:
        # The result register now holds the player's name string.
        state.saw_getname = True
        state.result = (K_STR_ID, 2, PLAYER_NAME)
    else:
        state.result = (K_UNKNOWN, 4, None)


def _classify_string_arg(slot):
    """Return (text, kind) describing what bark would receive.

    kind is one of: "literal" (text is the known string), "local"
    (string came from a local var; text unknown), "non_string" (the slot at
    that offset isn't a string at all — likely a stack-tracking failure),
    "lost" (slot is None).
    """
    if slot is None:
        return ("<stack lost>", "lost")
    kind, size, val = slot
    if kind == K_STR_PTR and val is not None:
        return (val, "literal")
    if kind == K_STR_PTR:
        return ("<non-literal string>", "local")
    if kind == K_UNKNOWN and size >= 4:
        return ("<unknown ptr>", "non_string")
    return (f"<unexpected kind {kind}>", "non_string")


def _h_intra_call(state, ops):
    # 11 cc cc oo oo: call function `oo` in class `cc`. The callee's stack
    # effect is opaque, so the stack is cleared. But if the callee is a frame
    # classifier and an integer category is on the stack, the call's boolean
    # result is the gate `getFrame() in <range for that category>` — record
    # it so a following 5D/5E + JNE turns it into a frame filter.
    callee_cls = ops[0] | (ops[1] << 8)
    callee_off = ops[2] | (ops[3] << 8)
    state.result = (K_UNKNOWN, 4, None)
    if state.call_resolver is not None:
        cmap = state.call_resolver(callee_cls, callee_off)
        if cmap:
            for depth in (0, 2, 4, 6):
                slot = state.stack.peek_slot_at(depth, 2)
                if slot and slot[0] == K_INT and slot[2] in cmap:
                    intervals = _iv_norm([(f, f) for f in cmap[slot[2]]])
                    state.result = (K_CMP, 4, (K_FRAME, intervals))
                    break
    state.stack._lost = True
    state.stack.clear()


def _h_pop16_to_temp(state, ops):
    state.stack.pop_bytes(2)
    state.result = (K_UNKNOWN, 2, None)


def _h_pop32_to_temp(state, ops):
    state.stack.pop_bytes(4)
    state.result = (K_UNKNOWN, 4, None)


def _h_arith16(state, ops):
    state.stack.pop_bytes(2)
    state.stack.pop_bytes(2)
    state.stack.push(K_INT, 2)


def _h_arith32(state, ops):
    state.stack.pop_bytes(4)
    state.stack.pop_bytes(4)
    state.stack.push(K_INT, 4)


# The U8 main actor's name (UCMachine::I_getName). NPC dialogue splices it
# into greetings ("Hello, <name>."); recovered as a literal so those lines
# read whole. U8 never renames the avatar, so this is a constant.
PLAYER_NAME = "Avatar"


def _h_concat(state, ops):
    # 16: pop two strings (2+2), push string id (2). When both operands are
    # known literals the result text is their concatenation — this also lets
    # a bark-local (pushed by 0x41 with its accumulated text) grow fragment
    # by fragment.
    deep = state.stack.peek_slot_at(2, 2)
    top = state.stack.peek_slot_at(0, 2)
    state.stack.pop_bytes(2)
    state.stack.pop_bytes(2)
    dt = deep[2] if (deep is not None and deep[0] == K_STR_ID) else None
    tt = top[2] if (top is not None and top[0] == K_STR_ID) else None
    text = (dt + tt) if (dt is not None and tt is not None) else None
    state.stack.push(K_STR_ID, 2, text)


def _h_list_append(state, ops):
    # 17: append an element to a list; pops element + list (2+2) and pushes
    # the list back. Keep the string-list value flowing so an answer list
    # built incrementally still reaches the I_ask call site.
    top = state.stack.peek_slot_at(0, 2)
    deep = state.stack.peek_slot_at(2, 2)
    state.stack.pop_bytes(2)
    state.stack.pop_bytes(2)
    if top is not None and top[0] == K_SLIST:
        lst, elem = top, deep
    elif deep is not None and deep[0] == K_SLIST:
        lst, elem = deep, top
    else:
        lst, elem = None, None
    if lst is not None:
        vals = list(lst[2]) if lst[2] else []
        vals.append(elem[2] if (elem is not None
                                and elem[0] == K_STR_ID) else None)
        state.stack.push(K_SLIST, 2, vals)
    else:
        state.stack.push(K_UNKNOWN, 2)


def _h_list_sub(state, ops):
    # 19/1A/1B: pop two lists (2+2), push one (2)
    state.stack.pop_bytes(2)
    state.stack.pop_bytes(2)
    state.stack.push(K_UNKNOWN, 2)


# Relational opcodes, keyed by the relation computed as `left op right`,
# where `left` is the deeper stack operand and `right` is the topmost.
_REL_FLIP = {"lt": "gt", "gt": "lt", "le": "ge", "ge": "le", "eq": "eq"}


def _cmp_field_const(state, op_kind):
    """Pop two 16-bit operands of a compare and return (field, intervals).

    Recognises `frame/quality op const` (and the operand-flipped form),
    reducing it to the interval set where the compare is true.
    """
    top = state.stack.peek_slot_at(0, 2)      # right operand
    deep = state.stack.peek_slot_at(2, 2)     # left operand
    state.stack.pop_bytes(2)
    state.stack.pop_bytes(2)
    field, intervals = None, None
    const = None
    if deep is not None and top is not None:
        if deep[0] in (K_FRAME, K_QUALITY) and top[0] == K_INT and top[2] is not None:
            field, intervals = deep[0], _iv_rel(op_kind, top[2])
            const = top[2]
        elif top[0] in (K_FRAME, K_QUALITY) and deep[0] == K_INT and deep[2] is not None:
            # const op frame  ==  frame (flipped op) const
            field = top[0]
            intervals = _iv_rel(_REL_FLIP[op_kind], deep[2])
            const = deep[2]
    # Locks pass uses this — record the K_QUALITY constants the class compares
    # against. The bark/readable/dialog walks ignore lock_consts.
    if field == K_QUALITY and const is not None:
        state.lock_consts.append((op_kind, const))
    return field, intervals


def _h_cmp16(state, ops):
    # 24: equality compare of two 16-bit values.
    field, intervals = _cmp_field_const(state, "eq")
    state.stack.push(K_CMP, 2, (field, intervals))


def _h_cmp32(state, ops):
    state.stack.pop_bytes(4)
    state.stack.pop_bytes(4)
    state.stack.push(K_INT, 2)


def _h_cmpstr(state, ops):
    state.stack.pop_bytes(2)
    state.stack.pop_bytes(2)
    state.stack.push(K_INT, 2)


def _h_rel16_lt(state, ops):
    state.stack.push(K_CMP, 2, _cmp_field_const(state, "lt"))


def _h_rel16_le(state, ops):
    state.stack.push(K_CMP, 2, _cmp_field_const(state, "le"))


def _h_rel16_gt(state, ops):
    state.stack.push(K_CMP, 2, _cmp_field_const(state, "gt"))


def _h_rel16_ge(state, ops):
    state.stack.push(K_CMP, 2, _cmp_field_const(state, "ge"))


def _h_rel32(state, ops):
    state.stack.pop_bytes(4)
    state.stack.pop_bytes(4)
    state.stack.push(K_INT, 2)


def _h_not16(state, ops):
    # 30: 16-bit boolean not. Inverts a compare's true-set.
    slot = state.stack.peek_slot_at(0, 2)
    state.stack.pop_bytes(2)
    if slot is not None and slot[0] == K_CMP:
        field, intervals = slot[2]
        if field == K_DEAD:
            state.stack.push(K_CMP, 2, (field, not intervals))
        else:
            state.stack.push(K_CMP, 2, (field, _iv_not(intervals)))
    else:
        state.stack.push(K_INT, 2)


def _h_not32(state, ops):
    state.stack.pop_bytes(4)
    state.stack.push(K_INT, 2)


def _h_logical(state, ops):
    # 33/35: logical ops we don't model precisely.
    state.stack.pop_bytes(2)
    state.stack.pop_bytes(2)
    state.stack.push(K_INT, 2)


def _combine_cmp(state, merge, keep_one):
    """Pop two 16-bit booleans; if they are compares on the same field,
    combine their true-sets with `merge`. `keep_one` (used for &&) lets the
    result keep a single frame compare even when the other operand is opaque,
    since `frame-in-range && X` still implies the range when true.
    """
    top = state.stack.peek_slot_at(0, 2)
    deep = state.stack.peek_slot_at(2, 2)
    state.stack.pop_bytes(2)
    state.stack.pop_bytes(2)
    tc = top[2] if (top is not None and top[0] == K_CMP) else None
    dc = deep[2] if (deep is not None and deep[0] == K_CMP) else None
    if tc is not None and dc is not None and tc[0] == dc[0] and tc[0] is not None:
        state.stack.push(K_CMP, 2, (tc[0], merge(tc[1], dc[1])))
        return
    if keep_one:
        for c in (tc, dc):
            if c is not None and c[0] is not None and c[1] is not None:
                state.stack.push(K_CMP, 2, c)
                return
    state.stack.push(K_INT, 2)


def _h_and(state, ops):
    # 32: logical AND of two compares.
    _combine_cmp(state, _iv_and, keep_one=True)


def _h_or(state, ops):
    # 34: logical OR of two compares.
    _combine_cmp(state, _iv_or, keep_one=False)


def _h_ne16(state, ops):
    # 36: 16-bit not-equal. The true-set (frame != const) is the interval
    # complement; left as a compare so a following 0x30 NOT recovers `eq`.
    field, eq_intervals = _cmp_field_const(state, "eq")
    state.stack.push(K_CMP, 2, (field, _iv_not(eq_intervals)))


def _h_in_list(state, ops):
    # 38 xx yy: pop list id (2), pop element (size xx); push bool (2)
    state.stack.pop_bytes(2)
    state.stack.pop_bytes(ops[0])
    state.stack.push(K_INT, 2)


def _h_bit16(state, ops):
    state.stack.pop_bytes(2)
    state.stack.pop_bytes(2)
    state.stack.push(K_INT, 2)


def _h_bit_not16(state, ops):
    state.stack.pop_bytes(2)
    state.stack.push(K_INT, 2)


def _h_push_bp_byte(state, ops):
    if ops[0] in state.int_locals:
        state.stack.push(K_INT, 2, state.int_locals[ops[0]])
    else:
        state.stack.push(state.locals.get(ops[0], K_INT), 2)


def _h_push_bp_u16(state, ops):
    if ops[0] in state.int_locals:
        state.stack.push(K_INT, 2, state.int_locals[ops[0]])
    else:
        state.stack.push(state.locals.get(ops[0], K_INT), 2)


def _h_push_bp_u32(state, ops):
    state.stack.push(K_UNKNOWN, 4)


def _h_push_str_local(state, ops):
    # 41 xx: push 16-bit string id from BP+xx. For a bark-local we carry its
    # accumulated literal text so a following concat can extend it.
    off = ops[0]
    text = state.str_acc.get(off) if off in state.bark_locals else None
    state.stack.push(K_STR_ID, 2, text)


def _h_push_list_local(state, ops):
    # 42/43 xx: push a list id from BP+xx. Carry a recovered string-list value
    # so an answer list stashed in a local can still be read at I_ask.
    vals = state.slist_locals.get(ops[0])
    if vals is not None:
        state.stack.push(K_SLIST, 2, list(vals))
    else:
        state.stack.push(K_UNKNOWN, 2)


def _h_push_element(state, ops):
    # 44 xx yy: pop index (2) + list id (2); push xx bytes (slist if yy)
    elem_size = ops[0]
    yy = ops[1]
    state.stack.pop_bytes(2)
    state.stack.pop_bytes(2)
    if yy:
        state.stack.push(K_STR_ID, elem_size)
    else:
        state.stack.push(K_UNKNOWN, elem_size)


def _h_push_huge(state, ops):
    # 45 xx yy: push yy bytes from BP+xx
    state.stack.push(K_UNKNOWN, ops[1])


def _h_push_bp_addr(state, ops):
    # 4B xx: push 4-byte pointer to BP+xx
    state.stack.push(K_UNKNOWN, 4)


def _h_indirect_push(state, ops):
    # 4C xx: pop 4-byte ptr, push xx bytes from that location
    state.stack.pop_bytes(4)
    state.stack.push(K_UNKNOWN, ops[0])


def _h_indirect_pop(state, ops):
    # 4D xx: pop 4-byte ptr + xx bytes
    state.stack.pop_bytes(4)
    state.stack.pop_bytes(ops[0])


def _h_push_global(state, ops):
    state.stack.push(K_INT, 2)


def _h_pop_global(state, ops):
    state.stack.pop_bytes(2)


def _h_ret(state, ops):
    state.stack.clear()


def _h_jne(state, ops):
    # 51 xx xx: pop a 16-bit cond, jump if zero. The fall-through path is the
    # one where the condition held, so it carries the compare's true-set
    # until the jump target is reached.
    cond_slot = state.stack.peek_slot_at(0, 2)
    state.stack.pop_bytes(2)
    target = state.jump_target  # filled in by caller
    if cond_slot is not None and cond_slot[0] == K_CMP:
        field, intervals = cond_slot[2]
        if field is not None and intervals:
            state.push_filter(target, field, intervals)


def _h_jmp(state, ops):
    # 52 xx xx: unconditional jump. Falls through is dead code until target reached.
    pass


def _h_suspend(state, ops):
    pass


def _h_implies(state, ops):
    # 54 01 01: pop 2 pids (2+2), push one (2)
    state.stack.pop_bytes(2)
    state.stack.pop_bytes(2)
    state.stack.push(K_INT, 2)


def _h_spawn(state, ops):
    # 57 aa tt xx xx yy yy: spawn a process running class `xx` at offset `yy`,
    # then pop the 4-byte thisptr (arg_bytes stay on stack for the caller to
    # clean). Result pid into temp32.
    state.record_spawn(ops[2] | (ops[3] << 8), ops[4] | (ops[5] << 8))
    state.stack.pop_bytes(4)
    state.result = (K_UNKNOWN, 4, None)


def _h_spawn_inline(state, ops):
    # 58 xx xx yy yy zz zz tt uu: pushes the new pid as 16-bit. Pops nothing.
    state.stack.push(K_INT, 2)


def _h_push_pid(state, ops):
    state.stack.push(K_INT, 2)


def _h_init_locals(state, ops):
    # 5A xx: allocate xx (aligned up to 2) zero bytes for locals
    xx = ops[0]
    if xx & 1:
        xx += 1
    if xx > 0:
        state.stack.push(K_INT, xx, 0)


def _h_debug(state, ops):
    pass


def _h_push_result8(state, ops):
    # 5D: push result as 8-bit bool. A classifier call leaves a tagged
    # compare in the result register; propagate it as a K_CMP.
    if state.result and state.result[0] == K_CMP:
        state.stack.push(K_CMP, 2, state.result[2])
    else:
        state.stack.push(K_INT, 2)


def _h_push_result16(state, ops):
    # 5E: push result as 16-bit. If result is tagged (K_FRAME / K_CMP / …),
    # propagate it.
    kind = state.result[0] if state.result else K_UNKNOWN
    if kind == K_CMP:
        state.stack.push(K_CMP, 2, state.result[2])
    elif kind in (K_FRAME, K_QUALITY):
        state.stack.push(kind, 2)
    elif kind == K_STR_ID:
        # A string result (e.g. I_getName) — carry its literal text so a
        # following concat folds it into the surrounding line.
        state.stack.push(K_STR_ID, 2, state.result[2])
    else:
        state.stack.push(K_INT, 2)


def _h_push_result32(state, ops):
    kind = state.result[0] if state.result else K_UNKNOWN
    if kind in (K_FRAME, K_QUALITY):
        state.stack.push(kind, 4)
    else:
        state.stack.push(K_INT, 4)


def _h_sext16to32(state, ops):
    state.stack.pop_bytes(2)
    state.stack.push(K_INT, 4)


def _h_trunc32to16(state, ops):
    state.stack.pop_bytes(4)
    state.stack.push(K_INT, 2)


def _h_free_bp(state, ops):
    # 62/63/64 xx: free a string/slist/list referenced by local. No stack effect.
    pass


def _h_free_sp(state, ops):
    # 65/66/67 xx: free string/list/slist at SP+xx. No stack pop.
    pass


def _h_push_str_ptr_local(state, ops):
    # 69 xx: push 4-byte pointer to the string stored in local BP+xx.
    # The local's text is unknown to us.
    state.stack.push(K_STR_PTR, 4, None)


def _h_str_to_ptr(state, ops):
    # 6B: pop 16-bit string id, push 4-byte string pointer. Propagate the
    # literal text if known.
    slot = state.stack.peek_slot_at(0, 2)
    state.stack.pop_bytes(2)
    text = slot[2] if (slot is not None and slot[0] == K_STR_ID) else None
    state.stack.push(K_STR_PTR, 4, text)


def _h_param_pid_chg(state, ops):
    # 6C xx yy: copy local string/list/slist into current proc. No stack effect.
    pass


def _h_push_result32_proc(state, ops):
    # 6D: push 32-bit process result
    state.stack.push(K_UNKNOWN, 4)


def _h_move_sp(state, ops):
    # 6E xx: move SP by signed xx; xx > 0 pops xx bytes.
    #
    # The negative form is almost always the caller-side cleanup right after
    # a calli (e.g. `0F getFrame` then `6E FC`). _h_calli already pops the
    # intrinsic args in this model, so that cleanup must be a no-op here —
    # modelling it as a push leaves 4 stray bytes that wedge between adjacent
    # compare results and break && / || chains across two getFrame() calls.
    v = ops[0]
    if v >= 0x80:
        v -= 0x100
    if v > 0:
        state.stack.pop_bytes(v)


def _h_push_sp_addr(state, ops):
    state.stack.push(K_UNKNOWN, 4)


def _h_loop(state, ops):
    # 70 xx yy zz: complex loop initializer. Pops yy bytes (loopscript) +
    # 2 + 2; pushes a fixed-size stack frame (~0x34 bytes for area search).
    state.stack._lost = True
    state.stack.clear()


def _h_loop_next(state, ops):
    # 73: pushes a 16-bit bool flag for whether the loop has more items.
    state.stack.push(K_INT, 2)


def _h_loopscr(state, ops):
    # 74 xx: push 1 byte onto the stack (loopscript builder)
    state.stack.push(K_INT, 1)


def _h_foreach(state, ops):
    # 75/76 xx yy zz zz: depending on whether loop ends, may pop 4 bytes.
    # We can't tell statically, so just mark uncertain.
    state.stack._lost = True
    state.stack.clear()


def _h_setinfo(state, ops):
    # 77: pop itemnum (2) + type (2)
    state.stack.pop_bytes(2)
    state.stack.pop_bytes(2)


def _h_proc_exclude(state, ops):
    pass


def _h_end(state, ops):
    state.stack.clear()


# (operand_bytes, handler)
OPCODE_TABLE = {
    0x00: (1, _h_pop_imm_byte),
    0x01: (1, _h_pop_imm16),
    0x02: (1, _h_pop_imm32),
    0x03: (2, _h_pop_huge),
    0x08: (0, _h_pop_result_long),
    0x09: (3, _h_assign_element),
    0x0A: (1, _h_push_sbyte),
    0x0B: (2, _h_push_u16),
    0x0C: (4, _h_push_u32),
    # 0x0D is variable-length, handled inline
    0x0E: (2, _h_create_list),
    # 0x0F is variable-length (operand byte tells nothing extra, but we
    # treat it as fixed 3 operand bytes); handled inline-ish via _h_calli
    0x0F: (3, _h_calli),
    0x11: (4, _h_intra_call),
    0x12: (0, _h_pop16_to_temp),
    0x13: (0, _h_pop32_to_temp),
    0x14: (0, _h_arith16),
    0x15: (0, _h_arith32),
    0x16: (0, _h_concat),
    0x17: (0, _h_list_append),
    0x19: (1, _h_list_sub),
    0x1A: (1, _h_list_sub),
    0x1B: (1, _h_list_sub),
    0x1C: (0, _h_arith16),
    0x1D: (0, _h_arith32),
    0x1E: (0, _h_arith16),
    0x1F: (0, _h_arith32),
    0x20: (0, _h_arith16),
    0x21: (0, _h_arith32),
    0x22: (0, _h_arith16),
    0x23: (0, _h_arith32),
    0x24: (0, _h_cmp16),
    0x25: (0, _h_cmp32),
    0x26: (0, _h_cmpstr),
    0x28: (0, _h_rel16_lt),
    0x29: (0, _h_rel32),
    0x2A: (0, _h_rel16_le),
    0x2B: (0, _h_rel32),
    0x2C: (0, _h_rel16_gt),
    0x2D: (0, _h_rel32),
    0x2E: (0, _h_rel16_ge),
    0x2F: (0, _h_rel32),
    0x30: (0, _h_not16),
    0x31: (0, _h_not32),
    0x32: (0, _h_and),
    0x33: (0, _h_logical),
    0x34: (0, _h_or),
    0x35: (0, _h_logical),
    0x36: (0, _h_ne16),
    0x37: (0, _h_rel32),
    0x38: (2, _h_in_list),
    0x39: (0, _h_bit16),
    0x3A: (0, _h_bit16),
    0x3B: (0, _h_bit_not16),
    0x3C: (0, _h_bit16),
    0x3D: (0, _h_bit16),
    0x3E: (1, _h_push_bp_byte),
    0x3F: (1, _h_push_bp_u16),
    0x40: (1, _h_push_bp_u32),
    0x41: (1, _h_push_str_local),
    0x42: (2, _h_push_list_local),
    0x43: (1, _h_push_list_local),
    0x44: (2, _h_push_element),
    0x45: (2, _h_push_huge),
    0x4B: (1, _h_push_bp_addr),
    0x4C: (1, _h_indirect_push),
    0x4D: (1, _h_indirect_pop),
    0x4E: (3, _h_push_global),
    0x4F: (3, _h_pop_global),
    0x50: (0, _h_ret),
    0x51: (2, _h_jne),
    0x52: (2, _h_jmp),
    0x53: (0, _h_suspend),
    0x54: (2, _h_implies),
    0x57: (6, _h_spawn),
    0x58: (8, _h_spawn_inline),
    0x59: (0, _h_push_pid),
    0x5A: (1, _h_init_locals),
    0x5B: (2, _h_debug),
    0x5C: (11, _h_debug),
    0x5D: (0, _h_push_result8),
    0x5E: (0, _h_push_result16),
    0x5F: (0, _h_push_result32),
    0x60: (0, _h_sext16to32),
    0x61: (0, _h_trunc32to16),
    0x62: (1, _h_free_bp),
    0x63: (1, _h_free_bp),
    0x64: (1, _h_free_bp),
    0x65: (1, _h_free_sp),
    0x66: (1, _h_free_sp),
    0x67: (1, _h_free_sp),
    0x69: (1, _h_push_str_ptr_local),
    0x6B: (0, _h_str_to_ptr),
    0x6C: (2, _h_param_pid_chg),
    0x6D: (0, _h_push_result32_proc),
    0x6E: (1, _h_move_sp),
    0x6F: (1, _h_push_sp_addr),
    0x70: (3, _h_loop),
    0x73: (0, _h_loop_next),
    0x74: (1, _h_loopscr),
    0x75: (4, _h_foreach),
    0x76: (4, _h_foreach),
    0x77: (0, _h_setinfo),
    0x78: (0, _h_proc_exclude),
    # 0x79 is a Crusader-only opcode but appears at the tail of U8 classes
    # as a sentinel; treat as end-of-class when there's no room for operands.
    0x79: (2, _h_end),
    0x7A: (0, _h_end),
}


# ---------------------------------------------------------------------------
# Per-class symbolic execution
# ---------------------------------------------------------------------------

class State:
    """Per-class execution state."""

    def __init__(self, classid, name):
        self.classid = classid
        self.name = name
        self.stack = Stack()
        self.result = (K_UNKNOWN, 4, None)
        # BP-relative locals known to hold a frame/quality value. Persists
        # across jump-target stack resets since locals are memory, not stack.
        self.locals = {}
        # BP-relative locals known to hold a plain integer constant.
        self.int_locals = {}
        # BP-relative locals known to hold a recovered string-list value.
        self.slist_locals = {}
        # bark records: list of dict(text, kind, calli_off, frames?, qualities?)
        self.barks = []
        # readable records: list of dict(type, text, kind, gump, func,
        # frames?, qualities?) — book/scroll/grave/plaque text from
        # non-look() events.
        self.readables = []
        # spawn records: list of dict(cls, off, frames?, qualities?) — process
        # spawns (opcode 0x57). A book/scroll item dispatches to a library
        # class' text function via a gated spawn.
        self.spawns = []
        # Conversation hallmarks seen while walking: I_ask (presents a player
        # answer menu) and I_getName (splices the player's name into a line).
        # Used to tell a real shared conversation (the SORCERER Disciples)
        # from a generic status bark a non-NPC class delegates to ("Locked
        # Door").
        self.saw_ask = False
        self.saw_getname = False
        # Code offset of the function currently being walked (readable pass).
        self._cur_func = 0
        # Frame/quality filter regions inherited via the fall-through of a
        # JNE whose condition gated getFrame()/getQuality(). Each entry is
        # (end_pc, field, intervals); active while pc < end_pc.
        self._filters = []
        self.jump_target = None  # set transiently while handling jumps
        self._calli_off = None   # set transiently while handling 0x0F
        self.call_resolver = None  # (callee_class, off) -> classifier map
        # Locals that hold a string later handed to a bark intrinsic (the
        # look() idiom: build the name in a local across frame branches, then
        # bark it once). str_acc[off] tracks the known literal text — fragments
        # only, since an unknown prefix counts as "".
        self.bark_locals = set()
        self.str_acc = {}
        # lock_consts: list of (op_kind, constant) recorded by _cmp_field_const
        # whenever the class compares the held item's K_QUALITY against an
        # integer literal. The locks pass aggregates these across every
        # function in the class.
        self.lock_consts = []

    def expire_filters(self, pc):
        self._filters = [f for f in self._filters if pc < f[0]]

    def push_filter(self, end_pc, field, intervals):
        self._filters.append((end_pc, field, intervals))

    def record_bark(self, intrinsic, text, kind):
        rec = {
            "intrinsic": intrinsic,
            "text": text,
            "kind": kind,
            "calli_off": self._calli_off,
        }
        # A bark inside several nested frame gates must satisfy them all, so
        # intersect the active interval sets per field.
        gates = {}
        for _end_pc, field, intervals in self._filters:
            if field == K_DEAD:
                if intervals is True:
                    rec["dead"] = True
                continue
            if field not in (K_FRAME, K_QUALITY):
                continue
            gates[field] = (intervals if field not in gates
                            else _iv_and(gates[field], intervals))
        frames = _iv_frames(gates.get(K_FRAME))
        quals = _iv_frames(gates.get(K_QUALITY))
        if frames:
            rec["frames"] = set(frames)
        if quals:
            rec["qualities"] = set(quals)
        self.barks.append(rec)

    def record_ask(self, options):
        """Record an I_ask answer list (player dialogue choices). Appended to
        the same `barks` list so a function's spoken lines and choices stay in
        code order; the dialog pass tells them apart by the "ask" kind."""
        self.barks.append({
            "intrinsic": INTRINSIC_ASK,
            "text": None,
            "kind": "ask",
            "options": options,
            "calli_off": self._calli_off,
        })

    def _active_gates(self, rec):
        """Stamp `rec` with frame/quality sets from the active filters."""
        gates = {}
        for _end_pc, field, intervals in self._filters:
            if field not in (K_FRAME, K_QUALITY):
                continue
            gates[field] = (intervals if field not in gates
                            else _iv_and(gates[field], intervals))
        frames = _iv_frames(gates.get(K_FRAME))
        quals = _iv_frames(gates.get(K_QUALITY))
        if frames:
            rec["frames"] = set(frames)
        if quals:
            rec["qualities"] = set(quals)
        return rec

    def record_readable(self, rtype, text, kind, gump):
        """Record a book/scroll/grave/plaque text site, gated by whatever
        frame/quality filters are active (the same machinery as record_bark —
        a generic reader class switches on getQuality() to pick its text)."""
        self.readables.append(self._active_gates({
            "type": rtype,
            "text": text,
            "kind": kind,
            "gump": gump,
            "func": self._cur_func,
            "calli_off": self._calli_off,
        }))

    def record_spawn(self, callee_cls, callee_off):
        """Record a process spawn (opcode 0x57), gated like a readable. Book
        and scroll items dispatch to a library class' per-text function with
        one gated spawn per quality value."""
        self.spawns.append(self._active_gates({
            "cls": callee_cls,
            "off": callee_off,
            "func": self._cur_func,
        }))


def parse_flex(data):
    if not any(b == FLEX_HDR_PAD for b in data[:0x52]):
        sys.exit("Not a FLEX file (no 0x1A header padding)")
    count = struct.unpack_from("<I", data, 0x54)[0]
    if count > 4095:
        sys.exit(f"Improbable FLEX entry count {count}")
    entries = []
    pos = FLEX_TABLE_OFFSET
    for _ in range(count):
        off, size = struct.unpack_from("<II", data, pos)
        entries.append((off, size))
        pos += 8
    return entries


def get_entry(data, entries, idx):
    off, size = entries[idx]
    if size == 0:
        return b""
    return data[off:off + size]


def class_name(name_table, classid):
    base = 4 + 13 * classid
    if base + 13 > len(name_table):
        return ""
    name = name_table[base:base + 13]
    nul = name.find(0)
    if nul >= 0:
        name = name[:nul]
    return name.decode("latin-1", errors="replace").strip()


def find_jump_targets(code):
    """Pre-scan the bytecode to collect all jump destinations.

    Linear scan; we ignore unreachable code embedded inside variable-length
    ops because the operand bytes never match opcode 0x0D / 0x0F prefix
    patterns by accident in well-formed code. (If a misalignment happens we
    just generate a slightly conservative set of leaders, which only forces
    extra stack resets.)
    """
    targets = set()
    pc = 0
    n = len(code)
    while pc < n:
        op = code[pc]
        op_pc = pc
        pc += 1
        if op == 0x0D:
            if pc + 2 > n:
                break
            slen = code[pc] | (code[pc + 1] << 8)
            pc += 2 + slen + 1
            continue
        if op == 0x51 or op == 0x52:
            if pc + 2 > n:
                break
            rel = code[pc] | (code[pc + 1] << 8)
            if rel >= 0x8000:
                rel -= 0x10000
            tgt = pc + 2 + rel
            if 0 <= tgt < n:
                targets.add(tgt)
            pc += 2
            continue
        if op == 0x75 or op == 0x76:
            if pc + 4 > n:
                break
            rel = code[pc + 2] | (code[pc + 3] << 8)
            if rel >= 0x8000:
                rel -= 0x10000
            tgt = pc + 4 + rel
            if 0 <= tgt < n:
                targets.add(tgt)
            pc += 4
            continue
        info = OPCODE_TABLE.get(op)
        if info is None:
            break
        pc += info[0]
    return targets


def scan_bark_locals(code):
    """Return the set of BP-relative local slots whose string is later passed
    to a bark intrinsic via `69 xx` (push str ptr local). These are the
    locals whose accumulated literal text we want to track."""
    slots = set()
    last69 = None
    pc, n = 0, len(code)
    while pc < n:
        op = code[pc]
        pc += 1
        if op == 0x0D:
            if pc + 2 > n:
                break
            slen = code[pc] | (code[pc + 1] << 8)
            pc += 2 + slen + 1
            continue
        info = OPCODE_TABLE.get(op)
        if info is None:
            break
        ops = code[pc:pc + info[0]]
        pc += info[0]
        if op == 0x69 and len(ops) >= 1:
            last69 = ops[0]
        elif op == 0x0F and len(ops) >= 3:
            intrinsic = ops[1] | (ops[2] << 8)
            if intrinsic == INTRINSIC_BARK and ops[0] == 8 and last69 is not None:
                slots.add(last69)
    return slots


def branch_entry_leaders(code, targets):
    """Of the jump-target leaders, return those that are *branch entries* —
    reached only by a jump, never fallen into. A bark-local's accumulated
    text must be reset there (a fresh frame branch starts), but kept at a
    *merge* leader (where two branches of an if/else rejoin)."""
    NO_FALLTHROUGH = {0x50, 0x52, 0x79, 0x7A}  # ret / jmp / end
    nofall_ends = set()
    pc, n = 0, len(code)
    while pc < n:
        op = code[pc]
        pc += 1
        if op == 0x0D:
            if pc + 2 > n:
                break
            slen = code[pc] | (code[pc + 1] << 8)
            pc += 2 + slen + 1
            if op in NO_FALLTHROUGH:
                nofall_ends.add(pc)
            continue
        info = OPCODE_TABLE.get(op)
        if info is None:
            break
        pc += info[0]
        if op in NO_FALLTHROUGH:
            nofall_ends.add(pc)
    # A leader is a branch entry when the instruction physically preceding it
    # cannot fall through into it.
    return {t for t in targets if t in nofall_ends}


def reachable_set(code):
    """Control-flow reachable byte offsets, starting at offset 0.

    A U8 class is a single blob; the event table only marks where each
    handler *starts*, not where it ends. look() ends at its `ret` (0x50),
    and any code past that belongs to other functions in the same class
    (often the use()/ritual logic full of dialog). Walking only the reachable
    set guarantees we stop where look() returns.
    """
    n = len(code)
    seen = set()
    work = [0]
    while work:
        pc = work.pop()
        while 0 <= pc < n and pc not in seen:
            seen.add(pc)
            op = code[pc]
            pc += 1
            if op == 0x0D:
                if pc + 2 > n:
                    break
                slen = code[pc] | (code[pc + 1] << 8)
                pc += 2 + slen + 1
                continue
            info = OPCODE_TABLE.get(op)
            if info is None:
                break
            opnd_len = info[0]
            operands = code[pc:pc + opnd_len]
            pc += opnd_len
            if op in (0x50, 0x79, 0x7A):  # ret / end-of-class — path stops
                break
            if op == 0x52:  # unconditional jump
                rel = operands[0] | (operands[1] << 8)
                if rel >= 0x8000:
                    rel -= 0x10000
                pc += rel
                continue
            if op == 0x51:  # JNE: fall through and branch are both live
                rel = operands[0] | (operands[1] << 8)
                if rel >= 0x8000:
                    rel -= 0x10000
                work.append(pc + rel)
                continue
            if op in (0x75, 0x76):  # foreach: loop body and exit both live
                rel = operands[2] | (operands[3] << 8)
                if rel >= 0x8000:
                    rel -= 0x10000
                work.append(pc + rel)
                continue
    return seen


def look_range(class_data, code_len):
    """Return (start, end) of the look() handler within the bytecode, or None.

    The 32-entry event table holds offsets relative to byte 12 of the class;
    an offset of EVENT_TABLE (0x80) therefore points at the first bytecode
    byte. Event 0 is the look() handler (Item::look). The handler ends where
    the next event's function begins.
    """
    events = [struct.unpack_from("<I", class_data, CLASS_HEADER + 4 * i)[0]
              for i in range(32)]
    look = events[0]
    if look in (0, 0xFFFFFFFF) or look < EVENT_TABLE:
        return None
    start = look - EVENT_TABLE
    if start >= code_len:
        return None
    end = code_len
    for ev in events:
        if ev in (0, 0xFFFFFFFF) or ev < EVENT_TABLE:
            continue
        o = ev - EVENT_TABLE
        if start < o < end:
            end = o
    return (start, min(end, code_len))


def _decode_fn(code, start):
    """Decode opcodes from `start` until the first top-level ret (0x50).

    Returns a list of (offset, opcode, operands). Used by the classifier
    resolver, which only needs instruction shapes, not a symbolic stack.
    """
    out = []
    pc = start
    n = len(code)
    while 0 <= pc < n:
        op = code[pc]
        opc = pc
        pc += 1
        if op == 0x0D:
            if pc + 2 > n:
                break
            slen = code[pc] | (code[pc + 1] << 8)
            pc += 2 + slen + 1
            out.append((opc, op, b""))
            continue
        info = OPCODE_TABLE.get(op)
        if info is None:
            break
        opnd_len = info[0]
        out.append((opc, op, code[pc:pc + opnd_len]))
        pc += opnd_len
        if op == 0x50:
            break
    return out


_REL_OPCODES = {0x24: "eq", 0x28: "lt", 0x2A: "le", 0x2C: "gt", 0x2E: "ge"}


def resolve_classifier(code, start):
    """Resolve a compiler-generated frame classifier function.

    These helpers (e.g. PENT's reagent check) have a rigid shape: a chain of
    `param == N` blocks, each guarding a getFrame() range test that returns 1.
    Shapes like 398 (reagents) call such a helper instead of testing the
    frame inline. Returns {category: [frame, ...]} so the caller can gate its
    barks; {} when the function doesn't match the pattern.
    """
    instrs = _decode_fn(code, start)
    cats = {}
    for i in range(len(instrs) - 3):
        (_, op0, opr0), (_, op1, opr1) = instrs[i], instrs[i + 1]
        (o2, op2, _), (o3, op3, opr3) = instrs[i + 2], instrs[i + 3]
        # param == N  -> JNE skips this category
        if not (op0 in (0x3E, 0x3F, 0x40) and op1 == 0x0A
                and op2 == 0x24 and op3 == 0x51):
            continue
        category = opr1[0]
        rel = opr3[0] | (opr3[1] << 8)
        if rel >= 0x8000:
            rel -= 0x10000
        target = o3 + 3 + rel  # 0x51 has two operand bytes
        # Collect getFrame() range tests inside the category block.
        intervals = None
        saw_frame = False
        j = i + 4
        while j < len(instrs) and instrs[j][0] < target:
            oj, opj, oprj = instrs[j]
            if (opj == 0x0F and len(oprj) >= 3
                    and (oprj[1] | (oprj[2] << 8)) == INTRINSIC_GETFRAME):
                saw_frame = True
            elif (opj in _REL_OPCODES and saw_frame
                  and instrs[j - 1][1] == 0x0A):
                r = _iv_rel(_REL_OPCODES[opj], instrs[j - 1][2][0])
                intervals = r if intervals is None else _iv_and(intervals, r)
            j += 1
        frames = _iv_frames(intervals)
        if frames:
            cats[category] = frames
    return cats


def _run_walk(state, code, targets, reach, reset_leaders, warn):
    """Drive the symbolic interpreter over `code` until it ends or aborts.

    Shared by the look()-only bark walk and the all-events readable walk;
    every observable effect lands on `state` (barks, readables, filters).
    """
    classid = state.classid
    n = len(code)
    pc = 0
    while pc < n:
        # Reset stack at jump targets — we can't statically merge stack states
        # from multiple predecessors, so be conservative.
        if pc in targets:
            state.stack.clear()
            state.stack._lost = False
            state.result = (K_UNKNOWN, 4, None)
            # A branch-entry leader also starts a fresh bark-local string.
            if pc in reset_leaders:
                for bl in state.bark_locals:
                    state.str_acc[bl] = ""
            # Don't clear filters here; their end_pc handles their lifetime.
        # Expire stale filters before the opcode runs.
        state.expire_filters(pc)

        op = code[pc]
        op_pc = pc
        pc += 1
        # Instructions outside the reachable control flow are decoded only to
        # keep pc aligned; their handlers are skipped so trailing functions in
        # the class blob contribute nothing.
        live = op_pc in reach

        if op == 0x0D:
            # 0D xxxx <bytes> 00
            if pc + 2 > n:
                warn(f"class {classid:04X}: truncated push-string at {op_pc:#x}")
                return state
            slen = code[pc] | (code[pc + 1] << 8)
            pc += 2
            if pc + slen + 1 > n:
                warn(f"class {classid:04X}: truncated push-string body at {op_pc:#x}")
                return state
            text = code[pc:pc + slen].decode(TEXT_ENCODING, errors="replace")
            term = code[pc + slen]
            pc += slen + 1
            if term != 0 and live:
                warn(f"class {classid:04X}: push-string missing NUL at {op_pc:#x}")
            if live:
                state.stack.push(K_STR_ID, 2, text)
            continue

        # Trailing 0x79 sentinel without operand room == clean end.
        if op == 0x79 and pc + 2 > n:
            return state

        info = OPCODE_TABLE.get(op)
        if info is None:
            if live:
                warn(f"class {classid:04X}: unknown opcode {op:02X} at {op_pc:#x}; "
                     f"stopping walk (further text in this class may be missed)")
            return state

        opnd_len, handler = info
        if pc + opnd_len > n:
            warn(f"class {classid:04X}: truncated opcode {op:02X} at {op_pc:#x}")
            return state
        operands = code[pc:pc + opnd_len]
        operand_pc = pc
        pc += opnd_len

        # Provide jump target to the handler if needed (jumps are relative to
        # the instruction *after* the operand bytes).
        if op == 0x51 or op == 0x52:
            rel = operands[0] | (operands[1] << 8)
            if rel >= 0x8000:
                rel -= 0x10000
            state.jump_target = pc + rel
        elif op in (0x75, 0x76):
            rel = operands[2] | (operands[3] << 8)
            if rel >= 0x8000:
                rel -= 0x10000
            state.jump_target = pc + rel
        else:
            state.jump_target = None

        # Attach the calli offset for bark recording.
        if op == 0x0F:
            state._calli_off = op_pc + CODE_OFFSET
        else:
            state._calli_off = None

        if live:
            handler(state, operands)

    return state


def function_entries(code):
    """Split a class blob into its individual function offsets.

    A U8 class is a flat run of functions laid out back-to-back; each ends
    with a `ret` (0x50) followed by an end marker (0x79 / 0x7A), so the byte
    after every end marker starts the next function. Decoded linearly,
    handling the only variable-length opcode (0x0D push-string); stops at the
    first unknown opcode (whatever follows is then walked as one trailing
    function).
    """
    entries = [0]
    pc, n = 0, len(code)
    while pc < n:
        op = code[pc]
        pc += 1
        if op == 0x0D:
            if pc + 2 > n:
                break
            slen = code[pc] | (code[pc + 1] << 8)
            pc += 2 + slen + 1
            continue
        # 0x79/0x7A end a function; the next byte starts the next one. Note
        # that between functions 0x79 carries no operands (OPCODE_TABLE lists
        # it as 2-operand only for the trailing class sentinel), so the
        # boundary is the byte immediately after the opcode.
        if op in (0x79, 0x7A):
            if pc < n:
                entries.append(pc)
            continue
        info = OPCODE_TABLE.get(op)
        if info is None:
            break
        pc += info[0]
    return entries


def walk_class_readables(classid, name, class_data, warn, call_resolver=None):
    """Symbolically execute every function of one class, collecting the
    book/scroll/grave/plaque text passed to the readable intrinsics.

    Bark recovery walks look() only, but readable text is displayed from a
    generic reader class whose per-quality text lives in internal helper
    functions reached by intra-class calls, not the event table. Each
    function is an independent CFG, so the symbolic state (stack, locals,
    filters) is reset between them.
    """
    state = State(classid, name)
    state.call_resolver = call_resolver
    if len(class_data) <= CODE_OFFSET:
        return state
    code = class_data[CODE_OFFSET:]
    starts = function_entries(code)
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(code)
        seg = code[start:end]
        state.stack = Stack()
        state.result = (K_UNKNOWN, 4, None)
        state.locals = {}
        state.int_locals = {}
        state._filters = []
        state.bark_locals = set()
        state.str_acc = {}
        state._cur_func = start
        _run_walk(state, seg, find_jump_targets(seg), reachable_set(seg),
                  set(), warn)
    return state


def walk_class(classid, name, class_data, warn, call_resolver=None):
    """Symbolically execute one class' look() handler.

    Only the look() event is walked, so the recovered barks are item
    descriptions ("look at" text) rather than dialog from use()/other events.
    """
    state = State(classid, name)
    state.call_resolver = call_resolver
    if len(class_data) <= CODE_OFFSET:
        return state

    code = class_data[CODE_OFFSET:]
    rng = look_range(class_data, len(code))
    if rng is None:
        return state
    code = code[rng[0]:rng[1]]
    n = len(code)
    targets = find_jump_targets(code)
    reach = reachable_set(code)

    # Locals whose string is barked, and the leaders where their accumulated
    # text must be reset (a new frame branch begins).
    state.bark_locals = scan_bark_locals(code)
    for bl in state.bark_locals:
        state.str_acc[bl] = ""
    reset_leaders = (branch_entry_leaders(code, targets)
                     if state.bark_locals else set())

    _run_walk(state, code, targets, reach, reset_leaders, warn)
    return state


def walk_function_dialog(classid, name, code, start, end, warn,
                         call_resolver=None):
    """Symbolically execute one function (code[start:end]) of a conversation
    class. Returns (lines, spawns, conversational):

      lines   — the NPC's spoken bark lines and the player's I_ask answer
                options in code order (see dicts below)
      spawns  — the process spawns the function issued, so a thin delegator
                can be followed into the shared class it dispatches to
      conversational — True if the function presents a player menu (I_ask) or
                addresses the player by name (I_getName); distinguishes a real
                shared conversation from a generic status bark.

    Line dicts:
      {"s": text}        — a line the NPC speaks (I_bark)
      {"a": [opt, ...]}  — a set of answer choices the player picks (I_ask)
    """
    seg = code[start:end]
    state = State(classid, name)
    state.call_resolver = call_resolver
    state.bark_locals = scan_bark_locals(seg)
    for bl in state.bark_locals:
        state.str_acc[bl] = ""
    state._cur_func = start
    _run_walk(state, seg, find_jump_targets(seg), reachable_set(seg),
              set(), warn)

    # Barks and asks landed on state.barks in code order. Stamp each with its
    # within-function code offset (calli_off is op_pc + CODE_OFFSET) so the
    # caller can interleave the statically-recovered Avatar lines by code
    # position rather than clumping them at the end. The temporary "_o" key is
    # stripped before the dialog is emitted.
    raw = []
    for rec in state.barks:
        off = (rec.get("calli_off") or CODE_OFFSET) - CODE_OFFSET
        if rec["kind"] == "ask":
            raw.append(("a", rec["options"], off))
        elif rec["kind"] in ("literal", "accum") and rec.get("text"):
            raw.append(("s", rec["text"], off))
    # A bark-local is built fragment by fragment, recording an "accum" at
    # every store — so a spoken line that is a strict prefix of the next
    # one is an intermediate fragment; keep only the completed line. Also
    # drop a line identical to the one right before it.
    lines = []
    for i, (kind, val, off) in enumerate(raw):
        if kind == "a":
            lines.append({"a": val, "_o": off})
            continue
        nxt = raw[i + 1] if i + 1 < len(raw) else None
        if nxt and nxt[0] == "s" and nxt[1] != val and nxt[1].startswith(val):
            continue
        if lines and lines[-1].get("s") == val:
            continue
        lines.append({"s": val, "_o": off})
    return lines, state.spawns, (state.saw_ask or state.saw_getname)


def _function_end(starts, start, code_len):
    """End offset of the function beginning at `start`: the next function
    entry after it, or end of code."""
    end = code_len
    for s in starts:
        if start < s < end:
            end = s
    return end


# Opcodes that consume a 16-bit string id as an Avatar answer/keyword:
#   0x0E create_list   — bundle answer strings into an I_ask list
#   0x17 append        — add one option to a list being built
#   0x19 append_slist  — concatenate answer lists
#   0x26 strcmp        — match the player's pick to dispatch the response
# Bark/readable strings instead pass through 0x6B str_to_ptr first, so the
# "next op is one of these" test cleanly separates the Avatar's lines from
# the NPC's spoken text.
_AVATAR_OPT_NEXT = frozenset((0x0E, 0x17, 0x19, 0x26))
_OP_PUSH_STRING = 0x0D
_OP_STRCMP = 0x26  # response-dispatch compare: sits right before each reply


def _avatar_options_by_function(class_data):
    """Static recovery of the Avatar's selectable conversation lines, grouped
    by the function that presents them (keyed by u8_disasm body offset). Each
    value is a list of (within_function_offset, text) ordered by code position.

    The symbolic dialog walk holds each I_ask answer list on the operand stack
    while building it, and _run_walk clears the stack at every branch target
    ("can't merge stack states") — so any flag-gated menu option, which U8
    conversations use constantly, is lost, leaving most NPCs with a single
    recovered choice. This pass ignores control flow entirely: it reads the
    raw instruction stream for every push_string that feeds an answer list
    (create_list / append) or the response-dispatch compare (strcmp against
    the player's pick), which together enumerate the Avatar's lines including
    the gated ones.

    An option is pushed twice: once when the menu list is built (create_list /
    append, far from its reply) and again at the strcmp that dispatches the
    NPC's response to it. We keep one site per option and *prefer the strcmp
    site*, because it sits immediately before the matching reply — so when the
    caller merges these by offset with the NPC's spoken lines, each Avatar
    keyword lands right ahead of the answer it triggers. An option that is
    only ever menu-built (e.g. "Goodbye", which just ends the talk) keeps its
    menu offset.
    """
    from u8_disasm import _decode_function
    out = {}
    if len(class_data) <= CODE_OFFSET:
        return out
    # u8_disasm body coordinates: 12-byte class header stripped, opcodes at
    # 0x80 (after the 32-entry event table) — body[0x80:] == class_data[140:].
    body = class_data[CLASS_HEADER:]
    max_off = len(body)
    cur = 0x80
    while cur < max_off:
        fn, nxt = _decode_function(body, cur, max_off)
        if nxt <= cur or not fn.instrs:
            break
        cur = nxt
        ins = fn.instrs
        # norm-text -> (within_func_offset, text, is_dispatch). One entry per
        # distinct option; a later strcmp site upgrades an earlier menu site.
        best = {}
        for i in range(len(ins) - 1):
            if ins[i].op == _OP_PUSH_STRING and ins[i + 1].op in _AVATAR_OPT_NEXT:
                # u8_disasm decodes push_string as latin-1; re-decode under the
                # localized encoding (CP437 / cp932) the rest of the file uses.
                raw = ins[i].args.get("str", "")
                text = raw.encode("latin-1", "replace").decode(TEXT_ENCODING, "replace")
                t = text.strip()
                if len(t) < 2 or t.isdigit():
                    continue
                off = ins[i].offset - fn.offset
                is_disp = ins[i + 1].op == _OP_STRCMP
                norm = t.lower()
                prev = best.get(norm)
                if prev is None or (is_disp and not prev[2]):
                    best[norm] = (off, text, is_disp)
        if best:
            opts = sorted(best.values(), key=lambda e: e[0])
            out[fn.offset] = [(off, text) for off, text, _d in opts]
    return out


def _merge_dialog_lines(lines, opts):
    """Interleave the NPC's spoken lines with the Avatar's recovered answer
    lines by code position, so the conversation reads as Avatar question →
    NPC reply instead of all NPC lines followed by all Avatar lines.

    `lines` are the symbolic-walk results (each carrying a within-function
    offset in "_o"); its own I_ask answer menus are dropped because `opts`
    (from _avatar_options_by_function: (offset, text) pairs) enumerates the
    same options — plus the flag-gated ones the walk loses — already placed at
    their dispatch site. Returns line dicts with the internal "_o" stripped.
    """
    merged = [(ln.get("_o", 0), {"s": ln["s"]}) for ln in lines if "s" in ln]
    merged += [(off, {"a": [text]}) for off, text in opts]
    merged.sort(key=lambda e: e[0])
    return [d for _off, d in merged]


# Opcodes used by the conversation-tree reconstruction (see below).
_OP_CREATE_LIST = 0x0E
_OP_APPEND_SLIST = 0x19
_OP_REMOVE_SLIST = 0x1A
_OP_STR_TO_PTR = 0x6B   # a bark/readable string is wrapped in str_to_ptr
_OP_JNE = 0x51


def _class_function_instrs(class_data):
    """Decode every function of a class once. Returns {body-offset: [Instr]},
    where body-offset is the u8_disasm coordinate (== code-offset + EVENT_TABLE
    == NONFIXED function start + EVENT_TABLE)."""
    from u8_disasm import _decode_function
    out = {}
    if len(class_data) <= CODE_OFFSET:
        return out
    body = class_data[CLASS_HEADER:]
    max_off = len(body)
    cur = 0x80
    while cur < max_off:
        fn, nxt = _decode_function(body, cur, max_off)
        if nxt <= cur or not fn.instrs:
            break
        cur = nxt
        out[fn.offset] = fn.instrs
    return out


def _reveal_op_after(instrs, i):
    """A menu option is added/removed by wrapping it in a singleton list and
    splicing it: push_string; create_list; push_slist_bp; {append_slist |
    remove_slist}. Given a push_string at index i, return the splice op
    (append_slist = newly-revealed topic, remove_slist = hidden topic) or None
    if this push_string isn't a menu mutation."""
    n = len(instrs)
    if i + 1 >= n or instrs[i + 1].op != _OP_CREATE_LIST:
        return None
    for j in range(i + 2, min(i + 6, n)):
        if instrs[j].op in (_OP_APPEND_SLIST, _OP_REMOVE_SLIST):
            return instrs[j].op
        if instrs[j].op == _OP_PUSH_STRING:
            break
    return None


def _build_conversation_tree(lines, instrs, fn_off):
    """Reconstruct a first-revealer dialogue tree for one conversation
    function, or None if it isn't a menu conversation (no I_ask).

    U8 conversations are a single menu loop: build a list of options, ask() the
    player to pick, then a flat chain of `strcmp pick,"keyword" / jne` blocks
    dispatches the reply. Each option's block barks a response and grows or
    shrinks the menu via append_slist / remove_slist — so an option *reveals*
    the child topics it append_slists. We attribute each topic to the first
    option that reveals it (earliest in code; the opening menu, before the
    first ask, is the root), and nest accordingly. A topic reachable from
    several options therefore appears once, under its first revealer.

    NPC reply text comes from the symbolic walk (`lines`, robust to barks built
    up from locals); the tree *shape* comes from the static instruction stream
    (`instrs`, in u8_disasm body coordinates). All offsets below are reduced to
    within-function so the two line up with the walk's "_o" bark offsets.

    Returns a list of nodes; each node is {"s": npc_line} or
    {"a": option, "c": [child nodes]} (the reply lines for an option are its
    leading {"s"} children, the topics it reveals are the {"a"} children).
    """
    from u8_disasm import jmp_target
    n = len(instrs)
    if not n:
        return None
    asks = [I for I in instrs
            if I.op == 0x0F and I.args.get("intrinsic") == INTRINSIC_ASK]
    if not asks:
        return None
    first_ask = asks[0].offset - fn_off

    def dstr(I):
        return I.args.get("str", "").encode("latin-1", "replace").decode(
            TEXT_ENCODING, "replace")

    def norm(s):
        return s.strip().lower()

    # Dispatch blocks: push_string; strcmp; jne -> target. The reply handler is
    # [instr after the jne, jne target); the chain links one block to the next.
    blocks = {}        # norm -> (region_start, region_end, display_text)
    block_order = []   # block norms in code order
    for i in range(n - 2):
        if not (instrs[i].op == _OP_PUSH_STRING
                and instrs[i + 1].op == _OP_STRCMP
                and instrs[i + 2].op == _OP_JNE):
            continue
        disp = dstr(instrs[i])
        t = disp.strip()
        if len(t) < 2 or t.isdigit():
            continue
        nxt_off = instrs[i + 3].offset if i + 3 < n else instrs[i + 2].offset
        tgt = jmp_target(instrs[i + 2], nxt_off)
        a = (instrs[i + 3].offset if i + 3 < n else tgt) - fn_off
        b = tgt - fn_off
        cn = norm(disp)
        if cn not in blocks:
            blocks[cn] = (a, b, disp)
            block_order.append(cn)
    if not blocks:
        return None

    # Reveal edges, keeping the earliest reveal of each topic. ROOT (None) is
    # the opening menu built before the first ask.
    ROOT = None
    reveal = {}  # child_norm -> (offset, parent_norm, display_text)

    def consider(parent_norm, a, b):
        for k in range(n):
            o = instrs[k].offset - fn_off
            if not (a <= o < b) or instrs[k].op != _OP_PUSH_STRING:
                continue
            if _reveal_op_after(instrs, k) != _OP_APPEND_SLIST:
                continue
            disp = dstr(instrs[k])
            t = disp.strip()
            if len(t) < 2 or t.isdigit():
                continue
            cn = norm(disp)
            prev = reveal.get(cn)
            if prev is None or o < prev[0]:
                reveal[cn] = (o, parent_norm, disp)

    consider(ROOT, -1, first_ask)
    for cn in block_order:
        a, b, _ = blocks[cn]
        consider(cn, a, b)

    children = {}
    for cn, (o, pn, disp) in reveal.items():
        children.setdefault(pn, []).append((o, cn, disp))
    for pn in children:
        children[pn].sort(key=lambda e: e[0])

    barks = sorted((ln["_o"], ln["s"]) for ln in lines if "s" in ln)

    def replies(a, b):
        out = []
        for o, t in barks:
            if a <= o < b and (not out or out[-1] != t):
                out.append(t)
        return out

    visited = set()

    def build(cn, disp):
        node = {"a": disp}
        if cn in visited:
            return node
        visited.add(cn)
        kids = []
        blk = blocks.get(cn)
        if blk:
            kids += [{"s": r} for r in replies(blk[0], blk[1])]
        for _o, ccn, cdisp in children.get(cn, []):
            if ccn != cn:
                kids.append(build(ccn, cdisp))
        if kids:
            node["c"] = kids
        return node

    top = [{"s": r} for r in replies(-1, first_ask)]
    for _o, cn, disp in children.get(ROOT, []):
        top.append(build(cn, disp))
    # Topics that are dispatched but never append_slisted (always-present in the
    # opening menu) attach at the root, in code order.
    for cn in block_order:
        if cn not in visited:
            top.append(build(cn, blocks[cn][2]))
    return top or None


def scan_avatar_lines(class_data):
    """Flat, de-duplicated union of the Avatar's lines across a whole class,
    in first-seen (code) order. See _avatar_options_by_function."""
    seen, out = set(), []
    for opts in _avatar_options_by_function(class_data).values():
        for _off, l in opts:
            k = l.strip().lower()
            if k not in seen:
                seen.add(k)
                out.append(l)
    return out


def walk_class_dialog(classid, name, class_data, warn, call_resolver=None):
    """Symbolically execute every function of an NPC conversation class,
    recovering the NPC's spoken bark lines and the player's I_ask answer
    options in code order.

    Returns a list of groups (one per function that contains any dialogue).
    A menu conversation becomes a first-revealer tree of nested nodes (see
    _build_conversation_tree); a function with no I_ask menu stays a flat list
    of line dicts with the Avatar's keyword lines interleaved by code position
    (see _merge_dialog_lines).
    """
    groups = []
    if len(class_data) <= CODE_OFFSET:
        return groups
    code = class_data[CODE_OFFSET:]
    starts = function_entries(code)
    opts_by_fn = _avatar_options_by_function(class_data)
    instrs_by_fn = _class_function_instrs(class_data)
    # Event 0 is look() — its barks are the NPC's "look-at" description
    # ("Devon", "fisherman", "man"), not conversation. Skip that function.
    look = look_range(class_data, len(code))
    look_start = look[0] if look else None
    for i, start in enumerate(starts):
        if start == look_start:
            continue
        end = starts[i + 1] if i + 1 < len(starts) else len(code)
        lines, _, _ = walk_function_dialog(classid, name, code, start, end,
                                           warn, call_resolver)
        # Body offset = code offset + the 32-entry event table (EVENT_TABLE),
        # which is exactly the u8_disasm function offset (verified equal).
        fn_off = start + EVENT_TABLE
        grp = None
        instrs = instrs_by_fn.get(fn_off)
        if instrs:
            grp = _build_conversation_tree(lines, instrs, fn_off)
        if grp is None:
            grp = _merge_dialog_lines(lines, opts_by_fn.get(fn_off, []))
        if grp:
            groups.append(grp)
    return groups


def walk_delegated_dialog(classid, name, class_data, get_class, name_table,
                          warn, call_resolver=None):
    """Recover dialogue for an NPC whose own class carries none, by following
    its event handlers into the shared library class they dispatch to.

    Some NPCs are thin shells: each (non-look) event handler just spawns a
    process running a function of a shared class. The six Sorcerer Disciples
    (Cardas, Daemos, Kothius, Mentar, Tallon, Emrichol) all dispatch their
    `use` conversation into one SORCERER function, which is why their own
    classes have no bark/ask opcodes. Walk each spawned target function and
    collect its lines. Auto-detecting the spawn target keeps this working
    across the per-language usecode recompiles (the offsets differ).
    """
    groups = []
    if len(class_data) <= CODE_OFFSET:
        return groups
    code = class_data[CODE_OFFSET:]
    starts = function_entries(code)
    look = look_range(class_data, len(code))
    look_start = look[0] if look else None
    seen = set()
    target_classes = {}
    for i, start in enumerate(starts):
        if start == look_start:
            continue
        end = starts[i + 1] if i + 1 < len(starts) else len(code)
        _lines, spawns, _ = walk_function_dialog(classid, name, code, start,
                                                 end, warn, call_resolver)
        for sp in spawns:
            # Only follow delegation into a *different* class, once each.
            if sp["cls"] == classid or (sp["cls"], sp["off"]) in seen:
                continue
            seen.add((sp["cls"], sp["off"]))
            tgt = get_class(sp["cls"])
            if not tgt or len(tgt) <= CODE_OFFSET:
                continue
            tcode = tgt[CODE_OFFSET:]
            # Spawn offsets are class-body relative (0x80 = first opcode);
            # shift to the CODE_OFFSET-relative coordinate used here.
            tstart = sp["off"] - EVENT_TABLE
            if not (0 <= tstart < len(tcode)):
                continue
            tend = _function_end(function_entries(tcode), tstart, len(tcode))
            tlines, _, tconv = walk_function_dialog(
                sp["cls"], class_name(name_table, sp["cls"]),
                tcode, tstart, tend, warn, call_resolver)
            # Only a genuine conversation (player menu or name splice) counts;
            # this rejects mechanism classes (doors) that spawn a status bark.
            if tlines and tconv:
                # The Avatar's lines live in the shared target class, not the
                # thin-shell NPC's own class — recover and interleave them the
                # same way as a self-contained NPC.
                topts = _avatar_options_by_function(tgt).get(
                    tstart + EVENT_TABLE, [])
                groups.append(_merge_dialog_lines(tlines, topts))
                target_classes[sp["cls"]] = tgt
    if groups:
        have = {o.strip().lower()
                for g in groups for ln in g if "a" in ln for o in ln["a"]}
        extra, seen_e = [], set()
        for tgt in target_classes.values():
            for l in scan_avatar_lines(tgt):
                k = l.strip().lower()
                if k not in have and k not in seen_e:
                    seen_e.add(k)
                    extra.append(l)
        if extra:
            groups.append([{"a": [l]} for l in extra])
    return groups


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def aggregate_barks(all_states, warn=lambda m: None):
    """Merge per-class look() barks into one descriptor table.

    Returns {shape: entry} where each entry has whichever of these apply:

      "default" : str
          The look() text returned without a frame/quality gate — the NPC
          name, the plain item name, the fallback description.
      "frames"  : {frame: text}
          Text gated on getFrame()==N (e.g. reagents, gems).
      "quality" : {quality: text}
          Text gated on getQuality()==N (e.g. book / scroll titles).

    A map object resolves to exactly one description: frames[frame], else
    quality[quality], else default. Barks gated on isDead() (the "dead
    <role>" corpse variants) are dropped — the alive name is the default.
    """
    table = {}

    def entry(shape_id):
        return table.setdefault(shape_id, {})

    def assign(sub_name, shape_id, keys, text, kind):
        sub = entry(shape_id).setdefault(sub_name, {})
        for k in keys:
            ks = str(k)
            if ks in sub and sub[ks] != text:
                warn(f"shape {shape_id}: {kind} {ks} has conflicting text "
                     f"{sub[ks]!r} vs {text!r}; keeping the first")
                continue
            sub[ks] = text

    for state in all_states:
        if not state.barks:
            continue
        shape_id = str(state.classid)
        for rec in state.barks:
            if rec["kind"] != "literal" or rec.get("dead"):
                continue
            text = rec["text"].rstrip()  # trailing whitespace is cosmetic
            if not text:
                continue
            frames = sorted(rec.get("frames", set()))
            quals = sorted(rec.get("qualities", set()))
            if frames:
                assign("frames", shape_id, frames, text, "frame")
            elif quals:
                assign("quality", shape_id, quals, text, "quality")
            else:
                # First ungated bark wins — look()'s primary description.
                entry(shape_id).setdefault("default", text)

    # Second pass: locals-accumulated barks. look() builds the name in a
    # local across frame branches, storing a fragment at a time, then barks
    # it once. Every store recorded an "accum" record under the gates active
    # then; for each gated key the fullest (longest) string is the complete
    # name. Real literal barks from the first pass take precedence.
    for state in all_states:
        shape_id = str(state.classid)
        best = {}  # (sub, frozenset(keys)) -> longest text
        for rec in state.barks:
            if rec["kind"] != "accum" or rec.get("dead"):
                continue
            # Fragments carry a leading separator space; the count word the
            # game prepends at runtime is gone, so trim both ends.
            text = rec["text"].strip()
            if not text:
                continue
            frames = frozenset(rec.get("frames", ()))
            quals = frozenset(rec.get("qualities", ()))
            if frames:
                key = ("frames", frames)
            elif quals:
                key = ("quality", quals)
            else:
                continue  # ungated fragment — too ambiguous to attribute
            if key not in best or len(text) > len(best[key]):
                best[key] = text
        for (sub, keys), text in best.items():
            d = entry(shape_id).setdefault(sub, {})
            for k in sorted(keys):
                d.setdefault(str(k), text)

    # Drop shapes that ended up with nothing usable.
    return {s: e for s, e in table.items() if e}


def _sort_numeric_keys(d):
    """Return a new dict with keys ordered by integer value."""
    return {k: d[k] for k in sorted(d, key=int)}


DEFAULT_GAME_DIR = "./ULTIMA8"


def find_game_file(game_dir, name):
    """Case-insensitively locate `name` anywhere under a U8 game install."""
    name_l = name.lower()
    for dirpath, _, files in os.walk(game_dir):
        for f in files:
            if f.lower() == name_l:
                return os.path.join(dirpath, f)
    raise FileNotFoundError(
        f"'{name}' not found under game directory '{game_dir}'. "
        f"Pass the correct path with --game-dir.")


# U8 ships one localized usecode FLX, named by a language-letter prefix:
# E)nglish, F)rench, G)erman, J)apanese, S)panish. Every extraction here keys
# on engine-level intrinsic ids and reads spawn targets straight from the
# bytecode, so all flavours parse the same way — only the recovered strings
# (barks, readables, dialogue) come out in that language.
USECODE_LANGS = {
    "E": "English", "F": "French", "G": "German",
    "J": "Japanese", "S": "Spanish",
}

# Encoding of the stored game text, by language. U8 is a DOS-era game with no
# Unicode. The Western releases share the game's CP437 DOS font, with accented
# letters at the classic high-byte positions (ä=0x84, ö=0x94, ü=0x81, ß=0xE1,
# Ä=0x8E, Ö=0x99, Ü=0x9A), so French/German/Spanish text must be decoded as
# CP437 — latin-1 garbles every accent (German 'Schlüssel' becomes 'Schl\x81ssel').
# English text is ASCII-only in practice, so latin-1 is harmless there. The
# Japanese (PC-98) release stores Shift-JIS, decoded via cp932 (its double-byte
# chars never contain 0x00, so the usecode's NUL string terminators stay
# unambiguous). Output is always Unicode (JSON is UTF-8, ensure_ascii=False).
USECODE_ENCODINGS = {
    "E": "latin-1", "F": "cp437", "G": "cp437",
    "J": "cp932",   "S": "cp437",
}

# Active text encoding — defaults to latin-1; main() overrides it once the
# install's language is known. The interpreter reads this module global when
# it decodes a push-string operand.
TEXT_ENCODING = "latin-1"


def find_usecode(game_dir):
    """Locate the localized USECODE FLX under a U8 install.

    Returns (path, language_letter) for the first of E/F/G/J/S USECODE.FLX
    found, preferring English when several are present. The letter keys
    USECODE_LANGS (display) and USECODE_ENCODINGS (text codec).
    """
    found = {}
    for dirpath, _, files in os.walk(game_dir):
        for f in files:
            n = f.upper()
            if (len(n) == len("EUSECODE.FLX") and n.endswith("USECODE.FLX")
                    and n[0] in USECODE_LANGS):
                found.setdefault(n[0], os.path.join(dirpath, f))
    for letter in ("E", "F", "G", "J", "S"):
        if letter in found:
            return found[letter], letter
    raise FileNotFoundError(
        f"no [EFGJS]USECODE.FLX found under game directory '{game_dir}'. "
        f"Pass the correct path with --game-dir.")


def resolve_english_or_spanish(usecode_flx):
    """Disambiguate the two builds that both ship as EUSECODE.FLX.

    The 'E' filename stands for English in one release and Español in the
    other, so find_usecode can't tell them apart by name — and the Spanish
    text is CP437 (accents at the DOS high-byte positions), so decoding it as
    English/latin-1 turns every accented letter into garbage (á=0xA0 becomes a
    non-breaking space, so "Quizás" reads as "Quiz s"). GUARD1's generic
    look-bark is a guaranteed-localized word — "guardsman" in English,
    "guardia" in Spanish — so it settles which build this is. Returns "E" or
    "S"; defaults to "E" if the marker can't be read. Mirrors the identical
    check in parse_schedules.resolve_english_or_spanish.
    """
    from u8_disasm import parse_eusecode
    GUARD1_CLASS = 1024 + 4   # palace guard; permanent NPC class id = 1024 + npc
    LOOK_EVENT = 0
    try:
        classes = parse_eusecode(usecode_flx)
    except Exception:
        return "E"
    guard = next((c for c in classes if c.class_id == GUARD1_CLASS), None)
    if guard is not None:
        ev0 = next((f for f in guard.functions if f.event == LOOK_EVENT), None)
        if ev0 is not None:
            barks = [i.args["str"] for i in ev0.instrs
                     if i.mnemonic == "push_string"]
            if barks and not barks[-1].lower().startswith("guardsman"):
                return "S"
    return "E"


def main(game_dir=DEFAULT_GAME_DIR, output=None, quiet=False):
    here = os.path.dirname(os.path.abspath(__file__))
    if output is None:
        output = os.path.join(here, "json", "barks.json")

    usecode_flx, lang = find_usecode(game_dir)
    if lang == "E":
        lang = resolve_english_or_spanish(usecode_flx)
    global TEXT_ENCODING
    TEXT_ENCODING = USECODE_ENCODINGS[lang]
    print(f"Using game directory: {game_dir} "
          f"({USECODE_LANGS[lang]} usecode: {os.path.basename(usecode_flx)}, "
          f"text encoding: {TEXT_ENCODING})", file=sys.stderr)
    with open(usecode_flx, "rb") as f:
        data = f.read()

    entries = parse_flex(data)
    name_table = get_entry(data, entries, 1)

    def warn(msg):
        if not quiet:
            print("warning:", msg, file=sys.stderr)

    # Resolver for interprocedural frame-classifier helpers (memoised).
    _classifier_cache = {}

    def call_resolver(callee_cls, callee_off):
        key = (callee_cls, callee_off)
        if key not in _classifier_cache:
            blob = (get_entry(data, entries, callee_cls + 2)
                    if 0 <= callee_cls + 2 < len(entries) else b"")
            cmap = {}
            if len(blob) > CODE_OFFSET and callee_off >= EVENT_TABLE:
                cmap = resolve_classifier(blob[CODE_OFFSET:],
                                          callee_off - EVENT_TABLE)
            _classifier_cache[key] = cmap
        return _classifier_cache[key]

    states = []
    for classid in range(len(entries) - 2):
        class_data = get_entry(data, entries, classid + 2)
        if not class_data:
            continue
        name = class_name(name_table, classid)
        states.append(walk_class(classid, name, class_data, warn,
                                  call_resolver))

    table = aggregate_barks(states, warn)

    # Order shape keys numerically, and frame/quality sub-keys numerically.
    merged = {}
    for shape in sorted(table, key=int):
        e = table[shape]
        out = {}
        if "default" in e:
            out["default"] = e["default"]
        if "frames" in e:
            out["frames"] = _sort_numeric_keys(e["frames"])
        if "quality" in e:
            out["quality"] = _sort_numeric_keys(e["quality"])
        merged[shape] = out

    out_dir = os.path.dirname(output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # Stats
    n_default = sum(1 for e in merged.values() if "default" in e)
    n_frame = sum(1 for e in merged.values() if "frames" in e)
    n_quality = sum(1 for e in merged.values() if "quality" in e)
    print(f"# {len(merged)} shapes -> {output}", file=sys.stderr)
    print(f"#   {n_default} with default, {n_frame} with frames, "
          f"{n_quality} with quality", file=sys.stderr)

    # ---- Readables: book/scroll/grave/plaque text -------------------------
    # Two shapes of reader exist:
    #   * self-contained (tombstones, plaques): the item class switches on
    #     getQuality() and calls I_readGrave/I_readPlaque inline.
    #   * library + dispatcher (books, scrolls): the item class ("book item")
    #     switches on getQuality() and *spawns* a function of a shared library
    #     class; that library function holds the literal text + I_readBook.
    # Pre-scan the bytecode for both the readable intrinsics and spawn edges.
    readable_ops = {INTRINSIC_READ_BOOK, INTRINSIC_READ_SCROLL,
                    INTRINSIC_READ_GRAVE, INTRINSIC_READ_PLAQUE}
    readable_classes = set()
    spawn_edges = {}              # caller classid -> set of target classid
    all_spawn_targets = set()
    for classid in range(len(entries) - 2):
        class_data = get_entry(data, entries, classid + 2)
        if not class_data or len(class_data) <= CODE_OFFSET:
            continue
        code = class_data[CODE_OFFSET:]
        pc, n = 0, len(code)
        while pc < n:
            op = code[pc]
            pc += 1
            if op == 0x0D:
                if pc + 2 > n:
                    break
                pc += 2 + (code[pc] | (code[pc + 1] << 8)) + 1
                continue
            if op in (0x79, 0x7A):
                continue
            info = OPCODE_TABLE.get(op)
            if info is None:
                break
            if op == 0x0F and pc + 2 < n:
                if code[pc + 1] in readable_ops and code[pc + 2] == 0:
                    readable_classes.add(classid)
            elif op == 0x57 and pc + 3 < n:
                tgt = code[pc + 2] | (code[pc + 3] << 8)
                spawn_edges.setdefault(classid, set()).add(tgt)
                all_spawn_targets.add(tgt)
            pc += info[0]

    dispatchers = {c for c, tg in spawn_edges.items()
                   if tg & readable_classes}

    rstates = {}
    for classid in sorted(readable_classes | dispatchers):
        class_data = get_entry(data, entries, classid + 2)
        rstates[classid] = walk_class_readables(
            classid, class_name(name_table, classid), class_data, warn,
            call_resolver)

    # (readable class, function offset) -> first literal readable in it, so a
    # dispatcher's gated spawn can be resolved to the spawned function's text.
    libfunc = {}
    for L in readable_classes:
        for rec in rstates[L].readables:
            if rec["kind"] != "literal" or not rec["text"].rstrip():
                continue
            libfunc.setdefault((L, rec["func"]), rec)

    rtable = {}

    def emit(shape, rec, frames, quals):
        text = rec["text"].rstrip()
        if not text:
            return
        e = rtable.setdefault(str(shape), {})
        e.setdefault("type", rec["type"])
        if rec.get("gump") is not None:
            e.setdefault("gump", rec["gump"])
        if frames:
            d = e.setdefault("frames", {})
            for k in frames:
                d.setdefault(str(k), text)
        elif quals:
            d = e.setdefault("quality", {})
            for k in quals:
                d.setdefault(str(k), text)
        else:
            e.setdefault("default", text)

    # Self-contained readers (tombstones, plaques): the readable intrinsics
    # are called inline under their own getQuality() gates — emit directly.
    # A pure library (a spawn target whose own text is all ungated) is left
    # to the dispatcher pass so its text lands on the real item shape.
    self_contained = set()
    for c in readable_classes:
        recs = [r for r in rstates[c].readables if r["kind"] == "literal"]
        gated = any(r.get("frames") or r.get("qualities") for r in recs)
        if recs and (gated or c not in all_spawn_targets):
            self_contained.add(c)
            for rec in recs:
                emit(c, rec, sorted(rec.get("frames", ())),
                     sorted(rec.get("qualities", ())))

    # Dispatchers (book/scroll/tombstone items): each gated spawn into another
    # class resolves to that class' text. A spawn into a self-contained class
    # re-runs that class' own getQuality() switch on the *item's* quality, so
    # the dispatcher shape inherits the whole table; a spawn into a pure
    # library picks the one function the dispatcher selected.
    for c in dispatchers:
        for sp in rstates[c].spawns:
            if sp["cls"] == c:
                continue
            if sp["cls"] in self_contained:
                src = rtable.get(str(sp["cls"]))
                if src:
                    dst = rtable.setdefault(str(c), {})
                    dst.setdefault("type", src["type"])
                    if "gump" in src:
                        dst.setdefault("gump", src["gump"])
                    for sub in ("frames", "quality"):
                        if sub in src:
                            d = dst.setdefault(sub, {})
                            for k, v in src[sub].items():
                                d.setdefault(k, v)
                    if "default" in src:
                        dst.setdefault("default", src["default"])
                continue
            rec = libfunc.get((sp["cls"], sp["off"] - EVENT_TABLE))
            if rec is not None:
                emit(c, rec, sorted(sp.get("frames", ())),
                     sorted(sp.get("qualities", ())))

    rmerged = {}
    for shape in sorted(rtable, key=int):
        e = rtable[shape]
        out = {"type": e["type"]}
        if "gump" in e:
            out["gump"] = e["gump"]
        if "default" in e:
            out["default"] = e["default"]
        if "frames" in e:
            out["frames"] = _sort_numeric_keys(e["frames"])
        if "quality" in e:
            out["quality"] = _sort_numeric_keys(e["quality"])
        rmerged[shape] = out

    readables_path = os.path.join(out_dir or ".", "readables.json")
    with open(readables_path, "w", encoding="utf-8") as f:
        json.dump(rmerged, f, indent=2, ensure_ascii=False)
        f.write("\n")
    n_text = sum(len(e.get("frames", {})) + len(e.get("quality", {}))
                 + (1 if "default" in e else 0) for e in rmerged.values())
    print(f"# {len(rmerged)} readable shapes ({n_text} texts) "
          f"-> {readables_path}", file=sys.stderr)

    # ---- NPC dialogue ----------------------------------------------------
    # A non-monster NPC runs usecode class (objid + 1024) — see Pentagram
    # Item::callUsecodeEvent. Walk each such class and recover the NPC's
    # spoken lines and the player's I_ask answer choices, grouped per
    # usecode function (≈ a conversation branch).
    def get_class_data(cid):
        idx = cid + 2
        return (get_entry(data, entries, idx)
                if 0 <= idx < len(entries) else None)

    dialog = {}
    for npcnum in range(1, 256):
        classid = npcnum + 1024
        idx = classid + 2
        if idx >= len(entries):
            break
        class_data = get_entry(data, entries, idx)
        if not class_data or len(class_data) <= CODE_OFFSET:
            continue
        cname = class_name(name_table, classid)
        groups = walk_class_dialog(classid, cname, class_data, warn,
                                   call_resolver)
        # A thin-shell NPC (e.g. the Sorcerer Disciples) carries no dialogue
        # of its own — it spawns the conversation into a shared library class.
        # Follow that delegation so the shared lines land on the NPC.
        if not groups:
            groups = walk_delegated_dialog(classid, cname, class_data,
                                           get_class_data, name_table, warn,
                                           call_resolver)
        if groups:
            dialog[str(npcnum)] = groups

    # The four Titans (Hydros, Pyros, Stratos, Lithos) are placed in the world
    # as plain actors rather than NPCs, so their usecode class is their shape
    # number — Item::callUsecodeEvent uses class_id = shape for a non-permanent
    # actor. Recover their conversations the same way, keyed by "s<shape>" so
    # the viewer can resolve them by shape when the object carries no npcnum.
    # Crowd NPCs (peasants 707/835/836, peasant children 708, guards 405/574,
    # generic Theurgist 269, generic Necromancer 623) work the same way: they
    # have no npcnum and dispatch dialog through their shape class, so include
    # them in the shape-keyed walk.
    for shape in (80, 109, 385, 433, 269, 405, 574, 623, 707, 708, 835, 836):
        class_data = get_entry(data, entries, shape + 2)
        if not class_data or len(class_data) <= CODE_OFFSET:
            continue
        groups = walk_class_dialog(shape, class_name(name_table, shape),
                                   class_data, warn, call_resolver)
        if groups:
            dialog["s" + str(shape)] = groups

    dialog_path = os.path.join(out_dir or ".", "dialog.json")
    with open(dialog_path, "w", encoding="utf-8") as f:
        json.dump(dialog, f, indent=1, ensure_ascii=False)
        f.write("\n")
    n_lines = sum(len(g) for groups in dialog.values() for g in groups)
    print(f"# {len(dialog)} NPCs with dialogue ({n_lines} lines) "
          f"-> {dialog_path}", file=sys.stderr)

    # ---- Locks: key↔lock id constants ------------------------------------
    # For each key/lock shape, walk every function in its class and harvest
    # the constants compared against the held item's K_QUALITY. The cmp
    # opcode helper (_cmp_field_const) records into state.lock_consts; here
    # we just aggregate and cross-link.
    #
    # In practice only key class 82 carries equality compares — chest classes
    # don't compare against quality at all (chest lock state lives in usecode
    # globals keyed by objid, not the chest's own quality byte). build_map.py
    # uses the byShape data for the inspector's "known to usecode" hint.
    KEY_SHAPES = {79, 82, 232}
    LOCK_SHAPES = {68, 69, 78, 114, 117, 135, 340, 341, 342, 618, 673}
    by_shape = {}
    for shape in sorted(KEY_SHAPES | LOCK_SHAPES):
        class_data = get_entry(data, entries, shape + 2)
        if not class_data or len(class_data) <= CODE_OFFSET:
            by_shape[shape] = {"eq": [], "rel": []}
            continue
        st = walk_class_readables(shape, class_name(name_table, shape),
                                   class_data, warn, call_resolver)
        eq = sorted({c for k, c in st.lock_consts if k == "eq" and 1 <= c <= 255})
        rel = sorted({c for k, c in st.lock_consts if k != "eq" and 1 <= c <= 255})
        by_shape[shape] = {"eq": eq, "rel": rel}

    pairs = {}
    for kshp in KEY_SHAPES:
        for c in by_shape.get(kshp, {}).get("eq", []):
            locks_with_c = sorted(s for s in LOCK_SHAPES
                                  if c in by_shape.get(s, {}).get("eq", []))
            if not locks_with_c:
                continue
            ent = pairs.setdefault(c, {"keys": [], "locks": []})
            if kshp not in ent["keys"]:
                ent["keys"].append(kshp)
            for ls in locks_with_c:
                if ls not in ent["locks"]:
                    ent["locks"].append(ls)

    locks_out = {
        "byShape": {str(k): v for k, v in by_shape.items()},
        "pairs": {str(k): v for k, v in sorted(pairs.items())},
    }
    locks_path = os.path.join(out_dir or ".", "locks.json")
    with open(locks_path, "w", encoding="utf-8") as f:
        json.dump(locks_out, f, indent=1)
        f.write("\n")
    n_eq = sum(len(v["eq"]) for v in by_shape.values())
    print(f"# locks: {n_eq} equality consts across "
          f"{sum(1 for v in by_shape.values() if v['eq'])} classes, "
          f"{len(pairs)} cross-class pair(s) -> {locks_path}",
          file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--game-dir", dest="game_dir", default=DEFAULT_GAME_DIR,
                    help=f"path to the Ultima VIII game directory "
                         f"(default: {DEFAULT_GAME_DIR})")
    ap.add_argument("-o", "--output", default=None,
                    help="merged descriptor JSON {shape: {default?,frames?,quality?}} "
                         "(default: json/barks.json)")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress walk warnings on stderr")
    main(**vars(ap.parse_args()))
