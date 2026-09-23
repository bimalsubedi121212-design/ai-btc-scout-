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
    "realized_pnl": 0.0, "trades": 0, "last_scan": None,
    "signal": "WAIT", "score": 0, "price": None, "rsi": None,
    "atr": None, "trend": "—", "entry": None, "stop": None,
    "target": None, "error": None
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
        gross = (ex - p["entry"]) * p["qty"]
        fees = (p["entry"] * p["qty"] + ex * p["qty"]) * FEE
        pnl = gross - fees
        state["cash"] += p["entry"] * p["qty"] + gross - fees
        state["realized_pnl"] += pnl
        state["trades"] += 1
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

@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE

PAGE = """<!doctype html>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI BTC Scout</title>
<style>
body{font-family:-apple-system,sans-serif;background:#080b10;color:#fff;margin:0;padding:16px}
.c{background:#121821;border:1px solid #26313d;border-radius:18px;padding:16px;margin:10px 0}
.g{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.v{font-size:24px;font-weight:700;margin-top:5px}.m{color:#94a0ad}
.p{display:inline-block;padding:6px 10px;border-radius:20px;background:#3a3118;color:#ffd36a}
.buy{background:#153d29;color:#74e39d}
.r{display:flex;justify-content:space-between;margin:8px 0}
</style>
<div class=c><h1>🤖 AI BTC Scout</h1><div class=m>BTC/USDT • 5-minute • PAPER ONLY</div>
<p><span id=s class=p>CONNECTING</span></p><div id=price class=v>—</div><div class=m>BTC price</div></div>
<div class=g>
<div class=c>AI score<div id=sc class=v>—</div></div>
<div class=c>Trend<div id=tr class=v>—</div></div>
<div class=c>RSI<div id=rs class=v>—</div></div>
<div class=c>Equity<div id=eq class=v>$20.00</div></div>
</div>
<div class=c><b>Paper position</b>
<div class=r>Entry <span id=en>—</span></div><div class=r>Stop <span id=st>—</span></div>
<div class=r>Target <span id=ta>—</span></div><div class=r>Realized P/L <span id=pl>$0.00</span></div>
<div class=r>Trades <span id=tx>0</span></div><div id=up class=m>Waiting…</div></div>
<div class="c m">Paper testing only. No exchange orders or API keys.</div>
<script>
async function load(){
try{
let x=await fetch('/api/status?x='+Date.now()),d=await x.json();
price.textContent=d.price?'$'+d.price.toLocaleString(undefined,{maximumFractionDigits:2}):'—';
sc.textContent=d.score;tr.textContent=d.trend;rs.textContent=d.rsi?d.rsi.toFixed(1):'—';
eq.textContent='$'+d.equity.toFixed(2);pl.textContent='$'+d.realized_pnl.toFixed(2);tx.textContent=d.trades;
for(let k of ['en','st','ta']){
let key={en:'entry',st:'stop',ta:'target'}[k];
document.getElementById(k).textContent=d[key]?'$'+d[key].toFixed(2):'—';
}
s.textContent=d.signal;s.className='p '+(d.signal==='BUY'?'buy':'');
up.textContent=d.error?'Error: '+d.error:'Last scan: '+(d.last_scan?new Date(d.last_scan).toLocaleString():'—');
}catch(e){up.textContent='Server connection error'}
}
load();setInterval(load,10000);
</script>"""
