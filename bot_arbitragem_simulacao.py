import os
import asyncio
import logging
from telegram import Update, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import ccxt.pro as ccxt
import nest_asyncio
import time
import random

# Aplica o patch para permitir loops aninhados
nest_asyncio.apply()

# --- Configurações básicas ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# LINHA ADICIONADA: Checa o valor do token
print(f"Valor do TOKEN lido do Heroku: {TOKEN}")

# --- Módulos do Bot (baseado na nossa conversa) ---
class ExchangeManager:
    def __init__(self, dry_run=True):
        self.exchanges = {}
        self.dry_run = dry_run
        logging.info("ExchangeManager iniciado. Conexões simuladas.")

    def get_exchange(self, exchange_id):
        if self.dry_run:
            return {'id': exchange_id}
        return self.exchanges.get(exchange_id)

    def check_balance(self, exchange_id, currency='USDT'):
        if self.dry_run:
            return 1000.0
        return 0.0

class TradingManager:
    def __init__(self, exchange_manager, dry_run=True):
        self.exchange_manager = exchange_manager
        self.dry_run = dry_run
        logging.info(f"TradingManager iniciado. Dry Run: {self.dry_run}")

    async def execute_market_buy_order(self, exchange_id, pair, amount_usdt):
        if self.dry_run:
            logger.info(f"[DRY RUN] SIMULANDO COMPRA: {amount_usdt:.2f} USDT de {pair} em {exchange_id}.")
            return {'id': 'dry_run_buy_id', 'amount': amount_usdt * 0.1, 'price': 10}

        # Lógica real de compra com ccxt. (Desativada para o modo de simulação)

        return None

    async def execute_market_sell_order(self, exchange_id, pair, amount_coin):
        if self.dry_run:
            logger.info(f"[DRY RUN] SIMULANDO VENDA: {amount_coin:.8f} {pair.split('/')[0]} em {exchange_id}.")
            return {'id': 'dry_run_sell_id', 'amount': amount_coin, 'price': 10.5}

        # Lógica real de venda com ccxt. (Desativada para o modo de simulação)

        return None

# --- Configurações do Bot de Arbitragem ---
DEFAULT_LUCRO_MINIMO_PORCENTAGEM = 2.0
DEFAULT_TRADE_AMOUNT_USD = 50.0
DEFAULT_FEE_PERCENTAGE = 0.1
DRY_RUN_MODE = True # MODO DE SIMULAÇÃO ATIVO

EXCHANGES_LIST = [
    'binance', 'coinbase', 'kraken', 'okx', 'bybit',
    'kucoin', 'bitstamp', 'bitget',
]

# Pares USDT - OTIMIZADA para o plano profissional (100 principais moedas)
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
last_alert_times = {}
COOLDOWN_SECONDS = 300

exchange_manager = ExchangeManager(dry_run=DRY_RUN_MODE)
trading_manager = TradingManager(exchange_manager, dry_run=DRY_RUN_MODE)

async def check_arbitrage_opportunities(application):
    bot = application.bot
    while True:
        try:
            chat_id = application.bot_data.get('admin_chat_id')
            if not chat_id:
                logger.warning("Nenhum chat_id de administrador definido.")
                await asyncio.sleep(5)
                continue

            lucro_minimo = application.bot_data.get('lucro_minimo_porcentagem', DEFAULT_LUCRO_MINIMO_PORCENTAGEM)
            trade_amount_usd = application.bot_data.get('trade_amount_usd', DEFAULT_TRADE_AMOUNT_USD)
            fee = application.bot_data.get('fee_percentage', DEFAULT_FEE_PERCENTAGE) / 100.0

            # --- Lógica de Simulação de Oportunidade ---
            # Em dry_run, geramos dados aleatórios para testar o fluxo de execução.
            # Na versão real, essa lógica seria mais complexa, usando o GLOBAL_MARKET_DATA.
            buy_ex_id = random.choice(EXCHANGES_LIST)
            sell_ex_id = random.choice([ex for ex in EXCHANGES_LIST if ex != buy_ex_id])
            pair = random.choice(PAIRS)

            best_buy_price = random.uniform(10, 20)
            best_sell_price = best_buy_price * (1 + random.uniform(0.01, 0.05)) # Garante um lucro potencial

            gross_profit_percentage = ((best_sell_price - best_buy_price) / best_buy_price) * 100
            net_profit_percentage = gross_profit_percentage - (2 * fee * 100)

            if net_profit_percentage >= lucro_minimo:
                arbitrage_key = f"{pair}-{buy_ex_id}-{sell_ex_id}"
                current_time = time.time()

                if arbitrage_key in last_alert_times and (current_time - last_alert_times[arbitrage_key]) < COOLDOWN_SECONDS:
                    logger.debug(f"Alerta para {arbitrage_key} em cooldown.")
                    continue

                msg = (f"✅ Oportunidade confirmada e EXECUTADA (DRY RUN)!\n"
                    f"💰 Arbitragem para {pair}!\n"
                    f"Compre em {buy_ex_id}: {best_buy_price:.8f}\n"
                    f"Venda em {sell_ex_id}: {best_sell_price:.8f}\n"
                    f"Lucro Líquido: {net_profit_percentage:.2f}%\n"
                )

                # Chama as funções de trading simuladas
                buy_order = await trading_manager.execute_market_buy_order(buy_ex_id, pair, trade_amount_usd)
                if buy_order:
                    coin_amount = buy_order['amount']
                    await trading_manager.execute_market_sell_order(sell_ex_id, pair, coin_amount)

                    await bot.send_message(chat_id=chat_id, text=msg)
                    last_alert_times[arbitrage_key] = current_time

        except Exception as e:
            logger.error(f"Erro no loop de arbitragem: {e}", exc_info=True)

        await asyncio.sleep(5)

async def watch_order_book_for_pair(exchange, pair, ex_id):
    """Função que atualiza os dados de mercado. Simula em dry_run."""
    try:
        while True:
            if DRY_RUN_MODE:
                best_bid = random.uniform(10, 20)
                best_ask = best_bid * (1 + random.uniform(0.005, 0.02))
                best_bid_volume = random.uniform(500, 1000)
                best_ask_volume = random.uniform(500, 1000)
            else:
                order_book = await exchange.watch_order_book(pair)
                best_bid = order_book['bids'][0][0] if order_book['bids'] else 0
                best_bid_volume = order_book['bids'][0][1] if order_book['bids'] else 0
                best_ask = order_book['asks'][0][0] if order_book['asks'] else float('inf')
                best_ask_volume = order_book['asks'][0][1] if order_book['asks'] else 0

            GLOBAL_MARKET_DATA[pair][ex_id] = {
                'bid': best_bid,
                'bid_volume': best_bid_volume,
                'ask': best_ask,
                'ask_volume': best_ask_volume
            }
            await asyncio.sleep(1)
    except Exception as e:
        logger.error(f"Erro inesperado no WebSocket para {pair} em {ex_id}: {e}")
    finally:
        if not DRY_RUN_MODE: await exchange.close()

async def watch_all_exchanges():
    tasks = []
    for ex_id in EXCHANGES_LIST:
        if DRY_RUN_MODE:
            markets_loaded[ex_id] = True
            for pair in PAIRS:
                tasks.append(asyncio.create_task(
                    watch_order_book_for_pair(None, pair, ex_id)
                ))
        else:
            exchange_class = getattr(ccxt, ex_id)
            exchange = exchange_class({'enableRateLimit': True, 'timeout': 10000,})
            global_exchanges_instances[ex_id] = exchange

            try:
                await exchange.load_markets()
                markets_loaded[ex_id] = True
                for pair in PAIRS:
                    if pair in exchange.markets:
                        tasks.append(asyncio.create_task(
                            watch_order_book_for_pair(exchange, pair, ex_id)
                        ))
            except Exception as e:
                logger.error(f"ERRO ao carregar mercados de {ex_id}: {e}")

    logger.info("Iniciando WebSockets para todas as exchanges e pares válidos...")
    await asyncio.gather(*tasks, return_exceptions=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['admin_chat_id'] = update.message.chat_id
    await update.message.reply_text(
        "Olá! Bot de Arbitragem Ativado (Modo de Simulação).\n"
        "Estou monitorando oportunidades de arbitragem e simulando a execução.\n"
        f"Lucro mínimo atual: {context.bot_data.get('lucro_minimo_porcentagem', DEFAULT_LUCRO_MINIMO_PORCENTAGEM)}%\n"
        f"Volume de trade para simulação: ${context.bot_data.get('trade_amount_usd', DEFAULT_TRADE_AMOUNT_USD):.2f}\n"
        f"Taxa de negociação por lado: {context.bot_data.get('fee_percentage', DEFAULT_FEE_PERCENTAGE)}%\n\n"
        "Você pode ajustar as configurações com os comandos:\n"
        "/setlucro <valor>\n/setvolume <valor>\n/setfee <valor>\n\n"
        "Use /stop para parar de receber alertas."
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
        logger.info(f"Lucro mínimo definido para {valor}% por {update.message.chat_id}")
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
        logger.info(f"Volume de trade definido para ${valor} por {update.message.chat_id}")
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
        logger.info(f"Taxa de negociação definida para {valor}% por {update.message.chat_id}")
    except (IndexError, ValueError):
        await update.message.reply_text("Uso incorreto. Exemplo: /setfee 0.075")

async def stop_arbitrage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['admin_chat_id'] = None
    await update.message.reply_text("Alertas e simulações desativados. Use /start para reativar.")
    logger.info(f"Alertas e simulações desativados por {update.message.chat_id}")

async def main():
    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setlucro", setlucro))
    application.add_handler(CommandHandler("setvolume", setvolume))
    application.add_handler(CommandHandler("setfee", setfee))
    application.add_handler(CommandHandler("stop", stop_arbitrage))

    await application.bot.set_my_commands([
        BotCommand("start", "Iniciar o bot e ver configurações"),
        BotCommand("setlucro", "Definir lucro mínimo em % (Ex: /setlucro 2.5)"),
        BotCommand("setvolume", "Definir volume de trade em USD para liquidez (Ex: /setvolume 100)"),
        BotCommand("setfee", "Definir taxa de negociação por lado em % (Ex: /setfee 0.075)"),
        BotCommand("stop", "Parar de receber alertas")
    ])

    logger.info("Bot iniciado com sucesso e aguardando mensagens...")

    try:
        asyncio.create_task(watch_all_exchanges())
        asyncio.create_task(check_arbitrage_opportunities(application))

        await application.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)

    except Exception as e:
        logger.error(f"Erro no loop principal do bot: {e}", exc_info=True)
    finally:
        logger.info("Fechando conexões das exchanges...")
        if not DRY_RUN_MODE:
            tasks = [ex.close() for ex in global_exchanges_instances.values()]
            await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == "__main__":
    asyncio.run(main())
