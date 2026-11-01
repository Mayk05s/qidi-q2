#!/usr/bin/env python3
"""Runs on the printer as root: installs or removes bambu_rfid and the panel. Safe to re-run."""
import argparse
import os
import pwd
import re
import shutil
import subprocess

BEGIN = "# >>> bambu_rfid (managed by qidi install script)"
END = "# <<< bambu_rfid"
SECTIONS = """[bambu_rfid card_reader_1]
cs_pin: mcu_box1:PC6
hook_stock_read: True

[bambu_rfid card_reader_2]
cs_pin: mcu_box1:PC7
hook_stock_read: True
"""
SAVE_MARKER = "#*# <---------------------- SAVE_CONFIG"
BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n*", re.S)


def remove_block(text):
    return BLOCK.sub("", text)


def add_block(text):
    """Put the managed block above SAVE_CONFIG, which Klipper requires to stay last."""
    text = remove_block(text)
    block = "%s\n%s%s\n\n" % (BEGIN, SECTIONS, END)
    pos = text.find(SAVE_MARKER)
    if pos < 0:
        return text.rstrip("\n") + "\n\n" + block
    return text[:pos] + block + text[pos:]


def rewrite(path, text):
    # Writing in place keeps the file's owner and mode.
    with open(path, "w") as f:
        f.write(text)


def install_key(user, pubkey_path):
    account = pwd.getpwnam(user)
    ssh_dir = os.path.join(account.pw_dir, ".ssh")
    keys_path = os.path.join(ssh_dir, "authorized_keys")
    os.makedirs(ssh_dir, exist_ok=True)
    with open(pubkey_path) as f:
        key = f.read().strip()
    existing = open(keys_path).read() if os.path.exists(keys_path) else ""
    if key not in existing:
        with open(keys_path, "a") as f:
            f.write(key + "\n")
    for path, mode in ((ssh_dir, 0o700), (keys_path, 0o600)):
        os.chown(path, account.pw_uid, account.pw_gid)
        os.chmod(path, mode)
    # sshd ignores keys when the home folder has a foreign owner or is writable by others.
    home = os.stat(account.pw_dir)
    print("Домашняя папка %s: владелец %s, права %o" % (
        account.pw_dir, pwd.getpwuid(home.st_uid).pw_name, home.st_mode & 0o777))


def install_map(source, cfg):
    """Create the editable map once. A later install never overwrites user edits."""
    target = os.path.join(os.path.dirname(cfg), "bambu_map.cfg")
    if os.path.exists(target):
        return
    shutil.copyfile(source, target)
    owner = os.stat(cfg)
    os.chown(target, owner.st_uid, owner.st_gid)
    os.chmod(target, 0o664)


PANEL_DIR = "/home/qidi/qidi_panel"
PANEL_UNIT = "/etc/systemd/system/qidi-panel.service"
FLUIDD_DIR = "/home/qidi/fluidd"
LINK_SCRIPT = "qidi_panel_link.js"
LINK_TAG = '<script src="/%s"></script>' % (LINK_SCRIPT,)
PANEL_FILES = ("qidi_tag_tool.py", "sample_model.py", "sample_font.json", "sample_project_settings.json",
               "qidi_materials.json", "bambu_rfid.py")
KEEP_IF_PRESENT = ("my_materials.json", "my_colors.json")


def add_link_tag(html):
    if LINK_TAG in html:
        return html
    if "</body>" not in html:
        raise SystemExit("Fluidd page has no </body>")
    return html.replace("</body>", LINK_TAG + "</body>", 1)


def remove_link_tag(html):
    return html.replace(LINK_TAG, "")


def _edit_fluidd(editor):
    path = os.path.join(FLUIDD_DIR, "index.html")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    updated = editor(text)
    if updated != text:
        rewrite(path, updated)


def _service(*command):
    subprocess.run(["systemctl"] + list(command), check=True)


def install_panel(source):
    account = pwd.getpwnam("qidi")
    os.makedirs(PANEL_DIR, exist_ok=True)
    for name in PANEL_FILES:
        shutil.copyfile(os.path.join(source, name), os.path.join(PANEL_DIR, name))
    # The user's choices made on the printer stay; the Mac's copy is only a first start.
    for name in KEEP_IF_PRESENT:
        target, origin = os.path.join(PANEL_DIR, name), os.path.join(source, name)
        if not os.path.exists(target) and os.path.exists(origin):
            shutil.copyfile(origin, target)
    for name in os.listdir(PANEL_DIR):
        os.chown(os.path.join(PANEL_DIR, name), account.pw_uid, account.pw_gid)
    os.chown(PANEL_DIR, account.pw_uid, account.pw_gid)
    shutil.copyfile(os.path.join(source, "qidi-panel.service"), PANEL_UNIT)
    shutil.copyfile(os.path.join(source, LINK_SCRIPT), os.path.join(FLUIDD_DIR, LINK_SCRIPT))
    os.chmod(os.path.join(FLUIDD_DIR, LINK_SCRIPT), 0o644)
    _edit_fluidd(add_link_tag)
    _service("daemon-reload")
    _service("enable", "qidi-panel.service")
    _service("restart", "qidi-panel.service")


def uninstall_panel():
    # Saved palette and materials stay in the panel folder for a later install.
    subprocess.run(["systemctl", "disable", "--now", "qidi-panel.service"], check=False)
    if os.path.exists(PANEL_UNIT):
        os.remove(PANEL_UNIT)
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    _edit_fluidd(remove_link_tag)
    script = os.path.join(FLUIDD_DIR, LINK_SCRIPT)
    if os.path.exists(script):
        os.remove(script)


def install(args):
    extras = os.path.join(args.klipper, "klippy", "extras")
    if not os.path.isdir(extras):
        raise SystemExit("Klipper extras folder not found: " + extras)
    shutil.copyfile(args.module, os.path.join(extras, "bambu_rfid.py"))
    backup = args.cfg + ".before_bambu_rfid"
    if not os.path.exists(backup):
        shutil.copy2(args.cfg, backup)
    with open(args.cfg) as f:
        text = f.read()
    rewrite(args.cfg, add_block(text))
    if args.map:
        install_map(args.map, args.cfg)
    if args.pubkey:
        install_key(args.user, args.pubkey)
    if args.panel:
        install_panel(args.panel)


def uninstall(args):
    with open(args.cfg) as f:
        text = f.read()
    rewrite(args.cfg, remove_block(text))
    path = os.path.join(args.klipper, "klippy", "extras", "bambu_rfid.py")
    if os.path.exists(path):
        os.remove(path)
    uninstall_panel()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--klipper", required=True)
    parser.add_argument("--cfg", required=True)
    parser.add_argument("--module")
    parser.add_argument("--pubkey")
    parser.add_argument("--user")
    parser.add_argument("--map")
    parser.add_argument("--panel")
    args = parser.parse_args()
    (install if args.action == "install" else uninstall)(args)


if __name__ == "__main__":
    main()
