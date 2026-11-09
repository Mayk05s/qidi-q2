#!/usr/bin/env python3
"""Builds emulator.html around the very files that go to the printer.

The page runs Python in the browser (Pyodide) and loads pair_logic.py and the spool model from
test_pair_logic.py as they are now, so what you watch is the module's own logic. Run this again
after every change to pair_logic.py: python3 build_emulator.py
"""
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
PACKAGE = HERE.parent.parent
# The search limit the printer uses before a slot's splitter is known.
_feed = (PACKAGE / "printer" / "box_feed.py").read_text(encoding="utf-8")
import re as _re
DEFAULT_LIMIT = int(float(_re.search(r"^FEED_LENGTH = ([0-9.]+)", _feed, _re.M).group(1))
                    - float(_re.search(r"^PAIR_SWING_ROOM = ([0-9.]+)", _feed, _re.M).group(1)))
SOURCES = {"pair_logic.py": (PACKAGE / "printer" / "pair_logic.py").read_text(encoding="utf-8"),
           "test_pair_logic.py": (PACKAGE / "tests" / "test_pair_logic.py").read_text(encoding="utf-8")}

BRIDGE = r'''
import json, random
import pair_logic as p
import test_pair_logic as T

SLOTS = (("1A", 0, "A"), ("1B", 0, "B"), ("1C", 1, "A"), ("1D", 1, "B"))
box = None

def new_box(cfg):
    global box
    c = json.loads(cfg)
    box = []
    for pair in (0, 1):
        a, b = c["angles"][pair * 2], c["angles"][pair * 2 + 1]
        w = T.World(a, b, delay=c["delay"], slack=(c["slackMin"], c["slackMax"]),
                    slip=(c["slipMin"], c["slipMax"]), has=(c["has"][pair * 2], c["has"][pair * 2 + 1]),
                    limit=c["limit"], seed=random.randrange(1 << 30))
        box.append(w)

def insert(slot):
    label, pair, name = SLOTS[slot]
    w = box[pair]
    w.logic.insert(name, w.t)

def reread(slot):
    label, pair, name = SLOTS[slot]
    w = box[pair]
    if w.logic.start_reread(name, w.t):
        w.next = w.t

def advance(seconds):
    for w in box:
        steps = int(round(seconds / T.STEP))
        for _ in range(steps):
            w.step()
            if not w.logic.done:
                w.tick()

def state():
    out = []
    for label, pair, name in SLOTS:
        w = box[pair]
        s, st = w.logic.spool(name), w.state[name]
        out.append(dict(label=label, angle=st["angle"], has=st["has"], inserted=s.inserted, pos=s.pos,
                        real=st["real"], tag=s.tag, sure=p.SURE_NAMES.get(s.sure, "") if s.tag else "", noTag=s.no_tag, out=s.out, dir=s.dir,
                        moving=s.moving, limit=w.logic.limit))
    logs = [w.logic.log[-10:] for w in box]
    phases = [w.logic.phase for w in box]
    guards = [w.logic.guard for w in box]
    return json.dumps(dict(slots=out, logs=logs, phases=phases, guards=guards))

def sweep(cfg, step):
    c = json.loads(cfg)
    runs = ok = wrong = miss = 0
    bad = []
    for order in ("AB", "BA"):
        for gap in (0., .5, 1., 3., 8., 30.):
            for a in range(0, 360, step):
                for b in range(0, 360, step):
                    w = T.World(a, b, delay=c["delay"], slack=(c["slackMin"], c["slackMax"]),
                                slip=(c["slipMin"], c["slipMax"]), limit=c["limit"], seed=a * 1000 + b)
                    finished = w.play(gap, order)
                    got = (w.logic.spool("A").tag, w.logic.spool("B").tag)
                    runs += 1
                    if finished and got == ("A", "B") and not w.logic.guard:
                        ok += 1
                    else:
                        if any(v is not None and v != n for v, n in zip(got, "AB")):
                            wrong += 1
                        else:
                            miss += 1
                        if len(bad) < 12:
                            bad.append([a, b, gap, order])
    return json.dumps(dict(runs=runs, ok=ok, wrong=wrong, miss=miss, bad=bad))
'''

PAGE = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Эмулятор подачи Box</title>
<style>
:root{--bg:#fff;--fg:#1f1f1d;--muted:#6b6a65;--card:#f5f4ef;--line:#c9c7bd}
@media (prefers-color-scheme:dark){:root{--bg:#1e1e1c;--fg:#ecebe6;--muted:#a3a19a;--card:#2a2a27;--line:#55534d}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,system-ui,sans-serif}
h1{font-size:18px;font-weight:500;margin:0 0 4px}.muted{color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:12px}
.card{background:var(--card);border-radius:8px;padding:8px}
.params{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-top:10px;color:var(--muted);font-size:13px}
input[type=number]{width:100%;box-sizing:border-box}button{padding:5px 10px;margin:2px}
pre{white-space:pre-wrap;font-size:12px;color:var(--muted);margin:0}
#res{margin-top:8px;padding:8px 12px;background:var(--card);border-radius:8px}
@media (max-width:640px){.grid,.params{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body>
<h1>Эмулятор подачи Box</h1>
<div class="muted" id="ver">Загружаю Python в браузере, около 10 секунд…</div>
<div class="grid" id="grid"></div>
<div class="params">
<label>Слабина от, мм<input id="ps" type="number" step="10" value="0"></label>
<label>Слабина до, мм<input id="pm" type="number" step="10" value="300"></label>
<label>Не подхватилась от, мм<input id="pg" type="number" step="10" value="0"></label>
<label>Не подхватилась до, мм<input id="ph" type="number" step="10" value="0"></label>
<label>Задержка стопа, с<input id="pd" type="number" step=".1" value=".3"></label>
<label>Предел, мм<input id="pl" type="number" step="50" value="__LIMIT__"></label>
<label>Ускорение показа<input id="px" type="number" step="1" value="3"></label>
<label>Шаг прогона, °<input id="pz" type="number" step="5" value="30"></label></div>
<div><button id="all">Вставить все 4</button><button id="rnd">Случайные положения</button><button id="rs">Сброс</button><button id="bt">Прогнать все положения</button></div>
<div id="res"></div>
<div class="grid" style="grid-template-columns:1fr 1fr"><pre id="log0"></pre><pre id="log1"></pre></div>
<script src="https://cdn.jsdelivr.net/pyodide/v0.26.4/full/pyodide.js"></script>
<script>
const SOURCES=__SOURCES__, BRIDGE=__BRIDGE__, BUILT="__BUILT__";
const $=i=>document.getElementById(i),num=i=>+$(i).value,L=['1A','1B','1C','1D'],COL=['#7F77DD','#1D9E75','#D85A30','#D4537E'];
$('grid').innerHTML=L.map((l,i)=>`<div class="card"><svg width="100%" viewBox="0 0 120 120"><path d="M60 60L48 4A57 57 0 0 1 72 4Z" fill="#EF9F27" opacity=".25"/><circle cx="60" cy="60" r="50" fill="none" stroke="var(--line)"/><g id="g${i}"><circle id="m${i}" cx="60" cy="16" r="7" fill="${COL[i]}"/></g><text x="60" y="65" text-anchor="middle" fill="currentColor" font-size="14">${l}</text></svg><div id="s${i}" style="min-height:66px;font-size:13px"></div><label class="muted" style="font-size:13px">Положение <span id="v${i}"></span>°<input id="o${i}" type="range" min="0" max="359" value="${[358,20,180,90][i]}" style="width:100%"></label><label style="font-size:13px"><input type="checkbox" id="h${i}" checked> Есть метка</label><div><button id="i${i}">Вставить</button><button id="r${i}">Перечитать</button></div></div>`).join('');
let py,last=0,ready=false;
const cfg=()=>JSON.stringify({angles:[0,1,2,3].map(i=>num('o'+i)),has:[0,1,2,3].map(i=>$('h'+i).checked),delay:num('pd'),slackMin:num('ps'),slackMax:Math.max(num('ps'),num('pm')),slipMin:num('pg'),slipMax:Math.max(num('pg'),num('ph')),limit:num('pl')});
function reset(){if(!ready)return;py.globals.get('new_box')(cfg());$('res').textContent='Поставь катушки ползунками и нажимай «Вставить».';draw()}
function draw(){const S=JSON.parse(py.globals.get('state')());S.slots.forEach((s,i)=>{$('g'+i).setAttribute('transform',`rotate(${s.angle} 60 60)`);$('m'+i).style.display=s.has?'':'none';$('v'+i).textContent=$('o'+i).value;
const st=!s.inserted?'Не вставлена':s.out?'НИТЬ ВЫШЛА':s.moving?(s.dir<0?'НАЗАД':'Вперёд'):(s.pos>=s.limit&&(s.tag||s.noTag)?'Готова':'Стоит');
const tag=s.tag?(L[[0,1,2,3].find(j=>Math.floor(j/2)===Math.floor(i/2)&&'AB'[j%2]===s.tag)]):(s.noTag?'нет':'неизвестно');
$('s'+i).textContent=`${st}. Команда ${Math.round(s.pos)} мм, реально ${Math.round(s.real)} мм. Метка: ${tag}${s.sure?' ('+({'guess':'под вопросом','left with the turning spool':'ушла с катушкой','appeared while only it turned':'надёжно','reread':'перечитано'}[s.sure]||s.sure)+')':''}`});
S.logs.forEach((l,p)=>$('log'+p).textContent=l.join('\n'));
const rep=S.slots.filter(s=>s.inserted).map((s,k)=>{const i=S.slots.indexOf(s),own='AB'[i%2];if(s.out)return s.label+': нить вышла';if(s.tag&&s.tag!==own)return s.label+': ЧУЖАЯ МЕТКА';if(s.tag)return s.label+': метка верная'+(s.pos>=s.limit&&!s.moving?', готова':'');if(s.noTag)return s.label+(s.has?': МЕТКА НЕ НАЙДЕНА':': меток нет, верно');return s.label+': ищем'});
if(rep.length)$('res').textContent=rep.join(' · ')+(S.guards.some(x=>x)?' · СРАБОТАЛА ЗАЩИТА':'')}
function frame(t){const dt=Math.min(.05,(t-last)/1000)*Math.max(1,num('px'));last=t;if(ready){py.globals.get('advance')(dt);draw()}requestAnimationFrame(frame)}
(async()=>{py=await loadPyodide();for(const[n,src]of Object.entries(SOURCES))py.FS.writeFile(n,src);py.runPython(BRIDGE);ready=true;$('ver').textContent='Логика: pair_logic.py из пакета spools, сборка '+BUILT+'. Это тот же файл, что ставится на принтер.';reset()})();
[0,1,2,3].forEach(i=>{$('i'+i).onclick=()=>py.globals.get('insert')(i);$('r'+i).onclick=()=>py.globals.get('reread')(i);['o','h'].forEach(k=>$(k+i).oninput=reset)});
['ps','pm','pg','ph','pd','pl'].forEach(i=>$(i).oninput=reset);$('rs').onclick=reset;
$('all').onclick=()=>[0,1,2,3].forEach(i=>py.globals.get('insert')(i));
$('rnd').onclick=()=>{[0,1,2,3].forEach(i=>$('o'+i).value=Math.floor(Math.random()*360));reset()};
$('bt').onclick=()=>{$('res').textContent='Прогоняю в Python, это может занять минуту…';setTimeout(()=>{const r=JSON.parse(py.globals.get('sweep')(cfg(),Math.max(5,num('pz'))));$('res').textContent=`Прогонов ${r.runs}. Верно ${r.ok}, чужая метка ${r.wrong}, не найдена или зависло ${r.miss}.`+(r.bad.length?' Первые неудачи (A°, B°, интервал, порядок): '+r.bad.map(b=>b.join('/')).join('; '):'')},50)};
requestAnimationFrame(t=>{last=t;frame(t)});
</script></body></html>
'''

if __name__ == "__main__":
    import datetime
    page = (PAGE.replace("__SOURCES__", json.dumps(SOURCES))
            .replace("__BRIDGE__", json.dumps(BRIDGE))
            .replace("__BUILT__", datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
            .replace("__LIMIT__", str(DEFAULT_LIMIT)))
    (HERE / "emulator.html").write_text(page, encoding="utf-8")
    print("written", HERE / "emulator.html")
