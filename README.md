# ECSS PCB design toolbox for KiCad

Helpers to check a KiCad PCB against **ECSS-Q-ST-70-12C** (design rules for printed circuit boards) and to generate the PCB definition dossier according to this standard.

**Scope:** rigid PCB, standard (non-"Reliable") class, 0 < V ≤ 10 V, no RF / HDI / AAD, as-designed values. The tables used (7-3, 7-4, 7-5, 13-7, 14-1, 14-3, 14-4) are transcribed in the code.

**Software secifications:** this toolbox has been tested Ubuntu 26.04 LTS, with KiCad 10 and LibreOffice 26.2.6.3.  

The toolbox has two independent Python scripts:

| Script | What it does |
|---|---|
| `ecss_pcb_tool.py` | Reads the `.kicad_pcb`, generates a design-specific `.kicad_dru` rule file, and runs the checks a rule file cannot do. Writes a report. |
| `ecss_pcb_dossier.py` | Fills a PCB definition dossier ODT template according to ECSS standards, from a KiCad project (.kicad_pcb file). |

---

## 1. Install

Requires Python 3.9+ (`git` and `python3` available in your shell).

```bash
# 1. Clone the repo
git clone https://github.com/florian-topeza/ECSS-PCB-design-toolbox.git ecss-pcb-design-toolbox
cd ecss-pcb-design-toolbox

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Linux / macOS
# .venv\Scripts\activate           # Windows (cmd / PowerShell)

# 3. Install the dependencies of the script
pip install -r requirements.txt
```

Next time, only re-activate the environment (`source .venv/bin/activate`). Leave it with `deactivate`.

## 2. ECSS PCB design tool

### Usage

```bash
python ecss_pcb_tool.py board.kicad_pcb                                     # DRU + checks + report
python ecss_pcb_tool.py board.kicad_pcb --write-config ecss_config.json     # dump default config
python ecss_pcb_tool.py board.kicad_pcb --config ecss_config.json --force   # use config, overwrite DRU
```

| Option | Effect |
|---|---|
| `--config FILE` | JSON config (answers to the open design choices) |
| `--write-config FILE` | Write the default config and exit |
| `--dru FILE` | Output rule file (default: `<board>.kicad_dru` next to the board) |
| `--report FILE` | Report path (default below) |
| `--force` | Overwrite an existing `.kicad_dru` (otherwise kept) |
| `--no-dru` / `--no-checks` | Skip one of the two steps |
| `--coating` | Conformal coating on external layers |

The report is written to `<board folder>/ecss_check_results/ecss_rule_check_DDMMYYYY_HH:MM:SS.txt` (`:` becomes `-` on Windows). The exit code is `1` if at least one ERROR was found, `0` otherwise, so it can be used in CI.

### Workflow

1. In KiCad, define the **physical stackup** (Board Setup → Physical Stackup) and save. Per-layer values are derived from it.
2. Run the tool. It writes `board.kicad_dru` (loaded automatically by KiCad next to the project) and the report.
3. Run KiCad's DRC for the rules in the `.kicad_dru`; read the report for the rest.

### What it covers

**Generated into the `.kicad_dru`** (per layer, from the stackup): X,Y conductor spacing, minimum track width, 0.1 mm minimum conductor, via hole-wall to hole-wall, conductor/track to board edge and to NPTH (Table 14-1 A1, A3, A6), annular ring (pad = hole + 0.8 / 0.6 mm), minimum distance between components (Table 14-3 floor).

**Checked by the script:** Z-direction insulation of the stackup, external spacing "should" level (warning), track width at chip SMT pads (≤ 1/3 pad) and PTH pads (≤ 1/2 pad), track length pad → via (Table 14-4, PTH rule), mounting holes (Table 14-1 A4, A5, A7), component-free edge zone (≥ 5 mm), "remove unused layers" flags on PTH/vias, teardrops on internal pads.

"shall" requirements are reported as errors and "should" as warnings (configurable).

### Configuration (`ecss_config.json`)

Key options (see `--write-config` for all, with help strings):

| Key | Default | Meaning |
|---|---|---|
| `conformal_coating` | `false` | Use the "with conformal coating" external spacing |
| `outer_copper_includes_plating` | `false` | If `false`, `outer_plating_um` is added to the outer foil |
| `bond_sequence` | `"single"` | `single` (450 µm) or `sequential` (650 µm) hole-wall spacing |
| `courtyard_margin_mm` | `0.0` | Courtyard margin used in the component-distance rule |
| `mounting_hole_ref_regex` | `^H` | Reference designators of mounting holes |
| `chip_footprint_regex` | SMD R/C/L/LED libs | Footprints treated as chip devices |
| `screw_head_diameter_mm` | `null` | Enables the track-to-screw check (A5) |
| `edge_zone_mm` / `edge_zone_exempt_ref_regex` | `5.0` / `^H` | Edge zone width and exempt components |
| `shall_severity` / `should_severity` | `error` / `warning` | Severity mapping |

### Not checked

- Pad design tables 14-6 to 14-10 (need component datasheet dimensions)
- Table 14-3 0.5 mm cases and Table 14-2; Table 14-1 A2 (no mechanical data in the PCB file)
- Conductor to hole wall, hole wall to heat sink, X,Y offset of superimposed conductors
- Spacing involving copper zone fills; pad-to-pad spacing inside one footprint
- Prepreg as-designed Z thickness, fine-pitch exceptions, "Reliable" columns, voltages above 10 V

---

## 3. ECSS PCB definition dossier

Builds the PCB definition dossier from an ODT template and a `.kicad_pcb`.

### What it does

1. Reads the `.kicad_pcb` (built-in S-expression parser): stackup, number of copper layers, finish, thickness, board dimensions, title block.
2. Exports the artwork of each requested layer as SVG with `kicad-cli` (one layer + the Edge.Cuts outline per image).
3. Replaces the `{{...}}` markers of the ODT template with text (simple values), images (artworks) or a table (stackup).

The result is a new ODT, the template is never modified.

### Usage

```bash
python ecss_pcb_dossier.py board.kicad_pcb template.odt -o dossier.odt --gerber Gerber/
python ecss_pcb_dossier.py board.kicad_pcb template.odt -o dossier.odt --lang en \
    --image-width 150 --theme "KiCad Classic"
# Reuse already exported SVGs (in build/artworks/F_Cu.svg, ...):
python ecss_pcb_dossier.py board.kicad_pcb template.odt -o dossier.odt --skip-export
```

### Template markers

**Block markers** (the marker must be alone in its paragraph):

| Marker | Result |
|---|---|
| `{{stackup}}` | Stackup table |
| `{{artwork:F.Cu}}` | Image of the F.Cu layer (canonical KiCad name) |
| `{{artwork:In1.Cu\|width=120}}` | Same, with the width forced to 120 mm |
| `{{artworks}}` | All copper layers + masks + silkscreens, each preceded by a title/caption |
| `{{artwork.mechanical_data}}` | Composite image of Edge.Cuts + F.Paste + Dwgs.User (= User.Drawings, PCB dimensions). Other composites: `--composite NAME=LAYER1,LAYER2`, then `{{artwork.NAME}}`. Black and white by default; the rest of the export follows the default KiCad theme. |
| `{{drilling_drawing}}` | Drilling plan generated by the script (not by `kicad-cli`): Edge.Cuts outline + one symbol per hole (through-hole pad, NPTH, via). Holes with the same diameter and the same plated/non-plated status share a symbol. |
| `{{drilling_table}}` | Matching "Through Holes" table: symbol, diameter (mm), plated (Yes/No), quantity. Same grouping as `{{drilling_drawing}}`, so symbols match between image and table even if only one of the two markers is used. The "Note 1/2/3" callouts for special dimensioning are **not** generated (not KiCad data). |
| `{{checklist}}` | Checklist table of the manufacturing files (Gerber and drill): file name, size, last modification date and CRC32. |

**Per-image options** (after `|`, comma-separated):

| Option | Effect |
|---|---|
| `width=120` | Width in mm |
| `bw=1` / `bw=0` | Black and white on / off |
| `theme=NAME` | KiCad theme for this image, e.g. `{{artwork:F.Cu\|theme=KiCad Classic}}` |

Extra options for `{{drilling_drawing}}`:

| Option | Effect | CLI equivalent |
|---|---|---|
| `symbol_size=1.2` | Symbol size in mm | `--symbol-size-mm` |
| `margin=10` | Margin around the outline, in mm | `--drilling-margin-mm` |

**Inline markers** (can sit mid-sentence, in a table, a header or a footer):

```
{{project_name}} {{pcb_file}} {{title}} {{rev}} {{company}} {{date}}
{{kicad_version}} {{copper_layers}} {{board_thickness}} {{copper_finish}}
{{board_width}} {{board_height}} {{board_size}}
```
---

## License

This project is licensed under the MIT License.
