#!/usr/bin/env python3
"""
u8save.py  –  Read Ultima 8 savegame archives (e.g. SAVEGAME/U8SAVE.000).

A U8 savegame is a flat concatenation of member files. This is also how the
game ships its *pristine new-game state*: SAVEGAME/U8SAVE.000 is the seed the
engine unpacks into GAMEDAT/ when you start a new game. A freshly-extracted,
never-played install therefore has the seed archive but not the unpacked
members (notably NONFIXED.DAT, which the map build needs for every map).

Container format (little-endian):
    +0x00  char[24]  "Ultima 8 SaveGame File.\\0"  (ident, NUL-terminated)
    +0x18  u16       version (4)
    +0x1a  members, each: u32 namelen, char[namelen] name (NUL-terminated),
                          u32 datasize, byte[datasize] data

Members observed in U8SAVE.000: NONFIXED.DAT, NPCDATA.DAT, ITEMCACH.DAT,
AVATAR.DAT.
"""

import os
import struct

IDENT = b"Ultima 8 SaveGame File."


def unpack(data: bytes) -> dict[str, bytes]:
    """Parse a U8 savegame blob into {member name (uppercase): bytes}."""
    if data[:len(IDENT)] != IDENT:
        raise ValueError("not a U8 savegame archive (bad ident)")
    p = 0x18 + 2                                  # ident block + u16 version
    members: dict[str, bytes] = {}
    while p + 8 <= len(data):
        (nlen,) = struct.unpack_from("<I", data, p); p += 4
        name = data[p:p + nlen].split(b"\0", 1)[0].decode("ascii", "replace")
        p += nlen
        (size,) = struct.unpack_from("<I", data, p); p += 4
        members[name.upper()] = data[p:p + size]
        p += size
    return members


def find_archive(game_dir: str):
    """Return the path to the new-game seed archive under game_dir, or None.

    Prefers U8SAVE.000 (the canonical new-game seed); any U8SAVE.* will do.
    """
    candidates = []
    for dirpath, _, files in os.walk(game_dir):
        for f in files:
            if f.upper().startswith("U8SAVE."):
                candidates.append(os.path.join(dirpath, f))
    if not candidates:
        return None
    candidates.sort(key=lambda p: 0 if p.upper().endswith(".000") else 1)
    return candidates[0]


# Parsed archives are cached so we read the ~1 MB seed at most once per path.
_cache: dict[str, dict[str, bytes]] = {}


def extract_member(game_dir: str, name: str):
    """Return the bytes of `name` from the install's seed archive, or None.

    Used as a fallback when a member file isn't present loose on disk (the
    typical state of a never-played install).
    """
    archive = find_archive(game_dir)
    if archive is None:
        return None
    members = _cache.get(archive)
    if members is None:
        with open(archive, "rb") as fh:
            members = unpack(fh.read())
        _cache[archive] = members
    return members.get(name.upper())


if __name__ == "__main__":
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(
        description="List or extract members of a U8 savegame archive.")
    ap.add_argument("archive", help="path to U8SAVE.000 (or any U8SAVE.*)")
    ap.add_argument("-x", "--extract", metavar="DIR",
                    help="extract all members into DIR")
    args = ap.parse_args()

    members = unpack(Path(args.archive).read_bytes())
    for n, b in members.items():
        print(f"{n:16} {len(b):>9} bytes")
    if args.extract:
        out = Path(args.extract)
        out.mkdir(parents=True, exist_ok=True)
        for n, b in members.items():
            (out / n).write_bytes(b)
        print(f"extracted {len(members)} member(s) → {out}/")
