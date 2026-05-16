#!/usr/bin/env python3
"""
Write a labeled sprite sheet for every monster that a shape-500 monster egg
can hatch. Each sheet lays out all of that shape's atlas frames in a grid
with the frame index drawn above each sprite — handy for picking the
front-facing pose frame used by build_map.apply_monster_eggs.

Output: monsters/monster_<shape>.png
"""
import json
import os
import struct

from PIL import Image, ImageDraw, ImageFont

import build_map as bm

OUT = "monsters"
COLS = 16
PAD = 4
LABEL_H = 12


def monster_shapes(game_dir=bm.DEFAULT_GAME_DIR):
    """Every distinct monster shape referenced by a shape-500 egg's quality."""
    shapes = set()
    for name in ("FIXED.DAT", "NONFIXED.DAT"):
        data = bm.load(bm.find_game_file(game_dir, name))
        recs = (bm.read_fixed if name == "FIXED.DAT" else bm.read_nonfixed)(
            bm.find_game_file(game_dir, name))[1]
        for _idx, off, ln in recs:
            for i in range(ln // bm.OBJ_SIZE):
                rec = struct.unpack_from(bm.OBJ_FMT, data, off + i * bm.OBJ_SIZE)
                shape, quality = rec[3], rec[6]
                if shape == 500:
                    ms = quality & 0x7FF
                    if ms:
                        shapes.add(ms)
    return sorted(shapes)


def build_sheet(shape, atlas, meta, font):
    frames = sorted(((int(k.split("_")[1]), v) for k, v in meta["frames"].items()
                     if k.startswith(f"{shape}_")), key=lambda t: t[0])
    if not frames:
        return None

    cw = max(v[2] for _, v in frames) + PAD * 2
    ch = max(v[3] for _, v in frames) + PAD * 2 + LABEL_H
    rows = (len(frames) + COLS - 1) // COLS

    sheet = Image.new("RGBA", (COLS * cw, rows * ch), (30, 30, 34, 255))
    draw = ImageDraw.Draw(sheet)
    for i, (fi, (sx, sy, sw, sh)) in enumerate(frames):
        cx, cy = (i % COLS) * cw, (i // COLS) * ch
        if i % 2:
            draw.rectangle([cx, cy, cx + cw - 1, cy + ch - 1], fill=(42, 42, 48, 255))
        spr = atlas.crop((sx, sy, sx + sw, sy + sh))
        px = cx + (cw - sw) // 2
        py = cy + LABEL_H + (ch - LABEL_H - sh) // 2
        sheet.alpha_composite(spr, (px, py))
        draw.text((cx + cw // 2, cy + 1), str(fi),
                  fill=(255, 220, 120, 255), font=font, anchor="mt")
    return sheet, len(frames)


def main():
    atlas = Image.open("atlas.png").convert("RGBA")
    meta = json.load(open("atlas.json"))
    os.makedirs(OUT, exist_ok=True)
    try:
        font = ImageFont.truetype("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf", 10)
    except Exception:
        font = ImageFont.load_default()

    for shape in monster_shapes():
        result = build_sheet(shape, atlas, meta, font)
        if not result:
            print(f"shape {shape}: no atlas frames, skipped")
            continue
        sheet, n = result
        path = os.path.join(OUT, f"monster_{shape}.png")
        sheet.save(path)
        print(f"wrote {path}  {sheet.width}x{sheet.height}  ({n} frames)")


if __name__ == "__main__":
    main()
