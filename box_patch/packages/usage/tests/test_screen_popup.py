import pathlib
import sys

# The tested files are those copied to the printer, in ../printer; the setup step is in ..
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "printer"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import json
import pathlib
import tempfile
import unittest

import screen_popup as s

STOCK_WINDOW = {"message": "BOX 3 sensor", "error_code": "QDC_300_002_005_003", "error_type": 1,
                "message_translation": {"ru_RU": "датчик"}, "prossess_msg_translation": {"ru_RU": "датчик"},
                "popup_stlye": {"main_pic": "NO_PIC", "is_model": False, "left_text": {"ru_RU": "ОК"}}}
SHORT = {"label": "1D", "color": "68724D", "verdict": "short", "left_to_print": 300.4, "remaining": 247.2}
LOW = {"label": "1B", "color": "000000", "verdict": "risk", "left_to_print": 240, "remaining": 247}
FINE = {"label": "1A", "color": "EFE255", "verdict": "ok", "left_to_print": 3, "remaining": 900}


class Popup(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = pathlib.Path(folder.name)
        self.old = s.CLIENT, s.PANEL, s.STOCK, s.RELOAD_FLAG, s.PICTURES
        self.addCleanup(lambda: setattr(s, "CLIENT", self.old[0]) or setattr(s, "PANEL", self.old[1])
                        or setattr(s, "STOCK", self.old[2]) or setattr(s, "RELOAD_FLAG", self.old[3])
                        or setattr(s, "PICTURES", self.old[4]))
        s.CLIENT, s.PANEL = root / "client", root / "panel"
        s.STOCK, s.RELOAD_FLAG, s.PICTURES = s.PANEL / "screen_stock", s.PANEL / "screen_reload", s.CLIENT / "access" / "plastic"
        s.STOCK.mkdir(parents=True)
        s.PICTURES.mkdir(parents=True)
        for code, _, _, _ in s.WINDOWS.values():
            (s.STOCK / (code + ".json")).write_text(json.dumps(dict(STOCK_WINDOW, error_code=code)), encoding="utf-8")
            (s.CLIENT / "resource" / "qidi_client_error" / code).mkdir(parents=True)
        self.scripts, self.waits = [], []

    def show(self, rows):
        class Printer:
            def upload(inner, name, text):
                self.scripts.append((name, text))
        return s.show(Printer(), rows, self.waits.append)

    def window(self, kind):
        code = s.WINDOWS[kind][0]
        return json.loads((s.CLIENT / "resource" / "qidi_client_error" / code / "0.json").read_text(encoding="utf-8"))

    def test_short_spool_gets_the_red_window(self):
        self.assertTrue(self.show([FINE, SHORT]))
        window = self.window("short")
        self.assertEqual(window["message_translation"]["ru_RU"], "Не хватит пластика")
        self.assertEqual(window["prossess_msg_translation"]["ru_RU"],
                         "Катушка 1D: нужно 300 г, осталось 247 г. Подготовьте новую катушку.")
        self.assertEqual(window["error_type"], 3)
        self.assertEqual(window["popup_stlye"]["main_pic"], str(s.PICTURES / "short.bin"))
        self.assertEqual(window["popup_stlye"]["left_text"], {"ru_RU": "ОК"})
        self.assertTrue((s.PICTURES / "short.bin").stat().st_size > 1000)
        self.assertTrue(s.RELOAD_FLAG.exists())
        self.assertEqual((self.waits, self.scripts), ([s.SCREEN_START], [("screen_warning.txt", "3 print\n")]))

    def test_short_and_thin_spools_share_one_window(self):
        self.assertTrue(self.show([FINE, LOW, SHORT, dict(SHORT, label="1C", left_to_print=5, remaining=1)]))
        self.assertEqual(self.window("short")["prossess_msg_translation"]["ru_RU"],
                         "Катушка 1D: нужно 300 г, осталось 247 г. Катушка 1C: нужно 5 г, осталось 1 г. "
                         "Катушка 1B: нужно 240 г, осталось 247 г, впритык. Подготовьте новые катушки.")
        self.assertEqual(self.scripts, [("screen_warning.txt", "3 print\n")])
        # QIDI Studio gets the English or Chinese title: the title and a line per spool.
        full = ["⛔️ Не хватит пластика", "", "🔴  1D — нужно 300 г, есть 247 г",
                "🔴  1C — нужно 5 г, есть 1 г", "🟠  1B — нужно 240 г, есть 247 г, впритык",
                "", "Подготовьте новые катушки."]
        titles = self.window("short")["message_translation"]
        self.assertEqual(titles["en_US"].split("\n"), full)
        self.assertEqual(titles["zh_CN"].split("\n"), full)
        # The screen itself keeps the short title.
        self.assertEqual(titles["ru_RU"], "Не хватит пластика")
        self.assertFalse((s.CLIENT / "resource" / "qidi_client_error" / s.WINDOWS["low"][0] / "0.json").exists())

    def test_thin_spool_gets_the_orange_window(self):
        self.assertTrue(self.show([FINE, LOW]))
        self.assertEqual(self.window("low")["prossess_msg_translation"]["ru_RU"], "Катушка 1B: нужно 240 г, осталось 247 г.")
        self.assertEqual(self.scripts, [("screen_warning.txt", "4 print\n")])
        self.assertEqual(self.window("low")["message_translation"]["en_US"],
                         "⚠️ Пластика может не хватить\n\n🟠  1B — нужно 240 г, есть 247 г")

    def test_text_comes_from_the_stock_file_each_time(self):
        self.show([SHORT])
        self.show([dict(SHORT, label="1C")])
        self.assertIn("1C", self.window("short")["prossess_msg_translation"]["ru_RU"])
        self.assertNotIn("1D", self.window("short")["prossess_msg_translation"]["ru_RU"])

    def test_nothing_when_all_is_fine_or_not_installed(self):
        self.assertFalse(self.show([FINE]))
        for path in s.STOCK.iterdir():
            path.unlink()
        self.assertFalse(self.show([SHORT]))
        self.assertEqual(self.scripts, [])


if __name__ == "__main__":
    unittest.main()
