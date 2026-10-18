"""Builds a synthetic KiCad-8-style board with known violations to exercise ecss_pcb_tool.py."""
import sys

out = []
a = out.append
a('(kicad_pcb (version 20240108) (generator "pcbnew") (generator_version "8.0")')
a('  (general (thickness 1.6) (legacy_teardrops no))')
a('  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)'
  ' (44 "Edge.Cuts" user) (46 "B.CrtYd" user) (47 "F.CrtYd" user))')
a('  (setup (stackup')
a('    (layer "F.Cu" (type "copper") (thickness 0.035))')
a('    (layer "dielectric 1" (type "prepreg") (thickness 0.1) (material "FR4"))')
a('    (layer "In1.Cu" (type "copper") (thickness 0.035))')
a('    (layer "dielectric 2" (type "core") (thickness 0.09) (material "FR4"))')   # violation: < 101.6 um
a('    (layer "In2.Cu" (type "copper") (thickness 0.035))')
a('    (layer "dielectric 3" (type "prepreg") (thickness 0.1) (material "FR4"))')
a('    (layer "B.Cu" (type "copper") (thickness 0.035))')
a('  ))')
nets = ["", "N1", "N2", "N3", "GND", "VIA_A", "VIA_B", "N4"]
for i, n in enumerate(nets):
    a(f'  (net {i} "{n}")')
nid = {n: i for i, n in enumerate(nets)}


def fp(lib, ref, x, y, rot, pads, court):
    a(f'  (footprint "{lib}" (layer "F.Cu") (at {x} {y} {rot})')
    a(f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))')
    for p in pads:
        num, kind, shape, px, py, prot, w, h, drill, net = p
        layers = '"*.Cu" "*.Mask"' if kind == "thru_hole" else '"F.Cu" "F.Mask" "F.Paste"'
        d = f' (drill {drill})' if drill else ''
        ro = f' {prot}' if prot else ''
        a(f'    (pad "{num}" {kind} {shape} (at {px} {py}{ro}) (size {w} {h}){d} (layers {layers}) (net {nid[net]} "{net}"))')
    if court:
        x0, y0, x1, y1 = court
        a(f'    (fp_rect (start {x0} {y0}) (end {x1} {y1}) (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))')
    a('  )')


# R1 : chip 0603 at (20,20), rot 0
fp("Resistor_SMD:R_0603_1608Metric", "R1", 20, 20, 0,
   [("1", "smd", "roundrect", -0.825, 0, 0, 0.8, 0.95, 0, "N1"), ("2", "smd", "roundrect", 0.825, 0, 0, 0.8, 0.95, 0, "N2")],
   (-1.25, -0.6, 1.25, 0.6))
# R2 : chip rotated 90 at (30,20)  (pad absolute rotation 90 as written by KiCad)
fp("Resistor_SMD:R_0603_1608Metric", "R2", 30, 20, 90,
   [("1", "smd", "roundrect", -0.825, 0, 90, 0.8, 0.95, 0, "N4"), ("2", "smd", "roundrect", 0.825, 0, 90, 0.8, 0.95, 0, "N2")],
   (-1.25, -0.6, 1.25, 0.6))
# R3 : near the left edge (edge zone violation)
fp("Resistor_SMD:R_0603_1608Metric", "R3", 3, 20, 0,
   [("1", "smd", "roundrect", -0.825, 0, 0, 0.8, 0.95, 0, "N2"), ("2", "smd", "roundrect", 0.825, 0, 0, 0.8, 0.95, 0, "N2")],
   (-1.25, -0.6, 1.25, 0.6))
# J1 : 2-pin THT header
fp("Connector_PinHeader_2.54mm:PinHeader_1x02", "J1", 10, 10, 0,
   [("1", "thru_hole", "circle", 0, 0, 0, 1.7, 1.7, 1.0, "N3"), ("2", "thru_hole", "circle", 0, 2.54, 0, 1.7, 1.7, 1.0, "GND")],
   (-1.5, -1.5, 1.5, 4.0))
# Mounting holes
fp("MountingHole:MountingHole_3.2mm_M3_ISO14580_Pad", "H1", 5, 5, 0,
   [("1", "thru_hole", "circle", 0, 0, 0, 6.4, 6.4, 3.2, "GND")], (-3.5, -3.5, 3.5, 3.5))
fp("MountingHole:MountingHole_3.2mm_M3_ISO14580_Pad", "H2", 48, 38, 0,
   [("1", "thru_hole", "circle", 0, 0, 0, 6.4, 6.4, 3.2, "GND")], (-3.5, -3.5, 3.5, 3.5))


def seg(x0, y0, x1, y1, w, layer, net):
    a(f'  (segment (start {x0} {y0}) (end {x1} {y1}) (width {w}) (layer "{layer}") (net {nid[net]}))')


def via(x, y, size, drill, net, extra=''):
    a(f'  (via (at {x} {y}) (size {size}) (drill {drill}) (layers "F.Cu" "B.Cu"){extra} (net {nid[net]}))')


# R1.1 (19.175,20) -> via in the pad zone (L=0), 0.4 wide (> 0.95/3)
seg(19.175, 20, 18.7, 20, 0.4, "F.Cu", "N1")
via(18.7, 20, 0.6, 0.3, "N1")
# R2.1 at (30,20.825): fan-out going +y, width 0.25, via far enough? pad edge at y=21.225, via at y=21.8 r=0.3 -> L=0.275 (>=0.25 for W<=0.2? W=0.25 -> 0.7 -> violation)
seg(30, 20.825, 30, 21.8, 0.25, "F.Cu", "N4")
via(30, 21.8, 0.6, 0.3, "N4")
# R2.2 at (30,19.175): good fan-out 0.2 wide, L>=0.25: pad edge y=18.775, via at y=18.0 -> r .3 -> L = 0.775-0.3 -> ok
seg(30, 19.175, 30, 18.0, 0.2, "F.Cu", "N2")
via(30, 18.0, 0.6, 0.3, "N2")
# J1.1: wide track (1.0 > 0.85) + short fan-out to via
seg(10, 10, 11.8, 10, 1.0, "F.Cu", "N3")
via(11.8, 10, 0.6, 0.3, "N3")
# H1 : via too close to the mounting hole pad
via(8, 5, 0.6, 0.3, "GND")
# two vias different nets too close hole-to-hole (DRU only) + one with remove_unused_layers
via(40, 10, 0.6, 0.3, "VIA_A", " (remove_unused_layers)")
via(40.7, 10, 0.6, 0.3, "VIA_B")
# inner layer track on a via (teardrop check -> no teardrop zone => INFO)
seg(40, 10, 40, 12, 0.15, "In1.Cu", "VIA_A")

for (x0, y0, x1, y1) in [(0, 0, 50, 0), (50, 0, 50, 40), (50, 40, 0, 40), (0, 40, 0, 0)]:
    a(f'  (gr_line (start {x0} {y0}) (end {x1} {y1}) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))')
a(')')
open(sys.argv[1], "w").write("\n".join(out) + "\n")
print("written", sys.argv[1])
