import unittest

import qidi_window as w

STATUS = {
    "type": "status", "state": "standby", "host": "10.0.0.5", "version": "1.0.0",
    "features": ["bambu"], "installed_versions": {"package": "1.0.0", "bambu": "1.0.0", "rfid_core": "0.9"},
    "modules": [
        {"id": "bambu", "title": "Метки Bambu", "version": "1.0.0", "klipper": True, "needs": [],
         "part_ids": ["rfid_core"]},
        {"id": "spools", "title": "Мультиподача", "version": "1.5.0", "klipper": True, "needs": ["bambu"],
         "part_ids": []},
        {"id": "guide", "title": "Справка", "version": "1.0.1", "klipper": False, "needs": [], "part_ids": []},
        {"id": "rfid_core", "title": "Модуль Klipper", "version": "1.0", "hidden": True},
    ],
}


def state():
    s = w.State()
    s.receive(STATUS)
    return s


class StateTest(unittest.TestCase):
    def test_an_old_hidden_part_makes_its_package_outdated(self):
        s = state()
        self.assertEqual(s.outdated, {"bambu"})
        self.assertEqual(s.old_parts(s.module("bambu")), ["Модуль Klipper"])
        self.assertEqual(s.action_title, "Обновить")
        self.assertEqual(s.apply_arguments(), ["apply", "--add", "bambu", "--remove", ""])

    def test_a_package_brings_what_it_needs_and_leaves_with_it(self):
        s = state()
        s.chosen = set()
        s.set_chosen("spools", True)
        self.assertEqual(s.chosen, {"spools", "bambu"})
        s.set_chosen("bambu", False)
        self.assertEqual(s.chosen, set())
        self.assertEqual(s.action_title, "Применить")

    def test_confirmation_names_each_change(self):
        s = state()
        s.set_chosen("guide", True)
        text = s.changes
        self.assertIn("Установить:\n• Справка", text)
        self.assertIn("Обновить:\n• Метки Bambu", text)
        self.assertIn("Klipper перезапустится.", text)

    def test_nothing_new_means_reinstall_of_the_chosen(self):
        s = state()
        s.installed_versions["rfid_core"] = "1.0"
        self.assertTrue(s.is_reinstall)
        self.assertEqual(s.action_title, "Переустановить")
        self.assertEqual(s.apply_arguments(), ["apply", "--add", "bambu", "--remove", ""])

    def test_nothing_changes_while_printing(self):
        s = state()
        s.receive(dict(STATUS, state="printing"))
        self.assertFalse(s.can_run_action)

    def test_firmware_line_and_the_chosen_printer_stays_listed(self):
        s = state()
        s.firmware = "QD_Q2_01.01.03.02"
        self.assertEqual(s.firmware_line, "QIDI Q2 · прошивка 01.01.03.02")
        s.receive({"type": "printers", "items": [{"host": "10.0.0.9", "name": "q2"}]})
        self.assertEqual([p["host"] for p in s.printers], ["10.0.0.5", "10.0.0.9"])

    def test_markdown_marks_go(self):
        self.assertEqual(w.plain("**Да** `x` и [тут](http://a)"), "Да x и тут")


if __name__ == "__main__":
    unittest.main()
