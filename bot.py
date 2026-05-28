import os, time, requests, io, hmac, hashlib, base64, json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
BG_KEY = os.environ["BITGET_API_KEY"]
BG_SECRET = os.environ["BITGET_SECRET"]
BG_PASS = os.environ["BITGET_PASSPHRASE"]
API = f"https://api.telegram.org/bot{TOKEN}"
BG_API = "https://api.bitget.com"

MAX_LEV = 3  # SEGURANCA: alavancagem maxima

# (par_kraken, simbolo, simbolo_bitget)
PAIRS = [
    ("XBTUSD","BTC","BTCUSDT"),("ETHUSD","ETH","ETHUSDT"),
    ("SOLUSD","SOL","SOLUSDT"),("XRPUSD","XRP","XRPUSDT"),
    ("ADAUSD","ADA","ADAUSDT"),("DOTUSDT","DOT","DOTUSDT"),
    ("LINKUSD","LINK","LINKUSDT"),("UNIUSD","UNI","UNIUSDT"),
    ("ATOMUSD","ATOM","ATOMUSDT"),("LTCUSD","LTC","LTCUSDT"),
    ("XDGUSD","DOGE","DOGEUSDT"),("AAVEUSD","AAVE","AAVEUSDT")
]

pending = {}
last_update_id = 0
last_analysis = 0

def send(msg):
    try:
        requests.post(f"{API}/sendMessage",
            json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"HTML"},timeout=10)
    except Exception as e:
        print(f"Erro telegram: {e}")

def send_photo(buf, caption):
    try:
        buf.seek(0)
        requests.post(f"{API}/sendPhoto",
            data={"chat_id":CHAT_ID,"caption":caption,"parse_mode":"HTML"},
            files={"photo":("chart.png",buf,"image/png")},timeout=20)
    except Exception as e:
        print(f"Erro foto: {e}")
        send(caption)

def get_updates():
    global last_update_id
    try:
        r = requests.get(f"{API}/getUpdates",
            params={"offset":last_update_id+1,"timeout":10},timeout=15)
        return r.json().get("result",[])
    except:
        return []

def fmt(p):
    if p>100: return f"{p:,.2f}"
    if p>1: return f"{p:.4f}"
    return f"{p:.6f}"

# ---------- BITGET ----------
def bg_sign(ts, method, path, body=""):
    msg = ts + method.upper() + path + body
    mac = hmac.new(BG_SECRET.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def bg_request(method, path, body_dict=None):
    ts = str(int(time.time()*1000))
    body = json.dumps(body_dict) if body_dict else ""
    sign = bg_sign(ts, method, path, body)
    headers = {
        "ACCESS-KEY": BG_KEY,
        "ACCESS-SIGN": sign,
        "ACCESS-PASSPHRASE": BG_PASS,
        "ACCESS-TIMESTAMP": ts,
        "locale": "en-US",
        "Content-Type": "application/json"
    }
    url = BG_API + path
    if method == "POST":
        r = requests.post(url, headers=headers, data=body, timeout=15)
    else:
        r = requests.get(url, headers=headers, timeout=15)
    return r.json()

def bg_set_leverage(symbol, lev):
    return bg_request("POST", "/api/v2/mix/account/set-leverage", {
        "symbol": symbol,
        "productType": "USDT-FUTURES",
        "marginCoin": "USDT",
        "leverage": str(lev)
    })

def calc_size(notional, price):
    s = notional / price
    if price > 1000: return round(s, 4)
    if price > 100: return round(s, 3)
    if price > 10: return round(s, 2)
    if price > 1: return round(s, 1)
    return round(s)

def bg_place_order(symbol, is_long, size, sl, tp):
    side = "buy" if is_long else "sell"
    return bg_request("POST", "/api/v2/mix/order/place-order", {
        "symbol": symbol,
        "productType": "USDT-FUTURES",
        "marginMode": "isolated",
        "marginCoin": "USDT",
        "size": str(size),
        "side": side,
        "orderType": "market",
        "presetStopSurplusPrice": str(round(tp,4)),
        "presetStopLossPrice": str(round(sl,4))
    })

def executar_trade(sig, bgsym, margem):
    sym,price,change,rsi_v,label,score,reasons = sig
    is_long = "LONG" in label
    sl,tp1,tp2,alav,sl_pct = calc_levels(price,label,rsi_v)
    lev = min(alav, MAX_LEV)
    notional = margem * lev
    size = calc_size(notional, price)
    # 1. definir alavancagem
    r1 = bg_set_leverage(bgsym, lev)
    if r1.get("code") not in ("00000", None):
        send(f"⚠️ Erro alavancagem: {r1.get('msg','?')}")
    # 2. abrir posicao com SL + TP1
    r2 = bg_place_order(bgsym, is_long, size, sl, tp1)
    if r2.get("code") == "00000":
        arrow = "↑" if is_long else "↓"
        m  = f"✅ <b>POSIÇÃO ABERTA!</b>\n"
        m += f"━━━━━━━━━━━━━━━\n"
        m += f"{arrow} <b>{sym}/USDT — {'LONG' if is_long else 'SHORT'}</b>\n"
        m += f"💲 Entrada: ~${fmt(price)}\n"
        m += f"⚡ Alavancagem: {lev}x\n"
        m += f"💰 Margem: ${margem}\n"
        m += f"📊 Tamanho: {size}\n"
        m += f"🛑 SL: ${fmt(sl)}\n"
        m += f"🎯 TP1: ${fmt(tp1)}\n"
        m += f"━━━━━━━━━━━━━━━\n"
        m += f"⚠️ TP2 (${fmt(tp2)}) coloca manualmente se quiseres\n"
        m += f"✅ Confirma na Bitget!"
        send(m)
    else:
        send(f"❌ Erro ao abrir: {r2.get('msg','?')}\nCódigo: {r2.get('code','?')}")

def calc_levels(price, signal, rsi_val):
    if rsi_val < 30 or rsi_val > 70:
        sl_pct,tp1_pct,tp2_pct = 0.03,0.05,0.10
        alav = 2
    else:
        sl_pct,tp1_pct,tp2_pct = 0.025,0.04,0.08
        alav = 3
    if "LONG" in signal:
        sl=price*(1-sl_pct); tp1=price*(1+tp1_pct); tp2=price*(1+tp2_pct)
    else:
        sl=price*(1+sl_pct); tp1=price*(1-tp1_pct); tp2=price*(1-tp2_pct)
    return sl,tp1,tp2,alav,sl_pct

def make_chart(ohlc, price, sl, tp1, tp2, sym, signal, ema20, ema50):
    try:
        candles = ohlc[-50:]
        opens=[float(c[1]) for c in candles]
        highs=[float(c[2]) for c in candles]
        lows=[float(c[3]) for c in candles]
        closes=[float(c[4]) for c in candles]
        xs=list(range(len(candles)))
        fig, ax = plt.subplots(figsize=(10,5))
        fig.patch.set_facecolor('#0d1318')
        ax.set_facecolor('#0d1318')
        for i,x in enumerate(xs):
            o,h,l,c=opens[i],highs[i],lows[i],closes[i]
            color='#00e676' if c>=o else '#ff3d5a'
            ax.plot([x,x],[l,h],color=color,linewidth=0.8)
            ax.add_patch(plt.Rectangle((x-0.3,min(o,c)),0.6,abs(c-o),color=color,zorder=3))
        ax.plot(xs,ema20[-50:],color='#4a9eff',linewidth=1.2,label='EMA20',zorder=4)
        ax.plot(xs,ema50[-50:],color='#ffd166',linewidth=1.2,label='EMA50',zorder=4)
        ax.axhline(price,color='#ffffff',linewidth=1.2,linestyle='--',label=f'Entrada ${fmt(price)}')
        ax.axhline(sl,color='#ff3d5a',linewidth=1.2,linestyle='--',label=f'SL ${fmt(sl)}')
        ax.axhline(tp1,color='#00e676',linewidth=1.0,linestyle=':',label=f'TP1 ${fmt(tp1)}')
        ax.axhline(tp2,color='#00e676',linewidth=1.2,linestyle='--',label=f'TP2 ${fmt(tp2)}')
        ax.tick_params(colors='#4a6070',labelsize=7)
        for sp in ax.spines.values(): sp.set_color('#1a2430')
        ax.yaxis.set_tick_params(labelcolor='#c8d8e8')
        ax.xaxis.set_tick_params(labelbottom=False)
        ax.grid(color='#1a2430',linewidth=0.5,alpha=0.5)
        d="LONG" if "LONG" in signal else "SHORT"
        ax.set_title(f'{sym}/USD - {d} | 50 velas (1h)',color='#c8d8e8',fontsize=10,pad=10)
        ax.legend(loc='upper left',fontsize=7,facecolor='#0d1318',edgecolor='#1a2430',labelcolor='#c8d8e8')
        plt.tight_layout()
        buf=io.BytesIO()
        plt.savefig(buf,format='png',dpi=120,facecolor='#0d1318')
        plt.close()
        return buf
    except Exception as e:
        print(f"Erro grafico: {e}")
        return None

def enviar_sinal(sig, ohlc, e20, e50, bgsym):
    sym,price,change,rsi_v,label,score,reasons = sig
    sl,tp1,tp2,alav,sl_pct = calc_levels(price,label,rsi_v)
    lev = min(alav, MAX_LEV)
    arrow = "↑" if "LONG" in label else "↓"
    cap  = f"{arrow} <b>{sym}/USD — {label}</b>\n"
    cap += "━━━━━━━━━━━━━━━\n"
    cap += f"💲 Entrada: ${fmt(price)}\n"
    cap += f"🛑 SL: ${fmt(sl)}\n"
    cap += f"🎯 TP1: ${fmt(tp1)}\n"
    cap += f"🎯 TP2: ${fmt(tp2)}\n"
    cap += f"⚡ Alavancagem: {lev}x\n"
    cap += "━━━━━━━━━━━━━━━\n"
    cap += f"📉 RSI: {rsi_v} | Score: {score:+d}/7\n"
    cap += f"📌 {', '.join(reasons)}\n"
    cap += "━━━━━━━━━━━━━━━\n"
    cap += f"💰 <b>ENTRAR?</b>\n"
    cap += f"✅ Responde: <b>sim VALOR</b> (ex: sim 50)\n"
    cap += f"❌ Ou: <b>não</b>\n"
    cap += "⚠️ <i>Não é aconselhamento financeiro.</i>"
    pending[CHAT_ID] = (sig, ohlc, e20, e50, bgsym)
    buf = make_chart(ohlc, price, sl, tp1, tp2, sym, label, e20, e50)
    if buf: send_photo(buf, cap)
    else: send(cap)

def process_replies():
    global last_update_id
    for u in get_updates():
        last_update_id = u["update_id"]
        text = u.get("message",{}).get("text","").strip().lower()
        if not text or text.startswith("/"): continue
        if CHAT_ID not in pending:
            continue
        if text in ("não","nao","n","no"):
            send("❌ Sinal cancelado. Aguardo o próximo!")
            pending.pop(CHAT_ID, None)
            continue
        if text.startswith("sim") or text.startswith("s "):
            partes = text.replace("sim","").replace("s","").strip().split()
            try:
                margem = float(partes[0].replace("$","").replace(",","."))
                sig, ohlc, e20, e50, bgsym = pending[CHAT_ID]
                send(f"⏳ A abrir posição com ${margem}...")
                executar_trade(sig, bgsym, margem)
                pending.pop(CHAT_ID, None)
            except Exception as e:
                send(f"⚠️ Formato errado. Ex: <b>sim 50</b>\n({e})")

def rsi(c, p=14):
    if len(c)<p+1: return 50
    g=l=0
    for i in range(len(c)-p,len(c)):
        d=c[i]-c[i-1]
        if d>0: g+=d
        else: l-=d
    ag,al=g/p,l/p
    if al==0: return 100
    return round(100-(100/(1+ag/al)),1)

def ema_arr(c, p):
    if len(c)<p: return [c[-1]]*len(c)
    k=2/(p+1)
    e=sum(c[:p])/p
    r=list(c[:p])
    for x in c[p:]:
        e=x*k+e*(1-k)
        r.append(e)
    return r

def get_ohlc(pair):
    r=requests.get("https://api.kraken.com/0/public/OHLC",
        params={"pair":pair,"interval":60},timeout=15)
    d=r.json()
    if d["error"]: raise Exception(str(d["error"]))
    return d["result"][list(d["result"].keys())[0]]

def get_ticker(pair):
    r=requests.get("https://api.kraken.com/0/public/Ticker",
        params={"pair":pair},timeout=10)
    d=r.json()
    if d["error"]: raise Exception(str(d["error"]))
    return d["result"][list(d["result"].keys())[0]]

def analyze():
    global last_analysis
    last_analysis=time.time()
    signals=[]
    for pair,sym,bgsym in PAIRS:
        try:
            ohlc=get_ohlc(pair)
            closes=[float(c[4]) for c in ohlc]
            t=get_ticker(pair)
            price=float(t["c"][0])
            op=float(t["o"])
            change=round((price-op)/op*100,2)
            r=rsi(closes)
            e20=ema_arr(closes,20)
            e50=ema_arr(closes,50)
            bull=e20[-1]>e50[-1]
            score=0; reasons=[]
            if r<30: score+=3; reasons.append("RSI sobrevendido")
            elif r<40: score+=1; reasons.append("RSI baixo")
            elif r>70: score-=3; reasons.append("RSI sobrecomprado")
            elif r>60: score-=1; reasons.append("RSI alto")
            if bull: score+=2; reasons.append("EMA bullish")
            else: score-=2; reasons.append("EMA bearish")
            if price>e20[-1] and bull: score+=1
            elif price<e20[-1] and not bull: score-=1
            if change>5: score+=1; reasons.append("Momentum forte")
            elif change<-5: score-=1; reasons.append("Queda forte")
            if score>=4:
                signals.append(((sym,price,change,r,"🟢 LONG FORTE",score,reasons),ohlc,e20,e50,bgsym))
            elif score<=-4:
                signals.append(((sym,price,change,r,"🔴 SHORT FORTE",score,reasons),ohlc,e20,e50,bgsym))
            print(f"{sym}: RSI={r} score={score}")
            time.sleep(1)
        except Exception as e:
            print(f"Erro {sym}: {e}")
    return signals

print("Bot iniciado!")
send("🤖 <b>FuturesScan Bot + Auto-Trade!</b>\nAnaliso o mercado a cada hora ⏱\nQuando houver sinal, pergunto se queres entrar.\n⚡ Máximo 3x | 🛡️ SL automático")

last_syms=set()
while True:
    process_replies()
    if time.time()-last_analysis>=3600:
        print("A analisar mercado...")
        try:
            signals=analyze()
            new=[s for s in signals if s[0][0] not in last_syms]
            for item in new:
                sig,ohlc,e20,e50,bgsym=item
                enviar_sinal(sig,ohlc,e20,e50,bgsym)
                break  # so 1 sinal de cada vez para nao confundir
            if not new: print("Sem novos sinais.")
            last_syms={s[0][0] for s in signals}
        except Exception as e:
            print(f"Erro geral: {e}")
    time.sleep(3)
