#!/usr/bin/env python3
"""One-shot build of the whole u8web pipeline from a single game install.

Imports the individual extractor/builder scripts and calls their entry points
in dependency order, so `python build_all.py [--game-dir DIR]` reproduces
everything the README lists step by step:

    atlas → gumps → fonts → music → sounds → usecode text → schedules → map

Only `build_map` is mandatory; every enrichment step is optional and is
skipped (with a warning) when the game files it needs are absent — the same
graceful degradation the viewer already relies on. Shape PNGs under `shapes/`
are produced by the external titan-ultima extractor, not by this script.
"""

import argparse
import sys
import traceback

import build_atlas
import build_gumps
import extract_fonts
import extract_music
import extract_sounds
import parse_usecode
import parse_schedules
import build_map

DEFAULT_GAME_DIR = build_map.DEFAULT_GAME_DIR


def _run(label, fn, *args, **kwargs):
    """Run one optional pipeline step, isolating its failures so a missing
    input or a decode error doesn't abort the whole build."""
    print(f"\n=== {label} ===", flush=True)
    try:
        fn(*args, **kwargs)
        return True
    except SystemExit as e:          # the scripts sys.exit() on missing inputs
        print(f"!! {label} skipped: {e}", file=sys.stderr)
    except Exception:
        print(f"!! {label} failed:", file=sys.stderr)
        traceback.print_exc()
    return False


def main(game_dir=DEFAULT_GAME_DIR):
    # Enrichment steps — each optional; a missing game file just skips it.
    _run("atlas (sprites)",    build_atlas.main, game_dir)
    _run("gumps (UI artwork)", build_gumps.main, game_dir)
    _run("fonts",              extract_fonts.main, game_dir)
    _run("music",              extract_music.main, game_dir)
    _run("sound effects",      extract_sounds.main, game_dir)
    _run("speech",             extract_sounds.extract_speech, game_dir)
    _run("usecode text",       parse_usecode.main, game_dir)
    _run("npc schedules",      parse_schedules.main, game_dir)

    # The map + viewer themselves are mandatory: let failures propagate.
    print("\n=== map + viewer ===", flush=True)
    build_map.build_all(game_dir=game_dir)
    print("\nBuild complete → map.html", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game-dir", dest="game_dir", default=DEFAULT_GAME_DIR,
                    help=f"Ultima VIII game directory (default: {DEFAULT_GAME_DIR})")
    main(**vars(ap.parse_args()))
