#!/usr/bin/env python3
"""Adds the plastic check to QIDI Studio as a separate process profile.

Studio keeps the script path in its settings, and the project path has a space, so the
script is copied to a folder without one. System profiles are left as they are.
"""

import json
import pathlib
import re
import shutil
import subprocess
import sys

PROJECT = pathlib.Path(__file__).resolve().parent
TARGET = pathlib.Path.home() / ".qidi_studio_check"
STUDIO = pathlib.Path.home() / "Library" / "Application Support" / "QIDIStudio"
BASE_PROFILE = "0.20mm Standard @Q2"
PROFILE = BASE_PROFILE.replace(" @", " + проверка пластика @")


def printer_host():
    match = re.search(r"^PRINTER_HOST=(\S+)", (PROJECT.parent / ".env").read_text(encoding="utf-8"), re.M)
    if not match:
        sys.exit("Нет PRINTER_HOST в файле .env")
    return match.group(1)


def account_folder():
    config = json.loads((STUDIO / "QIDIStudio.conf").read_text(encoding="utf-8"))
    return (config.get("app") or {}).get("preset_folder") or "default"


def main():
    TARGET.mkdir(exist_ok=True)
    shutil.copy(PROJECT.parent / "box_patch" / "packages" / "usage" / "printer" / "print_check.py", TARGET / "print_check.py")
    shutil.copy(PROJECT / "studio_check.py", TARGET / "studio_check.py")
    # Without the compiled window the check falls back to a plain system dialog.
    built = subprocess.run(["swiftc", "-swift-version", "5", "-O", "-o", str(TARGET / "studio_window"),
                            str(PROJECT / "studio_window.swift")])
    if built.returncode != 0:
        print("Окно не собралось, будет простой системный диалог")
    (TARGET / "printer_host.txt").write_text(printer_host() + "\n", encoding="utf-8")
    folder = STUDIO / "user" / account_folder() / "process"
    folder.mkdir(parents=True, exist_ok=True)
    profile = {
        "type": "process", "name": PROFILE, "from": "User", "inherits": BASE_PROFILE,
        "print_settings_id": PROFILE, "version": "2.7.2.73", "is_custom_defined": "0",
        "post_process": ["/usr/bin/python3 %s" % (TARGET / "studio_check.py",)],
    }
    path = folder / (PROFILE + ".json")
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    print("Профиль в QIDI Studio: %s" % (PROFILE,))


if __name__ == "__main__":
    main()
