"""Build a QIDI Studio 3MF project with filament samples, one object per sample.

Each sample is a rounded plate with a hanging hole and raised text, assigned
to the Box slot it is printed from. The plate is set to print object by object, so
samples come out one after another.
"""
import io
import json
import pathlib
import xml.sax.saxutils as saxutils
import zipfile

PLATE_WIDTH = 60.0
PLATE_DEPTH = 30.0
PLATE_FLOOR = 1.2  # Six layers at 0.2 mm.
TEXT_RELIEF = 0.4  # Two layers at 0.2 mm. Engraved text printed unclear, so text is raised.
OVERLAP = 0.1  # The top layer sinks into the base so the slicer sees one solid.
CELL = 0.1  # The top of the plate is built from a grid this fine.
MIN_TITLE_EM = 3.4
BED = 270.0
GAP = 20.0  # Keeps the hot end clear of a finished group; all groups are equally low.
GROUP_GAP = 3.0  # Samples of one color print together as one object.
SLOTS = 4
FLUSH_VOLUME = 280
STUDIO_VERSION = "02.07.02.73"
PROJECT = json.loads((pathlib.Path(__file__).resolve().parent / "sample_project_settings.json").read_text(encoding="utf-8"))

FONT = json.loads((pathlib.Path(__file__).resolve().parent / "sample_font.json").read_text(encoding="utf-8"))
GLYPHS = FONT["glyphs"]
# Rows in the font data are counted from below the descender, so "H" marks the baseline.
BASELINE = min(run[0] for run in GLYPHS["H"]["runs"])
CAP = 0.72  # Height of capitals relative to the font size.


class SampleError(Exception):
    pass


class Plate:
    """Sizes of the sample."""

    def __init__(self):
        self.width, self.depth = PLATE_WIDTH, PLATE_DEPTH
        self.floor = PLATE_FLOOR
        self.height = PLATE_FLOOR + TEXT_RELIEF
        self.corner = 4.0
        self.hole_r = 1.9
        self.hole_x = 5.5
        self.text_x = self.hole_x + self.hole_r + 2.0
        self.margin = 3.5
        self.title_em = 5.4
        self.info_em = 4.0
        self.title_gap = 2.4
        self.line_gap = 1.6
        self.columns = int(round(self.width / CELL))
        self.rows = int(round(self.depth / CELL))


def printable(text):
    """Keep what the font can draw."""
    return "".join(c for c in text if c in GLYPHS).strip()


def text_lines(sample):
    lines = [printable(sample["material"]) or "Образец"]
    if sample.get("min_temp") and sample.get("max_temp"):
        lines.append("Сопло %d–%d °C" % (sample["min_temp"], sample["max_temp"]))
    if sample.get("dry_temp"):
        dry = "Сушка %d °C" % (sample["dry_temp"],)
        if sample.get("dry_hours"):
            dry += ", %d ч" % (sample["dry_hours"],)
        lines.append(dry)
    return lines


def _line_width(line, em):
    return sum(GLYPHS[c]["advance"] for c in line) * em / FONT["em"]


def _fit_em(line, em, room):
    fitted = min(em, em * room / _line_width(line, em))
    if fitted < MIN_TITLE_EM:
        raise SampleError("Текст «%s» не помещается: увеличь образец" % (line,))
    return fitted


def _glyph_boxes(char, x, baseline_y, em):
    """Runs on neighbouring rows with the same span merge into one rectangle."""
    pixel = em / FONT["em"]
    spans = {}
    for row, x0, x1 in GLYPHS[char]["runs"]:
        spans.setdefault((x0, x1), []).append(row)
    boxes = []
    for (x0, x1), rows in spans.items():
        rows.sort()
        start = previous = rows[0]
        for row in rows[1:] + [None]:
            if row is None or row != previous + 1:
                boxes.append((x + x0 * pixel, baseline_y + (start - BASELINE) * pixel,
                              x + x1 * pixel, baseline_y + (previous + 1 - BASELINE) * pixel))
                start = row
            previous = row
    return boxes


def text_boxes(sample, plate):
    lines = text_lines(sample)
    room = plate.width - plate.text_x - plate.margin
    ems = [_fit_em(lines[0], plate.title_em, room)]
    ems += [_fit_em(line, plate.info_em, room) for line in lines[1:]]
    gaps = ([plate.title_gap] if len(lines) > 1 else []) + [plate.line_gap] * max(0, len(lines) - 2)
    total = sum(em * CAP for em in ems) + sum(gaps)
    if total > plate.depth - 2:
        raise SampleError("Текст не помещается по высоте: увеличь образец")
    y = (plate.depth + total) / 2
    boxes = []
    for number, line in enumerate(lines):
        y -= ems[number] * CAP
        x = plate.text_x
        for char in line:
            boxes.extend(_glyph_boxes(char, x, y, ems[number]))
            x += GLYPHS[char]["advance"] * ems[number] / FONT["em"]
        if number < len(gaps):
            y -= gaps[number]
    return boxes


def _inside_outline(plate, x, y):
    """Rounded plate without the hanging hole."""
    r = plate.corner
    dx = max(r - x, x - (plate.width - r), 0)
    dy = max(r - y, y - (plate.depth - r), 0)
    if dx * dx + dy * dy > r * r:
        return False
    return (x - plate.hole_x) ** 2 + (y - plate.depth / 2) ** 2 > plate.hole_r ** 2


def _outline_rows(plate):
    return [bytearray(_inside_outline(plate, (i + .5) * CELL, (j + .5) * CELL) for i in range(plate.columns))
            for j in range(plate.rows)]


def _text_rows(boxes, plate):
    rows = [bytearray(plate.columns) for _ in range(plate.rows)]
    for x0, y0, x1, y1 in boxes:
        i0, i1 = max(0, int(x0 / CELL)), min(plate.columns, int(x1 / CELL + .999))
        for j in range(max(0, int(y0 / CELL)), min(plate.rows, int(y1 / CELL + .999))):
            rows[j][i0:i1] = b"\x01" * (i1 - i0)
    return rows


def _rectangles(rows, plate):
    """Turn rows of on/off cells into rectangles, merging equal spans on neighbouring rows."""
    done, open_spans = [], {}
    for j, row in enumerate(rows + [bytearray(plate.columns)]):
        spans = set()
        i = 0
        while i < plate.columns:
            if row[i]:
                start = i
                while i < plate.columns and row[i]:
                    i += 1
                spans.add((start, i))
            else:
                i += 1
        for span in list(open_spans):
            if span not in spans:
                done.append((span[0] * CELL, open_spans.pop(span) * CELL, span[1] * CELL, j * CELL))
        for span in spans:
            open_spans.setdefault(span, j)
    return done


def top_rectangles(sample, plate, outline=None):
    """Top layers: only the text, raised above the plate."""
    outline = outline if outline is not None else _outline_rows(plate)
    text = _text_rows(text_boxes(sample, plate), plate)
    rows = [bytearray(a & b for a, b in zip(o, t)) for o, t in zip(outline, text)]
    return _rectangles(rows, plate)


def _add_box(vertices, triangles, x0, y0, z0, x1, y1, z1):
    base = len(vertices)
    vertices.extend([(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
                     (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)])
    for a, b, c in ((0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
                    (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)):
        triangles.append((base + a, base + b, base + c))


def sample_mesh(sample, plate=None, outline=None):
    plate = plate or Plate()
    outline = outline if outline is not None else _outline_rows(plate)
    vertices, triangles = [], []
    for x0, y0, x1, y1 in _rectangles(outline, plate):
        _add_box(vertices, triangles, x0, y0, 0, x1, y1, plate.floor)
    for x0, y0, x1, y1 in top_rectangles(sample, plate, outline):
        _add_box(vertices, triangles, x0, y0, plate.floor - OVERLAP, x1, y1, plate.height)
    return vertices, triangles


def groups_of(samples):
    """Samples of one slot in a row become one group, printed as a single object."""
    groups = []
    for sample in samples:
        if groups and groups[-1][0]["slot"] == sample["slot"]:
            groups[-1].append(sample)
        else:
            groups.append([sample])
    return groups


def layout(samples, plate):
    """Place groups on the bed with head clearance between them, samples close inside.
    Returns (group x, group y, width, depth, [(sample, x, y) inside the group])."""
    placed, x, y, row_depth = [], 0.0, 0.0, 0.0
    for group in groups_of(samples):
        best = None
        columns = min(len(group), int((BED + GROUP_GAP) // (plate.width + GROUP_GAP)))
        if columns < 1:
            raise SampleError("Образец шире стола")
        while columns >= 1:
            rows = (len(group) + columns - 1) // columns
            width = columns * plate.width + (columns - 1) * GROUP_GAP
            depth = rows * plate.depth + (rows - 1) * GROUP_GAP
            room = BED - x if x else BED
            if width <= room or x == 0:
                best = (columns, rows, width, depth)
                break
            columns -= 1
        if best is None or best[2] > BED - x:
            x, y, row_depth = 0.0, y + row_depth + GAP, 0.0
            columns = min(len(group), int((BED + GROUP_GAP) // (plate.width + GROUP_GAP)))
            rows = (len(group) + columns - 1) // columns
            best = (columns, rows, columns * plate.width + (columns - 1) * GROUP_GAP,
                    rows * plate.depth + (rows - 1) * GROUP_GAP)
        columns, rows, width, depth = best
        inside = [(sample, (i % columns) * (plate.width + GROUP_GAP),
                   depth - plate.depth - (i // columns) * (plate.depth + GROUP_GAP))
                  for i, sample in enumerate(group)]
        placed.append([x, y, width, depth, inside])
        x += width + GAP
        row_depth = max(row_depth, depth)
    total = max(p[1] + p[3] for p in placed)
    if total > BED:
        raise SampleError("Образцы не помещаются на стол: уменьши количество или размер")
    # Centered, first group at the back so the order reads from the top of the preview.
    used_x = max(p[0] + p[2] for p in placed)
    for p in placed:
        p[0] += (BED - used_x) / 2
        p[1] = (BED + total) / 2 - p[1] - p[3]
    return placed


def _group_mesh(inside, plate, outline):
    vertices, triangles = [], []
    cache = {}
    for sample, x, y in inside:
        key = id(sample)
        if key not in cache:
            cache[key] = sample_mesh(sample, plate, outline)
        mesh_v, mesh_t = cache[key]
        base = len(vertices)
        vertices.extend((vx + x, vy + y, vz) for vx, vy, vz in mesh_v)
        triangles.extend((a + base, b + base, c + base) for a, b, c in mesh_t)
    return vertices, triangles


def _model_xml(samples, plate):
    outline = _outline_rows(plate)
    objects, items = [], []
    for number, (x, y, _, _, inside) in enumerate(layout(samples, plate), 1):
        vertices, triangles = _group_mesh(inside, plate, outline)
        v = "".join('<vertex x="%.3f" y="%.3f" z="%.3f"/>' % p for p in vertices)
        t = "".join('<triangle v1="%d" v2="%d" v3="%d"/>' % p for p in triangles)
        objects.append('<object id="%d" type="model"><mesh><vertices>%s</vertices>'
                       '<triangles>%s</triangles></mesh></object>' % (number, v, t))
        items.append('<item objectid="%d" transform="1 0 0 0 1 0 0 0 1 %.3f %.3f 0" printable="1"/>'
                     % (number, x, y))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<model unit="millimeter" xml:lang="en-US" '
            'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
            'xmlns:QIDIStudio="http://schemas.qiditech.com/package/2021">'
            '<metadata name="Application">QIDIStudio-%s</metadata>'
            '<metadata name="QIDIStudio:3mfVersion">1</metadata>'
            '<resources>%s</resources><build>%s</build></model>' % (STUDIO_VERSION, "".join(objects), "".join(items)))


def _settings_xml(samples, plate):
    quote = saxutils.quoteattr
    parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>']
    placed = layout(samples, plate)
    for number, (_, _, _, _, inside) in enumerate(placed, 1):
        sample = inside[0][0]
        parts.append('<object id="%d"><metadata key="name" value=%s/>'
                     '<metadata key="extruder" value="%d"/></object>'
                     % (number, quote(printable(sample["material"]) or "Образец"), sample["slot"] + 1))
    parts.append('<plate><metadata key="plater_id" value="1"/>'
                 '<metadata key="print_sequence" value="by object"/>')
    for number in range(1, len(placed) + 1):
        parts.append('<model_instance><metadata key="object_id" value="%d"/>'
                     '<metadata key="instance_id" value="0"/>'
                     '<metadata key="identify_id" value="%d"/></model_instance>' % (number, number * 100))
    parts.append('</plate></config>')
    return "".join(parts)


def project_settings(colors):
    """The user's Q2 project settings with four filaments and sequential printing."""
    settings = dict(PROJECT["settings"])
    for key in PROJECT["per_filament"]:
        settings[key] = list(settings[key]) * SLOTS
    settings["filament_colour"] = ["#" + colors.get(slot, "808080") for slot in range(SLOTS)]
    settings["print_sequence"] = "by object"
    settings["version"] = STUDIO_VERSION
    settings["filament_self_index"] = [str(slot + 1) for slot in range(SLOTS)]
    # Square table of purge volumes between filaments; nothing to purge within one.
    settings["flush_volumes_matrix"] = ["0" if a == b else str(FLUSH_VOLUME)
                                        for a in range(SLOTS) for b in range(SLOTS)]
    settings["enable_prime_tower"] = "0"  # Not used when objects print in sequence.
    return json.dumps(settings, ensure_ascii=False, indent=1)


CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
                 '</Types>')
RELS = ('<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Target="/3D/3dmodel.model" Id="rel-1" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')


EXAMPLE = {"material": "Bambu Lab PLA Matte", "min_temp": 190, "max_temp": 230, "dry_temp": 55, "dry_hours": 8}


def preview(samples):
    """What the project will contain, for drawing the bed: one entry per sample."""
    plate = Plate()
    result = {"bed": BED, "plate": [plate.width, plate.depth], "corner": plate.corner,
              "height": plate.height, "hole": [plate.hole_x, plate.depth / 2, plate.hole_r], "items": []}
    example = samples[0] if samples else EXAMPLE
    result["template"] = {"slot": example.get("slot", 0), "lines": text_lines(example),
                          "boxes": [[round(v, 2) for v in box] for box in text_boxes(example, plate)]}
    if samples:
        for gx, gy, _, _, inside in layout(samples, plate):
            for sample, x, y in inside:
                result["items"].append({"slot": sample["slot"], "x": gx + x, "y": gy + y, "lines": text_lines(sample),
                                        "boxes": [[round(v, 2) for v in box] for box in text_boxes(sample, plate)]})
    return result


def build_project(samples, colors=None):
    """samples: dicts with slot (0-3), material and optional temperatures, in print order."""
    if not samples:
        raise SampleError("Укажи количество хотя бы для одной катушки")
    plate = Plate()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", RELS)
        archive.writestr("3D/3dmodel.model", _model_xml(samples, plate))
        archive.writestr("Metadata/model_settings.config", _settings_xml(samples, plate))
        archive.writestr("Metadata/project_settings.config", project_settings(colors or {}))
    return buffer.getvalue()
