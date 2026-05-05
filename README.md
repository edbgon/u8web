# u8web
Web based map viewer for the classic DOS game Ultima VIII: Pagan

## Information
This is a fever dream -- some idea I had for some time, I wanted to get to know the formats of one of my old favorite games and with the advent of AI, why not vibe-code my way into something interesting.

Well, that's what this is... you can look at the maps from Ultima VIII, almost the way they were intended to be shown and __incredibly__ inefficiently!

I'm not sure how much more I will develop this, but as of now I can explain some features.

### Prerequisites
 - Unified.py - This will parse the following files in the "data" directory and create the map HTML. You'll need your own copy of U8 to get these files.
   - U8SHAPES.FLX - Shape information
   - FIXED.DAT - Fixed objects
   - NONFIXED.DAT - Objects that can be moved
   - GLOB.FLX - Globs of fixed objects (macros)
   - TYPEFLAG.DAT - Info about what kind of object an object is.
  
### Quick start
 - Clone repo.
 - Obtain needed files from U8.
 - Run python unified.py and wait until all maps are generated.
 - Start webserver (python -m http.server)
 - Open browser http://localhost:8000/map.html
 - Enjoy

labels.json (needs some review) and mapnames.json help give some names to the objects.

TBD: Shapes - right now I use [titan-ultima](https://github.com/theGreyWanderer-uc/tgwUltima/tree/main/titan-ultima) to extract shapes. I will want to do this as part of the script in the future. All shapes should be extracted as PNG into the shapes folder.

## KNOWN BUGS
 - Z-order - I still have some bugs with the ordering. TODO.
 - Centering - Some maps start you off in the middle of nowhere. TODO.
 - Efficiency - Wow this is really inefficient!
 - Mobile use - not too mobile friendly.
 - Fit and finish - needs polish, get rid of the AI stink.
