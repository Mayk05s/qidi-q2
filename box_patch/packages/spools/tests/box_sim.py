#!/usr/bin/env python3
"""The whole Box around the package's own code: BoxMover from box_feed, the PairDrivers of
pair_feed and pair_logic run unchanged against four modelled spools, two readers, the slot
sensors and the one splitter sensor every slot leads to.

The model gives what Klipper would: a reactor with timers and pause(), motors that follow the
moves the mover appends to its trapqs, and SAVE_VARIABLE. Each spool turns with slack as in the
emulator's model (test_pair_logic.World); its tag is at the antenna of its reader in the same
way. python3 box_sim.py prints the timeline of one run."""
import ast
import math
import pathlib
import random
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "rfid_core" / "printer"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "printer"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import bambu_rfid  # noqa: E402
import box_feed  # noqa: E402
import pair_feed  # noqa: E402
import pair_logic  # noqa: E402
import test_pair_logic as T  # noqa: E402

STEP = .02
LABELS = ("1A", "1B", "1C", "1D")
# The box being run, so that log lines can carry its time.
CURRENT = [None]


def calc_move_time(dist, speed, accel):
    """As Klipper's force_move.calc_move_time."""
    axis_r = 1. if dist >= 0. else -1.
    dist = abs(dist)
    if not accel or not dist:
        return axis_r, 0., dist / speed, speed
    if dist * accel < speed ** 2:
        speed = math.sqrt(dist * accel)
    accel_t = speed / accel
    cruise_t = (dist - accel_t * speed) / speed
    return axis_r, accel_t, cruise_t, speed


class Lib:
    """trapq calls of the mover, kept per trapq: they are the motor's moves."""

    def __init__(self):
        self.moves = {}

    def trapq_alloc(self):
        handle = object()
        self.moves[handle] = []
        return handle

    def trapq_free(self, handle):
        pass

    def cartesian_stepper_alloc(self, axis):
        return object()

    def free(self, handle):
        pass

    def trapq_append(self, trapq, begin, accel_t, cruise_t, decel_t, x, y, z, ax, ay, az, start_v, cruise_v, accel):
        self.moves[trapq].append((begin, (accel_t, cruise_t, decel_t, ax, start_v, cruise_v, accel)))

    def trapq_finalize_moves(self, *args):
        pass


class Ffi:
    def gc(self, value, free):
        return value


class Stepper:
    def __init__(self, name):
        self.name = name

    def get_name(self):
        return self.name

    def set_stepper_kinematics(self, sk):
        return None

    def set_trapq(self, trapq):
        return None

    def set_position(self, pos):
        pass

    def generate_steps(self, end):
        pass


class Status:
    def __init__(self, get):
        self.get = get

    def get_status(self, eventtime):
        return self.get()


class Reader:
    def __init__(self, box, pair):
        self.box, self.pair, self.name = box, pair, "card_reader_%d" % (pair + 1,)
        self.proxy = None

    def _read_slot_tag(self, timeout, uids, trays):
        box = self.box
        # Klipper stands still while the reader reads.
        box.t += box.read_time
        box.move_world()
        seen = [slot for slot in (2 * self.pair, 2 * self.pair + 1) if box.at_antenna(slot)]
        if len(seen) > 1:
            raise bambu_rfid.SeveralTags("two tags")
        if not seen:
            raise bambu_rfid.TagAbsent("no tag")
        slot = seen[0]
        if box.twins[slot]:
            # A spool with two tags, the second one never read in full: every other look finds it.
            box.twin_flip[slot] = not box.twin_flip.get(slot)
            if box.twin_flip[slot]:
                error = bambu_rfid.RfidError("reader error 0x04")
                error.uid = "7E1" + LABELS[slot]
                raise error
        return {"uid": "TAG" + LABELS[slot], "material": "PLA", "color": "ffffff"}

    def mapping_for_uid(self, uid):
        return None


class Gcmd:
    """A G-code command as the screen or the print file sends it."""

    def __init__(self, box, **params):
        self.box, self.params = box, params

    def get_int(self, name, default=None, **kwargs):
        return int(self.params.get(name, default))

    def respond_raw(self, text):
        self.box.note(text)

    def respond_info(self, text):
        self.box.note(text)


class Box:
    """Reactor, toolhead, gcode and every Box object the package looks up."""
    NEVER = 9e99

    def __init__(self, angles, has=(True,) * 4, splitters=(1104., 1127., 1122., 1119.),
                 pushes=(30., 30., 30., 30.), slack=(0., 300.), seed=0, read_time=0.3,
                 slots_file=None, verbose=False, twins=(False,) * 4):
        self.t = 0.
        CURRENT[0] = self
        self.read_time = read_time
        self.twins, self.twin_flip = twins, {}
        self.verbose = verbose
        rnd = random.Random(seed)
        self.worlds = [T.World(angles[0], angles[1], slack=slack, has=has[:2], seed=rnd.randrange(1 << 30)),
                       T.World(angles[2], angles[3], slack=slack, has=has[2:], seed=rnd.randrange(1 << 30))]
        self.has = has
        # Where the splitter sensor is, counted from the slot sensor, and how far the hand pushes
        # the tip past the slot sensor when putting a spool in.
        self.splitters, self.pushes = splitters, pushes
        self.inserted = [False] * 4
        self.saved = {"auto_read_rfid": 0}
        self.state = "standby"
        # The slot whose filament is in the toolhead, how fast the print pulls it, and whether the
        # command being run comes from the print file.
        self.head = None
        self.head_speed = 0.
        self.from_sd = False
        self.file_pos = 0
        self.tool_changes = []
        self.pauses = 0
        self.timers = []
        self.running = set()
        self.lib = Lib()
        self.done_motion = {}
        self.events = []
        self.violations = []
        self.last_pos = [0.] * 4
        self.mover = box_feed.BoxMover(self, (Ffi(), self.lib))
        self.mover.calc_move_time = calc_move_time
        self.mover.mode_path = str(pathlib.Path(slots_file or "/nonexistent").with_name("box_feed.json"))
        self.mover.slots_path = slots_file or "/nonexistent/box_feed_slots.json"
        self.mover.splitter = {}
        for pair in (0, 1):
            reader = Reader(self, pair)
            self.mover.readers[reader.name] = reader
        # When each slot's splitter check ended: the stop and the way back follow it.
        self.check_done = {}
        check = self.mover.splitter_check

        def splitter_check(slot, *args):
            try:
                return check(slot, *args)
            finally:
                self.check_done[slot] = self.t
        self.mover.splitter_check = splitter_check
        self.mover._save_splitters = lambda: None
        self.mover.start_insert_watch()
        # Klipper runs a moment before anything is put in.
        self.run_until(0.5)

    # Klipper's printer object
    def get_reactor(self):
        return self

    def get_start_args(self):
        return {"config_file": "/nonexistent/printer.cfg"}

    def lookup_object(self, name, default=None):
        if name in ("toolhead", "stepper_enable", "gcode", "force_move"):
            return self
        if name.startswith("box_stepper slot"):
            slot = int(name[-1])
            return Status(lambda: {"runout_button": 0 if self.present(slot) else 1})
        if name == "box_extras":
            return self
        if name == "print_stats":
            return Status(lambda: {"state": self.state})
        if name == "save_variables":
            return self
        if name.startswith("filament_switch_sensor"):
            return Status(lambda: {"filament_detected": self.head is not None})
        if name == "virtual_sdcard":
            return self
        if name == "gcode_move":
            return Status(lambda: {"speed_factor": 1.})
        return default

    # save_variables
    @property
    def allVariables(self):
        return self.saved

    # box_extras
    def get_status(self, eventtime):
        return {"loaded_slot": "slot16" if self.head is None else "slot%d" % (self.head,)}

    @property
    def b_endstop_state(self):
        pressed = [slot for slot in range(4) if self.inserted[slot] and self.real(slot) >= self.splitters[slot]]
        return 0 if pressed or self.head is not None else 1

    # virtual_sdcard
    def is_cmd_from_sd(self):
        return self.from_sd

    def get_file_position(self):
        return self.file_pos + 1

    @property
    def file_position(self):
        return self.file_pos

    def set_file_position(self, pos):
        self.file_pos = pos

    # reactor
    def monotonic(self):
        return self.t

    def register_timer(self, callback, when):
        timer = [callback, when]
        self.timers.append(timer)
        return timer

    def unregister_timer(self, timer):
        if timer in self.timers:
            self.timers.remove(timer)

    def pause(self, until):
        # Other timers run meanwhile, as greenlets do in Klipper.
        self.run_until(until)

    # toolhead
    @property
    def mcu(self):
        return self

    @property
    def print_time(self):
        return self.t

    def estimated_print_time(self, eventtime):
        return eventtime

    def get_last_move_time(self):
        return self.t

    def note_mcu_movequeue_activity(self, time):
        pass

    # stepper_enable and force_move
    def lookup_enable(self, name):
        return self

    def motor_enable(self, print_time):
        pass

    def motor_disable(self, print_time):
        pass

    def lookup_stepper(self, name):
        return Stepper(name)

    # gcode
    def run_script(self, script):
        found = re.fullmatch(r"SAVE_VARIABLE VARIABLE=(\w+) VALUE=(.*)", script)
        if found:
            value = found.group(2).strip('"')
            try:
                self.saved[found.group(1)] = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                self.saved[found.group(1)] = value

    def run_script_from_command(self, script):
        if script == "PAUSE":
            self.state = "paused"
            self.pauses += 1
            self.note("print paused")

    def respond_info(self, text):
        self.note(text)

    def respond_raw(self, text):
        self.note(text)

    def note(self, text):
        self.events.append((round(self.t, 2), text))
        if self.verbose:
            print("%7.2f %s" % (self.t, text))

    # the world
    def displacement(self, slot):
        trapq = self.mover.trapqs.get(slot)
        if trapq is None:
            return 0.
        return sum(pair_feed.piece_position(begin, times, self.t) for begin, times in self.lib.moves[trapq])

    def world_state(self, slot):
        return self.worlds[slot // 2].state["AB"[slot % 2]]

    def real(self, slot):
        return self.world_state(slot)["real"]

    def present(self, slot):
        return self.inserted[slot] and self.real(slot) > 0.

    def at_antenna(self, slot):
        return self.inserted[slot] and self.has[slot] and self.worlds[slot // 2]._zone(self.world_state(slot))

    def move_world(self):
        """Turn each spool by what its motor did since the last look, with slack as World does."""
        for slot in range(4):
            pos = self.displacement(slot)
            d, self.last_pos[slot] = pos - self.last_pos[slot], pos
            if not self.inserted[slot]:
                continue
            st = self.world_state(slot)
            if slot == self.head:
                # Only the stock code and the extruder move the filament in the toolhead.
                if abs(d) > 1e-6:
                    self.violations.append((round(self.t, 2), LABELS[slot], "turned in the toolhead", round(d, 1)))
                if self.state == "printing":
                    m = self.head_speed * STEP
                    st["real"] += m
                    st["angle"] += m / pair_logic.TURN * 360.
                continue
            if d > 0:
                g = min(st["slip"], d)
                st["slip"] -= g
                m = d - g
                st["real"] += m
                u = min(st["slack"], m)
                st["slack"] -= u
                st["angle"] += (m - u) / pair_logic.TURN * 360.
            elif d < 0 and st["slip"] <= 0:
                st["real"] += d
                st["angle"] += d / pair_logic.TURN * 360.
            checked = self.mover.checking == slot or self.t < self.check_done.get(slot, -99.) + 5.
            if st["real"] > self.splitters[slot] + 0.5 and not checked:
                self.violations.append((round(self.t, 2), LABELS[slot], "past the splitter", round(st["real"])))
            if st["real"] > self.splitters[slot] + 60.:
                self.violations.append((round(self.t, 2), LABELS[slot], "far into the splitter", round(st["real"])))

    def insert(self, slot):
        self.inserted[slot] = True
        st = self.world_state(slot)
        st["real"] = self.pushes[slot]
        self.last_pos[slot] = self.displacement(slot)
        self.note("put %s in" % (LABELS[slot],))

    def eject(self, slot):
        self.mover.request_eject(slot)

    def screen_eject(self, slot):
        """The screen's Unload of a slot, as the stock code would get it."""
        self.mover.stock_eject = lambda gcmd: self.note("stock unload of %s" % (LABELS[slot],))
        self.mover.cmd_eject(Gcmd(self, SLOT=slot))

    def pull_out(self, slot):
        """The hand takes an ejected spool out."""
        self.inserted[slot] = False
        self.note("took %s out" % (LABELS[slot],))

    def load_head(self, slot, tag=True):
        """A spool fed and loaded into the toolhead before the run: its filament stands at the
        splitter sensor and on to the nozzle."""
        if self.inserted[slot]:
            # Fed by us before: the stock load pushes it on into the toolhead.
            self.world_state(slot)["real"] = self.splitters[slot] + 400.
            self.head = slot
            self.note("%s loaded in the toolhead" % (LABELS[slot],))
            return
        self.inserted[slot] = True
        self.world_state(slot)["real"] = self.splitters[slot] + 400.
        self.last_pos[slot] = self.displacement(slot)
        self.saved["slot%d" % (slot,)] = 1
        self.saved["value_t%d" % (slot,)] = "slot%d" % (slot,)
        if tag and self.has[slot]:
            self.saved["bambu_tag_slot%d" % (slot,)] = {"uid": "TAG" + LABELS[slot]}
        self.head = slot
        self.note("%s loaded in the toolhead" % (LABELS[slot],))

    def fed_before(self, slot, real, noted=True):
        """A spool fed before Klipper started: ready for printing, its filament real mm in, the
        splitter of its slot noted in the file, and its tag recorded."""
        self.inserted[slot] = True
        self.world_state(slot)["real"] = real
        self.last_pos[slot] = self.displacement(slot)
        self.saved["slot%d" % (slot,)] = 1
        if self.mover.seen is not None:
            # In its slot when Klipper started: not a spool put in.
            self.mover.seen[slot] = True
        if noted:
            self.mover.splitter[slot] = self.splitters[slot]

    def screen_reread(self, slot):
        """The screen's Reread of a slot."""
        self.mover.stock_reread = lambda gcmd: self.note("stock reread of %s" % (LABELS[slot],))
        self.mover.cmd_reread(Gcmd(self, SLOT=slot))

    def start_print(self, speed=1.):
        """A print from the slot in the toolhead: the stock code syncs it to the extruder."""
        self.state, self.head_speed = "printing", speed
        self.mover.synced.add(self.head)
        self.note("print started from %s" % (LABELS[self.head],))

    def resume(self):
        self.state = "printing"
        self.note("print resumed")

    def end_print(self):
        self.state = "standby"
        self.mover.synced.discard(self.head)
        self.note("print ended")

    def unload_head(self):
        """The stock unload takes the filament out of the toolhead and the slot."""
        slot, self.head = self.head, None
        self.inserted[slot] = False
        self.saved["slot%d" % (slot,)] = 0
        self.note("%s unloaded from the toolhead" % (LABELS[slot],))

    def tool_change(self, slot):
        """T_MANAGER from the print file: the stock change runs, or the print pauses."""
        for t in range(4):
            self.saved.setdefault("value_t%d" % (t,), "slot%d" % (t,))
        changed = []
        self.mover.stock_tool = lambda gcmd: changed.append(slot)
        self.from_sd = True
        try:
            self.mover.cmd_tool(Gcmd(self, T=slot))
        finally:
            self.from_sd = False
        self.tool_changes.append((round(self.t, 2), LABELS[slot], bool(changed)))
        self.note("tool change to %s: %s" % (LABELS[slot], "done" if changed else "paused"))
        return bool(changed)

    def run_until(self, until):
        while self.t < until:
            self.t += STEP
            self.move_world()
            for timer in sorted(self.timers, key=lambda timer: timer[1]):
                if timer not in self.timers or id(timer) in self.running or timer[1] > self.t:
                    continue
                self.running.add(id(timer))
                try:
                    when = timer[0](self.t)
                finally:
                    self.running.discard(id(timer))
                if timer in self.timers:
                    if when is None or when >= self.NEVER:
                        self.timers.remove(timer)
                    else:
                        timer[1] = when

    def idle(self):
        m = self.mover
        return not (m.moving() or m.feed_pending or m.eject_pending or m.reread_pending or m.check_pending
                    or m.feeding or m.checking is not None
                    or any(pair.timer is not None for pair in m.pairs.values()))

    def settle(self, limit=600.):
        """Run until every spool put in has been fed, checked and read again."""
        end = self.t + limit
        while self.t < end:
            self.run_until(self.t + 1.)
            if self.idle() and all(slot in self.mover.checked or not self.inserted[slot] or slot == self.head
                                   for slot in range(4)):
                return True
        return False

    def quiet(self):
        """Nothing turns or waits to turn but the splitter checks, which wait for the splitter."""
        m = self.mover
        return not (m.moving() or m.feed_pending or m.eject_pending or m.reread_pending
                    or m.feeding or m.checking is not None
                    or any(pair.timer is not None for pair in m.pairs.values()))

    def settle_busy(self, limit=600.):
        """With the splitter held by the toolhead: run until every spool put in has been fed and
        read again from its safe point, its check left waiting."""
        end = self.t + limit
        while self.t < end:
            self.run_until(self.t + 1.)
            if self.quiet() and all(slot in self.mover.retried or not self.inserted[slot] or slot == self.head
                                    for slot in range(4)):
                return True
        return False

    def slot_tags(self):
        out = []
        for slot in range(4):
            record = self.saved.get("bambu_tag_slot%d" % (slot,))
            out.append(record.get("uid") if isinstance(record, dict) else None)
        return tuple(out)

    def want(self):
        return tuple(("TAG" + LABELS[slot]) if self.inserted[slot] and self.has[slot] else None for slot in range(4))

    def wrong_tags(self):
        """Slots holding a tag that is not their spool's."""
        got, want = self.slot_tags(), self.want()
        return [LABELS[slot] for slot in range(4) if got[slot] is not None and got[slot] != want[slot]]


def run(angles, order=(0, 1, 2, 3), gap=0.5, verbose=False, **kwargs):
    box = Box(angles, verbose=verbose, **kwargs)
    for slot in order:
        box.insert(slot)
        box.run_until(box.t + gap)
    finished = box.settle()
    return box, finished


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    box, finished = run((40., 200., 300., 120.), has=(True, True, True, False), verbose=True)
    print("finished", finished, "at %.0f s" % (box.t,))
    print("tags", box.slot_tags(), "want", box.want())
    print("splitters", box.mover._splitters())
    print("violations", box.violations[:5])
