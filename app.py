import asyncio, sqlite3
from datetime import datetime, timezone, timedelta
import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

app=FastAPI(title="AI BTC Scout FINAL")
SYMBOL="BTCUSDT"; START=20.0; FEE=0.001; SLIP=0.0005; RISK=0.01
ATR_MULT=1.5; RR=2.0; COOLDOWN=6; MAX_DAILY_LOSS=0.03
DB="bot.db"
state={"equity":START,"cash":START,"price":0.0,"score":0,"rsi":50.0,
       "trend":"UNKNOWN","signal":"WAIT","last_scan":None,"error":None}

def ema(a,n):
    e=a[0]; k=2/(n+1); out=[]
    for x in a:
        e=x if not out else x*k+e*(1-k); out.append(e)
    return out

def rsi(a,n=14):
    out=[50.0]*len(a)
    if len(a)<=n:return out
    g=[max(a[i]-a[i-1],0) for i in range(1,len(a))]
    l=[max(a[i-1]-a[i],0) for i in range(1,len(a))]
    ag=sum(g[:n])/n; al=sum(l[:n])/n
    for j in range(n,len(a)):
        if j>n:
            ag=(ag*(n-1)+g[j-1])/n; al=(al*(n-1)+l[j-1])/n
        out[j]=100 if al==0 else 100-100/(1+ag/al)
    return out

def atr(rows,n=14):
    tr=[]
    for i,x in enumerate(rows):
        tr.append(x[2]-x[3] if i==0 else max(x[2]-x[3],abs(x[2]-rows[i-1][4]),abs(x[3]-rows[i-1][4])))
    out=[0.0]*len(rows)
    for i in range(n-1,len(rows)): out[i]=sum(tr[i-n+1:i+1])/n
    return out

def avg(a,n):
    out=[]; s=0.0
    for i,x in enumerate(a):
        s+=x
        if i>=n:s-=a[i-n]
        out.append(s/min(i+1,n))
    return out

async def candles(interval="5m",limit=1000,start=None,end=None):
    p={"symbol":SYMBOL,"interval":interval,"limit":min(limit,1000)}
    if start is not None:p["startTime"]=start
    if end is not None:p["endTime"]=end
    async with httpx.AsyncClient(timeout=25) as c:
        r=await c.get("https://api.binance.com/api/v3/klines",params=p); r.raise_for_status()
        d=r.json()
    return [[int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])] for x in d]

async def history(days):
    now=datetime.now(timezone.utc); end=int(now.timestamp()*1000)
    cur=int((now-timedelta(days=days)).timestamp()*1000); step=300000; d={}
    while cur<end:
        b=await candles("5m",1000,cur,end)
        if not b: break
        for x in b:d[x[0]]=x
        nxt=b[-1][0]+step
        if nxt<=cur: break
        cur=nxt
        if len(b)<1000: break
    return [d[k] for k in sorted(d) if k+step<=end]

def ind(rows):
    c=[x[4] for x in rows]; v=[x[5] for x in rows]
    return c,ema(c,20),ema(c,50),ema(c,200),rsi(c),atr(rows),avg(v,20)

def setup(rows,I,i):
    c,e20,e50,e200,rs,at,va=I
    if i<205 or at[i]<=0:return False,0
    p=c[i]; s=0
    if p>e20[i]:s+=20
    if e20[i]>e50[i]:s+=20
    if 45<=rs[i]<=68:s+=15
    if rows[i][5]>=va[i]*0.8:s+=10
    if p>rows[i-1][2]:s+=20
    if p>max(x[2] for x in rows[i-3:i]):s+=15
    if p>e20[i]+1.5*at[i]:s-=15
    return s>=60,max(0,min(100,s))

def simulate(rows):
    I=ind(rows); c,e20,e50,e200,rs,at,va=I
    cash=START; pos=None; peak=START; dd=0; cooldown=0; trades=[]; day_pnl=0; day=None
    for i in range(205,len(rows)):
        o,h,l,cl=rows[i][1:5]
        d=datetime.fromtimestamp(rows[i][0]/1000,timezone.utc).date()
        if d!=day:day=d;day_pnl=0
        if pos:
            exitp=None; reason=""
            if l<=pos["stop"]:exitp=pos["stop"];reason="STOP"
            elif h>=pos["target"]:exitp=pos["target"];reason="TARGET"
            if exitp is not None:
                exitp*=1-SLIP; fee=exitp*pos["qty"]*FEE
                pnl=(exitp-pos["entry"])*pos["qty"]-pos["entry_fee"]-fee
                cash+=exitp*pos["qty"]-fee; day_pnl+=pnl
                trades.append((rows[i][0],pnl,reason));pos=None;cooldown=COOLDOWN
        if cooldown:cooldown-=1
        if pos is None and cooldown==0 and day_pnl>-START*MAX_DAILY_LOSS:
            ok,_=setup(rows,I,i)
            if ok:
                dist=at[i]*ATR_MULT; risk=max(cash*RISK,0.01); qty=risk/dist
                entry=o*(1+SLIP); ef=entry*qty*FEE
                if qty*entry+ef<=cash:
                    cash-=qty*entry+ef
                    pos={"entry":entry,"qty":qty,"stop":entry-dist,"target":entry+dist*RR,"entry_fee":ef}
        eq=cash+(pos["qty"]*cl if pos else 0);peak=max(peak,eq);dd=max(dd,(peak-eq)/peak*100)
    end=cash+(pos["qty"]*c[-1] if pos else 0)
    wins=sum(1 for x in trades if x[1]>0)
    return {"ending":end,"return_pct":(end/START-1)*100,"trades":len(trades),
            "wins":wins,"losses":len(trades)-wins,"win_rate":wins/len(trades)*100 if trades else 0,
            "max_dd":dd,"fees":sum(abs(x[1])*0 for x in trades),"open_end":bool(pos)}

async def scan():
    try:
        rows=await candles("5m",220); I=ind(rows); c,e20,e50,e200,rs,at,va=I; i=len(rows)-1
        h=await candles("1h",220); hc,he20,he50,he200,_,_,_=ind(h)
        bull=he20[-1]>he50[-1]>he200[-1]
        ok,score=setup(rows,I,i); ok=ok and bull
        state.update(price=c[i],score=score,rsi=rs[i],
                     trend="BULLISH" if bull else "NOT BULLISH",
                     signal="BUY" if ok else "WAIT",
                     last_scan=datetime.now(timezone.utc).isoformat(),error=None)
    except Exception as e: state["error"]=str(e)

@app.get("/api/status")
async def status(): return JSONResponse(state)

@app.get("/api/backtest")
async def backtest(days:int=Query(180,ge=90,le=365)):
    try:
        rows=await history(days); split=int(len(rows)*.70)
        return JSONResponse({"days":days,"candles":len(rows),
            "train":simulate(rows[:split]),"test":simulate(rows[split:])})
    except Exception as e:return JSONResponse({"error":str(e)},status_code=500)

@app.get("/",response_class=HTMLResponse)
async def home():
    return HTMLResponse("""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<style>body{background:#080b10;color:#eee;font:16px Arial;max-width:900px;margin:auto;padding:16px}.c{background:#121821;border:1px solid #293545;border-radius:20px;padding:20px;margin:14px 0}.g{display:grid;grid-template-columns:1fr 1fr;gap:12px}.b{font-size:28px;font-weight:bold}button{padding:13px 16px;border:0;border-radius:10px;margin:4px}@media(max-width:600px){.g{grid-template-columns:1fr}}</style>
<div class=c><h1>AI BTC Scout FINAL</h1><p>BTC/USDT Â· 5m entry + 1h trend Â· PAPER ONLY</p><h2 id=s>WAIT</h2><div class=b id=p>$--</div></div>
<div class=g><div class=c>Score<div class=b id=sc>--</div></div><div class=c>Trend<div class=b id=t>--</div></div><div class=c>RSI<div class=b id=r>--</div></div><div class=c>Equity<div class=b id=e>$20.00</div></div></div>
<div class=c><h2>Final validation</h2><button onclick=run(180)>Run 180 days</button><button onclick=run(365)>Run 365 days</button><div id=o>Ready.</div></div>
<div class=c>Paper-only. No exchange orders, no API keys, no leverage. Backtests include fees/slippage assumptions and use stop-first when both stop and target occur within a candle.</div>
<script>
async function q(){let x=await(await fetch('/api/status')).json();p.textContent='$'+(+x.price).toLocaleString();sc.textContent=x.score;t.textContent=x.trend;r.textContent=(+x.rsi).toFixed(1);e.textContent='$'+(+x.equity).toFixed(2);s.textContent=x.signal}
async function run(d){o.textContent='Running '+d+'-day validation...';let x=await(await fetch('/api/backtest?days='+d)).json();if(x.error){o.textContent=x.error;return}let a=x.test;o.innerHTML='<p>'+x.candles.toLocaleString()+' candles</p><h3>Training 70%</h3>Ending $'+x.train.ending.toFixed(2)+' Â· Return '+x.train.return_pct.toFixed(2)+'% Â· Trades '+x.train.trades+' Â· DD '+x.train.max_dd.toFixed(2)+'%</h3><h3>Unseen test 30%</h3>Ending $'+a.ending.toFixed(2)+' Â· Return '+a.return_pct.toFixed(2)+'% Â· Trades '+a.trades+' Â· Win rate '+a.win_rate.toFixed(1)+'% Â· DD '+a.max_dd.toFixed(2)+'%<p>Test data is for validation, not parameter selection.</p>'}
q();setInterval(q,60000)
</script>""")

@app.on_event("startup")
async def startup(): asyncio.create_task(loop())

async def loop():
    while True:
        await scan(); await asyncio.sleep(60)






