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
        hide_in_game   = (b5 >> 4) & 1

        draw     = (b1 >> 0) & 1
        solid    = (b0 >> 1) & 1
        occluding= (b0 >> 4) & 1

        entry = {}
        if translucent:    entry["translucent"]   = True
        if animation_type: entry["animationType"] = animation_type
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
def count_frames(img_path, s):
    """Count how many sequential frame PNGs exist for shape s."""
    count = 0
    while (img_path / f"{s:04d}_f{count:04d}.png").exists():
        count += 1
    return count

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
                _frame_count_cache[s] = count_frames(img_path, s)
            anim_frames = _frame_count_cache[s]
        else:
            anim_frames = 0

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
            | (anim_frames << 16)
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

    print("Writing HTML…")
    write_html(index, labels, mapnames, image_folder, maps_dir, output_html)
    print(f"Done → {output_html}")

# ──────────────────────────────────────────────
# HTML generator
# ──────────────────────────────────────────────
def write_html(index, labels, mapnames, image_folder, maps_dir, output_html):
    labels_json = json.dumps(labels, separators=(",", ":"))
    mapnames_json = json.dumps({int(k): v for k, v in mapnames.items()}, separators=(",", ":"))
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

<label><input type="checkbox" id="hideInternal"> Hide hidden objs</label>

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
const MAP_INDEX={json.dumps(index)};
const MAPS_DIR="{maps_dir}";
const IMG="{image_folder}/";

const $=id=>document.getElementById(id);
const canvas=$("cv");
const ctx=canvas.getContext("2d");
const vp=$("vp");

var mapReady=false;
function resize(){{canvas.width=innerWidth;canvas.height=innerHeight;if(mapReady)render()}}
addEventListener("resize",resize);resize();

let imgs=[],shapeMap=new Map(),shapeIds=[],enabled=new Set();
let selected=null;
let ox=0,oy=0,scale=1;
let jumpIndex=new Map();

let dragging=false,moved=false,startX=0,startY=0;

const zMaxSl=$("zMax"),zMinSl=$("zMin");
const zMaxLbl=$("zMaxLbl"),zMinLbl=$("zMinLbl");
const info=$("info");

zMaxSl.oninput=render;
zMinSl.oninput=render;

MAP_INDEX.forEach(i=>{{
  const o=document.createElement("option");
  const displayNum=i+2;
  const name=MAPNAMES[displayNum];
  o.value=i;
  o.textContent=name?"Map "+displayNum+": "+name:"Map "+displayNum;
  $("mapSel").appendChild(o);
}});

$("mapSel").onchange=()=>loadMap(+$("mapSel").value);

$("btnResetZoom").onclick = () => {{
  scale = 1;
  ox = 0;
  oy = 0;
  $("zoomLbl").textContent = "1.00";
  render();
}};

function syncCheckboxes() {{
  $("shapeList").querySelectorAll("div").forEach(row => {{
    const shp = +row.dataset.shp;
    const cb = row.querySelector("input[type=checkbox]");
    if (cb) cb.checked = enabled.has(shp);
  }});
}}

async function loadMap(idx){{
  const res=await fetch(MAPS_DIR+"/map_"+idx+".json");
  const objs=await res.json();

  imgs=(await Promise.all(objs.map(async ([bx4,by4,z,dep,shp,fr,ifl=0,ox=0,oy=0,sw=0,sh=0])=>{{
    const im=new Image();
    const file=`${{String(shp).padStart(4,"0")}}_f${{String(fr).padStart(4,"0")}}.png`;
    im.src=IMG+file;
    try{{
      await im.decode();
    }}catch{{
      return null;
    }}

    const tr    =  ifl        & 1;
    const hide  = (ifl >> 1)  & 1;
    const solid = (ifl >> 2)  & 1;
    const occl  = (ifl >> 3)  & 1;
    const draw  = (ifl >> 4)  & 1;
    const atype = (ifl >> 5)  & 0xF;
    const xd    = ((ifl >> 9)  & 0x3) * 32 + 32;
    const yd    = ((ifl >> 11) & 0x3) * 32 + 32;
    const zd    = ((ifl >> 13) & 0x7) * 8;
    const anim  =  ifl >> 16;

    return {{
      img:im,
      x:bx4/4, y:by4/4,
      z, dep, shp, fr,
      ox, oy,
      sw, sh,
      hide, tr, solid, occl, draw, atype,
      xd, yd, zd, anim,
      w:im.width, h:im.height
    }};
  }}))
  ).filter(o=>o!==null);

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

  buildList("");
  mapReady=true;
  render();
}}

function render(){{
  const hi=+zMaxSl.value,lo=+zMinSl.value;
  zMaxLbl.textContent=hi;
  zMinLbl.textContent=lo;

  ctx.setTransform(1,0,0,1,0,0);
  ctx.clearRect(0,0,canvas.width,canvas.height);
  ctx.setTransform(scale,0,0,scale,ox,oy);

  for(const o of imgs){{
    if(o===selected) continue;
    if(o.z>hi||o.z<lo)continue;
    if(!enabled.has(o.shp))continue;
    if(o.hide&&$("hideInternal").checked)continue;

    const isFaded = o.tr && !o.solid;
    ctx.globalAlpha = isFaded ? 0.4 : 1;
    ctx.drawImage(o.img,o.x,o.y);
  }}

  if(selected){{
    const selFaded = selected.tr && !selected.solid;
    ctx.globalAlpha = selFaded ? 0.4 : 1;
    ctx.drawImage(selected.img,selected.x,selected.y);

    ctx.lineWidth=2/scale;
    ctx.strokeStyle="#f55";
    ctx.strokeRect(selected.x,selected.y,selected.w,selected.h);
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
  render();
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
  if (o.xd !== 32) display.xd = o.xd;
  if (o.yd !== 32) display.yd = o.yd;
  if (o.zd !== 8)  display.zd = o.zd;
  info.textContent = JSON.stringify(display, null, 2);

  render();

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
    startX=e.clientX; startY=e.clientY;
    render();
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
  $("zoomLbl").textContent=scale.toFixed(2);
  render();
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
    render();
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
    pinchDist=newDist;
    moved=true;
    render();
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

  $("zoomLbl").textContent=scale.toFixed(2);
  render();
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
    cb.onchange=()=>{{cb.checked?enabled.add(shp):enabled.delete(shp);render();}};

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

  select(o);
}}

$("btnAll").onclick=()=>{{enabled = new Set(shapeIds);syncCheckboxes();render();}};
$("btnNone").onclick=()=>{{enabled.clear();syncCheckboxes();render();}};
$("hideInternal").onchange=render;
$("search").oninput=e=>buildList(e.target.value);

loadMap(MAP_INDEX[0]);
</script>
</body>
</html>"""
    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html)

if __name__ == "__main__":
    build_all()