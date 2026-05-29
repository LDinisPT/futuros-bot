import os, time, requests, io, hmac, hashlib, base64, json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ==================== CONFIG ====================
TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
BG_KEY = os.environ["BOT_BG_KEY"]          # Atualizado para bater com as tuas chaves
BG_SECRET = os.environ["BOT_BG_SECRET"]
BG_PASS = os.environ["BOT_BG_PASS"]
API = f"https://api.telegram.org/bot{TOKEN}"
BG_API = "https://api.bitget.com"

MAX_LEV = 3
CALLBACK_RATIO = 2.5   # % do trailing stop
DRY_RUN = False        # True = simula | False = dinheiro real
MIN_NOTIONAL = 5.0     # Valor mínimo em dólares para uma ordem na Bitget

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
        r.raise_for_status() # Lança erro se o status do Telegram não for 200
        return r.json().get("result", [])
    except Exception as e:
        print(f"Erro ao conectar com API do Telegram: {e}")
        return []

def fmt(p):
    if p > 100: return f"{p:,.2f}"
    if p > 1: return f"{p:.4f}"
    return f"{p:.6f}"

# ==================== BITGET SIGN & REQUEST ====================
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
        print(f"Erro crítico na requisição Bitget: {e}")
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
            raise Exception(f"Resposta inválida da Bitget: {data.get('msg')}")
        valid = {c.get("symbol") for c in data["data"]}
        pairs = [p for p in ORIGINAL_PAIRS if p[2] in valid]
        print(f"✅ {len(pairs)} pares válidos na Bitget")
        return pairs if pairs else ORIGINAL_PAIRS
    except Exception as e:
        print(f"⚠️ Erro ao atualizar pares: {e} - Usando fallback fixo")
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
    except Exception as e:
        print(f"Erro ao obter precisão para {bgsym}: {e}")
    return 4, 4

def calc_size(notional, price, bgsym):
    vp, _ = get_precision(bgsym)
    s = notional / price
    m = 10 ** vp
    size = round(s * m) / m
    size = max(size, 0.001)
    print(f"SIZE {bgsym}: nocional=${notional:.2f} preço=${price:.4f} tamanho_lote={size}")
    return size

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
        print(f"check_pos erro: {resp.get('msg')}")
        return False
    for pos in resp.get("data", []):
        if pos.get("symbol") == symbol and float(pos.get("total",0)) > 0:
            return True
    return False

def bg_set_leverage(symbol, lev):
    resp = bg_request("POST", "/api/v2/mix/account/set-leverage", {
        "symbol":symbol, "productType":"USDT-FUTURES", "marginCoin":"USDT", "leverage":str(lev)
    })
    if resp.get("code") != "00000":
        print(f"⚠️ Alerta Alavancagem: Rejeitado ao mudar para {lev}x em {symbol}. Motivo: {resp.get('msg')}")
        return False
    return True

def bg_place_order(symbol, is_long, size, sl, tp=None):
    side = "buy" if is_long else "sell"
    body = {
        "symbol":symbol, "productType":"USDT-FUTURES", "marginMode":"isolated",
        "marginCoin":"USDT", "size":str(size), "side":side, "orderType":"market",
        "presetStopLossPrice":str(sl)
    }
    if tp is not None:
        body["presetStopSurplusPrice"] = str(tp)
    return bg_request("POST", "/api/v2/mix/order/place-order", body)

def bg_place_tpsl(symbol, hold_side, size, trigger, plan_type):
    return bg_request("POST", "/api/v2/mix/order/place-tpsl-order", {
        "marginCoin":"USDT", "productType":"USDT-FUTURES", "symbol":symbol,
        "planType":plan_type, "triggerPrice":str(trigger), "triggerType":"mark_price",
        "executePrice":"0", "holdSide":hold_side, "size":str(size)
    })

def bg_place_trailing(symbol, is_long, size, trigger, callback):
    side = "sell" if is_long else "buy"
    return bg_request("POST", "/api/v2/mix/order/place-plan-order", {
        "planType":"moving_plan", "symbol":symbol, "productType":"USDT-FUTURES",
        "marginMode":"isolated", "marginCoin":"USDT", "size":str(size),
        "callbackRatio":str(callback), "triggerPrice":str(trigger),
        "triggerType":"mark_price", "side":side, "reduceOnly":"YES", "orderType":"market"
    })

def calc_levels(price, signal, atr_val):
    if atr_val <= 0: atr_val = price * 0.01
    sl_m, tp1_m, tp2_m = 1.8, 2.8, 5.0
    if "LONG" in signal:
        return price - atr_val*sl_m, price + atr_val*tp1_m, price + atr_val*tp2_m, 3
    return price + atr_val*sl_m, price - atr_val*tp1_m, price - atr_val*tp2_m, 3

def executar_trade(sig, bgsym, margem, modo="normal"):
    sym, price, _, rsi_v, label, score, reasons, atr_val = sig
    is_long = "LONG" in label
    sl, tp1, tp2, alav = calc_levels(price, label, atr_val)
    
    sl = round_price(sl, bgsym)
    tp1 = round_price(tp1, bgsym)
    lev = min(alav, MAX_LEV)
    notional = margem * lev
    
    # Validação de Nocional Mínimo da Exchange
    if notional < MIN_NOTIONAL:
        send(f"❌ <b>Erro de tamanho:</b> O valor nocional total (${notional:.2f}) é menor que o mínimo exigido pela Bitget (${MIN_NOTIONAL}). Aumenta a margem ou a alavancagem.")
        return

    size = calc_size(notional, price, bgsym)
    hold_side = "long" if is_long else "short"
    direcao = "LONG" if is_long else "SHORT"
    arrow = "↑" if is_long else "↓"

    if DRY_RUN:
        m  = f"🔬 <b>DRY RUN — modo {modo.upper()}</b>\n━━━━━━━━━━━━━━━\n"
        m += f"{arrow} {sym}/USDT — {direcao}\n💲 Entrada: ~${fmt(price)}\n"
        m += f"⚡ {lev}x | 💰 ${margem}\n📊 Size: {size}\n🛑 SL: ${fmt(sl)}\n🎯 TP1: ${fmt(tp1)}\n"
        if modo == "normal":
            m += "📋 TP1 fecha 100%\n"
        elif modo == "trail":
            m += f"📋 Trailing {CALLBACK_RATIO}% nos 100%\n"
        elif modo == "hibrido":
            metade = round_size(size/2, bgsym)
            m += f"📋 TP1 fecha 50% ({metade}) + trailing {CALLBACK_RATIO}% nos restantes\n"
        m += f"━━━━━━━━━━━━━━━\n<i>(DRY_RUN — nada executado)</i>"
        send(m)
        return

    if check_open_position(bgsym):
        send(f"⚠️ Já tens posição aberta em <b>{sym}</b>. Operação abortada para evitar sobreposição.")
        return

    # Tenta definir a alavancagem e avisa se falhar (mas continua para segurança se a alavancagem antiga for compatível)
    bg_set_leverage(bgsym, lev)

    if modo == "normal":
        r = bg_place_order(bgsym, is_long, size, sl, tp1)
    else:
        r = bg_place_order(bgsym, is_long, size, sl)

    if r.get("code") != "00000":
        send(f"❌ Erro ao abrir ordem de entrada: {r.get('msg','?')} (cod {r.get('code','?')})")
        return

    extra = "TP1 fecha 100%"
    avisos = []
    if modo == "trail":
        rt = bg_place_trailing(bgsym, is_long, size, tp1, CALLBACK_RATIO)
        if rt.get("code") == "00000":
            extra = f"Trailing {CALLBACK_RATIO}% (100%)"
        else:
            avisos.append(f"⚠️ Trailing falhou: {rt.get('msg','?')}\n(SL fixo continua ativo)")
            extra = "SL fixo ativo (trailing falhou)"
    elif modo == "hibrido":
        metade = round_size(size/2, bgsym)
        resto = round_size(size - metade, bgsym)
        rtp = bg_place_tpsl(bgsym, hold_side, metade, tp1, "profit_plan")
        rtr = bg_place_trailing(bgsym, is_long, resto, tp1, CALLBACK_RATIO)
        ok_tp = rtp.get("code") == "00000"
        ok_tr = rtr.get("code") == "00000"
        extra = f"TP1 50% [{'ok' if ok_tp else 'FALHOU'}] + Trailing {CALLBACK_RATIO}% [{'ok' if ok_tr else 'FALHOU'}]"
        if not ok_tp: avisos.append(f"⚠️ Distribuição de TP1 falhou: {rtp.get('msg','?')}")
        if not ok_tr: avisos.append(f"⚠️ Submissão do Trailing falhou: {rtr.get('msg','?')}")

    m  = f"✅ <b>POSIÇÃO ABERTA COM SUCESSO!</b> (modo {modo})\n━━━━━━━━━━━━━━━\n"
    m += f"{arrow} <b>{sym}/USDT — {direcao}</b>\n💲 Entrada: ~${fmt(price)}\n"
    m += f"⚡ {lev}x | 💰 ${margem}\n📊 Size: {size}\n🛑 SL: ${fmt(sl)}\n🎯 TP1: ${fmt(tp1)}\n"
    m += f"📋 {extra}\n━━━━━━━━━━━━━━━\n"
    if avisos:
        m += "\n".join(avisos) + "\n"
    m += "⚠️ Recomenda-se conferir a ordem na Bitget!"
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
        ax.set_title(f'{sym}/USDT (Bitget) - {d} | 50 Velas (1h)',color='#c8d8e8',fontsize=10,pad=10)
        ax.legend(loc='upper left',fontsize=7,facecolor='#0d1318',edgecolor='#1a2430',labelcolor='#c8d8e8')
        plt.tight_layout()
        buf=io.BytesIO(); plt.savefig(buf,format='png',dpi=120,facecolor='#0d1318'); plt.close()
        return buf
    except Exception as e:
        print(f"Erro ao gerar gráfico: {e}")
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

# ==================== ANÁLISE (100% BITGET) ====================
def analyze():
    global last_analysis
    last_analysis = time.time()
    signals = []
    
    for pair, sym, bgsym in PAIRS:
        try:
            # 1. Coleta histórico de velas (OHLC) 1H da Bitget
            candle_url = f"{BG_API}/api/v2/mix/market/candles"
            candle_params = {
                "symbol": bgsym,
                "productType": "USDT-FUTURES",
                "granularity": "1H",
                "limit": "100"
            }
            res_candles = requests.get(candle_url, params=candle_params, timeout=15).json()
            if res_candles.get("code") != "00000" or not res_candles.get("data"):
                print(f"⚠️ Sem dados históricos de velas para {bgsym}")
                continue
                
            # Inverte os dados para cronologia correta (Passado -> Presente)
            ohlc = res_candles["data"][::-1]
            closes = [float(c[4]) for c in ohlc]
            highs = [float(c[2]) for c in ohlc]
            lows = [float(c[3]) for c in ohlc]
            
            # 2. Coleta preço em tempo real e mudança percentual de 24h via Ticker Bitget
            ticker_url = f"{BG_API}/api/v2/mix/market/ticker"
            res_ticker = requests.get(ticker_url, params={"symbol": bgsym, "productType": "USDT-FUTURES"}, timeout=10).json()
            if res_ticker.get("code") != "00000" or not res_ticker.get("data"):
                print(f"⚠️ Sem dados de ticker atualizados para {bgsym}")
                continue
                
            ticker_data = res_ticker["data"][0]
            price = float(ticker_data.get("lastPr", closes[-1]))
            change = float(ticker_data.get("change24h", 0)) * 100 
            
            # 3. Processamento de Indicadores
            r = rsi(closes)
            e20 = ema_arr(closes, 20)
            e50 = ema_arr(closes, 50)
            bull = e20[-1] > e50[-1]
            ml, sl_, hist = macd(closes)
            atr_val = atr(highs, lows, closes)
            
            # 4. Score de Decisão
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
            
            if ml > sl_ and hist > 0: score += 2; reasons.append("MACD bullish")
            elif ml < sl_ and hist < 0: score -= 2; reasons.append("MACD bearish")
            
            if score >= 4:
                signals.append(((sym, price, change, r, "🟢 LONG FORTE", score, reasons, atr_val), ohlc, e20, e50, bgsym))
            elif score <= -4:
                signals.append(((sym, price, change, r, "🔴 SHORT FORTE", score, reasons, atr_val), ohlc, e20, e50, bgsym))
                
            print(f"Bitget -> {sym}: Preço={price} RSI={r} MACD={hist:+.4f} score={score}")
            time.sleep(0.3) # Delay curto para não travar o loop de respostas do Telegram
        except Exception as e:
            print(f"Erro na análise do par {sym}: {e}")
    return signals

def enviar_sinal(sig, ohlc, e20, e50, bgsym):
    sym,price,change,rsi_v,label,score,reasons,atr_val = sig
    sl,tp1,tp2,alav = calc_levels(price,label,atr_val)
    lev = min(alav, MAX_LEV)
    arrow = "↑" if "LONG" in label else "↓"
    modo = "🔬 DRY RUN" if DRY_RUN else "💵 REAL"
    cap  = f"{arrow} <b>{sym}/USDT — {label}</b> ({modo})\n━━━━━━━━━━━━━━━\n"
    cap += f"💲 Entrada: ${fmt(price)}\n🛑 SL: ${fmt(sl)}\n🎯 TP1: ${fmt(tp1)}\n🎯 TP2: ${fmt(tp2)}\n"
    cap += f"⚡ Alavancagem: {lev}x\n━━━━━━━━━━━━━━━\n"
    cap += f"📉 RSI: {rsi_v} | Variação 24h: {change:+.2f}%\nScore: {score:+d}\n📌 {', '.join(reasons)}\n━━━━━━━━━━━━━━━\n"
    cap += f"💰 <b>ENTRAR NA OPERAÇÃO?</b>\n"
    cap += f"✅ <b>sim 5</b> → SL + TP1 fixo\n"
    cap += f"✅ <b>sim 5 trail</b> → trailing {CALLBACK_RATIO}%\n"
    cap += f"✅ <b>sim 5 hibrido</b> → 50% TP + 50% trailing\n"
    cap += f"❌ <b>não</b>\n⚠️ <i>Risco calculado por volatilidade ATR.</i>"
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
        candle_url = f"{BG_API}/api/v2/mix/market/candles"
        res_candles = requests.get(candle_url, params={"symbol": bgsym, "productType": "USDT-FUTURES", "granularity": "1H", "limit": "100"}, timeout=15).json()
        
        ohlc = res_candles["data"][::-1]
        closes = [float(c[4]) for c in ohlc]
        highs = [float(c[2]) for c in ohlc]
        lows = [float(c[3]) for c in ohlc]
        
        ticker_url = f"{BG_API}/api/v2/mix/market/ticker"
        res_ticker = requests.get(ticker_url, params={"symbol": bgsym, "productType": "USDT-FUTURES"}, timeout=10).json()
        ticker_data = res_ticker["data"][0]
        
        price = float(ticker_data.get("lastPr", closes[-1]))
        change = float(ticker_data.get("change24h", 0)) * 100
        
        r = rsi(closes)
        e20 = ema_arr(closes, 20); e50 = ema_arr(closes, 50)
        bull = e20[-1] > e50[-1]; atr_val = atr(highs, lows, closes)
        
        label = "🟢 LONG FORTE" if bull else "🔴 SHORT FORTE"
        sig=(sym,price,change,r,label,0,["TESTE MANUAL VIA BITGET"], atr_val)
        send(f"🧪 <b>Teste forçado Bitget: {sym}</b>")
        enviar_sinal(sig,ohlc,e20,e50,bgsym)
    except Exception as e:
        send(f"⚠️ Erro no teste Bitget: {e}")

def process_replies():
    global last_update_id
    for u in get_updates():
        last_update_id = u["update_id"]
        text = u.get("message",{}).get("text","").strip().lower()
        if not text: continue
        if text.startswith("/teste"):
            p=text.split(); forcar_teste(p[1] if len(p)>1 else "BTC"); continue
        if text.startswith("/"): continue
        if CHAT_ID not in pending: continue
        if text in ("não","nao","n","no"):
            send("❌ Sinal recusado e descartado.")
            pending.pop(CHAT_ID, None)
            continue
        if text.startswith("sim") or text.startswith("s "):
            partes = text.replace("sim","").strip().split()
            try:
                margem = float(partes[0].replace("$","").replace(",","."))
                modo = "normal"
                resto = " ".join(partes[1:])
                if "trail" in resto: modo = "trail"
                if "hib" in resto: modo = "hibrido"
                sig, ohlc, e20, e50, bgsym = pending[CHAT_ID]
                send(f"⏳ A processar ordem de ${margem} (modo {modo})...")
                executar_trade(sig, bgsym, margem, modo)
                pending.pop(CHAT_ID, None)
            except Exception as e:
                send("⚠️ Formato de resposta inválido. Tenta de novo usando: <b>sim 5</b> ou <b>sim 10 hibrido</b>")

# ==================== LOOP PRINCIPAL ====================
modo_inicial = "🔬 DRY RUN (Simulação)" if DRY_RUN else "💵 OPERAÇÃO REAL"
print(f"Bot Iniciado com sucesso! Modo de execução: {modo_inicial}")
send(f"🤖 <b>FuturesScan Bot Ativo</b>\nAmbiente: <b>{modo_inicial}</b>\n⚡ Limite: {MAX_LEV}x | Trailing: {CALLBACK_RATIO}%\nAnálise de mercado: 100% Bitget\n🧪 Executar Diagnóstico: <b>/teste BTC</b>")

while True:
    process_replies()
    
    # Execução periódica a cada 1 hora (3600 segundos)
    if time.time() - last_analysis >= 3600:
        print("Iniciando varredura cíclica de mercado...")
        try:
            signals = analyze()
            # ERRO CORRIGIDO: Removido o 'break' para enviar múltiplos sinais, se existirem
            for item in signals:
                enviar_sinal(*item)
            if not signals: 
                print("Varredura concluída: Nenhum sinal forte encontrado.")
        except Exception as e:
            print(f"Erro geral no loop de varredura: {e}")
            
    time.sleep(3)
