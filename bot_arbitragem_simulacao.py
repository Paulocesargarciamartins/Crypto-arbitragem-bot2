import os
import asyncio
import logging
import ccxt.pro as ccxt_pro
import ccxt as ccxt_rest
import nest_asyncio
import time
from datetime import datetime
from decimal import Decimal
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# --- Aplica o patch para permitir loops aninhados ---
nest_asyncio.apply()

# --- Configurações básicas e chaves de API ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Variáveis de ambiente padrão, podem ser alteradas com comandos
DEFAULT_LUCRO_MINIMO_PORCENTAGEM = float(os.getenv("DEFAULT_LUCRO_MINIMO_PORCENTAGEM", 1.0))
DEFAULT_FEE_PERCENTAGE = float(os.getenv("DEFAULT_FEE_PERCENTAGE", 0.1))
DEFAULT_MIN_USDT_BALANCE = float(os.getenv("DEFAULT_MIN_USDT_BALANCE", 10.0))
DEFAULT_TOTAL_CAPITAL = float(os.getenv("DEFAULT_TOTAL_CAPITAL", 50.0))
DEFAULT_TRADE_PERCENTAGE = float(os.getenv("DEFAULT_TRADE_PERCENTAGE", 2.0))

# Modo de segurança: True para simulação, False para trades reais
DRY_RUN_MODE = os.getenv("DRY_RUN_MODE", "True").lower() == "true"

# Dicionário para armazenar as chaves de API das exchanges
EXCHANGE_CREDENTIALS = {
    'gemini': {'apiKey': os.getenv("GEMINI_API_KEY"), 'secret': os.getenv("GEMINI_SECRET")},
    'okx': {'apiKey': os.getenv("OKX_API_KEY"), 'secret': os.getenv("OKX_SECRET")},
    'bybit': {'apiKey': os.getenv("BYBIT_API_KEY"), 'secret': os.getenv("BYBIT_SECRET"), 'password': os.getenv("BYBIT_PASSWORD")},
    'kucoin': {'apiKey': os.getenv("KUCOIN_API_KEY"), 'secret': os.getenv("KUCOIN_SECRET")},
    'gateio': {'apiKey': os.getenv("GATEIO_API_KEY"), 'secret': os.getenv("GATEIO_SECRET")},
    'huobi': {'apiKey': os.getenv("HUOBI_API_KEY"), 'secret': os.getenv("HUOBI_SECRET")},
    'lbank': {'apiKey': os.getenv("LBANK_API_KEY"), 'secret': os.getenv("LBANK_SECRET")},
    'mexc': {'apiKey': os.getenv("MEXC_API_KEY"), 'secret': os.getenv("MEXC_SECRET")},
    'bitso': {'apiKey': os.getenv("BITSO_API_KEY"), 'secret': os.getenv("BITSO_SECRET")},
    'cryptocom': {'apiKey': os.getenv("CRYPTOCOM_API_KEY"), 'secret': os.getenv("CRYPTOCOM_SECRET")},
}

# Exchanges a serem monitoradas (10, todas com mínimo de 1 USDT)
EXCHANGES_LIST = [
    'okx', 'bybit', 'kucoin', 'gateio', 'mexc',
    'huobi', 'lbank', 'cryptocom', 'bitso', 'gemini'
]

# Pares USDT - OTIMIZADA (30 pares principais)
PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "DOGE/USDT",
    "TON/USDT", "ADA/USDT", "AVAX/USDT", "DOT/USDT", "BCH/USDT", "LINK/USDT",
    "LTC/USDT", "UNI/USDT", "ICP/USDT", "PEPE/USDT", "SEI/USDT", "XLM/USDT",
    "APT/USDT", "IMX/USDT", "GRT/USDT", "ATOM/USDT", "AAVE/USDT", "JUP/USDT",
    "ARB/USDT", "MNT/USDT", "FIL/USDT", "OP/USDT", "STX/USDT", "FTM/USDT"
]

# Configuração de logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

global_exchanges_instances = {}
GLOBAL_MARKET_DATA = {pair: {} for pair in PAIRS}
markets_loaded = {}
GLOBAL_ACTIVE_TRADES = {}
GLOBAL_STUCK_POSITIONS = {}
last_alert_times = {}
COOLDOWN_SECONDS = 300

# Novas variáveis de segurança
GLOBAL_TRADE_LIMIT = None  # Use None para modo ilimitado
GLOBAL_TRADES_EXECUTED = 0
GLOBAL_BOT_ACTIVE = True

GLOBAL_STATS = {
    'exchange_discrepancy': {ex: {'total_diff': 0, 'count': 0} for ex in EXCHANGES_LIST},
    'pair_opportunities': {pair: {'count': 0, 'total_profit': 0} for pair in PAIRS},
    'trade_outcomes': {'success': 0, 'stuck': 0, 'failed': 0}
}
GLOBAL_TOTAL_CAPITAL_USDT = DEFAULT_TOTAL_CAPITAL
GLOBAL_BALANCES = {ex: {'USDT': 0.0} for ex in EXCHANGES_LIST}
watcher_tasks = []

async def get_exchange_instance(ex_id, authenticated=False, is_rest=False):
    """Retorna uma instância de exchange ccxt (REST) ou ccxt.pro (async)."""
    if authenticated and not EXCHANGE_CREDENTIALS.get(ex_id):
        logger.error(f"Credenciais de API não encontradas para {ex_id}.")
        return None

    config = {'enableRateLimit': True, 'timeout': 10000}
    if authenticated and EXCHANGE_CREDENTIALS.get(ex_id):
        config.update(EXCHANGE_CREDENTIALS[ex_id])

    try:
        exchange_class = getattr(ccxt_pro if not is_rest else ccxt_rest, ex_id)
        instance = exchange_class(config)
        return instance
    except Exception as e:
        logger.error(f"Erro ao instanciar exchange {ex_id}: {e}")
        return None

async def execute_trade(action, exchange_id, pair, amount_base, price=None):
    if DRY_RUN_MODE:
        logger.info(f"[DRY_RUN] Tentativa de {action} de {amount_base:.8f} {pair.split('/')[0]} em {exchange_id} com preço {price}")
        mock_order = {
            'status': 'closed',
            'id': f'dry_run_id_{int(time.time())}',
            'amount': amount_base,
            'price': price,
            'average': price,
            'side': action,
            'symbol': pair,
        }
        return mock_order

    exchange_rest = await get_exchange_instance(exchange_id, authenticated=True, is_rest=True)
    if not exchange_rest:
        return None

    try:
        order = await exchange_rest.create_order(pair, 'market', action, amount_base)
        return order
    except Exception as e:
        logger.error(f"Erro ao executar ordem de {action} em {exchange_id}: {e}")
        return None

async def sell_stuck_position_if_needed(application, pair):
    bot = application.bot
    chat_id = application.bot_data.get('admin_chat_id')

    if pair not in GLOBAL_STUCK_POSITIONS:
        return

    stuck_info = GLOBAL_STUCK_POSITIONS[pair]

    if time.time() - stuck_info['timestamp'] > 300:
        logger.warning(f"Posição travada de {pair} em {stuck_info['exchange_id']} excedeu o tempo limite. Executando venda a mercado.")
        await bot.send_message(chat_id=chat_id, text=f"🚨 Saída de emergência: Venda de {pair} em {stuck_info['exchange_id']} a preço de mercado.")

        try:
            exchange_rest = await get_exchange_instance(stuck_info['exchange_id'], authenticated=True, is_rest=True)
            sell_order = await exchange_rest.create_order(pair, 'market', 'sell', stuck_info['amount_base'])

            if sell_order and sell_order['status'] == 'closed':
                await bot.send_message(chat_id=chat_id, text=f"✅ Posição de {pair} liquidada com sucesso.")
            else:
                await bot.send_message(chat_id=chat_id, text=f"⛔ Falha na liquidação de {pair}. Verifique manualmente.")
        except Exception as e:
            logger.error(f"Erro na liquidação de emergência de {pair}: {e}")
            await bot.send_message(chat_id=chat_id, text=f"⛔ Erro fatal na liquidação de {pair}. Verifique manualmente.")

        del GLOBAL_STUCK_POSITIONS[pair]

async def analyze_market_data():
    while True:
        try:
            for pair in PAIRS:
                market_data = GLOBAL_MARKET_DATA.get(pair, {})
                if len(market_data) < 2:
                    continue

                best_buy_price = float('inf')
                buy_ex_id = None

                best_sell_price = 0
                sell_ex_id = None

                for ex_id, data in market_data.items():
                    if data.get('ask') is not None and data['ask'] < best_buy_price:
                        best_buy_price = data['ask']
                        buy_ex_id = ex_id

                    if data.get('bid') is not None and data['bid'] > best_sell_price:
                        best_sell_price = data['bid']
                        sell_ex_id = ex_id

                if buy_ex_id and sell_ex_id and buy_ex_id != sell_ex_id:
                    gross_profit = (best_sell_price - best_buy_price) / best_buy_price
                    gross_profit_percentage = gross_profit * 100

                    if pair in GLOBAL_STATS['pair_opportunities']:
                        GLOBAL_STATS['pair_opportunities'][pair]['count'] += 1

                    if best_buy_price > 0:
                        discrepancy = gross_profit_percentage
                        if buy_ex_id in GLOBAL_STATS['exchange_discrepancy']:
                            GLOBAL_STATS['exchange_discrepancy'][buy_ex_id]['total_diff'] += discrepancy
                            GLOBAL_STATS['exchange_discrepancy'][buy_ex_id]['count'] += 1
                        if sell_ex_id in GLOBAL_STATS['exchange_discrepancy']:
                            GLOBAL_STATS['exchange_discrepancy'][sell_ex_id]['total_diff'] += discrepancy
                            GLOBAL_STATS['exchange_discrepancy'][sell_ex_id]['count'] += 1
        except Exception as e:
            logger.error(f"Erro na análise de dados de mercado: {e}", exc_info=True)

        await asyncio.sleep(60)

async def check_arbitrage_opportunities(application):
    bot = application.bot
    while True:
        try:
            chat_id = application.bot_data.get('admin_chat_id')
            if not chat_id:
                await asyncio.sleep(5)
                continue

            global GLOBAL_BOT_ACTIVE, GLOBAL_TRADES_EXECUTED, GLOBAL_TRADE_LIMIT
            if not GLOBAL_BOT_ACTIVE:
                await asyncio.sleep(5)
                continue

            for pair in list(GLOBAL_STUCK_POSITIONS.keys()):
                await sell_stuck_position_if_needed(application, pair)

            if GLOBAL_ACTIVE_TRADES:
                await asyncio.sleep(5)
                continue

            lucro_minimo = application.bot_data.get('lucro_minimo_porcentagem', DEFAULT_LUCRO_MINIMO_PORCENTAGEM)
            trade_percentage = application.bot_data.get('trade_percentage', DEFAULT_TRADE_PERCENTAGE)
            trade_amount_usd = GLOBAL_TOTAL_CAPITAL_USDT * (trade_percentage / 100)
            fee = application.bot_data.get('fee_percentage', DEFAULT_FEE_PERCENTAGE) / 100.0
            best_opportunity = None

            for pair in PAIRS:
                market_data = GLOBAL_MARKET_DATA.get(pair, {})
                if len(market_data) < 2:
                    continue

                best_buy_price = float('inf')
                buy_ex_id = None
                best_sell_price = 0
                sell_ex_id = None

                for ex_id, data in market_data.items():
                    if data.get('ask') is not None and data['ask'] < best_buy_price:
                        best_buy_price = data['ask']
                        buy_ex_id = ex_id
                    if data.get('bid') is not None and data['bid'] > best_sell_price:
                        best_sell_price = data['bid']
                        sell_ex_id = ex_id

                if not buy_ex_id or not sell_ex_id or buy_ex_id == sell_ex_id:
                    continue

                try:
                    buy_exchange_rest = await get_exchange_instance(buy_ex_id, authenticated=False, is_rest=True)
                    sell_exchange_rest = await get_exchange_instance(sell_ex_id, authenticated=False, is_rest=True)

                    if not buy_exchange_rest or not sell_exchange_rest:
                        continue

                    ticker_buy = await buy_exchange_rest.fetch_ticker(pair)
                    ticker_sell = await sell_exchange_rest.fetch_ticker(pair)

                    confirmed_buy_price = ticker_buy['ask']
                    confirmed_sell_price = ticker_sell['bid']
                except Exception:
                    continue

                gross_profit = (confirmed_sell_price - confirmed_buy_price) / confirmed_buy_price
                gross_profit_percentage = gross_profit * 100
                net_profit_percentage = gross_profit_percentage - (2 * fee * 100)

                if net_profit_percentage >= 0.5 and net_profit_percentage < lucro_minimo:
                    current_time = time.time()
                    if pair not in last_alert_times or (current_time - last_alert_times[pair]) > COOLDOWN_SECONDS:
                        await bot.send_message(chat_id=chat_id, text=f"👀 Oportunidade menor que o lucro mínimo para {pair}!\nLucro: {net_profit_percentage:.2f}%.")
                        last_alert_times[pair] = current_time

                if net_profit_percentage >= lucro_minimo:
                    min_usdt_balance = application.bot_data.get('min_usdt_balance', DEFAULT_MIN_USDT_BALANCE)
                    if GLOBAL_BALANCES.get(buy_ex_id, {}).get('USDT', 0) < trade_amount_usd + min_usdt_balance:
                        logger.info(f"Saldo insuficiente em {buy_ex_id} para {pair}. Pulando trade.")
                        continue

                    if best_opportunity is None or net_profit_percentage > best_opportunity['net_profit']:
                        best_opportunity = {
                            'pair': pair,
                            'buy_ex_id': buy_ex_id,
                            'sell_ex_id': sell_ex_id,
                            'buy_price': confirmed_buy_price,
                            'sell_price': confirmed_sell_price,
                            'net_profit': net_profit_percentage,
                            'trade_amount_usd': trade_amount_usd
                        }

            if best_opportunity:
                # Verifique se o limite de trades foi atingido
                if GLOBAL_TRADE_LIMIT is not None and GLOBAL_TRADES_EXECUTED >= GLOBAL_TRADE_LIMIT:
                    GLOBAL_BOT_ACTIVE = False
                    await bot.send_message(chat_id=chat_id, text=f"✅ Limite de trades ({GLOBAL_TRADE_LIMIT}) atingido! O bot está pausado. Use /resume para retomar as operações.")
                    logger.info("Limite de trades atingido. Bot pausado.")
                    continue

                await bot.send_message(chat_id=chat_id, text=f"💰 Oportunidade encontrada para {best_opportunity['pair']}! Iniciando execução do trade...")
                GLOBAL_ACTIVE_TRADES[best_opportunity['pair']] = True
                asyncio.create_task(execute_arbitrage_trade(application, best_opportunity))

        except Exception as e:
            logger.error(f"Erro na checagem de arbitragem: {e}", exc_info=True)

        await asyncio.sleep(5)

async def execute_arbitrage_trade(application, opportunity):
    bot = application.bot
    chat_id = application.bot_data.get('admin_chat_id')

    pair = opportunity['pair']
    buy_ex_id = opportunity['buy_ex_id']
    sell_ex_id = opportunity['sell_ex_id']
    trade_amount_usd = opportunity['trade_amount_usd']

    try:
        # 1. Verificação de Saldo Final antes da compra
        if GLOBAL_BALANCES.get(buy_ex_id, {}).get('USDT', 0) < trade_amount_usd:
            await bot.send_message(chat_id=chat_id, text=f"⛔ Falha: Saldo insuficiente em {buy_ex_id}. Trade cancelado.")
            GLOBAL_STATS['trade_outcomes']['failed'] += 1
            return

        bought_amount_base = float(Decimal(str(trade_amount_usd)) / Decimal(str(opportunity['buy_price'])))
        buy_order = await execute_trade('buy', buy_ex_id, pair, bought_amount_base, opportunity['buy_price'])

        if not buy_order or buy_order['status'] != 'closed':
            await bot.send_message(chat_id=chat_id, text=f"⚠️ Compra falhou ou não foi executada em {buy_ex_id}. Trade cancelado.")
            GLOBAL_STATS['trade_outcomes']['failed'] += 1
            return

        bought_price_avg = float(buy_order['average'])
        bought_amount_base_filled = float(buy_order['amount'])

        await bot.send_message(chat_id=chat_id, text=f"🛒 Compra de {bought_amount_base_filled:.8f} {pair.split('/')[0]} em {buy_ex_id} concluída a um preço médio de {bought_price_avg:.8f}.")

        GLOBAL_STUCK_POSITIONS[pair] = {
            'exchange_id': buy_ex_id,
            'amount_base': bought_amount_base_filled,
            'timestamp': time.time()
        }

        try:
            exchange_rest = await get_exchange_instance(sell_ex_id, authenticated=True, is_rest=True)
            if not exchange_rest:
                raise Exception("Falha ao obter instância da exchange de venda.")

            ticker = await exchange_rest.fetch_ticker(pair)
            current_sell_price = ticker['bid']

            sell_order = await execute_trade('sell', sell_ex_id, pair, bought_amount_base_filled, current_sell_price)

            if sell_order and sell_order['status'] == 'closed':
                final_amount_usd = float(sell_order['amount']) * float(sell_order['average'])
                initial_amount_usd = bought_amount_base_filled * bought_price_avg
                final_profit = final_amount_usd - initial_amount_usd
                final_profit_percentage = (final_profit / initial_amount_usd) * 100

                global GLOBAL_TOTAL_CAPITAL_USDT, GLOBAL_TRADES_EXECUTED
                GLOBAL_TOTAL_CAPITAL_USDT += final_profit
                GLOBAL_TRADES_EXECUTED += 1

                msg_success = (f"✅ Arbitragem de {pair} CONCLUÍDA! ({GLOBAL_TRADES_EXECUTED}/{GLOBAL_TRADE_LIMIT if GLOBAL_TRADE_LIMIT is not None else '∞'})\n"
                            f"Comprado em {buy_ex_id} por {bought_price_avg:.8f}\n"
                            f"Vendido em {sell_ex_id} por {sell_order['average']:.8f}\n"
                            f"Lucro Líquido: {final_profit_percentage:.2f}%\n"
                            f"Capital Total Atualizado: ${GLOBAL_TOTAL_CAPITAL_USDT:.2f}")
                await bot.send_message(chat_id=chat_id, text=msg_success)
                GLOBAL_STATS['trade_outcomes']['success'] += 1
                if pair in GLOBAL_STATS['pair_opportunities']:
                    GLOBAL_STATS['pair_opportunities'][pair]['total_profit'] += final_profit
            else:
                await bot.send_message(chat_id=chat_id, text=f"⚠️ Venda falhou em {sell_ex_id}. Moeda travada.")
                GLOBAL_STATS['trade_outcomes']['stuck'] += 1

        except Exception as e:
            logger.error(f"Erro na fase de venda em {sell_ex_id}: {e}")
            await bot.send_message(chat_id=chat_id, text=f"⛔ Erro fatal durante a venda. Moeda travada. Verifique manualmente.")
            GLOBAL_STATS['trade_outcomes']['stuck'] += 1

    except Exception as e:
        logger.error(f"Erro na fase de compra em {buy_ex_id}: {e}")
        await bot.send_message(chat_id=chat_id, text=f"⛔ Erro fatal durante a compra. Trade cancelado.")
        GLOBAL_STATS['trade_outcomes']['failed'] += 1
    finally:
        if pair in GLOBAL_ACTIVE_TRADES:
            del GLOBAL_ACTIVE_TRADES[pair]
        if pair in GLOBAL_STUCK_POSITIONS:
            del GLOBAL_STUCK_POSITIONS[pair]
        asyncio.create_task(update_all_balances(application))

async def watch_order_book_for_pair(exchange, pair, ex_id):
    logger.info(f"Iniciando monitoramento de {pair} em {ex_id}...")
    try:
        while True:
            try:
                order_book = await exchange.watch_order_book(pair)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Falha ao chamar watch_order_book para {pair} em {ex_id}: {e}")
                await asyncio.sleep(5)
                continue

            if order_book and 'bids' in order_book and 'asks' in order_book and order_book['bids'] and order_book['asks']:
                best_bid = order_book['bids'][0][0]
                best_ask = order_book['asks'][0][0]

                if pair not in GLOBAL_MARKET_DATA:
                    GLOBAL_MARKET_DATA[pair] = {}
                GLOBAL_MARKET_DATA[pair][ex_id] = {
                    'bid': float(best_bid),
                    'ask': float(best_ask),
                    'timestamp': time.time()
                }

                logger.debug(f"✅ Dados atualizados para {pair} em {ex_id}: BID={best_bid} ASK={best_ask}")
            else:
                logger.warning(f"⚠️ Nenhum dado recebido ou dados incompletos para {pair} na exchange {ex_id}")

            await asyncio.sleep(exchange.rateLimit / 1000)

    except ccxt_pro.NetworkError as e:
        logger.error(f"❌ Erro de rede no WebSocket para {pair} em {ex_id}: {e}. Tentando reconectar...")
        await asyncio.sleep(5)
        new_exchange = await get_exchange_instance(ex_id)
        if new_exchange:
            await watch_order_book_for_pair(new_exchange, pair, ex_id)
    except ccxt_pro.ExchangeError as e:
        logger.error(f"🚫 Erro da exchange no WebSocket para {pair} em {ex_id}: {e}. Aguardando 60 segundos...")
        await asyncio.sleep(60)
    except Exception as e:
        logger.error(f"🔥 Erro inesperado no WebSocket para {pair} em {ex_id}: {e}. Aguardando 10 segundos...")
        await asyncio.sleep(10)
    finally:
        if exchange and not exchange.has_closed:
            await exchange.close()

async def update_all_balances(application=None):
    for ex_id in EXCHANGES_LIST:
        exchange_rest = await get_exchange_instance(ex_id, authenticated=True, is_rest=True)
        if exchange_rest:
            try:
                balance = await exchange_rest.fetch_balance()
                GLOBAL_BALANCES[ex_id]['USDT'] = float(balance['free'].get('USDT', 0))
            except Exception as e:
                logger.error(f"Erro ao atualizar saldo de {ex_id}: {e}")

    if application and application.bot_data.get('admin_chat_id'):
        for ex_id, bal in GLOBAL_BALANCES.items():
            if bal['USDT'] < DEFAULT_MIN_USDT_BALANCE:
                chat_id = application.bot_data.get('admin_chat_id')
                if chat_id:
                    await application.bot.send_message(chat_id=chat_id, text=f"⚠️ ALERTA: Saldo de USDT em {ex_id} está abaixo do mínimo ({bal['USDT']:.2f}).")

async def watch_all_exchanges():
    for ex_id in EXCHANGES_LIST:
        exchange = await get_exchange_instance(ex_id)
        if not exchange:
            logger.error(f"Não foi possível criar a instância da exchange {ex_id}. Pulando.")
            continue

        global_exchanges_instances[ex_id] = exchange

        try:
            await exchange.load_markets()
            markets_loaded[ex_id] = True
            logger.info(f"Mercados de {ex_id} carregados. Total de pares: {len(exchange.markets)}")

            for pair in PAIRS:
                if pair in exchange.markets:
                    watcher_tasks.append(asyncio.create_task(
                        watch_order_book_for_pair(exchange, pair, ex_id)
                    ))
                else:
                    logger.warning(f"Par {pair} não está disponível em {ex_id}. Ignorando...")
        except Exception as e:
            logger.error(f"ERRO ao carregar mercados de {ex_id}: {e}")

    if watcher_tasks:
        await asyncio.gather(*watcher_tasks, return_exceptions=True)
    else:
        logger.error("Nenhuma tarefa de monitoramento de WebSocket foi iniciada. Verifique as configurações e credenciais.")

# --- Comandos do Telegram ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['admin_chat_id'] = update.message.chat_id
    global GLOBAL_BOT_ACTIVE
    GLOBAL_BOT_ACTIVE = True

    limit_info = "sem limite (modo ilimitado)"
    if GLOBAL_TRADE_LIMIT is not None:
        limit_info = f"{GLOBAL_TRADES_EXECUTED}/{GLOBAL_TRADE_LIMIT}"

    # Adiciona data e hora
    current_time = datetime.now().strftime("%d-%m-%Y %H:%M:%S")

    await update.message.reply_text(
        f"Olá! Bot de Arbitragem Ativado.\nData e Hora: {current_time}\n"
        f"Modo de operação: {'Simulação' if DRY_RUN_MODE else 'Real'}\n"
        f"Controle de Trades: {limit_info}\n\n"
        "Configurações atuais:\n"
        f"Lucro mínimo: {context.bot_data.get('lucro_minimo_porcentagem', DEFAULT_LUCRO_MINIMO_PORCENTAGEM)}%\n"
        f"Volume de trade: {context.bot_data.get('trade_percentage', DEFAULT_TRADE_PERCENTAGE)}% do capital total\n"
        f"Taxa de negociação: {context.bot_data.get('fee_percentage', DEFAULT_FEE_PERCENTAGE)}%\n\n"
        "Use /report_stats para ver um relatório de análise de mercado."
    )
    logger.info(f"Bot iniciado por chat_id: {update.message.chat_id}")

async def setlucro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        valor = float(context.args[0])
        if valor < 0:
            await update.message.reply_text("O lucro mínimo não pode ser negativo.")
            return
        context.bot_data['lucro_minimo_porcentagem'] = valor
        await update.message.reply_text(f"Lucro mínimo atualizado para {valor:.2f}%")
    except (IndexError, ValueError):
        await update.message.reply_text("Uso incorreto. Exemplo: /setlucro 2.5")

async def setvolume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        valor = float(context.args[0])
        if valor <= 0:
            await update.message.reply_text("O volume de trade deve ser um valor positivo.")
            return
        context.bot_data['trade_percentage'] = valor
        await update.message.reply_text(f"Volume de trade atualizado para {valor:.2f}% do capital total")
    except (IndexError, ValueError):
        await update.message.reply_text("Uso incorreto. Exemplo: /setvolume 10")

async def setfee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        valor = float(context.args[0])
        if valor < 0:
            await update.message.reply_text("A taxa de negociação não pode ser negativa.")
            return
        context.bot_data['fee_percentage'] = valor
        await update.message.reply_text(f"Taxa de negociação por lado atualizada para {valor:.3f}%")
    except (IndexError, ValueError):
        await update.message.reply_text("Uso incorreto. Exemplo: /setfee 0.075")

async def setexchanges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        exchanges_input = [ex.strip().lower() for ex in ' '.join(context.args).split(',')]
        valid_exchanges = [ex for ex in exchanges_input if ex in EXCHANGE_CREDENTIALS]

        if not valid_exchanges:
            await update.message.reply_text("Nenhuma exchange válida foi fornecida. Por favor, use nomes de exchanges válidos.")
            return

        global EXCHANGES_LIST
        EXCHANGES_LIST = valid_exchanges

        global GLOBAL_STATS
        GLOBAL_STATS['exchange_discrepancy'] = {ex: {'total_diff': 0, 'count': 0} for ex in EXCHANGES_LIST}
        await update.message.reply_text(f"Lista de exchanges para monitorar atualizada para: {', '.join(EXCHANGES_LIST)}.")
        logger.info(f"Exchanges para monitorar atualizadas para: {EXCHANGES_LIST}.")
        await restart_watchers(update, context)

    except (IndexError, ValueError):
        await update.message.reply_text("Uso incorreto. Exemplo: /setexchanges kraken,okx,bybit")

async def setpairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pairs_input = [pair.strip().upper() for pair in ' '.join(context.args).split(',')]

        if not pairs_input:
            await update.message.reply_text("Nenhum par foi fornecido.")
            return

        global PAIRS
        PAIRS = pairs_input

        global GLOBAL_STATS
        GLOBAL_STATS['pair_opportunities'] = {pair: {'count': 0, 'total_profit': 0} for pair in PAIRS}
        await update.message.reply_text(f"Lista de pares para monitorar atualizada para: {', '.join(PAIRS)}.")
        logger.info(f"Pares para monitorar atualizados para: {PAIRS}.")
        await restart_watchers(update, context)

    except (IndexError, ValueError):
        await update.message.reply_text("Uso incorreto. Exemplo: /setpairs BTC/USDT,ETH/USDT")

async def restart_watchers(update, context):
    logger.info("Reiniciando os monitores...")
    await update.message.reply_text("Reiniciando os monitores...")

    for task in watcher_tasks:
        task.cancel()
    watcher_tasks.clear()

    for ex in global_exchanges_instances.values():
        if ex and not ex.has_closed:
            await ex.close()
    global_exchanges_instances.clear()

    asyncio.create_task(watch_all_exchanges())

async def report_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_time = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    report_text = f"📊 **Relatório de Análise de Mercado** 📊\n"
    report_text += f"**Data e Hora: {current_time}**\n\n"
    report_text += f"💰 **Capital Total (USDT)**\n"
    report_text += f" - Capital Total: ${GLOBAL_TOTAL_CAPITAL_USDT:.2f}\n\n"
    report_text += "📈 **Oportunidades por Par (Desde o início)**\n"
    sorted_pairs = sorted(GLOBAL_STATS['pair_opportunities'].items(), key=lambda item: item[1]['count'], reverse=True)
    for pair, stats in sorted_pairs:
        if stats['count'] > 0:
            report_text += f" - {pair}: {stats['count']} oportunidades detectadas\n"
    report_text += "\n"
    report_text += "🔄 **Discrepância Média de Preço por Exchange**\n"
    sorted_exchanges = sorted(GLOBAL_STATS['exchange_discrepancy'].items(), key=lambda item: (item[1]['total_diff'] / max(1, item[1]['count'])), reverse=True)
    for ex_id, stats in sorted_exchanges:
        if stats['count'] > 0:
            avg_discrepancy = stats['total_diff'] / stats['count']
            report_text += f" - {ex_id}: {avg_discrepancy:.2f}% de discrepância média\n"
    report_text += "\n"
    report_text += "✅ **Resultados dos Trades (Desde o início)**\n"
    report_text += f" - Trades Concluídos com sucesso: {GLOBAL_STATS['trade_outcomes']['success']}\n"
    report_text += f" - Moedas Travadas: {GLOBAL_STATS['trade_outcomes']['stuck']}\n"
    report_text += f" - Trades Falhados: {GLOBAL_STATS['trade_outcomes']['failed']}\n"
    await update.message.reply_text(report_text, parse_mode='Markdown')

async def debug_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_time = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    info_text = f"🔎 **Informações de Debug**\n"
    info_text += f"**Data e Hora: {current_time}**\n\n"
    for i, pair in enumerate(PAIRS[:5]):
        info_text += f"**{pair}**:\n"
        if pair in GLOBAL_MARKET_DATA and GLOBAL_MARKET_DATA[pair]:
            for ex_id, data in GLOBAL_MARKET_DATA[pair].items():
                if 'bid' in data and 'ask' in data:
                    info_text += f" - {ex_id}: Compra: {data['ask']:.8f} | Venda: {data['bid']:.8f}\n"
                else:
                    info_text += f" - {ex_id}: Dados indisponíveis\n"
        else:
            info_text += " - Dados não carregados\n"
        info_text += "\n"
    await update.message.reply_text(info_text, parse_mode='Markdown')

async def setlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        valor = int(context.args[0])
        if valor <= 0:
            await update.message.reply_text("O limite deve ser um número inteiro positivo.")
            return

        global GLOBAL_TRADE_LIMIT, GLOBAL_TRADES_EXECUTED
        GLOBAL_TRADE_LIMIT = valor
        GLOBAL_TRADES_EXECUTED = 0
        await update.message.reply_text(f"Limite de trades atualizado para {valor}. Contagem resetada.")
    except (IndexError, ValueError):
        await update.message.reply_text("Uso incorreto. Exemplo: /setlimit 5")

async def unlimited(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global GLOBAL_TRADE_LIMIT
    GLOBAL_TRADE_LIMIT = None
    await update.message.reply_text("♾️ Modo ilimitado ativado. O bot irá executar arbitragens continuamente.")

async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global GLOBAL_BOT_ACTIVE
    GLOBAL_BOT_ACTIVE = False
    await update.message.reply_text("⏸️ Bot pausado. Nenhuma nova arbitragem será iniciada. Use /resume para continuar.")

async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global GLOBAL_BOT_ACTIVE
    GLOBAL_BOT_ACTIVE = True
    await update.message.reply_text("▶️ Bot retomado. Ele voltará a procurar e executar arbitragens.")

async def stop_arbitrage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['admin_chat_id'] = None
    await update.message.reply_text("Alertas e simulações desativados. Use /start para reativar.")

async def silenciar_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['admin_chat_id'] = None
    await update.message.reply_text("Bot silenciado. Nenhum alerta será enviado. Use /start para reativar.")
    logger.info(f"Alertas silenciados por {update.message.chat_id}")

async def main():
    application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlucro", setlucro))
    application.add_handler(CommandHandler("setvolume", setvolume))
    application.add_handler(CommandHandler("setfee", setfee))
    application.add_handler(CommandHandler("stop", stop_arbitrage))
    application.add_handler(CommandHandler("setexchanges", setexchanges))
    application.add_handler(CommandHandler("setpairs", setpairs))
    application.add_handler(CommandHandler("report_stats", report_stats))
    application.add_handler(CommandHandler("silenciar", silenciar_alerts))
    application.add_handler(CommandHandler("debug", debug_info))
    application.add_handler(CommandHandler("setlimit", setlimit))
    application.add_handler(CommandHandler("unlimited", unlimited))
    application.add_handler(CommandHandler("pause", pause))
    application.add_handler(CommandHandler("resume", resume))

    try:
        await application.bot.set_my_commands([
            BotCommand("start", "Iniciar
