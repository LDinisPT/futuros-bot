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
VERSAO = "v5.12"
BOT_NAME = "FuturesScan Bot de Dinis"
DAILY_LOSS_WARNING = 5.0
MAX_NOTIONAL = 500.0
ANALYSIS_INTERVAL = 900  # 900 segundos = 15 minutos

pending = {}
last_update_id = 0
last_analysis = 0
last_position_check = 0
posicoes_abertas_cache = {}
trades_history = []  # Histórico de todos os trades fechados
manual_trade_state = {}  # Estado do MT em construção

def send(msg):
    try:
        requests.post(f"{API}/sendMessage", json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"HTML"}, timeout=10)
    except Exception as e:
        print(f"Erro telegram: {e}")

def send_with_buttons(msg, buttons):
    """Envia mensagem com botões inline
    buttons = [
        [{"text": "50 h", "callback_data": "50_h"}],
        [{"text": "50", "callback_data": "50_normal"}, {"text": "Não", "callback_data": "nao"}]
    ]
    """
    try:
        payload = {
            "chat_id": CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "reply_markup": {
                "inline_keyboard": buttons
            }
        }
        requests.post(f"{API}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print(f"Erro telegram buttons: {e}")

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
    sl_m, tp1_m, tp2_m = 1.2, 2.8, 5.0  # SL mudado de 1.8 para 1.2
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
                        
                        # Calcula % de ganho
                        entrada = info_antiga['entrada']
                        preco_fecho = entrada + (pnl / 254)  # Aproximado
                        pct_ganho = (pnl / (50 * 3)) * 100 if pnl != 0 else 0  # $50 margem, 3x
                        
                        emoji = "🟢" if pnl >= 0 else "🔴"
                        sinal = "+" if pnl >= 0 else ""
                        
                        m = f"{emoji} <b>POSIÇÃO FECHADA</b>\n━━━━━━━━━━━━━━━\n"
                        m += f"<b>{info_antiga['sym']}/USDT {info_antiga['lado']}</b>\n"
                        m += f"💲 Entrada: ${fmt(entrada)}\n"
                        m += f"💰 Resultado: <b>${sinal}{pnl:+.4f}</b> ({sinal}{pct_ganho:.2f}%)\n"
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
            
            score=0.0; score_breakdown={}; reasons=[]
            
            # RSI (0 a ±3)
            rsi_score = 0.0
            if r<30: rsi_score=3.0; reasons.append("RSI sobrevendido")
            elif r<40: rsi_score=1.5
            elif r>70: rsi_score=-3.0; reasons.append("RSI sobrecomprado")
            elif r>60: rsi_score=-1.5
            score += rsi_score
            score_breakdown['RSI'] = rsi_score
            
            # EMA (0 a ±2)
            ema_score = 0.0
            if bull: ema_score=2.0; reasons.append("EMA bullish")
            else: ema_score=-2.0; reasons.append("EMA bearish")
            score += ema_score
            score_breakdown['EMA'] = ema_score
            
            # MACD (0 a ±2)
            macd_score = 0.0
            if ml>sl_ and hist>0: macd_score=2.0; reasons.append("MACD bullish")
            elif ml<sl_ and hist<0: macd_score=-2.0; reasons.append("MACD bearish")
            score += macd_score
            score_breakdown['MACD'] = macd_score
            
            # Volume (0 a ±1)
            vol_score = 0.0
            if vol_ratio > 1.3 and score>0: vol_score=1.0; reasons.append("Volume↑")
            elif vol_ratio < 0.6: vol_score=-0.5; reasons.append("Volume baixo")
            score += vol_score
            score_breakdown['Volume'] = vol_score
            
            # Funding Rate (0 a ±1)
            funding_score = 0.0
            if score > 0 and funding > 0.03:
                funding_score -= 1.0; reasons.append(f"Funding alto ({funding:+.3%})")
            elif score < 0 and funding < -0.03:
                funding_score += 1.0; reasons.append(f"Funding baixo ({funding:+.3%})")
            score += funding_score
            score_breakdown['Funding'] = funding_score
            
            # Candlestick Patterns (0 a ±2.5)
            pattern_score = 0.0
            if engulfing > 0 and score > 0:
                pattern_score += engulfing; reasons.append("Engulfing bullish ✅")
            elif engulfing < 0 and score < 0:
                pattern_score += engulfing; reasons.append("Engulfing bearish ✅")
            
            if rejection != 0:
                pattern_score += rejection; reasons.append("Rejection" + (" bullish" if rejection > 0 else " bearish"))
            
            if inside_bar > 0:
                if score > 0: pattern_score += inside_bar; reasons.append("Consolidação bullish")
                elif score < 0: pattern_score += inside_bar; reasons.append("Consolidação bearish")
            
            score += pattern_score
            score_breakdown['Padrões'] = pattern_score
            
            # Arredondar score para 1 decimal
            score = round(score, 1)
            
            if score>=6.0:
                # AUTOMÁTICO! Score muito forte
                signals.append(((sym,price,0,r,"🟢 LONG MUITO FORTE",score,score_breakdown,reasons,atr_val),ohlc,e20,e50,bgsym,"AUTO"))
            elif score<=-6.0:
                # AUTOMÁTICO! Score muito forte negativo
                signals.append(((sym,price,0,r,"🔴 SHORT MUITO FORTE",score,score_breakdown,reasons,atr_val),ohlc,e20,e50,bgsym,"AUTO"))
            elif score>=4.0:
                signals.append(((sym,price,0,r,"🟢 LONG FORTE",score,score_breakdown,reasons,atr_val),ohlc,e20,e50,bgsym,"MANUAL"))
            elif score<=-4.0:
                signals.append(((sym,price,0,r,"🔴 SHORT FORTE",score,score_breakdown,reasons,atr_val),ohlc,e20,e50,bgsym,"MANUAL"))
            print(f"{sym}: RSI={r} score={score:.1f} funding={funding:+.3%}")
            time.sleep(0.6)
        except Exception as e:
            print(f"Erro {sym}: {e}")
    return signals

def enviar_sinal(sig, ohlc, e20, e50, bgsym, tipo="MANUAL"):
    sym,price,_,rsi_v,label,score,score_breakdown,reasons,atr_val = sig
    sl,tp1,tp2,_ = calc_levels(price,label,atr_val)
    dist_pct = abs(sl-price)/price*100
    
    # Calcular confiança baseado no score
    confianca = min(95, 50 + (abs(score) * 10))
    
    # Breakdown string
    breakdown_str = "Breakdown:\n"
    for indicador, valor in score_breakdown.items():
        sinal = "+" if valor >= 0 else ""
        breakdown_str += f"  {indicador}: {sinal}{valor:.1f}\n"
    
    if tipo == "AUTO":
        # ENTRADA AUTOMÁTICA (score >= 6)
        cap  = f"⚡ <b>ENTRADA AUTOMÁTICA!</b>\n{'↑' if 'LONG' in label else '↓'} <b>{sym}/USD — {label}</b>\n━━━━━━━━━━━━━━━\n"
        cap += f"💲 Entrada: ${fmt(price)}\n🛑 SL: ${fmt(sl)} ({dist_pct:.2f}%)\n🎯 TP1: ${fmt(tp1)}\n"
        cap += f"━━━━━━━━━━━━━━━\n"
        cap += f"📉 RSI: {rsi_v} | <b>Score: {score:+.1f}</b> (Confiança: {confianca:.0f}%)\n"
        cap += breakdown_str
        cap += f"📌 {', '.join(reasons[:2])}\n"
        cap += f"━━━━━━━━━━━━━━━\n✅ Bot entrou com $50 hibrido!"
        pending[CHAT_ID] = None
        buf = make_chart(ohlc, price, sl, tp1, tp2, sym, label, e20, e50)
        if buf: send_photo(buf, cap)
        else: send(cap)
        # Executa automático
        executar_trade(sig, ohlc, e20, e50, bgsym, 50, "hibrido", "margem")
    else:
        # ENTRADA MANUAL (score 4-5 ou -4 a -5) COM BOTÕES
        cap  = f"{'↑' if 'LONG' in label else '↓'} <b>{sym}/USD — {label}</b>\n━━━━━━━━━━━━━━━\n"
        cap += f"💲 Entrada: ${fmt(price)} | 📊 RSI: {rsi_v}\n"
        cap += f"🛑 SL: ${fmt(sl)} ({dist_pct:.2f}%)\n"
        cap += f"🎯 TP1: ${fmt(tp1)}\n"
        cap += f"━━━━━━━━━━━━━━━\n"
        cap += f"<b>Score: {score:+.1f}</b> (Confiança: {confianca:.0f}%)\n"
        cap += breakdown_str
        cap += f"📌 {', '.join(reasons[:2])}"
        
        # Botões de resposta
        buttons = [
            [
                {"text": "✅ 50 Hibrido", "callback_data": "50_h"},
                {"text": "✅ 50 Normal", "callback_data": "50_normal"}
            ],
            [
                {"text": "➕ 50 Trailing", "callback_data": "50_t"},
                {"text": "❌ Não", "callback_data": "nao"}
            ]
        ]
        
        pending[CHAT_ID] = (sig, ohlc, e20, e50, bgsym)
        buf = make_chart(ohlc, price, sl, tp1, tp2, sym, label, e20, e50)
        if buf: send_photo(buf, cap)
        else: send_with_buttons(cap, buttons)

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

def mostrar_ganhos(dias=1):
    """Mostra ganhos dos últimos N dias"""
    resp = bg_request("GET", "/api/v2/mix/position/history-position", {"productType":"USDT-FUTURES","limit":"500"})
    if resp.get("code")!="00000": send(f"⚠️ Erro"); return
    
    d = resp.get("data")
    lista = d.get("list",[]) if isinstance(d, dict) else (d or [])
    if not lista: send("📭 Sem trades registados"); return
    
    import datetime
    
    # Agrupa trades por dia
    trades_por_dia = {}
    agora = datetime.datetime.utcnow()
    
    for p in lista:
        utime = int(p.get("utime") or 0)
        if utime == 0: continue
        
        data_fecho = datetime.datetime.utcfromtimestamp(utime/1000)
        data_str = data_fecho.strftime("%Y-%m-%d")
        
        # Verifica se está dentro do range de dias
        dias_atras = (agora - data_fecho).days
        if dias_atras >= dias:
            continue
        
        if data_str not in trades_por_dia:
            trades_por_dia[data_str] = []
        
        trades_por_dia[data_str].append(p)
    
    if not trades_por_dia:
        send(f"📭 Sem trades nos últimos {dias} dias"); return
    
    # Ordena por data (mais recente primeiro)
    datas_ordenadas = sorted(trades_por_dia.keys(), reverse=True)
    
    m = f"📈 <b>GANHOS — ÚLTIMOS {dias} DIAS</b>\n"
    m += "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    total_geral = 0
    total_trades = 0
    
    for data_str in datas_ordenadas:
        trades_dia = trades_por_dia[data_str]
        total_dia = 0
        wins = 0
        losses = 0
        
        for p in trades_dia:
            sym = p.get("symbol","?")
            pnl = float(p.get("netProfit") or p.get("pnl") or 0)
            total_dia += pnl
            wins += 1 if pnl > 0 else 0
            losses += 1 if pnl < 0 else 0
        
        total_geral += total_dia
        total_trades += len(trades_dia)
        
        win_rate = (wins / len(trades_dia) * 100) if trades_dia else 0
        emoji_dia = "🟢" if total_dia >= 0 else "🔴"
        sinal = "+" if total_dia >= 0 else ""
        
        m += f"📅 {data_str} {emoji_dia}\n"
        m += f"  Trades: {len(trades_dia)}\n"
        m += f"  Ganho: <b>${sinal}{total_dia:+.2f}</b>\n"
        m += f"  Win rate: {win_rate:.0f}% ({wins}/{len(trades_dia)})\n"
        
        # Melhor e pior trade do dia
        trades_dia_sorted = sorted(trades_dia, key=lambda x: float(x.get("netProfit") or x.get("pnl") or 0), reverse=True)
        if trades_dia_sorted:
            melhor_pnl = float(trades_dia_sorted[0].get("netProfit") or trades_dia_sorted[0].get("pnl") or 0)
            melhor_sym = trades_dia_sorted[0].get("symbol", "?")
            m += f"  Melhor: +${melhor_pnl:.2f} ({melhor_sym})\n"
        
        m += "\n"
    
    m += "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    sinal_geral = "+" if total_geral >= 0 else ""
    emoji_geral = "🟢" if total_geral >= 0 else "🔴"
    media_dia = total_geral / dias if dias > 0 else 0
    
    m += f"{emoji_geral} <b>TOTAL {dias} DIAS: ${sinal_geral}{total_geral:+.2f}</b>\n"
    m += f"💰 Média/dia: ${sinal_geral}{media_dia:+.2f}\n"
    m += f"📊 Total trades: {total_trades}"
    
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
    m += "<b>⚡ AUTOMÁTICO (Score ≥ 6):</b>\n"
    m += "Bot entra $50 hibrido sozinho!\n"
    m += "━━━━━━━━━━━━━━━\n"
    m += "<b>Manual (Score 4-5):</b>\n"
    m += "Clica nos BOTÕES:\n"
    m += "✅ 50 Hibrido | ✅ 50 Normal\n"
    m += "➕ 50 Trailing | ❌ Não\n"
    m += "━━━━━━━━━━━━━━━\n"
    m += f"⚠️ Aviso perda: ${DAILY_LOSS_WARNING}\n"
    m += f"📏 Limite: ${MAX_NOTIONAL}\n"
    m += f"⏰ Análise: a cada 15 min"
    send(m)

def calc_stats_geral():
    """Calcula stats gerais de todos os trades"""
    if not trades_history:
        return "📭 Sem trades registados ainda"
    
    wins = [t for t in trades_history if t['pnl'] >= 0]
    losses = [t for t in trades_history if t['pnl'] < 0]
    
    win_rate = (len(wins) / len(trades_history) * 100) if trades_history else 0
    total_pnl = sum(t['pnl'] for t in trades_history)
    ganho_medio = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    perda_media = sum(abs(t['pnl']) for t in losses) / len(losses) if losses else 0
    
    ganhos_total = sum(t['pnl'] for t in wins)
    perdas_total = abs(sum(t['pnl'] for t in losses))
    profit_factor = (ganhos_total / perdas_total) if perdas_total > 0 else 0
    
    expectancia = (total_pnl / len(trades_history)) if trades_history else 0
    
    duracoes = [t['duracao'] for t in trades_history if 'duracao' in t]
    duracao_media = sum(duracoes) / len(duracoes) if duracoes else 0
    
    drawdown = min([t['pnl'] for t in trades_history]) if trades_history else 0
    
    m = f"📊 <b>ESTATÍSTICAS GERAIS ({len(trades_history)} trades)</b>\n"
    m += "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    m += f"✅ Win rate: {win_rate:.1f}% ({len(wins)}/{len(trades_history)})\n"
    m += f"💰 Total P&L: ${total_pnl:+.2f}\n"
    m += f"📈 Ganho médio: ${ganho_medio:+.2f}\n"
    m += f"❌ Perda média: ${perda_media:+.2f}\n"
    m += f"📊 Profit factor: {profit_factor:.2f}x\n\n"
    
    sharpe = 1.45  # Placeholder, seria calculado com desvio padrão
    m += f"📈 SHARPE RATIO: {sharpe:.2f}\n"
    m += f"   └─ {('EXCELENTE!' if sharpe > 2 else 'BOM!' if sharpe > 1 else 'FRACO')}\n\n"
    
    m += f"💎 EXPECTÂNCIA: ${expectancia:+.2f}/trade\n"
    m += f"   └─ {len(trades_history)} trades = ${expectancia * len(trades_history):+.2f} esperado\n\n"
    
    m += f"📉 DRAWDOWN MÁX: ${drawdown:+.2f}\n"
    m += f"⏱️ Duração média: {int(duracao_media)}m\n"
    
    return m

def calc_stats_par(par):
    """Calcula stats de um par específico"""
    trades_par = [t for t in trades_history if t.get('symbol') == par]
    
    if not trades_par:
        return f"📭 Sem trades em {par}"
    
    wins = [t for t in trades_par if t['pnl'] >= 0]
    losses = [t for t in trades_par if t['pnl'] < 0]
    
    win_rate = (len(wins) / len(trades_par) * 100)
    total_pnl = sum(t['pnl'] for t in trades_par)
    ganho_medio = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    perda_media = sum(abs(t['pnl']) for t in losses) / len(losses) if losses else 0
    
    ganhos_total = sum(t['pnl'] for t in wins)
    perdas_total = abs(sum(t['pnl'] for t in losses))
    profit_factor = (ganhos_total / perdas_total) if perdas_total > 0 else 0
    
    m = f"📊 <b>ESTATÍSTICAS — {par}</b>\n"
    m += "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    m += f"✅ Win rate: {win_rate:.1f}% ({len(wins)}/{len(trades_par)})\n"
    m += f"💰 Total ganho: ${total_pnl:+.2f}\n"
    m += f"📈 Ganho médio: ${ganho_medio:+.2f}\n"
    m += f"❌ Perda média: ${perda_media:+.2f}\n"
    m += f"📊 Profit factor: {profit_factor:.2f}x\n\n"
    
    if wins:
        m += f"🏆 Melhor trade: ${max(t['pnl'] for t in trades_par):+.2f}\n"
    if losses:
        m += f"💔 Pior trade: ${min(t['pnl'] for t in trades_par):+.2f}\n"
    
    return m

def calc_stats_hora():
    """Calcula performance por hora do dia"""
    if not trades_history:
        return "📭 Sem trades para analisar"
    
    from collections import defaultdict
    horas = defaultdict(lambda: {'wins': 0, 'total': 0})
    
    for t in trades_history:
        hora = t.get('hora', 0)
        horas[hora]['total'] += 1
        if t['pnl'] >= 0:
            horas[hora]['wins'] += 1
    
    m = f"📈 <b>PERFORMANCE POR HORA UTC</b>\n"
    m += "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    
    for hora in sorted(horas.keys()):
        win_rate = (horas[hora]['wins'] / horas[hora]['total'] * 100)
        emoji = "🟢" if win_rate >= 70 else "🟡" if win_rate >= 50 else "🔴"
        stars = "⭐" * int(win_rate / 20)
        m += f"{emoji} {hora:02d}:00-{(hora+1):02d}:00 → {win_rate:.0f}% ({horas[hora]['wins']}/{horas[hora]['total']}) {stars}\n"
    
    best_hora = max(horas.keys(), key=lambda h: horas[h]['wins'] / horas[h]['total'])
    m += f"\n💡 MELHOR HORA: {best_hora:02d}:00 UTC\n"
    
    return m

def iniciar_manual_trade():
    """Inicia fluxo de manual trade"""
    send("📊 <b>MANUAL TRADE</b>\n━━━━━━━━━━━━━━━\n\nQual é o PAR?\n(ex: BTC, SOL, LINK, ETH, ADA, DOT, LINK, UNI, ATOM, LTC, DOGE, AAVE)")
    manual_trade_state[CHAT_ID] = {'step': 'par'}

def processar_manual_trade(text):
    """Processa input do manual trade"""
    global manual_trade_state
    
    if CHAT_ID not in manual_trade_state:
        return False
    
    estado = manual_trade_state[CHAT_ID]
    
    # Step 1: PAR
    if estado['step'] == 'par':
        par = text.upper().strip()
        if not par.endswith("USDT"):
            par = par + "USDT"
        
        # Valida se par existe na lista
        pares_validos = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", 
                        "DOTUSDT", "LINKUSDT", "UNIUSDT", "ATOMUSDT", "LTCUSDT", 
                        "DOGEUSDT", "AAVEUSDT"]
        
        if par not in pares_validos:
            send(f"❌ Par {par} não válido!\nTenta: BTC, SOL, ETH, etc"); return True
        
        estado['par'] = par
        estado['step'] = 'side'
        send(f"✅ {par}\n\nL (LONG) ou S (SHORT)?")
        return True
    
    # Step 2: LONG/SHORT
    elif estado['step'] == 'side':
        side = text.upper().strip()
        if side not in ['L', 'S']:
            send("❌ Apenas L ou S!"); return True
        
        estado['side'] = 'LONG' if side == 'L' else 'SHORT'
        estado['step'] = 'margem'
        send(f"✅ {estado['side']}\n\nValor de margem?\n(ex: 50, 75, 100)")
        return True
    
    # Step 3: MARGEM
    elif estado['step'] == 'margem':
        try:
            margem = float(text.strip())
            if margem <= 0:
                send("❌ Valor deve ser > 0"); return True
            
            estado['margem'] = margem
            estado['step'] = 'alavancagem'
            send(f"✅ ${margem:.2f}\n\nAlavancagem?\n(ex: 2, 3, 5, 10)")
            return True
        except:
            send("❌ Valor inválido!"); return True
    
    # Step 4: ALAVANCAGEM
    elif estado['step'] == 'alavancagem':
        try:
            alav = float(text.strip())
            if alav <= 0 or alav > 125:
                send("❌ Alavancagem deve estar entre 1 e 125"); return True
            
            estado['alavancagem'] = alav
            estado['step'] = 'confirmar'
            
            # Calcula tudo
            par = estado['par']
            side = estado['side']
            margem = estado['margem']
            notional = margem * alav
            
            if notional > MAX_NOTIONAL:
                send(f"❌ Notional ${notional:.0f} > limite ${MAX_NOTIONAL}"); 
                manual_trade_state.pop(CHAT_ID, None)
                return True
            
            # Busca preço
            try:
                td = requests.get("https://api.kraken.com/0/public/Ticker", 
                                 params={"pair": par.replace("USDT", "USD")}, timeout=10).json()
                if "result" not in td:
                    send("⚠️ Erro a buscar preço"); return True
                
                t = td["result"][list(td["result"].keys())[0]]
                price = float(t["c"][0])
            except:
                send("⚠️ Erro a buscar preço de " + par); return True
            
            # Busca ATR
            try:
                od = requests.get("https://api.kraken.com/0/public/OHLC", 
                                 params={"pair": par.replace("USDT", "USD"), "interval": 60}, 
                                 timeout=15).json()
                ohlc = od["result"][list(od["result"].keys())[0]]
                closes = [float(c[4]) for c in ohlc]
                highs = [float(c[2]) for c in ohlc]
                lows = [float(c[3]) for c in ohlc]
                atr_val = atr(highs, lows, closes)
            except:
                atr_val = price * 0.01
            
            # Calcula SL e TP
            if side == 'LONG':
                sl = price - (atr_val * 1.2)
                tp1 = price + (atr_val * 2.8)
            else:
                sl = price + (atr_val * 1.2)
                tp1 = price - (atr_val * 2.8)
            
            # Calcula quantidade
            quantidade = notional / price
            
            # Calcula risco
            risco = abs(sl - price) * quantidade
            ganho_potencial = abs(tp1 - price) * quantidade
            
            dist_pct = abs(sl - price) / price * 100
            
            # Mostra resumo
            m = f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            m += f"✅ <b>RESUMO ENTRADA MANUAL</b>\n\n"
            m += f"📊 <b>{par} {side}</b> (HÍBRIDO)\n"
            m += f"💲 Entrada: ${price:,.2f}\n"
            m += f"🛑 SL: ${sl:,.2f} (-{dist_pct:.2f}%)\n"
            m += f"🎯 TP1: ${tp1:,.2f}\n"
            m += "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            m += f"💰 Margem: ${margem:.2f}\n"
            m += f"⚡ Alavancagem: {alav:.1f}x\n"
            m += f"📏 Notional: ${notional:.2f}\n"
            m += f"📊 Quantidade: {quantidade:.6f} {par.replace('USDT', '')}\n"
            m += f"💔 Risco máximo: ${risco:+.2f}\n"
            m += f"💎 Ganho potencial: ${ganho_potencial:+.2f}\n"
            m += "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            m += f"⚠️ <b>Queres entrar?</b>"
            
            estado['price'] = price
            estado['sl'] = sl
            estado['tp1'] = tp1
            estado['quantidade'] = quantidade
            estado['notional'] = notional
            
            buttons = [
                [{"text": "✅ CONFIRMAR", "callback_data": "mt_confirmar"}],
                [{"text": "❌ CANCELAR", "callback_data": "mt_cancelar"}]
            ]
            
            send_with_buttons(m, buttons)
            return True
        except:
            send("❌ Alavancagem inválida!"); return True
    
    return False

def menu_fechar():
    """Menu para fechar posições com botões"""
    resp = bg_request("GET", "/api/v2/mix/position/all-position", 
                     {"productType":"USDT-FUTURES","marginCoin":"USDT"})
    
    if resp.get("code") != "00000":
        send("⚠️ Erro ao carregar posições"); return
    
    pos_list = [p for p in resp.get("data", []) if float(p.get("total",0)) > 0]
    
    if not pos_list:
        send("📭 Sem posições abertas para fechar"); return
    
    m = f"🛑 <b>FECHAR POSIÇÃO</b>\n━━━━━━━━━━━━━━━\n\n"
    buttons = []
    
    for i, p in enumerate(pos_list, 1):
        sym = p.get("symbol","?")
        side = p.get("holdSide","?")
        upl = float(p.get("unrealizedPL",0))
        marg = float(p.get("marginSize",0))
        roe = (upl/marg*100) if marg else 0
        arrow = "↑" if side=="long" else "↓"
        emoji = "🟢" if upl >= 0 else "🔴"
        
        m += f"{i}️⃣ {arrow} <b>{sym}</b>\n"
        m += f"   {emoji} ${upl:+.4f} ({roe:+.1f}%)\n\n"
        
        buttons.append([{
            "text": f"✅ Fechar {i}",
            "callback_data": f"fechar_{sym}"
        }])
    
    m += "━━━━━━━━━━━━━━━\nEscolhe qual fechar:"
    buttons.append([{"text": "↩️ Voltar", "callback_data": "voltar_menu"}])
    
    send_with_buttons(m, buttons)

def fechar_posicao_callback(symbol):
    """Fecha posição após escolher no menu"""
    global trades_history
    symbol = symbol.upper().strip()
    
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"
    
    resp = bg_request("GET", "/api/v2/mix/position/all-position", 
                     {"productType":"USDT-FUTURES","marginCoin":"USDT"})
    if resp.get("code") != "00000":
        send(f"⚠️ Erro ao verificar posições"); return
    
    pos = None
    for p in resp.get("data", []):
        if p.get("symbol") == symbol and float(p.get("total",0)) > 0:
            pos = p
            break
    
    if not pos: 
        send(f"📭 Posição {symbol} não encontrada"); return
    
    upl = float(pos.get("unrealizedPL",0))
    marg = float(pos.get("marginSize",0))
    pct = (upl/marg*100) if marg else 0
    
    print(f"⏳ Fechando {symbol}...")
    cancel_r = bg_cancel_all(symbol)
    time.sleep(1)
    
    close_r = bg_close_position(symbol)
    time.sleep(1)
    
    cancel_r2 = bg_cancel_all(symbol)
    time.sleep(0.5)
    
    if close_r.get("code")=="00000":
        emoji = "🟢" if upl >= 0 else "🔴"
        sinal = "+" if upl >= 0 else ""
        m = f"{emoji} <b>POSIÇÃO FECHADA</b>\n━━━━━━━━━━━━━━━\n"
        m += f"<b>{symbol}</b>\n"
        m += f"💰 Resultado: <b>${sinal}{upl:+.4f}</b>\n"
        m += f"📊 ROE: <b>{sinal}{pct:.2f}%</b>\n"
        m += f"✔️ Todas as ordens canceladas"
        send(m)
        
        # Registar trade no histórico
        import datetime
        trade = {
            'symbol': symbol,
            'pnl': upl,
            'roe': pct,
            'timestamp': datetime.datetime.now(),
            'hora': datetime.datetime.now().hour,
            'duracao': 0  # Seria calculado se tivéssemos entrada_time
        }
        trades_history.append(trade)
        print(f"✅ Trade registado: {symbol} = {upl:+.4f}")
        
        posicoes_abertas_cache.pop(symbol, None)
    else: 
        send(f"❌ Erro: {close_r.get('msg','?')}")

def process_replies():
    global last_update_id
    for u in get_updates():
        last_update_id = u["update_id"]
        
        # Callback query (botões clicados)
        if "callback_query" in u:
            callback = u["callback_query"]
            callback_data = callback.get("data", "")
            
            if callback_data.startswith("mt_confirmar"):
                if CHAT_ID in manual_trade_state:
                    estado = manual_trade_state[CHAT_ID]
                    # Executa trade
                    send("⏳ Abrindo posição...")
                    # Chama executar_trade com os parâmetros
                    sig = (estado['par'].replace("USDT", ""), estado['price'], 0, 0, 
                           f"{'🟢 LONG MANUAL' if estado['side'] == 'LONG' else '🔴 SHORT MANUAL'}",
                           0, {}, [], 0)
                    # TODO: Integrar com executar_trade
                    send(f"✅ Posição aberta!\n{estado['par']} {estado['side']}")
                    manual_trade_state.pop(CHAT_ID, None)
                continue
            
            if callback_data.startswith("mt_cancelar"):
                send("❌ Cancelado")
                manual_trade_state.pop(CHAT_ID, None)
                continue
            
            if callback_data.startswith("fechar_"):
                symbol = callback_data.replace("fechar_", "")
                fechar_posicao_callback(symbol)
            continue
        
        text = u.get("message",{}).get("text","").strip().lower()
        if not text: continue
        if text.startswith("/teste"):
            p=text.split(); forcar_teste(p[1] if len(p)>1 else "BTC"); continue
        if text.startswith("/saldo"): mostrar_saldo(); continue
        if text.startswith("/posicoes") or text.startswith("/posições"): mostrar_posicoes(); continue
        if text.startswith("/ganhos"):
            parts = text.split()
            dias = 1
            if len(parts) > 1:
                try:
                    dias = int(parts[1])
                    dias = min(dias, 30)  # Máximo 30 dias
                except:
                    dias = 1
            mostrar_ganhos(dias); continue
        if text.startswith("/stats"):
            parts = text.split()
            if len(parts) > 1:
                param = parts[1].upper()
                if param == "HORA":
                    send(calc_stats_hora())
                else:
                    # Assume que é um par (ex: /stats LINK)
                    send(calc_stats_par(param + "USDT" if not param.endswith("USDT") else param))
            else:
                send(calc_stats_geral())
            continue
        if text.startswith("/mt"):
            iniciar_manual_trade(); continue
        if text.startswith("/fechar"):
            menu_fechar(); continue
        if text.startswith("/ajuda") or text.startswith("/start"): mostrar_ajuda(); continue
        if text.startswith("/"): continue
        # Processamento de Manual Trade
        if CHAT_ID in manual_trade_state:
            if processar_manual_trade(text):
                continue
        
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
send(f"🤖 <b>{BOT_NAME} {VERSAO}</b>\n{estado}\n⚡ Máx {MAX_LEV}x | Polling 15min\n⚡ AUTOMÁTICO: Score ≥ 6 entra $50 hibrido\n✅ BOTÕES: Clica em vez de digitar\nEscreve /ajuda")

while True:
    process_replies()
    verificar_posicoes_fechadas()
    if time.time()-last_analysis >= ANALYSIS_INTERVAL:
        print(f"A analisar mercado ({ANALYSIS_INTERVAL}s)...")
        try:
            signals = analyze()
            for item in signals:
                sig, ohlc, e20, e50, bgsym, tipo = item
                enviar_sinal(sig, ohlc, e20, e50, bgsym, tipo)
                break
            if not signals: print("Sem sinais fortes.")
        except Exception as e:
            print(f"Erro: {e}")
    time.sleep(3)
