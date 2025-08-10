import os
import asyncio
import logging
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import ccxt.pro as ccxt_pro
import ccxt
import nest_asyncio
import time
from decimal import Decimal

nest_asyncio.apply()

# Configurações via variáveis de ambiente já definidas no seu ambiente Heroku/GitHub/etc
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

DEFAULT_LUCRO_MINIMO_PORCENTAGEM = float(os.getenv("DEFAULT_LUCRO_MINIMO_PORCENTAGEM", 2.0))
DEFAULT_FEE_PERCENTAGE = float(os.getenv("DEFAULT_FEE_PERCENTAGE", 0.1))
DEFAULT_MIN_USDT_BALANCE = float(os.getenv("DEFAULT_MIN_USDT_BALANCE", 10.0))
DEFAULT_TOTAL_CAPITAL = float(os.getenv("DEFAULT_TOTAL_CAPITAL", 500.0))
DEFAULT_TRADE_PERCENTAGE = float(os.getenv("DEFAULT_TRADE_PERCENTAGE", 10.0))
DRY_RUN_MODE = os.getenv("DRY_RUN_MODE", "True").lower() == "true"

EXCHANGE_CREDENTIALS = {
    'binance': {'apiKey': os.getenv("BINANCE_API_KEY"), 'secret': os.getenv("BINANCE_SECRET")},
    'kraken': {'apiKey': os.getenv("KRAKEN_API_KEY"), 'secret': os.getenv("KRAKEN_SECRET")},
    'okx': {'apiKey': os.getenv("OKX_API_KEY"), 'secret': os.getenv("OKX_SECRET")},
    'bybit': {'apiKey': os.getenv("BYBIT_API_KEY"), 'secret': os.getenv("BYBIT_SECRET"), 'password': os.getenv("BYBIT_PASSWORD")},
    'kucoin': {'apiKey': os.getenv("KUCOIN_API_KEY"), 'secret': os.getenv("KUCOIN_SECRET")},
    'bitstamp': {'apiKey': os.getenv("BITSTAMP_API_KEY"), 'secret': os.getenv("BITSTAMP_SECRET")},
    'bitget': {'apiKey': os.getenv("BITGET_API_KEY"), 'secret': os.getenv("BITGET_SECRET")},
    'coinbase': {'apiKey': os.getenv("COINBASE_API_KEY"), 'secret': os.getenv("COINBASE_SECRET")},
    'htx': {'apiKey': os.getenv("HTX_API_KEY"), 'secret': os.getenv("HTX_SECRET")},
    'gate': {'apiKey': os.getenv("GATE_API_KEY"), 'secret': os.getenv("GATE_SECRET")},
    'cryptocom': {'apiKey': os.getenv("CRYPTOCOM_API_KEY"), 'secret': os.getenv("CRYPTOCOM_SECRET")},
    'gemini': {'apiKey': os.getenv("GEMINI_API_KEY"), 'secret': os.getenv("GEMINI_SECRET")},
}

EXCHANGES_LIST = [
    'binance', 'coinbase', 'kraken', 'okx', 'bybit',
    'kucoin', 'bitstamp', 'bitget',
]

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

GLOBAL_STATS = {
    'exchange_discrepancy': {ex: {'total_diff': 0, 'count': 0} for ex in EXCHANGES_LIST},
    'pair_opportunities': {pair: {'count': 0, 'total_profit': 0} for pair in PAIRS},
    'trade_outcomes': {'success': 0, 'stuck': 0, 'failed': 0}
}
GLOBAL_TOTAL_CAPITAL_USDT = DEFAULT_TOTAL_CAPITAL
GLOBAL_BALANCES = {ex: {'USDT': 0.0} for ex in EXCHANGES_LIST}

async def get_exchange_instance(ex_id, authenticated=False, is_rest=False):
    if authenticated and not EXCHANGE_CREDENTIALS.get(ex_id):
        logger.error(f"Credenciais API não encontradas para {ex_id}.")
        return None
    config = {'enableRateLimit': True, 'timeout': 10000}
    if authenticated and EXCHANGE_CREDENTIALS.get(ex_id):
        config.update(EXCHANGE_CREDENTIALS[ex_id])
    try:
        exchange_class = getattr(ccxt_pro if not is_rest else ccxt, ex_id)
        return exchange_class(config)
    except Exception as e:
        logger.error(f"Erro ao criar instância {ex_id}: {e}")
        return None

async def execute_trade(action, exchange_id, pair, amount_base, price=None):
    if DRY_RUN_MODE:
        logger.info(f"[DRY_RUN] {action} {amount_base:.8f} {pair.split('/')[0]} em {exchange_id} a preço {price}")
        return {'status': 'closed', 'id': f'dry_run_{int(time.time())}', 'amount': amount_base, 'price': price, 'average': price, 'side': action, 'symbol': pair}
    exchange_rest = await get_exchange_instance(exchange_id, authenticated=True, is_rest=True)
    if not exchange_rest:
        return None
    try:
        if action == 'buy':
            return await exchange_rest.create_order(pair, 'market', 'buy', amount_base)
        elif action == 'sell':
            return await exchange_rest.create_order(pair, 'market', 'sell', amount_base)
    except Exception as e:
        logger.error(f"Erro na ordem {action} em {exchange_id}: {e}")
        return None

async def watch_order_book_for_pair(exchange, pair, ex_id):
    try:
        while True:
            order_book = await exchange.watch_order_book(pair)
            bids = order_book.get('bids', [])
            asks = order_book.get('asks', [])
            best_bid = bids[0][0] if bids else 0
            best_bid_volume = bids[0][1] if bids else 0
            best_ask = asks[0][0] if asks else float('inf')
            best_ask_volume = asks[0][1] if asks else 0
            GLOBAL_MARKET_DATA[pair][ex_id] = {'bid': best_bid, 'bid_volume': best_bid_volume, 'ask': best_ask, 'ask_volume': best_ask_volume}
    except Exception as e:
        logger.error(f"Erro websocket {pair} em {ex_id}: {e}")
        await asyncio.sleep(5)
        new_exchange = await get_exchange_instance(ex_id)
        if new_exchange:
            await watch_order_book_for_pair(new_exchange, pair, ex_id)

async def watch_all_exchanges():
    tasks = []
    for ex_id in EXCHANGES_LIST:
        exchange = await get_exchange_instance(ex_id)
        if not exchange:
            logger.error(f"Não pode criar instância de {ex_id}")
            continue
        global_exchanges_instances[ex_id] = exchange
        try:
            await exchange.load_markets()
            markets_loaded[ex_id] = True
            for pair in PAIRS:
                if pair in exchange.markets:
                    tasks.append(asyncio.create_task(watch_order_book_for_pair(exchange, pair, ex_id)))
        except Exception as e:
            logger.error(f"Erro ao carregar mercados {ex_id}: {e}")
    await asyncio.gather(*tasks, return_exceptions=True)

async def update_all_balances():
    for ex_id in EXCHANGES_LIST:
        exchange_rest = await get_exchange_instance(ex_id, authenticated=True, is_rest=True)
        if exchange_rest:
            try:
                balance = await exchange_rest.fetch_balance()
                GLOBAL_BALANCES[ex_id]['USDT'] = float(balance['free'].get('USDT', 0))
            except Exception as e:
                logger.error(f"Erro atualizar saldo {ex_id}: {e}")

async def analyze_market_data():
    while True:
        try:
            for pair in PAIRS:
                market_data = GLOBAL_MARKET_DATA[pair]
                if len(market_data) < 2:
                    continue
                best_buy_price = float('inf')
                best_sell_price = 0
                buy_ex_id = None
                sell_ex_id = None
                for ex_id, data in market_data.items():
                    ask = data.get('ask')
                    bid = data.get('bid')
                    if ask and ask < best_buy_price:
                        best_buy_price = ask
                        buy_ex_id = ex_id
                    if bid and bid > best_sell_price:
                        best_sell_price = bid
                        sell_ex_id = ex_id
                if buy_ex_id and sell_ex_id and buy_ex_id != sell_ex_id:
                    gross_profit_pct = ((best_sell_price - best_buy_price) / best_buy_price) * 100
                    GLOBAL_STATS['pair_opportunities'][pair]['count'] += 1
                    GLOBAL_STATS['exchange_discrepancy'][buy_ex_id]['total_diff'] += gross_profit_pct
                    GLOBAL_STATS['exchange_discrepancy'][buy_ex_id]['count'] += 1
                    GLOBAL_STATS['exchange_discrepancy'][sell_ex_id]['total_diff'] += gross_profit_pct
                    GLOBAL_STATS['exchange_discrepancy'][sell_ex_id]['count'] += 1
        except Exception as e:
            logger.error(f"Erro análise dados mercado: {e}")
        await asyncio.sleep(60)

async def check_arbitrage_opportunities(application):
    bot = application.bot
    while True:
        try:
            chat_id = application.bot_data.get('admin_chat_id')
            if not chat_id:
                await asyncio.sleep(5)
                continue
            # Aqui lógica para arbitragem
            if GLOBAL_ACTIVE_TRADES:
                await asyncio.sleep(5)
                continue
            lucro_minimo = application.bot_data.get('lucro_minimo_porcentagem', DEFAULT_LUCRO_MINIMO_PORCENTAGEM)
            trade_percentage = application.bot_data.get('trade_percentage', DEFAULT_TRADE_PERCENTAGE)
            trade_amount_usd = GLOBAL_TOTAL_CAPITAL_USDT * (trade_percentage / 100)
            fee = application.bot_data.get('fee_percentage', DEFAULT_FEE_PERCENTAGE) / 100.0
            best_opportunity = None
            for pair in PAIRS:
                market_data = GLOBAL_MARKET_DATA[pair]
                if len(market_data) < 2:
                    continue
                best_buy_price = float('inf')
                best_sell_price = 0
                buy_ex_id = None
                sell_ex_id = None
                for ex_id, data in market_data.items():
                    ask = data.get('ask')
                    bid = data.get('bid')
                    if ask and ask < best_buy_price:
                        best_buy_price = ask
                        buy_ex_id = ex_id
                    if bid and bid > best_sell_price:
                        best_sell_price = bid
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
                except Exception as e:
                    logger.warning(f"Falha REST {pair}: {e}")
                    continue
                gross_profit = (confirmed_sell_price - confirmed_buy_price) / confirmed_buy_price
                gross_profit_percentage = gross_profit * 100
                net_profit_percentage = gross_profit_percentage - (2 * fee * 100)
                if GLOBAL_BALANCES.get(buy_ex_id, {}).get('USDT', 0) < trade_amount_usd + DEFAULT_MIN_USDT_BALANCE:
                    logger.info(f"Saldo insuficiente {buy_ex_id} para {pair}.")
                    continue
                if net_profit_percentage >= lucro_minimo:
                    if best_opportunity is None or net_profit_percentage > best_opportunity['net_profit']:
                        best_opportunity = {
                            'pair': pair,
                            'buy_ex_id': buy_ex_id,
                            'sell_ex_id': sell_ex_id,
                            'buy_price': confirmed_buy_price,
                            'sell_price': confirmed_sell_price,
                            'net_profit': net_profit_percentage,
                            'trade_amount_usd': trade_amount_usd,
                        }
            if best_opportunity:
                last_time = last_alert_times.get(best_opportunity['pair'], 0)
                if time.time() - last_time > COOLDOWN_SECONDS:
                    msg = (
                        f"🚀 Oportunidade de Arbitragem!\n"
                        f"Par: {best_opportunity['pair']}\n"
                        f"Comprar em {best_opportunity['buy_ex_id']} por {best_opportunity['buy_price']:.6f}\n"
                        f"Vender em {best_opportunity['sell_ex_id']} por {best_opportunity['sell_price']:.6f}\n"
                        f"Lucro líquido estimado: {best_opportunity['net_profit']:.2f}%\n"
                        f"Valor para trade: USDT {best_opportunity['trade_amount_usd']:.2f}"
                    )
                    await bot.send_message(chat_id=chat_id, text=msg)
                    last_alert_times[best_opportunity['pair']] = time.time()
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Erro em check_arbitrage_opportunities: {e}")
            await asyncio.sleep(5)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot CryptoAlertsBot2 ativo!\nUse /setlucro para definir lucro mínimo.\nUse /status para ver estatísticas."
    )
    context.application.bot_data['admin_chat_id'] = update.message.chat_id

async def set_lucro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /setlucro <percentual>")
        return
    try:
        lucro = float(context.args[0])
        context.application.bot_data['lucro_minimo_porcentagem'] = lucro
        await update.message.reply_text(f"Lucro mínimo definido para {lucro}%")
    except Exception:
        await update.message.reply_text("Por favor, envie um número válido.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "Estatísticas:\n"
    for pair, stats in GLOBAL_STATS['pair_opportunities'].items():
        if stats['count'] > 0:
            msg += f"{pair}: Oportunidades {stats['count']}, Lucro total {stats['total_profit']:.2f}%\n"
    await update.message.reply_text(msg)

async def main():
    application = ApplicationBuilder().token(TOKEN).build()
    application.bot_data['lucro_minimo_porcentagem'] = DEFAULT_LUCRO_MINIMO_PORCENTAGEM
    application.bot_data['fee_percentage'] = DEFAULT_FEE_PERCENTAGE
    application.bot_data['trade_percentage'] = DEFAULT_TRADE_PERCENTAGE

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlucro", set_lucro))
    application.add_handler(CommandHandler("status", status))

    # Executa tarefas paralelas
    asyncio.create_task(watch_all_exchanges())
    asyncio.create_task(update_all_balances())
    asyncio.create_task(analyze_market_data())
    asyncio.create_task(check_arbitrage_opportunities(application))

    await application.run_polling()

if __name
