import pathlib
import sys

# The tested files are those copied to the printer, in ../printer; the setup step is in ..
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "printer"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import unittest

import fluidd_card as f

SLOTS = {"slots": [
    {"index": 0, "label": "1A", "color": "EFE255", "tag": {"detail": "PLA Lite", "weight": 1000}, "spool": {"remaining": 996.4}},
    {"label": "1B", "color": None, "tag": None, "spool": None}]}


class CardData(unittest.TestCase):
    def test_spools_and_only_problem_rows(self):
        check = {"file": "a.gcode", "printing": False, "rows": [
            {"label": "1A", "need": 3, "left_to_print": 3, "remaining": 996, "verdict": "ok"},
            {"label": "1D", "need": 300, "left_to_print": 300, "remaining": 247, "verdict": "short"}]}
        data = f.card_data(SLOTS, check)
        self.assertEqual(data["spools"][0], {"index": 0, "label": "1A", "color": "EFE255", "material": "PLA Lite",
                                             "remaining": 996.4, "weight": 1000, "tagless": False})
        self.assertEqual(data["spools"][1]["remaining"], None)
        self.assertTrue(data["spools"][1]["tagless"])
        self.assertEqual(data["problems"], [{"label": "1D", "need": 300, "remaining": 247, "verdict": "short"}])

    def test_picked_plastic_shows_its_name_and_weight(self):
        slots = {"slots": [{"index": 1, "label": "1B", "color": "A878FF", "tag": {"empty": True},
                            "spool": {"remaining": 640, "full": 750, "name": "eSun PLA+"}}]}
        spool = f.card_data(slots, {})["spools"][0]
        self.assertEqual((spool["material"], spool["weight"], spool["tagless"]), ("eSun PLA+", 750, True))

    def test_rack_spool_comes_last_when_counted(self):
        rack = {"index": 16, "label": "Rack", "color": "B87F2B", "tag": None, "spool": {"remaining": 300, "full": 1000}}
        spools = f.card_data(dict(SLOTS, rack=rack), {})["spools"]
        self.assertEqual((spools[-1]["label"], spools[-1]["remaining"], spools[-1]["tagless"]), ("Rack", 300, True))
        # An older usage module counts no rack: no tile.
        self.assertEqual(len(f.card_data(dict(SLOTS, rack=dict(rack, spool=None)), {})["spools"]), 2)

    def test_during_print_need_is_what_is_left_to_print(self):
        check = {"file": "a.gcode", "printing": True, "rows": [
            {"label": "1D", "need": 300, "left_to_print": 120, "remaining": 100, "verdict": "short"}]}
        self.assertEqual(f.card_data(SLOTS, check)["problems"][0]["need"], 120)


if __name__ == "__main__":
    unittest.main()
