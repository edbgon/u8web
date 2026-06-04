#!/usr/bin/env python3
"""extract_install.py — unpack the ARJ install archives shipped on the
Ultima VIII media into a playable install tree.

The CD/floppy media carries the game as two self-contained ARJ archives:

    ULTIMA8.001    the main game (STATIC/, USECODE/, SOUND/, SAVEGAME/, exes)
    U8SPEECH.001   the optional speech pack (the E<NNN>.FLX voice archives)

Both store their members with the original relative paths, so extracting
them reproduces the directory layout the build scripts expect (see the
"Files used from the install" table in README.md). Point `-i` at the folder
holding the `.001` files (or directly at one) and `-o` at where to unpack:

    python extract_install.py -i /media/cdrom/ENGLISH -o ./ULTIMA8

This is a from-scratch ARJ decoder (no external `arj`/`7z` needed) — a Python
port of the ARJ method-1/4 decompressor as found in ScummVM's `unarj.cpp`
(itself derived from the GPL ARJ 3.10 sources). Each member is verified
against the CRC-32 stored in its header before it is written.
"""

import argparse
import os
import struct
import sys
import zlib

# ── ARJ format constants (decode.c / unarj.cpp) ──────────────────────────
HEADER_ID = 0xEA60          # little-endian magic 0x60 0xEA
FIRST_HDR_SIZE = 30
HEADERSIZE_MAX = FIRST_HDR_SIZE + 10 + 512 + 2048

# Decompressor parameters.
UCHAR_MAX = 255
CODE_BIT = 16
THRESHOLD = 3
DICSIZ = 26624
MAXMATCH = 256
NC = UCHAR_MAX + MAXMATCH + 2 - THRESHOLD   # 510
NP = 16 + 1                                 # 17
NT = CODE_BIT + 3                           # 19
NPT = NT if NT > NP else NP
CTABLESIZE = 4096
PTABLESIZE = 256
CBIT = 9
PBIT = 5
TBIT = 5

# arj_flags bits we care about.
FLAG_GARBLED = 0x01    # password-protected
FLAG_VOLUME = 0x04     # continued in the next volume (split archive)


class ArjError(Exception):
    pass


class _Decoder:
    """Bit-stream Huffman+LZSS decoder for ARJ methods 1–4."""

    def __init__(self, comp):
        self.comp = comp
        self.cpos = 0
        self.compsize = len(comp)
        self.bitbuf = 0
        self.bytebuf = 0
        self.bitcount = 0
        self.blocksize = 0
        self.left = [0] * (2 * NC - 1)
        self.right = [0] * (2 * NC - 1)
        self.c_len = bytearray(NC)
        self.pt_len = bytearray(NPT)
        self.c_table = [0] * CTABLESIZE
        self.pt_table = [0] * PTABLESIZE
        self.ntext = bytearray(DICSIZ)

    # ── bit reader ──────────────────────────────────────────────────────
    def _fillbuf(self, n):
        while self.bitcount < n:
            self.bitbuf = ((self.bitbuf << self.bitcount)
                           | (self.bytebuf >> (8 - self.bitcount))) & 0xFFFF
            n -= self.bitcount
            if self.compsize > 0:
                self.compsize -= 1
                self.bytebuf = self.comp[self.cpos]
                self.cpos += 1
            else:
                self.bytebuf = 0
            self.bitcount = 8
        self.bitcount -= n
        self.bitbuf = ((self.bitbuf << n)
                       | (self.bytebuf >> (8 - n))) & 0xFFFF
        self.bytebuf = (self.bytebuf << n) & 0xFFFF

    def _getbits(self, n):
        rc = self.bitbuf >> (CODE_BIT - n)
        self._fillbuf(n)
        return rc

    def _init_getbits(self):
        self.bitbuf = 0
        self.bytebuf = 0
        self.bitcount = 0
        self._fillbuf(CODE_BIT)

    # ── Huffman table construction ──────────────────────────────────────
    def _make_table(self, nchar, bitlen, tablebits, table, tablesize):
        # `start[]` is uint16 in the C original; the validity checks compare
        # against (uint16)(1<<16) == 0, so we mask every accumulation to 16
        # bits and test against 0 (not 65536).
        count = [0] * 17
        weight = [0] * 17
        start = [0] * 18
        for i in range(nchar):
            count[bitlen[i]] += 1
        start[1] = 0
        for i in range(1, 17):
            start[i + 1] = (start[i] + (count[i] << (16 - i))) & 0xFFFF
        if start[17] != 0:
            raise ArjError("bad Huffman table (codes do not fill range)")
        jutbits = 16 - tablebits
        for i in range(1, tablebits + 1):
            start[i] >>= jutbits
            weight[i] = 1 << (tablebits - i)
        i = tablebits + 1
        while i <= 16:
            weight[i] = 1 << (16 - i)
            i += 1
        i = start[tablebits + 1] >> jutbits
        if i != 0:
            k = 1 << tablebits
            while i != k:
                table[i] = 0
                i += 1
        avail = nchar
        mask = 1 << (15 - tablebits)
        left, right = self.left, self.right
        for ch in range(nchar):
            length = bitlen[ch]
            if length == 0:
                continue
            k = start[length]
            nextcode = k + weight[length]
            if length <= tablebits:
                if nextcode > tablesize:
                    raise ArjError("bad Huffman table (table overflow)")
                for i in range(start[length], nextcode):
                    table[i] = ch
            else:
                # Walk/extend the over-long-code subtree. `p` is a pointer in
                # C; here it is (container, index) into table/left/right.
                cont, idx = table, k >> jutbits
                i = length - tablebits
                while i != 0:
                    if cont[idx] == 0:
                        right[avail] = left[avail] = 0
                        cont[idx] = avail
                        avail += 1
                    nxt = cont[idx]
                    cont = right if (k & mask) else left
                    idx = nxt
                    k = (k << 1) & 0xFFFF
                    i -= 1
                cont[idx] = ch
            start[length] = nextcode

    # ── code-length tables ──────────────────────────────────────────────
    def _read_pt_len(self, nn, nbit, i_special):
        n = self._getbits(nbit)
        if n == 0:
            c = self._getbits(nbit)
            for i in range(nn):
                self.pt_len[i] = 0
            for i in range(256):
                self.pt_table[i] = c
        else:
            i = 0
            while i < n:
                c = self.bitbuf >> 13
                if c == 7:
                    mask = 1 << 12
                    while mask & self.bitbuf:
                        mask >>= 1
                        c += 1
                self._fillbuf(3 if c < 7 else c - 3)
                self.pt_len[i] = c
                i += 1
                if i == i_special:
                    c = self._getbits(2)
                    while c > 0:
                        self.pt_len[i] = 0
                        i += 1
                        c -= 1
            while i < nn:
                self.pt_len[i] = 0
                i += 1
            self._make_table(nn, self.pt_len, 8, self.pt_table, PTABLESIZE)

    def _read_c_len(self):
        n = self._getbits(CBIT)
        if n == 0:
            c = self._getbits(CBIT)
            for i in range(NC):
                self.c_len[i] = 0
            for i in range(CTABLESIZE):
                self.c_table[i] = c
        else:
            i = 0
            while i < n:
                c = self.pt_table[self.bitbuf >> 8]
                if c >= NT:
                    mask = 1 << 7
                    while True:
                        if self.bitbuf & mask:
                            c = self.right[c]
                        else:
                            c = self.left[c]
                        mask >>= 1
                        if c < NT:
                            break
                self._fillbuf(self.pt_len[c])
                if c <= 2:
                    if c == 0:
                        c = 1
                    elif c == 1:
                        c = self._getbits(4) + 3
                    else:
                        c = self._getbits(CBIT) + 20
                    while c > 0:
                        self.c_len[i] = 0
                        i += 1
                        c -= 1
                else:
                    self.c_len[i] = c - 2
                    i += 1
            while i < NC:
                self.c_len[i] = 0
                i += 1
            self._make_table(NC, self.c_len, 12, self.c_table, CTABLESIZE)

    # ── symbol decoders ─────────────────────────────────────────────────
    def _decode_c(self):
        if self.blocksize == 0:
            self.blocksize = self._getbits(CODE_BIT)
            self._read_pt_len(NT, TBIT, 3)
            self._read_c_len()
            self._read_pt_len(NP, PBIT, -1)
        self.blocksize -= 1
        j = self.c_table[self.bitbuf >> 4]
        if j >= NC:
            mask = 1 << 3
            while True:
                if self.bitbuf & mask:
                    j = self.right[j]
                else:
                    j = self.left[j]
                mask >>= 1
                if j < NC:
                    break
        self._fillbuf(self.c_len[j])
        return j

    def _decode_p(self):
        j = self.pt_table[self.bitbuf >> 8]
        if j >= NP:
            mask = 1 << 7
            while True:
                if self.bitbuf & mask:
                    j = self.right[j]
                else:
                    j = self.left[j]
                mask >>= 1
                if j < NP:
                    break
        self._fillbuf(self.pt_len[j])
        if j != 0:
            j -= 1
            j = (1 << j) + self._getbits(j)
        return j

    # ── method 1–3 ──────────────────────────────────────────────────────
    def decode(self, origsize):
        self.blocksize = 0
        self._init_getbits()
        out = bytearray()
        ntext = self.ntext
        count = origsize
        r = 0
        while count > 0:
            c = self._decode_c()
            if c <= UCHAR_MAX:
                ntext[r] = c
                count -= 1
                r += 1
                if r >= DICSIZ:
                    out += ntext
                    r = 0
            else:
                j = c - (UCHAR_MAX + 1 - THRESHOLD)
                count -= j
                i = r - self._decode_p() - 1
                if i < 0:
                    i += DICSIZ
                if r > i and r < DICSIZ - MAXMATCH - 1:
                    while j > 0:
                        ntext[r] = ntext[i]
                        r += 1
                        i += 1
                        j -= 1
                else:
                    while j > 0:
                        ntext[r] = ntext[i]
                        r += 1
                        if r >= DICSIZ:
                            out += ntext
                            r = 0
                        i += 1
                        if i >= DICSIZ:
                            i = 0
                        j -= 1
        if r > 0:
            out += ntext[:r]
        return bytes(out)

    # ── method 4 ────────────────────────────────────────────────────────
    def _decode_len(self):
        plus = 0
        pwr = 1
        width = 0
        while width < 7:
            if self._getbits(1) == 0:
                break
            plus += pwr
            pwr <<= 1
            width += 1
        c = self._getbits(width) if width != 0 else 0
        return c + plus

    def _decode_ptr(self):
        plus = 0
        pwr = 1 << 9
        width = 9
        while width < 13:
            if self._getbits(1) == 0:
                break
            plus += pwr
            pwr <<= 1
            width += 1
        c = self._getbits(width) if width != 0 else 0
        return c + plus

    def decode_f(self, origsize):
        self._init_getbits()
        out = bytearray()
        ntext = self.ntext
        ncount = 0
        r = 0
        while ncount < origsize:
            c = self._decode_len()
            if c == 0:
                ntext[r] = self._getbits(8)
                ncount += 1
                r += 1
                if r >= DICSIZ:
                    out += ntext
                    r = 0
            else:
                j = c - 1 + THRESHOLD
                ncount += j
                i = r - self._decode_ptr() - 1
                if i < 0:
                    i += DICSIZ
                while j > 0:
                    ntext[r] = ntext[i]
                    r += 1
                    if r >= DICSIZ:
                        out += ntext
                        r = 0
                    i += 1
                    if i >= DICSIZ:
                        i = 0
                    j -= 1
        if r > 0:
            out += ntext[:r]
        return bytes(out)


# ── archive parsing ──────────────────────────────────────────────────────
class ArjMember:
    __slots__ = ("name", "method", "flags", "comp_size", "orig_size",
                 "crc", "data_pos")


# Block kinds returned by _read_header.
_END = "end"      # zero-length marker — no more blocks
_MAIN = "main"    # archive/comment header (file_type 2) — no payload
_FILE = "file"    # a stored member


def _read_header(data, pos):
    """Parse one ARJ block at `pos`. Returns (kind, ArjMember-or-None, next_pos)."""
    if pos + 4 > len(data):
        raise ArjError("truncated archive (no header at %d)" % pos)
    magic = struct.unpack_from("<H", data, pos)[0]
    if magic != HEADER_ID:
        raise ArjError("bad header id 0x%04X at %d" % (magic, pos))
    basic_size = struct.unpack_from("<H", data, pos + 2)[0]
    if basic_size == 0:
        return _END, None, pos + 4
    if basic_size > HEADERSIZE_MAX:
        raise ArjError("oversized header (%d) at %d" % (basic_size, pos))
    bh_start = pos + 4
    bh = data[bh_start:bh_start + basic_size]
    stored_crc = struct.unpack_from("<I", data, bh_start + basic_size)[0]
    if zlib.crc32(bh) != stored_crc:
        raise ArjError("header CRC mismatch at %d" % pos)

    first_hdr_size = bh[0]
    file_type = bh[6]

    # Skip the extended-header chain (each: u16 size + payload + u32 crc).
    p = bh_start + basic_size + 4
    while True:
        ext_size = struct.unpack_from("<H", data, p)[0]
        p += 2
        if ext_size == 0:
            break
        p += ext_size + 4

    # file_type 2 is the main/comment header — it carries no payload.
    if file_type == 2:
        return _MAIN, None, p

    m = ArjMember()
    m.flags = bh[4]
    m.method = bh[5]
    m.comp_size = struct.unpack_from("<i", bh, 12)[0]
    m.orig_size = struct.unpack_from("<i", bh, 16)[0]
    m.crc = struct.unpack_from("<I", bh, 20)[0]
    name_end = bh.index(b"\0", first_hdr_size)
    m.name = bh[first_hdr_size:name_end].decode("latin1")
    m.data_pos = p
    return _FILE, m, p + m.comp_size


def read_members(data):
    """Yield every file ArjMember in the archive `data` (a bytes object)."""
    # U8's archives start with the magic at 0, but tolerate a leading
    # SFX/garbage stub by scanning for it.
    pos = 0
    if struct.unpack_from("<H", data, 0)[0] != HEADER_ID:
        pos = data.find(b"\x60\xEA")
        if pos < 0:
            raise ArjError("not an ARJ archive (no header magic)")

    while True:
        kind, member, pos = _read_header(data, pos)
        if kind is _END:
            break
        if kind is _FILE:
            yield member


def decompress_member(data, m):
    """Return the decompressed bytes for member `m`, CRC-verified."""
    if m.flags & FLAG_GARBLED:
        raise ArjError("%s is password-protected" % m.name)
    if m.flags & FLAG_VOLUME:
        raise ArjError("%s is split across volumes (unsupported)" % m.name)
    comp = data[m.data_pos:m.data_pos + m.comp_size]
    if m.method == 0:
        out = bytes(comp[:m.orig_size])
    elif m.method in (1, 2, 3):
        out = _Decoder(comp).decode(m.orig_size)
    elif m.method == 4:
        out = _Decoder(comp).decode_f(m.orig_size)
    else:
        raise ArjError("%s uses unsupported method %d" % (m.name, m.method))
    if len(out) != m.orig_size:
        raise ArjError("%s: decoded %d bytes, expected %d"
                       % (m.name, len(out), m.orig_size))
    if zlib.crc32(out) != m.crc:
        raise ArjError("%s: CRC mismatch (data corrupt or decoder bug)"
                       % m.name)
    return out


# ── extraction ─────────────────────────────────────────────────────────
def _safe_join(out_dir, name):
    """Map an archive member name to a path under out_dir, defeating path
    traversal (absolute paths, '..', drive letters)."""
    norm = name.replace("\\", "/")
    parts = [p for p in norm.split("/")
             if p not in ("", ".", "..") and ":" not in p]
    return os.path.join(out_dir, *parts)


def extract_archive(arj_path, out_dir):
    """Extract every member of one .001 archive into out_dir. Returns count."""
    with open(arj_path, "rb") as fh:
        data = fh.read()
    n = 0
    for m in read_members(data):
        out = decompress_member(data, m)
        dest = _safe_join(out_dir, m.name)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(out)
        rel = os.path.relpath(dest, out_dir)
        print("  %-24s %9d bytes" % (rel, len(out)))
        n += 1
    return n


def _find_archives(in_path):
    """Resolve the input path to a list of (kind, path) archives to extract.

    `in_path` may be a single .001 file or a directory; a directory is
    searched recursively for ULTIMA8.001 and U8SPEECH.001 (case-insensitive).
    """
    if os.path.isfile(in_path):
        return [("archive", in_path)]
    if not os.path.isdir(in_path):
        raise ArjError("no such file or directory: %s" % in_path)

    wanted = {"ultima8.001": "game", "u8speech.001": "speech"}
    found = {}
    for root, _dirs, files in os.walk(in_path):
        for f in files:
            kind = wanted.get(f.lower())
            if kind:
                found.setdefault(kind, []).append(os.path.join(root, f))

    archives = []
    for kind, label in (("game", "ULTIMA8.001"), ("speech", "U8SPEECH.001")):
        paths = found.get(kind)
        if not paths:
            continue
        if len(paths) > 1:
            raise ArjError(
                "found %d copies of %s under %s — point -i at one language "
                "folder:\n    %s" % (len(paths), label, in_path,
                                     "\n    ".join(sorted(paths))))
        archives.append((kind, paths[0]))
    if not archives:
        raise ArjError("no ULTIMA8.001 or U8SPEECH.001 found under %s"
                       % in_path)
    return archives


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Extract the Ultima VIII ARJ install archives "
                    "(ULTIMA8.001, U8SPEECH.001) into an install tree.")
    ap.add_argument("-i", "--input", required=True,
                    help="folder holding the .001 files (or a single .001)")
    ap.add_argument("-o", "--output", default="./ULTIMA8",
                    help="install directory to extract into "
                         "(default: ./ULTIMA8)")
    args = ap.parse_args(argv)

    try:
        archives = _find_archives(args.input)
    except ArjError as e:
        sys.exit("error: %s" % e)

    os.makedirs(args.output, exist_ok=True)
    total = 0
    for _kind, path in archives:
        print("Extracting %s -> %s" % (path, args.output))
        try:
            total += extract_archive(path, args.output)
        except ArjError as e:
            sys.exit("error: %s" % e)
    print("Done: %d files extracted to %s" % (total, args.output))


if __name__ == "__main__":
    main()
