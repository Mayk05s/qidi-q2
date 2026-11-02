# Feeding and ejecting spools in the QIDI Box without the stock queue.
# bambu_rfid only connects this package; tag reading stays there.
import logging
import re

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
EJECT_VELOCITY = 60.
EJECT_ACCEL = 200.
EJECT_BATCH_DELAY = 0.2
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
FEED_CATCH_VELOCITY = 15.
INSERT_POLL = 0.1


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
        self.eject_pending = set()
        self.eject_timer = None
        self.insert_timer = None
        self.trapqs = {}
        self.busy = set()
        self.ejecting = set()
        self.feeding = set()
        self.readers = {}
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
            plan.append((end_time, accel_t, cruise_t, position, axis_r, cruise_v, accel))
            end_time += accel_t + cruise_t + accel_t
            position += distance
        for slot in slots:
            name = "slot%d" % (slot,)
            stepper = force_move.lookup_stepper(name)
            if begin is None:
                enables.lookup_enable(name).motor_enable(estimated + SLOT_ENABLE_DELAY)
            trapq = self._trapq(slot)
            old_kinematics = stepper.set_stepper_kinematics(self._kinematics(slot))
            old_trapq = stepper.set_trapq(trapq)
            stepper.set_position((0., 0., 0.))
            for begin, accel_t, cruise_t, start_pos, axis_r, cruise_v, accel in plan:
                self.ffi_lib.trapq_append(trapq, begin, accel_t, cruise_t, accel_t,
                                          start_pos, 0., 0., axis_r, 0., 0., 0., cruise_v, accel)
            stepper.generate_steps(end_time)
            self.ffi_lib.trapq_finalize_moves(trapq, end_time + 99999.9, end_time + 99999.9)
            stepper.set_trapq(old_trapq)
            stepper.set_stepper_kinematics(old_kinematics)
            self.busy.add(slot)
        toolhead.note_mcu_movequeue_activity(end_time)
        if finish:
            self._finish_after(slots, end_time)
        return end_time

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

    def _start_batch(self, slots, distance):
        """Start the slots that are free; a slot still moving is left out with a warning."""
        free = [slot for slot in slots if slot not in self.busy]
        if free:
            self.start_move(free, distance, EJECT_VELOCITY, EJECT_ACCEL)
        return free

    def _eject_chunk(self, slots, begin, moved):
        """Eject in short pieces until the slot sensor sees the filament leave, whatever the
        length turns out to be. A short piece also frees the slot soon after that."""
        end_time = self.start_sequence(slots, [(-EJECT_CHUNK, EJECT_VELOCITY, EJECT_ACCEL)],
                                       begin=begin, finish=False)
        reactor = self.printer.get_reactor()
        estimated = self.printer.lookup_object("toolhead").mcu.estimated_print_time(reactor.monotonic())
        reactor.register_timer(lambda eventtime: self._eject_next(slots, end_time, moved + EJECT_CHUNK, eventtime),
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
            if free:
                self.ejecting.update(free)
                self._eject_chunk(free, None, 0.)
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
            if started:
                # A short move back and forth lets the gear catch the tip before the long feed.
                self.feeding.update(started)
                self.start_sequence(started, [
                    (FEED_CATCH_LENGTH, FEED_CATCH_VELOCITY, EJECT_ACCEL),
                    (-FEED_CATCH_LENGTH, FEED_CATCH_VELOCITY, EJECT_ACCEL),
                    (FEED_LENGTH, EJECT_VELOCITY, EJECT_ACCEL)])
                self._mark_ready(started)
            # A slot just ejected keeps its old plan until that ends, so it starts later.
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
