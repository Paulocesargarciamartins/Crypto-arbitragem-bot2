import asyncio
from decouple import config
from telethon import TelegramClient, events
import ccxt.async_support as ccxt

# --- Config Telegram ---
API_ID = int(config('API_ID'))           # só números
API_HASH = config('API_HASH')            # string
BOT_TOKEN = config('BOT_TOKEN')          # string completa, sem cast=int
TARGET_CHAT_ID = int(config('TARGET_CHAT_ID'))  # só números

# --- Exchanges ---
exchanges_list = {
    'okx': ccxt.okx,
    'cryptocom': ccxt.cryptocom,
    'kucoin': ccxt.kucoin,
    'bybit': ccxt.bybit,
    'huobi': ccxt.huobi,
}

# --- Taxas aproximadas em % ---
spot_fees = {
    'okx': 0.10,
    'cryptocom': 0.075,
    'kucoin': 0.10,
    'bybit': 0.10,
    'huobi': 0.20,
}
margin_fee_per_hour = {
    'okx': 0.003,
    'cryptocom': 0.03,
    'kucoin': 0.03,
    'bybit': 0.03,
    'huobi': 0.03,
}

# --- Pares já combinados + extras para completar 30 ---
target_pairs = [
    'XRP/USDT','DOGE/USDT','BCH/USDT','LTC/USDT','UNI/USDT',
    'XLM/USDT','BNB/USDT','AVAX/USDT','APT/USDT','AAVE/USDT',
    'ETH/USDT','BTC/USDT','SOL/USDT','ADA/USDT','DOT/USDT',
    'LINK/USDT','MATIC/USDT','ATOM/USDT','FTM/USDT','TRX/USDT',
    'EOS/USDT','NEAR/USDT','ALGO/USDT','VET/USDT','ICP/USDT',
    'FIL/USDT','SAND/USDT','MANA/USDT','THETA/USDT','AXS/USDT'
]

trade_amount_usdt = 1.0  # operação de teste

# --- Setup Telegram ---
client = TelegramClient('bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

exchanges = {}

# --- Inicialização das exchanges ---
async def init_exchanges():
    for name, cls in exchanges_list.items():
        exchange = cls({'enableRateLimit': True})
        exchanges[name] = exchange

# --- Carregar mercados ---
async def load_markets():
    markets = {}
    for name, ex in exchanges.items():
        await ex.load_markets()
        markets[name] = ex.markets
    return markets

# --- Filtrar pares comuns nas 5 exchanges ---
def filter_common_pairs(markets):
    common = set.intersection(*(set(m.keys()) for m in markets.values()))
    selected = [p for p in target_pairs if p in common]
    extras = list(common - set(target_pairs))
    return selected + extras[: (30 - len(selected))]

# --- Buscar order books ---
async def fetch_order_books(pairs):
    data = {}
    tasks = []
    for symbol in pairs:
        for name, ex in exchanges.items():
            tasks.append(fetch_order_book(ex, name, symbol))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for res in results:
        if isinstance(res, tuple):
            name, symbol, bid, ask = res
            data.setdefault(symbol, {})[name] = {'bid': bid, 'ask': ask}
    return data

async def fetch_order_book(exchange, name, symbol):
    try:
        order_book = await exchange.fetch_order_book(symbol, limit=5)
        bid = order_book['bids'][0][0] if order_book['bids'] else None
        ask = order_book['asks'][0][0] if order_book['asks'] else None
        return (name, symbol, bid, ask)
    except Exception as e:
        print(f"Erro fetch_order_book {name} {symbol}: {e}")
        return None

# --- Detectar oportunidades de arbitragem ---
def detect_arbitrage_opportunities(data):
    opportunities = []
    for symbol, prices in data.items():
        for ex_buy, buy_data in prices.items():
            for ex_sell, sell_data in prices.items():
                if ex_buy == ex_sell:
                    continue
                if buy_data['ask'] and sell_data['bid']:
                    profit_percent = ((sell_data['bid'] - buy_data['ask']) / buy_data['ask']) * 100
                    if profit_percent > 0.5:  # limiar mínimo
                        amount = trade_amount_usdt
                        opportunities.append({
                            'symbol': symbol,
                            'buy_exchange': ex_buy,
                            'buy_price': buy_data['ask'],
                            'sell_exchange': ex_sell,
                            'sell_price': sell_data['bid'],
                            'profit_percent': profit_percent,
                            'amount_usdt': amount
                        })
    return sorted(opportunities, key=lambda x: x['profit_percent'], reverse=True)

# --- Enviar mensagens no Telegram ---
async def send_telegram_message(message):
    try:
        await client.send_message(TARGET_CHAT_ID, message)
    except Exception as e:
        print(f"Erro ao enviar Telegram: {e}")

# --- Comandos Telegram ---
@client.on(events.NewMessage(pattern='/settrade (\\d+(\\.\\d+)?)'))
async def handler_settrade(event):
    global trade_amount_usdt
    try:
        value = float(event.pattern_match.group(1))
        if 0 < value <= 100:
            trade_amount_usdt = value
            await event.respond(f"Valor de trade ajustado para {trade_amount_usdt} USDT.")
        else:
            await event.respond("Informe um valor entre 0 e 100 USDT.")
    except Exception as e:
        await event.respond(f"Erro: {e}")

@client.on(events.NewMessage(pattern='/status'))
async def handler_status(event):
    msg = f"Valor de trade atual: {trade_amount_usdt} USDT\n"
    await event.respond(msg)

# --- Loop principal ---
async def main_loop():
    await init_exchanges()
    markets = await load_markets()
    pairs = filter_common_pairs(markets)
    await send_telegram_message("Bot iniciado com APIs públicas para arbitragem mercado descoberto.")
    while True:
        data = await fetch_order_books(pairs)
        opportunities = detect_arbitrage_opportunities(data)
        if opportunities:
            msg = "🤑 Oportunidades detectadas:\n"
            for opp in opportunities[:10]:
                msg += (f"{opp['symbol']} | Comprar em {opp['buy_exchange']} a {opp['buy_price']:.6f} USDT | "
                        f"Vender em {opp['sell_exchange']} a {opp['sell_price']:.6f} USDT | "
                        f"Lucro: {opp['profit_percent']:.2f}% | Valor trade: {opp['amount_usdt']:.2f} USDT\n")
            await send_telegram_message(msg)
        await asyncio.sleep(300)  # 5 minutos

async def main():
    await main_loop()

if __name__ == '__main__':
    client.loop.run_until_complete(main())
