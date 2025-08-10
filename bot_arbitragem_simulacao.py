import os
import asyncio
import logging
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import ccxt.pro as ccxt_pro
import ccxt as ccxt_rest
import nest_asyncio
import time
from decimal import Decimal

# --- Aplica o patch para permitir loops aninhados ---
nest_asyncio.apply()

# --- Configurações básicas e chaves de API ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Variáveis de ambiente padrão, podem ser alteradas com comandos
DEFAULT_LUCRO_MINIMO_PORCENTAGEM = float(os.getenv("DEFAULT_LUCRO_MINIMO_PORCENTAGEM", 1.0))
DEFAULT_FEE_PERCENTAGE = float(os.getenv("DEFAULT_FEE_PERCENTAGE", 0.1))
DEFAULT_MIN_USDT_BALANCE = float(os.getenv("DEFAULT_MIN_USDT_BALANCE", 10.0))
DEFAULT_TOTAL_CAPITAL = float(os.getenv("DEFAULT_TOTAL_CAPITAL", 50.0)) # Seu capital inicial
DEFAULT_TRADE_PERCENTAGE = float(os.getenv("DEFAULT_TRADE_PERCENTAGE", 2.0)) # Porcentagem do capital a ser usada por trade (10% = 0.1)

# Modo de segurança: True para simulação, False para trades reais
DRY_RUN_MODE = os.getenv("DRY_RUN_MODE", "True").lower() == "true"

# Dicionário para armazenar as chaves de API das exchanges
EXCHANGE_CREDENTIALS = {
    'binance': {'apiKey': os.getenv("BINANCE_API_KEY"), 'secret': os.getenv("BINANCE_SECRET")},
    'kraken': {'apiKey': os.getenv("KRAKEN_API_KEY"), 'secret': os.getenv("KRAKEN_SECRET")},
    'okx': {'apiKey': os.getenv("OKX_API_KEY"), 'secret': os.getenv("OKX_SECRET")},
    'bybit': {'apiKey': os.getenv("BYBIT_API_KEY"), 'secret': os.getenv("BYBIT_SECRET"), 'password': os.getenv("BYBIT_PASSWORD")},
    'kucoin': {'apiKey': os.getenv("KUCOIN_API_KEY"), 'secret': os.getenv("KUCOIN_SECRET")},
    'bitstamp': {'apiKey': os.getenv("BITSTAMP_API_KEY"), 'secret': os.getenv("BITSTAMP_SECRET")},
    'bitget': {'apiKey': os.getenv("BITGET_API_KEY"), 'secret': os.getenv("BITGET_SECRET")},
    'coinbase': {'apiKey': os.getenv("COINBASE_API_KEY"), 'secret': os.getenv("COINBASE_SECRET")},
    'huobi': {'apiKey': os.getenv("HUOBI_API_KEY"), 'secret': os.getenv("HUOBI_SECRET")},
    'gateio': {'apiKey': os.getenv("GATEIO_API_KEY"), 'secret': os.getenv("GATEIO_SECRET")},
}

# Exchanges confiáveis para monitorar
# ATENÇÃO: Alterado para Kucoin e Okx para evitar restrições da Binance.
EXCHANGES_LIST = ['kucoin', 'okx']

# Pares USDT - OTIMIZADA
PAIRS = ["BTC/USDT"]

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

# --- Novas estruturas de dados para análise e gerenciamento de capital ---
GLOBAL_STATS = {
    'exchange_discrepancy': {ex: {'total_diff': 0, 'count': 0} for ex in EXCHANGES_LIST},
    'pair_opportunities': {pair: {'count': 0, 'total_profit': 0} for pair in PAIRS},
    'trade_outcomes': {'success': 0, 'stuck': 0, 'failed': 0}
}
GLOBAL_TOTAL_CAPITAL_USDT = DEFAULT_TOTAL_CAPITAL
GLOBAL_BALANCES = {ex: {'USDT': 0.0} for ex in EXCHANGES_LIST}

# Lista global para gerenciar as tasks de watcher
watcher_tasks = []

async def get_exchange_instance(ex_id, authenticated=False, is_rest=False):
    """
    Retorna uma instância de exchange ccxt (REST) ou ccxt.pro (async).
    """
    if authenticated and not EXCHANGE_CREDENTIALS.get(ex_id):
        logger.error(f"Credenciais de API não encontradas para {ex_id}.")
        return None
    
    config = {
        'enableRateLimit': True,
        'timeout': 10000,
    }
    if authenticated and EXCHANGE_CREDENTIALS.get(ex_id):
        config.update(EXCHANGE_CREDENTIALS[ex_id])
    
    try:
        # <-- Alterado
        exchange_module = ccxt_pro if not is_rest else ccxt_rest
        exchange_class = getattr(exchange_module, ex_id)
        instance = exchange_class(config)
        return instance
    except Exception as e:
        logger.error(f"Erro ao instanciar exchange {ex_id}: {e}")
        return None

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

    exchange_rest = await get_exchange_instance(exchange_id, authenticated=True, is_rest=True)
    if not exchange_rest:
        return None
    
    try:
        if action == 'buy':
            order = exchange_rest.create_order(pair, 'market', 'buy', amount_base)
        elif action == 'sell':
            order = exchange_rest.create_order(pair, 'market', 'sell', amount_base)

        return order
    except Exception as e:
        logger.error(f"Erro ao executar ordem de {action} em {exchange_id}: {e}")
        return None

async def sell_stuck_position_if_needed(application, pair):
    """
    Função de saída de emergência para moedas travadas.
    """
    bot = application.bot
    chat_id = application.bot_data.get('admin_chat_id')
    
    if pair not in GLOBAL_STUCK_POSITIONS:
        return
        
    stuck_info = GLOBAL_STUCK_POSITIONS[pair]
    
    # 5 minutos para uma ordem travada antes de uma venda a mercado
    if time.time() - stuck_info['timestamp'] > 300: 
        logger.warning(f"Posição travada de {pair} em {stuck_info['exchange_id']} excedeu o tempo limite. Executando venda a mercado.")
        await bot.send_message(chat_id=chat_id, text=f"🚨 Saída de emergência: Venda de {pair} em {stuck_info['exchange_id']} a preço de mercado.")
        
        try:
            exchange_rest = await get_exchange_instance(stuck_info['exchange_id'], authenticated=True, is_rest=True)
            sell_order = exchange_rest.create_order(pair, 'market', 'sell', stuck_info['amount_base'])
            
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
        
        await asyncio.sleep(60) # Executa a cada minuto

async def check_arbitrage_opportunities(application):
    bot = application.bot
    while True:
        try:
            chat_id = application.bot_data.get('admin_chat_id')
            if not chat_id:
                await asyncio.sleep(5)
                continue

            # Verifica e executa saídas de emergência para posições travadas
            for pair in list(GLOBAL_STUCK_POSITIONS.keys()):
                await sell_stuck_position_if_needed(application, pair)
            
            if GLOBAL_ACTIVE_TRADES:
                await asyncio.sleep(5)
                continue

            lucro_minimo = application.bot_data.get('lucro_minimo_porcentagem', DEFAULT_LUCRO_MINIMO_PORCENTAGEM)
            # Juros compostos: o volume de trade é uma porcentagem do capital total
            trade_percentage = application.bot_data.get('trade_percentage', DEFAULT_TRADE_PERCENTAGE)
            trade_amount_usd = GLOBAL_TOTAL_CAPITAL_USDT * (trade_percentage / 100)

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
                
                # Checagem de preços com REST
                try:
                    buy_exchange_rest = await get_exchange_instance(buy_ex_id, authenticated=False, is_rest=True)
                    sell_exchange_rest = await get_exchange_instance(sell_ex_id, authenticated=False, is_rest=True)
                    
                    if not buy_exchange_rest or not sell_exchange_rest:
                        logger.warning(f"Não foi possível obter a instância da exchange REST para {pair}. Pulando.")
                        continue

                    ticker_buy = await buy_exchange_rest.fetch_ticker(pair)
                    ticker_sell = await sell_exchange_rest.fetch_ticker(pair)
                    
                    confirmed_buy_price = ticker_buy['ask']
                    confirmed_sell_price = ticker_sell['bid']
                    
                except Exception as e:
                    logger.warning(f"Falha na checagem REST para {pair}: {e}. Pulando.")
                    continue

                gross_profit = (confirmed_sell_price - confirmed_buy_price) / confirmed_buy_price
                gross_profit_percentage = gross_profit * 100
                net_profit_percentage = gross_profit_percentage - (2 * fee * 100)

                # Novo alerta para oportunidades menores que o mínimo
                if net_profit_percentage >= 0.5 and net_profit_percentage < lucro_minimo:
                    current_time = time.time()
                    if pair not in last_alert_times or (current_time - last_alert_times[pair]) > COOLDOWN_SECONDS:
                        await bot.send_message(chat_id=chat_id, text=f"👀 Oportunidade menor que o lucro mínimo para {pair}!\nLucro: {net_profit_percentage:.2f}%.")
                        last_alert_times[pair] = current_time

                if net_profit_percentage >= lucro_minimo:
                    # Verifica o saldo de USDT antes de considerar a oportunidade
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

        # 2. Inicia o monitoramento para saída de emergência
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
            
            gross_profit_after_buy = (current_sell_price - bought_price_avg) / bought_price_avg
            net_profit_after_buy = (gross_profit_after_buy * 100) - (2 * DEFAULT_FEE_PERCENTAGE)
            
            if net_profit_after_buy >= -2.0: # Regra de saída: vender mesmo com pequeno prejuízo
                sell_order = await execute_trade('sell', sell_ex_id, pair, bought_amount_base_filled, current_sell_price)
                
                if sell_order and sell_order['status'] == 'closed':
                    final_amount_usd = float(sell_order['amount']) * float(sell_order['average'])
                    initial_amount_usd = bought_amount_base_filled * bought_price_avg
                    final_profit = final_amount_usd - initial_amount_usd
                    final_profit_percentage = (final_profit / initial_amount_usd) * 100
                    
                    global GLOBAL_TOTAL_CAPITAL_USDT
                    GLOBAL_TOTAL_CAPITAL_USDT += final_profit
                    
                    msg_success = (f"✅ Arbitragem de {pair} CONCLUÍDA!\n"
                                f"Comprado em {buy_ex_id} por {bought_price_avg:.8f}\n"
                                f"Vendido em {sell_ex_id} por {sell_order['average']:.8f}\n"
                                f"Lucro Líquido: {final_profit_percentage:.2f}%\n"
                                f"Capital Total Atualizado: ${GLOBAL_TOTAL_CAPITAL_USDT:.2f}"
                    )
                    await bot.send_message(chat_id=chat_id, text=msg_success)
                    GLOBAL_STATS['trade_outcomes']['success'] += 1
                    if pair in GLOBAL_STATS['pair_opportunities']:
                        GLOBAL_STATS['pair_opportunities'][pair]['total_profit'] += final_profit
                else:
                    await bot.send_message(chat_id=chat_id, text=f"⚠️ Venda falhou em {sell_ex_id}. Moeda travada.")
                    GLOBAL_STATS['trade_outcomes']['stuck'] += 1
            
            else:
                await bot
