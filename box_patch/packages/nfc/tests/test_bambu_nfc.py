"""Tests for bambu_nfc: writing our own tags, against the simulated chip of the core tests."""
import pathlib
import sys

PACKAGES = pathlib.Path(__file__).resolve().parent.parent.parent
for folder in ("nfc/printer", "rfid_core/printer", "rfid_core/tests"):
    sys.path.insert(0, str(PACKAGES / folder))
import unittest

import bambu_nfc as n
import bambu_rfid as b
from test_bambu_rfid import BLOCKS, UID, FakeChip, FakeClock


class TagWriteTests(unittest.TestCase):
    NEW4, NEW5 = bytes(range(16)), b.CUSTOM_COLOR_MAGIC + bytes(12)

    def setUp(self):
        self.clock = FakeClock()

    def chip(self, key=b.DEFAULT_KEY, **kwargs):
        return FakeChip(key=key, **kwargs)

    def edit(self, chip):
        return b.Fm17550(chip, self.clock.pause)

    def write(self, chip, uid=UID, timeout=1.):
        return n.write_tag(self.edit(chip), uid, {4: self.NEW4, 5: self.NEW5}, timeout, self.clock.monotonic)

    def assert_problem(self, code, action):
        with self.assertRaises(b.TagProblem) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_writes_both_blocks(self):
        chip = self.chip()
        self.assertEqual(self.write(chip)["key"], "default")
        self.assertEqual(chip.blocks[4], self.NEW4)
        self.assertEqual(chip.blocks[5], self.NEW5)
        self.assertEqual(chip.regs[b.REG_T_PRESCALER], 0x55)  # registers restored

    def test_other_tag_is_not_written(self):
        chip = self.chip()
        self.assert_problem("other_tag", lambda: self.write(chip, uid=bytes(4)))
        self.assertEqual(chip.blocks[4], BLOCKS[4])

    def test_bambu_tag_is_recognised_and_not_written(self):
        chip = FakeChip()
        self.assert_problem("bambu", lambda: self.write(chip))
        self.assertEqual(chip.blocks[4], BLOCKS[4])
        self.assertFalse([f for f in chip.sent if f[0] == 0xA0])

    def test_unknown_key_reports_locked(self):
        self.assert_problem("locked", lambda: self.write(self.chip(key=bytes(6))))

    def test_other_tag_type_is_refused(self):
        chip = self.chip()
        chip.sak = 0x18
        self.assert_problem("not_classic", lambda: self.write(chip))

    def test_denied_write_stops_at_once(self):
        chip = self.chip()
        chip.deny_write = True
        self.assert_problem("denied", lambda: self.write(chip))
        self.assertEqual(len([f for f in chip.sent if f[0] == 0xA0]), 1)

    def test_lost_exchange_is_retried(self):
        chip = self.chip()
        chip.fail_second_block = True
        self.write(chip)
        self.assertEqual(chip.blocks[5], self.NEW5)

    def test_bad_read_back_is_an_error(self):
        chip = self.chip()
        chip.corrupt_write = True
        with self.assertRaises(b.RfidError):
            self.write(chip)

    def test_sector_trailer_cannot_be_written(self):
        with self.assertRaises(ValueError):
            n.write_block(self.edit(self.chip()), 7, bytes(16))


if __name__ == "__main__":
    unittest.main()
