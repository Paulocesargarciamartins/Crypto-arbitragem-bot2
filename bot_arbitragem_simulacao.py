import os
import asyncio
import logging
import nest_asyncio
from decimal import Decimal
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import ccxt.pro as ccxt_pro
import ccxt.async_support as ccxt_async
from dotenv import load_dotenv

load_dotenv()
nest_asyncio.apply()

# --- Configurações e variáveis de ambiente ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

DEFAULT_LUCRO_MINIMO_PORCENTAGEM = float(os.getenv("DEFAULT_LUCRO_MINIMO_PORCENTAGEM", 2.0))
DEFAULT_FEE_PERCENTAGE = float(os.getenv("DEFAULT_FEE_PERCENTAGE", 0.1))
DEFAULT_TOTAL_CAPITAL = float(os.getenv("DEFAULT_TOTAL_CAPITAL", 500.0))
DEFAULT_TRADE_PERCENTAGE = float(os.getenv("DEFAULT_TRADE_PERCENTAGE", 10.0))
DRY_RUN_MODE = os.getenv("DRY_RUN_MODE", "True").lower() == "true"

EXCHANGE_CREDENTIALS = {
    "binance": {
        "apiKey": os.getenv("BINANCE_API_KEY"),
        "secret": os.getenv("BINANCE_SECRET"),
    },
    "kraken": {
        "apiKey": os.getenv("KRAKEN_API_KEY"),
        "secret": os.getenv("KRAKEN_SECRET"),
    },
    # Adicione outras exchanges aqui
}

EXCHANGES_LIST = list(EXCHANGE_CREDENTIALS.keys())

PAIRS_PRIORITARIOS = [
    "BTC/USDT",
    "ETH/USDT",
    "BNB/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "ADA/USDT",
    "DOGE/USDT",
    "DOT/USDT",
    "MATIC/USDT",
    "LTC/USDT",
]

PAIRS_SECUNDARIOS = [
    # Outros pares para rodízio e monitoramento gradual
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --- Variáveis globais ---
global_exchanges = {}
global_market_data = {}  # {pair: {exchange: {'bid': , 'ask': , volumes}}}
global_balances = {}  # {exchange: {'USDT': float, ...}}
global_active_trades = {}
global_stuck_positions = {}
last_alert_times = {}
COOLDOWN_SECONDS = 300

# --- Funções principais ---

async def get_exchange_instance(ex_id, authenticated=False, is_rest=False):
    config = {"enableRateLimit": True, "timeout": 10000}
    if authenticated:
        creds = EXCHANGE_CREDENTIALS.get(ex_id)
        if creds:
            config.update(creds)
        else:
            logger.warning(f"Credenciais não encontradas para {ex_id}")
    try:
        cls = ccxt_pro if not is_rest else ccxt_async
        exchange_class = getattr(cls, ex_id)
        return exchange_class(config)
    except Exception as e:
        logger.error(f"Erro criando instância {ex_id}: {e}")
        return None

async def watch_order_book(exchange, pair, ex_id):
    while True:
        try:
            order_book = await exchange.watch_order_book(pair)
            bids = order_book.get("bids", [])
            asks = order_book.get("asks", [])
            best_bid = bids[0][0] if bids else 0
            best_ask = asks[0][0] if asks else float("inf")
            if pair not in global_market_data:
                global_market_data[pair] = {}
            global_market_data[pair][ex_id] = {"bid": best_bid, "ask": best_ask}
        except Exception as e:
            logger.error(f"Erro watch_order_book {pair} {ex_id}: {e}")
            await asyncio.sleep(5)

async def load_all_markets():
    tasks = []
    for ex_id in EXCHANGES_LIST:
        exchange = await get_exchange_instance(ex_id)
        if exchange:
            global_exchanges[ex_id] = exchange
            try:
                await exchange.load_markets()
                for pair in PAIRS_PRIORITARIOS:
                    if pair in exchange.markets:
                        tasks.append(asyncio.create_task(watch_order_book(exchange, pair, ex_id)))
            except Exception as e:
                logger.error(f"Erro carregar mercados {ex_id}: {e}")
        else:
            logger.error(f"Não foi possível criar exchange {ex_id}")
    await asyncio.gather(*tasks)

async def update_balances():
    for ex_id in EXCHANGES_LIST:
        exchange = await get_exchange_instance(ex_id, authenticated=True, is_rest=True)
        if exchange:
            try:
                balance = await exchange.fetch_balance()
                global_balances[ex_id] = balance["free"]
            except Exception as e:
                logger.error(f"Erro fetch_balance {ex_id}: {e}")

async def execute_trade(action, ex_id, pair, amount):
    if DRY_RUN_MODE:
        logger.info(f"[DRY_RUN] {action} {amount} {pair} on {ex_id}")
        return None
    exchange = await get_exchange_instance(ex_id, authenticated=True, is_rest=True)
    if not exchange:
        return None
    try:
        if action == "buy":
            order = await exchange.create_order(pair, "market", "buy", amount)
        elif action == "sell":
            order = await exchange.create_order(pair, "market", "sell", amount)
        logger.info(f"Trade executada: {order}")
        return order
    except Exception as e:
        logger.error(f"Erro executar trade {action} {ex_id} {pair}: {e}")
        return None

async def analyze_and_trade(application):
    while True:
        try:
            lucro_min = float(application.bot_data.get("lucro_minimo", DEFAULT_LUCRO_MINIMO_PORCENTAGEM))
            fee_pct = float(application.bot_data.get("fee_pct", DEFAULT_FEE_PERCENTAGE))
            trade_pct = float(application.bot_data.get("trade_pct", DEFAULT_TRADE_PERCENTAGE))
            capital = DEFAULT_TOTAL_CAPITAL

            for pair, data in global_market_data.items():
                if len(data) < 2:
                    continue
                best_buy = min(data.items(), key=lambda x: x[1]["ask"])
                best_sell = max(data.items(), key=lambda x: x[1]["bid"])
                buy_ex, buy_data = best_buy
                sell_ex, sell_data = best_sell

                if buy_ex == sell_ex:
                    continue

                gross_profit = (sell_data["bid"] - buy_data["ask"]) / buy_data["ask"]
                net_profit = gross_profit - 2 * (fee_pct / 100)

                if net_profit * 100 >= lucro_min:
                    amount_usdt = capital * (trade_pct / 100)
                    buy_balance = global_balances.get(buy_ex, {}).get("USDT", 0)
                    sell_balance = global_balances.get(sell_ex, {}).get("USDT", 0)

                    if buy_balance < amount_usdt:
                        logger.info(f"Saldo insuficiente em {buy_ex} para comprar {pair}")
                        continue

                    # Aqui pode converter amount_usdt para quantidade base do par (simplificado)
                    amount_base = amount_usdt / buy_data["ask"]

                    # Executar trades
                    await execute_trade("buy", buy_ex, pair, amount_base)
                    await execute_trade("sell", sell_ex, pair, amount_base)

                    # Logar e enviar alerta Telegram
                    chat_id = application.bot_data.get("admin_chat_id")
                    if chat_id:
                        msg = (
                            f"Arbitragem executada: {pair}\n"
                            f"Compra em {buy_ex} a {buy_data['ask']}\n"
                            f"Venda em {sell_ex} a {sell_data['bid']}\n"
                            f"Lucro estimado: {net_profit*100:.2f}%"
                        )
                        await application.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            logger.error(f"Erro analisar/trade: {e}")
        await asyncio.sleep(3)  # intervalo curto para maior agilidade

# --- Telegram Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot de arbitragem iniciado!")

async def report_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = f"Pares monitorados: {len(global_market_data)}\n"
    stats += f"Exchanges conectadas: {len(global_exchanges)}\n"
    stats += f"Trades ativos: {len(global_active_trades)}"
    await update.message.reply_text(stats)

async def bug_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    logger.info(f"Bug reportado: {text}")
    await update.message.reply_text("Bug reportado. Obrigado!")

async def set_lucro_minimo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        valor = float(context.args[0])
        context.application.bot_data["lucro_minimo"] = valor
        await update.message.reply_text(f"Lucro mínimo ajustado para {valor}%")
    except Exception:
        await update.message.reply_text("Uso correto: /setlucrominimo 2.5")

async def set_trade_pct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        valor = float(context.args[0])
        context.application.bot_data["trade_pct"] = valor
        await update.message.reply_text(f"Trade percentual ajustado para {valor}% do capital")
    except Exception:
        await update.message.reply_text("Uso correto: /settradepct 10")

# --- Função principal ---

async def main():
    application = ApplicationBuilder().token(TOKEN).build()

    application.bot_data["lucro_minimo"] = DEFAULT_LUCRO_MINIMO_PORCENTAGEM
    application.bot_data["fee_pct"] = DEFAULT_FEE_PERCENTAGE
    application.bot_data["trade_pct"] = DEFAULT_TRADE_PERCENTAGE
    # coloque o seu chat_id aqui (de admin)
    application.bot_data["admin_chat_id"] = int(os.getenv("ADMIN_CHAT_ID", 0))

    # Comandos Telegram
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", report_stats))
    application.add_handler(CommandHandler("bug", bug_report))
    application.add_handler(CommandHandler("setlucrominimo", set_lucro_minimo))
    application.add_handler(CommandHandler("settradepct", set_trade_pct))

    # Inicializar mercados e monitoramento
    await load_all_markets()

    # Atualizar saldos periodicamente
    asyncio.create_task(update_balances())

    # Rodar análise e trades
    asyncio.create_task(analyze_and_trade(application))

    # Rodar Telegram bot
    await application.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
