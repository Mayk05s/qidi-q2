#!/usr/bin/env python3
"""Runs on the printer as root: installs or removes bambu_rfid and the panel. Safe to re-run."""
import argparse
import json
import os
import pwd
import re
import shutil
import subprocess

BEGIN = "# >>> bambu_rfid (managed by qidi install script)"
END = "# <<< bambu_rfid"
READERS = (("card_reader_1", "mcu_box1:PC6"), ("card_reader_2", "mcu_box1:PC7"))
MODULE_FILES = ("bambu_rfid.py", "box_feed.py")
MODULE_FEATURES = ("bambu", "nfc", "spools", "usage")
TAG_FEATURES = ("bambu", "nfc")
# The panel shows the tags on the slots, writes tags and shows what is left on the spools.
PANEL_FEATURES = ("bambu", "nfc", "usage", "guide")
KNOWN_FEATURES = MODULE_FEATURES + ("mesh", "guide")
# The guide changes nothing in Klipper; this line only records that it is switched on.
GUIDE_MARK = "# qidi module: guide (managed by qidi install script)\n"
OPTIONS = (("bambu", "bambu_tags"), ("nfc", "custom_tags"), ("spools", "box_takeover"), ("usage", "spool_usage"))
SAVE_MARKER = "#*# <---------------------- SAVE_CONFIG"
BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n*", re.S)
MESH_BEGIN = "# >>> print area mesh (managed by qidi install script)"
MESH_END = "# <<< print area mesh"
MESH_BLOCK = re.compile(re.escape(MESH_BEGIN) + r".*?" + re.escape(MESH_END) + r"\n*", re.S)
# Replaces the macro of klipper-macros-qd/KAMP_Settings.cfg, which always probes the full depth of
# the bed and at least X 60-210. keep_purge keeps the stock purge line (X 108-158 at the front,
# partly at Z-0.1) inside the mesh: outside it Klipper would apply the height of the nearest edge.
# G29 asks for the profile kamp before a print; other callers and files without objects get a full mesh.
MESH_MACRO = """[gcode_macro BED_MESH_CALIBRATE]
variable_margin: 10
variable_keep_purge: 1
gcode:
    {% set pts = printer.exclude_object.objects | map(attribute='polygon') | sum(start=[]) %}
    {% set cfg = printer.configfile.settings.bed_mesh %}
    {% set prof = params.PROFILE|default("default") %}
    {% if pts and prof == "kamp" %}
        {% set xs = pts | map(attribute=0) | list %}
        {% set ys = pts | map(attribute=1) | list %}
        {% set x0 = [xs|min - margin, cfg.mesh_min[0]]|max %}
        {% set x1 = [xs|max + margin, cfg.mesh_max[0]]|min %}
        {% set y0 = [ys|min - margin, cfg.mesh_min[1]]|max %}
        {% set y1 = [ys|max + margin, cfg.mesh_max[1]]|min %}
        {% if keep_purge|int == 1 %}
            {% set x0 = [x0, 108]|min %}
            {% set x1 = [x1, 158]|max %}
            {% set y0 = cfg.mesh_min[1] %}
        {% endif %}
        {% set dx = (cfg.mesh_max[0]-cfg.mesh_min[0])/(cfg.probe_count[0]-1) %}
        {% set dy = (cfg.mesh_max[1]-cfg.mesh_min[1])/(cfg.probe_count[1]-1) %}
        {% set nx = [[((x1-x0)/dx)|round(0,'ceil')|int + 1, 4]|max, cfg.probe_count[0]]|min %}
        {% set ny = [[((y1-y0)/dy)|round(0,'ceil')|int + 1, 4]|max, cfg.probe_count[1]]|min %}
        _BED_MESH_CALIBRATE mesh_min={x0},{y0} mesh_max={x1},{y1} ALGORITHM=lagrange PROBE_COUNT={nx},{ny} PROFILE={prof}
    {% else %}
        _BED_MESH_CALIBRATE PROFILE={prof}
    {% endif %}
"""


def remove_block(text):
    return BLOCK.sub("", text)


def remove_mesh_block(text):
    return MESH_BLOCK.sub("", text)


VERSION_MARK = "# qidi versions (managed by qidi install script):"
VERSION_LINE = re.compile("^" + re.escape(VERSION_MARK) + r"(.*)\n?", re.M)


def installed_versions(text):
    """{"package": "1.1.0", "bambu": "1.0.0", ...}; empty when installed without the program."""
    match = VERSION_LINE.search(text)
    if match is None:
        return {}
    return dict(item.split("=", 1) for item in match.group(1).split() if "=" in item)


def set_versions(text, versions):
    text = VERSION_LINE.sub("", text)
    if not versions:
        return text
    items = " ".join("%s=%s" % (name, versions[name]) for name in sorted(versions))
    return _above_saved(text, "%s %s\n" % (VERSION_MARK, items))


def parse_versions(value):
    if not value:
        return {}
    return dict(item.split("=", 1) for item in value.split(",") if "=" in item)


def remove_guide_mark(text):
    return text.replace(GUIDE_MARK, "")


def add_guide_mark(text):
    return _above_saved(remove_guide_mark(text), GUIDE_MARK)


def _above_saved(text, block):
    """Klipper requires SAVE_CONFIG to stay last."""
    pos = text.find(SAVE_MARKER)
    if pos < 0:
        return text.rstrip("\n") + "\n\n" + block
    return text[:pos] + block + text[pos:]


def add_mesh_block(text):
    text = remove_mesh_block(text)
    return _above_saved(text, "%s\n%s%s\n\n" % (MESH_BEGIN, MESH_MACRO, MESH_END))


def sections(features):
    parts = []
    for name, pin in READERS:
        lines = ["[bambu_rfid %s]" % (name,), "cs_pin: %s" % (pin,),
                 "hook_stock_read: %s" % (bool(set(TAG_FEATURES).intersection(features)),)]
        lines += ["%s: %s" % (option, feature in features) for feature, option in OPTIONS]
        parts.append("\n".join(lines) + "\n")
    return "\n".join(parts)


def installed_features(text):
    """What the managed block in printer.cfg turns on. A block from before the switches
    has only hook_stock_read, and the module then has all of them on by default."""
    found = {"mesh"} if MESH_BLOCK.search(text) else set()
    if GUIDE_MARK in text:
        found.add("guide")
    match = BLOCK.search(text)
    if match is None:
        return found
    block = match.group(0)
    for feature, option in OPTIONS:
        value = re.search(r"^%s:\s*(\w+)" % (option,), block, re.M)
        if value is None or value.group(1) == "True":
            found.add(feature)
    return found


def add_block(text, features=MODULE_FEATURES):
    text = remove_block(text)
    return _above_saved(text, "%s\n%s%s\n\n" % (BEGIN, sections(features), END))


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
FEATURES_FILE = "features.json"
PANEL_UNIT = "/etc/systemd/system/qidi-panel.service"
FLUIDD_DIR = "/home/qidi/fluidd"
LINK_SCRIPT = "qidi_panel_link.js"
LINK_TAG = '<script src="/%s"></script>' % (LINK_SCRIPT,)
PANEL_FILES = ("drying_guide.html", "box_feed.py", "qidi_tag_tool.py", "print_check.py", "sample_model.py", "sample_font.json", "sample_project_settings.json",
               "qidi_materials.json", "bambu_rfid.py", "screen_popup.py", "plastic_picture.py", "slot_font.json")
# Run as root next to this file during the install; they change files outside the panel folder.
SETUP_FILES = ("screen_popup_setup.py",)
OPTIONAL_PANEL_FILES = ("drying_guide.html",)
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


def install_panel(source, features=MODULE_FEATURES):
    account = pwd.getpwnam("qidi")
    os.makedirs(PANEL_DIR, exist_ok=True)
    for name in PANEL_FILES:
        origin = os.path.join(source, name)
        # setup.sh does not carry the guide; the panel then has no guide tab content.
        if name in OPTIONAL_PANEL_FILES and not os.path.exists(origin):
            continue
        shutil.copyfile(origin, os.path.join(PANEL_DIR, name))
    # The user's choices made on the printer stay; the Mac's copy is only a first start.
    for name in KEEP_IF_PRESENT:
        target, origin = os.path.join(PANEL_DIR, name), os.path.join(source, name)
        if not os.path.exists(target) and os.path.exists(origin):
            shutil.copyfile(origin, target)
    # The panel shows only what is switched on.
    with open(os.path.join(PANEL_DIR, FEATURES_FILE), "w") as f:
        json.dump(sorted(features), f)
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
    screen_popup(False)
    subprocess.run(["systemctl", "disable", "--now", "qidi-panel.service"], check=False)
    if os.path.exists(PANEL_UNIT):
        os.remove(PANEL_UNIT)
    subprocess.run(["systemctl", "daemon-reload"], check=False)
    _edit_fluidd(remove_link_tag)
    script = os.path.join(FLUIDD_DIR, LINK_SCRIPT)
    if os.path.exists(script):
        os.remove(script)


def chosen_features(args, text):
    """Without --features a repeated install keeps what is on; the first one turns on all."""
    if args.features is None:
        return installed_features(text) or set(MODULE_FEATURES)
    chosen = {name for name in args.features.split(",") if name}
    unknown = chosen - set(KNOWN_FEATURES)
    if unknown:
        raise SystemExit("Unknown features: " + ", ".join(sorted(unknown)))
    return chosen


def remove_module_files(args):
    for name in MODULE_FILES:
        path = os.path.join(args.klipper, "klippy", "extras", name)
        if os.path.exists(path):
            os.remove(path)


def install_or_remove_panel(args, features):
    if not args.panel:
        return
    if features.intersection(PANEL_FEATURES):
        install_panel(args.panel, features)
        screen_popup("usage" in features, args.panel)
    else:
        uninstall_panel()


def screen_popup(on, source=None):
    """The low-plastic window on the printer screen belongs to the usage module."""
    import screen_popup_setup  # next to this file in the install bundle
    if on:
        screen_popup_setup.install(source)
    else:
        screen_popup_setup.uninstall()


def install(args):
    with open(args.cfg) as f:
        text = f.read()
    features = chosen_features(args, text)
    backup = args.cfg + ".before_bambu_rfid"
    if not os.path.exists(backup):
        shutil.copy2(args.cfg, backup)
    text = add_mesh_block(text) if "mesh" in features else remove_mesh_block(text)
    text = add_guide_mark(text) if "guide" in features else remove_guide_mark(text)
    # Without versions, as from setup.sh, the printer's copy counts as of unknown version.
    text = set_versions(text, parse_versions(args.versions) if features else {})
    module_features = features.intersection(MODULE_FEATURES)
    if not module_features:
        rewrite(args.cfg, remove_block(text))
        remove_module_files(args)
        install_or_remove_panel(args, features)
        return
    extras = os.path.join(args.klipper, "klippy", "extras")
    if not os.path.isdir(extras):
        raise SystemExit("Klipper extras folder not found: " + extras)
    for name in MODULE_FILES:
        shutil.copyfile(os.path.join(os.path.dirname(args.module), name), os.path.join(extras, name))
    rewrite(args.cfg, add_block(text, module_features))
    if args.map:
        install_map(args.map, args.cfg)
    if args.pubkey:
        install_key(args.user, args.pubkey)
    install_or_remove_panel(args, features)


def uninstall(args):
    with open(args.cfg) as f:
        text = f.read()
    rewrite(args.cfg, set_versions(remove_guide_mark(remove_mesh_block(remove_block(text))), {}))
    remove_module_files(args)
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
    parser.add_argument("--features")
    parser.add_argument("--versions")
    args = parser.parse_args()
    (install if args.action == "install" else uninstall)(args)


if __name__ == "__main__":
    main()
