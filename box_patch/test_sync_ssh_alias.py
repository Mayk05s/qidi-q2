import unittest

from sync_ssh_alias import sync

CONFIG = """Host hermes
    HostName 192.168.1.20
    User hermes

Host qidi
    HostName 192.168.1.197
    User mks

Host zeta
    HostName 192.168.1.30
"""


class SyncTests(unittest.TestCase):
    def test_changes_only_the_qidi_block(self):
        out = sync(CONFIG, "10.0.0.5")
        self.assertEqual(out, CONFIG.replace("192.168.1.197", "10.0.0.5"))

    def test_same_address_is_unchanged(self):
        self.assertEqual(sync(CONFIG, "192.168.1.197"), CONFIG)

    def test_last_block_without_trailing_newline(self):
        text = "Host qidi\n  HostName 1.1.1.1"
        self.assertEqual(sync(text, "2.2.2.2"), "Host qidi\n  HostName 2.2.2.2")

    def test_no_alias_leaves_config_alone(self):
        text = "Host qidi-other\n  HostName 1.1.1.1\n"
        self.assertEqual(sync(text, "2.2.2.2"), text)


if __name__ == "__main__":
    unittest.main()
