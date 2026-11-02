# Counts the filament left on each spool of the QIDI Box: the plastic usage package.
#
# Install as klippy/extras/bambu_spool_usage.py. Config:
#   [bambu_spool_usage]
# The count goes by the spool's tag, which the bambu_rfid readers save as bambu_tag_slotN,
# so it follows a spool moved to another slot. It is kept in spool_usage.json next to
# printer.cfg. A print subtracts the filament it uses from the spool in the toolhead.
#
# G-code:
#   BAMBU_RFID_SPOOL_SET SLOT=0 GRAMS=800
#       set how much filament the spool in a slot holds; printing then subtracts from it.
#       The panel instead leaves spool_set_<slot>.txt next to printer.cfg: G-code waits its turn
#       behind a running print, for most of a minute at its start, while the file is seen at once.
# A spool without a tag is counted by its slot, and only while the slot keeps the material and
# color it was counted with: a record of a tag that no longer matches the slot is an earlier spool's.
# The Fluidd card puts a plastic from the user's list in such a slot with spool_pick_<slot>.json
# ({name, filament, color, grams}): the count starts at those grams and the slot gets the material
# and color; during a print those two are written when it ends.
#   BAMBU_SCREEN_WARNING BOX=3|4
#       raise the stock Box 3 or 4 window of the printer screen for a moment; the panel turns
#       those windows into the low-plastic window. At a print start the panel leaves
#       screen_warning.txt next to printer.cfg instead ("3 print"): G-code would wait behind the
#       start and pop up after a cancel. With "print" the window shows only while still printing.
import json
import logging
import os
import re
import time

BOX_SLOTS = 4
USAGE_FILE = "spool_usage.json"
USAGE_INTERVAL = 1.
USAGE_SAVE_INTERVAL = 30.
SPOOL_REQUEST_FILE = "spool_set_%d.txt"
PICK_REQUEST_FILE = "spool_pick_%d.json"
SCREEN_REQUEST_FILE = "screen_warning.txt"
SCREEN_REQUEST_AGE = 600.
MAX_SPOOL_GRAMS = 5000
USUAL_SPOOL_GRAMS = 1000.
FILAMENT_MM2 = 2.405  # Cross-section of 1.75 mm filament.
DEFAULT_DENSITY = 1.24
DENSITY = {"PLA": 1.24, "PETG": 1.27, "PET": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21,
           "PA": 1.14, "PC": 1.20, "PVA": 1.23, "PPS": 1.30}


def slot_values(variables, slot):
    """[material code, color] the printer has for the slot."""
    filament = variables.get("filament_slot%d" % (slot,))
    color = variables.get("color_slot%d" % (slot,))
    color = color[0] if isinstance(color, list) and color else None
    return [filament if isinstance(filament, str) else None, color if isinstance(color, str) else None]


def tag_record(variables, slot):
    """The tag of the spool in the slot, or None. A record whose color or material differs from
    the slot's is an earlier spool's: the one in the slot now has no tag, as in the panel."""
    record = variables.get("bambu_tag_slot%d" % (slot,))
    if not isinstance(record, dict) or record.get("empty"):
        return None
    if record.get("unread"):
        return record
    filament, color = slot_values(variables, slot)
    if record.get("color") != color or (record.get("filament") and record["filament"] != filament):
        return None
    return record


def spool_key(variables, slot):
    """What the count belongs to: the spool when its tag is known, otherwise the slot."""
    record = tag_record(variables, slot)
    if record is not None:
        if record.get("tray"):
            return "tray:" + record["tray"]
        if record.get("uid"):
            return "uid:" + record["uid"]
    return "slot:%d" % (slot,)


def pick_scripts(slot, entry):
    """G-code that gives the slot the material and color of the plastic picked for it, and
    forgets the tag of the spool that was there before, as the panel's «forget» does."""
    filament, color = entry["slot_as"]
    return ["SAVE_VARIABLE VARIABLE=filament_slot%d VALUE=\"'%s'\"" % (slot, filament),
            "SAVE_VARIABLE VARIABLE=color_slot%d VALUE=\"['%s']\"" % (slot, color),
            "SAVE_VARIABLE VARIABLE=bambu_tag_slot%d VALUE=\"{'empty': True}\"" % (slot,)]


class SpoolLedger:
    """Grams set by the user and grams printed since, per spool."""

    def __init__(self, path):
        self.path = path
        self.dirty = False
        try:
            with open(path, encoding="utf-8") as f:
                self.spools = json.load(f)
            if not isinstance(self.spools, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            self.spools = {}
        except (OSError, ValueError) as e:
            logging.warning("bambu_rfid: %s ignored: %s", path, e)
            self.spools = {}

    def _resolve(self, key):
        """A read may miss the tray number, but a tag once seen with it stays that spool."""
        if key.startswith("uid:"):
            for other, entry in self.spools.items():
                if other.startswith("tray:") and key[4:] in entry.get("uids", ()):
                    return other
        return key

    def _link(self, key, uid):
        """Remember the tag of a spool, taking over what was counted under the tag alone."""
        old = self.spools.pop("uid:%s" % (uid,), None)
        entry = self.spools.get(key)
        if old is not None:
            if entry is None or old.get("manual"):
                entry = old
            else:
                entry["used_g"] += old["used_g"]
            self.spools[key] = entry
            self.dirty = True
        if entry is not None and uid not in entry.get("uids", ()):
            entry["uids"] = entry.get("uids", []) + [uid]
            self.dirty = True

    def _entry(self, key, identity=None):
        """The count under key. A slot without a tag keeps its count while the slot has the
        material and color it was set for (slot_as, or was until a pick made in a print is
        written): another spool without a tag put in starts unknown."""
        entry = self.spools.get(key)
        if entry is None or identity is None or not key.startswith("slot:") or entry.get("slot_as") is None:
            return entry
        return entry if list(identity) in (entry["slot_as"], entry.get("was")) else None

    def _fresh(self, key, entry, identity):
        if identity is not None and key.startswith("slot:"):
            entry["slot_as"] = list(identity)
        self.spools[key] = entry
        self.dirty = True
        return entry

    def add(self, key, grams, identity=None):
        key = self._resolve(key)
        entry = self._entry(key, identity)
        if entry is None:
            entry = self._fresh(key, {"set_g": None, "used_g": 0.}, identity)
        entry["used_g"] += grams
        self.dirty = True

    def set_remaining(self, key, grams, identity=None):
        key = self._resolve(key)
        old = self._entry(key, identity) or {}
        entry = {"set_g": float(grams), "used_g": 0., "manual": True}
        for field in ("uids", "name", "slot_as", "was"):
            if field in old:
                entry[field] = old[field]
        self._fresh(key, entry, None if "slot_as" in old else identity)

    def pick(self, slot, plastic, now, printing):
        """A plastic from the user's list put in a slot without a tag: its count starts at the
        grams given. Returns the G-code for the slot's material and color; while printing it is
        held back until the print ends (pending), and the slot keeps its count meanwhile."""
        entry = {"set_g": float(plastic["grams"]), "used_g": 0., "manual": True, "name": plastic["name"],
                 "slot_as": [plastic["filament"], plastic["color"]]}
        if printing:
            entry["was"], scripts = list(now), []
        else:
            scripts = pick_scripts(slot, entry)
        self.spools["slot:%d" % (slot,)] = entry
        self.dirty = True
        return scripts

    def pending(self, slots=BOX_SLOTS):
        """G-code of the picks made during a print, now that it is over."""
        scripts = []
        for slot in range(slots):
            entry = self.spools.get("slot:%d" % (slot,))
            if entry and entry.pop("was", None) is not None:
                scripts += pick_scripts(slot, entry)
                self.dirty = True
        return scripts

    def adopt(self, variables, slots=BOX_SLOTS):
        """A spool seen for the first time counts as full: the weight on its tag, or a usual spool."""
        for slot in range(slots):
            record = tag_record(variables, slot)
            if record is None:
                continue
            key = spool_key(variables, slot)
            if key.startswith("tray:") and record.get("uid"):
                self._link(key, record["uid"])
            key = self._resolve(key)
            identity = slot_values(variables, slot)
            entry = self._entry(key, identity)
            if entry is None or entry["set_g"] is None:
                weight = record.get("weight")
                valid = isinstance(weight, (int, float)) and 0 < weight <= MAX_SPOOL_GRAMS
                fresh = {"set_g": float(weight) if valid else USUAL_SPOOL_GRAMS,
                         "used_g": entry["used_g"] if entry else 0.}
                if entry and "uids" in entry:
                    fresh["uids"] = entry["uids"]
                self._fresh(key, fresh, identity)

    def view(self, key, identity=None):
        key = self._resolve(key)
        entry = self._entry(key, identity) or {"set_g": None, "used_g": 0.}
        remaining = None
        if entry["set_g"] is not None:
            remaining = max(0., entry["set_g"] - entry["used_g"])
        return {"key": key, "remaining": remaining, "used": entry["used_g"], "full": entry["set_g"],
                "name": entry.get("name")}

    def save(self):
        if not self.dirty:
            return
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(self.spools, f)
        os.replace(temp, self.path)
        self.dirty = False


def take_spool_request(folder, slot):
    """Grams asked for this slot, or None. The request is used up even when it is bad."""
    path = os.path.join(folder, SPOOL_REQUEST_FILE % (slot,))
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(32)
        os.remove(path)
    except OSError:
        return None
    try:
        grams = int(text.strip())
    except ValueError:
        return None
    return grams if 0 <= grams <= MAX_SPOOL_GRAMS else None


def take_pick_request(folder, slot):
    """The plastic the Fluidd card picked for this slot ({name, filament, color, grams}), or None.
    The request is used up even when it is bad."""
    path = os.path.join(folder, PICK_REQUEST_FILE % (slot,))
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(4096)
        os.remove(path)
    except OSError:
        return None
    try:
        plastic = json.loads(text)
        name = " ".join(str(plastic["name"]).split())[:40]
        filament, color = plastic["filament"], str(plastic["color"]).upper()
        grams = plastic["grams"]
    except (ValueError, KeyError, TypeError):
        return None
    if not name or not isinstance(filament, str) or not re.fullmatch(r"QF[A-Z0-9]{2,4}", filament) \
            or not re.fullmatch(r"[0-9A-F]{6}", color) or isinstance(grams, bool) \
            or not isinstance(grams, int) or not 0 <= grams <= MAX_SPOOL_GRAMS:
        return None
    return {"name": name, "filament": filament, "color": color, "grams": grams}


def take_screen_request(folder, now):
    """(box, only while printing) from the panel's file, which is removed; None without one.
    A file older than SCREEN_REQUEST_AGE is stale and dropped."""
    path = os.path.join(folder, SCREEN_REQUEST_FILE)
    try:
        with open(path) as f:
            words = f.read().split()
        old = now - os.path.getmtime(path) > SCREEN_REQUEST_AGE
        os.remove(path)
    except OSError:
        return None
    if old or not words or words[0] not in ("3", "4"):
        return None
    return int(words[0]), "print" in words[1:]


class FilamentMeter:
    """Turns the print's total filament into millimeters per slot. The last moment of a print
    still counts, and the total starting again from zero is not taken for a negative use."""

    def __init__(self):
        self.last = None
        self.slot = None

    def feed(self, state, used, slot):
        if slot is not None:
            self.slot = slot
        active = state in ("printing", "paused")
        if self.last is None:
            if active:
                self.last = used
            return None
        delta = used - self.last
        self.last = used if active else None
        if delta <= 0 or self.slot is None:
            return None
        return self.slot, delta


def active_slot(box_status):
    match = re.fullmatch(r"slot(\d+)", str(box_status.get("loaded_slot", "")))
    if match and int(match.group(1)) < BOX_SLOTS:
        return int(match.group(1))
    return None


def slot_density(box_status, slot):
    try:
        kind = box_status["boxes"][0]["filament_info"][slot]["filament_type"]
    except (KeyError, IndexError, TypeError):
        return DEFAULT_DENSITY
    return DENSITY.get(kind, DEFAULT_DENSITY)


class ScreenWarnings:
    """Raises a stock window of the printer screen. The screen watches the warning list of
    multi_color_controller, so an entry is added there for a moment. The stock object is not changed."""

    TEXT = "BOX %d temperature and humidity sensor abnormal, heating and drying functions have been turned off."
    SHOW_SECONDS = 20.

    def __init__(self, printer):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.active = []
        printer.register_event_handler("klippy:ready", self._wrap)
        printer.lookup_object("gcode").register_command(
            "BAMBU_SCREEN_WARNING", self.cmd_WARNING, desc="Raise a stock window on the printer screen")

    def _wrap(self):
        controller = self.printer.lookup_object("multi_color_controller", None)
        if controller is None:
            return
        stock = controller.get_status

        def get_status(eventtime=None):
            status = dict(stock(eventtime))
            if self.active:
                status["box_warning_info"] = list(status.get("box_warning_info") or []) + self.active
            return status
        try:
            controller.get_status = get_status
        except AttributeError as e:
            logging.warning("bambu_rfid: no screen warnings: %s", e)

    def cmd_WARNING(self, gcmd):
        self.show(gcmd.get_int("BOX", minval=3, maxval=4))

    def show(self, box):
        text = self.TEXT % (box,)
        if text not in self.active:
            self.active.append(text)
        self.reactor.register_callback(lambda e: self._clear(text), self.reactor.monotonic() + self.SHOW_SECONDS)

    def _clear(self, text):
        if text in self.active:
            self.active.remove(text)


class SpoolUsage:
    """The [bambu_spool_usage] section: one per printer."""

    def __init__(self, printer, path, warnings=None):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.warnings = warnings
        self.ledger = SpoolLedger(path)
        self.meter = FilamentMeter()
        self.next_save = 0.
        printer.register_event_handler("klippy:ready", self._start)
        printer.register_event_handler("klippy:disconnect", self._flush)
        printer.lookup_object("gcode").register_command(
            "BAMBU_RFID_SPOOL_SET", self.cmd_SET, desc="Set the filament left on a spool")

    def _variables(self):
        saved = self.printer.lookup_object("save_variables", None)
        return saved.allVariables if saved is not None else {}

    def _start(self):
        self.reactor.register_timer(self._tick, self.reactor.monotonic() + USAGE_INTERVAL)

    def _flush(self):
        try:
            self.ledger.save()
        except OSError:
            logging.exception("bambu_rfid: could not save %s", self.ledger.path)

    def _tick(self, eventtime):
        # An error in a reactor timer would shut the printer down.
        try:
            self._count(eventtime)
        except Exception:
            logging.exception("bambu_rfid: filament count failed")
        return eventtime + USAGE_INTERVAL

    def _count(self, eventtime):
        stats = self.printer.lookup_object("print_stats").get_status(eventtime)
        box = self.printer.lookup_object("box_extras", None)
        box_status = box.get_status(eventtime) if box is not None else {}
        self.ledger.adopt(self._variables())
        folder = os.path.dirname(self.ledger.path)
        printing = stats["state"] in ("printing", "paused")
        scripts = [] if printing else self.ledger.pending()
        for slot in range(BOX_SLOTS):
            grams = take_spool_request(folder, slot)
            if grams is not None:
                variables = self._variables()
                self.ledger.set_remaining(spool_key(variables, slot), grams, slot_values(variables, slot))
                self._flush()
            plastic = take_pick_request(folder, slot)
            if plastic is not None:
                scripts += self.ledger.pick(slot, plastic, slot_values(self._variables(), slot), printing)
                self._flush()
        for script in scripts:
            self.printer.lookup_object("gcode").run_script(script)
        request = take_screen_request(folder, time.time()) if self.warnings is not None else None
        if request is not None and (not request[1] or stats["state"] in ("printing", "paused")):
            self.warnings.show(request[0])
        part = self.meter.feed(stats["state"], stats["filament_used"], active_slot(box_status))
        if part is not None:
            slot, millimeters = part
            grams = millimeters * FILAMENT_MM2 * slot_density(box_status, slot) / 1000.
            variables = self._variables()
            self.ledger.add(spool_key(variables, slot), grams, slot_values(variables, slot))
        if self.ledger.dirty and (self.meter.last is None or eventtime >= self.next_save):
            self._flush()
            self.next_save = eventtime + USAGE_SAVE_INTERVAL

    def get_status(self, eventtime):
        variables = self._variables()
        return {"slots": [self.ledger.view(spool_key(variables, slot), slot_values(variables, slot))
                          for slot in range(BOX_SLOTS)]}

    def cmd_SET(self, gcmd):
        slot = gcmd.get_int("SLOT", minval=0, maxval=BOX_SLOTS - 1)
        grams = gcmd.get_float("GRAMS", minval=0., maxval=MAX_SPOOL_GRAMS)
        variables = self._variables()
        self.ledger.set_remaining(spool_key(variables, slot), grams, slot_values(variables, slot))
        self._flush()
        gcmd.respond_info("Slot %d: %.0f g left" % (slot, grams))


def load_config(config):
    printer = config.get_printer()
    config_file = printer.get_start_args().get("config_file", "")
    return SpoolUsage(printer, os.path.join(os.path.dirname(config_file), USAGE_FILE), ScreenWarnings(printer))
