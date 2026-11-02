#!/usr/bin/env python3
import pathlib
import sys

# The tested files are those copied to the printer, in ../printer; bambu_rfid.py comes from the
# rfid_core package. On the printer they all sit in klippy/extras.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "rfid_core" / "printer"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "printer"))
import json
import os
import tempfile
import time
import unittest

import box_feed as b


class FakeLib:
    def __init__(self):
        self.appends = []
        self.trapq_free = self.free = object()

    def trapq_alloc(self):
        return "trapq"

    def cartesian_stepper_alloc(self, axis):
        return object()

    def trapq_append(self, *args):
        self.appends.append(args)

    def trapq_finalize_moves(self, *args):
        pass


class FakeFfi:
    def gc(self, value, free):
        return value


class FakeStepper:
    def __init__(self, name):
        self.name, self.sk, self.trapq, self.steps, self.sks = name, "old_sk", "old_trapq", [], []

    def get_name(self):
        return self.name

    def set_stepper_kinematics(self, sk):
        old, self.sk = self.sk, sk
        self.sks.append(sk)
        return old

    def set_trapq(self, trapq):
        old, self.trapq = self.trapq, trapq
        return old

    def set_position(self, pos):
        pass

    def generate_steps(self, end):
        self.steps.append(end)


class FakeEnable:
    # Enable and disable are planned by time, so the calls are all that matter here.
    def __init__(self):
        self.calls = []

    def motor_enable(self, time):
        self.calls.append("on")

    def motor_disable(self, time):
        self.calls.append("off")


class FakeBoxPrinter:
    def __init__(self):
        self.steppers = {"slot%d" % i: FakeStepper("slot%d" % i) for i in range(4)}
        self.enable_of = {name: FakeEnable() for name in self.steppers}

    def lookup_object(self, name, default=None):
        return self

    def lookup_stepper(self, name):
        return self.steppers[name]

    def lookup_enable(self, name):
        return self.enable_of[name]

    def get_last_move_time(self):
        return 5.

    def dwell(self, delay):
        pass

    def flush_step_generation(self):
        pass

    def note_mcu_movequeue_activity(self, time):
        pass


class BoxMoverTests(unittest.TestCase):
    def setUp(self):
        self.printer = FakeBoxPrinter()
        self.lib = FakeLib()
        self.mover = b.BoxMover(self.printer, (FakeFfi(), self.lib))

    def test_slots_are_checked(self):
        self.assertEqual(b.parse_slots("0,2"), [0, 2])
        for bad in ("", "0,0", "4", "a"):
            with self.assertRaises(ValueError):
                b.parse_slots(bad)

    def test_motors_share_one_trajectory_and_are_restored(self):
        self.mover.move([0, 2], -100., 40., 200., lambda d, s, a: (-1., .2, 2.3, 40.))
        self.assertEqual(len(self.lib.appends), 1)
        one, other = self.printer.steppers["slot0"], self.printer.steppers["slot2"]
        self.assertEqual(one.steps, [7.7])
        self.assertEqual(other.steps, [7.7])
        self.assertIsNot(one.sks[0], other.sks[0])
        self.assertEqual((one.trapq, one.sk), ("old_trapq", "old_sk"))
        self.assertEqual(self.printer.steppers["slot1"].steps, [])

    def test_release_switches_only_the_moved_motors_off(self):
        self.mover.release([0, 2])
        self.assertEqual(self.printer.enable_of["slot0"].calls, ["off"])
        self.assertEqual(self.printer.enable_of["slot1"].calls, [])


class EjectTests(unittest.TestCase):
    class Reactor:
        NEVER = 1e99

        def monotonic(self):
            return 10.

        def register_timer(self, callback, when):
            self.callback, self.when = callback, when
            return "timer"

        def unregister_timer(self, timer):
            self.gone = timer

    class Status:
        def __init__(self, status):
            self.status = status

        def get_status(self, now):
            return self.status

    def setUp(self):
        self.printer = FakeBoxPrinter()
        self.reactor = self.Reactor()
        self.state, self.loaded, self.head = "standby", "slot1", True
        self.saved = {}

        def lookup(name, default=None):
            if name == "save_variables":
                class Saved:
                    allVariables = self.saved
                return Saved
            if name == "print_stats":
                return self.Status({"state": self.state})
            if name == "box_extras":
                return self.Status({"loaded_slot": self.loaded})
            if name.startswith("filament_switch_sensor"):
                return self.Status({"filament_detected": self.head})
            return self.printer
        self.printer.lookup_object = lookup
        self.printer.get_reactor = lambda: self.reactor
        self.mover = b.BoxMover(self.printer, (FakeFfi(), FakeLib()))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.mover.mode_path = os.path.join(folder.name, b.MODE_FILE)

    def set_mode(self, name):
        with open(self.mover.mode_path, "w") as f:
            f.write(json.dumps({"mode": name}))

    def test_only_a_free_slot_is_ours(self):
        self.assertTrue(self.mover._eject_ourselves(0))
        self.assertFalse(self.mover._eject_ourselves(1))
        self.assertFalse(self.mover._eject_ourselves(16))
        self.head = False
        self.assertTrue(self.mover._eject_ourselves(1))
        self.state = "printing"
        self.assertFalse(self.mover._eject_ourselves(0))

    def test_slots_asked_together_go_together(self):
        self.mover.request_eject(2)
        self.mover.request_eject(0)
        self.assertEqual(self.reactor.when, 10. + b.EJECT_BATCH_DELAY)
        moved = []
        self.mover._eject_chunk = lambda slots, begin, done, *fast: moved.append((slots, begin, done) + fast)
        self.reactor.callback(10.)
        # Nothing known about these spools: the usual fed length goes fast.
        self.assertEqual(moved, [([0, 2], None, 0., b.FEED_LENGTH - b.EJECT_SLOW_ZONE, b.MODES["normal"]["eject"])])
        self.assertEqual(self.mover.ejecting, {0, 2})
        self.assertIsNone(self.mover.eject_timer)

    def test_eject_goes_on_until_the_sensor_says_so(self):
        chunks = []
        self.mover._eject_chunk = lambda slots, begin, done: chunks.append((slots, begin, done))
        finished = []
        self.mover._finish_after = lambda slots, end: finished.append(slots)
        self.mover.ejecting.update({0, 2})
        self.mover._eject_next([0, 2], 50., 200., 49.)
        self.assertEqual(chunks, [([0, 2], 50., 200.)])
        self.assertEqual(finished, [])
        self.mover.ejecting.discard(0)
        self.mover._eject_next([0, 2], 53., 400., 52.)
        self.assertEqual(chunks[-1], ([2], 53., 400.))
        self.assertEqual(finished, [[0]])

    def test_eject_gives_up_at_the_limit(self):
        self.mover._eject_chunk = lambda *args: self.fail("no more pieces expected")
        finished = []
        self.mover._finish_after = lambda slots, end: finished.append(slots)
        self.mover.ejecting.add(1)
        said = []
        self.printer.respond_raw = said.append
        self.mover._eject_next([1], 50., b.EJECT_MAX_LENGTH, 49.)
        self.assertEqual(finished, [[1]])
        self.assertTrue(said[0].startswith("!! Eject of 1B"))
        self.assertEqual(self.mover.ejecting, set())

    def test_a_sequence_can_continue_without_enabling_again(self):
        self.start_real()
        self.mover.calc_move_time = lambda d, s, a: (-1., .5, 2., 60.)
        end = self.mover.start_sequence([0], [(-200., 60., 200.)], begin=120., finish=False)
        self.assertEqual(end, 123.)
        self.assertEqual(self.printer.enable_of["slot0"].calls, [])
        self.assertEqual(self.mover.ffi_lib.appends[0][1], 120.)

    def test_eject_runs_fast_over_the_known_length(self):
        self.set_mode("quiet")
        self.mover.lengths = {0: 1040., 2: 100.}
        moved = []
        self.mover._eject_chunk = lambda slots, begin, done, *fast: moved.append((slots,) + fast)
        self.mover._eject_start([0, 2])
        # A spool fed less than the slow zone goes slowly all the way.
        self.assertEqual(moved, [([2],), ([0], 1040. - b.EJECT_SLOW_ZONE, b.MODES["quiet"]["eject"])])
        self.assertEqual(self.mover.lengths, {})

    def test_mode_falls_back_to_normal(self):
        self.assertEqual(self.mover.mode()[0], "normal")
        self.set_mode("loud")
        self.assertEqual(self.mover.mode()[0], "normal")
        with open(self.mover.mode_path, "w") as f:
            f.write("{broken")
        self.assertEqual(self.mover.mode()[0], "normal")
        self.set_mode("fast")
        self.assertEqual(self.mover.mode(), ("fast", b.MODES["fast"]))

    def write_settings(self, settings):
        with open(self.mover.mode_path, "w") as f:
            f.write(json.dumps(settings))

    def at(self, hour, minute=0):
        self.mover.clock = lambda: time.struct_time((2026, 11, 2, hour, minute, 0, 0, 306, 0))

    def test_custom_values_are_kept_within_safe_limits(self):
        self.write_settings({"mode": "custom", "custom": {"feed": 45, "eject": 500, "eject_slow": "x",
                                                          "accel": 10, "motors": "pairs"}})
        name, speeds = self.mover.mode()
        self.assertEqual(name, "custom")
        self.assertEqual(speeds, {"feed": 45., "eject": 100., "eject_slow": b.MODES["normal"]["eject_slow"],
                                  "accel": 50., "motors": "pairs"})

    def test_night_time_is_quiet_across_midnight(self):
        self.write_settings({"mode": "fast", "night": {"on": True, "from": "23:00", "to": "07:00"}})
        self.at(23, 30)
        self.assertEqual(self.mover.mode(), ("quiet (night)", b.MODES["quiet"]))
        self.at(6, 59)
        self.assertEqual(self.mover.mode()[0], "quiet (night)")
        self.at(7, 0)
        self.assertEqual(self.mover.mode()[0], "fast")
        self.write_settings({"mode": "fast", "night": {"on": False, "from": "23:00", "to": "07:00"}})
        self.at(23, 30)
        self.assertEqual(self.mover.mode()[0], "fast")

    def test_qidi_silent_print_mode_is_followed_when_asked(self):
        factor = {"speed_factor": 0.5}
        previous = self.printer.lookup_object
        self.printer.lookup_object = lambda name, default=None: \
            self.Status(factor) if name == "gcode_move" else previous(name, default)
        self.at(12)
        self.write_settings({"mode": "normal", "night": {"follow_printer": True}})
        self.assertEqual(self.mover.mode()[0], "quiet (printer)")
        factor["speed_factor"] = 1.0
        self.assertEqual(self.mover.mode()[0], "normal")
        factor["speed_factor"] = 0.5
        self.write_settings({"mode": "normal", "night": {"follow_printer": False}})
        self.assertEqual(self.mover.mode()[0], "normal")

    def test_mode_command_keeps_the_panel_settings(self):
        self.write_settings({"mode": "custom", "custom": {"feed": 45}, "night": {"on": True}})
        said = []

        class Gcmd:
            def get(self, name, default=None):
                return "Quiet"

            def respond_info(self, text):
                said.append(text)
        self.mover.cmd_mode(Gcmd())
        with open(self.mover.mode_path) as f:
            self.assertEqual(json.load(f), {"mode": "quiet", "custom": {"feed": 45}, "night": {"on": True}})

    def test_motor_limit_holds_slots_back(self):
        self.write_settings({"mode": "custom", "custom": {"motors": "2"}})
        self.mover.busy.add(0)
        self.assertEqual(self.mover.admit([1, 2, 3]), [1])
        self.write_settings({"mode": "custom", "custom": {"motors": "pairs"}})
        # One motor per reader: 1A and 1B share one, 1C and 1D the other.
        self.assertEqual(self.mover.admit([1, 2, 3]), [2])
        self.mover.busy.clear()
        self.assertEqual(self.mover.admit([0, 1, 2, 3]), [0, 2])
        self.write_settings({"mode": "custom", "custom": {"motors": "1"}})
        self.assertEqual(self.mover.admit([0, 1, 2, 3]), [0])
        self.write_settings({"mode": "normal"})
        self.assertEqual(self.mover.admit([0, 1, 2, 3]), [0, 1, 2, 3])

    def test_eject_held_back_by_the_limit_goes_later(self):
        self.write_settings({"mode": "quiet"})
        started = []
        self.mover._eject_start = started.append
        for slot in (0, 1, 2):
            self.mover.request_eject(slot)
        self.reactor.callback(10.)
        self.assertEqual(started, [[0, 1]])
        self.assertEqual(self.mover.eject_pending, {2})
        self.assertEqual(self.reactor.when, 10. + b.LIMIT_RETRY)
        self.mover.ejecting.discard(0)
        self.reactor.callback(10.5)
        self.assertEqual(started, [[0, 1], [2]])
        self.assertEqual(self.mover.eject_pending, set())

    def test_slot_still_moving_is_left_out(self):
        self.mover.busy.add(0)
        moved = []
        self.mover.start_move = lambda slots, distance, *rest: moved.append(slots)
        self.assertEqual(self.mover._start_batch([0, 2], -10.), [2])
        self.assertEqual(moved, [[2]])

    def test_failed_eject_does_not_raise_in_a_timer(self):
        self.mover.request_eject(0)

        def fail(*args):
            raise ValueError("bus")
        self.mover.start_move = fail
        self.assertEqual(self.reactor.callback(10.), self.reactor.NEVER)

    def start_real(self):
        class Mcu:
            def estimated_print_time(self, now):
                return 100.
        self.printer.mcu = Mcu()
        self.mover.calc_move_time = lambda d, s, a: (-1., .2, 2.3, 40.)

    def test_a_move_starts_without_waiting_for_the_toolhead(self):
        self.start_real()
        self.printer.get_last_move_time = None
        self.printer.dwell = None
        self.mover.start_move([0, 2], -100., 40., 200.)
        one, other = self.printer.steppers["slot0"], self.printer.steppers["slot2"]
        self.assertEqual(one.steps, [100. + b.SLOT_START_DELAY + 2.7])
        self.assertEqual(other.steps, one.steps)
        self.assertEqual(self.printer.enable_of["slot0"].calls, ["on"])
        self.assertEqual(self.mover.busy, {0, 2})
        self.assertEqual((one.trapq, one.sk), ("old_trapq", "old_sk"))
        self.assertEqual(self.mover.ffi_lib.appends[0][1], 100. + b.SLOT_START_DELAY)
        self.assertEqual(len(self.mover.ffi_lib.appends), 2)

    def test_motors_are_released_when_the_move_ends(self):
        self.start_real()
        self.mover.start_move([0], -100., 40., 200.)
        self.reactor.callback(120.)
        self.assertEqual(self.printer.enable_of["slot0"].calls, ["on", "off"])
        self.assertEqual(self.mover.busy, set())


class InsertWatchTests(EjectTests):
    def setUp(self):
        EjectTests.setUp(self)
        self.runout = {slot: 1 for slot in range(4)}
        self.saved = {"auto_read_rfid": 0}
        previous = self.printer.lookup_object

        def lookup(name, default=None):
            if name.startswith("box_stepper slot"):
                return self.Status({"runout_button": self.runout[int(name[-1])]})
            if name == "save_variables":
                class Saved:
                    allVariables = self.saved
                return Saved
            return previous(name, default)
        self.printer.lookup_object = lookup

    def wrapper(self, slot=1):
        self.stock_calls = []
        return self.mover._slot_load_wrapper(slot, lambda: self.stock_calls.append("stock"))

    def present(self, *slots):
        for slot in slots:
            self.runout[slot] = 0

    def test_inserted_spools_go_together_without_the_stock_check(self):
        self.present(1, 3)
        self.wrapper(1)()
        self.wrapper(3)()
        self.assertEqual(self.stock_calls, [])
        self.assertEqual(self.reactor.when, 10. + b.FEED_START_DELAY)
        moved, inserted = [], []
        self.mover.start_sequence = lambda slots, moves, finish: moved.append((slots, moves, finish)) or 13.
        self.mover._pair_of = lambda slot: self.Pair(inserted)
        self.mover._run_feeds(12.)
        # Only the catch move here; the pair logic feeds on from where it ends.
        self.assertEqual(moved[0][0], [1, 3])
        self.assertEqual([move[0] for move in moved[0][1]], [b.FEED_CATCH_LENGTH, -b.FEED_CATCH_LENGTH])
        self.assertFalse(moved[0][2])
        self.assertEqual(inserted, [(1, 12., 13.), (3, 12., 13.)])

    class Pair:
        def __init__(self, inserted):
            self.inserted = inserted

        def insert(self, slot, now, after):
            self.inserted.append((slot, now, after))

    def test_spool_pulled_out_during_the_wait_is_not_fed(self):
        self.mover.feed_pending.add(2)
        self.mover.feed_timer = "timer"
        self.mover.start_sequence = lambda *args: self.fail("no feed expected")
        self.mover._run_feeds(12.)
        self.assertEqual(self.mover.feed_pending, set())

    def test_steps_of_a_sequence_follow_each_other(self):
        self.start_real()
        self.mover.calc_move_time = lambda d, s, a: (1. if d > 0 else -1., .5, 1., 40.)
        self.mover.start_sequence([2], [(30., 15., 200.), (-30., 15., 200.)])
        appends = self.mover.ffi_lib.appends
        self.assertEqual(len(appends), 2)
        self.assertEqual(appends[1][1], appends[0][1] + 2.)
        self.assertEqual((appends[0][5], appends[1][5]), (0., 30.))

    def test_flickering_sensor_adds_no_second_feed(self):
        self.mover.feeding.add(2)
        self.wrapper(2)()
        self.assertEqual(self.stock_calls, [])
        self.assertEqual(self.mover.feed_pending, set())
        self.mover.feeding.clear()
        self.wrapper(1)()
        self.wrapper(1)()
        self.assertEqual(self.mover.feed_pending, {1})

    def test_insert_right_after_an_eject_is_kept(self):
        self.mover.busy.add(2)
        self.wrapper(2)()
        self.assertEqual(self.mover.feed_pending, {2})

    def test_insert_right_after_an_eject_is_kept(self):
        self.mover.busy.add(2)
        self.wrapper(2)()
        self.assertEqual(self.mover.feed_pending, {2})

    def test_all_spools_start_together(self):
        self.present(0, 1, 2, 3)
        self.mover.feed_pending.update({0, 1, 2, 3})
        self.mover.feed_timer = "timer"
        self.mover._run_feeds(11.)
        self.assertEqual(self.mover.feeding, {0, 1, 2, 3})

    def test_insert_right_after_an_eject_is_kept(self):
        self.mover.busy.add(2)
        self.wrapper(2)()
        self.assertEqual(self.mover.feed_pending, {2})

    def test_motor_stops_when_the_filament_leaves(self):
        self.start_real()
        self.mover.ejecting.update({0, 2})
        self.mover._stop_finished_ejects({0: False, 2: True}, 50.)
        self.assertEqual(self.printer.enable_of["slot0"].calls, ["off"])
        self.assertEqual(self.printer.enable_of["slot2"].calls, [])
        self.assertEqual(self.mover.ejecting, {2})

    def test_feed_of_a_busy_slot_waits(self):
        self.printer.run_script = lambda script: None
        moved = []
        self.mover.start_sequence = lambda slots, moves, finish: moved.append(slots) or 12.
        self.mover._pair_of = lambda slot: self.Pair([])
        self.present(1, 2)
        self.mover.busy.add(1)
        self.mover.feed_pending.update({1, 2})
        self.mover.feed_timer = "timer"
        self.mover._run_feeds(11.)
        self.assertEqual(moved, [[2]])
        self.assertEqual(self.mover.feed_pending, {1})
        self.assertEqual(self.reactor.when, 11. + b.FEED_RETRY)
        self.mover.busy.clear()
        self.mover._run_feeds(11.5)
        self.assertEqual(moved, [[2], [1]])

    def test_reread_of_a_free_slot_turns_only_its_spool(self):
        calls, said = [], []
        self.mover.stock_reread = lambda gcmd: calls.append("stock")
        self.mover.reread = lambda slot: calls.append(slot) or True
        gcmd = self.Gcmd({"SLOT": 2}, said)
        self.present(2)
        self.mover.cmd_reread(gcmd)
        self.assertEqual(calls, [2])
        self.assertIn(2, self.mover.feeding)
        self.assertEqual(said, ["bambu_rfid: reading slot 2 again"])

    def test_reread_of_the_loaded_slot_or_an_empty_one_is_stock(self):
        calls = []
        self.mover.stock_reread = lambda gcmd: calls.append("stock")
        self.mover.reread = lambda slot: self.fail("not ours")
        self.present(1, 2)
        self.mover.cmd_reread(self.Gcmd({"SLOT": 1}, []))
        self.mover.cmd_reread(self.Gcmd({"SLOT": 3}, []))
        self.assertEqual(calls, ["stock", "stock"])

    def test_reread_of_a_moving_slot_waits(self):
        said = []
        self.mover.stock_reread = lambda gcmd: self.fail("not stock")
        self.mover.reread = lambda slot: self.fail("not now")
        self.present(2)
        self.mover.busy.add(2)
        self.mover.cmd_reread(self.Gcmd({"SLOT": 2}, said))
        self.assertEqual(said, ["bambu_rfid: slot 2 is moving, read it again when it stops"])

    class Gcmd:
        def __init__(self, params, said):
            self.params, self.said = params, said

        def get_int(self, name):
            return self.params[name]

        def respond_info(self, text):
            self.said.append(text)

    def test_stock_read_on_means_no_feed(self):
        self.saved["auto_read_rfid"] = 1
        self.wrapper()()
        self.assertEqual(self.stock_calls, ["stock"])
        self.assertEqual(self.mover.feed_pending, set())

    def test_no_feed_while_printing(self):
        self.state = "printing"
        self.wrapper()()
        self.assertEqual(self.stock_calls, ["stock"])
        self.assertEqual(self.mover.feed_pending, set())

    def test_watch_stops_finished_eject(self):
        self.start_real()
        self.mover.ejecting.add(0)
        self.mover._watch_inserts(10.)
        self.assertEqual(self.printer.enable_of["slot0"].calls, ["off"])


class TagSaveTests(unittest.TestCase):
    def test_writes_slot_values_and_record(self):
        tag = {"uid": "AABB", "detail": "PLA Matte", "color": "112233", "tag_color": "112233", "tray": "CC", "weight": 1000}
        scripts = b.tag_save_scripts(2, tag, "QFA01")
        self.assertEqual(scripts[0], "SAVE_VARIABLE VARIABLE=filament_slot2 VALUE=\"'QFA01'\"")
        self.assertEqual(scripts[1], "SAVE_VARIABLE VARIABLE=color_slot2 VALUE=\"['112233']\"")
        self.assertIn("'filament': 'QFA01'", scripts[2])
        self.assertIn("'tray': 'CC'", scripts[2])

    def test_unknown_material_keeps_the_slot_profile(self):
        scripts = b.tag_save_scripts(0, {"uid": "A", "color": "000000"}, None)
        self.assertEqual(len(scripts), 2)
        self.assertIn("color_slot0", scripts[0])

    def test_readers_serve_pairs(self):
        self.assertEqual([b.reader_of(slot) for slot in range(4)],
                         ["card_reader_1", "card_reader_1", "card_reader_2", "card_reader_2"])


class TraceTests(unittest.TestCase):
    def test_call_is_logged_and_passed_on(self):
        class Stepper:
            def slot_load(self, length):
                return length + 1
        stepper, logged = Stepper(), []
        names = b.trace_calls(stepper, "slot0", ("slot_load", "missing"), lambda *args: logged.append(args))
        self.assertEqual(names, ["slot_load"])
        self.assertEqual(stepper.slot_load(1), 2)
        self.assertEqual(logged[0][1:3], ("slot0", "slot_load"))


if __name__ == "__main__":
    unittest.main()
