"""Does the plastic on the tagged spools last for a print?

Kept apart from the panel and the Klipper module: it only reads what the printer already
publishes. The Fluidd card (fluidd_card.py) and the print watcher use it.
"""

import codecs
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
import zlib

# Remaining grams are counted, not weighed, so a thin spare is a risk and not a promise.
RISK_GRAMS = 15
FILE_LIMIT = 20
# The printer has under 500 MB of memory and Klipper shares it. A 22 MB .gcode.3mf holds 64 MB
# of G-code: read whole at print start, while the stock firmware unpacks the same file, Klipper
# stalled for 3 s and the MCU shut down. So the file is read from disk in pieces of this size.
CHUNK = 1 << 20
GCODE_FOLDERS = ("/home/qidi/printer_data/gcodes", "/home/mks/printer_data/gcodes")
# Not stock codes: the screen shows any code in an error window, with our text.

SLOT_NAME = re.compile(r"slot(\d+)")
# The rack spool behind the printer, which the stock firmware calls slot16.
RACK_SLOT = 16
GRAMS = re.compile(r"^; *(?:total filament weight \[g\] *:|filament used \[g\] *=)(.+)$", re.M)
MOVES = re.compile(r"^(?:T(\d+)\s*$|(M82|M83)\b|G92 E(-?[\d.]+)|G[0123] [^;\n]*E(-?[\d.]+))", re.M)


class CheckError(Exception):
    pass


def gcode_chunks(source):
    """The G-code of a plain file or of the first plate of a .gcode.3mf, in pieces that end on a
    line break. source is a seekable binary file or bytes."""
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    try:
        stream = source
        if source.read(2) == b"PK":
            archive = zipfile.ZipFile(source)
            names = sorted(n for n in archive.namelist() if re.fullmatch(r"Metadata/plate_\d+\.gcode", n))
            if not names:
                raise CheckError("В файле нет G-кода")
            stream = archive.open(names[0])
        else:
            source.seek(0)
        decoder = codecs.getincrementaldecoder("utf8")("ignore")
        rest = ""
        while True:
            block = stream.read(CHUNK)
            text = rest + decoder.decode(block, final=not block)
            if not block:
                if text:
                    yield text
                return
            cut = text.rfind("\n") + 1
            if cut:
                yield text[:cut]
            rest = text[cut:]
    except (zipfile.BadZipFile, zlib.error, EOFError):
        raise CheckError("Файл печати повреждён")


def gcode_text(data):
    return "".join(gcode_chunks(data))


def parse_plan(text):
    """Grams per tool from the slicer, plus the extrusion in print order: [(tool, mm), ...].
    text is the G-code or its pieces from gcode_chunks."""
    found = None
    timeline = []
    tool, relative, position = None, False, 0.0
    for chunk in [text] if isinstance(text, str) else text:
        found = found or GRAMS.search(chunk)
        for match in MOVES.finditer(chunk):
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
    if not found:
        raise CheckError("В файле нет расхода по катушкам. Нарежь его в QIDI Studio")
    try:
        grams = [float(part) for part in found.group(1).split(",")]
    except ValueError:
        raise CheckError("В файле нет расхода по катушкам. Нарежь его в QIDI Studio")
    # QIDI Studio lists the weight only for the filaments a plate uses, in tool order: with T0 and
    # T2 in use the list has two numbers, the second for T2. Spread it over the tools used.
    used = sorted({tool for tool, _ in timeline})
    if used and len(grams) == len(used) and len(grams) <= max(used):
        spread = [0.] * (max(used) + 1)
        for tool, weight in zip(used, grams):
            spread[tool] = weight
        grams = spread
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
        spool = spools.get(index) if isinstance(spools, dict) else \
            spools[index] if index is not None and index < len(spools) else None
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
    """One per panel: the Fluidd card and the print watcher share it, so a file is read once.
    A big file takes half a minute (see CHUNK): it is read in the background, the card shows the
    spools at once (wait=False) and the usage when it is ready. Counted files are kept in
    cache_path, so a panel restart does not read them again."""

    PLANS_KEPT = 20

    def __init__(self, printer, download=None, cache_path=None):
        self.printer = printer
        self.download = download or self._download
        self.cache_path = cache_path
        self.lock = threading.Lock()
        self.plans = self._load_plans()
        self.errors = {}
        self.jobs = {}

    def _load_plans(self):
        if not self.cache_path:
            return {}
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                data = json.load(f)
            return {entry["key"]: entry["plan"] for entry in data if isinstance(entry, dict)}
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def _save_plans(self):
        if not self.cache_path:
            return
        data = [{"key": key, "plan": plan} for key, plan in self.plans.items()]
        try:
            with open(self.cache_path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(self.cache_path + ".tmp", self.cache_path)
        except OSError as e:
            print("Расход по файлу не сохранён: %s" % (e,), flush=True)

    def _download(self, name):
        """The print file, open for reading: from disk on the printer, otherwise downloaded into
        a temporary file. Never whole in memory, see CHUNK."""
        for folder in GCODE_FOLDERS:
            path = os.path.realpath(os.path.join(folder, name))
            if path.startswith(os.path.realpath(folder) + os.sep) and os.path.isfile(path):
                return open(path, "rb")
        url = "http://%s:7125/server/files/gcodes/%s" % (self.printer.host, urllib.parse.quote(name, safe="/"))
        spool = tempfile.TemporaryFile()
        try:
            with urllib.request.urlopen(url, timeout=60) as res:
                shutil.copyfileobj(res, spool, CHUNK)
        except (OSError, ValueError) as e:
            spool.close()
            raise CheckError("Не удалось скачать файл печати: %s" % (e,))
        spool.seek(0)
        return spool

    def files(self):
        data = self.printer.fetch("/server/files/list?root=gcodes")
        items = list(data.get("result") or [])
        items.sort(key=lambda f: f.get("modified") or 0, reverse=True)
        return items

    def _plan(self, item, wait=True):
        """The file's plan, or None while it is still being read and wait is False."""
        key = json.dumps([item["path"], item.get("modified"), item.get("size")])
        with self.lock:
            if key in self.plans:
                return self.plans[key]
            if key in self.errors:
                raise self.errors[key]
            job = self.jobs.get(key)
            if job is None:
                job = self.jobs[key] = threading.Thread(target=self._read, args=(key, item["path"]), daemon=True)
                job.start()
        if not wait:
            return None
        job.join()
        with self.lock:
            if key in self.errors:
                raise self.errors[key]
            return self.plans[key]

    def _read(self, key, path):
        # Klipper runs on the same cores: reading a big file must not take its time.
        # On Linux a thread id is a valid target and only this thread slows down.
        if sys.platform.startswith("linux"):
            try:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
            except (AttributeError, OSError):
                pass
        plan = error = None
        try:
            source = self.download(path)
            try:
                plan = parse_plan(gcode_chunks(source))
            finally:
                if hasattr(source, "close"):
                    source.close()
        except CheckError as e:
            error = e
        except Exception as e:  # Whatever goes wrong, a waiting caller must hear of it.
            error = CheckError("Не удалось прочитать файл печати: %s" % (e,))
        with self.lock:
            self.jobs.pop(key, None)
            if error is not None:
                self.errors[key] = error
                return
            self.plans[key] = plan
            while len(self.plans) > self.PLANS_KEPT:
                self.plans.pop(next(iter(self.plans)))
            self._save_plans()

    def check(self, name=None, wait=True):
        """The running print if there is one, otherwise the named or the newest file. With wait
        False a file not read yet gives no rows and pending True; it is read in the background."""
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
        usage = status.get("bambu_spool_usage") or {}
        # Box slots by number, and the rack spool (slot16) when the module counts it.
        spools = dict(enumerate(usage.get("slots") or []))
        if usage.get("rack"):
            spools[RACK_SLOT] = usage["rack"]
        try:
            plan = self._plan(item, wait)
        except CheckError as e:
            result.update({"rows": [], "message": str(e)})
            return result
        if plan is None:
            result.update({"rows": [], "pending": True})
            return result
        labels = dict(self.printer.SLOTS)
        labels[RACK_SLOT] = "Rack"
        result["rows"] = evaluate(plan, variables, spools, labels,
                                  stats.get("filament_used") or 0.0 if printing else 0.0)
        return result


def show_popup(printer, rows):
    try:
        import screen_popup
    except ImportError:
        return False
    return screen_popup.show(printer, rows)


class PrintWatcher:
    """On the printer: when a print starts, however it was started, shows one window on the
    screen. Nothing else is sent: a G-code message would wait behind the print start and show up
    on the computer and the screen after a cancel. The stock runout sensor still pauses when a
    spool really ends."""

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
            self.popup(self.printer, rows)
        except Exception as e:  # The Fluidd card still tells; the watcher goes on.
            print("Окно о пластике не показано: %s" % (e,), flush=True)

    def run(self):
        while True:
            try:
                self.step()
            except Exception as e:  # The watcher must outlive any printer hiccup.
                print("Проверка пластика: %s" % (e,), flush=True)
            time.sleep(self.INTERVAL)

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

