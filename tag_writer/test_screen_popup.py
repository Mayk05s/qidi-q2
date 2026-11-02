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
            def fetch(inner, path, post=False):
                self.scripts.append(s.urllib.parse.parse_qs(path.split("?", 1)[1])["script"][0])
        return s.show(Printer(), rows, self.waits.append)

    def window(self, kind):
        code = s.WINDOWS[kind][0]
        return json.loads((s.CLIENT / "resource" / "qidi_client_error" / code / "0.json").read_text(encoding="utf-8"))

    def test_short_spool_gets_the_red_window(self):
        self.assertTrue(self.show([FINE, LOW, SHORT, dict(SHORT, label="1C")]))
        window = self.window("short")
        self.assertEqual(window["message_translation"]["ru_RU"], "Не хватит пластика")
        self.assertEqual(window["prossess_msg_translation"]["ru_RU"],
                         "Катушка 1D: нужно 300 г, осталось 247 г. Подготовьте новую катушку. Ещё: 1C.")
        self.assertEqual(window["error_type"], 3)
        self.assertEqual(window["popup_stlye"]["main_pic"], str(s.PICTURES / "short.bin"))
        self.assertEqual(window["popup_stlye"]["left_text"], {"ru_RU": "ОК"})
        self.assertTrue((s.PICTURES / "short.bin").stat().st_size > 1000)
        self.assertTrue(s.RELOAD_FLAG.exists())
        self.assertEqual((self.waits, self.scripts), ([s.SCREEN_START], ["BAMBU_SCREEN_WARNING BOX=3"]))

    def test_thin_spool_gets_the_orange_window(self):
        self.assertTrue(self.show([FINE, LOW]))
        self.assertEqual(self.window("low")["prossess_msg_translation"]["ru_RU"], "Катушка 1B: нужно 240 г, осталось 247 г.")
        self.assertEqual(self.scripts, ["BAMBU_SCREEN_WARNING BOX=4"])

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
