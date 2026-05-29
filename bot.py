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
CALLBACK_RATIO = 2.5
DRY_RUN = False
VERSAO = "v5"
DAILY_LOSS_WARNING = 5.0   # avisa se já perdeu mais que isto hoje
MAX_NOTIONAL = 500.0       # limite de posição em $ (segurança)

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

def gen_oid():
    return f"bot{int(time.time()*1000)}{os.urandom(3).hex()}"

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
    return max(size, 0.001)

def round_size(size, bgsym):
    vp, _ = get_precision(bgsym)
    m = 10 ** vp
    return max(round(size * m) / m, 0.001)

def round_price(price, bgsym):
    _, pp = get_precision(bgsym)
    m = 10 ** pp
    return round(price * m) / m

# ==================== TRADING ====================
def check_open_position(symbol):
    resp = bg_request("GET", "/api/v2/mix/position/all-position",
                      {"productType":"USDT-FUTURES","marginCoin":"USDT"})
    if resp.get("code") != "00000":
        return False
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

def bg_close_limit(symbol, is_long, size, price):
    side = "sell" if is_long else "buy"
    return bg_request("POST", "/api/v2/mix/order/place-order", {
        "symbol":symbol, "productType":"USDT-FUTURES", "marginMode":"isolated",
        "marginCoin":"USDT", "size":str(size), "side":side, "orderType":"limit",
        "price":str(price), "reduceOnly":"YES", "clientOid": gen_oid()
    })

def bg_place_trailing(symbol, is_long, size, trigger, callback):
    side = "sell" if is_long else "buy"
    return bg_request("POST", "/api/v2/mix/order/place-plan-order", {
        "planType":"track_plan", "symbol":symbol, "productType":"USDT-FUTURES",
        "marginMode":"isolated", "marginCoin":"USDT", "size":str(size),
        "callbackRatio":str(callback), "triggerPrice":str(trigger),
        "triggerType":"mark_price", "side":side, "reduceOnly":"YES",
        "orderType":"market", "clientOid": gen_oid()
    })

def bg_close_position(symbol):
    return bg_request("POST", "/api/v2/mix/order/close-positions", {
        "symbol": symbol, "productType": "USDT-FUTURES"
    })

def bg_cancel_all(symbol):
    return bg_request("POST", "/api/v2/mix/order/cancel-all-orders", {
        "symbol": symbol, "productType": "USDT-FUTURES", "marginCoin": "USDT"
    })

def calc_levels(price, signal, atr_val):
    if atr_val <= 0: atr_val = price * 0.01
    sl_m, tp1_m, tp2_m = 1.8, 2.8, 5.0
    if "LONG" in signal:
        return price - atr_val*sl_m, price + atr_val*tp1_m, price + atr_val*tp2_m, 3
    return price + atr_val*sl_m, price - atr_val*tp1_m, price - atr_val*tp2_m, 3

def perda_hoje():
    resp = bg_request("GET", "/api/v2/mix/position/history-position", {"productType":"USDT-FUTURES","limit":"50"})
    if resp.get("code") != "00000": return 0
    d = resp.get("data")
    lista = d.get("list",[]) if isinstance(d, dict) else (d or [])
    hoje = time.strftime("%Y-%m-%d", time.gmtime())
    total = 0
    for p in lista:
        utime = int(p.get("utime") or 0)
        if utime == 0: continue
        data_fecho = time.strftime("%Y-%m-%d", time.gmtime(utime/1000))
        if data_fecho == hoje:
            pnl = float(p.get("netProfit") or p.get("pnl") or 0)
            total += pnl
    return total

def executar_trade(sig, bgsym, valor, modo="normal", tipo_valor="margem"):
    sym, price, _, rsi_v, label, score, reasons, atr_val = sig
    is_long = "LONG" in label
    sl, tp1, tp2, alav = calc_levels(price, label, atr_val)
    sl = round_price(sl, bgsym); tp1 = round_price(tp1, bgsym)
    lev = min(alav, MAX_LEV)

    # SIZING
    if tipo_valor == "risco":
        distancia = abs(sl - price)
        if distancia <= 0:
            send("⚠️ Distância SL inválida."); return
        size_raw = valor / distancia
        notional = size_raw * price
        if notional > MAX_NOTIONAL:
            send(f"⚠️ Posição ficaria muito grande (${notional:.0f}). Reduz o risco ou usa modo margem.")
            return
        size = round_size(size_raw, bgsym)
        margem = notional / lev
        risco_real = valor
    else:
        margem = valor
        notional = margem * lev
        if notional > MAX_NOTIONAL:
            send(f"⚠️ Posição muito grande (${notional:.0f}). Reduz o valor.")
            return
        size = calc_size(notional, price, bgsym)
        distancia = abs(sl - price)
        risco_real = size * distancia

    direcao = "LONG" if is_long else "SHORT"
    arrow = "↑" if is_long else "↓"

    if DRY_RUN:
        m  = f"🔬 <b>DRY RUN — modo {modo.upper()}</b>\n━━━━━━━━━━━━━━━\n"
        m += f"{arrow} {sym}/USDT — {direcao}\n💲 Entrada: ~${fmt(price)}\n"
        m += f"⚡ {lev}x | 💰 Margem ${margem:.2f}\n📊 Size: {size}\n"
        m += f"🛑 SL: ${fmt(sl)} (risco real ${risco_real:.2f})\n🎯 TP1: ${fmt(tp1)}\n"
        if modo == "hibrido":
            metade = round_size(size/2, bgsym)
            m += f"📋 TP1 50% ({metade}) + trailing {CALLBACK_RATIO}%\n"
        elif modo == "trail":
            m += f"📋 Trailing {CALLBACK_RATIO}% 100%\n"
        else:
            m += "📋 TP1 100%\n"
        m += "<i>(DRY_RUN — nada executado)</i>"
        send(m); return

    if check_open_position(bgsym):
        send(f"⚠️ Já tens posição em <b>{sym}</b>."); return

    bg_set_leverage(bgsym, lev)

    if modo == "normal":
        r = bg_place_order(bgsym, is_long, size, sl, tp1)
    else:
        r = bg_place_order(bgsym, is_long, size, sl)

    if r.get("code") != "00000":
        send(f"❌ Erro ao abrir: {r.get('msg','?')} (cod {r.get('code','?')})")
        return

    extra = "TP1 fecha 100%"
    avisos = []
    if modo == "trail":
        rt = bg_place_trailing(bgsym, is_long, size, tp1, CALLBACK_RATIO)
        if rt.get("code") == "00000":
            extra = f"Trailing {CALLBACK_RATIO}% (100%)"
        else:
            avisos.append(f"⚠️ Trailing falhou: {rt.get('msg','?')}")
            extra = "SL fixo (trailing falhou)"
    elif modo == "hibrido":
        metade = round_size(size/2, bgsym)
        resto = round_size(size - metade, bgsym)
        rtp = bg_close_limit(bgsym, is_long, metade, tp1)
        rtr = bg_place_trailing(bgsym, is_long, resto, tp1, CALLBACK_RATIO)
        ok_tp = rtp.get("code") == "00000"
        ok_tr = rtr.get("code") == "00000"
        extra = f"TP1 50% [{'ok' if ok_tp else 'FALHOU'}] + Trailing [{'ok' if ok_tr else 'FALHOU'}]"
        if not ok_tp: avisos.append(f"⚠️ TP1: {rtp.get('msg','?')}")
        if not ok_tr: avisos.append(f"⚠️ Trailing: {rtr.get('msg','?')}")

    m  = f"✅ <b>POSIÇÃO ABERTA!</b> ({modo})\n━━━━━━━━━━━━━━━\n"
    m += f"{arrow} <b>{sym}/USDT — {direcao}</b>\n💲 Entrada: ~${fmt(price)}\n"
    m += f"⚡ {lev}x | 💰 Margem ${margem:.2f}\n📊 Size: {size}\n"
    m += f"🛑 SL: ${fmt(sl)} | 🎯 TP1: ${fmt(tp1)}\n"
    m += f"📉 Risco real no SL: <b>${risco_real:.2f}</b>\n"
    m += f"📋 {extra}\n━━━━━━━━━━━━━━━\n"
    if avisos: m += "\n".join(avisos) + "\n"
    m += "⚠️ CONFIRMA na Bitget!"
    send(m)

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
        ax.set_title(f'{sym}/USD - {d} | 50 velas (1h) {VERSAO}',color='#c8d8e8',fontsize=10,pad=10)
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
            volumes=[float(c[6]) for c in ohlc]
            td=requests.get("https://api.kraken.com/0/public/Ticker",params={"pair":pair},timeout=10).json()
            t=td["result"][list(td["result"].keys())[0]]
            price=float(t["c"][0]); op=float(t["o"]); change=round((price-op)/op*100,2)
            r=rsi(closes); e20=ema_arr(closes,20); e50=ema_arr(closes,50)
            bull=e20[-1]>e50[-1]
            ml,sl_,hist=macd(closes); atr_val=atr(highs,lows,closes)
            # filtro de volume
            vol_recente = sum(volumes[-5:]) / 5
            vol_medio = sum(volumes[-25:-5]) / 20 if len(volumes)>=25 else vol_recente
            vol_ratio = vol_recente / vol_medio if vol_medio else 1
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
            # volume
            if vol_ratio > 1.3:
                if score > 0: score+=1; reasons.append("Volume↑")
                elif score < 0: score-=1; reasons.append("Volume↑")
            elif vol_ratio < 0.6:
                reasons.append("Volume baixo")
                score = int(score * 0.7)  # reduz força do sinal
            if score>=4:
                signals.append(((sym,price,change,r,"🟢 LONG FORTE",score,reasons,atr_val),ohlc,e20,e50,bgsym))
            elif score<=-4:
                signals.append(((sym,price,change,r,"🔴 SHORT FORTE",score,reasons,atr_val),ohlc,e20,e50,bgsym))
            print(f"{sym}: RSI={r} MACD={hist:+.4f} VOL={vol_ratio:.2f} score={score}")
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
    # exemplos de risco
    dist_pct = abs(sl-price)/price*100
    risco_5m = 5 * lev * dist_pct/100  # se margem $5
    cap  = f"{arrow} <b>{sym}/USD — {label}</b> ({modo})\n━━━━━━━━━━━━━━━\n"
    cap += f"💲 Entrada: ${fmt(price)}\n🛑 SL: ${fmt(sl)} ({dist_pct:.2f}%)\n🎯 TP1: ${fmt(tp1)}\n🎯 TP2: ${fmt(tp2)}\n"
    cap += f"⚡ Alavancagem: {lev}x\n━━━━━━━━━━━━━━━\n"
    cap += f"📉 RSI: {rsi_v} | Score: {score:+d}\n📌 {', '.join(reasons)}\n━━━━━━━━━━━━━━━\n"
    cap += f"💰 <b>ENTRAR?</b> (margem ou risco)\n"
    cap += f"✅ <b>sim 5</b> → $5 margem (risco ~${risco_5m:.2f})\n"
    cap += f"✅ <b>sim r1</b> → arrisca $1 no SL (sizing inteligente)\n"
    cap += f"➕ adicionar <b>trail</b> ou <b>hibrido</b>\n"
    cap += f"❌ <b>não</b>"
    pending[CHAT_ID] = (sig, ohlc, e20, e50, bgsym)
    buf = make_chart(ohlc, price, sl, tp1, tp2, sym, label, e20, e50)
    if buf: send_photo(buf, cap)
    else: send(cap)

def forcar_teste(symbol):
    symbol = symbol.upper()
    alvo = None
    for pair,sym,bgsym in PAIRS:
        if sym == symbol:
            alvo = (pair,sym,bgsym); break
    if not alvo:
        nomes = ", ".join(p[1] for p in PAIRS)
        send(f"⚠️ Par '{symbol}' não existe.\nDisponíveis: {nomes}\nEx: /teste LTC")
        return
    pair,sym,bgsym = alvo
    try:
        od=requests.get("https://api.kraken.com/0/public/OHLC",params={"pair":pair,"interval":60},timeout=15).json()
        ohlc=od["result"][list(od["result"].keys())[0]]
        closes=[float(c[4]) for c in ohlc]; highs=[float(c[2]) for c in ohlc]; lows=[float(c[3]) for c in ohlc]
        td=requests.get("https://api.kraken.com/0/public/Ticker",params={"pair":pair},timeout=10).json()
        t=td["result"][list(td["result"].keys())[0]]
        price=float(t["c"][0]); op=float(t["o"]); change=round((price-op)/op*100,2)
        r=rsi(closes); e20=ema_arr(closes,20); e50=ema_arr(closes,50)
        bull=e20[-1]>e50[-1]; atr_val=atr(highs,lows,closes)
        label = "🟢 LONG FORTE" if bull else "🔴 SHORT FORTE"
        sig=(sym,price,change,r,label,0,["TESTE MANUAL"],atr_val)
        send(f"🧪 <b>Teste forçado: {sym}</b>")
        enviar_sinal(sig,ohlc,e20,e50,bgsym)
    except Exception as e:
        send(f"⚠️ Erro: {e}")

def mostrar_saldo():
    resp = bg_request("GET", "/api/v2/mix/account/accounts", {"productType":"USDT-FUTURES"})
    if resp.get("code") != "00000":
        send(f"⚠️ Erro: {resp.get('msg','?')}"); return
    data = resp.get("data", [])
    if not data: send("📭 Sem dados."); return
    a = data[0]
    equity = float(a.get("accountEquity", a.get("usdtEquity",0)))
    avail = float(a.get("available", 0))
    upl = float(a.get("unrealizedPL", 0))
    perda = perda_hoje()
    m  = f"💰 <b>SALDO BITGET</b>\n━━━━━━━━━━━━━━━\n"
    m += f"💵 Total: ${equity:.2f}\n✅ Disponível: ${avail:.2f}\n"
    m += f"📊 L/P aberto: ${upl:+.2f}\n📅 L/P hoje: ${perda:+.2f}"
    send(m)

def mostrar_posicoes():
    resp = bg_request("GET", "/api/v2/mix/position/all-position", {"productType":"USDT-FUTURES","marginCoin":"USDT"})
    if resp.get("code") != "00000":
        send(f"⚠️ Erro: {resp.get('msg','?')}"); return
    pos = [p for p in resp.get("data", []) if float(p.get("total",0)) > 0]
    if not pos: send("📭 Sem posições abertas."); return
    m = f"📊 <b>POSIÇÕES ABERTAS ({len(pos)})</b>\n"
    for p in pos:
        sym = p.get("symbol","?"); side = p.get("holdSide","?")
        total = float(p.get("total",0)); entry = float(p.get("openPriceAvg",0))
        upl = float(p.get("unrealizedPL",0)); marg = float(p.get("marginSize",0))
        liq = float(p.get("liquidationPrice",0) or 0)
        roe = (upl/marg*100) if marg else 0
        arrow = "↑" if side=="long" else "↓"
        m += f"━━━━━━━━━━━━━━━\n"
        m += f"{arrow} <b>{sym}</b> {side.upper()}\n"
        m += f"📊 Size: {total} | 💲 Entrada: ${fmt(entry)}\n"
        m += f"💰 L/P: ${upl:+.4f} ({roe:+.1f}%)\n"
        m += f"💥 Liquidação: ${fmt(liq)}"
    send(m)

def fechar_posicao(symbol):
    symbol = symbol.upper()
    if not symbol.endswith("USDT"): symbol += "USDT"
    resp = bg_request("GET", "/api/v2/mix/position/all-position", {"productType":"USDT-FUTURES","marginCoin":"USDT"})
    pos = None
    for p in resp.get("data", []):
        if p.get("symbol")==symbol and float(p.get("total",0))>0:
            pos = p; break
    if not pos:
        send(f"📭 Sem posição em <b>{symbol}</b>."); return
    upl = float(pos.get("unrealizedPL",0))
    r = bg_close_position(symbol)
    if r.get("code")=="00000":
        bg_cancel_all(symbol)
        send(f"✅ <b>{symbol} FECHADA!</b>\n💰 L/P: ~${upl:+.4f}\n🧹 Ordens canceladas.")
    else:
        send(f"❌ Erro ao fechar: {r.get('msg','?')}")

def mostrar_ganhos():
    resp = bg_request("GET", "/api/v2/mix/position/history-position", {"productType":"USDT-FUTURES","limit":"50"})
    if resp.get("code")!="00000":
        send(f"⚠️ Erro: {resp.get('msg','?')}"); return
    d = resp.get("data")
    lista = d.get("list",[]) if isinstance(d, dict) else (d or [])
    if not lista: send("📭 Sem histórico."); return
    total=0; wins=0; losses=0; linhas=[]
    for p in lista:
        sym=p.get("symbol","?")
        pnl=float(p.get("netProfit") or p.get("pnl") or 0)
        total+=pnl
        if pnl>0: wins+=1
        elif pnl<0: losses+=1
        linhas.append(f"{'🟢' if pnl>=0 else '🔴'} {sym}: ${pnl:+.2f}")
    m=f"📒 <b>HISTÓRICO ({len(lista)} trades)</b>\n━━━━━━━━━━━━━━━\n"
    m+="\n".join(linhas[:15])
    m+=f"\n━━━━━━━━━━━━━━━\n✅ {wins} ganhos | ❌ {losses} perdas\n"
    m+=f"💰 <b>TOTAL: ${total:+.2f}</b>"
    send(m)

def mostrar_ajuda():
    m  = f"🤖 <b>COMANDOS ({VERSAO})</b>\n━━━━━━━━━━━━━━━\n"
    m += "/saldo /posicoes /ganhos\n/fechar SOL /teste LTC /ajuda\n━━━━━━━━━━━━━━━\n"
    m += "<b>Entrar (após sinal):</b>\n"
    m += "<b>sim 5</b> → $5 margem\n"
    m += "<b>sim r1</b> → arrisca $1 no SL\n"
    m += "+ <b>trail</b> ou <b>hibrido</b>\n"
    m += "<b>não</b> → cancelar\n━━━━━━━━━━━━━━━\n"
    m += f"⚠️ Aviso se perda diária > ${DAILY_LOSS_WARNING}\n"
    m += f"📏 Limite posição: ${MAX_NOTIONAL}"
    send(m)

def process_replies():
    global last_update_id
    for u in get_updates():
        last_update_id = u["update_id"]
        text = u.get("message",{}).get("text","").strip().lower()
        if not text: continue
        if text.startswith("/teste"):
            p=text.split(); forcar_teste(p[1] if len(p)>1 else "BTC"); continue
        if text.startswith("/saldo"): mostrar_saldo(); continue
        if text.startswith("/posicoes") or text.startswith("/posições"): mostrar_posicoes(); continue
        if text.startswith("/ganhos") or text.startswith("/historico") or text.startswith("/histórico"): mostrar_ganhos(); continue
        if text.startswith("/fechar"):
            p=text.split()
            if len(p)>1: fechar_posicao(p[1])
            else: send("Usa: <b>/fechar SOL</b>")
            continue
        if text.startswith("/ajuda") or text.startswith("/start"): mostrar_ajuda(); continue
        if text.startswith("/"): continue
        if CHAT_ID not in pending: continue
        if text in ("não","nao","n","no"):
            send("❌ Sinal cancelado."); pending.pop(CHAT_ID, None); continue
        if text.startswith("sim") or text.startswith("s "):
            has_conf = "confirmar" in text
            t_limpo = text.replace("sim","").replace("confirmar","").strip()
            partes = t_limpo.split()
            if not partes:
                send("⚠️ Formato: <b>sim 5</b> ou <b>sim r1</b>"); continue
            try:
                primeiro = partes[0]
                if primeiro.startswith("r") and len(primeiro)>1:
                    valor = float(primeiro[1:].replace(",",".").replace("$",""))
                    tipo = "risco"
                else:
                    valor = float(primeiro.replace(",",".").replace("$",""))
                    tipo = "margem"
                resto = " ".join(partes[1:])
                modo = "normal"
                if "trail" in resto: modo = "trail"
                if "hib" in resto: modo = "hibrido"
                # aviso de drawdown
                if not has_conf and not DRY_RUN:
                    perda = perda_hoje()
                    if perda <= -DAILY_LOSS_WARNING:
                        send(f"⚠️ <b>Aviso:</b> já perdeste <b>${abs(perda):.2f}</b> hoje.\n"
                             f"Pausa para pensar? Se queres mesmo entrar:\n"
                             f"<b>{text} confirmar</b>\nOu <b>não</b> para cancelar.")
                        continue
                sig, ohlc, e20, e50, bgsym = pending[CHAT_ID]
                etiqueta = f"${valor} {'risco' if tipo=='risco' else 'margem'}"
                send(f"⏳ A processar {etiqueta} (modo {modo})...")
                executar_trade(sig, bgsym, valor, modo, tipo)
                pending.pop(CHAT_ID, None)
            except Exception as e:
                send(f"⚠️ Formato errado. Ex: <b>sim 5</b> ou <b>sim r1 hibrido</b>\n({e})")

# ==================== LOOP ====================
estado = "🔬 DRY RUN" if DRY_RUN else "💵 REAL"
print(f"Bot {VERSAO} iniciado! Modo: {estado}")
send(f"🤖 <b>FuturesScan Bot {VERSAO}</b>\nModo: <b>{estado}</b>\n⚡ Máx {MAX_LEV}x | Trailing {CALLBACK_RATIO}%\n📏 Limite ${MAX_NOTIONAL} | Aviso perda ${DAILY_LOSS_WARNING}\nEscreve <b>/ajuda</b>")

while True:
    process_replies()
    if time.time()-last_analysis >= 3600:
        print("A analisar mercado...")
        try:
            signals = analyze()
            for item in signals:
                enviar_sinal(*item); break
            if not signals: print("Sem sinais fortes.")
        except Exception as e:
            print(f"Erro geral: {e}")
    time.sleep(3)
