import os
import logging
import telebot.asyncio_helper as asyncio_helper
from telebot.async_telebot import AsyncTeleBot
import ccxt.pro as ccxt
from decimal import Decimal, getcontext
import traceback
import asyncio
from datetime import datetime, timedelta

# --- Global Configuration ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
getcontext().prec = 30

# --- Environment Variables ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD")

# --- Trade Parameters ---
TAXA_TAKER = Decimal("0.001")
MOEDAS_BASE_OPERACIONAIS = ['USDT', 'USDC']
MINIMO_ABSOLUTO_DO_VOLUME = Decimal("3.1")
MIN_ROUTE_DEPTH = 3
MARGEM_DE_SEGURANCA = Decimal("0.997")
FIAT_CURRENCIES = {'USD', 'EUR', 'GBP', 'JPY', 'BRL', 'AUD', 'CAD', 'CHF', 'CNY', 'HKD', 'SGD', 'KRW', 'INR', 'RUB', 'TRY', 'UAH', 'VND', 'THB', 'PHP', 'IDR', 'MYR', 'AED', 'SAR', 'ZAR', 'MXN', 'ARS', 'CLP', 'COP', 'PEN'}
BLACKLIST_MOEDAS = {'TON', 'SUI'}
ORDER_BOOK_DEPTH = 100
API_TIMEOUT_SECONDS = 60
VERBOSE_ERROR_LOGGING = True
MAX_RECONNECT_ATTEMPTS = 5
PROBLEM_PAIRS_COOLDOWN_MINUTES = 15
STOP_LOSS_LEVEL_1_PERCENT = Decimal("-0.5")
STOP_LOSS_LEVEL_2_PERCENT = Decimal("-1.0")

# --- Log Handlers ---
class TelegramHandler(logging.Handler):
    """
    Handler de log para enviar mensagens de CRITICAL para o Telegram.
    """
    def __init__(self, bot_instance, chat_id, loop, level=logging.CRITICAL):
        super().__init__(level)
        self.bot = bot_instance
        self.chat_id = chat_id
        self.loop = loop
        self.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

    def emit(self, record):
        log_entry = self.format(record)
        try:
            asyncio.run_coroutine_threadsafe(
                self.bot.send_message(self.chat_id, f"🔴 **CRITICAL BOT ERROR!**\n\n`{log_entry}`", parse_mode="Markdown"),
                self.loop
            )
        except Exception as e:
            print(f"Failed to send log to Telegram: {e}")

# --- Bot State and Engine Instance (Global) ---
state = {
    'is_running': True,
    'dry_run': True,
    'min_profit': Decimal("0.005"),
    'volume_percent': Decimal("100.0"),
    'max_depth': 3,
    'stop_loss_usdt': None
}

engine = None
bot = None
exchange = None

# --- Command Handlers ---
async def send_welcome(message):
    await bot.reply_to(message, "Bot v35.0 (Arbitrage Bot) is online. Use /status.")

async def send_balance_command(message):
    try:
        await bot.reply_to(message, "Fetching balances from OKX...")
        balance = await exchange.fetch_balance()
        reply = "📊 **Balances (OKX):**\n"
        for moeda in MOEDAS_BASE_OPERACIONAIS:
            saldo = balance.get(moeda, {'free': 0, 'total': 0})
            saldo_livre = Decimal(str(saldo.get('free', '0')))
            saldo_total = Decimal(str(saldo.get('total', '0')))
            reply += (f"- `{moeda}`\n"
                      f"  Available for Trade: `{saldo_livre:.4f}`\n"
                      f"  Total (incl. in orders): `{saldo_total:.4f}`\n")
        
        await bot.send_message(message.chat.id, reply, parse_mode="Markdown")
    except Exception as e:
        await bot.reply_to(message, f"❌ Error fetching balances: {e}")
        logging.error(f"Error in /saldo command: {e}")

async def send_status(message):
    status_text = "Running" if state['is_running'] else "Paused"
    mode_text = "Simulation" if state['dry_run'] else "⚠️ LIVE MODE ⚠️"
    
    problematic_pairs_count = len(engine.problematic_pairs) if engine else 0
    problem_pairs_text = f"Problematic Pairs: `{problematic_pairs_count}`" if problematic_pairs_count > 0 else "No problematic pairs."

    reply = (f"Status: {status_text}\n"
             f"Mode: **{mode_text}**\n"
             f"Minimum Profit: `{state['min_profit']:.4f}%`\n"
             f"Trade Volume: `{state['volume_percent']:.2f}%`\n"
             f"Max Route Depth: `{state['max_depth']}`\n"
             f"{problem_pairs_text}")
    await bot.send_message(message.chat.id, reply, parse_mode="Markdown")

async def simple_commands(message):
    command = message.text.split('@')[0][1:]
    if command == 'pausar':
        state['is_running'] = False
        await bot.reply_to(message, "Engine paused.")
    elif command == 'retomar':
        state['is_running'] = True
        await bot.reply_to(message, "Engine resumed.")
    elif command == 'modo_real':
        state['dry_run'] = False
        await bot.reply_to(message, "⚠️ LIVE MODE ACTIVATED! ⚠️ The next opportunities will be executed.")
    elif command == 'modo_simulacao':
        state['dry_run'] = True
        await bot.reply_to(message, "Simulation mode activated.")
    logging.info(f"Command '{command}' executed.")

async def value_commands(message):
    try:
        parts = message.text.split(maxsplit=1)
        command = parts[0].split('@')[0][1:]
        value = parts[1] if len(parts) > 1 else ""

        if command == 'setlucro':
            state['min_profit'] = Decimal(value)
            await bot.reply_to(message, f"Minimum profit set to {state['min_profit']:.4f}%")
        elif command == 'setvolume':
            vol = Decimal(value)
            if 0 < vol <= 100:
                state['volume_percent'] = vol
                await bot.reply_to(message, f"Trade volume set to {state['volume_percent']:.2f}%")
            else:
                await bot.reply_to(message, "Volume must be between 1 and 100.")
        elif command == 'setdepth':
            depth = int(value)
            if MIN_ROUTE_DEPTH <= depth <= 5:
                state['max_depth'] = depth
                await bot.reply_to(message, f"Route depth set to {state['max_depth']}. The map will be rebuilt in the next cycle.")
            else:
                await bot.reply_to(message, f"Depth must be between {MIN_ROUTE_DEPTH} and 5.")
        
        logging.info(f"Command '{command} {value}' executed.")
    except Exception as e:
        await bot.reply_to(message, f"Command error. Usage: /{command} <value>")
        logging.error(f"Error processing command '{message.text}': {e}")
        
async def check_websocket_status(message):
    try:
        start_time = datetime.now()
        timeout = 60
        
        while not engine.order_books and (datetime.now() - start_time).total_seconds() < timeout:
            await asyncio.sleep(1)
        
        if not engine.order_books:
            await bot.reply_to(message, "❌ **The engine has not started monitoring order books.** Check if the bot is running and if there are valid routes.")
            return

        report = "🔍 **WebSocket Connection Status**\n"
        current_time = datetime.now()
        
        for symbol, orderbook in engine.order_books.items():
            if 'timestamp' in orderbook:
                last_update_ms = orderbook['timestamp']
                last_update_s = last_update_ms / 1000.0
                last_update_dt = datetime.fromtimestamp(last_update_s)
                time_diff_s = (current_time - last_update_dt).total_seconds()
                
                status_emoji = "✅" if time_diff_s < 10 else "⚠️"
                report += f"{status_emoji} `{symbol}` - Last update: `{time_diff_s:.2f}s` ago.\n"
            else:
                report += f"❓ `{symbol}` - No timestamp data.\n"
        
        await bot.send_message(message.chat.id, report, parse_mode="Markdown")

    except Exception as e:
        await bot.reply_to(message, f"❌ Error checking WebSocket status: {e}")
        logging.error(f"Error in /verificar_ws command: {e}")

# --- Setup function for handlers ---
def setup_handlers(bot_instance):
    """Associa as funções de handler aos comandos do bot."""
    bot_instance.message_handler(commands=['start', 'ajuda'])(send_welcome)
    bot_instance.message_handler(commands=['saldo'])(send_balance_command)
    bot_instance.message_handler(commands=['status'])(send_status)
    bot_instance.message_handler(commands=['pausar', 'retomar', 'modo_real', 'modo_simulacao'])(simple_commands)
    bot_instance.message_handler(commands=['setlucro', 'setvolume', 'setdepth'])(value_commands)
    bot_instance.message_handler(commands=['verificar_ws'])(check_websocket_status)

# --- Arbitrage Logic ---
class ArbitrageEngine:
    def __init__(self, exchange_instance, event_loop):
        self.exchange = exchange_instance
        self.markets = self.exchange.markets
        self.loop = event_loop
        self.graph = {}
        self.rotas_viaveis = []
        self.last_depth = state['max_depth']
        self.order_books = {}
        self.problematic_pairs = {}
        self.websocket_tasks = {}
        
    def construir_rotas(self,):
        logging.info("Building route map...")
        self.graph = {}
        self.rotas_viaveis = []

        active_markets = {
            s: m for s, m in self.markets.items()
            if m.get('active')
            and m.get('base') and m.get('quote')
            and m['base'] not in FIAT_CURRENCIES and m['quote'] not in FIAT_CURRENCIES
            and m['base'] not in BLACKLIST_MOEDAS and m['quote'] not in BLACKLIST_MOEDAS
        }
        tradable_markets = active_markets

        for symbol, market in tradable_markets.items():
            base, quote = market['base'], market['quote']
            if base not in self.graph: self.graph[base] = []
            if quote not in self.graph: self.graph[quote] = []
            self.graph[base].append(quote)
            self.graph[quote].append(base)

        todas_as_rotas = []
        def encontrar_ciclos_dfs(u, path, depth):
            if depth > state['max_depth']: return
            for v in self.graph.get(u, []):
                if v in MOEDAS_BASE_OPERACIONAIS and len(path) >= MIN_ROUTE_DEPTH:
                    rota = path + [v]
                    if len(set(rota)) == len(rota) - 1: todas_as_rotas.append(rota)
                elif v not in path: encontrar_ciclos_dfs(v, path + [v], depth + 1)

        for base_moeda in MOEDAS_BASE_OPERACIONAIS:
            encontrar_ciclos_dfs(base_moeda, [base_moeda], 1)

        self.rotas_viaveis = [tuple(rota) for rota in todas_as_rotas]
        self.last_depth = state['max_depth']
        logging.info(f"Route map rebuilt for depth {self.last_depth}. {len(self.rotas_viaveis)} routes found.")
        asyncio.create_task(bot.send_message(CHAT_ID, f"🗺️ Route map rebuilt for depth {self.last_depth}. {len(self.rotas_viaveis)} routes found."))

    def _get_pair_details(self, coin_from, coin_to):
        pair_v1 = f"{coin_from}/{coin_to}"
        if pair_v1 in self.markets: return pair_v1, 'sell'
        pair_v2 = f"{coin_to}/{coin_from}"
        if pair_v2 in self.markets: return pair_v2, 'buy'
        return None, None

    def _simular_trade_com_slippage(self, cycle_path, investimento_inicial):
        try:
            if not self.order_books:
                return None

            valor_simulado = investimento_inicial
            order_books_cache = self.order_books
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                if not pair_id or pair_id not in order_books_cache: return None

                order_book = order_books_cache[pair_id]

                if side == 'buy':
                    valor_a_gastar = valor_simulado
                    quantidade_comprada = Decimal('0')
                    for preco_str, quantidade_str in order_book['asks']:
                        preco, quantidade_disponivel = Decimal(str(preco_str)), Decimal(str(quantidade_str))
                        custo_nivel = preco * quantidade_disponivel
                        if valor_a_gastar >= custo_nivel:
                            quantidade_comprada += quantidade_disponivel
                            valor_a_gastar -= custo_nivel
                        else:
                            if preco == 0: break
                            qtd_a_comprar = valor_a_gastar / preco
                            quantidade_comprada += qtd_a_comprar
                            valor_a_gastar = Decimal('0')
                            break
                    if valor_a_gastar > 0:
                        return None
                    valor_simulado = quantidade_comprada
                else:
                    quantidade_a_vender = valor_simulado
                    valor_recebido = Decimal('0')
                    for preco_str, quantidade_str in order_book['bids']:
                        preco, quantidade_disponivel = Decimal(str(preco_str)), Decimal(str(quantidade_str))
                        if quantidade_a_vender >= quantidade_disponivel:
                            valor_recebido += quantidade_disponivel * preco
                            quantidade_a_vender -= quantidade_disponivel
                        else:
                            valor_recebido += quantidade_a_vender * preco
                            quantidade_a_vender = Decimal('0')
                            break
                    if quantidade_a_vender > 0:
                        return None
                    valor_simulado = valor_recebido
                valor_simulado *= (1 - TAXA_TAKER)

            lucro_bruto = valor_simulado - investimento_inicial
            if investimento_inicial == 0: return Decimal('0')
            return (lucro_bruto / investimento_inicial) * 100
        except Exception as e:
            raise Exception(f"Error in simulation for route {' -> '.join(cycle_path)}: {e}")

    async def _executar_trade_async(self, cycle_path, volume_a_usar):
        base_moeda = cycle_path[0]
        asyncio.create_task(bot.send_message(CHAT_ID, f"🚀 **LIVE MODE** 🚀\nStarting route execution: `{' -> '.join(cycle_path)}`\nPlanned Investment: `{volume_a_usar:.8f} {base_moeda}`", parse_mode="Markdown"))

        moedas_presas = []
        current_asset = base_moeda

        try:
            live_balance = await self.exchange.fetch_balance()
            current_amount = Decimal(str(live_balance.get(current_asset, {}).get('free', '0'))) * MARGEM_DE_SEGURANCA
            if current_amount < MINIMO_ABSOLUTO_DO_VOLUME:
                await bot.send_message(CHAT_ID, f"❌ **ROUTE FAILED!** Balance of `{current_amount:.2f} {current_asset}` is below the minimum for trade (`{MINIMO_ABSOLUTO_DO_VOLUME:.2f} {current_asset}`).", parse_mode="Markdown")
                return

            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]

                pair_id, side = self._get_pair_details(coin_from, coin_to)
                if not pair_id: raise Exception(f"Invalid pair {coin_from}/{coin_to}")

                if side == 'buy':
                    ticker = await self.exchange.fetch_ticker(pair_id)
                    price_to_use = Decimal(str(ticker['ask']))
                    if price_to_use == 0: raise Exception(f"Invalid 'ask' price (zero) for pair {pair_id}.")
                    amount_to_buy = current_amount / price_to_use
                    trade_volume_precisao = self.exchange.amount_to_precision(pair_id, float(amount_to_buy))
                    logging.info(f"DEBUG: Attempting to buy {trade_volume_precisao} {coin_to} with {current_amount} {coin_from} on pair {pair_id}")
                    await bot.send_message(CHAT_ID, f"⏳ Step {i+1}/{len(cycle_path)-1}: Trading {current_amount:.4f} {coin_from} for {coin_to} on pair {pair_id.replace('/', '_')}.")
                    order = await self.exchange.create_market_buy_order(pair_id, trade_volume_precisao)

                else:
                    trade_volume = self.exchange.amount_to_precision(pair_id, float(current_amount))
                    logging.info(f"DEBUG: Attempting to sell with {trade_volume} {coin_from} for {coin_to} on pair {pair_id}")
                    await bot.send_message(CHAT_ID, f"⏳ Step {i+1}/{len(cycle_path)-1}: Trading {current_amount:.4f} {coin_from} for {coin_to} on pair {pair_id.replace('/', '_')}.")
                    order = await self.exchange.create_market_sell_order(pair_id, trade_volume)

                await asyncio.sleep(2.5)
                order_status = await self.exchange.fetch_order(order['id'], pair_id)
                if order_status['status'] != 'closed': raise Exception(f"Order {order['id']} was not fully filled. Status: {order_status['status']}")

                live_balance = await self.exchange.fetch_balance()
                current_amount = Decimal(str(live_balance.get(coin_to, {}).get('free', '0')))
                current_asset = coin_to
                moedas_presas.append({'symbol': current_asset, 'amount': current_amount})

        except Exception as leg_error:
            logging.critical(f"LEG FAILED {i+1} ({coin_from}->{coin_to}): {leg_error}")
            mensagem_detalhada = f"Error on leg {i+1} of the route: `{leg_error}`"
            await bot.send_message(CHAT_ID, f"🔴 **ROUTE FAILED!**\n{mensagem_detalhada}", parse_mode="Markdown")

            if moedas_presas:
                ativo_preso_details = moedas_presas[-1]
                ativo_symbol = ativo_preso_details['symbol']
                await bot.send_message(CHAT_ID, f"⚠️ **CAPITAL STUCK!**\nAsset: `{ativo_symbol}`.\n**Initiating emergency sell back to {base_moeda}...**", parse_mode="Markdown")

                try:
                    await asyncio.sleep(5)
                    live_balance = await self.exchange.fetch_balance()
                    ativo_amount = Decimal(str(live_balance.get(ativo_symbol, {}).get('free', '0')))
                    if ativo_amount == 0: raise Exception("Real balance of the stuck asset is zero. Cannot rescue.")

                    reversal_pair, reversal_side = self._get_pair_details(ativo_symbol, base_moeda)
                    if not reversal_pair: raise Exception(f"Reversal pair {ativo_symbol}/{base_moeda} not found.")

                    if reversal_side == 'buy':
                        reversal_amount = self.exchange.amount_to_precision(reversal_pair, float(ativo_amount))
                        await self.exchange.create_market_buy_order(reversal_pair, reversal_amount)
                    else:
                        reversal_amount = self.exchange.amount_to_precision(reversal_pair, float(ativo_amount))
                        await self.exchange.create_market_sell_order(reversal_pair, reversal_amount)

                    await bot.send_message(CHAT_ID, f"✅ **Emergency Sell EXECUTED!** Rescued: `{Decimal(str(reversal_amount)):.8f} {ativo_symbol}`", parse_mode="Markdown")
                except Exception as reversal_error:
                    await bot.send_message(CHAT_ID, f"❌ **CRITICAL FAILURE IN EMERGENCY SELL:** `{reversal_error}`. **CHECK ACCOUNT MANUALLY!**", parse_mode="Markdown")
            return

        live_balance_final = await self.exchange.fetch_balance()
        final_amount = Decimal(str(live_balance_final.get(base_moeda, {}).get('free', '0')))
        lucro_real_usdt = final_amount - volume_a_usar
        if volume_a_usar == 0: lucro_real_percent = Decimal('0')
        else: lucro_real_percent = (lucro_real_usdt / volume_a_usar) * 100

        await bot.send_message(CHAT_ID, f"✅ **SUCCESS! Route Completed.**\nRoute: `{' -> '.join(cycle_path)}`\nProfit: `{lucro_real_usdt:.4f} {base_moeda}` (`{lucro_real_percent:.4f}%`)", parse_mode="Markdown")

    async def _manage_websocket_task(self, symbol):
        """Gerencia o ciclo de vida de uma única tarefa de WebSocket com backoff exponencial."""
        attempts = 0
        while attempts < MAX_RECONNECT_ATTEMPTS:
            try:
                orderbook = await asyncio.wait_for(self.exchange.watch_order_book(symbol), timeout=API_TIMEOUT_SECONDS)
                self.order_books[symbol] = orderbook
                attempts = 0
            except asyncio.TimeoutError:
                attempts += 1
                delay = 2 ** attempts
                logging.warning(f"⚠️ Timeout for pair {symbol}. Attempt {attempts}/{MAX_RECONNECT_ATTEMPTS}. Next try in {delay}s.")
                await asyncio.sleep(delay)
            except Exception as e:
                attempts += 1
                delay = 2 ** attempts
                logging.critical(f"❌ CRITICAL ERROR for pair {symbol}: {e}. Next try in {delay}s.")
                await asyncio.sleep(delay)

        logging.critical(f"❌ PERSISTENT ERROR: Pair {symbol} failed {MAX_RECONNECT_ATTEMPTS} consecutive times. Removing from monitoring list.")
        self.problematic_pairs[symbol] = {'timestamp': datetime.now()}
        
        if symbol in self.order_books:
            del self.order_books[symbol]

    async def run_arbitrage_loop_inner(self):
        """O loop de arbitragem que pode falhar e ser reiniciado."""
        logging.info("Starting main arbitrage loop...")
        self.construir_rotas()
        
        last_problem_check = datetime.now()
        
        while True:
            if not state['is_running']:
                logging.info("Bot paused. Waiting for command to resume...")
                while not state['is_running']:
                    await asyncio.sleep(1)
                logging.info("Bot resumed. Continuing operation.")
            
            if datetime.now() - last_problem_check > timedelta(minutes=PROBLEM_PAIRS_COOLDOWN_MINUTES):
                logging.info("Re-evaluating problematic pairs...")
                problematic_to_reactivate = []
                for pair, info in self.problematic_pairs.items():
                    if datetime.now() - info['timestamp'] > timedelta(minutes=PROBLEM_PAIRS_COOLDOWN_MINUTES):
                        problematic_to_reactivate.append(pair)
                
                for pair in problematic_to_reactivate:
                    del self.problematic_pairs[pair]
                    logging.info(f"Pair {pair} will be reactivated for monitoring.")
                last_problem_check = datetime.now()

            volumes_a_usar = {}
            balance = await self.exchange.fetch_balance()
            for moeda in MOEDAS_BASE_OPERACIONAIS:
                saldo_disponivel = Decimal(str(balance.get(moeda, {}).get('free', '0')))
                volumes_a_usar[moeda] = (saldo_disponivel * (state['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA
            
            if self.last_depth != state['max_depth']:
                self.construir_rotas()

            required_pairs = set()
            for rota in self.rotas_viaveis:
                for i in range(len(rota) - 1):
                    pair_id, _ = self._get_pair_details(rota[i], rota[i+1])
                    if pair_id and pair_id not in self.problematic_pairs:
                        required_pairs.add(pair_id)
            
            for pair in required_pairs:
                if pair not in self.websocket_tasks or self.websocket_tasks[pair].done():
                    if pair in self.websocket_tasks:
                        logging.warning(f"WS task for {pair} finished unexpectedly. Restarting...")
                    self.websocket_tasks[pair] = asyncio.create_task(self._manage_websocket_task(pair))

            stale_tasks = [pair for pair in self.websocket_tasks if pair not in required_pairs and not self.websocket_tasks[pair].done()]
            for pair in stale_tasks:
                self.websocket_tasks[pair].cancel()
                del self.websocket_tasks[pair]
            
            await asyncio.sleep(0.5)

            if self.order_books:
                for cycle_tuple in self.rotas_viaveis:
                    base_moeda_da_rota = cycle_tuple[0]
                    volume_da_rota = volumes_a_usar.get(base_moeda_da_rota, Decimal('0'))

                    if volume_da_rota < MINIMO_ABSOLUTO_DO_VOLUME:
                        continue

                    resultado = self._simular_trade_com_slippage(list(cycle_tuple), volume_da_rota)
                    
                    if resultado is not None and resultado > state['min_profit']:
                        msg = f"✅ **OPPORTUNITY**\nProfit: `{resultado:.4f}%`\nRoute: `{' -> '.join(cycle_tuple)}`"
                        logging.info(msg)
                        asyncio.create_task(bot.send_message(CHAT_ID, msg, parse_mode="Markdown"))

                        if not state['dry_run']:
                            logging.info("LIVE MODE: Executing trade...")
                            await self._executar_trade_async(cycle_tuple, volume_da_rota)
                        else:
                            logging.info("SIMULATION MODE: Opportunity not executed.")
                        
                        logging.info("Pausing for 60s after opportunity for market stabilization.")
                        await asyncio.sleep(60)
                        break
            
            await asyncio.sleep(1)

    async def run_arbitrage_loop_outer(self):
        """Função que gerencia o loop principal e reinicia em caso de falha."""
        while True:
            try:
                await self.run_arbitrage_loop_inner()
            except Exception as e:
                error_trace = traceback.format_exc()
                logging.critical(f"❌ FATAL ERROR! The engine crashed. Restarting in 15 seconds...\nDetails: {e}\n\n{error_trace}")
                try:
                    await bot.send_message(CHAT_ID, f"🔴 **CRITICAL ERROR! The engine crashed.**\nDetails: `{e}`\n\n```\n{error_trace}\n```\n\n**Attempting to restart the engine...**", parse_mode="Markdown")
                except Exception as alert_e:
                    logging.error(f"Failed to send error alert: {alert_e}")
                
                for task in list(self.websocket_tasks.values()):
                    if not task.done():
                        task.cancel()
                self.websocket_tasks.clear()
                self.order_books.clear()
                self.problematic_pairs.clear()

                await asyncio.sleep(15)

async def main():
    """Função principal que inicia o bot e o loop de arbitragem."""
    try:
        logging.info("Starting bot v35.0 (Arbitrage Bot)...")
        global bot, exchange, engine
        
        # 1. Initialize Bot and Exchange
        bot = AsyncTeleBot(TOKEN)
        exchange = ccxt.okx({
            'apiKey': OKX_API_KEY,
            'secret': OKX_API_SECRET,
            'password': OKX_API_PASSWORD,
            'options': {'defaultType': 'spot'},
            'timeout': API_TIMEOUT_SECONDS * 1000
        })
        
        await exchange.load_markets()
        logging.info("Telebot and CCXT libraries initialized successfully.")
        
        # 2. Setup Log Handler
        telegram_handler = TelegramHandler(bot, CHAT_ID, asyncio.get_event_loop(), level=logging.CRITICAL)
        logging.getLogger().addHandler(telegram_handler)

        # 3. Setup Command Handlers
        setup_handlers(bot)

        # 4. Initialize Arbitrage Engine
        engine = ArbitrageEngine(exchange, asyncio.get_event_loop())

        logging.info("Bot and exchange initialized. Starting arbitrage and telegram polling tasks.")
        
        # 5. Run the core tasks
        await asyncio.gather(
            engine.run_arbitrage_loop_outer(),
            bot.polling(none_stop=True)
        )
        
    except Exception as e:
        logging.critical(f"❌ A fatal error occurred during bot execution: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
