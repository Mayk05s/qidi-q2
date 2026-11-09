import pathlib
import sys

# The tested files are those copied to the printer, in ../printer; the setup step is in ..
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "printer"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
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

    def test_big_file_is_read_in_pieces(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("Metadata/plate_1.gcode", GCODE * 50)
        out.seek(0)
        old, p.CHUNK = p.CHUNK, 7
        try:
            chunks = list(p.gcode_chunks(out))
            plan = p.parse_plan(p.gcode_chunks(io.BytesIO(out.getvalue())))
        finally:
            p.CHUNK = old
        self.assertGreater(len(chunks), 50)
        self.assertTrue(all(c.endswith("\n") for c in chunks))
        self.assertEqual("".join(chunks), GCODE * 50)
        self.assertEqual(plan, p.parse_plan(GCODE * 50))

    def test_broken_3mf_is_reported(self):
        with self.assertRaises(p.CheckError):
            p.gcode_text(b"PK\x03\x04 broken")


class SparseTools(unittest.TestCase):
    def test_weights_of_used_tools_only_go_to_those_tools(self):
        plan = p.parse_plan("; total filament weight [g] : 5.60,5.67\nM83\nT0\nG1 E10\nT2\nG1 E10\n")
        self.assertEqual(plan["grams"], [5.6, 0., 5.67])

    def test_full_list_stays_as_it_is(self):
        plan = p.parse_plan("; total filament weight [g] : 1.00,0.00,2.00\nM83\nT0\nG1 E10\nT2\nG1 E10\n")
        self.assertEqual(plan["grams"], [1., 0., 2.])


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

    def test_rack_spool_is_counted(self):
        spools = dict(enumerate(self.spools))
        spools[p.RACK_SLOT] = spool(2)
        rows = {r["label"]: r for r in p.evaluate(self.plan, dict(MAP, value_t3="slot16"), spools,
                                                  {**LABELS, p.RACK_SLOT: "Rack"})}
        self.assertEqual((rows["Rack"]["remaining"], rows["Rack"]["verdict"]), (2, "short"))
        self.assertNotIn("1D", rows)

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
            "bambu_spool_usage": {"slots": [spool(100), spool(100), spool(100), spool(100)], "rack": spool(1)}}}}


class Checking(unittest.TestCase):
    def make(self, printer):
        self.downloads = []

        def download(name):
            self.downloads.append(name)
            return GCODE.encode()
        return p.PrintCheck(printer, download)

    def test_tool_on_the_rack_gets_the_rack_spool(self):
        printer = FakePrinter()
        fetch = printer.fetch
        def with_rack(path, post=False):
            data = fetch(path, post)
            if "status" in data.get("result", {}):
                data["result"]["status"]["save_variables"]["variables"] = dict(MAP, value_t0="slot16")
            return data
        printer.fetch = with_rack
        rows = {r["label"]: r for r in self.make(printer).check()["rows"]}
        self.assertEqual((rows["Rack"]["remaining"], rows["Rack"]["verdict"]), (1, "short"))

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

    def test_card_does_not_wait_for_the_file(self):
        import threading
        gate = threading.Event()

        def slow(name):
            gate.wait(5)
            return GCODE.encode()
        check = p.PrintCheck(FakePrinter(), slow)
        first = check.check(wait=False)
        self.assertEqual((first["pending"], first["rows"]), (True, []))
        gate.set()
        self.assertTrue(check.check()["rows"])
        self.assertNotIn("pending", check.check(wait=False))

    def test_watcher_and_card_read_the_file_once(self):
        import threading
        gate = threading.Event()
        calls = []

        def slow(name):
            calls.append(name)
            gate.wait(5)
            return GCODE.encode()
        check = p.PrintCheck(FakePrinter(), slow)
        check.check(wait=False)
        waiter = threading.Thread(target=check.check)
        waiter.start()
        gate.set()
        waiter.join(5)
        check.check(wait=False)
        self.assertEqual(calls, ["new.gcode"])

    def test_read_files_survive_a_restart(self):
        import os
        import tempfile
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "print_plans.json")
        self.make(FakePrinter())  # resets self.downloads
        first = p.PrintCheck(FakePrinter(), lambda name: GCODE.encode(), cache_path=path)
        rows = first.check()["rows"]

        def never(name):
            raise AssertionError("read again")
        self.assertEqual(p.PrintCheck(FakePrinter(), never, cache_path=path).check()["rows"], rows)

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
        self.shown = []
        return p.PrintWatcher(Fake(), Check(), lambda printer, rows: self.shown.append(rows) or True)

    LOW = [{"label": "1D", "verdict": "short", "left_to_print": 300.4, "remaining": 247.2, "spare": -53.2},
           {"label": "1A", "verdict": "ok", "left_to_print": 3, "remaining": 900}]

    def test_low_spool_gives_one_window_per_start(self):
        watcher = self.make(self.LOW, ["standby", "printing", "printing", "paused", "printing"])
        for _ in range(5):
            watcher.step()
        self.assertEqual((self.shown, self.sent), ([self.LOW], []))

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
        self.assertEqual(len(self.shown), 2)

    def test_broken_window_sends_nothing_else(self):
        watcher = self.make(self.LOW, ["standby", "printing"])

        def broken(printer, rows):
            raise OSError("no screen")
        watcher.popup = broken
        watcher.step()
        watcher.step()
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
