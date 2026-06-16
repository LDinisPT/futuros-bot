# futuros-bot

FuturesScan Bot (Bitget USDT-Futures) — **v6.0**.

Análise de 4 pares validados (BTC, XRP, UNI, ATOM) a cada 15 min com RSI, EMA,
MACD, ATR, volume, funding e padrões de vela. Entradas automáticas em score alto,
alertas manuais em score médio. Telegram para controlo.

## Variável de ambiente importante (v6.0)
`ROLE` — papel da instância quando corres o bot em mais do que um sítio (Pi + Railway):
- `ROLE=principal` (default) — executa trades e gere posições.
- `ROLE=alerta` — só analisa e avisa; **não** mexe na conta.

Define `ROLE=alerta` numa das instâncias para evitar que as duas negoceiem a mesma conta.

Restantes variáveis: `BOT_TOKEN`, `CHAT_ID`, `BITGET_API_KEY`, `BITGET_SECRET`, `BITGET_PASSPHRASE`.
