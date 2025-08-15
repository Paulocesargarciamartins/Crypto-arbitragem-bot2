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

# --- 1. CONFIGURAÇÃO PRINCIPAL (MODO FUTUROS) ---

# --- MODO DE OPERAÇÃO ---
DRY_RUN = True  # True = Simulação, False = Execução Real. MANTENHA TRUE PARA TESTAR.

# --- GERENCIAMENTO DE CAPITAL E RISCO ---
TRADE_MODE = 'FIXED'      # 'FIXED' (valor fixo) ou 'PERCENTAGE' (percentual da banca)
TRADE_VALUE = 1.0         # 1.0 USDT se for 'FIXED', ou 2.0 (para 2%) se for 'PERCENTAGE'
MIN_USDT_BALANCE = 10.0   # Saldo mínimo em USDT para operar em uma exchange
MAX_TRADES_PER_HOUR = 5   # Limite de trades para evitar over-trading
LEVERAGE = 5              # Alavancagem a ser usada. CUIDADO: Aumenta tanto o lucro quanto o prejuízo.

# --- CONFIGURAÇÕES GERAIS ---
MIN_PROFIT_THRESHOLD = 0.3 # No mercado de futuros, as taxas são menores, então podemos buscar lucros menores.
LOOP_SLEEP_SECONDS = 90    # Verificar a cada 1.5 minutos

MAX_RETRIES = 3
RETRY_DELAY = 5 # seconds

MIN_VOLUME_THRESHOLD = 1000000 # Volume mínimo em USDT para considerar um par

# --- CARREGAMENTO DE CREDENCIAIS ---
try:
    API_ID = int(config('API_ID'))
    API_HASH = config('API_HASH')
    BOT_TOKEN = config('BOT_TOKEN')
    TARGET_CHAT_ID = int(config('TARGET_CHAT_ID'))
    # CONFIGURAÇÃO PARA 7 EXCHANGES (usará API pública se as chaves não forem encontradas)
    API_KEYS = {
        'okx': {'apiKey': config('OKX_API_KEY', default=None), 'secret': config('OKX_SECRET', default=None), 'password': config('OKX_PASSWORD', default=None)},
        'bybit': {'apiKey': config('BYBIT_API_KEY', default=None), 'secret': config('BYBIT_SECRET', default=None)},
        'kucoin': {'apiKey': config('KUCOIN_API_KEY', default=None), 'secret': config('KUCOIN_SECRET', default=None), 'password': config('KUCOIN_PASSWORD', default=None)},
        'gateio': {'apiKey': config('GATEIO_API_KEY', default=None), 'secret': config('GATEIO_SECRET', default=None)},
        'mexc': {'apiKey': config('MEXC_API_KEY', default=None), 'secret': config('MEXC_SECRET', default=None)},
        'bitget': {'apiKey': config('BITGET_API_KEY', default=None), 'secret': config('BITGET_SECRET', default=None), 'password': config('BITGET_PASSWORD', default=None)},
    }
except Exception as e:
    print(f"[FATAL] Erro ao carregar configurações do arquivo .env: {e}")
    exit()

# --- VARIÁVEIS GLOBAIS ---
EXCHANGES_TO_MONITOR = list(API_KEYS.keys())
# Pares de Futuros. O formato :USDT indica que a garantia é em USDT.
TARGET_PAIRS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT', 'XRP/USDT:USDT', 'ADA/USDT:USDT', 
    'AVAX/USDT:USDT', 'DOGE/USDT:USDT', 'TRX/USDT:USDT', 'DOT/USDT:USDT', 'MATIC/USDT:USDT', 'LTC/USDT:USDT',
    'BCH/USDT:USDT', 'ATOM/USDT:USDT', 'NEAR/USDT:USDT', 'APT/USDT:USDT', 'LINK/USDT:USDT', 'UNI/USDT:USDT',
    'OP/USDT:USDT', 'ARB/USDT:USDT', 'PEPE/USDT:USDT', 'WLD/USDT:USDT', 'SHIB/USDT:USDT'
]

active_exchanges = {}
telegram_client = TelegramClient('bot_session', API_ID, API_HASH)
telegram_ready = False
telegram_chat_entity = None
trade_timestamps = deque()

# --- 2. FUNÇÕES DE INICIALIZAÇÃO E COLETA DE DADOS (MODO FUTUROS) ---

async def initialize_exchanges():
    """Instancia as exchanges e as configura para o mercado de FUTUROS (SWAP)."""
    global active_exchanges
    print("[INFO] Inicializando exchanges em MODO FUTUROS...")
    for name, credentials in API_KEYS.items():
        try:
            exchange_class = getattr(ccxt, name)
            instance = exchange_class({**credentials, 'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
            
            if credentials.get('apiKey'):
                try:
                    await instance.set_leverage(LEVERAGE, symbol=None)
                    print(f"[INFO] Alavancagem definida para {LEVERAGE}x em '{name}'.")
                except Exception:
                    print(f"[WARN] Não foi possível definir a alavancagem para '{name}'. Verifique a configuração manual na exchange.")
            
            active_exchanges[name] = instance
            print(f"[INFO] Instância da exchange '{name}' criada para futuros.")
        except Exception as e:
            print(f"[ERROR] Falha ao instanciar a exchange '{name}': {e}")

async def load_all_markets():
    """Carrega os mercados de todas as exchanges e remove as que falharem."""
    global active_exchanges
    print("[INFO] Carregando mercados de futuros de todas as exchanges...")
    tasks = {name: ex.load_markets() for name, ex in active_exchanges.items()}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    failed_exchanges = []
    for (name, ex), result in zip(active_exchanges.items(), results):
        if isinstance(result, Exception):
            print(f"[ERROR] Falha ao carregar mercados para '{name}': {result}. Exchange desativada.")
            failed_exchanges.append(name)
        else:
            print(f"[INFO] Mercados de futuros para '{name}' carregados ({len(ex.markets)} pares).")
    for name in failed_exchanges:
        if name in active_exchanges:
            await active_exchanges[name].close()
            del active_exchanges[name]

async def get_common_pairs():
    """Filtra e retorna os pares de moedas que existem em TODAS as exchanges ativas e que atendem ao volume mínimo."""
    if len(active_exchanges) < 2: 
        return []
    
    sets_of_pairs = [set(ex.markets.keys()) for ex in active_exchanges.values()]
    common_symbols = set.intersection(*sets_of_pairs)
    monitored_pairs_with_volume = []
    
    common_and_target_pairs = [p for p in TARGET_PAIRS if p in common_symbols]
    
    print(f"[INFO] Encontrados {len(common_and_target_pairs)} pares de futuros comuns para verificar volume.")

    if not common_and_target_pairs:
        return []

    tasks = []
    for symbol in common_and_target_pairs:
        for ex in active_exchanges.values():
            tasks.append(ex.fetch_ticker(symbol))
    
    tickers = await asyncio.gather(*tasks, return_exceptions=True)
    
    tickers_by_symbol_and_exchange = {}
    idx = 0
    for symbol in common_and_target_pairs:
        tickers_by_symbol_and_exchange[symbol] = {}
        for name in active_exchanges.keys():
            tickers_by_symbol_and_exchange[symbol][name] = tickers[idx]
            idx += 1
            
    monitored_pairs = []
    for symbol, exchange_data in tickers_by_symbol_and_exchange.items():
        total_volume = 0
        for name, ticker in exchange_data.items():
            if not isinstance(ticker, Exception) and ticker and ticker.get("quoteVolume"):
                total_volume += ticker["quoteVolume"]
        
        if total_volume >= MIN_VOLUME_THRESHOLD:
            monitored_pairs.append(symbol)
        else:
            print(f"[INFO] Par {symbol} ignorado devido ao baixo volume total ({total_volume:.2f} USDT).")

    print(f"[INFO] Encontrados {len(monitored_pairs)} pares de futuros com volume adequado para monitorar.")
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

# --- 3. FUNÇÕES DE EXECUÇÃO E GERENCIAMENTO DE RISCO (MODO FUTUROS) ---

async def get_trade_amount_usdt(exchange_name):
    """Calcula o valor do trade em USDT com base no saldo de FUTUROS."""
    exchange = active_exchanges.get(exchange_name.lower())
    if not exchange or not exchange.apiKey:
        print(f"[INFO] Sem chave de API para {exchange_name}. Usando valor de trade fixo para simulação.")
        return TRADE_VALUE if TRADE_MODE == 'FIXED' else 1.0

    if TRADE_MODE == 'FIXED':
        return TRADE_VALUE
    
    if TRADE_MODE == 'PERCENTAGE':
        try:
            balance = await exchange.fetch_balance(params={'type': 'swap'})
            usdt_balance = balance.get('USDT', {}).get('free', 0)
            
            if usdt_balance < MIN_USDT_BALANCE:
                await send_telegram_message(f"📉 **Aviso de Saldo Baixo (Futuros)** em `{exchange_name}`: Saldo de {usdt_balance:.2f} USDT é menor que o mínimo de {MIN_USDT_BALANCE:.2f} USDT. Trade abortado.")
                return 0
            
            return (usdt_balance * TRADE_VALUE) / 100
        except Exception as e:
            await send_telegram_message(f"🔥 Erro ao buscar saldo de futuros em `{exchange_name}`: {e}")
            return 0
    return 0

def check_trade_limit():
    """Verifica se o limite de trades por hora foi atingido."""
    now = time.time()
    while trade_timestamps and trade_timestamps[0] < now - 3600:
        trade_timestamps.popleft()
    return len(trade_timestamps) < MAX_TRADES_PER_HOUR

async def place_order(exchange_name, symbol, side, amount, price):
    """Envia uma ordem de limite para o mercado de FUTUROS com retries."""
    exchange = active_exchanges.get(exchange_name.lower())
    if not exchange: return {"error": f"Exchange {exchange_name} não encontrada."}

    if DRY_RUN:
        msg = f"**[SIMULAÇÃO FUTUROS]** Ordem `{side.upper()}` de `{amount:.6f} {symbol.split(':')[0]}` em `{exchange_name}` a preço `{price:.6f}`"
        print(msg)
        await send_telegram_message(msg)
        return {"id": "simulated_order_123", "status": "closed", "symbol": symbol, "side": side, "amount": amount, "price": price}

    if not exchange.apiKey:
        return {"error": "Chave de API não configurada para execução real."}

    for i in range(MAX_RETRIES):
        try:
            print(f"EXECUTANDO ORDEM REAL (FUTUROS): {side.upper()} {amount:.6f} {symbol} em {exchange_name} a {price:.6f} (Tentativa {i+1}/{MAX_RETRIES})")
            order = await exchange.create_limit_order(symbol, side, amount, price)
            await send_telegram_message(f"✅ Ordem de FUTUROS `{side.upper()}` enviada para `{exchange_name}`. ID: `{order['id']}` (Tentativa {i+1}/{MAX_RETRIES})")
            return order
        except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
            error_msg = f"🌐 Erro de Rede/Timeout ao enviar ordem para `{exchange_name}` (Tentativa {i+1}/{MAX_RETRIES}): {e}"
            await send_telegram_message(f"🔥 {error_msg}")
            print(f"[ERROR] {error_msg}")
            if i < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY)
            else:
                return {"error": str(e)}
        except ccxt.ExchangeError as e:
            error_msg = f"🏦 Erro da Exchange ao enviar ordem para `{exchange_name}` (Tentativa {i+1}/{MAX_RETRIES}): {e}"
            await send_telegram_message(f"🔥 {error_msg}")
            print(f"[ERROR] {error_msg}")
            if i < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY)
            else:
                return {"error": str(e)}
        except Exception as e:
            error_msg = f"❓ Erro Inesperado ao enviar ordem para `{exchange_name}` (Tentativa {i+1}/{MAX_RETRIES}): {e}"
            await send_telegram_message(f"🔥 {error_msg}")
            print(f"[ERROR] {error_msg}")
            if i < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY)
            else:
                return {"error": str(e)}

async def close_open_position(exchange_name, symbol, side, amount):
    """Tenta fechar uma posição aberta em caso de erro na segunda perna do trade, com retries."""
    exchange = active_exchanges.get(exchange_name.lower())
    if not exchange: 
        await send_telegram_message(f"❌ Erro ao tentar fechar posição: Exchange {exchange_name} não encontrada.")
        return False

    for i in range(MAX_RETRIES):
        try:
            close_side = 'sell' if side == 'buy' else 'buy'
            print(f"TENTANDO FECHAR POSIÇÃO (FUTUROS): {close_side.upper()} {amount:.6f} {symbol} em {exchange_name} (Tentativa {i+1}/{MAX_RETRIES})")
            close_order = await exchange.create_market_order(symbol, close_side, amount)
            
            if close_order.get('status') == 'closed':
                await send_telegram_message(f"✅ Posição de {side.upper()} para {symbol} em {exchange_name} fechada com sucesso via ordem de mercado {close_side.upper()}.")
                return True
            else:
                await send_telegram_message(f"⚠️ Falha ao fechar posição de {side.upper()} para {symbol} em {exchange_name}. Status: {close_order.get('status')}. (Tentativa {i+1}/{MAX_RETRIES}).")
                if i < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    await send_telegram_message(f"🚨 FALHA CRÍTICA AO FECHAR POSIÇÃO: {side.upper()} para {symbol} em {exchange_name}. Ação manual NECESSÁRIA.")
                    return False
        except Exception as e:
            await send_telegram_message(f"🔥 Erro crítico ao tentar fechar posição de {side.upper()} para {symbol} em {exchange_name} (Tentativa {i+1}/{MAX_RETRIES}): {e}.")
            if i < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY)
            else:
                await send_telegram_message(f"🚨 FALHA CRÍTICA AO FECHAR POSIÇÃO: {side.upper()} para {symbol} em {exchange_name}. Ação manual NECESSÁRIA.")
                return False

async def cancel_all_open_orders(exchange_name, symbol):
    """Cancela todas as ordens abertas para um dado símbolo em uma exchange."""
    exchange = active_exchanges.get(exchange_name.lower())
    if not exchange: 
        print(f"[WARN] Exchange {exchange_name} não encontrada para cancelar ordens.")
        return False

    try:
        orders = await exchange.fetch_open_orders(symbol)
        if orders:
            print(f"[INFO] Encontradas {len(orders)} ordens abertas para {symbol} em {exchange_name}. Cancelando...")
            for order in orders:
                try:
                    await exchange.cancel_order(order['id'], symbol)
                    await send_telegram_message(f"✅ Ordem {order['id']} para {symbol} em {exchange_name} cancelada.")
                except Exception as e:
                    await send_telegram_message(f"⚠️ Falha ao cancelar ordem {order['id']} para {symbol} em {exchange_name}: {e}")
            return True
        else:
            print(f"[INFO] Nenhuma ordem aberta encontrada para {symbol} em {exchange_name}.")
            return False
    except Exception as e:
        await send_telegram_message(f"🔥 Erro ao buscar/cancelar ordens abertas em {exchange_name} para {symbol}: {e}")
        return False

async def execute_arbitrage_trade(opportunity):
    """Orquestra a execução de um trade de arbitragem com todas as verificações de risco."""
    try:
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

        await send_telegram_message(f"🚀 **Tentando executar arbitragem de FUTUROS para {symbol} com {trade_amount_usdt:.2f} USDT!**\nComprar em `{buy_ex_name}` | Vender em `{sell_ex_name}`")
        buy_order = await place_order(buy_ex_name, symbol, 'buy', amount_to_trade, buy_price)

        if not buy_order or 'error' in buy_order or buy_order.get('status') != 'closed':
            await send_telegram_message(f"❌ **Perna de COMPRA (LONG) falhou!** Trade abortado. Motivo: {buy_order.get('error', 'Status não foi `closed`')}. Verifique logs para mais detalhes.")
            return

        await send_telegram_message(f"✅ Perna de COMPRA (LONG) executada. Executando VENDA (SHORT)...")
        
        amount_to_sell = buy_order['amount']
        sell_order = await place_order(sell_ex_name, symbol, 'sell', amount_to_sell, sell_price)

        if not sell_order or 'error' in sell_order or sell_order.get('status') != 'closed':
            await send_telegram_message(f"🚨 **ALERTA DE RISCO: PERNA DE VENDA (SHORT) FALHOU!**\nPosição de compra de `{amount_to_sell:.6f} {symbol.split(':')[0]}` ficou aberta em `{buy_ex_name}`.\n**Tentando fechar automaticamente...**")
            await cancel_all_open_orders(sell_ex_name, symbol)
            await close_open_position(buy_ex_name, symbol, 'buy', amount_to_sell)
            return

        profit = (sell_price - buy_price) * amount_to_sell
        await send_telegram_message(f"🎉 **SUCESSO!** Arbitragem de FUTUROS para `{symbol}` concluída!\nLucro estimado: **{profit:.4f} USDT**")
        
        trade_timestamps.append(time.time())
    except Exception as e:
        await send_telegram_message(f"🔥 Erro inesperado durante a execução do trade de arbitragem para {opportunity['symbol']}: {e}")
        print(f"[ERROR] Erro inesperado em execute_arbitrage_trade: {traceback.format_exc()}")

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
        f"**🤖 Status do Bot (MODO FUTUROS)**\n\n"
        f"**Modo de Operação:** `{mode}`\n"
        f"**Alavancagem:** `{LEVERAGE}x`\n"
        f"**Gerenciamento de Capital:** {trade_config_msg}\n"
        f"**Lucro Mínimo por Trade:** `{MIN_PROFIT_THRESHOLD}%`\n"
        f"**Trades na Última Hora:** `{len(recent_trades)} / {MAX_TRADES_PER_HOUR}`\n"
        f"**Exchanges Ativas:** `{active_names}`"
    )
    await event.respond(msg)

@telegram_client.on(events.NewMessage(pattern=r'/setprofit (\d+(\.\d+)?)'))
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

@telegram_client.on(events.NewMessage(pattern=r'/setmode (fixed|percentage) (\d+(\.\d+)?)'))
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
    """Handler para o comando /balances, agora para a carteira de FUTUROS."""
    await event.respond("Buscando saldos de **FUTUROS**... Isso pode levar um momento.")
    msg = "**💰 Saldos na Carteira de Futuros (Garantia)**\n\n"
    for name, ex in active_exchanges.items():
        msg += f"**Exchange: {name.upper()}**\n"
        if not ex.apiKey:
            msg += "_Chave de API não configurada para esta exchange._\n\n"
            continue
        try:
            balance = await ex.fetch_balance(params={'type': 'swap'})
            usdt_balance = balance.get('USDT', {})
            total_usdt = usdt_balance.get('total', 0)
            if total_usdt > 0:
                msg += f"- `USDT`: `{total_usdt:.2f}`\n"
            else:
                msg += "_Nenhum saldo de garantia em USDT encontrado._\n"
            msg += "\n"
        except Exception as e:
            msg += f"_Falha ao buscar saldo de futuros: {e}_\n\n"
    await event.respond(msg)

@telegram_client.on(events.NewMessage(pattern=r'/fechar_posicao (\S+) (\S+) (\d+(\.\d+)?)'))
async def force_close_position_handler(event):
    """Handler para o comando /fechar_posicao, ativando fechamento manual em caso de falha crítica."""
    try:
        exchange_name = event.pattern_match.group(1).lower()
        symbol = event.pattern_match.group(2).upper()
        amount = float(event.pattern_match.group(3))
        
        await event.respond(f"⚠️ Comando manual recebido: Fechando posição de `{amount:.6f}` de `{symbol}` em `{exchange_name}`...")
        
        # Chama a função de fechamento de posição, que já tem a lógica de retries
        success = await close_open_position(exchange_name, symbol, 'buy', amount)
        
        if success:
            await event.respond("✅ Ordem de fechamento manual executada com sucesso.")
        else:
            await event.respond("❌ Falha na ordem de fechamento manual. Ação manual na exchange é necessária.")

    except Exception as e:
        await event.respond("❌ Erro de formato ou inesperado. Use: `/fechar_posicao <exchange> <par> <quantidade>`")
        print(f"[ERROR] Erro no comando /fechar_posicao: {traceback.format_exc()}")


# --- 5. LOOP PRINCIPAL E EXECUÇÃO DO BOT ---

async def main_loop():
    """O loop principal que orquestra a busca contínua por oportunidades."""
    global telegram_ready, telegram_chat_entity
    try:
        print("[INFO] Conectando ao Telegram...")
        telegram_chat_entity = await telegram_client.get_entity(TARGET_CHAT_ID)
        telegram_ready = True
        print("[INFO] Cliente do Telegram conectado e pronto.")
    except Exception as e:
        print(f"[WARN] Não foi possível conectar ao Telegram: {e}. O bot continuará sem alertas.")

    await initialize_exchanges()
    await load_all_markets()
    
    if len(active_exchanges) < 2:
        await send_telegram_message("⚠️ **Bot encerrando:** Menos de duas exchanges ativas.")
        return

    common_pairs = await get_common_pairs()
    if not common_pairs:
        await send_telegram_message("⚠️ **Aviso:** Nenhum par de futuros em comum encontrado.")
    else:
        await send_telegram_message(f"✅ **Bot iniciado em MODO FUTUROS!** Monitorando {len(common_pairs)} pares.")

    while True:
        try:
            print(f"\n[{pd.Timestamp.now()}] Iniciando ciclo de verificação de futuros...")
            order_book_data = await fetch_all_order_books(common_pairs)
            opportunities = find_arbitrage_opportunities(order_book_data)
            
            if opportunities:
                best_opportunity = opportunities[0]
                print(f"[SUCCESS] Oportunidade de FUTUROS encontrada! {best_opportunity['symbol']} com {best_opportunity['profit_percent']:.2f}% de lucro.")
                await execute_arbitrage_trade(best_opportunity)
            else:
                print("[INFO] Nenhuma oportunidade lucrativa de futuros encontrada neste ciclo.")

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
