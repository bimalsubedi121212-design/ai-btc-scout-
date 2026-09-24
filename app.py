import asyncio
from datetime import datetime, timezone, timedelta
import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

app=FastAPI(title="AI BTC Scout V6")
SYMBOL="BTCUSDT"; TF="5m"; FEE=.001; START=20.; RISK=.01; STOP_ATR=1.5
TARGETS=[1.5,2.0,2.5]; THRESHOLDS=[50,55,60,65]
state={"price":0.,"equity":20.,"score":0,"rsi":50.,"trend":"UNKNOWN","signal":"WAIT","last_scan":None,"error":None}

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
    out[n]=100 if al==0 else 100-100/(1+ag/al)
    for i in range(n,len(g)):
        ag=(ag*(n-1)+g[i])/n; al=(al*(n-1)+l[i])/n
        out[i+1]=100 if al==0 else 100-100/(1+ag/al)
    return out

def atr(rows,n=14):
    tr=[]
    for i,x in enumerate(rows):
        tr.append(x[2]-x[3] if i==0 else max(x[2]-x[3],abs(x[2]-rows[i-1][4]),abs(x[3]-rows[i-1][4])))
    out=[0.]*len(rows)
    for i in range(n,len(rows)):out[i]=sum(tr[i-n+1:i+1])/n
    return out

def avg(a,n):
    out=[]; s=0.
    for i,x in enumerate(a):
        s+=x
        if i>=n:s-=a[i-n]
        out.append(s/min(i+1,n))
    return out

async def klines(limit=1000,start=None,end=None,interval=TF):
    p={"symbol":SYMBOL,"interval":interval,"limit":min(limit,1000)}
    if start is not None:p["startTime"]=start
    if end is not None:p["endTime"]=end
    async with httpx.AsyncClient(timeout=25) as c:
        r=await c.get("https://api.binance.com/api/v3/klines",params=p); r.raise_for_status()
        d=r.json()
    return [[int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])] for x in d]

async def hist(days,interval=TF):
    now=datetime.now(timezone.utc); end=int(now.timestamp()*1000)
    step=300000 if interval=="5m" else 3600000
    cur=int((now-timedelta(days=days)).timestamp()*1000); d={}
    while cur<end:
        b=await klines(1000,cur,end,interval)
        if not b:break
        for x in b:d[x[0]]=x
        nxt=b[-1][0]+step
        if nxt<=cur:break
        cur=nxt
        if len(b)<1000:break
    return [d[k] for k in sorted(d) if k+step<=end]

def features(rows):
    c=[x[4] for x in rows]; v=[x[5] for x in rows]
    e20=ema(c,20); e50=ema(c,50); e200=ema(c,200); rs=rsi(c); at=atr(rows); va=avg(v,20)
    score=[]
    for i in range(len(rows)):
        s=0
        if c[i]>e20[i]:s+=20
        if e20[i]>e50[i]:s+=20
        if e50[i]>e200[i]:s+=20
        if 45<=rs[i]<=68:s+=15
        if v[i]>va[i]:s+=10
        if rs[i]>75:s-=10
        if rs[i]<30:s-=5
        score.append(max(0,min(100,s)))
    return c,e20,e50,e200,rs,at,score

def simulate(rows,I,threshold,target_r,start_i,end_i):
    c,e20,e50,e200,rs,at,score=I
    cash=START; pos=None; trades=wins=0; fees=0.; peak=START; dd=0.; pnls=[]; cooldown=0
    for i in range(start_i,end_i+1):
        p=c[i]
        if pos:
            hit_stop=p<=pos["stop"]; hit_target=p>=pos["target"]
            if hit_stop or hit_target:
                # Conservative if both are theoretically hit in a candle: stop first.
                ep=pos["stop"] if hit_stop else pos["target"]
                ef=pos["qty"]*ep*FEE; net=(ep-pos["entry"])*pos["qty"]-pos["entry_fee"]-ef
                cash+=pos["qty"]*ep-ef; fees+=ef; trades+=1
                if net>0:wins+=1
                pnls.append(net); pos=None; cooldown=3
        if cooldown>0:cooldown-=1
        if pos is None and cooldown==0 and score[i]>=threshold and c[i]>e20[i] and e20[i]>e50[i] and at[i]>0:
            risk=max(.01,cash*RISK); dist=max(at[i]*STOP_ATR,p*.001)
            qty=risk/dist; ef=qty*p*FEE
            if qty*p+ef<=cash:
                cash-=qty*p+ef; fees+=ef
                pos={"entry":p,"qty":qty,"stop":p-dist,"target":p+dist*target_r,"entry_fee":ef}
        eq=cash+(pos["qty"]*p if pos else 0); peak=max(peak,eq); dd=max(dd,(peak-eq)/peak*100)
    end=cash+(pos["qty"]*c[end_i] if pos else 0); n=trades
    return {"threshold":threshold,"target_R":target_r,"ending_equity":end,"return_pct":(end/START-1)*100,
            "trades":n,"wins":wins,"losses":n-wins,"win_rate_pct":wins/n*100 if n else 0,
            "max_drawdown_pct":dd,"fees":fees,"avg_trade":sum(pnls)/len(pnls) if pnls else 0}

async def research(days):
    rows=await hist(days); 
    if len(rows)<500:raise RuntimeError("Not enough candles.")
    I=features(rows); split=max(300,int(len(rows)*.70))
    train=[]; test=[]
    for t in THRESHOLDS:
        for r in TARGETS:
            train.append(simulate(rows,I,t,r,200,split-1))
            test.append(simulate(rows,I,t,r,split,len(rows)-1))
    return {"days":round((rows[-1][0]-rows[0][0])/86400000,1),"candles":len(rows),
            "train":train,"test":test}

async def scan():
    try:
        rows=await klines(220); I=features(rows); c,e20,e50,e200,rs,at,score=I; i=len(rows)-1
        state.update(price=c[i],score=score[i],rsi=rs[i],trend="BULLISH" if e20[i]>e50[i]>e200[i] else ("BEARISH" if e20[i]<e50[i]<e200[i] else "MIXED"),last_scan=datetime.now(timezone.utc).isoformat(),error=None)
        state["signal"]="BUY" if score[i]>=65 and c[i]>e20[i] and e20[i]>e50[i] else "WAIT"
    except Exception as e:state["error"]=str(e)

@app.get("/api/status")
async def status():return JSONResponse(state)

@app.get("/api/research")
async def api_research(days:int=Query(180,ge=90,le=365)):
    try:return JSONResponse(await research(days))
    except Exception as e:return JSONResponse({"error":str(e)},status_code=500)

@app.get("/",response_class=HTMLResponse)
async def home():
    return HTMLResponse(r'''<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<style>
body{background:#080b10;color:#eee;font:16px Arial;max-width:1000px;margin:auto;padding:18px}
.c{background:#121821;border:1px solid #293545;border-radius:20px;padding:20px;margin:14px 0}
.g{display:grid;grid-template-columns:1fr 1fr;gap:14px}.b{font-size:28px;font-weight:bold}
button{padding:13px 16px;border:0;border-radius:10px;margin:4px}.wrap{overflow:auto}
table{border-collapse:collapse;width:100%;min-width:900px}th,td{border:1px solid #586575;padding:7px;text-align:center}
@media(max-width:600px){.g{grid-template-columns:1fr}}
</style>
<div class=c><h1>ð¤ AI BTC Scout V6</h1><p>BTC/USDT Â· 5-minute Â· PAPER ONLY</p><h2 id=s>WAIT</h2><div class=b id=p>$--</div></div>
<div class=g><div class=c>Score<div class=b id=sc>--</div></div><div class=c>Trend<div class=b id=t>--</div></div>
<div class=c>RSI<div class=b id=r>--</div></div><div class=c>Paper equity<div class=b id=e>$20.00</div></div></div>
<div class=c><h2>V6 Strategy Research</h2><p>Flexible entry thresholds + 1.5R/2R/2.5R targets, 3-candle cooldown, and 70/30 train/test split.</p>
<button onclick=go(180)>Run 180 days</button><button onclick=go(365)>Run 365 days</button><div id=out>Ready.</div></div>
<div class=c>Paper testing only. No exchange orders or API keys. Historical results do not guarantee future performance.</div>
<script>
async function q(){let x=await(await fetch('/api/status')).json();p.textContent='$'+(+x.price).toLocaleString();sc.textContent=x.score;t.textContent=x.trend;r.textContent=(+x.rsi).toFixed(1);e.textContent='$'+(+x.equity).toFixed(2);s.textContent=x.signal}
function tab(a){let h='<div class=wrap><table><tr><th>Score</th><th>Target</th><th>End $</th><th>Return</th><th>Trades</th><th>Win%</th><th>DD</th><th>Fees</th><th>Avg/trade</th></tr>';for(let z of a)h+='<tr><td>â¥'+z.threshold+'</td><td>'+z.target_R+'R</td><td>$'+z.ending_equity.toFixed(2)+'</td><td>'+z.return_pct.toFixed(2)+'%</td><td>'+z.trades+'</td><td>'+z.win_rate_pct.toFixed(1)+'%</td><td>'+z.max_drawdown_pct.toFixed(2)+'%</td><td>$'+z.fees.toFixed(4)+'</td><td>$'+z.avg_trade.toFixed(4)+'</td></tr>';return h+'</table></div>'}
async function go(d){out.textContent='Running '+d+'-day V6 research...';let x=await(await fetch('/api/research?days='+d)).json();if(x.error){out.textContent='Research error: '+x.error;return}out.innerHTML='<p><b>'+x.days+' days Â· '+x.candles.toLocaleString()+' candles</b></p><h3>Training period (70%)</h3>'+tab(x.train)+'<h3>Unseen test period (30%)</h3>'+tab(x.test)+'<p>Compare trade counts and unseen-test behavior. This is not a prediction.</p>'}
q();setInterval(q,60000)
</script>''')

@app.on_event("startup")
async def startup():asyncio.create_task(loop())
async def loop():
    while True:
        await scan()
        await asyncio.sleep(60)





