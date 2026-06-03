"""
Extract NPC schedule waypoints from disassembled Ultima 8 usecode.

Each NPC class has an Event 8 (`schedule`) handler that the engine's
SchedulerProcess pings once per game hour (pentagram/world/actors/Schedule
rProcess.cpp). The handler reads the time of day via `FREE::28F9` (= hour/4,
i.e. one of 6 time blocks), branches on quest globals, sets a destination
in local stack slots, then `spawn METHOD::133F` / `METHOD::143A` to start
pathfinding. The stack layout is fixed: BP-07 = activity (uword), BP-05 =
dest x (uword), BP-03 = dest y (uword), BP-01 = dest z (ubyte), packed
into the 5-byte `push huge FB 05` operand.

This script walks every class via `u8_disasm.parse_eusecode` (a pure-
Python disassembler that reads the localized USECODE.FLX directly — no
pentagram dependency; see find_usecode for the E/F/G/J/S language flavours),
scans every function for `spawn METHOD::133F` /
`spawn METHOD::143A`, and records whatever (x, y, z, activity) was most-
recently written into those locals. The result is the set of destinations
the schedule can ever push an NPC toward — coarse (we don't try to recover
which time block / quest state owns which waypoint), but spatially
faithful, which is exactly what the map overlay needs.

Output:
  json/schedules.json   { "<class_id>": [{"x":..,"y":..,"z":..,"act":..}, ...] }

class_id == shape id for NPC actor classes (KEY is class 82 = shape 82,
DEVON is class 0xFF = shape 255, etc.).
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from u8_disasm import parse_eusecode, jmp_target
from parse_usecode import USECODE_ENCODINGS

HERE = Path(__file__).resolve().parent
DEFAULT_GAME_DIR = HERE / "ULTIMA8"

# U8 ships one localized usecode FLX, named by a language-letter prefix:
# E)nglish, F)rench, G)erman, J)apanese, S)panish. The bytecode (and the
# class/offset constants this script keys on) is identical across them — only
# the embedded strings differ — so the schedule scan works on any of them.
USECODE_LANGS = {
    "E": "English", "F": "French", "G": "German",
    "J": "Japanese", "S": "Spanish",
}


def find_usecode(game_dir):
    """Locate the localized USECODE FLX under a U8 install.

    Returns (path, language_letter) for the first of E/F/G/J/S USECODE.FLX
    found, preferring English when several are present; (None, None) if none.
    The letter keys both USECODE_LANGS (display) and USECODE_ENCODINGS (the
    text codec the NPC-name scan decodes with).
    """
    found = {}
    for p in Path(game_dir).rglob("*"):
        n = p.name.upper()
        if (len(n) == len("EUSECODE.FLX") and n.endswith("USECODE.FLX")
                and n[0] in USECODE_LANGS and p.is_file()):
            found.setdefault(n[0], p)
    for letter in ("E", "F", "G", "J", "S"):
        if letter in found:
            return found[letter], letter
    return None, None


def resolve_english_or_spanish(classes):
    """Disambiguate the two builds that both ship as EUSECODE.FLX.

    The 'E' filename stands for English in one release and Español in the
    other, so find_usecode can't tell them apart by name. GUARD1's generic
    look-bark is a guaranteed-localized word — "guardsman" in English,
    "guardia" in Spanish — so it settles which build this is. Returns "E" or
    "S"; defaults to "E" if the marker can't be read.
    """
    guard = next((c for c in classes
                  if c.class_id == NPC_CLASS_BASE + GUARD1_NPC), None)
    if guard is not None:
        ev0 = next((f for f in guard.functions if f.event == LOOK_EVENT), None)
        if ev0 is not None:
            barks = [i.args["str"] for i in ev0.instrs
                     if i.mnemonic == "push_string"]
            if barks and not barks[-1].lower().startswith("guardsman"):
                return "S"
    return "E"


# The two FREE classes the schedule scan reaches into. Class *ids* are
# engine-level and stable across language builds; only the method *offsets*
# inside them move when the usecode is recompiled per language, so they are
# computed from each build's own bytecode (compute_schedule_offsets) rather
# than tabulated — see that function for the fingerprints.
GO_TO_CLASS = 0x057C   # holds the pathfind/setActivity spawn targets
FREE_CLASS  = 0x0581   # holds the time-of-day helper (see below)

# Per Item::callUsecodeEvent (pentagram/world/Item.cpp:1031-1041), a permanent
# NPC's usecode class is `objid + 1024`, where objid is the actor's slot number
# 1..255 (the same `_npc` field parse_npcs records).
NPC_CLASS_BASE = 1024
NPC_CLASS_END  = NPC_CLASS_BASE + 256
GUARD1_NPC = 4   # generic palace guard; its look-bark localizes English/Spanish

# `spawn GO_TO_CLASS:<off>` with a 7-byte argument block is the pathfind/
# setActivity "send NPC to dest" spawn (the 7 bytes pack x, y, z + activity).
# Among GO_TO_CLASS spawns this arg layout is unique — every other target
# pushes 0, 4, or 6 bytes — and exactly two offsets use it: the standard spawn
# and the variant that returns a success flag (for NPCs that chain walks). The
# scan only tests membership, so the two are kept as an unordered set.
GO_TO_SPAWN_SIG = (7, 2)   # (argbytes, thissize) of the dest-tuple spawn
SPAWN_OP = 0x57
CALL_OP  = 0x11
PUSH_BYTE_OP = 0x0A
CMP_OP   = 0x24

# `timeofday` is the FREE_CLASS method returning `Npc::schedule()`'s "current
# time block" — TimeInGameHours() / 4, i.e. one of six 4-hour blocks:
#   0 Bloodwatch  00:00-04:00     3 Threemoons  12:00-16:00
#   1 Firstebb    04:00-08:00     4 Lastebb     16:00-20:00
#   2 Daytide     08:00-12:00     5 Eventide    20:00-00:00
# Schedule handlers gate each branch's waypoints with `timeofday == N` checks
# (often joined by `or` for multi-block branches), so attributing each spawn to
# its enclosing block set lets the viewer scrub through time. That comparison
# shape — `call FREE_CLASS::tod ; push_retval ; push byte N(0..5) ; cmp` — is
# how the method is recognised: it is the only FREE_CLASS call whose result is
# tested against a time-block constant, so no other call is mistaken for it.

# Active offsets — populated by compute_schedule_offsets() in main() once the
# build is parsed. The scan functions read these module globals at call time.
GO_TO_OFFSETS = frozenset()
TIMEOFDAY_OFF = None


def compute_schedule_offsets(classes):
    """Derive this build's go_to spawn targets and timeofday call offset from
    its own bytecode, so no per-language offset table is needed (the offsets
    shift on every localized recompile; their *roles* do not).

    Scans NPC schedule code (classes NPC_CLASS_BASE..NPC_CLASS_END) for:
      - GO_TO_CLASS spawns carrying the GO_TO_SPAWN_SIG dest-tuple arg block
        → the go_to set;
      - FREE_CLASS calls whose return value is compared against a 0..5 time
        block → timeofday (taken by majority vote; a stray call may coincide
        with the pattern once, but the real helper dominates).

    Returns (go_to: frozenset[int], timeofday: int | None).
    """
    go_to = set()
    tod_votes = Counter()
    for c in classes:
        if not (NPC_CLASS_BASE <= c.class_id < NPC_CLASS_END):
            continue
        for fn in c.functions:
            ins = fn.instrs
            for k, i in enumerate(ins):
                if (i.op == SPAWN_OP
                        and i.args.get("classid") == GO_TO_CLASS
                        and (i.args["argbytes"], i.args["thissize"])
                            == GO_TO_SPAWN_SIG):
                    go_to.add(i.args["offset"])
                elif i.op == CALL_OP and i.args.get("classid") == FREE_CLASS:
                    window = ins[k + 1:k + 5]
                    if (any(w.op == CMP_OP for w in window)
                            and any(w.op == PUSH_BYTE_OP
                                    and 0 <= w.args.get("b", -1) <= 5
                                    for w in window)):
                        tod_votes[i.args["offset"]] += 1
    timeofday = tod_votes.most_common(1)[0][0] if tod_votes else None
    return frozenset(go_to), timeofday

# Intrinsic ids we recognise for the map-filter pass.
INTR_NPC_GET_MAP = 0x9D    # Npc::getMap() — per ConvertUsecodeU8.h


def annotate_map_filters(instrs):
    """For each instruction, return the active `getMap() == N` filter in
    effect at that point, or None.

    The schedule bytecode for NPCs that route across multiple maps wraps
    each map's branch in `getMap() != N -> jne <skip>`. The branch body
    (with its waypoints) is only executed when the actor is on map N, so
    its dests are valid only in that map's world-coordinate frame.

    Recovers (after-jne-idx .. target-offset) ranges tagged with N by
    scanning for the `calli Npc::getMap() / push byte N / cmp / jne
    TARGET` window, then resolves per-instruction filters via a stack so
    nested map gates are handled. Non-map cmp/jne pairs are ignored.
    """
    ranges = []     # (start_idx_inclusive, end_offset_exclusive, map_n)
    n = len(instrs)
    for i, ins in enumerate(instrs):
        if not (ins.op == 0x0F and ins.args.get("intrinsic") == INTR_NPC_GET_MAP):
            continue
        # Find the push_byte immediately following (within 8 instrs).
        push_n = None
        cmp_idx = None
        for j in range(i + 1, min(i + 8, n)):
            nj = instrs[j]
            if nj.op == 0x0A and push_n is None:
                push_n = nj.args["b"]
            if nj.op == 0x24:    # cmp
                cmp_idx = j
                break
        if push_n is None or cmp_idx is None:
            continue
        # The jne is in the next one or two slots.
        for k in range(cmp_idx + 1, min(cmp_idx + 3, n)):
            nk = instrs[k]
            if nk.op == 0x51:    # jne
                # nextoffset = offset of the instruction immediately after this jne.
                next_off = instrs[k + 1].offset if k + 1 < n else instrs[k].offset + 3
                target = jmp_target(nk, next_off)
                ranges.append((k + 1, target, push_n))
                break

    per_instr = [None] * n
    stack = []   # (end_offset_exclusive, map_n) — innermost on top
    ri_iter = iter(ranges)
    pending_ranges = sorted(ranges, key=lambda r: r[0])
    pi = 0
    for idx, ins in enumerate(instrs):
        cur_off = ins.offset
        while stack and cur_off >= stack[-1][0]:
            stack.pop()
        while pi < len(pending_ranges) and pending_ranges[pi][0] == idx:
            stack.append((pending_ranges[pi][1], pending_ranges[pi][2]))
            pi += 1
        per_instr[idx] = stack[-1][1] if stack else None
    return per_instr


def annotate_time_filters(instrs):
    """For each instruction, return the active time-block set (frozenset
    of block ids 0..5) or None when the instruction is outside any guard.

    Schedule branches gate on time-of-day with one of two shapes:

      single block:
        call FREE::28F9 ; push retval ; push byte N ; cmp ; jne <skip>

      union of blocks:
        call FREE::28F9 ; push retval ; push byte N0 ; cmp
        ( call FREE::28F9 ; push retval ; push byte Ni ; cmp ; or )+
        jne <skip>

    The body between the jne and its target runs when the time block is
    in {N, N0, N1, ...}. Sequential guards in the same function fall
    through (next-block branches sit back-to-back), so we model a stack
    of active ranges and intersect them in case any do nest.
    """
    n = len(instrs)
    ranges = []   # (start_idx, end_offset_exclusive, frozenset(blocks))
    def _is_28f9(ins):
        return (ins.op == 0x11
                and ins.args.get("classid") == FREE_CLASS
                and ins.args.get("offset")  == TIMEOFDAY_OFF)

    i = 0
    while i < n:
        if not _is_28f9(instrs[i]):
            i += 1; continue
        # Consume the block-union expression: a run of `call FREE::28F9 ;
        # push retval ; push byte N ; cmp` triples combined by `or` opcodes,
        # terminated by the `jne`. The compiler emits the `or`s in either
        # layout — interleaved (`cmp cmp or cmp or jne`, as MORDEA's daytime
        # guard does) or trailing (`cmp cmp cmp or or jne`) — so accept an
        # `or` anywhere between triples, not only after the last cmp. (The
        # earlier "trailing only" assumption silently dropped every block but
        # the last whenever the `or`s were interleaved.)
        blocks = set()
        j = i
        while j < n:
            if j + 3 < n and _is_28f9(instrs[j]) \
                         and instrs[j+1].op == 0x5E \
                         and instrs[j+2].op == 0x0A \
                         and instrs[j+3].op == 0x24:
                blocks.add(instrs[j+2].args["b"])
                j += 4
            elif instrs[j].op == 0x34:    # or — fold another block into the union
                j += 1
            else:
                break
        # The terminator is `jne <skip>`: body runs when the time block
        # is in `blocks`.
        if j < n and instrs[j].op == 0x51 and blocks:
            next_off = instrs[j+1].offset if j + 1 < n else instrs[j].offset + 3
            target   = jmp_target(instrs[j], next_off)
            ranges.append((j + 1, target, frozenset(blocks)))
            i = j + 1
        else:
            i += 1

    per_instr = [None] * n
    stack = []                           # [(end_offset_exclusive, frozenset)]
    ranges.sort(key=lambda r: r[0])
    ri = 0
    for idx in range(n):
        cur_off = instrs[idx].offset
        while stack and cur_off >= stack[-1][0]:
            stack.pop()
        while ri < len(ranges) and ranges[ri][0] == idx:
            stack.append((ranges[ri][1], ranges[ri][2]))
            ri += 1
        if stack:
            blocks = stack[0][1]
            for _, b in stack[1:]:
                blocks = blocks & b
            per_instr[idx] = blocks
    return per_instr


def extract_waypoints(instrs):
    """Linear scan of a single function's bytecode.

    Returns a list of unique waypoint dicts. Approximate: writes to
    BP-05/-03/-01/-07 from any branch are folded together, so a schedule
    that walks A→B→C will surface all three waypoints regardless of which
    quest state or time block gated them — that's the desired output for
    a "places this NPC visits" overlay. `m` is the raw FIXED-record
    mapnum of the surrounding `getMap() == N` guard (or absent when the
    branch is unguarded — likely the NPC's home map).

    Cross-NPC routing: some classes (e.g. MORDEA) drive *other* NPCs by
    calling pathfind with the target's NPC id pushed inline:

        push pid
        push byte  <target_npc_id>
        push byte  <activity>
        push huge  FB 05            ; (x,y,z) from BP-5..-1
        push addr  [SP+...]         ; pointer to that tuple  (opcode 0x6F)
        spawn      057C:133F/143A

    versus the self-targeting form which ends with `push dword [BP+06h]`
    (the running class's own this-ptr, opcode 0x40 with bp==6). When we
    see the cross form, the waypoint belongs to the pushed NPC id, not
    the current class. This is how SALKIND (whose own Event 8 is a stub)
    ends up with East/Central Tenebrae destinations — MORDEA's schedule
    pushes them on his behalf. The target id rides out on the returned
    dict as `_target`.
    """
    map_filters  = annotate_map_filters(instrs)
    time_filters = annotate_time_filters(instrs)
    x = y = z = act = None
    pending = None          # value of the most recent push not yet consumed
    recent_bytes = []       # last two `push byte` literals (for cross-NPC)
    last_thisptr_kind = None   # 'self' (0x40 BP+06) / 'cross' (0x6F)  / None
    inline_act = None       # activity byte pushed inline right before the
                            # coord tuple in the self form (see spawn handler)
    waypoints = []
    # `seen` maps (x,y,z,act,m,target) -> index in `waypoints`. A coord
    # repeated under several time-of-day branches is merged into one
    # waypoint with its `t` set unioned, rather than emitted twice.
    seen = {}

    def _attach_time(wp, cur_t):
        """Union an additional time-block reach into `wp`.

        An unguarded reach (cur_t is None) contributes nothing rather than
        erasing the blocks a guarded reach established. In practice an
        unguarded reach to a coord that's *also* reached under a time guard
        is almost never a genuine "any hour" visit — it's an `else`/
        fall-through branch the parser can't time-attribute, or the redundant
        re-emission from a compute-the-dest-then-`spawn`-once schedule (DEVON's
        single end-spawn re-pushes the last branch's coords with no guard).
        The old "unguarded reach wins → drop the tag" rule turned both of
        those into spurious all-day waypoints, which the viewer can't schedule
        (scheduleWaypointAt needs a `t`). A coord stays untagged only when
        *every* reach to it is unguarded (e.g. a purely quest-gated dest)."""
        if cur_t is None:
            return
        if "t" not in wp:
            wp["t"] = sorted(cur_t)
        else:
            wp["t"] = sorted(set(wp["t"]) | set(cur_t))

    for idx, ins in enumerate(instrs):
        op = ins.op
        cur_map = map_filters[idx]
        cur_t   = time_filters[idx]

        # ── push family ─────────────────────────────────────────────
        if op == 0x0A:                              # push_byte
            pending = ins.args["b"]
            recent_bytes.append(pending)
            if len(recent_bytes) > 2:
                recent_bytes.pop(0)
            continue
        if op == 0x0B:                              # push_word
            pending = ins.args["w"]
            continue
        if op == 0x0C:                              # push_dword
            pending = ins.args["d"]
            continue
        if op == 0x40:                              # push_dword_bp
            if ins.args["bp"] == 0x06:    # [BP+06h] = self this-ptr
                last_thisptr_kind = "self"
            pending = None
            continue
        if op == 0x6F:                              # push_addr_sp
            last_thisptr_kind = "cross"
            pending = None
            continue
        if op == 0x45:                              # push huge (the FB 05 coord tuple)
            # The self form pushes the activity inline immediately before this
            # tuple push: `push byte <act>; push huge FB 05; push [BP+06h]; spawn`
            # (vs the standard form, which routes the activity through BP-07 and
            # re-pushes it via 0x3F — so the byte right before the tuple is NOT
            # an activity there). Capture it only when the previous instruction
            # is literally a push_byte, so a coord byte (e.g. z) is never
            # mistaken for the activity.
            prev = instrs[idx - 1] if idx > 0 else None
            inline_act = prev.args["b"] if (prev is not None and prev.op == 0x0A) else None
            pending = None
            continue
        # Any other "push <something not-a-literal>" — clears pending.
        if op in (0x0D, 0x0E, 0x3E, 0x3F, 0x41, 0x42, 0x43, 0x44,
                  0x4B, 0x4C, 0x4E, 0x59, 0x5D, 0x5E, 0x5F, 0x69, 0x6D):
            pending = None
            continue

        # ── pop into a local var ────────────────────────────────────
        if op in (0x00, 0x01, 0x02):
            bp_raw = ins.args["bp"]
            neg = 0x100 - bp_raw if bp_raw >= 0x80 else None
            if neg is not None and pending is not None:
                if   neg == 5: x   = pending
                elif neg == 3: y   = pending
                elif neg == 1: z   = pending
                elif neg == 7:
                    act = pending
                    # Activity is the last field written in the standard
                    # branch prelude (x, y, z, act) before the spawn at
                    # the join point. Emit here so branches that share a
                    # join don't all collapse to the last branch's coords.
                    if x is not None and y is not None and z is not None:
                        tup = (x, y, z, act, cur_map, None)
                        if tup in seen:
                            _attach_time(waypoints[seen[tup]], cur_t)
                        else:
                            wp = {"x": x, "y": y, "z": z, "act": act}
                            if cur_map is not None: wp["m"] = cur_map
                            if cur_t   is not None: wp["t"] = sorted(cur_t)
                            seen[tup] = len(waypoints)
                            waypoints.append(wp)
            pending = None
            continue

        # ── pathfind spawn ─────────────────────────────────────────
        if op == 0x57:
            sa = ins.args
            if (sa["classid"] == GO_TO_CLASS
                and sa["offset"] in GO_TO_OFFSETS):
                target = None
                spawn_act = act
                if last_thisptr_kind == "cross" and len(recent_bytes) >= 2:
                    # recent_bytes = [npc_id, activity] from the inline
                    # pushes right before `push huge FB 05`. Override
                    # both — activity in this form lives on the stack,
                    # not in BP-07.
                    target = recent_bytes[-2]
                    spawn_act = recent_bytes[-1]
                elif act is None and inline_act is not None:
                    # Self form that never routed the activity through BP-07
                    # (e.g. BEREN): the activity sits inline before the tuple.
                    # Without this, the waypoint loses its act → its dest-map
                    # signal, and build_map dumps it on the home map (so the
                    # waypoint renders off the map it actually belongs to).
                    spawn_act = inline_act
                if x is not None and y is not None and z is not None:
                    tup = (x, y, z,
                           spawn_act if spawn_act is not None else 0,
                           cur_map, target)
                    if tup in seen:
                        _attach_time(waypoints[seen[tup]], cur_t)
                    else:
                        wp = {"x": x, "y": y, "z": z,
                              "act": spawn_act if spawn_act is not None else 0}
                        if cur_map is not None: wp["m"] = cur_map
                        if cur_t   is not None: wp["t"] = sorted(cur_t)
                        if target  is not None: wp["_target"] = target
                        seen[tup] = len(waypoints)
                        waypoints.append(wp)
                # Don't clear x/y/z/act — sequential walks in the same
                # branch reuse the upstream values for the next 133F.
                recent_bytes = []
                last_thisptr_kind = None
                inline_act = None
            pending = None
            continue

        # ── everything else invalidates a pending push (matches the
        # old line-based parser's fall-through clear) ────────────────
        pending = None

    return waypoints


# ── Actor::I_teleport (Pentagram intrinsic 0x9e) ──────────────────────
# Signature: I_teleport(actor, x, y, z, map). The scene/setup scripts use
# it to move NPCs parked on the intro map to their real homes. Two emitted
# operand layouts precede the `calli 0x9e`:
#   self  : push map; push z; push y; push x; push dword [BP+06h]   (actor = this class)
#   other : push id;  push map; push z; push y; push x; push addr SP (actor = id)
# Only literal (push_byte/push_word) ids and maps are recovered; dynamic
# transitions (the boat/MAPTELE handler) carry the map in a variable and
# are deliberately left out.
TELEPORT_INTRINSIC = 0x9E


def extract_teleports(instrs, cls_npc):
    """Yield (npc_id, mapnum) for every literal-argument Actor::I_teleport."""
    out = []
    for i, ins in enumerate(instrs):
        if not (ins.op == 0x0F and ins.args.get("intrinsic") == TELEPORT_INTRINSIC):
            continue
        # The push immediately before the call is the actor pointer.
        j = i - 1
        if j < 0:
            continue
        pop = instrs[j].op
        if pop == 0x6F:                                   # push_addr_sp -> "other"
            form = "other"
        elif pop == 0x40 and instrs[j].args.get("bp") == 0x06:   # push [BP+06h] -> self
            form = "self"
        else:
            continue
        # Collect the literal scalars pushed before that pointer.
        scal = []
        k = j - 1
        while k >= 0 and len(scal) < 5:
            op = instrs[k].op
            if op == 0x0A:
                scal.append(instrs[k].args["b"])
            elif op == 0x0B:
                scal.append(instrs[k].args["w"])
            else:
                break
            k -= 1
        scal.reverse()
        if form == "self":
            if scal and cls_npc is not None:              # [map, z, y, x]
                out.append((cls_npc, scal[0]))
        else:
            if len(scal) >= 2:                            # [id, map, z, y, x]
                out.append((scal[0], scal[1]))
    return out


# ── Localized NPC display names from the look handler (event 0) ───────────
# Every named NPC class has a look handler (event 0) that barks its identity
# via I_bark. The bark is a chain of flag-guarded variants (corpse / name-
# known / trade-known / generic); the compiler emits the *corpse* branch
# first, so the bare living name sits at a later, NPC-specific index. Because
# every localized build is recompiled from the same source, that index is
# identical across E/F/G/J/S — only the string bytes differ — so one table
# covers all languages (the bytecode *offsets* shift per build, but bark order
# does not; cf. compute_schedule_offsets, which fingerprints those offsets).
#
# Derived from the English build by matching each look-bark against the FLX
# class name, then verified to yield correct German + Japanese names at the
# same index. Keys are NPC ids (class_id - NPC_CLASS_BASE). KORICK has no
# bare-name bark, so index 1 ("Korick the Smith") stands in for him.
#
# Classes NOT listed here (guards, the nameless evil Sorcerer) have no proper
# name; their look handler only barks a generic descriptor, and the *last*
# bark is the living, fully-generic one ("guardsman", "Sorcerer"), so that is
# used as the label. The Avatar (id 1) is the sole exception: its event 0 is
# the debug menu, not a look handler, so it gets a fixed "Avatar".
LOOK_EVENT = 0
AVATAR_NPC = 1
NPC_LOOK_NAME_IDX = {
    2: 0, 3: 3, 5: 2, 7: 1, 8: 0, 9: 1, 10: 2, 12: 2, 13: 2, 14: 0,
    15: 0, 16: 3, 18: 2, 19: 1, 21: 0, 22: 2, 23: 2, 24: 3, 25: 2, 26: 2,
    27: 3, 28: 3, 29: 3, 31: 3, 32: 3, 33: 0, 52: 0, 53: 0, 54: 0, 55: 0,
    56: 0, 57: 3, 60: 0,
}


def extract_npc_names(classes, lang):
    """Map NPC id -> localized display name via each class's look handler.

    Named NPCs use their NPC_LOOK_NAME_IDX bark; every other class with a look
    handler falls back to its last (generic living) bark. The disassembler
    hands strings back as raw bytes preserved in latin-1; re-decode through the
    per-language codec so CP437 accents (F/G/S) and Japanese (cp932) read
    correctly.
    """
    enc = USECODE_ENCODINGS[lang]
    names = {AVATAR_NPC: "Avatar"}
    for cls in classes:
        nid = cls.class_id - NPC_CLASS_BASE
        if not (1 <= nid < 256) or nid == AVATAR_NPC:
            continue
        ev0 = next((f for f in cls.functions if f.event == LOOK_EVENT), None)
        if ev0 is None:
            continue
        barks = [i.args["str"] for i in ev0.instrs
                 if i.mnemonic == "push_string"]
        if not barks:
            continue
        idx = NPC_LOOK_NAME_IDX.get(nid, len(barks) - 1)
        name = barks[idx].encode("latin-1").decode(enc, "replace").strip()
        if name:
            names[nid] = name
    return names


def main(game_dir=DEFAULT_GAME_DIR):
    # NPC usecode classes are rekeyed by npc number (NPC_CLASS_BASE, defined
    # at module scope) so the viewer can look up SCHEDULES[o.npc] directly.
    usecode, lang = find_usecode(game_dir)
    if usecode is None:
        sys.exit(f"no [EFGJS]USECODE.FLX found under {game_dir}")

    classes = list(parse_eusecode(usecode))
    if lang == "E":
        lang = resolve_english_or_spanish(classes)
    print(f"# {USECODE_LANGS[lang]} usecode: {usecode}", file=sys.stderr)

    # Locate the per-build schedule offsets by fingerprinting the bytecode
    # (no language-specific table — see compute_schedule_offsets).
    global GO_TO_OFFSETS, TIMEOFDAY_OFF
    GO_TO_OFFSETS, TIMEOFDAY_OFF = compute_schedule_offsets(classes)
    if not GO_TO_OFFSETS or TIMEOFDAY_OFF is None:
        sys.exit(f"could not fingerprint schedule offsets in {usecode.name} "
                 f"(go_to={sorted(GO_TO_OFFSETS)}, timeofday={TIMEOFDAY_OFF}). "
                 f"The GO_TO/FREE class ids ({GO_TO_CLASS:#06x}/{FREE_CLASS:#06x}) "
                 f"may differ in this build.")
    print("# schedule offsets: go_to={{{}}} timeofday={:#x}".format(
        ", ".join(f"{o:#x}" for o in sorted(GO_TO_OFFSETS)), TIMEOFDAY_OFF),
        file=sys.stderr)

    # First pass: map class_id → class name so cross-NPC waypoints can
    # attach the right label when the target NPC's own class produces no
    # waypoints of its own (e.g. SALKIND's Event 8 is a stub).
    class_names = {c.class_id - NPC_CLASS_BASE: c.name
                   for c in classes
                   if NPC_CLASS_BASE <= c.class_id < NPC_CLASS_END}

    # Aggregate waypoints across every function in the NPC's class, not
    # just Event 8. Many NPCs (SALKIND, VIVIDOS, NPCs with stub schedules)
    # wire their real route through enterFastArea (Event F) or helper
    # functions that the event handlers call into; restricting to Event 8
    # misses them.
    schedules = {}
    # owner -> dict mapping (x,y,z,act,m) → index into entry["wps"]. When
    # the same waypoint surfaces from multiple functions (or both within
    # MORDEA and the target NPC's own class), we merge their `t` sets
    # instead of dropping the duplicate.
    seen_tuples = {}

    for cls in classes:
        if not (NPC_CLASS_BASE <= cls.class_id < NPC_CLASS_END):
            continue
        cls_npc = cls.class_id - NPC_CLASS_BASE
        for fn in cls.functions:
            for wp in extract_waypoints(fn.instrs):
                target_npc = wp.pop("_target", None)
                owner      = target_npc if target_npc is not None else cls_npc
                owner_name = class_names.get(owner, cls.name)
                entry = schedules.setdefault(owner,
                    {"name": owner_name, "wps": []})
                tup = (wp["x"], wp["y"], wp["z"], wp.get("act", 0), wp.get("m"))
                seen = seen_tuples.setdefault(owner, {})
                if tup in seen:
                    existing = entry["wps"][seen[tup]]
                    # Union time blocks across the independent functions /
                    # classes that reach this coord. A source with no `t`
                    # (e.g. MORDEA drives ARAMINA at t=[0], while ARAMINA's
                    # own Event 8 reaches the same spot from a quest-gated
                    # branch the parser can't time-attribute) contributes
                    # nothing rather than ERASING an authoritative timed tag.
                    # The old "unguarded reach wins" rule turned exactly that
                    # case into a spurious all-day waypoint, which the viewer
                    # can't schedule (scheduleWaypointAt needs a `t`). The
                    # coord stays untagged only when NO source ever tagged it.
                    if "t" in wp:
                        existing["t"] = sorted(set(existing.get("t", [])) | set(wp["t"]))
                    continue
                seen[tup] = len(entry["wps"])
                entry["wps"].append(wp)

    # ── Exact NPC→map table from Actor::I_teleport → json/npc_maps.json ──
    npc_map_votes = {}
    for cls in classes:
        cls_npc = (cls.class_id - NPC_CLASS_BASE
                   if NPC_CLASS_BASE <= cls.class_id < NPC_CLASS_END else None)
        for fn in cls.functions:
            for npc_id, mapnum in extract_teleports(fn.instrs, cls_npc):
                if 1 <= npc_id < 256 and 0 < mapnum < 256:
                    npc_map_votes.setdefault(npc_id, Counter())[mapnum] += 1
    npc_maps = {}
    for npc_id, votes in npc_map_votes.items():
        non_intro = [m for m in votes if m != 3]
        pick = (max(non_intro, key=lambda m: votes[m]) if non_intro
                else max(votes, key=lambda m: votes[m]))
        npc_maps[npc_id] = pick
    maps_path = HERE / "json" / "npc_maps.json"
    with open(maps_path, "w") as f:
        json.dump({str(k): v for k, v in sorted(npc_maps.items())}, f,
                  separators=(",", ":"))
        f.write("\n")
    print(f"# {len(npc_maps)} NPC home maps -> {maps_path}", file=sys.stderr)

    # ── NPC display names from look handlers → json/npc.json ──
    # Fully generated from usecode (no hand-curated source); reflects whichever
    # language was parsed.
    npc_names = extract_npc_names(classes, lang)
    npc_path = HERE / "json" / "npc.json"
    ordered = {str(k): npc_names[k] for k in sorted(npc_names)}
    with open(npc_path, "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=4)
        f.write("\n")
    print(f"# {len(npc_names)} NPC names -> {npc_path}", file=sys.stderr)

    out_path = HERE / "json" / "schedules.json"
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({str(k): v for k, v in sorted(schedules.items())},
                  f, separators=(",", ":"))
        f.write("\n")

    total_wps = sum(len(v["wps"]) for v in schedules.values())
    print(f"# {len(schedules)} NPC classes, {total_wps} waypoints -> {out_path}",
          file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Extract NPC schedule waypoints from U8 usecode.")
    ap.add_argument("--game-dir", default=DEFAULT_GAME_DIR, type=Path,
                    help=f"Ultima VIII game directory (default: {DEFAULT_GAME_DIR})")
    main(**vars(ap.parse_args()))
