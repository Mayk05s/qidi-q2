#!/usr/bin/env python3
"""The installer window for Windows, in tkinter; the Mac has its own in QidiInstaller.swift and both
show the same. All the work happens in qidi_backend.py, run here in threads of this program, since
the Windows program carries no separate Python to start it with. `--demo` shows it without a printer."""
import os
import pathlib
import queue
import re
import subprocess
import sys
import threading
import traceback
import webbrowser

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import qidi_backend as backend  # noqa: E402

PANEL_PORT = backend.PANEL_PORT
# Buttons that make sense only with these packages on the printer.
DIAG_NEEDS = {"bambu", "nfc", "spools", "usage"}
PANEL_NEEDS = {"bambu", "nfc", "usage", "guide"}


class State:
    """What the window knows and decides, without any widgets, so it can be tested."""

    def __init__(self):
        self.all_packages = []
        self.risks = []
        self.package_version = ""
        self.tested_firmware = []
        self.firmware = None
        self.common_adds, self.common_changes = [], []
        self.installed_versions = {}
        self.state = "unknown"
        self.host = ""
        self.user = ""
        self.configured = True
        self.custom_login = False
        self.installed = set()
        self.chosen = set()
        self.found = []
        self.backups = []
        self.searching = False
        self.log_lines = []
        self.busy = False
        self.outcome = None

    @property
    def modules(self):
        return [m for m in self.all_packages if not m.get("hidden")]

    def module(self, name):
        return next((m for m in self.all_packages if m["id"] == name), None)

    def title(self, name):
        found = self.module(name)
        return found["title"] if found else name

    def receive(self, message):
        kind = message.get("type")
        if kind == "status":
            self.all_packages = message.get("modules") or []
            self.risks = message.get("risks") or []
            self.package_version = message.get("version") or ""
            self.tested_firmware = message.get("tested_firmware") or []
            self.firmware = message.get("firmware")
            common = message.get("common") or {}
            self.common_adds, self.common_changes = common.get("adds") or [], common.get("changes") or []
            self.installed_versions = message.get("installed_versions") or {}
            self.state = message.get("state") or "unknown"
            self.host = message.get("host") or ""
            self.user = message.get("user") or ""
            self.configured = bool(message.get("configured"))
            self.custom_login = bool(message.get("custom_login"))
            self.installed = set(message.get("features") or [])
            self.chosen = set(self.installed)
        elif kind == "backups":
            self.backups = message.get("items") or []
        elif kind == "printers":
            self.found = [item for item in message.get("items") or [] if item.get("host")]
        elif kind == "log":
            self.log_lines.append(message.get("text", ""))
        elif kind == "done":
            self.outcome = (bool(message.get("ok")), message.get("text", ""))

    @property
    def can_change(self):
        return not self.busy and self.state in ("ready", "standby")

    @property
    def has_changes(self):
        return self.chosen != self.installed

    @property
    def printer_package(self):
        return self.installed_versions.get("package")

    def is_old(self, name):
        found = self.module(name)
        return found is not None and self.installed_versions.get(name) != found.get("version")

    @property
    def outdated(self):
        """Installed modules older on the printer than here; their hidden parts count for them."""
        return {m["id"] for m in self.modules if m["id"] in self.installed
                and (self.is_old(m["id"]) or any(self.is_old(p) for p in m.get("part_ids") or ()))}

    def old_parts(self, module):
        if module["id"] not in self.installed:
            return []
        return [self.title(p) for p in module.get("part_ids") or () if self.is_old(p)]

    @property
    def is_reinstall(self):
        return bool(self.installed) and not self.has_changes and not self.outdated

    @property
    def action_title(self):
        if not self.installed:
            return "Установить"
        if self.has_changes:
            return "Применить"
        return "Переустановить" if self.is_reinstall else "Обновить"

    @property
    def can_run_action(self):
        return self.can_change and (bool(self.chosen) or self.has_changes)

    def set_chosen(self, name, on):
        """Switching a package on brings the ones it needs; switching it off takes the ones that need it."""
        if on:
            self.chosen.add(name)
            self.chosen.update((self.module(name) or {}).get("needs") or ())
        else:
            self.chosen.discard(name)
            for m in self.modules:
                if name in (m.get("needs") or ()):
                    self.chosen.discard(m["id"])

    @property
    def restarts_klipper(self):
        touched = (self.chosen ^ self.installed) | (self.outdated & self.chosen)
        return any(m["id"] in touched and m.get("klipper") for m in self.modules)

    @property
    def changes(self):
        """What the confirmation asks about, module by module."""
        def names(ids):
            return "\n".join("• " + m["title"] for m in self.modules if m["id"] in ids)
        parts = []
        added, removed = self.chosen - self.installed, self.installed - self.chosen
        updated = self.outdated & self.chosen
        if added:
            parts.append("Установить:\n" + names(added))
        if updated:
            parts.append("Обновить:\n" + names(updated))
        if removed:
            parts.append("Убрать:\n" + names(removed))
        if self.is_reinstall:
            parts.append("Выбранные пакеты будут переустановлены:\n" + names(self.chosen))
            restarts = any(m["id"] in self.chosen and m.get("klipper") for m in self.modules)
        else:
            restarts = self.restarts_klipper
        parts.append("Klipper перезапустится." if restarts else "Klipper не перезапускается.")
        parts.append("Перед установкой резервная копия принтера сохранится на этом компьютере.")
        return "\n\n".join(parts)

    def apply_arguments(self):
        """All the chosen changes in one run; packages that stay as they are are not touched."""
        add = self.chosen if self.is_reinstall else (self.chosen - self.installed) | (self.outdated & self.chosen)
        remove = self.installed - self.chosen
        return ["apply", "--add", ",".join(sorted(add)), "--remove", ",".join(sorted(remove))]

    @property
    def firmware_line(self):
        """"QIDI Q2 · прошивка 01.01.03.02" from QIDI's "QD_Q2_01.01.03.02"."""
        if not self.firmware:
            return "QIDI, прошивка неизвестна"
        parts = self.firmware.split("_")
        if len(parts) >= 3 and parts[0] == "QD":
            return "QIDI %s · прошивка %s" % (parts[1], "_".join(parts[2:]))
        return "прошивка " + self.firmware

    @property
    def printers(self):
        """Found printers plus the chosen one when the search did not see it."""
        found = list(self.found)
        if self.host and not any(p["host"] == self.host for p in found):
            found.insert(0, {"host": self.host, "name": ""})
        return found


def plain(text):
    """Markdown inline marks away: **bold**, `code`, [link](url)."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text.replace("**", "").replace("`", "")


# ---------------------------------------------------------------- the window

GREEN, BLUE, RED, ORANGE, GRAY = "#1a7f37", "#0969da", "#cf222e", "#bc4c00", "#6e7781"
TINTS = {GREEN: "#e6f6ea", BLUE: "#e7f1fd", RED: "#fdecee", ORANGE: "#fdf0e4", GRAY: "#f4f5f7"}
SIDEBAR = "#eef0f3"
TEXT = "#1f2328"
TEXT_GRAY = "#57606a"


def open_folder(path):
    path = str(path)
    os.makedirs(path, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", path])


class Window:
    def __init__(self, demo):
        import tkinter as tk
        from tkinter import font as tkfont, ttk
        self.tk, self.ttk = tk, ttk
        self.demo = demo
        self.model = State()
        self.events = queue.Queue()
        self.expanded = set()
        self.risks_folded = False
        self.backups_window = None
        self.root = tk.Tk()
        self.root.title("QIDI Q2 Patcher")
        self.root.geometry("960x780")
        self.root.minsize(860, 600)
        # The window is light whatever the system theme, so the text is dark everywhere.
        for option in ("*Label.foreground", "*Text.foreground", "*Text.insertBackground"):
            self.root.option_add(option, TEXT)
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        base = tkfont.nametofont("TkDefaultFont")
        if sys.platform == "win32":
            base.configure(family="Segoe UI", size=10)
        family, size = base.actual("family"), base.actual("size")
        self.font = (family, size)
        self.small = (family, max(size - 1, 8))
        self.bold = (family, size, "bold")
        self.head = (family, size + 1, "bold")
        self.big = (family, size + 6, "bold")
        self.mono = ("Consolas" if sys.platform == "win32" else "Menlo", max(size - 1, 8))
        style.configure("Accent.TButton", font=self.bold)
        self.build()
        self.root.after(60, self.pump)
        self.refresh()
        self.discover()

    # -------------------------------------------------------------- running commands

    def run(self, arguments, stdin=None, exclusive=True, fresh=False):
        """An exclusive command waits for no other; the printer search runs beside the rest."""
        if exclusive:
            if self.model.busy:
                return
            self.model.busy = True
        if fresh:
            self.model.log_lines, self.model.outcome = [], None
        command = arguments[0]
        argv = (["--demo"] if self.demo else []) + list(arguments)

        def work():
            backend.receiver.send = lambda message: self.events.put(("message", message))
            try:
                code = backend.main(argv, stdin)
            except Exception as e:  # the window must learn about any failure, not hang busy
                backend.note_error(command, traceback.format_exc())
                self.events.put(("message", {"type": "done", "ok": False, "text": "Ошибка: %s" % (e,)}))
                code = 1
            self.events.put(("end", (command, exclusive, code)))

        threading.Thread(target=work, daemon=True).start()
        self.render()

    def pump(self):
        changed = False
        try:
            while True:
                kind, payload = self.events.get_nowait()
                changed = True
                if kind == "message":
                    self.model.receive(payload)
                else:
                    self.finished(*payload)
        except queue.Empty:
            pass
        if changed:
            self.render()
        self.root.after(60, self.pump)

    def finished(self, command, exclusive, code):
        if not exclusive:
            if command == "discover":
                self.model.searching = False
            return
        self.model.busy = False
        if code != 0 and self.model.outcome is None and command != "status":
            self.model.outcome = (False, "Действие остановилось с ошибкой, подробности в журнале.")
        if command != "status":
            self.refresh()
            self.load_backups()

    def refresh(self):
        self.run(["status"])

    def discover(self):
        if self.model.searching:
            return
        self.model.searching = True
        self.run(["discover"], exclusive=False)

    def load_backups(self):
        self.run(["backups"], exclusive=False)

    def save_connection(self, connection):
        import io
        import json
        self.run(["save-connection"], stdin=io.StringIO(json.dumps(connection)), fresh=True)

    def action(self, *arguments):
        self.run(list(arguments), fresh=True)

    # -------------------------------------------------------------- layout

    def build(self):
        tk, ttk = self.tk, self.ttk
        self.root.configure(bg="white")
        self.sidebar = tk.Frame(self.root, bg=SIDEBAR, width=300)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        main = tk.Frame(self.root, bg="white", padx=18, pady=14)
        main.pack(side="left", fill="both", expand=True)

        top = tk.Frame(self.sidebar, bg=SIDEBAR, padx=16, pady=14)
        top.pack(fill="x")
        tk.Label(top, text="QIDI Q2 Patcher", font=self.big, bg=SIDEBAR).pack(anchor="w")
        tk.Label(top, text="для QIDI Q2 и QIDI Box", font=self.small, fg=TEXT_GRAY, bg=SIDEBAR).pack(anchor="w")
        row = tk.Frame(top, bg=SIDEBAR)
        row.pack(anchor="w", pady=(4, 0))
        self.dot = tk.Canvas(row, width=10, height=10, bg=SIDEBAR, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 6))
        self.state_label = tk.Label(row, font=self.font, fg=TEXT_GRAY, bg=SIDEBAR)
        self.state_label.pack(side="left")
        if self.demo:
            tk.Label(row, text="ДЕМО", font=self.bold, fg=ORANGE, bg=SIDEBAR).pack(side="left", padx=6)

        box = tk.Frame(self.sidebar, bg="white", padx=12, pady=10)
        box.pack(fill="x", padx=12)
        tk.Label(box, text="ПРИНТЕРЫ", font=self.small + ("bold",), fg=TEXT_GRAY, bg="white").pack(anchor="w")
        search = tk.Frame(box, bg="white")
        search.pack(fill="x", pady=(4, 4))
        self.search_label = tk.Label(search, font=self.font, fg=TEXT_GRAY, bg="white")
        self.search_label.pack(side="left")
        self.search_button = ttk.Button(search, text="↻", width=3, command=lambda: (self.discover(), self.refresh()))
        self.search_button.pack(side="right")
        self.printer_list = tk.Frame(box, bg="white")
        self.printer_list.pack(fill="x")
        ttk.Button(box, text="+ Добавить принтер", command=lambda: self.edit_printer(True)).pack(anchor="w", pady=(6, 0))

        self.risks_box = tk.Frame(self.sidebar, bg=TINTS[ORANGE], padx=12, pady=10)
        self.risks_box.pack(side="bottom", fill="x", padx=12, pady=12)

        header = tk.Frame(main, bg="white")
        header.pack(fill="x")
        tk.Label(header, text="Модули", font=self.big, bg="white").pack(side="left")
        self.version_button = ttk.Button(header, command=self.show_versions)
        self.version_button.pack(side="right")
        ttk.Button(header, text="?", width=3, command=lambda: self.show_help(None)).pack(side="right", padx=4)
        ttk.Button(header, text="⟲ Восстановить", command=self.show_backups).pack(side="right")
        self.update_banner = tk.Label(main, font=self.font, fg=ORANGE, bg=TINTS[ORANGE], anchor="w",
                                      justify="left", padx=10, pady=8, wraplength=560)

        # The cards scroll; the bottom of the window stays where it is.
        self.bottom = tk.Frame(main, bg="white")
        self.bottom.pack(side="bottom", fill="x")
        holder = tk.Frame(main, bg="white")
        holder.pack(fill="both", expand=True, pady=(10, 6))
        self.canvas = tk.Canvas(holder, bg="white", highlightthickness=0)
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.cards = tk.Frame(self.canvas, bg="white")
        window = self.canvas.create_window((0, 0), window=self.cards, anchor="nw")
        self.cards.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        self.root.bind_all("<MouseWheel>", self.wheel)
        self.update_banner_holder = holder

        self.outcome_label = tk.Label(self.bottom, font=self.font, bg="white", anchor="w", justify="left",
                                      wraplength=600)
        self.log = tk.Text(self.bottom, height=6, font=self.mono, bg=TINTS[GRAY], relief="flat", padx=8, pady=6,
                           wrap="word")
        self.reinstall_hint = tk.Label(self.bottom, text="Выбранные пакеты будут переустановлены", font=self.small,
                                       fg=TEXT_GRAY, bg="white")
        buttons = tk.Frame(self.bottom, bg="white")
        buttons.pack(side="bottom", fill="x", pady=(6, 0))
        self.diag_button = ttk.Button(buttons, text="Диагностика", command=lambda: self.action("diag"))
        self.diag_button.pack(side="left")
        self.panel_button = ttk.Button(buttons, text="Открыть панель", command=self.open_panel)
        self.panel_button.pack(side="left", padx=6)
        self.remove_button = ttk.Button(buttons, text="Удалить всё", command=self.confirm_remove)
        self.remove_button.pack(side="left")
        self.action_button = ttk.Button(buttons, style="Accent.TButton", command=self.confirm_apply)
        self.action_button.pack(side="right")
        self.progress = ttk.Progressbar(buttons, mode="indeterminate", length=80)

    def wheel(self, event):
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        while widget is not None and widget is not self.canvas:
            widget = widget.master
        if widget is self.canvas:
            step = -1 * (event.delta // 120 if abs(event.delta) >= 120 else event.delta)
            self.canvas.yview_scroll(step, "units")

    # -------------------------------------------------------------- drawing

    def render(self):
        m, tk = self.model, self.tk
        color, text = {"printing": (ORANGE, "Идёт печать, менять нельзя"), "paused": (ORANGE, "Идёт печать, менять нельзя"),
                       "ready": (GREEN, "Подключён"), "standby": (GREEN, "Подключён"),
                       "unavailable": (RED, "Нет связи" if m.configured else "Принтер не выбран")}.get(
            m.state, (GRAY, "Проверяю..."))
        self.dot.delete("all")
        self.dot.create_oval(1, 1, 9, 9, fill=color, outline=color)
        self.state_label.configure(text=text)
        self.search_label.configure(text="Ищу в сети..." if m.searching else
                                    ("В сети не найдено" if not m.found else "Найдены в сети"))
        self.search_button.state(["disabled"] if m.searching else ["!disabled"])
        self.render_printers()
        self.render_risks()

        self.version_button.configure(text="v" + m.package_version)
        outdated = m.outdated
        if outdated:
            self.update_banner.configure(text="⬇ Можно обновить: " + ", ".join(
                x["title"] for x in m.modules if x["id"] in outdated) + ".")
            self.update_banner.pack(fill="x", pady=(10, 0), before=self.update_banner_holder)
        else:
            self.update_banner.pack_forget()
        self.render_cards()

        for widget in (self.outcome_label, self.log, self.reinstall_hint):
            widget.pack_forget()
        if m.outcome:
            ok, message = m.outcome
            self.outcome_label.configure(text=("✔ " if ok else "✖ ") + message, fg=GREEN if ok else RED)
            self.outcome_label.pack(fill="x", pady=(0, 4))
        if m.log_lines:
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.insert("end", "\n".join(m.log_lines))
            self.log.see("end")
            self.log.configure(state="disabled")
            self.log.pack(fill="x", pady=(0, 4))
        if m.is_reinstall and not m.busy:
            self.reinstall_hint.pack(anchor="e")

        self.enable(self.diag_button, m.can_change and bool(m.installed & DIAG_NEEDS))
        self.enable(self.panel_button, bool(m.host) and bool(m.installed & PANEL_NEEDS))
        self.enable(self.remove_button, m.can_change and bool(m.installed))
        self.action_button.configure(text=m.action_title)
        self.enable(self.action_button, m.can_run_action)
        if m.busy:
            self.progress.pack(side="right", padx=8)
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()
        if self.backups_window is not None:
            self.backups_window.render()

    @staticmethod
    def enable(widget, on):
        widget.state(["!disabled"] if on else ["disabled"])

    def render_printers(self):
        tk, ttk, m = self.tk, self.ttk, self.model
        for child in self.printer_list.winfo_children():
            child.destroy()
        for printer in m.printers:
            current = printer["host"] == m.host
            bg = TINTS[GREEN] if current else "white"
            row = tk.Frame(self.printer_list, bg=bg, padx=8, pady=6)
            row.pack(fill="x", pady=2)
            # The button is packed first, so a long line of text does not squeeze it.
            if current:
                ttk.Button(row, text="Изменить", command=lambda: self.edit_printer(False)).pack(side="right")
            else:
                button = ttk.Button(row, text="Выбрать",
                                    command=lambda h=printer["host"]: self.save_connection({"host": h}))
                button.pack(side="right")
                self.enable(button, not m.busy)
            text = tk.Frame(row, bg=bg)
            text.pack(side="left", fill="x", expand=True)
            tk.Label(text, text=printer["host"], font=self.bold if current else self.font, bg=bg).pack(anchor="w")
            tk.Label(text, text=m.firmware_line if current else (printer.get("name") or "принтер Klipper в сети"),
                     font=self.small, fg=TEXT_GRAY, bg=bg, justify="left", wraplength=150).pack(anchor="w")
            if current and m.custom_login:
                tk.Label(text, text="Свой вход: " + m.user, font=self.small, fg=TEXT_GRAY, bg=bg).pack(anchor="w")

    def render_risks(self):
        tk = self.tk
        for child in self.risks_box.winfo_children():
            child.destroy()
        head = tk.Frame(self.risks_box, bg=TINTS[ORANGE], cursor="hand2")
        head.pack(fill="x")
        title = tk.Label(head, text="⚠ Риски", font=self.bold, fg=ORANGE, bg=TINTS[ORANGE])
        title.pack(side="left")
        fold = tk.Label(head, text="Показать ▾" if self.risks_folded else "Свернуть ▴", font=self.small,
                        fg=TEXT_GRAY, bg=TINTS[ORANGE])
        fold.pack(side="right")
        for widget in (head, title, fold):
            widget.bind("<Button-1>", self.toggle_risks)
        if not self.risks_folded:
            for risk in self.model.risks:
                tk.Label(self.risks_box, text="• " + risk, font=self.small, fg=TEXT_GRAY, bg=TINTS[ORANGE],
                         wraplength=250, justify="left").pack(anchor="w", pady=(4, 0))

    def toggle_risks(self, _event=None):
        self.risks_folded = not self.risks_folded
        self.render_risks()

    def badge(self, parent, text, color):
        return self.tk.Label(parent, text=" %s " % (text,), font=self.small + ("bold",), fg=color, bg=TINTS[color])

    def render_cards(self):
        tk, ttk, m = self.tk, self.ttk, self.model
        top = self.canvas.yview()[0]
        for child in self.cards.winfo_children():
            child.destroy()
        for module in m.modules:
            name = module["id"]
            installed = name in m.installed
            on = name in m.chosen
            own_old = installed and m.installed_versions.get(name) != module.get("version")
            old_parts = m.old_parts(module)
            outdated = own_old or bool(old_parts)
            change = None
            if on and not installed:
                change = ("будет установлен", BLUE)
            elif not on and installed:
                change = ("будет удалён", RED)
            elif on and outdated:
                change = ("будет обновлён", ORANGE)
            color = change[1] if change else (GREEN if installed else GRAY)
            bg = TINTS[color]
            card = tk.Frame(self.cards, bg=bg, highlightthickness=2 if change else 1,
                            highlightbackground=color if (change or installed) else "#d0d7de", padx=12, pady=8)
            card.pack(fill="x", pady=4, padx=(0, 4))
            row = tk.Frame(card, bg=bg)
            row.pack(fill="x")
            text = tk.Frame(row, bg=bg)
            text.pack(side="left", fill="x", expand=True)
            line = tk.Frame(text, bg=bg)
            line.pack(anchor="w")
            tk.Label(line, text=module["title"], font=self.head, bg=bg).pack(side="left")
            tk.Label(line, text="v" + (module.get("version") or ""), font=self.small, fg=TEXT_GRAY,
                     bg=bg).pack(side="left", padx=6)
            tk.Label(text, text=module.get("summary") or "", font=self.font, fg=TEXT_GRAY, bg=bg, justify="left",
                     wraplength=420).pack(anchor="w")
            badges = tk.Frame(text, bg=bg)
            badges.pack(anchor="w", pady=(3, 0))
            if installed and not outdated:
                self.badge(badges, "установлен", GREEN).pack(side="left", padx=(0, 4))
            if own_old:
                version = m.installed_versions.get(name)
                self.badge(badges, "на принтере " + ("v" + version if version else "старая версия"),
                           ORANGE).pack(side="left", padx=(0, 4))
            if change:
                self.badge(badges, change[0], change[1]).pack(side="left")

            variable = tk.BooleanVar(value=on)
            check = ttk.Checkbutton(row, variable=variable,
                                    command=lambda n=name, v=variable: self.toggle(n, v.get()))
            check.pack(side="right", padx=(8, 0))
            self.enable(check, m.can_change)
            opened = name in self.expanded
            ttk.Button(row, text="Подробнее " + ("▴" if opened else "▾"),
                       command=lambda n=name: self.expand(n)).pack(side="right")
            if opened:
                more = tk.Frame(card, bg=bg)
                more.pack(fill="x", pady=(6, 0))
                tk.Label(more, text=module.get("details") or "", font=self.font, bg=bg, justify="left",
                         wraplength=560).pack(anchor="w")
                if outdated:
                    tk.Label(more, text="⬇ Будет обновлено: " + ", ".join(([module["title"]] if own_old else []) + old_parts),
                             font=self.font, fg=ORANGE, bg=bg, justify="left", wraplength=560).pack(anchor="w", pady=(4, 0))
                if module.get("note"):
                    tk.Label(more, text="☑ " + module["note"], font=self.font, fg=TEXT_GRAY, bg=bg, justify="left",
                             wraplength=560).pack(anchor="w", pady=(4, 0))
                link = tk.Label(more, text="Справка и что меняется на принтере", font=self.small + ("underline",),
                                fg=BLUE, bg=bg, cursor="hand2")
                link.pack(anchor="w", pady=(4, 0))
                link.bind("<Button-1>", lambda e, n=name: self.show_help(n))
        self.cards.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(top)

    def toggle(self, name, on):
        self.model.set_chosen(name, on)
        self.render()

    def expand(self, name):
        self.expanded ^= {name}
        self.render_cards()

    # -------------------------------------------------------------- actions

    def confirm_apply(self):
        from tkinter import messagebox
        m = self.model
        if messagebox.askokcancel(m.action_title + "?", m.changes, parent=self.root):
            self.action(*m.apply_arguments())

    def confirm_remove(self):
        from tkinter import messagebox
        if messagebox.askokcancel("Удалить всё с принтера?", "Все пакеты будут удалены. Klipper перезапустится. "
                                  "Твои материалы и цвета останутся на принтере.", icon="warning", parent=self.root):
            self.action("apply", "--remove", ",".join(sorted(self.model.installed)))

    def open_panel(self):
        webbrowser.open("http://%s:%d" % (self.model.host, PANEL_PORT))

    def dialog(self, title):
        window = self.tk.Toplevel(self.root, bg="white", padx=18, pady=14)
        window.title(title)
        window.transient(self.root)
        return window

    def edit_printer(self, adding):
        tk, ttk, m = self.tk, self.ttk, self.model
        window = self.dialog("Добавить принтер" if adding else "Принтер")
        window.resizable(False, False)
        tk.Label(window, text="Добавить принтер" if adding else "Принтер", font=self.head, bg="white").pack(anchor="w")
        tk.Label(window, text="IP-адрес, например 192.168.1.50", font=self.small, fg=TEXT_GRAY,
                 bg="white").pack(anchor="w", pady=(8, 0))
        host = tk.StringVar(value="" if adding else m.host)
        ttk.Entry(window, textvariable=host, width=36).pack(fill="x")
        standard = tk.BooleanVar(value=adding or not m.custom_login)
        user = tk.StringVar(value="" if adding or not m.custom_login else m.user)
        password = tk.StringVar()
        ttk.Checkbutton(window, text="Стандартный вход QIDI", variable=standard).pack(anchor="w", pady=(10, 0))
        hint = tk.Label(window, font=self.small, fg=TEXT_GRAY, bg="white", justify="left", wraplength=300)
        hint.pack(anchor="w")
        login = tk.Frame(window, bg="white")
        tk.Label(login, text="Логин", font=self.small, bg="white").pack(anchor="w")
        ttk.Entry(login, textvariable=user).pack(fill="x")
        keep = m.custom_login and not adding
        tk.Label(login, text="Пароль (пусто: оставить прежний)" if keep else "Пароль", font=self.small,
                 bg="white").pack(anchor="w", pady=(4, 0))
        ttk.Entry(login, textvariable=password, show="•").pack(fill="x")
        buttons = tk.Frame(window, bg="white")
        buttons.pack(side="bottom", fill="x", pady=(12, 0))

        def valid():
            return bool(host.get().strip()) and (standard.get() or (
                bool(user.get().strip()) and (bool(password.get()) or keep)))

        def save():
            connection = {"host": host.get().strip()}
            if standard.get():
                connection["default_login"] = True
            else:
                connection["user"] = user.get().strip()
                if password.get():
                    connection["password"] = password.get()
            window.destroy()
            self.save_connection(connection)

        save_button = ttk.Button(buttons, text="Добавить" if adding else "Сохранить", style="Accent.TButton",
                                 command=save)
        save_button.pack(side="right")
        ttk.Button(buttons, text="Отмена", command=window.destroy).pack(side="right", padx=6)

        def update(*_):
            if standard.get():
                hint.configure(text="Логин и пароль, с которыми QIDI поставляет принтер.")
                login.pack_forget()
            else:
                hint.configure(text="Только если на принтере их меняли.")
                login.pack(fill="x", pady=(6, 0), before=buttons)
            self.enable(save_button, valid() and not m.busy)

        for variable in (host, standard, user, password):
            variable.trace_add("write", update)
        update()
        window.bind("<Return>", lambda e: valid() and save())
        window.bind("<Escape>", lambda e: window.destroy())
        window.grab_set()

    def show_versions(self):
        tk, m = self.tk, self.model
        window = self.dialog("Версии")
        window.resizable(False, False)
        tk.Label(window, text="Версии", font=self.head, bg="white").grid(row=0, column=0, sticky="w", pady=(0, 6))
        rows = [("Программа", m.package_version), ("Пакет здесь", m.package_version),
                ("Пакет на принтере", "не установлен" if not m.installed else (m.printer_package or "неизвестна")),
                ("Прошивка принтера", m.firmware or "неизвестна"),
                ("Проверено на прошивке", ", ".join(m.tested_firmware))]
        rows += [(x["title"], "v" + x.get("version", "") + (" · на принтере " + m.installed_versions.get(x["id"], "?")
                                                           if x["id"] in m.installed else "")) for x in m.modules]
        for number, (name, value) in enumerate(rows, 1):
            tk.Label(window, text=name, font=self.font, bg="white").grid(row=number, column=0, sticky="w")
            tk.Label(window, text=value, font=self.font, fg=TEXT_GRAY, bg="white").grid(
                row=number, column=1, sticky="e", padx=(24, 0))
        if m.firmware and m.firmware not in m.tested_firmware:
            tk.Label(window, text="⚠ Эта прошивка не проверялась, модули могут не работать", font=self.small,
                     fg=ORANGE, bg="white").grid(row=len(rows) + 1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        window.bind("<Escape>", lambda e: window.destroy())

    def show_help(self, only):
        """The package READMEs, which files each adds on the printer and which stock files it changes."""
        tk, ttk, m = self.tk, self.ttk, self.model
        window = self.dialog(m.title(only) if only else "Справка по пакетам")
        window.geometry("640x680")
        top = tk.Frame(window, bg="white")
        top.pack(fill="x")
        tk.Label(top, text=m.title(only) if only else "Справка по пакетам", font=self.big, bg="white").pack(side="left")
        ttk.Button(top, text="Готово", command=window.destroy).pack(side="right")
        holder = tk.Frame(window, bg="white")
        holder.pack(fill="both", expand=True, pady=(10, 0))
        text = tk.Text(holder, wrap="word", font=self.font, relief="flat", bg="white", padx=4, spacing1=2, spacing3=2)
        bar = ttk.Scrollbar(holder, command=text.yview)
        text.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.tag_configure("h1", font=self.big, spacing1=10, spacing3=4)
        text.tag_configure("h2", font=self.head, spacing1=8)
        text.tag_configure("bullet", lmargin1=8, lmargin2=22)
        text.tag_configure("mono", font=self.mono)
        text.tag_configure("small", font=self.small, foreground=TEXT_GRAY)
        text.tag_configure("label", font=self.small + ("bold",), foreground=TEXT_GRAY, spacing1=6)
        text.tag_configure("rule", font=(self.font[0], 4), overstrike=True, foreground="#d0d7de")

        def files(title, items):
            if items:
                text.insert("end", title + "\n", "label")
                for item in items:
                    text.insert("end", "• " + item + "\n", ("bullet", "small"))

        for module in [x for x in m.all_packages if only is None or x["id"] == only]:
            if only is None:
                text.insert("end", module["title"] + "\n", "h1")
            for line in (module.get("readme") or "").splitlines():
                stripped = line.strip()
                if stripped.startswith("# "):
                    continue
                if stripped.startswith("## "):
                    text.insert("end", plain(stripped[3:]) + "\n", "h2")
                elif stripped.startswith("- "):
                    text.insert("end", "• " + plain(stripped[2:]) + "\n", "bullet")
                elif stripped:
                    text.insert("end", plain(stripped) + "\n", "mono" if stripped.startswith("|") else ())
            if module.get("parts"):
                text.insert("end", "Вместе с ним ставится: " + ", ".join(module["parts"]) + "\n", "small")
            files("ДОБАВЛЯЕТ НА ПРИНТЕР", module.get("adds"))
            files("МЕНЯЕТ", module.get("changes"))
            text.insert("end", "\n")
        text.insert("end", "При любой установке\n", "h1")
        files("ДОБАВЛЯЕТ", m.common_adds)
        files("МЕНЯЕТ", m.common_changes)
        text.insert("end", "\nПроверено на прошивке " + ", ".join(m.tested_firmware)
                    + ". Файлы box_*.so и прошивка контроллера Box не меняются.\n", "small")
        text.configure(state="disabled")
        window.bind("<Escape>", lambda e: window.destroy())

    def show_backups(self):
        if self.backups_window is not None:
            self.backups_window.window.lift()
            return
        self.backups_window = BackupsWindow(self)
        self.load_backups()


class BackupsWindow:
    def __init__(self, owner):
        tk, ttk = owner.tk, owner.ttk
        self.owner, self.model = owner, owner.model
        self.window = owner.dialog("Резервные копии")
        self.window.geometry("600x560")
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        top = tk.Frame(self.window, bg="white")
        top.pack(fill="x")
        tk.Label(top, text="Резервные копии", font=owner.big, bg="white").pack(side="left")
        ttk.Button(top, text="Готово", command=self.close).pack(side="right")
        tk.Label(self.window, text="Перед каждой установкой программа сама сохраняет копию всего, что патч может "
                 "изменить: настройки Klipper, файлы пакетов, панель, кнопку в Fluidd и окна экрана. Копии лежат на "
                 "этом компьютере. Копию, сделанную раньше и перенесённую в другое место, можно загрузить из файла. "
                 "Восстановление возвращает принтер к выбранной копии и перезапускает Klipper.",
                 font=owner.font, fg=TEXT_GRAY, bg="white", justify="left", wraplength=560).pack(anchor="w", pady=8)
        buttons = tk.Frame(self.window, bg="white")
        buttons.pack(fill="x")
        self.make = ttk.Button(buttons, text="Сделать копию сейчас", command=lambda: owner.action("backup"))
        self.make.pack(side="left")
        self.load = ttk.Button(buttons, text="Загрузить копию из файла…", command=self.import_backup)
        self.load.pack(side="left", padx=6)
        self.folder = ttk.Button(buttons, text="Открыть папку",
                                 command=lambda: open_folder(backend.BACKUP_DIR / self.model.host))
        self.folder.pack(side="left")
        self.outcome = tk.Label(self.window, font=owner.font, bg="white", anchor="w", justify="left", wraplength=560)
        self.outcome.pack(fill="x", pady=(8, 0))
        holder = tk.Frame(self.window, bg="white")
        holder.pack(fill="both", expand=True, pady=(6, 0))
        self.canvas = tk.Canvas(holder, bg="white", highlightthickness=0)
        bar = ttk.Scrollbar(holder, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.list = tk.Frame(self.canvas, bg="white")
        item = self.canvas.create_window((0, 0), window=self.list, anchor="nw")
        self.list.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(item, width=e.width))
        self.render()

    def close(self):
        self.owner.backups_window = None
        self.window.destroy()

    def modules(self, backup):
        features = backup.get("features") or []
        return ", ".join(self.model.title(f) for f in features) if features else "ничего не установлено"

    def render(self):
        tk, ttk, m, owner = self.owner.tk, self.owner.ttk, self.model, self.owner
        owner.enable(self.make, m.can_change)
        owner.enable(self.load, not m.busy and bool(m.host))
        owner.enable(self.folder, bool(m.backups))
        if m.outcome:
            self.outcome.configure(text=("✔ " if m.outcome[0] else "✖ ") + m.outcome[1],
                                   fg=GREEN if m.outcome[0] else RED)
        else:
            self.outcome.configure(text="⏳ Работаю..." if m.busy else "", fg=TEXT_GRAY)
        for child in self.list.winfo_children():
            child.destroy()
        if not m.backups:
            tk.Label(self.list, text="Копий пока нет.", font=owner.font, fg=TEXT_GRAY, bg="white").pack(anchor="w")
        for backup in m.backups:
            bg = TINTS[GRAY]
            row = tk.Frame(self.list, bg=bg, padx=10, pady=8)
            row.pack(fill="x", pady=3)
            text = tk.Frame(row, bg=bg)
            text.pack(side="left", fill="x", expand=True)
            line = tk.Frame(text, bg=bg)
            line.pack(anchor="w")
            tk.Label(line, text=backup.get("time") or backup["id"], font=owner.head, bg=bg).pack(side="left")
            if backup["id"].endswith("_imported"):
                owner.badge(line, "из файла", BLUE).pack(side="left", padx=6)
            tk.Label(text, text=self.modules(backup), font=owner.font, fg=TEXT_GRAY, bg=bg, justify="left",
                     wraplength=380).pack(anchor="w")
            package = (backup.get("versions") or {}).get("package")
            tk.Label(text, text="Пакет " + ("v" + package if package else "без версии") + " · "
                     + (backup.get("firmware") or "прошивка неизвестна"), font=owner.small, fg=TEXT_GRAY,
                     bg=bg).pack(anchor="w")
            button = ttk.Button(row, text="Восстановить", command=lambda b=backup: self.restore(b))
            button.pack(side="right")
            owner.enable(button, m.can_change)

    def import_backup(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(parent=self.window, title="Выбери файл резервной копии",
                                          filetypes=[("Резервная копия", "*.tar.gz *.gz"), ("Все файлы", "*")])
        if path:
            self.owner.action("import-backup", "--file", path)

    def restore(self, backup):
        from tkinter import messagebox
        if messagebox.askokcancel("Вернуть принтер к копии %s?" % (backup.get("time") or backup["id"],),
                                  "Будет: " + self.modules(backup) + ". Всё, что патч добавил после этой копии, "
                                  "удалится. Klipper перезапустится.", icon="warning", parent=self.window):
            self.owner.action("restore", "--backup", backup["id"])


def main():
    if sys.platform == "win32":
        try:  # sharp text on screens with scaling instead of a blurred bitmap
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    Window("--demo" in sys.argv).root.mainloop()


if __name__ == "__main__":
    main()
