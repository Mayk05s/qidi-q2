#!/usr/bin/env python3
"""QIDI Box panel: edit the printer's slots and read, copy or rewrite filament tags.

Runs a web UI on http://127.0.0.1:18770 and talks to a PC/SC reader (ACR122U)
through the macOS PCSC framework. No third-party packages are required.

Only blocks 4 and 5 (sector 1) are ever written. Sector trailers, keys and
access bits are never touched, so a tag cannot be locked by this tool.
Slot edits go to the printer through Moonraker, using the address in ../.env.
"""
import ctypes
import itertools
import json
import pathlib
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# bambu_rfid.py is next to this file on the printer and in ../box_patch on the Mac.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "box_patch"))
from bambu_rfid import CUSTOM_COLOR_MAGIC, QIDI_COLORS, nearest_qidi_color  # noqa: E402
import print_check  # noqa: E402
import sample_model  # noqa: E402

PORT = 18770
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
FIRMWARE_MATERIALS = pathlib.Path("/home/qidi/QIDI_Client/resource/filament_config/filament-0.4.json")
# On the printer the panel is reached by the printer's address or name in the local network.
# QIDI Studio fetches a project by link; the token keeps it from being guessed.
SAMPLE_LINK = re.compile(r"/samples/([0-9a-f]{32})/qidi_samples\.3mf")
PRINTER_MODE_HOST = re.compile(r"[A-Za-z0-9.\-]{1,253}:%d" % (PORT,))
printer_mode = False
ALL_FEATURES = ("bambu", "nfc", "spools", "usage", "mesh", "guide")
GUIDE_FILE = pathlib.Path(__file__).resolve().parent / "drying_guide.html"
FEATURES_FILE = pathlib.Path(__file__).resolve().parent / "features.json"
features = set(ALL_FEATURES)
DATA_BLOCK = 4
# Optional block holding the exact color; block 4 keeps the nearest palette code.
COLOR_BLOCK = 5
# Tried in this order: QIDI factory tags use the first, blank tags the second.
KEYS = [("qidi", "D3F7D3F7D3F7"), ("default", "FFFFFFFFFFFF")]

# From QIDITECH/QIDI_Q2 config/officiall_filas_list.cfg
MATERIALS = {
    1: "PLA Rapido", 2: "PLA Matte", 3: "PLA Metal", 4: "PLA Silk", 5: "PLA-CF",
    6: "PLA-Wood", 7: "PLA Basic", 8: "PLA Matte Basic", 10: "Support For PLA",
    11: "ABS Rapido", 12: "ABS-GF", 13: "ABS-Metal", 14: "ABS-Odorless", 18: "ASA",
    19: "ASA-Aero", 20: "ASA-CF", 23: "PC", 24: "UltraPA", 25: "PA-CF",
    26: "UltraPA-CF25", 27: "PA12-CF", 30: "PAHT-CF", 31: "PAHT-GF",
    32: "Support For PAHT", 33: "Support For PET/PA", 34: "PC/ABS-FR", 35: "TPEE",
    36: "PEBA", 37: "PET-CF", 38: "PET-GF", 39: "PETG Basic", 40: "PETG-Tough",
    41: "PETG Rapido", 42: "PETG-CF", 43: "PETG-GF", 44: "PPS-CF",
    45: "PETG Translucent", 46: "PPS-GF", 47: "PVA", 48: "TPU-AERO 64D",
    49: "TPU-Aero", 50: "TPU 95A-HF",
}
COLORS = {
    1: ("Белый", "#FAFAFA"), 2: ("Чёрный", "#060606"), 3: ("Серебристый", "#D9E3ED"),
    4: ("Ярко-зелёный", "#5CF30F"), 5: ("Мятный", "#63E492"), 6: ("Синий", "#2850FF"),
    7: ("Розовый", "#FE98FE"), 8: ("Жёлтый", "#DFD628"), 9: ("Тёмно-зелёный", "#228332"),
    10: ("Светло-голубой", "#99DEFF"), 11: ("Тёмно-синий", "#1714B0"),
    12: ("Лавандовый", "#CEC0FE"), 13: ("Лаймовый", "#CADE4B"),
    14: ("Королевский синий", "#1353AB"), 15: ("Небесный", "#5EA9FD"),
    16: ("Фиолетовый", "#A878FF"), 17: ("Коралловый", "#FE717A"), 18: ("Красный", "#FF362D"),
    19: ("Слоновая кость", "#E2DFCD"), 20: ("Серый", "#898F9B"), 21: ("Коричневый", "#6E3812"),
    22: ("Хаки", "#CAC59F"), 23: ("Оранжевый", "#F28636"), 24: ("Золотистый", "#B87F2B"),
}


class TagError(Exception):
    pass


class NoTag(TagError):
    pass


class NoReader(TagError):
    pass


class BoxTagProblem(TagError):
    """What the module on the printer reported about a tag. `code` is a key of BOX_ERRORS."""

    def __init__(self, code, uid=None):
        TagError.__init__(self, BOX_ERRORS.get(code, code))
        self.code = code
        self.uid = uid


# Readers built into the Box: Klipper object and what the panel calls it.
BOX_READERS = {"box1": ("card_reader_1", "Считыватель Box, слоты 1A и 1B"),
               "box2": ("card_reader_2", "Считыватель Box, слоты 1C и 1D")}
BOX_ERRORS = {
    "absent": "Метка не найдена: поднеси её к антенне слота и повтори",
    "no_spool": "В этом слоте нет катушки",
    "slot_loaded": "Пластик из этого слота заправлен в голову. Сначала извлеки его",
    "unknown_tag": "Это не метка Bambu и не наша. Пока голова заправлена, такую метку прочитать нельзя: задай материал вручную",
    "several": "У антенны несколько меток: убери катушку из соседнего слота",
    "bambu": "Это метка Bambu: она защищена от записи",
    "locked": "Метка защищена неизвестным ключом или была убрана во время чтения",
    "not_classic": "Это не MIFARE Classic 1K: QIDI Box такую метку не прочитает",
    "other_tag": "У антенны другая метка, не та, что была прочитана. Прочитай её заново",
    "denied": "Метка отказала в записи: блок защищён",
    "failed": "Операция не удалась, метка могла записаться частично. Держи метку у антенны и повтори",
}
# Tags that end a search: they were found, but cannot be used.
BOX_FOUND_ERRORS = ("bambu", "locked", "not_classic")
# The page repeats short steps, so a search can be cancelled and G-code is not blocked for long.
SEARCH_STEP = 2
WRITE_FIND_TIMEOUT = 10
READ_TIMEOUT = 120  # A printer command such as a tag read may run for a long time.
READ_SETTLE = 3.5


class PrinterError(Exception):
    pass


def _moonraker_message(error):
    try:
        return json.loads(error.read())["error"]["message"]
    except (ValueError, KeyError, TypeError, OSError):
        return "Принтер отклонил команду: %s" % (error,)


def tag_identity(tag):
    """What tells one spool from another. The two tags of a Bambu spool share the tray number."""
    if not isinstance(tag, dict) or tag.get("empty"):
        return None
    return tag.get("tray") or tag.get("uid")


class Printer:
    """Slot values on the printer, through the Moonraker API."""

    SLOTS = [(0, "1A"), (1, "1B"), (2, "1C"), (3, "1D")]

    def __init__(self, host, fetch=None):
        self.host = host
        self.reading = {}
        self.read_errors = {}
        self.fetch = fetch or (lambda path, post=False: self._fetch(path, post, READ_TIMEOUT if post else 8))
        self.upload = self._upload_config
        self.tag_requests = itertools.count(1)

    def _fetch(self, path, post=False, timeout=8):
        url = "http://%s:7125%s" % (self.host, path)
        request = urllib.request.Request(url, method="POST" if post else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as res:
                return json.loads(res.read())
        except urllib.error.HTTPError as e:
            raise PrinterError(_moonraker_message(e))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise PrinterError("Принтер %s не отвечает: %s" % (self.host, e))

    def _status(self):
        data = self.fetch("/printer/objects/query?save_variables&print_stats=state&bambu_spool_usage")
        try:
            return data["result"]["status"]
        except (KeyError, TypeError):
            raise PrinterError("Klipper не готов")

    def slots(self):
        status = self._status()
        values = status["save_variables"]["variables"]
        spools = (status.get("bambu_spool_usage") or {}).get("slots") or []
        result = []
        for index, label in self.SLOTS:
            color = values.get("color_slot%d" % index)
            color = color[0] if isinstance(color, list) and color else None
            filament = values.get("filament_slot%d" % index)
            color = color if isinstance(color, str) else None
            tag = values.get("bambu_tag_slot%d" % index)
            # A record that no longer matches the slot belongs to an earlier spool;
            # {"empty": True} means the last read started and found nothing yet.
            if not isinstance(tag, dict):
                tag = None
            elif not tag.get("empty") and (tag.get("color") != color or tag.get("filament") != filament):
                tag = None
            result.append({"index": index, "label": label, "filament": filament,
                           "color": color, "tag": tag, "reading": bool(self.reading.get(index)),
                           "spool": spools[index] if index < len(spools) else None,
                           "read_error": self.read_errors.get(index)})
        for slot in result:
            key = tag_identity(slot["tag"])
            slot["same_tag"] = [other["index"] for other in result if key and other is not slot
                                and tag_identity(other["tag"]) == key]
        return {"slots": result, "state": status["print_stats"]["state"]}

    def set_spool(self, index, grams):
        if index not in [i for i, _ in self.SLOTS]:
            raise PrinterError("Нет такого слота")
        if isinstance(grams, bool) or not isinstance(grams, int) or not 0 <= grams <= 5000:
            raise PrinterError("Остаток должен быть от 0 до 5000 г")
        # A file rather than G-code: G-code waits behind a running print.
        self.upload("spool_set_%d.txt" % (index,), "%d\n" % (grams,))

    def loaded_slot(self):
        """Label of the slot whose filament is in the toolhead. The stock read refuses to run then
        (QDE_004_011) and only says so in the console, so this is checked before it."""
        data = self.fetch("/printer/objects/query?box_extras=loaded_slot&"
                          + urllib.parse.quote("filament_switch_sensor filament_switch_sensor") + "=filament_detected")
        try:
            status = data["result"]["status"]
            slot = status["box_extras"]["loaded_slot"]
            detected = status["filament_switch_sensor filament_switch_sensor"]["filament_detected"]
        except (KeyError, TypeError):
            return None
        match = re.fullmatch(r"slot(\d+)", str(slot))
        if not detected or not match:
            return None
        return dict(self.SLOTS).get(int(match.group(1)), slot)

    def forget_tag(self, index):
        """Drop the tag record of a slot whose spool has moved elsewhere."""
        if index not in (0, 1, 2, 3):
            raise PrinterError("В этом слоте нет считывателя")
        if self._status()["print_stats"]["state"] in ("printing", "paused"):
            raise PrinterError("Принтер печатает, сейчас этого сделать нельзя")
        query = urllib.parse.urlencode(
            {"script": "SAVE_VARIABLE VARIABLE=bambu_tag_slot%d VALUE=\"{'empty': True}\"" % (index,)})
        self.fetch("/printer/gcode/script?" + query, post=True)

    def read_slot(self, index, skip_neighbour=False):
        """Start the stock tag read, the same one as the button on the printer screen.
        skip_neighbour takes our own read, which ignores the spool next to this one on the reader."""
        if index not in (0, 1, 2, 3):
            raise PrinterError("В этом слоте нет считывателя")
        if self._status()["print_stats"]["state"] in ("printing", "paused"):
            raise PrinterError("Принтер печатает, метку сейчас читать нельзя")
        if self.reading.get(index):
            raise PrinterError("Этот слот уже читается")
        loaded = self.loaded_slot()
        if loaded == dict(self.SLOTS)[index]:
            raise PrinterError("Пластик из слота %s заправлен в голову. Сначала извлеки его на экране принтера" % (loaded,))
        if loaded or skip_neighbour:
            # The stock read refuses while any slot is loaded, so the tag is read by our module.
            self.reading[index] = True
            self.read_errors.pop(index, None)
            self.read_thread = threading.Thread(target=self._read_with_motor, args=(index,), daemon=True)
            self.read_thread.start()
            return
        # What was known about the tag is forgotten first, so the result is only this read.
        clear = urllib.parse.urlencode(
            {"script": "SAVE_VARIABLE VARIABLE=bambu_tag_slot%d VALUE=\"{'empty': True}\"" % (index,)})
        self.fetch("/printer/gcode/script?" + clear, post=True)
        self.reading[index] = True
        self.read_errors.pop(index, None)
        query = urllib.parse.urlencode({"script": "READ_RFID SLOT=%d" % (index,)})
        # The command answers only when the motor stops, so the page follows the slot instead.
        self.read_thread = threading.Thread(
            target=self._send_read, args=(index, "/printer/gcode/script?" + query), daemon=True)
        self.read_thread.start()

    def _send_read(self, index, path):
        try:
            self.fetch(path, post=True)
            # The module saves what it read a few seconds after the stock code finishes.
            time.sleep(READ_SETTLE)
        except PrinterError as e:
            self.read_errors[index] = str(e)
        finally:
            self.reading[index] = False

    def _read_with_motor(self, index):
        try:
            neighbour = self._status()["save_variables"]["variables"].get("bambu_tag_slot%d" % (index ^ 1))
            neighbour = neighbour if isinstance(neighbour, dict) else {}
            args = "SLOT=%d" % (index,)
            if neighbour.get("uid"):
                args += " EXCLUDE=" + neighbour["uid"]
            if neighbour.get("tray"):
                args += " TRAYS=" + neighbour["tray"]
            reader = "card_reader_%d" % (index // 2 + 1,)
            try:
                result = self.tag_command(reader, ("BAMBU_RFID_SLOT_READ", args))
            except BoxTagProblem as e:
                if e.code == "absent":
                    raise PrinterError("Метка не найдена: мотор покачал катушку, метка не дошла до антенны")
                raise
            self._save_read_tag(index, result)
        except (PrinterError, TagError) as e:
            self.read_errors[index] = str(e)
        finally:
            self.reading[index] = False

    def _save_read_tag(self, index, result):
        filament = result.get("profile") or legacy_filament(result.get("material"))
        if not filament:
            raise PrinterError("Нет профиля для «%s». Задай его в редакторе слота или в bambu_map.cfg" % (result.get("detail"),))
        self.save_slot(index, filament, result["color"])
        record = {k: v for k, v in result.items() if isinstance(v, (int, float))
                  or (isinstance(v, str) and re.fullmatch(r"[\w +.\-]*", v))}
        for key in ("ok", "id", "material", "profile"):
            record.pop(key, None)
        record["filament"] = filament
        query = urllib.parse.urlencode(
            {"script": "SAVE_VARIABLE VARIABLE=bambu_tag_slot%d VALUE=\"%r\"" % (index, record)})
        self.fetch("/printer/gcode/script?" + query, post=True)

    def tag_command(self, reader, script):
        """Run a tag command of our module on the Box reader and return its result."""
        if self._status()["print_stats"]["state"] in ("printing", "paused"):
            raise PrinterError("Принтер печатает, считыватель сейчас занят")
        # Local: several slots can be read at once, each thread must check its own request.
        request = next(self.tag_requests)
        query = urllib.parse.urlencode({"script": "%s READER=%s ID=%d %s" % (
            script[0], reader, request, script[1])})
        self.fetch("/printer/gcode/script?" + query, post=True)
        name = "bambu_rfid " + reader
        data = self.fetch("/printer/objects/query?" + urllib.parse.quote(name))
        try:
            result = data["result"]["status"][name]["tag_result"]
        except (KeyError, TypeError):
            result = None
        if not result or result.get("id") != request:
            raise PrinterError("Принтер не прислал результат чтения. Повтори ещё раз")
        if not result["ok"]:
            raise BoxTagProblem(result["error"], result.get("uid"))
        return result

    def save_slot(self, index, filament, color):
        if index not in [i for i, _ in self.SLOTS]:
            raise PrinterError("Нет такого слота")
        if not isinstance(filament, str) or not re.fullmatch(r"QF[A-Z0-9]{2,4}", filament):
            raise PrinterError("Неверный код материала")
        if not isinstance(color, str) or not re.fullmatch(r"[0-9A-F]{6}", color):
            raise PrinterError("Неверный цвет")
        if self._status()["print_stats"]["state"] in ("printing", "paused"):
            raise PrinterError("Принтер печатает, слоты сейчас менять нельзя")
        for script in ("SAVE_VARIABLE VARIABLE=filament_slot%d VALUE=\"'%s'\"" % (index, filament),
                       "SAVE_VARIABLE VARIABLE=color_slot%d VALUE=\"['%s']\"" % (index, color)):
            query = urllib.parse.urlencode({"script": script})
            self.fetch("/printer/gcode/script?" + query, post=True)


    def _map_text(self):
        try:
            with urllib.request.urlopen(
                    "http://%s:7125/server/files/config/%s" % (self.host, MAP_FILE), timeout=8) as res:
                return res.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as e:
            raise PrinterError("Нет файла %s на принтере, сначала выполни установку: %s" % (MAP_FILE, e))

    def _upload_map(self, text):
        self.upload(MAP_FILE, text)

    def _upload_config(self, name, text):
        boundary = "qidipanelboundary"
        body = ("--%s\r\nContent-Disposition: form-data; name=\"root\"\r\n\r\nconfig\r\n"
                "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                "Content-Type: text/plain\r\n\r\n%s\r\n--%s--\r\n"
                % (boundary, boundary, name, text, boundary)).encode("utf-8")
        request = urllib.request.Request(
            "http://%s:7125/server/files/upload" % (self.host,), data=body, method="POST",
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        try:
            urllib.request.urlopen(request, timeout=8).close()
        except (urllib.error.URLError, OSError) as e:
            raise PrinterError("Не удалось сохранить %s: %s" % (name, e))

    def save_mapping(self, index, detail, tag_color, filament, color):
        """Save the rule, apply it to the slot and keep the slot's tag record valid."""
        self._upload_map(update_map(self._map_text(), detail, tag_color, filament, color))
        record = self._status()["save_variables"]["variables"].get("bambu_tag_slot%d" % index)
        self.save_slot(index, filament, color)
        if isinstance(record, dict):
            kept = {k: v for k, v in record.items()
                    if isinstance(k, str) and isinstance(v, (int, float))
                    or (isinstance(v, str) and re.fullmatch(r"[\w +.\-]*", v))}
            kept.update(detail=detail, tag_color=tag_color, color=color, filament=filament)
            query = urllib.parse.urlencode(
                {"script": "SAVE_VARIABLE VARIABLE=bambu_tag_slot%d VALUE=\"%r\"" % (index, kept)})
            self.fetch("/printer/gcode/script?" + query, post=True)


MAP_FILE = "bambu_map.cfg"
MY_COLORS_FILE = pathlib.Path(__file__).resolve().parent / "my_colors.json"
MY_COLORS_LIMIT = 60
MY_MATERIALS_FILE = pathlib.Path(__file__).resolve().parent / "my_materials.json"


class MyMaterials:
    """Plastic types the user enabled and the profiles chosen inside them."""

    def __init__(self, path=MY_MATERIALS_FILE):
        self.path = path
        self.lock = threading.Lock()

    def load(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        ids = data.get("ids") if isinstance(data.get("ids"), list) else []
        types = data.get("types") if isinstance(data.get("types"), list) else []
        return {"ids": [i for i in ids if isinstance(i, str) and re.fullmatch(r"QF[A-Z0-9]{2,4}", i)],
                "types": [x for x in types if isinstance(x, str) and re.fullmatch(r"\w{1,20}", x)]}

    def change(self, add=None, remove=None, type_on=None, type_off=None):
        for value in (add, remove):
            if value is not None and not (isinstance(value, str) and re.fullmatch(r"QF[A-Z0-9]{2,4}", value)):
                raise TagError("Неверный код материала")
        for value in (type_on, type_off):
            if value is not None and not (isinstance(value, str) and re.fullmatch(r"\w{1,20}", value)):
                raise TagError("Неверный тип пластика")
        with self.lock:
            data = self.load()
            for key, on, off in (("ids", add, remove), ("types", type_on, type_off)):
                if off in data[key]:
                    data[key].remove(off)
                if on is not None and on not in data[key]:
                    data[key].append(on)
            self.path.write_text(json.dumps(data))
            return data


class MyColors:
    """The user's own palette, kept in a file so it survives restarts."""

    def __init__(self, path=MY_COLORS_FILE):
        self.path = path
        self.lock = threading.Lock()

    def load(self):
        try:
            colors = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        if not isinstance(colors, list):
            return []
        return [c for c in colors if isinstance(c, str) and re.fullmatch(r"[0-9A-F]{6}", c)]

    def change(self, add=None, remove=None):
        for value in (add, remove):
            if value is not None and not (isinstance(value, str) and re.fullmatch(r"[0-9A-F]{6}", value)):
                raise TagError("Цвет: недопустимое значение")
        with self.lock:
            colors = self.load()
            if remove in colors:
                # Editing a color keeps its place in the palette.
                position = colors.index(remove)
                colors.remove(remove)
                if add is not None and add not in colors:
                    colors.insert(position, add)
            if add is not None and add not in colors:
                if len(colors) >= MY_COLORS_LIMIT:
                    raise TagError("В палитре уже %d цветов, удали ненужные" % (MY_COLORS_LIMIT,))
                colors.append(add)
            self.path.write_text(json.dumps(colors))
            return colors


def _set_map_line(lines, section, key, value):
    """Replace, add or, with value None, remove one rule. Other lines stay as they are."""
    header = "[%s]" % (section,)
    if header not in [line.strip() for line in lines]:
        lines.extend(["", header])
    start = [line.strip() for line in lines].index(header) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("[")), len(lines))
    for i in range(start, end):
        stripped = lines[i].strip()
        if not stripped.startswith(("#", ";")) and "=" in stripped \
                and stripped.split("=", 1)[0].strip() == key:
            if value is None:
                del lines[i]
            else:
                lines[i] = "%s = %s" % (key, value)
            return
    if value is not None:
        while end > start and not lines[end - 1].strip():
            end -= 1
        lines.insert(end, "%s = %s" % (key, value))


def update_map(text, detail, tag_color, filament, color):
    """Set the rules for one real tag: its name to a profile, its color to a shown color."""
    if not isinstance(detail, str) or not re.fullmatch(r"[\w][\w +.\-]{0,31}", detail):
        raise PrinterError("Неверное название с метки")
    if not isinstance(filament, str) or not re.fullmatch(r"QF[A-Z0-9]{2,4}", filament):
        raise PrinterError("Неверный код материала")
    for value in (tag_color, color):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9A-F]{6}", value):
            raise PrinterError("Неверный цвет")
    lines = text.splitlines()
    _set_map_line(lines, "materials", detail, filament)
    _set_map_line(lines, "colors", tag_color, None if color == tag_color else color)
    return "\n".join(lines) + "\n"


def read_printer_host(path):
    try:
        text = path.read_text()
    except OSError:
        return None
    match = re.search(r"^PRINTER_HOST=(\S+)", text, re.M)
    return match.group(1) if match else None


def legacy_filament(code):
    """Profile id for a tag's material code. The list names materials without the QIDI brand
    and sometimes shorter than the profile: "PLA Matte" is "QIDI PLA Matte Basic"."""
    name = "QIDI " + MATERIALS.get(code, "?")
    found = [m for m in load_materials() if m["name"] == name or m["name"].startswith(name + " ")]
    return min(found, key=lambda m: len(m["name"]))["id"] if found else None


def load_materials(path=pathlib.Path(__file__).resolve().parent / "qidi_materials.json"):
    return json.loads(path.read_text(encoding="utf-8"))


class PcscTransport:
    """Minimal PC/SC binding. On macOS DWORD, LONG and handles are 32-bit."""

    SHARED, T0, T1 = 2, 1, 2
    LEAVE, RESET = 0, 1
    NO_READER_CODES = {0x8010002E, 0x8010001D, 0x8010001E, 0x80100017, 0x80100009, 0x80100003}
    NO_TAG_CODES = {0x8010000C, 0x80100069, 0x80100066, 0x80100067}

    def __init__(self):
        self.lib = ctypes.CDLL("/System/Library/Frameworks/PCSC.framework/PCSC")
        self.ctx = ctypes.c_int32()
        self._check(self.lib.SCardEstablishContext(2, None, None, ctypes.byref(self.ctx)))
        self.card = None
        self.protocol = ctypes.c_uint32()

    @staticmethod
    def _code(rv):
        return rv & 0xFFFFFFFF

    def _check(self, rv):
        code = self._code(rv)
        if code == 0:
            return
        if code in self.NO_READER_CODES:
            raise NoReader("Ридер не найден")
        if code in self.NO_TAG_CODES:
            raise NoTag("Метка не на ридере")
        raise TagError(f"Ошибка PC/SC 0x{code:08X}")

    def reader_name(self):
        size = ctypes.c_uint32(0)
        self._check(self.lib.SCardListReaders(self.ctx, None, None, ctypes.byref(size)))
        buf = ctypes.create_string_buffer(size.value)
        self._check(self.lib.SCardListReaders(self.ctx, None, buf, ctypes.byref(size)))
        names = [n for n in buf.raw.decode(errors="replace").split("\0") if n]
        if not names:
            raise NoReader("Ридер не найден")
        return names[0]

    def connect(self):
        handle = ctypes.c_int32()
        self._check(self.lib.SCardConnect(
            self.ctx, self.reader_name().encode(), self.SHARED, self.T0 | self.T1,
            ctypes.byref(handle), ctypes.byref(self.protocol)))
        self.card = handle

    def close(self):
        self.lib.SCardReleaseContext(self.ctx)

    def disconnect(self, reset=False):
        if self.card is not None:
            self.lib.SCardDisconnect(self.card, self.RESET if reset else self.LEAVE)
            self.card = None

    def atr(self):
        state, proto = ctypes.c_uint32(), ctypes.c_uint32()
        name = ctypes.create_string_buffer(256)
        name_len = ctypes.c_uint32(256)
        atr = (ctypes.c_ubyte * 33)()
        atr_len = ctypes.c_uint32(33)
        self._check(self.lib.SCardStatus(
            self.card, name, ctypes.byref(name_len), ctypes.byref(state),
            ctypes.byref(proto), atr, ctypes.byref(atr_len)))
        return bytes(atr[:atr_len.value])

    def transmit(self, apdu):
        symbol = "g_rgSCardT1Pci" if self.protocol.value == self.T1 else "g_rgSCardT0Pci"
        pci = ctypes.byref(ctypes.c_uint32.in_dll(self.lib, symbol))
        send = (ctypes.c_ubyte * len(apdu))(*apdu)
        recv = (ctypes.c_ubyte * 258)()
        recv_len = ctypes.c_uint32(258)
        self._check(self.lib.SCardTransmit(
            self.card, pci, send, len(apdu), None, recv, ctypes.byref(recv_len)))
        return bytes(recv[:recv_len.value])


class Tag:
    """MIFARE Classic operations over ACR122U pseudo-APDUs."""

    def __init__(self, transport):
        self.t = transport

    def _apdu(self, apdu):
        res = self.t.transmit(apdu)
        if len(res) < 2 or res[-2:] != b"\x90\x00":
            return None
        return res[:-2]

    def probe(self):
        """Connect and return (uid, is_classic_1k). Caller must disconnect."""
        self.t.connect()
        atr = self.t.atr()
        uid = self._apdu(bytes.fromhex("FFCA000000"))
        if uid is None:
            raise TagError("Не удалось прочитать UID")
        # PC/SC part 3 ATR: bytes 13-14 are the card name, 00 01 = Classic 1K.
        return uid.hex().upper(), len(atr) >= 15 and atr[13:15] == b"\x00\x01"

    def _authenticate(self, block):
        """Try each key. A failed auth halts the tag, so reconnect between tries."""
        for name, key in KEYS:
            self.t.disconnect(reset=True)
            self.t.connect()
            if self._apdu(bytes.fromhex("FF82000006" + key)) is None:
                raise TagError("Ридер не принял ключ")
            auth = bytes.fromhex(f"FF860000050100{block:02X}6000")
            if self._apdu(auth) is not None:
                return name
        raise TagError("Сектор 1 защищён неизвестным ключом")

    def _read_block(self, block):
        data = self._apdu(bytes.fromhex(f"FFB000{block:02X}10"))
        if data is None or len(data) != 16:
            raise TagError(f"Не удалось прочитать блок {block}")
        return data

    def read(self):
        try:
            uid, classic = self.probe()
            if not classic:
                raise TagError("Это не MIFARE Classic 1K: QIDI Box такую метку не прочитает")
            key = self._authenticate(DATA_BLOCK)
            color_block = self._read_block(COLOR_BLOCK)
            return {
                "uid": uid,
                "key": key,
                "block": self._read_block(DATA_BLOCK).hex().upper(),
                "extra": [color_block.hex().upper(), self._read_block(6).hex().upper()],
                "color": custom_color(color_block),
                "profile": custom_profile(color_block),
            }
        finally:
            self.t.disconnect(reset=True)

    def _write_checked(self, block, data):
        if self._apdu(bytes.fromhex(f"FFD600{block:02X}10") + data) is None:
            raise TagError("Метка отказала в записи: блок защищён")
        if self._read_block(block) != data:
            raise TagError("Проверка не прошла: прочитаны другие данные")

    def write_tag(self, data, color=None, profile=None):
        """Write block 4 and the color block. A color block with foreign data is left
        alone; color=None clears only a block this tool wrote earlier."""
        if len(data) != 16:
            raise TagError("Блок должен быть 16 байт")
        try:
            uid, classic = self.probe()
            if not classic:
                raise TagError("Это не MIFARE Classic 1K: QIDI Box такую метку не прочитает")
            key = self._authenticate(DATA_BLOCK)
            previous = self._read_block(DATA_BLOCK)
            previous_color = self._read_block(COLOR_BLOCK)
            self._write_checked(DATA_BLOCK, data)
            ours = color_block_is_ours(previous_color)
            if ours:
                wanted = color_block(color, profile) if color else bytes(16)
                if wanted != previous_color:
                    self._write_checked(COLOR_BLOCK, wanted)
            return {"uid": uid, "key": key, "previous": previous.hex().upper(),
                    "block": data.hex().upper(),
                    "exact_saved": bool(color) and ours,
                    "exact_skipped": bool(color) and not ours}
        finally:
            self.t.disconnect(reset=True)

    def write_block4(self, data):
        return self.write_tag(data)


class BoxTag:
    """The same operations as Tag, through a reader built into the Box."""

    def __init__(self, printer, reader):
        self.printer = printer
        self.reader = reader

    def read(self, timeout=15, exclude=()):
        args = "TIMEOUT=%d" % (timeout,)
        if exclude:
            args += " EXCLUDE=" + ",".join(exclude)
        result = self.printer.tag_command(self.reader, ("BAMBU_RFID_TAG_READ", args))
        blocks = result["blocks"]
        color_data = bytes.fromhex(blocks[str(COLOR_BLOCK)])
        return {"uid": result["uid"], "key": result["key"], "block": blocks[str(DATA_BLOCK)],
                "extra": [blocks[str(COLOR_BLOCK)], blocks["6"]],
                "color": custom_color(color_data), "profile": custom_profile(color_data)}

    def write_tag(self, data, color=None, profile=None, uid=None):
        """Only the tag the user read may be written: a spool in the slot is at the same antenna."""
        if uid is None:
            raise TagError("Сначала прочитай метку")
        current = self.read(WRITE_FIND_TIMEOUT)
        if current["uid"] != uid:
            raise TagError(BOX_ERRORS["other_tag"])
        previous = bytes.fromhex(current["block"])
        previous_color = bytes.fromhex(current["extra"][0])
        ours = color_block_is_ours(previous_color)
        args = "UID=%s BLOCK4=%s" % (uid, data.hex().upper())
        wanted = color_block(color, profile) if color else bytes(16)
        if ours and wanted != previous_color:
            args += " BLOCK5=" + wanted.hex().upper()
        result = self.printer.tag_command(self.reader, ("BAMBU_RFID_TAG_WRITE", args))
        return {"uid": result["uid"], "key": result["key"], "previous": current["block"],
                "block": data.hex().upper(), "exact_saved": bool(color) and ours,
                "exact_skipped": bool(color) and not ours}


def color_block_is_ours(block):
    """A color block with foreign data is left alone."""
    return block == bytes(16) or custom_color(block) is not None


def color_block(hex_color, profile=None):
    name = (profile or "").encode("ascii")
    return CUSTOM_COLOR_MAGIC + bytes.fromhex(hex_color) + name + bytes(9 - len(name))


def custom_profile(block):
    if block[:4] != CUSTOM_COLOR_MAGIC:
        return None
    return block[7:13].split(b"\0")[0].decode("ascii", "replace") or None


def custom_color(block):
    if block[:4] == CUSTOM_COLOR_MAGIC:
        return block[4:7].hex().upper()
    return None


def build_block(material, color, vendor, tail_hex):
    """Compose block 4. tail_hex holds bytes 3-15, kept verbatim from a source tag."""
    for name, value, low, high in (("Материал", material, 1, 99), ("Цвет", color, 1, 24),
                                   ("Производитель", vendor, 0, 255)):
        if not isinstance(value, int) or not low <= value <= high:
            raise TagError(f"{name}: недопустимое значение")
    if not re.fullmatch(r"[0-9A-Fa-f]{26}", tail_hex or ""):
        raise TagError("Хвост блока должен быть 13 байт в hex")
    return bytes([material, color, vendor]) + bytes.fromhex(tail_hex)


class App:
    def __init__(self, transport=None, printer=None, my_colors=None, my_materials=None):
        self.lock = threading.Lock()
        # Separate from the USB lock: a Box read waits for a tag and must not hold up the status.
        self.box_lock = threading.Lock()
        self.transport = transport
        self.printer = printer
        self.my_colors = my_colors or MyColors()
        self.my_materials = my_materials or MyMaterials()

    def _tag(self):
        if printer_mode:
            raise NoReader("На принтере нет ридера")
        if self.transport is None:
            self.transport = PcscTransport()
        return Tag(self.transport)

    def status(self):
        with self.lock:
            try:
                tag = self._tag()
                reader = self.transport.reader_name()
            except TagError as e:
                # A context from before an unplug stays invalid, so make a new one.
                if self.transport is not None:
                    self.transport.close()
                self.transport = None
                return {"reader": None, "uid": None, "message": str(e)}
            try:
                uid, classic = tag.probe()
                return {"reader": reader, "uid": uid, "classic": classic}
            except TagError:
                return {"reader": reader, "uid": None}
            finally:
                self.transport.disconnect()

    def _box_tag(self, source):
        return BoxTag(self._require_printer(), BOX_READERS[source][0])

    def read(self, body=None):
        source = (body or {}).get("source")
        if source in BOX_READERS:
            with self.box_lock:
                return self._box_tag(source).read()
        with self.lock:
            return self._tag().read()

    def find(self, body):
        """One step of a search on a Box reader. Only a usable or a recognised tag ends it."""
        source = body.get("source")
        exclude = body.get("exclude") or []
        if source not in BOX_READERS or not isinstance(exclude, list) or not all(
                isinstance(uid, str) and re.fullmatch(r"[0-9A-F]{8}", uid) for uid in exclude):
            raise ValueError("bad search")
        with self.box_lock:
            try:
                return {"status": "found", "tag": self._box_tag(source).read(SEARCH_STEP, exclude)}
            except BoxTagProblem as e:
                if e.code in BOX_FOUND_ERRORS:
                    return {"status": "unusable", "code": e.code, "uid": e.uid, "message": str(e)}
                return {"status": "searching", "code": e.code, "message": str(e)}

    def _require_printer(self):
        if self.printer is None:
            raise PrinterError("Нет PRINTER_HOST в файле .env")
        return self.printer

    def slots(self):
        return self._require_printer().slots()

    def print_check(self, name):
        if getattr(self, "plastic_check", None) is None:
            self.plastic_check = print_check.PrintCheck(self._require_printer())
        return self.plastic_check.check(name)

    def set_spool(self, body):
        self._require_printer().set_spool(body.get("index"), body.get("grams"))
        return {"ok": True}

    def read_slot(self, body):
        self._require_printer().read_slot(body.get("index"), body.get("skip_neighbour") is True)
        return {"ok": True}

    def forget_tag(self, body):
        self._require_printer().forget_tag(body.get("index"))
        return {"ok": True}

    def samples(self, body):
        colors = {s["index"]: s["color"] for s in self._require_printer().slots()["slots"] if s["color"]}
        return sample_model.build_project(self._sample_list(body), colors)

    def sample_link(self, body):
        """Build the project and keep it for QIDI Studio to fetch. Only the latest one is kept."""
        token = secrets.token_hex(16)
        self.sample_file = (token, self.samples(body))
        return {"path": "/samples/%s/qidi_samples.3mf" % (token,)}

    def open_samples(self, body, run=subprocess.run):
        """On the Mac the panel starts QIDI Studio itself, so the browser asks nothing."""
        if printer_mode:
            raise PrinterError("На принтере панель не может открыть программу на компьютере")
        folder = pathlib.Path(tempfile.gettempdir()) / "qidi_samples"
        folder.mkdir(exist_ok=True)
        path = folder / "qidi_samples.3mf"
        path.write_bytes(self.samples(body))
        if run(["open", "-a", "QIDIStudio", str(path)]).returncode != 0:
            raise PrinterError("Не удалось запустить QIDI Studio. Нажми «Скачать 3MF»")
        return {"ok": True}

    def sample_file_data(self, token):
        saved = getattr(self, "sample_file", None)
        return saved[1] if saved and secrets.compare_digest(saved[0], token) else None

    def samples_preview(self, body):
        return sample_model.preview(self._sample_list(body))

    def _sample_list(self, body):
        """Samples from the slots' current data, grouped by slot so each runs in one go."""
        items = body.get("items")
        if not isinstance(items, list):
            raise PrinterError("Неверный запрос")
        slots = {s["index"]: s for s in self._require_printer().slots()["slots"]}
        profiles = {m["id"]: m for m in load_materials()}
        samples = []
        for item in items:
            slot = slots.get(item.get("index")) if isinstance(item, dict) else None
            count = item.get("count") if isinstance(item, dict) else None
            if slot is None or not isinstance(count, int) or not 1 <= count <= 100:
                raise PrinterError("Неверное количество образцов")
            tag = slot.get("tag") or {}
            if item.get("filament") is not None and item["filament"] not in profiles:
                raise PrinterError("Неверный код материала")
            if tag.get("detail") and not item.get("filament"):
                source, material = tag, "%s %s" % (tag.get("brand", ""), tag["detail"])
            else:
                # Text comes from the profile; the plastic is whatever this slot holds.
                source = profiles.get(item.get("filament") or slot["filament"]) or {}
                material = source.get("name", "")
            sample = {"slot": slot["index"], "material": material.strip()}
            for key in ("min_temp", "max_temp", "dry_temp", "dry_hours"):
                if isinstance(source.get(key), int):
                    sample[key] = source[key]
            samples.extend([sample] * count)
        return samples

    def save_mapping(self, body):
        """Save the rule for this tag and apply it to the slot right away."""
        self._require_printer().save_mapping(body.get("index"), body.get("detail"), body.get("tag_color"),
                                             body.get("filament"), body.get("color"))
        return {"ok": True}

    def write(self, body):
        color = body.get("color")
        if not isinstance(color, str) or not re.fullmatch(r"[0-9A-F]{6}", color):
            raise TagError("Цвет: недопустимое значение")
        code = nearest_qidi_color(color)
        block = build_block(body.get("material"), code, body.get("vendor"), body.get("tail"))
        profile = body.get("profile")
        if profile is not None and not (isinstance(profile, str) and re.fullmatch(r"QF[A-Z0-9]{2,4}", profile)):
            raise TagError("Неверный код материала")
        # A palette color without a profile needs no extra block: the codes describe it.
        exact = color if profile or QIDI_COLORS[code] != color else None
        source = body.get("source")
        if source in BOX_READERS:
            uid = body.get("uid")
            if not isinstance(uid, str) or not re.fullmatch(r"[0-9A-F]{8}", uid):
                raise TagError("Сначала прочитай метку")
            with self.box_lock:
                result = self._box_tag(source).write_tag(block, exact, profile, uid)
        else:
            with self.lock:
                result = self._tag().write_tag(block, exact, profile)
        result["palette_code"] = code
        # What the page shows after a write comes from the tag, not from the form.
        try:
            if source in BOX_READERS:
                with self.box_lock:
                    result["readback"] = self._box_tag(source).read(WRITE_FIND_TIMEOUT)
            else:
                with self.lock:
                    result["readback"] = self._tag().read()
            if result["readback"]["uid"] != result["uid"]:
                raise TagError(BOX_ERRORS["other_tag"])
        except (TagError, PrinterError) as e:
            result.pop("readback", None)
            result["readback_error"] = str(e)
        return result


class Handler(BaseHTTPRequestHandler):
    app = None

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if self.path == "/api/codes":
            # Read-only; the Fluidd button script uses it from the printer's own page.
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        # Blocks DNS rebinding: other sites must not reach the reader through this server.
        host = self.headers.get("Host", "")
        return bool(PRINTER_MODE_HOST.fullmatch(host)) if printer_mode else host in ALLOWED_HOSTS

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        if self.path == "/":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if self.path == "/api/status":
            return self._send(200, self.app.status())
        if self.path == "/api/codes":
            return self._send(200, {
                "materials": [{"code": c, "name": n} for c, n in MATERIALS.items()],
                "colors": [{"code": c, "name": n, "hex": h} for c, (n, h) in COLORS.items()],
                "slot_materials": load_materials(),
                "my_colors": self.app.my_colors.load(),
                "my_materials": self.app.my_materials.load(),
                "on_printer": printer_mode,
                "features": sorted(features),
                "box_readers": [{"id": key, "name": value[1]} for key, value in BOX_READERS.items()]
                if self.app.printer else [],
            })
        if self.path == "/api/slots":
            try:
                return self._send(200, self.app.slots())
            except PrinterError as e:
                return self._send(502, {"error": str(e)})
        if self.path == "/guide" and "guide" in features and GUIDE_FILE.exists():
            return self._send(200, GUIDE_FILE.read_bytes(), "text/html; charset=utf-8")
        if self.path == "/print_check.js":
            script = print_check.SCRIPT if "usage" in features else ""
            return self._send(200, script.encode(), "text/javascript; charset=utf-8")
        if self.path.partition("?")[0] == "/api/print_check":
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            try:
                return self._send(200, self.app.print_check((query.get("file") or [None])[0]))
            except (PrinterError, print_check.CheckError) as e:
                return self._send(502, {"error": str(e)})
        link = SAMPLE_LINK.fullmatch(self.path)
        data = self.app.sample_file_data(link.group(1)) if link else None
        if data is not None:
            return self._send(200, data, "application/vnd.ms-package.3dmanufacturing-3dmodel+xml")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        origin = self.headers.get("Origin", "")
        origin_ok = origin.split("://")[-1] == self.headers.get("Host")
        if not self._host_ok() or not origin_ok or \
                self.headers.get("Content-Type") != "application/json":
            return self._send(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/read":
                return self._send(200, self.app.read(body))
            if self.path == "/api/find":
                return self._send(200, self.app.find(body))
            if self.path == "/api/write":
                return self._send(200, self.app.write(body))
            if self.path == "/api/my_materials":
                return self._send(200, {"my_materials": self.app.my_materials.change(
                    body.get("add"), body.get("remove"), body.get("type_on"), body.get("type_off"))})
            if self.path == "/api/my_colors":
                return self._send(200, {"my_colors": self.app.my_colors.change(body.get("add"), body.get("remove"))})
            if self.path == "/api/samples_preview":
                return self._send(200, self.app.samples_preview(body))
            if self.path == "/api/samples_open":
                return self._send(200, self.app.open_samples(body))
            if self.path == "/api/samples_link":
                return self._send(200, self.app.sample_link(body))
            if self.path == "/api/samples":
                data = self.app.samples(body)
                return self._send(200, data, "application/vnd.ms-package.3dmanufacturing-3dmodel+xml")
            if self.path == "/api/mapping":
                return self._send(200, self.app.save_mapping(body))
            if self.path == "/api/spool":
                return self._send(200, self.app.set_spool(body))
            if self.path == "/api/slot_read":
                return self._send(200, self.app.read_slot(body))
            if self.path == "/api/slot_forget":
                return self._send(200, self.app.forget_tag(body))
            self._send(404, {"error": "not found"})
        except TagError as e:
            self._send(409, {"error": str(e)})
        except (PrinterError, sample_model.SampleError) as e:
            self._send(502, {"error": str(e)})
        except ValueError:
            self._send(400, {"error": "Неверный запрос"})


PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QIDI Box</title>
<style>
:root{--bg:#f4f4f1;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#dcdcd5;--accent:#1f5fd0;--on-accent:#fff;--ok:#1c7c3f;--err:#b3261e;--flange:#d9d9d3;--flange-edge:#b4b4ac;--hub:#f4f4f1;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#21211f;--ink:#f1f1ec;--muted:#a3a39b;--line:#3a3a36;--accent:#7fa8f5;--on-accent:#10131a;--ok:#63c98a;--err:#f2897f;--flange:#3a3a37;--flange-edge:#55554f;--hub:#161615;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:1040px;margin:0 auto;padding:24px 16px 48px}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:22px;margin:0}
.tabs{display:flex;gap:6px}
.tab{font:inherit;font-weight:600;padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:transparent;color:var(--muted);cursor:pointer}
.tab[aria-selected=true]{background:var(--card);color:var(--ink);border-color:var(--accent)}
.hint{font-size:14px;color:var(--muted);margin:0 0 14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.slots{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:12px}
@media (max-width:760px){.slots{grid-template-columns:repeat(4,minmax(136px,1fr));overflow-x:auto;padding-bottom:6px}}
.slot{padding:12px;text-align:center;display:flex;flex-direction:column;gap:4px}
.slot[aria-current=true]{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.slot h2{font-size:16px;margin:0}
.slot .spool{width:min(112px,100%);margin:0 auto 4px}
.slot .name{font-size:14px;font-weight:600;min-height:38px;line-height:1.35}
.chip{display:inline-block;width:10px;height:10px;border-radius:50%;border:1px solid var(--flange-edge);vertical-align:-1px}
.slot .actions{flex-direction:column;gap:6px;margin-top:auto;padding-top:8px}
.slot .action{width:100%;padding:7px 8px;font-size:14px}
.compare{display:grid;grid-template-columns:minmax(180px,240px) auto 1fr;gap:18px;align-items:start}
@media (max-width:760px){.compare{grid-template-columns:1fr}.arrow{display:none}}
.side h3{font-size:13px;font-weight:600;color:var(--muted);margin:0 0 8px;text-transform:none}
.side .spool{width:120px;margin:0 auto 8px}
.real{text-align:center}
.real dl{display:grid;grid-template-columns:auto 1fr;gap:3px 10px;font-size:13px;text-align:left;margin:0}
.real dt{color:var(--muted)}.real dd{margin:0;word-break:break-all}
.arrow{align-self:center;font-size:24px;color:var(--muted)}
.edit-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.edit-head h2{font-size:17px;margin:0}
.spool-row{display:flex;gap:12px;align-items:center;margin-bottom:6px}
.spool-row .spool{width:64px;margin:0;flex:none}
.hex{font:13px ui-monospace,Menlo,monospace;color:var(--muted)}
label.field{display:block;font-size:13px;font-weight:600;margin:10px 0 5px}
select,input[type=text]{width:100%;padding:9px;font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:8px}
details{margin-top:10px}
summary{cursor:pointer;font-size:14px;font-weight:600;padding:3px 0}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
button.action{font:inherit;font-size:15px;font-weight:600;padding:9px 14px;border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:var(--on-accent);cursor:pointer}
button.alt{background:transparent;color:var(--accent)}
button.action:disabled{opacity:.45;cursor:not-allowed}
.note{font-size:13px;color:var(--muted);margin-top:9px}
.note:empty{display:none}
.note.err{color:var(--err)}.note.ok{color:var(--ok)}
.reader{display:flex;align-items:center;gap:10px;padding:11px 14px;margin-bottom:12px;font-weight:600}
.dot{flex:none;width:11px;height:11px;border-radius:50%;background:var(--err)}
.reader.ready .dot{background:var(--accent)}
.reader.tag .dot{background:var(--ok)}
.spool-left{display:block;width:100%;margin-top:6px;padding:6px 8px;font:inherit;font-size:13px;color:inherit;background:transparent;border:1px solid var(--line);border-radius:8px;cursor:pointer}
.spool-left:hover{border-color:var(--flange-edge)}
.spool-left.unset{color:var(--accent)}
.spool-left.low{border-color:var(--err);color:var(--err)}
.spool-bar{display:block;height:4px;margin-top:5px;border-radius:2px;background:var(--line);overflow:hidden}
.spool-bar span{display:block;height:100%;background:var(--ok)}
.spool-left.low .spool-bar span{background:var(--err)}
.spool-quick{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
.spool-quick button{flex:1;font:inherit;font-size:14px;color:inherit;background:transparent;border:1px solid var(--line);border-radius:8px;padding:7px 8px;cursor:pointer}
.spool-amount{display:flex;align-items:center;gap:8px;font-size:14px}
.spool-amount input{width:110px;padding:8px 9px;font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:8px}
.find-slots{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-bottom:12px}
.find-slots[hidden]{display:none}
@media (max-width:640px){.find-slots{grid-template-columns:1fr}}
.find-group{padding:10px;display:flex;flex-direction:column;gap:8px}
.find-group[aria-current=true]{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.find-pair{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.find-mini{display:flex;align-items:center;gap:8px;min-width:0}
.find-mini .spool{width:44px;flex:none;margin:0}
.find-mini div:last-child{min-width:0}
.find-mini b{display:block;font-size:14px}
.find-mini span{display:block;font-size:12px;line-height:1.3;overflow-wrap:anywhere}
.find-mini small{display:block;font-size:12px;color:var(--muted)}
.find-group .action{width:100%;padding:6px 8px;font-size:14px}
#find-dialog{width:min(520px,calc(100vw - 32px))}
#same-tag{width:min(540px,calc(100vw - 32px))}
.preview{position:relative}
.tag-sync{position:absolute;top:10px;right:10px;z-index:2;width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;border:1.5px solid var(--ok);color:var(--ok);background:var(--card);cursor:help}
.tag-sync[hidden],.tag-sync.hold{display:none}
.tag-sync.confirmed{background:var(--ok);color:var(--card)}
.tag-sync.changed{border-color:#d9962b;background:#d9962b;color:#1d1d1b}
.tag-sync::after{content:attr(data-tip);position:absolute;top:calc(100% + 6px);right:0;width:210px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font-size:13px;font-weight:400;line-height:1.35;text-align:left;opacity:0;visibility:hidden;transition:opacity .12s}
.tag-sync:hover::after,.tag-sync:focus-visible::after{opacity:1;visibility:visible}
.tag-toast{position:absolute;z-index:3;left:50%;top:42%;width:max-content;max-width:90%;padding:12px 16px;border-radius:10px;background:var(--ok);color:var(--card);font-size:14px;font-weight:600;line-height:1.35;transform:translate(-50%,-50%);animation:tag-toast 2.8s ease-in-out forwards}
.tag-toast[hidden]{display:none}
.tag-progress{position:absolute;z-index:3;left:50%;top:42%;display:flex;align-items:center;gap:10px;width:max-content;max-width:90%;padding:12px 16px;border:1px solid var(--accent);border-radius:10px;background:var(--card);font-size:13px;line-height:1.35;text-align:left;transform:translate(-50%,-50%)}
.tag-progress[hidden]{display:none}
.sample-progress{display:flex;align-items:center;gap:10px;padding:10px 12px;margin-top:8px;border:1px solid var(--accent);border-radius:10px;font-size:14px;font-weight:600}
.sample-progress[hidden]{display:none}
.tag-progress b{font-size:14px}
.tag-spinner{flex:none;width:20px;height:20px;border-radius:50%;border:3px solid var(--line);border-top-color:var(--accent);animation:tag-spin .8s linear infinite}
.preview.writing .tag-sync{display:none}
.preview.writing #tag-spool{animation:tag-spin 2.4s linear infinite}
@keyframes tag-spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.preview.writing #tag-spool{animation:none}.tag-spinner{animation-duration:2s}}
@keyframes tag-toast{0%{opacity:0;transform:translate(-50%,-50%) scale(.9)}8%,72%{opacity:1;left:50%;top:42%;transform:translate(-50%,-50%) scale(1)}100%{opacity:.4;left:calc(100% - 24px);top:24px;transform:translate(-50%,-50%) scale(.1)}}
@media (prefers-reduced-motion:reduce){.tag-toast{animation:none}}
.change-view{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);align-items:center;gap:10px;margin:4px 0 16px;text-align:center}
.change-side .spool{width:96px;max-width:100%;margin:0 auto 8px}
.change-side b{display:block;font-size:14px}
.change-side small{color:var(--muted);font-size:13px}
.change-arrow{color:var(--muted)}
#reader-reset{flex:none;padding:5px 10px;font-size:13px}
.find-status{display:flex;align-items:center;gap:12px;margin:12px 0 6px;font-weight:600}
.find-status .dot{width:16px;height:16px;background:var(--accent);animation:find-pulse 1.1s ease-in-out infinite}
.find-status.found .dot{background:var(--ok);box-shadow:0 0 0 5px color-mix(in srgb,var(--ok) 25%,transparent);animation:none}
.find-status.warn .dot,.find-status.none .dot{background:var(--err);animation:none}
@keyframes find-pulse{50%{opacity:.3}}
@media (prefers-reduced-motion:reduce){.find-status .dot{animation:none}}
.find-hint{font-size:14px;color:var(--muted);min-height:20px}
.find-bar{height:6px;border-radius:3px;background:var(--line);overflow:hidden;margin:8px 0 12px}
.find-bar span{display:block;height:100%;background:var(--accent)}
.find-list{display:flex;flex-direction:column;gap:8px;margin-bottom:12px}
.find-item{display:flex;align-items:center;gap:12px;padding:10px;border:1px solid var(--line);border-radius:10px;width:100%;font:inherit;color:inherit;background:none;text-align:left}
.find-item:not(:disabled){cursor:pointer}
.find-item:not(:disabled):hover{border-color:var(--flange-edge)}
.find-item[aria-pressed=true]{border-color:var(--ok);box-shadow:0 0 0 1px var(--ok)}
.find-item .spool{width:44px;flex:none;margin:0}
.find-item b{display:block;font-size:14px}
.find-item small{color:var(--muted);font-size:13px}
.reader small{font-weight:400;color:var(--muted);margin-left:auto;text-align:right}
.editor{display:grid;grid-template-columns:minmax(220px,300px) 1fr;gap:12px}
@media (max-width:720px){.editor{grid-template-columns:1fr}}
.preview{text-align:center}
.preview .spool{width:min(220px,100%);margin:4px auto 10px}
.preview h2{font-size:17px;margin:0}
.facts{font-size:13px;color:var(--muted);margin-top:5px;word-break:break-all}
.picker{margin-top:6px}
.presets{display:grid;grid-template-columns:repeat(auto-fill,minmax(32px,1fr));gap:6px}
.swatch{aspect-ratio:1;border:1px solid var(--line);border-radius:8px;cursor:pointer;padding:0}
.swatch[aria-pressed=true]{outline:3px solid var(--accent);outline-offset:1px}
.two-selects{display:grid;grid-template-columns:minmax(90px,1fr) 2fr;gap:8px}
.enabled{margin-bottom:14px}
.enabled h2{font-size:15px;margin:0 0 10px}
.enabled-row{display:flex;gap:10px;align-items:flex-start;padding:6px 0;border-top:1px solid var(--line)}
.enabled-row:first-of-type{border-top:0}
.enabled-type{flex:none;width:44px;font-size:13px;font-weight:600;padding-top:5px}
.enabled-items{display:flex;flex-wrap:wrap;gap:6px;min-width:0}
.pill{display:inline-flex;align-items:center;gap:6px;font:inherit;font-size:13px;padding:5px 10px;border-radius:999px;border:1px solid var(--line);background:transparent;color:var(--ink);cursor:pointer}
.pill:hover,.pill:focus-visible{border-color:var(--accent)}
.pill.on{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.pill .x{font-size:15px;line-height:1;opacity:.75}
.type-list{display:grid;gap:8px}
.type-card{padding:0;overflow:hidden}
.type-head{display:flex;align-items:center;gap:10px;padding:11px 14px}
.type-open{flex:1;min-width:0;display:flex;align-items:center;gap:10px;font:inherit;font-size:15px;font-weight:600;color:var(--ink);background:none;border:0;padding:0;cursor:pointer;text-align:left}
.type-open .count{font-size:13px;font-weight:400;color:var(--muted)}
.type-open .chev{margin-left:auto;color:var(--muted);transition:transform .15s}
.type-card.open .chev{transform:rotate(90deg)}
.switch{position:relative;flex:none;width:38px;height:22px;display:block;cursor:pointer}
.switch input{position:absolute;inset:0;width:100%;height:100%;margin:0;opacity:0;cursor:pointer}
.switch span{position:absolute;inset:0;border-radius:11px;background:var(--line);transition:background .15s;pointer-events:none}
.switch span::after{content:"";position:absolute;top:3px;left:3px;width:16px;height:16px;border-radius:50%;background:#fff;transition:left .15s}
.switch input:checked+span{background:var(--accent)}
.switch input:checked+span::after{left:19px}
.switch input:focus-visible+span{outline:2px solid var(--accent);outline-offset:2px}
.type-body{padding:0 14px 14px;border-top:1px solid var(--line)}
.brand-row{display:grid;grid-template-columns:110px 1fr;gap:10px;padding-top:12px}
@media (max-width:560px){.brand-row{grid-template-columns:1fr;gap:5px}}
.brand-name{font-size:13px;color:var(--muted);padding-top:5px}
.brand-items{display:flex;flex-wrap:wrap;gap:6px}
.bulk{display:flex;gap:6px;margin-top:12px}
.bulk button{font:inherit;font-size:13px;color:var(--accent);background:none;border:0;padding:3px 6px;cursor:pointer}
.palette-title{font-size:13px;color:var(--muted);margin:12px 0 6px}
.swap-text{font-size:12px;color:var(--muted);line-height:1.35;min-height:34px;margin-top:2px}
.slot{position:relative}
.slot.editable{cursor:pointer}
.slot.editable:hover{border-color:var(--flange-edge)}
.edit-btn{position:absolute;top:8px;right:8px;display:flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:8px;border:1px solid var(--accent);background:transparent;color:var(--accent);cursor:pointer;padding:0}
.edit-btn:hover,.edit-btn:focus-visible{background:var(--line)}
.map-empty{font-size:12px;color:var(--muted);min-height:34px;margin-top:2px}
.tile{position:relative}
.tile .swatch{width:100%;display:block}
.tile-tool{position:absolute;top:-6px;width:20px;height:20px;border-radius:50%;border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:12px;line-height:1;padding:0;cursor:pointer;opacity:0;display:flex;align-items:center;justify-content:center}
.tile-tool.remove{right:-6px}.tile-tool.change{left:-6px}
.tile:hover .tile-tool,.tile-tool:focus-visible{opacity:1}
@media (hover:none){.tile-tool{opacity:1}}
.add{display:flex;align-items:center;justify-content:center;aspect-ratio:1;width:100%;border:1px dashed var(--flange-edge);border-radius:8px;background:transparent;color:var(--muted);font-size:20px;cursor:pointer;padding:0}
.add:hover,.add:focus-visible{border-color:var(--accent);color:var(--accent)}
.selected{font-size:13px;color:var(--muted);margin-top:10px}
.sv{position:relative;height:170px;border-radius:9px;cursor:crosshair;touch-action:none;margin-bottom:12px;background-image:linear-gradient(to top,#000,transparent),linear-gradient(to right,#fff,transparent)}
.sv-dot{position:absolute;width:16px;height:16px;margin:-8px 0 0 -8px;border-radius:50%;border:2px solid #fff;box-shadow:0 0 0 1px #000;pointer-events:none}
.hue{width:100%;height:14px;margin:0 0 14px;-webkit-appearance:none;appearance:none;border-radius:7px;background:linear-gradient(to right,#f00,#ff0,#0f0,#0ff,#00f,#f0f,#f00);outline-offset:2px}
.hue::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:#fff;border:1px solid #000;cursor:pointer}
.codes{display:grid;grid-template-columns:1fr 1fr 1fr 1.6fr;gap:8px;margin-bottom:14px}
.codes label{font-size:12px;color:var(--muted);display:block}
.codes input{width:100%;margin-top:3px;padding:8px;font:14px ui-monospace,Menlo,monospace;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:8px}
.codes input.bad{border-color:var(--err)}
.compare-colors{display:flex;height:34px;border-radius:8px;overflow:hidden;border:1px solid var(--line);margin-bottom:16px}
.compare-colors div{flex:1}
dialog{border:1px solid var(--line);border-radius:14px;background:var(--card);color:var(--ink);padding:22px;width:min(420px,calc(100vw - 32px))}
dialog::backdrop{background:rgba(0,0,0,.55)}
dialog h2{font-size:18px;margin:0 0 8px}
dialog p{margin:0 0 18px;color:var(--muted);font-size:15px;white-space:pre-line}
dialog .actions{justify-content:flex-end;margin:0}
.real .brand{font-size:15px;font-weight:600;margin:0 0 8px}
.slots-head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.slots-head .hint{margin:0;flex:1;min-width:220px}
#sample-dialog{width:min(960px,calc(100vw - 32px))}
.sample-layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:18px;align-items:start}
@media (max-width:760px){.sample-layout{grid-template-columns:1fr}}
[hidden]{display:none!important}
.sample-side{display:grid;gap:10px;align-content:start}
.template-card{border:1px solid var(--line);border-radius:10px;padding:8px 10px;display:grid;grid-template-columns:72px 1fr;gap:6px 12px;align-items:center}
#sample-template-view svg{display:block;width:72px;height:auto}
.template-info{display:flex;align-items:center;justify-content:space-between;gap:10px;min-width:0}
.template-info b{display:block;font-size:14px}
.template-info small{color:var(--muted);font-size:12px}
.sequence-note{font-size:13px;color:var(--muted);padding:8px 10px;border:1px solid var(--line);border-radius:8px}
.sequence-note b{color:var(--ink);font-weight:600}
#sample-preview{aspect-ratio:1;width:min(100%,380px);margin:0 auto;border:1px solid var(--line);border-radius:9px;overflow:hidden}
#sample-preview svg{display:block;width:100%;height:100%}
.sample-status{min-height:48px}
#sample-preview-note{font-size:13px;color:var(--muted);margin-top:6px}
.sample-row{display:grid;grid-template-columns:16px 1fr auto;align-items:center;gap:10px;padding:10px 0;border-top:1px solid var(--line);font-size:14px}
.sample-row .chip{width:14px;height:14px}
.sample-name small{display:block;color:var(--muted);font-size:12px;margin-top:2px}
.sample-name .two-selects{margin-top:6px}
.sample-head{display:flex;align-items:center;gap:8px}
.sample-edit{flex:none;display:flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:7px;border:1px solid var(--line);background:transparent;color:var(--accent);cursor:pointer;padding:0}
.sample-edit:hover,.sample-edit:focus-visible{border-color:var(--accent)}
.new-mark{font-size:10px;font-weight:700;margin-left:4px;color:var(--ok)}
.new-note{font-size:13px;color:var(--ok);padding-top:8px}
#sample-note{margin:8px 0 14px}
.stepper{display:flex;align-items:center;gap:4px;flex:none}
.stepper button{width:32px;height:32px;font:inherit;font-size:18px;line-height:1;color:var(--accent);background:transparent;border:1px solid var(--line);border-radius:8px;cursor:pointer;padding:0}
.stepper button:disabled{opacity:.4;cursor:not-allowed}
.stepper output{width:26px;text-align:center;font-weight:600}
</style></head><body><main>
<header>
<h1>QIDI Box</h1>
<div class="tabs" role="tablist">
<button class="tab" role="tab" id="tab-slots" aria-selected="true">Слоты принтера</button>
<button class="tab" role="tab" id="tab-tags" aria-selected="false">NFC-метки</button>
<button class="tab" role="tab" id="tab-materials" aria-selected="false">Материалы</button>
<button class="tab" role="tab" id="tab-guide" aria-selected="false">Справка</button>
</div>
</header>

<div id="panel-slots">
<div class="slots-head"><p class="hint" id="slots-hint">Загружаю слоты…</p><button class="action alt" id="open-samples" type="button">Напечатать образцы</button></div>
<div class="slots" id="slots"></div>
<div class="card" id="editor" hidden></div>
</div>

<div id="panel-tags" hidden>
<div class="find-slots" id="find-slots" hidden></div>
<div class="card reader" id="reader"><span class="dot"></span><span id="reader-text">Проверяю ридер…</span><small id="reader-detail"></small><button class="action alt" id="reader-reset" hidden>Убрать метку</button></div>
<div class="editor">
<div class="card preview" id="tag-card">
<div class="tag-progress" id="tag-progress" role="status" hidden><span class="tag-spinner"></span><span><b>Идёт запись и проверка</b><br>Не убирай метку от антенны</span></div>
<div class="tag-sync" id="tag-sync" role="img" tabindex="0" hidden></div>
<div class="tag-toast" id="tag-toast" role="status" hidden>Сохранено на метке<br>и подтверждено чтением</div>
<div class="spool" id="tag-spool"></div>
<h2 id="tag-title">Метка не прочитана</h2>
<div class="facts" id="tag-facts">Здесь будет то, что прочитано с метки.</div>
<div class="actions" style="justify-content:center"><button class="action alt" id="read">Прочитать метку</button></div>
</div>
<div class="card">
<label class="field">Материал</label><div id="material"></div>
<label class="field">Цвет</label>
<div id="tag-picker"></div>
<div class="actions"><button class="action" id="write">Записать на метку</button></div>
<div class="note" id="msg" role="status"></div>
</div>
</div>
</div>
<div id="panel-guide" hidden><iframe id="guide-frame" title="Справка по пластику" style="width:100%;height:80vh;border:0;border-radius:12px"></iframe></div>
<div id="panel-materials" hidden>
<div class="card enabled" id="enabled"></div>
<div class="type-list" id="type-list"></div>
</div>
</main>
<dialog id="sample-dialog"><h2>Напечатать образцы</h2>
<p>Укажи, сколько образцов напечатать каждой катушкой. Карандаш меняет надпись на образце.</p>
<div class="sample-layout"><div id="sample-rows"></div>
<div class="sample-side">
<div class="template-card"><div id="sample-template-view"></div>
<div class="template-info"><div><b id="sample-template"></b><small>Шаблон образца, надпись выпуклая</small></div></div>
</div>
<div id="sample-preview"></div><div id="sample-preview-note"></div>
<div class="sequence-note">Катушки печатаются группами по очереди, образцы одного цвета стоят рядом. В QIDI Studio проверь: <b>Последовательность печати моделей → По очереди</b>.</div>
</div></div>
<div class="sample-status"><div class="sample-progress" id="sample-progress" role="status" hidden><span class="tag-spinner"></span><span id="sample-progress-text"></span></div>
<div class="note" id="sample-note"></div></div>
<div class="actions"><button class="action alt" id="sample-close" type="button">Закрыть</button><button class="action alt" id="sample-download" type="button">Скачать 3MF</button><button class="action" id="sample-open" type="button">Открыть в QIDI Studio</button></div>
</dialog>
<dialog id="spool-dialog"><h2 id="spool-title"></h2>
<p id="spool-text">Задай примерно, на глаз. Дальше панель будет вычитать то, что уходит при печати.</p>
<div class="spool-quick" id="spool-quick"></div>
<label class="spool-amount">Остаток<input id="spool-grams" type="number" min="0" max="5000" step="10"> г</label>
<div class="note" id="spool-note"></div>
<div class="actions" style="justify-content:flex-end"><button class="action alt" id="spool-cancel" type="button">Отмена</button><button class="action" id="spool-save" type="button">Сохранить</button></div></dialog>
<dialog id="find-dialog"><h2 id="find-title"></h2>
<div class="find-status" id="find-status" role="status"><span class="dot"></span><span id="find-text"></span></div>
<div class="find-hint" id="find-hint"></div>
<div class="find-bar" id="find-bar"><span id="find-progress"></span></div>
<div class="find-list" id="find-list"></div>
<div class="actions" style="justify-content:flex-end"><button class="action alt" id="find-close">Отмена</button><button class="action alt" id="find-again" hidden>Искать дальше</button><button class="action" id="find-take" hidden>Выбрать эту метку</button></div>
</dialog>
<dialog id="same-tag"><h2 id="same-tag-title"></h2><p id="same-tag-text"></p>
<div class="actions" style="flex-wrap:wrap"><button class="action alt" id="same-tag-no" type="button">Отмена</button><button class="action alt" id="same-tag-next" type="button"></button><button class="action" id="same-tag-here" type="button"></button></div></dialog>
<dialog id="confirm"><h2 id="confirm-title"></h2><p id="confirm-text"></p><div id="confirm-body"></div>
<div class="actions"><button class="action alt" id="confirm-no" type="button">Отмена</button><button class="action" id="confirm-yes" type="button"></button></div></dialog>
<dialog id="color-editor"><h2 id="ce-title"></h2>
<div class="sv" id="ce-sv"><div class="sv-dot" id="ce-dot"></div></div>
<input class="hue" id="ce-hue" type="range" min="0" max="360" aria-label="Тон">
<div class="codes">
<label>R<input id="ce-r" type="number" min="0" max="255"></label>
<label>G<input id="ce-g" type="number" min="0" max="255"></label>
<label>B<input id="ce-b" type="number" min="0" max="255"></label>
<label>HEX<input id="ce-hex" type="text" maxlength="7" spellcheck="false"></label>
</div>
<div class="compare-colors" title="Было и стало"><div id="ce-old"></div><div id="ce-new"></div></div>
<div class="actions"><button class="action alt" id="ce-no" type="button">Отмена</button><button class="action" id="ce-yes" type="button">Сохранить</button></div>
</dialog>
<script>
const $=id=>document.getElementById(id);
const state={vendor:1,tail:"00".repeat(13),sourceUid:null,codes:null,uid:null,reader:false,classic:false,dirty:{}};
const api=async(path,body)=>{
  const res=await fetch(path,body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:undefined);
  const data=await res.json();
  if(!res.ok)throw new Error(data.error||"Ошибка");
  return data;
};
function confirmDialog(title,text,yes,body){
  const d=$("confirm");
  $("confirm-title").textContent=title;$("confirm-text").textContent=text;$("confirm-yes").textContent=yes;
  $("confirm-body").replaceChildren(...(body?[body]:[]));
  return new Promise(resolve=>{
    const done=v=>{$("confirm-yes").onclick=$("confirm-no").onclick=d.oncancel=null;d.close();resolve(v)};
    $("confirm-yes").onclick=()=>done(true);
    $("confirm-no").onclick=()=>done(false);
    d.oncancel=e=>{e.preventDefault();done(false)};
    d.showModal();$("confirm-no").focus();
  });
}
const isHex=h=>/^[0-9A-F]{6}$/.test(h||"");
function spool(hex){
  const c=isHex(hex)?"#"+hex:"none";
  const rings=isHex(hex)?[34,39,44].map(r=>`<circle cx="60" cy="60" r="${r}" fill="none" stroke="#000" stroke-opacity=".13"/>`).join(""):`<circle cx="60" cy="60" r="38" fill="none" stroke="var(--flange-edge)" stroke-dasharray="4 5"/>`;
  return `<svg viewBox="0 0 120 120" role="img" aria-label="Катушка"><circle cx="60" cy="60" r="58" fill="var(--flange)" stroke="var(--flange-edge)" stroke-width="1.5"/><circle cx="60" cy="60" r="48" fill="${c}" stroke="#000" stroke-opacity=".25"/>${rings}<circle cx="60" cy="60" r="24" fill="var(--flange)" stroke="var(--flange-edge)" stroke-width="1.5"/><circle cx="60" cy="60" r="9" fill="var(--hub)" stroke="var(--flange-edge)" stroke-width="1.5"/></svg>`;
}
function hexToRgb(hex){return [0,2,4].map(i=>parseInt(hex.slice(i,i+2),16))}
function rgbToHex(rgb){return rgb.map(v=>v.toString(16).padStart(2,"0")).join("").toUpperCase()}
function rgbToHsv([r,g,b]){
  r/=255;g/=255;b/=255;
  const max=Math.max(r,g,b),d=max-Math.min(r,g,b);
  const h=!d?0:max===r?((g-b)/d+6)%6:max===g?(b-r)/d+2:(r-g)/d+4;
  return [h*60,max?d/max:0,max];
}
function hsvToRgb(h,s,v){
  const f=n=>{const k=(n+h/60)%6;return Math.round(255*(v-v*s*Math.max(0,Math.min(k,4-k,1))))};
  return [f(5),f(3),f(1)];
}
function parseHex(text){
  let h=text.trim().replace(/^#/,"").toUpperCase();
  if(/^[0-9A-F]{3}$/.test(h))h=[...h].map(c=>c+c).join("");
  return isHex(h)?h:null;
}
// Editor dialog: a color field and hue, or typed RGB and HEX. Resolves with a code or null.
function colorEditor(title,start){
  const d=$("color-editor");
  const inputs=[$("ce-r"),$("ce-g"),$("ce-b")];
  let [h,s,v]=rgbToHsv(hexToRgb(start));
  function paint(source){
    const rgb=hsvToRgb(h,s,v),hex=rgbToHex(rgb);
    $("ce-sv").style.backgroundColor=`hsl(${h} 100% 50%)`;
    $("ce-dot").style.left=s*100+"%";$("ce-dot").style.top=(1-v)*100+"%";
    if(source!=="hue")$("ce-hue").value=h;
    if(source!=="rgb")inputs.forEach((i,n)=>i.value=rgb[n]);
    if(source!=="hex")$("ce-hex").value="#"+hex;
    $("ce-hex").classList.remove("bad");
    $("ce-new").style.background="#"+hex;
    return hex;
  }
  function fromRgb(rgb,source){const hsv=rgbToHsv(rgb);if(hsv[1]>0)h=hsv[0];s=hsv[1];v=hsv[2];paint(source)}
  const field=$("ce-sv");
  function drag(e){
    const r=field.getBoundingClientRect();
    s=Math.min(1,Math.max(0,(e.clientX-r.left)/r.width));
    v=1-Math.min(1,Math.max(0,(e.clientY-r.top)/r.height));
    paint();
  }
  field.onpointerdown=e=>{field.setPointerCapture(e.pointerId);drag(e);field.onpointermove=drag};
  field.onpointerup=()=>{field.onpointermove=null};
  $("ce-hue").oninput=()=>{h=+$("ce-hue").value;paint("hue")};
  inputs.forEach(i=>i.oninput=()=>{
    if(inputs.some(x=>x.value===""))return;
    fromRgb(inputs.map(x=>Math.min(255,Math.max(0,Math.round(+x.value)||0))),"rgb");
  });
  $("ce-hex").oninput=()=>{
    const hex=parseHex($("ce-hex").value);
    $("ce-hex").classList.toggle("bad",!hex);
    if(hex)fromRgb(hexToRgb(hex),"hex");
  };
  $("ce-title").textContent=title;
  $("ce-old").style.background="#"+start;
  paint();
  return new Promise(resolve=>{
    const done=value=>{$("ce-yes").onclick=$("ce-no").onclick=d.oncancel=null;d.close();resolve(value)};
    $("ce-yes").onclick=()=>done(rgbToHex(hsvToRgb(h,s,v)));
    $("ce-no").onclick=()=>done(null);
    d.oncancel=e=>{e.preventDefault();done(null)};
    d.showModal();
  });
}
// Standard colors plus the user's own palette, shared by both tabs.
const pickers=[];
function makePicker(host,initial,onchange){
  host.classList.add("picker");
  const presets=document.createElement("div");presets.className="presets";
  const mineTitle=document.createElement("div");mineTitle.className="palette-title";mineTitle.textContent="Мои цвета";
  const mine=document.createElement("div");mine.className="presets";
  const selected=document.createElement("div");selected.className="selected";
  host.append(presets,mineTitle,mine,selected);
  let value=null;
  const swatch=(hex,title)=>{
    const b=document.createElement("button");b.type="button";b.className="swatch";b.style.background="#"+hex;
    b.dataset.hex=hex;b.title=title;b.setAttribute("aria-label",title);b.onclick=()=>set(hex,true);return b;
  };
  state.codes.colors.forEach(c=>presets.append(swatch(c.hex.slice(1),c.name)));
  function mark(){host.querySelectorAll(".swatch").forEach(s=>s.setAttribute("aria-pressed",String(s.dataset.hex===value)))}
  function set(hex,user){value=hex;selected.textContent="Выбран #"+hex;mark();if(user)onchange(hex)}
  async function changeColors(body,select){
    try{
      state.codes.my_colors=(await api("/api/my_colors",body)).my_colors;
      pickers.forEach(p=>p.renderMine());
      if(select)set(select,true);
    }catch(e){selected.textContent=e.message}
  }
  async function addColor(){
    const hex=await colorEditor("Новый цвет",value||"808080");
    if(hex)changeColors({add:hex},hex);
  }
  async function changeColor(old){
    const hex=await colorEditor("Изменить цвет",old);
    if(hex&&hex!==old)changeColors({add:hex,remove:old},hex);
  }
  function renderMine(){
    mine.innerHTML="";
    state.codes.my_colors.forEach(hex=>{
      const tile=document.createElement("div");tile.className="tile";
      const edit=document.createElement("button");edit.type="button";edit.className="tile-tool change";edit.textContent="✎";
      edit.title="Изменить цвет";edit.setAttribute("aria-label","Изменить #"+hex);edit.onclick=()=>changeColor(hex);
      const x=document.createElement("button");x.type="button";x.className="tile-tool remove";x.textContent="×";
      x.title="Удалить из палитры";x.setAttribute("aria-label","Удалить #"+hex+" из палитры");
      x.onclick=()=>changeColors({remove:hex});
      tile.append(swatch(hex,"#"+hex),edit,x);mine.append(tile);
    });
    const add=document.createElement("button");add.type="button";add.className="add";add.textContent="+";
    add.title="Добавить свой цвет";add.setAttribute("aria-label","Добавить свой цвет");add.onclick=addColor;
    mine.append(add);mark();
  }
  const self={set:hex=>set(hex,false),get:()=>value,renderMine};
  pickers.push(self);
  set(isHex(initial)?initial:"FAFAFA",false);renderMine();
  return self;
}

/* Materials */
const MATERIAL_GROUPS=[[/^Support/,"Поддержка"],[/^WOOD/,"PLA"],[/^PETG/,"PETG"],[/^PET/,"PET"],[/^PLA/,"PLA"],[/^(ABS)/,"ABS"],[/^ASA/,"ASA"],[/^(UltraPA|PA)/,"PA"],[/^PC/,"PC"],[/^(TPU|TPEE|PEBA)/,"TPU"],[/^PPS/,"PPS"],[/^PVA/,"PVA"]];
function groupOf(name){
  const bare=name.replace(/^(QIDI|Generic|HATCHBOX|PolyLite|Polymaker|Bambu|Overture)\s+/,"");
  const hit=MATERIAL_GROUPS.find(([re])=>re.test(bare));
  return hit?hit[1]:"Другое";
}
// Two linked selects: plastic type first, then the exact material of that type.
function makeMaterialField(host,items,current,onchange){
  host.innerHTML="";host.className="two-selects";
  const typeSel=document.createElement("select"),itemSel=document.createElement("select");
  typeSel.setAttribute("aria-label","Тип пластика");itemSel.setAttribute("aria-label","Материал");
  host.append(typeSel,itemSel);
  let list=items.slice();
  function fillItems(type,value){
    itemSel.innerHTML="";
    list.filter(m=>groupOf(m.name)===type).forEach(m=>itemSel.add(new Option(m.name,m.id)));
    if(value!==undefined)itemSel.value=value;
  }
  function fillTypes(){
    typeSel.innerHTML="";
    [...new Set(list.map(m=>groupOf(m.name)))].sort().forEach(g=>typeSel.add(new Option(g,g)));
  }
  function set(value,name){
    if(value&&!list.some(m=>m.id===value))list.push({id:value,name:name||String(value)});
    fillTypes();
    const item=list.find(m=>m.id===value)||list[0];
    if(!item)return;
    typeSel.value=groupOf(item.name);fillItems(typeSel.value,item.id);
  }
  typeSel.onchange=()=>{fillItems(typeSel.value);onchange&&onchange()};
  itemSel.onchange=()=>{onchange&&onchange()};
  set(current);
  return {get:()=>itemSel.value,set,name:()=>itemSel.selectedOptions[0]?itemSel.selectedOptions[0].text:""};
}
const BRAND=/^(QIDI|Generic|HATCHBOX|PolyLite|Polymaker|Bambu|Overture)\s+/;
const brandOf=name=>{const m=name.match(BRAND);if(!m)return "Другие";return m[1]==="Generic"?"Универсальные":m[1]};
function shortName(name,group){
  const bare=name.replace(BRAND,"");
  return bare.startsWith(group+" ")?bare.slice(group.length+1):bare;
}
function activeMaterials(){
  const my=state.codes.my_materials;
  return state.codes.slot_materials.filter(m=>my.ids.includes(m.id)&&my.types.includes(groupOf(m.name)));
}
// Only the chosen materials, plus the current one so it never disappears; all when nothing is chosen.
function slotMaterialItems(current){
  const active=activeMaterials();
  if(!active.length)return state.codes.slot_materials.slice();
  const extra=state.codes.slot_materials.filter(m=>m.id===current&&!active.includes(m));
  return active.concat(extra);
}
async function changeMaterials(...bodies){
  try{
    for(const body of bodies)state.codes.my_materials=(await api("/api/my_materials",body)).my_materials;
  }catch(e){$("enabled").textContent=e.message;return}
  renderMaterials();
}
const openTypes=new Set();
function renderEnabled(groups){
  const box=$("enabled"),active=activeMaterials();box.innerHTML="";
  const h=document.createElement("h2");h.textContent=active.length?`Включено: ${active.length}`:"Ничего не включено, в списках показаны все материалы";
  box.append(h);
  groups.forEach(g=>{
    const items=active.filter(m=>groupOf(m.name)===g);
    if(!items.length)return;
    const row=document.createElement("div");row.className="enabled-row";
    const type=document.createElement("div");type.className="enabled-type";type.textContent=g;
    const list=document.createElement("div");list.className="enabled-items";
    items.forEach(m=>{
      const b=document.createElement("button");b.type="button";b.className="pill";b.title="Выключить";
      b.setAttribute("aria-label",`Выключить ${m.name}`);
      const x=document.createElement("span");x.className="x";x.textContent="×";
      b.append(document.createTextNode(m.name),x);
      b.onclick=()=>changeMaterials({remove:m.id});list.append(b);
    });
    row.append(type,list);box.append(row);
  });
}
function renderMaterials(){
  const my=state.codes.my_materials,all=state.codes.slot_materials;
  const groups=[...new Set(all.map(m=>groupOf(m.name)))].sort();
  renderEnabled(groups);
  const fresh=all.filter(m=>m.new);
  if(fresh.length){
    const n=document.createElement("div");n.className="new-note";
    n.textContent=`Новые материалы после обновления принтера: ${fresh.map(m=>m.name).join(", ")}`;
    $("enabled").append(n);
  }
  const box=$("type-list");box.innerHTML="";
  groups.forEach(g=>{
    const items=all.filter(m=>groupOf(m.name)===g),on=my.types.includes(g);
    const chosen=items.filter(m=>my.ids.includes(m.id)).length;
    const isOpen=openTypes.has(g);
    const card=document.createElement("div");card.className="card type-card"+(isOpen?" open":"");
    const head=document.createElement("div");head.className="type-head";
    const sw=document.createElement("label");sw.className="switch";sw.title=on?"Выключить тип":"Включить тип";
    const check=document.createElement("input");check.type="checkbox";check.checked=on;check.setAttribute("aria-label",`Тип ${g}`);
    check.onchange=()=>{if(check.checked)openTypes.add(g);changeMaterials(check.checked?{type_on:g}:{type_off:g})};
    sw.append(check,document.createElement("span"));
    const open=document.createElement("button");open.type="button";open.className="type-open";open.setAttribute("aria-expanded",String(isOpen));
    const title=document.createElement("span");title.textContent=g;
    const count=document.createElement("span");count.className="count";count.textContent=`${on?chosen:0} из ${items.length}`;
    const chev=document.createElement("span");chev.className="chev";chev.textContent="›";
    open.append(title,count,chev);
    open.onclick=()=>{isOpen?openTypes.delete(g):openTypes.add(g);renderMaterials()};
    head.append(sw,open);card.append(head);
    if(isOpen){
      const body=document.createElement("div");body.className="type-body";
      [...new Set(items.map(m=>brandOf(m.name)))].forEach(brand=>{
        const row=document.createElement("div");row.className="brand-row";
        const name=document.createElement("div");name.className="brand-name";name.textContent=brand;
        const pills=document.createElement("div");pills.className="brand-items";
        items.filter(m=>brandOf(m.name)===brand).forEach(m=>{
          const selected=my.ids.includes(m.id);
          const b=document.createElement("button");b.type="button";b.className="pill"+(selected?" on":"");
          b.setAttribute("aria-pressed",String(selected));b.textContent=shortName(m.name,g);
          if(m.new){const mark=document.createElement("span");mark.className="new-mark";mark.textContent="новый";b.append(mark)}
          // Choosing a material in a switched-off type also switches the type on.
          b.onclick=()=>selected?changeMaterials({remove:m.id}):changeMaterials(...(on?[]:[{type_on:g}]),{add:m.id});
          pills.append(b);
        });
        row.append(name,pills);body.append(row);
      });
      const bulk=document.createElement("div");bulk.className="bulk";
      const allOn=document.createElement("button");allOn.type="button";allOn.textContent="Включить все";
      allOn.onclick=()=>changeMaterials(...(on?[]:[{type_on:g}]),...items.filter(m=>!my.ids.includes(m.id)).map(m=>({add:m.id})));
      const allOff=document.createElement("button");allOff.type="button";allOff.textContent="Выключить все";
      allOff.onclick=()=>changeMaterials(...items.filter(m=>my.ids.includes(m.id)).map(m=>({remove:m.id})));
      bulk.append(allOn,allOff);body.append(bulk);card.append(body);
    }
    box.append(card);
  });
}

/* NFC tab */
let tagPicker,tagMaterial;
// The left card shows only what was read from the tag; the form is what would be written.
let onTag=null;
function formTag(){return {id:tagMaterial?tagMaterial.get():null,hex:tagPicker.get()}}
function formDiffersFromTag(){
  const form=formTag();
  return !onTag||!!onTag.blank||form.id!==onTag.id||form.hex!==onTag.hex;
}
function renderTag(){
  $("tag-spool").innerHTML=spool(onTag&&!onTag.blank?onTag.hex:null);
  const box=$("tag-sync");
  box.hidden=!onTag;
  syncButtons();
  if(!onTag)return;
  const changed=formDiffersFromTag();
  const hold=box.classList.contains("hold");
  box.className="tag-sync"+(changed?" changed":onTag.confirmed?" confirmed":"")+(hold?" hold":"");
  box.innerHTML=changed?ICON_CHANGED:ICON_CHECK;
  const tip=(onTag.blank?"Чистая метка, ещё не записана":changed?"Изменено, на метку не записано"
    :onTag.confirmed?"Сохранено на метке и подтверждено чтением":"Форма совпадает с меткой")+(onTag.details?`. ${onTag.details}`:"");
  box.dataset.tip=tip;box.setAttribute("aria-label",tip);
}
function spoolColumn(hex,name,detail){
  const col=el("div","change-side");
  const pic=el("div","spool");pic.innerHTML=spool(hex);
  col.append(pic,el("b","",name),el("small","",detail));
  return col;
}
function writeChangeView(){
  const view=el("div","change-view"),arrow=el("div","change-arrow");
  arrow.innerHTML='<svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 12h16"/><path d="m14 6 6 6-6 6"/></svg>';
  const old=onTag&&!onTag.blank;
  view.append(old?spoolColumn(onTag.hex,$("tag-title").textContent,"#"+onTag.hex):spoolColumn(null,onTag?"Чистая метка":"Не прочитана",""),
    arrow,spoolColumn(tagPicker.get(),tagMaterial.name(),"#"+tagPicker.get()));
  return view;
}
const ICON_CHECK='<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 13 4 4L19 7"/></svg>';
const ICON_CHANGED='<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" aria-hidden="true"><path d="M12 5v9"/><path d="M12 19h.01"/></svg>';
// The whole tab waits: the tag must stay at the antenna until the read-back is done.
function setWriting(on){
  state.writing=on;
  $("panel-tags").inert=on;
  $("tag-card").classList.toggle("writing",on);
  $("tag-progress").hidden=!on;
  syncButtons();
}
function showSavedToast(){
  const toast=$("tag-toast"),icon=$("tag-sync");
  toast.hidden=false;icon.classList.add("hold");
  setTimeout(()=>{toast.hidden=true;icon.classList.remove("hold")},2800);
}
function clearOnTag(){
  onTag=null;
  $("tag-title").textContent="Метка не прочитана";
  $("tag-facts").textContent="Здесь будет то, что прочитано с метки.";
  renderTag();
}
function say(text,cls){const m=$("msg");m.textContent=text;m.className="note"+(cls?" "+cls:"")}
// Old tag code for each type: a Box without our module still sees the right plastic.
const LEGACY_BY_GROUP={PLA:7,ABS:11,ASA:18,PA:24,PC:23,PET:37,PETG:39,PPS:44,TPU:50,PVA:47,"Поддержка":33};
function buildTagMaterial(current,currentName){
  const items=slotMaterialItems(current);
  if(current&&!items.some(m=>m.id===current))items.push({id:current,name:currentName||current});
  tagMaterial=makeMaterialField($("material"),items,current||(items[0]&&items[0].id),renderTag);
}
// A tag from before profiles holds only an old code, kept as it is unless changed.
function legacyId(code){return "L"+code}
function legacyName(code){const m=state.codes.materials.find(x=>x.code===code);return m?m.name:"Неизвестный материал"}
const usesBox=()=>!!state.source&&state.source!=="usb";
const onPrinter=()=>!!state.codes&&state.codes.on_printer;
function clearChosenTag(){
  state.source=onPrinter()?null:"usb";state.chosenSlot=null;state.uid=null;state.classic=false;state.sourceUid=null;
  clearOnTag();renderFindSlots();poll();
}
$("reader-reset").onclick=clearChosenTag;
function syncButtons(){
  $("read").disabled=!state.uid;
  $("read").hidden=onPrinter()||usesBox();
  $("reader-reset").hidden=!usesBox();
  const same=!formDiffersFromTag();
  $("write").disabled=!state.uid||!state.classic||state.writing||same;
  $("write").title=same?"На метке уже это, менять нечего":"";
  $("write").textContent=state.writing?"Идёт запись…":"Записать на метку";
}
async function poll(){
  if(state.writing)return;
  const box=$("reader");
  try{
    if(usesBox()){
      box.className="card reader tag";
      $("reader-text").textContent=`Метка выбрана, слоты ${state.chosenSlot}`;
      $("reader-detail").textContent=`UID ${state.uid}. Держи её у антенны до конца записи`;
      syncButtons();
      return;
    }
    if(onPrinter()){
      box.className="card reader ready";
      $("reader-text").textContent="Метка не выбрана";
      $("reader-detail").textContent="Нажми «Найти метку» у нужного слота";
      syncButtons();
      return;
    }
    const s=await api("/api/status");
    state.uid=s.uid;state.reader=!!s.reader;state.classic=!!s.classic;
    if(onTag&&onTag.uid!==s.uid)clearOnTag();
    box.className="card reader"+(s.uid?" tag":s.reader?" ready":"");
    $("reader-text").textContent=!s.reader?"Ридер не подключён":s.uid?(s.classic?"Метка на ридере":"Эта метка не подойдёт: нужна MIFARE Classic 1K"):"Ридер подключён, метки нет";
    $("reader-detail").textContent=!s.reader?"Подключи ACR122U к USB этого компьютера":s.uid?`UID ${s.uid}`:s.reader;
  }catch{box.className="card reader";$("reader-text").textContent="Нет связи с сервером панели";$("reader-detail").textContent=""}
  syncButtons();
}
$("read").onclick=async()=>{
  try{applyTag(await api("/api/read",{source:state.source}))}catch(e){say(e.message,"err")}
};
function applyTag(r,confirmed){
  const b=r.block;
  if(/^0+$/.test(b)){
    state.sourceUid=null;
    onTag={uid:r.uid,blank:true};
    $("tag-title").textContent="Чистая метка";
    $("tag-facts").textContent=`UID ${r.uid}`;
    renderTag();
    say("");
    return;
  }
  const code=parseInt(b.slice(0,2),16);
  if(r.profile)buildTagMaterial(r.profile,materialName(r.profile));
  else buildTagMaterial(legacyId(code),legacyName(code));
  const hex=tagHex(r);
  if(hex)tagPicker.set(hex);
  onTag={uid:r.uid,id:r.profile||legacyId(code),hex,confirmed:!!confirmed,
         details:`Ключ ${r.key==="qidi"?"заводской QIDI":"стандартный"}${r.color?", точный цвет записан":""}`};
  renderTag();
  state.vendor=parseInt(b.slice(4,6),16);state.tail=b.slice(6);state.sourceUid=r.uid;
  $("tag-title").textContent=tagMaterial.name();
  $("tag-facts").textContent=`UID ${r.uid}`;
  say("");
}
function tagHex(r){
  const palette=state.codes.colors.find(c=>c.code===parseInt(r.block.slice(2,4),16));
  return r.color||(palette?palette.hex.slice(1):null);
}

/* Tag search on a Box reader */
const SEARCH_SECONDS=30;
const find={run:0,slot:null,source:null,items:[],picked:null};
function renderFindSlots(){
  const host=$("find-slots");
  host.hidden=!state.codes.box_readers.length||!slotData.length;
  // One reader serves two slots, so a search belongs to the pair.
  const groups=[slotData.slice(0,2),slotData.slice(2,4)];
  host.replaceChildren(...groups.map(pair=>{
    const group=el("div","card find-group");
    const title=pair.map(s=>s.label).join(" и ");
    group.setAttribute("aria-current",String(state.chosenSlot===title));
    const row=el("div","find-pair");
    pair.forEach(s=>{
      const tag=s.tag&&s.tag.detail?s.tag:null;
      const mini=el("div","find-mini");
      const pic=el("div","spool");pic.innerHTML=spool(tag?tag.tag_color:s.color);
      const text=el("div");
      text.append(el("b","",s.label),el("span","",tag?`${tag.brand||""} ${tag.detail}`.trim():materialName(s.filament)),
        el("small","",s.tag&&s.tag.stock?"Метка QIDI":tag?"#"+tag.tag_color:s.color?"#"+s.color:"Цвет не задан"));
      mini.append(pic,text);row.append(mini);
    });
    const button=el("button","action alt","Найти метку");
    button.onclick=()=>startFind(title,pair[0].index<2?"box1":"box2");
    group.append(row,button);
    return group;
  }));
}
async function loadFindSlots(){
  try{slotData=(await api("/api/slots")).slots;renderFindSlots()}catch{}
}
function findItemFromTag(r){
  const blank=/^0+$/.test(r.block);
  const name=blank?"Чистая метка":r.profile?materialName(r.profile):legacyName(parseInt(r.block.slice(0,2),16));
  return {uid:r.uid,name,hex:blank?null:tagHex(r),tag:r,facts:blank?`UID ${r.uid}, можно записывать`:`UID ${r.uid}, ключ ${r.key==="qidi"?"заводской QIDI":"стандартный"}`};
}
function renderFindList(){
  $("find-list").replaceChildren(...find.items.map(item=>{
    const row=el("button","find-item");row.type="button";
    row.disabled=!item.tag;
    row.setAttribute("aria-pressed",String(find.picked===item));
    const pic=el("div","spool");pic.innerHTML=spool(item.hex);
    const text=el("div");text.append(el("b","",item.name),el("small","",item.facts));
    row.append(pic,text);
    row.onclick=()=>{find.picked=item;showFindResult()};
    return row;
  }));
}
function setFindState(cls,text,hint){
  $("find-status").className="find-status"+(cls?" "+cls:"");
  $("find-text").textContent=text;
  $("find-hint").textContent=hint||"";
}
function setFindButtons(searching){
  const closing=$("find-close");
  closing.textContent=searching?"Отмена":"Закрыть";
  $("find-bar").hidden=!searching;
  $("find-again").hidden=searching;
  $("find-again").textContent=find.items.length?"Искать дальше":"Повторить";
  $("find-again").className="action"+(find.picked?" alt":"");
  $("find-take").hidden=searching||!find.picked;
}
function showFindResult(){
  const latest=find.items[find.items.length-1];
  if(find.picked)setFindState("found",`Метка найдена: ${find.picked.name}`,"Это она? Если нет, покрути катушку или поднеси другую метку и ищи дальше.");
  else setFindState("warn",latest.name,latest.facts);
  renderFindList();setFindButtons(false);
}
async function searchTag(){
  const run=++find.run;
  find.picked=null;
  const started=Date.now();
  renderFindList();setFindButtons(true);
  setFindState("","Ищу метку…",find.items.length?"Покрути катушку или поднеси другую метку. Уже найденные пропускаю.":"Поднеси метку к антенне слота или покрути катушку.");
  const tick=setInterval(()=>{
    const left=Math.max(0,SEARCH_SECONDS-(Date.now()-started)/1000);
    $("find-progress").style.width=(left/SEARCH_SECONDS*100)+"%";
    $("find-text").textContent=`Ищу метку… ${Math.ceil(left)} с`;
  },200);
  $("find-progress").style.width="100%";
  try{
    while(Date.now()-started<SEARCH_SECONDS*1000){
      const r=await api("/api/find",{source:find.source,exclude:find.items.map(i=>i.uid)});
      if(run!==find.run)return;
      if(r.status==="found"){
        const item=findItemFromTag(r.tag);
        find.items.push(item);find.picked=item;
        showFindResult();
        return;
      }
      if(r.status==="unusable"){
        find.items.push({uid:r.uid,name:r.code==="bambu"?"Метка Bambu":"Метка не подходит",hex:null,tag:null,facts:`UID ${r.uid}. ${r.message}`});
        showFindResult();
        return;
      }
      if(r.code==="several")$("find-hint").textContent=r.message;
    }
    setFindState("none","Метка не найдена",`За ${SEARCH_SECONDS} секунд новых меток у антенны не было.`);
    renderFindList();setFindButtons(false);
  }catch(e){
    if(run!==find.run)return;
    setFindState("none","Поиск не удался",e.message);
    renderFindList();setFindButtons(false);
  }finally{clearInterval(tick)}
}
function startFind(title,source){
  find.slot=title;find.source=source;find.items=[];
  $("find-title").textContent=`Поиск метки, слоты ${title}`;
  $("find-dialog").showModal();
  searchTag();
}
function closeFind(){find.run++;if($("find-dialog").open)$("find-dialog").close()}
$("find-close").onclick=closeFind;
$("find-dialog").addEventListener("cancel",()=>{find.run++});
$("find-again").onclick=searchTag;
$("find-take").onclick=()=>{
  const item=find.picked;
  closeFind();
  state.source=find.source;state.chosenSlot=find.slot;state.uid=item.uid;state.classic=true;
  applyTag(item.tag);
  renderFindSlots();poll();
};
function tagWriteBody(){
  const id=tagMaterial.get();
  const legacy=id.startsWith("L");
  return {material:legacy?+id.slice(1):LEGACY_BY_GROUP[groupOf(tagMaterial.name())]||7,profile:legacy?undefined:id,
          color:tagPicker.get(),vendor:state.vendor,tail:state.tail,source:state.source,uid:state.uid};
}
$("write").onclick=async()=>{
  const overwrite=!!onTag&&!onTag.blank;
  const yes=await confirmDialog(overwrite?"Перезаписать метку?":"Записать на метку?",state.uid?`Метка ${state.uid}`:"",overwrite?"Перезаписать":"Записать",writeChangeView());
  if(!yes)return;
  const wanted=formTag();
  say("");
  setWriting(true);
  try{
    const r=await api("/api/write",tagWriteBody());
    state.sourceUid=r.uid;
    if(!r.readback){
      renderTag();
      say(`Запись прошла, но метку не удалось прочитать обратно: ${r.readback_error} Прочитай её ещё раз.`,"err");
      return;
    }
    applyTag(r.readback,true);
    const read=formTag();
    if(read.id!==wanted.id||read.hex!==wanted.hex){
      onTag.confirmed=false;renderTag();
      say(`На метке оказалось другое: ${tagMaterial.name()}, #${read.hex}.${r.exact_skipped?" Блок 5 этой метки занят, точный цвет туда не записать.":""}`,"err");
      return;
    }
    const code=state.codes.colors.find(c=>c.code===r.palette_code);
    say(r.exact_saved?`Без модуля Box покажет: ${code.name}.`:"");
    showSavedToast();
  }catch(e){say(e.message,"err")}
  finally{setWriting(false)}
};


/* Slots tab */
function showTab(name){
  if(name==="guide"&&!$("guide-frame").src)$("guide-frame").src="/guide";
  ["slots","tags","materials","guide"].forEach(n=>{$("tab-"+n).setAttribute("aria-selected",String(n===name));$("panel-"+n).hidden=n!==name});
  if(name==="slots")loadSlots();
  if(name==="materials")renderMaterials();
  if(name==="tags"){const id=tagMaterial.get();buildTagMaterial(id,tagMaterial.name());loadFindSlots()}
}
$("tab-slots").onclick=()=>showTab("slots");
$("tab-tags").onclick=()=>showTab("tags");
$("tab-materials").onclick=()=>showTab("materials");
$("tab-guide").onclick=()=>showTab("guide");
const rows={};
let slotData=[];let editing=null;
const materialName=id=>{if(/^L\d+$/.test(id||""))return legacyName(+id.slice(1));const m=state.codes.slot_materials.find(x=>x.id===id);return m?m.name:(id||"Не задан")};
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e}
function setSlotNote(r,text,cls){r.note.textContent=text;r.note.className="note"+(cls?" "+cls:"")}
function renderSlot(s){
  const r=rows[s.index];
  renderSpoolLeft(s,r);
  const tag=s.tag&&s.tag.detail?s.tag:null;
  const qidi=!!(s.tag&&s.tag.stock);
  const blank=s.reading||(s.tag&&s.tag.empty);
  if(!tag&&editing===s.index)closeEditor();
  r.card.classList.toggle("editable",!!tag&&!s.reading);
  r.editSlot.innerHTML="";r.mapped.innerHTML="";
  if(r.read)r.read.disabled=s.reading;
  r.spool.innerHTML=spool(blank?null:tag?tag.tag_color:s.color);
  if(s.reading){
    r.name.textContent="Читаю метку…";r.hex.textContent="";
    r.mapped.append(el("div","map-empty","Катушка крутится, до минуты"));
  }else if(s.tag&&s.tag.empty){
    r.name.textContent="Метка не найдена";r.hex.textContent="";
    r.mapped.append(el("div","map-empty","Пусто"));
  }else{
    r.name.textContent=tag?`${tag.brand||""} ${tag.detail}`.trim():materialName(s.filament);
    r.hex.textContent=tag?"#"+tag.tag_color:s.color?"#"+s.color:"цвет не задан";
    if(tag){
      const text=el("div","swap-text");
      const dot=el("span","chip");dot.style.background=s.color?"#"+s.color:"transparent";
      text.append("→ ",dot,` ${materialName(s.filament)}`);
      const btn=el("button","edit-btn");btn.type="button";btn.title="Редактировать замену";
      btn.setAttribute("aria-label",`Редактировать замену для слота ${s.label}`);
      btn.innerHTML='<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
      r.editSlot.append(btn);r.mapped.append(text);
    }else{
      r.mapped.append(el("div","map-empty",qidi?"Метка QIDI":"Без замены"));
    }
  }
  const result=readResult(s);
  if(result&&result[0]!=="Метка не найдена")setSlotNote(r,result[0],result[1]);else if(r.note.className!=="note err"||!s.read_error)setSlotNote(r,"");
}
async function reread(s,r){
  const yes=await confirmDialog(`Перечитать слот ${s.label}?`,"Данные метки будут очищены и прочитаны заново. Мотор Box протянет филамент этого слота. Если в голову заправлен другой слот, мотор покачает катушку назад и вперёд и вернёт на место, а данные обновятся только когда метка найдётся.","Запустить");
  if(!yes)return;
  try{
    await api("/api/slot_read",{index:s.index});
    watchedReads.add(s.index);
    await loadSlots();
    followReads();
  }catch(e){setSlotNote(r,e.message,"err")}
}
// A read started here that found a tag another slot already holds: only the user knows why.
const watchedReads=new Set();
function checkSameTag(){
  for(const index of [...watchedReads]){
    const s=slotData.find(x=>x.index===index);
    if(!s||s.reading)continue;
    watchedReads.delete(index);
    if(!s.read_error&&s.same_tag.length)sameTagDialog(s);
  }
}
async function sameTagDialog(s){
  const others=s.same_tag.map(i=>slotData.find(x=>x.index===i));
  const names=others.map(o=>o.label).join(", ");
  const neighbour=s.same_tag.includes(s.index^1);
  const d=$("same-tag");
  $("same-tag-title").textContent=`Эта метка уже есть в ${names}`;
  $("same-tag-text").textContent=`В слоте ${s.label} прочитана метка, которая записана и в ${names}. Одна катушка не может стоять в двух слотах.`+(neighbour?` Считыватель у ${s.label} и ${slotData.find(x=>x.index===(s.index^1)).label} общий, поэтому он мог поймать соседнюю катушку.`:"");
  $("same-tag-here").textContent=`Катушка теперь в ${s.label}`;
  $("same-tag-next").textContent="Это соседняя катушка";
  $("same-tag-next").hidden=!neighbour;
  const answer=await new Promise(resolve=>{
    const done=v=>{$("same-tag-here").onclick=$("same-tag-next").onclick=$("same-tag-no").onclick=d.oncancel=null;d.close();resolve(v)};
    $("same-tag-here").onclick=()=>done("here");
    $("same-tag-next").onclick=()=>done("next");
    $("same-tag-no").onclick=()=>done(null);
    d.oncancel=e=>{e.preventDefault();done(null)};
    d.showModal();$("same-tag-no").focus();
  });
  try{
    if(answer==="here"){
      for(const o of others)await api("/api/slot_forget",{index:o.index});
    }else if(answer==="next"){
      await api("/api/slot_read",{index:s.index,skip_neighbour:true});
      watchedReads.add(s.index);
    }
    await loadSlots();
    followReads();
  }catch(e){setSlotNote(rows[s.index],e.message,"err")}
}
let followingReads=false;
// While a slot is being read the card shows that, whatever the page was opened from.
async function followReads(){
  if(followingReads)return;
  followingReads=true;
  try{
    while(slotData.some(s=>s.reading)){
      await new Promise(res=>setTimeout(res,1500));
      await loadSlots();
    }
  }finally{followingReads=false}
}
function readResult(s){
  if(s.read_error)return [s.read_error,"err"];
  const tag=s.tag;
  if(!tag)return null;
  if(tag.empty)return ["Метка не найдена",""];
  return null;
}
function slotCard(s){
  const card=el("div","card slot");
  const spoolBox=el("div","spool"),title=el("h2","",s.label),name=el("div","name"),hex=el("div","hex"),mapped=el("div");
  const actions=el("div","actions");
  const editSlot=el("div");
  const r={card,spool:spoolBox,name,hex,mapped,editSlot};
  // The whole card opens the editor; the pencil gives keyboard access to the same action.
  card.onclick=()=>{if(card.classList.contains("editable"))openEditor(s.index)};
  const read=el("button","action alt","Перечитать");read.type="button";r.read=read;
  read.onclick=e=>{e.stopPropagation();reread(slotData.find(x=>x.index===s.index),r)};
  actions.append(read);
  r.note=el("div","note");
  r.left=el("button","spool-left");r.left.type="button";r.left.hidden=true;
  r.left.onclick=e=>{e.stopPropagation();openSpoolDialog(slotData.find(x=>x.index===s.index))};
  card.append(editSlot,spoolBox,title,name,hex,mapped,r.left,actions,r.note);
  rows[s.index]=r;
  return card;
}
// A bar needs a full mark: the weight on the tag, or a usual 1 kg spool.
const spoolFull=s=>(s.tag&&s.tag.weight)||1000;
const LOW_SPOOL_GRAMS=100;
function renderSpoolLeft(s,r){
  const left=s.spool&&s.spool.remaining,set=typeof left==="number";
  r.left.hidden=!s.spool;
  if(!s.spool)return;
  r.left.className="spool-left"+(set?(left<LOW_SPOOL_GRAMS?" low":""):" unset");
  r.left.title="Изменить остаток";
  r.left.setAttribute("aria-label",`Изменить остаток для слота ${s.label}`);
  r.left.textContent=set?`≈ ${Math.round(left)} г`:"Задать остаток";
  if(set){
    const bar=el("span","spool-bar"),fill=el("span");
    fill.style.width=Math.min(100,left/Math.max(spoolFull(s),left)*100)+"%";
    bar.append(fill);r.left.append(bar);
  }
}
let spoolSlot=null;
function openSpoolDialog(s){
  spoolSlot=s;
  const full=spoolFull(s),left=s.spool.remaining;
  $("spool-title").textContent=`Остаток, слот ${s.label}`;
  $("spool-note").textContent="";$("spool-note").className="note";
  $("spool-grams").value=typeof left==="number"?Math.round(left):full;
  $("spool-quick").replaceChildren(...[[1,"Полная"],[.75,"3/4"],[.5,"Половина"],[.25,"1/4"]].map(([part,text])=>{
    const b=el("button","",text);b.type="button";
    b.onclick=()=>{$("spool-grams").value=Math.round(full*part)};
    return b;
  }));
  $("spool-dialog").showModal();
}
$("spool-cancel").onclick=()=>$("spool-dialog").close();
$("spool-save").onclick=async()=>{
  const grams=Number($("spool-grams").value);
  if(!Number.isInteger(grams)||grams<0||grams>5000){$("spool-note").className="note err";$("spool-note").textContent="Укажи целое число от 0 до 5000";return}
  $("spool-save").disabled=true;
  try{
    await api("/api/spool",{index:spoolSlot.index,grams});
    $("spool-dialog").close();
    // The printer picks the value up within a second; the card shows it right away.
    spoolSlot.spool.remaining=grams;
    renderSpoolLeft(spoolSlot,rows[spoolSlot.index]);
  }catch(e){$("spool-note").className="note err";$("spool-note").textContent=e.message}
  finally{$("spool-save").disabled=false}
};
function closeEditor(){
  editing=null;$("editor").hidden=true;$("editor").innerHTML="";
  Object.values(rows).forEach(r=>r.card.removeAttribute("aria-current"));
}
function tagFacts(tag){
  const facts=[["Материал",tag.detail],["Цвет","#"+tag.tag_color]];
  if(tag.weight)facts.push(["Вес",`${tag.weight} г`]);
  if(tag.diameter)facts.push(["Диаметр",`${tag.diameter} мм`]);
  if(tag.min_temp&&tag.max_temp)facts.push(["Сопло",`${tag.min_temp}–${tag.max_temp} °C`]);
  if(tag.dry_temp)facts.push(["Сушка",tag.dry_hours?`${tag.dry_temp} °C, ${tag.dry_hours} ч`:`${tag.dry_temp} °C`]);
  facts.push(["UID",tag.uid]);
  return facts;
}
function openEditor(index){
  const s=slotData.find(x=>x.index===index);
  const tag=s.tag;
  Object.values(rows).forEach(r=>r.card.removeAttribute("aria-current"));
  rows[index].card.setAttribute("aria-current","true");
  editing=index;
  const box=$("editor");box.innerHTML="";box.hidden=false;
  const head=el("div","edit-head");head.append(el("h2","",`Слот ${s.label}`));
  const close=el("button","action alt","Закрыть");close.type="button";close.onclick=closeEditor;head.append(close);
  const compare=el("div","compare");
  const real=el("div","side real");real.append(el("h3","","На метке"));
  const realSpool=el("div","spool");realSpool.innerHTML=spool(tag.tag_color);real.append(realSpool);
  if(tag.brand)real.append(el("div","brand",tag.brand));
  const dl=el("dl");tagFacts(tag).forEach(([k,v])=>dl.append(el("dt","",k),el("dd","",v)));real.append(dl);
  const shown=el("div","side");shown.append(el("h3","","Использовать в Box"));
  const row=el("div","spool-row");const shownSpool=el("div","spool");shownSpool.innerHTML=spool(s.color);
  const shownName=el("div","");const name=el("div","",materialName(s.filament));
  const shownHex=el("div","hex",s.color?"#"+s.color:"цвет не задан");shownName.append(name,shownHex);row.append(shownSpool,shownName);
  const select=el("div");
  const material=makeMaterialField(select,slotMaterialItems(s.filament),s.filament,()=>{name.textContent=material.name()});
  const pickerHost=el("div");
  const picker=makePicker(pickerHost,s.color,c=>{shownSpool.innerHTML=spool(c);shownHex.textContent="#"+c});
  const actions=el("div","actions");
  const save=el("button","action","Сохранить");save.type="button";
  const reset=el("button","action alt","Цвет как на метке");reset.type="button";
  reset.onclick=()=>{picker.set(tag.tag_color);shownSpool.innerHTML=spool(tag.tag_color);shownHex.textContent="#"+tag.tag_color};
  actions.append(save,reset);
  const note=el("div","note",`Будет работать для всех катушек ${tag.brand||""} ${tag.detail} этого цвета, этот слот обновится сразу.`);
  save.onclick=async()=>{
    save.disabled=true;note.className="note";note.textContent="Сохраняю…";
    try{
      await api("/api/mapping",{index:s.index,detail:tag.detail,tag_color:tag.tag_color,filament:material.get(),color:picker.get()});
      note.className="note ok";note.textContent="Сохранено.";
      await loadSlots();
    }catch(e){note.className="note err";note.textContent=e.message}
    save.disabled=false;
  };
  shown.append(row,el("label","field","Материал"),select,el("label","field","Цвет"),pickerHost,actions,note);
  compare.append(real,el("div","arrow","→"),shown);
  box.append(head,compare);
}
const sampleCounts={},sampleMaterials={};
function sampleFields(){return slotData.filter(s=>sampleCounts[s.index]).map(s=>{
  const item={index:s.index,count:sampleCounts[s.index]};
  if(sampleMaterials[s.index])item.filament=sampleMaterials[s.index];
  return item;
})}
let sampleBusy=false;
function syncSampleButtons(){
  const total=Object.values(sampleCounts).reduce((x,y)=>x+y,0);
  $("sample-download").disabled=$("sample-open").disabled=sampleBusy||total===0;
}
function updateSampleButton(){
  const total=Object.values(sampleCounts).reduce((x,y)=>x+y,0);
  syncSampleButtons();
  $("sample-open").textContent=total?`Открыть в QIDI Studio: образцов ${total}`:"Открыть в QIDI Studio";
  refreshPreview();
}
let previewTurn=0;
function textShade(hex){
  const [r,g,b]=hexToRgb(hex);
  // Shows the engraving: the same plastic in shadow, or catching light on dark colors.
  return r*.299+g*.587+b*.114>70?"rgba(0,0,0,.5)":"rgba(255,255,255,.45)";
}
// Top view of the bed, y up as in the slicer. Engraved text is the plate's own color, shaded here.
function plateSvg(data,item,x,top,bed,fill){
  const [w,d]=data.plate,[hx,hy,hr]=data.hole;
  const path=item.boxes.map(([x0,y0,x1,y1])=>`M${(x+x0).toFixed(2)} ${(bed-(bed-top-d)-y1).toFixed(2)}h${(x1-x0).toFixed(2)}v${(y1-y0).toFixed(2)}h${(x0-x1).toFixed(2)}z`).join("");
  return `<rect x="${x}" y="${top}" width="${w}" height="${d}" rx="${data.corner}" fill="#${fill}" stroke="#000" stroke-opacity=".35" stroke-width=".5"/><circle cx="${x+hx}" cy="${top+d-hy}" r="${hr}" fill="var(--card)" stroke="#000" stroke-opacity=".35" stroke-width=".5"/><path d="${path}" fill="${textShade(fill)}"/>`;
}
function drawTemplate(data){
  const [w,d]=data.plate,t=data.template,slot=slotData.find(s=>s.index===t.slot);
  const fill=slot&&isHex(slot.color)?slot.color:"808080";
  $("sample-template-view").innerHTML=`<svg viewBox="-1 -1 ${w+2} ${d+2}" role="img" aria-label="Шаблон образца">${plateSvg(data,t,0,0,d,fill)}</svg>`;
}
function drawPreview(data){
  drawTemplate(data);
  const size=n=>String(n).replace(".",",");
  $("sample-template").textContent=`${size(data.plate[0])} × ${size(data.plate[1])} × ${size(data.height)} мм`;
  const [w,d]=data.plate,bed=data.bed;
  const plates=data.items.map(item=>{
    const slot=slotData.find(s=>s.index===item.slot);
    const fill=slot&&isHex(slot.color)?slot.color:"808080";
    const top=bed-item.y-d;
    const path=item.boxes.map(([x0,y0,x1,y1])=>`M${(item.x+x0).toFixed(2)} ${(bed-item.y-y1).toFixed(2)}h${(x1-x0).toFixed(2)}v${(y1-y0).toFixed(2)}h${(x0-x1).toFixed(2)}z`).join("");
    const [hx,hy,hr]=data.hole;
    return `<rect x="${item.x}" y="${top}" width="${w}" height="${d}" rx="${data.corner}" fill="#${fill}" stroke="#000" stroke-opacity=".35" stroke-width=".5"/><circle cx="${item.x+hx}" cy="${bed-item.y-hy}" r="${hr}" fill="var(--bg)" stroke="#000" stroke-opacity=".35" stroke-width=".5"/><path d="${path}" fill="${textShade(fill)}"/>`;
  }).join("");
  // Always the whole bed: the view stays put while samples are added.
  $("sample-preview").innerHTML=`<svg viewBox="0 0 ${bed} ${bed}" role="img" aria-label="Образцы на столе принтера"><rect width="${bed}" height="${bed}" fill="var(--bg)"/>${plates}</svg>`;
  $("sample-preview-note").textContent=data.items.length?`Вид сверху, стол ${bed}×${bed} мм.`:`Укажи количество, и образцы появятся на столе ${bed}×${bed} мм.`;
}
async function refreshPreview(){
  const turn=++previewTurn;
  try{
    const data=await api("/api/samples_preview",{items:sampleFields()});
    if(turn===previewTurn){drawPreview(data);syncSampleButtons()}
  }catch(e){if(turn===previewTurn){$("sample-preview-note").textContent=e.message}}
}
function sampleInfo(m){
  const parts=[];
  if(m.min_temp&&m.max_temp)parts.push(`сопло ${m.min_temp}–${m.max_temp} °C`);
  if(m.dry_temp)parts.push(`сушка ${m.dry_temp} °C${m.dry_hours?`, ${m.dry_hours} ч`:""}`);
  return parts.join(", ");
}
async function openSamples(){
  await loadSlots();
  const box=$("sample-rows");box.innerHTML="";
  slotData.forEach(s=>{
    // One of each loaded plastic to start with; a count the user set stays.
    if(sampleCounts[s.index]===undefined&&(s.color||s.filament))sampleCounts[s.index]=1;
    const row=el("div","sample-row");
    const dot=el("span","chip");dot.style.background=s.color?"#"+s.color:"transparent";
    const tag=s.tag&&s.tag.detail?s.tag:null;
    const profile=id=>state.codes.slot_materials.find(x=>x.id===id);
    const name=el("div","sample-name");
    const title=el("div","sample-title"),info=el("small");
    const paintText=()=>{
      const chosen=sampleMaterials[s.index];
      const source=chosen?profile(chosen):tag||profile(s.filament);
      title.textContent=`${s.label}  ${source?(source.detail?`${source.brand||""} ${source.detail}`.trim():source.name):"Материал не задан"}`;
      info.textContent=source?sampleInfo(source):"";
    };
    const edit=el("button","sample-edit");edit.type="button";edit.title="Изменить надпись";
    edit.setAttribute("aria-label",`Изменить надпись для слота ${s.label}`);
    edit.innerHTML='<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
    const head=el("div","sample-head");head.append(title,edit);
    const host=el("div");host.hidden=true;
    edit.onclick=()=>{
      if(!host.hidden){host.hidden=true;return}
      const current=sampleMaterials[s.index]||s.filament;
      const field=makeMaterialField(host,slotMaterialItems(current),current,()=>{sampleMaterials[s.index]=field.get();paintText();refreshPreview()});
      host.hidden=false;
      if(!tag||sampleMaterials[s.index]){sampleMaterials[s.index]=field.get();paintText();refreshPreview()}
    };
    name.append(head,info,host);paintText();
    const stepper=el("div","stepper");
    const minus=el("button","","−"),plus=el("button","","+"),out=el("output");
    minus.type=plus.type="button";
    minus.setAttribute("aria-label",`Меньше образцов для слота ${s.label}`);plus.setAttribute("aria-label",`Больше образцов для слота ${s.label}`);
    const paint=()=>{const n=sampleCounts[s.index]||0;out.textContent=n;minus.disabled=n===0;updateSampleButton()};
    minus.onclick=()=>{sampleCounts[s.index]=Math.max(0,(sampleCounts[s.index]||0)-1);paint()};
    plus.onclick=()=>{sampleCounts[s.index]=(sampleCounts[s.index]||0)+1;paint()};
    stepper.append(minus,out,plus);row.append(dot,name,stepper);box.append(row);paint();
  });
  const note=$("sample-note");note.textContent="";note.className="note";
  $("sample-dialog").showModal();
}
$("open-samples").onclick=openSamples;
$("sample-close").onclick=()=>$("sample-dialog").close();
function openExternal(url){location.href=url}
// While a project builds the dialog shows it is still working: on the printer this takes a while.
async function withSampleProgress(action){
  const note=$("sample-note"),text=$("sample-progress-text"),started=Date.now();
  const total=Object.values(sampleCounts).reduce((x,y)=>x+y,0);
  const show=()=>{text.textContent=`Собираю проект, образцов ${total}… ${Math.floor((Date.now()-started)/1000)} с`};
  note.textContent="";note.className="note";
  sampleBusy=true;syncSampleButtons();$("sample-progress").hidden=false;show();
  const tick=setInterval(show,500);
  try{await action(note)}
  catch(e){note.className="note err";note.textContent=e.message}
  finally{clearInterval(tick);$("sample-progress").hidden=true;sampleBusy=false;syncSampleButtons()}
}
const sampleBody=()=>({items:sampleFields()});
$("sample-open").onclick=()=>withSampleProgress(async note=>{
  if(!onPrinter()){
    await api("/api/samples_open",sampleBody());
    note.className="note ok";note.textContent="Проект открыт в QIDI Studio.";
    return;
  }
  const {path}=await api("/api/samples_link",sampleBody());
  const file=encodeURIComponent(location.origin+path);
  openExternal(/Macintosh|Mac OS X/i.test(navigator.userAgent)?`qidistudioopen://${file}`:`qidistudio://open?file=${file}`);
  note.className="note ok";
  note.textContent="Проект отправлен в QIDI Studio. Если браузер спросит, разреши открыть. Если Studio не открылась, нажми «Скачать 3MF».";
});
$("sample-download").onclick=()=>withSampleProgress(async note=>{
  const res=await fetch("/api/samples",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(sampleBody())});
  if(!res.ok)throw new Error((await res.json()).error||"Ошибка");
  const url=URL.createObjectURL(await res.blob());
  const a=document.createElement("a");a.href=url;a.download="qidi_samples.3mf";document.body.append(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL(url),5000);
  note.className="note ok";note.textContent="Файл скачан. Открой его в QIDI Studio, синхронизируй филаменты с Box, проверь и напечатай.";
});
async function loadSlots(){
  const hint=$("slots-hint"),box=$("slots");
  try{
    const data=await api("/api/slots");
    slotData=data.slots;
    if(!box.children.length)slotData.forEach(s=>box.append(slotCard(s)));
    slotData.forEach(renderSlot);
    checkSameTag();
    if(slotData.some(s=>s.reading))followReads();
    hint.textContent="Нажми на карточку, чтобы выбрать, какой материал и цвет QIDI использовать для этой метки.";
  }catch(e){hint.textContent=e.message}
}
(async()=>{
  state.codes=await api("/api/codes");
  const has=name=>state.codes.features.includes(name);
  $("tab-tags").hidden=!has("nfc");
  $("tab-guide").hidden=!has("guide");
  const hasSlots=["bambu","nfc","spools","usage"].some(has);
  $("tab-slots").hidden=!hasSlots;
  buildTagMaterial();
  tagPicker=makePicker($("tag-picker"),"FAFAFA",renderTag);
  state.source=onPrinter()?null:"usb";renderTag();poll();setInterval(poll,1500);
  if(hasSlots)loadSlots();else if(has("guide"))showTab("guide");else showTab("materials");
})();
</script><script src="/print_check.js"></script></body></html>
"""


GROUP_RULES = [(r"Support", "Поддержка"), (r"WOOD", "PLA"), (r"PETG", "PETG"), (r"PET", "PET"),
               (r"PLA", "PLA"), (r"ABS", "ABS"), (r"ASA", "ASA"), (r"(UltraPA|PA)", "PA"), (r"PC", "PC"),
               (r"(TPU|TPEE|PEBA)", "TPU"), (r"PPS", "PPS"), (r"PVA", "PVA")]


def group_of(name):
    """Plastic type of a profile name; the page has the same rules in groupOf."""
    bare = re.sub(r"^(QIDI|Generic|HATCHBOX|PolyLite|Polymaker|Bambu|Overture)\s+", "", name)
    for pattern, group in GROUP_RULES:
        if re.match(pattern, bare):
            return group
    return "Другое"


def import_materials(raw, known):
    """Material list from the printer's filament JSON; ids not seen before are marked new."""
    data = json.loads(raw)
    known_ids = {m["id"]: m for m in known}
    result = []
    for name, value in data.items():
        if not isinstance(value, dict) or not re.fullmatch(r"QF[A-Z0-9]{2,4}", str(value.get("filament_id"))):
            continue
        item = {"id": value["filament_id"], "name": name}
        if isinstance(value.get("type"), str):
            item["type"] = value["type"]
        nozzle = value.get("nozzle_temperature") or []
        if len(nozzle) >= 2:
            item["min_temp"], item["max_temp"] = int(nozzle[0]), int(nozzle[1])
        temp, hours = value.get("filament_dev_ams_drying_temperature"), value.get("filament_dev_ams_drying_time")
        if temp and hours:
            item["dry_temp"], item["dry_hours"] = int(float(temp[0])), int(float(hours[0]))
        if known_ids and (item["id"] not in known_ids or known_ids[item["id"]].get("new")):
            item["new"] = True
        result.append(item)
    if not result:
        raise ValueError("no materials found")
    return result


def refresh_materials(raw):
    """Replace the saved list with the firmware's; returns names of the new materials."""
    target = pathlib.Path(__file__).resolve().parent / "qidi_materials.json"
    known = load_materials()
    items = import_materials(raw, known)
    target.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    known_ids = {k["id"] for k in known}
    return items, [m["name"] for m in items if m["id"] not in known_ids]


def apply_features(names):
    """Without a list, as with a panel from before the switches, everything is on."""
    global features
    features = set(names) if isinstance(names, list) else set(ALL_FEATURES)


def load_features(path=FEATURES_FILE):
    try:
        apply_features(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        apply_features(None)


def pull_state(host):
    """The Mac panel uses the palette and materials chosen on the printer's panel."""
    try:
        with urllib.request.urlopen("http://%s:%d/api/codes" % (host, PORT), timeout=5) as res:
            codes = json.loads(res.read())
    except (urllib.error.URLError, OSError, ValueError):
        return
    apply_features(codes.get("features"))
    if isinstance(codes.get("my_materials"), dict):
        MY_MATERIALS_FILE.write_text(json.dumps(codes["my_materials"]))
    if isinstance(codes.get("my_colors"), list):
        MY_COLORS_FILE.write_text(json.dumps(codes["my_colors"]))


def main():
    global printer_mode
    if len(sys.argv) == 3 and sys.argv[1] == "--import-materials":
        try:
            items, added = refresh_materials(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
        except ValueError as e:
            print("Список материалов с принтера не прочитан: %s" % (e,), file=sys.stderr)
            sys.exit(1)
        print("Материалов на принтере: %d%s" % (len(items), ", новые: " + ", ".join(added) if added else ""))
        return
    if sys.argv[1:] == ["--printer"]:
        printer_mode = True
        load_features()
        try:
            refresh_materials(FIRMWARE_MATERIALS.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print("Список материалов не обновлён: %s" % (e,), flush=True)
        Handler.app = App(printer=Printer("127.0.0.1"))
        if "usage" in features:
            print_check.PrintWatcher(Handler.app.printer).start()
        print("QIDI Box panel on port %d" % (PORT,), flush=True)
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
        return
    host = read_printer_host(pathlib.Path(__file__).resolve().parent.parent / ".env")
    if host:
        pull_state(host)
    Handler.app = App(printer=Printer(host) if host else None)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"QIDI Box panel: http://127.0.0.1:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
