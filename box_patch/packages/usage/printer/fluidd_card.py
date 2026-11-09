"""The plastic left on the spools, shown in Fluidd itself: grams under each spool of the Box card,
and a card under it that warns when the running or the newest print will not last.

The Fluidd page loads SCRIPT from the panel (see qidi_panel_link.js); the data comes from
/api/fluidd, which the panel answers to the printer's own page.

A click on a spool's tile opens the panel's page as /?pick=<slot> in a frame: the remainder of a
counted spool can be corrected there (spool_set_<slot>.txt), and a spool without a tag gets its
plastic from the user's own list with the panel's own form. The panel keeps the list and hands the
choice to bambu_spool_usage as spool_pick_<slot>.json next to printer.cfg. The Остатки button opens
/?spools: every counted spool with its remainder, the standing ones correctable, and the plastics.
A slot the printer sees empty shows so, without a count and without a click.
Each spool is a compact tile in one row, the rack first: the spool with a ring of what is left, the
grams, «осталось из 1000 г» and the plastic. While the window is open the Fluidd page does not scroll.
"""


# Tag details that are no plastic name; the panel has the same list (GENERIC_DETAILS).
GENERIC_DETAILS = ("QIDI", "Своя метка")


def plastic_name(tag, spool, filament, names):
    """Maker and plastic: from the tag (Bambu Lab PLA Matte), the plastic picked for a spool without
    a tag, or the printer's profile for a factory QIDI tag."""
    if tag.get("unread"):
        return tag.get("detail")
    # A factory QIDI tag and our tag without a name hold only codes: the profile names the plastic.
    if tag.get("detail") in GENERIC_DETAILS:
        return names.get(filament, filament)
    if tag.get("detail") and not tag.get("empty"):
        brand = tag.get("brand")
        return tag["detail"] if not brand or tag["detail"].startswith(brand) else brand + " " + tag["detail"]
    if spool.get("name"):
        return spool["name"]
    if tag.get("stock"):
        return names.get(filament, filament)
    return None


def card_data(slots, check, names=None):
    """What the Fluidd script needs, from Printer.slots() and PrintCheck.check(); names: material
    code to profile name."""
    names = names or {}
    printing = bool(check.get("printing"))
    # What the print takes from each spool: all of it before the print, the rest during it. The
    # bar shows it in its own colour, see SCRIPT.
    needs = {row["label"]: row["left_to_print"] if printing else row["need"]
             for row in check.get("rows") or [] if row.get("label")}
    spools = []
    # The rack spool first, as in Fluidd's own Box card, when the usage module counts it; then the Box.
    rack = slots.get("rack")
    for slot in ([rack] if rack and rack.get("spool") else []) + slots["slots"]:
        spool = slot.get("spool") or {}
        tag = slot.get("tag") or {}
        # A spool without a tag gets its plastic from the user's list, see SCRIPT.
        tagless = not tag or bool(tag.get("empty"))
        empty = bool(slot.get("empty"))
        entry = {"index": slot.get("index"), "label": slot["label"], "color": None if empty else slot.get("color"),
                 "material": "Слот пуст" if empty else plastic_name(tag, spool, slot.get("filament"), names),
                 "remaining": spool.get("remaining"),
                 # The counted full spool first: a tag says 1000 g even when the spool was set to 250 g.
                 "weight": spool.get("full") or tag.get("weight") or 1000, "tagless": tagless and not empty,
                 "empty": empty}
        if needs.get(slot["label"]):
            entry["need"] = round(needs[slot["label"]], 1)
        spools.append(entry)
    rows = [{"label": row["label"], "need": row["left_to_print"] if printing else row["need"],
             "remaining": row["remaining"], "verdict": row["verdict"]}
            for row in check.get("rows") or [] if row["verdict"] != "ok"]
    return {"spools": spools, "file": check.get("file"), "printing": printing,
            "problems": rows, "message": check.get("message"), "pending": bool(check.get("pending"))}


SCRIPT = r"""(function(){
var PANEL=location.protocol+"//"+location.hostname+":18770",API=PANEL+"/api/fluidd";
var data=null,busy=false;
var style=document.createElement("style");
style.textContent=[
".qidi-left{display:flex;width:max-content;flex-direction:column;align-items:center;gap:3px;margin:6px auto 0;padding:3px 8px;border-radius:10px;background:rgba(127,127,127,.14);font-size:12px;font-weight:500;font-variant-numeric:tabular-nums}",
".qidi-left i{display:flex;width:40px;height:3px;border-radius:2px;background:rgba(127,127,127,.3);overflow:hidden}",
".qidi-left i b{display:block;height:100%;background:#4caf50}",
".qidi-left.low{color:#ff6e6e;background:rgba(255,82,82,.14)}.qidi-left.low i b{background:#ff5252}",
".qidi-left i b.use{background:#7fa8f5}",
"#qidi-plastic-card .qt.empty{opacity:.55;border-style:dashed}",
"#qidi-plastic-card{margin-bottom:12px;border-radius:4px;overflow:hidden}",
"#qidi-plastic-card .qh{display:flex;align-items:center;gap:10px;padding:12px 16px;font-size:16px;font-weight:500;border-bottom:1px solid rgba(127,127,127,.18)}",
"#qidi-plastic-card .qh svg{width:20px;height:20px;opacity:.85}",
"#qidi-plastic-card .qall{margin-left:auto;font:inherit;font-size:12px;font-weight:600;letter-spacing:.03em;padding:5px 12px;border-radius:14px;border:1px solid rgba(127,168,245,.5);background:rgba(127,168,245,.12);color:#9fbaf5;cursor:pointer}",
"#qidi-plastic-card .qall:hover{background:rgba(127,168,245,.22)}",
"#qidi-plastic-card .qb{padding:14px 16px 16px}",
"#qidi-plastic-card .qlay{display:grid;grid-template-columns:150px minmax(0,1fr);gap:10px}",
"#qidi-plastic-card .qlay.norack{grid-template-columns:minmax(0,1fr)}",
"#qidi-plastic-card .qg{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}",
"#qidi-plastic-card .qrack .qt{height:100%;box-sizing:border-box;flex-direction:column;align-items:flex-start;justify-content:center}",
"@media (max-width:560px){#qidi-plastic-card .qlay{grid-template-columns:minmax(0,1fr)}#qidi-plastic-card .qrack .qt{flex-direction:row;align-items:center}}",
"#qidi-plastic-card .qt{position:relative;display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;border:1px solid rgba(127,127,127,.22);background:rgba(127,127,127,.06);min-width:0}",
"#qidi-plastic-card .qt.short{border-color:#ff5252;background:rgba(255,82,82,.08)}#qidi-plastic-card .qt.risk{border-color:#ffb74d;background:rgba(255,183,77,.08)}",
"#qidi-plastic-card .qt svg{flex:none;width:34px;height:34px}",
"#qidi-plastic-card .qn{min-width:0;flex:1;width:100%}#qidi-plastic-card .ql{font-weight:600;font-size:14px}",
"#qidi-plastic-card .qtop{display:flex;align-items:baseline;gap:8px}#qidi-plastic-card .qtop .qv{margin:0 0 0 auto}",
"#qidi-plastic-card .qm{font-size:12px;opacity:.75;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
"#qidi-plastic-card .qv{font-size:16px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:2px}",
"#qidi-plastic-card .qbar{display:flex;height:4px;border-radius:2px;background:rgba(127,127,127,.25);overflow:hidden;margin-top:5px}",
"#qidi-plastic-card .qbar.plan{height:6px;border-radius:3px}",
"#qidi-plastic-card .qbar b{display:block;height:100%;background:#4caf50}",
"#qidi-plastic-card .qt.short .qbar b,#qidi-plastic-card .low .qbar b{background:#ff5252}#qidi-plastic-card .qt.risk .qbar b{background:#ffb74d}",
"#qidi-plastic-card .qt .qbar b.use{background:#7fa8f5}#qidi-plastic-card .qt.short .qbar b.use{background:repeating-linear-gradient(135deg,#ff5252 0 4px,#b33a3a 4px 8px)}",
"#qidi-plastic-card .qu{display:flex;gap:6px;font-size:11px;margin-top:4px;opacity:.85;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-variant-numeric:tabular-nums}",
"#qidi-plastic-card .qu .use{color:#9fbaf5}#qidi-plastic-card .qu .bad{color:#ff7b7b;font-weight:600}",
"#qidi-plastic-card .qw{display:flex;gap:12px;align-items:flex-start;margin-top:14px;padding:12px 14px;border-radius:8px}",
"#qidi-plastic-card .qw svg{flex:none;width:22px;height:22px;margin-top:1px}",
"#qidi-plastic-card .qw .t{font-weight:600;font-size:14px}#qidi-plastic-card .qw .d{font-size:13px;opacity:.9;margin-top:2px}",
"#qidi-plastic-card .qw.short{background:rgba(255,82,82,.12);color:#ff7b7b}",
"#qidi-plastic-card .qw.risk,#qidi-plastic-card .qw.unknown{background:rgba(255,183,77,.12);color:#ffc370}",
"#qidi-plastic-card .qw.ok{background:rgba(76,175,80,.12);color:#6fcf73}",
"#qidi-plastic-card .qw.wait{background:rgba(127,168,245,.1);color:#9fbaf5}",
"#qidi-plastic-card .qrow{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,4.3fr);gap:10px;align-items:stretch}",
"#qidi-plastic-card .qrow.norack{grid-template-columns:minmax(0,1fr)}",
"#qidi-plastic-card .qboxw{display:flex;flex-direction:column;min-width:0}",
"#qidi-plastic-card .qcap{height:18px;padding-left:4px;font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#7fa8f5}",
"#qidi-plastic-card .qbox{flex:1;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-radius:12px;border:1px solid rgba(127,168,245,.5);background:rgba(127,168,245,.08);overflow:hidden}",
"#qidi-plastic-card .qbox .qc{border:0;border-radius:0;background:transparent}",
"#qidi-plastic-card .qbox .qc+.qc{box-shadow:inset 1px 0 0 rgba(127,168,245,.25)}",
"#qidi-plastic-card .qbox .qc.pick:hover{background:rgba(127,168,245,.1)}",
"#qidi-plastic-card .qbox .qc.short{background:rgba(255,82,82,.12)}#qidi-plastic-card .qbox .qc.risk{background:rgba(255,183,77,.12)}",
"#qidi-plastic-card .qbox .qc.empty{opacity:.45}",
"#qidi-plastic-card .qrack{display:flex;padding-top:18px}#qidi-plastic-card .qrack .qc{flex:1}",
"#qidi-plastic-card .qc{position:relative;display:flex;flex-direction:column;align-items:center;gap:2px;padding:10px 8px 10px;border-radius:10px;border:1px solid rgba(127,127,127,.22);background:rgba(127,127,127,.06);min-width:0;text-align:center}",
"#qidi-plastic-card .qc .ql{position:absolute;top:7px;left:8px;font-size:12px;font-weight:700;padding:1px 6px;border-radius:6px;background:rgba(127,127,127,.18)}",
"#qidi-plastic-card .qc svg.qring{width:100%;max-width:84px;height:auto;aspect-ratio:1;margin:6px 0 2px}",
"@media (max-width:640px){#qidi-plastic-card .qc .qof{display:none}#qidi-plastic-card .qc{padding:20px 4px 8px}#qidi-plastic-card .qc .qv{font-size:17px!important}}",
"#qidi-plastic-card .qc .qv{font-size:22px;font-weight:700;margin:0;line-height:1.1;font-variant-numeric:tabular-nums}#qidi-plastic-card .qc .qv span{font-size:13px;font-weight:600;margin-left:2px}",
"#qidi-plastic-card .qc .qof{font-size:11px;opacity:.65;font-variant-numeric:tabular-nums}",
"#qidi-plastic-card .qc .qm{max-width:100%;margin-top:3px;font-size:12px;line-height:1.3;opacity:.85;white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}",
"#qidi-plastic-card .qc .qu{justify-content:center;margin-top:2px}",
"#qidi-plastic-card .qc.short{border-color:#ff5252;background:rgba(255,82,82,.08)}#qidi-plastic-card .qc.risk{border-color:#ffb74d;background:rgba(255,183,77,.08)}",
"#qidi-plastic-card .qc.empty{opacity:.55;border-style:dashed}",
"#qidi-plastic-card .qc.pick{cursor:pointer}#qidi-plastic-card .qc.pick:hover{border-color:rgba(127,127,127,.55)}",
"#qidi-plastic-card .qt.pick{cursor:pointer}#qidi-plastic-card .qt.pick:hover{border-color:rgba(127,127,127,.55)}",
"#qidi-plastic-card .qpk{font-size:12px;font-weight:600;color:#7fa8f5;margin-top:3px}",
"#qidi-pick{position:fixed;inset:0;z-index:300;display:flex;align-items:center;justify-content:center;padding:16px;background:rgba(0,0,0,.55);overscroll-behavior:contain;touch-action:none}",
"html.qidi-locked,html.qidi-locked body{overflow:hidden!important}",
"#qidi-pick .qp{position:relative;width:100%;max-width:560px;height:min(760px,calc(100vh - 32px));border-radius:12px;overflow:hidden;box-shadow:0 12px 40px rgba(0,0,0,.5);background:#21211f}",
"#qidi-pick iframe{width:100%;height:100%;border:0;display:block}",
"#qidi-pick .qx{position:absolute;top:8px;right:10px;z-index:1;width:32px;height:32px;border:0;border-radius:50%;background:rgba(127,127,127,.25);color:#fff;font-size:20px;line-height:1;cursor:pointer}"
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
// The spool with a ring of what is left: green what stays after the print, blue what the print
// takes, red when short or under 50 g; the panel's spool window shows the same.
function ringSvg(s){
  var R=44,C=2*Math.PI*R,w=s.weight||1000,rem=Math.max(0,s.remaining||0),use=Math.min(s.need||0,rem);
  var c=s.color?"#"+s.color:"#555",short=s.need&&s.need>rem,low=rem<50;
  function part(v){return Math.max(0,Math.min(1,v/w))*C}
  var stay=part(rem-use),take=part(use),html='<svg class="qring" viewBox="0 0 100 100"><circle cx="50" cy="50" r="'+R+'" fill="none" stroke="rgba(127,127,127,.25)" stroke-width="7"/>';
  if(s.remaining!=null){
    html+='<g transform="rotate(-90 50 50)" fill="none" stroke-width="7" stroke-linecap="round">';
    if(short)html+='<circle cx="50" cy="50" r="'+R+'" stroke="#ff5252" stroke-dasharray="'+part(rem)+' '+C+'"/>';
    else{
      if(stay>0)html+='<circle cx="50" cy="50" r="'+R+'" stroke="'+(low?"#ff5252":"#4caf50")+'" stroke-dasharray="'+stay+' '+C+'"/>';
      if(take>0)html+='<circle cx="50" cy="50" r="'+R+'" stroke="#7fa8f5" stroke-dasharray="'+take+' '+C+'" stroke-dashoffset="'+(-stay)+'"/>';
    }
    html+='</g>';
  }
  return html+'<circle cx="50" cy="50" r="33" fill="#3a3a38" stroke="#585853"/><circle cx="50" cy="50" r="27" fill="'+c+'"/>'
    +'<circle cx="50" cy="50" r="21.5" fill="none" stroke="#000" stroke-opacity=".12"/><circle cx="50" cy="50" r="16.5" fill="none" stroke="#000" stroke-opacity=".12"/>'
    +'<circle cx="50" cy="50" r="12" fill="#9a9a95"/><circle cx="50" cy="50" r="4.5" fill="#2a2a28"/></svg>';
}
function g(n){return Math.round(n)+" г"}
// The bar: what stays after the print in green, what the print takes in its own colour, the rest
// of a full spool empty. Short of plastic: the whole remainder goes, striped red.
function barParts(s){
  var w=s.weight||1000,rem=Math.max(0,s.remaining||0),use=Math.min(s.need||0,rem);
  function pct(v){return (Math.max(0,Math.min(1,v/w))*100).toFixed(1)+"%"}
  var html='<b style="width:'+pct(rem-use)+'"></b>';
  if(use>0)html+='<b class="use" style="width:'+pct(use)+';min-width:2px"></b>';
  return html;
}
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
// A spool without a tag gets its plastic in the panel's own form (maker and name with the lists,
// material by type, the palette), opened in a frame; the panel says when it is done.
function openPick(spool){openFrame("/?pick="+spool.index,(spool.tagless?"Пластик для ":"Остаток в ")+spool.label)}
function openFrame(path,title){
  var old=document.getElementById("qidi-pick");if(old)old.remove();
  var bg=el("div");bg.id="qidi-pick";
  var box=el("div","qp");bg.appendChild(box);
  var close=el("button","qx","×");close.title="Закрыть";box.appendChild(close);
  var frame=document.createElement("iframe");frame.title=title;
  frame.src=PANEL+path;box.appendChild(frame);
  // The Fluidd page under the window stays put: its scroll is off while the window is open.
  var y=window.scrollY;document.documentElement.classList.add("qidi-locked");
  function done(){bg.remove();document.documentElement.classList.remove("qidi-locked");window.scrollTo(0,y);
    window.removeEventListener("message",onMessage);document.removeEventListener("keydown",onKey);setTimeout(load,300)}
  function onMessage(e){if(e.source===frame.contentWindow&&e.data&&e.data.qidiPick==="done"){done();setTimeout(load,1500)}}
  function onKey(e){if(e.key==="Escape")done()}
  close.onclick=done;bg.onclick=function(e){if(e.target===bg)done()};
  // A wheel or a swipe on the dark backdrop scrolls nothing; inside the frame the panel scrolls itself.
  function still(e){if(e.target===bg||e.target===box)e.preventDefault()}
  bg.addEventListener("wheel",still,{passive:false});bg.addEventListener("touchmove",still,{passive:false});
  window.addEventListener("message",onMessage);document.addEventListener("keydown",onKey);
  document.body.appendChild(bg);
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
  var LABEL=/^([1-4][A-D]|Rack)$/,spans=[];
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
    var html=g(s.remaining)+'<i>'+barParts(s)+'</i>';
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
  var all=el("button","qall","Остатки");all.type="button";all.title="Все катушки с остатками и свои пластики";
  all.onclick=function(){openFrame("/?spools","Остатки пластика")};head.appendChild(all);
  box.appendChild(head);
  var body=el("div","qb");box.appendChild(body);
  // One row of compact tiles, the rack first: the spool with its ring, the grams, the plastic.
  // The rack on its own, the Box spools in a frame of their own; always one line.
  var rack=data.spools.filter(function(s){return s.label==="Rack"})[0];
  var row=el("div","qrow"+(rack?"":" norack"));body.appendChild(row);
  var rackCol=el("div","qrack"),boxWrap=el("div","qboxw"),boxCol=el("div","qbox");
  boxWrap.appendChild(el("div","qcap","Box"));boxWrap.appendChild(boxCol);
  if(rack)row.appendChild(rackCol);row.appendChild(boxWrap);
  data.spools.forEach(function(s){
    var p=problemOf(s.label);
    var t=el("div","qc"+(p?" "+p.verdict:"")+(s.empty?" empty":""));
    t.appendChild(el("span","ql",s.label));
    var pic=el("div");pic.innerHTML=ringSvg(s);t.appendChild(pic.firstChild);
    if(s.remaining!=null){
      var v=el("div","qv",String(Math.round(s.remaining)));v.appendChild(el("span",null,"г"));t.appendChild(v);
      t.appendChild(el("div","qof","осталось из "+g(s.weight)));
    }else if(!s.empty&&s.tagless)t.appendChild(el("div","qpk","Выбрать пластик"));
    t.appendChild(el("div","qm",s.material||"Без метки"));
    if(s.need&&s.remaining!=null){
      var u=el("div","qu"),left=s.remaining-s.need;
      if(left<0)u.appendChild(el("span","bad","Не хватит "+g(-left)));
      else u.appendChild(el("span","use","уйдёт "+g(s.need)));
      t.appendChild(u);
    }
    // Every spool in place opens the panel's window: the remainder corrected, a plastic picked.
    if(!s.empty&&s.index!=null){t.classList.add("pick");t.title=s.tagless?"Катушка без метки: остаток и пластик из своего списка":"Поправить остаток";t.onclick=function(){openPick(s)}}
    (s===rack?rackCol:boxCol).appendChild(t);
  });
  var file=data.file?data.file.split("/").pop():"";
  var w;
  if(data.problems.length){
    var short=data.problems.filter(function(p){return p.verdict==="short"});
    var kind=short.length?"short":data.problems[0].verdict;
    w=el("div","qw "+kind);w.innerHTML=ICON_WARN;
    var tx=el("div");tx.appendChild(el("div","t",short.length?"Не хватит пластика":"Пластика может не хватить"));
    tx.appendChild(el("div","d",data.problems.map(function(p){
      return p.verdict==="unknown"?p.label+": остаток неизвестен, выбери пластик на катушке":p.label+": нужно "+g(p.need)+", осталось "+g(p.remaining);
    }).join(" · ")+(short.length?". Подготовьте новую катушку.":"")+(file?" ("+file+")":"")));
    w.appendChild(tx);body.appendChild(w);
  }else if(data.pending){
    // A big file is read in the background, about half a minute: the card asks again every 10 s.
    w=el("div","qw wait");w.innerHTML=ICON_SPOOL;var wt=el("div");wt.appendChild(el("div","t","Считаю расход"));
    wt.appendChild(el("div","d",(data.printing?"Печать ":"Последний файл ")+file+": большой файл читается до минуты"));w.appendChild(wt);body.appendChild(w);
  }else if(data.file&&!data.message){
    w=el("div","qw ok");w.innerHTML=ICON_OK;var ok=el("div");ok.appendChild(el("div","t","Пластика хватает"));
    ok.appendChild(el("div","d",(data.printing?"До конца печати ":"На последний файл ")+file));w.appendChild(ok);body.appendChild(w);
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
