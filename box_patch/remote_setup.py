#!/usr/bin/env python3
"""Runs on the printer as root: installs or removes packages. Safe to re-run.

Every package describes itself in box_patch/packages/<id>/package.json; this file only carries
out those descriptions and knows no package by name. A package goes on or off whole, together
with the packages it requires.

Our Klipper sections live in qidi_patcher.cfg next to printer.cfg; printer.cfg only gets one
[include] line. What is installed, with which versions and on which firmware, is kept in
qidi_patcher.json in the same folder."""
import argparse
import glob
import importlib.util
import json
import os
import pwd
import re
import shutil
import subprocess

PACKAGES_DIR = os.path.join("box_patch", "packages")
OWN_CFG = "qidi_patcher.cfg"
STATE_FILE = "qidi_patcher.json"
INCLUDE_LINE = "[include %s]  # QIDI Q2 Patcher, all its sections are in that file\n" % (OWN_CFG,)
INCLUDE = re.compile(r"^\[include %s\].*\n?" % (re.escape(OWN_CFG),), re.M)
FIRMWARE_FILE = "/home/qidi/iso_version.txt"
SAVED_VARIABLES = "saved_variables.cfg"
READERS = (("card_reader_1", "mcu_box1:PC6"), ("card_reader_2", "mcu_box1:PC7"))
SAVE_MARKER = "#*# <---------------------- SAVE_CONFIG"
BEGIN = "# >>> bambu_rfid (managed by qidi install script)"
END = "# <<< bambu_rfid"
BLOCK = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n*", re.S)

# Left in printer.cfg by earlier installs, which knew no package files; removed on the next one.
MESH_BEGIN = "# >>> print area mesh (managed by qidi install script)"
MESH_END = "# <<< print area mesh"
MESH_BLOCK = re.compile(re.escape(MESH_BEGIN) + r".*?" + re.escape(MESH_END) + r"\n*", re.S)
GUIDE_MARK = "# qidi module: guide (managed by qidi install script)\n"
VERSION_LINE = re.compile(r"^# qidi versions \(managed by qidi install script\):.*\n?", re.M)
# The reader options of those installs, by the package that sets them.
LEGACY_OPTIONS = (("bambu", "bambu_tags"), ("nfc", "custom_tags"), ("spools", "box_takeover"), ("usage", "spool_usage"))

PANEL_DIR = "/home/qidi/qidi_panel"
FEATURES_FILE = "features.json"
PANEL_UNIT = "/etc/systemd/system/qidi-panel.service"
FLUIDD_DIR = "/home/qidi/fluidd"
LINK_SCRIPT = "qidi_panel_link.js"
LINK_TAG = '<script src="/%s"></script>' % (LINK_SCRIPT,)
# Printed when Klipper has something new to read; only then the caller restarts it.
RESTART_MARK = "qidi-patcher: restart klipper"


# --- package descriptions -------------------------------------------------------------------

def load_packages(root):
    """{id: description}; each description also gets `_dir`, its folder."""
    packages = {}
    for path in sorted(glob.glob(os.path.join(root, PACKAGES_DIR, "*", "package.json"))):
        with open(path, encoding="utf-8") as f:
            package = json.load(f)
        package["_dir"] = os.path.dirname(path)
        packages[package["id"]] = package
    return packages


def visible(packages):
    return {name for name, package in packages.items() if not package.get("hidden")}


def closure(names, packages):
    """The packages together with everything they require."""
    found, todo = set(), list(names)
    while todo:
        name = todo.pop()
        if name in found:
            continue
        if name not in packages:
            raise SystemExit("Unknown package: " + name)
        found.add(name)
        todo.extend(packages[name].get("requires", ()))
    return found


def dependents(names, packages):
    """Visible packages that need one of `names`: they cannot stay without it."""
    return {p for p in visible(packages) if closure({p}, packages) & set(names)}


def touches_klipper(package):
    """Whether Klipper reads anything of this package, so that it must restart."""
    return bool(package.get("klipper_section") or package.get("klipper_options") or package.get("klipper_config")
                or package.get("saved_variables")
                or any(e["to"].startswith("klipper/") for e in package.get("install", ())))


def reader_options(enabled, packages):
    """Every option any package sets, True only where an enabled package sets it."""
    names = sorted({option for package in packages.values() for option in package.get("klipper_options", {})})
    return {name: any(packages[p].get("klipper_options", {}).get(name) for p in enabled) for name in names}


def sections(options):
    parts = []
    for name, pin in READERS:
        lines = ["[bambu_rfid %s]" % (name,), "cs_pin: %s" % (pin,)]
        lines += ["%s: %s" % (option, bool(value)) for option, value in sorted(options.items())]
        parts.append("\n".join(lines) + "\n")
    return "\n".join(parts)


def own_config(enabled, packages):
    """Everything the enabled packages add to Klipper; empty when Klipper needs nothing."""
    parts = []
    if any(packages[p].get("klipper_section") for p in enabled):
        parts.append("%s\n%s%s\n" % (BEGIN, sections(reader_options(enabled, packages)), END))
    for name in sorted(enabled):
        source = packages[name].get("klipper_config")
        if source:
            with open(os.path.join(packages[name]["_dir"], source), encoding="utf-8") as f:
                parts.append("# >>> package %s\n%s# <<< package %s\n" % (name, f.read(), name))
    return "\n".join(parts)


def targets(enabled, packages):
    """{where on the printer: install entry} for the files of the enabled packages."""
    found = {}
    for name in sorted(enabled):
        for entry in packages[name].get("install", ()):
            found[entry["to"]] = entry
    return found


def leftovers(before, after, packages):
    """Files of the packages that go away and that no remaining package needs."""
    wanted = targets(after, packages)
    gone = set()
    for name in before - after:
        package = packages[name]
        gone.update(entry["to"] for entry in package.get("install", ()) if not entry.get("keep"))
        gone.update(package.get("uninstall", ()))
    return sorted(gone - set(wanted))


def resolve(target, klipper, cfg):
    """A path from a description, as it is on this printer."""
    for prefix, base in (("klipper/", klipper), ("printer_data/config/", os.path.dirname(cfg)),
                         ("qidi_panel/", PANEL_DIR)):
        if target.startswith(prefix):
            return os.path.join(base, target[len(prefix):])
    raise SystemExit("Unknown place in a package description: " + target)


# --- printer.cfg and our own files ----------------------------------------------------------

def _above_saved(text, block):
    """Klipper requires SAVE_CONFIG to stay last."""
    pos = text.find(SAVE_MARKER)
    if pos < 0:
        return text.rstrip("\n") + "\n\n" + block
    return text[:pos] + block + text[pos:]


def remove_block(text):
    return BLOCK.sub("", text)


def add_block(text, options):
    return _above_saved(remove_block(text), "%s\n%s%s\n\n" % (BEGIN, sections(options), END))


def remove_mesh_block(text):
    return MESH_BLOCK.sub("", text)


def without_ours(text):
    """printer.cfg as it was before any install, earlier ways of installing included."""
    text = VERSION_LINE.sub("", remove_mesh_block(remove_block(text)).replace(GUIDE_MARK, ""))
    return INCLUDE.sub("", text)


def with_include(text):
    return _above_saved(without_ours(text), INCLUDE_LINE)


def installed_features(text, state=None):
    """The packages that are on. qidi_patcher.json says it; for an install made before it
    existed the blocks in `text` (printer.cfg with qidi_patcher.cfg) tell instead. A block from
    before the switches has only hook_stock_read, and the module then has all of them on."""
    if state and "features" in state:
        return set(state["features"])
    found = {"mesh"} if MESH_BLOCK.search(text) else set()
    if GUIDE_MARK in text:
        found.add("guide")
    match = BLOCK.search(text)
    if match is not None:
        for feature, option in LEGACY_OPTIONS:
            value = re.search(r"^%s:\s*(\w+)" % (option,), match.group(0), re.M)
            if value is None or value.group(1) == "True":
                found.add(feature)
    return found


def installed_versions(state):
    """{"package": "1.0.0", "bambu": "1.0.0", ...}; empty when installed without the program."""
    return dict((state or {}).get("versions") or {})


def parse_versions(value):
    if not value:
        return {}
    return dict(item.split("=", 1) for item in value.split(",") if "=" in item)


def firmware_version(path=FIRMWARE_FILE):
    """The last firmware QIDI installed, as the printer's own log names it."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    found = re.findall(r"Add (QD_\S+)", text)
    return found[-1] if found else None


def saved_variable(text, name):
    match = re.search(r"^%s = (.*)$" % (re.escape(name),), text, re.M)
    return match.group(1) if match else None


def set_saved_variable(text, name, value):
    """`value` is the text after "=", or None to remove the variable."""
    line = re.compile(r"^%s = .*\n?" % (re.escape(name),), re.M)
    if value is None:
        return line.sub("", text)
    if line.search(text):
        return line.sub(lambda m: "%s = %s\n" % (name, value), text, count=1)
    if "[Variables]" not in text:
        text = "[Variables]\n" + text
    return text.rstrip("\n") + "\n%s = %s\n" % (name, value)


def rewrite(path, text):
    # Writing in place keeps the file's owner and mode.
    with open(path, "w") as f:
        f.write(text)


def _read(path, default=""):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write_beside(path, text, cfg):
    """A new file next to printer.cfg, with its owner, so Fluidd can show and edit it."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    owner = os.stat(cfg)
    os.chown(path, owner.st_uid, owner.st_gid)
    os.chmod(path, 0o664)


def _remove(path):
    if os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.exists(path):
        os.remove(path)


def copy_files(enabled, packages, root, klipper, cfg):
    """Copy the files of `enabled`; packages that were on and stay untouched are left alone."""
    for target, entry in sorted(targets(enabled, packages).items()):
        source, path = os.path.join(root, entry["from"]), resolve(target, klipper, cfg)
        if entry.get("optional") and not os.path.exists(source):
            continue
        # A kept file holds the user's edits: only the first install creates it.
        if entry.get("keep") and os.path.exists(path):
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(source, path)
        if target.startswith("printer_data/config/"):
            owner = os.stat(cfg)
            os.chown(path, owner.st_uid, owner.st_gid)
            os.chmod(path, 0o664)


def apply_saved_variables(after, packages, path, kept):
    """Set what the enabled packages ask for and give back the old value once no package asks.
    `kept` holds the values from before us; the updated one is returned."""
    text = original = _read(path)
    kept = dict(kept or {})
    wanted = {}
    for name in sorted(after):
        wanted.update(packages[name].get("saved_variables", {}))
    for name, value in sorted(wanted.items()):
        if name not in kept:
            kept[name] = saved_variable(text, name)
        text = set_saved_variable(text, name, repr(value))
    for name in [n for n in kept if n not in wanted]:
        text = set_saved_variable(text, name, kept.pop(name))
    if text != original:
        rewrite(path, text)
    return kept


# --- actions a package can ask for ----------------------------------------------------------

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


def panel_features(enabled, packages):
    """What the panel shows: the name each visible package gives itself there."""
    return sorted(packages[p].get("panel_feature", p) for p in enabled if not packages[p].get("hidden"))


def install_panel(root, features, package):
    """`package` is the one asking for the panel: it names the service file and the Fluidd script."""
    service, script = package["service"], package["fluidd_script"]
    account = pwd.getpwnam("qidi")
    os.makedirs(PANEL_DIR, exist_ok=True)
    with open(os.path.join(PANEL_DIR, FEATURES_FILE), "w") as f:
        json.dump(features, f)
    for name in os.listdir(PANEL_DIR):
        os.chown(os.path.join(PANEL_DIR, name), account.pw_uid, account.pw_gid)
    os.chown(PANEL_DIR, account.pw_uid, account.pw_gid)
    shutil.copyfile(os.path.join(root, service), PANEL_UNIT)
    shutil.copyfile(os.path.join(root, script), os.path.join(FLUIDD_DIR, LINK_SCRIPT))
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
    _remove(os.path.join(FLUIDD_DIR, LINK_SCRIPT))


def run_setup(root, package, on):
    """A package's own root step: a module with install(source) and uninstall()."""
    path = os.path.join(root, package["setup"])
    spec = importlib.util.spec_from_file_location("package_setup_" + package["id"], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if on:
        module.install(root)
    else:
        module.uninstall()


# --- install and uninstall ------------------------------------------------------------------

def _names(value, packages):
    names = {name for name in (value or "").split(",") if name}
    unknown = names - visible(packages)
    if unknown:
        raise SystemExit("Unknown packages: " + ", ".join(sorted(unknown)))
    return names


def chosen_features(args, on_now, packages):
    """--features names the whole set; --add and --remove change what is on, a removed package
    taking along the ones that need it. With none of them a repeated install keeps what is on,
    and the first one turns on everything."""
    if args.features is not None:
        return _names(args.features, packages)
    add, remove = _names(getattr(args, "add", None), packages), _names(getattr(args, "remove", None), packages)
    on = on_now.intersection(visible(packages))
    if not add and not remove:
        return on or visible(packages)
    return (on | add) - dependents(remove, packages)


def to_copy(args, before, after, packages):
    """New packages and the ones asked for again; everything when the whole set is given."""
    if args.features is not None or not (getattr(args, "add", None) or getattr(args, "remove", None)):
        return after
    return (after - before) | closure(_names(getattr(args, "add", None), packages), packages)


def install(args):
    packages = load_packages(args.root)
    folder = os.path.dirname(args.cfg)
    own_path, state_path = os.path.join(folder, OWN_CFG), os.path.join(folder, STATE_FILE)
    text = _read(args.cfg)
    state = json.loads(_read(state_path, "{}"))
    on_now = installed_features(text + "\n" + _read(own_path), state).intersection(packages)
    features = chosen_features(args, on_now, packages)
    before, after = closure(on_now, packages), closure(features, packages)
    copied = to_copy(args, before, after, packages)
    old_config, old_saved = _read(own_path), _read(os.path.join(folder, SAVED_VARIABLES))

    backup = args.cfg + ".before_bambu_rfid"
    if not os.path.exists(backup):
        shutil.copy2(args.cfg, backup)
    config = own_config(after, packages)
    if config:
        _write_beside(own_path, config, args.cfg)
        rewrite(args.cfg, with_include(text))
    else:
        rewrite(args.cfg, without_ours(text))
        _remove(own_path)

    for name in sorted(before - after):
        if packages[name].get("setup"):
            run_setup(args.root, packages[name], False)
    for target in leftovers(before, after, packages):
        _remove(resolve(target, args.klipper, args.cfg))
    copy_files(copied, packages, args.root, args.klipper, args.cfg)
    saved_before = apply_saved_variables(after, packages, os.path.join(folder, SAVED_VARIABLES),
                                         state.get("saved_before"))

    hooks_after = {hook for p in after for hook in packages[p].get("hooks", ())}
    hooks_before = {hook for p in before for hook in packages[p].get("hooks", ())}
    if "ssh_key" in hooks_after and args.pubkey:
        install_key(args.user, args.pubkey)
    if "panel" in hooks_after:
        owner = next(packages[p] for p in sorted(after) if "panel" in packages[p].get("hooks", ()))
        install_panel(args.root, panel_features(after, packages), owner)
    elif "panel" in hooks_before:
        uninstall_panel()
    for name in sorted(after):
        if packages[name].get("setup"):
            run_setup(args.root, packages[name], True)

    if not features:
        _remove(state_path)
    else:
        # The versions given are those of the packages copied now; the others keep theirs.
        # Without versions, as from setup.sh, the printer's copy counts as of unknown version.
        versions = {name: value for name, value in installed_versions(state).items()
                    if name == "package" or name in after}
        versions.update(parse_versions(args.versions))
        _write_beside(state_path, json.dumps({
            "features": sorted(features), "versions": versions,
            "firmware": firmware_version(), "saved_before": saved_before}, indent=1), args.cfg)

    changed = (before ^ after) | copied
    if any(touches_klipper(packages[p]) for p in changed) or _read(own_path) != old_config \
            or _read(os.path.join(folder, SAVED_VARIABLES)) != old_saved:
        print(RESTART_MARK)


def uninstall(args):
    args.features = ""
    install(args)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--root", required=True, help="the unpacked bundle, laid out like the project")
    parser.add_argument("--klipper", required=True)
    parser.add_argument("--cfg", required=True)
    parser.add_argument("--pubkey")
    parser.add_argument("--user")
    parser.add_argument("--features", help="the whole set of packages to have on")
    parser.add_argument("--add", help="packages to install or update, the rest stays as it is")
    parser.add_argument("--remove", help="packages to remove, with the ones that need them")
    parser.add_argument("--versions", help="versions of the packages copied now")
    args = parser.parse_args()
    (install if args.action == "install" else uninstall)(args)


if __name__ == "__main__":
    main()
