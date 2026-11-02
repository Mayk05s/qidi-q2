"""The plastic left on the spools, shown in Fluidd itself: grams under each spool of the Box card,
and a card under it that warns when the running or the newest print will not last.

The Fluidd page loads SCRIPT from the panel (see qidi_panel_link.js); the data comes from
/api/fluidd, which the panel answers to the printer's own page.

A spool without a tag is picked from the user's own list of plastics: a click on its tile opens the
list. The list is my_plastics.json next to printer.cfg, the choice goes to bambu_spool_usage as
spool_pick_<slot>.json there; both through Moonraker on the Fluidd page's own address, which the
panel's server does not take writes from.
"""


def card_data(slots, check):
    """What the Fluidd script needs, from Printer.slots() and PrintCheck.check()."""
    spools = []
    for slot in slots["slots"]:
        spool = slot.get("spool") or {}
        tag = slot.get("tag") or {}
        # A spool without a tag gets its plastic from the user's list, see SCRIPT.
        tagless = not tag or bool(tag.get("empty"))
        spools.append({"index": slot.get("index"), "label": slot["label"], "color": slot.get("color"),
                       "material": tag.get("detail") or spool.get("name"), "remaining": spool.get("remaining"),
                       "weight": tag.get("weight") or spool.get("full") or 1000, "tagless": tagless})
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
"@media (max-width:700px){#qidi-plastic-card .qg{grid-template-columns:repeat(2,minmax(0,1fr))}}",
"#qidi-plastic-card .qt.pick{cursor:pointer}#qidi-plastic-card .qt.pick:hover{border-color:rgba(127,127,127,.55)}",
"#qidi-plastic-card .qpk{font-size:12px;font-weight:600;color:#7fa8f5;margin-top:3px}",
"#qidi-pick{position:fixed;inset:0;z-index:300;display:flex;align-items:center;justify-content:center;padding:16px;background:rgba(0,0,0,.55);font-family:inherit}",
"#qidi-pick .qp{width:100%;max-width:440px;max-height:calc(100vh - 32px);display:flex;flex-direction:column;border-radius:8px;background:#262626;color:#e8e8e3;box-shadow:0 12px 40px rgba(0,0,0,.5);overflow:hidden}",
"#qidi-pick.light .qp{background:#fff;color:#1d1d1b}",
"#qidi-pick .qph{display:flex;align-items:center;gap:10px;padding:14px 16px;font-size:16px;font-weight:500;border-bottom:1px solid rgba(127,127,127,.22)}",
"#qidi-pick .qph span{flex:1}#qidi-pick button{font:inherit;color:inherit;cursor:pointer}",
"#qidi-pick .qx{border:0;background:none;font-size:22px;line-height:1;opacity:.7;padding:0 4px}",
"#qidi-pick .qpb{padding:12px 16px;overflow:auto;display:flex;flex-direction:column;gap:8px}",
"#qidi-pick .qr{display:flex;align-items:center;gap:10px;width:100%;padding:8px 10px;border-radius:8px;border:1px solid rgba(127,127,127,.25);background:rgba(127,127,127,.06);text-align:left}",
"#qidi-pick .qr.on{border-color:#7fa8f5;background:rgba(127,168,245,.14)}",
"#qidi-pick .qd{flex:none;width:22px;height:22px;border-radius:50%;border:1px solid rgba(127,127,127,.5)}",
"#qidi-pick .qr .qn{flex:1;min-width:0}#qidi-pick .qr .t{font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
"#qidi-pick .qr .s{font-size:12px;opacity:.65}",
"#qidi-pick .qdel{flex:none;border:0;background:none;opacity:.55;font-size:12px;padding:4px 6px;border-radius:6px}#qidi-pick .qdel.sure{opacity:1;color:#ff7b7b;background:rgba(255,82,82,.14)}",
"#qidi-pick .qempty{font-size:13px;opacity:.7}",
"#qidi-pick .qbtn{padding:8px 14px;border-radius:6px;border:1px solid rgba(127,127,127,.35);background:rgba(127,127,127,.1);font-size:13px;font-weight:600;letter-spacing:.02em}",
"#qidi-pick .qbtn.main{border-color:#7fa8f5;background:#7fa8f5;color:#10131a}#qidi-pick .qbtn:disabled{opacity:.45;cursor:default}",
"#qidi-pick label{display:flex;flex-direction:column;gap:4px;font-size:12px;opacity:.85}",
"#qidi-pick input,#qidi-pick select{font:inherit;font-size:14px;color:inherit;background:rgba(127,127,127,.1);border:1px solid rgba(127,127,127,.35);border-radius:6px;padding:7px 9px}",
"#qidi-pick select option{color:#1d1d1b}",
"#qidi-pick .qsw{display:flex;flex-wrap:wrap;gap:6px}#qidi-pick .qsw button{width:26px;height:26px;border-radius:50%;border:2px solid transparent;padding:0;box-shadow:inset 0 0 0 1px rgba(127,127,127,.5)}",
"#qidi-pick .qsw button.on{border-color:#7fa8f5}#qidi-pick .qsw input[type=color]{width:30px;height:28px;padding:0 2px}",
"#qidi-pick .qrow{display:flex;gap:8px;align-items:flex-end}#qidi-pick .qrow label{flex:1}",
"#qidi-pick .qpf{display:flex;gap:8px;align-items:flex-end;padding:12px 16px;border-top:1px solid rgba(127,127,127,.22)}#qidi-pick .qpf label{flex:1}",
"#qidi-pick .qmsg{font-size:12px;padding:0 16px 12px;opacity:.85}#qidi-pick .qmsg.err{color:#ff7b7b;opacity:1}#qidi-pick .qmsg:empty{display:none}"
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
// The user's plastics and the pick for a spool without a tag: files next to printer.cfg, written
// through Moonraker at the Fluidd page's own address.
var FILES=location.origin+"/server/files/";
async function readList(){
  var res=await fetch(FILES+"config/my_plastics.json",{cache:"no-store"});
  if(res.status===404)return [];
  if(!res.ok)throw new Error("Список не прочитался");
  var list=await res.json();
  return Array.isArray(list)?list.filter(function(p){return p&&p.name&&/^QF[A-Z0-9]{2,4}$/.test(p.filament)&&/^[0-9A-F]{6}$/.test(p.color)}):[];
}
async function upload(name,value){
  var form=new FormData();form.append("root","config");
  form.append("file",new Blob([JSON.stringify(value)],{type:"application/json"}),name);
  var res=await fetch(FILES+"upload",{method:"POST",body:form});
  if(!res.ok)throw new Error("Принтер не принял файл");
}
async function materialList(){
  var res=await fetch(API.replace(/fluidd$/,"codes"));
  var codes=await res.json(),my=codes.my_materials||{ids:[],types:[]};
  var all=codes.slot_materials||[];
  var mine=all.filter(function(m){return my.ids.indexOf(m.id)>=0&&(!my.types.length||my.types.indexOf(m.type)>=0)});
  var colors=(codes.my_colors||[]).concat((codes.colors||[]).map(function(c){return c.hex.replace("#","").toUpperCase()}));
  return {materials:mine.length?mine:all,all:all,colors:colors.filter(function(c,i){return colors.indexOf(c)===i})};
}
function openPick(spool){
  var old=document.getElementById("qidi-pick");if(old)old.remove();
  var light=!!document.querySelector(".theme--light")&&!document.querySelector(".theme--dark");
  var bg=el("div",light?"light":null);bg.id="qidi-pick";
  var box=el("div","qp");bg.appendChild(box);
  var head=el("div","qph");head.appendChild(el("span",null,"Пластик для "+spool.label));
  var close=el("button","qx","×");close.title="Закрыть";head.appendChild(close);box.appendChild(head);
  var body=el("div","qpb");box.appendChild(body);
  var foot=el("div","qpf");box.appendChild(foot);
  var msg=el("div","qmsg");box.appendChild(msg);
  var list=[],codes={materials:[],all:[],colors:[]},chosen=null,deleting=null;
  function done(){bg.remove();document.removeEventListener("keydown",onKey)}
  function onKey(e){if(e.key==="Escape")done()}
  function say(text,bad){msg.textContent=text||"";msg.className="qmsg"+(bad?" err":"")}
  function materialName(id){for(var i=0;i<codes.all.length;i++)if(codes.all[i].id===id)return codes.all[i].name;return id}
  close.onclick=done;bg.onclick=function(e){if(e.target===bg)done()};document.addEventListener("keydown",onKey);
  function showList(){
    body.textContent="";foot.textContent="";
    if(!list.length)body.appendChild(el("div","qempty","Своих пластиков пока нет. Добавь пластик один раз, дальше он выбирается из списка."));
    list.forEach(function(p){
      var row=el("button","qr"+(chosen===p?" on":""));
      var dot=el("span","qd");dot.style.background="#"+p.color;row.appendChild(dot);
      var n=el("span","qn");n.appendChild(el("div","t",p.name));n.appendChild(el("div","s",materialName(p.filament)+" · "+p.grams+" г"));row.appendChild(n);
      var del=el("span","qdel"+(deleting===p?" sure":""),deleting===p?"Удалить?":"✕");del.title="Убрать из списка";row.appendChild(del);
      row.onclick=async function(e){
        if(e.target===del){
          if(deleting!==p){deleting=p;return showList()}
          var rest=list.filter(function(x){return x!==p});
          try{await upload("my_plastics.json",rest);list=rest;if(chosen===p)chosen=null;deleting=null;say("")}catch(err){say(err.message,true)}
          return showList();
        }
        chosen=p;deleting=null;showList();
      };
      body.appendChild(row);
    });
    var add=el("button","qbtn","+ Добавить пластик");add.onclick=function(){showForm()};body.appendChild(add);
    if(!chosen)return;
    var grams=el("input");grams.type="number";grams.min=0;grams.max=5000;grams.value=chosen.grams;
    var label=el("label",null,"Сколько на катушке, г");label.appendChild(grams);foot.appendChild(label);
    var put=el("button","qbtn main","Поставить в "+spool.label);foot.appendChild(put);
    put.onclick=async function(){
      var value=parseInt(grams.value,10);
      if(!(value>=0&&value<=5000)){say("Вес от 0 до 5000 г",true);return}
      put.disabled=true;
      try{
        await upload("spool_pick_"+spool.index+".json",{name:chosen.name,filament:chosen.filament,color:chosen.color,grams:value});
        say(data&&data.printing?"Готово. Остаток считается сразу, материал и цвет слота поменяются после печати":"Готово");
        setTimeout(load,2500);setTimeout(done,data&&data.printing?2500:900);
      }catch(err){say(err.message,true);put.disabled=false}
    };
  }
  function showForm(){
    body.textContent="";foot.textContent="";say("");
    var name=el("input");name.maxLength=40;name.placeholder="Например, eSun PLA+ чёрный";
    var l1=el("label",null,"Название");l1.appendChild(name);body.appendChild(l1);
    var select=el("select");codes.materials.forEach(function(m){var o=el("option",null,m.name);o.value=m.id;select.appendChild(o)});
    var l2=el("label",null,"Материал в принтере");l2.appendChild(select);body.appendChild(l2);
    var color=codes.colors[0]||"FAFAFA";
    var l3=el("label",null,"Цвет");var sw=el("div","qsw");l3.appendChild(sw);body.appendChild(l3);
    var custom=el("input");custom.type="color";custom.title="Свой цвет";
    function paint(){
      sw.textContent="";
      codes.colors.forEach(function(c){var b=el("button",c===color?"on":null);b.type="button";b.style.background="#"+c;b.title="#"+c;b.onclick=function(){color=c;paint()};sw.appendChild(b)});
      custom.value="#"+color.toLowerCase();sw.appendChild(custom);
    }
    custom.oninput=function(){color=custom.value.replace("#","").toUpperCase();paint()};
    paint();
    var weight=el("input");weight.type="number";weight.min=1;weight.max=5000;weight.value=1000;
    var l4=el("label",null,"Вес полной катушки, г");l4.appendChild(weight);body.appendChild(l4);
    var cancel=el("button","qbtn","Отмена");cancel.onclick=function(){showList()};foot.appendChild(cancel);
    var save=el("button","qbtn main","Сохранить");foot.appendChild(save);
    save.onclick=async function(){
      var p={id:Date.now().toString(36),name:name.value.trim().replace(/\s+/g," "),filament:select.value,color:color,grams:parseInt(weight.value,10)};
      if(!p.name){say("Впиши название",true);return}
      if(!(p.grams>0&&p.grams<=5000)){say("Вес от 1 до 5000 г",true);return}
      save.disabled=true;
      try{await upload("my_plastics.json",list.concat([p]));list=list.concat([p]);chosen=p;say("");showList()}
      catch(err){say(err.message,true);save.disabled=false}
    };
    name.focus();
  }
  document.body.appendChild(bg);
  say("Загружаю список…");
  Promise.all([readList(),materialList()]).then(function(r){list=r[0];codes=r[1];say("");showList()})
    .catch(function(err){say(err.message||"Список не загрузился",true)});
}
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
  // A spool's block is the largest one around its label that holds no other spool label:
  // Fluidd's grid columns differ with the window width, so its classes are not relied on.
  var LABEL=/^[1-4][A-D]$/,spans=[];
  // Fluidd draws the label in a button or in the slot icon, depending on its version and the
  // slot state: any element whose own text is the label counts.
  card.querySelectorAll("*").forEach(function(node){
    if(LABEL.test(node.textContent.trim())&&!Array.prototype.some.call(node.children,function(c){return LABEL.test(c.textContent.trim())}))spans.push(node);
  });
  function labelsIn(node){var seen={};spans.forEach(function(s){if(node.contains(s))seen[s.textContent.trim()]=1});return Object.keys(seen).length}
  // Buttons such as Загрузить and Выгрузить belong to the whole Box, never to one spool.
  function actionsIn(node){return Array.prototype.some.call(node.querySelectorAll("button"),function(btn){var text=btn.textContent.trim();return text&&!LABEL.test(text)})}
  // With a single label every block around it holds no other label, and the line would go to the
  // bottom of the whole card.
  if(labelsIn(card)<2)return {};
  var found={};
  spans.forEach(function(span){
    var start=span.closest("button")||span,block=start;
    while(block.parentElement&&block.parentElement!==card&&labelsIn(block.parentElement)===1&&!actionsIn(block.parentElement))block=block.parentElement;
    if(block!==start)found[span.textContent.trim()]=block;
  });
  return found;
}
function patchSpools(card){
  var cols=spoolColumns(card),blocks=Object.keys(cols).map(function(k){return cols[k]});
  // A line left in a block that is no longer a spool's, e.g. after the window was resized.
  card.querySelectorAll(".qidi-left").forEach(function(line){if(blocks.indexOf(line.parentElement)<0)line.remove()});
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
    if(s.tagless&&s.remaining==null)n.appendChild(el("div","qpk","Выбрать пластик"));
    else n.appendChild(el("div","qv",s.remaining==null?"—":g(s.remaining)));
    var bar=el("div","qbar");var fill=el("b");fill.style.width=(s.remaining==null?0:Math.max(0,Math.min(1,s.remaining/s.weight))*100).toFixed(0)+"%";bar.appendChild(fill);n.appendChild(bar);
    if(s.tagless){t.classList.add("pick");t.title="Катушка без метки: выбрать пластик из своего списка";t.onclick=function(){openPick(s)}}
    t.appendChild(n);grid.appendChild(t);
  });
  var w;
  if(data.problems.length){
    var short=data.problems.filter(function(p){return p.verdict==="short"});
    var kind=short.length?"short":data.problems[0].verdict;
    w=el("div","qw "+kind);w.innerHTML=ICON_WARN;
    var tx=el("div");tx.appendChild(el("div","t",short.length?"Не хватит пластика":"Пластика может не хватить"));
    tx.appendChild(el("div","d",data.problems.map(function(p){
      return p.verdict==="unknown"?p.label+": остаток неизвестен, выбери пластик на катушке":p.label+": нужно "+g(p.need)+", осталось "+g(p.remaining);
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
