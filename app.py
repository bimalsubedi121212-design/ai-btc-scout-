import os, sqlite3, asyncio, math, uuid
from datetime import datetime, timezone, timedelta
from statistics import mean

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

BINANCE = "https://api.binance.com/api/v3/klines"
SYMBOL = "BTCUSDT"
START = 20.0
FEE = 0.0010
SLIP = 0.0005
RISK = 0.01
MAX_NOTIONAL = 0.25
STOP_ATR = 1.5
TARGET_R = 1.5
MINSTOP = 0.01
COOLDOWN = 8
ML_HORIZON = 8
ML_REFIT = 192
ML_TRAIN_WINDOW = 1800
ML_THRESHOLD_GRID = (0.50, 0.52, 0.54, 0.56, 0.58, 0.60)
DB = os.getenv("DB_PATH", "paper_trading.db")

app = FastAPI(title="AI BTC Scout Research V8")
state = {"cash": START, "equity": START, "price": None, "score": 0,
         "trend": "UNKNOWN", "rsi": None, "pos": None, "processed": None,
         "cool": 0, "pending": None, "err": None, "ml_prob": None}
jobs = {}


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("create table if not exists account(id integer primary key,cash real,equity real,peak real,dd real,updated text)")
    c.execute("create table if not exists positions(id integer primary key,entry real,qty real,stop real,target real,fee real,entry_time text)")
    c.execute("create table if not exists trades(id integer primary key autoincrement,entry_time text,exit_time text,entry real,exit real,qty real,gross real,fees real,net real,reason text)")
    c.commit()
    return c


def load():
    c = db()
    a = c.execute("select * from account where id=1").fetchone()
    if not a:
        c.execute("insert into account values(1,?,?,?,?,?)", (START, START, START, 0, datetime.now(timezone.utc).isoformat()))
        c.commit()
        state["cash"] = state["equity"] = START
    else:
        state["cash"] = a["cash"]
        state["equity"] = a["equity"]
    p = c.execute("select * from positions where id=1").fetchone()
    state["pos"] = dict(p) if p else None
    c.close()


def save():
    c = db()
    a = c.execute("select peak,dd from account where id=1").fetchone()
    peak = max(a["peak"], state["equity"])
    dd = max(a["dd"], (peak - state["equity"]) / peak if peak else 0)
    c.execute("update account set cash=?,equity=?,peak=?,dd=?,updated=? where id=1",
              (state["cash"], state["equity"], peak, dd, datetime.now(timezone.utc).isoformat()))
    c.commit(); c.close()


def savepos(p):
    c = db(); c.execute("delete from positions where id=1")
    if p:
        c.execute("insert into positions values(1,?,?,?,?,?,?)",
                  (p["entry"], p["qty"], p["stop"], p["target"], p["fee"], p["entry_time"]))
    c.commit(); c.close()


def ema(v, n):
    if len(v) < n: return [None] * len(v)
    k = 2 / (n + 1); out = [None] * (n - 1); e = sum(v[:n]) / n; out.append(e)
    for x in v[n:]: e = x * k + e * (1 - k); out.append(e)
    return out


def rsi(v, n=14):
    out = [None] * len(v)
    if len(v) <= n: return out
    gains = []; losses = []
    for i in range(1, n + 1):
        d = v[i] - v[i-1]; gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag, al = sum(gains) / n, sum(losses) / n
    out[n] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(v)):
        d = v[i] - v[i-1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(c, n=14):
    tr = []
    for i, x in enumerate(c):
        if i == 0: tr.append(x[2] - x[3])
        else: tr.append(max(x[2] - x[3], abs(x[2] - c[i-1][4]), abs(x[3] - c[i-1][4])))
    out = [None] * len(tr)
    if len(tr) < n: return out
    a = sum(tr[:n]) / n; out[n-1] = a
    for i in range(n, len(tr)):
        a = (a * (n - 1) + tr[i]) / n; out[i] = a
    return out


def indicators(c):
    close = [x[4] for x in c]; vol = [x[5] for x in c]
    return {"e20": ema(close,20), "e50": ema(close,50), "e200": ema(close,200),
            "rsi": rsi(close), "atr": atr(c),
            "va": [None if i < 20 else sum(vol[i-20:i]) / 20 for i in range(len(c))]}


def trend_1h(h, ts):
    close = [x[4] for x in h]; a, b, c = ema(close,20), ema(close,50), ema(close,200)
    idx = max([i for i,x in enumerate(h) if x[0] <= ts], default=-1)
    return idx >= 200 and a[idx] and b[idx] and c[idx] and a[idx] > b[idx] > c[idx]


def technical_score(c, d, i, trend):
    if i < 205 or not trend or any(d[k][i] is None for k in ("e20","e50","e200","rsi","atr","va")): return 0
    close = c[i][4]; pc = c[i-1][4]; pl = c[i-1][3]; ph = c[i-1][2]
    e20,e50,e200 = d["e20"][i],d["e50"][i],d["e200"][i]
    rs,at,v = d["rsi"][i],d["atr"][i],d["va"][i]
    vol = c[i][5]
    pull = pl <= d["e20"][i-1] or pc <= d["e20"][i-1]
    reclaim = close > e20 and close > ph
    if e20 > e50 > e200 and pull and reclaim and 48 <= rs <= 64 and vol >= .90*v and close <= e20 + at:
        return 100
    return 0


async def fetch(days, interval):
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    cur = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    out = []
    async with httpx.AsyncClient(timeout=20) as client:
        while cur < end:
            r = await client.get(BINANCE, params={"symbol": SYMBOL, "interval": interval, "startTime": cur, "endTime": end, "limit": 1000})
            r.raise_for_status(); batch = r.json()
            if not batch: break
            out.extend(batch); nxt = batch[-1][0] + 1
            if nxt <= cur: break
            cur = nxt
            if len(batch) < 1000: break
    return [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])] for x in out if int(x[6]) <= end]


def trade_stats(trades, cash, peak, mdd, fees, slips, turnover, signals=0):
    wins = sum(x[0] > 0 for x in trades); gp = sum(x[0] for x in trades if x[0] > 0); gl = -sum(x[0] for x in trades if x[0] < 0)
    return {"end": cash, "return_pct": (cash / START - 1) * 100, "trades": len(trades), "signals": signals,
            "win_rate": 100 * wins / len(trades) if trades else 0, "dd": mdd * 100, "fees": fees,
            "slippage": slips, "turnover": turnover, "pf": gp/gl if gl else None,
            "expectancy": sum(x[0] for x in trades) / len(trades) if trades else 0}


def make_features(c, d, i):
    if i < 205 or any(d[k][i] is None for k in ("e20","e50","e200","rsi","atr","va")): return None
    close = [x[4] for x in c]; price = close[i]; at = d["atr"][i]
    rets = [close[j]/close[j-1]-1 for j in range(max(1,i-20), i+1)]
    vol = c[i][5] / d["va"][i] if d["va"][i] else 1
    return [1.0,
            close[i]/close[i-1]-1,
            close[i]/close[i-4]-1,
            close[i]/close[i-16]-1,
            price/d["e20"][i]-1,
            price/d["e50"][i]-1,
            price/d["e200"][i]-1,
            (d["e20"][i]/d["e50"][i]-1),
            (d["rsi"][i]-50)/50,
            at/price,
            (c[i][2]-c[i][3])/price,
            math.log(max(vol, 0.05)),
            math.sqrt(sum(x*x for x in rets)/len(rets))]


def label_trade(c, d, i):
    if i + ML_HORIZON >= len(c) or d["atr"][i] is None: return None
    entry = c[i][4]; risk = max(STOP_ATR*d["atr"][i], entry*MINSTOP); stop = entry-risk; target = entry+TARGET_R*risk
    for j in range(i+1, min(len(c), i+ML_HORIZON+1)):
        # Conservative tie rule: if both happen in one candle, count as stop.
        if c[j][3] <= stop: return 0
        if c[j][2] >= target: return 1
    return 0


def standardize(X):
    m = len(X[0]); mu=[0.0]*m; sd=[1.0]*m
    for j in range(m):
        vals=[r[j] for r in X]; mu[j]=sum(vals)/len(vals); var=sum((v-mu[j])**2 for v in vals)/len(vals); sd[j]=math.sqrt(var) or 1.0
    return [[(r[j]-mu[j])/sd[j] for j in range(m)] for r in X], mu, sd


def apply_scale(row, mu, sd): return [(row[j]-mu[j])/sd[j] for j in range(len(row))]


def fit_logistic(X, y, epochs=10, lr=.08, l2=.003):
    if len(X) < 40 or len(set(y)) < 2: return None
    Z, mu, sd = standardize(X); w=[0.0]*len(Z[0]); pos=sum(y); neg=len(y)-pos
    pw=(len(y)/(2*pos)) if pos else 1; nw=(len(y)/(2*neg)) if neg else 1
    for _ in range(epochs):
        g=[0.0]*len(w)
        for row, yy in zip(Z,y):
            p=1/(1+math.exp(-max(-30,min(30,sum(a*b for a,b in zip(w,row))))))
            wt=pw if yy else nw
            for j,a in enumerate(row): g[j]+=(p-yy)*a*wt
        for j in range(len(w)):
            g[j]=g[j]/len(Z)+l2*w[j]; w[j]-=lr*g[j]
    return {"w":w,"mu":mu,"sd":sd,"n":len(y),"positive_rate":sum(y)/len(y)}


def predict(model, row):
    z=sum(a*b for a,b in zip(model["w"], apply_scale(row, model["mu"], model["sd"])))
    return 1/(1+math.exp(-max(-30,min(30,z))))


def choose_threshold(c, d, train_start, train_end):
    # Internal validation inside the 70% training block. The final 30% remains untouched.
    span=train_end-train_start; split=train_start+int(span*.75); X=[];Y=[]
    for i in range(train_start, split-ML_HORIZON):
        f=make_features(c,d,i); y=label_trade(c,d,i)
        if f is not None and y is not None: X.append(f);Y.append(y)
    model=fit_logistic(X[-ML_TRAIN_WINDOW:],Y[-ML_TRAIN_WINDOW:])
    if not model: return .55, {"validation":"insufficient data"}
    scores=[]
    for th in ML_THRESHOLD_GRID:
        tp=fp=fn=0
        for i in range(split, train_end-ML_HORIZON):
            f=make_features(c,d,i)
            if f is None: continue
            p=predict(model,f); y=label_trade(c,d,i)
            if p>=th and y==1: tp+=1
            elif p>=th and y==0: fp+=1
            elif p<th and y==1: fn+=1
        precision=tp/(tp+fp) if tp+fp else 0
        recall=tp/(tp+fn) if tp+fn else 0
        f1=2*precision*recall/(precision+recall) if precision+recall else 0
        scores.append((f1,precision,th,tp,fp,fn))
    scores.sort(reverse=True)
    best=scores[0] if scores else (0,0,.55,0,0,0)
    return best[2], {"threshold":best[2],"validation_f1":best[0],"validation_precision":best[1],"tp":best[3],"fp":best[4],"fn":best[5]}


def walk_forward_ml(c, h):
    d=indicators(c); n=len(c); cut=int(n*.70); start=max(205, 205); test_start=cut
    if n < 1200: return {"error":"not enough candles"}
    threshold, tuning=choose_threshold(c,d,start,cut)
    model=None; cash=START; pos=None; trades=[]; fees=slips=turn=0.0; peak=START; mdd=0.0
    pred_count=0; psum=0; pmin=1; pmax=0; counts={x:0 for x in ML_THRESHOLD_GRID}; candidates=0; model_fits=0
    for i in range(test_start, n-ML_HORIZON):
        if model is None or (i-test_start)%ML_REFIT==0:
            X=[];Y=[]; lo=max(start,i-ML_TRAIN_WINDOW)
            for j in range(lo, i-ML_HORIZON):
                f=make_features(c,d,j); y=label_trade(c,d,j)
                if f is not None and y is not None: X.append(f);Y.append(y)
            model=fit_logistic(X,Y); model_fits+=1
        f=make_features(c,d,i)
        if f is None or model is None: continue
        p=predict(model,f); pred_count+=1; psum+=p; pmin=min(pmin,p); pmax=max(pmax,p)
        for th in ML_THRESHOLD_GRID:
            if p>=th: counts[th]+=1
        if p>=threshold: candidates+=1
        if pos:
            hs=c[i][3] <= pos["stop"]; ht=c[i][2] >= pos["target"]
            if hs or ht:
                raw=pos["stop"] if hs else pos["target"]; ex=raw*(1-SLIP); pro=pos["qty"]*ex; ef=pro*FEE
                net=(ex-pos["entry"])*pos["qty"]-pos["fee"]-ef; cash+=pro-ef; fees+=pos["fee"]+ef; slips+=raw*SLIP*pos["qty"]; turn+=pro
                trades.append(net); pos=None
        eq=cash+(pos["qty"]*c[i][4] if pos else 0); peak=max(peak,eq); mdd=max(mdd,(peak-eq)/peak)
        if not pos and p>=threshold and trend_1h(h, c[i][0]):
            entry=c[i+1][1]*(1+SLIP); risk=max(STOP_ATR*d["atr"][i],entry*MINSTOP); stop=entry-risk
            qty=min((eq*RISK)/risk, cash/(entry*(1+FEE))); qty=min(qty,(eq*MAX_NOTIONAL)/entry)
            if qty>0:
                no=qty*entry; ef=no*FEE; cash-=no+ef; fees+=ef; slips+=entry*SLIP*qty; turn+=no
                pos={"entry":entry,"qty":qty,"stop":stop,"target":entry+TARGET_R*risk,"fee":ef}
    if pos:
        ex=c[-1][4]*(1-SLIP); pro=pos["qty"]*ex; ef=pro*FEE; cash+=pro-ef; fees+=pos["fee"]+ef; turn+=pro
        trades.append((ex-pos["entry"])*pos["qty"]-pos["fee"]-ef)
    out=trade_stats(trades,cash,peak,mdd,fees,slips,turn,candidates)
    out.update({"predictions":pred_count,"avg_prob":psum/pred_count if pred_count else 0,"min_prob":pmin if pred_count else 0,
                "max_prob":pmax if pred_count else 0,"threshold":threshold,"threshold_counts":{str(k):v for k,v in counts.items()},
                "candidates":candidates,"model_fits":model_fits,"training_cut_pct":70,
                "tuning":tuning,"note":"Walk-forward ML with triple-barrier labels. Final 30% is never used for threshold selection or fitting."})
    return out


def trend_1h_from_15m(c,d,i):
    return d["e20"][i] and d["e50"][i] and d["e200"][i] and d["e20"][i]>d["e50"][i]>d["e200"][i]


async def ml_job(jid, days):
    try:
        jobs[jid]={"status":"running"}
        c=await fetch(days,"15m")
        h=await fetch(days+10,"1h")
        jobs[jid]["result"]=await asyncio.to_thread(walk_forward_ml,c,h)
        jobs[jid]["status"]="done"
    except Exception as e: jobs[jid]={"status":"error","error":str(e)}


PAGE='''<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1"><style>
body{background:#070a0f;color:#eee;font-family:-apple-system,BlinkMacSystemFont,Arial;margin:0}.w{max-width:760px;margin:auto;padding:16px}.c{background:#111822;border:1px solid #263548;border-radius:20px;padding:18px;margin:12px 0}.b{font-size:32px;font-weight:800}.m{color:#9aa7b6}button{padding:12px 16px;border:0;border-radius:11px;margin:4px;font-size:16px}
</style></head><body><div class=w><div class=c><h1>AI BTC Scout â RESEARCH V8</h1><div class=m>BTC/USDT Â· 15m research Â· ML + technical confirmation Â· PAPER ONLY</div><div id=s>Loading...</div></div>
<div class=c><div class=m>Paper equity</div><div class=b id=e>$20.00</div><div class=m>BTC</div><div class=b id=p>â</div></div>
<div class=c><h2>Live paper engine</h2><div id=l>Loading...</div></div>
<div class=c><h2>Research lab</h2><div class=m>70% chronological training block; final 30% unseen. ML threshold is selected only inside the training block. Triple-barrier labels align training with stop/target behavior.</div><button onclick=mlrun(180)>Run ML 180d</button><button onclick=mlrun(365)>Run ML 365d</button><div id=b>Not run yet.</div></div></div>
<script>
async function load(){let d=await(await fetch('/api/status')).json();e.textContent='$'+d.equity.toFixed(2);p.textContent=d.price?'$'+Math.round(d.price).toLocaleString():'â';s.innerHTML='<b>'+d.action+'</b> Â· Technical score '+d.score+' Â· '+d.trend+' Â· RSI '+(d.rsi??'â')+' Â· ML '+(d.ml_prob==null?'â':d.ml_prob.toFixed(3));l.innerHTML=d.position?'OPEN Â· Entry $'+d.position.entry.toFixed(2)+' Â· Stop $'+d.position.stop.toFixed(2)+' Â· Target $'+d.position.target.toFixed(2):'No open paper position'}
async function mlrun(d){b.textContent='Running V8 ML '+d+'-day testâ¦';let q=await(await fetch('/api/ml/'+d)).json();for(let i=0;i<300;i++){await new Promise(r=>setTimeout(r,1500));let z=await(await fetch('/api/job/'+q.job_id)).json();if(z.status==='done'){let x=z.result;let tc=Object.entries(x.threshold_counts).map(([k,v])=>k+': '+v).join(' Â· ');b.innerHTML='<div class=c><h2>WALK-FORWARD ML â '+d+' DAYS</h2>End $'+x.end.toFixed(2)+' Â· Return '+x.return_pct.toFixed(2)+'% Â· Trades '+x.trades+'<br>Win '+x.win_rate.toFixed(1)+'% Â· DD '+x.dd.toFixed(2)+'% Â· PF '+(x.pf==null?'â':x.pf.toFixed(2))+'<br>Fees $'+x.fees.toFixed(4)+' Â· Slippage $'+x.slippage.toFixed(4)+' Â· Turnover $'+x.turnover.toFixed(2)+'<hr><b>ML diagnostics</b><br>Predictions '+x.predictions+' Â· Avg '+x.avg_prob.toFixed(3)+' Â· Min '+x.min_prob.toFixed(3)+' Â· Max '+x.max_prob.toFixed(3)+'<br>Threshold selected inside training: '+x.threshold.toFixed(2)+'<br>Predictions by threshold â '+tc+'<br>Entry candidates: '+x.candidates+' Â· Model refits: '+x.model_fits+'<br><span class=m>Validation F1 '+(x.tuning.validation_f1??'â')+' Â· Precision '+(x.tuning.validation_precision??'â')+'</span><br><span class=m>'+x.note+'</span></div>';return}if(z.status==='error'){b.textContent='ML failed: '+z.error;return}b.textContent='ML runningâ¦ '+(i+1)+' / 300'}b.textContent='ML test timed out; paper engine unaffected'}
load();setInterval(load,60000)
</script></body></html>'''

@app.get('/', response_class=HTMLResponse)
async def home(): return PAGE

@app.get('/api/status')
async def status():
    load(); eq=state['cash']+(state['pos']['qty']*state['price'] if state['pos'] and state['price'] else 0)
    return {'equity':eq,'cash':state['cash'],'price':state['price'],'score':state['score'],'trend':state['trend'],'rsi':state['rsi'],'position':state['pos'],'action':'PAPER POSITION' if state['pos'] else ('SIGNAL' if state['score']>=100 else 'WAIT'),'error':state['err'],'ml_prob':state['ml_prob']}

@app.get('/api/ml/{days}')
async def ml(days:int):
    if days not in (180,365): return JSONResponse({'error':'Use 180 or 365 days'},400)
    if any(v.get('status') in ('queued','running') for v in jobs.values()): return JSONResponse({'error':'A research job is already running'},409)
    jid=uuid.uuid4().hex[:12]; jobs[jid]={'status':'queued','kind':'ml','days':days}; asyncio.create_task(ml_job(jid,days)); return {'job_id':jid,'status':'queued'}

@app.get('/api/job/{jid}')
async def job(jid:str): return jobs.get(jid,{'status':'error','error':'Unknown job'})

async def scan():
    await asyncio.sleep(3)
    while True:
        try:
            c=await fetch(3,'5m'); h=await fetch(10,'1h'); load(); d=indicators(c); i=len(c)-1; ts=c[i][0]
            t=trend_1h(h,ts); sc=technical_score(c,d,i,t)
            state.update(price=c[i][4],score=sc,trend='BULLISH' if t else 'BEARISH',rsi=d['rsi'][i],err=None)
            if state['processed']!=ts:
                if state['pending'] and not state['pos']:
                    entry=c[i][1]*(1+SLIP); at=state['pending']; risk=max(STOP_ATR*at,entry*MINSTOP); stop=entry-risk
                    qty=min((state['cash']*RISK)/risk,state['cash']/(entry*(1+FEE))); qty=min(qty,(state['cash']*MAX_NOTIONAL)/entry)
                    if qty>0:
                        no=qty*entry; ef=no*FEE; state['cash']-=no+ef
                        state['pos']={'entry':entry,'qty':qty,'stop':stop,'target':entry+TARGET_R*risk,'fee':ef,'entry_time':datetime.fromtimestamp(ts/1000,timezone.utc).isoformat()}; savepos(state['pos'])
                    state['pending']=None
                if state['pos']:
                    q=state['pos']; hs=c[i][3]<=q['stop']; ht=c[i][2]>=q['target']
                    if hs or ht:
                        raw=q['stop'] if hs else q['target']; reason='STOP' if hs else 'TARGET'; ex=raw*(1-SLIP); pro=q['qty']*ex; ef=pro*FEE; gross=(ex-q['entry'])*q['qty']; net=gross-q['fee']-ef; state['cash']+=pro-ef
                        cc=db();cc.execute("insert into trades(entry_time,exit_time,entry,exit,qty,gross,fees,net,reason) values(?,?,?,?,?,?,?,?,?)",(q['entry_time'],datetime.fromtimestamp(ts/1000,timezone.utc).isoformat(),q['entry'],ex,q['qty'],gross,q['fee']+ef,net,reason));cc.commit();cc.close();state['pos']=None;savepos(None);state['cool']=COOLDOWN
                if not state['pos'] and state['cool']==0 and sc>=100 and t: state['pending']=d['atr'][i]
                if state['cool']>0: state['cool']-=1
                state['equity']=state['cash']+(state['pos']['qty']*state['price'] if state['pos'] else 0);state['processed']=ts;save()
        except Exception as e: state['err']=str(e)
        await asyncio.sleep(60)

@app.on_event('startup')
async def startup(): load(); asyncio.create_task(scan())
