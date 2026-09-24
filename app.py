import asyncio
from datetime import datetime, timezone, timedelta
import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

app=FastAPI(title="AI BTC Scout V3.1")
SYMBOL="BTCUSDT"; INTERVAL="5m"; FEE=.001; START=20.; RISK=.01; STOP_ATR=1.5; RR=2

state={"equity":20.,"cash":20.,"position":None,"realized_pnl":0.,"trades":0,"wins":0,"losses":0,"fees":0.,"peak":20.,"dd":0.,"last_scan":None,"signal":"WAIT","score":0,"price":0.,"rsi":0.,"atr":0.,"trend":"UNKNOWN","history":[],"error":None}

def ema_series(values,n):
    out=[]; k=2/(n+1); e=None
    for x in values:
        e=x if e is None else x*k+e*(1-k); out.append(e)
    return out

def rsi_series(values,n=14):
    out=[50.0]*len(values)
    if len(values)<=n:return out
    gains=[]; losses=[]
    for i in range(1,len(values)):
        d=values[i]-values[i-1]; gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains[:n])/n; al=sum(losses[:n])/n
    out[n]=100 if al==0 else 100-100/(1+ag/al)
    for i in range(n,len(gains)):
        ag=(ag*(n-1)+gains[i])/n; al=(al*(n-1)+losses[i])/n
        out[i+1]=100 if al==0 else 100-100/(1+ag/al)
    return out

def atr_series(rows,n=14):
    out=[0.0]*len(rows); tr=[]
    for i,r in enumerate(rows):
        if i==0: tr.append(r[2]-r[3])
        else: tr.append(max(r[2]-r[3],abs(r[2]-rows[i-1][4]),abs(r[3]-rows[i-1][4])))
        if i>=n: out[i]=sum(tr[i-n+1:i+1])/n
    return out

def indicators_series(rows):
    c=[r[4] for r in rows]; v=[r[5] for r in rows]
    e20=ema_series(c,20); e50=ema_series(c,50); e200=ema_series(c,200)
    rv=rsi_series(c,14); av=atr_series(rows,14); scores=[0]*len(rows); trends=[]
    for i in range(len(rows)):
        s=0; va=sum(v[max(0,i-19):i+1])/len(v[max(0,i-19):i+1])
        if e20[i]>e50[i]:s+=20
        if e50[i]>e200[i]:s+=20
        if c[i]>e20[i]:s+=15
        if 50<=rv[i]<=68:s+=15
        if rv[i]<35:s-=10
        if rv[i]>75:s-=10
        if v[i]>va:s+=10
        if c[i]>e50[i]:s+=10
        scores[i]=max(0,min(100,s))
        trends.append("BULLISH" if e20[i]>e50[i]>e200[i] else ("BEARISH" if e20[i]<e50[i]<e200[i] else "MIXED/BEARISH"))
    return scores,rv,av,e20,e50,e200,trends

async def klines(limit=1000,start=None,end=None):
    p={"symbol":SYMBOL,"interval":INTERVAL,"limit":min(limit,1000)}
    if start is not None:p["startTime"]=start
    if end is not None:p["endTime"]=end
    async with httpx.AsyncClient(timeout=20) as c:
        r=await c.get("https://api.binance.com/api/v3/klines",params=p); r.raise_for_status()
    return [[int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])] for x in r.json()]

async def history(days):
    now=datetime.now(timezone.utc); end=int(now.timestamp()*1000); cur=int((now-timedelta(days=days)).timestamp()*1000); out={}
    while cur<end:
        b=await klines(1000,cur,end)
        if not b:break
        for x in b:out[x[0]]=x
        nxt=b[-1][0]+300000
        if nxt<=cur:break
        cur=nxt
        if len(b)<1000:break
    return [out[k] for k in sorted(out) if k+300000<=end]

def sim(rows):
    scores,rsis,atrs,e20,e50,e200,trends=indicators_series(rows)
    cash=START; pos=None; trades=wins=0; fees=0.; peak=START; dd=0.
    for i in range(200,len(rows)):
        p=rows[i][4]; s=scores[i]; av=atrs[i]
        if pos and (p<=pos["stop"] or p>=pos["target"]):
            ep=pos["stop"] if p<=pos["stop"] else pos["target"]; fee=pos["qty"]*ep*FEE
            net=(ep-pos["entry"])*pos["qty"]-pos["entry_fee"]-fee
            cash+=pos["qty"]*ep-fee; fees+=fee; trades+=1
            if net>0:wins+=1
            pos=None
        if not pos and s>=75 and p>e20[i]:
            rc=max(.01,cash*RISK); dist=max(av*STOP_ATR,p*.001); q=rc/dist; ef=q*p*FEE
            if q*p+ef<=cash:
                cash-=q*p+ef; fees+=ef
                pos={"entry":p,"qty":q,"stop":p-dist,"target":p+dist*RR,"entry_fee":ef}
        eq=cash+(pos["qty"]*p if pos else 0); peak=max(peak,eq); dd=max(dd,(peak-eq)/peak*100)
    ep=rows[-1][4]; end=cash+(pos["qty"]*ep if pos else 0)
    return {"days":round((rows[-1][0]-rows[0][0])/86400000,1),"candles":len(rows),"start_equity":START,"ending_equity":end,"return_pct":(end/START-1)*100,"trades":trades,"wins":wins,"losses":trades-wins,"win_rate_pct":wins/trades*100 if trades else 0,"max_drawdown_pct":dd,"fees":fees,"open_position":bool(pos)}

def draw(eq):
    state["peak"]=max(state["peak"],eq); state["dd"]=max(state["dd"],(state["peak"]-eq)/state["peak"]*100)

def close(p,reason):
    q=state["position"]["qty"]; fee=q*p*FEE; net=(p-state["position"]["entry"])*q-state["position"]["entry_fee"]-fee
    state["cash"]+=q*p-fee; state["realized_pnl"]+=net; state["fees"]+=fee; state["trades"]+=1
    if net>0:state["wins"]+=1
    else:state["losses"]+=1
    state["history"].insert(0,{"reason":reason,"entry":state["position"]["entry"],"exit":p,"pnl":net}); state["history"]=state["history"][:20]
    state["position"]=None; state["equity"]=state["cash"]; draw(state["equity"])

async def scan():
    try:
        rows=await klines(220); s,rv,av,e20,e50,e200,tr=indicators_series(rows); i=len(rows)-1; p=rows[-1][4]
        state.update(price=p,score=s[i],rsi=rv[i],atr=av[i],trend=tr[i],last_scan=datetime.now(timezone.utc).isoformat(),error=None)
        if state["position"]:
            if p<=state["position"]["stop"]:close(state["position"]["stop"],"STOP")
            elif p>=state["position"]["target"]:close(state["position"]["target"],"TARGET")
        if not state["position"] and s[i]>=75 and p>e20[i]:
            rc=max(.01,state["cash"]*RISK); dist=max(av[i]*STOP_ATR,p*.001); q=rc/dist; ef=q*p*FEE
            if q*p+ef<=state["cash"]:
                state["cash"]-=q*p+ef; state["fees"]+=ef
                state["position"]={"entry":p,"qty":q,"stop":p-dist,"target":p+dist*RR,"entry_fee":ef}
        state["equity"]=state["cash"]+(state["position"]["qty"]*p if state["position"] else 0); draw(state["equity"])
        state["signal"]="IN TRADE" if state["position"] else ("BUY" if s[i]>=75 else "WAIT")
    except Exception as e:state["error"]=str(e)

@app.get("/api/status")
async def status():return JSONResponse(state)

@app.get("/api/backtest")
async def backtest(days:int=Query(30,ge=7,le=90)):
    try:return JSONResponse(sim(await history(days)))
    except Exception as e:return JSONResponse({"error":str(e)},status_code=500)

@app.get("/",response_class=HTMLResponse)
async def home():
 return HTMLResponse("""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>AI BTC Scout V3.1</title><style>body{background:#080b10;color:#eee;font:16px Arial;max-width:900px;margin:auto;padding:20px}.c{background:#121821;border:1px solid #293545;border-radius:20px;padding:20px;margin:14px 0}.g{display:grid;grid-template-columns:1fr 1fr;gap:14px}.b{font-size:28px;font-weight:bold}button{padding:12px;border:0;border-radius:10px;margin:3px}@media(max-width:600px){.g{grid-template-columns:1fr}}</style><div class=c><h1>ð¤ AI BTC Scout V3.1</h1><p>BTC/USDT Â· 5-minute Â· PAPER ONLY</p><h2 id=s>WAIT</h2><div class=b id=p>$--</div></div><div class=g><div class=c>AI score<div class=b id=sc>--</div></div><div class=c>Trend<div class=b id=t>--</div></div><div class=c>RSI<div class=b id=r>--</div></div><div class=c>Equity<div class=b id=e>$20.00</div></div><div class=c>Win rate<div class=b id=w>0%</div></div><div class=c>Max drawdown<div class=b id=d>0%</div></div></div><div class=c><h2>Paper position</h2><div id=pos>None</div></div><div class=c><h2>V3.1 Backtest</h2><button onclick=b(30)>30 days</button><button onclick=b(60)>60 days</button><button onclick=b(90)>90 days</button><div id=bt>Ready.</div></div><div class=c><h2>Recent paper trades</h2><div id=tr>No closed trades yet.</div></div><div class=c>Paper testing only. No exchange orders or API keys. Historical simulation is not a guarantee of future performance.</div><script>
async function q(){let x=await(await fetch('/api/status')).json();p.textContent='$'+(+x.price).toLocaleString();sc.textContent=x.score;t.textContent=x.trend;r.textContent=(+x.rsi).toFixed(1);e.textContent='$'+(+x.equity).toFixed(2);w.textContent=(x.trades?100*x.wins/x.trades:0).toFixed(1)+'%';d.textContent=(+x.dd).toFixed(2)+'%';s.textContent=x.signal;pos.innerHTML=x.position?'Entry $'+x.position.entry.toFixed(2)+'<br>Stop $'+x.position.stop.toFixed(2)+'<br>Target $'+x.position.target.toFixed(2):'No open position.';tr.innerHTML=x.history.length?x.history.map(z=>z.reason+' Â· Entry $'+z.entry.toFixed(2)+' Â· Exit $'+z.exit.toFixed(2)+' Â· P/L $'+z.pnl.toFixed(4)).join('<hr>'):'No closed trades yet.'}async function b(n){bt.textContent='Running '+n+'-day backtest...';let x=await(await fetch('/api/backtest?days='+n)).json();bt.innerHTML=x.error?x.error:'<p>Period: '+x.days+' days<br>Candles: '+x.candles.toLocaleString()+'<br>Start: $'+x.start_equity.toFixed(2)+'<br>Ending: $'+x.ending_equity.toFixed(2)+'<br>Return: '+x.return_pct.toFixed(2)+'%<br>Trades: '+x.trades+'<br>Wins/Losses: '+x.wins+'/'+x.losses+'<br>Win rate: '+x.win_rate_pct.toFixed(1)+'%<br>Max drawdown: '+x.max_drawdown_pct.toFixed(2)+'%<br>Fees: $'+x.fees.toFixed(4)+'<br>Open position at end: '+x.open_position+'</p>'}q();setInterval(q,60000)</script>""")

@app.on_event("startup")
async def startup():asyncio.create_task(loop())
async def loop():
    while True:
        await scan(); await asyncio.sleep(60)

