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
DRY_RUN = False   # Muda para True apenas para testes sem abrir ordens

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

# ==================== PARES DINÂMICOS + PRECISÃO v3.3 ====================
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

# ==================== PRECISÃO DO CONTRATO (novo na v3.3) ====================
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
            contract_precision[bgsym] = vol_place
            return vol_place
    except:
        pass
    return 4  # fallback seguro

# ==================== CÁLCULO DE TAMANHO (corrigido v3.3) ====================
def calc_size(notional, price, bgsym):
    precision = get_contract_precision(bgsym)
    s = notional / price
    multiplier = 10 ** precision
    size = round(s * multiplier) / multiplier
    return max(size, 0.001)  # mínimo seguro

# ==================== RESTO DO CÓDIGO ====================
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
        "presetStopSurplusPrice": str(round(tp, 4)), "presetStopLossPrice": str(round(sl, 4))
    })

def calc_levels(price, signal, atr_val):
    if atr_val <= 0: atr_val = price * 0.01
    sl_mult = 1.8; tp1_mult = 2.8; tp2_mult = 5.0
    if "LONG" in signal:
        return price - atr_val*sl_mult, price + atr_val*tp1_mult, price + atr_val*tp2_mult, 3
    else:
        return price + atr_val*sl_mult, price - atr_val*tp1_mult, price - atr_val*tp2_mult, 3

def executar_trade(sig, bgsym, margem):
    if DRY_RUN:
        send("🔬 <b>DRY RUN ATIVADO (v3.3)</b>")
        return
    sym, price, _, rsi_v, label, score, reasons, atr_val = sig
    if check_open_position(bgsym):
        send(f"⚠️ Já existe posição aberta em <b>{sym}</b>.")
        return
    is_long = "LONG" in label
    sl, tp1, tp2, alav = calc_levels(price, label, atr_val)
    lev = min(alav, MAX_LEV)
    notional = margem * lev
    size = calc_size(notional, price, bgsym)          # ← CORRIGIDO

    r1 = bg_set_leverage(bgsym, lev)
    r2 = bg_place_order(bgsym, is_long, size, sl, tp1)

    if r2.get("code") == "00000":
        arrow = "↑" if is_long else "↓"
        m = f"✅ <b>POSIÇÃO ABERTA v3.3!</b>\n━━━━━━━━━━━━━━━\n"
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

# ==================== GRÁFICO E ANÁLISE (igual à v3.2) ====================
# (O resto do código é idêntico à v3.2 que enviei anteriormente – indicadores, make_chart, analyze, enviar_sinal, process_replies, loop principal)

# Para não tornar esta mensagem gigantesca, o resto do código é exatamente o mesmo da v3.2 que te enviei antes.

# Se quiseres o ficheiro 100% completo numa única mensagem, confirma que queres que eu cole tudo novamente.

print("🤖 Bot iniciado! (versão v3.3 — Precisão de tamanho corrigida)")
send("🤖 <b>FuturesScan Bot v3.3</b>\n✅ Pares dinâmicos + correção de tamanho Bitget\nPodes testar com $1 ou $2")

while True:
    process_replies()
    if time.time() - last_analysis >= 3600:
        print("A analisar mercado (v3.3)...")
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
