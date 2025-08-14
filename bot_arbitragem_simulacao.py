# -*- coding: utf-8 -*-

import asyncio
import traceback
import nest_asyncio
from decouple import config
from telethon import TelegramClient, events
import pandas as pd
from collections import deque
import time

# Aplica o nest_asyncio para compatibilidade
nest_asyncio.apply()

try:
    import ccxt.async_support as ccxt
except ImportError:
    print("[FATAL] A biblioteca 'ccxt' não foi encontrada. Instale-a com: pip install ccxt")
    exit()

# --- 1. CONFIGURAÇÃO PRINCIPAL E DE RISCO ---

# --- MODO DE OPERAÇÃO ---
DRY_RUN = True  # True = Simulação, False = Execução Real. MANTENHA TRUE PARA TESTAR.

# --- GERENCIAMENTO DE CAPITAL E RISCO ---
TRADE_MODE = 'FIXED'      # 'FIXED' (valor fixo) ou 'PERCENTAGE' (percentual da banca)
TRADE_VALUE = 1.0         # 1.0 USDT se for 'FIXED', ou 2.0 (para 2%) se for 'PERCENTAGE'
MIN_USDT_BALANCE = 10.0   # Saldo mínimo em USDT para operar em uma exchange
MAX_TRADES_PER_HOUR = 5   # Limite de trades para evitar over-trading

# --- CONFIGURAÇÕES GERAIS ---
MIN_PROFIT_THRESHOLD = 0.5 # Lucro mínimo de 0.5% para acionar um trade
LOOP_SLEEP_SECONDS = 90    # Verificar a cada 1.5 minutos

# --- CARREGAMENTO DE CREDENCIAIS ---
try:
    API_ID = int(config('API_ID'))
    API_HASH = config('API_HASH')
    BOT_TOKEN = config('BOT_TOKEN')
    TARGET_CHAT_ID = int(config('TARGET_CHAT_ID'))
    API_KEYS = {
        'okx': {'apiKey': config('OKX_API_KEY', default=None), 'secret': config('OKX_SECRET', default=None), 'password': config('OKX_PASSWORD', default=None)},
        'bybit': {'apiKey': config('BYBIT_API_KEY', default=None), 'secret': config('BYBIT_SECRET', default=None)},
        'kucoin': {'apiKey': config('KUCOIN_API_KEY', default=None), 'secret': config('KUCOIN_SECRET', default=None), 'password': config('KUCOIN_PASSWORD', default=None)},
        'cryptocom': {'apiKey': config('CRYPTOCOM_API_KEY', default=None), 'secret': config('CRYPTOCOM_SECRET', default=None)},
        'gateio': {'apiKey': config('GATEIO_API_KEY', default=None), 'secret': config('GATEIO_SECRET', default=None)},
    }
except Exception as e:
    print(f"[FATAL] Erro ao carregar configurações do arquivo .env: {e}")
    exit()

# --- VARIÁVEIS GLOBAIS ---
EXCHANGES_TO_MONITOR = list(API_KEYS.keys())
TARGET_PAIRS = ['XRP/USDT','DOGE/USDT','BCH/USDT','LTC/USDT','UNI/USDT','ETH/USDT','BTC/USDT','SOL/USDT','ADA/USDT','DOT/USDT','LINK/USDT','MATIC/USDT','ATOM/USDT']
active_exchanges = {}
telegram_client = TelegramClient('bot_session', API_ID, API_HASH)
telegram_ready = False
telegram_chat_entity = None
trade_timestamps = deque() # Fila para rastrear os horários dos trades

# --- 2. FUNÇÕES DE INICIALIZAÇÃO E COLETA DE DADOS ---

async def initialize_exchanges():
    """Instancia as exchanges e carrega as credenciais de API."""
    global active_exchanges
    print("[INFO] Inicializando exchanges com credenciais de API...")
    for name, credentials in API_KEYS.items():
        if not credentials.get('apiKey'):
            print(f"[WARN] Credenciais para '{name}' não encontradas. Será usada em modo público.")
        try:
            exchange_class = getattr(ccxt, name)
            instance = exchange_class({**credentials, 'enableRateLimit': True})
            active_exchanges[name] = instance
            print(f"[INFO] Instância da exchange '{name}' criada.")
        except Exception as e:
            print(f"[ERROR] Falha ao instanciar a exchange '{name}': {e}")

async def load_all_markets():
    """Carrega os mercados de todas as exchanges e remove as que falharem."""
    global active_exchanges
    print("[INFO] Carregando mercados de todas as exchanges...")
    tasks = {name: ex.load_markets() for name, ex in active_exchanges.items()}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    failed_exchanges = []
    for (name, ex), result in zip(active_exchanges.items(), results):
        if isinstance(result, Exception):
            print(f"[ERROR] Falha ao carregar mercados para '{name}': {result}. Exchange desativada.")
            failed_exchanges.append(name)
        else:
            print(f"[INFO] Mercados para '{name}' carregados ({len(ex.markets)} pares).")
    for name in failed_exchanges:
        if name in active_exchanges:
            await active_exchanges[name].close()
            del active_exchanges[name]

def get_common_pairs():
    """Filtra e retorna os pares de moedas que existem em TODAS as exchanges ativas."""
    if len(active_exchanges) < 2: return []
    sets_of_pairs = [set(ex.markets.keys()) for ex in active_exchanges.values()]
    common_symbols = set.intersection(*sets_of_pairs)
    monitored_pairs = [p for p in TARGET_PAIRS if p in common_symbols]
    print(f"[INFO] Encontrados {len(monitored_pairs)} pares comuns para monitorar.")
    return monitored_pairs

async def fetch_order_book(exchange_name, symbol):
    """Busca o livro de ofertas para um único par em uma exchange."""
    exchange = active_exchanges.get(exchange_name)
    if not exchange: return None
    try:
        order_book = await exchange.fetch_order_book(symbol, limit=5)
        bid = order_book['bids'][0][0] if order_book.get('bids') else None
        ask = order_book['asks'][0][0] if order_book.get('asks') else None
        if bid and ask:
            return {'name': exchange_name, 'symbol': symbol, 'bid': bid, 'ask': ask}
    except Exception:
        pass
    return None

async def fetch_all_order_books(pairs_to_check):
    """Busca todos os livros de ofertas de forma concorrente."""
    tasks = [fetch_order_book(name, symbol) for symbol in pairs_to_check for name in active_exchanges.keys()]
    results = await asyncio.gather(*tasks)
    structured_data = {}
    for res in filter(None, results):
        structured_data.setdefault(res['symbol'], {})[res['name']] = {'bid': res['bid'], 'ask': res['ask']}
    return structured_data

def find_arbitrage_opportunities(data):
    """Analisa os dados e identifica oportunidades de arbitragem."""
    opportunities = []
    for symbol, exchanges_data in data.items():
        if len(exchanges_data) < 2: continue
        for buy_exchange, buy_data in exchanges_data.items():
            for sell_exchange, sell_data in exchanges_data.items():
                if buy_exchange == sell_exchange: continue
                buy_price, sell_price = buy_data.get('ask'), sell_data.get('bid')
                if buy_price and sell_price and buy_price > 0:
                    profit_percent = ((sell_price - buy_price) / buy_price) * 100
                    if profit_percent > MIN_PROFIT_THRESHOLD:
                        opportunities.append({
                            'symbol': symbol,
                            'buy_exchange': buy_exchange.upper(), 'buy_price': buy_price,
                            'sell_exchange': sell_exchange.upper(), 'sell_price': sell_price,
                            'profit_percent': profit_percent,
                        })
    return sorted(opportunities, key=lambda x: x['profit_percent'], reverse=True)

# --- 3. FUNÇÕES DE EXECUÇÃO E GERENCIAMENTO DE RISCO ---

async def get_trade_amount_usdt(exchange_name):
    """Calcula o valor do trade em USDT com base no modo configurado e verifica saldo."""
    if TRADE_MODE == 'FIXED':
        return TRADE_VALUE
    
    if TRADE_MODE == 'PERCENTAGE':
        try:
            exchange = active_exchanges.get(exchange_name.lower())
            balance = await exchange.fetch_balance()
            usdt_balance = balance['free'].get('USDT', 0)
            
            if usdt_balance < MIN_USDT_BALANCE:
                await send_telegram_message(f"📉 **Aviso de Saldo Baixo** em `{exchange_name}`: Saldo de {usdt_balance:.2f} USDT é menor que o mínimo de {MIN_USDT_BALANCE:.2f} USDT. A exchange será ignorada para compras.")
                return 0
            
            return (usdt_balance * TRADE_VALUE) / 100
        except Exception as e:
            await send_telegram_message(f"🔥 Erro ao buscar saldo para cálculo percentual em `{exchange_name}`: {e}")
            return 0
    return 0

def check_trade_limit():
    """Verifica se o limite de trades por hora foi atingido."""
    now = time.time()
    while trade_timestamps and trade_timestamps[0] < now - 3600:
        trade_timestamps.popleft()
    return len(trade_timestamps) < MAX_TRADES_PER_HOUR

async def place_order(exchange_name, symbol, side, amount, price):
    """Envia uma ordem de limite para a exchange e retorna o resultado."""
    exchange = active_exchanges.get(exchange_name.lower())
    if not exchange: return {'error': f"Exchange {exchange_name} não encontrada."}

    if DRY_RUN:
        msg = f"**[SIMULAÇÃO]** Ordem `{side.upper()}` de `{amount:.6f} {symbol.split('/')[0]}` em `{exchange_name}` a preço `{price:.6f}`"
        print(msg)
        await send_telegram_message(msg)
        return {'id': 'simulated_order_123', 'status': 'closed', 'symbol': symbol, 'side': side, 'amount': amount, 'price': price}

    try:
        print(f"EXECUTANDO ORDEM REAL: {side.upper()} {amount:.6f} {symbol} em {exchange_name} a {price:.6f}")
        order = await exchange.create_limit_order(symbol, side, amount, price)
        await send_telegram_message(f"✅ Ordem `{side.upper()}` enviada para `{exchange_name}`. ID: `{order['id']}`")
        # Em um bot real, aqui entraria um loop para monitorar o status da ordem até ser 'closed'
        return order
    except Exception as e:
        await send_telegram_message(f"🔥 FALHA AO ENVIAR ORDEM para `{exchange_name}`: {e}")
        return {'error': str(e)}

async def execute_arbitrage_trade(opportunity):
    """Orquestra a execução de um trade de arbitragem com todas as verificações de risco."""
    if not check_trade_limit():
        print(f"[INFO] Limite de trades por hora atingido. Oportunidade para {opportunity['symbol']} ignorada.")
        return

    buy_ex_name = opportunity['buy_exchange']
    trade_amount_usdt = await get_trade_amount_usdt(buy_ex_name)
    if trade_amount_usdt <= 0:
        print(f"[INFO] Trade para {opportunity['symbol']} em {buy_ex_name} abortado devido a saldo/configuração.")
        return

    symbol = opportunity['symbol']
    sell_ex_name = opportunity['sell_exchange']
    buy_price, sell_price = opportunity['buy_price'], opportunity['sell_price']
    amount_to_trade = trade_amount_usdt / buy_price

    await send_telegram_message(f"🚀 **Tentando executar arbitragem para {symbol} com {trade_amount_usdt:.2f} USDT!**\nComprar em `{buy_ex_name}` | Vender em `{sell_ex_name}`")
    
    buy_order = await place_order(buy_ex_name, symbol, 'buy', amount_to_trade, buy_price)

    if not buy_order or 'error' in buy_order or buy_order.get('status') != 'closed':
        await send_telegram_message(f"❌ **Perna de COMPRA falhou!** Trade abortado. Motivo: {buy_order.get('error', 'Status não foi `closed`')}")
        return

    await send_telegram_message(f"✅ Perna de COMPRA executada. Executando VENDA...")
    
    amount_to_sell = buy_order['amount']
    sell_order = await place_order(sell_ex_name, symbol, 'sell', amount_to_sell, sell_price)

    if not sell_order or 'error' in sell_order or sell_order.get('status') != 'closed':
        await send_telegram_message(f"🚨 **ALERTA DE RISCO: PERNA DE VENDA FALHOU!**\nCompramos `{amount_to_sell:.6f} {symbol.split('/')[0]}` mas falhamos ao vender. **AÇÃO MANUAL NECESSÁRIA!**")
        return

    profit = (sell_price - buy_price) * amount_to_sell
    await send_telegram_message(f"🎉 **SUCESSO!** Arbitragem para `{symbol}` concluída!\nLucro estimado: **{profit:.4f} {symbol.split('/')[1]}**")
    
    trade_timestamps.append(time.time())

# --- 4. TELEGRAM: COMUNICAÇÃO E COMANDOS ---

async def send_telegram_message(message):
    """Envia uma mensagem para o chat alvo do Telegram."""
    if not telegram_ready or not telegram_chat_entity:
        print("[WARN] Telegram não pronto. Mensagem no console:", message)
        return
    try:
        await telegram_client.send_message(telegram_chat_entity, message, parse_mode='md')
    except Exception as e:
        print(f"[ERROR] Falha ao enviar mensagem no Telegram: {e}")

@telegram_client.on(events.NewMessage(pattern='/status'))
async def status_handler(event):
    """Handler para o comando /status."""
    active_names = ", ".join(active_exchanges.keys()) if active_exchanges else "Nenhuma"
    mode = "SIMULAÇÃO (DRY RUN)" if DRY_RUN else "EXECUÇÃO REAL"
    trade_config_msg = f"`{TRADE_VALUE:.2f}%` do saldo" if TRADE_MODE == 'PERCENTAGE' else f"`{TRADE_VALUE:.2f} USDT` (Fixo)"
    recent_trades = [t for t in trade_timestamps if t > time.time() - 3600]
    msg = (
        f"**🤖 Status do Bot**\n\n"
        f"**Modo de Operação:** `{mode}`\n"
        f"**Gerenciamento de Capital:** {trade_config_msg}\n"
        f"**Lucro Mínimo por Trade:** `{MIN_PROFIT_THRESHOLD}%`\n"
        f"**Trades na Última Hora:** `{len(recent_trades)} / {MAX_TRADES_PER_HOUR}`\n"
        f"**Exchanges Ativas:** `{active_names}`"
    )
    await event.respond(msg)

@telegram_client.on(events.NewMessage(pattern='/setprofit (\\d+(\\.\\d+)?)'))
async def set_profit_handler(event):
    """Handler para o comando /setprofit <porcentagem>."""
    global MIN_PROFIT_THRESHOLD
    try:
        value = float(event.pattern_match.group(1))
        if 0.1 <= value <= 10:
            MIN_PROFIT_THRESHOLD = value
            await event.respond(f"✅ Limiar de lucro ajustado para **{MIN_PROFIT_THRESHOLD:.2f}%**.")
        else:
            await event.respond("⚠️ Valor inválido. Informe um número entre 0.1 e 10.")
    except Exception:
        await event.respond("❌ Erro de formato. Use: `/setprofit 0.8`")

@telegram_client.on(events.NewMessage(pattern='/setmode (fixed|percentage) (\\d+(\\.\\d+)?)'))
async def set_mode_handler(event):
    """Handler para o comando /setmode <fixed|percentage> <valor>."""
    global TRADE_MODE, TRADE_VALUE
    try:
        mode = event.pattern_match.group(1).upper()
        value = float(event.pattern_match.group(2))
        TRADE_MODE = mode
        TRADE_VALUE = value
        if mode == 'FIXED':
            await event.respond(f"✅ Modo de trade alterado para **FIXO** com valor de **{value:.2f} USDT**.")
        elif mode == 'PERCENTAGE':
            await event.respond(f"✅ Modo de trade alterado para **PERCENTUAL** com valor de **{value:.2f}%** do saldo.")
    except Exception:
        await event.respond("❌ Erro de formato. Use:\n`/setmode fixed 10.5`\n`/setmode percentage 2`")

@telegram_client.on(events.NewMessage(pattern='/balances'))
async def balances_handler(event):
    """Handler para o comando /balances."""
    await event.respond("Buscando saldos... Isso pode levar um momento.")
    msg = "**💰 Saldos nas Exchanges (moedas com valor > $0.01)**\n\n"
    for name, ex in active_exchanges.items():
        try:
            balance = await ex.fetch_balance({'type': 'spot'}) # Pega saldo SPOT
            msg += f"**Exchange: {name.upper()}**\n"
            found_assets = False
            for currency, value in balance['total'].items():
                if value > 0:
                    msg += f"- `{currency}`: `{value:.6f}`\n"
                    found_assets = True
            if not found_assets:
                msg += "_Nenhum saldo encontrado._\n"
            msg += "\n"
        except Exception as e:
            msg += f"**Exchange: {name.upper()}**\n_Falha ao buscar saldo: {e}_\n\n"
    await event.respond(msg)

# --- 5. LOOP PRINCIPAL E EXECUÇÃO DO BOT ---

async def main_loop():
    """O loop principal que orquestra a busca contínua por oportunidades."""
    global telegram_ready, telegram_chat_entity
    try:
        print("[INFO] Conectando ao Telegram...")
        telegram_chat_entity = await client.get_entity(TARGET_CHAT_ID)
        telegram_ready = True
        print("[INFO] Cliente do Telegram conectado e pronto.")
    except Exception as e:
        print(f"[WARN] Não foi possível conectar ao Telegram: {e}. O bot continuará sem alertas.")

    await initialize_exchanges()
    await load_all_markets()
    
    if len(active_exchanges) < 2:
        await send_telegram_message("⚠️ **Bot encerrando:** Menos de duas exchanges ativas.")
        return

    common_pairs = get_common_pairs()
    if not common_pairs:
        await send_telegram_message("⚠️ **Aviso:** Nenhum par em comum encontrado. O bot continuará tentando.")
    else:
        await send_telegram_message(f"✅ **Bot iniciado!** Monitorando {len(common_pairs)} pares.")

    while True:
        try:
            print(f"\n[{pd.Timestamp.now()}] Iniciando ciclo de verificação...")
            order_book_data = await fetch_all_order_books(common_pairs)
            opportunities = find_arbitrage_opportunities(order_book_data)
            
            if opportunities:
                best_opportunity = opportunities[0]
                print(f"[SUCCESS] Oportunidade encontrada! {best_opportunity['symbol']} com {best_opportunity['profit_percent']:.2f}% de lucro.")
                await execute_arbitrage_trade(best_opportunity)
            else:
                print("[INFO] Nenhuma oportunidade lucrativa encontrada neste ciclo.")

        except Exception as e:
            print(f"[ERROR] Erro inesperado no loop principal: {e}")
            traceback.print_exc()
            await send_telegram_message(f"🐞 **Alerta de Bug!** Ocorreu um erro grave no loop principal: `{e}`. Verifique os logs.")
            await asyncio.sleep(60)

        print(f"Ciclo concluído. Aguardando {LOOP_SLEEP_SECONDS} segundos...")
        await asyncio.sleep(LOOP_SLEEP_SECONDS)

async def main():
    """Função principal que gerencia o ciclo de vida do bot."""
    try:
        await telegram_client.start(bot_token=BOT_TOKEN)
        print("[INFO] Cliente do Telegram iniciado.")
        await asyncio.gather(main_loop(), telegram_client.run_until_disconnected())
    except Exception as e:
        print(f"[FATAL] Um erro crítico impediu o bot de funcionar: {e}")
        traceback.print_exc()
    finally:
        if telegram_client.is_connected():
            await telegram_client.disconnect()
        print("[INFO] Bot finalizado.")

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    try:
        print("[INFO] Iniciando o bot...")
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\n[INFO] Desligamento solicitado pelo usuário.")
    finally:
        print("[INFO] Encerrando.")
