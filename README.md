# u8web
Web-based map viewer for the classic DOS game Ultima VIII: Pagan.

See it live (with somewhat reduced functionality) [here](https://zelphuria.com/u8map).

<img width="974" height="632" alt="image" src="https://github.com/user-attachments/assets/26d5e4a8-95bb-4dad-8b2f-aa8f26227ceb" />
  
A browser map viewer that renders Ultima VIII almost the way the game does.
Loosely based on the pentagram project's source code and written in Python,
majorly assisted by large language models.

## Prerequisites

You need your own copy of Ultima VIII: Pagan. If you have the install media
available, `extract_install.py` unpacks it into a ready-to-use install tree:

```
python extract_install.py -i <media dir> -o ./ULTIMA8
```

`-i` is the folder holding the `ULTIMA8.001` (main game) and `U8SPEECH.001`
(speech pack) archives, or a single `.001` file. A folder is searched
recursively and both archives are extracted in one pass; if it finds more
than one copy (e.g. several language folders on the disc), point `-i` at the
specific language folder instead. Each archive stores its members with the
original relative paths, so the output already has the `STATIC/`, `USECODE/`,
`SOUND/` and `SAVEGAME/` layout the build scripts expect — including the
speech pack's `E<NNN>.FLX` voice files under `SOUND/`.

The `.001` files are ARJ archives; `extract_install.py` decodes them itself
(no external `arj`/`7z` needed) and verifies every member's CRC. They can
equally be unpacked with any archiver that supports ARJ, and the game can
also just be installed in something like DOSBox.

It is not required to run the setup program or run the game the first time.
Normally the game will decompress the shapes file on the first run, but
it is decompressed automatically if the unpacked shapes file is missing.

The build scripts read directly from a game install via `--game-dir` 
(defaults to `./ULTIMA8`).

Python 3 + [Pillow](https://pillow.readthedocs.io/) (`pip install pillow`)
is used for building the graphics files and is the only dependency.  

Files used from the install:

| File | Folder | Used by |
| --- | --- | --- |
| `U8SHAPES.FLX` (or `U8SHAPES.CMP`) | `STATIC/` | `build_atlas.py`, `build_map.py` |
| `U8PAL.PAL` | `STATIC/` | `build_atlas.py`, `build_gumps.py`, `extract_fonts.py` |
| `TYPEFLAG.DAT` | `STATIC/` | `build_atlas.py`, `build_map.py` |
| `FIXED.DAT`, `GLOB.FLX` | `STATIC/` | `build_map.py` |
| `NONFIXED.DAT`, `ITEMCACH.DAT`, `NPCDATA.DAT` | `GAMEDAT/` | `build_map.py` |
| `GUMPAGE.DAT` | `STATIC/` | `build_map.py` |
| `U8GUMPS.FLX` | `STATIC/` | `build_gumps.py` |
| `U8FONTS.FLX` | `STATIC/` | `extract_fonts.py` |
| `MUSIC.FLX` | `SOUND/` | `extract_music.py` |
| `SOUND.FLX`, `<LANG><NNN>.FLX` | `SOUND/` | `extract_sounds.py` (speech files optional) |
| `[EFGJS]USECODE.FLX` | `USECODE/` | `parse_usecode.py`, `parse_schedules.py` |

The usecode tooling auto-detects the localized release — **E**nglish,
**F**rench, **G**erman, **J**apanese or **E**(spanish) — and pulls in-game text
(barks, dialogue, NPC names) in that language, decoded with the right codec
(CP437 / Shift-JIS). Spanish ships its usecode as `EUSECODE.FLX` too, so it's
told apart from English by content.

Repo-supplied helpers: `json/labels.json` (object names, provided by me and only 
English) and `json/mapnames.json` (friendly map names).

## Build everything

One command runs the whole pipeline from a single install:

```
python build_all.py          # atlas → gumps → fonts → audio → usecode → schedules → map
python -m http.server        # serve at http://localhost:8000/map.html
```

`build_all.py` just imports the individual scripts below and calls them in
order; each enrichment step is optional and skipped with a warning if its
game files are missing, so the build still completes.

Run the steps individually to rebuild only part of the pipeline (each is
one-time unless the underlying game files change):

```
python build_atlas.py        # → atlas.png + atlas.json (sprites)
python build_gumps.py        # → gumps.png + json/gumps.json (UI artwork)
python extract_fonts.py      # → fonts/   (bitmap fonts the viewer draws)
python extract_music.py      # → midi/    + json/music.json
python extract_sounds.py all # → sounds/  + json/speech.json
python parse_usecode.py      # → json/{barks,readables,dialog,locks}.json
python parse_schedules.py    # → json/{schedules,npc,npc_maps}.json
python build_map.py          # → maps/map_N.json[.gz] + map.html
```

`build_map.py` is the only step that's mandatory; the others enrich the
viewer (text, audio, schedules, …) and are skipped gracefully if absent.
Re-run `build_map.py` whenever you tweak the renderer, labels, or any of
the JSON inputs.

Every script accepts `--game-dir`:

```
python build_atlas.py    --game-dir /games/Ultima8
python parse_usecode.py  --game-dir /games/Ultima8
…
```

`extract_sounds.py` also takes `sfx` or `speech` to extract just one half;
`extract_fonts.py` takes `--all` for every font or `--font N` for one.

The viewer URL hash carries view state
(`#map=N&cx=<world-x>&cy=<world-y>&zoom=<scale>&sel=<row-idx>`), so
reloading or sharing a URL restores the exact same view.

## What each step does

**`build_atlas.py`** — from-scratch U8 shape decoder. Packs every 
`(shape, frame)` sprite into one paletted `atlas.png` with index 
255 reserved as transparent, plus a manifest of `[x,y,w,h]` rects.

**`build_gumps.py`** — same idea for U8's UI artwork (book pages, scrolls,
container backdrops). Drives the reading modal and container windows.

**`extract_fonts.py`** — decodes U8's bitmap fonts. Each FLX entry is one
font, each frame within it is one glyph keyed by ASCII code, so the same
RLE decoder reads them. Default run extracts the four faces the viewer 
draws with: font 6 ("Normal Red") for the on-map popup; fonts 1 / 10 / 
11 for book-scroll / plaque / tombstone reading modals. 
See `fonts/README.md` for the sheet layout.

**`extract_music.py`** — converts U8's XMIDI tracks to standard MIDI.
Files are named after their original `.xmi` (e.g. `056_tenebrae.mid`); 
each map's track is recovered from its shape-562 "music egg" objects 
and surfaced in `maps/index.json`, so the viewer's **Ambience** 
checkbox plays the right track per map.

**`extract_sounds.py`** — decodes U8's Sonarc-compressed audio to PCM WAV. 
Effects (`SOUND.FLX`) land in `sounds/sfx/` named `<idx>_<NAME>.wav`. 
Speech (per-conversation `E<NNN>.FLX` archives) land in 
`sounds/speech/E<NNN>/` with one wav per line. `json/speech.json` ties 
wavs to dialogue rows so titan / Guardian popups get speaker icons (`E80` 
Hydros, `E109` Pyros, `E385` Stratos, `E433` Lithos, `E44` Amoras, `E129` 
Odion, `E289` Khumash-Gor, `E597` Apathas, `E666` Guardian taunts).

**`parse_usecode.py`** — symbolic interpreter over the usecode bytecode
that recovers four text layers used by the viewer:

- `barks.json` — bark / look-at descriptions; drives the inspector and
  on-map selection popup.
- `readables.json` — full text of books, scrolls, tombstones and plaques.
- `dialog.json` — each NPC's conversation lines.
- `locks.json` — lock-id constants the key/lock classes.

If you skip this step, the popup falls back to the shape label and the
reading / dialogue modals are disabled.

**`u8_disasm.py`** — A pure-Python U8 usecode disassembler.

**`parse_schedules.py`** — extracts NPC schedule destinations from the
usecode and places them in `json/schedules.json`. It also writes `json/npc.json` 
(display names) and `json/npc_maps.json` (home maps). NPC names come straight 
from each character's look handler.

**`build_map.py`** — the main pipeline. Parses every binary format, runs
glob expansion, depth-sorts each map's render rows (reimplementation of
Pentagram's painter's-algorithm comparator + SCC bubble-sort), emits one
`maps/map_N.json` per map (with pre-gzipped `.json.gz` siblings for the
viewer's `DecompressionStream` fetch path), and writes a self-contained
`map.html` that renders the maps via 2D canvas.

## Viewer features

- **Pan / zoom** (mouse, touch + pinch); **z-slice sliders** to peel
  back floors / ceilings; **shape filter** with All / None and search.
- **Selection popup** shows the bark / label above any clicked object.
- **Reading modal** — books, scrolls, tombstones and plaques get a book
  icon; clicking it opens the matching gump as a backdrop with the text
  laid out in the appropriate U8 font. Books paginate across the spread.
- **Container windows** — chests, barrels, backpacks, bags, baskets,
  crates, drawers, dead bodies… open a draggable gump-backed window with
  contents laid out on the in-game grid (bounds from `GUMPAGE.DAT`).
  Containers nest; readables inside one still open the modal on top.
- **NPC dialogue popup** — flat browsable transcript of `dialog.json`,
  with speaker icons when `speech.json` is present. Not a playable
  conversation (U8's real dialogue is event/flag-gated).
- **Schedule overlay** — toggle NPC schedule to draw numbered waypoint
  pins around the selected NPC, with Prev/Next/Home nav across maps. A
  **time-of-day slider** below the nav steps through the six 4-hour
  blocks (Bloodwatch, Firstebb, Daytide, Threemoons, Lastebb, Eventide)
  and dims pins inactive in the current block; Prev/Next cycles only
  active waypoints.
- **Lock cross-link** — selecting a key, door or chest shows the matching
  counterpart in the inspector. Doors carry the lock id in `quality`'s
  low byte; chests carry it on a contained shape-756 Trap item. Click a
  row to jump (and open the chest gump on the matched item).
- **Search** — *Find spoken line* across every NPC and titan dialogue
  row; *Find book / scroll* across every readable, with a type filter.
- **Ambience (music)** — plays the MIDI for the loaded map, swapping
  tracks automatically when you switch maps.

## Implementation notes

- The viewer bakes the static (non-animated) layer into a grid of 1024 px
  tiles and only blits tiles that intersect the viewport; animations are
  clipped and redrawn per-frame against the cached tiles.
- Animation cycles (atypes 1–4 and 6) are ported from Pentagram's
  `Item::animateItem`. Atype 5 (usecode-driven) is a no-op here.
- The usecode tooling (`parse_usecode.py`, `parse_schedules.py`) walks the
  localized usecode FLX directly via `u8_disasm.py` — no pentagram/scummvm
  binary needed at build time.

## Known bugs

- **Z-order** — still one or two edge cases if you look really closely,
  otherwise some slight misalignment.
- **Mobile** — not too mobile-friendly.
- **Performance** — manageable on modern systems but still not great,
  especially with z-sliders moving.
