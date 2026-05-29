import os, time, requests, io, hmac, hashlib, base64, json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ==================== CONFIG ====================
TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
BG_KEY = os.environ["BITGET_API_KEY"]
BG_SECRET = os.environ["BITGET_SECRET"]
BG_PASS = os.environ["BITGET_PASSPHRASE"]
API = f"https://api.telegram.org/bot{TOKEN}"
BG_API = "https://api.bitget.com"

MAX_LEV = 3
DRY_RUN = True   # <<< TESTE: True = simula sem dinheiro real. Mudar para False quando confirmado.

pending = {}
last_update_id = 0
last_analysis = 0

def send(msg):
    try:
        requests.post(f"{API}/sendMessage", json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"HTML"}, timeout=10)
    except Exception as e:
        print(f"Erro telegram: {e}")

def send_photo(buf, caption):
    try:
        buf.seek(0)
        requests.post(f"{API}/sendPhoto", data={"chat_id":CHAT_ID,"caption":caption,"parse_mode":"HTML"},
                      files={"photo":("chart.png",buf,"image/png")}, timeout=20)
    except Exception as e:
        print(f"Erro foto: {e}")
        send(caption)

def get_updates():
    global last_update_id
    try:
        r = requests.get(f"{API}/getUpdates", params={"offset":last_update_id+1,"timeout":10}, timeout=15)
        return r.json().get("result", [])
    except:
        return []

def fmt(p):
    if p > 100: return f"{p:,.2f}"
    if p > 1: return f"{p:.4f}"
    return f"{p:.6f}"

# ==================== BITGET ====================
def bg_sign(ts, method, path, body=""):
    msg = ts + method.upper() + path + body
    mac = hmac.new(BG_SECRET.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def bg_request(method, path, params=None):
    ts = str(int(time.time()*1000))
    if method == "GET" and params:
        query = "&".join(f"{k}={v}" for k,v in params.items())
        path = path + "?" + query
        body = ""
    else:
        body = json.dumps(params) if params else ""
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
        return {"code":"99999","msg":str(e)}

# ==================== PARES + PRECISAO ====================
ORIGINAL_PAIRS = [
    ("XBTUSD","BTC","BTCUSDT"),("ETHUSD","ETH","ETHUSDT"),
    ("SOLUSD","SOL","SOLUSDT"),("XRPUSD","XRP","XRPUSDT"),
    ("ADAUSD","ADA","ADAUSDT"),("DOTUSDT","DOT","DOTUSDT"),
    ("LINKUSD","LINK","LINKUSDT"),("UNIUSD","UNI","UNIUSDT"),
    ("ATOMUSD","ATOM","ATOMUSDT"),("LTCUSD","LTC","LTCUSDT"),
    ("XDGUSD","DOGE","DOGEUSDT"),("AAVEUSD","AAVE","AAVEUSDT")
]

def get_dynamic_pairs():
    try:
        url = f"{BG_API}/api/v2/mix/market/contracts?productType=USDT-FUTURES"
        data = requests.get(url, timeout=15).json()
        if data.get("code") != "00000" or not data.get("data"):
            raise Exception("resposta invalida")
        valid = {c.get("symbol") for c in data["data"]}
        pairs = [p for p in ORIGINAL_PAIRS if p[2] in valid]
        print(f"✅ {len(pairs)} pares validos na Bitget")
        return pairs if pairs else ORIGINAL_PAIRS
    except Exception as e:
        print(f"⚠️ Erro pares: {e} - usando fallback")
        return ORIGINAL_PAIRS

PAIRS = get_dynamic_pairs()

contract_precision = {}
def get_precision(bgsym):
    if bgsym in contract_precision:
        return contract_precision[bgsym]
    try:
        url = f"{BG_API}/api/v2/mix/market/contracts?productType=USDT-FUTURES&symbol={bgsym}"
        data = requests.get(url, timeout=10).json()
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
    size = round(s * m) / m
    size = max(size, 0.001)
    print(f"SIZE {bgsym}: notional=${notional:.2f} price=${price:.4f} size={size}")
    return size

def round_price(price, bgsym):
    _, pp = get_precision(bgsym)
    m = 10 ** pp
    return round(price * m) / m

# ==================== TRADING ====================
def check_open_position(symbol):
    resp = bg_request("GET", "/api/v2/mix/position/all-position",
                      {"productType":"USDT-FUTURES","marginCoin":"USDT"})
    if resp.get("code") != "00000":
        print(f"check_pos erro: {resp.get('msg')}")
        return False
    for pos in resp.get("data", []):
        if pos.get("symbol") == symbol and float(pos.get("total",0)) > 0:
            return True
    return False

def bg_set_leverage(symbol, lev):
    return bg_request("POST", "/api/v2/mix/account/set-leverage", {
        "symbol":symbol, "productType":"USDT-FUTURES", "marginCoin":"USDT", "leverage":str(lev)
    })

def bg_place_order(symbol, is_long, size, sl, tp):
    side = "buy" if is_long else "sell"
    return bg_request("POST", "/api/v2/mix/order/place-order", {
        "symbol":symbol, "productType":"USDT-FUTURES", "marginMode":"isolated",
        "marginCoin":"USDT", "size":str(size), "side":side, "orderType":"market",
        "presetStopSurplusPrice":str(tp), "presetStopLossPrice":str(sl)
    })

def calc_levels(price, signal, atr_val):
    if atr_val <= 0: atr_val = price * 0.01
    sl_m, tp1_m, tp2_m = 1.8, 2.8, 5.0
    if "LONG" in signal:
        return price - atr_val*sl_m, price + atr_val*tp1_m, price + atr_val*tp2_m, 3
    return price + atr_val*sl_m, price - atr_val*tp1_m, price - atr_val*tp2_m, 3

def executar_trade(sig, bgsym, margem):
    sym, price, _, rsi_v, label, score, reasons, atr_val = sig
    is_long = "LONG" in label
    sl, tp1, tp2, alav = calc_levels(price, label, atr_val)
    sl = round_price(sl, bgsym)
    tp1 = round_price(tp1, bgsym)
    lev = min(alav, MAX_LEV)
    notional = margem * lev
    size = calc_size(notional, price, bgsym)
    direcao = "LONG" if is_long else "SHORT"

    if DRY_RUN:
        m  = f"🔬 <b>DRY RUN (simulação)</b>\n━━━━━━━━━━━━━━━\n"
        m += f"{'↑' if is_long else '↓'} {sym}/USDT — {direcao}\n"
        m += f"💲 Entrada: ~${fmt(price)}\n⚡ {lev}x | 💰 ${margem}\n"
        m += f"📊 Size: {size}\n🛑 SL: ${fmt(sl)}\n🎯 TP1: ${fmt(tp1)}\n"
        m += f"━━━━━━━━━━━━━━━\n✅ Se fosse real, abria isto.\n"
        m += f"<i>(DRY_RUN ativo — nada foi executado)</i>"
        send(m)
        return

    if check_open_position(bgsym):
        send(f"⚠️ Já tens posição aberta em <b>{sym}</b>. Não abri outra.")
        return

    bg_set_leverage(bgsym, lev)
    r2 = bg_place_order(bgsym, is_long, size, sl, tp1)
    if r2.get("code") == "00000":
        m  = f"✅ <b>POSIÇÃO ABERTA!</b>\n━━━━━━━━━━━━━━━\n"
        m += f"{'↑' if is_long else '↓'} <b>{sym}/USDT — {direcao}</b>\n"
        m += f"💲 Entrada: ~${fmt(price)}\n⚡ {lev}x | 💰 ${margem}\n"
        m += f"📊 Size: {size}\n🛑 SL: ${fmt(sl)}\n🎯 TP1: ${fmt(tp1)}\n"
        m += f"━━━━━━━━━━━━━━━\n✅ SL e TP1 incluídos na ordem.\n"
        m += f"⚠️ CONFIRMA na Bitget que o SL aparece!"
        send(m)
    else:
        send(f"❌ Erro ao abrir: {r2.get('msg','?')} (cod {r2.get('code','?')})")

# ==================== GRAFICO ====================
def make_chart(ohlc, price, sl, tp1, tp2, sym, signal, ema20, ema50):
    try:
        candles = ohlc[-50:]
        opens=[float(c[1]) for c in candles]; highs=[float(c[2]) for c in candles]
        lows=[float(c[3]) for c in candles]; closes=[float(c[4]) for c in candles]
        xs=list(range(len(candles)))
        fig, ax = plt.subplots(figsize=(10,5))
        fig.patch.set_facecolor('#0d1318'); ax.set_facecolor('#0d1318')
        for i,x in enumerate(xs):
            o,h,l,c=opens[i],highs[i],lows[i],closes[i]
            col='#00e676' if c>=o else '#ff3d5a'
            ax.plot([x,x],[l,h],color=col,linewidth=0.8)
            ax.add_patch(plt.Rectangle((x-0.3,min(o,c)),0.6,abs(c-o),color=col,zorder=3))
        ax.plot(xs,ema20[-50:],color='#4a9eff',linewidth=1.2,label='EMA20')
        ax.plot(xs,ema50[-50:],color='#ffd166',linewidth=1.2,label='EMA50')
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
        buf=io.BytesIO(); plt.savefig(buf,format='png',dpi=120,facecolor='#0d1318'); plt.close()
        return buf
    except Exception as e:
        print(f"Erro grafico: {e}")
        return None

# ==================== INDICADORES ====================
def ema_arr(c, p):
    if len(c)<p: return [c[-1]]*len(c)
    k=2/(p+1); e=sum(c[:p])/p; r=list(c[:p])
    for x in c[p:]:
        e=x*k+e*(1-k); r.append(e)
    return r

def macd(c, fast=12, slow=26, sig=9):
    if len(c)<slow: return 0,0,0
    ef=ema_arr(c,fast); es=ema_arr(c,slow)
    ml=[f-s for f,s in zip(ef,es)]
    sl=ema_arr(ml,sig)
    return ml[-1], sl[-1], ml[-1]-sl[-1]

def atr(h, l, c, p=14):
    if len(h)<p+1: return 0.0
    trs=[max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1,len(h))]
    a=sum(trs[:p])/p
    for i in range(p,len(trs)):
        a=(a*(p-1)+trs[i])/p
    return a

def rsi(c, p=14):
    if len(c)<p+1: return 50.0
    g=[max(c[i]-c[i-1],0) for i in range(1,len(c))]
    l=[abs(min(c[i]-c[i-1],0)) for i in range(1,len(c))]
    ag=sum(g[:p])/p; al=sum(l[:p])/p
    for i in range(p,len(g)):
        ag=(ag*(p-1)+g[i])/p; al=(al*(p-1)+l[i])/p
    if al==0: return 100.0
    return round(100-(100/(1+ag/al)),1)

# ==================== ANALISE ====================
def analyze():
    global last_analysis
    last_analysis=time.time()
    signals=[]
    for pair,sym,bgsym in PAIRS:
        try:
            od=requests.get("https://api.kraken.com/0/public/OHLC",params={"pair":pair,"interval":60},timeout=15).json()
            ohlc=od["result"][list(od["result"].keys())[0]]
            closes=[float(c[4]) for c in ohlc]; highs=[float(c[2]) for c in ohlc]; lows=[float(c[3]) for c in ohlc]
            td=requests.get("https://api.kraken.com/0/public/Ticker",params={"pair":pair},timeout=10).json()
            t=td["result"][list(td["result"].keys())[0]]
            price=float(t["c"][0]); op=float(t["o"]); change=round((price-op)/op*100,2)
            r=rsi(closes); e20=ema_arr(closes,20); e50=ema_arr(closes,50)
            bull=e20[-1]>e50[-1]
            ml,sl_,hist=macd(closes); atr_val=atr(highs,lows,closes)
            score=0; reasons=[]
            if r<30: score+=3; reasons.append("RSI sobrevendido")
            elif r<40: score+=1; reasons.append("RSI baixo")
            elif r>70: score-=3; reasons.append("RSI sobrecomprado")
            elif r>60: score-=1; reasons.append("RSI alto")
            if bull: score+=2; reasons.append("EMA bullish")
            else: score-=2; reasons.append("EMA bearish")
            if price>e20[-1] and bull: score+=1
            elif price<e20[-1] and not bull: score-=1
            if ml>sl_ and hist>0: score+=2; reasons.append("MACD bullish")
            elif ml<sl_ and hist<0: score-=2; reasons.append("MACD bearish")
            if score>=4:
                signals.append(((sym,price,change,r,"🟢 LONG FORTE",score,reasons,atr_val),ohlc,e20,e50,bgsym))
            elif score<=-4:
                signals.append(((sym,price,change,r,"🔴 SHORT FORTE",score,reasons,atr_val),ohlc,e20,e50,bgsym))
            print(f"{sym}: RSI={r} MACD={hist:+.4f} score={score}")
            time.sleep(0.6)
        except Exception as e:
            print(f"Erro {sym}: {e}")
    return signals

def enviar_sinal(sig, ohlc, e20, e50, bgsym):
    sym,price,change,rsi_v,label,score,reasons,atr_val = sig
    sl,tp1,tp2,alav = calc_levels(price,label,atr_val)
    lev = min(alav, MAX_LEV)
    arrow = "↑" if "LONG" in label else "↓"
    modo = "🔬 DRY RUN" if DRY_RUN else "💵 REAL"
    cap  = f"{arrow} <b>{sym}/USD — {label}</b> ({modo})\n━━━━━━━━━━━━━━━\n"
    cap += f"💲 Entrada: ${fmt(price)}\n🛑 SL: ${fmt(sl)}\n🎯 TP1: ${fmt(tp1)}\n🎯 TP2: ${fmt(tp2)}\n"
    cap += f"⚡ Alavancagem: {lev}x\n━━━━━━━━━━━━━━━\n"
    cap += f"📉 RSI: {rsi_v} | Score: {score:+d}\n📌 {', '.join(reasons)}\n━━━━━━━━━━━━━━━\n"
    cap += f"💰 <b>ENTRAR?</b>\n✅ <b>sim VALOR</b> (ex: sim 5)\n❌ <b>não</b>\n"
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
        if CHAT_ID not in pending: continue
        if text in ("não","nao","n","no"):
            send("❌ Sinal cancelado.")
            pending.pop(CHAT_ID, None)
            continue
        if text.startswith("sim") or text.startswith("s "):
            partes = text.replace("sim","").strip().split()
            try:
                margem = float(partes[0].replace("$","").replace(",","."))
                sig, ohlc, e20, e50, bgsym = pending[CHAT_ID]
                send(f"⏳ A processar ${margem}...")
                executar_trade(sig, bgsym, margem)
                pending.pop(CHAT_ID, None)
            except Exception as e:
                send(f"⚠️ Formato errado. Ex: <b>sim 5</b>")

# ==================== LOOP ====================
modo = "🔬 DRY RUN (teste)" if DRY_RUN else "💵 REAL"
print(f"Bot iniciado! Modo: {modo}")
send(f"🤖 <b>FuturesScan Bot</b>\nModo: <b>{modo}</b>\n⚡ Máx {MAX_LEV}x | 🛡️ SL+TP automático\nQuando houver sinal, pergunto se entras.")

while True:
    process_replies()
    if time.time()-last_analysis >= 3600:
        print("A analisar mercado...")
        try:
            signals = analyze()
            for item in signals:
                enviar_sinal(*item)
                break
            if not signals: print("Sem sinais fortes.")
        except Exception as e:
            print(f"Erro geral: {e}")
    time.sleep(3)
