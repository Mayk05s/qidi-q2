"""The low-plastic window on the printer screen, shown from the panel service.

The screen has no window of its own for this, so the two windows of Box 3 and Box 4, which a
printer with one Box never needs, carry it. screen_popup_setup.py keeps their stock files.
The screen reads a window's picture each time it opens but its text only when it starts, so the
text is written and the screen program is restarted before the window is raised.
"""

import json
import os
import pathlib
import time
import urllib.parse

import plastic_picture

CLIENT = pathlib.Path("/home/qidi/QIDI_Client")
PANEL = pathlib.Path("/home/qidi/qidi_panel")
STOCK = PANEL / "screen_stock"
RELOAD_FLAG = PANEL / "screen_reload"
PICTURES = CLIENT / "access" / "plastic"
# kind: (stock window, Box number that raises it, window type, title)
WINDOWS = {"short": ("QDC_300_002_005_003", 3, 3, "Не хватит пластика"),
           "low": ("QDC_300_002_005_004", 4, 2, "Пластика может не хватить")}
LANGUAGES = ("zh_CN", "en_US", "ja_JP", "ru_RU", "fr_FR", "de_DE", "it_IT", "es_ES", "ko_KR", "pt_PT")
SCREEN_START = 10


def installed():
    return all((STOCK / (code + ".json")).exists() for code, _, _, _ in WINDOWS.values()) and PICTURES.is_dir()


def choose(rows):
    """(kind, the spool to show, the other short ones) or None when all is fine."""
    short = [row for row in rows if row["verdict"] == "short"]
    if short:
        return "short", short[0], short[1:]
    low = [row for row in rows if row["verdict"] == "risk"]
    if low:
        return "low", low[0], low[1:]
    return None


def text(kind, row, others):
    body = "Катушка %s: нужно %d г, осталось %d г." % (row["label"], round(row["left_to_print"]), round(row["remaining"]))
    if kind == "short":
        body += " Подготовьте новую катушку."
    if others:
        body += " Ещё: %s." % (", ".join(other["label"] for other in others),)
    return body


def window_file(kind, body):
    code, _, error_type, title = WINDOWS[kind]
    window = json.loads((STOCK / (code + ".json")).read_text(encoding="utf-8"))
    window["message"] = title
    window["error_type"] = error_type
    window["message_translation"] = {language: title for language in LANGUAGES}
    window["prossess_msg_translation"] = {language: body for language in LANGUAGES}
    window["popup_stlye"]["main_pic"] = str(PICTURES / (kind + ".bin"))
    return json.dumps(window, ensure_ascii=False, indent=4)


def show(printer, rows, wait=time.sleep):
    """Shows the window for the worst spool. False when it cannot, then the caller falls back."""
    chosen = choose(rows)
    if chosen is None or not installed():
        return False
    kind, row, others = chosen
    if not row.get("color") or row.get("label") is None:
        return False
    code, box, _, _ = WINDOWS[kind]
    plastic_picture.write_bin(PICTURES / (kind + ".bin"), row["label"], row["color"], kind)
    target = CLIENT / "resource" / "qidi_client_error" / code / "0.json"
    target.write_text(window_file(kind, text(kind, row, others)), encoding="utf-8")
    # The root path unit sees the flag and restarts the screen program.
    RELOAD_FLAG.write_text(str(time.time()))
    wait(SCREEN_START)
    script = "BAMBU_SCREEN_WARNING BOX=%d" % (box,)
    printer.fetch("/printer/gcode/script?" + urllib.parse.urlencode({"script": script}), post=True)
    return True
