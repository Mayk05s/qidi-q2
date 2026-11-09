#!/usr/bin/env python3
"""Build "QIDI Q2 Patcher.exe" in dist/ with PyInstaller. Run it on Windows: PyInstaller makes a
program only for the system it runs on (GitHub builds it in .github/workflows/windows-exe.yml).
Needs: pip install pyinstaller paramiko. The packages go inside the program, so it needs no project
folder; which files they are comes from the packages themselves, as for the printer."""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "installer"))
import qidi_backend  # noqa: E402

# What the window reads here, besides what goes to the printer.
OWN_FILES = ("installer/patcher.json",)


def bundled():
    return sorted(set(qidi_backend.bundle_files(qidi_backend.load_packages())) | set(OWN_FILES))


def windows_line_ends(files):
    """A checkout with Windows line ends would put \\r into the printer's configs and scripts."""
    found = []
    for relative in files:
        data = (ROOT / relative).read_bytes()
        if b"\0" not in data[:1024] and b"\r\n" in data:
            found.append(relative)
    return found


def main():
    import PyInstaller.__main__
    files = bundled()
    broken = windows_line_ends(files)
    if broken:
        sys.exit("Файлы с переводами строк Windows (CRLF), на принтере они не заработают:\n  "
                 + "\n  ".join(broken) + "\nКлонируй заново после: git config --global core.autocrlf false")
    with tempfile.TemporaryDirectory() as work:
        arguments = ["--noconfirm", "--clean", "--onefile", "--windowed", "--name", "QIDI Q2 Patcher",
                     "--distpath", str(ROOT / "dist"), "--workpath", work, "--specpath", work,
                     "--paths", str(ROOT / "installer"), "--paths", str(ROOT / "box_patch"),
                     "--hidden-import", "paramiko", "--hidden-import", "sync_ssh_alias"]
        for relative in files:
            arguments += ["--add-data", "%s%s%s" % (ROOT / relative, os.pathsep, pathlib.PurePath(relative).parent)]
        PyInstaller.__main__.run(arguments + [str(ROOT / "installer" / "qidi_window.py")])
    print(ROOT / "dist")


if __name__ == "__main__":
    main()
