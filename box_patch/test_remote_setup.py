import json
import os
import pathlib
import tempfile
import unittest

import remote_setup as r

ROOT = str(pathlib.Path(__file__).resolve().parent.parent)
CFG = "[printer]\nkinematics: corexy\n\n"
SAVED = r.SAVE_MARKER + " SAVE_CONFIG\n#*# [extruder]\n#*# x = 1\n"


def package(name, **fields):
    return dict(fields, id=name, _dir="")


class FluiddLinkTests(unittest.TestCase):
    PAGE = "<html><body><div id=app></div></body></html>"

    def test_tag_added_once_and_removed_cleanly(self):
        once = r.add_link_tag(self.PAGE)
        self.assertIn(r.LINK_TAG, once)
        self.assertEqual(r.add_link_tag(once), once)
        self.assertEqual(r.remove_link_tag(once), self.PAGE)

    def test_unexpected_page_is_refused(self):
        with self.assertRaises(SystemExit):
            r.add_link_tag("<html>")


class DescriptionTests(unittest.TestCase):
    PACKAGES = {
        "core": package("core", hidden=True, klipper_section=True,
                        install=[{"from": "a.py", "to": "klipper/klippy/extras/a.py"}]),
        "tags": package("tags", requires=["core"], klipper_options={"read_tags": True},
                        install=[{"from": "map.cfg", "to": "printer_data/config/map.cfg", "keep": True}]),
        "feed": package("feed", requires=["tags"], klipper_options={"feed": True},
                        install=[{"from": "b.py", "to": "klipper/klippy/extras/b.py"}],
                        saved_variables={"auto_read_rfid": 0}),
        "plain": package("plain", install=[{"from": "c.html", "to": "qidi_panel/c.html"}]),
    }

    def test_required_packages_come_along(self):
        self.assertEqual(r.closure({"feed"}, self.PACKAGES), {"feed", "tags", "core"})
        self.assertEqual(r.visible(self.PACKAGES), {"tags", "feed", "plain"})
        with self.assertRaises(SystemExit):
            r.closure({"nope"}, self.PACKAGES)

    def test_options_are_on_only_for_enabled_packages(self):
        self.assertEqual(r.reader_options({"core", "tags"}, self.PACKAGES), {"feed": False, "read_tags": True})
        own = r.own_config({"core", "tags"}, self.PACKAGES)
        self.assertIn("read_tags: True", own)
        self.assertIn("feed: False", own)
        self.assertEqual(r.own_config({"plain"}, self.PACKAGES), "")

    def test_shared_file_stays_while_a_package_needs_it(self):
        before = r.closure({"feed", "plain"}, self.PACKAGES)
        after = r.closure({"tags"}, self.PACKAGES)
        self.assertEqual(r.leftovers(before, after, self.PACKAGES),
                         ["klipper/klippy/extras/b.py", "qidi_panel/c.html"])
        # A kept file holds the user's edits and is never removed.
        self.assertEqual(r.leftovers(before, set(), self.PACKAGES),
                         ["klipper/klippy/extras/a.py", "klipper/klippy/extras/b.py", "qidi_panel/c.html"])

    def test_places_resolve_on_the_printer(self):
        cfg = "/home/mks/printer_data/config/printer.cfg"
        self.assertEqual(r.resolve("klipper/klippy/extras/a.py", "/home/qidi/klipper", cfg),
                         "/home/qidi/klipper/klippy/extras/a.py")
        self.assertEqual(r.resolve("printer_data/config/map.cfg", "/k", cfg), "/home/mks/printer_data/config/map.cfg")
        self.assertEqual(r.resolve("qidi_panel/c.html", "/k", cfg), "/home/qidi/qidi_panel/c.html")
        with self.assertRaises(SystemExit):
            r.resolve("/etc/passwd", "/k", cfg)

    def test_saved_variables_are_set_and_given_back(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "saved_variables.cfg")
            with open(path, "w") as f:
                f.write("[Variables]\nauto_read_rfid = 1\nslot0 = 1\n")
            kept = r.apply_saved_variables({"feed"}, self.PACKAGES, path, None)
            self.assertEqual(kept, {"auto_read_rfid": "1"})
            self.assertIn("auto_read_rfid = 0\n", open(path).read())
            self.assertEqual(r.apply_saved_variables(set(), self.PACKAGES, path, kept), {})
            self.assertEqual(open(path).read(), "[Variables]\nauto_read_rfid = 1\nslot0 = 1\n")

    def test_a_variable_that_was_missing_is_removed_again(self):
        text = r.set_saved_variable("[Variables]\nx = 1\n", "y", "0")
        self.assertEqual(r.saved_variable(text, "y"), "0")
        self.assertEqual(r.set_saved_variable(text, "y", None), "[Variables]\nx = 1\n")


class RealPackagesTests(unittest.TestCase):
    def setUp(self):
        self.packages = r.load_packages(ROOT)

    def test_every_package_is_complete(self):
        for name, description in self.packages.items():
            self.assertEqual(name, os.path.basename(description["_dir"]))
            self.assertTrue(description.get("version"), name)
            if not description.get("hidden"):
                self.assertTrue(description.get("title") and description.get("icon"), name)
            for required in description.get("requires", ()):
                self.assertIn(required, self.packages, name)
            for entry in description.get("install", ()):
                if not entry.get("optional"):
                    self.assertTrue(os.path.exists(os.path.join(ROOT, entry["from"])), entry["from"])
                r.resolve(entry["to"], "/k", "/c/printer.cfg")
            for key in ("help", "klipper_config", "setup"):
                if description.get(key):
                    base = ROOT if key == "setup" else description["_dir"]
                    self.assertTrue(os.path.exists(os.path.join(base, description[key])), (name, key))

    def test_all_packages_give_one_include_and_one_reader_block(self):
        everything = r.closure(r.visible(self.packages), self.packages)
        own = r.own_config(everything, self.packages)
        self.assertEqual(own.count("[bambu_rfid card_reader_1]"), 1)
        self.assertIn("[gcode_macro BED_MESH_CALIBRATE]", own)
        out = r.with_include(CFG + SAVED)
        self.assertEqual(r.with_include(out), out)
        self.assertLess(out.index("[include qidi_patcher.cfg]"), out.index(r.SAVE_MARKER))
        self.assertEqual(r.without_ours(out).strip(), (CFG + SAVED).strip())


class StateTests(unittest.TestCase):
    def test_state_file_decides_what_is_on(self):
        state = {"features": ["guide"], "versions": {"package": "1.0.0"}}
        self.assertEqual(r.installed_features(CFG, state), {"guide"})
        self.assertEqual(r.installed_versions(state), {"package": "1.0.0"})
        self.assertEqual(r.installed_versions(None), {})

    def test_install_from_setup_sh_is_read_from_printer_cfg(self):
        old = r.add_block(CFG + SAVED, {"bambu_tags": True, "custom_tags": False})
        self.assertEqual(r.installed_features(old), {"bambu", "spools", "usage"})
        bare = r.BEGIN + "\n[bambu_rfid card_reader_1]\nhook_stock_read: True\n" + r.END + "\n"
        self.assertEqual(r.installed_features(CFG + bare), {"bambu", "nfc", "spools", "usage"})
        self.assertEqual(r.installed_features(CFG), set())

    def test_earlier_installs_are_cleaned_from_printer_cfg(self):
        old = r.add_block(CFG + SAVED, {}).replace(
            r.SAVE_MARKER, r.MESH_BEGIN + "\nx\n" + r.MESH_END + "\n" + r.GUIDE_MARK
            + "# qidi versions (managed by qidi install script): package=1\n" + r.SAVE_MARKER)
        self.assertEqual(r.without_ours(old).strip(), (CFG + SAVED).strip())

    def test_firmware_is_the_last_one_installed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "iso_version.txt")
            with open(path, "w") as f:
                f.write("x: Add System_1.1.0\n2026-11-01: Add QD_Q2_01.01.03.02\nINIT: Version: QD_Q2_01.01.03.02\n")
            self.assertEqual(r.firmware_version(path), "QD_Q2_01.01.03.02")
            self.assertIsNone(r.firmware_version(os.path.join(d, "none")))


class InstallTests(unittest.TestCase):
    """The whole install against a fake printer laid out in a temporary folder."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        base = self.dir.name
        self.klipper = os.path.join(base, "klipper")
        os.makedirs(os.path.join(self.klipper, "klippy", "extras"))
        self.config = os.path.join(base, "printer_data", "config")
        os.makedirs(self.config)
        self.cfg = os.path.join(self.config, "printer.cfg")
        with open(self.cfg, "w") as f:
            f.write(CFG + SAVED)
        with open(os.path.join(self.config, "saved_variables.cfg"), "w") as f:
            f.write("[Variables]\nauto_read_rfid = 1\n")
        self.calls = []
        self.saved = {name: getattr(r, name) for name in
                      ("PANEL_DIR", "install_panel", "uninstall_panel", "install_key", "run_setup", "firmware_version")}
        r.PANEL_DIR = os.path.join(base, "qidi_panel")
        r.install_panel = lambda root, features, package=None: self.calls.append(("panel", tuple(features)))
        r.uninstall_panel = lambda: self.calls.append(("panel off",))
        r.install_key = lambda user, key: self.calls.append(("key",))
        r.run_setup = lambda root, package, on: self.calls.append(("setup", package["id"], on))
        r.firmware_version = lambda: "QD_Q2_01.01.03.02"

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(r, name, value)
        self.dir.cleanup()

    def run_install(self, features=None, add=None, remove=None, versions="package=1.0.0"):
        class Args:
            pass
        args = Args()
        args.root, args.klipper, args.cfg = ROOT, self.klipper, self.cfg
        args.pubkey, args.user, args.features, args.versions = "key.pub", "mks", features, versions
        args.add, args.remove = add, remove
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            r.install(args)
        return r.RESTART_MARK in out.getvalue()

    def extras(self):
        return sorted(os.listdir(os.path.join(self.klipper, "klippy", "extras")))

    def test_packages_go_on_and_off_whole(self):
        self.run_install("spools,mesh")
        packages = r.load_packages(ROOT)
        wanted = sorted(os.path.basename(t) for t in r.targets(r.closure({"spools"}, packages), packages)
                        if t.startswith("klipper/"))
        self.assertEqual(self.extras(), wanted)
        self.assertIn("bambu_rfid.py", wanted)
        own = open(os.path.join(self.config, r.OWN_CFG)).read()
        self.assertIn("box_takeover: True", own)
        self.assertIn("bambu_tags: True", own)
        self.assertIn("[gcode_macro BED_MESH_CALIBRATE]", own)
        self.assertEqual(open(self.cfg).read().count("[include qidi_patcher.cfg]"), 1)
        self.assertIn("auto_read_rfid = 0", open(os.path.join(self.config, "saved_variables.cfg")).read())
        state = json.load(open(os.path.join(self.config, r.STATE_FILE)))
        self.assertEqual(state["features"], ["mesh", "spools"])
        self.assertTrue(os.path.exists(os.path.join(self.config, "bambu_map.cfg")))

        self.run_install("guide")
        self.assertEqual(self.extras(), [])
        self.assertFalse(os.path.exists(os.path.join(self.config, r.OWN_CFG)))
        self.assertEqual(open(self.cfg).read(), CFG + SAVED)
        self.assertEqual(open(os.path.join(self.config, "saved_variables.cfg")).read(),
                         "[Variables]\nauto_read_rfid = 1\n")
        self.assertTrue(os.path.exists(os.path.join(r.PANEL_DIR, "drying_guide.html")))
        # The user's material map stays when the package goes.
        self.assertTrue(os.path.exists(os.path.join(self.config, "bambu_map.cfg")))

        self.run_install("")
        self.assertFalse(os.path.exists(os.path.join(r.PANEL_DIR, "drying_guide.html")))
        self.assertFalse(os.path.exists(os.path.join(self.config, r.STATE_FILE)))
        self.assertEqual(self.calls[-1], ("panel off",))

    def test_repeated_install_without_a_choice_keeps_what_is_on(self):
        self.run_install("mesh")
        self.run_install(None)
        state = json.load(open(os.path.join(self.config, r.STATE_FILE)))
        self.assertEqual(state["features"], ["mesh"])

    def test_package_setup_runs_on_and_off(self):
        self.run_install("usage")
        self.assertIn(("setup", "usage", True), self.calls)
        self.run_install("bambu")
        self.assertIn(("setup", "usage", False), self.calls)

    def test_one_package_goes_on_and_off_alone(self):
        self.assertTrue(self.run_install(add="mesh", versions="package=1.0.0,mesh=1.0.0"))
        self.assertFalse(self.run_install(add="guide", versions="guide=1.0.0"))
        state = json.load(open(os.path.join(self.config, r.STATE_FILE)))
        self.assertEqual(state["features"], ["guide", "mesh"])
        self.assertEqual(state["versions"], {"package": "1.0.0", "mesh": "1.0.0", "guide": "1.0.0"})
        # Files of a package already on are not copied again when another one is added.
        guide = os.path.join(r.PANEL_DIR, "drying_guide.html")
        with open(guide, "w") as f:
            f.write("edited")
        self.run_install(add="mesh", versions="mesh=1.0.1")
        self.assertEqual(open(guide).read(), "edited")
        self.assertEqual(json.load(open(os.path.join(self.config, r.STATE_FILE)))["versions"]["mesh"], "1.0.1")
        self.assertTrue(self.run_install(remove="mesh"))
        self.assertEqual(json.load(open(os.path.join(self.config, r.STATE_FILE)))["features"], ["guide"])

    def test_chosen_changes_go_in_one_run(self):
        self.run_install(add="mesh,guide")
        self.run_install(add="bambu", remove="guide")
        state = json.load(open(os.path.join(self.config, r.STATE_FILE)))
        self.assertEqual(state["features"], ["bambu", "mesh"])
        self.assertFalse(os.path.exists(os.path.join(r.PANEL_DIR, "drying_guide.html")))
        self.assertIn("bambu_rfid.py", self.extras())

    def test_removing_a_package_takes_the_ones_that_need_it(self):
        self.run_install(add="spools")
        self.run_install(remove="bambu")
        self.assertEqual(self.extras(), [])
        self.assertFalse(os.path.exists(os.path.join(self.config, r.STATE_FILE)))

    def test_unknown_or_hidden_package_is_refused(self):
        for features in ("nope", "panel"):
            with self.assertRaises(SystemExit):
                self.run_install(features)


if __name__ == "__main__":
    unittest.main()
