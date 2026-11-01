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


if __name__ == "__main__":
    unittest.main()
