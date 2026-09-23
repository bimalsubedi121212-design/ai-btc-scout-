import asyncio, json, urllib.request
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

START = 20.0
RISK = 0.01
FEE = 0.001
RR = 2.0
MIN_SCORE = 75
SYMBOL = "BTCUSDT"
INTERVAL = "5m"

state = {
    "equity": START, "cash": START, "position": None,
    "realized_pnl": 0.0, "trades": 0, "wins": 0, "losses": 0,
    "fees": 0.0, "peak_equity": START, "max_drawdown_pct": 0.0,
    "last_scan": None, "signal": "WAIT", "score": 0, "price": None,
    "rsi": None, "atr": None, "trend": "—", "entry": None, "stop": None,
    "target": None, "error": None, "history": []
}

app = FastAPI(title="AI BTC Scout")

def fetch():
    url = f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={INTERVAL}&limit=220"
    with urllib.request.urlopen(url, timeout=10) as r:
        raw = json.loads(r.read())
    return [{"h": float(x[2]), "l": float(x[3]), "c": float(x[4]), "v": float(x[5])} for x in raw]

def ema(values, n):
    a = 2 / (n + 1)
    out, e = [], values[0]
    for x in values:
        e = x * a + e * (1 - a)
        out.append(e)
    return out

def rsi(values, n=14):
    if len(values) <= n:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    g = sum(gains[:n]) / n
    l = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        g = (g * (n-1) + gains[i]) / n
        l = (l * (n-1) + losses[i]) / n
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)

def atr(rows, n=14):
    tr = []
    for i, x in enumerate(rows):
        if i == 0:
            tr.append(x["h"] - x["l"])
        else:
            p = rows[i-1]["c"]
            tr.append(max(x["h"] - x["l"], abs(x["h"] - p), abs(x["l"] - p)))
    return sum(tr[-n:]) / n

def scan():
    rows = fetch()
    closes = [x["c"] for x in rows]
    vols = [x["v"] for x in rows]
    e20, e50, e200 = ema(closes,20)[-1], ema(closes,50)[-1], ema(closes,200)[-1]
    price, rv, av = closes[-1], rsi(closes), atr(rows)
    vma = sum(vols[-21:-1]) / 20

    score = (
        (20 if e20 > e50 else 0) +
        (20 if e50 > e200 else 0) +
        (15 if 50 <= rv <= 68 else 0) +
        (15 if vols[-1] > vma else 0) +
        (15 if price > e20 else 0) +
        (15 if rv < 72 else 0)
    )
    signal = "BUY" if score >= MIN_SCORE and price > e20 and e20 > e50 else "WAIT"

    state.update(
        last_scan=datetime.now(timezone.utc).isoformat(),
        signal=signal, score=score, price=price, rsi=rv, atr=av,
        trend="BULLISH" if e20 > e50 > e200 else "MIXED/BEARISH",
        error=None
    )

    p = state["position"]
    if p and (price <= p["stop"] or price >= p["target"]):
        ex = p["stop"] if price <= p["stop"] else p["target"]
        reason = "STOP" if price <= p["stop"] else "TARGET"
        gross = (ex - p["entry"]) * p["qty"]
        fees = (p["entry"] * p["qty"] + ex * p["qty"]) * FEE
        pnl = gross - fees
        state["cash"] += p["entry"] * p["qty"] + gross - fees
        state["realized_pnl"] += pnl
        state["fees"] += fees
        state["trades"] += 1
        if pnl > 0:
            state["wins"] += 1
        else:
            state["losses"] += 1
        state["history"].append({
            "time": datetime.now(timezone.utc).isoformat(),
            "entry": round(p["entry"], 2), "exit": round(ex, 2),
            "qty": p["qty"], "pnl": round(pnl, 4), "reason": reason
        })
        state["history"] = state["history"][-50:]
        state["position"] = None

    if state["position"] is None and signal == "BUY" and state["cash"] > 1:
        stop = price - av * 1.5
        risk_cash = state["equity"] * RISK
        risk_coin = max(price - stop, price * 0.001)
        qty = min(risk_cash / risk_coin, state["cash"] / (price * (1 + FEE)))
        if qty > 0:
            state["cash"] -= price * qty * (1 + FEE)
            state["position"] = {
                "entry": price, "stop": stop,
                "target": price + (price - stop) * RR, "qty": qty
            }

    if state["position"]:
        p = state["position"]
        state["equity"] = state["cash"] + price * p["qty"]
        state["entry"], state["stop"], state["target"] = p["entry"], p["stop"], p["target"]
    else:
        state["equity"] = state["cash"]
        state["entry"] = state["stop"] = state["target"] = None

    state["peak_equity"] = max(state["peak_equity"], state["equity"])
    if state["peak_equity"] > 0:
        dd = (state["peak_equity"] - state["equity"]) / state["peak_equity"] * 100
        state["max_drawdown_pct"] = max(state["max_drawdown_pct"], dd)

def backtest(rows):
    if len(rows) < 205:
        return {"error": "Not enough candles for backtest"}
    cash, position = START, None
    wins = losses = 0
    fees_total = 0.0
    peak, max_dd = START, 0.0

    for i in range(200, len(rows)):
        hist = rows[:i+1]
        closes = [x["c"] for x in hist]
        vols = [x["v"] for x in hist]
        e20, e50, e200 = ema(closes,20)[-1], ema(closes,50)[-1], ema(closes,200)[-1]
        price, rv, av = closes[-1], rsi(closes), atr(hist)
        vma = sum(vols[-21:-1]) / 20
        score = (
            (20 if e20 > e50 else 0) + (20 if e50 > e200 else 0) +
            (15 if 50 <= rv <= 68 else 0) + (15 if vols[-1] > vma else 0) +
            (15 if price > e20 else 0) + (15 if rv < 72 else 0)
        )
        signal = score >= MIN_SCORE and price > e20 and e20 > e50

        if position and (price <= position["stop"] or price >= position["target"]):
            ex = position["stop"] if price <= position["stop"] else position["target"]
            gross = (ex-position["entry"])*position["qty"]
            fees = (position["entry"]*position["qty"]+ex*position["qty"])*FEE
            pnl = gross-fees
            cash += position["entry"]*position["qty"]+gross-fees
            fees_total += fees
            if pnl > 0: wins += 1
            else: losses += 1
            position = None

        equity = cash if not position else cash + price*position["qty"]
        if position is None and signal and cash > 1:
            stop = price-av*1.5
            risk_cash = equity*RISK
            risk_coin = max(price-stop, price*0.001)
            qty = min(risk_cash/risk_coin, cash/(price*(1+FEE)))
            if qty > 0:
                cash -= price*qty*(1+FEE)
                position = {"entry":price,"stop":stop,"target":price+(price-stop)*RR,"qty":qty}
                equity = cash + price*qty
        peak = max(peak,equity)
        max_dd = max(max_dd,(peak-equity)/peak*100 if peak else 0)

    if position:
        ex=rows[-1]["c"]
        gross=(ex-position["entry"])*position["qty"]
        fees=(position["entry"]*position["qty"]+ex*position["qty"])*FEE
        pnl=gross-fees
        cash += position["entry"]*position["qty"]+gross-fees
        fees_total += fees
        if pnl>0: wins+=1
        else: losses+=1

    total=wins+losses
    return {"start":START,"end_equity":round(cash,2),
            "return_pct":round((cash-START)/START*100,2),
            "trades":total,"wins":wins,"losses":losses,
            "win_rate_pct":round(wins/total*100,1) if total else 0,
            "max_drawdown_pct":round(max_dd,2),"fees":round(fees_total,4),
            "candles":len(rows)}

async def loop():
    while True:
        try:
            scan()
        except Exception as e:
            state["error"] = str(e)[:180]
        await asyncio.sleep(60)

@app.on_event("startup")
async def start():
    asyncio.create_task(loop())

@app.get("/api/status")
def status():
    return state

@app.get("/api/backtest")
def api_backtest():
    try:
        return backtest(fetch())
    except Exception as e:
        return {"error": str(e)[:180]}

@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE

PAGE = """<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI BTC Scout</title>
<style>
body{font-family:-apple-system,sans-serif;background:#080b10;color:#fff;margin:0;padding:16px}
.c{background:#121821;border:1px solid #26313d;border-radius:18px;padding:16px;margin:10px 0}
.g{display:grid;grid-template-columns:1fr 1fr;gap:10px}.v{font-size:24px;font-weight:700;margin-top:5px}.m{color:#94a0ad}
.p{display:inline-block;padding:6px 10px;border-radius:20px;background:#3a3118;color:#ffd36a}
.buy{background:#153d29;color:#74e39d}.r{display:flex;justify-content:space-between;margin:8px 0}
.good{color:#74e39d}.bad{color:#ff8585}table{width:100%;border-collapse:collapse;font-size:13px}
td,th{text-align:left;padding:7px 4px;border-bottom:1px solid #26313d}
</style>
<div class=c><h1>🤖 AI BTC Scout</h1><div class=m>BTC/USDT • 5-minute • PAPER ONLY</div>
<p><span id=s class=p>CONNECTING</span></p><div id=price class=v>—</div><div class=m>BTC price</div></div>
<div class=g>
<div class=c>AI score<div id=sc class=v>—</div></div><div class=c>Trend<div id=tr class=v>—</div></div>
<div class=c>RSI<div id=rs class=v>—</div></div><div class=c>Equity<div id=eq class=v>$20.00</div></div>
<div class=c>Win rate<div id=wr class=v>0%</div></div><div class=c>Max drawdown<div id=dd class=v>0%</div></div>
</div>
<div class=c><b>Paper position</b>
<div class=r>Entry <span id=en>—</span></div><div class=r>Stop <span id=st>—</span></div>
<div class=r>Target <span id=ta>—</span></div><div class=r>Realized P/L <span id=pl>$0.00</span></div>
<div class=r>Trades <span id=tx>0</span></div><div class=r>Wins / Losses <span id=wl>0 / 0</span></div>
<div id=up class=m>Waiting…</div></div>
<div class=c><b>Backtest (latest 220 × 5-minute candles)</b>
<div class=r>Ending equity <span id=be>—</span></div><div class=r>Return <span id=br>—</span></div>
<div class=r>Trades <span id=bt>—</span></div><div class=r>Win rate <span id=bw>—</span></div>
<div class=r>Max drawdown <span id=bd>—</span></div><div class=r>Fees <span id=bf>—</span></div></div>
<div class=c><b>Recent paper trades</b><div id=hist class=m>No closed trades yet.</div></div>
<div class="c m">Paper testing only. No exchange orders or API keys. Historical simulation is not a guarantee of future performance.</div>
<script>
async function load(){
try{
let [a,b]=await Promise.all([fetch('/api/status?x='+Date.now()),fetch('/api/backtest?x='+Date.now())]);
let d=await a.json(),bt=await b.json();
price.textContent=d.price?'$'+d.price.toLocaleString(undefined,{maximumFractionDigits:2}):'—';
sc.textContent=d.score;tr.textContent=d.trend;rs.textContent=d.rsi?d.rsi.toFixed(1):'—';
eq.textContent='$'+d.equity.toFixed(2);pl.textContent='$'+d.realized_pnl.toFixed(2);
tx.textContent=d.trades;wl.textContent=d.wins+' / '+d.losses;
wr.textContent=(d.trades?((d.wins/d.trades)*100).toFixed(1):'0')+'%';
dd.textContent=d.max_drawdown_pct.toFixed(2)+'%';
for(let k of ['en','st','ta']){let key={en:'entry',st:'stop',ta:'target'}[k];
document.getElementById(k).textContent=d[key]?'$'+d[key].toFixed(2):'—';}
s.textContent=d.signal;s.className='p '+(d.signal==='BUY'?'buy':'');
up.textContent=d.error?'Error: '+d.error:'Last scan: '+(d.last_scan?new Date(d.last_scan).toLocaleString():'—');
if(bt.error){be.textContent='Error';}else{
be.textContent='$'+bt.end_equity.toFixed(2);br.textContent=bt.return_pct+'%';document.getElementById('bt').textContent=bt.trades;
bw.textContent=bt.win_rate_pct+'%';bd.textContent=bt.max_drawdown_pct+'%';bf.textContent='$'+bt.fees.toFixed(4);}
if(d.history&&d.history.length){
hist.innerHTML='<table><tr><th>Time</th><th>Entry</th><th>Exit</th><th>P/L</th><th>Reason</th></tr>'+
d.history.slice().reverse().slice(0,10).map(t=>'<tr><td>'+new Date(t.time).toLocaleTimeString()+'</td><td>$'+t.entry.toFixed(2)+'</td><td>$'+t.exit.toFixed(2)+'</td><td class="'+(t.pnl>=0?'good':'bad')+'">$'+t.pnl.toFixed(3)+'</td><td>'+t.reason+'</td></tr>').join('')+'</table>';
}
}catch(e){up.textContent='Server connection error'}
}
load();setInterval(load,10000);
</script>"""
