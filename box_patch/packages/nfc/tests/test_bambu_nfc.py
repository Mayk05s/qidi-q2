"""Tests for bambu_nfc: writing our own tags, against the simulated chip of the core tests."""
import pathlib
import sys
import types

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
        self.assertNotIn(7, n.WRITABLE_BLOCKS)

    def test_spool_number_goes_to_block_6_and_reads_as_tray(self):
        chip = self.chip()
        block6 = b.SPOOL_MAGIC + bytes.fromhex("0123456789ABCDEF") + bytes(4)
        n.write_tag(self.edit(chip), UID, {4: bytes([2, 22, 1]) + bytes(13), 5: self.NEW5, 6: block6},
                    1., self.clock.monotonic)
        self.assertEqual(chip.blocks[6], block6)
        self.assertEqual(self.edit(chip).read_custom()[1]["tray_uid"], "0123456789ABCDEF")

    def test_writes_maker_and_name_to_sector_2(self):
        chip = self.chip()
        chip.sector2_key = b.DEFAULT_KEY
        names = b.name_blocks("eSun", "PETG+")
        blocks = {4: self.NEW4, 5: self.NEW5}
        blocks.update(zip(b.NAME_BLOCKS, names))
        n.write_tag(self.edit(chip), UID, blocks, 1., self.clock.monotonic)
        self.assertEqual(b.parse_name([chip.blocks[x] for x in b.NAME_BLOCKS]), ("eSun", "PETG+"))

    def test_locked_sector_2_stops_the_name(self):
        chip = self.chip()
        blocks = {4: self.NEW4}
        blocks.update(zip(b.NAME_BLOCKS, b.name_blocks("eSun", "PETG+")))
        self.assert_problem("denied", lambda: n.write_tag(self.edit(chip), UID, blocks, 1., self.clock.monotonic))


class FakeGcmd:
    def __init__(self, **params):
        self.params = params

    def get(self, name, default=KeyError):
        if name in self.params:
            return self.params[name]
        if default is KeyError:
            raise KeyError(name)
        return default

    def get_float(self, name, default, **kwargs):
        return float(self.params.get(name, default))

    def error(self, message):
        return ValueError(message)


class WriteCommandTests(unittest.TestCase):
    def run_command(self, **params):
        clock = FakeClock()
        chip = FakeChip(key=b.DEFAULT_KEY)
        reader = types.SimpleNamespace(chip=b.Fm17550(chip, clock.pause), reactor=types.SimpleNamespace(
            monotonic=clock.monotonic), _check_stock_idle=None)
        reader._tag_command = lambda gcmd, action: action()
        n.cmd_TAG_WRITE(reader, FakeGcmd(UID=UID.hex(), **params))
        return chip

    def test_block_6_is_passed_on(self):
        block6 = (b.SPOOL_MAGIC + bytes(range(1, 9)) + bytes(4)).hex()
        chip = self.run_command(BLOCK4=bytes(range(16)).hex(), BLOCK6=block6)
        self.assertEqual(chip.blocks[6].hex(), block6)
        self.assertEqual(chip.blocks[5], BLOCKS[5])

    def test_excluded_tag_is_not_written(self):
        with self.assertRaises(b.RfidError):
            self.run_command(BLOCK4=bytes(range(16)).hex(), EXCLUDE=UID.hex())

    def test_short_block_6_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_command(BLOCK4=bytes(range(16)).hex(), BLOCK6="00")


if __name__ == "__main__":
    unittest.main()
