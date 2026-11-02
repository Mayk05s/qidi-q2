"""Root part of the low-plastic window: run by remote_setup.py on install and uninstall.

The panel service runs as qidi. It may write the two borrowed window files and the picture
folder, but only root restarts the screen program, so a path unit does that when the panel
touches a flag file.
"""

import os
import pwd
import shutil
import subprocess

CLIENT = "/home/qidi/QIDI_Client"
ERRORS = os.path.join(CLIENT, "resource", "qidi_client_error")
PICTURES = os.path.join(CLIENT, "access", "plastic")
PANEL = "/home/qidi/qidi_panel"
STOCK = os.path.join(PANEL, "screen_stock")
FLAG = os.path.join(PANEL, "screen_reload")
CODES = ("QDC_300_002_005_003", "QDC_300_002_005_004")
UNITS = {
    "qidi-screen-reload.path": "[Unit]\nDescription=Restart the screen program for the plastic window\n\n"
                               "[Path]\nPathModified=%s\n\n[Install]\nWantedBy=multi-user.target\n" % (FLAG,),
    "qidi-screen-reload.service": "[Unit]\nDescription=Restart the screen program for the plastic window\n\n"
                                  "[Service]\nType=oneshot\nExecStart=/bin/systemctl restart qidi-client.service\n",
}
UNIT_DIR = "/etc/systemd/system"


def _qidi():
    account = pwd.getpwnam("qidi")
    return account.pw_uid, account.pw_gid


def install(source=None, run=subprocess.run):
    """Keeps the stock files once: a second install must not save our own text as stock."""
    if not os.path.isdir(ERRORS):
        return False
    uid, gid = _qidi()
    os.makedirs(STOCK, exist_ok=True)
    os.chown(STOCK, uid, gid)
    for code in CODES:
        stock = os.path.join(STOCK, code + ".json")
        if not os.path.exists(stock):
            shutil.copy2(os.path.join(ERRORS, code, "0.json"), stock)
        os.chown(os.path.join(ERRORS, code, "0.json"), uid, gid)
    os.makedirs(PICTURES, exist_ok=True)
    os.chown(PICTURES, uid, gid)
    if not os.path.exists(FLAG):
        open(FLAG, "w").close()
    os.chown(FLAG, uid, gid)
    for name, text in UNITS.items():
        with open(os.path.join(UNIT_DIR, name), "w") as f:
            f.write(text)
    run(["systemctl", "daemon-reload"], check=False)
    run(["systemctl", "enable", "--now", "qidi-screen-reload.path"], check=False)
    return True


def uninstall(run=subprocess.run):
    run(["systemctl", "disable", "--now", "qidi-screen-reload.path"], check=False)
    for name in UNITS:
        path = os.path.join(UNIT_DIR, name)
        if os.path.exists(path):
            os.remove(path)
    run(["systemctl", "daemon-reload"], check=False)
    restored = False
    for code in CODES:
        stock = os.path.join(STOCK, code + ".json")
        if os.path.exists(stock):
            shutil.copy2(stock, os.path.join(ERRORS, code, "0.json"))
            restored = True
    shutil.rmtree(PICTURES, ignore_errors=True)
    if restored:
        run(["systemctl", "restart", "qidi-client.service"], check=False)
    return restored
