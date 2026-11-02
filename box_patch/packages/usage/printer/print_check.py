"""Does the plastic on the tagged spools last for a print?

Kept apart from the panel and the Klipper module: it only reads what the printer already
publishes. The Fluidd card (fluidd_card.py) and the print watcher use it.
"""

import io
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import zipfile

# Remaining grams are counted, not weighed, so a thin spare is a risk and not a promise.
RISK_GRAMS = 15
FILE_LIMIT = 20
# Not stock codes: the screen shows any code in an error window, with our text.
SHORT_CODE, LOW_CODE = "QDE_004_900", "QDE_004_901"

SLOT_NAME = re.compile(r"slot(\d+)")
GRAMS = re.compile(r"^; *(?:total filament weight \[g\] *:|filament used \[g\] *=)(.+)$", re.M)
MOVES = re.compile(r"^(?:T(\d+)\s*$|(M82|M83)\b|G92 E(-?[\d.]+)|G[0123] [^;\n]*E(-?[\d.]+))", re.M)


class CheckError(Exception):
    pass


def gcode_text(data):
    """The G-code of a plain file or of the first plate of a .gcode.3mf."""
    if data[:2] != b"PK":
        return data.decode("utf8", "ignore")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        names = sorted(n for n in archive.namelist() if re.fullmatch(r"Metadata/plate_\d+\.gcode", n))
        if not names:
            raise CheckError("В файле нет G-кода")
        return archive.read(names[0]).decode("utf8", "ignore")
    except zipfile.BadZipFile:
        raise CheckError("Файл печати повреждён")


def parse_plan(text):
    """Grams per tool from the slicer, plus the extrusion in print order: [(tool, mm), ...]."""
    found = GRAMS.search(text)
    if not found:
        raise CheckError("В файле нет расхода по катушкам. Нарежь его в QIDI Studio")
    try:
        grams = [float(part) for part in found.group(1).split(",")]
    except ValueError:
        raise CheckError("В файле нет расхода по катушкам. Нарежь его в QIDI Studio")
    timeline = []
    tool, relative, position = None, False, 0.0
    for match in MOVES.finditer(text):
        if match.group(1) is not None:
            tool = int(match.group(1))
        elif match.group(2):
            relative = match.group(2) == "M83"
        elif match.group(3) is not None:
            position = float(match.group(3))
        elif match.group(4) is not None:
            value = float(match.group(4))
            step = value if relative else value - position
            position = position + step
            if tool is None:
                continue
            if timeline and timeline[-1][0] == tool:
                timeline[-1][1] += step
            else:
                timeline.append([tool, step])
    return {"grams": grams, "timeline": [tuple(part) for part in timeline]}


def spent_by_tool(timeline, used_mm):
    """Share of each tool's plan already printed, given the total filament used so far."""
    totals, done = {}, {}
    left = max(used_mm, 0.0)
    for tool, mm in timeline:
        totals[tool] = totals.get(tool, 0.0) + mm
        step = min(max(mm, 0.0), left)
        done[tool] = done.get(tool, 0.0) + step
        left -= step
    return {tool: min(done[tool] / totals[tool], 1.0) if totals[tool] > 0 else 0.0 for tool in totals}


def evaluate(plan, variables, spools, labels, used_mm=0.0):
    """One line per slot in use. Two spools of one colour stay two lines: each has its own tag."""
    shares = spent_by_tool(plan["timeline"], used_mm)
    rows = {}
    for tool, grams in enumerate(plan["grams"]):
        if grams <= 0:
            continue
        match = SLOT_NAME.fullmatch(str(variables.get("value_t%d" % (tool,))))
        index = int(match.group(1)) if match else None
        row = rows.setdefault(index, {"index": index, "label": labels.get(index), "tools": [], "need": 0.0, "left_to_print": 0.0})
        row["tools"].append(tool)
        row["need"] += grams
        row["left_to_print"] += grams * (1.0 - shares.get(tool, 0.0))
    result = []
    for index in sorted(rows, key=lambda key: (key is None, key)):
        row = rows[index]
        spool = spools[index] if index is not None and index < len(spools) else None
        remaining = spool.get("remaining") if isinstance(spool, dict) else None
        color = variables.get("color_slot%s" % (index,))
        tag = variables.get("bambu_tag_slot%s" % (index,))
        row.update({
            "need": round(row["need"], 1), "left_to_print": round(row["left_to_print"], 1),
            "color": color[0] if isinstance(color, list) and color else None,
            "material": tag.get("detail") if isinstance(tag, dict) else None,
            "tag": spool.get("key") if isinstance(spool, dict) else None,
            "remaining": None, "spare": None, "verdict": "unknown",
        })
        if row["label"] is not None and isinstance(remaining, (int, float)):
            spare = remaining - row["left_to_print"]
            row.update({"remaining": round(remaining, 1), "spare": round(spare, 1),
                        "verdict": "short" if spare < 0 else "risk" if spare < RISK_GRAMS else "ok"})
        result.append(row)
    return result


class PrintCheck:
    def __init__(self, printer, download=None):
        self.printer = printer
        self.download = download or self._download
        self.lock = threading.Lock()
        self.cached = None

    def _download(self, name):
        url = "http://%s:7125/server/files/gcodes/%s" % (self.printer.host, urllib.parse.quote(name, safe="/"))
        try:
            with urllib.request.urlopen(url, timeout=60) as res:
                return res.read()
        except (OSError, ValueError) as e:
            raise CheckError("Не удалось скачать файл печати: %s" % (e,))

    def files(self):
        data = self.printer.fetch("/server/files/list?root=gcodes")
        items = list(data.get("result") or [])
        items.sort(key=lambda f: f.get("modified") or 0, reverse=True)
        return items

    def _plan(self, item):
        key = (item["path"], item.get("modified"))
        with self.lock:
            if not self.cached or self.cached[0] != key:
                self.cached = (key, parse_plan(gcode_text(self.download(item["path"]))))
            return self.cached[1]

    def check(self, name=None):
        """The running print if there is one, otherwise the named or the newest file."""
        status = self.printer.fetch("/printer/objects/query?save_variables&print_stats&bambu_spool_usage")
        try:
            status = status["result"]["status"]
            variables = status["save_variables"]["variables"]
            stats = status["print_stats"]
        except (KeyError, TypeError):
            raise CheckError("Klipper не готов")
        printing = stats.get("state") in ("printing", "paused") and bool(stats.get("filename"))
        files = self.files()
        # Files sent from the slicer land in a hidden folder: checkable, but not worth listing.
        visible = [f["path"] for f in files if not f["path"].startswith(".")][:FILE_LIMIT]
        result = {"printing": printing, "files": visible, "risk_grams": RISK_GRAMS}
        current = stats.get("filename") if printing else name or (visible[0] if visible else None)
        result["file"] = current
        if not current:
            result.update({"rows": [], "message": "На принтере нет файлов для печати"})
            return result
        item = next((f for f in files if f["path"] == current), None)
        if item is None:
            if not printing:
                raise CheckError("Такого файла нет на принтере")
            item = {"path": current}
        spools = (status.get("bambu_spool_usage") or {}).get("slots") or []
        try:
            plan = self._plan(item)
        except CheckError as e:
            result.update({"rows": [], "message": str(e)})
            return result
        result["rows"] = evaluate(plan, variables, spools, dict(self.printer.SLOTS),
                                  stats.get("filament_used") or 0.0 if printing else 0.0)
        return result


def low_message(rows):
    """(code, text) for the printer screen, or None when all is fine. One G-code line: no ; or quotes."""
    short = ["%s: нужно %d г, осталось %d г, не хватит %d г" % (
        row["label"], round(row["left_to_print"]), round(row["remaining"]), round(-row["spare"]))
        for row in rows if row["verdict"] == "short"]
    thin = ["%s: нужно %d г, осталось %d г, запас %d г" % (
        row["label"], round(row["left_to_print"]), round(row["remaining"]), round(row["spare"]))
        for row in rows if row["verdict"] == "risk"]
    if short:
        return SHORT_CODE, "Не хватит пластика. %s" % (". ".join(short + thin),)
    if thin:
        return LOW_CODE, "Пластика впритык. %s" % (". ".join(thin),)
    return None


def show_popup(printer, rows):
    try:
        import screen_popup
    except ImportError:
        return False
    return screen_popup.show(printer, rows)


class PrintWatcher:
    """On the printer: when a print starts, however it was started, shows a hint on the screen.
    It only sends a message, the stock runout sensor still pauses when a spool really ends."""

    INTERVAL = 5

    def __init__(self, printer, check=None, popup=None):
        self.printer = printer
        self.check = check or PrintCheck(printer)
        self.popup = popup or show_popup
        self.last = None

    def step(self):
        data = self.printer.fetch("/printer/objects/query?print_stats=state")
        state = data["result"]["status"]["print_stats"]["state"]
        # A service restart in the middle of a print is not a start: self.last is None then.
        started = state == "printing" and self.last not in (None, "printing", "paused")
        self.last = state
        if not started:
            return
        rows = self.check.check().get("rows") or []
        try:
            if self.popup(self.printer, rows):
                return
        except Exception as e:  # A broken window must not cost the hint: the plain message follows.
            print("Окно о пластике не показано: %s" % (e,), flush=True)
        message = low_message(rows)
        if message:
            script = 'RESPOND TYPE=error MSG="Code:%s Message:%s"' % message
            self.printer.fetch("/printer/gcode/script?" + urllib.parse.urlencode({"script": script}), post=True)

    def run(self):
        while True:
            try:
                self.step()
            except Exception as e:  # The watcher must outlive any printer hiccup.
                print("Проверка пластика: %s" % (e,), flush=True)
            time.sleep(self.INTERVAL)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

