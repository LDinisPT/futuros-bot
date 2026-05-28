import os, time, requests, io
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
API = f"https://api.telegram.org/bot{TOKEN}"

PAIRS = [
    ("XBTUSD","BTC"),("ETHUSD","ETH"),("SOLUSD","SOL"),
    ("XRPUSD","XRP"),("ADAUSD","ADA"),("DOTUSDT","DOT"),
    ("LINKUSD","LINK"),("UNIUSD","UNI"),("ATOMUSD","ATOM"),
    ("LTCUSD","LTC"),("XDGUSD","DOGE"),("AAVEUSDT","AAVE")
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
        opens  = [float(c[1]) for c in candles]
        highs  = [float(c[2]) for c in candles]
        lows   = [float(c[3]) for c in candles]
        closes = [float(c[4]) for c in candles]
        xs = list(range(len(candles)))

        fig, ax = plt.subplots(figsize=(10,5))
        fig.patch.set_facecolor('#0d1318')
        ax.set_facecolor('#0d1318')

        for i, x in enumerate(xs):
            o,h,l,c = opens[i],highs[i],lows[i],closes[i]
            color = '#00e676' if c >= o else '#ff3d5a'
            ax.plot([x,x],[l,h], color=color, linewidth=0.8)
            ax.add_patch(plt.Rectangle((x-0.3,min(o,c)),0.6,abs(c-o),
                color=color, zorder=3))

        ax.plot(xs, ema20[-50:], color='#4a9eff', linewidth=1.2, label='EMA20', zorder=4)
        ax.plot(xs, ema50[-50:], color='#ffd166', linewidth=1.2, label='EMA50', zorder=4)

        ax.axhline(price, color='#ffffff', linewidth=1.2, linestyle='--', label=f'Entrada ${fmt(price)}')
        ax.axhline(sl,    color='#ff3d5a', linewidth=1.2, linestyle='--', label=f'SL ${fmt(sl)}')
        ax.axhline(tp1,   color='#00e676', linewidth=1.0, linestyle=':', label=f'TP1 ${fmt(tp1)}')
        ax.axhline(tp2,   color='#00e676', linewidth=1.2, linestyle='--', label=f'TP2 ${fmt(tp2)}')

        ax.axhspan(min(price,tp2),max(price,tp2), alpha=0.07, color='#00e676')
        ax.axhspan(min(price,sl), max(price,sl),  alpha=0.07, color='#ff3d5a')

        mid_x = len(xs)*0.75
        dy = (tp1-price)*0.5
        ax.annotate('', xy=(mid_x,price+dy), xytext=(mid_x,price),
            arrowprops=dict(arrowstyle='->',
            color='#00e676' if "LONG" in signal else '#ff3d5a', lw=2.5))

        ax.tick_params(colors='#4a6070', labelsize=7)
        for spine in ax.spines.values(): spine.set_color('#1a2430')
        ax.yaxis.set_tick_params(labelcolor='#c8d8e8')
        ax.xaxis.set_tick_params(labelbottom=False)
        ax.grid(color='#1a2430', linewidth=0.5, alpha=0.5)
        direction = "LONG 📈" if "LONG" in signal else "SHORT 📉"
        ax.set_title(f'{sym}/USD — {direction}  |  Últimas 50 velas (1h)',
            color='#c8d8e8', fontsize=10, pad=10)
        ax.legend(loc='upper left', fontsize=7,
            facecolor='#0d1318', edgecolor='#1a2430', labelcolor='#c8d8e8')

        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=130, facecolor='#0d1318')
        plt.close()
        return buf
    except Exception as e:
        print(f"Erro gráfico: {e}")
        return None

def send_full_signal(sig, saldo, ohlc_data, ema20_arr, ema50_arr):
    sym,price,change,rsi_v,label,score,reasons = sig
    sl,tp1,tp2,alav,sl_pct = calc_levels(price,label,rsi_v)
    risco_usd = saldo*0.02
    tamanho = min(round(risco_usd/sl_pct,2), saldo*alav)
    lucro_tp1 = round(tamanho*(abs(tp1-price)/price),2)
    lucro_tp2 = round(tamanho*(abs(tp2-price)/price),2)
    arrow = "↑" if "LONG" in label else "↓"

    caption  = f"{arrow} <b>{sym}/USD — {label}</b>\n"
    caption += f"━━━━━━━━━━━━━━━\n"
    caption += f"💲 <b>Entrada:</b> ${fmt(price)}\n"
    caption += f"🛑 <b>Stop Loss:</b> ${fmt(sl)}\n"
    caption += f"🎯 <b>TP1:</b> ${fmt(tp1)}\n"
    caption += f"🎯 <b>TP2:</b> ${fmt(tp2)}\n"
    caption += f"━━━━━━━━━━━━━━━\n"
    caption += f"💼 <b>GESTÃO DE RISCO</b>\n"
    caption += f"💰 Saldo: ${saldo:,.0f}\n"
    caption += f"⚡ Alavancagem: {alav}x\n"
    caption += f"📊 Posição: ${tamanho:,.0f}\n"
    caption += f"❌ Risco: −${round(risco_usd,2)}\n"
    caption += f"✅ TP1: +${lucro_tp1} | TP2: +${lucro_tp2}\n"
    caption += f"━━━━━━━━━━━━━━━\n"
    caption += f"📉 RSI: {rsi_v} | ⚡ Score: {score:+d}/7\n"
    caption += f"📌 {', '.join(reasons)}\n"
    caption += f"⚠️ <i>Não é aconselhamento financeiro.</i>"

    buf = make_chart(ohlc_data, price, sl, tp1, tp2, sym, label, ema20_arr, ema50_arr)
    if buf:
        send_photo(buf, caption)
    else:
        send(caption)

def process_replies():
    global
