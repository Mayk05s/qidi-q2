#!/usr/bin/env python3
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

        def lookup(name, default=None):
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
        self.mover._eject_chunk = lambda slots, begin, done: moved.append((slots, begin, done))
        self.reactor.callback(10.)
        self.assertEqual(moved, [([0, 2], None, 0.)])
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

    def test_without_the_spools_package_spools_go_the_safe_length(self):
        self.present(1)
        moved, scripts = [], []
        self.printer.run_script = scripts.append
        self.mover.start_sequence = lambda slots, moves, finish: moved.append((slots, moves, finish))
        self.mover.feed_pending.add(1)
        self.mover.feed_timer = "timer"
        self.mover._run_feeds(12.)
        self.assertEqual([move[0] for move in moved[0][1]],
                         [b.FEED_CATCH_LENGTH, -b.FEED_CATCH_LENGTH, b.FEED_LENGTH])
        self.assertEqual(scripts, ["SAVE_VARIABLE VARIABLE=slot1 VALUE=1"])

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
        self.mover._start_pair_feeds = lambda slots: self.assertEqual(slots, [0, 1, 2, 3])
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
