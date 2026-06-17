# futuros-bot

FuturesScan Bot (Bitget USDT-Futures) — **v6.1**.

Analisa 4 pares (BTC, XRP, UNI, ATOM) a cada 15 min com RSI, EMA, MACD, ATR,
volume, funding e padrões de vela. Entradas automáticas em score alto, alertas
manuais em score médio. Controlo por Telegram.

## Variáveis de ambiente

Obrigatórias: `BOT_TOKEN`, `CHAT_ID`, `BITGET_API_KEY`, `BITGET_SECRET`, `BITGET_PASSPHRASE`.

Opcionais (v6.x):
- `ROLE` — `principal` (default, executa) ou `alerta` (só avisa, não mexe na conta).
  Só relevante se correres mais do que uma instância na mesma conta.
- `ESTRATEGIA` — `reversao` (default) ou `momentum`.
  - `reversao`: RSI de reversão à média (peso ±3) + volume só confirma longs.
  - `momentum`: RSI alinhado com a tendência (peso ±1.5) + volume simétrico (long e short).
- `CSV_DIR` — pasta onde grava o `trades.csv` (default: pasta do bot).
  No Railway o disco é efémero (apaga a cada deploy): monta um **Volume** em `CSV_DIR`,
  ou usa `/csv` no Telegram para puxar o ficheiro antes de cada redeploy.

## CSV sinal→resultado
Cada trade fechado escreve uma linha em `trades.csv` com as features do sinal
(score, RSI, EMA, MACD, volume, funding, padrões, ATR…) + o resultado real
(pnl, ROE, duração, WIN/LOSS) e a estratégia usada. Serve para afinar os pesos
com dados reais e comparar `reversao` vs `momentum`.

Comandos novos: `/csv` (exporta o ficheiro), `/estrategia` (mostra a tese ativa).
