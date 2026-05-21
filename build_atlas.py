#!/usr/bin/env python3
"""
Build atlas.png + atlas.json directly from a U8 game install's U8SHAPES.FLX
and U8PAL.PAL. No titan-ultima dependency — we decode the U8 shape RLE here.

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
import argparse
from pathlib import Path

from PIL import Image

from build_map import find_game_file, DEFAULT_GAME_DIR

ATLAS_PNG = Path("atlas.png")
ATLAS_JSON = Path("json/atlas.json")
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


def iter_avatar_spawn(game_dir, palette):
    """Yield the one sprite for the player Avatar (shape 1).

    The Avatar isn't a normal U8SHAPES shape: its frames live in FLX entry 1,
    which iter_shape_frames never sees — that loop reads the index table at
    144 (the +2 bias), so it starts at real entry 2. We only need a single
    pose: the one ITEMCACH.DAT places the Avatar in at the start of a new
    game (washed ashore, lying prone), so the viewer can show the player at
    the spawn point. Extracting all ~1550 avatar frames would just bloat the
    atlas with poses nothing ever draws."""
    flx = Path(find_game_file(game_dir, "U8SHAPES.FLX")).read_bytes()
    base = u32(flx, 0x80 + 1 * 8)            # real index table -> entry 1

    def flx_entry0(d):
        off, ln = struct.unpack_from("<II", d, 128)
        return d[off:off + ln]

    # Spawn frame = ITEMCACH low byte + NPCDATA high byte for actor slot 1,
    # exactly as World::loadItemCachNPCData / build_map.parse_npcs compute it.
    icd = flx_entry0(Path(find_game_file(game_dir, "ITEMCACH.DAT")).read_bytes())
    ndd = flx_entry0(Path(find_game_file(game_dir, "NPCDATA.DAT")).read_bytes())
    frame = icd[0x0FC00 + 1] + (ndd[1 * 0x31 + 7] << 8)

    frm_off = u24(flx, base + 6 + frame * 6)
    decoded = decode_frame(flx, base + frm_off, palette)
    if decoded is None:
        return
    img, xoff, yoff = decoded
    if img.getbbox():
        yield 1, frame, img, xoff, yoff


def main(game_dir=DEFAULT_GAME_DIR):
    print(f"Using game directory: {game_dir}")
    palette = load_palette(Path(find_game_file(game_dir, "U8PAL.PAL")))
    sprites = list(iter_shape_frames(
        Path(find_game_file(game_dir, "U8SHAPES.FLX")), palette))
    sprites += list(iter_avatar_spawn(game_dir, palette))
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

    # Define atlas size
    atlas_h = y + shelf_h
    print(f"atlas: {ATLAS_WIDTH} x {atlas_h}  ({ATLAS_WIDTH*atlas_h/1e6:.1f} MP)")

    # 1. Create a temporary RGBA canvas to assemble frames
    rgba_atlas = Image.new("RGBA", (ATLAS_WIDTH, atlas_h), (0, 0, 0, 0))
    frames = {}
    for shape, fi, px, py, w, h, img, xoff, yoff in placed:
        rgba_atlas.paste(img, (px, py))
        frames[f"{shape}_{fi}"] = [px, py, w, h]

    print(f"writing {ATLAS_PNG}…")
    
    # Extract the true alpha mask array channel
    alpha = rgba_atlas.getchannel('A')
    
    # Isolate RGB contents onto a solid white backing to avoid border artifact loops
    rgb_atlas = Image.new("RGB", rgba_atlas.size, (255, 255, 255))
    rgb_atlas.paste(rgba_atlas, mask=alpha)
    
    # Quantize RGB layout strictly down to 255 unique colors
    quantized_rgb = rgb_atlas.quantize(colors=255, method=Image.Quantize.FASTOCTREE)
    
    # Extract the original quantized RGB palette mapping
    original_palette = quantized_rgb.getpalette()  # Contains 765 values (255 colors * 3)
    
    # Construct a complete 256-color palette by appending a color to index 255
    # Index 255 becomes our dedicated transparent fallback color slot
    final_palette = original_palette + [0, 0, 0]
    
    # Instantiate a clean, raw palette template canvas image
    quantized_atlas = Image.new("P", rgba_atlas.size)
    quantized_atlas.putpalette(final_palette)
    
    # Fetch flattened, 1D array blocks for execution
    get_q_data = getattr(quantized_rgb, "get_flattened_data", quantized_rgb.getdata)
    get_a_data = getattr(alpha, "get_flattened_data", alpha.getdata)
    
    rgb_pixels = get_q_data()
    alpha_pixels = get_a_data()
    
    # Process memory updates via bytearray blocks for speed and precision
    output_bytes = bytearray(len(rgb_pixels))
    for i in range(len(rgb_pixels)):
        # If alpha is completely transparent, enforce the 255 color slot index allocation
        if alpha_pixels[i] == 0:
            output_bytes[i] = 255
        else:
            output_bytes[i] = rgb_pixels[i]
            
    # Inject optimized pixel structural blocks back into our canvas
    quantized_atlas.frombytes(bytes(output_bytes))
    # ──────────────────────────────────────────────────────────────────────

    # Save to file, explicitly mapping index 255 to transparent alpha in metadata
    quantized_atlas.save(ATLAS_PNG, optimize=True, transparency=255)
    print(f"  {ATLAS_PNG.stat().st_size/1024:.1f} KB")

    ATLAS_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(ATLAS_JSON, "w") as f:
        json.dump({"width": ATLAS_WIDTH, "height": atlas_h, "frames": frames},
                  f, separators=(",", ":"))
    print(f"writing {ATLAS_JSON} ({ATLAS_JSON.stat().st_size/1024:.1f} KB)")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Build atlas.png + atlas.json from a U8 game install.")
    ap.add_argument("--game-dir", default=DEFAULT_GAME_DIR,
                    help=f"Path to the Ultima VIII game directory "
                         f"(default: {DEFAULT_GAME_DIR})")
    args = ap.parse_args()
    main(game_dir=args.game_dir)
