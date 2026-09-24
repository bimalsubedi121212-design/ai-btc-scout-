import os,sqlite3,asyncio,math,uuid,bisect
from datetime import datetime,timezone,timedelta
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse,JSONResponse
BINANCE="https://api.binance.com/api/v3/klines"; SYMBOL="BTCUSDT"; START=20.0; FEE=.001; SLIP=.0005; RISK=.01; STOP_ATR=1.5; TARGET_R=2; COOLDOWN=6; MINSTOP=.01; SCORE=75; DB=os.getenv("DB_PATH","paper_trading.db")
app=FastAPI(title="AI BTC Scout Research V5"); state={"cash":20.,"equity":20.,"price":None,"score":0,"trend":"UNKNOWN","rsi":None,"pos":None,"processed":None,"cool":0,"pending":None,"err":None}
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
def score(c,d,i,t):
 if i<205 or any(d[k][i] is None for k in ("e20","e50","e200","rsi","atr","va")): return 0
 if not t: return 0
 # V3 hypothesis: bullish pullback/reclaim instead of fresh breakout chasing.
 close=c[i][4]; prev_close=c[i-1][4]; prev_low=c[i-1][3]
 e20,e50,e200=d["e20"][i],d["e50"][i],d["e200"][i]
 rs,at,v=d["rsi"][i],d["atr"][i],d["va"][i]
 pullback=(prev_low<=d["e20"][i-1] or prev_close<=d["e20"][i-1])
 reclaim=(close>e20 and close>c[i-1][2])
 healthy_rsi=(48<=rs<=64)
 volume_ok=(c[i][5]>=0.90*v)
 not_stretched=(close<=e20+1.0*at)
 s=0
 if e20>e50>e200: s+=30
 if pullback: s+=25
 if reclaim: s+=25
 if healthy_rsi: s+=10
 if volume_ok: s+=10
 if not not_stretched: s-=20
 return max(0,min(100,s))

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
def sim(c,h,a,b,cap=None):
 d=inds(c);p=prep(h);cash=20.;pos=None;tr=[];signals=0;turn=fees=slips=0.;peak=20.;mdd=0.;cool=0;first=max(205,a);last=min(b,len(c)-1)
 for i in range(first,last):
  x=c[i]
  if pos:
   hs=x[3]<=pos["stop"];ht=x[2]>=pos["target"]
   if hs or ht:
    raw=pos["stop"] if hs else pos["target"];reason="STOP" if hs else "TARGET";ex=raw*(1-SLIP);pro=pos["qty"]*ex;ef=pro*FEE;gross=(ex-pos["entry"])*pos["qty"];net=gross-pos["fee"]-ef;cash+=pro-ef;fees+=pos["fee"]+ef;slips+=raw*SLIP*pos["qty"];turn+=pro;tr.append((net,reason));pos=None;cool=COOLDOWN
  eq=cash+(pos["qty"]*x[4] if pos else 0);peak=max(peak,eq);mdd=max(mdd,(peak-eq)/peak)
  if not pos and cool==0 and i+1<last and score(c,d,i,trend(p,x[0]))>=SCORE:
   signals+=1;entry=c[i+1][1]*(1+SLIP);stop=entry-STOP_ATR*d["atr"][i];risk=max(entry-stop,entry*MINSTOP);qty=min((eq*RISK)/risk,cash/(entry*(1+FEE)));qty=min(qty,(eq*cap)/entry) if cap else qty
   if qty>0:
    no=qty*entry;ef=no*FEE;cash-=no+ef;fees+=ef;slips+=entry*SLIP*qty;turn+=no;pos={"entry":entry,"qty":qty,"stop":stop,"target":entry+TARGET_R*(entry-stop),"fee":ef}
  if cool:cool-=1
 if pos:
  ex=c[last][4]*(1-SLIP);pro=pos["qty"]*ex;ef=pro*FEE;gross=(ex-pos["entry"])*pos["qty"];tr.append((gross-pos["fee"]-ef,"END"));cash+=pro-ef;fees+=pos["fee"]+ef;turn+=pro
 wins=sum(x[0]>0 for x in tr);loss=sum(x[0]<0 for x in tr);gp=sum(x[0] for x in tr if x[0]>0);gl=-sum(x[0] for x in tr if x[0]<0);pf=gp/gl if gl else None
 return {"end":cash,"return_pct":(cash/20-1)*100,"trades":len(tr),"signals":signals,"win_rate":100*wins/len(tr) if tr else 0,"dd":mdd*100,"fees":fees,"slippage":slips,"turnover":turn,"tm":turn/20,"pf":pf,"expectancy":sum(x[0] for x in tr)/len(tr) if tr else 0,"stops":sum(x[1]=="STOP" for x in tr),"targets":sum(x[1]=="TARGET" for x in tr)}
async def bt(days):
 c=await fetch(days,"1h");h=await fetch(days+30,"4h");n=len(c);k=int(n*.7);return {"candles":n,"train":sim(c,h,0,k),"test":sim(c,h,k,n),"cap_train":sim(c,h,0,k,.25),"cap_test":sim(c,h,k,n,.25)}
jobs={}
async def worker(j,d):
 try:jobs[j]={"status":"running"};jobs[j]["result"]=await bt(d);jobs[j]["status"]="done"
 except Exception as e:jobs[j]={"status":"error","error":str(e)}
PAGE='''<!doctype html><html><meta name=viewport content="width=device-width,initial-scale=1"><style>body{background:#070a0f;color:#eee;font-family:Arial;margin:0}.w{max-width:760px;margin:auto;padding:16px}.c{background:#111822;border:1px solid #263548;border-radius:20px;padding:18px;margin:12px 0}.b{font-size:32px;font-weight:800}.muted{color:#9aa7b6}button{padding:12px;border:0;border-radius:10px;margin:4px} </style><div class=w><div class=c><h1>AI BTC Scout â AUDIT</h1><div class=muted>BTC/USDT Â· PAPER ONLY Â· score â¥75</div><div id=s></div></div><div class=c><span class=muted>Equity</span><div class=b id=e>$20.00</div><span class=muted>BTC</span><div class=b id=p>â</div></div><div class=c><h2>Live paper engine</h2><div id=l>No open paper position</div></div><div class=c><h2>Research audit</h2><button onclick=run(180)>180 days</button><button onclick=run(365)>365 days</button><div id=b>Not run yet.</div></div></div><script>async function load(){let d=await(await fetch('/api/status')).json();e.textContent='$'+d.equity.toFixed(2);p.textContent=d.price?'$'+Math.round(d.price).toLocaleString():'â';s.innerHTML='<b>'+d.action+'</b> Â· Score '+d.score+' Â· '+d.trend+' Â· RSI '+(d.rsi??'â');l.innerHTML=d.position?'OPEN Â· Entry $'+d.position.entry.toFixed(2)+' Â· Stop $'+d.position.stop.toFixed(2)+' Â· Target $'+d.position.target.toFixed(2):'No open paper position'}function box(n,x){return '<div class=c><b>'+n+'</b><br>End $'+x.end.toFixed(2)+' Â· Return '+x.return_pct.toFixed(2)+'% Â· Trades '+x.trades+'<br>Signals '+x.signals+' Â· Win '+x.win_rate.toFixed(1)+'% Â· DD '+x.dd.toFixed(2)+'%<br>Fees $'+x.fees.toFixed(4)+' Â· Slippage $'+x.slippage.toFixed(4)+' Â· Turnover $'+x.turnover.toFixed(2)+' ('+x.tm.toFixed(1)+'Ã)<br>PF '+(x.pf==null?'â':x.pf.toFixed(2))+' Â· Expectancy $'+x.expectancy.toFixed(4)+' Â· Stops '+x.stops+' Â· Targets '+x.targets+'</div>'}async function run(d){b.textContent='Running '+d+'-day auditâ¦';let q=await(await fetch('/api/backtest/'+d)).json();for(let i=0;i<240;i++){await new Promise(r=>setTimeout(r,2000));let z=await(await fetch('/api/backtest/status/'+q.job_id)).json();if(z.status==='done'){let r=z.result;b.innerHTML=box('CURRENT â TRAIN 70%',r.train)+box('CURRENT â UNSEEN 30%',r.test)+box('25% NOTIONAL CAP â TRAIN 70%',r.cap_train)+box('25% NOTIONAL CAP â UNSEEN 30%',r.cap_test);return}if(z.status==='error'){b.textContent='Failed: '+z.error;return}}b.textContent='Timed out; paper engine remains safe.'}load();setInterval(load,60000)</script>'''
@app.get('/',response_class=HTMLResponse)
async def home():return PAGE
@app.get('/api/status')
async def status():
 load();eq=state['cash']+(state['pos']['qty']*state['price'] if state['pos'] and state['price'] else 0);return {'equity':eq,'cash':state['cash'],'price':state['price'],'score':state['score'],'trend':state['trend'],'rsi':state['rsi'],'position':state['pos'],'action':'PAPER POSITION' if state['pos'] else ('SIGNAL' if state['score']>=SCORE else 'WAIT'),'error':state['err']}
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
   c=await fetch(3,'5m');h=await fetch(10,'1h');load();d=inds(c);i=len(c)-1;ts=c[i][0];t=trend(prep(h),ts);sc=score(c,d,i,t)
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

    
