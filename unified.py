"""
build_map.py  –  Ultima 8 unified map builder

Reads U8SHAPES.FLX, FIXED.DAT, NONFIXED.DAT, GLOB.FLX, TYPEFLAG.DAT and writes:
  maps/index.json    – list of available map indices
  maps/map_N.json    – per-map render objects
  map.html           – Map viewer

Requires:
  json/labels.json      - Object names
  shapes/xxxx_fyyyy.png - All U8 shapes in PNG format
    Shapes can be extracted with this awesome project:
    https://github.com/theGreyWanderer-uc/tgwUltima/tree/main/titan-ultima

"""

import struct
import json
import collections
from functools import cmp_to_key
from pathlib import Path

# ──────────────────────────────────────────────
# Low-level readers
# ──────────────────────────────────────────────
def u8(d, o):  return d[o]
def u16(d, o): return struct.unpack_from("<H", d, o)[0]
def i16(d, o): return struct.unpack_from("<h", d, o)[0]
def u24(d, o): return d[o] | (d[o+1] << 8) | (d[o+2] << 16)
def u32(d, o): return struct.unpack_from("<I", d, o)[0]
def load(path):
    with open(path, "rb") as f:
        return f.read()

# ──────────────────────────────────────────────
# Shape info  (U8SHAPES.FLX)
# ──────────────────────────────────────────────
def parse_shapes(path):
    data  = load(path)
    count = u32(data, 84)
    tbl   = 144
    result = {}
    for i in range(count):
        off = u32(data, tbl + i * 8)
        ln  = u32(data, tbl + i * 8 + 4)
        if off == 0 or ln == 0:
            continue
        base   = off
        n_frm  = u16(data, base + 4)
        fhbase = base + 6
        frames = []
        for j in range(n_frm):
            fh       = fhbase + j * 6
            frm_off  = u24(data, fh)
            frm_base = base + frm_off
            if frm_base + 18 > len(data):
                continue
            frm_idx = u16(data, frm_base + 2)
            frames.append({
                "fi": frm_idx if frm_idx != 0 else j,
                "sx": i16(data, frm_base + 10),
                "sy": i16(data, frm_base + 12),
                "ox": i16(data, frm_base + 14),
                "oy": i16(data, frm_base + 16),
            })
        result[i] = frames
    return result

# ──────────────────────────────────────────────
# Object parsers
# ──────────────────────────────────────────────
OBJ_FMT  = "<HHBHBHHBBH"
OBJ_SIZE = 16

def parse_objects(data, offset, length, include_glob=False):
    objects = []
    for i in range(length // OBJ_SIZE):
        base = offset + i * OBJ_SIZE
        (x, y, z, shape, frame, _f, glob, _n, _m, _x2) = \
            struct.unpack_from(OBJ_FMT, data, base)
        obj = {"x": x, "y": y, "z": z, "s": shape, "f": frame}
        if include_glob and shape == 2:
            obj["g"] = glob
        objects.append(obj)
    return objects

def parse_typeflags(path):
    data = load(path)
    result = {}
    record_size = 8
    count = len(data) // record_size
    for i in range(count):
        base = i * record_size
        b0 = u8(data, base + 0)
        b1 = u8(data, base + 1)
        b2 = u8(data, base + 2)
        b3 = u8(data, base + 3)
        b4 = u8(data, base + 4)
        b5 = u8(data, base + 5)
        translucent    = (b1 >> 3) & 1
        size_x         = (b2 >> 4) & 15
        size_y         = (b3 >> 0) & 15
        size_z         = (b3 >> 4) & 15
        animation_type = (b4 >> 0) & 15
        animation_data = (b4 >> 4) & 15
        hide_in_game   = (b5 >> 4) & 1

        draw     = (b1 >> 0) & 1
        solid    = (b0 >> 1) & 1
        occluding= (b0 >> 4) & 1

        entry = {}
        if translucent:    entry["translucent"]   = True
        if animation_type: entry["animationType"] = animation_type
        if animation_data: entry["animationData"] = animation_data
        if hide_in_game:   entry["hideInGame"]    = True
        if draw:           entry["draw"]          = True
        if solid:          entry["solid"]         = True
        if occluding:      entry["occl"]          = True

        is_ground_tile  = (max(size_x, 1) == 4 and max(size_y, 1) == 4)
        is_32x32_ground = (size_x == 4 and size_y == 4 and size_z == 0)

        entry["draw"] = 1 if draw else 0
        entry["xd"]     = size_x * 32
        entry["yd"]     = size_y * 32
        entry["zd"]     = size_z * 8

        entry["foot_x"] = size_x * 32
        entry["foot_y"] = size_y * 32
        # foot_z is the true z-dimension of the bounding box.
        # Zero IS legal – flat tiles have foot_z == 0.
        entry["foot_z"] = size_z * 8
        entry["flat"]   = 1 if size_z == 0 else 0
        entry["f32"]    = 1 if size_x == 4 and size_y == 4 and size_z == 0 else 0

        result[i] = entry
    return result

def parse_globs(path):
    data  = load(path)
    count = u32(data, 84)
    tbl   = 128
    globs = {}
    for i in range(count):
        off = u32(data, tbl + i * 8)
        ln  = u32(data, tbl + i * 8 + 4)
        if off == 0 or ln == 0:
            continue
        base      = off
        obj_count = u16(data, base)
        objs      = []
        ptr       = base + 2
        for _ in range(obj_count):
            x     = u8(data, ptr + 0)
            y     = u8(data, ptr + 1)
            z     = u8(data, ptr + 2)
            shape = u16(data, ptr + 3)
            frame = u8(data, ptr + 5)
            objs.append({"x": x, "y": y, "z": z, "s": shape, "f": frame})
            ptr += 6
        globs[i] = objs
    return globs

def expand_globs(objects, globs):
    out = []
    for obj in objects:
        if obj.get("g") is None:
            out.append(obj)
            continue
        glob_objs = globs.get(obj["g"])
        if not glob_objs:
            continue
        bx, by, bz = obj["x"], obj["y"], obj["z"]
        base_x = bx & ~0x1FF
        base_y = by & ~0x1FF
        for g in glob_objs:
            out.append({
                "x": g["x"] * 2 + base_x,
                "y": g["y"] * 2 + base_y,
                "z": bz + g["z"],
                "s": g["s"],
                "f": g["f"],
                "_glob": True
            })
    return out

# ──────────────────────────────────────────────
# FLX file readers
# ──────────────────────────────────────────────
def read_nonfixed(path):
    data  = load(path)
    count = u32(data, 84)
    tbl   = 144
    records = []
    for i in range(count):
        off = u32(data, tbl + i * 8)
        ln  = u32(data, tbl + i * 8 + 4)
        if off == 0 or ln == 0:
            continue
        records.append((i, off, ln))
    return data, records

def read_fixed(path):
    data  = load(path)
    count = u16(data, 84)
    tbl   = 128
    records = []
    for i in range(count):
        off = u32(data, tbl + i * 8)
        ln  = u32(data, tbl + i * 8 + 4)
        if off == 0 or ln == 0:
            continue
        records.append((i, off, ln))
    return data, records

# ──────────────────────────────────────────────
# Merge shape info into object list (in-place)
# ──────────────────────────────────────────────
def merge_shapes(objects, shape_info, typeflags):
    for obj in objects:
        frames = shape_info.get(obj["s"] - 2)
        if frames:
            fi    = obj["f"] % len(frames)
            frame = frames[fi]
            obj["sx_img"] = frame["sx"]
            obj["sy_img"] = frame["sy"]
            obj["ox"] = frame["ox"]
            obj["oy"] = frame["oy"]
        flags = typeflags.get(obj["s"])
        if flags:
            obj.update(flags)

# ──────────────────────────────────────────────
# Try to recreate depth-sort from pentagram
# ──────────────────────────────────────────────
def _make_sort_tuple(obj):
    """
    Layout (indices used by _cmp_tuple):
      0  x       1  y       2  z
      3  xleft   4  yfar    5  ztop
      6  flat    7  f32
      8  anim    9  transl  10 draw  11 solid  12 occl
      13 shape   14 frame
    """
    x   = obj["x"];  y  = obj["y"];  z  = obj["z"]
    xd  = obj.get("xd",  32)
    yd  = obj.get("yd",  32)
    fz  = obj.get("foot_z", 0)        # true bounding-box z-height
    return (
        x, y, z,
        x - xd,                        # xleft
        y - yd,                        # yfar
        z + fz,                        # ztop
        1 if fz == 0 else 0,           # flat
        obj.get("f32", 0),
        1 if obj.get("animationType", 0) != 0 else 0,
        1 if obj.get("translucent")  else 0,
        1 if obj.get("draw")         else 0,
        1 if obj.get("solid")        else 0,
        1 if obj.get("occl")         else 0,
        obj["s"],
        obj["f"],
    )


def _cmp_tuple(a, b):
    """
    Pentagram-inspired isometric depth comparison
    Returns -1 if a should be drawn before (behind) b, +1 if b before a.
    """
    ax, ay, az, axl, ayf, azt, af, af32, aa, atr, adr, aso, aoc, at, afr = a
    bx, by, bz, bxl, byf, bzt, bf, bf32, ba, btr, bdr, bso, boc, bt, bfr = b

    # --- Both flat ---
    if af and bf:
        if azt != bzt:  return -1 if azt < bzt else 1
        if aa  != ba:   return -1 if aa  < ba  else 1
        if atr != btr:  return -1 if atr < btr else 1
        if adr != bdr:  return -1 if adr > bdr else 1
        if aso != bso:  return -1 if aso > bso else 1
        if aoc != boc:  return -1 if aoc > boc else 1
        if af32 != bf32: return -1 if af32 > bf32 else 1
        # fall through to x/y separation
    else:
        # Clear z separation (strict — when items only TOUCH in z,
        # fall through to x/y separation which handles flush walls
        # supporting/under floors correctly)
        if azt <  bz:  return -1
        if bzt <  az:  return  1
        # Mixed flat/non-flat at the same z-base: flat draws first.
        # Matches Pentagram's land-tile-first convention — a flat
        # floor at z=N and a wall extending up from z=N should always
        # paint floor first, regardless of any x/y separation between
        # their footprints.
        if af != bf and az == bz:
            return -1 if af else 1

    # --- Clear x separation ---
    if ax <= bxl:  return -1
    if bx <= axl:  return  1

    # --- Clear y separation ---
    if ay <= byf:  return -1
    if by <= ayf:  return  1

    # --- Overlapping in all axes ---
    if az != bz:  return -1 if az < bz else 1

    if (azt + az) // 2 <= bz:  return -1
    if az >= (bzt + bz) // 2:  return  1

    if (ax + axl) // 2 <= bxl:  return -1
    if axl >= (bx + bxl) // 2:  return  1

    if (ay + ayf) // 2 <= byf:  return -1
    if ayf >= (by + byf) // 2:  return  1

    axy = ax + ay;  bxy = bx + by
    if axy != bxy:  return -1 if axy < bxy else 1

    aback = axl + ayf;  bback = bxl + byf
    if aback != bback:  return -1 if aback < bback else 1

    if ax  != bx:  return -1 if ax  < bx  else 1
    if ay  != by:  return -1 if ay  < by  else 1
    if at  != bt:  return -1 if at  < bt  else 1
    if afr != bfr: return -1 if afr < bfr else 1
    return 0


def topo_sort_objects(items):
    """
    Sort render-object wrappers ({"obj": {...}, "row": [...]}) into
    correct isometric paint order using the Pentagram comparator.
    """
    n = len(items)
    if n == 0:
        return items

    # 1. Initial sort by (z, x, y) — stable base for DFS traversal
    items.sort(key=lambda it: (it["obj"]["z"], it["obj"]["x"], it["obj"]["y"]))

    # 2. Pre-compute sort tuples and screen bboxes in one pass
    #    Screen bbox (iso_classic):
    #      sxleft  = xleft // 4 - y    // 4
    #      sxright = x     // 4 - yfar // 4
    #      sytop   = xleft // 8 + yfar // 8 - ztop
    #      sybot   = x     // 8 + y    // 8 - z
    si  = []   # sort tuples
    ss  = []   # screen bboxes (image rect: sxleft, sxright, sytop, sybot)
    # IMAGE bboxes are used for the overlap check (not 3D footprint bboxes)
    # because u8web shapes have images that often extend beyond their 3D
    # footprint (e.g. floor images include side-detail past the footprint).
    # Two items can overlap visually without their footprints overlapping
    # on screen — using the image rect catches those cases. The cmp itself
    # still uses world-coord footprint geometry to decide order.
    for it in items:
        t = _make_sort_tuple(it["obj"])
        si.append(t)
        row = it["row"]
        bx4, by4, sw, sh = row[0], row[1], row[9], row[10]
        sxl = bx4 // 4
        syt = by4 // 4
        ss.append((
            sxl,
            sxl + sw,    # sxright
            syt,
            syt + sh,    # sybot
        ))

    # 3. Sweep-line on screen-X to build dependency graph
    #    For each new item, prune the active set (items whose sxright has
    #    passed the current sxleft), then check screen-Y overlap with
    #    remaining active items and record dependency edges.
    deps   = [[] for _ in range(n)]
    sweep  = sorted(range(n), key=lambda i: ss[i][0])   # sort by sxleft
    active = []   # indices of items currently in the sweep window

    for idx in sweep:
        sxl_cur = ss[idx][0]
        # Prune items that no longer overlap on screen-X
        active = [a for a in active if ss[a][1] > sxl_cur]
        for other in active:
            if ss[idx][2] >= ss[other][3] or ss[other][2] >= ss[idx][3]:
                continue
            cr = _cmp_tuple(si[idx], si[other])
            if cr < 0:
                deps[other].append(idx)
            elif cr > 0:
                deps[idx].append(other)
        active.append(idx)

    # 4. Tarjan's SCC + intra-SCC stable ordering.
    #    The cmp is non-transitive in general 3D scenes, so the dep
    #    graph can contain cycles. Naive DFS topological sort would
    #    silently break a back-edge, producing an arbitrary "wrong"
    #    item at the end of each cycle. Tarjan's collapses each cycle
    #    into a strongly-connected component; SCCs are themselves a
    #    DAG that we emit in reverse-topo order (deps-first), and
    #    within each SCC we sort by (z, ztop, x, y) — a stable spatial
    #    heuristic that matches the cmp's intent in the common cases
    #    cycles arise from (mixed-z stacked items at building edges).
    indices  = [-1] * n
    lowlinks = [0]  * n
    on_stack = [False] * n
    tarjan_stack = []
    sccs = []
    next_index = [0]

    for v_root in range(n):
        if indices[v_root] != -1:
            continue
        # iterative Tarjan
        work = [(v_root, 0)]
        while work:
            v, di = work[-1]
            if indices[v] == -1:
                indices[v] = next_index[0]
                lowlinks[v] = next_index[0]
                next_index[0] += 1
                tarjan_stack.append(v)
                on_stack[v] = True
            d_list = deps[v]
            if di < len(d_list):
                work[-1] = (v, di + 1)
                w = d_list[di]
                if indices[w] == -1:
                    work.append((w, 0))
                elif on_stack[w]:
                    if indices[w] < lowlinks[v]:
                        lowlinks[v] = indices[w]
                continue
            # done with v's outgoing edges
            if lowlinks[v] == indices[v]:
                scc = []
                while True:
                    w = tarjan_stack.pop()
                    on_stack[w] = False
                    scc.append(w)
                    if w == v:
                        break
                sccs.append(scc)
            work.pop()
            if work:
                p, _ = work[-1]
                if lowlinks[v] < lowlinks[p]:
                    lowlinks[p] = lowlinks[v]

    # Tarjan emits SCCs in reverse topological order on the condensation
    # — exactly the paint order we want (deps-first). Inside each SCC
    # the cmp is non-transitive (that's why it's a cycle), but cmp_to_key
    # still gives a deterministic ordering that respects most pair
    # judgments, which combined with the cmp's flat-first-at-same-z
    # rule produces the right answer for floor-vs-walls, walls-vs-floors,
    # stairs, and other patterns.
    order = []
    for scc in sccs:
        if len(scc) == 1:
            order.append(scc[0])
            continue
        # Pre-sort by (z, !flat, x, y) — Pentagram-like initial order:
        # lower z first, flat-before-tall at equal z, then west/north
        # first. Then bubble-sort fix any adjacent pair the cmp says
        # is out of order; iterate until stable. cmp is non-transitive
        # in cycles so global cmp_to_key sort produces order-dependent
        # weirdness — local pass-based fixes converge to a sensible
        # order that respects each adjacent pair.
        cur = sorted(scc, key=lambda i: (si[i][2], 1 - si[i][6], si[i][0], si[i][1]))
        m = len(cur)
        for _ in range(m):  # bound passes to SCC size
            swapped = False
            for k in range(m - 1):
                a, b = cur[k], cur[k+1]
                if _cmp_tuple(si[a], si[b]) > 0:
                    cur[k], cur[k+1] = b, a
                    swapped = True
            if not swapped:
                break
        order.extend(cur)

    return [items[i] for i in order]

# ──────────────────────────────────────────────
# Build render objects
# ──────────────────────────────────────────────
def count_frames(shape_info, s):
    """Highest FLX frame index + 1 for shape s. Authoritative — sequential
    PNG scanning silently undercounts shapes whose extractor names files
    by FLX frame index (with gaps) or whose first frame isn't fi=0. The
    JS preloader .catch()'es PNGs that don't exist, so overcounting here
    is harmless; undercounting kills the animation entirely."""
    frames = shape_info.get(s - 2, [])
    return max((f["fi"] for f in frames), default=-1) + 1

_frame_count_cache = {}


def build_render_objects(objects, image_folder, shape_info):
    img_path = Path(image_folder)
    out = []
    for obj in objects:
        if obj["x"] < 3:
            continue
        s, f = obj["s"], obj["f"]
        filename = f"{s:04d}_f{f:04d}.png"
        if not (img_path / filename).exists():
            continue
        ox_ = obj.get("ox", 0)
        oy_ = obj.get("oy", 0)
        x, y, z = obj["x"], obj["y"], obj["z"]

        base_x4 = round((x - y) / 4 - ox_) * 4
        base_y4 = round((x + y) / 8 - z - oy_) * 4

        # ── iflags bitmask ──────────────────────────────────────────
        tr    = 1 if obj.get("translucent")  else 0
        hide  = 1 if obj.get("hideInGame")   else 0
        solid = 1 if obj.get("solid")        else 0
        occl  = 1 if obj.get("occl")         else 0
        draw  = 1 if obj.get("draw")         else 0
        atype = int(obj.get("animationType") or 0) & 0xF

        xd = obj.get("xd", 32)
        yd = obj.get("yd", 32)
        zd = obj.get("foot_z", 0)
        xd_enc = (xd // 32 - 1) & 0x3   # 32→0, 64→1, 96→2, 128→3
        yd_enc = (yd // 32 - 1) & 0x3
        zd_enc = (zd // 8)      & 0x7   # 0→0, 8→1, 16→2 …

        if obj.get("animationType"):
            if s not in _frame_count_cache:
                _frame_count_cache[s] = count_frames(shape_info, s)
            anim_frames = _frame_count_cache[s]
        else:
            anim_frames = 0

        adata = int(obj.get("animationData") or 0) & 0xF

        iflags = (
            tr
            | (hide  << 1)
            | (solid << 2)
            | (occl  << 3)
            | (draw  << 4)
            | (atype << 5)
            | (xd_enc << 9)
            | (yd_enc << 11)
            | (zd_enc << 13)
            | (adata << 16)
            | (anim_frames << 20)
        )

        info = {"x": x, "y": y, "z": z, "s": s, "f": f}
        if iflags:
            info["if"] = iflags

        if obj.get("g") is not None:
            info["g"] = obj["g"]

        row = [base_x4, base_y4, z, 0, s, f, 0, ox_, oy_, obj.get("sx_img", 0), obj.get("sy_img", 0)]
        if iflags:
            row[6] = iflags

        out.append({"obj": obj, "row": row})
    return out

# ──────────────────────────────────────────────
# Full pipeline
# ──────────────────────────────────────────────
def build_all(
    shapes_flx    = "./data/U8SHAPES.FLX",
    fixed_dat     = "./data/FIXED.DAT",
    nonfixed_dat  = "./data/NONFIXED.DAT",
    globs_dat     = "./data/GLOB.FLX",
    typeflag_dat  = "./data/TYPEFLAG.DAT",
    labels_json   = "./json/labels.json",
    image_folder  = "shapes",
    maps_dir      = "maps",
    output_html   = "map.html",
):
    maps_path = Path(maps_dir)
    maps_path.mkdir(exist_ok=True)
    print("Loading shape info…")
    shape_info = parse_shapes(shapes_flx)

    print("Loading type flags…")
    typeflags = parse_typeflags(typeflag_dat)
    combined = {}

    print("Parsing globs…")
    globs = parse_globs(globs_dat)

    print("Parsing fixed map info…")
    fdata, frecords = read_fixed(fixed_dat)
    FIXED_INDEX_BIAS = -2
    for idx, off, ln in frecords:
        real_idx = idx + FIXED_INDEX_BIAS
        if real_idx < 0: continue
        objs = parse_objects(fdata, off, ln, include_glob=True)
        objs = expand_globs(objs, globs)
        merge_shapes(objs, shape_info, typeflags)
        combined.setdefault(real_idx, []).extend(build_render_objects(objs, image_folder, shape_info))

    print("Parsing dynamic map info…")
    ndata, nrecords = read_nonfixed(nonfixed_dat)
    for idx, off, ln in nrecords:
        objs = parse_objects(ndata, off, ln, include_glob=False)
        merge_shapes(objs, shape_info, typeflags)
        combined.setdefault(idx, []).extend(build_render_objects(objs, image_folder, shape_info))

    print(f"Writing {len(combined)} map JSON files → {maps_dir}/")
    index = []

    #TEST_MAP = 1   # ← change to whatever map you want

    for map_idx, render_objs in sorted(combined.items()):
        #if map_idx != TEST_MAP:
        #    continue

        sorted_items = topo_sort_objects(render_objs)
        sorted_rows = []
        for z_idx, item in enumerate(sorted_items):
            row = item["row"]
            row[3] = z_idx
            sorted_rows.append(row)

        fname = f"map_{map_idx}.json"
        with open(maps_path / fname, "w", encoding="utf-8") as f:
            json.dump(sorted_rows, f, separators=(",", ":"))

        print(f"Wrote map {map_idx} to JSON")
        index.append(map_idx)

    with open(maps_path / "index.json", "w", encoding="utf-8") as f:
        json.dump(index, f, separators=(",", ":"))

    print("Loading labels…")
    with open(labels_json, "r", encoding="utf-8") as f:
        labels = json.load(f)
    mapnames_path = Path(labels_json).parent / "mapnames.json"
    mapnames = {}
    if mapnames_path.exists():
        with open(mapnames_path, "r", encoding="utf-8") as f:
            mapnames = json.load(f)

    # Per-frame (FLX index, ox, oy) entries for shapes that animate. Sent to
    # the viewer so each frame draws at its own hot-spot. We filter against
    # atlas.json so the viewer only references sprites we actually packed —
    # FLX has 1×1 placeholder frames that we skip when building the atlas.
    atlas_frames = set()
    atlas_path = Path("atlas.json")
    if atlas_path.exists():
        with open(atlas_path) as f:
            atlas_frames = set(json.load(f).get("frames", {}).keys())
    else:
        print("WARNING: atlas.json missing — run build_atlas.py first")
    anim_anchors = {}
    for s_id, tf in typeflags.items():
        if not tf.get("animationType"):
            continue
        frames = shape_info.get(s_id - 2, [])
        valid = []
        for f in frames:
            fi = f["fi"]
            if f"{s_id}_{fi}" in atlas_frames:
                valid.append([fi, f["ox"], f["oy"]])
        if valid:
            anim_anchors[s_id] = valid

    print("Writing HTML…")
    write_html(index, labels, mapnames, image_folder, maps_dir, output_html, anim_anchors)
    print(f"Done → {output_html}")

# ──────────────────────────────────────────────
# HTML generator
# ──────────────────────────────────────────────
def write_html(index, labels, mapnames, image_folder, maps_dir, output_html, anim_anchors):
    labels_json = json.dumps(labels, separators=(",", ":"))
    mapnames_json = json.dumps({int(k): v for k, v in mapnames.items()}, separators=(",", ":"))
    anim_anchors_json = json.dumps({int(k): v for k, v in anim_anchors.items()}, separators=(",", ":"))
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Ultima 8 Map Viewer</title>

<style>
body{{margin:0;overflow:hidden;background:#1a1a1a;color:#ddd;font-family:monospace}}

.ui{{
  position:fixed;top:10px;left:10px;width:280px;
  background:rgba(0,0,0,0.85);
  padding:12px;border-radius:6px;
  font-size:12px
}}

.viewport{{width:100vw;height:100vh;overflow:hidden;cursor:grab}}
canvas{{display:block}}

button{{padding:1px 4px;font-size:10px;margin-left:2px}}

#shapeList{{
  max-height:350px;
  overflow:auto;
  margin-top:6px;
}}

pre{{
  font-size:10px;
  background:#111;
  padding:4px;
  max-height:150px;
  overflow:auto;
}}

input[type=range]{{
  width:100%;
  margin-bottom:6px;
}}

#search{{
  margin-top:6px;
  margin-bottom:8px;
  width:100%;
  box-sizing:border-box;
}}

.shape-row-active{{
  background:#333;
}}
</style>
</head>

<body>

<div class="ui">
Map: <select id="mapSel"></select><br>

<button id="btnAll">All</button>
<button id="btnNone">None</button>

<label><input type="checkbox" id="hideInternal" checked> Hide hidden objs</label>

<div style="margin-top:8px">
Z max:<span id="zMaxLbl"></span>
<input type="range" id="zMax">

Z min:<span id="zMinLbl"></span>
<input type="range" id="zMin">
</div>

Zoom: <span id="zoomLbl">1.00</span>
<button id="btnResetZoom">Reset</button>

<pre id="info"></pre>

<input id="search" placeholder="filter shapes">

<div id="shapeList"></div>
</div>

<div class="viewport" id="vp">
<canvas id="cv"></canvas>
</div>

<script>
const LABELS={labels_json};
const MAPNAMES={mapnames_json};
// shape_id → [[ox,oy], ...] per FLX sequential frame index, only for shapes
// that animate. Lets each anim frame draw at its own hot-spot.
const ANIM_ANCHORS={anim_anchors_json};
const MAP_INDEX={json.dumps(index)};
const MAPS_DIR="{maps_dir}";
const IMG="{image_folder}/";

// Atlas: one big PNG containing every shape/frame sprite. Far better than
// fetching each PNG individually — large maps used to ask for 10k+ tiny
// requests and Chrome silently dropped many under connection pressure.
// atlas.json maps "shape_frame" → [sx, sy, sw, sh] sub-rect.
let ATLAS=null, ATLAS_FRAMES=null;
const atlasReady=(async()=>{{
  const [im,meta]=await Promise.all([
    (async()=>{{ const i=new Image(); i.src="atlas.png"; await i.decode(); return i; }})(),
    fetch("atlas.json").then(r=>r.json()),
  ]);
  ATLAS=im;
  ATLAS_FRAMES=meta.frames;
}})();
function sprite(shp,fr){{
  const r=ATLAS_FRAMES[shp+"_"+fr];
  if(!r) return null;
  return {{sx:r[0],sy:r[1],width:r[2],height:r[3]}};
}}
function blit(c,spr,dx,dy){{
  c.drawImage(ATLAS,spr.sx,spr.sy,spr.width,spr.height,dx,dy,spr.width,spr.height);
}}

const $=id=>document.getElementById(id);
const canvas=$("cv");
const ctx=canvas.getContext("2d");
const vp=$("vp");

var mapReady=false;
function resize(){{canvas.width=innerWidth;canvas.height=innerHeight;if(mapReady){{clampPan();render();}}}}
addEventListener("resize",resize);resize();

let imgs=[],shapeMap=new Map(),shapeIds=[],enabled=new Set();
let selected=null;
let animTick=0,animTimer=null;

// Returns the current animation frame plus a (dx,dy) shift that re-anchors
// it to the world point. The shift is the difference between the base
// frame's hot-spot and the current frame's hot-spot; when no anim is active
// (or when the current frame happens to be the base frame), the shift is 0.
function pickFrame(o){{
  if(!o.animFrames||!o.animTotal) return {{img:o.img,dx:0,dy:0}};
  const n=o.animTotal;
  const idx=(((o.curFrame||0)%n)+n)%n;
  const img=o.animFrames[idx]||o.img;
  let dx=0,dy=0;
  const a=o.animAnchors&&o.animAnchors[idx];
  if(a){{ dx=o.ox-a[0]; dy=o.oy-a[1]; }}
  return {{img,dx,dy}};
}}

// Per-tick animation update, ported from Pentagram's Item::animateItem.
// atype 5 (usecode) is a no-op here — we can't run U8 usecode.
function tickAnimation(o){{
  if(!o.atype||!o.animFrames||!o.animTotal) return;
  const total=o.animTotal;
  const ad=o.adata|0;
  const bit=()=>Math.random()<0.5;        // rs.getRandomBit()
  const ri=n=>Math.floor(Math.random()*(n+1)); // rs.getRandomNumber(n) → 0..n inclusive
  let f=o.curFrame|0;
  switch(o.atype){{
    case 2:
      if(bit()) f=ri(total-1);
      break;
    case 1:
    case 3:
      if(ad===0||(ad===1&&bit())){{
        f++; if(f>=total) f=0;
      }} else if(ad>1){{
        f++;
        const num=Math.floor((f-1)/ad);
        if(f===(num+1)*ad) f=num*ad;
      }}
      break;
    case 4:
      if(f||ri(ad+1)===0){{
        f++; if(f>=total) f=0;
      }}
      break;
    case 6:
      if(ad===0||(ad===1&&bit())){{
        if(f){{
          f++; if(f>=total) f=1;
        }}
      }} else if(ad>1){{
        if(f%ad!==0){{
          f++;
          const num=Math.floor((f-1)/ad);
          if(f===(num+1)*ad) f=num*ad+1;
        }}
      }}
      break;
  }}
  o.curFrame=f;
}}
let ox=0,oy=0,scale=1;
let mapBBox=null;   // {{x0,y0,x1,y1}} in world coords — union of all object rects on current map
let jumpIndex=new Map();

// Keep at least PAN_MARGIN px of the map's screen bbox inside the viewport.
// If the map fits entirely on-screen with room to spare, the clamp range
// inverts and we skip clamping that axis (lets the user pan freely when
// zoomed way out).
function clampPan(){{
  if(!mapBBox) return;
  const m=100;
  const minOx=m-mapBBox.x1*scale;
  const maxOx=innerWidth-m-mapBBox.x0*scale;
  if(minOx<=maxOx) ox=Math.min(maxOx,Math.max(minOx,ox));
  const minOy=m-mapBBox.y1*scale;
  const maxOy=innerHeight-m-mapBBox.y0*scale;
  if(minOy<=maxOy) oy=Math.min(maxOy,Math.max(minOy,oy));
}}

// Coalesce render requests to one per animation frame. High-Hz mice fire
// pointermove 200+ times/sec; without coalescing, we'd run the full render
// loop on every event instead of once per repaint.
let renderRaf=0;
function scheduleRender(){{
  if(renderRaf) return;
  renderRaf=requestAnimationFrame(()=>{{renderRaf=0;render();}});
}}
let animatedImgs=[];
// Single offscreen cache containing the full map (anims included, with their
// frame 0 baked in). Each frame we clip to each anim's bbox, clear it, and
// redraw the column of imgs that overlap it in sort order using the anim's
// current frame. Cost per frame: 1 cache blit + sum(coverList) drawImages,
// which is tiny because anim bboxes are small and the cover lists average
// 10-50 imgs each.
let staticCanvas=null;
let staticDirty=true;
function invalidateStatic(){{staticDirty=true;scheduleRender();}}
// While the z slider is being dragged, skip the cache entirely — each
// rebuild costs ~36k drawImage calls, which stalls the slider visibly.
// Live rendering iterates the same objects but lets cull eliminate them
// when zoomed in. After 150ms with no slider input, rebuild once and
// switch back to the fast cached path.
let liveZ=false;
let liveZTimer=0;
function onZSlider(){{
  liveZ=true;
  clearTimeout(liveZTimer);
  liveZTimer=setTimeout(()=>{{liveZ=false;staticDirty=true;scheduleRender();}},150);
  scheduleRender();
}}

let dragging=false,moved=false,startX=0,startY=0;

const zMaxSl=$("zMax"),zMinSl=$("zMin");
const zMaxLbl=$("zMaxLbl"),zMinLbl=$("zMinLbl");
const info=$("info");

zMaxSl.oninput=onZSlider;
zMinSl.oninput=onZSlider;

MAP_INDEX.forEach(i=>{{
  const o=document.createElement("option");
  const displayNum=i+2;
  const name=MAPNAMES[displayNum];
  o.value=i;
  o.textContent=name?"Map "+displayNum+": "+name:"Map "+displayNum;
  $("mapSel").appendChild(o);
}});

$("mapSel").onchange=()=>{{ location.hash="map="+(+$("mapSel").value); loadMap(+$("mapSel").value); }};
window.addEventListener("hashchange",()=>{{
  const idx=parseMapHash();
  if(idx!=null && idx!==+$("mapSel").value){{
    $("mapSel").value=idx;
    loadMap(idx);
  }}
}});
function parseMapHash(){{
  const m=/[#&]map=(\d+)/.exec(location.hash);
  if(!m) return null;
  const idx=+m[1];
  return MAP_INDEX.includes(idx)?idx:null;
}}

$("btnResetZoom").onclick = () => {{
  const mx = innerWidth / 2, my = innerHeight / 2;
  const wx = (mx - ox) / scale, wy = (my - oy) / scale;
  scale = 1;
  ox = mx - wx;
  oy = my - wy;
  clampPan();
  $("zoomLbl").textContent = "1.00";
  scheduleRender();
}};

function syncCheckboxes() {{
  $("shapeList").querySelectorAll("div").forEach(row => {{
    const shp = +row.dataset.shp;
    const cb = row.querySelector("input[type=checkbox]");
    if (cb) cb.checked = enabled.has(shp);
  }});
}}

async function loadMap(idx){{
  selected=null;
  $("info").innerHTML="";
  // Cache-bust: python -m http.server has no cache headers, so browsers
  // happily serve a stale map JSON across regenerations. The Date.now()
  // suffix forces a fresh fetch on each page load.
  const res=await fetch(MAPS_DIR+"/map_"+idx+".json?t="+Date.now());
  const objs=await res.json();

  await atlasReady;

  // Sprites are sub-rects of the shared atlas image. Lookup is synchronous;
  // unknown (shape,frame) pairs (titan-ultima sometimes skips empty frames)
  // yield null, and we drop those objects just like the old loader did.
  imgs=objs.map(([bx4,by4,z,dep,shp,fr,ifl=0,ox=0,oy=0,sw=0,sh=0])=>{{
    const im=sprite(shp,fr);
    if(!im) return null;

    const tr    =  ifl        & 1;
    const hide  = (ifl >> 1)  & 1;
    const solid = (ifl >> 2)  & 1;
    const occl  = (ifl >> 3)  & 1;
    const draw  = (ifl >> 4)  & 1;
    const atype = (ifl >> 5)  & 0xF;
    const xd    = ((ifl >> 9)  & 0x3) * 32 + 32;
    const yd    = ((ifl >> 11) & 0x3) * 32 + 32;
    const zd    = ((ifl >> 13) & 0x7) * 8;
    const adata = (ifl >> 16) & 0xF;
    const anim  =  ifl >>> 20;

    return {{
      img:im,
      x:bx4/4, y:by4/4,
      z, dep, shp, fr,
      ox, oy,
      sw, sh,
      hide, tr, solid, occl, draw, atype, adata,
      xd, yd, zd, anim,
      curFrame: fr,
      w:im.width, h:im.height
    }};
  }}).filter(o=>o!==null);

  // Preload animation frames for any atype that advances frames
  // (1,2,3,4,6 — atype 5 is usecode-driven, skipped). ANIM_ANCHORS lists
  // only FLX frame indices whose PNGs exist on disk, so we drive both the
  // preload URL and the anchor table from the same source.
  // Atlas lookup is synchronous, so there's nothing to await — we just
  // resolve each anim shape's frames to sprite descriptors in-place.
  const shapePreload=new Map();
  for(const o of imgs){{
    if(![1,2,3,4,6].includes(o.atype)) continue;
    const entries=ANIM_ANCHORS[o.shp];
    if(!entries||entries.length<2||shapePreload.has(o.shp)) continue;
    shapePreload.set(o.shp,entries.map(e=>sprite(o.shp,e[0])));
  }}
  // Build per-shape FLX-indexed frame/anchor arrays. Pentagram's animation
  // logic (tickAnimation) walks frame numbers in FLX-index space, so we must
  // index by FLX frame, not by position in the preloaded list — otherwise
  // a shape that holds several variants (e.g. multi-colored candles) will
  // hop between them as curFrame increments across positional slots.
  const shapeBuild=new Map();
  for(const [shp,arr] of shapePreload){{
    const entries=ANIM_ANCHORS[shp];
    let maxFi=0,have=0;
    const af=[],aa=[];
    for(let j=0;j<arr.length;j++){{
      const fi=entries[j][0];
      if(fi>maxFi) maxFi=fi;
      if(arr[j]){{
        af[fi]=arr[j];
        aa[fi]=[entries[j][1],entries[j][2]];
        have++;
      }}
    }}
    if(have>1) shapeBuild.set(shp,{{af,aa,total:maxFi+1}});
  }}
  for(const o of imgs){{
    const b=shapeBuild.get(o.shp);
    if(!b) continue;
    o.animFrames=b.af;
    o.animAnchors=b.aa;
    o.animTotal=b.total;
  }}

  // Pre-cache per-object render data: image-rect right/bottom edges (for
  // the cull check) and the faded flag (translucent && !solid). Allocate
  // once at load time rather than recomputing every frame.
  for(const o of imgs){{
    o.x2=o.x+o.w;
    o.y2=o.y+o.h;
    o.faded=(o.tr&&!o.solid)?1:0;
  }}
  animatedImgs=imgs.filter(o=>o.animFrames);

  // Per anim: compute the bbox that encloses every frame it can draw, then
  // collect imgs (in painter sort order) whose image rect intersects that
  // bbox. Each frame we'll clip to this bbox, clear, and redraw the column.
  for(const o of animatedImgs){{
    let bx0=o.x,by0=o.y,bx1=o.x2,by1=o.y2;
    for(let j=0;j<o.animTotal;j++){{
      const f=o.animFrames[j], aa=o.animAnchors[j];
      if(!f||!aa) continue;
      const px=o.x+(o.ox-aa[0]);
      const py=o.y+(o.oy-aa[1]);
      if(px<bx0) bx0=px;
      if(py<by0) by0=py;
      if(px+f.width>bx1) bx1=px+f.width;
      if(py+f.height>by1) by1=py+f.height;
    }}
    o.animBx0=bx0; o.animBy0=by0;
    o.animBx1=bx1; o.animBy1=by1;
    const cover=[];
    for(const q of imgs){{
      if(q.x2>bx0 && q.x<bx1 && q.y2>by0 && q.y<by1) cover.push(q);
    }}
    o.coverList=cover;
  }}

  if(animTimer){{clearInterval(animTimer);animTimer=null;}}
  if(animatedImgs.length){{
    animTimer=setInterval(()=>{{
      animTick++;
      for(const o of animatedImgs) tickAnimation(o);
      scheduleRender();
    }},167);
  }}

  shapeMap=new Map();
  for(const o of imgs){{
    if(!shapeMap.has(o.shp))shapeMap.set(o.shp,[]);
    shapeMap.get(o.shp).push(o);
  }}

  shapeIds=[...new Set(imgs.map(i=>i.shp))].sort((a,b)=>a-b);
  enabled=new Set(shapeIds);

  const zs=imgs.map(i=>i.z);
  const mn=Math.min(...zs),mx=Math.max(...zs);

  zMaxSl.min=zMinSl.min=mn;
  zMaxSl.max=zMinSl.max=mx;
  zMaxSl.value=mx;
  zMinSl.value=mn;

  // Compute map bbox in world coords from object screen rects, then center
  // the viewport on it so map changes never drop us into empty space.
  if(imgs.length){{
    let x0=Infinity,y0=Infinity,x1=-Infinity,y1=-Infinity;
    for(const o of imgs){{
      if(o.x<x0) x0=o.x;
      if(o.y<y0) y0=o.y;
      if(o.x+o.w>x1) x1=o.x+o.w;
      if(o.y+o.h>y1) y1=o.y+o.h;
    }}
    mapBBox={{x0,y0,x1,y1}};
    const cx=(x0+x1)/2, cy=(y0+y1)/2;
    ox=innerWidth/2-cx*scale;
    oy=innerHeight/2-cy*scale;
  }} else {{
    mapBBox=null;
  }}

  buildList("");
  mapReady=true;
  staticCanvas=null;
  staticDirty=true;
  render();
}}

// Repaint the offscreen static cache. Called lazily on first render after
// a filter (z slider, shape checkbox, hideInternal) changes — pan/zoom
// reuse the existing cache.
function rebuildStatic(){{
  staticDirty=false;
  if(!mapBBox){{staticCanvas=null;return;}}
  const w=Math.ceil(mapBBox.x1-mapBBox.x0);
  const h=Math.ceil(mapBBox.y1-mapBBox.y0);
  if(!staticCanvas||staticCanvas.width!==w||staticCanvas.height!==h){{
    staticCanvas=document.createElement("canvas");
    staticCanvas.width=w;
    staticCanvas.height=h;
  }}
  const sctx=staticCanvas.getContext("2d");
  sctx.setTransform(1,0,0,1,0,0);
  sctx.clearRect(0,0,w,h);
  sctx.setTransform(1,0,0,1,-mapBBox.x0,-mapBBox.y0);

  const hi=+zMaxSl.value,lo=+zMinSl.value;
  const hideInt=$("hideInternal").checked;
  let a=1;
  sctx.globalAlpha=1;
  for(const o of imgs){{
    if(o.z>hi||o.z<lo) continue;
    if(!enabled.has(o.shp)) continue;
    if(o.hide&&hideInt) continue;
    const wa=o.faded?0.4:1;
    if(wa!==a){{sctx.globalAlpha=wa;a=wa;}}
    // Bake the object's starting frame into the cache (it's what shows
    // before any animation tick advances). Fall back to o.img if that FLX
    // slot is missing — passing undefined to drawImage would throw and
    // abort the rest of the cache rebuild loop.
    const af=o.animFrames&&o.animFrames[o.fr];
    blit(sctx,af||o.img,o.x,o.y);
  }}
}}

function render(){{
  const hi=+zMaxSl.value,lo=+zMinSl.value;
  zMaxLbl.textContent=hi;
  zMinLbl.textContent=lo;

  if(staticDirty&&!liveZ) rebuildStatic();

  ctx.setTransform(1,0,0,1,0,0);
  ctx.clearRect(0,0,canvas.width,canvas.height);
  ctx.setTransform(scale,0,0,scale,ox,oy);

  ctx.globalAlpha=1;
  const hideInt=$("hideInternal").checked;
  const vx0=-ox/scale, vy0=-oy/scale;
  const vx1=vx0+canvas.width/scale, vy1=vy0+canvas.height/scale;

  if(liveZ){{
    // Slider-drag fallback: walk every img each frame.
    let a=1;
    for(const o of imgs){{
      if(o===selected) continue;
      if(o.z>hi||o.z<lo) continue;
      if(!enabled.has(o.shp)) continue;
      if(o.hide&&hideInt) continue;
      if(o.x2<vx0||o.x>vx1||o.y2<vy0||o.y>vy1) continue;
      const wa=o.faded?0.4:1;
      if(wa!==a){{ctx.globalAlpha=wa;a=wa;}}
      if(o.animFrames){{
        const f=pickFrame(o);
        blit(ctx,f.img,o.x+f.dx,o.y+f.dy);
      }} else {{
        blit(ctx,o.img,o.x,o.y);
      }}
    }}
  }} else if(staticCanvas){{
    ctx.drawImage(staticCanvas,mapBBox.x0,mapBBox.y0);
    // For each animation: clip to its bbox, clear it, and redraw its column
    // of overlapping imgs in painter sort order — using the anim's current
    // frame. This preserves occlusion (a roof tile in coverList that sorts
    // after the anim still paints over the anim) without touching the rest
    // of the map.
    for(const A of animatedImgs){{
      const bx0=A.animBx0,by0=A.animBy0,bx1=A.animBx1,by1=A.animBy1;
      if(bx1<vx0||bx0>vx1||by1<vy0||by0>vy1) continue;
      if(A.z>hi||A.z<lo){{
        // Anim itself culled but its column may still need a repaint if it
        // was baked into the cache with frame 0. Cheaper to just clear+redraw.
      }}
      ctx.save();
      ctx.beginPath();
      ctx.rect(bx0,by0,bx1-bx0,by1-by0);
      ctx.clip();
      ctx.clearRect(bx0,by0,bx1-bx0,by1-by0);
      let a=1;
      ctx.globalAlpha=1;
      for(const o of A.coverList){{
        if(o===selected) continue;
        if(o.z>hi||o.z<lo) continue;
        if(!enabled.has(o.shp)) continue;
        if(o.hide&&hideInt) continue;
        const wa=o.faded?0.4:1;
        if(wa!==a){{ctx.globalAlpha=wa;a=wa;}}
        if(o.animFrames){{
          const f=pickFrame(o);
          blit(ctx,f.img,o.x+f.dx,o.y+f.dy);
        }} else {{
          blit(ctx,o.img,o.x,o.y);
        }}
      }}
      ctx.restore();
    }}
  }}

  ctx.globalAlpha=1;

  if(selected){{
    const selFaded=selected.tr&&!selected.solid;
    ctx.globalAlpha=selFaded?0.4:1;
    const f=pickFrame(selected);
    blit(ctx,f.img,selected.x+f.dx,selected.y+f.dy);
    ctx.lineWidth=2/scale;
    ctx.strokeStyle="#f55";
    ctx.strokeRect(selected.x+f.dx,selected.y+f.dy,f.img.width,f.img.height);
  }}

  ctx.globalAlpha=1;
}}

function isVisible(o){{
  const hi = +zMaxSl.value;
  const lo = +zMinSl.value;

  if (o.z > hi || o.z < lo) return false;
  if (!enabled.has(o.shp)) return false;
  if (o.hide && $("hideInternal").checked) return false;

  return true;
}}

function handleClick(e){{
  if (moved) return;

  const mx = (e.clientX - ox) / scale;
  const my = (e.clientY - oy) / scale;

  const hits = imgs
    .filter(isVisible)
    .sort((a,b) => b.dep - a.dep);

  for (const o of hits){{
    if (mx < o.x || mx > o.x + o.w) continue;
    if (my < o.y || my > o.y + o.h) continue;

    select(o);
    return;
  }}

  selected = null;
  info.textContent = "";
  scheduleRender();
}}

function select(o){{
  selected = o;
  const display = {{s: o.shp, f: o.fr, x: o.x, y: o.y, z: o.z, ox: o.ox, oy: o.oy, sw: o.sw, sh: o.sh}};
  if (o.tr)    display.translucent   = true;
  if (o.hide)  display.hideInGame    = true;
  if (o.solid) display.solid         = true;
  if (o.occl)  display.occl          = true;
  if (o.draw)  display.draw          = true;
  if (o.atype) display.animationType = o.atype;
  if (o.adata) display.animationData = o.adata;
  if (o.xd !== 32) display.xd = o.xd;
  if (o.yd !== 32) display.yd = o.yd;
  if (o.zd !== 8)  display.zd = o.zd;
  info.textContent = JSON.stringify(display, null, 2);

  scheduleRender();

  const rows=$("shapeList").querySelectorAll("div");
  rows.forEach(r=>r.classList.remove("shape-row-active"));

  const row=[...rows].find(r=>+r.dataset.shp===o.shp);
  if(row){{
    row.classList.add("shape-row-active");
    row.scrollIntoView({{block:"nearest"}});
  }}
}}

vp.onpointerdown=e=>{{
  if(e.pointerType==="touch") return;
  dragging=true;
  moved=false;
  startX=e.clientX;
  startY=e.clientY;
}};

vp.onpointermove=e=>{{
  if(e.pointerType==="touch"||!dragging) return;
  const dx=e.clientX-startX;
  const dy=e.clientY-startY;
  if(Math.abs(dx)>3||Math.abs(dy)>3) moved=true;
  if(moved){{
    ox+=dx; oy+=dy;
    clampPan();
    startX=e.clientX; startY=e.clientY;
    scheduleRender();
  }}
}};

vp.onpointerup=e=>{{
  if(e.pointerType==="touch") return;
  dragging=false;
  handleClick(e);
}};

vp.onwheel=e=>{{
  e.preventDefault();
  const f=1.1;
  const mx=e.clientX,my=e.clientY;
  const wx=(mx-ox)/scale,wy=(my-oy)/scale;
  scale=e.deltaY<0?scale*f:scale/f;
  scale=Math.max(0.2,Math.min(5,scale));
  ox=mx-wx*scale; oy=my-wy*scale;
  clampPan();
  $("zoomLbl").textContent=scale.toFixed(2);
  scheduleRender();
}},{{passive:false}};

let touches={{}};
let pinchDist=null;

function touchMidpoint(){{
  const pts=Object.values(touches);
  return {{
    x:(pts[0].x+pts[1].x)/2,
    y:(pts[0].y+pts[1].y)/2
  }};
}}
function touchDist(){{
  const pts=Object.values(touches);
  const dx=pts[0].x-pts[1].x, dy=pts[0].y-pts[1].y;
  return Math.hypot(dx,dy);
}}

vp.addEventListener("touchstart",e=>{{
  e.preventDefault();
  moved=false;
  for(const t of e.changedTouches)
    touches[t.identifier]={{x:t.clientX,y:t.clientY}};
  if(Object.keys(touches).length===2) pinchDist=touchDist();
}},{{passive:false}});

vp.addEventListener("touchmove",e=>{{
  e.preventDefault();
  const prev={{...touches}};
  for(const t of e.changedTouches)
    touches[t.identifier]={{x:t.clientX,y:t.clientY}};

  const count=Object.keys(touches).length;

  if(count===1){{
    const id=Object.keys(touches)[0];
    if(!prev[id]) return;
    const dx=touches[id].x-prev[id].x;
    const dy=touches[id].y-prev[id].y;
    if(Math.abs(dx)>2||Math.abs(dy)>2) moved=true;
    ox+=dx; oy+=dy;
    clampPan();
    scheduleRender();
  }} else if(count===2){{
    const prevPts=Object.values(prev).slice(0,2);
    const curPts=Object.values(touches).slice(0,2);
    if(prevPts.length<2||curPts.length<2) return;

    const prevMid={{x:(prevPts[0].x+prevPts[1].x)/2,y:(prevPts[0].y+prevPts[1].y)/2}};
    const curMid=touchMidpoint();

    ox+=curMid.x-prevMid.x;
    oy+=curMid.y-prevMid.y;

    const newDist=touchDist();
    if(pinchDist){{
      const ratio=newDist/pinchDist;
      const wx=(curMid.x-ox)/scale, wy=(curMid.y-oy)/scale;
      scale=Math.max(0.2,Math.min(5,scale*ratio));
      ox=curMid.x-wx*scale; oy=curMid.y-wy*scale;
      $("zoomLbl").textContent=scale.toFixed(2);
    }}
    clampPan();
    pinchDist=newDist;
    moved=true;
    scheduleRender();
  }}
}},{{passive:false}});

vp.addEventListener("touchend",e=>{{
  e.preventDefault();
  for(const t of e.changedTouches) delete touches[t.identifier];
  pinchDist=null;
  if(!moved&&e.changedTouches.length===1){{
    const t=e.changedTouches[0];
    handleClick({{clientX:t.clientX,clientY:t.clientY}});
  }}
}},{{passive:false}});

vp.addEventListener("touchcancel",e=>{{
  for(const t of e.changedTouches) delete touches[t.identifier];
  pinchDist=null;
}},{{passive:false}});

vp.onwheel=e=>{{
  e.preventDefault();
  const f=1.1;
  const mx=e.clientX,my=e.clientY;
  const wx=(mx-ox)/scale,wy=(my-oy)/scale;

  scale=e.deltaY<0?scale*f:scale/f;
  scale=Math.max(0.2,Math.min(5,scale));

  ox=mx-wx*scale;
  oy=my-wy*scale;
  clampPan();

  $("zoomLbl").textContent=scale.toFixed(2);
  scheduleRender();
}},{{passive:false}};

function buildList(filter){{
  const sl=$("shapeList");
  sl.innerHTML="";
  const q=filter.toLowerCase();

  shapeIds.forEach(shp=>{{
    const label = LABELS[shp]
      ? shp+": "+LABELS[shp]
      : "Shape "+shp;

    if(q && !label.toLowerCase().includes(q)) return;

    const row=document.createElement("div");
    row.dataset.shp=shp;

    const cb=document.createElement("input");
    cb.type="checkbox";
    cb.checked=enabled.has(shp);
    cb.onchange=()=>{{cb.checked?enabled.add(shp):enabled.delete(shp);invalidateStatic();}};

    const lbl=document.createElement("span");
    lbl.textContent=label;
    lbl.style.flex=1;

    const btn=document.createElement("button");
    btn.textContent="→";
    btn.style.fontSize="9px";
    btn.onclick=()=>jumpTo(shp);

    row.append(cb,lbl,btn);
    sl.appendChild(row);
  }});
}}

function jumpTo(shp){{
  const list=(shapeMap.get(shp)||[]);
  if(!list.length)return;

  list.sort((a,b)=>a.dep-b.dep);

  const i=(jumpIndex.get(shp)||0)%list.length;
  jumpIndex.set(shp,i+1);

  const o=list[i];

  ox=innerWidth/2-(o.x+o.w/2)*scale;
  oy=innerHeight/2-(o.y+o.h/2)*scale;
  clampPan();

  select(o);
}}

$("btnAll").onclick=()=>{{enabled = new Set(shapeIds);syncCheckboxes();invalidateStatic();}};
$("btnNone").onclick=()=>{{enabled.clear();syncCheckboxes();invalidateStatic();}};
$("hideInternal").onchange=invalidateStatic;
$("search").oninput=e=>buildList(e.target.value);

{{
  const initial=parseMapHash()??MAP_INDEX[0];
  $("mapSel").value=initial;
  loadMap(initial);
}}
</script>
</body>
</html>"""
    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html)

if __name__ == "__main__":
    build_all()