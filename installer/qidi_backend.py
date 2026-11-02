#!/usr/bin/env python3
"""What the installer window runs. Every output line is one JSON message for the window.
Printer address and login come only from .env in the project folder."""
import argparse
import errno
import concurrent.futures
import json
import os
import pathlib
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
BOX_PATCH = ROOT / "box_patch"
TAG_WRITER = ROOT / "tag_writer"
sys.path.insert(0, str(BOX_PATCH))
import remote_setup  # noqa: E402
import sync_ssh_alias  # noqa: E402

ENV_PATH = ROOT / ".env"
KEY = pathlib.Path.home() / ".ssh" / "qidi" / "id_ed25519"
SSH_OPTS = ["-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
PANEL_PORT = 18770
# The login QIDI ships the Q2 with; .env holds another one only when it was changed.
DEFAULT_USER = "mks"
DEFAULT_PASSWORD = "makerbase"
REMOTE_DIR = "/tmp/qidi_setup"
# remote_setup.py names what goes on the printer, so a new file is listed in one place.
BUNDLE = [BOX_PATCH / name for name in ("remote_setup.py", "bambu_map.cfg", "qidi_panel_link.js", "qidi-panel.service")
          + remote_setup.MODULE_FILES + remote_setup.SETUP_FILES]
BUNDLE += [path for path in (TAG_WRITER / name for name in remote_setup.PANEL_FILES)
           if path.name not in remote_setup.MODULE_FILES]
MODULES_FILE = pathlib.Path(__file__).resolve().parent / "modules.json"
OPTIONAL_BUNDLE = [TAG_WRITER / "my_materials.json", TAG_WRITER / "my_colors.json"]


LOG_FILE = pathlib.Path.home() / "Library" / "Logs" / "QIDI Q2 Patcher.log"


def note_error(what, error):
    """Why the printer did not answer, for the log the window points to."""
    try:
        with open(str(LOG_FILE), "a", encoding="utf-8") as f:
            f.write("%s %s: %r\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), what, error))
    except OSError:
        pass


def blocked_by_macos(error):
    """macOS answers "no route" for every local address while the Local Network permission is off."""
    reason = getattr(error, "reason", error)
    return isinstance(reason, OSError) and reason.errno == errno.EHOSTUNREACH


def send(kind, **fields):
    print(json.dumps(dict(fields, type=kind), ensure_ascii=False), flush=True)


def log(text):
    send("log", text=text)


class Failed(Exception):
    pass


def read_env(path=ENV_PATH):
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Z_]+)=(.*)$", line)
        if match:
            parts = shlex.split(match.group(2), comments=True)
            values[match.group(1)] = parts[0] if parts else ""
    return values


def write_env(connection, path=ENV_PATH):
    """Empty fields keep what is saved, so changing only the address needs no retyping;
    `default_login` goes back to the login QIDI ships."""
    saved = read_env(path)
    connection = dict(connection)
    if connection.get("default_login"):
        connection["user"], connection["password"] = DEFAULT_USER, DEFAULT_PASSWORD
    if not connection.get("host"):
        connection["host"] = saved.get("PRINTER_HOST", "")
    if not connection.get("password"):
        connection["password"] = saved.get("PRINTER_PASSWORD") or DEFAULT_PASSWORD
    if not connection.get("user"):
        connection["user"] = saved.get("PRINTER_USER") or DEFAULT_USER
    # .env is also sourced by setup.sh, so the values are quoted for the shell.
    text = "".join("%s=%s\n" % (name, shlex.quote(connection[key])) for name, key in (
        ("PRINTER_HOST", "host"), ("PRINTER_USER", "user"), ("PRINTER_PASSWORD", "password")))
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(str(path), 0o600)


def local_address():
    # A UDP socket sends nothing on connect; it only picks the interface toward the router.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]


def probe(host):
    """A Klipper printer answers on Moonraker's port; None for anything else."""
    try:
        socket.create_connection((host, 7125), timeout=0.5).close()
        with urllib.request.urlopen("http://%s:7125/printer/info" % (host,), timeout=3) as answer:
            info = json.loads(answer.read())["result"]
    except (OSError, ValueError, KeyError):
        return None
    return {"host": host, "name": info.get("hostname") or host}


def discover():
    """Printers in the same /24 network as this computer."""
    prefix = local_address().rsplit(".", 1)[0]
    hosts = ["%s.%d" % (prefix, n) for n in range(1, 255)]
    with concurrent.futures.ThreadPoolExecutor(64) as pool:
        return [found for found in pool.map(probe, hosts) if found]


def diag_ok(text):
    """Both chips answered with a real version number."""
    versions = re.findall(r"Version=0x([0-9A-Fa-f]{2})", text)
    return len([v for v in versions if v.upper() not in ("00", "FF")]) == 2


class Printer:
    def __init__(self, env):
        self.host = env["PRINTER_HOST"]
        self.user = env.get("PRINTER_USER") or DEFAULT_USER
        self.password = env.get("PRINTER_PASSWORD") or DEFAULT_PASSWORD
        self.api = "http://%s:7125" % (self.host,)
        self.blocked = False

    def get(self, path, timeout=8):
        with urllib.request.urlopen(self.api + path, timeout=timeout) as answer:
            return answer.read()

    def post(self, path, timeout=10):
        request = urllib.request.Request(self.api + path, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return answer.read()

    def json(self, path, timeout=8):
        return json.loads(self.get(path, timeout))["result"]

    def print_state(self):
        """printing, paused, standby, ...; unavailable when the printer does not answer."""
        try:
            return self.json("/printer/objects/query?print_stats=state")["status"]["print_stats"]["state"]
        except (OSError, ValueError, KeyError) as e:
            note_error("state of " + self.host, e)
            self.blocked = blocked_by_macos(e)
            return "unavailable"

    def klipper_state(self):
        try:
            return self.json("/printer/info", 5)["state"]
        except (OSError, ValueError, KeyError):
            return "unavailable"

    def installed(self):
        """What is on the printer: the modules and the versions they were installed with."""
        text = self.get("/server/files/config/printer.cfg").decode("utf-8")
        return remote_setup.installed_features(text), remote_setup.installed_versions(text)

    def run_gcode(self, script):
        query = urllib.parse.urlencode({"script": script})
        self.post("/printer/gcode/script?" + query, 75)

    def responses_since(self, since):
        store = self.json("/server/gcode_store?count=20")["gcode_store"]
        return [item["message"] for item in store if item["time"] >= since - 2 and item["type"] == "response"]

    def wait_ready(self):
        # An error state can be stale right after a restart, so it must persist to count.
        errors = 0
        for _ in range(45):
            state = self.klipper_state()
            if state == "ready":
                return True
            if state in ("error", "shutdown"):
                errors += 1
                if errors >= 8:
                    return False
            time.sleep(2)
        return False

    def restart_klipper(self):
        time.sleep(5)
        if self.wait_ready():
            return True
        try:
            self.post("/printer/firmware_restart")
        except OSError:
            pass
        time.sleep(8)
        return self.wait_ready()

    def _ssh_env(self, askpass):
        return dict(os.environ, SSH_ASKPASS=askpass, SSH_ASKPASS_REQUIRE="force", PRINTER_PASSWORD=self.password)

    def remote(self, action, features=None, versions=None):
        """Copy everything into one folder on the printer and run remote_setup.py there."""
        info = self.json("/printer/info")
        target = "%s@%s" % (self.user, self.host)
        with tempfile.TemporaryDirectory() as work:
            askpass = os.path.join(work, "askpass")
            with open(askpass, "w") as f:
                f.write('#!/bin/sh\nprintf "%s\\n" "$PRINTER_PASSWORD"\n')
            os.chmod(askpass, 0o700)
            bundle = os.path.join(work, "bundle")
            os.mkdir(bundle)
            for path in BUNDLE + [KEY.with_suffix(".pub")]:
                shutil.copy(str(path), bundle)
            for path in OPTIONAL_BUNDLE:
                if path.exists():
                    shutil.copy(str(path), bundle)
            env = self._ssh_env(askpass)
            subprocess.run(["ssh"] + SSH_OPTS + [target, "rm -rf " + REMOTE_DIR], env=env, check=True,
                           stdin=subprocess.DEVNULL)
            subprocess.run(["scp", "-q", "-r"] + SSH_OPTS + [bundle, target + ":" + REMOTE_DIR], env=env, check=True)
            arguments = ["python3", REMOTE_DIR + "/remote_setup.py", action,
                         "--klipper", info["klipper_path"], "--cfg", info["config_file"],
                         "--module", REMOTE_DIR + "/bambu_rfid.py", "--map", REMOTE_DIR + "/bambu_map.cfg",
                         "--pubkey", REMOTE_DIR + "/id_ed25519.pub", "--panel", REMOTE_DIR, "--user", self.user]
            if features is not None:
                arguments += ["--features", ",".join(sorted(features))]
            if versions:
                arguments += ["--versions", ",".join("%s=%s" % item for item in sorted(versions.items()))]
            inner = shlex.join(arguments) + " && systemctl restart klipper"
            command = "chmod -R a+rX %s; sudo -S -p '' sh -c %s; rc=$?; rm -rf %s; exit $rc" % (
                REMOTE_DIR, shlex.quote(inner), REMOTE_DIR)
            done = subprocess.run(["ssh"] + SSH_OPTS + [target, command], env=env,
                                  input=self.password + "\n", text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for line in done.stdout.splitlines():
                log(line)
            if done.returncode != 0:
                raise Failed("Установка на принтере не удалась (код %d)." % (done.returncode,))

    def diagnose(self):
        since = time.time()
        for reader in ("card_reader_1", "card_reader_2"):
            try:
                self.run_gcode("BAMBU_RFID_DIAG READER=" + reader)
            except OSError as e:
                log("%s: нет ответа (%s)" % (reader, e))
        time.sleep(1)
        lines = self.responses_since(since)
        for line in lines:
            log(line)
        return diag_ok("\n".join(lines))

    def panel_answers(self):
        try:
            urllib.request.urlopen("http://%s:%d/" % (self.host, PANEL_PORT), timeout=10).close()
            return True
        except OSError:
            return False


def ensure_key():
    if not KEY.exists():
        KEY.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "mac-to-qidi-q2", "-f", str(KEY)],
                       check=True)


def sync_alias(host):
    config = pathlib.Path.home() / ".ssh" / "config"
    if config.exists():
        text = config.read_text(encoding="utf-8")
        updated = sync_ssh_alias.sync(text, host)
        if updated != text:
            config.write_text(updated, encoding="utf-8")


def require_idle(printer):
    state = printer.print_state()
    if state in ("printing", "paused"):
        raise Failed("Принтер сейчас печатает. Запусти после печати.")
    if state == "unavailable":
        raise Failed("Принтер не отвечает, проверь адрес и сеть.")


def load_catalog():
    return json.loads(MODULES_FILE.read_text(encoding="utf-8"))


def versions_for(features, catalog):
    """The versions recorded on the printer: the package and each chosen module."""
    versions = {"package": catalog["version"]}
    versions.update((m["id"], m["version"]) for m in catalog["modules"] if m["id"] in features)
    return versions


def apply(printer, features):
    require_idle(printer)
    previous, previous_versions = printer.installed()
    ensure_key()
    sync_alias(printer.host)
    log("Устанавливаю на %s..." % (printer.host,))
    printer.remote("install", features, versions_for(features, load_catalog()))
    log("Перезапускаю Klipper...")
    if not printer.restart_klipper():
        log("Klipper не запустился, возвращаю прежнее состояние...")
        printer.remote("install", previous, previous_versions)
        if not printer.restart_klipper():
            raise Failed("Klipper не поднялся и после отката, проверь Fluidd.")
        raise Failed("Klipper не запустился с новыми настройками, прежнее состояние возвращено.")
    if not features:
        return "Всё удалено с принтера. Твои материалы и цвета остались на нём."
    if not features.intersection(remote_setup.MODULE_FEATURES):
        return "Готово: установлено, Klipper работает."
    if not printer.diagnose():
        raise Failed("Установлено, но считыватели ответили не так, как ожидалось.")
    if features.intersection(remote_setup.PANEL_FEATURES) and not printer.panel_answers():
        return "Установлено, но панель пока не отвечает на порту %d." % (PANEL_PORT,)
    return "Готово: установлено, Klipper работает, оба считывателя отвечают."


class DemoPrinter:
    """Shows the window without a printer; the choice lives in a temporary file."""
    host = "192.168.0.100"
    user = "mks"
    STATE = pathlib.Path(tempfile.gettempdir()) / "qidi_installer_demo_v2.json"

    def print_state(self):
        return "standby"

    def installed(self):
        """At first an older package, so the window shows what an update looks like."""
        if self.STATE.exists():
            saved = json.loads(self.STATE.read_text())
            return set(saved["features"]), saved["versions"]
        return {"bambu", "usage"}, {"package": "0.9.0", "bambu": "0.9.0", "usage": "1.0.0"}

    def apply(self, features):
        for text in ("Устанавливаю на принтер...", "Перезапускаю Klipper...", "Version=0x92 на обоих считывателях"):
            log(text)
            time.sleep(0.7)
        self.STATE.write_text(json.dumps({"features": sorted(features),
                                          "versions": versions_for(features, load_catalog())}))
        return "Готово (демо): на принтере ничего не менялось."


def status(printer):
    state = printer.print_state() if printer else "unavailable"
    features, versions = set(), {}
    if printer and state != "unavailable":
        try:
            features, versions = printer.installed()
        except (OSError, ValueError):
            state = "unavailable"
    catalog = load_catalog()
    custom_login = printer is not None and (getattr(printer, "user", DEFAULT_USER), getattr(
        printer, "password", DEFAULT_PASSWORD)) != (DEFAULT_USER, DEFAULT_PASSWORD)
    send("status", state=state, host=printer.host if printer else "", user=getattr(printer, "user", ""),
         custom_login=custom_login, configured=printer is not None,
         network_blocked=getattr(printer, "blocked", False), features=sorted(features),
         installed_versions=versions, **catalog)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("command", choices=("status", "apply", "diag", "save-connection", "discover"))
    parser.add_argument("--features", default="")
    args = parser.parse_args(argv)
    chosen = {name for name in args.features.split(",") if name}

    if args.command == "save-connection":
        if not args.demo:
            write_env(json.load(sys.stdin))
        send("done", ok=True, text="Подключение сохранено.")
        return 0
    if args.command == "discover":
        try:
            found = [{"host": "192.168.0.100", "name": "qidi-q2"}] if args.demo else discover()
        except OSError as e:
            note_error("search", e)
            found = []
        send("printers", items=found)
        return 0
    if args.demo:
        printer = DemoPrinter()
    else:
        env = read_env()
        printer = Printer(env) if env.get("PRINTER_HOST") else None
    if args.command == "status":
        status(printer)
        return 0
    try:
        if printer is None:
            raise Failed("Сначала укажи принтер.")
        if args.demo:
            text = printer.apply(chosen) if args.command == "apply" else "Демо: считыватели не опрашивались."
        elif args.command == "apply":
            text = apply(printer, chosen)
        else:
            require_idle(printer)
            if not printer.diagnose():
                raise Failed("Считыватели ответили не так, как ожидалось.")
            text = "Оба считывателя отвечают."
    except (Failed, OSError, subprocess.CalledProcessError) as e:
        send("done", ok=False, text=str(e))
        return 1
    send("done", ok=True, text=text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
