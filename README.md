# u8web
Web based map viewer for the classic DOS game Ultima VIII: Pagan

<img width="1084" height="738" alt="image" src="https://github.com/user-attachments/assets/ca6f59f9-79de-4a9a-8e77-669ee37ab039" />

## Information
This is ...hmm... some idea I had for some time, I wanted to get to know the formats of one of my old favorite games and with the advent of AI, why not vibe-code my way inFto something interesting.

Well, that's what this is... you can look at the maps from Ultima VIII, almost the way they were intended to be shown and __incredibly__ inefficiently!

I'm not sure how much more I will develop this, but as of now I can explain some features.

### Prerequisites

You'll need your own copy of Ultima VIII: Pagan. **No file copying required** — the
build scripts read directly from a game install via the `--game-dir` argument and
locate the files they need (case-insensitively) inside `STATIC/`, `GAMEDAT/` and
`SOUND/`.

The scripts default to `./ULTIMA8`, so the simplest setup is to symlink (or copy)
your game directory there:

```
ln -s /path/to/your/Ultima8  ./ULTIMA8
```

Otherwise pass the path explicitly, e.g. `python build_map.py --game-dir /path/to/Ultima8`.

Files used from the install:

| File | Found in | Used for |
| --- | --- | --- |
| `U8SHAPES.FLX` | `STATIC/` | Sprite pixel data (decoded by `build_atlas.py`) |
| `U8PAL.PAL`    | `STATIC/` | 256-colour palette for shape decoding |
| `FIXED.DAT`    | `STATIC/` | Fixed objects |
| `GLOB.FLX`     | `STATIC/` | Glob macros (groups of fixed objects) |
| `TYPEFLAG.DAT` | `STATIC/` | Per-shape flags / footprint dimensions |
| `NONFIXED.DAT` | `GAMEDAT/` | Movable objects |
| `MUSIC.FLX`    | `SOUND/`  | XMIDI music tracks (for `extract_music.py`) |

Repo-supplied data files:
 - `json/labels.json` — labels for object names, needs some tweaking but is fairly descriptive.
 - `json/mapnames.json` — friendly names for maps.

You also need Python 3 with [Pillow](https://pillow.readthedocs.io/) installed (`pip install pillow`) for the atlas build step.

### Quick start

1. Clone the repo.
2. Point the scripts at your game install — symlink it to `./ULTIMA8` (see above) or
   pass `--game-dir` to each command.
3. **Build the sprite atlas** (one-time, slow — only re-run if the game files change):
   ```
   python build_atlas.py
   ```
   This decodes every shape from `U8SHAPES.FLX`, and using `U8PAL.PAL`, packs them into a single
   `atlas.png` (ca. 6 MB, 4096×5700) and writes an `atlas.json` manifest.
4. **Generate the maps and viewer HTML**:
   ```
   python build_map.py
   ```
   Writes one `maps/map_N.json` per map plus a self-contained `map.html`.
   Re-run this whenever you tweak the renderer, labels, or any of the
   non-shape game files.
5. **Serve and open**:
   ```
   python -m http.server
   ```
   then visit <http://localhost:8000/map.html>. Use `#map=N` in the URL to
   jump to a specific map (e.g. <http://localhost:8000/map.html#map=10>).

All three scripts accept `--game-dir`, e.g.:

```
python build_atlas.py  --game-dir /games/Ultima8
python build_map.py    --game-dir /games/Ultima8
python extract_music.py --game-dir /games/Ultima8
```

### The sprite atlas

`build_atlas.py` is a from-scratch U8 shape decoder (RLE format ported loosely from
ScummVM's `ultima8` engine). It reads `U8SHAPES.FLX` + `U8PAL.PAL` straight out of the
game install and writes:

 - `atlas.png` — a single paletted PNG holding every `(shape, frame)` sprite, with
   colour index 255 reserved as transparent.
 - `atlas.json` — manifest mapping `"SHAPE_FRAME"` keys to `[x, y, w, h]` rects.

The viewer loads the one atlas image instead of thousands of individual PNGs, so
there is no per-sprite image folder to manage.

### Music

`extract_music.py` pulls the background music for every map out of the game's
`SOUND/MUSIC.FLX` and converts it to standard MIDI:

 - The U8 songs are stored as **XMIDI**; the script converts XMIDI → standard MIDI
   (SMF), with logic ported from Pentagram's `XMidiFile.cpp` (summed-byte delays,
   note-on-with-duration → synthesised note-offs, fixed 120 ticks/sec timing).
 - Entry 0 of `MUSIC.FLX` is the song-name table, so output files are named after
   their original `.xmi`, e.g. `midi/056_tenebrae.mid`.
 - Writes the converted `.mid` files into `midi/` and refreshes `json/music.json`
   (a `track-number → .xmi-filename` map).

Which track plays on which map comes from the shape-562 "music egg" objects:
`build_map.py` records each map's track number(s) into `maps/index.json` under a
`"music"` key, so the viewer / `extract_music.py` can join the two.

In the viewer, the **Ambience (music)** checkbox plays the MIDI track for the
currently loaded map and swaps it automatically when you switch maps. Playback
is handled in-browser by [JZZ](https://jazz-soft.net/) with its built-in
waveform synth, so no soundfont is required. The track is a simple synth
rendition rather than a high-fidelity reproduction.

### Implementation notes

- The viewer uses a single offscreen canvas as a baked static cache, then per-frame clips and redraws just the bbox of each animation against the cache. Painter order is preserved without re-rendering thousands of sprites every frame.
- Animation cycles (atypes 1–4 and 6) are ported from Pentagram's `Item::animateItem`. Atype 5 (usecode-driven) is a no-op here.

## KNOWN BUGS
- Z-order - There are still one or two edge cases if you look really closely, otherwise there's some slight misalignment.
- Mobile use - not too mobile friendly.
- Performance - It's still not performing wonderfully, especially when using z-sliders, but should be manageable on more modern systems.
