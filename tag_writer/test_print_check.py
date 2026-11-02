import io
import unittest
import zipfile

import print_check as p

GCODE = """; total filament weight [g] : 2.00,3.00,0.00,5.00
M83
T0
G1 X1 E100
G1 X2 E-10
G1 X3 E10
T1
G92 E0
G1 X1 E200
T3
G1 X1 E300
T1
G1 X1 E100
"""
LABELS = {0: "1A", 1: "1B", 2: "1C", 3: "1D"}
MAP = {"value_t0": "slot0", "value_t1": "slot1", "value_t2": "slot2", "value_t3": "slot3"}


def spool(remaining, key="uid:AA"):
    return {"key": key, "remaining": remaining, "used": 0}


class Parsing(unittest.TestCase):
    def test_grams_and_order(self):
        plan = p.parse_plan(GCODE)
        self.assertEqual(plan["grams"], [2.0, 3.0, 0.0, 5.0])
        self.assertEqual(plan["timeline"], [(0, 100.0), (1, 200.0), (3, 300.0), (1, 100.0)])

    def test_absolute_extrusion(self):
        plan = p.parse_plan("; filament used [g] = 1.00\nM82\nT0\nG92 E0\nG1 E10\nG1 E25\nG92 E0\nG1 E5\n")
        self.assertEqual(plan["timeline"], [(0, 30.0)])

    def test_missing_usage_is_reported(self):
        with self.assertRaises(p.CheckError):
            p.parse_plan("T0\nG1 E5\n")

    def test_3mf_uses_plate_gcode(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("Metadata/plate_1.gcode", GCODE)
            z.writestr("Metadata/plate_1.json", "{}")
        self.assertEqual(p.gcode_text(out.getvalue()), GCODE)


class Evaluating(unittest.TestCase):
    def setUp(self):
        self.plan = p.parse_plan(GCODE)
        self.spools = [spool(100), spool(3.5), spool(500), spool(1)]

    def rows(self, spools=None, variables=None, used=0.0):
        return {r["label"]: r for r in p.evaluate(self.plan, variables or MAP, spools or self.spools, LABELS, used)}

    def test_verdicts_before_print(self):
        rows = self.rows()
        self.assertEqual(rows["1A"]["verdict"], "ok")
        self.assertEqual(rows["1B"]["verdict"], "risk")
        self.assertEqual(rows["1D"]["verdict"], "short")
        self.assertEqual(rows["1D"]["spare"], -4.0)

    def test_unused_tool_is_left_out(self):
        self.assertNotIn("1C", self.rows())

    def test_same_colour_spools_stay_separate(self):
        variables = dict(MAP, color_slot0=["000000"], color_slot1=["000000"])
        rows = self.rows(variables=variables)
        self.assertEqual((rows["1A"]["remaining"], rows["1B"]["remaining"]), (100, 3.5))

    def test_tools_on_one_slot_add_up(self):
        rows = self.rows(variables=dict(MAP, value_t1="slot0"))
        self.assertEqual(rows["1A"]["need"], 5.0)

    def test_progress_follows_print_order(self):
        # 100 mm of T0, 200 of 300 of T1, nothing of T3.
        rows = self.rows(used=300)
        self.assertEqual(rows["1A"]["left_to_print"], 0.0)
        self.assertEqual(rows["1B"]["left_to_print"], 1.0)
        self.assertEqual(rows["1D"]["left_to_print"], 5.0)

    def test_overrun_never_goes_negative(self):
        self.assertEqual(self.rows(used=99999)["1D"]["left_to_print"], 0.0)

    def test_no_counter_is_unknown_not_ok(self):
        self.assertEqual(self.rows(spools=[None, None, None, None])["1A"]["verdict"], "unknown")

    def test_unmapped_tool_is_unknown(self):
        rows = p.evaluate(self.plan, {}, self.spools, LABELS)
        self.assertEqual([(r["label"], r["verdict"]) for r in rows], [(None, "unknown")])


class FakePrinter:
    SLOTS = list(LABELS.items())
    host = "printer"

    def __init__(self, state="standby", filename="", used=0.0):
        self.state, self.filename, self.used = state, filename, used

    def fetch(self, path, post=False):
        if path.startswith("/server/files/list"):
            return {"result": [{"path": "old.gcode", "modified": 1}, {"path": "new.gcode", "modified": 2},
                               {"path": ".cache/x.gcode", "modified": 3}]}
        return {"result": {"status": {
            "save_variables": {"variables": MAP},
            "print_stats": {"state": self.state, "filename": self.filename, "filament_used": self.used},
            "bambu_spool_usage": {"slots": [spool(100), spool(100), spool(100), spool(100)]}}}}


class Checking(unittest.TestCase):
    def make(self, printer):
        self.downloads = []

        def download(name):
            self.downloads.append(name)
            return GCODE.encode()
        return p.PrintCheck(printer, download)

    def test_idle_takes_newest_visible_file(self):
        result = self.make(FakePrinter()).check()
        self.assertEqual(result["file"], "new.gcode")
        self.assertEqual(result["files"], ["new.gcode", "old.gcode"])
        self.assertFalse(result["printing"])

    def test_running_print_wins_and_counts_progress(self):
        check = self.make(FakePrinter("printing", ".cache/x.gcode", 100))
        result = check.check("old.gcode")
        self.assertEqual(result["file"], ".cache/x.gcode")
        self.assertEqual(result["rows"][0]["left_to_print"], 0.0)

    def test_file_is_parsed_once(self):
        check = self.make(FakePrinter())
        check.check()
        check.check()
        self.assertEqual(self.downloads, ["new.gcode"])

    def test_unknown_file_is_refused(self):
        with self.assertRaises(p.CheckError):
            self.make(FakePrinter()).check("../../etc/passwd")

    def test_bad_file_gives_message(self):
        check = p.PrintCheck(FakePrinter(), lambda name: b"no usage here")
        self.assertEqual(check.check()["rows"], [])
        self.assertIn("расход", check.check()["message"])


class Watching(unittest.TestCase):
    def make(self, rows, states):
        self.sent = []
        self.checks = 0
        states = list(states)
        outer = self

        class Fake:
            def fetch(self, path, post=False):
                if post:
                    outer.sent.append(p.urllib.parse.parse_qs(path.split("?", 1)[1])["script"][0])
                    return {"result": "ok"}
                return {"result": {"status": {"print_stats": {"state": states.pop(0)}}}}

        class Check:
            def check(self):
                outer.checks += 1
                return {"rows": rows}
        return p.PrintWatcher(Fake(), Check(), lambda printer, rows: False)

    LOW = [{"label": "1D", "verdict": "short", "left_to_print": 300.4, "remaining": 247.2, "spare": -53.2},
           {"label": "1A", "verdict": "ok", "left_to_print": 3, "remaining": 900}]

    def test_low_spool_gives_one_message_per_start(self):
        watcher = self.make(self.LOW, ["standby", "printing", "printing", "paused", "printing"])
        for _ in range(5):
            watcher.step()
        self.assertEqual(self.sent, ['RESPOND TYPE=error MSG="Code:QDE_004_900 Message:Не хватит пластика. '
                                     '1D: нужно 300 г, осталось 247 г, не хватит 53 г"'])

    def test_enough_plastic_stays_silent(self):
        watcher = self.make(self.LOW[1:], ["standby", "printing"])
        watcher.step()
        watcher.step()
        self.assertEqual((self.checks, self.sent), (1, []))

    def test_service_restart_during_print_is_not_a_start(self):
        watcher = self.make(self.LOW, ["printing", "printing"])
        watcher.step()
        watcher.step()
        self.assertEqual(self.checks, 0)

    def test_next_print_warns_again(self):
        watcher = self.make(self.LOW, ["standby", "printing", "complete", "printing"])
        for _ in range(4):
            watcher.step()
        self.assertEqual(len(self.sent), 2)

    def test_thin_spare_has_its_own_message(self):
        rows = [{"label": "1B", "verdict": "risk", "left_to_print": 240, "remaining": 247, "spare": 7}]
        self.assertEqual(p.low_message(rows),
                         (p.LOW_CODE, "Пластика впритык. 1B: нужно 240 г, осталось 247 г, запас 7 г"))

    def test_short_message_also_names_thin_spools(self):
        rows = self.LOW + [{"label": "1B", "verdict": "risk", "left_to_print": 240, "remaining": 247, "spare": 7}]
        code, text = p.low_message(rows)
        self.assertEqual(code, p.SHORT_CODE)
        self.assertIn("1B", text)

    def test_screen_window_replaces_the_plain_message(self):
        watcher = self.make(self.LOW, ["standby", "printing"])
        shown = []
        watcher.popup = lambda printer, rows: shown.append(rows) or True
        watcher.step()
        watcher.step()
        self.assertEqual((len(shown), self.sent), (1, []))

    def test_broken_window_falls_back_to_the_message(self):
        watcher = self.make(self.LOW, ["standby", "printing"])

        def broken(printer, rows):
            raise OSError("no screen")
        watcher.popup = broken
        watcher.step()
        watcher.step()
        self.assertEqual(len(self.sent), 1)

    def test_message_is_one_safe_gcode_line(self):
        code, text = p.low_message(self.LOW)
        self.assertEqual(code, p.SHORT_CODE)
        self.assertFalse(set(text) & set(';"\n'))
        self.assertIsNone(p.low_message(self.LOW[1:]))


if __name__ == "__main__":
    unittest.main()
