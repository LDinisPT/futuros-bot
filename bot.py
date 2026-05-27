import os, time, requests, threading

TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
API = f"https://api.telegram.org/bot{TOKEN}"

PAIRS = [
    ("XBTUSD","BTC"),("ETHUSD","ETH"),("SOLUSD","SOL"),
    ("XRPUSD","XRP"),("ADAUSD","ADA"),("DOTUSD","DOT"),
    ("LINKUSD","LINK"),("UNIUSD","UNI"),("ATOMUSD","ATOM"),
    ("LTCUSD","LTC"),("AVAXUSD","AVAX"),("NEARUSD","NEAR"),
    ("AAVEUSD","AAVE"),("DOGEUSD","DOGE")
]

pending = {}  # guarda sinais à espera de resposta
last_update_id = 0

def send(msg, chat_id=None):
    try:
        requests.post(f"{API}/sendMessage",
            json={"chat_id": chat_id or CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"Erro telegram: {e}")

def get_updates():
    global last_update_id
    try:
        r = requests.get(f"{API}/getUpdates",
            params={"offset": last_update_id+1, "timeout": 10}, timeout=15)
        data = r.json()
        return data.get("result", [])
    except:
        return []

def process_replies():
    global last_update_id
    updates = get_updates()
    for u in updates:
        last_update_id = u["update_id"]
        msg = u.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id == CHAT_ID and text and chat_id in pending:
            try:
                saldo = float(text.replace("$","").replace(",","."))
                sig = pending.pop(chat_id)
                send_full_signal(sig, saldo)
            except:
                send("⚠️ Valor inválido. Envia apenas o número. Ex: 500", chat_id)

def fmt(p):
    if p > 100: return f"{p:,.2f}"
    if p > 1: return f"{p:.4f}"
    return f"{p:.6f}"

def calc_levels(price, signal, rsi_val):
    if rsi_val < 30 or rsi_val > 70:
        sl_pct, tp1_pct, tp2_pct = 0.03, 0.05, 0.10
        alavancagem = 2
    else:
        sl_pct, tp1_pct, tp2_pct = 0.025, 0.04, 0.08
        alavancagem = 3

    if "LONG" in signal:
        sl  = price * (1 - sl_pct)
        tp1 = price * (1 + tp1_pct)
        tp2 = price * (1 + tp2_pct)
    else:
        sl  = price * (1 + sl_pct)
        tp1 = price * (1 - tp1_pct)
        tp2 = price * (1 - tp2_pct)

    return sl, tp1, tp2, alavancagem, sl_pct

def send_full_signal(sig, saldo):
    sym, price, change, rsi_v, label, score, reasons = sig
    sl, tp1, tp2, alav, sl_pct = calc_levels(price, label, rsi_v)

    risco_pct = 0.02  # 2% fixo
    risco_usd = saldo * risco_pct
    tamanho = round(risco_usd / sl_pct, 2)
    tamanho = min(tamanho, saldo * alav)  # nunca passa a alavancagem máxima

    lucro_tp1 = round(tamanho * (tp1/price - 1) * (1 if "LONG" in label else -1), 2)
    lucro_tp2 = round(tamanho * (tp2/price - 1) * (1 if "LONG" in label else -1), 2)
    perda_sl  = round(risco_usd, 2)

    arrow = "↑" if "LONG" in label else "↓"
    direction = "LONG 📈" if "LONG" in label else "SHORT 📉"

    msg  = f"{arrow} <b>{sym}/USD — {label}</b>\n"
    msg += f"━━━━━━━━━━━━━━━\n"
    msg += f"💲 <b>Entrada:</b> ${fmt(price)}\n"
    msg += f"🛑 <b>Stop Loss:</b> ${fmt(sl)}\n"
    msg += f"🎯 <b>TP1:</b> ${fmt(tp1)}\n"
    msg += f"🎯 <b>TP2:</b> ${fmt(tp2)}\n"
    msg += f"━━━━━━━━━━━━━━━\n"
    msg += f"💼 <b>GESTÃO DE RISCO</b>\n"
    msg += f"💰 Saldo: ${saldo:,.0f}\n"
    msg += f"⚡ Alavancagem sugerida: {alav}x\n"
    msg += f"📊 Tamanho da posição: ${tamanho:,.0f}\n"
    msg += f"━━━━━━━━━━━━━━━\n"
    msg += f"❌ Se correr mal: −${perda_sl}\n"
    msg += f"✅ Se bater TP1: +${lucro_tp1}\n"
    msg += f"✅ Se bater TP2: +${lucro_tp2}\n"
    msg += f"━━━━━━━━━━━━━━━\n"
    msg += f"📉 RSI: {rsi_v} | ⚡ Score: {score:+d}/7\n"
    msg += f"📌 {', '.join(reasons)}\n"
    msg += f"━━━━━━━━━━━━━━━\n"
    msg += f"⚠️ <i>Verifica o gráfico antes de entrar.\nNão é aconselhamento financeiro.</i>"
    send(msg)

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

print("Bot iniciado!")
send("🤖 <b>FuturesScan Bot iniciado!</b>\nA analisar mercado a cada hora ⏱\nQuando houver sinal pergunto o teu saldo e calculo tudo!")

last = set()
while True:
    process_replies()
    
    if not hasattr(analyze, '_last_run') or time.time() - analyze._last_run >= 3600:
        analyze._last_run = time.time()
        print("A analisar mercado...")
        try:
            signals = analyze()
            new = [s for s in signals if s[0] not in last]
            if new:
                for sig in new:
                    sym = sig[0]
                    label = sig[4]
                    arrow = "↑" if "LONG" in label else "↓"
                    pending[CHAT_ID] = sig
                    send(f"🔔 <b>Sinal detetado!</b>\n\n{arrow} <b>{sym}/USD — {label}</b>\n\n💰 Qual é o teu saldo atual?\n<i>Responde apenas com o número. Ex: 500</i>")
            else:
                print("Sem novos sinais fortes.")
            last = {s[0] for s in signals}
        except Exception as e:
            print(f"Erro geral: {e}")
    
    time.sleep(3)
