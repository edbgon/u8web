#!/usr/bin/env python3
"""
extract_fonts.py  –  Extract Ultima 8 bitmap fonts from STATIC/U8FONTS.FLX

U8 fonts are ordinary U8 shapes: each FLX entry is one font, and each frame
within it is one glyph keyed by ASCII code (frame index == character code).
The pixels already carry the colour *and* the black outline baked in, with a
transparent background — exactly the "blocky red text with black outline"
look, no post-processing needed.

For each requested font this writes, into fonts/:
  <name>.png / <name>@2x.png / <name>@4x.png  – glyph sheets, 16-column grid.
       2x/4x are nearest-neighbour scaled so the pixels stay crisp.
  <name>.json  – metadata: cell grid + per-glyph width/advance, so a browser
       can blit glyphs from the sheet (see fonts/README for usage).

Font index → name follows pentagram/docs/u8fonts.txt. The default run
extracts the four faces the viewer draws with: 6 ("Normal Red", the on-map
selection popup) and 1 / 10 / 11 (book-scroll / plaque / tombstone modals).

Usage:
  python extract_fonts.py                 # fonts 1, 6, 10, 11 (viewer set)
  python extract_fonts.py --font 0        # a specific font
  python extract_fonts.py --all           # every font in the FLX
"""

import json
import struct
import argparse
from pathlib import Path

from PIL import Image

from build_map import find_game_file, DEFAULT_GAME_DIR
from build_atlas import load_palette, decode_frame

OUT_DIR = Path("fonts")
COLS = 16          # glyphs per row in the sheet (classic 16x16 codepage grid)
SCALES = (2, 4)    # extra nearest-neighbour upscales to emit

u32 = lambda d, o: struct.unpack_from("<I", d, o)[0]
u16 = lambda d, o: struct.unpack_from("<H", d, o)[0]
u24 = lambda d, o: u32(d, o) & 0xFFFFFF

# FLX entry index → (filename stem, human description). From u8fonts.txt.
FONT_NAMES = {
    0:  ("font00_blue",        "Normal Light Blue"),
    1:  ("font01_black",       "Black (books and scrolls)"),
    2:  ("font02_tiny_blue",   "Tiny Dark Blue"),
    3:  ("font03_tiny_white",  "Tiny White"),
    4:  ("font04_tiny_black",  "Tiny Black"),
    5:  ("font05_orange",      "Normal Orange"),
    6:  ("font06_red",         "Normal Red"),
    7:  ("font07_green",       "Normal Green"),
    8:  ("font08_yellow",      "Normal Yellow"),
    9:  ("font09_small_blue",  "Small Dark Blue"),
    10: ("font10_gold_sign",   "Gold Sign (upper & numbers only)"),
    11: ("font11_tombstone",   "Tomb Stone (upper & numbers only)"),
    12: ("font12_green_num",   "Normal Green (numbers only)"),
    13: ("font13_red_num",     "Normal Red (numbers only)"),
    14: ("font14_dkblue",      "Normal Dark Blue"),
    15: ("font15_dkblue2",     "Normal Dark Blue"),
    16: ("font16_jp_dbcs",     "Japanese DBCS (Shift-JIS subset)"),
}

# hlead (kerning overlap) / vlead (line-gap delta) per font, from
# pentagram/data/u8.ini [fontleads]. baseline-skip = glyph height + vlead.
FONT_LEADS = {
    0: (0, -1), 1: (0, -1), 2: (0, -1), 3: (0, -1), 4: (0, 0), 5: (0, -1),
    6: (0, -1), 7: (0, -1), 8: (0, -1), 9: (0, 0), 10: (0, 4), 11: (0, 4),
    12: (0, -1), 13: (0, -1), 14: (0, -1), 15: (0, -1),
}

# The Japanese U8FONTS.FLX carries one extra entry (16): a Shift-JIS DBCS font.
# Frames 0x00-0xFF are single-byte glyphs (frame == byte value: ASCII plus the
# SJIS halfwidth katakana at 0xA1-0xDF). Frames 256+ pack a curated JIS X 0208
# subset, 94 cells per row, in this row order: JIS ku 1, 3, 4, 5 (symbols,
# fullwidth alphanumerics, hiragana, katakana), then ku 16-47 (all of level-1
# kanji) — 36 rows × 94 = 3384 glyphs. (ku 2 and ku 6-15 are omitted; level-2
# kanji in ku 48+ aren't included, matching the game's text.)
#
# We translate each frame to its Unicode codepoint and key the glyph by that,
# so the viewer looks glyphs up by character via the same `byCode` map the
# Latin fonts use (for ASCII, frame == codepoint already).
JP_DBCS_FONT = 16
_JP_NONKANJI_ROWS = {0: 1, 1: 3, 2: 4, 3: 5}   # packed row -> JIS ku


def jp_frame_codepoint(frame):
    """Unicode codepoint for a Japanese DBCS-font frame, or None for an
    unassigned / non-printable cell (blank grid slots, control codes)."""
    if frame < 0x100:
        try:
            s = bytes([frame]).decode("cp932")
        except UnicodeDecodeError:
            return None
        return ord(s) if len(s) == 1 and s.isprintable() else None
    row, ten0 = divmod(frame - 0x100, 94)
    if row in _JP_NONKANJI_ROWS:
        ku = _JP_NONKANJI_ROWS[row]
    elif 4 <= row <= 35:
        ku = 16 + (row - 4)            # JIS level-1 kanji, ku 16..47
    else:
        return None
    try:
        ch = bytes([0xA0 + ku, 0xA0 + ten0 + 1]).decode("euc_jp")
    except UnicodeDecodeError:
        return None                    # unassigned JIS cell
    return ord(ch)


def read_flx_entries(path: Path):
    """Yield (index, base_offset) for every non-empty FLX entry."""
    data = path.read_bytes()
    count = u32(data, 84)
    tbl = 128
    for i in range(count):
        off = u32(data, tbl + i * 8)
        ln = u32(data, tbl + i * 8 + 4)
        if off and ln:
            yield i, data, off


def extract_glyphs(data: bytes, base: int, palette):
    """Decode every glyph frame of one font shape.

    Returns (glyphs, cell_w, cell_h) where glyphs is a list of dicts keyed by
    ASCII code. Empty glyphs (e.g. space) are kept — their frame width is a
    real advance even though they paint nothing.
    """
    n_frm = u16(data, base + 4)
    glyphs = []
    cell_w = cell_h = 0
    for code in range(n_frm):
        fh = base + 6 + code * 6
        frm_base = base + u24(data, fh)
        if frm_base + 18 > len(data):
            continue
        decoded = decode_frame(data, frm_base, palette)
        if decoded is None:
            continue
        img, xoff, yoff, _ = decoded
        glyphs.append({
            "code": code, "img": img,
            "w": img.width, "h": img.height,
            "xoff": xoff, "yoff": yoff,
        })
        cell_w = max(cell_w, img.width)
        cell_h = max(cell_h, img.height)
    return glyphs, cell_w, cell_h


def build_font(idx: int, data: bytes, base: int, palette):
    stem, desc = FONT_NAMES.get(idx, (f"font{idx:02d}", f"Font {idx}"))
    glyphs, cell_w, cell_h = extract_glyphs(data, base, palette)
    if idx == JP_DBCS_FONT:
        # Re-key by Unicode codepoint and drop the blank/unassigned cells the
        # JIS grid leaves between characters (see jp_frame_codepoint).
        kept = []
        for g in glyphs:
            cp = jp_frame_codepoint(g["code"])
            if cp is None or not g["img"].getbbox():
                continue
            g["code"] = cp
            kept.append(g)
        glyphs = kept
    if not glyphs:
        print(f"  font {idx} ({desc}): no glyphs, skipped")
        return

    # Lay glyphs on a fixed grid: glyph `n` sits at the top-left of cell n.
    # A grid keeps the sheet trivial to address from JS (no per-glyph x/y).
    rows = (len(glyphs) + COLS - 1) // COLS
    sheet = Image.new("RGBA", (COLS * cell_w, rows * cell_h), (0, 0, 0, 0))
    for slot, g in enumerate(glyphs):
        cx = (slot % COLS) * cell_w
        cy = (slot // COLS) * cell_h
        sheet.paste(g["img"], (cx, cy))

    hlead, vlead = FONT_LEADS.get(idx, (0, 0))
    baseline = max(g["yoff"] for g in glyphs)
    meta = {
        "name": stem,
        "description": desc,
        "fontIndex": idx,
        "cols": COLS,
        "cellWidth": cell_w,
        "cellHeight": cell_h,
        "glyphHeight": cell_h,
        "baseline": baseline,            # px from cell top down to text baseline
        "hlead": hlead,                  # horizontal kerning overlap
        "vlead": vlead,                  # line-gap delta
        "lineHeight": cell_h + vlead,    # baseline-to-baseline advance
        # slot order == grid order; glyphs[i] occupies cell i.
        "glyphs": [
            {"code": g["code"], "slot": slot, "width": g["w"],
             "height": g["h"], "advance": g["w"]}
            for slot, g in enumerate(glyphs)
        ],
    }

    OUT_DIR.mkdir(exist_ok=True)
    png = OUT_DIR / f"{stem}.png"
    sheet.save(png)
    sizes = [f"1x {sheet.width}x{sheet.height}"]
    for s in SCALES:
        scaled = sheet.resize((sheet.width * s, sheet.height * s), Image.NEAREST)
        scaled.save(OUT_DIR / f"{stem}@{s}x.png")
        sizes.append(f"{s}x {scaled.width}x{scaled.height}")
    (OUT_DIR / f"{stem}.json").write_text(json.dumps(meta, separators=(",", ":")))
    print(f"  font {idx} ({desc}): {len(glyphs)} glyphs, "
          f"cell {cell_w}x{cell_h}  [{', '.join(sizes)}]")


def write_readme():
    (OUT_DIR / "README.md").write_text(
        "# Ultima 8 bitmap fonts\n\n"
        "Extracted from `STATIC/U8FONTS.FLX` by `extract_fonts.py`.\n\n"
        "Each font is a glyph **sheet PNG** plus a **JSON** manifest. The red\n"
        "fill and black outline are baked into the pixels on a transparent\n"
        "background. `@2x` / `@4x` PNGs are nearest-neighbour upscales — keep\n"
        "`image-rendering: pixelated` (CSS) or `imageSmoothingEnabled = false`\n"
        "(canvas) so they stay sharp.\n\n"
        "## Layout\n\n"
        "Glyphs sit on a fixed `cols`-wide grid of `cellWidth x cellHeight`\n"
        "cells. Glyph `i` in the JSON `glyphs` array lives at grid `slot i`:\n\n"
        "```\n"
        "col = slot % cols;  row = slot // cols\n"
        "srcX = col * cellWidth;  srcY = row * cellHeight\n"
        "```\n\n"
        "Each glyph entry has `code` (ASCII), `width`/`height` (painted size,\n"
        "anchored at the cell's top-left) and `advance` (pen step). Subtract\n"
        "`hlead` from the pen between glyphs; advance lines by `lineHeight`.\n\n"
        "## Canvas usage\n\n"
        "```js\n"
        "const meta = await (await fetch('fonts/font06_red.json')).json();\n"
        "const sheet = new Image(); sheet.src = 'fonts/font06_red.png';\n"
        "const byCode = new Map(meta.glyphs.map(g => [g.code, g]));\n\n"
        "function drawText(ctx, text, x, y, scale = 1) {\n"
        "  ctx.imageSmoothingEnabled = false;\n"
        "  let penX = x;\n"
        "  for (const ch of text) {\n"
        "    const g = byCode.get(ch.charCodeAt(0));\n"
        "    if (!g) continue;\n"
        "    const col = g.slot % meta.cols, row = (g.slot / meta.cols) | 0;\n"
        "    ctx.drawImage(sheet,\n"
        "      col * meta.cellWidth, row * meta.cellHeight, g.width, g.height,\n"
        "      penX, y, g.width * scale, g.height * scale);\n"
        "    penX += (g.advance - meta.hlead) * scale;\n"
        "  }\n"
        "}\n"
        "```\n")


def main(game_dir=DEFAULT_GAME_DIR, fonts=None, do_all=False):
    print(f"Using game directory: {game_dir}")
    palette = load_palette(Path(find_game_file(game_dir, "U8PAL.PAL")))
    flx = Path(find_game_file(game_dir, "U8FONTS.FLX"))
    entries = {i: (data, off) for i, data, off in read_flx_entries(flx)}

    if do_all:
        wanted = sorted(entries)
    elif fonts:
        wanted = fonts
    else:
        # The viewer needs all four faces it draws with: 6 (red) for the
        # on-map selection popup, and 1 / 10 / 11 for the book-scroll,
        # plaque and tombstone reading modals respectively.
        wanted = [1, 6, 10, 11]
        # The Japanese release adds entry 16 (the Shift-JIS DBCS font); it is
        # the only Japanese-capable face, so pull it whenever it's present.
        if JP_DBCS_FONT in entries:
            wanted.append(JP_DBCS_FONT)

    print(f"extracting {len(wanted)} font(s) from {flx.name}:")
    for idx in wanted:
        if idx not in entries:
            print(f"  font {idx}: not present in FLX, skipped")
            continue
        data, off = entries[idx]
        build_font(idx, data, off, palette)
    write_readme()
    print(f"done → {OUT_DIR}/")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Extract U8 bitmap fonts from U8FONTS.FLX.")
    ap.add_argument("--game-dir", default=DEFAULT_GAME_DIR,
                    help=f"Ultima VIII game directory (default: {DEFAULT_GAME_DIR})")
    ap.add_argument("--font", type=int, action="append", dest="fonts",
                    help="font index to extract (repeatable; default: 6)")
    ap.add_argument("--all", action="store_true", help="extract every font")
    args = ap.parse_args()
    main(game_dir=args.game_dir, fonts=args.fonts, do_all=args.all)
