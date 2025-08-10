# CryptoAlertsBot2 - arquivo único corrigido
import os
import asyncio
import logging
import time
from decimal import Decimal

import nest_asyncio
nest_asyncio.apply()

# ccxt imports: streaming (ccxt.pro) e REST async (ccxt.async_support)
try:
    import ccxt.pro as ccxt_pro
except Exception:
    ccxt_pro = None
try:
    import ccxt.async_support as ccxt_rest
except Exception:
    raise RuntimeError("Instale ccxt.async_support (ccxt) para REST assíncrono")

# telegram async
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# ------------------ Config (usa as ENVs que você já tem configuradas) ----------
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

# logging
logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO)
logger = logging.getLogger("CryptoAlertsBot2")

# Global state
GLOBAL_MARKET_DATA = {pair: {} for pair in PAIRS}
MARKET_LOCK = asyncio.Lock()
global_exchanges_instances = {}
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

# ---------------- utilities ----------------
def choose_module_for_exchange(is_rest: bool):
    """Retorna o módulo correto (streaming vs rest) para instanciar exchanges."""
    if is_rest:
        return ccxt_rest
    if ccxt_pro is not None:
        return ccxt_pro
    return ccxt_rest

async def get_exchange_instance(ex_id: str, authenticated: bool = False, is_rest: bool = False):
    """Cria ou retorna instância assíncrona de exchange (stream ou rest)."""
    ex_id = ex_id.lower()
    key = f"{'rest' if is_rest else 'stream'}:{ex_id}"
    if key in global_exchanges_instances:
        inst = global_exchanges_instances[key]
        return inst
    module = choose_module_for_exchange(is_rest)
    if authenticated and not EXCHANGE_CREDENTIALS.get(ex_id):
        logger.warning(f"Sem credenciais para {ex_id} (authenticated=True).")
    config = {'enableRateLimit': True, 'timeout': 20000}
    if authenticated and EXCHANGE_CREDENTIALS.get(ex_id):
        config.update(EXCHANGE_CREDENTIALS[ex_id])
    try:
        if not hasattr(module, ex_id):
            logger.warning(f"Exchange {ex_id} não encontrada no módulo {module}.")
            return None
        cls = getattr(module, ex_id)
        inst = cls(config)
        # load markets (async)
        try:
            await inst.load_markets()
        except Exception:
            # alguns adaptadores podem já ter mercados carregados
            pass
        global_exchanges_instances[key] = inst
        return inst
    except Exception as e:
        logger.exception(f"Erro instanciando {ex_id}: {e}")
        return None

# ---------------- market update ----------------
async def safe_update_market(pair: str, ex_id: str, bid: float, ask: float, bid_vol: float = 0.0, ask_vol: float = 0.0):
    async with MARKET_LOCK:
        if pair not in GLOBAL_MARKET_DATA:
            GLOBAL_MARKET_DATA[pair] = {}
        GLOBAL_MARKET_DATA[pair][ex_id] = {
            'bid': float(bid),
            'ask': float(ask),
            'bid_vol': float(bid_vol),
            'ask_vol': float(ask_vol),
            'ts': time.time()
        }

# ---------------- watchers ----------------
async def watch_order_book_for_pair_stream(inst, pair: str, ex_id: str):
    logger.info(f"Streaming {pair} @ {ex_id}")
    try:
        while True:
            try:
                ob = await inst.watch_order_book(pair)
                bids = ob.get('bids', [])
                asks = ob.get('asks', [])
                best_bid = float(bids[0][0]) if bids else 0.0
                best_bid_vol = float(bids[0][1]) if bids else 0.0
                best_ask = float(asks[0][0]) if asks else float('inf')
                best_ask_vol = float(asks[0][1]) if asks else 0.0
                await safe_update_market(pair, ex_id, best_bid, best_ask, best_bid_vol, best_ask_vol)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"stream erro {ex_id} {pair}: {e}")
                await asyncio.sleep(1)
    finally:
        try:
            await inst.close()
        except Exception:
            pass

async def watch_order_book_for_pair_poll(ex_id: str, pair: str, interval: float = 2.0):
    inst = await get_exchange_instance(ex_id, is_rest=True, authenticated=False)
    if not inst:
        return
    logger.info(f"Polling {pair} @ {ex_id} (interval {interval}s)")
    try:
        while True:
            try:
                ob = await inst.fetch_order_book(pair)
                bids = ob.get('bids', [])
                asks = ob.get('asks', [])
                best_bid = float(bids[0][0]) if bids else 0.0
                best_bid_vol = float(bids[0][1]) if bids else 0.0
                best_ask = float(asks[0][0]) if asks else float('inf')
                best_ask_vol = float(asks[0][1]) if asks else 0.0
                await safe_update_market(pair, ex_id, best_bid, best_ask, best_bid_vol, best_ask_vol)
            except Exception as e:
                logger.debug(f"poll erro {ex_id} {pair}: {e}")
            await asyncio.sleep(interval)
    finally:
        try:
            await inst.close()
        except Exception:
            pass

async def watch_all_exchanges():
    tasks = []
    for ex_id in EXCHANGES_LIST:
        # tenta streaming (ccxt.pro) se disponível, senão polling REST
        inst_stream = None
        if ccxt_pro is not None:
            inst_stream = await get_exchange_instance(ex_id, authenticated=False, is_rest=False)
        if inst_stream:
            # criar watchers por par apenas se o par existir nos mercados
            for pair in PAIRS:
                try:
                    if pair in getattr(inst_stream, "markets", {}):
                        tasks.append(asyncio.create_task(watch_order_book_for_pair_stream(inst_stream, pair, ex_id)))
                except Exception:
                    # se markets não carregado ou pair não existir, ignorar e usar polling
                    tasks.append(asyncio.create_task(watch_order_book_for_pair_poll(ex_id, pair)))
        else:
            for pair in PAIRS:
                tasks.append(asyncio.create_task(watch_order_book_for_pair_poll(ex_id, pair)))
    # aguarda todas (vão rodar indefinidamente)
    await asyncio.gather(*tasks, return_exceptions=True)

# ---------------- balances ----------------
async def update_all_balances():
    while True:
        try:
            for ex_id in EXCHANGES_LIST:
                try:
                    inst = await get_exchange_instance(ex_id, authenticated=True, is_rest=True)
                    if not inst:
                        continue
                    bal = await inst.fetch_balance()
                    GLOBAL_BALANCES[ex_id]['USDT'] = float(bal.get('free', {}).get('USDT', 0) or bal.get('free', {}).get('USDT', 0.0))
                except Exception as e:
                    logger.debug(f"Erro atualizando saldo {ex_id}: {e}")
        except Exception as e:
            logger.exception("Erro no loop update_all_balances: %s", e)
        await asyncio.sleep(60)  # atualiza a cada minuto

# ---------------- analysis & arbitrage ----------------
async def analyze_market_data_once():
    """Atualiza GLOBAL_STATS com discrepâncias (rodar periodicamente)."""
    try:
        async with MARKET_LOCK:
            snapshot = {p: dict(GLOBAL_MARKET_DATA.get(p, {})) for p in PAIRS}
        for pair, exs in snapshot.items():
            if len(exs) < 2:
                continue
            best_buy = (None, float('inf'))
            best_sell = (None, 0.0)
            for ex_id, d in exs.items():
                ask = d.get('ask')
                bid = d.get('bid')
                if ask and ask < best_buy[1]:
                    best_buy = (ex_id, ask)
                if bid and bid > best_sell[1]:
                    best_sell = (ex_id, bid)
            if best_buy[0] and best_sell[0] and best_buy[0] != best_sell[0]:
                gross_profit_pct = ((best_sell[1] - best_buy[1]) / best_buy[1]) * 100
                GLOBAL_STATS['pair_opportunities'][pair]['count'] += 1
                GLOBAL_STATS['pair_opportunities'][pair]['total_profit'] += gross_profit_pct
                if best_buy[0] in GLOBAL_STATS['exchange_discrepancy']:
                    GLOBAL_STATS['exchange_discrepancy'][best_buy[0]]['total_diff'] += gross_profit_pct
                    GLOBAL_STATS['exchange_discrepancy'][best_buy[0]]['count'] += 1
                if best_sell[0] in GLOBAL_STATS['exchange_discrepancy']:
                    GLOBAL_STATS['exchange_discrepancy'][best_sell[0]]['total_diff'] += gross_profit_pct
                    GLOBAL_STATS['exchange_discrepancy'][best_sell[0]]['count'] += 1
    except Exception:
        logger.exception("Erro em analyze_market_data_once")

async def analyze_market_data_loop():
    while True:
        await analyze_market_data_once()
        await asyncio.sleep(60)

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
            trade_percentage = application.bot_data.get('trade_percentage', DEFAULT_TRADE_PERCENTAGE)
            trade_amount_usd = GLOBAL_TOTAL_CAPITAL_USDT * (trade_percentage / 100)
            fee = application.bot_data.get('fee_percentage', DEFAULT_FEE_PERCENTAGE) / 100.0
            best_opportunity = None
            # snapshot seguro
            async with MARKET_LOCK:
                snapshot = {p: dict(GLOBAL_MARKET_DATA.get(p, {})) for p in PAIRS}
            for pair, market_data in snapshot.items():
                if len(market_data) < 2:
                    continue
                best_buy_price = float('inf')
                best_sell_price = 0.0
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
                # confirma via REST async
                try:
                    buy_rest = await get_exchange_instance(buy_ex_id, authenticated=False, is_rest=True)
                    sell_rest = await get_exchange_instance(sell_ex_id, authenticated=False, is_rest=True)
                    if not buy_rest or not sell_rest:
                        continue
                    ticker_buy = await buy_rest.fetch_ticker(pair)
                    ticker_sell = await sell_rest.fetch_ticker(pair)
                    confirmed_buy_price = float(ticker_buy.get('ask') or ticker_buy.get('last') or 0.0)
                    confirmed_sell_price = float(ticker_sell.get('bid') or ticker_sell.get('last') or 0.0)
                except Exception as e:
                    logger.debug(f"Erro confirm REST {pair} {buy_ex_id}/{sell_ex_id}: {e}")
                    continue
                if confirmed_buy_price <= 0:
                    continue
                gross_profit = (confirmed_sell_price - confirmed_buy_price) / confirmed_buy_price
                gross_profit_pct = gross_profit * 100
                net_profit_pct = gross_profit_pct - (2 * fee * 100)
                # verifica saldo (simulação: assume saldos em GLOBAL_BALANCES)
                if GLOBAL_BALANCES.get(buy_ex_id, {}).get('USDT', 0) < trade_amount_usd + DEFAULT_MIN_USDT_BALANCE:
                    logger.debug(f"Saldo insuficiente em {buy_ex_id} para {pair}")
                    continue
                if net_profit_pct >= lucro_minimo:
                    if best_opportunity is None or net_profit_pct > best_opportunity['net_profit']:
                        best_opportunity = {
                            'pair': pair,
                            'buy_ex_id': buy_ex_id,
                            'sell_ex_id': sell_ex_id,
                            'buy_price': confirmed_buy_price,
                            'sell_price': confirmed_sell_price,
                            'net_profit': net_profit_pct,
                            'trade_amount_usd': trade_amount_usd
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
                    try:
                        await bot.send_message(chat_id=chat_id, text=msg)
                        last_alert_times[best_opportunity['pair']] = time.time()
                    except Exception as e:
                        logger.debug(f"Erro enviando Telegram: {e}")
            await asyncio.sleep(5)
        except Exception:
            logger.exception("Erro em check_arbitrage_opportunities")
            await asyncio.sleep(5)

# ---------------- Telegram commands ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data['admin_chat_id'] = update.effective_chat.id
    await update.message.reply_text("CryptoAlertsBot2 ativo! Use /setlucro <percentual> para ajustar.")

async def setlucro_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        val = float(context.args[0])
        context.application.bot_data['lucro_minimo_porcentagem'] = val
        await update.message.reply_text(f"Lucro mínimo definido para {val}%")
    except Exception:
        await update.message.reply_text("Uso: /setlucro 1.5")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "Status CryptoAlertsBot2:\n"
    for ex in EXCHANGES_LIST:
        text += f"- {ex}: saldo USDT = {GLOBAL_BALANCES.get(ex, {}).get('USDT', 0):.2f}\n"
    await update.message.reply_text(text)

# ---------------- main ----------------
async def main():
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN não configurado nas variáveis de ambiente.")
        return
    app = ApplicationBuilder().token(TOKEN).build()
    # defaults
    app.bot_data['lucro_minimo_porcentagem'] = DEFAULT_LUCRO_MINIMO_PORCENTAGEM
    app.bot_data['fee_percentage'] = DEFAULT_FEE_PERCENTAGE
    app.bot_data['trade_percentage'] = DEFAULT_TRADE_PERCENTAGE
    # handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("setlucro", setlucro_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    # tasks
    asyncio.create_task(watch_all_exchanges())
    asyncio.create_task(update_all_balances())
    asyncio.create_task(analyze_market_data_loop())
    asyncio.create_task(check_arbitrage_opportunities(app))
    # start telegram polling (é coroutine)
    await app.run_polling()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Encerrado pelo usuário")
