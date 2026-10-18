#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ecss_pcb_tool.py - ECSS PCB helper for KiCad (rigid, no RF / HDI / AAD, 0-10 V)

  a) writes a design-specific KiCad custom rule file (.kicad_dru) from the board
     data (stackup -> per-layer clearance / min track width) and the answers
     stored in a JSON config;
  b) runs the checks a rule file cannot do (stackup, connectivity, board outline,
     mounting holes, padstack flags, teardrops) and writes a report.

The .kicad_pcb file is parsed directly (no KiCad / pcbnew needed). Only
dependency: shapely  (pip install shapely).

Usage
  python ecss_pcb_tool.py board.kicad_pcb                  # DRU + checks + report
  python ecss_pcb_tool.py board.kicad_pcb --write-config ecss_config.json
  python ecss_pcb_tool.py board.kicad_pcb --config ecss_config.json --force
The report goes to <board folder>/ecss_check_results/ecss_rule_check_DDMMYYYY_HH:MM:SS.txt
(folder created if missing; ':' becomes '-' on Windows). The terminal only shows 4 progress lines.
Exit code: 1 if at least one ERROR was found, else 0.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

try:
    from shapely import affinity
    from shapely.geometry import LineString, Point, Polygon, box
    from shapely.ops import polygonize, unary_union
    from shapely.strtree import STRtree
except ImportError:  # pragma: no cover
    sys.exit("This script needs shapely:  pip install shapely")

# ============================================================================
# 1. Configuration (answers to the open questions) -----------------------------
# ============================================================================
DEFAULTS = {
    "_help": {
        "voltage_band": "only '0-10' (V peak) is implemented",
        "conformal_coating": "true -> external spacing from the 'with conformal coating' columns",
        "outer_copper_includes_plating": "false: stackup outer copper thickness = base foil, plating added below",
        "outer_plating_um": "plated copper added on external layers when the line above is false",
        "bond_sequence": "'single' (450 um hole wall to hole wall) or 'sequential' (650 um)",
        "courtyard_margin_mm": "courtyard margin around body+pads (0 = tight courtyards)",
        "mounting_hole_ref_regex": "reference designators of mounting holes",
        "chip_footprint_regex": "footprint IDs considered chip devices (Track <= 1/3 pad width)",
        "chip_two_pad_heuristic": "also treat any footprint with exactly 2 SMD pads as a chip device",
        "screw_head_diameter_mm": "null -> A5 (track to screw) not checked; else screw head diameter",
        "edge_zone_mm": "width of the component-free zone along the PCB edge",
        "edge_zone_exempt_ref_regex": "components allowed inside the edge zone (e.g. '^(H|J)')",
        "default_copper_um": "copper thickness used when the board has no stackup section",
        "shall_severity": "severity for 'shall' requirements",
        "should_severity": "severity for 'should' requirements",
    },
    "voltage_band": "0-10",
    "conformal_coating": False,
    "outer_copper_includes_plating": False,
    "outer_plating_um": 25.0,
    "bond_sequence": "single",
    "courtyard_margin_mm": 0.0,
    "mounting_hole_ref_regex": "^H",
    "chip_footprint_regex": "^(Resistor_SMD|Capacitor_SMD|Inductor_SMD|LED_SMD):",
    "chip_two_pad_heuristic": True,
    "screw_head_diameter_mm": None,
    "edge_zone_mm": 5.0,
    "edge_zone_exempt_ref_regex": "^H",
    "default_copper_um": 35.0,
    "shall_severity": "error",
    "should_severity": "warning",
}


def load_config(path):
    cfg = {k: v for k, v in DEFAULTS.items()}
    if path:
        user = json.loads(Path(path).read_text(encoding="utf-8"))
        for k, v in user.items():
            if k != "_help":
                cfg[k] = v
    if cfg["voltage_band"] != "0-10":
        sys.exit("Only voltage_band '0-10' is implemented (tables for other bands not transcribed).")
    if cfg["bond_sequence"] not in ("single", "sequential"):
        sys.exit("bond_sequence must be 'single' or 'sequential'")
    return cfg


# ============================================================================
# 2. ECSS data transcribed from the tables supplied ----------------------------
# ============================================================================
# Each row: (upper bound of total Cu thickness Th in um, ...)
# Table 7-3: (Th<=, relative tol, max absolute tol um)
T7_3 = [(17, 0.20, 30.0), (60, 0.20, 50.0), (70, 0.20, 50.0), (95, 0.20, 70.0)]
# Table 7-5 internal, normal pitch, as-manufactured (width um, spacing um)
T7_5 = [(17, 104.0, 104.0), (60, 120.0, 120.0), (70, 160.0, 160.0), (95, 240.0, 240.0)]
# Table 7-4 external, normal pitch, as-manufactured (width um, spacing um)
T7_4 = [(60, 160.0, 160.0), (70, 160.0, 160.0), (95, 240.0, 240.0)]
# Table 13-7 part 3: X,Y internal conductor to conductor, 0<V<=10, as-designed, standard
T13_7_INT = [(17, 130.0), (60, 150.0), (70, 200.0), (95, 300.0)]
# Table 13-7 part 2: X,Y external, 0<V<=10, as-designed, standard, normal pitch
T13_7_EXT = {False: {"le70": 250.0, "gt70": 350.0},   # without conformal coating
             True: {"le70": 200.0, "gt70": 300.0}}    # with conformal coating
# Table 13-7 part 4: hole wall to hole wall, 0<V<=10, as-designed
T13_7_H2H = {"single": 450.0, "sequential": 650.0}
# Table 13-7 part 1: Z laminate, standard, as-designed (7.1.3 f/g): Cu foil <=35 -> >=4 mil, 70 -> >=5 mil
T13_7_Z_CORE = [(35.0, 101.6), (70.0, 127.0)]
Z_PREPREG_AS_MANUFACTURED_UM = 70.0
# Table 14-1 (mm)
A1_TRACK_EDGE, A3_TRACK_NPTH, A4_VIA_MECHHOLE = 0.7, 0.4, 1.0
A5_TRACK_SCREW, A6_COPPER_EDGE, A7_HOLE_EDGE, A7_HOLE_EDGE_RECOMMENDED = 0.5, 0.25, 1.0, 1.5
# Table 14-3 floor (mm)
T14_3_FLOOR = 0.3


def row_for(th, table):
    for row in table:
        if th <= row[0] + 1e-6:
            return row
    return None


def designed_min_width_um(w_manuf, rel, abs_max):
    """Smallest designed width whose worst case (w - min(rel*w, abs_max)) >= w_manuf."""
    w = w_manuf / (1.0 - rel)
    if rel * w > abs_max:
        w = w_manuf + abs_max
    return w


def table_14_4_min_length_mm(width_mm):
    """Table 14-4: W_T 0.2 -> L_T >= 0.25 ; W_T 0.3 -> L_T >= 0.7. Returns (L, extrapolated)."""
    if width_mm <= 0.2 + 1e-6:
        return 0.25, False
    if width_mm <= 0.3 + 1e-6:
        return 0.7, False
    return 0.7, True


# ============================================================================
# 3. S-expression parser and geometry helpers --------------------------------
# ============================================================================
_TOK = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')


def parse_sexpr(text):
    stack = [[]]
    for m in _TOK.finditer(text):
        t = m.group(0)
        if t == "(":
            node = []
            stack[-1].append(node)
            stack.append(node)
        elif t == ")":
            if len(stack) > 1:
                stack.pop()
        elif t[0] == '"':
            stack[-1].append(t[1:-1].replace('\\"', '"').replace("\\\\", "\\"))
        else:
            stack[-1].append(t)
    return stack[0][0]


def kids(n, head):
    return [c for c in n if isinstance(c, list) and c and c[0] == head]


def kid(n, head):
    k = kids(n, head)
    return k[0] if k else None


def has_flag(n, name):
    for c in n:
        if c == name:
            return True
        if isinstance(c, list) and c and c[0] == name:
            return len(c) == 1 or c[1] in ("yes", "true")
    return False


def fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def rotk(x, y, deg):
    """KiCad RotatePoint (y axis down, angle positive = counter-clockwise on screen)."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, y * c - x * s


def rot_geom(g, deg):
    return affinity.rotate(g, -deg, origin=(0, 0))


def key(p):
    return (round(p[0], 4), round(p[1], 4))


def arc_points(a, b, c, n=24):
    ax, ay = a
    bx, by = b
    cx, cy = c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [a, c]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0 = math.atan2(ay - uy, ax - ux)
    am = math.atan2(by - uy, bx - ux)
    a1 = math.atan2(cy - uy, cx - ux)
    tau = 2 * math.pi
    s_m, s_1 = (am - a0) % tau, (a1 - a0) % tau
    sweep = s_1 if s_m < s_1 else s_1 - tau
    pts = [(ux + r * math.cos(a0 + sweep * i / n), uy + r * math.sin(a0 + sweep * i / n)) for i in range(n + 1)]
    pts[0], pts[-1] = a, c
    return pts


# ============================================================================
# 4. Board model --------------------------------------------------------------
# ============================================================================
@dataclass
class Pad:
    fp: "Footprint"
    number: str
    kind: str          # smd | thru_hole | np_thru_hole | connect
    shape: str
    x: float
    y: float
    rot: float
    w: float
    h: float
    drill: float
    layers: list
    net: str
    poly: object
    copper: list
    remove_unused: bool

    @property
    def name(self):
        return f"{self.fp.ref}.{self.number}"

    @property
    def diameter(self):
        return min(self.w, self.h)


@dataclass
class Footprint:
    ref: str
    lib: str
    x: float
    y: float
    rot: float
    layer: str
    pads: list = field(default_factory=list)
    courtyard: object = None


@dataclass
class Seg:
    layer: str
    net: str
    width: float
    pts: list


@dataclass
class Via:
    x: float
    y: float
    size: float
    drill: float
    layers: list
    net: str
    circle: object
    remove_unused: bool


@dataclass
class Board:
    path: str
    copper_layers: list = field(default_factory=list)
    stackup: list | None = None
    footprints: list = field(default_factory=list)
    segs: list = field(default_factory=list)
    vias: list = field(default_factory=list)
    outline: object = None
    teardrops: list = field(default_factory=list)   # (layer, net, polygon)
    notes: list = field(default_factory=list)

    def pads(self):
        for f in self.footprints:
            for p in f.pads:
                yield p


def net_of(node, netmap):
    nn = kid(node, "net")
    if not nn:
        return ""
    a = nn[1:]
    if len(a) >= 2:
        return a[1]
    if a:
        v = a[0]
        if v.isdigit() and int(v) in netmap:
            return netmap[int(v)]
        return v
    return ""


def pad_polygon(shape, w, h):
    if shape == "circle":
        return Point(0, 0).buffer(w / 2.0, 16)
    if shape == "oval":
        if w >= h:
            d = (w - h) / 2.0
            return LineString([(-d, 0), (d, 0)]).buffer(h / 2.0, 16)
        d = (h - w) / 2.0
        return LineString([(0, -d), (0, d)]).buffer(w / 2.0, 16)
    return box(-w / 2.0, -h / 2.0, w / 2.0, h / 2.0)   # rect, roundrect, trapezoid, custom (approx.)


def gfx_geoms(node, tf):
    """Return list of ('line'|'poly', geometry) for a gr_*/fp_* graphic node."""
    head = node[0].split("_", 1)[1]
    xy = lambda n: tf(fnum(n[1]), fnum(n[2]))
    s, e, c, m = kid(node, "start"), kid(node, "end"), kid(node, "center"), kid(node, "mid")
    if head == "line" and s and e:
        return [("line", LineString([xy(s), xy(e)]))]
    if head == "rect" and s and e:
        x0, y0, x1, y1 = fnum(s[1]), fnum(s[2]), fnum(e[1]), fnum(e[2])
        return [("poly", Polygon([tf(x0, y0), tf(x1, y0), tf(x1, y1), tf(x0, y1)]))]
    if head == "circle" and c and e:
        r = math.hypot(fnum(e[1]) - fnum(c[1]), fnum(e[2]) - fnum(c[2]))
        pts = [tf(fnum(c[1]) + r * math.cos(t * math.pi / 16), fnum(c[2]) + r * math.sin(t * math.pi / 16)) for t in range(32)]
        return [("poly", Polygon(pts))]
    if head == "poly":
        pts_n = kid(node, "pts")
        if pts_n:
            pts = [tf(fnum(p[1]), fnum(p[2])) for p in kids(pts_n, "xy")]
            if len(pts) >= 3:
                return [("poly", Polygon(pts))]
    if head == "arc" and s and m and e:
        pts = [tf(*p) for p in arc_points((fnum(s[1]), fnum(s[2])), (fnum(m[1]), fnum(m[2])), (fnum(e[1]), fnum(e[2])))]
        return [("line", LineString(pts))]
    return []


def layer_of(n):
    k = kid(n, "layer")
    return k[1] if k else ""


def parse_board(path):
    root = parse_sexpr(Path(path).read_text(encoding="utf-8"))
    if not root or root[0] != "kicad_pcb":
        sys.exit("Not a .kicad_pcb file")
    b = Board(path=str(path))

    layers_n = kid(root, "layers")
    if layers_n:
        for l in layers_n[1:]:
            if isinstance(l, list) and len(l) >= 2 and str(l[1]).endswith(".Cu"):
                b.copper_layers.append(l[1])

    netmap = {}
    for n in kids(root, "net"):
        if len(n) >= 3 and n[1].isdigit():
            netmap[int(n[1])] = n[2]

    # stackup
    setup = kid(root, "setup")
    st = kid(setup, "stackup") if setup else None
    if st:
        b.stackup = []
        for l in kids(st, "layer"):
            t = kid(l, "type")
            th = sum(fnum(x[1]) for x in kids(l, "thickness")) * 1000.0   # um
            b.stackup.append({"name": l[1], "type": (t[1] if t else "").lower(), "th_um": th})
        order = [e["name"] for e in b.stackup if e["name"].endswith(".Cu")]
        if order:
            b.copper_layers = order

    # footprints
    for fn in kids(root, "footprint"):
        at = kid(fn, "at")
        fx, fy = fnum(at[1]), fnum(at[2])
        fr = fnum(at[3]) if len(at) > 3 else 0.0
        ref = ""
        for p in kids(fn, "property"):
            if p[1] == "Reference":
                ref = p[2]
        if not ref:
            for t in kids(fn, "fp_text"):
                if t[1] == "reference":
                    ref = t[2]
        fp = Footprint(ref=ref, lib=fn[1], x=fx, y=fy, rot=fr, layer=layer_of(fn))

        def tf(px, py, fx=fx, fy=fy, fr=fr):
            rx, ry = rotk(px, py, fr)
            return fx + rx, fy + ry

        for pn in kids(fn, "pad"):
            pat = kid(pn, "at")
            px, py = tf(fnum(pat[1]), fnum(pat[2]))
            prot = fnum(pat[3]) if len(pat) > 3 else 0.0      # assumed absolute orientation (KiCad file format)
            sz = kid(pn, "size")
            w, h = (fnum(sz[1]), fnum(sz[2])) if sz else (0.0, 0.0)
            dr = kid(pn, "drill")
            drill = max([fnum(x) for x in dr[1:] if isinstance(x, str) and re.match(r"^[0-9.]+$", x)] or [0.0]) if dr else 0.0
            lay = kid(pn, "layers")
            lays = [x for x in lay[1:] if isinstance(x, str)] if lay else []
            cu = []
            for l in lays:
                if l == "*.Cu":
                    cu = list(b.copper_layers)
                    break
                if l.endswith(".Cu"):
                    cu.append(l)
            if "F&B.Cu" in lays:
                cu = [b.copper_layers[0], b.copper_layers[-1]] if b.copper_layers else ["F.Cu", "B.Cu"]
            poly = pad_polygon(pn[3], w, h)
            poly = affinity.translate(rot_geom(poly, prot), px, py)
            fp.pads.append(Pad(fp, pn[1], pn[2], pn[3], px, py, prot, w, h, drill, lays, net_of(pn, netmap),
                               poly, cu, has_flag(pn, "remove_unused_layers")))
            if kid(pn, "padstack"):
                b.notes.append(f"{ref}.{pn[1]}: KiCad 9+ padstack present, only the main pad size is analysed")

        courts = []
        lines = []
        for g in fn:
            if isinstance(g, list) and g and g[0] in ("fp_line", "fp_rect", "fp_circle", "fp_poly", "fp_arc") \
                    and layer_of(g) in ("F.CrtYd", "B.CrtYd"):
                for kind, geom in gfx_geoms(g, tf):
                    (courts if kind == "poly" else lines).append(geom)
        if lines:
            courts += list(polygonize(unary_union(lines)))
        if courts:
            fp.courtyard = unary_union(courts)
        b.footprints.append(fp)

    # tracks, vias
    for s in kids(root, "segment"):
        st_, en = kid(s, "start"), kid(s, "end")
        b.segs.append(Seg(layer_of(s), net_of(s, netmap), fnum(kid(s, "width")[1]),
                          [(fnum(st_[1]), fnum(st_[2])), (fnum(en[1]), fnum(en[2]))]))
    for s in kids(root, "arc"):
        a, m, e = kid(s, "start"), kid(s, "mid"), kid(s, "end")
        pts = arc_points((fnum(a[1]), fnum(a[2])), (fnum(m[1]), fnum(m[2])), (fnum(e[1]), fnum(e[2])))
        b.segs.append(Seg(layer_of(s), net_of(s, netmap), fnum(kid(s, "width")[1]), pts))
    for v in kids(root, "via"):
        at = kid(v, "at")
        lay = kid(v, "layers")
        lays = [x for x in lay[1:] if isinstance(x, str)] if lay else []
        if len(lays) >= 2 and b.copper_layers and lays[0] in b.copper_layers and lays[-1] in b.copper_layers:
            i0, i1 = sorted((b.copper_layers.index(lays[0]), b.copper_layers.index(lays[-1])))
            lays = b.copper_layers[i0:i1 + 1]
        size = fnum(kid(v, "size")[1])
        b.vias.append(Via(fnum(at[1]), fnum(at[2]), size, fnum(kid(v, "drill")[1]) if kid(v, "drill") else 0.0, lays,
                          net_of(v, netmap), Point(fnum(at[1]), fnum(at[2])).buffer(size / 2.0, 16),
                          has_flag(v, "remove_unused_layers")))

    # outline (Edge.Cuts) - board level graphics only
    lines = []
    ident = lambda x, y: (x, y)
    for g in root:
        if isinstance(g, list) and g and g[0] in ("gr_line", "gr_rect", "gr_circle", "gr_poly", "gr_arc") \
                and layer_of(g) == "Edge.Cuts":
            for kind, geom in gfx_geoms(g, ident):
                lines.append(geom.exterior if kind == "poly" else geom)
    if lines:
        b.outline = unary_union(lines)

    # teardrop zones (best effort)
    for z in kids(root, "zone"):
        if kid(z, "teardrop"):
            poly_n = kid(z, "polygon")
            pts_n = kid(poly_n, "pts") if poly_n else None
            if pts_n:
                pts = [(fnum(p[1]), fnum(p[2])) for p in kids(pts_n, "xy")]
                if len(pts) >= 3:
                    lay = layer_of(z)
                    if not lay and kid(z, "layers"):
                        lay = kid(z, "layers")[1]
                    b.teardrops.append((lay, net_of(z, netmap), Polygon(pts).buffer(0)))
    return b


# ============================================================================
# 5. Layer-dependent values (stackup -> ECSS tables) ---------------------------
# ============================================================================
@dataclass
class LayerInfo:
    name: str
    role: str            # outer | inner
    foil_um: float
    th_um: float         # total copper thickness Th used for the tables
    spacing_um: float    # as-designed X,Y spacing (Table 13-7)
    width_um: float      # as-designed min track width (Tables 7-4/7-5 + 7-3)
    note: str = ""
    spacing_shall_um: float = 0.0   # "shall" floor: Table 7-4/7-5 spacing + Table 7-3 tolerance


def derive_layers(b, cfg):
    infos = []
    if not b.copper_layers:
        sys.exit("No copper layers found in the board file")
    thick = {}
    if b.stackup:
        for e in b.stackup:
            if e["name"].endswith(".Cu"):
                thick[e["name"]] = e["th_um"]
    else:
        b.notes.append("No stackup section in the board file: default_copper_um used for every layer "
                       "(open Board Setup > Physical Stackup and save, to store the real one).")
    for i, name in enumerate(b.copper_layers):
        outer = i in (0, len(b.copper_layers) - 1)
        foil = thick.get(name, cfg["default_copper_um"]) or cfg["default_copper_um"]
        th = foil if (not outer or cfg["outer_copper_includes_plating"]) else foil + cfg["outer_plating_um"]
        if outer:
            sp = T13_7_EXT[bool(cfg["conformal_coating"])]["le70" if th <= 70.0 + 1e-6 else "gt70"]
            wrow = row_for(th, T7_4)
            w_man = wrow[1] if wrow else None
        else:
            srow = row_for(th, T13_7_INT)
            sp = srow[1] if srow else None
            wrow = row_for(th, T7_5)
            w_man = wrow[1] if wrow else None
        trow = row_for(th, T7_3)
        if sp is None or w_man is None or trow is None:
            sys.exit(f"Layer {name}: total copper thickness {th:.0f} um is outside the ECSS tables (max 95 um)")
        wd = designed_min_width_um(w_man, trow[1], trow[2])
        if outer:
            # External as-designed spacing of Table 13-7 is a "should"; the "shall" floor comes from
            # Table 7-4 (as-manufactured spacing) combined with the Table 7-3 tolerance.
            sp_shall = designed_min_width_um(wrow[2], trow[1], trow[2])
        else:
            sp_shall = sp            # internal as-designed spacing of Table 13-7 is a "shall"
        infos.append(LayerInfo(name, "outer" if outer else "inner", foil, th, sp, wd, "", sp_shall))
    return infos


# ============================================================================
# 6. DRU generation -------------------------------------------------------------
# ============================================================================
def mm(v_um_or_mm, um=False):
    v = v_um_or_mm / 1000.0 if um else v_um_or_mm
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s + "mm"


def build_dru(b, cfg, infos):
    sh, so = cfg["shall_severity"], cfg["should_severity"]
    o = []
    a = o.append
    a("(version 1)\n")
    a("# ============================================================================")
    a("# ECSS PCB design rules - GENERATED by ecss_pcb_tool.py from the board data")
    a(f"# Board: {Path(b.path).name}")
    a("# Scope: rigid PCB, 0<V<=10, standard (non 'Reliable'), no RF/HDI/AAD, as-DESIGNED values.")
    a(f"# Conformal coating: {cfg['conformal_coating']} | bond sequence: {cfg['bond_sequence']}")
    a("# Per-layer values (derived from the stackup):")
    for li in infos:
        a(f"#   {li.name:<8} {li.role:<5} foil {li.foil_um:5.1f} um  Th {li.th_um:5.1f} um  "
          f"spacing shall >= {li.spacing_shall_um:.0f} um / should >= {li.spacing_um:.0f} um  "
          f"track width >= {li.width_um:.0f} um")
    a("# External spacing: only the 'shall' floor is in this file (KiCad applies ONE clearance rule per item);")
    a("# the 'should' level of Table 13-7 is checked by ecss_pcb_tool.py (warning).")
    a("# These rules replace Board Setup / netclass constraints of the same type for the items they match.")
    a("# KiCad evaluates rules from the bottom up: the last matching rule wins.")
    a("# ============================================================================\n")

    a('(rule "ECSS min conductor width 0.1mm"')
    a(f"\t(severity {sh})\n\t(constraint track_width (min 0.1mm))\n)\n")

    for li in infos:
        src = "Tables 7-4 + 7-3, shall" if li.role == "outer" else "Table 13-7, as-designed, shall"
        a(f'(rule "ECSS X,Y spacing {li.name} ({src})"')
        a(f'\t(severity {sh})\n\t(layer "{li.name}")\n\t(constraint clearance (min {mm(li.spacing_shall_um, True)}))\n)\n')
        a(f'(rule "ECSS min track width {li.name} (Tables 7-4/7-5 + 7-3 tolerance)"')
        a(f'\t(severity {sh})\n\t(layer "{li.name}")\n\t(constraint track_width (min {mm(li.width_um, True)}))\n)\n')

    h2h = T13_7_H2H[cfg["bond_sequence"]]
    a('(rule "ECSS via hole wall to via hole wall, different nets (Table 13-7)"')
    a(f"\t(severity {sh})\n\t(constraint hole_to_hole (min {mm(h2h, True)}))")
    a("\t(condition \"A.Type == 'Via' && B.Type == 'Via' && A.NetName != B.NetName\")\n)\n")

    a('(rule "ECSS Table 14-1 A6 - conductive pattern to PCB edge"')
    a(f"\t(severity {sh})\n\t(constraint edge_clearance (min {mm(A6_COPPER_EDGE)}))\n)\n")
    a('(rule "ECSS Table 14-1 A1 - track to PCB edge"')
    a(f"\t(severity {sh})\n\t(constraint edge_clearance (min {mm(A1_TRACK_EDGE)}))")
    a("\t(condition \"A.Type == 'Track'\")\n)\n")
    a("# A3: written in both orders (A/B roles of hole_clearance to be verified on a test board)")
    a('(rule "ECSS Table 14-1 A3 - track to NPTH"')
    a(f"\t(severity {sh})\n\t(constraint hole_clearance (min {mm(A3_TRACK_NPTH)}))")
    a("\t(condition \"(A.Type == 'Track' && B.Pad_Type == 'NPTH, mechanical') || "
      "(B.Type == 'Track' && A.Pad_Type == 'NPTH, mechanical')\")\n)\n")

    a("# Pad diameter >= hole + 0.8 mm (solder side) / + 0.6 mm (component side, vias) -> ring 0.4 / 0.3 mm.")
    a("# Uniform pad stack assumed, so the solder-side value is applied to every through-hole pad.")
    a('(rule "ECSS annular ring - component holes (0.4mm)"')
    a(f"\t(severity {so})\n\t(constraint annular_width (min 0.4mm))")
    a("\t(condition \"A.Type == 'Pad' && A.Pad_Type == 'Through-hole'\")\n)\n")
    a('(rule "ECSS annular ring - vias (0.3mm)"')
    a(f"\t(severity {so})\n\t(constraint annular_width (min 0.3mm))")
    a("\t(condition \"A.Type == 'Via'\")\n)\n")

    cy = T14_3_FLOOR - 2 * cfg["courtyard_margin_mm"]
    if cy > 0:
        a(f"# Table 14-3 floor 0.3 mm (courtyard margin {cfg['courtyard_margin_mm']} mm per side)")
        a('(rule "ECSS Table 14-3 minimum distance between components"')
        a(f"\t(severity {sh})\n\t(constraint courtyard_clearance (min {mm(cy)}))\n)\n")
    return "\n".join(o)


# ============================================================================
# 7. Checks ------------------------------------------------------------------
# ============================================================================
@dataclass
class Finding:
    sev: str       # ERROR | WARNING | INFO
    req: str
    where: str
    msg: str


class Checker:
    def __init__(self, b, cfg, infos):
        self.b, self.cfg, self.infos = b, cfg, infos
        self.F = []
        self.sh = cfg["shall_severity"].upper()
        self.so = cfg["should_severity"].upper()
        self._index()

    def add(self, sev, req, where, msg):
        self.F.append(Finding(sev, req, where, msg))

    # ---- indexes -----------------------------------------------------------
    def _index(self):
        b = self.b
        self.end_pts = {}       # layer -> (list of Point, list of (seg idx, end idx))
        self.end_by_node = {}   # (layer,key) -> [(seg idx, end idx)]
        for i, s in enumerate(b.segs):
            for e, p in ((0, s.pts[0]), (1, s.pts[-1])):
                pts, meta = self.end_pts.setdefault(s.layer, ([], []))
                pts.append(Point(p))
                meta.append((i, e))
                self.end_by_node.setdefault((s.layer, key(p)), []).append((i, e))
        self.end_tree = {l: STRtree(pts) for l, (pts, _) in self.end_pts.items()}
        self.via_tree = STRtree([v.circle.buffer(1e-3) for v in b.vias]) if b.vias else None
        self.all_pads = list(b.pads())
        self.pad_tree = STRtree([p.poly.buffer(1e-3) for p in self.all_pads]) if self.all_pads else None

    def ends_touching(self, geom, layer):
        if layer not in self.end_tree:
            return []
        idx = self.end_tree[layer].query(geom.buffer(1e-3), predicate="intersects")
        return [self.end_pts[layer][1][i] for i in idx]

    def vias_at(self, pt, layer):
        if not self.via_tree:
            return []
        return [self.b.vias[i] for i in self.via_tree.query(Point(pt), predicate="intersects")
                if layer in self.b.vias[i].layers]

    def pads_at(self, pt, layer):
        if not self.pad_tree:
            return []
        return [self.all_pads[i] for i in self.pad_tree.query(Point(pt), predicate="intersects")
                if layer in self.all_pads[i].copper]

    # ---- tracing -----------------------------------------------------------
    def trace(self, pad, layer, si, e0):
        """DFS from a track end sitting on `pad`; returns [(via, path_pts, max_width)] for vias reached."""
        out = []

        def dfs(si, enter, path, wmax, visited, depth):
            s = self.b.segs[si]
            pts = s.pts if enter == 0 else s.pts[::-1]
            path = path + (pts if not path else pts[1:])
            wmax = max(wmax, s.width)
            end = pts[-1]
            if depth > 60:
                return
            vs = [v for v in self.vias_at(end, layer) if v.net == s.net or not s.net]
            if vs:
                for v in vs:
                    out.append((v, path, wmax))
                return
            if any(p is not pad for p in self.pads_at(end, layer)):
                return
            if pad.poly.buffer(1e-3).contains(Point(end)) and len(path) > 2:
                return
            for sj, ej in self.end_by_node.get((layer, key(end)), []):
                if sj not in visited and sj != si and self.b.segs[sj].net == s.net:
                    dfs(sj, ej, path, wmax, visited | {si}, depth + 1)

        dfs(si, e0, [], 0.0, frozenset(), 0)
        return out

    def fan_outs(self, pad):
        """{via id: (via, min length_mm, max width_mm)} for the fan-out tracks of `pad`."""
        res = {}
        for layer in pad.copper:
            for si, e in self.ends_touching(pad.poly, layer):
                if self.b.segs[si].net != pad.net:
                    continue
                for v, path, wmax in self.trace(pad, layer, si, e):
                    if len(path) < 2:
                        continue
                    ln = LineString(path).difference(pad.poly).difference(v.circle).length
                    cur = res.get(id(v))
                    if cur is None or ln < cur[1]:
                        res[id(v)] = (v, ln, wmax)
        return list(res.values())

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def loc(pad):
        return f"{pad.name} ({pad.x:.2f}, {pad.y:.2f})"

    @staticmethod
    def vloc(v):
        return f"via net '{v.net}' ({v.x:.2f}, {v.y:.2f})"

    def is_chip(self, fp):
        if re.search(self.cfg["chip_footprint_regex"], fp.lib):
            return True
        if self.cfg["chip_two_pad_heuristic"]:
            return len(fp.pads) == 2 and all(p.kind == "smd" for p in fp.pads)
        return False

    # ---- checks ------------------------------------------------------------
    def run(self):
        for fn in (self.chk_stackup, self.chk_outer_spacing, self.chk_pad_tracks, self.chk_mounting, self.chk_edge_zone,
                   self.chk_padstack_flags, self.chk_teardrops):
            fn()
        for n in self.b.notes:
            self.add("INFO", "parser", "-", n)
        return self.F

    def chk_stackup(self):
        b = self.b
        req = "Table 13-7 Z insulation (0<V<=10, standard)"
        if not b.stackup:
            self.add("WARNING", req, "board", "No stackup in the file: Z-direction insulation not checked.")
            return
        seq = [e for e in b.stackup if e["name"].endswith(".Cu") or e["type"] in ("core", "prepreg")]
        foil = {li.name: li.foil_um for li in self.infos}
        for i, e in enumerate(seq):
            if e["type"] not in ("core", "prepreg"):
                continue
            above = next((x for x in reversed(seq[:i]) if x["name"].endswith(".Cu")), None)
            below = next((x for x in seq[i + 1:] if x["name"].endswith(".Cu")), None)
            if not above or not below:
                continue
            t = e["th_um"]
            if e["type"] == "core":
                f = max(foil.get(above["name"], 35.0), foil.get(below["name"], 35.0))
                row = row_for(f, T13_7_Z_CORE)
                if row is None:
                    self.add("INFO", req, e["name"], f"Copper foil {f:.0f} um not covered by Table 13-7 (Z laminate).")
                    continue
                if t + 1e-6 < row[1]:
                    self.add(self.sh, req, f"{e['name']} ({above['name']}/{below['name']})",
                             f"Laminate {t:.1f} um < {row[1]:.1f} um required as-designed for Cu <= {row[0]:.0f} um.")
                else:
                    self.add("INFO", req, f"{e['name']} ({above['name']}/{below['name']})",
                             f"Laminate {t:.1f} um >= {row[1]:.1f} um: OK.")
            else:
                if t < Z_PREPREG_AS_MANUFACTURED_UM:
                    self.add(self.sh, req, f"{e['name']} ({above['name']}/{below['name']})",
                             f"Prepreg nominal {t:.1f} um is below the 70 um as-manufactured minimum.")
                else:
                    self.add("INFO", req, f"{e['name']} ({above['name']}/{below['name']})",
                             f"Prepreg nominal {t:.1f} um; Table 13-7 gives no as-designed value (70 um as-manufactured): "
                             "confirm with the manufacturer's pressed thickness.")

    def chk_outer_spacing(self):
        """Table 13-7 external X,Y spacing is a 'should' (the 'shall' floor is in the .kicad_dru)."""
        b = self.b
        req = "Table 13-7 external X,Y spacing (as-designed, should)"
        for li in self.infos:
            if li.role != "outer":
                continue
            need = li.spacing_um / 1000.0
            floor = li.spacing_shall_um / 1000.0
            items = []   # (geom, net or None, footprint or None, label)
            for sg in b.segs:
                if sg.layer == li.name:
                    items.append((LineString(sg.pts).buffer(sg.width / 2.0, 8), sg.net or None, None,
                                  f"track net '{sg.net}' ({sg.pts[0][0]:.2f}, {sg.pts[0][1]:.2f})"))
            for v in b.vias:
                if li.name in v.layers:
                    items.append((v.circle, v.net or None, None, self.vloc(v)))
            for p in self.all_pads:
                if li.name in p.copper:
                    items.append((p.poly, p.net or None, p.fp, self.loc(p)))
            if not items:
                continue
            tree = STRtree([it[0] for it in items])
            seen = set()
            for i, (g, net, fp, lab) in enumerate(items):
                for j in tree.query(g.buffer(need), predicate="intersects"):
                    if j <= i or (i, j) in seen:
                        continue
                    seen.add((i, j))
                    g2, net2, fp2, lab2 = items[j]
                    if net is not None and net == net2:
                        continue
                    if fp is not None and fp is fp2:
                        continue          # pad-to-pad inside a footprint (fine-pitch parts): not checked
                    d = g.distance(g2)
                    if d + 1e-6 < need:
                        sev = self.sh if d + 1e-6 < floor else self.so
                        self.add(sev, req, f"{li.name}: {lab} / {lab2}",
                                 f"{d:.3f} mm < {need:.3f} mm (shall floor {floor:.3f} mm)")

    def chk_pad_tracks(self):
        chips = 0
        for fp in self.b.footprints:
            chip = self.is_chip(fp)
            chips += chip
            for pad in fp.pads:
                if pad.kind == "np_thru_hole" or not pad.copper:
                    continue
                # (1) track width at pad
                for layer in pad.copper:
                    for si, e in self.ends_touching(pad.poly, layer):
                        s = self.b.segs[si]
                        if s.net != pad.net:
                            continue
                        w = s.width
                        if pad.kind == "smd" and chip:
                            pts = s.pts if e == 0 else s.pts[::-1]
                            # board vector -> pad local frame = inverse of the pad rotation
                            dx, dy = rotk(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1], -pad.rot)
                            wp = pad.h if abs(dx) >= abs(dy) else pad.w
                            if pad.shape == "circle":
                                wp = pad.w
                            if w > wp / 3.0 + 1e-6:
                                self.add(self.so, "Track width <= 1/3 of chip SMT pad width", self.loc(pad),
                                         f"track {w:.3f} mm > {wp / 3.0:.3f} mm (pad width {wp:.3f} mm), layer {layer}")
                        if pad.kind == "thru_hole":
                            if w > pad.diameter / 2.0 + 1e-6:
                                self.add(self.so, "Track width at PTH pad <= 1/2 pad diameter", self.loc(pad),
                                         f"track {w:.3f} mm > {pad.diameter / 2.0:.3f} mm (pad D {pad.diameter:.3f} mm), layer {layer}")
                # (2) fan-out lengths
                for v, ln, wmax in self.fan_outs(pad):
                    if pad.kind == "smd":
                        need, extra = table_14_4_min_length_mm(wmax)
                        req = "Table 14-4 track length SMT pad -> via"
                        if ln + 1e-6 < need:
                            self.add(self.sh, req, f"{self.loc(pad)} -> {self.vloc(v)}",
                                     f"L_T = {ln:.3f} mm < {need} mm for W_T = {wmax:.3f} mm")
                        if extra:
                            self.add("WARNING", req, f"{self.loc(pad)} -> {self.vloc(v)}",
                                     f"W_T = {wmax:.3f} mm > 0.3 mm is outside Table 14-4; 0.7 mm used.")
                    elif pad.kind == "thru_hole":
                        need = 0.5 * max(pad.diameter, v.size)
                        if ln + 1e-6 < need:
                            self.add(self.so, "Track length PTH pad -> via >= 1/2 largest pad diameter",
                                     f"{self.loc(pad)} -> {self.vloc(v)}",
                                     f"L_c = {ln:.3f} mm < {need:.3f} mm")
            # via directly inside an SMD pad
            for pad in fp.pads:
                if pad.kind == "smd" and pad.copper and self.b.vias:
                    for i in self.via_tree.query(pad.poly, predicate="intersects"):
                        v = self.b.vias[i]
                        if v.net == pad.net and pad.copper[0] in v.layers:
                            self.add("INFO", "Table 14-4 track length SMT pad -> via", f"{self.loc(pad)} / {self.vloc(v)}",
                                     "Via inside the SMD pad (zero fan-out length): review manually.")
        self.add("INFO", "classification", "board", f"{chips} footprint(s) treated as chip devices "
                 f"(regex '{self.cfg['chip_footprint_regex']}' + 2-SMD-pad heuristic={self.cfg['chip_two_pad_heuristic']}).")

    def chk_mounting(self):
        b, cfg = self.b, self.cfg
        rx = re.compile(cfg["mounting_hole_ref_regex"])
        holes = []
        for fp in b.footprints:
            if rx.search(fp.ref):
                for p in fp.pads:
                    if p.drill > 0:
                        circle = Point(p.x, p.y).buffer(p.drill / 2.0, 32)
                        holes.append((p, circle, p.poly if p.copper else circle))
        self.add("INFO", "mounting holes", "board", f"{len(holes)} mounting hole(s) found (regex '{cfg['mounting_hole_ref_regex']}').")
        for p, circle, copper in holes:
            for v in b.vias:
                d = v.circle.distance(copper)
                if d + 1e-6 < A4_VIA_MECHHOLE:
                    self.add(self.sh, "Table 14-1 A4 via pad to hole for mechanical function (>= 1.0 mm)",
                             f"{self.vloc(v)} / {p.name}", f"{d:.3f} mm (measured to the hole's copper pad edge)")
            if b.outline is not None:
                d = circle.distance(b.outline)
                if d + 1e-6 < A7_HOLE_EDGE:
                    self.add(self.sh, "Table 14-1 A7 screw hole edge to PCB edge (>= 1.0 mm)", p.name, f"{d:.3f} mm")
                elif d + 1e-6 < A7_HOLE_EDGE_RECOMMENDED:
                    self.add("INFO", "Table 14-1 A7 recommended 1.5 mm", p.name, f"{d:.3f} mm (permitted, below the recommended value)")
            if cfg["screw_head_diameter_mm"]:
                head = Point(p.x, p.y).buffer(cfg["screw_head_diameter_mm"] / 2.0, 32)
                for s in b.segs:
                    d = LineString(s.pts).buffer(s.width / 2.0).distance(head)
                    if d + 1e-6 < A5_TRACK_SCREW:
                        self.add(self.sh, "Table 14-1 A5 track to screw (>= 0.5 mm)", f"{p.name} / track {s.layer} net '{s.net}'",
                                 f"{d:.3f} mm")
        if b.outline is None:
            self.add("WARNING", "Table 14-1 A7", "board", "No Edge.Cuts outline found: A7 not checked.")

    def chk_edge_zone(self):
        b, cfg = self.b, self.cfg
        if b.outline is None:
            return
        zone = cfg["edge_zone_mm"]
        ex = re.compile(cfg["edge_zone_exempt_ref_regex"]) if cfg["edge_zone_exempt_ref_regex"] else None
        for fp in b.footprints:
            if ex and ex.search(fp.ref):
                continue
            geom = fp.courtyard or (unary_union([p.poly for p in fp.pads]) if fp.pads else None)
            if geom is None:
                continue
            d = geom.distance(b.outline)
            if d + 1e-6 < zone:
                self.add(self.so, "Component-free zone along PCB edges >= 5 mm", f"{fp.ref} ({fp.x:.1f}, {fp.y:.1f})",
                         f"{d:.2f} mm from the board edge")

    def chk_padstack_flags(self):
        req = "Non-functional pads in PTH present on all layers"
        for p in self.b.pads():
            if p.kind == "thru_hole" and p.remove_unused:
                self.add(self.sh, req, self.loc(p), "pad has 'remove unused layers' enabled")
        for v in self.b.vias:
            if v.remove_unused:
                self.add(self.sh, req, self.vloc(v), "via has 'remove unused layers' enabled")

    def chk_teardrops(self):
        b = self.b
        req = "Teardrops on all internal pads"
        if not b.teardrops:
            self.add("INFO", req, "board", "No teardrop zones found in the file (none designed, or format not recognised): not checked.")
            return
        inner = b.copper_layers[1:-1]
        objs = [(p.name, p.poly, p.net, p.copper) for p in b.pads() if p.kind == "thru_hole"]
        objs += [(self.vloc(v), v.circle, v.net, v.layers) for v in b.vias]
        for name, geom, net, lays in objs:
            for layer in inner:
                if layer not in lays:
                    continue
                if not [1 for si, _ in self.ends_touching(geom, layer) if b.segs[si].net == net]:
                    continue    # no track on this layer: no teardrop expected
                if not any(l == layer and geom.buffer(1e-3).intersects(poly) for l, _, poly in b.teardrops):
                    self.add(self.so, req, f"{name}", f"no teardrop on {layer}")


# ============================================================================
# 8. Report -------------------------------------------------------------------
# ============================================================================
NOT_CHECKED = [
    "Pad design tables 14-6 to 14-10 (need component datasheet dimensions)",
    "Table 14-3 0.5 mm cases and Table 14-2 (need leaded / non-leaded classification - postponed)",
    "Table 14-1 A2 (track to mechanical part: no mechanical-part data in the PCB file)",
    "Conductor to hole wall (Table 13-7 part 4, 0-10 V cell ambiguous); conductor to PCB edge is covered by A1/A6",
    "Hole wall to heat sink; superimposed conductors offset in X,Y (not modelled)",
    "Spacing involving copper zone fills (fills are not read) and pad-to-pad spacing inside one footprint (fine pitch)",
    "Prepreg as-designed Z thickness (no value in Table 13-7 for 0-10 V)",
    "Fine-pitch exceptions, 'Reliable' columns, voltages above 10 V",
    "Table 7-3 tolerances are used to derive minimum designed widths, not checked separately",
]


def write_report(path, b, cfg, infos, findings):
    order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
    findings = sorted(findings, key=lambda f: (order[f.sev], f.req))
    cnt = {k: sum(1 for f in findings if f.sev == k) for k in order}
    L = [f"# ECSS check report - {Path(b.path).name}\n",
         f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}\n",
         f"**{cnt['ERROR']} error(s), {cnt['WARNING']} warning(s), {cnt['INFO']} info**\n",
         "## Values derived from the stackup\n",
         "| Layer | Role | Foil (um) | Th (um) | Spacing shall (um) | Spacing should (um) | Min track width (um) |",
         "|---|---|---|---|---|---|---|"]
    for li in infos:
        L.append(f"| {li.name} | {li.role} | {li.foil_um:.1f} | {li.th_um:.1f} | {li.spacing_shall_um:.0f} | "
                 f"{li.spacing_um:.0f} | {li.width_um:.0f} |")
    L.append(f"\nConformal coating: {cfg['conformal_coating']} - bond sequence: {cfg['bond_sequence']} - "
             f"outer plating added: {not cfg['outer_copper_includes_plating']} ({cfg['outer_plating_um']} um)\n")
    L += ["## Findings\n", "| Severity | Requirement | Where | Detail |", "|---|---|---|---|"]
    for f in findings:
        L.append(f"| {f.sev} | {f.req} | {f.where} | {f.msg} |")
    L += ["\n## Not checked by this tool (nor by the rule file)\n"] + [f"- {x}" for x in NOT_CHECKED]
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")
    return cnt


# ============================================================================
# 9. Main ---------------------------------------------------------------------
# ============================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="ECSS helper for KiCad: DRU generator + extra checks")
    ap.add_argument("board", nargs="?", help=".kicad_pcb file")
    ap.add_argument("--config", help="JSON config (see --write-config)")
    ap.add_argument("--write-config", metavar="FILE", help="write a default config file and exit")
    ap.add_argument("--dru", help="output .kicad_dru (default: next to the board, same stem)")
    ap.add_argument("--report", help="report path (default: <board folder>/ecss_check_results/ecss_rule_check_DDMMYYYY_HH:MM:SS.txt)")
    ap.add_argument("--no-dru", action="store_true")
    ap.add_argument("--no-checks", action="store_true")
    ap.add_argument("--force", action="store_true", help="overwrite an existing .kicad_dru")
    ap.add_argument("--coating", action="store_true", help="override: conformal coating on external layers")
    a = ap.parse_args(argv)

    if a.write_config:
        Path(a.write_config).write_text(json.dumps(DEFAULTS, indent=2) + "\n", encoding="utf-8")
        print(f"Default config written to {a.write_config}")
        return 0
    if not a.board:
        ap.error("board file required")
    cfg = load_config(a.config)
    if a.coating:
        cfg["conformal_coating"] = True

    board_path = Path(a.board)
    print(f"[1/4] Reading {board_path.name} ...", flush=True)
    b = parse_board(board_path)
    infos = derive_layers(b, cfg)

    if a.no_dru:
        print("[2/4] Rule file skipped")
    else:
        dru_path = Path(a.dru) if a.dru else board_path.with_suffix(".kicad_dru")
        if dru_path.exists() and not a.force:
            print(f"[2/4] Rule file kept (already exists, use --force to overwrite): {dru_path}")
        else:
            dru_path.write_text(build_dru(b, cfg, infos), encoding="utf-8")
            print(f"[2/4] Rule file written: {dru_path}")

    rc = 0
    if a.no_checks:
        print("[3/4] Checks skipped")
    else:
        print("[3/4] Running checks ...", flush=True)
        findings = Checker(b, cfg, infos).run()
        if a.report:
            rep = Path(a.report)
        else:
            fmt = "%d%m%Y_%H-%M-%S" if os.name == "nt" else "%d%m%Y_%H:%M:%S"   # ':' not allowed on Windows
            rep = board_path.resolve().parent / "ecss_check_results" / f"ecss_rule_check_{datetime.now().strftime(fmt)}.txt"
        rep.parent.mkdir(parents=True, exist_ok=True)
        cnt = write_report(rep, b, cfg, infos, findings)
        print(f"[4/4] Done: {cnt['ERROR']} error(s), {cnt['WARNING']} warning(s) -> {rep}")
        rc = 1 if cnt["ERROR"] else 0
    return rc


if __name__ == "__main__":
    sys.exit(main())
