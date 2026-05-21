#!/usr/bin/env python3
"""
Extract the audio from Ultima VIII as standard PCM .wav files.

Two sources, both decoded the same way:

* SOUND.FLX  -- the sound effects.  Entry 0 is a packed table of 8-char
  effect names; entries 1.. are Sonarc-compressed samples.
* E<NNN>.FLX -- the (optional) speech pack.  One archive per conversation;
  entry 0 is the NUL-separated dialogue text and entries 1.. are the spoken
  lines, line i voiced by audio entry i+1.

Both store 8-bit unsigned mono PCM.  The Sonarc decoder is ported straight
from Pentagram's audio/SonarcAudioSample.cpp -- a per-frame linear-predictive
coder whose residuals are entropy-coded (decode_EC) then folded back in
(decode_LPC).

Run from anywhere; paths are resolved relative to the repo root.
Output: <repo>/sounds/sfx/<index>_<NAME>.wav         (sound effects)
        <repo>/sounds/speech/E<NNN>/<idx>_<slug>.wav (speech)
        <repo>/json/speech.json                      (per-folder manifest)
"""
import argparse
import json
import os
import re
import struct
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from build_map import find_game_file, DEFAULT_GAME_DIR

OUT = str(ROOT / "sounds")
SFX_OUT = os.path.join(OUT, "sfx")
SPEECH_OUT = os.path.join(OUT, "speech")
SPEECH_INDEX_PATH = str(ROOT / "json" / "speech.json")

# FLX index table starts at offset 0x80; the record count is a u32 at 84.
FLX_COUNT = 84
FLX_TABLE = 128


def flx_entry(data, idx):
    """Raw bytes of FLEX archive entry `idx` (empty bytes if absent)."""
    count = struct.unpack_from("<I", data, FLX_COUNT)[0]
    if idx >= count:
        return b""
    off, ln = struct.unpack_from("<II", data, FLX_TABLE + idx * 8)
    if not off or not ln:
        return b""
    return data[off:off + ln]


def flx_count(data):
    return struct.unpack_from("<I", data, FLX_COUNT)[0]


def parse_names(data):
    """Entry 0 packs the effect names as fixed 8-byte, NUL-padded strings.

    Name record i belongs to FLX sample entry i + 1 (entry 0 is the table
    itself, so the names start one slot later)."""
    table = flx_entry(data, 0)
    names = {}
    for i in range(len(table) // 8):
        raw = table[i * 8:i * 8 + 8]
        name = raw.split(b"\x00")[0].decode("latin1").strip()
        if name:
            names[i + 1] = name
    return names


# --- Sonarc decompressor (port of Pentagram's SonarcAudioSample) -------------

def _generate_one_table():
    """OneTable[x] = number of consecutive low-order 1 bits in x (0..255)."""
    t = [0] * 256
    power = 2
    while power < 32:
        col = power - 1
        while col < 16:
            for row in range(16):
                t[row * 16 + col] += 1
            col += power
        power *= 2
    for i in range(16):
        t[i * 16 + 15] += t[i]
    return t


ONE_TABLE = _generate_one_table()


def decode_ec(mode, samplecount, source):
    """Entropy-code stage: unpack `samplecount` residual bytes from `source`."""
    dest = bytearray()
    zerospecial = False
    if mode >= 7:
        mode -= 7
        zerospecial = True

    data = 0
    inputbits = 0
    pos = 0
    n = len(source)

    while samplecount:
        while pos < n and inputbits <= 24:
            data |= source[pos] << inputbits
            pos += 1
            inputbits += 8

        if zerospecial and not (data & 0x1):
            dest.append(0x80)               # output zero
            data >>= 1
            inputbits -= 1
        else:
            if zerospecial:
                data >>= 1                  # strip one
                inputbits -= 1

            ones = ONE_TABLE[data & 0xFF]

            if ones == 0:
                data >>= 1                  # strip zero
                # low (mode+1) bits hold the sample; the C code sign-extends
                # by shifting up into an sint8 (8-bit truncation) then back.
                sample = ((data & 0xFF) << (7 - mode)) & 0xFF
                if sample & 0x80:
                    sample -= 0x100         # to signed
                sample >>= (7 - mode)       # arithmetic shift -> sign extend
                dest.append((sample + 0x80) & 0xFF)
                data >>= mode + 1
                inputbits -= mode + 2
            elif ones < 7 - mode:
                data >>= ones + 1           # strip ones and zero
                sample = data & 0xFF
                sample = (sample << (7 - mode - ones)) & 0x7F
                if not (sample & 0x40):
                    sample |= 0x80          # reconstruct sign bit
                if sample & 0x80:
                    sample -= 0x100
                sample >>= (7 - mode - ones)
                dest.append((sample + 0x80) & 0xFF)
                data >>= (mode + ones)
                inputbits -= mode + 2 * ones + 1
            else:
                data >>= (7 - mode)         # strip ones
                sample = data & 0x7F
                if not (sample & 0x40):
                    sample |= 0x80          # reconstruct sign bit
                dest.append((sample + 0x80) & 0xFF)
                data >>= 7
                inputbits -= 2 * 7 - mode

        samplecount -= 1

    return dest


def decode_lpc(order, nsamples, dest, factors):
    """Linear-predictive stage: fold the prediction back into `dest`."""
    for i in range(nsamples):
        accum = 0
        for j in range(order - 1, -1, -1):
            idx = i - 1 - j
            val1 = dest[idx] if idx >= 0 else 0
            val1 ^= 0x80
            if val1 & 0x80:
                val1 -= 0x100               # signed
            val2 = factors[j * 2] + (factors[j * 2 + 1] << 8)
            if val2 & 0x8000:
                val2 -= 0x10000             # signed 16-bit
            accum += val1 * val2
        accum += 0x00000800
        sub = (accum >> 12) & 0xFF
        if sub & 0x80:
            sub -= 0x100                    # signed 8-bit
        dest[i] = (dest[i] - sub) & 0xFF


def audio_decode(frame):
    """Decode one Sonarc frame into a bytearray of 8-bit unsigned samples."""
    size = frame[0] + (frame[1] << 8)
    checksum = 0
    for i in range(size // 2):
        checksum ^= frame[2 * i] + (frame[2 * i + 1] << 8)
    if checksum != 0xACED:
        raise ValueError("Sonarc frame checksum mismatch")

    order = frame[7]
    mode = frame[6] - 8
    samplecount = frame[2] + (frame[3] << 8)

    dest = decode_ec(mode, samplecount, frame[8 + 2 * order:size])
    decode_lpc(order, samplecount, dest, frame[8:])

    # Pentagram's clip-recovery heuristic.
    for i in range(1, samplecount):
        if dest[i] == 0 and dest[i - 1] > 192:
            dest[i] = 0xFF
    return dest


def decode_sample(buf):
    """Decode a full SOUND.FLX sample entry -> (sample_rate, pcm bytes)."""
    length = struct.unpack_from("<I", buf, 0)[0]
    sample_rate = struct.unpack_from("<H", buf, 4)[0]

    src_offset = 0x20
    # 'Large' samples carry an extra 256-byte index block before the frames.
    frame_bytes = struct.unpack_from("<H", buf, src_offset)[0]
    if frame_bytes == 0x20 and length > 32767:
        src_offset += 0x100

    pcm = bytearray()
    pos = src_offset
    while pos < len(buf) and len(pcm) < length:
        frame_bytes = struct.unpack_from("<H", buf, pos)[0]
        if frame_bytes == 0:
            break
        pcm += audio_decode(buf[pos:pos + frame_bytes])
        pos += frame_bytes

    return sample_rate, bytes(pcm[:length])


def write_wav(path, sample_rate, pcm):
    """8-bit unsigned mono PCM -> a standard RIFF/WAVE file."""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)                   # 8-bit; wave wants unsigned here
        w.setframerate(sample_rate)
        w.writeframes(pcm)


def parse_speech_text(data):
    """Entry 0 of an E<NNN>.FLX holds the dialogue lines, NUL-separated.

    Returns a list where element i is the text of speech sample entry i+1."""
    block = flx_entry(data, 0)
    if not block:
        return []
    return [seg.decode("latin1").strip()
            for seg in block.split(b"\x00")]


def slugify(text, maxlen=40):
    """Compact a dialogue line into a filename-safe slug."""
    slug = slug_full(text)
    return slug[:maxlen] or "line"


def slug_full(text):
    """Untruncated slug — used in the speech manifest for prefix-matching."""
    return re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()


def extract_speech(game_dir=DEFAULT_GAME_DIR):
    """Decode every E<NNN>.FLX speech archive under the game's SOUND/ dir.

    Writes wavs to sounds/speech/E<NNN>/, plus a json/speech.json manifest
    listing each folder's [full_slug, filename] pairs (full_slug from the
    untruncated raw entry-0 text) so the viewer can match dialog lines to
    the right wav (dialog lines often concatenate several entry-0 lines)."""
    flx_files = []
    for dirpath, _, files in os.walk(game_dir):
        for f in sorted(files):
            if re.fullmatch(r"E\d+\.FLX", f.upper()):
                flx_files.append(os.path.join(dirpath, f))
    flx_files.sort()
    if not flx_files:
        print(f"no E<NNN>.FLX speech archives found under '{game_dir}'. "
              f"Install the speech pack into SOUND/ first.")
        return

    manifest = {}
    done, failed = 0, []
    for path in flx_files:
        stem = Path(path).stem.upper()                  # e.g. E289
        data = open(path, "rb").read()
        lines = parse_speech_text(data)
        out_dir = os.path.join(SPEECH_OUT, stem)
        os.makedirs(out_dir, exist_ok=True)
        entries = []
        for idx in range(1, flx_count(data)):
            buf = flx_entry(data, idx)
            if not buf:
                continue
            try:
                sample_rate, pcm = decode_sample(buf)
            except (ValueError, struct.error) as e:
                failed.append((f"{stem}:{idx}", str(e)))
                continue
            if not pcm:
                continue
            text = lines[idx - 1] if idx - 1 < len(lines) else ""
            name = slugify(text) if text else ""
            wav = f"{idx:03d}_{name}.wav" if name else f"{idx:03d}.wav"
            wav_path = os.path.join(out_dir, wav)
            write_wav(wav_path, sample_rate, pcm)
            print(f"wrote speech/{stem}/{wav}  "
                  f"{len(pcm)} samples @ {sample_rate} Hz")
            entries.append([slug_full(text), wav, text])
            done += 1
        if entries:
            manifest[stem] = entries

    os.makedirs(os.path.dirname(SPEECH_INDEX_PATH), exist_ok=True)
    with open(SPEECH_INDEX_PATH, "w") as f:
        json.dump(manifest, f, separators=(",", ":"))
    print(f"\n{done} speech lines -> {SPEECH_OUT}/")
    print(f"manifest -> {SPEECH_INDEX_PATH} ({len(manifest)} folders)")
    if failed:
        print("failed to decode:",
              ", ".join(f"{i} ({e})" for i, e in failed))


def main(game_dir=DEFAULT_GAME_DIR):
    data = open(find_game_file(game_dir, "SOUND.FLX"), "rb").read()
    names = parse_names(data)
    os.makedirs(SFX_OUT, exist_ok=True)

    done, failed = 0, []
    for idx in range(1, flx_count(data)):
        buf = flx_entry(data, idx)
        if not buf:
            continue
        try:
            sample_rate, pcm = decode_sample(buf)
        except (ValueError, struct.error) as e:
            failed.append((idx, str(e)))
            continue
        if not pcm:
            continue
        name = names.get(idx, "")
        stem = f"{idx:03d}_{name}" if name else f"{idx:03d}"
        path = os.path.join(SFX_OUT, stem + ".wav")
        write_wav(path, sample_rate, pcm)
        print(f"wrote {os.path.basename(path)}  "
              f"{len(pcm)} samples @ {sample_rate} Hz")
        done += 1

    print(f"\n{done} sound effects -> {SFX_OUT}/")
    if failed:
        print("failed to decode:",
              ", ".join(f"{i} ({e})" for i, e in failed))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Extract & decode U8 audio (Sonarc -> WAV).")
    ap.add_argument("--game-dir", default=DEFAULT_GAME_DIR,
                    help=f"Path to the Ultima VIII game directory "
                         f"(default: {DEFAULT_GAME_DIR})")
    ap.add_argument("what", nargs="?", default="sfx",
                    choices=["sfx", "speech", "all"],
                    help="which audio to extract (default: sfx)")
    args = ap.parse_args()
    if args.what in ("sfx", "all"):
        main(game_dir=args.game_dir)
    if args.what in ("speech", "all"):
        extract_speech(game_dir=args.game_dir)
