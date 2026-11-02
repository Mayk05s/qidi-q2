#!/usr/bin/env python3
"""Post-processing script for QIDI Studio: asks what to do when a spool will not last for the file.

It never changes the G-code. Only the "cancel" answer exits non-zero, which stops the slicing;
any failure in here exits 0, so a broken check cannot stop a print.
Installed by install_studio_check.py, which also writes printer_host.txt next to it.
"""

import datetime
import html
import http.server
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

import print_check

HERE = pathlib.Path(__file__).resolve().parent
LOG = pathlib.Path.home() / "Library" / "Logs" / "qidi_studio_check.log"
SLOTS = [(0, "1A"), (1, "1B"), (2, "1C"), (3, "1D")]
PANEL_PORT = 18770
INFO_SECONDS = 12
CANCEL, CONTINUE = "Отменить", "Продолжить"
DIALOG = """on run argv
activate
display dialog (item 1 of argv) with title "Хватит ли пластика" buttons {"%s", "%s"} default button 1 with icon caution
end run""" % (CANCEL, CONTINUE)


def log(text):
    with LOG.open("a", encoding="utf-8") as out:
        out.write("%s %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text))


def gcode_path(args):
    """Studio hands over one path; a path with spaces may arrive split into several arguments."""
    joined = " ".join(args)
    return args[-1] if args and os.path.exists(args[-1]) else joined


def fetch_status(host):
    url = "http://%s:7125/printer/objects/query?save_variables&bambu_spool_usage" % (host,)
    with urllib.request.urlopen(url, timeout=3) as res:
        return json.loads(res.read())["result"]["status"]


def grams(value):
    return ("%.1f" % (value,)).replace(".", ",") + " г"


def warnings(rows):
    lines = []
    for row in rows:
        name = row["label"] or "T" + ", T".join(str(tool) for tool in row["tools"])
        if row["material"]:
            name += " (%s)" % (row["material"],)
        need = "нужно %s" % (grams(row["need"]),)
        if row["verdict"] == "short":
            lines.append("%s: %s, осталось %s, не хватит примерно %s" % (
                name, need, grams(row["remaining"]), grams(-row["spare"])))
        elif row["verdict"] == "risk":
            lines.append("%s: %s, осталось %s, запас всего %s" % (
                name, need, grams(row["remaining"]), grams(row["spare"])))
        elif row["verdict"] == "unknown":
            lines.append("%s: %s, остаток неизвестен" % (name, need))
    return lines


PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{--on-status:#fff;--bg:#f4f4f1;--card:#fff;--accent:#1f5fd0;--on-accent:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#dcdcd5;--ok:#1c7c3f;--err:#b3261e;--warn:#9a5c00;--flange:#d9d9d3;--edge:#b4b4ac;--hub:#f4f4f1;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--on-status:#10131a;--bg:#161615;--card:#21211f;--accent:#7fa8f5;--on-accent:#10131a;--ink:#f1f1ec;--muted:#a3a39b;--line:#3a3a36;--ok:#63c98a;--err:#f2897f;--warn:#e8b04a;--flange:#3a3a37;--edge:#55554f;--hub:#161615;color-scheme:dark}}
*{box-sizing:border-box;cursor:default;user-select:none}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.35 -apple-system,system-ui,sans-serif;padding:32px 18px 16px}
h1{font-size:19px;margin:0 0 12px;text-align:center}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px;text-align:center;display:flex;flex-direction:column;gap:2px}
.card.short{border-color:var(--err)}.card.risk,.card.unknown{border-color:var(--warn)}
.card.idle{background:transparent}.card.idle svg{opacity:.45}.card.idle .label,.card.idle .name,.card.idle .left{color:var(--muted)}
svg{width:84px;height:84px;margin:0 auto 6px;display:block}
.label{font-size:17px;font-weight:700}
.name{font-size:13px;font-weight:600}
.left{margin-top:10px;border:1px solid var(--line);border-radius:9px;padding:6px 9px;font-size:14px}
.bar{display:block;height:4px;margin-top:5px;border-radius:2px;background:var(--line);overflow:hidden}
.bar span{display:block;height:100%%;background:var(--muted)}
.ok .bar span{background:var(--ok)}.short .bar span{background:var(--err)}.risk .bar span{background:var(--warn)}
.short .left{border-color:var(--err);color:var(--err)}.risk .left{border-color:var(--warn);color:var(--warn)}
.need{color:var(--muted);font-size:12px;margin-top:7px;min-height:16px}
.verdict{margin-top:6px;padding:5px 6px;border-radius:9px;font-weight:700;font-size:13px}
.ok .verdict{color:var(--ok);background:color-mix(in srgb,var(--ok) 15%%,transparent)}
.short .verdict{color:var(--err);background:color-mix(in srgb,var(--err) 15%%,transparent)}
.risk .verdict,.unknown .verdict{color:var(--warn);background:color-mix(in srgb,var(--warn) 15%%,transparent)}
.idle .verdict{color:var(--muted);font-weight:400}
.card{position:relative}
.reread{position:absolute;top:8px;right:8px;width:30px;height:30px;padding:0;display:grid;place-items:center;background:transparent;border:1px solid var(--line);border-radius:9px;color:var(--muted)}
.reread:hover{color:var(--ink);border-color:var(--edge)}
.reread svg{width:16px;height:16px;margin:0;opacity:1!important}
.reread.busy{pointer-events:none}.reread.busy svg{animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.note{color:var(--muted);font-size:12px;margin:10px 0 12px;text-align:center;min-height:17px}
.note.err{color:var(--err);font-weight:600}
h1.good{color:var(--ok)}
.buttons{display:flex;justify-content:flex-end;gap:8px}
#ask{position:fixed;inset:0;display:grid;place-items:center;background:rgba(0,0,0,.45)}
#ask[hidden]{display:none}
#ask div.box{width:360px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;text-align:center}
#ask h2{margin:0 0 6px;font-size:16px}#ask p{margin:0 0 14px;color:var(--muted);font-size:13px}
button{font:inherit;font-weight:600;padding:7px 18px;border-radius:9px;border:1px solid transparent}
button svg{width:14px;height:14px;margin:0;display:inline-block;vertical-align:-2px;margin-right:6px}
.read{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.cancel,#ask .buttons button.no{background:color-mix(in srgb,var(--ink) 9%%,transparent);border-color:var(--edge);color:var(--ink)}
.continue{color:var(--on-status)}
.continue.good{background:var(--ok)}.continue.warn{background:var(--warn)}.continue.short{background:var(--err)}
.summary{margin-right:auto;align-self:center;font-weight:600}
.summary.good{color:var(--ok)}.summary.warn{color:var(--warn)}.summary.short{color:var(--err)}
h1.warn{color:var(--warn)}h1.short{color:var(--err)}
body.info{padding:30px 14px 12px}
.info h1{font-size:16px;margin-bottom:8px;color:var(--ok)}
.info .reread,.info .cancel,.info .need{display:none}
.info .cards{gap:8px}.info .card{padding:8px}
.info .card svg{width:48px;height:48px;margin-bottom:2px}
.info .label{font-size:14px}.info .name{font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.info .left{margin-top:6px;padding:4px 6px;font-size:13px}.info .verdict{font-size:12px;padding:3px 4px;margin-top:5px}
.info .note{margin:6px 0 8px}
.info .summary{font-size:13px}
</style></head><body>
<h1 id="title">&nbsp;</h1>
<div class="cards" id="cards"></div>
<p class="note" id="note"></p>
<div class="buttons">
<span class="summary" id="summary"></span>
<button class="cancel" data-a="cancel"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M3.5 3.5l9 9M12.5 3.5l-9 9"/></svg>Отменить</button>
<button class="continue short" id="go" data-a="continue"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2.5 8.5l3.5 3.5 7.5-8"/></svg><span id="go-text">Всё равно продолжить</span></button>
</div>
<div id="ask" hidden><div class="box"><h2 id="ask-title"></h2><p>Данные метки будут прочитаны заново. Мотор Box покачает катушку и двинет филамент.</p>
<div class="buttons"><button class="no" id="ask-no">Не надо</button><button class="read" id="ask-yes">Перечитать</button></div></div></div>
<script>
var NOTE="Остаток посчитан, а не взвешен. Другие слоты, выбранные при отправке, проверка не увидит.",slot=null,error="";
var $=function(id){return document.getElementById(id)};
document.querySelectorAll("[data-a]").forEach(function(b){b.onclick=function(){
window.webkit.messageHandlers.answer.postMessage(b.dataset.a)}});
$("cards").onclick=function(e){var b=e.target.closest(".reread");if(!b)return;
slot=b.dataset.slot;$("ask-title").textContent="Перечитать слот "+b.dataset.label+"?";$("ask").hidden=false};
$("ask-no").onclick=function(){$("ask").hidden=true};
$("ask-yes").onclick=async function(){$("ask").hidden=true;error="";
var res=await fetch("read/"+slot,{method:"POST"});var d=await res.json();error=d.error||"";load()};
function note(text){var n=$("note");n.textContent=text||NOTE;n.className=text?"note err":"note"}
var busy=false;
async function load(){if(busy)return;busy=true;try{
var d=await (await fetch("state")).json();
if(d.error){note(d.error);return}
$("cards").innerHTML=d.cards;$("title").textContent=window.INFO?"Пластика хватает":d.title;$("title").className=d.good?"good":d.level;
$("go").className="continue "+d.level;$("go-text").textContent=window.INFO?"Закрыть":d.good?"Продолжить":"Всё равно продолжить";
$("summary").textContent=d.summary;$("summary").className="summary "+d.level;
note(d.read_error||error)}catch(e){note("Принтер не отвечает, данные могут быть устаревшими")}
finally{busy=false}}
load();setInterval(load,2000);%(script)s
</script>
</body></html>"""
SPOOL = """<svg viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="48" fill="var(--flange)" stroke="var(--edge)"/>
<circle cx="50" cy="50" r="40" fill="%s"/><circle cx="50" cy="50" r="35" fill="none" stroke="#000" stroke-opacity=".12"/>
<circle cx="50" cy="50" r="30" fill="none" stroke="#000" stroke-opacity=".12"/><circle cx="50" cy="50" r="25" fill="none" stroke="#000" stroke-opacity=".12"/>
<circle cx="50" cy="50" r="20" fill="var(--edge)"/><circle cx="50" cy="50" r="8" fill="var(--hub)"/></svg>"""
VERDICTS = {"ok": "✓ Хватит", "short": "Не хватит %s", "risk": "Запас всего %s", "unknown": "Остаток неизвестен",
            "idle": "Не используется"}
HEX = re.compile(r"[0-9A-Fa-f]{6}")
REREAD = """<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>"""


def card(index, label, row, variables, spool, reading=False):
    """One slot, drawn like its card in the panel. A slot the file does not use stays dim."""
    color = variables.get("color_slot%d" % (index,))
    color = color[0] if isinstance(color, list) and color and HEX.fullmatch(str(color[0])) else None
    tag = variables.get("bambu_tag_slot%d" % (index,))
    tag = tag if isinstance(tag, dict) else {}
    name = " ".join(str(tag[key]) for key in ("brand", "detail") if tag.get(key)) or "Без метки"
    remaining = spool.get("remaining") if isinstance(spool, dict) else None
    verdict = row["verdict"] if row else "idle"
    text = VERDICTS[verdict]
    if "%s" in text:
        text %= (grams(-row["spare"] if verdict == "short" else row["spare"]),)
    left = "нет данных"
    if isinstance(remaining, (int, float)):
        width = min(max(remaining / (tag.get("weight") or 1000), 0), 1) * 100
        left = '≈ %d г<span class="bar"><span style="width:%.0f%%"></span></span>' % (round(remaining), width)
    button = ('<button class="reread%s" data-slot="%d" data-label="%s" title="Перечитать метку" aria-label="Перечитать метку %s">%s</button>'
              % (" busy" if reading else "", index, html.escape(label), html.escape(label), REREAD))
    return ('<div class="card %s">' + button + '%s<div class="label">%s</div><div class="name">%s</div><div class="left">%s</div>'
            '<div class="need">%s</div><div class="verdict">%s</div></div>') % (
        verdict, SPOOL % ("#" + color if color else "var(--line)",), html.escape(label), html.escape(name), left,
        "нужно " + grams(row["need"]) if row else "", html.escape(text))


def summary(rows):
    """Says what pressing "continue" means now, next to the button whose colour shows the same."""
    def names(verdict):
        return ", ".join(str(row["label"]) for row in rows if row["verdict"] == verdict)
    if names("short"):
        return "Пластика на %s не хватит до конца печати" % (names("short"),)
    if names("unknown"):
        return "Неизвестно, хватит ли пластика на %s" % (names("unknown"),)
    if names("risk"):
        return "На %s пластика почти впритык" % (names("risk"),)
    return "Пластика хватает на всю печать"


def panel_call(host, path, body=None):
    """The panel service on the printer. It owns the tag read, so the read works the same as there."""
    origin = "http://%s:%d" % (host, PANEL_PORT)
    request = urllib.request.Request(origin + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Origin": origin, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


class CheckWindow:
    """What the window shows: asks the printer on every refresh, so a swapped spool appears by itself."""

    def __init__(self, plan, host, status_of=fetch_status, panel=panel_call):
        self.plan, self.host, self.status_of, self.panel = plan, host, status_of, panel

    def state(self):
        status = self.status_of(self.host)
        variables = status["save_variables"]["variables"]
        spools = (status.get("bambu_spool_usage") or {}).get("slots") or []
        rows = print_check.evaluate(self.plan, variables, spools, dict(SLOTS))
        try:
            reads = {slot["index"]: slot for slot in self.panel(self.host, "/api/slots").get("slots") or []}
        except (OSError, ValueError):
            reads = {}
        by_index = {row["index"]: row for row in rows}
        cards = "".join(card(index, label, by_index.get(index), variables,
                             spools[index] if index < len(spools) else None,
                             bool(reads.get(index, {}).get("reading"))) for index, label in SLOTS)
        errors = ["%s: %s" % (slot["label"], slot["read_error"]) for slot in reads.values() if slot.get("read_error")]
        problems = [row for row in rows if row["verdict"] != "ok"]
        short = [row["label"] for row in rows if row["verdict"] == "short"]
        if not problems:
            title = "Теперь пластика хватает"
        elif short:
            title = "Не хватит пластика: " + ", ".join(short)
        else:
            title = "Стоит проверить катушки: " + ", ".join(str(row["label"]) for row in problems)
        return {"title": title, "good": not problems, "level": "short" if short else "warn" if problems else "good",
                "summary": summary(rows), "cards": cards, "read_error": "; ".join(errors)}

    def read(self, index):
        return self.panel(self.host, "/api/slot_read", {"index": index})


def serve(check, script=""):
    """Local server for the window, reachable only with the random part of its address."""
    token = secrets.token_hex(16)
    page = (PAGE % {"script": script}).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, kind="application/json; charset=utf-8"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                if self.path == "/%s/" % (token,):
                    return self.send(200, page, "text/html; charset=utf-8")
                if self.path == "/%s/state" % (token,):
                    return self.send(200, check.state())
            except (OSError, ValueError, KeyError) as e:
                return self.send(200, {"error": "Принтер не отвечает: %s" % (e,)})
            self.send(404, {"error": "not found"})

        def do_POST(self):
            match = re.fullmatch(r"/%s/read/([0-3])" % (token,), self.path)
            if not match:
                return self.send(404, {"error": "not found"})
            try:
                self.send(200, check.read(int(match.group(1))))
            except (OSError, ValueError) as e:
                self.send(200, {"error": "Панель на принтере не отвечает: %s" % (e,)})

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d/%s/" % (server.server_address[1], token)


def ask_window(plan, host, status_of=fetch_status, script=""):
    """The answer from the window, or None when the window cannot be shown."""
    helper = HERE / "studio_window"
    if not helper.exists():
        return None
    server, url = serve(CheckWindow(plan, host, status_of), script)
    try:
        result = subprocess.run([str(helper), url], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, encoding="utf-8")
    finally:
        server.shutdown()
    return {"cancel": CANCEL, "continue": CONTINUE}.get(result.stdout.strip())


# Closes by itself unless the pointer rests on it; pressing the button only closes it too.
INFO_SCRIPT = """window.INFO=true;document.body.classList.add("info");
NOTE="Это окно только сообщает и ни на что не влияет.";note();
var hover=false,left=%d;document.body.onmouseenter=function(){hover=true};document.body.onmouseleave=function(){hover=false};
setInterval(function(){if(hover)return;left--;if(left<=0)$("go").click()},1000);""" % (INFO_SECONDS,)


def show_info(plan):
    """All is fine: tell it from a process of its own, so Studio goes on at once."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as out:
        json.dump(plan, out)
    subprocess.Popen([sys.executable, str(HERE / "studio_check.py"), "--info", out.name],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, cwd=str(HERE))


def info_window(path):
    plan = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    os.unlink(path)
    host = (HERE / "printer_host.txt").read_text(encoding="utf-8").strip()
    server, url = serve(CheckWindow(plan, host), INFO_SCRIPT)
    try:
        subprocess.run([str(HERE / "studio_window"), url, "passive"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
    finally:
        server.shutdown()


def ask(lines):
    text = "\n".join(lines) + "\n\nОстаток посчитан по метке и не взвешен. Слоты взяты с принтера: если при отправке выберешь другие, проверка будет про другие катушки."
    # Studio waits here, which is what lets the answer stop the slicing.
    answer = subprocess.run(["osascript", "-e", DIALOG, text], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8")
    return answer.stdout.partition("button returned:")[2].strip() or CONTINUE


def run(args, status_of=fetch_status, choose=ask, window=ask_window, info=show_info):
    """The exit code for Studio: non-zero only when the answer is to cancel."""
    path = gcode_path(args)
    plan = print_check.parse_plan(print_check.gcode_text(pathlib.Path(path).read_bytes()))
    host = (HERE / "printer_host.txt").read_text(encoding="utf-8").strip()
    status = status_of(host)
    spools = (status.get("bambu_spool_usage") or {}).get("slots") or []
    rows = print_check.evaluate(plan, status["save_variables"]["variables"], spools, dict(SLOTS))
    lines = warnings(rows)
    log("%s: %s" % (path, "; ".join("%s %s" % (r["label"], r["verdict"]) for r in rows)))
    if not lines:
        info(plan)
        return 0
    answer = window(plan, host, status_of) or choose(lines)
    log("ответ: %s" % (answer,))
    return 1 if answer == CANCEL else 0


def main():
    code = 0
    try:
        if sys.argv[1:2] == ["--info"]:
            info_window(sys.argv[2])
        else:
            code = run(sys.argv[1:])
    except Exception as e:  # The slicer must keep working whatever happens here.
        try:
            log("%r: %s: %s" % (sys.argv[1:], type(e).__name__, e))
        except OSError:
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
