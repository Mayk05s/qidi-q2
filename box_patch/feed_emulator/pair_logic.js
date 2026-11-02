const C=630;
function mk(c,a0,b0,ha=true,hb=true){const sp=(n,a,h)=>({n,pos:0,rot:0,ang:a,v:0,stopAt:null,sl:c.slackMin+Math.random()*(c.slackMax-c.slackMin),slip:(c.slipMin||0)+Math.random()*((c.slipMax||0)-(c.slipMin||0)),real:c.inserted||40,out:false,has:h,inserted:false,insAt:null,started:false,tag:null,noTag:false,clearing:false,waiting:false});
return{t:0,c,sp:{A:sp('A',a0,ha),B:sp('B',b0,hb)},next:0,log:[],phase:'search',done:false,cand:null,progress:0,sig:''}}
function run(s,d,v){s.v=d*v;s.stopAt=null}
function stop(sim,s){if(s.v&&s.stopAt===null)s.stopAt=sim.t+sim.c.delay}
function hard(s){s.v=0;s.stopAt=null}
function step(sim,dt){sim.t+=dt;for(const s of Object.values(sim.sp)){if(s.stopAt!==null&&sim.t>=s.stopAt)hard(s);if(!s.v)continue;const d=s.v*dt;s.pos+=d;
if(d>0){const g=Math.min(s.slip,d);s.slip-=g;const m=d-g;s.real+=m;const u=Math.min(s.sl,m);s.sl-=u;s.ang+=(m-u)/C*360;s.rot+=m-u}
else{if(s.slip>0)continue;s.real+=d;s.ang+=d/C*360;s.rot+=d;if(s.real<=0){s.out=true;hard(s)}}}}
function inZone(s,z){const a=(s.ang%360+360)%360;return a<=z/2||a>=360-z/2}
function read(sim){const f=Object.values(sim.sp).filter(s=>s.inserted&&s.has&&inZone(s,sim.c.zone));if(f.length>1)return{k:'col'};if(f.length)return{k:'tag',uid:f[0].n};return{k:'none'}}
function insert(sim,n){const s=sim.sp[n];if(s.inserted)return;s.inserted=true;s.insAt=sim.t;sim.done=false;sim.progress=sim.t;sim.log.push(sim.t.toFixed(1)+'с вставлена '+n)}
function tick(sim){const c=sim.c,say=m=>sim.log.push(sim.t.toFixed(1)+'с '+m),A=sim.sp.A,B=sim.sp.B,L=c.limit,v=c.v,both=[A,B];
const put=(s,uid)=>{s.tag=uid;say('метка '+uid+' записана в '+s.n);for(const o of [A,B])o.solo=null};
for(const s of both)if(s.inserted&&!s.started&&sim.t-s.insAt>=c.startDelay){s.started=true;say(s.n+' начала подачу')}
const r=read(sim),owner=u=>both.find(s=>s.tag===u),open=s=>s.started&&!s.tag&&!s.noTag&&!s.out;
const re=both.find(s=>s.re);if(re){const o=re===A?B:A,base=re.reBase;if(o.v>0)stop(sim,o);
 if(re.out){re.re=0;say('Перечитать '+re.n+': нить вышла, остановлено');sim.done=true;return}
 if(r.k==='col'&&o.tag&&inZone(o,c.zone)){run(o,1,v);return}
 if(r.k==='tag'&&!owner(r.uid)){put(re,r.uid);re.noTag=false;re.re=3}
 if(re.re===1&&(re.pos<=base-C*0.52||re.pos<=c.minPos)){re.re=2;run(re,1,v)}
 else if(re.re===2&&re.pos>=base){re.re=4;run(re,1,v)}
 else if(re.re===4&&re.pos>=base+C*0.52){re.re=5;run(re,-1,v)}
 else if(re.re===5&&re.pos<=base){hard(re);re.re=0;if(!re.tag){re.noTag=true;say('Перечитать '+re.n+': метки нет')}sim.done=true;say('Перечитывание закончено');return}
 else if(re.re===3){if(re.pos>base){if(re.v>=0)run(re,-1,v)}else if(re.pos<base){if(re.v<=0)run(re,1,v)}if(Math.abs(re.pos-base)<15){hard(re);re.re=0;sim.done=true;say('Перечитывание закончено')}}
 return}
for(const s of both)if(s.out&&!s.outSaid){s.outSaid=true;s.half=0;s.clearing=false;s.waiting=false;say('ЗАЩИТА: нить '+s.n+' вышла за датчик, мотор выключен');for(const o of both)o.waiting=false;if(sim.phase!=='search'){sim.phase='search';sim.rock=0}}
if(sim.t-sim.progress>c.watchdog&&!sim.guard){sim.guard=true;say('ЗАЩИТА: нет прогресса '+c.watchdog+' с, остановка');for(const s of both)hard(s);sim.done=true;return}
if(sim.sig!==(sim.sig=JSON.stringify(both.map(s=>[Math.round(s.pos/50),s.tag,s.noTag,s.out,s.half])+sim.phase)))sim.progress=sim.t;
for(const s of both)if(s.started&&s.v>0&&s.pos>=L&&!s.half&&!s.clearing&&!(s.tag&&inZone(s,c.zone)))hard(s);
// a tag already known but standing at the antenna is taken away, its spool has done its job
for(const s of both)if(s.half&&s.tag){s.half=0;hard(s)}
for(const s of both)if(s.clearing&&!inZone(s,c.zone)){s.clearing=false;hard(s)}
if(r.k==='col')for(const s of both)if(s.tag&&inZone(s,c.zone)&&!s.clearing){s.clearing=true;run(s,1,v);const o=s===A?B:A;if(open(o)&&o.v>0){stop(sim,o);o.paused=true;say(o.n+' ждёт, пока '+s.n+' уведёт свою метку')}}
for(const s of both)if(s.paused&&r.k!=='col'){s.paused=false;if(r.k==='tag'&&!owner(r.uid)&&!s.tag)put(s,r.uid)}
if(sim.phase==='search'){
 for(const s of both)if(s.halfPaused&&!s.tag&&!s.out){s.half=s.halfPaused;s.halfPaused=0;hard(s)}else if(s.halfPaused)s.halfPaused=0;
 // correction: a known tag shows up while only the other spool turns
 if(r.k==='tag'&&owner(r.uid)){const o=owner(r.uid),other=o===A?B:A;if(!o.v&&o.stopAt===null&&other.v&&!other.tag&&inZone(o,c.zone)===false){say('метка '+r.uid+' на самом деле на '+other.n);o.tag=null;other.tag=r.uid}}
 const moving=both.filter(s=>open(s)&&s.waiting!==true);
 if(moving.length===2&&(r.k==='col'||(r.k==='tag'&&!owner(r.uid)))){
  for(const s of both)if(s.half){s.halfPaused=s.half;s.half=0;hard(s)}const T=A.pos<=B.pos?A:B,W=T===A?B:A;sim.T=T;sim.W=W;stop(sim,W);W.waiting=true;sim.phase='rock';sim.rock=1;sim.cand=r.k==='tag'?r.uid:null;say((r.k==='col'?'столкновение':'метка '+r.uid)+': '+W.n+' стоп, '+T.n+' крутится');return}
 if(moving.length===1&&r.k==='tag'&&!owner(r.uid)){put(moving[0],r.uid);for(const s of both)if(s.waiting&&!s.tag){s.waiting=false}return}
 for(const s of both)if(open(s)&&s.pos>=L&&!s.v&&s.stopAt===null&&!s.half&&!s.waiting){s.half=1;s.hbFrom=s.pos;say(s.n+': 800 мм без метки, полоборота назад');run(s,-1,v)}
 for(const s of both)if(s.half&&!s.v&&s.stopAt===null&&!s.waiting)run(s,s.half===1?-1:1,v);
 for(const s of both){if(s.half===1&&(s.pos<=s.hbFrom-C*c.swing||s.pos<=c.minPos)){s.half=2;run(s,1,v)}else if(s.half===2&&s.pos>=s.hbFrom){hard(s);s.half=0;if(!s.tag){s.noTag=true;say(s.n+': метки нет')}for(const o of both)if(o.waiting)o.waiting=false}}
}
if(sim.phase==='rock'){const W=sim.W;if(sim.rock===1&&!W.v&&W.stopAt===null){sim.base=W.pos;run(W,-1,c.rockV);sim.rock=2}
 else if(sim.rock===2&&W.pos<=sim.base-c.rock){run(W,1,c.rockV);sim.rock=3}
 else if(sim.rock===3&&(W.pos>=sim.base||!W.v)){hard(W);sim.rock=0;sim.phase='test';sim.from=sim.T.rot;sim.gone=0;if(r.k==='tag')sim.cand=r.uid;else sim.cand=null;say(W.n+' стоит, смотрим на ход '+sim.T.n)}
}
else if(sim.phase==='test'){const T=sim.T,W=sim.W;
 if(r.k==='col'&&sim.cand){stop(sim,T);T.waiting=true;W.waiting=false;run(W,1,v);sim.phase='split';say('две метки: '+T.n+' стоп, '+W.n+' уводит свою');return}
 const rolled=T.rot-sim.from>=c.test;
 if(r.k==='tag'&&!owner(r.uid))sim.cand=r.uid;
 if(r.k==='none'){if(++sim.gone>=2){
   if(sim.cand){put(T,sim.cand);say('метка ушла с ходом '+T.n);W.waiting=false}else say('антенна пуста: '+W.n+' стоит, ищет только '+T.n);
   sim.phase='search';return}}
 else sim.gone=0;
 if(rolled&&r.k==='tag'){put(W,r.uid);W.clearing=true;run(W,1,v);W.waiting=false;sim.phase='search';say('метка не ушла: она на '+W.n);return}
 if(T.pos>=L&&!T.v&&r.k!=='none'&&!rolled){if(sim.swapped){W.waiting=false;sim.phase='search';say('запас кончился, решения нет');return}
   sim.swapped=true;sim.T=W;sim.W=T;T.waiting=true;W.waiting=false;sim.from=W.rot;say('запас '+T.n+' кончился, теперь крутится '+W.n)}
}
if(sim.phase==='split'){const T=sim.T,W=sim.W;if(r.k==='tag'&&!owner(r.uid)&&r.uid!==sim.cand){put(T,r.uid);put(W,sim.cand);T.waiting=false;sim.phase='search';return}
 if(r.k==='tag'&&r.uid===sim.cand){put(W,sim.cand);T.waiting=false;sim.phase='search';return}
 if(r.k==='none'){if(sim.cand)put(W,sim.cand);T.waiting=false;sim.phase='search';return}}
// run everything that may run
const open2=s=>s.started&&!s.tag&&!s.noTag;for(const s of both){if(s.out)continue;const o=s===A?B:A,hold=s.tag&&!inZone(s,c.zone)&&(open2(o)||(o.inserted&&!o.started));if(s.started&&!s.v&&s.stopAt===null&&!s.waiting&&!s.half&&!s.paused&&s.pos<L&&!hold)run(s,1,v);if(hold&&s.v>0&&!s.clearing)stop(sim,s)}
const ins=both.filter(s=>s.inserted);if(ins.length&&ins.every(s=>s.started&&(s.out||(s.pos>=L&&!s.v&&(s.tag||s.noTag))))&&sim.phase==='search'&&!sim.done){sim.done=true;say(ins.map(s=>s.n).join(' и ')+': готово, ждём')}}
function verdict(sim){const o={};for(const n of['A','B']){const want=sim.sp[n].has?n:null,got=sim.sp[n].tag;o[n]=got===want?'ok':(got?'wrong':'miss')}return o}
function play(c,a,b,gap,order='AB',ha=true,hb=true){const sim=mk(c,a,b,ha,hb);let n=0;const[first,second]=order;insert(sim,first);while((!sim.done||!sim.sp[second].inserted)&&n++<40000){step(sim,.02);if(!sim.sp[second].inserted&&sim.t>=gap)insert(sim,second);if(!sim.done&&sim.t>=sim.next){sim.next=sim.t+c.poll;tick(sim)}}return{sim,timeout:!sim.done,v:verdict(sim)}}
if(typeof module!=='undefined')module.exports={mk,step,tick,insert,verdict,play,read,C};

function reread(sim,n){const s=sim.sp[n];if(s.v||s.re)return;s.re=1;s.reBase=s.pos;s.tag=null;s.noTag=false;sim.done=false;sim.guard=false;sim.progress=sim.t;sim.next=sim.t;sim.log.push(sim.t.toFixed(1)+'с Перечитать '+n);run(s,-1,sim.c.v)}
if(typeof module!=='undefined')module.exports.reread=reread;
