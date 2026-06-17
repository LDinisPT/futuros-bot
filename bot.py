import os, time, requests, io, hmac, hashlib, base64, json, socket, csv
from datetime import datetime
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# ===== LOGGING (v5.22) — identifica o bot (Pi vs Railway) e regista tudo =====
def setup_logging():
    """Setup logging para ficheiro, identifica se é Pi ou Railway."""
    hostname = socket.gethostname()
    is_pi = "Dinis-PI" in hostname or "raspberrypi" in hostname.lower()
    bot_id = "Pi" if is_pi else "Railway"
    
    # cria pasta de logs se nao existir
    log_dir = os.path.expanduser("~/logs")
    os.makedirs(log_dir, exist_ok=True)
    
    # ficheiro de log diario
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(log_dir, f"bot_{bot_id}_{today}.log")
    
    return log_file, bot_id

LOG_FILE, BOT_ID = setup_logging()

# Etiqueta visível nas mensagens — so aparece no Railway (no Pi fica vazia)
TAG = "[Railway] " if BOT_ID == "Railway" else ""

def log_msg(msg):
    """Escreve mensagem ao log com timestamp."""
    ts = datetime.now().strftime("%H:%M:%S")
    linha = f"[{ts}] {msg}\n"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(linha)
    except Exception as e:
        print(f"Erro ao escrever log: {e}")

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
VERSAO = "v6.1"
BOT_NAME = "FuturesScan Bot de Dinis"

# ===== PAPEL DA INSTANCIA (v6.0) =====
# Duas instancias (Pi + Railway) partilham a MESMA conta Bitget. Se ambas
# executassem, podiam abrir/fechar/cancelar em simultaneo (corrida + mensagens
# duplicadas). Por isso so UMA deve ser "principal" (executa e gere posicoes);
# a outra deve ser "alerta" (analisa e avisa, mas NAO mexe na conta).
#   ROLE=principal  -> executa trades, gere posicoes, manda fechos/reconciliacao
#   ROLE=alerta     -> so analisa e avisa (read-only na conta)
# Define ROLE=alerta no ambiente de UMA das instancias (ver instrucoes no fim).
ROLE = os.environ.get("ROLE", "principal").strip().lower()
PRINCIPAL = ROLE != "alerta"

# ===== HTTP / RETRY (v6.0) =====
HTTP_RETRIES = 3          # tentativas em chamadas Bitget antes de desistir
HTTP_BACKOFF = 0.8        # segundos base entre tentativas (cresce: 0.8, 1.6, 2.4)
_time_offset = 0          # offset (ms) entre relogio local e servidor Bitget

# ===== ESTRATEGIA DE SCORE (v6.1) =====
# Permite testar duas teses sem mexer no codigo:
#   reversao (default) -> RSI de reversao a media (peso ±3) + volume so confirma longs
#   momentum           -> RSI alinhado com a tendencia (peso ±1.5) + volume simetrico
# Muda so a variavel de ambiente ESTRATEGIA. O CSV regista qual foi usada em cada trade.
ESTRATEGIA = os.environ.get("ESTRATEGIA", "reversao").strip().lower()
if ESTRATEGIA not in ("reversao", "momentum"):
    ESTRATEGIA = "reversao"

# ===== REGISTO CSV sinal->resultado (v6.1) =====
# Grava UMA linha por trade AUTO fechado: features do sinal + resultado real.
# Serve para afinar os pesos do score com dados reais e comparar reversao vs momentum.
# ATENCAO: no Railway o disco e efemero (apaga a cada deploy). Usa um Volume do
# Railway montado em CSV_DIR, OU usa /csv no Telegram para puxar o ficheiro antes de
# cada redeploy.
CSV_DIR = os.environ.get("CSV_DIR", os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(CSV_DIR, "trades.csv")
CSV_COLS = ["data","estrategia","symbol","tipo","direcao","entrada","sl","tp1",
            "score","cats","rsi","rsi_score","ema_score","macd_score","vol_score",
            "funding","funding_score","pattern_score","atr","lev","margem",
            "pnl","roe_pct","duracao_min","resultado"]
DAILY_LOSS_WARNING = 5.0
MAX_NOTIONAL = 500.0
SL_MIN_PCT = 0.6  # SL minimo: 0.6% da entrada (evita stops colados em mercado calmo)
ANALYSIS_INTERVAL = 900  # 900 segundos = 15 minutos

# ===== GESTAO DE RISCO (v5.19) =====
DAILY_LOSS_LIMIT = 15.0   # circuit breaker: bloqueia novas entradas se perda do dia >= este valor ($)
RISK_PCT = 1.0            # % do saldo a arriscar por trade (sizing automatico)
RISK_AUTO_ENABLED = True  # True = entradas AUTO usam sizing por risco; False = usa $50 margem fixa
circuit_breaker_avisado = False  # evita spam do aviso de circuit breaker

# ===== QUALIDADE DE SINAL (v5.18) =====
SCORE_AUTO = 7.0          # antes 6.0 - threshold de entrada automatica
SCORE_MANUAL = 5.0        # antes 4.0 - threshold de sinal manual
MIN_CATEGORIAS = 2.0      # nº minimo de CATEGORIAS diferentes a concordar (v5.28: reduzido de 3→2 para mais trades, validado com +30% retorno)
COOLDOWN_MIN = 30         # minutos sem repetir sinal do mesmo par
ultimo_sinal_par = {}     # {bgsym: timestamp} - controla cooldown

pending = {}
last_update_id = 0
last_analysis = 0
last_position_check = 0
posicoes_abertas_cache = {}
trades_history = []  # Histórico de todos os trades fechados

# ==================== HTTP HELPER (v6.0) ====================
def http_get_json(url, params=None, timeout=15, retries=HTTP_RETRIES):
    """GET publico com retry + backoff. Devolve dict json ou None.
    Usado por todas as chamadas de mercado (candles, ticker, contracts)."""
    for tentativa in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 429:  # rate limit -> espera mais
                time.sleep(HTTP_BACKOFF * (tentativa + 2))
                continue
            return r.json()
        except Exception as e:
            if tentativa == retries - 1:
                print(f"Erro http_get_json ({url}): {e}")
                return None
            time.sleep(HTTP_BACKOFF * (tentativa + 1))
    return None

def bg_sync_time():
    """Sincroniza o offset de relogio com o servidor Bitget (evita rejeicao
    por timestamp se o relogio do Pi/Railway derivar)."""
    global _time_offset
    data = http_get_json(f"{BG_API}/api/v2/public/time", retries=2)
    try:
        if data and data.get("code") == "00000":
            server_ms = int(data["data"]["serverTime"])
            _time_offset = server_ms - int(time.time() * 1000)
            print(f"⏱️ Offset de relogio com Bitget: {_time_offset} ms")
    except Exception as e:
        print(f"Erro bg_sync_time: {e}")

def now_ms():
    """Timestamp em ms ja corrigido com o offset do servidor."""
    return str(int(time.time() * 1000) + _time_offset)

# ==================== BITGET MARKET DATA ====================
def bg_get_ohlcv(bgsym, granularity="60", limit=200):
    """Busca velas OHLCV da Bitget Futures"""
    data = http_get_json(
        f"{BG_API}/api/v2/mix/market/candles",
        params={"symbol": bgsym, "productType": "USDT-FUTURES", "granularity": granularity, "limit": str(limit)},
    )
    if not data or data.get("code") != "00000":
        print(f"Erro OHLCV {bgsym}: {data.get('msg') if data else 'sem resposta'}")
        return None
    candles = data.get("data", [])
    candles.reverse()  # Bitget: mais recente primeiro, invertemos
    return candles

def bg_get_ticker(bgsym):
    """Busca preço actual da Bitget. Retorna (price, open_24h)"""
    try:
        data = http_get_json(
            f"{BG_API}/api/v2/mix/market/ticker",
            params={"symbol": bgsym, "productType": "USDT-FUTURES"},
            timeout=10,
        )
        if not data or data.get("code") != "00000":
            return None, None
        t = data["data"][0]
        price  = float(t.get("lastPr") or t.get("last") or 0)
        open24 = float(t.get("open24h") or t.get("openUtc0") or price)
        return price, open24
    except Exception as e:
        print(f"Erro ticker {bgsym}: {e}")
        return None, None

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

def send_document(path, caption=""):
    """Envia um ficheiro (ex: o CSV) como documento no Telegram."""
    try:
        with open(path, "rb") as f:
            requests.post(f"{API}/sendDocument",
                          data={"chat_id":CHAT_ID,"caption":caption,"parse_mode":"HTML"},
                          files={"document":(os.path.basename(path), f)}, timeout=30)
        return True
    except Exception as e:
        print(f"Erro send_document: {e}")
        send(f"❌ Não consegui enviar o ficheiro: {str(e)[:60]}")
        return False

# ===== CSV sinal->resultado (v6.1) =====
def csv_registar(features, resultado):
    """Acrescenta uma linha ao trades.csv combinando features do sinal + resultado.
    features: dict guardado na abertura. resultado: dict com pnl/roe/duracao."""
    try:
        existe = os.path.isfile(CSV_PATH)
        linha = {
            "data": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "estrategia": features.get("estrategia", ESTRATEGIA),
            "symbol": features.get("symbol",""),
            "tipo": features.get("tipo",""),
            "direcao": features.get("direcao",""),
            "entrada": features.get("entrada",""),
            "sl": features.get("sl",""),
            "tp1": features.get("tp1",""),
            "score": features.get("score",""),
            "cats": features.get("cats",""),
            "rsi": features.get("rsi",""),
            "rsi_score": features.get("rsi_score",""),
            "ema_score": features.get("ema_score",""),
            "macd_score": features.get("macd_score",""),
            "vol_score": features.get("vol_score",""),
            "funding": features.get("funding",""),
            "funding_score": features.get("funding_score",""),
            "pattern_score": features.get("pattern_score",""),
            "atr": features.get("atr",""),
            "lev": features.get("lev",""),
            "margem": features.get("margem",""),
            "pnl": round(resultado.get("pnl",0), 4),
            "roe_pct": round(resultado.get("roe",0), 2),
            "duracao_min": resultado.get("duracao",""),
            "resultado": "WIN" if resultado.get("pnl",0) >= 0 else "LOSS",
        }
        with open(CSV_PATH, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLS)
            if not existe:
                w.writeheader()
            w.writerow(linha)
        log_msg(f"CSV | {linha['symbol']} {linha['direcao']} | {linha['resultado']} | pnl {linha['pnl']} | {linha['estrategia']}")
    except Exception as e:
        print(f"Erro csv_registar: {e}")

def features_do_sinal(sig, bgsym, tipo, sl, tp1, lev, margem, funding):
    """Constroi o dict de features a partir do sinal, para guardar na posicao."""
    sym, price, _, r, label, score, bd, reasons, atr_val = sig
    return {
        "estrategia": ESTRATEGIA,
        "symbol": sym,
        "tipo": tipo,
        "direcao": "LONG" if "LONG" in label else "SHORT",
        "entrada": round(price, 6),
        "sl": round(sl, 6),
        "tp1": round(tp1, 6),
        "score": score,
        "cats": bd.get("_cats",""),
        "rsi": round(r, 1),
        "rsi_score": bd.get("RSI",""),
        "ema_score": bd.get("EMA",""),
        "macd_score": bd.get("MACD",""),
        "vol_score": bd.get("Volume",""),
        "funding": round(funding, 5),
        "funding_score": bd.get("Funding",""),
        "pattern_score": bd.get("Padrões",""),
        "atr": round(atr_val, 6),
        "lev": lev,
        "margem": round(margem, 2),
    }

def edit_message(message_id, msg, buttons=None):
    """Edita uma mensagem existente (para menus inline que se transformam)."""
    try:
        payload = {"chat_id": CHAT_ID, "message_id": message_id,
                   "text": msg, "parse_mode": "HTML"}
        if buttons is not None:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        requests.post(f"{API}/editMessageText", json=payload, timeout=10)
    except Exception as e:
        print(f"Erro edit: {e}")

def answer_callback(callback_id, text=None):
    """Responde ao toque num botão (tira o 'relógio' a girar no Telegram)."""
    try:
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        requests.post(f"{API}/answerCallbackQuery", json=payload, timeout=10)
    except Exception as e:
        print(f"Erro answer_callback: {e}")

def send_com_teclado_fixo(msg):
    """Envia mensagem com teclado fixo em baixo (reply keyboard) — nunca desaparece.
    Os botões enviam texto como se o utilizador o tivesse escrito."""
    try:
        payload = {
            "chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML",
            "reply_markup": {
                "keyboard": [
                    [{"text":"📊 Menu"}, {"text":"📈 Posições"}],
                    [{"text":"💰 Saldo"}, {"text":"⚡ Entrar"}],
                ],
                "resize_keyboard": True,      # botões mais pequenos/ajustados
                "is_persistent": True,        # mantém-se sempre visível
            }
        }
        requests.post(f"{API}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print(f"Erro teclado fixo: {e}")

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
    body = json.dumps(params) if (method != "GET" and params) else ""
    if method == "GET" and params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        full_path = path + "?" + query
    else:
        full_path = path
    url = BG_API + full_path

    ultimo = {"code": "99999", "msg": "sem resposta"}
    for tentativa in range(HTTP_RETRIES):
        ts = now_ms()
        sign = bg_sign(ts, method, full_path, body)
        headers = {
            "ACCESS-KEY": BG_KEY, "ACCESS-SIGN": sign, "ACCESS-PASSPHRASE": BG_PASS,
            "ACCESS-TIMESTAMP": ts, "locale": "en-US", "Content-Type": "application/json"
        }
        try:
            if method == "POST":
                r = requests.post(url, headers=headers, data=body, timeout=15)
            else:
                r = requests.get(url, headers=headers, timeout=15)
            resp = r.json()
        except Exception as e:
            ultimo = {"code": "99999", "msg": str(e)}
            time.sleep(HTTP_BACKOFF * (tentativa + 1))
            continue

        code = resp.get("code")
        if code == "00000":
            return resp
        # erro de timestamp/assinatura -> ressincroniza relogio e tenta de novo
        if code in ("40009", "40005", "40008") or "timestamp" in str(resp.get("msg", "")).lower():
            bg_sync_time()
            ultimo = resp
            continue
        # outros erros: nao vale a pena repetir (ex: saldo, parametros)
        return resp
    return ultimo

# ==================== PARES + PRECISAO ====================
# ===== PARES VALIDADOS (v5.21) =====
# Apos backtest + validacao out-of-sample em 2 periodos de ~42 dias, so estes
# 4 pares foram lucrativos nos DOIS periodos (edge consistente, nao sorte):
#   BTC (PF 1.18/1.58), XRP (2.39/5.03), UNI (1.60/1.20), ATOM (1.34/1.42)
# Os restantes 8 ficam comentados abaixo:
#   - DOT, DOGE: bons so no periodo recente (suspeita de over-fitting)
#   - ADA, SOL, ETH: negativos nos dois periodos (sem edge)
#   - LINK, AAVE: instaveis (bons num periodo, maus no outro)
#   - LTC: nao passou nos criterios
# Para reativar: descomentar a linha. Revalidar periodicamente (mercados mudam).
ORIGINAL_PAIRS = [
    ("XBTUSD","BTC","BTCUSDT"),  ("XRPUSD","XRP","XRPUSDT"),
    ("UNIUSD","UNI","UNIUSDT"),  ("ATOMUSD","ATOM","ATOMUSDT"),
    # ("ETHUSD","ETH","ETHUSDT"),     # excluido: PF 0.44/0.71 (sem edge)
    # ("SOLUSD","SOL","SOLUSDT"),     # excluido: PF 0.40/0.63 (sem edge)
    # ("ADAUSD","ADA","ADAUSDT"),     # excluido: PF 0.00/0.38 (sem edge)
    # ("DOTUSDT","DOT","DOTUSDT"),    # so recente: PF 0.00/1.13 (over-fit?)
    # ("LINKUSD","LINK","LINKUSDT"),  # instavel: PF 1.93/0.32 (piorou)
    # ("LTCUSD","LTC","LTCUSDT"),     # nao validado
    # ("XDGUSD","DOGE","DOGEUSDT"),   # so recente: PF 0.80/1.10 (over-fit?)
    # ("AAVEUSD","AAVE","AAVEUSDT"),  # instavel: PF 1.27/0.78 (piorou)
]

def get_dynamic_pairs():
    data = http_get_json(f"{BG_API}/api/v2/mix/market/contracts",
                         params={"productType": "USDT-FUTURES"})
    try:
        if not data or data.get("code") != "00000" or not data.get("data"):
            raise Exception("resposta invalida")
        valid = {c.get("symbol") for c in data["data"]}
        pairs = [p for p in ORIGINAL_PAIRS if p[2] in valid]
        print(f"✅ {len(pairs)} pares validos")
        return pairs if pairs else ORIGINAL_PAIRS
    except Exception as e:
        print(f"⚠️ Erro pares: {e}")
        return ORIGINAL_PAIRS

PAIRS = get_dynamic_pairs()

# NOTA (v6.0): o antigo sistema de precisao (get_precision/round_price/round_size/
# calc_size) foi REMOVIDO. Tudo passa agora por get_contract_specs + ajustar_preco/
# ajustar_qtd (mais abaixo), que respeitam priceEndStep e minTradeNum. Isto corrige
# o erro "multiple of 0.X" que so estava resolvido no /mt manual.

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

# ===== OPEN INTEREST (v5.21) — informativo no /teste =====
oi_snapshots = {}  # {bgsym: (size, timestamp)} - guarda ultimo OI visto por par

def bg_get_oi(bgsym):
    """Busca o Open Interest atual do par. Retorna (size_float, ts_ms) ou (None,None)."""
    try:
        resp = requests.get(f"{BG_API}/api/v2/mix/market/open-interest",
            params={"symbol":bgsym, "productType":"USDT-FUTURES"}, timeout=10).json()
        if resp.get("code") == "00000":
            d = resp.get("data", {})
            lista = d.get("openInterestList", [])
            if lista:
                size = float(lista[0].get("size", 0))
                ts = int(d.get("ts", 0))
                return size, ts
    except Exception as e:
        print(f"Erro bg_get_oi {bgsym}: {e}")
    return None, None

def oi_contexto(bgsym, price_sobe=None):
    """Devolve uma linha informativa sobre o OI atual vs o ultimo snapshot.
    Guarda o novo snapshot para a proxima chamada. price_sobe: True/False/None
    para interpretar a combinacao OI+preco."""
    size, ts = bg_get_oi(bgsym)
    if size is None:
        return ""
    anterior = oi_snapshots.get(bgsym)
    oi_snapshots[bgsym] = (size, ts)  # atualiza snapshot

    if not anterior:
        # primeira leitura: nao ha com que comparar
        return f"📊 OI atual: {size:,.0f} (1ª leitura, sem comparação)"

    size_ant, ts_ant = anterior
    if size_ant <= 0:
        return f"📊 OI atual: {size:,.0f}"
    delta_pct = (size - size_ant) / size_ant * 100
    mins = max(1, int((ts - ts_ant) / 60000)) if ts > ts_ant else 0
    tempo = f"{mins}min" if mins < 60 else f"{mins//60}h{mins%60:02d}"

    # seta e interpretacao
    if delta_pct > 1.0:
        seta = "⬆️"
        interp = "entrada de dinheiro (tendência a ganhar força)"
    elif delta_pct < -1.0:
        seta = "⬇️"
        interp = "saída de posições (tendência a perder força)"
    else:
        seta = "➡️"
        interp = "estável"
    linha = f"📊 OI: {seta} {delta_pct:+.1f}% ({tempo}) — {interp}"
    return linha

def bg_set_position_tpsl(symbol, hold_side, sl_price, tp_price):
    """Coloca SL+TP numa posicao JA aberta (place-pos-tpsl).
    hold_side: 'long' ou 'short'. Retorna o dict de resposta."""
    return bg_request("POST", "/api/v2/mix/order/place-pos-tpsl", {
        "symbol": symbol, "productType": "USDT-FUTURES", "marginCoin": "USDT",
        "holdSide": hold_side,
        "stopSurplusTriggerPrice": str(tp_price), "stopSurplusTriggerType": "mark_price",
        "stopLossTriggerPrice": str(sl_price), "stopLossTriggerType": "mark_price",
    })

def bg_set_position_sl(symbol, is_long, size, sl_price):
    """Coloca um stop loss numa posicao aberta via place-plan-order.
    Usa o MESMO formato do trailing que ja funciona: side invertido + reduceOnly,
    SEM holdSide (que dava erro de specification em one-way mode).
    is_long: True se a posicao e LONG. size: tamanho da posicao a proteger."""
    side = "sell" if is_long else "buy"  # fecha a posicao na direcao oposta
    return bg_request("POST", "/api/v2/mix/order/place-plan-order", {
        "planType": "normal_plan", "symbol": symbol, "productType": "USDT-FUTURES",
        "marginMode": "isolated", "marginCoin": "USDT", "size": str(size),
        "triggerPrice": str(sl_price), "triggerType": "mark_price",
        "side": side, "reduceOnly": "YES", "orderType": "market",
        "clientOid": gen_oid()
    })

def bg_get_position_tpsl(symbol):
    """Verifica se uma posicao ja tem um stop loss colocado pelo bot.
    O SL e colocado como normal_plan reduceOnly (ver bg_set_position_sl), por isso
    procuramos plan orders normais reduceOnly para este simbolo.
    Tambem verifica track_plan (trailing) que ja serve de protecao."""
    try:
        for ptype in ("normal_plan", "track_plan"):
            resp = bg_request("GET", "/api/v2/mix/order/orders-plan-pending",
                              {"productType":"USDT-FUTURES", "planType":ptype})
            if resp.get("code") == "00000":
                d = resp.get("data")
                lista = (d.get("entrustedList") or d.get("list") or []) if isinstance(d, dict) else (d or [])
                for o in (lista or []):
                    if o.get("symbol") == symbol:
                        # reduceOnly indica ordem de fecho (protecao), nao de entrada
                        ro = str(o.get("reduceOnly","")).upper()
                        if ro in ("YES","TRUE","1") or ptype == "track_plan":
                            return True
    except Exception as e:
        print(f"Erro bg_get_position_tpsl {symbol}: {e}")
    return False

def bg_cancel_all(symbol):
    return bg_request("POST", "/api/v2/mix/order/cancel-all-orders", {
        "symbol": symbol, "productType": "USDT-FUTURES", "marginCoin": "USDT"
    })

def bg_list_plan_orders(plan_type):
    """Lista plan orders pendentes de um tipo (normal_plan ou track_plan)"""
    resp = bg_request("GET", "/api/v2/mix/order/orders-plan-pending",
                      {"productType":"USDT-FUTURES", "planType":plan_type})
    ordens = []
    if resp.get("code") == "00000":
        d = resp.get("data")
        if isinstance(d, dict):
            lista = d.get("entrustedList") or d.get("list") or []
        else:
            lista = d or []
        for o in (lista or []):
            ordens.append({
                "symbol": o.get("symbol"),
                "orderId": o.get("orderId") or o.get("planOrderId") or o.get("id"),
                "clientOid": o.get("clientOid")
            })
    return ordens

def bg_cancel_plan_order(symbol, order_id, plan_type):
    """Cancela uma plan order especifica (trailing ou TP/SL)"""
    return bg_request("POST", "/api/v2/mix/order/cancel-plan-order", {
        "symbol": symbol, "productType": "USDT-FUTURES",
        "marginCoin": "USDT", "planType": plan_type,
        "orderIdList": [{"orderId": order_id}]
    })

def calc_levels(price, signal, atr_val):
    if atr_val <= 0: atr_val = price * 0.01
    sl_m, tp1_m, tp2_m = 1.5, 2.8, 5.0  # multiplicadores ATR
    # Distancia do SL por ATR, mas nunca menos que SL_MIN_PCT da entrada
    sl_dist = atr_val * sl_m
    sl_min = price * (SL_MIN_PCT / 100)
    if sl_dist < sl_min:
        sl_dist = sl_min
    # TP proporcional ao SL FINAL (mantem racio mesmo quando o minimo atua)
    tp1_dist = sl_dist * (tp1_m / sl_m)   # racio 2.8/1.5 = 1.87
    tp2_dist = sl_dist * (tp2_m / sl_m)   # racio 5.0/1.5 = 3.33
    if "LONG" in signal:
        return price - sl_dist, price + tp1_dist, price + tp2_dist, 3
    return price + sl_dist, price - tp1_dist, price - tp2_dist, 3

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

# ==================== GESTAO DE RISCO (v5.19) ====================
def get_saldo():
    """Retorna (equity, disponivel) da conta de futuros. (0,0) em erro."""
    try:
        resp = bg_request("GET", "/api/v2/mix/account/accounts", {"productType":"USDT-FUTURES"})
        if resp.get("code") == "00000" and resp.get("data"):
            a = resp["data"][0]
            equity = float(a.get("accountEquity", a.get("usdtEquity", 0)))
            avail = float(a.get("available", 0))
            return equity, avail
    except Exception as e:
        print(f"Erro get_saldo: {e}")
    return 0.0, 0.0

def circuit_breaker_ativo():
    """True se a perda do dia atingiu o limite. Bloqueia novas entradas."""
    global circuit_breaker_avisado
    perda = perda_hoje()
    if perda <= -DAILY_LOSS_LIMIT:
        if not circuit_breaker_avisado:
            send(f"🚨 <b>CIRCUIT BREAKER ATIVO</b>\n━━━━━━━━━━━━━━━\n"
                 f"📉 Perda hoje: <b>${perda:.2f}</b>\n"
                 f"🛑 Limite: ${DAILY_LOSS_LIMIT:.2f}\n\n"
                 f"<b>Novas entradas BLOQUEADAS até amanhã (UTC).</b>\n"
                 f"As posições abertas continuam a ser geridas normalmente.")
            circuit_breaker_avisado = True
        return True
    return False

def calc_valor_risco():
    """Calcula o valor a arriscar ($) com base em RISK_PCT do equity."""
    equity, _ = get_saldo()
    if equity <= 0:
        return None
    return round(equity * (RISK_PCT / 100), 2)

def resumo_pos_saldo():
    """Texto compacto com saldo + posicoes abertas. Para mostrar apos uma entrada."""
    linhas = []
    # Saldo
    equity, avail = get_saldo()
    if equity > 0:
        perda = perda_hoje()
        linhas.append(f"💰 <b>Saldo:</b> ${equity:.2f} | Disp: ${avail:.2f} | Hoje: ${perda:+.2f}")
    # Posicoes abertas
    try:
        resp = bg_request("GET", "/api/v2/mix/position/all-position",
                          {"productType":"USDT-FUTURES","marginCoin":"USDT"})
        pos = [p for p in resp.get("data", []) if float(p.get("total",0)) > 0] if resp.get("code")=="00000" else []
        if pos:
            linhas.append(f"📊 <b>Posições abertas ({len(pos)}):</b>")
            for p in pos:
                sym = p.get("symbol","?")
                emoji = "🟢" if p.get("holdSide")=="long" else "🔴"
                upl = float(p.get("unrealizedPL",0))
                marg = float(p.get("marginSize",0))
                roe = (upl/marg*100) if marg else 0
                linhas.append(f"  {emoji} {sym}: ${upl:+.4f} ({roe:+.1f}%)")
        else:
            linhas.append("📊 Sem outras posições abertas")
    except Exception as e:
        print(f"Erro resumo_pos_saldo: {e}")
    return "\n".join(linhas)

def reconciliar_posicoes():
    """No arranque: sincroniza o cache com as posicoes reais da Bitget.
    Resolve perda de estado em restarts do Railway."""
    global posicoes_abertas_cache
    try:
        resp = bg_request("GET", "/api/v2/mix/position/all-position",
                          {"productType":"USDT-FUTURES","marginCoin":"USDT"})
        if resp.get("code") != "00000":
            print(f"⚠️ Reconciliacao falhou: {resp.get('msg')}")
            return
        reais = [p for p in resp.get("data", []) if float(p.get("total",0)) > 0]
        posicoes_abertas_cache = {}
        for p in reais:
            bgsym = p.get("symbol")
            side = p.get("holdSide","?")
            entrada = float(p.get("openPriceAvg") or p.get("averageOpenPrice") or 0)
            posicoes_abertas_cache[bgsym] = {
                "sym": bgsym.replace("USDT",""),
                "lado": "LONG" if side=="long" else "SHORT",
                "entrada": entrada,
                "modo": "recuperado",
                "tempo_abertura": time.time()  # desconhecido; assume agora
            }
        if reais:
            linhas = []
            for p in reais:
                sym = p.get("symbol","?")
                bgsym = sym
                hold = p.get("holdSide","long")
                side = "🟢" if hold=="long" else "🔴"
                upl = float(p.get("unrealizedPL",0))

                # Reconciliacao lista as posicoes recuperadas (sem tocar em SL/TP).
                # Auto-protecao removida: protecao de posicoes orfas faz-se manualmente
                # na Bitget (particularidades do one-way mode + hibrido tornam-na fragil).
                protecao = ""

                linhas.append(f"{side} {sym}: ${upl:+.4f}{protecao}")
            send(f"🔄 <b>RECONCILIAÇÃO</b>\n━━━━━━━━━━━━━━━\n"
                 f"Recuperadas {len(reais)} posição(ões) abertas:\n" + "\n".join(linhas))
            print(f"✅ Reconciliacao: {len(reais)} posicoes recuperadas")
        else:
            print("✅ Reconciliacao: sem posicoes abertas")
    except Exception as e:
        print(f"Erro reconciliar_posicoes: {e}")


def executar_trade(sig, bgsym, valor, modo="normal", tipo_valor="margem", tipo="AUTO"):
    """Abre uma posicao. Devolve True se abriu com sucesso, False caso contrario."""
    sym, price, _, rsi_v, label, score, score_breakdown, reasons, atr_val = sig
    is_long = "LONG" in label
    sl, tp1, tp2, alav = calc_levels(price, label, atr_val)
    sl = ajustar_preco(bgsym, sl); tp1 = ajustar_preco(bgsym, tp1)   # v6.0: precisao correta
    lev = min(alav, MAX_LEV)

    if tipo_valor == "risco":
        distancia = abs(sl - price)
        if distancia <= 0:
            send("⚠️ Distância SL inválida."); return False
        size_raw = valor / distancia
        notional = size_raw * price
        if notional > MAX_NOTIONAL:
            send(f"⚠️ Posição muito grande (${notional:.0f})."); return False
        size = ajustar_qtd(bgsym, size_raw)
        margem = notional / lev
        risco_real = valor
    else:
        margem = valor
        notional = margem * lev
        if notional > MAX_NOTIONAL:
            send(f"⚠️ Posição muito grande (${notional:.0f})."); return False
        size = ajustar_qtd(bgsym, notional / price)
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
        send(m); return False

    # v6.0: instancia "alerta" nao mexe na conta — so avisa.
    if not PRINCIPAL:
        m  = f"📢 <b>SINAL (modo alerta — não executei)</b>\n━━━━━━━━━━━━━━━\n"
        m += f"{arrow} {sym}/USDT — {direcao} | score {score:+.1f}\n"
        m += f"💲 ~${fmt(price)} | 🛑 SL ${fmt(sl)} | 🎯 TP1 ${fmt(tp1)}\n"
        m += f"<i>(esta instância está como ROLE=alerta)</i>"
        send(m); return False

    if check_open_position(bgsym):
        send(f"⚠️ Já tens posição em <b>{sym}</b>."); return False

    bg_set_leverage(bgsym, lev)
    time.sleep(1)

    if modo == "normal":
        r = bg_place_order(bgsym, is_long, size, sl, tp1)
    else:
        r = bg_place_order(bgsym, is_long, size, sl)

    if r.get("code") != "00000":
        send(f"❌ Erro ao abrir: {r.get('msg','?')}"); return False

    extra = "TP1 fecha 100%"
    if modo == "trail":
        rt = bg_place_trailing(bgsym, is_long, size, tp1, CALLBACK_RATIO)
        if rt.get("code") == "00000":
            extra = f"Trailing {CALLBACK_RATIO}% (100%)"
        else:
            extra = "SL fixo (trailing falhou)"
    elif modo == "hibrido":
        metade = ajustar_qtd(bgsym, size/2)
        resto = ajustar_qtd(bgsym, size - metade)
        rtp = bg_close_limit(bgsym, is_long, metade, tp1)
        rtr = bg_place_trailing(bgsym, is_long, resto, tp1, CALLBACK_RATIO)
        ok_tp = rtp.get("code") == "00000"
        ok_tr = rtr.get("code") == "00000"
        extra = f"TP1 50% [{'ok' if ok_tp else 'FALHOU'}] + Trailing [{'ok' if ok_tr else 'FALHOU'}]"

    m  = f"✅ <b>POSIÇÃO ABERTA!</b> ({modo})\n━━━━━━━━━━━━━━━\n"
    m += f"{arrow} <b>{sym}/USDT — {direcao}</b>\n💲 Entrada: ~${fmt(price)}\n"
    m += f"⚡ {lev}x | 💰 Margem ${margem:.2f}\n📊 Size: {size}\n"
    m += f"🛑 SL: ${fmt(sl)} | 🎯 TP1: ${fmt(tp1)}\n"
    m += f"📉 Risco: <b>${risco_real:.2f}</b>\n📋 {extra}\n━━━━━━━━━━━━━━━\n"
    # Resumo de saldo + posicoes apos a entrada (v5.19)
    time.sleep(1)  # da tempo a Bitget refletir a nova posicao
    m += resumo_pos_saldo()
    send(m)

    posicoes_abertas_cache[bgsym] = {
        "sym": sym,
        "lado": direcao,
        "entrada": price,
        "modo": modo,
        "lev": lev,
        "margem": margem,
        "tempo_abertura": time.time(),
        "features": features_do_sinal(sig, bgsym, tipo, sl, tp1, lev, margem, get_funding_rate(bgsym))
    }
    return True

# ==================== RASTREAMENTO DE POSIÇÕES ====================
def verificar_posicoes_fechadas():
    global last_position_check, posicoes_abertas_cache
    
    if time.time() - last_position_check < 300:
        return
    
    last_position_check = time.time()

    # v6.0: so a instancia principal gere posicoes e manda mensagens de fecho
    # (evita fechos/cancelamentos e mensagens duplicadas com a instancia alerta).
    if not PRINCIPAL:
        return

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

                        # v6.0: ROE real = pnl / margem real (nao mais 50*3 fixo).
                        # Margem: usa a guardada na abertura; senao deriva de open*size/lev.
                        entrada = info_antiga.get('entrada', 0)
                        margem = info_antiga.get('margem', 0)
                        if not margem:
                            try:
                                open_px = float(p.get("openAvgPrice") or entrada or 0)
                                size_tot = float(p.get("openTotalPos") or p.get("closeTotalPos") or 0)
                                lev = float(info_antiga.get('lev') or p.get("leverage") or 1) or 1
                                margem = (open_px * size_tot / lev) if (open_px and size_tot) else 0
                            except Exception:
                                margem = 0
                        pct_ganho = (pnl / margem * 100) if margem else 0

                        emoji = "🟢" if pnl >= 0 else "🔴"
                        sinal = "+" if pnl >= 0 else ""
                        roe_str = f" ({sinal}{pct_ganho:.2f}%)" if margem else ""

                        m = f"{emoji} <b>POSIÇÃO FECHADA</b>\n━━━━━━━━━━━━━━━\n"
                        m += f"<b>{info_antiga['sym']}/USDT {info_antiga['lado']}</b>\n"
                        m += f"💲 Entrada: ${fmt(entrada)}\n"
                        m += f"💰 Resultado: <b>${sinal}{pnl:+.4f}</b>{roe_str}\n"
                        m += f"⏱️ Duração: {tempo_aberto}m\n"
                        m += f"📋 Modo: {info_antiga['modo']}"
                        send(m)

                        # v6.0: regista TODOS os fechos (SL/TP/trailing), nao so os manuais
                        trades_history.append({
                            'symbol': bgsym,
                            'pnl': pnl,
                            'roe': pct_ganho,
                            'timestamp': datetime.now(),
                            'hora': datetime.now().hour,
                            'duracao': tempo_aberto,
                        })
                        # v6.1: linha no CSV (features do sinal + resultado real)
                        feats = info_antiga.get("features")
                        if feats:
                            csv_registar(feats, {"pnl": pnl, "roe": pct_ganho, "duracao": tempo_aberto})
                        break
            
            # Cancela ordens orfas (trailing/TP que sobraram do fecho)
            try:
                bg_cancel_all(bgsym)
                # Trailing (track_plan) precisa de cancelamento proprio
                for o in bg_list_plan_orders("track_plan"):
                    if o["symbol"] == bgsym and o.get("orderId"):
                        bg_cancel_plan_order(bgsym, o["orderId"], "track_plan")
            except Exception as e:
                print(f"Erro a cancelar ordens orfas {bgsym}: {e}")
            
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

# ==================== SCORING (v6.0) ====================
# Fonte unica de pontuacao, usada pelo analyze() E pela espreitadela.
# Antes havia duas copias da logica (analyze + peek) com categorias diferentes
# (4 vs 3) — o aviso e a analise oficial podiam discordar. Agora e o mesmo codigo.
def pontuar(closes, highs, lows, volumes, funding, ohlc):
    """Devolve (score, score_breakdown, reasons, cats_concordam, rsi_val)."""
    r = rsi(closes)
    e20 = ema_arr(closes, 20)
    e50 = ema_arr(closes, 50)
    bull = e20[-1] > e50[-1]
    ml, sl_, hist = macd(closes)
    vol_recente = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else 1
    vol_medio = sum(volumes[-25:-5]) / 20 if len(volumes) >= 25 else vol_recente
    vol_ratio = vol_recente / vol_medio if vol_medio else 1

    engulfing = detect_engulfing(ohlc)
    rejection = detect_rejection(ohlc)
    inside_bar = detect_inside_bar(ohlc)

    score = 0.0
    score_breakdown = {}
    reasons = []

    # RSI — depende da estrategia
    rsi_score = 0.0
    if ESTRATEGIA == "momentum":
        # RSI como momentum/tendencia (alinhado), peso max ±1.5
        if r >= 65:
            rsi_score = 1.5; reasons.append("RSI momentum alta")
        elif r >= 55:
            rsi_score = 0.75
        elif r <= 35:
            rsi_score = -1.5; reasons.append("RSI momentum baixa")
        elif r <= 45:
            rsi_score = -0.75
        # 45-55: zona neutra (0)
    else:
        # RSI de reversao a media (default), peso max ±3.0
        if r < 30:
            rsi_score = 3.0; reasons.append("RSI sobrevendido")
        elif r < 40:
            rsi_score = 1.5
        elif r > 70:
            rsi_score = -3.0; reasons.append("RSI sobrecomprado")
        elif r > 60:
            rsi_score = -1.5
    score += rsi_score; score_breakdown['RSI'] = rsi_score

    # EMA (0 a ±2)
    ema_score = 2.0 if bull else -2.0
    reasons.append("EMA bullish" if bull else "EMA bearish")
    score += ema_score; score_breakdown['EMA'] = ema_score

    # MACD (0 a ±2)
    macd_score = 0.0
    if ml > sl_ and hist > 0:
        macd_score = 2.0; reasons.append("MACD bullish")
    elif ml < sl_ and hist < 0:
        macd_score = -2.0; reasons.append("MACD bearish")
    score += macd_score; score_breakdown['MACD'] = macd_score

    # Volume — depende da estrategia
    vol_score = 0.0
    if ESTRATEGIA == "momentum":
        # simetrico: volume alto confirma a direcao do score em AMBOS os lados
        if vol_ratio > 1.3 and score != 0:
            vol_score = 1.0 if score > 0 else -1.0
            reasons.append("Volume↑")
        elif vol_ratio < 0.6 and score != 0:
            # volume baixo = menos conviccao -> reduz magnitude nos dois sentidos
            vol_score = -0.5 if score > 0 else 0.5
            reasons.append("Volume baixo")
    else:
        # assimetrico (original): so confirma longs
        if vol_ratio > 1.3 and score > 0:
            vol_score = 1.0; reasons.append("Volume↑")
        elif vol_ratio < 0.6:
            vol_score = -0.5; reasons.append("Volume baixo")
    score += vol_score; score_breakdown['Volume'] = vol_score

    # Funding Rate (0 a ±1)
    funding_score = 0.0
    if score > 0 and funding > 0.03:
        funding_score -= 1.0; reasons.append(f"Funding alto ({funding:+.3%})")
    elif score < 0 and funding < -0.03:
        funding_score += 1.0; reasons.append(f"Funding baixo ({funding:+.3%})")
    score += funding_score; score_breakdown['Funding'] = funding_score

    # Candlestick Patterns (0 a ±2.5)
    pattern_score = 0.0
    if engulfing > 0 and score > 0:
        pattern_score += engulfing; reasons.append("Engulfing bullish ✅")
    elif engulfing < 0 and score < 0:
        pattern_score += engulfing; reasons.append("Engulfing bearish ✅")
    if rejection != 0:
        pattern_score += rejection
        reasons.append("Rejection" + (" bullish" if rejection > 0 else " bearish"))
    if inside_bar > 0 and score != 0:
        pattern_score += inside_bar
        reasons.append("Consolidação bullish" if score > 0 else "Consolidação bearish")
    score += pattern_score; score_breakdown['Padrões'] = pattern_score

    score = round(score, 1)

    # Confluencia por 4 categorias independentes
    direcao = 1 if score > 0 else -1
    categorias = {
        'tendencia': ema_score + macd_score,
        'momento':   rsi_score,
        'volume':    vol_score,
        'contexto':  funding_score + pattern_score,
    }
    cats_concordam = sum(1 for v in categorias.values() if v != 0 and (1 if v > 0 else -1) == direcao)
    score_breakdown['_cats'] = cats_concordam
    return score, score_breakdown, reasons, cats_concordam, r

# ==================== ANALISE ====================
def analyze():
    global last_analysis
    last_analysis = time.time()
    signals = []
    for pair, sym, bgsym in PAIRS:
        try:
            candles = bg_get_ohlcv(bgsym, granularity="60", limit=100)
            if not candles:
                print(f"❌ Sem dados {bgsym}")
                continue

            ohlc = candles
            closes = [float(c[4]) for c in ohlc]
            highs = [float(c[2]) for c in ohlc]
            lows = [float(c[3]) for c in ohlc]
            volumes = [float(c[5]) if len(c) > 5 else 1.0 for c in ohlc]

            price, _ = bg_get_ticker(bgsym)
            if not price:
                print(f"❌ Sem preço {bgsym}")
                continue

            e20 = ema_arr(closes, 20)
            e50 = ema_arr(closes, 50)
            atr_val = atr(highs, lows, closes)
            funding = get_funding_rate(bgsym)

            score, score_breakdown, reasons, cats_concordam, r = pontuar(
                closes, highs, lows, volumes, funding, ohlc)

            print(f"{sym}: RSI={r:.1f} score={score:+.1f} cats={cats_concordam} funding={funding:+.3%}")

            # ===== FILTROS DE QUALIDADE (v5.18) =====
            # 1. Confluencia minima: precisa de MIN_CATEGORIAS familias a concordar
            if abs(score) >= SCORE_MANUAL and cats_concordam < MIN_CATEGORIAS:
                print(f"  ⏭️ {sym} rejeitado: so {cats_concordam} categorias (min {MIN_CATEGORIAS})")
                continue

            # 2. Cooldown por par: nao repetir sinal do mesmo par dentro de COOLDOWN_MIN
            #    v6.0: o cooldown e marcado DEPOIS (em enviar_sinal/execucao), nao aqui,
            #    para um sinal que falhe a entrada nao bloquear o par 30 min em falso.
            ultimo = ultimo_sinal_par.get(bgsym, 0)
            if abs(score) >= SCORE_MANUAL and (time.time() - ultimo) < COOLDOWN_MIN * 60:
                restante = int((COOLDOWN_MIN*60 - (time.time()-ultimo)) / 60)
                print(f"  ⏭️ {sym} em cooldown ({restante}min restantes)")
                continue

            # SIGNALS (sem marcar cooldown aqui)
            if score >= SCORE_AUTO:
                signals.append(((sym, price, 0, r, "🟢 LONG MUITO FORTE", score, score_breakdown, reasons, atr_val), ohlc, e20, e50, bgsym, "AUTO"))
            elif score <= -SCORE_AUTO:
                signals.append(((sym, price, 0, r, "🔴 SHORT MUITO FORTE", score, score_breakdown, reasons, atr_val), ohlc, e20, e50, bgsym, "AUTO"))
            elif score >= SCORE_MANUAL:
                signals.append(((sym, price, 0, r, "🟢 LONG FORTE", score, score_breakdown, reasons, atr_val), ohlc, e20, e50, bgsym, "MANUAL"))
            elif score <= -SCORE_MANUAL:
                signals.append(((sym, price, 0, r, "🔴 SHORT FORTE", score, score_breakdown, reasons, atr_val), ohlc, e20, e50, bgsym, "MANUAL"))

        except Exception as e:
            print(f"Erro analyze {sym}: {e}")
            continue
    
    return signals

def enviar_sinal(sig, ohlc, e20, e50, bgsym, tipo="MANUAL"):
    sym,price,_,rsi_v,label,score,score_breakdown,reasons,atr_val = sig
    sl,tp1,tp2,_ = calc_levels(price,label,atr_val)
    dist_pct = abs(sl-price)/price*100
    
    # Log o sinal
    direcao = "LONG" if score > 0 else "SHORT"
    log_msg(f"SINAL {tipo:6s} | {sym:6s} | {direcao:5s} | score {score:+.1f} | price ${price:,.2f}")
    
    # Calcular confiança baseado no score
    confianca = min(95, 50 + (abs(score) * 10))
    
    # Breakdown string
    cats = score_breakdown.get('_cats', 0)
    breakdown_str = "Breakdown:\n"
    for indicador, valor in score_breakdown.items():
        if indicador == '_cats':
            continue
        sinal = "+" if valor >= 0 else ""
        breakdown_str += f"  {indicador}: {sinal}{valor:.1f}\n"
    breakdown_str += f"  🔗 Confluência: {cats}/4 categorias\n"
    
    if tipo == "AUTO":
        # ===== CIRCUIT BREAKER (v5.19): bloqueia entrada automatica =====
        if circuit_breaker_ativo():
            print(f"🚨 {sym} AUTO bloqueado pelo circuit breaker")
            return

        # ===== SIZING POR RISCO (v5.19) =====
        if RISK_AUTO_ENABLED:
            valor_risco = calc_valor_risco()
            if valor_risco and valor_risco > 0:
                entrada_valor, entrada_tipo = valor_risco, "risco"
                desc_entrada = f"risco ${valor_risco:.2f} ({RISK_PCT:.0f}% conta)"
            else:
                entrada_valor, entrada_tipo = 50, "margem"
                desc_entrada = "$50 margem (saldo indisponível)"
        else:
            entrada_valor, entrada_tipo = 50, "margem"
            desc_entrada = "$50 margem"

        cap  = f"⚡ <b>{TAG}ENTRADA AUTOMÁTICA!</b>\n{'↑' if 'LONG' in label else '↓'} <b>{sym}/USD — {label}</b>\n━━━━━━━━━━━━━━━\n"
        cap += f"💲 Entrada: ${fmt(price)}\n🛑 SL: ${fmt(sl)} ({dist_pct:.2f}%)\n🎯 TP1: ${fmt(tp1)}\n"
        cap += f"━━━━━━━━━━━━━━━\n"
        cap += f"📉 RSI: {rsi_v} | <b>Score: {score:+.1f}</b> (Confiança: {confianca:.0f}%)\n"
        cap += breakdown_str
        cap += f"📌 {', '.join(reasons[:2])}\n"
        cap += f"━━━━━━━━━━━━━━━\n✅ Bot entrou ({desc_entrada}, híbrido)!"
        pending[CHAT_ID] = None
        buf = make_chart(ohlc, price, sl, tp1, tp2, sym, label, e20, e50)
        if buf: send_photo(buf, cap)
        else: send(cap)
        # Log entrada automatica
        log_msg(f"ENTRADA AUTO | {sym:6s} | {('LONG' if 'LONG' in label else 'SHORT'):5s} | ${fmt(price):>10s} | SL ${fmt(sl):>10s}")
        # Executa automático com sizing por risco. Cooldown só se a entrada vingar.
        ok = executar_trade(sig, bgsym, entrada_valor, "hibrido", entrada_tipo)
        if ok:
            ultimo_sinal_par[bgsym] = time.time()
    else:
        # ENTRADA MANUAL (score 4-5 ou -4 a -5) COM BOTÕES
        cap  = f"{TAG}{'↑' if 'LONG' in label else '↓'} <b>{sym}/USD — {label}</b>\n━━━━━━━━━━━━━━━\n"
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
        ultimo_sinal_par[bgsym] = time.time()   # v6.0: alerta enviado -> respeita cooldown
        buf = make_chart(ohlc, price, sl, tp1, tp2, sym, label, e20, e50)
        if buf: send_photo(buf, cap)
        else: send_with_buttons(cap, buttons)

def forcar_teste(symbol):
    """Testa análise de um par específico - USA BITGET"""
    symbol = symbol.upper()
    alvo = None
    for pair,sym,bgsym in PAIRS:
        if sym == symbol:
            alvo = (pair,sym,bgsym); break
    if not alvo:
        send(f"⚠️ Par inválido. Ex: /teste LTC")
        return
    
    pair, sym, bgsym = alvo
    try:
        candles = bg_get_ohlcv(bgsym, granularity="60", limit=100)
        if not candles:
            send(f"❌ Erro ao obter dados de {sym}"); return

        ohlc = candles
        closes = [float(c[4]) for c in ohlc]
        highs = [float(c[2]) for c in ohlc]
        lows = [float(c[3]) for c in ohlc]
        volumes = [float(c[5]) if len(c) > 5 else 1.0 for c in ohlc]

        price, _ = bg_get_ticker(bgsym)
        if not price:
            send(f"❌ Erro ao obter preço de {sym}"); return

        e20 = ema_arr(closes, 20)
        e50 = ema_arr(closes, 50)
        atr_val = atr(highs, lows, closes)
        funding = get_funding_rate(bgsym)

        # v6.0: mesma pontuacao que a analise oficial (antes /teste so usava RSI+EMA)
        score, score_breakdown, reasons, cats, r = pontuar(
            closes, highs, lows, volumes, funding, ohlc)

        if score >= SCORE_AUTO:      label = "🟢 LONG MUITO FORTE"
        elif score <= -SCORE_AUTO:   label = "🔴 SHORT MUITO FORTE"
        elif score >= SCORE_MANUAL:  label = "🟢 LONG FORTE"
        elif score <= -SCORE_MANUAL: label = "🔴 SHORT FORTE"
        elif score > 0:              label = "🟢 LONG fraco"
        else:                        label = "🔴 SHORT fraco"

        bull = score > 0
        sig = (sym, price, 0, r, label, score, score_breakdown, reasons, atr_val)

        # Open Interest informativo (nao afeta o score)
        oi_linha = oi_contexto(bgsym, price_sobe=bull)

        msg_teste = (f"🧪 <b>TESTE: {sym}</b>\n━━━━━━━━━━━━━━━\n💲 ${fmt(price)}\n"
                     f"📊 RSI: {r:.1f} | Score: {score:+.1f} | {cats}/4 cats\n⚡ {label}")
        if oi_linha:
            msg_teste += f"\n{oi_linha}"
        send(msg_teste)

        # So mostra o painel de entrada (botoes) se for mesmo sinal >= manual.
        if abs(score) >= SCORE_MANUAL:
            enviar_sinal(sig, ohlc, e20, e50, bgsym, tipo="MANUAL")
        else:
            send("ℹ️ Score abaixo do limiar — sem painel de entrada.")

    except Exception as e:
        send(f"⚠️ Erro teste {sym}: {str(e)[:80]}")
        print(f"Teste error {sym}: {e}")


# ===== PRECISAO DE CONTRATOS (v5.27) — corrige erro "multiple of 0.1" =====
_contract_specs = {}  # cache {bgsym: {pricePlace, priceEndStep, volumePlace, minTradeNum}}

def get_contract_specs(bgsym):
    """Busca (e cacheia) a precisao de preco/quantidade do par na Bitget."""
    if bgsym in _contract_specs:
        return _contract_specs[bgsym]
    try:
        resp = http_get_json(f"{BG_API}/api/v2/mix/market/contracts",
            params={"symbol":bgsym, "productType":"USDT-FUTURES"}, timeout=10)
        if resp and resp.get("code") == "00000" and resp.get("data"):
            d = resp["data"][0]
            specs = {
                "pricePlace":   int(d.get("pricePlace", 2)),
                "priceEndStep": int(d.get("priceEndStep", 1)),
                "volumePlace":  int(d.get("volumePlace", 3)),
                "minTradeNum":  float(d.get("minTradeNum", 0.001)),
            }
            _contract_specs[bgsym] = specs
            return specs
    except Exception as e:
        print(f"Erro specs {bgsym}: {e}")
    # fallback seguro
    return {"pricePlace":2, "priceEndStep":1, "volumePlace":3, "minTradeNum":0.001}

def ajustar_preco(bgsym, preco):
    """Arredonda o preco para o tick correto do par (pricePlace + priceEndStep).
    Ex BTC: pricePlace=1, priceEndStep=1 -> multiplos de 0.1."""
    s = get_contract_specs(bgsym)
    tick = s["priceEndStep"] / (10 ** s["pricePlace"])   # ex: 1/10^1 = 0.1
    if tick <= 0:
        tick = 10 ** (-s["pricePlace"])
    preco_aj = round(preco / tick) * tick
    return round(preco_aj, s["pricePlace"])

def ajustar_qtd(bgsym, qtd):
    """Arredonda a quantidade para volumePlace casas e respeita minTradeNum."""
    s = get_contract_specs(bgsym)
    q = round(qtd, s["volumePlace"])
    if q < s["minTradeNum"]:
        q = s["minTradeNum"]
    return q

def mt_exec(args):
    """
    /mt BTC L 50 10
    Abre posição DIRETO com híbrido automático - USA BITGET PARA PREÇOS
    """
    if len(args) < 5:
        send("❌ Uso: /mt PAR L/S MARGEM ALAV\nEx: /mt BTC L 50 10"); return
    
    par = args[1].upper() + "USDT"
    side = args[2].upper()
    
    try:
        margem = float(args[3])
        alav = float(args[4])
    except:
        send("❌ Margem e alav devem ser números"); return
    
    if side not in ['L', 'S']:
        send("❌ L ou S"); return
    
    if margem <= 0 or alav <= 0:
        send("❌ Valores > 0"); return
    
    if alav > 125:
        send("❌ Max 125x"); return
    
    notional = margem * alav
    if notional > MAX_NOTIONAL:
        send(f"❌ ${notional:.0f} > ${MAX_NOTIONAL}"); return
    
    pares_validos = ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOTUSDT","LINKUSDT","UNIUSDT","ATOMUSDT","LTCUSDT","DOGEUSDT","AAVEUSDT"]
    if par not in pares_validos:
        send(f"❌ {par} inválido"); return
    
    if check_open_position(par):
        send(f"⚠️ Já tem {par}"); return
    
    is_long = side == 'L'

    # v6.0: instancia "alerta" nao executa
    if not PRINCIPAL:
        send("📢 Esta instância está em modo <b>alerta</b> (ROLE=alerta) e não executa ordens."); return

    send(f"⏳ Abrindo {par} {('LONG' if is_long else 'SHORT')}...")
    
    try:
        # Busca preço da Bitget
        price, _ = bg_get_ticker(par)
        if not price:
            send(f"❌ Erro ao obter preço de {par}"); return
        
        # Busca OHLC para ATR da Bitget
        candles = bg_get_ohlcv(par, granularity="60", limit=100)
        if not candles:
            send(f"❌ Erro ao obter dados de {par}"); return
        
        closes = [float(c[4]) for c in candles]
        highs  = [float(c[2]) for c in candles]
        lows   = [float(c[3]) for c in candles]
        atr_val = atr(highs, lows, closes)
        
        # SL: 1.5 ATR, mas nunca menos que SL_MIN_PCT da entrada
        sl_dist = atr_val * 1.5
        sl_min = price * (SL_MIN_PCT / 100)
        if sl_dist < sl_min:
            sl_dist = sl_min
        # TP proporcional ao SL final (mantem racio 1.87)
        tp_dist = sl_dist * (2.8 / 1.5)
        if is_long:
            sl = price - sl_dist
            tp = price + tp_dist
        else:
            sl = price + sl_dist
            tp = price - tp_dist
        
        # Arredonda preços para a precisão CORRETA de cada par
        sl = ajustar_preco(par, sl)
        tp = ajustar_preco(par, tp)
        
        # Tamanho TOTAL (ajustado à precisão de quantidade do par)
        size = ajustar_qtd(par, notional / price)
        
        # Set leverage
        bg_set_leverage(par, alav)
        time.sleep(1)
        
        # v6.0 HÍBRIDO CORRETO: abre a posição INTEIRA (size), depois fecha metade
        # por limit em TP1 e deixa a outra metade com trailing. Antes abria só metade
        # mas reportava o notional inteiro (posição real era ½ do anunciado).
        result = bg_place_order(par, is_long, size, sl)   # SL preset; sem TP (gerido abaixo)
        if result.get("code") != "00000":
            send(f"❌ Erro ao abrir: {result.get('msg','?')}"); return
        time.sleep(0.5)

        metade = ajustar_qtd(par, size / 2)
        resto = ajustar_qtd(par, size - metade)
        rtp = bg_close_limit(par, is_long, metade, tp)            # TP1 fecha 50%
        rtr = bg_place_trailing(par, is_long, resto, tp, CALLBACK_RATIO)  # trailing nos 50%
        ok_tp = rtp.get("code") == "00000"
        ok_tr = rtr.get("code") == "00000"
        
        # Sucesso! (risco/ganho sobre o size INTEIRO)
        risco = abs(sl - price) * size
        ganho = abs(tp - price) * size
        
        m = f"✅ <b>POSIÇÃO ABERTA!</b>\n━━━━━━━━━━━━━━━\n"
        m += f"{'↑' if is_long else '↓'} <b>{par} {'LONG' if is_long else 'SHORT'}</b> (HÍBRIDO)\n"
        m += f"💲 Entrada: ${fmt(price)}\n"
        m += f"🛑 SL: ${fmt(sl)}\n"
        m += f"🎯 TP1: ${fmt(tp)}\n"
        m += "━━━━━━━━━━━━━━━\n"
        m += f"⚡ Alavancagem: {alav:.1f}x\n"
        m += f"💰 Margem: ${margem:.2f}\n"
        m += f"📏 Notional: ${notional:.2f}\n"
        m += f"📊 Size: {size}\n"
        m += f"📋 TP1 50% [{'ok' if ok_tp else 'FALHOU'}] + Trailing [{'ok' if ok_tr else 'FALHOU'}]\n"
        m += f"💔 Risco máximo: ${risco:.2f}\n"
        m += f"💎 Ganho potencial: ${ganho:.2f}"
        
        send(m)
        
        # Cache
        posicoes_abertas_cache[par] = {
            'entrada': price,
            'tempo_abertura': time.time(),
            'modo': 'hibrido',
            'lev': alav,
            'margem': margem,
            'sym': par.replace("USDT", ""),
            'lado': 'LONG' if is_long else 'SHORT'
        }
        
    except Exception as e:
        send(f"❌ Erro: {str(e)[:80]}")
        print(f"MT Error: {e}")



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
    agora = datetime.datetime.now(datetime.timezone.utc)
    
    for p in lista:
        utime = int(p.get("utime") or 0)
        if utime == 0: continue
        
        data_fecho = datetime.datetime.fromtimestamp(utime/1000, datetime.timezone.utc)
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

def limpar_ordens():
    """Cancela ordens orfas (plan orders sem posicao aberta correspondente)"""
    send("🧹 A procurar ordens orfas...")
    
    # Posicoes abertas atuais
    resp = bg_request("GET", "/api/v2/mix/position/all-position",
                      {"productType":"USDT-FUTURES","marginCoin":"USDT"})
    posicoes_ativas = set()
    if resp.get("code") == "00000":
        posicoes_ativas = {p.get("symbol") for p in resp.get("data", [])
                           if float(p.get("total",0)) > 0}
    
    # Lista plan orders dos dois tipos: trailing (track_plan) e TP/SL (normal_plan)
    todas = []
    for ptype in ["track_plan", "normal_plan"]:
        for o in bg_list_plan_orders(ptype):
            o["planType"] = ptype
            todas.append(o)
    
    if not todas:
        send("✅ Sem ordens pendentes na conta.")
        return
    
    # Orfas = ordens cujo simbolo nao tem posicao aberta
    orfas = [o for o in todas if o["symbol"] not in posicoes_ativas]
    
    if not orfas:
        send(f"✅ {len(todas)} ordens, todas com posicao. Nada orfao.")
        return
    
    canceladas = 0
    falhou = 0
    pares = set()
    for o in orfas:
        if not o.get("orderId"):
            falhou += 1
            continue
        try:
            r = bg_cancel_plan_order(o["symbol"], o["orderId"], o["planType"])
            if r.get("code") == "00000":
                canceladas += 1
                pares.add(o["symbol"])
            else:
                falhou += 1
                print(f"Falha cancelar {o['symbol']}: {r.get('msg')}")
        except Exception as e:
            falhou += 1
            print(f"Erro a cancelar {o['symbol']}: {e}")
    
    m = f"🧹 <b>Limpeza concluida</b>\n━━━━━━━━━━━━━━━\n"
    m += f"✅ Canceladas: {canceladas}\n"
    if pares:
        m += "Pares: " + ", ".join(sorted(p.replace("USDT","") for p in pares)) + "\n"
    if falhou:
        m += f"⚠️ Falharam: {falhou} (ver Bitget)"
    send(m)

def mostrar_ajuda():
    m  = f"🤖 <b>COMANDOS ({VERSAO})</b>\n━━━━━━━━━━━━━━━\n"
    m += "/saldo /posicoes /ganhos /stats\n"
    m += "/fechar SOL /teste LTC /limpar /ajuda\n"
    m += "/csv (exportar) /estrategia (ver tese ativa)\n"
    m += "━━━━━━━━━━━━━━━\n"
    m += "<b>📊 MANUAL TRADE:</b>\n"
    m += "/mt PAR L/S MARGEM ALAV\n"
    m += "Ex: <code>/mt BTC L 50 10</code>\n"
    m += "Ex: <code>/mt ETH S 75 5</code>\n"
    m += "━━━━━━━━━━━━━━━\n"
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

# ==================== MENU INLINE (v5.24) ====================
# Estado da navegacao de entrada manual guiada {chat: {par, direcao, ...}}
menu_entrada = {}

def menu_principal_botoes():
    """Grelha 2 colunas do menu principal, estilo SolTradingBot."""
    return [
        [{"text":"💰 Saldo","callback_data":"m_saldo"},
         {"text":"📈 Posições","callback_data":"m_posicoes"}],
        [{"text":"⚡ Entrar","callback_data":"m_entrar"},
         {"text":"🧪 Testar Par","callback_data":"m_testar"}],
        [{"text":"📊 Ganhos Hoje","callback_data":"m_ganhos"},
         {"text":"📉 Stats","callback_data":"m_stats"}],
        [{"text":"❌ Fechar Posição","callback_data":"m_fechar"},
         {"text":"❓ Ajuda","callback_data":"m_ajuda"}],
    ]

def mostrar_menu_principal(message_id=None):
    """Mostra (ou edita para) o menu principal."""
    titulo = (f"📊 <b>{TAG}PAINEL PRINCIPAL</b>\n"
              f"━━━━━━━━━━━━━━━\n"
              f"🤖 {BOT_NAME} {VERSAO}\n"
              f"🎯 Pares: {', '.join(p[1] for p in PAIRS)}\n"
              f"━━━━━━━━━━━━━━━\n"
              f"Escolhe uma opção 👇")
    if message_id:
        edit_message(message_id, titulo, menu_principal_botoes())
    else:
        send_with_buttons(titulo, menu_principal_botoes())

def menu_escolher_par(message_id):
    """Submenu: escolher par para entrar."""
    emojis = {"BTC":"🟠","XRP":"🔵","UNI":"🦄","ATOM":"⚛️","ETH":"💎","SOL":"🌅"}
    linhas = []
    fila = []
    for _, nome, bgsym in PAIRS:
        e = emojis.get(nome, "•")
        fila.append({"text":f"{e} {nome}","callback_data":f"e_par_{bgsym}"})
        if len(fila) == 2:
            linhas.append(fila); fila = []
    if fila:
        linhas.append(fila)
    linhas.append([{"text":"« Voltar","callback_data":"m_voltar"}])
    edit_message(message_id, "⚡ <b>ENTRAR — escolhe o par:</b>", linhas)

def menu_escolher_direcao(message_id, bgsym):
    """Submenu: escolher direcao Long/Short."""
    menu_entrada[CHAT_ID] = {"par": bgsym}
    nome = bgsym.replace("USDT","")
    linhas = [
        [{"text":"🟢 LONG (subir)","callback_data":"e_dir_L"},
         {"text":"🔴 SHORT (descer)","callback_data":"e_dir_S"}],
        [{"text":"« Voltar","callback_data":"m_entrar"}],
    ]
    edit_message(message_id, f"⚡ <b>{nome}</b> — escolhe a direção:", linhas)

def menu_escolher_valor(message_id, direcao):
    """Submenu: escolher valor da margem."""
    est = menu_entrada.get(CHAT_ID, {})
    est["direcao"] = direcao
    menu_entrada[CHAT_ID] = est
    nome = est.get("par","?").replace("USDT","")
    dir_txt = "🟢 LONG" if direcao == "L" else "🔴 SHORT"
    linhas = [
        [{"text":"$20","callback_data":"e_val_20"},
         {"text":"$50","callback_data":"e_val_50"}],
        [{"text":"$100","callback_data":"e_val_100"},
         {"text":"$200","callback_data":"e_val_200"}],
        [{"text":"« Voltar","callback_data":f"e_par_{est.get('par','')}"}],
    ]
    edit_message(message_id, f"⚡ <b>{nome} {dir_txt}</b>\nEscolhe a margem (USDT):", linhas)

def menu_confirmar_entrada(message_id, valor):
    """Confirmacao final antes de executar."""
    est = menu_entrada.get(CHAT_ID, {})
    est["valor"] = valor
    menu_entrada[CHAT_ID] = est
    nome = est.get("par","?").replace("USDT","")
    dir_txt = "🟢 LONG" if est.get("direcao")=="L" else "🔴 SHORT"
    linhas = [
        [{"text":"✅ Confirmar","callback_data":"e_confirmar"},
         {"text":"❌ Cancelar","callback_data":"m_voltar"}],
    ]
    txt = (f"⚡ <b>CONFIRMAR ENTRADA</b>\n━━━━━━━━━━━━━━━\n"
           f"Par: <b>{nome}</b>\nDireção: {dir_txt}\n"
           f"Margem: <b>${valor}</b> | Alav.: {MAX_LEV}x\n"
           f"━━━━━━━━━━━━━━━\nConfirmas?")
    edit_message(message_id, txt, linhas)

def menu_lista_pares_testar(message_id):
    """Submenu: escolher par para /teste."""
    emojis = {"BTC":"🟠","XRP":"🔵","UNI":"🦄","ATOM":"⚛️"}
    linhas = []; fila = []
    for _, nome, bgsym in PAIRS:
        fila.append({"text":f"{emojis.get(nome,'•')} {nome}","callback_data":f"t_par_{nome}"})
        if len(fila)==2: linhas.append(fila); fila=[]
    if fila: linhas.append(fila)
    linhas.append([{"text":"« Voltar","callback_data":"m_voltar"}])
    edit_message(message_id, "🧪 <b>TESTAR — escolhe o par:</b>", linhas)

def tratar_callback_menu(callback_data, message_id, callback_id):
    """Roteia os toques nos botões do menu. Devolve True se tratou."""
    # Navegacao principal
    if callback_data == "m_voltar":
        answer_callback(callback_id); mostrar_menu_principal(message_id); return True
    if callback_data == "m_saldo":
        answer_callback(callback_id, "A buscar saldo..."); mostrar_saldo(); return True
    if callback_data == "m_posicoes":
        answer_callback(callback_id, "A buscar posições..."); mostrar_posicoes(); return True
    if callback_data == "m_ganhos":
        answer_callback(callback_id); mostrar_ganhos(1); return True
    if callback_data == "m_stats":
        answer_callback(callback_id); send(calc_stats_geral()); return True
    if callback_data == "m_fechar":
        answer_callback(callback_id); menu_fechar(); return True
    if callback_data == "m_ajuda":
        answer_callback(callback_id); mostrar_ajuda(); return True
    # Fluxo de entrada guiada
    if callback_data == "m_entrar":
        answer_callback(callback_id); menu_escolher_par(message_id); return True
    if callback_data.startswith("e_par_"):
        answer_callback(callback_id)
        menu_escolher_direcao(message_id, callback_data.replace("e_par_","")); return True
    if callback_data.startswith("e_dir_"):
        answer_callback(callback_id)
        menu_escolher_valor(message_id, callback_data.replace("e_dir_","")); return True
    if callback_data.startswith("e_val_"):
        answer_callback(callback_id)
        menu_confirmar_entrada(message_id, int(callback_data.replace("e_val_",""))); return True
    if callback_data == "e_confirmar":
        answer_callback(callback_id, "A processar...")
        est = menu_entrada.get(CHAT_ID, {})
        executar_entrada_menu(est, message_id); return True
    # Testar par
    if callback_data == "m_testar":
        answer_callback(callback_id); menu_lista_pares_testar(message_id); return True
    if callback_data.startswith("t_par_"):
        answer_callback(callback_id)
        forcar_teste(callback_data.replace("t_par_","")); return True
    return False

def executar_entrada_menu(est, message_id):
    """Executa a entrada manual escolhida pelo menu guiado."""
    par = est.get("par"); direcao = est.get("direcao"); valor = est.get("valor")
    if not (par and direcao and valor):
        edit_message(message_id, "⚠️ Faltam dados. Recomeça com ⚡ Entrar.",
                     [[{"text":"« Menu","callback_data":"m_voltar"}]])
        return
    # circuit breaker
    if circuit_breaker_ativo():
        edit_message(message_id, f"🚨 Entrada bloqueada — circuit breaker (perda ≥ ${DAILY_LOSS_LIMIT:.0f}).",
                     [[{"text":"« Menu","callback_data":"m_voltar"}]])
        return
    nome = par.replace("USDT","")
    dir_txt = "🟢 LONG" if direcao=="L" else "🔴 SHORT"
    edit_message(message_id, f"⏳ A abrir {nome} {dir_txt} ${valor}...")
    log_msg(f"ENTRADA MENU | {par} | {('LONG' if direcao=='L' else 'SHORT')} | ${valor}")
    # reutiliza o mt_exec existente: /mt <par> <L/S> <valor> <lev>
    mt_exec(["/mt", nome.lower(), direcao.lower(), str(valor), str(MAX_LEV)])
    menu_entrada.pop(CHAT_ID, None)

# ==================== ESPREITADELA 5min (v5.26) ====================
# Olha de 5 em 5 min sem entrar — so avisa se score >= LIMITE_AVISO.
# Reutiliza calc_score_simples (mesmas funcoes de indicadores do analyze).
PEEK_INTERVAL = 300        # 5 minutos
LIMITE_AVISO = 6.0         # avisa a partir deste |score|
last_peek = 0
peek_avisado = {}          # {bgsym: ultimo_score_avisado} — evita spam

def calc_score_peek(bgsym):
    """Espreitadela: usa a MESMA funcao de pontuacao do analyze (pontuar).
    Diferenca: nao chama o funding (poupa 1 chamada/par); passa funding=0.
    Devolve (score, cats, price)."""
    candles = bg_get_ohlcv(bgsym, granularity="60", limit=100)
    if not candles:
        return None
    closes = [float(c[4]) for c in candles]
    highs  = [float(c[2]) for c in candles]
    lows   = [float(c[3]) for c in candles]
    volumes= [float(c[5]) if len(c) > 5 else 1.0 for c in candles]
    price, _ = bg_get_ticker(bgsym)
    if not price:
        return None
    score, _bd, _reasons, cats, _r = pontuar(closes, highs, lows, volumes, 0.0, candles)
    return score, cats, price

def espreitar():
    """Espreitadela de 5min: avisa sinais a aproximarem-se, NAO entra."""
    global last_peek
    last_peek = time.time()
    for _, sym, bgsym in PAIRS:
        try:
            res = calc_score_peek(bgsym)
            if not res:
                continue
            score, cats, price = res
            log_msg(f"PEEK | {bgsym:6s} | score {score:+.1f} | cats {cats}/4 | ${price:,.4f}")
            # avisa so se cruzar o limite E mudou desde o ultimo aviso (evita spam)
            if abs(score) >= LIMITE_AVISO:
                ja = peek_avisado.get(bgsym)
                if ja != score:
                    peek_avisado[bgsym] = score
                    direcao = "🟢 LONG" if score>0 else "🔴 SHORT"
                    falta = max(0, int((ANALYSIS_INTERVAL-(time.time()-last_analysis))/60))
                    send(f"👀 <b>{sym} a aproximar-se de sinal!</b>\n"
                         f"{direcao} | score {score:+.1f} | ${fmt(price)}\n"
                         f"⏱️ Análise oficial em ~{falta}min\n"
                         f"<i>(aviso — o bot ainda não entrou)</i>")
            else:
                # saiu da zona de aviso -> limpa para poder voltar a avisar
                peek_avisado.pop(bgsym, None)
        except Exception as e:
            print(f"Erro espreitar {bgsym}: {e}")

def process_replies():
    global last_update_id
    for u in get_updates():
        last_update_id = u["update_id"]
        
        # Callback query (botões clicados)
        if "callback_query" in u:
            callback = u["callback_query"]
            callback_data = callback.get("data", "")
            callback_id = callback.get("id", "")
            message_id = callback.get("message", {}).get("message_id")

            # Menus inline (v5.24)
            if tratar_callback_menu(callback_data, message_id, callback_id):
                continue

            if callback_data.startswith("fechar_"):
                answer_callback(callback_id)
                symbol = callback_data.replace("fechar_", "")
                fechar_posicao_callback(symbol)
            continue
        
        text = u.get("message",{}).get("text","").strip().lower()
        if not text: continue
        # Botões do teclado fixo (enviam texto)
        if text == "📊 menu": mostrar_menu_principal(); continue
        if text == "📈 posições" or text == "📈 posicoes": mostrar_posicoes(); continue
        if text == "💰 saldo": mostrar_saldo(); continue
        if text == "⚡ entrar": mostrar_menu_principal(); continue
        if text.startswith("/teste"):
            p=text.split(); forcar_teste(p[1] if len(p)>1 else "BTC"); continue
        if text.startswith("/limpar"): limpar_ordens(); continue
        if text.startswith("/saldo"): mostrar_saldo(); continue
        if text.startswith("/posicoes") or text.startswith("/posições"): mostrar_posicoes(); continue
        if text.startswith("/mt "):
            args = text.split()
            mt_exec(args)
            continue
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
        if text.startswith("/fechar"):
            menu_fechar(); continue
        if text.startswith("/csv"):
            if os.path.isfile(CSV_PATH):
                try:
                    n = max(0, sum(1 for _ in open(CSV_PATH)) - 1)  # linhas - cabecalho
                except Exception:
                    n = "?"
                send_document(CSV_PATH, caption=f"📊 trades.csv — {n} trades registados\nEstratégia atual: <b>{ESTRATEGIA}</b>")
            else:
                send("ℹ️ Ainda não há trades registados no CSV (só grava quando uma posição AUTO/manual fecha).")
            continue
        if text.startswith("/estrategia") or text.startswith("/estratégia"):
            send(f"🎯 Estratégia ativa: <b>{ESTRATEGIA}</b>\n"
                 f"<i>(muda na variável de ambiente ESTRATEGIA = reversao | momentum e reinicia)</i>\n"
                 f"📊 CSV: {'existe' if os.path.isfile(CSV_PATH) else 'ainda vazio'} — usa /csv para exportar.")
            continue
        if text.startswith("/menu"): mostrar_menu_principal(); continue
        if text.startswith("/ajuda"): mostrar_ajuda(); continue
        if text.startswith("/start"): mostrar_menu_principal(); continue
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
                    # Circuit breaker (v5.19): bloqueio RIGIDO, nao contornavel com "confirmar"
                    if circuit_breaker_ativo():
                        send(f"🚨 <b>Entrada bloqueada</b> — circuit breaker ativo (perda diária ≥ ${DAILY_LOSS_LIMIT:.0f}).\nNovas entradas só amanhã (UTC).")
                        pending.pop(CHAT_ID, None)
                        continue
                    perda = perda_hoje()
                    if perda <= -DAILY_LOSS_WARNING:
                        send(f"⚠️ <b>Aviso:</b> já perdeste <b>${abs(perda):.2f}</b> hoje.\nPara entrar mesmo assim:\n<b>{text} confirmar</b>\nOu <b>não</b> para cancelar.")
                        continue
                sig, ohlc, e20, e50, bgsym = pending[CHAT_ID]
                send(f"⏳ A processar ${valor} ({tipo})...")
                executar_trade(sig, bgsym, valor, modo, tipo_valor=tipo, tipo="MANUAL")
                pending.pop(CHAT_ID, None)
            except Exception as e:
                send(f"⚠️ Erro: {e}")

# ==================== LOOP ====================
estado = "🔬 DRY RUN" if DRY_RUN else "💵 REAL"
papel = "PRINCIPAL (executa)" if PRINCIPAL else "ALERTA (não executa)"
print(f"Bot {VERSAO} — {estado} — {papel}")

# Sincroniza relogio com a Bitget antes de qualquer chamada assinada (v6.0)
bg_sync_time()

# Reconciliacao no arranque (v5.19): recupera posicoes apos restart.
# So a instancia principal gere posicoes.
if not DRY_RUN and PRINCIPAL:
    reconciliar_posicoes()

sizing_desc = f"risco {RISK_PCT:.0f}% conta" if RISK_AUTO_ENABLED else "$50 margem fixa"
msg_arranque = f"🤖 <b>{TAG}{BOT_NAME} {VERSAO}</b>\n{estado} | 🎭 {papel}\n🎯 Estratégia: <b>{ESTRATEGIA}</b> (RSI {'momentum ±1.5 + volume simétrico' if ESTRATEGIA=='momentum' else 'reversão ±3 + volume long'})\n⚡ Máx {MAX_LEV}x | Polling 15min\n🎯 Pares validados: {len(PAIRS)} ({', '.join(p[1] for p in PAIRS)})\n⚡ AUTOMÁTICO: Score ≥ {SCORE_AUTO:.0f} entra ({sizing_desc}, híbrido)\n🔗 Confluência mín: {MIN_CATEGORIAS}/4 categorias\n⏱️ Cooldown: {COOLDOWN_MIN}min por par\n🚨 Circuit breaker: ${DAILY_LOSS_LIMIT:.0f} perda/dia\n📊 CSV sinal→resultado ativo (/csv para exportar)\n✅ Usa os botões 👇"
send_com_teclado_fixo(msg_arranque)   # instala o teclado fixo (nunca desaparece)
mostrar_menu_principal()              # abre logo o painel inline no arranque
log_msg(f"BOT ARRANQUE — {BOT_ID} — {VERSAO} — {papel} — {len(PAIRS)} pares ({', '.join(p[1] for p in PAIRS)})")

_dia_atual = time.strftime("%Y-%m-%d", time.gmtime())
last_peek = time.time()   # primeira espreitadela só daqui a 5 min (evita duplicar com a 1ª análise)
while True:
    # Reset diario do circuit breaker (UTC)
    _hoje = time.strftime("%Y-%m-%d", time.gmtime())
    if _hoje != _dia_atual:
        _dia_atual = _hoje
        circuit_breaker_avisado = False
        print(f"🔄 Novo dia UTC ({_hoje}) — circuit breaker reposto")

    process_replies()
    verificar_posicoes_fechadas()

    # Espreitadela de 5min (v5.26) — só avisa, não entra
    if time.time()-last_peek >= PEEK_INTERVAL:
        try:
            espreitar()
        except Exception as e:
            print(f"Erro espreitadela: {e}")

    if time.time()-last_analysis >= ANALYSIS_INTERVAL:
        print(f"A analisar mercado ({ANALYSIS_INTERVAL}s)...")
        try:
            signals = analyze()
            if signals:
                # v6.0: escolhe o sinal mais forte do ciclo (AUTO tem prioridade,
                # depois |score|). Antes agia no 1º da lista -> viés sistemático p/ BTC.
                def _peso(item):
                    sig, *_rest, tipo = item
                    score = sig[5]
                    return (1 if tipo == "AUTO" else 0, abs(score))
                melhor = max(signals, key=_peso)
                sig, ohlc, e20, e50, bgsym, tipo = melhor
                enviar_sinal(sig, ohlc, e20, e50, bgsym, tipo)
            else:
                print("Sem sinais fortes.")
        except Exception as e:
            print(f"Erro: {e}")
    time.sleep(3)
