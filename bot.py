import os, time, requests

TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
API = f"https://api.telegram.org/bot{TOKEN}"

PAIRS = [
    ("XBTUSD","BTC"),("ETHUSD","ETH"),("SOLUSD","SOL"),
    ("XRPUSD","XRP"),("ADAUSD","ADA"),("DOTUSD","DOT"),
    ("LINKUSD","LINK"),("UNIUSD","UNI"),("ATOMUSD","ATOM"),
    ("LTCUSD","LTC"),("AVAXUSD","AVAX"),("NEARUSD","NEAR"),
    ("AAVEUSD","AAVE"),("DOGEUSD","DOGE"),("MATICUSD","MATIC")
]

def send(msg):
    try:
        requests.post(f"{API}/sendMessage",
            json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"HTML"},timeout=10)
    except Exception as e:
        print(f"Erro telegram: {e}")

def get_ohlc(pair):
    r = requests.get("https://api.kraken.com/0/public/OHLC",
        params={"pair":pair,"interval":60},timeout=15)
    data = r.json()
    if data["error"]: raise Exception(str(data["error"]))
    key = list(data["result"].keys())[0]
    return data["result"][key]

def get_ticker(pair):
    r = requests.get("https://api.kraken.com/0/public/Ticker",
        params={"pair":pair},timeout=10)
    data = r.json()
    if data["error"]: raise Exception(str(data["error"]))
    key = list(data["result"].keys())[0]
    return data["result"][key]

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
    for pair, sym in PAIRS:
        try:
            ohlc = get_ohlc(pair)
            closes = [float(c[4]) for c in ohlc]
            ticker = get_ticker(pair)
            price = float(ticker["c"][0])
            open24 = float(ticker["o"])
            change = round((price - open24) / open24 * 100, 2)

            r = rsi(closes)
            e20 = ema(closes[-20:], 20)
            e50 = ema(closes[-50:], 50) if len(closes) >= 50 else ema(closes, len(closes))
            bull = e20 > e50

            score = 0
            reasons = []
            if r < 30: score+=3; reasons.append("RSI sobrevendido")
            elif r < 40: score+=1; reasons.append("RSI baixo")
            elif r > 70: score-=3; reasons.append("RSI sobrecomprado")
            elif r > 60: score-=1; reasons.append("RSI alto")
            if bull: score+=2; reasons.append("EMA20>EMA50 bullish")
            else: score-=2; reasons.append("EMA20<EMA50 bearish")
            if price > e20 and bull: score+=1
            elif price < e20 and not bull: score-=1
            if change > 5: score+=1; reasons.append("Momentum forte")
            elif change < -5: score-=1; reasons.append("Queda forte")

            if score >= 4:
                signals.append((sym, price, change, r, "🟢 LONG FORTE", score, reasons))
            elif score <= -4:
                signals.append((sym, price, change, r, "🔴 SHORT FORTE", score, reasons))

            print(f"{sym}: RSI={r} score={score}")
            time.sleep(1)
        except Exception as e:
            print(f"Erro {sym}: {e}")
    return signals

def fmt(p):
    if p > 100: return f"{p:,.2f}"
    if p > 1: return f"{p:.4f}"
    return f"{p:.6f}"

print("Bot iniciado!")
send("🤖 <b>FuturesScan Bot iniciado!</b>\nA analisar mercado a cada hora ⏱\nAlertas de sinais LONG/SHORT fortes no Telegram!")

last = set()
while True:
    print("A analisar mercado...")
    try:
        signals = analyze()
        new = [s for s in signals if s[0] not in last]
        if new:
            msg = "📊 <b>SINAIS CRIPTO</b>\n\n"
            for sym,price,change,rsi_v,label,score,reasons in new:
                msg += f"{'↑' if 'LONG' in label else '↓'} <b>{sym}/USD</b> {label}\n"
                msg += f"   💲 ${fmt(price)}\n"
                msg += f"   📈 24h: {change:+.2f}%\n"
                msg += f"   📉 RSI: {rsi_v}\n"
                msg += f"   ⚡ Score: {score:+d}/7\n"
                msg += f"   📌 {', '.join(reasons)}\n\n"
            msg += "⚠️ <i>Não é aconselhamento financeiro.</i>"
            send(msg)
        else:
            print("Sem novos sinais fortes.")
        last = {s[0] for s in signals}
    except Exception as e:
        print(f"Erro geral: {e}")
    time.sleep(3600)
