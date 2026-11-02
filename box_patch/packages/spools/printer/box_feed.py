# Feeding and ejecting spools in the QIDI Box without the stock queue.
# bambu_rfid only connects this package; tag reading stays there.
import json
import logging
import os
import re
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
# The stock check reaches the joint sensor about 1280 mm after insertion and parks 260 mm back.
FEED_LENGTH = 1000.
FEED_RETRY = 0.5
FEED_START_DELAY = 2.
FEED_CATCH_LENGTH = 30.
# Back by less than forward: on 2 Nov a spool pushed in only just past the sensor came out of it
# on a full 30 mm back move and was lost.
FEED_CATCH_BACK = 10.
FEED_CATCH_VELOCITY = 15.
INSERT_POLL = 0.1


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
                enables.lookup_enable("slot%d" % (slot,)).motor_disable(print_time)
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
            fast = self.lengths.pop(slot, FEED_LENGTH) - EJECT_SLOW_ZONE
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
            self.stock_reread(gcmd)
            return
        if slot in self.busy or slot in self.ejecting:
            gcmd.respond_info("bambu_rfid: slot %d is moving, read it again when it stops" % (slot,))
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
                self.reread_pending.discard(slot)
                if slot not in self.busy and slot not in self.ejecting and self.reread(slot):
                    self.feeding.add(slot)
            if self.reread_pending:
                self._start_reread_timer()
        except Exception:
            # An error here runs in a reactor timer and would shut the printer down.
            logging.exception("bambu_rfid: reread of %s failed", sorted(self.reread_pending))
            self.reread_pending.clear()
        return reactor.NEVER

    def _eject_ourselves(self, slot):
        now = self.printer.get_reactor().monotonic()
        stats = self.printer.lookup_object("print_stats").get_status(now)
        if stats["state"] in ("printing", "paused") or not 0 <= slot < BOX_SLOTS:
            return False
        sensor = self.printer.lookup_object("filament_switch_sensor filament_switch_sensor")
        loaded = self.printer.lookup_object("box_extras").get_status(now).get("loaded_slot")
        return not (sensor.get_status(now)["filament_detected"] and loaded == "slot%d" % (slot,))

    def cmd_eject(self, gcmd):
        slot = gcmd.get_int("SLOT")
        if not self._eject_ourselves(slot):
            self.stock_eject(gcmd)
            return
        self.request_eject(slot)
        gcmd.respond_info("bambu_rfid: ejecting slot %d" % (slot,))

    def request_eject(self, slot):
        """Slots asked for within EJECT_BATCH_DELAY start at the same moment."""
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
            stepper = self.printer.lookup_object("box_stepper slot%d" % (slot,))
            self.stock_slot_load[slot] = stepper.slot_load
            stepper.slot_load = self._slot_load_wrapper(slot, stepper.slot_load)

    def _slot_load_wrapper(self, slot, stock):
        def slot_load(*args, **kwargs):
            reactor = self.printer.get_reactor()
            if not self._insert_watch_on(reactor.monotonic()):
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

    def start_insert_watch(self):
        """Watch the slot sensors to stop an eject as soon as the filament has left."""
        if self.insert_timer is None:
            reactor = self.printer.get_reactor()
            self.insert_timer = reactor.register_timer(self._watch_inserts, reactor.monotonic() + INSERT_POLL)

    def _insert_watch_on(self, now):
        # Only while the stock read on insertion is off, so that only one of us moves the filament.
        saved = self.printer.lookup_object("save_variables").allVariables
        state = self.printer.lookup_object("print_stats").get_status(now)["state"]
        return saved.get("auto_read_rfid") == 0 and state not in ("printing", "paused")

    def _watch_inserts(self, eventtime):
        try:
            present = {}
            for slot in range(BOX_SLOTS):
                stepper = self.printer.lookup_object("box_stepper slot%d" % (slot,))
                present[slot] = stepper.get_status(eventtime)["runout_button"] == 0
            self._stop_finished_ejects(present, eventtime)
        except Exception:
            # An error here runs in a reactor timer and would shut the printer down.
            logging.exception("bambu_rfid: slot watch failed")
        return eventtime + INSERT_POLL

    def _stop_finished_ejects(self, present, eventtime):
        """Once the filament has left the sensor the motor has nothing to move. The planned steps
        stay queued, so the slot counts as busy until they end; only the driver is switched off."""
        for slot in sorted(self.ejecting):
            if present.get(slot):
                continue
            self.ejecting.discard(slot)
            toolhead = self.printer.lookup_object("toolhead")
            enables = self.printer.lookup_object("stepper_enable")
            print_time = toolhead.mcu.estimated_print_time(eventtime) + SLOT_ENABLE_DELAY
            enables.lookup_enable("slot%d" % (slot,)).motor_disable(print_time)
            toolhead.note_mcu_movequeue_activity(print_time)

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

    def release(self, slots):
        toolhead = self.printer.lookup_object("toolhead")
        enables = self.printer.lookup_object("stepper_enable")
        toolhead.dwell(SLOT_ENABLE_DELAY)
        print_time = toolhead.get_last_move_time()
        for slot in slots:
            enables.lookup_enable("slot%d" % (slot,)).motor_disable(print_time)
        toolhead.dwell(SLOT_ENABLE_DELAY)
