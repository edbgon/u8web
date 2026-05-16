# u8web
Web based map viewer for the classic DOS game Ultima VIII: Pagan

<img width="1084" height="738" alt="image" src="https://github.com/user-attachments/assets/ca6f59f9-79de-4a9a-8e77-669ee37ab039" />

## Information
Hello knaves, this is ...hmm... some idea I had for some time, I wanted to get to know the formats of one of my old favorite games and with the advent of AI, why not vibe-code my way inFto something interesting.

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
| `EUSECODE.FLX` | `USECODE/` | Usecode bytecode — bark/description strings + book/scroll/tombstone/plaque text (for `extract_barks.py`) |
| `U8FONTS.FLX`  | `STATIC/` | Bitmap fonts (for `extract_fonts.py`) |
| `U8GUMPS.FLX`  | `STATIC/` | Gump artwork — UI backdrops incl. the reading-modal pages (for `build_gumps.py`) |

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
4. **Extract object descriptions** (optional, one-time — only re-run if the
   game files change):
   ```
   python extract_barks.py
   ```
   Recovers text from the usecode and writes two files:
   - `json/barks.json` — each object's bark / look-at description.
   - `json/readables.json` — the full contents of books, scrolls,
     tombstones and plaques.
   `build_map.py` bakes both into the viewer: barks drive the inspector and
   on-map selection popup, readables drive the reading modal. If you skip
   this, the popup falls back to the shape label and the modal is disabled.
5. **Extract the bitmap fonts** (optional, one-time — only re-run if the
   game files change):
   ```
   python extract_fonts.py
   ```
   Decodes U8's bitmap fonts from `U8FONTS.FLX` into `fonts/` — a glyph-sheet
   PNG at 1×/2×/4× plus a JSON manifest each. The default run extracts the
   four faces the viewer draws with: font 6 ("Normal Red") for the on-map
   popup and fonts 1 / 10 / 11 for the book-scroll / plaque / tombstone
   reading modals. Pass `--all` to extract every font, or `--font N` for a
   specific one; see `fonts/README.md` for the sheet layout.
6. **Build the gump atlas** (optional, one-time — only re-run if the game
   files change):
   ```
   python build_gumps.py
   ```
   Decodes `U8GUMPS.FLX` into `gumps.png` + `json/gumps.json` — U8's UI
   artwork. The viewer uses the book / scroll / tombstone / plaque gumps as
   the backdrop of the reading modal; skipping this disables the modal.
7. **Generate the music** (optional, one-time):
   ```
   python extract_music.py
   ```
   Converts each map's background track to MIDI in `midi/` and writes
   `json/music.json` (see the Music section below).
8. **Generate the maps and viewer HTML**:
   ```
   python build_map.py
   ```
   Writes one `maps/map_N.json` per map plus a self-contained `map.html`.
   Re-run this whenever you tweak the renderer, labels, or any of the
   non-shape game files.
9. **Serve and open**:
   ```
   python -m http.server
   ```
   then visit <http://localhost:8000/map.html>. Use `#map=N` in the URL to
   jump to a specific map (e.g. <http://localhost:8000/map.html#map=10>).

All the build/extract scripts accept `--game-dir`, e.g.:

```
python build_atlas.py    --game-dir /games/Ultima8
python build_gumps.py    --game-dir /games/Ultima8
python build_map.py      --game-dir /games/Ultima8
python extract_music.py  --game-dir /games/Ultima8
python extract_barks.py  --game-dir /games/Ultima8
python extract_fonts.py  --game-dir /games/Ultima8
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

### Fonts

`extract_fonts.py` pulls U8's bitmap fonts out of `STATIC/U8FONTS.FLX`. The
fonts are ordinary U8 shapes — each FLX entry is one font, each frame within
it is one glyph keyed by ASCII code — so the same RLE shape decoder used by
`build_atlas.py` reads them. The colour and the black outline are baked into
the pixels on a transparent background, so no recolouring is needed.

For each font it writes, into `fonts/`:

 - `<name>.png` / `<name>@2x.png` / `<name>@4x.png` — glyph sheets on a
   16-column grid. The 2×/4× sheets are nearest-neighbour upscales, so the
   blocky pixels stay sharp (no interpolation).
 - `<name>.json` — manifest: the cell grid plus per-glyph width/advance.

The default run extracts the four faces the viewer draws with — font 6
("Normal Red") for the on-map selection popup and fonts 1 / 10 / 11 for the
reading modal — and `--all` extracts all 16. See `fonts/README.md` for the
sheet layout and a ready-to-paste canvas rendering helper.

The viewer draws the **selection popup** with the 2× red font: click any
object and its name (or bark text) floats above it in the original game
typeface.

### Gumps and the reading modal

`build_gumps.py` decodes `STATIC/U8GUMPS.FLX` — U8's UI artwork (book pages,
scrolls, container backdrops, …) — into a single `gumps.png` plus a
`json/gumps.json` manifest, exactly like the sprite atlas but for gumps.

When a selected object is a **book, scroll, tombstone or plaque** that has
text, a small book icon appears beneath it. Clicking the icon opens a modal:
the matching gump (book / scroll / tombstone / plaque) drawn 2× as a
pixel-sharp backdrop, with the contents rendered over it in the appropriate
U8 font — font 1 for books and scrolls, 10 for plaques, 11 for tombstones.
Long text scrolls within the page. The text itself is recovered from the
usecode by `extract_barks.py` into `json/readables.json`: tombstones and
plaques call the read intrinsics inline under a `getQuality()` switch, while
books and scrolls dispatch (via a process spawn) into a shared library class
whose per-quality functions hold the literal pages.

### Implementation notes

- The viewer uses a single offscreen canvas as a baked static cache, then per-frame clips and redraws just the bbox of each animation against the cache. Painter order is preserved without re-rendering thousands of sprites every frame.
- Animation cycles (atypes 1–4 and 6) are ported from Pentagram's `Item::animateItem`. Atype 5 (usecode-driven) is a no-op here.

## KNOWN BUGS
- Z-order - There are still one or two edge cases if you look really closely, otherwise there's some slight misalignment.
- Mobile use - not too mobile friendly.
- Performance - It's still not performing wonderfully, especially when using z-sliders, but should be manageable on more modern systems.
