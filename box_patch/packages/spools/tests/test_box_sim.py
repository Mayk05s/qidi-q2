#!/usr/bin/env python3
"""The whole Box (box_sim): spools put in at random, with tags at random angles or none, are fed,
checked at the splitter one by one and read again, all by the package's own code."""
import logging
import random
import unittest

import box_sim as S


def scenario(seed):
    r = random.Random(seed)
    return dict(angles=tuple(r.choice(range(0, 360, 15)) for _ in range(4)),
                has=tuple(r.random() > 0.25 for _ in range(4)),
                order=r.sample(range(4), r.choice((1, 2, 3, 4))),
                gap=r.choice((0.2, 1., 5., 20.)),
                slack=(0., r.choice((0., 150., 300.))),
                read_time=r.choice((0.3, 0.6, 1.0)),
                splitters=tuple(r.uniform(1000., 1250.) for _ in range(4)),
                pushes=tuple(r.uniform(5., 80.) for _ in range(4)),
                seed=seed)


class BoxTests(unittest.TestCase):
    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_four_spools_go_to_the_splitter_one_by_one(self):
        box, finished = S.run((40., 200., 300., 120.), has=(True, True, True, False))
        self.assertTrue(finished)
        self.assertEqual(box.slot_tags(), box.want())
        self.assertEqual(box.violations, [])
        # In the order put in, the readers taking turns: 1A, 1C, 1B, 1D.
        checked = [text.split()[2] for t, text in box.events if "reached the splitter" in text]
        self.assertEqual(checked, ["0", "2", "1", "3"])
        self.assertEqual(sorted(box.mover._splitters()), [0, 1, 2, 3])

    def test_random_boxes(self):
        # A tag never lands in the wrong slot, no filament goes past its splitter but for the
        # check, and every run ends. With reads of 1 s a tag may still be missed.
        for seed in range(5000, 5080):
            kwargs = scenario(seed)
            box, finished = S.run(kwargs.pop("angles"), **kwargs)
            got, want = box.slot_tags(), box.want()
            self.assertTrue(finished, seed)
            self.assertEqual(box.violations, [], seed)
            for slot in range(4):
                if got[slot] is not None:
                    self.assertEqual(got[slot], want[slot], seed)
                elif kwargs["read_time"] < 1.:
                    self.assertIsNone(want[slot], seed)


def print_scenario(seed):
    r = random.Random(seed)
    head = r.randrange(4)
    others = [slot for slot in range(4) if slot != head]
    return dict(angles=tuple(r.choice(range(0, 360, 15)) for _ in range(4)),
                has=tuple(r.random() > 0.25 for _ in range(4)),
                head=head,
                printing=r.random() > 0.25,
                fed_first=r.random() > 0.5,
                speed=r.choice((0.5, 2., 5.)),
                order=r.sample(others, r.choice((1, 2, 3))),
                gap=r.choice((0.2, 1., 5., 20.)),
                change=r.choice(others),
                swap=r.choice(others + [None]),
                read_time=r.choice((0.3, 0.6, 1.0)),
                splitters=tuple(r.uniform(1000., 1250.) for _ in range(4)),
                pushes=tuple(r.uniform(5., 80.) for _ in range(4)),
                seed=seed)


def run_with_head(kw):
    """Spools changed while another one is in the toolhead, printing or not; then the print ends,
    the toolhead is unloaded and the waiting checks run."""
    kw = dict(kw)
    head, printing, speed = kw.pop("head"), kw.pop("printing"), kw.pop("speed")
    order, gap, change, swap = kw.pop("order"), kw.pop("gap"), kw.pop("change"), kw.pop("swap")
    fed_first = kw.pop("fed_first", False)
    box = S.Box(kw.pop("angles"), **kw)
    if fed_first:
        # Fed, checked and read by us, then loaded into the toolhead.
        box.insert(head)
        box.settle()
    box.load_head(head)
    if printing:
        box.start_print(speed)
    steps = {"order": order, "paused": None, "refused": None}
    for slot in order:
        box.insert(slot)
        box.run_until(box.t + gap)
        if printing and slot == order[0]:
            # The print wants a slot still being fed: it pauses until the slot stands.
            fed = box.mover.ours_now(slot)
            steps["paused"] = (fed, box.tool_change(slot))
            if box.state == "paused":
                box.resume()
    if printing:
        # The slot that prints cannot be unloaded from the screen.
        box.screen_eject(head)
        steps["refused"] = box.events[-1][1]
    steps["busy"] = box.settle_busy()
    steps["tags_busy"] = (box.slot_tags(), box.want())
    if swap is not None and box.inserted[swap]:
        box.eject(swap)
        box.run_until(box.t + 25.)
        box.pull_out(swap)
        box.run_until(box.t + 2.)
        box.insert(swap)
        steps["busy"] = steps["busy"] and box.settle_busy()
    if printing:
        steps["change"] = box.tool_change(change) if box.inserted[change] else True
        box.end_print()
    steps["head_tag"] = box.slot_tags()[head]
    box.unload_head()
    steps["checked"] = box.settle()
    return box, steps


class PrintTests(unittest.TestCase):
    """While a spool is in the toolhead the splitter sensor is held: the other spools are fed
    and read again from their safe point, their checks wait for the toolhead to be unloaded."""

    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)

    def assert_tags(self, got, want, read_time, seed, skip=None, misses=False):
        for slot in range(4):
            if slot == skip:
                continue
            if got[slot] is not None:
                self.assertEqual(got[slot], want[slot], (seed, slot, got, want))
            elif read_time < 1. and not misses:
                self.assertIsNone(want[slot], (seed, slot, got, want))

    def test_print_from_1c_while_the_others_change(self):
        box, steps = run_with_head(dict(angles=(40., 200., 300., 120.), has=(True,) * 4, head=2,
                                        printing=True, speed=2., order=[0, 1, 3], gap=3., change=0,
                                        swap=1, seed=1))
        self.assertEqual(box.violations, [])
        self.assertEqual(steps["paused"], (True, False))
        self.assertIn("not allowed while printing", steps["refused"])
        self.assertTrue(steps["busy"])
        self.assertEqual(steps["tags_busy"][0], ("TAG1A", "TAG1B", "TAG1C", "TAG1D"))
        self.assertTrue(steps["change"])
        self.assertEqual(steps["head_tag"], "TAG1C")
        self.assertTrue(steps["checked"])
        self.assertEqual(sorted(box.mover.checked), [0, 1, 3])
        # No check ran while the toolhead held the splitter.
        ends = [t for t, text in box.events if text in ("print ended",)]
        checks = [t for t, text in box.events if "reached the splitter" in text]
        self.assertTrue(checks and min(checks) > ends[0])

    def test_random_boxes_with_the_toolhead_loaded(self):
        for seed in range(7000, 7040):
            kw = print_scenario(seed)
            box, steps = run_with_head(kw)
            read_time = kw["read_time"]
            self.assertEqual(box.violations, [], seed)
            self.assertTrue(steps["busy"], seed)
            self.assertTrue(steps["checked"], seed)
            if steps["paused"] is not None:
                fed, changed = steps["paused"]
                self.assertEqual(changed, not fed, seed)
            if steps["refused"] is not None:
                self.assertIn("not allowed while printing", steps["refused"], seed)
            if kw["printing"]:
                self.assertTrue(steps["change"], seed)
            # The spool in the toolhead keeps its tag; no other spool takes it.
            want_head = "TAG" + S.LABELS[kw["head"]] if kw["has"][kw["head"]] else None
            self.assertEqual(steps["head_tag"], want_head, seed)
            # While the toolhead holds a spool its tag may stand at the antenna: the neighbour
            # cannot be read then, but never takes that tag.
            self.assert_tags(*steps["tags_busy"], read_time, seed, misses=True)
            # The toolhead's spool was taken out by the stock unload: its record is not ours.
            self.assert_tags(box.slot_tags(), box.want(), read_time, seed, skip=kw["head"])


def settle_slot(box, slot, limit=300.):
    """Run until this slot has been checked and everything stands."""
    end = box.t + limit
    while box.t < end:
        box.run_until(box.t + 1.)
        if box.idle() and slot in box.mover.checked:
            return True
    return False


class RestartTests(unittest.TestCase):
    """Spools fed before Klipper started, read again from the screen (7 Nov on the printer)."""

    def setUp(self):
        logging.disable(logging.WARNING)
        self.addCleanup(logging.disable, logging.NOTSET)

    def test_reread_after_restart_keeps_the_noted_splitter(self):
        # 1A stands 47 mm further in than the safe point the reread takes it to be at.
        box = S.Box((40., 200., 300., 120.), splitters=(1104., 1143., 1122., 1071.))
        box.fed_before(0, box.mover.safe_length(0) + 47.)
        box.fed_before(1, box.mover.safe_length(1))
        box.screen_reread(0)
        self.assertTrue(settle_slot(box, 0))
        self.assertEqual(box.violations, [])
        self.assertEqual(box.mover._splitters()[0], 1104.)
        # Where it stands is known again, counted from the slot sensor.
        self.assertAlmostEqual(box.mover.lengths[0], box.real(0), delta=3.)
        self.assertEqual(box.slot_tags()[0], "TAG1A")

    def test_reread_without_a_noted_splitter_notes_none(self):
        box = S.Box((40., 200., 300., 120.))
        box.fed_before(0, box.mover.safe_length(0) + 47., noted=False)
        box.screen_reread(0)
        self.assertTrue(settle_slot(box, 0))
        self.assertNotIn(0, box.mover._splitters())
        self.assertEqual(box.violations, [])

    def test_a_tag_read_in_full_is_not_replaced_by_an_unread_one(self):
        # A Bambu spool: two tags, the second never read in full.
        for angle in range(0, 360, 30):
            box = S.Box((angle, 200., 300., 120.), twins=(True, False, False, False))
            box.fed_before(0, box.mover.safe_length(0))
            box.screen_reread(0)
            self.assertTrue(settle_slot(box, 0), angle)
            self.assertEqual(box.slot_tags()[0], "TAG1A", angle)


if __name__ == "__main__":
    unittest.main()
