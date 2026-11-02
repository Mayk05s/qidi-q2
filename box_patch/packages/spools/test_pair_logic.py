#!/usr/bin/env python3
"""The same world as feed_emulator: spools with slack, one tag each, a stop that takes time."""
import random
import unittest

import pair_logic as p

ZONE, POLL, STEP = 24., .2, .02


class World:
    def __init__(self, a, b, delay=.3, slack=(0., 300.), slip=(0., 0.), has=(True, True), limit=1000., seed=0, test=350.):
        rng = random.Random(seed)
        self.logic = p.PairLogic(("A", "B"), limit=limit, test=test)
        self.delay, self.t, self.next = delay, 0., 0.
        self.state = {}
        for name, angle, here in (("A", a, has[0]), ("B", b, has[1])):
            self.state[name] = dict(angle=float(angle), slack=rng.uniform(*slack), slip=rng.uniform(*slip),
                                    real=40., has=here, v=0., stop=None, want=0.)

    def step(self):
        self.t += STEP
        for spool in self.logic.spools:
            st = self.state[spool.name]
            want = spool.dir * spool.speed
            # A stop or a turn takes effect late, as on the motors.
            if want != st["want"]:
                st["want"], st["stop"] = want, self.t + self.delay if st["v"] else self.t
            if st["stop"] is not None and self.t >= st["stop"]:
                st["v"], st["stop"] = st["want"], None
            d = st["v"] * STEP
            if not d:
                continue
            spool.pos += d
            if d > 0:
                g = min(st["slip"], d)
                st["slip"] -= g
                m = d - g
                st["real"] += m
                u = min(st["slack"], m)
                st["slack"] -= u
                st["angle"] += (m - u) / p.TURN * 360.
                # The printer knows only the length fed, not how much of it turned the spool.
                spool.rot += d
            elif st["slip"] <= 0:
                st["real"] += d
                st["angle"] += d / p.TURN * 360.
                spool.rot += d
                if st["real"] <= 0:
                    spool.out = True
                    spool.dir, st["v"], st["want"] = 0, 0., 0.
        for spool in self.logic.spools:
            spool.moving = bool(self.state[spool.name]["v"])

    @staticmethod
    def _zone(st):
        a = st["angle"] % 360.
        return st["has"] and (a <= ZONE / 2 or a >= 360. - ZONE / 2)

    def read(self):
        seen = [s.name for s in self.logic.spools if s.inserted and self._zone(self.state[s.name])]
        if len(seen) > 1:
            return p.READ_COLLISION, None
        if seen:
            return p.READ_TAG, seen[0]
        return p.READ_NONE, None

    def tick(self):
        if self.t >= self.next:
            self.next = self.t + POLL
            kind, uid = self.read()
            p.note_reading(self.logic, kind, uid)
            self.logic.tick(self.t, kind, uid)

    def play(self, gap=0., order="AB"):
        logic = self.logic
        logic.insert(order[0], 0.)
        n = 0
        while (not logic.done or not logic.spool(order[1]).inserted) and n < 40000:
            n += 1
            self.step()
            if not logic.spool(order[1]).inserted and self.t >= gap:
                logic.insert(order[1], self.t)
            if not logic.done:
                self.tick()
        return n < 40000


class PairLogicTests(unittest.TestCase):
    def sweep(self, step=30, **kwargs):
        has = kwargs.get("has", (True, True))
        bad = []
        for order in ("AB", "BA"):
            for gap in (0., .5, 1., 3., 8., 30.):
                for a in range(0, 360, step):
                    for b in range(0, 360, step):
                        world = World(a, b, seed=a * 1000 + b, **kwargs)
                        finished = world.play(gap, order)
                        got = (world.logic.spool("A").tag, world.logic.spool("B").tag)
                        want = ("A" if has[0] else None, "B" if has[1] else None)
                        if not finished or got != want or world.logic.guard:
                            bad.append((a, b, gap, order, got))
        return bad

    def test_all_positions(self):
        self.assertEqual(self.sweep(), [])

    def test_long_stop_delay(self):
        self.assertEqual(self.sweep(delay=.8), [])

    def test_spool_without_a_tag(self):
        self.assertEqual(self.sweep(has=(False, True)), [])

    def test_no_tags_at_all(self):
        self.assertEqual(self.sweep(has=(False, False)), [])

    def test_spool_alone_is_decided_at_once(self):
        world = World(350., 90.)
        logic = world.logic
        logic.insert("A", 0.)
        for _ in range(2000):
            world.step()
            if not logic.done:
                world.tick()
        self.assertEqual(logic.spool("A").tag, "A")
        self.assertTrue(logic.done)
        self.assertFalse(logic.spool("B").inserted)

    def test_reread_finds_the_tag_and_returns(self):
        world = World(350., 90.)
        world.play(0.)
        logic = world.logic
        base = logic.spool("A").pos
        self.assertTrue(logic.start_reread("A", world.t))
        for _ in range(5000):
            world.step()
            if logic.done:
                break
            world.tick()
        self.assertEqual(logic.spool("A").tag, "A")
        self.assertEqual(logic.spool("B").tag, "B")
        self.assertLess(abs(logic.spool("A").pos - base), 40.)

    def run_until_done(self, world):
        logic = world.logic
        for _ in range(30000):
            world.step()
            if logic.done:
                break
            world.tick()
        return logic

    def test_doubtful_spool_of_a_shared_tag_is_read_again(self):
        world = World(350., 90.)
        world.play(0.)
        logic = world.logic
        logic.spool("B").sure = p.SURE_GUESS
        logic.done = False
        logic._put(logic.spool("B"), "A", p.SURE_GUESS)
        self.assertEqual(logic.reread_queue, ["B"])
        self.assertEqual(logic.spool("A").tag, "A")
        self.run_until_done(world)
        self.assertEqual((logic.spool("A").tag, logic.spool("B").tag), ("A", "B"))

    def test_reread_finding_the_same_tag_moves_the_doubt_to_the_other(self):
        world = World(350., 90.)
        world.play(0.)
        logic = world.logic
        # A wrongly holds B's tag with more certainty; B, unsure, holds nothing.
        logic.spool("A").tag, logic.spool("A").sure = "B", p.SURE_LEFT
        logic.spool("B").tag = None
        logic.done = False
        logic._queue_reread(logic.spool("B"))
        self.run_until_done(world)
        self.assertEqual((logic.spool("A").tag, logic.spool("B").tag), ("A", "B"))
        self.assertEqual(logic.reread_queue, [])

if __name__ == "__main__":
    unittest.main()
