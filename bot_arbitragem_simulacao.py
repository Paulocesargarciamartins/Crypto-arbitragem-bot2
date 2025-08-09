import os
import asyncio
import logging
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import ccxt.pro as ccxt
import ccxt as ccxt_rest
import nest_asyncio
import time
from decimal import Decimal

# Aplica o patch para permitir loops aninhados
nest_asyncio.apply()

# --- Configurações básicas e chaves de API ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEFAULT_LUCRO_MINIMO_PORCENTAGEM = 2.0
DEFAULT_TRADE_AMOUNT_USD = 50.0
DEFAULT_FEE_PERCENTAGE = 0.1
DRY_RUN_MODE = True

# Limite máximo de lucro bruto para validação de dados.
MAX_GROSS_PROFIT_PERCENTAGE_SANITY_CHECK = 100.0

# Dicionário para armazenar as chaves de API das exchanges
EXCHANGE_CREDENTIALS = {
    'binance': {
        'apiKey': os.getenv("BINANCE_API_KEY"),
        'secret': os.getenv("BINANCE_SECRET")
    },
    'kraken': {
        'apiKey': os.getenv("KRAKEN_API_KEY"),
        'secret': os.getenv("KRAKEN_SECRET")
    },
    'okx': {
        'apiKey': os.getenv("OKX_API_KEY"),
        'secret': os.getenv("OKX_SECRET")
    },
    'bybit': {
        'apiKey': os.getenv("BYBIT_API_KEY"),
        'secret': os.getenv("BYBIT_SECRET")
    },
    'kucoin': {
        'apiKey': os.getenv("KUCOIN_API_KEY"),
        'secret': os.getenv("KUCOIN_SECRET")
    },
    'bitstamp': {
        'apiKey': os.getenv("BITSTAMP_API_KEY"),
        'secret': os.getenv("BITSTAMP_SECRET")
    },
    'bitget': {
        'apiKey': os.getenv("BITGET_API_KEY"),
        'secret': os.getenv("BITGET_SECRET")
    },
    'coinbase': {
        'apiKey': os.getenv("COINBASE_API_KEY"),
        'secret': os.getenv("COINBASE_SECRET")
    },
    'htx': {
        'apiKey': os.getenv("HTX_API_KEY"),
        'secret': os.getenv("HTX_SECRET")
    },
    'gate': {
        'apiKey': os.getenv("GATE_API_KEY"),
        'secret': os.getenv("GATE_SECRET")
    },
    'cryptocom': {
        'apiKey': os.getenv("CRYPTOCOM_API_KEY"),
        'secret': os.getenv("CRYPTOCOM_SECRET")
    },
    'gemini': {
        'apiKey': os.getenv("GEMINI_API_KEY"),
        'secret': os.getenv("GEMINI_SECRET")
    },
}

# Endereços de depósito para transferências (segurança total)
DEPOSIT_ADDRESSES = {
    'binance': {
        'USDT': 'YOUR_BINANCE_USDT_DEPOSIT_ADDRESS',
        'BTC': 'YOUR_BINANCE_BTC_DEPOSIT_ADDRESS'
    },
    'kraken': {
        'USDT': 'YOUR_KRAKEN_USDT_DEPOSIT_ADDRESS',
        'BTC': 'YOUR_KRAKEN_BTC_DEPOSIT_ADDRESS'
    },
    'okx': {
        'USDT': 'YOUR_OKX_USDT_DEPOSIT_ADDRESS',
        'BTC': 'YOUR_OKX_BTC_DEPOSIT_ADDRESS'
    },
}

# Exchanges confiáveis para monitorar (lista configurável)
EXCHANGES_LIST = [
    'binance', 'coinbase', 'kraken', 'okx', 'bybit',
    'kucoin', 'bitstamp', 'bitget',
]

# Pares USDT - OTIMIZADA para o plano profissional (lista configurável)
PAIRS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT", "USDC/USDT",
    "DOGE/USDT", "ADA/USDT", "TRX/USDT", "SHIB/USDT", "AVAX/USDT", "DOT/USDT",
    "LINK/USDT", "WBTC/USDT", "STETH/USDT", "TON/USDT", "BCH/USDT", "LTC/USDT",
    "UNI/USDT", "ETC/USDT", "XLM/USDT", "PEPE/USDT", "FIL/USDT", "NEAR/USDT",
    "WIF/USDT", "RUNE/USDT", "THETA/USDT", "LDO/USDT", "TIA/USDT", "JUP/USDT",
    "CRO/USDT", "INJ/USDT", "MKR/USDT", "APT/USDT", "IMX/USDT", "ARB/USDT",
    "SUI/USDT", "FLOKI/USDT", "WLD/USDT", "OP/USDT", "HBAR/USDT", "SATS/USDT",
    "VET/USDT", "KAS/USDT", "GRT/USDT", "MINA/USDT", "ENA/USDT", "STRK/USDT",
    "TAO/USDT", "AAVE/USDT", "SEI/USDT", "FET/USDT", "FLOW/USDT", "FDUSD/USDT",
    "GALA/USDT", "QNT/USDT", "DYDX/USDT", "ORDI/USDT", "MNT/USDT", "AXS/USDT",
    "CHZ/USDT", "EOS/USDT", "SNX/USDT", "BONK/USDT", "SAND/USDT", "XTZ/USDT",
    "STX/USDT", "PYTH/USDT", "TFUEL/USDT", "ALGO/USDT", "AKT/USDT", "RON/USDT",
    "WEMIX/USDT", "EGLD/USDT", "RNDR/USDT", "CORE/USDT", "IOTA/USDT", "CFX/USDT",
    "GNO/USDT", "AR/USDT", "BTT/USDT", "KLAY/USDT", "NEO/USDT", "CRV/USDT",
    "SSV/USDT", "BEAMX/USDT", "ZEC/USDT", "JASMY/USDT", "MANA/USDT", "SFP/USDT",
    "LEO/USDT", "KDA/USDT", "BOME/USDT", "DYM/USDT", "JTO/USDT", "FTM/USDT",
    "WOO/USDT", "OM/USDT", "ZETA/USDT", "DASH/USDT",
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
GLOBAL_STUCK_COINS = {}
last_alert_times = {}
COOLDOWN_SECONDS = 300

# --- Novas estruturas de dados para análise de mercado ---
GLOBAL_STATS = {
    'exchange_discrepancy': {ex: {'total_diff': 0, 'count': 0} for ex in EXCHANGES_LIST},
    'pair_opportunities': {pair: {'count': 0, 'total_profit': 0} for pair in PAIRS},
    'trade_outcomes': {'success': 0, 'stuck': 0, 'failed': 0}
}
# A ser preenchido com a análise de transferência, se implementada
GLOBAL_TRANSFER_STATS = {} 

async def get_exchange_instance(ex_id, authenticated=False, is_rest=False):
    if authenticated and ex_id not in EXCHANGE_CREDENTIALS:
        logger.error(f"Credenciais de API não encontradas para {ex_id}.")
        return None
    
    config = {
        'enableRateLimit': True,
        'timeout': 10000,
    }
    if authenticated:
        config.update(EXCHANGE_CREDENTIALS[ex_id])
    
    exchange_class = getattr(ccxt.pro if not is_rest else ccxt_rest, ex_id)
    return exchange_class(config)

async def check_balance(exchange, currency='USDT'):
    try:
        balance = await exchange.fetch_balance()
        return float(balance['free'].get(currency, 0))
    except Exception as e:
        logger.error(f"Erro ao verificar saldo na exchange {exchange.id}: {e}")
        return 0.0

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

    exchange_rest = None
    try:
        exchange_rest = await get_exchange_instance(exchange_id, authenticated=True, is_rest=True)
        if not exchange_rest:
            return None
        
        if action == 'buy':
            order = await exchange_rest.create_order(pair, 'market', 'buy', amount_base)
        elif action == 'sell':
            order = await exchange_rest.create_order(pair, 'market', 'sell', amount_base)

        return order
    except Exception as e:
        logger.error(f"Erro ao executar ordem de {action} em {exchange_id}: {e}")
        return None
    finally:
        if exchange_rest:
            await exchange_rest.close()

async def place_limit_order_if_stuck(exchange_id, pair, bought_price_avg, amount_base, prejuizo_maximo):
    if DRY_RUN_MODE:
        sell_price_limit = float(Decimal(str(bought_price_avg)) * (Decimal(1) - Decimal(str(prejuizo_maximo)) / Decimal(100)))
        logger.info(f"[DRY_RUN] Colocando ordem limite em {exchange_id} para {pair}. Prejuízo máximo: {prejuizo_maximo}%. Preço limite: {sell_price_limit:.8f}")
        return {'status': 'open', 'id': f'dry_run_limit_id_{int(time.time())}'}

    exchange_rest = None
    try:
        exchange_rest = await get_exchange_instance(exchange_id, authenticated=True, is_rest=True)
        if not exchange_rest:
            return None

        sell_price_limit = float(Decimal(str(bought_price_avg)) * (Decimal(1) - Decimal(str(prejuizo_maximo)) / Decimal(100)))
        
        order = await exchange_rest.create_order(pair, 'limit', 'sell', amount_base, sell_price_limit)
        
        return order
    except Exception as e:
        logger.error(f"Erro ao colocar ordem limite para {pair} em {ex_id}: {e}")
        return None
    finally:
        if exchange_rest:
            await exchange_rest.close()

async def analyze_market_data():
    """
    Função para analisar dados do mercado em segundo plano.
    """
    while True:
        try:
            for pair in PAIRS:
                market_data = GLOBAL_MARKET_DATA[pair]
                if len(market_data) < 2:
                    continue

                best_buy_price = float('inf')
                buy_ex_id = None
                
                best_sell_price = 0
                sell_ex_id = None

                for ex_id, data in market_data.items():
                    if data.get('ask') is not None:
                        if data['ask'] < best_buy_price:
                            best_buy_price = data['ask']
                            buy_ex_id = ex_id
                    
                    if data.get('bid') is not None:
                        if data['bid'] > best_sell_price:
                            best_sell_price = data['bid']
                            sell_ex_id = ex_id

                if buy_ex_id and sell_ex_id and buy_ex_id != sell_ex_id:
                    gross_profit = (best_sell_price - best_buy_price) / best_buy_price
                    gross_profit_percentage = gross_profit * 100

                    # Aumentar a contagem de oportunidades para este par
                    if pair in GLOBAL_STATS['pair_opportunities']:
                        GLOBAL_STATS['pair_opportunities'][pair]['count'] += 1
                    
                    # Calcular a discrepância média entre as exchanges para o relatório
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
        
        await asyncio.sleep(60) # Executa a cada minuto

async def check_arbitrage_opportunities(application):
    bot = application.bot
    while True:
        try:
            chat_id = application.bot_data.get('admin_chat_id')
            if not chat_id:
                await asyncio.sleep(5)
                continue

            if GLOBAL_ACTIVE_TRADES:
                await asyncio.sleep(5)
                continue

            lucro_minimo = application.bot_data.get('lucro_minimo_porcentagem', DEFAULT_LUCRO_MINIMO_PORCENTAGEM)
            trade_amount_usd = application.bot_data.get('trade_amount_usd', DEFAULT_TRADE_AMOUNT_USD)
            fee = application.bot_data.get('fee_percentage', DEFAULT_FEE_PERCENTAGE) / 100.0

            best_opportunity = None

            for pair in PAIRS:
                market_data = GLOBAL_MARKET_DATA[pair]
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

                    ticker_buy = await buy_exchange_rest.fetch_ticker(pair)
                    ticker_sell = await sell_exchange_rest.fetch_ticker(pair)

                    confirmed_buy_price = ticker_buy['ask']
                    confirmed_sell_price = ticker_sell['bid']
                    
                    await buy_exchange_rest.close()
                    await sell_exchange_rest.close()
                except Exception as e:
                    logger.warning(f"Falha na checagem REST para {pair}: {e}")
                    continue

                gross_profit = (confirmed_sell_price - confirmed_buy_price) / confirmed_buy_price
                gross_profit_percentage = gross_profit * 100
                net_profit_percentage = gross_profit_percentage - (2 * fee * 100)

                if net_profit_percentage >= lucro_minimo:
                    if best_opportunity is None or net_profit_percentage > best_opportunity['net_profit']:
                        best_opportunity = {
                            'pair': pair,
                            'buy_ex_id': buy_ex_id,
                            'sell_ex_id': sell_ex_id,
                            'buy_price': confirmed_buy_price,
                            'sell_price': confirmed_sell_price,
                            'net_profit': net_profit_percentage
                        }
            
            if best_opportunity:
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
    
    trade_amount_usd = application.bot_data.get('trade_amount_usd', DEFAULT_TRADE_AMOUNT_USD)
    
    try:
        buy_exchange_auth = await get_exchange_instance(buy_ex_id, authenticated=True, is_rest=True)
        if not buy_exchange_auth:
            await bot.send_message(chat_id=chat_id, text=f"⛔ Falha: Credenciais não encontradas para {buy_ex_id}.")
            if pair in GLOBAL_STATS['pair_opportunities']:
                GLOBAL_STATS['trade_outcomes']['failed'] += 1
            return
        
        usdt_balance = await check_balance(buy_exchange_auth, 'USDT')
        if usdt_balance < trade_amount_usd:
            await bot.send_message(chat_id=chat_id, text=f"⛔ Falha: Saldo insuficiente (${usdt_balance:.2f} USDT) em {buy_ex_id} para uma compra de ${trade_amount_usd:.2f}. Trade cancelado.")
            if pair in GLOBAL_STATS['pair_opportunities']:
                GLOBAL_STATS['trade_outcomes']['failed'] += 1
            return

        bought_amount_base = float(Decimal(str(trade_amount_usd)) / Decimal(str(opportunity['buy_price'])))
        buy_order = await execute_trade('buy', buy_ex_id, pair, bought_amount_base, opportunity['buy_price'])

        if not buy_order or buy_order['status'] != 'closed':
            await bot.send_message(chat_id=chat_id, text=f"⚠️ Compra falhou ou não foi executada em {buy_ex_id}. Trade cancelado.")
            if pair in GLOBAL_STATS['pair_opportunities']:
                GLOBAL_STATS['trade_outcomes']['failed'] += 1
            return

        bought_price_avg = float(buy_order['average'])
        bought_amount_base_filled = float(buy_order['amount'])
        
        await bot.send_message(chat_id=chat_id, text=f"🛒 Compra de {bought_amount_base_filled:.8f} {pair.split('/')[0]} em {buy_ex_id} concluída a um preço médio de {bought_price_avg:.8f}.")

        try:
            sell_exchange_auth = await get_exchange_instance(sell_ex_id, authenticated=True, is_rest=True)
            if not sell_exchange_auth:
                await bot.send_message(chat_id=chat_id, text=f"⛔ Falha: Credenciais não encontradas para {sell_ex_id}.")
                if pair in GLOBAL_STATS['pair_opportunities']:
                    GLOBAL_STATS['trade_outcomes']['stuck'] += 1
                return

            current_sell_price_rest = await sell_exchange_auth.fetch_ticker(pair)
            current_sell_price = current_sell_price_rest['bid']
            
            gross_profit_after_buy = (current_sell_price - bought_price_avg) / bought_price_avg
            net_profit_after_buy = (gross_profit_after_buy * 100) - (2 * DEFAULT_FEE_PERCENTAGE)
            
            if net_profit_after_buy >= -2.0:
                sell_order = await execute_trade('sell', sell_ex_id, pair, bought_amount_base_filled, current_sell_price)
                
                if sell_order and sell_order['status'] == 'closed':
                    final_amount_usd = float(sell_order['amount']) * float(sell_order['average'])
                    initial_amount_usd = bought_amount_base_filled * bought_price_avg
                    final_profit = final_amount_usd - initial_amount_usd
                    final_profit_percentage = (final_profit / initial_amount_usd) * 100
                    
                    msg_success = (f"✅ Arbitragem de {pair} CONCLUÍDA!\n"
                                f"Comprado em {buy_ex_id} por {bought_price_avg:.8f}\n"
                                f"Vendido em {sell_ex_id} por {sell_order['average']:.8f}\n"
                                f"Lucro Líquido: {final_profit_percentage:.2f}%\n"
                                f"Volume: ${trade_amount_usd:.2f}"
                    )
                    await bot.send_message(chat_id=chat_id, text=msg_success)
                    if pair in GLOBAL_STATS['pair_opportunities']:
                        GLOBAL_STATS['trade_outcomes']['success'] += 1
                        GLOBAL_STATS['pair_opportunities'][pair]['total_profit'] += final_profit
                else:
                    await bot.send_message(chat_id=chat_id, text=f"⚠️ Venda falhou em {sell_ex_id}. Moeda travada. Tentando colocar ordem limite.")
                    order_limit = await place_limit_order_if_stuck(sell_ex_id, pair, bought_price_avg, bought_amount_base_filled, 2.0)
                    if order_limit:
                        await bot.send_message(chat_id=chat_id, text=f"⚠️ Ordem limite de venda de {pair} colocada em {sell_ex_id}. Prejuízo máximo: 2%.")
                    if pair in GLOBAL_STATS['pair_opportunities']:
                        GLOBAL_STATS['trade_outcomes']['stuck'] += 1
            
            else:
                await bot.send_message(chat_id=chat_id, text=f"⚠️ Venda não viável (prejuízo maior que 2%). Moeda travada. Tentando colocar ordem limite.")
                order_limit = await place_limit_order_if_stuck(buy_ex_id, pair, bought_price_avg, bought_amount_base_filled, 2.0)
                if order_limit:
                    await bot.send_message(chat_id=chat_id, text=f"⚠️ Ordem limite de venda de {pair} colocada em {buy_ex_id}. Prejuízo máximo: 2%.")
                if pair in GLOBAL_STATS['pair_opportunities']:
                    GLOBAL_STATS['trade_outcomes']['stuck'] += 1
        
        except Exception as e:
            logger.error(f"Erro na fase de venda em {sell_ex_id}: {e}")
            await bot.send_message(chat_id=chat_id, text=f"⛔ Erro fatal durante a venda. Moeda travada. Verifique manualmente.")
            if pair in GLOBAL_STATS['pair_opportunities']:
                GLOBAL_STATS['trade_outcomes']['stuck'] += 1
            
    except Exception as e:
        logger.error(f"Erro na fase de compra em {buy_ex_id}: {e}")
        await bot.send_message(chat_id=chat_id, text=f"⛔ Erro fatal durante a compra. Trade cancelado.")
        if pair in GLOBAL_STATS['pair_opportunities']:
            GLOBAL_STATS['trade_outcomes']['failed'] += 1
    finally:
        if pair in GLOBAL_ACTIVE_TRADES:
            del GLOBAL_ACTIVE_TRADES[pair]

        if buy_exchange_auth:
            await buy_exchange_auth.close()
        try:
            if sell_exchange_auth:
                await sell_exchange_auth.close()
        except:
            pass

async def watch_order_book_for_pair(exchange, pair, ex_id):
    try:
        while True:
            order_book = await exchange.watch_order_book(pair)
            best_bid = order_book['bids'][0][0] if order_book['bids'] else 0
            best_bid_volume = order_book['bids'][0][1] if order_book['bids'] else 0
            best_ask = order_book['asks'][0][0] if order_book['asks'] else float('inf')
            best_ask_volume = order_book['asks'][0][1] if order_book['asks'] else 0

            GLOBAL_MARKET_DATA[pair][ex_id] = {
                'bid': float(best_bid),
                'bid_volume': float(best_bid_volume),
                'ask': float(best_ask),
                'ask_volume': float(best_ask_volume)
            }
    except ccxt.NetworkError as e:
        logger.error(f"Erro de rede no WebSocket para {pair} em {ex_id}: {e}")
    except ccxt.ExchangeError as e:
        logger.error(f"Erro da exchange no WebSocket para {pair} em {ex_id}: {e}")
    except Exception as e:
        logger.error(f"Erro inesperado no WebSocket para {pair} em {ex_id}: {e}")
    finally:
        if exchange:
            await exchange.close()

async def watch_all_exchanges():
    tasks = []
    for ex_id in EXCHANGES_LIST:
        exchange_class = getattr(ccxt, ex_id)
        exchange = exchange_class({
            'enableRateLimit': True,
            'timeout': 10000,
        })
        global_exchanges_instances[ex_id] = exchange
        
        try:
            await exchange.load_markets()
            markets_loaded[ex_id] = True
            logger.info(f"Mercados de {ex_id} carregados. Total de pares: {len(exchange.markets)}")

            for pair in PAIRS:
                if pair in exchange.markets:
                    tasks.append(asyncio.create_task(
                        watch_order_book_for_pair(exchange, pair, ex_id)
                    ))
                else:
                    logger.warning(f"Par {pair} não está disponível em {ex_id}. Ignorando...")
        except Exception as e:
            logger.error(f"ERRO ao carregar mercados de {ex_id}: {e}")
    
    await asyncio.gather(*tasks, return_exceptions=True)

# --- Novos comandos do Telegram para análise e controle ---

async def setexchanges(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        exchanges_input = [ex.strip().lower() for ex in ' '.join(context.args).split(',')]
        valid_exchanges = [ex for ex in exchanges_input if ex in EXCHANGE_CREDENTIALS]
        
        if not valid_exchanges:
            await update.message.reply_text("Nenhuma exchange válida foi fornecida. Por favor, use nomes de exchanges válidos.")
            return

        global EXCHANGES_LIST
        EXCHANGES_LIST = valid_exchanges
        # Limpa as estatísticas para as novas exchanges
        global GLOBAL_STATS
        GLOBAL_STATS['exchange_discrepancy'] = {ex: {'total_diff': 0, 'count': 0} for ex in EXCHANGES_LIST}
        await update.message.reply_text(f"Lista de exchanges para monitorar atualizada para: {', '.join(EXCHANGES_LIST)}")
        logger.info(f"Exchanges para monitorar atualizadas para: {EXCHANGES_LIST}")
        await restart_watchers(update, context)
        
    except (IndexError, ValueError):
        await update.message.reply_text("Uso incorreto. Exemplo: /setexchanges binance,kraken,okx")

async def setpairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pairs_input = [pair.strip().upper() for pair in ' '.join(context.args).split(',')]
        
        if not pairs_input:
            await update.message.reply_text("Nenhum par foi fornecido.")
            return

        global PAIRS
        PAIRS = pairs_input
        # Limpa as estatísticas para os novos pares
        global GLOBAL_STATS
        GLOBAL_STATS['pair_opportunities'] = {pair: {'count': 0, 'total_profit': 0} for pair in PAIRS}
        await update.message.reply_text(f"Lista de pares para monitorar atualizada para: {', '.join(PAIRS)}")
        logger.info(f"Pares para monitorar atualizados para: {PAIRS}")
        await restart_watchers(update, context)
        
    except (IndexError, ValueError):
        await update.message.reply_text("Uso incorreto. Exemplo: /setpairs BTC/USDT,ETH/USDT")

async def restart_watchers(update, context):
    await update.message.reply_text("Reiniciando os monitores de mercado com as novas configurações...")
    
    global global_exchanges_instances
    
    for ex in global_exchanges_instances.values():
        if ex:
            await ex.close()
    
    global_exchanges_instances = {}
    
    await asyncio.create_task(watch_all_exchanges())

async def report_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    report_text = "📊 **Relatório de Análise de Mercado** 📊\n\n"
    
    report_text += "📈 **Oportunidades por Par (Último período)**\n"
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

# --- Funções principais do bot (main, start, etc.) permanecem as mesmas ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['admin_chat_id'] = update.message.chat_id
    await update.message.reply_text(
        "Olá! Bot de Arbitragem Ativado.\n"
        "Configurações atuais:\n"
        f"Lucro mínimo: {context.bot_data.get('lucro_minimo_porcentagem', DEFAULT_LUCRO_MINIMO_PORCENTAGEM)}%\n"
        f"Volume de trade: ${context.bot_data.get('trade_amount_usd', DEFAULT_TRADE_AMOUNT_USD):.2f}\n"
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
        context.bot_data['trade_amount_usd'] = valor
        await update.message.reply_text(f"Volume de trade para checagem de liquidez atualizado para ${valor:.2f} USD")
    except (IndexError, ValueError):
        await update.message.reply_text("Uso incorreto. Exemplo: /setvolume 100")

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

async def stop_arbitrage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['admin_chat_id'] = None
    await update.message.reply_text("Alertas e simulações desativados. Use /start para reativar.")

# Novo comando para silenciar o bot
async def silenciar_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['admin_chat_id'] = None
    await update.message.reply_text("Bot silenciado. Nenhum alerta será enviado. Use /start para reativar.")
    logger.info(f"Alertas silenciados por {update.message.chat_id}")

async def start_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        valor = float(context.args[0])
        if valor <= 0:
            await update.message.reply_text("O valor de trade deve ser um valor positivo.")
            return
        
        context.bot_data['trade_amount_usd'] = valor
        await update.message.reply_text(f"Bot configurado para iniciar trades com ${valor:.2f} USD. Agora procurando oportunidades...")
    except (IndexError, ValueError):
        await update.message.reply_text("Uso incorreto. Exemplo: /starttrade 50")

async def main():
    application = ApplicationBuilder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlucro", setlucro))
    application.add_handler(CommandHandler("setvolume", setvolume))
    application.add_handler(CommandHandler("setfee", setfee))
    application.add_handler(CommandHandler("stop", stop_arbitrage))
    application.add_handler(CommandHandler("starttrade", start_trade))
    application.add_handler(CommandHandler("setexchanges", setexchanges))
    application.add_handler(CommandHandler("setpairs", setpairs))
    application.add_handler(CommandHandler("report_stats", report_stats))
    application.add_handler(CommandHandler("silenciar", silenciar_alerts))


    try:
        logger.info("Tentando registrar comandos no Telegram...")
        await application.bot.set_my_commands([
            BotCommand("start", "Iniciar o bot e reativar alertas"),
            BotCommand("setlucro", "Definir lucro mínimo em % (Ex: /setlucro 2.5)"),
            BotCommand("setvolume", "Definir volume de trade em USD (Ex: /setvolume 100)"),
            BotCommand("setfee", "Definir taxa de negociação em % (Ex: /setfee 0.075)"),
            BotCommand("starttrade", "Configurar volume e iniciar a busca (Ex: /starttrade 50)"),
            BotCommand("setexchanges", "Configurar exchanges para monitorar (Ex: /setexchanges binance,kraken)"),
            BotCommand("setpairs", "Configurar pares para monitorar (Ex: /setpairs BTC/USDT,ETH/USDT)"),
            BotCommand("report_stats", "Gerar um relatório de análise de mercado"),
            BotCommand("stop", "Parar de receber alertas e simulações"),
            BotCommand("silenciar", "Silenciar todos os alertas")
        ])
        logger.info("Comandos registrados com sucesso!")
    except Exception as e:
        logger.error(f"Falha ao registrar comandos no Telegram: {e}")

    logger.info("Bot iniciado com sucesso e aguardando mensagens...")

    try:
        asyncio.create_task(watch_all_exchanges())
        asyncio.create_task(check_arbitrage_opportunities(application))
        asyncio.create_task(analyze_market_data())
        
        await application.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)

    except Exception as e:
        logger.error(f"Erro no loop principal do bot: {e}", exc_info=True)
    finally:
        logger.info("Fechando conexões das exchanges...")
        tasks = [ex.close() for ex in global_exchanges_instances.values() if ex]
        await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == "__main__":
    asyncio.run(main())
