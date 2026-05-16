#!/usr/bin/env python3
"""
Extract the songs referenced by maps/index.json from the game's MUSIC.FLX
and convert each from XMIDI to a standard MIDI file (.mid) in midi/.

XMIDI -> SMF conversion ported from Pentagram's audio/midi/XMidiFile.cpp:
  * event delays are a run of bytes < 0x80, summed (GetVLQ2)
  * note-on (0x9x) is followed by a standard VLQ duration; the note-off is
    synthesised at note_on_time + duration
  * XMIDI has no tempo of its own -- it runs at 120 ticks/sec, so the SMF
    gets division=60 and a single 500000us/qn (120 BPM) tempo event
"""
import json, struct, os, argparse

from build_map import find_game_file, DEFAULT_GAME_DIR

OUT = "midi"


# FLX index table starts at 0x80 (128). build_map.py's parse_shapes uses 144
# and gets away with it only because its shape lookup compensates with -2;
# MUSIC.FLX has no such bias, so the table must be read at the true offset.
FLX_TABLE = 128


def flx_entry(data, idx):
    count = struct.unpack_from("<I", data, 84)[0]
    if idx >= count:
        return b""
    off, ln = struct.unpack_from("<II", data, FLX_TABLE + idx * 8)
    if not off or not ln:
        return b""
    return data[off:off + ln]


def parse_song_names(data):
    """Entry 0 lists 'SONGNAME.XMI <char> <b> <c>'; ASCII(char) = flex track."""
    text = flx_entry(data, 0).decode("latin1")
    names = {}
    for line in text.splitlines():
        line = line.strip()
        if line == "#":               # section 1 ends at the first lone '#'
            break
        parts = line.split()
        if len(parts) >= 2 and parts[0].lower().endswith(".xmi"):
            names[ord(parts[1])] = parts[0].lower()
    return names


def get_vlq(buf, p):
    """Standard MIDI variable-length quantity."""
    q = 0
    while True:
        d = buf[p]; p += 1
        q = (q << 7) | (d & 0x7F)
        if not (d & 0x80):
            return q, p


def find_evnt(buf):
    """Yield the raw event bytes of every EVNT chunk inside an XMIDI blob."""
    p = 0
    n = len(buf)
    while p + 8 <= n:
        cid = buf[p:p + 4]
        ln = struct.unpack_from(">I", buf, p + 4)[0]
        if cid in (b"FORM", b"CAT "):
            p += 12          # descend: skip id+len+formtype
            continue
        if cid == b"EVNT":
            yield buf[p + 8:p + 8 + ln]
        p += 8 + ln + (ln & 1)


def convert_track(evnt):
    """XMIDI event stream -> list of (abs_time, event_bytes)."""
    events = []
    p, n, time = 0, len(evnt), 0
    while p < n:
        # delay: sum bytes until one with the high bit set (the status)
        while p < n and not (evnt[p] & 0x80):
            time += evnt[p]; p += 1
        if p >= n:
            break
        status = evnt[p]; p += 1
        hi = status >> 4
        if hi == 0x9:                                   # note on (+duration)
            note, vel = evnt[p], evnt[p + 1]; p += 2
            dur, p = get_vlq(evnt, p)
            events.append((time, bytes((status, note, vel))))
            events.append((time + dur, bytes((0x80 | (status & 0xF), note, 0))))
        elif hi == 0x8:                                 # note off
            events.append((time, bytes((status, evnt[p], evnt[p + 1])))); p += 2
        elif hi in (0xA, 0xB, 0xE):                     # 2 data bytes
            events.append((time, bytes((status, evnt[p], evnt[p + 1])))); p += 2
        elif hi in (0xC, 0xD):                          # 1 data byte
            events.append((time, bytes((status, evnt[p])))); p += 1
        elif status == 0xFF:                            # meta
            mtype = evnt[p]; p += 1
            ln, p = get_vlq(evnt, p)
            payload = evnt[p:p + ln]; p += ln
            if mtype == 0x2F:                           # end of track
                break
            if mtype == 0x51:                           # XMIDI ignores tempo
                continue
            events.append((time, bytes((0xFF, mtype)) + write_vlq(ln) + payload))
        elif status in (0xF0, 0xF7):                    # sysex
            ln, p = get_vlq(evnt, p)
            events.append((time, bytes((status,)) + write_vlq(ln) + evnt[p:p + ln]))
            p += ln
        else:
            break                                       # unknown -> bail
    return events


def write_vlq(q):
    out = bytearray([q & 0x7F])
    q >>= 7
    while q:
        out.insert(0, (q & 0x7F) | 0x80)
        q >>= 7
    return bytes(out)


def build_smf(events):
    events.sort(key=lambda e: e[0])                     # stable, by time
    track = bytearray()
    track += write_vlq(0) + bytes((0xFF, 0x51, 0x03, 0x07, 0xA1, 0x20))  # 120 BPM
    last = 0
    for t, ev in events:
        track += write_vlq(t - last) + ev
        last = t
    track += write_vlq(0) + bytes((0xFF, 0x2F, 0x00))   # end of track
    smf = b"MThd" + struct.pack(">IHHH", 6, 0, 1, 60)
    smf += b"MTrk" + struct.pack(">I", len(track)) + bytes(track)
    return smf


def main(game_dir=DEFAULT_GAME_DIR):
    data = open(find_game_file(game_dir, "MUSIC.FLX"), "rb").read()
    index = json.load(open("maps/index.json"))
    names = parse_song_names(data)
    os.makedirs(OUT, exist_ok=True)

    tracks = sorted({t for v in index.get("music", {}).values() for t in v})
    done, missing, music_json = [], [], {}
    for num in tracks:
        blob = flx_entry(data, num)
        if not blob:
            missing.append(num)
            continue
        events = []
        for ev in find_evnt(blob):
            events += convert_track(ev)
        xmi = names.get(num, "")
        music_json[str(num)] = xmi
        stem = os.path.splitext(xmi)[0] or str(num)
        fname = f"{num:03d}_{stem}.mid"
        open(os.path.join(OUT, fname), "wb").write(build_smf(events))
        done.append(fname)

    json.dump(music_json, open("json/music.json", "w"), indent=2, sort_keys=True)

    for f in done:
        print("wrote", f)
    print(f"\n{len(done)} tracks -> {OUT}/  ;  json/music.json updated")
    if missing:
        print("not present in this MUSIC.FLX (empty entries):", missing)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Extract & convert U8 music (XMIDI -> MIDI) from a game install.")
    ap.add_argument("--game-dir", default=DEFAULT_GAME_DIR,
                    help=f"Path to the Ultima VIII game directory "
                         f"(default: {DEFAULT_GAME_DIR})")
    args = ap.parse_args()
    main(game_dir=args.game_dir)
