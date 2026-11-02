#!/usr/bin/env python3
"""Build "QIDI Q2 Patcher.app" in the project folder. The app runs the backend from this folder."""
import json
import pathlib
import plistlib
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "QIDI Q2 Patcher.app"


def main():
    if APP.exists():
        shutil.rmtree(APP)
    macos = APP / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    subprocess.run(["swiftc", "-O", "-parse-as-library", "-target", "arm64-apple-macosx13.0",
                    str(ROOT / "installer" / "QidiInstaller.swift"), "-o", str(macos / "QidiQ2Patcher")], check=True)
    with open(APP / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({
            "CFBundleName": "QIDI Q2 Patcher",
            "CFBundleDisplayName": "QIDI Q2 Patcher",
            "CFBundleIdentifier": "local.qidi.q2-patcher",
            "CFBundleExecutable": "QidiQ2Patcher",
            "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": json.loads(
                (ROOT / "installer" / "patcher.json").read_text(encoding="utf-8"))["version"],
            "LSMinimumSystemVersion": "13.0",
            "NSHighResolutionCapable": True,
            "NSLocalNetworkUsageDescription": "Программа подключается к принтеру в локальной сети.",
            "QidiProjectRoot": str(ROOT),
        }, f)
    # Without a signature macOS on Apple silicon refuses to start the program.
    subprocess.run(["codesign", "--force", "--sign", "-", str(APP)], check=True)
    print(APP)


if __name__ == "__main__":
    main()
