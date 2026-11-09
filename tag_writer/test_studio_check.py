import pathlib
import tempfile
import unittest

import studio_check as s

GCODE = "; total filament weight [g] : 2.00,30.00\nM83\nT0\nG1 E10\nT1\nG1 E10\n"


def status(first, second):
    return {"save_variables": {"variables": {"value_t0": "slot0", "value_t1": "slot1"}},
            "bambu_spool_usage": {"slots": [{"key": "a", "remaining": first}, {"key": "b", "remaining": second}]}}


class Running(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = pathlib.Path(folder.name)
        self.gcode = self.folder / "plate 1.gcode"
        self.gcode.write_text(GCODE, encoding="utf-8")
        (self.folder / "printer_host.txt").write_text("printer\n", encoding="utf-8")
        self.old = s.HERE, s.LOG
        s.HERE, s.LOG = self.folder, self.folder / "log.txt"
        self.addCleanup(lambda: (setattr(s, "HERE", self.old[0]), setattr(s, "LOG", self.old[1])))
        self.shown = []
        self.infos = []

    def run_with(self, first, second, args=None, answer=s.CONTINUE):
        def choose(lines):
            self.shown.append(lines)
            return answer
        return s.run(args or [str(self.gcode)], lambda host: status(first, second), choose, lambda *a: None, self.infos.append)

    def test_silent_when_enough(self):
        self.run_with(500, 500)
        self.assertEqual(self.shown, [])
        self.assertIn("1A ok", s.LOG.read_text(encoding="utf-8"))

    def test_info_only_when_all_is_fine(self):
        self.run_with(500, 500)
        self.assertEqual(len(self.infos), 1)
        self.run_with(500, 10)
        self.assertEqual(len(self.infos), 1)

    def test_warns_only_about_the_short_spool(self):
        self.run_with(500, 10)
        self.assertEqual(self.shown, [["1B: нужно 30,0 г, осталось 10,0 г, не хватит примерно 20,0 г"]])

    def test_thin_spare_is_a_warning(self):
        self.run_with(500, 35)
        self.assertIn("запас всего 5,0 г", self.shown[0][0])

    def test_answers_set_the_exit_code(self):
        self.assertEqual(self.run_with(500, 10, answer=s.CANCEL), 1)
        self.assertEqual(self.run_with(500, 10, answer=s.CONTINUE), 0)
        self.assertEqual(self.run_with(500, 500), 0)

    def test_window_answer_wins_over_dialog(self):
        code = s.run([str(self.gcode)], lambda host: status(500, 10), self.fail, lambda *a: s.CANCEL, self.fail)
        self.assertEqual(code, 1)


class Window(unittest.TestCase):
    def make(self, states, slots=None):
        self.calls = []

        def panel(host, path, body=None):
            self.calls.append((path, body))
            return {"slots": slots or []}
        return s.CheckWindow(s.print_check.parse_plan(GCODE), "printer", lambda host: states.pop(0), panel)

    def test_swapped_spool_shows_by_itself(self):
        check = self.make([status(500, 10), status(500, 900)])
        first = check.state()
        self.assertEqual((first["title"], first["good"]), ("Не хватит пластика: 1B", False))
        self.assertIn('class="card short"', first["cards"])
        second = check.state()
        self.assertTrue(second["good"])
        self.assertEqual((first["level"], second["level"]), ("short", "good"))
        self.assertIn("1B", first["summary"])
        self.assertNotIn('class="card short"', second["cards"])

    def test_thin_spare_warns_without_alarm(self):
        result = self.make([status(500, 35)]).state()
        self.assertEqual(result["level"], "warn")
        self.assertIn("впритык", result["summary"])

    def test_unknown_remaining_warns(self):
        result = self.make([status(500, None)]).state()
        self.assertEqual(result["level"], "warn")
        self.assertIn("Неизвестно", result["summary"])

    def test_every_slot_has_a_reread_button(self):
        cards = self.make([status(500, 10)]).state()["cards"]
        self.assertEqual(cards.count('class="reread'), 4)
        self.assertEqual(cards.count('class="card idle"'), 2)

    def test_reading_and_errors_come_from_the_panel(self):
        slots = [{"index": 1, "label": "1B", "reading": True, "read_error": "Метка не найдена"}]
        result = self.make([status(500, 10)], slots).state()
        self.assertIn("reread busy", result["cards"])
        self.assertEqual(result["read_error"], "1B: Метка не найдена")

    def test_read_goes_through_the_panel(self):
        check = self.make([])
        check.read(2)
        self.assertEqual(self.calls, [("/api/slot_read", {"index": 2})])

    def test_panel_down_keeps_the_window_working(self):
        def panel(host, path, body=None):
            raise OSError("no route")
        check = s.CheckWindow(s.print_check.parse_plan(GCODE), "printer", lambda host: status(500, 10), panel)
        self.assertFalse(check.state()["good"])

    def test_names_are_escaped(self):
        st = status(500, 10)
        st["save_variables"]["variables"]["bambu_tag_slot1"] = {"brand": "<b>", "detail": "PLA"}
        self.assertIn("&lt;b&gt; PLA", self.make([st]).state()["cards"])


class Paths(Running):
    def test_path_split_at_spaces(self):
        self.run_with(500, 500, str(self.gcode).split(" "))
        self.assertIn("1A ok", s.LOG.read_text(encoding="utf-8"))

    def test_gcode_is_not_changed(self):
        self.run_with(500, 10)
        self.assertEqual(self.gcode.read_text(encoding="utf-8"), GCODE)


if __name__ == "__main__":
    unittest.main()
