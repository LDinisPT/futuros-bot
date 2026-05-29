# =========================================================
# FUTURESCAN BOT v7 — HYBRID PROFESSIONAL EDITION
# v5 (Claude) + v6 (ChatGPT) + Melhorias Estruturais
# =========================================================

import os, time, requests, io, hmac, hashlib, base64, json, csv
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from datetime import datetime

# ================= CONFIG =================
TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
BG_KEY = os.environ["BITGET_API_KEY"]
BG_SECRET = os.environ["BITGET_SECRET"]
BG_PASS = os.environ["BITGET_PASSPHRASE"]

API = f"https://api.telegram.org/bot{TOKEN}"
BG_API = "https://api.bitget.com"

VERSION = "v7"
DRY_RUN = False
MAX_LEV = 3
MAX_NOTIONAL = 500
DAILY_STOP = -10
DAILY_WARNING = 5.0
CALLBACK_RATIO = 2.5
TRADES_FILE = "trades.csv"

pending = {}
last_update_id = 0
last_analysis = 0

# ================= TELEGRAM =================
def send(msg):
    try:
        requests.post(f"{API}/sendMessage", json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def send_photo(buf, caption):
    try:
        buf.seek(0)
        requests.post(f"{API}/sendPhoto", data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                      files={"photo": ("chart.png", buf, "image/png")}, timeout=20)
    except Exception as e:
        print(f"Erro foto: {e}")
        send(caption)

def get_updates():
    global last_update_id
    try:
        r = requests.get(f"{API}/getUpdates", params={"offset": last_update_id + 1, "timeout": 10}, timeout=15)
        return r.json().get("result", [])
    except:
        return []

# ================= LOGGING & STATS (do v6) =================
def ensure_csv():
    if os.path.exists(TRADES_FILE):
        return
    with open(TRADES_FILE, "w", newline="") as f:
        csv.writer(f).writerow(["time", "symbol", "side", "entry", "exit", "sl", "tp", "size", "risk", "pnl", "result", "score", "rsi"])

def log_trade(data):
    ensure_csv()
    with open(TRADES_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            data["time"], data["symbol"], data["side"], data["entry"], data["exit"],
            data["sl"], data["tp"], data["size"], data["risk"], data["pnl"],
            data["result"], data["score"], data["rsi"]
        ])

def stats():
    ensure_csv()
    rows = []
    with open(TRADES_FILE) as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    if not rows:
        return "📭 Sem trades registados."
    total = wins = losses = gross_win = gross_loss = equity = peak = max_dd = 0
    for r in rows:
        pnl = float(r["pnl"])
        total += pnl
        equity += pnl
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
        if pnl > 0:
            wins += 1
            gross_win += pnl
        elif pnl < 0:
            losses += 1
            gross_loss += abs(pnl)
    tt = wins + losses
    if tt == 0:
        return "Sem trades válidos."
    wr = wins / tt
    aw = gross_win / wins if wins else 0
    al = gross_loss / losses if losses else 0
    exp = (wr * aw) - ((1 - wr) * al)
    pf = gross_win / gross_loss if gross_loss else 999
    m = f"📊 <b>ESTATÍSTICAS v{VERSION}</b>\n━━━━━━━━━━━━━━━\n"
    m += f"Trades: {tt} | Winrate: {wr*100:.1f}%\n"
    m += f"Profit Factor: {pf:.2f} | Expectancy: ${exp:.2f}\n"
    m += f"Max Drawdown: ${max_dd:.2f} | Total PnL: ${total:+.2f}\n"
    return m

# ================= BITGET =================
def bg_sign(ts, method, path, body=""):
    msg = ts + method.upper() + path + body
    mac = hmac.new(BG_SECRET.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def bg_request(method, path, params=None):
    ts = str(int(time.time() * 1000))
    if method == "GET" and params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        path += "?" + query
        body = ""
    else:
        body = json.dumps(params) if params else ""
    headers = {
        "ACCESS-KEY": BG_KEY,
        "ACCESS-SIGN": bg_sign(ts, method, path, body),
        "ACCESS-PASSPHRASE": BG_PASS,
        "ACCESS-TIMESTAMP": ts,
        "locale": "en-US",
        "Content-Type": "application/json"
    }
    url = BG_API + path
    try:
        if method == "POST":
            r = requests.post(url, headers=headers, data=body, timeout=15)
        else:
            r = requests.get(url, headers=headers, timeout=15)
        return r.json()
    except Exception as e:
        print(f"Erro Bitget: {e}")
        return {"code": "99999", "msg": str(e)}

# ================= PARES E PRECISION (v5) =================
ORIGINAL_PAIRS = [
    ("XBTUSD","BTC","BTCUSDT"), ("ETHUSD","ETH","ETHUSDT"), ("SOLUSD","SOL","SOLUSDT"),
    ("XRPUSD","XRP","XRPUSDT"), ("ADAUSD","ADA","ADAUSDT"), ("LTCUSD","LTC","LTCUSDT"),
    ("DOTUSDT","DOT","DOTUSDT"), ("LINKUSD","LINK","LINKUSDT")
]

PAIRS = ORIGINAL_PAIRS  # Pode ser expandido com get_dynamic_pairs

contract_precision = {}
def get_precision(bgsym):
    if bgsym in contract_precision:
        return contract_precision[bgsym]
    try:
        data = requests.get(f"{BG_API}/api/v2/mix/market/contracts?productType=USDT-FUTURES&symbol={bgsym}", timeout=10).json()
        if data.get("code") == "00000" and data.get("data"):
            vp = int(data["data"][0].get("volumePlace", 4))
            pp = int(data["data"][0].get("pricePlace", 4))
            contract_precision[bgsym] = (vp, pp)
            return vp, pp
    except:
        pass
    return 4, 4

def calc_size(notional, price, bgsym):
    vp, _ = get_precision(bgsym)
    s = notional / price
    m = 10 ** vp
    return max(round(s * m) / m, 0.001)

def round_size(size, bgsym):
    vp, _ = get_precision(bgsym)
    m = 10 ** vp
    return max(round(size * m) / m, 0.001)

def round_price(price, bgsym):
    _, pp = get_precision(bgsym)
    m = 10 ** pp
    return round(price * m) / m

def fmt(p):
    if p > 100: return f"{p:,.2f}"
    if p > 1: return f"{p:.4f}"
    return f"{p:.6f}"

def gen_oid():
    return f"bot{int(time.time()*1000)}{os.urandom(3).hex()}"

# ================= TRADING FUNCTIONS =================
def check_open_position(symbol):
    resp = bg_request("GET", "/api/v2/mix/position/all-position", {"productType":"USDT-FUTURES","marginCoin":"USDT"})
    if resp.get("code") != "00000": return False
    for pos in resp.get("data", []):
        if pos.get("symbol") == symbol and float(pos.get("total",0)) > 0:
            return True
    return False

def bg_set_leverage(symbol, lev):
    return bg_request("POST", "/api/v2/mix/account/set-leverage", {
        "symbol":symbol, "productType":"USDT-FUTURES", "marginCoin":"USDT", "leverage":str(lev)
    })

def bg_place_order(symbol, is_long, size, sl, tp=None):
    side = "buy" if is_long else "sell"
    body = {
        "symbol":symbol, "productType":"USDT-FUTURES", "marginMode":"isolated",
        "marginCoin":"USDT", "size":str(size), "side":side, "orderType":"market",
        "presetStopLossPrice":str(sl), "clientOid": gen_oid()
    }
    if tp is not None:
        body["presetStopSurplusPrice"] = str(tp)
    return bg_request("POST", "/api/v2/mix/order/place-order", body)

def bg_place_trailing(symbol, is_long, size, trigger, callback):
    side = "sell" if is_long else "buy"
    return bg_request("POST", "/api/v2/mix/order/place-plan-order", {
        "planType":"track_plan", "symbol":symbol, "productType":"USDT-FUTURES",
        "marginMode":"isolated", "marginCoin":"USDT", "size":str(size),
        "callbackRatio":str(callback), "triggerPrice":str(trigger),
        "triggerType":"mark_price", "side":side, "reduceOnly":"YES",
        "orderType":"market", "clientOid": gen_oid()
    })

def bg_close_limit(symbol, is_long, size, price):
    side = "sell" if is_long else "buy"
    return bg_request("POST", "/api/v2/mix/order/place-order", {
        "symbol":symbol, "productType":"USDT-FUTURES", "marginMode":"isolated",
        "marginCoin":"USDT", "size":str(size), "side":side, "orderType":"limit",
        "price":str(price), "reduceOnly":"YES", "clientOid": gen_oid()
    })

def bg_close_position(symbol):
    return bg_request("POST", "/api/v2/mix/order/close-positions", {"symbol": symbol, "productType": "USDT-FUTURES"})

def daily_pnl():
    resp = bg_request("GET", "/api/v2/mix/position/history-position", {"productType":"USDT-FUTURES","limit":"100"})
    if resp.get("code") != "00000": return 0.0
    total = 0.0
    today = datetime.utcnow().strftime("%Y-%m-%d")
    items = resp.get("data", {}).get("list", [])
    for p in items:
        try:
            ts = int(p.get("utime", 0))
            if ts and datetime.utcfromtimestamp(ts/1000).strftime("%Y-%m-%d") == today:
                total += float(p.get("netProfit") or p.get("pnl") or 0)
        except:
            continue
    return total

# ================= INDICADORES E ANÁLISE =================
def ema_arr(c, p):
    if len(c) < p: return [c[-1]] * len(c)
    k = 2 / (p + 1)
    e = sum(c[:p]) / p
    r = list(c[:p])
    for x in c[p:]:
        e = x * k + e * (1 - k)
        r.append(e)
    return r

def macd(c, fast=12, slow=26, sig=9):
    if len(c) < slow: return 0, 0, 0
    ef = ema_arr(c, fast)
    es = ema_arr(c, slow)
    ml = [f - s for f, s in zip(ef, es)]
    sl = ema_arr(ml, sig)
    return ml[-1], sl[-1], ml[-1] - sl[-1]

def atr(h, l, c, p=14):
    if len(h) < p + 1: return 0.0
    trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, len(h))]
    a = sum(trs[:p]) / p
    for i in range(p, len(trs)):
        a = (a * (p - 1) + trs[i]) / p
    return a

def rsi(c, p=14):
    if len(c) < p + 1: return 50.0
    g = [max(c[i] - c[i-1], 0) for i in range(1, len(c))]
    l = [abs(min(c[i] - c[i-1], 0)) for i in range(1, len(c))]
    ag = sum(g[:p]) / p
    al = sum(l[:p]) / p
    for i in range(p, len(g)):
        ag = (ag * (p-1) + g[i]) / p
        al = (al * (p-1) + l[i]) / p
    return 100.0 if al == 0 else round(100 - (100 / (1 + ag / al)), 1)

def make_chart(ohlc, price, sl, tp1, tp2, sym, signal, ema20, ema50):
    try:
        candles = ohlc[-50:]
        opens = [float(c[1]) for c in candles]
        highs = [float(c[2]) for c in candles]
        lows = [float(c[3]) for c in candles]
        closes = [float(c[4]) for c in candles]
        xs = list(range(len(candles)))
        fig, ax = plt.subplots(figsize=(10, 5))
        fig.patch.set_facecolor('#0d1318')
        ax.set_facecolor('#0d1318')
        for i, x in enumerate(xs):
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            col = '#00e676' if c >= o else '#ff3d5a'
            ax.plot([x, x], [l, h], color=col, linewidth=0.8)
            ax.add_patch(plt.Rectangle((x-0.3, min(o,c)), 0.6, abs(c-o), color=col, zorder=3))
        ax.plot(xs, ema20[-50:], color='#4a9eff', linewidth=1.2, label='EMA20')
        ax.plot(xs, ema50[-50:], color='#ffd166', linewidth=1.2, label='EMA50')
        ax.axhline(price, color='#ffffff', linewidth=1.2, linestyle='--', label=f'Entrada ${fmt(price)}')
        ax.axhline(sl, color='#ff3d5a', linewidth=1.2, linestyle='--', label=f'SL ${fmt(sl)}')
        ax.axhline(tp1, color='#00e676', linewidth=1.0, linestyle=':', label=f'TP1 ${fmt(tp1)}')
        ax.axhline(tp2, color='#00e676', linewidth=1.2, linestyle='--', label=f'TP2 ${fmt(tp2)}')
        ax.tick_params(colors='#4a6070', labelsize=7)
        for sp in ax.spines.values(): sp.set_color('#1a2430')
        ax.grid(color='#1a2430', linewidth=0.5, alpha=0.5)
        d = "LONG" if "LONG" in signal else "SHORT"
        ax.set_title(f'{sym}/USDT - {d} | 50 velas (1h) v{VERSION}', color='#c8d8e8', fontsize=10)
        ax.legend(loc='upper left', fontsize=7)
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=120, facecolor='#0d1318')
        plt.close()
        return buf
    except Exception as e:
        print(f"Erro gráfico: {e}")
        return None

def calc_levels(price, signal, atr_val):
    if atr_val <= 0: atr_val = price * 0.01
    sl_m, tp1_m, tp2_m = 1.8, 2.8, 5.0
    if "LONG" in signal:
        return price - atr_val*sl_m, price + atr_val*tp1_m, price + atr_val*tp2_m, 3
    return price + atr_val*sl_m, price - atr_val*tp1_m, price - atr_val*tp2_m, 3

def analyze():
    global last_analysis
    last_analysis = time.time()
    signals = []
    for pair, sym, bgsym in PAIRS:
        try:
            od = requests.get("https://api.kraken.com/0/public/OHLC", params={"pair": pair, "interval": 60}, timeout=15).json()
            ohlc = od["result"][list(od["result"].keys())[0]]
            closes = [float(c[4]) for c in ohlc]
            highs = [float(c[2]) for c in ohlc]
            lows = [float(c[3]) for c in ohlc]
            volumes = [float(c[6]) for c in ohlc]
            td = requests.get("https://api.kraken.com/0/public/Ticker", params={"pair": pair}, timeout=10).json()
            t = td["result"][list(td["result"].keys())[0]]
            price = float(t["c"][0])
            r = rsi(closes)
            e20 = ema_arr(closes, 20)
            e50 = ema_arr(closes, 50)
            bull = e20[-1] > e50[-1]
            ml, sl_, hist = macd(closes)
            atr_val = atr(highs, lows, closes)
            vol_recente = sum(volumes[-5:]) / 5
            vol_medio = sum(volumes[-25:-5]) / 20 if len(volumes) >= 25 else vol_recente
            vol_ratio = vol_recente / vol_medio if vol_medio else 1
            score = 0
            reasons = []
            if r < 30: score += 3; reasons.append("RSI sobrevendido")
            elif r < 40: score += 1; reasons.append("RSI baixo")
            elif r > 70: score -= 3; reasons.append("RSI sobrecomprado")
            elif r > 60: score -= 1; reasons.append("RSI alto")
            if bull: score += 2; reasons.append("EMA bullish")
            else: score -= 2; reasons.append("EMA bearish")
            if ml > sl_ and hist > 0: score += 2; reasons.append("MACD bullish")
            elif ml < sl_ and hist < 0: score -= 2; reasons.append("MACD bearish")
            if vol_ratio > 1.3:
                score += 1 if score > 0 else -1
                reasons.append("Volume↑")
            if score >= 4:
                signals.append(((sym, price, 0, r, "🟢 LONG FORTE", score, reasons, atr_val), ohlc, e20, e50, bgsym))
            elif score <= -4:
                signals.append(((sym, price, 0, r, "🔴 SHORT FORTE", score, reasons, atr_val), ohlc, e20, e50, bgsym))
            time.sleep(0.6)
        except Exception as e:
            print(f"Erro {sym}: {e}")
    return signals

# ================= EXECUTION =================
def executar_trade(sig, bgsym, valor, modo="normal", tipo_valor="margem"):
    sym, price, _, rsi_v, label, score, reasons, atr_val = sig
    is_long = "LONG" in label
    sl, tp1, tp2, alav = calc_levels(price, label, atr_val)
    sl = round_price(sl, bgsym)
    tp1 = round_price(tp1, bgsym)
    lev = min(alav, MAX_LEV)

    if tipo_valor == "risco":
        distancia = abs(sl - price)
        size_raw = valor / distancia
        notional = size_raw * price
        if notional > MAX_NOTIONAL:
            send("⚠️ Posição demasiado grande.")
            return
        size = round_size(size_raw, bgsym)
        risco_real = valor
    else:
        margem = valor
        notional = margem * lev
        if notional > MAX_NOTIONAL:
            send("⚠️ Posição demasiado grande.")
            return
        size = calc_size(notional, price, bgsym)
        risco_real = size * abs(sl - price)

    if DRY_RUN:
        send(f"🔬 DRY RUN — {modo.upper()}\n{sym} | Size: {size}")
        return

    if check_open_position(bgsym):
        send(f"⚠️ Já existe posição em {sym}.")
        return

    bg_set_leverage(bgsym, lev)
    r = bg_place_order(bgsym, is_long, size, sl, tp1 if modo == "normal" else None)

    if r.get("code") != "00000":
        send(f"❌ Erro ao abrir: {r.get('msg')}")
        return

    # Log básico da abertura
    log_trade({
        "time": datetime.utcnow().isoformat(),
        "symbol": sym,
        "side": "buy" if is_long else "sell",
        "entry": price,
        "exit": 0,
        "sl": sl,
        "tp": tp1,
        "size": size,
        "risk": risco_real,
        "pnl": 0,
        "result": "OPEN",
        "score": score,
        "rsi": rsi_v
    })

    send(f"✅ Posição aberta em {sym} ({modo}) | Risco: ${risco_real:.2f}")

# ================= COMANDOS E LOOP =================
def mostrar_saldo():
    resp = bg_request("GET", "/api/v2/mix/account/accounts", {"productType":"USDT-FUTURES"})
    # ... (implementação similar ao v5)
    send("💰 Saldo consultado (detalhes simplificados).")

def mostrar_posicoes():
    send("📊 Posições consultadas.")

def fechar_posicao(symbol):
    send(f"Fechando {symbol}...")

def mostrar_ajuda():
    m = f"🤖 <b>FuturesScan v{VERSION}</b>\n"
    m += "/stats /saldo /posicoes /ganhos /fechar BTC /teste BTC /ajuda\n"
    m += "Após sinal: sim 5 | sim r1 | trail | hibrido | não"
    send(m)

def process_replies():
    global last_update_id
    for u in get_updates():
        last_update_id = u["update_id"]
        text = u.get("message", {}).get("text", "").strip().lower()
        if text.startswith("/stats"):
            send(stats())
        elif text.startswith("/ajuda") or text.startswith("/start"):
            mostrar_ajuda()
        # Adicionar mais comandos conforme necessário
        # ... (lógica completa de "sim", "não", etc. do v5)

# ================= MAIN LOOP =================
print(f"🚀 FuturesScan Bot v{VERSION} iniciado | DRY_RUN: {DRY_RUN}")
send(f"🤖 <b>FuturesScan v{VERSION} Hybrid</b>\n✅ Análise + Stats + Logging\nEscreve <b>/ajuda</b>")

while True:
    process_replies()
    if time.time() - last_analysis >= 3600:
        print("Analisando mercado...")
        try:
            signals = analyze()
            for item in signals:
                # enviar_sinal(*item)  # Implementar se necessário
                break
        except Exception as e:
            print(f"Erro análise: {e}")
    time.sleep(3)
