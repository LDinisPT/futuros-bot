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
VERSAO = "v5.3.2"
DAILY_LOSS_WARNING = 5.0
MAX_NOTIONAL = 500.0
ANALYSIS_INTERVAL = 900  # 900 segundos = 15 minutos

pending = {}
last_update_id = 0
last_analysis = 0
last_position_check = 0
posicoes_abertas_cache = {}

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
        print(f"✅ {len(pairs)} pares validos")
        return pairs if pairs else ORIGINAL_PAIRS
    except Exception as e:
        print(f"⚠️ Erro pares: {e}")
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

def get_funding_rate(symbol):
    try:
        resp = bg_request("GET", "/api/v2/mix/market/funding-rate", {"symbol": symbol})
        if resp.get("code") == "00000" and resp.get("data"):
            return float(resp["data"][0].get("fundingRate", 0))
    except:
        pass
    return 0

def perda_hoje():
    resp = bg_request("GET", "/api/v2/mix/position/history-position", {"productType":"USDT-FUTURES","limit":"100"})
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

def count_trades_hoje():
    resp = bg_request("GET", "/api/v2/mix/position/history-position", {"productType":"USDT-FUTURES","limit":"100"})
    if resp.get("code") != "00000": return 0
    d = resp.get("data")
    lista = d.get("list",[]) if isinstance(d, dict) else (d or [])
    hoje = time.strftime("%Y-%m-%d", time.gmtime())
    count = 0
    for p in lista:
        utime = int(p.get("utime") or 0)
        if utime == 0: continue
        data_fecho = time.strftime("%Y-%m-%d", time.gmtime(utime/1000))
        if data_fecho == hoje:
            count += 1
    return count

def executar_trade(sig, bgsym, valor, modo="normal", tipo_valor="margem"):
    sym, price, _, rsi_v, label, score, reasons, atr_val = sig
    is_long = "LONG" in label
    sl, tp1, tp2, alav = calc_levels(price, label, atr_val)
    sl = round_price(sl, bgsym); tp1 = round_price(tp1, bgsym)
    lev = min(alav, MAX_LEV)

    if tipo_valor == "risco":
        distancia = abs(sl - price)
        if distancia <= 0:
            send("⚠️ Distância SL inválida."); return
        size_raw = valor / distancia
        notional = size_raw * price
        if notional > MAX_NOTIONAL:
            send(f"⚠️ Posição muito grande (${notional:.0f})."); return
        size = round_size(size_raw, bgsym)
        margem = notional / lev
        risco_real = valor
    else:
        margem = valor
        notional = margem * lev
        if notional > MAX_NOTIONAL:
            send(f"⚠️ Posição muito grande (${notional:.0f})."); return
        size = calc_size(notional, price, bgsym)
        distancia = abs(sl - price)
        risco_real = size * distancia

    direcao = "LONG" if is_long else "SHORT"
    arrow = "↑" if is_long else "↓"

    if DRY_RUN:
        m  = f"🔬 <b>DRY RUN — modo {modo.upper()}</b>\n━━━━━━━━━━━━━━━\n"
        m += f"{arrow} {sym}/USDT — {direcao}\n💲 Entrada: ~${fmt(price)}\n"
        m += f"⚡ {lev}x | 💰 Margem ${margem:.2f}\n📊 Size: {size}\n"
        m += f"🛑 SL: ${fmt(sl)} (risco ${risco_real:.2f})\n🎯 TP1: ${fmt(tp1)}\n"
        m += "<i>(DRY_RUN)</i>"
        send(m); return

    if check_open_position(bgsym):
        send(f"⚠️ Já tens posição em <b>{sym}</b>."); return

    bg_set_leverage(bgsym, lev)
    time.sleep(1)

    if modo == "normal":
        r = bg_place_order(bgsym, is_long, size, sl, tp1)
    else:
        r = bg_place_order(bgsym, is_long, size, sl)

    if r.get("code") != "00000":
        send(f"❌ Erro ao abrir: {r.get('msg','?')}"); return

    extra = "TP1 fecha 100%"
    avisos = []
    if modo == "trail":
        rt = bg_place_trailing(bgsym, is_long, size, tp1, CALLBACK_RATIO)
        if rt.get("code") == "00000":
            extra = f"Trailing {CALLBACK_RATIO}% (100%)"
        else:
            avisos.append(f"⚠️ Trailing falhou")
            extra = "SL fixo"
    elif modo == "hibrido":
        metade = round_size(size/2, bgsym)
        resto = round_size(size - metade, bgsym)
        rtp = bg_close_limit(bgsym, is_long, metade, tp1)
        rtr = bg_place_trailing(bgsym, is_long, resto, tp1, CALLBACK_RATIO)
        ok_tp = rtp.get("code") == "00000"
        ok_tr = rtr.get("code") == "00000"
        extra = f"TP1 50% [{'ok' if ok_tp else 'FALHOU'}] + Trailing [{'ok' if ok_tr else 'FALHOU'}]"

    m  = f"✅ <b>POSIÇÃO ABERTA!</b> ({modo})\n━━━━━━━━━━━━━━━\n"
    m += f"{arrow} <b>{sym}/USDT — {direcao}</b>\n💲 Entrada: ~${fmt(price)}\n"
    m += f"⚡ {lev}x | 💰 Margem ${margem:.2f}\n📊 Size: {size}\n"
    m += f"🛑 SL: ${fmt(sl)} | 🎯 TP1: ${fmt(tp1)}\n"
    m += f"📉 Risco: <b>${risco_real:.2f}</b>\n📋 {extra}\n━━━━━━━━━━━━━━━"
    send(m)
    
    posicoes_abertas_cache[bgsym] = {
        "sym": sym,
        "lado": direcao,
        "entrada": price,
        "modo": modo,
        "tempo_abertura": time.time()
    }

# ==================== RASTREAMENTO DE POSIÇÕES ====================
def verificar_posicoes_fechadas():
    global last_position_check, posicoes_abertas_cache
    
    if time.time() - last_position_check < 300:
        return
    
    last_position_check = time.time()
    
    resp = bg_request("GET", "/api/v2/mix/position/all-position",
                      {"productType":"USDT-FUTURES","marginCoin":"USDT"})
    
    if resp.get("code") != "00000":
        return
    
    posicoes_atuais = {p.get("symbol"): p for p in resp.get("data", []) 
                       if float(p.get("total",0)) > 0}
    
    for bgsym, info_antiga in list(posicoes_abertas_cache.items()):
        if bgsym not in posicoes_atuais:
            resp_hist = bg_request("GET", "/api/v2/mix/position/history-position", 
                                  {"productType":"USDT-FUTURES","limit":"10"})
            if resp_hist.get("code") == "00000":
                d = resp_hist.get("data")
                lista = d.get("list",[]) if isinstance(d, dict) else (d or [])
                for p in lista:
                    if p.get("symbol") == bgsym:
                        pnl = float(p.get("netProfit") or p.get("pnl") or 0)
                        tempo_aberto = int((time.time() - info_antiga["tempo_abertura"])/60)
                        emoji = "🟢" if pnl >= 0 else "🔴"
                        m = f"{emoji} <b>POSIÇÃO FECHADA</b>\n━━━━━━━━━━━━━━━\n"
                        m += f"<b>{info_antiga['sym']}/USDT {info_antiga['lado']}</b>\n"
                        m += f"💲 Entrada: ~${fmt(info_antiga['entrada'])}\n"
                        m += f"💰 Resultado: <b>${pnl:+.4f}</b>\n"
                        m += f"⏱️ Duração: {tempo_aberto}m\n"
                        m += f"📋 Modo: {info_antiga['modo']}"
                        send(m)
                        break
            
            del posicoes_abertas_cache[bgsym]

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

# ==================== CANDLESTICK PATTERNS ====================
def detect_engulfing(ohlc):
    """Detecta padrão Engulfing (bullish ou bearish)"""
    if len(ohlc) < 2: return 0
    o1, c1 = float(ohlc[-2][1]), float(ohlc[-2][4])
    o2, c2 = float(ohlc[-1][1]), float(ohlc[-1][4])
    
    # Engulfing bullish: vela vermelha seguida de vela verde grande
    if c1 < o1 and c2 > o2 and o2 < c1 and c2 > o1:
        return 2  # Reforça LONG
    # Engulfing bearish: vela verde seguida de vela vermelha grande
    if c1 > o1 and c2 < o2 and o2 > c1 and c2 < o1:
        return -2  # Reforça SHORT
    return 0

def detect_rejection(ohlc):
    """Detecta rejeição de resistência/suporte"""
    if len(ohlc) < 2: return 0
    o, h, l, c = float(ohlc[-1][1]), float(ohlc[-1][2]), float(ohlc[-1][3]), float(ohlc[-1][4])
    
    body = abs(c - o)
    wick_top = h - max(o, c)
    wick_bottom = min(o, c) - l
    
    # Rejeição de resistência: cauda grande no topo
    if wick_top > body * 1.5 and c < o:
        return -1  # Rejeição bearish
    # Rejeição de suporte: cauda grande no fundo
    if wick_bottom > body * 1.5 and c > o:
        return 1  # Rejeição bullish
    return 0

def detect_inside_bar(ohlc):
    """Detecta consolidação (inside bar)"""
    if len(ohlc) < 2: return 0
    o1, h1, l1 = float(ohlc[-2][1]), float(ohlc[-2][2]), float(ohlc[-2][3])
    h2, l2 = float(ohlc[-1][2]), float(ohlc[-1][3])
    
    # Vela atual dentro da vela anterior
    if h2 <= h1 and l2 >= l1:
        return 0.5  # Consolidação (reforça qualquer sinal)
    return 0

# ==================== ANALISE ====================
def analyze():
    global last_analysis
    last_analysis = time.time()
    signals = []
    for pair, sym, bgsym in PAIRS:
        try:
            od = requests.get("https://api.kraken.com/0/public/OHLC", params={"pair":pair,"interval":60}, timeout=15).json()
            ohlc = od["result"][list(od["result"].keys())[0]]
            closes=[float(c[4]) for c in ohlc]; highs=[float(c[2]) for c in ohlc]; lows=[float(c[3]) for c in ohlc]
            volumes=[float(c[6]) for c in ohlc]
            td=requests.get("https://api.kraken.com/0/public/Ticker", params={"pair":pair}, timeout=10).json()
            t=td["result"][list(td["result"].keys())[0]]
            price=float(t["c"][0]); op=float(t["o"])
            r=rsi(closes); e20=ema_arr(closes,20); e50=ema_arr(closes,50)
            bull=e20[-1]>e50[-1]
            ml,sl_,hist=macd(closes); atr_val=atr(highs,lows,closes)
            vol_recente = sum(volumes[-5:]) / 5
            vol_medio = sum(volumes[-25:-5]) / 20 if len(volumes)>=25 else vol_recente
            vol_ratio = vol_recente / vol_medio if vol_medio else 1
            
            # Funding Rate
            funding = get_funding_rate(bgsym)
            
            # Candlestick Patterns
            engulfing = detect_engulfing(ohlc)
            rejection = detect_rejection(ohlc)
            inside_bar = detect_inside_bar(ohlc)
            
            score=0; reasons=[]
            if r<30: score+=3; reasons.append("RSI sobrevendido")
            elif r<40: score+=1
            elif r>70: score-=3; reasons.append("RSI sobrecomprado")
            elif r>60: score-=1
            if bull: score+=2; reasons.append("EMA bullish")
            else: score-=2; reasons.append("EMA bearish")
            if ml>sl_ and hist>0: score+=2; reasons.append("MACD bullish")
            elif ml<sl_ and hist<0: score-=2; reasons.append("MACD bearish")
            if vol_ratio > 1.3 and score>0: score+=1; reasons.append("Volume↑")
            elif vol_ratio < 0.6: score = int(score * 0.7); reasons.append("Volume baixo")
            
            # Funding Rate impact
            if score > 0 and funding > 0.03:
                score -= 1; reasons.append(f"Funding alto ({funding:+.3%})")
            elif score < 0 and funding < -0.03:
                score += 1; reasons.append(f"Funding baixo ({funding:+.3%})")
            
            # Candlestick Patterns
            if engulfing > 0 and score > 0:
                score += engulfing; reasons.append("Engulfing bullish ✅")
            elif engulfing < 0 and score < 0:
                score += engulfing; reasons.append("Engulfing bearish ✅")
            
            if rejection != 0:
                score += rejection; reasons.append("Rejection" + (" bullish" if rejection > 0 else " bearish"))
            
            if inside_bar > 0:
                if score > 0: score += inside_bar; reasons.append("Consolidação bullish")
                elif score < 0: score += inside_bar; reasons.append("Consolidação bearish")
            
            if score>=4:
                signals.append(((sym,price,0,r,"🟢 LONG FORTE",score,reasons,atr_val),ohlc,e20,e50,bgsym))
            elif score<=-4:
                signals.append(((sym,price,0,r,"🔴 SHORT FORTE",score,reasons,atr_val),ohlc,e20,e50,bgsym))
            print(f"{sym}: RSI={r} score={score} funding={funding:+.3%}")
            time.sleep(0.6)
        except Exception as e:
            print(f"Erro {sym}: {e}")
    return signals

def enviar_sinal(sig, ohlc, e20, e50, bgsym):
    sym,price,_,rsi_v,label,score,reasons,atr_val = sig
    sl,tp1,tp2,_ = calc_levels(price,label,atr_val)
    dist_pct = abs(sl-price)/price*100
    risco_5m = 5 * 3 * dist_pct/100
    cap  = f"{'↑' if 'LONG' in label else '↓'} <b>{sym}/USD — {label}</b>\n━━━━━━━━━━━━━━━\n"
    cap += f"💲 Entrada: ${fmt(price)}\n🛑 SL: ${fmt(sl)} ({dist_pct:.2f}%)\n🎯 TP1: ${fmt(tp1)}\n"
    cap += f"📉 RSI: {rsi_v} | Score: {score:+d}\n📌 {', '.join(reasons[:3])}\n━━━━━━━━━━━━━━━\n"
    cap += f"💰 <b>ENTRAR?</b>\n"
    cap += f"✅ <b>sim 5</b> → $5 margem (risco ~${risco_5m:.2f})\n"
    cap += f"✅ <b>sim r1</b> → arrisca $1 no SL\n"
    cap += f"➕ <b>trail</b> ou <b>hibrido</b>\n❌ <b>não</b>"
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
        send(f"⚠️ Par inválido. Ex: /teste LTC")
        return
    pair,sym,bgsym = alvo
    try:
        od=requests.get("https://api.kraken.com/0/public/OHLC",params={"pair":pair,"interval":60},timeout=15).json()
        ohlc=od["result"][list(od["result"].keys())[0]]
        closes=[float(c[4]) for c in ohlc]; highs=[float(c[2]) for c in ohlc]; lows=[float(c[3]) for c in ohlc]
        td=requests.get("https://api.kraken.com/0/public/Ticker",params={"pair":pair},timeout=10).json()
        t=td["result"][list(td["result"].keys())[0]]
        price=float(t["c"][0]); r=rsi(closes); e20=ema_arr(closes,20); e50=ema_arr(closes,50)
        bull=e20[-1]>e50[-1]; atr_val=atr(highs,lows,closes)
        label = "🟢 LONG FORTE" if bull else "🔴 SHORT FORTE"
        sig=(sym,price,0,r,label,0,["TESTE MANUAL"],atr_val)
        send(f"🧪 Teste: {sym}")
        enviar_sinal(sig,ohlc,e20,e50,bgsym)
    except Exception as e:
        send(f"⚠️ Erro: {e}")

def mostrar_saldo():
    resp = bg_request("GET", "/api/v2/mix/account/accounts", {"productType":"USDT-FUTURES"})
    if resp.get("code") != "00000": send(f"⚠️ Erro"); return
    data = resp.get("data", [])
    if not data: send("📭 Sem dados"); return
    a = data[0]
    equity = float(a.get("accountEquity", a.get("usdtEquity",0)))
    avail = float(a.get("available", 0))
    upl = float(a.get("unrealizedPL", 0))
    perda = perda_hoje()
    m  = f"💰 <b>SALDO</b>\n━━━━━━━━━━━━━━━\n"
    m += f"💵 Total: ${equity:.2f}\n✅ Disponível: ${avail:.2f}\n"
    m += f"📊 L/P aberto: ${upl:+.2f}\n📅 L/P hoje: ${perda:+.2f}"
    send(m)

def mostrar_posicoes():
    resp = bg_request("GET", "/api/v2/mix/position/all-position", {"productType":"USDT-FUTURES","marginCoin":"USDT"})
    if resp.get("code") != "00000": send(f"⚠️ Erro"); return
    pos = [p for p in resp.get("data", []) if float(p.get("total",0)) > 0]
    if not pos: send("📭 Sem posições"); return
    m = f"📊 <b>POSIÇÕES ({len(pos)})</b>\n"
    for p in pos:
        sym=p.get("symbol","?"); side=p.get("holdSide","?")
        total=float(p.get("total",0)); entry=float(p.get("openPriceAvg",0))
        upl=float(p.get("unrealizedPL",0)); marg=float(p.get("marginSize",0))
        roe=(upl/marg*100) if marg else 0
        arrow="↑" if side=="long" else "↓"
        m+=f"━━━━━━\n{arrow} <b>{sym}</b>\n💰 ${upl:+.4f} ({roe:+.1f}%)"
    send(m)

def fechar_posicao(symbol):
    symbol = symbol.upper()
    if not symbol.endswith("USDT"): symbol += "USDT"
    
    # 1. Valida posição
    resp = bg_request("GET", "/api/v2/mix/position/all-position", 
                     {"productType":"USDT-FUTURES","marginCoin":"USDT"})
    pos = None
    for p in resp.get("data", []):
        if p.get("symbol")==symbol and float(p.get("total",0))>0:
            pos = p; break
    if not pos: 
        send(f"📭 Sem posição em {symbol}"); return
    
    upl = float(pos.get("unrealizedPL",0))
    
    # 2. Cancela TODAS as ordens PRIMEIRO
    print(f"⏳ Cancelando ordens de {symbol}...")
    cancel_r = bg_cancel_all(symbol)
    print(f"Cancel 1 response: {cancel_r}")
    time.sleep(1)
    
    # 3. DEPOIS fecha a posição
    print(f"⏳ Fechando posição de {symbol}...")
    close_r = bg_close_position(symbol)
    print(f"Close response: {close_r}")
    time.sleep(1)
    
    # 4. Cancela NOVAMENTE (por segurança) — mata qualquer ordem órfã
    print(f"⏳ Verificando limpeza final de {symbol}...")
    cancel_r2 = bg_cancel_all(symbol)
    print(f"Cancel 2 response: {cancel_r2}")
    time.sleep(0.5)
    
    if close_r.get("code")=="00000":
        send(f"✅ <b>{symbol}</b> fechada com sucesso!\n💰 L/P: ${upl:+.4f}\n✔️ Todas as ordens (SL/TP/Trailing) canceladas")
        posicoes_abertas_cache.pop(symbol, None)
    else: 
        send(f"❌ Erro ao fechar: {close_r.get('msg','?')}")

def mostrar_ganhos():
    resp = bg_request("GET", "/api/v2/mix/position/history-position", {"productType":"USDT-FUTURES","limit":"100"})
    if resp.get("code")!="00000": send(f"⚠️ Erro"); return
    d = resp.get("data")
    lista = d.get("list",[]) if isinstance(d, dict) else (d or [])
    hoje = time.strftime("%Y-%m-%d", time.gmtime())
    trades_hoje = []
    for p in lista:
        utime = int(p.get("utime") or 0)
        if utime == 0: continue
        data_fecho = time.strftime("%Y-%m-%d", time.gmtime(utime/1000))
        if data_fecho == hoje:
            trades_hoje.append(p)
    if not trades_hoje: send("📭 Sem trades hoje"); return
    total=0; wins=0; losses=0; linhas=[]
    for p in trades_hoje:
        sym=p.get("symbol","?")
        pnl=float(p.get("netProfit") or p.get("pnl") or 0)
        total+=pnl; wins+=1 if pnl>0 else 0; losses+=1 if pnl<0 else 0
        linhas.append(f"{'🟢' if pnl>=0 else '🔴'} {sym}: ${pnl:+.2f}")
    m=f"📒 <b>HOJE ({len(trades_hoje)} trades)</b>\n━━━━━━━━━━━━━━━\n"
    m+="\n".join(linhas)
    m+=f"\n━━━━━━━━━━━━━━━\n✅ {wins} | ❌ {losses}\n💰 <b>${total:+.2f}</b>"
    send(m)

def mostrar_stats():
    resp = bg_request("GET", "/api/v2/mix/position/history-position", {"productType":"USDT-FUTURES","limit":"100"})
    if resp.get("code")!="00000": send(f"⚠️ Erro"); return
    d = resp.get("data")
    lista = d.get("list",[]) if isinstance(d, dict) else (d or [])
    if len(lista) < 5: send("📭 Precisa de pelo menos 5 trades"); return
    
    total_pnl = 0; wins = 0; losses = 0; ganhos = []; perdas = []
    duracao_total = 0; count_duracao = 0
    
    for p in lista:
        pnl = float(p.get("netProfit") or p.get("pnl") or 0)
        total_pnl += pnl
        if pnl > 0: wins += 1; ganhos.append(pnl)
        elif pnl < 0: losses += 1; perdas.append(abs(pnl))
        
        ctime = int(p.get("ctime") or 0)
        utime = int(p.get("utime") or 0)
        if ctime and utime:
            duracao_total += (utime - ctime)
            count_duracao += 1
    
    media_ganho = sum(ganhos) / len(ganhos) if ganhos else 0
    media_perda = sum(perdas) / len(perdas) if perdas else 0
    profit_factor = (sum(ganhos) / sum(perdas)) if (perdas and sum(perdas) > 0) else 0
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    duracao_media = duracao_total / count_duracao / 1000 / 60 if count_duracao else 0
    
    m = f"📈 <b>ESTATÍSTICAS ({len(lista)} trades)</b>\n━━━━━━━━━━━━━━━\n"
    m += f"✅ Win rate: {win_rate:.1f}% ({wins}/{wins+losses})\n"
    m += f"💰 Profit factor: {profit_factor:.2f}x\n"
    m += f"🎯 Total P&L: ${total_pnl:+.2f}\n"
    m += f"📊 Média ganho: ${media_ganho:+.4f}\n"
    m += f"❌ Média perda: ${media_perda:+.4f}\n"
    m += f"⏱️ Duração média: {duracao_media:.0f}m"
    send(m)

def mostrar_ajuda():
    m  = f"🤖 <b>COMANDOS ({VERSAO})</b>\n━━━━━━━━━━━━━━━\n"
    m += "/saldo /posicoes /ganhos /stats\n"
    m += "/fechar SOL /teste LTC /ajuda\n━━━━━━━━━━━━━━━\n"
    m += "<b>Ao receber sinal:</b>\n"
    m += "<b>50 h</b> → $50, hibrido (RECOMENDADO)\n"
    m += "<b>50</b> → $50, normal\n"
    m += "<b>50 t</b> → $50, trailing\n"
    m += "<b>r1 h</b> → $1 risco, hibrido\n"
    m += "<b>não</b> → ignora sinal\n"
    m += "━━━━━━━━━━━━━━━\n"
    m += f"⚠️ Aviso perda: ${DAILY_LOSS_WARNING}\n"
    m += f"📏 Limite: ${MAX_NOTIONAL}\n"
    m += f"⏰ Análise: a cada 15 min"
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
        if text.startswith("/ganhos"): mostrar_ganhos(); continue
        if text.startswith("/stats"): mostrar_stats(); continue
        if text.startswith("/fechar"):
            p=text.split()
            fechar_posicao(p[1] if len(p)>1 else ""); continue
        if text.startswith("/ajuda") or text.startswith("/start"): mostrar_ajuda(); continue
        if text.startswith("/"): continue
        if CHAT_ID not in pending: continue
        if text in ("não","nao","n","no"):
            send("❌ Cancelado"); pending.pop(CHAT_ID, None); continue
        # Parser: aceita "50 h", "sim 50 h", "20", "r1 h", etc
        if any(text[0].isdigit() for _ in [0]) or text.startswith("sim") or text.startswith("r"):
            has_conf = "confirmar" in text
            
            # Remove "sim" se existir
            t_limpo = text.replace("sim","").replace("confirmar","").strip()
            partes = t_limpo.split()
            
            if not partes:
                send("⚠️ Formato: 50 h / 20 / r1 h\n✅ h = hibrido, t = trail"); continue
            try:
                primeiro = partes[0]
                if primeiro.startswith("r") and len(primeiro)>1:
                    valor = float(primeiro[1:].replace(",","."))
                    tipo = "risco"
                else:
                    valor = float(primeiro.replace(",","."))
                    tipo = "margem"
                resto = " ".join(partes[1:])
                modo = "normal"
                if "trail" in resto or "t" in resto: modo = "trail"
                if "hib" in resto or "h" in resto: modo = "hibrido"
                if not has_conf and not DRY_RUN:
                    perda = perda_hoje()
                    if perda <= -DAILY_LOSS_WARNING:
                        send(f"⚠️ <b>Aviso:</b> já perdeste <b>${abs(perda):.2f}</b> hoje.\nPara entrar mesmo assim:\n<b>{text} confirmar</b>\nOu <b>não</b> para cancelar.")
                        continue
                sig, ohlc, e20, e50, bgsym = pending[CHAT_ID]
                send(f"⏳ A processar ${valor} ({tipo})...")
                executar_trade(sig, bgsym, valor, modo, tipo)
                pending.pop(CHAT_ID, None)
            except Exception as e:
                send(f"⚠️ Erro: {e}")

# ==================== LOOP ====================
estado = "🔬 DRY RUN" if DRY_RUN else "💵 REAL"
print(f"Bot {VERSAO} — {estado}")
send(f"🤖 <b>FuturesScan Bot {VERSAO}</b>\n{estado}\n⚡ Máx {MAX_LEV}x | Polling 15min\n✅ Atalhos: 50 h (hibrido), 20, r1 h\nEscreve /ajuda")

while True:
    process_replies()
    verificar_posicoes_fechadas()
    if time.time()-last_analysis >= ANALYSIS_INTERVAL:
        print(f"A analisar mercado ({ANALYSIS_INTERVAL}s)...")
        try:
            signals = analyze()
            for item in signals:
                enviar_sinal(*item); break
            if not signals: print("Sem sinais fortes.")
        except Exception as e:
            print(f"Erro: {e}")
    time.sleep(3)
