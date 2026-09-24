import asyncio
from datetime import datetime, timezone, timedelta
import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="AI BTC Scout V5")
SYMBOL="BTCUSDT"; INTERVAL="5m"; FEE=.001; START=20.; RISK=.01; STOP_ATR=1.5; RR=2.
LIVE_THRESHOLD=65; THRESHOLDS=[55,60,65,70]

state={"equity":START,"cash":START,"position":None,"last_scan":None,
       "signal":"WAIT","score":0,"price":0.,"rsi":50.,"macd":0.,
       "trend":"UNKNOWN","error":None}

def ema(a,n):
    e=a[0]; k=2/(n+1); out=[]
    for x in a:
        e=x if not out else x*k+e*(1-k); out.append(e)
    return out

def rsi(a,n=14):
    out=[50.]*len(a)
    if len(a)<=n:return out
    g=[max(a[i]-a[i-1],0) for i in range(1,len(a))]
    l=[max(a[i-1]-a[i],0) for i in range(1,len(a))]
    ag=sum(g[:n])/n; al=sum(l[:n])/n
    out[n]=100. if al==0 else 100-100/(1+ag/al)
    for i in range(n,len(g)):
        ag=(ag*(n-1)+g[i])/n; al=(al*(n-1)+l[i])/n
        out[i+1]=100. if al==0 else 100-100/(1+ag/al)
    return out

def atr(rows,n=14):
    tr=[]
    for i,x in enumerate(rows):
        tr.append(x[2]-x[3] if i==0 else max(x[2]-x[3],abs(x[2]-rows[i-1][4]),abs(x[3]-rows[i-1][4])))
    out=[0.]*len(rows)
    for i in range(n,len(rows)): out[i]=sum(tr[i-n+1:i+1])/n
    return out

def avg(a,n):
    out=[]; total=0.
    for i,x in enumerate(a):
        total+=x
        if i>=n: total-=a[i-n]
        out.append(total/min(i+1,n))
    return out

def indicators(rows):
    c=[x[4] for x in rows]; h=[x[2] for x in rows]; l=[x[3] for x in rows]; v=[x[5] for x in rows]
    e20=ema(c,20); e50=ema(c,50); e200=ema(c,200)
    e12=ema(c,12); e26=ema(c,26); mac=[a-b for a,b in zip(e12,e26)]
    ms=ema(mac,9); hist=[a-b for a,b in zip(mac,ms)]
    rs=rsi(c); at=atr(rows); va=avg(v,20)
    sup=[0.]*len(rows); res=[0.]*len(rows)
    for i in range(len(rows)):
        a=max(0,i-20); b=i
        if b>a:
            sup[i]=min(l[a:b]); res[i]=max(h[a:b])
        else: sup[i]=l[i]; res[i]=h[i]
    score=[]; trend=[]
    for i in range(len(rows)):
        s=0
        if e20[i]>e50[i]>e200[i]: s+=25; trend.append("BULLISH")
        elif e20[i]<e50[i]<e200[i]: trend.append("BEARISH")
        else: trend.append("MIXED")
        if c[i]>e20[i]: s+=10
        if hist[i]>0: s+=15
        if 50<=rs[i]<=70: s+=15
        elif rs[i]<35: s-=5
        elif rs[i]>75: s-=10
        if v[i]>va[i]: s+=10
        if c[i]>res[i] and res[i]>0: s+=15
        elif c[i]>sup[i] and sup[i]>0: s+=5
        score.append(max(0,min(100,s)))
    return {"close":c,"e20":e20,"hist":hist,"rsi":rs,"atr":at,"score":score,"trend":trend}

async def klines(limit=1000,start=None,end=None):
    p={"symbol":SYMBOL,"interval":INTERVAL,"limit":min(limit,1000)}
    if start is not None:p["startTime"]=start
    if end is not None:p["endTime"]=end
    async with httpx.AsyncClient(timeout=25) as c:
        r=await c.get("https://api.binance.com/api/v3/klines",params=p); r.raise_for_status()
        data=r.json()
    return [[int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])] for x in data]

async def history(days):
    now=datetime.now(timezone.utc); end=int(now.timestamp()*1000)
    cur=int((now-timedelta(days=days)).timestamp()*1000); d={}
    while cur<end:
        b=await klines(1000,cur,end)
        if not b: break
        for x in b:d[x[0]]=x
        nxt=b[-1][0]+300000
        if nxt<=cur: break
        cur=nxt
        if len(b)<1000: break
    return [d[k] for k in sorted(d) if k+300000<=end]

def simulate(rows,I,t,a,b):
    cash=START; pos=None; trades=wins=0; fees=0.; peak=START; dd=0.; pnls=[]
    for i in range(a,b+1):
        p=I["close"][i]
        if pos and (p<=pos["stop"] or p>=pos["target"]):
            ep=pos["stop"] if p<=pos["stop"] else pos["target"]
            ef=pos["qty"]*ep*FEE
            net=(ep-pos["entry"])*pos["qty"]-pos["entry_fee"]-ef
            cash+=pos["qty"]*ep-ef; fees+=ef; trades+=1
            if net>0:wins+=1
            pnls.append(net); pos=None
        if pos is None and I["score"][i]>=t and p>I["e20"][i] and I["atr"][i]>0:
            risk=max(.01,cash*RISK); dist=max(I["atr"][i]*STOP_ATR,p*.001)
            qty=risk/dist; ef=qty*p*FEE
            if qty*p+ef<=cash:
                cash-=qty*p+ef; fees+=ef
                pos={"entry":p,"qty":qty,"stop":p-dist,"target":p+dist*RR,"entry_fee":ef}
        eq=cash+(pos["qty"]*p if pos else 0); peak=max(peak,eq); dd=max(dd,(peak-eq)/peak*100)
    end=cash+(pos["qty"]*I["close"][b] if pos else 0); n=trades
    return {"threshold":t,"ending_equity":end,"return_pct":(end/START-1)*100,
            "trades":n,"wins":wins,"losses":n-wins,"win_rate_pct":wins/n*100 if n else 0,
            "max_drawdown_pct":dd,"fees":fees,"avg_trade":sum(pnls)/len(pnls) if pnls else 0}

async def research(days):
    rows=await history(days)
    if len(rows)<500: raise RuntimeError("Not enough historical candles.")
    I=indicators(rows); split=max(300,int(len(rows)*.70))
    return {"days":round((rows[-1][0]-rows[0][0])/86400000,1),"candles":len(rows),
            "train":[simulate(rows,I,t,200,split-1) for t in THRESHOLDS],
            "test":[simulate(rows,I,t,split,len(rows)-1) for t in THRESHOLDS]}

async def scan():
    try:
        rows=await klines(220); I=indicators(rows); i=len(rows)-1; p=I["close"][i]
        state.update(price=p,score=I["score"][i],rsi=I["rsi"][i],macd=I["hist"][i],
                     trend=I["trend"][i],last_scan=datetime.now(timezone.utc).isoformat(),error=None)
        state["signal"]="BUY" if I["score"][i]>=LIVE_THRESHOLD and p>I["e20"][i] else "WAIT"
    except Exception as e: state["error"]=str(e)

@app.get("/api/status")
async def status(): return JSONResponse(state)

@app.get("/api/research")
async def api_research(days:int=Query(90,ge=30,le=180)):
    try:return JSONResponse(await research(days))
    except Exception as e:return JSONResponse({"error":str(e)},status_code=500)

@app.get("/",response_class=HTMLResponse)
async def home():
    return HTMLResponse(r'''<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<style>
body{background:#080b10;color:#eee;font:16px Arial;max-width:980px;margin:auto;padding:18px}
.c{background:#121821;border:1px solid #293545;border-radius:20px;padding:20px;margin:14px 0}
.g{display:grid;grid-template-columns:1fr 1fr;gap:14px}.b{font-size:28px;font-weight:bold}
button{padding:13px 16px;border:0;border-radius:10px;margin:4px}.wrap{overflow:auto}
table{border-collapse:collapse;width:100%;min-width:720px}th,td{border:1px solid #586575;padding:7px;text-align:center}
@media(max-width:600px){.g{grid-template-columns:1fr}}
</style>
<div class=c><h1>ð¤ AI BTC Scout V5</h1><p>BTC/USDT Â· 5-minute Â· PAPER ONLY</p><h2 id=s>WAIT</h2><div class=b id=p>$--</div></div>
<div class=g><div class=c>AI score<div class=b id=sc>--</div></div><div class=c>Trend<div class=b id=t>--</div></div>
<div class=c>RSI<div class=b id=r>--</div></div><div class=c>MACD histogram<div class=b id=m>--</div></div>
<div class=c>Paper equity<div class=b id=e>$20.00</div></div></div>
<div class=c><h2>V5 Strategy Research</h2>
<p>MACD + EMA trend + RSI + volume + prior-candle support/resistance. Results are split 70% training / 30% unseen test data.</p>
<button onclick=go(90)>Run 90 days</button><button onclick=go(180)>Run 180 days</button><div id=out>Ready.</div></div>
<div class=c>Paper testing only. No exchange orders or API keys. Historical results do not guarantee future performance.</div>
<script>
async function q(){let x=await(await fetch('/api/status')).json();p.textContent='$'+(+x.price).toLocaleString();sc.textContent=x.score;t.textContent=x.trend;r.textContent=(+x.rsi).toFixed(1);m.textContent=(+x.macd).toFixed(2);e.textContent='$'+(+x.equity).toFixed(2);s.textContent=x.signal}
function tab(a){let h='<div class=wrap><table><tr><th>Score</th><th>End $</th><th>Return</th><th>Trades</th><th>Win%</th><th>DD</th><th>Fees</th><th>Avg/trade</th></tr>';for(let z of a)h+='<tr><td>â¥'+z.threshold+'</td><td>$'+z.ending_equity.toFixed(2)+'</td><td>'+z.return_pct.toFixed(2)+'%</td><td>'+z.trades+'</td><td>'+z.win_rate_pct.toFixed(1)+'%</td><td>'+z.max_drawdown_pct.toFixed(2)+'%</td><td>$'+z.fees.toFixed(4)+'</td><td>$'+z.avg_trade.toFixed(4)+'</td></tr>';return h+'</table></div>'}
async function go(d){out.textContent='Running '+d+'-day V5 research...';let x=await(await fetch('/api/research?days='+d)).json();if(x.error){out.textContent='Research error: '+x.error;return}out.innerHTML='<p><b>'+x.days+' days Â· '+x.candles.toLocaleString()+' candles</b></p><h3>Training period (70%)</h3>'+tab(x.train)+'<h3>Unseen test period (30%)</h3>'+tab(x.test)+'<p>The unseen test results are validation only, not a prediction.</p>'}
q();setInterval(q,60000)
</script>''')

@app.on_event("startup")
async def startup(): asyncio.create_task(loop())

async def loop():
    while True:
        await scan()
        await asyncio.sleep(60)




