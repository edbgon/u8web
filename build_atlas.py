#!/usr/bin/env python3
"""
Build atlas.png + atlas.json directly from data/U8SHAPES.FLX and data/U8PAL.PAL.
No titan-ultima dependency — we decode the U8 shape RLE here.

Output:
  atlas.png        — single PNG holding every (shape, frame) sprite
  atlas.json       — manifest: {"frames": {"SHAPE_FRAME": [sx, sy, sw, sh], ...}}

Frame keys match the JS viewer's lookup: sprite(o.shp, o.fr). The viewer's
o.shp values come from map data (obj.s) and follow the bias documented in
CLAUDE.md: shape_info.get(obj.s - 2). So FLX entry `i` provides frames for
shape id `i + 2`. We key the atlas accordingly.
"""

import json
import struct
from pathlib import Path

from PIL import Image

FLX_PATH = Path("data/U8SHAPES.FLX")
PAL_PATH = Path("data/U8PAL.PAL")
ATLAS_PNG = Path("atlas.png")
ATLAS_JSON = Path("atlas.json")
ATLAS_WIDTH = 4096


def load_palette(path: Path):
    """U8PAL.PAL is 4-byte header then 256 RGB triplets at 6-bit per channel."""
    raw = path.read_bytes()
    assert len(raw) == 772, f"unexpected palette size {len(raw)}"
    pal = []
    for i in range(256):
        r, g, b = raw[4 + i * 3], raw[5 + i * 3], raw[6 + i * 3]
        # 6-bit (0..63) → 8-bit (0..255), preserving low bits
        pal.append(((r << 2) | (r >> 4), (g << 2) | (g >> 4), (b << 2) | (b >> 4)))
    return pal


u32 = lambda d, o: struct.unpack_from("<I", d, o)[0]
u16 = lambda d, o: struct.unpack_from("<H", d, o)[0]
i16 = lambda d, o: struct.unpack_from("<h", d, o)[0]
u24 = lambda d, o: u32(d, o) & 0xFFFFFF


def decode_frame(flx_data: bytes, frm_base: int, palette):
    """
    Decode one U8 shape frame to an RGBA Pillow image.

    Header layout (ScummVM ultima8 raw_shape_frame.cpp loadU8Format):
      +0..+7   skipped (FLX-level header)
      +8       compression flag (u8)
      +9       padding
      +10..11  width  (i16 LE)
      +12..13  height (i16 LE)
      +14..15  xoff   (i16 LE) — anchor x
      +16..17  yoff   (i16 LE) — anchor y
      +18..    line_offsets[height] (u16 LE each), where on-disk[i] minus
               ((height - i) * 2) yields the offset from the start of rle_data
      then     rle_data (pixel runs)

    RLE per scanline (per ShapeFrame::load loop):
      while xpos < width:
        skip = *p++; xpos += skip
        dlen = *p++
        if compressed: type = dlen & 1; dlen >>= 1
        if type == 0 (uncompressed): write dlen literal bytes from *p++
        else (compressed run):       write same byte *p, dlen times; p++
        xpos += dlen

    Returns (Image, xoff, yoff) or None for empty frames.
    """
    comp = flx_data[frm_base + 8]
    width = i16(flx_data, frm_base + 10)
    height = i16(flx_data, frm_base + 12)
    xoff = i16(flx_data, frm_base + 14)
    yoff = i16(flx_data, frm_base + 16)
    # Sanity bounds: real U8 sprites top out near ~200px on a side. Larger
    # values mean we walked into non-shape FLX entries (palette tables etc).
    if width <= 0 or height <= 0 or width > 512 or height > 512:
        return None

    # line_offsets table starts at frm_base + 18, height entries
    line_offsets_base = frm_base + 18
    rle_data_start = line_offsets_base + height * 2

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    px = img.load()

    for y in range(height):
        raw_off = u16(flx_data, line_offsets_base + y * 2)
        # Adjust so it's an offset from start of rle_data.
        line_off = raw_off - ((height - y) * 2)
        p = rle_data_start + line_off
        xpos = 0
        while xpos < width:
            skip = flx_data[p]; p += 1
            xpos += skip
            if xpos >= width:
                break
            dlen = flx_data[p]; p += 1
            if comp:
                run_type = dlen & 1
                dlen >>= 1
            else:
                run_type = 0
            if run_type == 0:
                # uncompressed: dlen literal palette bytes
                for d in range(dlen):
                    idx = flx_data[p + d]
                    if 0 <= xpos + d < width:
                        px[xpos + d, y] = palette[idx] + (255,)
                p += dlen
            else:
                # compressed: same byte repeated dlen times
                idx = flx_data[p]; p += 1
                color = palette[idx] + (255,)
                for d in range(dlen):
                    if 0 <= xpos + d < width:
                        px[xpos + d, y] = color
            xpos += dlen

    return img, xoff, yoff


def iter_shape_frames(flx_path: Path, palette):
    """Yield (shape_id, fi, image, xoff, yoff) for every frame. shape_id
    follows the +2 bias used elsewhere in the pipeline (FLX entry i serves
    shape_id i + 2)."""
    data = flx_path.read_bytes()
    count = u32(data, 84)
    tbl = 144
    for i in range(count):
        off = u32(data, tbl + i * 8)
        ln = u32(data, tbl + i * 8 + 4)
        if off == 0 or ln == 0:
            continue
        base = off
        n_frm = u16(data, base + 4)
        for j in range(n_frm):
            fh = base + 6 + j * 6
            frm_off = u24(data, fh)
            frm_base = base + frm_off
            if frm_base + 18 > len(data):
                continue
            frm_idx = u16(data, frm_base + 2)
            fi = frm_idx if frm_idx != 0 else j
            decoded = decode_frame(data, frm_base, palette)
            if decoded is None:
                continue
            img, xoff, yoff = decoded
            # Skip placeholder/empty frames (no opaque pixels). FLX uses 1×1
            # filler entries to keep frame counts contiguous; those would just
            # bloat the atlas without ever being drawn.
            if not img.getbbox():
                continue
            yield i + 2, fi, img, xoff, yoff


def main():
    palette = load_palette(PAL_PATH)
    sprites = list(iter_shape_frames(FLX_PATH, palette))
    print(f"{len(sprites)} sprites decoded")

    # Shelf-pack tallest-first.
    by_height = sorted(sprites, key=lambda s: -s[2].height)
    placed = []
    x = y = shelf_h = 0
    for shape, fi, img, xoff, yoff in by_height:
        w, h = img.width, img.height
        if x + w > ATLAS_WIDTH:
            y += shelf_h
            x = 0
            shelf_h = 0
        placed.append((shape, fi, x, y, w, h, img, xoff, yoff))
        x += w
        if h > shelf_h:
            shelf_h = h
    atlas_h = y + shelf_h
    print(f"atlas: {ATLAS_WIDTH} x {atlas_h}  ({ATLAS_WIDTH*atlas_h/1e6:.1f} MP)")

    atlas = Image.new("RGBA", (ATLAS_WIDTH, atlas_h), (0, 0, 0, 0))
    frames = {}
    for shape, fi, px, py, w, h, img, xoff, yoff in placed:
        atlas.paste(img, (px, py))
        frames[f"{shape}_{fi}"] = [px, py, w, h]

    print(f"writing {ATLAS_PNG}…")
    atlas.save(ATLAS_PNG, optimize=True)
    print(f"  {ATLAS_PNG.stat().st_size/1024:.1f} KB")

    with open(ATLAS_JSON, "w") as f:
        json.dump({"width": ATLAS_WIDTH, "height": atlas_h, "frames": frames},
                  f, separators=(",", ":"))
    print(f"writing {ATLAS_JSON} ({ATLAS_JSON.stat().st_size/1024:.1f} KB)")


if __name__ == "__main__":
    main()
