# ═══════════════════════════════════════════════════════════
#   TradeMind — Local Demo Server
#   
#   HOW TO RUN:
#   1. pip install flask flask-socketio websocket-client requests pandas numpy eventlet
#   2. Put this file in same folder as TradeMind_Tools.html
#   3. python server.py
#   4. Open browser: http://localhost:5000
#
#   NO API KEY NEEDED — uses free Binance public WebSocket
# ═══════════════════════════════════════════════════════════

import json, threading, time, os, requests
from flask import Flask, send_file
from flask_socketio import SocketIO
import websocket

# ── Flask setup — serves HTML from same folder ──────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app      = Flask(__name__, static_folder=BASE_DIR)
socket   = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

# ── Global state ─────────────────────────────────────────────
candles = {
    'BTCUSDT': [], 'ETHUSDT': [], 'SOLUSDT': [],
    'BNBUSDT': [], 'XRPUSDT': [], 'ADAUSDT': [], 'DOGEUSDT': []
}
live_prices = {}
ai_signals  = {}

# ── Serve the HTML chart ──────────────────────────────────────
@app.route('/')
def index():
    # Looks for TradeMind_Tools.html in same folder as server.py
    html_path = os.path.join(BASE_DIR, 'TradeMind_Tools.html')
    if os.path.exists(html_path):
        return send_file(html_path)
    return '<h2 style="font-family:sans-serif;color:#ff3d6e;padding:40px;">TradeMind_Tools.html not found in same folder as server.py</h2>'

# ── Load historical OHLCV from Binance ───────────────────────
def load_history(symbol, interval='1h', limit=500):
    # Fetch up to 1000 candles using pagination (Binance max = 1000 per request)
    try:
        all_candles = []
        end_time = None
        fetch_limit = min(limit, 1000)  # Binance allows max 1000

        res = requests.get('https://api.binance.com/api/v3/klines',
                           params={'symbol': symbol, 'interval': interval,
                                   'limit': fetch_limit},
                           timeout=10)
        data = res.json()
        if not isinstance(data, list):
            return []

        out = [{'time': int(k[0])//1000, 'open': float(k[1]),
                'high': float(k[2]),  'low':  float(k[3]),
                'close':float(k[4]),  'volume':float(k[5])} for k in data]
        print(f"  ✅ {symbol} ({interval}): {len(out)} candles loaded")
        return out
    except Exception as e:
        print(f"  ❌ {symbol} failed: {e}")
        return []

# ── Technical indicators ─────────────────────────────────────
def ema(closes, p):
    k, e = 2/(p+1), closes[0]
    for v in closes[1:]: e = v*k + e*(1-k)
    return e

def rsi(closes, p=14):
    if len(closes) < p+1: return 50.0
    g = sum(max(closes[-p+i]-closes[-p+i-1],0) for i in range(1,p+1))/p
    l = sum(max(closes[-p+i-1]-closes[-p+i],0) for i in range(1,p+1))/p
    return 100 - 100/(1+g/l) if l else 100

def pivots(cs):
    lows, highs = [], []
    for i in range(2, len(cs)-2):
        if cs[i]['low']  < cs[i-1]['low']  < cs[i-2]['low']  and cs[i]['low']  < cs[i+1]['low']:  lows.append(cs[i])
        if cs[i]['high'] > cs[i-1]['high'] > cs[i-2]['high'] and cs[i]['high'] > cs[i+1]['high']: highs.append(cs[i])
    return lows, highs

# ── AI-I: Candle prediction engine ───────────────────────────
# ══════════════════════════════════════════════════════════
# AI-I: PRODUCTION CANDLE PREDICTION ENGINE
# Multi-factor scoring: EMA + RSI + MACD + Volume + Pattern
# Returns: direction, confidence, candle type, predictions
# ══════════════════════════════════════════════════════════
def calc_atr(cs, period=14):
    if len(cs) < period+1: return cs[-1]['high'] - cs[-1]['low']
    trs = []
    for i in range(1, min(period+1, len(cs))):
        c, p = cs[-i], cs[-i-1]
        trs.append(max(c['high']-c['low'], abs(c['high']-p['close']), abs(c['low']-p['close'])))
    return sum(trs)/len(trs) if trs else 0.01

def calc_macd_server(closes):
    if len(closes) < 26: return {'macd':0,'signal':0,'hist':0}
    def ema_s(arr, p):
        k=2/(p+1); e=arr[0]
        for v in arr[1:]: e=v*k+e*(1-k)
        return e
    m = ema_s(closes,12) - ema_s(closes,26)
    # approximate signal
    sig = m * 0.9
    return {'macd':m,'signal':sig,'hist':m-sig}

def ai1(cs):
    if len(cs) < 30: return None
    cl   = [c['close']  for c in cs]
    vol  = [c['volume'] for c in cs]
    last = cs[-1]
    prev = cs[-2]
    prev2= cs[-3]

    # Indicators
    e9   = ema(cl, 9)
    e21  = ema(cl, 21)
    e50  = ema(cl, 50) if len(cl)>=50 else ema(cl, len(cl)//2)
    r    = rsi(cl, 14)
    macd = calc_macd_server(cl)
    atr  = calc_atr(cs, 14)

    avg_vol10 = sum(vol[-10:]) / 10 if len(vol)>=10 else vol[-1]
    avg_vol20 = sum(vol[-20:]) / 20 if len(vol)>=20 else vol[-1]
    vr        = vol[-1] / avg_vol10 if avg_vol10 > 0 else 1

    # Candle analysis
    rng   = last['high'] - last['low']
    body  = abs(last['close'] - last['open'])
    bp    = body/rng if rng > 0 else 0
    uw    = last['high']  - max(last['open'], last['close'])
    lw    = min(last['open'], last['close']) - last['low']
    bull  = last['close'] > last['open']
    uwp   = uw/rng if rng>0 else 0
    lwp   = lw/rng if rng>0 else 0

    # Candle pattern detection
    if bp > 0.88:
        ct = 'Marubozu (Bull)' if bull else 'Marubozu (Bear)'
    elif bull and lwp > 0.55 and bp < 0.35:
        ct = 'Hammer'
    elif not bull and uwp > 0.55 and bp < 0.35:
        ct = 'Shooting Star'
    elif bull and not prev['close']>prev['open'] and last['open']<prev['close'] and last['close']>prev['open']:
        ct = 'Bullish Engulfing'
    elif not bull and prev['close']>prev['open'] and last['open']>prev['close'] and last['close']<prev['open']:
        ct = 'Bearish Engulfing'
    elif bp < 0.08:
        ct = 'Doji'
    elif bp < 0.25 and uwp > 0.30 and lwp > 0.30:
        ct = 'Spinning Top'
    else:
        ct = 'Bullish' if bull else 'Bearish'

    # Trend structure
    highs10 = [c['high'] for c in cs[-10:]]
    lows10  = [c['low']  for c in cs[-10:]]
    uptrend   = highs10[-1]>highs10[0] and lows10[-1]>lows10[0]
    downtrend = highs10[-1]<highs10[0] and lows10[-1]<lows10[0]

    # ── Multi-factor scoring ──────────────────────────────
    bs = ds = ss = 0

    # EMA alignment (weight 20)
    if e9>e21 and e21>e50:   bs+=20
    elif e9<e21 and e21<e50: ds+=20
    else:                    ss+=20

    # RSI zone (weight 18)
    if   60<r<75:  bs+=18
    elif 25<r<40:  ds+=18
    elif r>=75:    ds+=12
    elif r<=25:    bs+=12
    else:          ss+=18

    # MACD (weight 15)
    if macd['macd']>0 and macd['hist']>0:   bs+=15
    elif macd['macd']<0 and macd['hist']<0: ds+=15
    elif macd['hist']>0:                    bs+=8
    else:                                   ds+=8

    # Trend structure (weight 18)
    if uptrend:   bs+=18
    elif downtrend: ds+=18
    else:         ss+=18

    # Volume (weight 15)
    if vr>1.4 and bull:      bs+=15
    elif vr>1.4 and not bull: ds+=15
    elif vr<0.7:             ss+=10
    else:
        if bull: bs+=7
        else:    ds+=7

    # Candle pattern (weight 14)
    pattern_bull = ['Bullish Engulfing','Marubozu (Bull)','Hammer']
    pattern_bear = ['Bearish Engulfing','Marubozu (Bear)','Shooting Star']
    if ct in pattern_bull:  bs+=14
    elif ct in pattern_bear: ds+=14
    else:                   ss+=8

    tot  = bs + ds + ss
    bc   = bs/tot if tot>0 else 0.33
    dc   = ds/tot if tot>0 else 0.33
    sc   = max(0.05, 1-bc-dc)
    dr   = 'BULL' if bc>=dc and bc>=sc else 'BEAR' if dc>bc and dc>=sc else 'SIDE'
    cf   = max(bc, dc, sc)

    # Entry / Stop / Target levels
    recent_highs = [c['high'] for c in cs[-20:]]
    recent_lows  = [c['low']  for c in cs[-20:]]
    pivot_h = max(recent_highs)
    pivot_l = min(recent_lows)
    entry   = last['close']

    if dr=='BULL':
        sl  = min(pivot_l, entry - atr*1.5)
        tp1 = entry + atr*2.0
        tp2 = entry + atr*3.5
    elif dr=='BEAR':
        sl  = max(pivot_h, entry + atr*1.5)
        tp1 = entry - atr*2.0
        tp2 = entry - atr*3.5
    else:
        sl  = entry - atr; tp1 = entry + atr; tp2 = entry + atr*2

    rr = abs(tp1-entry)/abs(sl-entry) if abs(sl-entry)>0 else 1

    # Shadow candle predictions
    preds, p = [], last['close']
    for i in range(1, 5):
        decay     = 0.80 ** (i-1)
        candle_sz = atr * (0.5 + 0.6*(i/4))
        direction = 1 if dr=='BULL' else -1 if dr=='BEAR' else 0.1
        mv        = candle_sz * direction * (0.4 + 0.8*(i/4))
        o=p; cl_p=p+mv; r2=abs(cl_p-o)
        preds.append({
            'time':       last['time'] + i*3600,
            'open':       round(o,    6),
            'high':       round(max(o,cl_p)+r2*0.3, 6),
            'low':        round(min(o,cl_p)-r2*0.3, 6),
            'close':      round(cl_p, 6),
            'confidence': round(cf * decay, 3),
            'direction':  dr,
        })
        p = cl_p

    return {
        'direction':  dr,
        'bull_conf':  round(bc,3), 'bear_conf': round(dc,3), 'side_conf': round(sc,3),
        'confidence': round(cf,3),
        'rsi':        round(r,1),
        'ema9':       round(e9,2), 'ema21': round(e21,2), 'ema50': round(e50,2),
        'vol_ratio':  round(vr,2),
        'candle_type':ct,
        'body_pct':   round(bp*100,1),
        'atr':        round(atr,6),
        'entry':      round(entry,6),
        'stop_loss':  round(sl,6),
        'take_profit1': round(tp1,6),
        'take_profit2': round(tp2,6),
        'risk_reward':  round(rr,2),
        'predictions':  preds,
    }

def ai2(cs, a1):
    if not a1 or len(cs)<10: return None
    pl, ph = pivots(cs)
    tls    = []

    if len(pl)>=3:
        l1,l2,l3 = pl[-3],pl[-2],pl[-1]
        if l3['low']>l2['low']>l1['low']:
            fr = 'HIGH' if a1['direction']=='BEAR' else 'LOW' if a1['direction']=='BULL' else 'MEDIUM'
            tls.append({'type':'UPTREND','touches':len(pl),'quality':min(95,70+len(pl)*3),
                        'price':round(l3['low'],4),'fake_risk':fr,'valid':True,'color':'#00e5a0',
                        'reason':f"Higher lows. {len(pl)} touches. Fake risk: {fr}"})

    if len(ph)>=3:
        h1,h2,h3 = ph[-3],ph[-2],ph[-1]
        if h3['high']<h2['high']<h1['high']:
            fr = 'HIGH' if a1['direction']=='BULL' else 'LOW' if a1['direction']=='BEAR' else 'MEDIUM'
            tls.append({'type':'DOWNTREND','touches':len(ph),'quality':min(90,65+len(ph)*3),
                        'price':round(h3['high'],4),'fake_risk':fr,'valid':True,'color':'#ff3d6e',
                        'reason':f"Lower highs. {len(ph)} touches. Fake risk: {fr}"})

    return {'trendlines':tls,'pivot_lows':len(pl),'pivot_highs':len(ph),'has_valid':len(tls)>0}

# ── Noise filter: 5 criteria ──────────────────────────────────
def noise(a1, a2):
    if not a1: return {'score':0,'decision':'NO_TRADE','passed':0,'scores':{}}
    rm   = {'LOW':85,'MEDIUM':55,'HIGH':25}
    sc   = {'trendline': 80 if a2 and a2['has_valid'] else 30,
            'confidence':int(a1['confidence']*100),
            'volume':    75 if a1['vol_ratio']>1.2 else 40,
            'rsi_zone':  70 if 35<a1['rsi']<65 else 40,
            'fake_risk': rm.get(a2['trendlines'][0]['fake_risk'],50) if a2 and a2['trendlines'] else 60}
    avg    = sum(sc.values())/len(sc)
    passed = sum(1 for v in sc.values() if v>60)
    dec    = 'TRADE' if avg>=70 and passed>=4 else 'CAUTION' if avg>=55 else 'NO_TRADE'
    return {'score':round(avg,1),'passed':passed,'scores':sc,'decision':dec}

# ── Binance WebSocket — real-time candles ─────────────────────
def start_ws():
    streams = '/'.join([s.lower()+'@kline_1h' for s in candles.keys()])
    url     = f"wss://stream.binance.com:9443/stream?streams={streams}"

    def on_msg(ws, msg):
        try:
            k   = json.loads(msg).get('data',{}).get('k',{})
            if not k: return
            sym = k['s']
            c   = {'time':int(k['t'])//1000,'open':float(k['o']),'high':float(k['h']),
                   'low':float(k['l']),'close':float(k['c']),'volume':float(k['v']),'closed':k['x']}
            live_prices[sym] = c['close']

            # Emit live candle every second
            socket.emit('candle_update', {'symbol':sym,'candle':{k2:c[k2] for k2 in ['time','open','high','low','close']}})
            socket.emit('price_tick',    {'symbol':sym,'price':c['close'],'change':c['close']-c['open']})

            # On candle close → run full AI pipeline
            if c['closed'] and sym in candles:
                candles[sym].append(c)
                if len(candles[sym])>200: candles[sym]=candles[sym][-200:]
                cs   = candles[sym]
                a1   = ai1(cs)
                a2   = ai2(cs,a1)
                n    = noise(a1,a2)
                sig  = {'symbol':sym,'ai1':a1,'ai2':a2,'noise':n,'timestamp':c['time'],'price':c['close']}
                ai_signals[sym] = sig
                socket.emit('ai_update', sig)
                e = '🟢' if n['decision']=='TRADE' else '🟡' if n['decision']=='CAUTION' else '🔴'
                print(f"  {e} {sym}: {a1['direction']} {a1['confidence']:.0%} | Score:{n['score']} | {n['decision']}")
        except Exception as ex:
            print(f"  WS error: {ex}")

    def on_close(ws, *a):
        print("  WS closed — reconnecting..."); time.sleep(5); start_ws()

    def on_open(ws):
        print("  ✅ Binance WebSocket connected!\n")

    websocket.WebSocketApp(url, on_message=on_msg, on_close=on_close, on_open=on_open)\
             .run_forever(ping_interval=30, ping_timeout=10)

# ── API endpoints ─────────────────────────────────────────────
@app.route('/api/history/<symbol>')
def get_history(symbol):
    sym = symbol.upper()+'USDT' if not symbol.upper().endswith('USDT') else symbol.upper()
    return {'symbol':sym,'candles':candles.get(sym,[]),'count':len(candles.get(sym,[]))}

@app.route('/api/signal/<symbol>')
def get_signal(symbol):
    sym = symbol.upper()+'USDT' if not symbol.upper().endswith('USDT') else symbol.upper()
    return ai_signals.get(sym,{'message':'Waiting for next candle close...'})

@app.route('/api/status')
def status():
    return {'running':True,'symbols':list(candles.keys()),
            'candle_counts':{k:len(v) for k,v in candles.items()},
            'live_prices':live_prices,
            'decisions':{k:v.get('noise',{}).get('decision') for k,v in ai_signals.items()}}

# ── Socket events ─────────────────────────────────────────────
@socket.on('connect')
def on_connect():
    print(f"  ✅ Browser connected!")

@socket.on('request_history')
def on_req_hist(data):
    sym      = data.get('symbol', 'BTCUSDT')
    interval = data.get('interval', '1h')
    # If client requests different interval, fetch fresh
    if interval != '1h' and sym in candles:
        fresh = load_history(sym, interval=interval, limit=1000)
        if fresh:
            socket.emit('history_data', {'symbol': sym, 'candles': fresh,
                                         'interval': interval})
            return
    socket.emit('history_data', {'symbol': sym,
                                  'candles': candles.get(sym, []),
                                  'interval': interval})

# ── Startup ───────────────────────────────────────────────────
if __name__ == '__main__':
    print()
    print("╔══════════════════════════════════════════════╗")
    print("║        TradeMind — Local Demo Server         ║")
    print("╠══════════════════════════════════════════════╣")
    print("║  Open browser → http://localhost:5000        ║")
    print("║  Phone (same WiFi) → http://YOUR_IP:5000     ║")
    print("║  No API key needed — free Binance data       ║")
    print("╚══════════════════════════════════════════════╝")
    print()
    print("Loading historical data...")
    for sym in candles:
        candles[sym] = load_history(sym)
        time.sleep(0.3)
    print()
    print("Starting WebSocket...")
    threading.Thread(target=start_ws, daemon=True).start()
    print()
    socket.run(app, host='0.0.0.0', port=5000, debug=False)
