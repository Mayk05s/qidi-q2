const {play,reread,step,tick}=require('./pair_logic.js');
const B={delay:.3,slackMin:0,slackMax:300,test:100,zone:24,v:60,limit:1000,poll:.2,rock:20,rockV:30,swing:1.04,startDelay:2,watchdog:60,minPos:60};
const out=[];
function sw(label,c,ha=true,hb=true,st=10){let n=0,ok=0,wr=0,mi=0,ou=0,gu=0,mx=0,T=0,bad=null;for(const o of['AB','BA'])for(const g of[0,.2,.5,1,2,3,5,8,15,30,60])for(let a=0;a<360;a+=st)for(let b=0;b<360;b+=st){const r=play(c,a,b,g,o,ha,hb);n++;T+=r.sim.t;mx=Math.max(mx,r.sim.t);if(r.sim.guard)gu++;if(r.sim.sp.A.out||r.sim.sp.B.out)ou++;if(r.v.A==='ok'&&r.v.B==='ok')ok++;else{if(r.v.A==='wrong'||r.v.B==='wrong')wr++;else mi++;bad=bad||[a,b,g,o,r.v.A,r.v.B]}}
const line=[label,n,ok,wr,mi,ou,gu,(T/n).toFixed(0),mx.toFixed(0),bad?JSON.stringify(bad):''].join(' | ');console.log(line);out.push(line)}
sw('base',B);
sw('slack 0-100',{...B,slackMax:100});sw('slack 0-500',{...B,slackMax:500});sw('delay .1',{...B,delay:.1});sw('delay .5',{...B,delay:.5});sw('delay .8',{...B,delay:.8});sw('zone 12',{...B,zone:12});sw('zone 40',{...B,zone:40});sw('zone 60',{...B,zone:60});sw('poll .1',{...B,poll:.1});sw('poll .5',{...B,poll:.5});
sw('no tag A',B,false,true);sw('no tag B',B,true,false);sw('no tags',B,false,false);
sw('slip 0-100',{...B,slipMax:100});sw('slip 0-300',{...B,slipMax:300});sw('limit 800',{...B,limit:800});sw('limit 900',{...B,limit:900});sw('limit 1100',{...B,limit:1100});
