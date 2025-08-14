# bot_arbitragem_simulacao.py
import asyncio
from decouple import config
from telethon import TelegramClient, events
import ccxt.async_support as ccxt
import traceback
import nest_asyncio
import time

nest_asyncio.apply()

# --- Config Telegram (certifique-se das variáveis no Heroku) ---
API_ID = int(config('API_ID'))
API_HASH = config('API_HASH')
BOT_TOKEN = config('BOT_TOKEN')
TARGET_CHAT_ID = int(config('TARGET_CHAT_ID'))  # ID numérico do chat/usuário/grupo

# --- Exchanges (mantendo as duas escolhidas) ---
exchanges_names = ['okx', 'cryptocom']

# --- Taxas e pares (mantidos como base) ---
spot_fees = {'okx': 0.10, 'cryptocom': 0.075}
margin_fee_per_hour = {'okx': 0.003, 'cryptocom': 0.03}

target_pairs = [
    'XRP/USDT','DOGE/USDT','BCH/USDT','LTC/USDT','UNI/USDT',
    'XLM/USDT','BNB/USDT','AVAX/USDT','APT/USDT','AAVE/USDT',
    'ETH/USDT','BTC/USDT','SOL/USDT','ADA/USDT','DOT/USDT',
    'LINK/USDT','MATIC/USDT','ATOM/USDT','FTM/USDT','TRX/USDT',
    'EOS/USDT','NEAR/USDT','ALGO/USDT','VET/USDT','ICP/USDT',
    'FIL/USDT','SAND/USDT','MANA/USDT','THETA/USDT','AXS/USDT'
]

trade_amount_usdt = 1.0

# --- Telethon client e fila de mensagens ---
client = TelegramClient('bot', API_ID, API_HASH)
telegram_connected = False
telegram_send_queue: asyncio.Queue = asyncio.Queue()

# --- Exchanges dict ---
exchanges = {}

# --- Helper para encontrar classe ccxt ---
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

# --- Inicialização das exchanges (no loop) ---
async def init_exchanges():
    global exchanges
    exchanges = {}
    for name in exchanges_names:
        cls = get_ccxt_exchange_class(name)
        if not cls:
            print(f"[WARN] Classe ccxt para '{name}' não encontrada — ignorada.")
            continue
        try:
            ex = cls({'enableRateLimit': True})
            exchanges[name] = ex
            print(f"[INFO] Iniciada exchange: {name}")
        except Exception as e:
            print(f"[ERROR] Falha ao inicializar {name}: {e}")
            traceback.print_exc()

# --- Load markets robusto ---
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
            try:
                await exchanges[name].close()
            except:
                pass
            del exchanges[name]
    return markets

# --- Filtra pares comuns ---
def filter_common_pairs(markets):
    if not markets:
        return []
    sets = [set(m.keys()) for m in markets.values() if m]
    common = set.intersection(*sets) if sets else set()
    selected = [p for p in target_pairs if p in common]
    extras = list(common - set(target_pairs))
    return selected + extras[: max(0, 30 - len(selected))]

# --- Busca order books ---
async def fetch_order_book(exchange, name, symbol):
    limit = 20 if name == 'kucoin' else 5
    try:
        ob = await exchange.fetch_order_book(symbol, limit=limit)
        bid = ob['bids'][0][0] if ob.get('bids') else None
        ask = ob['asks'][0][0] if ob.get('asks') else None
        return (name, symbol, bid, ask)
    except Exception as e:
        # não interrompe o loop por erro num par/exchange
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

# --- Detecta oportunidades ---
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

# --- Enfileira mensagens se Telegram não pronto, ou envia imediatamente ---
async def send_telegram_message(message):
    """
    Se o Telegram estiver conectado, envia na hora.
    Se não, adiciona na fila para envio posterior.
    """
    global telegram_connected
    if telegram_connected:
        try:
            await client.send_message(TARGET_CHAT_ID, message)
            print("[INFO] Mensagem enviada ao Telegram.")
        except Exception as e:
            print(f"[ERROR] Falha ao enviar mensagem (tentando enfileirar): {e}")
            traceback.print_exc()
            # se falhar ao enviar diretamente, enfileira para tentar depois
            await telegram_send_queue.put(message)
    else:
        print("[WARN] Telegram não conectado — enfileirando mensagem.")
        await telegram_send_queue.put(message)

# --- Worker que garante conexão com Telegram e faz flush da fila ---
async def telegram_connection_worker():
    """
    Tenta garantir que o client do Telethon está pronto (get_me) com retries.
    Quando conectado, esvazia a fila de mensagens.
    Executar isso em background assim que client.start() for chamado.
    """
    global telegram_connected
    # Tentativas com backoff
    max_attempts = 8
    attempt = 0
    while attempt < max_attempts and not telegram_connected:
        attempt += 1
        try:
            me = await client.get_me()
            if me:
                telegram_connected = True
                print(f"[INFO] Telegram conectado como: {me.username or me.first_name}")
                break
        except Exception as e:
            print(f"[WARN] Telethon get_me falhou (attempt {attempt}/{max_attempts}): {e}")
        await asyncio.sleep(min(5 * attempt, 30))  # backoff
    if not telegram_connected:
        print("[ERROR] Não foi possível conectar ao Telegram após várias tentativas. Continuarei tentando em background.")
    # Loop contínuo para tentar reconectar e enviar fila
    while True:
        if not telegram_connected:
            try:
                me = await client.get_me()
                if me:
                    telegram_connected = True
                    print(f"[INFO] Telegram reconectado: {me.username or me.first_name}")
            except Exception as e:
                # ainda não conectado
                # apenas log e aguardar
                print(f"[DEBUG] Reconexão Telegram falhou: {e}")
                await asyncio.sleep(20)
                continue
        # Se conectado, flush da fila
        try:
            while not telegram_send_queue.empty():
                msg = await telegram_send_queue.get()
                try:
                    await client.send_message(TARGET_CHAT_ID, msg)
                    print("[INFO] Mensagem da fila enviada.")
                except Exception as e:
                    print(f"[ERROR] Falha ao enviar mensagem da fila, re-enfileirando: {e}")
                    traceback.print_exc()
                    # re-put com delay
                    await asyncio.sleep(2)
                    await telegram_send_queue.put(msg)
                    break  # sai do loop para evitar loop tight
        except Exception as e:
            print(f"[ERROR] Erro ao processar fila Telegram: {e}")
            traceback.print_exc()
        await asyncio.sleep(5)

# --- Handlers de comando (registrados antes do start, funcionarão após client.start) ---
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
    msg = f"Valor de trade atual: {trade_amount_usdt} USDT\nExchanges ativas: {', '.join(exchanges.keys())}"
    await event.respond(msg)

# --- Loop principal de arbitragem ---
async def main_loop():
    try:
        await init_exchanges()
        markets = await load_markets()
        pairs = filter_common_pairs(markets)
        if not pairs:
            msg = "⚠️ Bot iniciado, mas não encontrou pares comuns."
            await send_telegram_message(msg)
            print(msg)
        else:
            msg = f"Bot iniciado com {len(pairs)} pares monitorados."
            await send_telegram_message(msg)
            print(msg)

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
                    print("[INFO] Oportunidade enviada/na fila.")
            except Exception as e:
                print(f"[ERROR] Erro no loop principal: {e}")
                traceback.print_exc()
            await asyncio.sleep(300)  # 5 minutos
    finally:
        for name, ex in exchanges.items():
            try:
                await ex.close()
            except Exception as e:
                print(f"[WARN] Falha ao fechar {name}: {e}")
        print("[INFO] Conexões fechadas.")

# --- Inicialização completa do bot com worker do Telegram ---
async def run_bot():
    try:
        # Start do Telethon (bot token)
        print("[INFO] Iniciando Telethon...")
        await client.start(bot_token=BOT_TOKEN)
        print("[INFO] Telethon start() retornou.")
        # Dispara worker em background que garante conexão e faz flush da fila
        asyncio.create_task(telegram_connection_worker())
        # Inicia main loop de arbitragem e mantém o client rodando para handlers
        await asyncio.gather(main_loop(), client.run_until_disconnected())
    except Exception as e:
        print(f"[FATAL] Erro ao iniciar bot: {e}")
        traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(run_bot())
