#!/usr/bin/env python3
"""The picture of the low-plastic window on the printer screen: the spool in its real colour
with its slot over the hub, and a red or orange mark for short or thin.

The screen reads the picture anew each time the window opens, unlike the window's text, so this
is where the numbers of the current print go. Written for the printer: no libraries needed.
The BIN is a 4-byte LVGL header (colour format 5, width, height), then blue, green, red and alpha.
"""

import base64
import json
import pathlib
import struct
import sys
import zlib

CANVAS = 96   # Smaller pictures repeat like tiles in the window.
SHOWN = 72    # The window cuts the picture a little below 96, starting from the bottom right.
SAMPLES = 3
BANDS = {"short": (229, 57, 53), "low": (240, 130, 20)}
FONT = json.loads((pathlib.Path(__file__).resolve().parent / "slot_font.json").read_text(encoding="utf-8"))


class SlotLabel:
    """The slot name, drawn from glyphs rendered smooth on a Mac (make_slot_font.swift)."""

    def __init__(self, text, center_x, center_y):
        self.alpha = {}
        cursor = center_x - sum(FONT["glyphs"][c]["advance"] for c in text if c in FONT["glyphs"]) / 2.
        baseline = center_y + FONT["cap"] / 2.
        for char in text:
            glyph = FONT["glyphs"].get(char)
            if glyph is None:
                continue
            data = base64.b64decode(glyph["alpha"])
            left = int(round(cursor)) - FONT["pad"]
            top = int(round(baseline - glyph["baseline"]))
            for row in range(glyph["height"]):
                for column in range(glyph["width"]):
                    value = data[row * glyph["width"] + column]
                    if value:
                        key = (left + column, top + row)
                        self.alpha[key] = max(self.alpha.get(key, 0), value)
            cursor += glyph["advance"]

    def paint(self, rows, color):
        for (x, y), value in self.alpha.items():
            if 0 <= x < CANVAS and 0 <= y < CANVAS:
                red, green, blue, alpha = rows[y][x]
                mix = value / 255.
                rows[y][x] = (int(red + (color[0] - red) * mix), int(green + (color[1] - green) * mix),
                              int(blue + (color[2] - blue) * mix), max(alpha, value))


def shade(color, factor):
    return tuple(max(0, min(255, int(c * factor))) for c in color)


def color_at(x, y, color, ring):
    """The topmost shape at a point of the shown square, or None."""
    dx, dy = x - 36, y - 36
    distance = (dx * dx + dy * dy) ** .5
    light = 1.12 - .22 * (dx + dy) / 70.  # Lit from the top left.
    # The warning badge over the bottom right corner.
    badge = ((x - 59) ** 2 + (y - 59) ** 2) ** .5
    if badge <= 11.5:
        if 57.6 <= x <= 60.4 and 52 <= y <= 61:
            return (255, 255, 255)
        if ((x - 59) ** 2 + (y - 64.2) ** 2) ** .5 <= 1.7:
            return (255, 255, 255)
        return ring
    if badge <= 13.5:
        return (24, 24, 22)
    # The slot name on a light plate over the hub.
    if 15 <= x <= 57 and 25 <= y <= 47:
        if not (min(x - 15, 57 - x) < 5 and min(y - 25, 47 - y) < 5
                and ((min(x - 15, 57 - x) - 5) ** 2 + (min(y - 25, 47 - y) - 5) ** 2) > 25):
            return (245, 245, 242)
    if distance <= 9:
        return (40, 40, 38)
    if distance <= 14:
        return shade((200, 200, 196), light)
    if distance <= 30:
        # The winding: the spool's own colour, with fine turns of filament.
        turn = .9 if int(distance * 1.5) % 2 else 1.
        return shade(color if sum(color) > 40 else (36, 36, 36), light * turn)
    if distance <= 33.5:
        return shade((120, 120, 116), light)
    if distance <= 36:
        return ring
    return None


def draw(slot, color, kind):
    rows = []
    for py in range(CANVAS):
        row = []
        for px in range(CANVAS):
            found = []
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    hit = color_at(px + (sx + .5) / SAMPLES, py + (sy + .5) / SAMPLES, color, BANDS[kind])
                    if hit:
                        found.append(hit)
            if not found:
                row.append((0, 0, 0, 0))
            else:
                mean = [sum(c[i] for c in found) // len(found) for i in range(3)]
                row.append((mean[0], mean[1], mean[2], 255 * len(found) // SAMPLES ** 2))
        rows.append(row)
    # The rack spool's name does not fit the plate: it is shown as R.
    SlotLabel("R" if slot == "Rack" else slot, 36, 39).paint(rows, (24, 24, 22))
    return rows


def to_bin(rows):
    data = bytearray(struct.pack("<I", 5 | CANVAS << 10 | CANVAS << 21))
    for row in rows:
        for red, green, blue, alpha in row:
            data += bytes((blue, green, red, alpha))
    return bytes(data)


def to_png(rows):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    raw = b"".join(b"\0" + bytes(v for pixel in row for v in pixel) for row in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", CANVAS, CANVAS, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def write_bin(path, slot, color, kind):
    """The screen file for one spool; color is RRGGBB."""
    data = to_bin(draw(slot, tuple(bytes.fromhex(color)), kind))
    temporary = pathlib.Path(str(path) + ".new")
    temporary.write_bytes(data)
    temporary.replace(path)


def main():
    # plastic_picture.py FOLDER NAME SLOT RRGGBB short|low
    folder, name, slot, color, kind = sys.argv[1:6]
    rows = draw(slot, tuple(bytes.fromhex(color)), kind)
    folder = pathlib.Path(folder)
    (folder / (name + ".bin")).write_bytes(to_bin(rows))
    (folder / (name + ".png")).write_bytes(to_png(rows))


if __name__ == "__main__":
    main()
