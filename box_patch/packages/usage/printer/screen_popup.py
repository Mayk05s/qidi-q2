"""The low-plastic window on the printer screen, shown from the panel service.

The screen has no window of its own for this, so the two windows of Box 3 and Box 4, which a
printer with one Box never needs, carry it. screen_popup_setup.py keeps their stock files.
The screen reads a window's picture each time it opens but its text only when it starts, so the
text is written and the screen program is restarted before the window is raised.
All the spools short of plastic or with a thin spare go into one window, the worst one first.
The window is asked for with a file, not G-code: G-code waits behind the print start and
would pop the window up after a cancel.
"""

import json
import os
import pathlib
import time

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
REQUEST_FILE = "screen_warning.txt"


def installed():
    return all((STOCK / (code + ".json")).exists() for code, _, _, _ in WINDOWS.values()) and PICTURES.is_dir()


def choose(rows):
    """(kind, problem spools with the worst first) or None when all is fine."""
    short = [row for row in rows if row["verdict"] == "short"]
    low = [row for row in rows if row["verdict"] == "risk"]
    if not short and not low:
        return None
    return ("short" if short else "low"), short + low


def text(kind, problems):
    """The text under the title on the printer screen: one line."""
    return " ".join(text_lines(kind, problems))


def text_lines(kind, problems):
    lines = []
    for row in problems:
        line = "Катушка %s: нужно %d г, осталось %d г" % (row["label"], round(row["left_to_print"]), round(row["remaining"]))
        if kind == "short" and row["verdict"] == "risk":
            line += ", впритык"
        lines.append(line + ".")
    short = sum(row["verdict"] == "short" for row in problems)
    if short:
        lines.append("Подготовьте новую катушку." if short == 1 else "Подготовьте новые катушки.")
    return lines


def studio_text(kind, problems):
    """The window QIDI Studio shows has no picture or title of its own, only this text: an
    icon, the title and a line per spool, a red mark when the spool is short, orange when thin."""
    lines = [("⛔️ " if kind == "short" else "⚠️ ") + WINDOWS[kind][3], ""]
    for row in problems:
        mark = "🔴" if row["verdict"] == "short" else "🟠"
        line = "%s  %s — нужно %d г, есть %d г" % (mark, row["label"], round(row["left_to_print"]), round(row["remaining"]))
        if kind == "short" and row["verdict"] == "risk":
            line += ", впритык"
        lines.append(line)
    short = sum(row["verdict"] == "short" for row in problems)
    if short:
        lines += ["", "Подготовьте новую катушку." if short == 1 else "Подготовьте новые катушки."]
    return "\n".join(lines)


def window_file(kind, body, studio=None):
    code, _, error_type, title = WINDOWS[kind]
    window = json.loads((STOCK / (code + ".json")).read_text(encoding="utf-8"))
    # QIDI Studio shows only the title, and the screen sends it the English or Chinese one
    # whatever its own language is: those two carry the whole message, the screen keeps the
    # short Russian title.
    full = studio or "\n".join([title, body])
    window["message"] = full
    window["error_type"] = error_type
    window["message_translation"] = {language: title for language in LANGUAGES}
    window["message_translation"].update({"en_US": full, "zh_CN": full})
    window["prossess_msg_translation"] = {language: body for language in LANGUAGES}
    window["popup_stlye"]["main_pic"] = str(PICTURES / (kind + ".bin"))
    return json.dumps(window, ensure_ascii=False, indent=4)


def show(printer, rows, wait=time.sleep):
    """Shows one window for all the problem spools, the picture is the worst one. False when
    there is nothing to show or the window is not installed."""
    chosen = choose(rows)
    if chosen is None or not installed():
        return False
    kind, problems = chosen
    row = problems[0]
    if not row.get("color") or row.get("label") is None:
        return False
    code, box, _, _ = WINDOWS[kind]
    plastic_picture.write_bin(PICTURES / (kind + ".bin"), row["label"], row["color"], kind)
    target = CLIENT / "resource" / "qidi_client_error" / code / "0.json"
    target.write_text(window_file(kind, text(kind, problems), studio_text(kind, problems)), encoding="utf-8")
    # The root path unit sees the flag and restarts the screen program.
    RELOAD_FLAG.write_text(str(time.time()))
    wait(SCREEN_START)
    # bambu_spool_usage picks the file up next to printer.cfg and drops it if the print is gone.
    printer.upload(REQUEST_FILE, "%d print\n" % (box,))
    return True
