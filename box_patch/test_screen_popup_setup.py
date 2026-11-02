import os
import tempfile
import unittest

import screen_popup_setup as s


class Setup(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = folder.name
        names = ("CLIENT", "ERRORS", "PICTURES", "PANEL", "STOCK", "FLAG", "UNIT_DIR")
        old = {name: getattr(s, name) for name in names}
        self.addCleanup(lambda: [setattr(s, name, value) for name, value in old.items()])
        s.CLIENT = os.path.join(root, "client")
        s.ERRORS = os.path.join(s.CLIENT, "errors")
        s.PICTURES = os.path.join(s.CLIENT, "plastic")
        s.PANEL = os.path.join(root, "panel")
        s.STOCK = os.path.join(s.PANEL, "stock")
        s.FLAG = os.path.join(s.PANEL, "flag")
        s.UNIT_DIR = os.path.join(root, "units")
        os.makedirs(s.PANEL)
        os.makedirs(s.UNIT_DIR)
        for code in s.CODES:
            os.makedirs(os.path.join(s.ERRORS, code))
            self.write(code, "stock " + code)
        self._qidi, self._chown = s._qidi, os.chown
        s._qidi = lambda: (os.getuid(), os.getgid())
        self.calls = []

    def tearDown(self):
        s._qidi = self._qidi

    def write(self, code, text):
        with open(os.path.join(s.ERRORS, code, "0.json"), "w") as f:
            f.write(text)

    def read(self, code):
        with open(os.path.join(s.ERRORS, code, "0.json")) as f:
            return f.read()

    def run_(self, command, check=False):
        self.calls.append(command)

    def test_round_trip_keeps_the_first_stock(self):
        self.assertTrue(s.install(None, self.run_))
        self.write(s.CODES[0], "our text")
        s.install(None, self.run_)
        self.assertTrue(s.uninstall(self.run_))
        self.assertEqual(self.read(s.CODES[0]), "stock " + s.CODES[0])
        self.assertFalse(os.path.exists(s.PICTURES))
        self.assertEqual(os.listdir(s.UNIT_DIR), [])
        self.assertIn(["systemctl", "restart", "qidi-client.service"], self.calls)

    def test_uninstall_without_install_touches_nothing(self):
        self.assertFalse(s.uninstall(self.run_))
        self.assertNotIn(["systemctl", "restart", "qidi-client.service"], self.calls)
        self.assertEqual(self.read(s.CODES[1]), "stock " + s.CODES[1])


if __name__ == "__main__":
    unittest.main()
