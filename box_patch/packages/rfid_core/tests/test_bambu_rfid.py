"""Tests for bambu_rfid against a simulated FM17550 and the real tag's data."""
import pathlib
import sys

PACKAGES = pathlib.Path(__file__).resolve().parent.parent.parent
for folder in ("rfid_core/printer",):
    sys.path.insert(0, str(PACKAGES / folder))
import os
import tempfile
import types
import unittest

import bambu_rfid as b

UID = bytes.fromhex("7081205A")
# Blocks read from the real tag 7081205A with the ACR122U
BLOCKS = {
    4: bytes.fromhex("504C41204C6974650000000000000000"),
    5: bytes.fromhex("EFE255FFE80300000000E03F00000000"),
    6: bytes.fromhex("3700080000000000F000BE0000000000"),
}


class FakeChip:
    """Register model of the FM17550 with one MIFARE Classic tag in the field."""

    def __init__(self, uid=UID, key=None, tag=True, collision=False, misses=0):
        self.misses = misses
        self.regs = {r: 0 for r in range(0x40)}
        self.regs[b.REG_VERSION] = 0x88
        self.regs[b.REG_CONTROL] = 0x10
        self.regs[b.REG_COMMAND] = 0x20  # reset value seen on the real chip
        self.regs[b.REG_T_PRESCALER] = 0x55
        self.uid, self.tag, self.collision = uid, tag, collision
        self.key = key if key is not None else b.derive_sector_keys(uid)[1]
        self.fifo, self.pending, self.sent = [], None, []
        self.blocks = dict(BLOCKS)
        self.selected = False
        self.writes = 0
        self.sak = 0x08
        self.tray_ok = False
        self.tray_fresh_ok = False
        self.sector2_key = None  # our own tags: the key of sector 2 for the name blocks
        self.write_target = None
        self.deny_write = False
        self.corrupt_write = False
        self.fail_second_block = False

    def _exchange(self, frame):
        self.sent.append(list(frame))
        crc = bool(self.regs[b.REG_TX_MODE] & 0x80)
        if not self.tag:
            return None
        authed = self.regs[b.REG_STATUS2] & b.CRYPTO1_ON
        if self.write_target is not None:
            block, self.write_target = self.write_target, None
            if not crc or self.regs[b.REG_RX_MODE] & 0x80:
                return None
            self.blocks[block] = bytes(frame[:15]) + b"\xEE" if self.corrupt_write else bytes(frame)
            return bytes([0x0A])
        if frame[0] == 0xA0 and authed:
            if not crc or self.regs[b.REG_RX_MODE] & 0x80:  # the 4-bit answer has no CRC
                return None
            if self.deny_write:
                return bytes([0x04])
            if self.fail_second_block and frame[1] == 5:
                self.fail_second_block = False
                return None
            self.write_target = frame[1]
            return bytes([0x0A])
        if frame == [0x52] and self.regs[b.REG_BIT_FRAMING] & 7 == 7 and not crc:
            if self.misses > 0:  # tag is still out of range on the turning spool
                self.misses -= 1
                return None
            return bytes([0x04, 0x00])
        if frame == [0x93, 0x20] and not crc:
            if self.collision:
                self.regs[b.REG_ERROR] = b.ERR_COLLISION
            bcc = self.uid[0] ^ self.uid[1] ^ self.uid[2] ^ self.uid[3]
            return self.uid + bytes([bcc])
        if frame[:2] == [0x93, 0x70] and crc:
            self.selected = True
            return bytes([self.sak])
        if frame[0] == 0x30 and crc and self.regs[b.REG_STATUS2] & b.CRYPTO1_ON:
            return self.blocks[frame[1]]
        return None

    def _authent(self):
        frame = self.fifo
        ok = (self.selected and frame[0] == 0x60 and frame[1] == 4
              and bytes(frame[2:8]) == self.key and bytes(frame[8:12]) == self.uid)
        tray_key = (frame[1] == b.TRAY_SECTOR_BLOCK and bytes(frame[8:12]) == self.uid
                    and bytes(frame[2:8]) == b.derive_sector_keys(self.uid)[2])
        nested = self.regs[b.REG_STATUS2] & b.CRYPTO1_ON
        tray = tray_key and (self.tray_ok and nested or self.tray_fresh_ok and self.selected and not nested)
        name = (nested and frame[1] == b.NAME_BLOCKS[0] and self.sector2_key is not None
                and bytes(frame[2:8]) == self.sector2_key and bytes(frame[8:12]) == self.uid)
        if ok or tray or name:
            self.regs[b.REG_STATUS2] |= b.CRYPTO1_ON
        else:
            self.regs[b.REG_STATUS2] &= ~b.CRYPTO1_ON
        self.regs[b.REG_COM_IRQ] = b.IRQ_IDLE

    def spi_transfer(self, data):
        addr = data[0]
        if addr & 0x80:
            reg = (addr & 0x7F) >> 1
            if reg == b.REG_FIFO_DATA:
                value = self.fifo.pop(0) if self.fifo else 0
            elif reg == b.REG_FIFO_LEVEL:
                value = len(self.fifo)
            else:
                value = self.regs[reg]
            return {"response": bytes([0, value])}
        reg, value = addr >> 1, data[1]
        self.writes += 1
        if reg == b.REG_FIFO_DATA:
            self.fifo.append(value)
        elif reg == b.REG_FIFO_LEVEL:
            if value & 0x80:
                self.fifo = []
        elif reg == b.REG_COM_IRQ:
            self.regs[reg] = 0
            self.regs[b.REG_ERROR] = 0
        elif reg == b.REG_BIT_FRAMING and value & 0x80:
            self.regs[reg] = value
            cmd = self.regs[b.REG_COMMAND]
            if cmd == b.CMD_TRANSCEIVE:
                answer = self._exchange(self.fifo)
                self.fifo = []
                if answer is None:
                    self.regs[b.REG_COM_IRQ] = b.IRQ_TIMER
                else:
                    self.fifo = list(answer)
                    self.regs[b.REG_COM_IRQ] = b.IRQ_RX
            elif cmd == b.CMD_AUTHENT:
                self._authent()
        else:
            self.regs[reg] = value
        return {"response": bytes(2)}


class FakeClock:
    def __init__(self):
        self.now = 0.

    def monotonic(self):
        return self.now

    def pause(self, delay):
        self.now += delay


def driver(chip):
    return b.Fm17550(chip, lambda delay: None)


class KeyTests(unittest.TestCase):
    def test_key_derivation_opens_the_real_tag(self):
        # Sector 1 key that opened tag 7081205A on the ACR122U
        keys = b.derive_sector_keys(UID)
        self.assertEqual(len(keys), 16)
        self.assertEqual(keys[1].hex().upper(), "52B677D9AAD5")


class ParseTests(unittest.TestCase):
    def test_real_tag_fields(self):
        tag = b.parse_bambu(BLOCKS[4], BLOCKS[5], BLOCKS[6])
        self.assertEqual(tag["detail"], "PLA Lite")
        self.assertEqual(tag["color"], "EFE255")
        self.assertEqual(tag["weight"], 1000)
        self.assertEqual((tag["min_temp"], tag["max_temp"]), (190, 240))
        self.assertEqual(tag["qidi_material"], 7)

    def test_yellow_spool_maps_to_yellow(self):
        self.assertEqual(b.nearest_qidi_color("EFE255"), 8)

    def test_real_spool_colors(self):
        for value, code in (("68724D", 9), ("FFFFFF", 1), ("000000", 2), ("EFE255", 8),
                            ("7D6556", 21), ("8E9089", 20)):
            self.assertEqual(b.nearest_qidi_color(value), code, value)

    def test_exact_colors_map_to_themselves(self):
        for code, value in b.QIDI_COLORS.items():
            self.assertEqual(b.nearest_qidi_color(value), code)

    def test_longest_material_prefix_wins(self):
        self.assertEqual(b.qidi_material("PLA Silk+"), 4)
        self.assertEqual(b.qidi_material("PETG-CF"), 42)
        self.assertEqual(b.qidi_material("PAHT-CF"), 30)
        self.assertIsNone(b.qidi_material("Unknown"))


class DriverTests(unittest.TestCase):
    def test_reads_tag_through_registers(self):
        uid, tag = driver(FakeChip()).read_bambu()
        self.assertEqual(uid, UID)
        self.assertEqual(tag["detail"], "PLA Lite")

    def test_tag_write_command_is_never_sent(self):
        chip = FakeChip()
        driver(chip).read_bambu()
        self.assertFalse([f for f in chip.sent if f[0] in (0xA0, 0xA2, 0xC0, 0xC1, 0xC2)])

    def test_registers_restored_after_success(self):
        chip = FakeChip()
        before = {r: chip.regs[r] for r in b.SAVED_REGS}
        driver(chip).read_bambu()
        self.assertEqual({r: chip.regs[r] for r in b.SAVED_REGS}, before)
        self.assertFalse(chip.regs[b.REG_STATUS2] & b.CRYPTO1_ON)

    def test_registers_restored_after_failure(self):
        for chip in (FakeChip(tag=False), FakeChip(key=bytes(6)), FakeChip(collision=True)):
            before = {r: chip.regs[r] for r in b.SAVED_REGS}
            with self.assertRaises(b.RfidError):
                driver(chip).read_bambu()
            self.assertEqual({r: chip.regs[r] for r in b.SAVED_REGS}, before)

    def test_error_messages(self):
        cases = ((FakeChip(tag=False), "no answer"), (FakeChip(key=bytes(6)), "not a Bambu"),
                 (FakeChip(collision=True), "several tags"))
        for chip, text in cases:
            with self.assertRaisesRegex(b.RfidError, text):
                driver(chip).read_bambu()

    def test_waits_until_turning_spool_brings_tag_in_range(self):
        clock = FakeClock()
        chip = FakeChip(misses=5)
        d = b.Fm17550(chip, clock.pause)
        uid, tag = d.read_bambu(30., clock.monotonic)
        self.assertEqual(tag["detail"], "PLA Lite")
        self.assertEqual(chip.misses, 0)

    def test_gives_up_after_timeout_and_restores(self):
        clock = FakeClock()
        chip = FakeChip(tag=False)
        before = {r: chip.regs[r] for r in b.SAVED_REGS}
        with self.assertRaisesRegex(b.RfidError, "no answer"):
            b.Fm17550(chip, clock.pause).read_bambu(1., clock.monotonic)
        self.assertGreaterEqual(clock.now, 1.)
        self.assertLess(clock.now, 2.)
        self.assertEqual({r: chip.regs[r] for r in b.SAVED_REGS}, before)

    def test_other_tag_is_not_retried(self):
        clock = FakeClock()
        with self.assertRaises(b.NotBambuTag):
            b.Fm17550(FakeChip(key=bytes(6)), clock.pause).read_bambu(30., clock.monotonic)
        self.assertLess(clock.now, 1.)

    def test_stops_when_stock_read_starts(self):
        clock = FakeClock()
        chip = FakeChip(tag=False)
        before = {r: chip.regs[r] for r in b.SAVED_REGS}

        def abort():
            raise b.RfidError("the stock RFID read is running")
        with self.assertRaisesRegex(b.RfidError, "stock"):
            b.Fm17550(chip, clock.pause).read_bambu(30., clock.monotonic, abort)
        self.assertLess(clock.now, 1.)
        self.assertEqual({r: chip.regs[r] for r in b.SAVED_REGS}, before)

    def test_register_reads_do_not_write(self):
        chip = FakeChip()
        d = driver(chip)
        for _, reg in b.DIAG_REGS:
            d.read_reg(reg)
        self.assertEqual(chip.writes, 0)


class TagEditTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def chip(self, key=b.DEFAULT_KEY, **kwargs):
        return FakeChip(key=key, **kwargs)

    def edit(self, chip):
        return b.Fm17550(chip, self.clock.pause)

    def test_reads_blocks_and_names_the_key(self):
        result = self.edit(self.chip()).read_tag(1., self.clock.monotonic)
        self.assertEqual(result["uid"], UID.hex().upper())
        self.assertEqual(result["key"], "default")
        self.assertEqual(result["blocks"][4], BLOCKS[4].hex().upper())

    def test_excluded_tag_counts_as_no_tag(self):
        chip = self.chip()
        with self.assertRaises(b.TagAbsent):
            self.edit(chip).read_tag(1., self.clock.monotonic, exclude=[UID])
        self.assertEqual(chip.regs[b.REG_T_PRESCALER], 0x55)

    def test_problem_names_the_tag(self):
        with self.assertRaises(b.TagProblem) as caught:
            self.edit(FakeChip()).read_tag(1., self.clock.monotonic)
        self.assertEqual(caught.exception.uid, UID)

    def test_factory_key_is_tried_first(self):
        chip = self.chip(key=bytes.fromhex("D3F7D3F7D3F7"))
        self.assertEqual(self.edit(chip).read_tag(1., self.clock.monotonic)["key"], "qidi")

    def test_reading_writes_nothing_to_the_tag(self):
        chip = self.chip()
        self.edit(chip).read_tag(1., self.clock.monotonic)
        self.assertFalse([f for f in chip.sent if f[0] == 0xA0])


class TrayTests(unittest.TestCase):
    def test_tray_number_is_read_from_the_next_sector(self):
        chip = FakeChip()
        chip.tray_ok = True
        chip.blocks[b.TRAY_BLOCK] = bytes(range(1, 17))
        _, tag = driver(chip).read_bambu()
        self.assertEqual(tag["tray_uid"], bytes(range(1, 17)).hex().upper())

    def test_tray_number_is_read_again_when_the_quick_way_fails(self):
        chip = FakeChip()
        chip.tray_fresh_ok = True
        chip.blocks[b.TRAY_BLOCK] = bytes(range(1, 17))
        uid, tag = driver(chip).read_bambu()
        self.assertEqual(tag["tray_uid"], bytes(range(1, 17)).hex().upper())
        self.assertEqual(uid, UID)

    def test_tray_of_another_tag_is_not_taken(self):
        chip = FakeChip()
        chip.tray_fresh_ok = True
        chip.blocks[b.TRAY_BLOCK] = bytes(range(1, 17))
        reader = driver(chip)
        original = reader.select_tag
        calls = []

        def select():
            calls.append(1)
            return original() if len(calls) == 1 else bytes(4)
        reader.select_tag = select
        self.assertIsNone(reader.read_bambu()[1]["tray_uid"])

    def test_failed_tray_read_keeps_the_tag(self):
        _, tag = driver(FakeChip()).read_bambu()
        self.assertIsNone(tag["tray_uid"])
        self.assertEqual(tag["detail"], "PLA Lite")


class SlotReadTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def read(self, chip, **kwargs):
        return b.Fm17550(chip, self.clock.pause).read_slot_tag(1., self.clock.monotonic, **kwargs)

    def test_returns_the_spool_tag(self):
        uid, tag = self.read(FakeChip())
        self.assertEqual(uid, UID)
        self.assertEqual(tag["detail"], "PLA Lite")

    def test_neighbour_uid_counts_as_no_tag(self):
        with self.assertRaises(b.TagAbsent):
            self.read(FakeChip(), uids=[UID])

    def test_neighbour_tray_counts_as_no_tag(self):
        chip = FakeChip()
        chip.tray_ok = True
        chip.blocks[b.TRAY_BLOCK] = bytes(range(1, 17))
        with self.assertRaises(b.TagAbsent):
            self.read(chip, trays=[bytes(range(1, 17)).hex().upper()])


class UnreadTagTests(unittest.TestCase):
    def test_rejected_key_names_the_tag(self):
        with self.assertRaises(b.NotBambuTag) as caught:
            driver(FakeChip(key=bytes(6))).read_bambu()
        self.assertEqual(caught.exception.uid, UID)
        self.assertEqual(caught.exception.sak, 0x08)

    def test_no_tag_has_no_number(self):
        with self.assertRaises(b.TagAbsent) as caught:
            driver(FakeChip(tag=False)).read_bambu()
        self.assertIsNone(caught.exception.uid)


class TagRulesTests(unittest.TestCase):
    def write(self, data):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            f.write(data)
        self.addCleanup(os.remove, f.name)
        return f.name

    def test_good_rules_are_read_bad_ones_left_out(self):
        path = self.write('{"5B692AF4": {"filament": "QFA01", "color": "FAFAFA"},'
                          ' "bad": {"filament": "QFA01", "color": "FAFAFA"},'
                          ' "7081205A": {"filament": "PLA", "color": "FAFAFA"}}')
        self.assertEqual(b.load_tag_rules(path), {"5B692AF4": {"filament": "QFA01", "color": "FAFAFA"}})

    def test_missing_or_broken_file_means_no_rules(self):
        self.assertEqual(b.load_tag_rules("/no/such/file.json"), {})
        self.assertEqual(b.load_tag_rules(self.write("[1, 2")), {})


class QidiTagTests(unittest.TestCase):
    def factory_chip(self, color=6):
        chip = FakeChip(key=dict(b.TAG_KEYS)["qidi"])
        chip.blocks[4] = bytes([1, color, 1]) + bytes(13)
        return chip

    def test_factory_tag_gives_material_and_color(self):
        block4, tag = driver(self.factory_chip()).read_qidi()
        self.assertEqual(block4[0], 1)
        self.assertEqual((tag["color"], tag["uid"], tag["detail"]), (b.QIDI_COLORS[6], UID.hex().upper(), "QIDI"))
        self.assertEqual(tag["tag_color"], tag["color"])

    def test_factory_tag_with_the_default_key(self):
        chip = self.factory_chip()
        chip.key = b.DEFAULT_KEY
        self.assertEqual(driver(chip).read_qidi()[1]["color"], b.QIDI_COLORS[6])

    def test_other_key_or_bad_color_is_not_a_factory_tag(self):
        self.assertIsNone(driver(FakeChip(key=bytes(6))).read_qidi())
        self.assertIsNone(driver(self.factory_chip(color=99)).read_qidi())

    def test_rejected_key_does_not_wait_out_every_poll(self):
        pauses = []
        reader = b.Fm17550(FakeChip(key=bytes(6)), pauses.append)
        reader.read_qidi()
        self.assertLess(len(pauses), 10)

    def test_custom_read_of_a_locked_tag_is_none(self):
        self.assertIsNone(driver(self.factory_chip()).read_custom())


class StockSettingsTests(unittest.TestCase):
    def test_keeps_only_move_numbers(self):
        class Stepper:
            slot_load_length_1 = 120.
            slot_load_length_1_speed = 80
            unload_length_name = "text"
            speed_enabled = True
            name = "slot0"
            _private_length = 1.

            @property
            def broken_length(self):
                raise ValueError("no value")
        self.assertEqual(b.stock_settings(Stepper()),
                         {"slot_load_length_1": 120., "slot_load_length_1_speed": 80})


class ObjectValuesTests(unittest.TestCase):
    def test_keeps_values_and_names_other_objects(self):
        class Stock:
            b_endstop_state = 1
            label = "x"
            pins = [1]

            def method(self):
                pass

            @property
            def broken(self):
                raise ValueError("no value")
        self.assertEqual(b.object_values(Stock()), {"b_endstop_state": 1, "label": "x", "pins": "list"})


class SwingReadTests(unittest.TestCase):
    def run_swing(self, answers, back=20., forward=10., gone_at=None):
        answers, log = iter(answers), []

        def read():
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        def spool_gone():
            return gone_at is not None and sum(x for x in log if x != "release") <= gone_at
        try:
            result = b.swing_read(read, log.append, lambda: log.append("release"), back, forward, spool_gone)
        except b.RfidError as e:
            result = e
        return result, log

    def test_found_at_once_moves_nothing(self):
        self.assertEqual(self.run_swing(["tag"]), ("tag", ["release"]))

    def test_goes_back_first_and_returns(self):
        result, log = self.run_swing([b.TagAbsent("x"), "tag"])
        self.assertEqual(result, "tag")
        self.assertEqual(log, [-10., 10., "release"])

    def test_found_on_the_forward_leg_returns(self):
        result, log = self.run_swing([b.TagAbsent("x")] * 5 + ["tag"])
        self.assertEqual(result, "tag")
        self.assertEqual(log, [-10., -10., 10., 10., "release"])

    def test_not_found_covers_both_legs_and_returns(self):
        result, log = self.run_swing([b.TagAbsent("x")] * 9)
        self.assertIsInstance(result, b.TagAbsent)
        self.assertEqual(log, [-10., -10., 10., 10., 10., -10., "release"])

    def test_spool_gone_ends_the_back_leg(self):
        result, log = self.run_swing([b.TagAbsent("x")] * 9, back=50., gone_at=-20.)
        self.assertIsInstance(result, b.TagAbsent)
        self.assertEqual(log, [-10., -10., 10., 10., 10., -10., "release"])

    def test_several_tags_keep_rocking(self):
        result, log = self.run_swing([b.SeveralTags("x"), "tag"])
        self.assertEqual(result, "tag")

    def test_another_problem_still_returns(self):
        result, log = self.run_swing([b.TagAbsent("x"), b.NotBambuTag("x")])
        self.assertIsInstance(result, b.NotBambuTag)
        self.assertEqual(log, [-10., 10., "release"])

    def test_motor_is_released_when_the_way_back_fails(self):
        log = []

        def move(distance):
            log.append(distance)
            if distance > 0:
                raise ValueError("bus")

        def read():
            raise b.TagAbsent("x")
        with self.assertRaises(ValueError):
            b.swing_read(read, move, lambda: log.append("release"), 10., 0., lambda: False)
        self.assertEqual(log, [-10., 10., 10., "release"])


class NeighbourAsideTests(unittest.TestCase):
    def test_neighbour_goes_back_before_the_read_and_returns(self):
        log = []
        result = b.with_neighbour_aside(lambda: log.append("read") or "tag", log.append,
                                        lambda: log.append("release"), 30.)
        self.assertEqual(result, "tag")
        self.assertEqual(log, [-10., -10., -10., "read", 30., "release"])

    def test_neighbour_returns_when_the_read_fails(self):
        log = []

        def read():
            raise b.TagAbsent("x")
        with self.assertRaises(b.TagAbsent):
            b.with_neighbour_aside(read, log.append, lambda: log.append("release"), 20.)
        self.assertEqual(log, [-10., -10., 20., "release"])

    def test_only_the_part_moved_is_returned(self):
        log = []

        failed = []

        def move(distance):
            if len(log) == 1 and not failed:
                failed.append(distance)
                raise ValueError("bus")
            log.append(distance)
        with self.assertRaises(ValueError):
            b.with_neighbour_aside(lambda: "tag", move, lambda: log.append("release"), 30.)
        self.assertEqual(log, [-10., 10., "release"])


class MapFileTests(unittest.TestCase):
    def write(self, text):
        f = tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False, encoding="utf-8")
        f.write(text)
        f.close()
        self.addCleanup(os.remove, f.name)
        return f.name

    def test_shipped_file_matches_built_in_table(self):
        path = str(PACKAGES / "bambu" / "printer" / "bambu_map.cfg")
        materials, colors = b.load_map(path)
        self.assertEqual(sorted(materials), sorted(b.QIDI_MATERIALS))
        self.assertEqual(colors, {})

    def test_material_and_color_overrides_apply(self):
        mapping = b.load_map(self.write(
            "[materials]\nPLA Lite = 2 # comment\n[colors]\n#EFE255 = #112233\nefe255 = 112233\n"))
        tag = b.parse_bambu(BLOCKS[4], BLOCKS[5], BLOCKS[6], mapping)
        self.assertEqual(tag["qidi_material"], 2)
        self.assertEqual(tag["color"], "112233")

    def test_colors_only_keeps_built_in_materials(self):
        materials, _ = b.load_map(self.write("[colors]\nEFE255 = 112233\n"))
        self.assertEqual(materials, b.QIDI_MATERIALS)

    def test_invalid_values_are_rejected(self):
        for text in ("[materials]\nPLA = abc\n", "[materials]\nPLA = QF\n", "[materials]\nPLA = 0\n",
                     "[colors]\nEFE255 = red\n", "not a config"):
            with self.assertRaises(ValueError):
                b.load_map(self.write(text))

    def test_missing_file_is_rejected(self):
        with self.assertRaises(ValueError):
            b.load_map("/nonexistent/bambu_map.cfg")

    def test_driver_applies_mapping(self):
        d = b.Fm17550(FakeChip(), lambda delay: None,
                      lambda: (b.QIDI_MATERIALS, {"EFE255": "000000"}))
        self.assertEqual(d.read_bambu()[1]["color"], "000000")


class SlotUpdateTests(unittest.TestCase):
    TAG = {"qidi_color": 22, "color": "D3B7A7", "tag_color": "D3B7A7", "uid": "55365C10",
           "detail": "PLA Matte", "profile": None}

    def test_saves_exact_color_and_real_tag_record(self):
        scripts = b.slot_update_scripts(
            {"color_slot2": ["CAC59F"], "filament_slot2": "QFA01"}, "slot2", self.TAG)
        self.assertEqual(scripts[0], "SAVE_VARIABLE VARIABLE=color_slot2 VALUE=\"['D3B7A7']\"")
        self.assertTrue(scripts[1].startswith("SAVE_VARIABLE VARIABLE=bambu_tag_slot2 VALUE=\"{'uid': '55365C10'"))
        self.assertIn("'detail': 'PLA Matte', 'tag_color': 'D3B7A7', 'color': 'D3B7A7', 'filament': 'QFA01'", scripts[1])

    def test_record_keeps_all_tag_data(self):
        tag = b.parse_bambu(BLOCKS[4], BLOCKS[5], BLOCKS[6])
        tag["uid"] = "7081205A"
        scripts = b.slot_update_scripts({"color_slot0": [b.QIDI_COLORS[tag["qidi_color"]]]}, "slot0", tag)
        for text in ("'brand': 'Bambu Lab'", "'weight': 1000", "'diameter': 1.75", "'min_temp': 190",
                     "'max_temp': 240", "'dry_temp': 55", "'dry_hours': 8"):
            self.assertIn(text, scripts[-1])

    def test_mapped_profile_replaces_filament(self):
        tag = dict(self.TAG, profile="QFL30")
        scripts = b.slot_update_scripts(
            {"color_slot2": ["CAC59F"], "filament_slot2": "QFA01"}, "slot2", tag)
        self.assertIn("SAVE_VARIABLE VARIABLE=filament_slot2 VALUE=\"'QFL30'\"", scripts)
        self.assertIn("'filament': 'QFL30'", scripts[-1])

    def test_leaves_slot_alone_when_stock_did_not_save(self):
        self.assertEqual(b.slot_update_scripts({"color_slot2": ["060606"]}, "slot2", self.TAG), [])
        self.assertEqual(b.slot_update_scripts({}, "slot2", self.TAG), [])

    def test_palette_color_still_records_the_tag(self):
        tag = dict(self.TAG, qidi_color=2, color="060606", tag_color="060606")
        scripts = b.slot_update_scripts({"color_slot0": ["060606"], "filament_slot0": "QFA01"}, "slot0", tag)
        self.assertEqual(len(scripts), 1)
        self.assertIn("bambu_tag_slot0", scripts[0])

    def test_quotes_in_tag_name_cannot_break_the_command(self):
        tag = dict(self.TAG, detail="PLA'\"\nBAD")
        scripts = b.slot_update_scripts({"color_slot2": ["CAC59F"]}, "slot2", tag)
        self.assertIn("'detail': 'PLABAD'", scripts[-1])


class MapProfileTests(unittest.TestCase):
    def test_profile_in_map_keeps_legacy_code_for_stock(self):
        mapping = (("PLA Lite", "QFL30"),), {"EFE255": "112233"}
        tag = b.parse_bambu(BLOCKS[4], BLOCKS[5], BLOCKS[6], mapping)
        self.assertEqual(tag["profile"], "QFL30")
        self.assertEqual(tag["qidi_material"], 7)
        self.assertEqual((tag["tag_color"], tag["color"]), ("EFE255", "112233"))


class SpoolNumberTests(unittest.TestCase):
    """Our tag carries a spool number in block 6; both tags of a spool share it, like a Bambu tray."""
    SPOOL = bytes.fromhex("0123456789ABCDEF")

    def chip(self, block6):
        chip = FakeChip(key=b.DEFAULT_KEY)
        chip.blocks[4] = bytes([2, 22, 1]) + bytes(13)
        chip.blocks[5] = b.CUSTOM_COLOR_MAGIC + bytes.fromhex("D3B7A7") + bytes(9)
        chip.blocks[6] = block6
        return chip

    def named_chip(self, maker="eSun", name="PETG+ синий"):
        chip = self.chip(b.SPOOL_MAGIC + self.SPOOL + bytes([b.NAME_FLAG]) + bytes(3))
        chip.sector2_key = b.DEFAULT_KEY
        for block, data in zip(b.NAME_BLOCKS, b.name_blocks(maker, name)):
            chip.blocks[block] = data
        return chip

    def test_maker_and_name_come_from_sector_2(self):
        _, tag = driver(self.named_chip()).read_custom()
        self.assertEqual((tag["brand"], tag["detail"]), ("eSun", "PETG+ синий"))
        _, tag = driver(self.named_chip()).read_qidi_format()
        self.assertEqual((tag["brand"], tag["detail"]), ("eSun", "PETG+ синий"))

    def test_without_the_flag_sector_2_is_not_read(self):
        chip = self.named_chip()
        chip.blocks[6] = b.SPOOL_MAGIC + self.SPOOL + bytes(4)
        _, tag = driver(chip).read_custom()
        self.assertEqual(tag["detail"], "Своя метка")
        self.assertFalse([f for f in chip.sent if f[:2] == [0x30, 8]])

    def test_name_blocks_round_trip_and_limit(self):
        self.assertEqual(b.parse_name(b.name_blocks("eSun", "PETG+")), ("eSun", "PETG+"))
        self.assertIsNone(b.parse_name([bytes(16)] * 3))
        with self.assertRaises(ValueError):
            b.name_blocks("x" * 30, "y" * 20)

    def test_tag_read_gives_the_name_blocks(self):
        result = driver(self.named_chip()).read_tag(1., lambda: 0.)
        self.assertEqual(b.parse_name([bytes.fromhex(result["blocks"][n]) for n in b.NAME_BLOCKS]), ("eSun", "PETG+ синий"))

    def test_spool_number_is_read_as_tray(self):
        _, tag = driver(self.chip(b.SPOOL_MAGIC + self.SPOOL + bytes(4))).read_custom()
        self.assertEqual(tag["tray_uid"], "0123456789ABCDEF")

    def test_no_spool_number_without_magic(self):
        for block6 in (bytes(16), b"QXSX" + self.SPOOL + bytes(4), b.SPOOL_MAGIC + bytes(12)):
            self.assertIsNone(driver(self.chip(block6)).read_custom()[1]["tray_uid"])

    def test_insertion_read_saves_the_tray_in_the_slot_record(self):
        seen = []
        chip = self.chip(b.SPOOL_MAGIC + self.SPOOL + bytes(4))
        proxy = b.StockReadProxy(FakeRealRead(), driver(chip), lambda m: None,
                                 FakeClock().monotonic, seen.append)
        proxy.send([1])
        scripts = b.slot_update_scripts({"color_slot1": ["CAC59F"]}, "slot1", seen[0])
        self.assertIn("'tray': '0123456789ABCDEF'", scripts[-1])

    def test_slot_read_carries_the_tray(self):
        chip = self.chip(b.SPOOL_MAGIC + self.SPOOL + bytes(4))
        reader = types.SimpleNamespace(bambu_tags=False, custom_tags=True, chip=driver(chip),
                                       mapping_for_uid=lambda uid: None)
        result = b.BambuRfid._read_slot_tag(reader, 1., [], [])
        self.assertEqual(result["tray"], "0123456789ABCDEF")

    def test_custom_tag_is_read_with_one_opening(self):
        chip = self.chip(b.SPOOL_MAGIC + self.SPOOL + bytes(4))
        found = driver(chip).read_qidi_format()
        self.assertEqual(found[1]["detail"], "Своя метка")
        self.assertEqual(len([f for f in chip.sent if f[:2] == [0x93, 0x70]]), 1)

    def test_neighbours_second_tag_counts_as_no_tag(self):
        chip = self.chip(b.SPOOL_MAGIC + self.SPOOL + bytes(4))
        reader = types.SimpleNamespace(bambu_tags=False, custom_tags=True, chip=driver(chip),
                                       mapping_for_uid=lambda uid: None)
        with self.assertRaises(b.TagAbsent):
            b.BambuRfid._read_slot_tag(reader, 1., [], ["0123456789ABCDEF"])


class FakeRealRead:
    def __init__(self):
        self.calls = 0

    def send(self, args):
        self.calls += 1
        return {"status": 1, "data": b"stock"}


class TagSwitchTests(unittest.TestCase):
    class Chip:
        def __init__(self):
            self.calls = []

        def read_bambu(self):
            self.calls.append("bambu")
            raise b.NotBambuTag("no")

        def read_custom(self):
            self.calls.append("custom")
            return None

    def reads(self, **switches):
        chip = self.Chip()
        real = FakeRealRead()
        b.StockReadProxy(real, chip, lambda m: None, lambda: 0., **switches).send([])
        return chip.calls

    def test_each_kind_of_tag_is_read_only_when_on(self):
        self.assertEqual(self.reads(), ["bambu", "custom"])
        self.assertEqual(self.reads(bambu_tags=False), ["custom"])
        self.assertEqual(self.reads(custom_tags=False), ["bambu"])
        self.assertEqual(self.reads(bambu_tags=False, custom_tags=False), [])


class ProxyTests(unittest.TestCase):
    def make(self, chip):
        self.real = FakeRealRead()
        self.reports = []
        self.clock = FakeClock()
        return b.StockReadProxy(self.real, driver(chip), self.reports.append,
                                self.clock.monotonic)

    def test_bambu_tag_is_answered_with_legacy_codes(self):
        proxy = self.make(FakeChip())
        result = proxy.send([1])
        self.assertEqual(result["status"], 1)
        self.assertEqual(result["data"], bytes([7, 8, 1]) + bytes(13))
        self.assertEqual(self.real.calls, 0)

    def test_repeated_reads_match_and_report_once(self):
        # The stock code compares two reads before saving them.
        proxy = self.make(FakeChip())
        self.assertEqual(proxy.send([1]), proxy.send([1]))
        self.assertEqual(len(self.reports), 1)

    def test_answer_repeats_after_tag_turned_away(self):
        chip = FakeChip()
        proxy = self.make(chip)
        first = proxy.send([1])
        chip.tag = False
        self.clock.now += 0.2
        self.assertEqual(proxy.send([1]), first)
        self.assertEqual(self.real.calls, 0)

    def test_held_answer_expires(self):
        chip = FakeChip()
        proxy = self.make(chip)
        proxy.send([1])
        chip.tag = False
        self.clock.now += b.ANSWER_HOLD + 0.1
        self.assertEqual(proxy.send([1])["status"], 0)

    def test_exact_color_scheduled_once_per_real_read(self):
        seen = []
        chip = FakeChip()
        proxy = b.StockReadProxy(FakeRealRead(), driver(chip), lambda m: None,
                                 FakeClock().monotonic, seen.append)
        proxy.send([1])
        proxy.send([1])
        self.assertEqual([tag["color"] for tag in seen], ["EFE255"])

    def custom_chip(self, block5):
        chip = FakeChip(key=b.DEFAULT_KEY)
        chip.blocks[4] = bytes([2, 22, 1]) + bytes(13)
        chip.blocks[5] = block5
        return chip

    def test_our_tag_answers_block_4_and_schedules_exact_color(self):
        seen = []
        chip = self.custom_chip(b.CUSTOM_COLOR_MAGIC + bytes.fromhex("D3B7A7") + bytes(9))
        proxy = b.StockReadProxy(FakeRealRead(), driver(chip), lambda m: None,
                                 FakeClock().monotonic, seen.append)
        self.assertEqual(proxy.send([1])["data"], bytes([2, 22, 1]) + bytes(13))
        self.assertEqual((seen[0]["color"], seen[0]["qidi_color"]), ("D3B7A7", 22))
        scripts = b.slot_update_scripts({"color_slot1": ["CAC59F"]}, "slot1", seen[0])
        self.assertIn("D3B7A7", scripts[0])

    def test_our_tag_carries_profile(self):
        seen = []
        chip = self.custom_chip(b.CUSTOM_COLOR_MAGIC + bytes.fromhex("D3B7A7") + b"QFG00\0" + bytes(3))
        proxy = b.StockReadProxy(FakeRealRead(), driver(chip), lambda m: None,
                                 FakeClock().monotonic, seen.append)
        proxy.send([1])
        self.assertEqual(seen[0]["profile"], "QFG00")

    def test_plain_default_key_tag_goes_to_stock_read(self):
        proxy = self.make(self.custom_chip(bytes(16)))
        self.assertEqual(proxy.send([1])["data"], b"stock")
        self.assertEqual(self.real.calls, 1)

    def stock_proxy(self, answer):
        seen = []

        class Real:
            def send(self, args):
                return answer
        proxy = b.StockReadProxy(Real(), driver(FakeChip(key=bytes(6))), lambda m: None,
                                 FakeClock().monotonic, None, lambda: seen.append(1))
        proxy.send([1])
        return seen

    def test_stock_read_with_valid_codes_is_noted(self):
        self.assertEqual(self.stock_proxy({"status": 1, "data": bytes([1, 2, 1]) + bytes(13)}), [1])

    def test_noise_or_failed_stock_read_is_not_noted(self):
        self.assertEqual(self.stock_proxy({"status": 1, "data": bytes([200, 77, 1])}), [])
        self.assertEqual(self.stock_proxy({"status": 0, "data": b""}), [])

    def test_stock_record_holds_slot_values(self):
        script = b.stock_record_script({"color_slot1": ["060606"], "filament_slot1": "QFA02"}, "slot1")
        self.assertIn("VARIABLE=bambu_tag_slot1", script)
        for text in ("'stock': True", "'color': '060606'", "'filament': 'QFA02'"):
            self.assertIn(text, script)

    def test_other_tag_goes_to_stock_read(self):
        proxy = self.make(FakeChip(key=bytes(6)))
        self.assertEqual(proxy.send([1])["data"], b"stock")
        self.assertEqual(self.real.calls, 1)

    def test_no_tag_skips_stock_read(self):
        proxy = self.make(FakeChip(tag=False))
        self.assertEqual(proxy.send([1])["status"], 0)
        self.assertEqual(self.real.calls, 0)

    def test_chip_is_restored_before_stock_read(self):
        chip = FakeChip(key=bytes(6))
        before = {r: chip.regs[r] for r in b.SAVED_REGS}
        self.make(chip).send([1])
        self.assertEqual({r: chip.regs[r] for r in b.SAVED_REGS}, before)

    def test_unknown_material_goes_to_stock_read(self):
        original = BLOCKS[4]
        BLOCKS[4] = b"Unknown".ljust(16, b"\0")
        try:
            proxy = self.make(FakeChip())
            self.assertEqual(proxy.send([1])["data"], b"stock")
        finally:
            BLOCKS[4] = original

    def test_unexpected_error_does_not_escape(self):
        class Broken(FakeChip):
            def spi_transfer(self, data):
                raise ValueError("bus failure")
        proxy = self.make(Broken())
        self.assertEqual(proxy.send([1])["data"], b"stock")

    def test_busy_reader_reports_no_tag(self):
        proxy = self.make(FakeChip())
        proxy.busy = True
        self.assertEqual(proxy.send([1])["status"], 0)
        self.assertEqual(self.real.calls, 0)

    def test_other_attributes_reach_the_real_command(self):
        proxy = self.make(FakeChip())
        self.real.marker = 5
        self.assertEqual(proxy.marker, 5)


if __name__ == "__main__":
    unittest.main()
