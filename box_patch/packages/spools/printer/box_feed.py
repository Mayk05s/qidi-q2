# Feeding and ejecting spools in the QIDI Box without the stock queue.
# bambu_rfid only connects this package; tag reading stays there.
import json
import logging
import os
import re
import threading
import time

BOX_SLOTS = 4
PROFILE_ID = re.compile(r"QF[A-Z0-9]{2,4}")
SLOT_ENABLE_DELAY = 0.1
# A move of ours: from the slot sensor to the joint is about 1065 mm.
BOX_MOVE_MAX = 800.
BOX_MOVE_VELOCITY = 40.
BOX_MOVE_MAX_VELOCITY = 80.
BOX_MOVE_ACCEL = 200.
# From the stock working position back to the slot sensor is about 1320 mm, more after an overfeed.
# Pieces go on until the sensor sees the filament leave, then the motor is switched off.
EJECT_CHUNK = 200.
EJECT_CHUNK_LEAD = 1.
EJECT_MAX_LENGTH = 4000.
EJECT_BATCH_DELAY = 0.2
# Most of an eject goes in one fast run over the length known to be fed; only the last
# EJECT_SLOW_ZONE goes slowly, in pieces, until the sensor sees the filament leave.
EJECT_SLOW_ZONE = 150.
# Speeds of the multi-feed modes, mm/s and mm/s². The stock firmware moves the Box motors at
# up to 90 mm/s with 50 mm/s², our feed has run at 200 mm/s² on this printer. Tag search was checked in the model at 30 to 100 mm/s feed.
MODES = {
    "quiet": {"feed": 30., "eject": 40., "eject_slow": 20., "accel": 50., "motors": "2"},
    "normal": {"feed": 60., "eject": 90., "eject_slow": 40., "accel": 200., "motors": "4"},
    "fast": {"feed": 80., "eject": 100., "eject_slow": 50., "accel": 200., "motors": "4"},
}
DEFAULT_MODE = "normal"
# A custom mode is held to these, the same limits as the sliders in the panel.
LIMITS = {"feed": (20., 80.), "eject": (20., 100.), "eject_slow": (10., 50.), "accel": (50., 200.)}
# How many Box motors may turn at once: a number, or "pairs" for one per reader (1A+1C, 1B+1D).
MOTOR_LIMITS = ("4", "3", "2", "pairs", "1")
# QIDI's own "Silent (50%)" print mode sets this speed factor.
SILENT_SPEED_FACTOR = 0.5
# Slots held back by the motor limit are tried again this often.
LIMIT_RETRY = 0.5
# Next to printer.cfg; the panel writes it as a file, as G-code would wait behind a print.
MODE_FILE = "box_feed.json"
# Our moves must not meet a stock move of the same motor: on 2 Nov the stock check of an
# inserted spool started during our feed and shut Klipper down, so that check is skipped.
BOX_TAKEOVER = True
BOX_TRACE = ("slot_load", "do_home", "do_move", "drip_move", "cmd_SLOT_PROMPT_MOVE",
             "cmd_EXTRUDER_LOAD", "cmd_SLOT_UNLOAD", "cmd_SLOT_RFID_READ", "slot_sync", "disable_stepper",
             "start_rfid_read", "switch_next_slot", "multi_complete")
SLOT_START_DELAY = 0.4
SLOT_RELEASE_DELAY = 0.3
# How far a spool may go forward before its slot's splitter is known: no motor turns it further,
# whatever the tag search wants. On 6 Nov the splitter of 1A was found 963 mm past where the gear
# took the filament, 1B 1073, 1D 1168; at 1000 mm all four had reached it on 2 Nov. The tag
# search is given PAIR_SWING_ROOM less, as it swings past its limit; tags it does not find on the
# way are looked for after the splitter check, turning back from where the spool then stands.
FEED_LENGTH = 850.
PAIR_SWING_ROOM = 100.
FEED_RETRY = 0.5
FEED_START_DELAY = 2.
FEED_CATCH_LENGTH = 30.
# Back by less than forward: on 2 Nov a spool pushed in only just past the sensor came out of it
# on a full 30 mm back move and was lost.
FEED_CATCH_BACK = 10.
FEED_CATCH_VELOCITY = 15.
INSERT_POLL = 0.1
# Stock methods of a Box motor that move it. On 5 Nov the stock flow moved slot 0 during our
# eject and Klipper shut down (Internal error in stepcompress): now ours stops first and the
# stock move waits until our planned steps have run, at most STOCK_WAIT_MAX seconds.
STOCK_MOVES = ("do_move", "do_home", "drip_move", "slot_sync", "disable_stepper")
STOCK_WAIT_MAX = 15.
STOCK_MARGIN = 0.1
# Commands of the screen that load or unload the toolhead: never from the screen while printing.
HEAD_COMMANDS = ("LOAD_FILAMENT_CONTROL", "UNLOAD_FILAMENT_CONTROL")
# The check of each fed spool up to the splitter sensor, one slot at a time, only while nothing
# prints and the splitter is free. The motor goes in short pieces while the sensor is watched
# every SPLITTER_POLL seconds, and stops within a few millimetres once it is pressed. On 6 Nov the
# stock homing called with guessed arguments did not stop at the sensor; the sensor itself, read
# while each slot turned 500 mm, was the only one that changed (b_endstop_state 1 -> 0).
SLOTS_FILE = "box_feed_slots.json"
# The check runs at the feed speed of the mode: at 60 mm/s the filament goes about 30 mm past the
# sensor before the motor stops (at 10 mm/s, 5 mm), and that is taken back with the rest.
SPLITTER_POLL = 0.02
SPLITTER_PIECE = 0.15
SPLITTER_LEAD = 0.35
SPLITTER_JOIN = 0.2
SPLITTER_SEARCH = 500.
# With the splitter of the slot known, the motor runs fast to this short of it, then homes.
SPLITTER_APPROACH = 120.
# After the check the filament stands this far back from the splitter.
SPLITTER_PARK = 200.
# The feed of a slot with a known splitter goes this short of it: the tag search swings about
# 100 mm further, and the hand pushes each spool in by a different length.
SPLITTER_MARGIN = 250.
SAFE_MIN = 500.
SAFE_MAX = 1200.
CHECK_RETRY = 0.5
# A reread that cannot start is tried again this many times, then given up.
REREAD_TRIES = 240
SENSOR_WORDS = ("state", "endstop", "button", "sensor", "runout", "trigger")
SENSOR_RESCAN = 2.


def load_pair_feed():
    # Imported late: pair_feed takes its settings from this module.
    try:
        from . import pair_feed
    except ImportError:
        import pair_feed
    return pair_feed


def tag_save_scripts(slot, tag, filament):
    """G-code that writes a tag read by the module into the slot, as a stock read would."""
    scripts = []
    if filament:
        scripts.append("SAVE_VARIABLE VARIABLE=filament_slot%d VALUE=\"'%s'\"" % (slot, filament))
    scripts.append("SAVE_VARIABLE VARIABLE=color_slot%d VALUE=\"['%s']\"" % (slot, tag["color"]))
    record = {key: tag[key] for key in ("uid", "detail", "tag_color", "color", "weight", "diameter",
                                        "min_temp", "max_temp", "dry_temp", "dry_hours", "brand")
              if tag.get(key) is not None}
    record["filament"] = filament
    if tag.get("tray"):
        record["tray"] = tag["tray"]
    scripts.append("SAVE_VARIABLE VARIABLE=bambu_tag_slot%d VALUE=\"%r\"" % (slot, record))
    return scripts


def legacy_filament_id(code):
    """Profile id the stock read gives to a tag's material code, or None if it is not known."""
    try:
        from . import box_rfid
        table = box_rfid.LEGACY_RFID_FILAMENT_ID_MAP
    except (ImportError, AttributeError):
        return None
    found = table.get(code, table.get(str(code)))
    return found if isinstance(found, str) and PROFILE_ID.fullmatch(found) else None


def reader_of(slot):
    return "card_reader_%d" % (slot // 2 + 1,)


def parse_slots(text):
    """Slot numbers of a move, or ValueError. Each slot counts once."""
    try:
        slots = [int(part) for part in text.split(",")]
    except ValueError:
        raise ValueError("SLOTS must be numbers")
    if not slots or len(set(slots)) != len(slots) or not all(0 <= slot < BOX_SLOTS for slot in slots):
        raise ValueError("bad SLOTS: %s" % (text,))
    return slots


def trace_calls(obj, label, names, log):
    """Write to the log when the stock methods of `obj` are called, and do nothing else."""
    traced = []
    for name in names:
        method = getattr(obj, name, None)
        if method is None:
            continue

        def wrapper(*args, _name=name, _method=method, **kwargs):
            log("bambu_rfid trace %s.%s %r %r", label, _name, args[:3], sorted(kwargs))
            return _method(*args, **kwargs)
        try:
            setattr(obj, name, wrapper)
        except AttributeError:
            continue
        traced.append(name)
    return traced


class BoxMover:
    """Moves several Box motors along one trajectory, the way FORCE_MOVE moves one."""

    def __init__(self, printer, ffi=None):
        self.calc_move_time = None
        if ffi is None:
            import chelper
            from . import force_move
            ffi = chelper.get_ffi()
            self.calc_move_time = force_move.calc_move_time
        self.printer = printer
        self.ffi_main, self.ffi_lib = ffi
        self.trapq = self.ffi_main.gc(self.ffi_lib.trapq_alloc(), self.ffi_lib.trapq_free)
        self.kinematics = {}
        self.stock_eject = None
        self.stock_reread = None
        # How far past the slot sensor each fed spool stands, as far as we fed it.
        self.lengths = {}
        self.mode_path = None
        self.clock = time.localtime
        self.reread_pending = set()
        self.reread_timer = None
        self.eject_pending = set()
        self.eject_timer = None
        self.insert_timer = None
        self.trapqs = {}
        self.busy = set()
        self.ejecting = set()
        self.feeding = set()
        self.readers = {}
        self.pairs = {}
        self.stock_slot_load = {}
        self.verify_pending = []
        self.pair_jobs = 0
        self.feed_timer = None
        self.feed_pending = set()
        # What the slot sensors showed at the last look, to see a spool put in.
        self.seen = None
        # When the steps we planned for each slot end, and what the stock code holds: a slot it
        # moves now or moved until stock_until, and the slot synced to the extruder for printing.
        self.until = {}
        self.stock_busy = {}
        self.stock_until = {}
        self.synced = set()
        self.own_call = None
        self.stock_head = {}
        self.stock_tool = None
        # The splitter check: how far from where it was gripped each slot's splitter was found,
        # slots checked since they were put in, slots waiting, and slots read again after it.
        self.slots_path = None
        self.splitter = None
        self.checked = set()
        self.check_pending = []
        self.check_timer = None
        self.retried = set()
        # Slots whose fed length is only assumed: fed before Klipper started and taken to stand at
        # the safe point by a reread. Their splitter check notes no splitter.
        self.guessed = set()
        self.overrun = 0.
        # The slot whose splitter check runs now, and whether it is to stop (ejected meanwhile).
        self.checking = None
        self.check_abort = False
        # Until when the last checked spool is still on its way back from the sensor: it keeps
        # the sensor pressed a moment, which is not a filament in the toolhead.
        self.splitter_back_until = 0.
        # The order spools were put in: they go to the splitter in it.
        self.insert_order = {}
        self.insert_count = 0
        self.reread_tries = {}
        # Every *_state value of the Box objects, logged when it changes: which sensors there are
        # and when each one switches is otherwise not visible.
        self.sensor_names = None
        self.sensor_values = {}
        self.sensor_scan = 0.

    @classmethod
    def of(cls, printer):
        mover = printer.lookup_object("bambu_box_mover", None)
        if mover is None:
            mover = cls(printer)
            printer.add_object("bambu_box_mover", mover)
        return mover

    def _kinematics(self, slot):
        # A solver keeps the state of its stepper, so the motors cannot share one.
        if slot not in self.kinematics:
            self.kinematics[slot] = self.ffi_main.gc(
                self.ffi_lib.cartesian_stepper_alloc(b"x"), self.ffi_lib.free)
        return self.kinematics[slot]

    def move(self, slots, distance, speed, accel, calc_move_time):
        toolhead = self.printer.lookup_object("toolhead")
        force_move = self.printer.lookup_object("force_move")
        enables = self.printer.lookup_object("stepper_enable")
        steppers = [(slot, force_move.lookup_stepper("slot%d" % (slot,))) for slot in slots]
        print_time = toolhead.get_last_move_time()
        for slot, stepper in steppers:
            enables.lookup_enable(stepper.get_name()).motor_enable(print_time)
        toolhead.dwell(SLOT_ENABLE_DELAY)
        toolhead.flush_step_generation()
        previous = []
        for slot, stepper in steppers:
            previous.append((stepper, stepper.set_stepper_kinematics(self._kinematics(slot)),
                             stepper.set_trapq(self.trapq)))
            stepper.set_position((0., 0., 0.))
        axis_r, accel_t, cruise_t, cruise_v = calc_move_time(distance, speed, accel)
        print_time = toolhead.get_last_move_time()
        self.ffi_lib.trapq_append(self.trapq, print_time, accel_t, cruise_t, accel_t,
                                  0., 0., 0., axis_r, 0., 0., 0., cruise_v, accel)
        end_time = print_time + accel_t + cruise_t + accel_t
        for slot, stepper in steppers:
            stepper.generate_steps(end_time)
        self.ffi_lib.trapq_finalize_moves(self.trapq, end_time + 99999.9, end_time + 99999.9)
        for stepper, kinematics, trapq in previous:
            stepper.set_trapq(trapq)
            stepper.set_stepper_kinematics(kinematics)
        toolhead.note_mcu_movequeue_activity(end_time)
        toolhead.dwell(accel_t + cruise_t + accel_t)
        toolhead.flush_step_generation()

    def start_move(self, slots, distance, speed, accel):
        self.start_sequence(slots, [(distance, speed, accel)])

    def start_sequence(self, slots, moves, begin=None, finish=True):
        """Start the motors now, whatever else is planned: each slot has its own trajectory and
        the toolhead timeline is not touched, so a move can begin while another one runs."""
        reactor = self.printer.get_reactor()
        toolhead = self.printer.lookup_object("toolhead")
        force_move = self.printer.lookup_object("force_move")
        enables = self.printer.lookup_object("stepper_enable")
        estimated = toolhead.mcu.estimated_print_time(reactor.monotonic())
        start_time = estimated + SLOT_START_DELAY if begin is None else begin
        plan, end_time, position = [], start_time, 0.
        for distance, speed, accel in moves:
            axis_r, accel_t, cruise_t, cruise_v = self.calc_move_time(distance, speed, accel)
            plan.append((end_time, accel_t, cruise_t, accel_t, position, axis_r, 0., cruise_v, accel))
            end_time += accel_t + cruise_t + accel_t
            position += distance
        if begin is None:
            for slot in slots:
                enables.lookup_enable("slot%d" % (slot,)).motor_enable(estimated + SLOT_ENABLE_DELAY)
        self._append(slots, plan, end_time)
        if finish:
            self._finish_after(slots, end_time)
        return end_time

    def start_piece(self, slot, begin, piece):
        """Plan one piece of a longer run (see piece()); begin None starts the motor now."""
        reactor = self.printer.get_reactor()
        estimated = self.printer.lookup_object("toolhead").mcu.estimated_print_time(reactor.monotonic())
        if begin is None:
            begin = estimated + SLOT_START_DELAY
            self.printer.lookup_object("stepper_enable").lookup_enable(
                "slot%d" % (slot,)).motor_enable(estimated + SLOT_ENABLE_DELAY)
        accel_t, cruise_t, decel_t, axis_r, start_v, cruise_v, accel = piece
        end_time = begin + accel_t + cruise_t + decel_t
        self._append([slot], [(begin, accel_t, cruise_t, decel_t, 0., axis_r, start_v, cruise_v, accel)],
                     end_time)
        return begin, end_time

    def _append(self, slots, plan, end_time):
        toolhead = self.printer.lookup_object("toolhead")
        force_move = self.printer.lookup_object("force_move")
        for slot in slots:
            name = "slot%d" % (slot,)
            stepper = force_move.lookup_stepper(name)
            trapq = self._trapq(slot)
            old_kinematics = stepper.set_stepper_kinematics(self._kinematics(slot))
            old_trapq = stepper.set_trapq(trapq)
            stepper.set_position((0., 0., 0.))
            for begin, accel_t, cruise_t, decel_t, start_pos, axis_r, start_v, cruise_v, accel in plan:
                self.ffi_lib.trapq_append(trapq, begin, accel_t, cruise_t, decel_t,
                                          start_pos, 0., 0., axis_r, 0., 0., start_v, cruise_v, accel)
            stepper.generate_steps(end_time)
            self.ffi_lib.trapq_finalize_moves(trapq, end_time + 99999.9, end_time + 99999.9)
            stepper.set_trapq(old_trapq)
            stepper.set_stepper_kinematics(old_kinematics)
            self.busy.add(slot)
            self.until[slot] = max(self.until.get(slot, 0.), end_time)
        toolhead.note_mcu_movequeue_activity(end_time)

    def _finish_after(self, slots, end_time):
        reactor = self.printer.get_reactor()
        estimated = self.printer.lookup_object("toolhead").mcu.estimated_print_time(reactor.monotonic())
        reactor.register_timer(lambda eventtime: self._finish_move(slots, eventtime),
                               reactor.monotonic() + (end_time - estimated) + SLOT_RELEASE_DELAY)

    def _finish_move(self, slots, eventtime):
        reactor = self.printer.get_reactor()
        try:
            toolhead = self.printer.lookup_object("toolhead")
            enables = self.printer.lookup_object("stepper_enable")
            print_time = toolhead.mcu.estimated_print_time(eventtime) + SLOT_ENABLE_DELAY
            for slot in slots:
                self.motor_off(slot, print_time, enables)
                self.busy.discard(slot)
                self.ejecting.discard(slot)
                self.feeding.discard(slot)
            toolhead.note_mcu_movequeue_activity(print_time)
        except Exception:
            # An error here runs in a reactor timer and would shut the printer down.
            logging.exception("bambu_rfid: could not release slots %s", slots)
        return reactor.NEVER

    def _trapq(self, slot):
        # One per slot: moves of a trajectory must follow each other in time.
        if slot not in self.trapqs:
            self.trapqs[slot] = self.ffi_main.gc(self.ffi_lib.trapq_alloc(), self.ffi_lib.trapq_free)
        return self.trapqs[slot]

    def _estimated(self):
        reactor = self.printer.get_reactor()
        return self.printer.lookup_object("toolhead").mcu.estimated_print_time(reactor.monotonic())

    def _planned_time(self):
        """Where the toolhead timeline ends, without flushing it as get_last_move_time does."""
        toolhead = self.printer.lookup_object("toolhead")
        planned = getattr(toolhead, "print_time", None)
        if not isinstance(planned, (int, float)):
            planned = toolhead.get_last_move_time()
        return max(planned, self._estimated())

    def stock_owns(self, slot, estimated=None):
        """The stock code moves this motor now, or has steps of it still to run, or the slot
        prints: then the motor is not ours to turn or switch off."""
        if slot in self.synced or self.stock_busy.get(slot):
            return True
        if slot not in self.stock_until:
            return False
        if estimated is None:
            estimated = self._estimated()
        return estimated < self.stock_until[slot] + STOCK_MARGIN

    def motor_off(self, slot, print_time, enables=None):
        """Switch the motor off at the end of our move, unless the stock code has taken it."""
        if self.stock_owns(slot):
            logging.info("bambu_rfid: slot%d is the stock code's now, its motor stays on", slot)
            return
        if enables is None:
            enables = self.printer.lookup_object("stepper_enable")
        enables.lookup_enable("slot%d" % (slot,)).motor_disable(print_time)

    def hook_guard(self):
        """A stock move of a motor we turn would mix its steps with ours and shut Klipper down:
        it waits until ours have run. Every stock move is noted, so ours wait for it in turn."""
        for slot in range(BOX_SLOTS):
            stepper = self.printer.lookup_object("box_stepper slot%d" % (slot,), None)
            if stepper is None or getattr(stepper, "_bambu_guarded", False):
                continue
            guarded = []
            for name in STOCK_MOVES:
                method = getattr(stepper, name, None)
                if method is None:
                    continue
                try:
                    setattr(stepper, name, self._guarded(slot, name, method))
                except AttributeError:
                    continue
                guarded.append(name)
            try:
                stepper._bambu_guarded = True
            except AttributeError:
                pass
            logging.info("bambu_rfid: stock moves of slot%d guarded: %s", slot, guarded)

    def _guarded(self, slot, name, stock):
        def call(*args, **kwargs):
            if self.own_call == slot:
                return stock(*args, **kwargs)
            if name == "slot_sync":
                on = bool(args[0]) if args else bool(kwargs.get("sync", kwargs.get("enable", False)))
                if not on:
                    self.synced.discard(slot)
                    return stock(*args, **kwargs)
                self._take_from_us(slot, name)
                self.synced.add(slot)
                return stock(*args, **kwargs)
            if name == "disable_stepper":
                if slot in self.moving() and not self.stock_owns(slot):
                    # Ours switches it off when its move ends; off now, the move would be lost.
                    logging.info("bambu_rfid: stock disable of slot%d skipped, it is ours and turning", slot)
                    return None
                return stock(*args, **kwargs)
            self._take_from_us(slot, name)
            self.stock_busy[slot] = self.stock_busy.get(slot, 0) + 1
            try:
                return stock(*args, **kwargs)
            finally:
                self.stock_busy[slot] -= 1
                try:
                    last = self._planned_time()
                    self.stock_until[slot] = max(self.stock_until.get(slot, 0.), last)
                except Exception:
                    logging.exception("bambu_rfid: end of the stock move of slot%d not known", slot)
                    self.stock_until[slot] = self._estimated() + STOCK_WAIT_MAX
        return call

    def _take_from_us(self, slot, name):
        """Stop what we do with this slot and wait until our planned steps have run."""
        try:
            pending = slot in self.feed_pending or slot in self.eject_pending or slot in self.reread_pending
            if slot not in self.moving() and not pending and self._estimated() >= self.until.get(slot, 0.):
                return
            logging.info("bambu_rfid: stock %s of slot%d while it is ours: ours stops first", name, slot)
            self.abandon(slot)
            self._wait_steps(slot)
        except Exception:
            # Whatever went wrong here, the stock move must still go on.
            logging.exception("bambu_rfid: could not hand slot%d to the stock code", slot)

    def abandon(self, slot):
        """Give a slot up: no more moves of ours are planned for it, and its fed length, now
        unknown, is forgotten. Steps already planned still run."""
        for group in (self.ejecting, self.eject_pending, self.feed_pending, self.reread_pending,
                      self.feeding):
            group.discard(slot)
        if slot in self.check_pending:
            self.check_pending.remove(slot)
        if slot == self.checking:
            self.check_abort = True
        self.lengths.pop(slot, None)
        pair = self.pairs.get(reader_of(slot))
        if pair is not None:
            pair.release_slot(slot)

    def _wait_steps(self, slot):
        reactor = self.printer.get_reactor()
        deadline = reactor.monotonic() + STOCK_WAIT_MAX
        while self._estimated() < self.until.get(slot, 0.) + STOCK_MARGIN:
            if reactor.monotonic() > deadline:
                logging.warning("bambu_rfid: steps of slot%d still planned after %.0f s", slot, STOCK_WAIT_MAX)
                return
            if threading.current_thread() is threading.main_thread():
                reactor.pause(reactor.monotonic() + 0.05)
            else:
                time.sleep(0.05)

    def _settings(self):
        """The file the Multi-feed tab of the panel writes; anything odd in it counts as unset."""
        if self.mode_path is None:
            config_file = self.printer.get_start_args().get("config_file", "")
            self.mode_path = os.path.join(os.path.dirname(config_file), MODE_FILE)
        try:
            with open(self.mode_path, encoding="utf-8") as f:
                settings = json.load(f)
        except (OSError, ValueError):
            settings = {}
        return settings if isinstance(settings, dict) else {}

    def _quiet_now(self, settings):
        """Night time set in the panel, or QIDI's silent print mode when the panel says to follow it."""
        night = settings.get("night")
        if not isinstance(night, dict):
            return None
        if night.get("on") is True:
            try:
                start, end = (int(night[k][:2]) * 60 + int(night[k][3:5]) for k in ("from", "to"))
            except (KeyError, TypeError, ValueError):
                start = end = None
            if start is not None:
                now = self.clock()
                minute = now.tm_hour * 60 + now.tm_min
                if (start <= minute < end) if start <= end else (minute >= start or minute < end):
                    return "night"
        if night.get("follow_printer") is True:
            reactor = self.printer.get_reactor()
            gcode_move = self.printer.lookup_object("gcode_move", None)
            if gcode_move is not None and \
                    gcode_move.get_status(reactor.monotonic())["speed_factor"] <= SILENT_SPEED_FACTOR + 0.01:
                return "printer"
        return None

    def mode(self):
        """Name and speeds to use now: the mode chosen in the panel, or the quiet one at night or
        while QIDI's silent print mode is on, if the panel asks for that."""
        settings = self._settings()
        name = settings.get("mode", DEFAULT_MODE)
        quiet = self._quiet_now(settings)
        if quiet:
            return "quiet (%s)" % (quiet,), MODES["quiet"]
        if name == "custom" and isinstance(settings.get("custom"), dict):
            custom = settings["custom"]
            speeds = dict(MODES[DEFAULT_MODE])
            for key, (low, high) in LIMITS.items():
                value = custom.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    speeds[key] = float(min(high, max(low, value)))
            if custom.get("motors") in MOTOR_LIMITS:
                speeds["motors"] = custom["motors"]
            return "custom", speeds
        if name not in MODES:
            name = DEFAULT_MODE
        return name, MODES[name]

    def cmd_mode(self, gcmd):
        name = gcmd.get("MODE", None)
        if name is None:
            current, speeds = self.mode()
            gcmd.respond_info("bambu_rfid: multi-feed mode %s %s" % (current, sorted(speeds.items())))
            return
        name = name.lower()
        if name not in MODES:
            raise gcmd.error("bambu_rfid: MODE must be one of %s" % (", ".join(MODES),))
        # Only the mode changes; custom values and night time set in the panel stay.
        settings = self._settings()
        settings["mode"] = name
        with open(self.mode_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False)
        gcmd.respond_info("bambu_rfid: multi-feed mode %s" % (name,))

    def moving(self):
        """Slots whose motor turns or is about to: planned moves, ejects and pair runs."""
        moving = set(self.busy) | set(self.ejecting)
        for pair in self.pairs.values():
            moving.update(pair.active)
        return moving

    def admit(self, slots, extra=()):
        """Which of these slots may start now under the motor limit of the mode; the rest wait.
        extra are slots already let in this round."""
        limit = self.mode()[1].get("motors", "4")
        moving = self.moving() | set(extra)
        admitted = []
        for slot in slots:
            if self.stock_owns(slot):
                # It waits until the stock code is done with this motor.
                continue
            if slot in moving:
                admitted.append(slot)
                continue
            if limit == "pairs":
                if any(reader_of(other) == reader_of(slot) for other in moving):
                    continue
            elif len(moving) >= int(limit):
                continue
            moving.add(slot)
            admitted.append(slot)
        return admitted

    def _start_batch(self, slots, distance):
        """Start the slots that are free; a slot still moving is left out with a warning."""
        free = [slot for slot in slots if slot not in self.busy]
        if free:
            name, speeds = self.mode()
            self.start_move(free, distance, speeds["eject_slow"], speeds["accel"])
        return free

    def _eject_start(self, slots):
        """One fast run over what is known to be fed, less EJECT_SLOW_ZONE, then slow pieces."""
        name, speeds = self.mode()
        groups = {}
        for slot in slots:
            fast = self.lengths.pop(slot, self.safe_length(slot)) - EJECT_SLOW_ZONE
            self.forget_check(slot)
            groups.setdefault(max(0., fast), []).append(slot)
        for fast, group in sorted(groups.items()):
            if fast > 0.:
                self._eject_chunk(group, None, 0., fast, speeds["eject"])
            else:
                self._eject_chunk(group, None, 0.)

    def _eject_chunk(self, slots, begin, moved, distance=EJECT_CHUNK, speed=None):
        """Eject in short pieces until the slot sensor sees the filament leave, whatever the
        length turns out to be. A short piece also frees the slot soon after that."""
        name, speeds = self.mode()
        end_time = self.start_sequence(slots, [(-distance, speed or speeds["eject_slow"], speeds["accel"])],
                                       begin=begin, finish=False)
        reactor = self.printer.get_reactor()
        estimated = self.printer.lookup_object("toolhead").mcu.estimated_print_time(reactor.monotonic())
        reactor.register_timer(lambda eventtime: self._eject_next(slots, end_time, moved + distance, eventtime),
                               reactor.monotonic() + (end_time - estimated) - EJECT_CHUNK_LEAD)

    def _eject_next(self, slots, end_time, moved, eventtime):
        reactor = self.printer.get_reactor()
        try:
            going = [slot for slot in slots if slot in self.ejecting]
            if going and moved >= EJECT_MAX_LENGTH:
                logging.warning("bambu_rfid: slots %s still see filament after %.0f mm", going, moved)
                names = ", ".join("1" + "ABCD"[slot] for slot in going)
                self.printer.lookup_object("gcode").respond_raw(
                    "!! Eject of %s stopped after %.0f mm: filament did not leave the sensor, it may be stuck"
                    % (names, moved))
                self.ejecting.difference_update(going)
                going = []
            done = [slot for slot in slots if slot not in going]
            if done:
                self._finish_after(done, end_time)
            if going:
                self._eject_chunk(going, end_time, moved)
        except Exception:
            # An error here runs in a reactor timer and would shut the printer down.
            logging.exception("bambu_rfid: eject of %s failed", slots)
            self._finish_after(slots, end_time)
        return reactor.NEVER

    def hook_eject(self):
        """Take over the stock eject of a slot not in the toolhead: it moves the toolhead for
        half a minute and blocks the queue, though only the slot's motor has to run."""
        if self.stock_eject is not None:
            return
        gcode = self.printer.lookup_object("gcode")
        self.stock_eject = gcode.register_command("EJECT_FILAMENT_CONTROL", None)
        if self.stock_eject is None:
            logging.warning("bambu_rfid: stock eject not found, hook not set")
            self.stock_eject = False
            return
        gcode.register_command("EJECT_FILAMENT_CONTROL", self.cmd_eject,
                               desc="Eject filament, slots not in the toolhead without the toolhead")
        self.hook_reread()
        self.hook_guard()
        self.hook_head()
        self.hook_tool()
        gcode.register_command("BOX_FEED_SPLITTER", self.cmd_splitter,
                               desc="Run a fed spool up to the splitter sensor and back, to know its length")

    def hook_head(self):
        """The screen's load into and unload from the toolhead: refused from the screen while
        printing, now that its buttons stay on during a print for the other slots."""
        gcode = self.printer.lookup_object("gcode")
        for name in HEAD_COMMANDS:
            if name in self.stock_head:
                continue
            stock = gcode.register_command(name, None)
            if stock is None:
                logging.warning("bambu_rfid: stock %s not found, hook not set", name)
                continue
            self.stock_head[name] = stock
            gcode.register_command(name, self._head_command(name, stock), desc="%s, not from the screen while printing" % (name,))

    def _head_command(self, name, stock):
        def cmd(gcmd):
            if self._refused_while_printing(gcmd, "Loading or unloading the toolhead"):
                return
            stock(gcmd)
        return cmd

    def hook_tool(self):
        """A colour change to a slot we still feed or eject pauses the print; the change runs
        again on resume."""
        if self.stock_tool is not None:
            return
        gcode = self.printer.lookup_object("gcode")
        self.stock_tool = gcode.register_command("T_MANAGER", None)
        if self.stock_tool is None:
            logging.warning("bambu_rfid: stock T_MANAGER not found, hook not set")
            self.stock_tool = False
            return
        gcode.register_command("T_MANAGER", self.cmd_tool, desc="Tool change, paused while the Box slot is ours")

    def _printing(self, now=None):
        if now is None:
            now = self.printer.get_reactor().monotonic()
        return self.printer.lookup_object("print_stats").get_status(now)["state"] == "printing"

    def _from_print(self):
        sdcard = self.printer.lookup_object("virtual_sdcard", None)
        return sdcard is not None and sdcard.is_cmd_from_sd()

    def _refused_while_printing(self, gcmd, what):
        """From the screen, the panel or Fluidd, never from the print itself."""
        if not self._printing() or self._from_print():
            return False
        gcmd.respond_raw("!! %s is not allowed while printing: pause the print first" % (what,))
        return True

    def ours_now(self, slot):
        """We turn this slot or are about to."""
        return slot in self.moving() or slot in self.feeding or slot in self.feed_pending \
            or slot in self.eject_pending or slot in self.reread_pending

    def cmd_tool(self, gcmd):
        slot = None
        try:
            if self._printing() and self._from_print():
                tool = gcmd.get_int("T", None)
                value = self.printer.lookup_object("save_variables").allVariables.get("value_t%s" % (tool,))
                found = re.fullmatch(r"slot(\d+)", str(value))
                if found and int(found.group(1)) < BOX_SLOTS and self.ours_now(int(found.group(1))):
                    slot = int(found.group(1))
        except Exception:
            logging.exception("bambu_rfid: tool change check failed")
            slot = None
        if slot is None:
            self.stock_tool(gcmd)
            return
        sdcard = self.printer.lookup_object("virtual_sdcard")
        after = sdcard.get_file_position()
        try:
            # The file goes on from this same line on resume, so the change is not lost.
            sdcard.set_file_position(sdcard.file_position)
            gcmd.respond_raw("!! Slot 1%s is still being fed or ejected: print paused, resume when it stops"
                             % ("ABCD"[slot],))
            self.printer.lookup_object("gcode").run_script_from_command("PAUSE")
        except Exception:
            logging.exception("bambu_rfid: pause for slot%d failed, the stock change goes on", slot)
            sdcard.set_file_position(after)
            self.stock_tool(gcmd)
            return
        logging.info("bambu_rfid: tool change to slot%d while it is ours, print paused", slot)

    def hook_reread(self):
        """Take over the screen's Reread of a slot not in the toolhead: the stock one runs the
        filament up to the splitter and back, half a minute with the queue and the screen held."""
        if self.stock_reread is not None:
            return
        gcode = self.printer.lookup_object("gcode")
        self.stock_reread = gcode.register_command("READ_RFID", None)
        if self.stock_reread is None:
            logging.warning("bambu_rfid: stock READ_RFID not found, hook not set")
            self.stock_reread = False
            return
        gcode.register_command("READ_RFID", self.cmd_reread,
                               desc="Read a spool tag again, slots not in the toolhead by turning the spool")
        gcode.register_command("BOX_FEED_MODE", self.cmd_mode,
                               desc="Multi-feed speed: MODE=quiet, normal or fast")

    def cmd_reread(self, gcmd):
        slot = gcmd.get_int("SLOT")
        now = self.printer.get_reactor().monotonic()
        if not self._eject_ourselves(slot) or self.printer.lookup_object(
                "box_stepper slot%d" % (slot,)).get_status(now)["runout_button"] != 0:
            # The stock read runs the filament up to the splitter: not while printing.
            if self._refused_while_printing(gcmd, "The stock read of this slot"):
                return
            self.stock_reread(gcmd)
            return
        if slot in self.busy or slot in self.ejecting:
            gcmd.respond_info("bambu_rfid: slot %d is moving, read it again when it stops" % (slot,))
            return
        if not self.is_fed(slot) and slot not in self.feeding:
            # Not known how far it is in: turning it back as a fed spool would pull it out.
            self.request_feed(slot)
            gcmd.respond_info("bambu_rfid: slot %d was not fed, feeding it" % (slot,))
            return
        if not self.admit([slot]):
            # Too many motors turn already: it starts as soon as one stops.
            self.reread_pending.add(slot)
            self._start_reread_timer()
            gcmd.respond_info("bambu_rfid: slot %d waits for a free motor to read again" % (slot,))
            return
        if not self.reread(slot):
            gcmd.respond_info("bambu_rfid: slot %d is moving, read it again when it stops" % (slot,))
            return
        self.feeding.add(slot)
        gcmd.respond_info("bambu_rfid: reading slot %d again" % (slot,))

    def _start_reread_timer(self):
        if self.reread_timer is None:
            reactor = self.printer.get_reactor()
            self.reread_timer = reactor.register_timer(self._run_rereads, reactor.monotonic() + LIMIT_RETRY)

    def _run_rereads(self, eventtime):
        reactor = self.printer.get_reactor()
        reactor.unregister_timer(self.reread_timer)
        self.reread_timer = None
        try:
            for slot in self.admit(sorted(self.reread_pending)):
                if slot in self.busy or slot in self.ejecting or not self.reader_quiet(slot, True):
                    # Still moving, after the splitter check for one, or its neighbour on the
                    # reader turns: it waits, as only the spool read may turn.
                    continue
                if self.reread(slot):
                    self.reread_pending.discard(slot)
                    self.reread_tries.pop(slot, None)
                    self.feeding.add(slot)
                    continue
                self.reread_tries[slot] = self.reread_tries.get(slot, 0) + 1
                if self.reread_tries[slot] > REREAD_TRIES:
                    logging.warning("bambu_rfid: reread of slot%d could not start, given up", slot)
                    self.reread_pending.discard(slot)
                    self.reread_tries.pop(slot, None)
            if self.reread_pending:
                self._start_reread_timer()
        except Exception:
            # An error here runs in a reactor timer and would shut the printer down.
            logging.exception("bambu_rfid: reread of %s failed", sorted(self.reread_pending))
            self.reread_pending.clear()
        return reactor.NEVER

    def in_head(self, slot, now=None):
        """The slot feeds the toolhead: synced to the extruder, or loaded while the head has
        filament or a print runs."""
        if slot in self.synced:
            return True
        if now is None:
            now = self.printer.get_reactor().monotonic()
        loaded = self.printer.lookup_object("box_extras").get_status(now).get("loaded_slot")
        if loaded != "slot%d" % (slot,):
            return False
        state = self.printer.lookup_object("print_stats").get_status(now)["state"]
        sensor = self.printer.lookup_object("filament_switch_sensor filament_switch_sensor")
        return state in ("printing", "paused") or bool(sensor.get_status(now)["filament_detected"])

    def _eject_ourselves(self, slot):
        """Ours is any slot not in the toolhead, during a print too: only its motor turns."""
        if not 0 <= slot < BOX_SLOTS:
            return False
        return not self.in_head(slot)

    def cmd_eject(self, gcmd):
        slot = gcmd.get_int("SLOT")
        if not self._eject_ourselves(slot):
            if self._refused_while_printing(gcmd, "Unloading the slot that prints"):
                return
            self.stock_eject(gcmd)
            return
        self.request_eject(slot)
        gcmd.respond_info("bambu_rfid: ejecting slot %d" % (slot,))

    def request_eject(self, slot):
        """Slots asked for within EJECT_BATCH_DELAY start at the same moment."""
        if slot == self.checking:
            # Its splitter check stops first; the eject starts once its steps have run.
            self.check_abort = True
        self.eject_pending.add(slot)
        if self.eject_timer is None:
            reactor = self.printer.get_reactor()
            self.eject_timer = reactor.register_timer(self._run_ejects, reactor.monotonic() + EJECT_BATCH_DELAY)

    def _run_ejects(self, eventtime):
        reactor = self.printer.get_reactor()
        reactor.unregister_timer(self.eject_timer)
        self.eject_timer = None
        slots, self.eject_pending = sorted(self.eject_pending), set()
        try:
            free = [slot for slot in slots if slot not in self.busy]
            free = self.admit(free) if free else []
            if free:
                self.ejecting.update(free)
                self._eject_start(free)
            # Slots still moving or held back by the motor limit go as soon as they can.
            self.eject_pending.update(set(slots) - set(free))
            if self.eject_pending:
                self.eject_timer = reactor.register_timer(self._run_ejects, eventtime + LIMIT_RETRY)
        except Exception:
            # An error here runs in a reactor timer and would shut the printer down.
            logging.exception("bambu_rfid: eject of %s failed", slots)
        return reactor.NEVER

    def hook_slot_load(self):
        """The stock check of an inserted spool runs one slot at a time: it homes to the joint
        sensor, so it cannot share the motors with our feed. A spool we feed skips it."""
        for slot in range(BOX_SLOTS):
            if slot in self.stock_slot_load:
                # Each reader calls this; one wrapper per slot.
                continue
            stepper = self.printer.lookup_object("box_stepper slot%d" % (slot,))
            self.stock_slot_load[slot] = stepper.slot_load
            stepper.slot_load = self._slot_load_wrapper(slot, stepper.slot_load)

    def _slot_load_wrapper(self, slot, stock):
        def slot_load(*args, **kwargs):
            reactor = self.printer.get_reactor()
            if not self._insert_watch_on(reactor.monotonic(), slot):
                return stock(*args, **kwargs)
            logging.info("bambu_rfid: slot%d is fed by us, stock check skipped", slot)
            self.request_feed(slot)
            return None
        return slot_load

    def request_feed(self, slot):
        """Wait for the hand to finish pushing the filament in; slots inserted meanwhile start
        together. A spool being pushed in may trigger the sensor again: that adds no second feed."""
        if slot in self.feeding or slot in self.feed_pending:
            logging.info("bambu_rfid: slot%d is already being fed", slot)
            return
        self.feed_pending.add(slot)
        if self.feed_timer is None:
            reactor = self.printer.get_reactor()
            self.feed_timer = reactor.register_timer(self._run_feeds, reactor.monotonic() + FEED_START_DELAY)

    def _pair_of(self, slot):
        """The driver of the two slots of one reader."""
        pair_feed = load_pair_feed()
        reader = reader_of(slot)
        if reader not in self.pairs:
            self.pairs[reader] = pair_feed.PairDriver(self, reader, (slot - slot % 2, slot - slot % 2 + 1))
        return self.pairs[reader]

    def reread(self, slot):
        """Read the tag of a fed spool again: the stock read refuses while the toolhead is loaded."""
        return self._pair_of(slot).reread(slot, self.printer.get_reactor().monotonic())

    def _mark_ready(self, slots):
        # 3 means waiting for the stock check, 1 means ready for printing.
        gcode = self.printer.lookup_object("gcode")
        for slot in slots:
            gcode.run_script("SAVE_VARIABLE VARIABLE=slot%d VALUE=1" % (slot,))

    def mark_empty(self, slots):
        """0 means empty. A slot left at 3 makes the stock code only push a spool put in by 50 mm,
        without the check our feed takes over (2 Nov, slots 1C and 1D after an eject)."""
        gcode = self.printer.lookup_object("gcode")
        for slot in slots:
            gcode.run_script("SAVE_VARIABLE VARIABLE=slot%d VALUE=0" % (slot,))

    def is_fed(self, slot):
        """Fed by us in this run, or marked ready for printing."""
        saved = self.printer.lookup_object("save_variables").allVariables
        return slot in self.lengths or saved.get("slot%d" % (slot,)) == 1

    def start_insert_watch(self):
        """Watch the slot sensors to stop an eject as soon as the filament has left."""
        if self.insert_timer is None:
            reactor = self.printer.get_reactor()
            self.insert_timer = reactor.register_timer(self._watch_inserts, reactor.monotonic() + INSERT_POLL)

    def _insert_watch_on(self, now, slot):
        # Only while the stock read on insertion is off, so that only one of us moves the filament.
        # During a print every slot but the one in the toolhead is ours.
        saved = self.printer.lookup_object("save_variables").allVariables
        return saved.get("auto_read_rfid") == 0 and not self.in_head(slot, now)

    def _watch_inserts(self, eventtime):
        try:
            present = {}
            for slot in range(BOX_SLOTS):
                stepper = self.printer.lookup_object("box_stepper slot%d" % (slot,))
                present[slot] = stepper.get_status(eventtime)["runout_button"] == 0
            self._stop_finished_ejects(present, eventtime)
            self._feed_new_spools(present, eventtime)
            self._log_sensors()
        except Exception:
            # An error here runs in a reactor timer and would shut the printer down.
            logging.exception("bambu_rfid: slot watch failed")
        return eventtime + INSERT_POLL

    def _feed_new_spools(self, present, eventtime):
        """A spool put in is fed even when the stock code does not call slot_load for it."""
        seen, self.seen = self.seen, present
        if seen is None:
            return
        for slot in sorted(present):
            if present[slot] and not seen.get(slot, True) and slot not in self.ejecting \
                    and slot not in self.feeding and slot not in self.feed_pending \
                    and self._insert_watch_on(eventtime, slot):
                logging.info("bambu_rfid: slot%d spool put in, fed by us", slot)
                self.request_feed(slot)

    def _stop_finished_ejects(self, present, eventtime):
        """Once the filament has left the sensor the motor has nothing to move. The planned steps
        stay queued, so the slot counts as busy until they end; only the driver is switched off."""
        for slot in sorted(self.ejecting):
            if present.get(slot):
                continue
            self.ejecting.discard(slot)
            toolhead = self.printer.lookup_object("toolhead")
            print_time = toolhead.mcu.estimated_print_time(eventtime) + SLOT_ENABLE_DELAY
            self.motor_off(slot, print_time)
            toolhead.note_mcu_movequeue_activity(print_time)
            self.lengths.pop(slot, None)
            self.forget_check(slot)
            self.mark_empty([slot])

    def _run_feeds(self, eventtime):
        reactor = self.printer.get_reactor()
        reactor.unregister_timer(self.feed_timer)
        self.feed_timer = None
        slots, self.feed_pending = sorted(self.feed_pending), set()
        try:
            present = [slot for slot in slots if self.printer.lookup_object(
                "box_stepper slot%d" % (slot,)).get_status(eventtime)["runout_button"] == 0]
            started = [slot for slot in present if slot not in self.busy]
            started = self.admit(started) if started else []
            if started:
                # A short move back and forth lets the gear catch the tip before the pair logic
                # takes the spool over.
                self.feeding.update(started)
                for slot in started:
                    # A new spool: checked at the splitter again and read again after it.
                    self.forget_check(slot)
                    self.insert_count += 1
                    self.insert_order[slot] = self.insert_count
                accel = self.mode()[1]["accel"]
                caught = self.start_sequence(started, [
                    (FEED_CATCH_LENGTH, FEED_CATCH_VELOCITY, accel),
                    (-FEED_CATCH_BACK, FEED_CATCH_VELOCITY, accel)], finish=False)
                for slot in started:
                    self._pair_of(slot).insert(slot, eventtime, caught)
            # A slot just ejected keeps its old plan until that ends, and one held back by the
            # motor limit waits for a free motor, so they start later.
            self.feed_pending.update(set(present) - set(started))
            if self.feed_pending:
                self.feed_timer = reactor.register_timer(self._run_feeds, eventtime + FEED_RETRY)
        except Exception:
            logging.exception("bambu_rfid: feed of %s failed", slots)
        return reactor.NEVER

    def _log_sensors(self):
        objects = ["box_extras"] + ["box_stepper slot%d" % (slot,) for slot in range(BOX_SLOTS)]
        now = self.printer.get_reactor().monotonic()
        if self.sensor_names is None or now > self.sensor_scan + SENSOR_RESCAN:
            # Some values only appear once their sensor has first switched.
            self.sensor_scan = now
            before = self.sensor_names
            self.sensor_names = {}
            for name in objects:
                obj = self.printer.lookup_object(name, None)
                found = []
                for key in dir(obj) if obj is not None else []:
                    if any(word in key for word in SENSOR_WORDS) and not key.startswith("_"):
                        try:
                            value = getattr(obj, key)
                        except Exception:
                            continue
                        if isinstance(value, (int, float, str, bool)) or value is None:
                            found.append(key)
                self.sensor_names[name] = found
            if self.sensor_names != before:
                logging.info("bambu_rfid: Box sensor values watched: %s", self.sensor_names)
        changed = []
        for name, keys in self.sensor_names.items():
            obj = self.printer.lookup_object(name, None)
            for key in keys:
                value = getattr(obj, key, None)
                label = "%s.%s" % (name.replace("box_stepper ", ""), key)
                if label in self.sensor_values and self.sensor_values[label] != value:
                    changed.append("%s %r->%r" % (label, self.sensor_values[label], value))
                self.sensor_values[label] = value
        if changed:
            logging.info("bambu_rfid: sensors %s", ", ".join(changed))

    def _splitters(self):
        """Where the splitter of each slot was found, counted from where the motor gripped the
        spool; kept in a file next to printer.cfg."""
        if self.splitter is None:
            if self.slots_path is None:
                config_file = self.printer.get_start_args().get("config_file", "")
                self.slots_path = os.path.join(os.path.dirname(config_file), SLOTS_FILE)
            self.splitter = {}
            try:
                with open(self.slots_path, encoding="utf-8") as f:
                    found = json.load(f).get("splitter", {})
                for key, value in found.items():
                    if int(key) in range(BOX_SLOTS) and isinstance(value, (int, float)) and value > 0:
                        self.splitter[int(key)] = float(value)
            except (OSError, ValueError, AttributeError, TypeError):
                pass
        return self.splitter

    def _save_splitters(self):
        try:
            with open(self.slots_path, "w", encoding="utf-8") as f:
                json.dump({"splitter": {str(k): round(v, 1) for k, v in sorted(self._splitters().items())}}, f)
        except OSError:
            logging.exception("bambu_rfid: could not save %s", self.slots_path)

    def safe_length(self, slot):
        """How far to feed this slot: short of its splitter when that is known."""
        known = self._splitters().get(slot)
        if known is None:
            return FEED_LENGTH
        return max(SAFE_MIN, min(SAFE_MAX, known - SPLITTER_MARGIN))

    def forward_cap(self, slot):
        """No motor of ours turns a spool further forward than this: its safe point, or after
        the splitter check the place it was parked at."""
        if slot in self.checked and slot in self.lengths:
            return self.lengths[slot]
        return self.safe_length(slot)

    def forget_check(self, slot):
        self.checked.discard(slot)
        self.guessed.discard(slot)
        self.retried.discard(slot)
        if slot in self.check_pending:
            self.check_pending.remove(slot)

    def feed_done(self, slots):
        """Spools fed and standing at the safe point go to the splitter check, in the order they
        were put in."""
        for slot in slots:
            if slot not in self.checked and slot not in self.check_pending:
                self.check_pending.append(slot)
        self.check_pending.sort(key=lambda slot: (self.insert_order.get(slot, 0), slot))
        if self.check_pending and self.check_timer is None:
            reactor = self.printer.get_reactor()
            self.check_timer = reactor.register_timer(self._run_checks, reactor.monotonic() + CHECK_RETRY)

    def reader_quiet(self, slot, reread=False):
        """Nothing turns on this slot's reader but the slot itself, and nothing is about to:
        a reread there needs only its spool turning. Two rereads of one reader go one by one."""
        other = slot ^ 1
        pair = self.pairs.get(reader_of(slot))
        if pair is not None and pair.timer is not None:
            return False
        waiting = self.feed_pending | self.eject_pending | (set() if reread else self.reread_pending)
        return not (other in self.busy or other in self.ejecting or other in self.feeding
                    or other in waiting or other == self.checking)

    def splitter_free(self, eventtime):
        """Nothing prints and no filament stands at the splitter sensor."""
        state = self.printer.lookup_object("print_stats").get_status(eventtime)["state"]
        if state in ("printing", "paused"):
            return False
        extras = self.printer.lookup_object("box_extras", None)
        # 0 when pressed: by a filament in the toolhead, or None if not known.
        return getattr(extras, "b_endstop_state", None) == 1

    def _check_ready(self, eventtime, slot=None):
        """This slot may go to the splitter now: it is free, no other spool goes there, the
        slot stands, and its neighbour on the reader stands too."""
        if self.checking not in (None, slot) or not self.splitter_free(eventtime):
            return False
        if slot is None:
            return True
        if slot in self.moving() or slot in self.feeding or slot in self.feed_pending \
                or slot in self.eject_pending or slot in self.reread_pending:
            return False
        return not self.stock_owns(slot) and self.reader_quiet(slot)

    def _run_checks(self, eventtime):
        reactor = self.printer.get_reactor()
        reactor.unregister_timer(self.check_timer)
        self.check_timer = None
        try:
            self.check_pending = [slot for slot in self.check_pending if slot not in self.checked
                                  and self.is_fed(slot) and self.printer.lookup_object(
                                      "box_stepper slot%d" % (slot,)).get_status(eventtime)["runout_button"] == 0]
            if self.checking is not None or self._estimated() < self.splitter_back_until + STOCK_MARGIN:
                pass
            elif self.splitter_free(eventtime):
                for slot in list(self.check_pending):
                    if self._check_ready(eventtime, slot):
                        self.check_pending.remove(slot)
                        # Not through the G-code queue: the screen's buttons stay free meanwhile.
                        gcode = self.printer.lookup_object("gcode")
                        self.splitter_check(slot, gcode.respond_info, gcode.respond_raw)
                        break
            else:
                # A print or a loaded toolhead holds the splitter: the check waits for it, the
                # reread does not, turning back from the safe point.
                for slot in self.check_pending:
                    if slot not in self.retried and slot not in self.reread_pending:
                        self.retried.add(slot)
                        self.reread_pending.add(slot)
                        self._start_reread_timer()
        except Exception:
            # An error here runs in a reactor timer and would shut the printer down.
            logging.exception("bambu_rfid: splitter check failed")
        if self.check_pending and self.check_timer is None:
            self.check_timer = reactor.register_timer(self._run_checks, reactor.monotonic() + CHECK_RETRY)
        return reactor.NEVER

    def _wait_until(self, end_time):
        reactor = self.printer.get_reactor()
        while self._estimated() < end_time + STOCK_MARGIN:
            reactor.pause(reactor.monotonic() + 0.05)

    def _run_to_splitter(self, slot, limit, speed, accel):
        """Turn the slot forward in short pieces until the splitter sensor is pressed, at most
        limit mm. Returns how far it had gone when the sensor was seen (None if never) and when
        the planned steps, a short stop included, end."""
        pair_feed = load_pair_feed()
        reactor = self.printer.get_reactor()
        extras = self.printer.lookup_object("box_extras")
        track, v, seen = pair_feed.Track(), speed, None
        while True:
            estimated = self._estimated()
            if getattr(extras, "b_endstop_state", None) == 0:
                seen = track.position(estimated)
                break
            if self.check_abort:
                break
            if track.planned >= limit - 0.01:
                if estimated >= track.end:
                    break
            elif track.end - estimated < SPLITTER_LEAD:
                joined = track.speed and track.end - estimated >= SPLITTER_JOIN
                begin = track.end if joined else None
                start_v = abs(track.speed) if joined else 0.
                ahead = v * SPLITTER_PIECE + (0. if joined else v * v / (2. * accel))
                rest = limit - track.planned
                if rest - ahead - v * v / (2. * accel) < 0.:
                    # The last piece stops exactly at the limit.
                    times = pair_feed.piece(rest, start_v, v, 0., accel)
                else:
                    times = pair_feed.piece(ahead, start_v, v, v, accel)
                track.add(*self.start_piece(slot, begin, times), times)
            reactor.pause(reactor.monotonic() + SPLITTER_POLL)
        if track.speed:
            # Pressed while turning: stop at the end of what is planned.
            v = abs(track.speed)
            times = pair_feed.piece(v * v / (2. * accel), v, v, 0., accel)
            track.add(*self.start_piece(slot, track.end, times), times)
        # Past the sensor when it was seen; else how far it went in all.
        self.overrun = track.planned - seen if seen is not None else track.planned
        return seen, track.end

    def cmd_splitter(self, gcmd):
        slot = gcmd.get_int("SLOT", minval=0, maxval=BOX_SLOTS - 1)
        self.splitter_check(slot, gcmd.respond_info, gcmd.respond_raw)

    def splitter_check(self, slot, respond_info, respond_raw):
        """Run a fed spool up to the splitter sensor, note how far it is, and take it back
        SPLITTER_PARK past the sensor. A spool without a tag is then read once more."""
        try:
            self.checking, self.check_abort = slot, False
            self._splitter_check(slot, respond_info, respond_raw)
        finally:
            self.checking, self.check_abort = None, False

    def _splitter_check(self, slot, respond_info, respond_raw):
        reactor = self.printer.get_reactor()
        now = reactor.monotonic()
        if not self._check_ready(now, slot) or not self.is_fed(slot) or self.printer.lookup_object(
                "box_stepper slot%d" % (slot,)).get_status(now)["runout_button"] != 0:
            respond_info("bambu_rfid: slot %d cannot go to the splitter now" % (slot,))
            return
        name, speeds = self.mode()
        # After a restart of Klipper it is not known how far the spool was fed: it still goes to
        # the sensor and stands at a known place after, but no splitter is noted for the slot.
        trusted = slot in self.lengths and slot not in self.guessed
        start = self.lengths.get(slot, self.safe_length(slot))
        known = self._splitters().get(slot)
        fast = known - start - SPLITTER_APPROACH if known is not None and trusted else 0.
        if fast > 20.:
            self._wait_until(self.start_sequence([slot], [(fast, speeds["feed"], speeds["accel"])], finish=False))
        else:
            fast = 0.
        travel, end_time = self._run_to_splitter(slot, SPLITTER_SEARCH, speeds["feed"], speeds["accel"])
        if travel is None and self.check_abort:
            # Ejected meanwhile: where it stands is known, the eject runs from there.
            self.lengths[slot] = start + fast + self.overrun
            self._finish_after([slot], end_time)
            logging.info("bambu_rfid: splitter check of slot%d stopped for its eject", slot)
            return
        error = None if travel is not None else "the sensor did not trigger"
        if travel is None:
            travel = SPLITTER_SEARCH
        reached = start + fast + travel
        if error is None:
            if trusted:
                self._splitters()[slot] = reached
                self._save_splitters()
            back = SPLITTER_PARK + self.overrun
            logging.info("bambu_rfid: slot%d splitter at %.0f mm%s, back %.0f", slot, reached,
                         "" if trusted else " (fed length not known, not noted)", back)
            respond_info("bambu_rfid: slot %d reached the splitter at %.0f mm" % (slot, reached))
        else:
            # Back to where it stood: something holds the filament, or the sensor did not see it.
            back = fast + travel
            logging.warning("bambu_rfid: slot%d did not reach the splitter: %s", slot, error)
            respond_raw("!! Filament of slot 1%s did not reach the splitter in %.0f mm, check the spool"
                        % ("ABCD"[slot], fast + travel))
        estimated = self._estimated()
        if back > 0.5:
            # Right after the stop, joined to it when it is still far enough ahead.
            begin = end_time if end_time - estimated >= SPLITTER_JOIN else None
            self.splitter_back_until = self.start_sequence([slot], [(-back, speeds["feed"], speeds["accel"])],
                                                           begin=begin)
        elif slot in self.busy:
            self._finish_after([slot], max(end_time, estimated))
        self.lengths[slot] = reached + (self.overrun if error is None else 0.) - back
        if error is None and not trusted and known is not None:
            # Its place is now known from the splitter noted before: the length counts from the
            # slot sensor again (7 Nov: after a restart 1A was taken to be 47 mm short of where it
            # was, and 1057 mm got noted as its splitter instead of 1104).
            self.lengths[slot] += known - reached
            self.guessed.discard(slot)
            logging.info("bambu_rfid: slot%d stands %.0f mm in, by the splitter noted before", slot,
                         self.lengths[slot])
        self.checked.add(slot)
        pair = self.pairs.get(reader_of(slot))
        if pair is not None:
            pair.moved_by(slot, self.lengths[slot] - start)
        if slot not in self.retried or (pair is not None and not pair.has_tag(slot)):
            # Read again from where it now stands, only it turning on its reader: a tag found
            # while the pair was fed together may be the neighbour's (1A got 1B's on 6 Nov).
            # A spool read while the toolhead held the splitter and found without a tag is read
            # once more: the neighbour's tag may have stood at the antenna then.
            self.retried.add(slot)
            self.reread_pending.add(slot)
            self._start_reread_timer()

    def release(self, slots):
        toolhead = self.printer.lookup_object("toolhead")
        enables = self.printer.lookup_object("stepper_enable")
        toolhead.dwell(SLOT_ENABLE_DELAY)
        print_time = toolhead.get_last_move_time()
        for slot in slots:
            self.motor_off(slot, print_time, enables)
        toolhead.dwell(SLOT_ENABLE_DELAY)
