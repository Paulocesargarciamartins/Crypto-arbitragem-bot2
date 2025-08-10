import asyncio
import logging
import time
from decimal import Decimal

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

import ccxt.pro as ccxt_pro
import ccxt
import nest_asyncio
nest_asyncio.apply()

# --- CONFIGURAÇÕES FIXAS DENTRO DO CÓDIGO ---

# Token do Telegram (coloque seu token aqui)
TOKEN = "SEU_TOKEN_TELEGRAM_AQUI"

# Chat ID do administrador (coloque seu ID aqui para receber alertas)
ADMIN_CHAT_ID = 123456789  # substitua pelo seu chat_id

# Lucro mínimo para executar arbitragem (%)
DEFAULT_LUCRO_MINIMO_PORCENTAGEM = 2.0

# Taxa média de fee (%)
DEFAULT_FEE_PERCENTAGE = 0.1

# Percentual do capital total para cada trade (%)
DEFAULT_TRADE_PERCENTAGE = 10.0

# Capital total inicial (USDT)
DEFAULT_TOTAL_CAPITAL = 500.0

# Modo Dry Run: True = simula ordens (não executa)
DRY_RUN_MODE = True

# Credenciais das exchanges (API keys e secrets, se quiser testar ordens reais)
EXCHANGE_CREDENTIALS = {
    'binance': {'apiKey': '', 'secret': ''},
    'kraken': {'apiKey': '', 'secret': ''},
    'okx': {'apiKey': '', 'secret': ''},
}

EXCHANGES_LIST = list(EXCHANGE_CREDENTIALS.keys())

# Pares prioritários para monitorar
PAIRS_PRIORITARIOS = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"
]

PAIRS = PAIRS_PRIORITARIOS[:]

# --- LOGGER ---
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- VARIÁVEIS GLOBAIS ---
global_exchanges_instances = {}
GLOBAL_MARKET_DATA = {pair: {} for pair in PAIRS}
GLOBAL_BALANCES = {ex: {'USDT': 0.0} for ex in EXCHANGES_LIST}
GLOBAL_ACTIVE_TRADES = {}
GLOBAL_TOTAL_CAPITAL_USDT = DEFAULT_TOTAL_CAPITAL


async def get_exchange_instance(ex_id, authenticated=False, is_rest=False):
    config = {'enableRateLimit': True, 'timeout': 10000}
    if authenticated and EXCHANGE_CREDENTIALS.get(ex_id):
        config.update(EXCHANGE_CREDENTIALS[ex_id])
    try:
        exchange_class = getattr(ccxt_pro if not is_rest else ccxt, ex_id)
        exchange = exchange_class(config)
        return exchange
    except Exception as e:
        logger.error(f"Erro criar instância {ex_id}: {e}")
        return None


async def update_all_balances():
    for ex_id in EXCHANGES_LIST:
        exchange_rest = await get_exchange_instance(ex_id, authenticated=True, is_rest=True)
        if exchange_rest:
            try:
                balance = await exchange_rest.fetch_balance()
                usdt_free = balance['free'].get('USDT', 0)
                GLOBAL_BALANCES[ex_id]['USDT'] = float(usdt_free)
                logger.info(f"Saldo {ex_id}: USDT = {usdt_free:.2f}")
            except Exception as e:
                logger.error(f"Erro atualizar saldo {ex_id}: {e}")


async def execute_trade(action, exchange_id, pair, amount_base, price=None):
    if DRY_RUN_MODE:
        logger.info(f"[DRY_RUN] {action.upper()} {amount_base:.8f} {pair.split('/')[0]} em {exchange_id} a preço {price}")
        return {'status': 'closed', 'id': f'dry_run_{int(time.time())}', 'amount': amount_base, 'price': price, 'average': price, 'side': action, 'symbol': pair}

    exchange_rest = await get_exchange_instance(exchange_id, authenticated=True, is_rest=True)
    if not exchange_rest:
        return None
    try:
        if action == 'buy':
            order = await exchange_rest.create_order(pair, 'market', 'buy', amount_base)
        elif action == 'sell':
            order = await exchange_rest.create_order(pair, 'market', 'sell', amount_base)
        else:
            logger.error(f"Ação inválida: {action}")
            return None
        return order
    except Exception as e:
        logger.error(f"Erro ordem {action} em {exchange_id}: {e}")
        return None


async def watch_order_book_for_pair(exchange, pair, ex_id):
    while True:
        try:
            order_book = await exchange.watch_order_book(pair)
            bids = order_book.get('bids', [])
            asks = order_book.get('asks', [])
            best_bid = bids[0][0] if bids else 0
            best_bid_volume = bids[0][1] if bids else 0
            best_ask = asks[0][0] if asks else float('inf')
            best_ask_volume = asks[0][1] if asks else 0
            GLOBAL_MARKET_DATA[pair][ex_id] = {
                'bid': best_bid,
                'bid_volume': best_bid_volume,
                'ask': best_ask,
                'ask_volume': best_ask_volume
            }
        except Exception as e:
            logger.error(f"Erro websocket {pair} em {ex_id}: {e}")
            await asyncio.sleep(5)
            new_exchange = await get_exchange_instance(ex_id)
            if new_exchange:
                exchange = new_exchange


async def check_arbitrage_opportunities(application):
    bot = application.bot
    lucro_minimo = application.bot_data.get('lucro_minimo_porcentagem', DEFAULT_LUCRO_MINIMO_PORCENTAGEM)
    fee = application.bot_data.get('fee_percentage', DEFAULT_FEE_PERCENTAGE) / 100.0
    trade_percentage = application.bot_data.get('trade_percentage', DEFAULT_TRADE_PERCENTAGE)
    trade_amount_usd = GLOBAL_TOTAL_CAPITAL_USDT * (trade_percentage / 100)
    chat_id = application.bot_data.get('admin_chat_id')

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
                    net_profit_pct = gross_profit_pct - (fee * 2 * 100)

                    if net_profit_pct >= lucro_minimo:
                        buy_balance = GLOBAL_BALANCES.get(buy_ex_id, {}).get('USDT', 0)
                        sell_balance = GLOBAL_BALANCES.get(sell_ex_id, {}).get('USDT', 0)

                        if buy_balance >= trade_amount_usd and sell_balance >= trade_amount_usd:
                            amount_to_buy = trade_amount_usd / best_buy_price
                            trade_id = f"{pair}-{int(time.time())}"

                            GLOBAL_ACTIVE_TRADES[trade_id] = {
                                'pair': pair,
                                'buy_ex': buy_ex_id,
                                'sell_ex': sell_ex_id,
                                'amount': amount_to_buy,
                                'buy_price': best_buy_price,
                                'sell_price': best_sell_price,
                                'net_profit_pct': net_profit_pct,
                                'start_time': time.time(),
                            }

                            buy_order = await execute_trade('buy', buy_ex_id, pair, amount_to_buy, price=best_buy_price)
                            sell_order = await execute_trade('sell', sell_ex_id, pair, amount_to_buy, price=best_sell_price)

                            logger.info(f"Arbitragem executada {pair}: BUY {buy_ex_id} @ {best_buy_price:.6f}, SELL {sell_ex_id} @ {best_sell_price:.6f}, lucro líquido {net_profit_pct:.2f}%")

                            GLOBAL_ACTIVE_TRADES.pop(trade_id, None)

                            if chat_id:
                                msg = (
                                    f"⚡ Arbitragem executada para {pair}:\n"
                                    f"Compra em {buy_ex_id} a {best_buy_price:.6f}\n"
                                    f"Venda em {sell_ex_id} a {best_sell_price:.6f}\n"
                                    f"Lucro líquido estimado: {net_profit_pct:.2f}%\n"
                                    f"Quantidade: {amount_to_buy:.6f} {pair.split('/')[0]}"
                                )
                                await bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            logger.error(f"Erro monitorar oportunidades: {e}")

        await asyncio.sleep(5)


# --- Comandos Telegram ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot de Arbitragem iniciado! Use /status para ver o status.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "Status das exchanges e balanços:\n"
    for ex_id in EXCHANGES_LIST:
        saldo = GLOBAL_BALANCES.get(ex_id, {}).get('USDT', 0)
        msg += f"{ex_id}: USDT = {saldo:.2f}\n"
    await update.message.reply_text(msg)


async def lucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lucro_min = context.application.bot_data.get('lucro_minimo_porcentagem', DEFAULT_LUCRO_MINIMO_PORCENTAGEM)
    await update.message.reply_text(f"Lucro mínimo configurado: {lucro_min:.2f}%")


async def setlucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        try:
            valor = float(context.args[0])
            context.application.bot_data['lucro_minimo_porcentagem'] = valor
            await update.message.reply_text(f"Lucro mínimo atualizado para {valor:.2f}%")
        except:
            await update.message.reply_text("Use /setlucro <valor> para definir o lucro mínimo.")
    else:
        await update.message.reply_text("Use /setlucro <valor> para definir o lucro mínimo.")


async def report_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "Estatísticas:\n"
    total_trades = len(GLOBAL_ACTIVE_TRADES)
    msg += f"Trades ativos: {total_trades}\n"
    await update.message.reply_text(msg)


async def main():
    application = ApplicationBuilder().token(TOKEN).build()

    application.bot_data['lucro_minimo_porcentagem'] = DEFAULT_LUCRO_MINIMO_PORCENTAGEM
    application.bot_data['fee_percentage'] = DEFAULT_FEE_PERCENTAGE
    application.bot_data['trade_percentage'] = DEFAULT_TRADE_PERCENTAGE
    application.bot_data['admin_chat_id'] = ADMIN_CHAT_ID

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("lucro", lucro_command))
    application.add_handler(CommandHandler("setlucro", setlucro_command))
    application.add_handler(CommandHandler("report_stats", report_stats_command))

    for ex_id in EXCHANGES_LIST:
        exchange_ws = await get_exchange_instance(ex_id, authenticated=False, is_rest=False)
        if exchange_ws:
            global_exchanges_instances[ex_id] = exchange_ws
            try:
                await exchange_ws.load_markets()
                logger.info(f"Mercados carregados para {ex_id}")
            except Exception as e:
                logger.error(f"Erro carregar mercados {ex_id}: {e}")

    await update_all_balances()

    ws_tasks = []
    for ex_id, exchange_ws in global_exchanges_instances.items():
        for pair in PAIRS:
            if pair in exchange_ws.markets:
                ws_tasks.append(asyncio.create_task(watch_order_book_for_pair(exchange_ws, pair, ex_id)))
    logger.info("Tarefas websocket iniciadas.")

    arb_task = asyncio.create_task(check_arbitrage_opportunities(application))

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    await asyncio.gather(*ws_tasks, arb_task)


if __name__ == "__main__":
    asyncio.run(main())
