import os
import time
import requests
import io
import hmac
import hashlib
import base64
import json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ==================== CONFIGURAÇÃO ====================
TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
BG_KEY = os.environ["BITGET_API_KEY"]
BG_SECRET = os.environ["BITGET_SECRET"]
BG_PASS = os.environ["BITGET_PASSPHRASE"]

API = f"https://api.telegram.org/bot{TOKEN}"
BG_API = "https://api.bitget.com"

MAX_LEV = 3
DRY_RUN = False

pending = {}
last_update_id = 0
last_analysis = 0

def send(msg):
    try:
        requests.post(f"{API}/sendMessage", json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Erro telegram: {e}")

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

def fmt(p):
    if p > 100: return f"{p:,.2f}"
    if p > 1: return f"{p:.4f}"
    return f"{p:.6f}"

# ==================== BITGET HELPERS ====================
def bg_sign(ts, method, path, body=""):
    msg = ts + method.upper() + path + body
    mac = hmac.new(BG_SECRET.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def bg_request(method, path, body_dict=None):
    ts = str(int(time.time() * 1000))
    body = json.dumps(body_dict) if body_dict else ""
    sign = bg_sign(ts, method, path, body)
    headers = {
        "ACCESS-KEY": BG_KEY, "ACCESS-SIGN": sign, "ACCESS-PASSPHRASE": BG_PASS,
        "ACCESS-TIMESTAMP": ts, "locale": "en-US", "Content-Type": "application/json"
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

# ==================== PARES DINÂMICOS + PRECISÃO v3.5 ====================
def get_dynamic_pairs():
    try:
        url = f"{BG_API}/api/v2/mix/market/contracts?productType=USDT-FUTURES"
        r = requests.get(url, timeout=15)
        data = r.json()
        if data.get("code") != "00000" or not data.get("data"):
            raise Exception("Resposta inválida")
        contracts = data.get("data", [])
        kraken_map = {
            "BTCUSDT": ("XBTUSD", "BTC"), "ETHUSDT": ("ETHUSD", "ETH"),
            "SOLUSDT": ("SOLUSD", "SOL"), "XRPUSDT": ("XRPUSD", "XRP"),
            "ADAUSDT": ("ADAUSD", "ADA"), "DOTUSDT": ("DOTUSDT", "DOT"),
            "LINKUSDT": ("LINKUSD", "LINK"), "UNIUSDT": ("UNIUSD", "UNI"),
            "ATOMUSDT": ("ATOMUSD", "ATOM"), "LTCUSDT": ("LTCUSD", "LTC"),
            "DOGEUSDT": ("XDGUSD", "DOGE"), "AAVEUSDT": ("AAVEUSD", "AAVE"),
            "AVAXUSDT": ("AVAXUSD", "AVAX"), "NEARUSDT": ("NEARUSD", "NEAR"),
            "TRXUSDT": ("TRXUSD", "TRX"), "BCHUSDT": ("BCHUSD", "BCH"),
            "FILUSDT": ("FILUSD", "FIL"), "ETCUSDT": ("ETCUSD", "ETC"),
            "SUIUSDT": ("SUIUSD", "SUI"), "TONUSDT": ("TONUSD", "TON"),
            "INJUSDT": ("INJUSD", "INJ"),
        }
        dynamic_pairs = []
        for c in contracts:
            symbol = c.get("symbol")
            if symbol in kraken_map:
                kraken_pair, display_sym = kraken_map[symbol]
                dynamic_pairs.append((kraken_pair, display_sym, symbol))
        print(f"✅ Carregados {len(dynamic_pairs)} pares dinâmicos da Bitget")
        return dynamic_pairs
    except Exception as e:
        print(f"⚠️ Erro ao buscar pares: {e} - usando fallback")
        return ORIGINAL_PAIRS

ORIGINAL_PAIRS = [
    ("XBTUSD", "BTC", "BTCUSDT"), ("ETHUSD", "ETH", "ETHUSDT"),
    ("SOLUSD", "SOL", "SOLUSDT"), ("XRPUSD", "XRP", "XRPUSDT"),
    ("ADAUSD", "ADA", "ADAUSDT"), ("DOTUSDT", "DOT", "DOTUSDT"),
    ("LINKUSD", "LINK", "LINKUSDT"), ("UNIUSD", "UNI", "UNIUSDT"),
    ("ATOMUSD", "ATOM", "ATOMUSDT"), ("LTCUSD", "LTC", "LTCUSDT"),
    ("XDGUSD", "DOGE", "DOGEUSDT"), ("AAVEUSD", "AAVE", "AAVEUSDT")
]

PAIRS = get_dynamic_pairs()

contract_precision = {}
def get_contract_precision(bgsym):
    if bgsym in contract_precision:
        return contract_precision[bgsym]
    try:
        url = f"{BG_API}/api/v2/mix/market/contracts?productType=USDT-FUTURES&symbol={bgsym}"
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("code") == "00000" and data.get("data"):
            vol_place = int(data["data"][0].get("volumePlace", 4))
            price_place = int(data["data"][0].get("pricePlace", 4))
            contract_precision[bgsym] = (vol_place, price_place)
            return vol_place, price_place
    except:
        pass
    return 4, 4

def calc_size(notional, price, bgsym):
    vol_place, _ = get_contract_precision(bgsym)
    s = notional / price
    multiplier = 10 ** vol_place
    size = round(s * multiplier) / multiplier
    size = max(size, 0.001)
    print(f"DEBUG SIZE - {bgsym} | Notional: ${notional:.2f} | Price: ${price:.4f} | Size: {size}")
    return size

def round_price(price, bgsym):
    _, price_place = get_contract_precision(bgsym)
    multiplier = 10 ** price_place
    return round(price * multiplier) / multiplier

# ==================== LÓGICA DE TRADING ====================
def check_open_position(symbol):
    resp = bg_request("GET", "/api/v2/mix/position/all-position", {"symbol": symbol, "productType": "USDT-FUTURES"})
    if resp.get("code") != "00000": return False
    for pos in resp.get("data", []):
        if pos.get("symbol") == symbol and float(pos.get("total", 0)) > 0:
            return True
    return False

def get_funding_rate(bgsym):
    resp = bg_request("GET", "/api/v2/mix/market/current-fund-rate", {"symbol": bgsym, "productType": "USDT-FUTURES"})
    if resp.get("code") == "00000" and resp.get("data"):
        return float(resp["data"][0]["fundingRate"]) * 100
    return 0.0

def bg_set_leverage(symbol, lev):
    return bg_request("POST", "/api/v2/mix/account/set-leverage", {
        "symbol": symbol, "productType": "USDT-FUTURES", "marginCoin": "USDT", "leverage": str(lev)
    })

def bg_place_order(symbol, is_long, size, sl, tp):
    side = "buy" if is_long else "sell"
    return bg_request("POST", "/api/v2/mix/order/place-order", {
        "symbol": symbol, "productType": "USDT-FUTURES", "marginMode": "isolated",
        "marginCoin": "USDT", "size": str(size), "side": side, "orderType": "market",
        "presetStopSurplusPrice": str(round_price(tp, symbol)), 
        "presetStopLossPrice": str(round_price(sl, symbol))
    })

def calc_levels(price, signal, atr_val):
    if atr_val <= 0: atr_val = price * 0.01
    sl_mult = 1.8; tp1_mult = 2.8; tp2_mult = 5.0
    if "LONG" in signal:
        sl = price - atr_val*sl_mult
        tp1 = price + atr_val*tp1_mult
        tp2 = price + atr_val*tp2_mult
    else:
        sl = price + atr_val*sl_mult
        tp1 = price - atr_val*tp1_mult
        tp2 = price - atr_val*tp2_mult
    return sl, tp1, tp2, 3

def executar_trade(sig, bgsym, margem):
    if DRY_RUN:
        send("🔬 <b>DRY RUN ATIVADO (v3.5)</b>")
        return
    sym, price, _, rsi_v, label, score, reasons, atr_val = sig
    if check_open_position(bgsym):
        send(f"⚠️ Já existe posição aberta em <b>{sym}</b>.")
        return
    is_long = "LONG" in label
    sl, tp1, tp2, alav = calc_levels(price, label, atr_val)
    lev = min(alav, MAX_LEV)
    notional = margem * lev
    size = calc_size(notional, price, bgsym)
    r1 = bg_set_leverage(bgsym, lev)
    r2 = bg_place_order(bgsym, is_long, size, sl, tp1)
    if r2.get("code") == "00000":
        arrow = "↑" if is_long else "↓"
        m = f"✅ <b>POSIÇÃO ABERTA v3.5!</b>\n━━━━━━━━━━━━━━━\n"
        m += f"{arrow} <b>{sym}/USDT — {'LONG' if is_long else 'SHORT'}</b>\n"
        m += f"💲 Entrada: ~${fmt(price)}\n"
        m += f"⚡ Alavancagem: {lev}x\n"
        m += f"💰 Margem: ${margem}\n"
        m += f"📊 Tamanho: {size}\n"
        m += f"🛑 SL: ${fmt(sl)} (ATR)\n"
        m += f"🎯 TP1: ${fmt(tp1)}\n"
        m += f"━━━━━━━━━━━━━━━\n"
        m += f"⚠️ TP2 (${fmt(tp2)}) manual\n✅ Confirma na Bitget!"
        send(m)
    else:
        send(f"❌ Erro ao abrir: {r2.get('msg','?')}")

# ==================== GRÁFICO ====================
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
            color = '#00e676' if c >= o else '#ff3d5a'
            ax.plot([x, x], [l, h], color=color, linewidth=0.8)
            ax.add_patch(plt.Rectangle((x - 0.3, min(o, c)), 0.6, abs(c - o), color=color, zorder=3))
        ax.plot(xs, ema20[-50:], color='#4a9eff', linewidth=1.2, label='EMA20')
        ax.plot(xs, ema50[-50:], color='#ffd166', linewidth=1.2, label='EMA50')
        ax.axhline(price, color='#ffffff', linewidth=1.2, linestyle='--', label=f'Entrada ${fmt(price)}')
        ax.axhline(sl, color='#ff3d5a', linewidth=1.2, linestyle='--', label=f'SL ${fmt(sl)}')
        ax.axhline(tp1, color='#00e676', linewidth=1.0, linestyle=':', label=f'TP1 ${fmt(tp1)}')
        ax.axhline(tp2, color='#00e676', linewidth=1.2, linestyle='--', label=f'TP2 ${fmt(tp2)}')
        ax.tick_params(colors='#4a6070', labelsize=7)
        for sp in ax.spines.values(): sp.set_color('#1a2430')
        ax.yaxis.set_tick_params(labelcolor='#c8d8e8')
        ax.xaxis.set_tick_params(labelbottom=False)
        ax.grid(color='#1a2430', linewidth=0.5, alpha=0.5)
        d = "LONG" if "LONG" in signal else "SHORT"
        ax.set_title(f'{sym}/USD - {d} | 50 velas (1h) v3.5', color='#c8d8e8', fontsize=10, pad=10)
        ax.legend(loc='upper left', fontsize=7, facecolor='#0d1318', edgecolor='#1a2430', labelcolor='#c8d8e8')
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=120, facecolor='#0d1318')
        plt.close()
        return buf
    except Exception as e:
        print(f"Erro gráfico: {e}")
        return None

# ==================== INDICADORES (igual à anterior) ====================
def ema_arr(closes, period):
    if len(closes) < period:
        return [closes[-1]] * len(closes)
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    result = list(closes[:period])
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
        result.append(ema)
    return result

def macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow: return 0, 0, 0
    ema_fast = ema_arr(closes, fast)
    ema_slow = ema_arr(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema_arr(macd_line, signal)
    histogram = macd_line[-1] - signal_line[-1]
    return macd_line[-1], signal_line[-1], histogram

def atr(highs, lows, closes, period=14):
    if len(highs) < period + 1: return 0.0
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])) for i in range(1, len(highs))]
    atr_val = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr_val = (atr_val * (period - 1) + trs[i]) / period
    return atr_val

def rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
    losses = [abs(min(closes[i] - closes[i-1], 0)) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)

# ==================== ANÁLISE ====================
def analyze():
    global last_analysis
    last_analysis = time.time()
    signals = []
    for pair, sym, bgsym in PAIRS:
        try:
            ohlc_data = requests.get("https://api.kraken.com/0/public/OHLC", params={"pair": pair, "interval": 60}, timeout=15).json()
            ohlc = ohlc_data["result"][list(ohlc_data["result"].keys())[0]]
            closes = [float(c[4]) for c in ohlc]
            highs = [float(c[2]) for c in ohlc]
            lows = [float(c[3]) for c in ohlc]
            ticker_data = requests.get("https://api.kraken.com/0/public/Ticker", params={"pair": pair}, timeout=10).json()
            t = ticker_data["result"][list(ticker_data["result"].keys())[0]]
            price = float(t["c"][0])
            op = float(t["o"])
            change = round((price - op) / op * 100, 2)

            r = rsi(closes)
            e20 = ema_arr(closes, 20)
            e50 = ema_arr(closes, 50)
            bull = e20[-1] > e50[-1]
            macd_line, signal_line, hist = macd(closes)
            atr_val = atr(highs, lows, closes)
            funding = get_funding_rate(bgsym)

            score = 0
            reasons = []

            if r < 30: score += 3; reasons.append("RSI sobrevendido")
            elif r < 40: score += 1; reasons.append("RSI baixo")
            elif r > 70: score -= 3; reasons.append("RSI sobrecomprado")
            elif r > 60: score -= 1; reasons.append("RSI alto")

            if bull: score += 2; reasons.append("EMA bullish")
            else: score -= 2; reasons.append("EMA bearish")
            if price > e20[-1] and bull: score += 1
            elif price < e20[-1] and not bull: score -= 1

            if macd_line > signal_line and hist > 0:
                score += 2; reasons.append("MACD bullish")
            elif macd_line < signal_line and hist < 0:
                score -= 2; reasons.append("MACD bearish")

            if funding > 0.01 and bull:
                score -= 1; reasons.append(f"Funding alto ({funding:.3f}%)")
            elif funding < -0.01 and not bull:
                score -= 1; reasons.append(f"Funding baixo ({funding:.3f}%)")

            if score >= 4:
                label = "🟢 LONG FORTE"
                signals.append(((sym, price, change, r, label, score, reasons, atr_val), ohlc, e20, e50, bgsym))
            elif score <= -4:
                label = "🔴 SHORT FORTE"
                signals.append(((sym, price, change, r, label, score, reasons, atr_val), ohlc, e20, e50, bgsym))

            print(f"{sym}: RSI={r} MACD={hist:+.4f} ATR={atr_val:.6f} Funding={funding:.3f}% score={score}")
            time.sleep(0.6)
        except Exception as e:
            print(f"Erro {sym}: {e}")
    return signals

def enviar_sinal(sig, ohlc, e20, e50, bgsym):
    sym, price, change, rsi_v, label, score, reasons, atr_val = sig
    sl, tp1, tp2, alav = calc_levels(price, label, atr_val)
    lev = min(alav, MAX_LEV)
    arrow = "↑" if "LONG" in label else "↓"
    cap = f"{arrow} <b>{sym}/USD — {label} v3.5</b>\n━━━━━━━━━━━━━━━\n"
    cap += f"💲 Entrada: ${fmt(price)}\n"
    cap += f"🛑 SL: ${fmt(sl)} (ATR)\n"
    cap += f"🎯 TP1: ${fmt(tp1)}\n"
    cap += f"🎯 TP2: ${fmt(tp2)}\n"
    cap += f"⚡ Alavancagem: {lev}x\n"
    cap += "━━━━━━━━━━━━━━━\n"
    cap += f"📉 RSI: {rsi_v} | Score: {score:+d}/9\n"
    cap += f"📌 {', '.join(reasons)}\n"
    cap += "━━━━━━━━━━━━━━━\n"
    cap += "💰 <b>ENTRAR?</b>\n✅ Responde: <b>sim VALOR</b> (ex: sim 5)\n❌ Ou: <b>não</b>\n"
    cap += "⚠️ <i>Não é aconselhamento financeiro.</i>"
    pending[CHAT_ID] = (sig, ohlc, e20, e50, bgsym)
    buf = make_chart(ohlc, price, sl, tp1, tp2, sym, label, e20, e50)
    if buf:
        send_photo(buf, cap)
    else:
        send(cap)

def process_replies():
    global last_update_id
    for u in get_updates():
        last_update_id = u["update_id"]
        text = u.get("message", {}).get("text", "").strip().lower()
        if not text or text.startswith("/"): continue
        if CHAT_ID not in pending: continue
        if text in ("não", "nao", "n", "no"):
            send("❌ Sinal cancelado.")
            pending.pop(CHAT_ID, None)
            continue
        if text.startswith(("sim", "s ")):
            partes = text.replace("sim", "").replace("s", "").strip().split()
            try:
                margem = float(partes[0].replace("$", "").replace(",", "."))
                sig, ohlc, e20, e50, bgsym = pending[CHAT_ID]
                send(f"⏳ A abrir posição com ${margem}...")
                executar_trade(sig, bgsym, margem)
                pending.pop(CHAT_ID, None)
            except Exception as e:
                send(f"⚠️ Formato errado. Ex: <b>sim 5</b>")

# ==================== LOOP PRINCIPAL ====================
print("🤖 Bot iniciado! (versão v3.5 — Precisão total corrigida)")
send("🤖 <b>FuturesScan Bot v3.5</b>\n✅ Pares dinâmicos + SL/TP arredondados\nPodes testar com $1 ou $2")

while True:
    process_replies()
    if time.time() - last_analysis >= 3600:
        print("A analisar mercado (v3.5)...")
        try:
            signals = analyze()
            for item in signals:
                sig, ohlc, e20, e50, bgsym = item
                enviar_sinal(sig, ohlc, e20, e50, bgsym)
                break
            if not signals:
                print("Sem sinais fortes esta hora.")
        except Exception as e:
            print(f"Erro geral: {e}")
    time.sleep(3)
