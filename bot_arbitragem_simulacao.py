import asyncio
from decouple import config
from telethon import TelegramClient, events
import ccxt.async_support as ccxt

# --- Config Telegram ---
API_ID = config('API_ID', cast=int)
API_HASH = config('API_HASH')
BOT_TOKEN = config('BOT_TOKEN')
TARGET_CHAT_ID = config('TARGET_CHAT_ID', cast=int)

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

# --- Pares originais (28 combinados) + extras para completar 30 ---
original_pairs = [
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

async def init_exchanges():
    for name, cls in exchanges_list.items():
        exchange = cls({'enableRateLimit': True})
        exchanges[name] = exchange

async def load_markets():
    markets = {}
    for name, ex in exchanges.items():
        await ex.load_markets()
        markets[name] = ex.markets
    return markets

def filter_common_pairs(markets):
    # pares existentes nas 5 exchanges
    common = set.intersection(*(set(m.keys()) for m in markets.values()))
    # mantém os 28 originais e adiciona extras para completar 30
    selected = [p for p in original_pairs if p in common]
    extras = list(common - set(original_pairs))
    return selected + extras[: (30 - len(selected))]

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

def detect_arbitrage_opportunities(data):
    opportunities = []
    for symbol, prices in data.items():
        for ex_buy, buy_data in prices.items():
            for ex_sell, sell_data in prices.items():
                if ex_buy == ex_sell:
                    continue
                if buy_data['ask'] and sell_data['bid']:
                    gross_profit = ((sell_data['bid'] - buy_data['ask']) / buy_data['ask']) * 100
                    total_fees = spot_fees[ex_buy] + spot_fees[ex_sell] + margin_fee_per_hour[ex_buy]
                    net_profit = gross_profit - total_fees
                    if net_profit > 0:
                        opportunities.append({
                            'symbol': symbol,
                            'buy_exchange': ex_buy,
                            'buy_price': buy_data['ask'],
                            'sell_exchange': ex_sell,
                            'sell_price': sell_data['bid'],
                            'gross_profit_percent': gross_profit,
                            'net_profit_percent': net_profit,
                            'amount_usdt': trade_amount_usdt
                        })
    return sorted(opportunities, key=lambda x: x['net_profit_percent'], reverse=True)

async def send_telegram_message(message):
    try:
        await client.send_message(TARGET_CHAT_ID, message)
    except Exception as e:
        print(f"Erro ao enviar Telegram: {e}")

@client.on(events.NewMessage(pattern='/status'))
async def handler_status(event):
    msg = f"Configuração de teste:\nTrade fixo: {trade_amount_usdt} USDT\n"
    await event.respond(msg)

async def main_loop():
    await init_exchanges()
    markets = await load_markets()
    pairs = filter_common_pairs(markets)
    await send_telegram_message("Bot iniciado. Pares monitorados:\n" + ", ".join(pairs))
    while True:
        data = await fetch_order_books(pairs)
        opportunities = detect_arbitrage_opportunities(data)
        if opportunities:
            msg = "🤑 Oportunidades detectadas (lucro líquido calculado):\n"
            for opp in opportunities[:10]:
                msg += (f"{opp['symbol']} | Comprar em {opp['buy_exchange']} a {opp['buy_price']:.6f} | "
                        f"Vender em {opp['sell_exchange']} a {opp['sell_price']:.6f} | "
                        f"Lucro bruto: {opp['gross_profit_percent']:.4f}% | "
                        f"Lucro líquido: {opp['net_profit_percent']:.4f}% | "
                        f"Valor trade: {opp['amount_usdt']} USDT\n")
            await send_telegram_message(msg)
        else:
            await send_telegram_message("Sem oportunidades no momento.")
        await asyncio.sleep(60)  # 1 minuto

async def main():
    await main_loop()

if __name__ == '__main__':
    client.loop.run_until_complete(main())
