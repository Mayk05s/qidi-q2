import os
import tempfile
import unittest

import remote_setup as r

CFG = "[printer]\nkinematics: corexy\n\n"
SAVED = r.SAVE_MARKER + " SAVE_CONFIG\n#*# [extruder]\n#*# x = 1\n"


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


class MapInstallTests(unittest.TestCase):
    def test_created_once_and_edits_survive(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, source = os.path.join(d, "printer.cfg"), os.path.join(d, "src.cfg")
            target = os.path.join(d, "bambu_map.cfg")
            for path, text in ((cfg, "x"), (source, "default")):
                with open(path, "w") as f:
                    f.write(text)
            r.install_map(source, cfg)
            with open(target) as f:
                self.assertEqual(f.read(), "default")
            with open(target, "w") as f:
                f.write("edited")
            r.install_map(source, cfg)
            with open(target) as f:
                self.assertEqual(f.read(), "edited")


class CfgTests(unittest.TestCase):
    def test_block_goes_above_save_config(self):
        out = r.add_block(CFG + SAVED)
        self.assertLess(out.index("[bambu_rfid card_reader_1]"), out.index(r.SAVE_MARKER))
        self.assertTrue(out.endswith(SAVED))

    def test_appended_without_save_config(self):
        out = r.add_block(CFG)
        self.assertTrue(out.startswith("[printer]"))
        self.assertIn("mcu_box1:PC7", out)

    def test_reinstall_does_not_duplicate(self):
        once = r.add_block(CFG + SAVED)
        self.assertEqual(r.add_block(once), once)
        self.assertEqual(once.count("[bambu_rfid card_reader_1]"), 1)

    def test_uninstall_restores_original_content(self):
        for original in (CFG + SAVED, CFG):
            out = r.remove_block(r.add_block(original))
            self.assertEqual(out.strip(), original.strip())
            self.assertNotIn("bambu_rfid", out)


class FeatureTests(unittest.TestCase):
    OLD_BLOCK = (r.BEGIN + "\n[bambu_rfid card_reader_1]\ncs_pin: mcu_box1:PC6\n"
                 "hook_stock_read: True\n" + r.END + "\n")

    class Args:
        features = None

    def test_block_says_which_features_are_on(self):
        for features in ({"bambu"}, {"nfc"}, {"spools"}, {"usage"}, {"bambu", "usage"}, set(r.MODULE_FEATURES)):
            self.assertEqual(r.installed_features(r.add_block(CFG, features)), features)

    def test_stock_read_is_wrapped_only_for_tags(self):
        self.assertIn("hook_stock_read: True", r.sections({"nfc"}))
        self.assertIn("hook_stock_read: True", r.sections({"bambu"}))
        self.assertIn("hook_stock_read: False", r.sections({"usage"}))

    def test_no_block_means_nothing_installed(self):
        self.assertEqual(r.installed_features(CFG), set())

    def test_block_from_before_the_switches_counts_as_all_on(self):
        self.assertEqual(r.installed_features(CFG + self.OLD_BLOCK), set(r.MODULE_FEATURES))

    def test_install_without_a_choice_keeps_what_is_on(self):
        args = self.Args()
        self.assertEqual(r.chosen_features(args, r.add_block(CFG, {"usage"})), {"usage"})
        self.assertEqual(r.chosen_features(args, CFG), set(r.MODULE_FEATURES))

    def test_choice_replaces_what_is_on_and_may_be_empty(self):
        args = self.Args()
        args.features = "nfc,spools"
        self.assertEqual(r.chosen_features(args, r.add_block(CFG, {"usage"})), {"nfc", "spools"})
        args.features = ""
        self.assertEqual(r.chosen_features(args, r.add_block(CFG)), set())

    def test_mesh_block_goes_above_save_config_and_comes_off_cleanly(self):
        out = r.add_mesh_block(r.add_block(CFG + SAVED))
        self.assertLess(out.index(r.MESH_BEGIN), out.index(r.SAVE_MARKER))
        self.assertEqual(out.count("[gcode_macro BED_MESH_CALIBRATE]"), 1)
        self.assertEqual(r.add_mesh_block(out), out)
        self.assertEqual(r.installed_features(out), {"mesh"} | set(r.MODULE_FEATURES))
        self.assertEqual(r.remove_mesh_block(r.remove_block(out)).strip(), (CFG + SAVED).strip())

    def test_mesh_alone_is_a_choice_and_is_kept(self):
        args = self.Args()
        self.assertEqual(r.installed_features(r.add_mesh_block(CFG)), {"mesh"})
        self.assertEqual(r.chosen_features(args, r.add_mesh_block(CFG)), {"mesh"})
        args.features = "mesh"
        self.assertEqual(r.chosen_features(args, CFG), {"mesh"})

    def test_guide_mark_is_recorded_and_comes_off_cleanly(self):
        out = r.add_guide_mark(CFG + SAVED)
        self.assertEqual(r.add_guide_mark(out), out)
        self.assertLess(out.index(r.GUIDE_MARK), out.index(r.SAVE_MARKER))
        self.assertEqual(r.installed_features(out), {"guide"})
        self.assertEqual(r.remove_guide_mark(out).strip(), (CFG + SAVED).strip())

    def test_versions_are_recorded_replaced_and_removed(self):
        out = r.set_versions(CFG + SAVED, {"package": "1.1.0", "bambu": "1.0.2"})
        self.assertEqual(r.installed_versions(out), {"package": "1.1.0", "bambu": "1.0.2"})
        self.assertLess(out.index(r.VERSION_MARK), out.index(r.SAVE_MARKER))
        out = r.set_versions(out, {"package": "1.2.0"})
        self.assertEqual(r.installed_versions(out), {"package": "1.2.0"})
        self.assertEqual(r.set_versions(out, {}).strip(), (CFG + SAVED).strip())
        self.assertEqual(r.parse_versions("package=1.0,bambu=2.0"), {"package": "1.0", "bambu": "2.0"})

    def test_unknown_feature_is_refused(self):
        args = self.Args()
        args.features = "nfc,oops"
        with self.assertRaises(SystemExit):
            r.chosen_features(args, CFG)


if __name__ == "__main__":
    unittest.main()
