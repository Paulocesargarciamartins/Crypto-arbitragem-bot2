import asyncio
from decouple import config
from telethon import TelegramClient, events
import ccxt.async_support as ccxt
import traceback
import nest_asyncio

# Aplica o nest_asyncio para evitar erros de loop de eventos aninhados
nest_asyncio.apply()

# --- Config Telegram ---
API_ID = int(config('API_ID'))
API_HASH = config('API_HASH')
BOT_TOKEN = config('BOT_TOKEN')
TARGET_CHAT_ID = int(config('TARGET_CHAT_ID'))

# --- Exchanges ---
exchanges_names = [
    'okx',
    'cryptocom',
    'kucoin',
    'bybit',
    'huobi',
]

# --- Taxas aproximadas ---
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

# --- Pares alvo ---
target_pairs = [
    'XRP/USDT','DOGE/USDT','BCH/USDT','LTC/USDT','UNI/USDT',
    'XLM/USDT','BNB/USDT','AVAX/USDT','APT/USDT','AAVE/USDT',
    'ETH/USDT','BTC/USDT','SOL/USDT','ADA/USDT','DOT/USDT',
    'LINK/USDT','MATIC/USDT','ATOM/USDT','FTM/USDT','TRX/USDT',
    'EOS/USDT','NEAR/USDT','ALGO/USDT','VET/USDT','ICP/USDT',
    'FIL/USDT','SAND/USDT','MANA/USDT','THETA/USDT','AXS/USDT'
]

trade_amount_usdt = 1.0

# --- Inicialização do Telethon ---
client = TelegramClient('bot', API_ID, API_HASH)
exchanges = {}
telegram_ready = False

# --- Helper para encontrar a classe da exchange no ccxt ---
def get_ccxt_exchange_class(name):
    candidates = [
        name,
        name.replace('-', '_'),
        name.replace('cryptocom', 'crypto_com'),
        name.replace('okx', 'okex'),
    ]
    for c in candidates:
        if hasattr(ccxt, c):
            return getattr(ccxt, c)
    return None

# --- Inicialização das exchanges ---
async def init_exchanges():
    global exchanges
    exchanges = {}
    for name in exchanges_names:
        cls = get_ccxt_exchange_class(name)
        if not cls:
            print(f"[WARN] Classe ccxt para '{name}' não encontrada — será ignorada.")
            continue
        try:
            if name == 'huobi':
                ex = cls({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
            else:
                ex = cls({'enableRateLimit': True})
            exchanges[name] = ex
            print(f"[INFO] Iniciada exchange: {name}")
        except Exception as e:
            print(f"[ERROR] Falha ao inicializar {name}: {e}")
            traceback.print_exc()

# --- Carregar mercados ---
async def load_markets():
    markets = {}
    failed_exchanges = []
    for name, ex in list(exchanges.items()):
        try:
            await ex.load_markets()
            markets[name] = ex.markets
            print(f"[INFO] Mercados carregados: {name} ({len(ex.markets)} mercados)")
        except Exception as e:
            print(f"[ERROR] load_markets {name}: {e}")
            traceback.print_exc()
            failed_exchanges.append(name)
    for name in failed_exchanges:
        if name in exchanges:
            await exchanges[name].close()
            del exchanges[name]
    return markets

# --- Filtrar pares comuns ---
def filter_common_pairs(markets):
    if not markets:
        return []
    sets = [set(m.keys()) for m in markets.values() if m]
    common = set.intersection(*sets) if sets else set()
    selected = [p for p in target_pairs if p in common]
    extras = list(common - set(target_pairs))
    return selected + extras[: max(0, 30 - len(selected))]

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
            if bid and ask:
                data.setdefault(symbol, {})[name] = {'bid': bid, 'ask': ask}
    return data

async def fetch_order_book(exchange, name, symbol):
    limit = 20 if name == 'kucoin' else 5
    try:
        order_book = await exchange.fetch_order_book(symbol, limit=limit)
        bid = order_book['bids'][0][0] if order_book.get('bids') else None
        ask = order_book['asks'][0][0] if order_book.get('asks') else None
        return (name, symbol, bid, ask)
    except Exception as e:
        print(f"[WARN] Erro fetch_order_book {name} {symbol}: {e}")
        return None

# --- Detectar oportunidades ---
def detect_arbitrage_opportunities(data):
    opportunities = []
    for symbol, prices in data.items():
        if len(prices) < 2:
            continue
        for ex_buy, buy_data in prices.items():
            for ex_sell, sell_data in prices.items():
                if ex_buy == ex_sell:
                    continue
                if buy_data.get('ask') and sell_data.get('bid'):
                    try:
                        profit_percent = ((sell_data['bid'] - buy_data['ask']) / buy_data['ask']) * 100
                    except Exception:
                        continue
                    if profit_percent > 0.5:
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

# --- Enviar mensagens ---
async def send_telegram_message(message):
    global telegram_ready
    if not telegram_ready:
        print("[WARN] Telegram não está pronto, ignorando mensagem.")
        return
    try:
        await client.send_message(TARGET_CHAT_ID, message)
    except Exception as e:
        print(f"[ERROR] Erro ao enviar Telegram: {e}")
        traceback.print_exc()

# --- Comandos Telegram ---
@client.on(events.NewMessage(incoming=True, chats=TARGET_CHAT_ID, pattern='/status'))
async def handler_status(event):
    msg = f"Valor de trade atual: {trade_amount_usdt} USDT\nExchanges ativas: {', '.join(exchanges.keys())}"
    await event.respond(msg)

@client.on(events.NewMessage(incoming=True, chats=TARGET_CHAT_ID, pattern='/settrade (\\d+(\\.\\d+)?)'))
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

# --- Loop principal ---
async def main_loop():
    global telegram_ready
    try:
        await init_exchanges()
        markets = await load_markets()
        pairs = filter_common_pairs(markets)
        if not pairs:
            msg = "⚠️ Bot iniciado, mas não encontrou pares comuns nas exchanges configuradas."
            await send_telegram_message(msg)
            print(msg)
        else:
            msg = f"Bot iniciado com {len(pairs)} pares monitorados."
            await send_telegram_message(msg)
            print(msg)
        telegram_ready = True

        while True:
            try:
                data = await fetch_order_books(pairs)
                opportunities = detect_arbitrage_opportunities(data)
                if opportunities:
                    msg = "🤑 Oportunidades detectadas:\n"
                    for opp in opportunities[:10]:
                        msg += (f"{opp['symbol']} | Comprar em {opp['buy_exchange']} a {opp['buy_price']:.6f} USDT | "
                                f"Vender em {opp['sell_exchange']} a {opp['sell_price']:.6f} USDT | "
                                f"Lucro: {opp['profit_percent']:.2f}% | Valor trade: {opp['amount_usdt']:.2f} USDT\n")
                    await send_telegram_message(msg)
                    print(msg)
            except Exception as e:
                print(f"[ERROR] Erro no loop principal: {e}")
                traceback.print_exc()
            await asyncio.sleep(300)
    finally:
        for name, ex in exchanges.items():
            try:
                await ex.close()
            except Exception as e:
                print(f"[WARN] Falha ao fechar conexão de {name}: {e}")
        print("[INFO] Todas as conexões das exchanges foram fechadas.")

async def run_bot():
    try:
        await client.start(bot_token=BOT_TOKEN)
        await asyncio.gather(main_loop(), client.run_until_disconnected())
    except Exception as e:
        print(f"[FATAL] Ocorreu um erro fatal ao iniciar o bot: {e}")
        traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(run_bot())
