# u8web
Web-based map viewer for the classic DOS game Ultima VIII: Pagan.

<img width="1084" height="738" alt="image" src="https://github.com/user-attachments/assets/ca6f59f9-79de-4a9a-8e77-669ee37ab039" />

A browser map viewer that renders Ultima VIII almost the way the game does
— and __incredibly__ inefficiently. Started as an excuse to learn the file
formats, with a healthy assist from vibe-coding.

## Prerequisites

You need your own copy of Ultima VIII: Pagan. The build scripts read
directly from a game install via `--game-dir` (defaults to `./ULTIMA8`),
locating files case-insensitively inside `STATIC/`, `GAMEDAT/`, `SOUND/`
and `USECODE/`. Simplest setup:

```
ln -s /path/to/your/Ultima8  ./ULTIMA8
```

Otherwise pass `--game-dir /path/to/Ultima8` to every command.

Python 3 + [Pillow](https://pillow.readthedocs.io/) (`pip install pillow`)
is the only dependency. Files used from the install:

| File | Folder | Used by |
| --- | --- | --- |
| `U8SHAPES.FLX`, `U8PAL.PAL` | `STATIC/` | `build_atlas.py` |
| `FIXED.DAT`, `GLOB.FLX`, `TYPEFLAG.DAT` | `STATIC/` | `build_map.py` |
| `NONFIXED.DAT`, `ITEMCACH.DAT`, `NPCDATA.DAT` | `GAMEDAT/` | `build_map.py` |
| `U8GUMPS.FLX`, `GUMPAGE.DAT` | `STATIC/` | `build_gumps.py`, `build_map.py` |
| `U8FONTS.FLX` | `STATIC/` | `extract_fonts.py` |
| `MUSIC.FLX` | `SOUND/` | `extract_music.py` |
| `SOUND.FLX`, `E<NNN>.FLX` | `SOUND/` | `extract_sounds.py` (E-files optional) |
| `[EFGJS]USECODE.FLX` | `USECODE/` | `parse_usecode.py`, `parse_schedules.py` |

The usecode tooling auto-detects the localized release — **E**nglish,
**F**rench, **G**erman, **J**apanese or **S**panish — and pulls in-game text
(barks, dialogue, NPC names) in that language, decoded with the right codec
(CP437 / Shift-JIS). Spanish ships its usecode as `EUSECODE.FLX` too, so it's
told apart from English by content.

Repo-supplied helpers: `json/labels.json` (object names) and
`json/mapnames.json` (friendly map names).

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

Every script accepts `--game-dir` if you don't symlink:

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

**`build_atlas.py`** — from-scratch U8 shape decoder (RLE format, loosely
ported from ScummVM's `ultima8`). Packs every `(shape, frame)` sprite into
one paletted `atlas.png` (~6 MB, 4096×5735) with index 255 reserved as
transparent, plus a manifest of `[x,y,w,h]` rects.

**`build_gumps.py`** — same idea for U8's UI artwork (book pages, scrolls,
container backdrops). Drives the reading modal and container windows.

**`extract_fonts.py`** — decodes U8's bitmap fonts. Each FLX entry is one
font, each frame within it is one glyph keyed by ASCII code, so the same
RLE decoder reads them. The colour and black outline are baked into the
pixels. Default run extracts the four faces the viewer draws with: font 6
("Normal Red") for the on-map popup; fonts 1 / 10 / 11 for book-scroll /
plaque / tombstone reading modals. See `fonts/README.md` for the sheet
layout.

**`extract_music.py`** — converts U8's XMIDI tracks to standard MIDI (port
of Pentagram's `XMidiFile.cpp`: summed-byte delays, note-on-with-duration
→ synthesised note-offs, 120 ticks/sec). Files are named after their
original `.xmi` (e.g. `056_tenebrae.mid`); each map's track is recovered
from its shape-562 "music egg" objects and surfaced in `maps/index.json`,
so the viewer's **Ambience** checkbox plays the right track per map.

**`extract_sounds.py`** — decodes U8's Sonarc-compressed audio (port of
Pentagram's `SonarcAudioSample`) to PCM WAV. Effects (`SOUND.FLX`) land
in `sounds/sfx/` named `<idx>_<NAME>.wav`. Speech (per-conversation
`E<NNN>.FLX` archives) land in `sounds/speech/E<NNN>/` with one wav per
line. `json/speech.json` ties wavs to dialogue rows so titan / Guardian
popups get speaker icons (`E80` Hydros, `E109` Pyros, `E385` Stratos,
`E433` Lithos, `E44` Amoras, `E129` Odion, `E289` Khumash-Gor, `E597`
Apathas, `E666` Guardian taunts).

**`parse_usecode.py`** — symbolic interpreter over the usecode bytecode
that recovers four text layers used by the viewer:

- `barks.json` — bark / look-at descriptions; drives the inspector and
  on-map selection popup.
- `readables.json` — full text of books, scrolls, tombstones and plaques.
  Tombstones / plaques inline the read intrinsic under a `getQuality()`
  switch; books / scrolls dispatch into a shared library class whose
  per-quality functions hold the literal pages.
- `dialog.json` — each NPC's conversation lines. A non-monster NPC runs
  usecode class `objid + 1024`; the interpreter walks that class and
  collects `Item::bark` / `Item::ask` outputs grouped per function (≈
  one conversation branch). `UCMachine::getName` resolves to "Avatar".
- `locks.json` — lock-id constants the key/lock classes compare against
  `K_QUALITY`, used by the inspector's key↔chest cross-link.

If you skip this step, the popup falls back to the shape label and the
reading / dialogue modals are disabled.

**`parse_schedules.py`** — extracts NPC schedule destinations from the
usecode via `u8_disasm.py`, a pure-Python U8 usecode disassembler in this
repo (no external dependency). Each schedule handler branches on time-of-day
(a `FREE` method returning hour/4 = block 0..5) and quest globals, then
spawns a `GO_TO` pathfind method. Those two method offsets move on every
localized recompile, so rather than a per-language table the script
**fingerprints them from each build's own bytecode** (the pathfind spawn by
its 7-byte dest-tuple argument, the time helper by its 0..5 comparison). It
records each spawn's `(x,y,z,activity)` plus the time-block set its
surrounding branch is reachable in, and re-attributes cross-NPC spawns (e.g.
MORDEA driving Salkind and Aramina) to the correct owner → `json/schedules.json`.

It also writes `json/npc.json` (display names) and `json/npc_maps.json` (home
maps). NPC names come straight from each character's look handler in the
parsed language — proper names for the 30-odd characters, generic
descriptors ("guardsman", "Sorcerer") for the rest — so the labels match the
release instead of a hand-kept English list.

**`build_map.py`** — the main pipeline. Parses every binary format, runs
glob expansion, depth-sorts each map's render rows (reimplementation of
Pentagram's painter's-algorithm comparator + SCC bubble-sort), emits one
`maps/map_N.json` per map (with pre-gzipped `.json.gz` siblings for the
viewer's `DecompressionStream` fetch path), and writes a self-contained
`map.html` that renders the maps via 2D canvas. See `CLAUDE.md` for the
internal architecture.

## Viewer features

- **Pan / zoom** (mouse, touch + pinch); **z-slice sliders** to peel
  back floors / ceilings; **shape filter** with All / None and search.
- **Selection popup** in the 2× red font shows the bark / label above
  any clicked object.
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
