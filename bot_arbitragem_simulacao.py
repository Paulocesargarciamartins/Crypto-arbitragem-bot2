# bot_arbitragem_simulacao.py
import asyncio
from decouple import config
from telethon import TelegramClient, events
import ccxt.async_support as ccxt
import traceback
import nest_asyncio

nest_asyncio.apply()

# --- Config Telegram ---
API_ID = int(config('API_ID'))
API_HASH = config('API_HASH')
BOT_TOKEN = config('BOT_TOKEN')
TARGET_CHAT_ID = int(config('TARGET_CHAT_ID'))  # assegure que está correto

# --- Exchanges ativas (mantidas como você pediu) ---
exchanges_names = ['okx', 'cryptocom']

# --- Base de pares e valores ---
target_pairs = [
    'XRP/USDT','DOGE/USDT','BCH/USDT','LTC/USDT','UNI/USDT',
    'XLM/USDT','BNB/USDT','AVAX/USDT','APT/USDT','AAVE/USDT',
    'ETH/USDT','BTC/USDT','SOL/USDT','ADA/USDT','DOT/USDT',
    'LINK/USDT','MATIC/USDT','ATOM/USDT','FTM/USDT','TRX/USDT',
    'EOS/USDT','NEAR/USDT','ALGO/USDT','VET/USDT','ICP/USDT',
    'FIL/USDT','SAND/USDT','MANA/USDT','THETA/USDT','AXS/USDT'
]
trade_amount_usdt = 1.0

# --- Inicializa Telethon ---
client = TelegramClient('bot', API_ID, API_HASH)
exchanges = {}

# --- Helpers ccxt ---
def get_ccxt_exchange_class(name):
    candidates = [name, name.replace('-', '_'), name.replace('cryptocom', 'crypto_com'), name.replace('okx', 'okex')]
    for c in candidates:
        if hasattr(ccxt, c):
            return getattr(ccxt, c)
    return None

async def init_exchanges():
    global exchanges
    exchanges = {}
    for name in exchanges_names:
        cls = get_ccxt_exchange_class(name)
        if not cls:
            print(f"[WARN] Classe ccxt para '{name}' não encontrada.")
            continue
        try:
            ex = cls({'enableRateLimit': True})
            exchanges[name] = ex
            print(f"[INFO] Iniciada exchange: {name}")
        except Exception as e:
            print(f"[ERROR] Falha ao iniciar {name}: {e}")
            traceback.print_exc()

async def load_markets():
    markets = {}
    failed = []
    for name, ex in list(exchanges.items()):
        try:
            await ex.load_markets()
            markets[name] = ex.markets
            print(f"[INFO] Mercados carregados: {name} ({len(ex.markets)} mercados)")
        except Exception as e:
            print(f"[ERROR] load_markets {name}: {e}")
            traceback.print_exc()
            failed.append(name)
    for name in failed:
        if name in exchanges:
            try:
                await exchanges[name].close()
            except: pass
            del exchanges[name]
    return markets

def filter_common_pairs(markets):
    if not markets:
        return []
    sets = [set(m.keys()) for m in markets.values() if m]
    common = set.intersection(*sets) if sets else set()
    selected = [p for p in target_pairs if p in common]
    extras = list(common - set(target_pairs))
    return selected + extras[: max(0, 30 - len(selected))]

# --- Order books / arbitragem (mantém sua lógica) ---
async def fetch_order_book(exchange, name, symbol):
    limit = 5
    try:
        ob = await exchange.fetch_order_book(symbol, limit=limit)
        bid = ob['bids'][0][0] if ob.get('bids') else None
        ask = ob['asks'][0][0] if ob.get('asks') else None
        return (name, symbol, bid, ask)
    except Exception as e:
        print(f"[WARN] Erro fetch_order_book {name} {symbol}: {e}")
        return None

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

def detect_arbitrage_opportunities(data):
    opportunities = []
    for symbol, prices in data.items():
        if len(prices) < 2: continue
        for ex_buy, buy_data in prices.items():
            for ex_sell, sell_data in prices.items():
                if ex_buy == ex_sell: continue
                if buy_data.get('ask') and sell_data.get('bid'):
                    try:
                        profit_percent = ((sell_data['bid'] - buy_data['ask']) / buy_data['ask']) * 100
                    except Exception:
                        continue
                    if profit_percent > 0.5:
                        opportunities.append({
                            'symbol': symbol,
                            'buy_exchange': ex_buy,
                            'buy_price': buy_data['ask'],
                            'sell_exchange': ex_sell,
                            'sell_price': sell_data['bid'],
                            'profit_percent': profit_percent,
                            'amount_usdt': trade_amount_usdt
                        })
    return sorted(opportunities, key=lambda x: x['profit_percent'], reverse=True)

# --- Envio de mensagens (usa chat id direto) ---
async def send_telegram_message(message):
    try:
        await client.send_message(TARGET_CHAT_ID, message)
        print("[INFO] Mensagem enviada ao Telegram.")
    except Exception as e:
        print(f"[ERROR] Falha ao enviar Telegram: {e}")
        traceback.print_exc()

# --- Handler universal: LOGA TUDO e processa comandos ---
@client.on(events.NewMessage(incoming=True))
async def universal_handler(event):
    """
    Loga todas as mensagens recebidas (útil para debug).
    Responde a /status e /settrade apenas se vierem do TARGET_CHAT_ID
    ou de conversa privada com o bot.
    """
    try:
        chat_id = getattr(event.chat_id, '__int__', lambda: event.chat_id)()
    except Exception:
        chat_id = event.chat_id
    text = (event.raw_text or "").strip()
    sender = getattr(event.sender_id, None)
    print(f"[MSG] chat_id={chat_id} sender_id={sender} text={text}")

    # Responde só se for do chat configurado (ou se for privado com o bot)
    # event.is_private is True for private chats
    is_allowed = (chat_id == TARGET_CHAT_ID) or getattr(event, 'is_private', False)
    if not is_allowed:
        return

    # Comando /status
    if text.startswith("/status"):
        msg = f"Valor de trade atual: {trade_amount_usdt} USDT\nExchanges ativas: {', '.join(exchanges.keys())}"
        try:
            await event.respond(msg)
            print("[INFO] Respondeu /status")
        except Exception as e:
            print(f"[ERROR] Falha ao responder /status: {e}")
        return

    # Comando /settrade <valor>
    if text.startswith("/settrade"):
        parts = text.split()
        if len(parts) >= 2:
            try:
                val = float(parts[1])
                if 0 < val <= 100:
                    global trade_amount_usdt
                    trade_amount_usdt = val
                    await event.respond(f"Valor de trade ajustado para {trade_amount_usdt} USDT.")
                    print(f"[INFO] trade_amount_usdt ajustado para {trade_amount_usdt}")
                else:
                    await event.respond("Informe um valor entre 0 e 100 USDT.")
            except Exception as e:
                await event.respond(f"Erro ao interpretar valor: {e}")
        else:
            await event.respond("Uso: /settrade 5")
        return

# --- Loop principal ---
async def main_loop():
    try:
        await init_exchanges()
        markets = await load_markets()
        pairs = filter_common_pairs(markets)
        if not pairs:
            msg = "⚠️ Nenhum par comum encontrado."
            await send_telegram_message(msg)
            print(msg)
        else:
            msg = f"Bot iniciado com {len(pairs)} pares monitorados."
            await send_telegram_message(msg)
            print(msg)

        while True:
            try:
                data = await fetch_order_books(pairs)
                ops = detect_arbitrage_opportunities(data)
                if ops:
                    text = "🤑 Oportunidades detectadas:\n"
                    for o in ops[:10]:
                        text += (f"{o['symbol']} | Comprar em {o['buy_exchange']} a {o['buy_price']:.6f} | "
                                 f"Vender em {o['sell_exchange']} a {o['sell_price']:.6f} | "
                                 f"Lucro: {o['profit_percent']:.2f}% | Valor: {o['amount_usdt']:.2f}\n")
                    await send_telegram_message(text)
                    print("[INFO] Enviada oportunidade.")
            except Exception as e:
                print(f"[ERROR] Loop principal: {e}")
                traceback.print_exc()
            await asyncio.sleep(300)
    finally:
        for name, ex in exchanges.items():
            try:
                await ex.close()
            except:
                pass
        print("[INFO] Exchanges fechadas.")

# --- Run: roda main_loop em background e mantém Telethon escutando comandos ---
async def run_bot():
    try:
        print("[INFO] Iniciando Telethon...")
        await client.start(bot_token=BOT_TOKEN)
        print("[INFO] Telethon started.")
        # roda main_loop em background
        asyncio.create_task(main_loop())
        # mantém o client escutando eventos/handlers
        await client.run_until_disconnected()
    except Exception as e:
        print(f"[FATAL] Erro ao iniciar: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(run_bot())
