import os,sqlite3,asyncio,math,uuid,bisect
from datetime import datetime,timezone,timedelta
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse,JSONResponse
BINANCE="https://api.binance.com/api/v3/klines"; SYMBOL="BTCUSDT"; START=20.0; FEE=.001; SLIP=.0005; RISK=.01; STOP_ATR=1.5; TARGET_R=2; COOLDOWN=6; MINSTOP=.01; SCORE=75; DB=os.getenv("DB_PATH","paper_trading.db")
app=FastAPI(title="AI BTC Scout Research V7.2"); state={"cash":20.,"equity":20.,"price":None,"score":0,"trend":"UNKNOWN","rsi":None,"pos":None,"processed":None,"cool":0,"pending":None,"err":None}
def db():
 c=sqlite3.connect(DB);c.row_factory=sqlite3.Row
 c.execute("create table if not exists account(id integer primary key,cash real,equity real,peak real,dd real,updated text)")
 c.execute("create table if not exists positions(id integer primary key,entry real,qty real,stop real,target real,fee real,entry_time text)")
 c.execute("create table if not exists trades(id integer primary key autoincrement,entry_time text,exit_time text,entry real,exit real,qty real,gross real,fees real,net real,reason text)");c.commit();return c
def load():
 c=db();a=c.execute("select * from account where id=1").fetchone()
 if not a:c.execute("insert into account values(1,?,?,?,?,?)",(20,20,20,0,datetime.now(timezone.utc).isoformat()));c.commit();state["cash"]=state["equity"]=20
 else:state["cash"]=a["cash"];state["equity"]=a["equity"]
 p=c.execute("select * from positions where id=1").fetchone();state["pos"]=dict(p) if p else None;c.close()
def save():
 c=db();a=c.execute("select peak,dd from account where id=1").fetchone();peak=max(a["peak"],state["equity"]);dd=max(a["dd"],(peak-state["equity"])/peak if peak else 0);c.execute("update account set cash=?,equity=?,peak=?,dd=?,updated=? where id=1",(state["cash"],state["equity"],peak,dd,datetime.now(timezone.utc).isoformat()));c.commit();c.close()
def savepos(p):
 c=db();c.execute("delete from positions where id=1")
 if p:c.execute("insert into positions values(1,?,?,?,?,?,?)",(p["entry"],p["qty"],p["stop"],p["target"],p["fee"],p["entry_time"]))
 c.commit();c.close()
def ema(v,n):
 if len(v)<n:return [None]*len(v)
 k=2/(n+1);o=[None]*(n-1);e=sum(v[:n])/n;o.append(e)
 for x in v[n:]:e=x*k+e*(1-k);o.append(e)
 return o
def rsi(v,n=14):
 o=[None]*len(v)
 if len(v)<=n:return o
 g=[];l=[]
 for i in range(1,n+1):d=v[i]-v[i-1];g.append(max(d,0));l.append(max(-d,0))
 ag=sum(g)/n;al=sum(l)/n;o[n]=100 if al==0 else 100-100/(1+ag/al)
 for i in range(n+1,len(v)):
  d=v[i]-v[i-1];ag=(ag*(n-1)+max(d,0))/n;al=(al*(n-1)+max(-d,0))/n;o[i]=100 if al==0 else 100-100/(1+ag/al)
 return o
def atr(c,n=14):
 t=[x[2]-x[3] if i==0 else max(x[2]-x[3],abs(x[2]-c[i-1][4]),abs(x[3]-c[i-1][4])) for i,x in enumerate(c)];o=[None]*len(t)
 if len(t)<n:return o
 a=sum(t[:n])/n;o[n-1]=a
 for i in range(n,len(t)):a=(a*(n-1)+t[i])/n;o[i]=a
 return o
def inds(c):
 cl=[x[4] for x in c];v=[x[5] for x in c];return {"e20":ema(cl,20),"e50":ema(cl,50),"e200":ema(cl,200),"rsi":rsi(cl),"atr":atr(c),"va":[None if i<20 else sum(v[i-20:i])/20 for i in range(len(c))]}
def prep(h):
 cl=[x[4] for x in h];return [x[0] for x in h],ema(cl,20),ema(cl,50),ema(cl,200)
def trend(p,ts):
 o,a,b,c=p;j=bisect.bisect_right(o,ts)-1;return j>=200 and a[j] and b[j] and c[j] and a[j]>b[j]>c[j]
STRATEGIES=("TREND_PULLBACK","BREAKOUT","MEAN_REVERSION","MOMENTUM")

def score(c,d,i,t,strategy):
 if i<205 or any(d[k][i] is None for k in ("e20","e50","e200","rsi","atr","va")): return 0
 if not t: return 0
 close=c[i][4]; pc=c[i-1][4]; pl=c[i-1][3]; ph=c[i-1][2]
 e20,e50,e200=d["e20"][i],d["e50"][i],d["e200"][i]
 rs,at,v=d["rsi"][i],d["atr"][i],d["va"][i]; vol=c[i][5]
 if strategy=="TREND_PULLBACK":
  pull=pl<=d["e20"][i-1] or pc<=d["e20"][i-1]
  reclaim=close>e20 and close>ph
  return 100 if e20>e50>e200 and pull and reclaim and 48<=rs<=64 and vol>=.90*v and close<=e20+1.0*at else 0
 if strategy=="BREAKOUT":
  prior=max(x[2] for x in c[i-4:i])
  return 100 if e20>e50>e200 and close>prior and 52<=rs<=72 and vol>=1.20*v and close<=e20+1.5*at else 0
 if strategy=="MEAN_REVERSION":
  dip=pc<d["e20"][i-1] and pc>d["e50"][i-1]
  return 100 if e20>e50 and dip and close>e20 and 42<=rs<=55 and vol>=.70*v and close<=e20+.75*at else 0
 if strategy=="MOMENTUM":
  momentum=close>ph and pc>c[i-2][2]
  return 100 if e20>e50>e200 and momentum and 55<=rs<=70 and vol>=1.10*v and close<=e20+1.75*at else 0
 return 0

async def fetch(days,iv):
 end=int(datetime.now(timezone.utc).timestamp()*1000);cur=int((datetime.now(timezone.utc)-timedelta(days=days)).timestamp()*1000);out=[]
 async with httpx.AsyncClient(timeout=20) as cl:
  while cur<end:
   r=await cl.get(BINANCE,params={"symbol":SYMBOL,"interval":iv,"startTime":cur,"endTime":end,"limit":1000});r.raise_for_status();b=r.json()
   if not b:break
   out+=b;n=b[-1][0]+1
   if n<=cur:break
   cur=n
   if len(b)<1000:break
 now=end;return [[int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])] for x in out if int(x[6])<=now]
def sim(c,h,a,b,cap=None,strategy='TREND_PULLBACK'):
 d=inds(c);p=prep(h);cash=20.;pos=None;tr=[];signals=0;turn=fees=slips=0.;peak=20.;mdd=0.;cool=0;first=max(205,a);last=min(b,len(c)-1)
 for i in range(first,last):
  x=c[i]
  if pos:
   hs=x[3]<=pos["stop"];ht=x[2]>=pos["target"]
   if hs or ht:
    raw=pos["stop"] if hs else pos["target"];reason="STOP" if hs else "TARGET";ex=raw*(1-SLIP);pro=pos["qty"]*ex;ef=pro*FEE;gross=(ex-pos["entry"])*pos["qty"];net=gross-pos["fee"]-ef;cash+=pro-ef;fees+=pos["fee"]+ef;slips+=raw*SLIP*pos["qty"];turn+=pro;tr.append((net,reason));pos=None;cool=COOLDOWN
  eq=cash+(pos["qty"]*x[4] if pos else 0);peak=max(peak,eq);mdd=max(mdd,(peak-eq)/peak)
  if not pos and cool==0 and i+1<last and score(c,d,i,trend(p,x[0]),strategy)>=SCORE:
   signals+=1;entry=c[i+1][1]*(1+SLIP);stop=entry-STOP_ATR*d["atr"][i];risk=max(entry-stop,entry*MINSTOP);qty=min((eq*RISK)/risk,cash/(entry*(1+FEE)));qty=min(qty,(eq*cap)/entry) if cap else qty
   if qty>0:
    no=qty*entry;ef=no*FEE;cash-=no+ef;fees+=ef;slips+=entry*SLIP*qty;turn+=no;pos={"entry":entry,"qty":qty,"stop":stop,"target":entry+TARGET_R*(entry-stop),"fee":ef}
  if cool:cool-=1
 if pos:
  ex=c[last][4]*(1-SLIP);pro=pos["qty"]*ex;ef=pro*FEE;gross=(ex-pos["entry"])*pos["qty"];tr.append((gross-pos["fee"]-ef,"END"));cash+=pro-ef;fees+=pos["fee"]+ef;turn+=pro
 wins=sum(x[0]>0 for x in tr);loss=sum(x[0]<0 for x in tr);gp=sum(x[0] for x in tr if x[0]>0);gl=-sum(x[0] for x in tr if x[0]<0);pf=gp/gl if gl else None
 return {"end":cash,"return_pct":(cash/20-1)*100,"trades":len(tr),"signals":signals,"win_rate":100*wins/len(tr) if tr else 0,"dd":mdd*100,"fees":fees,"slippage":slips,"turnover":turn,"tm":turn/20,"pf":pf,"expectancy":sum(x[0] for x in tr)/len(tr) if tr else 0,"stops":sum(x[1]=="STOP" for x in tr),"targets":sum(x[1]=="TARGET" for x in tr)}
async def bt(days):
 c=await fetch(days,"15m");h=await fetch(days+10,"1h");n=len(c);k=int(n*.7);out={}
 for name in STRATEGIES:
  out[name]={"train":sim(c,h,0,k,None,name),"test":sim(c,h,k,n,None,name),
             "cap_train":sim(c,h,0,k,.25,name),"cap_test":sim(c,h,k,n,.25,name)}
 return {"candles":n,"strategies":out,"ml_walk_forward":walk_forward_ml(c)}

def ml_features(c,i):
    if i<60: return None
    close=[x[4] for x in c]
    ret1=close[i]/close[i-1]-1
    ret3=close[i]/close[i-3]-1
    ret12=close[i]/close[i-12]-1
    ma20=sum(close[i-20:i])/20
    ma50=sum(close[i-50:i])/50
    vol=sum((close[j]/close[j-1]-1)**2 for j in range(i-20,i+1))/20
    rng=(c[i][2]-c[i][3])/close[i]
    return [1.0,ret1,ret3,ret12,close[i]/ma20-1,close[i]/ma50-1,vol,rng]

def sigmoid(z):
    z=max(-30,min(30,z))
    return 1/(1+math.exp(-z))

def fit_logistic(X,y,epochs=6,lr=.10,l2=.001):
    if not X: return None
    w=[0.0]*len(X[0])
    for _ in range(epochs):
        g=[0.0]*len(w)
        for row,yy in zip(X,y):
            p=sigmoid(sum(a*b for a,b in zip(w,row)))
            for j,a in enumerate(row): g[j]+=(p-yy)*a
        for j in range(len(w)):
            g[j]=g[j]/len(X)+l2*w[j]
            w[j]-=lr*g[j]
    return w

def walk_forward_ml(c):
    # 70/30 chronological split; model only sees prior labels/features.
    n=len(c); cut=int(n*.70); warm=80; horizon=4
    if n<cut+warm+horizon+20: return {"error":"not enough candles"}
    X=[];Y=[]
    preds=[]
    cash=20.;pos=None;tr=[];fees=slips=turn=0.;peak=20.;mdd=0.
    # Probability diagnostics: these let us see whether the model is simply too conservative.
    pred_count=0; prob_sum=0.0; prob_min=1.0; prob_max=0.0
    above50=above52=above55=above58=above60=0
    candidates=0
    # Labels are whether the next 4 entry candles are net-positive before costs.
    for i in range(warm,cut-horizon):
        f=ml_features(c,i)
        if f is None: continue
        y=1 if c[i+horizon][4]>c[i][4] else 0
        X.append(f);Y.append(y)
    w=fit_logistic(X[-600:],Y[-600:])
    for i in range(cut,n-horizon):
        f=ml_features(c,i)
        if f is None: continue
        # Walk-forward refit every 96 candles using only data available before i.
        if (i-cut)%768==0:
            X=[];Y=[]
            start=max(warm,i-1000)
            for j in range(start,i-horizon):
                fj=ml_features(c,j)
                if fj is not None:
                    X.append(fj);Y.append(1 if c[j+horizon][4]>c[j][4] else 0)
            w=fit_logistic(X[-600:],Y[-600:])
        p=sigmoid(sum(a*b for a,b in zip(w,f)))
        pred_count+=1; prob_sum+=p; prob_min=min(prob_min,p); prob_max=max(prob_max,p)
        above50+=p>=0.50; above52+=p>=0.52; above55+=p>=0.55; above58+=p>=0.58; above60+=p>=0.60
        if p>=0.55: candidates+=1
        if pos:
            hit_stop=c[i][3]<=pos["stop"]; hit_target=c[i][2]>=pos["target"]
            if hit_stop or hit_target:
                raw=pos["stop"] if hit_stop else pos["target"]
                reason="STOP" if hit_stop else "TARGET"
                ex=raw*(1-SLIP);pro=pos["qty"]*ex;ef=pro*FEE
                net=(ex-pos["entry"])*pos["qty"]-pos["fee"]-ef
                cash+=pro-ef;fees+=pos["fee"]+ef;slips+=raw*SLIP*pos["qty"];turn+=pro
                tr.append(net);pos=None
        eq=cash+(pos["qty"]*c[i][4] if pos else 0);peak=max(peak,eq);mdd=max(mdd,(peak-eq)/peak)
        if not pos and p>=.55:
            entry=c[i+1][1]*(1+SLIP)
            recent=[abs(c[j][2]-c[j][3]) for j in range(max(0,i-14),i+1)]
            at=sum(recent)/len(recent)
            stop=entry-max(STOP_ATR*at,entry*MINSTOP)
            risk=max(entry-stop,entry*MINSTOP)
            qty=min((eq*RISK)/risk,cash/(entry*(1+FEE)))
            qty=min(qty,(eq*.25)/entry)
            if qty>0:
                no=qty*entry;ef=no*FEE;cash-=no+ef;fees+=ef;slips+=entry*SLIP*qty;turn+=no
                pos={"entry":entry,"qty":qty,"stop":stop,"target":entry+1.5*(entry-stop),"fee":ef}
    if pos:
        ex=c[-1][4]*(1-SLIP);pro=pos["qty"]*ex;ef=pro*FEE
        cash+=pro-ef;fees+=pos["fee"]+ef;turn+=pro
        tr.append((ex-pos["entry"])*pos["qty"]-pos["fee"]-ef)
    wins=sum(x>0 for x in tr);losses=sum(x<0 for x in tr)
    gp=sum(x for x in tr if x>0);gl=-sum(x for x in tr if x<0)
    pf=gp/gl if gl else None
    return {"start":20.0,"end":cash,"return_pct":(cash/20-1)*100,"trades":len(tr),
            "win_rate":100*wins/len(tr) if tr else 0,"dd":100*mdd,"fees":fees,
            "slippage":slips,"turnover":turn,"pf":pf,
            "expectancy":sum(tr)/len(tr) if tr else 0,"test_start_index":cut,
            "predictions":pred_count,"avg_prob":(prob_sum/pred_count if pred_count else 0),
            "min_prob":(prob_min if pred_count else 0),"max_prob":(prob_max if pred_count else 0),
            "above_50":above50,"above_52":above52,"above_55":above55,
            "above_58":above58,"above_60":above60,"candidates":candidates,
            "note":"Walk-forward ML; 30% test period was not used for fitting."}


jobs={}
async def worker(j,d):
 try:jobs[j]={"status":"running"};jobs[j]["result"]=await bt(d);jobs[j]["status"]="done"
 except Exception as e:jobs[j]={"status":"error","error":str(e)}
PAGE='''<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<style>body{background:#070a0f;color:#eee;font-family:-apple-system,BlinkMacSystemFont,Arial;margin:0}.w{max-width:760px;margin:auto;padding:16px}.c{background:#111822;border:1px solid #263548;border-radius:20px;padding:18px;margin:12px 0}.b{font-size:32px;font-weight:800}.m{color:#9aa7b6}button{padding:12px 16px;border:0;border-radius:11px;margin:4px;font-size:16px}</style>
</head><body><div class=w>
<div class=c><h1>AI BTC Scout - RESEARCH V7.2</h1><div class=m>BTC/USDT Â· 15m entries Â· 1h trend Â· 4 fixed strategies + walk-forward ML Â· PAPER ONLY</div><div id=s>Loading...</div></div>
<div class=c><div class=m>Paper equity</div><div class=b id=e>$20.00</div><div class=m>BTC</div><div class=b id=p>â</div></div>
<div class=c><h2>Live paper engine</h2><div id=l>Loading...</div></div>
<div class=c><h2>Research audit</h2><div class=m>Fixed strategies plus walk-forward ML. The final 30% is not used for ML fitting.</div>
<button onclick=run(180)>180 days</button><button onclick=run(365)>365 days</button><br><button onclick=mlrun(180)>Run ML 180d</button><button onclick=mlrun(365)>Run ML 365d</button><div id=b>Not run yet.</div></div>
</div><script>
async function load(){let d=await(await fetch('/api/status')).json();e.textContent='$'+d.equity.toFixed(2);p.textContent=d.price?'$'+Math.round(d.price).toLocaleString():'â';s.innerHTML='<b>'+d.action+'</b> Â· Score '+d.score+' Â· '+d.trend+' Â· RSI '+(d.rsi??'â');l.innerHTML=d.position?'OPEN Â· Entry $'+d.position.entry.toFixed(2)+' Â· Stop $'+d.position.stop.toFixed(2)+' Â· Target $'+d.position.target.toFixed(2):'No open paper position'}
function box(n,x){return '<div class=c><b>'+n+'</b><br>End $'+x.end.toFixed(2)+' Â· Return '+x.return_pct.toFixed(2)+'% Â· Trades '+x.trades+'<br>Signals '+x.signals+' Â· Win '+x.win_rate.toFixed(1)+'% Â· DD '+x.dd.toFixed(2)+'%<br>Fees $'+x.fees.toFixed(4)+' Â· Slippage $'+x.slippage.toFixed(4)+' Â· Turnover $'+x.turnover.toFixed(2)+' ('+x.tm.toFixed(1)+'x)<br>PF '+(x.pf==null?'â':x.pf.toFixed(2))+' Â· Expectancy $'+x.expectancy.toFixed(4)+' Â· Stops '+x.stops+' Â· Targets '+x.targets+'</div>'}
async function run(d){b.textContent='Running '+d+'-day research...';try{let q=await(await fetch('/api/backtest/'+d)).json();for(let i=0;i<240;i++){await new Promise(r=>setTimeout(r,2000));let z=await(await fetch('/api/backtest/status/'+q.job_id)).json();if(z.status==='done'){let h='<div class=c><b>Same data split for every strategy</b><br>70% training Â· 30% unseen Â· 15m entries Â· 1h trend Â· 1.5R</div>';for(const n of Object.keys(z.result.strategies)){let x=z.result.strategies[n];h+='<h2>'+n.replaceAll('_',' ')+'</h2>'+box('TRAIN 70%',x.train)+box('UNSEEN 30%',x.test)+box('25% CAP - TRAIN',x.cap_train)+box('25% CAP - UNSEEN',x.cap_test)}b.innerHTML=h;return}if(z.status==='error')throw Error(z.error)}throw Error('Timed out; paper engine is unaffected')}catch(e){b.textContent='Research failed: '+e.message}}

async function mlrun(d){b.textContent='Running walk-forward ML '+d+'-day test...';let q=await(await fetch('/api/ml/'+d)).json();for(let i=0;i<240;i++){await new Promise(r=>setTimeout(r,2000));let z=await(await fetch('/api/backtest/status/'+q.job_id)).json();if(z.status==='done'){let x=z.result;b.innerHTML='<div class=c><h2>WALK-FORWARD ML â '+d+' DAYS</h2>End $'+x.end.toFixed(2)+' Â· Return '+x.return_pct.toFixed(2)+'% Â· Trades '+x.trades+'<br>Win '+x.win_rate.toFixed(1)+'% Â· DD '+x.dd.toFixed(2)+'% Â· PF '+(x.pf==null?'â':x.pf.toFixed(2))+'<br>Fees $'+x.fees.toFixed(4)+' Â· Slippage $'+x.slippage.toFixed(4)+' Â· Turnover $'+x.turnover.toFixed(2)+'<hr><b>ML diagnostics</b><br>Predictions '+x.predictions+' Â· Avg probability '+x.avg_prob.toFixed(3)+' Â· Min '+x.min_prob.toFixed(3)+' Â· Max '+x.max_prob.toFixed(3)+'<br>â¥50%: '+x.above_50+' Â· â¥52%: '+x.above_52+' Â· â¥55%: '+x.above_55+' Â· â¥58%: '+x.above_58+' Â· â¥60%: '+x.above_60+'<br>Entry candidates at 55%: '+x.candidates+'<br><span class=m>'+x.note+'</span></div>';return}if(z.status==='error'){b.textContent='ML failed: '+z.error;return}b.textContent='ML runningâ¦ '+(i+1)+' / 240';if(z.status==='error'){b.textContent='ML failed: '+z.error;return}}b.textContent='ML test timed out; paper engine unaffected'}

load();setInterval(load,60000)
</script></body></html>'''

@app.get('/',response_class=HTMLResponse)
async def home():return PAGE
@app.get('/api/status')
async def status():
 load();eq=state['cash']+(state['pos']['qty']*state['price'] if state['pos'] and state['price'] else 0);return {'equity':eq,'cash':state['cash'],'price':state['price'],'score':state['score'],'trend':state['trend'],'rsi':state['rsi'],'position':state['pos'],'action':'PAPER POSITION' if state['pos'] else ('SIGNAL' if state['score']>=SCORE else 'WAIT'),'error':state['err']}
@app.get('/api/ml/{days}')
async def ml_route(days:int):
 if days not in (180,365): return JSONResponse({"error":"Use 180 or 365 days"},400)
 jid=uuid.uuid4().hex[:12]; jobs[jid]={"status":"queued","kind":"ml","days":days}
 async def run_ml():
  try:
   jobs[jid]["status"]="running"
   c=await fetch(days,"15m")
   jobs[jid]["result"]=await asyncio.to_thread(walk_forward_ml,c)
   jobs[jid]["status"]="done"
  except Exception as e:
   jobs[jid]={"status":"error","error":str(e)}
 asyncio.create_task(run_ml())
 return {"job_id":jid,"status":"queued"}

@app.get('/api/backtest/{days}')
async def start(days:int):
 if days not in (180,365):return JSONResponse({'error':'Use 180 or 365 days'},400)
 j=uuid.uuid4().hex[:10];jobs[j]={'status':'queued'};asyncio.create_task(worker(j,days));return {'job_id':j}
@app.get('/api/backtest/status/{j}')
async def js(j):return jobs.get(j,{'status':'error','error':'Unknown job'})
async def scan():
 await asyncio.sleep(3)
 while True:
  try:
   c=await fetch(3,'5m');h=await fetch(10,'1h');load();d=inds(c);i=len(c)-1;ts=c[i][0];t=trend(prep(h),ts);sc=score(c,d,i,t,'TREND_PULLBACK')
   state.update(price=c[i][4],score=sc,trend='BULLISH' if t else 'BEARISH',rsi=d['rsi'][i],err=None)
   if state['processed']!=ts:
    # Enter pending signal on the next completed candle's open.
    if state['pending'] and not state['pos']:
     entry=c[i][1]*(1+SLIP);at=state['pending'];stop=entry-STOP_ATR*at;risk=max(entry-stop,entry*MINSTOP)
     qty=min((state['cash']*RISK)/risk,state['cash']/(entry*(1+FEE)))
     if qty>0:
      no=qty*entry;ef=no*FEE;state['cash']-=no+ef
      state['pos']={'entry':entry,'qty':qty,'stop':stop,'target':entry+TARGET_R*(entry-stop),'fee':ef,'entry_time':datetime.fromtimestamp(ts/1000,timezone.utc).isoformat()};savepos(state['pos'])
     state['pending']=None
    # Conservative stop-first exit if both levels occur in one candle.
    if state['pos']:
     q=state['pos'];hs=c[i][3]<=q['stop'];ht=c[i][2]>=q['target']
     if hs or ht:
      raw=q['stop'] if hs else q['target'];reason='STOP' if hs else 'TARGET';ex=raw*(1-SLIP);pro=q['qty']*ex;ef=pro*FEE;gross=(ex-q['entry'])*q['qty'];net=gross-q['fee']-ef
      state['cash']+=pro-ef
      cc=db();cc.execute("insert into trades(entry_time,exit_time,entry,exit,qty,gross,fees,net,reason) values(?,?,?,?,?,?,?,?,?)",(q['entry_time'],datetime.fromtimestamp(ts/1000,timezone.utc).isoformat(),q['entry'],ex,q['qty'],gross,q['fee']+ef,net,reason));cc.commit();cc.close()
      state['pos']=None;savepos(None);state['cool']=COOLDOWN
    if not state['pos'] and state['cool']==0 and sc>=SCORE and t:
     state['pending']=d['atr'][i]
    if state['cool']>0:state['cool']-=1
    state['equity']=state['cash']+(state['pos']['qty']*state['price'] if state['pos'] else 0);state['processed']=ts;save()
  except Exception as e:state['err']=str(e)
  await asyncio.sleep(60)
@app.on_event('startup')
async def startup():load();asyncio.create_task(scan())
