import json
import pathlib
import stat
import tempfile
import unittest

import qidi_backend as q


class EnvTests(unittest.TestCase):
    def test_written_values_read_back_and_stay_private(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / ".env"
            q.write_env({"host": "10.0.0.5", "user": "mks", "password": "a b'c$d"}, path)
            self.assertEqual(q.read_env(path), {"PRINTER_HOST": "10.0.0.5", "PRINTER_USER": "mks",
                                                "PRINTER_PASSWORD": "a b'c$d"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_empty_password_keeps_the_saved_one(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / ".env"
            q.write_env({"host": "10.0.0.5", "user": "mks", "password": "secret"}, path)
            q.write_env({"host": "10.0.0.9", "user": "", "password": ""}, path)
            self.assertEqual(q.read_env(path), {"PRINTER_HOST": "10.0.0.9", "PRINTER_USER": "mks",
                                                "PRINTER_PASSWORD": "secret"})

    def test_address_alone_gets_the_default_login(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / ".env"
            q.write_env({"host": "10.0.0.5"}, path)
            self.assertEqual(q.read_env(path), {"PRINTER_HOST": "10.0.0.5", "PRINTER_USER": q.DEFAULT_USER,
                                                "PRINTER_PASSWORD": q.DEFAULT_PASSWORD})
            q.write_env({"user": "me", "password": "x"}, path)
            self.assertEqual(q.read_env(path)["PRINTER_HOST"], "10.0.0.5")
            q.write_env({"default_login": True}, path)
            self.assertEqual(q.read_env(path)["PRINTER_USER"], q.DEFAULT_USER)

    def test_missing_file_is_empty(self):
        self.assertEqual(q.read_env(pathlib.Path("/nonexistent/.env")), {})


class ModuleListTests(unittest.TestCase):
    def test_list_matches_what_the_printer_side_accepts(self):
        ids = [m["id"] for m in json.loads(q.MODULES_FILE.read_text(encoding="utf-8"))["modules"]]
        self.assertEqual(sorted(ids), sorted(q.remote_setup.KNOWN_FEATURES))

    def test_every_bundled_file_exists(self):
        for path in q.BUNDLE:
            self.assertTrue(path.exists(), path)


class VersionTests(unittest.TestCase):
    def test_catalog_gives_every_module_a_version(self):
        catalog = q.load_catalog()
        self.assertTrue(catalog["version"])
        for module in catalog["modules"]:
            self.assertTrue(module["version"], module["id"])

    def test_only_chosen_modules_are_recorded(self):
        catalog = {"version": "2.0", "modules": [{"id": "a", "version": "1.1"}, {"id": "b", "version": "1.2"}]}
        self.assertEqual(q.versions_for({"a"}, catalog), {"package": "2.0", "a": "1.1"})


class BlockedTests(unittest.TestCase):
    def test_no_route_means_the_permission_is_off(self):
        import errno
        import urllib.error
        self.assertTrue(q.blocked_by_macos(urllib.error.URLError(OSError(errno.EHOSTUNREACH, "No route to host"))))
        self.assertFalse(q.blocked_by_macos(urllib.error.URLError(OSError(errno.ECONNREFUSED, "refused"))))
        self.assertFalse(q.blocked_by_macos(ValueError("bad json")))


class DiagTests(unittest.TestCase):
    def test_both_chips_must_answer(self):
        self.assertTrue(q.diag_ok("Version=0x92 x\nVersion=0x92 y"))
        self.assertFalse(q.diag_ok("Version=0x92 x"))
        self.assertFalse(q.diag_ok("Version=0x92 x\nVersion=0xFF y"))
        self.assertFalse(q.diag_ok("Version=0x00 x\nVersion=0x92 y"))


class FakePrinter:
    host = "h"

    def __init__(self, state="standby", ready=(True,), features=("bambu",), diag=True):
        self.state, self.ready, self.features, self.diag = state, list(ready), set(features), diag
        self.calls, self.versions = [], []

    def print_state(self):
        return self.state

    def installed(self):
        return set(self.features), {"package": "0.1"}

    def remote(self, action, features, versions):
        self.calls.append(set(features))
        self.versions.append(versions)

    def restart_klipper(self):
        return self.ready.pop(0)

    def diagnose(self):
        return self.diag

    def panel_answers(self):
        return True


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.ensure, self.alias = q.ensure_key, q.sync_alias
        q.ensure_key, q.sync_alias = (lambda: None), (lambda host: None)

    def tearDown(self):
        q.ensure_key, q.sync_alias = self.ensure, self.alias

    def test_printing_printer_is_not_touched(self):
        for state in ("printing", "paused", "unavailable"):
            printer = FakePrinter(state)
            with self.assertRaises(q.Failed):
                q.apply(printer, {"usage"})
            self.assertEqual(printer.calls, [])

    def test_failed_restart_returns_to_what_was_on(self):
        printer = FakePrinter(ready=(False, True), features=("bambu", "usage"))
        with self.assertRaises(q.Failed):
            q.apply(printer, {"spools"})
        self.assertEqual(printer.calls, [{"spools"}, {"bambu", "usage"}])
        self.assertEqual(printer.versions[1], {"package": "0.1"})
        self.assertEqual(set(printer.versions[0]), {"package", "spools"})

    def test_removing_everything_skips_the_readers(self):
        printer = FakePrinter(diag=False)
        self.assertIn("удалено", q.apply(printer, set()))

    def test_mesh_alone_does_not_ask_the_readers(self):
        self.assertIn("Готово", q.apply(FakePrinter(diag=False), {"mesh"}))

    def test_silent_readers_are_reported(self):
        with self.assertRaises(q.Failed):
            q.apply(FakePrinter(diag=False), {"nfc"})


class DemoTests(unittest.TestCase):
    def test_status_message_is_one_json_line(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(q.main(["--demo", "status"]), 0)
        message = json.loads(out.getvalue())
        self.assertEqual(message["type"], "status")
        self.assertTrue(message["configured"])


if __name__ == "__main__":
    unittest.main()
