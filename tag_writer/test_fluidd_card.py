import unittest

import fluidd_card as f

SLOTS = {"slots": [
    {"label": "1A", "color": "EFE255", "tag": {"detail": "PLA Lite", "weight": 1000}, "spool": {"remaining": 996.4}},
    {"label": "1B", "color": None, "tag": None, "spool": None}]}


class CardData(unittest.TestCase):
    def test_spools_and_only_problem_rows(self):
        check = {"file": "a.gcode", "printing": False, "rows": [
            {"label": "1A", "need": 3, "left_to_print": 3, "remaining": 996, "verdict": "ok"},
            {"label": "1D", "need": 300, "left_to_print": 300, "remaining": 247, "verdict": "short"}]}
        data = f.card_data(SLOTS, check)
        self.assertEqual(data["spools"][0], {"label": "1A", "color": "EFE255", "material": "PLA Lite",
                                             "remaining": 996.4, "weight": 1000})
        self.assertEqual(data["spools"][1]["remaining"], None)
        self.assertEqual(data["problems"], [{"label": "1D", "need": 300, "remaining": 247, "verdict": "short"}])

    def test_during_print_need_is_what_is_left_to_print(self):
        check = {"file": "a.gcode", "printing": True, "rows": [
            {"label": "1D", "need": 300, "left_to_print": 120, "remaining": 100, "verdict": "short"}]}
        self.assertEqual(f.card_data(SLOTS, check)["problems"][0]["need"], 120)


if __name__ == "__main__":
    unittest.main()
