#!/usr/bin/env python3
"""
u8cmp.py  –  Decoder for the compressed Ultima 8 shape format (U8SHAPES.CMP).

The European CD releases (German/French/Spanish) ship their shapes compressed
in Pentagram's "Ultima8 CMP" format instead of the plain U8SHAPES.FLX. The FLX
*container* is byte-for-byte the same; only each shape entry differs:

  * a 5-byte 'special' preamble. Each byte value becomes a back-reference
    marker: special[b] = run length (2..6). Everything below is relative to
    base + 5 (call it `body`), which then mirrors the uncompressed U8 layout:
  * header: 4 unk + 2 num_frames
  * frame table: 6 bytes/frame = u32 frame_offset + 2 unk   (U8 uses u24+1+u16);
    frame data lives at body + frame_offset.
  * frame header (10 bytes): u16 compression, i16 width/height/xoff/yoff.
    There is NO on-disk line-offset table — scanlines are stored back-to-back.
  * the scanline RLE is delta-compressed against the *previous* frame: a literal
    byte equal to a special marker copies `special[b]` pixels from the previous
    frame at the aligned position; a 0xFF literal copies an explicit run length
    (next byte) from it.

`_read_cmp_frame` decompresses one frame into the *standard* U8 RLE buffer plus
per-line offsets — exactly as ScummVM/Pentagram's ConvertShapeFrame::ReadCmpFrame
does — so the ordinary RLE→pixel walk (`_render_into`) renders it. The cross-
frame copy (`_get_pixels`) is ported from ConvertShapeFrame::GetPixels. Source:
pentagram/convert/ConvertShape.cpp.
"""

import struct

u16 = lambda d, o: struct.unpack_from("<H", d, o)[0]
i16 = lambda d, o: struct.unpack_from("<h", d, o)[0]
u32 = lambda d, o: struct.unpack_from("<I", d, o)[0]

BYTES_SPECIAL = 5          # length of the per-shape back-ref-marker preamble


def is_cmp_shapes(path) -> bool:
    """True if `path` names the compressed shapes file (…/U8SHAPES.CMP)."""
    return str(path).upper().endswith(".CMP")


def _get_pixels(prev, outbuf, o, count, x, y):
    """Copy `count` pixels from the previous frame into outbuf[o:], reading its
    decompressed standard-RLE at the position aligned by the frames' anchors.
    Faithful port of ConvertShapeFrame::GetPixels (writes may stop early at a
    transparent gap, exactly as the original; the caller still advances by
    `count`, which the encoder only emits over solid runs)."""
    x += prev["xoff"]
    y += prev["yoff"]
    if y > prev["h"] or y < 0:
        return
    rle = prev["rle"]
    comp = prev["comp"]
    width = prev["w"]
    ld = prev["lines"][y]
    xpos = 0
    wi = o
    while True:
        xpos += rle[ld]; ld += 1                 # skip (transparent) run
        if xpos == width:
            break
        dlen = rle[ld]; ld += 1
        type_ = (dlen & 1) if comp else 0
        if comp:
            dlen >>= 1
        if xpos <= x < xpos + dlen:
            diff = x - xpos
            dlen -= diff
            xpos = x
            num = dlen if dlen < count else count
            if not type_:
                ld += diff
                for k in range(num):
                    outbuf[wi] = rle[ld + k]; wi += 1
            else:
                val = rle[ld]
                for _ in range(num):
                    outbuf[wi] = val; wi += 1
            count -= num
            x += num
            if count == 0:
                return
        ld += dlen if not type_ else 1
        xpos += dlen
        if xpos >= width:
            break


def _read_cmp_frame(data, p, special, prev):
    """Decompress one CMP frame at offset `p` into standard U8 RLE.

    Returns a frame dict {comp,w,h,xoff,yoff,rle,lines}. `prev` is the previous
    frame dict (or None for frame 0) for inter-frame back-references."""
    comp = u16(data, p)
    width = i16(data, p + 2)
    height = i16(data, p + 4)
    xoff = i16(data, p + 6)
    yoff = i16(data, p + 8)
    p += 10
    rlebuf = bytearray()
    lines = [0] * max(height, 0)
    outbuf = bytearray(512)
    for y in range(height):
        lines[y] = len(rlebuf)
        xpos = 0
        while True:
            skip = data[p]; p += 1
            xpos += skip
            if xpos > width:
                p -= 1
                skip = width - (xpos - skip)
            rlebuf.append(skip & 0xFF)
            if xpos >= width:
                break
            dlen = data[p]; p += 1
            if dlen == 0 or dlen == 1:
                # Degenerate run: drop the skip just written, replace it with a
                # transparent run filling the rest of the line, and end the line.
                p -= 1
                rlebuf.pop()
                rlebuf.append((skip + (width - xpos)) & 0xFF)
                break
            type_ = (dlen & 1) if comp else 0
            if comp:
                dlen >>= 1
            if not type_:
                o = 0
                extra = 0
                j = 0
                while j < dlen:
                    c = data[p]; p += 1
                    if special[c] and prev is not None:
                        cnt = special[c]
                        _get_pixels(prev, outbuf, o, cnt, xpos - xoff, y - yoff)
                        o += cnt
                        extra += cnt - 1
                        xpos += cnt
                    elif c == 0xFF and prev is not None:
                        cnt = data[p]; p += 1
                        _get_pixels(prev, outbuf, o, cnt, xpos - xoff, y - yoff)
                        o += cnt
                        extra += cnt - 2
                        xpos += cnt
                        j += 1
                    else:
                        outbuf[o] = c; o += 1
                        xpos += 1
                    j += 1
                rlebuf.append(((dlen + extra) << comp) & 0xFF)
                rlebuf.extend(outbuf[:dlen + extra])         # dlen+extra == o
            else:
                rlebuf.append(((dlen << 1) | 1) & 0xFF)
                rlebuf.append(data[p]); p += 1
                xpos += dlen
            if xpos >= width:
                break
    return {"comp": comp, "w": width, "h": height, "xoff": xoff, "yoff": yoff,
            "rle": bytes(rlebuf), "lines": lines}


def _render_into(fr, palette, track_xform):
    """Render a decompressed frame's standard RLE to an RGBA image (+ optional
    XForm-pixel map). Mirrors build_atlas.decode_frame's pixel walk."""
    from PIL import Image
    w, h = fr["w"], fr["h"]
    if w <= 0 or h <= 0:
        fr["img"] = None
        fr["xfmap"] = None
        return
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = img.load()
    rle, lines, comp = fr["rle"], fr["lines"], fr["comp"]
    xfmap = None
    if track_xform:
        import numpy as np
        xfmap = np.zeros((h, w), np.uint8)
    for y in range(h):
        p = lines[y]
        xpos = 0
        while xpos < w:
            skip = rle[p]; p += 1
            xpos += skip
            if xpos >= w:
                break
            dlen = rle[p]; p += 1
            run_type = (dlen & 1) if comp else 0
            if comp:
                dlen >>= 1
            if run_type == 0:
                for d in range(dlen):
                    idx = rle[p + d]
                    if 0 <= xpos + d < w:
                        px[xpos + d, y] = palette[idx] + (255,)
                        if xfmap is not None and 8 <= idx <= 14:
                            xfmap[y, xpos + d] = idx
                p += dlen
            else:
                idx = rle[p]; p += 1
                color = palette[idx] + (255,)
                xf = idx if (xfmap is not None and 8 <= idx <= 14) else 0
                for d in range(dlen):
                    if 0 <= xpos + d < w:
                        px[xpos + d, y] = color
                        if xf:
                            xfmap[y, xpos + d] = xf
            xpos += dlen
    fr["img"] = img
    fr["xfmap"] = xfmap


def _frame_positions(data, base):
    """Return (num_frames, body, [frame_data_offset, ...]) for the shape at the
    FLX entry beginning at byte `base`."""
    body = base + BYTES_SPECIAL
    num = u16(data, body + 4)
    offs = [body + u32(data, body + 6 + f * 6) for f in range(num)]
    return num, body, offs


def cmp_frame_meta(data, base):
    """Per-frame metadata (no pixel decompression) for the CMP shape at `base`,
    matching build_map.parse_shapes' frame dicts: {fi, sx, sy, ox, oy}."""
    num, body, offs = _frame_positions(data, base)
    out = []
    for f, fp in enumerate(offs):
        out.append({"fi": f,
                    "sx": i16(data, fp + 2), "sy": i16(data, fp + 4),
                    "ox": i16(data, fp + 6), "oy": i16(data, fp + 8)})
    return out


def decode_cmp_entry(data, base, palette, track_xform=False, upto=None):
    """Decode the frames of the CMP shape at `base` to images.

    Returns a list of frame dicts (comp/w/h/xoff/yoff/rle/lines, plus img/xfmap
    once rendered; img is None for empty frames). Frames are *decompressed*
    sequentially because each may back-reference the previous one. If `upto` is
    given, only frames 0..upto are decompressed and only frame `upto` is
    rendered (the earlier ones are still produced as RLE for the back-ref chain
    but carry no image) — used to pull a single Avatar pose cheaply."""
    num, body, offs = _frame_positions(data, base)
    if upto is not None:
        offs = offs[:upto + 1]
    special = [0] * 256
    for i in range(BYTES_SPECIAL):
        special[data[base + i]] = i + 2
    frames = []
    for f, fp in enumerate(offs):
        fr = _read_cmp_frame(data, fp, special, frames[f - 1] if f > 0 else None)
        frames.append(fr)
    to_render = frames[-1:] if (upto is not None and frames) else frames
    for fr in to_render:
        _render_into(fr, palette, track_xform)
    return frames
