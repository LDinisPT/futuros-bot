import os, time, requests

TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
API = f"https://api.telegram.org/bot{TOKEN}"

PAIRS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","DOGEUSDT",
    "BNBUSDT","ADAUSDT","LINKUSDT","AVAXUSDT","NEARUSDT",
    "INJUSDT","SUIUSDT","PEPEUSDT","AAVEUSDT","WIFUSDT",
    "ARBUSDT","OPUSDT","DOTUSDT","UNIUSDT","ATOMUSDT"
]

def send(msg):
    try:
        requests.post(f"{API}/sendMessage",
            json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"HTML"},timeout=10)
    except Exception as e:
        print(f"Erro telegram: {e}")

def get_klines(symbol):
    r = requests.get(
        f"https://fapi.binance.com/fapi/v1/klines",
        params={"symbol":symbol,"interval":"1h","limit":55},
        timeout=15)
    return r.json()

def get_ticker(symbol):
    r = requests.get(
        f"https://fapi.binance.com/fapi/v1/ticker/24hr",
        params={"symbol":symbol},timeout=10)
    return r.json()

def rsi(closes, p=14):
    if len(closes) < p+1: return 50
    g = l = 0
    for i in range(len(closes)-p, len(closes)):
        d = closes[i]-closes[i-1]
        if d > 0: g += d
        else: l -= d
    ag, al = g/p, l/p
    if al == 0: return 100
    return round(100-(100/(1+ag/al)), 1)

def ema(closes, p):
    if len(closes) < p: return closes[-1]
    k = 2/(p+1)
    e = sum(closes[:p])/p
    for x in closes[p:]: e = x*k + e*(1-k)
    return e

def analyze():
    signals = []
    for sym in PAIRS:
        try:
            klines = get_klines(sym)
            closes = [float(k[4]) for k in klines]
            ticker = get_ticker(sym)
            price = float(ticker["lastPrice"])
            change = float(ticker["priceChangePercent"])
            vol24 = float(ticker["quoteVolume"])

            r = rsi(closes)
            e20 = ema(closes[-20:], 20)
            e50 = ema(closes[-50:], 50)
            bull = e20 > e50

            score = 0
            reasons = []
            if r < 30: score+=3; reasons.append("RSI sobrevendido")
            elif r < 40: score+=1; reasons.append("RSI baixo")
            elif r > 70: score-=3; reasons.append("RSI sobrecomprado")
            elif r > 60: score-=1; reasons.append("RSI alto")
            if bull: score+=2; reasons.append("EMA20 > EMA50")
            else: score-=2; reasons.append("EMA20 < EMA50")
            if price > e20 and bull: score+=1; reasons.append("Preço acima EMA20")
            elif price < e20 and not bull: score-=1; reasons.append("Preço abaixo EMA20")
            if change > 5: score+=1; reasons.append("Momentum forte")
            elif change < -5: score-=1; reasons.append("Queda forte")

            if score >= 4:
                signals.append((sym.replace("USDT",""), price, change, r, "🟢 LONG FORTE", score, reasons))
            elif score <= -4:
                signals.append((sym.replace("USDT",""), price, change, r, "🔴 SHORT FORTE", score, reasons))

            time.sleep(0.3)
        except Exception as e:
            print(f"Erro {sym}: {e}")
    return signals

def fmt(p):
    if p > 100: return f"{p:,.2f}"
    if p > 1: return f"{p:.4f}"
    return f"{p:.6f}"

print("Bot iniciado!")
send("🤖 <b>FuturesScan Bot iniciado!</b>\nA analisar mercado a cada hora ⏱\nVais receber alertas de sinais LONG/SHORT fortes!")

last = set()
while True:
    print("A analisar mercado...")
    try:
        signals = analyze()
        sym_set = {s[0] for s in signals}
        new = [s for s in signals if s[0] not in last]
        if new:
            msg = "📊 <b>SINAIS FUTUROS USDT</b>\n\n"
            for sym,price,change,rsi_v,label,score,reasons in new:
                msg += f"{'↑' if 'LONG' in label else '↓'} <b>{sym}/USDT</b> {label}\n"
                msg += f"   💲 ${fmt(price)}\n"
                msg += f"   📈 24h: {change:+.2f}%\n"
                msg += f"   📉 RSI: {rsi_v}\n"
                msg += f"   ⚡ Score: {score:+d}/7\n"
                msg += f"   📌 {', '.join(reasons)}\n\n"
            msg += "⚠️ <i>Não é aconselhamento financeiro.</i>"
            send(msg)
        else:
            print("Sem novos sinais fortes.")
        last = sym_set
    except Exception as e:
        print(f"Erro geral: {e}")
    time.sleep(3600)
