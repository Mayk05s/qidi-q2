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


class PackageTests(unittest.TestCase):
    def test_window_gets_every_package_from_its_folder(self):
        catalog = q.load_catalog()
        self.assertTrue(catalog["version"])
        shown = [m for m in catalog["modules"] if not m["hidden"]]
        self.assertEqual({m["id"] for m in shown}, q.remote_setup.visible(q.load_packages()))
        for module in catalog["modules"]:
            self.assertTrue(module["version"], module["id"])
            self.assertTrue(module["readme"].startswith("#"), module["id"])
        spools = next(m for m in catalog["modules"] if m["id"] == "spools")
        self.assertIn("bambu", spools["needs"])
        self.assertTrue(spools["klipper"])
        guide = next(m for m in catalog["modules"] if m["id"] == "guide")
        self.assertFalse(guide["klipper"])

    def test_every_bundled_file_exists(self):
        for relative in q.bundle_files(q.load_packages()):
            self.assertTrue((q.ROOT / relative).exists(), relative)

    def test_only_copied_packages_get_a_new_version(self):
        packages = {"a": {"version": "1.1"}, "b": {"version": "1.2"}}
        self.assertEqual(q.versions_for({"a"}, packages, {"version": "2.0"}), {"package": "2.0", "a": "1.1"})

    def test_plan_follows_the_printer_side(self):
        packages = q.load_packages()
        chosen, after, copied = q.Change(add={"guide"}).plan({"mesh"}, packages)
        self.assertEqual(chosen, {"mesh", "guide"})
        self.assertEqual(copied, {"guide", "panel"})
        chosen, after, copied = q.Change(remove={"bambu"}).plan({"bambu", "spools", "mesh"}, packages)
        self.assertEqual(chosen, {"mesh"})
        self.assertEqual(copied, set())


class BackupListTests(unittest.TestCase):
    def test_newest_first_and_the_first_one_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            saved = q.BACKUP_DIR
            q.BACKUP_DIR = pathlib.Path(d)
            try:
                folder = q.BACKUP_DIR / "h"
                folder.mkdir()
                for n in range(q.BACKUPS_KEPT + 3):
                    name = "2026-11-%02d" % (n + 1,)
                    (folder / (name + ".json")).write_text(json.dumps({"id": name}))
                    (folder / (name + ".tar.gz")).write_bytes(b"x")
                q.prune_backups(folder)
                names = [b["id"] for b in q.list_backups("h")]
                self.assertEqual(len(names), q.BACKUPS_KEPT + 1)
                self.assertEqual(names[0], "2026-11-%02d" % (q.BACKUPS_KEPT + 3,))
                self.assertEqual(names[-1], "2026-11-01")
            finally:
                q.BACKUP_DIR = saved


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

    def __init__(self, state="standby", ready=(True,), features=("bambu",), diag=True, restarts=True):
        self.state, self.ready, self.features, self.diag = state, list(ready), set(features), diag
        self.restarts = restarts
        self.calls, self.versions = [], []

    def print_state(self):
        return self.state

    def installed(self):
        return set(self.features), {"package": "0.1"}

    def remote(self, arguments, versions):
        self.calls.append(arguments)
        self.versions.append(versions)
        return self.restarts

    def backup(self):
        self.calls.append("backup")
        return {"id": "2026-11-02_12-00-00"}

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
                q.apply(printer, q.Change(add={"usage"}))
            self.assertEqual(printer.calls, [])

    def test_failed_restart_returns_to_what_was_on(self):
        printer = FakePrinter(ready=(False, True), features=("bambu", "usage"))
        with self.assertRaises(q.Failed):
            q.apply(printer, q.Change(add={"spools"}))
        self.assertEqual(printer.calls, ["backup", ["--add", "spools"], ["--features", "bambu,usage"]])
        self.assertEqual(printer.versions[1], {"package": "0.1"})
        self.assertTrue({"package", "spools"} <= set(printer.versions[0]))

    def test_removing_everything_skips_the_readers(self):
        printer = FakePrinter(diag=False)
        self.assertIn("удалено", q.apply(printer, q.Change(remove={"bambu"})))

    def test_without_a_restart_the_readers_are_not_asked(self):
        text = q.apply(FakePrinter(diag=False, restarts=False), q.Change(add={"guide"}))
        self.assertIn("не перезапускался", text)

    def test_silent_readers_are_reported(self):
        with self.assertRaises(q.Failed):
            q.apply(FakePrinter(diag=False), q.Change(add={"nfc"}))


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
