import os, sqlite3, asyncio, math, statistics
from datetime import datetime, timezone, timedelta
from typing import Optional
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

BINANCE = "https://api.binance.com/api/v3/klines"
SYMBOL = "BTCUSDT"
START_EQUITY = 20.0
FEE = 0.0010
SLIPPAGE = 0.0005
RISK_PCT = 0.01
STOP_ATR = 1.5
TARGET_R = 2.0
COOLDOWN_CANDLES = 6
DAILY_LOSS_CAP = 0.03
DB_PATH = os.getenv("DB_PATH", "paper_trading.db")

app = FastAPI(title="AI BTC Scout Final")
state = {
    "price": None, "score": 0, "trend": "UNKNOWN", "rsi": None,
    "paper_equity": START_EQUITY, "cash": START_EQUITY,
    "position": None, "last_candle": None, "last_error": None,
    "last_scan": None, "cooldown": 0, "day": None, "day_start_equity": START_EQUITY, "pending": None, "processed": None,
}

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS account (
        id INTEGER PRIMARY KEY CHECK(id=1), cash REAL NOT NULL, equity REAL NOT NULL,
        peak REAL NOT NULL, max_dd REAL NOT NULL, updated TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS positions (
        id INTEGER PRIMARY KEY CHECK(id=1), entry REAL, qty REAL, stop REAL,
        target REAL, entry_fee REAL, entry_time TEXT, entry_candle INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT, entry_time TEXT, exit_time TEXT,
        entry REAL, exit REAL, qty REAL, stop REAL, target REAL,
        gross_pnl REAL, fees REAL, net_pnl REAL, reason TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, candle INTEGER,
        score INTEGER, action TEXT, price REAL, reason TEXT)""")
    c.commit()
    return c

def load_state():
    c = db()
    row = c.execute("SELECT * FROM account WHERE id=1").fetchone()
    if row:
        state["cash"] = row["cash"]; state["paper_equity"] = row["equity"]
    else:
        now = datetime.now(timezone.utc).isoformat()
        c.execute("INSERT INTO account VALUES(1,?,?,?,?,?)",
                  (START_EQUITY, START_EQUITY, START_EQUITY, 0.0, now))
        c.commit()
    p = c.execute("SELECT * FROM positions WHERE id=1").fetchone()
    state["position"] = dict(p) if p else None
    c.close()

def save_account():
    c = db()
    now = datetime.now(timezone.utc).isoformat()
    row = c.execute("SELECT peak,max_dd FROM account WHERE id=1").fetchone()
    peak = max(float(row["peak"]), state["paper_equity"])
    dd = 0 if peak <= 0 else (peak - state["paper_equity"]) / peak
    maxdd = max(float(row["max_dd"]), dd)
    c.execute("UPDATE account SET cash=?,equity=?,peak=?,max_dd=?,updated=? WHERE id=1",
              (state["cash"], state["paper_equity"], peak, maxdd, now))
    c.commit(); c.close()

def save_position(p):
    c = db()
    if p:
        c.execute("""INSERT OR REPLACE INTO positions
          (id,entry,qty,stop,target,entry_fee,entry_time,entry_candle)
          VALUES(1,?,?,?,?,?,?,?)""",
          (p["entry"],p["qty"],p["stop"],p["target"],p["entry_fee"],p["entry_time"],p["entry_candle"]))
    else:
        c.execute("DELETE FROM positions WHERE id=1")
    c.commit(); c.close()

def ema(vals, n):
    if len(vals) < n: return [None]*len(vals)
    k=2/(n+1); out=[None]*(n-1)
    e=sum(vals[:n])/n; out.append(e)
    for x in vals[n:]:
        e=x*k+e*(1-k); out.append(e)
    return out

def rsi(vals, n=14):
    out=[None]*len(vals)
    if len(vals)<=n: return out
    gains=[]; losses=[]
    for i in range(1,n+1):
        d=vals[i]-vals[i-1]; gains.append(max(d,0)); losses.append(max(-d,0))
    ag=sum(gains)/n; al=sum(losses)/n
    out[n]=100 if al==0 else 100-(100/(1+ag/al))
    for i in range(n+1,len(vals)):
        d=vals[i]-vals[i-1]
        ag=(ag*(n-1)+max(d,0))/n; al=(al*(n-1)+max(-d,0))/n
        out[i]=100 if al==0 else 100-(100/(1+ag/al))
    return out

def atr(candles, n=14):
    tr=[]
    for i,x in enumerate(candles):
        if i==0: tr.append(x[2]-x[3])
        else: tr.append(max(x[2]-x[3], abs(x[2]-candles[i-1][4]), abs(x[3]-candles[i-1][4])))
    out=[None]*len(tr)
    if len(tr)<n: return out
    a=sum(tr[:n])/n; out[n-1]=a
    for i in range(n,len(tr)):
        a=(a*(n-1)+tr[i])/n; out[i]=a
    return out

def indicators(c):
    close=[x[4] for x in c]; vol=[x[5] for x in c]
    return {
        "e20": ema(close,20), "e50": ema(close,50), "e200": ema(close,200),
        "rsi": rsi(close,14), "atr": atr(c,14),
        "vavg": [None if i<20 else sum(vol[i-20:i])/20 for i in range(len(c))]
    }

def score_at(c, ind, i, trend_ok=True):
    if i<205 or any(ind[k][i] is None for k in ("e20","e50","e200","rsi","atr","vavg")):
        return 0, "WARMUP"
    close=c[i][4]; prev_high=c[i-1][2]
    prior3=max(x[2] for x in c[i-3:i])
    e20,e50,e200=ind["e20"][i],ind["e50"][i],ind["e200"][i]
    rs,at,v=ind["rsi"][i],ind["atr"][i],ind["vavg"][i]
    score=0; reasons=[]
    if close>e20: score+=20; reasons.append("above EMA20")
    if e20>e50: score+=20; reasons.append("EMA20>EMA50")
    if 45<=rs<=68: score+=15; reasons.append("RSI healthy")
    if c[i][5]>=0.8*v: score+=10; reasons.append("volume")
    if close>prev_high: score+=20; reasons.append("breaks prior high")
    if close>prior3: score+=15; reasons.append("3-candle breakout")
    if close>e20+1.5*at: score-=15; reasons.append("stretched")
    if not trend_ok: score=0; reasons=["1h trend filter off"]
    return max(0,min(100,score)), ", ".join(reasons)

async def fetch_klines(days, interval="5m"):
    limit=1000
    end=int(datetime.now(timezone.utc).timestamp()*1000)
    start=int((datetime.now(timezone.utc)-timedelta(days=days)).timestamp()*1000)
    out=[]
    async with httpx.AsyncClient(timeout=25) as client:
        cur=start
        while cur<end:
            r=await client.get(BINANCE,params={"symbol":SYMBOL,"interval":interval,"startTime":cur,"endTime":end,"limit":limit})
            r.raise_for_status()
            batch=r.json()
            if not batch: break
            out.extend(batch)
            nxt=batch[-1][0]+1
            if nxt<=cur: break
            cur=nxt
            if len(batch)<limit: break
    # use only completed candles
    now=int(datetime.now(timezone.utc).timestamp()*1000)
    return [[int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])] for x in out if int(x[6])<=now]

def trend_series_1h(h):
    close=[x[4] for x in h]
    e20,e50,e200=ema(close,20),ema(close,50),ema(close,200)
    return e20,e50,e200

def trend_ok_for_time(h, ts):
    # Last completed 1h candle whose open time <= 5m candle timestamp.
    import bisect
    opens=[x[0] for x in h]
    j=bisect.bisect_right(opens, ts)-1
    if j<200: return False
    e20,e50,e200=trend_series_1h(h)
    return e20[j] is not None and e50[j] is not None and e200[j] is not None and e20[j]>e50[j]>e200[j]


def simulate_range(candles, hourly, start_i, end_i, initial=20.0):
    ind=indicators(candles)
    cash=initial; pos=None; trades=[]; peak=initial; maxdd=0.0
    cooldown=0; current_day=None; day_start_equity=initial
    first=max(205,start_i)
    last=min(end_i,len(candles)-1)
    for i in range(first,last):
        x=candles[i]; ts=x[0]
        day=datetime.fromtimestamp(ts/1000,timezone.utc).date()
        if day!=current_day:
            current_day=day
            day_start_equity=cash+(pos["qty"]*x[4] if pos else 0)
        # Manage existing position using the closed candle.
        if pos:
            hi,lo=x[2],x[3]
            stop_hit=lo<=pos["stop"]; target_hit=hi>=pos["target"]
            if stop_hit or target_hit:
                reason="STOP" if stop_hit else "TARGET"
                raw_exit=pos["stop"] if stop_hit else pos["target"]
                exitp=raw_exit*(1-SLIPPAGE)
                proceeds=pos["qty"]*exitp
                exit_fee=proceeds*FEE
                gross=(exitp-pos["entry"])*pos["qty"]
                net=gross-pos["entry_fee"]-exit_fee
                cash += proceeds-exit_fee
                trades.append((pos["entry_time"],datetime.fromtimestamp(ts/1000,timezone.utc).isoformat(),
                               pos["entry"],exitp,pos["qty"],pos["stop"],pos["target"],gross,
                               pos["entry_fee"]+exit_fee,net,reason))
                pos=None; cooldown=COOLDOWN_CANDLES
        if cooldown>0: cooldown-=1
        equity=cash+(pos["qty"]*x[4] if pos else 0)
        peak=max(peak,equity); maxdd=max(maxdd,(peak-equity)/peak if peak else 0)
        daily_loss=max(0,(day_start_equity-equity)/day_start_equity) if day_start_equity else 0
        # Signal on candle i, enter at next candle OPEN. This prevents look-ahead.
        if pos is None and cooldown==0 and daily_loss<DAILY_LOSS_CAP and i+1<last:
            tok=trend_ok_for_time(hourly,ts)
            sc,_=score_at(candles,ind,i,tok)
            if sc>=60:
                entry=candles[i+1][1]*(1+SLIPPAGE)
                at=ind["atr"][i]
                if at:
                    stop=entry-STOP_ATR*at
                    risk=max(entry-stop,entry*0.002)
                    risk_budget=max(0,equity*RISK_PCT)
                    qty=min(risk_budget/risk, cash/(entry*(1+FEE)))
                    if qty>0:
                        notional=qty*entry; entry_fee=notional*FEE
                        cash-=notional+entry_fee
                        pos={"entry":entry,"qty":qty,"stop":stop,
                             "target":entry+TARGET_R*(entry-stop),"entry_fee":entry_fee,
                             "entry_time":datetime.fromtimestamp(candles[i+1][0]/1000,timezone.utc).isoformat(),
                             "entry_candle":candles[i+1][0]}
    # Mark/close any remaining position at the last available close.
    if pos:
        exitp=candles[last][4]*(1-SLIPPAGE)
        proceeds=pos["qty"]*exitp; exit_fee=proceeds*FEE
        gross=(exitp-pos["entry"])*pos["qty"]; net=gross-pos["entry_fee"]-exit_fee
        cash+=proceeds-exit_fee
        trades.append((pos["entry_time"],datetime.fromtimestamp(candles[last][0]/1000,timezone.utc).isoformat(),
                       pos["entry"],exitp,pos["qty"],pos["stop"],pos["target"],gross,
                       pos["entry_fee"]+exit_fee,net,"END"))
        pos=None
    equity=cash
    peak=max(peak,equity); maxdd=max(maxdd,(peak-equity)/peak if peak else 0)
    wins=sum(1 for t in trades if t[9]>0); losses=len(trades)-wins
    fees=sum(t[8] for t in trades)
    gross_profit=sum(t[9] for t in trades if t[9]>0)
    gross_loss=-sum(t[9] for t in trades if t[9]<0)
    pf=(gross_profit/gross_loss) if gross_loss else (float("inf") if gross_profit else 0)
    return {"start":initial,"end":equity,"return_pct":(equity/initial-1)*100,
            "trades":len(trades),"wins":wins,"losses":losses,
            "win_rate":(wins/len(trades)*100 if trades else 0),
            "max_dd_pct":maxdd*100,"fees":fees,"profit_factor":(pf if math.isfinite(pf) else None),
            "expectancy":(sum(t[9] for t in trades)/len(trades) if trades else 0),
            "trades_detail":trades}


async def run_backtest(days):
    five=await fetch_klines(days,"5m")
    hour=await fetch_klines(days+10,"1h")
    n=len(five); cut=int(n*0.7)
    train=simulate_range(five,hour,0,cut,20.0)
    test=simulate_range(five,hour,cut,n,20.0)
    return {"days":days,"candles":n,"train":train,"test":test}

def html():
    return """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>AI BTC Scout Final</title><style>
    body{margin:0;background:#070a0f;color:#f4f6f8;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif}
    .wrap{max-width:760px;margin:auto;padding:18px}.card{background:#111822;border:1px solid #263548;border-radius:22px;padding:20px;margin:14px 0}
    h1{font-size:28px;margin:0 0 6px}.muted{color:#9aa7b6}.big{font-size:34px;font-weight:800}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
    .pill{display:inline-block;padding:7px 12px;border-radius:999px;background:#1c2735}.btn{padding:13px 16px;border:0;border-radius:13px;font-size:16px;margin:4px;background:#e8edf3}
    table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #263548;text-align:left}.ok{color:#6ee7b7}.warn{color:#fbbf24}
    @media(max-width:520px){.grid{grid-template-columns:1fr}.big{font-size:30px}}
    </style></head><body><div class="wrap">
    <div class="card"><h1>AI BTC Scout â FINAL</h1><div class="muted">BTC/USDT Â· 5-minute Â· PAPER ONLY</div>
    <div id="status">Loadingâ¦</div></div>
    <div class="grid"><div class="card"><div class="muted">Paper equity</div><div class="big" id="eq">$20.00</div></div>
    <div class="card"><div class="muted">BTC</div><div class="big" id="price">â</div></div></div>
    <div class="card"><h2>Live paper engine</h2><div id="live"></div></div>
    <div class="card"><h2>Validation</h2><button class="btn" onclick="run(180)">Run 180 days</button><button class="btn" onclick="run(365)">Run 365 days</button><div id="bt">No run yet.</div></div>
    <div class="card"><h2>Recent paper trades</h2><div id="trades">Loadingâ¦</div></div>
    <div class="card"><div class="muted">Paper-only. No exchange orders, no API keys, no leverage.</div></div>
    </div><script>
    async function load(){let r=await fetch('/api/status');let d=await r.json();
      document.getElementById('eq').textContent='$'+d.paper_equity.toFixed(2);document.getElementById('price').textContent=d.price?'$'+d.price.toLocaleString():'â';
      document.getElementById('status').innerHTML='<b>'+d.action+'</b> Â· Score '+d.score+' Â· '+d.trend+' Â· RSI '+(d.rsi??'â');
      document.getElementById('live').innerHTML=d.position?`OPEN Â· Entry $${d.position.entry.toFixed(2)} Â· Stop $${d.position.stop.toFixed(2)} Â· Target $${d.position.target.toFixed(2)}`:'No open paper position';
      document.getElementById('trades').innerHTML=d.trades.length?'<table><tr><th>Exit</th><th>Reason</th><th>Net P/L</th></tr>'+d.trades.map(t=>`<tr><td>${t.exit_time}</td><td>${t.reason}</td><td>${t.net_pnl>=0?'+':''}$${t.net_pnl.toFixed(4)}</td></tr>`).join('')+'</table>':'No completed paper trades yet.';
    }
    async function run(days){document.getElementById('bt').textContent='Running '+days+'-day validationâ¦';let r=await fetch('/api/backtest/'+days);let d=await r.json();
      function box(x){return `<div class="card"><b>${x.name}</b><br>End $${x.end.toFixed(2)} Â· Return ${x.return_pct.toFixed(2)}% Â· Trades ${x.trades}<br>Win rate ${x.win_rate.toFixed(1)}% Â· DD ${x.max_dd_pct.toFixed(2)}% Â· Fees $${x.fees.toFixed(4)} Â· PF ${x.profit_factor===Infinity?'â':x.profit_factor.toFixed(2)}</div>`}
      document.getElementById('bt').innerHTML=box({...d.train,name:'Training 70%'})+box({...d.test,name:'Unseen test 30%'});
    } load();setInterval(load,60000);
    </script></body></html>"""

@app.get("/", response_class=HTMLResponse)
async def home(): return html()

@app.get("/api/status")
async def status():
    load_state()
    c=db(); trades=[dict(x) for x in c.execute("SELECT entry_time,exit_time,net_pnl,reason FROM trades ORDER BY id DESC LIMIT 20").fetchall()]; c.close()
    marked = state["cash"]
    if state["position"] and state["price"]:
        marked += state["position"]["qty"] * state["price"]
    return {"price":state["price"],"score":state["score"],"trend":state["trend"],"rsi":state["rsi"],
            "paper_equity":marked,"cash":state["cash"],"position":state["position"],
            "action":("PAPER POSITION" if state["position"] else ("SIGNAL" if state["score"]>=60 else "WAIT")),
            "last_scan":state["last_scan"],"last_error":state["last_error"],"trades":trades}

@app.get("/api/backtest/{days}")
async def backtest(days:int):
    if days not in (180,365): return JSONResponse({"error":"Use 180 or 365 days"},status_code=400)
    try: return await run_backtest(days)
    except Exception as e: return JSONResponse({"error":str(e)},status_code=500)


async def scan_loop():
    await asyncio.sleep(3)
    while True:
        try:
            c=await fetch_klines(3,"5m"); h=await fetch_klines(10,"1h")
            if len(c)<220: raise RuntimeError("Not enough candles")
            load_state()
            ind=indicators(c); i=len(c)-1; ts=c[i][0]
            tok=trend_ok_for_time(h,ts); sc,reason=score_at(c,ind,i,tok)
            state["price"]=c[i][4]; state["score"]=sc
            state["trend"]="BULLISH" if tok else "BEARISH"; state["rsi"]=ind["rsi"][i]
            state["last_candle"]=ts; state["last_scan"]=datetime.now(timezone.utc).isoformat()
            state["last_error"]=None

            # Only process each closed candle once.
            if state.get("processed") != ts:
                # 1) Execute pending signal at this candle's OPEN.
                if state.get("pending") and not state["position"]:
                    pnd=state["pending"]
                    entry=c[i][1]*(1+SLIPPAGE)
                    at=pnd["atr"]; equity=state["cash"]
                    if at:
                        stop=entry-STOP_ATR*at
                        risk=max(entry-stop,entry*0.002)
                        qty=min((equity*RISK_PCT)/risk, equity/(entry*(1+FEE)))
                        if qty>0:
                            notional=qty*entry; fee=notional*FEE
                            state["cash"]-=notional+fee
                            p={"entry":entry,"qty":qty,"stop":stop,
                               "target":entry+TARGET_R*(entry-stop),"entry_fee":fee,
                               "entry_time":datetime.fromtimestamp(c[i][0]/1000,timezone.utc).isoformat(),
                               "entry_candle":c[i][0]}
                            state["position"]=p; save_position(p)
                    state["pending"]=None

                # 2) Manage open position. If both are hit in one candle, STOP wins.
                if state["position"]:
                    p=state["position"]; hi,lo=c[i][2],c[i][3]
                    hit_stop=lo<=p["stop"]; hit_target=hi>=p["target"]
                    if hit_stop or hit_target:
                        reason_exit="STOP" if hit_stop else "TARGET"
                        raw=p["stop"] if hit_stop else p["target"]
                        exitp=raw*(1-SLIPPAGE)
                        proceeds=p["qty"]*exitp; fee=proceeds*FEE
                        gross=(exitp-p["entry"])*p["qty"]; net=gross-p["entry_fee"]-fee
                        state["cash"]+=proceeds-fee; state["paper_equity"]=state["cash"]
                        cc=db()
                        cc.execute("""INSERT INTO trades(entry_time,exit_time,entry,exit,qty,stop,target,gross_pnl,fees,net_pnl,reason)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                           (p["entry_time"],datetime.fromtimestamp(ts/1000,timezone.utc).isoformat(),
                            p["entry"],exitp,p["qty"],p["stop"],p["target"],gross,p["entry_fee"]+fee,net,reason_exit))
                        cc.execute("INSERT INTO events(ts,candle,score,action,price,reason) VALUES(?,?,?,?,?,?)",
                                   (datetime.now(timezone.utc).isoformat(),ts,sc,reason_exit,exitp,reason))
                        cc.commit(); cc.close()
                        state["position"]=None; save_position(None); state["cooldown"]=COOLDOWN_CANDLES
                        save_account()

                # 3) If flat, schedule a signal for the NEXT candle rather than entering on signal close.
                if not state["position"] and state["cooldown"]==0:
                    if sc>=60 and tok:
                        # daily loss gate based on realized cash vs start-of-day equity
                        today=datetime.fromtimestamp(ts/1000,timezone.utc).date()
                        if state.get("day")!=str(today):
                            state["day"]=str(today); state["day_start_equity"]=state["cash"]
                        daily_loss=max(0,(state["day_start_equity"]-state["cash"])/state["day_start_equity"]) if state["day_start_equity"] else 0
                        if daily_loss < DAILY_LOSS_CAP:
                            state["pending"]={"atr":ind["atr"][i],"signal_candle":ts}
                            cc=db(); cc.execute("INSERT INTO events(ts,candle,score,action,price,reason) VALUES(?,?,?,?,?,?)",
                                (datetime.now(timezone.utc).isoformat(),ts,sc,"SIGNAL",c[i][4],reason)); cc.commit(); cc.close()
                if state["cooldown"]>0: state["cooldown"]-=1
                state["paper_equity"]=state["cash"] + (state["position"]["qty"]*state["price"] if state["position"] else 0.0)
                state["processed"]=ts
                save_account()
        except Exception as e:
            state["last_error"]=str(e)
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup():
    load_state()
    asyncio.create_task(scan_loop())







