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
# ({name, filament, color, grams, full, plastic, keep}): the count starts at those grams of a spool
# of full grams (the plastic's weight in the list, for the bars) and the slot gets
# the material and color; during a print those two are written when it ends. With keep, the same
# plastic was edited in the list: material, color and name change, the count stays.
# The rack spool (slot16) is counted the same way, as a spool without a tag; SLOT=16 sets it.
#   BAMBU_RFID_SPOOL_SET SLOT=0 GRAMS=700 FULL=1000
#       the same, with the weight of the full spool, which the bars are drawn against. Without
#       FULL the spool keeps the full weight it had. The panel's file then holds "700 1000".
#   BAMBU_RFID_SPOOL_SET SLOT=0 CLEAR=1
#       forget the count of the spool in the slot: a spool without a tag waits for a plastic again,
#       a tagged one starts over from its tag. The panel's file holds "clear" for the same.
# A slot the printer sees empty (the slot sensor, runout_button of box_stepper slotN) is cleared
# with it: no count is shown there, and the tag record of the spool taken out is forgotten
# ({'empty': True}, as the panel's «forget»), so a spool put in later is not taken for it. A spool
# without a tag gets its old count back only while the slot keeps the material and color it had.
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
# The spool on the rack behind the printer: the stock firmware calls it slot16. It has no tag,
# so it is counted like a Box spool without one.
RACK_SLOT = 16
COUNTED_SLOTS = tuple(range(BOX_SLOTS)) + (RACK_SLOT,)
USAGE_FILE = "spool_usage.json"
USAGE_INTERVAL = 1.
USAGE_SAVE_INTERVAL = 30.
SPOOL_REQUEST_FILE = "spool_set_%d.txt"
# A spool request asking to forget the count instead of grams.
CLEAR_REQUEST = "clear"
# Seconds a slot sensor must show no spool before the slot counts as empty: a spool turning
# in the reader must not clear it.
EMPTY_SECONDS = 3.
EMPTY_RECORD = {"empty": True}
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


GENERIC_DETAILS = ("QIDI", "Своя метка")


def tag_about(record):
    """Maker and plastic, color and material of a tagged spool, as far as its tag tells."""
    about = {}
    detail, brand = record.get("detail"), record.get("brand")
    name = ""
    # A factory QIDI tag or our tag without a name holds only codes: the panel names it by profile.
    if isinstance(detail, str) and detail.strip() and detail not in GENERIC_DETAILS and not record.get("unread"):
        name = detail if not isinstance(brand, str) or not brand or detail.startswith(brand) else brand + " " + detail
    if name:
        about["name"] = name[:60]
    for field in ("color", "filament"):
        if isinstance(record.get(field), str) and record[field]:
            about[field] = record[field]
    return about


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
    scripts = ["SAVE_VARIABLE VARIABLE=filament_slot%d VALUE=\"'%s'\"" % (slot, filament),
               "SAVE_VARIABLE VARIABLE=color_slot%d VALUE=\"['%s']\"" % (slot, color)]
    if slot != RACK_SLOT:
        # The rack has no reader and no tag record of its own.
        scripts.append("SAVE_VARIABLE VARIABLE=bambu_tag_slot%d VALUE=\"{'empty': True}\"" % (slot,))
    return scripts


def forget_script(slot):
    return "SAVE_VARIABLE VARIABLE=bambu_tag_slot%d VALUE=\"{'empty': True}\"" % (slot,)


class SlotWatch:
    """Clears a Box slot the printer sees empty. The tag record of the spool taken out is
    forgotten, so the spool put in next is not counted as that one until its own tag is read.
    During a print G-code waits behind the print: the record is then only hidden (see overlay)
    and forgotten once the print is over. The slot whose filament is in the toolhead is left
    alone: the end of a spool passes the sensor while the rest of it is still printed."""

    def __init__(self, slots=BOX_SLOTS):
        self.slots = slots
        self.since = {}
        self.empty = set()
        self.hidden = {}

    def feed(self, present, variables, now, printing, loaded=None):
        """present: slot to True (a spool), False (none) or None (not known). Returns the G-code
        that forgets the records of spools taken out."""
        scripts = []
        for slot in range(self.slots):
            state = present.get(slot)
            if state is None or state:
                self.since.pop(slot, None)
                self.empty.discard(slot)
            elif slot != loaded:
                self.since.setdefault(slot, now)
                if now - self.since[slot] >= EMPTY_SECONDS:
                    self.empty.add(slot)
            name = "bambu_tag_slot%d" % (slot,)
            record = variables.get(name)
            if slot in self.empty and isinstance(record, dict) and not record.get("empty"):
                self.hidden.setdefault(slot, record)
            if slot in self.hidden and self.hidden[slot] != record:
                # Read again since: a new spool, or the same one put back, has its own record now.
                del self.hidden[slot]
            if slot in self.hidden and not printing:
                scripts.append(forget_script(slot))
                del self.hidden[slot]
        return scripts

    def overlay(self, variables):
        """The saved variables with the records of spools taken out hidden."""
        if not self.hidden:
            return variables
        seen = dict(variables)
        for slot, record in self.hidden.items():
            if seen.get("bambu_tag_slot%d" % (slot,)) == record:
                seen["bambu_tag_slot%d" % (slot,)] = dict(EMPTY_RECORD)
        return seen


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

    def set_remaining(self, key, grams, identity=None, full=None):
        """grams left now; full: the weight of the full spool, kept from before when not given."""
        key = self._resolve(key)
        old = self._entry(key, identity) or {}
        entry = {"set_g": float(grams), "used_g": 0., "manual": True}
        full = full if full else old.get("full_g") or old.get("set_g")
        if full:
            entry["full_g"] = float(max(full, grams))
        for field in ("uids", "name", "slot_as", "was", "plastic", "color", "filament"):
            if field in old:
                entry[field] = old[field]
        self._fresh(key, entry, None if "slot_as" in old else identity)

    def forget(self, key):
        """Drop the count: a spool without a tag waits for a plastic, a tagged one is adopted
        again from its tag."""
        key = self._resolve(key)
        if self.spools.pop(key, None) is not None:
            self.dirty = True

    def pick(self, slot, plastic, now, printing):
        """A plastic from the user's list put in a slot without a tag: its count starts at the
        grams given. Returns the G-code for the slot's material and color; while printing it is
        held back until the print ends (pending), and the slot keeps its count meanwhile."""
        entry = {"set_g": float(plastic["grams"]), "used_g": 0., "manual": True, "name": plastic["name"],
                 "slot_as": [plastic["filament"], plastic["color"]]}
        if plastic.get("plastic"):
            entry["plastic"] = plastic["plastic"]
        if plastic.get("full"):
            entry["full_g"] = float(max(plastic["full"], plastic["grams"]))
        old = self._entry("slot:%d" % (slot,), now)
        if plastic.get("keep") and old and old.get("plastic") == plastic.get("plastic"):
            # The plastic was edited in the list: the spool stays, so does its count.
            entry["set_g"], entry["used_g"] = old["set_g"], old["used_g"]
            if old.get("full_g"):
                entry["full_g"] = old["full_g"]
        if printing:
            entry["was"], scripts = list(now), []
        else:
            scripts = pick_scripts(slot, entry)
        self.spools["slot:%d" % (slot,)] = entry
        self.dirty = True
        return scripts

    def pending(self, slots=COUNTED_SLOTS):
        """G-code of the picks made during a print, now that it is over."""
        scripts = []
        for slot in slots:
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
            # What the spool is, for the list of all spools: kept while the spool is out of the Box.
            entry = self.spools.get(key)
            if entry is not None and not key.startswith("slot:"):
                about = tag_about(record)
                if any(entry.get(field) != value for field, value in about.items()):
                    entry.update(about)
                    self.dirty = True

    def view(self, key, identity=None):
        key = self._resolve(key)
        entry = self._entry(key, identity) or {"set_g": None, "used_g": 0.}
        remaining = None
        if entry["set_g"] is not None:
            remaining = max(0., entry["set_g"] - entry["used_g"])
        return {"key": key, "remaining": remaining, "used": entry["used_g"], "full": entry.get("full_g") or entry["set_g"],
                "name": entry.get("name"), "plastic": entry.get("plastic")}

    def save(self):
        if not self.dirty:
            return
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(self.spools, f)
        os.replace(temp, self.path)
        self.dirty = False


def take_spool_request(folder, slot):
    """(grams, full or None) asked for this slot, CLEAR_REQUEST, or None. The request is used up
    even when it is bad."""
    path = os.path.join(folder, SPOOL_REQUEST_FILE % (slot,))
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(32)
        os.remove(path)
    except OSError:
        return None
    if text.strip() == CLEAR_REQUEST:
        return CLEAR_REQUEST
    try:
        numbers = [int(word) for word in text.split()]
    except ValueError:
        return None
    if len(numbers) not in (1, 2) or not 0 <= numbers[0] <= MAX_SPOOL_GRAMS:
        return None
    full = numbers[1] if len(numbers) == 2 else None
    if full is not None and not 0 < full <= MAX_SPOOL_GRAMS:
        return None
    return numbers[0], full


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
    result = {"name": name, "filament": filament, "color": color, "grams": grams}
    full = plastic.get("full")
    if isinstance(full, int) and not isinstance(full, bool) and 0 < full <= MAX_SPOOL_GRAMS:
        result["full"] = full
    # Which plastic of the panel's list it is; keep: the plastic was edited, the count stays.
    if isinstance(plastic.get("plastic"), str) and re.fullmatch(r"[0-9a-z]{1,16}", plastic["plastic"]):
        result["plastic"] = plastic["plastic"]
        result["keep"] = plastic.get("keep") is True
    return result


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
    if match and int(match.group(1)) in COUNTED_SLOTS:
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
        self.watch = SlotWatch()
        self.next_save = 0.
        printer.register_event_handler("klippy:ready", self._start)
        printer.register_event_handler("klippy:disconnect", self._flush)
        printer.lookup_object("gcode").register_command(
            "BAMBU_RFID_SPOOL_SET", self.cmd_SET, desc="Set the filament left on a spool")

    def _variables(self):
        """The saved variables as the count sees them: tags of spools taken out are hidden."""
        saved = self.printer.lookup_object("save_variables", None)
        return self.watch.overlay(saved.allVariables if saved is not None else {})

    def _in_head(self, eventtime, box_status):
        """The slot whose filament is in the toolhead. loaded_slot alone stays after an unload."""
        sensor = self.printer.lookup_object("filament_switch_sensor filament_switch_sensor", None)
        if sensor is not None and not sensor.get_status(eventtime).get("filament_detected"):
            return None
        return active_slot(box_status)

    def _present(self, eventtime):
        """Slot to whether its sensor sees a spool; None where the printer does not say."""
        present = {}
        for slot in range(BOX_SLOTS):
            stepper = self.printer.lookup_object("box_stepper slot%d" % (slot,), None)
            state = stepper.get_status(eventtime).get("runout_button") if stepper is not None else None
            present[slot] = None if state is None else state == 0
        return present

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
        printing = stats["state"] in ("printing", "paused")
        saved = self.printer.lookup_object("save_variables", None)
        scripts = self.watch.feed(self._present(eventtime), saved.allVariables if saved is not None else {},
                                  eventtime, printing, self._in_head(eventtime, box_status))
        self.ledger.adopt(self._variables())
        folder = os.path.dirname(self.ledger.path)
        scripts += [] if printing else self.ledger.pending()
        for slot in COUNTED_SLOTS:
            request = take_spool_request(folder, slot)
            if request == CLEAR_REQUEST:
                self._set(slot, CLEAR_REQUEST)
            elif request is not None:
                self._set(slot, *request)
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

    def _set(self, slot, grams, full=None):
        """Grams left on the spool in the slot, or CLEAR_REQUEST to forget its count."""
        variables = self._variables()
        if grams == CLEAR_REQUEST:
            self.ledger.forget(spool_key(variables, slot))
        else:
            self.ledger.set_remaining(spool_key(variables, slot), grams, slot_values(variables, slot), full)
        self._flush()

    def get_status(self, eventtime):
        variables = self._variables()
        views = []
        for slot in COUNTED_SLOTS:
            if slot in self.watch.empty:
                # The printer sees no spool there: nothing to count, whatever was there before.
                views.append({"key": None, "remaining": None, "used": 0., "full": None, "name": None,
                              "plastic": None, "empty": True})
            else:
                views.append(self.ledger.view(spool_key(variables, slot), slot_values(variables, slot)))
        return {"slots": views[:BOX_SLOTS], "rack": views[BOX_SLOTS]}

    def cmd_SET(self, gcmd):
        slot = gcmd.get_int("SLOT", minval=0, maxval=RACK_SLOT)
        if slot not in COUNTED_SLOTS:
            raise gcmd.error("SLOT is 0-%d or %d for the rack" % (BOX_SLOTS - 1, RACK_SLOT))
        if gcmd.get_int("CLEAR", 0):
            self._set(slot, CLEAR_REQUEST)
            gcmd.respond_info("Slot %d: count forgotten" % (slot,))
            return
        grams = gcmd.get_float("GRAMS", minval=0., maxval=MAX_SPOOL_GRAMS)
        self._set(slot, grams, gcmd.get_float("FULL", 0., minval=0., maxval=MAX_SPOOL_GRAMS) or None)
        gcmd.respond_info("Slot %d: %.0f g left" % (slot, grams))


def load_config(config):
    printer = config.get_printer()
    config_file = printer.get_start_args().get("config_file", "")
    return SpoolUsage(printer, os.path.join(os.path.dirname(config_file), USAGE_FILE), ScreenWarnings(printer))
