import asyncio
from datetime import datetime, timezone, timedelta
import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

app=FastAPI(title="AI BTC Scout V4.1")
SYMBOL="BTCUSDT"; INTERVAL="5m"; FEE=.001; START=20.; RISK=.01; STOP_ATR=1.5; RR=2
THRESHOLDS=[55,60,65,70,75]
state={"equity":20.,"cash":20.,"position":None,"last_scan":None,"signal":"WAIT","score":0,"price":0.,"rsi":0.,"trend":"UNKNOWN","error":None}

def ema(a,n):
    e=a[0]; k=2/(n+1)
    for x in a[1:]: e=x*k+e*(1-k)
    return e
def series_ema(a,n):
    out=[]; e=a[0]; k=2/(n+1)
    for x in a: e=x if not out else x*k+e*(1-k); out.append(e)
    return out
def series_rsi(a,n=14):
    out=[50.]*len(a)
    if len(a)<=n:return out
    g=[max(a[i]-a[i-1],0) for i in range(1,len(a))]
    l=[max(a[i-1]-a[i],0) for i in range(1,len(a))]
    ag=sum(g[:n])/n; al=sum(l[:n])/n
    out[n]=100 if al==0 else 100-100/(1+ag/al)
    for i in range(n,len(g)):
        ag=(ag*(n-1)+g[i])/n; al=(al*(n-1)+l[i])/n
        out[i+1]=100 if al==0 else 100-100/(1+ag/al)
    return out
def series_atr(rows,n=14):
    out=[0.]*len(rows); tr=[]
    for i,r in enumerate(rows):
        tr.append(r[2]-r[3] if i==0 else max(r[2]-r[3],abs(r[2]-rows[i-1][4]),abs(r[3]-rows[i-1][4])))
        if i>=n:out[i]=sum(tr[i-n+1:i+1])/n
    return out
def features(rows):
    c=[r[4] for r in rows]; v=[r[5] for r in rows]
    e20=series_ema(c,20); e50=series_ema(c,50); e200=series_ema(c,200)
    rs=series_rsi(c); at=series_atr(rows)
    scores=[]
    for i in range(len(rows)):
        s=0; lo=max(0,i-19); va=sum(v[lo:i+1])/(i-lo+1)
        if e20[i]>e50[i]:s+=20
        if e50[i]>e200[i]:s+=20
        if c[i]>e20[i]:s+=15
        if 50<=rs[i]<=68:s+=15
        if rs[i]<35:s-=10
        if rs[i]>75:s-=10
        if v[i]>va:s+=10
        if c[i]>e50[i]:s+=10
        scores.append(max(0,min(100,s)))
    return scores,rs,at,e20

async def klines(limit=1000,start=None,end=None):
    p={"symbol":SYMBOL,"interval":INTERVAL,"limit":min(limit,1000)}
    if start is not None:p["startTime"]=start
    if end is not None:p["endTime"]=end
    async with httpx.AsyncClient(timeout=20) as c:
        r=await c.get("https://api.binance.com/api/v3/klines",params=p); r.raise_for_status()
    return [[int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])] for x in r.json()]

async def history(days):
    now=datetime.now(timezone.utc); end=int(now.timestamp()*1000); cur=int((now-timedelta(days=days)).timestamp()*1000); d={}
    while cur<end:
        b=await klines(1000,cur,end)
        if not b:break
        for x in b:d[x[0]]=x
        nxt=b[-1][0]+300000
        if nxt<=cur:break
        cur=nxt
        if len(b)<1000:break
    return [d[k] for k in sorted(d) if k+300000<=end]

def research(rows):
    scores,rs,at,e20=features(rows)
    sims={t:{"threshold":t,"cash":START,"pos":None,"trades":0,"wins":0,"fees":0.,"peak":START,"dd":0.,"pnls":[]} for t in THRESHOLDS}
    for i in range(200,len(rows)):
        p=rows[i][4]
        for z in sims.values():
            pos=z["pos"]
            if pos and (p<=pos["stop"] or p>=pos["target"]):
                ep=pos["stop"] if p<=pos["stop"] else pos["target"]
                fee=pos["qty"]*ep*FEE
                net=(ep-pos["entry"])*pos["qty"]-pos["entry_fee"]-fee
                z["cash"]+=pos["qty"]*ep-fee; z["fees"]+=fee; z["trades"]+=1
                if net>0:z["wins"]+=1
                z["pnls"].append(net); z["pos"]=None
            if z["pos"] is None and scores[i]>=z["threshold"] and p>e20[i]:
                rc=max(.01,z["cash"]*RISK); dist=max(at[i]*STOP_ATR,p*.001); q=rc/dist; ef=q*p*FEE
                if q*p+ef<=z["cash"]:
                    z["cash"]-=q*p+ef; z["fees"]+=ef
                    z["pos"]={"entry":p,"qty":q,"stop":p-dist,"target":p+dist*RR,"entry_fee":ef}
            eq=z["cash"]+(z["pos"]["qty"]*p if z["pos"] else 0)
            z["peak"]=max(z["peak"],eq); z["dd"]=max(z["dd"],(z["peak"]-eq)/z["peak"]*100)
    out=[]
    for z in sims.values():
        end=z["cash"]+(z["pos"]["qty"]*rows[-1][4] if z["pos"] else 0); n=z["trades"]
        out.append({"threshold":z["threshold"],"ending_equity":end,"return_pct":(end/START-1)*100,"trades":n,"wins":z["wins"],"losses":n-z["wins"],"win_rate_pct":z["wins"]/n*100 if n else 0,"max_drawdown_pct":z["dd"],"fees":z["fees"],"avg_trade":sum(z["pnls"])/len(z["pnls"]) if z["pnls"] else 0,"open_position":bool(z["pos"])})
    return {"days":round((rows[-1][0]-rows[0][0])/86400000,1),"candles":len(rows),"tests":out}

async def scan():
    try:
        rows=await klines(220); s,rs,at,e20=features(rows); i=len(rows)-1; p=rows[-1][4]
        state.update(price=p,score=s[i],rsi=rs[i],trend="BULLISH" if e20[i]>ema([x[4] for x in rows],50) else "BEARISH",last_scan=datetime.now(timezone.utc).isoformat(),error=None,signal="BUY" if s[i]>=75 else "WAIT")
    except Exception as e:state["error"]=str(e)

@app.get("/api/status")
async def status():return JSONResponse(state)
@app.get("/api/research")
async def api_research(days:int=Query(90,ge=30,le=90)):
    try:return JSONResponse(research(await history(days)))
    except Exception as e:return JSONResponse({"error":str(e)},status_code=500)

@app.get("/",response_class=HTMLResponse)
async def home():
 return HTMLResponse("""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>AI BTC Scout V4.1</title><style>body{background:#080b10;color:#eee;font:16px Arial;max-width:950px;margin:auto;padding:20px}.c{background:#121821;border:1px solid #293545;border-radius:20px;padding:20px;margin:14px 0}.g{display:grid;grid-template-columns:1fr 1fr;gap:14px}.b{font-size:28px;font-weight:bold}button{padding:12px;border:0;border-radius:10px;margin:3px}@media(max-width:600px){.g{grid-template-columns:1fr}table{font-size:12px}}</style><div class=c><h1>ð¤ AI BTC Scout V4.1</h1><p>BTC/USDT Â· 5-minute Â· PAPER ONLY</p><h2 id=s>WAIT</h2><div class=b id=p>$--</div></div><div class=g><div class=c>AI score<div class=b id=sc>--</div></div><div class=c>Trend<div class=b id=t>--</div></div><div class=c>RSI<div class=b id=r>--</div></div><div class=c>Equity<div class=b id=e>$20.00</div></div></div><div class=c><h2>V4.1 Strategy Research</h2><p>One indicator calculation, then five thresholds. Paper research only.</p><button onclick=go()>Run 90-day research</button><div id=out>Ready.</div></div><div class=c>Paper testing only. No exchange orders or API keys. Historical results do not guarantee future performance.</div><script>
async function q(){let x=await(await fetch('/api/status')).json();p.textContent='$'+(+x.price).toLocaleString();sc.textContent=x.score;t.textContent=x.trend;r.textContent=(+x.rsi).toFixed(1);e.textContent='$'+(+x.equity).toFixed(2);s.textContent=x.signal}async function go(){out.textContent='Running 90-day research...';let x=await(await fetch('/api/research?days=90')).json();if(x.error){out.textContent='Research error: '+x.error;return}let h='<p>Period: '+x.days+' days Â· '+x.candles.toLocaleString()+' candles</p><div style="overflow:auto"><table border=1 cellpadding=7><tr><th>Score</th><th>End $</th><th>Return</th><th>Trades</th><th>Win%</th><th>DD</th><th>Fees</th><th>Avg/trade</th></tr>';for(let z of x.tests)h+='<tr><td>â¥'+z.threshold+'</td><td>$'+z.ending_equity.toFixed(2)+'</td><td>'+z.return_pct.toFixed(2)+'%</td><td>'+z.trades+'</td><td>'+z.win_rate_pct.toFixed(1)+'%</td><td>'+z.max_drawdown_pct.toFixed(2)+'%</td><td>$'+z.fees.toFixed(4)+'</td><td>$'+z.avg_trade.toFixed(4)+'</td></tr>';out.innerHTML=h+'</table></div>'}q();setInterval(q,60000)</script>""")
@app.on_event("startup")
async def startup():asyncio.create_task(loop())
async def loop():
    while True:await scan();await asyncio.sleep(60)



