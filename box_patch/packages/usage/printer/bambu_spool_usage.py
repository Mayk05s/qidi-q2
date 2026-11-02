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
#   BAMBU_SCREEN_WARNING BOX=3|4
#       raise the stock Box 3 or 4 window of the printer screen for a moment; the panel turns
#       those windows into the low-plastic window.
import json
import logging
import os
import re

BOX_SLOTS = 4
USAGE_FILE = "spool_usage.json"
USAGE_INTERVAL = 1.
USAGE_SAVE_INTERVAL = 30.
SPOOL_REQUEST_FILE = "spool_set_%d.txt"
MAX_SPOOL_GRAMS = 5000
USUAL_SPOOL_GRAMS = 1000.
FILAMENT_MM2 = 2.405  # Cross-section of 1.75 mm filament.
DEFAULT_DENSITY = 1.24
DENSITY = {"PLA": 1.24, "PETG": 1.27, "PET": 1.27, "ABS": 1.04, "ASA": 1.07, "TPU": 1.21,
           "PA": 1.14, "PC": 1.20, "PVA": 1.23, "PPS": 1.30}


def spool_key(variables, slot):
    """What the count belongs to: the spool when its tag is known, otherwise the slot."""
    record = variables.get("bambu_tag_slot%d" % (slot,))
    if isinstance(record, dict):
        if record.get("tray"):
            return "tray:" + record["tray"]
        if record.get("uid"):
            return "uid:" + record["uid"]
    return "slot:%d" % (slot,)


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

    def add(self, key, grams):
        entry = self.spools.setdefault(self._resolve(key), {"set_g": None, "used_g": 0.})
        entry["used_g"] += grams
        self.dirty = True

    def set_remaining(self, key, grams):
        key = self._resolve(key)
        entry = {"set_g": float(grams), "used_g": 0., "manual": True}
        if "uids" in self.spools.get(key, {}):
            entry["uids"] = self.spools[key]["uids"]
        self.spools[key] = entry
        self.dirty = True

    def adopt(self, variables, slots=BOX_SLOTS):
        """A spool seen for the first time counts as full: the weight on its tag, or a usual spool."""
        for slot in range(slots):
            record = variables.get("bambu_tag_slot%d" % (slot,))
            if not isinstance(record, dict) or record.get("empty"):
                continue
            key = spool_key(variables, slot)
            if key.startswith("tray:") and record.get("uid"):
                self._link(key, record["uid"])
            key = self._resolve(key)
            entry = self.spools.get(key)
            if entry is None or entry["set_g"] is None:
                weight = record.get("weight")
                valid = isinstance(weight, (int, float)) and 0 < weight <= MAX_SPOOL_GRAMS
                fresh = {"set_g": float(weight) if valid else USUAL_SPOOL_GRAMS,
                         "used_g": entry["used_g"] if entry else 0.}
                if entry and "uids" in entry:
                    fresh["uids"] = entry["uids"]
                self.spools[key] = fresh
                self.dirty = True

    def view(self, key):
        key = self._resolve(key)
        entry = self.spools.get(key) or {"set_g": None, "used_g": 0.}
        remaining = None
        if entry["set_g"] is not None:
            remaining = max(0., entry["set_g"] - entry["used_g"])
        return {"key": key, "remaining": remaining, "used": entry["used_g"]}

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
        text = self.TEXT % (gcmd.get_int("BOX", minval=3, maxval=4),)
        if text not in self.active:
            self.active.append(text)
        self.reactor.register_callback(lambda e: self._clear(text), self.reactor.monotonic() + self.SHOW_SECONDS)

    def _clear(self, text):
        if text in self.active:
            self.active.remove(text)


class SpoolUsage:
    """The [bambu_spool_usage] section: one per printer."""

    def __init__(self, printer, path):
        self.printer = printer
        self.reactor = printer.get_reactor()
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
        for slot in range(BOX_SLOTS):
            grams = take_spool_request(folder, slot)
            if grams is not None:
                self.ledger.set_remaining(spool_key(self._variables(), slot), grams)
                self._flush()
        part = self.meter.feed(stats["state"], stats["filament_used"], active_slot(box_status))
        if part is not None:
            slot, millimeters = part
            grams = millimeters * FILAMENT_MM2 * slot_density(box_status, slot) / 1000.
            self.ledger.add(spool_key(self._variables(), slot), grams)
        if self.ledger.dirty and (self.meter.last is None or eventtime >= self.next_save):
            self._flush()
            self.next_save = eventtime + USAGE_SAVE_INTERVAL

    def get_status(self, eventtime):
        variables = self._variables()
        return {"slots": [self.ledger.view(spool_key(variables, slot)) for slot in range(BOX_SLOTS)]}

    def cmd_SET(self, gcmd):
        slot = gcmd.get_int("SLOT", minval=0, maxval=BOX_SLOTS - 1)
        grams = gcmd.get_float("GRAMS", minval=0., maxval=MAX_SPOOL_GRAMS)
        self.ledger.set_remaining(spool_key(self._variables(), slot), grams)
        self._flush()
        gcmd.respond_info("Slot %d: %.0f g left" % (slot, grams))


def load_config(config):
    printer = config.get_printer()
    config_file = printer.get_start_args().get("config_file", "")
    ScreenWarnings(printer)
    return SpoolUsage(printer, os.path.join(os.path.dirname(config_file), USAGE_FILE))
