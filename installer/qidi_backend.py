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
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
BOX_PATCH = ROOT / "box_patch"
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
# Each run has its own folder on the printer, so two runs never meet there.
REMOTE_DIR = "/tmp/qidi_setup_%d" % (os.getpid(),)
PATCHER_FILE = pathlib.Path(__file__).resolve().parent / "patcher.json"
# What the printer side needs besides the packages; everything a package needs it names itself.
SETUP_BUNDLE = ("box_patch/remote_setup.py",)


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


BACKUP_DIR = pathlib.Path.home() / "Library" / "Application Support" / "QIDI Q2 Patcher" / "backups"
BACKUPS_KEPT = 15


def list_backups(host):
    """Backups of this printer, newest first."""
    folder = BACKUP_DIR / host
    found = []
    for path in sorted(folder.glob("*.json"), reverse=True) if folder.exists() else ():
        if (folder / (path.stem + ".tar.gz")).exists():
            try:
                found.append(json.loads(path.read_text(encoding="utf-8")))
            except ValueError:
                pass
    return found


def import_backup(host, path):
    """Take a backup archive from anywhere, made earlier or moved away, into this printer's list.
    It gets a fresh name, so it is not pruned at once; its own time stays in the manifest."""
    import tarfile
    try:
        with tarfile.open(str(path), "r:gz") as archive:
            manifest = json.load(archive.extractfile(remote_setup.BACKUP_MANIFEST))
    except (OSError, KeyError, ValueError, tarfile.TarError):
        raise Failed("Это не резервная копия QIDI Q2 Patcher: %s" % (path,))
    if not isinstance(manifest.get("present"), list):
        raise Failed("В копии нет списка файлов: %s" % (path,))
    folder = BACKUP_DIR / host
    folder.mkdir(parents=True, exist_ok=True)
    name = time.strftime("%Y-%m-%d_%H-%M-%S") + "_imported"
    shutil.copy(str(path), str(folder / (name + ".tar.gz")))
    manifest.update(id=name, imported_from=str(path))
    (folder / (name + ".json")).write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def prune_backups(folder):
    """Only the newest BACKUPS_KEPT stay; the very first one, from before any patch, stays too."""
    names = sorted(p.stem for p in folder.glob("*.json"))
    for name in names[1:-BACKUPS_KEPT] if len(names) > BACKUPS_KEPT + 1 else ():
        for suffix in (".json", ".tar.gz"):
            path = folder / (name + suffix)
            if path.exists():
                path.unlink()


FIRMWARE_CACHE = pathlib.Path.home() / "Library" / "Caches" / "QIDI Q2 Patcher" / "firmware.json"
FIRMWARE_CACHE_SECONDS = 3600


def load_firmware_cache():
    try:
        return json.loads(FIRMWARE_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_firmware_cache(cache):
    try:
        FIRMWARE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        FIRMWARE_CACHE.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


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
        self.state = {}

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

    def config_file(self, name, default=""):
        try:
            return self.get("/server/files/config/" + urllib.parse.quote(name)).decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return default
            raise

    def installed(self):
        """What is on the printer: the modules, their versions and the firmware at install."""
        text = self.config_file("printer.cfg") + "\n" + self.config_file(remote_setup.OWN_CFG)
        self.state = json.loads(self.config_file(remote_setup.STATE_FILE, "{}"))
        return remote_setup.installed_features(text, self.state), remote_setup.installed_versions(self.state)

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

    def ssh_read(self, command, timeout=15):
        """Output of a command that only reads, run on the printer with the saved login."""
        with tempfile.TemporaryDirectory() as work:
            askpass = os.path.join(work, "askpass")
            with open(askpass, "w") as f:
                f.write('#!/bin/sh\nprintf "%s\\n" "$PRINTER_PASSWORD"\n')
            os.chmod(askpass, 0o700)
            done = subprocess.run(["ssh"] + SSH_OPTS + ["%s@%s" % (self.user, self.host), command],
                                  env=self._ssh_env(askpass), stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=timeout)
        if done.returncode != 0:
            raise Failed(done.stderr.strip() or "ssh exited with %d" % (done.returncode,))
        return done.stdout

    def live_firmware(self):
        """The firmware QIDI installed last; kept an hour per address, since it needs a login."""
        cache = load_firmware_cache()
        entry = cache.get(self.host)
        if entry and time.time() - entry["at"] < FIRMWARE_CACHE_SECONDS:
            return entry["firmware"]
        try:
            text = self.ssh_read("grep 'Add QD_' %s" % (remote_setup.FIRMWARE_FILE,))
        except (Failed, OSError, subprocess.TimeoutExpired) as e:
            note_error("firmware of " + self.host, e)
            return entry["firmware"] if entry else None
        found = re.findall(r"Add (QD_\S+)", text)
        firmware = found[-1] if found else None
        cache[self.host] = {"firmware": firmware, "at": time.time()}
        save_firmware_cache(cache)
        return firmware

    def on_printer(self, action, arguments, upload=None, fetch=None):
        """Copy the packages into one folder on the printer and run remote_setup.py `action`
        there as root. `upload` is a local file put next to them as upload.tar.gz; `fetch` is
        where to save the archive the action leaves as archive.tar.gz. Returns the output lines;
        Klipper is restarted when the action says it has something new to read."""
        info = self.json("/printer/info")
        login = "%s@%s" % (self.user, self.host)
        with tempfile.TemporaryDirectory() as work:
            askpass = os.path.join(work, "askpass")
            with open(askpass, "w") as f:
                f.write('#!/bin/sh\nprintf "%s\\n" "$PRINTER_PASSWORD"\n')
            os.chmod(askpass, 0o700)
            bundle = os.path.join(work, "bundle")
            for relative in bundle_files(load_packages()):
                path = os.path.join(bundle, relative)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                shutil.copy(str(ROOT / relative), path)
            shutil.copy(str(KEY.with_suffix(".pub")), os.path.join(bundle, "id_ed25519.pub"))
            if upload:
                shutil.copy(str(upload), os.path.join(bundle, "upload.tar.gz"))
            env = self._ssh_env(askpass)
            subprocess.run(["ssh"] + SSH_OPTS + [login, "rm -rf " + REMOTE_DIR], env=env, check=True,
                           stdin=subprocess.DEVNULL)
            subprocess.run(["scp", "-q", "-r"] + SSH_OPTS + [bundle, login + ":" + REMOTE_DIR], env=env, check=True)
            command = ["python3", REMOTE_DIR + "/box_patch/remote_setup.py", action, "--root", REMOTE_DIR,
                       "--klipper", info["klipper_path"], "--cfg", info["config_file"],
                       "--pubkey", REMOTE_DIR + "/id_ed25519.pub", "--user", self.user] + list(arguments)
            if upload:
                command += ["--archive", REMOTE_DIR + "/upload.tar.gz"]
            if fetch:
                command += ["--archive", REMOTE_DIR + "/archive.tar.gz"]
            # Klipper restarts only when the action says it has something new to read.
            inner = ("out=$(%s); rc=$?; printf '%%s\\n' \"$out\"; "
                     "if [ $rc -eq 0 ] && printf '%%s' \"$out\" | grep -qF %s; then systemctl restart klipper; fi; "
                     "chmod -R a+rX %s; exit $rc" % (shlex.join(command), shlex.quote(remote_setup.RESTART_MARK),
                                                     REMOTE_DIR))
            script = "chmod -R a+rX %s; sudo -S -p '' sh -c %s" % (REMOTE_DIR, shlex.quote(inner))
            done = subprocess.run(["ssh"] + SSH_OPTS + [login, script], env=env,
                                  input=self.password + "\n", text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                if done.returncode != 0:
                    for line in done.stdout.splitlines():
                        log(line)
                    raise Failed("Действие на принтере не удалось (код %d)." % (done.returncode,))
                if fetch:
                    subprocess.run(["scp", "-q"] + SSH_OPTS + [login + ":" + REMOTE_DIR + "/archive.tar.gz", str(fetch)],
                                   env=env, check=True)
            finally:
                subprocess.run(["ssh"] + SSH_OPTS + [login, "sudo -S -p '' rm -rf " + REMOTE_DIR], env=env,
                               input=self.password + "\n", text=True, capture_output=True)
            return done.stdout.splitlines()

    def remote(self, change, versions=None):
        """Install or remove packages; True when Klipper was restarted for them."""
        arguments = list(change)
        if versions:
            arguments += ["--versions", ",".join("%s=%s" % item for item in sorted(versions.items()))]
        lines = self.on_printer("install", arguments)
        for line in lines:
            if line != remote_setup.RESTART_MARK:
                log(line)
        return remote_setup.RESTART_MARK in lines

    def backup(self):
        """Download a backup of everything an install can change; returns its manifest."""
        folder = BACKUP_DIR / self.host
        folder.mkdir(parents=True, exist_ok=True)
        name = time.strftime("%Y-%m-%d_%H-%M-%S")
        archive = folder / (name + ".tar.gz")
        lines = self.on_printer("backup", [], fetch=archive)
        manifest = next(json.loads(line)["backup"] for line in lines if line.startswith('{"backup"'))
        manifest["id"] = name
        (folder / (name + ".json")).write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        prune_backups(folder)
        return manifest

    def restore(self, name):
        archive = BACKUP_DIR / self.host / (name + ".tar.gz")
        if not archive.exists():
            raise Failed("Нет такой резервной копии: " + name)
        for line in self.on_printer("restore", [], upload=archive):
            if line != remote_setup.RESTART_MARK:
                log(line)

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


def load_packages():
    return remote_setup.load_packages(str(ROOT))


def bundle_files(packages):
    """Project paths the printer side needs: the package folders and every file they name."""
    files = set(SETUP_BUNDLE)
    for package in packages.values():
        folder = pathlib.Path(package["_dir"])
        files.update(str(path.relative_to(ROOT)) for path in folder.rglob("*") if path.is_file())
        files.update(entry["from"] for entry in package.get("install", ())
                     if not entry.get("optional") or (ROOT / entry["from"]).exists())
        for key in ("setup", "service", "fluidd_script"):
            if package.get(key):
                files.add(package[key])
    return sorted(files)


def package_info(name, packages):
    """What the window shows about a package; all of it comes from the package's own folder."""
    package = packages[name]
    readme = pathlib.Path(package["_dir"]) / "README.md"
    needs = remote_setup.closure({name}, packages)
    info = {key: package.get(key) for key in ("id", "version", "icon", "title", "summary", "details", "note")}
    info.update(
        hidden=bool(package.get("hidden")),
        order=package.get("order", 99),
        adds=[entry["to"] for entry in package.get("install", ())],
        changes=package.get("changes", []),
        readme=readme.read_text(encoding="utf-8") if readme.exists() else "",
        needs=sorted(n for n in needs - {name} if not packages[n].get("hidden")),
        parts=sorted(packages[n]["title"] for n in needs - {name} if packages[n].get("hidden")),
        klipper=any(remote_setup.touches_klipper(packages[n]) for n in needs))
    return info


def load_catalog():
    """The patcher's own settings and every package as the packages describe themselves."""
    catalog = json.loads(PATCHER_FILE.read_text(encoding="utf-8"))
    packages = load_packages()
    catalog["modules"] = sorted((package_info(name, packages) for name in packages),
                                key=lambda m: (m["hidden"], m["order"], m["id"]))
    return catalog


class Change:
    """One step asked for: the whole set (--features), or packages to add or remove."""

    def __init__(self, features=None, add=(), remove=()):
        self.features = None if features is None else ",".join(sorted(features))
        self.add = ",".join(sorted(add)) or None
        self.remove = ",".join(sorted(remove)) or None

    def arguments(self):
        found = []
        for flag, value in (("--features", self.features), ("--add", self.add), ("--remove", self.remove)):
            if value is not None:
                found += [flag, value]
        return found

    def plan(self, installed, packages):
        """(packages on afterwards, packages copied now), as remote_setup will see it."""
        chosen = remote_setup.chosen_features(self, installed, packages)
        before = remote_setup.closure(installed.intersection(packages), packages)
        after = remote_setup.closure(chosen, packages)
        return chosen, after, remote_setup.to_copy(self, before, after, packages)


def versions_for(copied, packages, catalog):
    """The versions recorded on the printer for what is copied now, with the patcher's own."""
    versions = {"package": catalog["version"]}
    versions.update((name, packages[name]["version"]) for name in copied)
    return versions


def restore(printer, name):
    require_idle(printer)
    log("Возвращаю на принтер копию %s..." % (name,))
    printer.restore(name)
    log("Перезапускаю Klipper...")
    if not printer.restart_klipper():
        raise Failed("Копия возвращена, но Klipper не поднялся, проверь Fluidd.")
    return "Готово: принтер возвращён к копии %s." % (name,)


def apply(printer, change):
    require_idle(printer)
    previous, previous_versions = printer.installed()
    packages = load_packages()
    chosen, after, copied = change.plan(previous, packages)
    ensure_key()
    sync_alias(printer.host)
    log("Сохраняю резервную копию принтера...")
    saved = printer.backup()
    log("Копия %s сохранена на этом компьютере." % (saved["id"],))
    log("Устанавливаю на %s..." % (printer.host,))
    restarted = printer.remote(change.arguments(), versions_for(copied, packages, load_catalog()))
    if restarted:
        log("Перезапускаю Klipper...")
        if not printer.restart_klipper():
            log("Klipper не запустился, возвращаю прежнее состояние...")
            printer.remote(Change(features=previous).arguments(), previous_versions)
            if not printer.restart_klipper():
                raise Failed("Klipper не поднялся и после отката, проверь Fluidd.")
            raise Failed("Klipper не запустился с новыми настройками, прежнее состояние возвращено.")
    if not chosen:
        return "Всё удалено с принтера. Твои материалы и цвета остались на нём."
    if restarted and any(packages[n].get("klipper_section") for n in after) and not printer.diagnose():
        raise Failed("Установлено, но считыватели ответили не так, как ожидалось.")
    if any("panel" in packages[n].get("hooks", ()) for n in after) and not printer.panel_answers():
        return "Готово, но панель пока не отвечает на порту %d." % (PANEL_PORT,)
    return "Готово." + (" Klipper перезапущен и работает." if restarted else " Klipper не перезапускался.")


class DemoPrinter:
    """Shows the window without a printer; the choice lives in a temporary file."""
    host = "192.168.0.100"
    user = "mks"
    STATE = pathlib.Path(tempfile.gettempdir()) / "qidi_installer_demo_v2.json"

    def print_state(self):
        return "standby"

    def live_firmware(self):
        return "QD_Q2_01.01.03.02"

    def installed(self):
        """At first an older package, so the window shows what an update looks like."""
        if self.STATE.exists():
            saved = json.loads(self.STATE.read_text())
            return set(saved["features"]), saved["versions"]
        return {"bambu", "usage"}, {"package": "0.9.0", "bambu": "0.9.0", "usage": "1.0.0"}

    def apply(self, change):
        installed, versions = self.installed()
        packages = load_packages()
        chosen, after, copied = change.plan(installed, packages)
        for text in ("Устанавливаю на принтер...", "Version=0x92 на обоих считывателях"):
            log(text)
            time.sleep(0.7)
        versions = {name: value for name, value in versions.items() if name == "package" or name in after}
        versions.update(versions_for(copied, packages, load_catalog()))
        self.STATE.write_text(json.dumps({"features": sorted(chosen), "versions": versions}))
        return "Готово (демо): на принтере ничего не менялось."


def status(printer):
    state = printer.print_state() if printer else "unavailable"
    features, versions, firmware = set(), {}, None
    if printer and state != "unavailable":
        try:
            features, versions = printer.installed()
        except (OSError, ValueError):
            state = "unavailable"
    if printer and state != "unavailable":
        firmware = printer.live_firmware()
    catalog = load_catalog()
    custom_login = printer is not None and (getattr(printer, "user", DEFAULT_USER), getattr(
        printer, "password", DEFAULT_PASSWORD)) != (DEFAULT_USER, DEFAULT_PASSWORD)
    send("status", state=state, host=printer.host if printer else "", user=getattr(printer, "user", ""),
         custom_login=custom_login, configured=printer is not None,
         network_blocked=getattr(printer, "blocked", False),
         firmware=firmware or (getattr(printer, "state", None) or {}).get("firmware"), features=sorted(features),
         installed_versions=versions, **catalog)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("command", choices=("status", "apply", "diag", "save-connection", "discover",
                                                "backup", "backups", "restore", "import-backup"))
    parser.add_argument("--features", help="the whole set of packages")
    parser.add_argument("--add", default="", help="packages to install or update")
    parser.add_argument("--remove", default="", help="packages to remove")
    parser.add_argument("--backup", help="the backup to restore")
    parser.add_argument("--file", help="a backup archive to take into the list")
    args = parser.parse_args(argv)
    names = lambda value: {name for name in value.split(",") if name}
    change = Change(None if args.features is None else names(args.features), names(args.add), names(args.remove))

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
    if args.command == "backups":
        send("backups", items=list_backups(printer.host) if printer else [])
        return 0
    try:
        if printer is None:
            raise Failed("Сначала укажи принтер.")
        if args.demo:
            text = printer.apply(change) if args.command == "apply" else "Демо: на принтере ничего не менялось."
        elif args.command == "apply":
            text = apply(printer, change)
        elif args.command == "backup":
            require_idle(printer)
            text = "Копия %s сохранена на этом компьютере." % (printer.backup()["id"],)
        elif args.command == "restore":
            text = restore(printer, args.backup)
        elif args.command == "import-backup":
            text = "Копия от %s добавлена в список." % (import_backup(printer.host, args.file).get("time", "?"),)
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
