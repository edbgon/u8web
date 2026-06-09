#!/usr/bin/env python3
"""Derive a human-readable "what does this egg do" summary for every U8 egg
usecode class, written to json/egg_effects.json for the map viewer.

Shape-73 ("UnkEgg") objects each invoke usecode class `quality + 0x47F` when
they trigger (Pentagram Item::callUsecodeEvent / SF_UNKEGG). That class is the
egg's behaviour. Rather than show the bare class number in the inspector, we
disassemble each class (via u8_disasm), look at which intrinsics it calls,
which other classes it spawns, and any literal text it speaks, and condense
that into a short phrase plus a dialogue snippet.

The bytecode — and therefore the class ids and intrinsic numbers this keys on —
is identical across the localized E/F/G/J/S releases; only the embedded strings
differ. So one run against whichever USECODE.FLX is present produces a table
valid for any map render. Output is keyed by class id (string).

Run:  python parse_egg_effects.py [--game-dir ./ULTIMA8]
"""

import argparse
import json
import sys
from pathlib import Path

from u8_disasm import parse_eusecode
from parse_schedules import find_usecode, USECODE_LANGS

HERE = Path(__file__).resolve().parent
DEFAULT_GAME_DIR = HERE / "ULTIMA8"

# Egg classes are quality (0..255) + this base (SF_UNKEGG dispatch in Pentagram
# Item::callUsecodeEvent). We summarise the whole byte range; the viewer only
# looks up classes that actually occur as a shape-73 quality.
EGG_CLASS_BASE = 0x47F
EGG_CLASS_END = EGG_CLASS_BASE + 0x100

# Events (Item::callUsecodeEvent_*). Egg proximity triggers fire hatch (7);
# region cache-in fires enterFastArea (0xF). Which one a class implements tells
# the player whether the drawn trigger box is the live trigger.
EV_USE = 1
EV_HATCH = 7
EV_ENTER_FAST = 0xF

# Intrinsic numbers (Pentagram convert/u8/ConvertUsecodeU8.h _intrinsics[]).
I_ITEM_CREATE = 50
I_SET_MAPARRAY = 82
I_HURL = 68
I_BARK = 73
I_EXPLODE = 84
I_GUARDIAN_BARK = 109
I_MONSTEREGG_HATCH = 121
I_NPC_SETINCOMBAT = 131
I_NPC_SETTARGET = 133
I_NPC_SETALIGN = 135
I_NPC_TELEPORT = 158
I_NPC_CREATE = 173
I_NPC_AIRWALK = 175
I_CAM_SCROLLTO = 181
I_PLAYMUSIC = 187
I_CAM_MOVETO = 191
I_CAM_MOVEREL = 192
I_CAM_STARTQUAKE = 198
I_CAM_INVERT = 200
I_TELEPORT_TO_EGG = 204
I_SET_STASIS = 208
I_CREATE_SPRITE_A = 213
I_CREATE_SPRITE_B = 214
I_FADE_TO_BLACK = 222
I_FADE_FROM_BLACK = 223
I_PLAYSFX_A = 236
I_PLAYSFX_B = 237
I_PLAYSFX_C = 238
I_MUSIC_STOP_A = 248
I_MUSIC_STOP_B = 249
I_MUSIC_PLAY = 250

# Door-construction helper classes a door egg spawns/calls.
DOOR_CLASSES = {68, 1398}   # DOOR_NS, SLIDER


def _collect(uclass):
    """Walk every function of an egg class; return (events, intrinsics,
    spawned_class_ids, strings)."""
    events = set()
    intr = set()
    spawned = set()
    strings = []
    for fn in uclass.functions:
        if fn.event is not None:
            events.add(fn.event)
        for ins in fn.instrs:
            a = ins.args
            if ins.op == 0x0F:                      # calli
                intr.add(a.get("intrinsic"))
            elif ins.op in (0x57, 0x58):            # spawn / spawn_inline
                spawned.add(a.get("classid"))
            elif ins.op == 0x11:                    # intra/cross call
                spawned.add(a.get("classid"))
            elif "str" in a:                        # push-string literal
                s = a["str"].strip()
                if s and not _is_debug_string(s):
                    strings.append(s)
    return events, intr, spawned, strings


# Developer/assert strings that some eggs bark to the log (notably the generic
# monster-egg hatcher) — never player-facing, so keep them out of summaries.
_DEBUG_MARKERS = ("could not", "egg quality", "hatcher", "debug", "%")


def _is_debug_string(s):
    low = s.lower()
    return any(m in low for m in _DEBUG_MARKERS)


def _summarize(name, events, intr, spawned, strings):
    """Condense the collected signals into a short effect phrase + tags."""
    has = lambda *ids: any(i in intr for i in ids)
    tags = []

    teleport = has(I_NPC_TELEPORT, I_TELEPORT_TO_EGG)
    monster = has(I_MONSTEREGG_HATCH)
    spawn_npc = has(I_NPC_CREATE)
    hostile = has(I_NPC_SETINCOMBAT, I_NPC_SETTARGET)
    door = bool(spawned & DOOR_CLASSES)
    quake = has(I_CAM_STARTQUAKE)
    obj = has(I_ITEM_CREATE)
    airwalk = has(I_NPC_AIRWALK)
    music = has(I_PLAYMUSIC, I_MUSIC_PLAY)
    music_stop = has(I_MUSIC_STOP_A, I_MUSIC_STOP_B)
    sfx = has(I_PLAYSFX_A, I_PLAYSFX_B, I_PLAYSFX_C)
    fx = has(I_CREATE_SPRITE_A, I_CREATE_SPRITE_B, I_EXPLODE, I_HURL)
    cutscene = has(I_SET_STASIS, I_FADE_TO_BLACK, I_FADE_FROM_BLACK)
    camera = has(I_CAM_SCROLLTO, I_CAM_MOVETO, I_CAM_MOVEREL, I_CAM_INVERT)
    dialogue = has(I_BARK) or bool(strings)
    guardian = has(I_GUARDIAN_BARK)
    flag = has(I_SET_MAPARRAY)   # writes a global story/progress flag

    # Lead phrase — most salient effect first.
    if teleport:
        lead = "Teleports the Avatar"
        tags.append("teleport")
    elif monster:
        lead = "Hatches a monster"
        tags.append("monster")
    elif spawn_npc:
        lead = "Spawns hostile creatures" if hostile else "Spawns a creature"
        tags.append("spawn")
    elif door:
        lead = "Opens a door"
        tags.append("door")
    elif quake:
        lead = "Triggers an earthquake"
        tags.append("earthquake")
    elif airwalk:
        lead = "Grants levitation"
        tags.append("levitation")
    elif guardian and not dialogue:
        lead = "The Guardian taunts you"
        tags.append("guardian")
    elif dialogue or guardian:
        lead = "Plays a scripted scene"
        tags.append("scene")
    elif obj:
        lead = "Spawns an object"
        tags.append("object")
    elif music or music_stop:
        lead = "Changes the music"
        tags.append("music")
    elif sfx or fx:
        lead = "Plays a sound/visual effect"
        tags.append("effect")
    elif cutscene or camera:
        lead = "Scripted cutscene"
        tags.append("cutscene")
    elif flag:
        lead = "Sets a story flag"
        tags.append("flag")
    elif not intr and not spawned:
        lead = "No scripted effect"
    else:
        lead = "Scripted trigger"

    # Monster hatchers bark only debug text — never qualify them as dialogue.
    lead_tag = tags[0] if tags else None
    allow_dialogue = lead_tag != "monster"

    # Secondary qualifiers (skip ones already implied by the lead).
    extra = []
    if cutscene and "teleport" not in tags:
        extra.append("cutscene")
    if quake and lead_tag != "earthquake":
        extra.append("earthquake")
    if allow_dialogue and (dialogue or guardian) and lead_tag not in ("scene", "guardian"):
        extra.append("dialogue")
    if music and lead_tag != "music":
        extra.append("music")
    if cutscene and "teleport" in tags:
        extra.append("cutscene")
    summary = lead + (" (" + ", ".join(extra) + ")" if extra else "")

    # Which event actually fires the behaviour — tells whether the trigger box
    # (proximity/hatch) is the live trigger or it fires on area load.
    if EV_HATCH in events:
        trigger = "proximity"
    elif EV_ENTER_FAST in events:
        trigger = "area load"
    elif EV_USE in events:
        trigger = "on use"
    else:
        trigger = "other"

    out = {"name": name, "summary": summary, "trigger": trigger}
    if tags:
        out["tags"] = tags
    # The first literal line spoken — reveals the actual scene. Trim long ones.
    line = next((s for s in strings if len(s) > 1), None) if allow_dialogue else None
    if line:
        out["line"] = (line[:160] + "…") if len(line) > 161 else line
    return out


def build_egg_effects(usecode_path):
    table = {}
    for uclass in parse_eusecode(usecode_path):
        if not (EGG_CLASS_BASE <= uclass.class_id < EGG_CLASS_END):
            continue
        events, intr, spawned, strings = _collect(uclass)
        if not uclass.name and not intr and not spawned:
            continue  # empty/unused egg slot
        table[uclass.class_id] = _summarize(
            uclass.name, events, intr, spawned, strings)
    return table


def main(game_dir=DEFAULT_GAME_DIR):
    usecode, lang = find_usecode(game_dir)
    if usecode is None:
        sys.exit(f"no [EFGJS]USECODE.FLX found under {game_dir}")
    print(f"# {USECODE_LANGS.get(lang, lang)} usecode: {usecode}", file=sys.stderr)

    table = build_egg_effects(usecode)
    out_path = HERE / "json" / "egg_effects.json"
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in sorted(table.items())},
                  f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print(f"# {len(table)} egg classes -> {out_path}", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Summarize U8 egg usecode effects for the map viewer.")
    ap.add_argument("--game-dir", default=DEFAULT_GAME_DIR, type=Path,
                    help=f"Ultima VIII game directory (default: {DEFAULT_GAME_DIR})")
    main(**vars(ap.parse_args()))
