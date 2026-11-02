"""The plastic left on the spools, shown in Fluidd itself: grams under each spool of the Box card,
and a card under it that warns when the running or the newest print will not last.

The Fluidd page loads SCRIPT from the panel (see qidi_panel_link.js); the data comes from
/api/fluidd, which the panel answers to the printer's own page.
"""


def card_data(slots, check):
    """What the Fluidd script needs, from Printer.slots() and PrintCheck.check()."""
    spools = []
    for slot in slots["slots"]:
        spool = slot.get("spool") or {}
        tag = slot.get("tag") or {}
        spools.append({"label": slot["label"], "color": slot.get("color"), "material": tag.get("detail"),
                       "remaining": spool.get("remaining"), "weight": tag.get("weight") or 1000})
    rows = [{"label": row["label"], "need": row["left_to_print"] if check.get("printing") else row["need"],
             "remaining": row["remaining"], "verdict": row["verdict"]}
            for row in check.get("rows") or [] if row["verdict"] != "ok"]
    return {"spools": spools, "file": check.get("file"), "printing": bool(check.get("printing")),
            "problems": rows, "message": check.get("message")}


SCRIPT = r"""(function(){
var API=location.protocol+"//"+location.hostname+":18770/api/fluidd";
var data=null,busy=false;
var style=document.createElement("style");
style.textContent=[
".qidi-left{display:flex;width:max-content;flex-direction:column;align-items:center;gap:3px;margin:6px auto 0;padding:3px 8px;border-radius:10px;background:rgba(127,127,127,.14);font-size:12px;font-weight:500;font-variant-numeric:tabular-nums}",
".qidi-left i{display:block;width:40px;height:3px;border-radius:2px;background:rgba(127,127,127,.3);overflow:hidden}",
".qidi-left i b{display:block;height:100%;border-radius:2px;background:#4caf50}",
".qidi-left.low{color:#ff6e6e;background:rgba(255,82,82,.14)}.qidi-left.low i b{background:#ff5252}",
"#qidi-plastic-card{margin-bottom:12px;border-radius:4px;overflow:hidden}",
"#qidi-plastic-card .qh{display:flex;align-items:center;gap:10px;padding:12px 16px;font-size:16px;font-weight:500;border-bottom:1px solid rgba(127,127,127,.18)}",
"#qidi-plastic-card .qh svg{width:20px;height:20px;opacity:.85}",
"#qidi-plastic-card .qf{margin-left:auto;font-size:12px;font-weight:400;padding:2px 10px;border-radius:12px;background:rgba(127,127,127,.16);max-width:55%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
"#qidi-plastic-card .qb{padding:14px 16px 16px}",
"#qidi-plastic-card .qg{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}",
"#qidi-plastic-card .qt{position:relative;display:flex;align-items:center;gap:10px;padding:10px;border-radius:8px;border:1px solid rgba(127,127,127,.22);background:rgba(127,127,127,.06);min-width:0}",
"#qidi-plastic-card .qt.short{border-color:#ff5252;background:rgba(255,82,82,.08)}#qidi-plastic-card .qt.risk{border-color:#ffb74d;background:rgba(255,183,77,.08)}",
"#qidi-plastic-card .qt svg{flex:none;width:38px;height:38px}",
"#qidi-plastic-card .qn{min-width:0;flex:1}#qidi-plastic-card .ql{font-weight:600;font-size:14px}",
"#qidi-plastic-card .qm{font-size:11px;opacity:.65;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
"#qidi-plastic-card .qv{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}",
"#qidi-plastic-card .qbar{height:4px;border-radius:2px;background:rgba(127,127,127,.25);overflow:hidden;margin-top:5px}",
"#qidi-plastic-card .qbar b{display:block;height:100%;border-radius:2px;background:#4caf50}",
"#qidi-plastic-card .qt.short .qbar b,#qidi-plastic-card .low .qbar b{background:#ff5252}#qidi-plastic-card .qt.risk .qbar b{background:#ffb74d}",
"#qidi-plastic-card .qw{display:flex;gap:12px;align-items:flex-start;margin-top:14px;padding:12px 14px;border-radius:8px}",
"#qidi-plastic-card .qw svg{flex:none;width:22px;height:22px;margin-top:1px}",
"#qidi-plastic-card .qw .t{font-weight:600;font-size:14px}#qidi-plastic-card .qw .d{font-size:13px;opacity:.9;margin-top:2px}",
"#qidi-plastic-card .qw.short{background:rgba(255,82,82,.12);color:#ff7b7b}",
"#qidi-plastic-card .qw.risk,#qidi-plastic-card .qw.unknown{background:rgba(255,183,77,.12);color:#ffc370}",
"#qidi-plastic-card .qw.ok{background:rgba(76,175,80,.12);color:#6fcf73}",
"@media (max-width:700px){#qidi-plastic-card .qg{grid-template-columns:repeat(2,minmax(0,1fr))}}"
].join("");
document.head.appendChild(style);
var ICON_SPOOL='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/></svg>';
var ICON_WARN='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 3 2 20h20L12 3z"/><path d="M12 10v4M12 17v.5" stroke-linecap="round"/></svg>';
var ICON_OK='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></svg>';
function spoolSvg(color){
  var c=color?"#"+color:"#555";
  return '<svg viewBox="0 0 40 40"><circle cx="20" cy="20" r="19" fill="#3a3a38" stroke="#585853"/><circle cx="20" cy="20" r="15.5" fill="'+c+'"/>'
    +'<circle cx="20" cy="20" r="12.5" fill="none" stroke="#000" stroke-opacity=".12"/><circle cx="20" cy="20" r="9.5" fill="none" stroke="#000" stroke-opacity=".12"/>'
    +'<circle cx="20" cy="20" r="7" fill="#9a9a95"/><circle cx="20" cy="20" r="2.6" fill="#2a2a28"/></svg>';
}
function g(n){return Math.round(n)+" г"}
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function boxCard(){
  // The innermost card whose title is the Box card's, in Russian or English Fluidd.
  var best=null;
  document.querySelectorAll(".v-card").forEach(function(card){
    if(card.id==="qidi-plastic-card")return;
    var head=card.textContent.trim().slice(0,60);
    if(/^(Управление материалами|Control Box)/.test(head)&&(!best||best.contains(card)))best=card;
  });
  return best;
}
function spoolColumns(card){
  var found={};
  card.querySelectorAll("button span").forEach(function(span){
    var label=span.textContent.trim();
    if(/^[1-4][A-D]$/.test(label)){var col=span.closest(".col");if(col)found[label]=col}
  });
  return found;
}
function patchSpools(card){
  var cols=spoolColumns(card);
  data.spools.forEach(function(s){
    var col=cols[s.label];if(!col)return;
    var line=col.querySelector(".qidi-left");
    if(s.remaining==null){if(line)line.remove();return}
    if(!line){line=el("div","qidi-left");col.appendChild(line)}
    var width=Math.max(0,Math.min(1,s.remaining/s.weight))*100;
    var html=g(s.remaining)+'<i><b style="width:'+width.toFixed(0)+'%"></b></i>';
    if(line.dataset.html!==html){line.innerHTML=html;line.dataset.html=html}
    line.className="qidi-left"+(s.remaining<50?" low":"");
  });
}
function problemOf(label){
  for(var i=0;i<data.problems.length;i++)if(data.problems[i].label===label)return data.problems[i];
  return null;
}
function drawCard(card){
  var box=document.getElementById("qidi-plastic-card");
  var root=card.parentElement;
  if(!box){box=el("div","v-card v-sheet "+(document.querySelector(".theme--light")&&!document.querySelector(".theme--dark")?"theme--light":"theme--dark"));box.id="qidi-plastic-card"}
  if(box.previousElementSibling!==card)root.insertBefore(box,card.nextSibling);
  var key=JSON.stringify(data);if(box.dataset.key===key)return;box.dataset.key=key;
  box.textContent="";
  var head=el("div","qh");head.innerHTML=ICON_SPOOL;head.appendChild(el("span",null,"Остаток пластика"));
  if(data.file)head.appendChild(el("span","qf",(data.printing?"Печатается: ":"Последний файл: ")+data.file.split("/").pop()));
  box.appendChild(head);
  var body=el("div","qb");box.appendChild(body);
  var grid=el("div","qg");body.appendChild(grid);
  data.spools.forEach(function(s){
    var p=problemOf(s.label);
    var t=el("div","qt"+(p?" "+p.verdict:"")+(s.remaining!=null&&s.remaining<50?" low":""));
    t.innerHTML=spoolSvg(s.color);
    var n=el("div","qn");n.appendChild(el("div","ql",s.label));n.appendChild(el("div","qm",s.material||"Без метки"));
    n.appendChild(el("div","qv",s.remaining==null?"—":g(s.remaining)));
    var bar=el("div","qbar");var fill=el("b");fill.style.width=(s.remaining==null?0:Math.max(0,Math.min(1,s.remaining/s.weight))*100).toFixed(0)+"%";bar.appendChild(fill);n.appendChild(bar);
    t.appendChild(n);grid.appendChild(t);
  });
  var w;
  if(data.problems.length){
    var short=data.problems.filter(function(p){return p.verdict==="short"});
    var kind=short.length?"short":data.problems[0].verdict;
    w=el("div","qw "+kind);w.innerHTML=ICON_WARN;
    var tx=el("div");tx.appendChild(el("div","t",short.length?"Не хватит пластика":"Пластика может не хватить"));
    tx.appendChild(el("div","d",data.problems.map(function(p){
      return p.verdict==="unknown"?p.label+": остаток неизвестен":p.label+": нужно "+g(p.need)+", осталось "+g(p.remaining);
    }).join(" · ")+(short.length?". Подготовьте новую катушку.":"")));
    w.appendChild(tx);body.appendChild(w);
  }else if(data.file&&!data.message){
    w=el("div","qw ok");w.innerHTML=ICON_OK;var ok=el("div");ok.appendChild(el("div","t","Пластика хватает"));
    ok.appendChild(el("div","d",data.printing?"До конца этой печати":"На последний загруженный файл"));w.appendChild(ok);body.appendChild(w);
  }
}
function apply(){
  if(!data)return;
  var card=boxCard();if(!card)return;
  try{patchSpools(card);drawCard(card)}catch(e){}
}
async function load(){
  if(busy)return;busy=true;
  try{var res=await fetch(API);if(res.ok){data=await res.json();apply()}}catch(e){}
  finally{busy=false}
}
load();setInterval(load,10000);
// Fluidd redraws its cards: put the additions back after each change.
var pending=false;
new MutationObserver(function(){if(pending)return;pending=true;setTimeout(function(){pending=false;apply()},300)})
  .observe(document.documentElement,{childList:true,subtree:true});
})();
"""
