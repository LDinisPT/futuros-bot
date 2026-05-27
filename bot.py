import os, time, requests

TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
API = f"https://api.telegram.org/bot{TOKEN}"

COINS = [
    ("bitcoin","BTC"),("ethereum","ETH"),("solana","SOL"),
    ("ripple","XRP"),("dogecoin","DOGE"),("cardano","ADA"),
    ("avalanche-2","AVAX"),("chainlink","LINK"),("uniswap","UNI"),
    ("near","NEAR"),("injective-protocol","INJ"),("sui","SUI"),
    ("pepe","PEPE"),("aave","AAVE"),("dogwifcoin","WIF"),
]

def send(msg):
    requests.post(f"{API}/sendMessage",json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"HTML"})

def get_prices(coin_id):
    r = requests.get(f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart?vs_currency=usd&days=30&interval=daily",timeout=15)
    return r.json()

def rsi(prices,p=14):
    if len(prices)<p+1: return 50
    g=l=0
    for i in range(len(prices)-p,len(prices)):
        d=prices[i]-prices[i-1]
        if d>0: g+=d
        else: l-=d
    ag,al=g/p,l/p
    if al==0: return 100
    return round(100-(100/(1+ag/al)),1)

def ema(prices,p):
    if len(prices)<p: return prices[-1]
    k=2/(p+1)
    e=sum(prices[:p])/p
    for x in prices[p:]: e=x*k+e*(1-k)
    return e

def analyze():
    signals=[]
    ids=[c[0] for c in COINS]
    mkt=requests.get(f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={','.join(ids)}&per_page=50",timeout=15).json()
    mkt_map={c["id"]:c for c in mkt}
    for coin_id,sym in COINS:
        try:
            m=mkt_map.get(coin_id)
            if not m: continue
            data=get_prices(coin_id)
            prices=[p[1] for p in data["prices"]]
            change=m["price_change_percentage_24h"] or 0
            price=m["current_price"]
            r=rsi(prices)
            e7=ema(prices,7)
            e25=ema(prices,25)
            bull=e7>e25
            score=0
            if r<30: score+=3
            elif r<40: score+=1
            elif r>70: score-=3
            elif r>60: score-=1
            if bull: score+=2
            else: score-=2
            if price>e7 and bull: score+=1
            elif price<e7 and not bull: score-=1
            if change>5: score+=1
            elif change<-5: score-=1
            if score>=4: signals.append((sym,price,change,r,"🟢 LONG FORTE",score))
            elif score<=-4: signals.append((sym,price,change,r,"🔴 SHORT FORTE",score))
            time.sleep(2)
        except Exception as e:
            print(f"Erro {sym}: {e}")
    return signals

def fmt_price(p):
    if p>100: return f"{p:,.2f}"
    if p>1: return f"{p:.4f}"
    return f"{p:.6f}"

send("🤖 <b>FuturesScan Bot iniciado!</b>\nVou analisar o mercado a cada hora e avisar-te de sinais LONG/SHORT fortes.")

last_signals=set()
while True:
    print("A analisar mercado...")
    try:
        signals=analyze()
        new=[(s,p,c,r,l,sc) for s,p,c,r,l,sc in signals if s not in last_signals]
        if new:
            msg="📊 <b>NOVOS SINAIS FUTUROS</b>\n\n"
            for sym,price,change,rsi_val,label,score in new:
                arrow="↑" if "LONG" in label else "↓"
                msg+=f"{arrow} <b>{sym}/USDT</b> {label}\n"
                msg+=f"   💲 ${fmt_price(price)}\n"
                msg+=f"   📈 24h: {change:+.2f}%\n"
                msg+=f"   📉 RSI: {rsi_val}\n"
                msg+=f"   ⚡ Score: {score:+d}/7\n\n"
            msg+="⚠️ <i>Não é aconselhamento financeiro.</i>"
            send(msg)
            last_signals={s for s,*_ in signals}
        else:
            print("Sem novos sinais fortes.")
            last_signals={s for s,*_ in signals}
    except Exception as e:
        print(f"Erro geral: {e}")
    time.sleep(3600)
