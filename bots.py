# -*- coding: utf-8 -*-
import os, sys, time, hmac, base64, requests, json, threading, sqlite3, asyncio, signal
from datetime import datetime, timezone
from decimal import Decimal, getcontext, ROUND_DOWN
from dotenv import load_dotenv
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# 1. CONFIG
load_dotenv()
getcontext().prec = 28
getcontext().rounding = ROUND_DOWN

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_API_SECRET = os.getenv("OKX_API_SECRET", "")
OKX_API_PASSPHRASE = os.getenv("OKX_API_PASSPHRASE", "")

API_KEYS_FUTURES = {
    'okx': {'apiKey': OKX_API_KEY, 'secret': OKX_API_SECRET, 'password': OKX_API_PASSPHRASE},
    'gateio': {'apiKey': os.getenv('GATEIO_API_KEY'), 'secret': os.getenv('GATEIO_API_SECRET')},
    'mexc': {'apiKey': os.getenv('MEXC_API_KEY'), 'secret': os.getenv('MEXC_API_SECRET')},
    'bitget': {'apiKey': os.getenv('BITGET_API_KEY'), 'secret': os.getenv('BITGET_API_SECRET'), 'password': os.getenv('BITGET_API_PASSPHRASE')},
}

try:
    import ccxt.async_support as ccxt
except ImportError:
    ccxt = None

triangular_running = True
futures_running = True
triangular_min_profit_threshold = Decimal(os.getenv("MIN_PROFIT_THRESHOLD", "0.002"))
futures_min_profit_threshold = Decimal(os.getenv("FUTURES_MIN_PROFIT_THRESHOLD", "0.3"))
triangular_simulate = False
futures_dry_run = os.getenv("FUTURES_DRY_RUN", "true").lower() in ["1", "true", "yes"]
futures_trade_limit = int(os.getenv("FUTURES_TRADE_LIMIT", "0"))
futures_trades_executed = 0

triangular_trade_amount = Decimal("1")
triangular_trade_amount_is_percentage = False
futures_trade_amount = Decimal(os.getenv("FUTURES_TRADE_AMOUNT_USDT", "10"))
futures_trade_amount_is_percentage = False

connection_errors = {}

# 2. FUNCOES AUXILIARES
async def send_telegram_message(text, chat_id=None, update: Update = None):
    final_chat_id = chat_id or (update.effective_chat.id if update else TELEGRAM_CHAT_ID)
    if not TELEGRAM_TOKEN or not final_chat_id: return
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        await bot.send_message(chat_id=final_chat_id, text=text, parse_mode="Markdown")
    except Exception as e:
        print(f"Error sending telegram message: {e}")

async def get_okx_usdt_balance():
    try:
        url = "https://www.okx.com/api/v5/account/balance?ccy=USDT"
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        message = timestamp + 'GET' + '/api/v5/account/balance?ccy=USDT'
        mac = hmac.new(bytes(OKX_API_SECRET, 'utf8'), bytes(message, 'utf-8'), 'sha256')
        sign = base64.b64encode(mac.digest())
        headers = {'OK-ACCESS-KEY': OKX_API_KEY, 'OK-ACCESS-SIGN': sign, 'OK-ACCESS-TIMESTAMP': timestamp, 'OK-ACCESS-PASSPHRASE': OKX_API_PASSPHRASE}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data['code'] == '0' and data['data']:
            for detail in data['data'][0]['details']:
                if detail['ccy'] == 'USDT':
                    return Decimal(detail.get('availBal', '0'))
        return Decimal('0')
    except Exception as e:
        print(f"Error getting OKX balance: {e}")
        return None

async def get_trade_amount(exchange_name, symbol, is_triangular):
    amount_value = triangular_trade_amount if is_triangular else futures_trade_amount
    is_percentage = triangular_trade_amount_is_percentage if is_triangular else futures_trade_amount_is_percentage
    if not is_percentage: return amount_value
    try:
        available_usdt = Decimal('0')
        if is_triangular:
            balance = await get_okx_usdt_balance()
            if balance is not None: available_usdt = balance
        elif ccxt and exchange_name in active_futures_exchanges:
            ex = active_futures_exchanges[exchange_name]
            balance_data = await ex.fetch_balance()
            available_usdt = Decimal(balance_data.get('free', {}).get('USDT', 0))
        if available_usdt <= 0: raise ValueError("USDT balance is zero")
        return available_usdt * (amount_value / 100)
    except Exception as e:
        await send_telegram_message(f"⚠️ *Erro ao calcular volume:* `{e}`. Usando valor padrao.")
        return amount_value if not is_percentage else Decimal('1')

# 3. ARBITRAGEM TRIANGULAR
TRIANGULAR_DB_FILE = "/tmp/historico_triangular.db"
TRIANGULAR_FEE_RATE = Decimal("0.001")
triangular_monitored_cycles_count = 0
def init_triangular_db():
    with sqlite3.connect(TRIANGULAR_DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute("CREATE TABLE IF NOT EXISTS ciclos (timestamp TEXT, pares TEXT, lucro_percent REAL, lucro_usdt REAL, modo TEXT, status TEXT, detalhes TEXT)")
        conn.commit()
def get_all_okx_spot_instruments():
    r = requests.get("https://www.okx.com/api/v5/public/instruments?instType=SPOT", timeout=10)
    r.raise_for_status()
    return r.json().get("data", [])
def build_dynamic_cycles(instruments):
    main_currencies, pairs_by_quote, cycles = {'BTC', 'ETH', 'USDC', 'OKB'}, {}, []
    for inst in instruments:
        quote_ccy = inst.get('quoteCcy')
        if quote_ccy not in pairs_by_quote: pairs_by_quote[quote_ccy] = []
        pairs_by_quote[quote_ccy].append(inst)
    if 'USDT' in pairs_by_quote:
        for pair1 in pairs_by_quote['USDT']:
            base1 = pair1['baseCcy']
            for pivot in main_currencies:
                if base1 == pivot: continue
                for pair2 in pairs_by_quote.get(pivot, []):
                    if pair2['baseCcy'] == base1:
                        cycles.append([(f"{base1}-USDT", "buy"), (f"{base1}-{pivot}", "sell"), (f"{pivot}-USDT", "sell")])
    return cycles
def get_okx_spot_tickers(inst_ids):
    tickers = {}
    chunks = [inst_ids[i:i + 100] for i in range(0, len(inst_ids), 100)]
    for chunk in chunks:
        r = requests.get(f"https://www.okx.com/api/v5/market/tickers?instType=SPOT&instId={','.join(chunk)}", timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
        for d in data:
            if d.get("bidPx") and d.get("askPx"): tickers[d["instId"]] = {"bid": Decimal(d["bidPx"]), "ask": Decimal(d["askPx"])}
    return tickers
async def simulate_triangular_cycle(cycle, tickers):
    amt = await get_trade_amount('okx', 'N/A', is_triangular=True)
    if amt <= 0: return Decimal("0"), Decimal("0")
    start_amt, current_amt = amt, amt
    for instId, action in cycle:
        ticker = tickers.get(instId)
        if not ticker: raise RuntimeError(f"Ticker for {instId} not found.")
        price = ticker["ask"] if action == "buy" else ticker["bid"]
        fee = current_amt * TRIANGULAR_FEE_RATE
        if action == "buy": current_amt = (current_amt - fee) / price
        else: current_amt = (current_amt * price) - fee
    profit_abs = current_amt - start_amt
    profit_pct = profit_abs / start_amt if start_amt > 0 else 0
    return profit_pct, profit_abs
async def loop_bot_triangular():
    global triangular_monitored_cycles_count
    print("Starting Triangular Arbitrage Bot...")
    try:
        dynamic_cycles = build_dynamic_cycles(get_all_okx_spot_instruments())
        triangular_monitored_cycles_count = len(dynamic_cycles)
    except Exception as e:
        await send_telegram_message(f"❌ *Erro Critico Triangular:* Falha ao construir ciclos. `{e}`")
        return
    while True:
        if not triangular_running: await asyncio.sleep(30); continue
        try:
            all_tickers = get_okx_spot_tickers(list({i for c in dynamic_cycles for i, _ in c}))
            for cycle in dynamic_cycles:
                try:
                    profit_pct, profit_abs = await simulate_triangular_cycle(cycle, all_tickers)
                    if profit_pct > triangular_min_profit_threshold:
                        pares_fmt = " → ".join([p for p, a in cycle])
                        saldo_atual = await get_okx_usdt_balance()
                        saldo_txt = f"`{saldo_atual:.2f} USDT`" if saldo_atual is not None else "`N/A`"
                        msg = (f"✅ *Arbitragem Triangular*\n`{pares_fmt}`\n"
                               f"Lucro: `{profit_pct:.3%}` (~`{profit_abs:.4f} USDT`)\nSaldo OKX: {saldo_txt}")
                        await send_telegram_message(msg)
                except Exception: pass
        except Exception as e:
            await send_telegram_message(f"⚠️ *Erro no Loop Triangular:* `{e}`")
        await asyncio.sleep(20)

# 4. ARBITRAGEM FUTUROS
active_futures_exchanges = {}
FUTURES_TARGET_PAIRS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'ADA/USDT:USDT', 'AVAX/USDT:USDT', 'LINK/USDT:USDT', 'DOT/USDT:USDT', 'MATIC/USDT:USDT', 'BNB/USDT:USDT', 'TRX/USDT:USDT', 'LTC/USDT:USDT', 'NEAR/USDT:USDT', 'OP/USDT:USDT', 'ARB/USDT:USDT', 'APT/USDT:USDT', 'SUI/USDT:USDT', 'PEPE/USDT:USDT', 'WLD/USDT:USDT']
async def initialize_futures_exchanges():
    global active_futures_exchanges, connection_errors
    if not ccxt: return
    for name, creds in API_KEYS_FUTURES.items():
        if not creds or not creds.get('apiKey'): continue
        instance = None
        try:
            exchange_class = getattr(ccxt, name)
            instance = exchange_class({**creds, 'options': {'defaultType': 'swap'}})
            await instance.load_markets()
            active_futures_exchanges[name] = instance
            if name in connection_errors:
                await send_telegram_message(f"✅ *Conexao Restaurada:* `{name}`.")
                del connection_errors[name]
        except Exception as e:
            error_msg = f"{e}"
            if name not in connection_errors or connection_errors[name] != error_msg:
                await send_telegram_message(f"❌ *Erro de Conexao:* `{name}`: `{error_msg}`")
                connection_errors[name] = error_msg
            if instance: await instance.close()
async def find_futures_opportunities():
    global connection_errors
    tasks = {name: ex.fetch_tickers(FUTURES_TARGET_PAIRS) for name, ex in active_futures_exchanges.items()}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    prices_by_symbol = {}
    for (name, _), res in zip(tasks.items(), results):
        if isinstance(res, Exception):
            error_msg = f"{res}"
            if name not in connection_errors or connection_errors[name] != error_msg:
                await send_telegram_message(f"❌ *Erro de Conexao:* `{name}`: `{error_msg}`")
                connection_errors[name] = error_msg
            elif name in connection_errors: print(f"Connection error with {name} persists.")
            continue
        if name in connection_errors:
            await send_telegram_message(f"✅ *Erro Corrigido:* `{name}`.")
            del connection_errors[name]
        for symbol, ticker in res.items():
            if symbol not in prices_by_symbol: prices_by_symbol[symbol] = []
            if ticker.get('bid') and ticker.get('ask'):
                prices_by_symbol[symbol].append({'exchange': name, 'bid': Decimal(ticker['bid']), 'ask': Decimal(ticker['ask'])})
    opportunities = []
    for symbol, prices in prices_by_symbol.items():
        if len(prices) < 2: continue
        best_ask = min(prices, key=lambda x: x['ask'])
        best_bid = max(prices, key=lambda x: x['bid'])
        if best_ask['exchange'] != best_bid['exchange']:
            profit_pct = ((best_bid['bid'] - best_ask['ask']) / best_ask['ask']) * 100
            if profit_pct > futures_min_profit_threshold:
                opportunities.append({'symbol': symbol, 'buy_exchange': best_ask['exchange'], 'buy_price': best_ask['ask'], 'sell_exchange': best_bid['exchange'], 'sell_price': best_bid['bid'], 'profit_percent': profit_pct})
    return sorted(opportunities, key=lambda x: x['profit_percent'], reverse=True)
async def loop_bot_futures():
    global futures_running, futures_trades_executed, futures_trade_limit
    if not ccxt: return
    await initialize_futures_exchanges()
    if not active_futures_exchanges:
        await send_telegram_message("⚠️ *Bot Futuros nao iniciado:* Nenhuma chave de API valida.")
        return
    await send_telegram_message(f"✅ *Bot Futuros iniciado.* Exchanges: `{', '.join(active_futures_exchanges.keys())}`")
    while True:
        if not futures_running: await asyncio.sleep(30); continue
        if futures_trade_limit > 0 and futures_trades_executed >= futures_trade_limit:
            futures_running = False
            await send_telegram_message(f"🛑 *Limite de trades atingido:* Bot futuros desativado.")
            continue
        opportunities = await find_futures_opportunities()
        if opportunities:
            opp = opportunities[0]
            trade_amount_usd = await get_trade_amount(opp['buy_exchange'], opp['symbol'], is_triangular=False)
            if futures_dry_run:
                msg = (f"💸 *Oportunidade Futuros (Simulada)*\n`{opp['symbol']}`\n"
                       f"Comprar em: `{opp['buy_exchange'].upper()}` | Vender em: `{opp['sell_exchange'].upper()}`\n"
                       f"Lucro: *`{opp['profit_percent']:.3f}%`* | Volume: `{trade_amount_usd:.2f}` USDT")
                await send_telegram_message(msg)
                futures_trades_executed += 1
            else: futures_trades_executed += 1; pass
        await asyncio.sleep(90)

# 5. TELEGRAM COMMANDS
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("Bot online. Use /ajuda.")
async def ajuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ajuda_text = ("*Comandos:*\n`/status`\n`/saldos`\n`/setlucro <T> <F>`\n"
                  "`/setvolume <T> <F>` (use %)\n`/setlimite <N>`\n"
                  "`/setalavancagem <ex> <par> <val>`\n`/ligar <bot>`\n`/desligar <bot>`")
    await update.message.reply_text(ajuda_text, parse_mode="Markdown")
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def get_vol_txt(is_tri):
        amt = triangular_trade_amount if is_tri else futures_trade_amount
        is_p = triangular_trade_amount_is_percentage if is_tri else futures_trade_amount_is_percentage
        return f"`{amt}%` do saldo" if is_p else f"`{amt}` USDT"
    status = (f"📊 *Status*\n\n*Triangular:*\nStatus: `{'ON' if triangular_running else 'OFF'}`\n"
              f"Lucro Min: `{triangular_min_profit_threshold:.3%}` | Volume: {get_vol_txt(True)}\n\n"
              f"*Futuros:*\nStatus: `{'ON' if futures_running else 'OFF'}`\n"
              f"Lucro Min: `{futures_min_profit_threshold:.2f}%` | Volume: {get_vol_txt(False)}\n"
              f"Trades: `{futures_trades_executed}/{'inf' if futures_trade_limit == 0 else futures_trade_limit}`\n"
              f"Exchanges: `{', '.join(active_futures_exchanges.keys())}`\n"
              f"Erros: `{', '.join(connection_errors.keys()) if connection_errors else 'Nenhum'}`")
    await update.message.reply_text(status, parse_mode="Markdown")
async def saldos_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ccxt or not active_futures_exchanges: await update.message.reply_text("Nenhuma exchange conectada."); return
    txt = "💰 *Saldos (USDT)*\n\n"
    for name, ex in active_futures_exchanges.items():
        try:
            bal = await ex.fetch_balance()
            total = Decimal(bal.get('total', {}).get('USDT', 0))
            free = Decimal(bal.get('free', {}).get('USDT', 0))
            txt += f"*{name.upper()}*: Total: `{total:.2f}` | Disp: `{free:.2f}`\n"
        except Exception as e: txt += f"*{name.upper()}*: Erro ao carregar. `{e}`\n"
    await update.message.reply_text(txt, parse_mode="Markdown")
async def setlucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global triangular_min_profit_threshold, futures_min_profit_threshold
    try:
        args = context.args
        if len(args) != 2: await update.message.reply_text("Uso: /setlucro <triangular> <futuros>"); return
        triangular_min_profit_threshold, futures_min_profit_threshold = Decimal(args[0]), Decimal(args[1])
        await update.message.reply_text(f"Lucro min atualizado: T `{triangular_min_profit_threshold:.3%}` | F `{futures_min_profit_threshold:.2f}%`")
    except: await update.message.reply_text("Valores invalidos.")
async def setvolume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global triangular_trade_amount, triangular_trade_amount_is_percentage, futures_trade_amount, futures_trade_amount_is_percentage
    try:
        args = context.args
        if len(args) != 2: await update.message.reply_text("Uso: /setvolume <T> <F>"); return
        def parse_vol(arg_str): return (Decimal(arg_str[:-1]), True) if arg_str.endswith('%') else (Decimal(arg_str), False)
        triangular_trade_amount, triangular_trade_amount_is_percentage = parse_vol(args[0])
        futures_trade_amount, futures_trade_amount_is_percentage = parse_vol(args[1])
        tri_txt = f"`{triangular_trade_amount}%`" if triangular_trade_amount_is_percentage else f"`{triangular_trade_amount}` USDT"
        fut_txt = f"`{futures_trade_amount}%`" if futures_trade_amount_is_percentage else f"`{futures_trade_amount}` USDT"
        await update.message.reply_text(f"Volume atualizado:\nTriangular: {tri_txt}\nFuturos: {fut_txt}")
    except: await update.message.reply_text("Valores invalidos.")
async def setlimite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global futures_trade_limit, futures_trades_executed
    try:
        if not context.args: await update.message.reply_text(f"Limite atual: {'Ilimitado' if futures_trade_limit == 0 else futures_trade_limit}. Uso: /setlimite <num>"); return
        limit = int(context.args[0])
        if limit < 0: await update.message.reply_text("Limite deve ser >= 0."); return
        futures_trade_limit, futures_trades_executed = limit, 0
        await update.message.reply_text(f"Limite de trades para futuros: {'Ilimitado' if limit == 0 else limit}. Contador resetado.")
    except: await update.message.reply_text("Valor invalido.")
async def setalavancagem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ccxt: await update.message.reply_text("Modulo 'ccxt' nao disponivel."); return
    try:
        args = context.args
        if len(args) != 3: await update.message.reply_text("Uso: /setalavancagem <ex> <par> <valor>"); return
        ex_name, symbol, lev_str = args[0].lower(), args[1], args[2]
        if ex_name not in active_futures_exchanges: await update.message.reply_text(f"Exchange `{ex_name}` invalida."); return
        exchange, leverage = active_futures_exchanges[ex_name], int(lev_str)
        await update.message.reply_text(f"Tentando definir alavancagem de `{symbol}` para `{leverage}x` em `{ex_name.upper()}`...")
        try:
            await exchange.set_leverage(leverage, symbol, params={'mgnMode': 'cross'})
            await update.message.reply_text(f"✅ Alavancagem de `{symbol}` em `{ex_name.upper()}` definida para `{leverage}x`!")
        except Exception as e: await update.message.reply_text(f"❌ Falha ao definir alavancagem: `{e}`")
    except: await update.message.reply_text("Argumentos invalidos.")
async def ligar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global triangular_running, futures_running
    try:
        bot_name = context.args[0].lower()
        if bot_name == 'triangular': triangular_running = True; await update.message.reply_text("✅ Bot triangular ATIVADO.")
        elif bot_name == 'futuros': futures_running = True; await update.message.reply_text("✅ Bot de futuros ATIVADO.")
        else: await update.message.reply_text("Invalido. Use 'triangular' ou 'futuros'.")
    except: await update.message.reply_text("Uso: /ligar <bot>")
async def desligar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global triangular_running, futures_running
    try:
        bot_name = context.args[0].lower()
        if bot_name == 'triangular': triangular_running = False; await update.message.reply_text("🛑 Bot triangular DESATIVADO.")
        elif bot_name == 'futuros': futures_running = False; await update.message.reply_text("🛑 Bot de futuros DESATIVADO.")
        else: await update.message.reply_text("Invalido. Use 'triangular' ou 'futuros'.")
    except: await update.message.reply_text("Uso: /desligar <bot>")
async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE): await update.message.reply_text("Comando desconhecido. Use /ajuda.")

# 6. INICIALIZACAO
async def main():
    print("Iniciando bot...")
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    handlers = [
        CommandHandler("start", start_command), CommandHandler("ajuda", ajuda_command),
        CommandHandler("status", status_command), CommandHandler("saldos", saldos_command),
        CommandHandler("setlucro", setlucro_command), CommandHandler("setvolume", setvolume_command),
        CommandHandler("setlimite", setlimite_command), CommandHandler("setalavancagem", setalavancagem_command),
        CommandHandler("ligar", ligar_command), CommandHandler("desligar", desligar_command),
        MessageHandler(filters.COMMAND, unknown_command)
    ]
    for handler in handlers: application.add_handler(handler)
    init_triangular_db()
    asyncio.create_task(loop_bot_triangular())
    if ccxt: asyncio.create_task(loop_bot_futures())
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID: await send_telegram_message("✅ *Bot online!*")
    print("Bot do Telegram rodando...")
    await application.run_polling()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nBot encerrado.")
    finally:
        print("Finalizando tarefas...")
        tasks = asyncio.all_tasks(loop=loop)
        for task in tasks: task.cancel()
        async def gather_cancelled_tasks(): await asyncio.gather(*tasks, return_exceptions=True)
        loop.run_until_complete(gather_cancelled_tasks())
        loop.close()
