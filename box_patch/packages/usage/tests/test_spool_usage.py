"""Tests for bambu_spool_usage: the spool count and the screen window signal."""
import pathlib
import sys

PACKAGES = pathlib.Path(__file__).resolve().parent.parent.parent
for folder in ("usage/printer",):
    sys.path.insert(0, str(PACKAGES / folder))
import json
import os
import tempfile
import unittest

import bambu_spool_usage as b


class SpoolKeyTests(unittest.TestCase):
    def test_tray_then_uid_then_slot(self):
        self.assertEqual(b.spool_key({"bambu_tag_slot1": {"uid": "A1", "tray": "T1"}}, 1), "tray:T1")
        self.assertEqual(b.spool_key({"bambu_tag_slot1": {"uid": "A1"}}, 1), "uid:A1")
        self.assertEqual(b.spool_key({"bambu_tag_slot1": {"empty": True}}, 1), "slot:1")
        self.assertEqual(b.spool_key({}, 2), "slot:2")


class ScreenWarningsTests(unittest.TestCase):
    def make(self):
        class Controller:
            def get_status(self, eventtime=None):
                return {"box_warning_info": ["stock"], "enable_box": 1}
        self.controller = Controller()
        self.commands, self.handlers, self.later = {}, {}, []
        outer = self

        class Gcode:
            def register_command(self, name, func, desc=None):
                outer.commands[name] = func

        class Reactor:
            def monotonic(self):
                return 0.

            def register_callback(self, func, when):
                outer.later.append(func)

        class Printer:
            def get_reactor(self):
                return Reactor()

            def register_event_handler(self, name, func):
                outer.handlers[name] = func

            def lookup_object(self, name, default=None):
                return {"gcode": Gcode(), "multi_color_controller": outer.controller}.get(name, default)
        warnings = b.ScreenWarnings(Printer())
        self.handlers["klippy:ready"]()
        return warnings

    class Command:
        def get_int(self, name, minval=None, maxval=None):
            return 4

    def test_warning_appears_with_the_stock_ones_and_goes_away(self):
        self.make()
        self.assertEqual(self.controller.get_status()["box_warning_info"], ["stock"])
        self.commands["BAMBU_SCREEN_WARNING"](self.Command())
        listed = self.controller.get_status()["box_warning_info"]
        self.assertEqual(listed[0], "stock")
        self.assertIn("BOX 4 temperature", listed[1])
        self.assertEqual(self.controller.get_status()["enable_box"], 1)
        self.later[0](0.)
        self.assertEqual(self.controller.get_status()["box_warning_info"], ["stock"])


class ScreenRequestTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.path = os.path.join(self.folder, b.SCREEN_REQUEST_FILE)

    def write(self, text):
        with open(self.path, "w") as f:
            f.write(text)
        return os.path.getmtime(self.path)

    def test_request_is_taken_once(self):
        now = self.write("3 print\n")
        self.assertEqual(b.take_screen_request(self.folder, now), (3, True))
        self.assertFalse(os.path.exists(self.path))
        self.assertIsNone(b.take_screen_request(self.folder, now))

    def test_plain_request_and_bad_ones(self):
        self.assertEqual(b.take_screen_request(self.folder, self.write("4")), (4, False))
        self.assertIsNone(b.take_screen_request(self.folder, self.write("1 print")))
        now = self.write("3 print")
        self.assertIsNone(b.take_screen_request(self.folder, now + b.SCREEN_REQUEST_AGE + 1))
        self.assertFalse(os.path.exists(self.path))


class SpoolLedgerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = os.path.join(folder.name, b.USAGE_FILE)

    def seen_with_tray(self):
        ledger = b.SpoolLedger(self.path)
        ledger.set_remaining("tray:T", 250)
        ledger.add("tray:T", 3)
        ledger.adopt({"bambu_tag_slot1": {"uid": "A", "tray": "T", "weight": 1000}})
        return ledger

    def test_read_without_tray_keeps_the_count(self):
        ledger = self.seen_with_tray()
        ledger.adopt({"bambu_tag_slot1": {"uid": "A", "weight": 1000}})
        self.assertEqual(ledger.view("uid:A")["remaining"], 247)
        self.assertNotIn("uid:A", ledger.spools)

    def test_link_survives_a_restart(self):
        ledger = self.seen_with_tray()
        ledger.save()
        again = b.SpoolLedger(self.path)
        self.assertEqual(again.view("uid:A")["remaining"], 247)

    def test_printing_and_setting_without_tray_reach_the_spool(self):
        ledger = self.seen_with_tray()
        ledger.add("uid:A", 7)
        self.assertEqual(ledger.view("tray:T")["remaining"], 240)
        ledger.set_remaining("uid:A", 100)
        self.assertEqual(ledger.view("tray:T")["remaining"], 100)
        self.assertEqual(ledger.view("uid:A")["remaining"], 100)
        self.assertEqual(ledger.spools["tray:T"]["uids"], ["A"])

    def test_automatic_full_spool_gives_way_to_the_real_count(self):
        ledger = b.SpoolLedger(self.path)
        ledger.set_remaining("tray:T", 250)
        ledger.add("tray:T", 3)
        ledger.adopt({"bambu_tag_slot1": {"uid": "A", "weight": 1000}})
        ledger.add("uid:A", 2)
        ledger.adopt({"bambu_tag_slot1": {"uid": "A", "tray": "T", "weight": 1000}})
        self.assertEqual(ledger.view("tray:T")["remaining"], 245)
        self.assertNotIn("uid:A", ledger.spools)

    def test_value_set_by_hand_under_the_tag_wins(self):
        ledger = b.SpoolLedger(self.path)
        ledger.set_remaining("tray:T", 250)
        ledger.set_remaining("uid:A", 90)
        ledger.adopt({"bambu_tag_slot1": {"uid": "A", "tray": "T"}})
        self.assertEqual(ledger.view("tray:T")["remaining"], 90)

    def test_both_tags_of_a_spool_share_its_count(self):
        ledger = self.seen_with_tray()
        ledger.adopt({"bambu_tag_slot1": {"uid": "B", "tray": "T"}})
        self.assertEqual(ledger.view("uid:B")["remaining"], 247)

    def test_printing_subtracts_from_what_was_set(self):
        ledger = b.SpoolLedger(self.path)
        ledger.set_remaining("uid:A", 800)
        ledger.add("uid:A", 30.5)
        self.assertAlmostEqual(ledger.view("uid:A")["remaining"], 769.5)

    def test_setting_again_starts_over(self):
        ledger = b.SpoolLedger(self.path)
        ledger.set_remaining("uid:A", 800)
        ledger.add("uid:A", 30)
        ledger.set_remaining("uid:A", 500)
        self.assertEqual(ledger.view("uid:A")["remaining"], 500)

    def test_never_set_counts_but_has_no_remaining(self):
        ledger = b.SpoolLedger(self.path)
        ledger.add("uid:A", 12)
        self.assertIsNone(ledger.view("uid:A")["remaining"])
        self.assertEqual(ledger.view("uid:A")["used"], 12)

    def test_remaining_stops_at_zero(self):
        ledger = b.SpoolLedger(self.path)
        ledger.set_remaining("uid:A", 5)
        ledger.add("uid:A", 9)
        self.assertEqual(ledger.view("uid:A")["remaining"], 0)

    def test_new_spool_starts_from_the_tag_weight(self):
        ledger = b.SpoolLedger(self.path)
        ledger.adopt({"bambu_tag_slot1": {"uid": "A", "weight": 750}})
        self.assertEqual(ledger.view("uid:A")["remaining"], 750)

    def test_tag_without_weight_counts_as_usual_spool(self):
        ledger = b.SpoolLedger(self.path)
        ledger.adopt({"bambu_tag_slot0": {"stock": True}})
        self.assertEqual(ledger.view("slot:0")["remaining"], b.USUAL_SPOOL_GRAMS)

    def test_known_spool_is_not_refilled(self):
        ledger = b.SpoolLedger(self.path)
        ledger.set_remaining("uid:A", 300)
        ledger.add("uid:A", 20)
        ledger.adopt({"bambu_tag_slot1": {"uid": "A", "weight": 1000}})
        self.assertEqual(ledger.view("uid:A")["remaining"], 280)

    def test_empty_slot_is_not_a_spool(self):
        ledger = b.SpoolLedger(self.path)
        ledger.adopt({"bambu_tag_slot0": {"empty": True}})
        self.assertEqual(ledger.spools, {})

    def test_tray_number_takes_over_the_count_of_its_tag(self):
        ledger = b.SpoolLedger(self.path)
        ledger.set_remaining("uid:A", 300)
        ledger.adopt({"bambu_tag_slot1": {"uid": "A", "tray": "T", "weight": 1000}})
        self.assertEqual(ledger.view("tray:T")["remaining"], 300)
        self.assertNotIn("uid:A", ledger.spools)

    def test_request_file_is_used_once(self):
        folder = os.path.dirname(self.path)
        with open(os.path.join(folder, b.SPOOL_REQUEST_FILE % 2), "w") as f:
            f.write("750\n")
        self.assertEqual(b.take_spool_request(folder, 2), 750)
        self.assertIsNone(b.take_spool_request(folder, 2))

    def test_bad_request_is_dropped(self):
        folder = os.path.dirname(self.path)
        for text in ("abc", "99999", "-1"):
            with open(os.path.join(folder, b.SPOOL_REQUEST_FILE % 0), "w") as f:
                f.write(text)
            self.assertIsNone(b.take_spool_request(folder, 0))
            self.assertFalse(os.path.exists(os.path.join(folder, b.SPOOL_REQUEST_FILE % 0)))

    def test_survives_a_restart(self):
        ledger = b.SpoolLedger(self.path)
        ledger.set_remaining("uid:A", 800)
        ledger.add("uid:A", 10)
        ledger.save()
        self.assertEqual(b.SpoolLedger(self.path).view("uid:A")["remaining"], 790)

    def test_broken_file_starts_empty(self):
        with open(self.path, "w") as f:
            f.write("{broken")
        self.assertEqual(b.SpoolLedger(self.path).spools, {})


BLACK_TAG = {"uid": "A", "tray": "T", "color": "000000", "filament": "QFA01", "weight": 1000}
PLASTIC = {"name": "eSun PLA+", "filament": "QFA00", "color": "A878FF", "grams": 1000}


def slot(filament, color, record=None):
    variables = {"filament_slot1": filament, "color_slot1": [color]}
    if record is not None:
        variables["bambu_tag_slot1"] = record
    return variables


class TaglessSpoolTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.ledger = b.SpoolLedger(os.path.join(self.folder, b.USAGE_FILE))

    def test_tag_of_an_earlier_spool_is_not_counted(self):
        # The tag record left from another spool: the slot has another color now.
        self.assertEqual(b.spool_key(slot("QFA01", "000000", BLACK_TAG), 1), "tray:T")
        self.assertEqual(b.spool_key(slot("QFA01", "A878FF", BLACK_TAG), 1), "slot:1")
        self.assertEqual(b.spool_key(slot("QFA00", "000000", BLACK_TAG), 1), "slot:1")
        self.assertEqual(b.spool_key(slot("QFA00", "A878FF", {"uid": "U", "unread": True}), 1), "uid:U")
        self.ledger.adopt(slot("QFA01", "A878FF", BLACK_TAG))
        self.assertEqual(self.ledger.spools, {})

    def test_count_holds_while_the_slot_keeps_its_plastic(self):
        self.ledger.set_remaining("slot:1", 500, ["QFA00", "A878FF"])
        self.ledger.add("slot:1", 20, ["QFA00", "A878FF"])
        self.assertEqual(self.ledger.view("slot:1", ["QFA00", "A878FF"])["remaining"], 480)
        # Another spool without a tag, of another color: unknown, and printing starts it anew.
        self.assertIsNone(self.ledger.view("slot:1", ["QFA00", "FFFFFF"])["remaining"])
        self.ledger.add("slot:1", 5, ["QFA00", "FFFFFF"])
        self.assertEqual(self.ledger.view("slot:1", ["QFA00", "FFFFFF"])["used"], 5)
        self.assertIsNone(self.ledger.view("slot:1", ["QFA00", "A878FF"])["remaining"])

    def test_pick_sets_count_name_and_slot(self):
        scripts = self.ledger.pick(1, PLASTIC, ["QFA01", "000000"], printing=False)
        self.assertEqual(scripts, ["SAVE_VARIABLE VARIABLE=filament_slot1 VALUE=\"'QFA00'\"",
                                   "SAVE_VARIABLE VARIABLE=color_slot1 VALUE=\"['A878FF']\"",
                                   "SAVE_VARIABLE VARIABLE=bambu_tag_slot1 VALUE=\"{'empty': True}\""])
        view = self.ledger.view("slot:1", ["QFA00", "A878FF"])
        self.assertEqual((view["remaining"], view["full"], view["name"]), (1000, 1000, "eSun PLA+"))
        self.assertEqual(self.ledger.pending(), [])

    def test_pick_during_a_print_waits_for_its_end(self):
        self.assertEqual(self.ledger.pick(1, dict(PLASTIC, grams=700), ["QFA01", "000000"], printing=True), [])
        # Meanwhile the slot still has the old material and color, and the count is the pick's.
        self.ledger.add("slot:1", 10, ["QFA01", "000000"])
        self.assertEqual(self.ledger.view("slot:1", ["QFA01", "000000"])["remaining"], 690)
        self.assertEqual(len(self.ledger.pending()), 3)
        self.assertEqual(self.ledger.pending(), [])
        self.assertEqual(self.ledger.view("slot:1", ["QFA00", "A878FF"])["remaining"], 690)
        self.assertIsNone(self.ledger.view("slot:1", ["QFA01", "000000"])["remaining"])

    def test_setting_grams_by_hand_keeps_the_name(self):
        self.ledger.pick(1, PLASTIC, ["QFA01", "000000"], printing=False)
        self.ledger.set_remaining("slot:1", 300, ["QFA00", "A878FF"])
        view = self.ledger.view("slot:1", ["QFA00", "A878FF"])
        self.assertEqual((view["remaining"], view["name"]), (300, "eSun PLA+"))

    def test_pick_request(self):
        path = os.path.join(self.folder, b.PICK_REQUEST_FILE % 1)
        with open(path, "w") as f:
            json.dump(dict(PLASTIC, name="  eSun   PLA+ ", color="a878ff"), f)
        self.assertEqual(b.take_pick_request(self.folder, 1), PLASTIC)
        self.assertIsNone(b.take_pick_request(self.folder, 1))
        for bad in (dict(PLASTIC, filament="PLA"), dict(PLASTIC, color="red"), dict(PLASTIC, grams=9999),
                    dict(PLASTIC, grams=True), dict(PLASTIC, name=" "), {"name": "x"}):
            with open(path, "w") as f:
                json.dump(bad, f)
            self.assertIsNone(b.take_pick_request(self.folder, 1))
            self.assertFalse(os.path.exists(path))


class FilamentMeterTests(unittest.TestCase):
    def test_counts_only_what_a_print_adds(self):
        meter = b.FilamentMeter()
        self.assertIsNone(meter.feed("standby", 0., 1))
        self.assertIsNone(meter.feed("printing", 0., 1))
        self.assertEqual(meter.feed("printing", 50., 1), (1, 50.))

    def test_tool_change_moves_the_count(self):
        meter = b.FilamentMeter()
        meter.feed("printing", 0., 0)
        self.assertEqual(meter.feed("printing", 10., 0), (0, 10.))
        self.assertEqual(meter.feed("printing", 15., 2), (2, 5.))

    def test_end_of_print_is_counted_once(self):
        meter = b.FilamentMeter()
        meter.feed("printing", 0., 0)
        self.assertEqual(meter.feed("complete", 7., 0), (0, 7.))
        self.assertIsNone(meter.feed("complete", 7., 0))

    def test_new_print_is_not_negative(self):
        meter = b.FilamentMeter()
        meter.feed("printing", 100., 0)
        self.assertIsNone(meter.feed("printing", 0., 0))
        self.assertEqual(meter.feed("printing", 4., 0), (0, 4.))

    def test_keeps_last_slot_during_a_change(self):
        meter = b.FilamentMeter()
        meter.feed("printing", 0., 3)
        self.assertEqual(meter.feed("printing", 2., None), (3, 2.))

    def test_active_slot_and_density(self):
        status = {"loaded_slot": "slot2", "boxes": [{"filament_info": [{}, {}, {"filament_type": "PETG"}]}]}
        self.assertEqual(b.active_slot(status), 2)
        self.assertIsNone(b.active_slot({"loaded_slot": ""}))
        self.assertEqual(b.slot_density(status, 2), 1.27)
        self.assertEqual(b.slot_density({}, 2), b.DEFAULT_DENSITY)


if __name__ == "__main__":
    unittest.main()
