#!/usr/bin/env python3
"""PairDriver from pair_feed against the spool model of the emulator: the motors follow the
pieces the driver plans, the spools turn with slack, and the reader sees what is at the antenna."""
import unittest

import pathlib
import sys

# The tested files are those copied to the printer, in ../printer; bambu_rfid.py comes from the
# rfid_core package. On the printer they all sit in klippy/extras.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "rfid_core" / "printer"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "printer"))
import bambu_rfid  # noqa: E402
import box_feed  # noqa: E402
import pair_feed as b  # noqa: E402
import pair_logic as p
import test_pair_logic as T

STEP = .02


class Enable:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def motor_enable(self, print_time):
        self.log.append((self.name, "on"))

    def motor_disable(self, print_time):
        self.log.append((self.name, "off"))


class Printer:
    def __init__(self, bench):
        self.bench = bench

    def get_reactor(self):
        return self.bench

    def lookup_object(self, name, default=None):
        bench = self.bench
        if name == "toolhead":
            return bench
        if name == "stepper_enable":
            return bench
        if name == "gcode":
            return bench
        if name.startswith("box_stepper slot"):
            return bench.Sensor(bench, int(name[-1]))
        raise KeyError(name)


class Mover:
    """The parts of BoxMover the driver uses; start_piece only times the piece."""

    def __init__(self, bench):
        self.bench = bench
        self.printer = Printer(bench)
        self.readers = {"rfid": bench}
        self.ready, self.finished = [], []

    def start_piece(self, slot, begin, times):
        if begin is None:
            begin = self.bench.t + box_feed.SLOT_START_DELAY
        self.bench.pieces.append((slot, begin, times))
        return begin, begin + times[0] + times[1] + times[2]

    def _mark_ready(self, slots):
        self.ready.extend(slots)

    def _finish_after(self, slots, end):
        self.finished.append((slots, end))


class Bench:
    """Reactor, toolhead, reader and slot sensors around one pair, driven by the world model."""
    NEVER = 9e99

    class Sensor:
        def __init__(self, bench, slot):
            self.bench, self.slot = bench, slot

        def get_status(self, eventtime):
            st = self.bench.world.state["AB"[self.slot]]
            return {"runout_button": 0 if self.bench.inserted[self.slot] and st["real"] > 0 else 1}

    def __init__(self, a, b_angle, slack=(0., 300.), has=(True, True), seed=0):
        self.t = 0.
        self.world = T.World(a, b_angle, slack=slack, has=has, seed=seed)
        self.inserted = [False, False]
        self.pieces, self.log, self.scripts = [], [], []
        self.mcu = self
        self.mover = Mover(self)
        self.driver = b.PairDriver(self.mover, "rfid", (0, 1))
        self.next = None
        self.done_at = None
        self.last = [0., 0.]

    # reactor, toolhead and gcode
    def monotonic(self):
        return self.t

    def estimated_print_time(self, eventtime):
        return eventtime

    def register_timer(self, callback, when):
        self.next = when
        return callback

    def lookup_enable(self, name):
        return Enable(self.log, name)

    def run_script(self, script):
        self.scripts.append(script)

    def respond_raw(self, text):
        self.scripts.append(text)

    # reader, as bambu_rfid sees it
    proxy = None

    def _read_slot_tag(self, timeout, uids, trays):
        seen = [name for slot, name in enumerate("AB")
                if self.inserted[slot] and self.world._zone(self.world.state[name])]
        if len(seen) > 1:
            raise bambu_rfid.SeveralTags("two tags")
        if not seen:
            raise bambu_rfid.TagAbsent("no tag")
        return {"uid": seen[0], "material": "PLA", "color": "ffffff"}

    def insert(self, slot):
        self.inserted[slot] = True
        self.driver.insert(slot, self.t, self.t + .5)

    def _move_world(self):
        """Turn the spools by what the motors did since the last step, as World.step does."""
        for slot, name in ((0, "A"), (1, "B")):
            track = self.driver.track[slot]
            st = self.world.state[name]
            pos = track.position(self.t) if self.inserted[slot] else 0.
            d, self.last[slot] = pos - self.last[slot], pos
            if d > 0:
                g = min(st["slip"], d)
                st["slip"] -= g
                m = d - g
                st["real"] += m
                u = min(st["slack"], m)
                st["slack"] -= u
                st["angle"] += (m - u) / p.TURN * 360.
            elif d < 0 and st["slip"] <= 0:
                st["real"] += d
                st["angle"] += d / p.TURN * 360.

    def run(self, gap=0., order="AB", seconds=400.):
        first, second = ("AB".index(order[0]), "AB".index(order[1]))
        self.insert(first)
        while self.t < seconds:
            self.t += STEP
            self._move_world()
            if not self.inserted[second] and self.t >= gap:
                self.insert(second)
            if self.next is not None and self.t >= self.next:
                self.next = None
                when = self.driver._tick(self.t)
                if when != self.NEVER:
                    self.next = when
                elif self.inserted[second]:
                    return True
        return False


def tags(bench):
    return tuple(bench.driver._spool(slot).tag for slot in (0, 1))


class PieceTests(unittest.TestCase):
    def test_piece_reaches_its_length_and_speeds(self):
        for distance, start_v, end_v in ((30., 0., 60.), (24., 60., 60.), (9., 60., 0.), (-200., 0., 0.), (2., 0., 0.)):
            times = b.piece(distance, start_v, 60., end_v, 200.)
            end = sum(times[:3])
            self.assertAlmostEqual(b.piece_position(0., times, end), distance, places=6)
            accel_t, cruise_t, decel_t, axis_r, v0, cruise_v, accel = times
            self.assertAlmostEqual(cruise_v - accel * decel_t, end_v, places=6)
            self.assertLessEqual(cruise_v, 60.)

    def test_track_runs_smoothly_through_joined_pieces(self):
        track = b.Track()
        track.add(0., 0.45, b.piece(6.75 + 24., 0., 60., 60., 200.))
        track.add(0.45 + 0.4, 0.45 + 0.8, b.piece(24., 60., 60., 60., 200.))
        self.assertEqual(track.speed, 60.)
        track.add(1.25, 1.55, b.piece(9., 60., 60., 0., 200.))
        self.assertEqual(track.speed, 0.)
        self.assertAlmostEqual(track.planned, 63.75, places=6)
        self.assertAlmostEqual(track.position(10.), 63.75, places=6)


class PairDriverTests(unittest.TestCase):
    def sweep(self, step=45, **kwargs):
        has = kwargs.get("has", (True, True))
        want = ("A" if has[0] else None, "B" if has[1] else None)
        bad = []
        for order in ("AB", "BA"):
            for gap in (0., 1., 8., 30.):
                for a in range(0, 360, step):
                    for c in range(0, 360, step):
                        bench = Bench(a, c, seed=a * 1000 + c, **kwargs)
                        finished = bench.run(gap, order)
                        if not finished or tags(bench) != want or bench.driver.logic.guard:
                            bad.append((a, c, gap, order, finished, tags(bench)))
        return bad

    def test_all_positions(self):
        self.assertEqual(self.sweep(), [])

    def test_spool_without_a_tag(self):
        self.assertEqual(self.sweep(has=(False, True)), [])

    def test_pieces_join_at_speed_and_stop_smoothly(self):
        bench = Bench(350., 90.)
        bench.run(0.)
        for slot in (0, 1):
            pieces = [(begin, times) for s, begin, times in bench.pieces if s == slot]
            speed = 0.
            for begin, times in pieces:
                # Every piece starts at the speed the one before it ended with.
                self.assertAlmostEqual(times[3] * times[4], speed, places=6)
                end_v = times[5] - times[6] * times[2]
                speed = times[3] * end_v if end_v > 1e-6 else 0.
            self.assertEqual(speed, 0.)

    def test_ready_spools_are_marked_and_tags_saved(self):
        bench = Bench(350., 90.)
        self.assertTrue(bench.run(0.))
        self.assertEqual(tags(bench), ("A", "B"))
        self.assertEqual(sorted(bench.mover.ready), [0, 1])
        self.assertTrue(any("bambu_tag_slot0" in s and "'A'" in s for s in bench.scripts))
        self.assertTrue(any("bambu_tag_slot1" in s and "'B'" in s for s in bench.scripts))
        self.assertEqual(len(bench.mover.finished), 1)

    def test_backwards_stops_at_the_minimum(self):
        bench = Bench(350., 90., has=(False, False))
        bench.run(0.)
        for slot in (0, 1):
            track = b.Track()
            for s, begin, times in bench.pieces:
                if s == slot:
                    track.add(begin, begin + sum(times[:3]), times)
                    if times[3] < 0:
                        self.assertGreaterEqual(track.planned, b.PAIR_MIN_POS - 1e-6)

    def test_filament_pulled_out_switches_the_motor_off(self):
        bench = Bench(350., 90.)
        bench.insert(0)
        for _ in range(200):
            bench.t += STEP
            bench._move_world()
            if bench.next is not None and bench.t >= bench.next:
                bench.next = None
                when = bench.driver._tick(bench.t)
                bench.next = None if when == bench.NEVER else when
        bench.inserted[0] = False
        bench.t += STEP
        bench.driver._tick(bench.t)
        self.assertTrue(bench.driver._spool(0).out)
        self.assertIn(("slot0", "off"), bench.log)


if __name__ == "__main__":
    unittest.main()
