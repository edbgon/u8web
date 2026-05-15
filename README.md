# u8web
Web based map viewer for the classic DOS game Ultima VIII: Pagan

<img width="1084" height="738" alt="image" src="https://github.com/user-attachments/assets/ca6f59f9-79de-4a9a-8e77-669ee37ab039" />

## Information
This is a fever dream -- some idea I had for some time, I wanted to get to know the formats of one of my old favorite games and with the advent of AI, why not vibe-code my way into something interesting.

Well, that's what this is... you can look at the maps from Ultima VIII, almost the way they were intended to be shown and __incredibly__ inefficiently!

I'm not sure how much more I will develop this, but as of now I can explain some features.

### Prerequisites

You'll need your own copy of Ultima VIII: Pagan. Copy these six files from the game install into this repo's `data/` directory (flatten — they go directly into `data/`, not into subdirectories):

| Source in the game install | Used for |
| --- | --- |
| `STATIC/U8SHAPES.FLX` | Sprite pixel data (decoded by `build_atlas.py`) |
| `STATIC/U8PAL.PAL`    | 256-colour palette for shape decoding |
| `STATIC/FIXED.DAT`    | Fixed objects |
| `STATIC/GLOB.FLX`     | Glob macros (groups of fixed objects) |
| `STATIC/TYPEFLAG.DAT` | Per-shape flags / footprint dimensions |
| `GAMEDAT/NONFIXED.DAT` | Movable objects (note: `GAMEDAT`, not `STATIC`) |

Resulting layout:

```
data/
  U8SHAPES.FLX
  U8PAL.PAL
  FIXED.DAT
  GLOB.FLX
  TYPEFLAG.DAT
  NONFIXED.DAT
```

Used files:
 - `json/labels.json` labels for object names, needs some tweaking but is fairly descriptive.
 - `json/mapnames.json` provide friendly names for objects and maps.

You also need Python 3 with [Pillow](https://pillow.readthedocs.io/) installed (`pip install pillow`) for the atlas build step.

### Quick start

1. Clone the repo.
2. Copy the six game files into `data/` as shown above.
3. **Build the sprite atlas** (one-time, slow — only re-run if the game files change):
   ```
   python build_atlas.py
   ```
   This decodes every shape from `U8SHAPES.FLX`, packs them into a single
   `atlas.png` (ca. 14 MB, 4096×5700) and writes an `atlas.json` manifest.
4. **Generate the maps and viewer HTML**:
   ```
   python unified.py
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

### Implementation notes

- `build_atlas.py` is a from-scratch U8 shape decoder (RLE format ported loosely from ScummVM's `ultima8` engine). It used to depend on [titan-ultima](https://github.com/theGreyWanderer-uc/tgwUltima/tree/main/titan-ultima) for PNG extraction; that's no longer required.
- The viewer uses a single offscreen canvas as a baked static cache, then per-frame clips and redraws just the bbox of each animation against the cache. Painter order is preserved without re-rendering thousands of sprites every frame.
- Animation cycles (atypes 1–4 and 6) are ported from Pentagram's `Item::animateItem`. Atype 5 (usecode-driven) is a no-op here.

## KNOWN BUGS
 - Z-order - I still have some bugs with the ordering. TODO.
 - Centering - Some maps start you off in the middle of nowhere. TODO.
 - Mobile use - not too mobile friendly.
 - Fit and finish - needs polish, get rid of the AI stink.
