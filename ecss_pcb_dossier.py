#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ecss_pcb_dossier.py : remplit un template ODT de dossier de définition PCB
à partir d'un projet KiCad (fichier .kicad_pcb).

Ce que fait le script
---------------------
1. Lit le .kicad_pcb (parseur S-expression intégré) : stackup, nombre de
   couches cuivre, finition, épaisseur, dimensions carte, cartouche.
2. Exporte l'artwork de chaque couche demandée en SVG avec `kicad-cli`
   (une couche + le contour Edge.Cuts par image).
3. Remplace les marqueurs {{...}} du template ODT par :
     - du texte (valeurs simples),
     - des images (artworks),
     - un tableau (stackup).
   Le résultat est un nouvel ODT ; le template n'est jamais modifié.

Marqueurs reconnus dans le template
-----------------------------------
Marqueurs de bloc (le marqueur doit être SEUL dans son paragraphe) :
    {{stackup}}                   tableau du stackup
    
    {{artwork:F.Cu}}              image de la couche F.Cu (nom canonique KiCad)
    
    {{artwork:In1.Cu|width=120}}  idem, largeur imposée en mm
    
    {{artworks}}                  toutes les couches cuivre + masques + sérigraphies,
                                  chacune précédée d'un titre/légende
    
    {{artwork.mechanical_data}}   image composite Edge.Cuts + F.Paste + Dwgs.User
                                  (= User.Drawings, cotations du PCB) ; d'autres
                                  composites via --composite NOM=COUCHE1,COUCHE2
                                  puis {{artwork.NOM}} ; largeur : |width=120
                                  Cette image est en NOIR ET BLANC par défaut, le
                                  reste de l'export suit le thème KiCad par défaut.
    
    {{drilling_drawing}}          plan de perçage généré par le script (pas par
                                  kicad-cli) : contour Edge.Cuts + un symbole par
                                  perçage (pad traversant, NPTH, via). Les perçages
                                  de même diamètre et même statut plaqué/non plaqué
                                  partagent un symbole.
    
    {{drilling_table}}            tableau "Through Holes" associé : symbole,
                                  diamètre (mm), plaqué (Oui/Non), quantité - même
                                  regroupement que {{drilling_drawing}}, donc les
                                  symboles correspondent entre l'image et le tableau
                                  même si un seul des deux marqueurs est utilisé.
                                  NON généré : les bulles "Note 1/2/3" de cotation
                                  particulière (pas une donnée KiCad).

    {{checklist}}                 tableau checklist des fichiers de fabrication
                                  (Gerber et perçage) avec nom du fichier, taille,
                                  date de dernière modification et CRC32.
    
    Options par image (après « | », séparées par des virgules) :
        width=120   largeur en mm        bw=1 / bw=0   noir et blanc oui / non
        theme=NOM   thème KiCad de cette image (ex. {{artwork:F.Cu|theme=KiCad Classic}})
    
    Options supplémentaires pour {{drilling_drawing}} :
        symbol_size=1.2   taille des symboles en mm [--symbol-size-mm]
        margin=10         marge autour du contour, en mm [--drilling-margin-mm]

Marqueurs en ligne (peuvent être au milieu d'une phrase, dans un tableau,
dans un en-tête ou un pied de page) :
    {{project_name}} {{pcb_file}} {{title}} {{rev}} {{company}} {{date}}
    {{kicad_version}} {{copper_layers}} {{board_thickness}} {{copper_finish}}
    {{board_width}} {{board_height}} {{board_size}}

Dépendances : Python >= 3.9, lxml (pip install lxml), kicad-cli (KiCad >= 8,
inclus avec KiCad 10). Optionnel : cairosvg pour --image-format png.

Exemples
--------
    python ecss_pcb_dossier.py carte.kicad_pcb template.odt -o dossier.odt --gerber Gerber/
    python ecss_pcb_dossier.py carte.kicad_pcb template.odt -o dossier.odt --lang en \
        --image-width 150 --theme "KiCad Classic"
    # Réutiliser des SVG déjà exportés (dans build/artworks/F_Cu.svg, ...):
    python ecss_pcb_dossier.py carte.kicad_pcb template.odt -o dossier.odt --skip-export
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import re
import shutil
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

try:
    from lxml import etree
except ImportError:  # pragma: no cover
    sys.exit("lxml est requis : pip install lxml")

# --------------------------------------------------------------------------- #
# Espaces de noms ODF
# --------------------------------------------------------------------------- #
NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "manifest": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
}


def q(prefix_tag: str) -> str:
    """'text:p' -> '{ns}p' (notation de Clark)."""
    prefix, tag = prefix_tag.split(":")
    return f"{{{NS[prefix]}}}{tag}"


# --------------------------------------------------------------------------- #
# Libellés (fr / en)
# --------------------------------------------------------------------------- #
LABELS = {
    "fr": {
        "no": "N°", "layer": "Couche", "type": "Type", "material": "Matériau",
        "thickness": "Épaisseur (mm)", "er": "εr", "tand": "tan δ",
        "total": "Épaisseur totale (cuivre + diélectriques)",
        "artwork": "Artwork de la couche", "figure": "Figure", "sep": " : ",
        "no_stackup": "Stackup non défini dans le projet KiCad.",
        "drilling_title": "Perçages (Through Holes)",
        "symbol": "Symbole", "diameter_mm": "Diamètre (mm)", "plated": "Plaqué",
        "quantity": "Quantité", "yes": "Oui", "no": "Non",
        "no_holes": "Aucun perçage trouvé dans le projet KiCad.",
        "checklist_title": "Fichiers de fabrication (Gerber / perçage)",
        "file": "Fichier", "size": "Taille", "modified": "Dernière modification",
        "crc32": "CRC32",
        "no_gerber_dir": "Dossier Gerber introuvable ou non fourni (--gerber).",
        "no_gerber_files": "Aucun fichier trouvé dans le dossier Gerber.",
        "byte_unit": "o", "kb_unit": "Ko", "mb_unit": "Mo",
        "types": {
            "copper": "Cuivre", "core": "Âme (core)", "prepreg": "Préimprégné",
            "top solder mask": "Vernis épargne (dessus)",
            "bottom solder mask": "Vernis épargne (dessous)",
            "top silk screen": "Sérigraphie (dessus)",
            "bottom silk screen": "Sérigraphie (dessous)",
            "top solder paste": "Crème à braser (dessus)",
            "bottom solder paste": "Crème à braser (dessous)",
        },
    },
    "en": {
        "no": "No.", "layer": "Layer", "type": "Type", "material": "Material",
        "thickness": "Thickness (mm)", "er": "εr", "tand": "tan δ",
        "total": "Total thickness (copper + dielectrics)",
        "artwork": "Artwork of layer", "figure": "Figure", "sep": ": ",
        "no_stackup": "No stackup defined in the KiCad project.",
        "drilling_title": "Through Holes",
        "symbol": "Symbol", "diameter_mm": "Diameter (mm)", "plated": "Plated",
        "quantity": "Quantity", "yes": "Yes", "no": "No",
        "no_holes": "No drilled hole found in the KiCad project.",
        "checklist_title": "Fabrication files (Gerber / drill)",
        "file": "File", "size": "Size", "modified": "Last modified",
        "crc32": "CRC32",
        "no_gerber_dir": "Gerber folder not found or not provided (--gerber).",
        "no_gerber_files": "No file found in the Gerber folder.",
        "byte_unit": "B", "kb_unit": "KB", "mb_unit": "MB",
        "types": {
            "copper": "Copper", "core": "Core", "prepreg": "Prepreg",
            "top solder mask": "Solder mask (top)",
            "bottom solder mask": "Solder mask (bottom)",
            "top silk screen": "Silkscreen (top)",
            "bottom silk screen": "Silkscreen (bottom)",
            "top solder paste": "Solder paste (top)",
            "bottom solder paste": "Solder paste (bottom)",
        },
    },
}

# --------------------------------------------------------------------------- #
# Parseur S-expression (format .kicad_pcb)
# --------------------------------------------------------------------------- #
_TOKEN = re.compile(r'\(|\)|"((?:[^"\\]|\\.)*)"|[^\s()"]+')


def parse_sexpr(text: str) -> list:
    """Retourne l'arbre sous forme de listes imbriquées ; atomes et chaînes -> str."""
    stack: list[list] = [[]]
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        if tok == "(":
            new: list = []
            stack[-1].append(new)
            stack.append(new)
        elif tok == ")":
            if len(stack) > 1:
                stack.pop()
        elif m.group(1) is not None:
            stack[-1].append(re.sub(r"\\(.)", r"\1", m.group(1)))
        else:
            stack[-1].append(tok)
    return stack[0][0] if stack[0] else []


def find(node: list, tag: str):
    for c in node:
        if isinstance(c, list) and c and c[0] == tag:
            return c
    return None


def find_all(node: list, tag: str):
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]


def to_float(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Extraction des données du PCB
# --------------------------------------------------------------------------- #
def parse_stackup(root: list):
    setup = find(root, "setup")
    st = find(setup, "stackup") if setup else None
    if not st:
        return None, None
    layers = []
    for n in find_all(st, "layer"):
        d = {"name": n[1] if len(n) > 1 else "", "thickness": 0.0}
        in_sub = False
        for c in n[2:]:
            if isinstance(c, str):
                if c == "addsublayer":
                    in_sub = True  # sous-couches : on cumule les épaisseurs
                continue
            tag = c[0]
            if tag == "thickness":
                v = to_float(c[1] if len(c) > 1 else None, 0.0)
                d["thickness"] = d["thickness"] + v if in_sub else v
            elif tag in ("epsilon_r", "loss_tangent"):
                if not in_sub:
                    d[tag] = to_float(c[1] if len(c) > 1 else None)
            elif tag in ("type", "material", "color"):
                if not in_sub:
                    d[tag] = c[1] if len(c) > 1 else ""
        layers.append(d)
    finish = find(st, "copper_finish")
    return layers, (finish[1] if finish and len(finish) > 1 else None)


def board_layers(root: list):
    """Liste (nom canonique, type) depuis le bloc (layers ...)."""
    out = []
    node = find(root, "layers")
    if node:
        for c in node:
            if isinstance(c, list) and len(c) >= 3:
                out.append((c[1], c[2]))
    return out


def _arc_points(start, mid, end, n=64):
    """Points échantillonnés d'un arc défini par 3 points (bbox du contour)."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [start, end]
    ux = ((x1**2 + y1**2) * (y2 - y3) + (x2**2 + y2**2) * (y3 - y1)
          + (x3**2 + y3**2) * (y1 - y2)) / d
    uy = ((x1**2 + y1**2) * (x3 - x2) + (x2**2 + y2**2) * (x1 - x3)
          + (x3**2 + y3**2) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a0, a1, a2 = (math.atan2(y - uy, x - ux) for (x, y) in (start, mid, end))
    tau = 2 * math.pi
    ccw = ((a1 - a0) % tau) < ((a2 - a0) % tau)
    sweep = ((a2 - a0) % tau) if ccw else -((a0 - a2) % tau)
    return [(ux + r * math.cos(a0 + sweep * i / n), uy + r * math.sin(a0 + sweep * i / n))
            for i in range(n + 1)]


def _xy(node, tag):
    c = find(node, tag)
    if c and len(c) >= 3:
        return (float(c[1]), float(c[2]))
    return None


def rotk(x: float, y: float, deg: float) -> tuple[float, float]:
    """Rotation KiCad (axe Y vers le bas, angle positif = sens anti-horaire à l'écran)."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, y * c - x * s


def board_outline_segments(root: list):
    """Éléments de dessin du contour (Edge.Cuts), en mm, sous forme de primitives
    ('line', [p1, p2]) / ('polyline', [pts]) / ('circle', (cx, cy, r)) ainsi que la
    liste de tous les points (pour le calcul de la boîte englobante)."""
    prims = []
    pts_all = []
    for n in root:
        if not (isinstance(n, list) and n and n[0] in
                ("gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly", "gr_curve")):
            continue
        layer = find(n, "layer")
        if not layer or len(layer) < 2 or layer[1] != "Edge.Cuts":
            continue
        try:
            if n[0] == "gr_line":
                s, e = _xy(n, "start"), _xy(n, "end")
                if s and e:
                    prims.append(("line", [s, e]))
                    pts_all += [s, e]
            elif n[0] == "gr_rect":
                s, e = _xy(n, "start"), _xy(n, "end")
                if s and e:
                    poly = [s, (e[0], s[1]), e, (s[0], e[1]), s]
                    prims.append(("polyline", poly))
                    pts_all += poly
            elif n[0] == "gr_arc":
                s, m, e = _xy(n, "start"), _xy(n, "mid"), _xy(n, "end")
                if s and m and e:
                    arc = _arc_points(s, m, e)
                    prims.append(("polyline", arc))
                    pts_all += arc
            elif n[0] == "gr_circle":
                c, e = _xy(n, "center"), _xy(n, "end")
                if c and e:
                    r = math.hypot(e[0] - c[0], e[1] - c[1])
                    prims.append(("circle", (c[0], c[1], r)))
                    pts_all += [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
            elif n[0] in ("gr_poly", "gr_curve"):
                pl = find(n, "pts")
                if pl:
                    poly = [(float(p[1]), float(p[2])) for p in find_all(pl, "xy")]
                    if len(poly) >= 2:
                        prims.append(("polyline", poly + [poly[0]]))
                        pts_all += poly
        except (ValueError, IndexError):
            continue
    return prims, pts_all


def board_holes(root: list):
    """Perçages du PCB : pastilles traversantes (plaquées ou NPTH) de chaque
    empreinte, et vias. Retourne une liste de dicts {x, y, drill, plated} en mm."""
    holes = []
    for fn in find_all(root, "footprint"):
        at = find(fn, "at")
        if not at or len(at) < 3:
            continue
        fx, fy = to_float(at[1], 0.0), to_float(at[2], 0.0)
        fr = to_float(at[3], 0.0) if len(at) > 3 else 0.0
        for pn in find_all(fn, "pad"):
            if len(pn) < 3 or pn[2] not in ("thru_hole", "np_thru_hole"):
                continue
            pat = find(pn, "at")
            if not pat or len(pat) < 3:
                continue
            px, py = to_float(pat[1], 0.0), to_float(pat[2], 0.0)
            rx, ry = rotk(px, py, fr)
            dr = find(pn, "drill")
            if not dr:
                continue
            drill_vals = [to_float(x, None) for x in dr[1:] if isinstance(x, str)
                          and re.match(r"^[0-9.]+$", x)]
            drill_vals = [v for v in drill_vals if v]
            if not drill_vals:
                continue
            holes.append({"x": fx + rx, "y": fy + ry, "drill": max(drill_vals),
                          "plated": pn[2] == "thru_hole"})
    for vn in find_all(root, "via"):
        at = find(vn, "at")
        dr = find(vn, "drill")
        if not at or len(at) < 3 or not dr or len(dr) < 2:
            continue
        d = to_float(dr[1])
        if not d:
            continue
        holes.append({"x": to_float(at[1], 0.0), "y": to_float(at[2], 0.0),
                      "drill": d, "plated": True})
    return holes


def collect_pcb_data(pcb_path: Path) -> dict:
    root = parse_sexpr(pcb_path.read_text(encoding="utf-8"))
    layers = board_layers(root)
    copper = [n for n, t in layers if n.endswith(".Cu")]
    stackup, finish = parse_stackup(root)
    general = find(root, "general")
    thick = None
    if general:
        t = find(general, "thickness")
        thick = to_float(t[1]) if t and len(t) > 1 else None
    tb = find(root, "title_block") or []

    def tb_val(tag):
        c = find(tb, tag)
        return c[1] if c and len(c) > 1 else ""

    gv = find(root, "generator_version")
    outline_prims, outline_pts = board_outline_segments(root)
    bbox = None
    if outline_pts:
        xs, ys = [p[0] for p in outline_pts], [p[1] for p in outline_pts]
        bbox = (max(xs) - min(xs), max(ys) - min(ys))
    return {
        "root": root, "layers": layers, "copper_layers": copper,
        "stackup": stackup, "copper_finish": finish, "thickness": thick,
        "bbox": bbox, "title": tb_val("title"), "rev": tb_val("rev"),
        "company": tb_val("company"),
        "kicad_version": gv[1] if gv and len(gv) > 1 else "",
        "outline_prims": outline_prims, "outline_pts": outline_pts,
        "holes": board_holes(root),
    }


def fmt_num(v, nd=2):
    if v is None:
        return ""
    s = f"{v:.{nd}f}".rstrip("0").rstrip(".")
    return s or "0"


def build_values(pcb_path: Path, data: dict) -> dict:
    bbox = data["bbox"]
    w, h = (fmt_num(bbox[0]), fmt_num(bbox[1])) if bbox else ("", "")
    return {
        "project_name": pcb_path.stem,
        "pcb_file": pcb_path.name,
        "title": data["title"],
        "rev": data["rev"],
        "company": data["company"],
        "date": dt.date.today().strftime("%d/%m/%Y"),
        "kicad_version": data["kicad_version"],
        "copper_layers": str(len(data["copper_layers"])),
        "board_thickness": fmt_num(data["thickness"]),
        "copper_finish": data["copper_finish"] or "",
        "board_width": w,
        "board_height": h,
        "board_size": f"{w} × {h} mm" if bbox else "",
    }

def collect_gerber_files(gerber_dir: Path | None) -> list[dict] | None:
    """Fichiers du dossier Gerber (non récursif, triés par nom), avec taille, date
    de dernière modification et CRC32. Retourne None si le dossier est absent ou
    non fourni (distinct d'une liste vide, qui signifie « dossier vide »)."""
    if gerber_dir is None or not gerber_dir.is_dir():
        return None
    out = []
    for p in sorted(gerber_dir.iterdir(), key=lambda p: p.name.lower()):
        if not p.is_file():
            continue
        data = p.read_bytes()
        out.append({
            "name": p.name,
            "size": len(data),
            "mtime": dt.datetime.fromtimestamp(p.stat().st_mtime),
            "crc32": f"{zlib.crc32(data) & 0xFFFFFFFF:08X}",
        })
    return out

# --------------------------------------------------------------------------- #
# Export des artworks avec kicad-cli
# --------------------------------------------------------------------------- #
def find_kicad_cli(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    env = os.environ.get("KICAD_CLI")
    if env:
        return env
    found = shutil.which("kicad-cli")
    if found:
        return found
    for p in (r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe",
              r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe",
              "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"):
        if Path(p).exists():
            return p
    return None


def layer_file_stem(layer: str) -> str:
    return layer.replace(".", "_")


def export_layers_svg(cli: str, pcb: Path, layers: list[str], out: Path, args,
                      bw: bool = False, theme: str | None = None) -> None:
    """Exporte UNE image SVG regroupant toutes les couches de `layers`."""
    layers_arg = ",".join(layers)
    base = [cli, "pcb", "export", "svg", "--layers", layers_arg, "--mode-single",
            "--exclude-drawing-sheet", "-o", str(out)]
    if bw:
        base.append("--black-and-white")
    if theme:
        base += ["--theme", theme]
    if args.mirror_bottom and all(l.startswith("B.") or l == "Edge.Cuts" for l in layers):
        base.append("--mirror")
    # Le cadrage sur la carte a changé de syntaxe selon les versions : on tente
    # plusieurs variantes, de la plus probable à la plus permissive.
    variants = [["--page-size-mode", str(args.page_size_mode)], ["--fit-page-to-board"], []]
    last_err = ""
    for extra in variants:
        cmd = base + extra + [str(pcb)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 and out.exists():
            return
        last_err = (res.stderr or res.stdout).strip()
    raise RuntimeError(
        f"Échec de kicad-cli pour {layers_arg} :\n{last_err}\n"
        "Vérifiez les options avec : kicad-cli pcb export svg --help")


def svg_size_mm(path: Path) -> tuple[float, float]:
    """Dimensions (mm) d'un SVG ; retombe sur la viewBox (supposée en mm)."""
    head = path.read_text(encoding="utf-8", errors="ignore")[:4000]
    unit = {"mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72, "px": 25.4 / 96, "": 25.4 / 96}

    def dim(attr):
        m = re.search(rf'\b{attr}="([\d.]+)\s*([a-z%]*)"', head)
        if m and m.group(2) != "%":
            return float(m.group(1)) * unit.get(m.group(2), 1.0)
        return None

    w, h = dim("width"), dim("height")
    if w and h:
        return w, h
    m = re.search(r'viewBox="[-\d.]+[ ,]+[-\d.]+[ ,]+([\d.]+)[ ,]+([\d.]+)"', head)
    if m:
        return float(m.group(1)), float(m.group(2))
    return 100.0, 100.0

def add_svg_margin(path: Path, margin_mm: float) -> None:
    """Élargit le cadrage d'un SVG de `margin_mm` sur chaque côté. KiCad écrit tous les
    éléments dans le fichier ; seul le viewBox masque ceux qui dépassent de la carte."""
    txt = path.read_text(encoding="utf-8")
    tag_m = re.search(r"<svg\b[^>]*>", txt)
    if not tag_m:
        return
    tag = tag_m.group(0)
    w = re.search(r'(?<![-\w])width="([\d.]+)mm"', tag)
    h = re.search(r'(?<![-\w])height="([\d.]+)mm"', tag)
    vb = re.search(r'viewBox="(-?[\d.]+)[ ,]+(-?[\d.]+)[ ,]+([\d.]+)[ ,]+([\d.]+)"', tag)
    if not (w and h and vb):
        print(f"ATTENTION : marge non appliquée à {path.name} (width/height en mm ou viewBox absents)",
              file=sys.stderr)
        return
    w_mm, h_mm = float(w.group(1)), float(h.group(1))
    x, y, vw, vh = map(float, vb.groups())
    m = margin_mm * vw / w_mm            # marge exprimée en unités du viewBox
    new = tag.replace(w.group(0), f'width="{w_mm + 2 * margin_mm:.4f}mm"')
    new = new.replace(h.group(0), f'height="{h_mm + 2 * margin_mm:.4f}mm"')
    new = new.replace(vb.group(0),
                      f'viewBox="{x - m:.4f} {y - m:.4f} {vw + 2 * m:.4f} {vh + 2 * m:.4f}"')
    path.write_text(txt.replace(tag, new, 1), encoding="utf-8")


# Marge (mm) autour de la carte, uniquement pour ces composites (surchargeable : |margin=20)
DEFAULT_MARGIN = {"mechanical_data": 15.0}

_EXPORTED: set[str] = set()


def prepare_artwork(name: str, layers: list[str], pcb: Path, artdir: Path, args, cli,
                    bw: bool = False, theme: str | None = None, margin_mm: float = 0.0) -> Path:
    """Retourne le chemin de l'image (svg ou png) demandée, en l'exportant si besoin.
    `name` sert de nom de fichier ; `layers` = couches KiCad à superposer."""
    stem = layer_file_stem(name) + ("_bw" if bw else "")
    if theme:
        stem += "_" + re.sub(r"\W+", "", theme)
    svg = artdir / f"{stem}.svg"
    if stem not in _EXPORTED and (not args.skip_export or not svg.exists()):
        _EXPORTED.add(stem)
        if not cli:
            raise RuntimeError(
                "kicad-cli introuvable. Utilisez --kicad-cli CHEMIN ou la variable "
                "d'environnement KICAD_CLI, ou --skip-export avec des SVG existants.")
        print(f"  export {name} ({','.join(layers)}) ...")
        export_layers_svg(cli, pcb, layers, svg, args, bw, theme)
        if margin_mm:
            add_svg_margin(svg, margin_mm)
    if args.image_format == "png":
        try:
            import cairosvg  # type: ignore
        except ImportError:
            sys.exit("--image-format png nécessite cairosvg : pip install cairosvg")
        png = svg.with_suffix(".png")
        cairosvg.svg2png(url=str(svg), write_to=str(png), dpi=300)
        return png
    return svg


# --------------------------------------------------------------------------- #
# Manipulation ODT
# --------------------------------------------------------------------------- #
BLOCK_RE = re.compile(r"^\s*\{\{\s*(stackup|artworks|artwork|drilling_drawing|drilling_table"
                      r"|checklist)(?:\.([A-Za-z_]\w*))?\s*(?::\s*([^}|]+?))?\s*"
                      r"(?:\|\s*([^}]*?))?\s*\}\}\s*$")
INLINE_RE = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}")

STYLE_PREFIX = "PCBDoc"

def paragraph_text(p) -> str:
    return "".join(_slot_text(e, a) for e, a in collect_slots(p))


def _slot_text(el, attr) -> str:
    return getattr(el, attr) or ""


def collect_slots(p):
    """Emplacements de texte (élément, 'text'|'tail') d'un paragraphe, dans l'ordre.
    On ne descend que dans text:span et text:a (pas dans les cadres, notes, etc.)."""
    slots = []

    def walk(el):
        if el.text is not None or el is p:
            slots.append((el, "text"))
        for ch in el:
            if ch.tag in (q("text:span"), q("text:a")):
                walk(ch)
            if ch.tail is not None:
                slots.append((ch, "tail"))

    walk(p)
    return slots


def replace_inline(p, values: dict, unknown: set) -> int:
    """Remplace les {{clé}} d'un paragraphe, même si LibreOffice les a coupés en
    plusieurs runs (text:span). Retourne le nombre de remplacements."""
    slots = collect_slots(p)
    full = "".join(_slot_text(e, a) for e, a in slots)
    if "{{" not in full:
        return 0
    starts, pos = [], 0
    for e, a in slots:
        starts.append(pos)
        pos += len(_slot_text(e, a))
    count = 0
    for m in reversed(list(INLINE_RE.finditer(full))):
        key = m.group(1)
        if key not in values:
            unknown.add(key)
            continue
        s, e_ = m.span()
        first = True
        for i, (el, a) in enumerate(slots):
            t = _slot_text(el, a)
            a0, a1 = starts[i], starts[i] + len(t)
            if a1 <= s or a0 >= e_:
                continue
            lo, hi = max(s, a0) - a0, min(e_, a1) - a0
            setattr(el, a, t[:lo] + (values[key] if first else "") + t[hi:])
            first = False
        count += 1
    return count


def ensure_styles(content_root) -> None:
    """Ajoute les styles automatiques utilisés par les tableaux et images générés."""
    auto = content_root.find(q("office:automatic-styles"))
    if auto is None:
        auto = etree.Element(q("office:automatic-styles"))
        # doit précéder office:body
        body = content_root.find(q("office:body"))
        body.addprevious(auto)
    existing = {s.get(q("style:name")) for s in auto}
    if f"{STYLE_PREFIX}_Tbl" in existing:
        return
    xml = f"""
    <root xmlns:style="{NS['style']}" xmlns:fo="{NS['fo']}" xmlns:text="{NS['text']}"
          xmlns:table="{NS['table']}" xmlns:svg="{NS['svg']}" xmlns:draw="{NS['draw']}">
      <style:style style:name="{STYLE_PREFIX}_Tbl" style:family="table">
        <style:table-properties style:width="16cm" table:align="left"
                                fo:margin-top="0.2cm" fo:margin-bottom="0.3cm"/>
      </style:style>
      <style:style style:name="{STYLE_PREFIX}_Col" style:family="table-column">
        <style:table-column-properties style:column-width="2.42cm"/>
      </style:style>
      <style:style style:name="{STYLE_PREFIX}_ColNarrow" style:family="table-column">
        <style:table-column-properties style:column-width="1.2cm"/>
      </style:style>
      <style:style style:name="{STYLE_PREFIX}_ColWide" style:family="table-column">
        <style:table-column-properties style:column-width="3.6cm"/>
      </style:style>
      <style:style style:name="{STYLE_PREFIX}_Cell" style:family="table-cell">
        <style:table-cell-properties fo:padding="0.08cm" fo:border="0.5pt solid #000000"/>
      </style:style>
      <style:style style:name="{STYLE_PREFIX}_CellHead" style:family="table-cell">
        <style:table-cell-properties fo:padding="0.08cm" fo:border="0.5pt solid #000000"
                                     fo:background-color="#d9d9d9"/>
      </style:style>
      <style:style style:name="{STYLE_PREFIX}_Txt" style:family="paragraph">
        <style:paragraph-properties fo:text-align="center"/>
        <style:text-properties fo:font-size="9pt"/>
      </style:style>
      <style:style style:name="{STYLE_PREFIX}_TxtHead" style:family="paragraph">
        <style:paragraph-properties fo:text-align="center"/>
        <style:text-properties fo:font-size="9pt" fo:font-weight="bold"/>
      </style:style>
      <style:style style:name="{STYLE_PREFIX}_Img" style:family="paragraph">
        <style:paragraph-properties fo:text-align="center" fo:margin-top="0.2cm"
                                    fo:margin-bottom="0.2cm"/>
      </style:style>
      <style:style style:name="{STYLE_PREFIX}_Cap" style:family="paragraph">
        <style:paragraph-properties fo:text-align="center" fo:keep-together="always"/>
        <style:text-properties fo:font-style="italic"/>
      </style:style>
      <style:style style:name="{STYLE_PREFIX}_Frame" style:family="graphic">
        <style:graphic-properties text:anchor-type="as-char" svg:x="0cm" svg:y="0cm"
              style:wrap="none" style:vertical-pos="top" style:vertical-rel="baseline"/>
      </style:style>
    </root>"""
    for s in list(etree.fromstring(xml)):
        auto.append(s)


def make_par(text: str, style: str):
    p = etree.Element(q("text:p"))
    p.set(q("text:style-name"), style)
    p.text = text
    return p


def build_stackup_table(stackup, finish, lang: str, full: bool):
    L = LABELS[lang]
    if not stackup:
        return [make_par(L["no_stackup"], f"{STYLE_PREFIX}_Txt")]
    rows = []
    total = 0.0
    idx = 0
    for layer in stackup:
        ltype = (layer.get("type") or "").lower()
        is_cu = ltype == "copper"
        is_diel = ltype in ("core", "prepreg") or layer["name"].lower().startswith("dielectric")
        is_mask = "solder mask" in ltype
        if not (is_cu or is_diel or is_mask or full):
            continue
        if is_cu or is_diel:
            total += layer["thickness"]
        idx += 1
        rows.append([
            str(idx), layer["name"],
            L["types"].get(ltype, layer.get("type", "")),
            layer.get("material", "") or "",
            fmt_num(layer["thickness"], 4)
        ])
    header = [L["no"], L["layer"], L["type"], L["material"], L["thickness"]]
    col_styles = ["_ColNarrow", "_Col", "_ColWide", "_Col", "_Col"]

    tbl = etree.Element(q("table:table"))
    tbl.set(q("table:name"), "PCB_Stackup")
    tbl.set(q("table:style-name"), f"{STYLE_PREFIX}_Tbl")
    for cs in col_styles:
        col = etree.SubElement(tbl, q("table:table-column"))
        col.set(q("table:style-name"), STYLE_PREFIX + cs)

    def add_row(parent, cells, head=False, span_first=None):
        tr = etree.SubElement(parent, q("table:table-row"))
        i = 0
        while i < len(cells):
            tc = etree.SubElement(tr, q("table:table-cell"))
            tc.set(q("table:style-name"), f"{STYLE_PREFIX}_{'CellHead' if head else 'Cell'}")
            tc.set(q("office:value-type"), "string")
            if span_first and i == 0:
                tc.set(q("table:number-columns-spanned"), str(span_first))
            tc.append(make_par(cells[i], f"{STYLE_PREFIX}_{'TxtHead' if head else 'Txt'}"))
            i += 1
            if span_first and i == 1:
                for _ in range(span_first - 1):
                    etree.SubElement(tr, q("table:covered-table-cell"))
        return tr

    hdr = etree.SubElement(tbl, q("table:table-header-rows"))
    add_row(hdr, header, head=True)
    for r in rows:
        add_row(tbl, r)
    add_row(tbl, [L["total"], fmt_num(total, 4)], head=True, span_first=4)
    # La ligne total : cellule étalée sur 4 colonnes + valeur ; on complète à 7 colonnes.
    #last = tbl[-1]
    #for _ in range(2):
    #    tc = etree.SubElement(last, q("table:table-cell"))
    #    tc.set(q("table:style-name"), f"{STYLE_PREFIX}_Cell")
    #    tc.set(q("office:value-type"), "string")
    #    tc.append(make_par("", f"{STYLE_PREFIX}_Txt"))
    return [tbl]


# --------------------------------------------------------------------------- #
# Checklist des fichiers de fabrication (Gerber / perçage)
# --------------------------------------------------------------------------- #
def collect_gerber_files(gerber_dir: Path | None) -> list[dict] | None:
    """Fichiers du dossier Gerber (non récursif, triés par nom), avec taille, date
    de dernière modification et CRC32. Retourne None si le dossier est absent ou
    non fourni (distinct d'une liste vide, qui signifie « dossier vide »)."""
    if gerber_dir is None or not gerber_dir.is_dir():
        return None
    out = []
    for p in sorted(gerber_dir.iterdir(), key=lambda p: p.name.lower()):
        if not p.is_file():
            continue
        data = p.read_bytes()
        out.append({
            "name": p.name,
            "size": len(data),
            "mtime": dt.datetime.fromtimestamp(p.stat().st_mtime),
            "crc32": f"{zlib.crc32(data) & 0xFFFFFFFF:08X}",
        })
    return out


def format_size(n: int, lang: str) -> str:
    L = LABELS[lang]
    if n < 1024:
        return f"{n} {L['byte_unit']}"
    kb = n / 1024
    if kb < 1024:
        return f"{fmt_num(kb, 1)} {L['kb_unit']}"
    return f"{fmt_num(kb / 1024, 2)} {L['mb_unit']}"


def build_checklist_table(files: list[dict] | None, lang: str):
    L = LABELS[lang]
    if files is None:
        return [make_par(L["no_gerber_dir"], f"{STYLE_PREFIX}_Txt")]
    if not files:
        return [make_par(L["no_gerber_files"], f"{STYLE_PREFIX}_Txt")]
    header = [L["file"], L["size"], L["modified"], L["crc32"]]
    col_styles = ["_ColWide", "_ColNarrow", "_Col", "_ColNarrow"]

    tbl = etree.Element(q("table:table"))
    tbl.set(q("table:name"), "PCB_Checklist")
    tbl.set(q("table:style-name"), f"{STYLE_PREFIX}_Tbl")
    for cs in col_styles:
        col = etree.SubElement(tbl, q("table:table-column"))
        col.set(q("table:style-name"), STYLE_PREFIX + cs)

    def cell(parent, text, head=False, span=None):
        tc = etree.SubElement(parent, q("table:table-cell"))
        tc.set(q("table:style-name"), f"{STYLE_PREFIX}_{'CellHead' if head else 'Cell'}")
        tc.set(q("office:value-type"), "string")
        if span:
            tc.set(q("table:number-columns-spanned"), str(span))
        tc.append(make_par(text, f"{STYLE_PREFIX}_{'TxtHead' if head else 'Txt'}"))
        return tc

    tr = etree.SubElement(tbl, q("table:table-row"))
    cell(tr, L["checklist_title"], head=True, span=len(header))
    for _ in range(len(header) - 1):
        etree.SubElement(tr, q("table:covered-table-cell"))

    hdr = etree.SubElement(tbl, q("table:table-header-rows"))
    tr = etree.SubElement(hdr, q("table:table-row"))
    for htext in header:
        cell(tr, htext, head=True)

    for f in files:
        tr = etree.SubElement(tbl, q("table:table-row"))
        cell(tr, f["name"])
        cell(tr, format_size(f["size"], lang))
        cell(tr, f["mtime"].strftime("%d/%m/%Y %H:%M"))
        cell(tr, f["crc32"])
    return [tbl]


def build_image_paragraph(href: str, name: str, w_mm: float, h_mm: float):
    p = etree.Element(q("text:p"))
    p.set(q("text:style-name"), f"{STYLE_PREFIX}_Img")
    fr = etree.SubElement(p, q("draw:frame"))
    fr.set(q("draw:style-name"), f"{STYLE_PREFIX}_Frame")
    fr.set(q("draw:name"), name)
    fr.set(q("text:anchor-type"), "as-char")
    fr.set(q("svg:width"), f"{w_mm:.2f}mm")
    fr.set(q("svg:height"), f"{h_mm:.2f}mm")
    fr.set(q("draw:z-index"), "0")
    im = etree.SubElement(fr, q("draw:image"))
    im.set(q("xlink:href"), href)
    im.set(q("xlink:type"), "simple")
    im.set(q("xlink:show"), "embed")
    im.set(q("xlink:actuate"), "onLoad")
    return p

# --------------------------------------------------------------------------- #
# Plan de perçage ("drilling drawing") - dessiné par le script, pas par kicad-cli
# --------------------------------------------------------------------------- #
def _hole_symbol_markup(idx: int, s: float) -> str:
    """Symbole vectoriel (chemins centrés sur 0,0) pour le groupe de perçage `idx`.
    14 symboles distincts, puis on boucle (avec avertissement du code appelant)."""
    sw = max(s * 0.16, 0.025)
    k = idx % 14
    BLK = "#000000"
    if k == 0:    # losange plein
        return f'<polygon points="0,{-s:.3f} {s:.3f},0 0,{s:.3f} {-s:.3f},0" fill="{BLK}"/>'
    if k == 1:    # carré ouvert
        return (f'<rect x="{-s:.3f}" y="{-s:.3f}" width="{2*s:.3f}" height="{2*s:.3f}" '
               f'fill="none" stroke="{BLK}" stroke-width="{sw:.3f}"/>')
    if k == 2:    # triangle ouvert
        return (f'<polygon points="0,{-s:.3f} {s*0.87:.3f},{s*0.5:.3f} {-s*0.87:.3f},{s*0.5:.3f}" '
               f'fill="none" stroke="{BLK}" stroke-width="{sw:.3f}"/>')
    if k == 3:    # carré plein
        return f'<rect x="{-s:.3f}" y="{-s:.3f}" width="{2*s:.3f}" height="{2*s:.3f}" fill="{BLK}"/>'
    if k == 4:    # losange ouvert
        return (f'<polygon points="0,{-s:.3f} {s:.3f},0 0,{s:.3f} {-s:.3f},0" '
               f'fill="none" stroke="{BLK}" stroke-width="{sw:.3f}"/>')
    if k == 5:    # cercle ouvert
        return f'<circle r="{s:.3f}" fill="none" stroke="{BLK}" stroke-width="{sw:.3f}"/>'
    if k == 6:    # triangle plein
        return f'<polygon points="0,{-s:.3f} {s*0.87:.3f},{s*0.5:.3f} {-s*0.87:.3f},{s*0.5:.3f}" fill="{BLK}"/>'
    if k == 7:    # cercle avec fanion (repère de perçage spécial)
        fx, fy = s * 1.4, -s * 1.3
        return (f'<circle r="{s*0.7:.3f}" fill="none" stroke="{BLK}" stroke-width="{sw:.3f}"/>'
               f'<line x1="{s*0.5:.3f}" y1="{-s*0.5:.3f}" x2="{fx:.3f}" y2="{fy:.3f}" '
               f'stroke="{BLK}" stroke-width="{sw:.3f}"/>'
               f'<polygon points="{fx:.3f},{fy:.3f} {fx+s*0.5:.3f},{fy+s*0.2:.3f} '
               f'{fx:.3f},{fy+s*0.55:.3f}" fill="{BLK}"/>')
    if k == 8:    # cercle croisé (⊕)
        return (f'<circle r="{s:.3f}" fill="none" stroke="{BLK}" stroke-width="{sw:.3f}"/>'
               f'<line x1="{-s:.3f}" y1="0" x2="{s:.3f}" y2="0" stroke="{BLK}" stroke-width="{sw:.3f}"/>'
               f'<line x1="0" y1="{-s:.3f}" x2="0" y2="{s:.3f}" stroke="{BLK}" stroke-width="{sw:.3f}"/>')
    if k == 9:    # carré croisé (⊞)
        return (f'<rect x="{-s:.3f}" y="{-s:.3f}" width="{2*s:.3f}" height="{2*s:.3f}" '
               f'fill="none" stroke="{BLK}" stroke-width="{sw:.3f}"/>'
               f'<line x1="{-s:.3f}" y1="0" x2="{s:.3f}" y2="0" stroke="{BLK}" stroke-width="{sw:.3f}"/>'
               f'<line x1="0" y1="{-s:.3f}" x2="0" y2="{s:.3f}" stroke="{BLK}" stroke-width="{sw:.3f}"/>')
    if k == 10:   # cercle plein
        return f'<circle r="{s:.3f}" fill="{BLK}"/>'
    if k == 11:   # carré barré en diagonale
        return (f'<rect x="{-s:.3f}" y="{-s:.3f}" width="{2*s:.3f}" height="{2*s:.3f}" '
               f'fill="none" stroke="{BLK}" stroke-width="{sw:.3f}"/>'
               f'<line x1="{-s:.3f}" y1="{-s:.3f}" x2="{s:.3f}" y2="{s:.3f}" stroke="{BLK}" stroke-width="{sw:.3f}"/>'
               f'<line x1="{-s:.3f}" y1="{s:.3f}" x2="{s:.3f}" y2="{-s:.3f}" stroke="{BLK}" stroke-width="{sw:.3f}"/>')
    if k == 12:   # hexagone ouvert
        pts = " ".join(f"{s*math.cos(math.radians(60*i-30)):.3f},{s*math.sin(math.radians(60*i-30)):.3f}"
                       for i in range(6))
        return f'<polygon points="{pts}" fill="none" stroke="{BLK}" stroke-width="{sw:.3f}"/>'
    # k == 13 : croix simple
    return (f'<line x1="{-s:.3f}" y1="0" x2="{s:.3f}" y2="0" stroke="{BLK}" stroke-width="{sw*1.4:.3f}"/>'
           f'<line x1="0" y1="{-s:.3f}" x2="0" y2="{s:.3f}" stroke="{BLK}" stroke-width="{sw*1.4:.3f}"/>')


def build_drilling_groups(holes: list[dict], tol_mm: float) -> list[dict]:
    """Regroupe les perçages par (diamètre arrondi, plaqué). Tri : plaqués d'abord
    (diamètre croissant), puis non plaqués (diamètre croissant) - comme dans un
    tableau de perçage ECSS/IPC classique. Même regroupement utilisé par le dessin
    et par le tableau, donc les symboles restent cohérents entre les deux."""
    if not holes or tol_mm <= 0:
        return []
    groups: dict[tuple[float, bool], list[dict]] = {}
    for h in holes:
        key = (round(h["drill"] / tol_mm) * tol_mm, h["plated"])
        groups.setdefault(key, []).append(h)
    ordered = sorted(groups.items(), key=lambda kv: (not kv[0][1], round(kv[0][0], 4)))
    return [{"symbol": i, "diameter": k[0], "plated": k[1], "qty": len(v), "holes": v}
            for i, (k, v) in enumerate(ordered)]


def build_drilling_svg(groups: list[dict], outline_prims: list, outline_pts: list,
                       symbol_size_mm: float, margin_mm: float):
    """SVG complet (contour + un symbole par perçage). Retourne (texte_svg, w_mm, h_mm)
    ou None si le contour Edge.Cuts est introuvable."""
    if not outline_pts:
        return None
    xs = [p[0] for p in outline_pts]
    ys = [p[1] for p in outline_pts]
    for g in groups:
        for h in g["holes"]:
            xs.append(h["x"])
            ys.append(h["y"])
    x0, x1 = min(xs) - margin_mm, max(xs) + margin_mm
    y0, y1 = min(ys) - margin_mm, max(ys) + margin_mm
    w, h = x1 - x0, y1 - y0
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.4f}mm" height="{h:.4f}mm" '
            f'viewBox="{x0:.4f} {y0:.4f} {w:.4f} {h:.4f}">',
            f'<rect x="{x0:.4f}" y="{y0:.4f}" width="{w:.4f}" height="{h:.4f}" fill="#ffffff"/>']
    sw_outline = max(w, h) * 0.0015
    for kind, geo in outline_prims:
        if kind == "line":
            (ax, ay), (bx, by) = geo
            parts.append(f'<line x1="{ax:.4f}" y1="{ay:.4f}" x2="{bx:.4f}" y2="{by:.4f}" '
                        f'stroke="#000000" stroke-width="{sw_outline:.4f}"/>')
        elif kind == "polyline":
            pts = " ".join(f"{px:.4f},{py:.4f}" for px, py in geo)
            parts.append(f'<polyline points="{pts}" fill="none" stroke="#000000" '
                        f'stroke-width="{sw_outline:.4f}"/>')
        elif kind == "circle":
            cx, cy, r = geo
            parts.append(f'<circle cx="{cx:.4f}" cy="{cy:.4f}" r="{r:.4f}" fill="none" '
                        f'stroke="#000000" stroke-width="{sw_outline:.4f}"/>')
    s = symbol_size_mm / 2.0
    for g in groups:
        markup = _hole_symbol_markup(g["symbol"], s)
        for hh in g["holes"]:
            parts.append(f'<g transform="translate({hh["x"]:.4f},{hh["y"]:.4f})">{markup}</g>')
    parts.append("</svg>")
    return "\n".join(parts), w, h


def build_symbol_svg(idx: int, size_mm: float) -> str:
    """Petite image carrée isolée d'un symbole, pour l'insérer dans une cellule
    du tableau des perçages."""
    s = size_mm / 2.0
    half = s + size_mm * 0.15
    markup = _hole_symbol_markup(idx, s)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<svg xmlns="http://www.w3.org/2000/svg" width="{2*half:.3f}mm" height="{2*half:.3f}mm" '
           f'viewBox="{-half:.3f} {-half:.3f} {2*half:.3f} {2*half:.3f}">{markup}</svg>')

_fig = {"n": 0}


def make_caption(text: str, style: str, lang: str):
    """Légende « Figure N : texte » avec un champ de séquence LibreOffice de catégorie
    « Illustration » : c'est ce que reconnaît Insertion > Table des matières et index >
    Table des illustrations. Le numéro est recalculé par renumber_illustrations()."""
    _fig["n"] += 1
    p = etree.Element(q("text:p"))
    p.set(q("text:style-name"), style)
    p.text = f"{LABELS[lang]['figure']} "
    seq = etree.SubElement(p, q("text:sequence"))
    seq.set(q("text:ref-name"), f"refPCBDocFig{_fig['n']}")
    seq.set(q("text:name"), "Illustration")
    seq.set(q("text:formula"), "ooow:Illustration+1")
    seq.set(q("style:num-format"), "1")
    seq.text = str(_fig["n"])
    seq.tail = f"{LABELS[lang]['sep']}{text}"
    return p


def ensure_illustration_decl(content_root) -> None:
    """Déclare la séquence « Illustration » dans office:text si le template ne l'a pas."""
    body = content_root.find(f"{q('office:body')}/{q('office:text')}")
    decls = body.find(q("text:sequence-decls"))
    if decls is None:
        decls = etree.Element(q("text:sequence-decls"))
        pos = 0
        for i, ch in enumerate(body):
            if ch.tag in (q("office:forms"), q("text:tracked-changes"), q("text:variable-decls")):
                pos = i + 1
        body.insert(pos, decls)
    if not any(d.get(q("text:name")) == "Illustration" for d in decls):
        d = etree.SubElement(decls, q("text:sequence-decl"))
        d.set(q("text:display-outline-level"), "0")
        d.set(q("text:name"), "Illustration")


def renumber_illustrations(content_root) -> None:
    """Renumérote toutes les légendes « Illustration » dans l'ordre (celles du template
    comprises), pour que les numéros affichés soient justes dès l'ouverture."""
    n = 0
    for seq in content_root.iter(q("text:sequence")):
        if seq.get(q("text:name")) == "Illustration":
            n += 1
            seq.text = str(n)

def parse_bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "oui", "on")


def parse_params(s: str | None) -> dict:
    out = {}
    for part in re.split(r",\s*(?=[A-Za-z_]+=)", s or ""):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


ARTWORKS_DEFAULT_EXTRA = ["F.Mask", "B.Mask", "F.SilkS", "B.SilkS"]

# Images composites : {{artwork.NOM}}. Noms de couches CANONIQUES de KiCad
# (Dwgs.User = "User.Drawings", Cmts.User = "User.Comments", ...).
# Composites rendus en noir et blanc par défaut (surchargeable avec |bw=0).
DEFAULT_BW = {"mechanical_data"}

DEFAULT_COMPOSITES = {
    "mechanical_data": ["Edge.Cuts", "Dwgs.User"],
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pcb", type=Path, help="fichier .kicad_pcb")
    ap.add_argument("template", type=Path, help="template ODT avec marqueurs {{...}}")
    ap.add_argument("--gerber", type=Path,
                help="dossier contenant les Gerber et fichiers de perçage, pour {{checklist}}")
    ap.add_argument("-o", "--output", type=Path, required=True, help="ODT de sortie")
    ap.add_argument("--workdir", type=Path, default=Path("build"),
                    help="dossier de travail (SVG exportés) [build]")
    ap.add_argument("--kicad-cli", help="chemin de kicad-cli (sinon PATH / $KICAD_CLI)")
    ap.add_argument("--lang", choices=LABELS, default="fr", help="langue des libellés [fr]")
    ap.add_argument("--image-format", choices=("svg", "png"), default="svg",
                    help="format des artworks insérés [svg]")
    ap.add_argument("--image-width", type=float, default=100.0,
                    help="largeur max des images en mm [100]")
    ap.add_argument("--image-max-height", type=float, default=200.0,
                    help="hauteur max des images en mm [200]")
    ap.add_argument("--theme", help="thème de couleurs KiCad (nom)")
    ap.add_argument("--black-and-white", action="store_true", help="artworks en noir et blanc")
    ap.add_argument("--mirror-bottom", action="store_true",
                    help="miroir pour les couches B.* (vue depuis le dessous)")
    ap.add_argument("--composite", action="append", default=[], metavar="NOM=COUCHE1,COUCHE2",
                    help="définit/remplace une image composite {{artwork.NOM}} (répétable)")
    ap.add_argument("--page-size-mode", type=int, choices=(0, 1, 2), default=2,
                    help="cadrage SVG de kicad-cli : 0 page+cartouche, 1 page, 2 carte seule [2]")
    ap.add_argument("--stackup-full", action="store_true",
                    help="inclure aussi sérigraphie et pâte à braser dans le tableau stackup")
    ap.add_argument("--symbol-size-mm", type=float, default=1.2,
                    help="taille des symboles du plan de perçage, en mm [1.2]")
    ap.add_argument("--drill-tolerance-mm", type=float, default=0.01,
                    help="écart de diamètre (mm) en-deçà duquel deux perçages sont "
                         "considérés de même taille [0.01]")
    ap.add_argument("--drilling-margin-mm", type=float, default=10.0,
                    help="marge autour du contour pour le plan de perçage, en mm [10]")
    ap.add_argument("--skip-export", action="store_true",
                    help="ne pas relancer kicad-cli si le SVG existe déjà dans workdir/artworks")
    args = ap.parse_args()

    if not args.pcb.exists() or not args.template.exists():
        sys.exit("Fichier .kicad_pcb ou template introuvable.")

    print("Lecture du PCB ...")
    data = collect_pcb_data(args.pcb)
    values = build_values(args.pcb, data)
    cli = find_kicad_cli(args.kicad_cli)
    artdir = args.workdir / "artworks"
    artdir.mkdir(parents=True, exist_ok=True)

    # ---- Lecture de l'ODT ------------------------------------------------- #
    with zipfile.ZipFile(args.template) as zin:
        files = {i.filename: zin.read(i.filename) for i in zin.infolist()}
    if "content.xml" not in files:
        sys.exit("Ce fichier n'est pas un ODT valide (content.xml absent).")
    content = etree.fromstring(files["content.xml"])
    ensure_styles(content)
    ensure_illustration_decl(content)

    new_pictures: dict[str, bytes] = {}
    pic_counter = {"n": 0}
    unknown_inline: set[str] = set()
    warnings: list[str] = []

    if args.gerber and not args.gerber.is_dir():
        warnings.append(f"--gerber : dossier introuvable ({args.gerber})")
    gerber_files = collect_gerber_files(args.gerber)
    
    present_layers = {n for n, _ in data["layers"]}

    def artwork_blocks(layer: str, width_mm: float | None, with_caption: bool,
                       layers: list[str] | None = None, bw: bool | None = None,
                       theme: str | None = None, margin_mm: float = 0.0, caption: str | None = None):
        """`layer` = nom affiché/fichier ; `layers` = couches KiCad (défaut : layer + Edge.Cuts)."""
        if layers is None:
            layers = [layer] if layer == "Edge.Cuts" else [layer, "Edge.Cuts"]
        missing = [l for l in layers if l not in present_layers]
        if missing:
            warnings.append(f"Couche(s) absente(s) du .kicad_pcb pour « {layer} » : "
                            f"{', '.join(missing)} (noms canoniques KiCad attendus)")
        bw_eff = args.black_and_white if bw is None else bw
        img = prepare_artwork(layer, layers, args.pcb, artdir, args, cli,
                              bw_eff, theme or args.theme, margin_mm)
        w0, h0 = svg_size_mm(img.with_suffix(".svg")) if img.suffix == ".png" else svg_size_mm(img)
        w = width_mm or args.image_width
        h = w * h0 / w0
        if h > args.image_max_height:
            w, h = w * args.image_max_height / h, args.image_max_height
        pic_counter["n"] += 1
        href = f"Pictures/pcbdoc_{pic_counter['n']:02d}_{layer_file_stem(layer)}{img.suffix}"
        new_pictures[href] = img.read_bytes()
        blocks = []
        blocks.append(build_image_paragraph(href, f"Artwork {layer}", w, h))
        if with_caption or caption:
            text = caption or f"{LABELS[args.lang]['artwork']} {layer}"
            blocks.append(make_caption(text, f"{STYLE_PREFIX}_Cap", args.lang))
        return blocks

    composites = {k: list(v) for k, v in DEFAULT_COMPOSITES.items()}
    for spec in args.composite:
        if "=" not in spec:
            sys.exit(f"--composite attend NOM=COUCHE1,COUCHE2 (reçu : {spec})")
        cname, clayers = spec.split("=", 1)
        composites[cname.strip()] = [l.strip() for l in clayers.split(",") if l.strip()]

    _symbol_pics: dict[int, str] = {}   # cache : idx symbole -> href déjà publié

    def drilling_drawing_blocks(width_mm: float | None, symbol_size_mm: float, margin_mm: float, with_caption: bool = True, caption: str | None = None):
        groups = build_drilling_groups(data["holes"], args.drill_tolerance_mm)
        if not data["outline_pts"]:
            warnings.append("{{drilling_drawing}} : pas de contour Edge.Cuts trouvé, image non générée.")
            return [make_par(LABELS[args.lang]["no_holes"], f"{STYLE_PREFIX}_Txt")]
        if not groups:
            return [make_par(LABELS[args.lang]["no_holes"], f"{STYLE_PREFIX}_Txt")]
        if len(groups) > 14:
            warnings.append(f"{{{{drilling_drawing}}}} : {len(groups)} tailles de perçage distinctes, "
                            "les symboles se répètent au-delà de 14 (ambigu dans le dessin).")
        svg_text, w0, h0 = build_drilling_svg(groups, data["outline_prims"], data["outline_pts"],
                                              symbol_size_mm, margin_mm)
        w = width_mm or args.image_width
        h = w * h0 / w0
        if h > args.image_max_height:
            w, h = w * args.image_max_height / h, args.image_max_height
        pic_counter["n"] += 1
        href = f"Pictures/pcbdoc_{pic_counter['n']:02d}_drilling_drawing.svg"
        new_pictures[href] = svg_text.encode("utf-8")
        blocks = []
        blocks.append(build_image_paragraph(href, "Drilling drawing", w, h))
        if with_caption:
            text = caption or "Drilling drawing"
            blocks.append(make_caption(text, f"{STYLE_PREFIX}_Cap", args.lang))
        return blocks

    def drilling_table_blocks(symbol_size_mm: float):
        L = LABELS[args.lang]
        groups = build_drilling_groups(data["holes"], args.drill_tolerance_mm)
        if not groups:
            return [make_par(L["no_holes"], f"{STYLE_PREFIX}_Txt")]
        header = [L["symbol"], L["diameter_mm"], L["plated"], L["quantity"]]
        col_styles = ["_ColNarrow", "_Col", "_ColNarrow", "_ColNarrow"]
        tbl = etree.Element(q("table:table"))
        tbl.set(q("table:name"), "PCB_Drilling")
        tbl.set(q("table:style-name"), f"{STYLE_PREFIX}_Tbl")
        for cs in col_styles:
            col = etree.SubElement(tbl, q("table:table-column"))
            col.set(q("table:style-name"), STYLE_PREFIX + cs)

        def cell(parent, text=None, img_href=None, head=False, span=None):
            tc = etree.SubElement(parent, q("table:table-cell"))
            tc.set(q("table:style-name"), f"{STYLE_PREFIX}_{'CellHead' if head else 'Cell'}")
            tc.set(q("office:value-type"), "string")
            if span:
                tc.set(q("table:number-columns-spanned"), str(span))
            if img_href:
                tc.append(build_image_paragraph(img_href, "Symbole", symbol_size_mm * 1.3,
                                                symbol_size_mm * 1.3))
            else:
                tc.append(make_par(text or "", f"{STYLE_PREFIX}_{'TxtHead' if head else 'Txt'}"))
            return tc

        # Titre fusionné sur toute la largeur
        tr = etree.SubElement(tbl, q("table:table-row"))
        cell(tr, text=L["drilling_title"], head=True, span=len(header))
        for _ in range(len(header) - 1):
            etree.SubElement(tr, q("table:covered-table-cell"))

        hdr = etree.SubElement(tbl, q("table:table-header-rows"))
        tr = etree.SubElement(hdr, q("table:table-row"))
        for htext in header:
            cell(tr, text=htext, head=True)

        for g in groups:
            if g["symbol"] not in _symbol_pics:
                pic_counter["n"] += 1
                href = f"Pictures/pcbdoc_{pic_counter['n']:02d}_drilling_sym_{g['symbol']}.svg"
                new_pictures[href] = build_symbol_svg(g["symbol"], symbol_size_mm).encode("utf-8")
                _symbol_pics[g["symbol"]] = href
            tr = etree.SubElement(tbl, q("table:table-row"))
            cell(tr, img_href=_symbol_pics[g["symbol"]])
            cell(tr, text=fmt_num(g["diameter"], 4))
            cell(tr, text=L["yes"] if g["plated"] else L["no"])
            cell(tr, text=str(g["qty"]))
        return [tbl]

    def expand_block(kind: str, comp: str | None, arg: str | None, params: dict):
        if kind == "stackup":
            return build_stackup_table(data["stackup"], data["copper_finish"],
                                       args.lang, args.stackup_full)
        if kind == "drilling_drawing":
            width = float(params["width"]) if "width" in params else None
            ssize = float(params["symbol_size"]) if "symbol_size" in params else args.symbol_size_mm
            margin = float(params["margin"]) if "margin" in params else args.drilling_margin_mm
            return drilling_drawing_blocks(width, ssize, margin)
        if kind == "drilling_table":
            ssize = float(params["symbol_size"]) if "symbol_size" in params else args.symbol_size_mm
            return drilling_table_blocks(ssize)
        if kind == "checklist":
            return build_checklist_table(gerber_files, args.lang)
        if kind == "artwork" and comp:
            if comp not in composites:
                raise ValueError(f"image composite inconnue « {comp} » "
                                 f"(disponibles : {', '.join(sorted(composites))})")
            width = float(params["width"]) if "width" in params else None
            bw = parse_bool(params["bw"]) if "bw" in params else (True if comp in DEFAULT_BW else None)
            return artwork_blocks(comp, width, with_caption=False, layers=composites[comp],
                                  bw=bw, theme=params.get("theme"), margin_mm=float(params.get("margin", DEFAULT_MARGIN.get(comp, 0.0))), caption=params.get("caption"))
        if kind == "artwork":
            if not arg:
                raise ValueError("{{artwork:NOM_COUCHE}} : nom de couche manquant")
            width = float(params["width"]) if "width" in params else None
            bw = parse_bool(params["bw"]) if "bw" in params else None
            return artwork_blocks(arg.strip(), width, with_caption=False,
                                  bw=bw, theme=params.get("theme"), caption=params.get("caption"))
        # artworks : cuivre + masques + sérigraphies présents dans la carte
        present = {n for n, _ in data["layers"]}
        names = data["copper_layers"] + [n for n in ARTWORKS_DEFAULT_EXTRA if n in present]
        out = []
        for n in names:
            out += artwork_blocks(n, None, with_caption=True)
        return out

    # ---- Marqueurs de bloc (contenu uniquement) --------------------------- #
    for p in list(content.iter(q("text:p"), q("text:h"))):
        m = BLOCK_RE.match(paragraph_text(p))
        if not m:
            continue
        kind, comp, arg, prm = m.group(1), m.group(2), m.group(3), parse_params(m.group(4))
        try:
            new_nodes = expand_block(kind, comp, arg, prm)
        except (RuntimeError, ValueError) as exc:
            sys.exit(f"Erreur sur le marqueur « {m.group(0).strip()} » : {exc}")
        parent = p.getparent()
        pos = parent.index(p)
        parent.remove(p)
        for k, node in enumerate(new_nodes):
            parent.insert(pos + k, node)

    # ---- Marqueurs en ligne (contenu + styles : en-têtes/pieds de page) --- #
    n_inline = 0
    for p in content.iter(q("text:p"), q("text:h")):
        n_inline += replace_inline(p, values, unknown_inline)
    renumber_illustrations(content)  
    files["content.xml"] = etree.tostring(content, xml_declaration=True, encoding="UTF-8")
    if "styles.xml" in files:
        styles = etree.fromstring(files["styles.xml"])
        for p in styles.iter(q("text:p"), q("text:h")):
            n_inline += replace_inline(p, values, unknown_inline)
        files["styles.xml"] = etree.tostring(styles, xml_declaration=True, encoding="UTF-8")

    # Marqueurs de bloc restants (mal placés)
    remaining = re.findall(
        r"\{\{\s*(?:stackup|artworks?|drilling_drawing|drilling_table|checklist)[^}]*\}\}",
        paragraph_text_all(content))
    for r in remaining:
        warnings.append(f"Marqueur de bloc non remplacé (doit être seul dans son paragraphe) : {r}")
    for k in sorted(unknown_inline):
        warnings.append(f"Marqueur inconnu laissé tel quel : {{{{{k}}}}}")

    # ---- Manifeste : déclarer les images ---------------------------------- #
    if new_pictures:
        man = etree.fromstring(files["META-INF/manifest.xml"])
        for href in new_pictures:
            e = etree.SubElement(man, f"{{{NS['manifest']}}}file-entry")
            e.set(f"{{{NS['manifest']}}}full-path", href)
            e.set(f"{{{NS['manifest']}}}media-type",
                  "image/svg+xml" if href.endswith(".svg") else "image/png")
        files["META-INF/manifest.xml"] = etree.tostring(man, xml_declaration=True, encoding="UTF-8")

    # ---- Écriture (mimetype en premier, non compressé) -------------------- #
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w") as zout:
        zout.writestr(zipfile.ZipInfo("mimetype"), files.get("mimetype", b"application/vnd.oasis.opendocument.text"),
                      compress_type=zipfile.ZIP_STORED)
        for name, blob in files.items():
            if name == "mimetype":
                continue
            zout.writestr(name, blob, compress_type=zipfile.ZIP_DEFLATED)
        for href, blob in new_pictures.items():
            zout.writestr(href, blob, compress_type=zipfile.ZIP_DEFLATED)

    print(f"OK : {args.output}  ({len(new_pictures)} image(s), {n_inline} valeur(s) insérée(s))")
    for w in warnings:
        print("ATTENTION :", w, file=sys.stderr)
    return 0


def paragraph_text_all(root) -> str:
    return "\n".join(paragraph_text(p) for p in root.iter(q("text:p"), q("text:h")))


if __name__ == "__main__":
    sys.exit(main())