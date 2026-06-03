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

import numpy as np
from PIL import Image

from build_map import find_game_file, DEFAULT_GAME_DIR, parse_typeflags

ATLAS_PNG = Path("atlas.png")
ATLAS_JSON = Path("json/atlas.json")
ATLAS_WIDTH = 4096

# ── Palette-index translucency (Pentagram's XForm blend) ──────────────────
# A shape flagged "translucent" in TYPEFLAG.DAT does NOT fade as a whole.
# Pentagram blends ONLY the pixels whose palette index is 8..14, each with a
# fixed colour, and leaves every other pixel opaque (graphics/XFormBlend.cpp
# U8XFormPal + SoftRenderSurface.inl). The blend is premultiplied:
#     result = dst*(256-A)/256 + premultRGB
# We reproduce it by baking those pixels into the atlas as a straight-alpha
# RGBA equivalent (canvas source-over gives dst*(1-a)+c*a), so the viewer just
# draws the sprite normally — no whole-object globalAlpha hack.
#
# premultRGB + A from U8XFormPal (the table U8Game.cpp feeds the U8 palette;
# the on-disk XFORMPAL.DAT is a different packed format Pentagram ignores).
XFORM_PREMULT = {
    8:  (48, 48, 48, 80),    # green -> dark grey
    9:  (24, 24, 24, 80),    # black -> v.dark grey
    10: (64, 64, 24, 64),    # yellow
    11: (80, 80, 80, 80),    # white -> grey
    12: (180, 90, 0, 80),    # red -> orange
    13: (0, 0, 252, 40),     # blue
    14: (0, 0, 104, 40),     # blue
}
# Indices 8..14 occupy fixed atlas palette slots 248..254 (255 = transparent,
# 0..247 = the quantised opaque colours).
XFORM_SLOT = {idx: 248 + (idx - 8) for idx in XFORM_PREMULT}


def xform_straight_rgba(idx):
    """Convert a premultiplied XForm entry to straight-alpha RGBA so that a
    canvas source-over blit reproduces Pentagram's `dst*(256-A)/256 + premult`.
        a_straight = 255*A/256 ;  c_straight = premult*256/A  (clamped 0..255)
    """
    pr, pg, pb, A = XFORM_PREMULT[idx]
    a = round(255 * A / 256)
    sc = lambda c: min(255, round(c * 256 / A))
    return (sc(pr), sc(pg), sc(pb), a)


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


def decode_frame(flx_data: bytes, frm_base: int, palette, track_xform=False):
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

    When `track_xform` is set (translucent shapes only), also returns a
    height×width uint8 array holding the palette index (8..14) at each XForm
    pixel and 0 elsewhere, so the caller can bake those pixels translucent.

    Returns (Image, xoff, yoff, xfmap_or_None) or None for empty frames.
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
    xfmap = np.zeros((height, width), np.uint8) if track_xform else None

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
                        if xfmap is not None and 8 <= idx <= 14:
                            xfmap[y, xpos + d] = idx
                p += dlen
            else:
                # compressed: same byte repeated dlen times
                idx = flx_data[p]; p += 1
                color = palette[idx] + (255,)
                xf = idx if (xfmap is not None and 8 <= idx <= 14) else 0
                for d in range(dlen):
                    if 0 <= xpos + d < width:
                        px[xpos + d, y] = color
                        if xf:
                            xfmap[y, xpos + d] = xf
            xpos += dlen

    return img, xoff, yoff, xfmap


def iter_shape_frames(flx_path: Path, palette, translucent_shapes=frozenset()):
    """Yield (shape_id, fi, image, xoff, yoff, xfmap) for every frame. shape_id
    follows the +2 bias used elsewhere in the pipeline (FLX entry i serves
    shape_id i + 2). xfmap is the XForm-pixel index map for translucent shapes,
    else None."""
    data = flx_path.read_bytes()
    count = u32(data, 84)
    tbl = 144
    for i in range(count):
        off = u32(data, tbl + i * 8)
        ln = u32(data, tbl + i * 8 + 4)
        if off == 0 or ln == 0:
            continue
        shape_id = i + 2
        track = shape_id in translucent_shapes
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
            decoded = decode_frame(data, frm_base, palette, track_xform=track)
            if decoded is None:
                continue
            img, xoff, yoff, xfmap = decoded
            # Skip placeholder/empty frames (no opaque pixels). FLX uses 1×1
            # filler entries to keep frame counts contiguous; those would just
            # bloat the atlas without ever being drawn.
            if not img.getbbox():
                continue
            yield shape_id, fi, img, xoff, yoff, xfmap


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
    img, xoff, yoff, xfmap = decoded
    if img.getbbox():
        yield 1, frame, img, xoff, yoff, xfmap


def main(game_dir=DEFAULT_GAME_DIR):
    print(f"Using game directory: {game_dir}")
    palette = load_palette(Path(find_game_file(game_dir, "U8PAL.PAL")))
    typeflags = parse_typeflags(find_game_file(game_dir, "TYPEFLAG.DAT"))
    translucent_shapes = {s for s, tf in typeflags.items() if tf.get("translucent")}
    print(f"{len(translucent_shapes)} translucent shapes (XForm pixels)")
    sprites = list(iter_shape_frames(
        Path(find_game_file(game_dir, "U8SHAPES.FLX")), palette, translucent_shapes))
    sprites += list(iter_avatar_spawn(game_dir, palette))
    print(f"{len(sprites)} sprites decoded")

    # Shelf-pack tallest-first.
    by_height = sorted(sprites, key=lambda s: -s[2].height)
    placed = []
    x = y = shelf_h = 0
    for shape, fi, img, xoff, yoff, xfmap in by_height:
        w, h = img.width, img.height
        if x + w > ATLAS_WIDTH:
            y += shelf_h
            x = 0
            shelf_h = 0
        placed.append((shape, fi, x, y, w, h, img, xoff, yoff, xfmap))
        x += w
        if h > shelf_h:
            shelf_h = h

    # Define atlas size
    atlas_h = y + shelf_h
    print(f"atlas: {ATLAS_WIDTH} x {atlas_h}  ({ATLAS_WIDTH*atlas_h/1e6:.1f} MP)")

    # 1. Create a temporary RGBA canvas to assemble frames. atlas_xf tracks the
    #    XForm palette index (8..14) at each translucent pixel, 0 elsewhere.
    rgba_atlas = Image.new("RGBA", (ATLAS_WIDTH, atlas_h), (0, 0, 0, 0))
    atlas_xf = np.zeros((atlas_h, ATLAS_WIDTH), np.uint8)
    frames = {}
    for shape, fi, px, py, w, h, img, xoff, yoff, xfmap in placed:
        rgba_atlas.paste(img, (px, py))
        if xfmap is not None:
            atlas_xf[py:py + h, px:px + w] = xfmap
        frames[f"{shape}_{fi}"] = [px, py, w, h]

    print(f"writing {ATLAS_PNG}…")

    # Extract the true alpha mask array channel
    alpha = rgba_atlas.getchannel('A')

    # Isolate RGB contents onto a solid white backing to avoid border artifact loops
    rgb_atlas = Image.new("RGB", rgba_atlas.size, (255, 255, 255))
    rgb_atlas.paste(rgba_atlas, mask=alpha)

    # Quantize the opaque colours to 248 slots, reserving 248..254 for the
    # XForm translucent colours and 255 for transparent.
    quantized_rgb = rgb_atlas.quantize(colors=248, method=Image.Quantize.FASTOCTREE)

    # 256-colour palette: [0..247] quantised, [248..254] XForm straight colours
    # (one per index 8..14), [255] transparent backing.
    qpal = (list(quantized_rgb.getpalette() or []) + [0] * 744)[:744]
    xform_rgb = []
    for idx in range(8, 15):
        r, g, b, _a = xform_straight_rgba(idx)
        xform_rgb += [r, g, b]
    final_palette = qpal + xform_rgb + [0, 0, 0]

    # Build the index buffer: transparent → 255, XForm pixels → their reserved
    # slot, everything else → the quantised opaque index.
    out = np.asarray(quantized_rgb, np.uint8).copy()
    out[np.asarray(alpha, np.uint8) == 0] = 255
    for idx, slot in XFORM_SLOT.items():
        out[atlas_xf == idx] = slot

    quantized_atlas = Image.fromarray(out, mode="P")
    quantized_atlas.putpalette(final_palette)

    # Per-index alpha (tRNS): opaque everywhere except the XForm slots (their
    # straight-alpha) and the transparent backing slot.
    trns = bytearray([255] * 256)
    for idx, slot in XFORM_SLOT.items():
        trns[slot] = xform_straight_rgba(idx)[3]
    trns[255] = 0

    # optimize=False so PIL keeps our explicit palette/slot layout intact.
    quantized_atlas.save(ATLAS_PNG, optimize=False, transparency=bytes(trns))
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
