"""Tests for bambu_spool_usage: the spool count and the screen window signal."""
import pathlib
import sys

PACKAGES = pathlib.Path(__file__).resolve().parent.parent.parent
for folder in ("usage/printer",):
    sys.path.insert(0, str(PACKAGES / folder))
import json
import os
import re
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

    def test_edited_plastic_keeps_the_count(self):
        self.ledger.pick(1, dict(PLASTIC, plastic="p1"), ["QFA01", "000000"], printing=False)
        self.ledger.add("slot:1", 40, ["QFA00", "A878FF"])
        # The user changes the color of p1 in the list: the spool keeps its 960 g.
        scripts = self.ledger.pick(1, dict(PLASTIC, plastic="p1", color="CEC0FE", grams=5, keep=True),
                                   ["QFA00", "A878FF"], printing=False)
        self.assertIn("SAVE_VARIABLE VARIABLE=color_slot1 VALUE=\"['CEC0FE']\"", scripts)
        view = self.ledger.view("slot:1", ["QFA00", "CEC0FE"])
        self.assertEqual((view["remaining"], view["plastic"]), (960, "p1"))
        # Another plastic, or a pick without keep, starts the count anew.
        self.ledger.pick(1, dict(PLASTIC, plastic="p2", grams=500, keep=True), ["QFA00", "CEC0FE"], printing=False)
        self.assertEqual(self.ledger.view("slot:1", ["QFA00", "A878FF"])["remaining"], 500)

    def test_rack_is_counted_without_a_tag(self):
        rack = {"filament_slot16": "QFA00", "color_slot16": ["B87F2B"]}
        self.assertEqual(b.spool_key(rack, b.RACK_SLOT), "slot:16")
        self.assertEqual(b.active_slot({"loaded_slot": "slot16"}), b.RACK_SLOT)
        scripts = self.ledger.pick(b.RACK_SLOT, PLASTIC, ["QFA00", "B87F2B"], printing=False)
        # The rack has no tag record to forget.
        self.assertEqual(len(scripts), 2)
        self.ledger.add("slot:16", 25, ["QFA00", "A878FF"])
        self.assertEqual(self.ledger.view("slot:16", ["QFA00", "A878FF"])["remaining"], 975)

    def test_tagged_spool_keeps_what_it_is(self):
        self.ledger.adopt(slot("QFA01", "000000", dict(BLACK_TAG, brand="Bambu Lab", detail="PLA Matte")))
        entry = self.ledger.spools["tray:T"]
        self.assertEqual((entry["name"], entry["color"], entry["filament"]), ("Bambu Lab PLA Matte", "000000", "QFA01"))
        # The second sighting links the tag number once; after that nothing changes.
        self.ledger.adopt(slot("QFA01", "000000", dict(BLACK_TAG, brand="Bambu Lab", detail="PLA Matte")))
        self.ledger.dirty = False
        self.ledger.adopt(slot("QFA01", "000000", dict(BLACK_TAG, brand="Bambu Lab", detail="PLA Matte")))
        self.assertFalse(self.ledger.dirty)

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


class FakePrinter:
    """Klipper objects the usage module reads: saved variables, slot sensors, print state.
    SAVE_VARIABLE G-code changes the saved variables, as in Klipper."""

    def __init__(self, variables, state="standby"):
        import ast
        outer = self
        self.state = state
        self.present = {slot: True for slot in range(b.BOX_SLOTS)}
        self.loaded = None
        self.scripts = []
        self.commands = {}

        class Saved:
            allVariables = variables

        class Gcode:
            def register_command(self, name, func, desc=None):
                outer.commands[name] = func

            def run_script(self, script):
                outer.scripts.append(script)
                match = re.fullmatch(r'SAVE_VARIABLE VARIABLE=(\w+) VALUE="(.*)"', script)
                Saved.allVariables[match.group(1)] = ast.literal_eval(match.group(2))

        class Stepper:
            def __init__(self, slot):
                self.slot = slot

            def get_status(self, eventtime):
                return {"runout_button": 0 if outer.present[self.slot] else 1}

        class Stats:
            def get_status(self, eventtime):
                return {"state": outer.state, "filament_used": 0.}

        class Box:
            def get_status(self, eventtime):
                return {"loaded_slot": "slot%d" % (outer.loaded,) if outer.loaded is not None else "slot-1"}

        class Sensor:
            def get_status(self, eventtime):
                return {"filament_detected": outer.loaded is not None}

        class Reactor:
            def monotonic(self):
                return 0.

        self.saved = Saved
        self.objects = {"save_variables": Saved(), "gcode": Gcode(), "print_stats": Stats(), "box_extras": Box(),
                        "filament_switch_sensor filament_switch_sensor": Sensor()}
        self.objects.update({"box_stepper slot%d" % (slot,): Stepper(slot) for slot in range(b.BOX_SLOTS)})
        self.reactor = Reactor()

    def get_reactor(self):
        return self.reactor

    def register_event_handler(self, name, func):
        pass

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


class SlotChangeTests(unittest.TestCase):
    """What the user saw on the printer: a spool taken out still counted, one spool in two slots."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.time = 0.

    def usage(self, variables, state="standby"):
        self.printer = FakePrinter(variables, state)
        return b.SpoolUsage(self.printer, os.path.join(self.folder, b.USAGE_FILE))

    def run_for(self, usage, seconds):
        for _ in range(int(seconds)):
            self.time += 1.
            usage._count(self.time)

    def view(self, usage, slot):
        return usage.get_status(self.time)["slots"][slot]

    def test_tagged_spool_taken_out_is_forgotten(self):
        # Scenario 1: a tagged spool out, a spool without a tag in; the slot keeps its material.
        usage = self.usage(slot("QFA01", "000000", dict(BLACK_TAG)))
        self.run_for(usage, 2)
        usage.ledger.set_remaining("tray:T", 640)
        self.assertEqual(self.view(usage, 1)["remaining"], 640)
        self.printer.present[1] = False
        self.run_for(usage, 1)
        # A moment without a spool, e.g. while it turns in the reader, changes nothing.
        self.assertEqual(self.view(usage, 1)["remaining"], 640)
        self.run_for(usage, 3)
        self.assertTrue(self.view(usage, 1)["empty"])
        self.assertIsNone(self.view(usage, 1)["remaining"])
        self.assertEqual(self.printer.saved.allVariables["bambu_tag_slot1"], {"empty": True})
        self.printer.present[1] = True
        self.run_for(usage, 1)
        view = self.view(usage, 1)
        self.assertEqual((view["key"], view["remaining"]), ("slot:1", None))
        # The tagged spool keeps its count for when it comes back.
        self.assertEqual(usage.ledger.view("tray:T")["remaining"], 640)

    def test_same_tag_read_again_gets_its_count_back(self):
        usage = self.usage(slot("QFA01", "000000", dict(BLACK_TAG)))
        self.run_for(usage, 1)
        usage.ledger.set_remaining("tray:T", 640)
        self.printer.present[1] = False
        self.run_for(usage, 5)
        self.printer.present[1] = True
        self.printer.saved.allVariables["bambu_tag_slot1"] = dict(BLACK_TAG)
        self.run_for(usage, 1)
        self.assertEqual(self.view(usage, 1)["remaining"], 640)

    def test_taken_out_during_a_print_is_forgotten_after_it(self):
        usage = self.usage(slot("QFA01", "000000", dict(BLACK_TAG)), state="printing")
        self.run_for(usage, 1)
        usage.ledger.set_remaining("tray:T", 640)
        self.printer.present[1] = False
        self.run_for(usage, 5)
        # G-code waits behind a print: nothing is written, the record is only hidden meanwhile.
        self.assertEqual(self.printer.scripts, [])
        self.printer.present[1] = True
        self.run_for(usage, 1)
        self.assertEqual(self.view(usage, 1)["key"], "slot:1")
        self.printer.state = "complete"
        self.run_for(usage, 1)
        self.assertEqual(self.printer.saved.allVariables["bambu_tag_slot1"], {"empty": True})

    def test_spool_in_the_toolhead_is_not_cleared(self):
        # The end of a spool passes the sensor while the rest of it is still printed.
        usage = self.usage(slot("QFA01", "000000", dict(BLACK_TAG)), state="printing")
        self.printer.loaded = 1
        self.printer.present[1] = False
        self.run_for(usage, 10)
        self.assertEqual(self.view(usage, 1)["key"], "tray:T")
        self.printer.loaded = None
        self.run_for(usage, 4)
        self.assertTrue(self.view(usage, 1)["empty"])

    def test_tagless_spool_moved_to_another_slot_leaves_the_first(self):
        # Scenario 2: a spool without a tag moved from 1A to 1B, both slots with the same plastic.
        variables = {"filament_slot0": "QFA00", "color_slot0": ["A878FF"],
                     "filament_slot1": "QFA00", "color_slot1": ["A878FF"]}
        usage = self.usage(variables)
        usage.ledger.pick(0, PLASTIC, ["QFA00", "A878FF"], printing=False)
        usage.ledger.add("slot:0", 100, ["QFA00", "A878FF"])
        self.printer.present[0] = False
        self.run_for(usage, 4)
        usage.ledger.pick(1, dict(PLASTIC, grams=900), ["QFA00", "A878FF"], printing=False)
        self.assertTrue(self.view(usage, 0)["empty"])
        self.assertEqual(self.view(usage, 1)["remaining"], 900)

    def test_tagless_spool_back_with_the_same_plastic_keeps_its_count(self):
        # The user's rule: the old count comes back only while the slot's material and color match.
        variables = {"filament_slot0": "QFA00", "color_slot0": ["A878FF"]}
        usage = self.usage(variables)
        usage.ledger.pick(0, PLASTIC, ["QFA00", "A878FF"], printing=False)
        self.printer.present[0] = False
        self.run_for(usage, 4)
        self.printer.present[0] = True
        self.run_for(usage, 1)
        self.assertEqual(self.view(usage, 0)["remaining"], 1000)
        variables["color_slot0"] = ["FFFFFF"]
        self.assertIsNone(self.view(usage, 0)["remaining"])

    def test_clear_request_forgets_a_tagless_spool(self):
        # Scenario 2: another spool without a tag of the same plastic, the count cleared by hand.
        usage = self.usage({"filament_slot0": "QFA00", "color_slot0": ["A878FF"]})
        usage.ledger.pick(0, PLASTIC, ["QFA00", "A878FF"], printing=False)
        with open(os.path.join(self.folder, b.SPOOL_REQUEST_FILE % (0,)), "w") as f:
            f.write("clear\n")
        self.run_for(usage, 1)
        self.assertIsNone(self.view(usage, 0)["remaining"])
        self.assertNotIn("slot:0", usage.ledger.spools)

    def test_remaining_of_a_tagged_spool_is_corrected(self):
        # Scenario 3: a wrong weight set at the start, corrected from the card.
        usage = self.usage(slot("QFA01", "000000", dict(BLACK_TAG)))
        self.run_for(usage, 1)
        usage.ledger.set_remaining("tray:T", 2000)
        usage.ledger.add("tray:T", 50)
        with open(os.path.join(self.folder, b.SPOOL_REQUEST_FILE % (1,)), "w") as f:
            f.write("700\n")
        self.run_for(usage, 1)
        self.assertEqual(self.view(usage, 1)["remaining"], 700)
        with open(os.path.join(self.folder, b.USAGE_FILE)) as f:
            self.assertEqual(json.load(f)["tray:T"]["set_g"], 700)

    def test_sensors_unknown_change_nothing(self):
        usage = self.usage(slot("QFA01", "000000", dict(BLACK_TAG)))
        del self.printer.objects["box_stepper slot1"]
        self.run_for(usage, 10)
        self.assertEqual(self.view(usage, 1)["key"], "tray:T")

    def test_clear_command(self):
        usage = self.usage({"filament_slot0": "QFA00", "color_slot0": ["A878FF"]})
        usage.ledger.pick(0, PLASTIC, ["QFA00", "A878FF"], printing=False)
        said = []

        class Command:
            def get_int(self, name, default=None, minval=None, maxval=None):
                return {"SLOT": 0, "CLEAR": 1}[name]

            def respond_info(self, text):
                said.append(text)
        self.printer.commands["BAMBU_RFID_SPOOL_SET"](Command())
        self.assertIsNone(self.view(usage, 0)["remaining"])
        self.assertEqual(said, ["Slot 0: count forgotten"])


if __name__ == "__main__":
    unittest.main()
